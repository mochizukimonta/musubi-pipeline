# SPDX-FileCopyrightText: 2026 mochizukimonta
# SPDX-License-Identifier: GPL-3.0-or-later
"""共通フィクスチャ。

方針:
- 本物のディスク I/O で検証する(このコードの仕事はファイル操作そのもの
  なので、ファイルシステムをモックすると意味が薄い)。毎回 tmp_path の
  使い捨てフォルダを使うので、demo_project/ など実プロジェクトは汚さない。
- 端末名(host_id)は socket.gethostname を差し替えて偽装する。
  tasks / versions は `from .sync import host_id` と名前を直接 import して
  いるため、sync.host_id を setattr しても各モジュール内のコピーには効かない。
  host_id() は毎回 socket.gethostname() を呼ぶので、そこを差し替えるのが確実。
"""

from __future__ import annotations

import socket
import time
from pathlib import Path

import pytest

from musubi_pipeline import core


@pytest.fixture
def project(tmp_path) -> str:
    """使い捨てプロジェクト。ルートの str パスを返す。"""
    core.create_structure(str(tmp_path), template="film")
    return str(tmp_path)


@pytest.fixture
def as_host(monkeypatch):
    """host_id() が返す端末名を任意に切り替えるフィクスチャ。

    使い方: as_host("pc-A") のように呼ぶ。
    """
    def _set(name: str):
        monkeypatch.setattr(socket, "gethostname", lambda: name)
    return _set


@pytest.fixture
def freeze_time(monkeypatch):
    """time.strftime のタイムスタンプを固定する(同一秒衝突などの再現用)。

    使い方: freeze_time("20260720-120000")。%H%M%S を含む書式のときだけ
    固定文字列を返し、それ以外(タイムゾーン等)は本物に委ねる。
    """
    real_strftime = time.strftime

    def _set(stamp: str):
        def fake(fmt, *a):
            if fmt == "%Y%m%d-%H%M%S":
                return stamp
            return real_strftime(fmt, *a)
        monkeypatch.setattr(time, "strftime", fake)
    return _set


def make_blend(project: str, scene: str = "scene01", cut: str = "c01",
               data: bytes = b"BLENDER-v1") -> Path:
    """scenes/<scene>/<cut>.blend を作って Path を返すヘルパ。"""
    p = Path(project) / "scenes" / scene / f"{cut}.blend"
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_bytes(data)
    return p
