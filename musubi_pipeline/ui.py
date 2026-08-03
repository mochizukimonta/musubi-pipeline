# SPDX-FileCopyrightText: 2026 mochizukimonta
# SPDX-License-Identifier: GPL-3.0-or-later
"""musubi_pipeline.ui — 3Dビューポート サイドバーのパネル。

パネル構成(v0.22で機能グループごとの独立パネルに再編):

1. プロジェクト        — 全テンプレート共通の要。最上部・特別扱い
2. プロジェクト仕様    — チームで共有する設定値
3. 品質チェック        — ファイルの健康状態(映像でもアセット制作でも使う)
4. バージョン管理      — 全テンプレート共通(アセットの.blendにも効く)
5. 映像制作(制作進行) — カット作業/進行ボード/レビューのサブパネル群。
                          アセット制作ではグループごと折りたためる
6. チーム同期          — 意図的に末尾固定(検証・セキュリティはサブパネル)

各パネルは折りたたみ可能で、開閉状態はBlenderが記憶する。

プリファレンスの役割スイッチ(制作者/レビュアー)で表示範囲が変わる:
レビュアーでは 2〜4 とカット作業を隠し、プロジェクト・進行ボード・
レビュー・チーム同期だけを出す(確認と指示が中心の人向け)。
"""

from __future__ import annotations

import json
import time
from pathlib import Path

import bpy


def _is_reviewer(context) -> bool:
    """プリファレンスの役割がレビュアーか(読めないときは制作者扱い)。"""
    try:
        prefs = context.preferences.addons[__package__].preferences
        return prefs.role == 'reviewer'
    except (KeyError, AttributeError):
        return False


def _wrap(col, text: str, width: int = 38, indent: str = "  "):
    """長いパス等を折り返して全体を見せる(切り捨てて隠さない)。"""
    for i in range(0, len(text), width):
        col.label(text=indent + text[i:i + width])


# ---------------------------------------------------------------------------
# プロジェクト構成の自動判別(パネルに「今どのテンプレートの現場か」を示す)
# ---------------------------------------------------------------------------

_flavor_cache: dict[str, tuple[float, tuple[str, str, str]]] = {}

# 種別: (ラベル, アイコン)。アイコンは既存コードで実績のあるものだけ使う
# (Blender 5.xで廃止アイコンによるクラッシュ前歴があるため)
_FLAVORS = {
    "film": ("映像制作(シーン・カット)", 'RENDER_ANIMATION'),
    "asset": ("アセット制作(持ち寄り・リンク)", 'PACKAGE'),
    "free": ("自由構成(最小)", 'FILE_FOLDER'),
    "new": ("未セットアップ(下から構造を作成)", 'INFO'),
    "unknown": ("確認できません", 'ERROR'),
}


def _project_flavor(root_str: str) -> tuple[str, str, str]:
    """(種別key, 構成ラベル, アイコン) を返す。

    draw()は再描画のたびに呼ばれるため、ディスクアクセスは5秒キャッシュ。
    """
    hit = _flavor_cache.get(root_str)
    now = time.monotonic()
    if hit and now - hit[0] < 5.0:
        return hit[1]
    try:
        root = Path(root_str)
        if (root / "scenes").is_dir():
            kind = "film"
        elif (root / "assets").is_dir():
            kind = "asset"
        elif (root / ".musubi").is_dir():
            kind = "free"
        else:
            kind = "new"
    except OSError:
        kind = "unknown"
    result = (kind,) + _FLAVORS[kind]
    _flavor_cache[root_str] = (now, result)
    return result


def _is_film_project(root_str: str) -> bool:
    return _project_flavor(root_str)[0] == "film"


# ---------------------------------------------------------------------------
# チーム同期の描画部品
# ---------------------------------------------------------------------------

