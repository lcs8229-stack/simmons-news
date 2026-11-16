"""1단계: AI 정책(원언론사 기준), 시간 정보 분리, 같은 URL 기사 갱신."""

import json
from datetime import datetime, timedelta, timezone

from scripts import build_news as bn
from scripts.outlets import PolicyTable, ai_gate, load_policy, recheck

NOW = datetime(2026, 11, 16, 12, 0, tzinfo=timezone.utc)
TABLE = load_policy()


def src(**kw):
    base = {
        "name": "테스트 피드",
        "url": "https://example.com/rss",
        "outlet": "",
        "region": "kr",
        "section": "news",
        "category": "general",
        "lean": "",
        "gnews": False,
        "via": "direct",
        "feed_ai_deny": False,
        "tz": "Asia/Seoul",
    }
    base.update(kw)
    if base["gnews"]:
        base["via"] = "google_news"
    return base


def rss(items_xml: str) -> bytes:
    return f"""<?xml version="1.0" encoding="UTF-8"?>
<rss version="2.0"><channel><title>t</title>{items_xml}</channel></rss>""".encode("utf-8")


GNEWS_KR = rss(
    """
<item><title>정부, 수능 와이파이 차단 추진 - 연합뉴스</title>
  <link>https://news.google.com/rss/articles/AAA</link>
  <pubDate>Mon, 16 Nov 2026 10:00:00 GMT</pubDate>
  <source url="https://www.yna.co.kr">연합뉴스</source></item>
<item><title>Korea moves to block Wi-Fi at exam sites - Yonhap News Agency</title>
  <link>https://news.google.com/rss/articles/BBB</link>
  <pubDate>Mon, 16 Nov 2026 10:05:00 GMT</pubDate>
  <source url="https://en.yna.co.kr">Yonhap News Agency</source></item>
<item><title>한 총리 대응팀 구성 - 한겨레</title>
  <link>https://news.google.com/rss/articles/CCC</link>
  <pubDate>Mon, 16 Nov 2026 10:10:00 GMT</pubDate>
  <source url="https://www.hani.co.kr">한겨레</source></item>
<item><title>수능 대책 발표 - 처음보는신문</title>
  <link>https://news.google.com/rss/articles/DDD</link>
  <pubDate>Mon, 16 Nov 2026 10:20:00 GMT</pubDate>
  <source url="https://www.unknown-news.kr">처음보는신문</source></item>
"""
)


# ---------------------------------------------------------------- 1. AI 정책


def test_google_news_route_uses_original_outlet_policy():
    items = bn.parse_feed(GNEWS_KR, src(name="Google 뉴스 한국 헤드라인", gnews=True), NOW, TABLE)
    by_title = {i["title"]: i for i in items}
    yna = by_title["정부, 수능 와이파이 차단 추진"]
    assert yna["outlet"] == "연합뉴스" and yna["via"] == "google_news"
    assert yna["ai_ok"] is False and yna["ai"]["status"] == "deny"
    assert "outlet:연합뉴스:deny" in yna["ai"]["reasons"]
    # 영문 표기·다른 도메인(en.yna.co.kr)도 같은 언론사로 정규화되어 금지
    eng = by_title["Korea moves to block Wi-Fi at exam sites"]
    assert eng["outlet"] == "연합뉴스" and eng["ai_ok"] is False
    assert by_title["한 총리 대응팀 구성"]["outlet"] == "한겨레"
    assert by_title["한 총리 대응팀 구성"]["ai_ok"] is False
    # 정책 표에 없는 언론사는 미확인 → 제외
    unk = by_title["수능 대책 발표"]
    assert unk["ai_ok"] is False and unk["ai"]["status"] == "unknown"


