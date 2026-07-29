# SPDX-FileCopyrightText: 2026 mochizukimonta
# SPDX-License-Identifier: GPL-3.0-or-later
"""musubi_pipeline.security — セキュリティ監査。

P2P同期でチーム共有されるフォルダは「他人が書き込めるフォルダ」であり、
そこから開く .blend は信頼できない入力として扱う必要がある。
この監査は以下を一括チェックする:

1. Blender の Python 自動実行(Auto Run)が無効になっているか
   → 同期されてきた .blend に仕込まれたスクリプトの自動実行を防ぐ(最重要)
2. 同期フォルダ内の実行可能ファイル混入
3. 同期競合ファイルの有無(データ消失の兆候)
4. 放置されたロック
5. プロジェクト外を指すシンボリックリンク
"""

from __future__ import annotations

import json
import os
import time
from pathlib import Path

from . import sync
from .core import PipelineError, atomic_replace, resolve_root, safe_path

LEVEL_OK = "OK"
LEVEL_WARN = "WARN"
LEVEL_CRITICAL = "CRITICAL"

ENV_DIR = (".musubi", "env")


def write_env(root_str: str) -> Path | None:
    """この端末の環境(Blender/アドオン/Python)を記録する(Blender内のみ)。"""
    try:
        import bpy
        blender = bpy.app.version_string
        blender_mm = list(bpy.app.version[:2])
    except ModuleNotFoundError:
        return None
    import sys
    from . import bl_info
    root = resolve_root(root_str)
    data = {
        "host": sync.host_id(),
        "blender": blender,
        "blender_mm": blender_mm,
        "addon": list(bl_info["version"]),
        "python": sys.version.split()[0],
        "written_at": time.strftime("%Y-%m-%dT%H:%M:%S"),
    }
    path = safe_path(root, *ENV_DIR, f"env_{sync.host_id()}.json")
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(data, ensure_ascii=False, indent=1),
                   encoding="utf-8")
    atomic_replace(tmp, path)
    return path


def check_env(root_str: str) -> list[tuple[str, str]]:
    """全端末の環境記録を突き合わせ、バージョン不一致を検出する。"""
    root = resolve_root(root_str)
    edir = root.joinpath(*ENV_DIR)
    envs = []
    if edir.is_dir():
        for f in sorted(edir.glob("env_*.json")):
            try:
                envs.append(json.loads(f.read_text(encoding="utf-8")))
            except (OSError, json.JSONDecodeError):
                continue
    if len(envs) < 2:
        return [(LEVEL_OK, "環境記録: 比較対象の端末がまだありません")]
    issues = []
    blenders = {tuple(e.get("blender_mm", [])): [] for e in envs}
    for e in envs:
        blenders[tuple(e.get("blender_mm", []))].append(
            f"{e.get('host','?')}={e.get('blender','?')}")
    if len(blenders) > 1:
        detail = " / ".join(", ".join(v) for v in blenders.values())
        issues.append((
            LEVEL_CRITICAL,
            f"Blenderバージョン不一致: {detail}。新しい版で保存した.blendは"
            "古い版で開くと壊れる恐れがあります。全端末を揃えてください"))
    addons = {tuple(e.get("addon", [])) for e in envs}
    if len(addons) > 1:
        hosts = ", ".join(f"{e.get('host','?')}=v"
                          + ".".join(map(str, e.get("addon", [])))
                          for e in envs)
        issues.append((LEVEL_WARN, f"アドオンのバージョン不一致: {hosts}"))
    return issues or [(LEVEL_OK,
                       f"環境一致: {len(envs)}端末すべて同じBlender/アドオン")]


def _check_autorun() -> tuple[str, str]:
    try:
        import bpy
        autorun = bpy.context.preferences.filepaths.use_scripts_auto_execute
    except Exception:
        return (LEVEL_WARN, "Blender外で実行中のためAuto Run設定を確認できません")
    if autorun:
        return (
            LEVEL_CRITICAL,
            "PythonスクリプトのAuto Runが有効です。同期されてきた.blendに"
            "仕込まれたコードが自動実行される恐れがあります。"
            "設定 > セーブ&ロード > Auto Run Python Scripts を無効にしてください",
        )
    return (LEVEL_OK, "Python Auto Run: 無効(安全)")


def _check_symlinks(root: Path) -> list[tuple[str, str]]:
    issues = []
    for dirpath, dirnames, filenames in os.walk(root, followlinks=False):
        for name in dirnames + filenames:
            p = Path(dirpath) / name
            if p.is_symlink():
                target = Path(os.path.realpath(p))
                try:
                    target.relative_to(root)
                except ValueError:
                    issues.append((
                        LEVEL_CRITICAL,
                        f"プロジェクト外を指すシンボリックリンク: "
                        f"{p.relative_to(root)} -> {target}",
                    ))
    return issues


def _check_stale_locks(root: Path) -> list[tuple[str, str]]:
    issues = []
    now = time.time()
    for lock in root.rglob("*.blend.lock"):
        try:
            info = json.loads(lock.read_text(encoding="utf-8"))
            age_h = (now - float(info.get("acquired_at", now))) / 3600
        except (json.JSONDecodeError, OSError, ValueError):
            issues.append((LEVEL_WARN, f"読めないロックファイル: {lock.name}"))
            continue
        if age_h > sync.LOCK_STALE_HOURS:
            issues.append((
                LEVEL_WARN,
                f"{lock.name}: {info.get('user','?')}@{info.get('host','?')} の"
                f"ロックが{age_h:.0f}時間放置されています",
            ))
    return issues


def audit(root_str: str) -> list[tuple[str, str]]:
    """監査を実行し (レベル, メッセージ) のリストを返す。"""
    results = [_check_autorun()]
    try:
        root = resolve_root(root_str)
    except PipelineError as e:
        results.append((LEVEL_WARN, str(e)))
        return results

    dangerous = sync.find_dangerous_files(root_str)
    if dangerous:
        for rel in dangerous[:10]:
            results.append((
                LEVEL_CRITICAL,
                f"実行可能ファイルが同期フォルダに混入: {rel}",
            ))
        if len(dangerous) > 10:
            results.append((LEVEL_CRITICAL, f"…ほか{len(dangerous)-10}件"))
    else:
        results.append((LEVEL_OK, "実行可能ファイルの混入なし"))

    conflicts = sync.find_conflicts(root_str)
    if conflicts:
        for rel in conflicts[:10]:
            results.append((LEVEL_WARN, f"同期競合ファイル: {rel}"))
    else:
        results.append((LEVEL_OK, "同期競合なし"))

    results.extend(_check_symlinks(root) or [(LEVEL_OK, "不正なシンボリックリンクなし")])
    results.extend(_check_stale_locks(root) or [(LEVEL_OK, "放置ロックなし")])

    # 環境の一致(自端末の記録を更新してから全端末分を比較)
    try:
        write_env(root_str)
    except (PipelineError, OSError):
        pass
    results.extend(check_env(root_str))
    return results
