#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
kinetic_backdrop.py
direction の backdrops：単色の背景（テーマの配色の背景色・補間・紙の粒）の上に、静止画の質感と下の帯の滲みをゆっくり重ねる。
動画は使わない。backdrops の無い曲は何も通らない（既存の出力は変わらない）。

  "backdrops": [
    {"image": "<名前>", "mode": "texture",
     "level": [[<時刻>, <値>], ...],        # 質感の強さ。値＝その画像の「明るい側の揺れ（上位5%）」を背景色に何段（0〜255）足すか。区間の外は最初／最後の値
     "move":  [[<時刻>, <px/秒>], ...]},     # 任意。その時刻からの移動の速さ（正＝下向き、負＝上向き、0＝止める）。上下をつないで繰り返す
    {"image": "<名前>", "mode": "band",
     "level": [[<時刻>, <値>], ...],        # 画像を重ねる最大の割合（0〜1。例 0.3）
     "band":  {"from_y": 1440, "ramp": 260}}   # この高さから下へ、ramp px かけて 0→1 に増える（これより上は重ならない）
  ]
  時刻：秒（数）／"start:N"（行 N の開始）／"end:N"（行 N の表示の終わり）。後ろに +秒／-秒 を付けられる（例 "end:24+1.0"）。
  image：scripts/lyric_video/_work/backgrounds/ からの相対パス。拡張子は省略可（png・jpg・jpeg・webp の順）。同じ名前で置き換えれば差し替わる。
         どんな大きさでも、画面を覆うように拡大して中央を使う。texture は明るさの平均を引いて使うので、元の絵の明るさ・色は効かない（形だけ）。

