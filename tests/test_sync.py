# SPDX-FileCopyrightText: 2026 mochizukimonta
# SPDX-License-Identifier: GPL-3.0-or-later
"""sync — アドバイザリロック・照合リスト・競合/危険ファイル検出。

ロックは「壊れると人の作業が消える」核心なので、他端末シナリオを
host_id 偽装で作り込む。
"""

from __future__ import annotations

import json
import time

import pytest

from musubi_pipeline import sync
from musubi_pipeline.core import PipelineError

from conftest import make_blend


# --- ロック ---------------------------------------------------------------

def test_acquire_lock_writes_file(project, as_host):
    blend = make_blend(project)
    as_host("pc-A")
    info = sync.acquire_lock(project, blend)
    assert info["host"] == "pc-A"
    assert sync.read_lock(blend)["host"] == "pc-A"


def test_lock_blocks_other_host(project, as_host):
    blend = make_blend(project)
    as_host("pc-A")
    sync.acquire_lock(project, blend)
    as_host("pc-B")
    with pytest.raises(PipelineError, match="編集中"):
        sync.acquire_lock(project, blend)


def test_reacquire_own_lock_is_ok(project, as_host):
    blend = make_blend(project)
    as_host("pc-A")
    sync.acquire_lock(project, blend)
    # 同一端末による再取得は許される(自分の作業の続き)
    info = sync.acquire_lock(project, blend)
    assert info["host"] == "pc-A"


def test_cannot_release_others_lock(project, as_host):
    blend = make_blend(project)
    as_host("pc-A")
    sync.acquire_lock(project, blend)
    as_host("pc-B")
    with pytest.raises(PipelineError, match="解放できません"):
        sync.release_lock(project, blend)


def test_release_own_lock(project, as_host):
    blend = make_blend(project)
    as_host("pc-A")
    sync.acquire_lock(project, blend)
    assert sync.release_lock(project, blend) is True
    assert sync.read_lock(blend) is None


def test_release_when_no_lock_returns_false(project, as_host):
    blend = make_blend(project)
    as_host("pc-A")
    assert sync.release_lock(project, blend) is False


def test_corrupted_lock_treated_as_locked(project, as_host):
    """壊れたロック JSON は「誰かのロック」として扱い、勝手に奪わない。"""
    blend = make_blend(project)
    blend.with_name(blend.name + ".lock").write_text("{{{ broken not json")
    as_host("pc-B")
    with pytest.raises(PipelineError):
        sync.acquire_lock(project, blend)


def test_stale_lock_warns_but_not_stolen(project, as_host):
    """12時間超のロックは警告メッセージ付きだが、自動では奪えない。"""
    blend = make_blend(project)
    as_host("pc-A")
    info = sync.acquire_lock(project, blend)
    info["acquired_at"] = time.time() - 13 * 3600
    blend.with_name(blend.name + ".lock").write_text(
        json.dumps(info, ensure_ascii=False))
    as_host("pc-B")
    with pytest.raises(PipelineError, match="要確認"):
        sync.acquire_lock(project, blend)


def test_lock_rejects_path_outside_root(project, as_host, tmp_path):
    outside = tmp_path.parent / "outside.blend"
    outside.write_bytes(b"x")
    as_host("pc-A")
    with pytest.raises(PipelineError, match="ルート外"):
        sync.acquire_lock(project, outside)


# --- 放置ロックの解除 -----------------------------------------------------
#
# 「解除できるのは放置ロックだけ」が守るべき不変条件。ここが緩むと、
# 作業中の相手からロックを奪えてしまう。

def _age_lock(blend, hours: float):
    """既存のロックの取得時刻を hours 時間前に書き換える。"""
    lp = blend.with_name(blend.name + ".lock")
    info = json.loads(lp.read_text(encoding="utf-8"))
    info["acquired_at"] = time.time() - hours * 3600
    lp.write_text(json.dumps(info, ensure_ascii=False), encoding="utf-8")


def test_force_release_removes_stale_lock_of_other_host(project, as_host):
    blend = make_blend(project)
    as_host("pc-A")
    sync.acquire_lock(project, blend)
    _age_lock(blend, 13)
    as_host("pc-B")
    info = sync.force_release_lock(project, blend)
    assert info["host"] == "pc-A"        # 誰のロックを外したか返る
    assert sync.read_lock(blend) is None
    # 解除後は他端末が普通に取得できる
    assert sync.acquire_lock(project, blend)["host"] == "pc-B"


def test_force_release_refuses_fresh_lock_of_other_host(project, as_host):
    """まだ作業中かもしれない相手のロックは、確認ボタンを押しても外せない。"""
    blend = make_blend(project)
    as_host("pc-A")
    sync.acquire_lock(project, blend)
    _age_lock(blend, 11.9)
    as_host("pc-B")
    with pytest.raises(PipelineError, match="放置ロック"):
        sync.force_release_lock(project, blend)
    assert sync.read_lock(blend)["host"] == "pc-A"   # 残っている


