import io

import pytest
from botocore.exceptions import ClientError

from va_facilities_report.storage import LocalStorage, open_storage
from va_facilities_report.storage.s3 import S3Storage


class FakeS3:
    """In-memory stand-in for a boto3 S3 client (only the calls we use)."""

    def __init__(self, error_code: str | None = None) -> None:
        self.objects: dict[tuple[str, str], bytes] = {}
        self.error_code = error_code  # make head_object fail with this code

    def _missing(self, operation: str, code: str = "404") -> ClientError:
        return ClientError({"Error": {"Code": code, "Message": "x"}}, operation)

    def head_object(self, *, Bucket: str, Key: str) -> dict:
        if self.error_code:
            raise self._missing("HeadObject", self.error_code)
        if (Bucket, Key) not in self.objects:
            raise self._missing("HeadObject")
        return {}

    def get_object(self, *, Bucket: str, Key: str) -> dict:
        if (Bucket, Key) not in self.objects:
            raise self._missing("GetObject", "NoSuchKey")
        return {"Body": io.BytesIO(self.objects[(Bucket, Key)])}

    def put_object(self, *, Bucket: str, Key: str, Body: bytes) -> dict:
        self.objects[(Bucket, Key)] = Body
        return {}


def test_write_then_read() -> None:
    fake = FakeS3()
    storage = S3Storage("bucket", client=fake)

    storage.write_bytes("raw/a/b=1/file.json", b"{}")

    assert storage.exists("raw/a/b=1/file.json")
    assert storage.read_bytes("raw/a/b=1/file.json") == b"{}"
    # Keys are used as object keys unchanged: same layout as on disk.
    assert list(fake.objects) == [("bucket", "raw/a/b=1/file.json")]


def test_prefix_is_prepended() -> None:
    fake = FakeS3()
    S3Storage("bucket", "/test-run/", client=fake).write_bytes("raw/x.json", b"1")

    assert list(fake.objects) == [("bucket", "test-run/raw/x.json")]


def test_missing_key_does_not_exist() -> None:
    assert not S3Storage("bucket", client=FakeS3()).exists("nope.json")


def test_access_denied_is_not_treated_as_missing() -> None:
    # Treating AccessDenied as "missing" would make resume redo finished work.
    storage = S3Storage("bucket", client=FakeS3(error_code="403"))

    with pytest.raises(ClientError):
        storage.exists("raw/x.json")


def test_read_missing_key_raises() -> None:
    with pytest.raises(ClientError):
        S3Storage("bucket", client=FakeS3()).read_bytes("nope.json")


def test_rejects_parent_directory_keys() -> None:
    with pytest.raises(ValueError):
        S3Storage("bucket", client=FakeS3()).write_bytes("../x.json", b"")


def test_open_storage_local(tmp_path) -> None:
    storage = open_storage(str(tmp_path))

    assert isinstance(storage, LocalStorage)
    assert storage.root == tmp_path


@pytest.mark.parametrize(
    ("location", "bucket", "prefix"),
    [
        ("s3://my-bucket", "my-bucket", ""),
        ("s3://my-bucket/", "my-bucket", ""),
        ("s3://my-bucket/some/prefix", "my-bucket", "some/prefix"),
    ],
)
def test_open_storage_s3(monkeypatch, location, bucket, prefix) -> None:
    # Avoid creating a real boto3 client (needs no credentials, but is slow).
    monkeypatch.setattr("va_facilities_report.storage.s3.boto3.client", lambda _: None)

    storage = open_storage(location)

    assert isinstance(storage, S3Storage)
    assert (storage.bucket, storage.prefix) == (bucket, prefix)


def test_open_storage_rejects_missing_bucket() -> None:
    with pytest.raises(ValueError):
        open_storage("s3://")
