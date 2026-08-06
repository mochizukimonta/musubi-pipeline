# SPDX-FileCopyrightText: 2026 mochizukimonta
# SPDX-License-Identifier: GPL-3.0-or-later
"""musubi_pipeline.syncthing — Syncthing の自動セットアップと制御。

各端末での導入手順を「ボタン一発 + リーダーのデバイスID貼り付け」まで
自動化する。Blender非依存(bpyを使わない)。

セキュリティ設計:
- バイナリは公式 GitHub リリースから HTTPS で取得し、同リリースの
  sha256sum.txt.asc に記載のハッシュと照合してから展開する
  (改ざん・破損した実行ファイルの混入防止)
- 展開時は zip 内のパスを検証(Zip Slip 対策)し、syncthing.exe のみ取り出す
- REST API は 127.0.0.1 のみで待ち受け、APIキーは Blender設定とは別の
  ローカルフォルダ(同期対象外)に置く。同期フォルダには秘密を書かない
- デバイスIDは形式検証してから登録する
- 新規デバイスの参加は必ず人間の「承認」操作を要求する(自動承認しない)
"""

from __future__ import annotations

import json
import os
import re
import shutil
import socket
import ssl
import subprocess
import time
import urllib.error
import urllib.request
import zipfile
from hashlib import sha256
from pathlib import Path
from xml.etree import ElementTree

from . import bl_info
from .core import PipelineError, local_root

GITHUB_LATEST = "https://api.github.com/repos/syncthing/syncthing/releases/latest"
ALLOWED_HOSTS = ("api.github.com", "github.com", "objects.githubusercontent.com")

# HTTPで名乗る文字列。GitHub は空でない User-Agent を要求する(無いと403)。
# 版と連絡先を入れるのは、相手の運営者が「どの版が問題を起こしているか」を
# 特定できるようにするため。版が分からないと、不具合のある版だけでなく
# 製品名ごとブロックするしか手がなくなる。
#
# 版は手書きせず bl_info から生成する。手書きの定数は必ず取り残される
# (実際 v0.2 のときに書いた "0.2" が v0.26 まで放置されていた)。
# 連絡先URLは manifest の website と一致することを tests/test_manifest.py が検証。
WEBSITE = "https://github.com/mochizukimonta/musubi-pipeline"
VERSION = ".".join(str(n) for n in bl_info["version"])
USER_AGENT = f"musubi-pipeline/{VERSION} (+{WEBSITE})"

DEVICE_ID_RE = re.compile(r"^[A-Z2-7]{7}(-[A-Z2-7]{7}){7}$")
SYNC_ID_RE = re.compile(r"^[a-z0-9][a-z0-9\-]{2,49}$")

# ユーザーが個人利用しているSyncthing(既定 127.0.0.1:8384)と共存できるよう、
# アドオン専用インスタンスの制御APIは別ポートを使う(ループバック限定は不変)
DEFAULT_PORT = 28384
DEFAULT_GUI = f"127.0.0.1:{DEFAULT_PORT}"


def gui_addr(port: int) -> str:
    """制御APIのポート番号からGUIアドレスを組み立てる(ホストはループバック固定)。"""
    port = int(port)
    if not 1024 <= port <= 65535:
        raise PipelineError("ポート番号は1024〜65535にしてください")
    return f"127.0.0.1:{port}"


def install_root() -> Path:
    """Syncthing の実行ファイルと設定を置く場所(同期対象外のローカル領域)。

    ここに Syncthing のデバイス秘密鍵(端末の身分証)が入るため、
    このフォルダを消すと他端末とのペアリングがやり直しになる。
    """
    return local_root() / "syncthing"


def binary_path() -> Path:
    return install_root() / "bin" / "syncthing.exe"


def home_dir() -> Path:
    return install_root() / "home"


def is_installed() -> bool:
    return binary_path().exists()


# ---------------------------------------------------------------------------
# ダウンロード(SHA-256検証付き)
# ---------------------------------------------------------------------------

