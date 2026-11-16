"""2단계: 소스 상태 세분화, 배포 전 품질 검사, 캐시 유실 복구."""

import json
import subprocess
import sys
import threading
from datetime import datetime, timedelta, timezone
from functools import partial
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import pytest

from scripts import build_news as bn
from scripts import quality_gate as qg
from scripts.outlets import load_policy
from scripts.source_health import DEFAULT_CADENCE_HOURS, update_history

ROOT = Path(__file__).resolve().parents[1]
NOW = datetime(2026, 11, 16, 12, 0, tzinfo=timezone.utc)
TABLE = load_policy()
CONF = json.loads((ROOT / "config" / "quality.json").read_text(encoding="utf-8"))


def src(**kw):
    base = dict(
        name="피드", url="https://example.com/rss", outlet="조선일보", region="kr", section="news",
        category="general", lean="", gnews=False, via="direct", feed_ai_deny=False, tz="Asia/Seoul",
        cadence="daily", enabled=True, disabled_reason="",
    )
    base.update(kw)
    if base["gnews"]:
        base["via"] = "google_news"
    return base


class FakeResp:
    def __init__(self, status=200, body=b"", ctype="application/rss+xml"):
        self.status_code = status
        self.content = body
        self.headers = {"content-type": ctype}

    def raise_for_status(self):
        if self.status_code >= 400:
            raise bn.requests.HTTPError(f"{self.status_code} Client Error")


def rss_items(*pubdates):
    items = "".join(
        f"<item><title>기사 {i}</title><link>https://www.chosun.com/a/{i}/</link>"
        + (f"<pubDate>{d}</pubDate>" if d else "")
        + "</item>"
        for i, d in enumerate(pubdates)
    )
    return f'<?xml version="1.0"?><rss version="2.0"><channel><title>t</title>{items}</channel></rss>'.encode()


def run_source(monkeypatch, resp, **kw):
    monkeypatch.setattr(bn.requests, "get", lambda *a, **k: resp)
    monkeypatch.setattr(bn.time, "sleep", lambda *_: None)
    return bn.fetch_source(src(**kw), NOW, TABLE, DEFAULT_CADENCE_HOURS)[1]


# ---------------------------------------------------------------- 1. 소스 상태


def test_source_states_are_distinguished(monkeypatch):
    recent = "Mon, 16 Nov 2026 10:00:00 +0000"
    s = run_source(monkeypatch, FakeResp(body=rss_items(recent)))
    assert (s["state"], s["http_ok"], s["parse_ok"], s["valid_count"], s["recent_count"]) == ("ok", True, True, 1, 1)
    assert s["ok"] is True

    s = run_source(monkeypatch, FakeResp(status=403))
    assert s["state"] == "http_error" and s["http_status"] == 403 and s["ok"] is False

    s = run_source(monkeypatch, FakeResp(body=b"<html><body>Service Unavailable</body></html>", ctype="text/html"))
    assert s["state"] == "parse_error" and s["http_ok"] is True and s["ok"] is False

    s = run_source(monkeypatch, FakeResp(body=rss_items()))
    assert s["state"] == "empty" and s["entry_count"] == 0 and s["ok"] is False  # 0건은 정상이 아니다

    s = run_source(monkeypatch, FakeResp(body=rss_items(None, None)))
    assert s["state"] == "undated" and s["valid_count"] == 2 and s["dated_count"] == 0


