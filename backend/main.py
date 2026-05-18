import asyncio
import base64
import hashlib
import hmac
import json
import os
import secrets
import string
import uuid
from datetime import datetime, timedelta
from typing import Optional

import httpx
from dotenv import load_dotenv

load_dotenv()

from fastapi import BackgroundTasks, Depends, FastAPI, Header, HTTPException, Query, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, RedirectResponse
from pydantic import BaseModel
from sqlalchemy import func
from sqlalchemy.orm import Session

from database import get_db, init_db
from models import Ad, ApiKey, DownloadEvent, Part, SiteSetting, StorageProvider, Upload
from storage import B2Storage, BunnyStorage, S3Storage

# ── Config ────────────────────────────────────────────────────────────────────

MAX_BYTES    = int(os.getenv("MAX_FILE_SIZE_GB", "10")) * 1_073_741_824
API_KEY      = os.getenv("API_KEY", "")
FRONTEND_DIR = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "frontend"))
IMPORT_CHUNK     = 32 * 1024 * 1024   # 32 MB per B2 part
IMPORT_WORKERS   = 4                   # parallel S3 upload workers
IMPORT_QUEUE_MAX = 8                   # max buffered parts in queue
IMPORT_READ_SIZE = 2 * 1024 * 1024    # 2 MB HTTP read chunks

WORKER_URL    = os.getenv("WORKER_URL", "").rstrip("/")   # e.g. https://datadock-dl.abc.workers.dev
WORKER_SECRET = os.getenv("WORKER_SECRET", "")
WORKER_TOKEN_TTL = 300  # seconds — token expires after 5 minutes

_import_progress:   dict = {}    # upload_id -> progress dict
_storage_cache:     dict = {}    # provider_id -> S3Storage instance
_geo_cache:         dict = {}    # ip -> (country, country_code)
_geoip_reader             = None  # geoip2 Reader singleton
_recent_downloads:  dict = {}    # (ip, upload_id) -> last download datetime
_dl_tokens:         dict = {}    # token -> {share_id, expires}

GEOIP_DB_PATH      = os.getenv("GEOIP_DB_PATH", "/app/backend/data/GeoLite2-Country.mmdb")
DEDUP_WINDOW_SECS  = 3600  # same IP + same file within 1 hour = duplicate


def _parse_ua(ua: str) -> tuple:
    """Return (os_name, device_type) from a User-Agent string."""
    ua = ua or ""
    if "iPad" in ua or ("Tablet" in ua and "Android" in ua):
        device = "Tablet"
    elif "iPhone" in ua or "iPod" in ua or ("Android" in ua and "Mobile" in ua):
        device = "Mobile"
    elif "Android" in ua and "Mobile" not in ua:
        device = "Tablet"
    else:
        device = "Desktop"

    if "iPhone" in ua or "iPad" in ua or "iPod" in ua:
        os_name = "iOS"
    elif "Android" in ua:
        os_name = "Android"
    elif "Windows" in ua:
        os_name = "Windows"
    elif "CrOS" in ua:
        os_name = "ChromeOS"
    elif "Mac OS X" in ua:
        os_name = "macOS"
    elif "Linux" in ua:
        os_name = "Linux"
    else:
        os_name = "Unknown"

    return os_name, device


def _get_geoip_reader():
    """Return a cached geoip2 Reader, or None if the DB file is not present."""
    global _geoip_reader
    if _geoip_reader is not None:
        return _geoip_reader
    try:
        import geoip2.database
        _geoip_reader = geoip2.database.Reader(GEOIP_DB_PATH)
    except Exception:
        pass
    return _geoip_reader


def _geolocate(ip: str) -> tuple:
    """Return (country, country_code) using the local MaxMind GeoLite2 DB.
    Falls back to ('Unknown', '??') if the DB is missing or the IP is private.
    Results are cached in memory — no rate limits, no network calls."""
    if not ip or ip in ("127.0.0.1", "::1", ""):
        return "Local", "??"
    if ip in _geo_cache:
        return _geo_cache[ip]
    reader = _get_geoip_reader()
    if reader is None:
        return "Unknown", "??"
    try:
        response = reader.country(ip)
        result = (
            response.country.name or "Unknown",
            response.country.iso_code or "??",
        )
    except Exception:
        result = ("Unknown", "??")
    _geo_cache[ip] = result
    return result


async def _log_download_event(upload_id: str, filename: str, ip: str, ua: str):
    """Background task: resolve geolocation + UA then persist a DownloadEvent.
    DB-level dedup guard catches duplicates that slip past the in-memory check
    (e.g. second uvicorn worker seeing the same request)."""
    from database import SessionLocal
    os_name, device_type = _parse_ua(ua)
    country, country_code = _geolocate(ip)
    db = SessionLocal()
    try:
        cutoff = datetime.utcnow() - timedelta(seconds=DEDUP_WINDOW_SECS)
        already = (
            db.query(DownloadEvent.id)
            .filter(
                DownloadEvent.upload_id == upload_id,
                DownloadEvent.ip == ip,
                DownloadEvent.created_at >= cutoff,
            )
            .first()
        )
        if already:
            return
        db.add(DownloadEvent(
            id=str(uuid.uuid4()),
            upload_id=upload_id,
            filename=filename,
            ip=ip,
            country=country,
            country_code=country_code,
            device_type=device_type,
            os_name=os_name,
            user_agent=(ua or "")[:1000],
            created_at=datetime.utcnow(),
        ))
        db.commit()
    except Exception:
        db.rollback()
    finally:
        db.close()


def _gen_worker_token(file_key: str, filename: str, content_type: str,
                      token_type: str = "r2", url: Optional[str] = None) -> str:
    """Return a short-lived HMAC-SHA256-signed token for the Cloudflare Worker.
    token_type='r2'  → Worker fetches from R2 binding using file_key.
    token_type='url' → Worker proxies from the given signed URL."""
    expiry = int((datetime.utcnow() + timedelta(seconds=WORKER_TOKEN_TTL)).timestamp())
    payload: dict = {
        "t": token_type,
        "f": filename,
        "c": content_type or "application/octet-stream",
        "e": expiry,
    }
    if token_type == "r2":
        payload["k"] = file_key
    else:
        payload["u"] = url
    payload_b64 = base64.urlsafe_b64encode(
        json.dumps(payload, separators=(",", ":")).encode()
    ).rstrip(b"=").decode()
    sig = hmac.new(WORKER_SECRET.encode(), payload_b64.encode(), hashlib.sha256).hexdigest()
    return f"{payload_b64}.{sig}"


def _make_worker_redirect(upload, db: Session) -> str:
    """Mirror of _chained_download_url but wraps the result in a Worker token URL.
    Bandwidth tracking runs here so stats remain accurate."""
    file_key = upload.b2_file_key

    if not upload.storage_provider_id:
        token = _gen_worker_token(file_key, upload.filename, upload.content_type, "r2")
        return f"{WORKER_URL}/dl/{token}"

    current_month = datetime.utcnow().strftime("%Y-%m")
    visited: set = set()
    pid = upload.storage_provider_id

    while pid and pid not in visited:
        visited.add(pid)
        p = db.query(StorageProvider).filter(StorageProvider.id == pid).first()
        if not p:
            break

        if p.bandwidth_reset_month != current_month:
            p.monthly_bandwidth_used = 0
            p.bandwidth_reset_month = current_month

        cap_bytes = int(p.bandwidth_cap_gb * 1_073_741_824) if p.bandwidth_cap_gb else None
        used = p.monthly_bandwidth_used or 0
        cap_exceeded = bool(cap_bytes and used >= cap_bytes)

        if cap_exceeded:
            if p.fallback_provider_id:
                pid = p.fallback_provider_id
                continue
            if p.fallback_base_url:
                fallback_url = f"{p.fallback_base_url.rstrip('/')}/{file_key}"
                token = _gen_worker_token(file_key, upload.filename, upload.content_type, "url", fallback_url)
                return f"{WORKER_URL}/dl/{token}"

        p.monthly_bandwidth_used = used + (upload.file_size or 0)

        is_r2 = "r2.cloudflarestorage" in p.endpoint_url.lower()
        if is_r2:
            token = _gen_worker_token(file_key, upload.filename, upload.content_type, "r2")
        else:
            try:
                presigned = _get_storage(p.id, db).get_presigned_url(file_key, WORKER_TOKEN_TTL)
            except Exception:
                presigned = _get_storage(p.id, db).get_download_url(file_key)
            token = _gen_worker_token(file_key, upload.filename, upload.content_type, "url", presigned)
        return f"{WORKER_URL}/dl/{token}"

    # Safety net — assume R2
    token = _gen_worker_token(file_key, upload.filename, upload.content_type, "r2")
    return f"{WORKER_URL}/dl/{token}"


