# -*- coding: utf-8 -*-
"""
kinetic_points.py
点の層：字を「行の一部」ではなく「1字ずつの点」として扱う層。読み字（歌詞を読ませる字。kinetic._Cut が描く）の下に敷く。
並べ方は3つ（同じ描き方・キャッシュ・読み字の空け・密度の制御を共有する）：
  tsubu      粒の場（読ませない大きさの字をまき、ゆっくり漂わせる）
  tate_line  縦の線（字を縦1列に並べた端のある線。流れる／位置が切り替わる）
  shape      字が絵になる（円盤・渦・同心円・点列に、集まる→保つ→散る）
direction の最上位 "points"（配列）で指定した曲だけで使う。自動割り当てには入れない。

守ること：
  - 位置・不透明度は 時刻 t の閉じた式だけで決まる（前のフレームの状態を積み上げない）。乱数は seed 固定の配列
    → 部分書き出しと全編の書き出しで同じ画になる
  - 字の画像は点の層専用のキャッシュ（大きさは SIZE_STEPS、不透明度 1/16、角度 15°）。毎フレームの拡大縮小・回転をしない
  - 読み字の周り（外接矩形＋字高の 0.5 倍）では不透明度を 0.25 以下にする（点を動かさず、不透明度の式に掛ける）
  - 歌詞の本文を設定に書かない（行番号と字の位置で指す）。字は実行時にプランの行から取る
"""

import bisect
import math
import unicodedata
import zlib
from collections import OrderedDict

import numpy as np
from PIL import Image, ImageDraw, ImageFilter

W, H = 1080, 1920
FPS = 30
SIZE_STEPS = (8, 10, 12, 14, 16, 18, 20, 24, 28, 32, 40, 48, 60)   # 字の大きさの段階（これだけ別スプライトを作る）
BANDS = {"tsubu": (8, 20), "tate_line": (16, 32), "shape": (12, 60)}   # 種類ごとの大きさの帯（仕様 §3-4）
READ_MIN_PX = 60            # 読み字の下限
READ_RATIO = 1.5            # 読み字 ≧ 同じ画面の点の層の最大の字 × 1.5
MAX_CHARS = 1000            # 1フレームの点の層の字数の上限（仕様 §7）
MAX_BIG_CHARS, BIG_PX = 60, 60   # うち 60px を超える字は 60字まで（帯は 60px までなので、実際には超えない。式が崩れたときの検出用）
LEVELS = {"tsubu": (0, 60, 200, 600), "tate_line": (0, 1, 4, 10), "shape": (0, 40, 150, 400)}   # density の段階 → 字数（線は本数）
COUNT_RANGE = {"tsubu": (1, 800), "tate_line": (1, 12), "shape": (1, 400)}
CLEAR_OPACITY = 0.25        # 読み字の周りの不透明度の上限
CLEAR_PAD_RATIO = 0.5       # 読み字の外接矩形を広げる量（読み字の字高に対する割合）
CLEAR_SOFT = 60.0           # 空けの外側へ、不透明度が戻る幅（px。字が空けの縁をまたぐときの点滅を避ける）
CLEAR_RAMP = 0.2            # 行が出入りするときの空けの出入りの長さ（秒）
MIN_RAMP_SEC = 0.5          # 字数の増減にかける最短の時間
MIN_FLASH_GAP = 2.0         # 0 フレームの切り替えを置ける最短の間隔（kinetic.MIN_FLASH_GAP と同じ値）
REVERSE_SEC = 1.0           # 切り替えた後、逆向きに戻してよい最短の時間
RANK_FADE_SEC = 0.2         # 字ごとの出入りの長さ
EDGE_FADE_SEC = 0.5         # 粒・線の区間の頭と尻の出入り（仕様 §3-6「0.5 秒以上かけて増減」）
DISPERSE_MIN_SEC = 0.5       # shape の散りの最短（0 は区間の尻で一気に消える）
SHAPE_DEFAULTS = {"gather": 0.6, "draw_on": 0.6, "disperse": 0.45}   # _init_shape の既定（検査も同じ値で見る）
GATHER_MIN_SEC = 0.5        # shape の集まりの最短（仕様 §3-6。それより速いと数百字が一気に現れる）
UNSAFE_SKIP_TRACK_RULES = False   # 試験用（demo_parts.py の --unsafe-flicker-demo だけが立てる）。本番の CLI・GUI・direction からは立てられない
MARGIN = 92                 # 左右の余白
MAX_R = 448                 # 形の中心からの最大の半径
CACHE_MAX = 20000
INK_MODES = ("kasure", "dots")
INK_KASURE_MAX = 0.5
INK_DOTS_PITCH = (4, 10)
READ_INK_MIN_PX, READ_INK_MAX_AMOUNT = 96, 0.2   # 読み字に掛けるかすれの条件（仕様 §3-5）
LIGHT_BAD_FONTS = (("Hiragino Sans", "W9"), ("BIZ UDGothic", "Bold"), ("BIZ UDGothic", "B"))   # 白地の粒・線に使わない書体

KINDS = ("tsubu", "tate_line", "shape")
SPAN_KEYS = {"lines", "section", "start", "after_line", "start_at", "seconds", "end", "until_line", "until"}
COMMON_KEYS = {"kind", "span", "source", "density", "count", "track", "size_px", "opacity", "ink", "seed", "role"}
KIND_KEYS = {
    "tsubu": COMMON_KEYS | {"drift_px_s"},
    "tate_line": COMMON_KEYS | {"motion", "speed_px_s", "length_px", "on_sec", "beat_sync", "dir", "count_fade"},
    "shape": COMMON_KEYS | {"shape", "from", "orient", "gather", "draw_on", "disperse", "flow", "wobble_px", "big"},
}
SHAPE_KEYS = {"disc": {"type", "center", "r0", "r"}, "spiral": {"type", "center", "r0", "turns", "r_max"},
              "concentric": {"type", "center", "rings", "r_min", "gap"}, "outline": {"type", "points", "closed", "mask"}}
FROM_MODES = ("scatter", "line", "edge")
ORIENTS = ("upright", "tangent")
TATE_DIRS = ("mixed", "down", "up")   # 流れる線の向き：線ごとに乱数（既定）／全部上→下／全部下→上


# ---------------------------------------------------------------------------
# 小さな道具

def _num(v):
    return isinstance(v, (int, float)) and not isinstance(v, bool)


def _step(px):
    """大きさ px に最も近い段階"""
    return min(SIZE_STEPS, key=lambda s: (abs(s - px), s))


def _steps_in(lo, hi):
    out = [s for s in SIZE_STEPS if lo - 1e-9 <= s <= hi + 1e-9]
    return out or [_step((lo + hi) / 2)]


def _smooth(u):
    u = np.clip(u, 0.0, 1.0)
    return u * u * (3.0 - 2.0 * u)


def keep_char(ch):
    """点に使う字：文字（L）と数字（N）。空白・約物・記号は除く"""
    return unicodedata.category(ch)[0] in ("L", "N")


def source_chars(text):
    return "".join(c for c in text if keep_char(c))


def _crc(*parts):
    return zlib.crc32("|".join(str(p) for p in parts).encode("utf-8")) & 0xFFFFFFFF


# ---------------------------------------------------------------------------
# ink（字の質感）。点の層と読み字で同じ関数を使う。雑音は (字, 大きさ, seed) で固定（時間で変えない）

