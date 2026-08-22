# SPDX-FileCopyrightText: 2026 mochizukimonta
# SPDX-License-Identifier: GPL-3.0-or-later
"""musubi_pipeline.ver_ops — バージョン管理のBlenderオペレーターとUI部品。"""

from __future__ import annotations

from pathlib import Path

import bpy

from . import core, sync, versions
from .core import PipelineError

# 世代サムネイル用のプレビューコレクション。register/unregister で作り捨てる
# (アドオン再読込のたびに作ると、GPU側のアイコンが解放されずに溜まる)。
_pcoll = None


def preview_register():
    """サムネイル用のプレビューコレクションを1つだけ作る。"""
    global _pcoll
    # ヘッドレスではアイコンが割り当てられず icon_id が常に 0 になるため、
    # 作るだけ無駄(レンダーファームでの実行を重くしない)
    if _pcoll is None and not bpy.app.background:
        from bpy.utils import previews
        _pcoll = previews.new()


def preview_unregister():
    global _pcoll
    if _pcoll is not None:
        from bpy.utils import previews
        previews.remove(_pcoll)
        _pcoll = None


def _load_preview(key: str, path: Path) -> int:
    """.blend の埋め込みプレビューを読み、アイコンIDを返す(無ければ0)。

    Blender側がヘッダーのサムネイル部だけを読むので、大きな .blend でも
    全読みは起きない(82MBのファイルで load と判定あわせて 0.5ミリ秒)。

    引数はキーワードで渡してはいけない。4.2〜4.5 は (name, path, path_type)、
    5.x は (name, filepath, file_type) と名前が違い、片方で TypeError になる。
    """
    if _pcoll is None:
        return 0
    try:
        prev = _pcoll.load(key, str(path), 'BLEND')
        # load はプレビューが無いファイルでも、存在しないファイルでも例外を
        # 投げず、icon_id にも非0が割り当たる。有無を判別できるのは
        # image_size だけ(あり=(256,256) / なし=(0,0))。
        if prev.image_size[0] <= 0:
            return 0
        return prev.icon_id
    except Exception:
        return 0


class MusubiVersionItem(bpy.types.PropertyGroup):
    label: bpy.props.StringProperty()
    version_name: bpy.props.StringProperty()
    # サムネイルのアイコンID。プレビューは refresh のたびに読み直され、
    # そのつど新しいIDが割り当たるので、前回の値は使い回せない
    icon_id: bpy.props.IntProperty(default=0)


class MUSUBI_UL_versions(bpy.types.UIList):
    def draw_item(self, context, layout, data, item, icon,
                  active_data, active_propname):
        if item.icon_id:
            layout.label(text=item.label, icon_value=item.icon_id)
        else:
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
    wm.musubi_versions_no_thumb = False
    if _pcoll is not None:
        # サムネイルのキャッシュはここで必ず捨てる。この関数は
        # _current_file() の履歴しか読まないので、clear と load が常に対で
        # 回り、いま開いているファイルの世代分(既定20)しか溜まらない。
        # 復元は open_mainfile を挟むが、その直後もこの関数を通るため、
        # 対象ファイルが変わったときの取りこぼしもここで塞がる。
        _pcoll.clear()
    try:
        path = _current_file(context)
        items = versions.list_versions(context.scene.musubi_project_root, path)
    except PipelineError as e:
        wm.musubi_versions_summary = str(e)
        return
    for i, v in enumerate(items):
        m = v["meta"]
        date = m.get("saved_at", v["name"][:15])[5:16].replace("T", " ")
        row = wm.musubi_versions.add()
        row.version_name = v["name"]
        row.label = (f"{date} {m.get('author','?')} "
                     f"{m.get('comment','') or '(コメントなし)'}")
        # 同一秒の連番などで名前が重なってもキーが衝突しないよう番号を足す
        row.icon_id = _load_preview(f"{i}:{v['name']}", v["path"])
        if not row.icon_id:
            wm.musubi_versions_no_thumb = True
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


def _selected_version(wm):
    """一覧で選択中の世代。未選択なら None。"""
    idx = wm.musubi_versions_index
    if not wm.musubi_versions or idx < 0 or idx >= len(wm.musubi_versions):
        return None
    return wm.musubi_versions[idx]


def _cut_of_current_file(context):
    """いま開いているファイルがカットなら (シーン番号, カット番号)。"""
    try:
        root = core.resolve_root(context.scene.musubi_project_root)
    except PipelineError:
        return None
    return core.parse_cut_path(root, bpy.data.filepath)