def _get_storage(provider_id: Optional[str], db: Session) -> S3Storage:
    """Return the correct S3Storage for a given provider_id.
    Falls back to the env-based storage when provider_id is None (legacy uploads)."""
    if not provider_id:
        return storage
    if provider_id in _storage_cache:
        return _storage_cache[provider_id]
    p = db.query(StorageProvider).filter(StorageProvider.id == provider_id).first()
    if not p:
        return storage

    from urllib.parse import urlparse as _urlparse
    _ep = p.endpoint_url.lower()
    if "bunnycdn.com" in _ep or "b-cdn.net" in _ep:
        region = _urlparse(p.endpoint_url).hostname or p.endpoint_url
        inst = BunnyStorage(
            zone=p.bucket_name,
            api_key=p.application_key,
            region=region,
            public_base_url=p.public_base_url or "",
        )
    else:
        inst = S3Storage(
            endpoint_url=p.endpoint_url,
            key_id=p.key_id,
            application_key=p.application_key,
            bucket_name=p.bucket_name,
            public_base_url=p.public_base_url or "",
        )
    _storage_cache[provider_id] = inst
    return inst


def _get_default_provider(db: Session) -> Optional[StorageProvider]:
    return (
        db.query(StorageProvider)
        .filter(StorageProvider.is_default == 1, StorageProvider.active == 1)
        .first()
    )

# ── App ───────────────────────────────────────────────────────────────────────

app = FastAPI(title="DataDock", version="2.0.0", docs_url=None, redoc_url=None)
storage = B2Storage()

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


def _seed_master_key():
    """On first startup, seed the env API_KEY into the DB as the master admin key."""
    if not API_KEY:
        return
    from database import SessionLocal
    db = SessionLocal()
    try:
        if db.query(ApiKey).filter(ApiKey.is_master == 1).first():
            return
        db.add(ApiKey(
            id=str(uuid.uuid4()),
            name="Admin",
            key=API_KEY,
            role="admin",
            is_master=1,
            active=1,
            created_at=datetime.utcnow(),
        ))
        db.commit()
    except Exception:
        db.rollback()
    finally:
        db.close()


@app.on_event("startup")
async def _startup():
    init_db()
    _seed_master_key()


def _share_id() -> str:
    return "".join(secrets.choice(string.ascii_letters + string.digits) for _ in range(8))


# ── Auth ──────────────────────────────────────────────────────────────────────


def require_auth(x_api_key: Optional[str] = Header(None), db: Session = Depends(get_db)):
    """Return {'role': ..., 'name': ...} for any valid key, or raise 401."""
    if not x_api_key:
        raise HTTPException(401, "API key required")
    # DB-stored keys take priority (master + team keys)
    key_obj = db.query(ApiKey).filter(ApiKey.key == x_api_key, ApiKey.active == 1).first()
    if key_obj:
        return {"role": key_obj.role, "name": key_obj.name}
    # Env key as emergency recovery — only if no master key exists in DB (DB wiped / first run)
    master_exists = db.query(ApiKey).filter(ApiKey.is_master == 1).first()
    if not master_exists and API_KEY and x_api_key == API_KEY:
        return {"role": "admin", "name": "Admin"}
    # Dev mode: no API_KEY set → allow anything as admin
    if not API_KEY:
        return {"role": "admin", "name": "Admin"}
    raise HTTPException(401, "Unauthorized")


def require_admin(auth: dict = Depends(require_auth)):
    """Dependency that restricts an endpoint to admin-role keys only."""
    if auth["role"] != "admin":
        raise HTTPException(403, "Admin access required")
    return auth


def get_current_key(x_api_key: Optional[str] = Header(None), db: Session = Depends(get_db)) -> Optional[ApiKey]:
    """Return the ApiKey ORM object for the request (None for env-key / unauthenticated)."""
    if not x_api_key:
        return None
    return db.query(ApiKey).filter(ApiKey.key == x_api_key, ApiKey.active == 1).first()


# ── Schemas ───────────────────────────────────────────────────────────────────


class InitUploadIn(BaseModel):
    filename: str
    file_size: int
    file_hash: str
    content_type: str = "application/octet-stream"


class BrowserRelayInitIn(BaseModel):
    filename: str
    content_type: str = "application/octet-stream"


class CompleteUploadIn(BaseModel):
    actual_size: Optional[int] = None


# ── Upload API ────────────────────────────────────────────────────────────────


@app.post("/api/upload/init")
async def init_upload(
    body: InitUploadIn,
    db: Session = Depends(get_db),
    _=Depends(require_auth),
    auth_key: Optional[ApiKey] = Depends(get_current_key),
):
    if body.file_size > MAX_BYTES:
        raise HTTPException(400, f"File exceeds {os.getenv('MAX_FILE_SIZE_GB', 10)} GB limit")
    if body.file_size <= 0:
        raise HTTPException(400, "Invalid file size")

    existing = (
        db.query(Upload)
        .filter(Upload.file_hash == body.file_hash, Upload.status == "pending")
        .first()
    )
    if existing:
        try:
            b2_parts = _get_storage(existing.storage_provider_id, db).list_uploaded_parts(
                existing.b2_file_key, existing.b2_upload_id
            )
            known = {p.part_number for p in db.query(Part).filter(Part.upload_id == existing.id).all()}
            for bp in b2_parts:
                if bp["part_number"] not in known:
                    db.add(Part(
                        id=str(uuid.uuid4()),
                        upload_id=existing.id,
                        part_number=bp["part_number"],
                        etag=bp["etag"],
                        uploaded_at=datetime.utcnow(),
                    ))
            db.commit()
        except Exception:
            pass

        parts = db.query(Part).filter(Part.upload_id == existing.id).all()
        return {
            "upload_id": existing.id,
            "resuming": True,
            "completed_parts": [{"part_number": p.part_number, "etag": p.etag} for p in parts],
        }

    safe_name = os.path.basename(body.filename).replace("\0", "") or "unnamed"
    file_key = f"uploads/{uuid.uuid4()}/{safe_name}"

    default_prov = _get_default_provider(db)
    provider_id  = default_prov.id if default_prov else None
    file_storage = _get_storage(provider_id, db)
    b2_upload_id = file_storage.create_multipart_upload(file_key, body.content_type)

    upload = Upload(
        id=str(uuid.uuid4()),
        share_id=_share_id(),
        file_hash=body.file_hash,
        filename=safe_name,
        file_size=body.file_size,
        content_type=body.content_type,
        b2_upload_id=b2_upload_id,
        b2_file_key=file_key,
        status="pending",
        views=0,
        downloads=0,
        storage_provider_id=provider_id,
        uploaded_by_key_id=auth_key.id if auth_key else None,
        created_at=datetime.utcnow(),
    )
    db.add(upload)
    db.commit()
    return {"upload_id": upload.id, "resuming": False, "completed_parts": []}


@app.post("/api/upload/relay-init")
async def relay_init(
    body: BrowserRelayInitIn,
    db: Session = Depends(get_db),
    _=Depends(require_auth),
    auth_key: Optional[ApiKey] = Depends(get_current_key),
):
    """Create an upload slot for browser-side relay (no file_size required)."""
    safe_name = os.path.basename(body.filename).replace("\0", "") or "unnamed"
    file_key = f"uploads/{uuid.uuid4()}/{safe_name}"
    default_prov = _get_default_provider(db)
    provider_id = default_prov.id if default_prov else None
    file_storage = _get_storage(provider_id, db)
    try:
        b2_upload_id = file_storage.create_multipart_upload(file_key, body.content_type)
    except Exception as exc:
        raise HTTPException(500, f"Storage error: {exc}")

    upload = Upload(
        id=str(uuid.uuid4()),
        share_id=_share_id(),
        file_hash="",
        filename=safe_name,
        file_size=0,
        content_type=body.content_type,
        b2_upload_id=b2_upload_id,
        b2_file_key=file_key,
        status="pending",
        views=0,
        downloads=0,
        storage_provider_id=provider_id,
        uploaded_by_key_id=auth_key.id if auth_key else None,
        created_at=datetime.utcnow(),
    )
    db.add(upload)
    db.commit()
    return {"upload_id": upload.id}


