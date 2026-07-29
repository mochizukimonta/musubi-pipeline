# SPDX-FileCopyrightText: 2026 mochizukimonta
# SPDX-License-Identifier: GPL-3.0-or-later
"""musubi_pipeline.st_ops — Syncthing制御のBlenderオペレーターとライフサイクル管理。

ユーザーへの可視化方針:
- パネルは数秒ごとに自動更新され、稼働状態(PID)・ピアごとの接続・
  同期進捗・直近の転送ログ・問題診断(同期ID不一致など)を常時表示する
- Blender終了時(atexit)にこのセッションで起動したSyncthingを自動停止する
  → 「Blenderを閉じたのに裏で動き続ける」ことはない

パフォーマンス設計(クリエイターの作業を一切妨げない):
- 常設のタイマー・常駐スレッドは持たない。状態取得は
  「ファイルを開いた時 / 保存した時 / ボタンを押した時」だけ、
  1回きりのバックグラウンドスレッドで行う(イベント駆動)
- ネットワークI/OをUIスレッドで行わない。bpyへのアクセスはメインスレッドのみ。
  結果の画面反映は、取得完了までの間だけ生きる短命タイマーが行い、
  反映が終わったら自動消滅する
"""

from __future__ import annotations

import atexit
import json
import threading
import time
from pathlib import Path

import bpy

from . import core, syncthing as st
from .core import PipelineError
from .sync import host_id

_started_by_us = False   # このBlenderセッションが起動したか
_user_stopped = False    # このセッションでユーザーが明示的に停止したか
_last_status_json = ""

# --- イベント駆動の状態取得(常駐なし) ---
_fetch_lock = threading.Lock()
_fetch_result = ""      # 未反映の取得結果(JSON)
_fetching = False       # 取得中フラグ(多重起動防止)


def _fetch_once(sync_id: str):
    """バックグラウンドで1回だけ状態を取得する(bpyには一切触らない)。"""
    global _fetch_result, _fetching
    try:
        rep = st.status_report(sync_id)
        rep["checked_at"] = time.strftime("%H:%M:%S")
        text = json.dumps(rep, ensure_ascii=False)
    except Exception as e:
        text = json.dumps({"running": False, "lines": [],
                           "problems": [f"状態取得エラー: {e}"],
                           "activity": []}, ensure_ascii=False)
    with _fetch_lock:
        _fetch_result = text
        _fetching = False


def _apply_tick() -> float | None:
    """取得結果をUIに反映する短命タイマー。反映し終えたら消滅する。"""
    global _last_status_json
    with _fetch_lock:
        text = _fetch_result
        busy = _fetching
    if text:
        with _fetch_lock:
            globals()["_fetch_result"] = ""
        if text != _last_status_json:
            _last_status_json = text
            try:
                for wm in bpy.data.window_managers:
                    wm.musubi_st_status = text
                    for win in wm.windows:
                        for area in win.screen.areas:
                            if area.type == 'VIEW_3D':
                                area.tag_redraw()
            except Exception:
                pass
        return 0.3 if busy else None  # 取得中でなければタイマー終了
    return 0.3 if busy else None


def request_refresh():
    """状態更新を非同期に要求する(オープン時・保存時・ボタンから呼ばれる)。

    UIスレッドは一瞬もブロックしない。取得中の多重要求は無視。
    """
    global _fetching
    with _fetch_lock:
        if _fetching:
            return
        _fetching = True
    try:  # リンク元の更新確認もこのタイミングで(イベント駆動の集約点)
        from . import ops as _ops
        _ops.refresh_lib_notice()
    except Exception:
        pass
    sync_id = _active_sync_id()
    threading.Thread(target=_fetch_once, args=(sync_id,), daemon=True,
                     name="musubi-status-fetch").start()
    try:
        if not bpy.app.timers.is_registered(_apply_tick):
            bpy.app.timers.register(_apply_tick, first_interval=0.3)
    except Exception:
        pass


