import os
from typing import List, Dict

import boto3
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
