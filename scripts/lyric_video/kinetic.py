#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
kinetic.py
キネティックタイポグラフィ方式の歌詞動画レンダラー。

render.py（字幕を中央下にポップインさせる方式）とは別に、
「文字そのものが動いて意味を伝える」映像を作る。取り入れている原則:

  - 1カット1メッセージ。歌詞1行＝1カットで、前の行は残さない
  - 言葉の意味と動きを一致させる（MEANING_RULES）
      例: 刃・切る→斬る / 広がる・散る→字間が開く / 隠れる・消える→消しゴム /
          落ちる→落下 / 回る・変わる→回転 / 逃げる・速い→残像つきスライド
  - 文字の大きさに階層を付け、構図（中央/左/右/縦書き/斜め/分割）を毎回変える
  - 全編を最大テンションにしない。強弱は曲ノートの構成タグから決める
      囁き→静かな動き・明朝・暗い背景 / Hook・Chorus→叩きつけ・背景色の切り替え
  - 背景に同じ言葉を巨大・薄く（不透明度7%前後）敷く
  - 叩きつけ系の着地をビートに合わせる
  - 間奏（歌詞のない区間）: 背景画像にビート連動のエフェクト（RGBずらし /
    グリッチ / 二色刷り / 走査線）をかける。明るさを激しく変える点滅は使わない
  - カメラ: 背景が変わらない一続きのカット（ショット）ごとに、背景画像を
    パン/ティルト/寄り/引き/傾きで動かす。Hookの着地では画面全体を
    パンチイン＋揺れ。背景の動きを文字より大きくして奥行きを出す
  - 点滅の安全: 白フラッシュは1〜2フレーム、0.5秒以内に連発しない。
    背景色の切り替えも0.5秒以上の間隔を空ける

描画は moviepy のクリップ合成ではなく、PILで1フレームずつ描く
（1文字単位の変形を数百クリップで合成すると遅すぎるため）。

