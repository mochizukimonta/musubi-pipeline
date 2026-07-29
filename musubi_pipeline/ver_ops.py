# SPDX-FileCopyrightText: 2026 mochizukimonta
# SPDX-License-Identifier: GPL-3.0-or-later
"""musubi_pipeline.ver_ops — バージョン管理のBlenderオペレーターとUI部品。"""

from __future__ import annotations

from pathlib import Path

import bpy

from . import core, sync, versions
from .core import PipelineError


class MusubiVersionItem(bpy.types.PropertyGroup):
    label: bpy.props.StringProperty()
    version_name: bpy.props.StringProperty()


class MUSUBI_UL_versions(bpy.types.UIList):
    def draw_item(self, context, layout, data, item, icon,
                  active_data, active_propname):
        layout.label(text=item.label, icon='FILE_BLEND')


def _current_file(context) -> Path:
    fp = bpy.data.filepath
    if not fp:
        raise PipelineError("先にファイルを保存してください(未保存ファイルは履歴化できません)")
    root = core.resolve_root(context.scene.musubi_project_root)
    path = Path(fp)
    try:
        path.resolve().relative_to(root)
    except ValueError:
        raise PipelineError(
            "現在のファイルはプロジェクトフォルダの外にあります") from None
    return path


def refresh_list(context):
    wm = context.window_manager
    wm.musubi_versions.clear()
    try:
        path = _current_file(context)
        items = versions.list_versions(context.scene.musubi_project_root, path)
    except PipelineError as e:
        wm.musubi_versions_summary = str(e)
        return
    for v in items:
        m = v["meta"]
        date = m.get("saved_at", v["name"][:15])[5:16].replace("T", " ")
        row = wm.musubi_versions.add()
        row.version_name = v["name"]
        row.label = (f"{date} {m.get('author','?')} "
                     f"{m.get('comment','') or '(コメントなし)'}")
    cnt, total = versions.history_size(context.scene.musubi_project_root)
    wm.musubi_versions_summary = (
        f"{path.name}: {len(items)}世代 / プロジェクト全体 "
        f"{cnt}件 {total/1e6:.1f}MB")
    wm.musubi_versions_index = 0


class MUSUBI_OT_snapshot(bpy.types.Operator):
    """現在のファイルを保存し、コメント付きでバージョン履歴に退避する"""
    bl_idname = "musubi.snapshot"
    bl_label = "スナップショットを保存"

    def execute(self, context):
        from . import ops as ops_mod
        ops_mod._suppress_auto = True  # 自動保存処理と二重にしない
        try:
            path = _current_file(context)
            if bpy.data.is_dirty:
                ops_mod.save_mainfile_retry()
            meta = versions.snapshot(
                context.scene.musubi_project_root, path,
                context.window_manager.musubi_version_comment)
        except (PipelineError, RuntimeError) as e:
            self.report({'ERROR'}, str(e))
            return {'CANCELLED'}
        finally:
            ops_mod._suppress_auto = False
        if meta is not None:
            # 記録済みコメントは空欄へ戻す(以後の保存への混入防止)
            context.window_manager.musubi_version_comment = ""
        refresh_list(context)
        if meta is None:
            self.report({'INFO'}, "内容が前回と同じため新しい世代は作られませんでした")
        else:
            self.report({'INFO'}, f"履歴に保存: {meta['comment'] or path.name}")
        return {'FINISHED'}


class MUSUBI_OT_versions_refresh(bpy.types.Operator):
    """現在のファイルのバージョン一覧を更新"""
    bl_idname = "musubi.versions_refresh"
    bl_label = "一覧を更新"

    def execute(self, context):
        refresh_list(context)
        return {'FINISHED'}