def test_direct_feed_of_denied_outlet_is_blocked():
    feed = rss(
        "<item><title>기사</title><link>https://www.yna.co.kr/view/AKR1</link>"
        "<pubDate>Mon, 16 Nov 2026 19:00:00 +0900</pubDate></item>"
    )
    items = bn.parse_feed(feed, src(outlet="연합뉴스", feed_ai_deny=True), NOW, TABLE)
    assert items[0]["ai_ok"] is False
    assert set(items[0]["ai"]["reasons"]) >= {"feed:ai=no", "outlet:연합뉴스:deny"}


def test_outlet_alias_and_domain_normalization():
    assert TABLE.canonical("Yonhap") == "연합뉴스"
    assert TABLE.canonical("  yonhap news agency ") == "연합뉴스"
    assert TABLE.canonical("", "https://www.yna.co.kr/view/1") == "연합뉴스"
    assert TABLE.canonical("KBS 뉴스") == "KBS"
    assert TABLE.canonical("The New York Times") == "New York Times"
    assert TABLE.canonical("AP News", "https://apnews.com") == "AP"
    # 연합뉴스TV 는 별도 법인이라 연합뉴스로 합치지 않는다
    assert TABLE.canonical("연합뉴스TV", "https://www.yonhapnewstv.co.kr") == "연합뉴스TV"


def custom_table(outlet_ai: str, gnews_ai: str = "unknown") -> PolicyTable:
    return PolicyTable(
        outlets={"허용신문": {"aliases": [], "domains": ["ok-news.kr"], "ai": outlet_ai}},
        via={"direct": {"ai": "allow"}, "google_news": {"ai": gnews_ai}},
    )


def test_allow_requires_both_outlet_and_route_allow_and_feed_deny_wins():
    t = custom_table("allow")
    assert t.decide("허용신문", "direct", False)["allowed"] is True
    assert t.decide("허용신문", "google_news", False)["allowed"] is False  # 경로 미확인
    assert t.decide("허용신문", "direct", True)["allowed"] is False  # 피드 금지가 우선
    assert custom_table("unknown").decide("허용신문", "direct", False)["allowed"] is False
    t2 = custom_table("allow", gnews_ai="deny")
    assert t2.decide("허용신문", "google_news", False)["status"] == "deny"


def test_final_gate_ignores_stored_ai_ok_and_uses_current_policy():
    stale = {"outlet": "연합뉴스", "via": "google_news", "feed_ai_deny": False, "ai_ok": True}
    no_inputs = {"outlet": "허용신문", "ai_ok": True}  # 판정 재료가 없으면 금지
    allowed = {"outlet": "허용신문", "via": "direct", "feed_ai_deny": False, "ai_ok": False}
    t = custom_table("allow")
    t.outlets.update(TABLE.outlets)
    t.__post_init__()
    ok, blocked = ai_gate([stale, no_inputs, allowed], t)
    assert ok == [allowed]
    assert stale in blocked and no_inputs in blocked
    # 정책 표에서 금지로 바뀌면 이미 저장된 기사도 재검사에서 금지
    t.outlets["허용신문"]["ai"] = "deny"
    assert recheck(allowed, t)["allowed"] is False


def test_story_ai_ok_requires_every_article_allowed():
    items = bn.parse_feed(GNEWS_KR, src(gnews=True), NOW, TABLE)
    for i in items:
        i["id"] = bn.article_id(i["region"], i["outlet"], i["url"])
    story = bn.build_story(items[:2], NOW)
    assert story["ai_ok"] is False
    assert all(a["ai_ok"] is False for a in story["articles"])


# ---------------------------------------------------------------- 2. 시간


def test_missing_publish_time_stays_unknown_not_fetch_time():
    feed = rss("<item><title>날짜 없는 기사</title><link>https://www.hani.co.kr/arti/1.html</link></item>")
    it = bn.parse_feed(feed, src(outlet="한겨레"), NOW, TABLE)[0]
    assert it["published_at"] is None and it["updated_at"] is None
    assert it["time_basis"] == "unknown"
    archive = {}
    bn.merge_into_archive(archive, [it], NOW)
    rec = next(iter(archive.values()))
    assert rec["first_seen_at"] == bn.iso(NOW)
    assert bn.article_time(rec) is None  # 수집 시각을 보도 시각으로 쓰지 않음


