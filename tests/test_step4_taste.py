"""4단계: 취향 프로필 판단·후보 보관·독립 선정."""

from datetime import datetime, timedelta, timezone

from scripts import build_news as bn
from scripts import taste

NOW = datetime(2026, 11, 16, 12, 0, tzinfo=timezone.utc)
PROF = taste.load_profile()


def rec(title, outlet="조선일보", days_ago=1.0, summary="", region="kr", section="news", category="general", lean=""):
    t = bn.iso(NOW - timedelta(days=days_ago))
    url = f"https://{abs(hash(outlet))}.example/{abs(hash((title, days_ago)))}"
    return {
        "id": bn.article_id(region, outlet, url), "title": title, "url": url, "outlet": outlet, "summary": summary,
        "published_at": t, "updated_at": None, "time_basis": "published", "region": region, "section": section,
        "feed": outlet, "feed_category": category, "via": "direct", "first_seen_at": t, "last_seen_at": t, "lean": lean,
    }


def ev(r):
    return taste.evaluate(PROF, r, NOW, bn.parse_iso(r["published_at"]))


def select(cands):
    return taste.select(PROF, cands, NOW, bn.parse_iso, lambda rs: bn.cluster_with_undated(rs, [])[0], bn.title_tokens)


# ---------------------------------------------------------------- 판단


def test_single_keyword_mention_is_not_interest():
    e = ev(rec("구청, 주민 대상 인공지능 교실 개최"))
    assert e["best_topic"] is None  # '인공지능' 한 번만으로는 관심 글이 아니다
    e = ev(rec("AI 투자 급증이 데이터센터 전력망에 주는 부담", summary="전력 수요가 송전망 병목을 키운다"))
    assert e["best_topic"] == "ai_infra" and "전력망" in e["relevance"]["ai_infra"]["concepts"]


def test_connection_across_topics_is_recognized():
    e = ev(rec("AI 데이터센터 투자 붐과 금리·금융시장 위험", summary="연준이 금융 안정 보고서에서 경고했다"))
    assert e["connection"] and {"ai_infra", "econ_policy"} <= set(e["topics"])


def test_critical_analysis_is_kept_not_penalized():
    e = ev(rec("비트코인 가치저장 논리의 한계와 위험", outlet="Aeon", region="intl",
               summary="Why the store of value thesis may fail: data from past cycles " * 2))
    assert e["best_topic"] == "crypto" and "critical" in e["nature"]["signals"] and not e["penalties"]


def test_specialist_outlet_counts_as_one_concept_only():
    alone = ev(rec("Glamsterdam", outlet="Ethereum Foundation", region="intl"))
    assert alone["best_topic"] is None  # 전문 매체라는 사실만으로는 관심 글 아님
    with_concept = ev(rec("EIP-8037: rollup data costs", outlet="Ethereum Foundation", region="intl"))
    assert with_concept["best_topic"] == "crypto"


def test_penalties_for_price_moves_promo_and_listings():
    assert "stock" in ev(rec("비트코인 급락, 9만 달러 붕괴…가상자산 시장 흔들"))["penalties"]
    assert "stock" in ev(rec("XRP crashes to its lowest since 2024 as crypto money leaves", region="intl"))["penalties"]
    assert "promo" in ev(rec("○○전자, ESG 평가 A등급 획득"))["penalties"]
    assert "listing" in ev(rec("Bitcoin Optech Newsletter #431", outlet="Bitcoin Optech", region="intl"))["penalties"]


def test_depth_unknown_when_no_material():
    e = ev(rec("유럽의회, 제재 결의"))
    assert e["nature"]["depth"] == "unknown" and e["nature"]["score"] == PROF["nature"]["unknown_depth_score"]


def test_political_lean_is_not_used():
    a = ev(rec("민주주의 제도와 외교 안보 동맹의 재편", lean="progressive"))
    b = ev(rec("민주주의 제도와 외교 안보 동맹의 재편", lean="conservative"))
    assert a["interest_score"] == b["interest_score"]


# ---------------------------------------------------------------- 보관


def test_store_keeps_only_window_and_does_not_invent_history():
    store = {"started_at": None, "items": {}}
    recs = [
        rec("AI 데이터센터 전력망 병목 분석", days_ago=3),
        rec("AI 데이터센터 전력망 병목 오래된 분석", days_ago=40),  # 28일 밖
        rec("구청 행사 안내", days_ago=1),  # 관심 개념 없음 → 보관 안 함
    ]
    stats = taste.update_store(store, recs, PROF, NOW, bn.iso(NOW), bn.parse_iso)
    assert stats["total"] == 1 and store["started_at"] == bn.iso(NOW)
    payload = taste.build_payload(PROF, select(list(store["items"].values())), store, bn.iso(NOW))
    # 실제로 확보한 가장 오래된 후보만 보고한다 (28일 전부터 있다고 가정하지 않음)
    assert payload["coverage"]["oldest_candidate"] == recs[0]["published_at"]


# ---------------------------------------------------------------- 선정


def test_no_forced_fill_and_shortfall_reported():
    res = select([rec("AI 데이터센터 전력망 병목과 송전 투자 구조 분석", outlet="IEEE Spectrum", region="intl",
                      summary="Why grid interconnection queues limit AI data center growth, with data " * 2)])
    assert len(res["picked"]) == 1
    assert res["shortfall"] == {"interest": 5, "broaden": 4}
    assert res["topic_stats"]["crypto"]["short"] is True


def test_diversity_one_per_topic_first_and_outlet_cap():
    long = "Analysis of how the structure works, with data and case study. " * 3
    cands = [rec(f"AI 반도체 HBM 패키징 병목 분석 {i}", outlet="디일렉", summary=long, days_ago=0.5 + i * 0.1)
             for i in range(5)]
    cands.append(rec("이더리움 롤업 수수료 구조와 가치 귀속 분석", outlet="Ethereum Foundation", region="intl", summary=long))
    res = select(cands)
    interest = [p for p in res["picked"] if p["kind"] == "interest"]
    topics = [p["area"] for p in interest]
    assert "crypto" in topics  # 점수가 낮아도 다른 주제가 먼저 한 자리를 얻는다
    assert sum(1 for p in res["picked"] if p["rec"]["outlet"] == "디일렉") <= PROF["diversity"]["max_per_outlet"]


def test_same_story_or_republished_title_picked_once():
    long = "Why grid capacity constrains data centers: data and analysis. " * 3
    a = rec("AI 데이터센터 전력망 병목 심층 분석", outlet="조선일보", summary=long)
    b = rec("AI 데이터센터 전력망 병목 심층 분석", outlet="중앙일보", summary=long)  # 재게시
    res = select([a, b])
    assert len(res["picked"]) == 1 and res["skipped"].get("same_story", 0) + res["skipped"].get("similar_title", 0) >= 1


def test_region_quota_not_enforced():
    long = "Analysis explains why the structure matters, with data. " * 3
    cands = [
        rec("Ethereum rollup fees and value accrual explained", outlet="Ethereum Foundation", region="intl", summary=long),
        rec("Data center power demand strains the grid: analysis", outlet="Data Center Dynamics", region="intl", summary=long),
        rec("Inflation expectations and monetary policy research", outlet="VoxEU", region="intl", summary=long),
    ]
    res = select(cands)
    assert len(res["picked"]) == 3 and all(p["rec"]["region"] == "intl" for p in res["picked"])
