"""취향 탭: 프로필(config/taste.json) 기반 규칙 선정.

판단 항목을 따로 계산한다.
  relevance  관심 주제 관련도. 개념(낱말 묶음) 단위로 세고, strong 개념 1개 이상 + 가중치 합 기준.
             낱말이 한 번 나왔다는 이유만으로는 관련 있다고 보지 않는다.
  nature     글의 성격 (분석·질문·비판·근거 표기). 제목·RSS 요약의 표기만 본다.
             요약이 짧아 판단 재료가 없으면 depth='unknown' 으로 두고 중립값을 쓴다 (높이지도 낮추지도 않음).
  source     출처 성격 (연구기관·전문지·일반 언론·집계 경유).
  freshness  발행일 기준 최신성 (반감기).
  penalties  주가 등락·가격 전망·홍보·자극적 제목·속보·목록형 글 감점.
  duplicate  같은 사건(묶음)·같은 제목 재게시는 하나만.

후보 보관: data/state/taste_candidates.json 에 최근 window_days(28일) 후보를 따로 둔다.
일반 아카이브 보관 기간(뉴스 3일)은 늘리지 않는다.
"""

from __future__ import annotations

import json
import math
import re
from collections import Counter, defaultdict
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

PROFILE_PATH = Path(__file__).resolve().parents[1] / "config" / "taste.json"
STORE_FIELDS = (
    "id", "title", "url", "outlet", "summary", "published_at", "updated_at", "time_basis", "region", "section",
    "feed", "feed_category", "via", "first_seen_at", "last_seen_at",
)


def load_profile(path: Path | None = None) -> dict[str, Any]:
    prof = json.loads(Path(path or PROFILE_PATH).read_text(encoding="utf-8"))
    prof["_compiled"] = compile_profile(prof)
    return prof


# ---------------------------------------------------------------- 낱말 일치


def _term_re(term: str) -> re.Pattern:
    """한글·한자가 있으면 부분 일치, 영어는 앞뒤가 영숫자가 아닐 때만(한글 조사 '가'·'는' 앞도 허용)."""
    if term.startswith("\\"):
        return re.compile(term, re.I)  # 정규식 그대로
    prefix = term.endswith("*")
    t = re.escape(term.rstrip("*"))
    if re.search(r"[가-힣一-鿿]", term) or not re.search(r"[A-Za-z0-9]", term):
        return re.compile(t, re.I)
    # 끝 글자가 영숫자일 때만 뒤쪽 경계를 본다 ("Newsletter #" 다음의 숫자는 허용)
    tail = r"" if prefix or not term.rstrip("*")[-1:].isalnum() else r"(?![A-Za-z0-9])"
    return re.compile(rf"(?<![A-Za-z0-9]){t}{tail}", re.I)


def compile_profile(prof: dict[str, Any]) -> dict[str, Any]:
    topics = {}
    for tid, t in prof["topics"].items():
        topics[tid] = {
            name: (c["strong"], [_term_re(x) for x in c["terms"]], c.get("family", name))
            for name, c in t["concepts"].items()
        }
    nature = {k: [_term_re(x) for x in prof["nature"][k]] for k in ("analysis", "question", "critical", "evidence")}
    pen = {k: (v["factor"], [_term_re(x) for x in v["terms"]]) for k, v in prof["penalties"].items() if not k.startswith("_")}
    return {"topics": topics, "nature": nature, "penalties": pen}


def _hits(patterns: list[re.Pattern], text: str) -> bool:
    return any(p.search(text) for p in patterns)


# ---------------------------------------------------------------- 판단


