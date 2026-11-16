"""반복 보도 보정, 언론사 소재별 품질 검사·7일 중앙값 기준, 스냅샷 복구."""

import json
import shutil
from datetime import datetime, timedelta, timezone
from pathlib import Path

from scripts import build_news as bn
from scripts import quality_gate as qg
from scripts import state_snapshot as snap
from tests.test_step2_stability import CONF, feed_server, make_feeds, run, small_config, write_opml  # noqa: F401

NOW = datetime(2026, 11, 16, 12, 0, tzinfo=timezone.utc)


def rec(title, outlet, hours_ago=1, summary=""):
    t = bn.iso(NOW - timedelta(hours=hours_ago))
    u = f"https://{abs(hash(outlet))}.example/{abs(hash(title))}"
    return {
        "id": bn.article_id("kr", outlet, u), "title": title, "url": u, "outlet": outlet, "summary": summary,
        "published_at": t, "updated_at": None, "time_basis": "published", "first_seen_at": t, "last_seen_at": t,
        "region": "kr", "section": "news", "feed_category": "general", "lean": "", "via": "direct",
        "feed_ai_deny": False, "ai_ok": False,
    }


# ---------------------------------------------------------------- 같은 보도 계열


def test_identical_wire_copies_count_once_rewrites_count_separately():
    members = [
        rec("[속보] 정부, 수능 스마트기기 반납·공공 와이파이 차단 추진", "연합뉴스"),
        rec("[속보] 정부, 수능 스마트기기 반납·공공 와이파이 차단 추진", "JTBC"),
        rec("정부, 수능 스마트기기 반납·공공 와이파이 차단 추진(종합)", "SBS"),
        rec("정부, 수능일 ‘시험장 공공 와이파이’ 차단 추진…스마트기기 반납도", "경향신문"),
        rec("정부, 수능 시험장 스마트기기 반납·공공 와이파이 차단 추진", "한겨레"),
    ]
    s = bn.build_story(members, NOW)
    assert s["outlet_count"] == 5  # 화면 표시는 실제 언론사 수
    assert s["report_lineage_count"] == 3  # 점수는 보도 계열 수
    assert s["copy_groups"] == [["JTBC", "SBS", "연합뉴스"]]
    independent = bn.build_story(members[3:] + [rec("수능 와이파이 차단, 학부모 반응 엇갈려", "조선일보"),
                                                rec("수능 시험장 와이파이 차단 실효성 논란", "MBC")], NOW)
    assert s["score"] < independent["score"]  # 5곳이지만 3계열 < 4곳 4계열


def test_identical_summary_opening_marks_same_lineage():
    body = "(서울=연합뉴스) 정부가 19일 치러지는 대학수학능력시험에서 시험장 공공 와이파이를 차단하기로 했다."
    a = rec("수능 와이파이 차단", "매체A", summary=body)
    b = rec("시험장 와이파이 끈다", "매체B", summary=body)
    assert bn.report_lineages([a, b]) == [["매체A", "매체B"]]


# ---------------------------------------------------------------- 품질 검사


def payload(**kw):
    c = {"news_articles": 1700, "kr_candidates": 1000, "intl_candidates": 300, "sources_ok": 40, "sources_active": 42,
         "top_kr": 24, "top_intl": 16, "top_quota_kr": 24, "top_quota_intl": 16,
         "sources_ok_kr": 25, "sources_active_kr": 25, "sources_ok_intl": 11, "sources_active_intl": 11}
    c.update(kw)
    return {"generated_at": "2026-11-16T12:00:00Z", "counts": c}


STATUS = {"summary": {"failed": [], "empty": [], "stale": []}, "sources": []}


def test_all_foreign_outlets_failing_is_caught_even_if_totals_look_fine():
    # 해외 언론 11곳 중 1곳만 정상. 전체 정상 비율 26/36=72% 라 기존 검사로는 경고뿐이었다.
    r = qg.evaluate(payload(sources_ok=26, sources_active=36, sources_ok_intl=1), STATUS, None, CONF)
    assert r["passed"] is False and any("해외 언론 소스 정상 비율" in x for x in r["fails"])
    r = qg.evaluate(payload(sources_ok_intl=7), STATUS, None, CONF)
    assert r["passed"] is True and any("해외 언론 소스" in x for x in r["warnings"])


