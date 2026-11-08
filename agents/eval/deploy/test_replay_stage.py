"""Checks the replay build stage against throwaway git repositories; no network, no real repo."""

from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import subprocess
from pathlib import Path

import pytest

from agents.eval.deploy import replay_stage as rs

pytestmark = pytest.mark.skipif(shutil.which("git") is None, reason="needs git")

# Isolated from the user's git config, with fixed identities and dates.
GIT_ENV = {
    "GIT_CONFIG_GLOBAL": os.devnull,
    "GIT_CONFIG_NOSYSTEM": "1",
    "GIT_AUTHOR_NAME": "replay-stage-test",
    "GIT_AUTHOR_EMAIL": "replay-stage-test@example.invalid",
    "GIT_COMMITTER_NAME": "replay-stage-test",
    "GIT_COMMITTER_EMAIL": "replay-stage-test@example.invalid",
    "GIT_AUTHOR_DATE": "2026-11-08T00:00:00Z",
    "GIT_COMMITTER_DATE": "2026-11-08T00:00:00Z",
}

BASE = {
    "pyproject.toml": '[project]\nname = "fixture"\n',
    "uv.lock": "version = 1\n",
    "README.md": "not staged\n",
    ".env.example": "NAME_ONLY=\n",
    "alibi/__init__.py": "",
    "alibi/app/main.py": "APP = 1\n",
    "alibi/app/templates/passport/home.html": "<p>home</p>\n",
    "alibi/app/static/alibi.css": "body {}\n",
    "alibi/agents/prompts/curator.md": "# Curator\n",
    "alibi/connectors/keys.py": "KEY_DIR = 'data/alibi/keys'\n",
    "alibi/tests/test_app.py": "def test_app():\n    pass\n",
    "alibi/eval/seal.py": "SEAL = 1\n",
    "alibi/REPORTS/S1.md": "# S1\n",
    "alibi/fixtures/maya_supported.json": "{}\n",
    "alibi/fixtures/replays/maya_supported.json": '{"schema": "alibi.replay.v1"}\n',
    "alibi/fixtures/replays/maya_contradiction.json": '{"schema": "alibi.replay.v1"}\n',
    "tools/lockbox.py": "LOCKBOX = 1\n",
    "tools/team.py": "TEAM = 1\n",
    "tools/board.py": "BOARD = 1\n",
    "shared/splits.json": '{"lockbox_from": 1000}\n',
    "shared/CONTRACTS.md": "# Contracts\n",
    "agents/data/api.py": "API = 1\n",
    "agents/data/out/cases/case_0001.json": '{"case_number": 1}\n',
    "agents/data/out/cases/case_0002.json": '{"case_number": 2}\n',
    "agents/data/out/cases/case_0080.json": '{"case_number": 80}\n',
    "agents/data/out/cases/case_0081.json": '{"case_number": 81}\n',
    "agents/data/out/cases/case_1000.json": '{"case_number": 1000}\n',
    "agents/research/deploy/modal_app.py": "APP = 'alibi-backend'\n",
    "agents/eval/deploy/router.py": "ROUTER = 1\n",
    "agents/eval/NOTES.md": "# Notes\n",
}
RUNBOOK = {"alibi/RUNBOOK.md": "# Runbook\n"}

#: What BASE stages: runtime code and public inputs, never tests, eval, docs or cases past 80.
STAGED = sorted([
    "pyproject.toml",
    "uv.lock",
    "alibi/__init__.py",
    "alibi/app/main.py",
    "alibi/app/templates/passport/home.html",
    "alibi/app/static/alibi.css",
    "alibi/agents/prompts/curator.md",
    "alibi/connectors/keys.py",
    "alibi/fixtures/maya_supported.json",
    "alibi/fixtures/replays/maya_supported.json",
    "alibi/fixtures/replays/maya_contradiction.json",
    "tools/lockbox.py",
    "tools/team.py",
    "shared/splits.json",
    "agents/data/api.py",
    "agents/data/out/cases/case_0001.json",
    "agents/data/out/cases/case_0002.json",
    "agents/data/out/cases/case_0080.json",
    "agents/research/deploy/modal_app.py",
    "agents/eval/deploy/router.py",
])


def git(repo: Path, *args: str) -> str:
    env = {k: v for k, v in os.environ.items() if k not in rs._GIT_LOCATION_ENV}
    env.update(GIT_ENV)
    done = subprocess.run(["git", "-C", str(repo), *args], env=env, capture_output=True, text=True, check=True)
    return done.stdout.strip()


def write(repo: Path, files: dict[str, str]) -> None:
    for rel, text in files.items():
        path = repo / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8")


def commit(repo: Path, message: str) -> str:
    # --force: a user-level ignore file must not silently drop a fixture such as .env.
    git(repo, "add", "--all", "--force")
    git(repo, "-c", "commit.gpgsign=false", "commit", "-q", "-m", message)
    return git(repo, "rev-parse", "HEAD")