def relevance(prof: dict[str, Any], title: str, summary: str, outlet: str = "") -> dict[str, dict[str, Any]]:
    """주제별 관련도. 개념 단위(같은 개념은 한 번), 제목에 나오면 가산. 주제 전문 매체는 개념 1개로 센다."""
    rc = prof["relevance"]
    out = {}
    specialty = prof.get("outlet_topics", {}).get(outlet)
    for tid, concepts in prof["_compiled"]["topics"].items():
        matched, weight, strong_hit = [], 0.0, False
        families: set[str] = set()
        if specialty == tid:
            matched.append("전문 매체")
            families.add("전문 매체")
            weight += 1.0
            strong_hit = True  # 그래도 서로 다른 개념이 하나 더 있어야 한다
        for name, (strong, pats, family) in concepts.items():
            in_title = _hits(pats, title)
            in_sum = _hits(pats, summary) if summary else False
            if not (in_title or in_sum):
                continue
            w = (1.0 if strong else 0.5) + (rc["title_bonus"] if in_title else 0.0)
            matched.append(name)
            families.add(family)
            weight += w
            strong_hit = strong_hit or strong
        qualifies = strong_hit and weight >= rc["min_weight"] and len(families) >= 2
        out[tid] = {
            "concepts": matched,
            "weight": round(weight, 2),
            "qualifies": qualifies,
            "score": round(min(1.0, weight / rc["full_at"]), 3) if qualifies else 0.0,
        }
    return out


def nature(prof: dict[str, Any], title: str, summary: str) -> dict[str, Any]:
    n = prof["nature"]
    pats = prof["_compiled"]["nature"]
    text = f"{title} {summary}"
    signals = [k for k in ("analysis", "question", "critical", "evidence") if _hits(pats[k], text)]
    has_material = len(summary or "") >= n["min_summary_for_depth"]
    if not signals and not has_material:
        return {"signals": [], "depth": "unknown", "score": n["unknown_depth_score"]}
    score = 0.3 + 0.2 * ("analysis" in signals) + 0.15 * ("question" in signals) + 0.15 * ("critical" in signals) \
        + 0.1 * ("evidence" in signals) + (0.1 if has_material else 0.0)
    return {"signals": signals, "depth": "observed" if has_material else "title_only", "score": round(min(1.0, score), 3)}


def source_score(prof: dict[str, Any], rec: dict[str, Any]) -> tuple[str, float]:
    s = prof["sources"]
    if rec.get("via") in s["aggregator_via"]:
        tier = "aggregator"
    elif rec.get("outlet") in s["research"]:
        tier = "research"
    elif rec.get("outlet") in s["specialist"]:
        tier = "specialist"
    else:
        tier = "general"
    return tier, s["tiers"][tier]


def penalties(prof: dict[str, Any], title: str) -> tuple[list[str], float]:
    found, factor = [], 1.0
    for name, (f, pats) in prof["_compiled"]["penalties"].items():
        if _hits(pats, title):
            found.append(name)
            factor *= f
    return found, round(factor, 3)


def country_of(prof: dict[str, Any], rec: dict[str, Any]) -> str:
    return prof["outlet_country"].get(rec.get("outlet", ""), prof["default_country"].get(rec.get("region"), "INT"))


def evaluate(prof: dict[str, Any], rec: dict[str, Any], now: datetime, published: datetime | None) -> dict[str, Any]:
    title, summary = rec.get("title", ""), rec.get("summary", "") or ""
    rel = relevance(prof, title, summary, rec.get("outlet", ""))
    matched_topics = [t for t, r in rel.items() if r["qualifies"]]
    best_topic = max(matched_topics, key=lambda t: rel[t]["score"], default=None)
    nat = nature(prof, title, summary)
    tier, src = source_score(prof, rec)
    pens, factor = penalties(prof, title)
    age_days = (now - published).total_seconds() / 86400 if published else None
    fresh = math.pow(0.5, age_days / prof["freshness_half_life_days"]) if age_days is not None else 0.0
    connection = len(matched_topics) >= 2
    w = prof["weights"]
    interest = 0.0
    if best_topic:
        interest = (
            w["interest"]["relevance"] * rel[best_topic]["score"]
            + w["interest"]["nature"] * nat["score"]
            + w["interest"]["source"] * src
            + w["interest"]["freshness"] * fresh
            + (prof["nature"]["connection_bonus"] if connection else 0.0)
        ) * factor
        interest = min(1.0, interest)
    broaden = (w["broaden"]["nature"] * nat["score"] + w["broaden"]["source"] * src + w["broaden"]["freshness"] * fresh) * factor
    max_rel_weight = max((r["weight"] for r in rel.values()), default=0.0)
    return {
        "relevance": {t: r for t, r in rel.items() if r["concepts"]},
        "topics": sorted(matched_topics, key=lambda t: -rel[t]["score"]),
        "best_topic": best_topic,
        "connection": connection,
        "nature": nat,
        "source_tier": tier,
        "source": src,
        "freshness": round(fresh, 3),
        "age_days": round(age_days, 1) if age_days is not None else None,
        "penalties": pens,
        "penalty_factor": factor,
        "interest_score": round(interest, 4),
        "broaden_score": round(broaden, 4),
        "max_relevance_weight": max_rel_weight,
        "country": country_of(prof, rec),
    }


