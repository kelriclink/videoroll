from __future__ import annotations

import mimetypes
import os
import shutil
import tempfile
import time
from collections.abc import Iterator
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import BinaryIO

from videoroll.config import CommonSettings


@dataclass(frozen=True)
class PutResult:
    key: str


class StorageObjectNotFound(FileNotFoundError):
    pass


class FileBody:
    """Small file wrapper matching the streaming methods used by the API."""

    def __init__(self, path: Path, *, start: int = 0, end: int | None = None) -> None:
        self._file = path.open("rb")
        self._file.seek(start)
        self._remaining = None if end is None else max(0, end - start + 1)

    def read(self, size: int = -1) -> bytes:
        if self._remaining == 0:
            return b""
        if self._remaining is not None:
            size = self._remaining if size is None or size < 0 else min(size, self._remaining)
        data = self._file.read(size)
        if self._remaining is not None:
            self._remaining = max(0, self._remaining - len(data))
        return data

    def iter_chunks(self, chunk_size: int = 1024 * 1024) -> Iterator[bytes]:
        while True:
            chunk = self.read(chunk_size)
            if not chunk:
                return
            yield chunk

    def close(self) -> None:
        self._file.close()


class FileStore:
    """Shared-filesystem object store using relative keys as its public IDs."""

    def __init__(self, settings: CommonSettings) -> None:
        self._root = Path(settings.storage_root).expanduser().resolve()
        self._partial_root = self._root.parent / ".partial"

    @property
    def root(self) -> Path:
        return self._root

    @property
    def partial_root(self) -> Path:
        return self._partial_root

    @property
    def bucket(self) -> str:
        """Compatibility namespace used by the existing retention queue."""
        return "filesystem"

    def list_bucket_names(self) -> list[str]:
        return [self.bucket]

    def ensure_ready(self) -> None:
        self._root.mkdir(parents=True, exist_ok=True)
        self._partial_root.mkdir(parents=True, exist_ok=True)
        probe = self._partial_root / f".write-test-{os.getpid()}"
        try:
            probe.write_bytes(b"")
        finally:
            probe.unlink(missing_ok=True)

    def cleanup_partials(self, *, older_than_seconds: int = 86400) -> int:
        """Remove abandoned upload files left by a killed worker."""
        if not self._partial_root.is_dir():
            return 0
        cutoff = time.time() - max(60, int(older_than_seconds))
        removed = 0
        for path in self._partial_root.rglob("*"):
            if not path.is_file() or not path.name.endswith(".partial"):
                continue
            try:
                if path.stat().st_mtime < cutoff:
                    path.unlink()
                    removed += 1
            except OSError:
                continue
        return removed

    def path_for(self, key: str, *, require_exists: bool = True) -> Path:
        raw = str(key or "").strip()
        posix = PurePosixPath(raw)
        if (
            not raw
            or "\x00" in raw
            or "\\" in raw
            or posix.is_absolute()
            or any(part in {"", ".", ".."} for part in posix.parts)
        ):
            raise ValueError("invalid storage key")
        path = self._root.joinpath(*posix.parts)
        try:
            path.resolve(strict=False).relative_to(self._root)
        except ValueError as exc:
            raise ValueError("storage key escapes STORAGE_ROOT") from exc
        if require_exists and not path.is_file():
            raise StorageObjectNotFound(raw)
        return path

    def _destination(self, key: str) -> Path:
        path = self.path_for(key, require_exists=False)
        path.parent.mkdir(parents=True, exist_ok=True)
        self._partial_root.mkdir(parents=True, exist_ok=True)
        return path

    @staticmethod
    def _content_type(path: Path) -> str:
        overrides = {
            ".ass": "text/plain",
            ".log": "text/plain",
            ".srt": "application/x-subrip",
        }
        return overrides.get(path.suffix.lower()) or mimetypes.guess_type(path.name)[0] or "application/octet-stream"

    @staticmethod
    def _replace_from_temp(temp_path: Path, destination: Path) -> None:
        os.replace(temp_path, destination)

    def upload_file(self, path: Path, key: str, content_type: str | None = None) -> PutResult:
        del content_type
        source = Path(path)
        if not source.is_file():
            raise FileNotFoundError(source)
        destination = self._destination(key)
        if source.resolve() == destination.resolve(strict=False):
            return PutResult(key=key)
        temp_path: Path | None = None
        try:
            with tempfile.NamedTemporaryFile(
                prefix=f".{destination.name}.", suffix=".partial", dir=self._partial_root, delete=False
            ) as output:
                temp_path = Path(output.name)
                with source.open("rb") as input_file:
                    shutil.copyfileobj(input_file, output, length=8 * 1024 * 1024)
                output.flush()
                os.fsync(output.fileno())
            self._replace_from_temp(temp_path, destination)
            temp_path = None
        finally:
            if temp_path is not None:
                temp_path.unlink(missing_ok=True)
        return PutResult(key=key)

    def promote_file(self, path: Path, key: str) -> PutResult:
        """Move a completed work file into storage without retaining a second copy."""
        source = Path(path)
        if not source.is_file():
            raise FileNotFoundError(source)
        destination = self._destination(key)
        if source.resolve() == destination.resolve(strict=False):
            return PutResult(key=key)
        try:
            os.replace(source, destination)
        except OSError:
            self.upload_file(source, key)
            source.unlink()
        return PutResult(key=key)

    def put_bytes(self, data: bytes, key: str, content_type: str | None = None) -> PutResult:
        del content_type
        destination = self._destination(key)
        temp_path: Path | None = None
        try:
            with tempfile.NamedTemporaryFile(
                prefix=f".{destination.name}.", suffix=".partial", dir=self._partial_root, delete=False
            ) as output:
                temp_path = Path(output.name)
                output.write(data)
                output.flush()
                os.fsync(output.fileno())
            self._replace_from_temp(temp_path, destination)
            temp_path = None
        finally:
            if temp_path is not None:
                temp_path.unlink(missing_ok=True)
        return PutResult(key=key)

    def download_file(self, key: str, path: Path) -> None:
        source = self.path_for(key)
        destination = Path(path)
        destination.parent.mkdir(parents=True, exist_ok=True)
        if source.resolve() == destination.resolve(strict=False):
            return
        temp_path = destination.with_name(f".{destination.name}.{os.getpid()}.partial")
        try:
            with source.open("rb") as input_file, temp_path.open("wb") as output:
                shutil.copyfileobj(input_file, output, length=8 * 1024 * 1024)
            os.replace(temp_path, destination)
        finally:
            temp_path.unlink(missing_ok=True)

    def copy_object(
        self,
        source_key: str,
        destination_key: str,
        *,
        source_bucket: str | None = None,
        destination_bucket: str | None = None,
    ) -> PutResult:
        del source_bucket, destination_bucket
        source = self.path_for(source_key)
        destination = self._destination(destination_key)
        with tempfile.NamedTemporaryFile(
            prefix=f".{destination.name}.", suffix=".partial", dir=self._partial_root, delete=False
        ) as temp_file:
            temp_path = Path(temp_file.name)
        temp_path.unlink()
        try:
            try:
                os.link(source, temp_path)
            except OSError:
                shutil.copy2(source, temp_path)
            os.replace(temp_path, destination)
        finally:
            temp_path.unlink(missing_ok=True)
        return PutResult(key=destination_key)

    def head_object(self, key: str) -> dict[str, object]:
        path = self.path_for(key)
        return {
            "ContentLength": path.stat().st_size,
            "ContentType": self._content_type(path),
        }

    @staticmethod
    def _parse_range(value: str, total_size: int) -> tuple[int, int]:
        raw = str(value or "").strip().lower()
        if not raw.startswith("bytes=") or "," in raw:
            raise ValueError("invalid byte range")
        start_text, separator, end_text = raw[6:].partition("-")
        if not separator:
            raise ValueError("invalid byte range")
        if not start_text:
            suffix = int(end_text)
            if suffix <= 0:
                raise ValueError("invalid byte range")
            return max(0, total_size - suffix), total_size - 1
        start = int(start_text)
        end = total_size - 1 if not end_text else min(int(end_text), total_size - 1)
        if start < 0 or start >= total_size or end < start:
            raise ValueError("invalid byte range")
        return start, end

    def get_object(self, key: str, *, range_bytes: str | None = None) -> dict[str, object]:
        path = self.path_for(key)
        total_size = path.stat().st_size
        start, end = (0, total_size - 1)
        if range_bytes:
            start, end = self._parse_range(range_bytes, total_size)
        length = max(0, end - start + 1)
        result: dict[str, object] = {
            "Body": FileBody(path, start=start, end=end if range_bytes else None),
            "ContentLength": length,
            "ContentType": self._content_type(path),
        }
        if range_bytes:
            result["ContentRange"] = f"bytes {start}-{end}/{total_size}"
        return result

    def delete_object(self, key: str, *, bucket: str | None = None) -> None:
        del bucket
        path = self.path_for(key, require_exists=False)
        path.unlink(missing_ok=True)
        parent = path.parent
        while parent != self._root:
            try:
                parent.rmdir()
            except OSError:
                break
            parent = parent.parent

    def iter_object_keys(self, prefix: str = "", *, bucket: str | None = None) -> Iterator[str]:
        del bucket
        normalized = str(prefix or "").strip().lstrip("/")
        if normalized:
            self.path_for(normalized.rstrip("/"), require_exists=False)
        if not self._root.is_dir():
            return
        for path in self._root.rglob("*"):
            if not path.is_file():
                continue
            key = path.relative_to(self._root).as_posix()
            if key.startswith(normalized):
                yield key

    def delete_objects(self, keys: list[str], *, bucket: str | None = None) -> tuple[set[str], set[str]]:
        del bucket
        deleted: set[str] = set()
        failed: set[str] = set()
        for key in dict.fromkeys(str(value).strip() for value in keys if str(value).strip()):
            try:
                self.delete_object(key)
                deleted.add(key)
            except Exception:
                failed.add(key)
        return deleted, failed

    @staticmethod
    def iter_body(body: BinaryIO | FileBody, chunk_size: int = 1024 * 1024) -> Iterator[bytes]:
        try:
            iterator = getattr(body, "iter_chunks", None)
            if callable(iterator):
                yield from iterator(chunk_size=chunk_size)
                return
            while True:
                chunk = body.read(chunk_size)
                if not chunk:
                    return
                yield chunk
        finally:
            try:
                body.close()
            except Exception:
                pass