def _draw_st_overview(box, rep, sc):
    """概要カード: どのID・どのフォルダ・自分は誰か、を常に最上部に見せる。"""
    s = (rep.get("s") or {})
    card = box.box()
    if s.get("folder_ok"):
        card.label(text=f"同期ID: {sc.musubi_sync_id}", icon='LINKED')
    else:
        card.prop(sc, "musubi_sync_id", text="同期ID(チームで同じ値)",
                  icon='LINKED')
        card.prop(sc, "musubi_st_lan_only")
    col = card.column(align=True)
    col.label(text="共有フォルダ:", icon='FILE_FOLDER')
    _wrap(col, sc.musubi_project_root or "(未設定)")
    my = rep.get("my_id", "")
    if my:
        row = card.row(align=True)
        row.label(text=f"自分のID: {my[:15]}…", icon='USER')
        row.operator("musubi.st_copy_id", text="", icon='COPYDOWN')


def _draw_invite(box, wm):
    """招待ファイルの場所カード(送るための操作ボタン付き)。"""
    if not wm.musubi_invite_path:
        return
    import os
    inv = box.box()
    inv.label(text="招待ファイル(これをメンバーに送る):", icon='EXPORT')
    col = inv.column(align=True)
    _wrap(col, wm.musubi_invite_path)
    row = inv.row(align=True)
    row.operator("wm.path_open", text="場所を開く", icon='FILEBROWSER'
                 ).filepath = os.path.dirname(wm.musubi_invite_path)
    row.operator("musubi.copy_text", text="パスをコピー", icon='COPYDOWN'
                 ).text = wm.musubi_invite_path


# 状態を示す色付きドット。Blenderのラベルは文字色を自由に変えられないため、
# 色は色付きアイコンで表す。COLLECTION_COLOR_* は 4.2〜5.x で使える
# (STRIP_COLOR_* は 4.3 に存在しないので使わない)。
_DOT_STOPPED = 'COLLECTION_COLOR_03'   # 黄: 一時停止中
_DOT_ALONE = 'COLLECTION_COLOR_01'     # 赤: 誰とも共有していない
_DOT_TROUBLE = 'COLLECTION_COLOR_02'   # 橙: 同期できないファイルがある
_DOT_SYNCING = 'COLLECTION_COLOR_05'   # 青: 転送中
_DOT_OK = 'COLLECTION_COLOR_04'        # 緑: 待機中(同期済み)


def _folder_dot(f) -> str:
    """このプロジェクトの状態を1つのドットに落とす(重い順に判定)。"""
    if f.get("paused"):
        return _DOT_STOPPED
    if not (f.get("devices") or []):
        return _DOT_ALONE
    if f.get("errors"):
        return _DOT_TROUBLE
    if f.get("need_bytes"):
        return _DOT_SYNCING
    return _DOT_OK


def _folder_summary(f) -> str:
    """状態・進捗・相手を1行にまとめる。"""
    if f.get("paused"):
        head = "一時停止中"
    elif f.get("need_bytes"):
        head = f"{f.get('state') or '同期中'} 残り{f['need_bytes'] / 1e6:.1f}MB"
    else:
        head = f.get("state") or "状態不明"
        if f.get("files"):
            head += f" · {f['files']}ファイル"
    mates = ", ".join(f.get("devices") or [])
    return f"{head} · {mates}" if mates else head


def _draw_folder_map(box, rep):
    """共有一覧: 1プロジェクト = 1枠。何個あっても見渡せる形にする。

    参加しているプロジェクトが多い使い方(制作の面倒を見に複数の現場へ
    出入りする等)を想定している。他プロジェクトの共有は**異常ではない**
    ので赤くしない。赤は実害があるものだけに使う(誰とも共有できていない
    =同期されていない、など)。
    """
    folders = rep.get("folders") or []
    if not folders:
        return
    # 現在のプロジェクトを先頭に、残りはID順(並びが毎回変わらないように)
    folders = sorted(folders,
                     key=lambda f: (not f.get("current"), f.get("id", "")))

    head = box.row(align=True)
    head.label(text="共有中のプロジェクト", icon='FILE_FOLDER')
    tail = head.row()
    tail.alignment = 'RIGHT'
    tail.label(text=f"{len(folders)}件")

    for f in folders:
        card = box.box()
        col = card.column(align=True)

        row = col.row(align=True)
        row.label(text=f.get("id", "?"), icon=_folder_dot(f))
        if f.get("current"):
            tag = row.row()
            tag.alignment = 'RIGHT'
            tag.label(text="現在")
        else:
            # 「解除」は共有設定を壊すので再招待が必要。ふだん使うのは
            # 可逆な「一時停止」の方なので、そちらを先に置く
            if f.get("paused"):
                op = row.operator("musubi.st_pause_folder", text="再開",
                                  icon='PLAY')
                op.folder_id = f["id"]
                op.paused = False
            else:
                op = row.operator("musubi.st_pause_folder", text="一時停止",
                                  icon='PAUSE')
                op.folder_id = f["id"]
                op.paused = True
            row.operator("musubi.st_remove_folder", text="解除",
                         icon='UNLINKED').folder_id = f["id"]

        _wrap(col, f.get("path", ""), indent="")

        if not (f.get("devices") or []):
            warn = col.row()
            warn.alert = True
            warn.label(text="誰とも共有していません", icon='ERROR')
            if f.get("paused"):
                col.label(text="一時停止中", icon='BLANK1')
        else:
            _wrap(col, _folder_summary(f), indent="")
        if f.get("errors"):
            col.label(text=f"同期できないファイル {f['errors']}件",
                      icon='ERROR')


