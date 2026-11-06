from pathlib import Path

import pytest

from va_facilities_report.config import ConfigError, load_data_dir, load_settings

VALID_ENV = {
    "VA_API_KEY": "secret-key",
    "VA_API_BASE_URL": "https://sandbox-api.va.gov/services/va_facilities/v1/",
}


def test_load_settings_reads_values() -> None:
    settings = load_settings(VALID_ENV)

    assert settings.va_api_key == "secret-key"
    assert settings.va_api_base_url == (
        "https://sandbox-api.va.gov/services/va_facilities/v1"
    )


@pytest.mark.parametrize("missing", ["VA_API_KEY", "VA_API_BASE_URL"])
def test_missing_variable_raises(missing: str) -> None:
    env = {k: v for k, v in VALID_ENV.items() if k != missing}

    with pytest.raises(ConfigError, match=missing):
        load_settings(env)


def test_blank_api_key_raises() -> None:
    with pytest.raises(ConfigError, match="VA_API_KEY"):
        load_settings({**VALID_ENV, "VA_API_KEY": "   "})


def test_api_key_not_in_repr() -> None:
    assert "secret-key" not in repr(load_settings(VALID_ENV))


def test_data_dir_defaults_to_data() -> None:
    assert load_data_dir({}) == Path("data")
    assert load_data_dir({"DATA_DIR": "  "}) == Path("data")


def test_data_dir_from_environment() -> None:
    assert load_data_dir({"DATA_DIR": "C:/va-data"}) == Path("C:/va-data")
