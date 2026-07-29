# SPDX-FileCopyrightText: 2026 mochizukimonta
# SPDX-License-Identifier: GPL-3.0-or-later
"""musubi_pipeline.spec — プロジェクト共通仕様の管理。

仕様の本体は「本番」「仮」2つのレンダープロファイル(型付きの実設定)。
fps・解像度・View Layerパス・フィルム・出力形式などを
「この値にしてください」という形で持ち、
- ワンクリックでシーンへ適用
- 「シーンから取り込み」で編集(手入力なし)
- 品質チェックが差分を赤で指摘
に使う。.musubi/spec.json として同期で全端末に配布され、
board.html にも掲載される。更新者・更新日時を自動記録。

補助として自由記述の「備考」を1欄だけ持つ(映像尺・納品先などのメモ用。
表形式の行管理は廃止 — v0.14で単純化)。
"""

from __future__ import annotations

import getpass
import json
import time
from pathlib import Path

from .core import PipelineError, atomic_replace, resolve_root, safe_path
from .sync import host_id

SPEC_PATH = (".musubi", "spec.json")


def default_profiles() -> dict:
    """レンダー仕様テンプレートの既定値(本番=EXR連番 / 仮=mp4)。

    キーは render_profiles.SETTINGS のレジストリと対応する。
    実運用では「シーンから取り込み」で現場の設定に置き換える想定。
    """
    return {
        "final": {
            "label": "本番(EXRマルチレイヤー連番)",
            "settings": {
                "engine": "CYCLES",
                "cycles_samples": 128,
                "cycles_denoise": True,
                "resolution_x": 1920,
                "resolution_y": 1080,
                "resolution_percentage": 100,
                "fps": 24,
                "filter_size": 0.01,      # ピクセルフィルター実質0(輪郭シャープ)
                "film_transparent": True,  # コンポジット前提でアルファ付き
                "view_transform": "Standard",
                "output_format": "OPEN_EXR_MULTILAYER",
                "color_depth": "16",
                "exr_codec": "ZIP",
                "pass_combined": True,
                "pass_z": True,            # コンポジットでの奥行き利用
                "pass_normal": False,
                "pass_mist": False,
                "pass_emit": True,         # ライトは放射のみ
                "crypto_object": True,     # コンポジット用ID分け
                "crypto_material": True,
                "crypto_asset": False,
                "crypto_depth": 6,
            },
        },
        "preview": {
            "label": "仮(mp4プレビュー)",
            "settings": {
                "engine": "CYCLES",
                "cycles_samples": 16,
                "cycles_denoise": True,
                "resolution_x": 1920,
                "resolution_y": 1080,
                "resolution_percentage": 50,
                "fps": 24,
                "filter_size": 1.5,
                "film_transparent": False,
                "view_transform": "Standard",
                "output_format": "FFMPEG_MP4",
                "color_depth": "8",
                "pass_combined": True,
                "pass_z": False,
                "pass_normal": False,
                "pass_mist": False,
                "pass_emit": False,
                "crypto_object": False,
                "crypto_material": False,
                "crypto_asset": False,
            },
        },
    }


NOTES_MAX = 2000


def default_spec(project_name: str) -> dict:
    """初期テンプレート。仕様の本体はレンダープロファイル、備考は自由メモ。"""
    return {
        "format": 2,
        "title": f"{project_name} 基本仕様",
        "render_profiles": default_profiles(),
        "notes": "",
        "updated_at": "",
        "updated_by": "",
    }


def spec_path(root_str: str) -> Path:
    return safe_path(resolve_root(root_str), *SPEC_PATH)


def load(root_str: str) -> dict:
    """仕様を読む。ファイルがなければ初期テンプレートを返す(書き込みはしない)。"""
    root = resolve_root(root_str)
    p = root.joinpath(*SPEC_PATH)
    if not p.exists():
        return default_spec(root.name)
    try:
        data = json.loads(p.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as e:
        raise PipelineError(f"仕様ファイルが読めません: {e}") from None
    if data.get("format") not in (1, 2):
        raise PipelineError("仕様ファイルの形式が不正です")
    base = default_spec(root.name)
    base.update({k: data[k] for k in base if k in data})
    base["format"] = 2
    # 旧形式(v0.12以前の"render"キー)からの移行: 解像度とfpsを両プロファイルへ
    if "render_profiles" not in data and isinstance(data.get("render"), dict):
        old = data["render"]
        for prof in base["render_profiles"].values():
            for key in ("resolution_x", "resolution_y", "fps"):
                if isinstance(old.get(key), int):
                    prof["settings"][key] = old[key]
    # 旧形式(format 1)の表形式セクション → 備考テキストへ退避(データ保全)
    if data.get("format") == 1 and isinstance(data.get("sections"), list) \
            and not base.get("notes"):
        lines = []
        for sec in data["sections"]:
            try:
                lines.append(f"【{sec['name']}】")
                lines += [f"{k}: {v}" for k, v in sec.get("rows", [])]
            except (KeyError, TypeError, ValueError):
                continue
        base["notes"] = "\n".join(lines)[:NOTES_MAX]
    return base


def save(root_str: str, data: dict) -> Path:
    """仕様を保存する(更新者・日時を自動記録)。"""
    profs = data.get("render_profiles")
    if not isinstance(profs, dict) or not profs:
        raise PipelineError("レンダー仕様(render_profiles)がありません")
    for pid, prof in profs.items():
        if not isinstance(prof, dict) or not isinstance(prof.get("settings"),
                                                        dict):
            raise PipelineError(f"プロファイル '{pid}' の形式が不正です")
        prof["label"] = str(prof.get("label", pid))[:100]
        # 値はスカラーのみ許可(取り込み経由でも異物が混ざらないように)
        prof["settings"] = {
            str(k)[:50]: v for k, v in prof["settings"].items()
            if isinstance(v, (bool, int, float, str)) and len(str(v)) <= 100
        }
    data["format"] = 2
    data["title"] = str(data.get("title", ""))[:100]
    data["notes"] = str(data.get("notes", ""))[:NOTES_MAX]
    data.pop("render", None)    # v0.12以前のキーは破棄(移行済み)
    data.pop("sections", None)  # v0.13以前の表形式は破棄(備考へ移行済み)
    data["updated_at"] = time.strftime("%Y-%m-%dT%H:%M:%S")
    data["updated_by"] = f"{getpass.getuser()}@{host_id()}"

    p = spec_path(root_str)
    p.parent.mkdir(parents=True, exist_ok=True)
    tmp = p.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(data, ensure_ascii=False, indent=1),
                   encoding="utf-8")
    atomic_replace(tmp, p)
    return p