@app.post("/api/upload/{upload_id}/chunk/{part_number}")
async def upload_chunk(
    upload_id: str,
    part_number: int,
    request: Request,
    db: Session = Depends(get_db),
    _=Depends(require_auth),
):
    if not (1 <= part_number <= 10_000):
        raise HTTPException(400, "part_number must be 1–10000")

    upload = db.query(Upload).filter(Upload.id == upload_id).first()
    if not upload:
        raise HTTPException(404, "Upload not found")
    if upload.status != "pending":
        raise HTTPException(400, f"Upload is {upload.status}")

    data = await request.body()
    if not data:
        raise HTTPException(400, "Empty chunk")

    etag = _get_storage(upload.storage_provider_id, db).upload_part(
        upload.b2_file_key, upload.b2_upload_id, part_number, data
    )

    existing = (
        db.query(Part)
        .filter(Part.upload_id == upload_id, Part.part_number == part_number)
        .first()
    )
    if existing:
        existing.etag = etag
        existing.uploaded_at = datetime.utcnow()
    else:
        db.add(Part(
            id=str(uuid.uuid4()),
            upload_id=upload_id,
            part_number=part_number,
            etag=etag,
            uploaded_at=datetime.utcnow(),
        ))
    db.commit()
    return {"ok": True, "etag": etag}


@app.post("/api/upload/{upload_id}/complete")
async def complete_upload(
    upload_id: str,
    body: Optional[CompleteUploadIn] = None,
    db: Session = Depends(get_db),
    _=Depends(require_auth),
):
    upload = db.query(Upload).filter(Upload.id == upload_id).first()
    if not upload:
        raise HTTPException(404, "Upload not found")
    if upload.status != "pending":
        raise HTTPException(400, f"Upload is already {upload.status}")

    db_parts = db.query(Part).filter(Part.upload_id == upload_id).all()
    if not db_parts:
        raise HTTPException(400, "No parts uploaded yet")

    if not upload.share_id:
        upload.share_id = _share_id()

    parts = [{"part_number": p.part_number, "etag": p.etag} for p in db_parts]
    upload.status = "completing"
    db.commit()

    file_storage = _get_storage(upload.storage_provider_id, db)
    try:
        file_storage.complete_multipart_upload(upload.b2_file_key, upload.b2_upload_id, parts)
    except Exception as exc:
        upload.status = "failed"
        db.commit()
        raise HTTPException(500, f"Storage complete failed: {exc}") from exc

    upload.status = "completed"
    upload.completed_at = datetime.utcnow()
    if body and body.actual_size:
        upload.file_size = body.actual_size
    db.commit()

    return {
        "ok": True,
        "share_url": f"/f/{upload.share_id}",
        "direct_url": file_storage.get_download_url(upload.b2_file_key),
        "filename": upload.filename,
        "file_size": upload.file_size,
    }


@app.delete("/api/upload/{upload_id}")
async def abort_upload(
    upload_id: str,
    db: Session = Depends(get_db),
    _=Depends(require_auth),
):
    upload = db.query(Upload).filter(Upload.id == upload_id).first()
    if not upload:
        raise HTTPException(404, "Upload not found")
    # Signal any running import background task to stop
    if upload_id in _import_progress:
        _import_progress[upload_id]["status"] = "aborted"
    if upload.status == "pending":
        _get_storage(upload.storage_provider_id, db).abort_multipart_upload(
            upload.b2_file_key, upload.b2_upload_id
        )
    upload.status = "aborted"
    db.commit()
    return {"ok": True}


# ── File manager API ──────────────────────────────────────────────────────────


@app.get("/api/files")
async def list_files(
    db: Session = Depends(get_db),
    auth: dict = Depends(require_auth),
    auth_key: Optional[ApiKey] = Depends(get_current_key),
    all_files: bool = Query(False, alias="all"),
    limit: Optional[int] = Query(None, ge=1, le=500),
):
    query = db.query(Upload).filter(Upload.status == "completed")
    is_admin = auth["role"] == "admin"

    if not is_admin and not all_files:
        # Members see only their own uploads unless requesting all
        if auth_key:
            query = query.filter(Upload.uploaded_by_key_id == auth_key.id)
        else:
            return []

    q = query.order_by(Upload.completed_at.desc())
    rows = (q.limit(limit).all() if limit else q.all())

    # Provider name lookup
    pids = {f.storage_provider_id for f in rows if f.storage_provider_id}
    providers = {}
    if pids:
        for p in db.query(StorageProvider).filter(StorageProvider.id.in_(pids)).all():
            providers[p.id] = p.name

    # Uploader name lookup (admin always; members when requesting all files)
    uploader_names: dict = {}
    if is_admin or all_files:
        key_ids = {f.uploaded_by_key_id for f in rows if f.uploaded_by_key_id}
        if key_ids:
            for k in db.query(ApiKey).filter(ApiKey.id.in_(key_ids)).all():
                uploader_names[k.id] = k.name

    return [
        {
            "id": f.id,
            "share_id": f.share_id,
            "filename": f.filename,
            "file_size": f.file_size,
            "direct_url": _get_storage(f.storage_provider_id, db).get_download_url(f.b2_file_key),
            "share_url": f"/f/{f.share_id}" if f.share_id else None,
            "views": f.views or 0,
            "downloads": f.downloads or 0,
            "storage_name": providers.get(f.storage_provider_id, "Default (env)"),
            "completed_at": f.completed_at.isoformat() if f.completed_at else None,
            "uploaded_by": uploader_names.get(f.uploaded_by_key_id) if f.uploaded_by_key_id else None,
        }
        for f in rows
    ]


@app.delete("/api/files/{file_id}")
async def delete_file(file_id: str, db: Session = Depends(get_db), _=Depends(require_admin)):
    upload = (
        db.query(Upload)
        .filter(Upload.id == file_id, Upload.status == "completed")
        .first()
    )
    if not upload:
        raise HTTPException(404, "File not found")
    try:
        _get_storage(upload.storage_provider_id, db).delete_object(upload.b2_file_key)
    except Exception:
        pass
    db.delete(upload)
    db.commit()
    return {"ok": True}


@app.post("/api/auth/verify")
async def verify_auth(auth=Depends(require_auth)):
    return {"ok": True, "role": auth["role"], "name": auth["name"]}


# ── Dashboard stats ───────────────────────────────────────────────────────────


@app.get("/api/stats")
async def get_stats(db: Session = Depends(get_db), _=Depends(require_admin)):
    # Single query for all three aggregates
    agg = db.query(
        func.count(Upload.id),
        func.coalesce(func.sum(Upload.file_size), 0),
        func.coalesce(func.sum(Upload.downloads), 0),
    ).filter(Upload.status == "completed").one()
    total_files, total_size, total_downloads = agg

    # Single GROUP BY replaces 7 individual daily count queries
    today = datetime.utcnow().date()
    week_start = datetime(today.year, today.month, today.day) - timedelta(days=6)
    daily_rows = (
        db.query(
            func.strftime("%Y-%m-%d", Upload.completed_at).label("day"),
            func.count(Upload.id).label("cnt"),
        )
        .filter(Upload.status == "completed", Upload.completed_at >= week_start)
        .group_by(func.strftime("%Y-%m-%d", Upload.completed_at))
        .all()
    )
    counts = {r.day: r.cnt for r in daily_rows}
    uploads_per_day = [
        {
            "date":  (today - timedelta(days=i)).strftime("%b %d"),
            "count": counts.get((today - timedelta(days=i)).strftime("%Y-%m-%d"), 0),
        }
        for i in range(6, -1, -1)
    ]

    return {
        "total_files": total_files,
        "total_size": total_size,
        "total_downloads": total_downloads,
        "uploads_per_day": uploads_per_day,
    }


# ── Public share / download ───────────────────────────────────────────────────


