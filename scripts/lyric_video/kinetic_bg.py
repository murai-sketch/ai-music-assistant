#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
kinetic_bg.py
背景の見せ方（kinetic.py から使う）。単色の背景を平板にしないための部品と、
背景の切り替え方。参考：CL側 clients/ERPJ/参考/キネティック参考分析_20260917b.md

bgfx（ショットごとの背景処理）:
    pattern:<種類>  2色の動く模様。種類 = stripes / dots / rings / checker / waves /
                    halftone / seigaiha（青海波。和の曲向け）
    duotone         画像背景を2色で刷り直す
    （無指定）       単色 or 画像のまま。単色には常に紙の質感を薄く乗せる
under（文字の下に敷くもの）:
    brush           刷毛の帯（和なら墨色、それ以外は差し色）。行の頭から塗られていく
    card            文字の下の板（模様が強いときに読みやすくする）
背景の素材（複数の画像・動画）:
    _work/<音源ハッシュ>/backgrounds.json = {"items": [{"file": パス, "use": 用途}, ...]}
    用途 = quiet（囁き）/ verse / hook（サビ）/ interlude（間奏）/ any
    画像背景のショットに、強さに合う用途の素材を順番に割り当てる（bg_image）。
    無い用途は any → 起動時の --image の順で代用する。
wipe（背景の切り替え方）:
    straight / torn（破れた紙の縁）/ circle（円で広がる）
