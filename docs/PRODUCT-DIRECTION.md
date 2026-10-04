# Product Direction

## 제품 정의

ArchOntos는 건축설계사무소를 위한 evidence-backed Architecture Intelligence 플랫폼이다. 첫 제품은 범용 AI가 아니라 “법규 판단의 근거와 시간축을 보존하는 규정 fabric”이어야 한다.

## MVP-0 사용자 가치

1. 특정 건축물·관할·기준일에 적용되는 규정을 찾는다.
2. 서울과 부산 등 관할 차이를 비교한다.
3. 과거 허가 시점과 현재 기준을 비교한다.
4. 법률·시행령·시행규칙·조례의 authority를 구분한다.
5. 조항과 별표 원문을 즉시 확인한다.

## 성공지표

- 근거 없는 답변 0건
- evidence locator 포함 응답률 100%
- 동일 fixture 재처리 결과 일치율 100%
- 미검토 assertion의 자동 PASS/FAIL 0건
- 대표 질의의 응답 지연시간과 정확도 baseline 확보

## 사용하지 않을 약속

- “AI가 허가 적합성을 보장한다”라고 말하지 않는다.
- GraphRAG를 판정 엔진으로 사용하지 않는다.
- CAD 원본을 자동 덮어쓰지 않는다.
- AI confidence를 법적 authority로 표현하지 않는다.

## 이후 확장

MVP-1 DXF/CAD object normalization → MVP-2 regulation-to-CAD checks → MVP-3 permit/construction revision diff → MVP-4 IFC/SketchUp mapping → MVP-5 BCF/report/marker/action.