def _draw_st_nav(box, rep, sc):
    """「次の一手」ナビゲーション: 状態から計算した今やるべき操作だけを大きく出す。"""
    s = rep.get("s") or {}
    col = box.column()
    col.scale_y = 1.4

    if not s.get("running") or not s.get("folder_ok"):
        # 未導入・停止中・フォルダ未登録 → 入口は2つだけ
        box.label(text="▼ まずはどちらかを押してください", icon='INFO')
        col.operator("musubi.st_setup",
                     text="① 同期を始める(リーダー/再開)", icon='PLAY')
        col.operator("musubi.st_invite_import",
                     text="① 招待ファイルで参加(メンバー)", icon='IMPORT')
        box.label(text="メンバーは同期IDの入力不要(招待が自動設定します)")
        return

    if s.get("pending_devices"):
        alert = col.column()
        alert.alert = True
        alert.operator("musubi.st_accept",
                       text=f"▶ 参加申請が{s['pending_devices']}件 — 承認する",
                       icon='CHECKMARK')
        box.label(text="相手のデバイスIDを本人確認してから押してください")
        return

    if s.get("peers", 0) == 0:
        box.label(text="▼ 次はチームとつながります", icon='INFO')
        col.operator("musubi.st_invite_export",
                     text="② 招待ファイルを送る(リーダー)", icon='EXPORT')
        col.operator("musubi.st_invite_import",
                     text="② 招待ファイルで参加(メンバー)", icon='IMPORT')
        return

    if s.get("connected", 0) == 0:
        box.label(text="相手との接続を待っています…", icon='TIME')
        box.label(text="(相手側の起動と、リーダーの承認を確認)")
        return

    if s.get("need_items"):
        box.label(text=f"同期中… 残り{s.get('need_bytes', 0)/1e6:.1f}MB "
                       f"({s['need_items']}件)", icon='SORTTIME')
        return

    ok = col.row()
    ok.label(text="✓ 同期OK — 作業を始めてください", icon='CHECKMARK')