def _shutdown_on_blender_exit():
    """Blender終了時に自動でSyncthingを停止する(atexitから呼ばれる)。"""
    global _started_by_us
    if _started_by_us:
        try:
            st.shutdown()
        except Exception:
            pass
        _started_by_us = False


atexit.register(_shutdown_on_blender_exit)


def _active_sync_id() -> str:
    """タイマー内でも安全に同期IDを得る(設定済みシーンを探す)。"""
    for sc in bpy.data.scenes:
        try:
            if sc.musubi_project_root and sc.musubi_sync_id:
                return sc.musubi_sync_id
        except AttributeError:
            break
    return "musubi-project"


def _refresh_status(context):
    """オペレーター実行直後の更新要求(非同期・UIをブロックしない)。"""
    request_refresh()


# --- セットアップ監視(接続の握手が済むまでの間だけ動く一時タイマー) ---
_watch_deadline = 0.0


def _watch_tick() -> float | None:
    """接続が成立するまで数秒ごとに状態を更新し、完了か期限で自動消滅する。

    日常作業中は動かない(セットアップ操作の直後だけ起動される)ため、
    「常駐ゼロ」方針は維持される。
    """
    if time.time() > _watch_deadline:
        return None
    request_refresh()
    try:
        s = json.loads(_last_status_json).get("s", {})
        done = (s.get("running") and s.get("folder_ok")
                and s.get("peers", 0) > 0
                and s.get("connected", 0) == s.get("peers", 0)
                and not s.get("pending_devices")
                and not s.get("need_items"))
        if done:
            return None  # 同期成立 → 監視終了
    except (json.JSONDecodeError, AttributeError):
        pass
    return 3.0


def start_setup_watch(minutes: float = 5.0):
    """セットアップ操作の直後から、握手完了(最長minutes分)まで自動更新する。"""
    global _watch_deadline
    _watch_deadline = time.time() + minutes * 60
    try:
        if not bpy.app.timers.is_registered(_watch_tick):
            bpy.app.timers.register(_watch_tick, first_interval=1.0)
    except Exception:
        pass


def _resume_thread(sync_id: str):
    """ワーカースレッド: エンジンを起動し、状態を取得する(bpyには触らない)。"""
    global _started_by_us
    try:
        before = st.last_spawned_pid
        if st.start() and st.last_spawned_pid != before:
            _started_by_us = True  # 自分が起動した場合のみ終了時に自動停止
    except Exception:
        pass
    _fetch_once(sync_id)


def maybe_auto_resume():
    """セットアップ済みプロジェクトなら、同期エンジンを裏で自動再開する。

    「project.jsonがあるのに『同期を始める』を毎回押させられる」の解消。
    - 対象: project.json があり、この端末に Syncthing 導入・設定済みの場合のみ
      (新規プロジェクトの初回セットアップと未参加端末は従来どおり明示操作)
    - このセッションで「同期を停止」を押した後は自動再開しない(意思を尊重)
    - UIスレッドは一切ブロックしない(起動・確認はワーカースレッド)
    """
    global _fetching
    if _user_stopped or not hasattr(bpy.data, "scenes"):
        return
    root = ""
    for sc in bpy.data.scenes:
        try:
            if sc.musubi_project_root:
                root = sc.musubi_project_root
                break
        except AttributeError:
            return
    if not root:
        return
    sync_id = core.read_project_info(root).get("sync_id")
    if not sync_id or not isinstance(sync_id, str):
        return
    try:
        if not st.is_installed() or not (st.home_dir() / "config.xml").exists():
            return
    except Exception:
        return
    with _fetch_lock:
        if _fetching:
            return
        _fetching = True
    threading.Thread(target=_resume_thread, args=(sync_id,), daemon=True,
                     name="musubi-auto-resume").start()
    try:
        if not bpy.app.timers.is_registered(_apply_tick):
            bpy.app.timers.register(_apply_tick, first_interval=0.5)
    except Exception:
        pass
    start_setup_watch(1.0)  # 再開直後の1分だけ状態を追いかけて画面に反映


