# SPDX-FileCopyrightText: 2026 mochizukimonta
# SPDX-License-Identifier: GPL-3.0-or-later
"""musubi_pipeline.ops — Blenderオペレーター群。

保存の設計(v0.18〜): ルートフォルダの設定=パイプライン管理への同意とみなし、
Ctrl+S / File > Save を含む「プロジェクト内の全保存」を on_save_post が
パイプライン保存(自動スナップショット+カットのロック/進捗管理)にする。
専用ボタンを知らなくても、普通に保存すれば正しく管理される。
"""

from __future__ import annotations

import atexit
import threading
import time
from pathlib import Path

import bpy
from bpy.app.handlers import persistent

from . import core, security, sync
from .core import PipelineError


def _root(context) -> str:
    return context.scene.musubi_project_root


def _report_error(op, e: Exception) -> set:
    op.report({'ERROR'}, str(e))
    return {'CANCELLED'}


def save_mainfile_retry(filepath: str | None = None, retries: int = 3) -> None:
    """同期ツールが一瞬ファイルを掴んでいて保存(バックアップのリネーム)が
    失敗することがあるため、少し待ってやり直す。"""
    for i in range(retries):
        try:
            if filepath:
                bpy.ops.wm.save_as_mainfile(filepath=filepath)
            else:
                bpy.ops.wm.save_mainfile()
            return
        except RuntimeError:
            if i == retries - 1:
                raise
            time.sleep(0.8)


# --- Ctrl+S をパイプライン保存にする(save_postハンドラー) ---
_suppress_auto = False   # 専用オペレーター経由の保存では二重処理しない
_snap_lock = threading.Lock()  # スナップショットのコピーを直列化


_THROTTLE_S = 600  # 自動履歴の最短間隔(秒)。Ctrl+S連打で世代が無限に増えるのを防ぐ


def _snapshot_bg(root_str: str, path_str: str, comment: str,
                 max_bytes: int = 0):
    """バックグラウンドでバージョン履歴へ退避(保存を一切遅らせない)。

    Blenderは内容が同じでも保存のたびにバイト列が変わり、ハッシュの重複排除が
    効かない。そのため自動記録は「新しいコメントが付いたら即」「それ以外は
    _THROTTLE_S 間隔」でまとめる(手動のスナップショットボタンは常に記録)。
    max_bytes>0 なら記録後に履歴の合計サイズ上限で自動整理する。
    """
    from . import versions
    with _snap_lock:
        try:
            path = Path(path_str)
            items = versions.list_versions(root_str, path)
            if items:
                m = items[0]["meta"]
                # 「空でない新しいコメント」だけが節目=即記録。
                # 空欄・同一コメントの連続保存は一定間隔にまとめる
                new_comment = bool(comment) \
                    and comment != (m.get("comment") or "")
                try:
                    from datetime import datetime
                    age = (datetime.now() - datetime.fromisoformat(
                        m.get("saved_at", ""))).total_seconds()
                except (ValueError, TypeError):
                    age = _THROTTLE_S + 1
                if not new_comment and age < _THROTTLE_S:
                    return
            versions.snapshot(root_str, path, comment)
            if max_bytes > 0:
                versions.enforce_size_cap(root_str, max_bytes)
        except Exception:
            pass


def _warn_popup(msg: str):
    if bpy.app.background:  # ヘッドレスにUIはない(表示系はクラッシュの元)
        print(f"Musubi警告: {msg}")
        return

    def draw(self, _context):
        for i in range(0, len(msg), 48):
            self.layout.label(text=msg[i:i + 48])
    try:
        bpy.context.window_manager.popup_menu(
            draw, title="Musubi: 保存時の警告", icon='ERROR')
    except Exception:
        pass


# --- リンク元の更新検知と再読み込み(開き直し不要) ---
_lib_mtimes: dict[str, float] = {}  # リンク元パス → 読み込み時点の更新日時


def _lib_paths():
    for lib in bpy.data.libraries:
        try:
            yield lib, Path(bpy.path.abspath(lib.filepath)).resolve()
        except (OSError, ValueError):
            continue


