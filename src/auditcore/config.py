"""Runtime configuration, read from environment variables.

Everything defaults to a local, single-machine setup. Nothing here points at
employer infrastructure; see README "Operating rules".
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path


def _env(name: str, default: str | None = None) -> str | None:
    value = os.environ.get(name)
    return value if value not in (None, "") else default


@dataclass(frozen=True)
class Settings:
    database_url: str = field(
        default_factory=lambda: _env("AUDITCORE_DATABASE_URL", "postgresql+psycopg://localhost/auditcore")
    )
    archive_dir: Path = field(default_factory=lambda: Path(_env("AUDITCORE_ARCHIVE_DIR", "data/archive")))

    # FDA Data Dashboard API (free key: https://datadashboard.fda.gov/ora/api/index.htm)
    dashboard_user: str | None = field(default_factory=lambda: _env("FDA_DASHBOARD_USER"))
    dashboard_key: str | None = field(default_factory=lambda: _env("FDA_DASHBOARD_KEY"))
    dashboard_base_url: str = field(
        default_factory=lambda: _env("FDA_DASHBOARD_BASE_URL", "https://api-datadashboard.fda.gov/v1")
    )

    fda_base_url: str = field(default_factory=lambda: _env("FDA_BASE_URL", "https://www.fda.gov"))
    user_agent: str = field(
        default_factory=lambda: _env("AUDITCORE_USER_AGENT", "auditcore/0.1 (independent research; contact via site)")
    )
    request_delay_s: float = field(default_factory=lambda: float(_env("AUDITCORE_REQUEST_DELAY_S", "1.0")))

    llm_model: str = field(default_factory=lambda: _env("AUDITCORE_LLM_MODEL", "claude-opus-5-5"))
    llm_effort: str = field(default_factory=lambda: _env("AUDITCORE_LLM_EFFORT", "medium"))

    # Trend views hide any failure mode whose gold-set precision is below this bar (PRD section 7).
    precision_bar: float = field(default_factory=lambda: float(_env("AUDITCORE_PRECISION_BAR", "0.85")))


def get_settings() -> Settings:
    return Settings()
