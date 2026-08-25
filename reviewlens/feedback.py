"""개선 피드백 루프 — 부정 키워드 → 우선순위 → 조치 → 효과 측정 → 종료.

`extract` 는 "무엇이 문제인가"까지만 답한다. 개선 제안이 리포트에 실려도, **그 제안을
실행한 뒤 정말 나아졌는지**를 되짚는 자리가 없으면 다음 달에 같은 제안이 다시 나온다.
이 모듈이 그 자리다.

루프 다섯 단계:
  ① 후보     — 최신 추출 결과의 negative_keywords (없으면 부정 리뷰 빈출어)
  ② 우선순위 — 영향 · 심각도 · 추세를 합쳐 0~100 점 (`priority_score`)
  ③ 등록     — 상위 N개를 actions 에 저장하고 **그 시점 지표를 베이스라인으로 고정**
  ④ 재측정   — 주기(cadence_days)마다 베이스라인 창 vs 최근 창을 비교
  ⑤ 종료     — 개선이 확인되면 닫는다(`--close`). 악화면 열어 둔 채 우선순위가 오른다

**왜 베이스라인을 저장하나**: 조치 시행 시점을 기록해 두지 않으면 "고치기 전"과 "고친 후"를
가를 선이 없다. 지금 지표만 다시 계산해서는 좋아졌는지 나빠졌는지 말할 수 없다.

**왜 지표가 '부정 비율'인가**: 부정 **건수**는 리뷰가 늘면 같이 는다. 배송 관련 불만을
고쳤는데 그달 리뷰가 두 배가 되면 건수는 그대로여도 사실 절반으로 준 것이다.
"""

from __future__ import annotations

import json
import logging
from datetime import datetime, timedelta

from . import storage
from .ingest import now_iso
from .stats import STOPWORDS, WORD

logger = logging.getLogger(__name__)

# 추적 지표 이름 — actions.metric 에 문자열로 남긴다. 나중에 지표를 바꾸면 예전 액션이
# 무엇으로 측정됐는지 알 수 있어야 하기 때문에, 코드 상수가 아니라 행마다 기록한다.
METRIC = "해당 키워드 포함 리뷰의 부정 비율"


def _window(reference: str, days: int) -> tuple[str, str]:
    """기준일에서 뒤로 days 일 창 → (시작, 끝)."""
    end = datetime.strptime(reference, "%Y-%m-%d").date()
    return (end - timedelta(days=days - 1)).isoformat(), end.isoformat()


def _reference_date(conn) -> str:
    """기준일 = 가장 최근 리뷰 작성일. 오늘로 고정하면 과거 데이터에서 창이 빈다."""
    row = conn.execute(
        "SELECT MAX(created_at) AS d FROM clean_reviews WHERE created_at IS NOT NULL"
    ).fetchone()
    return row["d"] if row and row["d"] else datetime.today().date().isoformat()


# 조사 — 명사 뒤에 붙어 같은 말을 다른 말로 세게 만든다("배송이"·"배송은"·"배송도").
PARTICLES = ("이", "가", "은", "는", "을", "를", "에", "의", "도", "만", "과", "와", "로")
# 종결어미로 끝나면 서술어다 — 추적할 "문제 이름"이 아니다("실망입니다"·"느려요").
ENDINGS = ("다", "요", "죠", "네", "음", "함")


def _stem(word: str) -> str:
    """조사를 떼고 서술어를 버린다 — 형태소 분석기 없이 하는 최소 정규화.

    **왜 라이브러리를 쓰지 않나**: 이 경로는 `extract` 를 못 돌릴 때(키 없음)의 대체
    수단이다. 정확한 키워드는 AI 추출이 준다. 대체 경로 하나 때문에 형태소 분석기를
    의존성에 넣는 것은 비용이 크다 — 다만 조사조차 안 떼면 "배송이"와 "배송은"이 다른
    문제로 잡혀 우선순위가 갈라지므로, 그만큼만 손본다.
    """
    if len(word) >= 2 and word.endswith(ENDINGS):
        return ""
    if len(word) >= 3 and word.endswith(PARTICLES):
        return word[:-1]
    return word