@app.get("/api/f/{share_id}")
async def get_share_info(share_id: str, db: Session = Depends(get_db)):
    upload = (
        db.query(Upload)
        .filter(Upload.share_id == share_id, Upload.status == "completed")
        .first()
    )
    if not upload:
        raise HTTPException(404, "File not found")

    upload.views = (upload.views or 0) + 1
    db.commit()

    return {
        "share_id": share_id,
        "filename": upload.filename,
        "file_size": upload.file_size,
        "content_type": upload.content_type,
        "views": upload.views,
        "downloads": upload.downloads or 0,
        "completed_at": upload.completed_at.isoformat() if upload.completed_at else None,
    }


def _chained_download_url(upload: Upload, db: Session) -> str:
    """Walk the provider fallback chain and return the best available download URL.
    Moves to the next provider when a cap is exceeded. Always returns a URL —
    if every provider in the chain is over cap, the last one serves anyway."""
    if not upload.storage_provider_id:
        return _get_storage(None, db).get_download_url(upload.b2_file_key)

    current_month = datetime.utcnow().strftime("%Y-%m")
    visited: set = set()
    pid = upload.storage_provider_id

    while pid and pid not in visited:
        visited.add(pid)
        p = db.query(StorageProvider).filter(StorageProvider.id == pid).first()
        if not p:
            break

        # Auto-reset counter on new month
        if p.bandwidth_reset_month != current_month:
            p.monthly_bandwidth_used = 0
            p.bandwidth_reset_month = current_month

        cap_bytes = int(p.bandwidth_cap_gb * 1_073_741_824) if p.bandwidth_cap_gb else None
        used = p.monthly_bandwidth_used or 0
        cap_exceeded = bool(cap_bytes and used >= cap_bytes)

        if cap_exceeded:
            # Try provider chain first, then legacy URL fallback
            next_pid = p.fallback_provider_id
            if next_pid:
                pid = next_pid
                continue
            if p.fallback_base_url:
                return f"{p.fallback_base_url.rstrip('/')}/{upload.b2_file_key}"
            # No more fallbacks — serve from this provider anyway (never block user)

        # Serve from this provider and track bandwidth
        p.monthly_bandwidth_used = used + (upload.file_size or 0)
        return _get_storage(p.id, db).get_download_url(upload.b2_file_key)

    # Safety net
    return _get_storage(upload.storage_provider_id, db).get_download_url(upload.b2_file_key)


@app.post("/api/f/{share_id}/token")
async def create_download_token(share_id: str, db: Session = Depends(get_db)):
    """Issue a short-lived single-use download token for the share page."""
    upload = (
        db.query(Upload)
        .filter(Upload.share_id == share_id, Upload.status == "completed")
        .first()
    )
    if not upload:
        raise HTTPException(404, "File not found")
    token = secrets.token_urlsafe(24)
    expires = datetime.utcnow() + timedelta(seconds=120)
    _dl_tokens[token] = {"share_id": share_id, "expires": expires}
    # Prune expired tokens to prevent unbounded growth
    now = datetime.utcnow()
    for k in [k for k, v in list(_dl_tokens.items()) if v["expires"] < now]:
        del _dl_tokens[k]
    return {"token": token}


@app.get("/api/f/{share_id}/download")
async def download_file(
    share_id: str,
    request: Request,
    background_tasks: BackgroundTasks,
    token: Optional[str] = Query(None),
    db: Session = Depends(get_db),
):
    upload = (
        db.query(Upload)
        .filter(Upload.share_id == share_id, Upload.status == "completed")
        .first()
    )
    if not upload:
        raise HTTPException(404, "File not found")

    # Require a valid single-use token — prevents hotlinking the download URL
    tok = _dl_tokens.pop(token, None) if token else None
    if tok is None or tok["share_id"] != share_id or tok["expires"] < datetime.utcnow():
        raise HTTPException(403, "Missing or expired download token. Please use the share page to download.")

    ip = request.headers.get("X-Forwarded-For", "").split(",")[0].strip()
    if not ip:
        ip = request.headers.get("X-Real-IP", "").strip()
    if not ip and request.client:
        ip = request.client.host

    # Dedup: same IP + same file within DEDUP_WINDOW_SECS counts as one download
    now = datetime.utcnow()
    dedup_key = (ip, upload.id)
    last = _recent_downloads.get(dedup_key)
    is_dup = last is not None and (now - last).total_seconds() < DEDUP_WINDOW_SECS
    if not is_dup:
        _recent_downloads[dedup_key] = now
        upload.downloads = (upload.downloads or 0) + 1
        # Prune cache when it grows large to avoid unbounded memory use
        if len(_recent_downloads) > 50_000:
            cutoff = now - timedelta(seconds=DEDUP_WINDOW_SECS)
            for k in [k for k, v in _recent_downloads.items() if v < cutoff]:
                del _recent_downloads[k]

    if WORKER_URL and WORKER_SECRET:
        download_url = _make_worker_redirect(upload, db)
    else:
        download_url = _chained_download_url(upload, db)
    db.commit()

    ua = request.headers.get("User-Agent", "")
    if not is_dup:
        background_tasks.add_task(_log_download_event, upload.id, upload.filename, ip, ua)

    return RedirectResponse(url=download_url)


# ── Download Analytics ───────────────────────────────────────────────────────


@app.get("/api/analytics/downloads")
async def get_download_analytics(
    days: int = Query(7, ge=1, le=90),
    db: Session = Depends(get_db),
    _=Depends(require_admin),
):
    from sqlalchemy import distinct as sa_distinct

    since = datetime.utcnow() - timedelta(days=days)

    total = db.query(func.count(DownloadEvent.id)).filter(DownloadEvent.created_at >= since).scalar() or 0
    unique_ips = db.query(func.count(sa_distinct(DownloadEvent.ip))).filter(DownloadEvent.created_at >= since).scalar() or 0
    unique_countries = db.query(func.count(sa_distinct(DownloadEvent.country_code))).filter(DownloadEvent.created_at >= since).scalar() or 0

    today = datetime.utcnow().date()
    per_day = []
    for i in range(days - 1, -1, -1):
        day = today - timedelta(days=i)
        day_start = datetime(day.year, day.month, day.day)
        day_end = day_start + timedelta(days=1)
        count = (
            db.query(func.count(DownloadEvent.id))
            .filter(DownloadEvent.created_at >= day_start, DownloadEvent.created_at < day_end)
            .scalar() or 0
        )
        per_day.append({"date": day.strftime("%b %d"), "count": count})

    country_rows = (
        db.query(DownloadEvent.country, DownloadEvent.country_code, func.count(DownloadEvent.id).label("cnt"))
        .filter(DownloadEvent.created_at >= since)
        .group_by(DownloadEvent.country, DownloadEvent.country_code)
        .order_by(func.count(DownloadEvent.id).desc())
        .limit(10)
        .all()
    )
    top_countries = [{"country": r.country, "country_code": r.country_code, "count": r.cnt} for r in country_rows]

    device_rows = (
        db.query(DownloadEvent.device_type, func.count(DownloadEvent.id).label("cnt"))
        .filter(DownloadEvent.created_at >= since)
        .group_by(DownloadEvent.device_type)
        .order_by(func.count(DownloadEvent.id).desc())
        .all()
    )
    devices = [{"type": r.device_type or "Unknown", "count": r.cnt} for r in device_rows]

    os_rows = (
        db.query(DownloadEvent.os_name, func.count(DownloadEvent.id).label("cnt"))
        .filter(DownloadEvent.created_at >= since)
        .group_by(DownloadEvent.os_name)
        .order_by(func.count(DownloadEvent.id).desc())
        .all()
    )
    os_breakdown = [{"os": r.os_name or "Unknown", "count": r.cnt} for r in os_rows]

    file_rows = (
        db.query(DownloadEvent.upload_id, DownloadEvent.filename, func.count(DownloadEvent.id).label("cnt"))
        .filter(DownloadEvent.created_at >= since)
        .group_by(DownloadEvent.upload_id, DownloadEvent.filename)
        .order_by(func.count(DownloadEvent.id).desc())
        .limit(10)
        .all()
    )
    top_files = []
    for r in file_rows:
        u = db.query(Upload.share_id).filter(Upload.id == r.upload_id).first()
        top_files.append({"filename": r.filename, "count": r.cnt, "share_id": u.share_id if u else None})

    recent_rows = (
        db.query(DownloadEvent)
        .filter(DownloadEvent.created_at >= since)
        .order_by(DownloadEvent.created_at.desc())
        .limit(25)
        .all()
    )
    recent = [
        {
            "filename": r.filename,
            "country": r.country,
            "country_code": r.country_code,
            "device_type": r.device_type,
            "os_name": r.os_name,
            "created_at": r.created_at.isoformat(),
        }
        for r in recent_rows
    ]

    return {
        "total": total,
        "unique_ips": unique_ips,
        "unique_countries": unique_countries,
        "per_day": per_day,
        "top_countries": top_countries,
        "devices": devices,
        "os_breakdown": os_breakdown,
        "top_files": top_files,
        "recent": recent,
    }


