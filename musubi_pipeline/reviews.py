# SPDX-FileCopyrightText: 2026 mochizukimonta
# SPDX-License-Identifier: GPL-3.0-or-later
"""musubi_pipeline.reviews — バージョン単位のレビューコメント。

「output/scene01/c01.003.mp4 の 12フレーム目、口パク直し」のように
特定の出力バージョン(+任意でフレーム)に紐付けた指示を残す。

設計(競合ゼロ方針):
- 1コメント = 1ファイル(.musubi/reviews/sceneXX_cYY/<時刻>_<端末>.json)
- 共有インデックスなし(一覧はフォルダ走査)なので同時書き込みでも衝突しない
"""

from __future__ import annotations

import getpass
import json
import time
from pathlib import Path

from .core import PipelineError, cut_name, resolve_root, safe_path, scene_name
from .sync import host_id

REVIEWS_DIR = (".musubi", "reviews")
COMMENT_MAX = 1000

# 判定: コメント追加時にステータスへ反映できる
VERDICTS = {
    "comment": "コメントのみ",
    "retake": "リテイク",
    "approved": "承認",
}


def _cut_dir(root: Path, scene_no: int, cut_no: int) -> Path:
    return safe_path(root, *REVIEWS_DIR,
                     f"{scene_name(scene_no)}_{cut_name(cut_no)}")


def add_comment(root_str: str, scene_no: int, cut_no: int, version: int,
                text: str, frame: int = -1, verdict: str = "comment") -> dict:
    """レビューコメントを追加する。frame=-1 はフレーム指定なし。"""
    text = str(text).strip()[:COMMENT_MAX]
    if not text:
        raise PipelineError("コメントが空です")
    if not 1 <= version <= 999:
        raise PipelineError(f"バージョン番号が不正です: {version}")
    if frame != -1 and not 0 <= frame <= 999999:
        raise PipelineError(f"フレーム番号が不正です: {frame}")
    if verdict not in VERDICTS:
        raise PipelineError(f"不正な判定: {verdict}")

    root = resolve_root(root_str)
    cdir = _cut_dir(root, scene_no, cut_no)
    cdir.mkdir(parents=True, exist_ok=True)
    stamp = time.strftime("%Y%m%d-%H%M%S")
    path = cdir / f"{stamp}_{host_id()}.json"
    n = 1
    while path.exists():
        path = cdir / f"{stamp}_{host_id()}.{n}.json"
        n += 1

    data = {
        "format": 1,
        "scene": scene_no,
        "cut": cut_no,
        "version": version,
        "frame": frame,
        "verdict": verdict,
        "comment": text,
        "author": getpass.getuser(),
        "host": host_id(),
        "created_at": time.strftime("%Y-%m-%dT%H:%M:%S"),
    }
    path.write_text(json.dumps(data, ensure_ascii=False, indent=1),
                    encoding="utf-8")
    return data


def list_comments(root_str: str, scene_no: int, cut_no: int,
                  version: int | None = None) -> list[dict]:
    """コメントを新しい順に返す(versionを指定するとそのバージョンのみ)。"""
    root = resolve_root(root_str)
    cdir = _cut_dir(root, scene_no, cut_no)
    if not cdir.is_dir():
        return []
    items = []
    for f in sorted(cdir.glob("*.json"), reverse=True):
        try:
            d = json.loads(f.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            continue
        if d.get("format") != 1 or not d.get("comment"):
            continue
        if version is not None and d.get("version") != version:
            continue
        items.append(d)
    return items