def test_force_release_removes_own_fresh_lock(project, as_host):
    """自分のロックは経過時間に関係なく外せる(release_lock と同じ扱い)。"""
    blend = make_blend(project)
    as_host("pc-A")
    sync.acquire_lock(project, blend)
    assert sync.force_release_lock(project, blend)["host"] == "pc-A"
    assert sync.read_lock(blend) is None


def test_force_release_without_lock_errors(project, as_host):
    blend = make_blend(project)
    as_host("pc-A")
    with pytest.raises(PipelineError, match="ロックはありません"):
        sync.force_release_lock(project, blend)


def test_force_release_rejects_path_outside_root(project, as_host, tmp_path):
    outside = tmp_path.parent / "outside.blend"
    outside.write_bytes(b"x")
    as_host("pc-A")
    with pytest.raises(PipelineError, match="ルート外"):
        sync.force_release_lock(project, outside)


def test_force_release_removes_corrupted_lock(project, as_host):
    """読めないロックは放置扱いで外せる(監査も警告する対象)。"""
    blend = make_blend(project)
    blend.with_name(blend.name + ".lock").write_text("{{{ broken not json")
    as_host("pc-B")
    sync.force_release_lock(project, blend)
    assert sync.read_lock(blend) is None


def test_lock_age_survives_garbage_timestamp(project, as_host):
    """acquired_at が数値でなくても例外にしない(同期フォルダは信頼しない)。

    ここが落ちると、細工されたロックファイル1つでファイルを開けなくなる。
    """
    blend = make_blend(project)
    blend.with_name(blend.name + ".lock").write_text(
        json.dumps({"host": "pc-A", "user": "x", "acquired_at": "きのう"}))
    as_host("pc-B")
    assert sync.lock_age_hours(sync.read_lock(blend)) == float("inf")
    assert sync.is_stale_lock(sync.read_lock(blend)) is True
    with pytest.raises(PipelineError, match="編集中"):   # 開くのは阻止したまま
        sync.acquire_lock(project, blend)


# --- 照合リスト(SHA-256 マニフェスト) -----------------------------------

def test_manifest_verify_in_sync(project, as_host):
    make_blend(project, data=b"shot data")
    as_host("pc-A")
    manifest = sync.write_manifest(project)
    # 別端末視点で自分の実ファイルと突き合わせる
    as_host("pc-B")
    result = sync.verify_against(project, manifest)
    assert result["in_sync"] is True
    assert result["differs"] == []
    assert result["missing_here"] == []


def test_manifest_detects_modification(project, as_host):
    blend = make_blend(project, data=b"original")
    as_host("pc-A")
    manifest = sync.write_manifest(project)
    # マニフェスト作成後にローカルを書き換える → differs で検出されるべき
    blend.write_bytes(b"tampered content longer")
    as_host("pc-B")
    result = sync.verify_against(project, manifest)
    assert result["in_sync"] is False
    rel = "scenes/scene01/c01.blend"
    assert rel in result["differs"]


def test_manifest_detects_missing(project, as_host):
    blend = make_blend(project, data=b"data")
    as_host("pc-A")
    manifest = sync.write_manifest(project)
    blend.unlink()
    as_host("pc-B")
    result = sync.verify_against(project, manifest)
    assert "scenes/scene01/c01.blend" in result["missing_here"]


def test_verify_rejects_bad_format(project, tmp_path):
    bad = tmp_path / ".musubi" / "manifest_bad.json"
    bad.parent.mkdir(parents=True, exist_ok=True)
    bad.write_text(json.dumps({"format": 99, "files": {}}))
    with pytest.raises(PipelineError, match="形式が不正"):
        sync.verify_against(project, bad)


def test_manifest_ignores_lock_and_backup(project, as_host):
    """.lock や .blend1 は照合対象に入らない(誤検知しない)。"""
    blend = make_blend(project, data=b"data")
    blend.with_name(blend.name + ".lock").write_text("{}")
    blend.with_suffix(".blend1").write_bytes(b"backup")
    as_host("pc-A")
    manifest = sync.write_manifest(project)
    data = json.loads(manifest.read_text(encoding="utf-8"))
    files = data["files"]
    assert "scenes/scene01/c01.blend" in files
    assert not any(f.endswith(".lock") for f in files)
    assert not any(f.endswith(".blend1") for f in files)


# --- 競合・危険ファイル検出 -----------------------------------------------

def test_find_conflicts(project):
    d = __import__("pathlib").Path(project) / "scenes" / "scene01"
    d.mkdir(parents=True, exist_ok=True)
    (d / "c01.sync-conflict-20260101-120000-ABCDEF.blend").write_bytes(b"x")
    hits = sync.find_conflicts(project)
    assert any("sync-conflict" in h for h in hits)


def test_find_dangerous_files(project):
    from pathlib import Path
    root = Path(project)
    (root / "assets" / "char" / "evil.exe").write_bytes(b"MZ")
    (root / "assets" / "char" / "macro.py").write_text("print('hi')")
    (root / "assets" / "char" / "safe.png").write_bytes(b"PNG")
    hits = sync.find_dangerous_files(project)
    assert any(h.endswith("evil.exe") for h in hits)
    assert any(h.endswith("macro.py") for h in hits)
    assert not any(h.endswith("safe.png") for h in hits)