class MUSUBI_OT_version_restore(bpy.types.Operator):
    """選択したバージョンに復元する(いまの内容は自動で履歴に退避されます)"""
    bl_idname = "musubi.version_restore"
    bl_label = "このバージョンに復元"

    # invoke で読み取った内容を draw に渡す(draw では I/O をしない)
    target_label: bpy.props.StringProperty(options={'HIDDEN'})
    unsaved: bpy.props.BoolProperty(options={'HIDDEN'})
    approved: bpy.props.BoolProperty(options={'HIDDEN'})
    reset_status: bpy.props.BoolProperty(
        name="ステータスを「作業中」に戻す", default=False,
        description="ボードが承認済みと表示したまま、中身が承認された世代では"
                    "ない、という食い違いを防ぐ。チェックしなければ"
                    "ステータスは承認済みのまま変わりません")

    def invoke(self, context, event):
        item = _selected_version(context.window_manager)
        if item is None:
            self.report({'ERROR'}, "復元するバージョンを選択してください")
            return {'CANCELLED'}
        self.target_label = item.label
        self.unsaved = bool(bpy.data.is_dirty)
        self.approved = False
        self.reset_status = False
        nums = _cut_of_current_file(context)
        if nums:
            try:
                from . import tasks
                st = tasks.read_status(context.scene.musubi_project_root,
                                       *nums)
                self.approved = st["status"] == "approved"
            except (PipelineError, OSError):
                pass
        return context.window_manager.invoke_props_dialog(self, width=440)

    def draw(self, context):
        col = self.layout.column(align=True)
        col.label(text="この世代に復元します:", icon='LOOP_BACK')
        col.label(text=self.target_label, icon='BLANK1')
        if self.unsaved:
            col.label(text="未保存の変更は履歴に退避してから復元します",
                      icon='BLANK1')
        if self.approved:
            note = self.layout.column(align=True)
            note.label(text="このカットは承認済みです。復元しても",
                       icon='ERROR')
            note.label(text="ステータスは承認済みのままです", icon='BLANK1')
            self.layout.prop(self, "reset_status")

    def execute(self, context):
        from . import ops as ops_mod
        item = _selected_version(context.window_manager)
        if item is None:
            self.report({'ERROR'}, "復元するバージョンを選択してください")
            return {'CANCELLED'}
        # 一覧は復元後に作り直されるので、必要な値はここで取り出しておく
        version_name, label = item.version_name, item.label
        root = context.scene.musubi_project_root
        nums = _cut_of_current_file(context)
        ops_mod._suppress_auto = True  # 自動保存処理と二重にしない
        try:
            path = _current_file(context)
            sync.acquire_lock(root, path)  # 他端末が編集中なら中止
            # 未保存の編集を先にディスクへ載せる。execute から open_mainfile を
            # 呼ぶと保存確認ダイアログが出ないため、ここで保存しないと黙って
            # 捨てられる。続く restore の自動スナップショットがこの内容を
            # 拾うので、履歴に1世代として残り、あとから戻せる
            if bpy.data.is_dirty:
                ops_mod.save_mainfile_retry()
            r = versions.restore(root, path, version_name)
            bpy.ops.wm.open_mainfile(filepath=str(path))
        except (PipelineError, RuntimeError) as e:
            self.report({'ERROR'}, str(e))
            return {'CANCELLED'}
        finally:
            ops_mod._suppress_auto = False

        extra = ""
        if self.reset_status and nums:
            try:
                from . import board_html, tasks
                tasks.update_status(root, nums[0], nums[1], status="wip")
                board_html.export_board_html(root)
                extra = "/ステータスを作業中に戻しました"
            except (PipelineError, OSError) as e:
                self.report({'WARNING'}, f"ステータスを更新できませんでした: {e}")
        # 復元しても履歴は消えないので、一覧の先頭は「復元前の自動
        # スナップショット」= 進んでいた内容であって、いま開いている内容では
        # ない。サムネイルが付くと先頭の絵と画面の絵の食い違いが目立つため、
        # 次の通常保存まで「復元中」を出す(open_mainfile が on_load_post で
        # 一度消すので、必ずその後に入れる)
        wm = context.window_manager
        wm.musubi_restored_label = label
        refresh_list(context)
        # refresh_list は既定で先頭行を選ぶが、復元直後の先頭は上記のとおり
        # 「復元前の自動スナップショット」で、画面の内容ではない。いちばん
        # 目立つ大きなサムネイルだけが画面と食い違って見えるので、復元した
        # 世代を選び直し、サムネイル・「復元中」表示・画面を一致させる
        for i, row in enumerate(wm.musubi_versions):
            if row.version_name == version_name:
                wm.musubi_versions_index = i
                break
        self.report({'INFO'},
                    f"復元しました: {r['meta'].get('comment') or version_name}"
                    f"(直前の状態も履歴に退避済み){extra}")
        return {'FINISHED'}


class MUSUBI_OT_enable_blend_previews(bpy.types.Operator):
    """Blenderの「保存時に.blendへプレビューを埋め込む」設定を有効にする
(サムネイル表示に必要。Musubiが黙って設定を書き換えないための明示ボタン)"""
    bl_idname = "musubi.enable_blend_previews"
    bl_label = "プレビュー保存を有効にする"

    def invoke(self, context, event):
        return context.window_manager.invoke_confirm(self, event)

    def execute(self, context):
        paths = context.preferences.filepaths
        if not hasattr(paths, "file_preview_type"):
            self.report({'ERROR'}, "このBlenderには該当の設定がありません")
            return {'CANCELLED'}
        paths.file_preview_type = 'AUTO'
        msg = "有効にしました(次の保存分からサムネイルが記録されます)"
        if not context.preferences.use_preferences_save:
            msg += "/プリファレンスの自動保存が切れているため今回限りです"
        self.report({'INFO'}, msg)
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
    MUSUBI_OT_enable_blend_previews,
    MUSUBI_OT_versions_prune,
    MUSUBI_OT_versions_prune_all,
    MUSUBI_OT_versions_enforce_cap,
)
