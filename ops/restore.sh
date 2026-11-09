#!/usr/bin/env bash
# Restore a backup made by ops/backup.sh into a TARGET database and verify row counts.
#
#   ops/restore.sh BACKUP.dump TARGET_DSN [--clean]
#
# TARGET_DSN is a postgresql:// URL for an EXISTING database (create it first, owner = migrator).
# Refuses to touch a database that already has user tables unless --clean is given (then
# pg_restore --clean --if-exists drops and recreates the dumped objects). Pre-creates the
# `archontos_app` group role when missing (roles are cluster-wide and not part of a dump) so the
# dumped GRANTs apply. The LOGIN role is provisioned afterwards:
#   python -m archontos.db.roles ensure-login --dsn <migrator dsn>
# Exit codes: 0 ok, 2 usage / refusal, 3 checksum mismatch, 4 row-count mismatch.
set -euo pipefail

dump="${1:-}"; target="${2:-}"; flag="${3:-}"
if [ -z "$dump" ] || [ -z "$target" ] || [ ! -f "$dump" ]; then
  echo "usage: restore.sh BACKUP.dump TARGET_DSN [--clean]" >&2; exit 2
fi
base="${dump%.dump}"

if [ -f "${base}.sha256" ]; then
  ( cd "$(dirname "$dump")" && sha256sum --check --quiet "$(basename "${base}.sha256")" ) \
    || { echo "checksum mismatch for $dump" >&2; exit 3; }
fi

existing="$(psql --dbname "$target" -XAtq -c \
  "SELECT count(*) FROM pg_tables WHERE schemaname NOT IN ('pg_catalog','information_schema')")"
if [ "$existing" != "0" ] && [ "$flag" != "--clean" ]; then
  echo "target already has $existing tables; pass --clean to overwrite" >&2; exit 2
fi

psql --dbname "$target" -XAtq -v ON_ERROR_STOP=1 -c "
DO \$\$ BEGIN
  IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'archontos_app') THEN
    CREATE ROLE archontos_app NOLOGIN NOSUPERUSER NOBYPASSRLS NOCREATEDB NOCREATEROLE;
  END IF;
END \$\$;"

clean_args=()
[ "$flag" = "--clean" ] && clean_args=(--clean --if-exists)
pg_restore --dbname "$target" --no-owner --exit-on-error "${clean_args[@]}" "$dump"

if [ -f "${base}.counts.tsv" ]; then
  mapfile -t lines < "${base}.counts.tsv"
  bad=0
  for line in "${lines[@]}"; do
    table="${line%%$'\t'*}"; want="${line##*$'\t'}"
    schema="${table%%.*}"; name="${table#*.}"
    got="$(psql --dbname "$target" -XAtq -c "SELECT count(*) FROM \"$schema\".\"$name\"")"
    if [ "$got" != "$want" ]; then echo "MISMATCH $table: backup=$want restored=$got" >&2; bad=1; fi
  done
  [ "$bad" = 0 ] || exit 4
  echo "restore verified: ${#lines[@]} tables, row counts match"
else
  echo "restore done (no counts file, row counts not verified)"
fi
