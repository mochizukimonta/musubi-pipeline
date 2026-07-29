# SPDX-FileCopyrightText: 2026 mochizukimonta
# SPDX-License-Identifier: GPL-3.0-or-later
"""musubi_pipeline.render_profiles — レンダー設定テンプレートの取込・適用・差分。

現場でいじる設定を「レジストリ」として型付きで定義し、
- capture: 現在のシーン設定を読み取って仕様テンプレート化(手入力の廃止)
- apply:   仕様テンプレートをシーンへワンクリック反映
- diff:    シーンと仕様の差分列挙(品質チェック・パネルの赤表示の判定材料)
を同じ定義から行う。項目の追加はこのレジストリに1行足すだけ。

各エントリは (グループ, 表示ラベル, getter(sc, vl), setter(sc, vl, v))。
グループはBlenderの実際のUI上の場所に合わせてあり、
「現場でどこをいじるのか」がそのまま仕様書の見出しになる。

注意: bpyをimportしない(シーン/ビューレイヤーは引数で受ける)。
これによりboard.html生成などBlender外のコードからも安全に参照できる。
"""

from __future__ import annotations

G_RENDER = "レンダープロパティ"
G_FILM = "レンダープロパティ > フィルム"
G_OUTPUT = "出力プロパティ"
G_COLOR = "カラーマネジメント"
G_PASSES = "View Layer > パス"


def _get_output_format(sc, vl):
    """出力形式を1つの値に正規化(EXR連番/mp4/PNG連番)。"""
    fmt = sc.render.image_settings.file_format
    if fmt == 'FFMPEG':
        return "FFMPEG_MP4"
    return fmt


def _set_output_format(sc, vl, v):
    ims = sc.render.image_settings
    if v == "FFMPEG_MP4":
        if hasattr(ims, "media_type"):  # Blender 5.x
            ims.media_type = 'VIDEO'
        ims.file_format = 'FFMPEG'
        sc.render.ffmpeg.format = 'MPEG4'
        sc.render.ffmpeg.codec = 'H264'
    else:
        if hasattr(ims, "media_type"):
            ims.media_type = ('MULTI_LAYER_IMAGE'
                              if v == 'OPEN_EXR_MULTILAYER' else 'IMAGE')
        ims.file_format = v


def _set_fps(sc, vl, v):
    sc.render.fps = int(v)
    sc.render.fps_base = 1.0


SETTINGS = {
    # --- レンダープロパティ ---
    "engine": (G_RENDER, "レンダーエンジン",
               lambda sc, vl: sc.render.engine,
               lambda sc, vl, v: setattr(sc.render, "engine", v)),
    "cycles_samples": (G_RENDER, "サンプル数(Cycles)",
                       lambda sc, vl: sc.cycles.samples,
                       lambda sc, vl, v: setattr(sc.cycles, "samples",
                                                 int(v))),
    "cycles_denoise": (G_RENDER, "デノイズ",
                       lambda sc, vl: sc.cycles.use_denoising,
                       lambda sc, vl, v: setattr(sc.cycles, "use_denoising",
                                                 bool(v))),
    # --- フィルム ---
    "filter_size": (G_FILM, "ピクセルフィルター幅",
                    lambda sc, vl: round(sc.render.filter_size, 3),
                    lambda sc, vl, v: setattr(sc.render, "filter_size",
                                              float(v))),
    "film_transparent": (G_FILM, "透過(アルファ)",
                         lambda sc, vl: sc.render.film_transparent,
                         lambda sc, vl, v: setattr(sc.render,
                                                   "film_transparent",
                                                   bool(v))),
    # --- 出力プロパティ ---
    "resolution_x": (G_OUTPUT, "解像度 幅",
                     lambda sc, vl: sc.render.resolution_x,
                     lambda sc, vl, v: setattr(sc.render, "resolution_x",
                                               int(v))),
    "resolution_y": (G_OUTPUT, "解像度 高さ",
                     lambda sc, vl: sc.render.resolution_y,
                     lambda sc, vl, v: setattr(sc.render, "resolution_y",
                                               int(v))),
    "resolution_percentage": (G_OUTPUT, "解像度 %",
                              lambda sc, vl: sc.render.resolution_percentage,
                              lambda sc, vl, v: setattr(
                                  sc.render, "resolution_percentage", int(v))),
    "fps": (G_OUTPUT, "fps",
            lambda sc, vl: round(sc.render.fps / sc.render.fps_base),
            _set_fps),
    "output_format": (G_OUTPUT, "出力形式", _get_output_format,
                      _set_output_format),
    "color_depth": (G_OUTPUT, "色深度(bit)",
                    lambda sc, vl: sc.render.image_settings.color_depth,
                    lambda sc, vl, v: setattr(sc.render.image_settings,
                                              "color_depth", str(v))),
    "exr_codec": (G_OUTPUT, "EXR圧縮",
                  lambda sc, vl: sc.render.image_settings.exr_codec,
                  lambda sc, vl, v: setattr(sc.render.image_settings,
                                            "exr_codec", v)),
    # --- カラーマネジメント ---
    "view_transform": (G_COLOR, "ビュー変換",
                       lambda sc, vl: sc.view_settings.view_transform,
                       lambda sc, vl, v: setattr(sc.view_settings,
                                                 "view_transform", v)),
    # --- View Layer パス ---
    "pass_combined": (G_PASSES, "統合(Combined)",
                      lambda sc, vl: vl.use_pass_combined,
                      lambda sc, vl, v: setattr(vl, "use_pass_combined",
                                                bool(v))),
    "pass_z": (G_PASSES, "Z",
               lambda sc, vl: vl.use_pass_z,
               lambda sc, vl, v: setattr(vl, "use_pass_z", bool(v))),
    "pass_normal": (G_PASSES, "ノーマル",
                    lambda sc, vl: vl.use_pass_normal,
                    lambda sc, vl, v: setattr(vl, "use_pass_normal", bool(v))),
    "pass_mist": (G_PASSES, "ミスト",
                  lambda sc, vl: vl.use_pass_mist,
                  lambda sc, vl, v: setattr(vl, "use_pass_mist", bool(v))),
    "pass_emit": (G_PASSES, "放射(Emit)",
                  lambda sc, vl: vl.use_pass_emit,
                  lambda sc, vl, v: setattr(vl, "use_pass_emit", bool(v))),
    "crypto_object": (G_PASSES, "Cryptomatte オブジェクト",
                      lambda sc, vl: vl.use_pass_cryptomatte_object,
                      lambda sc, vl, v: setattr(
                          vl, "use_pass_cryptomatte_object", bool(v))),
    "crypto_material": (G_PASSES, "Cryptomatte マテリアル",
                        lambda sc, vl: vl.use_pass_cryptomatte_material,
                        lambda sc, vl, v: setattr(
                            vl, "use_pass_cryptomatte_material", bool(v))),
    "crypto_asset": (G_PASSES, "Cryptomatte アセット",
                     lambda sc, vl: vl.use_pass_cryptomatte_asset,
                     lambda sc, vl, v: setattr(
                         vl, "use_pass_cryptomatte_asset", bool(v))),
    "crypto_depth": (G_PASSES, "Cryptomatte レベル数",
                     lambda sc, vl: vl.pass_cryptomatte_depth,
                     lambda sc, vl, v: setattr(vl, "pass_cryptomatte_depth",
                                               int(v))),
}

