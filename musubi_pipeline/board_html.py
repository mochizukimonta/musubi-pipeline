# SPDX-FileCopyrightText: 2026 mochizukimonta
# SPDX-License-Identifier: GPL-3.0-or-later
"""musubi_pipeline.board_html — 進捗ボードの静的HTML書き出し。

.musubi/board.html を生成する。ステータス等のデータ(JSON)は同期される
ため、各端末でこのHTMLをローカル生成すれば同じ内容の進捗が見える。

セキュリティ:
- board.html 自体は同期しない(.stignore で除外)。同期で配ると悪意ある
  ピアが差し替えたHTMLを他端末のブラウザで開かせる経路になるため、
  「各端末で生成したものだけを開く」運用とする
- ユーザー入力(メモ・コメント・担当者名)はすべて html.escape で無害化
  (同期フォルダは他人が書けるため、HTML/スクリプト注入を防ぐ)
- 外部リソース(CDN・フォント・JS)を一切読み込まない自己完結ファイル
"""

from __future__ import annotations

import html
import time
from pathlib import Path

from . import reviews, tasks
from .core import atomic_replace, cut_name, resolve_root, safe_path, scene_name
from .sync import host_id
from .tasks import STATUSES

STATUS_COLORS = {
    "todo": "#9aa0a6",
    "wip": "#1a73e8",
    "review": "#f9ab00",
    "retake": "#d93025",
    "approved": "#188038",
    "omit": "#5f6368",
}

_CSS = """
body{font-family:'Hiragino Sans','Yu Gothic UI','Meiryo',sans-serif;
 margin:2rem auto;max-width:1100px;padding:0 1rem;color:#202124;background:#fff}
h1{font-size:1.4rem;border-bottom:2px solid #202124;padding-bottom:.4rem}
.meta{color:#5f6368;font-size:.8rem}
.bar{height:14px;background:#e8eaed;border-radius:7px;overflow:hidden;margin:.6rem 0}
.bar>div{height:100%;background:#188038}
.chips{margin:.4rem 0 1.2rem}
.chip{display:inline-block;padding:.15rem .6rem;border-radius:1rem;color:#fff;
 font-size:.78rem;margin-right:.4rem}
table{border-collapse:collapse;width:100%;font-size:.85rem}
th,td{border:1px solid #dadce0;padding:.45rem .6rem;text-align:left;
 vertical-align:top}
th{background:#f1f3f4}
tr.scene-head td{background:#f8f9fa;font-weight:bold}
.st{display:inline-block;padding:.1rem .5rem;border-radius:1rem;color:#fff;
 font-size:.78rem;white-space:nowrap}
.lock{color:#d93025;font-size:.78rem}
details{font-size:.8rem}
details summary{cursor:pointer;color:#1a73e8}
li.rv{margin:.2rem 0}
.verdict-retake{color:#d93025;font-weight:bold}
.verdict-approved{color:#188038;font-weight:bold}
a{color:#1a73e8}
footer{margin-top:1.5rem;color:#9aa0a6;font-size:.75rem}
"""


def _esc(s) -> str:
    return html.escape(str(s), quote=True)


def _comments_html(root_str: str, r: dict) -> str:
    items = reviews.list_comments(root_str, r["scene"], r["cut"])
    if not items:
        return ""
    lis = []
    for c in items[:10]:
        frame = f" f{c['frame']}" if c.get("frame", -1) >= 0 else ""
        verdict = c.get("verdict", "comment")
        vtxt = ""
        if verdict != "comment":
            vtxt = (f' <span class="verdict-{_esc(verdict)}">'
                    f'[{_esc(reviews.VERDICTS[verdict])}]</span>')
        lis.append(
            f'<li class="rv">v{c["version"]:03d}{_esc(frame)} '
            f'{_esc(c.get("created_at", "")[5:16].replace("T", " "))} '
            f'{_esc(c.get("author", "?"))}{vtxt}: {_esc(c["comment"])}</li>')
    return (f'<details><summary>コメント {len(items)}件</summary>'
            f'<ul>{"".join(lis)}</ul></details>')


