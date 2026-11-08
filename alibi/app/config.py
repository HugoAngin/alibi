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
ALIBI_BANK_URL      base URL of the mock bank service (``alibi.bank.service``, port 8102 under
                    ``alibi.demo_services``). Set: dispatch POSTs the signed package to
                    ``{url}/deliver``, the analyst console reads and writes that service, and
                    ``POST /connect/bank/sync`` pulls the holder's feed from it. Unset: this
                    process's own bank routes are used, with no network.
ALIBI_MERCHANT_URL  base URL of the mock merchant (port 8101). Set together with ALIBI_BANK_URL,
                    the passport is filled through the connect routes (merchant pushes, feed
                    sync, the bank's RFC forwarding) instead of the case adapter, and the
                    investigation's settlement lookup goes to the merchant over HTTP.
ALIBI_TOKENS_FILE   the one tokens file shared with the services (default
                    ``data/alibi/bank_tokens.json``; ``alibi.connectors.http.load_tokens``):
                    bank_a, bank_b, feed and admin. Created when missing, so the app and the
                    bank service always agree. Settings built from an explicit env dict (tests)
                    without ALIBI_TOKENS_FILE keep the demo defaults and touch no file.
ALIBI_BANK_A_TOKEN  bearer token that authenticates recipient bank_a (wins over the file).
ALIBI_BANK_B_TOKEN  bearer token that authenticates recipient bank_b (wins over the file).
ALIBI_FEED_TOKEN    the holder's feed credential on the bank service (wins over the file).
ALIBI_ADMIN_TOKEN   the services' admin credential (wins over the file; never used by a route).
ALIBI_HOLDER_NAME   display name of the account holder (default Maya).
ALIBI_HOLDER_TOKEN  optional bearer token for the holder's routes. Set: every holder route needs
                    ``Authorization: Bearer <token>`` or the ``alibi_holder`` session cookie that
                    ``GET /passport?token=<token>`` sets once. Unset (the demo): the holder is the
                    demo holder and no credential is asked for.
ALIBI_API_ORIGIN    the origin this backend is reachable at (default ``http://127.0.0.1:8100``);
                    reported by ``/health`` and used for every absolute URL the app hands out
                    (package and export links), so a server-side front end can follow them.
ALIBI_MASTER_KEY    passport encryption key; read by ``alibi.core.store``, never by this module.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path

from alibi.connectors.http import DEFAULT_TOKENS_PATH, TOKEN_ENV, load_tokens
from alibi.connectors.keys import CONNECTOR_KEYS
from alibi.core.schemas import parse_utc
from alibi.core.signing import DEFAULT_KEY_DIR, KeyPurpose

REPO_ROOT = Path(__file__).resolve().parent.parent.parent
DEFAULT_DATA_DIR = REPO_ROOT / "data" / "alibi" / "app"

# One demo key spec for every party (``alibi.connectors.keys.CONNECTOR_KEYS``): the core's keys,
# the merchant's ``merchant_mock`` and the two case-issuer keys the adapter signs with. The app
# keystore and the connector keystore therefore write the same trust registry.
APP_KEYS: dict[str, tuple[KeyPurpose, str]] = dict(CONNECTOR_KEYS)
PACKAGE_KID = "alibi_demo"
DEFAULT_API_ORIGIN = "http://127.0.0.1:8100"
DEFAULT_TOKENS_FILE = REPO_ROOT / DEFAULT_TOKENS_PATH

RECIPIENTS: dict[str, dict[str, str]] = {
    "bank_a": {"name": "Bank A", "analyst": "Analyst, Bank A", "token_env": "ALIBI_BANK_A_TOKEN",
               "default_token": "demo-token-bank-a"},
    "bank_b": {"name": "Bank B", "analyst": "Analyst, Bank B", "token_env": "ALIBI_BANK_B_TOKEN",
               "default_token": "demo-token-bank-b"},
}
# The service identities the app itself holds a credential for (never a recipient).
SERVICE_TOKENS: dict[str, dict[str, str]] = {
    "feed": {"token_env": "ALIBI_FEED_TOKEN", "default_token": "demo-token-feed"},
    "admin": {"token_env": "ALIBI_ADMIN_TOKEN", "default_token": "demo-token-admin"},
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
    merchant_url: str | None = None
    holder_name: str = "Maya"
    tokens: dict[str, str] = field(default_factory=dict)   # recipient -> bearer token
    service_tokens: dict[str, str] = field(default_factory=dict)   # feed, admin -> bearer token
    tokens_file: Path | None = None                          # the shared file the tokens came from
    holder_token: str | None = None                          # None: the demo holder, no credential
    api_origin: str = DEFAULT_API_ORIGIN
    static_prefix: str = "/static"

    @property
    def connectors(self) -> bool:
        """True when the passport is filled through the services (both URLs set)."""
        return bool(self.bank_url and self.merchant_url)

    @property
    def feed_token(self) -> str:
        return self.service_tokens.get("feed") or SERVICE_TOKENS["feed"]["default_token"]

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
    env_given = env is not None
    env = dict(os.environ if env is None else env)
    fixed = env.get("ALIBI_NOW", "").strip()
    if fixed and len(fixed) == 10:
        fixed += "T00:00:00Z"            # a date alone means midnight UTC
    if fixed:
        try:
            parse_utc(fixed)
        except ValueError as exc:
            raise ValueError(f"ALIBI_NOW must be a UTC ISO-8601 time such as 2026-11-05T12:00:00Z "
                             f"(got {fixed!r})") from exc
    tokens_file, shared = _shared_tokens(env, explicit=env_given)
    tokens = {rid: env.get(spec["token_env"], "").strip() or shared.get(rid) or spec["default_token"]
              for rid, spec in RECIPIENTS.items()}
    service = {name: env.get(spec["token_env"], "").strip() or shared.get(name) or spec["default_token"]
               for name, spec in SERVICE_TOKENS.items()}
    values = {
        "case_number": int(env.get("ALIBI_CASE", "1") or 1),
        "data_dir": Path(env.get("ALIBI_DATA_DIR") or DEFAULT_DATA_DIR),
        "key_dir": Path(env.get("ALIBI_KEY_DIR") or DEFAULT_KEY_DIR),
        "fixed_now": parse_utc(fixed) if fixed else None,
        "replay": env.get("ALIBI_REPLAY", "").strip() in ("1", "true", "yes"),
        "bank_url": (env.get("ALIBI_BANK_URL") or "").strip().rstrip("/") or None,
        "merchant_url": (env.get("ALIBI_MERCHANT_URL") or "").strip().rstrip("/") or None,
        "holder_name": env.get("ALIBI_HOLDER_NAME", "").strip() or "Maya",
        "tokens": tokens,
        "service_tokens": service,
        "tokens_file": tokens_file,
        "holder_token": env.get("ALIBI_HOLDER_TOKEN", "").strip() or None,
        "api_origin": (env.get("ALIBI_API_ORIGIN") or "").strip().rstrip("/") or DEFAULT_API_ORIGIN,
    }
    values.update(overrides)
    return Settings(**values)


def _shared_tokens(env: dict[str, str], *, explicit: bool) -> tuple[Path | None, dict[str, str]]:
    """The one tokens file and what it holds. ``ALIBI_TOKENS_FILE`` names it; otherwise the
    default ``data/alibi/bank_tokens.json`` is used when settings come from the process
    environment. An explicit env dict (tests) without the variable reads no file and creates
    none, so test runs keep the demo tokens and never touch ``data/alibi/``. ``load_tokens``
    itself lets the ``ALIBI_*_TOKEN`` variables of the process win, so the app and the services
    agree on every identity."""
    named = (env.get("ALIBI_TOKENS_FILE") or "").strip()
    if named:
        path = Path(named)
    elif not explicit:
        path = DEFAULT_TOKENS_FILE
    else:
        return None, {}
    return path, {name: value for name, value in load_tokens(path).items() if name in TOKEN_ENV}
