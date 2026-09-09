import asyncio
import base64
import concurrent.futures
import hashlib
import hmac
import html as _html
import ipaddress
import json
import os
import re
import secrets
import socket
import string
import struct
import uuid
from datetime import datetime, timedelta
from typing import Optional

import httpx
from dotenv import load_dotenv

load_dotenv()

from fastapi import BackgroundTasks, Depends, FastAPI, File, Header, HTTPException, Query, Request, UploadFile
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, RedirectResponse, Response
from pydantic import BaseModel
from sqlalchemy import and_, func, or_
from sqlalchemy.orm import Session

from database import get_db, init_db
from models import AccessRequest, Ad, ApiKey, DownloadEvent, DownloadToken, FileReport, Part, Referrer, SiteSetting, StorageProvider, SupportMessage, SupportReply, Upload
from storage import B2Storage, BunnyStorage, S3Storage

# ── Config ────────────────────────────────────────────────────────────────────

MAX_BYTES    = int(os.getenv("MAX_FILE_SIZE_GB", "10")) * 1_073_741_824
API_KEY      = os.getenv("API_KEY", "")
FRONTEND_DIR = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "frontend"))
IMPORT_CHUNK     = int(os.getenv("IMPORT_CHUNK_MB",  "32")) * 1024 * 1024
IMPORT_WORKERS   = int(os.getenv("IMPORT_WORKERS",  "4"))
IMPORT_QUEUE_MAX = int(os.getenv("IMPORT_QUEUE_MAX", "8"))
IMPORT_READ_SIZE = int(os.getenv("IMPORT_READ_MB",   "2")) * 1024 * 1024

# Dedicated pool for blocking R2 upload_part calls — sized well above IMPORT_WORKERS
# so workers never queue waiting for a thread (default pool = cpu_count+4 = only 6 on KVM 2).
_upload_executor = concurrent.futures.ThreadPoolExecutor(
    max_workers=max(32, IMPORT_WORKERS * 2),
    thread_name_prefix="r2-upload",
)

WORKER_URL    = os.getenv("WORKER_URL", "").rstrip("/")   # e.g. https://datadock-dl.abc.workers.dev
SUPPORT_MEDIA_DIR = os.path.abspath(
    os.getenv("SUPPORT_MEDIA_DIR", os.path.join(os.path.dirname(__file__), "data", "support-media"))
)
os.makedirs(SUPPORT_MEDIA_DIR, exist_ok=True)
_ALLOWED_IMG_TYPES = {"image/jpeg", "image/png", "image/gif", "image/webp", "image/bmp"}
_IMG_EXT_MAP = {"image/jpeg": ".jpg", "image/png": ".png", "image/gif": ".gif",
                "image/webp": ".webp", "image/bmp": ".bmp"}
WORKER_SECRET = os.getenv("WORKER_SECRET", "")
WORKER_TOKEN_TTL = 300  # seconds — token expires after 5 minutes

_import_progress:   dict = {}    # upload_id -> progress dict
_storage_cache:     dict = {}    # provider_id -> S3Storage instance
_geo_cache:         dict = {}    # ip -> (country, country_code)
_geoip_reader             = None  # geoip2 Reader singleton
_recent_downloads:  dict = {}    # (ip, upload_id) -> last download datetime
_landing_tpl:       str  = ""    # landing.html cached template

# Extensions whose /preview URL is a usable og:image
_OG_IMAGE_EXTS = {".jpg", ".jpeg", ".png", ".gif", ".webp", ".avif", ".bmp", ".svg"}


def _fmt_bytes(n: int) -> str:
    if n >= 1_000_000_000: return f"{n/1_000_000_000:.2f} GB"
    if n >= 1_000_000:     return f"{n/1_000_000:.1f} MB"
    if n >= 1_000:         return f"{n/1_000:.0f} KB"
    return f"{n} B"


def _track_bandwidth(provider_id: str, size: int, db) -> None:
    """Atomic SQL increment — avoids lost updates with multiple uvicorn workers."""
    from sqlalchemy import text as _sql_text
    db.execute(
        _sql_text(
            "UPDATE storage_providers SET monthly_bandwidth_used = "
            "COALESCE(monthly_bandwidth_used, 0) + :size WHERE id = :id"
        ),
        {"size": size, "id": provider_id},
    )


def _landing_template() -> str:
    """Return the landing.html template, cached in memory after first read."""
    global _landing_tpl
    if not _landing_tpl:
        with open(os.path.join(FRONTEND_DIR, "landing.html"), "r", encoding="utf-8") as f:
            _landing_tpl = f.read()
    return _landing_tpl


def _build_og_html(share_id: str, upload, base_url: str) -> str:
    """Render landing.html with full SEO meta tags injected into <head>."""
    tpl = _landing_template()

    ext      = os.path.splitext(upload.filename or "")[1].lower()
    ext_label = ext.lstrip(".").upper() or "FILE"
    filename  = upload.filename or "File"
    size      = _fmt_bytes(upload.file_size or 0)
    page_url  = f"{base_url}/f/{share_id}"

    # Escape for HTML attribute context
    esc_name  = _html.escape(filename)
    esc_title = _html.escape(f"Download {filename} | DataDock")
    esc_desc  = _html.escape(
        f"Download {filename} — {size} {ext_label} file. "
        f"Free, fast, and secure. No account required. Available on DataDock."
    )

    is_image = ext in _OG_IMAGE_EXTS
    og_image = (f"{base_url}/api/f/{share_id}/preview" if is_image
                else f"{base_url}/logo.webp")
    og_img_alt = _html.escape(f"Preview of {filename}" if is_image else "DataDock")
    tw_card    = "summary_large_image" if is_image else "summary"

    seo_block = (
        f'<title>{esc_title}</title>\n'
        f'    <link rel="canonical" href="{page_url}" />\n'
        f'    <meta name="description"              content="{esc_desc}" />\n'
        f'    <meta name="robots"                   content="index, follow" />\n'
        # Open Graph
        f'    <meta property="og:type"              content="website" />\n'
        f'    <meta property="og:url"               content="{page_url}" />\n'
        f'    <meta property="og:site_name"         content="DataDock" />\n'
        f'    <meta property="og:title"             content="{esc_name}" />\n'
        f'    <meta property="og:description"       content="{esc_desc}" />\n'
        f'    <meta property="og:image"             content="{og_image}" />\n'
        f'    <meta property="og:image:alt"         content="{og_img_alt}" />\n'
        # Twitter Card
        f'    <meta name="twitter:card"             content="{tw_card}" />\n'
        f'    <meta name="twitter:title"            content="{esc_name}" />\n'
        f'    <meta name="twitter:description"      content="{esc_desc}" />\n'
        f'    <meta name="twitter:image"            content="{og_image}" />\n'
        f'    <meta name="twitter:image:alt"        content="{og_img_alt}" />'
    )
    return tpl.replace("<title>DataDock – Download</title>", seo_block, 1)

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
    if len(_geo_cache) > 20_000:   # cap memory growth
        _geo_cache.clear()
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


def _parse_referrer_domain(referer: str, host: str) -> Optional[str]:
    """Extract bare domain from a Referer header.
    Returns None for direct traffic, same-site requests, or unparseable values."""
    if not referer:
        return None
    try:
        from urllib.parse import urlparse
        netloc = urlparse(referer).netloc.lower()
        if not netloc:
            return None
        domain = netloc.split(":")[0]  # strip port
        if domain.startswith("www."):
            domain = domain[4:]
        # drop same-site hits (e.g. user clicking within datadock itself)
        own = (host or "").split(":")[0].lower()
        if own and domain == own:
            return None
        return domain or None
    except Exception:
        return None


async def _log_referrer(share_id: str, domain: str) -> None:
    """Upsert referrer hit — atomic increment on existing row, insert otherwise.
    Rows are bucketed per UTC day so Today/Yesterday filters count accurately;
    pre-existing rows (day IS NULL) hold lifetime totals and stay frozen."""
    from database import SessionLocal
    from sqlalchemy import text as _t
    db = SessionLocal()
    now = datetime.utcnow()
    day_str = now.strftime("%Y-%m-%d")
    try:
        result = db.execute(
            _t("UPDATE referrers SET visit_count = visit_count + 1, last_seen = :now "
               "WHERE share_id = :sid AND domain = :domain AND day = :day"),
            {"now": now, "sid": share_id, "domain": domain, "day": day_str},
        )
        if result.rowcount == 0:
            db.add(Referrer(
                id=str(uuid.uuid4()),
                share_id=share_id,
                domain=domain,
                visit_count=1,
                last_seen=now,
                day=day_str,
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
            db.flush()  # write the reset before the atomic increment below

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

        _track_bandwidth(p.id, upload.file_size or 0, db)

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

# Auth uses the X-API-Key header (no cookies), so credentialed CORS is unnecessary
# and wildcard-origins + credentials together is an unsafe combination.
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=False,
    allow_methods=["*"],
    allow_headers=["*"],
)


# ── Abuse protection: client IP, rate limiting, auth lockout ─────────────────

def _client_ip(request: Request) -> str:
    """Trustworthy client IP behind nginx.

    X-Real-IP is set by nginx to $remote_addr and overrides anything the client
    sent, so it can't be spoofed. The first X-Forwarded-For entry CAN be forged
    by the client (nginx appends to it), so it is only a last resort.
    """
    ip = request.headers.get("X-Real-IP", "").strip()
    if not ip:
        xff = request.headers.get("X-Forwarded-For", "")
        if xff:
            ip = xff.split(",")[-1].strip()  # last hop = added by our proxy
    if not ip and request.client:
        ip = request.client.host
    return ip or "unknown"


import time as _time
from collections import deque as _deque

_rl_buckets: dict = {}  # (rule, ip) -> deque[monotonic timestamps]

# Prefix-matched against "METHOD /path". First match wins; everything else
# under /api/ falls into the global bucket.
_RL_RULES = [
    ("POST /api/access-requests", 5,    3600),  # access-request form
    ("POST /api/reports",         5,    3600),  # file reports
    ("POST /api/support",         30,   3600),  # support messages
    ("POST /api/f/",              60,   60),    # download token mint
    ("GET /api/public/",          240,  60),    # browse search / stats
    ("POST /api/upload/",         1800, 60),    # authed chunked uploads — many parts per file
]
_RL_GLOBAL = (600, 60)


def _rl_hit(key, limit: int, window: int) -> bool:
    """Record a request against a bucket; True means over the limit."""
    now = _time.monotonic()
    dq = _rl_buckets.get(key)
    if dq is None:
        if len(_rl_buckets) > 50_000:
            # Evict oldest 20% of entries rather than wiping all state
            # (a full clear would reset every IP's rate-limit simultaneously,
            # which an attacker could exploit to get burst windows on demand).
            cutoff = sorted(_rl_buckets)[: len(_rl_buckets) // 5]
            for k in cutoff:
                _rl_buckets.pop(k, None)
        dq = _rl_buckets[key] = _deque()
    while dq and now - dq[0] > window:
        dq.popleft()
    if len(dq) >= limit:
        return True
    dq.append(now)
    return False


@app.middleware("http")
async def _abuse_protection(request: Request, call_next):
    path = request.url.path
    if path.startswith("/api/"):
        ip  = _client_ip(request)
        sig = f"{request.method} {path}"
        for prefix, limit, window in _RL_RULES:
            if sig.startswith(prefix):
                if _rl_hit((prefix, ip), limit, window):
                    return Response('{"detail":"Too many requests — slow down"}',
                                    status_code=429, media_type="application/json",
                                    headers={"Retry-After": str(window)})
                break
        else:
            limit, window = _RL_GLOBAL
            if _rl_hit(("global", ip), limit, window):
                return Response('{"detail":"Too many requests — slow down"}',
                                status_code=429, media_type="application/json",
                                headers={"Retry-After": "60"})

    response = await call_next(request)
    response.headers.setdefault("X-Content-Type-Options", "nosniff")
    response.headers.setdefault("X-Frame-Options", "SAMEORIGIN")
    response.headers.setdefault("Referrer-Policy", "strict-origin-when-cross-origin")
    return response


# Brute-force lockout for API key guessing
_auth_fails: dict = {}  # ip -> deque[monotonic timestamps]
_AUTH_FAIL_LIMIT  = 10
_AUTH_FAIL_WINDOW = 900  # 15 min


def _auth_locked(ip: str) -> bool:
    dq = _auth_fails.get(ip)
    if not dq:
        return False
    now = _time.monotonic()
    while dq and now - dq[0] > _AUTH_FAIL_WINDOW:
        dq.popleft()
    return len(dq) >= _AUTH_FAIL_LIMIT


def _auth_record_fail(ip: str):
    if len(_auth_fails) > 50_000:
        _auth_fails.clear()
    _auth_fails.setdefault(ip, _deque()).append(_time.monotonic())


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
    if not API_KEY:
        import warnings
        warnings.warn(
            "API_KEY environment variable is not set — ALL requests are accepted as admin. "
            "Set API_KEY in your .env file before exposing this service.",
            stacklevel=1,
        )
    if WORKER_URL and len(WORKER_SECRET) < 32:
        import warnings
        warnings.warn(
            "WORKER_SECRET is unset or too short — download tokens can be forged. "
            "Set WORKER_SECRET to a 32+ character random string.",
            stacklevel=1,
        )
    init_db()
    _seed_master_key()


def _share_id() -> str:
    return "".join(secrets.choice(string.ascii_letters + string.digits) for _ in range(8))


# ── Auth ──────────────────────────────────────────────────────────────────────


def require_auth(request: Request, x_api_key: Optional[str] = Header(None), db: Session = Depends(get_db)):
    """Return {'role': ..., 'name': ...} for any valid key, or raise 401."""
    if not x_api_key:
        raise HTTPException(401, "API key required")
    ip = _client_ip(request)
    if _auth_locked(ip):
        raise HTTPException(429, "Too many failed attempts — try again in 15 minutes")
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
    _auth_record_fail(ip)
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

    def __init__(self, **data):
        super().__init__(**data)
        # Upper bound is checked in the endpoint against the admin-configured limit
        if self.actual_size is not None and self.actual_size <= 0:
            raise ValueError("actual_size must be positive")


# ── Upload API ────────────────────────────────────────────────────────────────


@app.post("/api/upload/init")
async def init_upload(
    body: InitUploadIn,
    db: Session = Depends(get_db),
    auth: dict = Depends(require_auth),
    auth_key: Optional[ApiKey] = Depends(get_current_key),
):
    _assert_uploads_enabled(auth, db)
    if body.file_size > _max_bytes_setting(db):
        raise HTTPException(400, f"File exceeds {_max_gb_value(db):g} GB limit")
    if body.file_size <= 0:
        raise HTTPException(400, "Invalid file size")

    existing = (
        db.query(Upload)
        .filter(
            Upload.file_hash == body.file_hash,
            Upload.status == "pending",
            Upload.uploaded_by_key_id == (auth_key.id if auth_key else None),
        )
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
    auth: dict = Depends(require_auth),
    auth_key: Optional[ApiKey] = Depends(get_current_key),
):
    """Create an upload slot for browser-side relay (no file_size required)."""
    _assert_uploads_enabled(auth, db)
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


_MAX_CHUNK_BYTES = 5 * 1024 * 1024 * 1024  # 5 GB — S3 part size hard limit


@app.post("/api/upload/{upload_id}/chunk/{part_number}")
async def upload_chunk(
    upload_id: str,
    part_number: int,
    request: Request,
    db: Session = Depends(get_db),
    auth: dict = Depends(require_auth),
    auth_key: Optional[ApiKey] = Depends(get_current_key),
):
    if not (1 <= part_number <= 10_000):
        raise HTTPException(400, "part_number must be 1–10000")

    upload = db.query(Upload).filter(Upload.id == upload_id).first()
    if not upload:
        raise HTTPException(404, "Upload not found")
    if upload.status != "pending":
        raise HTTPException(400, f"Upload is {upload.status}")
    # Only the key that created the upload (or an admin) may write to it
    if upload.uploaded_by_key_id and auth_key and upload.uploaded_by_key_id != auth_key.id:
        if auth.get("role") != "admin":
            raise HTTPException(403, "Not your upload")

    cl = request.headers.get("content-length")
    if cl and int(cl) > _MAX_CHUNK_BYTES:
        raise HTTPException(413, "Chunk exceeds 5 GB limit")

    data = await request.body()
    if not data:
        raise HTTPException(400, "Empty chunk")
    if len(data) > _MAX_CHUNK_BYTES:
        raise HTTPException(413, "Chunk exceeds 5 GB limit")

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
    auth: dict = Depends(require_auth),
    auth_key: Optional[ApiKey] = Depends(get_current_key),
):
    upload = db.query(Upload).filter(Upload.id == upload_id).first()
    if not upload:
        raise HTTPException(404, "Upload not found")
    if upload.status != "pending":
        raise HTTPException(400, f"Upload is already {upload.status}")
    if upload.uploaded_by_key_id and auth_key and upload.uploaded_by_key_id != auth_key.id:
        if auth.get("role") != "admin":
            raise HTTPException(403, "Not your upload")
    if body and body.actual_size and body.actual_size > _max_bytes_setting(db):
        raise HTTPException(400, f"File exceeds {_max_gb_value(db):g} GB limit")

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
    auth: dict = Depends(require_auth),
    auth_key: Optional[ApiKey] = Depends(get_current_key),
):
    upload = db.query(Upload).filter(Upload.id == upload_id).first()
    if not upload:
        raise HTTPException(404, "Upload not found")
    if upload.uploaded_by_key_id and auth_key and upload.uploaded_by_key_id != auth_key.id:
        if auth.get("role") != "admin":
            raise HTTPException(403, "Not your upload")
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
            "hidden": bool(f.hidden or 0),
            "hidden_redirect_url": f.hidden_redirect_url or "",
        }
        for f in rows
    ]


