#!/usr/bin/env python3
"""시몬스 뉴스: RSS 수집 → 아카이브 → 사건 묶기 → 순위 → 국내 60 / 해외 40 선정.

API 키 없이 동작한다. 중요도는 "같은 사건을 보도한 언론사 수"와 최신성으로 매긴다.
Claude 편집 단계(scripts/ai_editor.py)는 이 출력 위에 요약·번역·정밀 분류를 덧붙인다.
"""

from __future__ import annotations

import argparse
import hashlib
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
from zoneinfo import ZoneInfo

import feedparser
import requests
from dateutil import parser as dtparser

sys.path.insert(0, str(Path(__file__).resolve().parent))
from outlets import PolicyTable, load_policy, recheck  # noqa: E402
from radar_lib import (  # noqa: E402
    BROWSER_UA,
    TZINFOS,
    UTC,
    iso,
    make_item_id,
    normalize_url,
    parse_iso,
    sanitize_public_payload,
    utc_now,
)

ARCHIVE_SCHEMA = 2
# 시간대 표기가 없는 값에 적용할 기준 시간대 (OPML 의 tz 속성이 우선)
DEFAULT_TZ = {"kr": "Asia/Seoul", "intl": "UTC"}
# 발행 시각이 수집 시각보다 이만큼 이상 미래면 잘못된 값으로 본다
FUTURE_TOLERANCE = timedelta(hours=1)

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
        region = a.get("region", "kr")
        gnews = a.get("gnews", "") == "1"
        out.append(
            {
                "name": a.get("text") or url,
                "url": url,
                "outlet": a.get("outlet", ""),
                "region": region,
                "section": a.get("section", "news"),
                "category": a.get("category", "general"),
                "lean": a.get("lean", ""),
                "gnews": gnews,
                "via": "google_news" if gnews else "direct",
                # 피드 단위 금지. 원언론사 정책과 별개로 항상 금지 쪽으로만 작용한다.
                "feed_ai_deny": a.get("ai", "").lower() == "no",
                "tz": a.get("tz") or DEFAULT_TZ.get(region, "UTC"),
            }
        )
    return out


# ---------------------------------------------------------------- 시간


def parse_time(value: Any, tz_name: str) -> datetime | None:
    """피드의 시각 문자열 → UTC. 시간대가 없으면 소스 기준 시간대(tz_name)로 해석한다."""
    s = str(value or "").strip()
    if not s:
        return None
    try:
        dt = dtparser.parse(s, tzinfos=TZINFOS)
    except (ValueError, OverflowError, TypeError):
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=ZoneInfo(tz_name))
    return dt.astimezone(UTC)


def entry_times(entry: Any, feed_version: str, tz_name: str, now: datetime) -> dict[str, Any]:
    """기사 하나의 발행/수정 시각과 그 근거.

    - RSS 2.0 <pubDate>, Atom <published> → published
    - Atom <updated> → updated (수정 시각)
    - RSS 1.0/RSS 2.0 의 <dc:date> 는 feedparser 가 'updated' 로 넘기지만, 실제로는 기사 날짜이므로
      발행 시각으로 쓰고 근거를 'dc:date' 로 남긴다.
    - 어느 것도 없거나 미래 시각이면 'unknown'. 수집 시각으로 대신하지 않는다.
    """
    is_atom = (feed_version or "").startswith("atom")
    # feedparser 의 entry.get("updated") 는 updated 가 없으면 published 값을 대신 돌려준다(호환용 매핑).
    # 그러면 모든 RSS 기사에 가짜 수정 시각이 붙으므로, 피드에 실제로 있는 필드만 dict.get 으로 읽는다.
    raw_pub = dict.get(entry, "published") or ""
    raw_upd = dict.get(entry, "updated") or ""
    published = parse_time(raw_pub, tz_name)
    updated = parse_time(raw_upd, tz_name)
    basis = "published" if published else None
    if not published and updated and not is_atom:
        published, updated, basis = updated, None, "dc:date"
    if published and published > now + FUTURE_TOLERANCE:
        return {"published_at": None, "updated_at": None, "time_basis": "invalid_future", "raw_time": raw_pub or raw_upd}
    if updated and updated > now + FUTURE_TOLERANCE:
        updated = None
    if not published and updated:
        basis = "updated_only"
    return {
        "published_at": iso(published),
        "updated_at": iso(updated),
        "time_basis": basis or "unknown",
        "raw_time": raw_pub or raw_upd or None,
    }


