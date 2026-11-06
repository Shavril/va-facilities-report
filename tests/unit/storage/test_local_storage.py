import pytest

from va_facilities_report.storage import LocalStorage


def test_write_then_read(tmp_path) -> None:
    storage = LocalStorage(tmp_path)

    storage.write_bytes("raw/a/b=1/file.json", b"{}")

    assert storage.exists("raw/a/b=1/file.json")
    assert storage.read_bytes("raw/a/b=1/file.json") == b"{}"
    assert (tmp_path / "raw" / "a" / "b=1" / "file.json").is_file()


def test_overwrite_leaves_no_temp_files(tmp_path) -> None:
    storage = LocalStorage(tmp_path)

    storage.write_bytes("x/file.json", b"1")
    storage.write_bytes("x/file.json", b"2")

    assert storage.read_bytes("x/file.json") == b"2"
    assert [p.name for p in (tmp_path / "x").iterdir()] == ["file.json"]


def test_missing_key_does_not_exist(tmp_path) -> None:
    assert not LocalStorage(tmp_path).exists("nope.json")


def test_rejects_keys_outside_root(tmp_path) -> None:
    with pytest.raises(ValueError):
        LocalStorage(tmp_path / "root").write_bytes("../escape.json", b"")
