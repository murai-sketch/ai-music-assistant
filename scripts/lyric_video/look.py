#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
look.py
曲ごとの「見た目」（テーマ）と「演出」（direction）を読み込む。

  テーマ      scripts/lyric_video/looks/<名前>.json （Git に入る。曲を特定しない書体・配色の組だけ）
  演出        _work/<音源ハッシュ>/direction.json   （Git に入らない。行番号で書く。歌詞の本文は持たない）

コードは声の名前（「外」「二人」など）を持たない。声は任意の文字列で、テーマ／演出の側で付ける。

テーマの形（足りない項目は止める。黙って既定値に戻さない）:
  {
    "look": "<名前>",
    "fonts": {"<役>": {"family": "...", "style": "...", "index": 0,
                        "fallback_path": "任意", "tracking": 0.0, "leading": 1.2,
                        "min_px": 72, "max_px": 150, "thicken": {...}, "layer_outline": {...}}},
    "palettes": {"<名前>": {"bg": "#RRGGBB", "text": "#RRGGBB", ...}},   # 名前 "image" は使えない
    "voices": {"<声>": {"role": "<役>", "tail": 0.3}},                     # 任意
    "texture": {...},                                                       # 任意
    "points_color": "text"|"sub"|"accent"                                   # 任意。点の層の色（全部の配色にそのキーが要る。既定 text）
  }
  役の中身: tracking（字間 em）・leading（行送り ×サイズ）・min_px／max_px（下限を割ったら改行を増やす）・
            thicken {below_px, px}（以下の大きさで同色の縁で太らせる）・layer_outline {px}（層が重なるときだけ背景色の縁）
  palettes: bg・text（必須）、stroke・sub・accent（任意）。名前は自由（"image" は使えない）
  voices.<声>: role・palette・tail。default_role／default_palette: direction 無し（--look だけ）のときの既定
  layout.text_width: 文字を収める横幅 px（左右の余白 92px なら 896）。texture: paper（plain / none）・bg_image（false のみ。単色背景）

演出（direction.json）の形:
  {
    "look": "<テーマ名>",            # 任意
    "n_lines": 40,                   # 必須。alignment の行数と違ったら止める
    "voices": {"<声>": {"tail": 0.3}},   # 任意。テーマの voices を上書き
    "lines": {"1": {...}, "5-8": {...}}  # 行番号（1始まり）または範囲。同じ行に複数当たれば後勝ち
  }
  行の項目: voice / tail（秒）/ end（絶対時刻で固定）/ exit（swap・fade・fade:<秒>・fall 等）/ entrance / layout / hold / decor /
            role（書体の役）/ palette（配色の名前）/ accent（none・key_word・fill・outline・glow・rows）/ accent_rows（rows のとき差し色にする段）/ row_roles（縦組みの行だけ。段ごとの書体の役）/ row_lengths（横組みの行だけ。段の字数＝全角スペースを除いた字数。全角スペースは段の中なら 0.5字の空き・切れ目なら無し。半角スペースなど・欧文の行には書けない）/ clear_cap（読み字の周りの点の被覆の上限 0.05〜0.25。既定 0.25）/ align_to_prev（true だけ。この行の先頭字の中心 x を前の行の先頭字の x にそろえる。両方が横組み・1段・center・傾いていない・同じ大きさのときだけ。そろうのは静止位置だけ：入りの動き・slam の寄りの瞬間・前の行が動く場合は対象外）/ max_px / tracking（役の値の行ごとの上書き）
  key_word: {"from_line": N, "rule": "first_bracket"}   N 行目の最初の「」の中の語を実行時に取り出す（歌詞は書かない）
  bg_transitions: [{"from": 配色, "to": 配色, "start"|"after_line", "seconds"|"until_line"}]   背景色を時間で線形に補間する

段3の項目（曲を特定しない形だけ。値は direction に書く）:
  行: impact（impacts の名前。entrance: slam の行）／ land（first_word|start。着地を最初の単語の開始に合わせる）／
      karaoke_land（真偽）／ counter（hide|resume|off、または {count, state, rate, enter, break}）／ solo（真偽）／
      break_after {"<段番号>": <字の位置>}（その字の後で切る。段は 0 始まり）／ min_px（その行だけの下限）
  最上位: impacts {名前: {overshoot, land_frames, undershoot, glyph_shake_px, zoom, screen_shake_px}}（全キー必須）
          slam_voice "<声>" または ["<声>", ...]（slam を使える声。slam の行があるとき必須）
          karaoke {unlit_opacity, light_frames, keyword_unlit, min_match, min_cover}
          counter {appear: [...]} ／ interludes [...]
  テーマ: palettes.<名前>.unlit_opacity（任意）／ parts.glow {dilate_px, blur_ratio, max_alpha} ／
          parts.counter {role, px, border_px, dim, ...}
