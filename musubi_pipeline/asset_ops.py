# SPDX-FileCopyrightText: 2026 mochizukimonta
# SPDX-License-Identifier: GPL-3.0-or-later
"""musubi_pipeline.asset_ops — アセット一覧のオペレーターとUI部品。

走査は `assets.py`(bpy非依存)。ここは Blender 側の器で、
**ディスクI/Oは `refresh()` と操作ボタンの中だけ**で行う
(`draw()` / `poll()` からは読まない。ARCHITECTURE の「UI層でI/Oをしない」)。

「開く」をここに置く目的は利便性ではなく、**開く前にロック状態が見えること**。
v0.19.0 以降、他人が作業中のファイルは開いた瞬間に警告されるが、その時点で
すでにロックは取得済みで、相手には「誰かが開いた」が伝わっている。一覧から
開けば、開く前に「作業中」を見て引き返せる。
"""

from __future__ import annotations

from pathlib import Path

import bpy

from . import assets, core
from .core import PipelineError

# 行サムネイルを読み込む上限。履歴(既定20世代)と違い、アセット数には
# 自然な上限がない。超えた場合は行の読み込みをやめ、選択中の1件だけ読む
THUMB_LIMIT = 200

# アセット一覧用のプレビューコレクション。**バージョン履歴とは別に持つ**
# (履歴は「いま開いているファイルの世代」、こちらは「プロジェクト全体の
# ファイル」で、作り直される時機=寿命が違う)
_pcoll = None

# 上限を超えているか。超えているときだけ、選択された1件を遅れて読み込む
_over_limit = False

# 遅延読み込みした1件の (キー, 行番号)。次の選択で解放する
_lazy: tuple[str, int] | None = None


def preview_register():
    """アセット一覧用のプレビューコレクションを1つだけ作る。"""
    global _pcoll
    # ヘッドレスではアイコンが割り当てられないので、作るだけ無駄
    if _pcoll is None and not bpy.app.background:
        from bpy.utils import previews
        _pcoll = previews.new()


def preview_unregister():
    global _pcoll, _lazy, _over_limit
    if _pcoll is not None:
        from bpy.utils import previews
        previews.remove(_pcoll)
        _pcoll = None
    _lazy = None
    _over_limit = False


def _load_preview(key: str, path: Path) -> int:
    """.blend の埋め込みプレビューを読み、アイコンIDを返す(無ければ0)。

    ver_ops と同じ規則。引数は**位置引数**で渡す(4.2〜4.5 は
    (name, path, path_type)、5.x は (name, filepath, file_type) と
    名前が違い、キーワードで渡すと片方で TypeError になる)。
    有無を判別できるのは `image_size` だけで、`icon_id` は
    プレビューが無くてもファイルが無くても非0を返す。
    """
    if _pcoll is None:
        return 0
    try:
        prev = _pcoll.load(key, str(path), 'BLEND')
        if prev.image_size[0] <= 0:
            return 0
        return prev.icon_id
    except Exception:
        return 0


class MusubiAssetItem(bpy.types.PropertyGroup):
    # name(PropertyGroup既定のプロパティ)は UIList 標準の絞り込み・
    # 並べ替えが見る項目。ファイル名を入れておく
    rel: bpy.props.StringProperty()
    abs_path: bpy.props.StringProperty()
    label: bpy.props.StringProperty()
    age: bpy.props.StringProperty()
    author: bpy.props.StringProperty()
    comment: bpy.props.StringProperty()
    saved_at: bpy.props.StringProperty()
    generations: bpy.props.IntProperty(default=0)
    lock_user: bpy.props.StringProperty()
    lock_host: bpy.props.StringProperty()
    lock_age: bpy.props.StringProperty()
    lock_stale: bpy.props.BoolProperty(default=False)
    cuts: bpy.props.StringProperty()
    cut_count: bpy.props.IntProperty(default=0)
    # サムネイルのアイコンID。refresh のたびに読み直され、そのつど新しいIDが
    # 割り当たるので、前回の値は使い回せない(解放済みIDを描いてしまう)
    icon_id: bpy.props.IntProperty(default=0)


