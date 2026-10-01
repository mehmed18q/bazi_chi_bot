#!/usr/bin/env bash
# Deploy the latest origin/master to the systemd installation described in DEPLOYMENT.md.
set -Eeuo pipefail
export LC_MESSAGES=C

service_name="bazi_chi_bot.service"
unit_path="/etc/systemd/system/$service_name"
backup_parent="/var/lib/data/backup"
script_dir="$(CDPATH= cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd -P)"
repo_dir="$(dirname -- "$script_dir")"
backup_dir=""
database_backup_path=""
database_backup_ready=0
previous_commit=""
service_touched=0
was_active=0
unit_changed=0
redeploy_current=0
dependency_install_started=0

log() { printf '%s\n' "$*"; }
die() { printf 'Error: %s\n' "$*" >&2; exit 1; }

as_root() {
    if (( EUID == 0 )); then
        "$@"
    else
        sudo env LC_MESSAGES=C "$@"
    fi
}

install_dependencies() {
    if [[ -x .venv/bin/pip ]]; then
        .venv/bin/pip install -e '.[test,outreach]'
    else
        uv pip install --python .venv/bin/python -e '.[test,outreach]'
    fi
}

on_exit() {
    local exit_code="$1"
    local current_commit=""
    trap - EXIT
    if (( exit_code == 0 )); then
        return
    fi
    set +e
    if (( service_touched == 1 )); then
        current_commit="$(git rev-parse HEAD 2>/dev/null)"
        if (( redeploy_current == 0 && dependency_install_started == 0 )) &&
           [[ -n "$previous_commit" && "$current_commit" == "$previous_commit" &&
              -z "$(git status --porcelain 2>/dev/null)" ]]; then
            if (( was_active == 1 )); then
                log "The code was not updated; attempting to restart the previous service..." >&2
                as_root systemctl start "$service_name" ||
                    log "Could not restart the previous service; check its status and logs." >&2
            else
                log "The service was inactive before deployment and remains stopped." >&2
            fi
        else
            as_root systemctl stop "$service_name" >/dev/null 2>&1 || true
            log "The service remains stopped; restore the code and database as described in DEPLOYMENT.md." >&2
        fi
    fi
    if [[ -n "$backup_dir" ]]; then
        log "Deployment metadata backup: $backup_dir" >&2
    fi
    if [[ -n "$database_backup_path" ]]; then
        if (( database_backup_ready == 1 )); then
            log "Database backup: $database_backup_path" >&2
        else
            log "Database backup target (may be incomplete): $database_backup_path" >&2
        fi
    fi
}
trap 'on_exit $?' EXIT

