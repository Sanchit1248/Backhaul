"""Loads config/service.yaml and .env. One place for every tunable."""
from __future__ import annotations

import os
from functools import lru_cache
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parents[2]
CONFIG_PATH = Path(os.environ.get("SERVICE_CONFIG", ROOT / "config" / "service.yaml"))
DATA_DIR = Path(os.environ.get("SERVICE_DATA_DIR", ROOT / "data"))

try:
    from dotenv import load_dotenv

    load_dotenv(ROOT / ".env")
except ImportError:  # dotenv is optional
    pass


@lru_cache
def settings() -> dict:
    with open(CONFIG_PATH, encoding="utf-8") as f:
        return yaml.safe_load(f)


def env(name: str, default: str | None = None) -> str | None:
    return os.environ.get(name, default)


def demo_mode() -> bool:
    """BACKHAUL_DEMO=1 (the Vercel demo): test data only, jobs run inside the request,
    data lives in a temporary folder and resets."""
    return os.environ.get("BACKHAUL_DEMO", "").lower() in ("1", "true", "yes")


def usd_to_inr(usd: float) -> float:
    return usd * float(settings()["costs"]["usd_to_inr"])
