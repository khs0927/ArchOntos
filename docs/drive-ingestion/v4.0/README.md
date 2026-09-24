# 건축 온톨로지 AI FDE v4.0 — 설계·구현 계약 패키지

전체 Google Drive의 모든 도면과 건축 관련 문서를 대상으로 한다.
이 패키지는 아키텍처와 실행 가능한 합성 검증 계약이며, 운영 애플리케이션이나 Drive 크롤러가 아니다.

## 읽는 순서

1. architecture-v4.md — 수정된 전체 아키텍처·무료 운영 경로·실행 순서
2. validation-report.md — 실제 통과한 검사와 미실행 범위
3. ARCHONTOS-INTEGRATION.md — ArchOntos 기준 데이터와의 매핑 및 통합 제한
4. drive-observations.md — 익명화한 대표 자료 유형 관찰, 전수 통계 아님
5. acceptance.md — 실제 자료·운영 인수 시험 명세
6. contracts/README.md — SQL/JSON 계약과 구현자가 채워야 할 부분

## 파일

| 파일 | 역할 |
|---|---|
| contracts/catalog.sql | 계정·corpus·원본·메타데이터·스냅샷·작업·증거·주장·커서 참조 스키마 |
| contracts/extraction-result.schema.json | 프로파일별 추출 결과 JSON Schema |
| ontology.ttl | 건축 문서·출처 핵심 어휘, IFC/KBimCode 준수 인증 아님 |
| shapes.ttl | 콘텐츠 스냅샷·실행·근거·관계 주장 SHACL 무결성 |
| reference_policy.py | 완료율·아카이브 ID·단위·측정 보류·접근 정책의 오프라인 참조 함수 |
| policy.example.json | 추가 유료 API·ODA·원본 수정 기본 비활성 예제 |
| verify.py | SQL/JSON, RDF/SHACL, 정책 시험 일괄 실행 |
| requirements-validation.lock | 이번 검증 환경의 실제 패키지 버전 |
| verification.json | 실행 환경·검증 건수·미실행 범위 |

## 실행

~~~bash
python -m venv .venv
.venv/bin/python -m pip install -r requirements-validation.lock
.venv/bin/python verify.py
~~~

Windows에서는 .venv/Scripts/python.exe를 사용한다.
검증 자체에는 Google 인증이나 외부 AI API가 필요하지 않다.
실제 Drive OAuth, 변경 수집기, 다운로드·파서 작업자, 검토 UI, ACL 강제 적용, 백업 운영은 아직 구현된 서비스가 아니다.

## 계약 사용 시 유의점

- SQLite 스키마는 독립 검증용이다. PostgreSQL로 옮기면 별도 migration 및 동일 부정 사례를 검증한다.
- ArchOntos에 적용할 때 이 SQLite 카탈로그를 운영 정본으로 배포하지 않는다. 매핑 기준은 ARCHONTOS-INTEGRATION.md를 따른다.
- ContentSnapshot은 확보한 실제 바이트를 표현한다. 메타데이터 관측은 별도 테이블이다.
- 안정 revision을 확보할 수 없는 네이티브 export는 capture event ID·표현 형식·해시로 식별한다. 공급자 revision과 export hash를 동일시하지 않는다.
- SHACL은 핵심 연결 제약만 검증한다. 도면 위치·PDF bbox·셀 locator별 정확성, 문서 개정 순환과 법규 적용성은 별도 인수 대상이다.
- 추가 API 지출 0원 정책은 설정만으로 강제되지 않는다. 실제 실행 게이트 구현과 검증이 필요하다.
- 1.8m가 등장하는 합성 시험은 임의 숫자 비교 fixture이며 모든 복도의 법규 기준이 아니다.
