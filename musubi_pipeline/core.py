# SPDX-FileCopyrightText: 2026 mochizukimonta
# SPDX-License-Identifier: GPL-3.0-or-later
"""musubi_pipeline.core — プロジェクト構造・命名規則・パス安全性の中核モジュール。

セキュリティ方針:
- すべてのファイル操作はプロジェクトルート配下であることを検証してから行う
  (パストラバーサル / シンボリックリンク脱出の防止)
- フォルダ名・ファイル名は厳格な正規表現ホワイトリストで検証する
"""

from __future__ import annotations

import json
import os
import re
from pathlib import Path

# ---------------------------------------------------------------------------
# 命名規則(ホワイトリスト)
# ---------------------------------------------------------------------------

ASSET_CATEGORIES = ("char", "bg", "prop")

SCENE_RE = re.compile(r"^scene\d{2,3}$")
CUT_RE = re.compile(r"^c\d{2,3}$")
OUTPUT_RE = re.compile(r"^c\d{2,3}\.(\d{3})\.mp4$")

# プロジェクト直下に作る標準構造(業務内容別テンプレート)
_ASSET_DIRS = (
    "assets/char",
    "assets/char/texture",   # 基本モデルのテクスチャ置き場
    "assets/bg",
    "assets/bg/texture",
    "assets/prop",
    "assets/prop/texture",
)

# key: (表示名, 説明, 作成フォルダ)。どのテンプレートでも同期・バージョン・
# ボード等の全機能は動く(カット管理を使えば scenes は必要時に自動生成)
TEMPLATES = {
    "film": (
        "映像制作(シーン・カット)",
        "assets + scenes/sceneXX/cYY.blend + output。カット管理・進捗ボード・"
        "リールまで使う映像制作向けの標準構造",
        _ASSET_DIRS + ("scenes", "scenes/scene01", "scenes/scene02",
                       "scenes/scene03", "output", ".musubi"),
    ),
    "asset": (
        "アセット制作(持ち寄り・リンク用)",
        "モデル・背景・小物を分担制作し、他プロジェクトからリンクして使う"
        "現場向け。ref(資料)と output(プレビュー出力)付き。scenesなし",
        _ASSET_DIRS + ("ref", "output", ".musubi"),
    ),
    "minimal": (
        "最小構成(自由なフォルダ構成)",
        "管理フォルダ(.musubi)と output だけを作成。フォルダ構成を"
        "チーム独自に決めたい場合はこれ",
        ("output", ".musubi"),
    ),
}

# 後方互換: 既定(映像制作)の構造
STRUCTURE = TEMPLATES["film"][2]


class PipelineError(Exception):
    """パイプライン操作の検証エラー。"""


def atomic_replace(tmp: Path, dst: Path, retries: int = 10,
                   delay: float = 0.2) -> None:
    """os.replace のリトライ版。

    同期ツール(Syncthing/Dropbox)が書き込み直後のファイルを走査のため
    一瞬開いていると、Windowsでは置換が PermissionError になるため。
    """
    import time
    for i in range(retries):
        try:
            os.replace(tmp, dst)
            return
        except PermissionError:
            if i == retries - 1:
                raise
            time.sleep(delay)


# ---------------------------------------------------------------------------
# パス安全性
# ---------------------------------------------------------------------------

def resolve_root(root: str) -> Path:
    """プロジェクトルートを実体パスに解決して返す。空・不正なら例外。"""
    if not root or not root.strip():
        raise PipelineError("プロジェクトルートが設定されていません")
    p = Path(root).expanduser()
    rp = Path(os.path.realpath(p))
    if not rp.is_dir():
        raise PipelineError(f"プロジェクトルートが存在しません: {rp}")
    return rp


def safe_path(root: Path, *parts: str) -> Path:
    """root 配下であることを保証したパスを返す。

    シンボリックリンクを辿った実体パスで検証するため、
    リンクによるルート外への書き込み・読み出しを防ぐ。
    """
    candidate = root.joinpath(*parts)
    real = Path(os.path.realpath(candidate))
    real_root = Path(os.path.realpath(root))
    try:
        real.relative_to(real_root)
    except ValueError:
        raise PipelineError(
            f"パスがプロジェクトルート外を指しています: {candidate}"
        ) from None
    return real


def scene_name(number: int) -> str:
    if not 1 <= number <= 999:
        raise PipelineError(f"シーン番号が範囲外です: {number}")
    return f"scene{number:02d}"


