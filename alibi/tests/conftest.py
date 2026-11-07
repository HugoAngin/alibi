"""Shared test fixtures. Tests never touch the network, Gemini, or data/alibi/ in the repo."""

from datetime import datetime, timezone

import pytest

NOW = datetime(2026, 11, 7, 12, 0, tzinfo=timezone.utc)


@pytest.fixture(autouse=True)
def _isolated_env(monkeypatch):
    # Tests must not pick up real credentials or keys from the developer's environment.
    monkeypatch.delenv("GEMINI_API_KEY", raising=False)
    monkeypatch.delenv("ALIBI_MASTER_KEY", raising=False)


@pytest.fixture
def now() -> datetime:
    return NOW


@pytest.fixture
def keystore(tmp_path):
    from alibi.core.signing import DemoKeystore

    return DemoKeystore(tmp_path / "keys").ensure()


@pytest.fixture
def registry(keystore):
    return keystore.registry()
