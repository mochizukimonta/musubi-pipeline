# SPDX-FileCopyrightText: 2026 mochizukimonta
# SPDX-License-Identifier: GPL-3.0-or-later
"""musubi_pipeline.spec_ops — プロジェクト仕様(レンダープロファイル)のUI。

設計(v0.14で0ベース再設計):
- 仕様の本体は「本番」「仮」の2プロファイル(型付きの実設定)だけ。
  行を追加して書く表は廃止した
- 編集は「シーンから取り込み」一本(手入力なし)
- 表示はBlenderの実際のUI配置でグループ化したチェックリスト。
  現在のシーン値が仕様と違う項目は赤く「≠ 現在値」を添えて表示し、
  仕様書がそのまま生きたチェックリストとして機能する
"""

from __future__ import annotations

import json

import bpy

from . import render_profiles, spec, task_ops
from .core import PipelineError

PROFILE_ENUM = [
    ("final", "本番", "納品用のレンダー設定(EXR連番など)"),
    ("preview", "仮(プレビュー)", "チェック用の軽い設定(mp4など)"),
]


def refresh_spec(context):
    """仕様を読み込んでパネル用キャッシュへ(描画中のディスクI/O回避)。"""
    wm = context.window_manager
    try:
        data = spec.load(context.scene.musubi_project_root)
    except PipelineError as e:
        wm.musubi_spec_summary = str(e)
        wm.musubi_spec_profiles = ""
        return
    f = data["render_profiles"].get("final", {}).get("settings", {})
    wm.musubi_spec_summary = (
        f"{data['title']} | 本番: {f.get('engine', '?')} "
        f"{f.get('resolution_x', '?')}×{f.get('resolution_y', '?')} "
        f"{f.get('fps', '?')}fps {f.get('output_format', '?')}"
        + (f" | 更新: {data['updated_at'][5:16].replace('T', ' ')} "
           f"{data['updated_by']}" if data.get("updated_at") else ""))
    wm.musubi_spec_profiles = json.dumps(
        {"profiles": data["render_profiles"], "notes": data.get("notes", "")},
        ensure_ascii=False)


class MUSUBI_OT_spec_refresh(bpy.types.Operator):
    """プロジェクト仕様を読み込んで表示"""
    bl_idname = "musubi.spec_refresh"
    bl_label = "仕様を更新"

    def execute(self, context):
        refresh_spec(context)
        return {'FINISHED'}


class MUSUBI_OT_profile_apply(bpy.types.Operator):
    """レンダー仕様テンプレートを現在のシーンに一括適用する
(エンジン・サンプル数・パス・Cryptomatte・出力形式・fpsなど)"""
    bl_idname = "musubi.profile_apply"
    bl_label = "レンダー仕様を適用"

    profile: bpy.props.EnumProperty(items=PROFILE_ENUM, name="プロファイル")

    def execute(self, context):
        from . import quality_ops
        try:
            data = spec.load(context.scene.musubi_project_root)
            prof = data["render_profiles"].get(self.profile)
            if not prof:
                raise PipelineError(f"プロファイルがありません: {self.profile}")
            applied, failed = render_profiles.apply_profile(
                prof["settings"], context.scene, context.view_layer)
        except PipelineError as e:
            self.report({'ERROR'}, str(e))
            return {'CANCELLED'}
        quality_ops.run_and_store(context)  # 赤アラートを即時更新
        refresh_spec(context)
        msg = f"「{prof['label']}」を適用({applied}項目)"
        if failed:
            msg += f" / 失敗{len(failed)}件: {failed[0]}"
            self.report({'WARNING'}, msg)
        else:
            self.report({'INFO'}, msg)
        return {'FINISHED'}


class MUSUBI_OT_profile_capture(bpy.types.Operator):
    """現在のシーンのレンダー設定を読み取り、仕様テンプレートとして保存する
(シーンをBlenderの通常UIで整えてから押す。手入力は不要)"""
    bl_idname = "musubi.profile_capture"
    bl_label = "シーンから仕様に取り込み"

    profile: bpy.props.EnumProperty(items=PROFILE_ENUM, name="取り込み先")

    def invoke(self, context, event):
        return context.window_manager.invoke_confirm(self, event)

    def execute(self, context):
        root = context.scene.musubi_project_root
        try:
            captured = render_profiles.capture(context.scene,
                                               context.view_layer)
            data = spec.load(root)
            prof = data["render_profiles"].setdefault(
                self.profile, {"label": self.profile, "settings": {}})
            prof["settings"] = captured
            spec.save(root, data)
        except PipelineError as e:
            self.report({'ERROR'}, str(e))
            return {'CANCELLED'}
        refresh_spec(context)
        task_ops.export_html_quiet(context)
        self.report({'INFO'},
                    f"現在のシーン設定を「{prof['label']}」として保存しました"
                    f"({len(captured)}項目・全端末へ同期されます)")
        return {'FINISHED'}


