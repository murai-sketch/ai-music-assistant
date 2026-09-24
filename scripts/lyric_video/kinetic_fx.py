#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
kinetic_fx.py
kinetic.py に足す技法の部品と、「曲ごとにどの技法を使うか」を決めるルール。

参考作品（キネティックタイポグラフィのMV）の分析から、
2Dの静止画処理で質が出せるものだけを部品にしている。1曲に全部は使わない。
曲の性格（和か、テンポ、歌詞の密度、繰り返し、漢字の割合、囁きの有無）で
使える部品を絞り、カットの長さと強さで割り当てる。

背景の装飾（decor）:
    wall    言葉を何段も繰り返して画面を埋める（段ごとに逆向きに流れる）
    tunnel  同じ言葉が奥へ何重にも続き、手前へ迫り続ける
    rings   言葉を円周に並べた輪が回る（手前の楕円＋奥の同心円）
    tape    言葉を並べた帯が斜めに2本横切る
    radial  中心へ向かう集中線がゆっくり回る（中心に円は置かない）
    kanji   行の要の漢字1文字を巨大に、縁を墨のように崩して敷く
質感（texture）:
    grain        フィルムの汚れ・粒子・周辺減光（静かな場面）
    misregister  版ズレ（色版が少しずれて重なる。文字側で描く）
