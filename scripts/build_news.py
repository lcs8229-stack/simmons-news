#!/usr/bin/env python3
"""시몬스 뉴스: RSS 수집 → 아카이브 → 사건 묶기 → 순위 → 국내 60 / 해외 40 선정.

API 키 없이 동작한다. 중요도는 "같은 사건을 보도한 언론사 수"와 최신성으로 매긴다.
Claude 편집 단계(scripts/ai_editor.py)는 이 출력 위에 요약·번역·정밀 분류를 덧붙인다.
"""

from __future__ import annotations

import argparse
import html
import json
import math
import re
import sys
import time
import xml.etree.ElementTree as ET
from collections import Counter, defaultdict
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

import feedparser
import requests

sys.path.insert(0, str(Path(__file__).resolve().parent))
from radar_lib import (  # noqa: E402
    BROWSER_UA,
    UTC,
    iso,
    make_item_id,
    normalize_url,
    parse_date_any,
    parse_iso,
    sanitize_public_payload,
    utc_now,
)

# ---------------------------------------------------------------- 설정

CATEGORIES = {
    "politics": "정치",
    "economy": "경제",
    "society": "사회",
    "culture": "문화",
    "world": "국제",
    "northkorea": "북한",
    "science": "과학",
    "energy": "전기·에너지",
    "general": "종합",
}

# 주요 뉴스 비중: 국내 60%, 해외 40%
TOP_TOTAL = 40
REGION_SHARE = {"kr": 0.6, "intl": 0.4}
# 한 분야가 주요 뉴스를 독식하지 않도록 지역별 상한 비율
CATEGORY_CAP = 0.4

NEWS_WINDOW_HOURS = 36
SCIENCE_WINDOW_DAYS = 7
JOURNAL_WINDOW_DAYS = 31
ARCHIVE_DAYS = 31
HALF_LIFE_HOURS = 10.0

# 대표 기사 선정 시 우선할 언론사 (사실 위주 통신사·방송 우선)
OUTLET_PRIORITY = ["연합뉴스", "Reuters", "AP", "BBC", "SBS", "MBC", "JTBC"]

# 과학·전기 탭에서 먼저 보여줄 학술지·전문 저널
JOURNAL_OUTLETS = {
    "Nature", "Science", "Cell", "PNAS", "Nature Energy", "Nature Electronics",
    "IEEE Spectrum", "Quanta", "전기저널", "IEEE TPWRS", "IEEE TPEL",
}

# ---------------------------------------------------------------- 소스


def load_sources(opml_path: Path) -> list[dict[str, str]]:
    root = ET.parse(opml_path).getroot()
    out: list[dict[str, str]] = []
    seen: set[str] = set()
    for node in root.iter("outline"):
        url = (node.attrib.get("xmlUrl") or "").strip()
        if not url or url in seen:
            continue
        seen.add(url)
        if node.attrib.get("enabled", "true").lower() == "false":
            continue
        a = node.attrib
        out.append(
            {
                "name": a.get("text") or url,
                "url": url,
                "outlet": a.get("outlet", ""),
                "region": a.get("region", "kr"),
                "section": a.get("section", "news"),
                "category": a.get("category", "general"),
                "lean": a.get("lean", ""),
                "gnews": a.get("gnews", "") == "1",
                "ai_ok": a.get("ai", "yes").lower() != "no",
            }
        )
    return out


TAG_RE = re.compile(r"<[^>]+>")
WS_RE = re.compile(r"\s+")


def clean_text(raw: str, limit: int = 280) -> str:
    s = html.unescape(TAG_RE.sub(" ", raw or ""))
    s = WS_RE.sub(" ", s).strip()
    if len(s) > limit:
        s = s[: limit - 1].rstrip() + "…"
    return s


def split_gnews_title(title: str) -> tuple[str, str]:
    """'제목 - 언론사' → ('제목', '언론사')."""
    m = re.match(r"^(.*\S)\s+[-–—]\s+([^-–—]{1,40})$", title.strip())
    if not m:
        return title.strip(), ""
    return m.group(1).strip(), m.group(2).strip()