def test_naive_time_uses_source_timezone():
    feed = rss(
        "<item><title>전기 기사</title><link>https://www.electimes.com/news/1</link>"
        "<pubDate>2026-11-16 17:57:55</pubDate></item>"
    )
    kr = bn.parse_feed(feed, src(outlet="전기신문", tz="Asia/Seoul"), NOW, TABLE)[0]
    assert kr["published_at"] == "2026-11-16T08:57:55Z"  # KST 17:57 = UTC 08:57
    utc = bn.parse_feed(feed, src(outlet="전기신문", tz="UTC"), NOW, TABLE)[0]
    assert utc["time_basis"] == "invalid_future"  # UTC 로 읽으면 미래 시각 → 미상 처리


def test_rss_pubdate_only_does_not_invent_update_time():
    # feedparser 는 entry.get("updated") 에 published 값을 대신 돌려주는 호환 매핑이 있다
    feed = rss(
        "<item><title>A</title><link>https://www.chosun.com/a/</link>"
        "<pubDate>Mon, 16 Nov 2026 08:00:00 +0000</pubDate></item>"
    )
    it = bn.parse_feed(feed, src(outlet="조선일보"), NOW, TABLE)[0]
    assert it["published_at"] == "2026-11-16T08:00:00Z"
    assert it["updated_at"] is None and it["time_basis"] == "published"


def test_rss1_dc_date_is_publication_and_atom_updated_is_modification():
    rdf = b"""<?xml version="1.0"?>
<rdf:RDF xmlns:rdf="http://www.w3.org/1999/02/22-rdf-syntax-ns#" xmlns="http://purl.org/rss/1.0/"
  xmlns:dc="http://purl.org/dc/elements/1.1/">
<channel rdf:about="x"><title>n</title></channel>
<item rdf:about="https://www.nature.com/articles/1"><title>Paper</title>
<link>https://www.nature.com/articles/1</link><dc:date>2026-11-16</dc:date></item></rdf:RDF>"""
    it = bn.parse_feed(rdf, src(outlet="Nature", region="intl", tz="UTC"), NOW, TABLE)[0]
    assert it["time_basis"] == "dc:date" and it["published_at"] == "2026-11-16T00:00:00Z"

    atom = b"""<?xml version="1.0"?><feed xmlns="http://www.w3.org/2005/Atom"><title>a</title>
<entry><title>Edited</title><link href="https://example.org/a"/>
<published>2026-11-16T01:00:00Z</published><updated>2026-11-16T05:00:00Z</updated></entry>
<entry><title>Only updated</title><link href="https://example.org/b"/>
<updated>2026-11-16T06:00:00Z</updated></entry></feed>"""
    a, b = bn.parse_feed(atom, src(region="intl", tz="UTC"), NOW, TABLE)
    assert (a["published_at"], a["updated_at"], a["time_basis"]) == (
        "2026-11-16T01:00:00Z",
        "2026-11-16T05:00:00Z",
        "published",
    )
    assert b["published_at"] is None and b["time_basis"] == "updated_only"
    assert bn.article_time(b) == datetime(2026, 11, 16, 6, 0, tzinfo=timezone.utc)


def rec(title, outlet, hours_ago=None, url=None):
    t = bn.iso(NOW - timedelta(hours=hours_ago)) if hours_ago is not None else None
    u = url or f"https://example.com/{outlet}/{abs(hash(title))}"
    return {
        "id": bn.article_id("kr", outlet, u),
        "title": title,
        "url": u,
        "outlet": outlet,
        "summary": "",
        "published_at": t,
        "updated_at": None,
        "time_basis": "published" if t else "unknown",
        "first_seen_at": bn.iso(NOW),  # 방금 수집 — 미상 기사가 이것 때문에 최신으로 보이면 안 된다
        "last_seen_at": bn.iso(NOW),
        "region": "kr",
        "section": "news",
        "feed_category": "general",
        "lean": "",
        "via": "direct",
        "feed_ai_deny": False,
        "ai_ok": False,
    }


