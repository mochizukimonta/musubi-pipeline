# SPDX-FileCopyrightText: 2026 mochizukimonta
# SPDX-License-Identifier: GPL-3.0-or-later
"""versions — スナップショット/復元/プルーン。

「履歴が消えたら終わり」の領域。重複排除・同一秒衝突・復元前自動退避・
不変条件(メタのSHA-256 == 保存実体のSHA-256)を機械検証する。
"""

from __future__ import annotations

import pytest

from musubi_pipeline import versions
from musubi_pipeline.core import PipelineError
from musubi_pipeline.sync import _sha256

from conftest import make_blend


def test_snapshot_creates_version(project, as_host):
    f = make_blend(project, data=b"v1")
    as_host("pc-A")
    meta = versions.snapshot(project, f, comment="初回")
    assert meta is not None
    assert meta["comment"] == "初回"
    listed = versions.list_versions(project, f)
    assert len(listed) == 1


def test_snapshot_dedup(project, as_host):
    """内容が同じなら 2 回目は None(履歴を無駄に増やさない)。"""
    f = make_blend(project, data=b"same")
    as_host("pc-A")
    assert versions.snapshot(project, f) is not None
    assert versions.snapshot(project, f) is None
    assert len(versions.list_versions(project, f)) == 1


def test_snapshot_records_new_content(project, as_host):
    f = make_blend(project, data=b"v1")
    as_host("pc-A")
    versions.snapshot(project, f)
    f.write_bytes(b"v2 different")
    assert versions.snapshot(project, f) is not None
    assert len(versions.list_versions(project, f)) == 2


def test_same_second_collision_keeps_both(project, as_host, freeze_time):
    """同一秒内の連続保存で上書きせず、別世代として退避する。"""
    freeze_time("20260720-120000")
    f = make_blend(project, data=b"first")
    as_host("pc-A")
    versions.snapshot(project, f)
    f.write_bytes(b"second within same second")
    meta2 = versions.snapshot(project, f)
    assert meta2 is not None
    # 同じ秒・同じ端末でも 2 世代が残っている
    assert len(versions.list_versions(project, f)) == 2


def test_snapshot_hash_matches_stored_bytes(project, as_host):
    """不変条件: メタの sha256 は保存された実体の sha256 と一致する。

    元ファイルではなくコピー実体をハッシュする、というレース対策の回帰検知。
    """
    f = make_blend(project, data=b"payload")
    as_host("pc-A")
    versions.snapshot(project, f)
    for v in versions.list_versions(project, f):
        assert v["meta"]["sha256"] == _sha256(v["path"])


def test_restore_takes_pre_snapshot(project, as_host):
    """復元は現状を自動退避してから行う(復元で今の作業が消えない)。"""
    f = make_blend(project, data=b"BLENDER-v1")
    as_host("pc-A")
    versions.snapshot(project, f)
    v1 = versions.list_versions(project, f)[0]["name"]
    f.write_bytes(b"WORK IN PROGRESS not yet saved")
    result = versions.restore(project, f, v1)
    assert result["pre_snapshot"] is True
    assert f.read_bytes() == b"BLENDER-v1"
    # 復元前の作業も履歴に退避されている(v1 + 自動退避 = 2)
    assert len(versions.list_versions(project, f)) == 2


def test_restore_rejects_bad_version_name(project, as_host):
    f = make_blend(project)
    as_host("pc-A")
    versions.snapshot(project, f)
    with pytest.raises(PipelineError):
        versions.restore(project, f, "../../etc/passwd")


def test_restore_rejects_unknown_version(project, as_host):
    f = make_blend(project)
    as_host("pc-A")
    versions.snapshot(project, f)
    with pytest.raises(PipelineError, match="見つかりません"):
        versions.restore(project, f, "20200101-000000_ghost")


def test_prune_keeps_newest(project, as_host, freeze_time):
    f = make_blend(project, data=b"gen0")
    as_host("pc-A")
    # 秒をずらしながら 5 世代作る
    for i in range(5):
        freeze_time(f"20260720-12000{i}")
        f.write_bytes(f"gen{i}".encode())
        versions.snapshot(project, f)
    assert len(versions.list_versions(project, f)) == 5
    removed = versions.prune(project, f, keep=2)
    assert removed == 3
    remaining = versions.list_versions(project, f)
    assert len(remaining) == 2
    # 新しい順に残る
    names = [v["name"] for v in remaining]
    assert names == sorted(names, reverse=True)


