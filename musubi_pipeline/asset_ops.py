# SPDX-FileCopyrightText: 2026 mochizukimonta
# SPDX-License-Identifier: GPL-3.0-or-later
"""musubi_pipeline.asset_ops — 開くファイルの一覧(ポップアップ)と開く導線。

走査は `assets.py`(bpy非依存)。ここは Blender 側の器で、
**ディスクI/Oはオペレーターの `invoke()` / `execute()` とプロパティの
`update` コールバックの中だけ**で行う(`draw()` / `poll()` からは読まない。
ARCHITECTURE の「UI層でI/Oをしない」。update は「押した時」に1回しか
走らないので、再描画のたびに走る draw とは性質が違う)。

**v0.32 でサイドバーの常設パネルからポップアップへ移した。**
サイドバーの他のパネルはすべて `context.scene` に紐づく「いま開いている
ファイル」の表示だが、この一覧だけは対象がプロジェクト全体で、参照スコープ
が1階層違う。同じ場所に同じ見た目で並ぶと情報の階層が混ざる。呼び出して
使う別の場所に移すことで、この違いを見た目に出す。

**v0.34 でカットも同じ一覧で開けるようにした。**アセット一覧を実機で使うと
モデラーは一覧だけで「仕掛かり」と「次の一手」が分かるのに、アニメーターは
進行ボードを開いて「更新」を押し、文字1行の表から選んで開く必要があった。
同じ器に「アセット / カット」の2つの表示を持たせて、入口の形を揃える。

**この一覧は「開くためのもの」、進行ボードは「管理するためのもの」。**
状態変更・担当・指示メモ・HTML出力・リールは進行ボードの領分で、こちらは
一切書き込まない(読むだけ)。カットの状態は `assets.scan_cuts` 経由で
`tasks.board()` から借りるので、データの出所は1つのまま。

オペレーターIDが `musubi.asset_list` のままなのは、2版前から配布されている
IDを見た目の都合で変えないため(機能は増えたが、開く一覧であることは同じ)。

一覧は既定でサムネイルのグリッド。絵で探せることが目的だが、グリッドの
セルには文字がほとんど入らないので、更新時刻・作者・コメント(カットなら
状態・担当・指示メモ)を追いたいときのためにリスト表示へ切り替えられる。

「開く」をここに置く目的は利便性ではなく、**開く前にロック状態が見えること**。
v0.19.0 以降、他人が作業中のファイルは開いた瞬間に警告されるが、その時点で
すでにロックは取得済みで、相手には「誰かが開いた」が伝わっている。一覧から
開けば、開く前に「作業中」を見て引き返せる。
"""

from __future__ import annotations

import getpass
from pathlib import Path

import bpy

from . import assets, core
from .core import PipelineError
from .task_ops import STATUS_ICONS

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