def _record_lib_mtimes():
    """今読み込んでいるリンク元の更新日時を基準として記録する。"""
    _lib_mtimes.clear()
    for _lib, p in _lib_paths():
        try:
            _lib_mtimes[str(p)] = p.stat().st_mtime
        except OSError:
            pass


def refresh_lib_notice(wm=None):
    """リンク元が更新されていないか確認し、パネルの黄色通知を更新する。

    イベント駆動(開く/保存/状態更新ボタンの瞬間)のみ。処理はリンク数回の
    stat比較だけで、常駐・ポーリングはしない。
    """
    if not hasattr(bpy.data, "libraries"):
        return
    outdated = []
    for _lib, p in _lib_paths():
        key = str(p)
        try:
            cur = p.stat().st_mtime
        except OSError:
            continue
        rec = _lib_mtimes.get(key)
        if rec is None:
            _lib_mtimes[key] = cur  # セッション中に追加されたリンクは基準登録
        elif cur > rec + 0.5:
            outdated.append(p.name)
    if wm is None:
        wm = getattr(bpy.context, "window_manager", None)
    try:
        wm.musubi_lib_outdated = ", ".join(sorted(set(outdated)))
    except (AttributeError, TypeError):
        pass


class MUSUBI_OT_reload_libs(bpy.types.Operator):
    """リンクしている.blend(背景・キャラ等)を、ファイルを開いたまま
最新の内容に読み直す。ポーズやキーフレームは保持される"""
    bl_idname = "musubi.reload_libs"
    bl_label = "リンクを再読み込み"

    def execute(self, context):
        n = 0
        for lib in list(bpy.data.libraries):
            try:
                lib.reload()
                n += 1
            except Exception as e:
                self.report({'WARNING'}, f"{lib.name}: {e}")
        _record_lib_mtimes()
        refresh_lib_notice(context.window_manager)
        self.report({'INFO'},
                    f"{n}件のリンク元を再読み込みしました" if n
                    else "リンクしているファイルはありません")
        return {'FINISHED'}


# --- 自動ロック(開いた人=作業中) ---
_our_lock: tuple[str, str] | None = None  # (root_str, blend_path)


def _set_lock_warning(wm, msg: str, stale: bool = False):
    """ロック警告を UI に渡す。`stale` は放置ロック解除ボタンの表示条件。

    経過時間の判定はここ(ハンドラ/ボタンの中)で1回だけ行い、結果を
    真偽値で渡します。UI層は draw() が毎フレーム呼ばれるため、
    ロックファイルを読みに行かせません。
    """
    try:
        wm.musubi_lock_warning = msg
        wm.musubi_lock_stale = stale
    except (AttributeError, TypeError):
        pass


def _sync_lock_state(root, fp: str, wm) -> str | None:
    """現ファイルのロックを取得し、前ファイルの自分のロックを返す。

    他端末が保持中なら警告文を返す(取得はしない=奪わない)。
    """
    global _our_lock
    if _our_lock and _our_lock[1] != fp:
        try:
            sync.release_lock(_our_lock[0], Path(_our_lock[1]))
        except Exception:
            pass
        _our_lock = None
    try:
        sync.acquire_lock(str(root), Path(fp))
        _our_lock = (str(root), fp)
        _set_lock_warning(wm, "")
        return None
    except PipelineError as e:
        try:
            stale = sync.is_stale_lock(sync.read_lock(Path(fp)))
        except Exception:
            stale = False
        _set_lock_warning(wm, str(e), stale)
        return str(e)


def _release_current_lock():
    """Blender終了・アドオン無効化時に自分のロックを返す。"""
    global _our_lock
    if _our_lock:
        try:
            sync.release_lock(_our_lock[0], Path(_our_lock[1]))
        except Exception:
            pass
        _our_lock = None


atexit.register(_release_current_lock)


def _alert_later(msg: str):
    """ロード直後でも確実に出せるよう、少し遅らせて警告ダイアログを出す。"""
    if bpy.app.background:
        print(f"Musubi警告: {msg}")
        return

    def _fire():
        try:
            wm = bpy.context.window_manager
            win = wm.windows[0] if wm.windows else None
            if win is not None:
                with bpy.context.temp_override(window=win):
                    bpy.ops.musubi.lock_alert('INVOKE_DEFAULT', message=msg)
            else:
                _warn_popup(msg)
        except Exception:
            pass
        return None
    try:
        bpy.app.timers.register(_fire, first_interval=0.5)
    except Exception:
        pass