def test_baseline_is_seven_day_median_when_enough_history(tmp_path):
    state = tmp_path / "state"
    state.mkdir()
    runs = []
    for i, n in enumerate([1700, 1650, 1750, 300, 1680]):  # 한 번의 이상치(300)
        runs.append(payload(news_articles=n)["counts"] | {"generated_at": bn.iso(NOW - timedelta(hours=i + 1))})
    runs.append(payload(news_articles=10)["counts"] | {"generated_at": bn.iso(NOW - timedelta(days=9))})  # 7일 밖
    (state / "metrics_history.json").write_text(json.dumps({"runs": runs}), encoding="utf-8")
    base, src = qg.load_baseline(state, "", bn.iso(NOW))
    assert src.startswith("median_7d(5") and base["news_articles"] == 1680
    r = qg.evaluate(payload(news_articles=500), STATUS, base, CONF)
    assert r["passed"] is False and any("기사 수 급감" in x for x in r["fails"])


# ---------------------------------------------------------------- 스냅샷


def make_data(d: Path):
    (d / "state").mkdir(parents=True)
    (d / "archive.json").write_text(json.dumps({"schema": 2, "started_at": "2026-11-01T00:00:00Z", "items": []}))
    (d / "state" / "story_registry.json").write_text(json.dumps({"articles": {"a": "s_1"}, "stories": {}}))
    (d / "state" / "theme_candidates.json").write_text(json.dumps({"started_at": "2026-10-20T00:00:00Z", "items": []}))


def test_snapshot_roundtrip_and_integrity_check(tmp_path):
    src, out = tmp_path / "data", tmp_path / "snap"
    make_data(src)
    m = snap.create(src, out)
    assert set(m["files"]) == {"archive.json", "state/story_registry.json", "state/theme_candidates.json"}
    fresh = tmp_path / "fresh"
    info = snap.restore(out, fresh)
    assert info["ok"] and sorted(info["restored_files"]) == sorted(m["files"])
    assert json.loads((fresh / "state" / "restore_info.json").read_text())["from"] == "snapshot"
    # 캐시에서 이미 온 파일은 덮어쓰지 않는다
    other = tmp_path / "partial"
    (other / "state").mkdir(parents=True)
    (other / "archive.json").write_text("{}")
    info = snap.restore(out, other)
    assert "archive.json" in info["kept_existing"] and (other / "archive.json").read_text() == "{}"
    # 하나라도 손상되면 아무것도 복구하지 않는다
    (out / "state" / "story_registry.json").write_text('{"articles": {}}')
    bad = tmp_path / "bad"
    info = snap.restore(out, bad)
    assert info["ok"] is False and any("무결성" in p for p in info["problems"])
    assert not (bad / "archive.json").exists()


def test_end_to_end_cache_loss_restores_from_snapshot(feed_server, tmp_path):  # noqa: F811
    root, base = feed_server
    opml = tmp_path / "s.opml"
    write_opml(opml, make_feeds(root, base))
    conf = tmp_path / "q.json"
    small_config(conf)
    data = tmp_path / "data"
    args = ["--sources", str(opml), "--config", str(conf), "--top", "10"]
    assert run("build_news.py", "--output-dir", str(data), *args).returncode == 0
    assert run("quality_gate.py", "--data-dir", str(data), "--config", str(conf)).returncode == 0
    first = json.loads((data / "news.json").read_text(encoding="utf-8"))
    snapshot = tmp_path / "snapshot"
    assert run("state_snapshot.py", "create", "--data-dir", str(data), "--out", str(snapshot)).returncode == 0

    # 캐시가 통째로 사라진 실행: 빈 data 에 스냅샷을 복구한 뒤 수집
    fresh = tmp_path / "fresh"
    r = run("state_snapshot.py", "restore", "--snapshot", str(snapshot), "--data-dir", str(fresh))
    assert r.returncode == 0, r.stdout
    assert run("build_news.py", "--output-dir", str(fresh), *args).returncode == 0
    second = json.loads((fresh / "news.json").read_text(encoding="utf-8"))
    cov = second["coverage"]
    assert cov["restored_from"] == "snapshot" and cov["snapshot_created_at"]
    assert cov["archive_started_at"] == first["coverage"]["archive_started_at"]  # 보관 시작 시각 유지
    assert second["counts"]["story_ids"].get("kept", 0) > 0  # 사건 ID 이어받음
    assert {s["id"] for s in second["news"]} & {s["id"] for s in first["news"]}
    assert not (fresh / "state" / "restore_info.json").exists()  # 한 번만 보고
    # 다음 실행은 캐시(같은 디렉터리)에서 이어받은 것으로 보고
    assert run("build_news.py", "--output-dir", str(fresh), *args).returncode == 0
    third = json.loads((fresh / "news.json").read_text(encoding="utf-8"))
    assert third["coverage"]["restored_from"] == "cache"

    # 스냅샷도 없으면 '새로 수집'으로 분명히 표시
    empty = tmp_path / "empty"
    assert run("build_news.py", "--output-dir", str(empty), *args).returncode == 0
    assert json.loads((empty / "news.json").read_text(encoding="utf-8"))["coverage"]["restored_from"] == "none"
    shutil.rmtree(empty)
