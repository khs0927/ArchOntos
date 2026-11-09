#!/usr/bin/env bash
# ArchOntos restore + verification into a TARGET database, using a backup made by backup.sh.
#
#   restore_verify.sh BACKUP_DIR [--force-nonempty] [--verify-only] [--skip-artifacts]
#
# Connection settings come from libpq environment variables (PGHOST, PGPORT, PGUSER, PGDATABASE,
# PGPASSWORD or PGPASSFILE) and must be the MIGRATOR role (owns the schema, has CREATEROLE).
# No secret is accepted as an argument or printed. Optional:
#   ARCHONTOS_SERVICE_DSN         service-login DSN; verify_restore runs as it (least privilege).
#                                 Default: the PG* connection (then audit_log grants are not
#                                 meaningful because a superuser holds every privilege).
#   ARCHONTOS_ARTIFACT_RESTORE_PATH  where to unpack local artifacts (default: artifact local path)
#   ARCHONTOS_APP_DB_PASSWORD + ARCHONTOS_SERVICE_LOGIN  re-provision the service login after restore
#
# Refuses to restore into a database that already has tables unless --force-nonempty is given.
set -euo pipefail
umask 077

log() { printf '%s %s\n' "$(date -u +%FT%TZ)" "$*" >&2; }
die() { log "ERROR: $*"; exit 1; }

backup=""; force=0; verify_only=0; skip_artifacts=0
for arg in "$@"; do
  case "${arg}" in
    --force-nonempty) force=1 ;;
    --verify-only) verify_only=1 ;;
    --skip-artifacts) skip_artifacts=1 ;;
    -*) die "unknown flag ${arg}" ;;
    *) [ -z "${backup}" ] || die "only one BACKUP_DIR"; backup="${arg}" ;;
  esac
done
[ -n "${backup}" ] || die "usage: restore_verify.sh BACKUP_DIR [--force-nonempty] [--verify-only] [--skip-artifacts]"
: "${PGDATABASE:?set PGDATABASE (and PGHOST/PGUSER, PGPASSWORD or PGPASSFILE)}"
backend="${ARCHONTOS_ARTIFACT_BACKEND:-local}"
art_root="${ARCHONTOS_ARTIFACT_RESTORE_PATH:-${ARCHONTOS_ARTIFACT_LOCAL_PATH:-local-data/artifacts}}"

psql_q() { psql --no-psqlrc --quiet --tuples-only --no-align --set ON_ERROR_STOP=1 "$@"; }

if [ "${verify_only}" -eq 0 ]; then
  [ -f "${backup}/db.dump" ] || die "${backup}/db.dump not found"
  log "checking backup integrity (SHA256SUMS)"
  (cd "${backup}" && sha256sum --check --quiet SHA256SUMS) || die "backup checksum mismatch"

  tables="$(psql_q -c "SELECT count(*) FROM pg_tables WHERE schemaname = current_schema()")"
  if [ "${tables}" != "0" ] && [ "${force}" -ne 1 ]; then
    die "target database ${PGDATABASE} already has ${tables} table(s); refusing (use --force-nonempty to override)"
  fi

  log "ensuring group role archontos_app exists (cluster-level; not part of the dump)"
  psql_q -c "DO \$\$ BEGIN IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname='archontos_app') THEN CREATE ROLE archontos_app NOLOGIN NOSUPERUSER NOBYPASSRLS NOCREATEDB NOCREATEROLE; END IF; END \$\$"
  psql_q -c "CREATE EXTENSION IF NOT EXISTS vector" -c "CREATE EXTENSION IF NOT EXISTS pgcrypto"

  log "pg_restore ${backup}/db.dump"
  clean=()
  [ "${force}" -eq 1 ] && clean=(--clean --if-exists)
  pg_restore --no-owner --exit-on-error --single-transaction ${clean[@]+"${clean[@]}"} \
    --dbname="${PGDATABASE}" "${backup}/db.dump"

  if [ "${skip_artifacts}" -eq 0 ]; then
    case "${backend}" in
      local)
        [ -f "${backup}/artifacts.tar" ] || die "artifacts.tar missing from backup"
        mkdir -p "${art_root}"
        tar -C "${art_root}" -xf "${backup}/artifacts.tar"
        ;;
      minio)
        command -v mc >/dev/null || die "mc not installed"
        : "${MC_HOST_archontos:?set MC_HOST_archontos in the environment}"
        mc mirror --quiet "${backup}/artifacts" "archontos/${ARCHONTOS_MINIO_BUCKET:-archontos}"
        ;;
      *) die "unknown ARCHONTOS_ARTIFACT_BACKEND=${backend}" ;;
    esac
  fi

  if [ -n "${ARCHONTOS_SERVICE_LOGIN:-}" ]; then
    log "re-provisioning service login ${ARCHONTOS_SERVICE_LOGIN}"
    python -m archontos.db.roles ensure-login --name "${ARCHONTOS_SERVICE_LOGIN}" \
      --dsn "$(printf 'postgresql://%s@%s:%s/%s' "${PGUSER:-}" "${PGHOST:-localhost}" "${PGPORT:-5432}" "${PGDATABASE}")"
  fi
fi

log "row counts (compare with the source: ops/BACKUP-RESTORE.md)"
psql_q -c "
  SELECT 'source_document', count(*) FROM source_document UNION ALL
  SELECT 'source_version', count(*) FROM source_version UNION ALL
  SELECT 'artifact', count(*) FROM artifact UNION ALL
  SELECT 'assertion', count(*) FROM assertion UNION ALL
  SELECT 'decision', count(*) FROM decision UNION ALL
  SELECT 'outbox_message', count(*) FROM outbox_message UNION ALL
  SELECT 'audit_log', count(*) FROM audit_log UNION ALL
  SELECT 'schema_migrations', count(*) FROM schema_migrations
  ORDER BY 1"

log "HNSW index"
python -m archontos.db.vector_index --show --dsn "${ARCHONTOS_SERVICE_DSN:-postgresql://${PGUSER:-}@${PGHOST:-localhost}:${PGPORT:-5432}/${PGDATABASE}}"

log "verify_restore (artifact sha256, migrations, audit_log append-only)"
args=()
[ "${skip_artifacts}" -eq 1 ] && args+=(--skip-artifacts)
args+=(--artifact-backend "${backend}" --artifact-path "${art_root}")
if [ -n "${ARCHONTOS_SERVICE_DSN:-}" ]; then args+=(--dsn "${ARCHONTOS_SERVICE_DSN}"); fi
# Without --dsn the Python tool reads ARCHONTOS_DATABASE_URL; fall back to libpq env via PG*.
if [ -z "${ARCHONTOS_SERVICE_DSN:-}" ] && [ -z "${ARCHONTOS_DATABASE_URL:-}" ]; then
  export ARCHONTOS_DATABASE_URL="postgresql://${PGUSER:-}@${PGHOST:-localhost}:${PGPORT:-5432}/${PGDATABASE}"
fi
python -m archontos.ops.verify_restore "${args[@]}"
log "OK"