# Cyclesのみ意味を持つ項目(他エンジンでは差分・適用の対象外にする)
_CYCLES_ONLY = {"cycles_samples", "cycles_denoise"}


def groups() -> list[str]:
    """レジストリの登場順を保ったグループ一覧。"""
    seen = []
    for group, _label, _g, _s in SETTINGS.values():
        if group not in seen:
            seen.append(group)
    return seen


def capture(scene, view_layer) -> dict:
    """現在のシーン設定をプロファイル(dict)として読み取る。"""
    out = {}
    for key, (_group, _label, getter, _setter) in SETTINGS.items():
        try:
            v = getter(scene, view_layer)
            if isinstance(v, (bool, int, float, str)):
                out[key] = v
        except (AttributeError, TypeError):
            pass  # このBlenderバージョンに無い項目はスキップ
    return out


def apply_profile(profile: dict, scene, view_layer) -> tuple[int, list[str]]:
    """プロファイルをシーンへ適用。(適用数, 失敗項目) を返す。

    engineを最初に適用する(エンジン依存の項目が後に続くため)。
    """
    applied, failed = 0, []
    keys = sorted(profile.keys(), key=lambda k: 0 if k == "engine" else 1)
    for key in keys:
        if key not in SETTINGS:
            continue
        _group, label, _getter, setter = SETTINGS[key]
        if key in _CYCLES_ONLY and profile.get("engine",
                                               scene.render.engine) != 'CYCLES':
            continue
        try:
            setter(scene, view_layer, profile[key])
            applied += 1
        except (AttributeError, TypeError, ValueError) as e:
            failed.append(f"{label}: {e}")
    return applied, failed


def values_equal(a, b) -> bool:
    if isinstance(a, float) or isinstance(b, float):
        try:
            return abs(float(a) - float(b)) < 1e-3
        except (TypeError, ValueError):
            return False
    return a == b


def diff(profile: dict, scene, view_layer) -> list[tuple[str, object, object]]:
    """シーンとプロファイルの差分 [(ラベル, シーン値, 仕様値), ...]。"""
    out = []
    engine = profile.get("engine", scene.render.engine)
    for key, spec_val in profile.items():
        if key not in SETTINGS:
            continue
        if key in _CYCLES_ONLY and engine != 'CYCLES':
            continue
        _group, label, getter, _setter = SETTINGS[key]
        try:
            cur = getter(scene, view_layer)
        except (AttributeError, TypeError):
            continue
        if not values_equal(cur, spec_val):
            out.append((label, cur, spec_val))
    return out


def format_value(v) -> str:
    if isinstance(v, bool):
        return "✓" if v else "─"
    return str(v)
