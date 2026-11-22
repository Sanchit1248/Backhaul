"""Loads config/service.toml and .env. One place for every tunable."""
from __future__ import annotations

import os
import tomllib
from functools import lru_cache
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
CONFIG_PATH = Path(os.environ.get("SERVICE_CONFIG", ROOT / "config" / "service.toml"))
DATA_DIR = Path(os.environ.get("SERVICE_DATA_DIR", ROOT / "data"))

try:
    from dotenv import load_dotenv

    load_dotenv(ROOT / ".env")
except ImportError:  # dotenv is optional
    pass


@lru_cache
def settings() -> dict:
    with open(CONFIG_PATH, "rb") as f:
        return tomllib.load(f)


def env(name: str, default: str | None = None) -> str | None:
    return os.environ.get(name, default)