def _draw_st_status(box, wm, sc):
    """Syncthingのステータス表示(オープン/保存/ボタン時にイベント駆動で更新)。"""
    try:
        rep = json.loads(wm.musubi_st_status)
    except (json.JSONDecodeError, TypeError):
        box.label(text="状態を取得中…", icon='TIME')
        return

    # --- 稼働状態を明示 ---
    row = box.row()
    if rep.get("running"):
        up = int(rep.get("uptime") or 0)
        pid = rep.get("pid")
        pid_txt = f" PID {pid}" if pid else ""
        row.label(text=f"稼働中{pid_txt}(起動から{up//60}分{up%60}秒)",
                  icon='REC')
    else:
        row.label(text="停止中 — 同期は行われていません", icon='X')

    # --- ピアの状態(各行に承認取り消し=キックのボタン付き) ---
    for p in rep.get("peers", []):
        row = box.row(align=True)
        if p.get("connected"):
            row.label(text=f"{p['name']}: 接続中 ({p.get('address', '')})",
                      icon='CHECKMARK')
        else:
            row.label(text=f"{p['name']}: 未接続", icon='DOT')
        op = row.operator("musubi.st_remove_device", text="", icon='X')
        op.device_id = p["id"]
        op.device_name = p["name"]

    # --- フォルダの状態など ---
    for line in rep.get("lines", []):
        if line.startswith("* "):
            box.label(text=line[2:], icon='CHECKMARK')
        else:
            box.label(text=line, icon='DOT')

    # --- 問題診断(なぜ同期されないか) ---
    problems = rep.get("problems", [])
    if problems:
        col = box.column()
        col.alert = True
        for p in problems:
            for i in range(0, len(p), 38):  # 長文は折り返し
                col.label(text=p[i:i+38], icon='ERROR' if i == 0 else 'BLANK1')

    # --- 直近のアクティビティ(何を転送したか) ---
    activity = rep.get("activity", [])
    if activity:
        box.separator()
        box.label(text="直近の同期ログ:", icon='RECOVER_LAST')
        for a in activity:
            box.label(text=f"  {a}")

    # --- 安心のための保証表示 ---
    box.separator()
    if rep.get("running"):
        box.label(text="共有は上の一覧のフォルダのみ", icon='LOCKED')
        box.label(text="Blender終了時に自動停止します", icon='QUIT')
    if rep.get("checked_at"):
        box.label(text=f"最終確認: {rep['checked_at']}"
                       "(開く/保存/更新ボタン時に確認)")


# ---------------------------------------------------------------------------
# パネル群(CLASSESの並び順 = サイドバーでの表示順)
# ---------------------------------------------------------------------------

class _MusubiPanel:
    bl_space_type = 'VIEW_3D'
    bl_region_type = 'UI'
    bl_category = "Musubi"


class MUSUBI_PT_project(_MusubiPanel, bpy.types.Panel):
    """要のパネル。ロック警告・ルート設定・構成種別を最上部に集約。"""
    bl_idname = "MUSUBI_PT_project"
    bl_label = "プロジェクト"

    def draw_header(self, context):
        # 他人が作業中のファイルを開いている間は、畳んでいても赤で知らせる
        if context.window_manager.musubi_lock_warning:
            row = self.layout.row()
            row.alert = True
            row.label(text="", icon='ERROR')
        else:
            self.layout.label(text="", icon='FILE_FOLDER')

    def draw(self, context):
        layout = self.layout
        sc = context.scene
        wm = context.window_manager

        # 他の人が作業中のファイルを開いている場合は最上部で警告し続ける
        if wm.musubi_lock_warning:
            warn = layout.box()
            col = warn.column()
            col.alert = True
            msg = wm.musubi_lock_warning
            for i in range(0, len(msg), 38):
                col.label(text=msg[i:i + 38],
                          icon='ERROR' if i == 0 else 'BLANK1')
            col.label(text="編集せず閉じて、本人に確認してください",
                      icon='BLANK1')
            # 放置ロック(12時間超)のときだけ、引き継ぎの導線を出す。
            # 作業中の相手から奪えてしまわないよう、条件は ops 側で判定済み
            if wm.musubi_lock_stale:
                col.operator("musubi.force_release_lock", icon='UNLOCKED')

        reviewer = _is_reviewer(context)
        if not sc.musubi_project_root:
            big = layout.column()
            big.scale_y = 1.4
            big.prop(sc, "musubi_project_root", text="ルート")
            info = layout.column(align=True)
            if reviewer:
                info.label(text="同期するプロジェクトフォルダを指定:",
                           icon='INFO')
                info.label(text="・設定するとチーム同期が使えます")
                info.label(text="・同期後、進行ボードとレビューが見られます")
            else:
                info.label(text="ルート設定 = パイプライン管理の開始:",
                           icon='INFO')
                info.label(text="・保存(Ctrl+S)が自動でバージョン履歴に記録")
                info.label(text="・カットは保存だけでロック・進捗も自動管理")
                info.label(text="・チーム同期も自動で再開")
        else:
            layout.prop(sc, "musubi_project_root", text="ルート")
            card = layout.box()
            _, label, icon = _project_flavor(sc.musubi_project_root)
            card.label(text=f"構成: {label}", icon=icon)
            if reviewer:
                card.label(text="表示: レビュアー(変更はプリファレンス)",
                           icon='HIDE_OFF')
            else:
                card.label(text="管理中 — Ctrl+Sは自動で履歴に記録",
                           icon='CHECKMARK')
        if not reviewer:
            layout.operator_menu_enum(
                "musubi.create_structure", "template",
                text="フォルダ構造を作成(テンプレート選択)",
                icon='NEWFOLDER')


