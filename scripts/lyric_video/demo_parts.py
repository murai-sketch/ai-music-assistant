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
  python3 demo_parts.py D3            # 縦の線（流れる版 3本＋位置が切り替わる版 3本。ファイル名は 候補A〜F。暗い地・白地）
  python3 demo_parts.py D4            # 字が絵になる（円盤・渦・同心円・点列。集まりの長さ違いの3本。候補A〜C）
  python3 demo_parts.py D5            # 静と密（粒と形の字数を 20 ↔ 700／400 に）
  python3 demo_parts.py D6            # 白地黒字＋質感（読み字にかすれを掛けた版・掛けない版。候補A・B）
  python3 demo_parts.py D3R           # D3 の候補C（流れ・240px/秒）を流れの向き違いで作り直す（暗い地。混在／全部下向き／全部上向き）
  python3 demo_parts.py D6-P          # 検査器の確認の対照（同じ字数を 0.6 秒で1回だけ増やす。明るい地・純白の2本）
  python3 demo_parts.py D6-N --unsafe-flicker-demo   # 検査器の確認（わざと点滅させた見本。**人に見せない・再生しない**。静止画と数値だけを使う）
  python3 demo_parts.py C1            # carry（行全体を一定の速さで動かす保持）。あり・なし × 横組み3行（流れの線つき）・縦組み2列（T35 C1）
  python3 demo_parts.py C2            # 列を積む（前の列を残して薄くする）。あり・なし（T35 C2）
  python3 demo_parts.py D1 --stills   # 静止画一覧も作る

