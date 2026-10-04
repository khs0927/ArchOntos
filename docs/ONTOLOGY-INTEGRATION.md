# Ontology ↔ ArchOntos 연동 (도면 KG 사실 → 법규 규칙 평가 → KG 링크)

ArchOntos 규칙과 Ontology(도면 파싱 → 지식그래프) 사이의 계약은 세 가지 JSON 스키마로 정해져 있습니다.
네트워크 호출은 하지 않습니다. 모두 로컬 파일이나 Ontology API(bearer 토큰 필요)로 주고받습니다.

| 스키마 | 생산 | 소비 | 내용 |
|---|---|---|---|
| `archontos-aec-subject-ref/1` | Ontology | ArchOntos `AecSubjectRef` | 프로젝트·도면·객체 참조. `source_id`/`source_byte_revision_id`(파일 sha256)/`parser_revision_id`는 셋이 함께 있거나 모두 없어야 함 |
| `aec-facts-export/1` | Ontology `aec operational kg-facts <project>` 또는 `GET /v1/kg/projects/{key}/facts` | `python -m archontos.integration.ontology` | 관측 사실(`building.highest_observed_floor`, `building.deepest_observed_basement`, `building.storeys`, `space.uses`, `steel.sections`), 사실별 근거(provenance), 주체 참조 목록 |
| `archontos-rule-export/1` | `--export-links` | Ontology `kg-build --rules` (`AEC_RULES_FILE`) | 규칙 id·버전·평가 결과와 `applies_to` 주체 참조. Ontology가 `subjectTo` 엣지로 KG에 연결 |

## 사용 순서

```powershell
# 1) Ontology: 프로젝트 사실 내보내기 (PC)
aec operational kg-facts 주례동-315-4 --out D:\AECData\rules\facts-주례동.json
# 2) ArchOntos: 규칙 평가 (운영자가 관할·용도 같은 도면에 없는 사실을 보충)
python -m archontos.integration.ontology --rule rule.json --facts D:\AECData\rules\facts-주례동.json `
  --context '{"context": {"jurisdiction": "KR-26"}}' --rule-id R-STAIR-2 --version-label 2024-01 `
  --title "직통계단 2개소" --export-links D:\AECData\rules\links.json
# 3) Ontology: 링크를 KG에 반영
aec operational kg-build --rules D:\AECData\rules\links.json
```

## 판단 원칙 (fail closed)

- 도면에서 확인되지 않는 사실은 내보내지 않습니다(`absent_by_design`: `stair.direct_count`, `building.use_group`,
  `building.gross_floor_area`). 그런 사실을 쓰는 규칙은 `REVIEW`가 됩니다. PASS/FAIL로 추정하지 않습니다.
- 운영자 `--context`는 빈 자리만 채웁니다. 도면 사실과 다르면 도면 값을 유지하고 `context_conflicts`에 기록합니다.
  하위 경로를 써도 기존 단일 값을 객체로 바꿀 수 없습니다. 같은 경로를 상충하는 모양으로 쓴 운영자 입력은 거절합니다.
- 층 이름의 최대값은 건물 전체 층수가 아닙니다. 이전 export의 `building.floor_count`/`building.basement_count`도
  부분 coverage 또는 비권위 총계 표시가 있으면 제거하고 `fact_warnings`에 남깁니다.
- 단위가 있는 수치 규칙은 `fact_units`를 명시합니다. 예: `{"building": {"height": 2},
  "fact_units": {"building.height": "m"}}`. 단위 누락·불일치, NaN/Infinity·문자열·bool 수치는 `REVIEW`입니다.
  단위를 자동 환산하지 않습니다. `count`는 음수가 아닌 정수여야 합니다.
- 결과의 `fact_evidence`는 규칙이 읽은 각 사실의 출처(ontology 근거 노드·도면 / operator)를 보여 줍니다.
- Ontology는 `source_byte_revision_id`가 현재 도면 해시와 다른 참조를 `stale: true` 엣지로 연결합니다.
  도면이 바뀌었으니 다시 검토하라는 뜻입니다.

## 저장과 권위

- migration `008_assertion_aec_subjects`는 후보의 검증된 `applies_to`를 `assertion.applies_to_json`에 저장합니다.
  근거 조회에도 참조가 반환됩니다. 적용 대상이나 도면 개정이 달라지면 새 미승인 후보 identity가 생기며
  이전 승인을 조용히 재사용하지 않습니다. 기존 참조 없는 후보의 identity는 유지됩니다.
- 파일 기반 CLI는 `evaluation_mode=offline`, `binding=false`입니다. `--export-links`에 canonical context를
  꾸며 넣지 않습니다. Ontology는 이런 링크를 `REVIEW` 근거로 받아들이고 계산된 원래 분기는 보관합니다.
- canonical DB 평가에는 기존 승인·활성·평가일 시행 기간 검사가 있습니다. CLI의 오프라인 결과와 별도 경로입니다.
- PC에는 ArchOntos 운영 DB가 없습니다. 실제 법령 데이터 기반 규칙은 아직 없고, 검증은 합성 규칙으로만 했습니다.
