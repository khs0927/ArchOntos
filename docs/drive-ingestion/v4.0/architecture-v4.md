# 전체 Google Drive 건축 온톨로지 · AI FDE v4.0

## 1. 검증 결론과 완성 범위

목표는 접근 가능한 Google Drive 전체를 목록화하고, 모든 도면과 건축 관련 문서의 내용·개정·출처·업무 관계를 단계적으로 온톨로지화하는 것이다. 대표 프로젝트는 파서 품질 시험용 표본이며 처리 범위의 상한이 아니다.

이번 버전은 v3.2의 동기화, 완전성 지표, 아카이브 식별자, 권한 회수, 라이선스, 단위·측정 모델을 교정한 **구현 기준 설계와 검증 가능한 데이터 계약**이다. 배포된 앱이나 전체 Drive 처리 완료를 의미하지 않는다. 실제 Drive 전수 실행·사용자 CAD/SKP 바이너리 보존성 시험·복구 훈련은 인수 게이트로 남긴다. 패키지의 validation-report.md에서 실제 실행 결과와 미실행 항목을 구분한다.

### 요구사항

- 사용자 계정으로 접근 가능한 모든 Drive 원본: My Drive, 직접 공유받은 파일, 접근 가능한 공유 드라이브, 폴더·바로가기·휴지통 상태.
- 건축 도면과 관련 문서: CAD/BIM/3D, PDF/스캔, HWP/HWPX, Office, 표·산출, 구조·소방·설비, 대장·측량, 시방·견적, 제출·보완·회신, 저장된 이메일·회의록·현장 사진, 법령·체크리스트, 참고 모델과 표준 상세.
- 압축 내부 항목도 별도 목록화하되 Drive 원본 수와 혼합 집계하지 않는다.
- 추가 유료 API 호출은 기본 0원. 기존 장비·CAD 라이선스·Drive 저장공간 사용과 전기·유지보수 비용은 별도로 본다.
- 사용자 환경인 ZWCAD 2026을 우선 검증하고, 2025 호환성과 AutoCAD 경로는 독립 시험한다. 보유 여부·현재 접속 상태는 이번에 재확인하지 않았다.

## 2. v3.2에서 교정한 결함

| 우선순위 | 문제 | v4 교정 |
|---|---|---|
| P0 | 최초 전체 스캔이 끝난 뒤 변경 수집을 시작하면 경합 구간 누락 | 스캔 전에 변경 토큰 확보, baseline 후 재생 |
| P0 | 전체 파일 수를 이미 안다고 가정한 커버리지 분모 | corpus/page 완주 증거와 관측 수를 표시; 보이지 않는 파일 수는 알 수 없음 |
| P0 | 검토 큐를 분류 완료에 포함 | 상태 등록률과 확정 분류율 분리 |
| P0 | 예외 승인과 추출 성공을 혼합 | 예외는 승인돼도 내용 추출 성공에 포함하지 않음 |
| P0 | 접근 불가 파일에도 콘텐츠 스냅샷을 요구 | 메타데이터 관측과 실제 확보 콘텐츠 분리 |
| P0 | ZIP ID+경로만으로 내부 파일 식별 | 컨테이너 스냅샷+엔트리 순번+정규화 경로 |
| P0 | 삭제/접근 상실 후 기존 파생본 노출 가능 | 원본 접근 상실 시 검색·미리보기·컨텍스트·캐시 즉시 차단 |
| P0 | SQLite·RDF 파일의 기준 데이터가 불명확 | 관계형 DB 하나를 canonical로 지정; RDF/검색은 재생성 가능한 투영 |
| P0 | ODA를 무조건 무료 경로로 가정 | 상업 이용 권리 확인 전 선택 어댑터 비활성 |
| P1 | 파일명 확인만으로 sameAs 허용 가능 | 파일/표현본 매칭에 owl:sameAs 사용 금지 |
| P1 | HWP와 HWPX, 부분 추출과 성공을 혼합 | 형식별 어댑터·기능 프로파일·부분 실패 분리 |
| P1 | 단위·복도 폭 측정 정의 부족 | 단위 확인, 유효폭 정의, 좌표 변환, 오차 구간 필요 |
| P1 | 근거 없는 30~55 인일 추정 | 실측 처리량·검토 시간으로 재산정 |

