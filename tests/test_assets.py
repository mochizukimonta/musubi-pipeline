# SPDX-FileCopyrightText: 2026 mochizukimonta
# SPDX-License-Identifier: GPL-3.0-or-later
"""assets — アセット(カット以外の .blend)の一覧。

この一覧は「開かずに状態を知る」ための画面なので、**出るべき行が出ないこと**が
いちばんの実害になる(履歴がまだ無いアセット、壊れたサイドカーの隣の行)。
逆に**出てはいけない行**(カット、履歴フォルダの世代コピー)が混ざると、
一覧そのものが読めなくなる。その両方をここで固定する。
"""

from __future__ import annotations

import json
import os
import time
from pathlib import Path

import pytest

from musubi_pipeline import assets, versions


def _asset(project: str, rel: str, data: bytes = b"BLENDER-asset") -> Path:
    p = Path(project) / rel
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_bytes(data)
    return p


def _deps(project: str, scene: str, cut: str, libs: list) -> Path:
    d = Path(project) / ".musubi" / "deps"
    d.mkdir(parents=True, exist_ok=True)
    p = d / f"{scene}_{cut}.json"
    p.write_text(json.dumps({"format": 1, "libraries": libs}),
                 encoding="utf-8")
    return p


def _rels(rows) -> list[str]:
    return [r["rel"] for r in rows]


# --- カット / アセットの振り分け -------------------------------------------

def test_cuts_are_excluded_assets_are_listed(project):
    """カットは進捗ボードの領分。ここに出すと二重管理になる。"""
    _asset(project, "scenes/scene01/c01.blend")      # カット
    _asset(project, "assets/char/akane.blend")       # アセット
    _asset(project, "assets/bg/room.blend")          # アセット
    # 並びは更新の新しい順(別のテストで固定する)なので、ここは集合で見る
    assert set(_rels(assets.scan(project))) == {
        "assets/char/akane.blend", "assets/bg/room.blend"}


def test_blend_outside_the_standard_folders_is_listed(project):
    """フォルダ構成は現場ごとに違う(最小構成テンプレート)。
    カットの命名に当てはまらないものは全部アセットとして扱う。"""
    _asset(project, "作業中/試作.blend")
    assert _rels(assets.scan(project)) == ["作業中/試作.blend"]


def test_scene_like_but_not_a_cut_is_listed(project):
    """scenes/ の下でもカットの命名でなければアセット扱い(消さない)。"""
    _asset(project, "scenes/scene01/背景メモ.blend")
    assert _rels(assets.scan(project)) == ["scenes/scene01/背景メモ.blend"]


def test_version_copies_are_not_listed(project):
    """履歴フォルダには世代コピーの .blend が入っている。

    ここを除外できないと、一覧が同じファイルの世代で埋まる
    (rglob ではフォルダを枝刈りできないので使ってはいけない)。
    """
    src = _asset(project, "assets/char/akane.blend")
    versions.snapshot(project, src, comment="初回")
    src.write_bytes(b"BLENDER-asset-2")
    versions.snapshot(project, src, comment="2回目")
    assert _rels(assets.scan(project)) == ["assets/char/akane.blend"]


def test_syncthing_versions_folder_is_not_listed(project):
    """Syncthing の .stversions にも旧世代の .blend が入る。"""
    _asset(project, ".stversions/assets/char/akane~20260101.blend")
    _asset(project, "assets/char/akane.blend")
    assert _rels(assets.scan(project)) == ["assets/char/akane.blend"]


def test_backup_files_are_not_listed(project):
    """.blend1 / .blend2 は Blender のバックアップで、作業対象ではない。"""
    _asset(project, "assets/char/akane.blend")
    _asset(project, "assets/char/akane.blend1")
    assert _rels(assets.scan(project)) == ["assets/char/akane.blend"]


# --- 履歴が無い / 壊れている ------------------------------------------------

