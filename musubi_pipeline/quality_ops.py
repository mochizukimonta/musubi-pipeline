# SPDX-FileCopyrightText: 2026 mochizukimonta
# SPDX-License-Identifier: GPL-3.0-or-later
"""musubi_pipeline.quality_ops — 品質チェックのオペレーターとパネル表示。

チェックは保存時に自動実行され、結果がパネルに残る。表示の使い分け:
赤(CANCEL)=今すぐ直すべき重大、黄アイコン(ERROR)=把握しておけばよい注意。
修正(パック・不要データ削除・相対パス化)は確認付きの明示ボタンのみ。
"""

from __future__ import annotations

import json
import time

import bpy

from . import quality
from .core import PipelineError
from .quality import LEVEL_CRITICAL, LEVEL_OK, LEVEL_WARN


def run_and_store(context) -> int:
    """チェックを実行して結果をパネル用に保存。問題数を返す。"""
    try:
        results = quality.run_all(context.scene.musubi_project_root)
    except PipelineError as e:
        results = [(LEVEL_WARN, str(e))]
    issues = sum(1 for lv, _ in results if lv != LEVEL_OK)
    context.window_manager.musubi_qc_report = json.dumps({
        "checked_at": time.strftime("%H:%M:%S"),
        "issues": issues,
        "lines": results,
    }, ensure_ascii=False)
    return issues


class MUSUBI_OT_qc_run(bpy.types.Operator):
    """品質チェックを実行(スケール未適用・命名・テクスチャ・不要データ・リンク)"""
    bl_idname = "musubi.qc_run"
    bl_label = "品質チェックを実行"

    def execute(self, context):
        issues = run_and_store(context)
        if issues:
            self.report({'WARNING'}, f"問題 {issues}件(パネル参照)")
        else:
            self.report({'INFO'}, "問題なし")
        return {'FINISHED'}


class MUSUBI_OT_qc_select(bpy.types.Operator):
    """スケール未適用・デフォルト名のオブジェクトを選択する(修正は手動で)"""
    bl_idname = "musubi.qc_select"
    bl_label = "問題オブジェクトを選択"

    def execute(self, context):
        names = set(quality.check_unapplied_scale())
        names |= {n[4:] for n in quality.check_default_names()
                  if n.startswith("OBJ:")}
        count = 0
        for ob in context.view_layer.objects:
            sel = ob.name in names
            try:
                ob.select_set(sel)
                count += 1 if sel else 0
            except RuntimeError:
                pass
        self.report({'INFO'}, f"{count}オブジェクトを選択しました")
        return {'FINISHED'}


class MUSUBI_OT_qc_pack(bpy.types.Operator):
    """すべての外部テクスチャを.blendにパックする(ファイルサイズは増える)"""
    bl_idname = "musubi.qc_pack"
    bl_label = "テクスチャをパック"

    def invoke(self, context, event):
        return context.window_manager.invoke_confirm(self, event)

    def execute(self, context):
        try:
            bpy.ops.file.pack_all()
        except RuntimeError as e:
            self.report({'ERROR'}, str(e))
            return {'CANCELLED'}
        run_and_store(context)
        self.report({'INFO'}, "パックしました(保存を忘れずに)")
        return {'FINISHED'}


class MUSUBI_OT_qc_purge(bpy.types.Operator):
    """参照ゼロの不要データを削除する(フェイクユーザー付きは残る)"""
    bl_idname = "musubi.qc_purge"
    bl_label = "不要データを削除"

    def invoke(self, context, event):
        return context.window_manager.invoke_confirm(self, event)

    def execute(self, context):
        removed = bpy.data.orphans_purge(do_recursive=True)
        run_and_store(context)
        self.report({'INFO'}, f"{removed}件の不要データを削除(保存を忘れずに)")
        return {'FINISHED'}


class MUSUBI_OT_qc_relative(bpy.types.Operator):
    """すべての外部パスを相対パス(//)に変換する(他端末でのリンク切れ防止)"""
    bl_idname = "musubi.qc_relative"
    bl_label = "パスを相対化"

    def execute(self, context):
        if not bpy.data.filepath:
            self.report({'ERROR'}, "先にファイルを保存してください")
            return {'CANCELLED'}
        try:
            bpy.ops.file.make_paths_relative()
        except RuntimeError as e:
            self.report({'ERROR'}, str(e))
            return {'CANCELLED'}
        run_and_store(context)
        self.report({'INFO'}, "相対パス化しました(保存を忘れずに)")
        return {'FINISHED'}


class MUSUBI_OT_asset_usage(bpy.types.Operator):
    """アセット(リンクされた.blend)がどのカットで使われているかを一覧表示"""
    bl_idname = "musubi.asset_usage"
    bl_label = "アセット使用状況"

    def execute(self, context):
        try:
            usage = quality.asset_usage(context.scene.musubi_project_root)
        except PipelineError as e:
            self.report({'ERROR'}, str(e))
            return {'CANCELLED'}
        if not usage:
            lines = ["リンク記録がまだありません"
                     "(カット保存時に自動記録されます)"]
        else:
            lines = [f"{asset}  ←  {', '.join(cuts)}"
                     for asset, cuts in sorted(usage.items())]
        context.window_manager.musubi_last_report = "\n".join(lines)
        self.report({'INFO'}, f"{len(usage)}アセットの使用状況(パネル参照)")
        return {'FINISHED'}


def draw_qc_box(layout, context):
    """品質チェックパネルの中身(見出しはui.py側のパネルヘッダー)。"""
    wm = context.window_manager
    box = layout.column()
    try:
        rep = json.loads(wm.musubi_qc_report)
    except (json.JSONDecodeError, TypeError):
        rep = None
    if rep and rep.get("issues"):
        lines = rep.get("lines", [])
        crit = sum(1 for lv, _ in lines if lv == LEVEL_CRITICAL)
        warn = rep["issues"] - crit
        counts = box.row(align=True)
        if crit:
            r = counts.row()
            r.alert = True
            r.label(text=f"問題 {crit}件", icon='CANCEL')
        if warn:
            counts.label(text=f"注意 {warn}件", icon='ERROR')
    box.operator("musubi.qc_run", icon='VIEWZOOM')
    if rep:
        for lv, msg in rep.get("lines", []):
            if lv == LEVEL_OK:
                box.label(text=msg, icon='CHECKMARK')
            else:
                col = box.column()
                # 赤は「今すぐ直すべき」だけ。注意は黄アイコン+通常色の文字
                col.alert = lv == LEVEL_CRITICAL
                icon = 'CANCEL' if lv == LEVEL_CRITICAL else 'ERROR'
                for i in range(0, len(msg), 38):
                    col.label(text=msg[i:i+38],
                              icon=icon if i == 0 else 'BLANK1')
        box.label(text=f"最終チェック: {rep.get('checked_at','')}")
    row = box.row(align=True)
    row.operator("musubi.qc_select", text="選択", icon='RESTRICT_SELECT_OFF')
    row.operator("musubi.qc_pack", text="パック", icon='PACKAGE')
    row = box.row(align=True)
    row.operator("musubi.qc_purge", text="不要データ削除", icon='TRASH')
    row.operator("musubi.qc_relative", text="相対パス化", icon='FILE_FOLDER')
    box.operator("musubi.asset_usage", icon='LINKED')


CLASSES = (
    MUSUBI_OT_qc_run,
    MUSUBI_OT_qc_select,
    MUSUBI_OT_qc_pack,
    MUSUBI_OT_qc_purge,
    MUSUBI_OT_qc_relative,
    MUSUBI_OT_asset_usage,
)
