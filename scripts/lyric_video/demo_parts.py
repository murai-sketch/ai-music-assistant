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
  python3 demo_parts.py K1            # 送られた行の終わり方（濃さだけ／focus／圧痕／focus→圧痕。T39 回1）。置き場は _work/_demo_T38/T39/回1/。値と記号の対応は回1/_対応表_見せない.md
  python3 demo_parts.py K2            # 転換（薄れが止まる）と長い1行の保持（静止／染み込み／breathe）。T39 回2。置き場は _work/_demo_T38/T39/回2/。値と記号の対応は回2/_対応表_見せない.md
  python3 demo_parts.py K3            # 間奏明けの切れ目（その場で替わる／裏写り）。T39 回3。置き場は _work/_demo_T38/T39/回3/。値と記号の対応は回3/_対応表_見せない.md
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


def make_case(case, lines, duration, direction, style_name=DEFAULT_STYLE, out_dir=None, stills=False, image=None, root=None, work_name=None):
    """1本のデモを作る。lines は (文, 開始, 終わり, 歌い終わり) の並び。direction は dict（n_lines は自動で入る）。
    root：置き場（既定は _work/_demo_T33＝今までのデモは変わらない）。root を渡すと、作業データ（曲ノート・無音・alignment・direction）は root/_cache/ に置く。work_name：作業データの置き場の名前（既定は case。回をまたいで同じ記号を使うときに分ける）"""
    # D6-N・D6-P（検査器の確認用。人に見せない・再生しない）は、ユーザーに見せるフォルダと分ける
    if root is not None:
        demo_cache = Path(root) / "_cache"
        d = demo_cache / "cases" / (work_name or case)
    else:
        demo_cache = DEMO_CACHE
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
    cache = demo_cache / _audio_hash(audio)
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
    align.WORK_DIR = beats_mod.WORK_DIR = make_lyric_video.WORK_DIR = demo_cache
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


# --- 送られた行の終わり方（T39 回1）。汎用の文だけ。見出しは記号だけ。記号と値の対応は _対応表_見せない.md に書く ---

T39_ROOT = WORK_DIR / "_demo_T38" / "T39"
K1_LINES = [(S2, 1.0, 7.5, 7.0), (S4, 7.5, 14.0, 13.5), (S3, 14.0, 20.5, 20.0), (S1, 20.5, 27.0, 26.5)]
K1_BREAK = {"1": {"0": 6}, "2": {"0": 4}, "3": {"0": 4}}      # 行番号 → break_after（縦組みの列の切れ目）
K1_FOCUS = 0.07       # ぼけの半径の最大（字の大きさの割合）。主の4列
K1_AMP = 6            # 跡の明暗の差（/255）。主の4列


def _k1_direction(focus=None, trace=None):
    """送られた行の終わり方を変えるだけの direction。focus／trace が無い＝濃さだけ（T38 の page のまま）。trace があるときはインクを 0 まで薄れさせる"""
    base = {"role": "letter_body", "palette": "body", "layout": "vertical", "entrance": "cut", "tail": 30}
    lines = {"1-4": dict(base)}
    for n, ba in K1_BREAK.items():
        lines[n] = {"break_after": ba}
    group = {"lines": [2, 4], "keep": 1, "dims": [0.3], "fade": {"to": [0.0 if trace else 0.12], "sec": 2.6},
             "shift_sec": 0.35, "out": "fade", "enter": {"delay": 0.1, "sec": 0.2}}
    if focus:
        group["focus"] = {"r_max": focus}
    if trace:
        group["trace"] = {"amp": trace}
    return {"look": "letter", "lines": lines, "vertical": {"top": 300, "height": 1140},
            "safe_area": {"left": 132, "right": 132, "top": 300, "bottom": 1440, "corner": {"y_from": 940, "x_max": 850}},
            "page": {"right": 840, "gap": 86, "groups": [group]}}