def test_file_without_history_still_appears(project):
    """まだ一度も保存していないアセットこそ「誰も触っていない」を伝える行。"""
    _asset(project, "assets/prop/椅子.blend")
    rows = assets.scan(project)
    assert len(rows) == 1
    assert rows[0]["generations"] == 0
    assert rows[0]["author"] == ""
    assert rows[0]["comment"] == ""


def test_latest_generation_is_used(project):
    src = _asset(project, "assets/char/akane.blend")
    versions.snapshot(project, src, comment="モデル完了")
    src.write_bytes(b"BLENDER-asset-2")
    versions.snapshot(project, src, comment="リグ待ち")
    row = assets.scan(project)[0]
    assert row["generations"] == 2
    assert row["comment"] == "リグ待ち"
    assert row["author"]          # getpass.getuser() の値が入る


def test_broken_sidecar_json_does_not_break_the_row(project):
    """同期フォルダの中身は信頼しない。壊れていても行は消さない。"""
    src = _asset(project, "assets/char/akane.blend")
    versions.snapshot(project, src, comment="ok")
    vdir = versions.version_dir(project, src)
    for meta in vdir.glob("*.json"):
        meta.write_text("{{{ not json", encoding="utf-8")
    row = assets.scan(project)[0]
    assert row["rel"] == "assets/char/akane.blend"
    assert row["generations"] == 1     # 実体はあるので世代としては数える
    assert row["comment"] == ""


def test_sidecar_with_wrong_types_does_not_crash(project):
    """手で書き換えられた・別の版が書いた、のどちらでも落ちない。"""
    src = _asset(project, "assets/char/akane.blend")
    versions.snapshot(project, src, comment="ok")
    vdir = versions.version_dir(project, src)
    for meta in vdir.glob("*.json"):
        meta.write_text(json.dumps({"author": 42, "comment": ["a", "b"],
                                    "saved_at": None}), encoding="utf-8")
    row = assets.scan(project)[0]
    assert row["author"] == "42"
    assert row["comment"] == "['a', 'b']"
    assert row["saved_at"] == ""


def test_sidecar_that_is_not_an_object_does_not_crash(project):
    src = _asset(project, "assets/char/akane.blend")
    versions.snapshot(project, src, comment="ok")
    vdir = versions.version_dir(project, src)
    for meta in vdir.glob("*.json"):
        meta.write_text("[1, 2, 3]", encoding="utf-8")
    assert assets.scan(project)[0]["author"] == ""


# --- 逆引き索引 -------------------------------------------------------------

def test_usage_index_collects_all_cuts_using_one_asset(project):
    _deps(project, "scene01", "c01", ["assets/char/akane.blend"])
    _deps(project, "scene01", "c02", ["assets/char/akane.blend",
                                      "assets/bg/room.blend"])
    usage = assets.usage_index(project)
    assert usage["assets/char/akane.blend"] == ["scene01/c01", "scene01/c02"]
    assert usage["assets/bg/room.blend"] == ["scene01/c02"]


def test_usage_index_is_empty_without_deps(project):
    assert assets.usage_index(project) == {}


def test_unused_asset_has_no_cuts(project):
    _asset(project, "assets/char/akane.blend")
    _asset(project, "assets/prop/未使用.blend")
    _deps(project, "scene01", "c01", ["assets/char/akane.blend"])
    rows = {r["rel"]: r for r in assets.scan(project)}
    assert rows["assets/char/akane.blend"]["cuts"] == ["scene01/c01"]
    assert rows["assets/prop/未使用.blend"]["cuts"] == []


def test_usage_index_ignores_broken_and_foreign_files(project):
    """deps フォルダに何が置かれても、読める分だけ集める。"""
    d = Path(project) / ".musubi" / "deps"
    d.mkdir(parents=True, exist_ok=True)
    (d / "scene01_c01.json").write_text("{{{", encoding="utf-8")
    (d / "メモ.txt").write_text("hello", encoding="utf-8")
    _deps(project, "scene01", "c02", ["assets/char/akane.blend"])
    _deps(project, "scene02", "c01", "リストではない")
    assert assets.usage_index(project) == {
        "assets/char/akane.blend": ["scene01/c02"]}