## 3. 확정 아키텍처

초기 운영 단위는 한 애플리케이션과 제한된 작업자다. 패키지 SQL 계약은 독립 시범환경용 SQLite 구현이다. 다중 사용자·동시 쓰기가 필요한 운영 환경은 PostgreSQL로 이식하고 동일 인수 시험을 다시 수행한다. 이미 운영 중인 PostgreSQL 프로젝트와 통합할 경우 그 저장소를 감사한 후 기존 canonical을 재사용한다. 다른 대화의 저장소가 완성됐다고 가정하지 않는다.

~~~mermaid
flowchart TD
    D["Drive 원본·변경 피드"] --> I["인벤토리·동기화"]
    I --> C["Canonical DB·작업 큐"]
    C --> W["형식별 추출 작업자"]
    W --> C
    C --> R["근거·주장·사람 검토"]
    R --> C
    C --> P["검색·RDF 투영"]
    P --> U["파일 검색·도면 비교·보고서"]
    A["접근권한·비용 정책"] --> W
    A --> P
~~~

| 구성요소 | 책임 |
|---|---|
| 인벤토리 수집기 | 범위 확인, 페이지 수집, 메타데이터 관측, 체크포인트 |
| 동기화 수집기 | 변경 이벤트 저장, 메타데이터 갱신, 작업 enqueue와 커서 트랜잭션 |
| canonical DB | 원본·관측·스냅샷·작업·근거·주장·검토·규칙 버전의 기준 데이터 |
| 로컬 작업자 | 다운로드·내보내기·문서 추출·CAD 변환·OCR의 자원 제한 실행 |
| 투영기 | 확정/후보 상태를 보존하며 RDF·FTS·선택적 벡터 색인 재생성 |
| 검토 UI | 프로젝트·분류·개정·법규·추출 오류를 업무 묶음으로 검토 |
| FDE 도구 | 허용된 검색/비교/근거/보고서 기능 호출, 사용자가 볼 수 있는 근거만 반환 |

Drive는 원본 파일 보관소다. 실행 중인 SQLite DB 파일을 Drive 동기화 폴더에서 직접 다중 쓰기하지 않는다. 일관된 백업과 콘텐츠 manifest를 별도 파일로 저장하며 복원 후 투영을 재생성한다.

## 4. 전체 Drive 인벤토리와 동기화 계약

### 4.1 시작 전 CapabilityManifest

계정 식별자, 실제 승인 OAuth scope, 조회 가능한 corpus, 공유 드라이브 지원, 다운로드/내보내기 가능 여부, 페이지네이션·Changes API 지원, 실행 도구 버전을 기록한다. 비밀 토큰은 manifest에 넣지 않는다.

