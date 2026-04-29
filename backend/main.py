import asyncio
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
from models import Ad, Part, StorageProvider, Upload
from storage import B2Storage, S3Storage

# ── Config ────────────────────────────────────────────────────────────────────

MAX_BYTES = int(os.getenv("MAX_FILE_SIZE_GB", "10")) * 1_073_741_824
API_KEY = os.getenv("API_KEY", "")
FRONTEND_DIR = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "frontend"))
IMPORT_CHUNK = 10 * 1024 * 1024  # 10 MB per B2 part

_import_progress: dict = {}    # upload_id -> progress dict
_storage_cache:   dict = {}    # provider_id -> S3Storage instance


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


@app.on_event("startup")
async def _startup():
    init_db()


def _share_id() -> str:
    return "".join(secrets.choice(string.ascii_letters + string.digits) for _ in range(8))


# ── Auth ──────────────────────────────────────────────────────────────────────


def require_auth(x_api_key: Optional[str] = Header(None)):
    if API_KEY and x_api_key != API_KEY:
        raise HTTPException(status_code=401, detail="Unauthorized")


# ── Schemas ───────────────────────────────────────────────────────────────────


class InitUploadIn(BaseModel):
    filename: str
    file_size: int
    file_hash: str
    content_type: str = "application/octet-stream"


# ── Upload API ────────────────────────────────────────────────────────────────


@app.post("/api/upload/init")
async def init_upload(
    body: InitUploadIn,
    db: Session = Depends(get_db),
    _=Depends(require_auth),
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
        created_at=datetime.utcnow(),
    )
    db.add(upload)
    db.commit()
    return {"upload_id": upload.id, "resuming": False, "completed_parts": []}


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
    if upload.status == "pending":
        _get_storage(upload.storage_provider_id, db).abort_multipart_upload(
            upload.b2_file_key, upload.b2_upload_id
        )
    upload.status = "aborted"
    db.commit()
    return {"ok": True}


# ── File manager API ──────────────────────────────────────────────────────────


@app.get("/api/files")
async def list_files(db: Session = Depends(get_db), _=Depends(require_auth)):
    rows = (
        db.query(Upload)
        .filter(Upload.status == "completed")
        .order_by(Upload.completed_at.desc())
        .limit(200)
        .all()
    )
    # Build provider name lookup in one query
    pids = {f.storage_provider_id for f in rows if f.storage_provider_id}
    providers = {}
    if pids:
        for p in db.query(StorageProvider).filter(StorageProvider.id.in_(pids)).all():
            providers[p.id] = p.name

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
        }
        for f in rows
    ]


@app.delete("/api/files/{file_id}")
async def delete_file(file_id: str, db: Session = Depends(get_db), _=Depends(require_auth)):
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
async def verify_auth(_=Depends(require_auth)):
    return {"ok": True}


# ── Dashboard stats ───────────────────────────────────────────────────────────


@app.get("/api/stats")
async def get_stats(db: Session = Depends(get_db), _=Depends(require_auth)):
    total_files = (
        db.query(func.count(Upload.id)).filter(Upload.status == "completed").scalar() or 0
    )
    total_size = (
        db.query(func.coalesce(func.sum(Upload.file_size), 0))
        .filter(Upload.status == "completed")
        .scalar()
    )
    total_downloads = (
        db.query(func.coalesce(func.sum(Upload.downloads), 0))
        .filter(Upload.status == "completed")
        .scalar()
    )

    uploads_per_day = []
    today = datetime.utcnow().date()
    for i in range(6, -1, -1):
        day = today - timedelta(days=i)
        day_start = datetime(day.year, day.month, day.day, 0, 0, 0)
        day_end = datetime(day.year, day.month, day.day, 23, 59, 59)
        count = (
            db.query(func.count(Upload.id))
            .filter(
                Upload.status == "completed",
                Upload.completed_at >= day_start,
                Upload.completed_at <= day_end,
            )
            .scalar()
            or 0
        )
        uploads_per_day.append({"date": day.strftime("%b %d"), "count": count})

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