def on_root_update(self, context):
    """ルート欄の変更時: セットアップ済みプロジェクトなら同期IDを自動で読む。

    File > New の未保存ファイル(場所からの自動判別が効かない)でも、
    ルートを指定するだけでIDが揃うようにする。
    """
    try:
        sid = core.read_project_info(self.musubi_project_root).get("sync_id")
        if sid and isinstance(sid, str) and self.musubi_sync_id != sid:
            self.musubi_sync_id = sid
    except Exception:
        pass
    maybe_auto_resume()
    request_refresh()


def autodetect_project() -> bool:
    """開いたファイルの場所から、ルート・同期ID・シーン/カット番号を自動設定。

    ルートを一度セットアップすれば、その中の.blendは開くだけで設定済みに
    なる(ファイルごとの手入力を無くす)。ルートの判別は`.musubi`フォルダ、
    同期IDは`.musubi/project.json`(同期で全員に配られる)。
    プロジェクト外のファイルでは何もしない(手動設定を尊重)。
    """
    if not hasattr(bpy.data, "scenes"):
        return False
    root = core.detect_root(bpy.data.filepath)
    if root is None:
        return False
    sid = core.read_project_info(str(root)).get("sync_id")
    nums = core.parse_cut_path(root, bpy.data.filepath)
    changed = False
    for sc in bpy.data.scenes:
        try:
            if sc.musubi_project_root != str(root):
                sc.musubi_project_root = str(root)
                changed = True
            if sid and isinstance(sid, str) and sc.musubi_sync_id != sid:
                sc.musubi_sync_id = sid
                changed = True
            if nums and (sc.musubi_scene_no, sc.musubi_cut_no) != nums:
                sc.musubi_scene_no, sc.musubi_cut_no = nums
                changed = True
        except AttributeError:
            break
    return changed


from bpy.app.handlers import persistent


@persistent
def on_file_load(_a=None, _b=None):
    """ファイルを開いたら設定を自動判別し、同期を自動再開して状態を確認。"""
    autodetect_project()
    maybe_auto_resume()
    request_refresh()


@persistent
def on_file_save(_a=None, _b=None):
    """保存したら同期状態を確認(Syncthingは保存を検知して自動転送する)。

    プロジェクト内へ初めて保存された場合の自動判別もここで行う。
    """
    autodetect_project()
    request_refresh()


def _write_invite_file(sc, root: Path, force: bool = False) -> Path:
    """招待ファイルをルート直下に書き出して、その場所を返す。

    ファイルは同期でチーム全員に配られるため、他人(リーダー)が作った
    招待をメンバーの「再開」操作で上書きしない(force=Trueなら常に書く)。
    """
    path = root / f"musubi_invite_{root.name}.json"
    my_id = st.my_device_id()
    if not force and path.exists():
        try:
            existing = st.parse_invite(path.read_text(encoding="utf-8"))
            if existing["leader_device_id"] != my_id:
                return path  # リーダーの招待はそのまま使う
        except Exception:
            pass  # 壊れた招待は書き直す
    data = st.make_invite(sc.musubi_sync_id, my_id, root.name, host_id())
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(json.dumps(data, ensure_ascii=False, indent=1),
                   encoding="utf-8")
    core.atomic_replace(tmp, path)
    return path