# ── URL Import API ────────────────────────────────────────────────────────────


class ImportIn(BaseModel):
    url: str
    filename: Optional[str] = None


@app.post("/api/import")
async def import_from_url(
    body: ImportIn,
    background_tasks: BackgroundTasks,
    db: Session = Depends(get_db),
    _=Depends(require_auth),
    auth_key: Optional[ApiKey] = Depends(get_current_key),
):
    if not body.url.startswith(("http://", "https://")):
        raise HTTPException(400, "URL must start with http:// or https://")

    # Resolve redirects and sniff metadata.
    # Strategy: try HEAD first (fast, no body); if the server blocks HEAD (405/403)
    # fall back to a Range GET for the first byte — that still resolves all redirects
    # and returns headers without downloading the whole file.
    try:
        meta, final_url = await _resolve_url_meta(body.url)
    except Exception as exc:
        raise HTTPException(400, f"Cannot reach URL: {exc}")

    content_type = (meta.get("content-type", "application/octet-stream")
                    .split(";")[0].strip() or "application/octet-stream")

    # ── HTML page detected: use headless browser to extract the real download URL ──
    if "text/html" in content_type:
        default_prov = _get_default_provider(db)
        provider_id  = default_prov.id if default_prov else None

        upload = Upload(
            id=str(uuid.uuid4()),
            share_id=_share_id(),
            file_hash="",
            filename=body.filename or "Scanning page…",
            file_size=0,
            content_type="application/octet-stream",
            b2_upload_id="",
            b2_file_key="",
            status="analyzing",
            views=0,
            downloads=0,
            storage_provider_id=provider_id,
            uploaded_by_key_id=auth_key.id if auth_key else None,
            created_at=datetime.utcnow(),
        )
        db.add(upload)
        db.commit()

        _import_progress[upload.id] = {
            "status": "analyzing",
            "bytes_done": 0,
            "total": 0,
            "error": None,
        }

        background_tasks.add_task(_do_page_import, upload.id, final_url, provider_id)

        return {
            "upload_id": upload.id,
            "filename": upload.filename,
            "total": 0,
        }

    # ── Direct file URL ──
    content_length = int(meta.get("content-length", 0) or 0)

    if content_length and content_length > MAX_BYTES:
        raise HTTPException(400, f"Remote file exceeds {os.getenv('MAX_FILE_SIZE_GB', 10)} GB limit")

    # Determine filename (user override → Content-Disposition → final URL path)
    filename = (body.filename or "").strip()
    if not filename:
        cd = meta.get("content-disposition", "")
        if "filename=" in cd:
            filename = cd.split("filename=")[-1].strip().strip('"\'')
        if not filename:
            filename = final_url.split("?")[0].rstrip("/").split("/")[-1]
        if not filename:
            filename = "imported_file"

    filename = os.path.basename(filename).replace("\0", "") or "imported_file"

    # Create multipart upload on the default storage provider
    file_key = f"uploads/{uuid.uuid4()}/{filename}"
    default_prov = _get_default_provider(db)
    provider_id  = default_prov.id if default_prov else None
    file_storage = _get_storage(provider_id, db)
    try:
        b2_upload_id = file_storage.create_multipart_upload(file_key, content_type)
    except Exception as exc:
        raise HTTPException(500, f"Storage error: {exc}")

    upload = Upload(
        id=str(uuid.uuid4()),
        share_id=_share_id(),
        file_hash="",
        filename=filename,
        file_size=content_length,
        content_type=content_type,
        b2_upload_id=b2_upload_id,
        b2_file_key=file_key,
        status="importing",
        views=0,
        downloads=0,
        storage_provider_id=provider_id,
        uploaded_by_key_id=auth_key.id if auth_key else None,
        created_at=datetime.utcnow(),
    )
    db.add(upload)
    db.commit()

    _import_progress[upload.id] = {
        "status": "importing",
        "bytes_done": 0,
        "total": content_length,
        "error": None,
    }

    background_tasks.add_task(
        _do_import, upload.id, final_url, file_key, b2_upload_id, provider_id
    )

    return {
        "upload_id": upload.id,
        "filename": filename,
        "total": content_length,
    }


@app.get("/api/import/{upload_id}/status")
async def import_status(
    upload_id: str,
    db: Session = Depends(get_db),
    _=Depends(require_auth),
):
    prog = _import_progress.get(upload_id)
    if not prog:
        upload = db.query(Upload).filter(Upload.id == upload_id).first()
        if not upload:
            raise HTTPException(404, "Import job not found")
        prog = {"status": upload.status, "bytes_done": upload.file_size, "total": upload.file_size, "error": None}

    result = dict(prog)
    if prog.get("status") == "completed":
        upload = db.query(Upload).filter(Upload.id == upload_id).first()
        if upload:
            result["share_url"] = f"/f/{upload.share_id}"
            result["direct_url"] = _get_storage(upload.storage_provider_id, db).get_download_url(upload.b2_file_key)
            result["filename"] = upload.filename
            result["file_size"] = upload.file_size
    return result


_REDIRECT_STATUSES = {301, 302, 303, 307, 308}
_MAX_REDIRECTS = 20

_BROWSER_UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/124.0.0.0 Safari/537.36"
)

# Headers for HEAD / Range-GET metadata sniffing
_META_HEADERS = {
    "User-Agent": _BROWSER_UA,
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "en-US,en;q=0.9",
    "Accept-Encoding": "identity",
}

def _download_headers(url: str) -> dict:
    """Build full browser-like headers for the actual file download.
    Derives Referer from the URL's own origin so anti-hotlink checks pass."""
    from urllib.parse import urlparse
    p = urlparse(url)
    return {
        "User-Agent": _BROWSER_UA,
        "Accept": "*/*",
        "Accept-Language": "en-US,en;q=0.9",
        "Accept-Encoding": "identity",   # no compression — we upload raw bytes
        "Referer": f"{p.scheme}://{p.netloc}/",
        "Origin": f"{p.scheme}://{p.netloc}",
        "Connection": "keep-alive",
        "Sec-Fetch-Dest": "document",
        "Sec-Fetch-Mode": "navigate",
        "Sec-Fetch-Site": "same-origin",
        "Sec-Fetch-User": "?1",
        "Upgrade-Insecure-Requests": "1",
    }


async def _resolve_url_meta(url: str) -> tuple[dict, str]:
    """Follow all redirects and return (headers-dict, final-url).

    Strategy (each step falls through on failure):
    1. HEAD  — fast, no body
    2. Range GET bytes=0-0 — resolves redirects without downloading the file
    3. Give up on metadata — return empty headers + original URL so the actual
       download can still proceed (token-protected servers often block HEAD/Range
       but serve the full GET just fine).
    """
    client_kwargs = dict(
        follow_redirects=True,
        max_redirects=_MAX_REDIRECTS,
        timeout=20.0,
        headers=_META_HEADERS,
    )
    async with httpx.AsyncClient(**client_kwargs) as client:
        # 1. Try HEAD
        try:
            resp = await client.head(url)
            if resp.status_code not in (403, 405, 501):
                resp.raise_for_status()
                return dict(resp.headers), str(resp.url)
        except (httpx.HTTPStatusError, httpx.RequestError):
            pass

        # 2. Range GET — single byte, cheap
        try:
            resp = await client.get(url, headers={**_META_HEADERS, "Range": "bytes=0-0"})
            if resp.status_code in (200, 206):
                await resp.aclose()
                return dict(resp.headers), str(resp.url)
            await resp.aclose()
        except (httpx.HTTPStatusError, httpx.RequestError):
            pass

        # 3. Server blocks all pre-flight requests (token-protected URLs, etc.)
        # Return empty metadata — filename will be parsed from the URL and the
        # actual streaming GET will carry the token and succeed.
        return {}, url