# 記号 → (説明, focus, trace)。ユーザーには記号だけを見せる
K1_CASES = {
    "A": ("濃さだけ", None, None),
    "B": ("focus（ぼけ）", K1_FOCUS, None),
    "C": ("圧痕", None, K1_AMP),
    "D": ("focus→圧痕", K1_FOCUS, K1_AMP),
    "E1": ("圧痕 振幅違い", None, 3),
    "E2": ("圧痕 振幅違い", None, 10),
    "E3": ("圧痕 振幅違い（Cと同じ値）", None, 6),
    "F1": ("focus 半径違い", 0.04, None),
    "F2": ("focus 半径違い", 0.10, None),
    "F3": ("focus 半径違い（Bと同じ値）", 0.07, None),
}


def k1(out_dir=None, stills=False):
    """T39 回1：前半の、送られた行の終わり方。同じ文・同じ時刻・同じ配置で、部品の指定だけが違う。置き場 _work/_demo_T38/T39/回1/"""
    out_dir = out_dir or (T39_ROOT / "回1")
    made = []
    for sym, (_what, focus, trace) in K1_CASES.items():
        made.append(make_case(sym, K1_LINES, 28.0, _k1_direction(focus, trace), out_dir=out_dir, stills=stills, root=T39_ROOT))
    return made


# --- 転換と長い1行の保持（T39 回2）。前半の終わり方は focus→圧痕（回1 の D）に決まった前提。汎用の文だけ。記号と値の対応は _対応表_見せない.md ---
# 行1〜3＝前半（回1 の D と同じ薄れ。行2・3 を歌うとき、前の行が focus→圧痕）／行4 を歌うとき、行3 が薄れかけて止まる（転換。実曲の行19→20 に当たる）／
# 行5＝読める濃さで送られる（実曲の行20→21 に当たる）／行6＝長い1行の保持（実曲の行23・28 に当たる。送られた行なし）
K2_LINES = [(S2, 1.0, 7.5, 7.0), (S4, 7.5, 14.0, 13.5), (S3, 14.0, 20.5, 20.0), (S1, 20.5, 28.5, 28.0),
            (S2, 28.5, 35.0, 34.5), (S5, 35.0, 46.5, 46.0)]
K2_STOP_AT = 20.5 + 0.35 + 0.86       # 行3 の薄れが止まる時刻（送り＋1拍）
K2_HOLD_AT = 35.0                      # 保持の行の開始
K2_STOP_FADE = 0.24                    # 止まったときのインクの濃さ
K2_STOP_FOCUS = 0.03                   # 止まったときのぼけ（字の大きさの割合）
K2_SOAK = {"r_max": 8, "opacity": 0.10}      # 染み込みの最終の半径（px）・不透明度
K2_SOAK_STOP_SEC = 6.0                 # 送られた行の染み込みにかける秒（止まった時刻から）
K2_SOAK_HOLD_FROM, K2_SOAK_HOLD_SEC = 0.3, 9.0     # 保持の行：入りの後 0.3 秒から 9 秒かけて
K2_SOAK_WEAK = 0.5                     # 読める濃さで送られた行（行4→5）の染み込みは、不透明度をこの倍率で弱く
K2_BREATHE = {"hold": "breathe", "hold_amp": 0.010, "hold_hz": 0.58}


