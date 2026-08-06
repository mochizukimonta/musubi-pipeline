# SPDX-FileCopyrightText: 2026 mochizukimonta
# SPDX-License-Identifier: GPL-3.0-or-later
"""musubi_pipeline.project_ops — プロジェクトを開く導線。

ルートを指定することがこのアドオンの入口なので、そこを「パスの手入力」から
「前に開いたものを選ぶ」に変える。

`draw()` は再描画のたびに呼ばれるため、**一覧の組み立てはここで行わない**。
ファイルを開いた時・ボタンを押した時に `refresh()` でモジュール変数へ積み、
UI はそれを読むだけにする(ARCHITECTURE の「UI層で I/O をしない」)。
"""

from __future__ import annotations

import json

import bpy

from . import core, projects
from .core import PipelineError

# 表示用の合流済み一覧。draw() はこれを読むだけ
_menu_cache: list[dict] = []


def refresh() -> list[dict]:
    """履歴と(あれば)Syncthingの共有一覧を合流してキャッシュする。

    Syncthing の状態は取得済みのものを使うだけで、ここから通信はしない。
    停止中なら履歴だけの一覧になる。
    """
    global _menu_cache
    folders = None
    try:
        rep = json.loads(bpy.context.window_manager.musubi_st_status)
        folders = rep.get("folders")
    except (AttributeError, TypeError, ValueError, KeyError):
        folders = None
    _menu_cache = projects.merge_shared(projects.load(), folders)
    return _menu_cache


def cached() -> list[dict]:
    return _menu_cache


def remember(root_str: str) -> None:
    """ルートが設定されたときに履歴へ積む(存在するフォルダだけ)。"""
    try:
        projects.remember(root_str)
    except OSError:
        return
    refresh()


def _apply_root(context, path: str) -> None:
    """全シーンのルートを揃える。シーンごとに違うと混乱の元になる。"""
    for sc in bpy.data.scenes:
        try:
            if sc.musubi_project_root != path:
                sc.musubi_project_root = path
        except AttributeError:
            break


class MUSUBI_OT_open_project(bpy.types.Operator):
    """このプロジェクトを開く(ルートに設定する)"""
    bl_idname = "musubi.open_project"
    bl_label = "プロジェクトを開く"

    path: bpy.props.StringProperty(subtype='DIR_PATH', options={'HIDDEN'})

    def execute(self, context):
        path = (self.path or "").strip()
        if not path:
            self.report({'WARNING'}, "パスが空です")
            return {'CANCELLED'}
        # 選んだ瞬間に実体を確かめる(ボタン操作なのでI/Oしてよい文脈)。
        # 一覧は少し古くなることがあるため、ここが最後の関門になる
        try:
            root = core.resolve_root(path)
        except PipelineError:
            projects.forget(path)
            refresh()
            self.report({'ERROR'},
                        f"フォルダが見つかりません(一覧から外しました): {path}")
            return {'CANCELLED'}
        _apply_root(context, str(root))
        remember(str(root))
        self.report({'INFO'}, f"プロジェクトを開きました: {projects.display_name(str(root))}")
        return {'FINISHED'}


class MUSUBI_OT_open_last_project(bpy.types.Operator):
    """前回のプロジェクトを開く。File > New でルートが空になったときの戻り道"""
    bl_idname = "musubi.open_last_project"
    bl_label = "前回のプロジェクトを開く"

    @classmethod
    def poll(cls, context):
        # poll はボタンを描くたびに呼ばれる。ここでファイルを読んではいけない
        # ので、キャッシュ済みの一覧だけを見る
        return bool(_menu_cache)

    def execute(self, context):
        entries = projects.load()
        if not entries:
            self.report({'WARNING'}, "履歴がありません")
            return {'CANCELLED'}
        return bpy.ops.musubi.open_project(path=entries[0]["path"])


class MUSUBI_OT_forget_project(bpy.types.Operator):
    """この項目を一覧から消す(プロジェクトのファイルは消えません)"""
    bl_idname = "musubi.forget_project"
    bl_label = "一覧から消す"

    path: bpy.props.StringProperty(options={'HIDDEN'})

    def execute(self, context):
        projects.forget(self.path)
        refresh()
        self.report({'INFO'}, "一覧から消しました(フォルダはそのままです)")
        return {'FINISHED'}


class MUSUBI_OT_prune_projects(bpy.types.Operator):
    """無くなったフォルダを一覧から片付ける"""
    bl_idname = "musubi.prune_projects"
    bl_label = "無くなったものを整理"

    def execute(self, context):
        before = len(projects.load())
        after = len(projects.prune_missing())
        refresh()
        self.report({'INFO'}, f"{before - after}件を整理しました")
        return {'FINISHED'}


class MUSUBI_MT_projects(bpy.types.Menu):
    """開いたことのあるプロジェクトの一覧。"""
    bl_idname = "MUSUBI_MT_projects"
    bl_label = "プロジェクトを開く"

    def draw(self, context):
        layout = self.layout
        items = cached()
        if not items:
            layout.label(text="履歴がありません", icon='INFO')
            return
        cur = _current_key(context)
        for e in items:
            row = layout.row()
            row.enabled = projects.dedup_key(e["path"]) != cur
            label = e["name"]
            if e["peers"]:
                label += f"  ({e['peers']}人と共有)"
            elif e["shared"]:
                label += "  (共有相手なし)"
            icon = 'PAUSE' if e["paused"] else 'FILE_FOLDER'
            row.operator("musubi.open_project", text=label,
                         icon=icon).path = e["path"]
        layout.separator()
        layout.operator("musubi.prune_projects", icon='TRASH')


def _current_key(context) -> str:
    """現在のルートの比較用キー(空なら空文字)。"""
    try:
        return projects.dedup_key(context.scene.musubi_project_root or "")
    except AttributeError:
        return ""


def draw_open_project(layout, context) -> None:
    """ルート未設定のときに出す「開く」導線。プロジェクトパネルから呼ばれる。"""
    items = cached()
    if items:
        top = items[0]
        big = layout.column()
        big.scale_y = 1.5
        big.operator("musubi.open_last_project",
                     text=f"前回のプロジェクトを開く: {top['name']}",
                     icon='FILE_FOLDER')
        if len(items) > 1:
            layout.menu("MUSUBI_MT_projects",
                        text=f"ほかのプロジェクト({len(items) - 1}件)",
                        icon='DOWNARROW_HLT')


CLASSES = (
    MUSUBI_OT_open_project,
    MUSUBI_OT_open_last_project,
    MUSUBI_OT_forget_project,
    MUSUBI_OT_prune_projects,
    MUSUBI_MT_projects,
)
