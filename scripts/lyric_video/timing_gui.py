#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
timing_gui.py
歌詞動画を「アップロード→タイミング確認・修正→スタイル/フォント選択→書き出し」
まで一通りブラウザだけで完結させるローカルエディタ。

lyricflow（Base44製の元アプリ）のEditorページにあった「音源アップロード」
「アニメーション/テンプレート選択」「文字（フォント）選択」といった機能を、
このリポジトリの既存モジュール（song_note.py/align.py/beats.py/render.py/
styles.py）の上にGUIとして被せたもの。認証・課金・クラウド保存は一切ない
（ローカル単一ユーザー前提のため不要）。

外部ライブラリ・追加のpip依存は使わない
（Python標準ライブラリのhttp.serverのみ + ブラウザ標準のWeb Audio API）。
波形描画もブラウザ側でWeb Audio APIのdecodeAudioDataから直接行う。

--------------------------------------------------------------------------
使い方:
    python3 scripts/lyric_video/timing_gui.py [--port 8765]

    音声・画像・曲ノートは起動後にブラウザ側からアップロード/選択する
    （CLI引数での指定は不要。以前のように --audio/--image を渡して
     起動することもできるが、その場合は「すでにアライメント済み」の
     組み合わせを直接開く用途に限る）。

画面の流れ:
    1. 音声ファイルをドラッグ&ドロップ（またはファイル選択）でアップロード
    2. 背景画像も同様にアップロード
    3. 01_Songs/ の中から曲ノートを選ぶ（歌詞・BPMを読み込む）
    4. 「アライメント実行」でwhisper文字起こし+ビート検出を実行
       （数分かかる。進捗はポーリングで表示）
    5. 波形を見ながら再生し、ズレていれば行ごとに
       start/endを直接編集するか「▶ここを開始に」で修正
    6. スタイル（kawaii/deathcore/kawaii-deathcore-wametal）と
       フォントを選び、プレビューで見た目を確認
    7. 「最終動画を書き出す」でmp4を生成し、ダウンロード
