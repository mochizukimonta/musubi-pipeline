# SPDX-FileCopyrightText: 2026 mochizukimonta
# SPDX-License-Identifier: GPL-3.0-or-later
"""musubi_pipeline.asset_ops — アセット一覧(ポップアップ)とファイルを開く導線。

走査は `assets.py`(bpy非依存)。ここは Blender 側の器で、
**ディスクI/Oはオペレーターの `invoke()` / `execute()` の中だけ**で行う
(`draw()` / `poll()` からは読まない。ARCHITECTURE の「UI層でI/Oをしない」)。

**v0.32 でサイドバーの常設パネルからポップアップへ移した。**
サイドバーの他のパネルはすべて `context.scene` に紐づく「いま開いている
ファイル」の表示だが、アセット一覧だけは対象がプロジェクト全体で、
参照スコープが1階層違う。同じ場所に同じ見た目で並ぶと情報の階層が混ざる。
呼び出して使う別の場所に移すことで、この違いを見た目に出す。

一覧は既定でサムネイルのグリッド。絵で探せることが目的だが、グリッドの
セルには文字がほとんど入らないので、更新時刻・作者・コメントを追いたい
ときのためにリスト表示へ切り替えられる(同じ UIList の描き分け)。

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
# 自然な上限がない。超えた場合は一覧ぶんの読み込みをやめ、選択中の1件だけ読む
THUMB_LIMIT = 200

# グリッドの列数と、ポップアップの幅(px)。
# 6列 × 約100pxのセル + 余白 で 900。試作を実機で撮って決めた値
GRID_COLUMNS = 6
POPUP_WIDTH = 900

# グリッドに並べる最大数(6列 × 6行)。
# **ポップアップは画面高でクランプされ、あふれた分は隠れる**(実測)。
# 隠れたことに気づけないのがいちばん困るので、描く数を先に区切って
# 「あと何件あるか」を文字で出す。全部見たいときはリスト表示
# (こちらは template_list が自前でスクロールする)。
GRID_MAX = 36

# アセット一覧用のプレビューコレクション。**バージョン履歴とは別に持つ**
# (履歴は「いま開いているファイルの世代」、こちらは「プロジェクト全体の
# ファイル」で、作り直される時機=寿命が違う)
_pcoll = None

# 上限を超えているか。超えているときだけ、選択された1件を遅れて読み込む
_over_limit = False

# 遅延読み込みした1件の (キー, 行番号)。次の選択で解放する
_lazy: tuple[str, int] | None = None

# 走査結果そのもの(絞り込み前)。絞り込みはこれを読み直すだけで、
# ディスクには触れない
_scanned: list[dict] = []

# 相対パス → アイコンID。絞り込みで行を組み直すときに使い回す
# (プレビューの読み込みは1回の走査につき1度だけ)
_icons: dict[str, int] = {}

# プレビューが無いファイル用の代替アイコン。グリッドでは
# icon_value=0 を渡すとセルが潰れて下のラベルがずれるため、
# 同じ大きさの組み込みアイコンを敷いて高さを揃える
_placeholder_icon = 0


def _builtin_icon(name: str) -> int:
    """組み込みアイコンの数値ID。取れなければ0(その場合は敷かない)。"""
    try:
        params = bpy.types.UILayout.bl_rna.functions["prop"].parameters
        return params["icon"].enum_items[name].value
    except Exception:
        return 0


def preview_register():
    """アセット一覧用のプレビューコレクションを1つだけ作る。"""
    global _pcoll, _placeholder_icon
    # ヘッドレスではアイコンが割り当てられないので、作るだけ無駄
    if _pcoll is None and not bpy.app.background:
        from bpy.utils import previews
        _pcoll = previews.new()
    _placeholder_icon = _builtin_icon('FILE_BLEND')


def preview_unregister():
    global _pcoll, _lazy, _over_limit
    if _pcoll is not None:
        from bpy.utils import previews
        previews.remove(_pcoll)
        _pcoll = None
    _lazy = None
    _over_limit = False
    _scanned.clear()
    _icons.clear()


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
    # サムネイルのアイコンID。プレビューは走査のたびに読み直され、そのつど
    # 新しいIDが割り当たるので、前回の値は使い回せない(解放済みIDを描く)
    icon_id: bpy.props.IntProperty(default=0)


def _cell_name(name: str, limit: int) -> str:
    """グリッドのセルに入れる短い名前。

    セルの幅は列数で決まり、10文字強しか入らない。拡張子は全部 `.blend`
    で区別に使えないので落とす(`akane_body.ble` のように途中で切れるより、
    `akane_body` の方が読める)。切ったことは「…」で示す。
    """
    if name.lower().endswith(".blend"):
        name = name[:-6]
    return name if len(name) <= limit else name[:limit - 1] + "…"


class MUSUBI_UL_assets(bpy.types.UIList):
    """リスト表示(1行=1アセット)。

    **グリッドは UIList では描かない。**`template_list(type='GRID')` は
    Blender 5.x で削除されており(4.2〜4.5 にはある)、対応下限から最新まで
    同じ見た目にできない。グリッドは `grid_flow` で自前に描く(`_draw_grid`)。

    こちらのリストは template_list なので**自前で縦スクロールする**。
    件数が多いときに全部を見られるのはこちら側。
    """

    def draw_item(self, context, layout, data, item, icon,
                  active_data, active_propname, index):
        row = layout.row(align=True)
        if item.icon_id:
            row.label(text=item.label, icon_value=item.icon_id)
        else:
            row.label(text=item.label, icon='FILE_BLEND')
        if item.lock_user:
            sub = row.row(align=True)
            sub.alignment = 'RIGHT'
            # 赤は CRITICAL のみ。放置ロックは黄アイコン + 通常文字
            sub.label(text=f"作業中:{item.lock_user}",
                      icon='COLLECTION_COLOR_03' if item.lock_stale
                      else 'LOCKED')


# ---------------------------------------------------------------------------
# 走査と組み立て
# ---------------------------------------------------------------------------

def _row_label(r: dict) -> str:
    """リスト表示で1行に畳む文字列。

    ポップアップの幅でも `user@host` まで置くと窮屈なので、行内は
    **名前だけ**にして、端末名は選択行の詳細に回す。
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

    `draw()` からは呼ばれない(選択の変更時と組み立ての末尾だけ)ので、
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


