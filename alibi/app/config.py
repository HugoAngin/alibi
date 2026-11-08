"""App configuration from the environment. Nothing here reads or prints a secret value.

Every setting has a demo default so ``uv run --frozen python -m alibi.app`` works from a clean
checkout. Generated files (database, keys, bank-side state) live under ``data/alibi/``, which is
git-ignored.

Variables
---------
ALIBI_CASE          development case number the passport is loaded from (default 1).
ALIBI_DATA_DIR      root for generated files (default ``data/alibi/app``).
ALIBI_KEY_DIR       demo keystore directory (default ``data/alibi/keys``).
ALIBI_NOW           fixed UTC clock for the app, ISO-8601 (default: the wall clock).
ALIBI_REPLAY        ``1`` shows the replay banner on every page.
ALIBI_BANK_URL      base URL of the bank service dispatch posts to. Unset: this process's own
                    bank routes are called directly, with no network.
ALIBI_BANK_A_TOKEN  bearer token that authenticates recipient bank_a (demo default).
ALIBI_BANK_B_TOKEN  bearer token that authenticates recipient bank_b (demo default).
ALIBI_HOLDER_NAME   display name of the account holder (default Maya).
ALIBI_MASTER_KEY    passport encryption key; read by ``alibi.core.store``, never by this module.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path

from alibi.core.schemas import parse_utc
from alibi.core.signing import DEFAULT_KEY_DIR, DEMO_KEYS, KeyPurpose

REPO_ROOT = Path(__file__).resolve().parent.parent.parent
DEFAULT_DATA_DIR = REPO_ROOT / "data" / "alibi" / "app"

# Demo signing keys the app needs beyond S1's four: data's two merchant issuers sign their own
# records. The key ids are pinned in the trust registry written next to the keys.
APP_KEYS: dict[str, tuple[KeyPurpose, str]] = {
    **DEMO_KEYS,
    "mock_marketplace": (KeyPurpose.RECORD_SIGNING, "mock-marketplace"),
    "second_platform": (KeyPurpose.RECORD_SIGNING, "second-platform"),
}
PACKAGE_KID = "alibi_demo"

RECIPIENTS: dict[str, dict[str, str]] = {
    "bank_a": {"name": "Bank A", "analyst": "Analyst, Bank A", "token_env": "ALIBI_BANK_A_TOKEN",
               "default_token": "demo-token-bank-a"},
    "bank_b": {"name": "Bank B", "analyst": "Analyst, Bank B", "token_env": "ALIBI_BANK_B_TOKEN",
               "default_token": "demo-token-bank-b"},
}


def recipient_name(recipient: str) -> str:
    return RECIPIENTS.get(recipient, {}).get("name", recipient)


@dataclass(frozen=True)
class Settings:
    case_number: int = 1
    data_dir: Path = DEFAULT_DATA_DIR
    key_dir: Path = Path(DEFAULT_KEY_DIR)
    fixed_now: datetime | None = None
    replay: bool = False
    bank_url: str | None = None
    holder_name: str = "Maya"
    tokens: dict[str, str] = field(default_factory=dict)   # recipient -> bearer token
    static_prefix: str = "/static"

    @property
    def db_path(self) -> Path:
        return self.data_dir / f"case_{self.case_number}.db"

    @property
    def sidecar_path(self) -> Path:
        return self.data_dir / f"case_{self.case_number}.state.json"

    def now(self) -> datetime:
        """The app clock: ``ALIBI_NOW`` when set, else the current UTC time."""
        return self.fixed_now if self.fixed_now is not None else datetime.now(timezone.utc)

    def recipient_for_token(self, token: str | None) -> str | None:
        if not token:
            return None
        for recipient, expected in self.tokens.items():
            if token == expected:
                return recipient
        return None


def settings_from_env(env: dict[str, str] | None = None, **overrides) -> Settings:
    """Build settings from ``env`` (default ``os.environ``); keyword overrides win."""
    env = dict(os.environ if env is None else env)
    fixed = env.get("ALIBI_NOW", "").strip()
    tokens = {rid: env.get(spec["token_env"], "").strip() or spec["default_token"]
              for rid, spec in RECIPIENTS.items()}
    values = {
        "case_number": int(env.get("ALIBI_CASE", "1") or 1),
        "data_dir": Path(env.get("ALIBI_DATA_DIR") or DEFAULT_DATA_DIR),
        "key_dir": Path(env.get("ALIBI_KEY_DIR") or DEFAULT_KEY_DIR),
        "fixed_now": parse_utc(fixed) if fixed else None,
        "replay": env.get("ALIBI_REPLAY", "").strip() in ("1", "true", "yes"),
        "bank_url": (env.get("ALIBI_BANK_URL") or "").strip().rstrip("/") or None,
        "holder_name": env.get("ALIBI_HOLDER_NAME", "").strip() or "Maya",
        "tokens": tokens,
    }
    values.update(overrides)
    return Settings(**values)
