# -*- coding: utf-8 -*-
"""
kinetic_vertical.py
direction で layout: vertical を指定した行だけに使う、縦組みの組版（段の切り方・禁則・ぶら下げ・縦中横・大きさ）。

字形は OpenType の vert を PIL（raqm）で取る（kinetic._Sprites.glyph_v）。置き換え表には戻さない。
ここは字を描かない純粋な計算だけ（プランを作るときに段を決め、_Cut が大きさを読む）。
自動の構図選びで vertical になった行（従来の縦組み）は、ここを通らない。
"""

import re

SMALL_KANA = set("ぁぃぅぇぉっゃゅょゎァィゥェォッャュョヮヵヶ")
HANG = set("、。")                                    # 段の末尾にぶら下げる（枠の外に 0.5 字まで）。段の頭には置かない
CLOSERS = set("」』）〉》】〕］｝")
NO_START = HANG | CLOSERS | SMALL_KANA | set("ー〜～…‥")   # 段の頭に置かない字
NO_END = set("「『（〈《【〔［｛")                       # 段の末尾に置かない字
TCY_MARKS = set("!?！？")
ENTRANCES = ("cut", "karaoke", "mask", "fall", "float", "grow", "stagger", "scatter")   # 縦組みの行で使える入り（字ごとに動くもの）。direction の検査と描画の両方が見る
PARTICLES = set("をはがのにへでともや")             # 単語の時刻が無いとき、段の頭に来たら警告する助詞
PITCH = 1.02            # 字の送り（字の大きさ × この値）
COL_PITCH = 1.5         # 段の送り（字の大きさ × この値）
USABLE_W = 896          # 左右の余白 92px
DEFAULT_TOP, DEFAULT_H = 200, 1300
DEFAULT_MAX_COL = 12
REF_CHAR = "国"          # 縦組みの字の位置を測る基準の全角字（kinetic._Sprites.glyph_v）
SEP_CHAR = "\u3000"      # 基準字と字の間に挟む全角空白（1マス。縁が基準字に重ならないように）
HANG_EXTENT = 0.5       # ぶら下げた字が枠の外へ出る分（字）


def has_raqm():
    from PIL import features
    return bool(features.check("raqm"))


def need_raqm():
    """縦組みは raqm が要る。無いときは止める（置き換え表に黙って戻らない。環境で字形が変わる）"""
    if not has_raqm():
        from look import LookError
        raise LookError("縦組み（layout: vertical）には PIL の raqm（libraqm・fribidi）が要りますが、この環境にはありません。"
                        "黙って置き換え表に戻さず止めました（README の必要環境）")


def tcy_units(text):
    """縦中横にする2字の組の位置（先頭の添字の集合）。「!?」「!!」「？！」などの2字続き（3字以上の連続は組にしない）と、
    半角数字のちょうど2桁。"""
    out = set()
    for m in re.finditer(r"[!?！？]+", text):
        if len(m.group()) == 2:
            out.add(m.start())
    for m in re.finditer(r"(?<![0-9])[0-9]{2}(?![0-9])", text):
        out.add(m.start())
    return out


def units_of(text):
    """段の文字列を、1マスずつの単位（縦中横は2字で1単位）に分ける。戻り値: [(文字列, 先頭の添字)]"""
    pairs = tcy_units(text)
    out, i = [], 0
    while i < len(text):
        if i in pairs:
            out.append((text[i:i + 2], i))
            i += 2
        else:
            out.append((text[i], i))
            i += 1
    return out


def _char_class(ch):
    if "぀" <= ch <= "ゟ":
        return "hira"
    if "゠" <= ch <= "ヿ":
        return "kata"
    if "一" <= ch <= "鿿":
        return "kanji"
    return "other"


def mid_word_flags(chars, word_of):
    """mid[p]（p = 1..n-1）：p の直前で切ると単語の途中になるか。単語の対応（_match_line の word_of）があればそれ、
    無ければ字種の変わり目・約物の後ろを単語の切れ目とみなす。mid[0] は使わない"""
    n = len(chars)
    mid = [False] * n
    have = bool(word_of)
    for p in range(1, n):
        if have:
            mid[p] = (p in word_of and (p - 1) in word_of and word_of[p] == word_of[p - 1])
        else:
            mid[p] = not (_char_class(chars[p - 1]) != _char_class(chars[p]) or chars[p - 1] in "、。！？…")
    return mid


def _weights(chars):
    """マス数の重み（縦中横の2字目は 0）"""
    w = [1] * len(chars)
    for i in tcy_units(chars):
        w[i + 1] = 0
    return w


def _invalid_breaks(chars):
    """列の頭にしてはいけない位置 p（p の直前で切れない）"""
    bad = set()
    for i in tcy_units(chars):
        bad.add(i + 1)
    # 縦中横の判定は行全体で1回（_Cut は段ごとに units_of を呼ぶので、数字・!? の連なりの途中では切らない）
    for m in re.finditer(r"[0-9]+|[!?！？]+", chars):
        bad.update(range(m.start() + 1, m.end()))
    for p in range(1, len(chars)):
        c, prev = chars[p], chars[p - 1]
        if c in NO_START or prev in NO_END:
            bad.add(p)
        if c == prev and c in "…‥―—":
            bad.add(p)    # 「……」を割らない
    return bad


def col_len(chars, w, a, b):
    """列 [a, b) の字数（マス。ぶら下げた末尾の約物は数えない）"""
    cells = sum(w[a:b])
    if b - a > 1 and chars[b - 1] in HANG:
        cells -= 1
    return cells


def col_extent(chars, w, a, b):
    """列の見た目の長さ（マス。ぶら下げた分は 0.5）"""
    n = col_len(chars, w, a, b)
    if b - a > 1 and chars[b - 1] in HANG:
        n += HANG_EXTENT
    return n