def _populate(wm) -> None:
    """走査結果から表示用の一覧を組み直す。**ディスクには触れない。**

    絞り込み欄を打つたびに呼ばれるので、ここでファイルを読んではいけない
    (プレビューは走査時に読み終えていて、`_icons` から配るだけ)。
    """
    _release_lazy(wm)
    wm.musubi_assets.clear()
    rows = assets.filter_rows(_scanned, wm.musubi_assets_filter)
    for r in rows:
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
        item.icon_id = _icons.get(r["rel"], 0)
    locked = sum(1 for r in rows if r["lock_user"])
    total = f"{len(rows)}件" if len(rows) == len(_scanned) \
        else f"{len(rows)}/{len(_scanned)}件"
    wm.musubi_assets_summary = total + (f" / 作業中 {locked}件" if locked else "")
    wm.musubi_assets_index = min(wm.musubi_assets_index,
                                 max(0, len(wm.musubi_assets) - 1))
    _load_selected(wm)


def on_filter_update(self, context):
    """絞り込み欄が変わったときのフック(self は WindowManager)。"""
    _populate(self)


def on_index_update(self, context):
    """選択が変わったときのフック(self は WindowManager)。"""
    _load_selected(self)


def refresh(context) -> None:
    """走査してプレビューを読み、一覧を組み立てる。ディスクを読むのはここだけ。"""
    global _over_limit
    wm = context.window_manager
    _release_lazy(wm)
    wm.musubi_assets.clear()
    wm.musubi_assets_over_limit = False
    _over_limit = False
    _scanned.clear()
    _icons.clear()
    if _pcoll is not None:
        # 前回のサムネイルはここで必ず捨てる(clear と load を対で回す)
        _pcoll.clear()
    try:
        rows = assets.scan(context.scene.musubi_project_root)
    except PipelineError as e:
        wm.musubi_assets_summary = str(e)
        return
    _scanned.extend(rows)
    _over_limit = len(rows) > THUMB_LIMIT
    if not _over_limit:
        for i, r in enumerate(rows):
            # 同名ファイルが別フォルダにあってもキーが衝突しないよう番号を足す
            _icons[r["rel"]] = _load_preview(f"{i}:{r['rel']}", r["path"])
    wm.musubi_assets_over_limit = _over_limit
    _populate(wm)