def candidates(conn, cfg: dict) -> list[str]:
    """추적 후보 키워드. 최신 추출의 부정 키워드를 쓰고, 없으면 부정 리뷰 빈출어로 대신한다.

    대체 경로를 둔 이유: `extract` 는 API 키가 있어야 돈다. 키 없이도 루프를 시연할 수
    있어야 문서와 실행 결과가 어긋나지 않는다.
    """
    row = storage.latest_extraction(conn)
    if row:
        result = json.loads(row["result"])
        keywords = [str(k).strip() for k in (result.get("negative_keywords") or []) if str(k).strip()]
        if keywords:
            return keywords

    logger.info("추출 결과가 없어 부정 리뷰 빈출어로 후보를 대신합니다 (`extract` 권장)")

    # 단순 빈도로 뽑으면 "같은"·"만에" 처럼 **어느 리뷰에나 있는 말**이 상위를 채운다.
    # 부정 리뷰에 **치우친 정도**(lift = 부정 등장 수 / 전체 등장 수)로 걸러야 불만의
    # 실체에 가까워진다. 불용어 목록만으로는 이 문제를 못 잡는다 — 목록에 없는 흔한 말이
    # 계속 생기기 때문이다.
    def counted(texts: list[str]) -> dict[str, int]:
        counter: dict[str, int] = {}
        for text in texts:
            # 한 리뷰 안 중복은 1회 — 긴 리뷰 하나가 순위를 만들지 못하게 한다
            for word in {_stem(w) for w in WORD.findall(text or "")}:
                if word and len(word) >= 2 and word.lower() not in STOPWORDS:
                    counter[word] = counter.get(word, 0) + 1
        return counter

    negative = counted(storage.negative_texts(conn, "0000-01-01", "9999-12-31"))
    overall = counted([r["text"] for r in storage.select_clean(conn)])

    minimum = cfg["feedback"]["min_mentions"]
    ranked = [
        (word, count, count / overall.get(word, count))
        for word, count in negative.items()
        if overall.get(word, 0) >= minimum
    ]
    # 치우침(lift)은 **걸러내는** 기준이고, 순위는 **빈도**로 매긴다. lift 로 정렬하면
    # 부정 리뷰 2건에만 나온 말이 lift 1.0 으로 1위가 된다 — 드문 불만을 먼저 고칠 수 없다.
    ranked = [item for item in ranked if item[2] >= 0.5]
    ranked.sort(key=lambda item: (-item[1], -item[2]))
    return [word for word, _, _ in ranked][: cfg["feedback"]["max_actions"] * 2]


def priority_score(current: dict, previous: dict, total_negative: int) -> float:
    """우선순위 0~100. 세 축을 가중 합산한다.

      영향(50%)   — 이 키워드가 최근 부정 리뷰에서 차지하는 몫. 드문 불만을 먼저 고칠 수 없다
      심각도(30%) — 평균 별점이 낮을수록 높다. 같은 빈도라도 별점 1점짜리가 더 급하다
      추세(20%)   — 부정률이 오르고 있으면 가산. 내려가는 문제는 이미 잡히는 중이다

    가중치를 코드에 박은 이유: 세 축의 상대적 무게는 팀의 판단이지 데이터가 정하는 값이
    아니다. config 로 빼면 "왜 이 값인가"를 아무도 설명하지 못한 채 흔들린다.
    """
    impact = (current["negative"] / total_negative) if total_negative else 0.0
    severity = ((5 - current["avg_rating"]) / 4) if current["avg_rating"] is not None else 0.5
    if current["ratio"] is not None and previous.get("ratio") is not None:
        trend = max(current["ratio"] - previous["ratio"], 0.0)
    else:
        trend = 0.0
    return round(100 * (0.5 * impact + 0.3 * severity + 0.2 * trend), 1)


def verdict(baseline_ratio: float | None, current: dict, cfg: dict) -> str:
    """베이스라인 대비 판정 → 개선 | 정체 | 악화 | 표본 부족 | 기준선 없음."""
    if current["mentions"] < cfg["feedback"]["min_mentions"]:
        return "표본 부족"
    if baseline_ratio is None or current["ratio"] is None:
        return "기준선 없음"
    delta = current["ratio"] - baseline_ratio
    if delta <= cfg["feedback"]["improve_delta"]:
        return "개선"
    if delta >= cfg["feedback"]["worsen_delta"]:
        return "악화"
    return "정체"


