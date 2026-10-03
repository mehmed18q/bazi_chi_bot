"""The server database travels through Git without joining the code branch."""

import importlib.util
import os
import shlex
import shutil
import sqlite3
import subprocess
import sys
from pathlib import Path

import pytest


def load_script():
    path = Path(__file__).parents[1] / "scripts/server_data.py"
    spec = importlib.util.spec_from_file_location("server_data", path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def git(repo: Path, *args: str) -> str:
    return subprocess.run(
        ["git", *args],
        cwd=repo,
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()


def test_server_publishes_and_local_fetches_without_touching_code_or_live_data(tmp_path):
    script = load_script()
    remote = tmp_path / "remote.git"
    remote.mkdir()
    git(remote, "init", "--bare", "--initial-branch=master")
    server = tmp_path / "server"
    server.mkdir()
    git(server, "init", "--initial-branch=master")
    git(server, "config", "user.name", "Test")
    git(server, "config", "user.email", "test@example.invalid")
    (server / "README").write_text("code branch\n")
    git(server, "add", "README")
    git(server, "commit", "-m", "Initial code")
    git(server, "remote", "add", "origin", str(remote))
    git(server, "push", "origin", "master")
    code_head = git(server, "rev-parse", "HEAD")

    live = tmp_path / "server-live.sqlite3"
    with sqlite3.connect(live) as database:
        database.execute("PRAGMA journal_mode=WAL")
        database.execute("CREATE TABLE events (name TEXT)")
        database.execute("INSERT INTO events VALUES ('server')")
    assert live.with_name(live.name + "-wal").exists()
    assert script.publish(server, live)
    assert not script.publish(server, live)
    assert git(server, "rev-parse", "HEAD") == code_head
    assert git(server, "status", "--porcelain") == ""
    assert git(remote, "rev-parse", "refs/heads/master") == code_head

    local = tmp_path / "local"
    git(tmp_path, "clone", str(remote), str(local))
    active = local / "data/bazi_chi_bot.sqlite3"
    active.parent.mkdir()
    with sqlite3.connect(active) as database:
        database.execute("CREATE TABLE events (name TEXT)")
        database.execute("INSERT INTO events VALUES ('local')")
    assert script.fetch(local) == local / "data/server_snapshot.sqlite3"
    with sqlite3.connect(local / "data/server_snapshot.sqlite3") as database:
        assert database.execute("SELECT name FROM events").fetchall() == [("server",)]
    with sqlite3.connect(active) as database:
        assert database.execute("SELECT name FROM events").fetchall() == [("local",)]
    assert git(local, "rev-parse", "HEAD") == code_head
    assert script.DATA_BRANCH not in git(local, "branch", "--list")

    with sqlite3.connect(live) as database:
        database.execute("INSERT INTO events VALUES ('new on server')")
    assert script.publish(server, live)
    script.fetch(local)
    with sqlite3.connect(local / "data/server_snapshot.sqlite3") as database:
        assert database.execute("SELECT name FROM events").fetchall() == [
            ("server",),
            ("new on server",),
        ]
    assert git(remote, "rev-parse", "refs/heads/master") == code_head


def test_publish_refuses_database_inside_code_checkout(tmp_path):
    script = load_script()
    repo = tmp_path / "code"
    repo.mkdir()
    database = repo / "data/bazi_chi_bot.sqlite3"
    database.parent.mkdir()
    database.touch()
    with pytest.raises(ValueError, match="outside the Git checkout"):
        script.publish(repo, database)


def test_publish_wrapper_exports_live_database_and_cleans_temporary_copy(tmp_path):
    remote = tmp_path / "remote.git"
    remote.mkdir()
    git(remote, "init", "--bare", "--initial-branch=master")
    server = tmp_path / "server"
    scripts = server / "scripts"
    scripts.mkdir(parents=True)
    source_scripts = Path(__file__).parents[1] / "scripts"
    for name in ("server_data.py", "publish_server_data.sh"):
        shutil.copy2(source_scripts / name, scripts / name)
    python = server / ".venv/bin/python"
    python.parent.mkdir(parents=True)
    python.write_text(f'#!/bin/sh\nexec {shlex.quote(sys.executable)} "$@"\n')
    python.chmod(0o755)
    git(server, "init", "--initial-branch=master")
    git(server, "config", "user.name", "Test")
    git(server, "config", "user.email", "test@example.invalid")
    git(server, "add", "scripts")
    git(server, "commit", "-m", "Code")
    git(server, "remote", "add", "origin", str(remote))
    git(server, "push", "origin", "master")

    live = tmp_path / "live.sqlite3"
    with sqlite3.connect(live) as database:
        database.execute("CREATE TABLE events (name TEXT)")
        database.execute("INSERT INTO events VALUES ('from server')")
    (server / ".env").write_text(f"BOT_TOKEN={'x' * 30}\nDATABASE_PATH={live}\n")
    fake_bin = tmp_path / "bin"
    fake_bin.mkdir()
    fake_sudo = fake_bin / "sudo"
    fake_sudo.write_text('#!/bin/sh\nif [ "$1" = "-v" ]; then exit 0; fi\nexec "$@"\n')
    fake_sudo.chmod(0o755)
    temp_dir = tmp_path / "private-temp"
    temp_dir.mkdir()
    environment = os.environ | {
        "PATH": f"{fake_bin}:{os.environ['PATH']}",
        "TMPDIR": str(temp_dir),
    }
    result = subprocess.run(
        ["bash", "scripts/publish_server_data.sh"],
        cwd=server,
        env=environment,
        check=True,
        capture_output=True,
        text=True,
    )
    assert "Server snapshot pushed." in result.stdout
    assert list(temp_dir.iterdir()) == []
    local = tmp_path / "local"
    git(tmp_path, "clone", str(remote), str(local))
    load_script().fetch(local)
    with sqlite3.connect(local / "data/server_snapshot.sqlite3") as database:
        assert database.execute("SELECT name FROM events").fetchall() == [("from server",)]

    git(server, "remote", "set-url", "origin", str(tmp_path / "missing.git"))
    failed = subprocess.run(
        ["bash", "scripts/publish_server_data.sh"],
        cwd=server,
        env=environment,
        check=False,
        capture_output=True,
        text=True,
    )
    assert failed.returncode != 0
    assert list(temp_dir.iterdir()) == []