"""

import math
import re
import unicodedata

import numpy as np
from PIL import Image, ImageDraw, ImageFilter, ImageFont

VIDEO_SIZE = (1080, 1920)
FONT_HEAVY = "/System/Library/Fonts/ヒラギノ角ゴシック W9.ttc"
FONT_QUIET = "/System/Library/Fonts/ヒラギノ明朝 ProN.ttc"

WA_WORDS = ("和", "民謡", "演歌", "三味線", "尺八", "太鼓", "祭", "wa-", "wa metal", "wametal", "japanese folk", "enka")
POP_WORDS = ("pop", "city", "neon", "electro", "エレクトロ", "シティ", "ポップ", "future bass", "hyperpop")
NIGHT_WORDS = ("夜", "光", "星", "月", "ネオン", "街")
KIDS_WORDS = ("kids", "nursery", "童謡", "わらべ", "こども", "子ども", "キッズ", "toddler", "子守", "children")
HEART_WORDS = ("心", "胸", "鼓", "脈", "息", "命")
BREAK_WORDS = ("壊", "裂", "砕", "破", "斬", "切")


def _is_kanji(ch):
    return "一" <= ch <= "鿿" or ch in "々〆"


def _is_kana(ch):
    return "぀" <= ch <= "ヿ"


def _clean(text):
    return "".join(ch for ch in unicodedata.normalize("NFKC", text) if not ch.isspace())


def _hash01(n):
    x = math.sin(n * 12.9898 + 78.233) * 43758.5453
    return x - math.floor(x)


def _hex(c):
    c = c.lstrip("#")
    return tuple(int(c[i:i + 2], 16) for i in (0, 2, 4))


# ---------------------------------------------------------------------------
# 曲の性格と、技法の割り当て

def song_profile(alignment, sections, beats, meta=None):
    meta = meta or {}
    label = " ".join(str(meta.get(k, "")) for k in ("genre", "tags", "title")).lower()
    lines = [r["line"] for r in alignment]
    text = "".join(_clean(l) for l in lines)
    counts = {}
    for l in lines:
        counts[l] = counts.get(l, 0) + 1
    sung = sum(max(min(r["end"], r["start"] + 3.0) - r["start"], 0.1) for r in alignment) or 1.0
    bpm = meta.get("bpm")
    if not bpm and beats and len(beats) > 8:
        gaps = sorted(b - a for a, b in zip(beats, beats[1:]) if b - a > 0.2)
        if gaps:
            med = gaps[len(gaps) // 2]
            bpm = 60 / med
            while bpm < 80:
                bpm *= 2
            while bpm > 180:
                bpm /= 2
    kids = any(w in label for w in KIDS_WORDS)
    wa = any(w in label for w in WA_WORDS) and not kids
    pop = any(w in label for w in POP_WORDS) or sum(text.count(w) for w in NIGHT_WORDS) >= 3
    bpm = float(bpm or 120)
    return {
        "wa": wa,
        "kids": kids,
        "pop": pop,
        "bpm": bpm,
        # コマ打ち: 文字の動きを1秒あたり何コマに落とすか（0 = 毎フレーム滑らか）。
        # 文字PVらしい「カクッ」とした動き。和風と速いポップに入れ、子ども向けとしっとりは滑らかなまま
        "koma": 12 if (wa or (pop and bpm >= 120)) and not kids else 0,
        "density": len(text) / sung,
        "repeat_ratio": sum(c for c in counts.values() if c >= 2) / max(len(lines), 1),
        "kanji_ratio": sum(_is_kanji(c) for c in text) / max(len(text), 1),
        "has_quiet": any("囁" in (s or "") for s in sections),
    }


def enabled_techniques(profile):
    on = set()
    if profile["repeat_ratio"] >= 0.08:
        on |= {"wall", "tunnel"}
    if profile["kanji_ratio"] >= 0.25:
        on |= {"kanji", "emphasis"}
    if profile["bpm"] >= 128:
        on |= {"tape", "split", "fly", "shatter"}
    if profile["wa"]:
        on |= {"radial", "stamp", "misregister", "rings"}
    elif profile["pop"]:
        on |= {"neon", "rings"}
    if profile["bpm"] < 100 or profile["density"] < 2.5:
        on |= {"scatter", "grid", "grain"}
    if profile["has_quiet"]:
        on |= {"grain", "scatter", "heartbeat"}
    if not on & {"wall", "tunnel", "rings", "tape", "radial", "kanji"}:
        on |= {"wall", "radial"}
    # 保持（行が止まっている間の小さな動き）と、間のある退場
    on |= {"breathe", "drift"}
    if profile["bpm"] >= 128:
        on |= {"jitter"}
    if profile["bpm"] >= 110:
        on |= {"fall"}
    if not profile["wa"] and profile["bpm"] >= 110:
        on |= {"long_shadow"}
    if profile.get("kids"):
        # 幼児と親向け: 攻撃的な部品を外し、弾む・きらめく部品にする
        on -= {"tape", "split", "shatter", "stamp", "misregister", "grain", "kanji",
               "neon", "wall", "tunnel", "radial", "jitter", "fall", "long_shadow"}
        on |= {"rings", "scatter", "heartbeat", "sparkle", "bounce", "wave"}
    return on


def assign_techniques(plan, profile):
    """plan の各カットに decor / texture / exit / hold / emphasis を割り当てる。
    必要に応じて motion / entrance / layout も置き換える。"""
    on = enabled_techniques(profile)
    hook_order = [d for d in ("tunnel", "wall", "rings", "radial", "tape", "sparkle") if d in on]
    verse_order = [d for d in ("tape", "kanji", "wall", "radial", "sparkle") if d in on]
    kids = profile.get("kids")
    prev_decor = None
    hook_k = verse_k = 0
    for i, c in enumerate(plan):
        text = c["text"]
        clean = _clean(text)
        dur = c["end"] - c["start"]
        nxt = plan[i + 1] if i + 1 < len(plan) else None
        c.update({"decor": None, "texture": None, "exit": None, "hold": None, "emphasis": False})
        has_kanji = any(_is_kanji(ch) for ch in clean)
        mixed = has_kanji and any(_is_kana(ch) for ch in clean)
        repeated = sum(1 for p in plan if p["text"] == text) >= 2

        if c["level"] == 3:
            order = list(hook_order)
            if not repeated and "tunnel" in order:
                order.remove("tunnel")
                order.append("tunnel")
            if order and dur >= 0.6:
                d = order[hook_k % len(order)]
                if d == prev_decor and len(order) > 1:
                    d = order[(hook_k + 1) % len(order)]
                c["decor"] = d
                hook_k += 1
            if "stamp" in on and c["entrance"] == "slam" and len(clean) <= 6 and i % 2 == 0:
                c["motion"] = c["entrance"] = "stamp"
            if "misregister" in on and c["bg"] in (2, 3) and c["decor"] != "tunnel":
                c["texture"] = "misregister"
            if "split" in on and any(w in text for w in BREAK_WORDS):
                c["exit"] = "split"
            elif ("shatter" in on and nxt is not None and nxt["section"] != c["section"]
                    and hook_k % 2 == 1):
                # サビの締め。文字が砕けて飛び、次の区分へ渡す
                c["exit"] = "shatter"
            elif "fly" in on and nxt is not None and nxt["section"] != c["section"]:
                c["exit"] = "fly"
        elif c["level"] == 1:
            if "kanji" in on and has_kanji and dur >= 1.5 and i % 2 == 0:
                c["decor"] = "kanji"
            c["texture"] = "grain" if "grain" in on else None
            if "scatter" in on and len(clean) <= 6 and len(c["rows"]) == 1 and c["layout"] != "vertical" and i % 2 == 0:
                c["motion"] = c["entrance"] = "scatter"
            if "heartbeat" in on and (len(clean) <= 4 or any(w in text for w in HEART_WORDS)):
                c["hold"] = "heartbeat"
            if "neon" in on and c["motion"] != "scatter":
                c["motion"] = c["entrance"] = "neon"
        else:
            if dur >= 1.2 and verse_order and verse_k % 2 == 0:
                if "kanji" in verse_order and has_kanji and dur >= 1.8 and not (verse_k // 2) % 4:
                    d = "kanji"
                elif repeated and "wall" in verse_order:
                    d = "wall"
                else:
                    rest = [x for x in verse_order if x != "kanji"] or verse_order
                    d = rest[(verse_k // 2) % len(rest)]
                if d != prev_decor:
                    c["decor"] = d
            verse_k += 1
            if "grid" in on and len(clean) == 4 and len(c["rows"]) == 1 and c["layout"] != "vertical":
                c["layout"] = "grid"
                c["motion"] = c["entrance"] = "pop"
            elif ("scatter" in on and len(clean) <= 6 and len(c["rows"]) == 1 and verse_k % 3 == 0
                  and c["layout"] != "vertical"):
                c["motion"] = c["entrance"] = "scatter"
            if "emphasis" in on and mixed and len(clean) >= 5 and c["layout"] not in ("vertical", "grid"):
                c["emphasis"] = True
            if "misregister" in on and verse_k % 3 == 1 and c["bg"] != "image":
                c["texture"] = "misregister"
            if "heartbeat" in on and any(w in text for w in HEART_WORDS):
                c["hold"] = "heartbeat"
            if nxt is not None and nxt["level"] == 3 and "fly" in on:
                c["exit"] = "fly"
            elif "split" in on and any(w in text for w in BREAK_WORDS):
                c["exit"] = "split"
        if kids and c["entrance"] in ("slam", "slash", "shake", "fall", "stamp"):
            c["motion"] = c["entrance"] = "bounce"
        if kids and c["hold"] is None and c["level"] == 3:
            c["hold"] = "heartbeat"

        # 保持: 1.2秒以上止まる行にだけ、小さな動きを1つ。控えめに、全部の行には付けない
        if c["hold"] is None and dur >= 1.2:
            if kids and "wave" in on and c["level"] <= 2 and i % 2 == 0:
                c["hold"] = "wave"
            elif c["level"] == 1 and "breathe" in on and dur >= 1.5:
                c["hold"] = "breathe"
            elif c["level"] == 2 and "jitter" in on and verse_k % 4 == 3:
                c["hold"] = "jitter"
        # 間のある退場: 次の行までに 0.3 秒以上の空きがあり、行自体が 1 秒以上あるとき。
        # 空きが無いと次の行の入りとぶつかる
        gap = (nxt["start"] - c["end"]) if nxt is not None else 9.0
        if c["exit"] is None and dur >= 1.0 and gap >= 0.3:
            if c["level"] == 1 or kids:
                if "drift" in on:
                    c["exit"] = "drift"
            elif "fall" in on and (nxt is None or nxt["section"] != c["section"] or gap >= 0.8):
                c["exit"] = "fall"
        # 長い影: サビの単色背景に、版ズレと交互に
        if (c["texture"] is None and "long_shadow" in on and c["level"] == 3
                and c["bg"] != "image" and dur >= 0.8 and hook_k % 2 == 1):
            c["texture"] = "long_shadow"
        c["koma"] = profile.get("koma", 0)

        if dur < 0.9 and c["decor"] not in (None, "radial"):
            c["decor"] = None
        prev_decor = c["decor"]
    return plan


def key_kanji(text):
    """行の要になる漢字1文字（意味の強い字 → 最後の漢字）。"""
    for words in (BREAK_WORDS, HEART_WORDS):
        for ch in text:
            if ch in words:
                return ch
    kanji = [ch for ch in text if _is_kanji(ch)]
    return kanji[-1] if kanji else None


# ---------------------------------------------------------------------------
# 背景の装飾

class Decor:
    def __init__(self, fonts):
        self.fonts = fonts
        self._cache = {}

    def _get(self, key, build):
        im = self._cache.get(key)
        if im is None:
            if len(self._cache) > 400:
                self._cache.clear()
            im = build()
            self._cache[key] = im
        return im

    def draw(self, frame, cut, t, color, accent, cam):
        name = cut.get("decor")
        if not name:
            return
        tl = t - cut["start"]
        dur = max(cut["end"] - cut["start"], 0.1)
        if tl < 0 or tl > dur:
            return
        fade = min(tl / 0.12, (dur - tl) / 0.1, 1.0)
        if fade <= 0:
            return
        getattr(self, "_" + name)(frame, cut, tl, dur, color, accent, cam, fade)

    # --- 文字の壁
    def _strip(self, text, size, color, alpha):
        def build():
            font = self.fonts.get(FONT_HEAVY, size)
            unit = text + "　"
            unit_w = int(font.getlength(unit))
            reps = max(int(2400 / max(unit_w, 1)) + 2, 3)
            im = Image.new("RGBA", (unit_w * reps, int(size * 1.15)), (0, 0, 0, 0))
            ImageDraw.Draw(im).text((0, 0), unit * reps, font=font, fill=color + (int(255 * alpha),))
            return im, unit_w
        return self._get(("strip", text, size, color, alpha), build)

    def _wall(self, frame, cut, tl, dur, color, accent, cam, fade):
        text = _clean(cut["text"])
        size = 150 if len(text) <= 6 else 110
        alpha = 0.2 * fade
        strip, unit_w = self._strip(text, size, color, round(alpha, 2))
        row_h = int(size * 1.15)
        vertical = cut["index"] % 3 == 0
        if vertical:
            strip = self._get(("stripv", text, size, color, round(alpha, 2)), lambda: strip.rotate(-90, expand=True))
            n = VIDEO_SIZE[0] // row_h + 1
            for k in range(n):
                speed = (160 + 40 * (k % 3)) * (1 if k % 2 else -1)
                off = int((tl * speed) % unit_w)
                frame.paste(strip, (k * row_h, -unit_w + off), strip)
            return
        n = VIDEO_SIZE[1] // row_h + 1
        for k in range(n):
            speed = (160 + 40 * (k % 3)) * (1 if k % 2 else -1)
            off = int((tl * speed) % unit_w)
            frame.paste(strip, (-unit_w + off - int(cam[1] * 80), k * row_h), strip)

    # --- 無限トンネル
    def _tunnel(self, frame, cut, tl, dur, color, accent, cam, fade):
        text = _clean(cut["text"])

        def build():
            font = self.fonts.get(FONT_HEAVY, 220)
            l, t, r, b = font.getbbox(text)
            im = Image.new("RGBA", (r - l + 20, b - t + 20), (0, 0, 0, 0))
            ImageDraw.Draw(im).text((10 - l, 10 - t), text, font=font, fill=color + (255,))
            return im
        base = self._get(("tunnel", text, color), build)
        cx, cy = VIDEO_SIZE[0] / 2, VIDEO_SIZE[1] * 0.5
        phase = (tl * 1.1) % 1.0
        for k in range(7):
            s = 0.12 * (1.6 ** (k + phase))
            if s > 3.2:
                continue
            a = min(s / 0.6, 1.0) * (0.35 if s < 0.9 else max(0.0, 0.12 * (1 - (s - 0.9) / 2.3))) * fade
            if a < 0.03:
                continue
            w, h = int(base.width * s), int(base.height * s)
            if w < 4 or h < 4:
                continue
            im = base.resize((w, h), Image.BILINEAR)
            im.putalpha(im.getchannel("A").point(lambda v, a=a: int(v * a)))
            frame.paste(im, (int(cx - w / 2), int(cy - h / 2 + (k - 3) * 6)), im)

    # --- 回る輪
    def _ring_image(self, text, diameter, size, color):
        def build():
            font = self.fonts.get(FONT_HEAVY, size)
            unit = text + "・"
            circ = math.pi * (diameter / 2 - size)
            reps = max(int(circ / max(font.getlength(unit), 1)), 1)
            chars = list(unit * reps)
            im = Image.new("RGBA", (diameter, diameter), (0, 0, 0, 0))
            r = diameter / 2 - size * 0.8
            for j, ch in enumerate(chars):
                ang = j / len(chars) * 2 * math.pi
                g = Image.new("RGBA", (size + 8, size + 8), (0, 0, 0, 0))
                ImageDraw.Draw(g).text((4, 0), ch, font=font, fill=color + (255,))
                g = g.rotate(-math.degrees(ang) - 90, resample=Image.BILINEAR, expand=True)
                x = diameter / 2 + r * math.cos(ang) - g.width / 2
                y = diameter / 2 + r * math.sin(ang) - g.height / 2
                im.alpha_composite(g, (int(x), int(y)))
            return im
        return self._get(("ring", text, diameter, size, color), build)

    def _rings(self, frame, cut, tl, dur, color, accent, cam, fade):
        text = _clean(cut["text"])
        cx, cy = VIDEO_SIZE[0] / 2, VIDEO_SIZE[1] * 0.5
        back = self._ring_image(text, 1200, 96, color)
        rot = back.rotate(-tl * 35, resample=Image.BILINEAR)
        a = 0.32 * fade
        rot.putalpha(rot.getchannel("A").point(lambda v: int(v * a)))
        frame.paste(rot, (int(cx - 600), int(cy - 600)), rot)
        front = self._ring_image(text, 1060, 100, accent)
        rot = front.rotate(tl * 60, resample=Image.BILINEAR)
        rot = rot.resize((1060, 360), Image.BILINEAR)
        rot.putalpha(rot.getchannel("A").point(lambda v: int(v * fade)))
        frame.paste(rot, (int(cx - 530), int(cy + 250)), rot)

    # --- 斜めの帯
    def _tape(self, frame, cut, tl, dur, color, accent, cam, fade):
        text = _clean(cut["text"])
        for k, (ang, band, fg, y) in enumerate(((13, accent, (12, 12, 15), 0.3), (-9, (242, 194, 0), (12, 12, 15), 0.72))):
            def build(band=band, fg=fg, ang=ang):
                font = self.fonts.get(FONT_HEAVY, 72)
                unit = text + "  ／  "
                unit_w = int(font.getlength(unit))
                reps = int(3600 / max(unit_w, 1)) + 2
                im = Image.new("RGBA", (unit_w * reps, 110), band + (235,))
                ImageDraw.Draw(im).text((0, 14), unit * reps, font=font, fill=fg + (255,))
                return im.rotate(ang, resample=Image.BILINEAR, expand=True), unit_w
            strip, unit_w = self._get(("tape", text, k, band), build)
            direction = 1 if k == 0 else -1
            shift = (tl * 260 * direction) % unit_w - unit_w
            rad = math.radians(ang)
            dx = shift * math.cos(rad)
            dy = -shift * math.sin(rad)
            enter = min(tl / 0.15, 1.0)
            x = int(VIDEO_SIZE[0] / 2 - strip.width / 2 + dx + (1 - enter) * 900 * direction)
            yy = int(VIDEO_SIZE[1] * y - strip.height / 2 + dy)
            if fade < 1:
                s2 = strip.copy()
                s2.putalpha(s2.getchannel("A").point(lambda v: int(v * fade)))
                frame.paste(s2, (x, yy), s2)
            else:
                frame.paste(strip, (x, yy), strip)

    # --- 集中線
    def _radial(self, frame, cut, tl, dur, color, accent, cam, fade):
        W, H = VIDEO_SIZE
        cx, cy = W / 2, H * 0.5
        layer = Image.new("RGBA", VIDEO_SIZE, (0, 0, 0, 0))
        d = ImageDraw.Draw(layer)
        n = 28
        rot = tl * 0.25
        R = 2400
        for k in range(n):
            a0 = rot + k / n * 2 * math.pi
            width = 0.035 + 0.02 * _hash01(k + cut["index"])
            pts = [(cx + 180 * math.cos(a0), cy + 180 * math.sin(a0)),
                   (cx + R * math.cos(a0 - width), cy + R * math.sin(a0 - width)),
                   (cx + R * math.cos(a0 + width), cy + R * math.sin(a0 + width))]
            col = color if k % 2 else accent
            d.polygon(pts, fill=col + (int(60 * fade),))
        frame.paste(layer, (0, 0), layer)

    # --- きらめく星
    def _sparkle(self, frame, cut, tl, dur, color, accent, cam, fade):
        layer = Image.new("RGBA", VIDEO_SIZE, (0, 0, 0, 0))
        d = ImageDraw.Draw(layer)
        W, H = VIDEO_SIZE
        for k in range(26):
            x = _hash01(cut["index"] * 7 + k) * W
            y = _hash01(cut["index"] * 13 + k * 3) * H
            if abs(y - H * 0.5) < 260:
                y += 420 if y > H * 0.5 else -420
            size = 18 + 46 * _hash01(k * 5 + cut["index"])
            tw = 0.5 + 0.5 * math.sin(tl * (3 + k % 4) + k)
            r = size * (0.6 + 0.4 * tw)
            pts = []
            for j in range(10):
                ang = -math.pi / 2 + j * math.pi / 5 + tl * 0.5 * (1 if k % 2 else -1)
                rr = r if j % 2 == 0 else r * 0.45
                pts.append((x + rr * math.cos(ang), y + rr * math.sin(ang)))
            col = accent if k % 3 else (255, 255, 255)
            d.polygon(pts, fill=col + (int(200 * fade * (0.4 + 0.6 * tw)),))
        frame.paste(layer, (0, 0), layer)

    # --- 巨大な漢字1文字
    def _kanji(self, frame, cut, tl, dur, color, accent, cam, fade):
        ch = key_kanji(cut["text"])
        if not ch:
            return

        def build():
            size = 1300
            font = self.fonts.get(FONT_QUIET, size)
            l, t, r, b = font.getbbox(ch)
            im = Image.new("L", (r - l + 80, b - t + 80), 0)
            ImageDraw.Draw(im).text((40 - l, 40 - t), ch, font=font, fill=255)
            # 縁を墨のように崩す: ぼかした縁にノイズを掛けて二値化
            blur = im.filter(ImageFilter.GaussianBlur(10))
            rng = np.random.default_rng(ord(ch))
            noise = rng.integers(0, 110, size=(im.height, im.width), dtype=np.int16)
            arr = np.asarray(blur, dtype=np.int16) + noise - 60
            mask = Image.fromarray(np.where(arr > 128, 255, 0).astype(np.uint8)).filter(ImageFilter.MedianFilter(5))
            return mask
        mask = self._get(("kanji", ch), build)
        s = 1.0 + 0.08 * (tl / dur)
        w, h = int(mask.width * s), int(mask.height * s)
        m = mask.resize((w, h), Image.BILINEAR).point(lambda v: int(v * 0.22 * fade))
        side = 1 if cut["index"] % 2 else -1
        x = int(VIDEO_SIZE[0] / 2 - w / 2 + side * 180 - cam[1] * 120)
        y = int(VIDEO_SIZE[1] * 0.5 - h / 2 - cam[2] * 160)
        fill = Image.new("RGB", (w, h), accent)
        frame.paste(fill, (x, y), m)


# ---------------------------------------------------------------------------
# 質感

_GRAIN = {}


def apply_grain(frame, t, strength):
    if strength <= 0.01:
        return frame
    W, H = VIDEO_SIZE
    if "vignette" not in _GRAIN:
        yy, xx = np.mgrid[0:H, 0:W].astype(np.float32)
        d = np.sqrt(((xx - W / 2) / (W / 2)) ** 2 + ((yy - H / 2) / (H / 2)) ** 2)
        _GRAIN["vignette"] = np.clip(1.0 - 0.45 * np.clip(d - 0.55, 0, 1) ** 1.3, 0, 1)[:, :, None]
        rng = np.random.default_rng(7)
        specks = []
        for _k in range(4):
            layer = np.zeros((H, W), dtype=np.int16)
            ys = rng.integers(0, H, 900)
            xs = rng.integers(0, W, 900)
            layer[ys, xs] = rng.integers(60, 160, 900)
            for _s in range(6):
                y0, x0 = rng.integers(0, H - 400), rng.integers(0, W - 4)
                layer[y0:y0 + rng.integers(120, 400), x0:x0 + 2] = 70
            specks.append(Image.fromarray(np.clip(layer, 0, 255).astype(np.uint8)).filter(ImageFilter.MaxFilter(3)))
        _GRAIN["specks"] = [np.asarray(s, dtype=np.int16)[:, :, None] for s in specks]
    arr = np.asarray(frame).astype(np.float32)
    vig = _GRAIN["vignette"]
    arr = arr * (1 - strength + strength * vig)
    speck = _GRAIN["specks"][int(t * 12) % len(_GRAIN["specks"])]
    arr = arr + speck * (0.55 * strength)
    return Image.fromarray(np.clip(arr, 0, 255).astype(np.uint8))
