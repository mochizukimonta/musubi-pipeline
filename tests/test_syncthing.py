# SPDX-FileCopyrightText: 2026 mochizukimonta
# SPDX-License-Identifier: GPL-3.0-or-later
"""syncthing — 純関数(モック不要)と REST 層(api() 差し替え)。

ネットワークには一切触れない。REST を叩く関数は syncthing.api を偽物に
差し替え、呼ばれた method/path/payload を検査する。
"""

from __future__ import annotations

import pytest

from musubi_pipeline import syncthing as st
from musubi_pipeline.core import PipelineError


# ==========================================================================
# 純関数(モック不要・今すぐ書ける)
# ==========================================================================

VALID_DEV = "ABCDEF7-GHIJKLM-NOPQRS7-TUVWXY7-234567A-BCDEFG7-HIJKLM7-NOPQR77"


def test_normalize_device_id_strips_and_uppercases():
    raw = valid_lower = VALID_DEV.lower().replace("-", " - ")
    assert st.normalize_device_id(raw) == VALID_DEV


def test_normalize_device_id_rejects_garbage():
    with pytest.raises(PipelineError):
        st.normalize_device_id("not-a-device-id")


def test_validate_sync_id_ok():
    assert st.validate_sync_id("  Forest-150 ") == "forest-150"


@pytest.mark.parametrize("bad", ["ab", "-lead", "UPPER caps space", "x" * 60])
def test_validate_sync_id_rejects(bad):
    with pytest.raises(PipelineError):
        st.validate_sync_id(bad)


def test_derive_sync_id():
    assert st.derive_sync_id("Forest 150") == "forest-150"
    assert st.derive_sync_id("my__project!!") == "my-project"


def test_derive_sync_id_fallback_when_empty():
    assert st.derive_sync_id("!!") == "musubi-project"


def test_make_invite_parse_roundtrip():
    invite = st.make_invite("forest-150", VALID_DEV, "森のプロジェクト", "リーダー")
    # dict をそのまま JSON 文字列にして読み戻す
    import json
    parsed = st.parse_invite(json.dumps(invite))
    assert parsed["sync_id"] == "forest-150"
    assert parsed["leader_device_id"] == VALID_DEV
    assert parsed["project_name"] == "森のプロジェクト"


def test_parse_invite_rejects_non_json():
    with pytest.raises(PipelineError, match="読めません"):
        st.parse_invite("this is not json")


def test_parse_invite_rejects_wrong_type():
    import json
    with pytest.raises(PipelineError, match="招待ファイルではありません"):
        st.parse_invite(json.dumps({"type": "something-else", "format": 1}))


def test_parse_invite_rejects_bad_device_id():
    import json
    payload = {"type": "musubi-invite", "format": 1,
               "sync_id": "forest-150", "leader_device_id": "BROKEN"}
    with pytest.raises(PipelineError):
        st.parse_invite(json.dumps(payload))


# ==========================================================================
# REST 層(api() を偽物に差し替え)
# ==========================================================================

class FakeApi:
    """method/path/payload を記録し、GET には既定の応答を返す偽 api()。"""

    def __init__(self, responses=None):
        self.responses = responses or {}
        self.calls = []

    def __call__(self, method, path, payload=None, home=None, **kw):
        self.calls.append((method, path, payload))
        if method == "GET":
            # 完全一致 → 前方一致の順で応答を探す
            if path in self.responses:
                return self.responses[path]
            for key, val in self.responses.items():
                if path.startswith(key):
                    return val
            return []
        return {}

    def calls_of(self, method):
        return [c for c in self.calls if c[0] == method]


@pytest.fixture
def fake_api(monkeypatch):
    def _install(responses=None):
        fa = FakeApi(responses)
        monkeypatch.setattr(st, "api", fa)
        return fa
    return _install


def test_add_peer_registers_new_device(fake_api, monkeypatch):
    # フォルダ共有の副作用(_share_folder_with)は別途検証するのでダミー化
    monkeypatch.setattr(st, "_share_folder_with", lambda *a, **k: None)
    fa = fake_api({"/rest/config/devices": []})   # 既存デバイスなし
    st.add_peer(VALID_DEV, "太郎", "forest-150")
    posts = fa.calls_of("POST")
    assert any(p[1] == "/rest/config/devices" for p in posts)
    # 自動フォルダ受諾は無効(セキュリティ既定)
    dev_post = next(p for p in posts if p[1] == "/rest/config/devices")
    assert dev_post[2]["autoAcceptFolders"] is False


