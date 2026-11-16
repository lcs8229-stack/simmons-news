#!/usr/bin/env python3
"""배포 전 품질 검사.

build_news.py 가 만든 data/news.json, data/status.json 을 검사해서
  - fail 이 하나라도 있으면 종료 코드 1 → 워크플로가 배포를 건너뛰고 기존 페이지가 유지된다.
  - warn 만 있으면 배포한다. 경고는 data/health.json 에 남아 화면 하단에 표시된다.

기준값(baseline)은 최근 정상 실행들의 수치다.
  1) data/state/metrics_history.json 에 최근 7일 정상 실행이 3회 이상이면 항목별 중앙값
  2) 아니면 data/state/last_good.json (직전 정상 실행 1회)
  3) 없으면 배포된 페이지의 data/health.json (캐시 유실 대비)
  4) 모두 없으면 첫 실행으로 보고 절대 기준만 검사한다.
"""

from __future__ import annotations

import argparse
import json
import os
import statistics
import sys
from datetime import timedelta
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parent))
from build_news import fetch_site_json, load_quality_config, parse_iso, write_json_atomic  # noqa: E402

METRIC_KEYS = (
    "news_articles",
    "kr_candidates",
    "intl_candidates",
    "sources_ok",
    "sources_active",
    "top_kr",
    "top_intl",
    "sources_ok_kr",
    "sources_active_kr",
    "sources_ok_intl",
    "sources_active_intl",
)
HISTORY_DAYS = 7
HISTORY_MIN_RUNS = 3


def metrics_of(news: dict[str, Any]) -> dict[str, Any]:
    c = news.get("counts", {})
    return {k: c.get(k) for k in METRIC_KEYS} | {"generated_at": news.get("generated_at")}


def load_history(state_dir: Path) -> list[dict[str, Any]]:
    p = state_dir / "metrics_history.json"
    if not p.exists():
        return []
    try:
        return json.loads(p.read_text(encoding="utf-8")).get("runs", [])
    except Exception:  # noqa: BLE001
        return []


def recent_runs(history: list[dict[str, Any]], now_iso: str | None) -> list[dict[str, Any]]:
    now = parse_iso(now_iso)
    if now is None:
        return history[-HISTORY_DAYS * 24:]
    keep_after = now - timedelta(days=HISTORY_DAYS)
    return [r for r in history if (parse_iso(r.get("generated_at")) or now) >= keep_after]


def median_baseline(runs: list[dict[str, Any]]) -> dict[str, Any]:
    out: dict[str, Any] = {}
    for k in METRIC_KEYS:
        vals = [r[k] for r in runs if isinstance(r.get(k), (int, float))]
        if vals:
            out[k] = statistics.median(vals)
    return out


def load_baseline(state_dir: Path, site_url: str, now_iso: str | None = None) -> tuple[dict[str, Any] | None, str]:
    runs = recent_runs(load_history(state_dir), now_iso)
    if len(runs) >= HISTORY_MIN_RUNS:
        return median_baseline(runs), f"median_{HISTORY_DAYS}d({len(runs)}회)"
    p = state_dir / "last_good.json"
    if p.exists():
        try:
            return json.loads(p.read_text(encoding="utf-8"))["metrics"], "cache"
        except Exception:  # noqa: BLE001
            pass
    deployed = fetch_site_json(site_url, "data/health.json")
    if deployed and deployed.get("metrics"):
        return deployed["metrics"], "site"
    return None, "none"


