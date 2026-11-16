"""3단계: 주요 뉴스 선정, 사건 묶기 시간 폭·오병합 방지, 사건 ID 안정성, 관련 이슈, 대상 지역."""

from collections import Counter
from datetime import datetime, timedelta, timezone

from scripts import build_news as bn

NOW = datetime(2026, 11, 16, 12, 0, tzinfo=timezone.utc)


def rec(title, outlet, hours_ago, region="kr", summary="", via="direct", category="general", url=None):
    t = bn.iso(NOW - timedelta(hours=hours_ago))
    u = url or f"https://{outlet}.example/{abs(hash((title, hours_ago)))}"
    return {
        "id": bn.article_id(region, outlet, u),
        "title": title,
        "url": u,
        "outlet": outlet,
        "summary": summary,
        "published_at": t,
        "updated_at": None,
        "time_basis": "published",
        "first_seen_at": t,
        "last_seen_at": bn.iso(NOW),
        "region": region,
        "section": "news",
        "feed_category": category,
        "lean": "",
        "via": via,
        "feed_ai_deny": False,
        "ai_ok": False,
    }


# ---------------------------------------------------------------- A1. 선정 기사는 공개 목록에 항상 포함


def test_selected_ids_are_always_published_even_beyond_600():
    archive = {}
    # 국내 단독 기사 700건(점수 높음) + 해외 기사(점수 낮음): 해외 주요 뉴스가 600위 밖
    for i in range(700):
        r = rec(f"국내 고유 사건 제목 {i:04d} 가나다라{i}", f"국내{i % 30}", hours_ago=0.5)
        r["title"] = f"{bn.hashlib.sha1(str(i).encode()).hexdigest()[:12]} 소식"
        archive[r["id"]] = r
    for i in range(20):
        r = rec(f"{bn.hashlib.sha1(('i' + str(i)).encode()).hexdigest()[:12]} report", f"intl{i}", 30, region="intl")
        archive[r["id"]] = r
    out = bn.compose(archive, NOW, 40, {"articles": {}, "stories": {}})
    pub = {s["id"] for s in out["published_news"]}
    assert {s["id"] for s in out["top"]} <= pub
    assert len(out["published_news"]) > bn.PUBLISHED_NEWS_LIMIT  # 600 + 600위 밖 주요 뉴스


# ---------------------------------------------------------------- A3. 시간 폭은 최초 기사 기준


def test_cluster_span_measured_from_first_article_and_follow_up_linked_as_issue():
    title = "대법원 한덕수 내란 혐의 상고심 선고 기일 지정"
    recs = [rec(title, "연합뉴스", 40), rec(title + " 종합", "SBS", 22), rec(title + " 속보", "경향신문", 10)]
    groups, _, idf = bn.cluster_detail(recs, [])
    # 40h→22h 는 18시간(묶임), 40h→10h 는 30시간 > 24시간 → 22h 기사와 12시간 차이여도 묶지 않는다
    assert [len(g["members"]) for g in groups] == [2, 1]
    assert groups[1]["follow_of"] == 0
    issue_of = bn.link_issues(groups, idf)
    assert issue_of[0] == issue_of[1]  # 장기 후속 보도는 관련 이슈로 연결


# ---------------------------------------------------------------- B4. 의미 충돌이면 합치지 않음


def test_conflicting_entities_dates_and_directions_block_merge():
    assert bn.conflict_reason("DB증권, 3분기 영업익 116억원", "오리온, 3분기 영업익 1509억원").startswith("subject")
    assert bn.conflict_reason("대법, 19일 한덕수 선고", "대법, 17일 한덕수 선고") == "date"
    assert bn.conflict_reason("기준금리 0.25%p 인상", "기준금리 0.25%p 인하").startswith("opposite")
    # 같은 사건의 다른 표기는 통과
    assert bn.conflict_reason("李대통령, 마닐라 도착", "이 대통령, 다자외교 출국") is None
    assert bn.conflict_reason("이 대통령, 아세안에 제안", "李, 韓-아세안 제안") is None  # 호환 한자 李
    assert bn.conflict_reason("공정위, 굽네치킨 일가 조사", "굽네치킨 창업주, 부당지원 의혹") is None
    assert bn.conflict_reason("카카오뱅크, 애플페이 도입", "애플페이 현대카드 독점 깨졌다…카카오뱅크") is None
    assert bn.conflict_reason("코스닥도 4.86%↓", "코스닥도 4.8%↓") is None  # 반올림 차이는 다른 사건이 아님


