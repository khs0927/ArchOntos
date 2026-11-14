# 실제 법령 E2E (`ops/e2e_real_law.py`)

law.go.kr DRF에서 실제 법령을 받아 도면 검토 보고서까지 한 번에 돌리는 스크립트.
CI에서는 돌지 않는다(키가 없으면 `SKIP` 출력 후 종료코드 0).

## 실행

```powershell
docker run -d --rm --name ao-p0-pg -e POSTGRES_PASSWORD=<pw> -p 127.0.0.1:55460:5432 pgvector/pgvector:pg18
doppler run --project mcp-agent-credentials --config dev -- `
  python ops/e2e_real_law.py --dsn postgresql://postgres:<pw>@127.0.0.1:55460/postgres `
  --summary-json e2e-summary.json
docker rm -f ao-p0-pg
```

- 키: Doppler `LAW_OC` → 스크립트 안에서 `ARCHONTOS_LAWGO_OC`로 넘김. 출력·파일에 쓰는
  모든 문자열은 키와 `OC=` 값을 `***`로 가린다.
- DB: 임시 스키마(`archontos_e2e_*`)와 앱 로그인을 만들고 끝나면 지운다(`--keep`로 유지).

## 단계와 기대 결과

| # | 단계 | 사용 경로 |
|---|------|-----------|
| 1 | 건축법 시행령, 건축물의 피난ㆍ방화구조 등의 기준에 관한 규칙 수집 | `POST /v1/sources/lawgo/ingest-current` (실제 DRF) |
| 2 | 근거 구간(evidence span) 정규화 | `NormalizationOutboxWorker` |
| 3 | 수치 요건 assertion 후보 → 다른 사람이 승인 | `CanonicalAssertionRepository` (작성 `e2e-author`, 승인 `e2e-reviewer`, 같은 사람이면 중단) |
| 4 | 규칙 컴파일(미승인은 409 확인) | `POST /v1/rules/compile/{id}` |
| 5 | 생성 DXF 업로드 → 검토 → 이슈 → BCF → 보고서 | `/v1/cad/...` |
| 6 | 마커 action 4-eyes(제안자 승인 403, 다른 사람 200) → 실행 | `/v1/actions/...`, `/v1/cad/markers/...` |

도면(`tests.cad_fixtures.real_law_plan`): 복도 1.5 m·1.0 m, 계단 폭 1.2 m(단높이 정보 없음).

| 규칙 | 근거(규칙) | fact | 기대 |
|------|-----------|------|------|
| 복도 유효너비 ≥ 1.2 m | 제15조의2 | `corridor.width` | FAIL (객체 PASS 1, FAIL 1) |
| 계단·계단참 너비 ≥ 1.2 m | 제15조제2항 | `stair.width` | PASS |
| 계단 단높이 ≤ 0.18 m | 제15조제2항 | `stair.riser_height` | REVIEW (평면에 값 없음) |

모든 결과는 규칙이 만들어진 실제 evidence span의 locator(법령ID·MST·조·항·호)를
`evidence_locator`로 갖고 있어야 통과한다. 건축법 시행령 제48조(위임 조항) locator도 요약에 남긴다.

참고: assertion 검토는 아직 HTTP API가 없어서 저장소 계층을 직접 부르고, 4-eyes는
스크립트가 강제한다(작성자 ≠ 승인자). HTTP 4-eyes는 6단계 action 승인에서 확인한다.

## 실행 기록

### 2026-11-14 (DESKTOP-KTQHS1I)

- 결과: **실패 — law.go.kr이 요청을 거부.** 1단계 첫 호출(`lawSearch.do`)에
  `HTTP 302` → `https://www.law.go.kr/sorry/sorry.html?...` 로 리다이렉트(http/https 동일).
  DRF가 이 OC 값을 이 호출자(IP/도메인 등록)에서 받지 않거나 서비스가 막힌 상태로 보인다.
  키는 8자, 출력에는 노출하지 않았다.
- 2단계 이후는 실제 법령 없이 확인할 수 없어 실행 기록이 없다. (작성 중 합성 DRF 응답으로
  2~6단계 코드 경로만 따로 돌려 PASS/FAIL/REVIEW·locator·BCF·보고서·4-eyes가 기대대로
  나오는 것은 확인했으나, 실제 법령 결과가 아니므로 여기 결과로 싣지 않는다.)
- 다음 조치: open.law.go.kr에서 OC 계정의 등록 IP/도메인 확인 후 위 명령으로 재실행하고
  이 절에 규칙 ID·결과·locator·sha256을 기록.
