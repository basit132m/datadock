import os
import uuid
from datetime import datetime
from typing import List, Optional

from dotenv import load_dotenv

load_dotenv()

from fastapi import Depends, FastAPI, Header, HTTPException, Query
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel
from sqlalchemy.orm import Session

from database import get_db, init_db
from models import Part, Upload
from storage import B2Storage

# ── Config ───────────────────────────────────────────────────────────────────────

MAX_BYTES = int(os.getenv("MAX_FILE_SIZE_GB", "10")) * 1_073_741_824
API_KEY = os.getenv("API_KEY", "")
FRONTEND_DIR = os.path.join(os.path.dirname(__file__), "..", "frontend")

# ── App ───────────────────────────────────────────────────────────────────────────

app = FastAPI(title="DataDock", version="1.0.0", docs_url=None, redoc_url=None)
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


# ── Auth ─────────────────────────────────────────────────────────────────────────


def require_auth(x_api_key: Optional[str] = Header(None)):
    if API_KEY and x_api_key != API_KEY:
        raise HTTPException(status_code=401, detail="Invalid API key")


# ── Schemas ───────────────────────────────────────────────────────────────────────


class InitUploadIn(BaseModel):
    filename: str
    file_size: int
    file_hash: str
    content_type: str = "application/octet-stream"


class CompleteUploadIn(BaseModel):
    parts: List[dict]  # [{part_number: int, etag: str}]


# ── Upload API ───────────────────────────────────────────────────────────────────


@app.post("/api/upload/init")
async def init_upload(
    body: InitUploadIn,
    db: Session = Depends(get_db),
    _=Depends(require_auth),
):
    if body.file_size > MAX_BYTES:
        raise HTTPException(
            400, f"File exceeds {os.getenv('MAX_FILE_SIZE_GB', 10)} GB limit"
        )
    if body.file_size <= 0:
        raise HTTPException(400, "Invalid file size")

    # ── Resume existing pending upload ────────────────────────────────────────────
    existing = (
        db.query(Upload)
        .filter(Upload.file_hash == body.file_hash, Upload.status == "pending")
        .first()
    )
    if existing:
        # Sync DB parts with what B2 actually has (handles crashes mid-record)
        try:
            b2_parts = storage.list_uploaded_parts(
                existing.b2_file_key, existing.b2_upload_id
            )
            known = {
                p.part_number
                for p in db.query(Part)
                .filter(Part.upload_id == existing.id)
                .all()
            }
            for bp in b2_parts:
                if bp["part_number"] not in known:
                    db.add(
                        Part(
                            id=str(uuid.uuid4()),
                            upload_id=existing.id,
                            part_number=bp["part_number"],
                            etag=bp["etag"],
                            uploaded_at=datetime.utcnow(),
                        )
                    )
            db.commit()
        except Exception:
            pass

        parts = (
            db.query(Part).filter(Part.upload_id == existing.id).all()
        )
        return {
            "upload_id": existing.id,
            "resuming": True,
            "completed_parts": [
                {"part_number": p.part_number, "etag": p.etag} for p in parts
            ],
        }

    # ── New upload ────────────────────────────────────────────────────────────────
    safe_name = os.path.basename(body.filename).replace("\0", "") or "unnamed"
    file_key = f"uploads/{uuid.uuid4()}/{safe_name}"

    b2_upload_id = storage.create_multipart_upload(file_key, body.content_type)

    upload = Upload(
        id=str(uuid.uuid4()),
        file_hash=body.file_hash,
        filename=safe_name,
        file_size=body.file_size,
        content_type=body.content_type,
        b2_upload_id=b2_upload_id,
        b2_file_key=file_key,
        status="pending",
        created_at=datetime.utcnow(),
    )
    db.add(upload)
    db.commit()

    return {"upload_id": upload.id, "resuming": False, "completed_parts": []}


@app.get("/api/upload/{upload_id}/presign/{part_number}")
async def presign_part(
    upload_id: str,
    part_number: int,
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

    url = storage.presign_part(upload.b2_file_key, upload.b2_upload_id, part_number)
    return {"presigned_url": url}


@app.post("/api/upload/{upload_id}/part/{part_number}")
async def record_part(
    upload_id: str,
    part_number: int,
    etag: str = Query(...),
    db: Session = Depends(get_db),
    _=Depends(require_auth),
):
    upload = db.query(Upload).filter(Upload.id == upload_id).first()
    if not upload:
        raise HTTPException(404, "Upload not found")

    existing = (
        db.query(Part)
        .filter(Part.upload_id == upload_id, Part.part_number == part_number)
        .first()
    )
    clean_etag = etag.strip('"')
    if existing:
        existing.etag = clean_etag
        existing.uploaded_at = datetime.utcnow()
    else:
        db.add(
            Part(
                id=str(uuid.uuid4()),
                upload_id=upload_id,
                part_number=part_number,
                etag=clean_etag,
                uploaded_at=datetime.utcnow(),
            )
        )
    db.commit()
    return {"ok": True}


@app.post("/api/upload/{upload_id}/complete")
async def complete_upload(
    upload_id: str,
    body: CompleteUploadIn,
    db: Session = Depends(get_db),
    _=Depends(require_auth),
):
    upload = db.query(Upload).filter(Upload.id == upload_id).first()
    if not upload:
        raise HTTPException(404, "Upload not found")
    if upload.status != "pending":
        raise HTTPException(400, f"Upload is already {upload.status}")
    if not body.parts:
        raise HTTPException(400, "No parts provided")

    upload.status = "completing"
    db.commit()

    try:
        storage.complete_multipart_upload(
            upload.b2_file_key, upload.b2_upload_id, body.parts
        )
    except Exception as exc:
        upload.status = "failed"
        db.commit()
        raise HTTPException(500, f"B2 complete failed: {exc}") from exc

    upload.status = "completed"
    upload.completed_at = datetime.utcnow()
    db.commit()

    return {
        "ok": True,
        "download_url": storage.get_download_url(upload.b2_file_key),
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


# ── File manager API ──────────────────────────────────────────────────────────────


@app.get("/api/files")
async def list_files(
    db: Session = Depends(get_db),
    _=Depends(require_auth),
):
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
            "filename": f.filename,
            "file_size": f.file_size,
            "download_url": storage.get_download_url(f.b2_file_key),
            "completed_at": f.completed_at.isoformat() if f.completed_at else None,
        }
        for f in rows
    ]


@app.delete("/api/files/{file_id}")
async def delete_file(
    file_id: str,
    db: Session = Depends(get_db),
    _=Depends(require_auth),
):
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


# ── Serve frontend ────────────────────────────────────────────────────────────────

_frontend = os.path.abspath(FRONTEND_DIR)


@app.get("/", include_in_schema=False)
async def serve_index():
    return FileResponse(os.path.join(_frontend, "index.html"))


@app.get("/app.js", include_in_schema=False)
async def serve_js():
    return FileResponse(os.path.join(_frontend, "app.js"))


@app.get("/style.css", include_in_schema=False)
async def serve_css():
    return FileResponse(os.path.join(_frontend, "style.css"))