def fetch_source(src: dict[str, str], now: datetime) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    start = time.perf_counter()
    items: list[dict[str, Any]] = []
    error = None
    try:
        resp = None
        for attempt in range(3):  # 일시적 403/연결 끊김(한국경제·MBC 등) 대비
            try:
                resp = requests.get(
                    src["url"],
                    timeout=15,
                    headers={"User-Agent": BROWSER_UA, "Accept-Language": "ko-KR,ko;q=0.9,en;q=0.8"},
                )
                if resp.status_code in (403, 429, 500, 502, 503) and attempt < 2:
                    time.sleep(2 + 3 * attempt)
                    continue
                break
            except requests.RequestException:
                if attempt == 2:
                    raise
                time.sleep(2 + 3 * attempt)
        assert resp is not None
        resp.raise_for_status()
        parsed = feedparser.parse(resp.content)
        for e in parsed.entries:
            title = clean_text(str(e.get("title", "")), 300)
            link = str(e.get("link", "")).strip()
            if not title or not link.startswith("http"):
                continue
            outlet = src["outlet"]
            if src["gnews"]:
                title, gn_outlet = split_gnews_title(title)
                gn_source = (e.get("source") or {}).get("title", "") if isinstance(e.get("source"), dict) else ""
                found = gn_source or gn_outlet
                # 언론사가 지정된 Google 뉴스 검색은 그 언론사 기사만 남긴다.
                if outlet and found and found != outlet:
                    continue
                outlet = outlet or found or "Google 뉴스"
            published = (
                parse_date_any(e.get("published"), now)
                or parse_date_any(e.get("updated"), now)
                or parse_date_any(e.get("dc_date"), now)
            )
            summary = "" if src["gnews"] else clean_text(str(e.get("summary", "")))
            items.append(
                {
                    "title": title,
                    "url": normalize_url(link),
                    "outlet": outlet or src["name"],
                    "summary": summary,
                    "published_at": iso(published),
                    "region": src["region"],
                    "section": src["section"],
                    "feed_category": src["category"],
                    "lean": src["lean"],
                    "ai_ok": src["ai_ok"],
                    "feed": src["name"],
                }
            )
    except Exception as exc:  # noqa: BLE001 - 소스 하나 실패가 전체를 멈추면 안 된다
        error = str(exc)[:200]
    status = {
        "name": src["name"],
        "url": src["url"],
        "region": src["region"],
        "section": src["section"],
        "ok": error is None,
        "item_count": len(items),
        "duration_ms": int((time.perf_counter() - start) * 1000),
        "error": error,
    }
    return items, status


def fetch_all(sources: list[dict[str, str]], now: datetime) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """호스트별로는 순서대로, 호스트끼리는 병렬로 요청한다 (동시 요청 차단 회피)."""
    by_host: dict[str, list[dict[str, str]]] = defaultdict(list)
    for s in sources:
        by_host[urlparse(s["url"]).netloc.lower()].append(s)

    def run_host(group: list[dict[str, str]]) -> list[tuple[list[dict[str, Any]], dict[str, Any]]]:
        results = []
        for i, s in enumerate(group):
            if i:
                time.sleep(0.5)
            results.append(fetch_source(s, now))
        return results

    items: list[dict[str, Any]] = []
    statuses: list[dict[str, Any]] = []
    with ThreadPoolExecutor(max_workers=12) as ex:
        for results in ex.map(run_host, by_host.values()):
            for got, st in results:
                items.extend(got)
                statuses.append(st)
    return items, statuses


# ---------------------------------------------------------------- 아카이브


def load_archive(path: Path) -> dict[str, dict[str, Any]]:
    if not path.exists():
        return {}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        return {it["id"]: it for it in data.get("items", []) if it.get("id")}
    except Exception:
        return {}


def merge_into_archive(archive: dict[str, dict[str, Any]], items: list[dict[str, Any]], now: datetime) -> None:
    for it in items:
        # 같은 기사가 여러 섹션 피드에 동시에 나오는 경우가 많아 언론사+URL 로 ID 를 만든다.
        item_id = make_item_id(it["region"], it["outlet"], "", it["url"])
        rec = archive.get(item_id)
        if rec is None:
            rec = dict(it, id=item_id, first_seen_at=iso(now))
            archive[item_id] = rec
        else:
            # 더 구체적인 섹션 분류가 들어오면 갱신
            if rec.get("feed_category") == "general" and it["feed_category"] != "general":
                rec["feed_category"] = it["feed_category"]
            if not rec.get("summary") and it.get("summary"):
                rec["summary"] = it["summary"]
            if not rec.get("published_at") and it.get("published_at"):
                rec["published_at"] = it["published_at"]
            rec["title"] = it["title"]
        rec["last_seen_at"] = iso(now)


