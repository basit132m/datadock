import hashlib
import os
import shutil
import uuid
from typing import List, Dict

import boto3
import httpx
from botocore.config import Config


class S3Storage:
    """Generic S3-compatible storage — works with Backblaze B2, Cloudflare R2,
    Wasabi, Amazon S3, Bunny Storage, and any other S3-compatible provider."""

    def __init__(self, endpoint_url: str, key_id: str, application_key: str,
                 bucket_name: str, public_base_url: str = ""):
        self.bucket = bucket_name
        self.public_base = (public_base_url or "").rstrip("/")
        self.s3 = boto3.client(
            "s3",
            endpoint_url=endpoint_url,
            aws_access_key_id=key_id,
            aws_secret_access_key=application_key,
            config=Config(signature_version="s3v4"),
        )

    def test_connection(self):
        """Raises if credentials or bucket are invalid."""
        self.s3.list_objects_v2(Bucket=self.bucket, MaxKeys=1)

    # ── Multipart lifecycle ──────────────────────────────────────────────────

    def create_multipart_upload(self, key: str, content_type: str) -> str:
        resp = self.s3.create_multipart_upload(
            Bucket=self.bucket, Key=key, ContentType=content_type,
        )
        return resp["UploadId"]

    def upload_part(self, key: str, upload_id: str, part_number: int, data: bytes) -> str:
        resp = self.s3.upload_part(
            Bucket=self.bucket, Key=key, UploadId=upload_id,
            PartNumber=part_number, Body=data,
        )
        return resp["ETag"].strip('"')

    def list_uploaded_parts(self, key: str, upload_id: str) -> List[Dict]:
        parts: List[Dict] = []
        kwargs: Dict = {"Bucket": self.bucket, "Key": key, "UploadId": upload_id}
        while True:
            resp = self.s3.list_parts(**kwargs)
            for p in resp.get("Parts", []):
                parts.append({"part_number": p["PartNumber"], "etag": p["ETag"].strip('"')})
            if resp.get("IsTruncated"):
                kwargs["PartNumberMarker"] = resp["NextPartNumberMarker"]
            else:
                break
        return parts

    def complete_multipart_upload(self, key: str, upload_id: str, parts: List[Dict]) -> None:
        self.s3.complete_multipart_upload(
            Bucket=self.bucket, Key=key, UploadId=upload_id,
            MultipartUpload={
                "Parts": sorted(
                    [{"PartNumber": p["part_number"], "ETag": p["etag"]} for p in parts],
                    key=lambda x: x["PartNumber"],
                )
            },
        )

    def abort_multipart_upload(self, key: str, upload_id: str) -> None:
        try:
            self.s3.abort_multipart_upload(Bucket=self.bucket, Key=key, UploadId=upload_id)
        except Exception:
            pass

    # ── Object operations ────────────────────────────────────────────────────

    def delete_object(self, key: str) -> None:
        self.s3.delete_object(Bucket=self.bucket, Key=key)

    def get_download_url(self, key: str, expires: int = 604800) -> str:
        if self.public_base:
            return f"{self.public_base}/{key}"
        return self.s3.generate_presigned_url(
            "get_object",
            Params={"Bucket": self.bucket, "Key": key},
            ExpiresIn=expires,
        )


class B2Storage(S3Storage):
    """Backwards-compatible: reads credentials from environment variables."""
    def __init__(self):
        super().__init__(
            endpoint_url=os.getenv("B2_ENDPOINT_URL", ""),
            key_id=os.getenv("B2_KEY_ID", ""),
            application_key=os.getenv("B2_APPLICATION_KEY", ""),
            bucket_name=os.getenv("B2_BUCKET_NAME", ""),
            public_base_url=os.getenv("B2_PUBLIC_BASE_URL", ""),
        )


class BunnyStorage:
    """Bunny.net Edge Storage — uses Bunny's HTTP API (not S3-compatible).
    Parts are buffered to disk in TEMP_DIR, then streamed to Bunny on complete."""

    TEMP_DIR = "/tmp/datadock_bunny"

    def __init__(self, zone: str, api_key: str, region: str,
                 public_base_url: str = ""):
        self.zone       = zone
        self.api_key    = api_key
        self.base_url   = f"https://{region.rstrip('/')}"
        self.public_base = (public_base_url or "").rstrip("/")
        os.makedirs(self.TEMP_DIR, exist_ok=True)

    # ── Helpers ──────────────────────────────────────────────────────────────

    def _part_dir(self, upload_id: str) -> str:
        return os.path.join(self.TEMP_DIR, upload_id)

    def _part_path(self, upload_id: str, part_number: int) -> str:
        return os.path.join(self._part_dir(upload_id), f"{part_number:06d}")

    def _headers(self) -> dict:
        return {"AccessKey": self.api_key}

    # ── Connection test ───────────────────────────────────────────────────────

    def test_connection(self):
        with httpx.Client(timeout=15) as client:
            r = client.get(f"{self.base_url}/{self.zone}/", headers=self._headers())
            r.raise_for_status()

    # ── Multipart lifecycle (disk-buffered) ───────────────────────────────────

    def create_multipart_upload(self, key: str, content_type: str) -> str:
        upload_id = str(uuid.uuid4())
        os.makedirs(self._part_dir(upload_id), exist_ok=True)
        return upload_id

    def upload_part(self, key: str, upload_id: str, part_number: int, data: bytes) -> str:
        os.makedirs(self._part_dir(upload_id), exist_ok=True)
        with open(self._part_path(upload_id, part_number), "wb") as f:
            f.write(data)
        return hashlib.md5(data).hexdigest()

    def list_uploaded_parts(self, key: str, upload_id: str) -> List[Dict]:
        part_dir = self._part_dir(upload_id)
        if not os.path.isdir(part_dir):
            return []
        parts = []
        for fname in sorted(os.listdir(part_dir)):
            try:
                pn = int(fname)
                with open(os.path.join(part_dir, fname), "rb") as f:
                    parts.append({"part_number": pn,
                                  "etag": hashlib.md5(f.read()).hexdigest()})
            except Exception:
                pass
        return parts

    def complete_multipart_upload(self, key: str, upload_id: str, parts: List[Dict]) -> None:
        sorted_parts = sorted(parts, key=lambda p: p["part_number"])

        def _stream():
            for p in sorted_parts:
                path = self._part_path(upload_id, p["part_number"])
                with open(path, "rb") as f:
                    while True:
                        chunk = f.read(65_536)
                        if not chunk:
                            break
                        yield chunk

        with httpx.Client(timeout=None) as client:
            r = client.put(
                f"{self.base_url}/{self.zone}/{key}",
                headers={**self._headers(), "Content-Type": "application/octet-stream"},
                content=_stream(),
            )
            r.raise_for_status()

        shutil.rmtree(self._part_dir(upload_id), ignore_errors=True)

    def abort_multipart_upload(self, key: str, upload_id: str) -> None:
        shutil.rmtree(self._part_dir(upload_id), ignore_errors=True)

    # ── Object operations ─────────────────────────────────────────────────────

    def delete_object(self, key: str) -> None:
        with httpx.Client(timeout=30) as client:
            r = client.delete(
                f"{self.base_url}/{self.zone}/{key}",
                headers=self._headers(),
            )
            if r.status_code not in (200, 204, 404):
                r.raise_for_status()

    def get_download_url(self, key: str, **_) -> str:
        if self.public_base:
            return f"{self.public_base}/{key}"
        return f"{self.base_url}/{self.zone}/{key}"
