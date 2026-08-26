# SPDX-FileCopyrightText: 2026 mochizukimonta
# SPDX-License-Identifier: GPL-3.0-or-later
"""musubi_pipeline.assets — アセット(カット以外の .blend)の一覧。

アセットの状態を知る手段が「1つずつ開いてバージョン管理パネルを見る」しか
なかった。進捗ボードは `.musubi/status/` を走査するので、カットではない
ファイルは1行も出ない。**必要な情報はすでに全部ディスク上にあり、.blend を
開かずに読める**ので、それを合流させて1枚のパネルに出す。

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

from . import versions
from .core import PipelineError, parse_cut_path, resolve_root
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

    照合先はファイル名(相対パス)・作者・コメント。コメントを合図に使う
    運用なので、「リグ待ち」で引けることに意味がある。

    **大文字小文字を無視するのは「人が打った文字の検索」だから**で、
    `usage_key()` のパス同一判定とは別の話。あちらは OS の流儀に従う
    必要があるが(Linux では別ファイル)、こちらは全 OS で無視してよい。
    """
    terms = (query or "").lower().split()
    if not terms:
        return list(rows)
    out = []
    for r in rows:
        hay = f"{r['rel']} {r['author']} {r['comment']}".lower()
        if all(t in hay for t in terms):
            out.append(r)
    return out


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

def scan(root_str: str, now: float | None = None) -> list[dict]:
    """アセット1件=1行の一覧を、更新の新しい順に返す。

    各行:
      rel          ルートからの相対パス(POSIX)
      name         ファイル名
      path         実体の Path
      mtime / age  実ファイルの最終更新(と「2日前」表記)
      author / comment / saved_at / generations
                   最新世代のサイドカーの内容(履歴が無ければ空・0)
      lock_user / lock_host / lock_age_h / lock_stale
                   `.blend.lock` の内容(無ければ空・0.0・False)
      cuts         このアセットを使っているカット(逆引き)

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
        # カットは進捗ボードの領分。ここに出すと二重管理になる
        if parse_cut_path(root, path) is not None:
            continue
        try:
            mtime = path.stat().st_mtime
        except OSError:
            continue  # 走査中に消えた/同期中で開けない
        try:
            vdir = versions.version_dir(root_str, path)
        except PipelineError:
            vdir = None
        meta, generations = _latest_generation(vdir)
        lock = read_lock(path)
        rows.append({
            "rel": rel,
            "name": rel.rsplit("/", 1)[-1],
            "path": path,
            "mtime": mtime,
            "age": format_age(now - mtime),
            "author": _text(meta, "author", 40),
            "comment": _text(meta, "comment"),
            "saved_at": _text(meta, "saved_at", 40),
            "generations": generations,
            "lock_user": _text(lock or {}, "user", 40),
            "lock_host": _text(lock or {}, "host", 40),
            "lock_age_h": lock_age_hours(lock) if lock else 0.0,
            "lock_stale": is_stale_lock(lock),
            "cuts": usage.get(usage_key(rel), []),
        })
    rows.sort(key=lambda r: (-r["mtime"], r["rel"]))
    return rows
