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
from PIL import Image, ImageDraw, ImageEnhance, ImageFilter, ImageFont

import kinetic_bg
import kinetic_fx

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

# 子ども向けの曲（淡い色。文字は濃い色で読みやすく）
KIDS_PALETTE = [
    ("#FFF4D6", "#3A2A5A", "#FFF4D6", "#FF6FA8"),
    ("#BDE7FF", "#2B3A67", "#BDE7FF", "#FF8A00"),
    ("#FFD1E8", "#5A2A4A", "#FFD1E8", "#7A5CFF"),
    ("#FFE66D", "#3A2A5A", "#FFE66D", "#00A6A6"),
]
FONT_KIDS = "/System/Library/Fonts/ヒラギノ丸ゴ ProN W4.ttc"


def palette_for(plan):
    return KIDS_PALETTE if any(c.get("profile_kids") for c in plan) else DEFAULT_PALETTE


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
    # 仮名の擬音・動詞（一般的な語）
    (("きら", "ぴか", "かがや", "ひか"), "grow"),
    (("ころ", "ぐる", "くる", "まわ"), "rotate"),
    (("ぴょん", "じゃんぷ", "ジャンプ", "はね", "とぶ"), "bounce"),
    (("だっしゅ", "ダッシュ", "はし", "にげ"), "dash"),
]

VERSE_MOTIONS = ["slide_l", "stagger", "mask", "slide_r", "rotate", "stagger", "spread", "mask"]
QUIET_MOTIONS = ["mask", "float"]
HOOK_MOTIONS = ["slam", "grow", "slam", "converge"]
LAYOUTS = ["center", "left", "right", "vertical", "diagonal", "arc"]
CAMERA_MOVES = ["pan_l", "push_in", "tilt_up", "pan_r", "pull_out", "dutch", "tilt_down"]

# 入り（フレーム数）。記事の目安: 叩きつけ4〜8f / スライド5〜10f / マスク6〜12f
ENTRANCE_FRAMES = {
    "slam": 6, "slash": 6, "shake": 6, "stagger": 4, "slide_l": 7, "slide_r": 7,
    "dash": 6, "mask": 9, "spread": 12, "converge": 10, "rotate": 8, "fall": 8,
    "grow": 10, "float": 14, "erase": 9,
    "stamp": 6, "scatter": 5, "pop": 6, "neon": 6, "bounce": 12,
}
EXIT_FRAMES = 3
# 間のある退場（fall / drift）の長さ。カットの3割、0.14〜0.55秒（JIZURA の目安）
EXIT_RATIO, EXIT_MIN_SEC, EXIT_MAX_SEC = 0.3, 0.14, 0.55
# 保持（行が止まっている間の小さな動き）。入りの直後に 0.25 秒かけて立ち上がる
HOLD_MOTIONS = ("breathe", "wave", "jitter")
HOLD_RAMP_SEC = 0.25
MIN_FLASH_GAP = 2.0
MIN_BG_SWITCH_GAP = 0.5
GAP_FOR_REST = 1.2  # これ以上の無歌詞区間は「間」として背景を画像に戻す

INTERLUDE_EFFECTS = ["rgb_split", "glitch", "duotone", "scan", "kaleido"]
INTERLUDE_FADE = 0.35  # 間奏の出入りでエフェクトを強める/弱める秒数
INTERLUDE_MIN = 2.5    # これより短い歌詞の切れ目は間奏として扱わない

# 文字を収める横幅。画面の端はプラットフォームのUI（TikTok の右の操作ボタン、
# 下のユーザー名やキャプション、YouTube ショートの操作列）に隠れる可能性があるので、
# 1080px の画面に対して左右に余白を残す。
TEXT_WIDTH = 860          # center / diagonal（左右それぞれ約110px の余白）
TEXT_WIDTH_NARROW = 790   # 左右に寄せたレイアウト（寄せた側がより端に近づくため）

VERTICAL_MAP = {"ー": "｜", "「": "﹁", "」": "﹂", "『": "﹃", "』": "﹄", "（": "︵", "）": "︶", "…": "︙"}


# ---------------------------------------------------------------------------
# 設計（plan）

# グロウル・スクリーム・ブレイクダウン。甘い歌の部分とはっきり別の声なので、サビと同じ「強」にし、
# 揺れて入る（build_plan）。構成タグ名で判定する
GROWL_TAGS = ("growl", "scream", "breakdown", "shout")


def is_growl(section):
    s = (section or "").lower()
    return any(t in s for t in GROWL_TAGS)


def section_intensity(section):
    s = section or ""
    if "囁" in s:
        return 1
    if is_growl(s):
        return 3
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


NO_HEAD = set("んーっゃゅょぁぃぅぇぉンッャュョァィゥェォ☆★！？!?、。」』）…〜♪")
PARTICLE_ENDS = ("じゃ", "は", "が", "を", "に", "で", "も", "の", "と", "へ", "て", "ね", "よ", "から", "まで")


def _script(ch):
    o = ord(ch)
    if 0x3040 <= o <= 0x309F:
        return "hira"
    if 0x30A0 <= o <= 0x30FF:
        return "kata" if ch != "ー" else "long"
    if kinetic_fx._is_kanji(ch):
        return "kanji"
    return "other"


def _chunk(token, limit=8):
    """長い段を、言葉の切れ目らしい位置で limit 文字以下に折る。"""
    if len(token) <= limit:
        return [token]
    best, best_score = None, -1e9
    for i in range(2, len(token) - 1):
        left, right = token[:i], token[i:]
        if right[0] in NO_HEAD:
            continue
        score = -abs(len(left) - len(right)) * 0.8
        if min(len(left), len(right)) <= 2:
            # 「おふろあが／りで」のような、片方だけ極端に短い割り方を避ける
            score -= 3
        if left[-1] in "ゃゅょャュョ":
            # 拗音の途中で言葉が終わったように見える
            score -= 2.5
        # 助詞で終わるなら切れ目らしい。ただし1文字の助詞は語の途中にも頻出するので弱く
        # （「のらね｜こさわって」のような割り方を防ぐ）
        hit = next((pw for pw in PARTICLE_ENDS if left.endswith(pw)), None)
        if hit:
            score += 2.5 if len(hit) >= 2 else 1.0
        if right[0] in "のにをがはでともへ":
            # 段の頭が助詞だと、前の段から千切れて見える（「はじめて／のねつで」）
            score -= 2.0
        k = len(left) - 1
        while k > 0 and left[k] == "ー":
            k -= 1
        a, b = _script(left[k]), _script(right[0])
        if a != b and "other" not in (a, b):
            score += 2.5
        if left[-1] in "！？!?」』" or right[0] in "「『":
            score += 3
        if len(left) >= 4 and left[-2:] == left[-4:-2]:
            score += 2.5
        if score > best_score:
            best, best_score = i, score
    if best is None:
        best = (len(token) + 1) // 2
    return _chunk(token[:best], limit) + _chunk(token[best:], limit)


LATIN_ROW_CHARS = 18  # 英語の1段の上限（文字数）。これを超える句を割る。1段が長いと文字が小さくなる
LATIN_GROWL_ROW_CHARS = 16
# 段の終わりに来ると、言葉が途中で千切れて見える語（冠詞・前置詞・所有格など）
LATIN_WEAK_ENDS = {"a", "an", "the", "to", "of", "in", "at", "on", "as", "my", "your", "our",
                   "and", "but", "or", "if", "when", "where", "i", "you", "we", "me", "that", "what"}


def _latin_word(w):
    return "".join(c for c in w.lower() if c.isalpha())


def _split_latin_phrase(words, limit=LATIN_ROW_CHARS):
    """読点を含まない1句を、LATIN_ROW_CHARS 以下になるまで語の切れ目で二つに割っていく。
    割る位置は、長い方の段が短く、前の段が冠詞・前置詞で終わらず、1語だけの段ができない所。"""
    joined = " ".join(words)
    if len(words) <= 1 or len(joined) <= limit:
        return [joined]
    best, best_score = 1, None
    for i in range(1, len(words)):
        left, right = " ".join(words[:i]), " ".join(words[i:])
        score = max(len(left), len(right))
        if _latin_word(words[i - 1]) in LATIN_WEAK_ENDS:
            score += 8
        if i == 1 or i == len(words) - 1:
            score += 6  # 1語だけの段は千切れて見える
        if best_score is None or score < best_score:
            best, best_score = i, score
    return _split_latin_phrase(words[:best], limit) + _split_latin_phrase(words[best:], limit)


