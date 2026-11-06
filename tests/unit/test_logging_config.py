import json
import logging

from va_facilities_report.logging_config import JsonFormatter


def test_formats_message_and_extra_fields_as_json() -> None:
    record = logging.makeLogRecord(
        {
            "name": "x",
            "levelname": "INFO",
            "msg": "page %s saved",
            "args": (2,),
            "page": 2,
            "records": 1000,
        }
    )

    payload = json.loads(JsonFormatter().format(record))

    assert payload["message"] == "page 2 saved"
    assert payload["level"] == "INFO"
    assert payload["page"] == 2
    assert payload["records"] == 1000
    assert "args" not in payload
