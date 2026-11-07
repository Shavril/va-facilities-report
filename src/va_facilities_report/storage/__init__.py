"""Object storage for raw and processed data.

Keys are S3-style paths ("raw/facilities/facility_type=health/..."). Two
implementations share the same three methods: LocalStorage mirrors the
layout on disk, S3Storage (storage/s3.py) writes it to a bucket.
`open_storage` picks one from a location string such as DATA_DIR.
"""

import os
import tempfile
from dataclasses import dataclass
from datetime import date
from pathlib import Path
from typing import Protocol


@dataclass(frozen=True)
class Partition:
    facility_type: str
    ingestion_date: date

    def key(self, dataset: str, filename: str, zone: str = "raw") -> str:
        return (
            f"{zone}/{dataset}/facility_type={self.facility_type}/"
            f"ingestion_date={self.ingestion_date.isoformat()}/{filename}"
        )


class Storage(Protocol):
    def exists(self, key: str) -> bool: ...
    def read_bytes(self, key: str) -> bytes: ...
    def write_bytes(self, key: str, data: bytes) -> None: ...


def open_storage(location: str) -> Storage:
    """`s3://bucket[/prefix]` -> S3Storage, anything else -> local folder."""
    if location.startswith("s3://"):
        # Imported here so local runs (and tests) never load boto3.
        from va_facilities_report.storage.s3 import S3Storage

        bucket, _, prefix = location.removeprefix("s3://").partition("/")
        if not bucket:
            raise ValueError(f"no bucket in storage location: {location}")
        return S3Storage(bucket, prefix)
    return LocalStorage(Path(location))


class LocalStorage:
    def __init__(self, root: Path) -> None:
        self.root = root

    def __repr__(self) -> str:
        return f"LocalStorage({self.root})"

    def _path(self, key: str) -> Path:
        # Refuse keys like "../x" that would point outside the storage root.
        path = (self.root / key).resolve()
        if not path.is_relative_to(self.root.resolve()):
            raise ValueError(f"key escapes storage root: {key}")
        return path

    def exists(self, key: str) -> bool:
        return self._path(key).is_file()

    def read_bytes(self, key: str) -> bytes:
        return self._path(key).read_bytes()

    def write_bytes(self, key: str, data: bytes) -> None:
        """Write atomically: a crash never leaves a half-written file behind,
        so `exists()` is a reliable "this step is done" check for resuming."""
        path = self._path(key)
        path.parent.mkdir(parents=True, exist_ok=True)
        # 1. Write to a hidden temp file in the same folder...
        fd, tmp = tempfile.mkstemp(dir=path.parent, prefix=f".{path.name}.")
        try:
            with os.fdopen(fd, "wb") as file:
                file.write(data)
            # 2. ...then rename it into place. A rename within one folder is
            #    atomic: readers see the old file or the new one, never half.
            os.replace(tmp, path)
        except BaseException:
            # Interrupted (error, Ctrl+C): remove the temp file, keep the error.
            Path(tmp).unlink(missing_ok=True)
            raise