if (( $# != 0 )); then
    if (( $# == 1 )) && [[ "$1" == "--help" || "$1" == "-h" ]]; then
        log "Usage: bash scripts/deploy.sh"
        log "Deploy origin/master to $service_name. See DEPLOYMENT.md for details."
        exit 0
    fi
    die "This script takes no arguments. Use --help for usage."
fi

cd -- "$repo_dir"
[[ -d .git ]] || die "The project directory is not a standard Git repository: $repo_dir"
for command_name in git systemctl install mktemp; do
    command -v "$command_name" >/dev/null || die "Required command not found: $command_name"
done
if (( EUID != 0 )); then
    command -v sudo >/dev/null || die "Required command not found: sudo"
    sudo -v || die "sudo access is required to control the service and create backups."
fi
[[ "$(git branch --show-current)" == "master" ]] ||
    die "Check out the master branch before deploying."
[[ -z "$(git status --porcelain)" ]] ||
    die "The working tree has local changes or untracked files; review them first."
[[ -x .venv/bin/python ]] || die "The .venv Python interpreter is not ready."
if [[ ! -x .venv/bin/pip ]]; then
    command -v uv >/dev/null ||
        die "Neither .venv/bin/pip nor uv is available to install dependencies."
fi
.venv/bin/python -c 'import sys; raise SystemExit(sys.version_info < (3, 14))' ||
    die "The virtual environment requires Python 3.14 or newer."
as_root test -f .env || die "The .env file is missing."
as_root test -f "$unit_path" || die "The systemd unit file was not found: $unit_path"
if as_root systemctl is-active --quiet "$service_name"; then
    was_active=1
else
    log "Warning: The service is inactive; deployment will attempt to start it after updating."
fi

previous_commit="$(git rev-parse HEAD)"
log "Fetching the latest version from origin/master..."
git fetch origin master
target_commit="$(git rev-parse FETCH_HEAD)"
git merge-base --is-ancestor "$previous_commit" "$target_commit" ||
    die "The local master branch cannot be fast-forwarded to origin/master."
git cat-file -e "$target_commit:pyproject.toml" ||
    die "The target revision does not contain pyproject.toml."
git cat-file -e "$target_commit:bazi_chi_bot.service" ||
    die "The target revision does not contain the systemd unit file."
if [[ "$previous_commit" == "$target_commit" ]]; then
    redeploy_current=1
    log "master is already up to date; redeploying the current revision."
else
    log "Revision: $previous_commit -> $target_commit"
    git diff --stat "$previous_commit" "$target_commit"
fi

if ! git diff --quiet "$previous_commit" "$target_commit" -- bazi_chi_bot.service; then
    unit_changed=1
    as_root cmp -s "$unit_path" bazi_chi_bot.service ||
        die "The installed unit file is customized; review the unit update manually."
    [[ -z "$(as_root systemctl show -p DropInPaths --value "$service_name")" ]] ||
        die "The service has drop-ins; review the unit update manually."
fi

log "Stopping the service and creating a backup..."
service_touched=1
as_root systemctl stop "$service_name"
if as_root systemctl is-active --quiet "$service_name"; then
    die "The service is still active after systemctl stop."
fi
umask 077
if ! as_root test -d "$backup_parent"; then
    as_root install -d -m 0700 "$backup_parent"
fi
backup_stamp="$(date -u +%Y-%m-%dT%H-%M-%S.%NZ)"
backup_dir="$(as_root mktemp -d "$backup_parent/deploy-${backup_stamp}.XXXXXX")"
database_backup_path="$backup_parent/bazi_chi_bot(${backup_stamp}).sqlite3"
printf '%s\n' "$previous_commit" | as_root tee "$backup_dir/previous-commit.txt" >/dev/null
as_root chmod 0600 "$backup_dir/previous-commit.txt"
as_root install -m 0600 .env "$backup_dir/.env"
as_root install -m 0600 "$unit_path" "$backup_dir/$service_name"
as_root env BOT_BACKUP_PATH="$database_backup_path" .venv/bin/python - <<'PY'
import os
import sqlite3
from contextlib import closing
from pathlib import Path

from bazi_chi_bot.config import Settings

source_path = Settings().database_path.resolve()
if not source_path.is_file():
    raise SystemExit(f"Database not found: {source_path}")
backup_path = Path(os.environ["BOT_BACKUP_PATH"])
try:
    descriptor = os.open(backup_path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
except FileExistsError:
    raise SystemExit(f"Database backup already exists: {backup_path}") from None
else:
    os.close(descriptor)
with closing(sqlite3.connect(source_path.as_uri() + "?mode=ro", uri=True)) as source:
    with closing(sqlite3.connect(backup_path)) as backup:
        source.backup(backup)
        result = backup.execute("PRAGMA integrity_check").fetchone()[0]
        if result != "ok":
            raise SystemExit(f"Backup integrity check failed: {result}")
backup_path.chmod(0o600)
print(f"Database backup OK: {backup_path}")
PY
database_backup_ready=1
printf '%s\n' "$database_backup_path" | as_root tee "$backup_dir/database-backup-path.txt" >/dev/null
as_root chmod 0600 "$backup_dir/database-backup-path.txt"

log "Applying the update and installing dependencies..."
if (( redeploy_current == 0 )); then
    git merge --ff-only "$target_commit"
fi
dependency_install_started=1
install_dependencies

log "Running tests and checking the configured question bank..."
.venv/bin/pytest -q --ignore=tests/test_question_bank.py
as_root .venv/bin/python - <<'PY'
import sqlite3
from contextlib import closing

from bazi_chi_bot.config import Settings

database_path = Settings().database_path.resolve()
with closing(sqlite3.connect(database_path.as_uri() + "?mode=ro", uri=True)) as database:
    counts = dict(database.execute(
        "SELECT kind, COUNT(*) FROM questions WHERE active = 1 GROUP BY kind"
    ).fetchall())
    empty_count = database.execute(
        "SELECT COUNT(*) FROM questions WHERE text IS NULL OR trim(text) = ''"
    ).fetchone()[0]
if counts.get("truth", 0) < 1 or counts.get("dare", 0) < 1 or empty_count:
    raise SystemExit(f"Question bank invalid: active={counts}, empty={empty_count}")
print(f"Question bank OK: active={counts}, empty={empty_count}")
PY

if (( unit_changed == 1 )); then
    log "Installing the updated systemd unit..."
    as_root install -m 0644 bazi_chi_bot.service "$unit_path"
    as_root systemctl daemon-reload
fi

log "Starting the service..."
as_root systemctl start "$service_name"
sleep 3
as_root systemctl is-active --quiet "$service_name" || die "The service did not remain active after starting."
as_root journalctl -u "$service_name" -n 30 --no-pager ||
    log "Warning: Could not read the logs; check the service status separately."
if (( redeploy_current == 1 )); then
    log "Redeployment completed: $target_commit"
else
    log "Deployment completed: $previous_commit -> $target_commit"
fi
log "Deployment metadata backup: $backup_dir"
log "Database backup: $database_backup_path"
log "Also verify /start and a game interaction in Telegram."
