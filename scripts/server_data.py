"""Publish a consistent server SQLite snapshot on a data-only Git branch.

The code branch never contains the live database. Fetching only writes a local
snapshot; it never changes the active database or the current Git branch.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import sqlite3
import subprocess
import tempfile
from contextlib import closing
from datetime import UTC, datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
DATA_BRANCH = "server-data"
SNAPSHOT = "snapshot/bazi_chi_bot.sqlite3"
MANIFEST = "snapshot/manifest.json"
MARKER = ".bazi-chi-server-data"
LOCAL_SNAPSHOT = Path("data/server_snapshot.sqlite3")


def git(repo: Path, *args: str, output: bool = False) -> str:
    result = subprocess.run(
        ["git", *args],
        cwd=repo,
        check=True,
        stdout=subprocess.PIPE if output else subprocess.DEVNULL,
    )
    return result.stdout.decode().strip() if output else ""


def git_file(repo: Path, revision: str, name: str) -> bytes:
    return subprocess.run(
        ["git", "show", f"{revision}:{name}"],
        cwd=repo,
        check=True,
        stdout=subprocess.PIPE,
    ).stdout


def sqlite_snapshot(source: Path, destination: Path) -> str:
    if not source.is_file():
        raise ValueError(f"Database does not exist: {source}")
    with (
        closing(sqlite3.connect(source.as_uri() + "?mode=ro", uri=True)) as live,
        closing(sqlite3.connect(destination)) as copy,
    ):
        live.backup(copy)
        if copy.execute("PRAGMA integrity_check").fetchone()[0] != "ok":
            raise ValueError("SQLite snapshot failed integrity_check")
    destination.chmod(0o600)
    with destination.open("rb") as stream:
        return hashlib.file_digest(stream, "sha256").hexdigest()


def verify_snapshot(path: Path, expected_hash: str) -> None:
    with path.open("rb") as stream:
        actual = hashlib.file_digest(stream, "sha256").hexdigest()
    if actual != expected_hash:
        raise ValueError("Downloaded snapshot hash does not match its manifest")
    with closing(sqlite3.connect(path.as_uri() + "?mode=ro", uri=True)) as database:
        if database.execute("PRAGMA integrity_check").fetchone()[0] != "ok":
            raise ValueError("Downloaded snapshot failed integrity_check")


def publish(repo: Path, database: Path, *, remote: str = "origin") -> bool:
    """Push a server-owned snapshot without changing the code worktree."""
    repo = repo.resolve()
    database = database.resolve()
    if database.is_relative_to(repo):
        raise ValueError("Live server database must be outside the Git checkout")
    remote_url = git(repo, "remote", "get-url", "--push", remote, output=True)
    branch_ref = f"refs/heads/{DATA_BRANCH}"
    result = subprocess.run(
        ["git", "ls-remote", "--exit-code", remote, branch_ref],
        cwd=repo,
        stdout=subprocess.DEVNULL,
        check=False,
    )
    if result.returncode not in (0, 2):
        raise RuntimeError("Could not check the remote server-data branch")

    with tempfile.TemporaryDirectory(prefix="bazi-server-data-") as temporary:
        worktree = Path(temporary)
        git(worktree, "init", "-q")
        git(worktree, "remote", "add", "origin", remote_url)
        if result.returncode == 0:
            git(worktree, "fetch", "-q", "--depth=1", "origin", branch_ref)
            git(worktree, "checkout", "-q", "-b", DATA_BRANCH, "FETCH_HEAD")
            if not (worktree / MARKER).is_file():
                raise ValueError("Remote branch is not a Bazi Chi server-data branch")
        else:
            git(worktree, "checkout", "-q", "--orphan", DATA_BRANCH)
            (worktree / MARKER).write_text("Bazi Chi server database snapshots\n")

        snapshot = worktree / SNAPSHOT
        snapshot.parent.mkdir(parents=True, exist_ok=True)
        snapshot.unlink(missing_ok=True)
        digest = sqlite_snapshot(database, snapshot)
        manifest = worktree / MANIFEST
        if manifest.is_file() and json.loads(manifest.read_text())["sha256"] == digest:
            return False
        manifest.write_text(
            json.dumps(
                {
                    "sha256": digest,
                    "created_at": datetime.now(UTC).isoformat(),
                    "code_commit": git(repo, "rev-parse", "HEAD", output=True),
                },
                indent=2,
            )
            + "\n"
        )
        git(worktree, "add", "--", MARKER, SNAPSHOT, MANIFEST)
        git(
            worktree,
            "-c",
            "user.name=Bazi Chi server",
            "-c",
            "user.email=server-data@local.invalid",
            "commit",
            "-qm",
            "Update server database snapshot",
        )
        git(worktree, "push", "-q", "origin", f"HEAD:{branch_ref}")
    return True


def fetch(repo: Path, *, remote: str = "origin") -> Path:
    """Download the server snapshot without checking out its branch."""
    repo = repo.resolve()
    git(repo, "fetch", "-q", remote, f"refs/heads/{DATA_BRANCH}")
    if git_file(repo, "FETCH_HEAD", MARKER).decode().strip() != (
        "Bazi Chi server database snapshots"
    ):
        raise ValueError("Remote branch is not a Bazi Chi server-data branch")
    manifest = json.loads(git_file(repo, "FETCH_HEAD", MANIFEST))
    target = repo / LOCAL_SNAPSHOT
    target.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temp_name = tempfile.mkstemp(prefix=".server-snapshot-", dir=target.parent)
    temporary = Path(temp_name)
    try:
        with os.fdopen(descriptor, "wb") as stream:
            subprocess.run(
                ["git", "show", f"FETCH_HEAD:{SNAPSHOT}"],
                cwd=repo,
                check=True,
                stdout=stream,
            )
        verify_snapshot(temporary, manifest["sha256"])
        os.replace(temporary, target)
    finally:
        temporary.unlink(missing_ok=True)
    return target


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("action", choices=("publish", "fetch"))
    parser.add_argument("--remote", default="origin")
    parser.add_argument("--database", type=Path, help="Snapshot file to publish")
    args = parser.parse_args()
    if args.action == "publish":
        os.chdir(ROOT)
        if args.database is None:
            from bazi_chi_bot.config import Settings

            database = Settings().database_path
        else:
            database = args.database
        changed = publish(ROOT, database, remote=args.remote)
        print("Server snapshot pushed." if changed else "Server snapshot is unchanged.")
    else:
        if args.database is not None:
            parser.error("--database is only valid for publish")
        print(f"Snapshot downloaded to {fetch(ROOT, remote=args.remote)}")


if __name__ == "__main__":
    main()