@app.get("/api/f/{share_id}/download")
async def download_file(share_id: str, db: Session = Depends(get_db)):
    upload = (
        db.query(Upload)
        .filter(Upload.share_id == share_id, Upload.status == "completed")
        .first()
    )
    if not upload:
        raise HTTPException(404, "File not found")

    upload.downloads = (upload.downloads or 0) + 1
    db.commit()

    return RedirectResponse(url=_get_storage(upload.storage_provider_id, db).get_download_url(upload.b2_file_key))


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

    content_length = int(meta.get("content-length", 0) or 0)
    content_type = (meta.get("content-type", "application/octet-stream")
                    .split(";")[0].strip() or "application/octet-stream")

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
_META_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/124.0.0.0 Safari/537.36"
    ),
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "en-US,en;q=0.5",
    "Accept-Encoding": "identity",   # keep off so Content-Length stays accurate
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


async def _do_import(upload_id: str, url: str, file_key: str, b2_upload_id: str,
                     provider_id: Optional[str] = None):
    """Stream a remote file directly into the target storage provider."""
    from database import SessionLocal
    loop = asyncio.get_event_loop()
    db = SessionLocal()
    prog = _import_progress[upload_id]

    try:
        file_storage = _get_storage(provider_id, db)
        parts: list = []
        part_number = 0
        buf = bytearray()
        total_bytes = 0

        async with httpx.AsyncClient(
            follow_redirects=True,
            max_redirects=_MAX_REDIRECTS,
            timeout=httpx.Timeout(30.0, read=600.0),
            headers=_META_HEADERS,
        ) as client:
            async with client.stream("GET", url) as resp:
                resp.raise_for_status()
                async for chunk in resp.aiter_bytes(65_536):
                    buf.extend(chunk)
                    total_bytes += len(chunk)
                    prog["bytes_done"] = total_bytes

                    while len(buf) >= IMPORT_CHUNK:
                        part_number += 1
                        data = bytes(buf[:IMPORT_CHUNK])
                        del buf[:IMPORT_CHUNK]
                        pn = part_number
                        etag = await loop.run_in_executor(
                            None,
                            lambda d=data, p=pn: file_storage.upload_part(file_key, b2_upload_id, p, d),
                        )
                        parts.append({"part_number": pn, "etag": etag})

        # Upload any remaining bytes as the final part
        if buf:
            part_number += 1
            pn = part_number
            data = bytes(buf)
            etag = await loop.run_in_executor(
                None,
                lambda d=data, p=pn: file_storage.upload_part(file_key, b2_upload_id, p, d),
            )
            parts.append({"part_number": pn, "etag": etag})

        if not parts:
            raise ValueError("Remote file was empty")

        prog["status"] = "completing"
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


def _provider_dict(p: StorageProvider) -> dict:
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
    }


@app.get("/api/admin/storage")
async def list_storage_providers(db: Session = Depends(get_db), _=Depends(require_auth)):
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
    _=Depends(require_auth),
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
    _=Depends(require_auth),
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
    db.commit()
    _storage_cache.pop(provider_id, None)
    return _provider_dict(provider)


@app.delete("/api/admin/storage/{provider_id}")
async def delete_storage_provider(
    provider_id: str,
    db: Session = Depends(get_db),
    _=Depends(require_auth),
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
    _=Depends(require_auth),
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
    _=Depends(require_auth),
):
    provider = db.query(StorageProvider).filter(StorageProvider.id == provider_id).first()
    if not provider:
        raise HTTPException(404, "Provider not found")
    try:
        inst = S3Storage(
            endpoint_url=provider.endpoint_url,
            key_id=provider.key_id,
            application_key=provider.application_key,
            bucket_name=provider.bucket_name,
        )
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
async def list_ads_admin(db: Session = Depends(get_db), _=Depends(require_auth)):
    rows = db.query(Ad).order_by(Ad.display_order.asc(), Ad.created_at.asc()).all()
    return [_ad_dict(a) for a in rows]


@app.post("/api/admin/ads")
async def create_ad(body: AdIn, db: Session = Depends(get_db), _=Depends(require_auth)):
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
async def update_ad(ad_id: str, body: AdIn, db: Session = Depends(get_db), _=Depends(require_auth)):
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
async def delete_ad(ad_id: str, db: Session = Depends(get_db), _=Depends(require_auth)):
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
