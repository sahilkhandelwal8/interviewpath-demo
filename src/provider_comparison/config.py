from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from dotenv import load_dotenv
from pydantic import BaseModel, Field

from .models import Prospect


ROOT = Path(__file__).resolve().parents[2]
CONFIG_DIR = ROOT / "comparison" / "config"


class Settings(BaseModel):
    schema_version: str
    results_per_search: int = Field(ge=1, le=10)
    max_search_calls: int = Field(ge=4, le=6)
    max_searches_per_area: int = Field(ge=1, le=2)
    max_pages: int = Field(ge=1, le=8)
    max_chars_per_page: int = Field(ge=1000)
    draft_timeout_seconds: int = Field(gt=0)
    request_timeout_seconds: int = Field(gt=0)
    repetitions: int = Field(ge=1, le=3)
    gemini_model: str
    draft_temperature: float = Field(ge=0, le=2)
    firecrawl_base_url: str
    exa_base_url: str


def read_json(path: Path) -> Any:
    with path.open("r", encoding="utf-8") as handle:
        return json.load(handle)


def write_json(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    if isinstance(value, BaseModel):
        payload = value.model_dump(mode="json")
    else:
        payload = value
    with path.open("w", encoding="utf-8") as handle:
        json.dump(payload, handle, indent=2, ensure_ascii=False)
        handle.write("\n")


def load_project_env() -> None:
    load_dotenv(ROOT / ".env", override=False)


def load_settings(path: Path | None = None) -> Settings:
    return Settings.model_validate(read_json(path or CONFIG_DIR / "settings.json"))


def load_prospects(path: Path | None = None) -> list[Prospect]:
    return [Prospect.model_validate(item) for item in read_json(path or CONFIG_DIR / "prospects.json")]