# ---------------------------------------------------------------- 후보 보관


def is_candidate(prof: dict[str, Any], rec: dict[str, Any]) -> bool:
    """보관할 후보: 관심 개념이 하나라도 있거나, 취향·과학 섹션이거나, 연구·전문 출처. 발행일 필수."""
    if not rec.get("published_at"):
        return False
    if rec.get("section") in ("taste", "science"):
        return True
    if rec.get("outlet") in prof["sources"]["research"] or rec.get("outlet") in prof["sources"]["specialist"]:
        return True
    rel = relevance(prof, rec.get("title", ""), rec.get("summary", "") or "", rec.get("outlet", ""))
    return any(r["concepts"] for r in rel.values())


def load_store(path: Path) -> dict[str, Any]:
    if path.exists():
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
            return {"started_at": data.get("started_at"), "items": {i["id"]: i for i in data.get("items", [])}}
        except Exception:  # noqa: BLE001
            pass
    return {"started_at": None, "items": {}}


def update_store(
    store: dict[str, Any], records: list[dict[str, Any]], prof: dict[str, Any], now: datetime, now_iso: str, parse_iso
) -> dict[str, int]:
    items = store["items"]
    if not store.get("started_at"):
        store["started_at"] = now_iso
    added = 0
    for rec in records:
        if not is_candidate(prof, rec):
            continue
        slim = {k: rec.get(k) for k in STORE_FIELDS}
        if slim["summary"]:
            slim["summary"] = slim["summary"][:400]
        if rec["id"] not in items:
            added += 1
            items[rec["id"]] = slim
        else:
            first = items[rec["id"]].get("first_seen_at")
            items[rec["id"]].update(slim)
            items[rec["id"]]["first_seen_at"] = first or slim["first_seen_at"]
    keep_after = now - timedelta(days=prof["window_days"])
    for k in [k for k, v in items.items() if (parse_iso(v.get("published_at")) or now) < keep_after]:
        del items[k]
    return {"added": added, "total": len(items)}


# ---------------------------------------------------------------- 선정


def _title_key(title: str) -> str:
    return re.sub(r"[^0-9a-z가-힣]", "", (title or "").lower())[:40]


