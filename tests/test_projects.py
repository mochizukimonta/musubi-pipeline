# SPDX-FileCopyrightText: 2026 mochizukimonta
# SPDX-License-Identifier: GPL-3.0-or-later
"""projects — この端末が開いたプロジェクトの履歴。

ルート指定はアドオンの入口なので、一覧が壊れると「何も始められない」に
直結する。壊れたファイル・消えたフォルダ・大文字小文字の揺れを重点的に見る。
"""

from __future__ import annotations

import json
import os

import pytest

from musubi_pipeline import projects


def _store(tmp_path):
    return tmp_path / "projects.json"


def _mkproj(tmp_path, name):
    p = tmp_path / name
    (p / ".musubi").mkdir(parents=True)
    return p


# --- 記憶と並び順 ---------------------------------------------------------

def test_remember_puts_newest_first(tmp_path):
    store = _store(tmp_path)
    a, b = _mkproj(tmp_path, "alpha"), _mkproj(tmp_path, "beta")
    projects.remember(str(a), store, now=100.0)
    entries = projects.remember(str(b), store, now=200.0)
    assert [e["name"] for e in entries] == ["beta", "alpha"]


def test_remember_moves_existing_to_top_without_duplicating(tmp_path):
    store = _store(tmp_path)
    a, b = _mkproj(tmp_path, "alpha"), _mkproj(tmp_path, "beta")
    projects.remember(str(a), store, now=100.0)
    projects.remember(str(b), store, now=200.0)
    entries = projects.remember(str(a), store, now=300.0)
    assert [e["name"] for e in entries] == ["alpha", "beta"]
    assert len(entries) == 2


def test_remember_ignores_missing_folder(tmp_path):
    """存在しないパスは覚えない。覚えると必ず失敗する項目が並ぶことになる。"""
    store = _store(tmp_path)
    assert projects.remember(str(tmp_path / "not_there"), store) == []
    assert not store.exists()


def test_remember_ignores_empty(tmp_path):
    store = _store(tmp_path)
    assert projects.remember("   ", store) == []


def test_repeated_remember_does_not_rewrite(tmp_path):
    """ファイルを開くと全シーン分の更新が走るので、連打を吸収する。"""
    store = _store(tmp_path)
    a = _mkproj(tmp_path, "alpha")
    projects.remember(str(a), store, now=1000.0)
    mtime = store.stat().st_mtime_ns
    projects.remember(str(a), store, now=1005.0)   # 猶予(60秒)の内側
    assert store.stat().st_mtime_ns == mtime


def test_cap_at_max_entries(tmp_path):
    store = _store(tmp_path)
    for i in range(projects.MAX_ENTRIES + 5):
        p = _mkproj(tmp_path, f"p{i:02d}")
        projects.remember(str(p), store, now=1000.0 + i)
    entries = projects.load(store)
    assert len(entries) == projects.MAX_ENTRIES
    assert entries[0]["name"] == f"p{projects.MAX_ENTRIES + 4:02d}"


# --- 壊れた入力 -----------------------------------------------------------

def test_load_survives_broken_json(tmp_path):
    store = _store(tmp_path)
    store.write_text("{{{ not json", encoding="utf-8")
    assert projects.load(store) == []


def test_load_skips_garbage_entries(tmp_path):
    """同期フォルダ由来ではないが、手で壊される可能性はある。"""
    store = _store(tmp_path)
    store.write_text(json.dumps({"projects": [
        {"path": "", "last_used": 1},
        {"path": 42, "last_used": 1},
        "not a dict",
        {"path": "D:/ok", "last_used": "きのう"},
    ]}), encoding="utf-8")
    entries = projects.load(store)
    assert [e["path"] for e in entries] == ["D:/ok"]
    assert entries[0]["last_used"] == 0.0


def test_load_of_missing_file_is_empty(tmp_path):
    assert projects.load(_store(tmp_path)) == []


