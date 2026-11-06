"""Runtime configuration, read from environment variables.

The code never knows where values come from: locally they are loaded from
a gitignored .env file, on AWS they are injected from SSM Parameter Store.
"""

import os
from collections.abc import Mapping
from dataclasses import dataclass, field
from pathlib import Path


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


def load_settings(env: Mapping[str, str] | None = None) -> Settings:
    """Build Settings from the environment (or a given mapping, for tests)."""
    env = os.environ if env is None else env
    return Settings(
        va_api_base_url=_require(env, "VA_API_BASE_URL").rstrip("/"),
        va_api_key=_require(env, "VA_API_KEY"),
    )


def load_data_dir(env: Mapping[str, str] | None = None) -> Path:
    """Storage root from DATA_DIR (default: data/). Separate from the API
    settings so commands that never call the API need no API key."""
    env = os.environ if env is None else env
    return Path(env.get("DATA_DIR", "").strip() or "data")
