"""Typed configuration.

Everything is read from the environment (and an optional ``.env`` file) into a
single :class:`Settings` object. Nothing here performs I/O beyond reading the
dotenv file and inspecting the environment, so it is safe to import from tests.

Design notes
------------
* ``SecretStr`` is used for the bot token / api_hash so they never leak into
  logs or tracebacks by accident.
* ``--check`` (see :mod:`gohan.__main__`) reports problems without raising, so a
  half-configured checkout still gives useful output.
* The keep-alive URL is *auto-detected* from the usual PaaS environment
  variables (Render, Koyeb, Railway, Heroku, Fly, Replit) - same idea as
  Videl1's ``keep_alive.py``.
"""

from __future__ import annotations

import os
from functools import lru_cache
from pathlib import Path
from typing import Literal

from pydantic import Field, SecretStr, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

MtprotoMode = Literal["auto", "user", "bot", "off"]
LlmProvider = Literal["off", "openai", "openai_compatible"]
RichMode = Literal["auto", "html", "blocks"]

#: PaaS environment variable -> how to build a public URL out of it.
_PLATFORM_URL_VARS: tuple[tuple[str, str, str], ...] = (
    # env var, platform label, URL template ({v} = the variable's value)
    ("RENDER_EXTERNAL_URL", "Render", "{v}"),
    ("KOYEB_PUBLIC_DOMAIN", "Koyeb", "https://{v}"),
    ("RAILWAY_PUBLIC_DOMAIN", "Railway", "https://{v}"),
    ("FLY_APP_NAME", "Fly.io", "https://{v}.fly.dev"),
    ("REPLIT_DEV_DOMAIN", "Replit", "https://{v}"),
    ("HEROKU_APP_NAME", "Heroku", "https://{v}.herokuapp.com"),
)


def detect_host() -> str:
    """Best-effort name of the platform we are running on (for log reports)."""
    for env, name, _ in _PLATFORM_URL_VARS:
        if os.environ.get(env):
            return name
    if os.environ.get("DYNO"):
        return "Heroku"
    if os.environ.get("K_SERVICE"):
        return "Cloud Run"
    if os.environ.get("RENDER"):
        return "Render"
    if os.path.exists("/.dockerenv"):
        return "Docker"
    return "VPS"


def detect_public_url() -> str:
    """Guess this instance's public base URL from the environment (may be '')."""
    for env, _name, template in _PLATFORM_URL_VARS:
        value = (os.environ.get(env) or "").strip()
        if value:
            return template.format(v=value).rstrip("/")
    return ""


