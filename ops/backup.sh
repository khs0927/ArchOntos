#!/usr/bin/env bash
# Logical backup of the ArchOntos canonical database (custom-format pg_dump).
#
#   ops/backup.sh [OUT_DIR]            # default OUT_DIR=./backups
#
# Connection: ARCHONTOS_BACKUP_DSN (postgresql://user:pass@host:5432/db) or the standard
# PG* variables (PGHOST, PGPORT, PGUSER, PGDATABASE, PGPASSWORD / ~/.pgpass). Never put the
# password on the command line. Writes, next to each other:
#   archontos-<UTC timestamp>.dump        pg_dump -Fc
#   archontos-<UTC timestamp>.counts.tsv  row count per table (used by restore.sh --verify)
#   archontos-<UTC timestamp>.sha256      checksum of the dump
# Run it as the migrator/owner role, BEFORE every migration run (see ops/RUNBOOK.md).
set -euo pipefail

out_dir="${1:-./backups}"
dsn="${ARCHONTOS_BACKUP_DSN:-}"
stamp="$(date -u +%Y%m%dT%H%M%SZ)"
base="${out_dir}/archontos-${stamp}"
mkdir -p "$out_dir"

pg_args=()
[ -n "$dsn" ] && pg_args=(--dbname "$dsn")

# Row counts per user table, exact (count(*)), so a restore can be checked against them.
# One UNION ALL statement = one snapshot.
counts_sql="$(cat <<'SQL'
SELECT string_agg(
  format('SELECT %L AS t, count(*) AS n FROM %I.%I', schemaname || '.' || tablename, schemaname, tablename),
  ' UNION ALL ' ORDER BY schemaname, tablename)
FROM pg_tables
WHERE schemaname NOT IN ('pg_catalog', 'information_schema')
SQL
)"
query="$(psql "${pg_args[@]}" -XAtq -c "$counts_sql")"
: > "${base}.counts.tsv"
if [ -n "$query" ]; then
  psql "${pg_args[@]}" -XAtq -F $'	' -c "$query" > "${base}.counts.tsv"
fi

pg_dump "${pg_args[@]}" --format=custom --no-owner --compress=6 --file "${base}.dump"
( cd "$out_dir" && sha256sum "$(basename "${base}.dump")" > "$(basename "${base}.sha256")" )
echo "backup: ${base}.dump ($(wc -c < "${base}.dump") bytes, $(wc -l < "${base}.counts.tsv") tables)"
