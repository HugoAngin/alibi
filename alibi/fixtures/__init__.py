"""Synthetic fixtures for Alibi: Maya's ledger variants and a retail purchase example.

The committed JSON files beside this module hold unsigned bodies (``schema`` is
``alibi.fixture.v1``). Load and sign them through ``alibi.fixtures.maya``::

    from alibi.fixtures.maya import load_variant, sign_event, sign_request, settlement_lookup

Regenerate them with ``uv run --frozen python -m alibi.fixtures.maya``. The names below are
re-exported lazily, so importing this package does not import ``maya`` until one is used.
"""

_EXPORTS = frozenset({
    "ALL_FIXTURES", "FIXTURE_DIR", "RETAIL", "SCHEMA", "VARIANTS", "build_fixture",
    "build_retail_fixture", "fixture_path", "load_variant", "settlement_lookup", "sign_event",
    "sign_request", "txn_refresh", "write_all",
})

__all__ = sorted(_EXPORTS)


def __getattr__(name: str):
    if name in _EXPORTS:
        from alibi.fixtures import maya

        return getattr(maya, name)
    raise AttributeError(f"module 'alibi.fixtures' has no attribute {name!r}")