class Settings(BaseSettings):
    """Runtime configuration for the bot."""

    model_config = SettingsConfigDict(
        # GOHAN_ENV_FILE lets `python -m gohan --env other.env` point elsewhere
        env_file=os.environ.get("GOHAN_ENV_FILE", ".env"),
        env_file_encoding="utf-8",
        extra="ignore",
        case_sensitive=False,
    )

    # --- identity -----------------------------------------------------------
    bot_name: str = "GOHAN"

    # --- Telegram Bot API ---------------------------------------------------
    bot_token: SecretStr | None = None

    # --- Kurigram / MTProto -------------------------------------------------
    api_id: int | None = None
    api_hash: SecretStr | None = None
    mtproto_mode: MtprotoMode = "auto"
    session_dir: Path = Path("data/sessions")
    user_session_name: str = "gohan_user"
    bot_session_name: str = "gohan_bot"
    phone: str | None = None

    # --- Access -------------------------------------------------------------
    owner_user_ids: str = ""

    # --- Storage ------------------------------------------------------------
    database_path: Path = Path("data/gohan.sqlite3")
    temp_dirs: str = "data/tmp,data/downloads"

    # --- Rich messages ------------------------------------------------------
    rich_default: bool = True
    rich_mode: RichMode = "auto"
    #: Use <tg-emoji> custom emoji in outgoing messages. Requires the bot owner
    #: to have Telegram Premium; turn off to fall back to plain emoji.
    custom_emoji: bool = True
    draft_interval: float = Field(default=0.7, ge=0.1)
    max_rich_media: int = Field(default=50, gt=0)
    max_rich_blocks: int = Field(default=500, gt=0)

    # --- LLM ----------------------------------------------------------------
    llm_provider: LlmProvider = "off"
    llm_api_key: SecretStr | None = None
    llm_base_url: str = "https://api.openai.com/v1"
    llm_model: str = "gpt-4o-mini"

    # --- Runtime --------------------------------------------------------------
    # Skip updates that piled up while the bot was offline (a restart should not
    # replay yesterday's moderation queue)
    drop_pending_updates: bool = True

    # --- Behaviour ----------------------------------------------------------
    throttle_delay: float = Field(default=0.6, ge=0)
    max_download_mb: int = Field(default=2000, gt=0)

    # --- Keep-alive web server (free hosts sleep without it) ----------------
    keep_alive: bool = True
    port: int = Field(default=8080, ge=1, le=65535)
    keep_alive_url: str = ""  # auto-detected when empty
    keep_alive_interval: int = Field(default=240, ge=30)

    # --- Watchdog -----------------------------------------------------------
    watchdog: bool = True
    watchdog_interval: int = Field(default=600, ge=30)
    cleanup_after_hours: float = Field(default=6.0, ge=0)
    min_free_disk_gb: float = Field(default=2.0, ge=0)
    state_timeout_min: int = Field(default=15, gt=0)
    auto_restart_on_hang: bool = True

    # --- Log channel --------------------------------------------------------
    log_channel_id: int = 0
    # Events that also land in the owners' DMs (comma separated tags).
    owner_dm_events: str = "Error,LowDisk,AutoRestart,BotStopped,WatchdogRestart"
    log_start_events: bool = True
    start_log_cooldown_min: int = Field(default=30, ge=0)
    daily_report: bool = True
    daily_report_hour: int = Field(default=0, ge=0, le=23)
    log_timezone: str = "Asia/Kolkata"

    # --- Logging ------------------------------------------------------------
    log_level: str = "INFO"
    log_json: bool = False

    # -- validators ----------------------------------------------------------

    @field_validator("log_level")
    @classmethod
    def _upper_level(cls, v: str) -> str:
        v = v.strip().upper()
        allowed = {"CRITICAL", "ERROR", "WARNING", "INFO", "DEBUG", "NOTSET"}
        if v not in allowed:
            raise ValueError(f"LOG_LEVEL must be one of {sorted(allowed)}")
        return v

    @field_validator("llm_base_url")
    @classmethod
    def _strip_url(cls, v: str) -> str:
        return v.rstrip("/")

    @field_validator("log_timezone")
    @classmethod
    def _known_tz(cls, v: str) -> str:
        from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

        try:
            ZoneInfo(v)
        except (ZoneInfoNotFoundError, ValueError):
            return "UTC"
        return v

    # -- derived values ------------------------------------------------------

    @property
    def owner_ids(self) -> frozenset[int]:
        """Numeric ids parsed out of the comma/space separated ``OWNER_USER_IDS``."""
        raw = self.owner_user_ids.replace(",", " ").split()
        out: set[int] = set()
        for chunk in raw:
            try:
                out.add(int(chunk))
            except ValueError:
                continue
        return frozenset(out)

    @property
    def owner_dm_event_tags(self) -> frozenset[str]:
        return frozenset(
            part.strip().lstrip("#")
            for part in self.owner_dm_events.replace(" ", ",").split(",")
            if part.strip()
        )

    @property
    def temp_dir_list(self) -> list[Path]:
        return [Path(p.strip()) for p in self.temp_dirs.split(",") if p.strip()]

    @property
    def resolved_keep_alive_url(self) -> str:
        """The self-ping target: explicit setting, else auto-detected, else ''."""
        explicit = self.keep_alive_url.strip().rstrip("/")
        if explicit:
            return explicit if explicit.startswith("http") else f"https://{explicit}"
        return detect_public_url()

    @property
    def tzinfo(self):
        from zoneinfo import ZoneInfo

        return ZoneInfo(self.log_timezone)

    @property
    def user_session_file(self) -> Path:
        return self.session_dir / f"{self.user_session_name}.session"

    @property
    def bot_session_file(self) -> Path:
        return self.session_dir / f"{self.bot_session_name}.session"

    @property
    def restart_reason_file(self) -> Path:
        return Path("data/.restart_reason")

    @property
    def has_api_credentials(self) -> bool:
        return bool(self.api_id and self.api_hash)

    @property
    def mtproto_wanted(self) -> bool:
        return self.mtproto_mode != "off"

    @property
    def mtproto_configured(self) -> bool:
        return self.mtproto_wanted and self.has_api_credentials

    @property
    def llm_enabled(self) -> bool:
        return self.llm_provider != "off" and self.llm_api_key is not None

    # -- helpers -------------------------------------------------------------

    def ensure_dirs(self) -> None:
        """Create the directories the bot writes to."""
        for directory in (self.session_dir, self.database_path.parent, *self.temp_dir_list):
            directory.mkdir(parents=True, exist_ok=True)
        try:
            os.chmod(self.session_dir, 0o700)  # auth keys are secrets
        except OSError:  # pragma: no cover - best effort on odd filesystems
            pass

    def problems(self) -> list[str]:
        """Return human-readable configuration problems (empty list == good)."""
        issues: list[str] = []
        if not self.bot_token or not self.bot_token.get_secret_value().strip():
            issues.append("BOT_TOKEN is not set (get one from @BotFather)")
        elif ":" not in self.bot_token.get_secret_value():
            issues.append("BOT_TOKEN does not look like a bot token (expected '<digits>:<hash>')")

        if self.mtproto_wanted and not self.has_api_credentials:
            issues.append(
                "API_ID/API_HASH are not set - MTProto features will be disabled. "
                "Get them at https://my.telegram.org (or set MTPROTO_MODE=off to silence this)"
            )
        if self.llm_provider != "off" and self.llm_api_key is None:
            issues.append(f"LLM_PROVIDER={self.llm_provider} but LLM_API_KEY is empty")
        if self.log_channel_id and not str(self.log_channel_id).startswith("-100"):
            issues.append(
                f"LOG_CHANNEL_ID={self.log_channel_id} does not look like a channel id "
                "(should be negative, e.g. -1001234567890); owner DMs will be used instead"
            )
        if not self.owner_ids and not self.log_channel_id and self.watchdog:
            issues.append(
                "Neither OWNER_USER_IDS nor LOG_CHANNEL_ID is set - watchdog alerts have "
                "nowhere to go (informational)"
            )
        return issues

    def describe(self) -> str:
        """A redacted one-screen summary, useful for bug reports."""
        token = self.bot_token.get_secret_value() if self.bot_token else ""
        token_hint = (
            f"{token.split(':')[0]}:***" if ":" in token else ("set" if token else "missing")
        )
        lines = [
            f"bot_name         : {self.bot_name}",
            f"bot_token        : {token_hint}",
            f"mtproto_mode     : {self.mtproto_mode}",
            f"api credentials  : {'yes' if self.has_api_credentials else 'no'}",
            f"user session     : {'present' if self.user_session_file.exists() else 'absent'}",
            f"bot session      : {'present' if self.bot_session_file.exists() else 'absent'}",
            f"database         : {self.database_path}",
            f"llm_provider     : {self.llm_provider}"
            + (f" ({self.llm_model})" if self.llm_enabled else ""),
            f"rich_default     : {self.rich_default} (mode={self.rich_mode})",
            f"owners           : {sorted(self.owner_ids) or 'unset'}",
            f"log channel      : {self.log_channel_id or 'unset (owner DMs)'}",
            f"keep-alive       : {'on' if self.keep_alive else 'off'} :{self.port}"
            + (f" -> {self.resolved_keep_alive_url}" if self.resolved_keep_alive_url else ""),
            f"watchdog         : {'every %ss' % self.watchdog_interval if self.watchdog else 'off'}",
            f"host             : {detect_host()}",
            f"log_level        : {self.log_level}",
        ]
        return "\n".join(lines)


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    """Process-wide settings singleton."""
    return Settings()


def reload_settings() -> Settings:
    """Drop the cached settings (used by tests and ``--check``)."""
    get_settings.cache_clear()
    return get_settings()


__all__ = [
    "LlmProvider",
    "MtprotoMode",
    "RichMode",
    "Settings",
    "detect_host",
    "detect_public_url",
    "get_settings",
    "reload_settings",
]
