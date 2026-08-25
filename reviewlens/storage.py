"""영구 저장소 — SQLite. raw 와 clean 을 다른 테이블로 나눈다.

**왜 나누나**: 리뷰는 한 번 받으면 다시 받기 어렵다(고객이 지우거나 쇼핑몰이 페이지를
내린다). 정제 규칙(짧은 리뷰 걸러내기·별점 범위)은 나중에 바뀔 수 있는데, 원본을 덮어쓰면
규칙을 되돌릴 수 없다. **수집은 되돌릴 수 없고 정제는 되돌릴 수 있다** — 그 경계를 테이블
경계로 만든 것이다.

메모리를 쓰지 않는 이유: 서브커맨드가 10개인데 각각 별도 프로세스로 실행된다
(`import` 다음에 `clean`, 그다음 `analyze`). 프로세스가 끝나면 메모리는 사라지므로
**디스크가 아니면 단계 사이를 이을 수 없다.**
"""

from __future__ import annotations

import json
import sqlite3
from contextlib import contextmanager
from pathlib import Path
from typing import Iterator

SCHEMA = """
-- 수집 원본. 어디서·언제·어떻게 받았는지를 함께 남긴다.
CREATE TABLE IF NOT EXISTS raw_reviews (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    ingested_at  TEXT NOT NULL,   -- 적재 시각 (ISO8601)
    source       TEXT NOT NULL,   -- 파일 경로 또는 'manual'(add 명령)
    method       TEXT NOT NULL,   -- 'csv' | 'manual'
    payload      TEXT NOT NULL    -- 원본 행 전체(JSON)
);

-- 정제 결과. review_id 가 멱등키다(UNIQUE).
CREATE TABLE IF NOT EXISTS clean_reviews (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    raw_id       INTEGER,
    review_id    TEXT NOT NULL UNIQUE,   -- 원본 리뷰 식별자 = 중복 판정 기준
    product      TEXT,
    rating       INTEGER,                -- 1~5, 범위 밖이면 NULL
    created_at   TEXT,                   -- ISO8601(YYYY-MM-DD), 파싱 실패 시 NULL
    text         TEXT NOT NULL,
    language     TEXT,                   -- 'ko' | 'en' (보너스: 다국어)
    sentiment    TEXT,                   -- 긍정/중립/부정 (analyze 가 채움)
    confidence   REAL,                   -- 0.0~1.0
    cleaned_at   TEXT NOT NULL,
    analyzed_at  TEXT,
    FOREIGN KEY (raw_id) REFERENCES raw_reviews(id)
);

-- 조건별 AI 추출 결과(키워드·요약·개선 제안). 대시보드가 읽는다.
CREATE TABLE IF NOT EXISTS extractions (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    created_at  TEXT NOT NULL,
    scope       TEXT NOT NULL,   -- 어떤 조건으로 뽑았는지(사람이 읽는 문장)
    item_count  INTEGER NOT NULL,
    result      TEXT NOT NULL    -- 추출 결과(JSON)
);

-- 개선 피드백 루프. 부정 키워드 하나 = 액션 하나.
--
-- **왜 별도 테이블인가**: extractions 는 "AI 가 그때 뭐라고 했나"의 기록이고, actions 는
-- "우리가 무엇을 고치기로 했고 그 뒤 어떻게 됐나"의 기록이다. 조치 시행 시점(opened_at)과
-- 그 직전 지표(baseline_*)를 남겨 두지 않으면 나중에 **개선 전/후를 가를 기준선이 없다**.
CREATE TABLE IF NOT EXISTS actions (
    id                INTEGER PRIMARY KEY AUTOINCREMENT,
    opened_at         TEXT NOT NULL,          -- 액션 등록 = 조치 착수 시각
    keyword           TEXT NOT NULL UNIQUE,   -- 추적 대상 부정 키워드(멱등키)
    priority          REAL NOT NULL,          -- 우선순위 점수 0~100 (feedback.priority_score)
    metric            TEXT NOT NULL,          -- 추적 지표 이름(무엇으로 성패를 재는가)
    baseline_ratio    REAL,                   -- 조치 직전 창의 부정 비율
    baseline_mentions INTEGER,                -- 조치 직전 창의 언급 건수
    baseline_from     TEXT,
    baseline_to       TEXT,
    cadence_days      INTEGER NOT NULL,       -- 재측정 주기(일)
    status            TEXT NOT NULL,          -- open | closed
    closed_at         TEXT
);

CREATE INDEX IF NOT EXISTS idx_clean_created ON clean_reviews(created_at);
CREATE INDEX IF NOT EXISTS idx_clean_product ON clean_reviews(product);
CREATE INDEX IF NOT EXISTS idx_clean_sentiment ON clean_reviews(sentiment);
"""