def select(
    prof: dict[str, Any], candidates: list[dict[str, Any]], now: datetime, parse_iso, cluster_fn, tokens_fn=None
) -> dict[str, Any]:
    """관심 분야 / 시야 넓히기를 따로 고른다. 기준 미달이면 건수를 채우지 않는다."""
    th, comp, div = prof["thresholds"], prof["composition"], prof["diversity"]
    window_start = now - timedelta(days=prof["window_days"])
    pool = []
    for rec in candidates:
        t = parse_iso(rec.get("published_at"))
        if t is None or t < window_start or t > now + timedelta(hours=1):
            continue
        ev = evaluate(prof, rec, now, t)
        pool.append({"rec": rec, "ev": ev, "t": t})

    # 같은 사건·재게시 정리: 지역별 사건 묶기 + 같은 제목 → 묶음마다 점수 최고 1건만 대표
    groups = cluster_fn([p["rec"] for p in pool])
    group_of: dict[str, int] = {}
    for gi, members in enumerate(groups):
        for m in members:
            group_of[m["id"]] = gi
    title_groups: dict[str, int] = {}
    for p in pool:
        key = _title_key(p["rec"]["title"])
        gid = group_of.get(p["rec"]["id"], -1)
        if key in title_groups:  # 다른 매체에 같은 제목으로 재게시
            group_of[p["rec"]["id"]] = title_groups[key]
        else:
            title_groups[key] = gid
        p["group"] = group_of.get(p["rec"]["id"], -1)
        p["group_size"] = len(groups[p["group"]]) if p["group"] >= 0 else 1

    picked: list[dict[str, Any]] = []
    used_groups: set[int] = set()
    per_outlet: Counter[str] = Counter()
    per_country: Counter[str] = Counter()
    total_cap = comp["interest"] + comp["broaden"]
    skipped = Counter()

    tok = tokens_fn or (lambda t: set(re.findall(r"[가-힣]{2}|[a-z0-9]{3,}", (t or "").lower())))
    picked_tokens: list[set[str]] = []
    sim_limit = div.get("similar_title_jaccard", 0.3)

    def can_take(p: dict[str, Any]) -> str | None:
        if p["group"] in used_groups:
            return "same_story"
        ts = tok(p["rec"]["title"])
        for other in picked_tokens:
            if ts and other and len(ts & other) / len(ts | other) >= sim_limit:
                return "similar_title"  # 같은 소재의 다른 기사(사설·후속 등)
        if per_outlet[p["rec"]["outlet"]] >= div["max_per_outlet"]:
            return "outlet_cap"
        if per_country[p["ev"]["country"]] >= max(1, math.floor(total_cap * div["max_country_share"])):
            return "country_cap"
        return None

    def take(p: dict[str, Any], kind: str, area: str) -> None:
        used_groups.add(p["group"])
        picked_tokens.append(tok(p["rec"]["title"]))
        per_outlet[p["rec"]["outlet"]] += 1
        per_country[p["ev"]["country"]] += 1
        picked.append({**p, "kind": kind, "area": area})

    # 1) 관심 분야
    interest_pool = sorted(
        [p for p in pool if p["ev"]["best_topic"] and p["ev"]["interest_score"] >= th["interest"]],
        key=lambda p: (-p["ev"]["interest_score"], p["rec"]["id"]),
    )
    per_topic: Counter[str] = Counter()
    n_interest = 0

    def try_interest(p: dict[str, Any]) -> bool:
        nonlocal n_interest
        topic = p["ev"]["best_topic"]
        if per_topic[topic] >= div["max_per_topic_interest"]:
            skipped["topic_cap"] += 1
            return False
        why = can_take(p)
        if why:
            skipped[why] += 1
            return False
        take(p, "interest", topic)
        per_topic[topic] += 1
        n_interest += 1
        return True

    # 1차: 주제마다 가장 좋은 글 1건 (주제의 최고 점수 순) → 한 주제가 자리를 독점하지 않게
    by_topic: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for p in interest_pool:
        by_topic[p["ev"]["best_topic"]].append(p)
    for topic in sorted(by_topic, key=lambda t: -by_topic[t][0]["ev"]["interest_score"]):
        for p in by_topic[topic]:
            if n_interest >= comp["interest"]:
                break
            if try_interest(p):
                break
    # 2차: 남은 자리를 점수 순으로
    for p in interest_pool:
        if n_interest >= comp["interest"]:
            break
        if any(q["rec"]["id"] == p["rec"]["id"] for q in picked):
            continue
        try_interest(p)

    # 2) 시야 넓히기: 관심 주제 관련도가 낮은 글 중 성격·출처가 좋은 것
    areas = prof["broaden_areas"]
    broaden_pool = sorted(
        [
            p for p in pool
            if not p["ev"]["best_topic"]
            and p["ev"]["max_relevance_weight"] <= th["broaden_max_relevance"]
            and p["ev"]["broaden_score"] >= th["broaden"]
            and p["ev"]["nature"]["depth"] != "unknown"
            and {"analysis", "question"} & set(p["ev"]["nature"]["signals"])
        ],
        key=lambda p: (-p["ev"]["broaden_score"], p["rec"]["id"]),
    )
    per_area: Counter[str] = Counter()
    n_broaden = 0
    for p in broaden_pool:
        if n_broaden >= comp["broaden"]:
            break
        area = areas.get(p["rec"].get("feed_category") or "general", "기타")
        if per_area[area] >= div["max_per_broaden_area"]:
            skipped["broaden_area_cap"] += 1
            continue
        why = can_take(p)
        if why:
            skipped[why] += 1
            continue
        take(p, "broaden", area)
        per_area[area] += 1
        n_broaden += 1

    # 분야별 후보 현황 (부족 분야 판단)
    topic_stats = {}
    for tid, t in prof["topics"].items():
        related = [p for p in pool if tid in p["ev"]["topics"]]
        eligible = [p for p in related if p["ev"]["interest_score"] >= th["interest"] and p["ev"]["best_topic"] == tid]
        times = [p["t"] for p in related]
        topic_stats[tid] = {
            "label": t["label"],
            "related_candidates": len(related),
            "eligible": len(eligible),
            "selected": per_topic.get(tid, 0),
            "oldest": min(times).isoformat() if times else None,
            "newest": max(times).isoformat() if times else None,
            "short": len(eligible) < 2,
        }
    return {
        "picked": picked,
        "pool_size": len(pool),
        "interest_eligible": len(interest_pool),
        "broaden_eligible": len(broaden_pool),
        "skipped": dict(skipped),
        "topic_stats": topic_stats,
        "shortfall": {
            "interest": max(0, comp["interest"] - n_interest),
            "broaden": max(0, comp["broaden"] - n_broaden),
        },
    }