def ink_alpha(alpha, ink, key):
    """alpha（PIL 'L'）の字の中のインクを抜いた 'L' を返す。ink: {"mode": "kasure", "amount": 0〜0.5} か
    {"mode": "dots", "pitch": 4〜10}。key: 雑音を決める値（字・大きさ・seed）"""
    mode = ink["mode"]
    a = np.asarray(alpha, dtype=np.float64) / 255.0
    h, w = a.shape
    if mode == "kasure":
        amount = float(ink.get("amount", 0.3))
        if amount <= 0:
            return alpha
        rng = np.random.RandomState(_crc("kasure", *key))
        noise = Image.fromarray((rng.rand(h, w) * 255).astype(np.uint8), "L")
        noise = noise.filter(ImageFilter.GaussianBlur(max(0.8, min(h, w) * 0.04)))
        n = np.asarray(noise, dtype=np.float64)
        solid = a > 0.5
        if not solid.any():
            return alpha
        q = float(np.quantile(n[solid], amount))
        spread = max(float(n[solid].std()) * 0.35, 1e-6)       # 穴の縁のやわらかさ
        keep = np.clip((n - q) / spread + 0.5, 0.0, 1.0)
        out = a * keep
    elif mode == "dots":
        pitch = float(ink.get("pitch", 6))
        S = 4
        yy, xx = np.mgrid[0:h, 0:w]
        cell_x = ((xx + 0.5) // pitch).astype(np.int64)
        cell_y = ((yy + 0.5) // pitch).astype(np.int64)
        ncx = int(cell_x.max()) + 1
        cid = (cell_y * ncx + cell_x).ravel()
        tot = np.bincount(cid, weights=a.ravel(), minlength=int(cid.max()) + 1)
        cnt = np.bincount(cid, minlength=int(cid.max()) + 1).astype(np.float64)
        # 網の目の面積は pitch²（端は欠ける）。インクの平均 cov に対し、点の面積 = cov × 目の面積 → 半径は √cov に比例
        cov = tot / np.maximum(np.minimum(cnt, pitch * pitch), 1.0)
        rad = np.minimum(np.sqrt(np.clip(cov, 0.0, 1.0) / math.pi) * pitch, pitch * 0.7072)
        ys, xs = (np.mgrid[0:h * S, 0:w * S] + 0.5) / S
        cx = (np.floor(xs / pitch) + 0.5) * pitch
        cy = (np.floor(ys / pitch) + 0.5) * pitch
        c2 = (np.floor(ys / pitch).astype(np.int64) * ncx + np.floor(xs / pitch).astype(np.int64))
        c2 = np.clip(c2, 0, len(rad) - 1)
        inside = ((xs - cx) ** 2 + (ys - cy) ** 2) <= rad[c2] ** 2
        out = inside.reshape(h, S, w, S).mean(axis=(1, 3))
    else:
        raise ValueError(f"ink: 未知の mode '{mode}'")
    return Image.fromarray(np.clip(out * 255.0 + 0.5, 0, 255).astype(np.uint8), "L")


# ---------------------------------------------------------------------------
# 検査（direction の points）

def _err(msg):
    from look import LookError
    raise LookError(msg)


def check_ink(ink, where, read=False):
    if not isinstance(ink, dict) or not ink or "mode" not in ink:
        _err(f'direction: {where} の ink は {{"mode": "kasure", "amount": 0〜{INK_KASURE_MAX}}} か {{"mode": "dots", "pitch": 4〜10}} の形で書いてください')
    mode = ink["mode"]
    if mode not in INK_MODES:
        _err(f"direction: {where} の ink.mode '{mode}' は {', '.join(INK_MODES)} のどちらかです")
    allowed = {"mode", "amount"} if mode == "kasure" else {"mode", "pitch"}
    if set(ink) - allowed:
        _err(f"direction: {where} の ink に未知の項目 '{sorted(set(ink) - allowed)[0]}' があります（{mode}: {', '.join(sorted(allowed))}）")
    if mode == "kasure":
        a = ink.get("amount", 0.3)
        if not _num(a) or not 0 < a <= INK_KASURE_MAX:
            _err(f"direction: {where} の ink.amount は 0 より大きく {INK_KASURE_MAX} 以下で書いてください")
        if read and a > READ_INK_MAX_AMOUNT:
            _err(f"direction: {where} の読み字の ink.amount は {READ_INK_MAX_AMOUNT} 以下です（それより抜くと読めない）")
    else:
        p = ink.get("pitch", 6)
        if not _num(p) or not INK_DOTS_PITCH[0] <= p <= INK_DOTS_PITCH[1]:
            _err(f"direction: {where} の ink.pitch（網の間隔 px）は {INK_DOTS_PITCH[0]}〜{INK_DOTS_PITCH[1]} で書いてください")
        if read:
            _err(f"direction: {where} の読み字には kasure だけ掛けられます（dots は読み字に掛けない）")


def _check_pair(v, where, lo, hi, what, integer=False):
    if (not isinstance(v, list) or len(v) != 2 or not all(_num(x) for x in v) or v[0] > v[1]
            or not lo <= v[0] or not v[1] <= hi):
        _err(f"direction: {where} の {what} は [最小, 最大]（{lo}〜{hi}、最小≦最大）で書いてください")


TRACK_TIME_KEYS = ("at", "at_line", "at_time")


def parse_track(spec, kind, where, n_lines=None):
    """字数の時間変化 → (時刻の配列（区間の頭からの秒）, 字数の配列)。
    density（0〜3）か count（字数・本数）の1つ、または track（[{時刻, "density"|"count": 値}, ...]）。count が density より優先。
    時刻の書き方は3つ（1要素に1つだけ。T35 U3）：at（区間の頭からの秒）／at_line: N ＋ 任意の offset（行 N の開始からの秒、0 以上）／
    at_time（曲の頭からの絶対時刻）。at_line・at_time の要素は、行の時刻が要るので検査の段階では秒に直せない：時刻の配列にその位置の None を入れて返す
    （秒に直すのは resolve_track。直した後の秒で check_track・check_edges を掛ける）"""
    levels = LEVELS[kind]
    lo, hi = COUNT_RANGE[kind]

    def one(item, w):
        if "count" in item:
            c = item["count"]
            if isinstance(c, bool) or not isinstance(c, int) or not lo <= c <= hi:
                _err(f"direction: {w} の count は {lo}〜{hi} の整数で書いてください"
                     f"（{'本数' if kind == 'tate_line' else '字数'}。0 にしたいときは count ではなく density: 0）")
            return float(c)
        lv = item.get("density")
        if isinstance(lv, bool) or not isinstance(lv, int) or not 0 <= lv <= 3:
            _err(f"direction: {w} の density は 0〜3 の整数で書いてください")
        return float(levels[lv])

    if "track" in spec:
        if "density" in spec or "count" in spec:
            _err(f"direction: {where} は track と density／count を一緒に書けません")
        tr = spec["track"]
        if not isinstance(tr, list) or not tr:
            _err(f"direction: {where} の track は [{{\"at\": 秒, \"density\": 0〜3}}, ...] の配列で書いてください")
        ts, cs, prev = [], [], -1.0
        for i, it in enumerate(tr):
            w = f"{where}.track[{i}]"
            if (not isinstance(it, dict) or not set(it) <= set(TRACK_TIME_KEYS) | {"offset", "density", "count"}
                    or ("density" not in it and "count" not in it)):
                _err(f"direction: {w} は {{時刻（at・at_line・at_time のどれか1つ）, \"density\" か \"count\"}} で書いてください")
            kinds = [k for k in TRACK_TIME_KEYS if k in it]
            if len(kinds) != 1:
                _err(f"direction: {w} の時刻は at・at_line・at_time のどれか1つだけ書いてください（{'併記されています' if kinds else '時刻がありません'}）")
            if "offset" in it and "at_line" not in it:
                _err(f"direction: {w} の offset は at_line と一緒にだけ書けます")
            if "at_line" in it:
                n, off = it["at_line"], it.get("offset", 0)
                if isinstance(n, bool) or not isinstance(n, int) or n < 1 or (n_lines is not None and n > n_lines):
                    _err(f"direction: {w}.at_line が行番号（1〜{n_lines if n_lines is not None else '行数'}）ではありません")
                if not _num(off) or off < 0:
                    _err(f"direction: {w}.offset は 0 以上の秒で書いてください")
                ts.append(None)
                prev = -1.0
            elif "at_time" in it:
                if not _num(it["at_time"]) or it["at_time"] < 0:
                    _err(f"direction: {w}.at_time は 0 以上の秒（曲の頭から）で書いてください")
                ts.append(None)
                prev = -1.0
            else:
                at = it["at"]
                if not _num(at) or at < 0 or at < prev - 1e-12:
                    _err(f"direction: {w}.at は 0 以上で、前の値以上（昇順）の秒で書いてください")
                prev = float(at)
                ts.append(float(at))
            cs.append(one(it, w))
        return ts, cs
    if "density" not in spec and "count" not in spec:
        _err(f"direction: {where} に density か count（か track）がありません")
    return [0.0], [one(spec, where)]


def resolve_track(spec, kind, line_starts, t0, where):
    """at_line・at_time を使った track を、区間の頭からの秒の at に直した spec を返す（使っていなければ spec のまま）。
    line_starts：プランの各行の開始（秒。行番号 1 ＝ [0]）。t0：区間の頭（曲の頭からの秒）。直した後に昇順でなければ止める
    （行の時刻を直して順番が入れ替わったときに、黙って並べ替えない）。直した後の秒で check_track を掛ける（at_line の経路で検査を飛ばさない）。
    戻り値：(spec, 直したか)"""
    tr = spec.get("track")
    if not tr or not any(("at_line" in it or "at_time" in it) for it in tr):
        return spec, False
    out, prev = [], -1.0
    for i, it in enumerate(tr):
        w = f"{where}.track[{i}]"
        if "at_line" in it:
            n = it["at_line"]
            if n > len(line_starts):
                _err(f"direction: {w}.at_line {n} が行数（{len(line_starts)}）を超えています")
            at = line_starts[n - 1] + float(it.get("offset", 0)) - t0
        elif "at_time" in it:
            at = float(it["at_time"]) - t0
        else:
            at = float(it["at"])
        if at < -1e-9:
            _err(f"direction: {w} の時刻が区間の頭（{t0:.2f}秒）より前になります（{at + t0:.2f}秒）")
        at = max(at, 0.0)
        if at < prev - 1e-9:
            _err(f"direction: {w} の時刻（区間の頭から {at:.2f}秒）が前の要素（{prev:.2f}秒）より前になります。行の時刻・offset を見直してください（黙って並べ替えません）")
        prev = at
        new = {"at": at}
        for k in ("density", "count"):
            if k in it:
                new[k] = it[k]
        out.append(new)
    sp2 = dict(spec, track=out)
    ts, cs = parse_track(sp2, kind, where)
    check_track(ts, cs, where)
    return sp2, True


def check_track(ts, cs, where):
    """字数の増減の速さの規則（仕様 §3-6）：増減は 0.5 秒以上かける／0 フレームの切り替えは前の切り替えから 2 秒以上空ける／
    切り替えた後 1 秒以内に逆向きに戻さない"""
    if UNSAFE_SKIP_TRACK_RULES:
        return
    changes = []
    for i in range(len(ts) - 1):
        if abs(cs[i + 1] - cs[i]) > 1e-9:
            changes.append((ts[i], ts[i + 1], 1 if cs[i + 1] > cs[i] else -1))
    for k, (a, b, s) in enumerate(changes):
        d = b - a
        if 0 < d < MIN_RAMP_SEC - 1e-9:
            _err(f"direction: {where}：字数の増減（{a:.2f}〜{b:.2f}秒）が {d:.2f} 秒です。{MIN_RAMP_SEC} 秒以上かけるか、0 秒（一気に切り替え）にしてください")
        if d <= 1e-9 and k and a - changes[k - 1][1] < MIN_FLASH_GAP - 1e-9:
            _err(f"direction: {where}：一気に切り替える（{a:.2f}秒）のは、前の切り替えの終わり（{changes[k - 1][1]:.2f}秒）から {MIN_FLASH_GAP} 秒以上空けてください")
        if k and changes[k - 1][2] != s and a - changes[k - 1][1] < REVERSE_SEC - 1e-9:
            _err(f"direction: {where}：切り替えた後 {REVERSE_SEC} 秒以内（{changes[k - 1][1]:.2f}→{a:.2f}秒）に逆向きに戻しています（点滅になる）")


def check_edges(ts, cs, dur, where, head=EDGE_FADE_SEC, tail=EDGE_FADE_SEC):
    """区間の頭・尻の出入りも字数の増減として数える（仕様 §3-6）。区間の頭で 0 から増え、尻で 0 へ減るのを
    それぞれ1回の増減として、track の増減との間隔・逆向きの規則（check_track と同じ値）に掛ける。ts は区間の頭からの秒。
    head・tail は出入りの長さ（既定 EDGE_FADE_SEC。shape は 現れる長さ A と disperse を渡す）"""
    if UNSAFE_SKIP_TRACK_RULES:
        return
    changes = []
    if cs[0] > 1e-9:
        changes.append((0.0, head, 1))
    for i in range(len(ts) - 1):
        if abs(cs[i + 1] - cs[i]) > 1e-9:
            changes.append((ts[i], ts[i + 1], 1 if cs[i + 1] > cs[i] else -1))
    if cs[-1] > 1e-9:
        changes.append((dur - tail, dur, -1))
    changes.sort()
    label = f"{EDGE_FADE_SEC} 秒" if head == tail == EDGE_FADE_SEC else f"頭 {head:.2f} 秒・尻 {tail:.2f} 秒"
    for k in range(1, len(changes)):
        a0, b0, s0 = changes[k - 1]
        a, b, s = changes[k]
        if s0 != s and a - b0 < REVERSE_SEC - 1e-9:
            _err(f"direction: {where}：区間の頭・尻の出入り（{label}）と字数の増減が {REVERSE_SEC} 秒以内（{b0:.2f}→{a:.2f}秒）に逆向きになります（点滅になる）")


def shape_appear_sec(gather, draw_on):
    """shape の字が現れきるまでの長さ A（秒）＝ 字ごとの遅れの広がり（draw_on×gather）＋ 各字の現れ（移動の最初の 25%）。
    _init_shape・_at_shape の式と同じ（移動の長さは max(gather − draw_on×gather, 0.1)）。描画の式は変えず、この長さを検査する"""
    span = draw_on * gather
    return span + 0.25 * max(gather - span, 0.1)


def _check_shape(sh, where, size_hi):
    if not isinstance(sh, dict) or sh.get("type") not in SHAPE_KEYS:
        _err(f"direction: {where}.shape.type は {', '.join(SHAPE_KEYS)} のどれかで書いてください")
    typ = sh["type"]
    if set(sh) - SHAPE_KEYS[typ]:
        _err(f"direction: {where}.shape に未知の項目 '{sorted(set(sh) - SHAPE_KEYS[typ])[0]}' があります（{typ}: {', '.join(sorted(SHAPE_KEYS[typ]))}）")
    if "mask" in sh:
        _err(f"direction: {where}.shape.mask（輪郭のマスク画像）はまだ使えません（素材の登録を待っています）。点列（points）で書いてください")
    if typ != "outline":
        c = sh.get("center", [0.5, 0.42])
        if not (isinstance(c, list) and len(c) == 2 and all(_num(x) and 0 <= x <= 1 for x in c)):
            _err(f"direction: {where}.shape.center は [x, y]（0〜1 の割合）で書いてください")
        cx, cy = c[0] * W, c[1] * H
    for k in ("r0", "r", "r_max", "r_min", "gap"):
        if k in sh and (not _num(sh[k]) or sh[k] < 0):
            _err(f"direction: {where}.shape.{k} は 0 以上の数値で書いてください")
    rmax = 0.0
    if typ == "disc":
        r0, r = sh.get("r0", 0), sh.get("r")
        if r is None or not _num(r) or r <= r0 or r > MAX_R:
            _err(f"direction: {where}.shape.r（半径）は r0 より大きく {MAX_R} 以下で書いてください")
        rmax = r
    elif typ == "spiral":
        r0, rm, turns = sh.get("r0", 20), sh.get("r_max"), sh.get("turns")
        if rm is None or not _num(rm) or rm <= r0 or rm > MAX_R or not _num(turns) or not 0.5 <= turns <= 8:
            _err(f"direction: {where}.shape（spiral）は r_max（r0 より大きく {MAX_R} 以下）と turns（0.5〜8）が要ります")
        rmax = rm
    elif typ == "concentric":
        k, rmin, gap = sh.get("rings"), sh.get("r_min"), sh.get("gap")
        if isinstance(k, bool) or not isinstance(k, int) or not 2 <= k <= 12 or not _num(rmin) or rmin <= 0 or not _num(gap) or gap <= 0:
            _err(f"direction: {where}.shape（concentric）は rings（2〜12 の整数）・r_min・gap が要ります")
        rmax = rmin + (k - 1) * gap
        if rmax > MAX_R:
            _err(f"direction: {where}.shape の最大の半径が {rmax:.0f}px で、{MAX_R}px を超えます")
    else:
        pts = sh.get("points")
        if (not isinstance(pts, list) or len(pts) < 3 or not all(isinstance(p, list) and len(p) == 2 and all(_num(x) and 0 <= x <= 1 for x in p) for p in pts)):
            _err(f"direction: {where}.shape.points は [[x, y], ...]（0〜1 の割合、3点以上）で書いてください")
        if "closed" in sh and not isinstance(sh["closed"], bool):
            _err(f"direction: {where}.shape.closed は true / false で書いてください")
        xs = [p[0] * W for p in pts]
        ys = [p[1] * H for p in pts]
        if max(max(xs) - min(xs), max(ys) - min(ys)) > 2 * MAX_R + 1:
            _err(f"direction: {where}.shape.points の形が大きすぎます（中心から最大 {MAX_R}px＝幅 {2 * MAX_R}px まで）")
        return
    if cx - rmax < 0 or cx + rmax > W or cy - rmax < 0 or cy + rmax > H:
        _err(f"direction: {where}.shape が画面からはみ出します（中心 {cx:.0f},{cy:.0f}、半径 {rmax:.0f}px）")


def validate_points(points, n_lines, theme):
    """direction の points を検査する（未知の項目・範囲外の値は止める）。戻り値なし"""
    if theme is None:
        _err("direction: points（点の層）はテーマ（look）を使う曲でだけ使えます（字の色・書体をテーマの役から決めます）")
    if not isinstance(points, list) or not points:
        _err("direction: points は [{...}, ...] の配列で書いてください")
    for i, sp in enumerate(points):
        where = f"points[{i}]"
        if not isinstance(sp, dict):
            _err(f"direction: {where} はオブジェクトで書いてください")
        kind = sp.get("kind")
        if kind not in KINDS:
            _err(f"direction: {where}.kind は {', '.join(KINDS)} のどれかで書いてください")
        bad = set(sp) - KIND_KEYS[kind]
        if bad:
            _err(f"direction: {where}（{kind}）に未知の項目 '{sorted(bad)[0]}' があります（使える項目: {', '.join(sorted(KIND_KEYS[kind]))}）。黙って既定値に戻さず止めました")
        # span
        span = sp.get("span")
        if not isinstance(span, dict) or not span or set(span) - SPAN_KEYS:
            _err(f"direction: {where}.span は {{\"lines\": [a, b]}}・{{\"section\": 名前}}・{{\"start\"|\"after_line\", \"seconds\"|\"end\"|\"until_line\"|\"until\"}} の形で書いてください")
        if "lines" in span or "section" in span:
            if len(span) != 1:
                _err(f"direction: {where}.span の lines／section は、他の項目と一緒に書けません")
            if "lines" in span:
                ln = span["lines"]
                if (not isinstance(ln, list) or len(ln) != 2 or not all(isinstance(x, int) and not isinstance(x, bool) for x in ln)
                        or not 1 <= ln[0] <= ln[1] <= n_lines):
                    _err(f"direction: {where}.span.lines は [開始の行, 終わりの行]（1〜{n_lines}、開始≦終わり）で書いてください")
            elif not isinstance(span["section"], str) or not span["section"]:
                _err(f"direction: {where}.span.section は区分の名前（文字列）で書いてください")
        else:
            if ("start" in span) == ("after_line" in span):
                _err(f"direction: {where}.span は start（秒）か after_line（行）のどちらか1つを書いてください")
            ends = [k for k in ("seconds", "end", "until_line", "until") if k in span]
            if len(ends) != 1:
                _err(f"direction: {where}.span は seconds・end・until_line・until のどれか1つを書いてください")
            for k in ("start", "seconds", "end"):
                if k in span and (not _num(span[k]) or span[k] < 0):
                    _err(f"direction: {where}.span.{k} は 0 以上の秒で書いてください")
            for k in ("after_line", "until_line"):
                if k in span and not (isinstance(span[k], int) and not isinstance(span[k], bool) and 1 <= span[k] <= n_lines):
                    _err(f"direction: {where}.span.{k} が行番号（1〜{n_lines}）ではありません")
        # source
        src = sp.get("source", "line")
        if isinstance(src, dict):
            k = src.get("key")
            if set(src) != {"key"} or not isinstance(k, dict) or set(k) != {"line", "from", "len"}:
                _err(f"direction: {where}.source は \"line\"・\"section\"・{{\"key\": {{\"line\", \"from\", \"len\"}}}} のどれかで書いてください")
            if not (isinstance(k["line"], int) and 1 <= k["line"] <= n_lines and isinstance(k["from"], int) and k["from"] >= 0
                    and isinstance(k["len"], int) and k["len"] >= 1) or any(isinstance(v, bool) for v in k.values()):
                _err(f"direction: {where}.source.key は line（1〜{n_lines}）・from（0 以上）・len（1 以上）の整数で書いてください")
        elif src not in ("line", "section"):
            _err(f"direction: {where}.source は \"line\"・\"section\"・{{\"key\": ...}} のどれかで書いてください")
        # 大きさ・不透明度・seed・role・ink
        lo, hi = BANDS[kind]
        sz = sp.get("size_px", [lo, hi])
        _check_pair(sz, f"{where}", lo, hi, f"size_px（{kind} の帯 {lo}〜{hi}px。{'粒は 21px 以上を使わない' if kind == 'tsubu' else ''}）".replace("。）", "）"))
        op = sp.get("opacity", [0.3, 0.7])
        _check_pair(op, where, 0.1, 1.0, "opacity")
        if "seed" in sp and (isinstance(sp["seed"], bool) or not isinstance(sp["seed"], int)):
            _err(f"direction: {where}.seed は整数で書いてください")
        if "role" in sp:
            if not isinstance(sp["role"], str) or sp["role"] not in theme["fonts"]:
                _err(f"direction: {where}.role '{sp.get('role')}' がテーマの fonts にありません")
        if "ink" in sp:
            check_ink(sp["ink"], where)
        # 字数
        mode = sp.get("motion", "flow") if kind == "tate_line" else None
        if mode is not None and mode not in ("flow", "switch"):
            _err(f"direction: {where}.motion は flow か switch で書いてください")
        if mode == "switch":
            c = sp.get("count")
            if "density" in sp or "track" in sp or isinstance(c, bool) or not isinstance(c, int) or not 1 <= c <= 3:
                _err(f"direction: {where}（switch）は count（同時に出る本数 1〜3 の整数）だけで書いてください（density・track は使えません）")
            ts, cs = [0.0], [float(c)]
        else:
            ts, cs = parse_track(sp, kind, where, n_lines)
            if None not in ts:       # at_line・at_time を含む track は、行の時刻が要るので秒に直した後（kinetic._setup_points）に check_track を掛ける
                check_track(ts, cs, where)
        if kind == "shape" and max(cs) < 20:
            _err(f"direction: {where}（shape）の字数は 20 以上にしてください（形が分からない）")
        if kind == "tsubu":
            if "drift_px_s" in sp:
                _check_pair(sp["drift_px_s"], where, 0, 200, "drift_px_s")
        if kind == "tate_line":
            _check_tate(sp, where, hi_size=sz[1], nmax=int(max(cs)))
        if kind == "shape":
            _check_shape_item(sp, where)


def _check_tate(sp, where, hi_size, nmax):
    if "count_fade" in sp:
        if sp["count_fade"] not in ("rank", "linear"):
            _err(f"direction: {where}.count_fade は rank か linear で書いてください")
        if sp.get("motion", "flow") != "flow":
            _err(f"direction: {where}.count_fade は motion: flow の線だけに書けます")
    if "dir" in sp:
        if sp["dir"] not in TATE_DIRS:
            _err(f"direction: {where}.dir は {', '.join(TATE_DIRS)} のどれかで書いてください")
        if sp.get("motion", "flow") != "flow":
            _err(f"direction: {where}.dir は motion: flow の線だけに書けます（switch の線は動かないので向きを持ちません）")
    if nmax < 1:
        _err(f"direction: {where}（tate_line）の本数が全部 0 です。線を出さない点の層は書かないでください（density／count を 1 以上に）")
    if "speed_px_s" in sp:
        v = sp["speed_px_s"]
        if _num(v):
            v = [v, v]
        _check_pair(v, where, 20, 600, "speed_px_s")
    if "length_px" in sp:
        _check_pair(sp["length_px"], where, 300, 1600, "length_px")
    if "on_sec" in sp and (not _num(sp["on_sec"]) or not 0.1 <= sp["on_sec"] <= 2.0):
        _err(f"direction: {where}.on_sec（線が出ている時間）は 0.1〜2.0 秒で書いてください")
    if "beat_sync" in sp and not isinstance(sp["beat_sync"], bool):
        _err(f"direction: {where}.beat_sync は true / false で書いてください")
    n_slots = nmax if sp.get("motion", "flow") == "flow" else max(int((W - 2 * MARGIN) // (3 * hi_size)), 1)
    if (W - 2 * MARGIN) / n_slots < 3 * hi_size - 1e-9:
        _err(f"direction: {where}：{n_slots} 本の線を、字の 3 倍以上の間隔（字 {hi_size}px → {3 * hi_size}px）で左右の余白の内側（{W - 2 * MARGIN}px）に並べられません。"
             f"字を小さくするか本数を減らしてください")


def _check_shape_item(sp, where):
    if "shape" not in sp:
        _err(f"direction: {where}（shape）に shape（形の指定）がありません")
    _check_shape(sp["shape"], where, sp.get("size_px", [12, 60])[1])
    if sp.get("from", "scatter") not in FROM_MODES:
        _err(f"direction: {where}.from は {', '.join(FROM_MODES)} のどれかで書いてください")
    if sp.get("orient", "upright") not in ORIENTS:
        _err(f"direction: {where}.orient は {', '.join(ORIENTS)} のどちらかで書いてください")
    for k, lo, hi in (("gather", GATHER_MIN_SEC, 3.0), ("draw_on", 0.0, 1.0), ("disperse", DISPERSE_MIN_SEC, 1.5), ("flow", 0.0, 0.5), ("wobble_px", 0.0, 12.0)):
        if k in sp and (not _num(sp[k]) or not lo <= sp[k] <= hi):
            _err(f"direction: {where}.{k} は {lo}〜{hi} の数値で書いてください")
    # 既定値のまま通る組み合わせでも、現れる長さ・散りが 0.5 秒未満なら止める（仕様 §3-6「0.5 秒以上かけて増減」）
    disperse = sp.get("disperse", SHAPE_DEFAULTS["disperse"])
    if disperse < DISPERSE_MIN_SEC - 1e-9:
        _err(f"direction: {where}.disperse（散り）が {disperse} 秒です。{DISPERSE_MIN_SEC}〜1.5 秒で書いてください（書かないときの既定 {SHAPE_DEFAULTS['disperse']} 秒は範囲の外です。0 は区間の尻で一気に消えます）")
    a_in = shape_appear_sec(sp.get("gather", SHAPE_DEFAULTS["gather"]), sp.get("draw_on", SHAPE_DEFAULTS["draw_on"]))
    if a_in < EDGE_FADE_SEC - 1e-9:
        _err(f"direction: {where}（shape）の字が現れきるまでが {a_in:.2f} 秒です。{EDGE_FADE_SEC} 秒以上にしてください"
             f"（現れる長さ ＝ draw_on×gather ＋ 0.25×max(gather − draw_on×gather, 0.1)。gather を長く／draw_on を大きく）")
    if "big" in sp:
        b = sp["big"]
        if not isinstance(b, dict) or set(b) != {"ratio", "size_px"} or not _num(b["ratio"]) or not 0 < b["ratio"] <= 0.3:
            _err(f"direction: {where}.big は {{\"ratio\": 0〜0.3、\"size_px\": [最小, 最大]}} で書いてください（大きめの字を少数混ぜる）")
        _check_pair(b["size_px"], f"{where}.big", 12, 60, "size_px")


# ---------------------------------------------------------------------------
# 読み字の周りの空け（画素ごとの被覆の頭打ち）
#
# 点の層の字は重なる。字ごとに不透明度を 0.25 以下にしても、重なると被覆は 0.44・0.58 と積み上がり、読み字の比（4.5:1）が崩れる。
# そこで空けの範囲にかかる字は、いったん被覆（'L'）に「上に重ねる」式で貼り、画素ごとに上限で頭打ちしてから、色を1回だけ貼る。
# 空けの外の字は今までどおり frame に直接貼る（見え方を変えない）。色は全層で同じ（その時刻の本文色）なので、全層を1枚の被覆に集める。

def clear_zones(rects, t):
    """時刻 t に効いている空けの範囲。[{"w": 効き具合 0〜1（行の出入り 0.2 秒）, "box": 広げた矩形, "roi": 縁の戻り幅を足した矩形}]。
    rects: 読み字の外接矩形（[{"ts","te","box","h"}]。PointLayer.rects。層をまたいで足してよい）"""
    out = []
    for r in rects:
        ts, te = r["ts"], r["te"]
        w = min((t - (ts - CLEAR_RAMP)) / CLEAR_RAMP, ((te + CLEAR_RAMP) - t) / CLEAR_RAMP, 1.0)
        if w <= 0:
            continue
        x0, y0, x1, y1 = r["box"]
        pad = r["h"] * CLEAR_PAD_RATIO
        box = (x0 - pad, y0 - pad, x1 + pad, y1 + pad)
        out.append({"w": float(_smooth(w)), "box": box,
                    "roi": (box[0] - CLEAR_SOFT, box[1] - CLEAR_SOFT, box[2] + CLEAR_SOFT, box[3] + CLEAR_SOFT)})
    return out


def _hits_zone(zones, pos, size):
    x, y = pos
    w, h = size
    for z in zones:
        r = z["roi"]
        if x < r[2] and x + w > r[0] and y < r[3] and y + h > r[1]:
            return True
    return False


_CAP_MEMO = {}
_CAP_MEMO_MAX = 256


def _cap_roi(z):
    """空けの範囲 z の、被覆の上限（0〜255 の整数）を画素ごとに持つ小さな配列と、その左上（画面の座標）。
    入力（roi・box・効き具合 w）が同じなら結果は同じ（純関数）なので、覚えておく（行の途中の w＝1 の間は毎フレーム同じ。T35 R0-12）。
    返した配列は読むだけ（呼び出し側は上限として使い、書き換えない）"""
    if z["w"] < 1.0:        # 行の出入りの 0.2 秒は w が毎フレーム違うので覚えない（覚えると 1 件約 0.9MB が積もる）
        return _cap_roi_calc(z)
    key = (z["roi"], z["box"])
    hit = _CAP_MEMO.get(key)
    if hit is not None:
        return hit
    out = _cap_roi_calc(z)
    if len(_CAP_MEMO) >= _CAP_MEMO_MAX:
        _CAP_MEMO.clear()
    _CAP_MEMO[key] = out
    return out


def _cap_roi_calc(z):
    r = z["roi"]
    X0, Y0 = max(int(math.floor(r[0])), 0), max(int(math.floor(r[1])), 0)
    X1, Y1 = min(int(math.ceil(r[2])), W), min(int(math.ceil(r[3])), H)
    if X1 <= X0 or Y1 <= Y0:
        return None
    xs = np.arange(X0, X1) + 0.5
    ys = np.arange(Y0, Y1) + 0.5
    bx0, by0, bx1, by1 = z["box"]
    dx = np.maximum(np.maximum(bx0 - xs, xs - bx1), 0.0)[None, :]
    dy = np.maximum(np.maximum(by0 - ys, ys - by1), 0.0)[:, None]
    d = np.hypot(dx, dy)
    cap = 1.0 - z["w"] * (1.0 - CLEAR_OPACITY) * (1.0 - np.clip(d / CLEAR_SOFT, 0.0, 1.0))
    return X0, Y0, np.rint(cap * 255.0).astype(np.uint8)


def composite_cov(frame, cov, color, zones, bb=None):
    """被覆 cov（'L'）を空けの範囲ごとに頭打ちして、色を1回だけ frame に貼る。frame が None なら貼らず、
    空けの範囲の核（広げた矩形の中）の被覆の最大（0〜1。頭打ちの後の値）だけ返す。全層で色が同じことが前提（層ごとに色が違う場合は使えない）。
    bb：cov に貼った字の外接の和 [x0, y0, x1, y1]（画面内に切った範囲。cov のうち 0 でない画素は必ずこの中）。渡されると、全面でなくこの範囲だけを
    複製・頭打ち・貼り付けする（結果の画素は全面で処理したときと同じ。T35 R0-12）。None なら cov の 0 でない範囲を調べる（従来）"""
    if bb is None:
        bb = cov.getbbox()
    if bb is None:
        return 0.0
    bx0_, by0_, bx1_, by1_ = [int(v) for v in bb]
    arr = np.array(cov.crop((bx0_, by0_, bx1_, by1_)))      # 範囲だけの複製。arr[y - by0_, x - bx0_] が画面の (x, y)
    for z in zones:
        c = _cap_roi(z)
        if c is None:
            continue
        X0, Y0, cap = c
        ix0, iy0 = max(X0, bx0_), max(Y0, by0_)
        ix1, iy1 = min(X0 + cap.shape[1], bx1_), min(Y0 + cap.shape[0], by1_)
        if ix1 <= ix0 or iy1 <= iy0:
            continue          # 範囲の外は被覆 0（頭打ちしても 0）
        sub = arr[iy0 - by0_:iy1 - by0_, ix0 - bx0_:ix1 - bx0_]
        np.minimum(sub, cap[iy0 - Y0:iy1 - Y0, ix0 - X0:ix1 - X0], out=sub)
    core = 0
    for z in zones:
        if z["w"] < 1.0 - 1e-9:              # 行の出入りの 0.2 秒は上限が段階的に戻る途中なので、核の検査に入れない
            continue
        bx0, by0, bx1, by1 = z["box"]
        X0, Y0 = max(int(math.ceil(bx0)), 0), max(int(math.ceil(by0)), 0)
        X1, Y1 = min(int(math.floor(bx1)), W), min(int(math.floor(by1)), H)
        ix0, iy0, ix1, iy1 = max(X0, bx0_), max(Y0, by0_), min(X1, bx1_), min(Y1, by1_)
        if X1 > X0 and Y1 > Y0 and ix1 > ix0 and iy1 > iy0:     # 範囲の外は 0（最大に影響しない）
            core = max(core, int(arr[iy0 - by0_:iy1 - by0_, ix0 - bx0_:ix1 - bx0_].max()))
    if frame is not None:
        frame.paste(color, (bx0_, by0_), Image.fromarray(arr, "L"))
    return core / 255.0


def render_points(frame, t, color, items, sprites, rects, pre=None):
    """点の層（items: [(PointLayer, font_ref)]）を貼る。frame が None なら、被覆だけ作って空けの核の最大を測る（検査用）。
    戻り値: (貼った字数, 60px を超える字数, 空けの核の被覆の最大 0〜1。頭打ちの後の値＝コードの自己確認で、測定ではない)
    前提：全層で色が同じ（その時刻の本文色 1 色）。被覆を全層で 1 枚に集めて 1 色で貼るため、層ごとに色が違う場合は使えない。
    色を分ける変更が来たら、R0-3 の決定（被覆の持ち方）に戻る。
    pre：{層の番号: その時刻の points_at の結果}（走査が同じ時刻の points_at を二重に計算しないため。無ければ層が自分で計算する）"""
    zones = clear_zones(rects, t)
    cov = Image.new("L", (W, H), 0) if zones else None
    bb = [W, H, 0, 0] if zones else None          # cov に貼った字の外接の和（空 ＝ x0 ≧ x1）
    total = big = 0
    for L, font_ref in items:
        if t < L.t0 - 1e-9 or t > L.t1 + 1e-9:
            continue
        n, b = L.draw(frame, t, color, sprites, font_ref, zones=zones, cov=cov, cov_bb=bb,
                      pts=(pre or {}).get(L.index))
        total += n
        big += b
    cmax = 0.0
    if zones and bb[2] > bb[0] and bb[3] > bb[1]:
        cmax = composite_cov(frame, cov, color, zones, bb=tuple(bb))
    return total, big, cmax


# ---------------------------------------------------------------------------
# 字の画像（点の層専用のキャッシュ）

class PointSprites:
    """1字の「型」（'L' のマスク。色は貼るときに塗る）。大きさ＝SIZE_STEPS、不透明度＝1/16、角度＝15°。
    上限つきの LRU（CACHE_MAX 枚）。点の層の外（読み字のスプライト）とは別のキャッシュ"""

    def __init__(self, fonts):
        self.fonts = fonts
        self._base = {}
        self._ops = OrderedDict()
        self.hits = self.misses = 0

    def base(self, font_ref, ch, size, angle, ink, seed):
        key = (font_ref, ch, size, angle, None if ink is None else tuple(sorted(ink.items())), seed if ink else 0)
        m = self._base.get(key)
        if m is None:
            side = int(size * 1.15) + 2
            m = Image.new("L", (side, side), 0)
            ImageDraw.Draw(m).text((side / 2, side / 2), ch, font=self.fonts.get(font_ref, size), fill=255, anchor="mm")
            if ink is not None:
                m = ink_alpha(m, ink, (ch, size, seed))
            if angle:
                m = m.rotate(angle, resample=Image.BILINEAR, expand=True)
            self._base[key] = m
        return m

    def mask(self, font_ref, ch, size, op16, angle, ink, seed):
        key = (font_ref, ch, size, op16, angle, None if ink is None else tuple(sorted(ink.items())), seed if ink else 0)
        m = self._ops.get(key)
        if m is not None:
            self._ops.move_to_end(key)
            self.hits += 1
            return m
        self.misses += 1
        b = self.base(font_ref, ch, size, angle, ink, seed)
        if op16 >= 16:
            m = b
        else:
            lut = [int(v * op16 / 16.0 + 0.5) for v in range(256)]
            m = b.point(lut)
        self._ops[key] = m
        if len(self._ops) > CACHE_MAX:
            self._ops.popitem(last=False)
        return m

    def size(self):
        return len(self._ops)


# ---------------------------------------------------------------------------
# 点の層

class PointLayer:
    """点の層の1件（direction の points の1要素）。points_at(t) が各点の字・位置・大きさ・不透明度・角度を返す。
    すべて 時刻 t と、初期化で決めた seed 固定の配列だけから決まる（閉じた式）。

    spec: 検査済みの points の要素。index: points の何件目か（seed の既定）。t0・t1: 区間（秒）。
    variants: [(その字が使われ始める時刻, 字の文字列)]（昇順）。source が "line" のときは行ごとに変わる。
    rects: 読み字の外接矩形（広げる前）。[{"ts","te","box":(x0,y0,x1,y1),"h":字高}]（ts〜te の間、空けを作る）
    beats: 拍の時刻（tate_line の switch の出る時刻を拍に合わせる。無ければ一定の間隔）。anchor: 読み字の中心（from: line の出発点）"""

    def __init__(self, spec, index, t0, t1, variants, rects, beats=None, anchor=None):
        self.spec = spec
        self.index = index
        self.kind = spec["kind"]
        self.t0, self.t1 = float(t0), float(t1)
        self.dur = self.t1 - self.t0
        if self.dur <= 0.05:
            _err(f"points[{index}]：区間が短すぎます（{self.t0:.2f}〜{self.t1:.2f}秒）")
        self.seed = int(spec.get("seed", index))
        self.rects = rects
        self.anchor = anchor or (W / 2, H / 2)
        lo, hi = spec.get("size_px", list(BANDS[self.kind]))
        self.size_lo, self.size_hi = lo, hi
        self.op_lo, self.op_hi = spec.get("opacity", [0.3, 0.7])
        self.ink = dict(spec["ink"]) if spec.get("ink") else None
        self.role = spec.get("role")
        self.variants = [(float(a), np.array(list(s))) for a, s in variants]
        self._vstarts = [a for a, _ in self.variants]
        if not self.variants or any(len(a) == 0 for _, a in self.variants):
            _err(f"points[{index}]：点に使う字が空です（source の行に文字がありません）")
        self.rng = np.random.RandomState((self.seed * 7919 + 104729 + index) % (2 ** 32))
        self.beats = list(beats or [])
        self.big_px = 0
        getattr(self, "_init_" + self.kind)(spec)
        self.size_max = max(self.size_hi, self.big_px)
        self._rect_arr = None

    # --- 共通 ---

    def chars_arr(self, t):
        k = max(bisect.bisect_right(self._vstarts, t) - 1, 0)
        return self.variants[k][1]

    def _track_n(self, tau):
        return float(np.interp(tau, self.tr_t, self.tr_c))

    def _setup_track(self, spec):
        ts, cs = parse_track(spec, self.kind, f"points[{self.index}]")
        if None in ts:
            _err(f"points[{self.index}]：at_line・at_time が秒に直されていません（kinetic._setup_points の resolve_track を通してください）")
        t = np.array(ts, dtype=np.float64)
        for i in range(1, len(t)):                      # 同じ時刻（一気に切り替え）は 1e-6 秒ずらして補間を定義する
            if t[i] <= t[i - 1]:
                t[i] = t[i - 1] + 1e-6
        self.tr_t, self.tr_c = t, np.array(cs, dtype=np.float64)
        self.nmax = int(max(cs))
        if self.kind in ("tsubu", "tate_line"):      # 区間の頭・尻の出入り（EDGE_FADE_SEC）も増減として数える
            check_edges(ts, cs, self.dur, f"points[{self.index}]")
        elif self.kind == "shape":                   # shape は 頭＝現れる長さ A、尻＝散り（disperse）
            d = {k: float(spec.get(k, v)) for k, v in SHAPE_DEFAULTS.items()}
            check_edges(ts, cs, self.dur, f"points[{self.index}]", head=shape_appear_sec(d["gather"], d["draw_on"]), tail=d["disperse"])

    def _rank_vis(self, tau):
        """順位が N(t) より小さい点の見え方（0〜1）。字ごとに RANK_FADE_SEC かけて出入りする（箱形の平均）。
        count_fade: "linear"（tate_line の flow）のときは、順位ごとの見え方 ＝ clip(本数(t) − 順位, 0, 1)（本数の変化にそのまま付いて、ゆっくり薄く・濃くなる。T35 R2）"""
        if getattr(self, "_count_linear", False):
            return np.clip(float(np.interp(tau, self.tr_t, self.tr_c)) - self.rank, 0.0, 1.0)
        ks = tau - (RANK_FADE_SEC / 8.0) * np.arange(9)
        ns = np.interp(ks, self.tr_t, self.tr_c)
        return (ns[:, None] > self.rank[None, :]).mean(axis=0)

    def _edge_env(self, t):
        return float(_smooth(min(t - self.t0, self.t1 - t) / EDGE_FADE_SEC))

    def points_at(self, t):
        """時刻 t の点。{"ch": 字の配列, "x","y": 中心（px）, "size": 段階, "op": 不透明度（1/16 刻みへ丸める前）, "ang": 角度（15°刻み）}。
        区間の外・何も出ない時刻は 空の配列。読み字の空け（不透明度の上限）はここでは掛けない：貼るときに画素ごとに掛ける（render_points）"""
        if t < self.t0 - 1e-9 or t > self.t1 + 1e-9:
            return self._empty()
        d = getattr(self, "_at_" + self.kind)(t, t - self.t0)
        if d is None:
            return self._empty()
        idx, x, y, size, op, ang = d
        if len(idx) == 0:
            return self._empty()
        keep = op >= (0.5 / 16.0)
        if not keep.all():
            idx, x, y, size, op, ang = idx[keep], x[keep], y[keep], size[keep], op[keep], ang[keep]
        arr = self.chars_arr(t)
        return {"ch": arr[idx % len(arr)], "x": x, "y": y, "size": size, "op": op, "ang": ang}

    @staticmethod
    def _empty():
        z = np.zeros(0)
        return {"ch": np.array([], dtype="U1"), "x": z, "y": z, "size": z.astype(int), "op": z, "ang": z.astype(int)}

    def draw(self, frame, t, color, sprites, font_ref, zones=None, cov=None, cov_bb=None, pts=None):
        """frame（PIL RGB）に貼る。color: (r,g,b)。貼った字数と、60px を超える字数を返す。
        zones（読み字の空けの範囲。clear_zones）がある場合、空けの範囲にかかる字は frame でなく cov（'L' の被覆）に貼る
        （空けの範囲の頭打ちを、重なりを含めた画素の被覆で掛けるため。composite_cov で1回だけ色を貼る）。frame が None なら cov だけ作る"""
        p = pts if pts is not None else self.points_at(t)
        n = len(p["x"])
        if n == 0:
            return 0, 0
        op16 = np.clip(np.rint(p["op"] * 16.0), 0, 16).astype(int)
        xs = np.rint(p["x"]).astype(int)
        ys = np.rint(p["y"]).astype(int)
        seed = self.seed
        ink = self.ink
        big = 0
        for ch, x, y, sz, o, a in zip(p["ch"].tolist(), xs.tolist(), ys.tolist(), p["size"].tolist(), op16.tolist(), p["ang"].tolist()):
            if o <= 0:
                continue
            if sz > BIG_PX:
                big += 1
            m = sprites.mask(font_ref, ch, sz, o, a, ink, seed)
            pos = (x - m.width // 2, y - m.height // 2)
            if zones and _hits_zone(zones, pos, m.size):
                cov.paste(255, pos, m)
                if cov_bb is not None:        # 貼った字の外接を、画面内に切って和に足す（composite_cov が全面でなくこの範囲だけ処理する）
                    cov_bb[0] = min(cov_bb[0], max(pos[0], 0))
                    cov_bb[1] = min(cov_bb[1], max(pos[1], 0))
                    cov_bb[2] = max(cov_bb[2], min(pos[0] + m.width, W))
                    cov_bb[3] = max(cov_bb[3], min(pos[1] + m.height, H))
            elif frame is not None:
                frame.paste(color, pos, m)
        return n, big

    # --- nominal（検査用の字数）---

    def nominal(self, t):
        """時刻 t の字数（読み字を除く、点の層の字数の目安。1フレームの上限の検査に使う）"""
        if t < self.t0 or t > self.t1:
            return 0
        return getattr(self, "_nominal_" + self.kind)(t - self.t0)

    def nominal_max(self):
        return getattr(self, "_nominal_max_" + self.kind)()

    # === tsubu ===

    def _init_tsubu(self, spec):
        self._setup_track(spec)
        n = self.nmax
        r = self.rng
        self.x0, self.y0 = r.rand(n) * W, r.rand(n) * H
        ang = r.rand(n) * 2 * math.pi
        dlo, dhi = spec.get("drift_px_s", [20, 60])
        sp = dlo + r.rand(n) * (dhi - dlo)
        self.vx, self.vy = np.cos(ang) * sp, np.sin(ang) * sp
        self.wa = 4 + 8 * r.rand(n)
        self.wp = 1.5 + 2.5 * r.rand(n)
        self.ph1, self.ph2 = r.rand(n) * 2 * math.pi, r.rand(n) * 2 * math.pi
        steps = np.array(_steps_in(self.size_lo, self.size_hi))
        self.sizes = steps[r.randint(0, len(steps), n)]
        self.opa = self.op_lo + r.rand(n) * (self.op_hi - self.op_lo)
        self.rank = r.permutation(n).astype(np.float64)
        self.idx = np.arange(n)

    def _at_tsubu(self, t, tau):
        vis = self._rank_vis(tau) * self._edge_env(t)
        on = vis > 0.0
        if not on.any():
            return None
        i = np.nonzero(on)[0]
        x = (self.x0[i] + self.vx[i] * tau + self.wa[i] * np.sin(2 * math.pi * tau / self.wp[i] + self.ph1[i])) % W
        y = (self.y0[i] + self.vy[i] * tau + self.wa[i] * np.cos(2 * math.pi * tau / (self.wp[i] * 1.31) + self.ph2[i])) % H
        return self.idx[i], x, y, self.sizes[i], self.opa[i] * vis[i], np.zeros(len(i), dtype=int)

    def _nominal_tsubu(self, tau):
        return int(math.ceil(self._track_n(tau) - 1e-9))

    def _nominal_max_tsubu(self):
        return self.nmax

    # === tate_line ===

    def _init_tate_line(self, spec):
        self.motion = spec.get("motion", "flow")
        r = self.rng
        lo_len, hi_len = spec.get("length_px", [400, 1200]) if self.motion == "flow" else spec.get("length_px", [300, 900])
        steps = np.array(_steps_in(self.size_lo, self.size_hi))
        if self.motion == "flow":
            self._count_linear = spec.get("count_fade", "rank") == "linear"
            self._setup_track(spec)
            L = self.nmax
            slot_w = (W - 2 * MARGIN) / L
            perm = r.permutation(L)
            self.l_x = MARGIN + (perm + 0.5) * slot_w
            self.l_size = steps[r.randint(0, len(steps), L)]
            self.l_len = lo_len + r.rand(L) * (hi_len - lo_len)
            sp = spec.get("speed_px_s", [80, 240])
            slo, shi = (sp, sp) if _num(sp) else sp
            speed = slo + r.rand(L) * (shi - slo)
            sign = np.where(r.rand(L) < 0.5, 1.0, -1.0)      # 乱数は dir に関係なく必ず引く（後ろの乱数をずらさない）
            if spec.get("dir") == "down":
                sign = np.ones(L)
            elif spec.get("dir") == "up":
                sign = -np.ones(L)
            self.l_v = speed * sign                          # 符号＝向き（＋は下へ）、絶対値＝速さ
            self.l_s0 = r.rand(L) * (H + self.l_len)
            self.l_op = self.op_lo + r.rand(L) * (self.op_hi - self.op_lo)
            self.l_off = r.randint(0, 997, L)
            self.rank = np.arange(L, dtype=np.float64)       # 線の順位（位置は perm でばらしてある）
            n_l = np.maximum((self.l_len / (self.l_size * 1.05)).astype(int), 2)
            self.l_n = n_l
            line_id = np.repeat(np.arange(L), n_l)
            k = np.concatenate([np.arange(n) for n in n_l])
            self.e_line, self.e_k = line_id, k
            self.e_cidx = self.l_off[line_id] + k
        else:
            self.count = int(spec["count"])
            self.on = float(spec.get("on_sec", 0.4))
            self.fade = min(0.05, self.on / 4)
            size = int(steps[-1]) if len(steps) else self.size_hi
            self.sw_slots = max(int((W - 2 * MARGIN) // (3 * self.size_hi)), 1)
            self.sw_slot_w = (W - 2 * MARGIN) / self.sw_slots
            use_beats = spec.get("beat_sync", True) and any(self.t0 <= b <= self.t1 - self.on for b in self.beats)
            ev, last = [], -1e9
            if use_beats:
                for b in self.beats:
                    if b < self.t0 - 1e-9 or b > self.t1 - self.on + 1e-9:
                        continue
                    if b - last >= self.on - 1e-9:
                        ev.append(b)
                        last = b
            else:
                per = max(self.on * 1.6, 0.5)
                tt = self.t0
                while tt <= self.t1 - self.on + 1e-9:
                    ev.append(tt)
                    tt += per
            if not ev:
                _err(f"points[{self.index}]（switch）：区間（{self.dur:.2f}秒）に線を出せません（on_sec {self.on}）")
            self.ev = np.array(ev)
            self._ev_cache = {}
            self.sw_lo_len, self.sw_hi_len = lo_len, hi_len

    def _event(self, e):
        """e 番目の出来事の線（位置・長さ・字の位置は seed と e から決まる。時間で動かない）"""
        c = self._ev_cache.get(e)
        if c is not None:
            return c
        r = np.random.RandomState((self.seed * 104729 + e * 7919 + 17) % (2 ** 32))
        slots = r.permutation(self.sw_slots)[:self.count]
        steps = np.array(_steps_in(self.size_lo, self.size_hi))
        xs, ys, szs, ops, cidx, ks, lens = [], [], [], [], [], [], []
        for s in slots:
            size = int(steps[r.randint(0, len(steps))])
            ln = self.sw_lo_len + r.rand() * (self.sw_hi_len - self.sw_lo_len)
            n = max(int(ln / (size * 1.05)), 2)
            top = r.rand() * max(H - ln, 1.0)
            off = int(r.randint(0, 997))
            op = self.op_lo + r.rand() * (self.op_hi - self.op_lo)
            x = MARGIN + (s + 0.5) * self.sw_slot_w
            kk = np.arange(n)
            xs.append(np.full(n, x))
            ys.append(top + (kk + 0.5) * size * 1.05)
            szs.append(np.full(n, size))
            frac = kk * size * 1.05 / ln
            ops.append(op * np.where(frac <= 0.7, 1.0, np.clip((1.0 - frac) / 0.3, 0.0, 1.0)))
            cidx.append(off + kk)
        c = tuple(np.concatenate(v) for v in (xs, ys, szs, ops, cidx))
        self._ev_cache[e] = c
        return c

    def _at_tate_line(self, t, tau):
        if self.motion == "switch":
            e = bisect.bisect_right(self.ev, t + 1e-9) - 1
            if e < 0:
                return None
            into = t - self.ev[e]
            if into >= self.on:
                return None
            a = min(into / self.fade, (self.on - into) / self.fade, 1.0)
            if a <= 0:
                return None
            x, y, sz, op, ci = self._event(e)
            return ci, x, y, sz.astype(int), op * a, np.zeros(len(x), dtype=int)
        vis_l = self._rank_vis(tau) * self._edge_env(t)
        v_e = vis_l[self.e_line]
        on = v_e > 0
        if not on.any():
            return None
        ln = self.e_line[on]
        k = self.e_k[on]
        size = self.l_size[ln]
        pitch = size * 1.05
        span_len = H + self.l_len[ln]
        head = (self.l_s0[ln] + np.abs(self.l_v[ln]) * tau) % span_len   # 進んだ距離（向きによらず増える）
        down = self.l_v[ln] > 0
        d = k * pitch + pitch / 2
        y = np.where(down, head - d, H - head + d)                       # 頭（k＝0・濃い側）が進む向きの先、尾が後ろ
        frac = k * pitch / self.l_len[ln]
        tail = np.where(frac <= 0.7, 1.0, np.clip((1.0 - frac) / 0.3, 0.0, 1.0))
        keep = (y > -size) & (y < H + size) & (tail > 0)
        if not keep.any():
            return None
        ln, k, y, size, tail = ln[keep], k[keep], y[keep], size[keep], tail[keep]
        sel = np.nonzero(on)[0][keep]
        op = self.l_op[ln] * tail * v_e[sel]
        return (self.e_cidx[sel], self.l_x[ln], y, size.astype(int), op, np.zeros(len(y), dtype=int))

    def _nominal_tate_line(self, tau):
        if self.motion == "switch":
            e = bisect.bisect_right(self.ev, self.t0 + tau + 1e-9) - 1
            if e < 0 or self.t0 + tau - self.ev[e] >= self.on:
                return 0
            return int(len(self._event(e)[0]))
        n = int(math.ceil(self._track_n(tau) - 1e-9))
        return int(self.l_n[:n].sum())

    def _nominal_max_tate_line(self):
        if self.motion == "switch":
            return int(max(len(self._event(e)[0]) for e in range(len(self.ev))))
        return int(self.l_n[:int(math.ceil(self.tr_c.max()))].sum())

    # === shape ===

    @staticmethod
    def _alloc(radii, n_total):
        w = np.asarray(radii, dtype=np.float64)
        n = np.maximum(np.rint(n_total * w / w.sum()).astype(int), 1)
        diff = n_total - int(n.sum())
        j = len(n) - 1
        while diff != 0:
            if diff > 0:
                n[j] += 1
                diff -= 1
            elif n[j] > 1:
                n[j] -= 1
                diff += 1
            j = (j - 1) % len(n)
            if diff < 0 and (n <= 1).all():
                break
        return n

    @staticmethod
    def _path_table(xy, closed, m=2048):
        """折れ線 xy（k,2）を、弧長で等間隔の表（m 点）にする。s=0..1 に対応。閉じた道は s=1 が s=0 に戻る"""
        pts = np.asarray(xy, dtype=np.float64)
        if closed:
            pts = np.vstack([pts, pts[:1]])
        seg = np.hypot(*(np.diff(pts, axis=0).T))
        cum = np.concatenate([[0.0], np.cumsum(seg)])
        s = np.linspace(0.0, cum[-1], m)
        return np.stack([np.interp(s, cum, pts[:, 0]), np.interp(s, cum, pts[:, 1])], axis=1), float(cum[-1])

    def _init_shape(self, spec):
        self._setup_track(spec)
        r = self.rng
        n = self.nmax
        sh = spec["shape"]
        typ = sh["type"]
        self.gather = float(spec.get("gather", SHAPE_DEFAULTS["gather"]))
        self.draw_on = float(spec.get("draw_on", SHAPE_DEFAULTS["draw_on"]))
        self.disperse = float(spec.get("disperse", SHAPE_DEFAULTS["disperse"]))
        self.flow = float(spec.get("flow", 0.1))
        self.wob = float(spec.get("wobble_px", 3.0))
        self.orient = spec.get("orient", "upright")
        if self.gather + self.disperse > self.dur + 1e-9:
            _err(f"points[{self.index}]（shape）：集まり {self.gather}秒＋散り {self.disperse}秒が区間（{self.dur:.2f}秒）を超えます")
        self.typ = typ
        steps = np.array(_steps_in(self.size_lo, self.size_hi))
        self.sizes = steps[r.randint(0, len(steps), n)]
        self.open_path = False
        if typ in ("disc", "concentric"):
            cx, cy = sh.get("center", [0.5, 0.42])
            self.cx, self.cy = cx * W, cy * H
            if typ == "disc":
                r0, R = float(sh.get("r0", 0)), float(sh["r"])
                m = max(1, int(round((R - r0) / (1.1 * float(np.mean(self.sizes))))))
                m = min(m, n)
                radii = r0 + (np.arange(m) + 0.5) * (R - r0) / m
                signs = np.ones(m)
                mult = 1.0 + 0.6 * np.arange(m) / max(m - 1, 1)
            else:
                k = int(sh["rings"])
                radii = float(sh["r_min"]) + np.arange(k) * float(sh["gap"])
                m = min(k, n)
                radii = radii[:m]
                signs = np.where(np.arange(m) % 2 == 0, 1.0, -1.0)
                mult = 1.0 + 0.5 * ((np.arange(m) * 3) % 5) / 4.0
            cnt = self._alloc(radii, n)
            ring = np.repeat(np.arange(m), cnt)
            th = np.concatenate([2 * math.pi * np.arange(c) / c + r.rand() * 2 * math.pi for c in cnt])
            self.p_r = radii[ring]
            self.p_th = th
            self.p_om = 2 * math.pi * self.flow * signs[ring] * mult[ring]
            self.ord = np.arange(n)
        else:
            if typ == "spiral":
                cx, cy = sh.get("center", [0.5, 0.42])
                self.cx, self.cy = cx * W, cy * H
                r0, rm, turns = float(sh.get("r0", 20)), float(sh["r_max"]), float(sh["turns"])
                thv = np.linspace(0.0, 2 * math.pi * turns, 4096)
                rr = r0 + (rm - r0) * thv / thv[-1]
                xy = np.stack([self.cx + rr * np.cos(thv), self.cy + rr * np.sin(thv)], axis=1)
                self.open_path = True
                self.size_graded = True
            else:
                pts = np.array(sh["points"], dtype=np.float64) * [W, H]
                xy = pts
                self.open_path = not sh.get("closed", True)
                self.cx, self.cy = float(pts[:, 0].mean()), float(pts[:, 1].mean())
                self.size_graded = False
            self.table, self.plen = self._path_table(xy, closed=not self.open_path)
            self.p_s0 = (np.arange(n) / max(n - 1, 1)) if self.open_path else (np.arange(n) / n)
            self.ord = np.arange(n)
            if self.size_graded and self.size_hi > self.size_lo:
                grade = self.p_s0
                self.sizes = np.array([_step(self.size_lo + (self.size_hi - self.size_lo) * g) for g in grade])
        big = spec.get("big")
        if big:
            nb = max(1, int(round(n * big["ratio"])))
            who = r.permutation(n)[:nb]
            bsteps = np.array(_steps_in(*big["size_px"]))
            self.sizes = self.sizes.copy()
            self.sizes[who] = bsteps[r.randint(0, len(bsteps), nb)]
            self.big_px = int(self.sizes.max())
        self.opa = self.op_lo + r.rand(n) * (self.op_hi - self.op_lo)
        self.rank = r.permutation(n).astype(np.float64)
        # 出発点 Q
        frm = spec.get("from", "scatter")
        if frm == "scatter":
            self.q = np.stack([r.rand(n) * W, r.rand(n) * H], axis=1)
        elif frm == "line":
            self.q = np.stack([self.anchor[0] + (r.rand(n) - 0.5) * 30, self.anchor[1] + (r.rand(n) - 0.5) * 30], axis=1)
        else:
            side = r.randint(0, 4, n)
            u = r.rand(n)
            qx = np.where(side == 0, u * W, np.where(side == 1, W + 80, np.where(side == 2, u * W, -80.0)))
            qy = np.where(side == 0, -80.0, np.where(side == 1, u * H, np.where(side == 2, H + 80.0, u * H)))
            self.q = np.stack([qx, qy], axis=1)
        T_g = self.gather
        span = self.draw_on * T_g
        self.travel = max(T_g - span, 0.1)
        self.delay = self.ord / max(n - 1, 1) * span
        self.arrive = self.delay + self.travel
        self.d_dir = r.rand(n) * 2 * math.pi
        self.d_dist = 180 + 150 * r.rand(n)
        self.w_a = (0.5 + r.rand(n)) * self.wob
        self.w_p = 2.0 + 1.5 * r.rand(n)
        self.w_p1, self.w_p2 = r.rand(n) * 2 * math.pi, r.rand(n) * 2 * math.pi

    def _flow(self, u):
        """流れる位置（集まった後。u＝流れ始めてからの秒）と、道の接線の角度（度、画面の向き）"""
        if self.typ in ("disc", "concentric"):
            th = self.p_th + self.p_om * u
            x = self.cx + self.p_r * np.cos(th)
            y = self.cy + self.p_r * np.sin(th)
            ang = -(np.degrees(th) + 90.0 * np.sign(self.p_om))
            return x, y, ang, np.ones(len(x))
        s = (self.p_s0 + self.flow * u) % 1.0 if self.flow else self.p_s0.copy()
        m = len(self.table) - 1
        f = s * m
        i0 = np.clip(f.astype(int), 0, m - 1)
        fr = f - i0
        p0, p1 = self.table[i0], self.table[i0 + 1]
        x = p0[:, 0] + (p1[:, 0] - p0[:, 0]) * fr
        y = p0[:, 1] + (p1[:, 1] - p0[:, 1]) * fr
        ang = -np.degrees(np.arctan2(p1[:, 1] - p0[:, 1], p1[:, 0] - p0[:, 0]))
        edge = _smooth(s / 0.04) * _smooth((1.0 - s) / 0.04) if self.open_path else np.ones(len(x))
        return x, y, ang, edge

    def _at_shape(self, t, tau):
        n = self.nmax
        vis = self._rank_vis(tau)
        fx, fy, fang, edge = self._flow(max(tau - self.gather, 0.0))
        q = (tau - self.delay) / self.travel                       # 各点の到着の進み具合
        e = 1.0 - (1.0 - np.clip(q, 0.0, 1.0)) ** 3                # 減速
        x = self.q[:, 0] + (fx - self.q[:, 0]) * e
        y = self.q[:, 1] + (fy - self.q[:, 1]) * e
        a_in = _smooth(q / 0.25)
        amp = self.w_a * _smooth((tau - self.arrive) / 0.6)
        x = x + amp * np.sin(2 * math.pi * tau / self.w_p + self.w_p1)
        y = y + amp * np.cos(2 * math.pi * tau / (self.w_p * 1.17) + self.w_p2)
        op = self.opa * vis * a_in * edge
        if self.disperse > 0:
            p = float(np.clip((t - (self.t1 - self.disperse)) / self.disperse, 0.0, 1.0))
            if p > 0:
                ux, uy = x - self.cx, y - self.cy
                nrm = np.maximum(np.hypot(ux, uy), 1.0)
                k = p * p * self.d_dist
                x = x + ux / nrm * k
                y = y + uy / nrm * k
                op = op * (1.0 - p)
        on = (q > 0) & (op > 0)
        if not on.any():
            return None
        if self.orient == "tangent":
            ang = (np.rint(fang / 15.0).astype(int) * 15) % 360
        else:
            ang = np.zeros(n, dtype=int)
        i = np.nonzero(on)[0]
        return i, x[i], y[i], self.sizes[i], op[i], ang[i]

    def _nominal_shape(self, tau):
        return int(math.ceil(self._track_n(tau) - 1e-9))

    def _nominal_max_shape(self):
        return self.nmax

    def describe(self):
        k = self.kind
        if k == "tate_line" and self.motion == "switch":
            return f"switch（同時 {self.count} 本、出ている {self.on} 秒、出る回数 {len(self.ev)}）"
        return f"最大 {self.nominal_max()} 字" + ("（本数 %d）" % self.nmax if k == "tate_line" else "")
