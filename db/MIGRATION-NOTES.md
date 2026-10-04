# Migration Notes

## 적용 완료 (migration 파일을 직접 읽어 확인할 것)

이 문서는 요약이 아니라 색인다. 실제로 무엇을 하는지는 migration 파일이
정답이며, 아래 서술이 그 파일과 어긋나면 파일을 고치는 게 아니라 이 문서를
고쳐야 한다.

- `004_evidence_identity.sql` — `evidence_span.evidence_key`와
  `normalized_text_hash` 컬럼 추가, 그리고
  `(source_version_id, evidence_key)` 부분 유일 인덱스
  (`WHERE evidence_key IS NOT EXISTS`)
- `005_assertion_review_workflow.sql` — assertion의 review 상태 전이와
  `assertion_review` 이력 테이블, evidence에 대한 provenance FK
- `006_rule_compilation_lifecycle.sql` — `rule_version`에 `rule_key`,
  `compiler_version`, `compiled_at` 추가, 그리고
  `status IN ('draft','active','suspended','retired')` CHECK 제약.
  `rule_assertion`을 **읽어서** 안전하지 않은 rule을 suspended로 만드는 데이터
  UPDATE를 포함한다
- `007_outbox_retry_metadata.sql` — `outbox_message`에 `last_error`,
  `last_attempt_at` 추가

주의: 이전 판본의 이 문서는 006이 `rule_assertion` 검증 제약과 applicability
차원을 만든다고 적었고, 007이 `attempts`와 `status`를 추가한다고 적었다. 둘 다
틀렸다. `rule_assertion` 테이블은 `003_provenance_links.sql`이,
`applicability` 테이블은 `001_initial.sql`이 만들며, `outbox_message.attempts`와
`status`도 `001_initial.sql`에 있다. 006과 007이 건드리지 않은 것을
"구현 완료"로 올린 것은 드리프트를 고치는 것이 아니라 새로 만드는 것이었다.

## 아직 구현되지 않은 것

아래는 여전히 실제로 빠져 있다. 문서가 이를 다음 후보로 적어 놓은 것이
맞았다.

- `rule_assertion`에 대한 검증 제약. 001~007 어디에도 `rule_assertion`의 CHECK가
  없다. 지금 검증은 애플리케이션 계층(`rules/evaluation.py`)에만 있다
- applicability 조건을 규칙에 영속화하는 경로. `applicability` 테이블은
  존재하지만 이를 채우는 애플리케이션 코드가 없다
- query 감사 필드 (누가, 언제, 어떤 근거로 질의했는지)
- `action` / `action_run` 영속화. 스키마는 있으나 이를 쓰는 Python이 없다
- projection_checkpoint 갱신 경로. 스키마는 있으나 드레인하는 워커가 없다

## 마이그레이션 실행기 (`archontos.db.migrate`)

운영·CI·테스트가 모두 같은 경로를 쓴다: `python -m archontos.db.migrate`.

- `db/migrations/NNN_name.sql`을 이름 순서로 한 번씩 적용하고 `schema_migrations`
  (version, sha256 checksum, applied_at)에 기록한다. Postgres advisory lock으로 여러
  서비스가 동시에 시작해도 한 번만 실행된다.
- `docker compose up`은 일회성 `migrate` 서비스를 먼저 실행하고, 앱 서비스는 그것이
  0으로 끝난 뒤에 시작한다. 예전의 `docker-entrypoint-initdb.d` 마운트(빈 볼륨에서만
  실행)는 제거했다.
- `tests/integration/conftest.py`도 같은 실행기를 쓴다. `tests/integration/test_migrations.py`는
  "이전 일부 migration만 적용된 DB를 업그레이드한 결과 = 새 DB" (컬럼·인덱스·제약 비교)를 검사한다.
- 이미 적용된 migration 파일을 고치면 checksum 불일치로 실패한다. 변경은 새 번호 파일로 한다.
  (CRLF/LF 차이는 무시한다.)
- 같은 번호 접두(`004_a`, `004_b`)는 거부한다.
- `alembic`은 설정 파일이 하나도 없는 유령 의존성이었으므로 `pyproject.toml`에서 제거했다.

### 예전 initdb 마운트로 만든 볼륨

`schema_migrations`가 없는데 테이블이 있으면 실행기는 멈추고 안내한다. 그 볼륨이 어떤
migration까지 갖고 있는지 확인한 뒤 한 번만:

```bash
docker compose run --rm migrate python -m archontos.db.migrate --status
docker compose run --rm migrate python -m archontos.db.migrate --baseline 007
```

### 2026-10 수정: 005의 제약 존재 검사

`005_assertion_review_workflow.sql`은 `pg_constraint`를 이름만으로 검사해서, 같은 DB의
다른 스키마(예: 테스트 스키마)에 같은 이름의 제약이 있으면 `uq_evidence_span_id_source_version`과
`assertion_evidence_source_fk`를 만들지 않고 건너뛰었다. `conrelid = '<table>'::regclass`로
범위를 한정했다. 실행기 도입 전이라 기록된 checksum이 없으므로 파일을 직접 고쳤다.

## 안전 규칙

- 기존 source_document identity를 변경하지 않는다.
- content hash는 source_version/artifact에 둔다.
- provenance FK는 삭제 cascade보다 보존을 우선한다.
- migration 전 backup과 fixture replay를 수행한다.
- 새 migration 뒤에는 E2E golden path를 실제 PostgreSQL에서 한 번 돌린다.
- migration 파일 번호는 브랜치 간 충돌이 없다. 현재 MVP-0이 `004`-`007`,
  Drive가 `004_drive_inventory`를 쓰고 있어 같은 접두 번호가 두 개 존재한다.
  파일명이 달라 git은 충돌 없이 병합하지만, 정렬 순서상 Drive가
  `005_assertion_review_workflow`보다 먼저 실행되며 후자는 004가 먼저
  실행됐다고 전제한다. 병합 전에 번호를 정리해야 한다.