def test_usage_index_skips_non_string_libraries(project):
    _deps(project, "scene01", "c01", ["assets/char/akane.blend", 42, None, ""])
    assert list(assets.usage_index(project)) == ["assets/char/akane.blend"]


# --- 突き合わせキー(区切り文字・大文字小文字) -----------------------------

def test_usage_key_ignores_separator_style():
    """deps は POSIX で記録し、走査は OS の区切り文字で返す。OSを問わない性質。"""
    assert assets.usage_key("assets/char/akane.blend") == \
        assets.usage_key(os.path.join("assets", "char", "akane.blend"))
    assert assets.usage_key("assets//char/akane.blend") == \
        assets.usage_key("assets/char/akane.blend")


@pytest.mark.skipif(os.name != "nt",
                    reason="大文字小文字を無視するのはWindowsだけ")
def test_usage_key_ignores_case_on_windows():
    assert assets.usage_key("Assets/Char/Akane.blend") == \
        assets.usage_key("assets/char/akane.blend")


@pytest.mark.skipif(os.name == "nt",
                    reason="Windows以外では別のファイルとして扱う")
def test_usage_key_keeps_case_elsewhere():
    """Linux では Akane.blend と akane.blend は別のファイル。同一視は誤報になる。"""
    assert assets.usage_key("assets/char/Akane.blend") != \
        assets.usage_key("assets/char/akane.blend")


def test_scan_accepts_root_with_trailing_separator(project):
    """Blender のフォルダ選択は末尾に区切り文字を付けて返すことがある。"""
    _asset(project, "assets/char/akane.blend")
    _deps(project, "scene01", "c01", ["assets/char/akane.blend"])
    rows = assets.scan(project + os.sep)
    assert rows[0]["cuts"] == ["scene01/c01"]


# --- ロック -----------------------------------------------------------------

def test_lock_holder_and_age_are_reported(project):
    from musubi_pipeline import sync
    _asset(project, "assets/char/akane.blend")
    lock = Path(project) / "assets" / "char" / "akane.blend.lock"
    lock.write_text(json.dumps({"host": "rig-pc", "user": "mochizuki",
                                "acquired_at": time.time() - 3 * 3600}),
                    encoding="utf-8")
    row = assets.scan(project)[0]
    assert row["lock_user"] == "mochizuki"
    assert row["lock_host"] == "rig-pc"
    assert 2.9 < row["lock_age_h"] < 3.1
    assert row["lock_stale"] is False
    assert sync.LOCK_STALE_HOURS == 12.0


def test_stale_lock_is_flagged(project):
    _asset(project, "assets/char/akane.blend")
    lock = Path(project) / "assets" / "char" / "akane.blend.lock"
    lock.write_text(json.dumps({"host": "rig-pc", "user": "mochizuki",
                                "acquired_at": time.time() - 30 * 3600}),
                    encoding="utf-8")
    assert assets.scan(project)[0]["lock_stale"] is True


def test_lock_file_itself_is_not_listed(project):
    """.lock は走査の対象外(拡張子が .blend で終わらないので当然だが固定する)。"""
    _asset(project, "assets/char/akane.blend")
    (Path(project) / "assets" / "char" / "akane.blend.lock").write_text(
        "{}", encoding="utf-8")
    assert _rels(assets.scan(project)) == ["assets/char/akane.blend"]


def test_no_lock_leaves_the_fields_empty(project):
    _asset(project, "assets/char/akane.blend")
    row = assets.scan(project)[0]
    assert row["lock_user"] == "" and row["lock_age_h"] == 0.0
    assert row["lock_stale"] is False


# --- 並び順と経過時間 -------------------------------------------------------

def test_newest_first(project):
    old = _asset(project, "assets/char/old.blend")
    new = _asset(project, "assets/char/new.blend")
    os.utime(old, (1000, 1000))
    os.utime(new, (2000, 2000))
    assert _rels(assets.scan(project)) == ["assets/char/new.blend",
                                           "assets/char/old.blend"]


