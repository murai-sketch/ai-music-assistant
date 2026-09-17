#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
timing_gui.py
歌詞動画エディタ（ローカルGUI）。歌詞タイミングの微調整と、歌詞の抜け・漏れの
修正を行い、キネティックタイポグラフィ（kinetic.py）で書き出す。

外部ライブラリ・追加のpip依存は使わない
（Python標準ライブラリのhttp.server + ブラウザ標準のWeb Audio API）。
プレビュー画像だけは kinetic.py（Pillow）で実際の描画を返す。

--------------------------------------------------------------------------
使い方:
    scripts/lyric_video/.venv/bin/python scripts/lyric_video/timing_gui.py \\
        [--audio 音源] [--image 背景画像] [--song 01_Songs/曲名.md] [--port 8765]

    素材は起動後にブラウザからアップロード/選択してもよい。

できること:
    - 波形タイムライン上で、歌詞ブロックの端をドラッグして開始/終了を調整、
      中央をドラッグして行ごと移動（ビートへの吸着はチェックで切り替え）
    - タップ入力: 再生しながら T キーを押すと、選択行の開始を再生位置にして次の行へ
    - 行の追加・再生位置への複製・選択範囲のコピー（サビの繰り返し用）・分割・結合・削除、
      文言と区分（構成タグ）の修正
    - 抜け・漏れの一覧:
        抜け   = whisperが歌を聞き取っているのに字幕が出ていない区間
        怪しい = 字幕はあるが、その時間に歌が聞き取れていない／認識と一致しない行
        未使用 = 歌詞ノートにあるのに字幕に使われていない行
    - キネティック描画の実物プレビュー、静止画チェック、書き出し（進捗表示つき）
    - 保存のたびに直前の alignment.json を alignment.bak-<日時>.json に退避する

キーボード:
    Space 再生/停止   Enter 選択行の頭から再生   T タップ（開始=再生位置→次の行）
    ↑↓ 行の選択   ←→ 選択行を±0.05秒（Shiftで±0.5秒）   [ ] 開始だけ±0.05秒
    , . 再生位置を∓1秒   Delete 行を削除   ⌘Z / ⌘⇧Z 元に戻す/やり直し   ⌘S 保存
