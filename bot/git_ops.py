import logging
from pathlib import Path

import git  # type: ignore

logger = logging.getLogger(__name__)


class GitOps:
    def __init__(self, repo_path: str, branch: str = "main"):
        self.repo_path = Path(repo_path)
        self.branch = branch

    def commit_and_push(
        self, message: str, files: list[str]
    ) -> tuple[bool, str]:
        """Stage files, commit, push. Returns (success, error_message)."""
        try:
            repo = git.Repo(self.repo_path)
        except git.InvalidGitRepositoryError:
            return False, f"{self.repo_path} is not a git repository"

        try:
            existing = [f for f in files if (self.repo_path / f).exists()]
            if not existing:
                return False, "No files to commit"
            repo.index.add(existing)
            repo.index.commit(message)
        except Exception as exc:
            return False, f"Commit failed: {exc}"

        try:
            origin = repo.remote("origin")
            origin.push(self.branch)
            return True, ""
        except Exception as exc:
            return False, f"Push failed (commit is local): {exc}"

    def revert_last_commit(self) -> tuple[bool, str]:
        """Run git revert HEAD (non-interactive)."""
        try:
            repo = git.Repo(self.repo_path)
            repo.git.revert("HEAD", no_edit=True)
            try:
                repo.remote("origin").push(self.branch)
            except Exception as exc:
                return True, f"Reverted locally but push failed: {exc}"
            return True, ""
        except Exception as exc:
            return False, str(exc)

    def todo_entries(self, journal_path: str) -> list[str]:
        """Return lines from the journal that contain ; TODO."""
        path = Path(journal_path)
        if not path.exists():
            return []
        return [
            line.strip()
            for line in path.read_text().splitlines()
            if "; TODO" in line
        ]
