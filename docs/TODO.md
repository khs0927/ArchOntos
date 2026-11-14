# TODO

기준: main `7b4785f` (MVP-0~5 머지 완료, 2026-11-12). Ontology-platform은 이 커밋을
의존성으로 고정해 사용한다(Ontology-platform#133).

항목을 끝내면 이 파일에서 지우고 CHANGELOG에 기록한다.

## P0 — 실데이터 검증 (다음 단계)

- [ ] 실제 법령 E2E 실행 기록 남기기: `ops/e2e_real_law.py`(docs/E2E-REAL-LAW.md)는 준비됨.
      2026-11-14 실행에서 law.go.kr DRF가 302 → `/sorry/sorry.html`로 거부해 1단계에서 중단.
      OC 등록 IP/도메인 확인 후 재실행하고 결과를 문서에 기록

## 사용자 조치 필요

코드로는 끝낼 수 없고 사람이 실제 프로그램·데이터로 확인해야 하는 항목.

- [ ] law.go.kr Open API(open.law.go.kr) OC 계정의 등록 IP/도메인 확인 — 지금 이 PC에서
      DRF 호출이 `/sorry/sorry.html`로 리다이렉트됨
- [ ] 실제 회사 도면(DXF)으로 업로드 → 검토 → 이슈 → BCF → 보고서 확인
      (E2E 스크립트는 생성 도면 사용)
- [ ] Revit/SketchUp에서 실제로 내보낸 IFC·SketchUp JSON으로 MVP-4 매핑 검증
      (지금까지는 코드로 만든 작은 모델로만 테스트)
- [ ] 실제 IdP(Keycloak 등)와 OIDC 연동 검증 (지금은 로컬 RSA 키 테스트만)

- [ ] BCF 3.0 zip을 BIMcollab·Solibri 등 실제 뷰어에서 열어 확인
- [ ] power-cad 실제 AutoCAD에서 마커 payload 실행 확인: `to_power_cad_payload` 결과에
      문서 바인딩(`cad_bind_document`)을 하고 `cad_create_many dry_run=true`로 먼저 실행
      (키 이름은 power-cad 소스 845f63a4 기준으로 맞춤, 라이브 실행은 미확인)
- [ ] 실제 MinIO 서버로 업로드→읽기 1회 확인 (코드 경로는 클라이언트 대역으로 단위 테스트함)
- [ ] site-pipeline `parcel_facts`를 실제 필지에서 만든 값으로 `land_parcel_from_site_pipeline`에
      넣어 검토 결과 확인 (어댑터는 기록한 모양·합성 값으로만 테스트함)
- [ ] Ontology-platform에서 `archontos-stale-edges/1` 파일을 읽어 `stale_links`에 반영
      (ArchOntos 쪽 내보내기는 완료, 소비 쪽은 Ontology-platform 작업)

## P2 — 기능 보완

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
