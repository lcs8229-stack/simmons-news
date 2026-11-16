"""언론사 이름 정규화와 AI 활용 정책 판정.

판정 규칙 (보수적):
  1. 피드에 ai="no" 가 있으면 금지.
  2. 원언론사 정책이 deny 이면 금지.
  3. 수집 경로(via) 정책이 deny 이면 금지.
  4. 원언론사 정책과 경로 정책이 모두 allow 일 때만 허용.
  5. 그 밖(미확인 언론사, 미확인 경로)은 허용하지 않는다.

기사에는 판정 결과와 함께 판정 재료(outlet, via, feed_ai_deny)를 저장한다.
AI 요청을 만드는 마지막 단계에서는 저장된 결과를 믿지 말고 `recheck()` / `ai_gate()` 로
현재 정책 표를 기준으로 다시 판정해야 한다.
"""

from __future__ import annotations

import json
import re
import unicodedata
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterable
from urllib.parse import urlparse

DEFAULT_POLICY_PATH = Path(__file__).resolve().parents[1] / "feeds" / "outlets.json"

ALLOW, DENY, UNKNOWN = "allow", "deny", "unknown"


def _key(name: str) -> str:
    """비교용 키: 유니코드 정규화, 소문자, 공백·구두점 제거."""
    s = unicodedata.normalize("NFKC", name or "").casefold()
    return re.sub(r"[\s\.\-_·'’\"()]+", "", s)


def _domain(url_or_host: str) -> str:
    s = (url_or_host or "").strip().lower()
    if "://" in s:
        s = urlparse(s).netloc
    s = s.split(":")[0]
    return s[4:] if s.startswith("www.") else s


@dataclass
class PolicyTable:
    outlets: dict[str, dict[str, Any]]
    via: dict[str, dict[str, Any]]
    _by_key: dict[str, str] = field(default_factory=dict)
    _by_domain: dict[str, str] = field(default_factory=dict)

    def __post_init__(self) -> None:
        for name, info in self.outlets.items():
            self._by_key[_key(name)] = name
            for alias in info.get("aliases", []):
                self._by_key.setdefault(_key(alias), name)
            for d in info.get("domains", []):
                self._by_domain[_domain(d)] = name

    # ------------------------------------------------------------ 정규화

    def canonical(self, name: str = "", domain: str = "") -> str:
        """언론사 표기를 표준 이름으로. 도메인이 있으면 도메인을 우선한다."""
        d = _domain(domain)
        if d:
            if d in self._by_domain:
                return self._by_domain[d]
            # 하위 도메인(news.example.com) → 상위 도메인(example.com) 순으로 찾기
            parts = d.split(".")
            for i in range(1, len(parts) - 1):
                parent = ".".join(parts[i:])
                if parent in self._by_domain:
                    return self._by_domain[parent]
        k = _key(name)
        if k in self._by_key:
            return self._by_key[k]
        return unicodedata.normalize("NFKC", name or "").strip()

    # ------------------------------------------------------------ 정책

    def outlet_policy(self, outlet: str) -> str:
        info = self.outlets.get(outlet)
        return (info or {}).get("ai", UNKNOWN)

    def via_policy(self, via: str) -> str:
        return (self.via.get(via) or {}).get("ai", UNKNOWN)

    def decide(self, outlet: str, via: str, feed_ai_deny: bool) -> dict[str, Any]:
        reasons: list[str] = []
        o = self.outlet_policy(outlet)
        v = self.via_policy(via)
        if feed_ai_deny:
            reasons.append("feed:ai=no")
        if o == DENY:
            reasons.append(f"outlet:{outlet}:deny")
        if v == DENY:
            reasons.append(f"via:{via}:deny")
        if reasons:
            return {"allowed": False, "status": DENY, "reasons": reasons}
        if o == ALLOW and v == ALLOW:
            return {"allowed": True, "status": ALLOW, "reasons": [f"outlet:{outlet}:allow", f"via:{via}:allow"]}
        if o != ALLOW:
            reasons.append(f"outlet:{outlet or '?'}:unknown")
        if v != ALLOW:
            reasons.append(f"via:{via or '?'}:unknown")
        return {"allowed": False, "status": UNKNOWN, "reasons": reasons}


def load_policy(path: Path | None = None) -> PolicyTable:
    data = json.loads(Path(path or DEFAULT_POLICY_PATH).read_text(encoding="utf-8"))
    return PolicyTable(outlets=data.get("outlets", {}), via=data.get("via", {}))


def recheck(article: dict[str, Any], table: PolicyTable) -> dict[str, Any]:
    """저장된 판정 재료로 현재 정책 표 기준 재판정. 재료가 없으면 금지로 본다."""
    outlet = article.get("outlet")
    via = article.get("via")
    if not outlet or not via or "feed_ai_deny" not in article:
        return {"allowed": False, "status": UNKNOWN, "reasons": ["missing-policy-inputs"]}
    return table.decide(table.canonical(outlet), via, bool(article.get("feed_ai_deny")))


def ai_gate(articles: Iterable[dict[str, Any]], table: PolicyTable) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """AI 요청 직전의 마지막 관문. (보낼 수 있는 기사, 걸러진 기사) 를 돌려준다.

    저장된 ai_ok 값은 무시하고 현재 정책으로 다시 판정한다.
    """
    allowed, blocked = [], []
    for a in articles:
        d = recheck(a, table)
        (allowed if d["allowed"] else blocked).append(a)
    return allowed, blocked
