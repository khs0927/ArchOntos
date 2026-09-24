# ArchOntos Upgrade Package

기준일: 2026-09-25

목표: MVP-0를 `source_version → evidence_span → assertion → rule → evaluation → decision`까지 완성하고, 이후 CAD·프로젝트 온톨로지로 확장 가능한 운영 기반을 만든다.

## 원칙
- PostgreSQL이 canonical truth다.
- Evidence와 Assertion을 분리한다.
- Authority와 Confidence를 분리한다.
- PASS/FAIL은 deterministic rule engine만 판정한다.
- 모든 파생 projection은 rebuildable이어야 한다.
- 외부 변경은 제안·승인·실행의 3단계로 제한한다.

## 실행 순서
1. 법령 normalizer와 evidence span writer
2. assertion 모델·검토 workflow
3. rule compiler와 applicability resolver
4. 5개 query executor
5. MVP-0 E2E 및 운영 hardening
6. pgvector projection
7. DXF/CAD MVP-1

이 문서는 2026-09-25 업그레이드 패키지의 기준 설명서다.