_DL_EXTS = (
    '.zip', '.rar', '.7z', '.tar', '.gz', '.bz2', '.xz', '.zst',
    '.iso', '.img', '.bin', '.nrg', '.mdf',
    '.exe', '.dmg', '.pkg', '.deb', '.rpm', '.apk', '.msi',
    '.mp4', '.mkv', '.avi', '.mov', '.wmv', '.flv', '.webm',
    '.mp3', '.flac', '.wav', '.aac', '.ogg',
    '.pdf', '.epub', '.mobi',
    '.jar', '.apk',
)


async def _headless_extract(url: str) -> Optional[str]:
    """Visit a download page with a headless Chromium browser, wait up to 20 s for
    any countdown timer, and return the first file-download URL found."""
    try:
        from playwright.async_api import async_playwright
    except ImportError:
        return None

    found: Optional[str] = None

    async with async_playwright() as pw:
        browser = await pw.chromium.launch(
            headless=True,
            args=["--no-sandbox", "--disable-dev-shm-usage"],
        )
        ctx = await browser.new_context(
            user_agent=_BROWSER_UA,
            viewport={"width": 1280, "height": 800},
        )
        page = await ctx.new_page()

        async def on_response(resp):
            nonlocal found
            if found:
                return
            ct = (resp.headers.get("content-type") or "").split(";")[0].lower()
            skip = ("text/", "application/javascript", "application/json",
                    "application/xml", "image/", "font/")
            if ct and not any(ct.startswith(s) for s in skip):
                found = resp.url
                return
            raw = resp.url.lower().split("?")[0]
            if any(raw.endswith(e) for e in _DL_EXTS):
                found = resp.url

        page.on("response", on_response)

        try:
            await page.goto(url, wait_until="domcontentloaded", timeout=30_000)
        except Exception:
            pass

        # Poll up to 20 s for countdown timers and link injection
        for _ in range(20):
            if found:
                break
            try:
                ext_list = "|".join(e.lstrip(".") for e in _DL_EXTS)
                links = await page.evaluate(
                    f"() => Array.from(document.querySelectorAll('a[href]'))"
                    f".map(a=>a.href)"
                    f".filter(h=>/\\.({ext_list})(\\?|#|$)/i.test(h))"
                )
                if links:
                    found = links[0]
                    break
            except Exception:
                pass
            await asyncio.sleep(1)

        await browser.close()

    return found


async def _do_page_import(upload_id: str, page_url: str,
                          provider_id: Optional[str] = None):
    """Background task: use headless browser to extract a download URL from a
    web page, then hand off to the normal _do_import pipeline."""
    from database import SessionLocal
    db = SessionLocal()
    prog = _import_progress[upload_id]

    try:
        prog["status"] = "analyzing"

        extracted = await _headless_extract(page_url)

        if not extracted:
            prog["status"] = "failed"
            prog["error"] = (
                "Could not find a direct download link on that page. "
                "Try visiting the page yourself, copying the actual file URL, "
                "and pasting it into the import box."
            )
            upload = db.query(Upload).filter(Upload.id == upload_id).first()
            if upload:
                upload.status = "failed"
                db.commit()
            return

        # Resolve final URL and metadata
        try:
            meta, final_url = await _resolve_url_meta(extracted)
        except Exception:
            meta, final_url = {}, extracted

        content_length = int(meta.get("content-length", 0) or 0)
        content_type   = (meta.get("content-type", "application/octet-stream")
                          .split(";")[0].strip() or "application/octet-stream")
        filename = final_url.split("?")[0].rstrip("/").split("/")[-1]
        filename = os.path.basename(filename).replace("\0", "") or "imported_file"

        file_key     = f"uploads/{uuid.uuid4()}/{filename}"
        file_storage = _get_storage(provider_id, db)
        b2_upload_id = file_storage.create_multipart_upload(file_key, content_type)

        upload = db.query(Upload).filter(Upload.id == upload_id).first()
        if upload:
            upload.filename       = filename
            upload.file_size      = content_length
            upload.content_type   = content_type
            upload.b2_file_key    = file_key
            upload.b2_upload_id   = b2_upload_id
            upload.status         = "importing"
            db.commit()

        prog["status"] = "importing"
        prog["total"]  = content_length

    except Exception as exc:
        prog["status"] = "failed"
        prog["error"]  = f"Page analysis failed: {exc}"
        upload = db.query(Upload).filter(Upload.id == upload_id).first()
        if upload:
            upload.status = "failed"
            db.commit()
        db.close()
        return

    db.close()
    # Hand off to the regular import pipeline
    await _do_import(upload_id, final_url, file_key, b2_upload_id, provider_id)


async def _do_import(upload_id: str, url: str, file_key: str, b2_upload_id: str,
                     provider_id: Optional[str] = None):
    """Stream a remote file directly into the target storage provider."""
    from database import SessionLocal
    loop = asyncio.get_event_loop()
    db = SessionLocal()
    prog = _import_progress[upload_id]

    try:
        file_storage = _get_storage(provider_id, db)
        parts_dict: dict = {}  # part_number → etag
        errors: list = []
        part_number = 0
        buf = bytearray()
        total_bytes = 0

        part_queue: asyncio.Queue = asyncio.Queue(maxsize=IMPORT_QUEUE_MAX)

        async def upload_worker():
            while True:
                item = await part_queue.get()
                try:
                    if item is None:  # sentinel — worker should exit
                        return
                    if errors or prog.get("status") == "aborted":
                        continue  # drain queue without processing
                    pn, data = item
                    etag = await loop.run_in_executor(
                        None,
                        lambda d=data, p=pn: file_storage.upload_part(file_key, b2_upload_id, p, d),
                    )
                    parts_dict[pn] = etag
                except Exception as exc:
                    errors.append(exc)
                finally:
                    part_queue.task_done()

        workers = [asyncio.create_task(upload_worker()) for _ in range(IMPORT_WORKERS)]

        try:
            async with httpx.AsyncClient(
                follow_redirects=True,
                max_redirects=_MAX_REDIRECTS,
                timeout=httpx.Timeout(30.0, read=600.0),
                headers=_download_headers(url),
            ) as client:
                async with client.stream("GET", url) as resp:
                    resp.raise_for_status()
                    async for chunk in resp.aiter_bytes(IMPORT_READ_SIZE):
                        if prog.get("status") == "aborted" or errors:
                            break
                        buf.extend(chunk)
                        total_bytes += len(chunk)
                        prog["bytes_done"] = total_bytes

                        while len(buf) >= IMPORT_CHUNK:
                            if prog.get("status") == "aborted" or errors:
                                break
                            part_number += 1
                            data = bytes(buf[:IMPORT_CHUNK])
                            del buf[:IMPORT_CHUNK]
                            await part_queue.put((part_number, data))  # backpressure

                        if prog.get("status") == "aborted" or errors:
                            break

            # Upload any remaining bytes as the final part
            if buf and not errors and prog.get("status") != "aborted":
                part_number += 1
                await part_queue.put((part_number, bytes(buf)))

        finally:
            # Drain any unconsumed items before sending sentinels
            while not part_queue.empty():
                try:
                    part_queue.get_nowait()
                    part_queue.task_done()
                except asyncio.QueueEmpty:
                    break
            for _ in range(IMPORT_WORKERS):
                part_queue.put_nowait(None)

        await asyncio.gather(*workers, return_exceptions=True)

        if errors:
            raise errors[0]

        # If cancelled, clean up any parts already uploaded to storage and exit
        if prog.get("status") == "aborted":
            await loop.run_in_executor(
                None, lambda: file_storage.abort_multipart_upload(file_key, b2_upload_id)
            )
            db.close()
            return

        if not parts_dict:
            raise ValueError("Remote file was empty")

        prog["status"] = "completing"
        parts = [{"part_number": pn, "etag": etag} for pn, etag in sorted(parts_dict.items())]
        await loop.run_in_executor(
            None,
            lambda: file_storage.complete_multipart_upload(file_key, b2_upload_id, parts),
        )

        upload = db.query(Upload).filter(Upload.id == upload_id).first()
        if upload:
            upload.status = "completed"
            upload.file_size = total_bytes
            upload.completed_at = datetime.utcnow()
            db.commit()

        prog["status"] = "completed"
        prog["bytes_done"] = total_bytes
        prog["total"] = total_bytes

    except httpx.HTTPStatusError as exc:
        code = exc.response.status_code
        if code == 403:
            msg = (
                "Access denied (403). This URL has an IP-locked token — it was "
                "generated for your browser's IP and cannot be used from our server. "
                "Solution: download the file to your computer first, then upload it here."
            )
        elif code == 401:
            msg = "Authentication required (401). The link requires a login or has expired."
        elif code == 404:
            msg = "File not found (404). The link may have expired or been removed."
        else:
            msg = f"Server returned HTTP {code}. The remote server rejected our request."

        prog["status"] = "failed"
        prog["error"] = msg

        upload = db.query(Upload).filter(Upload.id == upload_id).first()
        if upload:
            upload.status = "failed"
            db.commit()

        try:
            file_storage.abort_multipart_upload(file_key, b2_upload_id)
        except Exception:
            pass

    except Exception as exc:
        prog["status"] = "failed"
        prog["error"] = str(exc)[:300]

        upload = db.query(Upload).filter(Upload.id == upload_id).first()
        if upload:
            upload.status = "failed"
            db.commit()

        try:
            file_storage.abort_multipart_upload(file_key, b2_upload_id)
        except Exception:
            pass
    finally:
        db.close()