def make_repo(root: Path, files: dict[str, str], links: dict[str, str] | None = None) -> tuple[Path, str]:
    repo = root / "repo"
    repo.mkdir()
    git(repo, "init", "-q", "-b", "main")
    write(repo, files)
    for rel, target in (links or {}).items():
        (repo / rel).parent.mkdir(parents=True, exist_ok=True)
        try:
            os.symlink(target, repo / rel)
        except OSError:
            pytest.skip("symlinks are not available here")
    return repo, commit(repo, "fixture")


def staged_files(dest: Path) -> list[str]:
    return sorted(p.relative_to(dest).as_posix() for p in dest.rglob("*") if p.is_file() or p.is_symlink())


# --------------------------------------------------------------------------- allowlist


@pytest.mark.parametrize("path", [
    "alibi/app/main.py",
    "alibi/app/__main__.py",
    "alibi/connectors/keys.py",
    "alibi/app/static/alibi.css",
    "alibi/app/templates/partials/replay_banner.html",
    "alibi/agents/prompts/matcher.json",
    "alibi/fixtures/replays/maya_contradiction_inj01.json",
    "agents/data/out/cases/case_0001.json",
    "agents/data/out/cases/case_0080.json",
    "agents/data/out/merchant_registry.json",
    "agents/research/deploy/modal_app.py",
    "tools/lockbox.py",
    "shared/splits.json",
    "uv.lock",
])
def test_runtime_and_public_paths_are_allowlisted_and_clean(path):
    assert rs.allowed(path)
    assert not rs.secret_shaped(path)
    assert not rs.private_shaped(path)


@pytest.mark.parametrize("path", [
    "agents/data/out/cases/case_0000.json",
    "agents/data/out/cases/case_0081.json",
    "agents/data/out/cases/case_1001.json",
    "alibi/tests/test_app_routes.py",
    "alibi/eval/seal.py",
    "alibi/eval/schemas/manifest.schema.json",
    "alibi/REPORTS/S5b.md",
    "alibi/SPEC.md",
    "alibi/fixtures/sealed/case.json",
    "alibi/fixtures/replays/nested/x.json",
    "agents/eval/run_sealed.py",
    "agents/eval/deploy/test_router.py",
    "agents/data/out/receipts/receipts.json",
    "data/alibi/app/case_1.db",
    ".env",
    ".env.example",
    "team.json",
    "tools/board.py",
    "alibi/app/static/../../.env",
    "alibi/app/static/.hidden.css",
])
def test_everything_else_is_not_allowlisted(path):
    assert not rs.allowed(path)


@pytest.mark.parametrize("path", [
    ".env", "alibi/.env.local", "alibi/.env/config", "x/id_rsa", "x/id_ed25519.pub", "x/server.pem",
    "x/signing.key", "x/app.db", "x/case_1.sqlite3", "x/client_secret.json", "x/credentials",
    "x/github_token.txt", "x/.netrc",
])
def test_secret_shaped_names(path):
    assert rs.secret_shaped(path)


# --------------------------------------------------------------------------- dry and release


def test_dry_stage_copies_only_allowlisted_tracked_files(tmp_path):
    repo, sha = make_repo(tmp_path, BASE)
    # Working-tree changes after the commit must not reach the stage.
    write(repo, {
        "alibi/app/main.py": "APP = 'edited'\n",
        "alibi/app/untracked.py": "X = 1\n",
        "alibi/fixtures/replays/untracked.json": "{}\n",
        ".env": "NAME_ONLY=\n",
    })
    dest = tmp_path / "stage"
    out = rs.stage(dest, mode="dry", repo=repo)
    assert (out["candidate"], out["mode"]) == (sha, "dry")
    assert [f["path"] for f in out["files"]] == STAGED
    assert out["missing"] == ["alibi/RUNBOOK.md"]
    assert staged_files(dest) == STAGED
    assert (dest / "alibi/app/main.py").read_bytes() == b"APP = 1\n"
    entry = next(f for f in out["files"] if f["path"] == "alibi/app/main.py")
    assert entry == {"path": "alibi/app/main.py", "size": 8, "sha256": hashlib.sha256(b"APP = 1\n").hexdigest()}


def test_dry_stage_is_deterministic(tmp_path):
    repo, _ = make_repo(tmp_path, BASE)
    assert rs.stage(tmp_path / "a", mode="dry", repo=repo) == rs.stage(tmp_path / "b", mode="dry", repo=repo)