def _ssl_context() -> ssl.SSLContext:
    """OSが信頼するCAでTLS検証するコンテキストを作る(検証は常に有効)。

    Blender同梱のPythonはWindowsの証明書ストアを読み込めないことがあり、
    さらに社内プロキシやアンチウイルスがHTTPSを傍受している環境では、
    それらのルート証明書がPythonの既定トラストに入っておらず
    CERTIFICATE_VERIFY_FAILED になる。ブラウザが使うのと同じ
    Windowsのシステム証明書ストア(ROOT/CA。社内・AVのルートもここに入る)を
    明示的に取り込むことで、ブラウザで開けるサイトは検証を通せるようにする。
    """
    ctx = ssl.create_default_context()
    server_auth_oid = "1.3.6.1.5.5.7.3.1"  # TLSサーバ認証のEKU
    if os.name == "nt":
        for store in ("ROOT", "CA"):
            try:
                for cert, encoding, trust in ssl.enum_certificates(store):
                    if encoding != "x509_asn":
                        continue
                    # Windowsが「サーバ認証」用途で信頼している証明書だけ採用する。
                    # trust==True(全用途)か、EKUにserverAuthを含むもののみ。
                    # コード署名やメール等、別用途だけの証明書をTLS検証の
                    # トラストアンカーに混ぜない(過剰な信頼付与を防ぐ)。
                    if trust is True or (
                            isinstance(trust, (set, frozenset, tuple, list))
                            and server_auth_oid in trust):
                        try:
                            ctx.load_verify_locations(
                                cadata=ssl.DER_cert_to_PEM_cert(cert))
                        except ssl.SSLError:
                            pass
            except Exception:
                pass
    # certifi があれば併用(Blenderにpipでインストールされているケースやmac/Linux)
    try:
        import certifi
        ctx.load_verify_locations(cafile=certifi.where())
    except Exception:
        pass
    return ctx


def _fetch(url: str, timeout: int = 60) -> bytes:
    from urllib.parse import urlparse
    parsed = urlparse(url)
    if parsed.scheme != "https" or parsed.hostname not in ALLOWED_HOSTS:
        raise PipelineError(f"許可されていないダウンロード元: {url}")
    req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
    try:
        with urllib.request.urlopen(req, timeout=timeout,
                                    context=_ssl_context()) as r:
            return r.read()
    except urllib.error.URLError as e:
        reason = getattr(e, "reason", e)
        if isinstance(reason, ssl.SSLCertVerificationError):
            raise PipelineError(
                "HTTPSの証明書を検証できませんでした。社内プロキシやアンチウイルスが"
                "通信を傍受している環境で起こります。対処:(1)会社支給PCなら"
                "IT管理者に確認、(2)手動でsyncthing.exeを配置してください"
                "(READMEの「手動でSyncthingを用意する」参照)。"
                f"詳細: {reason}") from None
        raise PipelineError(
            f"ダウンロードに失敗しました(ネットワークを確認してください): {reason}"
        ) from None


def download_binary(log=print) -> dict:
    """公式リリースから syncthing.exe を検証付きで取得する。"""
    log("Syncthing 最新リリース情報を取得中…")
    rel = json.loads(_fetch(GITHUB_LATEST))
    tag = rel["tag_name"]  # 例: v1.27.6
    asset_name = f"syncthing-windows-amd64-{tag}.zip"
    assets = {a["name"]: a["browser_download_url"] for a in rel["assets"]}
    if asset_name not in assets or "sha256sum.txt.asc" not in assets:
        raise PipelineError(f"リリース {tag} に期待するアセットがありません")

    log(f"チェックサム(sha256sum.txt.asc)を取得中…")
    sums = _fetch(assets["sha256sum.txt.asc"]).decode("utf-8", "replace")
    expected = None
    for line in sums.splitlines():
        m = re.match(r"^([0-9a-f]{64})\s+\*?(\S+)$", line.strip())
        if m and m.group(2) == asset_name:
            expected = m.group(1)
            break
    if not expected:
        raise PipelineError(f"{asset_name} のチェックサムが見つかりません")

    log(f"{asset_name} をダウンロード中(数十MB)…")
    blob = _fetch(assets[asset_name], timeout=600)
    actual = sha256(blob).hexdigest()
    if actual != expected:
        raise PipelineError(
            f"SHA-256不一致。ダウンロードが改ざん/破損しています "
            f"(expected {expected[:12]}…, got {actual[:12]}…)"
        )
    log("SHA-256 検証OK。展開中…")

    bin_dir = binary_path().parent
    bin_dir.mkdir(parents=True, exist_ok=True)
    tmp_zip = bin_dir / "_download.zip"
    tmp_zip.write_bytes(blob)
    try:
        with zipfile.ZipFile(tmp_zip) as zf:
            member = None
            for name in zf.namelist():
                if ".." in name or name.startswith(("/", "\\")):
                    raise PipelineError(f"zip内に不正なパス: {name}")
                if name.endswith("/syncthing.exe"):
                    member = name
            if member is None:
                raise PipelineError("zip内に syncthing.exe が見つかりません")
            with zf.open(member) as src, open(binary_path(), "wb") as dst:
                shutil.copyfileobj(src, dst)
    finally:
        tmp_zip.unlink(missing_ok=True)

    meta = {"version": tag, "sha256": expected,
            "downloaded_at": time.strftime("%Y-%m-%dT%H:%M:%S")}
    (install_root() / "install.json").write_text(
        json.dumps(meta, indent=1), encoding="utf-8")
    log(f"Syncthing {tag} を導入しました: {binary_path()}")
    return meta


