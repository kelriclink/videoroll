from __future__ import annotations

import mimetypes
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

import boto3
from botocore.config import Config
from botocore.exceptions import ClientError
from botocore.response import StreamingBody

from videoroll.config import CommonSettings


@dataclass(frozen=True)
class PutResult:
    bucket: str
    key: str
    etag: Optional[str] = None


class S3Store:
    def __init__(self, settings: CommonSettings) -> None:
        self._bucket = settings.s3_bucket
        self._client = boto3.client(
            "s3",
            endpoint_url=settings.s3_endpoint_url,
            aws_access_key_id=settings.s3_access_key_id,
            aws_secret_access_key=settings.s3_secret_access_key,
            region_name=settings.s3_region_name,
            use_ssl=settings.s3_use_ssl,
            config=Config(s3={"addressing_style": "path"}),
        )

    @property
    def bucket(self) -> str:
        return self._bucket

    def ensure_bucket(self) -> None:
        try:
            self._client.head_bucket(Bucket=self._bucket)
        except ClientError:
            self._client.create_bucket(Bucket=self._bucket)

    def upload_file(self, path: Path, key: str, content_type: Optional[str] = None) -> PutResult:
        if content_type is None:
            content_type, _ = mimetypes.guess_type(str(path))
        extra = {"ContentType": content_type} if content_type else None
        if extra:
            self._client.upload_file(str(path), self._bucket, key, ExtraArgs=extra)
        else:
            self._client.upload_file(str(path), self._bucket, key)
        return PutResult(bucket=self._bucket, key=key)

    def put_bytes(self, data: bytes, key: str, content_type: Optional[str] = None) -> PutResult:
        args = {"Bucket": self._bucket, "Key": key, "Body": data}
        if content_type:
            args["ContentType"] = content_type
        resp = self._client.put_object(**args)
        return PutResult(bucket=self._bucket, key=key, etag=resp.get("ETag"))

    def download_file(self, key: str, path: Path) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        self._client.download_file(self._bucket, key, str(path))

    def get_object(self, key: str) -> dict:
        return self._client.get_object(Bucket=self._bucket, Key=key)

    def delete_object(self, key: str) -> None:
        self._client.delete_object(Bucket=self._bucket, Key=key)

    @staticmethod
    def iter_body(body: StreamingBody, chunk_size: int = 1024 * 1024):
        try:
            for chunk in body.iter_chunks(chunk_size=chunk_size):
                if chunk:
                    yield chunk
        finally:
            try:
                body.close()
            except Exception:
                pass
