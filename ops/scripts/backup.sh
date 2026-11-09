#!/usr/bin/env bash
# ArchOntos backup: Postgres custom-format dump + artifact store snapshot + sha256 manifest.
#
# Connection settings come from libpq environment variables (PGHOST, PGPORT, PGUSER, PGDATABASE)
# and PGPASSWORD or ~/.pgpass / PGPASSFILE. Nothing secret is accepted as an argument or printed.
# The dump role must bypass row-level security (superuser, or a dedicated BYPASSRLS backup login):
# pg_dump refuses to dump a table whose RLS would hide rows from the role (see ops/BACKUP-RESTORE.md).
#
# Environment:
#   ARCHONTOS_BACKUP_DIR          target directory (required; created 0700)
#   ARCHONTOS_ARTIFACT_BACKEND    local (default) | minio
#   ARCHONTOS_ARTIFACT_LOCAL_PATH local artifact root (default local-data/artifacts)
#   ARCHONTOS_MINIO_BUCKET        bucket for minio (default archontos)
#   MC_HOST_archontos             MinIO client alias URL (minio backend; kept in the environment)
set -euo pipefail
umask 077

: "${ARCHONTOS_BACKUP_DIR:?set ARCHONTOS_BACKUP_DIR}"
: "${PGDATABASE:?set PGDATABASE (and PGHOST/PGUSER, PGPASSWORD or PGPASSFILE)}"
backend="${ARCHONTOS_ARTIFACT_BACKEND:-local}"
stamp="$(date -u +%Y%m%dT%H%M%SZ)"
out="${ARCHONTOS_BACKUP_DIR}/${stamp}"
mkdir -p "${out}"

log() { printf '%s %s\n' "$(date -u +%FT%TZ)" "$*" >&2; }

log "pg_dump -> ${out}/db.dump (database ${PGDATABASE})"
# --no-owner: the restoring role becomes the owner. ACLs and RLS policies stay in the dump.
pg_dump --format=custom --no-owner --file="${out}/db.dump"
pg_restore --list "${out}/db.dump" >"${out}/db.toc"   # fails if the archive is unreadable

case "${backend}" in
  local)
    root="${ARCHONTOS_ARTIFACT_LOCAL_PATH:-local-data/artifacts}"
    [ -d "${root}" ] || { log "artifact root ${root} not found"; exit 1; }
    log "tar artifacts from ${root}"
    tar -C "${root}" -cf "${out}/artifacts.tar" .
    ;;
  minio)
    command -v mc >/dev/null || { log "mc (MinIO client) not installed"; exit 1; }
    : "${MC_HOST_archontos:?set MC_HOST_archontos (alias URL) in the environment}"
    bucket="${ARCHONTOS_MINIO_BUCKET:-archontos}"
    log "mc mirror archontos/${bucket} -> ${out}/artifacts/"
    mkdir -p "${out}/artifacts"
    mc mirror --quiet "archontos/${bucket}" "${out}/artifacts"
    ;;
  *)
    log "unknown ARCHONTOS_ARTIFACT_BACKEND=${backend}"
    exit 2
    ;;
esac

# Taken after both snapshots so a later `sha256sum -c` proves the set is intact.
(cd "${out}" && find . -type f ! -name SHA256SUMS -print0 | sort -z | xargs -0 sha256sum >SHA256SUMS)
log "done: ${out} (verify with: cd ${out} && sha256sum -c SHA256SUMS)"
echo "${out}"
