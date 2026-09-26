# Migration Notes

## 적용 완료

아래 항목은 이미 migration으로 구현되어 있다. 이전 문서의 "다음 후보" 목록은
마이그레이션 코드와 어긋나 있었다.

- `004_evidence_identity.sql` — evidence_span canonical locator
  (`evidence_key`, `normalized_text_hash`)와 (source_version_id, evidence_key)
  유일 인덱스
- `005_assertion_review_workflow.sql` — assertion candidate / review 필드와
  provenance 복합 FK
- `006_rule_compilation_lifecycle.sql` — rule_assertion 검증 제약과
  applicability dimension, compile 상태(active/suspended) 수명주기
- `007_outbox_retry_metadata.sql` — outbox 재시도 메타데이터(attempt, last_error,
  상태)

각 migration의 실제 내용을 먼저 읽을 것. 이 문서는 요약일 뿐 원본이 아니다.

## 다음 migration 후보

- query 감사 필드 (누가, 언제, 어떤 근거로 질의했는지)
- action / action_run 영속화. 스키마는 있으나 이를 쓰는 Python이 없다
- projection_checkpoint 갱신 경로. 스키마는 있으나 드레인하는 워커가 없다

## 안전 규칙

- 기존 source_document identity를 변경하지 않는다.
- content hash는 source_version/artifact에 둔다.
- provenance FK는 삭제 cascade보다 보존을 우선한다.
- migration 전 backup과 fixture replay를 수행한다.
- 새 migration 뒤에는 E2E golden path를 실제 PostgreSQL에서 한 번 돌린다.
