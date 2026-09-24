# ArchOntos 통합 판단 및 데이터 계약 매핑

## 결정

이 패키지는 별도 제품·정본 저장소가 아니라 ArchOntos의 Drive 및 AEC 자료 인입 기능을 구체화하는 설계 계약으로 관리한다. ArchOntos는 이미 PostgreSQL을 정본으로 삼고 원본 버전, 증거, 주장, 이벤트와 재생성 가능한 검색·RDF 투영을 가진다. Drive·CAD 문서 온톨로지는 이 관계에 연결되어야 한다.

이 브랜치의 범위는 문서와 독립 합성 검증 계약이다. OAuth 수집기, 실제 Google Drive 전체 열거, CAD 파서, PostgreSQL 운영 migration은 포함하거나 완료했다고 주장하지 않는다.

## 기존 정본과의 매핑

| v4 개념 | ArchOntos 통합 위치 | 경계 |
|---|---|---|
| connector/account/corpus | Drive 연결·수집 범위를 나타내는 별도 provider 계층 | 계정과 corpus는 건축 법령 `source_document`가 아니다. |
| Drive `fileId` 및 위치 관측 | 기존 아티팩트 출처 identity에 연결하고, Drive별 관측·corpus membership·ACL은 필요한 범위에서 확장 | fileId는 이동 가능한 경로와 구분하며, 바이트 해시로 원본 신원을 합치지 않는다. |
| 다운로드/내보내기 콘텐츠 | 기존 `artifact` 및 `source_version` 계약에 연결 | 네이티브 Workspace export의 식별은 provider revision과 export hash를 같다고 가정하지 않는다. |
| 메타데이터·검토 근거 | 기존 관측·`evidence_span` 모델에 연결 | 본문을 얻지 못한 메타데이터 관측을 콘텐츠 증거로 승격하지 않는다. |
| 온톨로지 관계 주장 | 기존 `assertion` 및 검토 상태에 연결 | 임베딩·LLM·기하 결과는 승인 전 후보이며 `owl:sameAs`로 원본을 병합하지 않는다. |
| 동기화 이벤트와 투영 갱신 | 기존 domain event/outbox 및 projection 경계를 따른다. Drive corpus별 cursor/receipt 확장은 기존 원자성 모델과 함께 설계한다. | 독립 SQLite cursor가 운영 상태가 되지 않도록 한다. |
| 법규와 판정 | ArchOntos의 권위 있는 문서·규칙 버전·근거·적용성 모델을 재사용 | 이 패키지의 규칙 추출 예시는 규칙 승인이나 법규 정확성 검증이 아니다. |

## SQLite 계약 사용 제한

`contracts/catalog.sql`은 FK·상태·멱등성의 부정 사례를 실행하는 SQLite 참조 harness다. 운영 PostgreSQL migration이 아니며, 기존 `artifact`, `source_document/source_version`, `evidence_span`, `assertion`을 대체하거나 같은 의미의 두 번째 테이블군을 추가해서는 안 된다. 특히 v4의 `sources`는 connector/account 의미이고 ArchOntos의 `source_document`는 법령 등 출처 문서 의미이므로 이름만 보고 직접 매핑하지 않는다.

실제 구현 전에는 다음을 수행한다.

1. Drive provider identity와 파일별 접근 경계를 기존 아티팩트 출처 모델에 매핑한다.
2. 관측·revision·export 내용을 기존 버전 및 provenance 계약에 매핑하고 PostgreSQL migration을 설계한다.
3. corpus별 초기 열거·공유 드라이브·휴지통·변경 토큰·접근 회수와 재처리를 outbox 원자성 및 멱등성 시험에 추가한다.
4. 원본, 검색, 스니펫, 미리보기, 파생 콘텐츠와 캐시까지 사용자 ACL이 전파되는지 검증한다.
5. ZWCAD/SketchUp/PDF/HWP 등 실제 보유 파일로 parser registry와 품질 게이트를 검증한다.

## 공개 범위

저장소에는 익명화된 자료 유형 요약과 합성 fixture만 둔다. 원본 Drive URL·ID, 프로젝트 주소·고객명, 도면 및 실제 문서 본문은 커밋하지 않는다. 이 디렉터리의 검증 통과는 전체 Drive 수집·실파일 파싱·법규 운영 승인으로 해석하지 않는다.
