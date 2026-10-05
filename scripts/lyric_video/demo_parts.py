#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
demo_parts.py
部品のデモ動画を作る。本番と同じ経路（make_lyric_video.main → prepare_plan → KineticRenderer → render_kinetic）を通す。
デモ専用の描き方は持たない。手で作るのは、曲ノート・タイミング（alignment・単語時刻・ビート）・無音の音声・direction だけ。
歌詞ではない汎用の短い文だけを使う（下の S1〜S6。足さない）。

  python3 demo_parts.py D1            # 縦組みの約物（暗い地・明るい地 × 小書きの仮名を寄せない／寄せる）
  python3 demo_parts.py D2            # 長い行の縦組み（karaoke あり・なし、横組み center）
  python3 demo_parts.py D1o           # 縁（輪郭線）のある縦組みの約物（テーマ無しの direction／テーマの outline × 暗い地・明るい地）
  python3 demo_parts.py D2o           # 縁のある縦組みの長い行（同じ4通り）
  python3 demo_parts.py D1 --stills   # 静止画一覧も作る

出力は _work/_demo_T33/<名前>/（Git に入らない）。--out で変える。
作業データ（alignment・単語時刻・ビート・direction の登録）も _work/_demo_T33/_cache/ に置く（実曲のキャッシュ _work/<hash>/ に混ぜない）。
"""

import argparse
import json
import subprocess
import sys
from pathlib import Path

from PIL import Image

import make_lyric_video
import align
import beats as beats_mod
from align import WORK_DIR, _audio_hash
from styles import DEFAULT_STYLE, get_style

DEMO_DIR = WORK_DIR / "_demo_T33"

# 歌詞ではない汎用の文（単語＝文節の切れ目を | で書く）
S1 = "雨の|あとの|道"
S2 = "窓を|あけて、|風を|入れる。"
S3 = "ちょっと|待ってて"
S4 = "ノートの|最後の|ページ"
S5 = "（ここから|先は、|まだ|白い）"
S6 = "駅までの坂道を|ゆっくり歩いて、|角のパン屋で|立ち止まる"

SHIFT = 0.12   # 小書きの仮名を右上へ寄せる量（字の大きさの割合。仮）

# 行：(文, 開始, 終わり, 歌い終わり)
D1_LINES = [(S2, 0.3, 2.5, 2.1), (S3, 2.6, 4.9, 4.5), (S4, 5.0, 7.4, 7.0), (S5, 7.5, 9.9, 9.5)]
D2_LINES = [(S6, 0.5, 7.5, 7.0)]


def _words(text, start, sung_end):
    """単語（文節）の時刻。字を均等に割る"""
    parts = text.split("|")
    n = sum(len(p) for p in parts)
    done, out = 0, []
    for p in parts:
        t0 = start + (sung_end - start) * done / n
        done += len(p)
        t1 = start + (sung_end - start) * done / n
        out.append({"word": p, "start": round(t0, 3), "end": round(t1, 3)})
    return out


DEMO_CACHE = DEMO_DIR / "_cache"

# テーマ無しの direction の背景（画像）。字は白＋暗い縁（max(字の大きさ/24, 3)px）で描かれる
IMAGE_DARK = ((40, 56, 92), (22, 28, 52))      # 上端・下端の色（縁の暗い色が見える中間の暗さ）
IMAGE_LIGHT = ((236, 228, 206), (214, 200, 170))


def _bg_image(path, colors=None):
    if colors is None:
        if not path.exists():
            Image.new("RGB", (1080, 1920), (20, 20, 24)).save(path)
        return
    (r0, g0, b0), (r1, g1, b1) = colors
    im = Image.new("RGB", (1080, 1920))
    px = im.load()
    for y in range(1920):
        k = y / 1919
        c = (round(r0 + (r1 - r0) * k), round(g0 + (g1 - g0) * k), round(b0 + (b1 - b0) * k))
        for x in range(1080):
            px[x, y] = c
    im.save(path)


def make_case(case, lines, duration, direction, style_name=DEFAULT_STYLE, out_dir=None, stills=False, image=None):
    """1本のデモを作る。lines は (文, 開始, 終わり, 歌い終わり) の並び。direction は dict（n_lines は自動で入る）"""
    d = DEMO_DIR / case
    d.mkdir(parents=True, exist_ok=True)
    texts = ["".join(l[0].split("|")) for l in lines]
    note = d / "note.md"
    note.write_text("---\ntitle: \"demo\"\nbpm: 120\ngenre: \"\"\n---\n\n## 歌詞 & 楽曲構成\n\n```\n[Verse]\n" + "\n".join(texts) + "\n```\n",
                    encoding="utf-8")
    audio = d / "silence.mp3"
    subprocess.run(["ffmpeg", "-y", "-loglevel", "error", "-f", "lavfi", "-i", "anullsrc=r=44100:cl=mono", "-t", str(duration),
                    "-metadata", f"comment={case}", "-q:a", "9", str(audio)], check=True)
    img = d / "bg.png"
    _bg_image(img, image)
    cache = DEMO_CACHE / _audio_hash(audio)
    cache.mkdir(parents=True, exist_ok=True)
    alignment = [{"line": t, "start": s, "end": e, "src": i, "match": 1.0, "section": "Verse"}
                 for i, (t, (_x, s, e, _u)) in enumerate(zip(texts, lines))]
    (cache / "alignment.json").write_text(json.dumps({"lyric_lines": texts, "alignment": alignment}, ensure_ascii=False, indent=2),
                                          encoding="utf-8")
    segs = []
    for (x, s, e, u) in lines:
        ws = _words(x, s, u)
        segs.append({"start": s, "end": u, "text": "".join(w["word"] for w in ws), "words": ws})
    (cache / "whisper_words.json").write_text(json.dumps(segs, ensure_ascii=False, indent=1), encoding="utf-8")
    style = get_style(style_name)
    beats = [round(0.5 * k, 3) for k in range(int(duration / 0.5))]
    (cache / "beats.json").write_text(json.dumps({"params": {
        "min_gap_sec": style["beat_min_gap_sec"], "strength_percentile": style["beat_strength_percentile"], "bpm_hint": 120},
        "beats": beats}, ensure_ascii=False, indent=2), encoding="utf-8")
    dirn = dict(direction, n_lines=len(lines))
    dpath = d / "direction.json"
    dpath.write_text(json.dumps(dirn, ensure_ascii=False, indent=1), encoding="utf-8")
    out = Path(out_dir or d)
    out.mkdir(parents=True, exist_ok=True)
    base = ["make_lyric_video.py", "--song", str(note), "--audio", str(audio), "--image", str(img), "--style", style_name,
            "--direction-from", str(dpath), "--replan"]
    # 作業データの置き場を _work/_demo_T33/_cache/ に切り替える（終わったら戻す）
    saved = (align.WORK_DIR, beats_mod.WORK_DIR, make_lyric_video.WORK_DIR)
    align.WORK_DIR = beats_mod.WORK_DIR = make_lyric_video.WORK_DIR = DEMO_CACHE
    try:
        sys.argv = base + ["--out", str(out / f"{case}.mp4")]
        make_lyric_video.main()
        if stills:
            sys.argv = base + ["--stills", str(out / f"{case}_stills")]
            make_lyric_video.main()
    finally:
        align.WORK_DIR, beats_mod.WORK_DIR, make_lyric_video.WORK_DIR = saved
    return out / f"{case}.mp4"


def d1(out_dir=None, stills=False):
    """縦組みの約物。暗い地・明るい地 × 小書きの仮名を寄せない／寄せる"""
    made = []
    for look, tag in (("demo-dark", "dark"), ("demo-light", "light")):
        for shift, stag in ((0.0, "noshift"), (SHIFT, "shift")):
            direction = {"look": look, "lines": {"1-4": {"layout": "vertical", "entrance": "cut"}}}
            if shift:
                direction["vertical"] = {"kana_shift": shift}
            made.append(make_case(f"D1_{tag}_{stag}", D1_LINES, 10.0, direction, out_dir=out_dir, stills=stills))
    return made


def d2(out_dir=None, stills=False):
    """長い行の縦組み。karaoke あり・なし、横組み center の同じ行"""
    karaoke = {"unlit_opacity": 0.65, "light_frames": 3, "keyword_unlit": "text", "min_match": 0.5, "min_cover": 0.5}
    made = []
    for tag, line in (("plain", {"layout": "vertical", "entrance": "cut"}),
                      ("karaoke", {"layout": "vertical", "entrance": "karaoke"}),
                      ("yoko_center", {"layout": "center", "entrance": "cut"})):
        direction = {"look": "demo-dark", "lines": {"1": line}}
        if line["entrance"] == "karaoke":
            direction["karaoke"] = karaoke
        made.append(make_case(f"D2_{tag}", D2_LINES, 8.0, direction, out_dir=out_dir, stills=stills))
    return made


def _outline_cases(prefix, lines, duration, line_item, out_dir, stills):
    """縁のある縦組み：テーマ無しの direction（画像の地・白字＋暗い縁）／テーマの accent: outline（差し色の縁 3px）× 暗い地・明るい地"""
    made = []
    for tag, image in (("dark", IMAGE_DARK), ("light", IMAGE_LIGHT)):
        direction = {"lines": {"1-%d" % len(lines): dict(line_item)}}
        made.append(make_case(f"{prefix}_plain_{tag}", lines, duration, direction, out_dir=out_dir, stills=stills, image=image))
    for look, tag in (("demo-dark", "dark"), ("demo-light", "light")):
        direction = {"look": look, "lines": {"1-%d" % len(lines): dict(line_item, accent="outline")}}
        made.append(make_case(f"{prefix}_theme_{tag}", lines, duration, direction, out_dir=out_dir, stills=stills))
    return made


def d1o(out_dir=None, stills=False):
    """縁のある縦組みの約物（D1 と同じ行）"""
    return _outline_cases("D1o", D1_LINES, 10.0, {"layout": "vertical", "entrance": "cut"}, out_dir, stills)


def d2o(out_dir=None, stills=False):
    """縁のある縦組みの長い行（D2 と同じ行）"""
    return _outline_cases("D2o", D2_LINES, 8.0, {"layout": "vertical", "entrance": "cut"}, out_dir, stills)


def main():
    ap = argparse.ArgumentParser(description="部品のデモ動画を作る（本番と同じ描画の経路）")
    ap.add_argument("demo", choices=["D1", "D2", "D1o", "D2o"])
    ap.add_argument("--out", help="出力先（既定: _work/_demo_T33/<名前>/）")
    ap.add_argument("--stills", action="store_true", help="静止画一覧も作る")
    args = ap.parse_args()
    for p in {"D1": d1, "D2": d2, "D1o": d1o, "D2o": d2o}[args.demo](args.out, args.stills):
        print(f"[DEMO] {p}")


if __name__ == "__main__":
    main()