class MUSUBI_OT_lock_alert(bpy.types.Operator):
    """他の人が作業中のファイルを開いたときの警告"""
    bl_idname = "musubi.lock_alert"
    bl_label = "このファイルは他の人が作業中です"

    message: bpy.props.StringProperty(options={'HIDDEN'})

    def invoke(self, context, event):
        return context.window_manager.invoke_props_dialog(self, width=400)

    def draw(self, context):
        col = self.layout.column()
        col.alert = True
        for i in range(0, len(self.message), 44):
            col.label(text=self.message[i:i + 44],
                      icon='ERROR' if i == 0 else 'BLANK1')
        col2 = self.layout.column()
        col2.label(text="編集せずに閉じて、本人に確認してください")
        col2.label(text="(この警告はパネル上部にも表示され続けます)")

    def execute(self, context):
        return {'FINISHED'}


@persistent
def on_load_post(_a=None, _b=None):
    """ファイルを開いた瞬間にロックを確認・取得する(開いた人=作業中)。

    他端末が保持中なら「保存する前に」ダイアログとパネルで警告する
    (保存後の警告では上書きしてからになり遅い)。プロジェクト外へ
    移ったときは、前のファイルの自分のロックを自動で返す。
    """
    global _our_lock
    fp = bpy.data.filepath
    wm = getattr(bpy.context, "window_manager", None)
    # このファイルのリンク元の更新日時を基準として記録
    _record_lib_mtimes()
    try:
        wm.musubi_lib_outdated = ""
    except (AttributeError, TypeError):
        pass
    root = core.detect_root(fp) if fp.endswith(".blend") else None
    if root is None:
        if _our_lock:
            try:
                sync.release_lock(_our_lock[0], Path(_our_lock[1]))
            except Exception:
                pass
            _our_lock = None
        _set_lock_warning(wm, "")
        return
    msg = _sync_lock_state(root, fp, wm)
    if msg:
        _alert_later(msg)


@persistent
def on_save_post(_a=None, _b=None):
    """プロジェクト内の保存(Ctrl+S含む)をパイプライン保存にする。

    - 全ファイル: バージョン履歴へ自動スナップショット(重複排除つき・
      バックグラウンド実行なので保存は重くならない)
    - カットファイル(scenes/sceneXX/cYY.blend): ロック取得(他人が保持中なら
      警告ポップアップ)・ステータス自動遷移・依存記録・品質チェック
    - プロジェクト外のファイルでは何もしない
    """
    fp = bpy.data.filepath
    root = core.detect_root(fp)
    if root is None:
        # プロジェクト外へ「名前を付けて保存」した場合も前のロックを返す
        if _our_lock and _our_lock[1] != fp:
            _release_current_lock()
            _set_lock_warning(getattr(bpy.context, "window_manager", None), "")
        return
    try:
        # 「保存はされている」ことをパネルに明示(履歴の10分ルールとの混同防止)
        bpy.context.window_manager.musubi_last_save_at = \
            time.strftime("%H:%M:%S")
    except AttributeError:
        pass
    if _suppress_auto:
        return
    try:
        wm = bpy.context.window_manager
        comment = wm.musubi_version_comment
    except AttributeError:
        wm, comment = None, ""
    # サイズ上限はメインスレッドで読む(バックグラウンドでbpyに触れないため)
    from .st_ops import pref_history_max_bytes
    try:
        max_bytes = pref_history_max_bytes()
    except Exception:
        max_bytes = 0
    threading.Thread(target=_snapshot_bg,
                     args=(str(root), fp, comment, max_bytes),
                     daemon=True, name="musubi-auto-snapshot").start()
    if wm is not None and comment:
        # コメントは今回の記録(または直前世代)に反映済み。次の保存へ
        # 持ち越さないよう空欄へ戻す(WMプロパティなのでファイルは汚れない)
        wm.musubi_version_comment = ""

    refresh_lib_notice()  # リンク元の更新確認(黄色通知)
    msg = _sync_lock_state(root, fp,
                           getattr(bpy.context, "window_manager", None))
    if msg:
        _warn_popup(msg)  # 保存自体は済んでいるため、衝突を大きく知らせる
    nums = core.parse_cut_path(root, fp)
    if not nums:
        return
    scene_no, cut_no = nums
    try:
        from . import tasks
        st = tasks.read_status(str(root), scene_no, cut_no)
        fields = {}
        if st["status"] in ("todo", "retake"):
            fields["status"] = "wip"
        if not st["assignee"]:
            import getpass
            fields["assignee"] = getpass.getuser()
        if fields:
            tasks.update_status(str(root), scene_no, cut_no, **fields)
            from . import board_html
            board_html.export_board_html(str(root))
    except Exception:
        pass
    try:
        from . import quality, quality_ops
        quality.record_deps(str(root), scene_no, cut_no)
        quality_ops.run_and_store(bpy.context)
    except Exception:
        pass