def event_time(rec: dict[str, Any]) -> datetime | None:
    return parse_iso(rec.get("published_at")) or parse_iso(rec.get("first_seen_at"))


def prune_archive(archive: dict[str, dict[str, Any]], now: datetime, days: int) -> dict[str, dict[str, Any]]:
    """과학은 days 일, 일반 뉴스는 3일만 보관 (뉴스 창은 36시간이라 그 이상은 필요 없다)."""
    keep_science = now - timedelta(days=days)
    keep_news = now - timedelta(days=3)
    out = {}
    for k, v in archive.items():
        t = event_time(v) or now
        if t >= (keep_science if v.get("section") == "science" else keep_news):
            out[k] = v
    return out


# ---------------------------------------------------------------- 분류 (키워드, 키 없이)

KEYWORDS: dict[str, list[str]] = {
    "politics": [
        "대통령", "국회", "여당", "야당", "민주당", "국민의힘", "의원", "장관", "총리", "청와대", "대통령실",
        "선거", "공천", "탄핵", "법사위", "국정", "정당", "與", "野", "후보자", "청문회", "개헌",
        "parliament", "election", "minister", "president", "senate", "congress", "lawmakers",
    ],
    "economy": [
        "경제", "금리", "환율", "코스피", "코스닥", "주가", "증시", "물가", "부동산", "아파트", "집값", "수출",
        "기업", "실적", "매출", "영업이익", "투자", "은행", "한은", "기준금리", "관세", "반도체", "삼성", "SK",
        "현대차", "상장", "채권", "GDP", "인플레",
        "economy", "inflation", "stocks", "shares", "market", "tariff", "bank", "earnings", "gdp", "rates",
    ],
    "society": [
        "경찰", "검찰", "법원", "재판", "판결", "구속", "기소", "사고", "화재", "사망", "부상", "수능", "교육",
        "학교", "의대", "의료", "병원", "날씨", "폭염", "한파", "지진", "노동", "파업", "복지", "저출생",
    ],
    "culture": [
        "영화", "드라마", "공연", "전시", "음악", "가수", "배우", "아이돌", "앨범", "문학", "책", "출간",
        "미술", "축제", "K팝", "케이팝", "방송", "예능", "film", "music", "album", "art", "theatre", "book",
    ],
    "world": [
        "미국", "중국", "일본", "러시아", "우크라", "이스라엘", "가자", "이란", "유럽", "EU", "나토", "유엔",
        "트럼프", "시진핑", "푸틴", "외교", "정상회담", "다카이치", "특파원",
    ],
    "northkorea": ["북한", "김정은", "평양", "조선중앙", "북측", "노동당"],
}
SPORTS_ENT_RE = re.compile(r"(프로야구|K리그|월드컵 예선|골프|홈런|감독 선임|열애|결혼 발표|\[포토\]|\[사진\]|화보)")


def classify(rec: dict[str, Any]) -> str:
    fc = rec.get("feed_category") or "general"
    if fc != "general":
        return fc
    text = f"{rec.get('title', '')} {rec.get('summary', '')}"
    low = text.lower()
    best, best_hits = "society", 0
    for cat, words in KEYWORDS.items():
        hits = sum(1 for w in words if (w.lower() in low if w.isascii() else w in text))
        if hits > best_hits:
            best, best_hits = cat, hits
    if rec.get("region") == "intl" and best in {"society", "politics"} and best_hits <= 1:
        return "world"
    return best


# 방송 프로그램 다시보기 항목 ("2026년 11월 16일 19시뉴스", "뉴스데스크 11/16")
PROGRAM_RE = re.compile(r"^(\d{4}년\s*\d{1,2}월\s*\d{1,2}일\s*\S*뉴스|뉴스데스크|뉴스투데이|정오뉴스)")


def is_low_value(rec: dict[str, Any]) -> bool:
    t = rec.get("title", "")
    return (
        bool(SPORTS_ENT_RE.search(t))
        or bool(PROGRAM_RE.search(t))
        or t.startswith(("[인사]", "[부고]", "[게시판]", "[오늘의 운세]", "[날씨]"))
    )


# ---------------------------------------------------------------- 사건 묶기

