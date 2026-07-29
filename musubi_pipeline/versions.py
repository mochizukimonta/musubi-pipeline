# SPDX-FileCopyrightText: 2026 mochizukimonta
# SPDX-License-Identifier: GPL-3.0-or-later
"""musubi_pipeline.versions — スナップショット式バージョン管理。

設計:
- 保存時にファイルを .musubi/versions/<相対パス>/ へ退避し、
  サイドカーJSON(作者・コメント・SHA-256)を添える
- 共有インデックスを持たない(一覧はフォルダ走査で構築)ため、
  複数端末が同時に保存してもP2P同期で競合しない
- ファイル名は <タイムスタンプ>_<端末名> で一意
- 内容が直前バージョンと同一(SHA-256一致)ならスキップ(重複排除)
- 復元は「現状を自動スナップショット→上書き」の順で行い、履歴を失わない
"""

from __future__ import annotations

import getpass
import json
import re
import shutil
import time
from pathlib import Path

from .core import PipelineError, resolve_root, safe_path
from .sync import _sha256, host_id

VERSIONS_DIR = (".musubi", "versions")
VERSION_NAME_RE = re.compile(r"^(\d{8}-\d{6})_([A-Za-z0-9\-_.]+)$")
DEFAULT_KEEP = 20


def _rel_under_root(root: Path, file_path: Path) -> Path:
    try:
        parts = Path(file_path).resolve().relative_to(root).parts
    except ValueError:
        raise PipelineError(
            f"プロジェクトルート外のファイルです: {file_path}") from None
    real = safe_path(root, *parts)
    rel = real.relative_to(root)
    if rel.parts[:1] == (".musubi",):
        raise PipelineError("管理フォルダ内のファイルはバージョン管理できません")
    return rel


def version_dir(root_str: str, file_path: Path) -> Path:
    """このファイルの履歴フォルダ(.musubi/versions/<rel>/)。"""
    root = resolve_root(root_str)
    rel = _rel_under_root(root, file_path)
    return safe_path(root, *VERSIONS_DIR, *rel.parts)