def _k2_direction(stop_soak=None, hold=None):
    """前半は回1 の D（focus 0.07・圧痕 6）。stop_soak：転換で止まった行に染み込みを付ける指定 {"r_max","opacity"}。
    hold：保持の行（行6）の項目（{"soak": …} か breathe の指定）。None＝静止"""
    base = {"role": "letter_body", "palette": "body", "layout": "vertical", "entrance": "cut", "tail": 30}
    lines = {"1-6": dict(base),
             "2": {"break_after": {"0": 6}}, "3": {"break_after": {"0": 4}}, "4": {"break_after": {"0": 4}},
             "6": {"col_lengths": [8, 5]}}
    if hold:
        lines["6"] = dict(lines["6"], **hold)
    enter = {"delay": 0.1, "sec": 0.2}
    soak = lambda o, sec: {"from": "stop", "sec": sec, "r_max": stop_soak["r_max"], "opacity": round(stop_soak["opacity"] * o, 4)}
    g_d = {"lines": [2, 3], "keep": 1, "dims": [0.3], "fade": {"to": [0.0], "sec": 2.6}, "shift_sec": 0.35, "out": "fade", "enter": enter,
           "focus": {"r_max": K1_FOCUS}, "trace": {"amp": K1_AMP}}
    g_stop = {"lines": [4, 4], "keep": 1, "dims": [0.3], "fade": {"to": [K2_STOP_FADE], "sec": 0.86}, "shift_sec": 0.35, "out": "fade", "enter": enter,
              "focus": {"r_max": K2_STOP_FOCUS}}
    g_read = {"lines": [5, 5], "keep": 1, "dims": [0.8], "shift_sec": 0.35, "out": "slide", "enter": enter, "readable": True}
    if stop_soak:
        g_stop["soak"] = soak(1.0, K2_SOAK_STOP_SEC)
        g_read["soak"] = soak(K2_SOAK_WEAK, K2_SOAK_STOP_SEC)
    return {"look": "letter", "lines": lines, "vertical": {"top": 300, "height": 1140},
            "safe_area": {"left": 132, "right": 132, "top": 300, "bottom": 1440, "corner": {"y_from": 940, "x_max": 850}},
            "page": {"right": 840, "gap": 86, "groups": [g_d, g_stop, g_read]}}


def _k2_soak_hold(r_max, opacity):
    return {"soak": {"from": K2_SOAK_HOLD_FROM, "sec": K2_SOAK_HOLD_SEC, "r_max": r_max, "opacity": opacity}}


# 記号 → (転換の染み込み, 保持の行の項目)。ユーザーには記号だけを見せる
K2_CASES = {
    "A": ("止まるだけ／保持 静止", None, None),
    "B": ("止まって染み込み／保持 染み込み", K2_SOAK, _k2_soak_hold(**K2_SOAK)),
    "C": ("止まるだけ／保持 breathe", None, dict(K2_BREATHE)),
    "D": ("止まって染み込み／保持 breathe（組み合わせ。計画書に無い置き方）", K2_SOAK, dict(K2_BREATHE)),
    "E1": ("染み込み 半径違い", {"r_max": 4, "opacity": 0.10}, _k2_soak_hold(4, 0.10)),
    "E2": ("染み込み 半径違い", {"r_max": 12, "opacity": 0.10}, _k2_soak_hold(12, 0.10)),
    "E3": ("染み込み 不透明度違い", {"r_max": 8, "opacity": 0.06}, _k2_soak_hold(8, 0.06)),
    "E4": ("染み込み（Bと同じ値）", K2_SOAK, _k2_soak_hold(**K2_SOAK)),
    "F1": ("breathe 振幅違い", None, dict(K2_BREATHE, hold_amp=0.015)),
    "F2": ("breathe（Cと同じ値）", None, dict(K2_BREATHE)),
}


def k2(out_dir=None, stills=False):
    """T39 回2：転換（薄れが止まる）と長い1行の保持。同じ文・同じ時刻・同じ配置で、部品の指定だけが違う。置き場 _work/_demo_T38/T39/回2/"""
    out_dir = out_dir or (T39_ROOT / "回2")
    made = []
    for sym, (_what, stop_soak, hold) in K2_CASES.items():
        made.append(make_case(sym, K2_LINES, 48.0, _k2_direction(stop_soak, hold), out_dir=out_dir, stills=stills, root=T39_ROOT, work_name=f"回2_{sym}"))
    return made