BRACKET_RE = re.compile(r"\[[^\]]{0,12}\]|\((?:종합|속보|1보|2보|3보|단독|영상|포토|사진|일문일답)[^)]*\)")
PUNCT_RE = re.compile(r"[^\w\s가-힣]")
HANGUL_RUN_RE = re.compile(r"[가-힣]+")
LATIN_RE = re.compile(r"[a-z0-9]+")
EN_STOP = {
    "the", "and", "for", "with", "from", "that", "this", "after", "over", "into", "says", "said",
    "will", "are", "was", "has", "have", "its", "his", "her", "their", "new", "about", "amid", "how",
    "what", "why", "who", "but", "not", "you", "they", "more", "than", "out", "can", "could", "would",
}
KO_STOP_BIGRAMS = {
    "종합", "속보", "단독", "기자", "뉴스", "오늘", "내일", "에서", "으로", "했다", "한다", "있다",
    # 실적·시황 기사의 정형 표현: 회사가 달라도 제목이 거의 같아 서로 엮이는 것을 막는다
    "분기", "영업", "업익", "이익", "억원", "조원", "전년", "증가", "감소", "흑자", "적자", "전환",
    "매출", "순이", "누적", "대비", "기록", "전분", "분기",
}


def title_tokens(title: str) -> set[str]:
    s = BRACKET_RE.sub(" ", title or "")
    s = PUNCT_RE.sub(" ", s).lower()
    toks: set[str] = set()
    for run in HANGUL_RUN_RE.findall(s):
        if len(run) == 1:
            continue
        for i in range(len(run) - 1):
            bg = run[i : i + 2]
            if bg not in KO_STOP_BIGRAMS:
                toks.add(bg)
    for w in LATIN_RE.findall(s):
        if len(w) >= 3 and w not in EN_STOP:
            # 아주 단순한 어간 처리: 복수형/3인칭 s 제거 (bans→ban, talks→talk)
            toks.add(w[:-1] if len(w) > 4 and w.endswith("s") and not w.endswith("ss") else w)
    return toks


SIM_THRESHOLD = 0.45
CLUSTER_SPAN_HOURS = 30


def weighted_similarity(a: set[str], b: set[str], idf: dict[str, float]) -> float:
    """IDF 가중 겹침 비율. '3분기', '영업이익' 같은 흔한 표현은 가중치가 낮다."""
    inter = a & b
    if len(inter) < 2:
        return 0.0
    wa = sum(idf.get(t, 1.0) for t in a)
    wb = sum(idf.get(t, 1.0) for t in b)
    wi = sum(idf.get(t, 1.0) for t in inter)
    if wi < 6.0:  # 희귀 토큰 2~3개 이상은 겹쳐야 같은 사건으로 본다
        return 0.0
    return wi / min(wa, wb) * 0.6 + wi / (wa + wb - wi) * 0.4


def cluster(records: list[dict[str, Any]]) -> list[list[dict[str, Any]]]:
    """제목 토큰 유사도로 같은 사건 기사를 묶는다.

    단일 연결(union-find)은 'A~B, B~C' 식으로 서로 다른 사건이 줄줄이 엮이므로,
    각 사건의 첫 기사(seed)와 직접 비교하는 탐욕적 묶기를 쓴다.
    """
    recs = sorted(records, key=lambda r: event_time(r) or datetime.min.replace(tzinfo=UTC))
    toks = [title_tokens(r["title"]) for r in recs]
    df: Counter[str] = Counter(t for ts in toks for t in ts)
    # 기사 수가 적을 때(테스트, 과학 탭) IDF 가 왜곡되지 않도록 문서 수에 하한을 둔다
    n = max(200, len(recs))
    idf = {t: math.log((n + 1) / (c + 0.5)) for t, c in df.items()}

    clusters: list[dict[str, Any]] = []  # {"seed": tokens, "region":..., "t": time, "members": [...]}
    index: dict[str, list[int]] = defaultdict(list)  # token -> cluster ids (seed 기준)
    span = timedelta(hours=CLUSTER_SPAN_HOURS)

    for rec, ts in zip(recs, toks):
        t = event_time(rec) or datetime.min.replace(tzinfo=UTC)
        cand: Counter[int] = Counter()
        for tok in ts:
            for cid in index.get(tok, ()):
                cand[cid] += 1
        best, best_sim = -1, 0.0
        for cid, shared in cand.items():
            if shared < 2:
                continue
            c = clusters[cid]
            if c["region"] != rec["region"] or t - c["t"] > span:
                continue
            sim = weighted_similarity(ts, c["seed"], idf)
            if sim > best_sim:
                best, best_sim = cid, sim
        if best >= 0 and best_sim >= SIM_THRESHOLD:
            clusters[best]["members"].append(rec)
            clusters[best]["t"] = max(clusters[best]["t"], t)
            continue
        cid = len(clusters)
        clusters.append({"seed": ts, "region": rec["region"], "t": t, "members": [rec]})
        for tok in ts:
            index[tok].append(cid)

    return [c["members"] for c in clusters]