class MUSUBI_UL_assets(bpy.types.UIList):
    """1行 = 1アセット。UIList の行は1段しか使えないので情報は1行に畳む。

    詳細(相対パス・世代数・使用カット・保持者の端末名)は選択行の下に出す。
    """

    def draw_item(self, context, layout, data, item, icon,
                  active_data, active_propname):
        row = layout.row(align=True)
        if item.icon_id:
            row.label(text=item.label, icon_value=item.icon_id)
        else:
            row.label(text=item.label, icon='FILE_BLEND')
        if item.lock_user:
            sub = row.row(align=True)
            sub.alignment = 'RIGHT'
            # 赤は CRITICAL のみ。放置ロックは黄アイコン+通常文字
            sub.label(text=f"作業中:{item.lock_user}",
                      icon='COLLECTION_COLOR_03' if item.lock_stale
                      else 'LOCKED')
        op = row.operator("musubi.open_blend", text="", icon='FILEBROWSER')
        op.rel = item.rel


# ---------------------------------------------------------------------------
# 一覧の組み立て
# ---------------------------------------------------------------------------

def _row_label(r: dict) -> str:
    """1行に畳む表示文字列。

    サイドバーの既定幅では `user@host` まで置くと行が窮屈になるので、
    行内は**名前だけ**にして、端末名は選択行の詳細に回す。
    """
    parts = [r["name"], r["age"]]
    if r["author"]:
        parts.append(r["author"])
    if r["comment"]:
        parts.append(f"「{r['comment'][:20]}」")
    elif not r["generations"]:
        parts.append("(履歴なし)")
    if r["cuts"]:
        parts.append(f"使用{len(r['cuts'])}")
    return "  ".join(parts)


def _lock_age_text(r: dict) -> str:
    """ロックの経過時間の表示。

    ロックファイルは同期で運ばれてくるので、壊れていることがある
    (`sync.read_lock` は取得時刻0や `inf` を返す)。桁の合わない数字を
    そのまま出すより「不明」の方が正しい。
    """
    if not r["lock_user"]:
        return ""
    hours = r["lock_age_h"]
    if hours != hours or hours in (float("inf"), float("-inf")) \
            or hours > 24 * 365:
        return "取得時刻不明"
    return f"{hours:.1f}時間"


def _release_lazy(wm) -> None:
    """遅延読み込みした1件を解放する(解放済みIDを描かないよう0に戻す)。"""
    global _lazy
    if _lazy is None:
        return
    key, index = _lazy
    _lazy = None
    try:
        if 0 <= index < len(wm.musubi_assets):
            wm.musubi_assets[index].icon_id = 0
    except (AttributeError, TypeError):
        pass
    try:
        del _pcoll[key]
    except (KeyError, TypeError, AttributeError):
        pass


def _load_selected(wm) -> None:
    """上限超過時に、選択中の1件だけサムネイルを読む。

    `draw()` からは呼ばれない(選択の変更時と `refresh()` の末尾だけ)ので、
    ここでのファイル読み込みは「UI層でI/Oをしない」に反しない。
    """
    global _lazy
    if _pcoll is None or not _over_limit:
        return
    index = wm.musubi_assets_index
    if not (0 <= index < len(wm.musubi_assets)):
        return
    if _lazy is not None and _lazy[1] == index:
        return
    _release_lazy(wm)
    item = wm.musubi_assets[index]
    key = f"sel:{index}:{item.rel}"
    item.icon_id = _load_preview(key, Path(item.abs_path))
    # プレビューが無いファイル(アイコンID 0)でもキーはコレクションに
    # 載っているので、必ず記録して次の選択で解放する。同じキーで2度
    # load すると版によっては例外になる
    _lazy = (key, index)


def on_index_update(self, context):
    """選択が変わったときのフック(self は WindowManager)。"""
    _load_selected(self)