def open_actions(db_path: str, cfg: dict, reference: str | None = None) -> list[dict]:
    """후보를 우선순위로 정렬해 상위 N개를 액션으로 등록 → 등록 결과 목록."""
    days = cfg["feedback"]["cadence_days"]
    opened: list[dict] = []

    with storage.connect(db_path) as conn:
        reference = reference or _reference_date(conn)
        cur_start, cur_end = _window(reference, days)
        prev_end = (datetime.strptime(cur_start, "%Y-%m-%d").date() - timedelta(days=1)).isoformat()
        prev_start = (datetime.strptime(prev_end, "%Y-%m-%d").date()
                      - timedelta(days=days - 1)).isoformat()

        total_negative = sum(n for _, n in storage.negative_dates(conn, cur_start, cur_end))
        scored = []
        for keyword in candidates(conn, cfg):
            current = storage.keyword_window(conn, keyword, cur_start, cur_end)
            previous = storage.keyword_window(conn, keyword, prev_start, prev_end)
            if current["mentions"] == 0:
                continue
            scored.append((priority_score(current, previous, total_negative), keyword, current))

        scored.sort(key=lambda item: -item[0])
        for score, keyword, current in scored[: cfg["feedback"]["max_actions"]]:
            state = storage.open_action(conn, {
                "opened_at": now_iso(),
                "keyword": keyword,
                "priority": score,
                "metric": METRIC,
                "baseline_ratio": current["ratio"],
                "baseline_mentions": current["mentions"],
                "baseline_from": cur_start,
                "baseline_to": cur_end,
                "cadence_days": days,
            })
            opened.append({"keyword": keyword, "priority": score, "state": state,
                           "baseline": current, "from": cur_start, "to": cur_end})
            logger.info("액션 %s: %s (우선순위 %.1f)", state, keyword, score)
    return opened


def review(db_path: str, cfg: dict, reference: str | None = None) -> list[dict]:
    """열려 있는 액션을 재측정 → 액션별 판정 목록."""
    days = cfg["feedback"]["cadence_days"]
    results: list[dict] = []

    with storage.connect(db_path) as conn:
        reference = reference or _reference_date(conn)
        cur_start, cur_end = _window(reference, days)
        for action in storage.list_actions(conn, status="open"):
            current = storage.keyword_window(conn, action["keyword"], cur_start, cur_end)
            due = _due_date(action["baseline_to"], action["cadence_days"])
            results.append({
                "keyword": action["keyword"],
                "priority": action["priority"],
                "metric": action["metric"],
                "baseline_ratio": action["baseline_ratio"],
                "baseline_mentions": action["baseline_mentions"],
                "baseline_window": f"{action['baseline_from']}~{action['baseline_to']}",
                "current": current,
                "current_window": f"{cur_start}~{cur_end}",
                "due": due,
                "verdict": verdict(action["baseline_ratio"], current, cfg),
            })
    return results


def _due_date(baseline_to: str | None, cadence_days: int) -> str:
    """다음 재측정 예정일 = 베이스라인 끝 + 주기."""
    if not baseline_to:
        return "-"
    end = datetime.strptime(baseline_to, "%Y-%m-%d").date()
    return (end + timedelta(days=cadence_days)).isoformat()


def format_review(results: list[dict]) -> str:
    """재측정 결과를 사람이 읽는 표로. 리포트·CLI 가 같은 문자열을 쓴다."""
    if not results:
        return "  추적 중인 액션이 없습니다 (`feedback --open` 으로 등록하세요)"

    lines = [
        f"  {'키워드':<12}{'우선순위':>8}{'기준선':>9}{'현재':>9}{'변화':>9}  {'판정':<9}재측정",
        "  " + "-" * 74,
    ]
    for item in results:
        base = item["baseline_ratio"]
        now = item["current"]["ratio"]
        base_text = f"{base * 100:.1f}%" if base is not None else "-"
        now_text = f"{now * 100:.1f}%" if now is not None else "-"
        delta_text = f"{(now - base) * 100:+.1f}%p" if (base is not None and now is not None) else "-"
        lines.append(
            f"  {item['keyword'][:11]:<12}{item['priority']:>8.1f}{base_text:>9}"
            f"{now_text:>9}{delta_text:>9}  {item['verdict']:<9}{item['due']}"
        )
    lines.append(f"  추적 지표: {results[0]['metric']} · 창 {results[0]['current_window']}")
    return "\n".join(lines)
