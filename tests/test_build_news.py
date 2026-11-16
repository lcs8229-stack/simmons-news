from datetime import datetime, timedelta, timezone
from pathlib import Path

from scripts import build_news as bn

NOW = datetime(2026, 11, 16, 12, 0, tzinfo=timezone.utc)


def rec(title, outlet, region="kr", hours_ago=1, category="general", section="news"):
    t = (NOW - timedelta(hours=hours_ago)).isoformat()
    return {
        "id": f"{outlet}:{title}",
        "title": title,
        "url": f"https://example.com/{abs(hash((outlet, title)))}",
        "outlet": outlet,
        "summary": "",
        "published_at": t,
        "first_seen_at": t,
        "region": region,
        "section": section,
        "feed_category": category,
        "lean": "",
        "ai_ok": True,
    }


def test_split_gnews_title():
    assert bn.split_gnews_title("정부, 수능 와이파이 차단 추진 - 중앙일보") == ("정부, 수능 와이파이 차단 추진", "중앙일보")
    assert bn.split_gnews_title("No outlet suffix here") == ("No outlet suffix here", "")


def test_same_event_from_different_outlets_is_clustered():
    recs = [
        rec("[속보] 정부, 수능 스마트기기 반납·공공 와이파이 차단 추진", "연합뉴스"),
        rec("정부, 수능 시험장 스마트기기 반납·공공 와이파이 차단 추진", "한겨레"),
        rec("정부, 수능일 ‘시험장 공공 와이파이’ 차단 추진…스마트기기 반납도", "경향신문"),
        rec("코스피 3.59% 급락, 5,100선으로 후퇴", "SBS"),
    ]
    groups = bn.cluster(recs)
    sizes = sorted(len(g) for g in groups)
    assert sizes == [1, 3]


def test_earnings_template_titles_are_not_chained():
    recs = [
        rec("DB증권, 3분기 영업익 116억원…전년比 74%↓", "연합뉴스"),
        rec("오리온, 3분기 영업익 1509억원… 전년比 9.4% 증가", "조선일보"),
        rec("매일유업, 3분기 영업익 222억원… 전년比 11.6%↑", "조선일보"),
        rec("대상, 3분기 영업익 413억원… 전년比 12.9%↓", "조선일보"),
    ]
    assert len(bn.cluster(recs)) == 4


def test_regions_never_mix_in_a_cluster():
    recs = [
        rec("Jordan blocks Telegram app over security", "Reuters", region="intl"),
        rec("Jordan blocks Telegram app over security concerns", "연합뉴스", region="kr"),
    ]
    assert len(bn.cluster(recs)) == 2


def test_classify_uses_feed_category_then_keywords():
    assert bn.classify(rec("아무 제목", "x", category="economy")) == "economy"
    assert bn.classify(rec("국회 법사위, 예산안 여당 주도 의결", "x")) == "politics"
    assert bn.classify(rec("코스피 급락…환율 1,450원 돌파", "x")) == "economy"
    assert bn.classify(rec("김정은, 평양서 열병식 참관", "x")) == "northkorea"


def test_low_value_filters_program_listings_and_notices():
    assert bn.is_low_value(rec("2026년 11월 16일 19시뉴스", "MBC"))
    assert bn.is_low_value(rec("[인사] 산업통상부", "연합뉴스"))
    assert not bn.is_low_value(rec("대법, 19일 선고", "연합뉴스"))


def story(region, category, score, outlet_count=1):
    return {
        "id": f"{region}-{category}-{score}",
        "region": region,
        "section": "news",
        "category": category,
        "score": score,
        "low_value": False,
        "outlet_count": outlet_count,
    }


def test_select_top_keeps_60_40_split_and_category_cap():
    stories = [story("kr", "politics", 10 - i * 0.1) for i in range(20)]
    stories += [story("kr", "economy", 5 - i * 0.1) for i in range(20)]
    stories += [story("intl", "world", 4 - i * 0.1) for i in range(20)]
    stories += [story("intl", "economy", 3 - i * 0.1) for i in range(20)]
    top = bn.select_top(stories, total=10)
    kr = [s for s in top if s["region"] == "kr"]
    intl = [s for s in top if s["region"] == "intl"]
    assert len(kr) == 6 and len(intl) == 4
    # 국내 정치는 상한(6 * 0.4 → 3)에 걸려 경제가 나머지를 채운다
    assert sum(1 for s in kr if s["category"] == "politics") == 3
    # 해외 '국제'는 상한 예외
    assert all(s["category"] == "world" for s in intl)


def test_score_rewards_more_outlets():
    one = bn.build_story([rec("단독 보도 하나", "한겨레")], NOW)
    many = bn.build_story(
        [rec("같은 사건 보도", o) for o in ["연합뉴스", "SBS", "조선일보", "한겨레"]], NOW
    )
    assert many["score"] > one["score"] * 2
    assert many["outlet"] == "연합뉴스"  # 통신사 우선 대표 기사


def test_sources_opml_is_valid_and_balanced():
    sources = bn.load_sources(Path(__file__).resolve().parents[1] / "feeds" / "sources.opml")
    assert len(sources) >= 40
    news = [s for s in sources if s["section"] == "news"]
    assert {s["region"] for s in news} == {"kr", "intl"}
    assert any(s["section"] == "science" for s in sources)
    # 연합뉴스·한겨레는 RSS 약관('AI 학습 및 활용 금지') 때문에 피드 단위로도 금지
    assert all(s["feed_ai_deny"] for s in sources if s["outlet"] in ("연합뉴스", "한겨레"))
    assert all(s["via"] == ("google_news" if s["gnews"] else "direct") for s in sources)