def export_board_html(root_str: str) -> Path:
    """進捗ボードを .musubi/board.html に書き出す。"""
    root = resolve_root(root_str)
    rows = tasks.board(root_str)
    s = tasks.summary(root_str)
    c = s["counts"]

    body = []
    body.append(f"<h1>{_esc(root.name)} 進行ボード</h1>")
    body.append(
        f'<p class="meta">更新: {time.strftime("%Y-%m-%d %H:%M:%S")} '
        f'({_esc(host_id())}) — Musubi Pipeline により自動生成。'
        f'進捗ボードはBlenderの『HTMLボードを書き出す』で各端末生成してください</p>')
    body.append(f'<div class="bar"><div style="width:{s["percent"]}%"></div></div>')
    chips = [f'<span class="chip" style="background:#188038">'
             f'承認 {s["approved"]}/{s["total"]} ({s["percent"]}%)</span>']
    for sid, label in STATUSES.items():
        if c[sid]:
            chips.append(f'<span class="chip" '
                         f'style="background:{STATUS_COLORS[sid]}">'
                         f'{_esc(label)} {c[sid]}</span>')
    body.append(f'<div class="chips">{"".join(chips)}</div>')

    # プロジェクト仕様(折りたたみ): レンダープロファイル対照表 + 文書仕様
    try:
        from . import spec as spec_mod
        from .render_profiles import SETTINGS, format_value
        sp = spec_mod.load(root_str)
        profs = sp.get("render_profiles", {})
        rows_html = []
        # レンダー仕様の対照表(本番 / 仮)— Blender UIの場所でグループ化
        final_s = profs.get("final", {}).get("settings", {})
        prev_s = profs.get("preview", {}).get("settings", {})
        if final_s or prev_s:
            rows_html.append(
                '<tr class="scene-head"><td>設定項目</td>'
                f'<td><b>{_esc(profs.get("final", {}).get("label", "本番"))}'
                f'</b></td><td><b>'
                f'{_esc(profs.get("preview", {}).get("label", "仮"))}'
                f'</b></td></tr>')
            last_group = None
            for key, (group, label, _g, _s) in SETTINGS.items():
                if key not in final_s and key not in prev_s:
                    continue
                if group != last_group:
                    last_group = group
                    rows_html.append(
                        f'<tr class="scene-head"><td colspan="3">'
                        f'{_esc(group)}</td></tr>')
                fv = format_value(final_s[key]) if key in final_s else "─"
                pv = format_value(prev_s[key]) if key in prev_s else "─"
                rows_html.append(f"<tr><td>{_esc(label)}</td>"
                                 f"<td>{_esc(fv)}</td>"
                                 f"<td>{_esc(pv)}</td></tr>")
        notes = sp.get("notes", "")
        if notes:
            notes_html = "<br>".join(_esc(line) for line in
                                     notes.split("\n")[:40])
            rows_html.append(f'<tr class="scene-head"><td colspan="3">'
                             f'備考</td></tr>'
                             f'<tr><td colspan="3">{notes_html}</td></tr>')
        upd = ""
        if sp.get("updated_at"):
            upd = (f' <span class="meta">(更新: '
                   f'{_esc(sp["updated_at"][5:16].replace("T", " "))} '
                   f'{_esc(sp["updated_by"])})</span>')
        head = (f'{final_s.get("resolution_x", "?")}×'
                f'{final_s.get("resolution_y", "?")} '
                f'{final_s.get("fps", "?")}fps '
                f'{final_s.get("output_format", "")}')
        body.append(
            f'<details><summary><b>{_esc(sp["title"])}</b> — 本番: '
            f'{_esc(head)}{upd}</summary>'
            f'<table>{"".join(rows_html)}</table></details><br>')
    except Exception:
        pass

    body.append("<table><tr><th>カット</th><th>状態</th><th>担当</th>"
                "<th>最新出力</th><th>編集中</th><th>指示・メモ / レビュー</th>"
                "<th>最終更新</th></tr>")
    last_scene = None
    for r in rows:
        if r["scene"] != last_scene:
            last_scene = r["scene"]
            body.append(f'<tr class="scene-head"><td colspan="7">'
                        f'{_esc(scene_name(r["scene"]))}</td></tr>')
        st = r["status"]
        out = "─"
        if r["latest_output"]:
            rel = (f'../output/{scene_name(r["scene"])}/'
                   f'{cut_name(r["cut"])}.{r["latest_output"]:03d}.mp4')
            out = f'<a href="{_esc(rel)}">v{r["latest_output"]:03d}</a>'
        lock = (f'<span class="lock">{_esc(r["locked_by"])}</span>'
                if r["locked_by"] else "")
        planned = "" if r["blend_exists"] else "(予定)"
        note = _esc(r["note"]) if r["note"] else ""
        note += _comments_html(root_str, r)
        updated = ""
        if r["updated_at"]:
            updated = (f'{_esc(r["updated_at"][5:16].replace("T", " "))}<br>'
                       f'{_esc(r["updated_by"])}')
        body.append(
            f'<tr><td>s{r["scene"]:02d}/c{r["cut"]:02d} {planned}</td>'
            f'<td><span class="st" style="background:{STATUS_COLORS[st]}">'
            f'{_esc(STATUSES[st])}</span></td>'
            f'<td>{_esc(r["assignee"]) or "─"}</td>'
            f'<td>{out}</td><td>{lock}</td><td>{note}</td>'
            f'<td>{updated}</td></tr>')
    body.append("</table>")
    body.append("<footer>Musubi Pipeline — 状態変更はBlenderの進行ボードから。"
                "このファイルの手動編集は次回の書き出しで失われます</footer>")

    doc = ('<!doctype html><html lang="ja"><head><meta charset="utf-8">'
           '<meta name="viewport" content="width=device-width,initial-scale=1">'
           f"<title>{_esc(root.name)} 進行ボード</title>"
           f"<style>{_CSS}</style></head><body>"
           + "".join(body) + "</body></html>")

    out_path = safe_path(root, ".musubi", "board.html")
    out_path.parent.mkdir(parents=True, exist_ok=True)
    tmp = out_path.with_suffix(".html.tmp")
    tmp.write_text(doc, encoding="utf-8")
    atomic_replace(tmp, out_path)
    return out_path