# 最後に走査に成功したルート。表示の切り替え(アセット⇄カット)は
# ポップアップの中の update コールバックから走査し直すので、そこで
# context.scene が期待どおり取れなかった場合の拠り所にする
_root = ""

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
    global _pcoll, _lazy, _over_limit, _root
    _root = ""
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
    """一覧の1行。**アセットとカットで同じ器を使う。**

    表示は一度に片方だけなので、コレクションを2つ持つ必要がない
    (サムネイルの読み込み・遅延解放・選択の仕組みも1組で済む)。
    どちらの行かは `kind` で分かる。カット専用の項目はアセットのときは
    既定値のまま(空文字・0・False)になる。
    """
    # name(PropertyGroup既定のプロパティ)は UIList 標準の絞り込み・
    # 並べ替えが見る項目。ファイル名を入れておく
    kind: bpy.props.StringProperty(default="asset")
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
    # 実体があるか。False はボード上の「予定」(ステータスだけ先にある
    # カット)で、開けない。グリッドではボタンを無効にして形だけ残す
    exists: bpy.props.BoolProperty(default=True)
    # --- カットのときだけ入る ---
    cut_label: bpy.props.StringProperty()      # "s01/c01"
    status_label: bpy.props.StringProperty()   # "リテイク"
    status_icon: bpy.props.StringProperty(default='NONE')
    assignee: bpy.props.StringProperty()
    note: bpy.props.StringProperty()
    latest_output: bpy.props.IntProperty(default=0)
    status_updated: bpy.props.StringProperty()
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
        elif item.kind == "cut" and not item.exists:
            # まだ無いファイル。サムネイルの代わりに「予定」だと分かる絵
            row.label(text=item.label, icon='DOT')
        else:
            row.label(text=item.label, icon='FILE_BLEND')
        right = row.row(align=True)
        right.alignment = 'RIGHT'
        if item.kind == "cut" and item.status_icon != 'NONE':
            # 状態は行の文字にも入っているが、絵があると縦に流し読みできる
            right.label(text="", icon=item.status_icon)
        if item.lock_user:
            # 赤は CRITICAL のみ。放置ロックは黄アイコン + 通常文字
            right.label(text=f"作業中:{item.lock_user}",
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
    if r["kind"] == "cut":
        return _cut_row_label(r)
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


def _cut_row_label(r: dict) -> str:
    """カット1行。**先頭はカット名**(`c01.blend` はシーンをまたぐと重なる)。

    並びは「何を・どういう状態で・誰が・何を言われているか」。指示メモは
    リテイクの中身なので、コメント(保存時のメモ)より先に出す。
    """
    parts = [r["label"], r["status_label"]]
    if r["assignee"]:
        parts.append(f"担当:{r['assignee']}")
    if not r["exists"]:
        parts.append("(予定・未作成)")
    else:
        parts.append(r["age"])
    if r["note"]:
        parts.append(f"指示「{r['note'][:20]}」")
    elif r["comment"]:
        parts.append(f"「{r['comment'][:20]}」")
    if r["latest_output"]:
        parts.append(f"出力v{r['latest_output']:03d}")
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


def _summary(wm, rows: list[dict], shown: list[dict]) -> str:
    """一覧の上に出す一行。**絞り込みで何件が隠れているかを必ず出す。**"""
    total = f"{len(shown)}件" if len(shown) == len(rows) \
        else f"{len(shown)}/{len(rows)}件"
    if wm.musubi_assets_mode == 'CUT':
        # 作業者が次の一手を探す画面なので、残っている仕事の数を出す
        # (承認済みの数は進行ボードの集計が出している)
        counts = []
        for status, label in (("retake", "リテイク"), ("wip", "作業中"),
                              ("todo", "未着手")):
            n = sum(1 for r in shown if r["status"] == status)
            if n:
                counts.append(f"{label}{n}")
        return total + (" / " + " ".join(counts) if counts else "")
    locked = sum(1 for r in shown if r["lock_user"])
    return total + (f" / 作業中 {locked}件" if locked else "")


def _populate(wm) -> None:
    """走査結果から表示用の一覧を組み直す。**ディスクには触れない。**

    絞り込み欄を打つたびに呼ばれるので、ここでファイルを読んではいけない
    (プレビューは走査時に読み終えていて、`_icons` から配るだけ)。
    """
    _release_lazy(wm)
    wm.musubi_assets.clear()
    rows = assets.filter_rows(_scanned, wm.musubi_assets_filter)
    if wm.musubi_assets_mode == 'CUT' and wm.musubi_assets_mine:
        # 担当はカットにしかない。アセット側で効かせると全部消える
        rows = assets.mine_rows(rows, wm.musubi_assets_user)
    for r in rows:
        item = wm.musubi_assets.add()
        item.kind = r["kind"]
        item.name = r["name"]
        item.rel = r["rel"]
        item.abs_path = str(r["path"])
        item.label = _row_label(r)
        item.age = r["age"]
        item.author = r["author"]
        item.comment = r["comment"]
        item.saved_at = r["saved_at"]
        item.generations = r["generations"]
        item.exists = r["exists"]
        item.lock_user = r["lock_user"]
        item.lock_host = r["lock_host"]
        item.lock_stale = r["lock_stale"]
        item.lock_age = _lock_age_text(r)
        item.cuts = ", ".join(r["cuts"][:40])
        item.cut_count = len(r["cuts"])
        if r["kind"] == "cut":
            item.cut_label = r["label"]
            item.status_label = r["status_label"]
            item.status_icon = STATUS_ICONS.get(r["status"], 'NONE')
            item.assignee = r["assignee"]
            item.note = r["note"]
            item.latest_output = r["latest_output"]
            item.status_updated = r["updated_at"][:16].replace("T", " ")
        item.icon_id = _icons.get(r["rel"], 0)
    wm.musubi_assets_summary = _summary(wm, _scanned, rows)
    wm.musubi_assets_index = min(wm.musubi_assets_index,
                                 max(0, len(wm.musubi_assets) - 1))
    _load_selected(wm)


def on_filter_update(self, context):
    """絞り込み欄が変わったときのフック(self は WindowManager)。"""
    _populate(self)


def on_index_update(self, context):
    """選択が変わったときのフック(self は WindowManager)。"""
    _load_selected(self)


def on_mode_update(self, context):
    """アセット⇄カットの切り替え(self は WindowManager)。

    **ここだけは走査し直す**(見せる対象そのものが変わり、サムネイルも
    別のファイル群になる)。押した時に1回走るだけなので、draw からの
    I/O 禁止には触れない。ポップアップは開いたまま中身が入れ替わる。
    """
    refresh(context)


def on_mine_update(self, context):
    """「自分の担当だけ」の切り替え(self は WindowManager)。I/Oなし。"""
    _populate(self)


def _me() -> str:
    """このPCのログイン名。担当者名の突き合わせに使う。

    担当設定ダイアログの初期値と同じ出所(`getpass.getuser()`)にする。
    取れない環境でも一覧が出せなくなってはいけないので、失敗は空文字。
    """
    try:
        return getpass.getuser()
    except Exception:
        return ""


def _root_of(context) -> str:
    """走査対象のルート。

    表示の切り替えはポップアップの中の update コールバックから走るので、
    そこで `context.scene` が期待どおり取れなかった場合に備えて、
    最後に走査したルートを控えておく。
    """
    try:
        root = context.scene.musubi_project_root
    except (AttributeError, TypeError):
        root = ""
    return root or _root


def refresh(context) -> None:
    """走査してプレビューを読み、一覧を組み立てる。ディスクを読むのはここだけ。"""
    global _over_limit, _root
    wm = context.window_manager
    _release_lazy(wm)
    wm.musubi_assets.clear()
    wm.musubi_assets_over_limit = False
    wm.musubi_assets_user = _me()
    _over_limit = False
    _scanned.clear()
    _icons.clear()
    if _pcoll is not None:
        # 前回のサムネイルはここで必ず捨てる(clear と load を対で回す)
        _pcoll.clear()
    root = _root_of(context)
    cuts = wm.musubi_assets_mode == 'CUT'
    try:
        rows = assets.scan_cuts(root) if cuts else assets.scan(root)
    except PipelineError as e:
        wm.musubi_assets_summary = str(e)
        return
    _root = root
    _scanned.extend(rows)
    _over_limit = len(rows) > THUMB_LIMIT
    if not _over_limit:
        for i, r in enumerate(rows):
            if not r["exists"]:
                continue  # まだ無いカット(予定)。読むファイルがない
            # 同名ファイルが別フォルダにあってもキーが衝突しないよう番号を足す
            _icons[r["rel"]] = _load_preview(f"{i}:{r['rel']}", r["path"])
    wm.musubi_assets_over_limit = _over_limit
    _populate(wm)


# ---------------------------------------------------------------------------
# 一覧のポップアップ
# ---------------------------------------------------------------------------

def _draw_open_button(layout, item, text: str, icon: str = 'FILEBROWSER'):
    """「開く」ボタン。**まだ無いカットでは押せないが、場所は残す。**

    行やセルから消すと、一覧の並びが行ごとにずれて読みにくくなる。
    押せない理由(未作成)は呼び出し側が文字で出す。
    """
    row = layout.row(align=True)
    row.enabled = bool(item.exists)
    row.operator("musubi.open_blend", text=text, icon=icon).rel = item.rel


def _draw_cut_detail(box, item) -> None:
    """カットの詳細。**状態・担当・指示メモは読むだけ**(変更は進行ボード)。"""
    col = box.column(align=True)
    col.label(text=f"{item.cut_label}  ({item.name})", icon='SEQUENCE')
    col.label(text=item.rel)
    line = box.row(align=True)
    line.label(text=item.status_label or "未着手",
               icon=item.status_icon if item.status_icon != 'NONE'
               else 'RADIOBUT_OFF')
    line.label(text=f"担当: {item.assignee or '─'}", icon='USER')
    if item.latest_output:
        line.label(text=f"最新出力: v{item.latest_output:03d}",
                   icon='RENDER_ANIMATION')
    if item.note:
        # リテイク指示はこの画面でいちばん読ませたい文字。枠で独立させる
        note = box.box()
        note.label(text=f"指示: {item.note}", icon='TEXT')
    if not item.exists:
        miss = box.column(align=True)
        miss.label(text="このカットの .blend はまだありません", icon='INFO')
        miss.label(text="「カットとして配置保存」で作られます", icon='BLANK1')
    else:
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
    if item.status_updated:
        box.label(text=f"状態の更新: {item.status_updated}", icon='PRESET')
    box.label(text="状態・担当・指示の変更は「進行ボード」から",
              icon='INFO')


def _draw_detail(layout, item):
    """選択した1件の詳細。グリッドのセルには入らない情報をここに集める。"""
    box = layout.box()
    if item.kind == "cut":
        _draw_cut_detail(box, item)
    else:
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
    if item.kind != "cut":
        if item.cut_count:
            box.label(text=f"使用: {item.cut_count}カット — {item.cuts}",
                      icon='LINKED')
        else:
            box.label(text="使用カットの記録なし", icon='UNLINKED')
    big = box.column()
    big.scale_y = 1.4
    label = item.cut_label if item.kind == "cut" else item.name
    _draw_open_button(big, item, f"{label} を開く")


def _draw_grid(layout, wm) -> None:
    """サムネイルのグリッド。**セルのボタンがそのまま「開く」**。

    ポップアップの中ではオペレーターを押した時点でポップアップが閉じるため、
    「選んでから開く」の2段にはできない(選択はプロパティ操作なので閉じない
    が、大きいサムネイルを押せるボタンにする方法が無い —
    `operator(icon_value=…)` はセルを拡大してもアイコンが1単位のまま)。
    そこで**サムネイルの下のボタンが名前ラベル兼「開く」**になっている。
    そのファイルの詳細(コメント・ロック・使用カット)はボタンの
    ツールチップに出る(`MUSUBI_OT_open_blend.description`)。

    カットのセルは3段(絵 / 状態アイコン付きの「開く」/ 担当・作業中)。
    **段数は行ごとに変えない** — `even_rows` でそろえた高さが崩れる。
    """
    cuts = wm.musubi_assets_mode == 'CUT'
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
        if cuts:
            _draw_cut_cell(cell, item)
            continue
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


def _draw_cut_cell(cell, item) -> None:
    """カット1セル。ボタンは状態アイコン + カット名、下の1行は人の情報。

    セルの文字数は10文字強しか入らない。カット名(`s01/c01`)だけで
    7文字使うので、状態はアイコンに、担当と「作業中」は下の行に回す。
    """
    icon = item.status_icon if item.status_icon != 'NONE' else 'BLANK1'
    _draw_open_button(cell, item, item.cut_label, icon)
    if item.lock_user:
        cell.label(text=_cell_name(item.lock_user, 10),
                   icon='COLLECTION_COLOR_03' if item.lock_stale
                   else 'LOCKED')
    elif not item.exists:
        cell.label(text="予定", icon='DOT')
    elif item.assignee:
        cell.label(text=_cell_name(item.assignee, 10), icon='USER')
    else:
        cell.label(text="担当なし", icon='BLANK1')


class MUSUBI_OT_asset_list(bpy.types.Operator):
    """開くファイルの一覧をサムネイルで開く(アセット / カット)

プロジェクト全体が対象です。開いた時点で1回だけ走査します"""
    bl_idname = "musubi.asset_list"
    bl_label = "ファイル一覧"

    # 押したボタンに応じて最初に出す表示。空なら前回のまま
    # (サイドバーの「アセット一覧」「カット一覧」が入口ごとに指定する)
    mode: bpy.props.StringProperty(default="", options={'HIDDEN', 'SKIP_SAVE'})

    @classmethod
    def poll(cls, context):
        # poll はボタンを描くたびに呼ばれる。ここでファイルを読まない
        try:
            return bool(context.scene.musubi_project_root)
        except AttributeError:
            return False

    def invoke(self, context, event):
        wm = context.window_manager
        if self.mode in ('ASSET', 'CUT'):
            # update コールバック(= refresh)が走るので、ここで代入して
            # から改めて走査すると2度読みになる。代入だけで走査は済む
            if wm.musubi_assets_mode != self.mode:
                wm.musubi_assets_mode = self.mode
            else:
                refresh(context)
        else:
            # 走査はここで1回だけ。draw() はこの結果を読むだけにする
            refresh(context)
        return wm.invoke_popup(self, width=POPUP_WIDTH)

    def draw(self, context):
        layout = self.layout
        wm = context.window_manager
        cuts = wm.musubi_assets_mode == 'CUT'

        # 表示の切り替えは最上段。**何の一覧を見ているか**が、この画面で
        # いちばん先に分かるべきこと(アセットとカットは別の仕事の入口)
        tabs = layout.row(align=True)
        tabs.scale_y = 1.2
        tabs.prop(wm, "musubi_assets_mode", expand=True)

        head = layout.row(align=True)
        head.prop(wm, "musubi_assets_filter", text="", icon='VIEWZOOM')
        head.prop(wm, "musubi_assets_grid", text="",
                  icon='IMGDISPLAY' if wm.musubi_assets_grid else 'LONGDISPLAY',
                  toggle=True)
        if cuts:
            # 照合するのはログイン名。表示名を手で入れている現場では
            # 一致しないので、**使う名前を画面に出す**(黙って0件にしない)
            me = wm.musubi_assets_user or "?"
            head.prop(wm, "musubi_assets_mine", text=f"自分の担当だけ({me})",
                      icon='USER', toggle=True)
        head.label(text=wm.musubi_assets_summary)

        if not len(wm.musubi_assets):
            what = "カット" if cuts else "アセット"
            layout.label(text=f"表示する{what}がありません", icon='INFO')
            if wm.musubi_assets_filter:
                layout.label(text="(絞り込みを消すと全件に戻ります)",
                             icon='BLANK1')
            if cuts and wm.musubi_assets_mine:
                layout.label(text=f"(「自分の担当だけ」を切ると全件に戻ります。"
                                  f"担当者名が {wm.musubi_assets_user} と"
                                  f"一致する行だけを出しています)",
                             icon='BLANK1')
            elif cuts:
                layout.label(text="(カットは「カットとして配置保存」または"
                                  "ボードの「カットを予定に追加」で増えます)",
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

def _tooltip(r: dict) -> str:
    """1件のツールチップ本文。**純関数**(走査結果の dict を読むだけ)。

    グリッドではここが唯一の詳細表示なので、一覧の行に入り切らなかった
    ものを全部載せる。カットは「状態 → 指示 → 担当」の順 — リテイクで
    戻ってきたカットは、指示がいちばん先に読まれるべき文字。
    """
    if r["kind"] == "cut":
        lines = [f"{r['label']}  {r['status_label']}"]
        if r["note"]:
            lines.append(f"指示「{r['note']}」")
        lines.append(f"担当: {r['assignee'] or '─'}")
        if not r["exists"]:
            lines.append("このカットの .blend はまだありません(予定)")
            return "\n".join(lines)
        if r["latest_output"]:
            lines.append(f"最新出力: v{r['latest_output']:03d}")
    else:
        lines = [r["rel"]]
        if r["comment"]:
            lines.append(f"「{r['comment']}」")
    who = r["author"] or "?"
    lines.append(f"最終更新: {r['age']} / {who}"
                 + (f" / {r['generations']}世代"
                    if r["generations"] else " / 履歴なし"))
    if r["kind"] == "cut":
        if r["comment"]:
            lines.append(f"保存時のメモ「{r['comment']}」")
    elif r["cuts"]:
        lines.append(f"使用: {len(r['cuts'])}カット"
                     f"({', '.join(r['cuts'][:6])})")
    if r["lock_user"]:
        lines.append(f"作業中: {r['lock_user']}@{r['lock_host']}"
                     f"({_lock_age_text(r)})")
    lines.append("── 開くとこのファイルのロックを取得します")
    return "\n".join(lines)


def _open_note(rel: str) -> str:
    """確認ダイアログに出す1行。走査結果を読むだけ(ディスクに触れない)。

    グリッドから押した場合はツールチップを読まずに来ることもあるので、
    「いま何を開こうとしているか」をここでもう一度だけ出す。
    """
    for r in _scanned:
        if r["rel"] != rel:
            continue
        if r["kind"] == "cut":
            parts = [r["status_label"]]
            if r["assignee"]:
                parts.append(f"担当: {r['assignee']}")
            if r["note"]:
                parts.append(f"指示「{r['note'][:40]}」")
            return "  ".join(parts)
        return f"「{r['comment'][:60]}」" if r["comment"] else ""
    return ""


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
    # 何を開こうとしているかの1行(カットなら状態と担当、アセットなら
    # 最後のコメント)。グリッドから押した場合、ここが最後の確認になる
    context_note: bpy.props.StringProperty(options={'HIDDEN'})

    @classmethod
    def description(cls, context, properties):
        """ボタンごとのツールチップ。

        グリッドではセルに文字がほとんど入らないので、コメント・作業中・
        使用カット数(カットなら状態・担当・指示メモ)はここで見せる。
        **走査結果(メモリ)を読むだけ**で、ホバーのたびにディスクへは
        触らない。
        """
        rel = getattr(properties, "rel", "") or ""
        for r in _scanned:
            if r["rel"] != rel:
                continue
            return _tooltip(r)
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
        self.context_note = _open_note(self.rel)
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
        if self.context_note:
            col.label(text=self.context_note, icon='BLANK1')
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


def draw_asset_button(layout, context, cuts: bool = False) -> None:
    """一覧を開くボタン。プロジェクトパネルから呼ばれる。

    ルート選択(入口)の直後に置く。モデラー・アニメーターの実際の流れは
    「プロジェクトを決める → 作業対象のファイルを開く」で、その2つが同じ
    パネルに並ぶと導線が一本になる。

    **映像構成のときはカット側のボタンも並べる。**タブの中に隠すと、
    アニメーターは「アセット一覧」という名前のボタンを自分と無関係だと
    判断して押さない(v0.33 までの実機確認で実際にそうなっていた)。
    入口はそれぞれの名前で見えている必要がある。
    """
    row = layout.row(align=True)
    row.scale_y = 1.3
    op = row.operator("musubi.asset_list", text="アセット一覧", icon='PACKAGE')
    op.mode = 'ASSET'
    if cuts:
        op = row.operator("musubi.asset_list", text="カット一覧",
                          icon='SEQUENCE')
        op.mode = 'CUT'


CLASSES = (
    MusubiAssetItem,
    MUSUBI_UL_assets,
    MUSUBI_OT_asset_list,
    MUSUBI_OT_open_blend,
)