def test_filtered_all_reproduces_former_ap_zero_case(monkeypatch):
    # Google 뉴스 site: 검색인데 다른 언론사 기사만 들어오면 항목은 있지만 유효 기사 0
    body = (
        '<?xml version="1.0"?><rss version="2.0"><channel><title>g</title>'
        "<item><title>Story - Reuters</title><link>https://news.google.com/rss/articles/X</link>"
        "<pubDate>Mon, 16 Nov 2026 10:00:00 GMT</pubDate>"
        '<source url="https://www.reuters.com">Reuters</source></item></channel></rss>'
    ).encode()
    s = run_source(monkeypatch, FakeResp(body=body), outlet="AP", region="intl", gnews=True, tz="UTC")
    assert s["state"] == "filtered_all" and s["entry_count"] == 1 and s["valid_count"] == 0
    # 'AP News' 표기는 1단계 정규화로 AP 로 인식되어 정상 (이전 0건 원인)
    body_ap = body.replace(b"Reuters</source>", b"AP News</source>").replace(b"reuters.com", b"apnews.com")
    s = run_source(monkeypatch, FakeResp(body=body_ap), outlet="AP", region="intl", gnews=True, tz="UTC")
    assert s["state"] == "ok" and s["valid_count"] == 1


def test_stale_threshold_depends_on_cadence(monkeypatch):
    ten_days_ago = (NOW - timedelta(days=10)).strftime("%a, %d %b %Y %H:%M:%S +0000")
    daily = run_source(monkeypatch, FakeResp(body=rss_items(ten_days_ago)), cadence="daily")
    monthly = run_source(monkeypatch, FakeResp(body=rss_items(ten_days_ago)), cadence="monthly")
    assert daily["state"] == "stale" and daily["recent_count"] == 0
    assert monthly["state"] == "ok" and monthly["recent_count"] == 1


def test_history_counts_consecutive_problems_and_keeps_last_success():
    t = [f"2026-11-16T{h:02d}:17:00Z" for h in range(5)]
    h = update_history(None, {"state": "ok", "http_ok": True}, t[0])
    h = update_history(h, {"state": "http_error", "http_ok": False}, t[1])
    h = update_history(h, {"state": "http_error", "http_ok": False}, t[2])
    assert h["consecutive_failures"] == 2 and h["last_success_at"] == t[0] and h["problem_since"] == t[1]
    h = update_history(h, {"state": "empty", "http_ok": True}, t[3])
    assert h["consecutive_failures"] == 0 and h["consecutive_empty"] == 1 and h["last_http_ok_at"] == t[3]
    assert h["last_success_at"] == t[0]  # 0건은 정상 수집으로 세지 않음
    h = update_history(h, {"state": "ok", "http_ok": True}, t[4])
    assert h["consecutive_empty"] == 0 and h["problem_since"] is None and h["last_success_at"] == t[4]


# ---------------------------------------------------------------- 2. 품질 검사


def news_payload(articles=1700, kr=1000, intl=300, ok=50, active=52, top_kr=24, top_intl=16):
    return {
        "generated_at": "2026-11-16T12:00:00Z",
        "counts": {
            "news_articles": articles, "kr_candidates": kr, "intl_candidates": intl,
            "sources_ok": ok, "sources_active": active, "top_kr": top_kr, "top_intl": top_intl,
            "top_quota_kr": 24, "top_quota_intl": 16,
        },
    }


def status_payload(failed=(), empty=(), stale=()):
    return {"summary": {"failed": list(failed), "empty": list(empty), "stale": list(stale)}, "sources": []}


def test_gate_fails_when_everything_fails():
    r = qg.evaluate(news_payload(0, 0, 0, ok=0, top_kr=0, top_intl=0), status_payload(failed=["a"] * 52), None, CONF)
    assert r["passed"] is False and r["first_run"] is True
    assert any("정상 소스 부족" in x for x in r["fails"])


def test_gate_passes_with_partial_source_failure_but_warns():
    r = qg.evaluate(news_payload(ok=46), status_payload(failed=["MBC", "SBS 정치"], empty=["X"]), news_payload()["counts"], CONF)
    assert r["passed"] is True
    assert any("요청·파싱 실패 소스 2곳" in w for w in r["warnings"])


