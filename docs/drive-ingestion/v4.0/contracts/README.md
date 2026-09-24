# v4 구현 계약과 오프라인 검증

이 디렉터리는 실행 서비스가 아닌 구현 계약 예제입니다. 실제 Drive 스캔·CAD 파싱·운영 검증을 수행하지 않습니다.

- `catalog.sql`: 출처, 원본 아티팩트, 스냅샷, 멱등 작업, 증거, 주장, 변경 커서 스키마.
- `extraction-result.schema.json`: Draft 2020-12 결과 계약. COMPLETE는 명시된 scope에만 적용됩니다. GEOMETRY 완료가 법규 적합을 뜻하지 않습니다.
- `validate_contract.py`: JSON Schema 검사 외에도 동일 실행 내 증거 참조, 중복 증거 ID, 처리 건수 일치를 검사합니다. JSON Schema 파일 단독으로 이 참조 무결성을 보장하지 않습니다.
- `validation-result.json`: 24개 합성 검증 결과. FK 위반, 중복 실행, 다른 스냅샷/실행 증거 연결, 근거 없는 승인, 커서 롤백, 불완전 COMPLETE, 추출기의 자기 승인 등을 검사합니다.

실행: `python -m pip install jsonschema` 후 `python validate_contract.py`.

## 구현자가 지켜야 할 계약

모든 SQLite 연결에 `PRAGMA foreign_keys=ON`을 적용합니다. 재시도는 기존 job의 attempts/lease를 갱신하며 같은 스냅샷·단계·추출 프로파일 ID와 버전·파서 ID와 버전·설정으로 새 job을 생성하지 않습니다. 다른 작업자에게 할당할 때 조건부 UPDATE와 트랜잭션으로 lease를 획득해야 합니다. 그 작업자 구현은 포함하지 않습니다.

변경 페이지의 항목 저장, 작업 등록, page receipt 저장, 이전 토큰을 조건으로 한 cursor 갱신은 같은 트랜잭션에 둡니다. lease 회수, 토큰 만료 재수집, 네트워크 장애 시험, 스냅샷 불변성 및 운영 감사 로그는 후속 구현 항목입니다.

압축 내부 아티팩트의 외부 ID는 컨테이너 스냅샷 ID + entry ordinal + 정규화 전 내부 경로를 충돌 없는 인코딩으로 구성합니다. ordinal은 동일 경로가 여러 번 나타나는 ZIP도 구별합니다. 경로는 풀기 전에 별도로 보안 검증해야 합니다.

수집 범위/초기 페이지 완료 지표, 폴더 이동의 관측 이력, 분류 검토 이력, 법규 규칙·판정 계약, 접근권한 동기화는 이 최소 스키마에 모두 구현되어 있지 않습니다. 이 예제의 통과를 전체 애플리케이션 완성으로 해석해서는 안 됩니다.

## v4 일관성 보완

source는 connector/provider + account_namespace의 안정적인 출처 신원이며, 검색·동기화 범위인 corpus와 분리했습니다. 파일이 My Drive와 공유 범위 사이에서 이동해도 source + external_id는 중복되지 않습니다. 커서와 page receipt는 corpus별입니다.

ContentSnapshot은 실제 바이트 수집 시점에만 생성합니다. SHA-256, 바이트 위치, 크기는 필수입니다. 메타데이터 관측·접근 실패는 metadata_observations에 저장합니다. 데이터베이스는 해시 형식을 검증하며 실제 파일과 해시 일치는 수집기가 검증해야 합니다.

아카이브는 original_path/normalized_path와 컨테이너 스냅샷 내 고유 ordinal을 저장합니다. 파일 매칭에서 owl:sameAs는 금지하며, STALE 주장은 근거를 보존한 채 현행 답변에서 제외할 수 있습니다. 실제 조회 서비스가 STALE 제외 정책을 구현해야 합니다.

추출 결과 coverage에는 profile_id와 profile_version이 필수입니다. COMPLETE는 그 프로파일의 처리 범위와 expected/processed 일치에 한정됩니다.
