# SPDX-FileCopyrightText: 2026 mochizukimonta
# SPDX-License-Identifier: GPL-3.0-or-later
"""tasks — ステータス更新・ボード集計。

ボードは status ファイル・出力 mp4・ロックの 3 情報源を合成する。
"""

from __future__ import annotations

import pytest

from musubi_pipeline import sync, tasks
from musubi_pipeline.core import PipelineError

from conftest import make_blend


def test_read_status_default(project):
    st = tasks.read_status(project, 1, 1)
    assert st["status"] == "todo"
    assert st["scene"] == 1 and st["cut"] == 1


def test_update_status_records_author(project, as_host):
    as_host("pc-A")
    st = tasks.update_status(project, 1, 1, status="wip", assignee="太郎")
    assert st["status"] == "wip"
    assert st["assignee"] == "太郎"
    assert st["updated_by"].endswith("@pc-A")
    # 書き戻しても保たれる
    assert tasks.read_status(project, 1, 1)["status"] == "wip"


def test_update_status_rejects_bad_status(project):
    with pytest.raises(PipelineError, match="不正なステータス"):
        tasks.update_status(project, 1, 1, status="done")


def test_update_status_rejects_unknown_field(project):
    with pytest.raises(PipelineError, match="変更できない項目"):
        tasks.update_status(project, 1, 1, secret="x")


def test_update_status_truncates_note(project):
    long = "あ" * 1000
    st = tasks.update_status(project, 1, 1, note=long)
    assert len(st["note"]) == tasks.NOTE_MAX


def test_board_combines_sources(project, as_host):
    as_host("pc-A")
    blend = make_blend(project, scene="scene01", cut="c01")
    tasks.update_status(project, 1, 1, status="wip")
    # 出力 mp4 を 2 本置く
    from pathlib import Path
    odir = Path(project) / "output" / "scene01"
    odir.mkdir(parents=True, exist_ok=True)
    (odir / "c01.001.mp4").write_bytes(b"v")
    (odir / "c01.002.mp4").write_bytes(b"v")
    # 別端末がロック中
    as_host("pc-B")
    sync.acquire_lock(project, blend)
    as_host("pc-A")

    rows = tasks.board(project)
    row = next(r for r in rows if r["scene"] == 1 and r["cut"] == 1)
    assert row["status"] == "wip"
    assert row["blend_exists"] is True
    assert row["latest_output"] == 2
    assert row["locked_by"]  # ロック保持者名が入る


def test_board_discovers_from_blend_files(project):
    make_blend(project, scene="scene02", cut="c05")
    rows = tasks.board(project)
    assert any(r["scene"] == 2 and r["cut"] == 5 for r in rows)


def test_summary_percentage(project):
    tasks.update_status(project, 1, 1, status="approved")
    tasks.update_status(project, 1, 2, status="wip")
    tasks.update_status(project, 1, 3, status="omit")  # 分母から除外
    s = tasks.summary(project)
    assert s["total"] == 2          # omit を除いた 2 件
    assert s["approved"] == 1
    assert s["percent"] == 50
