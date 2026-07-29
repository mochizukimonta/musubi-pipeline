# SPDX-FileCopyrightText: 2026 mochizukimonta
# SPDX-License-Identifier: GPL-3.0-or-later
"""musubi_pipeline.review_ops — レビューコメントとHTMLボード書き出しのオペレーター。"""

from __future__ import annotations

import bpy

from . import board_html, reviews, task_ops, tasks
from .core import PipelineError
from .reviews import VERDICTS

VERDICT_ENUM = [(vid, label, "") for vid, label in VERDICTS.items()]
VERDICT_ICONS = {"comment": 'TEXT', "retake": 'ERROR', "approved": 'CHECKMARK'}


class MusubiReviewItem(bpy.types.PropertyGroup):
    label: bpy.props.StringProperty()
    icon: bpy.props.StringProperty(default='TEXT')


class MUSUBI_UL_reviews(bpy.types.UIList):
    def draw_item(self, context, layout, data, item, icon,
                  active_data, active_propname):
        layout.label(text=item.label, icon=item.icon or 'TEXT')


def _target_cut(context) -> tuple[int, int]:
    """ボードで選択中のカット(未選択ならシーン/カット番号プロパティ)。"""
    try:
        return task_ops._selected(context)
    except PipelineError:
        sc = context.scene
        return sc.musubi_scene_no, sc.musubi_cut_no


def refresh_reviews(context):
    wm = context.window_manager
    wm.musubi_reviews.clear()
    try:
        s_no, c_no = _target_cut(context)
        items = reviews.list_comments(context.scene.musubi_project_root,
                                      s_no, c_no)
    except PipelineError:
        return
    for c in items:
        frame = f" f{c['frame']}" if c.get("frame", -1) >= 0 else ""
        verdict = c.get("verdict", "comment")
        vtxt = f"[{VERDICTS[verdict]}] " if verdict != "comment" else ""
        row = wm.musubi_reviews.add()
        row.label = (f"v{c['version']:03d}{frame}  {vtxt}"
                     f"{c.get('created_at', '')[5:16].replace('T', ' ')} "
                     f"{c.get('author', '?')}: {c['comment'][:60]}")
        row.icon = VERDICT_ICONS[verdict]
    wm.musubi_reviews_index = 0


class MUSUBI_OT_review_add(bpy.types.Operator):
    """選択カットの出力バージョンにレビューコメントを追加
(判定リテイク/承認はステータスにも反映される)"""
    bl_idname = "musubi.review_add"
    bl_label = "レビューコメントを追加"

    version: bpy.props.IntProperty(
        name="対象バージョン", default=1, min=1, max=999,
        description="コメント対象の出力バージョン(c01.003.mp4なら3)")
    frame: bpy.props.IntProperty(
        name="フレーム", default=-1, min=-1, max=999999,
        description="対象フレーム(-1で指定なし)")
    verdict: bpy.props.EnumProperty(name="判定", items=VERDICT_ENUM,
                                    default="comment")
    text: bpy.props.StringProperty(name="コメント", default="")

    def invoke(self, context, event):
        try:
            s_no, c_no = _target_cut(context)
            from .tasks import _latest_output
            from .core import resolve_root
            latest = _latest_output(
                resolve_root(context.scene.musubi_project_root), s_no, c_no)
        except PipelineError as e:
            self.report({'ERROR'}, str(e))
            return {'CANCELLED'}
        if latest == 0:
            self.report({'ERROR'}, "このカットにはまだ出力がありません")
            return {'CANCELLED'}
        self.version = latest
        self.text = ""
        return context.window_manager.invoke_props_dialog(self, width=420)

    def execute(self, context):
        root = context.scene.musubi_project_root
        try:
            s_no, c_no = _target_cut(context)
            reviews.add_comment(root, s_no, c_no, self.version, self.text,
                                self.frame, self.verdict)
            if self.verdict == "retake":
                tasks.update_status(root, s_no, c_no, status="retake",
                                    note=self.text)
            elif self.verdict == "approved":
                tasks.update_status(root, s_no, c_no, status="approved")
        except PipelineError as e:
            self.report({'ERROR'}, str(e))
            return {'CANCELLED'}
        refresh_reviews(context)
        task_ops.refresh_board(context)
        task_ops.export_html_quiet(context)
        self.report({'INFO'},
                    f"v{self.version:03d}へコメントを追加 "
                    f"({VERDICTS[self.verdict]})")
        return {'FINISHED'}


class MUSUBI_OT_review_refresh(bpy.types.Operator):
    """選択カットのレビューコメント一覧を更新"""
    bl_idname = "musubi.review_refresh"
    bl_label = "コメント一覧を更新"

    def execute(self, context):
        refresh_reviews(context)
        return {'FINISHED'}


class MUSUBI_OT_board_html(bpy.types.Operator):
    """進捗ボードを .musubi/board.html にローカル生成してブラウザで開く
(データは同期されるので、各端末で生成すれば同じ進捗が見える)"""
    bl_idname = "musubi.board_html"
    bl_label = "HTMLボードを書き出す"

    open_browser: bpy.props.BoolProperty(name="ブラウザで開く", default=True)

    def execute(self, context):
        try:
            path = board_html.export_board_html(
                context.scene.musubi_project_root)
        except PipelineError as e:
            self.report({'ERROR'}, str(e))
            return {'CANCELLED'}
        if self.open_browser:
            import webbrowser
            webbrowser.open(path.as_uri())
        self.report({'INFO'}, f"書き出しました: {path}")
        return {'FINISHED'}


CLASSES = (
    MusubiReviewItem,
    MUSUBI_UL_reviews,
    MUSUBI_OT_review_add,
    MUSUBI_OT_review_refresh,
    MUSUBI_OT_board_html,
)