--------------------------------------------------------------------------
"""

import argparse
import hashlib
import io
import json
import shutil
import sys
import threading
import traceback
import uuid
import webbrowser
from datetime import datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

sys.path.insert(0, str(Path(__file__).resolve().parent))

from align import WORK_DIR, _audio_hash, align_lyrics  # noqa: E402
from beats import detect_beats  # noqa: E402
from song_note import SongNote, sections_for_alignment  # noqa: E402
from styles import DEFAULT_STYLE, STYLES  # noqa: E402

SCRIPT_DIR = Path(__file__).resolve().parent
VAULT_ROOT = SCRIPT_DIR.parent.parent
SONGS_DIR = VAULT_ROOT / "01_Songs"
UPLOAD_DIR = WORK_DIR / "uploads"
MAX_BACKUPS = 30

MIME_TYPES = {
    ".mp3": "audio/mpeg", ".wav": "audio/wav", ".m4a": "audio/mp4", ".flac": "audio/flac",
    ".jpg": "image/jpeg", ".jpeg": "image/jpeg", ".png": "image/png", ".webp": "image/webp",
    ".mp4": "video/mp4",
}

STATE = {
    "audio_path": None,
    "image_path": None,
    "song_path": None,
    "align_status": "idle",
    "align_error": None,
    "render_status": "idle",
    "render_error": None,
    "render_output": None,
    "render_progress": 0.0,
    "stills": [],
}
_LOCK = threading.Lock()
_PREVIEW = {"key": None, "renderer": None}


def _list_songs():
    if not SONGS_DIR.exists():
        return []
    return sorted(p.name for p in SONGS_DIR.glob("*.md") if not p.name.startswith("_"))


def _cache_dir():
    return WORK_DIR / _audio_hash(Path(STATE["audio_path"]))


def _load_json(path, default):
    path = Path(path)
    if not path.exists():
        return default
    return json.loads(path.read_text(encoding="utf-8"))


def _note():
    return SongNote(STATE["song_path"]) if STATE["song_path"] else None


def _beats():
    return _load_json(_cache_dir() / "beats.json", {}).get("beats", [])


def _backup_alignment(reason):
    path = _cache_dir() / "alignment.json"
    if not path.exists():
        return None
    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    dest = path.with_name(f"alignment.bak-{stamp}-{reason}.json")
    shutil.copy2(path, dest)
    backups = sorted(path.parent.glob("alignment.bak-*.json"))
    for old in backups[:-MAX_BACKUPS]:
        old.unlink()
    return dest


def _transcript():
    segs = _load_json(_cache_dir() / "whisper_words.json", [])
    words = []
    for s in segs:
        for w in s.get("words", []):
            text = w["word"].strip()
            if text:
                words.append({"s": round(w["start"], 3), "e": round(w["end"], 3), "w": text})
    return words


def _run_alignment(reset):
    STATE["align_status"] = "running"
    STATE["align_error"] = None
    try:
        note = _note()
        if reset:
            _backup_alignment("before-realign")
            (_cache_dir() / "alignment.json").unlink(missing_ok=True)
        align_lyrics(STATE["audio_path"], note.lyric_lines, use_cache=True)
        style = STYLES[DEFAULT_STYLE]
        detect_beats(
            STATE["audio_path"],
            min_gap_sec=style["beat_min_gap_sec"],
            strength_percentile=style["beat_strength_percentile"],
            bpm_hint=note.bpm,
            use_cache=True,
        )
        STATE["align_status"] = "done"
    except Exception:
        STATE["align_status"] = "error"
        STATE["align_error"] = traceback.format_exc()


def _plan_for(alignment, style_name):
    from kinetic import build_plan

    note = _note()
    sections = sections_for_alignment(alignment, note.lyric_sections)
    return build_plan(alignment, sections, _beats(), STYLES[style_name])


def _preview_jpeg(alignment, t, style_name):
    from kinetic import KineticRenderer

    key = hashlib.sha1(
        json.dumps([alignment, style_name, STATE["image_path"]], ensure_ascii=False, sort_keys=True).encode()
    ).hexdigest()
    with _LOCK:
        if _PREVIEW["key"] != key:
            plan = _plan_for(alignment, style_name)
            _PREVIEW["renderer"] = KineticRenderer(STATE["image_path"], plan, _beats(), STYLES[style_name])
            _PREVIEW["key"] = key
        frame = _PREVIEW["renderer"].frame_at(t)
    frame = frame.resize((432, 768))
    buf = io.BytesIO()
    frame.save(buf, "JPEG", quality=82)
    return buf.getvalue()


def _run_render(style_name, stills_only):
    from kinetic import load_or_build_plan, render_kinetic, render_stills

    STATE["render_status"] = "running"
    STATE["render_error"] = None
    STATE["render_progress"] = 0.0
    try:
        note = _note()
        cache_dir = _cache_dir()
        cache = _load_json(cache_dir / "alignment.json", {})
        alignment = cache["alignment"]
        style = STYLES[style_name]
        sections = sections_for_alignment(alignment, note.lyric_sections)
        plan = load_or_build_plan(cache_dir / "kinetic_plan.json", alignment, sections, _beats(), style)
        stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
        if stills_only:
            out_dir = cache_dir / f"gui_stills_{stamp}"
            STATE["stills"] = [str(p) for p in render_stills(STATE["image_path"], plan, _beats(), style, out_dir)]
        else:
            out = cache_dir / f"gui_kinetic_{stamp}.mp4"

            def progress(p):
                STATE["render_progress"] = p

            render_kinetic(STATE["image_path"], STATE["audio_path"], plan, _beats(), style, out, progress=progress)
            STATE["render_output"] = str(out)
        STATE["render_progress"] = 1.0
        STATE["render_status"] = "done"
    except Exception:
        STATE["render_status"] = "error"
        STATE["render_error"] = traceback.format_exc()


class Handler(BaseHTTPRequestHandler):
    def log_message(self, fmt, *args):
        pass

    def _json(self, obj, code=200):
        data = json.dumps(obj, ensure_ascii=False).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(data)

    def _bytes(self, data, content_type):
        self.send_response(200)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(data)

    def _file(self, path):
        """HTTP Range対応の配信。Range非対応だと<audio>がシーク不可になる。"""
        path = Path(path)
        content_type = MIME_TYPES.get(path.suffix.lower(), "application/octet-stream")
        size = path.stat().st_size
        range_header = self.headers.get("Range")
        start, end = 0, size - 1
        if range_header:
            try:
                _unit, _, spec = range_header.partition("=")
                s, _, e = spec.partition("-")
                start = int(s) if s else 0
                end = min(int(e) if e else size - 1, size - 1)
            except ValueError:
                start, end = 0, size - 1
            self.send_response(206)
            self.send_header("Content-Range", f"bytes {start}-{end}/{size}")
        else:
            self.send_response(200)
        length = end - start + 1
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(length))
        self.send_header("Accept-Ranges", "bytes")
        self.end_headers()
        with open(path, "rb") as f:
            f.seek(start)
            remaining = length
            while remaining > 0:
                chunk = f.read(min(remaining, 1 << 20))
                if not chunk:
                    break
                self.wfile.write(chunk)
                remaining -= len(chunk)

    def _need(self, *keys):
        missing = [k for k in keys if not STATE[k]]
        if missing:
            self._json({"error": "未設定: " + ", ".join(missing)}, 400)
            return False
        return True

    def do_GET(self):
        parsed = urlparse(self.path)
        path, query = parsed.path, parse_qs(parsed.query)

        if path == "/":
            self._bytes(HTML.encode("utf-8"), "text/html; charset=utf-8")
        elif path == "/config":
            self._json({
                "styles": {k: {"max_hold_sec": v["max_hold_sec"]} for k, v in STYLES.items()},
                "default_style": DEFAULT_STYLE,
                "songs": _list_songs(),
                "state": {
                    "audio": Path(STATE["audio_path"]).name if STATE["audio_path"] else None,
                    "image": Path(STATE["image_path"]).name if STATE["image_path"] else None,
                    "song": Path(STATE["song_path"]).name if STATE["song_path"] else None,
                },
            })
        elif path == "/audio":
            if STATE["audio_path"]:
                self._file(STATE["audio_path"])
            else:
                self.send_response(404); self.end_headers()
        elif path == "/project":
            if not self._need("audio_path", "song_path"):
                return
            note = _note()
            cache = _load_json(_cache_dir() / "alignment.json", {})
            alignment = cache.get("alignment", [])
            if cache and cache.get("lyric_lines") != note.lyric_lines:
                alignment = []
            sections = sections_for_alignment(alignment, note.lyric_sections)
            for row, sec in zip(alignment, sections):
                row.setdefault("section", sec)
            self._json({
                "alignment": alignment,
                "note_lines": [{"text": t, "section": s} for t, s in note.lyric_sections],
                "transcript": _transcript(),
                "beats": _beats(),
                "ignored_gaps": cache.get("ignored_gaps", []),
                "backups": sorted(p.name for p in _cache_dir().glob("alignment.bak-*.json"))[-8:],
            })
        elif path == "/align/status":
            self._json({"status": STATE["align_status"], "error": STATE["align_error"]})
        elif path == "/render/status":
            self._json({
                "status": STATE["render_status"], "error": STATE["render_error"],
                "progress": STATE["render_progress"],
                "output": Path(STATE["render_output"]).name if STATE["render_output"] else None,
                "stills": [Path(p).name for p in STATE["stills"]],
            })
        elif path == "/render/download":
            if STATE["render_output"] and Path(STATE["render_output"]).exists():
                self._file(STATE["render_output"])
            else:
                self.send_response(404); self.end_headers()
        elif path == "/stills":
            name = query.get("name", [""])[0]
            match = [p for p in STATE["stills"] if Path(p).name == name]
            if match:
                self._file(match[0])
            else:
                self.send_response(404); self.end_headers()
        else:
            self.send_response(404); self.end_headers()

    def do_POST(self):
        parsed = urlparse(self.path)
        path, query = parsed.path, parse_qs(parsed.query)
        length = int(self.headers.get("Content-Length", 0))
        body = self.rfile.read(length) if length else b""

        if path in ("/upload/audio", "/upload/image"):
            kind = path.rsplit("/", 1)[1]
            ext = Path("x" + query.get("ext", [".bin"])[0]).suffix.lower()
            if ext not in MIME_TYPES:
                self._json({"error": f"対応していない形式: {ext}"}, 400); return
            UPLOAD_DIR.mkdir(parents=True, exist_ok=True)
            dest = UPLOAD_DIR / f"{kind}_{uuid.uuid4().hex[:8]}{ext}"
            dest.write_bytes(body)
            STATE[f"{kind}_path"] = str(dest)
            self._json({"ok": True})
        elif path == "/song":
            name = Path(query.get("name", [""])[0]).name
            song_path = SONGS_DIR / name
            if not name or not song_path.exists():
                self._json({"error": "曲ノートが見つかりません"}, 404); return
            STATE["song_path"] = str(song_path)
            note = SongNote(song_path)
            self._json({"ok": True, "title": note.title, "bpm": note.bpm, "lines": len(note.lyric_lines)})
        elif path == "/alignment":
            if not self._need("audio_path", "song_path"):
                return
            data = json.loads(body.decode("utf-8"))
            rows = []
            for r in data["alignment"]:
                row = {"line": str(r["line"]), "start": round(float(r["start"]), 3),
                       "end": round(float(r["end"]), 3)}
                for k in ("src", "match", "section"):
                    if r.get(k) is not None:
                        row[k] = r[k]
                rows.append(row)
            rows.sort(key=lambda r: r["start"])
            cache_path = _cache_dir() / "alignment.json"
            cache = _load_json(cache_path, {})
            backup = _backup_alignment("before-save")
            cache["lyric_lines"] = _note().lyric_lines
            cache["alignment"] = rows
            cache["ignored_gaps"] = data.get("ignored_gaps", [])
            cache["edited"] = True
            cache_path.parent.mkdir(parents=True, exist_ok=True)
            cache_path.write_text(json.dumps(cache, ensure_ascii=False, indent=2), encoding="utf-8")
            self._json({"ok": True, "backup": backup.name if backup else None})
        elif path == "/restore":
            name = Path(json.loads(body.decode("utf-8"))["name"]).name
            src = _cache_dir() / name
            if not name.startswith("alignment.bak-") or not src.exists():
                self._json({"error": "バックアップが見つかりません"}, 404); return
            _backup_alignment("before-restore")
            shutil.copy2(src, _cache_dir() / "alignment.json")
            self._json({"ok": True})
        elif path == "/align/run":
            if not self._need("audio_path", "song_path"):
                return
            params = json.loads(body.decode("utf-8")) if body else {}
            threading.Thread(target=_run_alignment, args=(bool(params.get("reset")),), daemon=True).start()
            self._json({"ok": True})
        elif path == "/preview":
            if not self._need("audio_path", "song_path", "image_path"):
                return
            params = json.loads(body.decode("utf-8"))
            try:
                jpeg = _preview_jpeg(params["alignment"], float(params["t"]), params.get("style", DEFAULT_STYLE))
            except Exception:
                self._json({"error": traceback.format_exc()}, 500); return
            self._bytes(jpeg, "image/jpeg")
        elif path == "/render/run":
            if not self._need("audio_path", "song_path", "image_path"):
                return
            if STATE["render_status"] == "running":
                self._json({"error": "書き出し中です"}, 409); return
            params = json.loads(body.decode("utf-8")) if body else {}
            args = (params.get("style", DEFAULT_STYLE), bool(params.get("stills")))
            threading.Thread(target=_run_render, args=args, daemon=True).start()
            self._json({"ok": True})
        else:
            self.send_response(404); self.end_headers()


HTML = r"""<!doctype html>
<html lang="ja">
<head>
<meta charset="utf-8">
<title>歌詞動画エディタ</title>
<style>
:root { --bg:#111; --panel:#1a1a1d; --line:#2c2c31; --text:#e8e8ea; --dim:#8b8b93;
        --accent:#ff3b70; --ok:#3ecf8e; --warn:#ffb020; --miss:#ff7a1a; --sel:#2b6cff; }
* { box-sizing: border-box; }
body { margin:0; background:var(--bg); color:var(--text); font:13px/1.45 -apple-system, "Hiragino Sans", sans-serif; }
header { display:flex; gap:10px; align-items:center; flex-wrap:wrap; padding:8px 12px; border-bottom:1px solid var(--line); background:#0c0c0e; position:sticky; top:0; z-index:5; }
header h1 { font-size:14px; margin:0 8px 0 0; }
button, select, input { font:inherit; color:var(--text); background:#24242a; border:1px solid #3a3a42; border-radius:5px; padding:4px 8px; }
button { cursor:pointer; } button:hover { border-color:#666; }
button.primary { background:var(--accent); border-color:var(--accent); color:#fff; font-weight:600; }
button.small { padding:1px 6px; font-size:12px; }
.drop { border:1px dashed #555; border-radius:5px; padding:4px 10px; color:var(--dim); cursor:pointer; }
.drop.ready { border-style:solid; border-color:var(--ok); color:var(--ok); }
.status { color:var(--dim); font-size:12px; }
main { display:grid; grid-template-columns: 300px minmax(0, 1fr); gap:10px; padding:10px; }
main > div { min-width:0; }
aside, section { background:var(--panel); border:1px solid var(--line); border-radius:8px; padding:10px; }
aside { align-self:start; position:sticky; top:54px; }
#previewBox { width:100%; aspect-ratio:9/16; background:#000; border-radius:6px; overflow:hidden; position:relative; }
#previewImg { width:100%; height:100%; object-fit:cover; display:block; }
#previewNote { position:absolute; bottom:4px; left:6px; font-size:11px; color:#aaa; text-shadow:0 0 3px #000; }
.transport { display:flex; gap:6px; align-items:center; flex-wrap:wrap; margin:8px 0; }
#time { font-family: ui-monospace, monospace; }
#timeline { width:100%; height:190px; display:block; background:#0b0b0d; border-radius:6px; cursor:crosshair; touch-action:none; }
#overview { width:100%; height:26px; display:block; background:#0b0b0d; border-radius:4px; margin-top:4px; cursor:pointer; }
.toolbar { display:flex; gap:8px; align-items:center; flex-wrap:wrap; margin:6px 0; }
table { width:100%; border-collapse:collapse; }
th, td { border-bottom:1px solid var(--line); padding:3px 4px; text-align:left; white-space:nowrap; }
th { color:var(--dim); font-weight:500; position:sticky; top:0; background:var(--panel); }
td.text { width:100%; } td.text input { width:100%; min-width:160px; }
input.t { width:74px; font-family: ui-monospace, monospace; }
tr.sel { background:#1d2a4a; } tr.playing td:first-child { box-shadow: inset 3px 0 0 var(--accent); }
tr.low td.match { color:var(--warn); }
#tableWrap { max-height:46vh; overflow:auto; }
.issues { display:grid; grid-template-columns: repeat(3, 1fr); gap:8px; }
.issue-col h3 { margin:0 0 4px; font-size:12px; }
.issue { border:1px solid var(--line); border-radius:5px; padding:4px 6px; margin:3px 0; }
.issue .t { font-family: ui-monospace, monospace; color:var(--dim); }
.issue .heard { color:#ccc; }
.miss h3 { color:var(--miss); } .sus h3 { color:var(--warn); } .unused h3 { color:#aab; }
.hint { color:var(--dim); font-size:11px; }
kbd { background:#2a2a30; border:1px solid #444; border-radius:3px; padding:0 4px; font-size:11px; }
#stills img { width:100%; margin-top:6px; border-radius:4px; }
.hidden { display:none !important; }
</style>
</head>
<body>
<header>
  <h1>歌詞動画エディタ</h1>
  <span class="drop" id="dropAudio">🎵 音源</span>
  <span class="drop" id="dropImage">🖼 背景</span>
  <input type="file" id="fileAudio" class="hidden" accept="audio/*">
  <input type="file" id="fileImage" class="hidden" accept="image/*">
  <select id="songSelect"><option value="">曲ノートを選ぶ</option></select>
  <button id="alignBtn">自動タイミング</button>
  <button id="realignBtn" title="保存済みの手直しを退避してから作り直す">作り直す</button>
  <span class="status" id="alignStatus"></span>
  <span style="flex:1"></span>
  <select id="styleSelect"></select>
  <button id="undoBtn" title="⌘Z">↶</button><button id="redoBtn" title="⌘⇧Z">↷</button>
  <button class="primary" id="saveBtn">保存 ⌘S</button>
  <span class="status" id="saveStatus"></span>
</header>
<main>
  <aside>
    <div id="previewBox"><img id="previewImg" alt=""><span id="previewNote">プレビュー（キネティック描画）</span></div>
    <div class="transport">
      <button id="playBtn">▶ 再生</button>
      <span id="time">0.00 / 0.00</span>
      <label><input type="checkbox" id="livePreview" checked> 再生中も更新</label>
    </div>
    <div class="toolbar">
      <button id="stillsBtn">静止画チェック</button>
      <button class="primary" id="renderBtn">書き出す</button>
    </div>
    <div class="status" id="renderStatus"></div>
    <a id="downloadLink" class="hidden" href="/render/download" download>⬇ 書き出した動画</a>
    <div id="stills"></div>
    <p class="hint">
      <kbd>Space</kbd> 再生/停止　<kbd>Enter</kbd> 行の頭から<br>
      <kbd>T</kbd> タップ（開始=今→次の行）<br>
      <kbd>↑</kbd><kbd>↓</kbd> 行選択　<kbd>←</kbd><kbd>→</kbd> 行を±0.05秒（⇧で±0.5）<br>
      <kbd>[</kbd><kbd>]</kbd> 開始だけ±0.05秒　<kbd>,</kbd><kbd>.</kbd> ∓1秒<br>
      <kbd>Delete</kbd> 削除　<kbd>⌘Z</kbd> 元に戻す　<kbd>⌘S</kbd> 保存<br>
      ⇧クリックで範囲選択 → 「選択を再生位置にコピー」でサビの繰り返しを足せます。
    </p>
    <div class="toolbar">
      <select id="backupSelect"><option value="">バックアップから戻す…</option></select>
    </div>
  </aside>
  <div>
    <section>
      <canvas id="timeline"></canvas>
      <canvas id="overview"></canvas>
      <div class="toolbar">
        <label>表示幅 <input type="range" id="zoom" min="4" max="60" value="14"> <span id="zoomVal">14</span>秒</label>
        <label><input type="checkbox" id="snapBeats"> ビートに吸着</label>
        <span class="hint">ブロックの端をドラッグ=開始/終了、中央=移動。波形をクリック=再生位置。上段の橙＝歌っているのに字幕がない所</span>
      </div>
    </section>
    <section style="margin-top:10px">
      <div class="toolbar">
        <button id="addBtn">＋ 再生位置に行を追加</button>
        <button id="copySelBtn">⎘ 選択を再生位置にコピー</button>
        <button id="delSelBtn">🗑 選択を削除</button>
        <span class="status" id="selInfo"></span>
      </div>
      <div id="tableWrap">
        <table>
          <thead><tr><th>#</th><th>開始</th><th>終了</th><th>歌詞</th><th>区分</th><th>一致</th><th></th></tr></thead>
          <tbody id="rows"></tbody>
        </table>
      </div>
    </section>
    <section style="margin-top:10px">
      <div class="issues">
        <div class="issue-col miss"><h3>抜け：歌っているのに字幕がない <span id="missCount"></span></h3><div id="missList"></div></div>
        <div class="issue-col sus"><h3>怪しい：字幕はあるが歌と合わない <span id="susCount"></span></h3><div id="susList"></div></div>
        <div class="issue-col unused"><h3>未使用：ノートにあるのに字幕にない <span id="unusedCount"></span></h3><div id="unusedList"></div></div>
      </div>
    </section>
  </div>
</main>
<audio id="audio" preload="auto"></audio>
<script>
const $ = id => document.getElementById(id);
const audio = $('audio');
let CONFIG = null;
let rows = [];            // [{line,start,end,src,match,section}]
let noteLines = [];       // [{text,section}]
let words = [];           // [{s,e,w}]
let beats = [];
let ignoredGaps = [];
let duration = 0;
let peaks = null, peaksPerSec = 100;
let sel = new Set();      // selected row indexes
let anchorSel = -1;
let undoStack = [], redoStack = [];
let dirty = false;
let viewStart = 0;

// ---------- utilities ----------
const fmt = t => (t ?? 0).toFixed(2);
const clamp = (v, a, b) => Math.min(Math.max(v, a), b);
function maxHold() { return (CONFIG.styles[$('styleSelect').value] || {}).max_hold_sec || 2.8; }
function shownEnd(r, i) {
  const next = rows[i + 1] ? rows[i + 1].start : Infinity;
  return Math.min(r.end, r.start + maxHold(), next);
}
function norm(s) {
  let out = '';
  for (const ch of (s || '').normalize('NFKC')) {
    if (/[\s\p{P}\p{S}]/u.test(ch)) continue;
    const c = ch.charCodeAt(0);
    out += (c >= 0x30A1 && c <= 0x30F6) ? String.fromCharCode(c - 0x60) : ch.toLowerCase();
  }
  return out;
}
function lev(a, b) {
  if (a === b) return 0;
  const m = a.length, n = b.length;
  if (!m) return n; if (!n) return m;
  let prev = Array.from({length: n + 1}, (_, j) => j);
  for (let i = 1; i <= m; i++) {
    const cur = [i];
    for (let j = 1; j <= n; j++) cur[j] = Math.min(prev[j] + 1, cur[j-1] + 1, prev[j-1] + (a[i-1] === b[j-1] ? 0 : 1));
    prev = cur;
  }
  return prev[n];
}
function sim(a, b) { a = norm(a); b = norm(b); const L = Math.max(a.length, b.length); return L ? 1 - lev(a, b) / L : 1; }
function esc(s) { return String(s).replace(/[&<>"]/g, c => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;'}[c])); }

// ---------- history ----------
function snapshot() { return JSON.stringify({rows, ignoredGaps}); }
function commit() {
  undoStack.push(lastSnap); if (undoStack.length > 200) undoStack.shift();
  redoStack = []; lastSnap = snapshot(); setDirty(true); refresh();
}
let lastSnap = '[]';
function restore(s) { const o = JSON.parse(s); rows = o.rows; ignoredGaps = o.ignoredGaps; lastSnap = s; sel = new Set([...sel].filter(i => i < rows.length)); setDirty(true); refresh(); }
function undo() { if (!undoStack.length) return; redoStack.push(lastSnap); restore(undoStack.pop()); }
function redo() { if (!redoStack.length) return; undoStack.push(lastSnap); restore(redoStack.pop()); }
function setDirty(v) { dirty = v; $('saveStatus').textContent = v ? '未保存の変更あり' : ''; }
function sortRows() {
  const selRows = new Set([...sel].map(i => rows[i]));
  rows.sort((a, b) => a.start - b.start);
  sel = new Set(rows.map((r, i) => selRows.has(r) ? i : -1).filter(i => i >= 0));
}

// ---------- loading ----------
async function loadConfig() {
  CONFIG = await (await fetch('/config')).json();
  const ss = $('songSelect');
  CONFIG.songs.forEach(n => ss.add(new Option(n, n)));
  const st = $('styleSelect');
  Object.keys(CONFIG.styles).forEach(n => st.add(new Option(n, n, n === CONFIG.default_style, n === CONFIG.default_style)));
  const s = CONFIG.state;
  if (s.audio) { markDrop('dropAudio', '🎵 ' + s.audio); audio.src = '/audio'; }
  if (s.image) markDrop('dropImage', '🖼 ' + s.image);
  if (s.song) ss.value = s.song;
  if (s.audio && s.song) await loadProject();
}
function markDrop(id, text) { $(id).classList.add('ready'); $(id).textContent = text; }

async function loadProject() {
  const res = await fetch('/project');
  if (!res.ok) return;
  const p = await res.json();
  rows = p.alignment; noteLines = p.note_lines; words = p.transcript; beats = p.beats;
  ignoredGaps = p.ignored_gaps || [];
  const bs = $('backupSelect');
  bs.length = 1; p.backups.slice().reverse().forEach(n => bs.add(new Option(n.replace('alignment.bak-', '').replace('.json', ''), n)));
  undoStack = []; redoStack = []; lastSnap = snapshot(); setDirty(false);
  if (!rows.length) $('alignStatus').textContent = 'タイミング未作成：「自動タイミング」を押してください';
  if (!peaks) await decodeWaveform();
  refresh(); requestPreview();
}

async function decodeWaveform() {
  const buf = await (await fetch('/audio')).arrayBuffer();
  const actx = new (window.AudioContext || window.webkitAudioContext)();
  const ab = await actx.decodeAudioData(buf);
  duration = ab.duration;
  const ch = ab.getChannelData(0);
  const n = Math.ceil(duration * peaksPerSec);
  const step = ch.length / n;
  peaks = new Float32Array(n);
  for (let i = 0; i < n; i++) {
    let m = 0; const s0 = Math.floor(i * step), s1 = Math.floor((i + 1) * step);
    for (let j = s0; j < s1; j += 4) { const v = Math.abs(ch[j]); if (v > m) m = v; }
    peaks[i] = m;
  }
  let mx = 0; for (const v of peaks) mx = Math.max(mx, v);
  if (mx > 0) for (let i = 0; i < n; i++) peaks[i] /= mx;
}

// ---------- uploads ----------
function setupDrop(dropId, inputId, url, done) {
  const drop = $(dropId), input = $(inputId);
  drop.onclick = () => input.click();
  drop.ondragover = e => e.preventDefault();
  drop.ondrop = e => { e.preventDefault(); if (e.dataTransfer.files.length) send(e.dataTransfer.files[0]); };
  input.onchange = () => input.files.length && send(input.files[0]);
  async function send(file) {
    drop.textContent = 'アップロード中…';
    const ext = '.' + file.name.split('.').pop().toLowerCase();
    const r = await fetch(url + '?ext=' + encodeURIComponent(ext), {method: 'POST', body: await file.arrayBuffer()});
    if (!r.ok) { drop.textContent = (await r.json()).error; return; }
    markDrop(dropId, (dropId === 'dropAudio' ? '🎵 ' : '🖼 ') + file.name);
    done();
  }
}
setupDrop('dropAudio', 'fileAudio', '/upload/audio', async () => { audio.src = '/audio?' + Date.now(); peaks = null; await decodeWaveform(); await loadProject(); });
setupDrop('dropImage', 'fileImage', '/upload/image', () => requestPreview(true));
$('songSelect').onchange = async e => {
  if (!e.target.value) return;
  const r = await (await fetch('/song?name=' + encodeURIComponent(e.target.value), {method: 'POST'})).json();
  $('alignStatus').textContent = r.ok ? `${r.title}（BPM ${r.bpm ?? '不明'}／${r.lines}行）` : r.error;
  if (audio.src) await loadProject();
};

async function runAlign(reset) {
  if (reset && !confirm('保存済みの手直しをバックアップに退避して、自動タイミングを作り直します。よろしいですか？')) return;
  $('alignStatus').textContent = '実行中…（初回は文字起こしに数分かかります）';
  const r = await fetch('/align/run', {method: 'POST', body: JSON.stringify({reset})});
  if (!r.ok) { $('alignStatus').textContent = (await r.json()).error; return; }
  const poll = setInterval(async () => {
    const s = await (await fetch('/align/status')).json();
    if (s.status === 'done') { clearInterval(poll); $('alignStatus').textContent = '✅ 完了'; await loadProject(); }
    if (s.status === 'error') { clearInterval(poll); $('alignStatus').textContent = '❌ ' + (s.error || '').trim().split('\n').pop(); }
  }, 1500);
}
$('alignBtn').onclick = () => runAlign(false);
$('realignBtn').onclick = () => runAlign(true);
$('backupSelect').onchange = async e => {
  const name = e.target.value; if (!name) return;
  if (!confirm(name + ' に戻します（今の保存内容も退避します）')) { e.target.value = ''; return; }
  await fetch('/restore', {method: 'POST', body: JSON.stringify({name})});
  await loadProject();
};

async function save() {
  $('saveStatus').textContent = '保存中…';
  const r = await fetch('/alignment', {method: 'POST', body: JSON.stringify({alignment: rows, ignored_gaps: ignoredGaps})});
  const d = await r.json();
  if (r.ok) { setDirty(false); $('saveStatus').textContent = '✅ 保存しました' + (d.backup ? '（前の版を退避）' : ''); }
  else $('saveStatus').textContent = '❌ ' + d.error;
  return r.ok;
}
$('saveBtn').onclick = save;
window.addEventListener('beforeunload', e => { if (dirty) { e.preventDefault(); e.returnValue = ''; } });

// ---------- timeline ----------
const tl = $('timeline'), tctx = tl.getContext('2d');
const ov = $('overview'), octx = ov.getContext('2d');
const LANE = {wave: [0, 70], words: [74, 34], rows: [112, 70]};
function viewLen() { return +$('zoom').value; }
function x2t(x) { return viewStart + x / tl.clientWidth * viewLen(); }
function t2x(t) { return (t - viewStart) / viewLen() * tl.clientWidth; }
function resizeCanvas(c) {
  const dpr = window.devicePixelRatio || 1;
  const w = c.clientWidth, h = c.clientHeight;
  if (c.width !== Math.round(w * dpr) || c.height !== Math.round(h * dpr)) { c.width = Math.round(w * dpr); c.height = Math.round(h * dpr); }
  c.getContext('2d').setTransform(dpr, 0, 0, dpr, 0, 0);
}
function followPlayhead() {
  const t = audio.currentTime, L = viewLen();
  if (!audio.paused && (t > viewStart + L * 0.85 || t < viewStart)) viewStart = clamp(t - L * 0.15, 0, Math.max(duration - L, 0));
}
function drawTimeline() {
  resizeCanvas(tl);
  const W = tl.clientWidth, H = tl.clientHeight;
  tctx.clearRect(0, 0, W, H);
  const L = viewLen();
  // grid
  tctx.fillStyle = '#26262c'; tctx.font = '10px ui-monospace, monospace';
  for (let s = Math.ceil(viewStart); s < viewStart + L; s++) {
    const x = t2x(s); tctx.fillRect(x, 0, 1, H);
    if (s % 2 === 0) { tctx.fillStyle = '#666'; tctx.fillText(s + 's', x + 2, 10); tctx.fillStyle = '#26262c'; }
  }
  // beats
  tctx.fillStyle = 'rgba(255,255,255,0.08)';
  for (const b of beats) { if (b < viewStart || b > viewStart + L) continue; tctx.fillRect(t2x(b), LANE.wave[0], 1, LANE.wave[1]); }
  // waveform
  if (peaks) {
    const [y0, h] = LANE.wave; const mid = y0 + h / 2;
    tctx.fillStyle = '#4a9eff';
    for (let x = 0; x < W; x++) {
      const i0 = Math.floor(x2t(x) * peaksPerSec), i1 = Math.max(Math.floor(x2t(x + 1) * peaksPerSec), i0 + 1);
      let m = 0; for (let i = i0; i < i1 && i < peaks.length; i++) if (i >= 0) m = Math.max(m, peaks[i]);
      tctx.fillRect(x, mid - m * h / 2, 1, Math.max(m * h, 1));
    }
  }
  // gaps (missing captions)
  for (const g of gaps) {
    if (g.ignored) continue;
    tctx.fillStyle = 'rgba(255,122,26,0.28)';
    tctx.fillRect(t2x(g.s), 0, Math.max(t2x(g.e) - t2x(g.s), 2), LANE.words[0] + LANE.words[1]);
  }
  // words
  const [wy, wh] = LANE.words;
  tctx.font = '11px -apple-system, sans-serif';
  for (const w of words) {
    if (w.e < viewStart || w.s > viewStart + L) continue;
    const x0 = t2x(w.s), x1 = t2x(w.e);
    tctx.fillStyle = w.covered ? '#33333a' : '#6a3a14';
    tctx.fillRect(x0, wy, Math.max(x1 - x0 - 1, 1), wh);
    tctx.save(); tctx.beginPath(); tctx.rect(x0, wy, Math.max(x1 - x0 - 1, 1), wh); tctx.clip();
    tctx.fillStyle = '#ddd'; tctx.fillText(w.w, x0 + 2, wy + 21); tctx.restore();
  }
  // rows
  const [ry, rh] = LANE.rows;
  tctx.font = 'bold 13px "Hiragino Sans", sans-serif';
  rows.forEach((r, i) => {
    const se = shownEnd(r, i);
    if (se < viewStart || r.start > viewStart + L) return;
    const x0 = t2x(r.start), x1 = t2x(r.end), xs = t2x(se);
    const isSel = sel.has(i);
    const low = r.match != null && r.match < 0.5;
    tctx.fillStyle = 'rgba(255,59,112,0.12)';
    tctx.fillRect(x0, ry + (i % 2) * 8, Math.max(x1 - x0, 2), rh - 8);
    tctx.fillStyle = isSel ? '#2b6cff' : (low ? '#8a5a00' : '#b8234f');
    tctx.fillRect(x0, ry + (i % 2) * 8, Math.max(xs - x0, 2), rh - 8);
    tctx.fillStyle = '#fff';
    tctx.fillRect(x0, ry + (i % 2) * 8, 2, rh - 8);
    tctx.fillRect(x1 - 2, ry + (i % 2) * 8 + (rh - 8) / 2 - 8, 2, 16);
    tctx.save(); tctx.beginPath(); tctx.rect(x0, ry, Math.max(xs - x0, 2), rh); tctx.clip();
    tctx.fillText(`${i + 1} ${r.line}`, x0 + 5, ry + (i % 2) * 8 + 20); tctx.restore();
  });
  // playhead
  const px = t2x(audio.currentTime);
  tctx.fillStyle = '#fff'; tctx.fillRect(px - 1, 0, 2, H);
  drawOverview();
}
function drawOverview() {
  resizeCanvas(ov);
  const W = ov.clientWidth, H = ov.clientHeight;
  octx.clearRect(0, 0, W, H);
  if (!duration) return;
  if (peaks) { octx.fillStyle = '#2d4f7a'; for (let x = 0; x < W; x++) { const v = peaks[Math.floor(x / W * peaks.length)] || 0; octx.fillRect(x, H - v * H, 1, v * H); } }
  octx.fillStyle = 'rgba(255,59,112,0.7)';
  rows.forEach((r, i) => octx.fillRect(r.start / duration * W, 0, Math.max((shownEnd(r, i) - r.start) / duration * W, 1), 5));
  octx.fillStyle = 'rgba(255,122,26,0.9)';
  gaps.forEach(g => { if (!g.ignored) octx.fillRect(g.s / duration * W, 6, Math.max((g.e - g.s) / duration * W, 2), 5); });
  octx.strokeStyle = '#fff'; octx.strokeRect(viewStart / duration * W, 0.5, viewLen() / duration * W, H - 1);
  octx.fillStyle = '#fff'; octx.fillRect(audio.currentTime / duration * W, 0, 1, H);
}
ov.onpointerdown = e => {
  const move = ev => { const t = (ev.clientX - ov.getBoundingClientRect().left) / ov.clientWidth * duration; viewStart = clamp(t - viewLen() / 2, 0, Math.max(duration - viewLen(), 0)); drawTimeline(); };
  move(e); ov.setPointerCapture(e.pointerId); ov.onpointermove = move; ov.onpointerup = () => { ov.onpointermove = null; };
};
$('zoom').oninput = e => { $('zoomVal').textContent = e.target.value; drawTimeline(); };
tl.addEventListener('wheel', e => {
  e.preventDefault();
  if (e.ctrlKey || e.metaKey) {
    const t = x2t(e.offsetX); const z = clamp(viewLen() * (e.deltaY > 0 ? 1.15 : 0.87), 4, 60);
    $('zoom').value = z; $('zoomVal').textContent = Math.round(z);
    viewStart = clamp(t - e.offsetX / tl.clientWidth * z, 0, Math.max(duration - z, 0));
  } else {
    viewStart = clamp(viewStart + (e.deltaX || e.deltaY) / tl.clientWidth * viewLen(), 0, Math.max(duration - viewLen(), 0));
  }
  drawTimeline();
}, {passive: false});

function snap(t) {
  if (!$('snapBeats').checked || !beats.length) return t;
  let best = t, bd = 0.08;
  for (const b of beats) { const d = Math.abs(b - t); if (d < bd) { bd = d; best = b; } }
  return best;
}
function hitRow(x, y) {
  const [ry, rh] = LANE.rows;
  if (y < ry || y > ry + rh) return null;
  for (let i = rows.length - 1; i >= 0; i--) {
    const r = rows[i]; const x0 = t2x(r.start), x1 = t2x(r.end), xs = t2x(shownEnd(r, i));
    if (Math.abs(x - x0) <= 5) return {i, part: 'start'};
    if (Math.abs(x - x1) <= 5) return {i, part: 'end'};
    if (x > x0 && x < Math.max(xs, x0 + 6)) return {i, part: 'body'};
  }
  return null;
}
tl.onpointermove = e => {
  if (drag) return;
  const h = hitRow(e.offsetX, e.offsetY);
  tl.style.cursor = !h ? 'crosshair' : h.part === 'body' ? 'grab' : 'ew-resize';
};
let drag = null;
tl.onpointerdown = e => {
  const h = hitRow(e.offsetX, e.offsetY);
  if (!h) { audio.currentTime = clamp(x2t(e.offsetX), 0, duration); requestPreview(); drawTimeline(); return; }
  selectRow(h.i, e.shiftKey, e.metaKey || e.ctrlKey);
  const r = rows[h.i];
  drag = {h, t0: x2t(e.offsetX), start: r.start, end: r.end, moved: false};
  tl.setPointerCapture(e.pointerId);
  tl.onpointermove = ev => {
    const dt = x2t(ev.offsetX) - drag.t0;
    if (Math.abs(dt) > 0.005) drag.moved = true;
    const rr = rows[drag.h.i];
    if (drag.h.part === 'start') rr.start = clamp(snap(drag.start + dt), 0, rr.end - 0.05);
    else if (drag.h.part === 'end') rr.end = clamp(drag.end + dt, rr.start + 0.05, duration);
    else { const s = clamp(snap(drag.start + dt), 0, duration); rr.end = s + (drag.end - drag.start); rr.start = s; }
    drawTimeline();
  };
  tl.onpointerup = () => {
    tl.onpointermove = null; tl.onpointerup = null;
    if (drag.moved) { sortRows(); commit(); audio.currentTime = rows[[...sel][0]]?.start ?? audio.currentTime; requestPreview(); }
    else { audio.currentTime = r.start; requestPreview(); }
    drag = null;
  };
};

// ---------- table ----------
function sectionOptions(cur) {
  const secs = [...new Set(noteLines.map(n => n.section))];
  if (cur && !secs.includes(cur)) secs.push(cur);
  return secs.map(s => `<option ${s === cur ? 'selected' : ''} value="${esc(s)}">${esc(s || '（なし）')}</option>`).join('');
}
function renderTable() {
  const tb = $('rows');
  tb.innerHTML = rows.map((r, i) => `
    <tr data-i="${i}" class="${sel.has(i) ? 'sel' : ''} ${r.match != null && r.match < 0.5 ? 'low' : ''}">
      <td>${i + 1}</td>
      <td><input class="t" type="number" step="0.01" data-f="start" value="${fmt(r.start)}"></td>
      <td><input class="t" type="number" step="0.01" data-f="end" value="${fmt(r.end)}"></td>
      <td class="text"><input data-f="line" value="${esc(r.line)}"></td>
      <td><select data-f="section">${sectionOptions(r.section ?? '')}</select></td>
      <td class="match">${r.match == null ? '—' : Math.round(r.match * 100) + '%'}</td>
      <td>
        <button class="small" data-a="play" title="この行から再生">▶</button>
        <button class="small" data-a="here" title="開始を再生位置に">⇤今</button>
        <button class="small" data-a="split" title="カーソル位置で分割">✂</button>
        <button class="small" data-a="merge" title="次の行と結合">⤓</button>
        <button class="small" data-a="dup" title="再生位置に複製">⎘</button>
        <button class="small" data-a="del" title="削除">🗑</button>
      </td>
    </tr>`).join('');
  $('selInfo').textContent = sel.size ? `${sel.size}行選択中` : '';
}
$('rows').addEventListener('change', e => {
  const tr = e.target.closest('tr'); if (!tr) return;
  const i = +tr.dataset.i, f = e.target.dataset.f;
  if (f === 'start' || f === 'end') rows[i][f] = clamp(parseFloat(e.target.value) || 0, 0, duration || 1e9);
  else if (f === 'line') { rows[i].line = e.target.value; rows[i].match = null; }
  else if (f === 'section') rows[i].section = e.target.value;
  if (f === 'start') sortRows();
  commit(); requestPreview();
});
$('rows').addEventListener('click', e => {
  const tr = e.target.closest('tr'); if (!tr) return;
  const i = +tr.dataset.i;
  const a = e.target.dataset.a;
  if (!a) { if (e.target.tagName !== 'INPUT' && e.target.tagName !== 'SELECT') { selectRow(i, e.shiftKey, e.metaKey || e.ctrlKey); seek(rows[i].start); } return; }
  const r = rows[i];
  if (a === 'play') { seek(r.start); audio.play(); }
  else if (a === 'here') { r.start = audio.currentTime; if (r.end <= r.start) r.end = r.start + 1; sortRows(); commit(); }
  else if (a === 'split') {
    const inp = tr.querySelector('input[data-f=line]');
    let pos = inp.selectionStart; if (!pos || pos >= r.line.length) pos = Math.ceil(r.line.length / 2);
    const a1 = r.line.slice(0, pos).trim(), a2 = r.line.slice(pos).trim();
    const mid = r.start + (r.end - r.start) * pos / Math.max(r.line.length, 1);
    rows.splice(i, 1, {...r, line: a1, end: mid, match: null}, {...r, line: a2, start: mid, match: null, src: null});
    commit();
  } else if (a === 'merge') {
    const n = rows[i + 1]; if (!n) return;
    rows.splice(i, 2, {...r, line: r.line + '　' + n.line, end: n.end, match: null});
    commit();
  } else if (a === 'dup') {
    const len = r.end - r.start;
    rows.push({...r, start: audio.currentTime, end: audio.currentTime + len, match: null});
    sortRows(); commit();
  } else if (a === 'del') { rows.splice(i, 1); sel.clear(); commit(); }
  requestPreview();
});
function selectRow(i, range, toggle) {
  if (range && anchorSel >= 0) { sel = new Set(); for (let k = Math.min(anchorSel, i); k <= Math.max(anchorSel, i); k++) sel.add(k); }
  else if (toggle) { sel.has(i) ? sel.delete(i) : sel.add(i); anchorSel = i; }
  else { sel = new Set([i]); anchorSel = i; }
  refresh(false);
  const tr = document.querySelector(`#rows tr[data-i="${i}"]`); if (tr) tr.scrollIntoView({block: 'nearest'});
}
$('addBtn').onclick = () => {
  const t = audio.currentTime;
  const prev = [...rows].reverse().find(r => r.start <= t);
  rows.push({line: '（歌詞）', start: t, end: t + 1.5, src: null, match: null, section: prev ? prev.section : ''});
  sortRows(); commit();
  const i = rows.findIndex(r => r.start === t && r.line === '（歌詞）');
  sel = new Set([i]); refresh();
  const inp = document.querySelector(`#rows tr[data-i="${i}"] input[data-f=line]`); if (inp) { inp.focus(); inp.select(); }
};
$('copySelBtn').onclick = () => {
  if (!sel.size) return alert('コピーする行を選択してください（⇧クリックで範囲選択）');
  const src = [...sel].sort((a, b) => a - b).map(i => rows[i]);
  const off = audio.currentTime - src[0].start;
  const added = src.map(r => ({...r, start: r.start + off, end: r.end + off, match: null}));
  rows.push(...added); sortRows();
  sel = new Set(added.map(a => rows.indexOf(a)));
  commit(); requestPreview();
};
$('delSelBtn').onclick = () => {
  if (!sel.size) return;
  rows = rows.filter((_, i) => !sel.has(i)); sel.clear(); commit(); requestPreview();
};

// ---------- issues (抜け・漏れ) ----------
let gaps = [];
function computeIssues() {
  const spans = rows.map((r, i) => [r.start, shownEnd(r, i)]);
  for (const w of words) {
    const mid = (w.s + w.e) / 2;
    w.covered = spans.some(([s, e]) => mid >= s - 0.15 && mid <= e + 0.15);
  }
  gaps = [];
  let cur = null;
  for (const w of words) {
    if (w.covered || w.e - w.s < 0.02) { if (cur) { gaps.push(cur); cur = null; } continue; }
    if (cur && w.s - cur.e < 1.0) { cur.e = w.e; cur.text += w.w; }
    else { if (cur) gaps.push(cur); cur = {s: w.s, e: w.e, text: w.w}; }
  }
  if (cur) gaps.push(cur);
  gaps = gaps.filter(g => norm(g.text).length >= 2);
  gaps.forEach(g => {
    g.key = `${g.s.toFixed(1)}-${norm(g.text)}`;
    g.ignored = ignoredGaps.includes(g.key);
    // ノートの連続した1〜6行をまとめた候補（サビの繰り返しなど、数行分の抜けに対応）
    const cands = [];
    for (let k = 0; k < noteLines.length; k++) {
      let joined = '';
      for (let n = 1; n <= 6 && k + n <= noteLines.length; n++) {
        joined += noteLines[k + n - 1].text;
        cands.push({k, n, text: n === 1 ? noteLines[k].text : `${noteLines[k].text} … ${noteLines[k + n - 1].text}（${n}行）`,
                    section: noteLines[k].section, score: sim(joined, g.text)});
      }
    }
    g.cands = cands.sort((a, b) => b.score - a.score || a.n - b.n).slice(0, 4);
  });
  const sus = [];
  rows.forEach((r, i) => {
    const se = shownEnd(r, i);
    const heard = words.filter(w => w.e > r.start && w.s < se).map(w => w.w).join('');
    let reason = null;
    if (!heard) reason = 'この時間に歌が聞き取れていない';
    else if (r.match != null && r.match < 0.5) reason = `認識との一致 ${Math.round(r.match * 100)}%`;
    else if (sim(r.line, heard) < 0.2 && norm(heard).length >= 3) reason = '聞こえた言葉と違う';
    if (reason) sus.push({i, r, heard, reason});
  });
  const used = new Set(rows.map(r => r.src).filter(v => v != null));
  const texts = new Set(rows.map(r => norm(r.line)));
  const unused = noteLines.map((n, k) => ({k, ...n})).filter(n => !used.has(n.k) && !texts.has(norm(n.text)));
  return {sus, unused};
}
function renderIssues(issues) {
  const open = gaps.filter(g => !g.ignored);
  $('missCount').textContent = `(${open.length})`;
  $('missList').innerHTML = open.map((g, gi) => `
    <div class="issue" data-g="${gaps.indexOf(g)}">
      <span class="t">${fmt(g.s)}–${fmt(g.e)}</span> <button class="small" data-a="hear">▶</button>
      <button class="small" data-a="ignore" title="歌詞ではない（掛け声など）">無視</button><br>
      <span class="heard">聞こえた：${esc(g.text)}</span><br>
      ${g.cands.map(c => `<button class="small" data-a="fill" data-k="${c.k}" data-n="${c.n}" title="${esc(c.section)}">＋ ${esc(c.text)} <span class="hint">${Math.round(c.score * 100)}%</span></button>`).join(' ')}
      <button class="small" data-a="fillText" title="聞こえた言葉のまま追加">＋ 聞こえたまま</button>
    </div>`).join('') || '<div class="hint">なし</div>';
  $('susCount').textContent = `(${issues.sus.length})`;
  $('susList').innerHTML = issues.sus.map(s => `
    <div class="issue" data-i="${s.i}">
      <span class="t">#${s.i + 1} ${fmt(s.r.start)}</span> <button class="small" data-a="hear">▶</button>
      <button class="small" data-a="pick">選択</button> <button class="small" data-a="del">🗑</button><br>
      ${esc(s.r.line)} <span class="hint">— ${esc(s.reason)}</span><br>
      <span class="heard">聞こえた：${esc(s.heard || '（なし）')}</span>
    </div>`).join('') || '<div class="hint">なし</div>';
  $('unusedCount').textContent = `(${issues.unused.length})`;
  $('unusedList').innerHTML = issues.unused.map(n => `
    <div class="issue" data-k="${n.k}">
      <span class="t">ノート${n.k + 1}行目</span> <span class="hint">${esc(n.section)}</span><br>
      ${esc(n.text)} <button class="small" data-a="restore">＋ 再生位置に戻す</button>
    </div>`).join('') || '<div class="hint">なし</div>';
}
$('missList').onclick = e => {
  const box = e.target.closest('.issue'); const a = e.target.closest('button')?.dataset.a; if (!box || !a) return;
  const g = gaps[+box.dataset.g];
  if (a === 'hear') { seek(Math.max(g.s - 0.5, 0)); audio.play(); return; }
  if (a === 'ignore') { ignoredGaps.push(g.key); commit(); return; }
  let add;
  if (a === 'fill') {
    const btn = e.target.closest('button'); const k = +btn.dataset.k, n = +btn.dataset.n;
    const src = noteLines.slice(k, k + n);
    const total = src.reduce((acc, x) => acc + Math.max(norm(x.text).length, 1), 0);
    const span = Math.max(g.e - g.s, 0.6 * n);
    let cur = g.s;
    add = src.map((x, j) => {
      const len = span * Math.max(norm(x.text).length, 1) / total;
      const row = {line: x.text, section: x.section, src: k + j, start: cur, end: cur + len, match: null};
      cur += len; return row;
    });
  } else {
    const prev = [...rows].reverse().find(r => r.start <= g.s);
    add = [{line: g.text, section: prev ? prev.section : '', src: null, start: g.s, end: Math.max(g.e, g.s + 0.6), match: null}];
  }
  rows.push(...add); sortRows();
  sel = new Set(add.map(r => rows.indexOf(r)));
  commit(); seek(g.s);
};
$('susList').onclick = e => {
  const box = e.target.closest('.issue'); const a = e.target.closest('button')?.dataset.a; if (!box || !a) return;
  const i = +box.dataset.i;
  if (a === 'hear') { seek(Math.max(rows[i].start - 0.5, 0)); audio.play(); }
  else if (a === 'pick') { selectRow(i); seek(rows[i].start); }
  else if (a === 'del') { rows.splice(i, 1); sel.clear(); commit(); }
};
$('unusedList').onclick = e => {
  const box = e.target.closest('.issue'); if (e.target.dataset.a !== 'restore') return;
  const k = +box.dataset.k; const t = audio.currentTime;
  rows.push({line: noteLines[k].text, section: noteLines[k].section, src: k, start: t, end: t + 1.5, match: null});
  sortRows(); sel = new Set([rows.findIndex(r => r.src === k && r.start === t)]); commit();
};

// ---------- preview ----------
let previewBusy = false, previewPending = false, lastPreviewT = -1;
async function requestPreview(force) {
  if (!rows.length || !$('dropImage').classList.contains('ready')) return;
  if (previewBusy) { previewPending = true; return; }
  previewBusy = true;
  const t = audio.currentTime;
  try {
    const r = await fetch('/preview', {method: 'POST', body: JSON.stringify({alignment: rows, t, style: $('styleSelect').value})});
    if (r.ok) {
      const url = URL.createObjectURL(await r.blob());
      const img = $('previewImg'); const old = img.src; img.src = url; if (old.startsWith('blob:')) URL.revokeObjectURL(old);
      $('previewNote').textContent = `プレビュー ${fmt(t)}s`;
      lastPreviewT = t;
    } else {
      $('previewNote').textContent = 'プレビュー失敗: ' + ((await r.json()).error || '').trim().split('\n').pop();
    }
  } finally {
    previewBusy = false;
    if (previewPending) { previewPending = false; requestPreview(); }
  }
}
$('styleSelect').onchange = () => { refresh(); requestPreview(); };

// ---------- transport ----------
function seek(t) { audio.currentTime = clamp(t, 0, duration || t); const L = viewLen(); if (t < viewStart || t > viewStart + L) viewStart = clamp(t - L * 0.2, 0, Math.max(duration - L, 0)); requestPreview(); drawTimeline(); }
$('playBtn').onclick = () => audio.paused ? audio.play() : audio.pause();
audio.onplay = () => $('playBtn').textContent = '⏸ 停止';
audio.onpause = () => { $('playBtn').textContent = '▶ 再生'; requestPreview(); };
let lastLive = 0;
function tick(ts) {
  $('time').textContent = `${fmt(audio.currentTime)} / ${fmt(duration)}`;
  followPlayhead();
  drawTimeline();
  const t = audio.currentTime;
  document.querySelectorAll('#rows tr.playing').forEach(tr => tr.classList.remove('playing'));
  const pi = rows.findIndex((r, i) => t >= r.start && t < shownEnd(r, i));
  if (pi >= 0) document.querySelector(`#rows tr[data-i="${pi}"]`)?.classList.add('playing');
  if (!audio.paused && $('livePreview').checked && ts - lastLive > 180) { lastLive = ts; requestPreview(); }
  requestAnimationFrame(tick);
}

document.addEventListener('keydown', e => {
  const tag = e.target.tagName;
  const mod = e.metaKey || e.ctrlKey;
  if (mod && e.key.toLowerCase() === 's') { e.preventDefault(); save(); return; }
  if (mod && e.key.toLowerCase() === 'z' && tag !== 'INPUT') { e.preventDefault(); e.shiftKey ? redo() : undo(); return; }
  if (tag === 'INPUT' || tag === 'SELECT') { if (e.key === 'Enter') e.target.blur(); return; }
  const cur = sel.size ? Math.min(...sel) : -1;
  const nudge = (d, startOnly) => {
    if (cur < 0) return;
    for (const i of sel) { rows[i].start = Math.max(rows[i].start + d, 0); if (!startOnly) rows[i].end += d; if (rows[i].end <= rows[i].start) rows[i].end = rows[i].start + 0.1; }
    sortRows(); commit(); seek(rows[Math.min(...sel)].start);
  };
  switch (e.key) {
    case ' ': e.preventDefault(); $('playBtn').click(); break;
    case 'Enter': if (cur >= 0) { seek(rows[cur].start); audio.play(); } break;
    case 't': case 'T': {
      e.preventDefault();
      const i = cur >= 0 ? cur : rows.findIndex(r => r.start > audio.currentTime);
      if (i < 0) break;
      const now = audio.currentTime;
      rows[i].start = now;
      if (rows[i].end <= now) rows[i].end = now + 0.8;
      if (i > 0 && rows[i - 1].end > now) rows[i - 1].end = now;
      commit(); sel = new Set([Math.min(i + 1, rows.length - 1)]); anchorSel = Math.min(i + 1, rows.length - 1); refresh(false);
      break;
    }
    case 'ArrowUp': e.preventDefault(); if (cur > 0) { selectRow(cur - 1); seek(rows[cur - 1].start); } break;
    case 'ArrowDown': e.preventDefault(); if (cur < rows.length - 1) { selectRow(cur + 1); seek(rows[cur + 1].start); } break;
    case 'ArrowLeft': e.preventDefault(); nudge(e.shiftKey ? -0.5 : -0.05, false); break;
    case 'ArrowRight': e.preventDefault(); nudge(e.shiftKey ? 0.5 : 0.05, false); break;
    case '[': nudge(-0.05, true); break;
    case ']': nudge(0.05, true); break;
    case ',': seek(audio.currentTime - 1); break;
    case '.': seek(audio.currentTime + 1); break;
    case 'Delete': case 'Backspace': if (sel.size) { e.preventDefault(); $('delSelBtn').click(); } break;
  }
});
$('undoBtn').onclick = undo; $('redoBtn').onclick = redo;

// ---------- render ----------
async function startRender(stills) {
  if (dirty && !(await save())) return;
  const st = $('renderStatus');
  $('downloadLink').classList.add('hidden');
  const r = await fetch('/render/run', {method: 'POST', body: JSON.stringify({style: $('styleSelect').value, stills})});
  if (!r.ok) { st.textContent = (await r.json()).error; return; }
  st.textContent = stills ? '静止画を作成中…' : '書き出し中… 0%';
  const poll = setInterval(async () => {
    const s = await (await fetch('/render/status')).json();
    if (s.status === 'running' && !stills) st.textContent = `書き出し中… ${Math.round(s.progress * 100)}%`;
    if (s.status === 'done') {
      clearInterval(poll);
      if (stills) {
        st.textContent = '✅ 静止画チェック（各カットの 0/25/50/75/97%）';
        $('stills').innerHTML = s.stills.map(n => `<a href="/stills?name=${encodeURIComponent(n)}" target="_blank"><img src="/stills?name=${encodeURIComponent(n)}&v=${Date.now()}"></a>`).join('');
      } else { st.textContent = '✅ ' + s.output; $('downloadLink').classList.remove('hidden'); }
    }
    if (s.status === 'error') { clearInterval(poll); st.textContent = '❌ ' + (s.error || '').trim().split('\n').pop(); }
  }, 1500);
}
$('stillsBtn').onclick = () => startRender(true);
$('renderBtn').onclick = () => startRender(false);

// ---------- refresh ----------
function refresh(rebuildTable = true) {
  const issues = computeIssues();
  if (rebuildTable) renderTable(); else document.querySelectorAll('#rows tr').forEach(tr => tr.classList.toggle('sel', sel.has(+tr.dataset.i)));
  $('selInfo').textContent = sel.size ? `${sel.size}行選択中` : '';
  renderIssues(issues);
  drawTimeline();
}
window.addEventListener('resize', () => drawTimeline());
loadConfig().then(() => requestAnimationFrame(tick));
</script>
</body>
</html>
"""


def main():
    parser = argparse.ArgumentParser(description="歌詞動画エディタ（ローカルGUI）")
    parser.add_argument("--port", type=int, default=8765)
    parser.add_argument("--audio", help="起動時にセットする音源")
    parser.add_argument("--image", help="起動時にセットする背景画像")
    parser.add_argument("--song", help="起動時にセットする曲ノート（01_Songs/ 配下）")
    parser.add_argument("--no-browser", action="store_true", help="ブラウザを自動で開かない")
    args = parser.parse_args()

    if args.audio:
        STATE["audio_path"] = str(Path(args.audio).resolve())
    if args.image:
        STATE["image_path"] = str(Path(args.image).resolve())
    if args.song:
        song = Path(args.song)
        if not song.is_absolute() and not song.exists():
            song = SONGS_DIR / song.name
        STATE["song_path"] = str(song.resolve())

    server = ThreadingHTTPServer(("127.0.0.1", args.port), Handler)
    url = f"http://127.0.0.1:{args.port}/"
    print(f"[INFO] GUIを起動しました: {url}  （Ctrl+C で終了）", flush=True)
    if not args.no_browser:
        threading.Timer(0.5, lambda: webbrowser.open(url)).start()
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\n[INFO] 終了します。")


if __name__ == "__main__":
    main()
