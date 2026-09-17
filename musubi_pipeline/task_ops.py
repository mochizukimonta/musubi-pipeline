# SPDX-FileCopyrightText: 2026 mochizukimonta
# SPDX-License-Identifier: GPL-3.0-or-later
"""musubi_pipeline.task_ops — タスク・ステータス管理のオペレーターと進捗ボード。"""

from __future__ import annotations

import getpass

import bpy

from . import assets, tasks
from .core import PipelineError
from .tasks import STATUSES

STATUS_ICONS = {
    "todo": 'RADIOBUT_OFF',
    "wip": 'TIME',
    "review": 'HIDE_OFF',
    "retake": 'ERROR',
    "approved": 'CHECKMARK',
    "omit": 'X',
}

STATUS_ENUM = [(sid, label, "") for sid, label in STATUSES.items()]


class MusubiBoardItem(bpy.types.PropertyGroup):
    scene_no: bpy.props.IntProperty()
    cut_no: bpy.props.IntProperty()
    label: bpy.props.StringProperty()
    icon: bpy.props.StringProperty(default='NONE')
    # 開く導線(musubi.open_blend)に渡すルート相対パスと、押せるかどうか。
    # **走査時に確定させて UI は読むだけにする** — draw でパスを組み立てると
    # 番号が範囲外のときに例外になり、パネル全体が描けなくなる
    rel: bpy.props.StringProperty()
    exists: bpy.props.BoolProperty(default=False)


class MUSUBI_UL_board(bpy.types.UIList):
    def draw_item(self, context, layout, data, item, icon,
                  active_data, active_propname):
        layout.label(text=item.label, icon=item.icon or 'NONE')


def refresh_board(context):
    wm = context.window_manager
    wm.musubi_board.clear()
    root = context.scene.musubi_project_root
    try:
        rows = tasks.board(root)
        s = tasks.summary(root)
    except PipelineError as e:
        wm.musubi_board_summary = str(e)
        return
    flt = wm.musubi_board_filter
    for r in rows:
        if flt != 'ALL' and r["status"] != flt:
            continue
        parts = [f"s{r['scene']:02d}/c{r['cut']:02d}",
                 STATUSES[r["status"]],
                 r["assignee"] or "─"]
        if r["latest_output"]:
            parts.append(f"v{r['latest_output']:03d}")
        if not r["blend_exists"]:
            parts.append("(予定)")
        if r["locked_by"]:
            parts.append(f"[編集中:{r['locked_by']}]")
        if r["note"]:
            parts.append(r["note"][:24])
        item = wm.musubi_board.add()
        item.scene_no, item.cut_no = r["scene"], r["cut"]
        item.label = "  ".join(parts)
        item.icon = STATUS_ICONS[r["status"]]
        item.exists = bool(r["blend_exists"])
        try:
            item.rel = assets.cut_rel(r["scene"], r["cut"])
        except PipelineError:
            item.exists = False  # 番号が範囲外(手で置かれたステータス)
    c = s["counts"]
    wm.musubi_board_summary = (
        f"承認 {s['approved']}/{s['total']} ({s['percent']}%) | "
        f"作業中{c['wip']} レビュー{c['review']} リテイク{c['retake']} "
        f"未着手{c['todo']}")
    wm.musubi_board_index = min(wm.musubi_board_index,
                               max(0, len(wm.musubi_board) - 1))


def clear_board(wm) -> None:
    """ボードを空にする(プロジェクト外へ移ったとき)。I/O なし。

    ボードはプロジェクト単位の表なので、プロジェクト外のファイルを開いた
    まま前のプロジェクトの表が残っていると、そこから状態を変えられて
    しまう(_selected は行があれば通る)。
    """
    wm.musubi_board.clear()
    wm.musubi_board_summary = ""


def export_html_quiet(context):
    """ステータス変更後にHTMLボードを黙って更新する(失敗は無視)。"""
    try:
        from . import board_html
        board_html.export_board_html(context.scene.musubi_project_root)
    except Exception:
        pass


def _selected(context) -> tuple[int, int]:
    wm = context.window_manager
    if not wm.musubi_board or wm.musubi_board_index >= len(wm.musubi_board):
        raise PipelineError("ボードでカットを選択してください(まず「更新」)")
    item = wm.musubi_board[wm.musubi_board_index]
    return item.scene_no, item.cut_no


class MUSUBI_OT_board_refresh(bpy.types.Operator):
    """進捗ボードを更新"""
    bl_idname = "musubi.board_refresh"
    bl_label = "ボードを更新"

    def execute(self, context):
        refresh_board(context)
        return {'FINISHED'}


