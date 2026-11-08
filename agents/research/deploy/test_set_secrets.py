"""The hosted-demo secrets plan (set_secrets.py): every side gets the same values, nothing is printed."""

from __future__ import annotations

import itertools

from agents.research.deploy import set_secrets as s


def _plan():
    counter = itertools.count(1)
    return s.plan("https://example.modal.run/", token=lambda: f"tok{next(counter)}")


def test_modal_and_workers_share_each_token():
    p = _plan()
    assert p.modal == {"ALIBI_HOLDER_TOKEN": "tok1", "ALIBI_BANK_A_TOKEN": "tok2", "ALIBI_DEMO_TOKEN": "tok3"}
    assert p.workers["www"]["ALIBI_HOLDER_TOKEN"] == p.modal["ALIBI_HOLDER_TOKEN"]
    assert p.workers["bank"]["BANK_A_TOKEN"] == p.modal["ALIBI_BANK_A_TOKEN"]
    for worker in p.workers.values():
        assert worker["ALIBI_DEMO_TOKEN"] == p.modal["ALIBI_DEMO_TOKEN"]
        assert worker["ALIBI_DEMO_ORIGIN"] == "https://example.modal.run"
    assert p.workers["www"]["ALIBI_SIMULATE_ACTIONS"] == "1"
    assert set(p.workers) == set(s.WORKERS)


def test_only_tokens_the_demo_needs_and_no_unrelated_secret():
    p = _plan()
    every = set(p.modal) | {k for w in p.workers.values() for k in w}
    assert not every & {"GEMINI_API_KEY", "ALIBI_MASTER_KEY", "ANALYST_PASSCODE",
                        "BANK_SESSION_SECRET", "CLERK_SECRET_KEY"}


def test_dry_run_prints_names_never_values(capsys):
    assert s.main(["--dry-run"]) == 0
    out = capsys.readouterr().out
    assert "ALIBI_DEMO_TOKEN" in out and "would set" in out
    real = s.plan(s.DEFAULT_ORIGIN)
    assert not any(v in out for v in real.modal.values())


def test_fresh_tokens_are_long_and_distinct():
    p = s.plan(s.DEFAULT_ORIGIN)
    values = list(p.modal.values())
    assert len(set(values)) == 3 and all(len(v) >= 40 for v in values)


def test_redeploy_refuses_before_any_change_without_the_clerk_env(tmp_path, monkeypatch):
    monkeypatch.setattr(s, "WEB", tmp_path)
    called = []
    monkeypatch.setattr(s, "set_modal", lambda values: called.append("modal"))
    try:
        s.main(["--redeploy"])
    except SystemExit as exc:
        assert "Clerk" in str(exc)
    else:
        raise AssertionError("expected a refusal")
    assert called == []
