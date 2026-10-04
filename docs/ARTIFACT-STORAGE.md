# Artifact storage decision (2026-10)

**Decision: the local content-addressed store is the default and the supported backend.
MinIO stays only as an opt-in, loopback-only development profile.**

| | |
|---|---|
| Default | `ARCHONTOS_ARTIFACT_BACKEND=local` → `LocalArtifactStore` under `ARCHONTOS_ARTIFACT_LOCAL_PATH` (compose: named volume `archontos-artifacts` at `/data/artifacts`) |
| Opt-in | `docker compose --profile s3 up -d minio` + `ARCHONTOS_ARTIFACT_BACKEND=minio` |

## Why

- ArchOntos artifacts are raw law.go.kr response envelopes (JSON, KB-MB), written once and
  addressed by SHA-256. A single host with a filesystem (plus the normal backup of the volume)
  covers this; an object store adds a service, credentials and an attack surface without a need.
- MinIO community edition became source-only (no official binaries/images after late 2025) and
  the upstream repository was archived in 2026. The last official image
  (`RELEASE.2025-09-07T16-13-09Z`) will not receive further security fixes, so it must not be the
  default and must never be exposed beyond `127.0.0.1`.
- `MinioArtifactStore` uses the generic `minio` Python S3 client, so it works unchanged with any
  maintained S3-compatible server. If multi-host or off-box storage is needed later, run
  **SeaweedFS** (Apache-2.0, `weed server -s3`) or **Garage** (AGPL-3.0, lightweight) and point
  `ARCHONTOS_MINIO_ENDPOINT`/`_ACCESS_KEY`/`_SECRET_KEY`/`_BUCKET` at it; no code change.

## Rules

- Non-dev environments refuse to start with the placeholder MinIO secret (`archontos.config`).
- Compose passes the same `ARCHONTOS_MINIO_*` values to the `minio` service and to the apps, so
  the credentials cannot drift (`tests/test_env_compose_consistency.py`).
- Backups: back up the `archontos-artifacts` volume together with the Postgres dump; artifact
  rows in Postgres reference artifacts by content hash.