def article_time(rec: dict[str, Any]) -> datetime | None:
    """기사의 보도 시각. 발행 시각 → (발행 시각이 없을 때만) 수정 시각. 수집 시각은 쓰지 않는다."""
    return parse_iso(rec.get("published_at")) or parse_iso(rec.get("updated_at"))


def retention_time(rec: dict[str, Any]) -> datetime | None:
    """보관 기간 계산용 시각. 시각 미상 기사는 마지막으로 피드에서 본 시각을 쓴다."""
    return article_time(rec) or parse_iso(rec.get("last_seen_at")) or parse_iso(rec.get("first_seen_at"))


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


def content_hash(title: str, summary: str) -> str:
    return hashlib.sha1(f"{title}\n{summary}".encode("utf-8")).hexdigest()[:16]


def parse_feed(content: bytes, src: dict[str, Any], now: datetime, table: PolicyTable) -> list[dict[str, Any]]:
    """피드 본문 → 기사 목록. 원언론사·AI 정책·시간 근거를 기사마다 기록한다."""
    parsed = feedparser.parse(content)
    version = getattr(parsed, "version", "") or ""
    items: list[dict[str, Any]] = []
    for e in parsed.entries:
        title = clean_text(str(e.get("title", "")), 300)
        link = str(e.get("link", "")).strip()
        if not title or not link.startswith("http"):
            continue
        outlet_raw = src["outlet"]
        outlet_domain = urlparse(link).netloc
        if src["gnews"]:
            title, suffix = split_gnews_title(title)
            gsrc = e.get("source") if isinstance(e.get("source"), dict) else {}
            outlet_raw = gsrc.get("title") or suffix or src["outlet"] or ""
            # Google 뉴스 링크는 news.google.com 이므로 원언론사 도메인은 <source url> 에서 얻는다
            outlet_domain = gsrc.get("href") or ""
            found = table.canonical(outlet_raw, outlet_domain)
            # 언론사가 지정된 Google 뉴스 검색(site:...)은 그 언론사 기사만 남긴다
            if src["outlet"] and found and found != table.canonical(src["outlet"]):
                continue
        outlet = table.canonical(outlet_raw or src["name"], outlet_domain)
        policy = table.decide(outlet, src["via"], src["feed_ai_deny"])
        summary = "" if src["gnews"] else clean_text(str(e.get("summary", "")))
        times = entry_times(e, version, src["tz"], now)
        items.append(
            {
                "title": title,
                "url": normalize_url(link),
                "outlet": outlet,
                "outlet_raw": outlet_raw,
                "outlet_domain": urlparse(outlet_domain).netloc if "://" in outlet_domain else outlet_domain,
                "via": src["via"],
                "feed": src["name"],
                "feed_ai_deny": src["feed_ai_deny"],
                "ai": policy,
                "ai_ok": policy["allowed"],
                "summary": summary,
                **times,
                "content_hash": content_hash(title, summary),
                "region": src["region"],
                "section": src["section"],
                "feed_category": src["category"],
                "lean": src["lean"],
            }
        )
    return items


