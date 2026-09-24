"""Environment configuration for Patchline.

Every value is sourced from the environment. The vendor allowlist is re-read
from the environment on EVERY authenticated request (FR-052) — call
``allowlisted_login()``; never cache it at startup.
"""
from __future__ import annotations

import os
from dataclasses import dataclass, field
from typing import Optional

TRUE_VALUES = {"1", "true", "yes", "on"}


def _env_bool(name: str, default: bool = False) -> bool:
    raw = os.environ.get(name)
    if raw is None or raw == "":
        return default
    return raw.strip().lower() in TRUE_VALUES


def _env_int(name: str, default: Optional[int] = None) -> Optional[int]:
    raw = os.environ.get(name)
    if raw is None or raw.strip() == "":
        return default
    try:
        return int(raw.strip())
    except ValueError:
        return default


def demo_mode() -> bool:
    """FR-060: DEMO_MODE env var, read live (tests flip it per-case)."""
    return _env_bool("DEMO_MODE", False)


def engine_mode() -> str:
    return (os.environ.get("ENGINE_MODE") or "mock").strip().lower()


def allowlisted_login() -> Optional[str]:
    """FR-052: sole source of truth, re-read per request.

    Returns None when unset or empty string — both mean "misconfigured".
    """
    raw = os.environ.get("GITHUB_ALLOWLISTED_LOGIN")
    if raw is None or raw.strip() == "":
        return None
    return raw.strip()


def database_url() -> str:
    return os.environ.get("DATABASE_URL") or "sqlite:///./patchline.db"


def default_language_env() -> str:
    """The env var seeds the Config table on first startup only (CP-6/FR-026)."""
    return (os.environ.get("DEFAULT_LANGUAGE") or "python").strip().lower()


def session_secret() -> str:
    return os.environ.get("SESSION_SECRET") or ""


def notification_webhook_url() -> Optional[str]:
    raw = os.environ.get("NOTIFICATION_WEBHOOK_URL")
    return raw.strip() if raw and raw.strip() else None


def notification_signing_secret() -> Optional[str]:
    raw = os.environ.get("NOTIFICATION_SIGNING_SECRET")
    return raw.strip() if raw and raw.strip() else None


def notification_batch_window_seconds() -> int:
    return _env_int("NOTIFICATION_BATCH_WINDOW_SECONDS", 0) or 0


def instance_id_env() -> Optional[str]:
    raw = os.environ.get("INSTANCE_ID")
    return raw.strip() if raw and raw.strip() else None


def rescan_interval_seconds() -> int:
    """FR-063: RESCAN_INTERVAL_SECONDS (test-only) overrides the hourly value."""
    test_override = _env_int("RESCAN_INTERVAL_SECONDS", None)
    if test_override is not None and test_override > 0:
        return test_override
    hours = _env_int("RESCAN_INTERVAL_HOURS", 168) or 168
    return hours * 3600


def step_up_age_seconds() -> int:
    """FR-050: default 900 (15 minutes); SESSION_STEP_UP_AGE_SECONDS is test-only."""
    return _env_int("SESSION_STEP_UP_AGE_SECONDS", 900) or 900


def allow_destructive_migrations() -> bool:
    return _env_bool("ALLOW_DESTRUCTIVE_MIGRATIONS", False)


def max_repos_per_installation() -> int:
    return _env_int("MAX_REPOS_PER_INSTALLATION", 20) or 20


def worker_heartbeat_path() -> str:
    return os.environ.get("WORKER_HEARTBEAT_PATH") or "/data/.worker_heartbeat"


def sneferu_base_url() -> Optional[str]:
    raw = os.environ.get("SNEFERU_BASE_URL")
    return raw.strip() if raw and raw.strip() else None


def sneferu_service_token() -> Optional[str]:
    raw = os.environ.get("SNEFERU_SERVICE_TOKEN")
    return raw.strip() if raw and raw.strip() else None


@dataclass
class Settings:
    """Snapshot of non-secret operational configuration.

    Secrets (tokens, keys) are deliberately NOT fields here; they are read
    from the environment at the point of use. The allowlist is also excluded
    on purpose (FR-052 per-request re-read).
    """

    engine_mode: str = field(default_factory=engine_mode)
    database_url: str = field(default_factory=database_url)
    demo_mode: bool = field(default_factory=demo_mode)
    default_language: str = field(default_factory=default_language_env)
    github_app_id: Optional[str] = field(default_factory=lambda: os.environ.get("GITHUB_APP_ID"))
    github_app_slug: Optional[str] = field(default_factory=lambda: os.environ.get("GITHUB_APP_SLUG"))
    github_client_id: Optional[str] = field(default_factory=lambda: os.environ.get("GITHUB_CLIENT_ID"))
    workflow_derive: str = field(default_factory=lambda: os.environ.get("ENGINE_WORKFLOW_DERIVE") or "derive_patterns")
    workflow_scan: str = field(default_factory=lambda: os.environ.get("ENGINE_WORKFLOW_SCAN") or "scan_repository")
    workflow_remediate: str = field(default_factory=lambda: os.environ.get("ENGINE_WORKFLOW_REMEDIATE") or "generate_remediation")
    worker_heartbeat_path: str = field(default_factory=worker_heartbeat_path)

    @classmethod
    def from_env(cls) -> "Settings":
        return cls()


def github_app_private_key_pem() -> Optional[str]:
    """FR-056: file path takes precedence; env content has smart newline handling.

    Env content with real newlines is used as-is; content with literal ``\\n``
    sequences has them converted — only when needed.
    """
    path = os.environ.get("GITHUB_APP_PRIVATE_KEY_PATH")
    if path and path.strip():
        try:
            with open(path.strip(), "r", encoding="utf-8") as fh:
                return fh.read()
        except OSError:
            return None
    raw = os.environ.get("GITHUB_APP_PRIVATE_KEY")
    if not raw:
        return None
    if "\n" in raw:
        return raw
    if "\\n" in raw:
        return raw.replace("\\n", "\n")
    return raw