# --- 間奏明けの切れ目（T39 回3）。前半の終わり方は focus→圧痕（回1 の D）、転換は止まって染み込み・保持は breathe（回2 の D）に決まった前提。
# 汎用の文だけ。記号と値の対応は _対応表_見せない.md ---
# 紙1＝行1〜3（行3 は長く、歌い終わりの後が間奏＝送られた行なしで残る。実曲の行12 に当たる）／切れ目＝行4 の開始（実曲の行13 の開始 111.22 に当たる）／
# 紙2＝行4〜5／行6 で次の紙（実曲の次の切れ目。裏写りはここへ向けて消える）
K3_LINES = [(S2, 1.0, 7.5, 7.0), (S4, 7.5, 14.0, 13.5), (S5, 14.0, 28.0, 20.0), (S1, 28.0, 34.5, 34.0),
            (S3, 34.5, 41.0, 40.5), (S2, 41.0, 48.0, 47.5)]
K3_CUT_AT = 28.0                        # 切れ目（行4 の開始）
K3_BACK = {"from_group": 0, "at_line": 4, "until_line": 6, "blur": 3.0, "opacity": 0.08}      # 主


def _k3_direction(back=None):
    """切れ目の出方だけを変える direction。back が無い＝その場で替わる（行3 が消え、行4 が現れるだけ）。
    前半は回1 の D（focus・圧痕）、紙2 の行5 が行4 を送るときも同じ。保持の行は静止（実曲の行12 は動かさない）"""
    base = {"role": "letter_body", "palette": "body", "layout": "vertical", "entrance": "cut", "tail": 30}
    lines = {"1-6": dict(base),
             "2": {"break_after": {"0": 6}}, "3": {"col_lengths": [8, 5]}, "4": {"break_after": {"0": 4}}, "5": {"break_after": {"0": 4}}}
    enter = {"delay": 0.1, "sec": 0.2}
    d_group = lambda a, b: {"lines": [a, b], "keep": 1, "dims": [0.3], "fade": {"to": [0.0], "sec": 2.6}, "shift_sec": 0.35, "out": "fade",
                            "enter": enter, "focus": {"r_max": K1_FOCUS}, "trace": {"amp": K1_AMP}}
    page = {"right": 840, "gap": 86, "groups": [d_group(2, 3), d_group(5, 5)]}
    if back is not None:
        page["back"] = [dict(K3_BACK, **back)]
    return {"look": "letter", "lines": lines, "vertical": {"top": 300, "height": 1140},
            "safe_area": {"left": 132, "right": 132, "top": 300, "bottom": 1440, "corner": {"y_from": 940, "x_max": 850}},
            "page": page}


# 記号 → (説明, back の上書き)。ユーザーには記号だけを見せる。強め S1（めくり）は計画書の V9 に無いので作らない
K3_CASES = {
    "A": ("その場で替わる", None),
    "B": ("裏写り 主", {}),
    "C": ("裏写り 弱め（ぼけを大きく・薄く）", {"blur": 4.0, "opacity": 0.06}),
    "D": ("裏写り 強め（ぼけを小さく・濃く）", {"blur": 2.0, "opacity": 0.10}),
}


def k3(out_dir=None, stills=False):
    """T39 回3：間奏明けの切れ目。同じ文・同じ時刻・同じ配置で、部品の指定だけが違う。置き場 _work/_demo_T38/T39/回3/"""
    out_dir = out_dir or (T39_ROOT / "回3")
    made = []
    for sym, (_what, back) in K3_CASES.items():
        made.append(make_case(sym, K3_LINES, 49.0, _k3_direction(back), out_dir=out_dir, stills=stills, root=T39_ROOT, work_name=f"回3_{sym}"))
    return made


def main():
    ap = argparse.ArgumentParser(description="部品のデモ動画を作る（本番と同じ描画の経路）")
    ap.add_argument("demo", choices=["D1", "D2", "D1o", "D2o", "D3", "D3R", "D4", "D5", "D6", "D6-N", "D6-P", "C1", "C2", "K1", "K2", "K3"])
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
              "D6-N": d6n, "D6-P": d6p, "C1": c1, "C2": c2, "K1": k1, "K2": k2, "K3": k3}[args.demo](args.out, args.stills):
        print(f"[DEMO] {p}")


if __name__ == "__main__":
    main()
