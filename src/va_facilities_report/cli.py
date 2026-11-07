"""Pipeline command line: `va-facilities <command>`.

commands:
  run                       Daily job: download today's data from the API
                            (raw/), then build the processed tables
                            (processed/). Takes ~25 min, limited by the API
                            rate limit. Re-running the same day resumes an
                            interrupted download, or makes no API calls if the
                            download is complete and only rebuilds the tables.
  process [--date DATE]     Rebuild the processed tables for a saved day from
                            its raw files, e.g. after changing processing
                            logic. No API calls, takes seconds. DATE is
                            YYYY-MM-DD (default: today, UTC).

Only health facilities are processed (wait times exist only there).
Days are UTC. `run` always works on today, because the API only returns
current data.

configuration (environment variables, see .env.example):
  VA_API_BASE_URL               needed by `run`
  VA_API_KEY                    API key, needed by `run`; or instead
  VA_API_KEY_SSM_PARAMETER      SSM parameter name to read the key from
  DATA_DIR                      local folder (default: data) or
                                s3://bucket[/prefix]
  AWS_PROFILE                   AWS credentials, when S3 or SSM is used

examples:
  uv run --env-file .env va-facilities run
  uv run --env-file .env va-facilities process --date 2026-11-06
"""

import argparse
import logging
import sys
from datetime import UTC, date, datetime

from va_facilities_report.api import VAFacilitiesClient
from va_facilities_report.config import load_data_dir, load_settings
from va_facilities_report.ingestion.extract import run_extraction
from va_facilities_report.logging_config import configure_logging
from va_facilities_report.processing.pipeline import run_processing
from va_facilities_report.storage import Storage, open_storage

log = logging.getLogger("va_facilities_report.cli")

FACILITY_TYPE = "health"


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="va-facilities",
        description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    commands = parser.add_subparsers(dest="command", required=True)
    commands.add_parser("run", help="Download today's data, then process it.")
    process = commands.add_parser("process", help="Rebuild a saved day's tables.")
    process.add_argument(
        "--date",
        type=date.fromisoformat,
        help="Day to rebuild, YYYY-MM-DD (default: today, UTC).",
    )
    return parser


def run(storage: Storage, ingestion_date: date) -> None:
    settings = load_settings()
    with VAFacilitiesClient(settings.va_api_base_url, settings.va_api_key) as client:
        run_extraction(
            client,
            storage,
            facility_type=FACILITY_TYPE,
            ingestion_date=ingestion_date,
            api_base_url=settings.va_api_base_url,
        )
    process(storage, ingestion_date)


def process(storage: Storage, ingestion_date: date) -> None:
    run_processing(storage, facility_type=FACILITY_TYPE, ingestion_date=ingestion_date)


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    configure_logging()

    # Which day to work on: `run` always today; `process` today unless --date.
    today = datetime.now(UTC).date()
    ingestion_date = getattr(args, "date", None) or today
    storage = open_storage(load_data_dir())
    log.info(
        "command started",
        extra={
            "command": args.command,
            "ingestion_date": ingestion_date,
            "storage": repr(storage),
        },
    )

    # Run the command. Any failure is logged with its traceback and turned
    # into exit code 1, so schedulers and shells can detect it.
    try:
        if args.command == "run":
            run(storage, ingestion_date)
        else:
            process(storage, ingestion_date)
    except Exception:
        log.exception("command failed", extra={"command": args.command})
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
