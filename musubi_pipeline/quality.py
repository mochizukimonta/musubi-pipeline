# SPDX-FileCopyrightText: 2026 mochizukimonta
# SPDX-License-Identifier: GPL-3.0-or-later
"""musubi_pipeline.quality — 品質チェック(バリデーション)とアセットリンク管理。

方針(ユーザー決定):
- work/publish分離は行わない。チェックは「赤いアラートで気づける」ことが目的で、
  保存やパブリッシュを止めない
- 自動修正はしない。修正は明示的なボタン(確認付き)でのみ実行する

チェック項目:
1. スケール未適用(ローカルのメッシュ系、スケールにキーが無いもののみ)
2. 命名規則違反(Cube/Material.001 などBlenderデフォルト名のまま)
3. テクスチャ: パック済みか / リンク切れ / プロジェクト外参照
4. 不要データ(参照ゼロの孤立データブロック)
5. ライブラリリンク: リンク切れ / 絶対パス / プロジェクト外参照

依存関係の記録:
- カット保存時に、そのカットがリンクしているライブラリ(.blend)を
  .musubi/deps/sceneXX_cYY.json に記録 → 「このアセットはどのカットで
  使われているか」を横断参照できる(breakdown)
"""

from __future__ import annotations

import json
import re
import time
from pathlib import Path

import bpy

from .core import atomic_replace, cut_name, resolve_root, safe_path, scene_name
from .sync import host_id

LEVEL_OK = "OK"
LEVEL_WARN = "WARN"
LEVEL_CRITICAL = "CRITICAL"

DEPS_DIR = (".musubi", "deps")
_DEPS_FILE_RE = re.compile(r"^(scene\d{2,3})_(c\d{2,3})\.json$")

DEFAULT_NAME_RE = re.compile(
    r"^(Cube|Cylinder|Sphere|Icosphere|IcoSphere|Plane|Circle|Cone|Torus|Grid|"
    r"Suzanne|Monkey|Empty|Text|Curve|BézierCurve|BezierCurve|BezierCircle|"
    r"NurbsPath|NurbsCurve|Material|Mesh|Light|Point|Spot|Sun|Area|Camera|"
    r"Armature)(\.\d{3})?$")

_SPECIAL_IMAGES = {"Render Result", "Viewer Node"}


def _scale_animated(ob) -> bool:
    ad = ob.animation_data
    if not ad or not ad.action:
        return False
    try:  # Blender 5.x レイヤードアクション
        for layer in ad.action.layers:
            for strip in layer.strips:
                for bag in strip.channelbags:
                    if any(fc.data_path == "scale" for fc in bag.fcurves):
                        return True
    except AttributeError:  # 旧API
        try:
            return any(fc.data_path == "scale" for fc in ad.action.fcurves)
        except AttributeError:
            return False
    return False


def check_unapplied_scale() -> list[str]:
    """スケールが(1,1,1)でないローカルオブジェクト(スケールキー付きは除外)。"""
    bad = []
    for ob in bpy.data.objects:
        if ob.library or ob.type not in {'MESH', 'CURVE', 'FONT', 'SURFACE',
                                         'META'}:
            continue
        if any(abs(s - 1.0) > 1e-5 for s in ob.scale) and not _scale_animated(ob):
            bad.append(ob.name)
    return sorted(bad)


def check_default_names() -> list[str]:
    """Blenderデフォルト名のままのオブジェクト・マテリアル。"""
    bad = [f"OBJ:{ob.name}" for ob in bpy.data.objects
           if not ob.library and DEFAULT_NAME_RE.match(ob.name)]
    bad += [f"MAT:{m.name}" for m in bpy.data.materials
            if not m.library and DEFAULT_NAME_RE.match(m.name)]
    return sorted(bad)


