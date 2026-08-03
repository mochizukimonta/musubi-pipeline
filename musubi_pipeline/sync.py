# SPDX-FileCopyrightText: 2026 mochizukimonta
# SPDX-License-Identifier: GPL-3.0-or-later
"""musubi_pipeline.sync — 同期整合性の担保。

P2P同期(Resilio Sync / Syncthing)そのものは外部ツールに委任する。
このモジュールは「本当に同期できているか」を暗号学的ハッシュで検証する:

- 各端末が SHA-256 同期照合リスト(.musubi/manifest_<host>.json)を書き出す
- 照合リスト自体も同期されるので、他端末の照合リストと
  ローカルの実ファイルを突き合わせて 一致 / 欠落 / 差分 を報告できる
- Resilio / Syncthing の競合ファイルを検出する
- アドバイザリロックで .blend の同時編集を防ぐ(ロックも同期に載せる)
"""

from __future__ import annotations

import fnmatch
import getpass
import hashlib
import json
import os
import socket
import time
from pathlib import Path

from .core import PipelineError, resolve_root, safe_path

MANIFEST_DIR = ".musubi"

# 照合リスト対象から除外するもの(一時ファイル・同期ツールの内部・ロック)
IGNORE_PATTERNS = (
    ".musubi/*",          # 照合リスト自身・管理ファイル
    ".sync/*",           # Resilio Sync 内部
    ".stfolder*", ".stversions/*", ".stignore",  # Syncthing 内部
    "*.blend1", "*.blend2", "*.blend@",          # Blender バックアップ/一時
    "*.tmp", "*.TMP", "*~",
    "*.lock",
    "Thumbs.db", "desktop.ini", ".DS_Store",
    "*.!sync",           # Resilio 転送中ファイル
)

# 同期フォルダに混入してはならない実行可能ファイル(フォルダポイズニング対策)
# .html/.svg 等はブラウザで開くとスクリプトが動くため実行形式に準じて扱う
# (進捗ボードは各端末のローカル生成・同期対象外なので誤検知しない)
DANGEROUS_EXTENSIONS = (
    ".py", ".pyc", ".pyw", ".exe", ".dll", ".scr", ".com", ".msi",
    ".bat", ".cmd", ".ps1", ".vbs", ".js", ".jse", ".wsf", ".lnk",
    ".html", ".htm", ".svg", ".url", ".scf", ".hta",
)

# 競合ファイルの命名パターン(Resilio / Syncthing)
CONFLICT_MARKERS = (".sync-conflict-", ".Conflict-", ".Conflict.")

LOCK_STALE_HOURS = 12.0


def _ignored(rel: str) -> bool:
    rel = rel.replace(os.sep, "/")
    name = rel.rsplit("/", 1)[-1]
    return any(
        fnmatch.fnmatch(rel, pat) or fnmatch.fnmatch(name, pat)
        for pat in IGNORE_PATTERNS
    )


def _sha256(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def _walk_files(root: Path):
    """ルート外へ出るシンボリックリンクを辿らずに走査する。"""
    for dirpath, dirnames, filenames in os.walk(root, followlinks=False):
        rel_dir = os.path.relpath(dirpath, root)
        if rel_dir != "." and _ignored(rel_dir + "/x"):
            dirnames.clear()
            continue
        for fn in filenames:
            rel = fn if rel_dir == "." else f"{rel_dir}/{fn}"
            rel = rel.replace(os.sep, "/")
            if not _ignored(rel):
                yield rel, Path(dirpath) / fn


def host_id() -> str:
    return socket.gethostname()


# ---------------------------------------------------------------------------
# 同期照合リスト
# ---------------------------------------------------------------------------

def write_manifest(root_str: str) -> Path:
    """この端末の同期照合リストを .musubi/manifest_<host>.json に書き出す。"""
    root = resolve_root(root_str)
    files = {}
    for rel, path in sorted(_walk_files(root)):
        st = path.stat()
        files[rel] = {"sha256": _sha256(path), "size": st.st_size}
    data = {
        "format": 1,
        "host": host_id(),
        "user": getpass.getuser(),
        "generated_at": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
        "file_count": len(files),
        "files": files,
    }
    out = safe_path(root, MANIFEST_DIR, f"manifest_{host_id()}.json")
    out.parent.mkdir(parents=True, exist_ok=True)
    tmp = out.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(data, indent=1, ensure_ascii=False),
                   encoding="utf-8")
    from .core import atomic_replace
    atomic_replace(tmp, out)
    return out


