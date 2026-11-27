"""Object storage abstraction: local filesystem (dev) and S3-compatible (production).

Guarantees
----------
* Keys are always generated server-side from UUIDs. User-supplied strings (filenames, project
  names) never become part of a path, which removes path traversal as a category of bug.
* Nothing is ever served by the API process: callers receive short-lived signed URLs.
* The local backend refuses to start in production (enforced in Settings).
"""

from __future__ import annotations

import hashlib
import shutil
import uuid
from dataclasses import dataclass
from pathlib import Path

from app.core.config import Settings


@dataclass(frozen=True, slots=True)
class StoredObject:
    key: str
    bytes: int
    sha256: str


class StorageError(RuntimeError):
    pass


class Storage:
    """Interface. Implementations must never produce a path from user input."""

    def put_stream(self, *, key: str, chunks, max_bytes: int) -> StoredObject:  # noqa: ANN001
        raise NotImplementedError

    def open(self, key: str):  # noqa: ANN201 - binary stream, backend-specific type
        raise NotImplementedError

    def delete(self, key: str) -> None:
        raise NotImplementedError

    def exists(self, key: str) -> bool:
        raise NotImplementedError

    def signed_url(self, key: str, *, expires_in: int, download_name: str | None = None) -> str:
        raise NotImplementedError

    def size(self, key: str) -> int:
        raise NotImplementedError


def build_key(*, workspace_id: uuid.UUID, project_id: uuid.UUID, kind: str, ext: str) -> str:
    """Server-generated object key. No user-controlled component.

    Shape: ws/<workspace>/<kind>/<project>/<uuid><ext>
    """
    safe_kind = "".join(c for c in kind if c.isalnum() or c == "_")[:32]
    safe_ext = "." + "".join(c for c in ext.lstrip(".").lower() if c.isalnum())[:8] if ext else ""
    return f"ws/{workspace_id}/{safe_kind}/{project_id}/{uuid.uuid4().hex}{safe_ext}"


# --------------------------------------------------------------------- local


class LocalStorage(Storage):
    """Development backend. Files live under LOCAL_STORAGE_ROOT, outside any web root."""

    def __init__(self, root: str) -> None:
        self.root = Path(root).resolve()
        self.root.mkdir(parents=True, exist_ok=True)

    def _path(self, key: str) -> Path:
        # Defence in depth: even though keys are generated, verify containment.
        candidate = (self.root / key).resolve()
        if not str(candidate).startswith(str(self.root)):
            raise StorageError("resolved path escapes the storage root")
        return candidate

    def put_stream(self, *, key: str, chunks, max_bytes: int) -> StoredObject:  # noqa: ANN001
        path = self._path(key)
        path.parent.mkdir(parents=True, exist_ok=True)
        digest = hashlib.sha256()
        total = 0
        # Write to a temp sibling then rename, so a failed upload never leaves a partial object
        # that looks complete.
        tmp = path.with_suffix(path.suffix + ".part")
        try:
            with tmp.open("wb") as fh:
                for chunk in chunks:
                    if not chunk:
                        continue
                    total += len(chunk)
                    if total > max_bytes:
                        raise StorageError(
                            f"upload exceeds the {max_bytes // (1024 * 1024)} MiB limit"
                        )
                    digest.update(chunk)
                    fh.write(chunk)
            tmp.replace(path)
        finally:
            tmp.unlink(missing_ok=True)
        return StoredObject(key=key, bytes=total, sha256=digest.hexdigest())

    def open(self, key: str):  # noqa: ANN201
        return self._path(key).open("rb")

    def delete(self, key: str) -> None:
        self._path(key).unlink(missing_ok=True)

    def exists(self, key: str) -> bool:
        return self._path(key).exists()

    def size(self, key: str) -> int:
        return self._path(key).stat().st_size

    def signed_url(
        self,
        key: str,
        *,
        expires_in: int,
        download_name: str | None = None,
    ) -> str:
        """Local dev has no signing authority.

        Returning a fake signed URL would be a lie that breaks in production, so this raises
        unless a dedicated media-serving route is configured. The API never streams media itself.
        """
        raise StorageError(
            "local storage cannot issue signed URLs; configure S3-compatible storage for "
            "downloads, or serve via the development media route"
        )

    def free_bytes(self) -> int:
        return shutil.disk_usage(self.root).free


# --------------------------------------------------------------------- s3


class S3Storage(Storage):
    """S3-compatible backend (AWS S3, Cloudflare R2, MinIO).

    `boto3` is imported lazily: it is an optional dependency that only production deployments
    need, and importing it eagerly would make the local backend unusable without it.
    """

    def __init__(self, settings: Settings) -> None:
        import boto3  # noqa: PLC0415 - optional dependency, only for the S3 backend
        from botocore.config import Config  # noqa: PLC0415

        if not settings.storage_configured:
            raise StorageError(
                "S3 storage is selected but credentials are incomplete; set S3_BUCKET, "
                "S3_ACCESS_KEY_ID and S3_SECRET_ACCESS_KEY"
            )
        self.bucket = settings.s3_bucket
        self.client = boto3.client(
            "s3",
            endpoint_url=settings.s3_endpoint_url,
            region_name=settings.s3_region,
            aws_access_key_id=settings.s3_access_key_id,
            aws_secret_access_key=settings.s3_secret_access_key,
            config=Config(
                signature_version="s3v4",
                s3={"addressing_style": "path" if settings.s3_force_path_style else "virtual"},
                retries={"max_attempts": 3, "mode": "standard"},
            ),
        )

    def put_stream(self, *, key: str, chunks, max_bytes: int) -> StoredObject:  # noqa: ANN001
        # Buffer to a spooled temp file so we can enforce the size cap and compute the checksum
        # before starting a (potentially long) multipart upload.
        import tempfile  # noqa: PLC0415 - keeps the module import light

        digest = hashlib.sha256()
        total = 0
        with tempfile.SpooledTemporaryFile(max_size=16 * 1024 * 1024) as spool:
            for chunk in chunks:
                if not chunk:
                    continue
                total += len(chunk)
                if total > max_bytes:
                    raise StorageError(f"upload exceeds the {max_bytes // (1024 * 1024)} MiB limit")
                digest.update(chunk)
                spool.write(chunk)
            spool.seek(0)
            self.client.upload_fileobj(spool, self.bucket, key)
        return StoredObject(key=key, bytes=total, sha256=digest.hexdigest())

    def open(self, key: str):  # noqa: ANN201
        return self.client.get_object(Bucket=self.bucket, Key=key)["Body"]

    def delete(self, key: str) -> None:
        self.client.delete_object(Bucket=self.bucket, Key=key)

    def exists(self, key: str) -> bool:
        from botocore.exceptions import ClientError  # noqa: PLC0415

        try:
            self.client.head_object(Bucket=self.bucket, Key=key)
            return True
        except ClientError:
            return False

    def size(self, key: str) -> int:
        return int(self.client.head_object(Bucket=self.bucket, Key=key)["ContentLength"])

    def signed_url(self, key: str, *, expires_in: int, download_name: str | None = None) -> str:
        params: dict[str, str] = {"Bucket": self.bucket, "Key": key}  # type: ignore[dict-item]
        if download_name:
            params["ResponseContentDisposition"] = f'attachment; filename="{download_name}"'
        return self.client.generate_presigned_url("get_object", Params=params, ExpiresIn=expires_in)


def get_storage(settings: Settings) -> Storage:
    if settings.storage_backend == "s3":
        return S3Storage(settings)
    return LocalStorage(settings.local_storage_root)