# ---------------------------------------------------------------- 점수·선정


def pick_representative(members: list[dict[str, Any]]) -> dict[str, Any]:
    def key(r: dict[str, Any]) -> tuple:
        outlet = r.get("outlet", "")
        prio = OUTLET_PRIORITY.index(outlet) if outlet in OUTLET_PRIORITY else len(OUTLET_PRIORITY)
        return (prio, 0 if r.get("summary") else 1, -(event_time(r) or datetime.min.replace(tzinfo=UTC)).timestamp())

    return sorted(members, key=key)[0]


def build_story(members: list[dict[str, Any]], now: datetime) -> dict[str, Any]:
    rep = pick_representative(members)
    times = [t for t in (event_time(m) for m in members) if t]
    latest = max(times) if times else now
    earliest = min(times) if times else now
    outlets = sorted({m["outlet"] for m in members})
    cats = Counter(classify(m) for m in members)
    category = cats.most_common(1)[0][0]
    age_h = max(0.0, (now - latest).total_seconds() / 3600)
    recency = math.pow(0.5, age_h / HALF_LIFE_HOURS)
    # 보도 언론사 수가 핵심 신호. 로그로 눌러 초대형 이슈가 전부를 덮지 않게 한다.
    score = (1.0 + 2.2 * math.log2(len(outlets))) * (0.35 + 0.65 * recency)
    leans = sorted({m.get("lean") for m in members if m.get("lean")})
    return {
        "id": rep["id"],
        "title": rep["title"],
        "url": rep["url"],
        "summary": rep.get("summary", ""),
        "outlet": rep["outlet"],
        "region": rep["region"],
        "section": rep["section"],
        "category": category,
        "category_label": CATEGORIES.get(category, category),
        "published_at": iso(latest),
        "first_reported_at": iso(earliest),
        "outlet_count": len(outlets),
        "article_count": len(members),
        "leans": leans,
        "score": round(score, 4),
        "low_value": all(is_low_value(m) for m in members),
        "ai_ok": all(m.get("ai_ok", True) for m in members),
        # Claude 편집 단계가 채우는 필드 (없으면 화면은 원문 제목·요약을 보여준다)
        "title_ko": None,
        "summary_ko": None,
        "articles": [
            {"title": m["title"], "url": m["url"], "outlet": m["outlet"], "published_at": m.get("published_at")}
            for m in sorted(members, key=lambda r: r["outlet"])
        ],
    }


def select_top(stories: list[dict[str, Any]], total: int = TOP_TOTAL) -> list[dict[str, Any]]:
    """국내/해외 비중과 분야별 상한을 지키며 점수 순으로 고른다."""
    picked: list[dict[str, Any]] = []
    for region, share in REGION_SHARE.items():
        quota = round(total * share)
        cap = max(2, math.ceil(quota * CATEGORY_CAP))
        per_cat: Counter[str] = Counter()
        pool = [s for s in stories if s["region"] == region and s["section"] == "news" and not s["low_value"]]
        pool.sort(key=lambda s: s["score"], reverse=True)
        chosen: list[dict[str, Any]] = []
        for s in pool:
            if len(chosen) >= quota:
                break
            # 해외 피드는 대부분 '국제' 분류라 상한을 걸면 남는 자리를 가벼운 기사가 채운다
            exempt = region == "intl" and s["category"] == "world"
            if not exempt and per_cat[s["category"]] >= cap:
                continue
            chosen.append(s)
            per_cat[s["category"]] += 1
        # 상한 때문에 모자라면 남은 것 중 점수 순으로 채움
        if len(chosen) < quota:
            rest = [s for s in pool if s not in chosen]
            chosen.extend(rest[: quota - len(chosen)])
        picked.extend(chosen)
    picked.sort(key=lambda s: s["score"], reverse=True)
    return picked


