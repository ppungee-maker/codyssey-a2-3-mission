"""부정률 급증의 **원인 가설 검증** — 경고 다음 단계.

`alert.py` 는 "부정 비율이 올랐다"까지 답한다. 그 다음 질문은 언제나 **"왜"** 다.
경고만 보고 대응하면 엉뚱한 곳을 고친다 — 실제로는 제품 하나에서만 터진 문제일 수도,
리뷰 유입 자체가 바뀐 것일 수도, 감정 분석이 틀린 것일 수도 있다.

이 모듈은 답을 내지 않는다. **가설을 순서대로 검증하고 근거 수치를 붙여 지지/기각/판정
불가로 갈라 준다.** 결론은 사람이 낸다.

가설 순서에 의도가 있다 — **싼 것부터, 그리고 "정말 나빠진 게 맞나"를 먼저** 본다.

  H3 별점-감정 불일치  → 급증이 **측정 오류**일 가능성. 이게 지지되면 아래는 무의미하다
  H4 표본 급변         → 리뷰 유입량 자체가 바뀌었나(이벤트·프로모션·리뷰 요청 메일)
  H1 제품 편중         → 전 제품 문제인가, 한 제품 문제인가
  H2 신규 키워드 급증  → 이전에 없던 불만이 갑자기 나타났나
  H5 특정일 편중       → 하루에 몰렸나(배송 사고·서버 장애처럼 단발 사건)
  H6 외부 운영 지표    → 주문취소·반품·유입채널과 함께 움직였나 (**현재 미수집**)

H6 를 "판정 불가"로 남겨 두는 것도 결과다. 무엇을 몰라서 결론을 못 내는지 적어 두지 않으면
다음 사람이 같은 자리에서 다시 막힌다. 수집해야 할 지표 목록은 README 에 표로 있다.
"""

from __future__ import annotations

import logging
from collections import Counter
from datetime import datetime

from . import alert, storage
from .stats import STOPWORDS, WORD

logger = logging.getLogger(__name__)

WEEKDAYS = ["월", "화", "수", "목", "금", "토", "일"]

SUPPORTED = "지지"
REJECTED = "기각"
UNKNOWN = "판정 불가"


def _finding(code: str, name: str, verdict: str, detail: str, note: str = "") -> dict:
    return {"code": code, "name": name, "verdict": verdict, "detail": detail, "note": note}


def _h3_mismatch(conn, window, cfg) -> dict:
    """H3 — 별점과 본문 감정이 어긋나는 비율. 높으면 '급증'이 분석 오류일 수 있다."""
    start, end = window
    data = storage.mismatch_window(conn, start, end)
    threshold = cfg["diagnose"]["mismatch_threshold"]
    if data["scored"] == 0:
        return _finding("H3", "별점-감정 불일치(측정 오류)", UNKNOWN,
                        "별점과 감정이 모두 있는 리뷰가 없습니다")
    detail = (f"불일치 {data['mismatch']}/{data['scored']}건 "
              f"({data['ratio'] * 100:.1f}%) · 평균 신뢰도 {data['avg_confidence']}")
    if data["ratio"] >= threshold:
        return _finding("H3", "별점-감정 불일치(측정 오류)", SUPPORTED, detail,
                        "먼저 감정 분석을 검증하세요 — 아래 가설은 그 뒤에 봅니다")
    return _finding("H3", "별점-감정 불일치(측정 오류)", REJECTED,
                    f"{detail} · 기준 {threshold * 100:.0f}% 미만")


