# SPDX-FileCopyrightText: 2026 mochizukimonta
# SPDX-License-Identifier: GPL-3.0-or-later
"""Musubi Pipeline — Blenderチーム制作パイプラインアドオン。

- プロジェクト構造 (assets/char|bg|prop, scenes/sceneXX/cYY.blend, output)
- バージョン付きレンダリング出力 (output/sceneXX/cYY.NNN.mp4)
- Syncthing の自動セットアップ(検証付きダウンロード・起動・フォルダ共有・
  デバイス承認)によるP2P同期
- 同期整合性検証(SHA-256照合リスト)・競合検出・アドバイザリロック・
  セキュリティ監査

同期プロトコル自体は実績ある Syncthing に委任する。アドオンの外部通信は
「公式GitHubからのバイナリ取得(初回のみ・SHA-256検証)」と
「127.0.0.1 のローカルREST API」に限定される(セキュリティ設計上の意図)。
"""

# バージョンはここが単一の出所。blender_manifest.toml にも書かれており
# (Blender拡張の仕様上必須)、syncthing.py の User-Agent はここから生成する。
# 食い違うと配布物と実行時の申告がズレるため、tests/test_manifest.py が
# 3者の一致を自動検証している。どれか1つだけ直すとCIが赤くなる。
bl_info = {
    "name": "Musubi Pipeline",
    "author": "mochizukimonta",
    "version": (0, 29, 0),
    "blender": (4, 2, 0),
    "location": "3D Viewport > Sidebar > Musubi",
    "description": "チーム制作パイプライン(フォルダ構造・カット管理・同期検証)",
    "category": "Pipeline",
}

# core / sync はBlender外(検証スクリプト等)でも使えるようにする
try:
    import bpy
    from . import (ops, project_ops, quality_ops, reel, review_ops, spec_ops,
                   st_ops, task_ops, ui, ver_ops)
except ModuleNotFoundError:  # Blender外での import(bpyなし)
    bpy = None


def _scene_props():
    bpy.types.Scene.musubi_project_root = bpy.props.StringProperty(
        name="プロジェクトルート",
        description="チームで同期するプロジェクトフォルダ",
        subtype='DIR_PATH',
        default="",
        update=st_ops.on_root_update,  # 既存プロジェクトなら同期IDを自動入力
    )
    bpy.types.Scene.musubi_scene_no = bpy.props.IntProperty(
        name="シーン番号", default=1, min=1, max=999)
    bpy.types.Scene.musubi_cut_no = bpy.props.IntProperty(
        name="カット番号", default=1, min=1, max=999)
    bpy.types.Scene.musubi_sync_id = bpy.props.StringProperty(
        name="同期ID",
        description="チーム全員で同じ値にする共有フォルダID(英小文字・数字・ハイフン)",
        default="musubi-project")
    bpy.types.Scene.musubi_leader_id = bpy.props.StringProperty(
        name="リーダーのデバイスID",
        description="リーダーから受け取ったSyncthingデバイスIDを貼り付ける",
        default="")
    bpy.types.Scene.musubi_st_lan_only = bpy.props.BoolProperty(
        name="LAN/VPN内のみ(推奨)",
        description="リレー・グローバル探索を無効化し、外部サーバへの露出をなくす。"
                    "インターネット越しの場合はTailscale等のVPNと併用",
        default=False)
    bpy.types.Scene.musubi_version_keep = bpy.props.IntProperty(
        name="保持世代数", default=20, min=1, max=200,
        description="「古い履歴を整理」で残す世代数")
    # コメントはその場のメモなのでファイルに保存しない(WindowManager)。
    # 記録された時点で自動で空欄に戻る(古いコメントの混入防止)
    bpy.types.WindowManager.musubi_version_comment = bpy.props.StringProperty(
        name="バージョンコメント",
        description="次の保存/スナップショットで履歴に記録するメモ。"
                    "記録されると自動で空欄に戻ります",
        default="")
    bpy.types.WindowManager.musubi_last_save_at = bpy.props.StringProperty(
        default="")
    bpy.types.WindowManager.musubi_lock_warning = bpy.props.StringProperty(
        default="")
    # 警告中のロックが放置ロック(12時間超)かどうか。解除ボタンの表示条件。
    # 経過時間の判定は ops 側で1回だけ行い、UI は真偽値を見るだけにする
    bpy.types.WindowManager.musubi_lock_stale = bpy.props.BoolProperty(
        default=False)
    bpy.types.WindowManager.musubi_lib_outdated = bpy.props.StringProperty(
        default="")
    bpy.types.WindowManager.musubi_last_report = bpy.props.StringProperty(
        name="最終レポート", default="")
    bpy.types.WindowManager.musubi_st_status = bpy.props.StringProperty(
        name="同期状態", default="")
    bpy.types.WindowManager.musubi_invite_path = bpy.props.StringProperty(
        name="招待ファイルの場所", default="")
    bpy.types.WindowManager.musubi_st_show_advanced = bpy.props.BoolProperty(
        name="詳細(手動操作)", default=False,
        description="デバイスIDの手動コピペなど、上級者向けの操作を表示")
    bpy.types.WindowManager.musubi_show_other_projects = bpy.props.BoolProperty(
        name="ほかに参加中のプロジェクト", default=False,
        description="いま開いていないプロジェクトの同期状態も表示する")
    bpy.types.WindowManager.musubi_versions = bpy.props.CollectionProperty(
        type=ver_ops.MusubiVersionItem)
    bpy.types.WindowManager.musubi_versions_index = bpy.props.IntProperty(
        default=0)
    bpy.types.WindowManager.musubi_versions_summary = bpy.props.StringProperty(
        default="")
    bpy.types.WindowManager.musubi_board = bpy.props.CollectionProperty(
        type=task_ops.MusubiBoardItem)
    bpy.types.WindowManager.musubi_board_index = bpy.props.IntProperty(
        default=0)
    bpy.types.WindowManager.musubi_board_summary = bpy.props.StringProperty(
        default="")
    bpy.types.WindowManager.musubi_board_filter = bpy.props.EnumProperty(
        name="絞り込み",
        items=([('ALL', "すべて", "")] + task_ops.STATUS_ENUM),
        default='ALL',
        update=lambda self, ctx: task_ops.refresh_board(ctx))
    bpy.types.WindowManager.musubi_reviews = bpy.props.CollectionProperty(
        type=review_ops.MusubiReviewItem)
    bpy.types.WindowManager.musubi_reviews_index = bpy.props.IntProperty(
        default=0)
    bpy.types.WindowManager.musubi_qc_report = bpy.props.StringProperty(
        name="品質チェック結果", default="")
    bpy.types.WindowManager.musubi_spec_summary = bpy.props.StringProperty(
        default="")
    bpy.types.WindowManager.musubi_spec_profiles = bpy.props.StringProperty(
        default="")
    bpy.types.WindowManager.musubi_spec_show_final = bpy.props.BoolProperty(
        name="本番の仕様値", default=False)
    bpy.types.WindowManager.musubi_spec_show_preview = bpy.props.BoolProperty(
        name="仮の仕様値", default=False)