# ── Storage Provider API ──────────────────────────────────────────────────────


class StorageProviderIn(BaseModel):
    name: str
    endpoint_url: str
    key_id: str
    application_key: str
    bucket_name: str
    public_base_url: Optional[str] = None
    is_default: int = 0
    active: int = 1
    bandwidth_cap_gb: Optional[float] = None
    fallback_provider_id: Optional[str] = None
    fallback_base_url: Optional[str] = None


def _provider_dict(p: StorageProvider) -> dict:
    used = p.monthly_bandwidth_used or 0
    cap  = p.bandwidth_cap_gb
    return {
        "id": p.id,
        "name": p.name,
        "endpoint_url": p.endpoint_url,
        "key_id": p.key_id,
        "application_key": p.application_key,
        "bucket_name": p.bucket_name,
        "public_base_url": p.public_base_url or "",
        "is_default": p.is_default,
        "active": p.active,
        "created_at": p.created_at.isoformat() if p.created_at else None,
        "bandwidth_cap_gb": cap,
        "fallback_provider_id": p.fallback_provider_id or "",
        "fallback_base_url": p.fallback_base_url or "",
        "monthly_bandwidth_used": used,
        "bandwidth_used_gb": round(used / 1_073_741_824, 2),
        "bandwidth_pct": round(used / (cap * 1_073_741_824) * 100, 1) if cap else None,
        "cap_exceeded": bool(cap and used >= cap * 1_073_741_824),
        "bandwidth_reset_month": p.bandwidth_reset_month or "",
    }


@app.get("/api/admin/storage")
async def list_storage_providers(db: Session = Depends(get_db), _=Depends(require_admin)):
    rows = db.query(StorageProvider).order_by(StorageProvider.created_at).all()
    result = []
    for p in rows:
        d = _provider_dict(p)
        d["file_count"] = (
            db.query(func.count(Upload.id))
            .filter(Upload.storage_provider_id == p.id, Upload.status == "completed")
            .scalar() or 0
        )
        result.append(d)
    return result


@app.post("/api/admin/storage")
async def create_storage_provider(
    body: StorageProviderIn,
    db: Session = Depends(get_db),
    _=Depends(require_admin),
):
    if not body.endpoint_url.startswith(("http://", "https://")):
        raise HTTPException(400, "endpoint_url must start with http:// or https://")
    if body.is_default:
        db.query(StorageProvider).update({StorageProvider.is_default: 0})
    provider = StorageProvider(
        id=str(uuid.uuid4()),
        name=body.name,
        endpoint_url=body.endpoint_url.rstrip("/"),
        key_id=body.key_id,
        application_key=body.application_key,
        bucket_name=body.bucket_name,
        public_base_url=(body.public_base_url or "").rstrip("/") or None,
        is_default=body.is_default,
        active=body.active,
        bandwidth_cap_gb=body.bandwidth_cap_gb or None,
        fallback_provider_id=body.fallback_provider_id or None,
        fallback_base_url=(body.fallback_base_url or "").rstrip("/") or None,
        monthly_bandwidth_used=0,
        bandwidth_reset_month=datetime.utcnow().strftime("%Y-%m"),
        created_at=datetime.utcnow(),
    )
    db.add(provider)
    db.commit()
    return _provider_dict(provider)


@app.patch("/api/admin/storage/{provider_id}")
async def update_storage_provider(
    provider_id: str,
    body: StorageProviderIn,
    db: Session = Depends(get_db),
    _=Depends(require_admin),
):
    provider = db.query(StorageProvider).filter(StorageProvider.id == provider_id).first()
    if not provider:
        raise HTTPException(404, "Provider not found")
    if body.is_default:
        db.query(StorageProvider).filter(StorageProvider.id != provider_id).update(
            {StorageProvider.is_default: 0}
        )
    provider.name = body.name
    provider.endpoint_url = body.endpoint_url.rstrip("/")
    provider.key_id = body.key_id
    provider.application_key = body.application_key
    provider.bucket_name = body.bucket_name
    provider.public_base_url = (body.public_base_url or "").rstrip("/") or None
    provider.is_default = body.is_default
    provider.active = body.active
    provider.bandwidth_cap_gb = body.bandwidth_cap_gb or None
    provider.fallback_provider_id = body.fallback_provider_id or None
    provider.fallback_base_url = (body.fallback_base_url or "").rstrip("/") or None
    db.commit()
    _storage_cache.pop(provider_id, None)
    return _provider_dict(provider)


@app.delete("/api/admin/storage/{provider_id}")
async def delete_storage_provider(
    provider_id: str,
    db: Session = Depends(get_db),
    _=Depends(require_admin),
):
    provider = db.query(StorageProvider).filter(StorageProvider.id == provider_id).first()
    if not provider:
        raise HTTPException(404, "Provider not found")
    file_count = (
        db.query(func.count(Upload.id))
        .filter(Upload.storage_provider_id == provider_id)
        .scalar() or 0
    )
    if file_count:
        raise HTTPException(400, f"Cannot delete: {file_count} file(s) are stored here. Delete those files first.")
    db.delete(provider)
    db.commit()
    _storage_cache.pop(provider_id, None)
    return {"ok": True}


@app.post("/api/admin/storage/{provider_id}/set-default")
async def set_default_storage(
    provider_id: str,
    db: Session = Depends(get_db),
    _=Depends(require_admin),
):
    provider = db.query(StorageProvider).filter(StorageProvider.id == provider_id).first()
    if not provider:
        raise HTTPException(404, "Provider not found")
    db.query(StorageProvider).update({StorageProvider.is_default: 0})
    provider.is_default = 1
    db.commit()
    return {"ok": True}


@app.post("/api/admin/storage/{provider_id}/test")
async def test_storage_provider(
    provider_id: str,
    db: Session = Depends(get_db),
    _=Depends(require_admin),
):
    provider = db.query(StorageProvider).filter(StorageProvider.id == provider_id).first()
    if not provider:
        raise HTTPException(404, "Provider not found")
    # Bust cache so the test always uses fresh credentials
    _storage_cache.pop(provider_id, None)
    try:
        inst = _get_storage(provider_id, db)
        inst.test_connection()
        return {"ok": True}
    except Exception as exc:
        return {"ok": False, "error": str(exc)}


# ── Ads API ───────────────────────────────────────────────────────────────────


class AdIn(BaseModel):
    type: str           # "banner" or "button"
    label: str
    image_url: Optional[str] = None
    link_url: str
    active: int = 1
    display_order: int = 0


@app.get("/api/ads")
async def list_ads_public(db: Session = Depends(get_db)):
    """Public endpoint — returns only active ads ordered for display."""
    rows = (
        db.query(Ad)
        .filter(Ad.active == 1)
        .order_by(Ad.display_order.asc(), Ad.created_at.asc())
        .all()
    )
    return [_ad_dict(a) for a in rows]