def check_images(root: Path) -> tuple[list[str], list[str]]:
    """(リンク切れ画像, プロジェクト外参照でパック未の画像)。"""
    missing, outside = [], []
    for img in bpy.data.images:
        if img.library or img.name in _SPECIAL_IMAGES:
            continue
        if img.source not in ('FILE', 'SEQUENCE', 'MOVIE'):
            continue
        if img.packed_file:
            continue
        if not img.filepath:
            missing.append(img.name)
            continue
        ap = Path(bpy.path.abspath(img.filepath, library=img.library))
        if not ap.exists():
            missing.append(f"{img.name} ({img.filepath})")
        else:
            try:
                ap.resolve().relative_to(root)
            except (ValueError, OSError):
                outside.append(f"{img.name} ({img.filepath})")
    return sorted(missing), sorted(outside)


def check_orphans() -> int:
    """参照ゼロ(フェイクユーザーなし)の孤立データブロック数。"""
    count = 0
    for coll in (bpy.data.meshes, bpy.data.materials, bpy.data.images,
                 bpy.data.textures, bpy.data.actions, bpy.data.curves,
                 bpy.data.lights, bpy.data.cameras, bpy.data.armatures,
                 bpy.data.node_groups):
        for block in coll:
            if block.users == 0 and not block.use_fake_user \
                    and getattr(block, "name", "") not in _SPECIAL_IMAGES:
                count += 1
    return count


def check_libraries(root: Path) -> tuple[list[str], list[str], list[str]]:
    """(リンク切れライブラリ, 絶対パスのライブラリ, プロジェクト外ライブラリ)。"""
    missing, absolute, outside = [], [], []
    for lib in bpy.data.libraries:
        ap = Path(bpy.path.abspath(lib.filepath))
        if not ap.exists():
            missing.append(lib.filepath)
            continue
        if not lib.filepath.startswith("//"):
            absolute.append(lib.filepath)
        try:
            ap.resolve().relative_to(root)
        except (ValueError, OSError):
            outside.append(lib.filepath)
    return sorted(missing), sorted(absolute), sorted(outside)


def _fmt(items: list[str], limit: int = 4) -> str:
    shown = ", ".join(items[:limit])
    more = f" …ほか{len(items) - limit}件" if len(items) > limit else ""
    return shown + more


