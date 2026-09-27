"""Load config/pipeline.yaml, then apply environment overrides (.env or real env vars).

Keeping configuration out of the stage code means the same pipeline can move from a
laptop to staging/production by changing config, not code (Class 8).
"""
from __future__ import annotations

import hashlib
import json
import os
from dataclasses import dataclass, field
from pathlib import Path

import yaml

PROJECT_ROOT = Path(__file__).resolve().parent.parent
CONFIG_PATH = PROJECT_ROOT / "config" / "pipeline.yaml"
ENV_PATH = PROJECT_ROOT / ".env"


def _load_dotenv(path: Path) -> None:
    """Minimal .env loader (no extra dependency). Real env vars win over the file."""
    if not path.exists():
        return
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        os.environ.setdefault(key.strip(), value.strip())


def _as_bool(value, default: bool) -> bool:
    if value is None:
        return default
    return str(value).strip().lower() in {"1", "true", "yes", "y", "on"}


@dataclass
class PipelineConfig:
    raw: dict
    project_root: Path = PROJECT_ROOT
    overrides: dict = field(default_factory=dict)

    # ---- convenience accessors -------------------------------------------------
    def source_path(self, key: str) -> Path:
        return self.project_root / self.raw["sources"][key]

    @property
    def dispatch_api_url(self) -> str:
        return self.raw["sources"]["dispatch_api_url"]

    @property
    def api(self) -> dict:
        return self.raw["api"]

    @property
    def kpi(self) -> dict:
        return self.raw["kpi"]

    @property
    def validation(self) -> dict:
        return self.raw["validation"]

    @property
    def required_columns(self) -> dict:
        return self.raw["required_columns"]

    @property
    def data_dir(self) -> Path:
        return self.project_root / self.raw["output"]["data_dir"]

    @property
    def reports_dir(self) -> Path:
        return self.project_root / self.raw["output"]["reports_dir"]

    @property
    def logs_dir(self) -> Path:
        return self.project_root / self.raw["output"]["logs_dir"]

    @property
    def log_level(self) -> str:
        return os.getenv("LOG_LEVEL", "INFO").upper()

    def fingerprint(self) -> str:
        """Hash of the effective config — recorded in the run manifest for traceability."""
        blob = json.dumps(self.raw, sort_keys=True, default=str).encode()
        return hashlib.sha256(blob).hexdigest()[:12]


def load_config(config_path: Path = CONFIG_PATH) -> PipelineConfig:
    _load_dotenv(ENV_PATH)
    raw = yaml.safe_load(config_path.read_text(encoding="utf-8"))

    env_map = {
        "DISPATCH_API_URL": (("sources", "dispatch_api_url"), str),
        "PAGE_SIZE": (("api", "page_size"), int),
        "MAX_RETRIES": (("api", "max_retries"), int),
        "RETRY_BASE_SECONDS": (("api", "retry_base_seconds"), float),
        "MAX_DATA_AGE_DAYS": (("validation", "max_data_age_days"), int),
        "OUTPUT_DIR": (("output", "data_dir"), str),
        "REPORTS_DIR": (("output", "reports_dir"), str),
        "LOGS_DIR": (("output", "logs_dir"), str),
    }
    overrides = {}
    for env_key, ((section, key), cast) in env_map.items():
        if os.getenv(env_key) not in (None, ""):
            raw[section][key] = cast(os.environ[env_key])
            overrides[env_key] = raw[section][key]

    if os.getenv("START_MOCK_API") is not None:
        raw["api"]["start_mock_api"] = _as_bool(os.getenv("START_MOCK_API"), True)
        overrides["START_MOCK_API"] = raw["api"]["start_mock_api"]

    return PipelineConfig(raw=raw, overrides=overrides)
