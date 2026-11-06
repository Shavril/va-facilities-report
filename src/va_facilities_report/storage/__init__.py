"""Object storage for raw and processed data.

Keys are S3-style paths ("raw/facilities/facility_type=health/..."). The
local implementation mirrors that layout on disk; an S3 implementation with
the same three methods will be added in Phase 3.
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


class LocalStorage:
    def __init__(self, root: Path) -> None:
        self.root = root

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