def _purge_upload(upload: Upload, db: Session) -> bool:
    """Delete a file from storage and remove its DB rows. Returns True on success.
    Storage errors are swallowed so a stuck object never blocks the DB cleanup."""
    try:
        _get_storage(upload.storage_provider_id, db).delete_object(upload.b2_file_key)
    except Exception:
        pass
    db.query(Part).filter(Part.upload_id == upload.id).delete()
    db.delete(upload)
    return True


@app.delete("/api/files/{file_id}")
async def delete_file(file_id: str, db: Session = Depends(get_db), _=Depends(require_admin)):
    upload = (
        db.query(Upload)
        .filter(Upload.id == file_id, Upload.status == "completed")
        .first()
    )
    if not upload:
        raise HTTPException(404, "File not found")
    _purge_upload(upload, db)
    db.commit()
    return {"ok": True}


# ── Storage cleanup ───────────────────────────────────────────────────────────

def _cleanup_query(db: Session, days: int, max_downloads: int):
    """Files eligible for cleanup: completed, at or below the download threshold,
    older than `days`, and NOT hidden (hidden files are deliberately kept).
    Redirect targets are also spared so existing share links never break."""
    cutoff = datetime.utcnow() - timedelta(days=days)
    # share_ids that other uploads redirect to — never delete these
    redirect_targets = db.query(Upload.redirects_to).filter(Upload.redirects_to.isnot(None))
    return (
        db.query(Upload)
        .filter(
            Upload.status == "completed",
            func.coalesce(Upload.downloads, 0) <= max_downloads,
            or_(Upload.completed_at.is_(None), Upload.completed_at <= cutoff),
            or_(Upload.hidden.is_(None), Upload.hidden == 0),
            Upload.share_id.notin_(redirect_targets),
        )
    )


@app.get("/api/admin/cleanup/candidates")
async def cleanup_candidates(
    days: int = Query(90, ge=0, le=3650),
    max_downloads: int = Query(0, ge=0, le=1_000_000_000),
    limit: int = Query(500, ge=1, le=2000),
    db: Session = Depends(get_db),
    _=Depends(require_admin),
):
    # Whole-library totals (for the "you could reclaim X of Y" framing)
    lib = db.query(
        func.count(Upload.id), func.coalesce(func.sum(Upload.file_size), 0)
    ).filter(Upload.status == "completed").one()

    q = _cleanup_query(db, days, max_downloads)
    totals = q.with_entities(
        func.count(Upload.id), func.coalesce(func.sum(Upload.file_size), 0)
    ).one()

    rows = q.order_by(Upload.file_size.desc()).limit(limit).all()

    key_ids = {r.uploaded_by_key_id for r in rows if r.uploaded_by_key_id}
    names = {}
    if key_ids:
        for k in db.query(ApiKey).filter(ApiKey.id.in_(key_ids)).all():
            names[k.id] = k.name

    now = datetime.utcnow()
    files = [{
        "id": r.id,
        "filename": r.filename,
        "file_size": r.file_size,
        "downloads": r.downloads or 0,
        "completed_at": r.completed_at.isoformat() if r.completed_at else None,
        "age_days": (now - r.completed_at).days if r.completed_at else None,
        "uploaded_by": names.get(r.uploaded_by_key_id) if r.uploaded_by_key_id else None,
    } for r in rows]

    return {
        "library_files": int(lib[0]),
        "library_bytes": int(lib[1]),
        "candidate_files": int(totals[0]),
        "candidate_bytes": int(totals[1]),
        "shown": len(files),
        "files": files,
    }


class CleanupDeleteIn(BaseModel):
    file_ids: list[str]
    redirect_url: Optional[str] = None   # if set, deleted files' share links point here


@app.post("/api/admin/cleanup/delete")
async def cleanup_delete(
    body: CleanupDeleteIn,
    db: Session = Depends(get_db),
    _=Depends(require_admin),
):
    ids = [i for i in (body.file_ids or []) if i]
    if not ids:
        raise HTTPException(400, "No files selected")
    if len(ids) > 2000:
        raise HTTPException(400, "Too many files in one request (max 2000)")

    redirect_url = (body.redirect_url or "").strip() or None
    if redirect_url and not redirect_url.startswith(("http://", "https://")):
        raise HTTPException(400, "redirect_url must be an http/https URL")

    uploads = db.query(Upload).filter(
        Upload.id.in_(ids), Upload.status == "completed"
    ).all()

    deleted = 0
    freed = 0
    for up in uploads:
        # Never delete a hidden file through cleanup
        if up.hidden:
            continue
        freed += up.file_size or 0
        # Storage object is removed either way — that's what frees the space.
        try:
            _get_storage(up.storage_provider_id, db).delete_object(up.b2_file_key)
        except Exception:
            pass
        db.query(Part).filter(Part.upload_id == up.id).delete()
        if redirect_url:
            # Keep the row so the share link stays alive and redirects out
            up.status = "redirected"
            up.redirect_url = redirect_url
        else:
            # Keep the link alive showing a "max downloads reached" notice
            up.status = "quota_reached"
        deleted += 1
    db.commit()
    return {"ok": True, "deleted": deleted, "freed_bytes": freed, "redirected": bool(redirect_url)}


@app.post("/api/admin/storage/reclaim")
async def reclaim_storage(
    limit: int = Query(500, ge=1, le=2000),
    db: Session = Depends(get_db),
    _=Depends(require_admin),
):
    """Re-issue storage deletes for files already removed logically (redirected or
    quota_reached) whose objects may still exist — a safety net for deletes that
    failed silently earlier. On success the key is cleared so it isn't reprocessed;
    failures are reported (not swallowed) so a bad/read-only token is visible.
    Call repeatedly until `remaining` is 0."""
    def _pending_q():
        return db.query(Upload).filter(
            Upload.status.in_(["redirected", "quota_reached"]),
            Upload.b2_file_key.isnot(None),
            Upload.b2_file_key != "",
        )

    rows = _pending_q().limit(limit).all()
    ok = 0
    failed = 0
    errors: list = []
    for u in rows:
        try:
            _get_storage(u.storage_provider_id, db).delete_object(u.b2_file_key)
            u.b2_file_key = ""   # mark as truly purged
            ok += 1
        except Exception as exc:
            failed += 1
            if len(errors) < 3:
                errors.append(str(exc)[:200])
    db.commit()
    remaining = _pending_q().count()
    return {"ok": True, "purged": ok, "failed": failed, "remaining": remaining, "errors": errors}


class AdminRenameIn(BaseModel):
    filename: str


class AdminHideIn(BaseModel):
    hidden: bool
    redirect_url: Optional[str] = None   # where the hidden share link should redirect (optional)


@app.post("/api/admin/files/{file_id}/hide")
async def admin_hide_file(
    file_id: str,
    body: AdminHideIn,
    db: Session = Depends(get_db),
    _=Depends(require_admin),
):
    """Hide/unhide a file. Hidden files stay in storage and in the admin panel.
    A hidden file's share link either returns 404 or, if a redirect URL is set,
    permanently redirects there — so links shared elsewhere don't break after a
    DMCA takedown. Downloads/previews are always blocked while hidden."""
    upload = db.query(Upload).filter(Upload.id == file_id).first()
    if not upload:
        raise HTTPException(404, "File not found")
    redirect_url = (body.redirect_url or "").strip() or None
    if redirect_url and not redirect_url.startswith(("http://", "https://")):
        raise HTTPException(400, "redirect_url must be an http/https URL")
    upload.hidden = 1 if body.hidden else 0
    upload.hidden_redirect_url = redirect_url if body.hidden else None
    db.commit()
    return {"ok": True, "hidden": bool(upload.hidden), "redirect_url": upload.hidden_redirect_url}


@app.post("/api/admin/files/{file_id}/rename")
async def admin_rename_file(
    file_id: str,
    body: AdminRenameIn,
    db: Session = Depends(get_db),
    _=Depends(require_admin),
):
    filename = body.filename.strip()
    if not filename:
        raise HTTPException(400, "Filename cannot be empty")
    if len(filename) > 500:
        raise HTTPException(400, "Filename too long (max 500 chars)")
    upload = db.query(Upload).filter(Upload.id == file_id, Upload.status == "completed").first()
    if not upload:
        raise HTTPException(404, "File not found")
    upload.filename = filename
    db.commit()
    return {"ok": True, "filename": filename}