def test_gate_detects_sharp_drop_and_region_shortage():
    base = news_payload()["counts"]
    r = qg.evaluate(news_payload(articles=500, kr=700, intl=60), status_payload(), base, CONF)
    assert r["passed"] is False
    assert any("기사 수 급감" in x for x in r["fails"]) and any("해외 후보 급감" in x for x in r["fails"])
    r = qg.evaluate(news_payload(intl=10, top_intl=8), status_payload(), None, CONF)  # 첫 실행: 절대 기준
    assert r["passed"] is False and any("해외 후보 부족" in x for x in r["fails"])
    assert any("해외 할당 미달" in x for x in r["fails"])


def test_gate_first_run_without_baseline_is_explicit():
    r = qg.evaluate(news_payload(), status_payload(), None, CONF)
    assert r["passed"] is True and r["first_run"] is True and r["baseline"] is None


# ---------------------------------------------------------------- 3. 종단 시험 (로컬 피드 서버)


@pytest.fixture()
def feed_server(tmp_path):
    root = tmp_path / "srv"
    root.mkdir()
    handler = partial(SimpleHTTPRequestHandler, directory=str(root))
    handler.log_message = lambda *a, **k: None
    srv = ThreadingHTTPServer(("127.0.0.1", 0), handler)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    yield root, f"http://127.0.0.1:{srv.server_address[1]}"
    srv.shutdown()


def uniq(*parts):
    # 서로 겹치는 토큰이 없는 제목 → 각각 별개 사건
    import hashlib

    h = hashlib.sha1(repr(parts).encode()).hexdigest()
    return f"{h[:10]} {h[10:20]} {h[20:30]}"


def make_feeds(root: Path, base: str, n_kr=6, n_intl=4, per_feed=8):
    now = datetime.now(timezone.utc)
    lines = []
    for region, n in (("kr", n_kr), ("intl", n_intl)):
        for f in range(n):
            items = "".join(
                f"<item><title>{uniq(region, f, i)}</title>"
                f"<link>https://site{f}.{region}.example/a/{i}</link>"
                f"<pubDate>{(now - timedelta(hours=i)).strftime('%a, %d %b %Y %H:%M:%S +0000')}</pubDate></item>"
                for i in range(per_feed)
            )
            (root / f"{region}{f}.xml").write_text(
                f'<?xml version="1.0"?><rss version="2.0"><channel><title>x</title>{items}</channel></rss>',
                encoding="utf-8",
            )
            lines.append(
                f'<outline text="{region}{f}" outlet="매체{region}{f}" region="{region}" section="news" '
                f'category="politics" xmlUrl="{base}/{region}{f}.xml"/>'
            )
    return lines


def write_opml(path: Path, lines):
    path.write_text('<?xml version="1.0"?><opml version="2.0"><body>' + "".join(lines) + "</body></opml>", encoding="utf-8")


def small_config(path: Path):
    conf = json.loads(json.dumps(CONF))
    conf["fail"].update(min_ok_sources=3, min_news_articles=20, min_kr_candidates=10, min_intl_candidates=5)
    path.write_text(json.dumps(conf), encoding="utf-8")


def run(script, *args, env_site=""):
    import os

    env = dict(os.environ, SITE_URL=env_site)
    return subprocess.run(
        [sys.executable, "-W", "ignore", str(ROOT / "scripts" / script), *args],
        capture_output=True, text=True, env=env, timeout=120,
    )