drive.file은 사용자가 앱과 공유하거나 앱으로 연 파일 범위이므로 전체 Drive 수집을 보장하지 않는다. 메타데이터 목록은 drive.metadata.readonly, 전체 읽기·다운로드는 drive.readonly가 적합한 읽기 범위다. 실제 조직 제한은 별도 확인한다. 현재 ChatGPT 커넥터가 원시 API의 모든 기능을 제공한다고 가정하지 않는다. 기능이 없으면 구현 어댑터 요구사항으로 기록한다. [공식 scope 설명](https://developers.google.com/workspace/drive/api/guides/api-specific-auth)

### 4.2 최초 수집 순서

1. 사용자 영역과 접근 가능한 공유 드라이브 목록을 발견한다. drives.list도 마지막 페이지까지 처리한다.
2. 각 변경 피드의 시작 토큰을 **baseline 스캔 전에** 저장한다.
3. 사용자 corpus를 files.list로 열거한다. 루트 폴더 재귀만으로 대체하지 않는다. My Drive 밖에서 직접 공유받은 파일도 포함해야 한다.
4. 각 공유 드라이브는 `corpora=drive`, `driveId`, `supportsAllDrives=true`, `includeItemsFromAllDrives=true`로 별도 열거한다. 사용자 corpus와 중복되는 파일은 connector/account namespace + fileId로 결합한다. [files.list 공식 매개변수](https://developers.google.com/workspace/drive/api/reference/rest/v3/files/list)
5. 빈 결과 페이지에도 nextPageToken이 있으면 계속한다. incompleteSearch=true는 실패/재수집 상태다.
6. 시작 토큰부터 변경을 재생한다. 페이지 변경 원문, 메타데이터, 후속 작업, 다음 커서를 동일 트랜잭션으로 커밋한다.
7. 마지막 페이지의 newStartPageToken을 이후 폴링 기준으로 저장한다.
8. 범위·시각·관측 수·목록 종료·변경 추적 시점을 보고한다.

전수 인벤토리 요청은 `trashed=false`로 필터링하지 않는다. `files.list`는 기본적으로 휴지통 파일도 반환하므로 `trashed` 값을 관측·저장하고, 휴지통 이동과 복구를 변경 이력에 반영한다. 권한·관리자 정책 또는 커넥터 제한으로 해당 항목을 확인할 수 없으면 이를 숨기지 말고 범위의 미확인 한계로 보고한다. [files.list 휴지통 동작](https://developers.google.com/workspace/drive/api/reference/rest/v3/files/list)

페이지 토큰이 거부되면 해당 corpus의 scan generation을 새로 시작하고 멱등 upsert한다. 불완전 스캔에서 보이지 않은 파일을 삭제 처리하지 않는다. 공유 드라이브 신규 발견·접근 회복은 baseline을 다시 수행한다. 열거 결과는 고정 시점의 완벽한 스냅샷이 아니므로, 변경 재생으로 수렴시킨 상태를 보고한다.

### 4.3 원본 식별과 관측

원본의 안정 키는 connector/account namespace + provider fileId다. driveId, 부모·경로는 이동 가능한 위치 속성이다. 동일 파일이 드라이브 사이를 이동해도 새 원본으로 생성하지 않는다.

메타데이터 관측에는 관측 시각, 이름·MIME·확장자·부모·크기·제공된 version/revision/checksum·shortcut target·휴지통·접근 상태를 기록한다. 메타데이터만 확보했으면 ContentSnapshot을 만들지 않는다. 다운로드/내보내기 전후 version을 확인하고 변경이 감지되면 그 결과를 안정 스냅샷으로 확정하지 않고 재시도한다. 해시는 실제 확보 바이트의 해시이며 Google 문서의 논리 revision과 같다고 가정하지 않는다.

### 4.4 접근권한 변경

removed 이벤트는 삭제 확정이 아니라 접근 손실일 수도 있다. 우선 UNAVAILABLE_UNKNOWN_CAUSE로 처리하고 콘텐츠 파생물 조회를 차단한다. 감사 메타데이터 보존과 사용자 콘텐츠 열람은 별도 정책이다. 원본별 접근 경계를 해시 중복 때문에 합치지 않는다. 공유 검색에서는 후보 검색부터 권한 필터를 적용하고, 권한 불명확 자료는 답변에 사용하지 않는다. 오래된 ACL 캐시는 최신 권한의 증명이 아니다.

## 5. 분류와 완료 의미

| 수준 | 성공 기준 | 성공으로 세지 않는 것 |
|---|---|---|
| T0 인벤토리 | 발견 corpus 마지막 페이지 처리·불완전 검색 없음·변경 기준점 기록 | 미완료 corpus, API 오류 |
| T1 관련성 확정 | 관련/보조/무관 확정과 근거 기록 | REVIEW_REQUIRED, UNKNOWN |
| T2 아티팩트 등록 | SourceArtifact, MetadataObservation, 분류 주장·처리 상태·소속 후보 기록 | 필수 출처 없는 객체 |
| T3 내용 추출 | 파일 유형별 요구 기능 프로파일과 품질 검증 충족 | 부분 추출, 미지원, 손상, 접근 불가, 승인 예외 |

T2 100%는 도면의 모든 벽·문·공간을 이해했다는 뜻이 아니다. T3도 적용된 프로파일 범위의 성공이다. CAD 기본 메타데이터와 공간 의미 해석은 별도 프로파일로 선언한다. 예외 승인 시 표현은 “잔여 예외를 명시한 운영 인수”로 한다.

전체 인벤토리의 실제 외부 분모는 미리 알 수 없다. 따라서 관측 고유 파일 수, 완료 corpus/발견 corpus, 마지막 페이지 여부, incompleteSearch, 마지막 변경 토큰 처리 시각, 미해결 오류를 표시한다. 무관 판정의 층화 표본을 재검토해 건축 자료의 거짓 음성을 측정한다. 미확인 파일을 조용히 제외하면 전수 목표를 달성할 수 없다.

## 6. 형식별 추출과 추가 API 0원 경로

| 형식 | 기본 경로 | 제한 및 인수 조건 |
|---|---|---|
| DXF | ezdxf | 단위·레이아웃·XREF·중첩 블록·OCS·비균일 변환·한글 텍스트 시험 |
| DWG | ezdwg 평가 → 보유 ZWCAD의 DXF 파생본 경로 평가 → ezdxf | 모든 객체 지원/완전 호환 미보장. ZWCAD 무인 실행·2025/2026 호환 미검증 |
| SKP | OpenSKP 평가 | 프로젝트 실파일 geometry·장면·태그·재질·컴포넌트 변환·GLB 정합성 시험 |
| 텍스트 PDF | pdfminer.six 등 허용 라이선스 경로 평가 | 페이지·위치·표 순서 검증; 이미지 OCR 별도 |
| PyMuPDF | 선택 어댑터 | AGPL 또는 상용 라이선스 조건에 맞는 배포 여부 확인 |
| HWP | pyhwp 등 HWP v5 경로 평가 | AGPL 조건, 암호화·배포용·구버전·표/그림 별도 실패 상태 |
| HWPX | ZIP/XML 전용 어댑터 | 문단·표·이미지·header/section 참조 보존; HWP 파서로 대체 불가 |
| XLSX/표 | 셀 값·수식·캐시값·시트 ID와 범위 보존 | 캐시값이 최신 계산값이라고 가정하지 않음 |
| Docs/Sheets/Slides | 원본 ID + 내보내기/읽기 관측 | export 제한·지원 형식·큰 파일 실패에 대한 별도 경로 필요 |
| 이메일·이미지·압축 | 관련 후보만 형식별 추출 | 저장된 메일/첨부 범위; 안전 한도와 provenance 보존 |
| 미지원 CAD/BIM | 메타데이터 + 어댑터 대기 | 지원 등록만으로 내용 추출 완료 표시 금지 |

ODA 공식 FAQ는 비회원 사용을 비상업용으로 한정한다. 따라서 ODA File Converter는 사무소의 무조건 무료 기본 경로에서 해제하고 해당 사용권이 확인됐을 때만 활성화한다. [공식 FAQ](https://www.opendesign.com/faq/question/what-are-oda-viewer-and-oda-file-converter)

OpenSKP와 ezdwg는 실제 공개 프로젝트이나 이 사용자의 실파일 보존성은 검증되지 않았다. 버전·커밋·라이선스·의존 패키지·샘플 결과를 parser registry에 고정한 뒤 승격한다. 자동 유료 fallback은 없다. 외부 LLM 없이 목록화·규칙 분류·기본 문서 추출이 가능해야 한다.

## 7. 온톨로지·버전·근거 계약

### 7.1 계층

SourceArtifact → MetadataObservation / ContentSnapshot → LogicalDocument → DocumentRevision → IssueEvent / SubmissionPackage / ReviewRound.

프로젝트, 대지, 건물, 허가 사건을 별도 식별한다. 단계·분야·목적·현황/변경 전후·발행 상태는 독립 축이다. 날짜나 허가 폴더만으로 승인 상태를 확정하지 않는다. 프로젝트에 속하지 않는 표준 상세·법령·부품은 SharedReferenceCollection에 속할 수 있다.

Evidence는 정확한 콘텐츠 스냅샷과 추출 실행 및 locator에 연결한다. 메타데이터 기반 분류는 MetadataObservation을 근거로 하며 콘텐츠를 읽었다는 주장으로 바꾸지 않는다. RDF 출처는 PROV-O의 Entity/Activity/Agent 관계에 정렬한다.

### 7.2 주장과 매칭

RelationshipAssertion은 subject, predicate, object/value, evidence, run, candidate/accepted/rejected/stale, 검토자·일시·사유를 가진다. 승인 그래프는 이 기록으로부터 생성한다.

파일·스냅샷·PDF/DWG 표현본을 owl:sameAs로 합치지 않는다. sameContentAs, hasRepresentation, correspondsTo, derivedFrom을 구별한다. supersedes는 논리 문서·목적·분기 내에서 순환이 없는 경우에만 사용한다. 원본 변경은 관련 주장을 stale로 만들고 과거 검토 결과를 보존한다.

### 7.3 압축 내부 파일

내부 항목 키 = container_snapshot_id + entry_ordinal + normalized_path. 동일 ZIP 내부에 같은 이름이 두 번 있어도 순번으로 구분한다. 원문 경로, 정규화 경로, CRC/바이트 해시, 중첩 컨테이너 계보를 보존한다. snapshot이 바뀌면 내부 항목도 별도 버전이다.

경로 탈출·심볼릭 링크·암호화·손상·중첩 깊이·총 해제 크기·엔트리 수를 검사한다. 자체 ZIP 컨테이너인 HWPX/DOCX는 일반 압축 재귀와 구별한다.

## 8. CAD 측정·법규 검증

v3.0 단위 주석은 교정한다: INSUNITS 1=inches, 4=mm, 6=m. unitless 또는 서로 충돌하는 축척은 측정 불가로 처리한다. LWPOLYLINE의 const_width는 선의 두께이며 복도 유효폭이 아니다. 표시 치수의 text override와 실제 기하 치수도 구분한다. [단위](https://ezdxf.readthedocs.io/en/stable/concepts/units.html), [LWPOLYLINE](https://ezdxf.readthedocs.io/en/stable/dxfentities/lwpolyline.html)

MeasurementObservation은 원시 값/단위, 정규화 값/단위, 대상 속성의 정의, 측정법 버전, 좌표변환, 근거, 오차 유형·범위를 가진다. 최소값 비교에서 검증된 결정론적 오차 구간 [lower, upper]가 기준을 걸치면 NEEDS_REVIEW다. 확률적 신뢰구간을 확정 오차 경계로 오인하지 않는다.

법규는 공식 문서·관할·공포/시행일·프로젝트 기준일·경과조치·적용조건·예외·측정법·승인 규칙 버전을 저장한다. 현재 규정을 과거 허가도면에 무조건 소급하지 않는다. 규칙 적용 여부와 수치 비교 결과를 별도 기록한다.

건축법 제57조는 대지 분할 제한이며 복도 너비 조문으로 쓸 수 없다. 복도 기준은 피난·방화구조 규칙 제15조의2 등 관련 규정의 조건을 확인해야 한다. 사용자 Drive의 156개 룰 설계서는 규칙 후보 입력 자산이며 156개 운영 검증 완료를 뜻하지 않는다. OntoBPR 저장소를 KBimCode 자체 배포본이라고 단정하지 않는다. 자체 JSON 규칙 계약은 KBimCode 호환성이 검증되기 전까지 별개 계약이다.

## 9. 실행 정책과 복구

- 유료 외부 API 기본 비활성, 명시된 허용 공급자만 호출. API 키 존재로 유료 사용을 자동 허용하지 않는다.
- 작업 키: snapshot + capability/stage + parser version + configuration hash.
- claim/lease/heartbeat와 제한된 재시도, 만료 lease 재수집, dead-letter 큐. 결과 생성과 성공 상태는 원자적으로 기록한다.
- 변경 수집 트랜잭션은 추출 완료를 기다리지 않는다. enqueue 후 파서는 별도로 실행한다.
- 다운로드 bytes, OCR pages, CAD CPU time, peak RAM, scratch·파생본 용량, 사람 검토 시간을 계측한다. 예산 초과는 체크포인트 후 대기다.
- 파일 본문·도면 텍스트는 비신뢰 데이터다. 문서에 적힌 명령으로 외부 전송·권한 변경·툴 실행을 하지 않는다.
- 일관된 DB 백업, 콘텐츠 manifest, 어댑터 버전·설정, 감사 이력을 복원 가능한 묶음으로 보관한다. 복원 후 검색/RDF 투영을 재생성한다.
- 외부 접속 여부·장비 가용 시간·처리량을 측정한 뒤 일정 산정. 기존 추정 인일 숫자는 폐기한다.

## 10. 사용자 흐름과 오케스트레이션

한글 UI 우선: 전체 현황 → 건축 분류 검토함 → 프로젝트/공용자료 → 제출·보완·개정 계보 → 원본 근거 보기 → 비교/법규 검토.

여섯 작업 역할은 수집·문서추출·CAD추출·온톨로지정렬·법규후보·품질검증이다. 오케스트레이터는 멱등 작업 키와 자원 예산으로 분배한다. 여러 에이전트가 같은 원본을 독립적으로 다시 내려받거나 승인 상태를 서로 덮어쓰지 않는다. 처리 결과는 후보 주장으로 들어오고 별도 검증에서 승격한다. 병렬 수는 실제 CPU/RAM/API 제한으로 결정하며 에이전트 수 증가를 성능 보장으로 표현하지 않는다.

검색 결과는 원본 링크, 제목, 페이지/시트, 스냅샷·추출 시각, 최신 여부와 검토 상태를 표시한다. 허가 기준본과 실시 발행본을 “가장 최근 파일” 하나로 대체하지 않는다.

## 11. 출시 게이트

| 게이트 | 산출 | 필수 통과 |
|---|---|---|
| G0 계약 검증 | SQL/JSON Schema/RDF·SHACL | 잘못된 참조·중복 작업·불완전 성공 거부 |
| G1 Drive 인벤토리 | 실제 corpus 목록·페이지·변경 체크포인트 | 스캔 중 변경·권한 회수·재시작 시험 |
| G2 문서 온톨로지 | 관련 파일 T2, 문서 프로파일별 T3 | 거짓 음성 표본 평가·출처 locator·예외 건수 |
| G3 CAD/SKP | 실제 파일·버전별 추출 기록 | 기하·단위·XREF·시트·한글·렌더 원본 대조 |
| G4 업무 인수 | 검색·계보·규칙 후보·복구 | 권한 시험·백업 복원·기준본 구별·사람 승인 |

출시 범위는 프로파일별로 명시한다. G0만 통과한 상태를 전체 운영 완료라고 표시하지 않는다. 상세 시나리오는 acceptance.md에 있다.

## 12. 실측 기반 일정과 다음 실행

1. 실제 연결의 scope·API 기능·실행 호스트 확인.
2. 시작 토큰 확보 후 전체 corpus 전수 메타데이터 수집.
3. 타입·크기·스캔 비율·CAD 버전·압축 내부 항목 분포 측정.
4. 각 형식과 프로젝트를 층화해 샘플을 선택하고 원본 대조.
5. 검증된 어댑터를 전체 관련 파일에 배치 실행.
6. 낮은 신뢰도와 같은 원인의 오류를 묶어 검토하고 변경 동기화 운영.
7. 복구 시험 후 프로파일별 운영 인수.

예상 시간 = 형식별 미처리량 ÷ 실측 처리량 + 재시도·사람 검토 시간. 사용 가능한 Drive 용량 3TB를 실제 저장된 파일량으로 가정하지 않는다.

## 참고 문서

- [Drive 목록](https://developers.google.com/workspace/drive/api/reference/rest/v3/files/list), [변경](https://developers.google.com/workspace/drive/api/reference/rest/v3/changes/list), [공유 드라이브](https://developers.google.com/workspace/drive/api/guides/enable-shareddrives)
- [OAuth 범위](https://developers.google.com/workspace/drive/api/guides/api-specific-auth)
- [PROV-O](https://www.w3.org/TR/prov-o/), [OWL 2](https://www.w3.org/TR/owl2-primer/)
- [ezdwg](https://github.com/monozukuri-ai/ezdwg), [OpenSKP](https://github.com/iamahsanmehmood/openskp)
- [ZWCAD 개발 문서](https://www.zwsoft.com/support/zwcad-devdoc)
- [HWP 파서](https://github.com/mete0r/pyhwp), [HWPX 공식 구조](https://tech.hancom.com/hwpxformat/)
- [PyMuPDF 라이선스](https://pymupdf.readthedocs.io/en/latest/faq/index.html), [pdfminer.six](https://github.com/pdfminer/pdfminer.six)
- [건축법 제57조](https://www.law.go.kr/LSW/lsLinkCommonInfo.do?ancYnChk=&chrClsCd=010202&lsJoLnkSeq=1019308269)