class MUSUBI_OT_st_setup(bpy.types.Operator):
    """Syncthingを導入(初回はSHA-256検証付きダウンロード)して起動し、
プロジェクトフォルダを共有登録する"""
    bl_idname = "musubi.st_setup"
    bl_label = "同期セットアップ / 起動"

    def execute(self, context):
        global _started_by_us, _user_stopped
        _user_stopped = False
        sc = context.scene
        try:
            root = core.resolve_root(sc.musubi_project_root)
            # 同期IDの自動決定: 既存プロジェクトは記録値、新規はフォルダ名から
            recorded = core.read_project_info(str(root)).get("sync_id")
            if recorded and isinstance(recorded, str):
                sc.musubi_sync_id = recorded
            elif sc.musubi_sync_id in ("", "musubi-project"):
                sc.musubi_sync_id = st.derive_sync_id(root.name)
            if not st.is_installed():
                self.report({'INFO'}, "Syncthingをダウンロード中…(初回のみ)")
                st.download_binary(log=print)
            st.ensure_config(gui_addr=_pref_gui_addr())
            if not st.start():
                self.report({'ERROR'}, "Syncthingの起動がタイムアウトしました")
                return {'CANCELLED'}
            _started_by_us = True  # Blender終了時に自動停止する対象
            st.set_lan_only(sc.musubi_st_lan_only)
            st.ensure_folder(sc.musubi_sync_id, root,
                             local_only_history=_pref_history_local_only())
            # メンバーに送る招待ファイルもここで用意する(場所はパネルに表示)
            invite_path = _write_invite_file(sc, root)
            context.window_manager.musubi_invite_path = str(invite_path)
            # 同期IDをプロジェクト内に記録 → 以後は.blendを開くだけで自動設定
            core.write_project_info(str(root), sc.musubi_sync_id)
        except (PipelineError, OSError) as e:
            self.report({'ERROR'}, str(e))
            return {'CANCELLED'}
        _refresh_status(context)
        start_setup_watch()
        self.report({'INFO'}, f"稼働開始。招待ファイルはここにあります: {invite_path}")
        return {'FINISHED'}


class MUSUBI_OT_st_copy_id(bpy.types.Operator):
    """自分のデバイスIDをクリップボードへコピー(チャット等でリーダーに送る)"""
    bl_idname = "musubi.st_copy_id"
    bl_label = "デバイスIDをコピー"

    def execute(self, context):
        try:
            did = st.my_device_id()
        except Exception:
            self.report({'ERROR'}, "Syncthingが起動していません")
            return {'CANCELLED'}
        context.window_manager.clipboard = did
        self.report({'INFO'}, f"コピーしました: {did[:15]}…")
        return {'FINISHED'}


class MUSUBI_OT_st_connect_leader(bpy.types.Operator):
    """リーダーのデバイスIDを登録して接続(メンバーが1回だけ行う)"""
    bl_idname = "musubi.st_connect_leader"
    bl_label = "リーダーに接続"

    def execute(self, context):
        sc = context.scene
        try:
            st.add_peer(sc.musubi_leader_id, "leader", sc.musubi_sync_id,
                        introducer=True)
        except (PipelineError, OSError) as e:
            self.report({'ERROR'}, str(e))
            return {'CANCELLED'}
        _refresh_status(context)
        start_setup_watch()
        self.report({'INFO'},
                    "リーダーに接続要求を送りました。リーダー側の承認を待ってください")
        return {'FINISHED'}


class MUSUBI_OT_st_accept(bpy.types.Operator):
    """承認待ちデバイスを承認して共有に追加(リーダーが実行。
IDを本人確認してから押すこと)"""
    bl_idname = "musubi.st_accept"
    bl_label = "承認待ちデバイスを承認"

    def invoke(self, context, event):
        try:
            self._pending = st.pending_devices()
        except Exception as e:
            self.report({'ERROR'}, f"Syncthingに接続できません: {e}")
            return {'CANCELLED'}
        if not self._pending:
            self.report({'INFO'}, "承認待ちデバイスはありません")
            return {'CANCELLED'}
        return context.window_manager.invoke_confirm(self, event)

    def draw(self, context):
        col = self.layout.column()
        col.label(text="以下のデバイスを承認します(IDを本人確認済みですか?):")
        for did, info in self._pending.items():
            col.label(text=f"  {info.get('name','?')}  {did[:23]}…")

    def execute(self, context):
        try:
            # invoke で表示・確認したデバイスだけを承認する
            accepted = st.accept_pending(context.scene.musubi_sync_id,
                                         only=list(self._pending))
        except (PipelineError, OSError) as e:
            self.report({'ERROR'}, str(e))
            return {'CANCELLED'}
        _refresh_status(context)
        start_setup_watch()
        self.report({'INFO'}, f"{len(accepted)}台を承認しました")
        return {'FINISHED'}