def test_age_is_measured_from_the_real_file(project):
    """世代は10分に1回しか作られないので、「いつ」は実ファイルの方を見る。"""
    p = _asset(project, "assets/char/akane.blend")
    os.utime(p, (1_000_000, 1_000_000))
    row = assets.scan(project, now=1_000_000 + 2 * 86400)[0]
    assert row["age"] == "2日前"


def test_format_age_steps():
    assert assets.format_age(0) == "たった今"
    assert assets.format_age(59) == "たった今"
    assert assets.format_age(60) == "1分前"
    assert assets.format_age(3600) == "1時間前"
    assert assets.format_age(5 * 3600) == "5時間前"
    assert assets.format_age(2 * 86400) == "2日前"
    assert assets.format_age(400 * 86400) == "1年前"


def test_future_timestamp_is_not_shown_as_negative():
    """同期フォルダには他端末の時計で書かれた未来の日時が来ることがある。"""
    assert assets.format_age(-500) == "たった今"


# --- 絞り込み(ポップアップの検索欄) ---------------------------------------

def _rows_for_filter():
    return [
        {"rel": "assets/char/akane_body.blend", "author": "mochizuki",
         "comment": "モデル完了・リグ待ち"},
        {"rel": "assets/char/akane_hair.blend", "author": "sato",
         "comment": "毛先の房を追加"},
        {"rel": "assets/bg/room_kitchen.blend", "author": "mochizuki",
         "comment": ""},
    ]


def test_empty_query_keeps_everything():
    rows = _rows_for_filter()
    assert len(assets.filter_rows(rows, "")) == 3
    assert len(assets.filter_rows(rows, "   ")) == 3
    assert len(assets.filter_rows(rows, None)) == 3


def test_filter_matches_the_file_name():
    rows = _rows_for_filter()
    got = assets.filter_rows(rows, "akane")
    assert [r["rel"] for r in got] == ["assets/char/akane_body.blend",
                                       "assets/char/akane_hair.blend"]


def test_filter_matches_the_folder_too():
    """フォルダで絞れると「背景だけ見たい」が1語で済む。"""
    rows = _rows_for_filter()
    assert len(assets.filter_rows(rows, "bg/")) == 1


def test_filter_matches_comment_and_author():
    """コメントは受け渡しの合図なので、そこを引けることに意味がある。"""
    rows = _rows_for_filter()
    assert len(assets.filter_rows(rows, "リグ待ち")) == 1
    assert len(assets.filter_rows(rows, "sato")) == 1


def test_filter_terms_are_and_not_or():
    rows = _rows_for_filter()
    assert len(assets.filter_rows(rows, "akane mochizuki")) == 1
    assert len(assets.filter_rows(rows, "akane 存在しない")) == 0


def test_filter_ignores_case_on_every_os():
    """これは文字の検索であって、パスの同一判定ではない。

    `usage_key()` は OS の流儀に従う必要があるが(Linux では
    Akane.blend と akane.blend は別物)、人が打った文字の照合は
    どの OS でも大文字小文字を無視してよい。
    """
    rows = _rows_for_filter()
    assert len(assets.filter_rows(rows, "AKANE")) == 2
    assert len(assets.filter_rows(rows, "MoChIzUkI")) == 2


def test_filter_does_not_mutate_the_input():
    rows = _rows_for_filter()
    assets.filter_rows(rows, "akane")
    assert len(rows) == 3


def test_filter_result_is_a_new_list():
    """呼び出し側がキャッシュを持つので、同じリストを返してはいけない。"""
    rows = _rows_for_filter()
    assert assets.filter_rows(rows, "") is not rows


# --- ルートの扱い -----------------------------------------------------------

def test_scan_rejects_missing_root(tmp_path):
    from musubi_pipeline.core import PipelineError
    with pytest.raises(PipelineError):
        assets.scan(str(tmp_path / "not_there"))


def test_empty_project_is_an_empty_list(project):
    assert assets.scan(project) == []