class MUSUBI_OT_create_structure(bpy.types.Operator):
    """業務内容に合ったテンプレートでフォルダ構造を作成する
(既存フォルダには触れず、足りないフォルダだけ追加)"""
    bl_idname = "musubi.create_structure"
    bl_label = "フォルダ構造を作成"

    template: bpy.props.EnumProperty(
        name="テンプレート",
        items=[(key, label, desc)
               for key, (label, desc, _dirs) in core.TEMPLATES.items()],
        default="film")

    def execute(self, context):
        try:
            created = core.create_structure(_root(context), self.template)
        except PipelineError as e:
            return _report_error(self, e)
        label = core.TEMPLATES[self.template][0]
        msg = (f"「{label}」で{len(created)}個のフォルダを作成"
               if created else "構造は作成済みです(不足なし)")
        self.report({'INFO'}, msg)
        return {'FINISHED'}


class MUSUBI_OT_save_cut(bpy.types.Operator):
    """現在のファイルを scenes/sceneXX/cYY.blend として配置保存する(初回用)。
配置後は通常の保存(Ctrl+S)だけで履歴・ロック・進捗が自動管理されます"""
    bl_idname = "musubi.save_cut"
    bl_label = "カットとして配置保存"

    def execute(self, context):
        global _suppress_auto
        from . import tasks, versions
        sc = context.scene
        _suppress_auto = True
        try:
            path = core.cut_blend_path(_root(context), sc.musubi_scene_no,
                                       sc.musubi_cut_no)
            sync.acquire_lock(_root(context), path)
            global _our_lock
            # 直前まで開いていた別ファイルのロックを返す(このパスは
            # _suppress_auto により on_save_post の解放処理を通らない)
            if _our_lock and _our_lock[1] != str(path):
                try:
                    sync.release_lock(_our_lock[0], Path(_our_lock[1]))
                except Exception:
                    pass
            _our_lock = (_root(context), str(path))
            save_mainfile_retry(str(path))
            meta = versions.snapshot(
                _root(context), path,
                context.window_manager.musubi_version_comment)
            if meta:
                context.window_manager.musubi_version_comment = ""
            # ステータス自動遷移: 未着手/リテイク → 作業中、担当が空なら自分に
            st = tasks.read_status(_root(context), sc.musubi_scene_no,
                                   sc.musubi_cut_no)
            fields = {}
            if st["status"] in ("todo", "retake"):
                fields["status"] = "wip"
            if not st["assignee"]:
                import getpass
                fields["assignee"] = getpass.getuser()
            if fields:
                tasks.update_status(_root(context), sc.musubi_scene_no,
                                    sc.musubi_cut_no, **fields)
                try:
                    from . import board_html
                    board_html.export_board_html(_root(context))
                except Exception:
                    pass
            # 依存関係(リンク)の記録と品質チェック(赤アラート表示用)
            try:
                from . import quality, quality_ops
                quality.record_deps(_root(context), sc.musubi_scene_no,
                                    sc.musubi_cut_no)
                quality_ops.run_and_store(context)
            except Exception:
                pass
        except (PipelineError, RuntimeError) as e:
            return _report_error(self, e)
        finally:
            _suppress_auto = False
        ver = "(新しい世代を履歴に保存)" if meta else "(内容変化なし・履歴は前回のまま)"
        self.report({'INFO'}, f"保存しました: {path.name} {ver}")
        return {'FINISHED'}