class MUSUBI_OT_task_set_status(bpy.types.Operator):
    """選択カットのステータスを変更(変更は同期で全端末に共有される)"""
    bl_idname = "musubi.task_set_status"
    bl_label = "状態を変更"
    bl_property = "status"

    status: bpy.props.EnumProperty(items=STATUS_ENUM, name="ステータス")

    def execute(self, context):
        try:
            s_no, c_no = _selected(context)
            tasks.update_status(context.scene.musubi_project_root,
                                s_no, c_no, status=self.status)
        except PipelineError as e:
            self.report({'ERROR'}, str(e))
            return {'CANCELLED'}
        refresh_board(context)
        export_html_quiet(context)
        self.report({'INFO'},
                    f"s{s_no:02d}/c{c_no:02d} → {STATUSES[self.status]}")
        return {'FINISHED'}


class MUSUBI_OT_task_assign(bpy.types.Operator):
    """選択カットの担当者を設定(空欄で担当解除)"""
    bl_idname = "musubi.task_assign"
    bl_label = "担当者を設定"

    assignee: bpy.props.StringProperty(name="担当者", default="")

    def invoke(self, context, event):
        try:
            s_no, c_no = _selected(context)
        except PipelineError as e:
            self.report({'ERROR'}, str(e))
            return {'CANCELLED'}
        current = tasks.read_status(context.scene.musubi_project_root,
                                    s_no, c_no)
        self.assignee = current["assignee"] or getpass.getuser()
        return context.window_manager.invoke_props_dialog(self)

    def execute(self, context):
        try:
            s_no, c_no = _selected(context)
            tasks.update_status(context.scene.musubi_project_root,
                                s_no, c_no, assignee=self.assignee)
        except PipelineError as e:
            self.report({'ERROR'}, str(e))
            return {'CANCELLED'}
        refresh_board(context)
        export_html_quiet(context)
        return {'FINISHED'}


class MUSUBI_OT_task_note(bpy.types.Operator):
    """選択カットの指示・メモを編集(リテイク指示などをここに書く)"""
    bl_idname = "musubi.task_note"
    bl_label = "指示・メモを編集"

    note: bpy.props.StringProperty(name="指示・メモ", default="")

    def invoke(self, context, event):
        try:
            s_no, c_no = _selected(context)
        except PipelineError as e:
            self.report({'ERROR'}, str(e))
            return {'CANCELLED'}
        self.note = tasks.read_status(context.scene.musubi_project_root,
                                      s_no, c_no)["note"]
        return context.window_manager.invoke_props_dialog(self, width=400)

    def execute(self, context):
        try:
            s_no, c_no = _selected(context)
            tasks.update_status(context.scene.musubi_project_root,
                                s_no, c_no, note=self.note)
        except PipelineError as e:
            self.report({'ERROR'}, str(e))
            return {'CANCELLED'}
        refresh_board(context)
        export_html_quiet(context)
        return {'FINISHED'}


class MUSUBI_OT_task_add_cut(bpy.types.Operator):
    """シーン/カット番号のカットを「予定(未着手)」としてボードに追加"""
    bl_idname = "musubi.task_add_cut"
    bl_label = "カットを予定に追加"

    def execute(self, context):
        sc = context.scene
        try:
            tasks.update_status(sc.musubi_project_root, sc.musubi_scene_no,
                                sc.musubi_cut_no, status="todo")
        except PipelineError as e:
            self.report({'ERROR'}, str(e))
            return {'CANCELLED'}
        refresh_board(context)
        export_html_quiet(context)
        self.report({'INFO'},
                    f"s{sc.musubi_scene_no:02d}/c{sc.musubi_cut_no:02d} を予定に追加")
        return {'FINISHED'}


# **v0.34.0 で MUSUBI_OT_task_open_cut を廃止した。**
# 「開く」はアセット一覧の musubi.open_blend に一本化されている。
# あちらは (1) 未保存の編集を黙って捨てず先に保存し、(2) 確認画面に
# ロックの説明と「誰がいつから作業中か」を出す。こちらは invoke_confirm
# だけで、どちらも持っていなかった(v0.31.0 で「次の版で共用できる」と
# 書いたまま残っていた宿題)。同じ操作で安全性が2段違うものを2つ置かない。
#
# 進行ボードの「開く」ボタンは musubi.open_blend を呼ぶ(ui.py)。
# 渡すルート相対パスは refresh_board が MusubiBoardItem.rel に入れている。


CLASSES = (
    MusubiBoardItem,
    MUSUBI_UL_board,
    MUSUBI_OT_board_refresh,
    MUSUBI_OT_task_set_status,
    MUSUBI_OT_task_assign,
    MUSUBI_OT_task_note,
    MUSUBI_OT_task_add_cut,
)
