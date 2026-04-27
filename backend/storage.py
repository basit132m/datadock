import os
from typing import List, Dict

import boto3
from botocore.config import Config


class B2Storage:
    def __init__(self):
        self.s3 = boto3.client(
            "s3",
            endpoint_url=os.getenv("B2_ENDPOINT_URL"),
            aws_access_key_id=os.getenv("B2_KEY_ID"),
            aws_secret_access_key=os.getenv("B2_APPLICATION_KEY"),
            config=Config(signature_version="s3v4"),
        )
        self.bucket = os.getenv("B2_BUCKET_NAME", "")
        self.public_base = os.getenv("B2_PUBLIC_BASE_URL", "").rstrip("/")

    # ── Multipart lifecycle ──────────────────────────────────────────────────────

    def create_multipart_upload(self, key: str, content_type: str) -> str:
        resp = self.s3.create_multipart_upload(
            Bucket=self.bucket,
            Key=key,
            ContentType=content_type,
        )
        return resp["UploadId"]

    def presign_part(
        self, key: str, upload_id: str, part_number: int, expires: int = 3600
    ) -> str:
        return self.s3.generate_presigned_url(
            "upload_part",
            Params={
                "Bucket": self.bucket,
                "Key": key,
                "UploadId": upload_id,
                "PartNumber": part_number,
            },
            ExpiresIn=expires,
        )

    def complete_multipart_upload(
        self, key: str, upload_id: str, parts: List[Dict]
    ) -> None:
        self.s3.complete_multipart_upload(
            Bucket=self.bucket,
            Key=key,
            UploadId=upload_id,
            MultipartUpload={
                "Parts": sorted(
                    [
                        {"PartNumber": p["part_number"], "ETag": p["etag"]}
                        for p in parts
                    ],
                    key=lambda x: x["PartNumber"],
                )
            },
        )

    def upload_part(self, key: str, upload_id: str, part_number: int, data: bytes) -> str:
        resp = self.s3.upload_part(
            Bucket=self.bucket,
            Key=key,
            UploadId=upload_id,
            PartNumber=part_number,
            Body=data,
        )
        return resp["ETag"].strip('"')

    def abort_multipart_upload(self, key: str, upload_id: str) -> None:
        try:
            self.s3.abort_multipart_upload(
                Bucket=self.bucket, Key=key, UploadId=upload_id
            )
        except Exception:
            pass

    # ── Resume helpers ───────────────────────────────────────────────────────────

    def list_uploaded_parts(self, key: str, upload_id: str) -> List[Dict]:
        """Return [{part_number, etag}] already stored in B2 for this multipart upload."""
        parts: List[Dict] = []
        kwargs: Dict = {"Bucket": self.bucket, "Key": key, "UploadId": upload_id}
        while True:
            resp = self.s3.list_parts(**kwargs)
            for p in resp.get("Parts", []):
                parts.append(
                    {
                        "part_number": p["PartNumber"],
                        "etag": p["ETag"].strip('"'),
                    }
                )
            if resp.get("IsTruncated"):
                kwargs["PartNumberMarker"] = resp["NextPartNumberMarker"]
            else:
                break
        return parts

    # ── Object operations ────────────────────────────────────────────────────────

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