class MUSUBI_OT_st_invite_export(bpy.types.Operator):
    """招待ファイルをプロジェクトルート直下に書き出す(リーダー用)。
中身は公開情報のみ。できたファイルをチャット等でメンバーに送り、
メンバーは「招待ファイルで参加」で読み込む"""
    bl_idname = "musubi.st_invite_export"
    bl_label = "招待ファイルを書き出す"

    def execute(self, context):
        sc = context.scene
        if not st.ping():
            self.report({'ERROR'},
                        "先に「同期セットアップ / 起動」を押してください")
            return {'CANCELLED'}
        try:
            root = core.resolve_root(sc.musubi_project_root)
            # 明示操作なので自分の招待として書き直す(場所はパネルにも常時表示)
            path = _write_invite_file(sc, root, force=True)
        except (PipelineError, OSError) as e:
            self.report({'ERROR'}, str(e))
            return {'CANCELLED'}
        context.window_manager.musubi_invite_path = str(path)
        self.report({'INFO'}, f"招待ファイルはここにあります: {path}")
        return {'FINISHED'}


class MUSUBI_OT_st_invite_import(bpy.types.Operator):
    """招待ファイルで参加(メンバー用)。同期IDの設定・Syncthingの起動・
リーダーへの接続申請まで自動で行う。あとはリーダーの承認を待つだけ"""
    bl_idname = "musubi.st_invite_import"
    bl_label = "招待ファイルで参加"

    filepath: bpy.props.StringProperty(subtype='FILE_PATH')
    filter_glob: bpy.props.StringProperty(default="*.json", options={'HIDDEN'})

    def invoke(self, context, event):
        try:
            core.resolve_root(context.scene.musubi_project_root)
        except PipelineError:
            self.report({'ERROR'},
                        "先に「ルート」にプロジェクト用フォルダ(空でOK)を"
                        "指定してください")
            return {'CANCELLED'}
        context.window_manager.fileselect_add(self)
        return {'RUNNING_MODAL'}

    def execute(self, context):
        global _started_by_us, _user_stopped
        _user_stopped = False
        sc = context.scene
        try:
            invite = st.parse_invite(
                Path(self.filepath).read_text(encoding="utf-8"))
            root = core.resolve_root(sc.musubi_project_root)
            # 招待の内容を反映してセットアップ〜接続申請まで一気に行う
            sc.musubi_sync_id = invite["sync_id"]
            sc.musubi_leader_id = invite["leader_device_id"]
            if not st.is_installed():
                self.report({'INFO'}, "Syncthingをダウンロード中…(初回のみ)")
                st.download_binary(log=print)
            st.ensure_config(gui_addr=_pref_gui_addr())
            if not st.start():
                self.report({'ERROR'}, "Syncthingの起動がタイムアウトしました")
                return {'CANCELLED'}
            _started_by_us = True
            st.set_lan_only(sc.musubi_st_lan_only)
            st.ensure_folder(invite["sync_id"], root,
                             local_only_history=_pref_history_local_only())
            st.add_peer(invite["leader_device_id"],
                        invite["leader_name"] or "leader",
                        invite["sync_id"], introducer=True)
            core.write_project_info(str(root), invite["sync_id"])
        except (PipelineError, OSError) as e:
            self.report({'ERROR'}, str(e))
            return {'CANCELLED'}
        _refresh_status(context)
        start_setup_watch()
        self.report({'INFO'},
                    f"「{invite['project_name']}」に参加申請しました。"
                    "リーダーの承認をお待ちください")
        return {'FINISHED'}


class MUSUBI_OT_st_remove_folder(bpy.types.Operator):
    """想定外の共有フォルダの登録を解除する(ディスク上のファイルは消えない)"""
    bl_idname = "musubi.st_remove_folder"
    bl_label = "共有解除"

    folder_id: bpy.props.StringProperty()

    def invoke(self, context, event):
        return context.window_manager.invoke_confirm(self, event)

    def execute(self, context):
        try:
            removed = st.remove_folder(self.folder_id)
        except (PipelineError, OSError) as e:
            self.report({'ERROR'}, str(e))
            return {'CANCELLED'}
        _refresh_status(context)
        self.report({'INFO'},
                    f"'{self.folder_id}' の共有を解除しました"
                    "(ファイルは消えていません)" if removed
                    else f"'{self.folder_id}' は登録されていません")
        return {'FINISHED'}


