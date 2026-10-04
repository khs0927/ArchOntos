# Operations Runbook

## 장애 우선순위

1. canonical DB 쓰기 실패: ingestion 중단, 재시도
2. artifact 저장 실패: source_version commit 금지
3. outbox 지연: canonical state는 보존하고 worker 재시작
4. projection 오류: projection을 격리하고 rebuild
5. rule 오류: 해당 rule version을 비활성화하고 REVIEW로 전환

## 원칙

파생 시스템의 장애는 canonical state를 수정하지 않는다. conflict는 rollback으로 사라지지 않도록 별도 commit 경계를 유지한다.

## 복구 후 확인

- artifact hash
- source version count
- outbox pending count
- projection checkpoint
- 최근 decision provenance
