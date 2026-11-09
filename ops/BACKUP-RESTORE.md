# Backup / Restore Runbook

`ops/RUNBOOK.md`의 "복구 후 확인"을 절차로 풀어 쓴 문서다. 스크립트: `ops/scripts/backup.sh`,
`ops/scripts/restore_verify.sh`. 검증기: `python -m archontos.ops.verify_restore`.

## 1. 무엇을 백업하는가

canonical state만 백업한다. projection/embedding은 파생 데이터이므로 복구 후 재생성할 수 있다.

| 대상 | 내용 | 비고 |
| --- | --- | --- |
| PostgreSQL 전체 DB | 모든 테이블, `audit_log`, `schema_migrations`, RLS 정책, GRANT | `pg_dump --format=custom`. `audit_log`는 반드시 포함 (감사 증빙) |
| artifact store | local: `ARCHONTOS_ARTIFACT_LOCAL_PATH` (기본 `local-data/artifacts`), MinIO: 버킷 `ARCHONTOS_MINIO_BUCKET` (기본 `archontos`) | content-addressed (`raw/<source>/<hash[:2]>/<sha256>.json`), 불변이므로 증분 복사 가능 |
| 설정 | 환경변수 *이름* 목록과 Helm values / compose 파일 (Git에 있음) | 값(비밀)은 백업 문서에 쓰지 않고 비밀 저장소에서 별도 보관 |

설정 이름 (값은 기록하지 않는다): `ARCHONTOS_DATABASE_URL`, `ARCHONTOS_APP_DB_PASSWORD`,
`ARCHONTOS_API_KEYS`, `ARCHONTOS_ACTOR_ROLES`, `ARCHONTOS_ACTOR_JURISDICTIONS`,
`ARCHONTOS_ALLOWED_JURISDICTIONS`, `ARCHONTOS_ARTIFACT_BACKEND`, `ARCHONTOS_ARTIFACT_LOCAL_PATH`,
`ARCHONTOS_MINIO_ENDPOINT`, `ARCHONTOS_MINIO_ACCESS_KEY`, `ARCHONTOS_MINIO_SECRET_KEY`,
`ARCHONTOS_MINIO_BUCKET`, `ARCHONTOS_EMBEDDER`, `ARCHONTOS_DB_PRIVILEGE_CHECK`, `LAWGO` 키(`ARCHONTOS_LAWGO_OC`).

복원하지 않는 것: HNSW 인덱스 내용(덤프에는 정의만 있고 복원 시 재구축), outbox 처리 중이던 메모리 상태.

## 2. RPO / RTO 가이드

| 항목 | 권장 시작값 | 메모 |
| --- | --- | --- |
| RPO (DB) | 24시간 (일 1회 `backup.sh`); 더 짧게 하려면 WAL 아카이빙/PITR | `pg_dump`는 시점 스냅샷이다. 15분 이하 RPO는 PITR 필요 (이 저장소 범위 밖) |
| RPO (artifact) | DB와 같은 주기 | artifact는 DB 커밋 전에 기록되므로 (`source_version` commit 금지 규칙) DB보다 artifact가 같거나 앞서는 것이 정상. 반대(DB에는 있고 파일 없음)는 장애 |
| RTO | 4시간 (드릴로 실측 후 조정) | 복원 시간은 DB 크기에 비례. HNSW 재구축이 가장 길 수 있다 |

백업 순서: DB 덤프 -> artifact 스냅샷. artifact는 추가만 되므로 이 순서면 덤프에 있는 모든 artifact가
스냅샷에 존재한다. 복원 후 `verify_restore`가 이를 확인한다.

## 3. 역할 (least privilege)

백업과 복원은 서비스 로그인(`archontos_svc`)으로 하지 않는다. 서비스 로그인은 `audit_log`에서
UPDATE/DELETE가 불가능하고 `source_document`는 RLS(013)로 관할 범위 밖 행이 보이지 않아, 이 로그인으로
덤프하면 불완전한 백업이 된다.