class MUSUBI_OT_st_pause_folder(bpy.types.Operator):
    """このプロジェクトの同期だけを一時停止する(いつでも再開できる)。
共有設定は壊れないので、再開に招待のやり直しは要らない"""
    bl_idname = "musubi.st_pause_folder"
    bl_label = "同期を一時停止"

    folder_id: bpy.props.StringProperty()
    paused: bpy.props.BoolProperty(default=True)

    @classmethod
    def description(cls, context, props):
        if props.paused:
            return ("このプロジェクトの同期だけを一時停止します。"
                    "共有設定は壊れないので、いつでも再開できます")
        return "このプロジェクトの同期を再開します"

    def execute(self, context):
        try:
            ok = st.pause_folder(self.folder_id, self.paused)
        except (PipelineError, OSError) as e:
            self.report({'ERROR'}, str(e))
            return {'CANCELLED'}
        _refresh_status(context)
        if not ok:
            self.report({'WARNING'},
                        f"'{self.folder_id}' は登録されていません")
            return {'CANCELLED'}
        self.report({'INFO'},
                    f"'{self.folder_id}' の同期を"
                    + ("一時停止しました" if self.paused else "再開しました"))
        return {'FINISHED'}


class MUSUBI_OT_st_remove_device(bpy.types.Operator):
    """このデバイスの承認を取り消す(キック)。以後は接続できず、
再参加にはあらためて承認が必要。相手の手元のファイルは消えない"""
    bl_idname = "musubi.st_remove_device"
    bl_label = "デバイスの承認を取り消す"

    device_id: bpy.props.StringProperty(options={'HIDDEN'})
    device_name: bpy.props.StringProperty(options={'HIDDEN'})

    def invoke(self, context, event):
        # 影響範囲(このデバイスと共有中のフォルダ)を先に調べて見せる。
        # キックは全フォルダから外すため、別プロジェクトを巻き込むかを
        # 押す前に判断できるようにする
        try:
            self._folders = st.device_folders(self.device_id)
        except Exception:
            self._folders = None  # 表示できないだけでキック自体は可能
        return context.window_manager.invoke_confirm(self, event)

    def draw(self, context):
        col = self.layout.column()
        col.label(text=f"{self.device_name} を解除します。", icon='ERROR')
        col.label(text=f"  {self.device_id[:23]}…")
        col.label(text="以後この端末は同期に参加できません(再承認まで)")
        folders = getattr(self, "_folders", None)
        if folders is None:
            col.label(text="共有フォルダの一覧を確認できませんでした",
                      icon='QUESTION')
            return
        if not folders:
            col.label(text="共有中のフォルダはありません(承認の取り消しのみ)")
            return
        col.separator()
        col.label(text="このデバイスが外れる共有フォルダ:", icon='FILE_FOLDER')
        current = context.scene.musubi_sync_id
        for fid in folders:
            mark = "(このプロジェクト)" if fid == current else ""
            col.label(text=f"  {fid}{mark}")
        if any(fid != current for fid in folders):
            warn = col.column()
            warn.alert = True
            warn.label(text="別プロジェクトの共有からも外れます!",
                       icon='ERROR')

    def execute(self, context):
        try:
            removed = st.remove_device(self.device_id)
        except (PipelineError, OSError) as e:
            self.report({'ERROR'}, str(e))
            return {'CANCELLED'}
        _refresh_status(context)
        self.report({'INFO'},
                    f"{self.device_name or self.device_id[:7]} を解除しました"
                    if removed else "対象のデバイスが見つかりません")
        return {'FINISHED'}


