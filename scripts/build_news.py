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
import os
import re
import sys
import time
import unicodedata
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
from source_health import (  # noqa: E402
    DEFAULT_CADENCE_HOURS,
    GOOD_STATES,
    STATE_LABELS,
    update_history,
)
from source_health import classify as classify_source  # noqa: E402
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


def load_sources(opml_path: Path, include_disabled: bool = False) -> list[dict[str, Any]]:
    root = ET.parse(opml_path).getroot()
    out: list[dict[str, Any]] = []
    seen: set[str] = set()
    for node in root.iter("outline"):
        url = (node.attrib.get("xmlUrl") or "").strip()
        if not url or url in seen:
            continue
        seen.add(url)
        a = node.attrib
        enabled = a.get("enabled", "true").lower() != "false"
        if not enabled and not include_disabled:
            continue
        region = a.get("region", "kr")
        section = a.get("section", "news")
        gnews = a.get("gnews", "") == "1"
        out.append(
            {
                "name": a.get("text") or url,
                "url": url,
                "outlet": a.get("outlet", ""),
                "region": region,
                "section": section,
                "category": a.get("category", "general"),
                "lean": a.get("lean", ""),
                "gnews": gnews,
                "via": "google_news" if gnews else "direct",
                # 피드 단위 금지. 원언론사 정책과 별개로 항상 금지 쪽으로만 작용한다.
                "feed_ai_deny": a.get("ai", "").lower() == "no",
                "tz": a.get("tz") or DEFAULT_TZ.get(region, "UTC"),
                # 발행 주기: 오래된 피드 판정 기준 (config/quality.json 의 cadence_hours)
                "cadence": a.get("cadence") or ("weekly" if section == "science" else "daily"),
                "enabled": enabled,
                "disabled_reason": a.get("disabled_reason", ""),
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
    return parse_feed_detail(content, src, now, table)[0]


def parse_feed_detail(
    content: bytes, src: dict[str, Any], now: datetime, table: PolicyTable
) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    """피드 본문 → (기사 목록, 파싱 정보). 원언론사·AI 정책·시간 근거를 기사마다 기록한다."""
    parsed = feedparser.parse(content)
    version = getattr(parsed, "version", "") or ""
    # RSS/Atom 으로 인식되지 않았고 항목도 없으면 파싱 실패 (HTML 오류 페이지 등)
    meta = {"parse_ok": bool(version) or bool(parsed.entries), "feed_version": version, "entry_count": len(parsed.entries)}
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
    return items, meta


def fetch_source(
    src: dict[str, Any], now: datetime, table: PolicyTable, cadence_hours: dict[str, float] | None = None
) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    """소스 하나를 가져와 단계별 결과(HTTP → 파싱 → 유효 → 시각 → 최근)를 기록한다."""
    start = time.perf_counter()
    items: list[dict[str, Any]] = []
    error = None
    http_ok = parse_ok = False
    http_status = None
    meta: dict[str, Any] = {"entry_count": 0, "feed_version": ""}
    try:
        resp = None
        for attempt in range(3):  # 일시적 429/5xx/연결 끊김(MBC 등) 대비
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
        http_status = resp.status_code
        resp.raise_for_status()
        http_ok = True
        items, meta = parse_feed_detail(resp.content, src, now, table)
        parse_ok = meta["parse_ok"]
        if not parse_ok:
            error = f"RSS/Atom 아님 (content-type: {resp.headers.get('content-type', '?')})"
    except Exception as exc:  # noqa: BLE001 - 소스 하나 실패가 전체를 멈추면 안 된다
        error = str(exc)[:200]
    basis = Counter(it["time_basis"] for it in items)
    times = [t for t in (article_time(it) for it in items) if t]
    newest = max(times) if times else None
    ch = cadence_hours or DEFAULT_CADENCE_HOURS
    recent_after = now - timedelta(hours=ch.get(src["cadence"], ch["daily"]))
    status = {
        "name": src["name"],
        "url": src["url"],
        "region": src["region"],
        "section": src["section"],
        "cadence": src["cadence"],
        "http_ok": http_ok,
        "http_status": http_status,
        "parse_ok": parse_ok,
        "feed_version": meta.get("feed_version", ""),
        "entry_count": meta.get("entry_count", 0),
        "valid_count": len(items),
        "dated_count": len(times),
        "recent_count": sum(1 for t in times if t >= recent_after),
        "newest_at": iso(newest),
        "newest_at_dt": newest,
        "undated_count": basis.get("unknown", 0) + basis.get("invalid_future", 0),
        "time_basis": dict(basis),
        "duration_ms": int((time.perf_counter() - start) * 1000),
        "error": error,
    }
    status["state"] = classify_source(status, now, ch)
    status["state_label"] = STATE_LABELS[status["state"]]
    # 호환: 이전 화면·도구가 쓰던 필드. ok 는 '정상 수집(최근 기사 확보 또는 시각 없음)'만 참.
    status["ok"] = status["state"] in GOOD_STATES
    status["item_count"] = len(items)
    return items, status


def fetch_all(
    sources: list[dict[str, Any]], now: datetime, table: PolicyTable, cadence_hours: dict[str, float] | None = None
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
            results.append(fetch_source(s, now, table, cadence_hours))
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
# 같은 사건 묶음의 최초 기사 ~ 최종 기사 시간 폭 상한.
# 이보다 늦은 후속 보도는 새 사건으로 두고 관련 이슈로 연결한다.
CLUSTER_MAX_SPAN_HOURS = 24


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


# ---------------------------------------------------------------- 오병합 방지 (의미 충돌 검사)
#
# 토큰이 많이 겹쳐도 아래 중 하나면 다른 사건일 가능성이 높으므로 합치지 않는다.
#   - 문두 주어(쉼표 앞)가 서로 다른 기업·인물·기관   "DB증권, 3분기…" vs "오리온, 3분기…"
#   - 같은 단위의 금액·비율이 서로 다름                "코스피 3.6% 급락" vs "코스피 1.2% 급락"
#   - 날짜(N일)가 서로 다름                            "19일 선고" vs "17일 회담"
#   - 반대 방향 표현                                   "인상" vs "인하", "가결" vs "부결"
# 같은 사건인데 갈라지는 경우(중복)가 생길 수 있지만, 애매하면 합치지 않는 쪽을 택한다.

# 문두 주어: 쉼표 앞의 짧은 명사구만 (말줄임표 앞 구절은 주어가 아니라 문장 일부인 경우가 많다)
LEAD_SUBJECT_RE = re.compile(r"^(?:\s*\[[^\]]{0,12}\])*\s*([^,\[\]\"“”‘’'\s][^,…\"“”]{0,9}?)\s*,")
# 같은 사건을 기관 이름만 바꿔 부르는 일이 잦은 일반 주어: 서로 달라도 충돌로 보지 않는다
GENERIC_SUBJECTS = {
    "정부", "당국", "경찰", "검찰", "법원", "대법", "대법원", "국회", "여야", "與", "野", "靑", "청와대",
    "대통령실", "교육부", "외교부", "국방부", "통일부", "기재부", "합참", "軍", "군", "속보", "종합", "단독",
    # 다른 주체를 조사·제재하는 기관: "공정위, 굽네치킨 조사" vs "굽네치킨 창업주, 의혹" 은 같은 사건
    "공정위", "금감원", "금융위", "국세청", "감사원", "선관위", "헌재", "특검", "공수처", "경찰청", "검찰청",
}
# 같은 인물·직위의 다른 표기 ("李대통령", "이 대통령", "李") → 직위로 맞춘다
TITLE_WORDS = "대통령|총리|장관|의장|대표|위원장|원내대표|비대위원장|대변인|지사|시장"
SUBJECT_ALIAS_RE = re.compile(rf"^(?:[\u4e00-\u9fff]|[가-힣]{{1,3}}\s*)?({TITLE_WORDS})$")
SUBJECT_ALIASES = {"李": "대통령", "이재명": "대통령", "尹": "윤석열"}
DAY_RE = re.compile(r"(?<!\d)(\d{1,2})일(?!간|째|부터)")
OPPOSITES = [
    ("인상", "인하"), ("상승", "하락"), ("급등", "급락"), ("상승", "급락"), ("가결", "부결"),
    ("찬성", "반대"), ("승인", "불허"), ("체포", "석방"), ("구속", "석방"), ("흑자", "적자"),
    ("유죄", "무죄"), ("rise", "fall"), ("rises", "falls"), ("up", "down"),
]


def lead_subject(title: str) -> str:
    # 호환용 한자(예: U+F9E1 '李')가 섞여 들어오므로 먼저 정규화한다
    m = LEAD_SUBJECT_RE.match(unicodedata.normalize("NFKC", title or ""))
    if not m:
        return ""
    subj = m.group(1).strip()
    if subj.count(" ") > 1:  # 띄어쓰기가 많으면 주어가 아니라 구절
        return ""
    if subj in SUBJECT_ALIASES:
        return SUBJECT_ALIASES[subj]
    alias = SUBJECT_ALIAS_RE.match(subj)
    return alias.group(1) if alias else subj


def _subject_tokens(s: str) -> set[str]:
    return title_tokens(s) | ({s} if len(s) <= 2 else set())


def conflict_reason(a: str, b: str) -> str | None:
    """두 제목이 다른 사건일 가능성이 높은 의미 충돌이면 그 이유, 아니면 None."""
    sa, sb = lead_subject(a), lead_subject(b)
    if sa and sb and sa not in GENERIC_SUBJECTS and sb not in GENERIC_SUBJECTS:
        ta, tb = _subject_tokens(sa), _subject_tokens(sb)
        a_tokens, b_tokens = title_tokens(a), title_tokens(b)
        # 상대 제목 어디에도 내 주어가 없을 때만 충돌 (주어 자리만 바뀐 같은 사건은 통과)
        if ta and tb and not (ta & tb) and not (ta & b_tokens) and not (tb & a_tokens) \
                and sa not in b and sb not in a:
            return f"subject:{sa}|{sb}"
    da, db = set(DAY_RE.findall(a)), set(DAY_RE.findall(b))
    if da and db and not (da & db):
        return "date"
    la, lb = a.lower(), b.lower()
    for x, y in OPPOSITES:
        if len(x) <= 3:  # 짧은 영어 단어는 단어 경계로
            ax, ay = re.search(rf"\b{x}\b", la), re.search(rf"\b{y}\b", la)
            bx, by = re.search(rf"\b{x}\b", lb), re.search(rf"\b{y}\b", lb)
        else:
            ax, ay, bx, by = x in la, y in la, x in lb, y in lb
        if (ax and by and not ay and not bx) or (ay and bx and not ax and not by):
            return f"opposite:{x}|{y}"
    return None


def cluster(records: list[dict[str, Any]]) -> list[list[dict[str, Any]]]:
    return cluster_with_undated(records, [])[0]


def cluster_with_undated(
    dated: list[dict[str, Any]], undated: list[dict[str, Any]], stats: Counter | None = None
) -> tuple[list[list[dict[str, Any]]], list[dict[str, Any]]]:
    groups, leftovers, _ = cluster_detail(dated, undated, stats)
    return [g["members"] for g in groups], leftovers


def cluster_detail(
    dated: list[dict[str, Any]],
    undated: list[dict[str, Any]],
    stats: Counter | None = None,
    examples: list | None = None,
) -> tuple[list[dict[str, Any]], list[dict[str, Any]], dict[str, float]]:
    """제목 토큰 유사도로 같은 사건 기사를 묶는다.

    - 각 사건의 첫 기사(seed)와 직접 비교하는 탐욕적 묶기 (A~B~C 연쇄 병합 방지).
    - 묶음의 시간 폭은 seed(최초 기사) 기준으로 검사한다. 최신 기사가 들어와도 기준이 움직이지 않는다.
    - 의미 충돌(conflict_reason)이 있으면 합치지 않는다.
    - 시간 폭을 넘었지만 내용이 같은 후속 보도는 새 사건으로 만들고 follow_of 로 기록 → 관련 이슈 연결.
    - 시각 미상 기사는 사건을 새로 만들 수 없고 기존 사건에만 붙는다.
    """
    stats = stats if stats is not None else Counter()
    recs = sorted(dated, key=lambda r: article_time(r) or datetime.min.replace(tzinfo=UTC))
    toks = [title_tokens(r["title"]) for r in recs]
    u_toks = [title_tokens(r["title"]) for r in undated]
    df: Counter[str] = Counter(t for ts in toks + u_toks for t in ts)
    # 기사 수가 적을 때(테스트, 과학 탭) IDF 가 왜곡되지 않도록 문서 수에 하한을 둔다
    n = max(200, len(recs) + len(undated))
    idf = {t: math.log((n + 1) / (c + 0.5)) for t, c in df.items()}

    groups: list[dict[str, Any]] = []
    index: dict[str, list[int]] = defaultdict(list)  # token -> group ids (seed 기준)
    span = timedelta(hours=CLUSTER_MAX_SPAN_HOURS)

    def candidates(rec: dict[str, Any], ts: set[str]) -> list[tuple[float, int]]:
        cand: Counter[int] = Counter()
        for tok in ts:
            for gid in index.get(tok, ()):
                cand[gid] += 1
        out = []
        for gid, shared in cand.items():
            if shared < 2 or groups[gid]["region"] != rec["region"]:
                continue
            sim = weighted_similarity(ts, groups[gid]["seed"], idf)
            if sim >= SIM_THRESHOLD:
                out.append((sim, gid))
        return sorted(out, reverse=True)

    def blocked(rec: dict[str, Any], g: dict[str, Any]) -> str | None:
        for other in (g["members"][0], g["members"][-1]):  # seed 와 가장 최근 기사
            why = conflict_reason(rec["title"], other["title"])
            if why:
                return why
        return None

    for rec, ts in zip(recs, toks):
        t = article_time(rec) or datetime.min.replace(tzinfo=UTC)
        joined, follow_of = -1, None
        for sim, gid in candidates(rec, ts):
            g = groups[gid]
            if t - g["t_first"] > span:
                follow_of = gid if follow_of is None else follow_of
                stats["span_exceeded"] += 1
                continue
            why = blocked(rec, g)
            if why:
                stats["conflict:" + why.split(":")[0]] += 1
                if examples is not None:
                    examples.append((why, rec["title"], g["members"][0]["title"]))
                continue
            joined = gid
            break
        if joined >= 0:
            g = groups[joined]
            g["members"].append(rec)
            g["t_last"] = max(g["t_last"], t)
            continue
        gid = len(groups)
        groups.append({"seed": ts, "region": rec["region"], "t_first": t, "t_last": t, "members": [rec],
                       "follow_of": follow_of})
        for tok in ts:
            index[tok].append(gid)

    leftovers: list[dict[str, Any]] = []
    for rec, ts in zip(undated, u_toks):
        target = next((gid for _, gid in candidates(rec, ts) if not blocked(rec, groups[gid])), -1)
        if target >= 0:
            groups[target]["members"].append(rec)
        else:
            leftovers.append(rec)

    return groups, leftovers, idf


# ---------------------------------------------------------------- 관련 이슈
#
# 같은 사건(같은 일을 보도한 기사 묶음)보다 한 단계 넓은 '관련 이슈'.
# 예: "수능 와이파이 차단"과 "범정부 대응팀 구성"은 제목 낱말이 거의 겹치지 않지만,
#     "AI 부정행위 우려에 수능 와이파이 차단" → "수능 부정행위 대비" 같은 기사를 거쳐 이어진다.
# 그래서 사건마다 구성 기사 제목 전체의 낱말을 모아 두 사건이 공유하는 희귀 낱말의 IDF 합이
# ISSUE_MIN_SHARED_WEIGHT 이상이면(3개 이상) 연결 후보로 둔다.
# 2026-11-16 표본: 실제 관련 쌍은 대부분 24 이상, 무관한 쌍은 12~14 (흔한 낱말 때문).
# 연결을 그대로 따라가면 흔한 정치 낱말로 수백 개 사건이 한 이슈가 되므로(표본에서 218개),
# 강한 연결부터 차례로 잇되 이슈 크기가 ISSUE_MAX_UNITS 를 넘게 되는 연결은 하지 않는다.
# 크기는 2곳 이상 보도한 사건 1, 1곳만 보도한 사건 0.25 로 센다 (단독 칼럼·해설이 자리를 채우지 않게).
# 1곳만 보도한 사건은 '잎'으로만 붙는다: 한 이슈에 붙을 수는 있지만 두 이슈를 잇는 다리가 될 수 없다
# (표본에서 해설 기사 하나가 집값·AI 커닝·태양광 기사를 한 이슈로 엮었다).
ISSUE_MIN_SHARED_WEIGHT = 13.0
ISSUE_MAX_UNITS = 20.0
ISSUE_MIN_SHARED_TOKENS = 3
ISSUE_MAX_GAP_HOURS = 72
# 여러 사건에 두루 나오는 낱말: 이슈 연결에서는 세지 않는다
ISSUE_STOP_TOKENS = {
    "개월", "만에", "최대", "최고", "역대", "상회", "회의", "위원", "대통", "통령", "정부", "국회", "의장",
    "논란", "검토", "추진", "발표", "공식", "오늘", "내일", "올해", "내년", "기자", "현장", "사진",
}


def link_issues(groups: list[dict[str, Any]], idf: dict[str, float]) -> list[int]:
    """사건 묶음끼리 관련 이슈로 연결한다. 반환: groups 와 같은 순서의 이슈 번호.

    시간 폭 초과로 갈라진 후속 보도(follow_of)는 항상 같은 이슈로 둔다.
    """
    n = len(groups)
    parent = list(range(n))

    def find(x: int) -> int:
        while parent[x] != x:
            parent[x] = parent[parent[x]]
            x = parent[x]
        return x

    def union(x: int, y: int) -> None:
        rx, ry = find(x), find(y)
        if rx != ry:
            parent[max(rx, ry)] = min(rx, ry)

    # 사건의 낱말 묶음: 첫 기사 제목 낱말 + 구성 기사 2개 이상에 나온 낱말.
    # 구성 기사 하나에만 나온 낱말은 빼서, 엉뚱한 기사 하나가 다른 주제를 끌어오지 않게 한다.
    profiles = []
    for g in groups:
        counts: Counter[str] = Counter()
        for m in g["members"]:
            counts.update(title_tokens(m["title"]))
        core = {t for t, c in counts.items() if c >= 2} | title_tokens(g["members"][0]["title"])
        profiles.append(core - ISSUE_STOP_TOKENS)
    index: dict[str, list[int]] = defaultdict(list)
    for i, toks in enumerate(profiles):
        for t in toks:
            index[t].append(i)
    gap = timedelta(hours=ISSUE_MAX_GAP_HOURS)
    size = [1.0 if len({m["outlet"] for m in g["members"]}) > 1 else 0.25 for g in groups]
    edges: list[tuple[float, int, int]] = []
    for i, g in enumerate(groups):
        if g.get("follow_of") is not None:
            edges.append((float("inf"), i, g["follow_of"]))  # 후속 보도는 항상 먼저 연결
        cand: Counter[int] = Counter()
        for t in profiles[i]:
            post = index[t]
            if len(post) > 60:  # 너무 흔한 낱말
                continue
            for j in post:
                if j > i:
                    cand[j] += 1
        for j, shared in cand.items():
            if shared < ISSUE_MIN_SHARED_TOKENS or groups[j]["region"] != g["region"]:
                continue
            h = groups[j]
            if h["t_first"] - g["t_last"] > gap or g["t_first"] - h["t_last"] > gap:
                continue
            w = sum(idf.get(t, 1.0) for t in profiles[i] & profiles[j])
            if w >= ISSUE_MIN_SHARED_WEIGHT:
                edges.append((w, i, j))
    edges.sort(key=lambda e: (-e[0], e[1], e[2]))
    single = [len({m["outlet"] for m in g["members"]}) <= 1 for g in groups]
    attached = [False] * n
    for w, i, j in edges:
        ri, rj = find(i), find(j)
        if ri == rj or size[ri] + size[rj] > ISSUE_MAX_UNITS:
            continue
        if w != float("inf"):
            if single[i] and single[j]:
                continue  # 단독 사건끼리는 잇지 않는다
            if (single[i] and attached[i]) or (single[j] and attached[j]):
                continue  # 이미 붙은 단독 사건은 다리가 될 수 없다
        attached[i] = attached[j] = True
        keep, drop = min(ri, rj), max(ri, rj)
        parent[drop] = keep
        size[keep] += size[drop]
    roots: dict[int, int] = {}
    return [roots.setdefault(find(i), len(roots)) for i in range(n)]


# ---------------------------------------------------------------- 사건 대상 지역
#
# source_region: 보도한 언론사의 소재 (kr / intl / mixed)
# target_region: 사건이 일어난 곳·당사자 (kr / intl / both)
#   - 국내 언론: 기본 kr. 해외 신호만 있으면 intl, 해외 신호와 한국 신호가 함께 있으면 both.
#     국제 섹션 기사는 기본 intl, 한국 신호가 있으면 both. 북한 기사는 kr(한반도 사안).
#   - 해외 언론: 기본 intl. 한국 신호가 있으면 kr (해외 언론이 본 한국 사건).
#   - 신호가 하나도 없으면 언론사 소재로 정하고 basis 를 'outlet_fallback' 으로 남긴다(분류 불확실).
# 주요 뉴스 60:40 은 target 기준: kr 할당 ← kr + both(한국이 당사자인 공동 사건), intl 할당 ← intl.

KR_SIGNALS_KO = [
    "한국", "한미", "한일", "한중", "한-", "이 대통령", "이재명", "국내", "우리 정부", "서울", "부산",
    "삼성", "현대차", "SK", "LG", "K-", "K팝", "케이팝", "북한", "김정은", "주한", "방한", "재외국민", "교민",
]
# 한 글자 약어(美·中·日·러 등)는 '달러', '中企' 처럼 다른 낱말 안에 들어가므로 경계를 확인한다
FOREIGN_ABBR_RE = re.compile(r"(?<![가-힣\u4e00-\u9fff])(美|中|日|러|英|佛|獨|印|伊|佛)(?![가-힣\u4e00-\u9fff])")
KR_ABBR_RE = re.compile(r"(?<![가-힣\u4e00-\u9fff])(韓|北|李)(?![\u4e00-\u9fff])|(?<![A-Za-z])K-?[가-힣]")
FOREIGN_SIGNALS_KO = [
    "미국", "중국", "일본", "러시아", "우크라", "이스라엘", "이란", "가자", "하마스", "유럽",
    "EU", "나토", "유엔", "트럼프", "시진핑", "푸틴", "다카이치", "젤렌스키", "네타냐후", "백악관", "독일", "영국",
    "프랑스", "인도", "대만", "필리핀", "베트남", "태국", "인니", "인도네시아", "캐나다", "멕시코", "브라질", "튀르키예",
    "사우디", "예멘", "시리아", "아세안", "APEC", "G20", "G7", "FT", "WSJ", "블룸버그", "로이터", "NYT",
]
KR_SIGNALS_EN = re.compile(
    r"\b(south korea|korea|korean|koreans|seoul|busan|pyongyang|kim jong|lee jae|yoon suk|samsung|hyundai|"
    r"sk hynix|k-pop|bts|blackpink)\b",
    re.I,
)


def article_target(rec: dict[str, Any]) -> tuple[str, str]:
    # 제목만 본다. RSS 요약 첫머리는 "(마닐라=연합뉴스)" 처럼 취재지가 붙어 국내 사안도 해외로 보이게 한다.
    text = rec.get("title", "")
    if rec.get("region") == "intl":
        return ("kr", "korea_signal") if KR_SIGNALS_EN.search(text) else ("intl", "outlet_intl")
    kr = any(w in text for w in KR_SIGNALS_KO) or bool(KR_ABBR_RE.search(text))
    foreign = any(w in text for w in FOREIGN_SIGNALS_KO) or bool(FOREIGN_ABBR_RE.search(text))
    cat = rec.get("feed_category")
    if cat == "northkorea":
        return "kr", "feed_northkorea"
    if cat == "world":
        return ("both", "world_feed+korea_signal") if kr else ("intl", "world_feed")
    if foreign and kr:
        return "both", "foreign+korea_signal"
    if foreign:
        return "intl", "foreign_signal"
    if kr:
        return "kr", "korea_signal"
    return "kr", "outlet_fallback"


def story_target(members: list[dict[str, Any]]) -> tuple[str, str]:
    """사건 대상: 구성 기사 다수결. 국내·해외 판정이 각각 1/3 이상 섞이면 공동 사건(both).

    기사 하나의 판정이 사건 전체를 바꾸지 않게 한다. 근거(basis)는 다수 쪽 기사들의 것.
    """
    judged = [article_target(m) for m in members]
    votes = Counter(t for t, _ in judged)
    n = len(judged)
    kr_share = (votes.get("kr", 0) + votes.get("both", 0)) / n
    intl_share = (votes.get("intl", 0) + votes.get("both", 0)) / n
    if votes.get("both", 0) / n > 0.5 or (votes.get("kr") and votes.get("intl") and min(kr_share, intl_share) >= 1 / 3):
        target = "both"
    else:
        target = max(("kr", "intl", "both"), key=lambda k: (votes.get(k, 0), k == "kr"))
    basis = Counter(b for t, b in judged if t == target).most_common(1)
    return target, basis[0][0] if basis else "mixed"


def quota_bucket(target: str) -> str:
    return "intl" if target == "intl" else "kr"


# ---------------------------------------------------------------- 대표 기사

TIME_RELIABILITY = {"published": 1.0, "dc:date": 0.9, "updated_only": 0.6, "legacy": 0.5}
CONSOLIDATED_RE = re.compile(r"\((?:종합|종합\d보)\)|\(\d보\)|\b(?:update|updated)\b", re.I)
FLASH_RE = re.compile(r"^\s*\[(?:속보|1보|포토|사진|영상|그래픽)\]")
UPDATE_NO_RE = re.compile(r"\(종합(\d)보\)|\((\d)보\)")


def representative_basis(m: dict[str, Any], latest: datetime | None) -> dict[str, Any]:
    """대표 기사 판단 재료. observed 는 피드에서 확인한 사실, estimated 는 제목 표기로 미루어 본 추정."""
    t = article_time(m)
    summary = m.get("summary") or ""
    title = m.get("title") or ""
    observed = {
        "time_reliability": TIME_RELIABILITY.get(m.get("time_basis"), 0.0) if t else 0.0,
        "summary_len": len(summary),
        "direct_link": m.get("via") == "direct",
        "hours_behind_latest": round((latest - t).total_seconds() / 3600, 2) if (t and latest) else None,
    }
    estimated = {}
    if CONSOLIDATED_RE.search(title):
        estimated["consolidated"] = "제목에 (종합)/(N보) 표기 → 내용을 정리·보강한 기사로 추정"
    if FLASH_RE.search(title):
        estimated["flash"] = "제목에 [속보]/[포토] 등 표기 → 짧은 초기 보도로 추정"
    no = UPDATE_NO_RE.search(title)
    if no:
        estimated["update_no"] = int(no.group(1) or no.group(2))
    return {"observed": observed, "estimated": estimated}


def representative_score(basis: dict[str, Any]) -> float:
    o, e = basis["observed"], basis["estimated"]
    # 완결성: 요약 길이(관측) + 종합·속보 표기(추정). 품질·독립 취재를 판정하는 값이 아니다.
    completeness = min(1.0, o["summary_len"] / 120) * 0.7 + (0.3 if o["direct_link"] else 0.0)
    if "consolidated" in e:
        completeness += 0.15
    if "flash" in e:
        completeness -= 0.25
    behind = o["hours_behind_latest"]
    freshness = 0.0 if behind is None else math.pow(0.5, behind / 6.0)  # 새 전개 반영: 묶음 최신 기사에 가까울수록
    return round(0.35 * o["time_reliability"] + 0.4 * max(0.0, completeness) + 0.25 * freshness, 4)


def pick_representative(members: list[dict[str, Any]]) -> dict[str, Any]:
    """대표 기사: 시각 신뢰도 · 내용 완결성 · 새 전개 반영을 함께 본다.

    언론사 고정 순위(OUTLET_PRIORITY)는 점수가 같을 때만 쓰는 마지막 기준이다.
    """
    dated = [m for m in members if article_time(m)]
    pool = dated or members
    times = [t for t in (article_time(m) for m in pool) if t]
    latest = max(times) if times else None

    def key(r: dict[str, Any]) -> tuple:
        score = representative_score(representative_basis(r, latest))
        outlet = r.get("outlet", "")
        prio = OUTLET_PRIORITY.index(outlet) if outlet in OUTLET_PRIORITY else len(OUTLET_PRIORITY)
        return (-score, prio, -(article_time(r) or datetime.min.replace(tzinfo=UTC)).timestamp(), r.get("id", ""))

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


def provisional_story_id(members: list[dict[str, Any]]) -> str:
    """대장(registry)에 기록이 없을 때 쓰는 사건 ID: 최초(seed) 기사 ID 에서 만든다."""
    seed = min(members, key=lambda m: (article_time(m) or datetime.max.replace(tzinfo=UTC), m.get("id", "")))
    return "s_" + hashlib.sha1(("story|" + seed["id"]).encode("utf-8")).hexdigest()[:12]


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
    source_regions = {m["region"] for m in members}
    target, target_basis = story_target(members)
    return {
        # 사건 ID 는 대표 기사와 별개다. 대장(registry)이 실행 간에 이어 준다 (assign_story_ids).
        "id": provisional_story_id(members),
        # 카드에 보이는 제목·링크·언론사·시각은 모두 대표 기사 하나에서 온다
        "title": rep["title"],
        "url": rep["url"],
        "summary": rep.get("summary", ""),
        "outlet": rep["outlet"],
        "published_at": iso(rep_time),
        "time_basis": rep.get("time_basis"),
        "rep_article_id": rep["id"],
        "rep_basis": representative_basis(rep, latest),
        # region: 주요 뉴스 할당 구분(사건 대상 기준). source_region: 보도 언론사 소재.
        "region": quota_bucket(target),
        "target_region": target,
        "target_basis": target_basis,
        "source_region": source_regions.pop() if len(source_regions) == 1 else "mixed",
        "section": rep["section"],
        "category": category,
        "category_label": CATEGORIES.get(category, category),
        "latest_reported_at": iso(latest),
        "first_reported_at": iso(earliest),
        "span_hours": round((latest - earliest).total_seconds() / 3600, 2) if times else None,
        "outlet_count": len(outlets),
        "article_count": len(members),
        "undated_article_count": sum(1 for m in members if not article_time(m)),
        "leans": leans,
        "score": round(score, 4),
        "low_value": all(is_low_value(m) for m in members),
        # 사건 단위 AI 허용: 구성 기사가 전부 허용일 때만. 실제 요청 직전에 outlets.ai_gate 로 재검사한다.
        "ai_ok": all(bool(m.get("ai_ok")) for m in members),
        "issue_id": None,
        "issue_size": 1,
        # Claude 편집 단계가 채우는 필드 (없으면 화면은 원문 제목·요약을 보여준다)
        "title_ko": None,
        "summary_ko": None,
        "articles": [public_article(m) for m in sorted(members, key=lambda r: r["outlet"])],
    }


# ---------------------------------------------------------------- 사건 ID 대장
#
# 대장(state/story_registry.json): 기사 ID → 사건 ID, 사건 ID → 생성 시각.
# 매 실행 묶음 결과를 대장과 맞춰 ID 를 이어 준다.
#   - 묶음의 기사들이 이전에 속했던 사건 ID 중 가장 많은 기사가 속했던 것을 물려받는다.
#     (동수면 먼저 생긴 사건). 대표 기사가 바뀌어도 ID 는 그대로다.
#   - 합쳐짐: 이전 사건 여러 개의 기사가 한 묶음에 모이면, 다수 쪽 ID 를 유지하고
#     나머지는 merged_from 에 기록한다 (대장에는 merged_into 로 남김).
#   - 나뉨: 이전 사건 하나의 기사가 여러 묶음으로 흩어지면, 가장 많이 가져간 묶음이 ID 를 유지하고
#     나머지는 새 ID 와 split_from 을 받는다.
#   - 처음 보는 묶음은 seed 기사에서 만든 임시 ID 를 새 사건 ID 로 등록한다.


def assign_story_ids(stories: list[dict[str, Any]], registry: dict[str, Any], now_iso: str) -> dict[str, int]:
    arts = registry.setdefault("articles", {})
    meta = registry.setdefault("stories", {})
    claims = []  # (기사 수, -생성시각 순위, 사건 인덱스, 이전 ID)
    prev_by_story: list[Counter] = []
    for i, s in enumerate(stories):
        prev = Counter(arts[a["id"]] for a in s["articles"] if a["id"] in arts)
        prev_by_story.append(prev)
        for sid, n in prev.items():
            claims.append((n, meta.get(sid, {}).get("created_at", "~"), i, sid))
    # 기사 수 많은 순, 같으면 먼저 생긴 사건 순
    claims.sort(key=lambda c: (-c[0], c[1]))
    assigned: dict[int, str] = {}
    used: set[str] = set()
    for _, _, i, sid in claims:
        if i in assigned or sid in used:
            continue
        assigned[i] = sid
        used.add(sid)
    stats = Counter()
    for i, s in enumerate(stories):
        prev = prev_by_story[i]
        if i in assigned:
            s["id"] = assigned[i]
            stats["kept"] += 1
        else:
            sid = s["id"]
            while sid in used:  # 임시 ID 충돌 (드묾)
                sid = "s_" + hashlib.sha1(sid.encode()).hexdigest()[:12]
            s["id"] = sid
            used.add(sid)
            meta.setdefault(sid, {"created_at": now_iso})
            stats["new"] += 1
            if prev:  # 이전 사건의 일부가 떨어져 나와 새 사건이 됨
                s["lineage"] = {"split_from": prev.most_common(1)[0][0]}
                stats["split"] += 1
        merged = [sid for sid in prev if sid != s["id"]]
        if merged and i in assigned:
            s.setdefault("lineage", {})["merged_from"] = sorted(merged)
            stats["merged"] += 1
            for sid in merged:
                if sid not in used:
                    meta.setdefault(sid, {})["merged_into"] = s["id"]
        meta.setdefault(s["id"], {"created_at": now_iso})
        s["created_at"] = meta[s["id"]].get("created_at", now_iso)
        for a in s["articles"]:
            arts[a["id"]] = s["id"]
    return dict(stats)


def prune_registry(registry: dict[str, Any], keep_article_ids: set[str]) -> None:
    arts = registry.get("articles", {})
    for aid in [a for a in arts if a not in keep_article_ids]:
        del arts[aid]
    live = set(arts.values())
    meta = registry.get("stories", {})
    for sid in [s for s in meta if s not in live and not meta[s].get("merged_into")]:
        del meta[sid]


def assign_issues(stories: list[dict[str, Any]], issue_of: list[int]) -> list[dict[str, Any]]:
    """같은 이슈 번호의 사건들에 안정적인 이슈 ID 를 붙이고, 2개 이상인 이슈 목록을 돌려준다."""
    members: dict[int, list[dict[str, Any]]] = defaultdict(list)
    for s, k in zip(stories, issue_of):
        members[k].append(s)
    issues = []
    for group in members.values():
        anchor = min(group, key=lambda s: (s.get("created_at") or "~", s["id"]))
        iid = "i_" + anchor["id"][2:]
        for s in group:
            s["issue_id"] = iid
            s["issue_size"] = len(group)
        if len(group) > 1:
            lead = max(group, key=lambda s: s["score"])
            issues.append(
                {
                    "id": iid,
                    "title": lead["title"],
                    "story_ids": [s["id"] for s in sorted(group, key=lambda s: -s["score"])],
                    "region": lead["region"],
                }
            )
    return issues


# ---------------------------------------------------------------- 주요 뉴스 선정

# 같은 이슈(관련 사건 묶음)가 주요 뉴스에서 차지할 수 있는 최대 칸 수
ISSUE_CAP_TOP = 2


def select_top(
    stories: list[dict[str, Any]],
    total: int = TOP_TOTAL,
    report: dict[str, Any] | None = None,
    cap_ratio: float = CATEGORY_CAP,
    issue_cap: int = ISSUE_CAP_TOP,
) -> list[dict[str, Any]]:
    """사건 대상 지역 기준 국내/해외 할당을 점수 순으로 채운다.

    - 분야 상한: 할당 × cap_ratio 의 내림(floor). 상한을 넘기는 채우기는 하지 않는다.
      예외: 해외 할당의 '국제'(world) 분류. 해외 피드는 거의 모두 이 분류라 상한을 걸면 후보가 사라진다.
    - 이슈 상한: 같은 이슈의 사건은 issue_cap 칸까지 (국내·해외 합산).
    - 후보가 모자라면 적게 고르고 report 에 부족 사유를 남긴다.
    """
    report = report if report is not None else {}
    picked: list[dict[str, Any]] = []
    per_issue: Counter[str] = Counter()
    for region, share in REGION_SHARE.items():
        quota = round(total * share)
        cap = math.floor(quota * cap_ratio)
        per_cat: Counter[str] = Counter()
        skipped: Counter[str] = Counter()
        pool = [s for s in stories if s["region"] == region and s["section"] == "news" and not s["low_value"]]
        pool.sort(key=lambda s: (-s["score"], s["id"]))
        chosen: list[dict[str, Any]] = []
        for s in pool:
            if len(chosen) >= quota:
                break
            exempt = region == "intl" and s["category"] == "world"
            if not exempt and per_cat[s["category"]] >= cap:
                skipped["category_cap:" + s["category"]] += 1
                continue
            issue = s.get("issue_id") or s["id"]
            if per_issue[issue] >= issue_cap:
                skipped["issue_cap"] += 1
                continue
            chosen.append(s)
            per_cat[s["category"]] += 1
            per_issue[issue] += 1
        shortfall = quota - len(chosen)
        report[region] = {
            "quota": quota,
            "selected": len(chosen),
            "shortfall": shortfall,
            "category_cap": cap,
            "category_cap_exempt": ["world"] if region == "intl" else [],
            "by_category": dict(per_cat),
            "skipped": dict(skipped),
            "candidates": len(pool),
            "reason": None if shortfall <= 0 else (
                "후보 부족" if len(pool) < quota else "분야·이슈 상한 때문에 남은 후보로 채울 수 없음"
            ),
        }
        picked.extend(chosen)
    picked.sort(key=lambda s: (-s["score"], s["id"]))
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


# ---------------------------------------------------------------- 상태 저장·복구

QUALITY_CONFIG_PATH = Path(__file__).resolve().parents[1] / "config" / "quality.json"


def load_quality_config(path: Path | None = None) -> dict[str, Any]:
    return json.loads(Path(path or QUALITY_CONFIG_PATH).read_text(encoding="utf-8"))


def write_json_atomic(path: Path, data: Any, pretty: bool = False) -> None:
    """임시 파일에 쓴 뒤 바꿔치기. 중간에 실패해도 반쯤 쓴 파일이 남지 않는다."""
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    text = json.dumps(data, ensure_ascii=False, indent=2) if pretty else json.dumps(
        data, ensure_ascii=False, separators=(",", ":")
    )
    tmp.write_text(text, encoding="utf-8")
    os.replace(tmp, path)


def fetch_site_json(site_url: str, rel: str) -> dict[str, Any] | None:
    """배포된 페이지의 JSON (캐시 유실 시 이력·기준값 복구용). 실패하면 None."""
    if not site_url:
        return None
    try:
        r = requests.get(site_url.rstrip("/") + "/" + rel, timeout=15, headers={"User-Agent": BROWSER_UA})
        if r.status_code != 200:
            return None
        return r.json()
    except Exception:  # noqa: BLE001
        return None


def load_source_history(state_dir: Path, site_url: str) -> tuple[dict[str, Any], str]:
    """소스별 이력. 캐시(state/) → 배포된 status.json → 없음 순으로 찾는다."""
    p = state_dir / "source_history.json"
    if p.exists():
        try:
            return json.loads(p.read_text(encoding="utf-8")).get("sources", {}), "cache"
        except Exception:  # noqa: BLE001
            pass
    deployed = fetch_site_json(site_url, "data/status.json")
    if deployed:
        hist = {s["url"]: s["history"] for s in deployed.get("sources", []) if s.get("url") and s.get("history")}
        if hist:
            return hist, "site"
    return {}, "none"


def archive_started_at(path: Path) -> str | None:
    if not path.exists():
        return None
    try:
        return json.loads(path.read_text(encoding="utf-8")).get("started_at")
    except Exception:  # noqa: BLE001
        return None


def summarize_sources(statuses: list[dict[str, Any]]) -> dict[str, Any]:
    active = [s for s in statuses if s["state"] != "disabled"]
    by_state = Counter(s["state"] for s in active)
    return {
        "active": len(active),
        "ok": sum(1 for s in active if s["state"] in GOOD_STATES),
        "by_state": dict(by_state),
        "failed": [s["name"] for s in active if s["state"] in ("http_error", "parse_error")],
        "empty": [s["name"] for s in active if s["state"] in ("empty", "filtered_all")],
        "stale": [s["name"] for s in active if s["state"] == "stale"],
        "undated": [s["name"] for s in active if s["state"] == "undated"],
        "disabled": [s["name"] for s in statuses if s["state"] == "disabled"],
    }


def load_story_registry(state_dir: Path, site_url: str) -> tuple[dict[str, Any], str]:
    """사건 ID 대장. 캐시 → 배포된 news.json(기사→사건 대응) → 새로 시작."""
    p = state_dir / "story_registry.json"
    if p.exists():
        try:
            data = json.loads(p.read_text(encoding="utf-8"))
            return {"articles": data.get("articles", {}), "stories": data.get("stories", {})}, "cache"
        except Exception:  # noqa: BLE001
            pass
    deployed = fetch_site_json(site_url, "data/news.json")
    if deployed:
        reg: dict[str, Any] = {"articles": {}, "stories": {}}
        for st in deployed.get("news", []) + deployed.get("science", []):
            sid = st.get("id", "")
            if not sid.startswith("s_"):
                continue  # 이전 형식(대표 기사 ID 를 사건 ID 로 쓰던 때)은 이어받지 않는다
            reg["stories"][sid] = {"created_at": st.get("created_at") or deployed.get("generated_at")}
            for a in st.get("articles", []):
                reg["articles"][a["id"]] = sid
        if reg["articles"]:
            return reg, "site"
    return {"articles": {}, "stories": {}}, "none"


PUBLISHED_NEWS_LIMIT = 600


def compose(
    archive: dict[str, dict[str, Any]], now: datetime, top_n: int, registry: dict[str, Any]
) -> dict[str, Any]:
    """아카이브 → 사건·이슈·주요 뉴스. 네트워크를 쓰지 않으므로 같은 표본에 다시 돌려 비교할 수 있다."""
    now_iso = iso(now)
    news_after = now - timedelta(hours=NEWS_WINDOW_HOURS)
    sci_after = now - timedelta(days=SCIENCE_WINDOW_DAYS)
    journal_after = now - timedelta(days=JOURNAL_WINDOW_DAYS)
    news_recs, sci_recs = [], []
    news_undated, sci_undated = [], []
    for rec in archive.values():
        if rec["section"] == "taste":
            continue  # 취향 탭 후보 전용 피드는 주요 뉴스·과학 탭에 넣지 않는다
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

    cluster_stats: Counter[str] = Counter()
    news_groups, news_left, news_idf = cluster_detail(news_recs, news_undated, cluster_stats)
    sci_groups, sci_left, _ = cluster_detail(sci_recs, sci_undated, Counter())
    news_stories = [build_story(g["members"], now) for g in news_groups]
    sci_stories = [build_story(g["members"], now) for g in sci_groups]
    id_stats = assign_story_ids(news_stories + sci_stories, registry, now_iso)
    issues = assign_issues(news_stories, link_issues(news_groups, news_idf))
    for st in sci_stories:
        st["issue_id"] = "i_" + st["id"][2:]
    prune_registry(registry, set(archive))

    news_stories.sort(key=lambda s: (-s["score"], s["id"]))
    # 과학은 묶기만 하고, 매체별로 번갈아 최신순 배치 (국내 전기 매체가 저널을 덮지 않도록)
    sci_stories = interleave_by_outlet(sci_stories)
    selection: dict[str, Any] = {}
    top = select_top(news_stories, top_n, selection)
    top_ids = {s["id"] for s in top}
    visible = [s for s in news_stories if not s["low_value"]]
    published = [s for i, s in enumerate(visible) if i < PUBLISHED_NEWS_LIMIT or s["id"] in top_ids]
    return {
        "news_recs": news_recs,
        "sci_recs": sci_recs,
        "news_undated": news_undated,
        "sci_undated": sci_undated,
        "undated_left": news_left + sci_left,
        "news_stories": news_stories,
        "sci_stories": sci_stories,
        "visible_news": visible,
        "published_news": published,
        "top": top,
        "selection": selection,
        "issues": issues,
        "cluster_stats": dict(cluster_stats),
        "id_stats": id_stats,
    }


def build_taste(
    archive: dict[str, dict[str, Any]], now: datetime, out_dir: Path, state_dir: Path, profile_path: str = ""
) -> str:
    """취향 탭 데이터. 실패해도 뉴스 배포를 막지 않도록 오류를 taste.json 에 남기고 계속한다."""
    import taste

    now_iso = iso(now)
    try:
        prof = taste.load_profile(Path(profile_path) if profile_path else None)
        store_path = state_dir / "taste_candidates.json"
        store = taste.load_store(store_path)
        recs = [r for r in archive.values() if r.get("time_basis") not in ("unknown", "invalid_future")]
        stats = taste.update_store(store, recs, prof, now, now_iso, parse_iso)
        result = taste.select(
            prof, list(store["items"].values()), now, parse_iso, lambda rs: cluster_with_undated(rs, [])[0], title_tokens
        )
        payload = taste.build_payload(prof, result, store, now_iso)
        write_json_atomic(out_dir / "taste.json", sanitize_public_payload(payload))
        write_json_atomic(
            store_path, {"generated_at": now_iso, "started_at": store["started_at"], "items": list(store["items"].values())}
        )
        kinds = Counter(i["kind"] for i in payload["items"])
        return f"candidates {stats['total']} (+{stats['added']}) | picked {dict(kinds)} | shortfall {payload['shortfall']}"
    except Exception as exc:  # noqa: BLE001
        write_json_atomic(out_dir / "taste.json", {"generated_at": now_iso, "items": [], "error": str(exc)[:300]})
        return f"error: {exc}"


# ---------------------------------------------------------------- main


def main() -> int:
    ap = argparse.ArgumentParser(description="시몬스 뉴스 데이터 생성")
    ap.add_argument("--sources", default="feeds/sources.opml")
    ap.add_argument("--output-dir", default="data")
    ap.add_argument("--top", type=int, default=TOP_TOTAL)
    ap.add_argument("--policy", default="", help="언론사 정책 표 (기본: feeds/outlets.json)")
    ap.add_argument("--config", default="", help="품질 기준 (기본: config/quality.json)")
    ap.add_argument("--taste-profile", default="", help="취향 프로필 (기본: config/taste.json)")
    ap.add_argument(
        "--site-url",
        default=os.environ.get("SITE_URL", ""),
        help="배포된 페이지 주소. 캐시가 없을 때 소스 이력을 여기서 복구한다",
    )
    args = ap.parse_args()

    now = utc_now()
    now_iso = iso(now)
    out_dir = Path(args.output_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    archive_path = out_dir / "archive.json"
    state_dir = out_dir / "state"
    qconf = load_quality_config(Path(args.config) if args.config else None)
    cadence_hours = {**DEFAULT_CADENCE_HOURS, **qconf.get("cadence_hours", {})}

    table = load_policy(Path(args.policy) if args.policy else None)
    all_sources = load_sources(Path(args.sources), include_disabled=True)
    sources = [s for s in all_sources if s["enabled"]]
    fetched, statuses = fetch_all(sources, now, table, cadence_hours)
    for s in all_sources:
        if not s["enabled"]:
            statuses.append(
                {
                    "name": s["name"],
                    "url": s["url"],
                    "region": s["region"],
                    "section": s["section"],
                    "cadence": s["cadence"],
                    "state": "disabled",
                    "state_label": STATE_LABELS["disabled"],
                    "ok": False,
                    "item_count": 0,
                    "error": s["disabled_reason"] or None,
                }
            )

    # 소스 이력 (마지막 정상 수집, 연속 실패·0건 횟수)
    history, history_source = load_source_history(state_dir, args.site_url)
    for st in statuses:
        if st["state"] != "disabled":
            st["history"] = update_history(history.get(st["url"]), st, now_iso)
        st.pop("newest_at_dt", None)

    cache_restored = archive_path.exists()
    started_at = archive_started_at(archive_path) if cache_restored else None
    archive = load_archive(archive_path, table)
    if not started_at:
        # 이전 형식이거나 캐시 유실: 남아 있는 기록 중 가장 이른 수집 시각, 없으면 지금부터
        started_at = min((r.get("first_seen_at") for r in archive.values() if r.get("first_seen_at")), default=now_iso)
    merge_stats = merge_into_archive(archive, fetched, now)
    refresh_policy(archive, table)
    # 취향 후보는 일반 뉴스 보관(3일)으로 잘라내기 전에 넘긴다. 취향 후보는 별도 보관소에서 28일 관리.
    pre_prune = dict(archive)
    archive = prune_archive(archive, now, ARCHIVE_DAYS)

    registry, registry_from = load_story_registry(state_dir, args.site_url)
    out = compose(archive, now, args.top, registry)
    news_recs, sci_recs = out["news_recs"], out["sci_recs"]
    news_stories, sci_stories, top = out["news_stories"], out["sci_stories"], out["top"]
    news_undated, sci_undated, undated_left = out["news_undated"], out["sci_undated"], out["undated_left"]
    top_ids = [s["id"] for s in top]
    visible_news = out["visible_news"]
    src_summary = summarize_sources(statuses)

    payload = {
        "schema": ARCHIVE_SCHEMA,
        "generated_at": iso(now),
        "categories": CATEGORIES,
        "region_share": REGION_SHARE,
        "top_ids": top_ids,
        # 화면이 이 시간보다 오래된 데이터를 보면 '갱신 지연'을 표시한다
        "stale_after_minutes": qconf.get("stale_after_minutes", 180),
        # 보관 데이터 범위. 캐시가 사라지면 이 시각 이전 기록(피드에서 이미 빠진 기사)은 복구할 수 없다.
        "coverage": {
            "archive_started_at": started_at,
            "cache_restored": cache_restored,
            "source_history_from": history_source,
        },
        # 주요 뉴스로 뽑힌 사건은 600건 제한과 관계없이 항상 포함한다
        "news": out["published_news"],
        "science": sci_stories[:150],
        # 관련 사건이 2개 이상인 이슈. 각 사건은 issue_id 로 연결된다.
        "issues": out["issues"],
        # 주요 뉴스 할당별 선정 결과와 부족 사유
        "selection": out["selection"],
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
            "top_quota_kr": round(args.top * REGION_SHARE["kr"]),
            "top_quota_intl": round(args.top * REGION_SHARE["intl"]),
            "kr_candidates": sum(1 for s in visible_news if s["region"] == "kr"),
            "intl_candidates": sum(1 for s in visible_news if s["region"] == "intl"),
            "sources_active": src_summary["active"],
            "sources_ok": src_summary["ok"],
            "undated_seen": len(news_undated) + len(sci_undated),
            "undated_attached": len(news_undated) + len(sci_undated) - len(undated_left),
            "archive_new": merge_stats.get("new", 0),
            "archive_changed": merge_stats.get("changed", 0),
            "ai_allowed_articles": sum(1 for r in archive.values() if r.get("ai_ok")),
            "cluster_blocked": out["cluster_stats"],
            "story_ids": out["id_stats"],
        },
    }
    status = {
        "generated_at": now_iso,
        "ok": src_summary["ok"],
        "active": src_summary["active"],
        "summary": src_summary,
        # 호환 필드
        "failed": src_summary["failed"],
        "empty": src_summary["empty"],
        "sources": sorted(statuses, key=lambda s: s["name"]),
    }

    write_json_atomic(out_dir / "news.json", sanitize_public_payload(payload))
    write_json_atomic(out_dir / "status.json", status, pretty=True)
    write_json_atomic(
        archive_path,
        {"schema": ARCHIVE_SCHEMA, "generated_at": now_iso, "started_at": started_at, "items": list(archive.values())},
    )
    write_json_atomic(state_dir / "story_registry.json", {"generated_at": now_iso, **registry})
    taste_summary = build_taste(pre_prune, now, out_dir, state_dir, args.taste_profile)
    write_json_atomic(
        state_dir / "source_history.json",
        {"generated_at": now_iso, "sources": {s["url"]: s["history"] for s in statuses if s.get("history")}},
        pretty=True,
    )

    c = payload["counts"]
    print(
        f"sources ok {status['ok']}/{status['active']} {src_summary['by_state']} | fetched {c['fetched']} | "
        f"news {c['news_articles']} articles → {c['news_stories']} stories | "
        f"top {len(top)} (kr {c['top_kr']} / intl {c['top_intl']}) | science {len(sci_stories)} | "
        f"undated {c['undated_seen']} (attached {c['undated_attached']}) | "
        f"archive +{c['archive_new']} changed {c['archive_changed']} | ai-allowed {c['ai_allowed_articles']}"
    )
    for key in ("failed", "empty", "stale", "disabled"):
        if src_summary[key]:
            print(f"{key}:", ", ".join(src_summary[key]))
    print(f"archive since {started_at} (cache restored: {cache_restored}, source history: {history_source}, story registry: {registry_from})")
    print(f"taste {taste_summary}")
    print(f"stories ids {out['id_stats']} | merge blocked {out['cluster_stats']} | issues {len(out['issues'])} | selection " +
          json.dumps({k: (v['selected'], v['quota'], v['reason']) for k, v in out['selection'].items()}, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