@app.post("/api/files/{file_id}/rename")
async def rename_own_file(
    file_id: str,
    body: AdminRenameIn,
    db: Session = Depends(get_db),
    auth: dict = Depends(require_auth),
    auth_key: Optional[ApiKey] = Depends(get_current_key),
):
    filename = body.filename.strip()
    if not filename:
        raise HTTPException(400, "Filename cannot be empty")
    if len(filename) > 500:
        raise HTTPException(400, "Filename too long (max 500 chars)")
    upload = db.query(Upload).filter(Upload.id == file_id, Upload.status == "completed").first()
    if not upload:
        raise HTTPException(404, "File not found")
    if auth.get("role") != "admin":
        if not auth_key or upload.uploaded_by_key_id != auth_key.id:
            raise HTTPException(403, "You can only rename files you uploaded")
    upload.filename = filename
    db.commit()
    return {"ok": True, "filename": filename}


class AdminDeleteIn(BaseModel):
    redirect_type: str = "none"          # "none" | "file" | "url"
    redirect_to_share_id: Optional[str] = None
    redirect_to_url:      Optional[str] = None


@app.post("/api/admin/files/{file_id}/delete")
async def admin_delete_file(
    file_id: str,
    body: AdminDeleteIn,
    db: Session = Depends(get_db),
    _=Depends(require_admin),
):
    """Delete a file with optional redirect: none (404), to another file, or to an external URL."""
    upload = db.query(Upload).filter(Upload.id == file_id, Upload.status == "completed").first()
    if not upload:
        raise HTTPException(404, "File not found")

    if body.redirect_type == "file":
        if not body.redirect_to_share_id:
            raise HTTPException(400, "redirect_to_share_id required")
        target = db.query(Upload).filter(
            Upload.share_id == body.redirect_to_share_id, Upload.status == "completed"
        ).first()
        if not target:
            raise HTTPException(404, "Redirect target file not found")
        if target.id == upload.id:
            raise HTTPException(400, "Cannot redirect a file to itself")
    elif body.redirect_type == "url":
        if not body.redirect_to_url:
            raise HTTPException(400, "redirect_to_url required")
        if not body.redirect_to_url.startswith(("http://", "https://")):
            raise HTTPException(400, "redirect_to_url must be an http/https URL")

    try:
        _get_storage(upload.storage_provider_id, db).delete_object(upload.b2_file_key)
    except Exception:
        pass

    # Storage object is gone in every branch — clean up its chunk records too
    db.query(Part).filter(Part.upload_id == upload.id).delete()

    if body.redirect_type == "none":
        db.delete(upload)
    elif body.redirect_type == "file":
        upload.status = "redirected"
        upload.redirects_to = body.redirect_to_share_id
    else:  # url
        upload.status = "redirected"
        upload.redirect_url = body.redirect_to_url

    db.commit()
    return {"ok": True}


class MergeUploadIn(BaseModel):
    redirect_to: str  # share_id of the canonical file to redirect to


_DUP_COPY_SUFFIX_RE = re.compile(r'\s*\(\d+\)\s*$')  # trailing " (1)", " (2)" auto-rename markers


def _dup_name_key(name: str) -> str:
    """Normalize a filename for duplicate matching: lowercase, trim, collapse
    whitespace, and drop a trailing "(n)" copy marker before the extension.
    So "Game (1).ZIP" and "game.zip" produce the same key."""
    s = (name or "").strip().lower()
    if "." in s:
        stem, ext = s.rsplit(".", 1)
    else:
        stem, ext = s, ""
    stem = _DUP_COPY_SUFFIX_RE.sub("", stem).strip()
    stem = re.sub(r"\s+", " ", stem)
    return f"{stem}.{ext}" if ext else stem


# Generic tokens that don't help tell two files apart (format/platform words)
_DUP_STOPWORDS = {
    "nsp", "xci", "nsz", "xcz", "iso", "zip", "rar", "7z", "pkg", "bin",
    "switch", "nintendo", "edition", "base", "game", "eur", "usa", "jpn",
    "the", "of", "and", "for", "version", "update", "dlc", "repack",
}


def _dup_tokens(name: str) -> set:
    stem = name.rsplit(".", 1)[0] if "." in name else name
    toks = re.findall(r"[a-z0-9]+", stem.lower())
    return {t for t in toks if len(t) > 1 and t not in _DUP_STOPWORDS}


def _names_similar(a: str, b: str) -> bool:
    """True if two filenames clearly refer to the same title — used only among
    files of the exact same byte size, so this decides "same game, different
    filename". Requires 2+ shared meaningful words and Jaccard >= 0.5."""
    ta, tb = _dup_tokens(a), _dup_tokens(b)
    if len(ta) < 2 or len(tb) < 2:
        # Short names: fall back to exact normalized-name equality
        return _dup_name_key(a) == _dup_name_key(b)
    inter = len(ta & tb)
    union = len(ta | tb)
    return inter >= 2 and union > 0 and (inter / union) >= 0.5


def _are_duplicates(a: Upload, b: Upload) -> bool:
    """Same pairing rule the detector uses: identical content hash, OR exact same
    byte size with a matching/similar title."""
    if a.file_hash and a.file_hash == b.file_hash:
        return True
    if (a.file_size or 0) > 0 and a.file_size == b.file_size and _names_similar(a.filename, b.filename):
        return True
    return False


def _duplicate_components(db: Session) -> list:
    """Return connected components (lists of Upload) of duplicate files. Two files
    are linked if they share an identical content hash OR the same normalized
    filename + exact byte size. Only components with 2+ files are returned."""
    from collections import defaultdict
    uploads = (
        db.query(Upload)
        .filter(Upload.status == "completed", Upload.dup_excluded != 1)
        .order_by(Upload.completed_at)
        .all()
    )
    parent: dict = {u.id: u.id for u in uploads}

    def find(x):
        root = x
        while parent[root] != root:
            root = parent[root]
        while parent[x] != root:
            parent[x], x = root, parent[x]
        return root

    def union(a, b):
        ra, rb = find(a), find(b)
        if ra != rb:
            parent[ra] = rb

    seen_key: dict = {}
    size_buckets: dict = defaultdict(list)
    for u in uploads:
        keys = []
        if u.file_hash:                       # identical content
            keys.append(("h", u.file_hash))
        if u.file_size:                       # same name + exact size
            keys.append(("ns", _dup_name_key(u.filename), u.file_size))
            size_buckets[u.file_size].append(u)
        for k in keys:
            if k in seen_key:
                union(u.id, seen_key[k])
            else:
                seen_key[k] = u.id

    # Aggressive pass: among files of the EXACT same byte size, link any whose
    # names clearly refer to the same title (catches "Game.NSP" vs "Game Edition").
    for bucket in size_buckets.values():
        n = len(bucket)
        if n < 2 or n > 200:   # cap keeps this from blowing up on huge same-size sets
            continue
        for i in range(n):
            for j in range(i + 1, n):
                if find(bucket[i].id) == find(bucket[j].id):
                    continue
                if _names_similar(bucket[i].filename, bucket[j].filename):
                    union(bucket[i].id, bucket[j].id)

    comps: dict = defaultdict(list)
    for u in uploads:
        comps[find(u.id)].append(u)
    return [files for files in comps.values() if len(files) >= 2]


def _pick_keeper(files: list):
    """Choose which copy to keep: most downloads, then most views, then oldest."""
    return sorted(
        files,
        key=lambda u: (-(u.downloads or 0), -(u.views or 0), u.created_at or datetime.min),
    )[0]


@app.get("/api/admin/duplicates")
async def get_duplicates(db: Session = Depends(get_db), _=Depends(require_admin)):
    """Find duplicate uploads aggressively. Two files are considered the same if
    they share EITHER an identical content hash OR the same normalized filename
    AND exact byte size — the latter catches imported files (which have no hash)
    and re-uploaded copies. Groups are connected components across both signals."""
    components = _duplicate_components(db)
    all_files = [u for files in components for u in files]

    key_ids = list({u.uploaded_by_key_id for u in all_files if u.uploaded_by_key_id})
    key_map: dict = {}
    if key_ids:
        keys = db.query(ApiKey).filter(ApiKey.id.in_(key_ids)).all()
        key_map = {k.id: k.name for k in keys}

    result_groups = []
    total_wasted = 0
    total_duplicates = 0

    for files in components:
        if len(files) < 2:
            continue
        file_size = max((f.file_size or 0) for f in files)
        wasted = file_size * (len(files) - 1)
        total_wasted += wasted
        total_duplicates += len(files) - 1
        files_sorted = sorted(files, key=lambda u: (-(u.downloads or 0), u.created_at or datetime.min))
        hashes = {f.file_hash for f in files if f.file_hash}
        match_type = "content" if len(hashes) == 1 and all(f.file_hash for f in files) else "name+size"
        result_groups.append({
            "file_hash": (next(iter(hashes)) if hashes else ""),
            "match_type": match_type,
            "count": len(files),
            "wasted_bytes": wasted,
            "files": [
                {
                    "id": u.id,
                    "share_id": u.share_id,
                    "filename": u.filename,
                    "file_size": u.file_size,
                    "downloads": u.downloads or 0,
                    "views": u.views or 0,
                    "completed_at": u.completed_at.isoformat() if u.completed_at else None,
                    "uploaded_by": key_map.get(u.uploaded_by_key_id, "Unknown") if u.uploaded_by_key_id else "Admin",
                }
                for u in files_sorted
            ],
        })

    result_groups.sort(key=lambda g: g["wasted_bytes"], reverse=True)
    return {
        "groups": result_groups,
        "total_groups": len(result_groups),
        "total_wasted_bytes": total_wasted,
        "total_duplicate_files": total_duplicates,
    }


@app.get("/api/admin/duplicates/excluded")
async def get_excluded_files(db: Session = Depends(get_db), _=Depends(require_admin)):
    """Return all files the admin has marked as 'not a duplicate'."""
    uploads = (
        db.query(Upload)
        .filter(Upload.status == "completed", Upload.dup_excluded == 1)
        .order_by(Upload.completed_at.desc())
        .all()
    )
    key_ids = list({u.uploaded_by_key_id for u in uploads if u.uploaded_by_key_id})
    key_map: dict = {}
    if key_ids:
        keys = db.query(ApiKey).filter(ApiKey.id.in_(key_ids)).all()
        key_map = {k.id: k.name for k in keys}

    return [
        {
            "id": u.id,
            "share_id": u.share_id,
            "filename": u.filename,
            "file_size": u.file_size,
            "file_hash": u.file_hash,
            "uploaded_by": key_map.get(u.uploaded_by_key_id, "Unknown") if u.uploaded_by_key_id else "Admin",
            "completed_at": u.completed_at.isoformat() if u.completed_at else None,
        }
        for u in uploads
    ]


@app.post("/api/admin/files/{file_id}/ignore-duplicate")
async def ignore_duplicate(file_id: str, db: Session = Depends(get_db), _=Depends(require_admin)):
    """Mark a file as 'not a duplicate' — exclude it from the duplicates scanner."""
    upload = db.query(Upload).filter(Upload.id == file_id, Upload.status == "completed").first()
    if not upload:
        raise HTTPException(404, "File not found")
    upload.dup_excluded = 1
    db.commit()
    return {"ok": True}


@app.post("/api/admin/files/{file_id}/unignore-duplicate")
async def unignore_duplicate(file_id: str, db: Session = Depends(get_db), _=Depends(require_admin)):
    """Restore a previously ignored file back into duplicate scanning."""
    upload = db.query(Upload).filter(Upload.id == file_id).first()
    if not upload:
        raise HTTPException(404, "File not found")
    upload.dup_excluded = 0
    db.commit()
    return {"ok": True}


@app.post("/api/admin/files/{file_id}/merge")
async def merge_duplicate(
    file_id: str,
    body: MergeUploadIn,
    db: Session = Depends(get_db),
    _=Depends(require_admin),
):
    """Soft-delete a duplicate: remove from storage, redirect its share link to the canonical."""
    duplicate = db.query(Upload).filter(Upload.id == file_id, Upload.status == "completed").first()
    if not duplicate:
        raise HTTPException(404, "Duplicate file not found")

    canonical = db.query(Upload).filter(Upload.share_id == body.redirect_to, Upload.status == "completed").first()
    if not canonical:
        raise HTTPException(404, "Canonical file not found")

    if duplicate.id == canonical.id:
        raise HTTPException(400, "Source and target are the same file")

    if not _are_duplicates(duplicate, canonical):
        raise HTTPException(400, "Files are not duplicates (different content and different name/size)")

    try:
        _get_storage(duplicate.storage_provider_id, db).delete_object(duplicate.b2_file_key)
    except Exception:
        pass

    db.query(Part).filter(Part.upload_id == duplicate.id).delete()
    duplicate.status = "redirected"
    duplicate.redirects_to = canonical.share_id
    db.commit()
    return {"ok": True, "redirects_to": canonical.share_id}


class MergeGroupIn(BaseModel):
    keep_share_id: str          # canonical to keep and redirect everything else to
    file_ids: list[str]         # duplicates to delete + redirect