class MUSUBI_PT_spec(_MusubiPanel, bpy.types.Panel):
    bl_idname = "MUSUBI_PT_spec"
    bl_label = "プロジェクト仕様"

    @classmethod
    def poll(cls, context):
        return not _is_reviewer(context)

    def draw_header(self, context):
        self.layout.label(text="", icon='TEXT')

    def draw(self, context):
        from . import spec_ops
        spec_ops.draw_spec_box(self.layout, context)


class MUSUBI_PT_quality(_MusubiPanel, bpy.types.Panel):
    """このファイルの健康状態。映像でもアセット制作でも使う。"""
    bl_idname = "MUSUBI_PT_quality"
    bl_label = "品質チェック"

    @classmethod
    def poll(cls, context):
        return not _is_reviewer(context)

    def draw_header(self, context):
        self.layout.label(text="", icon='CHECKMARK')

    def draw(self, context):
        layout = self.layout
        wm = context.window_manager
        # リンク元の鮮度はカット専用ではない(アセット同士のリンクにも効く)
        if wm.musubi_lib_outdated:
            note = layout.column(align=True)
            note.label(text="リンク元が更新されています:", icon='ERROR')
            _wrap(note, wm.musubi_lib_outdated, indent="      ")
        layout.operator("musubi.reload_libs", icon='FILE_REFRESH')
        from . import quality_ops
        quality_ops.draw_qc_box(layout, context)


class MUSUBI_PT_versions(_MusubiPanel, bpy.types.Panel):
    """全テンプレート共通(アセットの.blendにも効く)なので映像制作より上。"""
    bl_idname = "MUSUBI_PT_versions"
    bl_label = "バージョン管理"

    @classmethod
    def poll(cls, context):
        return not _is_reviewer(context)

    def draw_header(self, context):
        self.layout.label(text="", icon='RECOVER_LAST')

    def draw(self, context):
        layout = self.layout
        sc = context.scene
        wm = context.window_manager
        if wm.musubi_last_save_at:
            layout.label(text=f"最終保存: {wm.musubi_last_save_at}"
                              "(ファイルは常に最新)", icon='CHECKMARK')
        col = layout.column(align=True)
        col.label(text="履歴への記録: コメント付き保存=すぐ /")
        col.label(text="無記入の保存=10分ごとにまとめて記録")
        layout.prop(wm, "musubi_version_comment", text="コメント")
        layout.operator("musubi.snapshot", icon='FILE_TICK')
        row = layout.row()
        row.template_list("MUSUBI_UL_versions", "", wm, "musubi_versions",
                          wm, "musubi_versions_index", rows=4)
        col = row.column(align=True)
        col.operator("musubi.versions_refresh", text="", icon='FILE_REFRESH')
        col.operator("musubi.version_restore", text="", icon='LOOP_BACK')
        row = layout.row(align=True)
        row.prop(sc, "musubi_version_keep", text="保持世代")
        row.operator("musubi.versions_prune", text="このファイル")
        row.operator("musubi.versions_prune_all", text="全体")
        if wm.musubi_versions_summary:
            layout.label(text=wm.musubi_versions_summary)
        # 履歴の同期可否・サイズ上限(プリファレンス連動)
        try:
            prefs = context.preferences.addons[__package__].preferences
        except (KeyError, AttributeError):
            prefs = None
        if prefs is not None:
            if prefs.history_local_only:
                layout.label(text="履歴はこのPCのみ(同期しません)",
                             icon='UNLINKED')
            if prefs.history_max_gb > 0:
                row = layout.row(align=True)
                row.label(text=f"上限 {prefs.history_max_gb:.1f}GB",
                          icon='INFO')
                row.operator("musubi.versions_enforce_cap", text="今すぐ整理")