def list_peer_manifests(root_str: str) -> list[Path]:
    """同期されてきた他端末の照合リスト一覧。"""
    root = resolve_root(root_str)
    mdir = root / MANIFEST_DIR
    if not mdir.is_dir():
        return []
    me = f"manifest_{host_id()}.json"
    return sorted(
        p for p in mdir.glob("manifest_*.json") if p.name != me
    )


def verify_against(root_str: str, manifest_path: str | Path) -> dict:
    """他端末の照合リストとローカル実ファイルを突き合わせる。

    返り値: {"peer", "generated_at", "match", "missing_here",
             "differs", "extra_here"}
    """
    root = resolve_root(root_str)
    mp = Path(os.path.realpath(manifest_path))
    try:
        mp.relative_to(root)
    except ValueError:
        raise PipelineError("照合リストがプロジェクト外にあります") from None

    data = json.loads(mp.read_text(encoding="utf-8"))
    if data.get("format") != 1 or not isinstance(data.get("files"), dict):
        raise PipelineError(f"照合リストの形式が不正です: {mp.name}")

    local = {rel: path for rel, path in _walk_files(root)}
    peer_files = data["files"]

    match, missing_here, differs = [], [], []
    for rel, meta in peer_files.items():
        lp = local.get(rel)
        if lp is None:
            missing_here.append(rel)
        elif lp.stat().st_size != meta.get("size") or _sha256(lp) != meta.get("sha256"):
            differs.append(rel)
        else:
            match.append(rel)
    extra_here = sorted(set(local) - set(peer_files))

    return {
        "peer": data.get("host", "?"),
        "generated_at": data.get("generated_at", "?"),
        "match": sorted(match),
        "missing_here": sorted(missing_here),
        "differs": sorted(differs),
        "extra_here": extra_here,
        "in_sync": not missing_here and not differs and not extra_here,
    }


# ---------------------------------------------------------------------------
# 競合・危険ファイル検出
# ---------------------------------------------------------------------------

def find_conflicts(root_str: str) -> list[str]:
    """Resilio / Syncthing が生成した競合コピーを列挙する。"""
    root = resolve_root(root_str)
    hits = []
    for rel, _ in _walk_files(root):
        if any(m in rel for m in CONFLICT_MARKERS):
            hits.append(rel)
    return sorted(hits)


def find_dangerous_files(root_str: str) -> list[str]:
    """同期フォルダに混入した実行可能ファイルを列挙する。

    P2P共有では他端末経由で悪意あるファイルが置かれる可能性があるため、
    成果物(.blend / 画像 / 動画)以外の実行形式は警告対象とする。
    """
    root = resolve_root(root_str)
    return sorted(
        rel for rel, _ in _walk_files(root)
        if rel.lower().endswith(DANGEROUS_EXTENSIONS)
    )


# ---------------------------------------------------------------------------
# アドバイザリロック(同時編集防止)
# ---------------------------------------------------------------------------

def _lock_path(blend_path: Path) -> Path:
    return blend_path.with_name(blend_path.name + ".lock")