def test_add_peer_skips_existing_device(fake_api, monkeypatch):
    monkeypatch.setattr(st, "_share_folder_with", lambda *a, **k: None)
    fa = fake_api({"/rest/config/devices": [{"deviceID": VALID_DEV}]})
    st.add_peer(VALID_DEV, "太郎", "forest-150")
    # 既に居るので新規 POST しない
    assert not any(p[1] == "/rest/config/devices" for p in fa.calls_of("POST"))


def test_remove_device_returns_false_when_absent(fake_api):
    fa = fake_api({
        "/rest/config/folders": [],
        "/rest/config/devices": [],   # そのデバイスは居ない
    })
    assert st.remove_device(VALID_DEV) is False
    # 居ないので DELETE は呼ばれない
    assert not fa.calls_of("DELETE")


def test_remove_device_deletes_when_present(fake_api):
    fa = fake_api({
        "/rest/config/folders": [],
        "/rest/config/devices": [{"deviceID": VALID_DEV}],
    })
    assert st.remove_device(VALID_DEV) is True
    assert any(c[1].startswith("/rest/config/devices/")
               for c in fa.calls_of("DELETE"))


def test_remove_folder_returns_false_when_absent(fake_api):
    fa = fake_api({"/rest/config/folders": [{"id": "other-proj"}]})
    assert st.remove_folder("forest-150") is False


def test_device_folders_lists_shared(fake_api):
    fake_api({"/rest/config/folders": [
        {"id": "forest-150", "devices": [{"deviceID": VALID_DEV}]},
        {"id": "other", "devices": []},
    ]})
    assert st.device_folders(VALID_DEV) == ["forest-150"]


# --- ensure_folder のパス相乗り防止(v0.25 セキュリティ修正) --------------

def test_ensure_folder_rejects_path_mismatch(fake_api, tmp_path):
    """同じ同期IDが別ディレクトリに登録済みなら中止する。

    同名の別プロジェクトが過去の共有設定に相乗りして、旧プロジェクトの
    ファイルが流れる事故を防ぐための境界。
    """
    registered = tmp_path / "old-project"
    registered.mkdir()
    current = tmp_path / "new-project"
    current.mkdir()
    fake_api({"/rest/config/folders": [
        {"id": "forest-150", "path": str(registered)},
    ]})
    with pytest.raises(PipelineError, match="別のフォルダ"):
        st.ensure_folder("forest-150", current)


def test_ensure_folder_ok_when_path_matches(fake_api, monkeypatch, tmp_path):
    """登録済みパスが現ルートと一致するなら中止せず、重複登録もしない。"""
    monkeypatch.setattr(st, "write_stignore", lambda *a, **k: False)
    monkeypatch.setattr(st, "accept_pending_folder_offers", lambda *a, **k: [])
    current = tmp_path / "proj"
    current.mkdir()
    fa = fake_api({"/rest/config/folders": [
        {"id": "forest-150", "path": str(current)},
    ]})
    st.ensure_folder("forest-150", current)   # 例外が出なければ合格
    # 既存なので新規フォルダ POST はしない
    assert not any(c[1] == "/rest/config/folders" for c in fa.calls_of("POST"))


# --- accept_pending(only=...) の確認画面バイパス防止(TOCTOU) ----------

# only テスト用に有効なデバイスIDを 3 つ用意する([A-Z2-7]{7}×8ブロック)
DEV_A = "AAAAAA2-AAAAAA2-AAAAAA2-AAAAAA2-AAAAAA2-AAAAAA2-AAAAAA2-AAAAAA2"
DEV_B = "BBBBBB3-BBBBBB3-BBBBBB3-BBBBBB3-BBBBBB3-BBBBBB3-BBBBBB3-BBBBBB3"
DEV_C = "CCCCCC4-CCCCCC4-CCCCCC4-CCCCCC4-CCCCCC4-CCCCCC4-CCCCCC4-CCCCCC4"