def split_columns(chars, mid, max_col, k):
    """chars を k 列に切る（最適なものを1つ）。単語の途中で切る数 → 最長の列 → 列の長さの二乗和 の順に小さいもの。
    切れない（段数が足りない・禁則で作れない）ときは None。戻り値: [(a, b)]"""
    n = len(chars)
    w = _weights(chars)
    bad = _invalid_breaks(chars)
    INF = (10 ** 9, 10 ** 9, 10 ** 9)
    # f[j][p] = (mid, maxL, sumsq, breaks) 先頭 p 字を j 列に切る
    f = [[None] * (n + 1) for _ in range(k + 1)]
    f[0][0] = (0, 0, 0, ())
    for j in range(1, k + 1):
        for p in range(1, n + 1):
            for a in range(j - 1, p):
                prev = f[j - 1][a]
                if prev is None:
                    continue
                if a > 0 and a in bad:
                    continue
                L = col_len(chars, w, a, p)
                if L > max_col or L <= 0 and p - a > 1:
                    continue
                m = prev[0] + (1 if a > 0 and mid[a] else 0)
                cand = (m, max(prev[1], L), prev[2] + L * L, prev[3] + ((a, p),))
                cur = f[j][p]
                if cur is None or cand[:3] < cur[:3]:
                    f[j][p] = cand
    best = f[k][n]
    return None if best is None else list(best[3])


def choose_columns(chars, mid, max_col):
    """段の切り方を決める。切れる最少の段数 k と k+1 を比べ、単語の途中で切る数が少ないほう（同じなら少ない段）。
    戻り値: [(a, b)]。作れなければ None"""
    n = len(chars)
    sols = []
    for k in range(1, n + 1):
        s = split_columns(chars, mid, max_col, k)
        if s is not None:
            sols.append((k, s))
            if len(sols) == 2:
                break
    if not sols:
        return None

    def mids(s):
        return sum(1 for a, _b in s if a > 0 and mid[a])
    return min(sols, key=lambda ks: (mids(ks[1]), ks[0]))[1]


def size_for(chars, cols, height, cap, usable_w=None):
    """列の組に対する字の大きさ（整数 px）。高さ：最長の列が height に収まる。幅：段の送り 1.5 倍で 896px に収まる"""
    w = _weights(chars)
    ext = max(col_extent(chars, w, a, b) for a, b in cols)
    k = len(cols)
    size_h = height / (max(ext, 1) * PITCH)
    size_w = (usable_w or USABLE_W) / (COL_PITCH * (k - 1) + 1)
    return int(min(cap, size_h, size_w))


def arrange(chars, mid, max_col, height, cap, min_px, forced=None, usable_w=None):
    """段の切り方と大きさを決める。下限（min_px）を割るなら段を増やす（縮小で収めない）。
    forced: 段の切れ目の指定（break_after 済みの段の文字数の並び）。あればそれを使う。
    戻り値: (段の文字列のリスト, 大きさ px)。収まらなければ LookError"""
    from look import LookError

    if forced is not None:
        cols, pos = [], 0
        for ln in forced:
            cols.append((pos, pos + ln))
            pos += ln
        # 指定の段にも、自動の段と同じ検査を通す（禁則・縦中横を割らない・1段の上限）。黙って動かさず止める
        bad = _invalid_breaks(chars)
        w = _weights(chars)
        for a, _b in cols[1:]:
            if a in bad:
                why = (f"「{chars[a]}」は段の頭に置けない" if chars[a] in NO_START else
                       f"「{chars[a - 1]}」は段の末尾に置けない" if chars[a - 1] in NO_END else
                       "縦中横・数字や!?の連なり・「……」の途中は切れない")
                raise LookError(f"縦組み: break_after で指定した段の切れ目（{a}字目の前）が禁則に当たります（{why}）。"
                                f"break_after を 1 字ずらしてください。黙って動かさず止めました")
        for a, b in cols:
            if col_len(chars, w, a, b) > max_col:
                raise LookError(f"縦組み: break_after で指定した段（{b - a}字）が 1段の上限 {max_col}字を超えます。"
                                f"break_after か max_col_chars を見直してください。黙って動かさず止めました")
        size = size_for(chars, cols, height, cap, usable_w)
        if size < min_px:
            raise LookError(f"縦組み: 指定した段（{len(cols)}段）では字が {size}px になり、下限 {min_px}px を割ります。"
                            f"break_after・max_px・min_px を見直してください。黙って縮めず止めました")
        return [chars[a:b] for a, b in cols], size
    first = choose_columns(chars, mid, max_col)
    if first is None:
        raise LookError("縦組み: 禁則を守って段に切れません（max_col_chars を見直してください）")
    last_size = 0
    for k in range(len(first), len(chars) + 1):
        cols = first if k == len(first) else split_columns(chars, mid, max_col, k)
        if cols is None:
            continue
        size = size_for(chars, cols, height, cap, usable_w)
        last_size = size
        if size >= min_px:
            return [chars[a:b] for a, b in cols], size
        if size_for_width_only(k, usable_w) < min_px:
            break
    raise LookError(f"縦組み: 段を増やしても字の下限 {min_px}px を割ります（{last_size}px）。"
                    f"行の字数・max_col_chars・使える高さ（direction の vertical）を見直してください")


def size_for_width_only(k, usable_w=None):
    return int((usable_w or USABLE_W) / (COL_PITCH * (k - 1) + 1))


def row_extent(text):
    """段の文字列の見た目の長さ（マス。縦中横は1マス、ぶら下げは 0.5）"""
    return col_extent(text, _weights(text), 0, len(text))