# ---------------------------------------------------------------------------
# 一覧のポップアップ
# ---------------------------------------------------------------------------

def _draw_detail(layout, item):
    """選択した1件の詳細。グリッドのセルには入らない情報をここに集める。"""
    box = layout.box()
    col = box.column(align=True)
    col.label(text=item.name, icon='FILE_BLEND')
    col.label(text=item.rel)
    line = box.row(align=True)
    if item.generations:
        line.label(text=f"最新世代: {item.saved_at or '?'} "
                        f"{item.author or '?'} / {item.generations}世代",
                   icon='RECOVER_LAST')
    else:
        line.label(text="履歴なし(このPCに世代が届いていません)",
                   icon='RECOVER_LAST')
    line.label(text=f"ファイルの更新: {item.age}", icon='TIME')
    if item.comment:
        box.label(text=f"「{item.comment}」")
    if item.lock_user:
        # 放置ロックは黄アイコン。解除の導線はここには置かない
        # (対象を開いてから、v0.28.0 の導線で外す)
        lock = box.row(align=True)
        lock.label(text=f"作業中: {item.lock_user}@{item.lock_host}"
                        f"({item.lock_age})",
                   icon='COLLECTION_COLOR_03' if item.lock_stale else 'LOCKED')
        if item.lock_stale:
            lock.label(text="放置ロックの可能性(開いてから解除できます)")
    if item.cut_count:
        box.label(text=f"使用: {item.cut_count}カット — {item.cuts}",
                  icon='LINKED')
    else:
        box.label(text="使用カットの記録なし", icon='UNLINKED')
    big = box.column()
    big.scale_y = 1.4
    big.operator("musubi.open_blend", text=f"{item.name} を開く",
                 icon='FILEBROWSER').rel = item.rel


def _draw_grid(layout, wm) -> None:
    """サムネイルのグリッド。**セルのボタンがそのまま「開く」**。

    ポップアップの中ではオペレーターを押した時点でポップアップが閉じるため、
    「選んでから開く」の2段にはできない(選択はプロパティ操作なので閉じない
    が、大きいサムネイルを押せるボタンにする方法が無い —
    `operator(icon_value=…)` はセルを拡大してもアイコンが1単位のまま)。
    そこで**サムネイルの下のボタンが名前ラベル兼「開く」**になっている。
    そのファイルの詳細(コメント・ロック・使用カット)はボタンの
    ツールチップに出る(`MUSUBI_OT_open_blend.description`)。
    """
    shown = min(len(wm.musubi_assets), GRID_MAX)
    grid = layout.grid_flow(row_major=True, columns=GRID_COLUMNS,
                            even_columns=True, even_rows=True)
    for i in range(shown):
        item = wm.musubi_assets[i]
        cell = grid.column(align=True)
        # プレビューが無いファイルは代替アイコンを同じ大きさで敷く。
        # icon_value=0 だとセルが潰れて、下のボタンの位置がずれる
        cell.template_icon(icon_value=item.icon_id or _placeholder_icon,
                           scale=5.0)
        if item.lock_user:
            icon = 'COLLECTION_COLOR_03' if item.lock_stale else 'LOCKED'
            text = _cell_name(item.name, 11)
        else:
            icon = 'BLANK1'
            text = _cell_name(item.name, 13)
        cell.operator("musubi.open_blend", text=text, icon=icon).rel = item.rel
    if len(wm.musubi_assets) > shown:
        rest = layout.column(align=True)
        rest.label(text=f"ほかに {len(wm.musubi_assets) - shown} 件あります"
                        "(隠れています)", icon='ERROR')
        rest.label(text="絞り込むか、右上のボタンでリスト表示に切り替えると"
                        "全部見られます", icon='BLANK1')


