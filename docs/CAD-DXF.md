# DXF 정규화 (MVP-1)

`python -m archontos.cad.dxf <file.dxf>` 는 DXF 파일을 읽기 전용으로 열어 안정적인 JSON 문서를 출력합니다.
선택 의존성(`pip install -e '.[cad]'`, ezdxf, MIT)이며 기본 설치에는 들어가지 않습니다. 없으면 명확한 오류로 종료합니다.
CI에는 `embeddings` 잡과 같은 방식의 `cad` 잡이 있습니다.

## 범위

- 입력 파일 바이트의 sha256 → `source_byte_revision_id`.
- ezdxf 버전 + 정규화기 버전에서 파생한 sha256 → `parser_revision_id`.
- 레이어 목록, 블록(INSERT) 이름별 개수.
- 닫힌 LWPOLYLINE / 2D POLYLINE의 면적·둘레. 단위는 `$INSUNITS`에서만 읽습니다.
- 모든 사실에 `locator = {layer, handle}`가 붙고, 정렬 순서가 고정되어 같은 바이트는 항상 같은 출력을 냅니다.

## 판단 원칙 (fail closed)

- 형식이 깨졌거나 지원하지 않는 입력은 `DxfError`(CLI 종료 코드 2)이며 부분 결과를 내지 않습니다.
- 단위를 알 수 없으면(`$INSUNITS`가 0이거나 목록에 없음) 값은 그대로 두고 `unit: "unknown"`, `flags: ["unit_unknown"]`로 표시합니다.
- 열린 폴리라인은 면적 대상에서 제외합니다. 호(bulge)가 있는 폴리라인은 면적·둘레를 `null`로 두고 `bulge_arcs_unsupported`로 표시합니다.

## 하지 않는 것 (non-goals)

- 자동 PASS/FAIL 판정을 하지 않습니다. 이 출력은 사실 후보일 뿐 법규 평가가 아닙니다.
- CAD 원본을 쓰거나 덮어쓰지 않습니다. 파일은 읽기만 합니다.
- 단위를 추측하거나 환산하지 않습니다.
- 전체 도면 파서가 아닙니다(해치, 치수, 3D, 외부참조, 블록 내부 중첩 전개는 다루지 않음).

## Ontology 계약과의 관계

도면 파싱과 지식그래프는 Ontology의 몫이고 `aec-facts-export/1`이 정식 경로입니다(`docs/ONTOLOGY-INTEGRATION.md`).
이 모듈의 `source_byte_revision_id`, `parser_revision_id`, `locator`는 `AecSubjectRef`와 같은 모양이라
사실 출처를 같은 방식으로 가리킬 수 있습니다. 다만 이 출력은 Ontology export를 대체하지 않으며,
`source_id`와 `project_id`는 Ontology가 부여하므로 여기서 만들지 않습니다.

## 한계

- 파일을 두 번 읽습니다(해시용 바이트, ezdxf 파싱). 처리 중 파일이 바뀌면 해시와 내용이 어긋날 수 있습니다.
- 블록 개수는 모델 공간의 INSERT만 셉니다.