カットごとの設計（構図・動き・背景）は plan として JSON に書き出す。
_work/<hash>/kinetic_plan.json を手で書き換えれば、その内容で描画される。
"""

import bisect
import json
import math
from pathlib import Path

import numpy as np
from PIL import Image, ImageDraw, ImageEnhance, ImageFont

VIDEO_SIZE = (1080, 1920)
FPS = 30

FONT_HEAVY = "/System/Library/Fonts/ヒラギノ角ゴシック W9.ttc"
FONT_QUIET = "/System/Library/Fonts/ヒラギノ明朝 ProN.ttc"

# (背景, 文字, 縁取り, アクセント)。文字と背景のコントラストを確保する組み合わせ
DEFAULT_PALETTE = [
    ("#0B0B0F", "#FFFFFF", "#0B0B0F", "#FF3B70"),
    ("#C1121F", "#FFFFFF", "#0B0B0F", "#0B0B0F"),
    ("#F2E8D5", "#111111", "#F2E8D5", "#C1121F"),
    ("#FF5FA2", "#111111", "#FF5FA2", "#FFFFFF"),
]

# 言葉 → 動き。先に書いたものが優先
MEANING_RULES = [
    (("刃", "斬", "切", "刑", "裂"), "slash"),
    (("消", "隠", "忘", "失"), "erase"),
    (("落", "堕", "沈", "崩"), "fall"),
    (("晒", "広", "拡", "散"), "spread"),
    (("集", "詰", "閉", "寄"), "converge"),
    (("回", "巡", "変", "転"), "rotate"),
    (("逃", "速", "早", "走", "追"), "dash"),
    (("燃", "炎", "怒", "叫"), "shake"),
    (("大", "巨", "増", "膨"), "grow"),
    (("息", "浮", "軽", "舞"), "float"),
]

VERSE_MOTIONS = ["slide_l", "stagger", "mask", "slide_r", "rotate", "stagger", "spread", "mask"]
QUIET_MOTIONS = ["mask", "float"]
HOOK_MOTIONS = ["slam", "grow", "slam", "converge"]
LAYOUTS = ["center", "left", "right", "vertical", "diagonal"]
CAMERA_MOVES = ["pan_l", "push_in", "tilt_up", "pan_r", "pull_out", "dutch", "tilt_down"]

# 入り（フレーム数）。記事の目安: 叩きつけ4〜8f / スライド5〜10f / マスク6〜12f
ENTRANCE_FRAMES = {
    "slam": 6, "slash": 6, "shake": 6, "stagger": 4, "slide_l": 7, "slide_r": 7,
    "dash": 6, "mask": 9, "spread": 12, "converge": 10, "rotate": 8, "fall": 8,
    "grow": 10, "float": 14, "erase": 9,
}
EXIT_FRAMES = 3
MIN_FLASH_GAP = 2.0
MIN_BG_SWITCH_GAP = 0.5
GAP_FOR_REST = 1.2  # これ以上の無歌詞区間は「間」として背景を画像に戻す

INTERLUDE_EFFECTS = ["rgb_split", "glitch", "duotone", "scan"]
INTERLUDE_FADE = 0.35  # 間奏の出入りでエフェクトを強める/弱める秒数
INTERLUDE_MIN = 2.5    # これより短い歌詞の切れ目は間奏として扱わない

VERTICAL_MAP = {"ー": "｜", "「": "﹁", "」": "﹂", "『": "﹃", "』": "﹄", "（": "︵", "）": "︶", "…": "︙"}


# ---------------------------------------------------------------------------
# 設計（plan）

def section_intensity(section):
    s = section or ""
    if "囁" in s:
        return 1
    if "Pre" in s:
        return 2
    if "Hook" in s or "Chorus" in s:
        return 3
    return 2


def _clean_len(text):
    return len(text.replace("　", "").replace(" ", ""))


def _pick_meaning(text):
    for keys, motion in MEANING_RULES:
        if any(k in text for k in keys):
            return motion
    return None


def _split_rows(text):
    """全角/半角スペースで区切られた行は、区切りごとに段を分ける。
    区切りがなく12文字を超える行は半分で折る。"""
    tokens = [t for t in text.replace("　", " ").split(" ") if t]
    if len(tokens) >= 2 and _clean_len(text) > 6:
        return tokens
    joined = "".join(tokens) or text
    if len(joined) > 12:
        mid = (len(joined) + 1) // 2
        return [joined[:mid], joined[mid:]]
    return [joined]


def build_plan(alignment, sections, beats, style):
    """alignment（[{line,start,end}]）と、行ごとの構成タグ名から、
    カットごとの設計を作る。"""
    max_hold = style.get("max_hold_sec", 2.8)
    counts = {}
    for item in alignment:
        counts[item["line"]] = counts.get(item["line"], 0) + 1

    plan = []
    shot = -1
    prev_bg = object()
    prev_end = -99.0
    prev_layout = None
    verse_i = 0
    hook_i = 0
    palette_i = 0
    bg_mode = "image"
    last_bg_switch = -99.0
    for i, item in enumerate(alignment):
        text = item["line"]
        section = sections[i] if i < len(sections) else ""
        level = section_intensity(section)
        if counts[text] >= 2 and level == 2 and _clean_len(text) <= 6:
            level = 3
        start = float(item["start"])
        next_start = float(alignment[i + 1]["start"]) if i + 1 < len(alignment) else float(item["end"])
        show_end = min(next_start, start + max_hold)
        rows = _split_rows(text)
        n = _clean_len(text)
        if level == 3 and len(rows) == 1 and 4 <= n <= 8:
            rows = [rows[0][:-2], rows[0][-2:]]

        meaning = _pick_meaning(text)
        if level == 1:
            motion = "erase" if meaning == "erase" else QUIET_MOTIONS[i % len(QUIET_MOTIONS)]
        elif level == 3:
            motion = meaning if meaning in ("slash", "shake", "spread", "fall", "erase") else HOOK_MOTIONS[hook_i % len(HOOK_MOTIONS)]
            hook_i += 1
        else:
            motion = meaning or VERSE_MOTIONS[verse_i % len(VERSE_MOTIONS)]
            verse_i += 1
        if motion == "erase":
            entrance = "mask"
        else:
            entrance = motion

        if level == 3 or len(rows) >= 2:
            layout = "center"
        else:
            choices = [l for l in LAYOUTS if l != prev_layout]
            if n > 7:
                choices = [l for l in choices if l != "vertical"]
            if level == 1:
                choices = [l for l in choices if l in ("center", "vertical", "left")] or ["center"]
            layout = choices[(i * 7) % len(choices)]
        prev_layout = layout

        if level == 3:
            tier = 1
        elif level == 1:
            tier = 2
        else:
            tier = 1 if n <= 8 else 2

        # 背景: Hookは毎行切り替え、Verseは4行ごと、囁きは暗色固定、長い間の後は画像に戻す
        prev_gap = start - (float(alignment[i - 1]["start"]) + max_hold) if i > 0 else 99
        want = bg_mode
        if level == 1:
            want = 0
        elif level == 3:
            palette_i = (palette_i + 1) % len(DEFAULT_PALETTE)
            want = palette_i
        elif prev_gap > GAP_FOR_REST:
            want = "image"
        elif verse_i % 4 == 0:
            want = "image" if bg_mode != "image" else (palette_i + 2) % len(DEFAULT_PALETTE)
        if want != bg_mode and start - last_bg_switch >= MIN_BG_SWITCH_GAP:
            bg_mode = want
            last_bg_switch = start

        land = start + 0.1
        if beats and entrance in ("slam", "slash", "shake", "stagger", "fall"):
            k = bisect.bisect_left(beats, land)
            near = [b for b in beats[max(k - 1, 0):k + 1] if abs(b - land) <= 0.12]
            if near:
                land = min(near, key=lambda b: abs(b - land))

        if bg_mode != prev_bg or level == 3 or start - prev_end > GAP_FOR_REST:
            shot += 1
            if level == 3:
                camera = "punch"
            elif level == 1:
                camera = "drift"
            else:
                camera = CAMERA_MOVES[shot % len(CAMERA_MOVES)]
        prev_bg = bg_mode
        prev_end = show_end

        plan.append({
            "index": i + 1,
            "shot": shot,
            "camera": camera,
            "text": text,
            "section": section,
            "level": level,
            "rows": rows,
            "start": round(start, 3),
            "end": round(show_end, 3),
            "land": round(land, 3),
            "motion": motion,
            "entrance": entrance,
            "layout": layout,
            "tier": tier,
            "bg": bg_mode,
            "flash": level == 3 and entrance in ("slam", "slash")
                     and (i == 0 or plan[-1]["level"] != 3),
        })

    last_flash = -99.0
    for cut in plan:
        if cut["flash"]:
            if cut["land"] - last_flash < MIN_FLASH_GAP:
                cut["flash"] = False
            else:
                last_flash = cut["land"]
    return plan


def plan_to_markdown(plan):
    out = ["| # | 時間 | 強さ | 構図 | 動き | 背景 | カメラ | フラッシュ |", "|---|---|---|---|---|---|---|---|"]
    for c in plan:
        bg = c["bg"] if c["bg"] == "image" else f"色{c['bg']}"
        out.append(
            f"| {c['index']} | {c['start']:.2f}–{c['end']:.2f} | {c['level']} | {c['layout']} | "
            f"{c['motion']} | {bg} | {c.get('camera', '')} | {'●' if c['flash'] else ''} |"
        )
    return "\n".join(out) + "\n"


# ---------------------------------------------------------------------------
# 描画の部品

def _hex(c):
    c = c.lstrip("#")
    return tuple(int(c[i:i + 2], 16) for i in (0, 2, 4))


def _ease_out(p):
    p = min(max(p, 0.0), 1.0)
    return 1 - (1 - p) ** 3


def _lerp(a, b, p):
    return a + (b - a) * p


class _Fonts:
    def __init__(self):
        self._cache = {}

    def get(self, path, size):
        key = (path, size)
        if key not in self._cache:
            self._cache[key] = ImageFont.truetype(path, size)
        return self._cache[key]


class _Sprites:
    """1文字の画像と、その変形版（拡大率・角度・不透明度・切り抜き）のキャッシュ。"""

    def __init__(self, fonts):
        self.fonts = fonts
        self.base = {}
        self.xform = {}

    def glyph(self, ch, font_path, size, fill, stroke, stroke_w):
        key = (ch, font_path, size, fill, stroke, stroke_w)
        g = self.base.get(key)
        if g is None:
            font = self.fonts.get(font_path, size)
            l, t, r, b = font.getbbox(ch, stroke_width=stroke_w)
            w, h = max(r - l, 1), max(b - t, 1)
            img = Image.new("RGBA", (w, h), (0, 0, 0, 0))
            ImageDraw.Draw(img).text(
                (-l, -t), ch, font=font, fill=_hex(fill) + (255,),
                stroke_width=stroke_w, stroke_fill=_hex(stroke) + (255,),
            )
            g = (img, l, t, font.getlength(ch))
            self.base[key] = g
        return g

    def transformed(self, key, img, scale, angle, alpha, crop_top, crop_right):
        sq = round(scale / 0.02) * 0.02
        aq = round(angle)
        alq = round(alpha * 10) / 10
        ctq = round(crop_top * 20) / 20
        crq = round(crop_right * 20) / 20
        k = (key, sq, aq, alq, ctq, crq)
        out = self.xform.get(k)
        if out is not None:
            return out
        if len(self.xform) > 6000:
            self.xform.clear()
        im = img
        if ctq > 0 or crq > 0:
            w, h = im.size
            box = (0, int(h * ctq), max(int(w * (1 - crq)), 0), h)
            if box[2] <= 0 or box[1] >= h:
                self.xform[k] = None
                return None
            cropped = Image.new("RGBA", im.size, (0, 0, 0, 0))
            cropped.paste(im.crop(box), (box[0], box[1]))
            im = cropped
        if abs(sq - 1.0) > 1e-6:
            w, h = im.size
            nw, nh = max(int(w * sq), 1), max(int(h * sq), 1)
            im = im.resize((nw, nh), Image.BILINEAR)
        if aq:
            im = im.rotate(aq, resample=Image.BILINEAR, expand=True)
        if alq < 1.0:
            a = im.getchannel("A").point(lambda v: int(v * alq))
            im = im.copy()
            im.putalpha(a)
        self.xform[k] = im
        return im


class _Cut:
    """1カット分の文字配置（基準サイズ・回転なしの状態）を持つ。"""

    def __init__(self, cut, sprites, colors, palette_bg):
        self.cut = cut
        text_color, stroke_color, accent = colors
        level = cut["level"]
        rows = cut["rows"]
        vertical = cut["layout"] == "vertical"
        font_path = FONT_QUIET if level == 1 else FONT_HEAVY

        def row_size(row):
            n = max(len(row), 1)
            if vertical:
                return max(min(int(1500 / n), 320 if cut["tier"] == 1 else 220), 60)
            budget = 980 if cut["layout"] in ("center", "diagonal") else 900
            if cut["tier"] == 1:
                cap = 480 if n <= 2 else 400 if n <= 4 else 300
            else:
                cap = 210
            if len(rows) >= 2:
                if level == 1:
                    cap = min(cap, 210)
                else:
                    cap = 400 if n == 1 else 340 if n <= 2 else 300 if n <= 3 else 260
            return max(min(int(budget / n), cap), 60)

        if vertical:
            sizes = [min(row_size(r) for r in rows)] * len(rows)
        else:
            sizes = [row_size(r) for r in rows]
        size = max(sizes)
        self.size = size

        # 段ごとに、最後の段（またはHookの末尾2文字）をアクセント色にする
        glyphs = []
        order = 0
        row_hs = [sz * 1.1 for sz in sizes]
        total_h = sum(row_hs)
        y_cursor = -total_h / 2
        for ri, row in enumerate(rows):
            rsize = sizes[ri]
            stroke_w = max(rsize // 24, 3) if palette_bg is None else max(rsize // 40, 2)
            row_h = rsize * 1.12
            accent_row = len(rows) >= 2 and ri == len(rows) - 1
            if vertical:
                x0 = -(len(rows) - 1) * row_h / 2 + (len(rows) - 1 - ri) * row_h - rsize / 2
                y = -len(row) * rsize * 1.02 / 2
            else:
                font = sprites.fonts.get(font_path, rsize)
                row_w = sum(font.getlength(ch) for ch in row)
                if cut["layout"] == "left":
                    x = -row_w / 2 - 60 + ri * 40
                elif cut["layout"] == "right":
                    x = -row_w / 2 + 60 - ri * 40
                else:
                    x = -row_w / 2
                y0 = y_cursor
                y_cursor += row_hs[ri]
            for ci, ch in enumerate(row):
                is_accent = accent_row or (level == 3 and len(rows) == 1 and len(row) >= 4 and ci >= len(row) - 2)
                fill = accent if is_accent else text_color
                stroke = stroke_color
                if is_accent and palette_bg is not None and _hex(accent) == _hex(stroke_color):
                    stroke = text_color
                draw_ch = VERTICAL_MAP.get(ch, ch) if vertical else ch
                g = sprites.glyph(draw_ch, font_path, rsize, fill, stroke, stroke_w)
                img, l, t, adv = g
                w, h = img.size
                if vertical:
                    cx = x0 + rsize / 2
                    cy = y + rsize * 1.02 * ci + rsize / 2
                    angle = 0
                else:
                    cx = x + l + w / 2
                    cy = y0 + t + h / 2
                    x += adv
                    angle = 0
                glyphs.append({
                    "key": (draw_ch, font_path, rsize, fill, stroke, stroke_w),
                    "img": img, "cx": cx, "cy": cy, "order": order, "row": ri, "angle": angle,
                })
                order += 1
        self.glyphs = glyphs
        self.count = max(order, 1)
        self.n_rows = len(rows)

        if cut["layout"] == "left":
            self.anchor = (VIDEO_SIZE[0] * 0.47, VIDEO_SIZE[1] * 0.45)
        elif cut["layout"] == "right":
            self.anchor = (VIDEO_SIZE[0] * 0.53, VIDEO_SIZE[1] * 0.55)
        elif cut["layout"] == "vertical":
            self.anchor = (VIDEO_SIZE[0] * (0.68 if cut["index"] % 2 else 0.32), VIDEO_SIZE[1] * 0.46)
        else:
            self.anchor = (VIDEO_SIZE[0] / 2, VIDEO_SIZE[1] * 0.5)
        self.base_angle = -8 if cut["layout"] == "diagonal" else 0

        # 背景に敷く巨大な文字
        echo_text = max(rows, key=len)
        echo_size = int(min(1500 / max(len(echo_text), 1), 900))
        echo_font = sprites.fonts.get(FONT_HEAVY, echo_size)
        l, t, r, b = echo_font.getbbox(echo_text)
        echo = Image.new("RGBA", (max(r - l, 1), max(b - t, 1)), (0, 0, 0, 0))
        echo_color = _hex(text_color) if palette_bg is not None else (255, 255, 255)
        ImageDraw.Draw(echo).text((-l, -t), echo_text, font=echo_font, fill=echo_color + (255,))
        a = echo.getchannel("A").point(lambda v: int(v * 0.08))
        echo.putalpha(a)
        self.echo = echo

    def glyph_state(self, g, tl, dur):
        """時刻tl（カット開始からの秒）での1文字の変形を返す:
        (dx, dy, scale, angle, alpha, crop_top, crop_right)"""
        cut = self.cut
        motion = cut["entrance"]
        f = tl * FPS
        E = ENTRANCE_FRAMES.get(motion, 6)
        dx = dy = 0.0
        scale = 1.0
        angle = 0.0
        alpha = 1.0
        crop_top = 0.0
        crop_right = 0.0
        o = g["order"]

        if motion in ("slam", "slash", "shake"):
            lead = (cut["land"] - cut["start"]) * FPS - 3
            p = f - lead
            if p < 0:
                alpha = 0.0
            elif p < 3:
                scale = _lerp(1.8, 0.92, p / 3)
                alpha = min(p / 2, 1.0)
            elif p < E:
                scale = _lerp(0.92, 1.0, (p - 3) / (E - 3))
            if motion == "slash":
                angle = -6
            if motion == "shake" or (3 <= p < 7):
                amp = 7 if motion == "shake" else 5 * (1 - (p - 3) / 4)
                dx += amp * math.sin(tl * 71 + o)
                dy += amp * math.cos(tl * 53 + o)
        elif motion == "stagger":
            p = f - o * 2
            if p < 0:
                alpha = 0.0
            else:
                q = _ease_out(p / E)
                scale = _lerp(1.5, 1.0, q)
                dy = _lerp(-50, 0, q)
                alpha = min(p / 2, 1.0)
        elif motion in ("slide_l", "slide_r", "dash"):
            direction = -1 if motion != "slide_r" else 1
            if motion == "dash":
                direction = -1 if cut["index"] % 2 else 1
            q = _ease_out((f - g["row"] * 2) / E)
            dx = direction * _lerp(760, 0, q)
            alpha = 1.0 if q > 0 else 0.0
        elif motion == "mask":
            # 持ち上げと切り抜きは draw() 側で行う
            alpha = 1.0 if f - o > 0 else 0.0
        elif motion == "spread":
            q = _ease_out(f / E)
            dx = g["cx"] * _lerp(-0.45, 0.12, q)
            alpha = min(f / 3, 1.0)
        elif motion == "converge":
            q = _ease_out(f / E)
            rnd = math.sin(o * 12.9898) * 43758.5453
            rx = (rnd - math.floor(rnd)) * 2 - 1
            rnd2 = math.sin(o * 78.233) * 12345.678
            ry = (rnd2 - math.floor(rnd2)) * 2 - 1
            dx = rx * 520 * (1 - q)
            dy = ry * 700 * (1 - q)
            angle = rx * 70 * (1 - q)
            alpha = min(f / 3, 1.0)
        elif motion == "rotate":
            q = _ease_out(f / E)
            angle = _lerp(-90, 0, q)
            scale = _lerp(0.6, 1.0, q)
            alpha = min(f / 2, 1.0)
        elif motion == "fall":
            lead = (cut["land"] - cut["start"]) * FPS - E
            p = f - lead - o * 1
            if p < 0:
                alpha = 0.0
            else:
                q = min(p / E, 1.0)
                dy = -1100 * (1 - q) ** 2
                if 1.0 <= p / E < 1.5:
                    dy += 18 * math.sin((p / E - 1) * 2 * math.pi)
        elif motion == "grow":
            p = f / E
            if p < 0.7:
                scale = _lerp(0.3, 1.15, p / 0.7)
            elif p < 1:
                scale = _lerp(1.15, 1.0, (p - 0.7) / 0.3)
            alpha = min(f / 2, 1.0)
        elif motion == "float":
            q = _ease_out(f / E)
            dy = _lerp(70, 0, q) + 6 * math.sin(tl * 3 + o * 0.5)
            alpha = q

        # 退場
        remain = (dur - tl) * FPS
        if cut["motion"] == "erase" and remain < 8:
            crop_right = 1 - max(remain, 0) / 8
        elif remain < EXIT_FRAMES:
            q = max(remain, 0) / EXIT_FRAMES
            alpha *= q
            scale *= _lerp(0.9, 1.0, q)
        return dx, dy, scale, angle, alpha, crop_top, crop_right

    def draw(self, frame, t, sprites, cam=(1.0, 0.0, 0.0, 0.0)):
        cut = self.cut
        tl = t - cut["start"]
        dur = cut["end"] - cut["start"]
        if tl < -0.2 or tl > dur:
            return
        # 背景の巨大文字: ゆっくり流れ、背景カメラと逆向きに大きく動く（単色背景でもカメラが感じられる）
        _zoom, px, py, _angle = cam
        ex = int(VIDEO_SIZE[0] / 2 - self.echo.size[0] / 2
                 + (40 - 80 * tl / max(dur, 0.1)) * (1 if cut["index"] % 2 else -1) - px * 260)
        ey = int(VIDEO_SIZE[1] * (0.22 if cut["index"] % 2 else 0.78) - self.echo.size[1] / 2 - py * 360)
        frame.paste(self.echo, (ex, ey), self.echo)

        ax, ay = self.anchor
        base_angle = self.base_angle
        rad = math.radians(base_angle)
        cos_a, sin_a = math.cos(rad), math.sin(rad)
        motion = cut["entrance"]
        E = ENTRANCE_FRAMES.get(motion, 6)
        f = tl * FPS

        if motion == "dash" and 0 < f < E + 2:
            ghosts = [(0.35, 3), (0.18, 6)]
        else:
            ghosts = []

        for g in self.glyphs:
            dx, dy, scale, angle, alpha, crop_top, crop_right = self.glyph_state(g, tl, dur)
            if alpha <= 0.02:
                continue
            reveal_clip = None
            if motion == "mask":
                p = f - g["order"]
                q = _ease_out(p / E) if p > 0 else 0.0
                if q <= 0:
                    continue
                reveal_clip = q
            gx = g["cx"] * scale
            gy = g["cy"] * scale
            rx = gx * cos_a - gy * sin_a
            ry = gx * sin_a + gy * cos_a
            total_angle = base_angle + angle + g["angle"]
            for ghost_alpha, back in [(1.0, 0)] + ghosts:
                if back:
                    gdx, gdy, _, _, _, _, _ = self.glyph_state(g, max(tl - back / FPS, 0), dur)
                    a = alpha * ghost_alpha
                else:
                    gdx, gdy = dx, dy
                    a = alpha
                if reveal_clip is not None:
                    # 下から持ち上がり、元の枠の下端で切れて見える
                    h = g["img"].size[1]
                    im = sprites.transformed(g["key"], g["img"], scale, total_angle, a, 0.0, crop_right)
                    if im is None:
                        continue
                    shown = int(im.size[1] * reveal_clip)
                    if shown <= 0:
                        continue
                    im = im.crop((0, 0, im.size[0], shown))
                    px = ax + rx + gdx - im.size[0] / 2
                    top = ay + ry - (h * scale) / 2
                    py = top + (h * scale) - shown
                    frame.paste(im, (int(px), int(py)), im)
                    continue
                im = sprites.transformed(g["key"], g["img"], scale, total_angle, a, crop_top, crop_right)
                if im is None:
                    continue
                px = ax + rx + gdx - im.size[0] / 2
                py = ay + ry + gdy - im.size[1] / 2
                frame.paste(im, (int(px), int(py)), im)

        # 斬る: 斜めの線が走る
        if cut["entrance"] == "slash":
            p = (tl - (cut["land"] - cut["start"])) * FPS
            if 0 <= p <= 6:
                d = ImageDraw.Draw(frame)
                q = p / 6
                x0 = -200 + q * 1480
                d.line([(x0 - 700, ay + 420), (x0, ay - 420)], fill=(255, 255, 255), width=10)


# ---------------------------------------------------------------------------
# 背景

class _Background:
    """背景画像は画面を覆う大きさ（縦長画面に横長画像なら横に余りが出る）で
    保持し、カメラの窓（寄り・パン位置・傾き）で切り出す。"""

    def __init__(self, image_path, beats, style):
        img = Image.open(image_path).convert("RGB")
        W, H = VIDEO_SIZE
        s = max(W / img.width, H / img.height)
        img = img.resize((int(img.width * s) + 1, int(img.height * s) + 1), Image.LANCZOS)
        self.cover = ImageEnhance.Brightness(img).enhance(0.55)
        self.beats = beats or []
        self.pulse_scale = style.get("pulse_scale", 1.08)
        self.pulse_decay = style.get("pulse_decay_sec", 0.14)
        self._solid = {}

    def pulse(self, t):
        if not self.beats:
            return 1.0
        idx = bisect.bisect_right(self.beats, t) - 1
        if idx < 0:
            return 1.0
        dt = t - self.beats[idx]
        if dt > self.pulse_decay * 4:
            return 1.0
        return 1.0 + (self.pulse_scale - 1.0) * math.exp(-dt / self.pulse_decay)

    def image_at(self, t, cam):
        W, H = VIDEO_SIZE
        zoom, px, py, angle = cam
        zoom *= self.pulse(t)
        th = math.radians(angle)
        # 傾けても画面外（黒）が見えない最小の寄り
        zoom = max(zoom, math.cos(th) + (W / H) * abs(math.sin(th)) + 0.01, 1.0)
        CW, CH = self.cover.size
        room_x = max((CW - W / zoom) / 2, 0)
        room_y = max((CH - H / zoom) / 2, 0)
        cx = CW / 2 + px * room_x
        cy = CH / 2 + py * room_y
        cos_t, sin_t = math.cos(th), math.sin(th)
        a, b = cos_t / zoom, -sin_t / zoom
        d, e = sin_t / zoom, cos_t / zoom
        c = cx - a * W / 2 - b * H / 2
        f = cy - d * W / 2 - e * H / 2
        return self.cover.transform(VIDEO_SIZE, Image.AFFINE, (a, b, c, d, e, f), resample=Image.BILINEAR)

    def solid(self, color):
        im = self._solid.get(color)
        if im is None:
            im = Image.new("RGB", VIDEO_SIZE, _hex(color))
            self._solid[color] = im
        return im

    def frame(self, mode, t, cam):
        if mode == "image":
            return self.image_at(t, cam)
        return self.solid(DEFAULT_PALETTE[mode][0]).copy()


def find_interludes(plan, duration=None):
    """歌詞のない区間（INTERLUDE_MIN秒以上）を [(開始, 終了, エフェクト名)] で返す。
    イントロ（最初の行まで）とアウトロ（最後の行の後）も含む。"""
    spans = []
    if plan and plan[0]["start"] > INTERLUDE_MIN:
        spans.append((0.0, plan[0]["start"]))
    for a, b in zip(plan, plan[1:]):
        if b["start"] - a["end"] > INTERLUDE_MIN:
            spans.append((a["end"], b["start"]))
    if plan and duration and duration - plan[-1]["end"] > INTERLUDE_MIN:
        spans.append((plan[-1]["end"], duration))
    return [(s, e, INTERLUDE_EFFECTS[k % len(INTERLUDE_EFFECTS)]) for k, (s, e) in enumerate(spans)]


def _hash01(n):
    x = math.sin(n * 12.9898) * 43758.5453
    return x - math.floor(x)


def apply_interlude_effect(frame, name, strength, t, beat_idx, beat_amt, palette):
    """frame(PIL RGB) に間奏エフェクトをかける。strength 0..1、beat_amt はビート直後ほど1。"""
    if strength <= 0.01:
        return frame
    arr = np.asarray(frame).astype(np.int16)
    H, W, _ = arr.shape
    out = arr
    if name in ("rgb_split", "glitch"):
        shift = int((12 + 30 * beat_amt) * strength)
        if shift:
            out = arr.copy()
            out[:, :, 0] = np.roll(arr[:, :, 0], shift, axis=1)
            out[:, :, 2] = np.roll(arr[:, :, 2], -shift, axis=1)
    if name == "glitch":
        out = out.copy() if out is arr else out
        amt = max(beat_amt, 0.35)
        for k in range(int(4 + 6 * strength)):
            r = _hash01(beat_idx * 31 + k)
            y0 = int(r * (H - 40))
            h = int(20 + _hash01(beat_idx * 17 + k) * 160)
            dx = int((_hash01(beat_idx * 7 + k) - 0.5) * 320 * strength * amt)
            out[y0:y0 + h] = np.roll(out[y0:y0 + h], dx, axis=1)
    if name == "duotone":
        lum = (arr[:, :, 0] * 0.299 + arr[:, :, 1] * 0.587 + arr[:, :, 2] * 0.114) / 255.0
        lum = np.clip(lum * (1.25 + 0.25 * beat_amt), 0, 1)[:, :, None]
        dark = np.array(_hex(palette[0]), dtype=np.float32)
        light = np.array(_hex(palette[1]), dtype=np.float32)
        tone = dark + (light - dark) * lum
        out = (arr * (1 - strength) + tone * strength).astype(np.int16)
    if name in ("scan", "duotone"):
        out = out.copy() if out is arr else out
        offset = int(t * 60) % 6
        out[offset::6] = (out[offset::6] * (1 - 0.5 * strength)).astype(np.int16)
        out[offset + 1::6] = (out[offset + 1::6] * (1 - 0.3 * strength)).astype(np.int16)
    if name == "scan":
        # VHS風: 暗い帯がゆっくり流れ、帯の中は横にずれる
        band_y = int((t * 260) % (H + 300)) - 300
        y0, y1 = max(band_y, 0), min(band_y + 300, H)
        if y1 > y0:
            out[y0:y1] = (np.roll(out[y0:y1], int(18 * strength), axis=1) * (1 - 0.35 * strength)).astype(np.int16)
        g_shift = int((4 + 14 * beat_amt) * strength)
        if g_shift:
            out[:, :, 1] = np.roll(out[:, :, 1], g_shift, axis=0)
    return Image.fromarray(np.clip(out, 0, 255).astype(np.uint8))


def _smooth(u):
    u = min(max(u, 0.0), 1.0)
    return u * u * (3 - 2 * u)


def camera_move(name, u, since_land, index):
    """背景カメラ: (寄り, 横位置-1..1, 縦位置-1..1, 傾き度)"""
    s = _smooth(u)
    side = 1 if index % 2 else -1
    if name == "push_in":
        return (_lerp(1.05, 1.3, s), 0.0, 0.0, 0.0)
    if name == "pull_out":
        return (_lerp(1.3, 1.05, s), 0.0, 0.0, 0.0)
    if name == "pan_l":
        return (1.12, _lerp(0.85, -0.85, s), 0.0, 0.0)
    if name == "pan_r":
        return (1.12, _lerp(-0.85, 0.85, s), 0.0, 0.0)
    if name == "tilt_up":
        return (1.22, 0.3 * side, _lerp(0.8, -0.8, s), 0.0)
    if name == "tilt_down":
        return (1.22, -0.3 * side, _lerp(-0.8, 0.8, s), 0.0)
    if name == "dutch":
        return (1.15, _lerp(0.4, -0.4, s) * side, 0.0, _lerp(-2.5, 2.5, s) * side)
    if name == "punch":
        base = _lerp(1.18, 1.26, s)
        if since_land >= 0:
            base += 0.25 * math.exp(-since_land / 0.12)
        return (base, 0.55 * side, 0.0, 1.5 * side)
    # drift
    return (_lerp(1.05, 1.12, s), _lerp(-0.3, 0.3, s) * side, 0.0, 0.0)


# ---------------------------------------------------------------------------

# ---------------------------------------------------------------------------

class KineticRenderer:
    def __init__(self, image_path, plan, beats, style, duration=None):
        self.plan = plan
        self.starts = [c["start"] for c in plan]
        # ショットごとの開始・終了（次のショットの開始まで動き続ける）
        self.shot_span = {}
        for j, c in enumerate(plan):
            sh = c.get("shot", j)
            lo, _hi = self.shot_span.get(sh, (c["start"], c["end"]))
            self.shot_span[sh] = (lo, c["end"])
        shots = sorted(self.shot_span)
        for a, b in zip(shots, shots[1:]):
            self.shot_span[a] = (self.shot_span[a][0], self.shot_span[b][0])
        if shots:
            lo, hi = self.shot_span[shots[-1]]
            self.shot_span[shots[-1]] = (lo, hi + 4.0)
        self.bg = _Background(image_path, beats, style)
        self.sprites = _Sprites(_Fonts())
        self.duration = duration
        self.interludes = find_interludes(plan, duration)
        self.interlude_starts = [s for s, _e, _n in self.interludes]
        self.cuts = []
        for c in plan:
            if c["bg"] == "image":
                colors = ("#FFFFFF", "#0B0B0F", style.get("caption_color", "#FF3B70"))
                palette_bg = None
            else:
                bg, text, stroke, accent = DEFAULT_PALETTE[c["bg"]]
                colors = (text, stroke, accent)
                palette_bg = bg
            self.cuts.append(_Cut(c, self.sprites, colors, palette_bg))

    def _active(self, t):
        k = bisect.bisect_right(self.starts, t + 0.2) - 1
        found = []
        for j in (k - 1, k):
            if 0 <= j < len(self.plan):
                c = self.plan[j]
                if c["start"] - 0.2 <= t <= c["end"]:
                    found.append(j)
        # 1カット1メッセージ: 次のカットが始まったら前のカットは描かない
        if len(found) == 2 and t >= self.plan[found[1]]["start"]:
            found = found[1:]
        if len(found) == 2:
            found = found[:1] if t < self.plan[found[1]]["start"] else found[1:]
        return found

    def _bg_mode_at(self, t):
        k = bisect.bisect_right(self.starts, t) - 1
        if k < 0:
            return "image", None, 1.0
        c = self.plan[k]
        mode = c["bg"]
        next_start = self.plan[k + 1]["start"] if k + 1 < len(self.plan) else float("inf")
        if t > c["end"] and next_start - c["end"] > INTERLUDE_MIN:
            return "image", None, 1.0
        prev = "image"
        if k > 0 and c["start"] - self.plan[k - 1]["end"] <= GAP_FOR_REST:
            prev = self.plan[k - 1]["bg"]
        p = (t - c["start"]) * FPS / 5
        if prev != mode and p < 1:
            return mode, prev, max(p, 0.0)
        return mode, None, 1.0

    def camera_at(self, t):
        """(背景カメラ, 画面全体の寄り, 画面全体の揺れx, 揺れy)"""
        k = bisect.bisect_right(self.starts, t) - 1
        if k < 0:
            first = self.starts[0] if self.starts else 1.0
            return camera_move("drift", t / max(first, 0.1), -1, 0), 1.0, 0.0, 0.0
        c = self.plan[k]
        sh = c.get("shot", k)
        lo, hi = self.shot_span.get(sh, (c["start"], c["end"]))
        u = (t - lo) / max(hi - lo, 0.1)
        since_land = t - c["land"]
        bg_cam = camera_move(c.get("camera", "drift"), u, since_land, sh)

        span_c = max((self.plan[k + 1]["start"] if k + 1 < len(self.plan) else c["end"]) - c["start"], 0.1)
        uc = min(max((t - c["start"]) / span_c, 0.0), 1.0)
        # 単色背景では背景カメラが見えないので、画面全体の寄りを強める
        g_zoom = 1.0 + (0.02 if c["bg"] == "image" else 0.06) * uc
        sx = sy = 0.0
        if c["level"] == 3 and since_land >= 0:
            g_zoom += 0.07 * math.exp(-since_land / 0.12)
            amp = 14 * math.exp(-since_land / 0.08)
            sx = amp * math.sin(t * 90)
            sy = amp * math.cos(t * 77)
            g_zoom += 0.012 * (self.bg.pulse(t) - 1.0) / max(self.bg.pulse_scale - 1.0, 1e-3)
        elif c["level"] == 1:
            g_zoom = 1.0 + 0.015 * uc
        return bg_cam, g_zoom, sx, sy

    def frame_at(self, t):
        bg_cam, g_zoom, sx, sy = self.camera_at(t)
        mode, prev, p = self._bg_mode_at(t)
        frame = self.bg.frame(mode, t, bg_cam)
        k = bisect.bisect_right(self.interlude_starts, t) - 1
        if k >= 0 and mode == "image" and prev is None:
            s0, e0, effect = self.interludes[k]
            if s0 <= t <= e0:
                strength = min((t - s0) / INTERLUDE_FADE, (e0 - t) / INTERLUDE_FADE, 1.0)
                bi = bisect.bisect_right(self.bg.beats, t) - 1
                beat_amt = 0.0
                if bi >= 0:
                    beat_amt = math.exp(-(t - self.bg.beats[bi]) / 0.12)
                frame = apply_interlude_effect(frame, effect, strength, t, bi, beat_amt,
                                               (DEFAULT_PALETTE[0][0], DEFAULT_PALETTE[0][3]) if k % 2
                                               else (DEFAULT_PALETTE[1][0], DEFAULT_PALETTE[2][0]))
        if prev is not None:
            old = self.bg.frame(prev, t, bg_cam)
            cut_x = int(VIDEO_SIZE[0] * _ease_out(p))
            k = bisect.bisect_right(self.starts, t) - 1
            if k % 2:
                frame.paste(old.crop((cut_x, 0, VIDEO_SIZE[0], VIDEO_SIZE[1])), (cut_x, 0))
            else:
                w = VIDEO_SIZE[0] - cut_x
                frame.paste(old.crop((0, 0, w, VIDEO_SIZE[1])), (0, 0))
        for j in self._active(t):
            self.cuts[j].draw(frame, t, self.sprites, bg_cam)
            c = self.plan[j]
            if c["flash"]:
                df = (t - c["land"]) * FPS
                if 0 <= df < 2:
                    white = Image.new("RGB", VIDEO_SIZE, (255, 255, 255))
                    frame = Image.blend(frame, white, 0.7 if df < 1 else 0.3)
        if g_zoom > 1.0005 or sx or sy:
            W, H = VIDEO_SIZE
            z = max(g_zoom, 1.0 + 2 * max(abs(sx), abs(sy)) / W)
            a = 1 / z
            c0 = W / 2 - a * W / 2 - sx / z
            f0 = H / 2 - a * H / 2 - sy / z
            frame = frame.transform(VIDEO_SIZE, Image.AFFINE, (a, 0, c0, 0, a, f0), resample=Image.BILINEAR)
        return frame


def render_kinetic(image_path, audio_path, plan, beats, style, output_path, progress=None):
    from moviepy import AudioFileClip, VideoClip

    audio = AudioFileClip(str(audio_path))
    renderer = KineticRenderer(image_path, plan, beats, style, duration=audio.duration)
    total = max(audio.duration, 0.1)

    def frame(t):
        if progress:
            progress(min(t / total, 0.99))
        return np.asarray(renderer.frame_at(t))

    clip = VideoClip(frame_function=frame, duration=audio.duration).with_audio(audio)
    output_path = Path(output_path)
    output_path.parent.mkdir(parents=True, exist_ok=True)
    clip.write_videofile(
        str(output_path), fps=FPS, codec="libx264", audio_codec="aac",
        preset="medium", logger=None,
    )
    return output_path


def render_stills(image_path, plan, beats, style, out_dir, cuts_per_sheet=8):
    """各カットの 0/25/50/75/100% と入りの着地直後を静止画にし、
    一覧画像（コンタクトシート）にまとめる。書き出し前の目視確認用。"""
    renderer = KineticRenderer(image_path, plan, beats, style)
    out_dir = Path(out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    thumb_w, thumb_h = 216, 384
    fractions = [0.0, 0.25, 0.5, 0.75, 0.97]
    sheets = []
    for s in range(0, len(plan), cuts_per_sheet):
        chunk = plan[s:s + cuts_per_sheet]
        sheet = Image.new("RGB", (thumb_w * len(fractions) + 80, thumb_h * len(chunk)), (40, 40, 40))
        d = ImageDraw.Draw(sheet)
        label_font = ImageFont.truetype(FONT_HEAVY, 22)
        for r, c in enumerate(chunk):
            d.text((6, r * thumb_h + 8), f"#{c['index']}", font=label_font, fill=(255, 255, 255))
            d.text((6, r * thumb_h + 40), c["motion"][:7], font=label_font, fill=(200, 200, 200))
            d.text((6, r * thumb_h + 70), c["layout"][:7], font=label_font, fill=(200, 200, 200))
            dur = c["end"] - c["start"]
            first_frame = ENTRANCE_FRAMES.get(c["entrance"], 6) / FPS
            for k, fr in enumerate(fractions):
                t = c["start"] + max(dur * fr, 0 if fr else 0) + (min(first_frame * 0.5, dur * 0.2) if fr == 0 else 0)
                im = renderer.frame_at(t).resize((thumb_w, thumb_h), Image.BILINEAR)
                sheet.paste(im, (80 + k * thumb_w, r * thumb_h))
        path = out_dir / f"sheet_{s // cuts_per_sheet + 1:02d}.png"
        sheet.save(path)
        sheets.append(path)
    return sheets


def load_or_build_plan(plan_path, alignment, sections, beats, style, replan=False):
    plan_path = Path(plan_path)
    lines = [a["line"] for a in alignment]
    if plan_path.exists() and not replan:
        saved = json.loads(plan_path.read_text(encoding="utf-8"))
        if [c["text"] for c in saved.get("plan", [])] == lines and saved.get("alignment") == alignment:
            return saved["plan"]
        print("      kinetic_plan.json は歌詞かタイミングが変わっているため作り直します")
    plan = build_plan(alignment, sections, beats, style)
    plan_path.parent.mkdir(parents=True, exist_ok=True)
    plan_path.write_text(
        json.dumps({"alignment": alignment, "plan": plan}, ensure_ascii=False, indent=1),
        encoding="utf-8",
    )
    plan_path.with_suffix(".md").write_text(plan_to_markdown(plan), encoding="utf-8")
    return plan