class MUSUBI_PT_film(_MusubiPanel, bpy.types.Panel):
    """映像制作の制作進行グループ(カット作業・進行ボード・レビューの親)。

    アセット制作・自由構成の現場では、このグループごと折りたたんで
    視界から外せる(開閉状態はBlenderが記憶する)。
    """
    bl_idname = "MUSUBI_PT_film"
    bl_label = "映像制作(制作進行)"

    def draw_header(self, context):
        self.layout.label(text="", icon='RENDER_ANIMATION')

    def draw(self, context):
        layout = self.layout
        sc = context.scene
        wm = context.window_manager
        if wm.musubi_board_summary:
            layout.label(text=wm.musubi_board_summary, icon='INFO')
        root = sc.musubi_project_root
        if root and not _is_film_project(root):
            layout.label(text="このプロジェクトは映像構成ではありません",
                         icon='INFO')
            layout.label(text="(カット管理を使うと scenes/ を自動作成)")


class MUSUBI_PT_film_cut(_MusubiPanel, bpy.types.Panel):
    bl_idname = "MUSUBI_PT_film_cut"
    bl_parent_id = "MUSUBI_PT_film"
    bl_label = "カット作業"

    @classmethod
    def poll(cls, context):
        # レビュアーはカットの配置保存・レンダリングをしない
        return not _is_reviewer(context)

    def draw_header(self, context):
        self.layout.label(text="", icon='SEQUENCE')

    def draw(self, context):
        layout = self.layout
        sc = context.scene
        row = layout.row(align=True)
        row.prop(sc, "musubi_scene_no", text="シーン")
        row.prop(sc, "musubi_cut_no", text="カット")
        layout.operator("musubi.save_cut", icon='FILE_BLEND')
        layout.label(text="配置後はCtrl+S保存だけでOK(履歴・ロック自動)")
        layout.operator("musubi.release_lock", icon='UNLOCKED')
        layout.operator("musubi.render_output", icon='RENDER_ANIMATION')


class MUSUBI_PT_film_board(_MusubiPanel, bpy.types.Panel):
    bl_idname = "MUSUBI_PT_film_board"
    bl_parent_id = "MUSUBI_PT_film"
    bl_label = "進行ボード"

    def draw_header(self, context):
        self.layout.label(text="", icon='PRESET')

    def draw(self, context):
        layout = self.layout
        wm = context.window_manager
        row = layout.row(align=True)
        row.prop(wm, "musubi_board_filter", text="")
        row.operator("musubi.board_refresh", text="", icon='FILE_REFRESH')
        layout.template_list("MUSUBI_UL_board", "", wm, "musubi_board",
                             wm, "musubi_board_index", rows=6)
        row = layout.row(align=True)
        row.operator_menu_enum("musubi.task_set_status", "status",
                               text="状態変更", icon='DOWNARROW_HLT')
        row.operator("musubi.task_assign", text="担当", icon='USER')
        row.operator("musubi.task_note", text="指示", icon='TEXT')
        row = layout.row(align=True)
        row.operator("musubi.task_add_cut", icon='ADD')
        row.operator("musubi.task_open_cut", text="開く", icon='FILE_BLEND')
        layout.operator("musubi.board_html", icon='URL')
        layout.operator("musubi.render_reel", icon='SEQUENCE')


class MUSUBI_PT_film_review(_MusubiPanel, bpy.types.Panel):
    bl_idname = "MUSUBI_PT_film_review"
    bl_parent_id = "MUSUBI_PT_film"
    bl_label = "レビュー(選択カット)"

    def draw_header(self, context):
        self.layout.label(text="", icon='HIDE_OFF')

    def draw(self, context):
        layout = self.layout
        wm = context.window_manager
        layout.template_list("MUSUBI_UL_reviews", "", wm, "musubi_reviews",
                             wm, "musubi_reviews_index", rows=3)
        row = layout.row(align=True)
        row.operator("musubi.review_add", text="コメント追加", icon='ADD')
        row.operator("musubi.review_refresh", text="", icon='FILE_REFRESH')