| 작업 | 사용할 역할 | 이유 |
| --- | --- | --- |
| 백업 (`pg_dump`) | superuser 또는 전용 `archontos_backup` (`LOGIN BYPASSRLS`, `pg_read_all_data`) | `pg_dump`는 RLS가 행을 숨길 수 있으면 실패한다 (`row_security=off`). 통과시키려면 BYPASSRLS가 필요 |
| 복원 (`pg_restore`) | migrator (스키마 소유자, `CREATEROLE`) | 객체 소유자가 된다 (`--no-owner`). 서비스 로그인은 소유자가 되면 안 된다 (011/013 참고) |
| 검증 (`verify_restore`) | 서비스 로그인 | 실제 서비스와 같은 권한에서 `audit_log` append-only를 확인한다 |

`archontos_backup` 생성 예 (migrator/superuser, 비밀번호는 환경변수/`psql \password`로, argv 금지):

```sql
CREATE ROLE archontos_backup LOGIN NOSUPERUSER BYPASSRLS;   -- 비밀번호는 \password 로 설정
GRANT pg_read_all_data TO archontos_backup;
```

`archontos_app` 그룹 역할은 클러스터 수준 객체라 `pg_dump`(단일 DB)에 포함되지 않는다. 새 클러스터에
복원하면 GRANT가 존재하지 않는 역할을 참조하므로 `restore_verify.sh`가 복원 전에 (없으면)
`archontos_app`을 012와 동일한 속성으로 만든다. 서비스 로그인은 비밀번호가 필요하므로 복원 후
`db.roles ensure-login`으로 재발급한다.

## 4. 절차

### 4.1 백업

```bash
export PGHOST=... PGUSER=archontos_backup PGDATABASE=archontos   # PGPASSWORD 또는 PGPASSFILE (argv 금지)
export ARCHONTOS_BACKUP_DIR=/var/backups/archontos
export ARCHONTOS_ARTIFACT_LOCAL_PATH=/data/artifacts             # minio면 ARCHONTOS_ARTIFACT_BACKEND=minio + MC_HOST_archontos
ops/scripts/backup.sh        # 마지막 줄에 백업 디렉터리 경로 출력
```

결과: `<dir>/<UTC 타임스탬프>/{db.dump,db.toc,artifacts.tar,SHA256SUMS}`. 백업 디렉터리는 0700이며
오프사이트(다른 계정/리전)로 복사하고, 복사 후 `sha256sum -c SHA256SUMS`로 확인한다.
백업에는 개인/법령 데이터와 감사 로그가 있으므로 저장소 암호화와 접근 통제를 적용한다.

### 4.2 복원

빈 데이터베이스를 준비한다 (`CREATE DATABASE archontos_restore OWNER archontos;`). 대상이 비어 있지 않으면
스크립트는 거부한다. 덮어쓰기는 `--force-nonempty`를 명시했을 때만 (`--clean --if-exists` 적용).

```bash
export PGHOST=... PGUSER=archontos PGDATABASE=archontos_restore   # migrator
export ARCHONTOS_ARTIFACT_RESTORE_PATH=/data/artifacts
export ARCHONTOS_SERVICE_DSN='postgresql://archontos_svc@host/archontos_restore'  # 비밀번호는 PGPASSFILE
ARCHONTOS_SERVICE_LOGIN=archontos_svc ARCHONTOS_APP_DB_PASSWORD=... \
  ops/scripts/restore_verify.sh /var/backups/archontos/20260101T000000Z
```

스크립트가 하는 일: `SHA256SUMS` 검증 -> 대상 DB 비어 있음 확인 -> `archontos_app`/확장 준비 ->
`pg_restore --single-transaction --exit-on-error` -> artifact 복원 -> (선택) 서비스 로그인 재발급 ->
행 수 출력 -> HNSW 상태 -> `verify_restore`. 이미 복원된 DB만 점검하려면 `--verify-only`.

복원 후 마이그레이션이 뒤처져 있으면(`pending`) `python -m archontos.db.migrate`를 migrator로 실행한 뒤
다시 검증한다.

## 5. 복원 검증

