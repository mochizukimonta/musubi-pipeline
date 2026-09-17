# SPDX-FileCopyrightText: 2026 mochizukimonta
# SPDX-License-Identifier: GPL-3.0-or-later
"""「いま開いているファイルに追従するパネル」の契約(v0.33.0)。

bpy に依存する層(review_ops / ops)は Blender 外で実行できないため、
ここではソースの不変条件だけを機械的に固定する。

背景: v0.32 まではレビュー一覧が「ボード未選択ならシーン/カット番号
プロパティ」に落ちていた。アセット(bg.blend など)を開いてもこの値は
既定の 1/1 のままなので、背景モデラーの画面に s01/c01 のリテイク指示が
出ていた。また、バージョン一覧はボタンを押すまで空だった。
"""

from __future__ import annotations

import re
from pathlib import Path

PKG = Path(__file__).resolve().parents[1] / "musubi_pipeline"


def _src(name: str) -> str:
    return (PKG / name).read_text(encoding="utf-8")


def test_review_target_never_falls_back_to_scene_cut_props():
    """レビューの対象決定に musubi_scene_no / musubi_cut_no を使わない。

    あの既定値(1/1)へ落ちると、カット以外のファイルを開いている人に
    無関係なカットのレビューが出る。
    """
    src = _src("review_ops.py")
    assert "musubi_scene_no" not in src
    assert "musubi_cut_no" not in src


def test_review_target_prefers_open_file_over_board():
    """対象は「開いているカット」が先、ボード選択はその次。"""
    src = _src("review_ops.py")
    body = src[src.index("def review_target"):src.index("def _target_cut")]
    assert body.index("current_cut(") < body.index("task_ops._selected(")


def test_load_post_refreshes_file_panels():
    """ファイルを開いたら、バージョン一覧とレビューをそのファイルの
    ものに自動更新する(プロジェクト外へ移ったら空にする)。"""
    src = _src("ops.py")
    body = src[src.index("def on_load_post"):src.index("def on_save_post")]
    assert "_refresh_panels_later(" in body
    assert "clear_file_panels(" in body


def test_save_post_refreshes_after_snapshot_thread():
    """保存後は、スナップショット(別スレッド)が終わってから一覧を更新
    する。終わる前に読むと、いま作られた世代が一覧に無い。"""
    src = _src("ops.py")
    body = src[src.index("def on_save_post"):]
    assert re.search(r"_refresh_panels_when_done\(\s*snap\s*\)", body)


def test_panels_show_their_target():
    """一覧の上に対象(ファイル名/カット)を必ず出す。"""
    src = _src("ui.py")
    assert "musubi_versions_target" in src
    assert "musubi_reviews_target" in src