def _h4_volume(conn, recent, previous, cfg) -> dict:
    """H4 — 리뷰 유입량 자체가 급변했나. 모수가 흔들리면 비율 해석이 달라진다."""
    # 부정 건수가 아니라 **분석된 전체 건수**를 본다 — 여기서 보려는 것은 모수의 변화다
    now = len(storage.select_clean(conn, date_from=recent[0], date_to=recent[1], status="analyzed"))
    before = len(storage.select_clean(conn, date_from=previous[0], date_to=previous[1],
                                      status="analyzed"))
    threshold = cfg["diagnose"]["volume_change_threshold"]
    if before == 0:
        return _finding("H4", "리뷰 유입량 급변", UNKNOWN,
                        f"직전 기간에 분석된 리뷰가 없습니다 (최근 {now}건)")
    change = (now - before) / before
    detail = f"최근 {now}건 · 직전 {before}건 ({change * 100:+.1f}%)"
    if abs(change) >= threshold:
        return _finding("H4", "리뷰 유입량 급변", SUPPORTED, detail,
                        "리뷰 요청 메일·프로모션·이벤트 일정과 대조하세요")
    return _finding("H4", "리뷰 유입량 급변", REJECTED,
                    f"{detail} · 기준 ±{threshold * 100:.0f}% 미만")


def _h1_product(conn, window, cfg) -> dict:
    """H1 — 부정이 특정 제품에 몰렸나. 몰렸다면 전사 대응이 아니라 그 제품 문제다."""
    start, end = window
    rows = storage.negative_by_product(conn, start, end)
    total_negative = sum(int(r["negative"] or 0) for r in rows)
    if total_negative == 0:
        return _finding("H1", "특정 제품 편중", REJECTED, "구간에 부정 리뷰가 없습니다")

    top = max(rows, key=lambda r: int(r["negative"] or 0))
    share = int(top["negative"]) / total_negative
    threshold = cfg["diagnose"]["product_share_threshold"]
    ratio = int(top["negative"]) / int(top["total"]) if top["total"] else 0
    detail = (f"{top['product']} 이(가) 부정 {top['negative']}/{total_negative}건 "
              f"({share * 100:.1f}%) · 해당 제품 부정률 {ratio * 100:.1f}%")
    if share >= threshold and len(rows) > 1:
        return _finding("H1", "특정 제품 편중", SUPPORTED, detail,
                        f"`list --product '{top['product']}' --sentiment 부정` 으로 원문 확인")
    return _finding("H1", "특정 제품 편중", REJECTED,
                    f"{detail} · 기준 {threshold * 100:.0f}% 미만(여러 제품에 퍼져 있음)")


def _keywords(texts: list[str]) -> Counter:
    counter: Counter[str] = Counter()
    for text in texts:
        # 한 리뷰 안에서 같은 말을 반복해도 1회로 센다 — 긴 리뷰 하나가 순위를 만들면 안 된다
        seen = {w for w in WORD.findall(text or "")
                if len(w) >= 2 and w.lower() not in STOPWORDS}
        counter.update(seen)
    return counter


def _h2_new_keywords(conn, recent, previous, cfg) -> dict:
    """H2 — 직전에 없던 불만 키워드가 새로 나타났나."""
    now = _keywords(storage.negative_texts(conn, *recent))
    before = _keywords(storage.negative_texts(conn, *previous))
    minimum = cfg["diagnose"]["new_keyword_min_count"]
    fresh = [(w, c) for w, c in now.most_common() if before[w] == 0 and c >= minimum]

    if not now:
        return _finding("H2", "신규 부정 키워드 급증", UNKNOWN, "최근 구간에 부정 리뷰가 없습니다")
    if fresh:
        listed = ", ".join(f"{w}({c}회)" for w, c in fresh[:5])
        return _finding("H2", "신규 부정 키워드 급증", SUPPORTED,
                        f"직전 구간에 없던 키워드 {len(fresh)}종 — {listed}",
                        "새 문제일 가능성이 큽니다 — 해당 키워드로 액션을 여세요(`feedback --open`)")
    return _finding("H2", "신규 부정 키워드 급증", REJECTED,
                    f"{minimum}회 이상 새로 등장한 키워드가 없습니다(기존 불만의 연장)")