def run_all(root_str: str) -> list[tuple[str, str]]:
    """全チェックを実行し (レベル, メッセージ) を返す。"""
    root = resolve_root(root_str)
    results = []

    scale = check_unapplied_scale()
    results.append((LEVEL_WARN, f"スケール未適用 {len(scale)}件: {_fmt(scale)}")
                   if scale else (LEVEL_OK, "スケール: 全て適用済み"))

    names = check_default_names()
    results.append((LEVEL_WARN, f"デフォルト名のまま {len(names)}件: {_fmt(names)}")
                   if names else (LEVEL_OK, "命名: デフォルト名なし"))

    missing_img, outside_img = check_images(root)
    if missing_img:
        results.append((LEVEL_CRITICAL,
                        f"テクスチャのリンク切れ {len(missing_img)}件: "
                        f"{_fmt(missing_img)}"))
    if outside_img:
        results.append((LEVEL_WARN,
                        f"プロジェクト外のテクスチャ(パック推奨) "
                        f"{len(outside_img)}件: {_fmt(outside_img)}"))
    if not missing_img and not outside_img:
        results.append((LEVEL_OK, "テクスチャ: パック済み/プロジェクト内"))

    orphans = check_orphans()
    results.append((LEVEL_WARN, f"不要データ(孤立データブロック) {orphans}件")
                   if orphans else (LEVEL_OK, "不要データなし"))

    # プロジェクト仕様(レンダープロファイル)との整合 — 3段階判定
    try:
        from . import render_profiles, spec as spec_mod
        profs = spec_mod.load(root_str).get("render_profiles", {})
        scn, vl = bpy.context.scene, bpy.context.view_layer
        d_final = render_profiles.diff(
            profs.get("final", {}).get("settings", {}), scn, vl)
        d_prev = render_profiles.diff(
            profs.get("preview", {}).get("settings", {}), scn, vl)
        if profs.get("final") and not d_final:
            results.append((LEVEL_OK, "レンダー設定: 本番仕様と一致"))
        elif profs.get("preview") and not d_prev:
            results.append((LEVEL_WARN,
                            "レンダー設定: 仮(プレビュー)仕様です。"
                            "本番出力の前に「本番を適用」を忘れずに"))
        elif d_final:
            fv = render_profiles.format_value
            items = " / ".join(f"{lbl} {fv(cur)}(仕様:{fv(sv)})"
                               for lbl, cur, sv in d_final[:4])
            more = f" …ほか{len(d_final) - 4}件" if len(d_final) > 4 else ""
            results.append((LEVEL_WARN,
                            f"レンダー設定が本番仕様と不一致: {items}{more}。"
                            "「本番を適用」で直せます"))
    except Exception:
        pass

    lib_missing, lib_abs, lib_out = check_libraries(root)
    if lib_missing:
        results.append((LEVEL_CRITICAL,
                        f"リンク切れライブラリ {len(lib_missing)}件: "
                        f"{_fmt(lib_missing)}"))
    if lib_abs:
        results.append((LEVEL_WARN,
                        f"絶対パスのリンク(他端末で壊れる) {len(lib_abs)}件: "
                        f"{_fmt(lib_abs)}"))
    if lib_out:
        results.append((LEVEL_WARN,
                        f"プロジェクト外へのリンク {len(lib_out)}件: "
                        f"{_fmt(lib_out)}"))
    if any("copybuffer.blend" in p.lower()
           for p in lib_missing + lib_abs + lib_out):
        results.append((LEVEL_WARN,
                        "↑ copybuffer.blend はBlenderがコピー&ペースト"
                        "(Ctrl+C/V)に使う一時ファイルです(このアドオンや"
                        "バックアップとは無関係)。貼り付け時に紛れ込んだ参照"
                        "なので、「未使用データを整理」で消えない場合は該当"
                        "オブジェクトを選択して「オブジェクト > 関係 > "
                        "ローカル化」でリンクを切ってください"))
    if not lib_missing and not lib_abs and not lib_out:
        results.append((LEVEL_OK, "リンク: 問題なし"))

    return results


# ---------------------------------------------------------------------------
# 依存関係の記録(breakdown)
# ---------------------------------------------------------------------------

def record_deps(root_str: str, scene_no: int, cut_no: int) -> Path:
    """このカットがリンクしているライブラリを記録する。"""
    root = resolve_root(root_str)
    libs = []
    for lib in bpy.data.libraries:
        ap = Path(bpy.path.abspath(lib.filepath))
        try:
            rel = str(ap.resolve().relative_to(root)).replace("\\", "/")
        except (ValueError, OSError):
            rel = lib.filepath  # プロジェクト外はそのまま記録(チェックで警告)
        libs.append(rel)
    data = {
        "format": 1,
        "scene": scene_no,
        "cut": cut_no,
        "libraries": sorted(set(libs)),
        "saved_at": time.strftime("%Y-%m-%dT%H:%M:%S"),
        "host": host_id(),
    }
    path = safe_path(root, *DEPS_DIR,
                     f"{scene_name(scene_no)}_{cut_name(cut_no)}.json")
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(data, ensure_ascii=False, indent=1),
                   encoding="utf-8")
    atomic_replace(tmp, path)
    return path


def asset_usage(root_str: str) -> dict[str, list[str]]:
    """アセット(ライブラリ) → 使用カット の対応表。bpy不要で全端末分を集計。"""
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
        cut_label = f"{m.group(1)}/{m.group(2)}"
        for lib in d.get("libraries", []):
            usage.setdefault(lib, []).append(cut_label)
    return usage
