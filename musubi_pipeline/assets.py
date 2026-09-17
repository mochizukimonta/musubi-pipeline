# SPDX-FileCopyrightText: 2026 mochizukimonta
# SPDX-License-Identifier: GPL-3.0-or-later
"""musubi_pipeline.assets — 開く前に状態が見える .blend の一覧(走査層)。

アセットの状態を知る手段が「1つずつ開いてバージョン管理パネルを見る」しか
なかった。進捗ボードは `.musubi/status/` を走査するので、カットではない
ファイルは1行も出ない。**必要な情報はすでに全部ディスク上にあり、.blend を
開かずに読める**ので、それを合流させて1枚のパネルに出す。

**v0.34.0 でカット側(`scan_cuts`)を足した。**アセット一覧を実機で使うと、
モデラーは「いま何が仕掛かりか・次に何を開くか」が一覧だけで分かるのに、
アニメーターは進行ボードを開いて「更新」を押し、文字1行の表から選んで
開く必要があった。同じ絵の一覧をカットにも用意して、入口の形を揃える。

切り分けは「アセットかカットか」ではなく **「開くのか、管理するのか」**。
ここが作るのは開くための一覧(読むだけ)で、状態変更・担当・指示メモ・
HTML出力は進行ボード(`tasks` / `task_ops`)の領分のまま動かさない。
カットの状態は `tasks.board()` から借りるので、**データは二重にならない**
(v0.31.0 の「カットを一覧に出すと二重管理になる」はデータの話であって、
表示の話ではない)。

このモジュールは**新しいデータを一切作らない**。読むのは既存の3種類だけ:

- `.musubi/versions/<相対パス>/*.json` — 最終更新者・日時・コメント・世代数
- `.musubi/deps/sceneXX_cYY.json`     — 逆引き(どのカットが使っているか)
- `<ファイル名>.blend.lock`            — 誰が作業中か

Blender非依存(bpyを使わない)。走査も合流も純粋なファイル操作なので、
Blenderを起動せずにテストできる。

対象ファイルの決め方(重要):
**実在する .blend を主にする。**履歴フォルダから逆算すると「履歴はあるが
実体が消えたファイル」が並び、逆に「まだ一度も保存していないアセット」が
出ない。作業者が見たいのは実在のファイルの方。

走査に `rglob("*.blend")` を使ってはいけない。`.musubi/versions/` には
世代コピーの .blend が(ファイル数×最大20個)入っており、Syncthing の
`.stversions/` にも旧世代が入る。`rglob` はフォルダを枝刈りできないので、
一覧が履歴コピーで埋まる。`sync._walk_files()` は `os.walk` ベースで
これらを枝刈りし、ルート外へ出るシンボリックリンクも辿らない。
"""

from __future__ import annotations

import json
import os
import re
import time
from pathlib import Path

from . import tasks, versions
from .core import (PipelineError, cut_name, parse_cut_path, resolve_root,
                   safe_path, scene_name)
from .sync import _walk_files, is_stale_lock, lock_age_hours, read_lock

# 依存関係の記録の置き場所。書き手は quality.record_deps(bpyが要る)だが、
# 読むだけならBlenderは不要なので、読み手であるここに定義を置く
DEPS_DIR = (".musubi", "deps")
_DEPS_FILE_RE = re.compile(r"^(scene\d{2,3})_(c\d{2,3})\.json$")


# ---------------------------------------------------------------------------
# 逆引き(このアセットはどのカットで使われているか)
# ---------------------------------------------------------------------------

