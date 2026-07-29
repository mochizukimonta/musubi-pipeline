# SPDX-FileCopyrightText: 2026 mochizukimonta
# SPDX-License-Identifier: GPL-3.0-or-later
"""配布メタデータの不変条件。

バージョンやライセンスは `blender_manifest.toml` と `bl_info` の2箇所に
書かれている。人間の注意力に頼ると必ずズレるので、ここで機械的に固定する。
片方だけ直したらこのテストが赤くなる。

外部に名乗る User-Agent もここで縛る。ネットワーク越しに出ていく申告が
実体とズレると、相手のログが当てにならなくなる(嘘の版を名乗るのは、
版を名乗らないより悪い)。
"""

from __future__ import annotations

import tomllib
from pathlib import Path

import pytest

from musubi_pipeline import bl_info
from musubi_pipeline.syncthing import USER_AGENT

REPO_ROOT = Path(__file__).resolve().parents[1]
PKG_DIR = REPO_ROOT / "musubi_pipeline"
MANIFEST_PATH = PKG_DIR / "blender_manifest.toml"


@pytest.fixture(scope="module")
def manifest() -> dict:
    with open(MANIFEST_PATH, "rb") as f:
        return tomllib.load(f)


def test_manifest_exists_inside_package():
    """manifest はパッケージフォルダ内にある。

    このフォルダがそのまま配布zipの中身になるため、位置がずれると
    `blender --command extension build` が別物を梱包してしまう。
    """
    assert MANIFEST_PATH.is_file()


def test_version_matches_bl_info(manifest):
    """manifest の version と bl_info の version が一致する。"""
    assert manifest["version"] == ".".join(str(n) for n in bl_info["version"])


def test_user_agent_reports_the_real_version(manifest):
    """外部に名乗る版が、配布物の実際の版と一致する。

    User-Agent はハードコードすると更新を忘れる(v0.2 の値が v0.26 まで
    残っていた)。GitHub 側のログでどの版が問題を起こしているか特定できる
    ことが目的なので、実体とズレたら意味がない。
    """
    assert USER_AGENT.startswith(f"musubi-pipeline/{manifest['version']} ")


def test_user_agent_carries_contact_url(manifest):
    """連絡先URLを含み、manifest の website と一致する。

    自動化されたクライアントは連絡先を名乗るのが作法で、素性の分からない
    User-Agent を拒否するサービスもある。
    """
    assert f"(+{manifest['website']})" in USER_AGENT


def test_blender_min_matches_bl_info(manifest):
    """対応Blenderバージョンの下限が2箇所で一致する。"""
    major, minor = bl_info["blender"][:2]
    assert manifest["blender_version_min"].startswith(f"{major}.{minor}")


def test_license_is_gpl(manifest):
    """ライセンスは GPL-3.0-or-later。

    選択の余地はない: bpy を import する Blender の派生著作物であり、
    extensions.blender.org も GPL-3.0-or-later 準拠を要求している。
    MIT に戻すと配布できなくなる。
    """
    assert manifest["license"] == ["SPDX:GPL-3.0-or-later"]


def test_copyright_present(manifest):
    """GPL は著作権表示を要求する。書式は "年 名前"。"""
    assert manifest["copyright"]
    for entry in manifest["copyright"]:
        year = entry.split()[0].split("-")[0]
        assert year.isdigit() and len(year) == 4, entry


def test_id_matches_package_dir(manifest):
    """extension の id は Python のモジュール名と一致する必要がある。"""
    assert manifest["id"] == PKG_DIR.name
    assert "-" not in manifest["id"]  # モジュール名にハイフンは使えない


def test_license_text_bundled_and_identical():
    """GPL 全文が配布zipにも同梱され、リポジトリ直下のものと同一である。

    GPL はライセンス全文の頒布を条件にしているので、パッケージ内の
    コピーが古びていないことを確認する。
    """
    root_license = (REPO_ROOT / "LICENSE").read_bytes()
    pkg_license = (PKG_DIR / "LICENSE").read_bytes()
    assert root_license == pkg_license
    assert b"GNU GENERAL PUBLIC LICENSE" in root_license
    assert b"Version 3" in root_license


def test_all_sources_carry_spdx_header():
    """全 .py に SPDX ヘッダーがある(ライセンスの出所を機械可読にする)。"""
    missing = [
        p.relative_to(REPO_ROOT).as_posix()
        for p in sorted(REPO_ROOT.rglob("*.py"))
        if "SPDX-License-Identifier: GPL-3.0-or-later"
        not in p.read_text(encoding="utf-8")
    ]
    assert not missing, f"SPDXヘッダーがないファイル: {missing}"


def test_build_excludes_bytecode(manifest):
    """配布zipに __pycache__ / .pyc を混ぜない。"""
    patterns = manifest["build"]["paths_exclude_pattern"]
    assert "__pycache__/" in patterns
    assert "*.pyc" in patterns