def test_release_stages_a_full_sha_with_runbook_and_replays(tmp_path):
    repo, sha = make_repo(tmp_path, {**BASE, **RUNBOOK})
    dest = tmp_path / "stage"
    out = rs.stage(dest, mode="release", candidate=sha, repo=repo)
    assert (out["candidate"], out["mode"]) == (sha, "release")
    assert "missing" not in out
    assert [f["path"] for f in out["files"]] == sorted([*STAGED, "alibi/RUNBOOK.md"])
    assert staged_files(dest) == sorted([*STAGED, "alibi/RUNBOOK.md"])


@pytest.mark.parametrize("drop", rs.REQUIRED_RELEASE)
def test_release_refuses_when_a_required_file_is_missing(tmp_path, drop):
    repo, sha = make_repo(tmp_path, {k: v for k, v in {**BASE, **RUNBOOK}.items() if k != drop})
    dest = tmp_path / "stage"
    with pytest.raises(rs.StageError) as err:
        rs.stage(dest, mode="release", candidate=sha, repo=repo)
    assert f"{drop}: required for release but not tracked at {sha}" in err.value.problems
    assert not dest.exists()


def test_release_refuses_a_runbook_that_is_on_disk_but_untracked(tmp_path):
    repo, sha = make_repo(tmp_path, BASE)
    write(repo, RUNBOOK)
    with pytest.raises(rs.StageError, match=re.escape("alibi/RUNBOOK.md: required for release")):
        rs.stage(tmp_path / "stage", mode="release", candidate=sha, repo=repo)


@pytest.mark.parametrize("name", ["HEAD", "main", "short"])
def test_release_takes_a_full_sha_only(tmp_path, name):
    repo, sha = make_repo(tmp_path, {**BASE, **RUNBOOK})
    dest = tmp_path / "stage"
    with pytest.raises(rs.StageError, match="full commit SHA"):
        rs.stage(dest, mode="release", candidate=sha[:12] if name == "short" else name, repo=repo)
    assert not dest.exists()


def test_release_refuses_an_unknown_sha(tmp_path):
    repo, _ = make_repo(tmp_path, {**BASE, **RUNBOOK})
    with pytest.raises(rs.StageError, match="is not a commit"):
        rs.stage(tmp_path / "stage", mode="release", candidate="0" * 40, repo=repo)


def test_require_adds_files_and_never_leaves_the_allowlist(tmp_path):
    repo, sha = make_repo(tmp_path, {**BASE, **RUNBOOK})
    extra = "alibi/fixtures/replays/case_1_supported.json"
    with pytest.raises(rs.StageError, match="agents/eval/NOTES.md: required but not on the allowlist"):
        rs.stage(tmp_path / "a", mode="release", candidate=sha, repo=repo, require=["agents/eval/NOTES.md"])
    with pytest.raises(rs.StageError, match=re.escape(f"{extra}: required for release")):
        rs.stage(tmp_path / "b", mode="release", candidate=sha, repo=repo, require=[extra])
    assert rs.stage(tmp_path / "c", mode="dry", repo=repo, require=[extra])["missing"] == [extra]


# --------------------------------------------------------------------------- fail closed


@pytest.mark.parametrize("path", [
    "alibi/app/static/.env",
    "alibi/.env.local/readme.md",
    "alibi/connectors/signing.pem",
    "tools/id_ed25519",
    "shared/credentials.json",
    "agents/data/out/app.db",
    "alibi/fixtures/replays/api_key.json",
    "agents/eval/deploy/service_token.txt",
])
def test_secret_shaped_names_fail_closed(tmp_path, path):
    repo, _ = make_repo(tmp_path, {**BASE, path: "placeholder\n"})
    dest = tmp_path / "stage"
    with pytest.raises(rs.StageError, match=re.escape(f"{path}: secret-shaped filename")):
        rs.stage(dest, mode="dry", repo=repo)
    assert not dest.exists()


def test_inline_credential_refusal_discloses_only_path(tmp_path):
    credential = "AQ." + "x" * 48
    repo, _ = make_repo(tmp_path, {**BASE, "alibi/app/main.py": f"config = {credential!r}\n"})
    dest = tmp_path / "stage"
    with pytest.raises(rs.StageError, match="alibi/app/main.py: credential-shaped content") as exc:
        rs.stage(dest, mode="dry", repo=repo)
    assert credential not in str(exc.value)
    assert not dest.exists()


def test_stage_preserves_a_destination_that_changed_while_writing(tmp_path, monkeypatch):
    repo, _ = make_repo(tmp_path, BASE)
    dest = tmp_path / "stage"
    dest.mkdir()
    write_stage = rs._write

    def fill_destination(*args):
        (dest / "other.txt").write_text("another process", encoding="utf-8")
        return write_stage(*args)

    monkeypatch.setattr(rs, "_write", fill_destination)
    with pytest.raises(rs.StageError, match="publishing the stage failed"):
        rs.stage(dest, mode="dry", repo=repo)
    assert list(dest.iterdir()) == [dest / "other.txt"]