def _h5_burst(conn, window, cfg) -> dict:
    """H5 — 부정이 특정 날짜/요일에 몰렸나. 몰렸다면 단발 사건을 의심한다."""
    start, end = window
    rows = storage.negative_dates(conn, start, end)
    total = sum(n for _, n in rows)
    if total == 0:
        return _finding("H5", "특정일·요일 편중", REJECTED, "구간에 부정 리뷰가 없습니다")

    top_date, top_count = max(rows, key=lambda item: item[1])
    share = top_count / total
    weekday = WEEKDAYS[datetime.strptime(top_date, "%Y-%m-%d").weekday()]
    threshold = cfg["diagnose"]["burst_share_threshold"]
    detail = f"{top_date}({weekday}) 하루에 부정 {top_count}/{total}건 ({share * 100:.1f}%)"
    if share >= threshold and len(rows) > 1:
        return _finding("H5", "특정일·요일 편중", SUPPORTED, detail,
                        "그날의 배송·재고·시스템 이력을 확인하세요")
    return _finding("H5", "특정일·요일 편중", REJECTED,
                    f"{detail} · 기준 {threshold * 100:.0f}% 미만(기간에 고르게 퍼짐)")


def _h6_ops() -> dict:
    """H6 — 외부 운영 지표와의 동행 여부. **현재 스키마에 지표가 없어 판정할 수 없다.**

    데이터가 없다는 사실을 결과로 남기는 이유: "확인했는데 정상"과 "확인할 수단이 없음"은
    전혀 다른 상태다. 후자를 빈칸으로 두면 다음 사람이 같은 곳에서 다시 막힌다.
    """
    return _finding(
        "H6", "외부 운영 지표 동행", UNKNOWN,
        "주문취소·반품·배송지연·유입채널 지표가 수집되지 않아 대조할 수 없습니다",
        "수집 대상과 위치는 README 「부정률 급증 — 원인 가설 검증」의 지표 표 참조",
    )


def run(db_path: str, cfg: dict, reference: str | None = None) -> dict:
    """가설 6종을 검증 → {'alert': 경고 문구|None, 'evidence': ..., 'findings': [...]}.

    경고가 없어도 실행할 수 있게 둔다 — 평상시 수치를 봐 둬야 경고가 떴을 때 비교할 감이
    생긴다.
    """
    warning, evidence = alert.check(db_path, cfg, reference=reference)
    recent = (evidence["recent"]["from"], evidence["recent"]["to"])
    previous = (evidence["previous"]["from"], evidence["previous"]["to"])

    with storage.connect(db_path) as conn:
        findings = [
            _h3_mismatch(conn, recent, cfg),
            _h4_volume(conn, recent, previous, cfg),
            _h1_product(conn, recent, cfg),
            _h2_new_keywords(conn, recent, previous, cfg),
            _h5_burst(conn, recent, cfg),
            _h6_ops(),
        ]

    supported = [f["code"] for f in findings if f["verdict"] == SUPPORTED]
    logger.info("가설 검증 완료 — 지지 %d건 %s", len(supported), supported or "")
    return {"alert": warning, "evidence": evidence, "findings": findings}


def format_result(result: dict) -> str:
    """검증 결과를 사람이 읽는 형태로."""
    lines = ["  [원인 가설 검증]", alert.format_evidence(result["evidence"]), ""]
    for finding in result["findings"]:
        mark = {SUPPORTED: "●", REJECTED: "○", UNKNOWN: "?"}[finding["verdict"]]
        lines.append(f"  {mark} {finding['code']} {finding['name']} — {finding['verdict']}")
        lines.append(f"      {finding['detail']}")
        if finding["note"]:
            lines.append(f"      → {finding['note']}")

    supported = [f for f in result["findings"] if f["verdict"] == SUPPORTED]
    lines.append("")
    if supported:
        lines.append("  지지된 가설: " + ", ".join(f"{f['code']} {f['name']}" for f in supported))
    else:
        lines.append("  지지된 가설이 없습니다 — 단일 원인이 아니거나 지표가 부족합니다")
    return "\n".join(lines)