def test_accept_pending_only_excludes_others(fake_api, monkeypatch):
    """pending に A,B,C いても only=[A,B] なら C は絶対に承認されない。

    確認画面に出したデバイスだけを承認する境界(表示後に接続してきた
    未確認デバイスの巻き込み承認を防ぐ)。
    """
    fake_api({"/rest/cluster/pending/devices": {
        DEV_A: {"name": "a"}, DEV_B: {"name": "b"}, DEV_C: {"name": "c"},
    }})
    approved = []
    monkeypatch.setattr(st, "add_peer",
                        lambda did, name, sync_id, **k: approved.append(did))
    monkeypatch.setattr(st, "accept_pending_folder_offers",
                        lambda *a, **k: [])

    result = st.accept_pending("forest-150", only=[DEV_A, DEV_B])

    assert set(result) == {DEV_A, DEV_B}
    assert DEV_C not in result
    assert DEV_C not in approved   # add_peer にも渡っていない


def test_accept_pending_without_only_accepts_all(fake_api, monkeypatch):
    """only=None は従来どおり全 pending を承認(退行の有無を対比で固定)。"""
    fake_api({"/rest/cluster/pending/devices": {
        DEV_A: {"name": "a"}, DEV_B: {"name": "b"},
    }})
    approved = []
    monkeypatch.setattr(st, "add_peer",
                        lambda did, name, sync_id, **k: approved.append(did))
    monkeypatch.setattr(st, "accept_pending_folder_offers",
                        lambda *a, **k: [])
    result = st.accept_pending("forest-150")
    assert set(result) == {DEV_A, DEV_B}


# --- 一時停止(解除の代わりに使う可逆な操作) -----------------------------

def test_pause_folder_patches_only_paused(fake_api):
    """paused だけを部分更新する。

    設定全体を読み書きすると、その間に他の設定を失う余地がある。
    PATCH で1項目だけ触ることを固定する。
    """
    fa = fake_api({"/rest/config/folders": [{"id": "forest-150"}]})
    assert st.pause_folder("forest-150", True) is True
    patches = fa.calls_of("PATCH")
    assert len(patches) == 1
    assert patches[0][1] == "/rest/config/folders/forest-150"
    assert patches[0][2] == {"paused": True}
    # 解除(DELETE)は絶対に起きない。一時停止は可逆な操作である
    assert not fa.calls_of("DELETE")


def test_pause_folder_resume_sends_false(fake_api):
    """再開は paused=False を送る(同じ入口で往復できる)。"""
    fa = fake_api({"/rest/config/folders": [{"id": "forest-150"}]})
    assert st.pause_folder("forest-150", False) is True
    assert fa.calls_of("PATCH")[0][2] == {"paused": False}


def test_pause_folder_unknown_id_does_nothing(fake_api):
    """登録されていないIDでは何も送らず False を返す。"""
    fa = fake_api({"/rest/config/folders": [{"id": "other"}]})
    assert st.pause_folder("forest-150", True) is False
    assert not fa.calls_of("PATCH")


def test_pause_folder_validates_id(fake_api):
    """同期IDの検証を通す(不正なIDでURLを組ませない)。"""
    fake_api({"/rest/config/folders": []})
    with pytest.raises(PipelineError):
        st.pause_folder("../../etc", True)


# --- 共有一覧(1プロジェクト1枠の表示に渡すデータ) -----------------------

def test_folder_detail_maps_state_and_progress(fake_api):
    """状態は日本語に、進捗とファイル数はそのまま渡す。"""
    fake_api({
        "/rest/db/status": {"state": "syncing", "needBytes": 12_400_000,
                            "needTotalItems": 7, "localFiles": 1204},
        "/rest/folder/errors": {"errors": [{"path": "a/b.blend"}]},
    })
    d = st.folder_detail("forest-150")
    assert d["state"] == "同期中(転送しています)"
    assert d["need_bytes"] == 12_400_000
    assert d["need_items"] == 7
    assert d["files"] == 1204
    assert d["errors"] == 1
    assert d["error_example"] == "a/b.blend"


def test_folder_detail_survives_missing_fields(fake_api):
    """応答が欠けていても既定値で返す(表示を止めない)。"""
    fake_api({"/rest/db/status": {}})
    d = st.folder_detail("forest-150")
    assert d["need_bytes"] == 0 and d["files"] == 0 and d["errors"] == 0