class MUSUBI_OT_copy_text(bpy.types.Operator):
    """テキストをクリップボードへコピー"""
    bl_idname = "musubi.copy_text"
    bl_label = "コピー"

    text: bpy.props.StringProperty(options={'HIDDEN'})

    def execute(self, context):
        context.window_manager.clipboard = self.text
        self.report({'INFO'}, "コピーしました")
        return {'FINISHED'}


class MUSUBI_OT_st_status(bpy.types.Operator):
    """同期状態(接続ピア・同期率・承認待ち)を更新表示"""
    bl_idname = "musubi.st_status"
    bl_label = "同期状態を更新"

    def execute(self, context):
        _refresh_status(context)
        return {'FINISHED'}


class MUSUBI_OT_st_stop(bpy.types.Operator):
    """Syncthingを今すぐ停止する。停止後、このBlenderを閉じるまでは
自動再開しない(再開したいときは「同期セットアップ / 起動」)"""
    bl_idname = "musubi.st_stop"
    bl_label = "同期を停止"

    def execute(self, context):
        global _started_by_us, _user_stopped
        ok = st.shutdown()
        _started_by_us = False
        _user_stopped = True  # 明示停止後はこのセッション中は自動再開しない
        _refresh_status(context)
        self.report({'INFO'}, "停止しました" if ok else "既に停止しています")
        return {'FINISHED'}


# ---------------------------------------------------------------------------
# アドオン設定(編集 > プリファレンス > アドオン > Musubi Pipeline)
# ---------------------------------------------------------------------------

def _on_port_custom_toggle(self, context):
    """手動設定をオフに戻したら数値も既定値へ戻す(触った痕跡を残さない)。"""
    if not self.st_port_custom and self.st_port != st.DEFAULT_PORT:
        self.st_port = st.DEFAULT_PORT


def _on_history_local_only_toggle(self, context):
    """履歴をこの端末だけに留めるか同期するかを .stignore に即反映する。

    非破壊: 既にある履歴ファイルは消さない。同期に戻すとSyncthingが
    再スキャンして自分のローカル履歴を送り、他端末の履歴も受け取り直す。
    """
    local_only = self.history_local_only
    done = []
    try:
        done = st.apply_history_ignore(local_only)
    except Exception:
        pass
    if not done:
        # Syncthing停止・未導入時: せめて設定済みプロジェクトの.stignoreは更新し、
        # 次回起動時にignoreが効くようにしておく
        for sc in bpy.data.scenes:
            r = getattr(sc, "musubi_project_root", "")
            if not r:
                continue
            try:
                st.write_stignore(core.resolve_root(r),
                                  local_only_history=local_only)
            except Exception:
                pass


