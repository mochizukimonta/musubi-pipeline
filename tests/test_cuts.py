# SPDX-FileCopyrightText: 2026 mochizukimonta
# SPDX-License-Identifier: GPL-3.0-or-later
"""assets.scan_cuts — カットを「開くための一覧」として並べる。

この一覧の仕事は**アニメーターの「次に何を開くか」に答えること**なので、
固定すべきなのは次の3つ:

1. 出るべき行が全部出ること(まだ .blend が無い「予定」のカットも含む)
2. 並びが「何から始めるか」の順であること(リテイク → 作業中 → 未着手)
3. 状態・担当・指示が**進行ボードと同じ出所**から来ること
   (`tasks.board()` に委ねる。ここで読み直すとデータが二重になる)

`scan()`(アセット)との違いも固定する。両者は同じポップアップの2つの
表示なので、片方だけ壊れると気づきにくい。
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from musubi_pipeline import assets, core, sync, tasks, versions


def _cut(project: str, scene: int, cut: int,
         data: bytes = b"BLENDER-cut") -> Path:
    p = (Path(project) / "scenes" / core.scene_name(scene)
         / f"{core.cut_name(cut)}.blend")
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_bytes(data)
    return p


def _labels(rows) -> list[str]:
    return [r["label"] for r in rows]


def _row(rows, label: str) -> dict:
    return next(r for r in rows if r["label"] == label)


# --- 何が出るか -------------------------------------------------------------

def test_cuts_are_listed_and_assets_are_not(project):
    """カット一覧とアセット一覧は裏返しの関係。両方に出る行があってはいけない。"""
    _cut(project, 1, 1)
    p = Path(project) / "assets" / "char" / "akane.blend"
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_bytes(b"BLENDER")
    assert _labels(assets.scan_cuts(project)) == ["s01/c01"]
    assert [r["rel"] for r in assets.scan(project)] == \
        ["assets/char/akane.blend"]


def test_planned_cut_without_a_blend_is_listed(project):
    """担当だけ決まってまだ誰も作っていないカットは、真っ先に知りたいもの。

    ステータスファイルが先にあり .blend が無い状態(ボードの「予定」)。
    開けないことは exists=False で示し、行そのものは消さない。
    """
    tasks.update_status(project, 4, 2, status="todo", assignee="akane")
    rows = assets.scan_cuts(project)
    assert _labels(rows) == ["s04/c02"]
    row = rows[0]
    assert row["exists"] is False
    assert row["assignee"] == "akane"
    assert row["age"] == ""          # 読む実ファイルが無い
    assert row["generations"] == 0


def test_existing_cut_without_a_status_file_is_listed(project):
    """保存しただけでステータスを触っていないカットも出る(既定=未着手)。"""
    _cut(project, 1, 3)
    row = assets.scan_cuts(project)[0]
    assert row["status"] == "todo"
    assert row["status_label"] == "未着手"
    assert row["exists"] is True


def test_version_copies_of_cuts_are_not_listed(project):
    """履歴フォルダの世代コピーが行になってはいけない(アセット側と同じ罠)。"""
    src = _cut(project, 1, 1)
    versions.snapshot(project, src, comment="初回")
    src.write_bytes(b"BLENDER-cut-2")
    versions.snapshot(project, src, comment="2回目")
    assert _labels(assets.scan_cuts(project)) == ["s01/c01"]


def test_out_of_range_status_file_does_not_break_the_list(project):
    """手で置かれた・別の版が書いたステータスファイルで落ちない。"""
    sdir = Path(project) / ".musubi" / "status"
    sdir.mkdir(parents=True, exist_ok=True)
    (sdir / "scene000_c000.json").write_text(
        json.dumps({"format": 1, "scene": 0, "cut": 0, "status": "wip"}),
        encoding="utf-8")
    _cut(project, 1, 1)
    assert _labels(assets.scan_cuts(project)) == ["s01/c01"]


def test_unknown_status_falls_back_to_todo(project):
    """同期で届くファイルは信頼しない。知らない状態でも並べ替えで落ちない。"""
    sdir = Path(project) / ".musubi" / "status"
    sdir.mkdir(parents=True, exist_ok=True)
    (sdir / "scene01_c01.json").write_text(
        json.dumps({"format": 1, "scene": 1, "cut": 1, "status": "宇宙"}),
        encoding="utf-8")
    row = assets.scan_cuts(project)[0]
    assert row["status"] == "todo"
    assert row["status_label"] == "未着手"


# --- 並び(この一覧のいちばんの仕事) ---------------------------------------

def test_order_is_what_to_work_on_next(project):
    """リテイク → 作業中 → 未着手 → レビュー待ち → 承認済み → オミット。

    進行ボード(`tasks.board`)はシーン/カット順で、あちらは全体を見る
    ための並び。こちらは作業者が次の一手を探すための並びなので違う。
    """
    for (s, c), status in (((1, 1), "approved"), ((1, 2), "todo"),
                           ((1, 3), "retake"), ((1, 4), "omit"),
                           ((1, 5), "review"), ((1, 6), "wip")):
        _cut(project, s, c)
        tasks.update_status(project, s, c, status=status)
    assert _labels(assets.scan_cuts(project)) == [
        "s01/c03", "s01/c06", "s01/c02", "s01/c05", "s01/c01", "s01/c04"]


def test_same_status_keeps_scene_and_cut_order(project):
    """同じ状態の中では物語の順。更新順にすると毎回並びが変わって探せない。"""
    for s, c in ((2, 1), (1, 10), (1, 2)):
        _cut(project, s, c)
        tasks.update_status(project, s, c, status="wip")
    assert _labels(assets.scan_cuts(project)) == \
        ["s01/c02", "s01/c10", "s02/c01"]


# --- 状態は進行ボードと同じ出所か -------------------------------------------

def test_status_fields_come_from_the_board(project):
    _cut(project, 2, 7)
    tasks.update_status(project, 2, 7, status="retake", assignee="mochi",
                        note="尺を詰める")
    row = assets.scan_cuts(project)[0]
    assert (row["status"], row["status_label"]) == ("retake", "リテイク")
    assert row["assignee"] == "mochi"
    assert row["note"] == "尺を詰める"
    assert row["updated_by"]                     # 更新者が記録されている
    assert row["rel"] == "scenes/scene02/c07.blend"
    assert row["kind"] == "cut"


def test_latest_output_is_picked_up(project):
    """「もう出した」かどうかは、開く前に知りたいことのひとつ。"""
    _cut(project, 1, 1)
    odir = Path(project) / "output" / "scene01"
    odir.mkdir(parents=True, exist_ok=True)
    for n in (1, 2, 12):
        (odir / f"c01.{n:03d}.mp4").write_bytes(b"x")
    assert assets.scan_cuts(project)[0]["latest_output"] == 12


def test_file_side_facts_are_merged(project):
    """作者・コメント・世代数はアセット一覧とまったく同じ出所から来る。"""
    src = _cut(project, 1, 1)
    versions.snapshot(project, src, comment="ラフ")
    src.write_bytes(b"BLENDER-cut-2")
    versions.snapshot(project, src, comment="中割り完了")
    row = assets.scan_cuts(project)[0]
    assert row["generations"] == 2
    assert row["comment"] == "中割り完了"
    assert row["author"]
    assert row["age"]


def test_lock_is_merged(project, as_host):
    """開く前に「誰かが作業中か」が見えることが、この一覧の第一の目的。"""
    src = _cut(project, 1, 1)
    as_host("pc-B")
    sync.acquire_lock(project, src)
    as_host("pc-A")
    row = assets.scan_cuts(project)[0]
    assert row["lock_user"]
    assert row["lock_host"] == "pc-B"
    assert row["lock_stale"] is False


# --- 絞り込み ---------------------------------------------------------------

@pytest.mark.parametrize("query,expected", [
    ("s02", ["s02/c07"]),            # カット名で引く
    ("リテイク", ["s02/c07"]),        # 状態で引く
    ("mochi", ["s02/c07"]),          # 担当で引く
    ("尺", ["s02/c07"]),             # 指示メモで引く
    ("MOCHI", ["s02/c07"]),          # 大文字小文字は無視する
    ("mochi 尺", ["s02/c07"]),       # 空白区切りはAND
    ("mochi 無関係", []),
])
def test_filter_covers_what_an_animator_types(project, query, expected):
    _cut(project, 2, 7)
    _cut(project, 3, 1)
    tasks.update_status(project, 2, 7, status="retake", assignee="mochi",
                        note="尺を詰める")
    rows = assets.filter_rows(assets.scan_cuts(project), query)
    assert _labels(rows) == expected


def test_filter_still_works_on_hand_built_rows():
    """`search` を持たない行(古い呼び出し・テスト)でも落ちない。"""
    rows = [{"rel": "assets/char/akane.blend", "author": "sato",
             "comment": "リグ待ち"}]
    assert len(assets.filter_rows(rows, "リグ")) == 1
    assert len(assets.filter_rows(rows, "存在しない")) == 0


# --- 自分の担当だけ ---------------------------------------------------------

def test_mine_matches_the_assignee_name(project):
    for (s, c), who in (((1, 1), "mochi"), ((1, 2), "sato"), ((1, 3), "")):
        _cut(project, s, c)
        tasks.update_status(project, s, c, status="wip", assignee=who)
    rows = assets.scan_cuts(project)
    assert _labels(assets.mine_rows(rows, "mochi")) == ["s01/c01"]


def test_mine_ignores_case_and_surrounding_spaces(project):
    _cut(project, 1, 1)
    tasks.update_status(project, 1, 1, status="wip", assignee="Mochi")
    rows = assets.scan_cuts(project)
    assert _labels(assets.mine_rows(rows, "  mochi ")) == ["s01/c01"]


def test_mine_with_no_user_keeps_everything(project):
    """ログイン名が取れない環境で一覧が消えてはいけない。"""
    _cut(project, 1, 1)
    rows = assets.scan_cuts(project)
    assert assets.mine_rows(rows, "") == rows
    assert assets.mine_rows(rows, None) == rows


def test_mine_drops_rows_without_an_assignee(project):
    """担当なしの行は「自分の担当」ではない(担当の概念が無い行も同じ)。"""
    _cut(project, 1, 1)
    rows = assets.scan_cuts(project)
    assert rows[0]["assignee"] == ""
    assert assets.mine_rows(rows, "mochi") == []
    assert assets.mine_rows(assets.scan(project), "mochi") == []


def test_mine_returns_a_new_list(project):
    _cut(project, 1, 1)
    rows = assets.scan_cuts(project)
    assert assets.mine_rows(rows, "") is not rows


# --- 開く導線に渡すパス -----------------------------------------------------

def test_cut_rel_is_the_path_the_open_operator_takes(project):
    """進行ボードと一覧が同じ形の文字列を渡すこと(組み立ては1か所)。"""
    assert assets.cut_rel(2, 7) == "scenes/scene02/c07.blend"
    assert assets.cut_rel(12, 345) == "scenes/scene12/c345.blend"
    _cut(project, 2, 7)
    assert assets.scan_cuts(project)[0]["rel"] == assets.cut_rel(2, 7)


def test_cut_rel_rejects_numbers_out_of_range():
    for args in ((0, 1), (1, 0), (1000, 1), (1, 1000)):
        with pytest.raises(core.PipelineError):
            assets.cut_rel(*args)