class MUSUBI_PT_sync(_MusubiPanel, bpy.types.Panel):
    """意図的に末尾固定(「一番うしろ=チーム同期」という定位置)。"""
    bl_idname = "MUSUBI_PT_sync"
    bl_label = "チーム同期"

    def draw_header(self, context):
        self.layout.label(text="", icon='UV_SYNC_SELECT')

    def draw(self, context):
        layout = self.layout
        sc = context.scene
        wm = context.window_manager
        try:
            rep = json.loads(wm.musubi_st_status)
        except (json.JSONDecodeError, TypeError):
            rep = {}
        box = layout.column()
        _draw_st_overview(box, rep, sc)
        _draw_st_nav(box, rep, sc)
        _draw_invite(box, wm)
        _draw_folder_map(box, rep)
        _draw_st_status(box, wm, sc)
        box.operator("musubi.st_status", icon='FILE_REFRESH')
        # 上級者向け: 手動操作は「詳細」に折りたたむ
        row = box.row()
        row.prop(wm, "musubi_st_show_advanced",
                 icon='TRIA_DOWN' if wm.musubi_st_show_advanced
                 else 'TRIA_RIGHT',
                 text="詳細(手動操作)", emboss=False)
        if wm.musubi_st_show_advanced:
            sub = box.column()
            sub.prop(sc, "musubi_sync_id", text="同期ID")
            sub.prop(sc, "musubi_st_lan_only")
            sub.operator("musubi.st_setup", icon='PLAY')
            sub.operator("musubi.st_invite_export", icon='EXPORT')
            sub.operator("musubi.st_copy_id", icon='COPYDOWN')
            row = sub.row(align=True)
            row.prop(sc, "musubi_leader_id", text="")
            row.operator("musubi.st_connect_leader", text="接続", icon='LINKED')
            sub.operator("musubi.st_accept", icon='CHECKMARK')
            sub.operator("musubi.st_stop", icon='PAUSE')


class MUSUBI_PT_sync_verify(_MusubiPanel, bpy.types.Panel):
    bl_idname = "MUSUBI_PT_sync_verify"
    bl_parent_id = "MUSUBI_PT_sync"
    bl_label = "同期の担保(検証)"
    bl_options = {'DEFAULT_CLOSED'}

    def draw_header(self, context):
        self.layout.label(text="", icon='FAKE_USER_ON')

    def draw(self, context):
        self.layout.operator("musubi.write_manifest", icon='FILE_TEXT')
        self.layout.operator("musubi.verify_sync", icon='CHECKMARK')


class MUSUBI_PT_sync_security(_MusubiPanel, bpy.types.Panel):
    bl_idname = "MUSUBI_PT_sync_security"
    bl_parent_id = "MUSUBI_PT_sync"
    bl_label = "セキュリティ"
    bl_options = {'DEFAULT_CLOSED'}

    def draw_header(self, context):
        self.layout.label(text="", icon='LOCKED')

    def draw(self, context):
        self.layout.operator("musubi.audit", icon='VIEWZOOM')


class MUSUBI_PT_report(_MusubiPanel, bpy.types.Panel):
    """直近の操作結果。レポートがあるときだけ現れる。"""
    bl_idname = "MUSUBI_PT_report"
    bl_label = "最終レポート"

    @classmethod
    def poll(cls, context):
        return bool(getattr(context.window_manager,
                            "musubi_last_report", ""))

    def draw_header(self, context):
        self.layout.label(text="", icon='INFO')

    def draw(self, context):
        report = context.window_manager.musubi_last_report
        for line in report.split("\n")[:20]:
            self.layout.label(text=line)


CLASSES = (
    MUSUBI_PT_project,
    MUSUBI_PT_spec,
    MUSUBI_PT_quality,
    MUSUBI_PT_versions,
    MUSUBI_PT_film,
    MUSUBI_PT_film_cut,
    MUSUBI_PT_film_board,
    MUSUBI_PT_film_review,
    MUSUBI_PT_sync,
    MUSUBI_PT_sync_verify,
    MUSUBI_PT_sync_security,
    MUSUBI_PT_report,
)
