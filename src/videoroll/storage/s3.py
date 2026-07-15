from __future__ import annotations

import mimetypes
from dataclasses import dataclass
from pathlib import Path
from collections.abc import Iterator
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
            config=Config(
                s3={"addressing_style": "path"},
                retries={"max_attempts": 10, "mode": "adaptive"},
                connect_timeout=10,
                read_timeout=120,
            ),
        )

    @property
    def bucket(self) -> str:
        return self._bucket

    @staticmethod
    def _is_missing_bucket_error(exc: ClientError) -> bool:
        err = exc.response.get("Error") or {}
        code = str(err.get("Code") or "").strip()
        status = int((exc.response.get("ResponseMetadata") or {}).get("HTTPStatusCode") or 0)
        return code in {"404", "NoSuchBucket", "NotFound"} or status == 404

    @staticmethod
    def _is_bucket_already_exists_error(exc: ClientError) -> bool:
        err = exc.response.get("Error") or {}
        code = str(err.get("Code") or "").strip()
        return code in {"BucketAlreadyOwnedByYou", "BucketAlreadyExists"}

    def ensure_bucket(self) -> None:
        try:
            self._client.head_bucket(Bucket=self._bucket)
        except ClientError as e:
            if not self._is_missing_bucket_error(e):
                raise
            try:
                self._client.create_bucket(Bucket=self._bucket)
            except ClientError as create_error:
                if self._is_bucket_already_exists_error(create_error):
                    return
                raise

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

    def head_object(self, key: str) -> dict:
        return self._client.head_object(Bucket=self._bucket, Key=key)

    def get_object(self, key: str, *, range_bytes: str | None = None) -> dict:
        args = {"Bucket": self._bucket, "Key": key}
        if range_bytes:
            args["Range"] = range_bytes
        return self._client.get_object(**args)

    def delete_object(self, key: str, *, bucket: str | None = None) -> None:
        self._client.delete_object(Bucket=bucket or self._bucket, Key=key)

    def list_bucket_names(self) -> list[str]:
        return [
            str(item.get("Name") or "").strip()
            for item in self._client.list_buckets().get("Buckets") or []
            if str(item.get("Name") or "").strip()
        ]

    def iter_object_keys(self, prefix: str = "", *, bucket: str | None = None) -> Iterator[str]:
        paginator = self._client.get_paginator("list_objects_v2")
        for page in paginator.paginate(Bucket=bucket or self._bucket, Prefix=prefix):
            for item in page.get("Contents") or []:
                key = str(item.get("Key") or "").strip()
                if key:
                    yield key

    def delete_objects(self, keys: list[str], *, bucket: str | None = None) -> tuple[set[str], set[str]]:
        """Delete up to 1,000 objects and return (deleted, failed) keys.

        S3 bulk deletes are idempotent: a key that is already absent is
        considered deleted, which makes retention retries safe.
        """
        unique_keys = list(dict.fromkeys(str(key).strip() for key in keys if str(key).strip()))
        if not unique_keys:
            return set(), set()
        if len(unique_keys) > 1000:
            raise ValueError("delete_objects accepts at most 1000 keys")
        try:
            response = self._client.delete_objects(
                Bucket=bucket or self._bucket,
                Delete={"Objects": [{"Key": key} for key in unique_keys], "Quiet": True},
            )
        except Exception:
            return set(), set(unique_keys)
        failed = {
            str(item.get("Key") or "").strip()
            for item in response.get("Errors") or []
            if str(item.get("Key") or "").strip()
        }
        return set(unique_keys) - failed, failed

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