def _list_dir_versions(vdir: Path) -> list[dict]:
    """履歴フォルダを直接受け取り、新しい順に {name, path, meta} を返す。"""
    if not vdir.is_dir():
        return []
    items = []
    for f in vdir.iterdir():
        if f.suffix in (".json", ".tmp") or not f.is_file():
            continue
        if not VERSION_NAME_RE.match(f.stem):
            continue
        meta_path = f.with_suffix(".json")
        try:
            meta = json.loads(meta_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            meta = {}
        items.append({"name": f.stem, "path": f, "meta": meta})
    items.sort(key=lambda x: x["name"], reverse=True)
    return items


def list_versions(root_str: str, file_path: Path) -> list[dict]:
    """履歴を新しい順に返す。各要素: {name, path, meta}。"""
    return _list_dir_versions(version_dir(root_str, file_path))


def _iter_version_dirs(root: Path):
    """.musubi/versions/ 配下で、バージョンファイルを直接持つ全フォルダを返す。

    履歴フォルダはプロジェクト構造を写した形(例: .musubi/versions/scenes/
    scene01/c01.blend/)なので、その葉フォルダを列挙する。
    """
    vroot = root.joinpath(*VERSIONS_DIR)
    if not vroot.is_dir():
        return
    for d in vroot.rglob("*"):
        if not d.is_dir():
            continue
        for f in d.iterdir():
            if (f.is_file() and f.suffix not in (".json", ".tmp")
                    and VERSION_NAME_RE.match(f.stem)):
                yield d
                break


def snapshot(root_str: str, file_path: Path, comment: str = "") -> dict | None:
    """ファイルの現状態を履歴に退避する。

    直前バージョンと内容が同一ならNoneを返す(重複排除)。
    """
    root = resolve_root(root_str)
    rel = _rel_under_root(root, file_path)
    src = root / rel
    if not src.is_file():
        raise PipelineError(f"ファイルがありません: {src}")

    vdir = version_dir(root_str, src)
    vdir.mkdir(parents=True, exist_ok=True)
    stamp = time.strftime("%Y%m%d-%H%M%S")
    stem = f"{stamp}_{host_id()}"
    dst = vdir / f"{stem}{src.suffix}"
    n = 1
    while dst.exists():  # 同一秒内の連続保存
        dst = vdir / f"{stem}.{n}{src.suffix}"
        n += 1

    # 先にコピーし、コピー実体をハッシュする。元ファイルはコピー中に
    # 次の保存で書き換わり得るため、元をハッシュすると記録と実体がズレる。
    tmp = dst.parent / (dst.name + ".tmp")
    shutil.copy2(src, tmp)
    digest = _sha256(tmp)
    size = tmp.stat().st_size
    existing = list_versions(root_str, src)
    if existing and existing[0]["meta"].get("sha256") == digest:
        tmp.unlink(missing_ok=True)
        return None  # 内容が変わっていない
    tmp.replace(dst)

    meta = {
        "file": str(rel).replace("\\", "/"),
        "sha256": digest,
        "size": size,
        "author": getpass.getuser(),
        "host": host_id(),
        "comment": comment.strip(),
        "saved_at": time.strftime("%Y-%m-%dT%H:%M:%S"),
    }
    dst.with_suffix(".json").write_text(
        json.dumps(meta, ensure_ascii=False, indent=1), encoding="utf-8")
    return meta


def restore(root_str: str, file_path: Path, version_name: str) -> dict:
    """指定バージョンへ復元する。復元前に現状を自動スナップショットする。"""
    if not VERSION_NAME_RE.match(version_name.split(".")[0]):
        raise PipelineError(f"バージョン名が不正です: {version_name}")
    root = resolve_root(root_str)
    rel = _rel_under_root(root, file_path)
    target = root / rel
    vdir = version_dir(root_str, target)
    candidates = [v for v in list_versions(root_str, target)
                  if v["name"] == version_name]
    if not candidates:
        raise PipelineError(f"バージョンが見つかりません: {version_name}")
    src = candidates[0]["path"]
    # 実体パスの最終確認(履歴フォルダ配下であること)
    try:
        safe_path(root, *src.resolve().relative_to(root).parts)
    except ValueError:
        raise PipelineError("履歴フォルダ外からの復元は禁止です") from None
    if not str(src).startswith(str(vdir)):
        raise PipelineError("履歴フォルダ外からの復元は禁止です")

    pre = None
    if target.exists():
        pre = snapshot(root_str, target, comment="復元前の自動スナップショット")
    shutil.copy2(src, target)
    return {
        "restored": version_name,
        "meta": candidates[0]["meta"],
        "pre_snapshot": bool(pre),
    }


def _prune_dir(vdir: Path, keep: int) -> int:
    """1つの履歴フォルダで新しい順に keep 世代を残し、残りを削除。"""
    removed = 0
    for v in _list_dir_versions(vdir)[keep:]:
        v["path"].unlink(missing_ok=True)
        v["path"].with_suffix(".json").unlink(missing_ok=True)
        removed += 1
    return removed


def prune(root_str: str, file_path: Path, keep: int = DEFAULT_KEEP) -> int:
    """新しい順に keep 世代を残して古い履歴を削除。削除数を返す。"""
    if keep < 1:
        raise PipelineError("保持世代数は1以上にしてください")
    vdir = version_dir(root_str, file_path)
    return _prune_dir(vdir, keep)


def prune_all(root_str: str, keep: int = DEFAULT_KEEP) -> tuple[int, int]:
    """プロジェクト内の全ファイルの履歴を keep 世代まで整理する。

    戻り値: (削除した世代数, 対象になったファイル数)。
    """
    if keep < 1:
        raise PipelineError("保持世代数は1以上にしてください")
    root = resolve_root(root_str)
    removed = files = 0
    for vdir in _iter_version_dirs(root):
        files += 1
        removed += _prune_dir(vdir, keep)
    return removed, files


def enforce_size_cap(root_str: str, max_bytes: int) -> tuple[int, int]:
    """履歴の合計サイズが上限を超えていたら、古い世代から削除して収める。

    各ファイルの最新世代は必ず残す(復元不能を防ぐ)。世代名は
    <日時>_<端末> なので、名前順=時刻順で全ファイル横断の最古から削る。
    max_bytes<=0 は無効(何もしない)。戻り値: (削除世代数, 解放バイト)。
    """
    if max_bytes <= 0:
        return (0, 0)
    root = resolve_root(root_str)
    gens = []       # 削除候補(各ファイルの最新は protected=True で除外)
    total = 0
    for vdir in _iter_version_dirs(root):
        vers = _list_dir_versions(vdir)  # 新しい順
        for i, v in enumerate(vers):
            try:
                size = v["path"].stat().st_size
            except OSError:
                size = 0
            total += size
            gens.append({"path": v["path"], "size": size,
                         "name": v["name"], "protected": i == 0})
    if total <= max_bytes:
        return (0, 0)
    deletable = sorted((g for g in gens if not g["protected"]),
                       key=lambda g: g["name"])  # 最古から
    removed = freed = 0
    for g in deletable:
        if total <= max_bytes:
            break
        g["path"].unlink(missing_ok=True)
        g["path"].with_suffix(".json").unlink(missing_ok=True)
        total -= g["size"]
        freed += g["size"]
        removed += 1
    return (removed, freed)


def history_size(root_str: str) -> tuple[int, int]:
    """プロジェクト全体の履歴の (ファイル数, 合計バイト) 。"""
    root = resolve_root(root_str)
    vroot = root.joinpath(*VERSIONS_DIR)
    if not vroot.is_dir():
        return (0, 0)
    count = total = 0
    for f in vroot.rglob("*"):
        if f.is_file() and f.suffix != ".json":
            count += 1
            total += f.stat().st_size
    return (count, total)