def test_earnings_titles_with_shared_template_stay_separate():
    recs = [
        rec("오뚜기, 3분기 영업이익 363억원…작년 동기 대비 34.5% 감소", "연합뉴스", 3),
        rec("대상, 3분기 영업이익 413억원…작년 동기 대비 12.9% 감소", "연합뉴스", 2),
    ]
    stats = Counter()
    groups, _, _ = bn.cluster_detail(recs, [], stats)
    assert len(groups) == 2


# ---------------------------------------------------------------- A4. 사건 ID


def story_ids(archive, registry, now=NOW):
    out = bn.compose(archive, now, 40, registry)
    return out["news_stories"], out


def test_story_id_survives_representative_change():
    a = rec("정부 수능 스마트기기 반납 공공 와이파이 차단 추진", "SBS", 5)
    archive = {a["id"]: a}
    registry = {"articles": {}, "stories": {}}
    s1, _ = story_ids(archive, registry)
    sid = s1[0]["id"]
    assert sid.startswith("s_") and s1[0]["rep_article_id"] == a["id"]
    # 요약이 있는 직접 수집 기사가 나중에 들어와 대표가 바뀐다
    b = rec("정부 수능 스마트기기 반납 공공 와이파이 차단 추진 종합", "연합뉴스", 1, summary="가" * 150)
    archive[b["id"]] = b
    s2, _ = story_ids(archive, registry, NOW + timedelta(minutes=60))
    assert len(s2) == 1 and s2[0]["rep_article_id"] == b["id"]
    assert s2[0]["id"] == sid  # 대표가 바뀌어도 사건 ID 유지
    assert s2[0]["id"] != b["id"] and s2[0]["id"] != a["id"]  # 기사 ID 와 분리


def test_story_merge_and_split_lineage():
    reg = {"articles": {"a1": "s_old1", "a2": "s_old1", "a3": "s_old2"}, "stories": {
        "s_old1": {"created_at": "2026-11-15T00:00:00Z"}, "s_old2": {"created_at": "2026-11-15T01:00:00Z"}}}

    def st(*ids):
        return {"id": "s_tmp" + "".join(ids), "articles": [{"id": i} for i in ids], "score": 1.0}

    merged = [st("a1", "a2", "a3")]
    stats = bn.assign_story_ids(merged, reg, "2026-11-16T00:00:00Z")
    assert merged[0]["id"] == "s_old1" and merged[0]["lineage"]["merged_from"] == ["s_old2"]
    assert reg["stories"]["s_old2"]["merged_into"] == "s_old1" and stats["merged"] == 1
    # 이제 a3 가 다시 떨어져 나간다 → 다수(a1,a2) 쪽이 ID 유지, a3 묶음은 새 ID + split_from
    split = [st("a1", "a2"), st("a3")]
    bn.assign_story_ids(split, reg, "2026-11-16T01:00:00Z")
    assert split[0]["id"] == "s_old1"
    assert split[1]["id"] != "s_old1" and split[1]["lineage"]["split_from"] == "s_old1"


# ---------------------------------------------------------------- B2. 관련 이슈


