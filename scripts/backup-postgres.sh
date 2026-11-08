#!/usr/bin/env bash
set -euo pipefail
umask 077

usage() {
    echo "Usage: $0 --directory PATH [--production | --test-db NAME]" >&2
    exit 2
}

fail() {
    echo "$1" >&2
    exit 1
}

destination=
production=false
test_db=
while (( $# )); do
    case "$1" in
        --directory)
            (( $# >= 2 )) && [[ -n "$2" && -z "$destination" ]] || usage
            destination=$2
            shift 2
            ;;
        --production)
            [[ "$production" == false ]] || usage
            production=true
            shift
            ;;
        --test-db)
            (( $# >= 2 )) && [[ -n "$2" && -z "$test_db" ]] || usage
            test_db=$2
            shift 2
            ;;
        *) usage ;;
    esac
done
[[ -n "$destination" ]] || usage

root=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd -P)
compose=(docker compose --project-directory "$root")
if [[ -n "$test_db" ]]; then
    [[ "$production" == false && "$test_db" =~ ^test_enactus_restore_source_([0-9a-f]{12})$ ]] ||
        fail "Test backup requires a restore verification source database."
    expected_project="enactus_restore_verify_${BASH_REMATCH[1]}"
    [[ "${COMPOSE_PROJECT_NAME:-}" == "$expected_project" && -f "${COMPOSE_FILE:-}" ]] ||
        fail "Test backup requires the matching isolated Compose project and file."
    compose+=(--project-name "$expected_project" --file "$COMPOSE_FILE")
elif [[ "$production" == true ]]; then
    compose+=(--project-name enactus_os_next_prod --env-file "$root/.env.production" --file "$root/compose.production.yaml")
else
    compose+=(--env-file "$root/.env" --file "$root/compose.yaml")
fi

mkdir -p -- "$destination"
destination=$(cd -- "$destination" && pwd -P)
[[ -O "$destination" && "$(stat -c '%a' -- "$destination")" == 700 ]] ||
    fail "Backup directory must be owned by the current user and have mode 0700."

timestamp=$(date -u +'%Y%m%dT%H%M%SZ')
staging=$(mktemp -d -- "$destination/.enactus-postgres-$timestamp-XXXXXXXX")
backup_name="${staging##*/}"
backup_name="${backup_name#.}"
archive="$backup_name.dump"
cleanup() {
    rm -f -- "$staging/$archive" "$staging/$archive.sha256"
    rmdir -- "$staging"
}
trap cleanup EXIT
trap 'exit 130' INT
trap 'exit 143' TERM

"${compose[@]}" exec -T db sh -eu -c '
    target="$POSTGRES_DB"
    if [ -n "$1" ]; then
        if [ "$POSTGRES_DB" != postgres ] || [ "$1" = "$POSTGRES_DB" ]; then
            echo "Test backup requires the isolated maintenance database." >&2
            exit 1
        fi
        target="$1"
    fi
    case "$target" in
        ""|*[!a-zA-Z0-9_-]*)
            echo "PostgreSQL database must be a plain local database name." >&2
            exit 1
            ;;
    esac
    export PGPASSWORD="$POSTGRES_PASSWORD"
    exec pg_dump --format=custom --host=127.0.0.1 --no-password \
        --username="$POSTGRES_USER" --dbname="$target"
' sh "$test_db" > "$staging/$archive"

"${compose[@]}" exec -T db pg_restore --exit-on-error --file=/dev/null < "$staging/$archive"
(
    cd -- "$staging"
    sha256sum -- "$archive" > "$archive.sha256"
)
mv -T -- "$staging" "$destination/$backup_name"
trap - EXIT
printf '%s\n' "$destination/$backup_name/$archive"