# ---------------------------------------------------------------------------
# 設定生成・起動・停止
# ---------------------------------------------------------------------------

def _run_cli(args: list[str], timeout: int = 60) -> subprocess.CompletedProcess:
    flags = subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0
    return subprocess.run([str(binary_path())] + args, capture_output=True,
                          text=True, timeout=timeout, creationflags=flags)


def ensure_config(home: Path | None = None, gui_addr: str = DEFAULT_GUI) -> Path:
    """設定・鍵ペアを生成(初回のみ)し、GUI/APIを127.0.0.1に固定する。"""
    home = home or home_dir()
    cfg = home / "config.xml"
    if not cfg.exists():
        home.mkdir(parents=True, exist_ok=True)
        # v1系は --no-default-folder あり、v2系は廃止(デフォルトフォルダを作らない)
        r = _run_cli(["generate", "--home", str(home), "--no-default-folder"])
        if r.returncode != 0 and not cfg.exists():
            r = _run_cli(["generate", "--home", str(home)])
        if r.returncode != 0 and not cfg.exists():
            raise PipelineError(f"設定生成に失敗: {r.stderr[-400:]}")
    tree = ElementTree.parse(cfg)
    gui = tree.getroot().find("gui")
    addr_el = gui.find("address")
    if not gui_addr.startswith("127.0.0.1:"):
        raise PipelineError("GUI/APIはループバック以外に公開できません")
    if addr_el.text != gui_addr:
        # ポート変更時、旧ポートで稼働中なら先に停止する。config.xmlだけ
        # 書き換えると稼働中プロセスは旧ポートのままAPI不通になり、
        # 新ポートでの再起動もDBロック(旧プロセスが保持)で失敗して詰むため
        if ping(home):
            shutdown(home)
            deadline = time.time() + 20
            while time.time() < deadline and port_open(home):
                time.sleep(0.5)
            if port_open(home):
                raise PipelineError(
                    "ポート変更のためのSyncthing停止がタイムアウトしました。"
                    "少し待ってからもう一度お試しください")
        addr_el.text = gui_addr
        tree.write(cfg, encoding="utf-8")
    return home


def read_gui(home: Path | None = None) -> tuple[str, str]:
    """(アドレス, APIキー) を config.xml から読む。"""
    home = home or home_dir()
    tree = ElementTree.parse(home / "config.xml")
    gui = tree.getroot().find("gui")
    return gui.find("address").text, gui.find("apikey").text


def api(method: str, path: str, payload=None, home: Path | None = None,
        timeout: int = 15):
    addr, key = read_gui(home)
    data = json.dumps(payload).encode() if payload is not None else None
    req = urllib.request.Request(
        f"http://{addr}{path}", data=data, method=method,
        headers={"X-API-Key": key, "Content-Type": "application/json",
                 "User-Agent": USER_AGENT})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        body = r.read()
    return json.loads(body) if body else None


def port_open(home: Path | None = None, timeout: float = 0.3) -> bool:
    """GUIポートが開いているかの高速チェック。

    Windowsは閉じたポートへのconnectを約2秒かけて内部リトライするため、
    HTTPを試す前に短いタイムアウトの生ソケットで確認する(UIカクつき防止)。
    """
    try:
        addr, _ = read_gui(home)
        host, port = addr.rsplit(":", 1)
        with socket.create_connection((host, int(port)), timeout=timeout):
            return True
    except OSError:
        return False


def ping(home: Path | None = None) -> bool:
    if not port_open(home):
        return False
    try:
        return api("GET", "/rest/system/ping", home=home,
                   timeout=5)["ping"] == "pong"
    except Exception:
        return False


last_spawned_pid: int | None = None  # このセッションで起動したプロセスID


