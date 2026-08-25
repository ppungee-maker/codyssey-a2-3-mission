# 📊 [Project C] AI 기반 고객 리뷰 감정 분석 대시보드 종합 실행 계획서

**미션명**: [Project C] AI 기반 고객 리뷰 감정 분석 대시보드  
**분야 / 구분**: AI 활용 학습 / AI 활용  
**학습시간**: 40시간  
**기본 환경**: Python 3.10+, SQLite, CLI (argparse 기반)

---

## 1. 프로젝트 아키텍처 및 모듈 분리 설계

단일 파일 작성을 지양하고 기능별 독립성과 확장성을 위해 **6개 모듈**로 구조화합니다.

```text
review_dashboard/
├── config/
│   ├── config.json            # API 키, 중복 정책, 임계치, 한글 폰트 경로 설정
│   └── settings.py            # 설정 로더 및 logging 모듈 초기화
├── storage/
│   ├── database.py            # SQLite 연결 및 테이블 관리 (raw, clean, analysis, summary)
│   └── models.py              # 데이터 스키마 및 DTO 정의
├── core/
│   ├── cleaner.py             # 데이터 정제, 정규화, 유효성 검증 및 중복 처리(skip/upsert)
│   ├── ai_service.py          # AI API 연동 (감정 분석, 다국어 처리, 키워드/요약 추출)
│   └── visualizer.py          # matplotlib 차트 생성, 한글 폰트 적용, HTML 대시보드 빌더
├── handlers/
│   ├── query_handler.py       # list, show, stats, compare 로직 처리
│   └── export_handler.py      # CSV, Excel, JSONL 내보내기 처리
├── data/
│   └── sample_reviews.csv     # 30건 이상의 테스트용 샘플 데이터 (다국어 포함)
├── output/                    # 생성된 차트(PNG), 리포트(TXT/MD/HTML), Export 파일 저장소
├── main.py                    # argparse 기반 CLI 서브커맨드 엔트리포인트
├── requirements.txt           # 프로젝트 의존성 (pandas, matplotlib, openpyxl 등)
└── README.md                  # 설치, 실행 가이드 및 서브커맨드 매뉴얼
```

---

## 2. 단계별 세부 실행 계획 (보너스 과제 포함)

### Phase 1: 개발 환경 구축 및 영구 저장소(DB) 설계
1. **의존성 환경 구성 (`requirements.txt`)**
   - `pandas`, `openpyxl`, `matplotlib`, `requests` (또는 공식 AI SDK), `python-dotenv`
2. **설정 및 보안 관리 (`config/`)**
   - `config.json` 및 `.env` 파일로 AI API Key, DB 경로, 한글 폰트 설정 관리 (코드 내 하드코딩 금지).
   - `logging` 모듈로 `INFO`, `WARNING`, `ERROR` 레벨 로깅 체계 구현.
3. **SQLite 영구 저장소 테이블 스키마 설계 (`storage/database.py`)**
   - `raw_reviews`: 원본 데이터 수집 테이블 (`id`, `source_file`, `raw_text`, `rating`, `review_date`, `product_name`, `created_at`).
   - `clean_reviews`: 정제 완료 테이블 (`id`, `raw_id`, `cleaned_text`, `rating`, `review_date`, `product_name`, `language`).
   - `sentiment_results`: 감정 분석 결과 (`id`, `review_id`, `sentiment`, `confidence_score`, `analyzed_at`).
   - `insight_summaries`: 키워드 및 요약 결과 (`id`, `target_type`, `keywords_json`, `summary_text`, `action_items`, `created_at`).

---

### Phase 2: 데이터 수집 및 정제 파이프라인 (`import`, `clean`)
1. **리뷰 데이터 수집 서브커맨드 (`main.py import`)**
   - CSV 및 Excel(`.xlsx`) 파일 로드.
   - 필수 필드(`review_text`) 누락 검증.
   - `raw_reviews` 적재 시 중복 정책(`--duplicate-policy=skip` 또는 `upsert`) 처리.
2. **데이터 정제 엔진 (`main.py clean`)**
   - 빈 텍스트 제거, 최소 길이(3자 미만) 필터링.
   - 불필요한 특수문자, HTML 태그 정규화.
   - 별점 범위(1.0 ~ 5.0) 검증 및 날짜 형식(`YYYY-MM-DD`) 표준화.
   - 정제 완료 데이터를 `clean_reviews` 테이블로 적재.

---

### Phase 3: AI 감정 분석 및 인사이트 추출 엔진 (`analyze`, `extract`)
1. **AI 기반 감정 분석 (`main.py analyze`)**
   - 감정(긍정/부정/중립) 및 신뢰도 점수(0.0 ~ 1.0) 도출.
   - 옵션: `--all`, `--id <id>`, `--unanalyzed`, `--limit <n>`.
   - 기분석 데이터 기본 스킵 및 API 오류 발생 시 로깅 후 건너뛰기(Fault-tolerant).
2. **[보너스 1] 다국어 감정 분석 지원**
   - 한국어 외 영어(English) 리뷰 자동 언어 감지 및 동일 규격 감정 분석 수행.
3. **AI 기반 키워드 및 요약 추출 (`main.py extract`)**
   - 필터 조건(`--sentiment`, `--product`, `--date-from/to`)별 집계 분석.
   - 필수 추출 항목: 긍정/부정 주요 키워드, 총평 요약, 비즈니스 개선 제안점(Action Items).
   - 결과를 `insight_summaries` 테이블에 영구 저장.