class MUSUBI_OT_release_lock(bpy.types.Operator):
    """現在のカットのロックを解放(作業終了時に実行)"""
    bl_idname = "musubi.release_lock"
    bl_label = "ロックを解放"

    def execute(self, context):
        sc = context.scene
        try:
            path = core.cut_blend_path(_root(context), sc.musubi_scene_no,
                                       sc.musubi_cut_no)
            released = sync.release_lock(_root(context), path)
        except PipelineError as e:
            return _report_error(self, e)
        self.report({'INFO'}, "ロックを解放しました" if released else "ロックはありません")
        return {'FINISHED'}


class MUSUBI_OT_force_release_lock(bpy.types.Operator):
    """放置ロック(12時間以上)を解除して、自分が作業を引き継ぐ"""
    bl_idname = "musubi.force_release_lock"
    bl_label = "放置ロックを解除"

    # invoke で読み取った内容を draw に渡す(draw では I/O をしない)
    holder: bpy.props.StringProperty(options={'HIDDEN'})
    age_text: bpy.props.StringProperty(options={'HIDDEN'})
    confirmed: bpy.props.BoolProperty(
        name="本人が作業していないことを確認した", default=False,
        description="連絡がついた、または連絡がつかず作業中でないと判断できる場合だけ")

    def invoke(self, context, event):
        fp = bpy.data.filepath
        info = sync.read_lock(Path(fp)) if fp.endswith(".blend") else None
        if info is None:
            self.report({'INFO'}, "ロックはありません")
            return {'CANCELLED'}
        self.holder = f"{info.get('user','?')}@{info.get('host','?')}"
        self.age_text = f"{sync.lock_age_hours(info):.0f}時間"
        self.confirmed = False
        return context.window_manager.invoke_props_dialog(self, width=420)

    def draw(self, context):
        col = self.layout.column()
        col.label(text=f"{self.holder} のロックを解除します", icon='UNLOCKED')
        col.label(text=f"取得から {self.age_text} 経過しています", icon='BLANK1')
        warn = self.layout.column()
        warn.alert = True
        warn.label(text="本人がまだ作業中なら、その作業が消えることがあります",
                   icon='ERROR')
        self.layout.prop(self, "confirmed")

    def execute(self, context):
        if not self.confirmed:
            self.report({'WARNING'}, "確認にチェックが入っていません(解除しません)")
            return {'CANCELLED'}
        fp = bpy.data.filepath
        root = core.detect_root(fp)
        if root is None:
            self.report({'WARNING'}, "プロジェクト内のファイルではありません")
            return {'CANCELLED'}
        try:
            info = sync.force_release_lock(str(root), Path(fp))
        except PipelineError as e:
            return _report_error(self, e)
        # 解除しただけでは誰も持っていない状態になるので、続けて自分が取得する
        msg = _sync_lock_state(root, fp, getattr(bpy.context, "window_manager", None))
        who = f"{info.get('user','?')}@{info.get('host','?')}"
        if msg:
            self.report({'WARNING'}, f"{who} のロックを解除しましたが、取得できませんでした")
        else:
            self.report({'INFO'}, f"{who} の放置ロックを解除し、作業を引き継ぎました")
        return {'FINISHED'}


class MUSUBI_OT_render_output(bpy.types.Operator):
    """output/sceneXX/cYY.NNN.mp4 に次バージョン番号でレンダリング"""
    bl_idname = "musubi.render_output"
    bl_label = "最終出力をレンダリング"

    def execute(self, context):
        sc = context.scene
        try:
            target = core.next_output_path(_root(context), sc.musubi_scene_no,
                                           sc.musubi_cut_no)
        except PipelineError as e:
            return _report_error(self, e)

        rd = sc.render
        if hasattr(rd.image_settings, "media_type"):  # Blender 5.x
            rd.image_settings.media_type = 'VIDEO'
        rd.image_settings.file_format = 'FFMPEG'
        rd.ffmpeg.format = 'MPEG4'
        rd.ffmpeg.codec = 'H264'
        rd.ffmpeg.audio_codec = 'AAC'
        rd.use_file_extension = True
        rd.filepath = str(target)

        start = time.time()
        try:
            bpy.ops.render.render(animation=True, write_still=False)
        except RuntimeError as e:
            return _report_error(self, e)

        # Blenderが拡張子前にフレーム範囲を付けた場合は規定名にリネームする
        if not target.exists():
            candidates = [
                p for p in target.parent.glob(target.stem + "*.mp4")
                if p.stat().st_mtime >= start - 1
            ]
            if candidates:
                candidates[0].rename(target)
        if not target.exists():
            self.report({'ERROR'}, "出力ファイルが見つかりません")
            return {'CANCELLED'}
        # ステータス自動遷移: 最終出力を出した → レビュー待ち
        try:
            from . import board_html, tasks
            tasks.update_status(_root(context), sc.musubi_scene_no,
                                sc.musubi_cut_no, status="review")
            board_html.export_board_html(_root(context))
        except (PipelineError, OSError):
            pass
        self.report({'INFO'}, f"出力: {target.name}(ステータス→レビュー待ち)")
        return {'FINISHED'}