`restore_verify.sh`가 자동 수행하며, 수동으로는 다음과 같다.

1. **마이그레이션 상태**: `python -m archontos.db.migrate --status` 가 `pending=[]`, `edited=[]`.
   `SELECT version, checksum FROM schema_migrations ORDER BY version;`가 `db/migrations/*.sql`과 일치
   (`verify_restore`가 체크섬을 비교).
2. **행 수**: 백업 시점 원본과 비교 (`source_document`, `source_version`, `artifact`, `assertion`, `decision`,
   `outbox_message`, `audit_log`). 백업 이후 쓰기가 있었다면 복원본이 같거나 적어야 한다.
3. **audit_log append-only 유지**: 서비스 로그인으로 `verify_restore`가 `relforcerowsecurity = true`이고
   UPDATE/DELETE/TRUNCATE 권한이 없음을 확인한다. 수동 확인:
   `UPDATE audit_log SET actor = actor;` 가 `permission denied`로 실패해야 한다 (superuser 제외).
4. **HNSW 인덱스**: `python -m archontos.db.vector_index --show`. 인덱스가 없거나 옵션이 다르면
   `python -m archontos.db.vector_index --m 16 --ef-construction 64` (migrator, `CONCURRENTLY`)로 재구축한다.
   `pg_restore`는 대량 데이터 로드 후 인덱스를 만들기 때문에 시간이 걸린다.
5. **artifact 해시**: `python -m archontos.ops.verify_restore`가 `artifact` 테이블의 모든 행에 대해
   `storage_uri`의 객체를 읽어 sha256 == `content_hash`, 길이 == `byte_size`를 확인한다. 누락/불일치는
   `FAIL [artifact] ...`로 출력되고 종료 코드 1.
6. **서비스 점검**: 서비스 기동 시 `db.roles check` (`ARCHONTOS_DB_PRIVILEGE_CHECK=enforce`)가 통과하고
   `/v1` smoke 요청과 `outbox pending count`, `projection checkpoint`가 정상 (RUNBOOK 복구 후 확인).
   projection이 비어 있거나 오래되었으면 projection을 rebuild한다.

종료 코드: 0 정상, 1 검증 실패(finding 있음), 2 사용법/접속 오류. `--json`으로 기계 판독 출력.

## 6. 복원 드릴 체크리스트 (분기 1회)

- [ ] 운영과 분리된 환경(별도 DB 서버 또는 별도 DB)에서 수행, 운영 비밀 사용 안 함
- [ ] 최신 백업의 `sha256sum -c SHA256SUMS` 통과
- [ ] 시작 시각 기록 (RTO 측정)
- [ ] 빈 DB에 `restore_verify.sh` 실행 성공; 비어 있지 않은 DB에서는 플래그 없이 거부되는지 확인
- [ ] `verify_restore` 종료 코드 0 (artifact 해시, 마이그레이션, audit_log)
- [ ] 의도적으로 artifact 파일 1개를 변조해 `verify_restore`가 실패하는지 확인 후 원복
- [ ] 서비스 로그인 재발급 후 `db.roles check` 통과, 서비스 기동 및 smoke 요청 성공
- [ ] 행 수가 백업 시점과 일치, 최근 decision provenance 조회 가능
- [ ] HNSW 인덱스 존재 및 유사도 검색 1건 성공 (필요 시 재구축 시간 기록)
- [ ] 종료 시각 기록: 실측 RTO와 RPO(마지막 백업 시각 - 장애 시각)를 목표와 비교
- [ ] 로그/터미널 기록에 비밀(DSN 비밀번호, 키)이 없음을 확인
- [ ] 발견 사항과 개선 항목을 이슈로 등록하고 이 문서에 반영

## 7. 제한

- `pg_dump` 스냅샷이므로 PITR이 아니다.
- MinIO 백업은 `mc mirror`에 의존한다 (버전 관리/객체 잠금 정책은 별도 설정).
- 이 문서의 스크립트는 실제 Postgres/MinIO에 대한 전체 드릴을 통해 환경별로 검증해야 한다.
