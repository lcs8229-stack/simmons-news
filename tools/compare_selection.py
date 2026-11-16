#!/usr/bin/env python3
"""같은 기사 표본에 기존·변경 선정 방식을 적용해 비교한다.

사용:
  git show <기존커밋>:scripts/build_news.py > /tmp/old_build_news.py
  python tools/compare_selection.py --archive data/archive.json --old /tmp/old_build_news.py

측정 (두 방식에 같은 기준을 쓴다):
  - 오병합 의심: 한 사건 안에 의미 충돌(다른 주어·금액·날짜·반대 표현) 제목 쌍이 있는 사건 수,
                시간 폭이 24시간을 넘는 사건 수
  - 중복: 주요 뉴스 안에서 제목 유사도가 높은(같은 이슈로 볼 만한) 사건 쌍
  - 주요 기사 누락: 주요 뉴스 ID 중 공개 목록(news)에 없는 것, 4곳 이상 보도했는데 주요 뉴스에 없는 사건
  - 집중도: 주요 뉴스의 분야별·대표 언론사별 비율, 할당별 분야 상한(40%) 초과
"""

from __future__ import annotations

import argparse
import importlib.util
import itertools
import json
import math
import sys
from collections import Counter
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))
import build_news as new  # noqa: E402
from outlets import load_policy  # noqa: E402


