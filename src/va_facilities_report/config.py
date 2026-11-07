"""Runtime configuration, read from environment variables.

The API key comes from one of two places:
- VA_API_KEY: the key itself (local development, from the gitignored .env)
- VA_API_KEY_SSM_PARAMETER: the name of an SSM Parameter Store parameter
  holding the key, fetched (decrypted) at startup
"""

import os
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field

# Value Terraform puts in the SSM parameter before the real key is set.
SSM_PLACEHOLDER = "set-me-with-aws-ssm-put-parameter"


class ConfigError(RuntimeError):
    """Raised when required configuration is missing."""


@dataclass(frozen=True)
class Settings:
    va_api_base_url: str
    # repr=False keeps the key out of logs, tracebacks and debug prints.
    va_api_key: str = field(repr=False)


def _require(env: Mapping[str, str], name: str) -> str:
    value = env.get(name, "").strip()
    if not value:
        raise ConfigError(
            f"Missing required environment variable {name}. "
            "Copy .env.example to .env and fill it in."
        )
    return value


def load_settings(
    env: Mapping[str, str] | None = None,
    fetch_parameter: Callable[[str], str] | None = None,
) -> Settings:
    """Build Settings from the environment (or a given mapping, for tests).

    `fetch_parameter` reads an SSM parameter; tests pass a fake.
    """
    env = os.environ if env is None else env
    return Settings(
        va_api_base_url=_require(env, "VA_API_BASE_URL").rstrip("/"),
        va_api_key=_api_key(env, fetch_parameter or _fetch_ssm_parameter),
    )


def _api_key(env: Mapping[str, str], fetch_parameter: Callable[[str], str]) -> str:
    # 1. A key given directly wins (local development).
    key = env.get("VA_API_KEY", "").strip()
    if key:
        return key

    # 2. Otherwise read it from SSM Parameter Store.
    parameter = env.get("VA_API_KEY_SSM_PARAMETER", "").strip()
    if not parameter:
        raise ConfigError(
            "No API key configured: set VA_API_KEY, or VA_API_KEY_SSM_PARAMETER "
            "to the SSM parameter that holds it. See .env.example."
        )
    key = fetch_parameter(parameter).strip()

    # 3. Catch a parameter that Terraform created but nobody filled in.
    if not key or key == SSM_PLACEHOLDER:
        raise ConfigError(
            f"SSM parameter {parameter} holds no API key yet; set it with "
            "`aws ssm put-parameter --overwrite` (see README)."
        )
    return key


def _fetch_ssm_parameter(name: str) -> str:
    # Imported here so local runs with VA_API_KEY never load boto3.
    import boto3

    response = boto3.client("ssm").get_parameter(Name=name, WithDecryption=True)
    return response["Parameter"]["Value"]


def load_data_dir(env: Mapping[str, str] | None = None) -> str:
    """Storage location from DATA_DIR: a local folder (default: data) or
    s3://bucket[/prefix]. Separate from the API settings so commands that
    never call the API need no API key."""
    env = os.environ if env is None else env
    return env.get("DATA_DIR", "").strip() or "data"