class MUSUBI_OT_asset_list(bpy.types.Operator):
    """アセット(カット以外の.blend)の一覧をサムネイルで開く

プロジェクト全体が対象です。開いた時点で1回だけ走査します"""
    bl_idname = "musubi.asset_list"
    bl_label = "アセット一覧"

    @classmethod
    def poll(cls, context):
        # poll はボタンを描くたびに呼ばれる。ここでファイルを読まない
        try:
            return bool(context.scene.musubi_project_root)
        except AttributeError:
            return False

    def invoke(self, context, event):
        # 走査はここで1回だけ。draw() はこの結果を読むだけにする
        refresh(context)
        return context.window_manager.invoke_popup(self, width=POPUP_WIDTH)

    def draw(self, context):
        layout = self.layout
        wm = context.window_manager

        head = layout.row(align=True)
        head.prop(wm, "musubi_assets_filter", text="", icon='VIEWZOOM')
        head.prop(wm, "musubi_assets_grid", text="",
                  icon='IMGDISPLAY' if wm.musubi_assets_grid else 'LONGDISPLAY',
                  toggle=True)
        head.label(text=wm.musubi_assets_summary)

        if not len(wm.musubi_assets):
            layout.label(text="表示するアセットがありません", icon='INFO')
            if wm.musubi_assets_filter:
                layout.label(text="(絞り込みを消すと全件に戻ります)",
                             icon='BLANK1')
            return

        if wm.musubi_assets_over_limit:
            note = layout.row(align=True)
            note.label(text=f"{THUMB_LIMIT}件を超えるため、サムネイルの"
                            "読み込みを止めています(絵は出ません)",
                       icon='ERROR')

        if wm.musubi_assets_grid:
            _draw_grid(layout, wm)
            return

        # リスト表示は template_list が自前でスクロールするので全件見られる。
        # こちらは選択(プロパティ操作なのでポップアップは閉じない)ができ、
        # 選んだ1件の詳細を下に出せる
        layout.template_list("MUSUBI_UL_assets", "", wm, "musubi_assets",
                             wm, "musubi_assets_index", rows=10)
        idx = wm.musubi_assets_index
        if 0 <= idx < len(wm.musubi_assets):
            _draw_detail(layout, wm.musubi_assets[idx])

    def execute(self, context):
        return {'FINISHED'}


# ---------------------------------------------------------------------------
# 開く(ポップアップを閉じてから確認ダイアログ)
# ---------------------------------------------------------------------------