def refresh(context) -> None:
    """一覧を作り直す。ディスクを読むのはここだけ。"""
    global _over_limit
    wm = context.window_manager
    _release_lazy(wm)
    wm.musubi_assets.clear()
    wm.musubi_assets_over_limit = False
    _over_limit = False
    if _pcoll is not None:
        # 前回のサムネイルはここで必ず捨てる(clear と load を対で回す)
        _pcoll.clear()
    try:
        rows = assets.scan(context.scene.musubi_project_root)
    except PipelineError as e:
        wm.musubi_assets_summary = str(e)
        return
    _over_limit = len(rows) > THUMB_LIMIT
    for i, r in enumerate(rows):
        item = wm.musubi_assets.add()
        item.name = r["name"]
        item.rel = r["rel"]
        item.abs_path = str(r["path"])
        item.label = _row_label(r)
        item.age = r["age"]
        item.author = r["author"]
        item.comment = r["comment"]
        item.saved_at = r["saved_at"]
        item.generations = r["generations"]
        item.lock_user = r["lock_user"]
        item.lock_host = r["lock_host"]
        item.lock_stale = r["lock_stale"]
        item.lock_age = _lock_age_text(r)
        item.cuts = ", ".join(r["cuts"][:40])
        item.cut_count = len(r["cuts"])
        if not _over_limit:
            # 同名ファイルが別フォルダにあってもキーが衝突しないよう番号を足す
            item.icon_id = _load_preview(f"{i}:{r['rel']}", r["path"])
    wm.musubi_assets_over_limit = _over_limit
    locked = sum(1 for r in rows if r["lock_user"])
    wm.musubi_assets_summary = (
        f"{len(rows)}件" + (f" / 作業中 {locked}件" if locked else ""))
    wm.musubi_assets_index = min(wm.musubi_assets_index,
                                 max(0, len(wm.musubi_assets) - 1))
    _load_selected(wm)


class MUSUBI_OT_assets_refresh(bpy.types.Operator):
    """アセット(カット以外の.blend)の一覧を読み直す"""
    bl_idname = "musubi.assets_refresh"
    bl_label = "一覧を更新"

    def execute(self, context):
        refresh(context)
        return {'FINISHED'}


class MUSUBI_OT_open_blend(bpy.types.Operator):
    """このファイルを開く

開くとこのファイルのロックを取得します(あなたが作業中だと全員に伝わります)。
他の人が作業中でも開けますが、その場合は警告が出ます。
いまのファイルに未保存の変更があるときは、先に保存してから開きます"""
    bl_idname = "musubi.open_blend"
    bl_label = "開く"

    # ルートからの相対パス(POSIX)。開く直前に safe_path で検証し直す
    rel: bpy.props.StringProperty(options={'HIDDEN'})

    def invoke(self, context, event):
        return context.window_manager.invoke_confirm(self, event)

    def execute(self, context):
        from . import ops as ops_mod
        root = context.scene.musubi_project_root
        rel = (self.rel or "").strip()
        if not rel:
            self.report({'ERROR'}, "開くファイルが指定されていません")
            return {'CANCELLED'}
        try:
            rootp = core.resolve_root(root)
            path = core.safe_path(rootp, *rel.split("/"))
        except PipelineError as e:
            self.report({'ERROR'}, str(e))
            return {'CANCELLED'}
        if not path.is_file():
            self.report({'ERROR'},
                        f"ファイルがありません(一覧を更新してください): {rel}")
            return {'CANCELLED'}
        # 未保存の編集を黙って捨てない。execute から open_mainfile を呼ぶと
        # 保存確認ダイアログが出ないため、ここで保存しないと失われる
        # (通常の保存として扱うので、履歴にも普段どおり記録される)
        if bpy.data.is_dirty:
            if not bpy.data.filepath:
                self.report({'ERROR'},
                            "いまのファイルが未保存です。先に保存してください")
                return {'CANCELLED'}
            try:
                ops_mod.save_mainfile_retry()
            except (RuntimeError, OSError) as e:
                self.report({'ERROR'}, f"いまのファイルを保存できません: {e}")
                return {'CANCELLED'}
        try:
            # ロックの確認・取得と警告は on_load_post が行う(v0.19.0)。
            # ここで独自に警告を出すと二重になる
            bpy.ops.wm.open_mainfile(filepath=str(path))
        except RuntimeError as e:
            self.report({'ERROR'}, str(e))
            return {'CANCELLED'}
        sc = bpy.context.scene
        try:
            sc.musubi_project_root = root
            nums = core.parse_cut_path(rootp, str(path))
            if nums:
                sc.musubi_scene_no, sc.musubi_cut_no = nums
        except (AttributeError, TypeError):
            pass
        refresh(bpy.context)
        self.report({'INFO'}, f"開きました: {path.name}")
        return {'FINISHED'}