class MUSUBI_OT_spec_notes(bpy.types.Operator):
    """備考(映像尺・納品先などの自由メモ)を編集する"""
    bl_idname = "musubi.spec_notes"
    bl_label = "備考を編集"

    notes: bpy.props.StringProperty(name="備考", default="")

    def invoke(self, context, event):
        try:
            self.notes = spec.load(
                context.scene.musubi_project_root).get("notes", "")
        except PipelineError as e:
            self.report({'ERROR'}, str(e))
            return {'CANCELLED'}
        return context.window_manager.invoke_props_dialog(self, width=500)

    def execute(self, context):
        root = context.scene.musubi_project_root
        try:
            data = spec.load(root)
            data["notes"] = self.notes
            spec.save(root, data)
        except PipelineError as e:
            self.report({'ERROR'}, str(e))
            return {'CANCELLED'}
        refresh_spec(context)
        task_ops.export_html_quiet(context)
        return {'FINISHED'}


def _draw_checklist(box, context, prof: dict):
    """1プロファイルのチェックリスト(グループ見出し+仕様値+シーン差分)。"""
    settings = prof.get("settings", {})
    sc, vl = context.scene, context.view_layer
    fv = render_profiles.format_value
    engine = settings.get("engine", "")
    for group in render_profiles.groups():
        rows = []
        for key, (g, label, getter, _s) in render_profiles.SETTINGS.items():
            if g != group or key not in settings:
                continue
            if key in render_profiles._CYCLES_ONLY and engine != 'CYCLES':
                continue
            spec_val = settings[key]
            try:
                cur = getter(sc, vl)
                mismatch = not render_profiles.values_equal(cur, spec_val)
            except (AttributeError, TypeError):
                cur, mismatch = None, False
            rows.append((label, spec_val, cur, mismatch))
        if not rows:
            continue
        box.label(text=group, icon='DISCLOSURE_TRI_DOWN')
        col = box.column(align=True)
        for label, spec_val, cur, mismatch in rows:
            if mismatch:
                # 仕様との差は「注意」— 仮作業中は正常なので赤にはしない
                col.label(text=f"    {label}: {fv(spec_val)}  ≠ 現在 {fv(cur)}",
                          icon='ERROR')
            else:
                col.label(text=f"    {label}: {fv(spec_val)}", icon='BLANK1')


def draw_spec_box(layout, context):
    """プロジェクト仕様パネルの中身(見出しはui.py側のパネルヘッダー)。"""
    wm = context.window_manager
    box = layout.column()
    if wm.musubi_spec_summary:
        for i in range(0, len(wm.musubi_spec_summary), 42):
            box.label(text=wm.musubi_spec_summary[i:i+42])

    row = box.row(align=True)
    row.operator("musubi.profile_apply", text="本番を適用",
                 icon='RENDER_STILL').profile = "final"
    row.operator("musubi.profile_apply", text="仮を適用",
                 icon='RENDER_ANIMATION').profile = "preview"
    row.operator("musubi.spec_refresh", text="", icon='FILE_REFRESH')
    box.operator_menu_enum("musubi.profile_capture", "profile",
                           text="シーンから仕様に取り込み", icon='IMPORT')

    try:
        cache = json.loads(wm.musubi_spec_profiles)
        profs = cache.get("profiles", {})
        notes = cache.get("notes", "")
    except (json.JSONDecodeError, TypeError):
        box.label(text="「仕様を更新」を押してください", icon='INFO')
        return

    for pid, toggle in (("final", "musubi_spec_show_final"),
                        ("preview", "musubi_spec_show_preview")):
        prof = profs.get(pid)
        if not prof:
            continue
        row = box.row()
        row.prop(wm, toggle,
                 icon='TRIA_DOWN' if getattr(wm, toggle) else 'TRIA_RIGHT',
                 text=prof.get("label", pid), emboss=False)
        if getattr(wm, toggle):
            _draw_checklist(box, context, prof)

    row = box.row()
    row.operator("musubi.spec_notes", icon='GREASEPENCIL')
    if notes:
        for line in notes.split("\n")[:8]:
            box.label(text=line)


CLASSES = (
    MUSUBI_OT_spec_refresh,
    MUSUBI_OT_profile_apply,
    MUSUBI_OT_profile_capture,
    MUSUBI_OT_spec_notes,
)
