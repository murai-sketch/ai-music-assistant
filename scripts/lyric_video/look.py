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
    "texture": {...}                                                        # 任意
  }
  役の中身: tracking（字間 em）・leading（行送り ×サイズ）・min_px／max_px（下限を割ったら改行を増やす）・
            thicken {below_px, px}（以下の大きさで同色の縁で太らせる）・layer_outline {px}（層が重なるときだけ背景色の縁）
  palettes: bg・text（必須）、stroke・sub・accent（任意）。名前は自由（"image" は使えない）
  voices.<声>: role・palette・tail。default_role／default_palette: direction 無し（--look だけ）のときの既定
  layout.text_width: 文字を収める横幅 px。texture: paper（plain / none）・bg_image（false のみ。単色背景）

演出（direction.json）の形:
  {
    "look": "<テーマ名>",            # 任意
    "n_lines": 40,                   # 必須。alignment の行数と違ったら止める
    "voices": {"<声>": {"tail": 0.3}},   # 任意。テーマの voices を上書き
    "lines": {"1": {...}, "5-8": {...}}  # 行番号（1始まり）または範囲。同じ行に複数当たれば後勝ち
  }
  行の項目: voice / tail（秒）/ end（絶対時刻で固定）/ exit（swap・fade・fade:<秒>・fall 等）/ entrance / layout / hold / decor /
            role（書体の役）/ palette（配色の名前）/ accent（none・key_word・fill・outline）/ max_px / tracking（役の値の行ごとの上書き）
  key_word: {"from_line": N, "rule": "first_bracket"}   N 行目の最初の「」の中の語を実行時に取り出す（歌詞は書かない）
  bg_transitions: [{"from": 配色, "to": 配色, "start"|"after_line", "seconds"|"until_line"}]   背景色を時間で線形に補間する
"""

import hashlib
import json
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
                  "role", "palette", "accent", "max_px", "tracking"}
ACCENT_MODES = ("none", "key_word", "fill", "outline")
ITEM_ALIASES = {"tail_sec": "tail"}
VOICE_ITEM_KEYS = {"role", "tail", "palette"}
DIRECTION_TOP_KEYS = {"n_lines", "voices", "lines", "look", "key_word", "bg_transitions"}


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
        if name in ("voice", "exit", "entrance", "layout", "hold", "decor", "role", "palette", "accent") and not isinstance(v, str):
            raise LookError(f"direction: {where} の '{k}' は文字列で書いてください")
        if name in ("max_px", "tracking") and (isinstance(v, bool) or not isinstance(v, (int, float))
                                               or (name == "max_px" and v <= 0)):
            raise LookError(f"direction: {where} の '{k}' は数値で書いてください")
        if name == "accent" and v not in ACCENT_MODES:
            raise LookError(f"direction: {where} の accent '{v}' は {', '.join(ACCENT_MODES)} のどれかで書いてください")
        out[name] = v
    return out


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


def validate_direction(direction, n_lines, vdefaults, theme=None):
    """direction の中身の検査（書き間違いを黙って既定値に戻さない）。未知の項目・存在しない声は LookError で止める。
    vdefaults は voice_defaults の結果（テーマ・direction の voices の和）"""
    for k in direction:
        if k not in DIRECTION_TOP_KEYS and not str(k).startswith("_"):
            raise LookError(f"direction: 未知の最上位の項目 '{k}' があります（{', '.join(sorted(DIRECTION_TOP_KEYS))}）")
    by_line = parse_line_keys(direction.get("lines", {}), n_lines)
    for n, item in sorted(by_line.items()):
        v = item.get("voice")
        if v is not None and v not in vdefaults:
            raise LookError(f"direction: 行{n} の声 '{v}' が voices にありません（{', '.join(sorted(vdefaults)) or 'なし'}）")
        look_keys = [k for k in ("role", "palette", "accent", "max_px", "tracking") if k in item]
        if look_keys and theme is None:
            raise LookError(f"direction: 行{n} に見た目の項目（{', '.join(look_keys)}）がありますが、テーマが指定されていません"
                            f"（direction の look か --look）")
        if theme is not None:
            if item.get("role") is not None and item["role"] not in theme["fonts"]:
                raise LookError(f"direction: 行{n} の role '{item['role']}' がテーマの fonts にありません")
            if item.get("palette") is not None and item["palette"] not in theme["palettes"]:
                raise LookError(f"direction: 行{n} の palette '{item['palette']}' がテーマの palettes にありません")
            if item.get("layout") == "vertical":
                raise LookError(f"direction: 行{n} の layout 'vertical'（縦組み）はテーマを使う曲では使えません")
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
        for key in ("start", "seconds"):
            if key in tr and (isinstance(tr[key], bool) or not isinstance(tr[key], (int, float)) or tr[key] < 0):
                raise LookError(f"direction: {where}.{key} は 0 以上の数値で書いてください")


def load_direction(cache_dir):
    """_work/<hash>/direction.json を読む。無ければ None（＝従来の経路）。"""
    path = direction_path(cache_dir)
    if not path.exists():
        return None
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
