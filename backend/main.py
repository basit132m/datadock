import os
import secrets
import string
import uuid
from datetime import datetime, timedelta
from typing import Optional

from dotenv import load_dotenv

load_dotenv()

from fastapi import Depends, FastAPI, Header, HTTPException, Query, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, RedirectResponse
from pydantic import BaseModel
from sqlalchemy import func
from sqlalchemy.orm import Session

from database import get_db, init_db
from models import Part, Upload
from storage import B2Storage

# ── Config ────────────────────────────────────────────────────────────────────

MAX_BYTES = int(os.getenv("MAX_FILE_SIZE_GB", "10")) * 1_073_741_824
API_KEY = os.getenv("API_KEY", "")
FRONTEND_DIR = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "frontend"))

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
            b2_parts = storage.list_uploaded_parts(existing.b2_file_key, existing.b2_upload_id)
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
    b2_upload_id = storage.create_multipart_upload(file_key, body.content_type)

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

    etag = storage.upload_part(upload.b2_file_key, upload.b2_upload_id, part_number, data)

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

    try:
        storage.complete_multipart_upload(upload.b2_file_key, upload.b2_upload_id, parts)
    except Exception as exc:
        upload.status = "failed"
        db.commit()
        raise HTTPException(500, f"B2 complete failed: {exc}") from exc

    upload.status = "completed"
    upload.completed_at = datetime.utcnow()
    db.commit()

    return {
        "ok": True,
        "share_url": f"/f/{upload.share_id}",
        "direct_url": storage.get_download_url(upload.b2_file_key),
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
        storage.abort_multipart_upload(upload.b2_file_key, upload.b2_upload_id)
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
    return [
        {
            "id": f.id,
            "share_id": f.share_id,
            "filename": f.filename,
            "file_size": f.file_size,
            "direct_url": storage.get_download_url(f.b2_file_key),
            "share_url": f"/f/{f.share_id}" if f.share_id else None,
            "views": f.views or 0,
            "downloads": f.downloads or 0,
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
        storage.delete_object(upload.b2_file_key)
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

    return RedirectResponse(url=storage.get_download_url(upload.b2_file_key))


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