def _del_scene_props():
    for attr in ("musubi_project_root", "musubi_scene_no", "musubi_cut_no",
                 "musubi_sync_id", "musubi_leader_id", "musubi_st_lan_only",
                 "musubi_version_keep"):
        if hasattr(bpy.types.Scene, attr):
            delattr(bpy.types.Scene, attr)
    for attr in ("musubi_version_comment", "musubi_last_save_at",
                 "musubi_lock_warning", "musubi_lock_stale",
                 "musubi_lib_outdated",
                 "musubi_last_report",
                 "musubi_st_status", "musubi_invite_path",
                 "musubi_st_show_advanced", "musubi_show_other_projects",
                 "musubi_versions",
                 "musubi_versions_index", "musubi_versions_summary",
                 "musubi_board", "musubi_board_index", "musubi_board_summary",
                 "musubi_board_filter", "musubi_reviews",
                 "musubi_reviews_index", "musubi_qc_report",
                 "musubi_spec_summary", "musubi_spec_profiles",
                 "musubi_spec_show_final", "musubi_spec_show_preview"):
        if hasattr(bpy.types.WindowManager, attr):
            delattr(bpy.types.WindowManager, attr)


def register():
    # PropertyGroupを先に登録してからプロパティを定義する
    for cls in (ver_ops.CLASSES + task_ops.CLASSES + review_ops.CLASSES
                + quality_ops.CLASSES + reel.CLASSES + spec_ops.CLASSES
                + ops.CLASSES + project_ops.CLASSES + st_ops.CLASSES
                + ui.CLASSES):
        bpy.utils.register_class(cls)
    _scene_props()
    # すでにルートが入ったファイルを開いた状態で有効化された場合、それを
    # 履歴の種にする(旧版からの更新直後に一覧が空にならないように)
    try:
        project_ops.remember(bpy.context.scene.musubi_project_root)
    except (AttributeError, TypeError):
        pass
    # プロジェクト履歴をここで1度だけ読む(以降は開いた時・押した時に更新)
    project_ops.refresh()
    # 同期状態の更新はイベント駆動(オープン時・保存時・ボタン時のみ)。
    # 常設タイマー・常駐スレッドは持たない(クリエイターの作業を妨げない)
    if st_ops.on_file_load not in bpy.app.handlers.load_post:
        bpy.app.handlers.load_post.append(st_ops.on_file_load)
    # 開いた瞬間のロック確認・自動取得(開いた人=作業中)
    if ops.on_load_post not in bpy.app.handlers.load_post:
        bpy.app.handlers.load_post.append(ops.on_load_post)
    if st_ops.on_file_save not in bpy.app.handlers.save_post:
        bpy.app.handlers.save_post.append(st_ops.on_file_save)
    # Ctrl+S をパイプライン保存にする(自動スナップショット+カット管理)
    if ops.on_save_post not in bpy.app.handlers.save_post:
        bpy.app.handlers.save_post.append(ops.on_save_post)


def unregister():
    import atexit
    for handler_list, fn in ((bpy.app.handlers.load_post, st_ops.on_file_load),
                             (bpy.app.handlers.load_post, ops.on_load_post),
                             (bpy.app.handlers.save_post, st_ops.on_file_save),
                             (bpy.app.handlers.save_post, ops.on_save_post)):
        if fn in handler_list:
            handler_list.remove(fn)
    for tick in (st_ops._apply_tick, st_ops._watch_tick):
        if bpy.app.timers.is_registered(tick):
            bpy.app.timers.unregister(tick)
    # アドオン無効化時もこのセッションで起動したSyncthingは停止する
    st_ops._shutdown_on_blender_exit()
    atexit.unregister(st_ops._shutdown_on_blender_exit)
    # 自分の保持するロックも返す
    ops._release_current_lock()
    atexit.unregister(ops._release_current_lock)
    _del_scene_props()
    for cls in reversed(ver_ops.CLASSES + task_ops.CLASSES
                        + review_ops.CLASSES + quality_ops.CLASSES
                        + reel.CLASSES + spec_ops.CLASSES + ops.CLASSES
                        + project_ops.CLASSES + st_ops.CLASSES + ui.CLASSES):
        bpy.utils.unregister_class(cls)