@contextmanager
def connect(db_path: str) -> Iterator[sqlite3.Connection]:
    """DB 연결을 열고 **반드시 닫는다**.

    `with sqlite3.connect(...)` 만 쓰면 트랜잭션만 관리되고 연결은 안 닫힌다 —
    반복 호출하면 파일 디스크립터가 쌓인다.
    """
    Path(db_path).parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row  # 컬럼명으로 접근 — 순서가 바뀌어도 안 깨진다
    try:
        yield conn
        conn.commit()
    finally:
        conn.close()


def init_db(db_path: str) -> None:
    """테이블·인덱스를 만든다. 이미 있으면 아무 일도 하지 않는다."""
    with connect(db_path) as conn:
        conn.executescript(SCHEMA)


def insert_raw(conn: sqlite3.Connection, record: dict) -> int:
    """원본 1건 저장 → raw_id. 검증하지 않는다 — raw 는 '받은 그대로'가 원칙이다."""
    cursor = conn.execute(
        "INSERT INTO raw_reviews (ingested_at, source, method, payload) VALUES (?, ?, ?, ?)",
        (
            record["ingested_at"],
            record["source"],
            record["method"],
            json.dumps(record["payload"], ensure_ascii=False),
        ),
    )
    return int(cursor.lastrowid or 0)


def fetch_raw(conn: sqlite3.Connection, only_uncleaned: bool = True) -> list[sqlite3.Row]:
    """정제 대상 raw 목록. 기본은 아직 clean 으로 넘어가지 않은 것만."""
    if only_uncleaned:
        sql = """SELECT * FROM raw_reviews
                 WHERE id NOT IN (SELECT raw_id FROM clean_reviews WHERE raw_id IS NOT NULL)
                 ORDER BY id"""
    else:
        sql = "SELECT * FROM raw_reviews ORDER BY id"
    return list(conn.execute(sql))