質感は「背景色 + (画像の揺れ ÷ 上位5%の揺れ) × level」なので、画像を明るい絵に差し替えても、字の後ろの明るさは level が上限を決める。
"""

import re
from pathlib import Path

import numpy as np
from PIL import Image

W, H = 1080, 1920
MODES = ("texture", "band")
ITEM_KEYS = {"image", "mode", "level", "move", "band"}
_TIME = re.compile(r"^(start|end):(\d+)([+-]\d+(?:\.\d+)?)?$")
_EXTS = (".png", ".jpg", ".jpeg", ".webp")


def _check_time(v, where, n_lines, LookError):
    if isinstance(v, bool):
        raise LookError(f"direction: {where} の時刻が不正です")
    if isinstance(v, (int, float)):
        if v < 0:
            raise LookError(f"direction: {where} の時刻は 0 以上で書いてください")
        return
    m = _TIME.match(v) if isinstance(v, str) else None
    if not m or not 1 <= int(m.group(2)) <= n_lines:
        raise LookError(f"direction: {where} の時刻は 秒（数）か \"start:行\"・\"end:行\"（後ろに +秒／-秒）で書いてください")


def _check_keys(v, where, n_lines, LookError, lo=None, hi=None):
    if not isinstance(v, list) or not v or any(not isinstance(k, list) or len(k) != 2 for k in v):
        raise LookError(f"direction: {where} は [[時刻, 値], ...] の形で書いてください")
    for k in v:
        _check_time(k[0], where, n_lines, LookError)
        if isinstance(k[1], bool) or not isinstance(k[1], (int, float)):
            raise LookError(f"direction: {where} の値は数で書いてください")
        if lo is not None and not lo <= k[1] <= hi:
            raise LookError(f"direction: {where} の値は {lo}〜{hi} で書いてください")


def validate(spec, n_lines, LookError):
    if not isinstance(spec, list) or not spec:
        raise LookError("direction: backdrops は配列で書いてください")
    for i, it in enumerate(spec):
        where = f"backdrops[{i}]"
        if not isinstance(it, dict) or not set(it) <= ITEM_KEYS or not isinstance(it.get("image"), str) or it.get("mode") not in MODES:
            raise LookError(f"direction: {where} は image（文字列）・mode（{'・'.join(MODES)}）・level と、任意の move・band だけで書いてください")
        if it["mode"] == "texture":
            _check_keys(it.get("level"), where + ".level", n_lines, LookError, 0, 40)
            if "move" in it:
                _check_keys(it["move"], where + ".move", n_lines, LookError, -200, 200)
            if "band" in it:
                raise LookError(f"direction: {where} の band は mode: band だけに書けます")
        else:
            _check_keys(it.get("level"), where + ".level", n_lines, LookError, 0, 1)
            b = it.get("band")
            if (not isinstance(b, dict) or set(b) != {"from_y", "ramp"} or any(isinstance(x, bool) or not isinstance(x, (int, float)) for x in b.values())
                    or not 0 <= b["from_y"] < H or b["ramp"] <= 0):
                raise LookError(f"direction: {where} の band は {{\"from_y\": 高さ(px), \"ramp\": 増える幅(px)}} で書いてください")
            if "move" in it:
                raise LookError(f"direction: {where} の move は mode: texture だけに書けます")


def find_image(name, work_dir):
    base = Path(work_dir) / "backgrounds" / name
    if base.suffix.lower() in _EXTS and base.is_file():
        return base
    for e in _EXTS:
        p = base.with_name(base.name + e)
        if p.is_file():
            return p
    return None


def _cover(im, mode_l):
    """画面（W×H）を覆うように拡大して中央を切り出す"""
    s = max(W / im.width, H / im.height)
    im = im.resize((max(int(round(im.width * s)), W), max(int(round(im.height * s)), H)), Image.LANCZOS)
    x0, y0 = (im.width - W) // 2, (im.height - H) // 2
    return im.crop((x0, y0, x0 + W, y0 + H))


class Backdrops:
    def __init__(self, spec, resolve_line_time, work_dir, LookError):
        """resolve_line_time(kind, n) -> 秒（kind＝start|end）"""
        self.items = []
        for i, it in enumerate(spec):
            path = find_image(it["image"], work_dir)
            if path is None:
                raise LookError(f"direction: backdrops[{i}] の画像 '{it['image']}' が {Path(work_dir) / 'backgrounds'} にありません"
                                f"（png・jpg・jpeg・webp。同じ名前で置けば差し替わります）")
            level = self._keys(it["level"], resolve_line_time, LookError, where=f"backdrops[{i}].level")
            rec = {"mode": it["mode"], "level": level, "path": str(path)}
            if it["mode"] == "texture":
                g = _cover(Image.open(path).convert("L"), True)
                a = np.asarray(g, dtype=np.float32)
                med = float(np.median(a))
                p95 = float(np.percentile(a, 95))
                n = np.clip((a - med) / max(p95 - med, 1.0), -1.5, 1.5)
                tile = np.concatenate([n, n[::-1]], axis=0)        # 上下をつないで繰り返す（継ぎ目で鏡写し）
                rec["tile"] = np.concatenate([tile, tile[:1]], axis=0).astype(np.float32)
                mv = self._keys(it["move"], resolve_line_time, LookError, where=f"backdrops[{i}].move") if "move" in it else []
                rec["move_t"] = [k[0] for k in mv]
                rec["move_v"] = [k[1] for k in mv]
                offs, o = [], 0.0
                for j, (t0, v) in enumerate(mv):
                    offs.append(o)
                    if j + 1 < len(mv):
                        o += v * (mv[j + 1][0] - t0)
                rec["move_o"] = offs
            else:
                img = np.asarray(_cover(Image.open(path).convert("RGB"), False), dtype=np.float32)
                y = np.arange(H, dtype=np.float32)
                b = it["band"]
                m = np.clip((y - b["from_y"]) / b["ramp"], 0.0, 1.0)
                rec["img"] = img
                rec["mask"] = (m * m * (3 - 2 * m))[:, None, None]   # smoothstep
            self.items.append(rec)

    @staticmethod
    def _keys(keys, resolve, LookError, where):
        out = []
        for t, v in keys:
            if isinstance(t, str):
                m = _TIME.match(t)
                t = resolve(m.group(1), int(m.group(2))) + (float(m.group(3)) if m.group(3) else 0.0)
            out.append((float(t), float(v)))
        for a, b in zip(out, out[1:]):
            if b[0] < a[0] - 1e-9:
                raise LookError(f"direction: {where} の時刻が昇順になっていません（{a[0]:.2f}秒の次が {b[0]:.2f}秒）")
        return out

    @staticmethod
    def level_at(keys, t):
        if t <= keys[0][0]:
            return keys[0][1]
        if t >= keys[-1][0]:
            return keys[-1][1]
        for (t0, v0), (t1, v1) in zip(keys, keys[1:]):
            if t0 <= t <= t1:
                return v0 if t1 - t0 < 1e-9 else v0 + (v1 - v0) * (t - t0) / (t1 - t0)
        return keys[-1][1]

    @staticmethod
    def offset_at(rec, t):
        mt = rec["move_t"]
        if not mt or t <= mt[0]:
            return 0.0
        j = max(i for i, x in enumerate(mt) if x <= t)
        return rec["move_o"][j] + rec["move_v"][j] * (t - mt[j])

    def apply(self, frame, t):
        """frame：PIL RGB（背景色＋紙の粒）。強さが 0 の項目は通らない"""
        arr = None
        for rec in self.items:
            lv = self.level_at(rec["level"], t)
            if lv <= 1e-4:
                continue
            if arr is None:
                arr = np.asarray(frame, dtype=np.float32).copy()
            if rec["mode"] == "texture":
                tile = rec["tile"]
                period = 2 * H
                o = self.offset_at(rec, t) % period
                i0 = int(np.floor(o))
                fr = o - i0
                idx = (np.arange(H) - i0) % period
                rows = tile[idx] * (1 - fr) + tile[(idx - 1) % period] * fr
                arr += (rows * lv)[:, :, None]
            else:
                arr = arr * (1 - rec["mask"] * lv) + rec["img"] * (rec["mask"] * lv)
        if arr is None:
            return frame
        return Image.fromarray(np.clip(np.rint(arr), 0, 255).astype(np.uint8))