@pytest.mark.parametrize("path", [
    "alibi/fixtures/replays/sealed_case_1001.json",
    "alibi/fixtures/gold.json",
    "alibi/fixtures/lockbox_rows.json",
    "alibi/fixtures/private_export.json",
])
def test_private_paths_fail_closed(tmp_path, path):
    repo, _ = make_repo(tmp_path, {**BASE, path: "{}\n"})
    dest = tmp_path / "stage"
    with pytest.raises(rs.StageError, match=re.escape(f"{path}: private path")):
        rs.stage(dest, mode="dry", repo=repo)
    assert not dest.exists()


@pytest.mark.parametrize("path", ["alibi/fixtures/replays/linked.json", "alibi/notes_link"])
def test_symlinks_fail_closed(tmp_path, path):
    repo, _ = make_repo(tmp_path, BASE, links={path: "../../../.env.example"})
    dest = tmp_path / "stage"
    with pytest.raises(rs.StageError, match=re.escape(f"{path}: symlink")):
        rs.stage(dest, mode="dry", repo=repo)
    assert not dest.exists()


def test_submodules_fail_closed(tmp_path):
    repo, sha = make_repo(tmp_path, BASE)
    git(repo, "update-index", "--add", "--cacheinfo", f"160000,{sha},alibi/vendor")
    git(repo, "-c", "commit.gpgsign=false", "commit", "-q", "-m", "gitlink")
    with pytest.raises(rs.StageError, match=re.escape("alibi/vendor: not a regular file (git mode 160000)")):
        rs.stage(tmp_path / "stage", mode="dry", repo=repo)


def test_destination_must_be_new_or_empty_and_outside_the_repo(tmp_path):
    repo, _ = make_repo(tmp_path, BASE)
    full = tmp_path / "full"
    full.mkdir()
    (full / "left.txt").write_text("x")
    with pytest.raises(rs.StageError, match="is not empty"):
        rs.stage(full, mode="dry", repo=repo)
    assert [p.name for p in full.iterdir()] == ["left.txt"]

    with pytest.raises(rs.StageError, match="inside the repository"):
        rs.stage(repo / "stage", mode="dry", repo=repo)
    assert not (repo / "stage").exists()

    empty = tmp_path / "empty"
    empty.mkdir()
    rs.stage(empty, mode="dry", repo=repo)
    assert staged_files(empty) == STAGED

    target = tmp_path / "target"
    target.mkdir()
    link = tmp_path / "link"
    try:
        link.symlink_to(target, target_is_directory=True)
    except OSError:
        pytest.skip("symlinks are not available here")
    with pytest.raises(rs.StageError, match="is a symlink"):
        rs.stage(link, mode="dry", repo=repo)
    assert list(target.iterdir()) == []


def test_verify_accepts_only_the_manifest(tmp_path):
    (tmp_path / "a.txt").write_bytes(b"a")
    expected = {"a.txt": (1, hashlib.sha256(b"a").hexdigest())}
    assert rs._verify(tmp_path, expected) == []
    (tmp_path / "extra.json").write_bytes(b"{}")
    assert rs._verify(tmp_path, expected) == ["extra.json: in the stage but not in the manifest"]
    (tmp_path / "extra.json").unlink()
    (tmp_path / "a.txt").write_bytes(b"b")
    assert rs._verify(tmp_path, expected) == ["a.txt: differs from the candidate"]


# --------------------------------------------------------------------------- CLI


def test_cli_prints_one_json_line_without_local_paths(tmp_path, capsys):
    repo, sha = make_repo(tmp_path, {**BASE, **RUNBOOK})
    code = rs.main(["release", "--candidate", sha, "--dest", str(tmp_path / "stage"), "--repo", str(repo)])
    out, err = capsys.readouterr()
    assert (code, err) == (0, "")
    assert len(out.splitlines()) == 1
    result = json.loads(out)
    assert (result["candidate"], result["mode"]) == (sha, "release")
    assert set(result) == {"candidate", "mode", "files"}
    assert str(tmp_path) not in out


def test_cli_refusal_goes_to_stderr_and_writes_nothing(tmp_path, capsys):
    repo, sha = make_repo(tmp_path, BASE)
    dest = tmp_path / "stage"
    code = rs.main(["release", "--candidate", sha, "--dest", str(dest), "--repo", str(repo)])
    out, err = capsys.readouterr()
    assert (code, out) == (1, "")
    assert "replay_stage: refused: alibi/RUNBOOK.md: required for release" in err
    assert not dest.exists()


def test_cli_release_needs_a_candidate(tmp_path):
    with pytest.raises(SystemExit) as exit_:
        rs.main(["release", "--dest", str(tmp_path / "stage")])
    assert exit_.value.code == 2
