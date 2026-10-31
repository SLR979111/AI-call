from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path


def _bool(name: str, default: str = "1") -> bool:
    return os.getenv(name, default).lower() in {"1", "true", "yes", "on"}


@dataclass
class Settings:
    app_env: str = field(default_factory=lambda: os.getenv("APP_ENV", "dev"))
    release_id: str = field(default_factory=lambda: os.getenv("RELEASE_ID", "rel_dev_001"))
    data_dir: Path = field(default_factory=lambda: Path(os.getenv("DATA_DIR", "data")))
    dev_no_auth: bool = field(default_factory=lambda: _bool("DEV_NO_AUTH", "1"))
    clock_id: str = field(default_factory=lambda: os.getenv("CLOCK_ID", "app-dev"))
    scenario_id: str = field(default_factory=lambda: os.getenv("SCENARIO_ID", "service_outbound"))

    def ensure_dirs(self) -> None:
        (self.data_dir / "sessions").mkdir(parents=True, exist_ok=True)
        (self.data_dir / "index").mkdir(parents=True, exist_ok=True)
        (self.data_dir / "evals").mkdir(parents=True, exist_ok=True)


settings = Settings()
settings.ensure_dirs()