class MUSUBI_OT_open_blend(bpy.types.Operator):
    """このファイルを開く

開くとこのファイルのロックを取得します(あなたが作業中だと全員に伝わります)。
他の人が作業中でも開けますが、その場合は警告が出ます。
いまのファイルに未保存の変更があるときは、先に保存してから開きます"""
    bl_idname = "musubi.open_blend"
    bl_label = "開く"

    # ルートからの相対パス(POSIX)。開く直前に safe_path で検証し直す
    rel: bpy.props.StringProperty(options={'HIDDEN'})

    # invoke で読み取った状態を draw に渡す(draw では I/O をしない)
    unsaved: bpy.props.BoolProperty(options={'HIDDEN'})
    lock_note: bpy.props.StringProperty(options={'HIDDEN'})
    lock_stale: bpy.props.BoolProperty(options={'HIDDEN'})

    @classmethod
    def description(cls, context, properties):
        """ボタンごとのツールチップ。

        グリッドではセルに文字がほとんど入らないので、コメント・作業中・
        使用カット数はここで見せる。**走査結果(メモリ)を読むだけ**で、
        ホバーのたびにディスクへは触らない。
        """
        rel = getattr(properties, "rel", "") or ""
        for r in _scanned:
            if r["rel"] != rel:
                continue
            lines = [rel]
            if r["comment"]:
                lines.append(f"「{r['comment']}」")
            who = f"{r['author']}" if r["author"] else "?"
            lines.append(f"最終更新: {r['age']} / {who}"
                         + (f" / {r['generations']}世代"
                            if r["generations"] else " / 履歴なし"))
            if r["cuts"]:
                lines.append(f"使用: {len(r['cuts'])}カット"
                             f"({', '.join(r['cuts'][:6])})")
            if r["lock_user"]:
                lines.append(f"作業中: {r['lock_user']}@{r['lock_host']}"
                             f"({_lock_age_text(r)})")
            lines.append("── 開くとこのファイルのロックを取得します")
            return "\n".join(lines)
        return cls.__doc__

    def invoke(self, context, event):
        """ポップアップは押した時点で閉じている。ここで確認ダイアログを出す。

        ポップアップの中から確認を重ねることはできないため、この2段構えに
        なる。キャンセルすると一覧には戻らない(もう一度開く)。
        """
        from . import sync
        self.unsaved = bool(bpy.data.is_dirty)
        self.lock_note = ""
        self.lock_stale = False
        try:
            root = core.resolve_root(context.scene.musubi_project_root)
            path = core.safe_path(root, *self.rel.split("/"))
            info = sync.read_lock(path)
        except (PipelineError, OSError):
            info = None
        if info and info.get("host") != sync.host_id():
            self.lock_note = (f"{info.get('user', '?')}@{info.get('host', '?')}"
                              " が作業中です")
            self.lock_stale = sync.is_stale_lock(info)
        return context.window_manager.invoke_props_dialog(self, width=420)

    def draw(self, context):
        col = self.layout.column(align=True)
        col.label(text=f"{self.rel.rsplit('/', 1)[-1]} を開きます",
                  icon='FILEBROWSER')
        col.label(text=self.rel, icon='BLANK1')
        note = self.layout.column(align=True)
        note.label(text="開くとこのファイルのロックを取得します",
                   icon='LOCKED')
        note.label(text="(あなたが作業中だと全員に伝わります)",
                   icon='BLANK1')
        if self.lock_note:
            warn = self.layout.column(align=True)
            warn.label(text=self.lock_note, icon='ERROR')
            if self.lock_stale:
                warn.label(text="12時間以上前からのロックです", icon='BLANK1')
            else:
                warn.label(text="開く前に本人に確認してください",
                           icon='BLANK1')
        if self.unsaved:
            self.layout.label(text="いまのファイルは保存してから開きます",
                              icon='FILE_TICK')

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
                        f"ファイルがありません(一覧を開き直してください): {rel}")
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
        self.report({'INFO'}, f"開きました: {path.name}")
        return {'FINISHED'}


def draw_asset_button(layout, context) -> None:
    """アセット一覧を開くボタン。プロジェクトパネルから呼ばれる。

    ルート選択(入口)の直後に置く。モデラー・アニメーターの実際の流れは
    「プロジェクトを決める → 作業対象のファイルを開く」で、その2つが同じ
    パネルに並ぶと導線が一本になる。
    """
    row = layout.row()
    row.scale_y = 1.3
    row.operator("musubi.asset_list", icon='PACKAGE')


CLASSES = (
    MusubiAssetItem,
    MUSUBI_UL_assets,
    MUSUBI_OT_asset_list,
    MUSUBI_OT_open_blend,
)
