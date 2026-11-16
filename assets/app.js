"use strict";

const PAGE = 30;
const TAB_NOTES = {
  top: "여러 언론이 함께 보도한 사건 순 · 국내 60% / 해외 40%",
  kr: "국내 언론 보도 · 많이 보도된 순",
  intl: "해외 언론 보도 · 많이 보도된 순",
  science: "과학 저널 · 전기·에너지 전문지 · 최신순",
};

const state = {
  data: null,
  tab: "top",
  category: "all",
  query: "",
  shown: PAGE,
};

const $ = (id) => document.getElementById(id);

function store(key, value) {
  try {
    if (value === undefined) return localStorage.getItem(key);
    localStorage.setItem(key, value);
  } catch (_) {
    return null;
  }
  return null;
}

function timeAgo(iso) {
  if (!iso) return "";
  const diff = (Date.now() - new Date(iso).getTime()) / 60000;
  if (diff < 1) return "방금";
  if (diff < 60) return `${Math.floor(diff)}분 전`;
  if (diff < 60 * 24) return `${Math.floor(diff / 60)}시간 전`;
  return `${Math.floor(diff / 1440)}일 전`;
}

function kstTime(iso) {
  if (!iso) return "";
  return new Date(iso).toLocaleString("ko-KR", {
    timeZone: "Asia/Seoul",
    month: "numeric",
    day: "numeric",
    hour: "2-digit",
    minute: "2-digit",
  });
}

function storiesForTab() {
  const d = state.data;
  if (!d) return [];
  if (state.tab === "science") return d.science;
  if (state.tab === "top") {
    const byId = new Map(d.news.map((s) => [s.id, s]));
    return d.top_ids.map((id) => byId.get(id)).filter(Boolean);
  }
  return d.news.filter((s) => s.region === state.tab);
}

function filtered() {
  const q = state.query.trim().toLowerCase();
  return storiesForTab().filter((s) => {
    if (state.category !== "all" && s.category !== state.category) return false;
    if (!q) return true;
    const hay = [s.title, s.title_ko, s.summary, s.summary_ko, s.outlet, ...s.articles.map((a) => a.outlet)]
      .filter(Boolean)
      .join(" ")
      .toLowerCase();
    return hay.includes(q);
  });
}

function renderChips() {
  const counts = new Map();
  for (const s of storiesForTab()) counts.set(s.category, (counts.get(s.category) || 0) + 1);
  const labels = state.data.categories;
  const order = Object.keys(labels).filter((c) => counts.has(c));
  if (!counts.has(state.category)) state.category = "all";
  const chips = $("chips");
  chips.replaceChildren();
  const make = (key, text) => {
    const b = document.createElement("button");
    b.type = "button";
    b.textContent = text;
    b.className = key === state.category ? "chip active" : "chip";
    b.addEventListener("click", () => {
      state.category = key;
      state.shown = PAGE;
      render();
    });
    chips.append(b);
  };
  make("all", "전체");
  for (const c of order) make(c, labels[c]);
}

function leanNote(s) {
  const l = s.leans || [];
  if (l.includes("progressive") && l.includes("conservative")) return "진보·보수 언론 모두 보도";
  return "";
}

function renderStory(s, rank) {
  const li = document.createElement("li");
  li.className = "card";

  const head = document.createElement("div");
  head.className = "meta";
  const cat = document.createElement("span");
  cat.className = `cat cat-${s.category}`;
  cat.textContent = s.category_label;
  head.append(cat);
  if (state.tab === "top") {
    const r = document.createElement("span");
    r.className = "rank";
    r.textContent = s.region === "kr" ? "국내" : "해외";
    head.prepend(r);
  }
  if (s.outlet_count > 1) {
    const n = document.createElement("span");
    n.className = "count";
    n.textContent = `${s.outlet_count}개 언론`;
    head.append(n);
  }
  li.append(head);

  const a = document.createElement("a");
  a.className = "title";
  a.href = s.url;
  a.target = "_blank";
  a.rel = "noopener";
  a.textContent = s.title_ko || s.title;
  li.append(a);

  if (s.title_ko && s.title_ko !== s.title) {
    const orig = document.createElement("p");
    orig.className = "orig";
    orig.textContent = s.title;
    li.append(orig);
  }

  const summary = s.summary_ko || s.summary;
  if (summary) {
    const p = document.createElement("p");
    p.className = "summary";
    p.textContent = summary;
    li.append(p);
  }

  const foot = document.createElement("div");
  foot.className = "foot-meta";
  const parts = [s.outlet, timeAgo(s.published_at), leanNote(s)].filter(Boolean);
  foot.textContent = parts.join(" · ");
  li.append(foot);

  const others = s.articles.filter((x) => x.url !== s.url);
  if (others.length) {
    const det = document.createElement("details");
    det.className = "others";
    const sum = document.createElement("summary");
    sum.textContent = `다른 보도 ${others.length}건`;
    det.append(sum);
    const ul = document.createElement("ul");
    for (const o of others) {
      const oli = document.createElement("li");
      const oa = document.createElement("a");
      oa.href = o.url;
      oa.target = "_blank";
      oa.rel = "noopener";
      oa.textContent = o.title;
      const src = document.createElement("span");
      src.textContent = o.outlet;
      oli.append(src, oa);
      ul.append(oli);
    }
    det.append(ul);
    li.append(det);
  }
  li.dataset.rank = rank;
  return li;
}

