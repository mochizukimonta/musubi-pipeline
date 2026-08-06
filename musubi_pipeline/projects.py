# SPDX-FileCopyrightText: 2026 mochizukimonta
# SPDX-License-Identifier: GPL-3.0-or-later
"""musubi_pipeline.projects — この端末が開いたプロジェクトの履歴。

ルートの指定はこのアドオンの入口で、設定しなければどの機能も動かない。
そこで「前に開いたプロジェクト」を端末側に覚えておき、パスを手入力せずに
選び直せるようにする。

置き場所は `core.local_root()`(同期対象外のローカル領域)。理由は2つ:

1. **私のPCのパスは私だけの事実**。プロジェクトフォルダの中に置くと
   チーム全員に配られてしまい、他人のドライブレターが混ざる
2. **アドオンを入れ直しても消えない**。Blender のアドオン設定
   (`userpref.blend`)はアンインストールで失われる

Blender非依存(bpyを使わない)。表示用の合流・並べ替えは純関数なので、
Blenderを起動せずにテストできる。
"""

from __future__ import annotations

import json
import os
import time
from pathlib import Path

from .core import atomic_replace, local_root

# 履歴の上限。多すぎるとメニューが選べなくなるだけで利点がない
MAX_ENTRIES = 20

# 同じルートを続けて開いた時に書き込みを繰り返さない猶予(秒)。
# ファイルを開くと全シーン分の update コールバックが走るため
_TOUCH_INTERVAL = 60.0


def store_path() -> Path:
    return local_root() / "projects.json"


def display_name(path_str: str) -> str:
    """メニューに出す名前。フォルダ名をそのまま使う。

    プロジェクトに別途の名前を持たせない。フォルダに分かる名前を付けるのは
    利用者がすでにやっていることで、入力を増やす理由がない。
    """
    name = Path(path_str).name
    return name or path_str


def dedup_key(path_str: str) -> str:
    """重複判定用のキー。純関数(ディスクを見ない)。

    実体解決(realpath)は I/O なので、履歴に**書く時だけ**行う。
    ここは大文字小文字と区切り文字の揺れだけを吸収する。
    """
    return os.path.normcase(os.path.normpath(path_str))


# ---------------------------------------------------------------------------
# 読み書き
# ---------------------------------------------------------------------------

def load(store: Path | None = None) -> list[dict]:
    """履歴を新しい順で返す。壊れていれば空として扱う(消さない)。"""
    p = store or store_path()
    try:
        data = json.loads(p.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return []
    if not isinstance(data, dict) or not isinstance(data.get("projects"), list):
        return []
    out = []
    for e in data["projects"]:
        if not isinstance(e, dict):
            continue
        path = e.get("path")
        if not isinstance(path, str) or not path:
            continue
        try:
            last = float(e.get("last_used", 0))
        except (TypeError, ValueError):
            last = 0.0
        out.append({"path": path,
                    "name": display_name(path),
                    "last_used": last})
    out.sort(key=lambda e: e["last_used"], reverse=True)
    return out[:MAX_ENTRIES]


def save(entries: list[dict], store: Path | None = None) -> None:
    p = store or store_path()
    p.parent.mkdir(parents=True, exist_ok=True)
    payload = {"projects": [{"path": e["path"], "last_used": e["last_used"]}
                            for e in entries[:MAX_ENTRIES]]}
    tmp = p.with_name(p.name + ".tmp")
    tmp.write_text(json.dumps(payload, ensure_ascii=False, indent=1),
                   encoding="utf-8")
    atomic_replace(tmp, p)


def remember(root_str: str, store: Path | None = None,
             now: float | None = None) -> list[dict]:
    """開いたプロジェクトを履歴の先頭に置く。

    実在するフォルダだけを覚える。存在しないパスを覚えると、次に開いた時に
    必ず失敗する項目がメニューに並ぶことになる。
    """
    if not root_str or not root_str.strip():
        return load(store)
    try:
        real = os.path.realpath(str(Path(root_str).expanduser()))
    except OSError:
        return load(store)
    if not Path(real).is_dir():
        return load(store)

    ts = time.time() if now is None else now
    entries = load(store)
    key = dedup_key(real)
    # 直前に同じルートを覚えたばかりなら書かない(開くたびの連打を吸収)
    if entries and dedup_key(entries[0]["path"]) == key \
            and ts - entries[0]["last_used"] < _TOUCH_INTERVAL:
        return entries
    rest = [e for e in entries if dedup_key(e["path"]) != key]
    entries = [{"path": real, "name": display_name(real),
                "last_used": ts}] + rest
    entries = entries[:MAX_ENTRIES]
    save(entries, store)
    return entries


def forget(path_str: str, store: Path | None = None) -> list[dict]:
    """履歴から1件消す。プロジェクトフォルダ自体には触らない。"""
    key = dedup_key(path_str)
    entries = [e for e in load(store) if dedup_key(e["path"]) != key]
    save(entries, store)
    return entries


def prune_missing(store: Path | None = None) -> list[dict]:
    """実体が無くなったものを履歴から落とす(I/Oあり・ボタン操作から呼ぶ)。"""
    entries = load(store)
    alive = [e for e in entries if Path(e["path"]).is_dir()]
    if len(alive) != len(entries):
        save(alive, store)
    return alive


# ---------------------------------------------------------------------------
# 表示用の合流(純関数 — draw から呼べるようにディスクを見ない)
# ---------------------------------------------------------------------------

def merge_shared(entries: list[dict],
                 folders: list[dict] | None) -> list[dict]:
    """履歴に、Syncthing が知っている共有フォルダを合流させる。

    履歴が土台。Syncthing 側にしかないもの(履歴を失った・別経路で参加した)は
    末尾に足す。同期の状態(相手の人数・一時停止)は、あれば添える。

    `folders` は `syncthing.status_report()` の `folders` をそのまま渡す。
    Syncthing が停止していれば None / 空でよく、その場合は履歴だけになる。
    """
    by_key: dict[str, dict] = {}
    out: list[dict] = []
    for e in entries:
        item = {"path": e["path"], "name": e.get("name") or display_name(e["path"]),
                "last_used": e.get("last_used", 0.0),
                "sync_id": "", "peers": 0, "paused": False, "shared": False}
        by_key[dedup_key(e["path"])] = item
        out.append(item)

    for f in (folders or []):
        path = f.get("path") or ""
        if not path:
            continue
        key = dedup_key(path)
        item = by_key.get(key)
        if item is None:
            item = {"path": path, "name": display_name(path),
                    "last_used": 0.0, "sync_id": "", "peers": 0,
                    "paused": False, "shared": False}
            by_key[key] = item
            out.append(item)
        item["sync_id"] = f.get("id", "")
        item["peers"] = len(f.get("devices") or [])
        item["paused"] = bool(f.get("paused"))
        item["shared"] = True

    # 使ったことがあるものが先。同点は名前順で、並びが毎回変わらないように
    out.sort(key=lambda e: (-e["last_used"], e["name"].lower()))
    return out