def test_end_to_end_total_failure_keeps_last_good_and_cache_loss_recovers(feed_server, tmp_path):
    root, base = feed_server
    good_opml = tmp_path / "good.opml"
    write_opml(good_opml, make_feeds(root, base))
    conf = tmp_path / "q.json"
    small_config(conf)
    data = tmp_path / "data"
    common = ["--output-dir", str(data), "--config", str(conf), "--top", "10"]

    # 1) 정상 실행 → 통과, 정상본 기록
    assert run("build_news.py", "--sources", str(good_opml), *common).returncode == 0
    g = run("quality_gate.py", "--data-dir", str(data), "--config", str(conf))
    assert g.returncode == 0, g.stdout
    last_good = (data / "state" / "last_good.json").read_text(encoding="utf-8")
    good_news = json.loads((data / "news.json").read_text(encoding="utf-8"))
    assert good_news["coverage"]["cache_restored"] is False  # 첫 실행: 아카이브 없음에서 시작

    # 2) 전체 소스 실패 → 검사 실패(종료 코드 1). 정상본 기록은 그대로.
    #    아카이브에 남은 기사로 뉴스는 일부 만들어지지만, 정상 소스 0 이므로 배포하지 않는다.
    # 같은 주소에서 서버가 실패하는 상황: 피드 파일을 치워 모두 HTTP 404
    hidden = tmp_path / "hidden"
    hidden.mkdir()
    for f in root.glob("*.xml"):
        f.rename(hidden / f.name)
    assert run("build_news.py", "--sources", str(good_opml), *common).returncode == 0
    g = run("quality_gate.py", "--data-dir", str(data), "--config", str(conf))
    assert g.returncode == 1 and "정상 소스 부족" in g.stdout
    assert (data / "state" / "last_good.json").read_text(encoding="utf-8") == last_good
    health = json.loads((data / "health.json").read_text(encoding="utf-8"))
    assert health["passed"] is False and health["baseline_from"] == "cache"
    status = json.loads((data / "status.json").read_text(encoding="utf-8"))
    assert all(s["state"] == "http_error" for s in status["sources"])
    assert all(s["http_status"] == 404 for s in status["sources"])
    assert all(s["history"]["consecutive_failures"] == 1 for s in status["sources"])
    assert all(s["history"]["last_success_at"] for s in status["sources"])  # 직전 정상 시각 보존

    # 3) 일부 소스 실패 → 통과, 정상 소스로 계속 서비스
    for f in hidden.glob("*.xml"):
        if f.name != "kr0.xml":  # kr0 만 계속 실패
            f.rename(root / f.name)
    assert run("build_news.py", "--sources", str(good_opml), *common).returncode == 0
    g = run("quality_gate.py", "--data-dir", str(data), "--config", str(conf))
    assert g.returncode == 0 and "요청·파싱 실패 소스 1곳" in g.stdout
    status = json.loads((data / "status.json").read_text(encoding="utf-8"))
    failed = [s for s in status["sources"] if s["state"] == "http_error"]
    assert len(failed) == 1 and failed[0]["history"]["consecutive_failures"] == 2

    (hidden / "kr0.xml").rename(root / "kr0.xml")

    # 4) 캐시 유실: 아카이브·state 없이, 배포된 페이지(로컬 서버)에서 이력·기준값 복구
    site = root / "site" / "data"
    site.mkdir(parents=True)
    for f in ("status.json", "health.json"):
        (site / f).write_text((data / f).read_text(encoding="utf-8"), encoding="utf-8")
    fresh = tmp_path / "fresh"
    args = ["--sources", str(good_opml), "--output-dir", str(fresh), "--config", str(conf), "--top", "10"]
    b = run("build_news.py", *args, env_site=f"{base}/site/")
    assert b.returncode == 0, b.stderr
    news = json.loads((fresh / "news.json").read_text(encoding="utf-8"))
    assert news["coverage"]["cache_restored"] is False
    assert news["coverage"]["source_history_from"] == "site"
    # 보관 시작 시각이 이번 실행으로 재설정되어, 그 이전 기록은 복구 불가임이 드러난다
    assert news["coverage"]["archive_started_at"] == news["generated_at"]
    st = json.loads((fresh / "status.json").read_text(encoding="utf-8"))
    recovered = [s for s in st["sources"] if s["name"] == "kr0"][0]["history"]
    assert recovered["first_tracked_at"] < news["generated_at"]  # 이전 이력을 이어받음
    g = run("quality_gate.py", "--data-dir", str(fresh), "--config", str(conf), env_site=f"{base}/site/")
    assert g.returncode == 0 and "기준값: site" in g.stdout
