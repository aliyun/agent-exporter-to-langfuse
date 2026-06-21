import logging
import subprocess
import sys
from pathlib import Path

logger = logging.getLogger("langstash-tester.git")


class MergeConflictError(Exception):
    def __init__(self, conflict_files: list[str]):
        self.conflict_files = conflict_files
        super().__init__(f"merge conflict: {conflict_files}")


class GitManager:
    def __init__(self, repo_url: str, local_repo: str, worktree_dir: str):
        self._repo_url = repo_url
        self._local_repo = Path(local_repo)
        self._worktree_dir = Path(worktree_dir)

    def validate_bare_repo(self) -> None:
        if not self._local_repo.exists():
            print(f"ERROR: bare repo not found at {self._local_repo}", file=sys.stderr)
            print(f"Run: git clone --bare {self._repo_url} {self._local_repo}", file=sys.stderr)
            sys.exit(1)

    def fetch(self) -> None:
        self._run_git(["fetch", "origin"], cwd=self._local_repo)

    def create_worktree(self, job_id: str, branch: str, commit: str | None = None) -> Path:
        worktree_path = self._worktree_dir / job_id
        worktree_path.parent.mkdir(parents=True, exist_ok=True)

        ref = commit or f"origin/{branch}"
        self._run_git(
            ["worktree", "add", str(worktree_path), ref, "--detach"],
            cwd=self._local_repo,
        )
        return worktree_path

    def merge_main(self, worktree_path: Path) -> None:
        try:
            self._run_git(
                ["merge", "origin/main", "--no-edit"],
                cwd=worktree_path,
            )
        except subprocess.CalledProcessError:
            conflict_files = self._get_conflict_files(worktree_path)
            self._run_git(["merge", "--abort"], cwd=worktree_path)
            raise MergeConflictError(conflict_files)

    def remove_worktree(self, job_id: str) -> None:
        worktree_path = self._worktree_dir / job_id
        if worktree_path.exists():
            self._run_git(
                ["worktree", "remove", str(worktree_path), "--force"],
                cwd=self._local_repo,
            )

    def cleanup_all_worktrees(self) -> None:
        if self._worktree_dir.exists():
            for child in self._worktree_dir.iterdir():
                if child.is_dir():
                    try:
                        self._run_git(
                            ["worktree", "remove", str(child), "--force"],
                            cwd=self._local_repo,
                        )
                    except Exception:
                        logger.warning("failed to remove worktree %s", child)

    def _get_conflict_files(self, worktree_path: Path) -> list[str]:
        try:
            result = subprocess.run(
                ["git", "diff", "--name-only", "--diff-filter=U"],
                cwd=worktree_path, capture_output=True, text=True, timeout=10,
            )
            return [f.strip() for f in result.stdout.strip().split("\n") if f.strip()]
        except Exception:
            return []

    @staticmethod
    def _run_git(args: list[str], cwd: Path) -> subprocess.CompletedProcess:
        cmd = ["git"] + args
        logger.debug("git %s (cwd=%s)", " ".join(args), cwd)
        return subprocess.run(
            cmd, cwd=cwd, capture_output=True, text=True, timeout=120, check=True,
        )