def evaluate(
    news: dict[str, Any], status: dict[str, Any], baseline: dict[str, Any] | None, conf: dict[str, Any]
) -> dict[str, Any]:
    f, w = conf["fail"], conf["warn"]
    m = metrics_of(news)
    fails: list[str] = []
    warns: list[str] = []

    active = max(1, int(m["sources_active"] or 0))
    ok = int(m["sources_ok"] or 0)
    ratio = ok / active
    if ok < f["min_ok_sources"] or ratio < f["min_ok_ratio"]:
        fails.append(f"정상 소스 부족: {ok}/{active} (기준 {f['min_ok_sources']}곳 이상, {f['min_ok_ratio']:.0%} 이상)")
    elif ratio < w["min_ok_ratio"]:
        warns.append(f"정상 소스 비율 낮음: {ok}/{active} ({ratio:.0%})")

    # 언론사 소재별: 한쪽 소재 언론이 대부분 실패하면 전체 비율이 괜찮아도 문제로 본다
    for region, label in (("kr", "국내"), ("intl", "해외")):
        act = int(m.get(f"sources_active_{region}") or 0)
        okr = int(m.get(f"sources_ok_{region}") or 0)
        if act < f.get("region_min_active", 3):
            continue
        ratio_r = okr / act
        if ratio_r < f["min_region_ok_ratio"]:
            fails.append(f"{label} 언론 소스 정상 비율 낮음: {okr}/{act} (기준 {f['min_region_ok_ratio']:.0%} 이상)")
        elif ratio_r < w["min_region_ok_ratio"]:
            warns.append(f"{label} 언론 소스 정상 비율 낮음: {okr}/{act}")

    if (m["news_articles"] or 0) < f["min_news_articles"]:
        fails.append(f"뉴스 기사 부족: {m['news_articles']}건 (기준 {f['min_news_articles']}건 이상)")
    if (m["kr_candidates"] or 0) < f["min_kr_candidates"]:
        fails.append(f"국내 후보 부족: {m['kr_candidates']}건 (기준 {f['min_kr_candidates']}건 이상)")
    if (m["intl_candidates"] or 0) < f["min_intl_candidates"]:
        fails.append(f"해외 후보 부족: {m['intl_candidates']}건 (기준 {f['min_intl_candidates']}건 이상)")

    c = news.get("counts", {})
    for region, label in (("kr", "국내"), ("intl", "해외")):
        quota = int(c.get(f"top_quota_{region}") or 0)
        got = int(c.get(f"top_{region}") or 0)
        if quota and got < quota * f["min_top_fill_ratio"]:
            fails.append(f"주요 뉴스 {label} 할당 미달: {got}/{quota}")

    if baseline:
        def drop(key: str) -> float | None:
            base = baseline.get(key) or 0
            return None if base <= 0 else 1 - (m.get(key) or 0) / base

        d = drop("news_articles")
        if d is not None and d > f["max_news_drop_vs_baseline"]:
            fails.append(f"기사 수 급감: {baseline['news_articles']} → {m['news_articles']} ({d:.0%} 감소)")
        elif d is not None and d > w["max_news_drop_vs_baseline"]:
            warns.append(f"기사 수 감소: {baseline['news_articles']} → {m['news_articles']} ({d:.0%} 감소)")
        for key, label in (("kr_candidates", "국내"), ("intl_candidates", "해외")):
            d = drop(key)
            if d is not None and d > f["max_region_drop_vs_baseline"]:
                fails.append(f"{label} 후보 급감: {baseline[key]} → {m[key]} ({d:.0%} 감소)")

    summary = status.get("summary", {})
    for key, label in (("failed", "요청·파싱 실패"), ("empty", "0건·유효 기사 0"), ("stale", "오래됨")):
        if summary.get(key):
            warns.append(f"{label} 소스 {len(summary[key])}곳: {', '.join(summary[key])}")
    long_fail = [
        s["name"]
        for s in status.get("sources", [])
        if (s.get("history") or {}).get("consecutive_failures", 0) >= w["max_consecutive_failures"]
    ]
    if long_fail:
        warns.append(f"{w['max_consecutive_failures']}회 이상 연속 실패: {', '.join(long_fail)}")

    return {
        "passed": not fails,
        "fails": fails,
        "warnings": warns,
        "metrics": m,
        "baseline": baseline,
        "first_run": baseline is None,
    }


def main() -> int:
    ap = argparse.ArgumentParser(description="배포 전 품질 검사")
    ap.add_argument("--data-dir", default="data")
    ap.add_argument("--config", default="")
    ap.add_argument("--site-url", default=os.environ.get("SITE_URL", ""))
    args = ap.parse_args()

    data = Path(args.data_dir)
    state_dir = data / "state"
    conf = load_quality_config(Path(args.config) if args.config else None)
    news = json.loads((data / "news.json").read_text(encoding="utf-8"))
    status = json.loads((data / "status.json").read_text(encoding="utf-8"))
    baseline, baseline_from = load_baseline(state_dir, args.site_url, news.get("generated_at"))

    result = evaluate(news, status, baseline, conf)
    result["baseline_from"] = baseline_from
    result["checked_at"] = news.get("generated_at")
    result["coverage"] = news.get("coverage")

    # 배포본에 함께 실리는 건강 정보. 통과했을 때만 배포되므로 '마지막 정상 갱신' 정보가 된다.
    write_json_atomic(data / "health.json", result, pretty=True)
    # 검사 기록은 성공·실패 모두 캐시에 남긴다 (원인 추적용)
    write_json_atomic(state_dir / "last_check.json", result, pretty=True)
    if result["passed"]:
        write_json_atomic(state_dir / "last_good.json", {"metrics": result["metrics"]}, pretty=True)
        runs = recent_runs(load_history(state_dir) + [result["metrics"]], news.get("generated_at"))
        write_json_atomic(state_dir / "metrics_history.json", {"runs": runs}, pretty=True)

    lines = [
        f"## 품질 검사: {'통과' if result['passed'] else '실패 — 배포 보류, 기존 페이지 유지'}",
        f"- 기준값: {baseline_from}" + (" (첫 실행: 절대 기준만 검사)" if result["first_run"] else ""),
        f"- 수치: {json.dumps(result['metrics'], ensure_ascii=False)}",
    ]
    lines += [f"- ❌ {x}" for x in result["fails"]]
    lines += [f"- ⚠️ {x}" for x in result["warnings"]]
    text = "\n".join(lines)
    print(text)
    summary_path = os.environ.get("GITHUB_STEP_SUMMARY")
    if summary_path:
        with open(summary_path, "a", encoding="utf-8") as fh:
            fh.write(text + "\n")
    return 0 if result["passed"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
