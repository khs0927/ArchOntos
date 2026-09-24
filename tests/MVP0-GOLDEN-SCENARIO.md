# MVP-0 Golden Scenario

1. 공식 law.go.kr 응답을 RawSourceEnvelope로 보존한다.
2. canonical JSON을 hash하고 immutable artifact로 저장한다.
3. source_document/source_version을 생성한다.
4. 조·항·호·목·별표 locator를 evidence span으로 만든다.
5. assertion candidate를 만들고 reviewer가 승인한다.
6. 승인 assertion을 제한 DSL rule로 compile한다.
7. 기준일과 관할을 입력해 query를 실행한다.
8. evaluation과 decision을 생성한다.
9. 응답에서 decision→evidence→artifact→source version을 역추적한다.
10. 같은 fixture를 다시 처리해 중복과 결과 변동이 없는지 확인한다.