--------------------------------------------------------------------------
"""

import argparse
import json
import sys
import threading
import traceback
import uuid
import webbrowser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlparse, parse_qs

sys.path.insert(0, str(Path(__file__).resolve().parent))

from align import _audio_hash, align_lyrics, WORK_DIR
from beats import detect_beats
from render import render_video
from song_note import SongNote
from styles import STYLES, DEFAULT_STYLE

SCRIPT_DIR = Path(__file__).resolve().parent
VAULT_ROOT = SCRIPT_DIR.parent.parent
SONGS_DIR = VAULT_ROOT / "01_Songs"
UPLOAD_DIR = SCRIPT_DIR / "_work" / "uploads"

MIME_TYPES = {
    ".mp3": "audio/mpeg", ".wav": "audio/wav", ".m4a": "audio/mp4",
    ".jpg": "image/jpeg", ".jpeg": "image/jpeg", ".png": "image/png",
    ".mp4": "video/mp4",
}

# フォント選択肢。すべてこのMac(/System/Library/Fonts/)に実在するもののみ。
FONT_CHOICES = {
    "極太ゴシック (W9・デフォルト)": "/System/Library/Fonts/ヒラギノ角ゴシック W9.ttc",
    "太めゴシック (W6)": "/System/Library/Fonts/ヒラギノ角ゴシック W6.ttc",
    "レギュラーゴシック (W3)": "/System/Library/Fonts/ヒラギノ角ゴシック W3.ttc",
    "丸ゴシック (kawaii向き)": "/System/Library/Fonts/ヒラギノ丸ゴ ProN W4.ttc",
    "明朝体": "/System/Library/Fonts/ヒラギノ明朝 ProN.ttc",
}

# サーバー側の状態（このプロセス内で単一セッション分だけ保持すれば足りる、
# ローカル単一ユーザーツールのため）
STATE = {
    "audio_path": None,
    "image_path": None,
    "song_path": None,
    "align_status": "idle",   # idle | running | done | error
    "align_error": None,
    "render_status": "idle",
    "render_error": None,
    "render_output": None,
}


def _list_songs():
    if not SONGS_DIR.exists():
        return []
    return sorted(p.name for p in SONGS_DIR.glob("*.md") if not p.name.startswith("_"))


def _alignment_path_for(audio_path):
    audio_hash = _audio_hash(Path(audio_path))
    return WORK_DIR / audio_hash / "alignment.json", WORK_DIR / audio_hash / "beats.json"


def _run_alignment():
    STATE["align_status"] = "running"
    STATE["align_error"] = None
    try:
        note = SongNote(STATE["song_path"])
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


def _run_render(style_name, font_path):
    STATE["render_status"] = "running"
    STATE["render_error"] = None
    try:
        note = SongNote(STATE["song_path"])
        alignment_path, beats_path = _alignment_path_for(STATE["audio_path"])
        alignment = json.loads(alignment_path.read_text(encoding="utf-8"))["alignment"]
        beats = json.loads(beats_path.read_text(encoding="utf-8"))["beats"]
        style = STYLES[style_name]
        out_dir = alignment_path.parent
        output_path = out_dir / f"gui_render_{style_name}.mp4"
        render_video(
            image_path=STATE["image_path"],
            audio_path=STATE["audio_path"],
            alignment=alignment,
            beats=beats,
            style=style,
            output_path=output_path,
            font_path=font_path,
        )
        STATE["render_output"] = str(output_path)
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
        self.send_header("Content-Type", "application/json")
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def _bytes(self, data, content_type):
        self.send_response(200)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Access-Control-Allow-Origin", "*")
        self.end_headers()
        self.wfile.write(data)

    def _file(self, path):
        """HTTP Rangeリクエストに対応したファイル配信。
        これが無いと、ブラウザの<audio>要素は音声を「シーク不可
        (seekable=[0,0])」と判断し、波形クリックや再生位置ジャンプが
        一切効かなくなる（実機で確認済みの不具合）。"""
        path = Path(path)
        ext = path.suffix.lower()
        content_type = MIME_TYPES.get(ext, "application/octet-stream")
        size = path.stat().st_size
        range_header = self.headers.get("Range")

        if not range_header:
            self.send_response(200)
            self.send_header("Content-Type", content_type)
            self.send_header("Content-Length", str(size))
            self.send_header("Accept-Ranges", "bytes")
            self.send_header("Access-Control-Allow-Origin", "*")
            self.end_headers()
            with open(path, "rb") as f:
                self.wfile.write(f.read())
            return

        # "bytes=START-END" 形式（ENDは省略されることが多い）
        try:
            unit, _, range_spec = range_header.partition("=")
            start_str, _, end_str = range_spec.partition("-")
            start = int(start_str) if start_str else 0
            end = int(end_str) if end_str else size - 1
            end = min(end, size - 1)
        except ValueError:
            start, end = 0, size - 1

        length = end - start + 1
        self.send_response(206)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(length))
        self.send_header("Content-Range", f"bytes {start}-{end}/{size}")
        self.send_header("Accept-Ranges", "bytes")
        self.send_header("Access-Control-Allow-Origin", "*")
        self.end_headers()
        with open(path, "rb") as f:
            f.seek(start)
            self.wfile.write(f.read(length))

    def do_GET(self):
        parsed = urlparse(self.path)
        path, query = parsed.path, parse_qs(parsed.query)

        if path == "/":
            self._bytes(HTML.encode("utf-8"), "text/html; charset=utf-8")
        elif path == "/config":
            self._json({
                "styles": STYLES,
                "default_style": DEFAULT_STYLE,
                "fonts": FONT_CHOICES,
                "songs": _list_songs(),
                "state": {
                    "audio_ready": STATE["audio_path"] is not None,
                    "image_ready": STATE["image_path"] is not None,
                    "song_path": STATE["song_path"],
                },
            })
        elif path == "/audio":
            if STATE["audio_path"]:
                self._file(STATE["audio_path"])
            else:
                self.send_response(404); self.end_headers()
        elif path == "/image":
            if STATE["image_path"]:
                self._file(STATE["image_path"])
            else:
                self.send_response(404); self.end_headers()
        elif path == "/alignment":
            if not STATE["audio_path"]:
                self._json({"error": "audio not set"}, 400); return
            alignment_path, _ = _alignment_path_for(STATE["audio_path"])
            if not alignment_path.exists():
                self._json([]); return
            cache = json.loads(alignment_path.read_text(encoding="utf-8"))
            self._json(cache["alignment"])
        elif path == "/align/status":
            self._json({"status": STATE["align_status"], "error": STATE["align_error"]})
        elif path == "/render/status":
            self._json({"status": STATE["render_status"], "error": STATE["render_error"]})
        elif path == "/render/download":
            if STATE["render_output"] and Path(STATE["render_output"]).exists():
                self._file(STATE["render_output"])
            else:
                self.send_response(404); self.end_headers()
        else:
            self.send_response(404); self.end_headers()

    def do_POST(self):
        parsed = urlparse(self.path)
        path, query = parsed.path, parse_qs(parsed.query)
        length = int(self.headers.get("Content-Length", 0))
        body = self.rfile.read(length) if length else b""

        if path == "/upload/audio":
            ext = query.get("ext", [".mp3"])[0]
            UPLOAD_DIR.mkdir(parents=True, exist_ok=True)
            dest = UPLOAD_DIR / f"audio_{uuid.uuid4().hex[:8]}{ext}"
            dest.write_bytes(body)
            STATE["audio_path"] = str(dest)
            self._json({"ok": True, "path": str(dest)})
        elif path == "/upload/image":
            ext = query.get("ext", [".jpg"])[0]
            UPLOAD_DIR.mkdir(parents=True, exist_ok=True)
            dest = UPLOAD_DIR / f"image_{uuid.uuid4().hex[:8]}{ext}"
            dest.write_bytes(body)
            STATE["image_path"] = str(dest)
            self._json({"ok": True, "path": str(dest)})
        elif path == "/song":
            name = query.get("name", [None])[0]
            if not name:
                self._json({"error": "name required"}, 400); return
            song_path = SONGS_DIR / name
            if not song_path.exists():
                self._json({"error": "not found"}, 404); return
            STATE["song_path"] = str(song_path)
            note = SongNote(song_path)
            self._json({"ok": True, "title": note.title, "bpm": note.bpm, "lines": len(note.lyric_lines)})
        elif path == "/alignment":
            if not STATE["audio_path"]:
                self._json({"error": "audio not set"}, 400); return
            alignment_path, _ = _alignment_path_for(STATE["audio_path"])
            new_alignment = json.loads(body.decode("utf-8"))
            cache = json.loads(alignment_path.read_text(encoding="utf-8")) if alignment_path.exists() else {}
            cache["alignment"] = new_alignment
            alignment_path.parent.mkdir(parents=True, exist_ok=True)
            alignment_path.write_text(json.dumps(cache, ensure_ascii=False, indent=2), encoding="utf-8")
            self._json({"ok": True})
        elif path == "/align/run":
            if not STATE["audio_path"] or not STATE["song_path"]:
                self._json({"error": "audio/song not set"}, 400); return
            threading.Thread(target=_run_alignment, daemon=True).start()
            self._json({"ok": True})
        elif path == "/render/run":
            if not STATE["audio_path"] or not STATE["image_path"]:
                self._json({"error": "audio/image not set"}, 400); return
            params = json.loads(body.decode("utf-8")) if body else {}
            style_name = params.get("style", DEFAULT_STYLE)
            font_path = params.get("font", FONT_CHOICES["極太ゴシック (W9・デフォルト)"])
            threading.Thread(target=_run_render, args=(style_name, font_path), daemon=True).start()
            self._json({"ok": True})
        else:
            self.send_response(404); self.end_headers()


HTML = """<!doctype html>
<html lang="ja">
<head>
<meta charset="utf-8">
<title>歌詞動画エディタ</title>
<style>
  body { font-family: -apple-system, sans-serif; background: #111; color: #eee; margin: 0; padding: 16px; }
  h1 { font-size: 14px; color: #888; font-weight: normal; }
  h2 { font-size: 13px; color: #aaa; margin: 18px 0 6px; border-top: 1px solid #333; padding-top: 12px; }
  .row { display: flex; gap: 12px; align-items: center; flex-wrap: wrap; margin: 6px 0; }
  .dropzone { border: 2px dashed #555; border-radius: 6px; padding: 14px; text-align: center;
              color: #888; cursor: pointer; flex: 1; min-width: 180px; }
  .dropzone.ready { border-color: #4a8; color: #4a8; }
  select, input[type=number] { background: #222; color: #eee; border: 1px solid #444; padding: 4px; }
  #preview { position: relative; width: 100%; max-width: 480px; aspect-ratio: 9/16; background: #000;
             background-size: cover; background-position: center; margin: 8px 0; border: 1px solid #333; }
  #caption { position: absolute; left: 50%; top: 72%; transform: translate(-50%, -50%); text-align: center;
             font-size: 28px; font-weight: 900; white-space: nowrap; transition: color 0.2s; }
  #waveform { width: 100%; height: 100px; background: #000; cursor: pointer; display: block; }
  #transport { margin: 8px 0; display: flex; align-items: center; gap: 12px; }
  #transport button { font-size: 16px; padding: 6px 14px; }
  #time { font-family: monospace; }
  table { width: 100%; border-collapse: collapse; font-size: 13px; }
  th, td { border-bottom: 1px solid #333; padding: 4px 6px; text-align: left; }
  tr.active { background: #2a2a2a; }
  input[type=number].t { width: 70px; }
  button.mark { background: #3b5; border: none; color: #000; padding: 3px 8px; cursor: pointer; }
  button.primary { background: #e63; border: none; color: #fff; padding: 8px 20px; font-size: 15px; cursor: pointer; }
  .status { color: #999; font-size: 12px; }
  .hidden { display: none; }
  a.dl { color: #4af; }
</style>
</head>
<body>
<h1>歌詞動画エディタ（ローカル） — スペースキーで再生/一時停止</h1>

<h2>1. 素材</h2>
<div class="row">
  <div class="dropzone" id="dropAudio">🎵 音声ファイルをドロップ / クリックして選択</div>
  <div class="dropzone" id="dropImage">🖼️ 背景画像をドロップ / クリックして選択</div>
  <input type="file" id="fileAudio" class="hidden" accept="audio/*">
  <input type="file" id="fileImage" class="hidden" accept="image/*">
</div>
<div class="row">
  <label>曲ノート: <select id="songSelect"><option value="">-- 選択 --</option></select></label>
  <span class="status" id="songStatus"></span>
</div>
<div class="row">
  <button class="primary" id="alignBtn">① アライメント実行（whisper文字起こし＋ビート検出）</button>
  <span class="status" id="alignStatus"></span>
</div>

<h2>2. タイミング確認・修正</h2>
<div id="preview"><div id="caption"></div></div>
<canvas id="waveform" height="100"></canvas>
<div id="transport">
  <button id="playBtn">▶ 再生</button>
  <span id="time">0.00 / 0.00</span>
  <button id="nudgeBack">-0.1s</button>
  <button id="nudgeFwd">+0.1s</button>
  <button class="primary" id="saveBtn">💾 タイミングを保存</button>
  <span class="status" id="saveStatus"></span>
</div>
<table>
  <thead><tr><th>#</th><th>start</th><th>end</th><th>歌詞</th><th></th></tr></thead>
  <tbody id="rows"></tbody>
</table>

<h2>3. スタイル・フォント</h2>
<div class="row">
  <label>スタイル: <select id="styleSelect"></select></label>
  <label>フォント: <select id="fontSelect"></select></label>
</div>

<h2>4. 書き出し</h2>
<div class="row">
  <button class="primary" id="renderBtn">② 最終動画を書き出す</button>
  <span class="status" id="renderStatus"></span>
  <a class="dl hidden" id="downloadLink" href="/render/download">⬇ 動画をダウンロード</a>
</div>

<audio id="audio" preload="auto"></audio>

<script>
const audio = document.getElementById('audio');
const preview = document.getElementById('preview');
const captionEl = document.getElementById('caption');
const canvas = document.getElementById('waveform');
const ctx = canvas.getContext('2d');
const rowsEl = document.getElementById('rows');
const timeEl = document.getElementById('time');

let CONFIG = null;
let alignment = [];
let duration = 1;
let peaks = null;

function fmt(t) { return t.toFixed(2); }

async function loadConfig() {
  CONFIG = await (await fetch('/config')).json();
  const songSelect = document.getElementById('songSelect');
  CONFIG.songs.forEach(name => {
    const opt = document.createElement('option');
    opt.value = name; opt.textContent = name;
    songSelect.appendChild(opt);
  });
  const styleSelect = document.getElementById('styleSelect');
  Object.keys(CONFIG.styles).forEach(name => {
    const opt = document.createElement('option');
    opt.value = name; opt.textContent = name;
    if (name === CONFIG.default_style) opt.selected = true;
    styleSelect.appendChild(opt);
  });
  const fontSelect = document.getElementById('fontSelect');
  Object.entries(CONFIG.fonts).forEach(([label, path]) => {
    const opt = document.createElement('option');
    opt.value = path; opt.textContent = label;
    fontSelect.appendChild(opt);
  });
  applyStylePreview();
  if (CONFIG.state.audio_ready) {
    document.getElementById('dropAudio').classList.add('ready');
    document.getElementById('dropAudio').textContent = '🎵 音声セット済み';
    audio.src = '/audio';
  }
  if (CONFIG.state.image_ready) {
    document.getElementById('dropImage').classList.add('ready');
    document.getElementById('dropImage').textContent = '🖼️ 画像セット済み';
    preview.style.backgroundImage = "url('/image')";
  }
}

function applyStylePreview() {
  const name = document.getElementById('styleSelect').value;
  const style = CONFIG.styles[name];
  if (!style) return;
  captionEl.style.color = style.caption_color;
  captionEl.style.webkitTextStroke = '2px ' + style.caption_stroke_color;
  const fontPath = document.getElementById('fontSelect').value;
  // ブラウザはローカルフォントファイルを直接指定できないため、太さの近さで代用表示のみ
  captionEl.style.fontWeight = fontPath.includes('W9') || fontPath.includes('W6') ? '900' : '400';
}
document.getElementById('styleSelect').addEventListener('change', applyStylePreview);
document.getElementById('fontSelect').addEventListener('change', applyStylePreview);

function currentMaxHold() {
  const name = document.getElementById('styleSelect').value;
  return (CONFIG.styles[name] || {}).max_hold_sec || 3.0;
}

// --- アップロード ---
function setupDrop(dropId, inputId, uploadUrl, extGuess, onDone) {
  const drop = document.getElementById(dropId);
  const input = document.getElementById(inputId);
  drop.addEventListener('click', () => input.click());
  drop.addEventListener('dragover', e => { e.preventDefault(); });
  drop.addEventListener('drop', e => {
    e.preventDefault();
    if (e.dataTransfer.files.length) handleFile(e.dataTransfer.files[0]);
  });
  input.addEventListener('change', () => { if (input.files.length) handleFile(input.files[0]); });

  async function handleFile(file) {
    drop.textContent = 'アップロード中...';
    const ext = '.' + (file.name.split('.').pop() || extGuess);
    const buf = await file.arrayBuffer();
    const res = await fetch(uploadUrl + '?ext=' + encodeURIComponent(ext), {
      method: 'POST', body: buf,
    });
    const data = await res.json();
    drop.classList.add('ready');
    drop.textContent = (dropId === 'dropAudio' ? '🎵' : '🖼️') + ' ' + file.name;
    onDone(data.path);
  }
}
setupDrop('dropAudio', 'fileAudio', '/upload/audio', 'mp3', () => { audio.src = '/audio?' + Date.now(); });
setupDrop('dropImage', 'fileImage', '/upload/image', 'jpg', () => { preview.style.backgroundImage = "url('/image?" + Date.now() + "')"; });

document.getElementById('songSelect').addEventListener('change', async (e) => {
  const name = e.target.value;
  if (!name) return;
  const res = await fetch('/song?name=' + encodeURIComponent(name), { method: 'POST' });
  const data = await res.json();
  document.getElementById('songStatus').textContent = data.ok
    ? `${data.title} (BPM:${data.bpm ?? '不明'} / ${data.lines}行)` : 'エラー';
});

// --- アライメント実行 ---
document.getElementById('alignBtn').addEventListener('click', async () => {
  const status = document.getElementById('alignStatus');
  status.textContent = '実行中...(数分かかります)';
  await fetch('/align/run', { method: 'POST' });
  const poll = setInterval(async () => {
    const s = await (await fetch('/align/status')).json();
    if (s.status === 'done') {
      clearInterval(poll);
      status.textContent = '✅ 完了';
      await loadAlignment();
    } else if (s.status === 'error') {
      clearInterval(poll);
      status.textContent = '❌ エラー: ' + (s.error || '').split('\\n').pop();
    }
  }, 2000);
});

async function loadAlignment() {
  const res = await fetch('/alignment');
  alignment = await res.json();
  renderRows();
  if (audio.src) await decodeWaveform();
}

async function decodeWaveform() {
  const buf = await (await fetch('/audio')).arrayBuffer();
  const actx = new (window.AudioContext || window.webkitAudioContext)();
  const audioBuffer = await actx.decodeAudioData(buf);
  duration = audioBuffer.duration;
  const raw = audioBuffer.getChannelData(0);
  const width = canvas.clientWidth || 1000;
  canvas.width = width;
  const step = Math.floor(raw.length / width) || 1;
  peaks = new Float32Array(width);
  for (let i = 0; i < width; i++) {
    let max = 0;
    const start = i * step;
    for (let j = 0; j < step; j++) {
      const v = Math.abs(raw[start + j] || 0);
      if (v > max) max = v;
    }
    peaks[i] = max;
  }
  drawWaveform();
}

function drawWaveform() {
  if (!peaks) return;
  const w = canvas.width, h = canvas.height;
  ctx.clearRect(0, 0, w, h);
  ctx.fillStyle = '#4af';
  for (let i = 0; i < peaks.length; i++) {
    const barH = peaks[i] * h;
    ctx.fillRect(i, (h - barH) / 2, 1, barH);
  }
  ctx.fillStyle = 'rgba(255,60,110,0.25)';
  alignment.forEach(item => {
    const x0 = (item.start / duration) * w;
    const x1 = (item.end / duration) * w;
    ctx.fillRect(x0, 0, Math.max(x1 - x0, 1), h);
  });
  const px = (audio.currentTime / duration) * w;
  ctx.fillStyle = '#fff';
  ctx.fillRect(px, 0, 2, h);
}

canvas.addEventListener('click', (e) => {
  const rect = canvas.getBoundingClientRect();
  const frac = (e.clientX - rect.left) / rect.width;
  audio.currentTime = frac * duration;
});

function renderRows() {
  rowsEl.innerHTML = '';
  alignment.forEach((item, i) => {
    const tr = document.createElement('tr');
    tr.id = 'row-' + i;
    tr.innerHTML = `
      <td>${i + 1}</td>
      <td><input type="number" class="t" step="0.01" value="${item.start.toFixed(2)}" data-field="start" data-i="${i}"></td>
      <td><input type="number" class="t" step="0.01" value="${item.end.toFixed(2)}" data-field="end" data-i="${i}"></td>
      <td>${item.line}</td>
      <td><button class="mark" data-i="${i}">▶ここを開始に</button></td>
    `;
    tr.addEventListener('click', (e) => {
      if (e.target.tagName !== 'INPUT' && e.target.tagName !== 'BUTTON') audio.currentTime = item.start;
    });
    rowsEl.appendChild(tr);
  });
  rowsEl.querySelectorAll('input').forEach(inp => {
    inp.addEventListener('change', (e) => {
      const i = parseInt(e.target.dataset.i);
      alignment[i][e.target.dataset.field] = parseFloat(e.target.value);
      drawWaveform();
    });
  });
  rowsEl.querySelectorAll('button.mark').forEach(btn => {
    btn.addEventListener('click', (e) => {
      const i = parseInt(e.target.dataset.i);
      alignment[i].start = audio.currentTime;
      document.querySelector(`#row-${i} input[data-field=start]`).value = audio.currentTime.toFixed(2);
      drawWaveform();
    });
  });
}

function updateActiveCaption() {
  const t = audio.currentTime;
  const maxHold = currentMaxHold();
  let active = null, activeIdx = -1;
  alignment.forEach((item, i) => {
    const shownEnd = Math.min(item.end, item.start + maxHold);
    if (t >= item.start && t < shownEnd) { active = item; activeIdx = i; }
  });
  captionEl.textContent = active ? active.line : '';
  rowsEl.querySelectorAll('tr').forEach(tr => tr.classList.remove('active'));
  if (activeIdx >= 0) {
    const row = document.getElementById('row-' + activeIdx);
    if (row) row.classList.add('active');
  }
}

function tick() {
  timeEl.textContent = fmt(audio.currentTime) + ' / ' + fmt(duration);
  updateActiveCaption();
  drawWaveform();
  requestAnimationFrame(tick);
}
requestAnimationFrame(tick);

// requestAnimationFrameはタブが非表示/非フォーカスだと大きくスロットル
// される（最悪ほぼ止まる）ため、<audio>のtimeupdateイベントでも同じ更新を
// 行い、そちらをフォールバックにする（timeupdateは再生が進んでいれば
// バックグラウンドタブでもある程度の頻度で発火する）。
audio.addEventListener('timeupdate', () => {
  timeEl.textContent = fmt(audio.currentTime) + ' / ' + fmt(duration);
  updateActiveCaption();
});

document.getElementById('playBtn').addEventListener('click', () => {
  if (audio.paused) { audio.play(); document.getElementById('playBtn').textContent = '⏸ 一時停止'; }
  else { audio.pause(); document.getElementById('playBtn').textContent = '▶ 再生'; }
});
document.addEventListener('keydown', (e) => {
  if (e.code === 'Space' && e.target.tagName !== 'INPUT') { e.preventDefault(); document.getElementById('playBtn').click(); }
});
document.getElementById('nudgeBack').addEventListener('click', () => audio.currentTime = Math.max(0, audio.currentTime - 0.1));
document.getElementById('nudgeFwd').addEventListener('click', () => audio.currentTime += 0.1);

document.getElementById('saveBtn').addEventListener('click', async () => {
  const status = document.getElementById('saveStatus');
  status.textContent = '保存中...';
  const res = await fetch('/alignment', { method: 'POST', body: JSON.stringify(alignment) });
  status.textContent = res.ok ? '✅ 保存しました' : '❌ 保存に失敗しました';
  setTimeout(() => status.textContent = '', 2000);
});

document.getElementById('renderBtn').addEventListener('click', async () => {
  const status = document.getElementById('renderStatus');
  const link = document.getElementById('downloadLink');
  link.classList.add('hidden');
  status.textContent = '書き出し中...(数分かかります)';
  await fetch('/render/run', {
    method: 'POST',
    body: JSON.stringify({
      style: document.getElementById('styleSelect').value,
      font: document.getElementById('fontSelect').value,
    }),
  });
  const poll = setInterval(async () => {
    const s = await (await fetch('/render/status')).json();
    if (s.status === 'done') {
      clearInterval(poll);
      status.textContent = '✅ 完了';
      link.classList.remove('hidden');
    } else if (s.status === 'error') {
      clearInterval(poll);
      status.textContent = '❌ エラー: ' + (s.error || '').split('\\n').pop();
    }
  }, 3000);
});

loadConfig().then(loadAlignment);
</script>
</body>
</html>
"""


def main():
    parser = argparse.ArgumentParser(description="歌詞動画エディタ（ローカルGUI）")
    parser.add_argument("--port", type=int, default=8765)
    parser.add_argument("--audio", help="起動時にセットする音声（省略可、GUIからアップロードも可）")
    parser.add_argument("--image", help="起動時にセットする画像（省略可、GUIからアップロードも可）")
    parser.add_argument("--song", help="起動時にセットする曲ノート（省略可、GUIから選択も可）")
    args = parser.parse_args()

    if args.audio:
        STATE["audio_path"] = str(Path(args.audio).resolve())
    if args.image:
        STATE["image_path"] = str(Path(args.image).resolve())
    if args.song:
        STATE["song_path"] = str(Path(args.song).resolve())

    server = ThreadingHTTPServer(("127.0.0.1", args.port), Handler)
    url = f"http://127.0.0.1:{args.port}/"
    print(f"[INFO] GUIを起動しました: {url}")
    print("       Ctrl+C で終了します。")

    threading.Timer(0.5, lambda: webbrowser.open(url)).start()
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\n[INFO] 終了します。")


if __name__ == "__main__":
    main()