@app.post("/api/admin/duplicates/merge-group")
async def merge_duplicate_group(
    body: MergeGroupIn,
    db: Session = Depends(get_db),
    _=Depends(require_admin),
):
    """Delete every listed duplicate and redirect its share link to the kept file,
    in one call. Skips files that aren't a real duplicate of the canonical."""
    canonical = db.query(Upload).filter(
        Upload.share_id == body.keep_share_id, Upload.status == "completed"
    ).first()
    if not canonical:
        raise HTTPException(404, "Canonical file not found")

    dups = db.query(Upload).filter(
        Upload.id.in_([i for i in body.file_ids if i]),
        Upload.status == "completed",
    ).all()

    merged = 0
    freed = 0
    for dup in dups:
        if dup.id == canonical.id:
            continue
        if not _are_duplicates(dup, canonical):
            continue
        try:
            _get_storage(dup.storage_provider_id, db).delete_object(dup.b2_file_key)
        except Exception:
            pass
        db.query(Part).filter(Part.upload_id == dup.id).delete()
        dup.status = "redirected"
        dup.redirects_to = canonical.share_id
        freed += dup.file_size or 0
        merged += 1
    db.commit()
    return {"ok": True, "merged": merged, "freed_bytes": freed, "redirects_to": canonical.share_id}


@app.post("/api/admin/duplicates/merge-all")
async def merge_all_duplicates(db: Session = Depends(get_db), _=Depends(require_admin)):
    """Auto-resolve every duplicate group in one pass: keep the copy with the most
    downloads (ties broken by views, then oldest), delete the rest from storage,
    and redirect their share links to the kept file."""
    components = _duplicate_components(db)
    groups = 0
    merged = 0
    freed = 0
    for files in components:
        keeper = _pick_keeper(files)
        if not keeper.share_id:
            continue  # can't redirect to a file without a share link
        group_merged = 0
        for dup in files:
            if dup.id == keeper.id:
                continue
            try:
                _get_storage(dup.storage_provider_id, db).delete_object(dup.b2_file_key)
            except Exception:
                pass
            db.query(Part).filter(Part.upload_id == dup.id).delete()
            dup.status = "redirected"
            dup.redirects_to = keeper.share_id
            freed += dup.file_size or 0
            group_merged += 1
        if group_merged:
            groups += 1
            merged += group_merged
    db.commit()
    return {"ok": True, "groups": groups, "merged": merged, "freed_bytes": freed}


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
async def get_share_info(
    share_id: str,
    background_tasks: BackgroundTasks,
    db: Session = Depends(get_db),
    _ref: str = Query("", alias="_ref"),
):
    upload = db.query(Upload).filter(Upload.share_id == share_id).first()
    if upload and upload.status == "redirected":
        if upload.redirect_url:
            return RedirectResponse(upload.redirect_url, status_code=302)
        canonical = _resolve_canonical(share_id, db)
        if canonical and canonical != share_id:
            return RedirectResponse(f"/api/f/{canonical}", status_code=301)
        raise HTTPException(404, "File not found")
    # Retired file — keep the link alive with a "max downloads reached" notice
    if upload and upload.status == "quota_reached":
        return {
            "state": "quota_reached",
            "filename": upload.filename,
            "message": _get_setting(db, "quota_message")
                or "This file has reached its maximum downloads for today. Please visit again tomorrow to download it.",
        }
    # Hidden (DMCA) file: redirect its share link if a target was set, else 404
    if upload and upload.hidden:
        if upload.hidden_redirect_url:
            return RedirectResponse(upload.hidden_redirect_url, status_code=302)
        raise HTTPException(404, "File not found")
    if not upload or upload.status != "completed":
        raise HTTPException(404, "File not found")

    from sqlalchemy import text as _sql_text
    db.execute(_sql_text("UPDATE uploads SET views = COALESCE(views,0)+1 WHERE id=:id"), {"id": upload.id})
    db.commit()
    db.refresh(upload)

    # _ref is the domain extracted from document.referrer by landing.js —
    # more reliable than the HTTP Referer header on the fetch() call itself.
    if _ref and len(_ref) <= 300:
        background_tasks.add_task(_log_referrer, share_id, _ref)

    return {
        "share_id": share_id,
        "filename": upload.filename,
        "file_size": upload.file_size,
        "content_type": upload.content_type,
        "views": upload.views,
        "downloads": upload.downloads or 0,
        "completed_at": upload.completed_at.isoformat() if upload.completed_at else None,
    }


def _resolve_canonical(share_id: str, db: Session, _depth: int = 0) -> Optional[str]:
    """Follow redirects_to chain and return the final canonical share_id, or None if broken."""
    if _depth > 10:
        return None
    upload = db.query(Upload).filter(Upload.share_id == share_id).first()
    if not upload:
        return None
    if upload.status == "completed":
        return share_id
    if upload.status == "redirected" and upload.redirects_to:
        return _resolve_canonical(upload.redirects_to, db, _depth + 1)
    return None


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
            db.flush()  # write the reset before the atomic increment below

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
        _track_bandwidth(p.id, upload.file_size or 0, db)
        return _get_storage(p.id, db).get_download_url(upload.b2_file_key)

    # Safety net
    return _get_storage(upload.storage_provider_id, db).get_download_url(upload.b2_file_key)


@app.post("/api/f/{share_id}/token")
async def create_download_token(share_id: str, db: Session = Depends(get_db)):
    """Issue a short-lived single-use download token for the share page."""
    upload = db.query(Upload).filter(Upload.share_id == share_id).first()
    if upload and upload.status == "redirected":
        if upload.redirect_url:
            return RedirectResponse(upload.redirect_url, status_code=307)
        canonical = _resolve_canonical(share_id, db)
        if canonical and canonical != share_id:
            return RedirectResponse(f"/api/f/{canonical}/token", status_code=307)
        raise HTTPException(404, "File not found")
    if not upload or upload.status != "completed" or upload.hidden:
        raise HTTPException(404, "File not found")
    token = secrets.token_urlsafe(24)
    # Tokens live in the DB so they work across all uvicorn workers
    db.add(DownloadToken(token=token, share_id=share_id,
                         expires=datetime.utcnow() + timedelta(seconds=120)))
    db.query(DownloadToken).filter(DownloadToken.expires < datetime.utcnow()).delete()
    db.commit()
    return {"token": token}


@app.get("/api/f/{share_id}/download")
async def download_file(
    share_id: str,
    request: Request,
    background_tasks: BackgroundTasks,
    token: Optional[str] = Query(None),
    db: Session = Depends(get_db),
):
    upload = db.query(Upload).filter(Upload.share_id == share_id).first()

    # Validate and consume the token first so it's never reusable, even on redirect
    tok = (
        db.query(DownloadToken).filter(DownloadToken.token == token).first()
        if token else None
    )
    if tok is None or tok.share_id != share_id or tok.expires < datetime.utcnow():
        raise HTTPException(403, "Missing or expired download token. Please use the share page to download.")
    db.delete(tok)
    db.commit()

    if upload and upload.status == "redirected":
        if upload.redirect_url:
            return RedirectResponse(upload.redirect_url, status_code=302)
        canonical = _resolve_canonical(share_id, db)
        if canonical and canonical != share_id:
            return RedirectResponse(f"/api/f/{canonical}/download", status_code=302)
        raise HTTPException(404, "File not found")
    if not upload or upload.status != "completed" or upload.hidden:
        raise HTTPException(404, "File not found")

    ip = _client_ip(request)

    # Dedup: same IP + same file within DEDUP_WINDOW_SECS counts as one download
    now = datetime.utcnow()
    dedup_key = (ip, upload.id)
    last = _recent_downloads.get(dedup_key)
    is_dup = last is not None and (now - last).total_seconds() < DEDUP_WINDOW_SECS
    if not is_dup:
        _recent_downloads[dedup_key] = now
        # Atomic SQL increment — safe with multiple uvicorn workers
        from sqlalchemy import text as _sql_text
        db.execute(
            _sql_text("UPDATE uploads SET downloads = COALESCE(downloads, 0) + 1 WHERE id = :id"),
            {"id": upload.id},
        )
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


# ── File Preview ─────────────────────────────────────────────────────────────

@app.get("/api/f/{share_id}/preview")
async def preview_file(share_id: str, db: Session = Depends(get_db)):
    """Redirect to the raw file URL for inline media preview. No token, no download count."""
    upload = db.query(Upload).filter(Upload.share_id == share_id).first()
    if upload and upload.status == "redirected":
        if upload.redirect_url:
            return RedirectResponse(upload.redirect_url, status_code=302)
        canonical = _resolve_canonical(share_id, db)
        if canonical and canonical != share_id:
            return RedirectResponse(f"/api/f/{canonical}/preview", status_code=301)
    upload = (
        db.query(Upload)
        .filter(Upload.share_id == share_id, Upload.status == "completed")
        .first()
    )
    if not upload or upload.hidden:
        raise HTTPException(404, "File not found")
    url = _get_storage(upload.storage_provider_id, db).get_download_url(upload.b2_file_key)
    return RedirectResponse(url=url, status_code=302)


@app.get("/api/f/{share_id}/preview-text")
async def preview_text(share_id: str, db: Session = Depends(get_db)):
    """Fetch and return the first 50 KB of a text/code file for inline preview."""
    upload = (
        db.query(Upload)
        .filter(Upload.share_id == share_id, Upload.status == "completed")
        .first()
    )
    if not upload or upload.hidden:
        raise HTTPException(404, "File not found")
    url = _get_storage(upload.storage_provider_id, db).get_download_url(upload.b2_file_key)
    try:
        async with httpx.AsyncClient(follow_redirects=True, timeout=15.0) as client:
            resp = await client.get(url, headers={"Range": "bytes=0-51199"})
        raw = resp.content[:51200]
        try:
            text = raw.decode("utf-8")
        except UnicodeDecodeError:
            text = raw.decode("latin-1", errors="replace")
        truncated = len(raw) >= 51200
        return Response(
            content=text,
            media_type="text/plain; charset=utf-8",
            headers={"X-Preview-Truncated": "1" if truncated else "0"},
        )
    except Exception:
        raise HTTPException(502, "Could not fetch preview")


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


@app.get("/api/analytics/referrers")
async def get_referrer_analytics(
    days: int = Query(30, ge=1, le=365),
    day: Optional[str] = Query(None, pattern="^(today|yesterday)$"),
    db: Session = Depends(get_db),
    _=Depends(require_admin),
):
    q = db.query(Referrer.domain, func.sum(Referrer.visit_count).label("total"))

    if day:
        # Exact single-day counts come from day-bucketed rows only — legacy
        # lifetime rows (day IS NULL) can't be attributed to a specific day.
        now = datetime.utcnow()
        target = now.date() if day == "today" else (now.date() - timedelta(days=1))
        q = q.filter(Referrer.day == target.strftime("%Y-%m-%d"))
    else:
        since = datetime.utcnow() - timedelta(days=days)
        q = q.filter(Referrer.last_seen >= since)

    top_domains = (
        q.group_by(Referrer.domain)
        .order_by(func.sum(Referrer.visit_count).desc())
        .limit(50)
        .all()
    )

    total_visits = sum(r.total for r in top_domains)
    unique_domains = len(top_domains)

    # Top files per domain — for the most active domain, show which files it links to
    return {
        "total_visits": total_visits,
        "unique_domains": unique_domains,
        "top_domains": [{"domain": r.domain, "count": r.total} for r in top_domains],
    }