def _split_latin_rows(text, limit=LATIN_ROW_CHARS):
    """英語の行を段に折る。空白ごとに1語1段にすると「Walk / down / to / the …」と
    細切れになって読めないので、まず読点（,）・ダッシュの後で句に分け、
    長い句だけを語の切れ目で割る。"""
    words = text.split()
    phrases, cur = [], []
    for w in words:
        if w in ("—", "–"):
            if cur:
                cur.append(w)
                phrases.append(cur)
                cur = []
            continue
        cur.append(w)
        if w[-1] in ",;:!?—–":
            phrases.append(cur)
            cur = []
    if cur:
        phrases.append(cur)
    rows = []
    for ph in phrases:
        rows.extend(_split_latin_phrase(ph, limit))
    return rows or [text]


def _split_rows(text, short=False, latin=False, growl=False):
    """全角/半角スペースで区切られた行は、区切りごとに段を分ける。
    区切りがなく12文字を超える行は、言葉の切れ目らしい位置で折る。
    short=True（子ども向け）は、7文字を超える段も折る。1段が短いほど文字が大きくなる。
    latin=True（英語詞）は、読点と語の切れ目で折る。"""
    if latin:
        # グロウルは短い段で大きく叩きつける
        return _split_latin_rows(text, LATIN_GROWL_ROW_CHARS if growl else LATIN_ROW_CHARS)
    if short:
        rows = []
        for r in _split_rows(text):
            rows.extend(_chunk(r, 7))
        return rows
    tokens = [t for t in text.replace("　", " ").split(" ") if t]
    if len(tokens) >= 2 and _clean_len(text) > 6:
        return tokens
    joined = "".join(tokens) or text
    if len(joined) > 12:
        return _chunk(joined, 12)
    return [joined]


def build_plan(alignment, sections, beats, style, meta=None, backgrounds=None):
    """alignment（[{line,start,end}]）と、行ごとの構成タグ名から、
    カットごとの設計を作る。meta（曲ノートの title/genre/tags/bpm）があれば、
    曲の性格に合わせて参考作品由来の技法（kinetic_fx）を割り当てる。"""
    max_hold = style.get("max_hold_sec", 2.8)
    profile = kinetic_fx.song_profile(alignment, sections, beats, meta)
    if profile.get("kids"):
        # 子ども向けの曲は1行を4〜5秒かけてゆっくり歌う。既定の頭打ち（約3秒）だと
        # 歌い終わる前に文字が消えて「歌詞が抜けている」ように見えるので、長く残す。
        # 次の行が始まればどのみちそこで消える。
        max_hold = max(max_hold, 5.5)
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
        latin = profile.get("latin", False)
        growl = is_growl(section)
        rows = _split_rows(text, short=profile.get("kids", False), latin=latin, growl=growl)
        n = _clean_len(text)
        if level == 3 and len(rows) == 1 and 4 <= n <= 8 and not latin:
            rows = [rows[0][:-2], rows[0][-2:]]

        meaning = _pick_meaning(text)
        if growl:
            motion = "shake"
        elif level == 1:
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
            if n > 7 or latin:
                # 英語を縦に積むと読めない
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
            tier = 1 if n <= (14 if latin else 8) or profile.get("kids") else 2

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
            "text_y": 0.42 if profile.get("kids") else 0.5,
            "shot": shot,
            "camera": camera,
            "text": text,
            "section": section,
            "level": level,
            "rows": rows,
            "latin": latin,
            "growl": growl,
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

    kinetic_fx.assign_techniques(plan, profile)
    kinetic_bg.assign_backgrounds(plan, profile)
    kinetic_bg.assign_bg_images(plan, backgrounds)
    for cut in plan:
        if cut.get("growl") and not profile.get("kids"):
            # グロウルは技法の割り当て（判子・散らし等）より「揺れて入る」を優先し、
            # 和風の曲なら版ズレで荒らす。甘い歌の部分と一目で別の声だとわかるように
            cut["motion"] = cut["entrance"] = "shake"
            if profile["wa"] and cut.get("texture") in (None, "grain"):
                cut["texture"] = "misregister"
        cut["profile_wa"] = profile["wa"]
        cut["profile_kids"] = profile.get("kids", False)
        if cut["profile_kids"]:
            cut["flash"] = False

    last_flash = -99.0
    for cut in plan:
        if cut["flash"]:
            if cut["land"] - last_flash < MIN_FLASH_GAP:
                cut["flash"] = False
            else:
                last_flash = cut["land"]
    return plan


def plan_to_markdown(plan):
    out = ["| # | 時間 | 強さ | 構図 | 動き | 背景 | カメラ | 装飾 | 質感 | 保持 | 退場 | フラッシュ | 背景処理 | 下敷き | 切替 |",
           "|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|"]
    for c in plan:
        bg = c["bg"] if c["bg"] == "image" else f"色{c['bg']}"
        out.append(
            f"| {c['index']} | {c['start']:.2f}–{c['end']:.2f} | {c['level']} | {c['layout']} | "
            f"{c['motion']} | {bg} | {c.get('camera', '')} | {c.get('decor') or ''} | "
            f"{c.get('texture') or ''} | {c.get('hold') or ''} | {c.get('exit') or ''} | {'●' if c['flash'] else ''} | "
            f"{c.get('bgfx') or ''} | {c.get('under') or ''} | {c.get('wipe') or ''} |"
        )
    return "\n".join(out) + "\n"


# ---------------------------------------------------------------------------
# 描画の部品

def _hex(c):
    c = c.lstrip("#")
    return tuple(int(c[i:i + 2], 16) for i in (0, 2, 4))


def _luma(color):
    r, g, b = _hex(color)[:3]
    return (0.299 * r + 0.587 * g + 0.114 * b) / 255


def _darken(color, k):
    r, g, b = _hex(color)[:3]
    return "#%02X%02X%02X" % (int(r * k), int(g * k), int(b * k))