def upsert_clean(conn: sqlite3.Connection, item: dict, policy: str = "skip") -> str:
    """정제 1건 저장 → 'inserted' | 'skipped' | 'updated'.

    중복 판정은 **review_id** 로 한다. 리뷰 본문이 같아도 다른 사람이 쓴 다른 리뷰일 수
    있고("배송 빨라요" 는 흔하다), 같은 사람이 수정한 리뷰는 id 가 유지된다.

    policy:
      skip   — 이미 있으면 손대지 않는다. **이미 분석한 감정 결과를 지우지 않는다.**
      upsert — 본문·별점을 새 값으로 갱신한다(고객이 리뷰를 수정한 경우).
    """
    existing = conn.execute(
        "SELECT id FROM clean_reviews WHERE review_id = ?", (item["review_id"],)
    ).fetchone()
    if existing:
        if policy == "skip":
            return "skipped"
        conn.execute(
            """UPDATE clean_reviews
               SET product = ?, rating = ?, created_at = ?, text = ?, language = ?, cleaned_at = ?
               WHERE id = ?""",
            (
                item.get("product"),
                item.get("rating"),
                item.get("created_at"),
                item["text"],
                item.get("language"),
                item["cleaned_at"],
                existing["id"],
            ),
        )
        return "updated"

    conn.execute(
        """INSERT INTO clean_reviews
           (raw_id, review_id, product, rating, created_at, text, language, cleaned_at)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
        (
            item.get("raw_id"),
            item["review_id"],
            item.get("product"),
            item.get("rating"),
            item.get("created_at"),
            item["text"],
            item.get("language"),
            item["cleaned_at"],
        ),
    )
    return "inserted"


def _where_clause(
    sentiment: str | None,
    rating: int | None,
    rating_min: int | None,
    product: str | None,
    date_from: str | None,
    date_to: str | None,
    status: str | None,
) -> tuple[str, list]:
    """조건 → (WHERE 절, 파라미터).

    목록 조회와 건수 세기가 **같은 조건**을 쓰도록 뽑아냈다. 둘이 어긋나면 "3페이지 중
    2페이지"인데 3페이지가 비는 일이 생긴다.

    조건을 문자열로 이어 붙이지 않고 **파라미터 바인딩(?)** 을 쓴다 — 값에 따옴표가
    들어가면 쿼리가 깨지고, 외부 입력이라면 SQL 주입이 된다.
    """
    sql = " WHERE 1=1"
    params: list = []
    if sentiment:
        sql += " AND sentiment = ?"
        params.append(sentiment)
    if rating is not None:
        sql += " AND rating = ?"
        params.append(rating)
    if rating_min is not None:
        sql += " AND rating >= ?"
        params.append(rating_min)
    if product:
        sql += " AND product = ?"
        params.append(product)
    if date_from:
        sql += " AND created_at >= ?"
        params.append(date_from)
    if date_to:
        sql += " AND created_at <= ?"
        params.append(date_to)
    if status == "analyzed":
        sql += " AND sentiment IS NOT NULL"
    elif status == "unanalyzed":
        sql += " AND sentiment IS NULL"
    return sql, params


# 정렬 허용 목록 — 사용자 입력을 SQL 에 그대로 넣지 않기 위해 **미리 정한 값만** 받는다.
SORT_COLUMNS = {
    "date": "created_at",
    "rating": "rating",
    "confidence": "confidence",
    "id": "id",
}


def select_clean(
    conn: sqlite3.Connection,
    *,
    sentiment: str | None = None,
    rating: int | None = None,
    rating_min: int | None = None,
    product: str | None = None,
    date_from: str | None = None,
    date_to: str | None = None,
    status: str | None = None,
    sort: str = "date",
    desc: bool = True,
    limit: int | None = None,
    offset: int = 0,
) -> list[sqlite3.Row]:
    """조건으로 clean 목록을 뽑는다. 정렬·페이지네이션 지원."""
    where, params = _where_clause(sentiment, rating, rating_min, product, date_from, date_to,
                                  status)
    column = SORT_COLUMNS.get(sort, "created_at")
    direction = "DESC" if desc else "ASC"
    sql = f"SELECT * FROM clean_reviews{where} ORDER BY {column} {direction}, id {direction}"
    if limit:
        sql += " LIMIT ?"
        params.append(limit)
        if offset:
            # SQLite 는 OFFSET 을 LIMIT 뒤에만 받는다 — 순서를 바꾸면 문법 오류다.
            sql += " OFFSET ?"
            params.append(offset)
    return list(conn.execute(sql, params))


def count_clean(conn: sqlite3.Connection, **filters) -> int:
    """같은 조건의 총 건수 — 페이지 수 계산에 쓴다."""
    where, params = _where_clause(
        filters.get("sentiment"), filters.get("rating"), filters.get("rating_min"),
        filters.get("product"), filters.get("date_from"), filters.get("date_to"),
        filters.get("status"),
    )
    row = conn.execute("SELECT COUNT(*) FROM clean_reviews" + where, params).fetchone()
    return int(row[0]) if row else 0


def get_clean(conn: sqlite3.Connection, review_id: str) -> sqlite3.Row | None:
    """상세 조회 — review_id 하나. 없으면 None."""
    return conn.execute(
        "SELECT * FROM clean_reviews WHERE review_id = ?", (review_id,)
    ).fetchone()


def save_sentiment(conn: sqlite3.Connection, row_id: int, sentiment: str, confidence: float,
                   at: str) -> None:
    """감정·신뢰도를 채운다. analyzed_at 으로 '언제 분석했는지'를 남긴다."""
    conn.execute(
        "UPDATE clean_reviews SET sentiment = ?, confidence = ?, analyzed_at = ? WHERE id = ?",
        (sentiment, confidence, at, row_id),
    )


def save_extraction(conn: sqlite3.Connection, scope: str, item_count: int, result: dict,
                    at: str) -> int:
    """추출 결과 1건 저장 → id."""
    cursor = conn.execute(
        "INSERT INTO extractions (created_at, scope, item_count, result) VALUES (?, ?, ?, ?)",
        (at, scope, item_count, json.dumps(result, ensure_ascii=False)),
    )
    return int(cursor.lastrowid or 0)


def latest_extraction(conn: sqlite3.Connection) -> sqlite3.Row | None:
    """가장 최근 추출 1건. 대시보드·리포트가 이걸 싣는다."""
    return conn.execute("SELECT * FROM extractions ORDER BY id DESC LIMIT 1").fetchone()


def counts(conn: sqlite3.Connection) -> dict:
    """품질 지표용 집계."""
    def one(sql: str) -> int:
        row = conn.execute(sql).fetchone()
        return int(row[0]) if row else 0

    row = conn.execute("SELECT AVG(rating) FROM clean_reviews WHERE rating IS NOT NULL").fetchone()
    return {
        "raw": one("SELECT COUNT(*) FROM raw_reviews"),
        "clean": one("SELECT COUNT(*) FROM clean_reviews"),
        "analyzed": one("SELECT COUNT(*) FROM clean_reviews WHERE sentiment IS NOT NULL"),
        "with_rating": one("SELECT COUNT(*) FROM clean_reviews WHERE rating IS NOT NULL"),
        "with_date": one("SELECT COUNT(*) FROM clean_reviews WHERE created_at IS NOT NULL"),
        "avg_rating": round(row[0], 2) if row and row[0] is not None else None,
    }


def group_by_sentiment(conn: sqlite3.Connection, product: str | None = None) -> list[tuple[str, int]]:
    """감정별 건수 — 차트 1번. product 를 주면 그 제품만."""
    sql = """SELECT sentiment AS s, COUNT(*) AS n FROM clean_reviews
             WHERE sentiment IS NOT NULL"""
    params: list = []
    if product:
        sql += " AND product = ?"
        params.append(product)
    sql += " GROUP BY s ORDER BY n DESC"
    return [(r["s"], r["n"]) for r in conn.execute(sql, params)]


def group_by_date_sentiment(conn: sqlite3.Connection) -> list[tuple[str, str, int]]:
    """(날짜, 감정, 건수) — 차트 2번(시간별 추이)."""
    rows = conn.execute(
        """SELECT created_at AS d, sentiment AS s, COUNT(*) AS n FROM clean_reviews
           WHERE created_at IS NOT NULL AND sentiment IS NOT NULL
           GROUP BY d, s ORDER BY d"""
    )
    return [(r["d"], r["s"], r["n"]) for r in rows]


def group_by_rating_sentiment(conn: sqlite3.Connection) -> list[tuple[int, str, int]]:
    """(별점, 감정, 건수) — 차트 3번(별점별 감정 분포)."""
    rows = conn.execute(
        """SELECT rating AS r, sentiment AS s, COUNT(*) AS n FROM clean_reviews
           WHERE rating IS NOT NULL AND sentiment IS NOT NULL
           GROUP BY r, s ORDER BY r"""
    )
    return [(r["r"], r["s"], r["n"]) for r in rows]


def group_by_product(conn: sqlite3.Connection) -> list[sqlite3.Row]:
    """제품별 집계 — 보너스(제품 비교 분석)."""
    return list(conn.execute(
        """SELECT product,
                  COUNT(*) AS total,
                  AVG(rating) AS avg_rating,
                  SUM(CASE WHEN sentiment = '긍정' THEN 1 ELSE 0 END) AS positive,
                  SUM(CASE WHEN sentiment = '중립' THEN 1 ELSE 0 END) AS neutral,
                  SUM(CASE WHEN sentiment = '부정' THEN 1 ELSE 0 END) AS negative
           FROM clean_reviews
           WHERE product IS NOT NULL
           GROUP BY product ORDER BY total DESC"""
    ))


def group_by_language(conn: sqlite3.Connection) -> list[tuple[str, int]]:
    """언어별 건수 — 보너스(다국어) 확인용."""
    rows = conn.execute(
        """SELECT COALESCE(language, '미상') AS lang, COUNT(*) AS n
           FROM clean_reviews GROUP BY lang ORDER BY n DESC"""
    )
    return [(r["lang"], r["n"]) for r in rows]


# ─────────────────────────────────────────────────────────────────────────────
# 개선 피드백 루프 (actions) — feedback.py 가 쓴다
# ─────────────────────────────────────────────────────────────────────────────

def open_action(conn: sqlite3.Connection, item: dict) -> str:
    """액션 1건 등록 → 'opened' | 'exists'.

    이미 추적 중인 키워드는 **덮어쓰지 않는다**. 베이스라인을 새로 잡으면 그때까지의
    개선 이력이 지워져 "고쳤는데 나아졌나"를 영영 답할 수 없다.
    """
    row = conn.execute(
        "SELECT id FROM actions WHERE keyword = ?", (item["keyword"],)
    ).fetchone()
    if row:
        return "exists"
    conn.execute(
        """INSERT INTO actions
           (opened_at, keyword, priority, metric, baseline_ratio, baseline_mentions,
            baseline_from, baseline_to, cadence_days, status)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, 'open')""",
        (
            item["opened_at"], item["keyword"], item["priority"], item["metric"],
            item.get("baseline_ratio"), item.get("baseline_mentions"),
            item.get("baseline_from"), item.get("baseline_to"), item["cadence_days"],
        ),
    )
    return "opened"


def list_actions(conn: sqlite3.Connection, status: str | None = "open") -> list[sqlite3.Row]:
    """추적 중인 액션 목록. status=None 이면 닫힌 것까지 전부."""
    sql = "SELECT * FROM actions"
    params: list = []
    if status:
        sql += " WHERE status = ?"
        params.append(status)
    sql += " ORDER BY priority DESC, id"
    return list(conn.execute(sql, params))


def close_action(conn: sqlite3.Connection, keyword: str, at: str) -> bool:
    """액션을 닫는다 → 닫혔으면 True. 없거나 이미 닫혔으면 False."""
    cursor = conn.execute(
        "UPDATE actions SET status = 'closed', closed_at = ? WHERE keyword = ? AND status = 'open'",
        (at, keyword),
    )
    return cursor.rowcount > 0


def keyword_window(conn: sqlite3.Connection, keyword: str, start: str, end: str) -> dict:
    """구간 안에서 특정 키워드가 들어간 리뷰의 (언급 수, 부정 수, 부정률, 평균 별점).

    본문 부분 일치(LIKE)로 세는 이유: 형태소 분석기를 붙이면 의존성이 늘고, 리뷰의
    불만 키워드는 대개 "배송"·"불량"처럼 **원형 그대로** 나타난다. 대신 LIKE 패턴의
    와일드카드(%, _)를 이스케이프해 키워드에 특수문자가 들어가도 깨지지 않게 한다.
    """
    pattern = "%" + keyword.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_") + "%"
    row = conn.execute(
        """SELECT COUNT(*) AS mentions,
                  SUM(CASE WHEN sentiment = '부정' THEN 1 ELSE 0 END) AS negative,
                  AVG(rating) AS avg_rating
           FROM clean_reviews
           WHERE text LIKE ? ESCAPE '\\'
             AND sentiment IS NOT NULL
             AND created_at IS NOT NULL AND created_at >= ? AND created_at <= ?""",
        (pattern, start, end),
    ).fetchone()
    mentions = int(row["mentions"] or 0)
    negative = int(row["negative"] or 0)
    return {
        "mentions": mentions,
        "negative": negative,
        "ratio": (negative / mentions) if mentions else None,
        "avg_rating": round(row["avg_rating"], 2) if row["avg_rating"] is not None else None,
    }


# ─────────────────────────────────────────────────────────────────────────────
# 원인 가설 검증 (diagnose.py 가 쓴다)
# ─────────────────────────────────────────────────────────────────────────────

def negative_by_product(conn: sqlite3.Connection, start: str, end: str) -> list[sqlite3.Row]:
    """구간의 제품별 (전체, 부정) — 가설 H1(제품 편중) 검증용."""
    return list(conn.execute(
        """SELECT COALESCE(product, '(미상)') AS product,
                  COUNT(*) AS total,
                  SUM(CASE WHEN sentiment = '부정' THEN 1 ELSE 0 END) AS negative
           FROM clean_reviews
           WHERE sentiment IS NOT NULL
             AND created_at IS NOT NULL AND created_at >= ? AND created_at <= ?
           GROUP BY product ORDER BY negative DESC, total DESC""",
        (start, end),
    ))


def mismatch_window(conn: sqlite3.Connection, start: str, end: str) -> dict:
    """구간의 별점-감정 불일치 — 가설 H3(오탐) 검증용.

    별점 4~5인데 본문이 부정, 별점 1~2인데 본문이 긍정인 건을 센다. 이 비율이 높으면
    "정말 나빠졌다"보다 **"분석이 틀렸다"** 를 먼저 의심해야 한다.
    """
    row = conn.execute(
        """SELECT COUNT(*) AS scored,
                  SUM(CASE WHEN rating >= 4 AND sentiment = '부정' THEN 1 ELSE 0 END) AS high_neg,
                  SUM(CASE WHEN rating <= 2 AND sentiment = '긍정' THEN 1 ELSE 0 END) AS low_pos,
                  AVG(confidence) AS avg_confidence
           FROM clean_reviews
           WHERE sentiment IS NOT NULL AND rating IS NOT NULL
             AND created_at IS NOT NULL AND created_at >= ? AND created_at <= ?""",
        (start, end),
    ).fetchone()
    scored = int(row["scored"] or 0)
    mismatch = int(row["high_neg"] or 0) + int(row["low_pos"] or 0)
    return {
        "scored": scored,
        "mismatch": mismatch,
        "ratio": (mismatch / scored) if scored else None,
        "avg_confidence": round(row["avg_confidence"], 3) if row["avg_confidence"] is not None else None,
    }


def negative_texts(conn: sqlite3.Connection, start: str, end: str) -> list[str]:
    """구간의 부정 리뷰 본문 — 가설 H2(신규 키워드 급증) 검증용."""
    rows = conn.execute(
        """SELECT text FROM clean_reviews
           WHERE sentiment = '부정'
             AND created_at IS NOT NULL AND created_at >= ? AND created_at <= ?""",
        (start, end),
    )
    return [r["text"] for r in rows]


def negative_dates(conn: sqlite3.Connection, start: str, end: str) -> list[tuple[str, int]]:
    """구간의 (날짜, 부정 건수) — 가설 H5(특정일·요일 편중) 검증용."""
    rows = conn.execute(
        """SELECT created_at AS d, COUNT(*) AS n FROM clean_reviews
           WHERE sentiment = '부정'
             AND created_at IS NOT NULL AND created_at >= ? AND created_at <= ?
           GROUP BY d ORDER BY d""",
        (start, end),
    )
    return [(r["d"], r["n"]) for r in rows]
