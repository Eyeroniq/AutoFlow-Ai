#!/bin/bash
# Database backups for the production stack (the `backup` service in compose.prod.yaml runs this).
#
#   backup.sh loop    run forever: one backup a day at BACKUP_HOUR_UTC (the service's default command)
#   backup.sh once    take one backup now and exit
#
# Each backup is a pg_dump in PostgreSQL's custom format (compressed, restorable with pg_restore, even a
# single table) at $BACKUP_DIR/flowforge-YYYYmmdd-HHMMSS.dump, checked by listing its contents before it is
# kept. Unless BACKUP_FILES=false, the uploaded files (/data/files) are archived next to it as
# flowforge-files-YYYYmmdd-HHMMSS.tar.gz. Backups older than BACKUP_KEEP_DAYS are deleted.
#
# Configuration (environment): PGHOST PGUSER PGPASSWORD PGDATABASE (set by Compose), BACKUP_DIR (/backups),
# BACKUP_HOUR_UTC (3), BACKUP_KEEP_DAYS (14), BACKUP_FILES (true), BACKUP_ON_START (false).
set -euo pipefail

BACKUP_DIR="${BACKUP_DIR:-/backups}"
KEEP_DAYS="${BACKUP_KEEP_DAYS:-14}"
HOUR="${BACKUP_HOUR_UTC:-3}"
FILES_DIR="${BACKUP_FILES_DIR:-/data/files}"

log() { echo "$(date -u +%Y-%m-%dT%H:%M:%SZ) backup: $*"; }

backup_once() {
  mkdir -p "$BACKUP_DIR"
  local stamp out tmp
  stamp="$(date -u +%Y%m%d-%H%M%S)"
  out="$BACKUP_DIR/flowforge-$stamp.dump"
  tmp="$out.partial"
  log "dumping database '$PGDATABASE' on $PGHOST"
  pg_dump --format=custom --no-owner --file="$tmp" "$PGDATABASE"
  # An archive that can't be listed is not a backup: refuse to keep it.
  if ! pg_restore --list "$tmp" > /dev/null; then
    rm -f "$tmp"
    log "ERROR: the dump could not be read back; nothing was kept"
    return 1
  fi
  mv "$tmp" "$out"
  log "wrote $out ($(du -h "$out" | cut -f1))"

  if [ "${BACKUP_FILES:-true}" = "true" ] && [ -d "$FILES_DIR" ]; then
    local archive="$BACKUP_DIR/flowforge-files-$stamp.tar.gz"
    tar -czf "$archive.partial" -C "$FILES_DIR" . && mv "$archive.partial" "$archive"
    log "wrote $archive ($(du -h "$archive" | cut -f1))"
  fi

  find "$BACKUP_DIR" -maxdepth 1 \( -name 'flowforge-*.dump' -o -name 'flowforge-files-*.tar.gz' \) -mtime "+$KEEP_DAYS" -print -delete \
    | sed 's/^/backup: removed old /'
}

seconds_until_next_run() {
  local now target
  now="$(date -u +%s)"
  target="$(date -u -d "today $(printf '%02d' "$HOUR"):00" +%s)"
  [ "$target" -le "$now" ] && target=$((target + 86400))
  echo $((target - now))
}

case "${1:-loop}" in
  once)
    backup_once
    ;;
  loop)
    log "daily at $(printf '%02d' "$HOUR"):00 UTC, keeping $KEEP_DAYS days, into $BACKUP_DIR"
    if [ "${BACKUP_ON_START:-false}" = "true" ]; then backup_once || log "first backup failed; will retry at the next run"; fi
    while true; do
      wait="$(seconds_until_next_run)"
      log "next backup in ${wait}s"
      sleep "$wait"
      backup_once || log "backup failed; will retry at the next run"
    done
    ;;
  *)
    echo "usage: $0 [loop|once]" >&2
    exit 2
    ;;
esac