---

### Phase 4: CLI 데이터 조회 및 통계 엔진 (`list`, `show`, `stats`)
1. **리뷰 목록 조회 (`main.py list`)**
   - 필터링: `--sentiment`, `--rating`, `--date-from`, `--date-to`, `--product`.
   - 페이지네이션(`--page`, `--size`) 및 정렬 옵션 지원.
2. **리뷰 상세 조회 (`main.py show`)**
   - `--id <id>`: 원문, 정제 텍스트, 메타데이터, AI 분석 결과 콘솔 출력.
3. **통계 요약 출력 (`main.py stats`)**
   - 총 리뷰 수, 분석 완료율, 감정별 건수/비율, 별점 분포(1~5점), 평균 별점, 평균 감정 점수 집계.

---

### Phase 5: 대시보드 시각화, 리포트 생성 및 모니터링 (`dashboard`)
1. **matplotlib 기반 정적 차트 3종 생성 (`core/visualizer.py`)**
   - 한글 폰트(NanumGothic / Malgun Gothic) 자동 적용.
   - `sentiment_distribution.png`: 긍정/중립/부정 도넛 차트.
   - `sentiment_trend.png`: 날짜별 감정 변화 추이 꺾은선 그래프.
   - `rating_sentiment_matrix.png`: 별점별 감정 분포 누적 막대 차트.
2. **종합 리포트 생성**
   - 품질 지표(완료율, 긍정 지수), TOP N 키워드, AI 인사이트 요약 포함.
   - 콘솔 출력 및 파일(`output/report.txt`, `output/report.md`) 자동 저장.
3. **[보너스 2] 감정 변화 급증 알림 기능 (Alert)**
   - 최근 $N$일간 부정 리뷰 비율이 설정된 임계치(예: 30% 이상)를 초과할 경우 `[WARNING] 부정 리뷰 급증 감지` 알림 출력.
4. **[보너스 3] 단일 HTML 대시보드 생성**
   - `output/dashboard.html`: 요약 KPI 카드, 차트 이미지 임베딩, AI 요약 텍스트 및 경고 배너를 포함한 단일 반응형 HTML 파일 생성.

---

### Phase 6: 데이터 내보내기 및 비교 분석 (`export`, 보너스 4)
1. **다중 포맷 데이터 Export (`main.py export`)**
   - 지원 포맷: `CSV`, `JSONL`, `Excel(.xlsx)` (3종 포맷 지원).
   - 필터 옵션: `--sentiment`, `--rating-min`, `--format`.
2. **[보너스 4] 제품/카테고리별 비교 분석 기능 (`main.py compare`)**
   - `--products "제품A,제품B"` 옵션을 통해 제품 간 평균 별점, 긍정 비율, 키워드 비교.
   - `output/product_comparison.png` 비교 차트 생성 및 리포트 출력.

---

### Phase 7: 종합 테스트 및 제출 패키징
1. **테스트용 샘플 데이터셋 구축 (`data/sample_reviews.csv`)**
   - 30건 이상 (한국어 35건, 영어 10건 등 총 45건 이상, 중복/결측치/비정상값 포함).
2. **CLI 서브커맨드 E2E 검증 표**

| 서브커맨드 | 실행 명령어 예시 | 검증 항목 |
| :--- | :--- | :--- |
| `import` | `python main.py import --file data/sample_reviews.csv` | Raw DB 적재 및 중복 카운트 검증 |
| `clean` | `python main.py clean` | 정제 규칙 적용 및 Clean DB 적재 |
| `analyze` | `python main.py analyze --unanalyzed --limit 50` | AI 감정/점수 산출 및 다국어(영어) 분석 검증 |
| `extract` | `python main.py extract --sentiment negative` | 불만 키워드, 요약, 개선 제안 추출 |
| `list` | `python main.py list --sentiment negative --page 1 --size 5` | 조건 필터링 및 페이지네이션 동작 |
| `show` | `python main.py show --id 1` | 단건 상세 메타데이터 및 분석 결과 출력 |
| `stats` | `python main.py stats` | 전체 통계 요약 집계 정밀도 검증 |
| `dashboard` | `python main.py dashboard --alert-days 7` | PNG 차트 3종, MD/HTML 대시보드, 알림 기능 확인 |
| `compare` | `python main.py compare --products "A,B"` | 제품 간 비교 통계 및 차트 생성 검증 |
| `export` | `python main.py export --format excel --sentiment positive` | 엑셀/CSV/JSONL 파일 정상 내보내기 확인 |

---

## 3. 최종 산출물 체크리스트

- [ ] **Python CLI 소스 코드**: 6개 모듈로 분리된 Python 애플리케이션
- [ ] **영구 저장소**: SQLite DB 파일 (`reviews.db`) 내 4개 분리 테이블
- [ ] **설정 파일**: `config/config.json`, `.env.example`
- [ ] **샘플 데이터셋**: `data/sample_reviews.csv` (최소 30건 이상, 다국어 및 예외 케이스 포함)
- [ ] **시각화 및 리포트 파일**:
  - PNG 차트 3종 (`sentiment_distribution.png`, `sentiment_trend.png`, `rating_sentiment_matrix.png`)
  - 마크다운 및 텍스트 리포트 (`report.md`, `report.txt`)
  - 단일 HTML 대시보드 파일 (`dashboard.html`)
- [ ] **README.md**: 환경 설정법, 라이브러리 설치, 전체 CLI 명령어 사용 가이드
