# Query Contracts

모든 query response는 다음을 포함한다.

- query intent
- effective date
- jurisdiction
- authority class
- source document/version
- evidence locator
- assertion/rule/evaluation/decision identifiers
- review status

## MVP-0 intents

- applicability
- jurisdiction_comparison
- temporal_comparison
- authority_classification
- source_evidence

근거가 충분하지 않으면 답변 대신 REVIEW 또는 NOT_FOUND를 반환한다.