def load_module(path: Path, name: str):
    spec = importlib.util.spec_from_file_location(name, path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def run_old(old, archive: dict, now) -> dict:
    """기존 main() 의 선정 부분을 그대로 재현."""
    now_iso = old.iso(now)
    news_after = now - old.timedelta(hours=old.NEWS_WINDOW_HOURS)
    news_recs, news_undated = [], []
    for rec in archive.values():
        if rec["section"] == "science":
            continue
        t = old.article_time(rec)
        if t is not None and t > now + old.FUTURE_TOLERANCE:
            t = None
        if t is None:
            if rec.get("last_seen_at") == now_iso:
                news_undated.append(rec)
            continue
        if t >= news_after:
            news_recs.append(rec)
    groups, _ = old.cluster_with_undated(news_recs, news_undated)
    stories = [old.build_story(g, now) for g in groups]
    stories.sort(key=lambda s: s["score"], reverse=True)
    top = old.select_top(stories, old.TOP_TOTAL)
    visible = [s for s in stories if not s["low_value"]]
    return {"stories": stories, "top": top, "published": visible[:600], "groups": groups}


def run_new(archive: dict, now) -> dict:
    out = new.compose(archive, now, new.TOP_TOTAL, {"articles": {}, "stories": {}})
    return {
        "stories": out["news_stories"],
        "top": out["top"],
        "published": out["published_news"],
        "selection": out["selection"],
        "issues": out["issues"],
        "blocked": out["cluster_stats"],
    }


def metrics(res: dict, archive: dict, idf: dict) -> dict:
    stories, top, published = res["stories"], res["top"], res["published"]
    pub_ids = {s["id"] for s in published}
    top_ids = {s["id"] for s in top}

    def members(s):
        return [archive[a["id"]] for a in s["articles"] if a["id"] in archive]

    # 오병합 의심
    conflict_stories, conflict_examples, long_span = 0, [], 0
    for s in stories:
        ms = members(s)
        if len(ms) < 2:
            continue
        times = [t for t in (new.article_time(m) for m in ms) if t]
        if times and (max(times) - min(times)).total_seconds() > new.CLUSTER_MAX_SPAN_HOURS * 3600:
            long_span += 1
        for a, b in itertools.combinations(ms, 2):
            why = new.conflict_reason(a["title"], b["title"])
            if why:
                conflict_stories += 1
                if len(conflict_examples) < 6:
                    conflict_examples.append((why, a["title"][:40], b["title"][:40]))
                break

    # 주요 뉴스 안의 같은 이슈 중복: 두 사건의 구성 기사 제목 낱말 중 희귀 낱말 공유량 (두 방식에 같은 기준)
    def profile(s):
        toks = set()
        for m in members(s):
            toks |= new.title_tokens(m["title"])
        return toks - new.ISSUE_STOP_TOKENS

    prof = {s["id"]: profile(s) for s in top}
    dup_pairs = []
    for a, b in itertools.combinations(top, 2):
        common = prof[a["id"]] & prof[b["id"]]
        if len(common) >= 3 and sum(idf.get(t, 1.0) for t in common) >= new.ISSUE_MIN_SHARED_WEIGHT:
            dup_pairs.append((a["title"][:34], b["title"][:34]))
    # 이슈별 칸 수 (연결 그래프의 연결 요소)
    adj = {s["id"]: set() for s in top}
    for a, b in itertools.combinations(top, 2):
        common = prof[a["id"]] & prof[b["id"]]
        if len(common) >= 3 and sum(idf.get(t, 1.0) for t in common) >= new.ISSUE_MIN_SHARED_WEIGHT:
            adj[a["id"]].add(b["id"])
            adj[b["id"]].add(a["id"])
    seen, comp_sizes = set(), []
    for sid in adj:
        if sid in seen:
            continue
        stack, size = [sid], 0
        while stack:
            x = stack.pop()
            if x in seen:
                continue
            seen.add(x)
            size += 1
            stack.extend(adj[x] - seen)
        comp_sizes.append(size)

    # 지역: 두 방식 모두 '사건 대상' 기준으로 다시 계산해 비교 (기존 region 은 언론사 소재)
    def target(s):
        return new.quota_bucket(new.story_target(members(s))[0]) if members(s) else s["region"]

    by_bucket: dict[str, Counter] = {"kr": Counter(), "intl": Counter()}
    for s in top:
        by_bucket[target(s)][s["category"]] += 1
    cap_violations = []
    for b, c in by_bucket.items():
        n = sum(c.values())
        for cat, k in c.items():
            if n and k / n > 0.4 and not (b == "intl" and cat == "world"):
                cap_violations.append(f"{b}:{cat} {k}/{n}")
    rep_outlets = Counter(s["outlet"] for s in top)
    big_missing = [s for s in stories if s["outlet_count"] >= 4 and s["id"] not in top_ids and not s["low_value"]]
    return {
        "stories": len(stories),
        "multi_article_stories": sum(1 for s in stories if s["article_count"] > 1),
        "max_cluster_size": max(s["article_count"] for s in stories),
        "suspect_overmerge_stories": conflict_stories,
        "overmerge_examples": conflict_examples,
        "span_over_24h_stories": long_span,
        "top_count": len(top),
        "top_by_source_region": dict(Counter(s.get("source_region", s["region"]) for s in top)),
        "top_by_target_bucket": {b: sum(c.values()) for b, c in by_bucket.items()},
        "top_category_by_bucket": {b: dict(c) for b, c in by_bucket.items()},
        "category_cap_violations": cap_violations,
        "top_rep_outlets": dict(rep_outlets.most_common(6)),
        "top_max_outlet_share": round(max(rep_outlets.values()) / max(1, len(top)), 2),
        "top_missing_from_published": len(top_ids - pub_ids),
        "same_issue_pairs_in_top": len(dup_pairs),
        "max_slots_one_issue_in_top": max(comp_sizes) if comp_sizes else 0,
        "same_issue_examples": dup_pairs[:6],
        "big_stories_not_in_top": len(big_missing),
        "big_not_in_top_examples": [(s["outlet_count"], s["title"][:40]) for s in big_missing[:5]],
    }


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--archive", required=True)
    ap.add_argument("--old", required=True, help="기존 build_news.py 사본 경로")
    ap.add_argument("--json", default="", help="결과를 JSON 으로 저장")
    args = ap.parse_args()

    raw = json.loads(Path(args.archive).read_text(encoding="utf-8"))
    now = new.parse_iso(raw["generated_at"])
    table = load_policy()
    archive = new.load_archive(Path(args.archive), table)
    old = load_module(Path(args.old), "old_build_news")

    recs = [r for r in archive.values() if r["section"] != "science"]
    df = Counter(t for r in recs for t in new.title_tokens(r["title"]))
    n = max(200, len(recs))
    idf = {t: math.log((n + 1) / (c + 0.5)) for t, c in df.items()}

    old_res = run_old(old, archive, now)
    new_res = run_new(archive, now)
    result = {
        "sample": {"generated_at": raw["generated_at"], "articles": len(archive)},
        "old": metrics(old_res, archive, idf),
        "new": metrics(new_res, archive, idf),
        "new_selection": new_res["selection"],
        "new_merge_blocked": new_res["blocked"],
        "new_issues_multi": len(new_res["issues"]),
    }
    text = json.dumps(result, ensure_ascii=False, indent=2)
    if args.json:
        Path(args.json).write_text(text, encoding="utf-8")
    print(text)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
