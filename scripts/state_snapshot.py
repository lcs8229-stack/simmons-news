#!/usr/bin/env python3
"""수집 상태 스냅샷: 캐시가 사라졌을 때 복구하기 위한 별도 보관본.

GitHub Actions 캐시는 7일 미사용·용량 초과 시 지워진다. 그래서 품질 검사를 통과한 실행이
상태 파일을 스냅샷으로 만들어 Actions artifact(30일 보관)로 따로 올린다.

  create  data/ → 스냅샷 디렉터리. 파일별 sha256·크기와 스키마 버전을 manifest.json 에 기록.
  restore 스냅샷 → data/. manifest 의 sha256 이 하나라도 맞지 않으면 아무것도 복구하지 않는다.
          캐시에서 이미 복구된 파일은 덮어쓰지 않는다 (캐시가 더 최신).

복구 결과는 data/state/restore_info.json 에 남고, build_news.py 가 news.json 의 coverage 에 싣는다.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import shutil
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

SNAPSHOT_FORMAT = 1
# 스냅샷에 넣는 파일 (data/ 기준 상대 경로). 기사 기록·사건 ID·테마 후보·소스 이력·품질 기준값.
INCLUDE = [
    "archive.json",
    "state/story_registry.json",
    "state/theme_candidates.json",
    "state/source_history.json",
    "state/metrics_history.json",
    "state/last_good.json",
]


def _sha256(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def create(data_dir: Path, out_dir: Path) -> dict[str, Any]:
    out_dir.mkdir(parents=True, exist_ok=True)
    files: dict[str, Any] = {}
    for rel in INCLUDE:
        src = data_dir / rel
        if not src.exists():
            continue
        dst = out_dir / rel
        dst.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(src, dst)
        files[rel] = {"sha256": _sha256(dst), "bytes": dst.stat().st_size}
    archive_schema = None
    if (data_dir / "archive.json").exists():
        try:
            archive_schema = json.loads((data_dir / "archive.json").read_text(encoding="utf-8")).get("schema")
        except Exception:  # noqa: BLE001
            archive_schema = None
    manifest = {
        "format": SNAPSHOT_FORMAT,
        "created_at": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
        "archive_schema": archive_schema,
        "files": files,
    }
    (out_dir / "manifest.json").write_text(json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8")
    return manifest


def verify(snap_dir: Path) -> tuple[dict[str, Any] | None, list[str]]:
    problems: list[str] = []
    mp = snap_dir / "manifest.json"
    if not mp.exists():
        return None, ["manifest.json 없음"]
    try:
        manifest = json.loads(mp.read_text(encoding="utf-8"))
    except Exception as exc:  # noqa: BLE001
        return None, [f"manifest.json 읽기 실패: {exc}"]
    if manifest.get("format") != SNAPSHOT_FORMAT:
        problems.append(f"알 수 없는 스냅샷 형식: {manifest.get('format')}")
    for rel, info in manifest.get("files", {}).items():
        if rel not in INCLUDE:
            problems.append(f"예상하지 않은 파일: {rel}")
            continue
        p = snap_dir / rel
        if not p.exists():
            problems.append(f"파일 없음: {rel}")
        elif _sha256(p) != info.get("sha256"):
            problems.append(f"무결성 불일치: {rel}")
    return manifest, problems


def restore(snap_dir: Path, data_dir: Path) -> dict[str, Any]:
    manifest, problems = verify(snap_dir)
    info: dict[str, Any] = {
        "from": "snapshot",
        "snapshot_created_at": (manifest or {}).get("created_at"),
        "restored_files": [],
        "kept_existing": [],
        "problems": problems,
        "ok": not problems and manifest is not None,
    }
    if info["ok"]:
        for rel in manifest["files"]:
            dst = data_dir / rel
            if dst.exists():
                info["kept_existing"].append(rel)  # 캐시에서 온 파일이 더 최신
                continue
            dst.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(snap_dir / rel, dst)
            info["restored_files"].append(rel)
    (data_dir / "state").mkdir(parents=True, exist_ok=True)
    (data_dir / "state" / "restore_info.json").write_text(json.dumps(info, ensure_ascii=False, indent=2), encoding="utf-8")
    return info


def main() -> int:
    ap = argparse.ArgumentParser(description="수집 상태 스냅샷 만들기·복구")
    sub = ap.add_subparsers(dest="cmd", required=True)
    c = sub.add_parser("create")
    c.add_argument("--data-dir", default="data")
    c.add_argument("--out", required=True)
    r = sub.add_parser("restore")
    r.add_argument("--snapshot", required=True)
    r.add_argument("--data-dir", default="data")
    args = ap.parse_args()
    if args.cmd == "create":
        m = create(Path(args.data_dir), Path(args.out))
        print(f"snapshot created: {len(m['files'])} files, {sum(f['bytes'] for f in m['files'].values())} bytes")
        return 0
    info = restore(Path(args.snapshot), Path(args.data_dir))
    print(json.dumps(info, ensure_ascii=False))
    return 0 if info["ok"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