def cut_name(number: int) -> str:
    if not 1 <= number <= 999:
        raise PipelineError(f"カット番号が範囲外です: {number}")
    return f"c{number:02d}"


# ---------------------------------------------------------------------------
# プロジェクト構造
# ---------------------------------------------------------------------------

def create_structure(root_str: str, template: str = "film") -> list[str]:
    """標準フォルダ構造を作成し、作成したパスの一覧を返す(既存はスキップ)。"""
    root = resolve_root(root_str)
    if template not in TEMPLATES:
        raise PipelineError(f"不明なテンプレート: {template}")
    created = []
    for rel in TEMPLATES[template][2]:
        d = safe_path(root, *rel.split("/"))
        if not d.exists():
            d.mkdir(parents=True)
            created.append(str(d.relative_to(root)))
    return created


def structure_ok(root_str: str) -> bool:
    """標準構造がそろっているか。"""
    try:
        root = resolve_root(root_str)
    except PipelineError:
        return False
    return all(root.joinpath(*rel.split("/")).is_dir() for rel in STRUCTURE)


def detect_root(filepath: str) -> Path | None:
    """開いている.blendの場所から遡ってプロジェクトルートを自動判別する。

    ルートの目印は `.musubi` フォルダ。見つからなければ None
    (プロジェクト外のファイル — 手動設定を尊重して何もしない)。
    """
    if not filepath:
        return None
    try:
        p = Path(filepath).resolve().parent
    except OSError:
        return None
    for cand in (p, *p.parents):
        if (cand / ".musubi").is_dir():
            return cand
    return None


def parse_cut_path(root: Path, filepath: str) -> tuple[int, int] | None:
    """scenes/sceneXX/cYY.blend の形ならシーン/カット番号を読み取る。"""
    try:
        rel = Path(filepath).resolve().relative_to(Path(root).resolve())
    except (ValueError, OSError):
        return None
    parts = rel.parts
    if len(parts) == 3 and parts[0] == "scenes" \
            and SCENE_RE.match(parts[1]) and parts[2].endswith(".blend") \
            and CUT_RE.match(parts[2][:-6]):
        return int(parts[1][5:]), int(parts[2][1:-6])
    return None


def write_project_info(root_str: str, sync_id: str) -> None:
    """プロジェクト共通情報(.musubi/project.json)を書く(同期で全員に届く)。

    既に同じ同期IDが書かれていれば何もしない(複数端末が同時に
    書き込むことによる同期競合を避ける)。
    """
    root = resolve_root(root_str)
    if read_project_info(root_str).get("sync_id") == sync_id:
        return
    path = safe_path(root, ".musubi", "project.json")
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(json.dumps({"format": 1, "sync_id": sync_id,
                               "project_name": root.name},
                              ensure_ascii=False, indent=1),
                   encoding="utf-8")
    atomic_replace(tmp, path)


def read_project_info(root_str: str) -> dict:
    """.musubi/project.json を読む(無ければ空dict)。"""
    try:
        root = resolve_root(root_str)
        data = json.loads((root / ".musubi" / "project.json")
                          .read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else {}
    except (PipelineError, OSError, json.JSONDecodeError):
        return {}


def cut_blend_path(root_str: str, scene_no: int, cut_no: int) -> Path:
    """scenes/sceneXX/cYY.blend のパス(ディレクトリは作成する)。"""
    root = resolve_root(root_str)
    sdir = safe_path(root, "scenes", scene_name(scene_no))
    sdir.mkdir(parents=True, exist_ok=True)
    return safe_path(root, "scenes", scene_name(scene_no),
                     f"{cut_name(cut_no)}.blend")


def next_output_path(root_str: str, scene_no: int, cut_no: int) -> Path:
    """output/sceneXX/cYY.NNN.mp4 の次バージョンのパスを返す。"""
    root = resolve_root(root_str)
    odir = safe_path(root, "output", scene_name(scene_no))
    odir.mkdir(parents=True, exist_ok=True)
    cut = cut_name(cut_no)
    latest = 0
    for f in odir.iterdir():
        m = OUTPUT_RE.match(f.name)
        if m and f.name.startswith(cut + "."):
            latest = max(latest, int(m.group(1)))
    if latest >= 999:
        raise PipelineError("バージョン番号が上限(999)に達しました")
    return safe_path(root, "output", scene_name(scene_no),
                     f"{cut}.{latest + 1:03d}.mp4")