def usage_index(root_str: str) -> dict[str, list[str]]:
    """アセット(ライブラリ) → 使用カット の対応表。全端末分をまとめて集計。

    `.musubi/deps/` を**1度だけ**読む。1ファイルごとに呼ぶと
    アセット数 × deps ファイル数 の読み込みになるため、呼び出し側は
    この索引を1回組んでから各行に配ること。

    キーは記録されたままの文字列(ルート相対のPOSIXパス。プロジェクト外への
    リンクは `lib.filepath` が生で入る)。突き合わせには `usage_key()` を使う。
    """
    root = resolve_root(root_str)
    ddir = root.joinpath(*DEPS_DIR)
    usage: dict[str, list[str]] = {}
    if not ddir.is_dir():
        return usage
    for f in sorted(ddir.iterdir()):
        m = _DEPS_FILE_RE.match(f.name)
        if not m:
            continue
        try:
            d = json.loads(f.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            continue
        if not isinstance(d, dict):
            continue
        cut_label = f"{m.group(1)}/{m.group(2)}"
        libs = d.get("libraries")
        if not isinstance(libs, list):
            continue
        for lib in libs:
            if isinstance(lib, str) and lib:
                usage.setdefault(lib, []).append(cut_label)
    return usage


def usage_key(path_str: str) -> str:
    """逆引きの突き合わせ用キー。純関数(ディスクを見ない)。

    区切り文字の揺れを吸収し、大文字小文字は**OSの流儀に従う**
    (`normcase` は Windows でだけ小文字化する)。Linux では
    `assets/Akane.blend` と `assets/akane.blend` は別のファイルなので、
    ここで同一視してはいけない。
    """
    return os.path.normcase(os.path.normpath(path_str))


def _by_key(usage: dict[str, list[str]]) -> dict[str, list[str]]:
    """`usage_index()` の結果を突き合わせ用キーで引けるようにする。"""
    out: dict[str, list[str]] = {}
    for lib, cuts in usage.items():
        out.setdefault(usage_key(lib), []).extend(cuts)
    return out


# ---------------------------------------------------------------------------
# 表示用の小道具(純関数)
# ---------------------------------------------------------------------------

def format_age(seconds: float) -> str:
    """経過秒を「2日前」の形にする。

    同期フォルダのタイムスタンプは他端末の時計に由来するので、未来の値が
    来ることがある。マイナスは「たった今」に丸める(嘘の未来を出さない)。
    """
    if seconds < 60:
        return "たった今"
    minutes = seconds / 60
    if minutes < 60:
        return f"{int(minutes)}分前"
    hours = minutes / 60
    if hours < 24:
        return f"{int(hours)}時間前"
    days = hours / 24
    if days < 365:
        return f"{int(days)}日前"
    return f"{int(days / 365)}年前"


def filter_rows(rows: list[dict], query: str) -> list[dict]:
    """絞り込み(空欄なら全件)。空白区切りの語を**すべて**含む行を残す。

    照合先は各行の `search`(`scan` / `scan_cuts` が組み立てる)。アセットは
    ファイル名・作者・コメント、カットはそれに加えてカット名・状態・担当・
    指示メモまで引ける。**行ごとに照合先が違う**ので、ここで組み立てずに
    行に持たせている(アセットの行に担当は無い)。

    コメントを合図に使う運用なので「リグ待ち」で引けることに意味がある。
    カット側で「リテイク」「自分の名前」が引けるのも同じ理由。

    **大文字小文字を無視するのは「人が打った文字の検索」だから**で、
    `usage_key()` のパス同一判定とは別の話。あちらは OS の流儀に従う
    必要があるが(Linux では別ファイル)、こちらは全 OS で無視してよい。
    """
    terms = (query or "").lower().split()
    if not terms:
        return list(rows)
    out = []
    for r in rows:
        hay = r.get("search")
        if not hay:  # 手組みの行(テスト等)への保険
            hay = " ".join(str(r.get(k, ""))
                           for k in ("rel", "author", "comment"))
        hay = hay.lower()
        if all(t in hay for t in terms):
            out.append(r)
    return out


def mine_rows(rows: list[dict], user: str) -> list[dict]:
    """担当が自分の行だけ残す(空欄の user なら全件)。純関数。

    突き合わせるのは**担当者名の文字列**。`tasks.update_status` はここに
    人が打った値をそのまま入れ、担当設定ダイアログの初期値が
    `getpass.getuser()` なので、既定のまま使っていれば一致する。
    表示名(「あかね」など)を手で入れている現場では一致しない —
    だからこそ UI 側は照合に使う名前を画面に出す(黙って0件にしない)。

    アセットの行には担当が無い(`assignee` を持たない)。その場合は
    絞り込みの対象外として**残さない**: 「自分の担当だけ」を押した人が
    見たいのは割り当てられた仕事で、担当の概念が無い行ではない。
    """
    me = (user or "").strip().lower()
    if not me:
        return list(rows)
    return [r for r in rows
            if (r.get("assignee") or "").strip().lower() == me]


def _text(meta: dict, key: str, limit: int = 200) -> str:
    """サイドカーの項目を表示用の文字列にする。

    同期フォルダの中身は信頼しない。数値やリストが入っていても落ちず、
    長すぎる値はここで切る(UIのラベルは折り返せない)。
    """
    value = meta.get(key)
    if value is None:
        return ""
    if not isinstance(value, str):
        value = str(value)
    return value.strip()[:limit]


def _latest_generation(vdir: Path | None) -> tuple[dict, int]:
    """履歴フォルダから (最新世代のサイドカー, 世代数) を返す。"""
    if vdir is None:
        return {}, 0
    items = versions._list_dir_versions(vdir)
    if not items:
        return {}, 0
    meta = items[0]["meta"]
    return (meta if isinstance(meta, dict) else {}), len(items)


# ---------------------------------------------------------------------------
# 走査と合流
# ---------------------------------------------------------------------------

def _file_facts(root_str: str, path: Path, now: float) -> dict:
    """実ファイル側の事実(更新時刻・最新世代・ロック)をまとめて読む。

    アセットとカットで**同じ読み方**にするためにここへ集約している。
    どちらの一覧も「誰が・いつ・何と書いて保存したか」「いま誰が開いて
    いるか」を同じ意味で出すので、読み方が分かれると表示だけがずれる。

    **まだ存在しないファイルでも落ちない。**進捗ボードには「予定」として
    ファイルより先に載るカットがあり(`tasks._discover_cuts` はステータス
    ファイルからも拾う)、その行もこの関数を通る。存在しない場合は
    mtime 0・空の事実を返し、呼び出し側が「予定」として描く。
    """
    try:
        mtime = path.stat().st_mtime
        exists = True
    except OSError:
        # 無い/走査中に消えた/同期中で開けない。mtime 0 の実ファイルと
        # 区別が付かなくなるので、「無い」は必ず exists で表す
        mtime, exists = 0.0, False
    try:
        vdir = versions.version_dir(root_str, path)
    except PipelineError:
        vdir = None
    meta, generations = _latest_generation(vdir)
    lock = read_lock(path)
    return {
        "exists": exists,
        "mtime": mtime,
        "age": format_age(now - mtime) if exists else "",
        "author": _text(meta, "author", 40),
        "comment": _text(meta, "comment"),
        "saved_at": _text(meta, "saved_at", 40),
        "generations": generations,
        "lock_user": _text(lock or {}, "user", 40),
        "lock_host": _text(lock or {}, "host", 40),
        "lock_age_h": lock_age_hours(lock) if lock else 0.0,
        "lock_stale": is_stale_lock(lock),
    }


def scan(root_str: str, now: float | None = None) -> list[dict]:
    """アセット1件=1行の一覧を、更新の新しい順に返す。

    各行:
      kind         "asset" 固定(カットの行と混ぜても見分けられるように)
      rel          ルートからの相対パス(POSIX)
      name         ファイル名
      path         実体の Path
      exists       True(実在する .blend だけを並べるため)
      mtime / age  実ファイルの最終更新(と「2日前」表記)
      author / comment / saved_at / generations
                   最新世代のサイドカーの内容(履歴が無ければ空・0)
      lock_user / lock_host / lock_age_h / lock_stale
                   `.blend.lock` の内容(無ければ空・0.0・False)
      cuts         このアセットを使っているカット(逆引き)
      search       絞り込みの照合先(`filter_rows`)

    **日時は実ファイルの mtime、作者とコメントは最新世代のサイドカー**という
    別々の出所を1行に並べている。無記入保存は10分に1回しか世代を作らないので
    (`ops._snapshot_bg`)、両者は一致しないことがある。実ファイルの方が必ず
    新しいので、並べ替えと「いつ」には mtime を使う。
    """
    root = resolve_root(root_str)
    now = time.time() if now is None else now
    usage = _by_key(usage_index(root_str))

    rows = []
    for rel, path in _walk_files(root):
        if not rel.lower().endswith(".blend"):
            continue
        # カットは進行ボードの領分(状態・担当・指示メモを持つ)。
        # 開くための一覧は scan_cuts が別に組む
        if parse_cut_path(root, path) is not None:
            continue
        facts = _file_facts(root_str, path, now)
        if not facts["exists"]:
            continue  # 走査中に消えた/同期中で開けない
        rows.append({
            "kind": "asset",
            "rel": rel,
            "name": rel.rsplit("/", 1)[-1],
            "path": path,
            **facts,
            "cuts": usage.get(usage_key(rel), []),
            "search": f"{rel} {facts['author']} {facts['comment']}",
        })
    rows.sort(key=lambda r: (-r["mtime"], r["rel"]))
    return rows


# ---------------------------------------------------------------------------
# カット(scenes/sceneXX/cYY.blend)の一覧
# ---------------------------------------------------------------------------

# 並び順。**「何から始めればいいか」の答えをそのまま並び順にする。**
# 進行ボード(`tasks.board`)はシーン/カット順 = 制作進行が全体を見るための
# 並びで、こちらは作業者が自分の次の一手を探すための並び。差し戻された
# もの(リテイク)、手を付けたもの(作業中)、これからのもの(未着手)の順。
# 出したあと(レビュー待ち)と終わったもの(承認済み・オミット)は下。
CUT_PRIORITY = ("retake", "wip", "todo", "review", "approved", "omit")


def cut_rel(scene_no: int, cut_no: int) -> str:
    """カットのルート相対パス(POSIX)。純関数。

    `musubi.open_blend` に渡す形。進行ボード(`task_ops.refresh_board`)も
    同じ形で渡すので、組み立ては1か所にまとめてある。範囲外の番号では
    `scene_name` / `cut_name` が PipelineError を投げる。
    """
    return f"scenes/{scene_name(scene_no)}/{cut_name(cut_no)}.blend"


def scan_cuts(root_str: str, now: float | None = None) -> list[dict]:
    """カット1件=1行の一覧を、作業者が次に開く順で返す。

    状態(ステータス・担当・指示メモ・最新出力・予定かどうか)は
    **`tasks.board()` から借りる**。ここでステータスを読み直したり別の
    索引を作ったりはしない — 進行ボードと同じ1つのファイルが出所で、
    表示だけを作業者向けに組み替える。

    そこへアセット一覧と同じ「ファイル側の事実」(最終更新・最新世代の
    作者とコメント・ロック)を `_file_facts` で合流させる。ロックは
    `tasks.board()` も読んでいるが、あちらは担当者名しか返さない
    (経過時間と放置判定が要る)。カット1件につき小さな stat が1回
    増えるだけなので、`tasks.board()` の戻り値の形は変えない。

    各行は `scan()` と同じキーに加えて:
      kind             "cut" 固定
      scene / cut      番号
      label            "s01/c01"
      status / status_label / assignee / note
      updated_at / updated_by   ステータスを最後に触った人と時刻
      latest_output    output/ にある最新版の番号(無ければ0)

    **.blend がまだ無いカットも返す**(`exists` が False)。ボード上で
    「予定」として先に立っているもので、担当だけ決まってまだ誰も作って
    いないカットは、アニメーターが最初に知りたいもののひとつ。
    開けないことは呼び出し側が `exists` を見て示す。
    """
    root = resolve_root(root_str)
    now = time.time() if now is None else now

    rows = []
    for st in tasks.board(root_str):
        s_no, c_no = st["scene"], st["cut"]
        try:
            rel = cut_rel(s_no, c_no)
            path = safe_path(root, "scenes", scene_name(s_no),
                             f"{cut_name(c_no)}.blend")
        except PipelineError:
            # 範囲外の番号。tasks._discover_cuts が弾いているはずだが、
            # この関数だけを呼んでも落ちないようにここでも受ける
            continue
        facts = _file_facts(root_str, path, now)
        status = st["status"] if st["status"] in tasks.STATUSES else "todo"
        label = f"s{s_no:02d}/c{c_no:02d}"
        status_label = tasks.STATUSES[status]
        rows.append({
            "kind": "cut",
            "rel": rel,
            "name": f"{cut_name(c_no)}.blend",
            "path": path,
            **facts,
            "cuts": [],
            "scene": s_no,
            "cut": c_no,
            "label": label,
            "status": status,
            "status_label": status_label,
            "assignee": st["assignee"],
            "note": st["note"],
            "updated_at": st["updated_at"],
            "updated_by": st["updated_by"],
            "latest_output": st["latest_output"],
            "search": (f"{label} {rel} {status_label} {st['assignee']} "
                       f"{st['note']} {facts['author']} {facts['comment']}"),
        })
    rows.sort(key=lambda r: (CUT_PRIORITY.index(r["status"]),
                             r["scene"], r["cut"]))
    return rows
