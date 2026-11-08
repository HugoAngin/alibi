"""Only the verifier's exact untracked data symlink bypasses the clean-tree gate."""

import pytest

from agents.eval import run_sealed


def configure(monkeypatch, root, status):
    monkeypatch.setattr(run_sealed, "REPO", root)

    def git(command, *, cwd, text):
        assert cwd == root and text
        if command[1] == "status":
            assert command[2:] == ["--porcelain", "--untracked-files=all"]
            return status
        assert command[1] == "rev-parse"
        return "evaluator" if command[-1] == "HEAD" else "candidate"

    monkeypatch.setattr(run_sealed.subprocess, "check_output", git)


def test_verifier_data_symlink_is_allowed_without_reading_target(tmp_path, monkeypatch):
    (tmp_path / "data").symlink_to(tmp_path / "nonexistent-external-target", target_is_directory=True)
    configure(monkeypatch, tmp_path, "?? data\n")
    assert run_sealed._pinned_candidate("sealed-candidate-3") == ("candidate", "evaluator")


@pytest.mark.parametrize("status", [" M data\n", "A  data\n", "?? other\n", "?? data\n?? other\n",
                                   "?? data\n M agents/eval/run_sealed.py\n", "?? data/child\n"])
def test_symlink_does_not_hide_other_changes(tmp_path, monkeypatch, status):
    (tmp_path / "data").symlink_to(tmp_path / "external", target_is_directory=True)
    configure(monkeypatch, tmp_path, status)
    with pytest.raises(ValueError, match="checkout must be clean"):
        run_sealed._pinned_candidate("sealed-candidate-3")


@pytest.mark.parametrize("kind", ["directory", "file", "absent"])
def test_ordinary_data_path_is_not_exempt(tmp_path, monkeypatch, kind):
    if kind == "directory":
        (tmp_path / "data").mkdir()
    elif kind == "file":
        (tmp_path / "data").write_text("synthetic")
    configure(monkeypatch, tmp_path, "?? data\n")
    with pytest.raises(ValueError, match="checkout must be clean"):
        run_sealed._pinned_candidate("sealed-candidate-3")
