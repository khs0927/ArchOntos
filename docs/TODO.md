# TODO

기준: main `7b4785f` (MVP-0~5 머지 완료, 2026-11-12). Ontology-platform은 이 커밋을
의존성으로 고정해 사용한다(Ontology-platform#133).

항목을 끝내면 이 파일에서 지우고 CHANGELOG에 기록한다.

## P0 — 실데이터 검증 (다음 단계)

- [ ] 실제 법령 규칙 몇 개(예: 복도 폭, 계단 폭, 실 면적)를 assertion → 승인 → 규칙으로
      만들고, 실제 회사 도면으로 업로드 → 검토 → 이슈 → BCF → 보고서까지 E2E 실행
- [ ] Revit/SketchUp에서 실제로 내보낸 IFC·SketchUp JSON으로 MVP-4 매핑 검증
      (지금까지는 코드로 만든 작은 모델로만 테스트)
- [ ] 실제 IdP(Keycloak 등)와 OIDC 연동 검증 (지금은 로컬 RSA 키 테스트만)
- [ ] 로컬 PostgreSQL 통합 테스트 실행 (그동안 PC 자원 문제로 CI에서만 확인)

## P1 — 연동 확인

- [ ] BCF 3.0 zip을 BIMcollab·Solibri 등 실제 뷰어에서 열어 확인
- [ ] `markers.to_power_cad_payload` 키 이름을 power-cad `cad_create_many` 실제 스키마와 대조
- [ ] MinIO 저장소 `get_bytes` 테스트 (CI는 Local 저장소만 사용)
- [ ] site-pipeline `parcel_facts` → `land_parcel` 입력을 실제 필지로 검증
- [ ] Ontology로 stale edge 내보내기 (지금은 API로만 stale 확인 가능)

## P2 — 기능 보완

- [ ] 객체 쌍 규칙(문-벽, 계단-층) 지원 — 지금은 REVIEW 처리
- [ ] 유효폭 계산 개선 — 지금은 bbox 근사값
- [ ] 벽 분할/병합을 리비전 diff에서 인식 — 지금은 삭제+추가로 표시
- [ ] 층/건물 사실(`building.use_group` 등) DXF에서 추출 — IFC는 층수만 자동
- [ ] IFC·SketchUp 리비전에도 도면 마커 적용 — 지금은 DXF만, 나머지는 BCF로만
- [ ] `cad_issue_note` API (테이블만 있음)
- [ ] BCF import (토픽 상태·코멘트를 검토 메모로)
- [ ] marker execute를 단일 트랜잭션으로 (지금은 출력 저장 → 액션 succeeded 순서, 멱등)
- [ ] 리비전 diff 이후 이슈 resolved 판정을 `rerun_checks` 없이도 가능하게
- [ ] DXF↔IFC 형상 기반 자동 링크, IfcOpeningElement 처리
- [ ] SketchUp Ruby exporter 스크립트, Revit→IFC 자동화

## P3 — 운영

- [ ] 실제 서버 배포, GitOps 조정(reconciliation)
- [ ] 백업 스케줄링, PITR/WAL 아카이빙, MinIO 버킷 백업
- [ ] 토큰 폐기/인트로스펙션
- [ ] CI 백업/복구 단계(PGDG 클라이언트 설치) 정기 모니터링

## 관련 저장소 정리

- [ ] `C:\CODE\Ontology`(옛 사본)에 남은 ArchOntos 복사본 정리 또는 보관 결정
- [ ] Ontology-platform: ArchOntos 새 릴리스마다 고정 커밋 갱신
      (pyproject + uv.lock + docs/INTEGRATION_CONTRACTS.md 동시 갱신, 복사본 금지 가드 유지)
- [ ] aec-mcp-router: mcp 2.x 호환(또는 `mcp<2` 고정)으로 기동 실패 해결,
      regulation 네임스페이스 포트(8019/8020)와 building-regulation-gateway 포트 정합
- [ ] power-cad `POWERCAD_ONTOLOGY_URL` 구성
