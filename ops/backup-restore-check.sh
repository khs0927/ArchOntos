#!/usr/bin/env bash
# dump -> restore -> row-count check against throwaway databases on one server (CI and local).
#
#   ARCHONTOS_TEST_DATABASE_URL=postgresql://user:pass@host:5432/postgres ops/backup-restore-check.sh
#
# Needs a role that can CREATE DATABASE. Migrates a fresh source DB with the real migration
# runner, seeds a few rows, backs it up, restores into an empty DB and compares every table count.
set -euo pipefail
admin="${ARCHONTOS_TEST_DATABASE_URL:?set ARCHONTOS_TEST_DATABASE_URL}"
admin="${admin/postgresql+asyncpg:\/\//postgresql://}"
here="$(cd "$(dirname "$0")" && pwd)"
suffix="$$_$RANDOM"
src="archontos_bk_src_${suffix}"; dst="archontos_bk_dst_${suffix}"
work="$(mktemp -d)"
with_db() { printf '%s' "$admin" | sed -E "s#^(postgresql://[^/]*)/[^?]*#\1/$1#"; }
cleanup() {
  psql --dbname "$admin" -XAtq -c "DROP DATABASE IF EXISTS \"$src\" WITH (FORCE)" || true
  psql --dbname "$admin" -XAtq -c "DROP DATABASE IF EXISTS \"$dst\" WITH (FORCE)" || true
  rm -rf "$work"
}
trap cleanup EXIT

psql --dbname "$admin" -XAtq -v ON_ERROR_STOP=1 -c "CREATE DATABASE \"$src\"" -c "CREATE DATABASE \"$dst\""
python -m archontos.db.migrate --dsn "$(with_db "$src")" > /dev/null
psql --dbname "$(with_db "$src")" -XAtq -v ON_ERROR_STOP=1 <<'SQL'
BEGIN;
SET LOCAL app.allowed_jurisdictions = 'KR';
INSERT INTO jurisdiction (code, name, level) VALUES ('KR', 'Korea', 'national') ON CONFLICT DO NOTHING;
INSERT INTO source_document (source_key, title, issuer, jurisdiction_code, document_type)
SELECT 'bk-' || g, 'backup check ' || g, 'test', 'KR', 'statute'
FROM generate_series(1, 25) g;
COMMIT;
SQL

ARCHONTOS_BACKUP_DSN="$(with_db "$src")" "$here/backup.sh" "$work" > /dev/null
dump="$(ls "$work"/*.dump)"
"$here/restore.sh" "$dump" "$(with_db "$dst")"