def test_prune_removes_sidecar_json(project, as_host, freeze_time):
    f = make_blend(project, data=b"a")
    as_host("pc-A")
    for i in range(3):
        freeze_time(f"20260720-13000{i}")
        f.write_bytes(f"c{i}".encode())
        versions.snapshot(project, f)
    vdir = versions.version_dir(project, f)
    versions.prune(project, f, keep=1)
    # 残ったバイナリと .json は必ず対で存在する(孤児メタを残さない)
    blends = [p for p in vdir.iterdir() if p.suffix == ".blend"]
    assert len(blends) == 1
    assert blends[0].with_suffix(".json").exists()


def test_cannot_version_musubi_internal(project, as_host):
    """管理フォルダ内のファイルはバージョン管理対象外。"""
    from pathlib import Path
    internal = Path(project) / ".musubi" / "spec.json"
    internal.write_text("{}")
    as_host("pc-A")
    with pytest.raises(PipelineError):
        versions.snapshot(project, internal)


# --- prune_all(全ファイル一括整理・v0.25) -------------------------------

def _make_generations(project, as_host, freeze_time, cut, count, base):
    """指定カットに count 世代の履歴を作り、最新世代名を返す。"""
    as_host("pc-A")
    f = make_blend(project, cut=cut, data=b"g0")
    last = None
    for i in range(count):
        freeze_time(f"{base}{i}")
        f.write_bytes(f"{cut}-gen{i}".encode())  # 各世代で内容を変える
        versions.snapshot(project, f)
        last = versions.list_versions(project, f)[0]["name"]
    return f, last


def test_prune_all_trims_every_file(project, as_host, freeze_time):
    fa, _ = _make_generations(project, as_host, freeze_time, "c01", 3, "20260720-12000")
    fb, _ = _make_generations(project, as_host, freeze_time, "c02", 3, "20260720-13000")
    removed, files = versions.prune_all(project, keep=1)
    assert files == 2
    assert removed == 4  # 各ファイル 3→1 = 2 削除 ×2
    assert len(versions.list_versions(project, fa)) == 1
    assert len(versions.list_versions(project, fb)) == 1


def test_prune_all_rejects_keep_zero(project):
    with pytest.raises(PipelineError):
        versions.prune_all(project, keep=0)


# --- enforce_size_cap(容量上限・v0.25) ----------------------------------

def test_size_cap_keeps_newest_of_each_file(project, as_host, freeze_time):
    """不変条件: 上限を極小にしても各ファイルの最新世代は必ず残る。

    ここが壊れると復元不能になる。将来の改修に対する防波堤。
    """
    fa, newest_a = _make_generations(project, as_host, freeze_time, "c01", 3, "20260720-14000")
    fb, newest_b = _make_generations(project, as_host, freeze_time, "c02", 3, "20260720-15000")

    removed, freed = versions.enforce_size_cap(project, max_bytes=1)
    assert removed > 0 and freed > 0

    remaining_a = versions.list_versions(project, fa)
    remaining_b = versions.list_versions(project, fb)
    # 各ファイルに最低 1 世代残り、それは最新世代である
    assert len(remaining_a) == 1 and remaining_a[0]["name"] == newest_a
    assert len(remaining_b) == 1 and remaining_b[0]["name"] == newest_b


def test_size_cap_deletes_oldest_first(project, as_host, freeze_time):
    """上限に収まるまで、全ファイル横断で最古の世代から削る。"""
    f, newest = _make_generations(project, as_host, freeze_time, "c01", 4, "20260720-16000")
    before = versions.list_versions(project, f)
    oldest = before[-1]["name"]
    # 3 世代分だけ残る程度の上限(1 世代 = "c01-genN" ≒ 8 バイト)にする
    removed, _ = versions.enforce_size_cap(project, max_bytes=24)
    names = [v["name"] for v in versions.list_versions(project, f)]
    assert newest in names          # 最新は必ず残る
    assert oldest not in names      # 最古から削られている
    assert removed >= 1


def test_size_cap_noop_under_limit(project, as_host, freeze_time):
    _make_generations(project, as_host, freeze_time, "c01", 2, "20260720-17000")
    # 十分大きな上限なら何もしない
    assert versions.enforce_size_cap(project, max_bytes=10_000_000) == (0, 0)


def test_size_cap_noop_when_disabled(project, as_host, freeze_time):
    _make_generations(project, as_host, freeze_time, "c01", 2, "20260720-18000")
    assert versions.enforce_size_cap(project, max_bytes=0) == (0, 0)
    assert versions.enforce_size_cap(project, max_bytes=-5) == (0, 0)