def _readable(color, bg, fallback, gap=0.45):
    """背景と明度が近すぎる文字色を、読める色にする。
    パステルの背景にパステルの差し色を置くと、輪郭は見えても文字が沈む。
    まず色味を保ったまま暗くし、それでも足りなければ本文色に逃がす。"""
    if bg is None:
        return color
    bg_l = _luma(bg)
    if abs(_luma(color) - bg_l) >= gap:
        return color
    for k in (0.8, 0.65, 0.5, 0.4):
        darker = _darken(color, k)
        if abs(_luma(darker) - bg_l) >= gap:
            return darker
    return fallback if abs(_luma(fallback) - bg_l) >= gap else color


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
        self.echoes = {}

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

    def outlined(self, ch, font_path, size, fill, outline):
        """太い外側の縁取り＋文字色の細い縁取り（細い書体を太く見せる）。"""
        key = ("outlined", ch, font_path, size, fill, outline)
        g = self.base.get(key)
        if g is None:
            font = self.fonts.get(font_path, size)
            sw_out = max(size // 9, 6)
            sw_in = max(size // 28, 2)
            l, t, r, b = font.getbbox(ch, stroke_width=sw_out)
            w, h = max(r - l, 1), max(b - t, 1)
            img = Image.new("RGBA", (w, h), (0, 0, 0, 0))
            d = ImageDraw.Draw(img)
            d.text((-l, -t), ch, font=font, fill=_hex(outline) + (255,),
                   stroke_width=sw_out, stroke_fill=_hex(outline) + (255,))
            d.text((-l, -t), ch, font=font, fill=_hex(fill) + (255,),
                   stroke_width=sw_in, stroke_fill=_hex(fill) + (255,))
            g = (img, l, t, font.getlength(ch))
            self.base[key] = g
        return g

    def neon(self, ch, font_path, size, color):
        key = ("neon", ch, font_path, size, color)
        g = self.base.get(key)
        if g is None:
            font = self.fonts.get(font_path, size)
            sw = max(size // 28, 3)
            pad = size // 6
            l, t, r, b = font.getbbox(ch, stroke_width=sw)
            w, h = r - l + pad * 2, b - t + pad * 2
            outline = Image.new("RGBA", (w, h), (0, 0, 0, 0))
            ImageDraw.Draw(outline).text(
                (pad - l, pad - t), ch, font=font, fill=(0, 0, 0, 0),
                stroke_width=sw, stroke_fill=_hex(color) + (255,),
            )
            glow = outline.filter(ImageFilter.GaussianBlur(max(size // 16, 4)))
            glow.putalpha(glow.getchannel("A").point(lambda v: min(int(v * 2.2), 255)))
            core = Image.new("RGBA", (w, h), (0, 0, 0, 0))
            ImageDraw.Draw(core).text(
                (pad - l, pad - t), ch, font=font, fill=(0, 0, 0, 0),
                stroke_width=max(sw // 3, 1), stroke_fill=(255, 255, 255, 255),
            )
            img = Image.alpha_composite(Image.alpha_composite(glow, outline), core)
            g = (img, l - pad, t - pad, font.getlength(ch))
            self.base[key] = g
        return g

    def echo(self, text, color):
        key = (text, color)
        im = self.echoes.get(key)
        if im is None:
            size = int(min(1500 / max(len(text), 1), 900))
            font = self.fonts.get(FONT_HEAVY, size)
            l, t, r, b = font.getbbox(text)
            im = Image.new("RGBA", (max(r - l, 1), max(b - t, 1)), (0, 0, 0, 0))
            ImageDraw.Draw(im).text((-l, -t), text, font=font, fill=color + (255,))
            im.putalpha(im.getchannel("A").point(lambda v: int(v * 0.08)))
            self.echoes[key] = im
        return im

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

    def __init__(self, cut, sprites, colors, palette_bg, beats=None):
        self.cut = cut
        self._shared = sprites
        self.colors = colors
        self.beats = beats or []
        text_color, stroke_color, accent = colors
        level = cut["level"]
        rows = cut["rows"]
        vertical = cut["layout"] == "vertical"
        font_path = FONT_QUIET if level == 1 else FONT_HEAVY
        if cut.get("profile_kids"):
            font_path = FONT_KIDS
        if cut.get("profile_kids"):
            accent = _readable(accent, palette_bg, text_color)
            colors = (text_color, stroke_color, accent)
        emphasis = bool(cut.get("emphasis"))
        neon = cut.get("entrance") == "neon"
        self.overlay = None
        # 長い影（texture == long_shadow）用。文字の形をアクセント色の暗い版で塗った画像を1字ずつ持つ
        self._shadows = {}
        self.shadow_color = _darken(accent, 0.55)

        latin = bool(cut.get("latin"))

        def weight(row):
            if latin:
                # 欧文は1字が全角の約6割の幅。文字数のままだと小さくなりすぎる（幅は後で実寸で詰める）
                return max(len(row) * 0.6, 1)
            if not emphasis:
                return max(len(row), 1)
            return max(sum(1.0 if kinetic_fx._is_kanji(ch) else 0.66 for ch in row), 1)

        def row_size(row):
            n = weight(row)
            if vertical:
                return max(min(int(1500 / n), 320 if cut["tier"] == 1 else 220), 60)
            budget = TEXT_WIDTH if cut["layout"] in ("center", "diagonal") else TEXT_WIDTH_NARROW
            kids = bool(cut.get("profile_kids"))
            if cut["tier"] == 1:
                cap = 480 if n <= 2 else 400 if n <= 4 else 300
            else:
                cap = 300 if kids else 210
            if len(rows) >= 2:
                if level == 1:
                    cap = min(cap, 300 if kids else 210)
                elif kids:
                    # 子ども向けは行数が増えても大きく。幅は実寸で詰めるので溢れない
                    cap = 400 if n <= 2 else 360 if n <= 4 else 320
                else:
                    cap = 400 if n == 1 else 340 if n <= 2 else 300 if n <= 3 else 260
            return max(min(int(budget / n), cap), 60)

        if vertical or cut.get("profile_kids"):
            # 段ごとに大きさが変わると、2文字の段だけ巨大になって落ち着かない
            sizes = [min(row_size(r) for r in rows)] * len(rows)
        else:
            sizes = [row_size(r) for r in rows]
            # 実際に描いたときの横幅で詰める。文字数からの見積もりだけだと、
            # 書体ごとの字送りや強調の縮小でずれて、端のUIに重なることがある。
            safe_w = TEXT_WIDTH if cut["layout"] in ("center", "diagonal") else TEXT_WIDTH_NARROW

            def row_width(row, s):
                return sum(
                    sprites.fonts.get(
                        font_path,
                        int(s * 0.66) if emphasis and not kinetic_fx._is_kanji(ch) else s,
                    ).getlength(ch)
                    for ch in row
                )

            # 縁取り（白フチ）は字送りの外側に出るので、その分を見込む
            for ri, row in enumerate(rows):
                while sizes[ri] > 60 and row_width(row, sizes[ri]) * 1.09 > safe_w:
                    sizes[ri] = max(int(sizes[ri] * 0.94), 60)
        size = max(sizes)
        self.size = size

        # 行の中で一番長い漢字の連なり（強調する語）
        emph_idx = set()
        if emphasis:
            flat = "".join(rows)
            best, cur = (0, 0), None
            for k, ch in enumerate(flat + " "):
                if kinetic_fx._is_kanji(ch):
                    cur = k if cur is None else cur
                elif cur is not None:
                    if k - cur > best[1] - best[0]:
                        best = (cur, k)
                    cur = None
            emph_idx = set(range(*best))
        mis_colors = [accent, "#F2C200"] if cut.get("texture") == "misregister" else []

        kids_style = bool(cut.get("profile_kids"))

        def make_glyph(draw_ch, fpath, gsize, fill, stroke, sw):
            if kids_style:
                return sprites.outlined(draw_ch, fpath, gsize, fill, "#FFFFFF"), ("outlined", draw_ch, fpath, gsize, fill)
            if neon:
                return sprites.neon(draw_ch, fpath, gsize, accent if fill == accent else "#FF5FA2"), ("neon", draw_ch, fpath, gsize, fill)
            return sprites.glyph(draw_ch, fpath, gsize, fill, stroke, sw), (draw_ch, fpath, gsize, fill, stroke, sw)

        # 段ごとに、最後の段（またはHookの末尾2文字）をアクセント色にする
        glyphs = []
        order = 0
        flat_i = 0
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
                def csize(ch):
                    if emphasis and not kinetic_fx._is_kanji(ch):
                        return int(rsize * 0.66)
                    return rsize
                row_w = sum(sprites.fonts.get(font_path, csize(ch)).getlength(ch) for ch in row)
                # 弧に沿わせるときの半径。段が長いほど緩い弧にして、端が落ちすぎないようにする
                arc_r = max(row_w * 1.5, 700.0) if cut["layout"] == "arc" else 0.0
                if cut["layout"] == "left":
                    x = -row_w / 2 - 60 + ri * 40
                elif cut["layout"] == "right":
                    x = -row_w / 2 + 60 - ri * 40
                else:
                    x = -row_w / 2
                y0 = y_cursor
                y_cursor += row_hs[ri]
            for ci, ch in enumerate(row):
                if emphasis:
                    is_accent = flat_i in emph_idx
                else:
                    if latin:
                        # 欧文は末尾2文字ではなく最後の1語を差し色に
                        is_accent = accent_row or (level == 3 and len(rows) == 1 and " " in row and ci > row.rfind(" "))
                    else:
                        is_accent = accent_row or (level == 3 and len(rows) == 1 and len(row) >= 4 and ci >= len(row) - 2)
                flat_i += 1
                fill = accent if is_accent else text_color
                stroke = stroke_color
                if is_accent and palette_bg is not None and _hex(accent) == _hex(stroke_color):
                    stroke = text_color
                draw_ch = VERTICAL_MAP.get(ch, ch) if vertical else ch
                gsize = rsize if vertical else csize(ch)
                gsw = max(gsize // 24, 3) if palette_bg is None else max(gsize // 40, 2)
                g, key = make_glyph(draw_ch, font_path, gsize, fill, stroke, gsw)
                img, l, t, adv = g
                w, h = img.size
                if vertical:
                    cx = x0 + rsize / 2
                    cy = y + rsize * 1.02 * ci + rsize / 2
                    angle = 0
                else:
                    drop = (rsize - gsize) * 0.78
                    cx = x + l + w / 2
                    cy = y0 + t + h / 2 + drop
                    x += adv
                    angle = 0
                    if arc_r:
                        # 行の中心からの距離を角度に読み替え、円周上へ。文字も接線の向きに傾ける
                        # （cx は行の中心が 0。ここに row_w/2 を足すと行ごと片側へ寄る）
                        theta = cx / arc_r
                        cx = arc_r * math.sin(theta)
                        cy = cy - arc_r * (1 - math.cos(theta)) * (1 if ri % 2 == 0 else -1)
                        angle = -math.degrees(theta) * (1 if ri % 2 == 0 else -1)
                mis = []
                for mc in mis_colors:
                    mg = sprites.glyph(draw_ch, font_path, gsize, mc, mc, gsw)
                    mis.append(((draw_ch, font_path, gsize, mc, mc, gsw), mg[0]))
                glyphs.append({
                    "key": key, "img": img, "cx": cx, "cy": cy, "order": order, "row": ri,
                    "angle": angle, "mis": mis,
                })
                order += 1
        if cut["layout"] == "grid":
            glyphs = self._grid_glyphs(sprites, "".join(rows), font_path, text_color, stroke_color, accent, palette_bg)
        if cut.get("entrance") == "scatter":
            for g in glyphs:
                o = g["order"] + cut["index"] * 7
                g["cx"] += (kinetic_fx._hash01(o) - 0.5) * self.size * 0.5
                g["cy"] += (kinetic_fx._hash01(o + 11) - 0.5) * self.size * 0.8
                g["angle"] += (kinetic_fx._hash01(o + 23) - 0.5) * 36
                g["gscale"] = 0.75 + 0.5 * kinetic_fx._hash01(o + 31)
        self.glyphs = glyphs
        self.count = max(order, 1)
        self.n_rows = len(rows)
        if cut.get("entrance") == "stamp":
            self.overlay = self._stamp_overlay(accent)
        self.under = None
        if cut.get("under") and self.glyphs:
            x0, y0, x1, y1 = self._bounds()
            if cut["under"] == "brush":
                wa = cut.get("profile_wa")
                light_text = sum(_hex(text_color)) > 380
                color = ("#111111" if not light_text else "#C1121F") if wa else accent
                if _hex(color) == _hex(text_color):
                    color = "#111111" if light_text else "#FFFFFF"
                self.under = kinetic_bg.brush_stroke(x1 - x0 + 180, (y1 - y0) * 1.35 + 40, color, cut["index"])
            else:
                pad = 48
                card_color = _hex(palette_bg) if palette_bg else (12, 12, 15)
                card = Image.new("RGBA", (int(x1 - x0 + pad * 2), int(y1 - y0 + pad * 2)), card_color + (238,))
                ImageDraw.Draw(card).rectangle([0, 0, card.width - 1, card.height - 1],
                                               outline=_hex(accent) + (255,), width=8)
                self.under = card

        # text_y は画面の高さに対する文字の中心位置（0.5 が中央）。
        # kinetic_plan.json で1カットずつ直せる
        ty = float(cut.get("text_y", 0.5))
        if cut["layout"] == "left":
            self.anchor = (VIDEO_SIZE[0] * 0.47, VIDEO_SIZE[1] * (ty - 0.05))
        elif cut["layout"] == "right":
            self.anchor = (VIDEO_SIZE[0] * 0.53, VIDEO_SIZE[1] * (ty + 0.05))
        elif cut["layout"] == "vertical":
            self.anchor = (VIDEO_SIZE[0] * (0.68 if cut["index"] % 2 else 0.32), VIDEO_SIZE[1] * (ty - 0.04))
        else:
            self.anchor = (VIDEO_SIZE[0] / 2, VIDEO_SIZE[1] * ty)
        if self.glyphs:
            # 画面の上下からはみ出さないよう、文字の中心位置を戻す
            _x0, y0, _x1, y1 = self._bounds()
            ax, ay = self.anchor
            margin = 70
            if ay + y0 < margin:
                ay = margin - y0
            if ay + y1 > VIDEO_SIZE[1] - margin:
                ay = VIDEO_SIZE[1] - margin - y1
            self.anchor = (ax, ay)
        self.base_angle = -8 if cut["layout"] == "diagonal" else 0
        if cut.get("entrance") == "stamp":
            self.base_angle = -4 if cut["index"] % 2 else 3
        if cut["layout"] == "grid":
            self.base_angle = -6 if cut["index"] % 2 else 5

        # 背景に敷く巨大な文字
        echo_text = max(rows, key=len)
        echo_color = _hex(text_color) if palette_bg is not None else (255, 255, 255)
        self.echo = sprites.echo(echo_text, echo_color)

    def _bounds(self):
        xs0 = [g["cx"] - g["img"].width / 2 for g in self.glyphs]
        xs1 = [g["cx"] + g["img"].width / 2 for g in self.glyphs]
        ys0 = [g["cy"] - g["img"].height / 2 for g in self.glyphs]
        ys1 = [g["cy"] + g["img"].height / 2 for g in self.glyphs]
        return min(xs0), min(ys0), max(xs1), max(ys1)

    def _grid_glyphs(self, sprites, text, font_path, text_color, stroke_color, accent, palette_bg):
        """4文字を2×2の格子に置く。格子の線は overlay として描く。"""
        cell = 420
        gsize = 300
        sw = max(gsize // 24, 3) if palette_bg is None else max(gsize // 40, 2)
        glyphs = []
        for k, ch in enumerate(text[:4]):
            fill = accent if k >= 2 else text_color
            img, l, t, adv = sprites.glyph(ch, font_path, gsize, fill, stroke_color, sw)
            cx = (k % 2 - 0.5) * cell
            cy = (k // 2 - 0.5) * cell
            glyphs.append({"key": (ch, font_path, gsize, fill, stroke_color, sw), "img": img,
                           "cx": cx, "cy": cy, "order": k, "row": k // 2, "angle": 0, "mis": []})
        size = cell * 2 + 40
        ov = Image.new("RGBA", (size, size), (0, 0, 0, 0))
        d = ImageDraw.Draw(ov)
        col = _hex(accent) + (255,)
        d.rectangle([20, 20, size - 20, size - 20], outline=col, width=12)
        d.line([(size / 2, 20), (size / 2, size - 20)], fill=col, width=12)
        d.line([(20, size / 2), (size - 20, size / 2)], fill=col, width=12)
        self.overlay = ov
        self.size = gsize
        return glyphs

    def _stamp_overlay(self, accent):
        """判子の枠と、墨の飛び散り。"""
        x0, y0, x1, y1 = self._bounds()
        pad = 50
        w, h = int(x1 - x0 + pad * 2 + 160), int(y1 - y0 + pad * 2 + 160)
        ov = Image.new("RGBA", (w, h), (0, 0, 0, 0))
        d = ImageDraw.Draw(ov)
        col = _hex(accent) + (255,)
        d.rectangle([80, 80, w - 80, h - 80], outline=col, width=max(int(self.size / 22), 8))
        for k in range(26):
            r1 = kinetic_fx._hash01(self.cut["index"] * 13 + k)
            r2 = kinetic_fx._hash01(self.cut["index"] * 29 + k)
            r3 = kinetic_fx._hash01(self.cut["index"] * 41 + k)
            side = k % 4
            if side == 0:
                cx, cy = r1 * w, r2 * 90
            elif side == 1:
                cx, cy = r1 * w, h - r2 * 90
            elif side == 2:
                cx, cy = r2 * 90, r1 * h
            else:
                cx, cy = w - r2 * 90, r1 * h
            rad = 4 + r3 * 22
            d.ellipse([cx - rad, cy - rad, cx + rad, cy + rad], fill=col)
        # 版の欠け（枠の一部をかすれさせる）
        a = np.asarray(ov.getchannel("A"), dtype=np.int16)
        rng = np.random.default_rng(self.cut["index"])
        a = np.where(rng.random(a.shape) < 0.18, 0, a).astype(np.uint8)
        ov.putalpha(Image.fromarray(a))
        return ov

    def _beat_pulse(self, t):
        if not self.beats:
            return 0.0
        k = bisect.bisect_right(self.beats, t) - 1
        if k < 0:
            return 0.0
        return math.exp(-(t - self.beats[k]) / 0.15)

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
        elif motion == "bounce":
            p = f - o * 1.5
            if p < 0:
                alpha = 0.0
            else:
                q = min(p / E, 1.0)
                # 上から落ちて2回弾む
                dy = -420 * abs(math.cos(q * math.pi * 1.5)) * (1 - q) ** 1.5
                scale = 1.0 + 0.08 * math.sin(q * math.pi * 3) * (1 - q)
                alpha = min(p / 2, 1.0)
        elif motion == "stamp":
            lead = (cut["land"] - cut["start"]) * FPS - 3
            p = f - lead
            if p < 0:
                alpha = 0.0
            elif p < 3:
                scale = _lerp(2.4, 0.9, p / 3)
                alpha = min(p / 1.5, 1.0)
            elif p < E:
                scale = _lerp(0.9, 1.0, (p - 3) / (E - 3))
            if 3 <= p < 6:
                dy += 10 * (1 - (p - 3) / 3)
        elif motion == "scatter":
            p = f - o * 3
            if p < 0:
                alpha = 0.0
            else:
                q = _ease_out(p / E)
                scale = _lerp(1.7, 1.0, q)
                alpha = min(p / 2, 1.0)
        elif motion == "pop":
            p = f - 2 - o * 3
            if p < 0:
                alpha = 0.0
            else:
                q = min(p / E, 1.0)
                scale = _lerp(0.2, 1.12, q / 0.7) if q < 0.7 else _lerp(1.12, 1.0, (q - 0.7) / 0.3)
                alpha = min(p / 2, 1.0)
        elif motion == "neon":
            p = f - o * 2
            if p < 0:
                alpha = 0.0
            else:
                crop_right = 1 - _ease_out(p / E)
                flicker = (0.35, 1.0, 0.5, 1.0)
                alpha = flicker[int(p)] if p < len(flicker) else 1.0

        if cut.get("hold") == "heartbeat" and f > E:
            scale *= 1 + 0.08 * self._beat_pulse(cut["start"] + tl)
        if cut.get("hold") in HOLD_MOTIONS and f > E:
            hdx, hdy, hscale, hangle = self._hold_state(cut["hold"], g, tl, dur, f - E)
            dx += hdx
            dy += hdy
            scale *= hscale
            angle += hangle
        scale *= g.get("gscale", 1.0)

        # 退場
        remain = (dur - tl) * FPS
        if cut.get("exit") == "fly" and remain < 7:
            q = 1 - max(remain, 0) / 7
            scale *= 1 + 3.0 * q * q
            alpha *= 1 - q * q
        elif cut.get("exit") == "split" and remain < 8:
            pass
        elif cut.get("exit") == "shatter" and remain < 10:
            q = 1 - max(remain, 0) / 10
            alpha *= 1 - q * q
        elif cut.get("exit") in ("fall", "drift"):
            edx, edy, escale, eangle, ealpha = self._exit_state(cut["exit"], g, tl, dur)
            dx += edx
            dy += edy
            scale *= escale
            angle += eangle
            alpha *= ealpha
        elif cut["motion"] == "erase" and remain < 8:
            crop_right = 1 - max(remain, 0) / 8
        elif remain < EXIT_FRAMES:
            q = max(remain, 0) / EXIT_FRAMES
            alpha *= q
            scale *= _lerp(0.9, 1.0, q)
        return dx, dy, scale, angle, alpha, crop_top, crop_right

    def _exit_frames(self, dur):
        """退場にかける長さ（フレーム）。fall / drift はカットの長さに応じて伸縮する"""
        if self.cut.get("exit") in ("fall", "drift"):
            return max(min(dur * EXIT_RATIO, EXIT_MAX_SEC), EXIT_MIN_SEC) * FPS
        return {"fly": 7, "split": 8, "shatter": 10}.get(self.cut.get("exit"), EXIT_FRAMES)

    def _noise(self, g, *keys):
        """この文字・このカットで決まる -1〜1 の乱数（フレームごとに変えたいときは keys に刻みを入れる）"""
        n = self.cut["index"] * 7919 + g["order"] * 131
        for k in keys:
            n = n * 31 + int(k)
        return kinetic_fx._hash01(n) * 2 - 1

    def _hold_state(self, hold, g, tl, dur, since):
        """保持: 行が止まっている間の小さな動き。(dx, dy, scale, angle)。
        量は入りの直後に立ち上がり、退場に入ると消える。控えめが原則"""
        remain = (dur - tl) * FPS
        amt = min(since / (HOLD_RAMP_SEC * FPS), 1.0) * min(max(remain, 0) / max(self._exit_frames(dur), 1), 1.0)
        if amt <= 0:
            return 0.0, 0.0, 1.0, 0.0
        size = g["img"].size[1]
        o = g["order"]
        if hold == "breathe":
            # 呼吸: 行全体が 0.9Hz でわずかに膨らみ縮む（しっとりした行）
            return 0.0, 0.0, 1 + 0.035 * math.sin(tl * math.tau * 0.9) * amt, 0.0
        if hold == "wave":
            # ウェーブ: 1字ずつ位相をずらして上下し、少し傾く（弾む曲・子ども向け）
            ph = tl * 7 + o * 0.75
            return 0.0, math.sin(ph) * size * 0.07 * amt, 1.0, math.cos(ph) * 5 * amt
        if hold == "jitter":
            # ジッター: 12Hz の刻みで 1 字ずつ小さく震える（速い曲）
            step = int(tl * 12)
            a = size * 0.025 * amt
            return (self._noise(g, step, 1) * a, self._noise(g, step, 2) * a,
                    1.0, self._noise(g, step, 3) * 4 * amt)
        return 0.0, 0.0, 1.0, 0.0

    def _exit_state(self, exit_name, g, tl, dur):
        """間のある退場。(dx, dy, scale, angle, alpha)。
        fall: 直前に震えてから 1 字ずつ重力で落ちる。drift: 1 字ずつ縮みながら舞い上がって消える"""
        out_f = self._exit_frames(dur)
        te = out_f - (dur - tl) * FPS  # 退場に入ってからのフレーム数（負なら手前）
        size = g["img"].size[1]
        u1, u2, u3 = ((self._noise(g, k) + 1) / 2 for k in (1, 2, 3))
        if exit_name == "fall":
            if te < 0:
                if te > -0.25 * FPS:
                    return self._noise(g, int(tl * 12)) * size * 0.03, 0.0, 1.0, 0.0, 1.0
                return 0.0, 0.0, 1.0, 0.0, 1.0
            x = max(te - u1 * out_f * 0.4, 0.0) / max(out_f * 0.6, 1.0)
            return ((u2 * 2 - 1) * VIDEO_SIZE[0] * 0.05 * x, VIDEO_SIZE[1] * 1.3 * x * x,
                    1.0, (u3 * 2 - 1) * 70 * x, 1.0)
        x = min(max((te - u1 * out_f * 0.3) / max(out_f * 0.7, 1.0), 0.0), 1.0)
        e = x * x
        ang = u2 * math.tau
        dist = size * 1.6 * (0.3 + 0.7 * u3)
        return (math.cos(ang) * dist * e, math.sin(ang) * dist * e - size * 0.3 * e,
                1 - 0.35 * e, 0.0, 1 - e * e)

    def _exit_fade(self, tl, dur):
        """下敷き・重ね物を fall / drift に合わせて消すための不透明度（1 = そのまま）"""
        if self.cut.get("exit") not in ("fall", "drift"):
            return 1.0
        out_f = self._exit_frames(dur)
        te = out_f - (dur - tl) * FPS
        return 1.0 if te <= 0 else max(1 - te / max(out_f * 0.6, 1.0), 0.0)

    def _shadow_img(self, g):
        im = self._shadows.get(g["key"])
        if im is None:
            im = Image.new("RGBA", g["img"].size, _hex(self.shadow_color) + (255,))
            im.putalpha(g["img"].getchannel("A"))
            self._shadows[g["key"]] = im
        return im

    def _draw_long_shadow(self, frame, tl, dur, sprites, cos_a, sin_a, total_angle_of):
        """長い影: 全部の文字の影を右下へ段状に伸ばしてから、文字本体を上に描く。
        影を先に全字分描くので、隣の文字の上に影がかぶらない"""
        cut = self.cut
        ax, ay = self.anchor
        for g in self.glyphs:
            dx, dy, scale, angle, alpha, crop_top, crop_right = self.glyph_state(g, tl, dur)
            if alpha <= 0.05 or cut["entrance"] == "mask":
                continue
            size = g["img"].size[1]
            step = max(size * 0.045, 4) * scale
            key = ("shadow",) + tuple(g["key"]) if isinstance(g["key"], tuple) else ("shadow", g["key"])
            im = sprites.transformed(key, self._shadow_img(g), scale, total_angle_of(g, angle), alpha * 0.9,
                                     crop_top, crop_right)
            if im is None:
                continue
            gx = g["cx"] * scale
            gy = g["cy"] * scale
            rx = gx * cos_a - gy * sin_a
            ry = gx * sin_a + gy * cos_a
            px = ax + rx + dx - im.size[0] / 2
            py = ay + ry + dy - im.size[1] / 2
            for k in range(6, 0, -1):
                frame.paste(im, (int(px + step * k), int(py + step * k)), im)

    def draw(self, frame, t, sprites, cam=(1.0, 0.0, 0.0, 0.0)):
        cut = self.cut
        tl = t - cut["start"]
        dur = cut["end"] - cut["start"]
        if tl < -0.2 or tl > dur:
            return
        koma = cut.get("koma") or 0
        if koma:
            # コマ打ち: 文字の時間だけを 1/koma 秒の刻みに落とす（背景とカメラは滑らかなまま）
            tl = math.floor(tl * koma + 1e-6) / koma
        # 背景の巨大文字: ゆっくり流れ、背景カメラと逆向きに大きく動く（単色背景でもカメラが感じられる）
        _zoom, px, py, _angle = cam
        show_echo = (cut.get("decor") not in ("wall", "tunnel", "kanji", "rings")
                     and not cut.get("profile_kids"))
        ex = int(VIDEO_SIZE[0] / 2 - self.echo.size[0] / 2
                 + (40 - 80 * tl / max(dur, 0.1)) * (1 if cut["index"] % 2 else -1) - px * 260)
        ey = int(VIDEO_SIZE[1] * (0.22 if cut["index"] % 2 else 0.78) - self.echo.size[1] / 2 - py * 360)
        if show_echo:
            frame.paste(self.echo, (ex, ey), self.echo)

        ax, ay = self.anchor
        if self.under is not None:
            self._draw_under(frame, tl, dur)
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

        if cut.get("texture") == "long_shadow":
            self._draw_long_shadow(frame, tl, dur, sprites, cos_a, sin_a,
                                   lambda g, angle: base_angle + angle + g["angle"])

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
                for (mkey, mimg), (ox, oy) in zip(g.get("mis", []), ((9, 6), (-7, -5))):
                    mim = sprites.transformed(mkey, mimg, scale, total_angle, a * 0.9, crop_top, crop_right)
                    if mim is not None:
                        frame.paste(mim, (int(ax + rx + gdx - mim.size[0] / 2 + ox),
                                          int(ay + ry + gdy - mim.size[1] / 2 + oy)), mim)
                im = sprites.transformed(g["key"], g["img"], scale, total_angle, a, crop_top, crop_right)
                if im is None:
                    continue
                px = ax + rx + gdx - im.size[0] / 2
                py = ay + ry + gdy - im.size[1] / 2
                remain = (dur - tl) * FPS
                if cut.get("exit") == "split" and remain < 8:
                    q = 1 - max(remain, 0) / 8
                    half = im.size[1] // 2
                    top, bottom = im.crop((0, 0, im.size[0], half)), im.crop((0, half, im.size[0], im.size[1]))
                    frame.paste(top, (int(px - 70 * q), int(py - 50 * q)), top)
                    frame.paste(bottom, (int(px + 70 * q), int(py + half + 50 * q)), bottom)
                    continue
                if cut.get("exit") == "shatter" and remain < 10:
                    # 1字を4片に割り、片ごとに違う向きへ飛ばして落とす
                    q = 1 - max(remain, 0) / 10
                    w_, h_ = im.size
                    mx, my = w_ // 2, h_ // 2
                    for k, (x0, y0, x1, y1, sx, sy) in enumerate((
                            (0, 0, mx, my, -1, -1), (mx, 0, w_, my, 1, -1),
                            (0, my, mx, h_, -1, 1), (mx, my, w_, h_, 1, 1))):
                        if x1 <= x0 or y1 <= y0:
                            continue
                        piece = im.crop((x0, y0, x1, y1))
                        j = kinetic_fx._hash01(g["order"] * 4 + k + cut["index"])
                        ox = sx * (30 + 150 * j) * q * q
                        oy = sy * (20 + 90 * (1 - j)) * q * q + 150 * q * q * q
                        frame.paste(piece, (int(px + x0 + ox), int(py + y0 + oy)), piece)
                    continue
                frame.paste(im, (int(px), int(py)), im)

        if self.overlay is not None:
            self._draw_overlay(frame, tl, dur)
        self._draw_slash(frame, tl)
        if cut.get("exit") == "split":
            remain = (dur - tl) * FPS
            if 5 < remain < 8:
                d = ImageDraw.Draw(frame)
                d.line([(-50, ay + 60), (VIDEO_SIZE[0] + 50, ay - 60)], fill=(255, 255, 255), width=8)

    def _draw_under(self, frame, tl, dur):
        cut = self.cut
        f = tl * FPS
        remain = (dur - tl) * FPS
        if f < 0:
            return
        im = self.under
        if cut["under"] == "brush":
            reveal = min(f / 7, 1.0)
            if reveal <= 0:
                return
            im = im.crop((0, 0, max(int(im.width * _ease_out(reveal)), 1), im.height))
            angle = -2
        else:
            angle = self.base_angle
        a = 1.0
        if remain < EXIT_FRAMES:
            a = max(remain, 0) / EXIT_FRAMES
        if cut.get("exit") == "fly" and remain < 7:
            a *= (max(remain, 0) / 7)
        a *= self._exit_fade(tl, dur)
        key = ("under", cut["index"], im.size)
        im = self._shared.transformed(key, im, 1.0, angle, a, 0.0, 0.0)
        if im is None:
            return
        ax, ay = self.anchor
        x0, y0, x1, y1 = self._bounds()
        full_w = self.under.width
        cx = ax + (x0 + x1) / 2
        cy = ay + (y0 + y1) / 2
        left = cx - full_w / 2
        frame.paste(im, (int(left), int(cy - im.height / 2)), im)

    def _draw_overlay(self, frame, tl, dur):
        cut = self.cut
        f = tl * FPS
        remain = (dur - tl) * FPS
        if cut["layout"] == "grid":
            a = min(f / 4, 1.0)
            scale = 1.0
        else:
            p = f - ((cut["land"] - cut["start"]) * FPS - 3)
            if p < 3:
                return
            a = 1.0
            scale = 1.0 if p >= ENTRANCE_FRAMES["stamp"] else _lerp(0.9, 1.0, (p - 3) / 3)
        if remain < EXIT_FRAMES:
            a *= max(remain, 0) / EXIT_FRAMES
        if cut.get("exit") == "fly" and remain < 7:
            q = 1 - max(remain, 0) / 7
            scale *= 1 + 3.0 * q * q
            a *= 1 - q * q
        a *= self._exit_fade(tl, dur)
        if a <= 0.02:
            return
        key = ("overlay", cut["index"], id(self.overlay))
        im = self._shared.transformed(key, self.overlay, scale, self.base_angle, a, 0.0, 0.0)
        if im is None:
            return
        ax, ay = self.anchor
        x0, y0, x1, y1 = self._bounds()
        cx = ax + (x0 + x1) / 2 * scale if cut["layout"] != "grid" else ax
        cy = ay + (y0 + y1) / 2 * scale if cut["layout"] != "grid" else ay
        frame.paste(im, (int(cx - im.size[0] / 2), int(cy - im.size[1] / 2)), im)

    def _draw_slash(self, frame, tl):
        cut = self.cut
        ax, ay = self.anchor
        if cut["entrance"] == "slash":
            p = (tl - (cut["land"] - cut["start"])) * FPS
            if 0 <= p <= 6:
                d = ImageDraw.Draw(frame)
                q = p / 6
                x0 = -200 + q * 1480
                d.line([(x0 - 700, ay + 420), (x0, ay - 420)], fill=(255, 255, 255), width=10)


# ---------------------------------------------------------------------------
# 背景

_SPRITES = None
_DECOR = None
_COVERS = {}


def _shared_sprites():
    """文字画像のキャッシュはレンダラーを作り直しても使い回す（GUIのプレビュー用）。"""
    global _SPRITES
    if _SPRITES is None:
        _SPRITES = _Sprites(_Fonts())
    return _SPRITES


def _shared_decor():
    global _DECOR
    if _DECOR is None:
        _DECOR = kinetic_fx.Decor(_shared_sprites().fonts)
    return _DECOR


VIDEO_EXTS = (".mp4", ".mov", ".webm", ".m4v")


def _make_cover(img, brightness=0.55):
    W, H = VIDEO_SIZE
    s = max(W / img.width, H / img.height)
    img = img.resize((int(img.width * s) + 1, int(img.height * s) + 1), Image.LANCZOS)
    return ImageEnhance.Brightness(img).enhance(brightness)


class _ImageSource:
    def __init__(self, path, brightness=0.55):
        path = Path(path)
        key = (str(path), path.stat().st_mtime, brightness)
        cover = _COVERS.get(key)
        if cover is None:
            cover = _make_cover(Image.open(path).convert("RGB"), brightness)
            if len(_COVERS) > 16:
                _COVERS.clear()
            _COVERS[key] = cover
        self.cover = cover

    def cover_at(self, t):
        return self.cover


class _VideoSource:
    """動画の背景。曲の時刻に合わせて繰り返し再生する。"""

    def __init__(self, path, brightness=0.6):
        from moviepy import VideoFileClip

        self.clip = VideoFileClip(str(path), audio=False)
        self.duration = max(self.clip.duration, 0.1)
        self._last = (None, None)
        self.brightness = brightness

    def cover_at(self, t):
        fi = int((t % self.duration) * FPS)
        if self._last[0] == fi:
            return self._last[1]
        arr = self.clip.get_frame(min(fi / FPS, self.duration - 0.001))
        img = Image.fromarray(arr)
        W, H = VIDEO_SIZE
        s = max(W / img.width, H / img.height) * 1.12
        img = img.resize((int(img.width * s) + 1, int(img.height * s) + 1), Image.BILINEAR)
        cover = ImageEnhance.Brightness(img).enhance(self.brightness)
        self._last = (fi, cover)
        return cover


def _thin_beats(beats, min_gap):
    """min_gap 秒より近い拍を間引く。背景の弾みを落ち着かせるために使う。"""
    out = []
    for b in beats or []:
        if not out or b - out[-1] >= min_gap:
            out.append(b)
    return out


class _Background:
    """背景画像は画面を覆う大きさ（縦長画面に横長画像なら横に余りが出る）で
    保持し、カメラの窓（寄り・パン位置・傾き）で切り出す。
    複数の背景（画像・動画）を持ち、カットごとに bg_image で選ぶ。"""

    def __init__(self, image_path, beats, style, brightness=0.55, calm=False):
        self.main = str(image_path)
        self._sources = {}
        self.brightness = brightness
        self.cover = self._source(self.main).cover_at(0)
        self.beats = beats or []
        self.pulse_scale = style.get("pulse_scale", 1.08)
        self.pulse_decay = style.get("pulse_decay_sec", 0.14)
        if calm:
            # 背景が拍のたびに弾むと、文字を追う目が休まらない。子ども向けの曲は
            # 拍が細かい（1秒に2つ前後）ので、弾みを弱めたうえで間引き、
            # 「ときどき息をする」程度にする。
            self.pulse_scale = 1.0 + (self.pulse_scale - 1.0) * 0.28
            self.pulse_decay *= 0.7
            self.beats = _thin_beats(self.beats, 0.75)
        self._solid = {}
        self.wa = False
        self.palette = DEFAULT_PALETTE

    def _source(self, path):
        path = str(path or self.main)
        src = self._sources.get(path)
        if src is None:
            if not Path(path).exists():
                path = self.main
                src = self._sources.get(path)
                if src is not None:
                    return src
            if Path(path).suffix.lower() in VIDEO_EXTS:
                src = _VideoSource(path, self.brightness + 0.05)
            else:
                src = _ImageSource(path, self.brightness)
            self._sources[path] = src
        return src

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

    def image_at(self, t, cam, path=None):
        W, H = VIDEO_SIZE
        cover = self._source(path).cover_at(t)
        zoom, px, py, angle = cam
        zoom *= self.pulse(t)
        th = math.radians(angle)
        # 傾けても画面外（黒）が見えない最小の寄り
        zoom = max(zoom, math.cos(th) + (W / H) * abs(math.sin(th)) + 0.01, 1.0)
        CW, CH = cover.size
        room_x = max((CW - W / zoom) / 2, 0)
        room_y = max((CH - H / zoom) / 2, 0)
        cx = CW / 2 + px * room_x
        cy = CH / 2 + py * room_y
        cos_t, sin_t = math.cos(th), math.sin(th)
        a, b = cos_t / zoom, -sin_t / zoom
        d, e = sin_t / zoom, cos_t / zoom
        c = cx - a * W / 2 - b * H / 2
        f = cy - d * W / 2 - e * H / 2
        return cover.transform(VIDEO_SIZE, Image.AFFINE, (a, b, c, d, e, f), resample=Image.BILINEAR)

    def solid(self, color):
        im = self._solid.get(color)
        if im is None:
            im = kinetic_bg.apply_paper(Image.new("RGB", VIDEO_SIZE, _hex(color)), self.wa)
            self._solid[color] = im
        return im

    def frame(self, mode, t, cam, cut=None, image=None):
        """cut（その時点のカット設計）の bgfx / bg_image があれば背景に反映する。"""
        bgfx = cut.get("bgfx") if cut else None
        if mode == "image":
            im = self.image_at(t, cam, image or (cut.get("bg_image") if cut else None))
            if bgfx == "duotone":
                accent = self.palette[cut["index"] % len(self.palette)][0]
                im = kinetic_bg.apply_bgfx(im, bgfx, t, "#0C0C0F", accent, self.beat_amt(t), cut["index"])
            return im
        color = self.palette[mode][0]
        if bgfx and bgfx.startswith("pattern:"):
            im = kinetic_bg.apply_bgfx(Image.new("RGB", VIDEO_SIZE, _hex(color)), bgfx, t, color,
                                       self.palette[mode][3], self.beat_amt(t), cut.get("shot", 0))
            return kinetic_bg.apply_paper(im, self.wa)
        return self.solid(color).copy()

    def beat_amt(self, t):
        idx = bisect.bisect_right(self.beats, t) - 1
        if idx < 0:
            return 0.0
        return math.exp(-(t - self.beats[idx]) / 0.12)


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
    if name == "kaleido_soft":
        return kinetic_bg.kaleidoscope(frame, t, strength)
    if name == "sparkle":
        return kinetic_bg.sparkle(frame, t, strength, beat_amt, palette)
    if name == "kaleido":
        frame = kinetic_bg.kaleidoscope(frame, t, strength)
        name = "rgb_split"
        strength *= 0.5
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
    def __init__(self, image_path, plan, beats, style, duration=None, backgrounds=None):
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
        kids = any(c.get("profile_kids") for c in plan)
        self.bg = _Background(image_path, beats, style,
                              brightness=0.92 if kids else 0.55, calm=kids)
        self.bg.wa = any(c.get("profile_wa") for c in plan)
        self.kids = any(c.get("profile_kids") for c in plan)
        self.palette = palette_for(plan)
        self.bg.palette = self.palette
        self.sprites = _shared_sprites()
        self.duration = duration
        self.interludes = find_interludes(plan, duration)
        self.interlude_starts = [s for s, _e, _n in self.interludes]
        self.interlude_images = kinetic_bg.interlude_images(backgrounds, len(self.interludes))
        self.cuts = []
        for c in plan:
            if c["bg"] == "image":
                if self.kids:
                    colors = ("#3A2A5A", "#FFFFFF", "#FF4F9A")
                else:
                    colors = ("#FFFFFF", "#0B0B0F", style.get("caption_color", "#FF3B70"))
                palette_bg = None
            else:
                bg, text, stroke, accent = self.palette[c["bg"]]
                colors = (text, stroke, accent)
                palette_bg = bg
            self.cuts.append(_Cut(c, self.sprites, colors, palette_bg, beats))
        self.decor = _shared_decor()

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
            amp = (4 if self.kids else 14) * math.exp(-since_land / 0.08)
            sx = amp * math.sin(t * 90)
            sy = amp * math.cos(t * 77)
            g_zoom += 0.012 * (self.bg.pulse(t) - 1.0) / max(self.bg.pulse_scale - 1.0, 1e-3)
        elif c["level"] == 1:
            g_zoom = 1.0 + 0.015 * uc
        return bg_cam, g_zoom, sx, sy

    def frame_at(self, t):
        bg_cam, g_zoom, sx, sy = self.camera_at(t)
        mode, prev, p = self._bg_mode_at(t)
        kc = bisect.bisect_right(self.starts, t) - 1
        cur_cut = self.plan[kc] if 0 <= kc < len(self.plan) and mode == self.plan[kc]["bg"] else None
        ki = bisect.bisect_right(self.interlude_starts, t) - 1
        in_interlude = ki >= 0 and self.interludes[ki][0] <= t <= self.interludes[ki][1] and mode == "image" and prev is None
        if in_interlude:
            frame = self.bg.frame(mode, t, bg_cam, None, self.interlude_images[ki])
        else:
            frame = self.bg.frame(mode, t, bg_cam, cur_cut)
        k = bisect.bisect_right(self.interlude_starts, t) - 1
        if k >= 0 and mode == "image" and prev is None:
            s0, e0, effect = self.interludes[k]
            if s0 <= t <= e0:
                strength = min((t - s0) / INTERLUDE_FADE, (e0 - t) / INTERLUDE_FADE, 1.0)
                bi = bisect.bisect_right(self.bg.beats, t) - 1
                beat_amt = 0.0
                if bi >= 0:
                    beat_amt = math.exp(-(t - self.bg.beats[bi]) / 0.12)
                pal = self.palette
                if self.kids:
                    effect = ("sparkle", "kaleido_soft", "duotone")[k % 3]
                frame = apply_interlude_effect(frame, effect, strength, t, bi, beat_amt,
                                               (pal[0][0], pal[0][3]) if k % 2 else (pal[1][0], pal[2][0]))
        if prev is not None:
            prev_cut = self.plan[kc - 1] if kc > 0 else None
            old = self.bg.frame(prev, t, bg_cam, prev_cut if prev_cut and prev_cut["bg"] == prev else None)
            kind = cur_cut.get("wipe", "straight") if cur_cut else "straight"
            mask = kinetic_bg.wipe_mask(kind, _ease_out(p), kc)
            frame = Image.composite(frame, old, mask)
        active = self._active(t)
        for j in active:
            text_color, _stroke, accent = self.cuts[j].colors
            self.decor.draw(frame, self.plan[j], t, _hex(text_color), _hex(accent), bg_cam)
        grain = 0.0
        for j in active:
            self.cuts[j].draw(frame, t, self.sprites, bg_cam)
            c = self.plan[j]
            if c.get("texture") == "grain":
                tl = t - c["start"]
                grain = max(grain, min(max(tl + 0.2, 0) / 0.3, (c["end"] - t) / 0.3 + 1, 1.0))
            if c["flash"]:
                df = (t - c["land"]) * FPS
                if 0 <= df < 2:
                    white = Image.new("RGB", VIDEO_SIZE, (255, 255, 255))
                    frame = Image.blend(frame, white, 0.7 if df < 1 else 0.3)
        if grain > 0:
            frame = kinetic_fx.apply_grain(frame, t, grain)
        if g_zoom > 1.0005 or sx or sy:
            W, H = VIDEO_SIZE
            z = max(g_zoom, 1.0 + 2 * max(abs(sx), abs(sy)) / W)
            a = 1 / z
            c0 = W / 2 - a * W / 2 - sx / z
            f0 = H / 2 - a * H / 2 - sy / z
            frame = frame.transform(VIDEO_SIZE, Image.AFFINE, (a, 0, c0, 0, a, f0), resample=Image.BILINEAR)
        return frame


def render_kinetic(image_path, audio_path, plan, beats, style, output_path, progress=None,
                   t_start=None, t_end=None, backgrounds=None):
    """t_start / t_end を渡すと、その区間だけを書き出す（音声も同じ区間）。"""
    from moviepy import AudioFileClip, VideoClip

    audio = AudioFileClip(str(audio_path))
    # subclipped の後は audio.duration が切り出した長さになるので、曲全体の長さは先に控える
    song_duration = audio.duration
    renderer = KineticRenderer(image_path, plan, beats, style, duration=song_duration, backgrounds=backgrounds)
    t0 = max(float(t_start or 0.0), 0.0)
    t1 = min(float(t_end), song_duration) if t_end is not None else song_duration
    if t1 - t0 < 0.1:
        raise ValueError(f"書き出す区間が短すぎます: {t0:.2f}〜{t1:.2f}秒")
    if t0 > 0 or t1 < song_duration:
        audio = audio.subclipped(t0, t1)
    total = t1 - t0

    # 途中で切り出したものは、頭と尻が唐突に始まって唐突に終わる。
    # 短い出入りを付けて、曲の途中から切ったことが分かるようにする。
    fade_in = 0.25 if t0 > 0 else 0.0
    fade_out = min(0.8, total * 0.25) if t1 < song_duration - 0.05 else 0.0
    if fade_in or fade_out:
        from moviepy.audio.fx import AudioFadeIn, AudioFadeOut
        fx = []
        if fade_in:
            fx.append(AudioFadeIn(fade_in))
        if fade_out:
            fx.append(AudioFadeOut(fade_out))
        audio = audio.with_effects(fx)

    def frame(t):
        if progress:
            progress(min(t / total, 0.99))
        im = np.asarray(renderer.frame_at(t0 + t))
        # 画は音より短く暗転させる（切れ際だけ。頭は暗転させない）
        if fade_out and t > total - fade_out * 0.6:
            k = max(0.0, (total - t) / (fade_out * 0.6))
            im = (im * k).astype(np.uint8)
        return im

    clip = VideoClip(frame_function=frame, duration=total).with_audio(audio)
    output_path = Path(output_path)
    output_path.parent.mkdir(parents=True, exist_ok=True)
    clip.write_videofile(
        str(output_path), fps=FPS, codec="libx264", audio_codec="aac",
        preset="medium", logger=None,
    )
    return output_path


def render_stills(image_path, plan, beats, style, out_dir, cuts_per_sheet=8, backgrounds=None):
    """各カットの 0/25/50/75/100% と入りの着地直後を静止画にし、
    一覧画像（コンタクトシート）にまとめる。書き出し前の目視確認用。"""
    renderer = KineticRenderer(image_path, plan, beats, style, backgrounds=backgrounds)
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


PLAN_VERSION = 13


def load_or_build_plan(plan_path, alignment, sections, beats, style, replan=False, meta=None, backgrounds=None):
    plan_path = Path(plan_path)
    lines = [a["line"] for a in alignment]
    if plan_path.exists() and not replan:
        saved = json.loads(plan_path.read_text(encoding="utf-8"))
        if ([c["text"] for c in saved.get("plan", [])] == lines and saved.get("alignment") == alignment
                and saved.get("version") == PLAN_VERSION and saved.get("meta") == meta
                and saved.get("backgrounds") == (backgrounds or [])):
            return saved["plan"]
        print("      kinetic_plan.json は歌詞・タイミング・曲情報・設計の版のいずれかが変わったため作り直します")
    plan = build_plan(alignment, sections, beats, style, meta, backgrounds)
    plan_path.parent.mkdir(parents=True, exist_ok=True)
    plan_path.write_text(
        json.dumps({"version": PLAN_VERSION, "meta": meta, "backgrounds": backgrounds or [],
                    "alignment": alignment, "plan": plan},
                   ensure_ascii=False, indent=1),
        encoding="utf-8",
    )
    plan_path.with_suffix(".md").write_text(plan_to_markdown(plan), encoding="utf-8")
    return plan
