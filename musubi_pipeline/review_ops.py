# SPDX-FileCopyrightText: 2026 mochizukimonta
# SPDX-License-Identifier: GPL-3.0-or-later
"""musubi_pipeline.review_ops — レビューコメントとHTMLボード書き出しのオペレーター。"""

from __future__ import annotations

import bpy

from . import board_html, core, reviews, task_ops, tasks, ver_ops
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


def review_target(context) -> tuple[int, int, str] | None:
    """レビューの対象カット (シーン番号, カット番号, 由来)。

    由来は 'file'(いま開いているファイルがそのカット)か 'board'(進行
    ボードで選択中)。**開いているファイルがカットならそれを最優先**にする。
    カット以外(アセットなど)を開いているときだけボードの選択を使い、
    どちらも無ければ None。

    シーン/カット番号プロパティへは戻らない。あの値はアセットを開いても
    書き換わらず既定の 1/1 のままなので、背景モデラーの画面に s01/c01 の
    レビューが出ていた(v0.33.0 で廃止)。
    """
    nums = ver_ops.current_cut(context)
    if nums:
        return nums[0], nums[1], "file"
    try:
        s_no, c_no = task_ops._selected(context)
    except PipelineError:
        return None
    return s_no, c_no, "board"


def _target_cut(context) -> tuple[int, int]:
    target = review_target(context)
    if target is None:
        raise PipelineError(
            "レビュー対象のカットがありません"
            "(カットのファイルを開くか、ボードでカットを選択)")
    return target[0], target[1]


def _target_label(target) -> str:
    s_no, c_no, source = target
    origin = "いま開いているカット" if source == "file" else "ボードで選択"
    return f"s{s_no:02d}/c{c_no:02d}({origin})"


def _non_cut_note(context) -> str:
    """プロジェクト内のカット以外のファイルを開いているなら、その旨。

    「なぜ空なのか」を1行で言う(ラベルは折り返せないので短く)。
    """
    fp = bpy.data.filepath
    if not fp:
        return ""
    try:
        core.resolve_root(context.scene.musubi_project_root)
    except PipelineError:
        return ""
    if core.detect_root(fp) is None:
        return ""
    import os
    return f"{os.path.basename(fp)[:22]} はカットではありません"


def clear_reviews(wm) -> None:
    """一覧を空にする(プロジェクト外のファイルへ移ったとき)。I/O なし。"""
    if wm is None:
        return
    try:
        wm.musubi_reviews.clear()
        wm.musubi_reviews_target = ""
        wm.musubi_reviews_note = ""
    except (AttributeError, TypeError):
        pass


def refresh_reviews(context):
    wm = context.window_manager
    wm.musubi_reviews.clear()
    wm.musubi_reviews_target = ""
    wm.musubi_reviews_note = ""
    target = review_target(context)
    if target is None:
        wm.musubi_reviews_note = _non_cut_note(context)
        return
    s_no, c_no, _source = target
    try:
        items = reviews.list_comments(context.scene.musubi_project_root,
                                      s_no, c_no)
    except PipelineError:
        return
    wm.musubi_reviews_target = _target_label(target)
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
    """対象カット(開いているカット、またはボードで選択中)の出力バージョンに
レビューコメントを追加(判定リテイク/承認はステータスにも反映される)"""
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
    """レビューコメント一覧を読み直す(カットを開いたときは自動で更新される。
他の端末から同期で届いたコメントを拾うときに押す)"""
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
