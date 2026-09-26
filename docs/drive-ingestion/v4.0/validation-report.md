# v4 실제 검증 기록

생성 시각(UTC): 2026-09-26T04:17:14.602092+00:00

**오프라인 합성 검증 41개 통과.**
실제 Drive 전수 실행이나 실도면 파싱 정확도를 검증한 결과는 아니다.

| 검증 묶음 | 통과 수 |
|---|---:|
| sql_json_contract_checks | 24 |
| rdf_shacl_cases | 6 |
| policy_unittests | 11 |

## 확인한 동작

- 존재하지 않는 스냅샷·근거와 서로 다른 실행의 근거 연결 거부
- 동일 논리 작업 중복, 근거 없는 승인, 불완전 COMPLETE 거부
- 계정/수집범위 분리, 메타데이터와 실제 바이트 스냅샷 구분
- ZIP 개정·같은 경로 중복 엔트리 식별, 경로 탈출 후보 거부
- 검토 대기·승인 예외를 성공률에서 제외
- CAD 단위 변환, 기준을 걸치는 측정 오차의 검토 보류
- RDF 근거 체인·실행 스냅샷·리뷰어·sameAs 금지 검증

## 미실행

- Google Drive full inventory and live change-feed replay
- Real CAD/SKP/PDF/HWP content parsing and fidelity
- ZWCAD 2025/2026 automation
- Production ACL, concurrent leases, HTTP retries and recovery
- PostgreSQL migration and production restore drill
- Professional legal applicability and measurement validation

## 재현

~~~bash
python -m venv .venv
.venv/bin/python -m pip install -r requirements-validation.lock
.venv/bin/python verify.py
~~~

Windows에서는 .venv/Scripts/python.exe 경로를 사용한다.
검증 실행은 네트워크/API 키 없이 동작한다. 패키지 설치에는 다운로드가 필요할 수 있다.
이 검증은 참조 계약의 일부 무결성을 확인한다. 운영 작업자나 실제 API 연동은 포함하지 않는다.

환경 상세와 전체 확인 항목은 verification.json 및 contracts/validation-result.json 참조.