def test_undated_article_never_leads_or_freshens_a_story():
    dated = [rec("정부, 수능 스마트기기 반납·공공 와이파이 차단 추진", "SBS", hours_ago=20)]
    undated = [
        rec("정부, 수능 시험장 스마트기기 반납·공공 와이파이 차단 추진", "연합뉴스"),  # 우선순위 1위 언론사
        rec("전혀 다른 단독 기사 제목입니다", "한겨레"),
    ]
    groups, left = bn.cluster_with_undated(dated, undated)
    assert len(groups) == 1 and len(groups[0]) == 2
    assert [r["outlet"] for r in left] == ["한겨레"]  # 붙을 사건이 없는 미상 기사는 따로
    story = bn.build_story(groups[0], NOW)
    # 대표는 시각이 확인된 SBS (연합뉴스가 우선순위는 높지만 시각 미상)
    assert story["outlet"] == "SBS"
    assert story["latest_reported_at"] == dated[0]["published_at"]
    assert story["undated_article_count"] == 1 and story["outlet_count"] == 2
    fresh = bn.build_story([rec("같은 제목", "SBS", hours_ago=0)], NOW)
    old = bn.build_story([rec("같은 제목", "SBS", hours_ago=20)], NOW)
    assert old["score"] < fresh["score"]  # 20시간 전 기사는 수집이 방금이어도 신선하지 않다


def test_card_fields_come_from_one_representative_article():
    members = [
        rec("수능 와이파이 차단 추진 대책 발표 공식화", "SBS", hours_ago=5),
        rec("수능 와이파이 차단 추진 대책 발표 공식화 종합", "연합뉴스", hours_ago=9),
        rec("수능 와이파이 차단 추진 대책 발표 공식화 속보", "경향신문", hours_ago=1),
    ]
    s = bn.build_story(members, NOW)
    rep = next(a for a in s["articles"] if a["id"] == s["rep_article_id"])
    assert (s["title"], s["url"], s["outlet"], s["published_at"]) == (
        rep["title"],
        rep["url"],
        rep["outlet"],
        rep["published_at"],
    )
    # 3단계: 요약·시각 신뢰도가 같으면 새 전개(가장 최근 기사)를 반영한 기사가 대표. 언론사 순위는 동점일 때만.
    assert s["outlet"] == "경향신문"
    assert s["latest_reported_at"] == members[2]["published_at"] == s["published_at"]
    assert s["first_reported_at"] == members[1]["published_at"]


# ---------------------------------------------------------------- 3. 같은 URL 갱신


def test_same_url_correction_updates_content_and_keeps_first_seen():
    url = "https://www.chosun.com/politics/2026/11/16/ABC/"
    v1 = rss(
        f"<item><title>장관 후보자 지명</title><link>{url}</link><description>초판 요약</description>"
        "<pubDate>Mon, 16 Nov 2026 08:00:00 +0000</pubDate></item>"
    )
    v2 = rss(
        f"<item><title>[정정] 차관 후보자 지명</title><link>{url}</link><description>정정 요약</description>"
        "<pubDate>Mon, 16 Nov 2026 08:00:00 +0000</pubDate></item>"
    )
    s = src(outlet="조선일보")
    archive = {}
    t1, t2 = NOW, NOW + timedelta(hours=1)
    bn.merge_into_archive(archive, bn.parse_feed(v1, s, t1, TABLE), t1)
    stats = bn.merge_into_archive(archive, bn.parse_feed(v2, s, t2, TABLE), t2)
    assert len(archive) == 1 and stats["changed"] == 1
    r = next(iter(archive.values()))
    assert r["title"] == "[정정] 차관 후보자 지명" and r["summary"] == "정정 요약"
    assert r["previous_title"] == "장관 후보자 지명"
    assert r["first_seen_at"] == bn.iso(t1) and r["last_seen_at"] == bn.iso(t2)
    assert r["content_changed_at"] == bn.iso(t2) and r["revision"] == 1
    # 같은 내용이 다시 오면 수정으로 세지 않는다
    stats = bn.merge_into_archive(archive, bn.parse_feed(v2, s, t2, TABLE), t2 + timedelta(hours=1))
    assert stats.get("changed", 0) == 0 and r["revision"] == 1