def start(home: Path | None = None, wait: float = 30.0) -> bool:
    """serve をバックグラウンド起動(既に動いていれば何もしない)。"""
    global last_spawned_pid
    home = home or home_dir()
    if ping(home):
        return True
    flags = subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0
    proc = subprocess.Popen(
        [str(binary_path()), "serve", "--home", str(home), "--no-browser"],
        creationflags=flags,
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    deadline = time.time() + wait
    while time.time() < deadline:
        if ping(home):
            last_spawned_pid = proc.pid
            return True
        time.sleep(0.5)
    return False


def shutdown(home: Path | None = None) -> bool:
    try:
        api("POST", "/rest/system/shutdown", home=home)
        return True
    except Exception:
        return False


# ---------------------------------------------------------------------------
# デバイス・フォルダ管理
# ---------------------------------------------------------------------------

def normalize_device_id(raw: str) -> str:
    did = re.sub(r"[\s]", "", raw).upper()
    if not DEVICE_ID_RE.match(did):
        raise PipelineError(
            "デバイスIDの形式が不正です(7文字×8ブロック、ハイフン区切り)")
    return did


def validate_sync_id(sync_id: str) -> str:
    sync_id = sync_id.strip().lower()
    if not SYNC_ID_RE.match(sync_id):
        raise PipelineError(
            "同期IDは英小文字・数字・ハイフン(3〜50文字)にしてください")
    return sync_id


def derive_sync_id(name: str) -> str:
    """フォルダ名から同期IDを自動生成する(例: forest_150 → forest-150)。

    英小文字・数字・ハイフンに正規化。使える文字が残らなければ既定値。
    """
    sid = re.sub(r"-{2,}", "-",
                 re.sub(r"[^a-z0-9-]", "-", str(name).lower())).strip("-")
    if len(sid) < 3:
        return "musubi-project"
    return sid[:50].rstrip("-")


def my_device_id(home: Path | None = None) -> str:
    return api("GET", "/rest/system/status", home=home)["myID"]


def set_lan_only(enabled: bool, home: Path | None = None) -> None:
    """リレー・グローバル探索・NAT越えを無効化(LAN/VPN内運用・推奨)。"""
    api("PATCH", "/rest/config/options", {
        "globalAnnEnabled": not enabled,
        "relaysEnabled": not enabled,
        "natEnabled": not enabled,
        "localAnnEnabled": True,
    }, home=home)


def ensure_folder(sync_id: str, path: Path, home: Path | None = None,
                  local_only_history: bool = False) -> None:
    """プロジェクトフォルダを共有フォルダとして登録(バージョニング付き)。

    同期IDはフォルダ名由来で衝突し得るため、既存登録がある場合は
    そのパスが現在のルートと一致するか検証する(同名の別プロジェクトが
    過去の共有設定に相乗りして旧プロジェクトのファイルが流れる事故の防止)。

    local_only_history=True のときはバージョン履歴を同期対象から外す
    (この端末の .stignore にのみ反映。端末ごとの設定)。
    """
    sync_id = validate_sync_id(sync_id)
    folders = {f["id"]: f for f in api("GET", "/rest/config/folders", home=home)}
    if sync_id in folders:
        reg = str(folders[sync_id].get("path", ""))
        if os.path.normcase(os.path.realpath(reg)) != \
                os.path.normcase(os.path.realpath(path)):
            raise PipelineError(
                f"同期ID '{sync_id}' はこの端末で別のフォルダに使用中です: "
                f"{reg}\n別プロジェクトへの相乗りを防ぐため中止しました。"
                f"プロジェクト設定で別の同期IDを指定してください")
    else:
        api("POST", "/rest/config/folders", {
            "id": sync_id,
            "label": f"Musubi: {path.name}",
            "path": str(path),
            "type": "sendreceive",
            "fsWatcherEnabled": True,
            "versioning": {  # 誤削除・誤上書き・ランサム対策
                "type": "staggered",
                "params": {"cleanInterval": "3600", "maxAge": "2592000"},
            },
        }, home=home)
    write_stignore(path, local_only_history)  # 既存プロジェクトにも反映
    # 承認済みの相手が同じ同期IDを共有してきていれば、こちらからも共有する
    # (これが無いと双方「同期済み」表示のままファイルが流れない)
    accept_pending_folder_offers(sync_id, home)


# 進捗ボードHTMLは各端末でローカル生成する(同期で配ると、悪意ある
# ピアが差し替えたHTMLを他端末のブラウザで開かせる経路になるため)
STIGNORE_LINES = (
    "*.blend1", "*.blend2", "*.blend@", "*.tmp",
    "Thumbs.db", "desktop.ini",
    "/.musubi/board.html",
)

# バージョン履歴を「この端末だけ」に留めるための除外パターン。
# .stignore はSyncthingで端末間同期されないため、端末ごとに独立して効く。
HISTORY_IGNORE_LINE = "/.musubi/versions"


def write_stignore(root: Path, local_only_history: bool = False) -> bool:
    """一時ファイルと端末ローカル生成物を同期対象から外す。

    既存の .stignore は極力壊さず、不足している行だけ末尾に追記する
    (ユーザーが独自パターンを足していても保つ)。
    local_only_history に応じて履歴除外行を追加/削除する。
    変更があれば True を返す(再スキャン要否の判断に使う)。
    """
    ignore = root / ".stignore"
    if not ignore.exists():
        lines = list(STIGNORE_LINES)
        if local_only_history:
            lines.append(HISTORY_IGNORE_LINE)
        ignore.write_text("\n".join(lines) + "\n", encoding="utf-8")
        return True

    text = ignore.read_text(encoding="utf-8", errors="replace")
    existing_lines = text.splitlines()
    have = {ln.strip() for ln in existing_lines}
    changed = False

    missing = [ln for ln in STIGNORE_LINES if ln not in have]
    if local_only_history and HISTORY_IGNORE_LINE not in have:
        missing.append(HISTORY_IGNORE_LINE)
    if missing:
        prefix = "" if (not text or text.endswith("\n")) else "\n"
        with open(ignore, "a", encoding="utf-8") as f:
            f.write(prefix + "\n".join(missing) + "\n")
        changed = True

    if not local_only_history and HISTORY_IGNORE_LINE in have:
        kept = [ln for ln in existing_lines
                if ln.strip() != HISTORY_IGNORE_LINE]
        ignore.write_text("\n".join(kept) + ("\n" if kept else ""),
                          encoding="utf-8")
        changed = True
    return changed


def apply_history_ignore(local_only: bool, home: Path | None = None) -> list[str]:
    """この端末の全Musubi共有フォルダの履歴同期可否を切り替える。

    各フォルダの .stignore を書き換え、稼働中なら再スキャンさせて
    Syncthingにignore変更を即反映する。書き換えたフォルダのパス一覧を返す。
    Syncthingが停止・未導入なら空リスト(呼び出し側でフォールバックする)。
    """
    try:
        folders = api("GET", "/rest/config/folders", home=home)
    except Exception:
        return []
    running = ping(home)
    changed = []
    for f in folders:
        p = Path(f.get("path", ""))
        if not p.is_dir():
            continue
        try:
            write_stignore(p, local_only_history=local_only)
            changed.append(str(p))
            if running:
                try:
                    api("POST", f"/rest/db/scan?folder={f['id']}", home=home)
                except Exception:
                    pass  # 再スキャンは失敗してもfsWatcherがいずれ拾う
        except Exception:
            pass
    return changed


def _share_folder_with(sync_id: str, device_id: str, home: Path | None) -> None:
    folder = api("GET", f"/rest/config/folders/{sync_id}", home=home)
    devs = folder.get("devices", [])
    if not any(d["deviceID"] == device_id for d in devs):
        devs.append({"deviceID": device_id})
        folder["devices"] = devs
        api("PUT", f"/rest/config/folders/{sync_id}", folder, home=home)


def add_peer(device_id: str, name: str, sync_id: str,
             introducer: bool = False, addresses: list[str] | None = None,
             home: Path | None = None) -> None:
    """ピアを登録し、共有フォルダに参加させる。"""
    device_id = normalize_device_id(device_id)
    devices = {d["deviceID"] for d in api("GET", "/rest/config/devices", home=home)}
    if device_id not in devices:
        api("POST", "/rest/config/devices", {
            "deviceID": device_id,
            "name": name,
            "introducer": introducer,   # リーダー経由で他メンバーを自動紹介
            # 相手が別フォルダを共有してきても自動受諾しない(明示的な保証)
            "autoAcceptFolders": False,
            "addresses": addresses or ["dynamic"],
        }, home=home)
    _share_folder_with(validate_sync_id(sync_id), device_id, home)


def accept_pending_folder_offers(sync_id: str,
                                 home: Path | None = None) -> list[str]:
    """承認済みデバイスからの「同じ同期IDのフォルダ共有」の申し出を受ける。

    デバイス承認とフォルダ共有はSyncthingでは別物で、両方が揃って初めて
    ファイルが流れる。ここで受けるのは「人間が承認済みのデバイス」が
    「こちらと同じ同期ID」を共有してきている場合のみ。未知のデバイスや
    別IDのフォルダは従来どおり一切受けない(セキュリティ境界は不変)。
    """
    sync_id = validate_sync_id(sync_id)
    try:
        pend = api("GET", "/rest/cluster/pending/folders", home=home) or {}
    except (PipelineError, OSError, urllib.error.URLError):
        return []
    offered = (pend.get(sync_id) or {}).get("offeredBy") or {}
    if not offered:
        return []
    known = {d["deviceID"]
             for d in api("GET", "/rest/config/devices", home=home)}
    attached = []
    for did in offered:
        if did in known:
            _share_folder_with(sync_id, did, home)
            attached.append(did)
    return attached


def remove_device(device_id: str, home: Path | None = None) -> bool:
    """デバイスの承認を取り消す(キック)。全フォルダの共有からも外す。

    以後そのデバイスからの接続は拒否され、再参加には再承認が必要。
    リーダーは紹介者(introducer)なので、リーダーがキックすれば
    メンバー側の登録もSyncthingの標準動作で自動的に取り除かれる。
    """
    device_id = normalize_device_id(device_id)
    for f in api("GET", "/rest/config/folders", home=home):
        devs = [d for d in f.get("devices", [])
                if d.get("deviceID") != device_id]
        if len(devs) != len(f.get("devices", [])):
            f["devices"] = devs
            api("PUT", f"/rest/config/folders/{f['id']}", f, home=home)
    ids = {d["deviceID"]
           for d in api("GET", "/rest/config/devices", home=home)}
    if device_id not in ids:
        return False
    api("DELETE", f"/rest/config/devices/{device_id}", home=home)
    return True


def device_folders(device_id: str, home: Path | None = None) -> list[str]:
    """このデバイスと共有しているフォルダIDの一覧。

    キック(remove_device)は全フォルダから外すため、実行前に
    影響範囲(別プロジェクトも含むか)を人間に見せる用途。
    """
    device_id = normalize_device_id(device_id)
    return [f["id"] for f in api("GET", "/rest/config/folders", home=home)
            if any(d.get("deviceID") == device_id
                   for d in f.get("devices", []))]


def pending_devices(home: Path | None = None) -> dict:
    """接続してきた未承認デバイス {deviceID: {name, address, ...}}。"""
    return api("GET", "/rest/cluster/pending/devices", home=home) or {}


def remove_folder(folder_id: str, home: Path | None = None) -> bool:
    """共有フォルダの登録を解除する(ディスク上のファイルは消えない)。"""
    folder_id = validate_sync_id(folder_id)
    ids = [f["id"] for f in api("GET", "/rest/config/folders", home=home)]
    if folder_id not in ids:
        return False
    api("DELETE", f"/rest/config/folders/{folder_id}", home=home)
    return True


def pause_folder(folder_id: str, paused: bool = True,
                 home: Path | None = None) -> bool:
    """フォルダの同期を一時停止/再開する。

    remove_folder と違い**可逆**で、相手との共有設定は壊れない。
    複数のプロジェクトに参加しているとき、いま作業するプロジェクト以外を
    一時的に止める用途。再開はこの関数に paused=False を渡すだけ。

    設定の部分更新(PATCH)で paused だけを書き換えるので、
    読み込み→書き戻しの間に他の設定が失われることがない。
    """
    folder_id = validate_sync_id(folder_id)
    ids = [f["id"] for f in api("GET", "/rest/config/folders", home=home)]
    if folder_id not in ids:
        return False
    api("PATCH", f"/rest/config/folders/{folder_id}",
        {"paused": bool(paused)}, home=home)
    return True


# ---------------------------------------------------------------------------
# 招待ファイル(メンバーの手入力を無くす)
# ---------------------------------------------------------------------------

INVITE_FORMAT = 1


def make_invite(sync_id: str, device_id: str, project_name: str,
                leader_name: str = "") -> dict:
    """招待ファイルの中身を組み立てる(含まれるのは公開情報のみ)。"""
    return {
        "format": INVITE_FORMAT,
        "type": "musubi-invite",
        "sync_id": validate_sync_id(sync_id),
        "leader_device_id": normalize_device_id(device_id),
        "leader_name": str(leader_name)[:100],
        "project_name": str(project_name)[:100],
        "created_at": time.strftime("%Y-%m-%dT%H:%M:%S"),
    }


def parse_invite(text: str) -> dict:
    """招待ファイルを検証付きで読む(不正な内容は拒否)。"""
    try:
        data = json.loads(text)
    except json.JSONDecodeError:
        raise PipelineError("招待ファイルが読めません(JSONではありません)") from None
    if not isinstance(data, dict) or data.get("type") != "musubi-invite" \
            or data.get("format") != INVITE_FORMAT:
        raise PipelineError("Musubi Pipelineの招待ファイルではありません")
    return {
        "sync_id": validate_sync_id(str(data.get("sync_id", ""))),
        "leader_device_id": normalize_device_id(
            str(data.get("leader_device_id", ""))),
        "leader_name": str(data.get("leader_name", ""))[:100],
        "project_name": str(data.get("project_name", ""))[:100],
    }


def accept_pending(sync_id: str, home: Path | None = None,
                   only: list[str] | None = None) -> list[str]:
    """承認待ちデバイスを承認して共有フォルダに追加。承認したIDを返す。

    注意: 呼び出し側(人間)がデバイスIDを本人確認してから実行すること。
    only には確認画面に表示したIDのリストを渡す。表示後に接続してきた
    未確認デバイスまで一緒に承認されるのを防ぐ(承認待ちには残るので、
    次回の承認操作で改めて表示・確認される)。
    """
    accepted = []
    for did, info in pending_devices(home).items():
        if only is not None and did not in only:
            continue
        add_peer(did, info.get("name") or f"member-{did[:7]}", sync_id,
                 home=home)
        accepted.append(did)
    # 承認済みの相手からのフォルダ共有の申し出もここで受ける
    accept_pending_folder_offers(sync_id, home)
    return accepted


def recent_activity(home: Path | None = None, limit: int = 5) -> list[str]:
    """直近の同期アクティビティ(人間可読)。"""
    try:
        events = api("GET", "/rest/events?since=0&limit=30&timeout=1",
                     home=home, timeout=5) or []
    except Exception:
        return []
    lines = []
    for e in events:
        t = e.get("type")
        d = e.get("data") or {}
        hhmm = (e.get("time") or "")[11:19]
        if t == "ItemFinished" and not d.get("error"):
            verb = {"update": "受信", "delete": "削除を反映"}.get(
                d.get("action"), d.get("action", ""))
            lines.append(f"{hhmm} {verb}: {d.get('item')}")
        elif t == "DeviceConnected":
            lines.append(f"{hhmm} ピア接続: {d.get('deviceName') or str(d.get('id',''))[:7]}")
        elif t == "DeviceDisconnected":
            lines.append(f"{hhmm} ピア切断: {str(d.get('id',''))[:7]}")
        elif t == "FolderCompletion" and d.get("completion") == 100:
            lines.append(f"{hhmm} ピア側の同期完了 ({str(d.get('device',''))[:7]})")
    return lines[-limit:]


_FOLDER_STATE_JA = {
    "idle": "待機中(同期済み)",
    "scanning": "ローカルの変更を走査中",
    "syncing": "同期中(転送しています)",
    "sync-preparing": "同期準備中",
    "scan-waiting": "走査待ち",
    "sync-waiting": "同期待ち",
    "error": "エラー",
    "unknown": "不明",
}


def folder_detail(folder_id: str, home: Path | None = None) -> dict:
    """1フォルダの同期状態(状態・残り・ファイル数・エラー)を取得する。

    共有一覧を「1プロジェクト1枠」で見せるために、フォルダごとに呼ぶ。
    どれも 127.0.0.1 のローカル呼び出しで、呼ぶのはイベント駆動の
    バックグラウンドスレッドから(常駐ポーリングはしない)。
    取得できない項目は既定値のまま返す(表示を止めない)。
    """
    out = {"state": "", "need_bytes": 0, "need_items": 0, "files": 0,
           "errors": 0, "error_example": ""}
    try:
        db = api("GET", f"/rest/db/status?folder={folder_id}", home=home)
        out["state"] = _FOLDER_STATE_JA.get(db.get("state", "unknown"),
                                            db.get("state", "?"))
        out["need_bytes"] = db.get("needBytes") or 0
        out["need_items"] = db.get("needTotalItems") or 0
        out["files"] = db.get("localFiles") or 0
    except Exception:
        pass
    try:
        errs = api("GET", f"/rest/folder/errors?folder={folder_id}",
                   home=home).get("errors") or []
        out["errors"] = len(errs)
        if errs:
            out["error_example"] = errs[0].get("path", "")
    except Exception:
        pass
    return out


def status_report(sync_id: str, home: Path | None = None) -> dict:
    """パネル表示用の詳細ステータス+問題診断。

    返り値: {"running", "pid", "uptime", "my_id", "lines": [表示行],
             "problems": [警告行], "activity": [直近ログ]}
    行は接頭辞で意味付け: "*"=正常, 無印=情報。problemsは常に警告表示。
    """
    rep = {"running": False, "pid": last_spawned_pid, "uptime": 0,
           "my_id": "", "lines": [], "problems": [], "activity": [],
           # この端末の共有一覧: [{id, path, devices(相手名), current}]
           "folders": [],
           # ピア一覧: [{id, name, connected, address}](解除ボタン用)
           "peers": [],
           # 構造化状態(「次の一手」ナビゲーション用)
           "s": {"installed": False, "running": False, "folder_ok": False,
                 "peers": 0, "connected": 0, "pending_devices": 0,
                 "need_items": 0, "need_bytes": 0, "unexpected": []}}
    if not is_installed():
        return rep
    rep["s"]["installed"] = True
    if not ping(home):
        rep["lines"].append("Syncthing: 停止中(同期は行われていません)")
        return rep

    rep["running"] = True
    rep["s"]["running"] = True
    try:
        sysstat = api("GET", "/rest/system/status", home=home)
        rep["my_id"] = sysstat.get("myID", "")
        rep["uptime"] = sysstat.get("uptime", 0)
    except Exception as e:
        rep["problems"].append(f"状態取得エラー: {e}")
        return rep

    sync_id = validate_sync_id(sync_id)

    # --- デバイス(ピア)ごとの接続状態 ---
    devices = {}
    try:
        devices = {d["deviceID"]: d for d in
                   api("GET", "/rest/config/devices", home=home)}
        conns = api("GET", "/rest/system/connections", home=home)["connections"]
        peers = {did: d for did, d in devices.items() if did != rep["my_id"]}
        rep["s"]["peers"] = len(peers)
        n_connected = 0
        for did, dev in peers.items():
            c = conns.get(did, {})
            connected = bool(c.get("connected"))
            n_connected += 1 if connected else 0
            rep["peers"].append({"id": did,
                                 "name": dev.get("name") or did[:7],
                                 "connected": connected,
                                 "address": c.get("address", "")})
        rep["s"]["connected"] = n_connected
    except Exception as e:
        rep["problems"].append(f"接続情報の取得エラー: {e}")

    # --- 共有フォルダの状態と進捗 ---
    try:
        folders = api("GET", "/rest/config/folders", home=home)
        ids = [f["id"] for f in folders]
        rep["s"]["folder_ok"] = sync_id in ids
        if sync_id not in ids:
            rep["problems"].append(
                f"共有フォルダ '{sync_id}' が未登録です。"
                "「同期セットアップ」を押してください")
        # 共有一覧: 全プロジェクトを同じ形で集める(1プロジェクト1枠の表示用)。
        # 停止中のフォルダは状態を問い合わせても意味がないので省く。
        for f in folders:
            mates = [devices.get(d.get("deviceID"), {}).get("name")
                     or str(d.get("deviceID", ""))[:7]
                     for d in f.get("devices", [])
                     if d.get("deviceID") != rep["my_id"]]
            fid = f["id"]
            info = {"id": fid, "path": f.get("path", ""), "devices": mates,
                    "current": fid == sync_id, "paused": bool(f.get("paused")),
                    "state": "", "need_bytes": 0, "need_items": 0,
                    "files": 0, "errors": 0, "error_example": ""}
            if not info["paused"]:
                info.update(folder_detail(fid, home))
            rep["folders"].append(info)
        # 現在のプロジェクトの要約は、集めた情報から作る(問い合わせは1回で足りる)
        cur = next((f for f in rep["folders"] if f["current"]), None)
        if cur is not None:
            rep["s"]["need_bytes"] = cur["need_bytes"]
            rep["s"]["need_items"] = cur["need_items"]
            if cur["paused"]:
                rep["problems"].append(
                    f"このプロジェクト '{sync_id}' は一時停止中です。"
                    "共有一覧の「再開」で同期を再開できます")
            elif cur["need_bytes"]:
                rep["lines"].append(
                    f"* フォルダ: {cur['state']} 残り"
                    f"{cur['need_bytes'] / 1e6:.1f}MB({cur['need_items']}件)")
            else:
                rep["lines"].append(
                    f"* フォルダ: {cur['state']}"
                    f"(共有中 {cur['files']}ファイル)")
            if cur["errors"]:
                rep["problems"].append(
                    f"同期できないファイル {cur['errors']}件"
                    f"(例: {cur['error_example'] or '?'})")
            if not cur["devices"] and rep["s"]["peers"]:
                rep["problems"].append(
                    f"フォルダ '{sync_id}' をまだ誰とも共有していません。"
                    "「同期セットアップ」を押してください")
        # 他プロジェクトの共有は「異常」ではない(意図して参加している場合が
        # ある)。件数と状態は共有一覧が1枠ずつ示すので、ここでは警告しない。
        rep["s"]["unexpected"] = [i for i in ids if i != sync_id]
    except Exception as e:
        rep["problems"].append(f"フォルダ情報の取得エラー: {e}")

    # --- 同期ID不一致の検出(相手が別IDのフォルダを提案してきている) ---
    try:
        pend_f = api("GET", "/rest/cluster/pending/folders", home=home) or {}
        for fid, info in pend_f.items():
            offered = ", ".join(
                (devices.get(d, {}).get("name") or d[:7])
                for d in (info.get("offeredBy") or {}))
            if fid != sync_id:
                rep["problems"].append(
                    f"同期IDの不一致: {offered} は '{fid}' を共有しようと"
                    f"していますが、こちらの同期IDは '{sync_id}' です。"
                    "どちらかを合わせてください")
            else:
                rep["problems"].append(
                    f"{offered} がフォルダ '{fid}' を共有してきています。"
                    "「同期セットアップ」を押すと参加します")
    except Exception:
        pass

    # --- 承認待ちデバイス ---
    try:
        pend = pending_devices(home)
        rep["s"]["pending_devices"] = len(pend)
        if pend:
            rep["problems"].append(
                f"承認待ちデバイス {len(pend)}件: ID本人確認のうえ"
                "「承認待ちデバイスを承認」を押してください")
    except Exception:
        pass

    rep["activity"] = recent_activity(home)
    return rep