@app.get("/api/admin/bandwidth")
async def get_bandwidth_analytics(
    days: int = Query(30, ge=1, le=365),
    db: Session = Depends(get_db),
    _=Depends(require_admin),
):
    since = datetime.utcnow() - timedelta(days=days)

    # Per-provider per-day bytes served (join download_events → uploads for file_size)
    rows = (
        db.query(
            Upload.storage_provider_id,
            func.date(DownloadEvent.created_at).label("day"),
            func.sum(Upload.file_size).label("bytes"),
            func.count(DownloadEvent.id).label("cnt"),
        )
        .join(Upload, DownloadEvent.upload_id == Upload.id)
        .filter(DownloadEvent.created_at >= since)
        .group_by(Upload.storage_provider_id, func.date(DownloadEvent.created_at))
        .all()
    )

    # All-time totals per provider (for all-time-total stat card)
    alltime_rows = (
        db.query(
            Upload.storage_provider_id,
            func.sum(Upload.file_size).label("bytes"),
            func.count(DownloadEvent.id).label("cnt"),
        )
        .join(Upload, DownloadEvent.upload_id == Upload.id)
        .group_by(Upload.storage_provider_id)
        .all()
    )

    # Collect provider IDs to resolve names in one query
    all_pids = {r.storage_provider_id for r in rows} | {r.storage_provider_id for r in alltime_rows}
    pnames: dict = {}
    real_pids = [p for p in all_pids if p]
    if real_pids:
        for p in db.query(StorageProvider.id, StorageProvider.name).filter(StorageProvider.id.in_(real_pids)).all():
            pnames[p.id] = p.name

    def _pname(pid):
        if not pid:
            return "Default (env vars)"
        return pnames.get(pid, "Unknown")

    # Aggregate period data
    daily_map: dict = {}
    provider_period: dict = {}
    for r in rows:
        day_str = str(r.day)
        daily_map.setdefault(day_str, {"bytes": 0, "cnt": 0})
        daily_map[day_str]["bytes"] += r.bytes or 0
        daily_map[day_str]["cnt"]   += r.cnt or 0

        pid = r.storage_provider_id or ""
        provider_period.setdefault(pid, {"bytes": 0, "cnt": 0})
        provider_period[pid]["bytes"] += r.bytes or 0
        provider_period[pid]["cnt"]   += r.cnt or 0

    # Fill daily series (no gaps)
    today = datetime.utcnow().date()
    daily = []
    for i in range(days):
        day = today - timedelta(days=days - 1 - i)
        day_str = day.strftime("%Y-%m-%d")
        d = daily_map.get(day_str, {"bytes": 0, "cnt": 0})
        daily.append({"date": day.strftime("%b %d"), "bytes": d["bytes"], "downloads": d["cnt"]})

    # Provider list sorted by bytes desc
    providers_out = [
        {"id": pid, "name": _pname(pid), "bytes": t["bytes"], "downloads": t["cnt"]}
        for pid, t in provider_period.items()
    ]
    providers_out.sort(key=lambda x: x["bytes"], reverse=True)

    alltime_bytes = sum(r.bytes or 0 for r in alltime_rows)
    period_bytes  = sum(d["bytes"] for d in daily)
    period_dl     = sum(d["downloads"] for d in daily)

    return {
        "period_bytes":     period_bytes,
        "period_downloads": period_dl,
        "alltime_bytes":    alltime_bytes,
        "providers":        providers_out,
        "daily":            daily,
    }


# ── URL Import API ────────────────────────────────────────────────────────────


async def _assert_public_url(url: str) -> None:
    """Block SSRF by rejecting URLs that resolve to private/loopback addresses.
    DNS lookup runs in a thread pool so it never blocks the event loop."""
    parsed = None
    try:
        import urllib.parse as _up
        parsed = _up.urlparse(url)
    except Exception:
        pass
    if not parsed or parsed.scheme not in ("http", "https"):
        raise HTTPException(400, "URL must start with http:// or https://")
    host = parsed.hostname or ""
    # Block raw IP literals that are private/loopback/reserved
    try:
        ip = ipaddress.ip_address(host)
        if ip.is_private or ip.is_loopback or ip.is_link_local or ip.is_reserved or ip.is_multicast:
            raise HTTPException(400, "URL resolves to a disallowed address")
    except ValueError:
        pass  # not a raw IP — fall through to DNS lookup
    # Resolve hostname in a thread pool (socket.getaddrinfo is blocking)
    try:
        loop = asyncio.get_running_loop()
        infos = await asyncio.wait_for(
            loop.run_in_executor(None, lambda: socket.getaddrinfo(host, 80, socket.AF_INET, socket.SOCK_STREAM)),
            timeout=10.0,
        )
        for info in infos:
            ip = ipaddress.ip_address(info[4][0])
            if ip.is_private or ip.is_loopback or ip.is_link_local or ip.is_reserved or ip.is_multicast:
                raise HTTPException(400, "URL resolves to a disallowed address")
    except HTTPException:
        raise
    except asyncio.TimeoutError:
        raise HTTPException(400, "Cannot resolve host: DNS timeout")
    except Exception as e:
        raise HTTPException(400, f"Cannot resolve host: {e}")


class ImportIn(BaseModel):
    url: str
    filename: Optional[str] = None


@app.post("/api/import")
async def import_from_url(
    body: ImportIn,
    background_tasks: BackgroundTasks,
    db: Session = Depends(get_db),
    auth: dict = Depends(require_auth),
    auth_key: Optional[ApiKey] = Depends(get_current_key),
):
    _assert_uploads_enabled(auth, db)
    await _assert_public_url(body.url)

    # ── Mega.nz — client-side encrypted, cannot be handled by normal HTTP pipeline ──
    if _MEGA_RE.match(body.url):
        try:
            handle, key_b64 = _mega_parse(body.url)
            raw_key = _mega_b64(key_b64)
            if len(raw_key) != 32:
                raise ValueError(f"Invalid Mega key length ({len(raw_key)} bytes)")
            aes_key, aes_iv = _mega_key_iv(raw_key)

            # Call Mega API to get CDN download URL + file metadata
            async with httpx.AsyncClient(timeout=15.0) as _mc:
                _mr = await _mc.post(
                    "https://g.api.mega.co.nz/cs",
                    params={"id": secrets.randbelow(999999999), "app": "python"},
                    json=[{"a": "g", "g": 1, "p": handle}],
                )
                _mr.raise_for_status()
                api_data = _mr.json()

            if not api_data or not isinstance(api_data, list):
                raise ValueError("Empty response from Mega API")
            file_info = api_data[0]
            if isinstance(file_info, int):
                raise ValueError(_MEGA_ERRORS.get(file_info, f"Mega API error {file_info}"))

            mega_dl_url   = file_info.get("g") or ""
            mega_file_size = int(file_info.get("s") or 0)
            if not mega_dl_url:
                raise ValueError("Mega API did not return a download URL")

            # Decrypt file attributes to get the original filename
            filename = (body.filename or "").strip()
            if not filename:
                attr_enc = file_info.get("at", "")
                if attr_enc:
                    try:
                        from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes
                        from cryptography.hazmat.backends import default_backend
                        attr_raw = _mega_b64(attr_enc)
                        pad = (16 - len(attr_raw) % 16) % 16
                        _c = Cipher(algorithms.AES(aes_key), modes.CBC(b'\x00' * 16),
                                    backend=default_backend()).decryptor()
                        attr_dec = _c.update(attr_raw + b'\x00' * pad)
                        attr_str = attr_dec.decode('utf-8', errors='ignore')
                        _am = re.search(r'MEGA(\{.+?\})', attr_str)
                        if _am:
                            filename = json.loads(_am.group(1)).get('n', '')
                    except Exception:
                        pass
            filename = os.path.basename(filename or f"mega_{handle}").replace('\0', '') or f"mega_{handle}"

        except HTTPException:
            raise
        except Exception as exc:
            raise HTTPException(400, f"Mega.nz error: {exc}")

        if mega_file_size and mega_file_size > _max_bytes_setting(db):
            raise HTTPException(400, f"Remote file exceeds {_max_gb_value(db):g} GB limit")

        # Create storage slot
        file_key_r2  = f"uploads/{uuid.uuid4()}/{filename}"
        default_prov = _get_default_provider(db)
        provider_id  = default_prov.id if default_prov else None
        file_storage = _get_storage(provider_id, db)
        try:
            b2_upload_id = file_storage.create_multipart_upload(file_key_r2, "application/octet-stream")
        except Exception as exc:
            raise HTTPException(500, f"Storage error: {exc}")

        upload = Upload(
            id=str(uuid.uuid4()), share_id=_share_id(), file_hash="",
            filename=filename, file_size=mega_file_size,
            content_type="application/octet-stream",
            b2_upload_id=b2_upload_id, b2_file_key=file_key_r2,
            status="importing", views=0, downloads=0,
            storage_provider_id=provider_id,
            uploaded_by_key_id=auth_key.id if auth_key else None,
            created_at=datetime.utcnow(),
        )
        db.add(upload); db.commit()

        _import_progress[upload.id] = {
            "status": "importing", "bytes_done": 0,
            "total": mega_file_size, "filename": filename, "error": None,
        }
        background_tasks.add_task(
            _import_from_mega,
            upload.id, mega_dl_url, aes_key, aes_iv,
            file_key_r2, b2_upload_id, provider_id, mega_file_size,
        )
        return {"upload_id": upload.id, "filename": filename, "total": mega_file_size}

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

        background_tasks.add_task(
            _do_page_import, upload.id, final_url, provider_id,
            (body.filename or "").strip(),
        )

        return {
            "upload_id": upload.id,
            "filename": upload.filename,
            "total": 0,
        }

    # ── Direct file URL ──
    content_length = int(meta.get("content-length", 0) or 0)

    if content_length and content_length > _max_bytes_setting(db):
        raise HTTPException(400, f"Remote file exceeds {_max_gb_value(db):g} GB limit")

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
        # The import runs in whichever uvicorn worker took the POST — this poll
        # may have landed on the other one. Serve the DB-synced progress instead
        # of pretending the import is at 100%.
        upload = db.query(Upload).filter(Upload.id == upload_id).first()
        if not upload:
            raise HTTPException(404, "Import job not found")
        if upload.status in ("importing", "analyzing"):
            prog = {"status": upload.status,
                    "bytes_done": upload.import_bytes_done or 0,
                    "total": upload.file_size or 0, "error": None}
        else:
            prog = {"status": upload.status, "bytes_done": upload.file_size, "total": upload.file_size, "error": None}

    result = dict(prog)
    if prog.get("status") == "completed":
        upload = db.query(Upload).filter(Upload.id == upload_id).first()
        if upload:
            result["share_url"] = f"/f/{upload.share_id}"
            result["direct_url"] = _get_storage(upload.storage_provider_id, db).get_download_url(upload.b2_file_key)
            result["filename"] = upload.filename
            result["file_size"] = upload.file_size
    if result.get("status") in ("completed", "failed", "aborted"):
        _schedule_progress_cleanup(upload_id)
    return result


def _sync_import_progress(upload_id: str, bytes_done: int) -> bool:
    """Persist live import progress to the DB and return True if the upload was
    aborted (possibly from the other uvicorn worker process). Blocking — run in
    an executor. _import_progress is per-process, so the DB is the only channel
    shared by both workers: status polls and aborts can land on either one."""
    from database import SessionLocal
    from sqlalchemy import text as _sql_text
    db = SessionLocal()
    try:
        row = db.execute(
            _sql_text("SELECT status FROM uploads WHERE id = :id"), {"id": upload_id}
        ).first()
        if row and row[0] == "aborted":
            return True
        db.execute(
            _sql_text("UPDATE uploads SET import_bytes_done = :b WHERE id = :id"),
            {"b": bytes_done, "id": upload_id},
        )
        db.commit()
        return False
    except Exception:
        return False
    finally:
        db.close()


def _schedule_progress_cleanup(upload_id: str, delay: float = 900.0):
    """Drop a finished import's in-memory progress entry after `delay` seconds —
    the status endpoint falls back to the DB, so late polls still work."""
    try:
        asyncio.get_event_loop().call_later(delay, _import_progress.pop, upload_id, None)
    except Exception:
        pass


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
                          provider_id: Optional[str] = None,
                          user_filename: str = ""):
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
        detected = final_url.split("?")[0].rstrip("/").split("/")[-1]
        detected = os.path.basename(detected).replace("\0", "") or "imported_file"
        # User-provided filename always wins over auto-detected name
        filename = user_filename or detected

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


# ── Mega.nz helpers ──────────────────────────────────────────────────────────

_MEGA_RE = re.compile(
    r'https?://mega\.nz/(?:file/(?P<h1>[^#\s]+)#(?P<k1>[^\s&]+)|#!(?P<h2>[^!\s]+)!(?P<k2>[^\s&]+))'
)
_MEGA_ERRORS = {-1: "Internal error", -2: "Bad arguments", -3: "Rate limited",
                -9: "File not found or link expired", -11: "Access denied",
                -14: "Invalid node", -16: "Upload failed"}


def _mega_parse(url: str):
    m = _MEGA_RE.match(url)
    if not m:
        raise ValueError("Unrecognised Mega.nz URL format")
    return (m.group('h1'), m.group('k1')) if m.group('h1') else (m.group('h2'), m.group('k2'))


def _mega_b64(s: str) -> bytes:
    s = s.replace('-', '+').replace('_', '/')
    return base64.b64decode(s + '=' * ((-len(s)) % 4))


def _mega_key_iv(raw: bytes):
    """Return (aes_key_16b, cbc_iv_16b) from the 32-byte Mega file key."""
    a = struct.unpack('>8I', raw)
    key = struct.pack('>4I', a[0] ^ a[4], a[1] ^ a[5], a[2] ^ a[6], a[3] ^ a[7])
    iv  = struct.pack('>4I', a[4], a[5], 0, 0)
    return key, iv