# --- 削除・整理 -----------------------------------------------------------

def test_forget_removes_only_that_one(tmp_path):
    store = _store(tmp_path)
    a, b = _mkproj(tmp_path, "alpha"), _mkproj(tmp_path, "beta")
    projects.remember(str(a), store, now=100.0)
    projects.remember(str(b), store, now=200.0)
    entries = projects.forget(str(a), store)
    assert [e["name"] for e in entries] == ["beta"]


def test_prune_missing_drops_deleted_folders(tmp_path):
    store = _store(tmp_path)
    a, b = _mkproj(tmp_path, "alpha"), _mkproj(tmp_path, "beta")
    projects.remember(str(a), store, now=100.0)
    projects.remember(str(b), store, now=200.0)
    import shutil
    shutil.rmtree(a)
    entries = projects.prune_missing(store)
    assert [e["name"] for e in entries] == ["beta"]


def test_dedup_key_ignores_trailing_separator():
    """Blender のフォルダ選択は末尾に区切り文字を付けて返すことがある。

    ここを吸収しないと、同じプロジェクトが2件並ぶ。OS を問わない性質。
    """
    assert projects.dedup_key("D:/works/pv/") == projects.dedup_key("D:/works/pv")
    assert projects.dedup_key("D:/works//pv") == projects.dedup_key("D:/works/pv")


@pytest.mark.skipif(os.name != "nt", reason="大文字小文字を無視するのはWindowsだけ")
def test_dedup_key_ignores_case_on_windows():
    """Linux では /foo と /FOO は別物なので、同一視してはいけない。"""
    assert projects.dedup_key("D:/works/PV") == projects.dedup_key("d:\\works\\pv")


# --- Syncthing との合流(純関数) -----------------------------------------

def test_merge_adds_state_to_known_projects():
    entries = [{"path": "D:/works/pv", "name": "pv", "last_used": 10.0}]
    folders = [{"id": "pv-2026", "path": "D:/works/pv",
                "devices": ["sato", "suzuki"], "paused": False}]
    out = projects.merge_shared(entries, folders)
    assert len(out) == 1
    assert out[0]["sync_id"] == "pv-2026"
    assert out[0]["peers"] == 2
    assert out[0]["shared"] is True


def test_merge_appends_syncthing_only_projects():
    """履歴を失っても、参加中のプロジェクトは Syncthing 側から拾える。"""
    entries = [{"path": "D:/works/pv", "name": "pv", "last_used": 10.0}]
    folders = [{"id": "other", "path": "D:/works/other", "devices": []}]
    out = projects.merge_shared(entries, folders)
    assert [e["name"] for e in out] == ["pv", "other"]   # 使った方が先
    assert out[1]["last_used"] == 0.0


def test_merge_without_syncthing_returns_history_only():
    """Syncthing 停止中でも履歴だけで選べる(これが土台である理由)。"""
    entries = [{"path": "D:/works/pv", "name": "pv", "last_used": 10.0}]
    assert len(projects.merge_shared(entries, None)) == 1


def test_merge_does_not_duplicate_on_trailing_separator():
    """Syncthing は区切り文字なし、Blender は付きで返すことがある。"""
    entries = [{"path": "D:/works/pv/", "name": "pv", "last_used": 10.0}]
    folders = [{"id": "pv", "path": "D:/works/pv", "devices": ["a"]}]
    out = projects.merge_shared(entries, folders)
    assert len(out) == 1
    assert out[0]["peers"] == 1


def test_merge_orders_unused_by_name():
    entries = []
    folders = [{"id": "z", "path": "D:/works/zeta", "devices": []},
               {"id": "a", "path": "D:/works/alpha", "devices": []}]
    out = projects.merge_shared(entries, folders)
    assert [e["name"] for e in out] == ["alpha", "zeta"]


def test_display_name_uses_folder_name():
    assert projects.display_name("D:/works/usagi_pv") == "usagi_pv"