class MUSUBI_OT_version_restore(bpy.types.Operator):
    """選択したバージョンに復元する(現状は自動で履歴に退避されます)"""
    bl_idname = "musubi.version_restore"
    bl_label = "このバージョンに復元"

    def invoke(self, context, event):
        return context.window_manager.invoke_confirm(self, event)

    def execute(self, context):
        wm = context.window_manager
        if not wm.musubi_versions or wm.musubi_versions_index >= len(wm.musubi_versions):
            self.report({'ERROR'}, "復元するバージョンを選択してください")
            return {'CANCELLED'}
        item = wm.musubi_versions[wm.musubi_versions_index]
        try:
            path = _current_file(context)
            root = context.scene.musubi_project_root
            sync.acquire_lock(root, path)  # 他端末が編集中なら中止
            r = versions.restore(root, path, item.version_name)
            bpy.ops.wm.open_mainfile(filepath=str(path))
        except (PipelineError, RuntimeError) as e:
            self.report({'ERROR'}, str(e))
            return {'CANCELLED'}
        refresh_list(context)
        self.report({'INFO'},
                    f"復元しました: {r['meta'].get('comment') or item.version_name}"
                    "(直前の状態も履歴に退避済み)")
        return {'FINISHED'}


class MUSUBI_OT_versions_prune(bpy.types.Operator):
    """古い履歴を削除して保持世代数まで整理(削除は同期で全端末に反映される)"""
    bl_idname = "musubi.versions_prune"
    bl_label = "古い履歴を整理"

    def invoke(self, context, event):
        return context.window_manager.invoke_confirm(self, event)

    def execute(self, context):
        try:
            path = _current_file(context)
            removed = versions.prune(context.scene.musubi_project_root, path,
                                     context.scene.musubi_version_keep)
        except PipelineError as e:
            self.report({'ERROR'}, str(e))
            return {'CANCELLED'}
        refresh_list(context)
        self.report({'INFO'}, f"{removed}世代を削除しました")
        return {'FINISHED'}


class MUSUBI_OT_versions_prune_all(bpy.types.Operator):
    """プロジェクト内の全ファイルの履歴を保持世代数まで一括整理する
(500MB級の.blendが世代ごとに溜まるのを一度に掃除。削除は同期で全端末に反映)"""
    bl_idname = "musubi.versions_prune_all"
    bl_label = "全カットの履歴を一括整理"

    def invoke(self, context, event):
        return context.window_manager.invoke_confirm(self, event)

    def execute(self, context):
        try:
            removed, files = versions.prune_all(
                context.scene.musubi_project_root,
                context.scene.musubi_version_keep)
        except PipelineError as e:
            self.report({'ERROR'}, str(e))
            return {'CANCELLED'}
        refresh_list(context)
        self.report({'INFO'},
                    f"{files}ファイルから合計{removed}世代を削除しました")
        return {'FINISHED'}


class MUSUBI_OT_versions_enforce_cap(bpy.types.Operator):
    """履歴の合計サイズをプリファレンスの上限まで今すぐ整理する
(各ファイルの最新世代は必ず残し、古い世代から削除。削除は同期で全端末に反映)"""
    bl_idname = "musubi.versions_enforce_cap"
    bl_label = "サイズ上限で整理"

    def invoke(self, context, event):
        return context.window_manager.invoke_confirm(self, event)

    def execute(self, context):
        from .st_ops import pref_history_max_bytes
        max_bytes = pref_history_max_bytes()
        if max_bytes <= 0:
            self.report({'ERROR'},
                        "プリファレンスで履歴の合計サイズ上限を設定してください")
            return {'CANCELLED'}
        try:
            removed, freed = versions.enforce_size_cap(
                context.scene.musubi_project_root, max_bytes)
        except PipelineError as e:
            self.report({'ERROR'}, str(e))
            return {'CANCELLED'}
        refresh_list(context)
        if removed:
            self.report({'INFO'},
                        f"{removed}世代を削除し {freed/1e6:.1f}MB 解放しました")
        else:
            self.report({'INFO'}, "上限内なので削除はありませんでした")
        return {'FINISHED'}


CLASSES = (
    MusubiVersionItem,
    MUSUBI_UL_versions,
    MUSUBI_OT_snapshot,
    MUSUBI_OT_versions_refresh,
    MUSUBI_OT_version_restore,
    MUSUBI_OT_versions_prune,
    MUSUBI_OT_versions_prune_all,
    MUSUBI_OT_versions_enforce_cap,
)