def test_updated_time_change_is_recorded_and_empty_summary_does_not_erase():
    atom1 = b"""<?xml version="1.0"?><feed xmlns="http://www.w3.org/2005/Atom"><title>a</title>
<entry><title>T</title><link href="https://example.org/x"/><summary>S</summary>
<published>2026-11-16T01:00:00Z</published><updated>2026-11-16T02:00:00Z</updated></entry></feed>"""
    atom2 = atom1.replace(b"<summary>S</summary>", b"").replace(b"02:00:00Z", b"04:00:00Z")
    s = src(region="intl", tz="UTC")
    archive = {}
    bn.merge_into_archive(archive, bn.parse_feed(atom1, s, NOW, TABLE), NOW)
    stats = bn.merge_into_archive(archive, bn.parse_feed(atom2, s, NOW, TABLE), NOW + timedelta(hours=1))
    r = next(iter(archive.values()))
    assert r["updated_at"] == "2026-11-16T04:00:00Z" and stats["time_updated"] == 1
    assert r["summary"] == "S" and r["revision"] == 0


def test_schema1_archive_is_migrated(tmp_path):
    old = {
        "generated_at": "2026-11-16T10:00:00Z",
        "items": [
            {  # 초기 MVP 가 Google 뉴스 헤드라인으로 받은 연합뉴스 기사: ai_ok=True 로 잘못 저장됨
                "id": "old1",
                "site": "x",
                "title": "수능 대책",
                "url": "https://news.google.com/rss/articles/AAA",
                "outlet": "연합뉴스",
                "summary": "",
                "published_at": "2026-11-16T09:00:00Z",
                "region": "kr",
                "section": "news",
                "feed_category": "general",
                "lean": "",
                "ai_ok": True,
                "feed": "Google 뉴스 한국 헤드라인",
                "first_seen_at": "2026-11-16T09:30:00Z",
                "last_seen_at": "2026-11-16T10:00:00Z",
            },
            {  # 발행 시각 없이 수집 시각만 있던 한겨레 기사
                "id": "old2",
                "title": "한겨레 기사",
                "url": "https://www.hani.co.kr/arti/1.html",
                "outlet": "한겨레",
                "summary": "",
                "published_at": None,
                "region": "kr",
                "section": "news",
                "feed_category": "politics",
                "lean": "progressive",
                "ai_ok": False,
                "first_seen_at": "2026-11-16T09:30:00Z",
                "last_seen_at": "2026-11-16T10:00:00Z",
            },
        ],
    }
    p = tmp_path / "archive.json"
    p.write_text(json.dumps(old, ensure_ascii=False), encoding="utf-8")
    arch = bn.load_archive(p, TABLE)
    assert len(arch) == 2
    yna = next(r for r in arch.values() if r["outlet"] == "연합뉴스")
    assert yna["via"] == "google_news" and yna["ai_ok"] is False and yna["time_basis"] == "legacy"
    assert yna["first_seen_at"] == "2026-11-16T09:30:00Z"
    hani = next(r for r in arch.values() if r["outlet"] == "한겨레")
    assert hani["time_basis"] == "unknown" and bn.article_time(hani) is None
    assert hani["feed_ai_deny"] is True  # 스키마 1 의 ai_ok=False 는 피드 금지로 보존