class MUSUBI_OT_write_manifest(bpy.types.Operator):
    """この端末の全ファイルのSHA-256照合リストを作成する(同期検証の基準になる)"""
    bl_idname = "musubi.write_manifest"
    bl_label = "同期照合リストを作成"

    def execute(self, context):
        try:
            out = sync.write_manifest(_root(context))
        except PipelineError as e:
            return _report_error(self, e)
        self.report({'INFO'}, f"照合リストを作成: {out.name}")
        return {'FINISHED'}


class MUSUBI_OT_verify_sync(bpy.types.Operator):
    """他端末の照合リストとローカルファイルを突き合わせて同期状態を検証"""
    bl_idname = "musubi.verify_sync"
    bl_label = "同期状態を検証"

    def execute(self, context):
        wm = context.window_manager
        try:
            peers = sync.list_peer_manifests(_root(context))
            if not peers:
                wm.musubi_last_report = "他端末の照合リストがまだ同期されていません"
                self.report({'WARNING'}, wm.musubi_last_report)
                return {'FINISHED'}
            lines = []
            all_ok = True
            for mp in peers:
                r = sync.verify_against(_root(context), mp)
                if r["in_sync"]:
                    lines.append(f"[OK] {r['peer']}: 完全一致 ({len(r['match'])}ファイル)")
                else:
                    all_ok = False
                    lines.append(
                        f"[NG] {r['peer']}: 一致{len(r['match'])} / "
                        f"未到達{len(r['missing_here'])} / 差分{len(r['differs'])} / "
                        f"こちらのみ{len(r['extra_here'])}"
                    )
                    for rel in (r["missing_here"] + r["differs"])[:5]:
                        lines.append(f"    - {rel}")
        except PipelineError as e:
            return _report_error(self, e)
        wm.musubi_last_report = "\n".join(lines)
        level = 'INFO' if all_ok else 'WARNING'
        self.report({level}, lines[0] if len(lines) == 1 else f"{len(peers)}端末を検証")
        return {'FINISHED'}


class MUSUBI_OT_audit(bpy.types.Operator):
    """セキュリティ監査(Auto Run設定・危険ファイル・競合・ロック・symlink)"""
    bl_idname = "musubi.audit"
    bl_label = "セキュリティ監査"

    def execute(self, context):
        results = security.audit(_root(context))
        lines = [f"[{lv}] {msg}" for lv, msg in results]
        context.window_manager.musubi_last_report = "\n".join(lines)
        worst = ('ERROR' if any(lv == security.LEVEL_CRITICAL for lv, _ in results)
                 else 'WARNING' if any(lv == security.LEVEL_WARN for lv, _ in results)
                 else 'INFO')
        self.report({worst}, f"監査完了: {len(lines)}項目(詳細はパネル参照)")
        return {'FINISHED'}


CLASSES = (
    MUSUBI_OT_lock_alert,
    MUSUBI_OT_reload_libs,
    MUSUBI_OT_create_structure,
    MUSUBI_OT_save_cut,
    MUSUBI_OT_release_lock,
    MUSUBI_OT_force_release_lock,
    MUSUBI_OT_render_output,
    MUSUBI_OT_write_manifest,
    MUSUBI_OT_verify_sync,
    MUSUBI_OT_audit,
)