"""

import math

import numpy as np
from PIL import Image, ImageDraw, ImageFilter

VIDEO_SIZE = (1080, 1920)
HALF = (VIDEO_SIZE[0] // 2, VIDEO_SIZE[1] // 2)

PATTERNS_WA = ["seigaiha", "checker", "stripes", "halftone"]
PATTERNS_POP = ["dots", "waves", "rings", "stripes"]
PATTERNS_DEFAULT = ["stripes", "dots", "rings", "halftone", "waves"]
BUSY_DECOR = ("wall", "tunnel", "rings", "radial", "tape")


def _hex(c):
    if isinstance(c, tuple):
        return c
    c = c.lstrip("#")
    return tuple(int(c[i:i + 2], 16) for i in (0, 2, 4))


def _mix(a, b, t):
    return tuple(int(a[i] + (b[i] - a[i]) * t) for i in range(3))


# ---------------------------------------------------------------------------
# 割り当て

def assign_backgrounds(plan, profile):
    """ショットごとに bgfx、カットごとに under / card、背景の切り替え方 wipe を決める。"""
    if profile.get("kids"):
        patterns, wipe_kinds = ["dots", "waves", "rings", "checker"], ["circle", "circle", "straight"]
    elif profile.get("wa"):
        patterns, wipe_kinds = PATTERNS_WA, ["torn", "straight"]
    elif profile.get("pop"):
        patterns, wipe_kinds = PATTERNS_POP, ["circle", "straight"]
    else:
        patterns, wipe_kinds = PATTERNS_DEFAULT, ["straight", "circle", "torn"]
    shot_fx = {}
    image_shots = 0
    solid_shots = 0
    for c in plan:
        sh = c.get("shot", c["index"])
        if sh not in shot_fx:
            if c["bg"] == "image":
                image_shots += 1
                kids = profile.get("kids")
                shot_fx[sh] = "duotone" if image_shots % 3 == 0 and c["level"] >= 2 and not kids else None
            else:
                solid_shots += 1
                shot_fx[sh] = "pattern:" + patterns[solid_shots % len(patterns)] if solid_shots % 2 == 0 or c["level"] == 3 else None
        c["bgfx"] = shot_fx[sh]
        if c.get("decor") in BUSY_DECOR and (c["bgfx"] or "").startswith("pattern"):
            c["bgfx"] = None
        c["wipe"] = wipe_kinds[c["index"] % len(wipe_kinds)]
        c["under"] = None
        strong = c["level"] == 3
        if (c["bgfx"] or "").startswith("pattern") and strong and c.get("decor") is None:
            c["under"] = "card"
        elif (c["level"] == 2 and len(c["rows"]) == 1 and not c.get("decor")
              and c["layout"] in ("center", "left", "right") and c["index"] % 4 == 0
              and c.get("entrance") not in ("scatter", "pop")):
            c["under"] = "brush"
    return plan


# ---------------------------------------------------------------------------
# 模様

_GRID = {}


def _grid():
    if "xy" not in _GRID:
        yy, xx = np.mgrid[0:HALF[1], 0:HALF[0]].astype(np.float32)
        _GRID["xy"] = (xx * 2, yy * 2)
    return _GRID["xy"]


def pattern_mask(kind, t, beat_amt, seed):
    """0/1 の模様（半分の解像度）。"""
    x, y = _grid()
    cx, cy = VIDEO_SIZE[0] / 2, VIDEO_SIZE[1] / 2
    if kind == "stripes":
        ang = math.radians(35 if seed % 2 else -35)
        u = x * math.cos(ang) + y * math.sin(ang) + t * 90
        return (np.mod(u, 90) < 34)
    if kind == "dots":
        ang = math.radians(15)
        u = x * math.cos(ang) + y * math.sin(ang) + t * 40
        v = -x * math.sin(ang) + y * math.cos(ang)
        cell = 70
        du = np.mod(u, cell) - cell / 2
        dv = np.mod(v, cell) - cell / 2
        r = 13 + 9 * beat_amt
        return du * du + dv * dv < r * r
    if kind == "rings":
        d = np.sqrt((x - cx) ** 2 + (y - cy) ** 2)
        return np.mod(d - t * 120, 80) < 26
    if kind == "checker":
        s = 120
        u = x + t * 40
        v = y + t * 40
        return (np.floor(u / s) + np.floor(v / s)) % 2 == 0
    if kind == "waves":
        u = y + 40 * np.sin(x / 90 + t * 2.2)
        return np.mod(u + t * 60, 70) < 20
    if kind == "halftone":
        cell = 56
        u = x + t * 30
        v = y
        du = np.mod(u, cell) - cell / 2
        dv = np.mod(v, cell) - cell / 2
        grad = np.clip((y / VIDEO_SIZE[1]) * 1.2 - 0.1 + 0.08 * math.sin(t), 0, 1)
        r = grad * cell * 0.55 * (1 + 0.15 * beat_amt)
        return du * du + dv * dv < r * r
    if kind == "seigaiha":
        R = 80.0
        w, h = 2 * R, R / 2
        v = y - t * 30
        row = np.floor(v / h)
        best = None
        # 手前の段（下の段）が上に重なる
        for dr in (0, 1, 2):
            j = row + dr
            off = np.where(np.mod(j, 2) == 0, 0.0, R)
            i = np.floor((x - off) / w + 0.5)
            ccx = i * w + off
            ccy = j * h
            dist = np.sqrt((x - ccx) ** 2 + (v - ccy) ** 2)
            inside = (dist < R) & (v <= ccy)
            ring = np.mod(np.floor(dist / (R / 5)), 2) == 0
            if best is None:
                best = np.where(inside, ring, False)
                covered = inside
            else:
                best = np.where(inside, ring, best)
                covered = covered | inside
        return best
    return None


def apply_bgfx(frame, bgfx, t, bg_color, accent, beat_amt, seed, strength=1.0):
    if not bgfx or strength <= 0.01:
        return frame
    bg = np.array(_hex(bg_color), dtype=np.float32)
    ac = np.array(_hex(accent), dtype=np.float32)
    if bgfx.startswith("pattern:"):
        kind = bgfx.split(":", 1)[1]
        mask = pattern_mask(kind, t, beat_amt, seed)
        if mask is None:
            return frame
        tone = ac if abs(ac.mean() - bg.mean()) > 40 else (255 - bg)
        col = bg + (tone - bg) * (0.22 * strength)
        small = np.where(mask[:, :, None], col, bg).astype(np.uint8)
        pat = Image.fromarray(small).resize(VIDEO_SIZE, Image.NEAREST)
        return pat
    if bgfx == "duotone":
        arr = np.asarray(frame).astype(np.float32)
        lum = (arr[:, :, 0] * 0.299 + arr[:, :, 1] * 0.587 + arr[:, :, 2] * 0.114) / 255.0
        lum = np.clip(lum * 1.6, 0, 1)[:, :, None]
        dark = np.array((12, 12, 15), dtype=np.float32)
        tone = dark + (ac - dark) * lum
        out = arr * (1 - 0.85 * strength) + tone * (0.85 * strength)
        return Image.fromarray(np.clip(out, 0, 255).astype(np.uint8))
    return frame


_PAPER = {}


def paper_texture(wa):
    key = "wa" if wa else "plain"
    if key not in _PAPER:
        rng = np.random.default_rng(3 if wa else 5)
        H, W = VIDEO_SIZE[1], VIDEO_SIZE[0]
        noise = rng.normal(0, 1, (H // 2, W // 2)).astype(np.float32)
        img = Image.fromarray(np.clip(128 + noise * 40, 0, 255).astype(np.uint8)).filter(ImageFilter.GaussianBlur(0.8))
        if wa:
            d = ImageDraw.Draw(img)
            for _k in range(900):
                x0, y0 = rng.integers(0, W // 2), rng.integers(0, H // 2)
                ang = rng.uniform(0, math.pi)
                ln = rng.integers(8, 40)
                d.line([(x0, y0), (x0 + ln * math.cos(ang), y0 + ln * math.sin(ang))],
                       fill=int(rng.integers(150, 200)), width=1)
        img = img.resize((W, H), Image.BILINEAR)
        _PAPER[key] = ((np.asarray(img).astype(np.float32) - 128) / 128.0 * (14 if wa else 10))[:, :, None].astype(np.int16)
    return _PAPER[key]


def apply_paper(frame, wa):
    """単色の背景に紙の質感（粒子＋繊維）を薄く乗せる。"""
    arr = np.asarray(frame).astype(np.int16) + paper_texture(wa)
    return Image.fromarray(np.clip(arr, 0, 255).astype(np.uint8))


# ---------------------------------------------------------------------------
# 背景の切り替え

_TORN = {}


def wipe_mask(kind, p, seed):
    """p(0→1) で新しい背景が占める範囲（L, 255=新）。"""
    W, H = VIDEO_SIZE
    if kind == "circle":
        x, y = _grid()
        r = p * math.hypot(W, H) / 2 * 1.05
        m = ((x - W / 2) ** 2 + (y - H / 2) ** 2 < r * r).astype(np.uint8) * 255
        return Image.fromarray(m).resize(VIDEO_SIZE, Image.NEAREST)
    if kind == "torn":
        if seed not in _TORN:
            rng = np.random.default_rng(seed)
            edge = np.cumsum(rng.normal(0, 6, H))
            edge = edge - np.linspace(edge[0], edge[-1], H)
            jag = rng.integers(-18, 18, H)
            _TORN[seed] = (edge + jag).astype(np.int32)
        edge = _TORN[seed]
        cut = int((W + 140) * p) - 70
        cols = np.arange(W)[None, :]
        m = (cols < (cut + edge)[:, None]).astype(np.uint8) * 255
        if seed % 2:
            m = m[:, ::-1]
        return Image.fromarray(np.ascontiguousarray(m))
    cut = int(W * p)
    m = Image.new("L", VIDEO_SIZE, 0)
    if seed % 2:
        m.paste(255, (W - cut, 0, W, H))
    else:
        m.paste(255, (0, 0, cut, H))
    return m


# ---------------------------------------------------------------------------
# 文字の下に敷くもの

def brush_stroke(w, h, color, seed):
    """刷毛の帯（RGBA）。左端から右端へ、かすれと毛の筋がある。"""
    rng = np.random.default_rng(seed)
    W, H = int(w), int(h)
    x = np.linspace(0, 1, W)
    top = (0.12 + 0.06 * np.sin(x * 7 + seed) + rng.normal(0, 0.012, W).cumsum() * 0.2)
    bot = (0.9 - 0.05 * np.sin(x * 5 + seed * 2) + rng.normal(0, 0.012, W).cumsum() * 0.2)
    ys = np.linspace(0, 1, H)[:, None]
    body = (ys > top[None, :]) & (ys < bot[None, :])
    streak = rng.random(H)[:, None] > (0.12 + 0.5 * np.clip((x[None, :] - 0.75) / 0.25, 0, 1))
    alpha = (body & streak).astype(np.float32)
    head = np.clip(x / 0.05, 0, 1)[None, :]
    alpha = alpha * head
    img = np.zeros((H, W, 4), dtype=np.uint8)
    img[:, :, :3] = _hex(color)
    img[:, :, 3] = (alpha * 235).astype(np.uint8)
    im = Image.fromarray(img).filter(ImageFilter.GaussianBlur(1.2))
    d = ImageDraw.Draw(im)
    for _k in range(10):
        cx = rng.uniform(0.85, 1.05) * W
        cy = rng.uniform(0.1, 0.9) * H
        r = rng.uniform(3, 12)
        d.ellipse([cx - r, cy - r, cx + r, cy + r], fill=_hex(color) + (220,))
    return im


def kaleidoscope(frame, t, strength):
    """画面中央の正方形を回して4方向に鏡映し、万華鏡にする（間奏用）。"""
    if strength <= 0.01:
        return frame
    W, H = VIDEO_SIZE
    s = W // 2
    src = frame.crop((W // 2 - s, H // 2 - s, W // 2 + s, H // 2 + s)).rotate(t * 25, resample=Image.BILINEAR)
    q = src.crop((0, 0, s, s))
    tile = Image.new("RGB", (2 * s, 2 * s))
    tile.paste(q, (0, 0))
    tile.paste(q.transpose(Image.FLIP_LEFT_RIGHT), (s, 0))
    tile.paste(q.transpose(Image.FLIP_TOP_BOTTOM), (0, s))
    tile.paste(q.transpose(Image.ROTATE_180), (s, s))
    out = Image.new("RGB", VIDEO_SIZE)
    for yy in range(-((H // 2 - s) % (2 * s)) - 2 * s, H, 2 * s):
        out.paste(tile, (0, yy))
    return Image.blend(frame, out, strength)


# ---------------------------------------------------------------------------
# 複数の背景素材

USES = ("quiet", "verse", "hook", "interlude", "any")


def load_backgrounds(cache_dir):
    import json
    from pathlib import Path

    path = Path(cache_dir) / "backgrounds.json"
    if not path.exists():
        return []
    items = json.loads(path.read_text(encoding="utf-8")).get("items", [])
    return [i for i in items if i.get("file") and Path(i["file"]).exists() and i.get("use", "any") in USES]


def save_backgrounds(cache_dir, items):
    import json
    from pathlib import Path

    path = Path(cache_dir) / "backgrounds.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({"items": items}, ensure_ascii=False, indent=1), encoding="utf-8")


def _pool(backgrounds, use):
    items = backgrounds or []
    pool = [i["file"] for i in items if i.get("use") == use]
    return pool or [i["file"] for i in items if i.get("use") == "any"]


def assign_bg_images(plan, backgrounds):
    """画像背景のショットごとに、強さに合う素材を順番に割り当てる。"""
    counters = {}
    shot_img = {}
    # 囁き用・サビ用の素材があれば、単色のカットの一部を画像背景にして見せる
    if _pool(backgrounds, "quiet") and any(i.get("use") == "quiet" for i in backgrounds or []):
        for c in plan:
            if c["level"] == 1:
                c["bg"] = "image"
                c["bgfx"] = None
    if any(i.get("use") == "hook" for i in backgrounds or []) and not any(c.get("profile_kids") for c in plan):
        k = 0
        for c in plan:
            if c["level"] == 3:
                if k % 3 == 0:
                    c["bg"] = "image"
                    c["bgfx"] = None
                    if c.get("under") == "card":
                        c["under"] = None
                k += 1
    for c in plan:
        c["bg_image"] = None
        if c["bg"] != "image" or not backgrounds:
            continue
        sh = c.get("shot", c["index"])
        if sh not in shot_img:
            use = {1: "quiet", 3: "hook"}.get(c["level"], "verse")
            pool = _pool(backgrounds, use)
            if pool:
                k = counters.get(use, 0)
                shot_img[sh] = pool[k % len(pool)]
                counters[use] = k + 1
            else:
                shot_img[sh] = None
        c["bg_image"] = shot_img[sh]
    return plan


def interlude_images(backgrounds, n):
    pool = _pool(backgrounds, "interlude")
    return [pool[k % len(pool)] if pool else None for k in range(n)]


def sparkle(frame, t, strength, beat_amt, palette):
    """間奏用: 背景にきらめく星を散らす（子ども向け）。"""
    layer = Image.new("RGBA", VIDEO_SIZE, (0, 0, 0, 0))
    d = ImageDraw.Draw(layer)
    W, H = VIDEO_SIZE
    rng = np.random.default_rng(11)
    cols = [_hex(palette[0]), _hex(palette[1]), (255, 255, 255)]
    for k in range(60):
        x = rng.uniform(0, W)
        y0 = rng.uniform(0, H)
        y = (y0 - t * (30 + 40 * rng.uniform())) % H
        r = rng.uniform(10, 40) * (1 + 0.3 * beat_amt)
        tw = 0.5 + 0.5 * math.sin(t * rng.uniform(2, 5) + k)
        pts = []
        for j in range(10):
            ang = -math.pi / 2 + j * math.pi / 5
            rr = r if j % 2 == 0 else r * 0.45
            pts.append((x + rr * math.cos(ang), y + rr * math.sin(ang)))
        d.polygon(pts, fill=cols[k % 3] + (int(220 * strength * (0.3 + 0.7 * tw)),))
    out = frame.convert("RGBA")
    out.alpha_composite(layer)
    return out.convert("RGB")
