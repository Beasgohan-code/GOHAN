"""Environment-driven configuration."""

from __future__ import annotations

import os
from dataclasses import dataclass

from dotenv import load_dotenv

load_dotenv()


def _int_set(raw: str) -> set[int]:
    out: set[int] = set()
    for part in raw.split(","):
        part = part.strip()
        if part.lstrip("-").isdigit():
            out.add(int(part))
    return out


@dataclass(slots=True)
class Config:
    bot_token: str
    owner_ids: set[int]
    api_id: int | None
    api_hash: str | None
    session_string: str | None
    tz: str
    database: str
    effect_id: str | None

    @property
    def mtproto_configured(self) -> bool:
        return bool(self.api_id and self.api_hash)


def load_config() -> Config:
    api_id_raw = os.getenv("API_ID", "").strip()
    return Config(
        bot_token=os.getenv("BOT_TOKEN", "").strip(),
        owner_ids=_int_set(os.getenv("OWNER_IDS", "")),
        api_id=int(api_id_raw) if api_id_raw.isdigit() else None,
        api_hash=os.getenv("API_HASH", "").strip() or None,
        session_string=os.getenv("SESSION_STRING", "").strip() or None,
        tz=os.getenv("TZ", "").strip() or "Asia/Kolkata",
        database=os.getenv("DATABASE", "").strip() or "gohan.db",
        effect_id=os.getenv("EFFECT_ID", "").strip() or None,
    )


cfg = load_config()