async def _import_from_mega(upload_id: str, mega_dl_url: str,
                             aes_key: bytes, aes_iv: bytes,
                             file_key_r2: str, b2_upload_id: str,
                             provider_id: Optional[str], mega_file_size: int):
    """Stream a Mega.nz CDN file, decrypt AES-128-CBC on-the-fly, upload to R2."""
    from database import SessionLocal
    from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes
    from cryptography.hazmat.backends import default_backend

    loop = asyncio.get_event_loop()
    db   = SessionLocal()
    prog = _import_progress[upload_id]
    file_storage = _get_storage(provider_id, db)

    parts_dict: dict = {}
    errors:     list = []
    part_number      = 0
    buf              = bytearray()
    total_enc        = 0   # encrypted bytes received
    decrypted_count  = 0   # decrypted bytes queued for upload

    # maxsize must fit the shutdown sentinels (one per worker) after a drain
    part_queue: asyncio.Queue = asyncio.Queue(maxsize=max(IMPORT_QUEUE_MAX, IMPORT_WORKERS))

    async def upload_worker():
        while True:
            item = await part_queue.get()
            try:
                if item is None:
                    return
                if errors or prog.get("status") == "aborted":
                    continue
                pn, data = item
                # Retry transient part failures — uploads are idempotent per
                # PartNumber, and one hiccup shouldn't kill a multi-GB import.
                for attempt in range(3):
                    try:
                        etag = await loop.run_in_executor(
                            _upload_executor,
                            lambda d=data, p=pn: file_storage.upload_part(file_key_r2, b2_upload_id, p, d),
                        )
                        break
                    except Exception:
                        if attempt == 2 or prog.get("status") == "aborted":
                            raise
                        await asyncio.sleep(2 * (attempt + 1))
                parts_dict[pn] = etag
            except Exception as exc:
                errors.append(exc)
            finally:
                part_queue.task_done()

    workers = [asyncio.create_task(upload_worker()) for _ in range(IMPORT_WORKERS)]

    try:
        cipher    = Cipher(algorithms.AES(aes_key), modes.CBC(aes_iv), backend=default_backend())
        decryptor = cipher.decryptor()

        last_sync = loop.time()

        async with httpx.AsyncClient(
            follow_redirects=True,
            timeout=httpx.Timeout(30.0, read=600.0),
        ) as client:
            async with client.stream("GET", mega_dl_url) as resp:
                resp.raise_for_status()
                async for chunk in resp.aiter_bytes(IMPORT_READ_SIZE):
                    if prog.get("status") == "aborted" or errors:
                        break
                    buf.extend(chunk)
                    total_enc += len(chunk)
                    prog["bytes_done"] = total_enc

                    now = loop.time()
                    if now - last_sync >= 2.0:
                        last_sync = now
                        if await loop.run_in_executor(
                            None, _sync_import_progress, upload_id, total_enc
                        ):
                            prog["status"] = "aborted"

                    while len(buf) >= IMPORT_CHUNK:
                        if prog.get("status") == "aborted" or errors:
                            break
                        raw = bytes(buf[:IMPORT_CHUNK])
                        del buf[:IMPORT_CHUNK]
                        # CPU-bound AES-CBC on 64 MB blocks — run off the event
                        # loop so polls and socket reads aren't stalled. Calls
                        # stay sequential here, so decryptor state is safe.
                        decrypted = await loop.run_in_executor(
                            _upload_executor, decryptor.update, raw
                        )
                        # Trim if we've gone past the real file size
                        if mega_file_size > 0 and decrypted_count + len(decrypted) > mega_file_size:
                            decrypted = decrypted[:mega_file_size - decrypted_count]
                        decrypted_count += len(decrypted)
                        if decrypted:
                            part_number += 1
                            await part_queue.put((part_number, decrypted))

        # Flush remaining bytes (final partial AES block)
        if buf and not errors and prog.get("status") != "aborted":
            keep    = (mega_file_size - decrypted_count) if mega_file_size > 0 else len(buf)
            pad_len = (16 - len(buf) % 16) % 16
            raw     = bytes(buf) + b'\x00' * pad_len
            decrypted = (await loop.run_in_executor(
                _upload_executor, decryptor.update, raw
            ))[:keep]
            if decrypted:
                part_number += 1
                await part_queue.put((part_number, decrypted))

    finally:
        while not part_queue.empty():
            try:
                part_queue.get_nowait(); part_queue.task_done()
            except asyncio.QueueEmpty:
                break
        for _ in range(IMPORT_WORKERS):
            part_queue.put_nowait(None)

    await asyncio.gather(*workers, return_exceptions=True)

    if errors:
        prog["status"] = "failed"
        prog["error"]  = str(errors[0])[:300]
        upload = db.query(Upload).filter(Upload.id == upload_id).first()
        if upload:
            upload.status = "failed"; db.commit()
        try: file_storage.abort_multipart_upload(file_key_r2, b2_upload_id)
        except Exception: pass
        db.close(); return

    if prog.get("status") == "aborted":
        try: file_storage.abort_multipart_upload(file_key_r2, b2_upload_id)
        except Exception: pass
        db.close(); return

    if not parts_dict:
        prog["status"] = "failed"
        prog["error"]  = "Mega download returned an empty file"
        db.close(); return

    prog["status"] = "completing"
    parts = [{"part_number": pn, "etag": et} for pn, et in sorted(parts_dict.items())]
    try:
        await loop.run_in_executor(
            None, lambda: file_storage.complete_multipart_upload(file_key_r2, b2_upload_id, parts)
        )
    except Exception as exc:
        prog["status"] = "failed"
        prog["error"]  = f"Storage complete failed: {exc}"
        db.close(); return

    total_bytes = mega_file_size or total_enc
    upload = db.query(Upload).filter(Upload.id == upload_id).first()
    if upload:
        upload.status       = "completed"
        upload.file_size    = total_bytes
        upload.completed_at = datetime.utcnow()
        db.commit()

    prog["status"]     = "completed"
    prog["bytes_done"] = total_bytes
    prog["total"]      = total_bytes
    db.close()


async def _do_import(upload_id: str, url: str, file_key: str, b2_upload_id: str,
                     provider_id: Optional[str] = None):
    """Stream a remote file directly into the target storage provider."""
    from database import SessionLocal
    loop = asyncio.get_event_loop()
    db = SessionLocal()
    prog = _import_progress[upload_id]

    try:
        file_storage = _get_storage(provider_id, db)
        max_bytes = _max_bytes_setting(db)
        parts_dict: dict = {}  # part_number → etag
        errors: list = []
        part_number = 0
        buf = bytearray()
        total_bytes = 0

        # maxsize must fit the shutdown sentinels (one per worker) after a drain
        part_queue: asyncio.Queue = asyncio.Queue(maxsize=max(IMPORT_QUEUE_MAX, IMPORT_WORKERS))

        async def upload_worker():
            while True:
                item = await part_queue.get()
                try:
                    if item is None:  # sentinel — worker should exit
                        return
                    if errors or prog.get("status") == "aborted":
                        continue  # drain queue without processing
                    pn, data = item
                    # Retry transient part failures — uploads are idempotent per
                    # PartNumber, and one hiccup shouldn't kill a multi-GB import.
                    for attempt in range(3):
                        try:
                            etag = await loop.run_in_executor(
                                _upload_executor,
                                lambda d=data, p=pn: file_storage.upload_part(file_key, b2_upload_id, p, d),
                            )
                            break
                        except Exception:
                            if attempt == 2 or prog.get("status") == "aborted":
                                raise
                            await asyncio.sleep(2 * (attempt + 1))
                    parts_dict[pn] = etag
                except Exception as exc:
                    errors.append(exc)
                finally:
                    part_queue.task_done()

        workers = [asyncio.create_task(upload_worker()) for _ in range(IMPORT_WORKERS)]

        last_sync = loop.time()

        try:
            async with httpx.AsyncClient(
                follow_redirects=True,
                max_redirects=_MAX_REDIRECTS,
                timeout=httpx.Timeout(30.0, read=600.0),
                headers=_download_headers(url),
            ) as client:
                async with client.stream("GET", url) as resp:
                    resp.raise_for_status()
                    # Pre-flight HEAD may have been blocked (token URLs) — pick up
                    # the real size from the streaming response so the UI shows %.
                    if not prog.get("total"):
                        try:
                            prog["total"] = int(resp.headers.get("content-length", 0) or 0)
                        except (TypeError, ValueError):
                            pass
                    async for chunk in resp.aiter_bytes(IMPORT_READ_SIZE):
                        if prog.get("status") == "aborted" or errors:
                            break
                        buf.extend(chunk)
                        total_bytes += len(chunk)
                        prog["bytes_done"] = total_bytes

                        if max_bytes and total_bytes > max_bytes:
                            raise ValueError(
                                f"Remote file exceeds {max_bytes / 1_073_741_824:g} GB limit"
                            )

                        now = loop.time()
                        if now - last_sync >= 2.0:
                            last_sync = now
                            if await loop.run_in_executor(
                                None, _sync_import_progress, upload_id, total_bytes
                            ):
                                prog["status"] = "aborted"

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
        _schedule_progress_cleanup(upload_id)


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
        "application_key": (p.application_key[:4] + "••••••••••••") if p.application_key else "",
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
    type: str           # "banner" | "button" | "script"
    label: str
    image_url: Optional[str] = None
    link_url: Optional[str] = ""
    script_code: Optional[str] = None
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


def _validate_ad(body: AdIn) -> tuple:
    """Validate an ad payload; returns (link_url, script_code) to store."""
    if body.type not in ("banner", "button", "script"):
        raise HTTPException(400, "type must be 'banner', 'button' or 'script'")
    if body.type == "script":
        code = (body.script_code or "").strip()
        if not code:
            raise HTTPException(400, "script_code is required for a script ad")
        return "", code
    link = (body.link_url or "").strip()
    if not link.startswith(("http://", "https://")):
        raise HTTPException(400, "link_url must be an http/https URL")
    if body.type == "banner" and body.image_url and not body.image_url.startswith(("http://", "https://")):
        raise HTTPException(400, "image_url must be an http/https URL")
    return link, None