def interleave_by_outlet(stories: list[dict[str, Any]]) -> list[dict[str, Any]]:
    queues: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for s in sorted(stories, key=lambda s: s["published_at"] or "", reverse=True):
        queues[s["outlet"]].append(s)
    # 학술지·전문 저널을 먼저, 그다음 업계 뉴스 매체
    order = sorted(
        queues,
        key=lambda o: (o not in JOURNAL_OUTLETS, -(parse_iso(queues[o][0]["published_at"]) or datetime.min.replace(tzinfo=UTC)).timestamp()),
    )
    out: list[dict[str, Any]] = []
    while any(queues.values()):
        for o in order:
            if queues[o]:
                out.append(queues[o].pop(0))
    return out


# ---------------------------------------------------------------- main


def main() -> int:
    ap = argparse.ArgumentParser(description="시몬스 뉴스 데이터 생성")
    ap.add_argument("--sources", default="feeds/sources.opml")
    ap.add_argument("--output-dir", default="data")
    ap.add_argument("--top", type=int, default=TOP_TOTAL)
    args = ap.parse_args()

    now = utc_now()
    out_dir = Path(args.output_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    archive_path = out_dir / "archive.json"

    sources = load_sources(Path(args.sources))
    fetched, statuses = fetch_all(sources, now)

    archive = load_archive(archive_path)
    merge_into_archive(archive, fetched, now)
    archive = prune_archive(archive, now, ARCHIVE_DAYS)

    news_after = now - timedelta(hours=NEWS_WINDOW_HOURS)
    sci_after = now - timedelta(days=SCIENCE_WINDOW_DAYS)
    journal_after = now - timedelta(days=JOURNAL_WINDOW_DAYS)
    news_recs, sci_recs = [], []
    for rec in archive.values():
        t = event_time(rec)
        if not t or t > now + timedelta(hours=1):
            continue
        if rec["section"] == "science":
            # 월간·주간 학술지는 7일 창에 걸려 사라지지 않도록 31일까지 본다
            limit = journal_after if rec.get("outlet") in JOURNAL_OUTLETS else sci_after
            if t >= limit:
                sci_recs.append(rec)
        elif t >= news_after:
            news_recs.append(rec)

    news_stories = [build_story(g, now) for g in cluster(news_recs)]
    news_stories.sort(key=lambda s: s["score"], reverse=True)
    # 과학은 묶기만 하고, 매체별로 번갈아 최신순 배치 (국내 전기 매체가 저널을 덮지 않도록)
    sci_stories = interleave_by_outlet([build_story(g, now) for g in cluster(sci_recs)])

    top = select_top(news_stories, args.top)
    top_ids = [s["id"] for s in top]

    payload = {
        "generated_at": iso(now),
        "categories": CATEGORIES,
        "region_share": REGION_SHARE,
        "top_ids": top_ids,
        "news": [s for s in news_stories if not s["low_value"]][:600],
        "science": sci_stories[:150],
        "counts": {
            "fetched": len(fetched),
            "archive": len(archive),
            "news_articles": len(news_recs),
            "news_stories": len(news_stories),
            "science_articles": len(sci_recs),
            "top_kr": sum(1 for s in top if s["region"] == "kr"),
            "top_intl": sum(1 for s in top if s["region"] == "intl"),
        },
    }
    status = {
        "generated_at": iso(now),
        "ok": sum(1 for s in statuses if s["ok"]),
        "failed": [s["name"] for s in statuses if not s["ok"]],
        "empty": [s["name"] for s in statuses if s["ok"] and s["item_count"] == 0],
        "sources": sorted(statuses, key=lambda s: s["name"]),
    }

    (out_dir / "news.json").write_text(
        json.dumps(sanitize_public_payload(payload), ensure_ascii=False, separators=(",", ":")), encoding="utf-8"
    )
    (out_dir / "status.json").write_text(json.dumps(status, ensure_ascii=False, indent=2), encoding="utf-8")
    archive_path.write_text(
        json.dumps({"generated_at": iso(now), "items": list(archive.values())}, ensure_ascii=False, separators=(",", ":")),
        encoding="utf-8",
    )

    c = payload["counts"]
    print(
        f"sources ok {status['ok']}/{len(statuses)} | fetched {c['fetched']} | "
        f"news {c['news_articles']} articles → {c['news_stories']} stories | "
        f"top {len(top)} (kr {c['top_kr']} / intl {c['top_intl']}) | science {len(sci_stories)}"
    )
    if status["failed"]:
        print("failed:", ", ".join(status["failed"]))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