出力は _work/_demo_T33/<名前>/（Git に入らない）。--out で変える。
D6-N・D6-P（検査器の確認用。人に見せない・再生しない）だけは、既定の出力先が _work/_demo_T33/_test_再生しない/<名前>/（ユーザーに見せるフォルダと分ける）。
作業データ（alignment・単語時刻・ビート・direction の登録）も _work/_demo_T33/_cache/ に置く（実曲のキャッシュ _work/<hash>/ に混ぜない）。
"""

import argparse
import json
import math
import subprocess
import sys
from pathlib import Path

from PIL import Image

import kinetic_points
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
    # D6-N・D6-P（検査器の確認用。人に見せない・再生しない）は、ユーザーに見せるフォルダと分ける
    d = (DEMO_DIR / "_test_再生しない" if case.startswith(("D6N_", "D6P_")) else DEMO_DIR) / case
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


# --- 点の層のデモ（D3〜D6）。ユーザーに見せるときは候補の記号（A・B・C…）だけを出す。候補と値の対応は MAP_NAME に書く（見せない） ---

MAP_NAME = "_対応表.json"
GROUNDS = (("demo-dark", "暗", "heavy"), ("ink-white", "白", "read_v"))   # (テーマ, 名前, 読み字の役)


def _note_map(entries):
    """候補の記号と値の対応を DEMO_DIR/_対応表.json に足す（デモの出力先には置かない）"""
    path = DEMO_DIR / MAP_NAME
    cur = json.loads(path.read_text(encoding="utf-8")) if path.exists() else {}
    cur.update(entries)
    path.write_text(json.dumps(cur, ensure_ascii=False, indent=1), encoding="utf-8")


def _lines_dir(n, role, extra=None):
    item = {"role": role, "entrance": "cut"}
    item.update(extra or {})
    return {"1-%d" % n: item}


def d3(out_dir=None, stills=False):
    """縦の線。A〜C＝流れる（速さ違い）、D〜F＝位置が切り替わる（出ている時間違い）。暗い地・白地"""
    lines = [(S1, 2.0, 5.0, 4.6), (S2, 6.5, 9.5, 9.1)]
    flow_track = [{"at": 0, "density": 1}, {"at": 3.0, "density": 1}, {"at": 3.6, "density": 2},
                  {"at": 6.0, "density": 2}, {"at": 6.6, "density": 3}]
    cands = [("A", {"motion": "flow", "speed_px_s": 80, "track": flow_track}),
             ("B", {"motion": "flow", "speed_px_s": 120, "track": flow_track}),
             ("C", {"motion": "flow", "speed_px_s": 240, "track": flow_track}),
             ("D", {"motion": "switch", "on_sec": 0.25, "count": 2}),
             ("E", {"motion": "switch", "on_sec": 0.4, "count": 2}),
             ("F", {"motion": "switch", "on_sec": 0.6, "count": 2})]
    made, mp = [], {}
    for tag, extra in cands:
        mp[f"D3_候補{tag}"] = {k: v for k, v in extra.items() if k != "track"}
        for look, gname, role in GROUNDS:
            pt = dict({"kind": "tate_line", "span": {"start": 0, "end": 10}, "source": "line", "size_px": [20, 28],
                       "opacity": [0.4, 0.7]}, **extra)
            direction = {"look": look, "lines": _lines_dir(2, role), "points": [pt]}
            made.append(make_case(f"D3_候補{tag}_{gname}", lines, 10.0, direction, out_dir=out_dir, stills=stills))
    _note_map(mp)
    return made


def _star(cx, cy, r_out, r_in, n=5):
    """五角形の星の頂点（0〜1 の割合）。点列の outline の例（数字だけ）"""
    pts = []
    for k in range(2 * n):
        r = r_out if k % 2 == 0 else r_in
        a = -math.pi / 2 + math.pi * k / n
        pts.append([round((cx * 1080 + r * math.cos(a)) / 1080, 4), round((cy * 1920 + r * math.sin(a)) / 1920, 4)])
    return pts


SHAPE_DISPERSE = 0.5     # 形の散り（秒）。shape の散りの下限（kinetic_points.DISPERSE_MIN_SEC）に合わせる（T35 R0-11）
SHAPE_GATHER = 0.8       # 形の集まり（秒）。draw_on 0.6 のとき字が現れきる長さが 0.5 秒以上になる値（D4 候補C と同じ。R0-11）


def _shape_items(t0, g, hold, disperse, ink=None):
    """4つの形を順に。集まり g 秒、保つ hold 秒、散り disperse 秒。小さい字（12〜20px）を主体に、大きめの字を少数混ぜる"""
    win = g + hold + disperse
    big = {"ratio": 0.05, "size_px": [28, 36]}
    base = {"kind": "shape", "source": "line", "size_px": [12, 20], "opacity": [0.4, 0.8], "from": "scatter", "orient": "upright",
            "gather": g, "draw_on": 0.6, "disperse": disperse, "flow": 0.1, "wobble_px": 3.0, "big": big}
    shapes = [({"type": "disc", "center": [0.5, 0.46], "r0": 60, "r": 260}, 360),
              ({"type": "spiral", "center": [0.5, 0.46], "r0": 20, "turns": 2.5, "r_max": 400}, 220),
              ({"type": "concentric", "center": [0.5, 0.46], "rings": 3, "r_min": 150, "gap": 90}, 150),
              ({"type": "outline", "points": _star(0.5, 0.46, 380, 150)}, 260)]
    items = []
    for k, (sh, n) in enumerate(shapes):
        it = dict(base, shape=sh, count=n, span={"start": round(t0 + k * win, 3), "end": round(t0 + (k + 1) * win, 3)})
        if ink:
            it["ink"] = dict(ink["disc"] if sh["type"] == "disc" else ink["other"])
        items.append(it)
    return items, win


def d4(out_dir=None, stills=False):
    """字が絵になる。集まりの長さ違いの3本（候補A〜C）。保つ時間は約2.5秒、保つ間にわずかに変形する。暗い地"""
    made, mp = [], {}
    for tag, g in (("A", 0.3), ("B", 0.5), ("C", 0.8)):
        a_in = kinetic_points.shape_appear_sec(g, 0.6)          # 字が現れきる長さ（_shape_items の draw_on は 0.6）
        if g < kinetic_points.GATHER_MIN_SEC - 1e-9:
            print(f"[DEMO] D4_候補{tag}：集まり {g} 秒は下限（{kinetic_points.GATHER_MIN_SEC} 秒）未満なので作りません（T35 R0-6。ユーザーの選び直しは進行管理 Q-6）")
            continue
        if a_in < kinetic_points.EDGE_FADE_SEC - 1e-9:
            print(f"[DEMO] D4_候補{tag}：集まり {g} 秒では字が現れきるまでが {a_in:.2f} 秒（0.5 秒以上が必要）なので作りません"
                  f"（T35 R0-11。draw_on 0.6 のとき集まりは 0.8 秒など。ユーザーの選び直しは進行管理 Q-6）")
            continue
        items, win = _shape_items(0.4, g, 2.5, SHAPE_DISPERSE)
        lines = [(S1, round(0.4 + k * win + g + 0.2, 3), round(0.4 + k * win + g + 2.3, 3), round(0.4 + k * win + g + 2.0, 3))
                 for k in range(4)]
        total = round(0.4 + 4 * win + 0.4, 2)
        mp[f"D4_候補{tag}"] = {"gather_sec": g, "hold_sec": 2.5, "window_sec": round(win, 2)}
        direction = {"look": "demo-dark", "lines": _lines_dir(4, "heavy", {"text_y": 0.74}), "points": items}
        made.append(make_case(f"D4_候補{tag}", lines, total, direction, out_dir=out_dir, stills=stills))
    _note_map(mp)
    return made


def d5(out_dir=None, stills=False):
    """静と密。粒 20字 ↔ 700字（0.6 秒かけて増減）、形 20字 → 400字。読み字（S4・S2・S1）と同居。暗い地"""
    lines = [(S4, 0.5, 3.5, 3.1), (S2, 5.0, 8.0, 7.6), (S1, 9.5, 11.5, 11.1)]
    tsubu = {"kind": "tsubu", "span": {"start": 0, "end": 9.4}, "source": "line", "size_px": [8, 18], "opacity": [0.3, 0.7],
             "track": [{"at": 0, "count": 20}, {"at": 4.0, "count": 20}, {"at": 4.6, "count": 700},
                       {"at": 8.4, "count": 700}, {"at": 9.0, "count": 20}]}
    shape = {"kind": "shape", "span": {"start": 9.0, "end": 12.2}, "source": "line", "size_px": [12, 20], "opacity": [0.4, 0.8],
             "shape": {"type": "disc", "center": [0.5, 0.42], "r0": 40, "r": 250},
             "track": [{"at": 0, "count": 20}, {"at": 0.8, "count": 20}, {"at": 1.4, "count": 400}],
             "gather": SHAPE_GATHER, "draw_on": 0.6, "disperse": SHAPE_DISPERSE, "flow": 0.1, "wobble_px": 3.0,
             "big": {"ratio": 0.05, "size_px": [28, 36]}}
    direction = {"look": "demo-dark", "lines": {"1-2": {"role": "heavy", "entrance": "cut"},
                                                "3": {"role": "heavy", "entrance": "cut", "text_y": 0.74}},
                 "points": [tsubu, shape]}
    return [make_case("D5", lines, 12.4, direction, out_dir=out_dir, stills=stills)]


def d6(out_dir=None, stills=False):
    """白地黒字＋質感。点の字に kasure 0.3、円盤だけ dots（間隔 6px）。候補A＝読み字にかすれ 0.15 を掛けた版、B＝掛けない版。白地"""
    lines = [(S1, 1.0, 3.0, 2.7), (S4, 4.0, 6.4, 6.0), (S2, 8.0, 11.0, 10.6)]
    items, _w = _shape_items(0.0, SHAPE_GATHER, 2.5, SHAPE_DISPERSE, ink={"disc": {"mode": "dots", "pitch": 6}, "other": {"mode": "kasure", "amount": 0.3}})
    items = items[:2]                                # 円盤（dots）・渦（kasure）の2つ
    for it in items:
        it["size_px"] = [14, 20] if it["shape"]["type"] == "disc" else [14, 28]
        it.pop("big", None)
    tsubu = {"kind": "tsubu", "span": {"start": 7.0, "end": 13.0}, "source": "line", "size_px": [8, 18], "opacity": [0.3, 0.7],
             "ink": {"mode": "kasure", "amount": 0.3},
             "track": [{"at": 0, "count": 20}, {"at": 1.0, "count": 20}, {"at": 1.6, "count": 700},
                       {"at": 4.4, "count": 700}, {"at": 5.0, "count": 20}]}
    made, mp = [], {}
    for tag, read_ink in (("A", {"mode": "kasure", "amount": 0.15}), ("B", None)):
        item = {"role": "read_v", "entrance": "cut", "min_px": 96}
        if read_ink:
            item["ink"] = read_ink
        direction = {"look": "ink-white", "lines": {"1-3": item}, "points": items + [tsubu]}
        mp[f"D6_候補{tag}"] = {"read_ink": read_ink}
        made.append(make_case(f"D6_候補{tag}", lines, 13.4, direction, out_dir=out_dir, stills=stills))
    _note_map(mp)
    return made


DEMO_OUT_FIXED = DEMO_DIR / "_修正後_20261006"


def d3r(out_dir=None, stills=False):
    """D3 の候補C（流れ・240px/秒）を、流れの向き違いで作り直す（T35 R0-7）。暗い地。
    混在＝線ごとに乱数（今までの指定）、全部下向き（dir: down）、全部上向き（dir: up）。ユーザーに見せるときは候補の数字を書かない"""
    out_dir = out_dir or DEMO_OUT_FIXED
    lines = [(S1, 2.0, 5.0, 4.6), (S2, 6.5, 9.5, 9.1)]
    flow_track = [{"at": 0, "density": 1}, {"at": 3.0, "density": 1}, {"at": 3.6, "density": 2},
                  {"at": 6.0, "density": 2}, {"at": 6.6, "density": 3}]
    made = []
    for tag, extra in (("", {}), ("_全部下向き", {"dir": "down"}), ("_全部上向き", {"dir": "up"})):
        pt = dict({"kind": "tate_line", "span": {"start": 0, "end": 10}, "source": "line", "size_px": [20, 28], "opacity": [0.4, 0.7],
                   "motion": "flow", "speed_px_s": 240, "track": flow_track}, **extra)
        direction = {"look": "demo-dark", "lines": _lines_dir(2, "heavy"), "points": [pt]}
        made.append(make_case(f"D3R_候補C_暗{tag}", lines, 10.0, direction, out_dir=out_dir, stills=stills))
    return made


def _flicker_cases(kind, out_dir, stills):
    """D6-N（わざと点滅させた見本）／D6-P（同じ字数を 0.6 秒で1回だけ増やす対照）。明るい地（ink-white）と純白（demo-flicker-white）の2本ずつ。
    粒 800字（20px）＋形（円盤）200字（48px）。N は 0.2 秒ごとに全部出す／全部消す"""
    lines = [(S1, 0.5, 2.5, 2.2), (S4, 3.0, 5.4, 5.0)]
    end = 7.0
    span = {"start": 0, "end": end}

    def rise(n):
        """0.6 秒で1回だけ増やす（D6-P）"""
        return [{"at": 0, "density": 0}, {"at": 1.0, "density": 0}, {"at": 1.6, "count": n}]

    def hard(n):
        """一気に切り替える列（同じ時刻に2点）"""
        tr, state = [{"at": 0, "density": 0}], 0
        for k in range(30):
            t = round(1.0 + 0.2 * k, 3)
            new = n if k % 2 == 0 else 0
            tr.append({"at": t, "density": 0} if state == 0 else {"at": t, "count": state})
            tr.append({"at": t, "count": new} if new else {"at": t, "density": 0})
            state = new
        return tr

    made = []
    for look, tag in (("ink-white", "明るい地"), ("demo-flicker-white", "純白")):
        tsubu = {"kind": "tsubu", "span": dict(span), "source": "line", "size_px": [20, 20], "opacity": [0.3, 0.7],
                 "track": hard(800) if kind == "N" else rise(800)}
        shape = {"kind": "shape", "span": dict(span), "source": "line", "size_px": [48, 48], "opacity": [0.4, 0.8],
                 "shape": {"type": "disc", "center": [0.5, 0.42], "r0": 40, "r": 300}, "gather": SHAPE_GATHER, "draw_on": 0.6, "disperse": SHAPE_DISPERSE,
                 "flow": 0.1, "wobble_px": 3.0, "track": hard(200) if kind == "N" else rise(200)}
        item = {"role": "read_v", "entrance": "cut", "min_px": 96}
        direction = {"look": look, "lines": {"1-2": item}, "points": [tsubu, shape]}
        made.append(make_case(f"D6{kind}_{tag}", lines, end + 0.4, direction, out_dir=out_dir, stills=stills))
    return made


def d6n(out_dir=None, stills=False):
    return _flicker_cases("N", out_dir, stills)


def d6p(out_dir=None, stills=False):
    return _flicker_cases("P", out_dir, stills)


# --- 新しい動き2つのデモ（T35 C1・C2）。曲の direction には書かない。ユーザーが見て決める。歌詞ではない汎用の文だけ ---

C1_FLOW = {"kind": "tate_line", "span": {"start": 0, "end": 16}, "source": "line", "size_px": [20, 28], "opacity": [0.4, 0.7],
           "motion": "flow", "speed_px_s": 240, "dir": "down", "track": [{"at": 0, "density": 3}, {"at": 16, "density": 3}]}


def c1(out_dir=None, stills=False):
    """carry。横組み3行（下へ 8px/秒。背後に流れの線）・縦組み2列（右の列は上へ・左の列は下へ 6px/秒）。それぞれ carry なしの対照つき。暗い地。
    行は次の行の開始まで表示（tail を大きく）。ユーザーに見せるときは速さの数字を並べない"""
    made = []
    h_lines = [(S1, 1.0, 5.2, 4.8), (S4, 5.5, 9.7, 9.3), (S2, 10.0, 14.2, 13.8)]
    v_lines = [(S2, 1.0, 9.0, 8.6)]
    for tag, carry in (("あり", {"hold": "carry", "carry": {"px_s": 8, "dir": "down"}}), ("なし", {})):
        item = dict({"layout": "center", "role": "heavy", "entrance": "cut", "tail": 60}, **carry)    # 構図は自動に任せず中央（弧・斜めにしない）
        direction = {"look": "demo-dark", "lines": {"1-3": item}, "points": [dict(C1_FLOW)]}
        made.append(make_case(f"C1_{tag}_横組み", h_lines, 15.0, direction, out_dir=out_dir, stills=stills))
    for tag, carry in (("あり", {"hold": "carry", "carry": {"0": {"px_s": 6, "dir": "up"}, "1": {"px_s": 6, "dir": "down"}}}), ("なし", {})):
        item = dict({"layout": "vertical", "role": "heavy", "entrance": "cut", "tail": 60, "break_after": {"0": 6}}, **carry)
        direction = {"look": "demo-dark", "lines": {"1": item}}
        made.append(make_case(f"C1_{tag}_縦組み", v_lines, 10.0, direction, out_dir=out_dir, stills=stills))
    return made


def c2(out_dir=None, stills=False):
    """列を積む。縦組み4行（6〜10字）を右から積み、5行目（横組み）で全部消す。なし＝同じ4行を1行ずつ（今の縦組み）。暗い地"""
    lines = [(S1, 1.0, 3.8, 3.4), (S3, 4.0, 6.8, 6.4), ("駅までの|坂道を", 7.0, 9.8, 9.4), (S4, 10.0, 12.8, 12.4), (S2, 13.0, 15.6, 15.2)]
    made = []
    for tag, with_stack in (("あり", True), ("なし", False)):
        vert = {"layout": "vertical", "role": "heavy", "entrance": "cut", "tail": 60, "max_px": 100}
        direction = {"look": "demo-dark", "lines": {"1-4": vert, "5": {"role": "heavy", "entrance": "cut"}}}
        if with_stack:
            direction["stack"] = [{"lines": [1, 4], "dim": 0.55, "clear_at_line": 5}]
        made.append(make_case(f"C2_{tag}", lines, 16.0, direction, out_dir=out_dir, stills=stills))
    return made


def main():
    ap = argparse.ArgumentParser(description="部品のデモ動画を作る（本番と同じ描画の経路）")
    ap.add_argument("demo", choices=["D1", "D2", "D1o", "D2o", "D3", "D3R", "D4", "D5", "D6", "D6-N", "D6-P", "C1", "C2"])
    ap.add_argument("--out", help="出力先（既定: _work/_demo_T33/<名前>/。D6-N・D6-P は _work/_demo_T33/_test_再生しない/<名前>/）")
    ap.add_argument("--stills", action="store_true", help="静止画一覧も作る")
    ap.add_argument("--unsafe-flicker-demo", action="store_true",
                    help="試験用。字数の増減の速さの規則（点滅の防御）を外して D6-N（わざと点滅させた見本）を作る。人に見せない・再生しない。"
                         "本番の CLI・GUI・direction からは指定できない（この道具の中だけ）")
    args = ap.parse_args()
    if args.demo == "D6-N" and not args.unsafe_flicker_demo:
        ap.error("D6-N は点滅の防御を外して作る見本です。--unsafe-flicker-demo を付けてください（人に見せない・再生しない）")
    if args.unsafe_flicker_demo and args.demo != "D6-N":
        ap.error("--unsafe-flicker-demo は D6-N だけに使えます")
    if args.unsafe_flicker_demo:
        kinetic_points.UNSAFE_SKIP_TRACK_RULES = True
    for p in {"D1": d1, "D2": d2, "D1o": d1o, "D2o": d2o, "D3": d3, "D3R": d3r, "D4": d4, "D5": d5, "D6": d6,
              "D6-N": d6n, "D6-P": d6p, "C1": c1, "C2": c2}[args.demo](args.out, args.stills):
        print(f"[DEMO] {p}")


if __name__ == "__main__":
    main()