@app.post("/api/admin/ads")
async def create_ad(body: AdIn, db: Session = Depends(get_db), _=Depends(require_admin)):
    link_url, script_code = _validate_ad(body)
    ad = Ad(
        id=str(uuid.uuid4()),
        type=body.type,
        label=body.label[:300],
        image_url=body.image_url,
        link_url=link_url,
        script_code=script_code,
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
    link_url, script_code = _validate_ad(body)
    ad.type = body.type
    ad.label = body.label[:300]
    ad.image_url = body.image_url
    ad.link_url = link_url
    ad.script_code = script_code
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
        "script_code": a.script_code or "",
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


def _max_bytes_setting(db: Session) -> int:
    """Max upload size in bytes — the admin-panel setting overrides the
    MAX_FILE_SIZE_GB env default."""
    raw = _get_setting(db, "max_file_size_gb")
    if raw:
        try:
            v = float(raw)
            if 0 < v <= 10000:
                return int(v * 1_073_741_824)
        except ValueError:
            pass
    return MAX_BYTES


def _max_gb_value(db: Session) -> float:
    return round(_max_bytes_setting(db) / 1_073_741_824, 2)


def _uploads_paused(db: Session) -> bool:
    return _get_setting(db, "uploads_paused") == "1"


def _assert_uploads_enabled(auth: dict, db: Session):
    """Block new uploads/imports when an admin has paused the system.
    Admins are exempt so they can still upload while it's paused for the team."""
    if auth.get("role") == "admin":
        return
    if _uploads_paused(db):
        raise HTTPException(
            503,
            "Uploads are temporarily paused by the administrator. Please try again later.",
        )


@app.get("/api/settings")
async def get_public_settings(db: Session = Depends(get_db)):
    """Public endpoint — returns settings used by landing pages."""
    return {
        "redirect_url":    _get_setting(db, "redirect_url"),
        "popup_url":       _get_setting(db, "popup_url"),
        "monetag_head":    _get_setting(db, "monetag_head"),
        "monetag_banner":  _get_setting(db, "monetag_banner"),
        "monetag_side":    _get_setting(db, "monetag_side"),
        "download_hint":   _get_setting(db, "download_hint"),
        "download_click_script": _get_setting(db, "download_click_script"),
        "max_file_size_gb": _max_gb_value(db),
        "uploads_paused":  _uploads_paused(db),
    }


class UpdateSettingsIn(BaseModel):
    redirect_url:   Optional[str] = None
    popup_url:      Optional[str] = None
    monetag_head:   Optional[str] = None
    monetag_banner: Optional[str] = None
    monetag_side:   Optional[str] = None
    download_hint:  Optional[str] = None
    download_click_script: Optional[str] = None
    max_file_size_gb: Optional[float] = None
    uploads_paused: Optional[bool] = None


_TEXT_SETTING_KEYS = ("redirect_url", "popup_url", "monetag_head",
                      "monetag_banner", "monetag_side", "download_hint",
                      "download_click_script")


@app.post("/api/admin/settings")
async def update_settings(
    body: UpdateSettingsIn,
    db: Session = Depends(get_db),
    _=Depends(require_admin),
):
    # Only touch fields the client actually sent, so saving one setting
    # can never wipe the others.
    data = (body.model_dump(exclude_unset=True) if hasattr(body, "model_dump")
            else body.dict(exclude_unset=True))

    for url_key in ("redirect_url", "popup_url"):
        if data.get(url_key) and not data[url_key].startswith(("http://", "https://")):
            raise HTTPException(400, f"{url_key} must be an http/https URL")

    for key in _TEXT_SETTING_KEYS:
        if key in data:
            _upsert_setting(db, key, data[key] or None)

    if "max_file_size_gb" in data:
        v = data["max_file_size_gb"]
        if v is None:
            _upsert_setting(db, "max_file_size_gb", None)  # back to env default
        else:
            if not (0.1 <= v <= 10000):
                raise HTTPException(400, "Max upload size must be between 0.1 and 10000 GB")
            _upsert_setting(db, "max_file_size_gb", f"{v:g}")

    if "uploads_paused" in data:
        _upsert_setting(db, "uploads_paused", "1" if data["uploads_paused"] else None)

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


@app.get("/api/admin/member-upload-counts")
async def member_upload_counts(db: Session = Depends(get_db), _=Depends(require_admin)):
    """How many files each team member uploaded today and yesterday (UTC days)."""
    now = datetime.utcnow()
    today_start     = datetime(now.year, now.month, now.day)
    yesterday_start = today_start - timedelta(days=1)
    tomorrow_start  = today_start + timedelta(days=1)

    def _counts(start, end):
        rows = (
            db.query(Upload.uploaded_by_key_id, func.count(Upload.id))
            .filter(
                Upload.status == "completed",
                Upload.completed_at >= start,
                Upload.completed_at < end,
            )
            .group_by(Upload.uploaded_by_key_id)
            .all()
        )
        return dict(rows)

    today_counts     = _counts(today_start, tomorrow_start)
    yesterday_counts = _counts(yesterday_start, today_start)

    keys = db.query(ApiKey).all()
    name_by_id = {k.id: k.name for k in keys}
    member_ids = [k.id for k in keys if not k.is_master and k.active]

    def _fmt(counts):
        out = []
        # Every active team member appears, even with 0 uploads
        for kid in member_ids:
            out.append({"name": name_by_id.get(kid, "Unknown"), "count": counts.get(kid, 0)})
        # Admin / legacy uploads only shown when they actually uploaded something
        for kid, cnt in counts.items():
            if kid in member_ids:
                continue
            label = name_by_id.get(kid) if kid else "Unattributed"
            out.append({"name": label or "Unknown", "count": cnt})
        out.sort(key=lambda r: (-r["count"], r["name"].lower()))
        return out

    return {"today": _fmt(today_counts), "yesterday": _fmt(yesterday_counts)}


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


# ── Access Requests ───────────────────────────────────────────────────────────

class AccessRequestIn(BaseModel):
    name: str
    email: str
    reason: Optional[str] = None
    website: Optional[str] = None  # honeypot — humans never see this field


def _req_dict(r: AccessRequest) -> dict:
    return {
        "id": r.id,
        "name": r.name,
        "email": r.email,
        "reason": r.reason,
        "status": r.status,
        "key_id": r.key_id,
        "created_at": r.created_at.isoformat(),
        "updated_at": r.updated_at.isoformat() if r.updated_at else None,
    }


@app.post("/api/access-requests")
async def submit_access_request(body: AccessRequestIn, db: Session = Depends(get_db)):
    """Public: submit an access key request."""
    if body.website:  # honeypot filled → bot. Fake success so it doesn't adapt.
        return {"ok": True, "id": str(uuid.uuid4())}
    name = body.name.strip()
    email = body.email.strip().lower()
    if not name or not email or "@" not in email:
        raise HTTPException(400, "Valid name and email are required")
    existing = db.query(AccessRequest).filter(
        AccessRequest.email == email,
        AccessRequest.status.in_(["pending", "approved"]),
    ).first()
    if existing:
        raise HTTPException(409, "A request with this email already exists")
    req = AccessRequest(
        id=str(uuid.uuid4()),
        name=name,
        email=email,
        reason=body.reason.strip() if body.reason else None,
        status="pending",
        created_at=datetime.utcnow(),
    )
    db.add(req)
    db.commit()
    return {"ok": True, "id": req.id}


@app.get("/api/admin/access-requests")
async def list_access_requests(
    status: Optional[str] = Query(None),
    db: Session = Depends(get_db),
    _=Depends(require_admin),
):
    q = db.query(AccessRequest)
    if status:
        q = q.filter(AccessRequest.status == status)
    rows = q.order_by(AccessRequest.created_at.desc()).all()
    pending = db.query(func.count(AccessRequest.id)).filter(AccessRequest.status == "pending").scalar()
    return {"requests": [_req_dict(r) for r in rows], "pending_count": pending}


@app.post("/api/admin/access-requests/{req_id}/approve")
async def approve_access_request(req_id: str, db: Session = Depends(get_db), _=Depends(require_admin)):
    """Approve request and auto-generate a member API key."""
    req = db.query(AccessRequest).filter(AccessRequest.id == req_id).first()
    if not req:
        raise HTTPException(404, "Request not found")
    if req.status == "approved":
        raise HTTPException(409, "Already approved")
    new_key = ApiKey(
        id=str(uuid.uuid4()),
        name=req.name,
        key=secrets.token_urlsafe(32),
        role="member",
        active=1,
        created_at=datetime.utcnow(),
    )
    db.add(new_key)
    req.status = "approved"
    req.key_id = new_key.id
    req.updated_at = datetime.utcnow()
    db.commit()
    return {"ok": True, "key": new_key.key, "key_id": new_key.id, "name": req.name, "email": req.email}


@app.post("/api/admin/access-requests/{req_id}/reject")
async def reject_access_request(req_id: str, db: Session = Depends(get_db), _=Depends(require_admin)):
    req = db.query(AccessRequest).filter(AccessRequest.id == req_id).first()
    if not req:
        raise HTTPException(404, "Request not found")
    req.status = "rejected"
    req.updated_at = datetime.utcnow()
    db.commit()
    return {"ok": True}


@app.delete("/api/admin/access-requests/{req_id}")
async def delete_access_request(req_id: str, db: Session = Depends(get_db), _=Depends(require_admin)):
    req = db.query(AccessRequest).filter(AccessRequest.id == req_id).first()
    if not req:
        raise HTTPException(404, "Request not found")
    db.delete(req)
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


# ── Support messages ──────────────────────────────────────────────────────────


def _validate_attachment_url(v: Optional[str]) -> Optional[str]:
    if not v:
        return v
    # Only allow local media uploads or absolute https URLs; reject javascript:, data:, etc.
    if v.startswith("/api/support/media/") or v.startswith("https://"):
        return v
    raise ValueError("attachment_url must be a local /api/support/media/ path or an https:// URL")


class SupportMessageIn(BaseModel):
    subject: str
    body: str
    attachment_url: Optional[str] = None

    class Config:
        pass

    def __init__(self, **data):
        super().__init__(**data)
        self.attachment_url = _validate_attachment_url(self.attachment_url)


class SupportReplyIn(BaseModel):
    reply: str
    attachment_url: Optional[str] = None

    def __init__(self, **data):
        super().__init__(**data)
        self.attachment_url = _validate_attachment_url(self.attachment_url)


def _support_replies(msg_id: str, db: Session) -> list:
    rows = (
        db.query(SupportReply)
        .filter(SupportReply.message_id == msg_id)
        .order_by(SupportReply.created_at.asc())
        .all()
    )
    return [{"id": r.id, "sender": r.sender, "body": r.body,
             "attachment_url": r.attachment_url,
             "created_at": r.created_at.isoformat() if r.created_at else None} for r in rows]


def _support_dict(m: SupportMessage, db: Session = None) -> dict:
    return {
        "id":             m.id,
        "key_id":         m.key_id,
        "member_name":    m.member_name,
        "subject":        m.subject,
        "body":           m.body,
        "attachment_url": m.attachment_url,
        "status":         m.status,
        "created_at":     m.created_at.isoformat() if m.created_at else None,
        "replied_at":     m.replied_at.isoformat() if m.replied_at else None,
        "replies":        _support_replies(m.id, db) if db else [],
    }


@app.post("/api/support/messages")
async def submit_support_message(
    body: SupportMessageIn,
    auth: dict = Depends(require_auth),
    x_api_key: Optional[str] = Header(None),
    db: Session = Depends(get_db),
):
    key_obj = db.query(ApiKey).filter(ApiKey.key == x_api_key, ApiKey.active == 1).first()
    key_id = key_obj.id if key_obj else "env-admin"
    name   = key_obj.name if key_obj else auth.get("name", "Admin")
    msg = SupportMessage(
        id=str(uuid.uuid4()),
        key_id=key_id,
        member_name=name,
        subject=body.subject.strip()[:500],
        body=body.body.strip(),
        attachment_url=body.attachment_url,
        status="open",
        created_at=datetime.utcnow(),
    )
    db.add(msg)
    db.commit()
    db.refresh(msg)
    return _support_dict(msg, db)


@app.get("/api/support/messages")
async def get_my_support_messages(
    auth: dict = Depends(require_auth),
    x_api_key: Optional[str] = Header(None),
    db: Session = Depends(get_db),
):
    key_obj = db.query(ApiKey).filter(ApiKey.key == x_api_key, ApiKey.active == 1).first()
    key_id = key_obj.id if key_obj else "env-admin"
    msgs = (
        db.query(SupportMessage)
        .filter(SupportMessage.key_id == key_id)
        .order_by(SupportMessage.created_at.desc())
        .all()
    )
    return [_support_dict(m, db) for m in msgs]


@app.post("/api/support/messages/{msg_id}/reply")
async def member_reply_support(
    msg_id: str,
    body: SupportReplyIn,
    auth: dict = Depends(require_auth),
    x_api_key: Optional[str] = Header(None),
    db: Session = Depends(get_db),
):
    msg = db.query(SupportMessage).filter(SupportMessage.id == msg_id).first()
    if not msg:
        raise HTTPException(404, "Message not found")
    key_obj = db.query(ApiKey).filter(ApiKey.key == x_api_key, ApiKey.active == 1).first()
    key_id = key_obj.id if key_obj else "env-admin"
    if msg.key_id != key_id:
        raise HTTPException(403, "Not your message")
    if msg.status == "closed":
        raise HTTPException(400, "This thread is closed")
    r = SupportReply(
        id=str(uuid.uuid4()),
        message_id=msg_id,
        sender="member",
        body=body.reply.strip(),
        attachment_url=body.attachment_url,
        created_at=datetime.utcnow(),
    )
    db.add(r)
    msg.status = "open"   # re-open so admin sees it needs attention
    db.commit()
    db.refresh(msg)
    return _support_dict(msg, db)


@app.get("/api/admin/support/messages")
async def admin_list_support_messages(
    status: Optional[str] = Query(None),
    db: Session = Depends(get_db),
    _=Depends(require_admin),
):
    q = db.query(SupportMessage)
    if status:
        q = q.filter(SupportMessage.status == status)
    msgs = q.order_by(SupportMessage.created_at.desc()).all()
    return [_support_dict(m, db) for m in msgs]


@app.get("/api/admin/support/messages/count")
async def admin_support_open_count(
    db: Session = Depends(get_db),
    _=Depends(require_admin),
):
    count = db.query(SupportMessage).filter(SupportMessage.status == "open").count()
    return {"open": count}


@app.post("/api/admin/support/messages/{msg_id}/reply")
async def admin_reply_support(
    msg_id: str,
    body: SupportReplyIn,
    db: Session = Depends(get_db),
    _=Depends(require_admin),
):
    msg = db.query(SupportMessage).filter(SupportMessage.id == msg_id).first()
    if not msg:
        raise HTTPException(404, "Message not found")
    r = SupportReply(
        id=str(uuid.uuid4()),
        message_id=msg_id,
        sender="admin",
        body=body.reply.strip(),
        attachment_url=body.attachment_url,
        created_at=datetime.utcnow(),
    )
    db.add(r)
    msg.status = "replied"
    msg.replied_at = datetime.utcnow()
    db.commit()
    db.refresh(msg)
    return _support_dict(msg, db)


@app.post("/api/admin/support/messages/{msg_id}/close")
async def admin_close_support(
    msg_id: str,
    db: Session = Depends(get_db),
    _=Depends(require_admin),
):
    msg = db.query(SupportMessage).filter(SupportMessage.id == msg_id).first()
    if not msg:
        raise HTTPException(404, "Message not found")
    msg.status = "closed"
    db.commit()
    db.refresh(msg)
    return _support_dict(msg, db)


@app.post("/api/support/upload-image")
async def upload_support_image(
    file: UploadFile = File(...),
    _: dict = Depends(require_auth),
):
    if file.content_type not in _ALLOWED_IMG_TYPES:
        raise HTTPException(400, "Only JPEG, PNG, GIF, WebP, or BMP images are allowed")
    content = await file.read()
    if len(content) > 10 * 1024 * 1024:
        raise HTTPException(400, "Image too large — max 10 MB")
    ext = _IMG_EXT_MAP.get(file.content_type, ".jpg")
    filename = str(uuid.uuid4()) + ext
    with open(os.path.join(SUPPORT_MEDIA_DIR, filename), "wb") as f:
        f.write(content)
    return {"url": f"/api/support/media/{filename}"}


@app.get("/api/support/media/{filename}", include_in_schema=False)
async def serve_support_media(filename: str):
    if "/" in filename or "\\" in filename or ".." in filename:
        raise HTTPException(400, "Invalid filename")
    path = os.path.join(SUPPORT_MEDIA_DIR, filename)
    if not os.path.isfile(path):
        raise HTTPException(404, "Not found")
    return FileResponse(path)


@app.delete("/api/admin/support/messages/{msg_id}")
async def admin_delete_support(
    msg_id: str,
    db: Session = Depends(get_db),
    _=Depends(require_admin),
):
    msg = db.query(SupportMessage).filter(SupportMessage.id == msg_id).first()
    if not msg:
        raise HTTPException(404, "Message not found")
    db.query(SupportReply).filter(SupportReply.message_id == msg_id).delete()
    db.delete(msg)
    db.commit()
    return {"ok": True}


@app.delete("/api/support/messages/{msg_id}")
async def member_delete_support(
    msg_id: str,
    auth: dict = Depends(require_auth),
    x_api_key: Optional[str] = Header(None),
    db: Session = Depends(get_db),
):
    msg = db.query(SupportMessage).filter(SupportMessage.id == msg_id).first()
    if not msg:
        raise HTTPException(404, "Message not found")
    key_obj = db.query(ApiKey).filter(ApiKey.key == x_api_key, ApiKey.active == 1).first()
    key_id = key_obj.id if key_obj else "env-admin"
    if msg.key_id != key_id:
        raise HTTPException(403, "Not your message")
    db.query(SupportReply).filter(SupportReply.message_id == msg_id).delete()
    db.delete(msg)
    db.commit()
    return {"ok": True}


# ── File Reports ──────────────────────────────────────────────────────────────

VALID_REASONS = {"not_downloading", "link_expired", "corrupt_or_wrong", "other"}


class FileReportIn(BaseModel):
    share_id: str
    reason: str
    message: Optional[str] = None


# Rate limiting (5/IP/hour) is enforced centrally by the _abuse_protection middleware.
@app.post("/api/reports")
async def submit_report(body: FileReportIn, request: Request, db: Session = Depends(get_db)):
    if body.reason not in VALID_REASONS:
        raise HTTPException(400, "Invalid reason")
    upload = db.query(Upload).filter(Upload.share_id == body.share_id, Upload.status == "completed").first()
    if not upload:
        raise HTTPException(404, "File not found")
    ip = _client_ip(request)
    db.add(FileReport(
        id=str(uuid.uuid4()),
        share_id=body.share_id,
        filename=upload.filename,
        reason=body.reason,
        message=(body.message or "").strip() or None,
        ip=ip,
        status="open",
        created_at=datetime.utcnow(),
    ))
    db.commit()
    return {"ok": True}


@app.get("/api/admin/reports")
async def list_reports(
    status: str = Query(""),
    db: Session = Depends(get_db),
    _=Depends(require_admin),
):
    q = db.query(FileReport)
    if status:
        q = q.filter(FileReport.status == status)
    reports = q.order_by(FileReport.created_at.desc()).all()
    open_count     = db.query(func.count(FileReport.id)).filter(FileReport.status == "open").scalar() or 0
    resolved_count = db.query(func.count(FileReport.id)).filter(FileReport.status == "resolved").scalar() or 0
    return {
        "open_count":     open_count,
        "resolved_count": resolved_count,
        "reports": [
            {
                "id":         r.id,
                "share_id":   r.share_id,
                "filename":   r.filename,
                "reason":     r.reason,
                "message":    r.message,
                "ip":         r.ip,
                "status":     r.status,
                "created_at": r.created_at.isoformat(),
            }
            for r in reports
        ],
    }


@app.post("/api/admin/reports/{report_id}/resolve")
async def resolve_report(report_id: str, db: Session = Depends(get_db), _=Depends(require_admin)):
    report = db.query(FileReport).filter(FileReport.id == report_id).first()
    if not report:
        raise HTTPException(404, "Report not found")
    report.status = "resolved"
    db.commit()
    return {"ok": True}


@app.post("/api/admin/reports/{report_id}/reopen")
async def reopen_report(report_id: str, db: Session = Depends(get_db), _=Depends(require_admin)):
    report = db.query(FileReport).filter(FileReport.id == report_id).first()
    if not report:
        raise HTTPException(404, "Report not found")
    report.status = "open"
    db.commit()
    return {"ok": True}


# ── Serve frontend ────────────────────────────────────────────────────────────


@app.get("/f/{share_id}", include_in_schema=False)
async def share_page(share_id: str, request: Request, db: Session = Depends(get_db)):
    upload = db.query(Upload).filter(Upload.share_id == share_id).first()
    if upload and upload.status == "redirected":
        if upload.redirect_url:
            return RedirectResponse(upload.redirect_url, status_code=301)
        canonical = _resolve_canonical(share_id, db)
        if canonical and canonical != share_id:
            return RedirectResponse(f"/f/{canonical}", status_code=301)
    # Hidden (DMCA) file: redirect to the set target, else show the landing shell
    if upload and upload.hidden:
        if upload.hidden_redirect_url:
            return RedirectResponse(upload.hidden_redirect_url, status_code=302)
        return FileResponse(os.path.join(FRONTEND_DIR, "landing.html"))
    upload = (
        db.query(Upload)
        .filter(Upload.share_id == share_id, Upload.status == "completed")
        .first()
    )
    if not upload:
        return FileResponse(os.path.join(FRONTEND_DIR, "landing.html"))

    base_url = str(request.base_url).rstrip("/")
    html = _build_og_html(share_id, upload, base_url)
    return Response(content=html, media_type="text/html; charset=utf-8")


@app.get("/", include_in_schema=False)
async def serve_home():
    return FileResponse(os.path.join(FRONTEND_DIR, "home.html"))


_NO_CACHE  = {"Cache-Control": "no-cache"}


@app.get("/admin", include_in_schema=False)
async def serve_index():
    # no-cache so the admin panel HTML always revalidates — otherwise a cached
    # index.html runs against freshly-served app.js and new UI elements go missing.
    return FileResponse(os.path.join(FRONTEND_DIR, "index.html"), headers=_NO_CACHE)


@app.get("/request", include_in_schema=False)
async def serve_request():
    return FileResponse(os.path.join(FRONTEND_DIR, "request.html"), headers=_NO_CACHE)
_IMG_CACHE = {"Cache-Control": "public, max-age=604800"}  # 7 days for images


@app.get("/sw.js", include_in_schema=False)
async def serve_sw():
    return FileResponse(
        os.path.join(FRONTEND_DIR, "sw.js"),
        media_type="application/javascript",
        headers={"Cache-Control": "no-cache"},
    )


@app.get("/app.js", include_in_schema=False)
async def serve_js():
    return FileResponse(os.path.join(FRONTEND_DIR, "app.js"), headers=_NO_CACHE)


@app.get("/style.css", include_in_schema=False)
async def serve_css():
    return FileResponse(os.path.join(FRONTEND_DIR, "style.css"), headers=_NO_CACHE)


@app.get("/landing.js", include_in_schema=False)
async def serve_landing_js():
    return FileResponse(os.path.join(FRONTEND_DIR, "landing.js"), headers=_NO_CACHE)


@app.get("/browse.js", include_in_schema=False)
async def serve_browse_js_cached():
    return FileResponse(os.path.join(FRONTEND_DIR, "browse.js"), headers=_NO_CACHE)


@app.get("/home.js", include_in_schema=False)
async def serve_home_js_cached():
    return FileResponse(os.path.join(FRONTEND_DIR, "home.js"), headers=_NO_CACHE)


@app.get("/request.js", include_in_schema=False)
async def serve_request_js_cached():
    return FileResponse(os.path.join(FRONTEND_DIR, "request.js"), headers=_NO_CACHE)


@app.get("/favicon.ico", include_in_schema=False)
async def serve_favicon():
    return FileResponse(os.path.join(FRONTEND_DIR, "favicon.ico"), media_type="image/x-icon", headers=_IMG_CACHE)


@app.get("/logo.webp", include_in_schema=False)
async def serve_logo():
    return FileResponse(os.path.join(FRONTEND_DIR, "logo.webp"), media_type="image/webp", headers=_IMG_CACHE)


# ── Public browse page ────────────────────────────────────────────────────────

_BROWSE_CAT_EXTS: dict[str, list[str]] = {
    "video":    ["mp4", "mkv", "avi", "mov", "webm"],
    "audio":    ["mp3", "wav", "flac", "aac", "ogg"],
    "images":   ["jpg", "jpeg", "png", "gif", "webp", "svg", "bmp"],
    "archives": ["zip", "rar", "gz", "tar", "7z", "bz2"],
    "docs":     ["pdf", "doc", "docx", "xls", "xlsx", "csv", "ppt", "pptx", "txt"],
    "software": ["exe", "msi", "dmg", "iso", "apk"],
    "games":    ["nsp", "xci", "rom", "pkg"],
}
_BROWSE_ALL_EXTS = [ext for exts in _BROWSE_CAT_EXTS.values() for ext in exts]

_BROWSE_SORT = {
    "newest":   Upload.completed_at.desc(),
    "oldest":   Upload.completed_at.asc(),
    "name-az":  Upload.filename.asc(),
    "name-za":  Upload.filename.desc(),
    "largest":  Upload.file_size.desc(),
    "smallest": Upload.file_size.asc(),
}


@app.get("/api/public/stats")
async def public_stats(db: Session = Depends(get_db)):
    """Quick aggregate stats — file count, total size, and total data served."""
    row = db.query(
        func.count(Upload.id).label("file_count"),
        func.coalesce(func.sum(Upload.file_size), 0).label("total_size"),
        func.coalesce(func.sum(Upload.file_size * func.coalesce(Upload.downloads, 0)), 0).label("total_served"),
    ).filter(
        Upload.status == "completed",
        Upload.share_id.isnot(None),
        or_(Upload.hidden.is_(None), Upload.hidden == 0),
    ).one()
    return {
        "file_count":    int(row.file_count),
        "total_size":    int(row.total_size),
        "total_served":  int(row.total_served),
    }


@app.get("/api/public/files")
async def public_files(
    search:    str = Query("", max_length=200),
    category:  str = Query("all"),
    sort:      str = Query("newest"),
    page:      int = Query(1, ge=1),
    page_size: int = Query(60, ge=1, le=200),
    db: Session = Depends(get_db),
):
    """Paginated, filterable public file listing. Returns {total, files}."""
    q = db.query(
        Upload.filename, Upload.file_size, Upload.share_id, Upload.completed_at,
    ).filter(
        Upload.status == "completed",
        Upload.share_id.isnot(None),
        or_(Upload.hidden.is_(None), Upload.hidden == 0),
    )

    if search:
        q = q.filter(Upload.filename.ilike(f"%{search}%"))

    if category != "all":
        exts = _BROWSE_CAT_EXTS.get(category)
        if exts:
            q = q.filter(or_(*[Upload.filename.ilike(f"%.{e}") for e in exts]))
        else:  # "other" — exclude all known extensions
            q = q.filter(and_(*[~Upload.filename.ilike(f"%.{e}") for e in _BROWSE_ALL_EXTS]))

    q = q.order_by(_BROWSE_SORT.get(sort, Upload.completed_at.desc()))

    total = q.count()
    rows  = q.offset((page - 1) * page_size).limit(page_size).all()

    return {
        "total": total,
        "files": [
            {
                "filename":     r.filename,
                "file_size":    r.file_size,
                "share_id":     r.share_id,
                "completed_at": r.completed_at.isoformat() if r.completed_at else None,
            }
            for r in rows
        ],
    }


@app.get("/browse", include_in_schema=False)
async def serve_browse():
    return FileResponse(os.path.join(FRONTEND_DIR, "browse.html"))


@app.get("/sitemap.xml", include_in_schema=False)
async def sitemap(request: Request, db: Session = Depends(get_db)):
    base = os.getenv("SITE_URL", "").rstrip("/") or f"{request.url.scheme}://{request.url.netloc}"

    rows = (
        db.query(Upload.share_id, Upload.completed_at)
        .filter(Upload.status == "completed", Upload.share_id.isnot(None))
        .order_by(Upload.completed_at.desc())
        .all()
    )

    urls = [
        f'  <url><loc>{base}/</loc><changefreq>daily</changefreq><priority>1.0</priority></url>',
        f'  <url><loc>{base}/browse</loc><changefreq>hourly</changefreq><priority>0.9</priority></url>',
    ]
    for row in rows:
        lm = f"<lastmod>{row.completed_at.strftime('%Y-%m-%d')}</lastmod>" if row.completed_at else ""
        urls.append(
            f'  <url><loc>{base}/f/{row.share_id}</loc>{lm}'
            f'<changefreq>never</changefreq><priority>0.7</priority></url>'
        )

    xml = (
        '<?xml version="1.0" encoding="UTF-8"?>\n'
        '<urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">\n'
        + "\n".join(urls)
        + "\n</urlset>"
    )
    return Response(content=xml, media_type="application/xml")


@app.get("/robots.txt", include_in_schema=False)
async def robots(request: Request):
    base = os.getenv("SITE_URL", "").rstrip("/") or f"{request.url.scheme}://{request.url.netloc}"
    content = (
        "User-agent: *\n"
        "Allow: /\n"
        "Allow: /browse\n"
        "Allow: /f/\n"
        "Disallow: /api/\n"
        f"Sitemap: {base}/sitemap.xml\n"
    )
    return Response(content=content, media_type="text/plain")
