#!/usr/bin/env bash
set -euo pipefail
umask 077

if (( $# )); then
    echo "Usage: bash scripts/verify-postgres-restore.sh" >&2
    exit 2
fi

project_root="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd -P)"
backup_script="$project_root/scripts/backup-postgres.sh"
if [[ ! -f "$backup_script" ]]; then
    echo "Backup script is missing: $backup_script" >&2
    exit 1
fi

suffix="$(python3 -c 'import secrets; print(secrets.token_hex(6))')"
project="enactus_restore_verify_$suffix"
source_db="test_enactus_restore_source_$suffix"
target_db="test_enactus_restore_target_$suffix"
image="$project-web"
workdir="$(mktemp -d "${TMPDIR:-/tmp}/enactus-restore-verify.XXXXXXXX")"
workdir="$(cd -- "$workdir" && pwd -P)"
compose_file="$workdir/compose.yaml"
compose=(docker compose --project-directory "$workdir" --project-name "$project" --file "$compose_file")
owns_project=false

cleanup() {
    local status=$?
    trap - EXIT
    if [[ "$owns_project" == true ]]; then
        if ! "${compose[@]}" down >&2; then
            status=1
        fi
        if docker image inspect "$image" >/dev/null 2>&1; then
            if ! docker image rm "$image" >&2; then
                status=1
            fi
        fi
    fi
    rm -rf -- "$workdir"
    exit "$status"
}
trap cleanup EXIT
trap 'exit 130' INT
trap 'exit 143' TERM

if [[ -n "$(docker ps -aq --filter "label=com.docker.compose.project=$project")" ]] ||
   [[ -n "$(docker network ls -q --filter "label=com.docker.compose.project=$project")" ]] ||
   docker image inspect "$image" >/dev/null 2>&1; then
    echo "Refusing to reuse an existing Docker verification project." >&2
    exit 1
fi

export COMPOSE_PROJECT_NAME="$project" COMPOSE_FILE="$compose_file" COMPOSE_DISABLE_ENV_FILE=1
export RESTORE_VERIFY_CONTEXT="$project_root" RESTORE_VERIFY_PROJECT="$project"
export POSTGRES_DB=postgres POSTGRES_USER=enactus_restore_verify
export POSTGRES_PASSWORD="$(python3 -c 'import secrets; print(secrets.token_hex(24))')"
export DJANGO_SECRET_KEY="$(python3 -c 'import secrets; print(secrets.token_hex(32))')"

cat > "$compose_file" <<'EOF'
services:
  db:
    image: postgres:16-bookworm
    environment:
      POSTGRES_DB: ${POSTGRES_DB:?}
      POSTGRES_USER: ${POSTGRES_USER:?}
      POSTGRES_PASSWORD: ${POSTGRES_PASSWORD:?}
    tmpfs:
      - /var/lib/postgresql/data:rw,size=256m
    healthcheck:
      test: ["CMD-SHELL", 'pg_isready -h 127.0.0.1 -U "$${POSTGRES_USER}" -d postgres']
      interval: 2s
      timeout: 3s
      retries: 30
  web:
    image: ${COMPOSE_PROJECT_NAME:?}-web
    build:
      context: ${RESTORE_VERIFY_CONTEXT:?}
    environment:
      DJANGO_SETTINGS_MODULE: config.settings
      DJANGO_SECRET_KEY: ${DJANGO_SECRET_KEY:?}
      DJANGO_DEBUG: "0"
      POSTGRES_USER: ${POSTGRES_USER:?}
      POSTGRES_PASSWORD: ${POSTGRES_PASSWORD:?}
      POSTGRES_HOST: db
      POSTGRES_PORT: "5432"
      RESTORE_VERIFY_PROJECT: ${RESTORE_VERIFY_PROJECT:?}
EOF

owns_project=true
"${compose[@]}" up --detach --wait --wait-timeout 90 db >&2
db_container="$("${compose[@]}" ps --quiet db)"
if [[ "$(docker inspect --format '{{index .Config.Labels "com.docker.compose.project"}}' "$db_container")" != "$project" ]] ||
   [[ "$(docker inspect --format '{{index .HostConfig.Tmpfs "/var/lib/postgresql/data"}}' "$db_container")" != rw,size=256m ]]; then
    echo "Refusing to run against a database outside the isolated tmpfs container." >&2
    exit 1
fi
"${compose[@]}" build web >&2

postgres() {
    "${compose[@]}" exec -T db "$@"
}

django() {
    local db="$1"
    shift
    if [[ "$db" != "$source_db" && "$db" != "$target_db" ]]; then
        echo "Refusing to run Django against an unexpected database." >&2
        return 1
    fi
    "${compose[@]}" run --rm --no-deps -T -e POSTGRES_DB="$db" web python "$@"
}

fixture() {
    local db="$1" mode="$2"
    django "$db" -c \
        'import runpy, sys; sys.argv = ["verify-postgres-restore-fixture.py", sys.argv[1]]; runpy.run_path("scripts/verify-postgres-restore-fixture.py", run_name="__main__")' \
        "$mode"
}

postgres createdb --no-password --username="$POSTGRES_USER" --maintenance-db=postgres \
    --template=template0 "$source_db"
django "$source_db" manage.py migrate --noinput >&2
fixture "$source_db" seed
fixture "$source_db" snapshot > "$workdir/source.json"

backup_directory="$workdir/backups"
mkdir -m 700 -- "$backup_directory"
dump_path="$(bash "$backup_script" --directory "$backup_directory" --test-db "$source_db")"
dump_directory="$(cd -- "$(dirname -- "$dump_path")" && pwd -P)"
if [[ "$dump_directory" != "$backup_directory/"* || ! -f "$dump_path" || ! -f "$dump_path.sha256" ]]; then
    echo "Backup script returned an unexpected dump or checksum path." >&2
    exit 1
fi
(
    cd -- "$dump_directory"
    sha256sum --check --strict "$(basename -- "$dump_path").sha256"
)

postgres createdb --no-password --username="$POSTGRES_USER" --maintenance-db=postgres \
    --template=template0 "$target_db"
target_tables="$(postgres psql --no-psqlrc --no-password --username="$POSTGRES_USER" \
    --dbname="$target_db" --set=ON_ERROR_STOP=1 --tuples-only --no-align \
    --command="SELECT count(*) FROM pg_tables WHERE schemaname = 'public'")"
if [[ "$target_tables" != 0 ]]; then
    echo "Refusing to restore into a nonempty database." >&2
    exit 1
fi
postgres pg_restore --no-password --username="$POSTGRES_USER" --dbname="$target_db" \
    --no-owner --no-privileges --exit-on-error < "$dump_path"
django "$target_db" manage.py migrate --check --noinput
django "$target_db" manage.py check
fixture "$target_db" snapshot > "$workdir/restored.json"

if ! cmp -s -- "$workdir/source.json" "$workdir/restored.json"; then
    diff -u -- "$workdir/source.json" "$workdir/restored.json" || true
    exit 1
fi

django "$target_db" manage.py notification_worker --once
fixture "$target_db" snapshot > "$workdir/replayed.json"
django "$target_db" manage.py notification_worker --once
fixture "$target_db" snapshot > "$workdir/replayed-again.json"
if ! cmp -s -- "$workdir/replayed.json" "$workdir/replayed-again.json"; then
    echo "A second worker process changed the replayed notification data." >&2
    exit 1
fi
python3 - "$workdir/restored.json" "$workdir/replayed.json" <<'PY'
import json
import sys
from pathlib import Path

before, after = (json.loads(Path(path).read_text())["rows"] for path in sys.argv[1:])
pending = [event for event in before["notification_event"] if event["delivered_at"] is None]
if len(pending) != 1 or pending[0]["delivery_attempts"] != 1:
    raise SystemExit("Expected one retryable outbox event before worker replay.")
event_id = pending[0]["id"]
delivered = next(event for event in after["notification_event"] if event["id"] == event_id)
if (
    delivered["delivered_at"] is None or delivered["delivery_attempts"] != 2
    or delivered["next_attempt_at"] is not None or delivered["last_error"]
    or any(row["event_id"] == event_id for row in before["notification"])
    or sum(row["event_id"] == event_id for row in after["notification"]) != 1
    or len(after["notification"]) != len(before["notification"]) + 1
):
    raise SystemExit("Worker replay did not deliver the restored event exactly once.")
print("Restored pending notification delivered once across two worker processes.")
PY

python3 -c 'import json, sys; counts = json.load(open(sys.argv[1]))["counts"]; print(json.dumps(counts, sort_keys=True))' \
    "$workdir/restored.json"
echo "Restore verified from $source_db to $target_db in isolated project $project."
