import pytest

from va_facilities_report.config import (
    SSM_PLACEHOLDER,
    ConfigError,
    load_data_dir,
    load_settings,
)

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
    assert load_data_dir({}) == "data"
    assert load_data_dir({"DATA_DIR": "  "}) == "data"


def test_data_dir_from_environment() -> None:
    assert load_data_dir({"DATA_DIR": "C:/va-data"}) == "C:/va-data"
    assert load_data_dir({"DATA_DIR": "s3://bucket"}) == "s3://bucket"


SSM_ENV = {
    "VA_API_BASE_URL": "https://example.test/v1",
    "VA_API_KEY_SSM_PARAMETER": "/va-facilities-report/sandbox/api-key",
}


def test_api_key_from_ssm() -> None:
    requested = []

    def fetch(name: str) -> str:
        requested.append(name)
        return "ssm-key\n"

    settings = load_settings(SSM_ENV, fetch_parameter=fetch)

    assert settings.va_api_key == "ssm-key"
    assert requested == ["/va-facilities-report/sandbox/api-key"]


def test_direct_api_key_wins_over_ssm() -> None:
    def fetch(name: str) -> str:
        raise AssertionError("SSM must not be called")

    env = {**SSM_ENV, "VA_API_KEY": "direct-key"}

    assert load_settings(env, fetch_parameter=fetch).va_api_key == "direct-key"


def test_neither_key_source_raises() -> None:
    with pytest.raises(ConfigError, match="VA_API_KEY_SSM_PARAMETER"):
        load_settings({"VA_API_BASE_URL": "https://example.test/v1"})


@pytest.mark.parametrize("value", ["", "  ", SSM_PLACEHOLDER])
def test_unset_ssm_parameter_raises(value: str) -> None:
    with pytest.raises(ConfigError, match="put-parameter"):
        load_settings(SSM_ENV, fetch_parameter=lambda name: value)
