# SPDX-FileCopyrightText: 2026 mochizukimonta
# SPDX-License-Identifier: GPL-3.0-or-later
"""core — パス安全性・命名規則・構造生成・アトミック置換。

ここは全モジュールが依存するセキュリティ境界なので、
パストラバーサル系は網羅的に潰す。
"""

from __future__ import annotations

import threading
import time
from pathlib import Path

import pytest

from musubi_pipeline import core
from musubi_pipeline.core import PipelineError


# --- 命名規則 -------------------------------------------------------------

def test_scene_and_cut_names():
    assert core.scene_name(1) == "scene01"
    assert core.scene_name(120) == "scene120"
    assert core.cut_name(7) == "c07"


@pytest.mark.parametrize("n", [0, -1, 1000])
def test_scene_name_out_of_range(n):
    with pytest.raises(PipelineError):
        core.scene_name(n)


@pytest.mark.parametrize("n", [0, -1, 1000])
def test_cut_name_out_of_range(n):
    with pytest.raises(PipelineError):
        core.cut_name(n)


# --- パス安全性(セキュリティ境界) ---------------------------------------

def test_safe_path_allows_inside(tmp_path):
    got = core.safe_path(tmp_path, "assets", "char", "hero.blend")
    assert str(got).startswith(str(tmp_path.resolve()))


@pytest.mark.parametrize("parts", [
    ("..",),
    ("..", "..", "etc", "passwd"),
    ("assets", "..", "..", "secret"),
])
def test_safe_path_blocks_traversal(tmp_path, parts):
    with pytest.raises(PipelineError):
        core.safe_path(tmp_path, *parts)


def test_safe_path_blocks_absolute_escape(tmp_path):
    # 絶対パス片を joinpath するとルート外に飛ぶ → 拒否されるべき
    outside = str(Path(tmp_path.anchor or "/") / "windows" / "system32")
    with pytest.raises(PipelineError):
        core.safe_path(tmp_path, outside)


def test_resolve_root_rejects_empty():
    with pytest.raises(PipelineError):
        core.resolve_root("   ")


def test_resolve_root_rejects_missing(tmp_path):
    with pytest.raises(PipelineError):
        core.resolve_root(str(tmp_path / "does-not-exist"))


# --- 構造生成 -------------------------------------------------------------

def test_create_structure_makes_dirs(tmp_path):
    created = core.create_structure(str(tmp_path), template="film")
    assert (tmp_path / ".musubi").is_dir()
    assert (tmp_path / "scenes" / "scene01").is_dir()
    assert (tmp_path / "output").is_dir()
    assert created  # 何か作った


def test_create_structure_idempotent(tmp_path):
    core.create_structure(str(tmp_path), template="film")
    second = core.create_structure(str(tmp_path), template="film")
    assert second == []  # 2回目は何も作らない


def test_create_structure_unknown_template(tmp_path):
    with pytest.raises(PipelineError):
        core.create_structure(str(tmp_path), template="bogus")


# --- カット/出力パスの往復 ------------------------------------------------

def test_parse_cut_path_roundtrip(tmp_path):
    core.create_structure(str(tmp_path), template="film")
    blend = core.cut_blend_path(str(tmp_path), 3, 12)
    assert core.parse_cut_path(tmp_path, str(blend)) == (3, 12)


def test_parse_cut_path_rejects_non_cut(tmp_path):
    core.create_structure(str(tmp_path), template="film")
    other = tmp_path / "assets" / "char" / "hero.blend"
    assert core.parse_cut_path(tmp_path, str(other)) is None


def test_detect_root_finds_musubi(tmp_path):
    core.create_structure(str(tmp_path), template="film")
    blend = core.cut_blend_path(str(tmp_path), 1, 1)
    blend.write_bytes(b"x")
    assert core.detect_root(str(blend)) == tmp_path.resolve()


def test_detect_root_none_outside(tmp_path):
    lone = tmp_path / "lone.blend"
    lone.write_bytes(b"x")
    assert core.detect_root(str(lone)) is None


def test_next_output_path_increments(tmp_path):
    core.create_structure(str(tmp_path), template="film")
    p1 = core.next_output_path(str(tmp_path), 1, 1)
    assert p1.name == "c01.001.mp4"
    p1.write_bytes(b"video")
    p2 = core.next_output_path(str(tmp_path), 1, 1)
    assert p2.name == "c01.002.mp4"


# --- アトミック置換のリトライ(同期ツールの共有違反対策) -----------------

def test_atomic_replace_basic(tmp_path):
    tmp = tmp_path / "a.tmp"
    dst = tmp_path / "a.txt"
    tmp.write_text("new")
    core.atomic_replace(tmp, dst)
    assert dst.read_text() == "new"
    assert not tmp.exists()


def test_atomic_replace_retries_then_succeeds(tmp_path, monkeypatch):
    """最初の数回 PermissionError を返しても、最終的に置換が成功する。"""
    calls = {"n": 0}
    real_replace = __import__("os").replace

    def flaky(src, dst):
        calls["n"] += 1
        if calls["n"] < 3:
            raise PermissionError("share violation (simulated)")
        return real_replace(src, dst)

    monkeypatch.setattr(core.os, "replace", flaky)
    tmp = tmp_path / "b.tmp"
    dst = tmp_path / "b.txt"
    tmp.write_text("payload")
    core.atomic_replace(tmp, dst, retries=5, delay=0.001)
    assert dst.read_text() == "payload"
    assert calls["n"] == 3


def test_atomic_replace_gives_up_after_retries(tmp_path, monkeypatch):
    def always_fail(src, dst):
        raise PermissionError("locked forever")

    monkeypatch.setattr(core.os, "replace", always_fail)
    tmp = tmp_path / "c.tmp"
    tmp.write_text("x")
    with pytest.raises(PermissionError):
        core.atomic_replace(tmp, tmp_path / "c.txt", retries=3, delay=0.001)
