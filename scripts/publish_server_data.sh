#!/usr/bin/env bash
# Read the live SQLite database as root, then push its snapshot with the deploy user's Git credentials.
set -Eeuo pipefail
export LC_MESSAGES=C
umask 077

script_dir="$(CDPATH= cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd -P)"
repo_dir="$(dirname -- "$script_dir")"
snapshot_path=""

cleanup() {
    local exit_code="$?"
    trap - EXIT
    set +e
    if [[ -n "$snapshot_path" ]]; then
        rm -f -- "$snapshot_path" ||
            printf 'Could not remove temporary snapshot: %s\n' "$snapshot_path" >&2
    fi
    exit "$exit_code"
}
trap cleanup EXIT

as_root() {
    if (( EUID == 0 )); then
        "$@"
    else
        sudo env LC_MESSAGES=C "$@"
    fi
}

if (( $# != 0 )); then
    printf 'Usage: bash scripts/publish_server_data.sh\n' >&2
    exit 2
fi
cd -- "$repo_dir"
[[ -d .git && -x .venv/bin/python ]] || {
    printf 'The Git checkout or virtual environment is missing.\n' >&2
    exit 1
}
if (( EUID != 0 )); then
    sudo -v
fi

snapshot_path="$(mktemp "${TMPDIR:-/tmp}/bazi-server-data.XXXXXXXX.sqlite3")"
as_root env BOT_PUBLISH_SNAPSHOT="$snapshot_path" BOT_REPO_DIR="$repo_dir" .venv/bin/python - <<'PY'
import os
import sqlite3
from contextlib import closing
from pathlib import Path

from bazi_chi_bot.config import Settings

source_path = Settings().database_path.resolve()
repo_path = Path(os.environ["BOT_REPO_DIR"]).resolve()
snapshot_path = Path(os.environ["BOT_PUBLISH_SNAPSHOT"])
if source_path.is_relative_to(repo_path) or not source_path.is_file():
    raise SystemExit("Live database must exist outside the Git checkout")
with (
    closing(sqlite3.connect(source_path.as_uri() + "?mode=ro", uri=True)) as source,
    closing(sqlite3.connect(snapshot_path)) as snapshot,
):
    source.backup(snapshot)
    if snapshot.execute("PRAGMA integrity_check").fetchone()[0] != "ok":
        raise SystemExit("Publish snapshot failed integrity_check")
PY

.venv/bin/python scripts/server_data.py publish --database "$snapshot_path"