@app.get("/api/admin/ads")
async def list_ads_admin(db: Session = Depends(get_db), _=Depends(require_admin)):
    rows = db.query(Ad).order_by(Ad.display_order.asc(), Ad.created_at.asc()).all()
    return [_ad_dict(a) for a in rows]


@app.post("/api/admin/ads")
async def create_ad(body: AdIn, db: Session = Depends(get_db), _=Depends(require_admin)):
    if body.type not in ("banner", "button"):
        raise HTTPException(400, "type must be 'banner' or 'button'")
    if not body.link_url.startswith(("http://", "https://")):
        raise HTTPException(400, "link_url must be an http/https URL")
    if body.type == "banner" and body.image_url and not body.image_url.startswith(("http://", "https://")):
        raise HTTPException(400, "image_url must be an http/https URL")
    ad = Ad(
        id=str(uuid.uuid4()),
        type=body.type,
        label=body.label[:300],
        image_url=body.image_url,
        link_url=body.link_url,
        active=body.active,
        display_order=body.display_order,
        created_at=datetime.utcnow(),
    )
    db.add(ad)
    db.commit()
    return _ad_dict(ad)


@app.patch("/api/admin/ads/{ad_id}")
async def update_ad(ad_id: str, body: AdIn, db: Session = Depends(get_db), _=Depends(require_admin)):
    ad = db.query(Ad).filter(Ad.id == ad_id).first()
    if not ad:
        raise HTTPException(404, "Ad not found")
    ad.type = body.type
    ad.label = body.label[:300]
    ad.image_url = body.image_url
    ad.link_url = body.link_url
    ad.active = body.active
    ad.display_order = body.display_order
    db.commit()
    return _ad_dict(ad)


@app.delete("/api/admin/ads/{ad_id}")
async def delete_ad(ad_id: str, db: Session = Depends(get_db), _=Depends(require_admin)):
    ad = db.query(Ad).filter(Ad.id == ad_id).first()
    if not ad:
        raise HTTPException(404, "Ad not found")
    db.delete(ad)
    db.commit()
    return {"ok": True}


def _ad_dict(a: Ad):
    return {
        "id": a.id,
        "type": a.type,
        "label": a.label,
        "image_url": a.image_url,
        "link_url": a.link_url,
        "active": a.active,
        "display_order": a.display_order,
        "created_at": a.created_at.isoformat() if a.created_at else None,
    }


# ── Site Settings ────────────────────────────────────────────────────────────


def _get_setting(db: Session, key: str) -> Optional[str]:
    row = db.query(SiteSetting).filter(SiteSetting.key == key).first()
    return row.value if row else None


def _upsert_setting(db: Session, key: str, value: Optional[str]):
    row = db.query(SiteSetting).filter(SiteSetting.key == key).first()
    if row:
        row.value = value
        row.updated_at = datetime.utcnow()
    else:
        db.add(SiteSetting(key=key, value=value, updated_at=datetime.utcnow()))


@app.get("/api/settings")
async def get_public_settings(db: Session = Depends(get_db)):
    """Public endpoint — returns settings used by landing pages."""
    return {"redirect_url": _get_setting(db, "redirect_url")}


class UpdateSettingsIn(BaseModel):
    redirect_url: Optional[str] = None


@app.post("/api/admin/settings")
async def update_settings(
    body: UpdateSettingsIn,
    db: Session = Depends(get_db),
    _=Depends(require_admin),
):
    if body.redirect_url:
        if not body.redirect_url.startswith(("http://", "https://")):
            raise HTTPException(400, "redirect_url must be an http/https URL")
    _upsert_setting(db, "redirect_url", body.redirect_url or None)
    db.commit()
    return {"ok": True}


# ── Team key management ───────────────────────────────────────────────────────


class CreateKeyIn(BaseModel):
    name: str
    role: str = "member"


class UpdateKeyIn(BaseModel):
    name: Optional[str] = None
    active: Optional[bool] = None


def _key_dict(k: ApiKey, reveal: bool = False) -> dict:
    masked = k.key[:6] + "••••••••••••••••"
    return {
        "id": k.id,
        "name": k.name,
        "key": k.key if reveal else masked,
        "role": k.role,
        "active": bool(k.active),
        "created_at": k.created_at.isoformat(),
    }


@app.get("/api/admin/keys")
async def list_team_keys(db: Session = Depends(get_db), _=Depends(require_admin)):
    keys = db.query(ApiKey).filter(ApiKey.is_master == 0).order_by(ApiKey.created_at.desc()).all()
    return [_key_dict(k) for k in keys]


@app.post("/api/admin/keys")
async def create_team_key(body: CreateKeyIn, db: Session = Depends(get_db), _=Depends(require_admin)):
    if body.role not in ("member", "admin"):
        raise HTTPException(400, "Role must be 'member' or 'admin'")
    new_key = ApiKey(
        id=str(uuid.uuid4()),
        name=body.name.strip(),
        key=secrets.token_urlsafe(32),
        role=body.role,
        active=1,
        created_at=datetime.utcnow(),
    )
    db.add(new_key)
    db.commit()
    return _key_dict(new_key, reveal=True)


@app.patch("/api/admin/keys/{key_id}")
async def update_team_key(key_id: str, body: UpdateKeyIn, db: Session = Depends(get_db), _=Depends(require_admin)):
    k = db.query(ApiKey).filter(ApiKey.id == key_id).first()
    if not k:
        raise HTTPException(404, "Key not found")
    if body.name is not None:
        k.name = body.name.strip()
    if body.active is not None:
        k.active = 1 if body.active else 0
    db.commit()
    return _key_dict(k)


@app.post("/api/admin/keys/{key_id}/regenerate")
async def regenerate_team_key(key_id: str, db: Session = Depends(get_db), _=Depends(require_admin)):
    k = db.query(ApiKey).filter(ApiKey.id == key_id).first()
    if not k:
        raise HTTPException(404, "Key not found")
    k.key = secrets.token_urlsafe(32)
    db.commit()
    return _key_dict(k, reveal=True)


@app.delete("/api/admin/keys/{key_id}")
async def delete_team_key(key_id: str, db: Session = Depends(get_db), _=Depends(require_admin)):
    k = db.query(ApiKey).filter(ApiKey.id == key_id).first()
    if not k:
        raise HTTPException(404, "Key not found")
    if k.is_master:
        raise HTTPException(400, "Cannot delete the master admin key. Use Change Admin Key instead.")
    db.delete(k)
    db.commit()
    return {"ok": True}


@app.post("/api/admin/change-master-key")
async def change_master_key(db: Session = Depends(get_db), _=Depends(require_admin)):
    """Generate a new master admin key. The old key is immediately invalidated."""
    new_key_value = secrets.token_urlsafe(32)
    master = db.query(ApiKey).filter(ApiKey.is_master == 1).first()
    if master:
        master.key = new_key_value
    else:
        db.add(ApiKey(
            id=str(uuid.uuid4()),
            name="Admin",
            key=new_key_value,
            role="admin",
            is_master=1,
            active=1,
            created_at=datetime.utcnow(),
        ))
    db.commit()
    return {"new_key": new_key_value}


# ── Serve frontend ────────────────────────────────────────────────────────────


@app.get("/f/{share_id}", include_in_schema=False)
async def share_page(share_id: str):
    return FileResponse(os.path.join(FRONTEND_DIR, "landing.html"))


@app.get("/", include_in_schema=False)
async def serve_index():
    return FileResponse(os.path.join(FRONTEND_DIR, "index.html"))


@app.get("/app.js", include_in_schema=False)
async def serve_js():
    return FileResponse(os.path.join(FRONTEND_DIR, "app.js"))


@app.get("/style.css", include_in_schema=False)
async def serve_css():
    return FileResponse(os.path.join(FRONTEND_DIR, "style.css"))


@app.get("/landing.js", include_in_schema=False)
async def serve_landing_js():
    return FileResponse(os.path.join(FRONTEND_DIR, "landing.js"))


@app.get("/favicon.ico", include_in_schema=False)
async def serve_favicon():
    return FileResponse(os.path.join(FRONTEND_DIR, "favicon.ico"), media_type="image/x-icon")


@app.get("/logo.webp", include_in_schema=False)
async def serve_logo():
    return FileResponse(os.path.join(FRONTEND_DIR, "logo.webp"), media_type="image/webp")