function render() {
  if (!state.data) return;
  for (const b of $("tabs").querySelectorAll("button")) {
    const on = b.dataset.tab === state.tab;
    b.classList.toggle("active", on);
    b.setAttribute("aria-selected", on ? "true" : "false");
  }
  $("tabNote").textContent = TAB_NOTES[state.tab];
  renderChips();
  const items = filtered();
  const list = $("list");
  list.replaceChildren(...items.slice(0, state.shown).map((s, i) => renderStory(s, i + 1)));
  if (!items.length) {
    const li = document.createElement("li");
    li.className = "empty";
    li.textContent = "해당하는 뉴스가 없습니다.";
    list.append(li);
  }
  $("more").hidden = items.length <= state.shown;
}

// 소스 상태 → 표시 등급. 0건·오래됨을 정상과 같은 색으로 보이지 않게 한다.
const STATE_TONE = {
  ok: "ok",
  undated: "warn",
  stale: "warn",
  filtered_all: "bad",
  empty: "bad",
  parse_error: "bad",
  http_error: "bad",
  disabled: "off",
};

function renderStatus(status) {
  if (!status) return;
  const active = status.active ?? status.sources.length;
  const sum = status.summary || {};
  const problems = (sum.failed?.length || 0) + (sum.empty?.length || 0);
  const notes = (sum.stale?.length || 0) + (sum.undated?.length || 0);
  let text = `소스 상태 ${status.ok}/${active} 정상`;
  if (problems) text += ` · 문제 ${problems}`;
  if (notes) text += ` · 주의 ${notes}`;
  $("statusSummary").textContent = text;
  const order = { bad: 0, warn: 1, ok: 2, off: 3 };
  const rows = [...status.sources].sort(
    (a, b) => order[STATE_TONE[a.state] || "bad"] - order[STATE_TONE[b.state] || "bad"],
  );
  $("statusList").replaceChildren(
    ...rows.map((s) => {
      const li = document.createElement("li");
      const tone = STATE_TONE[s.state] || (s.ok ? "ok" : "bad");
      li.className = tone;
      const label = s.state_label || (s.ok ? "정상" : "실패");
      const n = s.valid_count ?? s.item_count;
      let detail = s.state === "ok" ? `${n}건` : s.state === "undated" ? `${label} · ${n}건` : label;
      if (s.state === "stale" && s.newest_at) detail = `${label} (최근 기사 ${timeAgo(s.newest_at)})`;
      const h = s.history || {};
      if (tone === "bad" && h.last_success_at) detail += ` · 마지막 정상 ${timeAgo(h.last_success_at)}`;
      if (h.consecutive_failures > 1) detail += ` · 연속 실패 ${h.consecutive_failures}회`;
      if (h.consecutive_empty > 1) detail += ` · 연속 0건 ${h.consecutive_empty}회`;
      li.textContent = `${s.name} — ${detail}`;
      return li;
    }),
  );
}

function renderHealth(health) {
  const ul = $("healthNotes");
  if (!health) return;
  const items = (health.warnings || []).map((w) => {
    const li = document.createElement("li");
    li.textContent = w;
    return li;
  });
  ul.replaceChildren(...items);
}

// 배포가 멈춰도 페이지가 스스로 알 수 있도록, 데이터 생성 시각과 현재 시각을 비교한다.
// 이것은 화면 표시일 뿐 알림이 아니다. 알림은 docs/OPERATIONS.md 의 외부 감시 설정이 필요하다.
function checkStale() {
  const d = state.data;
  if (!d) return;
  const limit = (d.stale_after_minutes || 180) * 60000;
  const age = Date.now() - new Date(d.generated_at).getTime();
  const banner = $("staleBanner");
  if (age > limit) {
    banner.hidden = false;
    banner.textContent = `갱신 지연: 마지막 정상 갱신 ${kstTime(d.generated_at)} (${timeAgo(d.generated_at)}). 자동 갱신이 멈췄을 수 있습니다.`;
    $("updatedAt").classList.add("late");
  } else {
    banner.hidden = true;
    $("updatedAt").classList.remove("late");
  }
}

function renderCoverage(d) {
  const c = d.coverage || {};
  let text = `마지막 정상 갱신 ${kstTime(d.generated_at)}`;
  if (c.archive_started_at) text += ` · 기록 보관 시작 ${kstTime(c.archive_started_at)}`;
  $("lastGood").textContent = text;
}

async function init() {
  const saved = store("tab");
  if (saved && TAB_NOTES[saved]) state.tab = saved;

  $("tabs").addEventListener("click", (e) => {
    const b = e.target.closest("button[data-tab]");
    if (!b) return;
    state.tab = b.dataset.tab;
    state.category = "all";
    state.shown = PAGE;
    store("tab", state.tab);
    render();
    window.scrollTo({ top: 0 });
  });
  $("search").addEventListener("input", (e) => {
    state.query = e.target.value;
    state.shown = PAGE;
    render();
  });
  $("more").addEventListener("click", () => {
    state.shown += PAGE;
    render();
  });

  try {
    const res = await fetch(`./data/news.json?t=${Date.now()}`);
    if (!res.ok) throw new Error(res.status);
    state.data = await res.json();
    $("updatedAt").textContent = `${kstTime(state.data.generated_at)} 업데이트`;
    render();
    renderCoverage(state.data);
    checkStale();
    // 페이지를 오래 열어 둔 경우에도 지연을 알 수 있게 1분마다 다시 확인
    setInterval(checkStale, 60000);
  } catch (err) {
    $("updatedAt").textContent = "불러오기 실패";
    $("list").innerHTML = '<li class="empty">뉴스 데이터를 불러오지 못했습니다.</li>';
  }
  try {
    const res = await fetch(`./data/status.json?t=${Date.now()}`);
    if (res.ok) renderStatus(await res.json());
  } catch (_) {
    /* 상태 정보는 선택 사항 */
  }
  try {
    const res = await fetch(`./data/health.json?t=${Date.now()}`);
    if (res.ok) renderHealth(await res.json());
  } catch (_) {
    /* 품질 검사 정보는 선택 사항 */
  }
}

init();
