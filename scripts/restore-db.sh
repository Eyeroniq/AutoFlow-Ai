#!/usr/bin/env sh
# Restore a database backup into the production stack.
#
#   scripts/restore-db.sh backups/flowforge-20261010-030000.dump
#   scripts/restore-db.sh backups/flowforge-20261010-030000.dump --files backups/flowforge-files-20261010-030000.tar.gz
#
# Replaces the CURRENT database contents with the backup. It stops the app (api, workers, web) first, so
# nothing writes during the restore, restores, and starts them again (the API runs any newer migrations on
# start). Run it from the repo root, on the server. ENV_FILE=... / COMPOSE_FILE=... select non-default files;
# ASSUME_YES=1 skips the confirmation.
set -eu
cd "$(dirname "$0")/.."

DUMP="${1:?usage: scripts/restore-db.sh <backup.dump> [--files <files.tar.gz>]}"
FILES=""
if [ "${2:-}" = "--files" ]; then FILES="${3:?--files needs the archive path}"; fi
[ -f "$DUMP" ] || { echo "no such file: $DUMP" >&2; exit 1; }

COMPOSE="docker compose ${ENV_FILE:+--env-file $ENV_FILE} -f ${COMPOSE_FILE:-compose.prod.yaml}"
APP="api worker-default worker-llm worker-ocr worker-audio beat web"
DB_USER="$($COMPOSE exec -T postgres printenv POSTGRES_USER)"
DB_NAME="$($COMPOSE exec -T postgres printenv POSTGRES_DB)"

echo "This replaces the database '$DB_NAME' with $DUMP."
if [ "${ASSUME_YES:-}" != "1" ]; then
  printf "Type 'restore' to continue: "
  read -r answer
  [ "$answer" = "restore" ] || { echo "cancelled"; exit 1; }
fi

echo "== stopping the app"
$COMPOSE stop $APP

echo "== restoring"
# --clean --if-exists drops each object before recreating it; the extensions are recreated too.
$COMPOSE exec -T postgres pg_restore -U "$DB_USER" -d "$DB_NAME" --clean --if-exists --no-owner < "$DUMP" || {
  # pg_restore exits non-zero on harmless warnings (e.g. an extension that already exists); check the data instead.
  echo "pg_restore reported warnings (above); checking the restored data"
}
TABLES="$($COMPOSE exec -T postgres psql -U "$DB_USER" -d "$DB_NAME" -Atc "select count(*) from information_schema.tables where table_schema='public'")"
USERS="$($COMPOSE exec -T postgres psql -U "$DB_USER" -d "$DB_NAME" -Atc "select count(*) from users" 2>/dev/null || echo "?")"
echo "restored: $TABLES tables, $USERS user(s)"

if [ -n "$FILES" ]; then
  echo "== restoring uploaded files from $FILES"
  $COMPOSE run --rm --no-deps -T -v "$(pwd)/$FILES:/restore/files.tar.gz:ro" --entrypoint sh api -c "tar -xzf /restore/files.tar.gz -C /data/files"
fi

echo "== starting the app"
$COMPOSE up -d $APP
echo "done. Check: curl -fsS https://\$DOMAIN/health"