def read_lock(blend_path: Path) -> dict | None:
    lp = _lock_path(blend_path)
    if not lp.exists():
        return None
    try:
        return json.loads(lp.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return {"host": "?", "user": "?", "acquired_at": 0}


def lock_age_hours(info: dict | None) -> float:
    """ロック取得からの経過時間(時間)。

    同期フォルダは信頼しないので、`acquired_at` が数値でない場合は
    「壊れている = いくらでも古い」とみなす(解除の対象にはなるが、
    取得の妨げにはならない側に倒す)。
    """
    if not info:
        return 0.0
    try:
        return (time.time() - float(info.get("acquired_at", 0))) / 3600
    except (TypeError, ValueError):
        return float("inf")


def is_stale_lock(info: dict | None) -> bool:
    """放置ロック(既定12時間超)かどうか。"""
    return bool(info) and lock_age_hours(info) > LOCK_STALE_HOURS


def acquire_lock(root_str: str, blend_path: Path) -> dict:
    """ロックを取得する。他端末が保持していれば PipelineError。"""
    root = resolve_root(root_str)
    try:
        parts = blend_path.relative_to(root).parts
    except ValueError:
        raise PipelineError(
            f"プロジェクトルート外のファイルです: {blend_path}") from None
    blend_path = safe_path(root, *parts)
    existing = read_lock(blend_path)
    if existing and existing.get("host") != host_id():
        age_h = lock_age_hours(existing)
        stale = f"(取得から{age_h:.1f}時間経過・要確認)" if age_h > LOCK_STALE_HOURS else ""
        raise PipelineError(
            f"{blend_path.name} は {existing.get('user','?')}@{existing.get('host','?')} "
            f"が編集中です {stale}"
        )
    info = {
        "host": host_id(),
        "user": getpass.getuser(),
        "acquired_at": time.time(),
        "file": blend_path.name,
    }
    _lock_path(blend_path).write_text(
        json.dumps(info, ensure_ascii=False), encoding="utf-8"
    )
    return info


def release_lock(root_str: str, blend_path: Path) -> bool:
    """自分の保持するロックを解放する。他人のロックは解放しない。"""
    root = resolve_root(root_str)
    try:
        parts = blend_path.relative_to(root).parts
    except ValueError:
        raise PipelineError(
            f"プロジェクトルート外のファイルです: {blend_path}") from None
    blend_path = safe_path(root, *parts)
    existing = read_lock(blend_path)
    if existing is None:
        return False
    if existing.get("host") != host_id():
        raise PipelineError(
            f"他端末({existing.get('host','?')})のロックは解放できません"
        )
    _lock_path(blend_path).unlink(missing_ok=True)
    return True


def force_release_lock(root_str: str, blend_path: Path) -> dict:
    """放置ロックに限り、他端末のロックでも解除する。

    `release_lock` が他端末のロックを拒むのは、作業中の相手からロックを
    奪わないためです。しかし Blender の強制終了・PC故障・長期不在では
    ロックが残り続け、手作業で `.lock` を消す以外に手がなくなります。
    そこで **LOCK_STALE_HOURS を過ぎたものだけ** を解除できるようにします。

    まだ経過時間が足りないロックは PipelineError にします(奪い合いの防止)。
    解除した相手の情報を返すので、呼び出し側は誰のロックを外したかを
    報告できます。
    """
    root = resolve_root(root_str)
    try:
        parts = blend_path.relative_to(root).parts
    except ValueError:
        raise PipelineError(
            f"プロジェクトルート外のファイルです: {blend_path}") from None
    blend_path = safe_path(root, *parts)
    existing = read_lock(blend_path)
    if existing is None:
        raise PipelineError(f"{blend_path.name} にロックはありません")
    if existing.get("host") != host_id() and not is_stale_lock(existing):
        age_h = lock_age_hours(existing)
        raise PipelineError(
            f"{existing.get('user','?')}@{existing.get('host','?')} が"
            f"{age_h:.1f}時間前から編集中です。"
            f"解除できるのは{LOCK_STALE_HOURS:.0f}時間を過ぎた放置ロックだけです"
        )
    _lock_path(blend_path).unlink(missing_ok=True)
    return existing