"""

import hashlib
import json
import math
import re
import shutil
import subprocess
from collections import namedtuple
from datetime import datetime
from pathlib import Path

LOOKS_DIR = Path(__file__).resolve().parent / "looks"
DIRECTION_NAME = "direction.json"

# 書体の参照（パスと TTC の番号の組）。既存経路の文字列パスとは別の型にして、既存のキャッシュキーを変えない
FontRef = namedtuple("FontRef", ["path", "index"])

_HEX = re.compile(r"^#[0-9A-Fa-f]{6}$")


class LookError(RuntimeError):
    """テーマ・演出の不備。黙って既定値に戻さず、止める"""


# ---------------------------------------------------------------------------
# テーマ

def _theme_path(name_or_path):
    p = Path(str(name_or_path))
    if p.suffix == ".json" or p.parent != Path("."):
        return p
    return LOOKS_DIR / f"{name_or_path}.json"


def validate_theme(theme, where="テーマ"):
    if not isinstance(theme, dict):
        raise LookError(f"{where}: JSON のオブジェクトではありません")
    for key in ("fonts", "palettes"):
        if not isinstance(theme.get(key), dict) or not theme[key]:
            raise LookError(f"{where}: '{key}' がありません")
    for role, spec in theme["fonts"].items():
        if not isinstance(spec, dict):
            raise LookError(f"{where}: fonts.{role} がオブジェクトではありません")
        for key in ("family", "style", "index"):
            if key not in spec:
                raise LookError(f"{where}: fonts.{role}.{key} がありません")
        if not isinstance(spec["index"], int):
            raise LookError(f"{where}: fonts.{role}.index は整数で書いてください")
    for name, pal in theme["palettes"].items():
        if name == "image":
            raise LookError(f"{where}: 配色の名前に 'image' は使えません（背景画像の指定と衝突します）")
        if not isinstance(pal, dict):
            raise LookError(f"{where}: palettes.{name} がオブジェクトではありません")
        for key in ("bg", "text"):
            if key not in pal:
                raise LookError(f"{where}: palettes.{name}.{key} がありません")
        for key, val in pal.items():
            if key == "unlit_opacity":
                if isinstance(val, bool) or not isinstance(val, (int, float)) or not 0 < val <= 1:
                    raise LookError(f"{where}: palettes.{name}.unlit_opacity は 0 より大きく 1 以下の数値で書いてください")
                continue
            if not (isinstance(val, str) and _HEX.match(val)):
                raise LookError(f"{where}: palettes.{name}.{key} が #RRGGBB ではありません")
    for role, spec in theme["fonts"].items():
        for key in ("tracking", "leading", "min_px", "max_px"):
            if key in spec and (isinstance(spec[key], bool) or not isinstance(spec[key], (int, float))):
                raise LookError(f"{where}: fonts.{role}.{key} は数値で書いてください")
        for key in ("thicken", "layer_outline"):
            sub = spec.get(key)
            if sub is not None and (not isinstance(sub, dict) or not isinstance(sub.get("px"), int)
                                    or (key == "thicken" and not isinstance(sub.get("below_px"), (int, float)))):
                raise LookError(f"{where}: fonts.{role}.{key} は {{px: 整数{', below_px: 数値' if key == 'thicken' else ''}}} で書いてください")
        if "min_px" in spec and "max_px" in spec and spec["min_px"] > spec["max_px"]:
            raise LookError(f"{where}: fonts.{role} の min_px が max_px より大きいです")
    for voice, spec in (theme.get("voices") or {}).items():
        role = (spec or {}).get("role")
        if role is not None and role not in theme["fonts"]:
            raise LookError(f"{where}: voices.{voice}.role '{role}' が fonts にありません")
        pal = (spec or {}).get("palette")
        if pal is not None and pal not in theme["palettes"]:
            raise LookError(f"{where}: voices.{voice}.palette '{pal}' が palettes にありません")
    if theme.get("default_role") is not None and theme["default_role"] not in theme["fonts"]:
        raise LookError(f"{where}: default_role '{theme['default_role']}' が fonts にありません")
    if theme.get("default_palette") is not None and theme["default_palette"] not in theme["palettes"]:
        raise LookError(f"{where}: default_palette '{theme['default_palette']}' が palettes にありません")
    if "points_color" in theme:
        pc = theme["points_color"]
        if not isinstance(pc, str) or pc not in ("text", "sub", "accent"):
            raise LookError(f"{where}: points_color '{pc}' は text・sub・accent のどれかで書いてください")
        miss = [n for n, pal in theme["palettes"].items() if pc not in pal]
        if miss:
            raise LookError(f"{where}: points_color '{pc}' の色が、配色 {', '.join(miss)} にありません（全部の配色に必要）")
    tex = theme.get("texture") or {}
    for key, val in tex.items():
        if key == "paper" and val in ("plain", "none"):
            continue
        if key == "bg_image" and val is False:
            continue
        # 版ズレ・粒子とひっかき傷・長い影・模様・背景画像は、この版のテーマでは使えない（黙って無視しない）
        if val in (False, None, [], "none", "off"):
            continue
        raise LookError(f"{where}: texture.{key}={val!r} は未対応です（paper: plain|none、bg_image: false のみ）")
    parts = theme.get("parts") or {}
    if not isinstance(parts, dict) or not set(parts) <= {"glow", "counter"}:
        raise LookError(f"{where}: parts は glow・counter だけ書けます")
    glow = parts.get("glow")
    if glow is not None:
        if not isinstance(glow, dict) or set(glow) != {"dilate_px", "blur_ratio", "max_alpha"}:
            raise LookError(f"{where}: parts.glow は dilate_px・blur_ratio・max_alpha の全部を書いてください")
        if (isinstance(glow["dilate_px"], bool) or not isinstance(glow["dilate_px"], int) or glow["dilate_px"] < 0
                or not isinstance(glow["blur_ratio"], (int, float)) or glow["blur_ratio"] <= 0
                or not isinstance(glow["max_alpha"], (int, float)) or not 0 < glow["max_alpha"] <= 1):
            raise LookError(f"{where}: parts.glow の値が不正です（dilate_px 整数、blur_ratio 正の数、max_alpha 0〜1）")
    counter = parts.get("counter")
    if counter is not None:
        if not isinstance(counter, dict) or counter.get("role") not in theme["fonts"]:
            raise LookError(f"{where}: parts.counter.role が fonts にありません")
        for key in ("px", "border_px", "dim"):
            if key not in counter or isinstance(counter[key], bool) or not isinstance(counter[key], (int, float)):
                raise LookError(f"{where}: parts.counter.{key} は数値で書いてください")
        if not 0 < counter["dim"] <= 1:
            raise LookError(f"{where}: parts.counter.dim は 0 より大きく 1 以下で書いてください")
        extra = set(counter) - {"role", "px", "border_px", "dim", "palette", "slots", "radius", "pad_x", "pad_y", "avoid_px", "min_y"}
        if extra:
            raise LookError(f"{where}: parts.counter に未知の項目 {', '.join(sorted(extra))} があります")
        if "palette" in counter and counter["palette"] not in theme["palettes"]:
            raise LookError(f"{where}: parts.counter.palette '{counter['palette']}' が palettes にありません")
        for key in ("radius", "pad_x", "pad_y", "avoid_px", "min_y"):
            if key in counter and (isinstance(counter[key], bool) or not isinstance(counter[key], (int, float)) or counter[key] < 0):
                raise LookError(f"{where}: parts.counter.{key} は 0 以上の数値で書いてください")
        slots = counter.get("slots")
        if slots is not None:
            if not isinstance(slots, list) or not slots:
                raise LookError(f"{where}: parts.counter.slots は配列で書いてください")
            for sl in slots:
                if (not isinstance(sl, dict) or set(sl) != {"corner", "x", "y"} or sl["corner"] not in ("tl", "tr")
                        or any(isinstance(sl[k], bool) or not isinstance(sl[k], (int, float)) for k in ("x", "y"))):
                    raise LookError(f"{where}: parts.counter.slots の要素は {{corner: tl|tr, x: 余白, y: 上からの位置}} で書いてください"
                                    f"（上側の左右の隅だけ。下側はショートの操作列に隠れる）")
    width = (theme.get("layout") or {}).get("text_width")
    if width is not None and (isinstance(width, bool) or not isinstance(width, (int, float)) or not 300 <= width <= 1080):
        raise LookError(f"{where}: layout.text_width は 300〜1080 の数値で書いてください")
    return theme


def load_theme(name_or_path):
    path = _theme_path(name_or_path)
    if not path.exists():
        raise LookError(f"テーマが見つかりません: {path}")
    theme = json.loads(path.read_text(encoding="utf-8"))
    return validate_theme(theme, where=f"テーマ {path.name}")


def _digest(obj):
    return hashlib.sha256(json.dumps(obj, ensure_ascii=False, sort_keys=True).encode("utf-8")).hexdigest()[:12]


def theme_digest(theme):
    return _digest(theme)


# ---------------------------------------------------------------------------
# 書体の解決

def _fc_match(family, style):
    """fontconfig に問い合わせる。(ファイル, 番号, 見つかった family 一覧, style 一覧) か、無ければ None"""
    exe = shutil.which("fc-match") or ("/opt/homebrew/bin/fc-match" if Path("/opt/homebrew/bin/fc-match").exists() else None)
    if not exe:
        return None
    try:
        out = subprocess.run(
            [exe, "-f", "%{file}\n%{index}\n%{family}\n%{style}", f"{family}:style={style}"],
            capture_output=True, text=True, timeout=20,
        ).stdout.split("\n")
    except (OSError, subprocess.SubprocessError):
        return None
    if len(out) < 4 or not out[0]:
        return None
    try:
        index = int(out[1]) & 0xFFFF   # 上位ビットは可変書体の指定。TTC の番号は下位16bit
    except ValueError:
        return None
    return out[0], index, [s.strip().lower() for s in out[2].split(",")], [s.strip().lower() for s in out[3].split(",")]


def resolve_font(spec):
    """役の指定（family・style・index・fallback_path）から FontRef を返す。
    fontconfig は「無い書体」にも別の書体を返すので、返った family が指定と違えば見つからなかったものとして扱う。
    fontconfig で見つからなければ fallback_path。どちらも無ければ止める（ヒラギノに黙って戻さない）。"""
    family, style = spec["family"], spec["style"]
    got = _fc_match(family, style)
    if got:
        path, fc_index, families, styles = got
        if family.lower() in families and style.lower() in styles and Path(path).exists():
            if fc_index != spec["index"]:
                print(f"      [警告] 書体 {family} {style}: fontconfig の番号は {fc_index}、指定は {spec['index']}。指定を優先します")
            return FontRef(path, spec["index"])
    fb = spec.get("fallback_path")
    if fb and Path(fb).exists():
        print(f"      [警告] 書体 {family} {style} を fontconfig で解決できず、fallback_path を使います")
        return FontRef(fb, spec["index"])
    raise LookError(f"書体 {family} {style} が見つかりません（fontconfig にも fallback_path にもありません）。"
                    f"ヒラギノには戻しません")


def resolve_theme_fonts(theme):
    """テーマの全役を解決する。{役: {family, style, path, index}}"""
    out = {}
    for role, spec in theme["fonts"].items():
        ref = resolve_font(spec)
        out[role] = {"family": spec["family"], "style": spec["style"], "path": ref.path, "index": ref.index}
    return out


# ---------------------------------------------------------------------------
# 曲の演出

def direction_path(cache_dir):
    return Path(cache_dir) / DIRECTION_NAME


# direction の行の項目（許可リスト）。別名は読み込み時に正式名へ直す（動き §2 R2 は余韻を tail_sec と書く）。
LINE_ITEM_KEYS = {"voice", "tail", "end", "exit", "entrance", "layout", "hold", "decor",
                  "role", "palette", "accent", "max_px", "tracking",
                  "impact", "land", "karaoke_land", "counter", "solo", "break_after", "min_px", "max_col_chars", "ink", "text_y",
                  "carry", "accent_rows", "row_roles", "row_lengths", "clear_cap", "karaoke_cap", "align_to_prev", "hidden", "step", "reveal", "bouten"}
ACCENT_MODES = ("none", "key_word", "fill", "outline", "glow", "rows")   # rows：accent_rows の段だけ差し色（T35 L1）
ITEM_ALIASES = {"tail_sec": "tail"}
VOICE_ITEM_KEYS = {"role", "tail", "palette"}
DIRECTION_TOP_KEYS = {"n_lines", "voices", "lines", "look", "key_word", "bg_transitions",
                      "impacts", "slam_voice", "karaoke", "counter", "interludes", "vertical", "points", "stack", "safe_area", "halftone"}
CARRY_DIRS = ("down", "up")          # carry（行全体を一定の速さで動かす保持）の向き
CARRY_KEYS = {"px_s", "dir"}
STACK_KEYS = {"lines", "dim", "clear_at_line"}   # stack（前の列を残して薄くする）
VERTICAL_KEYS = {"height", "top", "kana_shift"}   # kana_shift: 小書きの仮名を右上へ寄せる量（字の大きさの割合。既定 0＝寄せない）
import kinetic_vertical as _kv   # noqa: E402（定数だけ。kinetic_vertical は look を遅延 import するので循環しない）

VERTICAL_ENTRANCES = _kv.ENTRANCES   # 縦組みの行で使える入り（字ごとに動くもの。描画側 _Cut も同じ定数を見る）
LIGHT_BAD_ENTRANCES = ("neon",)                 # 明るい地で合わない部品（光が見えない）
LIGHT_BAD_DECOR = ("tape",)                     # 字が暗い色で固定
LIGHT_BAD_TEXTURES = ("misregister", "long_shadow")   # 色が要る
MAX_COL_CHARS_RANGE = (2, 16)
LAND_MODES = ("first_word", "start")
REVEAL_DIRS = ("up", "down")         # 入りの切り抜き（reveal）の向き：up＝字の枠の下端から持ち上がる、down＝上端から下りる
REVEAL_SEC = (0.1, 0.6)
STEP_LEVEL_NAMES = ("s", "m", "l")   # 踏み込みの段階（小・中・大。値は kinetic.STEP_LEVELS）
COUNTER_STATES = ("hide", "resume", "off")
COUNTER_KEYS = {"count", "state", "rate", "enter", "break"}
COUNTER_TOP_KEYS = {"appear", "voices"}
APPEAR_KEYS = {"at", "after_line", "until_line", "at_fraction", "count", "rate"}
INTERLUDE_KEYS = {"start", "after_line", "end", "until_line", "until", "kind", "zoom_peak", "zoom_ramp"}
INTERLUDE_KINDS = ("duotone",)
COUNTER_MIN_CONTRAST = 4.5   # カウンター（副要素）の下限。最悪の背景（背景+10。明るい地は −10）に対して（設計書 §4）
IMPACT_KEYS = ("overshoot", "land_frames", "undershoot", "glyph_shake_px", "zoom", "screen_shake_px")
IMPACT_OPTIONAL_KEYS = ("ease",)    # 任意。縮み方（linear が既定／quad）
IMPACT_EASES = ("linear", "quad")
KARAOKE_KEYS = {"unlit_opacity", "light_frames", "keyword_unlit", "min_match", "min_cover"}
SLAM_MAX_LINES = 8         # slam の行数の上限（動き §9-7 ③）
UNLIT_MIN_CONTRAST = 5.0   # karaoke の未点灯（bg+10 の最悪の背景。明るい地は −10）の下限（書体配色 §11.4）
GLOW_MIN_CONTRAST = 7.0    # glow を重ねた背景に対する文字の下限（書体配色 §11.3）


def _normalize_item(item, allowed, where):
    """項目の名前を許可リストで検査し、別名を正式名に直す。未知のキー・別名と正式名の重複・数値でない値は止める"""
    if not isinstance(item, dict):
        raise LookError(f"direction: {where} はオブジェクトで書いてください")
    out = {}
    for k, v in item.items():
        name = ITEM_ALIASES.get(k, k)
        if name not in allowed:
            raise LookError(f"direction: {where} に未知の項目 '{k}' があります（使える項目: {', '.join(sorted(allowed))}）。"
                            f"黙って既定値に戻さず止めました")
        if name in out:
            raise LookError(f"direction: {where} の項目 '{name}' が別名と重複しています")
        if name in ("tail", "end") and (isinstance(v, bool) or not isinstance(v, (int, float)) or v < 0):
            raise LookError(f"direction: {where} の '{k}' は 0 以上の数値で書いてください")
        if name in ("voice", "exit", "entrance", "layout", "hold", "decor", "role", "palette", "accent", "impact") and not isinstance(v, str):
            raise LookError(f"direction: {where} の '{k}' は文字列で書いてください")
        if name == "max_col_chars" and (isinstance(v, bool) or not isinstance(v, int)
                                        or not MAX_COL_CHARS_RANGE[0] <= v <= MAX_COL_CHARS_RANGE[1]):
            raise LookError(f"direction: {where} の max_col_chars は {MAX_COL_CHARS_RANGE[0]}〜{MAX_COL_CHARS_RANGE[1]} の整数で書いてください")
        if name in ("max_px", "tracking", "min_px") and (isinstance(v, bool) or not isinstance(v, (int, float))
                                                         or (name in ("max_px", "min_px") and v <= 0)):
            raise LookError(f"direction: {where} の '{k}' は数値で書いてください")
        if name == "land" and v not in LAND_MODES:
            raise LookError(f"direction: {where} の land '{v}' は {', '.join(LAND_MODES)} のどれかで書いてください")
        if name == "hidden" and v is not True:
            raise LookError(f"direction: {where} の hidden は true だけ書けます（字幕に出さない行。false・数値は書けません。出す行には hidden を書かない）")
        if name in ("karaoke_land", "solo") and not isinstance(v, bool):
            raise LookError(f"direction: {where} の '{k}' は true / false で書いてください")
        if name == "text_y" and (isinstance(v, bool) or not isinstance(v, (int, float)) or not 0.15 <= v <= 0.85):
            raise LookError(f"direction: {where} の text_y は 0.15〜0.85（画面の高さに対する文字の中心位置）で書いてください")
        if name == "ink":
            import kinetic_points   # 遅延 import（kinetic_points は look を遅延 import する）

            kinetic_points.check_ink(v, where, read=True)
        if name == "break_after":
            _check_break_after(v, where)
        if name == "carry":
            _check_carry_item(v, where)
        if name == "karaoke_cap" and v != "word":
            raise LookError(f"direction: {where} の karaoke_cap は \"word\"（点灯の上限を、声の終わりでなく最後に対応した単語の終わりにする）だけです")
        if name == "step" and v not in STEP_LEVEL_NAMES:
            raise LookError(f"direction: {where} の step（踏み込みの段階）は {', '.join(STEP_LEVEL_NAMES)} のどれかで書いてください")
        if name == "reveal":
            if (not isinstance(v, dict) or set(v) != {"dir", "sec"} or v["dir"] not in REVEAL_DIRS or isinstance(v["sec"], bool)
                    or not isinstance(v["sec"], (int, float)) or not REVEAL_SEC[0] <= v["sec"] <= REVEAL_SEC[1]):
                raise LookError(f"direction: {where} の reveal は {{\"dir\": {'・'.join(REVEAL_DIRS)} のどちらか, \"sec\": {REVEAL_SEC[0]}〜{REVEAL_SEC[1]} 秒}} で書いてください（入りの切り抜き。字ごとの遅れはなし）")
        if name == "bouten" and v is not True:
            raise LookError(f"direction: {where} の bouten は true だけを書けます（karaoke の行で、点灯した字の上に点が灯って残る）")
        if name == "align_to_prev" and v is not True:
            raise LookError(f"direction: {where} の align_to_prev は true だけを書けます（前の行の先頭字の x にそろえる。やめるときは項目ごと消す）")
        if name == "clear_cap" and (isinstance(v, bool) or not isinstance(v, (int, float)) or not 0.05 <= v <= 0.25):
            raise LookError(f"direction: {where} の clear_cap は 0.05〜0.25 の数値で書いてください（読み字の周りの点の被覆の上限。既定 0.25）")
        if name == "row_lengths":
            if (not isinstance(v, list) or len(v) < 1 or any(isinstance(x, bool) or not isinstance(x, int) or x <= 0 for x in v)):
                raise LookError(f"direction: {where} の row_lengths は、段の字数（正の整数）を1つ以上並べた配列で書いてください（例 [4, 3]。[7] は「切らずに1段」）")
        if name == "accent_rows":
            if (not isinstance(v, list) or not v or any(isinstance(x, bool) or not isinstance(x, int) or x < 0 for x in v)
                    or len(set(v)) != len(v)):
                raise LookError(f"direction: {where} の accent_rows は、段番号（0 以上の整数。0 始まり）を重ねずに並べた配列で書いてください")
        if name == "row_roles":
            if not isinstance(v, dict) or not v or any(not re.fullmatch(r"\d+", str(k)) or not isinstance(r, str) for k, r in v.items()):
                raise LookError(f'direction: {where} の row_roles は {{"<段番号>": "<役>"}} の形で書いてください')
        if name == "counter":
            _check_counter_item(v, where)
        if name == "accent" and v not in ACCENT_MODES:
            raise LookError(f"direction: {where} の accent '{v}' は {', '.join(ACCENT_MODES)} のどれかで書いてください")
        out[name] = v
    return out


def _check_carry_item(v, where):
    """carry：{"px_s": 正の数, "dir": "down"|"up"}。縦組みの行は列ごとに {"<段番号>": {"px_s", "dir"}} も書ける（2つの書き方の併記はしない）"""
    def one(spec, w):
        if not isinstance(spec, dict) or set(spec) != CARRY_KEYS:
            raise LookError(f'direction: {w} は {{"px_s": 1秒あたりのpx, "dir": "down"|"up"}} の形で書いてください')
        if isinstance(spec["px_s"], bool) or not isinstance(spec["px_s"], (int, float)) or spec["px_s"] <= 0:
            raise LookError(f"direction: {w} の px_s は 0 より大きい数値で書いてください")
        if spec["dir"] not in CARRY_DIRS:
            raise LookError(f"direction: {w} の dir '{spec['dir']}' は {', '.join(CARRY_DIRS)} のどれかで書いてください")

    if not isinstance(v, dict) or not v:
        raise LookError(f"direction: {where} の carry はオブジェクトで書いてください")
    if "px_s" in v or "dir" in v:
        one(v, f"{where} の carry")
        return
    for key, spec in v.items():
        if not re.fullmatch(r"\d+", str(key)):
            raise LookError(f"direction: {where} の carry の段番号 '{key}' は 0 以上の整数で書いてください（全体なら px_s・dir を直接書く）")
        one(spec, f"{where} の carry[{key}]")


def _check_break_after(v, where):
    if not isinstance(v, dict) or not v:
        raise LookError(f'direction: {where} の break_after は {{"<段番号>": <字の位置>}} の形で書いてください')
    for key, pos in v.items():
        if not re.fullmatch(r"\d+", str(key)):
            raise LookError(f"direction: {where} の break_after の段番号 '{key}' は 0 以上の整数で書いてください")
        if isinstance(pos, bool) or not isinstance(pos, int) or pos <= 0:
            raise LookError(f"direction: {where} の break_after[{key}] は正の整数（その字の後で切る）で書いてください")


def _check_counter_item(v, where):
    if isinstance(v, str):
        if v not in COUNTER_STATES:
            raise LookError(f"direction: {where} の counter '{v}' は {', '.join(COUNTER_STATES)} か辞書で書いてください")
        return
    if not isinstance(v, dict):
        raise LookError(f"direction: {where} の counter は文字列か辞書で書いてください")
    for key, val in v.items():
        if key not in COUNTER_KEYS:
            raise LookError(f"direction: {where} の counter に未知の項目 '{key}' があります（{', '.join(sorted(COUNTER_KEYS))}）")
        if key == "count" and (isinstance(val, bool) or not isinstance(val, int) or val < 0):
            raise LookError(f"direction: {where} の counter.count は 0 以上の整数で書いてください")
        if key == "state" and val not in ("run", "dim"):
            raise LookError(f"direction: {where} の counter.state は run か dim で書いてください")
        if key == "rate" and val not in ("per_word", "per_word_x2"):
            raise LookError(f"direction: {where} の counter.rate は per_word か per_word_x2 で書いてください")
        if key == "enter" and val != "push":
            raise LookError(f"direction: {where} の counter.enter は push だけです")
        if key == "break":
            if not isinstance(val, dict) or val.get("kind") != "shatter":
                raise LookError(f"direction: {where} の counter.break.kind は shatter だけです（peel は作っていません）")
            for kk in ("crack_f", "fall_f"):
                if isinstance(val.get(kk), bool) or not isinstance(val.get(kk), int) or val[kk] <= 0:
                    raise LookError(f"direction: {where} の counter.break.{kk} は正の整数で書いてください")


def parse_line_keys(keys, n_lines):
    """{"1": x, "5-8": y} を {行番号(1始まり): 項目} に展開する。後に書いたキーが勝つ。項目は検査して別名を直す。"""
    out = {}
    for key, item in keys.items():
        m = re.fullmatch(r"\s*(\d+)\s*(?:-\s*(\d+)\s*)?", str(key))
        if not m:
            raise LookError(f"direction: 行のキー '{key}' が読めません（'1' か '5-8' の形）")
        lo = int(m.group(1))
        hi = int(m.group(2) or lo)
        if lo < 1 or hi < lo or hi > n_lines:
            raise LookError(f"direction: 行のキー '{key}' が範囲外です（1〜{n_lines}）")
        item = _normalize_item(item, LINE_ITEM_KEYS, f"行 '{key}'")
        for n in range(lo, hi + 1):
            merged = dict(out.get(n, {}))
            merged.update(item)
            out[n] = merged
    return out


SAFE_AREA_KEYS = {"left", "right", "top", "bottom", "corner"}


def _validate_safe_area(sa):
    """safe_area：読み字（carry・踏み込み・画面の寄りを当てた後の外接矩形）が全フレームで入る枠（px。画面は 1080×1920）。
    {"left", "right"（左右の余白）, "top", "bottom"（枠の上端・下端の y）, "corner": {"y_from", "x_max"}（任意。y が y_from より下では x が x_max まで）}。
    値は暫定でコードに持たない（direction に書く）"""
    if not isinstance(sa, dict) or not {"left", "right", "top", "bottom"} <= set(sa) or set(sa) - SAFE_AREA_KEYS:
        raise LookError("direction: safe_area は {\"left\", \"right\", \"top\", \"bottom\"（必須）, \"corner\": {\"y_from\", \"x_max\"}（任意）} の形で書いてください")
    for k in ("left", "right", "top", "bottom"):
        if isinstance(sa[k], bool) or not isinstance(sa[k], (int, float)) or not math.isfinite(sa[k]) or sa[k] < 0:
            raise LookError(f"direction: safe_area.{k} は 0 以上の有限の数値で書いてください（NaN・inf は不可）")
    if sa["left"] + sa["right"] >= 1080 or sa["top"] >= sa["bottom"] or sa["bottom"] > 1920:
        raise LookError("direction: safe_area の枠が画面（1080×1920）に収まらないか、空です（left＋right＜1080、top＜bottom≦1920）")
    c = sa.get("corner")
    if c is not None:
        if not isinstance(c, dict) or set(c) != {"y_from", "x_max"} or any(isinstance(v, bool) or not isinstance(v, (int, float)) or not math.isfinite(v) for v in c.values()):
            raise LookError('direction: safe_area.corner は {"y_from": 数値, "x_max": 数値} の形で書いてください')
        if not sa["top"] < c["y_from"] < sa["bottom"] or not sa["left"] < c["x_max"] <= 1080 - sa["right"]:
            raise LookError("direction: safe_area.corner が枠の外です（top＜y_from＜bottom、left＜x_max≦1080−right）")


def _validate_stack(stack, n_lines, by_line, theme):
    """stack：前の列を残して薄くする。[{"lines": [最初の行, 最後の行], "dim": 0〜1（残した列の不透明度）, "clear_at_line": 全部消す行}]。
    積む行はすべて layout: vertical（exit は swap のみ）。区間（最初の行〜消す行の手前）は重ねない。残した列の比は kinetic 側（テーマの配色）で検査する"""
    if not isinstance(stack, list) or not stack:
        raise LookError('direction: stack は [{"lines": [a, b], "dim": 不透明度, "clear_at_line": N}] の形で書いてください')
    if theme is None:
        raise LookError("direction: stack はテーマを使う曲でだけ使えます（残した列の比をテーマの配色から検査する）")
    spans = []
    for i, st in enumerate(stack):
        where = f"stack[{i}]"
        if not isinstance(st, dict) or set(st) != STACK_KEYS:
            raise LookError(f"direction: {where} は {', '.join(sorted(STACK_KEYS))} をすべて書いたオブジェクトで書いてください")
        ln = st["lines"]
        if (not isinstance(ln, list) or len(ln) != 2 or any(isinstance(x, bool) or not isinstance(x, int) for x in ln)
                or not 1 <= ln[0] < ln[1] <= n_lines):
            raise LookError(f"direction: {where}.lines は [最初の行, 最後の行]（1〜{n_lines} の整数で、最初 < 最後）で書いてください")
        dim = st["dim"]
        if isinstance(dim, bool) or not isinstance(dim, (int, float)) or not 0 < dim < 1:
            raise LookError(f"direction: {where}.dim は 0 より大きく 1 より小さい数値で書いてください")
        n = st["clear_at_line"]
        if isinstance(n, bool) or not isinstance(n, int) or n != ln[1] + 1 or n > n_lines:
            raise LookError(f"direction: {where}.clear_at_line は、最後の行の次の行（{ln[1] + 1}。{n_lines} 以下）だけ書けます（間に積まない行を挟むと、重なる・列が早く消える）")
        hid = hidden_lines(by_line)
        if n in hid or any(k in hid for k in range(ln[0], ln[1] + 1)):
            raise LookError(f"direction: {where} の lines／clear_at_line に字幕に出さない行（hidden）があります（行の表示の終わりが無いので、残した列の区間が決まりません）")
        for k in range(ln[0], ln[1] + 1):
            it = by_line.get(k, {})
            if it.get("layout") != "vertical":
                raise LookError(f"direction: {where} の行{k} は layout: vertical ではありません（積む行はすべて縦組み）")
            if it.get("exit", "swap") != "swap":
                raise LookError(f"direction: {where} の行{k} の exit は swap だけです（積んだ列は消え方の動きを持たない）")
        spans.append((ln[0], n, where))
    spans.sort()
    for (a0, n0, w0), (a1, _n1, w1) in zip(spans, spans[1:]):
        if a1 < n0:
            raise LookError(f"direction: {w0} と {w1} の区間（最初の行〜消す行の手前）が重なっています")


def hidden_lines(by_line):
    """字幕に出さない行（hidden: true）の行番号の集合（1 始まり）"""
    return frozenset(n for n, it in by_line.items() if it.get("hidden"))


def _validate_hidden(hidden, by_line, n_lines):
    """hidden（U13）：字幕に出さない行。カットはプランに残り、行の開始は at_line／until_line の基準に使える。
    hidden の行に書けるのは hidden だけ（効かない値を残さない）。前の行が字幕に出ない行の align_to_prev・全行 hidden は止める"""
    if not hidden:
        return
    for n in sorted(hidden):
        other = sorted(k for k in by_line[n] if k != "hidden")
        if other:
            raise LookError(f"direction: 行{n} は hidden（字幕に出さない行）なので、ほかの項目（{', '.join(other)}）は書けません（効かない値を残さず止めました）")
        if n + 1 <= n_lines and by_line.get(n + 1, {}).get("align_to_prev"):
            raise LookError(f"direction: 行{n + 1} の align_to_prev は、前の行（行{n}）が hidden（字幕に出さない）ので使えません")
    if len(hidden) >= n_lines:
        raise LookError("direction: 全部の行が hidden です（字幕に出す行が1つもありません）")


def validate_direction(direction, n_lines, vdefaults, theme=None):
    """direction の中身の検査（書き間違いを黙って既定値に戻さない）。未知の項目・存在しない声は LookError で止める。
    vdefaults は voice_defaults の結果（テーマ・direction の voices の和）"""
    for k in direction:
        if k not in DIRECTION_TOP_KEYS and not str(k).startswith("_"):
            raise LookError(f"direction: 未知の最上位の項目 '{k}' があります（{', '.join(sorted(DIRECTION_TOP_KEYS))}）")
    by_line = parse_line_keys(direction.get("lines", {}), n_lines)
    hidden = hidden_lines(by_line)
    _validate_hidden(hidden, by_line, n_lines)
    for n, item in sorted(by_line.items()):
        v = item.get("voice")
        if v is not None and v not in vdefaults:
            raise LookError(f"direction: 行{n} の声 '{v}' が voices にありません（{', '.join(sorted(vdefaults)) or 'なし'}）")
        look_keys = [k for k in ("role", "palette", "accent", "max_px", "tracking", "min_px", "break_after", "accent_rows", "row_roles", "row_lengths", "clear_cap", "align_to_prev") if k in item]
        if look_keys and theme is None:
            raise LookError(f"direction: 行{n} に見た目の項目（{', '.join(look_keys)}）がありますが、テーマが指定されていません"
                            f"（direction の look か --look）")
        if theme is not None:
            if item.get("role") is not None and item["role"] not in theme["fonts"]:
                raise LookError(f"direction: 行{n} の role '{item['role']}' がテーマの fonts にありません")
            if item.get("palette") is not None and item["palette"] not in theme["palettes"]:
                raise LookError(f"direction: 行{n} の palette '{item['palette']}' がテーマの palettes にありません")
            if item.get("accent") == "glow" and "glow" not in (theme.get("parts") or {}):
                raise LookError(f"direction: 行{n} は accent: glow ですが、テーマに parts.glow がありません")
        if (item.get("accent") == "rows") != (item.get("accent_rows") is not None):
            raise LookError(f"direction: 行{n} の accent_rows は accent: rows の行だけに書けます（accent: rows には accent_rows の指定が要ります）")
        if item.get("karaoke_cap") is not None and item.get("entrance") != "karaoke":
            raise LookError(f"direction: 行{n} の karaoke_cap は entrance: karaoke の行だけに書けます")
        if item.get("align_to_prev"):
            if n == 1:
                raise LookError("direction: 行1 の align_to_prev は、前の行が無いので使えません")
            if item.get("layout") is not None and item["layout"] != "center":
                raise LookError(f"direction: 行{n} の align_to_prev は layout: center の行だけに書けます（書いたのは {item['layout']}）")
        if item.get("row_lengths") is not None:
            if item.get("break_after") is not None:
                raise LookError(f"direction: 行{n} の row_lengths は break_after と一緒に書けません（段の切り方は片方だけ）")
            if item.get("layout") == "vertical":
                raise LookError(f"direction: 行{n} の row_lengths は横組みの行だけに書けます（縦組みの段は break_after と自動の組版で決まる）")
        if item.get("row_roles") is not None:
            if item.get("layout") != "vertical":
                raise LookError(f"direction: 行{n} の row_roles は layout: vertical の行だけに書けます（横組みは段の幅の計算が書体で変わる）")
            if theme is not None:
                bad = [r for r in item["row_roles"].values() if r not in theme["fonts"]]
                if bad:
                    raise LookError(f"direction: 行{n} の row_roles の役 '{bad[0]}' がテーマの fonts にありません")
        if item.get("decor") is not None:
            import kinetic_fx   # 遅延 import（kinetic_fx → look の向きを作らない）

            if item["decor"] not in kinetic_fx.DECOR_NAMES:
                raise LookError(f"direction: 行{n} の decor '{item['decor']}' は未対応です（{', '.join(kinetic_fx.DECOR_NAMES)}）")
        if item.get("max_col_chars") is not None and item.get("layout") != "vertical":
            raise LookError(f"direction: 行{n} の max_col_chars は layout: vertical の行だけに書けます")
        if item.get("layout") == "vertical":
            if item.get("entrance", "cut") not in VERTICAL_ENTRANCES:
                raise LookError(f"direction: 行{n} は縦組みですが、入り '{item.get('entrance')}' は縦組みでは使えません"
                                f"（使える入り: {', '.join(VERTICAL_ENTRANCES)}）。横向きの動き・slam は止めました")
        if (item.get("hold") == "carry") != (item.get("carry") is not None):
            raise LookError(f"direction: 行{n} の carry は hold: carry の行だけに書けます（hold: carry には carry の指定が要ります）")
        if item.get("hold") == "carry":
            if theme is None:
                raise LookError(f"direction: 行{n} の hold: carry はテーマを使う曲でだけ使えます（動いた後の余白の検査をテーマ使用時の検査と同じ経路で行う。stack と同じ扱い）")
            if item.get("entrance", "cut") in ("mask", "stamp", "slash"):
                raise LookError(f"direction: 行{n} の hold: carry は entrance: {item.get('entrance', 'cut')} と一緒に使えません（切り抜き・枠・斜線が動く前の位置に残る）")
            if "px_s" not in item["carry"] and item.get("layout") != "vertical":
                raise LookError(f"direction: 行{n} の carry を段ごとに書けるのは layout: vertical の行だけです")
        if item.get("ink") is not None and item.get("entrance") == "karaoke":
            raise LookError(f"direction: 行{n} の ink（読み字の質感）は karaoke の行には掛けません（未点灯の濃さの基準が崩れる）")
        if item.get("entrance") == "karaoke":
            if theme is None:
                raise LookError(f"direction: 行{n} の entrance karaoke はテーマを使う曲でだけ使えます（未点灯の色をテーマの配色から決めます）")
            if direction.get("karaoke") is None:
                raise LookError(f"direction: 行{n} は entrance: karaoke ですが、最上位に karaoke の指定がありません")
        if item.get("impact") is not None:
            if item.get("entrance") != "slam":
                raise LookError(f"direction: 行{n} の impact は entrance: slam の行だけに書けます")
            if item["impact"] not in (direction.get("impacts") or {}):
                raise LookError(f"direction: 行{n} の impact '{item['impact']}' が impacts にありません"
                                f"（{', '.join(sorted(direction.get('impacts') or {})) or 'impacts なし'}）")
    _validate_stage3(direction, by_line, theme)
    if "safe_area" in direction:        # null も検査に通す（黙って効かなくしない）
        if theme is None:
            raise LookError("direction: safe_area はテーマを使う曲でだけ使えます（検査はテーマの経路の描画器で行う）")
        _validate_safe_area(direction["safe_area"])
    if direction.get("stack") is not None:
        _validate_stack(direction["stack"], n_lines, by_line, theme)
    if direction.get("points") is not None:
        import kinetic_points   # 遅延 import

        kinetic_points.validate_points(direction["points"], n_lines, theme, hidden=hidden)
    if direction.get("halftone") is not None:
        import kinetic_points   # 遅延 import

        if theme is None:
            raise LookError("direction: halftone はテーマ（look）を使う曲でだけ使えます（色をテーマの points_color から取ります）")
        kinetic_points.validate_halftone(direction["halftone"], n_lines)
    vt = direction.get("vertical")
    if vt is not None:
        if not isinstance(vt, dict) or not vt or not set(vt) <= VERTICAL_KEYS:
            raise LookError(f'direction: vertical は {{"height": px, "top": px, "kana_shift": 割合}} の形で書いてください（使える項目: {", ".join(sorted(VERTICAL_KEYS))}）')
        for k, v in vt.items():
            if isinstance(v, bool) or not isinstance(v, (int, float)):
                raise LookError(f"direction: vertical.{k} は数値で書いてください")
        ks = vt.get("kana_shift", 0)
        if not 0 <= ks <= 0.3:
            raise LookError("direction: vertical.kana_shift は 0〜0.3（字の大きさの割合）で書いてください")
        import kinetic_vertical   # 遅延 import（組版の既定値と同じ1か所を使う）

        top, height = vt.get("top", kinetic_vertical.DEFAULT_TOP), vt.get("height", kinetic_vertical.DEFAULT_H)
        if height < 300 or top < 70 or top + height > 1850:
            raise LookError(f"direction: vertical の範囲（top {top}・height {height}）が画面に収まりません（height 300 以上、top 70 以上、top+height 1850 以下）")
    if theme is not None:
        for voice, spec in vdefaults.items():
            if spec.get("role") is not None and spec["role"] not in theme["fonts"]:
                raise LookError(f"direction: 声 '{voice}' の role '{spec['role']}' がテーマの fonts にありません")
            if spec.get("palette") is not None and spec["palette"] not in theme["palettes"]:
                raise LookError(f"direction: 声 '{voice}' の palette '{spec['palette']}' がテーマの palettes にありません")
    elif any((spec or {}).get("role") or (spec or {}).get("palette") for spec in vdefaults.values()):
        raise LookError("direction: 声に role・palette がありますが、テーマが指定されていません")
    kw = direction.get("key_word")
    if kw is not None:
        if (not isinstance(kw, dict) or not isinstance(kw.get("from_line"), int)
                or not 1 <= kw["from_line"] <= n_lines or kw.get("rule") != "first_bracket"):
            raise LookError('direction: key_word は {"from_line": 行番号, "rule": "first_bracket"} の形で書いてください')
        if kw["from_line"] in hidden:
            raise LookError(f"direction: key_word.from_line の行{kw['from_line']} は hidden（字幕に出さない行）なので、差し色の語の取り出し元にできません")
    trs = direction.get("bg_transitions", [])
    if not isinstance(trs, list):
        raise LookError("direction: bg_transitions は配列で書いてください")
    for i, tr in enumerate(trs):
        where = f"bg_transitions[{i}]"
        if not isinstance(tr, dict) or not (set(tr) <= {"from", "to", "start", "after_line", "seconds", "until_line"}):
            raise LookError(f"direction: {where} に未知の項目があります（from・to・start|after_line・seconds|until_line）")
        if theme is None:
            raise LookError(f"direction: {where} がありますが、テーマが指定されていません")
        for key in ("from", "to"):
            if tr.get(key) not in theme["palettes"]:
                raise LookError(f"direction: {where}.{key} '{tr.get(key)}' がテーマの palettes にありません")
        if ("start" in tr) == ("after_line" in tr):
            raise LookError(f"direction: {where} は start（秒）か after_line（行番号）のどちらか1つを書いてください")
        if ("seconds" in tr) == ("until_line" in tr):
            raise LookError(f"direction: {where} は seconds（秒）か until_line（行番号）のどちらか1つを書いてください")
        for key in ("after_line", "until_line"):
            if key in tr and not (isinstance(tr[key], int) and 1 <= tr[key] <= n_lines):
                raise LookError(f"direction: {where}.{key} が行番号（1〜{n_lines}）ではありません")
        if tr.get("after_line") in hidden:
            raise LookError(f"direction: {where}.after_line は字幕に出さない行（hidden）です。行の表示の終わりが無いので意味が決まりません（start か until_line を使うか、字幕に出す行を指してください）")
        for key in ("start", "seconds"):
            if key in tr and (isinstance(tr[key], bool) or not isinstance(tr[key], (int, float)) or tr[key] < 0):
                raise LookError(f"direction: {where}.{key} は 0 以上の数値で書いてください")


def _validate_stage3(direction, by_line, theme):
    """段3の最上位の項目と、slam の使い方の検査（impacts のある曲だけ。無い曲は今までと同じ）"""
    impacts = direction.get("impacts")
    if impacts is not None:
        if not isinstance(impacts, dict) or not impacts:
            raise LookError("direction: impacts は {名前: {...}} の形で書いてください")
        for name, spec in impacts.items():
            if not isinstance(spec, dict) or not set(IMPACT_KEYS) <= set(spec) <= set(IMPACT_KEYS) | set(IMPACT_OPTIONAL_KEYS):
                raise LookError(f"direction: impacts.{name} は {', '.join(IMPACT_KEYS)} の全部（と任意の {', '.join(IMPACT_OPTIONAL_KEYS)}）を書いてください"
                                f"（足りない・余分な項目があります）")
            if spec.get("ease", "linear") not in IMPACT_EASES:
                raise LookError(f"direction: impacts.{name}.ease は {', '.join(IMPACT_EASES)} のどちらかで書いてください")
            for key, val in spec.items():
                if key in IMPACT_OPTIONAL_KEYS:
                    continue
                if isinstance(val, bool) or not isinstance(val, (int, float)) or val < 0:
                    raise LookError(f"direction: impacts.{name}.{key} は 0 以上の数値で書いてください")
            if not isinstance(spec["land_frames"], int) or not 1 <= spec["land_frames"] <= 5:
                raise LookError(f"direction: impacts.{name}.land_frames は 1〜5 の整数で書いてください（入りは 6 フレーム）")
            if not 0 < spec["undershoot"] <= 1 or spec["overshoot"] < 1:
                raise LookError(f"direction: impacts.{name} の overshoot は 1 以上、undershoot は 0 より大きく 1 以下で書いてください")
        slam_rows = [n for n, it in by_line.items() if it.get("entrance") == "slam"]
        if len(slam_rows) > SLAM_MAX_LINES:
            raise LookError(f"direction: slam の行が {len(slam_rows)} 行あります（上限 {SLAM_MAX_LINES}）。叩きが常態化するので止めました")
        sv = direction.get("slam_voice")
        if slam_rows and sv is None:
            raise LookError("direction: slam の行がありますが、slam を使える声（最上位の slam_voice）が書かれていません")
        if sv is not None:
            names = [sv] if isinstance(sv, str) else sv
            if not isinstance(names, list) or not names or not all(isinstance(x, str) for x in names):
                raise LookError("direction: slam_voice は声の名前（文字列）か、その配列で書いてください")
            for n in slam_rows:
                if by_line[n].get("voice") not in names:
                    raise LookError(f"direction: 行{n} は slam ですが、声が slam_voice（{', '.join(names)}）ではありません"
                                    f"（slam は指定した声の行だけ）")
    elif direction.get("slam_voice") is not None:
        raise LookError("direction: slam_voice がありますが、impacts がありません")
    kd = direction.get("karaoke")
    if kd is not None:
        if not isinstance(kd, dict) or not set(kd) <= KARAOKE_KEYS:
            raise LookError(f"direction: karaoke は {', '.join(sorted(KARAOKE_KEYS))} の項目で書いてください")
        for key in ("unlit_opacity", "light_frames", "min_match", "min_cover"):
            if key not in kd:
                raise LookError(f"direction: karaoke.{key} がありません")
        if not (isinstance(kd["unlit_opacity"], (int, float)) and not isinstance(kd["unlit_opacity"], bool)
                and 0 < kd["unlit_opacity"] <= 1):
            raise LookError("direction: karaoke.unlit_opacity は 0 より大きく 1 以下の数値で書いてください")
        if isinstance(kd["light_frames"], bool) or not isinstance(kd["light_frames"], int) or kd["light_frames"] < 1:
            raise LookError("direction: karaoke.light_frames は 1 以上の整数で書いてください")
        for key in ("min_match", "min_cover"):
            if isinstance(kd[key], bool) or not isinstance(kd[key], (int, float)) or not 0 <= kd[key] <= 1:
                raise LookError(f"direction: karaoke.{key} は 0〜1 の数値で書いてください")
        if kd.get("keyword_unlit", "text") != "text":
            raise LookError("direction: karaoke.keyword_unlit は text だけです")
    _validate_counter_top(direction, by_line)
    _validate_interludes(direction, hidden_lines(by_line))


def _num(v):
    return isinstance(v, (int, float)) and not isinstance(v, bool)


def _validate_counter_top(direction, by_line):
    """最上位の counter（appear・voices）と、行の counter を使うときの前提"""
    cd = direction.get("counter")
    uses_rows = any("counter" in it for it in by_line.values())
    if cd is None:
        if uses_rows:
            raise LookError("direction: 行に counter がありますが、最上位の counter（voices）がありません")
        return
    if not isinstance(cd, dict) or not set(cd) <= COUNTER_TOP_KEYS:
        raise LookError(f"direction: counter は {', '.join(sorted(COUNTER_TOP_KEYS))} の項目を持つ辞書で書いてください")
    vs = cd.get("voices")
    names = [vs] if isinstance(vs, str) else vs
    if not isinstance(names, list) or not names or not all(isinstance(x, str) for x in names):
        raise LookError("direction: counter.voices は声の名前（文字列）か、その配列で書いてください"
                        "（カウンターが出てよい声。それ以外の声の行は消える・出ない）")
    appear = cd.get("appear", [])
    if not isinstance(appear, list):
        raise LookError("direction: counter.appear は配列で書いてください")
    for i, ap in enumerate(appear):
        where = f"counter.appear[{i}]"
        if not isinstance(ap, dict) or not set(ap) <= APPEAR_KEYS:
            raise LookError(f"direction: {where} は {', '.join(sorted(APPEAR_KEYS))} の項目で書いてください")
        if ("at" in ap) == ("after_line" in ap):
            raise LookError(f"direction: {where} は at（秒）か after_line＋until_line（行）のどちらか1つで書いてください")
        if "at" in ap and (not _num(ap["at"]) or ap["at"] < 0):
            raise LookError(f"direction: {where}.at は 0 以上の秒で書いてください")
        if "after_line" in ap:
            if "until_line" not in ap:
                raise LookError(f"direction: {where} は after_line と until_line を両方書いてください")
            n = direction.get("n_lines")
            for key in ("after_line", "until_line"):
                if not (isinstance(ap[key], int) and not isinstance(ap[key], bool) and 1 <= ap[key] <= n):
                    raise LookError(f"direction: {where}.{key} が行番号（1〜{n}）ではありません")
            if ap["after_line"] in hidden_lines(by_line):
                raise LookError(f"direction: {where}.after_line は字幕に出さない行（hidden）です。行の表示の終わりが無いので意味が決まりません（until_line は hidden の行でも使えます）")
            if ap["until_line"] <= ap["after_line"]:
                raise LookError(f"direction: {where} は until_line が after_line より後の行でなければなりません（空の区間）")
        if "at_fraction" in ap and (not _num(ap["at_fraction"]) or not 0 <= ap["at_fraction"] <= 1):
            raise LookError(f"direction: {where}.at_fraction は 0〜1 で書いてください")
        if isinstance(ap.get("count"), bool) or not isinstance(ap.get("count"), int) or ap["count"] < 1:
            raise LookError(f"direction: {where}.count は 1 以上の整数で書いてください")
        if ap.get("rate", "per_onset") != "per_onset":
            raise LookError(f"direction: {where}.rate は per_onset だけです（単語の無い区間は beats のオンセットで増やす）")


def _validate_interludes(direction, hidden=frozenset()):
    il = direction.get("interludes")
    if il is None:
        return
    if not isinstance(il, list):
        raise LookError("direction: interludes は配列で書いてください")
    n = direction.get("n_lines")
    for i, it in enumerate(il):
        where = f"interludes[{i}]"
        if not isinstance(it, dict) or not set(it) <= INTERLUDE_KEYS:
            raise LookError(f"direction: {where} は {', '.join(sorted(INTERLUDE_KEYS))} の項目で書いてください")
        if it.get("kind", "duotone") not in INTERLUDE_KINDS:
            raise LookError(f"direction: {where}.kind は {', '.join(INTERLUDE_KINDS)} だけです")
        if ("start" in it) == ("after_line" in it):
            raise LookError(f"direction: {where} は start（秒）か after_line（行）のどちらか1つを書いてください")
        ends = [k for k in ("end", "until_line", "until") if k in it]
        if len(ends) != 1:
            raise LookError(f"direction: {where} は end（秒）・until_line（行）・until（\"end\"＝曲末）のどれか1つを書いてください")
        for key in ("start", "end"):
            if key in it and (not _num(it[key]) or it[key] < 0):
                raise LookError(f"direction: {where}.{key} は 0 以上の秒で書いてください")
        for key in ("after_line", "until_line"):
            if key in it and not (isinstance(it[key], int) and not isinstance(it[key], bool) and 1 <= it[key] <= n):
                raise LookError(f"direction: {where}.{key} が行番号（1〜{n}）ではありません")
        if it.get("after_line") in hidden:
            raise LookError(f"direction: {where}.after_line は字幕に出さない行（hidden）です。行の表示の終わりが無いので意味が決まりません")
        if "until" in it and it["until"] != "end":
            raise LookError(f'direction: {where}.until は "end"（曲末）だけです')
        if "zoom_peak" in it and (not _num(it["zoom_peak"]) or not 1.0 <= it["zoom_peak"] <= 1.2):
            raise LookError(f"direction: {where}.zoom_peak は 1.0〜1.2 で書いてください（区間の中点で最大になる往復の寄り）")
        zr = it.get("zoom_ramp")
        if zr is not None and (not isinstance(zr, dict) or set(zr) != {"to", "seconds"} or not _num(zr["to"])
                               or not 1.0 <= zr["to"] <= 1.2 or not _num(zr["seconds"]) or zr["seconds"] <= 0):
            raise LookError(f"direction: {where}.zoom_ramp は {{to: 1.0〜1.2, seconds: 正の数}} で書いてください")
        if "zoom_peak" in it and zr is not None:
            raise LookError(f"direction: {where} は zoom_peak か zoom_ramp のどちらか1つにしてください")


# ---------------------------------------------------------------------------
# 色の比（コントラスト）。式はここ1か所（未点灯・glow・カウンター・レポート・検査のすべてがこれを使う）

def _rgb(c):
    if isinstance(c, str):
        c = c.lstrip("#")
        return tuple(int(c[i:i + 2], 16) for i in (0, 2, 4))
    return tuple(c)


def _rel_lum(c):
    def lin(v):
        v = v / 255.0
        return v / 12.92 if v <= 0.03928 else ((v + 0.055) / 1.055) ** 2.4
    r, g, b = _rgb(c)
    return 0.2126 * lin(r) + 0.7152 * lin(g) + 0.0722 * lin(b)


def contrast(fg, bg):
    """WCAG 2.x のコントラスト比（1〜21）。色は '#RRGGBB' か (r, g, b)"""
    a, b = _rel_lum(fg), _rel_lum(bg)
    hi, lo = max(a, b), min(a, b)
    return (hi + 0.05) / (lo + 0.05)


def over(fg, bg, a):
    """背景 bg の上に、不透明度 a で fg を重ねた色（8 ビットに丸める）"""
    f, b = _rgb(fg), _rgb(bg)
    return tuple(int(round(b[i] + (f[i] - b[i]) * a)) for i in range(3))


def worst_sign(fg, bg):
    """紙の微粒子（各チャンネル ±n）のうち、比が下がる側の向き。文字（fg）のほうへ背景が寄る側。
    文字が背景より明るい（暗い地）なら +1、暗い（明るい地）なら −1。同じ明るさなら +1（今までの値）"""
    return 1 if _rel_lum(fg) >= _rel_lum(bg) else -1


def worst_bg(fg, bg, a=1.0, n=10):
    """最悪の背景の色：紙の微粒子で比が下がる側（worst_sign）に ±n ずらした背景。暗い地では今までと同じ bg+n"""
    return lift(bg, n * worst_sign(fg, bg))


def worst_contrast(fg, bg, a):
    """最悪の背景に対する比の定義：contrast(over(fg, bg, a), worst_bg(fg, bg, a))。
    文字（バッジ）は名目の背景 bg の上で重ね、比べる背景は紙の微粒子で比が下がる側（暗い地は +10、明るい地は −10）にずれた背景"""
    return contrast(over(fg, bg, a), worst_bg(fg, bg, a))


def lift(bg, n=10):
    """紙の微粒子で各チャンネルを n ずらした背景（n が負なら暗くなる側。0〜255 に収める）"""
    return tuple(min(max(c + n, 0), 255) for c in _rgb(bg))


def is_light_palette(pal):
    """明るい地（背景が文字より明るい）の配色の組か"""
    return _rel_lum(pal["bg"]) > _rel_lum(pal["text"])


def load_direction(cache_dir):
    """_work/<hash>/direction.json を読む。無ければ None（＝従来の経路）。"""
    path = direction_path(cache_dir)
    if not path.exists():
        return None
    return load_direction_file(path)


def load_direction_file(path):
    """direction の JSON を読み、形（n_lines・lines）を確かめる。--direction-from が、置く前に検査するために使う。"""
    path = Path(path)
    data = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(data, dict) or not isinstance(data.get("n_lines"), int):
        raise LookError(f"direction: {path.name} に整数の n_lines がありません")
    if not isinstance(data.get("lines", {}), dict):
        raise LookError("direction: lines はオブジェクトで書いてください")
    return data


def check_direction(direction, n_alignment):
    """行数の照合。GUI で行を足し引きすると番号がずれて別の行に当たるので、違ったら止める。"""
    if direction["n_lines"] != n_alignment:
        raise LookError(
            f"direction の行数（{direction['n_lines']}）と alignment の行数（{n_alignment}）が違います。"
            f"行番号がずれて別の行に当たるので止めました。direction の n_lines と行番号を直してください")


def direction_digest(direction):
    return _digest(direction)


def register_direction(cache_dir, src_path):
    """--direction-from: direction.json を置く。既にあれば direction.bak-<日時>.json に退避してから置き換える。"""
    cache_dir = Path(cache_dir)
    src = Path(src_path)
    data = json.loads(src.read_text(encoding="utf-8"))
    if not isinstance(data.get("n_lines"), int):
        raise LookError("direction: 整数の n_lines がありません")
    dest = direction_path(cache_dir)
    cache_dir.mkdir(parents=True, exist_ok=True)
    if dest.exists():
        bak = cache_dir / f"direction.bak-{datetime.now().strftime('%Y%m%d-%H%M%S')}.json"
        shutil.copy2(dest, bak)
        print(f"      既存の direction.json を退避: {bak.name}")
    dest.write_text(json.dumps(data, ensure_ascii=False, indent=1) + "\n", encoding="utf-8")
    return dest


def voice_defaults(direction, theme):
    """声ごとの既定（テーマ → direction の順に上書き）。{声: {role?, tail?}}。項目名は検査する（tail_sec は tail に直す）"""
    out = {}
    for label, src in (("テーマ", (theme or {}).get("voices")), ("direction", (direction or {}).get("voices"))):
        for voice, spec in (src or {}).items():
            spec = _normalize_item(spec or {}, VOICE_ITEM_KEYS, f"{label}の声 '{voice}'")
            out[voice] = {**out.get(voice, {}), **spec}
    return out
