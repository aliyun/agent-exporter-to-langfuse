import subprocess
from pathlib import Path

import pytest

from src.git_manager import GitManager


def _git(cwd, *args):
    return subprocess.run(["git", *args], cwd=str(cwd), capture_output=True, text=True, check=True)


def _head_sha(repo):
    return _git(repo, "rev-parse", "HEAD").stdout.strip()


def _worktree_list(bare):
    return _git(bare, "worktree", "list").stdout


@pytest.fixture
def bare_repo(tmp_path):
    work = tmp_path / "work"
    work.mkdir()
    _git(work, "init", "-b", "main")
    _git(work, "config", "user.email", "t@t")
    _git(work, "config", "user.name", "t")
    (work / "README.md").write_text("hi\n")
    _git(work, "add", ".")
    _git(work, "commit", "-m", "init")

    bare = tmp_path / "repo.git"
    _git(tmp_path, "clone", "--bare", str(work), str(bare))

    worktree_dir = tmp_path / "worktrees"
    worktree_dir.mkdir()
    gm = GitManager(str(work), str(bare), str(worktree_dir))
    return gm, bare, worktree_dir


def test_create_then_remove_cleans_directory_and_registration(bare_repo):
    gm, bare, worktree_dir = bare_repo
    head = _head_sha(bare)

    gm.create_worktree("job-1", "main", commit=head)
    wt = worktree_dir / "job-1"
    assert wt.exists()
    assert str(wt) in _worktree_list(bare)

    gm.remove_worktree("job-1")
    assert not wt.exists()
    assert str(wt) not in _worktree_list(bare)


def test_remove_worktree_recovers_when_git_remove_fails(bare_repo, monkeypatch):
    """git worktree remove --force can return 255 (.venv/locked/busy); removal
    must fall back to rmtree + prune and never raise (R-2)."""
    gm, bare, worktree_dir = bare_repo
    head = _head_sha(bare)
    gm.create_worktree("job-2", "main", commit=head)
    wt = worktree_dir / "job-2"
    # simulate uv sync artifacts that make `git worktree remove --force` choke
    (wt / "exporter").mkdir(parents=True)
    (wt / "exporter" / ".venv").mkdir()
    (wt / "exporter" / ".venv" / "marker").write_text("x")
    assert str(wt) in _worktree_list(bare)

    real_run_git = GitManager._run_git

    def fake_run_git(args, cwd):
        if args[:2] == ["worktree", "remove"]:
            raise subprocess.CalledProcessError(255, "git " + " ".join(args))
        return real_run_git(args, cwd)

    monkeypatch.setattr(GitManager, "_run_git", staticmethod(fake_run_git))

    gm.remove_worktree("job-2")  # must not raise

    assert not wt.exists()
    assert str(wt) not in _worktree_list(bare)


def test_remove_worktree_missing_is_noop(bare_repo):
    gm, _bare, _worktree_dir = bare_repo
    gm.remove_worktree("never-existed")  # must not raise


def test_cleanup_all_worktrees_removes_every_tree(bare_repo):
    gm, bare, worktree_dir = bare_repo
    head = _head_sha(bare)
    gm.create_worktree("a", "main", commit=head)
    gm.create_worktree("b", "main", commit=head)
    assert (worktree_dir / "a").exists()
    assert (worktree_dir / "b").exists()

    gm.cleanup_all_worktrees()

    assert not (worktree_dir / "a").exists()
    assert not (worktree_dir / "b").exists()
    listing = _worktree_list(bare)
    assert str(worktree_dir / "a") not in listing
    assert str(worktree_dir / "b") not in listing
