# SPDX-FileCopyrightText: 2026 mochizukimonta
# SPDX-License-Identifier: GPL-3.0-or-later
"""musubi_pipeline.tasks — タスク・ステータス管理(進捗ボードのデータ層)。

設計(バージョン管理と同じ競合ゼロ方針):
- 1カット = 1ステータスファイル(.musubi/status/sceneXX_cYY.json)
- 共有インデックスを持たない(ボードはフォルダ走査で構築)
- 更新は「読み→マージ→アトミック書き」で、更新者と時刻を必ず記録
- 秘密情報は書かない(担当者名・状態・指示メモのみ)
"""

from __future__ import annotations

import getpass
import json
import re
import time
from pathlib import Path

from .core import (CUT_RE, OUTPUT_RE, SCENE_RE, PipelineError,
                   atomic_replace, cut_name, resolve_root, safe_path,
                   scene_name)
from .sync import host_id, read_lock

STATUS_DIR = (".musubi", "status")

# 表示順を含むステータス定義
STATUSES = {
    "todo": "未着手",
    "wip": "作業中",
    "review": "レビュー待ち",
    "retake": "リテイク",
    "approved": "承認済み",
    "omit": "オミット",
}

_STATUS_FILE_RE = re.compile(r"^(scene\d{2,3})_(c\d{2,3})\.json$")
NOTE_MAX = 500


def _status_path(root: Path, scene_no: int, cut_no: int) -> Path:
    return safe_path(root, *STATUS_DIR,
                     f"{scene_name(scene_no)}_{cut_name(cut_no)}.json")


def _default(scene_no: int, cut_no: int) -> dict:
    return {
        "format": 1,
        "scene": scene_no,
        "cut": cut_no,
        "status": "todo",
        "assignee": "",
        "note": "",
        "updated_at": "",
        "updated_by": "",
    }


def read_status(root_str: str, scene_no: int, cut_no: int) -> dict:
    """ステータスを読む(ファイルがなければ既定値=未着手)。"""
    root = resolve_root(root_str)
    p = _status_path(root, scene_no, cut_no)
    if not p.exists():
        return _default(scene_no, cut_no)
    try:
        data = json.loads(p.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return _default(scene_no, cut_no)
    merged = _default(scene_no, cut_no)
    merged.update({k: data[k] for k in merged if k in data})
    return merged


def update_status(root_str: str, scene_no: int, cut_no: int,
                  **fields) -> dict:
    """ステータスを部分更新して保存する(更新者・時刻を自動記録)。"""
    if "status" in fields and fields["status"] not in STATUSES:
        raise PipelineError(f"不正なステータス: {fields['status']}")
    for key in fields:
        if key not in ("status", "assignee", "note"):
            raise PipelineError(f"変更できない項目: {key}")
    root = resolve_root(root_str)
    data = read_status(root_str, scene_no, cut_no)
    for k, v in fields.items():
        data[k] = str(v).strip()[:NOTE_MAX] if k in ("note", "assignee") else v
    data["updated_at"] = time.strftime("%Y-%m-%dT%H:%M:%S")
    data["updated_by"] = f"{getpass.getuser()}@{host_id()}"

    p = _status_path(root, scene_no, cut_no)
    p.parent.mkdir(parents=True, exist_ok=True)
    tmp = p.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(data, ensure_ascii=False, indent=1),
                   encoding="utf-8")
    atomic_replace(tmp, p)
    return data


def _discover_cuts(root: Path) -> set[tuple[int, int]]:
    """scenes/ の実ファイルと status/ の予定エントリからカット全集合を得る。"""
    cuts: set[tuple[int, int]] = set()
    scenes_dir = root / "scenes"
    if scenes_dir.is_dir():
        for sdir in scenes_dir.iterdir():
            if not (sdir.is_dir() and SCENE_RE.match(sdir.name)):
                continue
            s_no = int(sdir.name.replace("scene", ""))
            for f in sdir.glob("c*.blend"):
                if CUT_RE.match(f.stem):
                    cuts.add((s_no, int(f.stem[1:])))
    status_dir = root.joinpath(*STATUS_DIR)
    if status_dir.is_dir():
        for f in status_dir.iterdir():
            m = _STATUS_FILE_RE.match(f.name)
            if m:
                cuts.add((int(m.group(1).replace("scene", "")),
                          int(m.group(2)[1:])))
    return cuts


def _latest_output(root: Path, scene_no: int, cut_no: int) -> int:
    odir = root / "output" / scene_name(scene_no)
    if not odir.is_dir():
        return 0
    prefix = cut_name(cut_no) + "."
    latest = 0
    for f in odir.iterdir():
        m = OUTPUT_RE.match(f.name)
        if m and f.name.startswith(prefix):
            latest = max(latest, int(m.group(1)))
    return latest


def board(root_str: str) -> list[dict]:
    """進捗ボードの全行(シーン→カット順)。"""
    root = resolve_root(root_str)
    rows = []
    for s_no, c_no in sorted(_discover_cuts(root)):
        st = read_status(root_str, s_no, c_no)
        blend = root / "scenes" / scene_name(s_no) / f"{cut_name(c_no)}.blend"
        lock = read_lock(blend) if blend.exists() else None
        rows.append({
            **st,
            "blend_exists": blend.exists(),
            "locked_by": (lock or {}).get("user", ""),
            "latest_output": _latest_output(root, s_no, c_no),
        })
    return rows


def summary(root_str: str) -> dict:
    """ステータス別集計と完了率。"""
    rows = [r for r in board(root_str) if r["status"] != "omit"]
    counts = {sid: 0 for sid in STATUSES}
    for r in rows:
        counts[r["status"]] += 1
    total = len(rows)
    done = counts["approved"]
    return {
        "counts": counts,
        "total": total,
        "approved": done,
        "percent": (done * 100 // total) if total else 0,
    }