def test_status_report_collects_every_folder(fake_api, monkeypatch):
    """全プロジェクトが同じ形で folders に並び、現在の1件に印が付く。"""
    monkeypatch.setattr(st, "is_installed", lambda: True)
    monkeypatch.setattr(st, "ping", lambda home=None: True)
    monkeypatch.setattr(st, "recent_activity", lambda home=None: [])
    fake_api({
        "/rest/system/status": {"myID": DEV_A, "uptime": 60},
        "/rest/config/devices": [{"deviceID": DEV_B, "name": "pc-B"}],
        "/rest/system/connections": {"connections": {DEV_B: {"connected": True}}},
        "/rest/config/folders": [
            {"id": "forest-150", "path": "/p/cur",
             "devices": [{"deviceID": DEV_B}]},
            {"id": "other-proj", "path": "/p/other", "paused": True,
             "devices": [{"deviceID": DEV_B}]},
        ],
        "/rest/db/status": {"state": "idle", "localFiles": 10},
        "/rest/folder/errors": {"errors": []},
        "/rest/cluster/pending/folders": {},
        "/rest/cluster/pending/devices": {},
    })
    rep = st.status_report("forest-150")
    ids = {f["id"]: f for f in rep["folders"]}
    assert set(ids) == {"forest-150", "other-proj"}
    assert ids["forest-150"]["current"] is True
    assert ids["other-proj"]["current"] is False
    assert ids["other-proj"]["paused"] is True
    assert ids["forest-150"]["devices"] == ["pc-B"]


def test_status_report_does_not_warn_about_other_projects(fake_api,
                                                         monkeypatch):
    """他プロジェクトの共有は警告にしない。

    複数の現場に出入りする使い方では、他プロジェクトの共有は正常な状態。
    赤い警告を出すと、本当に対処が必要な問題が埋もれる。
    """
    monkeypatch.setattr(st, "is_installed", lambda: True)
    monkeypatch.setattr(st, "ping", lambda home=None: True)
    monkeypatch.setattr(st, "recent_activity", lambda home=None: [])
    fake_api({
        "/rest/system/status": {"myID": DEV_A, "uptime": 60},
        "/rest/config/devices": [{"deviceID": DEV_B, "name": "pc-B"}],
        "/rest/system/connections": {"connections": {DEV_B: {"connected": True}}},
        "/rest/config/folders": [
            {"id": "forest-150", "path": "/p/cur",
             "devices": [{"deviceID": DEV_B}]},
            {"id": "other-a", "path": "/p/a", "devices": [{"deviceID": DEV_B}]},
            {"id": "other-b", "path": "/p/b", "devices": [{"deviceID": DEV_B}]},
        ],
        "/rest/db/status": {"state": "idle", "localFiles": 10},
        "/rest/folder/errors": {"errors": []},
        "/rest/cluster/pending/folders": {},
        "/rest/cluster/pending/devices": {},
    })
    rep = st.status_report("forest-150")
    assert not any("別プロジェクト" in p for p in rep["problems"])
    # 一覧には出る(件数と状態は枠で示す)
    assert len(rep["folders"]) == 3


def test_status_report_paused_folders_skip_detail_calls(fake_api, monkeypatch):
    """停止中のフォルダには状態を問い合わせない(無駄な呼び出しを増やさない)。"""
    monkeypatch.setattr(st, "is_installed", lambda: True)
    monkeypatch.setattr(st, "ping", lambda home=None: True)
    monkeypatch.setattr(st, "recent_activity", lambda home=None: [])
    fa = fake_api({
        "/rest/system/status": {"myID": DEV_A, "uptime": 60},
        "/rest/config/devices": [],
        "/rest/system/connections": {"connections": {}},
        "/rest/config/folders": [
            {"id": "forest-150", "path": "/p/cur", "devices": []},
            {"id": "stopped-1", "path": "/p/s1", "paused": True, "devices": []},
            {"id": "stopped-2", "path": "/p/s2", "paused": True, "devices": []},
        ],
        "/rest/db/status": {"state": "idle", "localFiles": 1},
        "/rest/folder/errors": {"errors": []},
        "/rest/cluster/pending/folders": {},
        "/rest/cluster/pending/devices": {},
    })
    st.status_report("forest-150")
    asked = [c[1] for c in fa.calls_of("GET") if "/rest/db/status" in c[1]]
    assert len(asked) == 1                    # 稼働中の1件だけ
    assert "folder=forest-150" in asked[0]