class MusubiAddonPreferences(bpy.types.AddonPreferences):
    """端末ごとの設定。ポートはローカル制御API用で、チームで揃える必要はない。

    誤操作対策の2段階仕様: チェックボックスを有効にしたときだけ数値欄が
    現れる。オフの間は数値欄の値がどうであれ必ず既定ポートを使い
    (_pref_gui_addr が読まない)、オフに戻すと数値も既定値にリセットされる。
    """
    bl_idname = __package__

    role: bpy.props.EnumProperty(
        name="役割",
        description="サイドバーに表示する機能の範囲",
        items=[
            ('production', "制作者",
             "全機能を表示(モデリング・アニメーション等の作業者向け)"),
            ('reviewer', "レビュアー",
             "進行ボード・レビュー・チーム同期だけを表示"
             "(ディレクター・制作進行など、確認と指示が中心の人向け)"),
        ],
        default='production')

    st_port_custom: bpy.props.BoolProperty(
        name="Syncthing制御ポートを手動設定する(上級者向け)",
        description="通常は変更不要です。既定ポート(28384)を別のアプリが"
                    "使用してしまっている場合のみ有効にしてください",
        default=False, update=_on_port_custom_toggle)
    st_port: bpy.props.IntProperty(
        name="制御ポート",
        description="アドオンがローカルのSyncthingを操作するためのポート"
                    "(127.0.0.1限定)。ピア間の同期通信とは無関係なので、"
                    "チームで揃える必要はありません",
        default=st.DEFAULT_PORT, min=1024, max=65535)

    history_local_only: bpy.props.BoolProperty(
        name="バージョン履歴をこのPCだけに保存(同期しない)",
        description="有効にすると .musubi/versions/ を同期対象から外します。"
                    "500MBの.blendが全端末で増え続けるのを防げます。"
                    "既にある履歴は消えません。無効に戻すと再び同期を開始し、"
                    "他端末の履歴も受け取り直します(端末ごとの設定)",
        default=False, update=_on_history_local_only_toggle)
    history_max_gb: bpy.props.FloatProperty(
        name="履歴の合計サイズ上限(GB)",
        description="履歴の合計がこのサイズを超えたら、各ファイルの最新世代は"
                    "残しつつ古い世代から自動で削除します。0で無制限",
        default=0.0, min=0.0, max=2000.0, precision=1)

    def draw(self, context):
        col = self.layout.column()
        row = col.row()
        row.label(text="役割:")
        row.prop(self, "role", expand=True)
        if self.role == 'reviewer':
            col.label(text="サイドバーには進行ボード・レビュー・チーム同期"
                           "だけが表示されます", icon='INFO')

        col.separator()
        box = col.box()
        box.label(text="バージョン履歴", icon='RECOVER_LAST')
        box.prop(self, "history_local_only")
        if self.history_local_only:
            box.label(text="履歴はこのPCのみ。他端末とは共有しません"
                           "(既存の履歴は保持)", icon='UNLINKED')
        else:
            box.label(text="履歴を全端末で共有(誰の履歴からも復元できる"
                           "利点と引き換えに容量・転送コストが増えます)",
                      icon='FILE_REFRESH')
        box.prop(self, "history_max_gb")
        if self.history_max_gb > 0:
            box.label(text=f"上限 {self.history_max_gb:.1f}GB 超過分は"
                           "古い世代から自動削除(最新は各ファイル必ず保持)",
                      icon='INFO')

        col.separator()
        col.prop(self, "st_port_custom")
        if self.st_port_custom:
            col.prop(self, "st_port")
            col.label(text="変更は次回の「同期セットアップ / 起動」から適用"
                           "されます(稼働中の場合は自動で再起動します)",
                      icon='INFO')
        else:
            col.label(text=f"既定ポート {st.DEFAULT_PORT} を使用します",
                      icon='CHECKMARK')


def _pref_gui_addr() -> str:
    """プリファレンスのポート設定からGUIアドレスを得る。

    手動設定が有効なときだけ数値を読む。無効時・読めないときは既定値
    (数値欄に何が残っていても影響しないことの保証)。
    """
    try:
        prefs = bpy.context.preferences.addons[__package__].preferences
        if prefs.st_port_custom:
            return st.gui_addr(prefs.st_port)
    except (KeyError, AttributeError):
        pass
    return st.DEFAULT_GUI


def _pref_history_local_only() -> bool:
    """履歴をこの端末だけに留める設定か(読めなければFalse=同期する)。"""
    try:
        prefs = bpy.context.preferences.addons[__package__].preferences
        return bool(prefs.history_local_only)
    except (KeyError, AttributeError):
        return False


def pref_history_max_bytes() -> int:
    """履歴サイズ上限(バイト)。0は無制限。読めなければ0。"""
    try:
        prefs = bpy.context.preferences.addons[__package__].preferences
        return int(prefs.history_max_gb * (1024 ** 3))
    except (KeyError, AttributeError):
        return 0


CLASSES = (
    MusubiAddonPreferences,
    MUSUBI_OT_st_setup,
    MUSUBI_OT_st_copy_id,
    MUSUBI_OT_st_connect_leader,
    MUSUBI_OT_st_accept,
    MUSUBI_OT_st_invite_export,
    MUSUBI_OT_st_invite_import,
    MUSUBI_OT_st_remove_folder,
    MUSUBI_OT_st_pause_folder,
    MUSUBI_OT_st_remove_device,
    MUSUBI_OT_copy_text,
    MUSUBI_OT_st_status,
    MUSUBI_OT_st_stop,
)