# ---------------------------------------------------------------- 출력


def tags_for(prof: dict[str, Any], p: dict[str, Any]) -> list[str]:
    ev = p["ev"]
    tags: list[str] = []
    if p["kind"] == "interest":
        tags.append(prof["topics"][p["area"]]["label"])
        rel = ev["relevance"].get(p["area"], {})
        tags += [c for c in rel.get("concepts", []) if c not in tags and c != "전문 매체"][:2]
        for other in ev["topics"]:
            if other != p["area"] and len(tags) < 3:
                tags.append(prof["topics"][other]["label"])
    else:
        tags.append(p["area"])
        if "critical" in ev["nature"]["signals"]:
            tags.append("비판·위험")
        if ev["source_tier"] == "research":
            tags.append("연구")
    return tags[:3]


def reason_text(prof: dict[str, Any], p: dict[str, Any]) -> dict[str, Any]:
    ev = p["ev"]
    sig_ko = {"analysis": "분석·해설 표기", "question": "질문 제기", "critical": "비판·위험 분석", "evidence": "근거(수치·조사) 표기"}
    return {
        "kind": p["kind"],
        "area": p["area"],
        "score": ev["interest_score"] if p["kind"] == "interest" else ev["broaden_score"],
        "relevance": {prof["topics"][t]["label"]: r["concepts"] for t, r in ev["relevance"].items() if r["qualifies"]},
        "connection": ev["connection"],
        "nature": [sig_ko[s] for s in ev["nature"]["signals"]],
        "depth": {"observed": "요약 있음", "title_only": "제목만으로 판단", "unknown": "판단 불가"}[ev["nature"]["depth"]],
        "source_tier": ev["source_tier"],
        "age_days": ev["age_days"],
        "penalties": ev["penalties"],
        "country": ev["country"],
        "duplicates_collapsed": p["group_size"] - 1,
    }


def build_payload(prof: dict[str, Any], result: dict[str, Any], store: dict[str, Any], now_iso: str) -> dict[str, Any]:
    items = []
    for p in result["picked"]:
        r = p["rec"]
        items.append(
            {
                "id": r["id"],
                "kind": p["kind"],
                "area": p["area"],
                "area_label": prof["topics"][p["area"]]["label"] if p["kind"] == "interest" else p["area"],
                "title": r["title"],
                "url": r["url"],
                "outlet": r["outlet"],
                "published_at": r["published_at"],
                "tags": tags_for(prof, p),
                # 검토용. 화면에는 표시하지 않는다.
                "review": reason_text(prof, p),
            }
        )
    times = sorted(v["published_at"] for v in store["items"].values() if v.get("published_at"))
    return {
        "generated_at": now_iso,
        "profile_version": prof["version"],
        "composition": prof["composition"],
        "items": items,
        "shortfall": result["shortfall"],
        "coverage": {
            "window_days": prof["window_days"],
            "store_started_at": store.get("started_at"),
            "oldest_candidate": times[0] if times else None,
            "newest_candidate": times[-1] if times else None,
            "candidates": len(store["items"]),
            "in_window": result["pool_size"],
            "interest_eligible": result["interest_eligible"],
            "broaden_eligible": result["broaden_eligible"],
        },
        "topics": result["topic_stats"],
        "skipped": result["skipped"],
    }
