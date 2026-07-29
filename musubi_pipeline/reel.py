# SPDX-FileCopyrightText: 2026 mochizukimonta
# SPDX-License-Identifier: GPL-3.0-or-later
"""musubi_pipeline.reel — デイリーズ/リール生成。

各カットの最新出力mp4をシーケンサーで連結し、カット名ラベルを重ねて
output/reel/reel.NNN.mp4 に1本の通し動画として書き出す。
監督チェック(デイリーズ)や進捗確認用。
"""

from __future__ import annotations

import re
import time
from pathlib import Path

import bpy

from . import tasks
from .core import PipelineError, cut_name, resolve_root, safe_path, scene_name

REEL_RE = re.compile(r"^reel\.(\d{3})\.mp4$")


def next_reel_path(root_str: str) -> Path:
    root = resolve_root(root_str)
    rdir = safe_path(root, "output", "reel")
    rdir.mkdir(parents=True, exist_ok=True)
    latest = 0
    for f in rdir.iterdir():
        m = REEL_RE.match(f.name)
        if m:
            latest = max(latest, int(m.group(1)))
    if latest >= 999:
        raise PipelineError("リールのバージョンが上限に達しました")
    return safe_path(root, "output", "reel", f"reel.{latest + 1:03d}.mp4")


def collect_clips(root_str: str, approved_only: bool) -> list[dict]:
    """リールに含めるカット(最新出力があるもの)をシーン/カット順で返す。"""
    root = resolve_root(root_str)
    clips = []
    for r in tasks.board(root_str):
        if not r["latest_output"] or r["status"] == "omit":
            continue
        if approved_only and r["status"] != "approved":
            continue
        path = (root / "output" / scene_name(r["scene"]) /
                f"{cut_name(r['cut'])}.{r['latest_output']:03d}.mp4")
        if path.exists():
            clips.append({
                "scene": r["scene"], "cut": r["cut"],
                "version": r["latest_output"], "path": path,
                "label": (f"s{r['scene']:02d}/c{r['cut']:02d} "
                          f"v{r['latest_output']:03d} [{r['status']}]"),
            })
    return clips


def build_reel(context, approved_only: bool = False) -> Path:
    """一時シーンのシーケンサーでリールを組んでレンダリングする。"""
    root_str = context.scene.musubi_project_root
    clips = collect_clips(root_str, approved_only)
    if not clips:
        raise PipelineError("リールに入れる出力mp4がありません"
                            "(承認済みのみ指定の場合は対象ステータスも確認)")
    target = next_reel_path(root_str)
    src_scene = context.scene

    reel_scene = bpy.data.scenes.new("musubi_reel_tmp")
    window = getattr(context, "window", None)
    if window is None and bpy.data.window_managers[0].windows:
        window = bpy.data.window_managers[0].windows[0]
    prev_scene = window.scene if window else None
    try:
        # 描画設定は現在のシーンに合わせる
        reel_scene.render.resolution_x = src_scene.render.resolution_x
        reel_scene.render.resolution_y = src_scene.render.resolution_y
        reel_scene.render.fps = src_scene.render.fps
        rd = reel_scene.render
        if hasattr(rd.image_settings, "media_type"):
            rd.image_settings.media_type = 'VIDEO'
        rd.image_settings.file_format = 'FFMPEG'
        rd.ffmpeg.format = 'MPEG4'
        rd.ffmpeg.codec = 'H264'
        rd.use_file_extension = True
        rd.filepath = str(target)

        reel_scene.sequence_editor_create()
        se = reel_scene.sequence_editor
        # 5.x は strips、4.x は sequences(空でも有効なので hasattr で判定)
        strips = se.strips if hasattr(se, "strips") else se.sequences

        cursor = 1
        for c in clips:
            movie = strips.new_movie(name=c["label"], filepath=str(c["path"]),
                                     channel=1, frame_start=cursor)
            dur = max(1, int(movie.frame_final_duration))
            try:  # カット名ラベル(APIが変わっても本編は出るようにする)
                text = strips.new_effect(
                    name=f"label_{c['label']}", type='TEXT', channel=2,
                    frame_start=cursor, frame_end=cursor + dur)
                text.text = c["label"]
                text.font_size = max(12, src_scene.render.resolution_y // 12)
                text.location = (0.5, 0.06)
            except (AttributeError, TypeError, RuntimeError):
                pass
            cursor += dur
        reel_scene.frame_start = 1
        reel_scene.frame_end = cursor - 1

        start = time.time()
        if window is not None:
            window.scene = reel_scene
            bpy.ops.render.render(animation=True)
        else:  # ウィンドウがない環境(ヘッドレス等)
            with context.temp_override(scene=reel_scene):
                bpy.ops.render.render(animation=True)
        if not target.exists():  # フレーム範囲サフィックス対策
            cands = [p for p in target.parent.glob(target.stem + "*.mp4")
                     if p.stat().st_mtime >= start - 1]
            if cands:
                cands[0].rename(target)
        if not target.exists():
            raise PipelineError("リールの出力ファイルが見つかりません")
    finally:
        if window is not None and prev_scene is not None:
            window.scene = prev_scene
        bpy.data.scenes.remove(reel_scene)
    return target


class MUSUBI_OT_render_reel(bpy.types.Operator):
    """全カットの最新出力を連結したデイリーズリールを output/reel/ に書き出す"""
    bl_idname = "musubi.render_reel"
    bl_label = "デイリーズリールを作成"

    approved_only: bpy.props.BoolProperty(
        name="承認済みカットのみ", default=False,
        description="オンで承認済みのみ、オフで最新出力のある全カット")

    def invoke(self, context, event):
        return context.window_manager.invoke_props_dialog(self)

    def execute(self, context):
        try:
            n = len(collect_clips(context.scene.musubi_project_root,
                                  self.approved_only))
            path = build_reel(context, self.approved_only)
        except PipelineError as e:
            self.report({'ERROR'}, str(e))
            return {'CANCELLED'}
        self.report({'INFO'}, f"リール完成: {path.name}({n}カット)")
        return {'FINISHED'}


CLASSES = (MUSUBI_OT_render_reel,)
