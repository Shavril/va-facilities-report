from datetime import UTC, date, datetime

import pytest

from va_facilities_report import cli

TODAY = datetime.now(UTC).date()


@pytest.fixture
def calls(monkeypatch, tmp_path):
    monkeypatch.setenv("DATA_DIR", str(tmp_path))
    recorded = []
    monkeypatch.setattr(cli, "run", lambda storage, day: recorded.append(("run", day)))
    monkeypatch.setattr(
        cli, "process", lambda storage, day: recorded.append(("process", day))
    )
    return recorded


def test_run_uses_today(calls) -> None:
    assert cli.main(["run"]) == 0
    assert calls == [("run", TODAY)]


def test_process_defaults_to_today(calls) -> None:
    assert cli.main(["process"]) == 0
    assert calls == [("process", TODAY)]


def test_process_accepts_a_date(calls) -> None:
    assert cli.main(["process", "--date", "2026-11-01"]) == 0
    assert calls == [("process", date(2026, 11, 1))]


def test_run_does_not_accept_a_date() -> None:
    with pytest.raises(SystemExit):
        cli.build_parser().parse_args(["run", "--date", "2026-11-01"])


@pytest.mark.parametrize("removed", ["extract"])
def test_removed_commands_are_rejected(removed) -> None:
    with pytest.raises(SystemExit):
        cli.build_parser().parse_args([removed])


def test_failure_returns_exit_code_1(monkeypatch, tmp_path) -> None:
    monkeypatch.setenv("DATA_DIR", str(tmp_path))

    def failing_run(storage, day):
        raise RuntimeError("API down")

    monkeypatch.setattr(cli, "run", failing_run)

    assert cli.main(["run"]) == 1


def test_run_extracts_then_processes(monkeypatch, tmp_path) -> None:
    order = []
    monkeypatch.setenv("VA_API_KEY", "k")
    monkeypatch.setenv("VA_API_BASE_URL", "https://example.test/v1")
    monkeypatch.setattr(cli, "run_extraction", lambda *a, **kw: order.append("extract"))
    monkeypatch.setattr(cli, "run_processing", lambda *a, **kw: order.append("process"))

    cli.run(storage=None, ingestion_date=TODAY)

    assert order == ["extract", "process"]


def test_run_skips_processing_when_extraction_fails(monkeypatch) -> None:
    order = []
    monkeypatch.setenv("VA_API_KEY", "k")
    monkeypatch.setenv("VA_API_BASE_URL", "https://example.test/v1")

    def failing_extraction(*args, **kwargs):
        raise RuntimeError("API down")

    monkeypatch.setattr(cli, "run_extraction", failing_extraction)
    monkeypatch.setattr(cli, "run_processing", lambda *a, **kw: order.append("process"))

    with pytest.raises(RuntimeError):
        cli.run(storage=None, ingestion_date=TODAY)
    assert order == []
