# Migration Notes

## 다음 migration 후보

- evidence_span의 canonical locator 및 normalized text hash
- assertion candidate/review fields
- rule_assertion validation constraint
- applicability dimensions
- query audit fields

## 안전 규칙

- 기존 source_document identity를 변경하지 않는다.
- content hash는 source_version/artifact에 둔다.
- provenance FK는 삭제 cascade보다 보존을 우선한다.
- migration 전 backup과 fixture replay를 수행한다.