# ---------------------------------------------------------------------------
# パネルの中身(見出しは ui.py 側のパネルヘッダー)
# ---------------------------------------------------------------------------

def _wrap(col, text: str, width: int = 38, indent: str = "  "):
    for i in range(0, len(text), width):
        col.label(text=indent + text[i:i + width])


def _draw_detail(layout, item):
    """選択した1件の詳細。大きいサムネイル・相対パス・世代数・使用カット。"""
    box = layout.box()
    if item.icon_id:
        box.template_icon(icon_value=item.icon_id, scale=7.0)
    col = box.column(align=True)
    col.label(text=item.name, icon='FILE_BLEND')
    _wrap(col, item.rel)
    if item.generations:
        col.label(text=f"最新世代: {item.saved_at or '?'} "
                       f"{item.author or '?'} / {item.generations}世代",
                  icon='RECOVER_LAST')
        if item.comment:
            _wrap(col, f"「{item.comment}」")
    else:
        col.label(text="履歴なし(このPCに世代が届いていません)",
                  icon='RECOVER_LAST')
    col.label(text=f"ファイルの更新: {item.age}", icon='TIME')
    if item.lock_user:
        lock = box.column(align=True)
        # 放置ロックは黄アイコン。解除の導線はここには置かない
        # (対象を開いてから、v0.28.0 の導線で外す)
        icon = 'COLLECTION_COLOR_03' if item.lock_stale else 'LOCKED'
        lock.label(text=f"作業中: {item.lock_user}@{item.lock_host}"
                        f"({item.lock_age})", icon=icon)
        if item.lock_stale:
            lock.label(text="放置ロックの可能性(開いてから解除できます)",
                       icon='BLANK1')
    if item.cut_count:
        use = box.column(align=True)
        use.label(text=f"使用: {item.cut_count}カット", icon='LINKED')
        _wrap(use, item.cuts)
    else:
        box.label(text="使用カットの記録なし", icon='UNLINKED')
    big = box.column()
    big.scale_y = 1.3
    big.operator("musubi.open_blend", text=f"{item.name} を開く",
                 icon='FILEBROWSER').rel = item.rel


def draw_assets_box(layout, context):
    wm = context.window_manager
    row = layout.row(align=True)
    row.label(text=wm.musubi_assets_summary or "「更新」で一覧を作ります",
              icon='PACKAGE')
    row.operator("musubi.assets_refresh", text="", icon='FILE_REFRESH')
    layout.template_list("MUSUBI_UL_assets", "", wm, "musubi_assets",
                         wm, "musubi_assets_index", rows=6)
    if wm.musubi_assets_over_limit:
        note = layout.column(align=True)
        note.label(text=f"{THUMB_LIMIT}件を超えるため、行のサムネイルは",
                   icon='ERROR')
        note.label(text="止めています(選択した1件だけ表示)", icon='BLANK1')
    idx = wm.musubi_assets_index
    if 0 <= idx < len(wm.musubi_assets):
        _draw_detail(layout, wm.musubi_assets[idx])


CLASSES = (
    MusubiAssetItem,
    MUSUBI_UL_assets,
    MUSUBI_OT_assets_refresh,
    MUSUBI_OT_open_blend,
)