def test_related_events_are_separate_stories_under_one_issue():
    recs = [
        rec("정부, 수능 스마트기기 반납·공공 와이파이 차단 추진", "연합뉴스", 6),
        rec("정부, 수능 시험장 스마트기기 반납·공공 와이파이 차단 추진", "한겨레", 6),
        rec("AI 부정행위 우려에…수능 시험장 공공 와이파이 차단", "SBS", 5),
        rec("AI 부정행위 우려에 수능 시험장 공공 와이파이 끈다", "MBC", 5),
        rec("韓총리 수능 부정행위 철저 대비…범정부 대응팀 구성", "연합뉴스", 4),
        rec("한 총리, 수능 AI 부정행위 범정부 대응팀 구성", "경향신문", 4),
        rec("서울 집값 상승폭 석 달째 둔화", "조선일보", 3),
        rec("서울 집값 상승폭 석달째 둔화…전셋값 5억", "SBS", 3),
    ]
    groups, _, idf = bn.cluster_detail(recs, [])
    titles = [g["members"][0]["title"] for g in groups]
    wifi = next(i for i, t in enumerate(titles) if "스마트기기" in t)
    team = next(i for i, t in enumerate(titles) if "대응팀" in t)
    house = next(i for i, t in enumerate(titles) if "집값" in t)
    assert wifi != team  # 각각의 사건
    issue_of = bn.link_issues(groups, idf)
    assert issue_of[wifi] == issue_of[team]  # 공통 이슈로 연결 (중간 기사를 거쳐)
    assert issue_of[house] != issue_of[wifi]


def test_single_outlet_story_cannot_bridge_two_issues():
    recs = [
        rec("젠슨 황 엔비디아 CEO 복귀 논의", "연합뉴스", 5), rec("젠슨 황 엔비디아 CEO 복귀 타진", "SBS", 5),
        rec("서울 아파트 집값 상승폭 둔화", "조선일보", 5), rec("서울 아파트 집값 상승폭 석달째 둔화", "SBS", 5),
        # 두 주제의 낱말을 다 가진 단독 해설
        rec("젠슨 황 엔비디아 복귀 논의와 서울 아파트 집값 상승폭 해설", "경향신문", 4),
    ]
    groups, _, idf = bn.cluster_detail(recs, [])
    issue_of = bn.link_issues(groups, idf)
    t = {g["members"][0]["outlet"] + g["members"][0]["title"][:6]: issue_of[i] for i, g in enumerate(groups)}
    jensen = next(v for k, v in t.items() if "젠슨" in k and "경향" not in k)
    house = next(v for k, v in t.items() if "서울" in k)
    assert jensen != house


# ---------------------------------------------------------------- B1. 대상 지역


def test_target_region_separate_from_outlet_region():
    s = bn.build_story([rec("李·다카이치 17일 필리핀서 정상회담", "연합뉴스", 2, category="world")], NOW)
    assert (s["source_region"], s["target_region"], s["region"]) == ("kr", "both", "kr")
    s = bn.build_story([rec("젠슨 황, 엔비디아 복귀 논의…백악관 설득", "연합뉴스", 2, category="world")], NOW)
    assert (s["source_region"], s["target_region"], s["region"]) == ("kr", "intl", "intl")
    s = bn.build_story([rec("South Korea's top scorer admits using AI", "BBC", 2, region="intl")], NOW)
    assert (s["source_region"], s["target_region"], s["region"]) == ("intl", "kr", "kr")
    s = bn.build_story([rec("환율 1,450원…달러 강세", "SBS", 2)], NOW)
    assert s["target_region"] == "kr" and s["target_basis"] == "outlet_fallback"  # 신호 없음 → 언론사 소재


# ---------------------------------------------------------------- B3. 대표 기사


def test_representative_uses_observable_basis_not_fixed_outlet_rank():
    # 연합뉴스(고정 순위 1위)는 요약 없는 [속보], 조선일보는 요약 있는 종합 기사
    members = [
        rec("[속보] 정부, 수능 와이파이 차단 추진", "연합뉴스", 2),
        rec("정부, 수능 와이파이 차단 추진(종합)", "조선일보", 1, summary="정부가 오는 19일 수능 시험장에서 " * 5),
        rec("정부, 수능 와이파이 차단 추진", "Google경유", 1, via="google_news"),
    ]
    s = bn.build_story(members, NOW)
    assert s["outlet"] == "조선일보"
    basis = s["rep_basis"]
    assert basis["observed"]["direct_link"] is True and basis["observed"]["summary_len"] >= 100
    assert "consolidated" in basis["estimated"]  # (종합) 표기는 '추정'으로만 기록
    assert "추정" in basis["estimated"]["consolidated"]