def fetch_source(
    src: dict[str, Any], now: datetime, table: PolicyTable
) -> tuple[list[dict[str, Any]], dict[str, Any]]:
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
        items = parse_feed(resp.content, src, now, table)
    except Exception as exc:  # noqa: BLE001 - 소스 하나 실패가 전체를 멈추면 안 된다
        error = str(exc)[:200]
    basis = Counter(it["time_basis"] for it in items)
    status = {
        "name": src["name"],
        "url": src["url"],
        "region": src["region"],
        "section": src["section"],
        "ok": error is None,
        "item_count": len(items),
        "undated_count": basis.get("unknown", 0) + basis.get("invalid_future", 0),
        "time_basis": dict(basis),
        "duration_ms": int((time.perf_counter() - start) * 1000),
        "error": error,
    }
    return items, status


def fetch_all(
    sources: list[dict[str, Any]], now: datetime, table: PolicyTable
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """호스트별로는 순서대로, 호스트끼리는 병렬로 요청한다 (동시 요청 차단 회피)."""
    by_host: dict[str, list[dict[str, str]]] = defaultdict(list)
    for s in sources:
        by_host[urlparse(s["url"]).netloc.lower()].append(s)

    def run_host(group: list[dict[str, str]]) -> list[tuple[list[dict[str, Any]], dict[str, Any]]]:
        results = []
        for i, s in enumerate(group):
            if i:
                time.sleep(0.5)
            results.append(fetch_source(s, now, table))
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


def article_id(region: str, outlet: str, url: str) -> str:
    # 같은 기사가 여러 섹션 피드에 동시에 나오므로 (지역, 원언론사, URL) 로 ID 를 만든다
    return make_item_id(region, outlet, "", url)


def migrate_record(old: dict[str, Any], table: PolicyTable) -> dict[str, Any]:
    """스키마 1(초기 MVP) 기록 → 스키마 2. 판단할 수 없는 값은 보수적으로 둔다."""
    rec = dict(old)
    url = rec.get("url", "")
    via = rec.get("via") or ("google_news" if "news.google.com" in url else "direct")
    rec["via"] = via
    rec["outlet_raw"] = rec.get("outlet_raw") or rec.get("outlet", "")
    rec["outlet"] = table.canonical(rec.get("outlet", ""), "" if via == "google_news" else url)
    # 스키마 1 은 피드의 ai 설정을 ai_ok 로만 남겼다. false 였다면 피드 금지로 보존한다.
    rec["feed_ai_deny"] = bool(rec.get("feed_ai_deny", rec.get("ai_ok") is False))
    if "time_basis" not in rec:
        # 스키마 1 의 published_at 은 updated/dc:date 에서 왔을 수도 있다. 근거를 'legacy' 로 표시.
        rec["time_basis"] = "legacy" if rec.get("published_at") else "unknown"
        rec.setdefault("updated_at", None)
    rec.setdefault("content_hash", content_hash(rec.get("title", ""), rec.get("summary", "")))
    rec.setdefault("revision", 0)
    rec.setdefault("content_changed_at", None)
    rec["ai"] = table.decide(rec["outlet"], via, rec["feed_ai_deny"])
    rec["ai_ok"] = rec["ai"]["allowed"]
    rec["id"] = article_id(rec.get("region", "kr"), rec["outlet"], url)
    return rec


def load_archive(path: Path, table: PolicyTable) -> dict[str, dict[str, Any]]:
    if not path.exists():
        return {}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return {}
    schema = data.get("schema", 1)
    out: dict[str, dict[str, Any]] = {}
    for it in data.get("items", []):
        if not it.get("url"):
            continue
        rec = migrate_record(it, table) if schema < ARCHIVE_SCHEMA else it
        prev = out.get(rec["id"])
        # ID 규칙이 바뀌어 합쳐지는 경우 먼저 본 기록의 최초 수집 시각을 남긴다
        if prev and (prev.get("first_seen_at") or "") <= (rec.get("first_seen_at") or "~"):
            continue
        out[rec["id"]] = rec
    return out


def merge_into_archive(
    archive: dict[str, dict[str, Any]], items: list[dict[str, Any]], now: datetime
) -> dict[str, int]:
    """수집 결과를 아카이브에 반영한다. 반환값은 신규/갱신 건수."""
    stats = Counter()
    seen_this_run: set[str] = set()
    for it in items:
        item_id = article_id(it["region"], it["outlet"], it["url"])
        rec = archive.get(item_id)
        if rec is None:
            rec = dict(it, id=item_id, first_seen_at=iso(now), revision=0, content_changed_at=None)
            archive[item_id] = rec
            stats["new"] += 1
        elif item_id in seen_this_run:
            # 같은 실행에서 다른 섹션 피드로 또 들어온 사본: 빈 값과 분류만 보강
            if rec.get("feed_category") == "general" and it["feed_category"] != "general":
                rec["feed_category"] = it["feed_category"]
            rec["feed_ai_deny"] = rec.get("feed_ai_deny", False) or it["feed_ai_deny"]
            if not rec.get("summary") and it.get("summary"):
                rec["summary"] = it["summary"]
                rec["content_hash"] = content_hash(rec["title"], rec["summary"])
            continue
        else:
            # 같은 URL 의 기사가 다시 들어옴: 제목·요약·시각이 바뀌었으면 새 값으로 갱신
            new_summary = it.get("summary") or rec.get("summary", "")  # Google 뉴스처럼 요약이 없으면 기존 값 유지
            new_hash = content_hash(it["title"], new_summary)
            if new_hash != rec.get("content_hash"):
                if it["title"] != rec.get("title"):
                    rec["previous_title"] = rec.get("title")
                rec["title"] = it["title"]
                rec["summary"] = new_summary
                rec["content_hash"] = new_hash
                rec["content_changed_at"] = iso(now)
                rec["revision"] = int(rec.get("revision") or 0) + 1
                stats["changed"] += 1
            if it.get("time_basis") not in (None, "unknown", "invalid_future"):
                if it.get("published_at") and it["published_at"] != rec.get("published_at"):
                    rec["published_at"] = it["published_at"]
                rec["time_basis"] = it["time_basis"]
            if it.get("updated_at") and it["updated_at"] != rec.get("updated_at"):
                rec["updated_at"] = it["updated_at"]
                stats["time_updated"] += 1
            if rec.get("feed_category") == "general" and it["feed_category"] != "general":
                rec["feed_category"] = it["feed_category"]
            for k in ("outlet_raw", "outlet_domain", "via", "feed", "lean", "raw_time"):
                if it.get(k) is not None:
                    rec[k] = it[k]
            # 피드 금지는 한 번 걸리면 그 기사에서 풀리지 않는다 (최종 판정은 refresh_policy)
            rec["feed_ai_deny"] = bool(rec.get("feed_ai_deny")) or it["feed_ai_deny"]
            stats["seen"] += 1
        rec["last_seen_at"] = iso(now)
        seen_this_run.add(item_id)
    return dict(stats)


def refresh_policy(archive: dict[str, dict[str, Any]], table: PolicyTable) -> None:
    """정책 표가 바뀌었을 수 있으므로 매 실행마다 모든 기록을 재판정한다."""
    for rec in archive.values():
        rec["ai"] = recheck(rec, table)
        rec["ai_ok"] = rec["ai"]["allowed"]


def prune_archive(archive: dict[str, dict[str, Any]], now: datetime, days: int) -> dict[str, dict[str, Any]]:
    """과학은 days 일, 일반 뉴스는 3일만 보관 (뉴스 창은 36시간이라 그 이상은 필요 없다)."""
    keep_science = now - timedelta(days=days)
    keep_news = now - timedelta(days=3)
    out = {}
    for k, v in archive.items():
        t = retention_time(v) or now
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
    return cluster_with_undated(records, [])[0]


def cluster_with_undated(
    dated: list[dict[str, Any]], undated: list[dict[str, Any]]
) -> tuple[list[list[dict[str, Any]]], list[dict[str, Any]]]:
    """제목 토큰 유사도로 같은 사건 기사를 묶는다.

    단일 연결(union-find)은 'A~B, B~C' 식으로 서로 다른 사건이 줄줄이 엮이므로,
    각 사건의 첫 기사(seed)와 직접 비교하는 탐욕적 묶기를 쓴다.

    시각 미상 기사(undated)는 사건을 새로 만들거나 대표가 될 수 없고, 이미 있는 사건에
    '함께 보도한 언론사'로만 붙는다. 어디에도 붙지 않은 것은 따로 돌려준다.
    """
    recs = sorted(dated, key=lambda r: article_time(r) or datetime.min.replace(tzinfo=UTC))
    toks = [title_tokens(r["title"]) for r in recs]
    u_toks = [title_tokens(r["title"]) for r in undated]
    df: Counter[str] = Counter(t for ts in toks + u_toks for t in ts)
    # 기사 수가 적을 때(테스트, 과학 탭) IDF 가 왜곡되지 않도록 문서 수에 하한을 둔다
    n = max(200, len(recs) + len(undated))
    idf = {t: math.log((n + 1) / (c + 0.5)) for t, c in df.items()}

    clusters: list[dict[str, Any]] = []  # {"seed": tokens, "region":..., "t": time, "members": [...]}
    index: dict[str, list[int]] = defaultdict(list)  # token -> cluster ids (seed 기준)
    span = timedelta(hours=CLUSTER_SPAN_HOURS)

    def best_match(rec: dict[str, Any], ts: set[str], t: datetime | None) -> int:
        cand: Counter[int] = Counter()
        for tok in ts:
            for cid in index.get(tok, ()):
                cand[cid] += 1
        best, best_sim = -1, 0.0
        for cid, shared in cand.items():
            if shared < 2:
                continue
            c = clusters[cid]
            if c["region"] != rec["region"] or (t is not None and t - c["t"] > span):
                continue
            sim = weighted_similarity(ts, c["seed"], idf)
            if sim > best_sim:
                best, best_sim = cid, sim
        return best if best_sim >= SIM_THRESHOLD else -1

    for rec, ts in zip(recs, toks):
        t = article_time(rec) or datetime.min.replace(tzinfo=UTC)
        best = best_match(rec, ts, t)
        if best >= 0:
            clusters[best]["members"].append(rec)
            clusters[best]["t"] = max(clusters[best]["t"], t)
            continue
        cid = len(clusters)
        clusters.append({"seed": ts, "region": rec["region"], "t": t, "members": [rec]})
        for tok in ts:
            index[tok].append(cid)

    leftovers: list[dict[str, Any]] = []
    for rec, ts in zip(undated, u_toks):
        best = best_match(rec, ts, None)
        if best >= 0:
            clusters[best]["members"].append(rec)
        else:
            leftovers.append(rec)

    return [c["members"] for c in clusters], leftovers


# ---------------------------------------------------------------- 점수·선정


def pick_representative(members: list[dict[str, Any]]) -> dict[str, Any]:
    """대표 기사: 시각이 확인된 기사 중에서 통신사·방송 우선, 요약 있는 것, 최신 순."""
    dated = [m for m in members if article_time(m)]
    pool = dated or members

    def key(r: dict[str, Any]) -> tuple:
        outlet = r.get("outlet", "")
        prio = OUTLET_PRIORITY.index(outlet) if outlet in OUTLET_PRIORITY else len(OUTLET_PRIORITY)
        return (prio, 0 if r.get("summary") else 1, -(article_time(r) or datetime.min.replace(tzinfo=UTC)).timestamp())

    return sorted(pool, key=key)[0]


def public_article(m: dict[str, Any]) -> dict[str, Any]:
    return {
        "id": m["id"],
        "title": m["title"],
        "url": m["url"],
        "outlet": m["outlet"],
        "via": m.get("via"),
        "published_at": m.get("published_at"),
        "updated_at": m.get("updated_at"),
        "time_basis": m.get("time_basis"),
        "first_seen_at": m.get("first_seen_at"),
        "revision": m.get("revision", 0),
        "ai_ok": bool(m.get("ai_ok")),
    }


def build_story(members: list[dict[str, Any]], now: datetime) -> dict[str, Any]:
    rep = pick_representative(members)
    times = [t for t in (article_time(m) for m in members) if t]
    # 사건의 최신 보도 시각: 시각이 확인된 기사만. 수집 시각으로 대신하지 않는다.
    latest = max(times) if times else None
    earliest = min(times) if times else None
    outlets = sorted({m["outlet"] for m in members})
    cats = Counter(classify(m) for m in members)
    category = cats.most_common(1)[0][0]
    if latest is not None:
        age_h = max(0.0, (now - latest).total_seconds() / 3600)
        recency = math.pow(0.5, age_h / HALF_LIFE_HOURS)
    else:
        recency = 0.0  # 시각 미상 사건은 최신성 가산 없음
    # 보도 언론사 수가 핵심 신호. 로그로 눌러 초대형 이슈가 전부를 덮지 않게 한다.
    score = (1.0 + 2.2 * math.log2(len(outlets))) * (0.35 + 0.65 * recency)
    leans = sorted({m.get("lean") for m in members if m.get("lean")})
    rep_time = article_time(rep)
    return {
        "id": rep["id"],
        # 카드에 보이는 제목·링크·언론사·시각은 모두 대표 기사 하나에서 온다
        "title": rep["title"],
        "url": rep["url"],
        "summary": rep.get("summary", ""),
        "outlet": rep["outlet"],
        "published_at": iso(rep_time),
        "time_basis": rep.get("time_basis"),
        "rep_article_id": rep["id"],
        "region": rep["region"],
        "section": rep["section"],
        "category": category,
        "category_label": CATEGORIES.get(category, category),
        "latest_reported_at": iso(latest),
        "first_reported_at": iso(earliest),
        "outlet_count": len(outlets),
        "article_count": len(members),
        "undated_article_count": sum(1 for m in members if not article_time(m)),
        "leans": leans,
        "score": round(score, 4),
        "low_value": all(is_low_value(m) for m in members),
        # 사건 단위 AI 허용: 구성 기사가 전부 허용일 때만. 실제 요청 직전에 outlets.ai_gate 로 재검사한다.
        "ai_ok": all(bool(m.get("ai_ok")) for m in members),
        # Claude 편집 단계가 채우는 필드 (없으면 화면은 원문 제목·요약을 보여준다)
        "title_ko": None,
        "summary_ko": None,
        "articles": [public_article(m) for m in sorted(members, key=lambda r: r["outlet"])],
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
    for s in sorted(stories, key=lambda s: s["latest_reported_at"] or "", reverse=True):
        queues[s["outlet"]].append(s)
    # 학술지·전문 저널을 먼저, 그다음 업계 뉴스 매체
    order = sorted(
        queues,
        key=lambda o: (
            o not in JOURNAL_OUTLETS,
            -(parse_iso(queues[o][0]["latest_reported_at"]) or datetime.min.replace(tzinfo=UTC)).timestamp(),
        ),
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
    ap.add_argument("--policy", default="", help="언론사 정책 표 (기본: feeds/outlets.json)")
    args = ap.parse_args()

    now = utc_now()
    out_dir = Path(args.output_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    archive_path = out_dir / "archive.json"

    table = load_policy(Path(args.policy) if args.policy else None)
    sources = load_sources(Path(args.sources))
    fetched, statuses = fetch_all(sources, now, table)

    archive = load_archive(archive_path, table)
    merge_stats = merge_into_archive(archive, fetched, now)
    refresh_policy(archive, table)
    archive = prune_archive(archive, now, ARCHIVE_DAYS)

    news_after = now - timedelta(hours=NEWS_WINDOW_HOURS)
    sci_after = now - timedelta(days=SCIENCE_WINDOW_DAYS)
    journal_after = now - timedelta(days=JOURNAL_WINDOW_DAYS)
    now_iso = iso(now)
    news_recs, sci_recs = [], []
    news_undated, sci_undated = [], []
    for rec in archive.values():
        t = article_time(rec)
        if t is not None and t > now + FUTURE_TOLERANCE:
            t = None  # 이전 형식 기록 등에 남은 미래 시각은 미상으로 취급
        is_sci = rec["section"] == "science"
        if t is None:
            # 시각 미상: 이번 실행에 피드에 실제로 있었던 것만 '함께 보도' 후보로 쓴다
            if rec.get("last_seen_at") == now_iso:
                (sci_undated if is_sci else news_undated).append(rec)
            continue
        if is_sci:
            # 월간·주간 학술지는 7일 창에 걸려 사라지지 않도록 31일까지 본다
            limit = journal_after if rec.get("outlet") in JOURNAL_OUTLETS else sci_after
            if t >= limit:
                sci_recs.append(rec)
        elif t >= news_after:
            news_recs.append(rec)

    news_groups, news_left = cluster_with_undated(news_recs, news_undated)
    sci_groups, sci_left = cluster_with_undated(sci_recs, sci_undated)
    news_stories = [build_story(g, now) for g in news_groups]
    news_stories.sort(key=lambda s: s["score"], reverse=True)
    # 과학은 묶기만 하고, 매체별로 번갈아 최신순 배치 (국내 전기 매체가 저널을 덮지 않도록)
    sci_stories = interleave_by_outlet([build_story(g, now) for g in sci_groups])
    undated_left = news_left + sci_left

    top = select_top(news_stories, args.top)
    top_ids = [s["id"] for s in top]

    payload = {
        "schema": ARCHIVE_SCHEMA,
        "generated_at": iso(now),
        "categories": CATEGORIES,
        "region_share": REGION_SHARE,
        "top_ids": top_ids,
        "news": [s for s in news_stories if not s["low_value"]][:600],
        "science": sci_stories[:150],
        # 시각 미상이고 어느 사건에도 붙지 않은 기사: 순위·최신 목록에 넣지 않고 따로 둔다
        "undated": [public_article(m) for m in sorted(undated_left, key=lambda r: r["outlet"])][:200],
        "counts": {
            "fetched": len(fetched),
            "archive": len(archive),
            "news_articles": len(news_recs),
            "news_stories": len(news_stories),
            "science_articles": len(sci_recs),
            "top_kr": sum(1 for s in top if s["region"] == "kr"),
            "top_intl": sum(1 for s in top if s["region"] == "intl"),
            "undated_seen": len(news_undated) + len(sci_undated),
            "undated_attached": len(news_undated) + len(sci_undated) - len(undated_left),
            "archive_new": merge_stats.get("new", 0),
            "archive_changed": merge_stats.get("changed", 0),
            "ai_allowed_articles": sum(1 for r in archive.values() if r.get("ai_ok")),
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
        json.dumps(
            {"schema": ARCHIVE_SCHEMA, "generated_at": iso(now), "items": list(archive.values())},
            ensure_ascii=False,
            separators=(",", ":"),
        ),
        encoding="utf-8",
    )

    c = payload["counts"]
    print(
        f"sources ok {status['ok']}/{len(statuses)} | fetched {c['fetched']} | "
        f"news {c['news_articles']} articles → {c['news_stories']} stories | "
        f"top {len(top)} (kr {c['top_kr']} / intl {c['top_intl']}) | science {len(sci_stories)} | "
        f"undated {c['undated_seen']} (attached {c['undated_attached']}) | "
        f"archive +{c['archive_new']} changed {c['archive_changed']} | ai-allowed {c['ai_allowed_articles']}"
    )
    if status["failed"]:
        print("failed:", ", ".join(status["failed"]))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
