"""소스 상태 판정과 실행 간 이력.

한 번의 수집 결과를 단계별로 나눈다:
  HTTP 요청 → 피드 파싱 → 유효 기사(제목·링크) → 시각 확인 기사 → 발행 주기 안의 최근 기사

상태(state):
  http_error    요청 실패(연결 오류, 4xx/5xx)
  parse_error   응답은 받았지만 RSS/Atom 으로 읽을 수 없음
  empty         피드에 항목이 0개
  filtered_all  항목은 있지만 유효 기사가 0개 (제목·링크 없음, 언론사 필터에 전부 걸림 등)
  undated       유효 기사는 있지만 발행 시각이 하나도 없음 (최신 여부 판단 불가)
  stale         가장 최근 기사가 발행 주기 기준보다 오래됨
  ok            최근 기사 확보
"""

from __future__ import annotations

from datetime import datetime, timedelta
from typing import Any

STATE_LABELS = {
    "ok": "정상",
    "undated": "시각 없음",
    "stale": "오래됨",
    "filtered_all": "유효 기사 0",
    "empty": "0건",
    "parse_error": "파싱 실패",
    "http_error": "요청 실패",
    "disabled": "수집 중지",
}
FAILURE_STATES = {"http_error", "parse_error"}
EMPTY_STATES = {"empty", "filtered_all"}
# '정상 수집'으로 세는 상태. undated 는 기사가 실제로 들어오므로 정상으로 본다(시각만 없음).
GOOD_STATES = {"ok", "undated"}

# 발행 주기별 '오래됨' 기준 (가장 최근 기사의 나이). config/quality.json 으로 덮어쓸 수 있다.
DEFAULT_CADENCE_HOURS = {"frequent": 12, "daily": 48, "weekly": 21 * 24, "monthly": 45 * 24}


def classify(status: dict[str, Any], now: datetime, cadence_hours: dict[str, float]) -> str:
    if not status.get("http_ok"):
        return "http_error"
    if not status.get("parse_ok"):
        return "parse_error"
    if status.get("entry_count", 0) == 0:
        return "empty"
    if status.get("valid_count", 0) == 0:
        return "filtered_all"
    if status.get("dated_count", 0) == 0:
        return "undated"
    newest = status.get("newest_at_dt")
    limit = timedelta(hours=cadence_hours.get(status.get("cadence", "daily"), cadence_hours["daily"]))
    if newest is None or now - newest > limit:
        return "stale"
    return "ok"


def update_history(
    prev: dict[str, Any] | None, status: dict[str, Any], now_iso: str
) -> dict[str, Any]:
    """직전 이력 + 이번 결과 → 새 이력. prev 가 없으면(첫 실행·캐시 유실) 새로 시작."""
    h = dict(prev or {})
    state = status["state"]
    h.setdefault("first_tracked_at", now_iso)
    if status.get("http_ok"):
        h["last_http_ok_at"] = now_iso
    if state in GOOD_STATES:
        h["last_success_at"] = now_iso
    h["consecutive_failures"] = int(h.get("consecutive_failures", 0)) + 1 if state in FAILURE_STATES else 0
    h["consecutive_empty"] = int(h.get("consecutive_empty", 0)) + 1 if state in EMPTY_STATES else 0
    h["consecutive_stale"] = int(h.get("consecutive_stale", 0)) + 1 if state == "stale" else 0
    if state in GOOD_STATES:
        h["problem_since"] = None
    elif not h.get("problem_since"):
        h["problem_since"] = now_iso
    h["last_state"] = state
    return h
