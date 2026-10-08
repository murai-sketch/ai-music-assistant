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
import copy
import hashlib
import json
import math
import shutil
from datetime import datetime
from pathlib import Path

import numpy as np
from PIL import Image, ImageDraw, ImageEnhance, ImageFilter, ImageFont

import kinetic_bg
import kinetic_fx
import kinetic_points
import kinetic_vertical

VIDEO_SIZE = (1080, 1920)
FPS = 30
_SCAN_MEMO = {}   # 点の層の走査の結果（同じ入力・同じ範囲なら再利用。T35 R0-12）
IMPACT_P_EPS = 1e-3   # slam（衝撃の段階をもつカット）の p ＝ 0 の判定の許容（フレーム。land の丸め 1e-6 秒 ≒ 3e-5 フレームより十分大きい）

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
    "cut": 0,   # 動かない入り。開始の瞬間に全文が出る（開始より前は出ない）
    "karaoke": 4.5,   # 全文が 0.15 秒で未点灯の濃さまで出る。以後は字ごとの時刻で点灯（direction の曲だけ）
}
EXIT_FRAMES = 3
# 間のある退場（fall / drift）の長さ。カットの3割、0.14〜0.55秒（JIZURA の目安）
EXIT_RATIO, EXIT_MIN_SEC, EXIT_MAX_SEC = 0.3, 0.14, 0.55
# 保持（行が止まっている間の小さな動き）。入りの直後に 0.25 秒かけて立ち上がる
HOLD_MOTIONS = ("breathe", "wave", "jitter")
HOLD_RAMP_SEC = 0.25
# carry（行全体を一定の速さで動かす保持。direction で hold: carry と carry を書いた行だけ。T35 C1）。HOLD_MOTIONS には入れない
# （HOLD_MOTIONS の保持は入りの後 0.25 秒で立ち上がり退場で消える小さな動きで、移動しない。carry は行の頭から一定の速さで動き続ける）
CARRY = "carry"
# carry で動いた後の読み字の上下の余白の下限（px）。計画 T35 §7 の値（左右の余白 SIDE_MARGIN と同じ 92px）。slam 等の上下の検査（VERTICAL_MARGIN＝70）より
# 厳しい安全側の値。92 を採った理由は計画の指定で、画面の上下の UI の帯に入らない根拠の実測は無い（要検証）。変えるときは計画と合わせる
CARRY_MARGIN = 92


def carry_spec(cut, row):
    """行 cut の、段 row（縦組みの列。横組みは段の番号を使わない）の carry の指定 (px_s, 向き符号)。無ければ None。
    向き符号：down＝+1（画面の下へ）、up＝-1"""
    if cut.get("hold") != CARRY or not cut.get("carry"):
        return None
    spec = cut["carry"]
    if "px_s" not in spec:
        spec = spec.get(str(row))
        if spec is None:
            return None
    return float(spec["px_s"]), (1 if spec["dir"] == "down" else -1)


def carry_extent(cut):
    """carry で動く範囲（上へ・下へ、px）。行の表示の長さ（end − start）の間、速さ × 長さだけ動く。縦組みで列ごとに向きが違うときは、
    列ごとの最大を取った保守的な値（空けの矩形・余白の検査用）"""
    if cut.get("hold") != CARRY or not cut.get("carry"):
        return 0.0, 0.0
    dur = max(cut["end"] - cut["start"], 0.0)
    spec = cut["carry"]
    specs = [spec] if "px_s" in spec else list(spec.values())
    up = max([s_["px_s"] * dur for s_ in specs if s_["dir"] == "up"] + [0.0])
    down = max([s_["px_s"] * dur for s_ in specs if s_["dir"] == "down"] + [0.0])
    return float(up), float(down)
MIN_FLASH_GAP = 2.0
MIN_BG_SWITCH_GAP = 0.5
GAP_FOR_REST = 1.2  # これ以上の無歌詞区間は「間」として背景を画像に戻す

INTERLUDE_EFFECTS = ["rgb_split", "glitch", "duotone", "scan", "kaleido"]
INTERLUDE_FADE = 0.35  # 間奏の出入りでエフェクトを強める/弱める秒数
INTERLUDE_MIN = 2.5    # これより短い歌詞の切れ目は間奏として扱わない
# 間奏は小節ごとに背景の絵・カメラ・エフェクトを替える（2026-09-25「歌がない時間が長いと退屈、
# 1小節ずつでも替えたほうがよい」）。1小節がこれより短い速い曲は2小節ずつにする
INTERLUDE_BAR_MIN_SEC = 1.8
INTERLUDE_CAMERAS = ["push_in", "pan_l", "tilt_up", "pull_out", "pan_r", "dutch", "tilt_down"]
# 小節ごとに回すエフェクト（万華鏡は画面が黒い菱形に割れて見えるので外す）
INTERLUDE_BAR_EFFECTS = ["rgb_split", "glitch", "duotone", "scan"]
INTERLUDE_BAR_BRIGHTEN = 1.7  # 背景は文字のために暗く保持しているので、歌の無い間は明るく戻す

# 文字を収める横幅。画面の端はプラットフォームのUI（TikTok の右の操作ボタン、
# 下のユーザー名やキャプション、YouTube ショートの操作列）に隠れる可能性があるので、
# 1080px の画面に対して左右に余白を残す。
SIDE_MARGIN = 92          # テーマを使う曲の左右の余白の下限（px。direction のある曲の検査）
TEXT_WIDTH = 860          # center / diagonal（左右それぞれ約110px の余白）
TEXT_WIDTH_NARROW = 790   # 左右に寄せたレイアウト（寄せた側がより端に近づくため）
# 構図のずらし（描画と、テーマの幅の見積もり _fit_look_rows が同じ値を使う。ここ1か所）
ANCHOR_X = {"left": 0.47, "right": 0.53}   # 文字の中心の画面幅に対する位置（中央からのずれ）
ANCHOR_DY = 0.05                           # left は上へ、right は下へ（画面の高さに対する割合）
ROW_SHIFT_BASE, ROW_SHIFT_STEP = 60, 40    # 段 ri の行頭の位置: left は −BASE＋ri×STEP、right は ＋BASE−ri×STEP
DIAGONAL_DEG = 8                           # diagonal の傾き（度。左上がり）
VERTICAL_MARGIN = 70                       # 文字の外接矩形の上下の余白の下限（px）
WORD_WINDOW_LEAD = 0.3                     # 行の単語の区間は [開始 − この秒数, 次の行の開始)（歌い終わり・点灯・着地が共通で使う）

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


SUNG_CHAIN_GAP_SEC = 1.5   # 単語どうしがこれ以上空いたら、そこで「歌い終わり」とみなす（間奏の誤認識を拾わない）


def sung_ends_from_words(alignment, word_segments, chain_gap=SUNG_CHAIN_GAP_SEC):
    """各行の歌い終わり（秒）を、whisperの単語終了時刻から求める。
    alignment の end は「次の行の start」で埋められていて歌い終わりではないため使えない。
    行の [start-0.3, 次の行のstart) に始まる単語を時刻順に辿り、直前の単語との間が chain_gap を
    超えたところで打ち切る。単語が1つも見つからない行は None（呼び出し側で上限方式に戻す）。"""
    words = sorted((w for seg in word_segments for w in seg.get("words", [])),
                   key=lambda w: w["start"])
    result = []
    for i, item in enumerate(alignment):
        lo = float(item["start"]) - WORD_WINDOW_LEAD
        hi = float(alignment[i + 1]["start"]) if i + 1 < len(alignment) else float("inf")
        last = None
        for w in words:
            if w["start"] < lo:
                continue
            if w["start"] >= hi:
                break
            if last is not None and w["start"] - last > chain_gap:
                break
            last = max(last or 0.0, float(w["end"]))
        result.append(last)
    return result


SUNG_MISMATCH_SEC = 1.0   # 単語の終わり W と音量の終わり D がこれ以上食い違う行は、一覧に印を付ける
VOCAL_WINDOW_SEC, VOCAL_HOP_SEC, VOCAL_RATIO = 0.05, 0.01, 0.04
VOCAL_GAP_SEC = 1.0       # D の探索は、W 以降で最初にこの秒数以上の無音（最大の ratio 未満）が続く手前で打ち切る


def vocal_ends(alignment, vocals_path, cache_dir=None, window=VOCAL_WINDOW_SEC, hop=VOCAL_HOP_SEC,
               ratio=VOCAL_RATIO, w_ends=None, gap=VOCAL_GAP_SEC):
    """各行の歌い終わり D（秒）を、分離音声の音量から求める。
    窓 window 秒・刻み hop 秒の RMS が、曲全体の RMS の最大値 × ratio を超えた最後の刻みの終わり。
    探す区間は [行の開始, 次の行の開始)（最後の行は曲の終わりまで）。ただし w_ends（行ごとの単語の終わり W）があれば、
    W 以降で最初に gap 秒以上の無音が続く手前で探索を打ち切る（間奏・アウトロの歌詞外の声を拾わない）。
    W が None の行は、行の開始から数える。区間内に1つも無ければ None。
    結果は cache_dir/vocal_ends.json にキャッシュする（分離音声のサイズと更新時刻・窓・刻み・閾値・gap・W・行の開始の並びがキー）。"""
    vocals_path = Path(vocals_path)
    starts = [round(float(a["start"]), 3) for a in alignment]
    ws = [None if (w_ends is None or i >= len(w_ends) or w_ends[i] is None) else round(float(w_ends[i]), 3)
          for i in range(len(starts))]
    st = vocals_path.stat()
    key = {"size": st.st_size, "mtime_ns": st.st_mtime_ns, "window": window, "hop": hop,
           "ratio": ratio, "gap": gap, "starts": starts, "w": ws}
    cache_path = Path(cache_dir) / "vocal_ends.json" if cache_dir else None
    if cache_path and cache_path.exists():
        try:
            saved = json.loads(cache_path.read_text(encoding="utf-8"))
            if saved.get("key") == key:
                return saved["ends"]
        except (ValueError, KeyError):
            pass
    import librosa

    y, sr = librosa.load(str(vocals_path), sr=None, mono=True)
    hop_n = max(int(round(sr * hop)), 1)
    rms = librosa.feature.rms(y=y, frame_length=max(int(round(sr * window)), 2), hop_length=hop_n)[0]
    thr = float(rms.max()) * ratio
    step = hop_n / sr
    gap_n = max(int(math.ceil(gap / step)), 1)
    ends = []
    for i, s in enumerate(starts):
        hi = starts[i + 1] if i + 1 < len(starts) else float("inf")
        lo_k = max(int(math.floor(s / step)), 0)
        hi_k = len(rms) - 1 if hi == float("inf") else min(int(math.ceil(hi / step)) - 1, len(rms) - 1)
        if ws[i] is not None:
            # W 以降で、gap 秒以上 thr 以下が続く最初の位置の手前までに絞る
            run_start, run = None, 0
            for k in range(min(max(int(math.floor(ws[i] / step)), lo_k), hi_k + 1), hi_k + 1):
                if rms[k] > thr:
                    run_start, run = None, 0
                else:
                    if run_start is None:
                        run_start = k
                    run += 1
                    if run >= gap_n:
                        hi_k = run_start - 1
                        break
        last = None
        for k in range(hi_k, lo_k - 1, -1):
            if rms[k] > thr:
                last = k
                break
        ends.append(None if last is None else round((last + 1) * step, 3))
    if cache_path:
        cache_path.write_text(json.dumps({"key": key, "threshold": thr, "ends": ends}, ensure_ascii=False) + "\n",
                              encoding="utf-8")
    return ends


def combine_sung_ends(w_ends, d_ends):
    """W（単語）と D（音量）の配列を、build_plan に渡す [{"w":..,"d":..}] にする（D が無ければ W だけ）"""
    n = len(w_ends)
    d_ends = d_ends or [None] * n
    return [{"w": w, "d": d_ends[i] if i < len(d_ends) else None} for i, w in enumerate(w_ends)]


def _sung_end_of(entry):
    """sung_ends の1要素から (E, W, D)。float / None（従来の形）も受ける。E = max(W, D)。"""
    if isinstance(entry, dict):
        w, d = entry.get("w"), entry.get("d")
    else:
        w, d = entry, None
    vals = [v for v in (w, d) if v is not None]
    return (max(vals) if vals else None), w, d


def _exit_frames_of(name, dur):
    """退場にかける長さ（フレーム）。退場の長さの唯一の定義（描画の _Cut._exit_frames と、余韻に収まるかの判定が使う）。
    fall / drift はカットの長さ dur（秒）に応じて伸縮する"""
    if name == "swap":
        return 0
    sec = _exit_fade_sec(name)
    if sec is not None:
        return sec * FPS
    if name in ("fall", "drift"):
        return max(min(dur * EXIT_RATIO, EXIT_MAX_SEC), EXIT_MIN_SEC) * FPS
    return {"fly": 7, "split": 8, "shatter": 10}.get(name, EXIT_FRAMES)


def _exit_len_sec(cut):
    """退場の長さ（秒）"""
    return _exit_frames_of(cut.get("exit"), cut["end"] - cut["start"]) / FPS


def resolve_span(plan, spec, duration=None, where="区間", shown=None):
    """direction の区間指定（背景の補間・間奏・カウンターの出現・素材・点の層）を (開始, 終了) の秒に直す。時刻の解決はここだけ。
    開始：start（秒）／after_line（その行の表示の終わり）。start_at: "exit_start" を足すと after_line の行の退場の頭
    　　　（表示の終わり − 退場の長さ）。（bg_transitions の "from"（配色の名前）とは別の名前にしてある）
    終了：seconds（開始から）／end（秒）／until_line（その行の開始）／until: "end"（曲の長さ。duration が要る）。
    点の層の書き方：lines: [a, b]（行 a の開始〜行 b の終わり）／section: 名前（その区分の最初の行〜最後の行。飛び飛びなら止める）。
    shown（行ごとの表示の終わり。plan と同じ並び）を渡すと、lines・section の終わりは 行の終わりでなく表示の終わり（次の行の見え始めまで延ばした分を含む）。
    空の区間かどうかは呼び出し側が決める（ここでは止めない）"""
    from look import LookError

    if "lines" in spec or "section" in spec:
        if "lines" in spec:
            a, b = spec["lines"]
            idx = list(range(a - 1, b))
        else:
            idx = [j for j, c in enumerate(plan) if c.get("section") == spec["section"]]
            if not idx:
                raise LookError(f"{where}: 区分 '{spec['section']}' の行がありません")
            if idx != list(range(idx[0], idx[-1] + 1)):
                raise LookError(f"{where}: 区分 '{spec['section']}' は飛び飛びです（行 {', '.join(str(plan[j]['index']) for j in idx)}）。"
                                f"lines で範囲を指定してください")
        ends = [max(float(plan[j]["end"]), float(shown[j])) if shown is not None else float(plan[j]["end"]) for j in idx]
        return float(plan[idx[0]]["start"]), max(ends)
    if "start" in spec:
        t0 = float(spec["start"])
    else:
        cut = plan[spec["after_line"] - 1]
        t0 = float(cut["end"])
        if spec.get("start_at") == "exit_start":
            t0 -= _exit_len_sec(cut)
    if "seconds" in spec:
        t1 = t0 + float(spec["seconds"])
    elif "end" in spec:
        t1 = float(spec["end"])
    elif "until_line" in spec:
        t1 = float(plan[spec["until_line"] - 1]["start"])
    elif spec.get("until") == "end":
        if not duration:
            raise LookError(f"{where}の until: end には曲の長さが要ります（書き出し・静止画の呼び出しに duration を渡してください）")
        t1 = float(duration)
    else:
        raise LookError("区間の終わり（seconds・end・until_line・until）がありません")
    return t0, t1


def frame_overlap(a0, a1, b0, b1):
    """半開の2区間 [a0, a1) と [b0, b1)（秒）が、30fps の格子のフレーム（時刻 k/30）のうち何枚で同時に出るか。
    格子の2つのフレームの間に収まる重なり（1フレーム未満）は 0。検査の「同じ時間に出る」はこの数で決める
    （区間の端は延ばした分で格子に乗り、行の開始は格子に乗らない。秒の差で比べると、画面に出ない重なりで止まる）"""
    lo = max(math.ceil(a0 * FPS - 1e-9), math.ceil(b0 * FPS - 1e-9))
    hi = min(math.ceil(a1 * FPS - 1e-9), math.ceil(b1 * FPS - 1e-9))
    return max(hi - lo, 0)


def span_at(span, value):
    """区間の中の時刻。区間の頭からの秒、または "end"・"end-<秒>"（区間の終わりから戻る）"""
    t0, t1 = span
    if isinstance(value, str):
        if value == "end":
            return t1
        if value.startswith("end-"):
            return t1 - float(value[4:])
        raise ValueError(f"区間の時刻の書き方が不正です: {value!r}")
    return t0 + float(value)


def _fit_exit_to_tail(cut):
    """退場の長さ ≦ 余韻（歌い終わりから消えるまでの空き）。判定はここだけ。
    収まらない退場（swap と既定の fade 以外）は、既定の短い退場 fade に落とす（長い退場は歌い終わる前に崩れ始める）。
    既定の fade（3フレーム）も余韻より長いときは落とし先が無いので、そのまま（余韻を 0.1 秒未満にした指定の側の問題）"""
    if cut.get("sung_end") is None or cut.get("exit") in (None, "swap", "fade"):
        return
    if _exit_len_sec(cut) > cut["end"] - cut["sung_end"] + 1e-9:
        cut["exit"] = "fade"


def build_plan(alignment, sections, beats, style, meta=None, backgrounds=None, sung_ends=None,
               tails=None, fixed_ends=None, duration=None):
    """alignment（[{line,start,end}]）と、行ごとの構成タグ名から、
    カットごとの設計を作る。meta（曲ノートの title/genre/tags/bpm）があれば、
    曲の性格に合わせて参考作品由来の技法（kinetic_fx）を割り当てる。

    sung_end のとき: sung_ends は 行ごとの歌い終わり（float / None、または {"w","d"}）。E = max(W, D)。
    tails は行ごとの余韻（秒、None なら style の tail_sec）、fixed_ends は行ごとの絶対時刻の終わり（None なら E＋余韻）。
    duration は曲の長さ（最後の行の上限。無ければ alignment の end）。"""
    max_hold = style.get("max_hold_sec", 2.8)
    hold_mode = style.get("hold_mode", "cap")
    tail_sec = style.get("tail_sec", 0.6)
    profile = kinetic_fx.song_profile(alignment, sections, beats, meta)
    if profile.get("kids") and hold_mode == "cap":
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
        if hold_mode == "sung_end" and i + 1 >= len(alignment) and duration is not None and duration > next_start:
            next_start = float(duration)   # 最後の行は曲の終わりまで残せる（alignment の end で切らない）
        show_end = min(next_start, start + max_hold)
        sung_e = sung_w = sung_d = None
        if hold_mode == "sung_end":
            # 歌い終わり E ＋ 余韻まで残す（E = max(W, D)）。余韻は 行の指定 → style の tail_sec の順。
            # 歌っている間は文字が完全に見える。歌い終わりが取れない行は上限方式に戻す。
            se = sung_ends[i] if sung_ends and i < len(sung_ends) else None
            sung_e, sung_w, sung_d = _sung_end_of(se)
            tail = tails[i] if tails and i < len(tails) and tails[i] is not None else tail_sec
            if sung_e is not None:
                show_end = min(next_start, max(sung_e, start) + tail)
            show_end = max(show_end, min(start + 0.3, next_start))
            if fixed_ends and i < len(fixed_ends) and fixed_ends[i] is not None:
                show_end = max(min(next_start, float(fixed_ends[i])), min(start + 0.3, next_start))
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
        if hold_mode == "sung_end" and i > 0:
            prev_gap = start - plan[-1]["end"]
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
        if hold_mode == "sung_end":
            plan[-1]["sung_end"] = None if sung_e is None else round(sung_e, 3)
            plan[-1]["sung_w"] = None if sung_w is None else round(sung_w, 3)
            plan[-1]["sung_d"] = None if sung_d is None else round(sung_d, 3)

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
    if hold_mode == "sung_end":
        # 退場の長さ ≦ 余韻（判定は _fit_exit_to_tail の1か所）。cap モード（既存の曲）は何もしない
        for cut in plan:
            _fit_exit_to_tail(cut)
    return plan


def _fmt(v):
    return "" if v is None else f"{v:.2f}"


def plan_to_markdown(plan, header=None):
    """カット一覧。歌い終わりまで残す方式（sung_end）のプランには W・D・E と食い違い印の列を、
    direction のあるプランには声・余韻の列を足す。既定のプランの出力は変えない。"""
    sung = any("sung_end" in c for c in plan)
    voiced = any("voice" in c for c in plan)
    looked = any("font_role" in c for c in plan)
    staged = any(k in c for c in plan for k in ("impact", "char_times", "karaoke_all_lit", "break_after", "marks", "counter", "solo", "vertical_typeset", "carry", "stack_group", "row_lengths"))
    head = "| # | 時間 | 強さ | 構図 | 動き | 背景 | カメラ | 装飾 | 質感 | 保持 | 退場 | フラッシュ | 背景処理 | 下敷き | 切替 |"
    rule = "|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|"
    if voiced:
        head += " 声 | 余韻 |"
        rule += "---|---|"
    if looked:
        head += " 役 | 差し色 |"
        rule += "---|---|"
    if staged:
        head += " 段3 |"
        rule += "---|"
    if sung:
        head += " W | D | E | 印 |"
        rule += "---|---|---|---|"
    out = list(header or []) + [head, rule]
    for c in plan:
        bg = c["bg"] if isinstance(c["bg"], str) else f"色{c['bg']}"
        row = (
            f"| {c['index']} | {c['start']:.2f}–{c['end']:.2f} | {c['level']} | {c['layout']} | "
            f"{c['motion']} | {bg} | {c.get('camera', '')} | {c.get('decor') or ''} | "
            f"{c.get('texture') or ''} | {c.get('hold') or ''} | {c.get('exit') or ''} | {'●' if c['flash'] else ''} | "
            f"{c.get('bgfx') or ''} | {c.get('under') or ''} | {c.get('wipe') or ''} |"
        )
        if voiced:
            row += f" {c.get('voice') or ''} | {_fmt(c.get('tail'))} |"
        if looked:
            acc = c.get("accent_mode") or ""
            if acc == "rows":
                acc = "rows 段" + ",".join(str(x) for x in c.get("accent_rows") or [])
            row += f" {c.get('font_role') or ''} | {acc} |"
        if staged:
            bits = []
            if c.get("impact"):
                bits.append(f"衝撃 {c['impact']} 着地 {c['land']:.2f}")
            if c.get("karaoke_all_lit"):
                bits.append("全文点灯")
            elif c.get("char_times"):
                bits.append(f"点灯 対応{c.get('karaoke_cover', 0):.0%}")
            if c.get("break_after"):
                bits.append("改行 " + ",".join(f"{k}:{v}" for k, v in sorted(c["break_after"].items())))
            if c.get("counter") is not None:
                sp = c["counter"]
                if isinstance(sp, str):
                    bits.append(f"カウンター {sp}")
                else:
                    bits.append("カウンター " + ",".join(
                        f"{k}={'割れ' if k == 'break' else v}" for k, v in sp.items()))
            if c.get("solo"):
                bits.append("solo")
            if c.get("vertical_typeset"):
                bits.append(f"縦組み(vert) {len(c['rows'])}段 {c.get('vertical_size')}px")
                if c.get("row_roles"):
                    bits.append("段の役 " + ",".join(f"段{k}:{v}" for k, v in sorted(c["row_roles"].items(), key=lambda kv: int(kv[0]))))
            if c.get("align_to_prev"):
                bits.append("前の行の先頭字にそろえる")
            if c.get("row_lengths"):
                bits.append("段の字数 " + ",".join(str(x) for x in c["row_lengths"]))
            if c.get("carry"):
                cs = c["carry"]
                bits.append("carry " + (f"{cs['px_s']:g}px/秒 {cs['dir']}" if "px_s" in cs else
                                        ",".join(f"段{k}:{v['px_s']:g}px/秒 {v['dir']}" for k, v in sorted(cs.items()))))
            if c.get("stack_group") is not None:
                bits.append(f"列積み(dim {c['stack_dim']:g}・行{c['stack_clear_line']}の開始で全部消す・x {c.get('stack_dx', 0):+.1f}px)")
            bits.extend(c.get("marks") or [])
            row += " " + "；".join(bits) + " |"
        if sung:
            w, d = c.get("sung_w"), c.get("sung_d")
            mark = "W≠D" if (w is not None and d is not None and abs(w - d) >= SUNG_MISMATCH_SEC) else ""
            row += f" {_fmt(w)} | {_fmt(d)} | {_fmt(c.get('sung_end'))} | {mark} |"
        out.append(row)
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


def _exit_fade_sec(name):
    """exit が "fade:<秒>" なら、その秒数。それ以外は None（"fade" 単体は既定の退場）"""
    if isinstance(name, str) and name.startswith("fade:"):
        return max(float(name.split(":", 1)[1]), 1.0 / FPS)
    return None


class _Fonts:
    def __init__(self):
        self._cache = {}

    def get(self, path, size):
        # 文字列のパスは従来どおり（キーも読み方も変えない）。look.FontRef は TTC の番号を渡す
        key = (path, size)
        if key not in self._cache:
            if isinstance(path, tuple):
                self._cache[key] = ImageFont.truetype(path[0], size, index=path[1])
            else:
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

    def glyph_v(self, unit, font_path, size, fill, stroke, stroke_w, kana_shift=0.0):
        """縦組み用の1マス（direction で layout: vertical を指定した行だけ）。返り値は glyph と同じ形 (画像, l, t, 送り) だが、
        l・t は字の枠（size × size のマス）の左上からのインクの位置（負もありうる）。ttb の getbbox は字のインクでなく枠を返すので、
        枠の大きさの画面に描いてからインクで切り抜き、枠の中の位置を控える。字形は OpenType の vert（raqm）。
        unit: 1字、または縦中横の2字。—（U+2014）は vert で替わらないので横に描いて90°回す。
        kana_shift: 小書きの仮名を右上へ寄せる量（字の大きさの割合。既定 0）"""
        key = ("v", unit, font_path, size, fill, stroke, stroke_w, kana_shift)
        g = self.base.get(key)
        if g is not None:
            return g
        kinetic_vertical.need_raqm()
        font = self.fonts.get(font_path, size)
        fillc, strokec = _hex(fill) + (255,), _hex(stroke) + (255,)
        cw = size * 3
        canvas = Image.new("RGBA", (cw, 5 * size + size // 2), (0, 0, 0, 0))
        d = ImageDraw.Draw(canvas)
        ox = oy = None     # インクを置く枠の中の位置（None なら縦に描いた画の位置から求める）
        if len(unit) == 2:
            # 縦中横：横に描いて、枠の幅に合わせて横だけ縮める。枠の中央に置く
            d.text((size, size), unit, font=font, fill=fillc, stroke_width=stroke_w, stroke_fill=strokec)
            bb = canvas.getchannel("A").getbbox()
            im = canvas.crop(bb) if bb else Image.new("RGBA", (1, 1), (0, 0, 0, 0))
            if im.width > size:
                im = im.resize((size, im.height), Image.LANCZOS)
            ox, oy = (size - im.width) / 2, (size - im.height) / 2
        elif unit == "\u2014":
            d.text((size, size), unit, font=font, fill=fillc, stroke_width=stroke_w, stroke_fill=strokec)
            bb = canvas.getchannel("A").getbbox()
            im = canvas.crop(bb).rotate(-90, expand=True) if bb else Image.new("RGBA", (1, 1), (0, 0, 0, 0))
            ox, oy = (size - im.width) / 2, (size - im.height) / 2
        else:
            # PIL の ttb は、列に含まれる字の横の箱を合わせた範囲の中心を軸にする。1字だけで描くと、字形が右へ寄った約物（、。っ）が
            # 中央に戻り、基準字と組むと「基準字ごと」列が左へずれる。そこで 基準字（国）・全角空白・描く字 の列を描き、
            # 基準字だけで描いたときの位置との差 dx（＝列のずれ）を測って引く（基準字の位置で合わせる）。
            # 基準字と字は全角空白の1マスで離れるので、字の縁が基準字に重ならず、引き算も切り取りも要らない（縁は枠の外へ出てよい）。
            ref = kinetic_vertical.REF_CHAR
            sep = kinetic_vertical.SEP_CHAR
            split = int(size * 2.5)                      # 基準字（〜2.5マス）と字（3マス〜）の境の行
            rk = ("vref", font_path, size, stroke_w)
            rb = self.base.get(rk)
            if rb is None:
                refcv = Image.new("RGBA", canvas.size, (0, 0, 0, 0))
                ImageDraw.Draw(refcv).text((cw / 2, size), ref, font=font, fill=fillc, stroke_width=stroke_w, stroke_fill=strokec,
                                           direction="ttb", features=["vert"], anchor="mt")
                spcv = Image.new("L", canvas.size, 0)
                ImageDraw.Draw(spcv).text((cw / 2, size), sep, font=font, fill=255, direction="ttb", features=["vert"], anchor="mt")
                if spcv.getbbox() or not refcv.getchannel("A").getbbox():
                    from look import LookError
                    raise LookError(f"縦組み: この書体には全角空白（U+3000）か基準字「{ref}」の字形がありません。字の位置を測れないので止めました")
                rb = self.base[rk] = refcv.getchannel("A").getbbox()[0]
            d.text((cw / 2, size), ref + sep + unit, font=font, fill=fillc, stroke_width=stroke_w, stroke_fill=strokec,
                   direction="ttb", features=["vert"], anchor="mt")
            alpha = canvas.getchannel("A")
            top = alpha.crop((0, 0, cw, split)).getbbox()
            dx = (top[0] - rb) if top else 0            # 列が基準字だけのときより右へずれた量
            low = canvas.crop((0, split, cw, canvas.height))
            bb = low.getchannel("A").getbbox()
            if bb:
                im = low.crop(bb)
                ox, oy = bb[0] - dx - (cw / 2 - size / 2), split + bb[1] - 3 * size
                if unit in kinetic_vertical.SMALL_KANA and kana_shift:
                    ox += kana_shift * size
                    oy -= kana_shift * size
            else:
                im = Image.new("RGBA", (1, 1), (0, 0, 0, 0))
                ox = oy = size / 2
        g = (im, ox, oy, size * kinetic_vertical.PITCH)
        self.base[key] = g
        return g

    def inked(self, g, key, ink):
        """読み字に質感（かすれ）を掛けた1字。g は glyph・glyph_v の返り値（画像, l, t, 送り）、key はその字のキー。
        字の画像の不透明度（縁を含む）のインクを抜く。雑音は (字・画像の大きさ) で固定（色・時間は含まない）。
        既存の glyph のキーは変えない（ink の指定が無い行はここを通らない）。戻り値は (g, key) と同じ形"""
        ikey = ("ink", key, ink["mode"], ink.get("amount"))
        hit = self.base.get(ikey)
        if hit is None:
            img = g[0]
            a = kinetic_points.ink_alpha(img.getchannel("A"), ink, (str(key[1] if key[0] == "v" else key[0]), img.size[0], img.size[1], 0))
            im2 = img.copy()
            im2.putalpha(a)
            hit = (im2, g[1], g[2], g[3])
            self.base[ikey] = hit
        return hit, ikey

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

    def transformed(self, key, img, scale, angle, alpha, crop_top, crop_right, alpha_step=10):
        """alpha_step: 不透明度の刻み（既定 10 ＝ 0.1 刻み）。karaoke だけ 20（0.05 刻み）。
        0.65 は 0.1 刻みだと 0.6 に丸められる（round(6.5) == 6）ので、細かい刻みを渡す"""
        sq = round(scale / 0.02) * 0.02
        aq = round(angle)
        alq = round(alpha * alpha_step) / alpha_step
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


def shatter_pieces(im, q, seed):
    """1枚の画像を4片に割り、片ごとに違う向きへ飛ばして落とす。q は 0〜1 の進み。
    戻り値: [(片の画像, 片の左上の x, 片の左上の y, x のずれ, y のずれ)]。貼る位置は int(基準 x + x0 + ox)（足す順を変えない）。
    seed は 乱数の種（文字の退場では 字の順×4＋カット番号。片 k は seed + k）。文字の退場とカウンターの割れの両方が使う"""
    w_, h_ = im.size
    mx, my = w_ // 2, h_ // 2
    out = []
    for k, (x0, y0, x1, y1, sx, sy) in enumerate((
            (0, 0, mx, my, -1, -1), (mx, 0, w_, my, 1, -1),
            (0, my, mx, h_, -1, 1), (mx, my, w_, h_, 1, 1))):
        if x1 <= x0 or y1 <= y0:
            continue
        piece = im.crop((x0, y0, x1, y1))
        j = kinetic_fx._hash01(seed + k)
        ox = sx * (30 + 150 * j) * q * q
        oy = sy * (20 + 90 * (1 - j)) * q * q + 150 * q * q * q
        out.append((piece, x0, y0, ox, oy))
    return out


class _Cut:
    """1カット分の文字配置（基準サイズ・回転なしの状態）を持つ。"""

    def __init__(self, cut, sprites, colors, palette_bg, beats=None, look=None):
        """look: テーマの役を当てるカットだけ渡す（_look_context の結果）。None なら従来どおり"""
        self.cut = cut
        self.look_notes = []
        if look is not None:
            look = dict(look, tracking=float(cut.get("tracking", look["tracking"])))   # 行ごとの字間の上書き
        self._shared = sprites
        self.colors = colors
        self.beats = beats or []
        text_color, stroke_color, accent = colors
        level = cut["level"]
        rows = cut["rows"]
        vertical = cut["layout"] == "vertical"
        vt = vertical and bool(cut.get("vertical_typeset"))   # direction で指定した縦組み（新しい組版。従来の縦組みは vt=False のまま）
        if vt and (cut.get("profile_kids") or cut.get("entrance") == "neon"):
            raise RuntimeError(f"行{cut['index']}: 縦組み（vert）は子ども向けの書体・neon の入りには使えません")
        if vt and cut.get("entrance", "cut") not in kinetic_vertical.ENTRANCES:
            raise RuntimeError(f"行{cut['index']}: 縦組み（vert）では入り '{cut.get('entrance')}' は使えません"
                               f"（使える入り: {', '.join(kinetic_vertical.ENTRANCES)}）")
        kana_shift = float(cut.get("vertical_kana_shift", 0.0))
        font_path = FONT_QUIET if level == 1 else FONT_HEAVY
        if cut.get("profile_kids"):
            font_path = FONT_KIDS
        if look is not None:
            font_path = look["font"]   # look.FontRef（パスと TTC の番号）
        if cut.get("profile_kids"):
            accent = _readable(accent, palette_bg, text_color)
            colors = (text_color, stroke_color, accent)
        emphasis = bool(cut.get("emphasis"))
        neon = cut.get("entrance") == "neon"
        self.overlay = None
        # karaoke（歌い進みの点灯）。未点灯の濃さは配色の組の指定が先、無ければ direction の karaoke
        self.karaoke = cut.get("entrance") == "karaoke" and look is not None
        kcfg = cut.get("karaoke") or {}
        self.unlit = float((look or {}).get("unlit_opacity") or kcfg.get("unlit_opacity", 0.65))
        # 入替の行（前の行の表示が次の行の開始まで続く行）は、0.15 秒かけず 0 フレームで未点灯の濃さで出す（動き §10-8 ①）
        self.hard_in = bool((look or {}).get("hard_in"))
        self.light_frames = int(kcfg.get("light_frames", 3))
        self.glow = None
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

        if vt:
            # 縦組み（vert）：段の切り方と大きさはプランを作るときに決めてある（kinetic_vertical.arrange）。全段を同じ大きさに
            sizes = [int(cut["vertical_size"])] * len(rows)
        elif look is not None:
            # 役の書体：字間・行送り・上限下限 px。下限を割ったら改行を増やして組み直す（縮小で収めない）。
            # 全段を同じ大きさにする（段ごとの大きさの差は使わない）
            plan_rows = len(rows)
            rows, size_each, self.look_notes = self._fit_look_rows(rows, look, cut, sprites)
            if (cut.get("accent_mode") == "rows" or cut.get("row_lengths")) and len(rows) != plan_rows and look.get("strict"):
                from look import LookError
                raise LookError(f"行{cut['index']}: accent: rows／row_lengths の行の段が、描くときに {plan_rows} 段から {len(rows)} 段に増えました"
                                f"（幅・下限 px のため）。段が指定とずれるので止めました。max_px・min_px・字数を見直してください")
            sizes = [size_each] * len(rows)
        elif vertical or cut.get("profile_kids"):
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

        self.read_ink = cut.get("read_ink")    # 読み字の質感（direction の行の ink。kasure だけ）
        if self.read_ink:
            if self.karaoke:
                raise RuntimeError(f"行{cut['index']}: 読み字の ink は karaoke の行には掛けません（未点灯の濃さの基準が崩れる）")
            if kids_style or neon:
                raise RuntimeError(f"行{cut['index']}: 読み字の ink は子ども向けの書体・neon の入りには掛けません")

        def make_glyph(draw_ch, fpath, gsize, fill, stroke, sw):
            if self.read_ink:
                if gsize < kinetic_points.READ_INK_MIN_PX:
                    raise RuntimeError(f"行{cut['index']}: 読み字の ink は字 {kinetic_points.READ_INK_MIN_PX}px 以上だけです（この行は {gsize}px）。"
                                       f"小さい字にかすれを掛けると読めない")
                if vt:
                    g0, k0 = (sprites.glyph_v(draw_ch, fpath, gsize, fill, stroke, sw, kana_shift),
                              ("v", draw_ch, fpath, gsize, fill, stroke, sw, kana_shift))
                else:
                    g0, k0 = sprites.glyph(draw_ch, fpath, gsize, fill, stroke, sw), (draw_ch, fpath, gsize, fill, stroke, sw)
                return sprites.inked(g0, k0, self.read_ink)
            if kids_style:
                return sprites.outlined(draw_ch, fpath, gsize, fill, "#FFFFFF"), ("outlined", draw_ch, fpath, gsize, fill)
            if neon:
                return sprites.neon(draw_ch, fpath, gsize, accent if fill == accent else "#FF5FA2"), ("neon", draw_ch, fpath, gsize, fill)
            if vt:
                return (sprites.glyph_v(draw_ch, fpath, gsize, fill, stroke, sw, kana_shift),
                        ("v", draw_ch, fpath, gsize, fill, stroke, sw, kana_shift))
            return sprites.glyph(draw_ch, fpath, gsize, fill, stroke, sw), (draw_ch, fpath, gsize, fill, stroke, sw)

        # 段ごとに、最後の段（またはHookの末尾2文字）をアクセント色にする
        cut_accent_idx = set(cut.get("accent_idx") or [])
        glyphs = []
        order = 0
        flat_i = 0
        row_hs = [sz * (look["leading"] if look is not None else 1.1) for sz in sizes]
        total_h = sum(row_hs)
        y_cursor = -total_h / 2
        if vt:
            vt_top = -max(kinetic_vertical.row_extent(r) for r in rows) * sizes[0] * kinetic_vertical.PITCH / 2   # 段の頭をそろえる
        for ri, row in enumerate(rows):
            rsize = sizes[ri]
            rg_ = cut.get("row_gaps")
            row_gap_idx = set(rg_[ri]) if (rg_ and ri < len(rg_) and not vertical) else set()
            stroke_w = max(rsize // 24, 3) if palette_bg is None else max(rsize // 40, 2)
            row_h = rsize * 1.12
            accent_row = len(rows) >= 2 and ri == len(rows) - 1
            if vt:
                pass
            elif vertical:
                x0 = -(len(rows) - 1) * row_h / 2 + (len(rows) - 1 - ri) * row_h - rsize / 2
                y = -len(row) * rsize * 1.02 / 2
            else:
                def csize(ch):
                    if emphasis and not kinetic_fx._is_kanji(ch):
                        return int(rsize * 0.66)
                    return rsize
                row_w = sum(sprites.fonts.get(font_path, csize(ch)).getlength(ch) for ch in row)
                if look is not None:
                    row_w += look["tracking"] * rsize * (len(row) - 1)
                row_w += 0.5 * rsize * len(row_gap_idx)      # 段の中の全角スペース（0.5字。row_lengths。T35 U11）
                # 弧に沿わせるときの半径。段が長いほど緩い弧にして、端が落ちすぎないようにする
                arc_r = max(row_w * 1.5, 700.0) if cut["layout"] == "arc" else 0.0
                if cut["layout"] == "left":
                    x = -row_w / 2 - ROW_SHIFT_BASE + ri * ROW_SHIFT_STEP
                elif cut["layout"] == "right":
                    x = -row_w / 2 + ROW_SHIFT_BASE - ri * ROW_SHIFT_STEP
                else:
                    x = -row_w / 2
                y0 = y_cursor
                y_cursor += row_hs[ri]
            cells = kinetic_vertical.units_of(row) if vt else [(c_, i_) for i_, c_ in enumerate(row)]
            for ci, (ch, ci0) in enumerate(cells):
                unit_first = flat_i
                if row_gap_idx and ci0 in row_gap_idx:
                    x += 0.5 * rsize          # 段の中の全角スペース分の空き（0.5字）
                if look is not None:
                    # テーマの差し色の付け方（accent_mode）。段の最後・末尾2文字を差し色にする既存の規則は使わない
                    mode = cut.get("accent_mode", "none")
                    is_accent = mode == "fill" or (mode in ("key_word", "rows") and any((flat_i + d) in cut_accent_idx for d in range(len(ch))))
                elif emphasis:
                    is_accent = flat_i in emph_idx
                else:
                    if latin:
                        # 欧文は末尾2文字ではなく最後の1語を差し色に
                        is_accent = accent_row or (level == 3 and len(rows) == 1 and " " in row and ci0 > row.rfind(" "))
                    else:
                        is_accent = accent_row or (level == 3 and len(rows) == 1 and len(row) >= 4 and ci0 >= len(row) - 2)
                flat_i += len(ch)
                fill = accent if is_accent else text_color
                stroke = stroke_color
                if is_accent and palette_bg is not None and _hex(accent) == _hex(stroke_color):
                    stroke = text_color
                draw_ch = ch if vt else VERTICAL_MAP.get(ch, ch) if vertical else ch
                gsize = rsize if vertical else csize(ch)
                gsw = max(gsize // 24, 3) if palette_bg is None else max(gsize // 40, 2)
                if look is not None:
                    stroke, gsw = self._look_stroke(look, cut, gsize, fill, accent, palette_bg)
                fpath = ((look or {}).get("row_fonts") or {}).get(ri, font_path)
                g, key = make_glyph(draw_ch, fpath, gsize, fill, stroke, gsw)
                alt = None
                if self.karaoke and is_accent:
                    # 差し色の字は、未点灯のあいだ本文色で描くので、本文色のスプライトも持つ（点灯の 3 フレームで入れ替える）
                    st2, gsw2 = self._look_stroke(look, cut, gsize, text_color, accent, palette_bg)
                    alt_g, alt_key = make_glyph(draw_ch, fpath, gsize, text_color, st2, gsw2)
                    alt = (alt_key, alt_g[0])
                img, l, t, adv = g
                w, h = img.size
                if vt:
                    # 字の枠（マス）で揃える。マスの中心から、枠の中のインクの位置（l・t）の分だけずらした画像の中心
                    cx = ((len(rows) - 1) / 2 - ri) * rsize * kinetic_vertical.COL_PITCH + (l + w / 2 - rsize / 2)
                    cy = vt_top + (ci + 0.5) * rsize * kinetic_vertical.PITCH + (t + h / 2 - rsize / 2)
                    angle = 0
                elif vertical:
                    cx = x0 + rsize / 2
                    cy = y + rsize * 1.02 * ci + rsize / 2
                    angle = 0
                else:
                    drop = (rsize - gsize) * 0.78
                    cx = x + l + w / 2
                    cy = y0 + t + h / 2 + drop
                    x += adv + (look["tracking"] * gsize if look is not None else 0)
                    angle = 0
                    if arc_r:
                        # 行の中心からの距離を角度に読み替え、円周上へ。文字も接線の向きに傾ける
                        # （cx は行の中心が 0。ここに row_w/2 を足すと行ごと片側へ寄る）
                        theta = cx / arc_r
                        cx = arc_r * math.sin(theta)
                        cy = cy - arc_r * (1 - math.cos(theta)) * (1 if ri % 2 == 0 else -1)
                        angle = -math.degrees(theta) * (1 if ri % 2 == 0 else -1)
                mis = []
                for mc in ([] if vt else mis_colors):
                    mg = sprites.glyph(draw_ch, font_path, gsize, mc, mc, gsw)
                    mis.append(((draw_ch, font_path, gsize, mc, mc, gsw), mg[0]))
                glyphs.append({
                    "key": key, "img": img, "cx": cx, "cy": cy, "order": order, "row": ri,
                    "angle": angle, "mis": mis,
                })
                if vt:
                    glyphs[-1]["ct"] = unit_first + len(ch) - 1   # karaoke の点灯は、縦中横の2字のうち遅いほうの時刻
                if alt is not None:
                    glyphs[-1]["alt"] = alt
                if look is not None and cut.get("accent_mode") == "glow":
                    glyphs[-1]["glow_src"] = (draw_ch, gsize)
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
        if look is not None and cut.get("accent_mode") == "glow" and glyphs:
            self._build_glow(look, font_path, accent)
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
            self.anchor = (VIDEO_SIZE[0] * ANCHOR_X["left"], VIDEO_SIZE[1] * (ty - ANCHOR_DY))
        elif cut["layout"] == "right":
            self.anchor = (VIDEO_SIZE[0] * ANCHOR_X["right"], VIDEO_SIZE[1] * (ty + ANCHOR_DY))
        elif vt:
            self.anchor = (VIDEO_SIZE[0] / 2 + float(cut.get("stack_dx", 0.0)), float(cut.get("vertical_top", kinetic_vertical.DEFAULT_TOP))
                           + float(cut.get("vertical_h", kinetic_vertical.DEFAULT_H)) / 2)
        elif cut["layout"] == "vertical":
            self.anchor = (VIDEO_SIZE[0] * (0.68 if cut["index"] % 2 else 0.32), VIDEO_SIZE[1] * (ty - 0.04))
        else:
            self.anchor = (VIDEO_SIZE[0] / 2, VIDEO_SIZE[1] * ty)
        if self.glyphs:
            # 画面の上下からはみ出さないよう、文字の中心位置を戻す
            _x0, y0, _x1, y1 = self._bounds()
            ax, ay = self.anchor
            margin = VERTICAL_MARGIN
            if ay + y0 < margin:
                ay = margin - y0
            if ay + y1 > VIDEO_SIZE[1] - margin:
                ay = VIDEO_SIZE[1] - margin - y1
            self.anchor = (ax, ay)
        self.base_angle = -DIAGONAL_DEG if cut["layout"] == "diagonal" else 0
        if cut.get("entrance") == "stamp":
            self.base_angle = -4 if cut["index"] % 2 else 3
        if cut["layout"] == "grid":
            self.base_angle = -6 if cut["index"] % 2 else 5

        # 背景に敷く巨大な文字
        echo_text = max(rows, key=len)
        echo_color = _hex(text_color) if palette_bg is not None else (255, 255, 255)
        self.echo = sprites.echo(echo_text, echo_color)

    @staticmethod
    def _look_stroke(look, cut, gsize, fill, accent, palette_bg):
        """役の縁の指定から (縁の色, 縁の太さ px) を決める。
        accent: outline（差し色の縁 3px）／層が重なるとき（cut["layered"]）は背景色の縁（layer_outline）／
        thicken（below_px 以下の大きさで、文字と同じ色の縁で太らせる）／なし"""
        if cut.get("accent_mode") == "outline":
            return accent, 3
        lo = look.get("layer_outline")
        if lo and cut.get("layered"):
            return palette_bg, int(lo["px"])
        th = look.get("thicken")
        if th and gsize <= th["below_px"]:
            return fill, int(th["px"])
        return fill, 0

    @staticmethod
    def _look_stroke_extra(look, cut):
        """縁が字送りの外へ出る分（行の幅の見積もりに足す。片側 px）"""
        extra = 0
        if cut.get("accent_mode") == "outline":
            extra = 3
        if look.get("layer_outline") and cut.get("layered"):
            extra = max(extra, int(look["layer_outline"]["px"]))
        if look.get("thicken"):
            extra = max(extra, int(look["thicken"]["px"]))
        return extra

    def _fit_look_rows(self, rows, look, cut, sprites):
        """役の書体で、段ごとの大きさを揃えて決める。戻り値: (段, 大きさ px, 注記の一覧)。
        大きさ = min(上限 px, 全段が幅に収まる最大)。下限 px（行の min_px が先、無ければ役の min_px）を割るときは、
        長い段を _chunk で折って段を増やし、下限以上で収まる最初の割り方を採る（break_after は plan 時に当て済み）。
        それでも割るなら、direction のある曲（look["strict"]）は止める。無い曲（--look だけ）は今までどおり注記して描く。
        幅：left / right の構図は、段ごとのずらしと中心のずれの分だけ使える幅が狭い（余白 92px を割らない）。
        diagonal は回転後の外接矩形の幅で確かめる。center は今までどおり"""
        from look import LookError

        fonts = sprites.fonts
        font = look["font"]
        track = look["tracking"]
        budget = look["text_width"] - 2 * self._look_stroke_extra(look, cut)
        cap = int(cut.get("max_px") or look["max_px"])
        min_px = int(cut.get("min_px") or look["min_px"])
        layout = cut["layout"]
        strict = bool(look.get("strict"))
        SIDE = SIDE_MARGIN
        # slam の行は、寄り・画面の揺れ・字の揺れの分を先に引いた幅・高さで組む（演出 §9-12）。
        # 幅の上限 ＝ (896 − 2×画面の揺れ) ÷ (1＋寄り) − 2×字の揺れ
        # 高さの上限 ＝ (1780 − 2×画面の揺れ) ÷ (1＋寄り) − 2×字の揺れ
        # 画面の揺れは寄りの外、字の揺れは寄りの内側で引く。寄りは段階の値のまま（頭打ちしない）
        iv = cut.get("impact_vals")
        z_nom = float(iv["zoom"]) if iv else 0.0
        shake = float(iv["screen_shake_px"]) if iv else 0.0
        gshake = float(iv.get("glyph_shake_px", 0)) if iv else 0.0
        usable_w = VIDEO_SIZE[0] - 2 * SIDE
        usable_h = VIDEO_SIZE[1] - 140
        if iv:
            usable_w = (usable_w - 2 * shake) / (1.0 + z_nom) - 2 * gshake
            usable_h = (usable_h - 2 * shake) / (1.0 + z_nom) - 2 * gshake
            budget = min(budget, usable_w)

        row_gaps = cut.get("row_gaps")

        def width(row, s, ri=None):
            f = fonts.get(font, s)
            extra = 0.5 * s * len(row_gaps[ri]) if (row_gaps and ri is not None and ri < len(row_gaps)) else 0.0   # 段の中の全角スペース（0.5字。T35 U11）
            return sum(f.getlength(ch) for ch in row) + track * s * (len(row) - 1) + extra

        def row_budget(ri):
            if layout not in ("left", "right"):
                return budget
            # 段 ri の中心の画面中央からのずれ（描画と同じ定数。left は x = -w/2 - BASE + ri*STEP、right は逆）
            off = (VIDEO_SIZE[0] * ANCHOR_X[layout] - VIDEO_SIZE[0] / 2) \
                + (-ROW_SHIFT_BASE + ri * ROW_SHIFT_STEP if layout == "left" else ROW_SHIFT_BASE - ri * ROW_SHIFT_STEP)
            return min(budget, usable_w - 2 * abs(off))

        def fit_row(row, ri):
            bud = row_budget(ri)
            lo, hi = 1, cap
            while lo < hi:
                mid = (lo + hi + 1) // 2
                if width(row, mid, ri) <= bud:
                    lo = mid
                else:
                    hi = mid - 1
            return lo

        def fit(rs):
            size = min(fit_row(r, i) for i, r in enumerate(rs))
            if layout == "diagonal":
                # 回転（-8°）後の外接矩形の幅が収まるまで下げる
                rad = math.radians(DIAGONAL_DEG)
                while size > 1:
                    w_max = max(width(r, size, i_) for i_, r in enumerate(rs))
                    h_all = len(rs) * size * look["leading"]
                    if w_max * math.cos(rad) + h_all * math.sin(rad) <= usable_w:
                        break
                    size -= 1
            return size

        notes = []
        size = fit(rows)
        if size < min_px:
            max_len = max(len(r) for r in rows)
            best = None
            for limit in range(max_len - 1, 1, -1):
                cand = []
                for r in rows:
                    cand.extend(_chunk(r, limit) if len(r) > limit else [r])
                if len(cand) == len(rows):
                    continue
                s_c = fit(cand)
                if len(cand) * s_c * look["leading"] > usable_h:
                    break   # これ以上段を増やすと画面の高さに収まらない
                if best is None or s_c > best[1]:
                    best = (cand, s_c)
                if s_c >= min_px:
                    best = (cand, s_c)
                    break
            if best is not None and best[1] >= min_px:
                notes.append(f"行{cut['index']}: 下限 {min_px}px を割るので段を {len(rows)}→{len(best[0])} に増やした（{best[1]}px）"
                             f"【break_after に書き写す】")
                rows, size = best[0], best[1]
            elif strict:
                raise LookError(f"行{cut['index']}: 役 {cut.get('font_role')} の下限 {min_px}px を割ります"
                                f"（必要 {min_px}px、今 {size}px。段を増やしても収まりません）。"
                                f"break_after・max_px・min_px を見直してください。黙って縮めず止めました")
            else:
                notes.append(f"行{cut['index']}: 段を増やしても下限 {min_px}px を割る（{size}px で描く）【要確認】")
        if strict and len(rows) * size * look["leading"] > usable_h:
            raise LookError(f"行{cut['index']}: {len(rows)}段 × {size}px で、画面の高さ（上下の余白 70px と、寄り・揺れの分を除く）に収まりません。"
                            f"break_after・row_lengths を見直してください")
        return rows, size, notes

    def _build_glow(self, look, font_path, accent):
        """accent: glow の行。文字の形を dilate_px 太らせ、ガウスぼかし（半径＝字の大きさ × blur_ratio）をかけ、
        最大値が max_alpha になるよう正規化して、差し色で塗った画像を1枚作る（カットごとに1回。毎フレーム作らない）。
        文字の塗りは本文色のまま、縁なし。光は文字の後ろに描く（_draw_glow）"""
        spec = look.get("glow")
        if spec is None:
            raise RuntimeError("accent: glow の行がありますが、テーマに parts.glow がありません")
        dil = int(spec["dilate_px"])
        blur = max(self.size * float(spec["blur_ratio"]), 1.0)
        x0, y0, x1, y1 = self._bounds()
        pad = int(dil + 3 * blur + 4)
        W, H = int(math.ceil(x1 - x0)) + 2 * pad, int(math.ceil(y1 - y0)) + 2 * pad
        mask = Image.new("L", (W, H), 0)
        for g in self.glyphs:
            ch, gsize = g["glow_src"]
            # 太らせは PIL の縁（円形）で行う。縁つきの画像は字より両側へ dil だけ広がるので、中心を合わせて置く
            gi = self._shared.glyph(ch, font_path, gsize, "#FFFFFF", "#FFFFFF", dil)[0]
            px = int(round(g["cx"] - x0 + pad - gi.width / 2))
            py = int(round(g["cy"] - y0 + pad - gi.height / 2))
            mask.paste(255, (px, py), gi.getchannel("A"))
        arr = np.asarray(mask.filter(ImageFilter.GaussianBlur(blur)), dtype=np.float32)
        peak = float(arr.max())
        a = np.rint(arr / peak * float(spec["max_alpha"]) * 255.0).astype(np.uint8) if peak > 0 else np.zeros_like(arr, dtype=np.uint8)
        col = _hex(accent)
        im = Image.new("RGBA", (W, H), col + (0,))
        im.putalpha(Image.fromarray(a))
        self.glow = im
        self.glow_center = ((x0 + x1) / 2, (y0 + y1) / 2)

    def karaoke_parts(self, g, tl):
        """karaoke の1字の不透明度 (本文色のスプライト, 差し色のスプライト)。退場の掛け率は含まない。
        入り：全文が 0.15 秒で未点灯の濃さまで（入替の行は 0 フレームで未点灯の濃さ）。点灯：その字の時刻から light_frames で未点灯 → 1.0。
        差し色の字は、未点灯のあいだ本文色のスプライト（未点灯の濃さ）、点灯の間に本文色を薄くしながら差し色を濃くする。
        char_times が無い行（対応が足りず、全文点灯に落とした行）は、入りから点灯済み"""
        if tl < 0:
            return 0.0, 0.0
        cut = self.cut
        f_in = 1.0 if self.hard_in else min(tl * FPS / ENTRANCE_FRAMES["karaoke"], 1.0)
        ct = cut.get("char_times")
        if ct is None:
            q = 1.0
        else:
            q = min(max((cut["start"] + tl - ct[g.get("ct", g["order"])]) * FPS / self.light_frames, 0.0), 1.0)
        u = self.unlit
        if "alt" in g:
            return f_in * u * (1.0 - q), f_in * q
        return f_in * (u + (1.0 - u) * q), 0.0

    def glyph_center(self, g, scale, dx, dy):
        """1字の中心（アンカーを足した画面の座標。カメラの前）。draw が字を置く位置と同じ式（行の傾き base_angle で回す）"""
        ax, ay = self.anchor
        rad = math.radians(self.base_angle)
        cos_a, sin_a = math.cos(rad), math.sin(rad)
        gx, gy = g["cx"] * scale, g["cy"] * scale
        rx, ry = gx * cos_a - gy * sin_a, gx * sin_a + gy * cos_a
        return ax + rx + dx, ay + ry + dy

    def glyph_opacity(self, g, tl, dur):
        """1字の不透明度（入り・退場の掛け率 × karaoke の点灯。draw が重ねる不透明度と同じ。draw は 0.02 以下を描かない）"""
        alpha = self.glyph_state(g, tl, dur)[4]
        if alpha > 0.02 and self.karaoke:
            alpha *= max(self.karaoke_parts(g, tl))
        return alpha

    def glyph_box(self, g, tl, dur):
        """1字の外接矩形（アンカーを足した画面の座標。カメラの前）と不透明度。回転は外接の幅・高さに直す。描かない字は None"""
        dx, dy, scale, angle, alpha, _ct, _cr = self.glyph_state(g, tl, dur)
        if alpha <= 0.02:
            return None
        cx, cy = self.glyph_center(g, scale, dx, dy)
        w, h = g["img"].width * scale, g["img"].height * scale
        tr = math.radians(self.base_angle + angle + g["angle"])
        bw = abs(w * math.cos(tr)) + abs(h * math.sin(tr))
        bh = abs(w * math.sin(tr)) + abs(h * math.cos(tr))
        return cx - bw / 2, cy - bh / 2, cx + bw / 2, cy + bh / 2

    def lit_count(self, tl):
        """点灯済みの字数と全字数（静止画のラベル用）"""
        ct = self.cut.get("char_times")
        n = len(self.glyphs)
        if ct is None:
            return n, n
        t = self.cut["start"] + tl
        return sum(1 for x in ct if (t - x) * FPS >= self.light_frames), n

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
            # 衝撃の段階（direction の impacts）を持つカットだけ値を差し替える。無いカットは今までの値（1.8・0.92・3f・5px）
            iv = cut.get("impact_vals") if motion == "slam" else None
            LF = iv["land_frames"] if iv else 3
            lead = (cut["land"] - cut["start"]) * FPS - LF
            p = f - lead
            if p < (-IMPACT_P_EPS if iv else 0.0):   # 段階をもつカットの p＝0 は格子の上（land の丸めの誤差で最初のフレームを落とさない）
                alpha = 0.0
            elif p < LF:
                if iv:
                    # 衝撃の段階をもつカット：入りの1フレーム目から不透明度 1.0（動き §10-3）。縮み方は ease（既定 linear）
                    if iv.get("ease") == "quad":
                        scale = iv["undershoot"] + (iv["overshoot"] - iv["undershoot"]) * (1.0 - p / LF) ** 2
                    else:
                        scale = _lerp(iv["overshoot"], iv["undershoot"], p / LF)
                else:
                    scale = _lerp(1.8, 0.92, p / LF)
                    alpha = min(p / 2, 1.0)
            elif p < E:
                scale = _lerp(iv["undershoot"] if iv else 0.92, 1.0, (p - LF) / (E - LF))
            if motion == "slash":
                angle = -6
            if motion == "shake" or (LF <= p < LF + 4):
                amp = 7 if motion == "shake" else (iv["glyph_shake_px"] if iv else 5) * (1 - (p - LF) / 4)
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
        elif motion == "cut":
            # 開始の 0.2 秒前から draw が呼ばれるので、開始前は出さない（分岐が無いと全文が先に出る）
            alpha = 0.0 if tl < 0 else 1.0
        elif motion == "karaoke":
            # 不透明度は karaoke_parts（未点灯・点灯の段階）が決める。ここは退場の掛け率の土台（開始前だけ 0）
            alpha = 0.0 if tl < 0 else 1.0
            ct = cut.get("char_times")
            if cut.get("karaoke_land") and ct:
                p = (cut["start"] + tl - cut.get("karaoke_land_at", ct[0])) * FPS
                if 0 <= p < 3:
                    scale = _lerp(1.06, 1.0, p / 3)

        if cut.get("hold") == "heartbeat" and f > E:
            scale *= 1 + 0.08 * self._beat_pulse(cut["start"] + tl)
        if cut.get("hold") in HOLD_MOTIONS and f > E:
            hdx, hdy, hscale, hangle = self._hold_state(cut["hold"], g, tl, dur, f - E)
            dx += hdx
            dy += hdy
            scale *= hscale
            angle += hangle
        if cut.get("hold") == CARRY:
            dy += self.carry_dy(g, tl, dur)
        scale *= g.get("gscale", 1.0)

        # 退場
        remain = (dur - tl) * FPS
        exit_name = cut.get("exit")
        exit_sec = _exit_fade_sec(exit_name)
        if exit_name == "swap":
            pass   # 退場なし。次の行の入りと同じフレームで差し替わる
        elif exit_sec is not None:
            n = exit_sec * FPS
            if remain < n:
                alpha *= max(remain, 0) / n
        elif cut.get("exit") == "fly" and remain < 7:
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

    def carry_dy(self, g, tl, dur):
        """carry の移動量（px。下が正）。行の開始（tl＝0）から一定の速さ × 経過時間。表示の長さ（dur）で止める（延ばした間は止めた状態）。
        carry の無い行・指定の無い列は 0.0（足しても出力は変わらない）"""
        sp = carry_spec(self.cut, g["row"])
        if sp is None:
            return 0.0
        # 整数 px に丸めてから足す（小数のまま字ごとに int() で切り捨てると、同じ列の字が別々のフレームで1px ずつ動く。T35 C1 レビュー 中-1）
        return float(math.floor(sp[1] * sp[0] * min(max(tl, 0.0), dur) + 0.5))

    def _plain_exit_alpha(self, remain):
        """下敷き・重ね物の、既定の退場の不透明度。swap（退場なし）と fade:<秒> は glyph_state と同じ扱い"""
        name = self.cut.get("exit")
        if name == "swap":
            return 1.0
        sec = _exit_fade_sec(name)
        if sec is not None:
            return min(max(remain, 0) / (sec * FPS), 1.0)
        if remain < EXIT_FRAMES:
            return max(remain, 0) / EXIT_FRAMES
        return 1.0

    def _exit_frames(self, dur):
        """退場にかける長さ（フレーム）。定義は _exit_frames_of（判定側と同じ値）"""
        return _exit_frames_of(self.cut.get("exit"), dur)

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

    def draw(self, frame, t, sprites, cam=(1.0, 0.0, 0.0, 0.0), dim=1.0):
        """dim：残した列（stack。T35 C2）として描くときの不透明度の掛け率（0〜1）。1.0 は今までと同じ（掛けない・刻みも変えない）"""
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
                     and not cut.get("profile_kids") and cut.get("echo", True))
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

        if self.glow is not None and self.glyphs:
            # 文字の後ろの光。入りと同時に出て行の間は保持（明滅なし）。退場は文字と同じ掛け率
            ga = self.glyph_state(self.glyphs[0], tl, dur)[4] * dim
            if ga > 0.02:
                gim = sprites.transformed(("glow", cut["index"]), self.glow, 1.0, 0, ga, 0.0, 0.0)
                if gim is not None:
                    gcx, gcy = self.glow_center
                    frame.paste(gim, (int(ax + gcx - gim.size[0] / 2), int(ay + gcy - gim.size[1] / 2)), gim)

        for g in self.glyphs:
            dx, dy, scale, angle, alpha, crop_top, crop_right = self.glyph_state(g, tl, dur)
            if dim != 1.0:
                alpha *= dim
            if alpha <= 0.02:
                continue
            if self.karaoke:
                a_main, a_acc = self.karaoke_parts(g, tl)
                total_angle = base_angle + angle + g["angle"]
                kcx, kcy = self.glyph_center(g, scale, dx, dy)
                layers = [(g["alt"][0], g["alt"][1], a_main), (g["key"], g["img"], a_acc)] if "alt" in g \
                    else [(g["key"], g["img"], a_main)]
                for lkey, limg, la in layers:
                    if la * alpha <= 0.02:
                        continue
                    im = sprites.transformed(lkey, limg, scale, total_angle, la * alpha, crop_top, crop_right, alpha_step=20)
                    if im is None:
                        continue
                    frame.paste(im, (int(kcx - im.size[0] / 2), int(kcy - im.size[1] / 2)), im)
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
                gcx, gcy = self.glyph_center(g, scale, gdx, gdy)
                for (mkey, mimg), (ox, oy) in zip(g.get("mis", []), ((9, 6), (-7, -5))):
                    mim = sprites.transformed(mkey, mimg, scale, total_angle, a * 0.9, crop_top, crop_right)
                    if mim is not None:
                        frame.paste(mim, (int(gcx - mim.size[0] / 2 + ox),
                                          int(gcy - mim.size[1] / 2 + oy)), mim)
                if dim != 1.0:
                    im = sprites.transformed(g["key"], g["img"], scale, total_angle, a, crop_top, crop_right, alpha_step=20)   # 0.05 刻み（0.55 を 0.6 に丸めない）
                else:
                    im = sprites.transformed(g["key"], g["img"], scale, total_angle, a, crop_top, crop_right)
                if im is None:
                    continue
                px = gcx - im.size[0] / 2
                py = gcy - im.size[1] / 2
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
                    for piece, x0, y0, ox, oy in shatter_pieces(im, q, g["order"] * 4 + cut["index"]):
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
        a = self._plain_exit_alpha(remain)
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
        a *= self._plain_exit_alpha(remain)
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
        # 単色の代わりに色で刷り直す絵（KineticRenderer が背景素材から入れる。空なら従来の単色）
        self.tint_pool = []

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

    def tinted(self, color, t, cam, path):
        """背景の絵を、パレットの色の濃淡だけで刷り直す（単色のベタ塗りの代わり）。
        明るさの幅を色の 0.7〜1.1 倍に抑え、パレットで決めた文字色との対比を保つ。"""
        im = self.image_at(t, cam, path)
        base = np.array(_hex(color), dtype=np.float32)
        if base.mean() < 60:
            # 黒に近い色は掛け算では絵が消えるので、暗くした絵をそのまま重ねる（文字は白）
            tone = base[None, None, :] + np.asarray(im, dtype=np.float32) * 0.45
        else:
            lum = np.asarray(im.convert("L"), dtype=np.float32) / 255.0
            tone = base[None, None, :] * (0.7 + 0.4 * lum[:, :, None])
        out = Image.fromarray(np.clip(tone, 0, 255).astype(np.uint8))
        return kinetic_bg.apply_paper(out, self.wa)

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
        # 背景素材がある曲は、単色のベタ塗りをやめて絵を色で刷り直す（2026-09-25「ベタ塗りはチープ」）
        tint_src = None
        if self.tint_pool:
            tint_src = (cut.get("bg_image") if cut else None) or \
                self.tint_pool[(cut.get("shot", 0) if cut else 0) % len(self.tint_pool)]
        if bgfx and bgfx.startswith("pattern:"):
            base = self.tinted(color, t, cam, tint_src) if tint_src else Image.new("RGB", VIDEO_SIZE, _hex(color))
            im = kinetic_bg.apply_bgfx(base, bgfx, t, color,
                                       self.palette[mode][3], self.beat_amt(t), cut.get("shot", 0))
            return kinetic_bg.apply_paper(im, self.wa)
        if tint_src:
            return self.tinted(color, t, cam, tint_src)
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


def apply_interlude_effect(frame, name, strength, t, beat_idx, beat_amt, palette, scanlines=True):
    """frame(PIL RGB) に間奏エフェクトをかける。strength 0..1、beat_amt はビート直後ほど1。
    scanlines=False は duotone の走査線（横縞）を付けない（テーマの経路で使う）。既定は今までどおり"""
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
    if name == "scan" or (name == "duotone" and scanlines):
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
    if name == "still":
        return (1.05, 0.0, 0.0, 0.0)
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
    def __init__(self, image_path, plan, beats, style, duration=None, backgrounds=None, look=None, scan_range=None):
        """look: prepare_plan が返す runtime。テーマ（名前付きの配色・書体の役）を使う曲だけ渡す。
        渡さない（または theme が None）なら、従来の経路（添字のパレット・背景画像）のまま。
        scan_range: 点の層の全フレームの走査（_scan_points）をかける秒の範囲 (from, to)。None ＝ 全区間（従来。全編の書き出し・direction の登録）。
        描くフレームだけ走査すればよい経路（部分書き出し・GUI のプレビュー）が渡す。to < from なら走査しない（T35 R0-12）"""
        self._scan_range = scan_range
        self.plan = plan
        self.theme = (look or {}).get("theme")
        self.themed = self.theme is not None
        if self.themed and not all(c.get("font_role") for c in plan):
            raise RuntimeError("テーマを使うのに、役（font_role）の無いカットがあります。kinetic_plan.json を作り直してください（--replan）")
        self._theme_solid = {}
        self.look_notes = []
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
        # 間奏で小節ごとに替える絵。間奏用を先頭に、静かな場面用（囁き・Bridge）以外の全素材
        items = [i for i in (backgrounds or []) if Path(i["file"]).suffix.lower() not in VIDEO_EXTS]
        order = {"interlude": 0, "hook": 1, "growl": 2, "verse": 3, "any": 4}
        self.bar_pool = [i["file"] for i in sorted(items, key=lambda i: order.get(i.get("use"), 9))
                         if i.get("use") in order]
        if not self.kids:
            self.bg.tint_pool = [i["file"] for i in items if i.get("use") in ("verse", "hook", "any", "interlude")]
        bpm = next((c.get("bpm") for c in plan if c.get("bpm")), None) or 120.0
        bar = 4 * 60.0 / float(bpm)
        while bar < INTERLUDE_BAR_MIN_SEC:
            bar *= 2
        self.bar_sec = bar
        self.cuts = []
        if self.themed:
            self._setup_theme(look, plan)
        for c in plan:
            if self.themed:
                pal = self.theme["palettes"][c["bg"]]
                bg = pal["bg"]
                text = pal["text"]
                colors = (text, pal.get("stroke", bg), pal.get("accent", text))
                cut_obj = _Cut(c, self.sprites, colors, bg, beats, look=self._look_context(c))
                self.look_notes.extend(cut_obj.look_notes)
                self.cuts.append(cut_obj)
                continue
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
        if self.themed:
            self._apply_align_to_prev()
        self.decor = _shared_decor()
        self.impact_zoom = {}
        self.impact_cap = {}
        self.impact_notes = []
        self._resolve_impact_zoom()
        self.entry_log = {}
        if getattr(self, "_strict", False):
            self._check_entry_margins()
        self._compute_shown()
        self._note_align_vanished()
        if getattr(self, "_strict", False):
            self._check_no_blank_frames()
        self._stack_prev = {}       # 行 j（0 始まり）を描くとき、一緒に薄く残す前の列 [(行, 不透明度)]。stack の無い曲は空
        if self.themed:
            self._check_carry()
            self._setup_stack()
            self._check_safe_area()
        self.counter = None
        self.counter_skipped = 0
        self._cut_rects = {}
        if self.themed:
            self._setup_counter(look, beats)
            self._check_stage3_late()
        self.points = []            # 点の層（direction の points）。無い曲は空のまま（描画・検査・レポートに何も足さない）
        self.points_max = 0
        self._points_scan = None
        self._point_rects_cache = None
        if self.themed and self._dir.get("points"):
            self._setup_points(look, beats)
            self._check_points()
        for note in self.look_notes + self.impact_notes:
            print(f"      [書体] {note}")

    def _apply_align_to_prev(self):
        """direction の align_to_prev：この行の先頭字の中心 x を、1つ前の行の先頭字の中心 x にそろえる（行全体の平行移動だけ。
        大きさ・字間・y は変えない）。アンカーの x を動かすので、描画・外接・点の層の空け・安全域の検査は同じアンカーを読んで一緒に動く。
        カットを組んだ直後に当てる（後続の検査・点の層は動いた後の位置で働く）。条件は解決後の構図に対して確かめ、外れたら止める：
        この行と前の行がどちらも横組み・1段・center・入りが scatter でなく・同じ大きさ。書体だけが違うときは止めず注記する。
        そろうのは静止位置だけ。入りの動き・slam の寄りの瞬間・前の行が動く場合（carry・踏み込みの沈みなど）は対象外。
        傾いた行（stamp 等で base_angle が 0 でない）は、この行・前の行とも止める。y・字間・書体が違うときは止めず注記を出す。
        前の行がこの行の開始より前に消えるときは、注記を出す（_compute_shown の後で足す）。"""
        from look import LookError

        for j, (c, o) in enumerate(zip(self.plan, self.cuts)):
            if not c.get("align_to_prev"):
                continue
            n = c["index"]
            if j == 0:
                raise LookError(f"direction: 行{n} の align_to_prev は、前の行が無いので使えません")
            pc, po = self.plan[j - 1], self.cuts[j - 1]
            for tag, cc, oo in (("この行", c, o), ("前の行", pc, po)):
                why = None
                if cc.get("layout") != "center":
                    why = f"構図が {cc.get('layout')} です（center の行だけ）"
                elif cc.get("vertical_typeset") or len(cc["rows"]) != 1 or oo.n_rows != 1:
                    why = "1段の横組みではありません"
                elif cc.get("entrance") == "scatter":
                    why = "入りが scatter です"
                elif abs(oo.base_angle) > 1e-9:
                    why = f"行が傾いています（base_angle {oo.base_angle}°。入りが stamp 等）"
                elif not oo.glyphs:
                    why = "字がありません"
                if why:
                    raise LookError(f"direction: 行{n} の align_to_prev は使えません（{tag}は{why}）")
            if abs(o.size - po.size) > 1e-6:
                raise LookError(f"direction: 行{n} の align_to_prev は使えません（字の大きさが前の行と違います：{o.size} と {po.size}。平行移動だけでは重ならない）")
            dx = (po.anchor[0] + po.glyphs[0]["cx"]) - (o.anchor[0] + o.glyphs[0]["cx"])
            o.anchor = (o.anchor[0] + dx, o.anchor[1])
            o.align_dx = dx
            note = f"行{n}：前の行の先頭字にそろえて x {dx:+.1f}px"
            if c.get("font_role") != pc.get("font_role"):
                note += "（書体が違うので先頭字以外は一致しない）"
            # 先頭字以外の静止位置・y の比較（字間・書体・text_y の違いを拾う。止めない）
            pairs = list(zip(po.glyphs, o.glyphs))[1:]
            mx = max((abs(o.glyph_center(g, 1, 0, 0)[0] - po.glyph_center(pg, 1, 0, 0)[0]) for pg, g in pairs), default=0.0)
            my = max((abs(o.glyph_center(g, 1, 0, 0)[1] - po.glyph_center(pg, 1, 0, 0)[1]) for pg, g in zip(po.glyphs, o.glyphs)), default=0.0)
            if mx > 0.5 and "先頭字以外は一致しない" not in note:
                note += f"（先頭字以外は一致しない：x 最大 {mx:.1f}px。字間・書体の違い）"
            if my > 0.5:
                note += f"（y は {my:.1f}px 違う。y はそろえない）"
            self.look_notes.append(note)

    @staticmethod
    def _align_hint(o):
        """止まる文に足す：その行が align_to_prev で x を動かした行なら原因として示す（L-3）"""
        dx = getattr(o, "align_dx", None)
        return "" if dx is None else f"（この行は align_to_prev で x を {dx:+.1f}px 動かした行です。そろえが原因の可能性があります）"

    def _note_align_vanished(self):
        """_compute_shown の後：align_to_prev の行で、前の行がこの行の開始までに消えているなら注記（止めない）"""
        for j, c in enumerate(self.plan):
            if j and c.get("align_to_prev") and self.shown_until[j - 1] < c["start"] - 1e-6:
                self.look_notes.append(f"行{c['index']}：前の行はこの行の開始までに消えています（そろえは入れ替わりでは見えない。前の行の表示を開始まで残すには前の行の tail 等）")

    def _check_safe_area(self):
        """検査（止める）：direction の safe_area（暫定の枠。値は direction に書く）に、全フレーム（30fps）で、読み字の外接矩形（carry の移動・
        踏み込みの沈み・字ごとの入りの動き・画面の寄り（カメラ）を当てた後の位置）が入ること。入りの最初の2フレーム（字が見え始めてから2枚）は除く。
        描かずに、字の矩形（_Cut.glyph_box。draw と同じ置き方）の計算で行う。描くフレームだけ（scan_range）。T35 U8-c"""
        from look import LookError

        sa = self._dir.get("safe_area")
        if not sa or not self._strict:
            return
        W, H = VIDEO_SIZE
        L, R, T, B = float(sa["left"]), float(sa["right"]), float(sa["top"]), float(sa["bottom"])
        corner = sa.get("corner")
        for j, (c, o) in enumerate(zip(self.plan, self.cuts)):
            if not o.glyphs:
                continue
            k0 = int(math.ceil((c["start"] - 0.2) * FPS - 1e-9))
            k1 = int(math.ceil(self.shown_until[j] * FPS - 1e-9))
            if self._scan_range is not None:
                a, b = self._scan_range
                k0, k1 = max(k0, int(math.floor(a * FPS)) - 1), min(k1, int(math.ceil(b * FPS)) + 1)
            seen = 0
            for k in range(k0, k1 + 1):
                t = k / FPS
                if j not in self._active(t):
                    continue
                tc = self._cut_time(j, t)
                dur = c["end"] - c["start"]
                boxes = [b_ for b_ in (o.glyph_box(g, tc - c["start"], dur) for g in o.glyphs) if b_ is not None]
                if not boxes:
                    continue
                seen += 1
                if seen <= 2:
                    continue          # 入りの最初の2フレーム
                _bg, g_zoom, sx, sy = self.camera_at(t)
                z = self._screen_zoom(g_zoom, sx, sy)
                x0 = W / 2 + z * (min(b_[0] for b_ in boxes) - W / 2) + sx
                x1 = W / 2 + z * (max(b_[2] for b_ in boxes) - W / 2) + sx
                y0 = H / 2 + z * (min(b_[1] for b_ in boxes) - H / 2) + sy
                y1 = H / 2 + z * (max(b_[3] for b_ in boxes) - H / 2) + sy
                over = []
                if x0 < L - 1e-6:
                    over.append(f"左へ {L - x0:.1f}px")
                if W - x1 < R - 1e-6:
                    over.append(f"右へ {x1 - (W - R):.1f}px")
                if y0 < T - 1e-6:
                    over.append(f"上へ {T - y0:.1f}px")
                if y1 > B + 1e-6:
                    over.append(f"下へ {y1 - B:.1f}px")
                if corner and y1 > corner["y_from"] + 1e-6 and x1 > corner["x_max"] + 1e-6:
                    over.append(f"右下の角へ（y＞{corner['y_from']} では x≦{corner['x_max']}。右へ {x1 - corner['x_max']:.1f}px、下端 {y1:.1f}px＝角の上端から {y1 - corner['y_from']:.1f}px）")
                if over:
                    raise LookError(f"行{c['index']}・{t:.2f}秒：読み字が安全域（safe_area）からはみ出します（{'、'.join(over)}）。"
                                    f"text_y・大きさ・carry・構図を見直してください。黙って縮めず止めました{self._align_hint(o)}")

    def _check_carry(self):
        """検査（止める）：carry で動いた後の読み字が、上下の余白（CARRY_MARGIN＝92px）に収まる（動く範囲は carry_extent。縦組みで列ごとに
        向きが違うときは列ごとの最大を全体に足した保守的な値）。段ごとの指定の段番号が段の数を超えない。T35 C1"""
        from look import LookError

        H = VIDEO_SIZE[1]
        for c, o in zip(self.plan, self.cuts):
            if c.get("hold") != CARRY or not o.glyphs:
                continue
            if c.get("accent_mode") == "glow":
                raise LookError(f"direction: 行{c['index']} の hold: carry は accent: glow と一緒に使えません（光の位置が動かない。未対応）")
            if c.get("layout") == "grid":      # layout を書かない4字・1段の行は自動で grid になる（枠が固定位置で、字だけ動く）
                raise LookError(f"direction: 行{c['index']} の hold: carry は自動の構図 grid（4字・1段の行）と一緒に使えません"
                                f"（枠が字に付いて行かない。layout: center などを書いてください）")
            if c.get("entrance") in ("stamp", "slash"):
                raise LookError(f"direction: 行{c['index']} の hold: carry は entrance: {c['entrance']} と一緒に使えません（枠・斜線が字に付いて行かない。未対応）")
            spec = c["carry"]
            if "px_s" not in spec:
                bad = [k for k in spec if int(k) >= len(c["rows"])]
                if bad:
                    raise LookError(f"direction: 行{c['index']} の carry の段番号 {', '.join(bad)} が、段の数（{len(c['rows'])}）以上です")
            if self._dir.get("safe_area"):
                continue          # safe_area がある曲は、carry の上下の余白の検査を _check_safe_area（全フレーム）に置き換える（T35 U8-c）
            _X0, Y0, _X1, Y1 = self._screen_rect(o)
            up, down = carry_extent(c)
            if Y0 - up < CARRY_MARGIN - 1e-6 or Y1 + down > H - CARRY_MARGIN + 1e-6:
                raise LookError(f"行{c['index']}: carry で動いた後の読み字が上下の余白 {CARRY_MARGIN}px に収まりません"
                                f"（上端 {Y0 - up:.1f}px・下端の余白 {H - Y1 - down:.1f}px。動く範囲は上へ {up:.1f}px・下へ {down:.1f}px）。"
                                f"速さ・表示の長さ・構図を見直してください。黙って止めずに止めました")

    def _setup_stack(self):
        """stack（前の列を残して薄くする。T35 C2）の検査と、描くときの対応表 self._stack_prev。検査（止める）：
        ①積んだ全部の列が左右の余白（SIDE_MARGIN）に収まる（黙って縮めない）②残した列（不透明度 dim）の比が、最悪の背景で 4.5 以上
        （副要素。COUNTER_MIN_CONTRAST）。残す列は行 j の表示の間、その行の前の積んだ行を、最後の状態のまま dim で描く（clear_at_line の行が
        描かれる最初のフレームから、残した列は描かない＝0 フレームで全部消える）"""
        import look as look_mod
        from look import LookError

        groups = {}
        for j, c in enumerate(self.plan):
            if c.get("stack_group") is not None:
                groups.setdefault(c["stack_group"], []).append(j)
        W = VIDEO_SIZE[0]
        for gi, members in sorted(groups.items()):
            c0 = self.plan[members[0]]
            dim, clear = c0["stack_dim"], c0["stack_clear_line"]
            rects = [self._screen_rect(self.cuts[j]) for j in members if self.cuts[j].glyphs]
            x0, x1 = min(r[0] for r in rects), max(r[2] for r in rects)
            if x0 < SIDE_MARGIN - 1e-6 or x1 > W - SIDE_MARGIN + 1e-6:
                raise LookError(f"stack[{gi}]：積んだ全部の列の外接矩形が左右の余白 {SIDE_MARGIN}px に収まりません"
                                f"（左 {x0:.1f}px・右の余白 {W - x1:.1f}px。幅 {x1 - x0:.1f}px、使える幅 {W - 2 * SIDE_MARGIN}px）。"
                                f"字の大きさ（max_px）・列の数を見直してください。黙って縮めず止めました")
            for j in members:
                pal = self.theme["palettes"][self.plan[j]["bg"]]
                ratio = look_mod.worst_contrast(pal["text"], pal["bg"], round(dim * 20) / 20)   # 描くときの刻み（0.05）に揃える
                if ratio < look_mod.COUNTER_MIN_CONTRAST:
                    raise LookError(f"stack[{gi}]：行{self.plan[j]['index']} の残した列（不透明度 {dim}）の比が、最悪の背景で {ratio:.2f} になり、"
                                    f"{look_mod.COUNTER_MIN_CONTRAST} を割ります。dim を上げてください")
            for j in range(members[0] + 1, clear - 1):          # 積む行の2行目から、clear_at_line の行の手前まで
                self._stack_prev[j] = [(p, dim) for p in members if p < j]

    def _screen_rect(self, o):
        """カットの文字の外接矩形（アンカーを足した画面の座標）。回転（diagonal 等）は両向きの角を取って広いほうを採る"""
        x0, y0, x1, y1 = o._bounds()
        ax, ay = o.anchor
        if not o.base_angle:
            return ax + x0, ay + y0, ax + x1, ay + y1
        rad = math.radians(abs(o.base_angle))
        c, s_ = math.cos(rad), math.sin(rad)
        xs, ys = [], []
        for sg in (1, -1):
            for px in (x0, x1):
                for py in (y0, y1):
                    xs.append(ax + px * c - sg * py * s_)
                    ys.append(ay + sg * px * s_ + py * c)
        return min(xs), min(ys), max(xs), max(ys)

    def _resolve_impact_zoom(self):
        """余白の検査（direction のある曲）と、slam（impact のあるカット）の寄りの上限の検査。
        上限 z ＝ min(448 ÷ max(540−x0, x1−540), 890 ÷ max(960−y0, y1−960)) − 1
        （448 ＝ 540 − 左右の余白 92、890 ＝ 960 − 上下の余白 70。座標は着地後の外接矩形に anchor を足した画面の座標）。
        画面の揺れは分子から、字の揺れは分母（寄りの内側）から引く（(448 − 画面の揺れ) ÷ (… ＋ 字の揺れ)）。slam の行は組む時点で寄りと揺れの分を残してあるので、
        上限は段階の値以上になる。寄りは段階の値のまま使う（頭打ち・引き下げはしない）。
        上限が段階の値を下回ったら、組み方の誤りとして止める。結果は self.impact_zoom[カット番号]"""
        from look import LookError

        W, H = VIDEO_SIZE
        strict = bool(getattr(self, "_strict", False))
        zoom = {}
        for c, o in zip(self.plan, self.cuts):
            if not o.glyphs:
                continue
            X0, Y0, X1, Y1 = self._screen_rect(o)
            iv = c.get("impact_vals")
            z = float(iv["zoom"]) if iv else 0.0
            sh = float(iv["screen_shake_px"]) if iv else 0.0
            gs = float(iv.get("glyph_shake_px", 0)) if iv else 0.0
            if strict:
                # 左右の余白 92px（着地後の静止時）。寄りと揺れを含めた最大の瞬間は、下の slam の検査で見る
                m = min(X0, W - X1)
                if m < SIDE_MARGIN - 1e-6:
                    raise LookError(f"行{c['index']}: 文字の外接矩形の左右の余白が {m:.2f}px で、{SIDE_MARGIN}px を割ります"
                                    f"（左 {X0:.2f}px・右 {W - X1:.2f}px）。構図・max_px・break_after を見直してください。黙って縮めず止めました{self._align_hint(o)}")
            if not iv:
                continue
            hx = max(W / 2 - X0, X1 - W / 2)
            hy = max(H / 2 - Y0, Y1 - H / 2)
            cap = min((W / 2 - SIDE_MARGIN - sh) / max(hx + gs, 1.0), (H / 2 - VERTICAL_MARGIN - sh) / max(hy + gs, 1.0)) - 1.0
            if cap < z - 1e-9:
                raise LookError(f"行{c['index']}: 衝撃 {c['impact']} の寄り {z} に対し、外接矩形から出る寄りの上限が {cap:.4f} です"
                                f"（左右の余白 {SIDE_MARGIN}px・上下 70px・画面の揺れ {sh:g}px・字の揺れ {gs:g}px を守る）。slam の行は寄りと揺れの分を残して"
                                f"組むはずなので、組み方の誤りです。寄りを 0 に丸めず止めました")
            zoom[c["index"]] = z
            self.impact_cap[c["index"]] = cap
        self.impact_zoom = zoom

    def _entry_frames(self, c):
        """slam（impact のあるカット）の入りで、描かれるフレーム（30fps の格子で p ≧ 0 になった最初から、入りの終わり p ＜ E まで）。
        [(番号（描かれた最初が 1）, 時刻, p)]。行の開始は格子に乗らないので、p は 0 から始まるとは限らない"""
        iv = c["impact_vals"]
        LF = iv["land_frames"]
        E = ENTRANCE_FRAMES.get("slam", 6)
        lead = (c["land"] - c["start"]) * FPS - LF
        out = []
        n = int(math.ceil((c["start"] - 0.2) * FPS - 1e-9))
        while True:
            t = n / FPS
            p = (t - c["start"]) * FPS - lead
            if p >= E:
                break
            if p >= -IMPACT_P_EPS:
                out.append((len(out) + 1, t, max(p, 0.0)))
            n += 1
        return out

    def _entry_rect(self, o, c, t):
        """入りのフレームの、字ごとの拡大・字の揺れ・画面全体の寄り・画面の揺れを入れた外接矩形（画面の座標）。
        字の矩形は _Cut.glyph_box（draw と同じ置き方）、画面全体の寄り・揺れは _screen_zoom と _foreground の変換と同じ式"""
        W, H = VIDEO_SIZE
        tl = t - c["start"]
        dur = c["end"] - c["start"]
        _bg, g_zoom, sx, sy = self.camera_at(t)
        z = self._screen_zoom(g_zoom, sx, sy)
        boxes = [b for b in (o.glyph_box(g, tl, dur) for g in o.glyphs) if b is not None]
        if not boxes:
            return None
        x0, x1 = min(b[0] for b in boxes), max(b[2] for b in boxes)
        y0, y1 = min(b[1] for b in boxes), max(b[3] for b in boxes)
        f = lambda v, half, sh: half + z * (v - half) + sh   # draw 後の画面全体の寄り・揺れ（_foreground の transform と同じ）
        return f(x0, W / 2, sx), f(y0, H / 2, sy), f(x1, W / 2, sx), f(y1, H / 2, sy)

    def still_frame(self, t, safe_overlay=False):
        """静止画用のフレーム。safe_overlay＝True なら安全域（direction の safe_area）の枠を細い線で重ねる（静止画だけ。mp4 には描かない）"""
        fr = self.frame_at(t)
        if safe_overlay:
            from look import LookError

            sa = self._dir.get("safe_area") if self.themed else None
            if not sa:
                raise LookError("--safe-overlay には、direction の safe_area が要ります")
            fr = fr.copy()
            d = ImageDraw.Draw(fr)
            W, H = VIDEO_SIZE
            L, R, T, B = sa["left"], sa["right"], sa["top"], sa["bottom"]
            col = (255, 60, 60)
            c = sa.get("corner")
            pts = [(L, T), (W - R, T), (W - R, c["y_from"]), (c["x_max"], c["y_from"]), (c["x_max"], B), (L, B), (L, T)] if c else \
                  [(L, T), (W - R, T), (W - R, B), (L, B), (L, T)]
            d.line(pts, fill=col, width=3)
        return fr

    @staticmethod
    def _screen_zoom(g_zoom, sx, sy):
        """画面全体の実際の寄りの倍率。揺れで端が見えないよう、揺れの分を寄りに足す（_foreground と _entry_rect の共通の式）"""
        return max(g_zoom, 1.0 + 2 * max(abs(sx), abs(sy)) / VIDEO_SIZE[0])

    def _check_entry_margins(self):
        """検査（止める）：叩く行の入りで、描かれる3フレーム目以降の外接矩形が左右 92px・上下 70px を割らない。
        入りの最初の2フレームは除く（動き §10-3・裁定10）。着地以後の静止・寄りは _resolve_impact_zoom が見ている。
        結果は self.entry_log[行] = [(フレーム番号, 時刻, p, 左右の余白, 上下の余白)]（look_report 用）"""
        from look import LookError

        W, H = VIDEO_SIZE
        for c, o in zip(self.plan, self.cuts):
            if not c.get("impact_vals") or not o.glyphs:
                continue
            log = []
            for k, t, p in self._entry_frames(c):
                r = self._entry_rect(o, c, t)
                if r is None:
                    continue
                mx, my = min(r[0], W - r[2]), min(r[1], H - r[3])
                log.append((k, round(t, 4), round(p, 3), round(mx, 2), round(my, 2)))
                if k >= 3 and (mx < SIDE_MARGIN - 1e-6 or my < VERTICAL_MARGIN - 1e-6):
                    raise LookError(f"行{c['index']}: 入りの{k}フレーム目（{t:.3f}秒、p={p:.2f}）の外接矩形の余白が"
                                    f"左右 {mx:.1f}px・上下 {my:.1f}px で、左右 {SIDE_MARGIN}px・上下 {VERTICAL_MARGIN}px を割ります"
                                    f"（入りの最初の2フレームは除く）。max_px・衝撃の段階（overshoot・ease・land_frames）を見直してください{self._align_hint(o)}")
            self.entry_log[c["index"]] = log

    # --- 表示区間（見え始め・表示の終わり）。direction のある曲だけ。無い曲は従来どおり（見え始め＝開始、表示の終わり＝end） ---

    def _cut_opacity(self, j, t):
        """カット j を時刻 t に描いたときの、文字の不透明度の最大（描かない・文字が無い＝0。decor だけのカットは 1）。
        字ごとの値は _Cut.glyph_opacity（draw が重ねる不透明度と同じ）"""
        o, c = self.cuts[j], self.plan[j]
        if not o.glyphs:
            return 1.0
        tl = t - c["start"]
        dur = c["end"] - c["start"]
        if tl < -0.2 or tl > dur:
            return 0.0
        vals = [o.glyph_opacity(g, tl, dur) for g in o.glyphs]
        return max([v for v in vals if v > 0.02] + [0.0])

    def _compute_shown(self):
        """見え始め（不透明度の最大が 0.5 以上になる最初の 30fps のフレーム）と、表示の終わり。
        入れ替わり（前の行の終わり ≧ 次の行の開始）の境目では、前の行を次の行の見え始めまで延ばす（延ばした間は、
        前の行が最後に描かれた状態のまま止める）。self.seen_at／shown_until／handoff（次の行へ替わる時刻）／frozen_t（止める時刻）"""
        n = len(self.plan)
        self.seen_at = [c["start"] for c in self.plan]
        self.shown_until = [c["end"] for c in self.plan]
        self.handoff = [c["start"] for c in self.plan]
        self.frozen_t = {}
        self.extended = {}
        self._shown_by_row = {}
        self._switch_starts = list(self.starts)
        if not getattr(self, "_strict", False):
            return
        for j, c in enumerate(self.plan):
            k0 = int(math.ceil(c["start"] * FPS - 1e-9))
            for k in range(k0, k0 + 90):
                if self._cut_opacity(j, k / FPS) >= 0.5:
                    self.seen_at[j] = k / FPS
                    break
        for j in range(n - 1):
            c, nx = self.plan[j], self.plan[j + 1]
            if c["end"] >= nx["start"] - 1e-6:
                self.handoff[j + 1] = self.seen_at[j + 1]
                self.shown_until[j] = self.seen_at[j + 1]
                self.frozen_t[j] = min(c["end"], nx["start"])
                # 延ばしたフレーム数（境目ごと。前の行が余分に描かれる 30fps の格子のフレームの数。格子のフレームを含まないほどの延びは 0）
                self.extended[j] = (int(math.ceil(self.seen_at[j + 1] * FPS - 1e-9)) - int(math.ceil(nx["start"] * FPS - 1e-9)))
        self._shown_by_row = {c["index"]: self.shown_until[j] for j, c in enumerate(self.plan)}
        run = float("-inf")
        for j, h in enumerate(self.handoff):
            run = max(run, h)
            self._switch_starts[j] = run

    def _switch_index(self, t):
        """時刻 t の背景・本文色の元になる行（番号は 0 始まり）。切り替えのフレーム（handoff ＝ 次の行が実際に描かれる最初のフレーム）で
        次の行へ替わる。direction の無い曲は handoff ＝ 開始なので、従来の「開始で替わる」と同じ（動き §10-8 ①）"""
        return max(bisect.bisect_right(self._switch_starts, t) - 1, 0)

    def _check_no_blank_frames(self):
        """検査（止める）：どれかのカットの [開始, 表示の終わり) に入る 30fps のフレームで、描かれる文字の不透明度の最大が 0.02 未満のものが
        あれば止める。除くのは、隙間（前の行の表示の終わり ＜ 次の行の開始）の後の最初の1フレームだけ。設計の空白（隙間・間奏・アウトロ）は
        表示区間の外なので除かれる。結果は self.blank_frames（[(時刻, 前の行, 次の行)]。除いたものは含めない）"""
        from look import LookError

        self.blank_frames = []
        bad = []
        end = max(c["end"] for c in self.plan)
        firsts = {}
        for j, c in enumerate(self.plan):
            if j == 0 or self.plan[j - 1]["end"] < c["start"] - 1e-6:
                firsts[int(math.ceil(c["start"] * FPS - 1e-9))] = j     # 隙間の後の最初の1フレーム
        for k in range(int(end * FPS) + 1):
            t = k / FPS
            idx = bisect.bisect_right(self.starts, t) - 1
            if idx < 0:
                continue
            inside = any(self.plan[j]["start"] <= t < self.shown_until[j] or (j == idx and self.plan[j]["start"] <= t < self.plan[j]["end"])
                         for j in (idx - 1, idx) if j >= 0)
            if not inside:
                continue
            act = self._active(t)
            mx = max([self._cut_opacity(j, self._cut_time(j, t)) for j in act] + [0.0])
            if mx < 0.02 and firsts.get(k) is None:
                prev = self.plan[idx - 1]["index"] if idx > 0 else None
                bad.append((t, prev, self.plan[idx]["index"]))
        self.blank_frames = bad
        if bad:
            lines = [f"{t:.3f}秒（前の行 {a}・次の行 {b}）" for t, a, b in bad[:8]]
            raise LookError(f"文字が1つも描かれないフレームが {len(bad)} 個あります（表示区間の中。隙間の後の最初の1フレームは除く）。止めました：\n  "
                            + "\n  ".join(lines))

    def _cut_time(self, j, t):
        """カット j を描く時刻。延ばした間（次の行の見え始めまで）は、最後に描かれた状態で止める"""
        ft = self.frozen_t.get(j)
        return t if ft is None else min(t, ft)

    # --- カウンター（外の層）と、段3後半の検査 ---

    def _setup_counter(self, look, beats):
        """direction に counter があるときだけ、カウンターの時間軸を作る（プランの counter・単語の開始・beats・テーマの部品だけから決まる）"""
        import look as look_mod
        from look import LookError

        if not self._dir.get("counter"):
            return
        cfg = (self.theme.get("parts") or {}).get("counter")
        if cfg is None or "palette" not in cfg or not cfg.get("slots"):
            raise LookError("direction に counter がありますが、テーマの parts.counter に palette・slots がありません")
        switch = {c["index"]: self.handoff[j] for j, c in enumerate(self.plan)}   # 切り替えのフレーム（割れの始まり）
        self.counter = kinetic_fx.Counter(self.plan, self._dir["counter"], beats, cfg, switch=switch)
        ref = self._fonts.get(cfg["role"])
        if ref is None:
            raise LookError(f"parts.counter.role '{cfg['role']}' の書体が解決されていません")
        font = self.sprites.fonts.get(look_mod.FontRef(ref["path"], ref["index"]), int(cfg["px"]))
        self.counter.attach_font(font, self.theme["palettes"][cfg["palette"]]["accent"])
        self._counter_margin = float(cfg.get("avoid_px", 24))
        self._compute_cut_rects()

    def _compute_cut_rects(self):
        """文字の外接矩形（着地後の大きさ × (1 ＋ 寄り)）。**余白なし**だけを持つ（clip＝割れの片を描かない範囲）。
        カウンターは自分の余白（avoid_px）を足して置き場を判定する（_counter_rects）。点の層（読み字の周りの空け）は余白なしをそのまま読む。
        同じ辞書を余白違いで上書きしない（点の層を足すとカウンターの余白が 0 になった。T35 R0-2）"""
        W, H = VIDEO_SIZE
        for j, (c, o) in enumerate(zip(self.plan, self.cuts)):
            if not o.glyphs:
                continue
            X0, Y0, X1, Y1 = self._screen_rect(o)
            up, down = carry_extent(c)     # carry で動く範囲を含める（含めないと、動いた先で点が読み字に重なる。T35 C1）
            Y0, Y1 = Y0 - up, Y1 + down
            z = 1.0 + self.impact_zoom.get(c["index"], 0.0)
            clip = (W / 2 + (X0 - W / 2) * z, H / 2 + (Y0 - H / 2) * z, W / 2 + (X1 - W / 2) * z, H / 2 + (Y1 - H / 2) * z)
            self._cut_rects[j] = clip

    def _stack_end(self, j):
        """残した列（stack）を保つ区間の終わり（clear_at_line の行の開始）。積んだ行でなければ None"""
        c = self.plan[j]
        if c.get("stack_group") is None:
            return None
        return self.plan[c["stack_clear_line"] - 1]["start"]

    def _counter_rects(self, t):
        """t に出ている文字の外接矩形（余白込み・余白なし）。slam の入りの拡大中は含めない（読ませる時間ではない）"""
        avoid, clip = [], []
        m = self._counter_margin
        for j, b in self._cut_rects.items():
            c = self.plan[j]
            if c["start"] <= t <= max(c["end"], self.shown_until[j], self._stack_end(j) or 0.0):   # 延ばした間（次の行の見え始めまで）の前の行も含める
                avoid.append((b[0] - m, b[1] - m, b[2] + m, b[3] + m))
                clip.append(b)
        return avoid, clip

    # --- 点の層（direction の points。読み字の下に敷く） ---

    def _setup_points(self, look, beats):
        """direction の points から PointLayer を作る。区間は resolve_span（表示の終わり込み）、字は実行時にプランの行から取る
        （direction・コードに歌詞を書かない）。読み字の周りの空けは _compute_cut_rects の外接矩形から"""
        import look as look_mod
        from look import LookError

        self._compute_cut_rects()
        shown = [self._shown_end(c) for c in self.plan]
        duration = self.duration or look.get("duration")
        self.point_sprites = kinetic_points.PointSprites(self.sprites.fonts)
        n_alpha = len(self.plan)
        for i, sp in enumerate(self._dir["points"]):
            where = f"points[{i}]"
            span = sp["span"]
            t0, t1 = resolve_span(self.plan, span, duration, where=where, shown=shown)
            if t1 <= t0:
                raise LookError(f"direction: {where} の区間が空です（{t0:.2f}〜{t1:.2f}秒）")
            rows = [j for j, c in enumerate(self.plan) if frame_overlap(c["start"], shown[j], t0, t1) > 0]
            src = sp.get("source", "line")
            flats = ["".join(c["rows"]) for c in self.plan]
            if isinstance(src, dict):
                k = src["key"]
                if k["line"] > n_alpha:
                    raise LookError(f"direction: {where}.source.key.line が行数（{n_alpha}）を超えています")
                text = kinetic_points.source_chars(flats[k["line"] - 1][k["from"]:k["from"] + k["len"]])
                variants = [(0.0, text)]
            elif src == "section":
                name = span.get("section") or (self.plan[rows[0]].get("section") if rows else None)
                text = "".join(kinetic_points.source_chars(flats[j]) for j, c in enumerate(self.plan) if c.get("section") == name)
                variants = [(0.0, text)]
            else:
                if not rows:
                    before = [j for j, c in enumerate(self.plan) if c["start"] <= t0]
                    rows_src = [before[-1]] if before else [0]
                else:
                    rows_src = rows
                variants = [(self.plan[j]["start"], kinetic_points.source_chars(flats[j])) for j in rows_src]
                variants = [(a, tx) if tx else (a, "・") for a, tx in variants]
                if any(tx == "・" for _a, tx in variants):
                    raise LookError(f"direction: {where}（source: line）の行に、点に使える字（文字・数字）がありません")
            if not variants[0][1]:
                raise LookError(f"direction: {where} の source から点に使う字が取れません（空白・約物だけ）")
            rects = []
            for j in range(len(self.plan)):
                if j not in self._cut_rects or self.plan[j]["start"] > t1 + 1.0 or shown[j] < t0 - 1.0:
                    continue
                te = max(self.plan[j]["end"], shown[j])
                if self._stack_end(j) is not None:      # 残した列（stack）は clear_at_line の行の開始まで空けを保つ
                    te = max(te, self._stack_end(j))
                rects.append({"ts": self.plan[j]["start"], "te": te,
                              "box": self._cut_rects[j], "h": float(self.cuts[j].size),
                              "cap": float(self.plan[j].get("clear_cap", kinetic_points.CLEAR_OPACITY))})
            anchor = None
            if rows and rows[0] in self._cut_rects:
                b = self._cut_rects[rows[0]]
                anchor = ((b[0] + b[2]) / 2, (b[1] + b[3]) / 2)
            elif not rows and sp.get("kind") == "shape" and sp.get("from") == "line":
                # 区間に重なる行が無いとき、出発点は直前の行（区間の頭より前に始まった最後の行。source: line の字を取る選び方と同じ）の読み字の中心（T35 R10）
                prev_rows = [j for j, c in enumerate(self.plan) if c["start"] <= t0 and j in self._cut_rects]
                if prev_rows:
                    b = self._cut_rects[prev_rows[-1]]
                    anchor = ((b[0] + b[2]) / 2, (b[1] + b[3]) / 2)
            # track の at_line・at_time を、区間の頭からの秒に直す（ここ1か所。直した後の秒で check_track を掛ける。T35 U3）
            sp, _resolved = kinetic_points.resolve_track(sp, sp["kind"], [c["start"] for c in self.plan], t0, where)
            layer = kinetic_points.PointLayer(sp, i, t0, t1, variants, rects, beats=beats, anchor=anchor)
            role = sp.get("role") or ("tsubu" if "tsubu" in self.theme["fonts"] else self.theme.get("default_role"))
            ref = self._fonts.get(role)
            if ref is None:
                raise LookError(f"direction: {where} の書体の役 '{role}' が解決されていません")
            fspec = self.theme["fonts"][role]
            if (fspec.get("family"), fspec.get("style")) in kinetic_points.LIGHT_BAD_FONTS and any(
                    look_mod.is_light_palette(self.theme["palettes"][self.plan[j]["bg"]]) for j in rows):
                raise LookError(f"direction: {where}：白地（明るい地）の粒・線・形に使えない書体です（{fspec.get('family')} {fspec.get('style')}。"
                                f"字が黒い塊になる。書体配色カタログ §6）")
            self.points.append({"layer": layer, "font_ref": look_mod.FontRef(ref["path"], ref["index"]), "role": role,
                                "rows": rows, "t0": t0, "t1": t1})

    def _check_points(self):
        """点の層の検査（止める）：①読み字の大きさ（60px 以上・点の最大の字の 1.5 倍以上）と、各行に読み字があること ②1フレームの字数の上限
        ③solo の行・間奏の区間との重なり ④読み字の比（空けの範囲に点を重ねた最悪の背景で 4.5 以上） ⑤全フレームの走査（_scan_points）：
        空けの範囲の被覆（重なりを含めた画素。頭打ちの後の値なので、コードが正しければ上限を超えない＝自己確認であって独立の測定ではない。
        独立の測定は書き出したフレームの画素からの逆算）、字数の上限（実際に描く字数）"""
        import look as look_mod
        from look import LookError

        P = kinetic_points
        self._points_contrast = {}
        for pt in self.points:
            L = pt["layer"]
            for j in pt["rows"]:
                c, o = self.plan[j], self.cuts[j]
                if not o.glyphs:
                    raise LookError(f"points[{L.index}]：行{c['index']} に読み字がありません（点の層だけで行を表さない）")
                if o.size < P.READ_MIN_PX:
                    raise LookError(f"points[{L.index}]：行{c['index']} の読み字が {o.size}px で、{P.READ_MIN_PX}px を割ります")
                if o.size < P.READ_RATIO * L.size_max - 1e-9:
                    raise LookError(f"points[{L.index}]：行{c['index']} の読み字（{o.size}px）が、点の層の最大の字（{L.size_max}px）の "
                                    f"{P.READ_RATIO} 倍（{P.READ_RATIO * L.size_max:.0f}px）に足りません。点を小さくするか、読み字を大きくしてください")
                pal = self.theme["palettes"][c["bg"]]
                bg = look_mod.worst_bg(pal["text"], pal["bg"])
                cap = float(c.get("clear_cap", P.CLEAR_OPACITY))
                # 点（テーマの points_color の色）が読み字の周りの被覆 cap で重なった最悪の地（紙の微粒子 ＋10 込み）に対する、読み字の色すべての比の最悪：
                # 本文色・karaoke の行の未点灯の色（本文色を未点灯の濃さで名目の地に重ねた色。字の真下には点が無く、すぐ隣の地にだけ点が cap まで重なる最悪。worst_contrast と同じ流儀）・差し色のある行の差し色（T35 U7）
                base = look_mod.over(pal[self.theme.get("points_color", "text")], bg, cap)
                kinds = [("本文色", pal["text"])]
                if self.cuts[j].karaoke:
                    kinds.append(("未点灯の色", look_mod.over(pal["text"], pal["bg"], self.cuts[j].unlit)))
                if c.get("accent_mode") in ("fill", "key_word", "rows") and pal.get("accent"):
                    kinds.append(("差し色", pal["accent"]))
                rs = [(look_mod.contrast(col, base), name) for name, col in kinds]
                ratio, worst_name = min(rs)
                self._points_contrast[(L.index, c["index"])] = ratio
                if ratio < 4.5:
                    raise LookError(f"points[{L.index}]：行{c['index']} の読み字の比（{worst_name}）が、空けの範囲に点（被覆の上限 {cap}）を重ねた最悪の背景で "
                                    f"{ratio:.2f} になり、4.5 を割ります（clear_cap を下げる・点の色を暗くする・色を見直す）")
            if L.nominal_max() > P.MAX_CHARS:
                raise LookError(f"points[{L.index}]：1フレームの字数が {L.nominal_max()} で、上限 {P.MAX_CHARS} を超えます（自動で減らさない）")
            w0, w1 = L.t0, L.t1
            for c in self.plan:
                if c.get("solo") and frame_overlap(c["start"] - 0.2, self._shown_end(c), w0, w1):
                    raise LookError(f"points[{L.index}]（{w0:.2f}〜{w1:.2f}秒）が、solo の行{c['index']} の表示中と重なります。止めました")
            for sp_ in self.interlude_spans:
                if frame_overlap(sp_["t0"], sp_["t1"], w0, w1):
                    raise LookError(f"points[{L.index}]（{w0:.2f}〜{w1:.2f}秒）が、間奏・アウトロの効果の区間（{sp_['t0']:.2f}〜{sp_['t1']:.2f}秒）と重なります。止めました")
        # 字数の合計（同じ時刻に出る点の層を足す）
        if len(self.points) > 1:
            lo = min(p_["t0"] for p_ in self.points)
            hi = max(p_["t1"] for p_ in self.points)
            for k in range(int(math.ceil(lo * FPS - 1e-9)), int(hi * FPS) + 1):
                tot = sum(p_["layer"].nominal(k / FPS) for p_ in self.points)
                if tot > P.MAX_CHARS:
                    raise LookError(f"{k / FPS:.2f}秒：点の層の字数の合計が {tot} で、上限 {P.MAX_CHARS} を超えます（同じ時刻に出る点の層を足した数。自動で減らさない）")
        self._scan_points()

    def _scan_points(self):
        """全フレーム（30fps）の走査（止める）：読み字の空けの範囲（広げた矩形の中）の点の被覆（重なりを含めた画素）が 0.25＋1/255 を超えない／
        1フレームに描く字数の上限。被覆は頭打ち（composite_cov）を掛けた後の値を測るので、コードが正しければ上限を超えず、止まる経路は
        ほぼ働かない（コードの自己確認）。測定ではない。独立の測定は、書き出したフレームの画素から被覆を逆算する（点の層なし・ありの差）。
        結果（最大字数・平均字数・頭打ちの後の被覆の最大）は look_report に出す"""
        from look import LookError

        P = kinetic_points
        stats = {p_["layer"].index: {"max": 0, "sum": 0, "n": 0, "clear_max": 0.0} for p_ in self.points}
        lo = min(p_["t0"] for p_ in self.points)
        hi = max(p_["t1"] for p_ in self.points)
        rects = self._point_rects()
        items = [(p_["layer"], p_["font_ref"]) for p_ in self.points]
        k0, k1 = int(math.ceil(lo * FPS - 1e-9)), int(hi * FPS)
        if self._scan_range is not None:        # 描くフレームだけ走査する（前後 1 フレームの余裕。範囲の外は走査しない）
            a, b = self._scan_range
            k0 = max(k0, int(math.floor(a * FPS)) - 1)
            k1 = min(k1, int(math.ceil(b * FPS)) + 1)
        memo_key = self._scan_memo_key(k0, k1)
        if memo_key in _SCAN_MEMO:               # 同じ入力を同じ範囲で走査済み（direction の登録の検査で作った描画器の直後に作る本番の描画器）
            self._points_scan = copy.deepcopy(_SCAN_MEMO[memo_key])
            return
        for k in range(k0, k1 + 1):
            t = k / FPS
            total = big = 0
            live = []
            pre = {}
            for pt in self.points:
                L = pt["layer"]
                if t < L.t0 - 1e-9 or t > L.t1 + 1e-9:
                    continue
                d = L.points_at(t)
                pre[L.index] = d          # 被覆の測定（render_points）に渡して、同じ時刻の points_at を二重に計算しない
                n = len(d["x"])
                total += n
                big += int((d["size"] > P.BIG_PX).sum())
                st = stats[L.index]
                st["max"] = max(st["max"], n)
                st["sum"] += n
                st["n"] += 1
                live.append(st)
            if live and total and any(r["ts"] - P.CLEAR_RAMP <= t <= r["te"] + P.CLEAR_RAMP for r in rects):
                # 空けの範囲の被覆を、重なりを含めた画素で測る（字ごとの不透明度ではなく）
                _n, _b, m = P.render_points(None, t, None, items, self.point_sprites, rects, pre=pre)
                for st in live:
                    st["clear_max"] = max(st["clear_max"], m)
                lim = max([r["cap"] for r in rects if r["ts"] - P.CLEAR_RAMP <= t <= r["te"] + P.CLEAR_RAMP] + [0.0])   # その時刻に効いている空けの上限の最大（行の clear_cap）
                if m > lim + 1.0 / 255 + 1e-9:
                    raise LookError(f"{t:.2f}秒、読み字の空けの範囲の点の被覆が {m:.3f} です（上限 {P.CLEAR_OPACITY}）。止めました")
            if total > P.MAX_CHARS or big > P.MAX_BIG_CHARS:
                raise LookError(f"{t:.2f}秒：点の層の字数が {total}（60px を超える字 {big}）で、上限（{P.MAX_CHARS}字・60px 超は {P.MAX_BIG_CHARS}字）を超えます。止めました")
        self._points_scan = stats
        if len(_SCAN_MEMO) >= 8:
            _SCAN_MEMO.clear()
        _SCAN_MEMO[memo_key] = copy.deepcopy(stats)

    def _scan_memo_key(self, k0, k1):
        """走査の結果を覚えておく鍵：走査が読む入力（各層の指定・区間・字・読み字の矩形・出発点・拍・書体）と走査するフレームの範囲。
        プラン・direction・テーマからこれらが決まるので、同じ入力なら走査の結果（最大字数・平均字数・被覆の最大）は同じ（止まる場合は覚えない）"""
        parts = []
        for pt in self.points:
            L = pt["layer"]
            parts.append([L.spec, L.index, L.t0, L.t1, [(a, "".join(c.tolist())) for a, c in L.variants], L.rects,
                          list(L.anchor), L.beats, str(pt["font_ref"]), L.seed])
        raw = json.dumps([parts, k0, k1, kinetic_points.__file__], ensure_ascii=False, sort_keys=True, default=str)
        return hashlib.sha1(raw.encode("utf-8")).hexdigest()

    def _point_rects(self):
        """全層の読み字の矩形（空けの範囲）。層をまたいで足す（色が同じなので被覆は1枚に集める）"""
        if getattr(self, "_point_rects_cache", None) is None:
            seen, out = set(), []
            for pt in self.points:
                for r in pt["layer"].rects:
                    key = (r["ts"], r["te"], r["box"], r["h"], r["cap"])
                    if key not in seen:
                        seen.add(key)
                        out.append(r)
            self._point_rects_cache = out
        return self._point_rects_cache

    def _draw_points(self, frame, t):
        """点の層を貼る（decor の後・カウンターと読み字の前）。色は その時刻の、テーマの points_color の色（既定 text＝本文色）。空けの範囲の字は被覆に集めて画素ごとに頭打ちし、
        色を1回だけ貼る（kinetic_points.render_points）。貼った字数の上限は式の誤りの検出（通常は走査で止まっている）"""
        color = tuple(int(round(v)) for v in self.palette_color_at(t, self.theme.get("points_color", "text")))
        items = [(pt["layer"], pt["font_ref"]) for pt in self.points]
        total, big, _m = kinetic_points.render_points(frame, t, color, items, self.point_sprites, self._point_rects())
        if total > self.points_max:
            self.points_max = total
        if total > kinetic_points.MAX_CHARS or big > kinetic_points.MAX_BIG_CHARS:
            raise RuntimeError(f"{t:.2f}秒：点の層の字数が上限を超えました（{total}字、60px 超 {big}字）")

    def _read_run(self, j):
        """行 j の読み字が「全字が最終位置・不透明度 1（karaoke は未点灯の濃さ以上）」で連続して見える最長の区間（秒, 開始, 終わり）。
        30fps の全フレームを走査（表示の終わりまで。入りの途中・退場の途中は含まない）"""
        o, c = self.cuts[j], self.plan[j]
        dur = c["end"] - c["start"]
        thr = (o.unlit - 0.02) if o.karaoke else 0.98
        best, cur_n, cur_s = (0.0, None, None), 0, None
        k0 = int(math.ceil(c["start"] * FPS - 1e-9))
        k1 = int(math.ceil(self.shown_until[j] * FPS - 1e-9))
        for k in range(k0, k1 + 1):
            t = k / FPS
            ok = False
            if t < self.shown_until[j] - 1e-9 or k == k1:
                tl = self._cut_time(j, t) - c["start"]
                if 0 <= tl <= dur and o.glyphs:
                    ok = True
                    for g in o.glyphs:
                        dx, dy, sc, ang, _a, _ct, _cr = o.glyph_state(g, tl, dur)
                        dy -= o.carry_dy(g, tl, dur)      # carry の一定の移動は「止まっている」の判定から除く（carry の無い行は 0.0）
                        if (abs(dx) > 1.0 or abs(dy) > 1.0 or abs(sc - 1.0) > 0.02 or abs(ang) > 1.0
                                or o.glyph_opacity(g, tl, dur) < thr):
                            ok = False
                            break
            if ok:
                if cur_n == 0:
                    cur_s = t
                cur_n += 1
                if cur_n / FPS > best[0]:
                    best = (cur_n / FPS, cur_s, t)
            else:
                cur_n = 0
        return best

    def points_report_lines(self):
        """look_report に足す節（点の層があるときだけ）"""
        P = kinetic_points
        stats = self._points_scan or {}
        scan_note = []
        if self._scan_range is not None:
            a, b = self._scan_range
            if b >= a:
                scan_note = ["", f"（走査の範囲：{a:.2f}〜{b:.2f} 秒のフレームだけ。部分書き出し・プレビューの描画器。全編の値ではない）"]
            else:
                scan_note = ["", "（走査なし：プレビューの描画器。この表の字数・被覆は空）"]
        out = scan_note + ["", "## 点の層", "",
               "字は実行時に行から取る（歌詞は書かない）。空けの中の被覆（頭打ち後）は、頭打ちを掛けた後の値の全フレーム走査の最大（重なりを含めた画素。全層を合わせた値。上限は行の clear_cap（既定 0.25）＋1/255）。**測定ではなく、頭打ちのコードの自己確認。この列を合格の根拠にしない（独立の測定は書き出したフレームの画素からの逆算）。**"
               "点滅の検査（§6）は書き出した mp4 に measure_flicker.py を掛ける（この表には入らない）。", "",
               f"点の色（テーマの points_color）：{self.theme.get('points_color', 'text')}（既定 text＝本文色）。", "",
               "| 点 | 種類 | 区間 | 内容 | 役 | opacity | dir | count_fade | 最大字数 | 平均字数 | 被覆（頭打ち後・自己確認） | 最大の字 | ink |",
               "|---|---|---|---|---|---|---|---|---|---|---|---|---|"]
        for pt in self.points:
            L = pt["layer"]
            st = stats.get(L.index, {})
            avg = (st["sum"] / st["n"]) if st.get("n") else 0.0
            out.append(f"| {L.index} | {L.kind} | {L.t0:.2f}–{L.t1:.2f} | {L.describe()} | {pt['role']} | {L.op_lo:g}〜{L.op_hi:g} | {L.spec.get('dir', '') if L.kind == 'tate_line' else ''} | {L.spec.get('count_fade', '') if L.kind == 'tate_line' else ''} | {st.get('max', 0)} | {avg:.0f} | "
                       f"{st.get('clear_max', 0.0):.2f} | {L.size_max}px | {(L.ink or {}).get('mode', '')} |")
        tracks = [(pt["layer"].index, pt["layer"]) for pt in self.points if hasattr(pt["layer"], "tr_t")]
        if tracks:
            out += ["", "字数・本数の変化（track。秒に直した後。区間の頭からの秒 → 字数・本数）：", "", "| 点 | 区間の頭（秒） | track |", "|---|---|---|"]
            for idx, L in tracks:
                out.append(f"| {idx} | {L.t0:.2f} | " + "、".join(f"{t:.2f}→{c:g}" for t, c in zip(L.tr_t, L.tr_c)) + " |")
        out += ["", "| 点 | 行 | 読み字 | 読める時間（秒） | 区間 | 読み字の比（空けに点を重ねた最悪） |", "|---|---|---|---|---|---|"]
        short = []
        for pt in self.points:
            L = pt["layer"]
            for j in pt["rows"]:
                c = self.plan[j]
                sec, a, b = self._read_run(j)
                span = f"{a:.2f}–{b:.2f}" if a is not None else "—"
                flag = "（1.0 秒未満）" if sec < 1.0 else ""
                if sec < 1.0:
                    short.append((c["index"], sec))
                out.append(f"| {L.index} | {c['index']} | {self.cuts[j].size}px | {sec:.2f}{flag} | {span} | "
                           f"{self._points_contrast.get((L.index, c['index']), 0):.2f} |")
        out += ["", "読める時間が 1.0 秒未満の行：" + (", ".join(f"行{n}（{s_:.2f}秒）" for n, s_ in short) if short else "なし")]
        return out

    def color_intervals(self):
        """黄緑（カウンターの色）と琥珀（鍵語の色）が出る区間（半開）。どちらも [(開始, 終了, 理由, 行)]。
        黄緑 ＝ カウンターの不透明度が 0 でない区間・割れの片が残る区間・glow／fill の行の表示区間。
        琥珀 ＝ 鍵語を含む行の表示区間（点灯前の未点灯の間も含めて、行の開始から終わりまで）"""
        green, amber = [], []
        part = (self.theme.get("parts") or {}).get("counter")
        if not part or "palette" not in part:
            return green, amber
        gcol = self.theme["palettes"][part["palette"]].get("accent")
        if self.counter is not None:
            green.extend((a, b, "カウンター", None) for a, b in self.counter.intervals())
        for c in self.plan:
            pal = self.theme["palettes"][c["bg"]]
            if c.get("accent_mode") in ("glow", "fill") and pal.get("accent") == gcol:
                green.append((c["start"], self._shown_end(c), c["accent_mode"], c["index"]))
            if c.get("accent_idx"):
                amber.append((c["start"], self._shown_end(c), "段の差し色" if c.get("accent_mode") == "rows" else "鍵語", c["index"]))
        return green, amber

    def _shown_end(self, c):
        """カットの表示の終わり（次の行の見え始めまで延ばした分を含む。end 以上）"""
        return max(c["end"], self._shown_by_row.get(c["index"], c["end"]))

    def counter_contrast(self):
        """カウンターの比（最悪の背景：背景+10。明るい地は −10。間奏の duotone をかけた後の色で）。{行: (最小の比, そのときの不透明度)}
        消えていく途中・割れの落下は装飾として除く（読ませる対象ではない）"""
        import look as look_mod

        out = {}
        if self.counter is None:
            return out
        part = self.theme["parts"]["counter"]
        accent = self.theme["palettes"][part["palette"]]["accent"]
        end = max(c["end"] for c in self.plan)
        for k in range(int(end * FPS) + 1):
            t = k / FPS
            if self.counter.hide_factor(t) < 1.0:
                continue
            for st in self.counter.states(t):
                if st["phase"] == "fall":
                    continue
                r = look_mod.contrast(look_mod.over(accent, self.effective_bg(t, 0), st["alpha"]),
                                      self.effective_bg(t, 10 * look_mod.worst_sign(accent, self.effective_bg(t, 0))))
                row = self.plan[max(bisect.bisect_right(self.starts, t) - 1, 0)]["index"]
                if row not in out or r < out[row][0]:
                    out[row] = (r, st["alpha"])
        return out

    def extended_contrast(self):
        """延ばす間（次の行の開始から切り替えのフレームまで）の、前の行の文字と、そのときの背景（bg+10。明るい地は −10。切り替えの前なので前の行の
        配色のまま）のコントラスト比。{前の行の番号: (最小の比, 延ばしたフレーム数)}。延ばしたフレームが無い行は入らない（動き §10-8 ①）"""
        import look as look_mod

        out = {}
        for j, c in enumerate(self.plan[:-1]):
            if j not in self.frozen_t:
                continue
            nx = self.plan[j + 1]
            k0 = int(math.ceil(nx["start"] * FPS - 1e-9))
            k1 = int(math.ceil(self.seen_at[j + 1] * FPS - 1e-9))
            cols = [self.cuts[j].colors[0]]
            if c.get("accent_mode") in ("fill", "key_word", "rows"):
                cols.append(self.cuts[j].colors[2])
            ratios = [look_mod.contrast(col, self.effective_bg(k / FPS, 10 * look_mod.worst_sign(col, self.effective_bg(k / FPS, 0))))
                      for k in range(k0, k1) for col in cols]
            if ratios:
                out[c["index"]] = (min(ratios), k1 - k0)
        return out

    def _check_stage3_late(self):
        """direction のある曲の、段3後半の検査（止める）。①鍵語の行の表示中にカウンターが見えている ②割れの落ち切りが次の鍵語の行の開始より後
        ③外の声でない行にカウンターが見えている（割れの行は割れの終わりまで許す）④黄緑と琥珀が同じ時間に出る ⑤カウンターの比が 4.5 を割る
        ⑥solo の行の表示中に、カウンター・間奏・背景の補間・画面の寄り／揺れがある"""
        if not self._strict:
            return
        from look import COUNTER_MIN_CONTRAST, LookError

        green, amber = self.color_intervals()
        bad = []
        for a0, a1, _w, n in amber:
            for g0, g1, why, gn in green:
                nf = frame_overlap(a0, a1, g0, g1)   # 30fps の格子で両方が出るフレームの数（1フレーム未満の重なりは許す）
                if nf:
                    who = "カウンター" if why == "カウンター" else f"行{gn} の {why}"
                    bad.append(f"鍵語の行{n}（{a0:.2f}〜{a1:.2f}秒）と{who}（{g0:.2f}〜{g1:.2f}秒）が {nf} フレーム重なります")
        if bad:
            raise LookError("黄緑と琥珀が同じ時間に出ます（琥珀の行の間は外の層を出さない）。止めました：\n  " + "\n  ".join(bad[:8]))
        if self.counter is not None:
            for brow, (tb, t_end) in self.counter.break_rows.items():
                for a0, _a1, _w, n in amber:
                    if a0 > tb and t_end > a0 + 1e-9:
                        raise LookError(f"行{brow} の割れの落ち切り（{t_end:.2f}秒）が、次の鍵語の行{n}の開始（{a0:.2f}秒）より後です。止めました")
            cd = self._dir["counter"]
            vs = cd["voices"]
            outer = [vs] if isinstance(vs, str) else list(vs)
            ivs = self.counter.intervals()
            for c in self.plan:
                if c.get("voice") in outer:
                    continue
                brk = self.counter.break_rows.get(c["index"])
                for g0, g1 in ivs:
                    ov0, ov1 = max(g0, c["start"]), min(g1, self._shown_end(c))
                    if frame_overlap(c["start"], self._shown_end(c), g0, g1) and not (brk is not None and ov1 <= brk[1] + 1e-6):
                        raise LookError(f"行{c['index']}（声 {c.get('voice')}）の表示中（{ov0:.2f}〜{ov1:.2f}秒）にカウンターが出ています。"
                                        f"外の層は外の声の行にだけ出す（二人の声の行は hide・off）。止めました")
            lows = [(r, row) for row, (r, _a) in self.counter_contrast().items() if r < COUNTER_MIN_CONTRAST]
            if lows:
                r, row = min(lows)
                raise LookError(f"行{row}：カウンターの比が最悪の背景で {r:.2f} で、{COUNTER_MIN_CONTRAST} を割ります（parts.counter.dim を見直す）。止めました")
        for c in self.plan:
            if not c.get("solo"):
                continue
            w0, w1 = c["start"] - 0.2, self._shown_end(c)
            what = None
            if self.counter is not None and any(frame_overlap(w0, w1, g0, g1) for g0, g1 in self.counter.intervals()):
                what = "カウンターが出ています"
            elif any(frame_overlap(w0, w1, sp["t0"], sp["t1"]) for sp in self.interlude_spans):
                what = "間奏・アウトロの効果の区間と重なります"
            elif any(frame_overlap(w0, w1, tr[0], tr[1]) for tr in self._transitions):
                what = "背景の補間の区間と重なります"
            else:
                for k in range(int(w0 * FPS), int(w1 * FPS) + 2):
                    _bg, z, sx, sy = self.camera_at(k / FPS)
                    if abs(z - 1.0) > 1e-9 or sx or sy:
                        what = f"画面の寄り・揺れが 0 ではありません（{k / FPS:.2f}秒）"
                        break
            if what:
                raise LookError(f"行{c['index']} は solo（唯一の主役）ですが、表示中（{w0:.2f}〜{w1:.2f}秒）に{what}。止めました")

    # --- 検査の出力（look_report.md・動きの静止画） ---

    def counter_row_table(self):
        """行ごとのカウンターの状態。[{行, 声, 開始−0.3, 開始−0.15, 開始（不透明度）, 個数, 値, 状態}]"""
        rows = []
        if self.counter is None:
            return rows
        cn = self.counter
        for c in self.plan:
            t0 = c["start"]
            st = cn.states(t0)
            if c["index"] in cn.break_rows:
                kind = "割れ"
            elif cn.hide_factor(t0) == 0.0:
                kind = "消えている"
            elif isinstance(c.get("counter"), str):
                kind = c["counter"]
            else:
                kind = "出ている" if st else "なし"
            rows.append({"row": c["index"], "voice": c.get("voice"), "a_m30": cn.opacity_at(t0 - 0.3),
                         "a_m15": cn.opacity_at(t0 - 0.15), "a_0": cn.opacity_at(t0),
                         "a_end": cn.opacity_at(max(c["end"] - 1e-6, t0)), "n": len(st),
                         "values": [s_["value"] for s_ in st], "kind": kind})
        return rows

    def look_report_text(self, theme_name=None):
        """各行の 役／配色／背景色／文字色／主文字の比（点灯）／未点灯の比（bg+10）／glow の比／カウンターの比／寄りの上限／黄緑と琥珀の出る区間／延ばす間の前の行の文字比（M2 の検査）。
        比はすべて look.contrast（式は1か所）。歌詞は書かない"""
        import look as look_mod

        green, amber = self.color_intervals()
        ccon = self.counter_contrast()
        econ = self.extended_contrast()
        part = (self.theme.get("parts") or {}).get("counter") or {}
        out = [f"# look_report（テーマ {theme_name or self.theme.get('look')}）", "",
               "比は WCAG 2.x（look.contrast）。最悪の背景 ＝ 背景色の各チャンネル +10（紙の微粒子）。歌詞は書かない。", "",
               "| 行 | 役 | 配色 | 背景色 | 文字色 | 主文字の比（点灯） | 未点灯の比（bg+10） | glow の比 | カウンターの比（最小） | 寄り 段階/上限 | 黄緑の出る区間 | 琥珀の出る区間 | 同時 | 延ばす間の文字比（最小・フレーム数） |",
               "|---|---|---|---|---|---|---|---|---|---|---|---|---|---|"]
        if any(look_mod.is_light_palette(self.theme["palettes"][c["bg"]]) for c in self.plan):
            # 明るい地の組があるときだけ注記（暗い地だけの曲の出力は変えない）
            out[4:4] = ["**明るい地の組（背景が文字より明るい）では、最悪の背景は各チャンネル −10 側**（見出しの「+10」は暗い地の向き）。"
                        "値は向きに合わせて計算してある。", ""]
        for c in self.plan:
            pal = self.theme["palettes"][c["bg"]]
            main = look_mod.worst_contrast(pal["text"], pal["bg"], 1.0)
            unlit = ""
            if c.get("entrance") == "karaoke":
                u = pal.get("unlit_opacity", (c.get("karaoke") or {}).get("unlit_opacity", 0.65))
                unlit = f"{look_mod.worst_contrast(pal['text'], pal['bg'], u):.2f}（{u}）"
            gl = ""
            if c.get("accent_mode") == "glow":
                ga = self.theme["parts"]["glow"]["max_alpha"]
                gl = f"{look_mod.contrast(pal['text'], look_mod.over(pal.get('accent', pal['text']), pal['bg'], ga)):.2f}"
            cc = ""
            if c["index"] in ccon:
                cc = f"{ccon[c['index']][0]:.2f}（{ccon[c['index']][1]:.2f}）"
            zoom = ""
            if c.get("impact"):
                zoom = f"{c['impact']} {self.impact_zoom.get(c['index'], 0):.3f}/{self.impact_cap.get(c['index'], 0):.3f}"
            w0, w1 = c["start"], self._shown_end(c)
            gs = [f"{why}{max(g0, w0):.2f}–{min(g1, w1):.2f}" for g0, g1, why, gn in green
                  if min(g1, w1) - max(g0, w0) > 1e-6]
            am = f"{'段の差し色' if c.get('accent_mode') == 'rows' else '鍵語'} {w0:.2f}–{w1:.2f}" if c.get("accent_idx") else ""
            clash = ""
            if am:
                clash = "重なる" if any(frame_overlap(w0, w1, g0, g1) for g0, g1, _y, _n in green) else "なし"
            ec = f"{econ[c['index']][0]:.2f}（{econ[c['index']][1]}f）" if c["index"] in econ else ""
            out.append(f"| {c['index']} | {c['font_role']} | {c['bg']} | {pal['bg']} | {pal['text']} | {main:.2f} | {unlit} | {gl} | {cc} | {zoom} | "
                       f"{'; '.join(gs)} | {am} | {clash} | {ec} |")
        if self.counter is not None:
            cn = self.counter
            out += ["", f"## カウンター（dim {part.get('dim')}、色 {self.theme['palettes'][part['palette']].get('accent')}）", "",
                    "| 行 | 声 | 状態 | 不透明度 開始−0.3 | 開始−0.15 | 開始 | 終わり | 個数 | 値（開始時点） |",
                    "|---|---|---|---|---|---|---|---|---|"]
            for r in self.counter_row_table():
                out.append(f"| {r['row']} | {r['voice']} | {r['kind']} | {r['a_m30']:.2f} | {r['a_m15']:.2f} | {r['a_0']:.2f} | {r['a_end']:.2f} | "
                           f"{r['n']} | {','.join(str(v) for v in r['values'])} |")
            out += ["", "割れ：" + (", ".join(f"行{n} {a:.2f}→{b:.2f}" for n, (a, b) in sorted(cn.break_rows.items())) or "なし"),
                    "間奏の出現：" + (", ".join(f"{t:.2f}" for t in cn.appear_at) or "なし")]
        if self.interlude_spans:
            out += ["", "## 間奏・アウトロの効果", "", "| 区間 | 種類 | 寄り |", "|---|---|---|"]
            for sp in self.interlude_spans:
                z = (f"往復 1.00→{sp['zoom_peak']}→1.00" if sp["zoom_peak"] else
                     f"1.00→{sp['zoom_ramp']['to']}（{sp['zoom_ramp']['seconds']}秒）" if sp["zoom_ramp"] else "なし")
                out.append(f"| {sp['t0']:.2f}–{sp['t1']:.2f} | {sp['kind']}（走査線なし） | {z} |")
        out += ["", "## 黄緑と琥珀の同時表示", "",
                f"琥珀の区間 {len(amber)} 行 × 黄緑の区間 {len(green)} 件を照合。重なり：" +
                ("なし" if not any(frame_overlap(a0, a1, g0, g1) for a0, a1, _w, _n in amber for g0, g1, _y, _m in green) else "あり（止める）")]
        if self.points:
            out += self.points_report_lines()
        return "\n".join(out) + "\n"

    def write_look_report(self, cache_dir, part=False):
        """direction のある曲の look_report.md を cache_dir（_work/<hash>/）に書く。Git の外。
        part＝True（部分書き出し。走査が描く区間だけの値）は look_report.part.md に書き、全編の look_report.md を上書きしない"""
        path = Path(cache_dir) / ("look_report.part.md" if part else "look_report.md")
        path.write_text(self.look_report_text(), encoding="utf-8")
        return path

    def motion_entries(self):
        """動きの静止画（別シート）の行。[(ラベル, 行番号, 種類, [時刻...])]
        slam＝着地 −2f・着地・+2f・+6f／karaoke＝点灯済み 0%・約50%・100%／カウンターの hide＝開始 −0.3・−0.15・開始（resume は開始の前後）／
        割れ＝割れ始め・ひびの最後・落下の中ほど・落ち切り／間奏・アウトロ＝区間を6等分"""
        ent = []
        for j, c in enumerate(self.plan):
            o = self.cuts[j]
            t0, t1 = c["start"], c["end"]
            if c.get("impact"):
                L = c["land"]
                ent.append((f"叩き {c['impact']}", c["index"], "slam", [L - 2 / FPS, L, L + 2 / FPS, L + 6 / FPS]))
            if o.karaoke:
                ct = c.get("char_times")
                n = len(o.glyphs)
                lf = o.light_frames / FPS
                if ct:
                    half = ct[max(math.ceil(n / 2) - 1, 0)] + lf
                    full = min(ct[-1] + lf, t1 - 1e-3)
                    ent.append(("点灯", c["index"], "karaoke", [t0 + 0.15, min(half, t1 - 1e-3), full]))
                else:
                    ent.append(("点灯（全文点灯に落とした行）", c["index"], "karaoke", [t0 + 0.15, (t0 + t1) / 2, t1 - 0.2]))
            sp = c.get("counter")
            if self.counter is not None and isinstance(sp, str) and sp == "hide":
                ent.append(("カウンター hide", c["index"], "hide", [t0 - 0.3, t0 - 0.15, t0]))
            if self.counter is not None and isinstance(sp, str) and sp == "resume":
                ent.append(("カウンター resume", c["index"], "resume", [t0 - 1 / FPS, t0, t0 + 0.1]))
            br = self.counter.break_rows.get(c["index"]) if self.counter is not None else None
            if br:
                b = next(b for b in self.counter.badges if b["break"] and abs(b["break"][0] - br[0]) < 1e-9)
                _tb, cf, ff = b["break"]
                ent.append(("カウンター割れ", c["index"], "break",
                            [br[0], br[0] + cf / FPS, br[0] + (cf + ff / 2) / FPS, br[0] + (cf + ff - 1) / FPS]))
        for k, sp in enumerate(self.interlude_spans):
            a, b = sp["t0"], sp["t1"]
            ent.append((f"間奏・アウトロ {k + 1}", 0, "interlude", [a + (b - a) * (i + 0.5) / 6 for i in range(6)]))
        return ent

    # --- テーマ（名前付きの配色・書体の役・背景色の時間軸） ---

    def _setup_theme(self, look, plan):
        theme = self.theme
        self._fonts = look.get("fonts") or {}
        self._paper = (theme.get("texture") or {}).get("paper", "plain") != "none"
        self._text_width = (theme.get("layout") or {}).get("text_width", TEXT_WIDTH)
        self._strict = bool(look.get("direction"))   # direction のある曲は、下限割れ・高さ超過で止める
        # 入替の行の karaoke は 0 フレームで出す（direction のある曲だけ。入替 ＝ 前の行の終わり ≧ 開始。_compute_shown と同じ規則）
        self._hard_in = {c["index"] for j, c in enumerate(plan)
                         if self._strict and j > 0 and c.get("entrance") == "karaoke" and plan[j - 1]["end"] >= c["start"] - 1e-6}
        self._dir = look.get("direction_data") or {}
        self._transitions = []
        for tr in (self._dir.get("bg_transitions") or []):
            t0, t1 = resolve_span(plan, tr)
            self._transitions.append((t0, max(t1, t0 + 1e-6), _hex(theme["palettes"][tr["from"]]["bg"]),
                                      _hex(theme["palettes"][tr["to"]]["bg"]),
                                      _hex(theme["palettes"][tr["from"]]["text"]), _hex(theme["palettes"][tr["to"]]["text"]),
                                      tr["from"], tr["to"]))
        self._transitions.sort(key=lambda x: x[0])
        self._tr_starts = [x[0] for x in self._transitions]
        # 間奏・アウトロの効果（direction の interludes。指定した区間だけ。他の空きには出ない）
        self.interlude_spans = []
        song_len = self.duration or look.get("duration")
        for it in (self._dir.get("interludes") or []):
            from look import LookError

            to_end = it.get("until") == "end"
            t0, t1 = resolve_span(plan, it, song_len, where="interludes")
            if t1 <= t0:
                raise LookError(f"interludes: 区間が空です（{t0:.2f}〜{t1:.2f}秒）")
            self.interlude_spans.append({"t0": t0, "t1": t1, "kind": it.get("kind", "duotone"), "to_end": to_end,
                                         "zoom_peak": it.get("zoom_peak"), "zoom_ramp": it.get("zoom_ramp")})

    def _look_context(self, c):
        import look as look_mod

        spec = self.theme["fonts"][c["font_role"]]
        ref = self._fonts.get(c["font_role"])
        if ref is None:
            raise RuntimeError(f"役 '{c['font_role']}' の書体が解決されていません")
        row_fonts = {}
        for k, role in (c.get("row_roles") or {}).items():     # 縦組みの段ごとの書体（T35 L2）
            rr = self._fonts.get(role)
            if rr is None:
                raise RuntimeError(f"段の役 '{role}' の書体が解決されていません")
            row_fonts[int(k)] = look_mod.FontRef(rr["path"], rr["index"])
        return {"font": look_mod.FontRef(ref["path"], ref["index"]), "row_fonts": row_fonts,
                "tracking": float(spec.get("tracking", 0.0)), "leading": float(spec.get("leading", 1.2)),
                "min_px": spec.get("min_px", 60), "max_px": spec.get("max_px", 150),
                "thicken": spec.get("thicken"), "layer_outline": spec.get("layer_outline"),
                "text_width": self._text_width, "strict": self._strict,
                "glow": (self.theme.get("parts") or {}).get("glow"),
                "unlit_opacity": self.theme["palettes"][c["bg"]].get("unlit_opacity"),
                "hard_in": c["index"] in self._hard_in}

    def bg_color_at(self, t):
        """テーマの背景色（時刻 → 色）。各カットの切り替えのフレーム（次の行が実際に描かれる最初のフレーム。延ばす間は前の行の
        配色のまま。direction の無い曲は開始）で、そのカットの配色の背景色へ硬く切り替える。
        bg_transitions の区間は、2色の間を線形に補間する（区間が終わったら、次のカットが始まるまで終点の色のまま）。
        背景画像には戻さない（カットの空き・間奏・アウトロも単色）。"""
        k = self._switch_index(t)
        cut = self.plan[k]
        color = _hex(self.theme["palettes"][cut["bg"]]["bg"])
        j = bisect.bisect_right(self._tr_starts, t) - 1
        if j >= 0:
            t0, t1, ca, cb = self._transitions[j][:4]
            if cut["start"] <= t0:   # この区間より前に始まったカットの間だけ。次のカットの開始で硬く切り替わる
                u = min(max((t - t0) / (t1 - t0), 0.0), 1.0)
                color = tuple(a + (b - a) * u for a, b in zip(ca, cb))
        return color

    def text_color_at(self, t):
        """その時刻の本文色（点の層の色は palette_color_at＝テーマの points_color）。背景色の補間と同じ進み具合で、補間の区間は 2 つの配色の本文色の間を線形に補間する"""
        k = self._switch_index(t)
        cut = self.plan[k]
        color = _hex(self.theme["palettes"][cut["bg"]]["text"])
        j = bisect.bisect_right(self._tr_starts, t) - 1
        if j >= 0:
            t0, t1 = self._transitions[j][:2]
            ta, tb = self._transitions[j][4:6]
            if cut["start"] <= t0:
                u = min(max((t - t0) / (t1 - t0), 0.0), 1.0)
                color = tuple(a + (b - a) * u for a, b in zip(ta, tb))
        return color

    def palette_color_at(self, t, key):
        """その時刻の、配色の中のキー（text・sub・accent）の色。text_color_at と同じ進み（背景の補間の区間は 2 つの配色のそのキーの色の間を線形に補間）。
        点の層の色（テーマの points_color）に使う。key が text のときは text_color_at と同じ値"""
        if key == "text":
            return self.text_color_at(t)
        k = self._switch_index(t)
        cut = self.plan[k]
        pals = self.theme["palettes"]
        color = _hex(pals[cut["bg"]][key])
        j = bisect.bisect_right(self._tr_starts, t) - 1
        if j >= 0:
            t0, t1 = self._transitions[j][:2]
            fa, fb = self._transitions[j][6:8]
            ta, tb = _hex(pals[fa][key]), _hex(pals[fb][key])
            if cut["start"] <= t0:
                u = min(max((t - t0) / (t1 - t0), 0.0), 1.0)
                color = tuple(a + (b - a) * u for a, b in zip(ta, tb))
        return color

    # --- 間奏・アウトロ（テーマの経路。direction の interludes で指定した区間だけ） ---

    def _interlude_at(self, t):
        """(区間, 強さ 0〜1)。区間の外は (None, 0)。強さは INTERLUDE_FADE で出入り（曲末まで続く区間は出だしだけ）"""
        for sp in self.interlude_spans:
            if sp["t0"] <= t <= sp["t1"]:
                st = min((t - sp["t0"]) / INTERLUDE_FADE, 1.0)
                if not sp["to_end"]:
                    st = min(st, (sp["t1"] - t) / INTERLUDE_FADE)
                return sp, max(st, 0.0)
        return None, 0.0

    def interlude_zoom(self, t):
        """画面全体の寄りの倍率。往復（zoom_peak：区間の中点で最大、前半・後半とも smoothstep）か、
        一方向（zoom_ramp：開始から seconds かけて to まで。以後は保つ）"""
        f = 1.0
        for sp in self.interlude_spans:
            if not sp["t0"] <= t <= sp["t1"]:
                continue
            if sp["zoom_peak"] is not None:
                u = (t - sp["t0"]) / (sp["t1"] - sp["t0"])
                f *= 1.0 + (float(sp["zoom_peak"]) - 1.0) * _smooth(2 * u if u < 0.5 else 2 - 2 * u)
            elif sp["zoom_ramp"] is not None:
                zr = sp["zoom_ramp"]
                f *= 1.0 + (float(zr["to"]) - 1.0) * _smooth((t - sp["t0"]) / float(zr["seconds"]))
        return f

    def _duotone_colors(self, t):
        """duotone の2色。暗い側 ＝ その時刻の背景色、明るい側 ＝ その時刻の本文色（背景の補間と同じ進み具合）"""
        def to_hex(c):
            return "#%02X%02X%02X" % tuple(int(round(v)) for v in c)

        return to_hex(self.bg_color_at(t)), to_hex(self.text_color_at(t))

    def _themed_interlude(self, frame, t):
        sp, st = self._interlude_at(t)
        if sp is None or st <= 0.01:
            return frame
        # 拍の弾み（beat_amt）は使わない。走査線なし（グリッチを使わない）
        return apply_interlude_effect(frame, sp["kind"], st, t, 0, 0.0, self._duotone_colors(t), scanlines=False)

    def effective_bg(self, t, lift=0):
        """間奏の duotone をかけた後の、背景の1画素の色（lift ＝ 紙の微粒子でずれた分。比が下がる側へ ±10。暗い地は +10、明るい地は −10）。効果の外では背景色（＋lift）そのまま"""
        px = np.array([min(max(int(round(v)) + lift, 0), 255) for v in self.bg_color_at(t)], dtype=np.float32)
        sp, st = self._interlude_at(t)
        if sp is None or st <= 0.01:
            return tuple(int(v) for v in px)
        dark = np.array(_hex(self._duotone_colors(t)[0]), dtype=np.float32)
        light = np.array(_hex(self._duotone_colors(t)[1]), dtype=np.float32)
        lum = float(np.clip((px[0] * 0.299 + px[1] * 0.587 + px[2] * 0.114) / 255.0 * 1.25, 0, 1))
        tone = dark + (light - dark) * lum
        out = px * (1 - st) + tone * st
        return tuple(int(v) for v in np.clip(out, 0, 255))

    def _themed_background(self, t):
        """単色（＋紙の微粒子だけ）。補間中の色はキャッシュしない（色の数だけ 6MB の画像が溜まるため）"""
        color = tuple(int(round(v)) for v in self.bg_color_at(t))
        im = self._theme_solid.get(color)
        if im is not None:
            return im.copy()
        arr = np.empty((VIDEO_SIZE[1], VIDEO_SIZE[0], 3), dtype=np.int16)
        arr[:, :] = color
        if self._paper:
            arr += kinetic_bg.paper_texture(False)
        out = Image.fromarray(np.clip(arr, 0, 255).astype(np.uint8))
        if len(self._theme_solid) < 12 and any(color == _hex(p["bg"]) for p in self.theme["palettes"].values()):
            self._theme_solid[color] = out   # 配色そのものの色だけキャッシュする
        return out.copy() if color in self._theme_solid else out

    def _active(self, t):
        """時刻 t に描くカット（番号）。1カット1メッセージ：次のカットが見え始める（handoff）まで前のカットを描き、次は描かない。
        direction の無い曲は shown_until ＝ 終わり・handoff ＝ 開始（_compute_shown の既定）で、従来の「次の開始で替わる」と同じ"""
        k = bisect.bisect_right(self.starts, t + 0.2) - 1
        found = []
        for j in (k - 1, k):
            if 0 <= j < len(self.plan):
                c = self.plan[j]
                if c["start"] - 0.2 <= t and (t <= c["end"] or t < self.shown_until[j]):
                    found.append(j)
        if len(found) == 2:
            found = found[1:] if t >= self.handoff[found[1]] else found[:1]
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
        g_zoom = 1.0 + c.get("creep", 0.02 if c["bg"] == "image" else 0.06) * uc
        sx = sy = 0.0
        iv = c.get("impact_vals")
        if iv:
            # 衝撃の段階をもつカット（slam）は、level に関係なく着地で寄りと揺れが効く（寄りは外接矩形で頭打ち済み）
            if since_land >= 0:
                g_zoom += self.impact_zoom.get(c["index"], float(iv["zoom"])) * math.exp(-since_land / 0.12)
                amp = float(iv["screen_shake_px"]) * math.exp(-since_land / 0.08)
                sx = amp * math.sin(t * 90)
                sy = amp * math.cos(t * 77)
        elif c["level"] == 3 and since_land >= 0:
            g_zoom += c.get("land_zoom", 0.07) * math.exp(-since_land / 0.12)
            amp = c.get("shake", 4 if self.kids else 14) * math.exp(-since_land / 0.08)
            sx = amp * math.sin(t * 90)
            sy = amp * math.cos(t * 77)
            g_zoom += c.get("beat_zoom", 0.012) * (self.bg.pulse(t) - 1.0) / max(self.bg.pulse_scale - 1.0, 1e-3)
        elif c["level"] == 1:
            g_zoom = 1.0 + c.get("creep", 0.015) * uc
        if self.themed and self.interlude_spans:
            g_zoom *= self.interlude_zoom(t)   # 間奏・アウトロの寄り引き（指定した区間だけ 1.0 以外）
        return bg_cam, g_zoom, sx, sy

    def frame_at(self, t):
        bg_cam, g_zoom, sx, sy = self.camera_at(t)
        if self.themed:
            frame = self._themed_interlude(self._themed_background(t), t)
        else:
            frame = self._plain_background(t, bg_cam)
        return self._foreground(frame, t, bg_cam, g_zoom, sx, sy)

    def _plain_background(self, t, bg_cam):
        """従来の背景（添字のパレット・背景画像・間奏の効果・ワイプ）"""
        mode, prev, p = self._bg_mode_at(t)
        kc = bisect.bisect_right(self.starts, t) - 1
        cur_cut = self.plan[kc] if 0 <= kc < len(self.plan) and mode == self.plan[kc]["bg"] else None
        ki = bisect.bisect_right(self.interlude_starts, t) - 1
        in_interlude = ki >= 0 and self.interludes[ki][0] <= t <= self.interludes[ki][1] and mode == "image" and prev is None
        bar_j = None
        if in_interlude and not self.kids and self.bar_pool:
            # 小節ごとに、絵・カメラの動き・エフェクトを順に替える。替わり目で一瞬寄る
            s0 = self.interludes[ki][0]
            bar_j = int((t - s0) / self.bar_sec)
            u = (t - s0 - bar_j * self.bar_sec) / self.bar_sec
            name = INTERLUDE_CAMERAS[(ki * 3 + bar_j) % len(INTERLUDE_CAMERAS)]
            zoom, px, py, ang = camera_move(name, u, -1, bar_j)
            zoom *= 1.0 + 0.10 * math.exp(-(u * self.bar_sec) / 0.12)
            bg_cam = (zoom, px, py, ang)
            img = self.bar_pool[(ki * 5 + bar_j) % len(self.bar_pool)]
            frame = ImageEnhance.Brightness(self.bg.frame(mode, t, bg_cam, None, img)).enhance(INTERLUDE_BAR_BRIGHTEN)
        elif in_interlude:
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
                elif bar_j is not None:
                    # 絵とエフェクトの組み合わせが同じ周期で固定されないよう、4小節ごとにずらす
                    effect = INTERLUDE_BAR_EFFECTS[(k + bar_j + bar_j // 4) % len(INTERLUDE_BAR_EFFECTS)]
                frame = apply_interlude_effect(frame, effect, strength, t, bi, beat_amt,
                                               (pal[0][0], pal[0][3]) if k % 2 else (pal[1][0], pal[2][0]))
        if prev is not None:
            prev_cut = self.plan[kc - 1] if kc > 0 else None
            old = self.bg.frame(prev, t, bg_cam, prev_cut if prev_cut and prev_cut["bg"] == prev else None)
            kind = cur_cut.get("wipe", "straight") if cur_cut else "straight"
            mask = kinetic_bg.wipe_mask(kind, _ease_out(p), kc)
            frame = Image.composite(frame, old, mask)
        return frame

    def _foreground(self, frame, t, bg_cam, g_zoom, sx, sy):
        active = self._active(t)
        for j in active:
            text_color, _stroke, accent = self.cuts[j].colors
            self.decor.draw(frame, self.plan[j], self._cut_time(j, t), _hex(text_color), _hex(accent), bg_cam)
        if self.points:
            self._draw_points(frame, t)   # 点の層。decor の上、カウンターと読み字の下
        if self.counter is not None:
            avoid, clip = self._counter_rects(t)
            self.counter_skipped += self.counter.draw(frame, t, avoid, clip, shatter_pieces)   # 外の層。文字の下
        grain = 0.0
        for j in active:
            for p, dim in self._stack_prev.get(j, ()):
                # 残した列（stack）。前の行の最後の状態（表示の終わりの1フレーム前。退場は swap だけなので掛け率は 1）を dim で描く
                cp = self.plan[p]
                self.cuts[p].draw(frame, cp["start"] + max(cp["end"] - cp["start"] - 1.0 / FPS, 0.0), self.sprites, bg_cam, dim=dim)
            self.cuts[j].draw(frame, self._cut_time(j, t), self.sprites, bg_cam)
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
            z = self._screen_zoom(g_zoom, sx, sy)
            a = 1 / z
            c0 = W / 2 - a * W / 2 - sx / z
            f0 = H / 2 - a * H / 2 - sy / z
            frame = frame.transform(VIDEO_SIZE, Image.AFFINE, (a, 0, c0, 0, a, f0), resample=Image.BILINEAR)
        return frame


def render_kinetic(image_path, audio_path, plan, beats, style, output_path, progress=None,
                   t_start=None, t_end=None, backgrounds=None, look=None):
    """t_start / t_end を渡すと、その区間だけを書き出す（音声も同じ区間）。"""
    from moviepy import AudioFileClip, VideoClip

    audio = AudioFileClip(str(audio_path))
    # subclipped の後は audio.duration が切り出した長さになるので、曲全体の長さは先に控える
    song_duration = audio.duration
    t0 = max(float(t_start or 0.0), 0.0)
    t1 = min(float(t_end), song_duration) if t_end is not None else song_duration
    if t1 - t0 < 0.1:
        raise ValueError(f"書き出す区間が短すぎます: {t0:.2f}〜{t1:.2f}秒")
    partial = t0 > 0 or t1 < song_duration
    # 部分書き出しは、描く区間のフレームだけ点の層を走査する（全編の書き出しは全区間。T35 R0-12）
    renderer = KineticRenderer(image_path, plan, beats, style, duration=song_duration, backgrounds=backgrounds, look=look,
                               scan_range=(t0, t1) if partial else None)
    if renderer.themed and (look or {}).get("direction") and (look or {}).get("cache_dir"):
        renderer.write_look_report(look["cache_dir"], part=partial)   # 部分書き出しは別ファイル（全編の報告を部分区間の値で上書きしない）
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


def render_stills(image_path, plan, beats, style, out_dir, cuts_per_sheet=8, backgrounds=None, look=None, safe_overlay=False):
    """各カットの 0/25/50/75/100% と入りの着地直後を静止画にし、
    一覧画像（コンタクトシート）にまとめる。書き出し前の目視確認用。"""
    renderer = KineticRenderer(image_path, plan, beats, style, backgrounds=backgrounds, look=look)
    out_dir = Path(out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    thumb_w, thumb_h = 216, 384
    fractions = [0.0, 0.25, 0.5, 0.75, 0.97]
    # 歌い終わりまで残す方式のプランは、「歌い終わりの 0.2 秒前」の列を足す（歌っている最中に消えていないかの確認用）
    sung_col = any(c.get("sung_end") is not None for c in plan)
    ncols = len(fractions) + (1 if sung_col else 0)
    sheets = []
    for s in range(0, len(plan), cuts_per_sheet):
        chunk = plan[s:s + cuts_per_sheet]
        sheet = Image.new("RGB", (thumb_w * ncols + 80, thumb_h * len(chunk)), (40, 40, 40))
        d = ImageDraw.Draw(sheet)
        label_font = ImageFont.truetype(FONT_HEAVY, 22)
        for r, c in enumerate(chunk):
            d.text((6, r * thumb_h + 8), f"#{c['index']}", font=label_font, fill=(255, 255, 255))
            d.text((6, r * thumb_h + 40), c["motion"][:7], font=label_font, fill=(200, 200, 200))
            d.text((6, r * thumb_h + 70), (c["layout"][:7] + ("+vt" if c.get("vertical_typeset") else "")), font=label_font, fill=(200, 200, 200))
            dur = c["end"] - c["start"]
            first_frame = ENTRANCE_FRAMES.get(c["entrance"], 6) / FPS
            for k, fr in enumerate(fractions):
                t = c["start"] + max(dur * fr, 0 if fr else 0) + (min(first_frame * 0.5, dur * 0.2) if fr == 0 else 0)
                if fr == 0:
                    t = max(t, renderer.seen_at[s + r])   # 1列目は、この行が実際に描かれる最初のフレーム以降（延ばした間は前の行が写る）
                im = renderer.still_frame(t, safe_overlay).resize((thumb_w, thumb_h), Image.BILINEAR)
                sheet.paste(im, (80 + k * thumb_w, r * thumb_h))
                if renderer.themed and renderer.cuts[s + r].karaoke:
                    n_lit, n_all = renderer.cuts[s + r].lit_count(t - c["start"])   # 点灯済み字数／全字数
                    d.text((80 + k * thumb_w + 4, r * thumb_h + 4), f"点灯 {n_lit}/{n_all}",
                           font=ImageFont.truetype(FONT_HEAVY, 13), fill=(255, 230, 120))
            if renderer.themed:
                # 役・配色・背景色と文字色の色コード（コントラストの照合用）
                pal = renderer.theme["palettes"][c["bg"]]
                tiny = ImageFont.truetype(FONT_HEAVY, 13)
                role_info = renderer._fonts.get(c["font_role"], {})
                size_px = renderer.cuts[s + r].size
                for k2, line in enumerate((f"{c['font_role']} {size_px}px", f"{c['bg']} {c.get('accent_mode', '')}",
                                           f"bg {pal['bg']}", f"tx {pal['text']}",
                                           f"{Path(str(role_info.get('path', ''))).name[:10]} i{role_info.get('index', '')}")):
                    d.text((6, r * thumb_h + 204 + k2 * 17), line, font=tiny, fill=(190, 210, 255))
                if c.get("impact"):
                    # 衝撃の段階と、寄りの値（外接矩形で頭打ちにした後）
                    d.text((6, r * thumb_h + 204 + 5 * 17),
                           f"衝撃 {c['impact']} 寄り{renderer.impact_zoom.get(c['index'], 0):.3f}",
                           font=tiny, fill=(190, 210, 255))
            if sung_col:
                e = c.get("sung_end")
                small = ImageFont.truetype(FONT_HEAVY, 15)
                if e is None:
                    d.text((6, r * thumb_h + 100), "E なし", font=small, fill=(255, 160, 160))
                else:
                    t = max(e - 0.2, c["start"])
                    nxt = plan[s + r + 1] if s + r + 1 < len(plan) else None
                    switched = nxt is not None and t >= nxt["start"]   # この列は次の行を写している
                    im = renderer.still_frame(t, safe_overlay).resize((thumb_w, thumb_h), Image.BILINEAR)
                    sheet.paste(im, (80 + len(fractions) * thumb_w, r * thumb_h))
                    d.text((6, r * thumb_h + 100), f"E {e:.2f}", font=small, fill=(255, 220, 120))
                    if switched:
                        # 次の行が E−0.2 秒より前に始まる。この列の絵は次の行で、「歌っている最中に消えた」ではない
                        d.text((80 + len(fractions) * thumb_w + 4, r * thumb_h + 4), "次の行に切替済",
                               font=small, fill=(255, 120, 120))
                    d.text((6, r * thumb_h + 120), f"W {_fmt(c.get('sung_w'))}", font=small, fill=(200, 200, 200))
                    d.text((6, r * thumb_h + 140), f"D {_fmt(c.get('sung_d'))}", font=small, fill=(200, 200, 200))
                    d.text((6, r * thumb_h + 160), f"余韻 {_fmt(c.get('tail'))}", font=small, fill=(200, 200, 200))
                    d.text((6, r * thumb_h + 180), f"終 {c['end']:.2f}", font=small, fill=(200, 200, 200))
        path = out_dir / f"sheet_{s // cuts_per_sheet + 1:02d}.png"
        sheet.save(path)
        sheets.append(path)
    if renderer.themed and (look or {}).get("direction"):
        # 動きの静止画（別シート）と、検査の報告。今の列（上）は変えない
        sheets.extend(render_motion_sheets(renderer, out_dir, cuts_per_sheet=cuts_per_sheet, safe_overlay=safe_overlay))
        if (look or {}).get("cache_dir"):
            renderer.write_look_report(look["cache_dir"])
    return sheets


def render_motion_sheets(renderer, out_dir, cuts_per_sheet=8, safe_overlay=False):
    """動きの静止画の別シート（sheet_motion_NN.png）。行の種類に応じた列。
    ラベル：時刻・役と大きさ・背景色と文字色の色コード・点灯済み字数・カウンターの値と不透明度・画面の寄り"""
    ent = renderer.motion_entries()
    if not ent:
        return []
    out_dir = Path(out_dir)
    thumb_w, thumb_h = 216, 384
    ncols = max(len(e[3]) for e in ent)
    label_font = ImageFont.truetype(FONT_HEAVY, 22)
    tiny = ImageFont.truetype(FONT_HEAVY, 12)
    sheets = []
    for s0 in range(0, len(ent), cuts_per_sheet):
        chunk = ent[s0:s0 + cuts_per_sheet]
        sheet = Image.new("RGB", (thumb_w * ncols + 140, thumb_h * len(chunk)), (40, 40, 40))
        d = ImageDraw.Draw(sheet)
        for r, (label, row, kind, times) in enumerate(chunk):
            y0 = r * thumb_h
            d.text((6, y0 + 8), f"#{row}" if row else "間奏", font=label_font, fill=(255, 255, 255))
            d.text((6, y0 + 40), label, font=tiny, fill=(255, 230, 120))
            if row:
                c = renderer.plan[row - 1]
                pal = renderer.theme["palettes"][c["bg"]]
                for k, line in enumerate((f"{c['font_role']} {renderer.cuts[row - 1].size}px", f"bg {pal['bg']}", f"tx {pal['text']}")):
                    d.text((6, y0 + 60 + k * 16), line, font=tiny, fill=(190, 210, 255))
            for k, t in enumerate(times):
                im = renderer.still_frame(t, safe_overlay).resize((thumb_w, thumb_h), Image.BILINEAR)
                x0 = 140 + k * thumb_w
                sheet.paste(im, (x0, y0))
                lines = [f"{t:.3f}s"]
                if row:
                    c = renderer.plan[row - 1]
                    o = renderer.cuts[row - 1]
                    if o.karaoke:
                        n_lit, n_all = o.lit_count(t - c["start"])
                        lines.append(f"点灯 {n_lit}/{n_all}")
                    bg = renderer.bg_color_at(t)
                    lines.append("bg #%02X%02X%02X" % tuple(int(round(v)) for v in bg))
                if renderer.counter is not None:
                    sts = renderer.counter.states(t)
                    lines.append("C " + (" ".join(f"{s_['value']}({s_['alpha']:.2f}{'' if s_['phase'] == 'run' else s_['phase'][0]})" for s_ in sts) or "なし"))
                _b, z, sx, sy = renderer.camera_at(t)
                lines.append(f"寄り {z:.3f}")
                for m, line in enumerate(lines):
                    d.text((x0 + 4, y0 + 4 + m * 14), line, font=tiny, fill=(255, 230, 120))
        path = out_dir / f"sheet_motion_{s0 // cuts_per_sheet + 1:02d}.png"
        sheet.save(path)
        sheets.append(path)
    return sheets


PLAN_VERSION = 13


def _direction_items(direction, n_lines):
    """行ごとの項目（行番号 0 始まりの配列）。direction の lines を展開する"""
    import look

    by_line = look.parse_line_keys(direction.get("lines", {}), n_lines)
    return [by_line.get(i + 1, {}) for i in range(n_lines)]


def direction_line_options(direction, n_lines, vdefaults, voice_key="voice"):
    """build_plan に渡す行ごとの余韻（tails）と固定の終わり（fixed_ends）。
    余韻は 行の tail → 声の既定 → None（style の tail_sec）の順"""
    items = _direction_items(direction, n_lines)
    tails, fixed = [], []
    for it in items:
        tail = it.get("tail")
        if tail is None:
            tail = (vdefaults.get(it.get(voice_key)) or {}).get("tail")
        tails.append(None if tail is None else float(tail))
        fixed.append(None if it.get("end") is None else float(it["end"]))
    return tails, fixed


_DIRECTION_EXITS = ("swap", "fade", "fall", "drift", "fly", "split", "shatter")


def _is_chorus_or_bridge(section):
    """Chorus・Bridge の区分か。Pre-Chorus は含まない（slam は Pre-Chorus には使える。動き §9-7 ②）"""
    t = (section or "").lower()
    for pre in ("pre-chorus", "pre chorus", "prechorus"):
        t = t.replace(pre, "")
    return "chorus" in t or "bridge" in t


def _window_words(words, plan, i):
    """行 i の区間 [開始 − 0.3, 次の行の開始) に始まる単語（開始順。sung_ends_from_words と同じ区間）"""
    lo = plan[i]["start"] - WORD_WINDOW_LEAD
    hi = plan[i + 1]["start"] if i + 1 < len(plan) else float("inf")
    return [w for w in words if lo <= w["start"] < hi]


def _match_line(flat, words, use_lcs):
    """行の字（段を連結した並び）と、区間の単語の字を、align と同じ取り方で対応させる（別の照合を作らない）。
    戻り値: (time_of {字の位置: 単語の開始}, word_of {字の位置: 単語の番号}, 照合の対象になる字数)。
    照合の対象は _normalize_char が None にしない字（空白・記号は対象外）。連続一致が MIN_MATCH_BLOCK 未満の字は対応させない"""
    import difflib

    from align import MIN_MATCH_BLOCK, _monotonic_blocks, _normalize_char

    units = [(i, n) for i, n in ((i, _normalize_char(ch)) for i, ch in enumerate(flat)) if n]
    w_chars, w_idx = [], []
    for wi, w in enumerate(words):
        for ch in w["word"]:
            n = _normalize_char(ch)
            if n:
                w_chars.append(n)
                w_idx.append(wi)
    a_chars = [u[1] for u in units]
    if use_lcs:
        blocks = _monotonic_blocks(a_chars, w_chars)
    else:
        blocks = difflib.SequenceMatcher(None, a_chars, w_chars, autojunk=False).get_matching_blocks()
    time_of, word_of = {}, {}
    for ia, ib, size in blocks:
        if size < MIN_MATCH_BLOCK:
            continue
        for k in range(size):
            time_of[units[ia + k][0]] = float(words[w_idx[ib + k]]["start"])
            word_of[units[ia + k][0]] = w_idx[ib + k]
    return time_of, word_of, len(units)


def _karaoke_times(n, time_of, start, cap):
    """n 字の点灯の時刻。対応した字 ＝ 単語の開始。対応しない字は、前後の対応した字の時刻の間を字数で均等割り
    （先頭側で前が無い字は行の開始、末尾側で後ろが無い字は cap（歌い終わり）に向けて割る。cap の字自体は作らない）。
    先頭は開始以上、末尾は cap 以下、単調増加に直す。対応が1字も無ければ None"""
    if not time_of:
        return None
    t = [None] * n
    for i, v in time_of.items():
        t[i] = v
    matched = sorted(time_of)
    for i in range(matched[0]):
        t[i] = start
    for a, b in zip(matched, matched[1:]):
        for i in range(a + 1, b):
            t[i] = t[a] + (t[b] - t[a]) * (i - a) / (b - a)
    last = matched[-1]
    for i in range(last + 1, n):
        t[i] = t[last] + (max(cap, t[last]) - t[last]) * (i - last) / (n - last)
    out, prev = [], start
    hi = max(cap, start)
    for x in t:
        x = max(min(max(x, start), hi), prev)
        out.append(round(x, 3))
        prev = x
    return out


def split_rows_after(rows, break_after, index):
    """break_after（{"<段番号>": <字の位置>}）を段に当てて、段を増やす。段番号は rows（全角スペースで分けた段）を 0 から数え、
    字の位置はその段の頭からの字数（その字の後で切る）。字は増減しない（鍵語の位置はずれない）。範囲外は止める"""
    import look

    out = []
    for ri, row in enumerate(rows):
        pos = break_after.get(str(ri))
        if pos is None:
            out.append(row)
            continue
        if not 0 < pos < len(row):
            raise look.LookError(f"direction: 行{index} の break_after[{ri}]＝{pos} は、段 {ri}（{len(row)}字）の中で切れる位置ではありません"
                                 f"（1〜{len(row) - 1} で書いてください）")
        out.extend([row[:pos], row[pos:]])
    for key in break_after:
        if int(key) >= len(rows):
            raise look.LookError(f"direction: 行{index} の break_after の段番号 {key} が、段の数（{len(rows)}）以上です")
    return out


def apply_direction(plan, direction, vdefaults, alignment=None, words=None, use_lcs=False, notes=None):
    """build_plan の後に、曲の演出（direction）を当てる。
    direction のある曲は、自動割り当て（装飾・質感・保持・退場・背景処理・下敷き・切替・強調・コマ打ち・フラッシュ・
    巨大文字・カメラ・寄りの漸増・強さ3の揺れ）を全カットでいったん切り、指定のある項目だけ当てる。
    指定の無い行の入りは cut（動かない入り）。退場は、次の行まで残る行は swap（退場なし）、空きがある行は fade。
    段3（impacts・karaoke・land・break_after）は、そのキーがある行・曲だけ動く。words は 開始順の単語時刻（whisper）。
    notes には警告の文を足す（呼び出し側が表示する）。カットの marks には kinetic_plan.md に出す印を足す"""
    import look

    items = _direction_items(direction, len(plan))
    impacts = direction.get("impacts")
    kcfg = direction.get("karaoke")
    if notes is None:
        notes = []

    def mark(c, text):
        c.setdefault("marks", []).append(text)
        notes.append(f"行{c['index']}: {text}")

    for i, (c, it) in enumerate(zip(plan, items)):
        c.update({"decor": None, "texture": None, "hold": None, "exit": None, "under": None, "bgfx": None,
                  "wipe": "straight", "emphasis": False, "koma": 0, "flash": False,
                  "echo": False, "camera": "still", "creep": 0.0, "shake": 0, "land_zoom": 0.0,
                  "beat_zoom": 0.0})
        entrance = it.get("entrance", "cut")
        if entrance not in ENTRANCE_FRAMES:
            raise RuntimeError(f"direction 行{c['index']}: 入り '{entrance}' は未対応です（{', '.join(ENTRANCE_FRAMES)}）")
        c["entrance"] = c["motion"] = entrance
        if it.get("layout") is not None:
            if it["layout"] not in LAYOUTS:
                raise RuntimeError(f"direction 行{c['index']}: 構図 '{it['layout']}' は未対応です")
            c["layout"] = it["layout"]
            if it["layout"] == "vertical":
                # direction で縦組みを指定した行だけ新しい組版（vert）。自動の構図選びで vertical になった行は従来の描き方のまま
                c["vertical_typeset"] = "vert"
                if it.get("max_col_chars") is not None:
                    c["max_col_chars"] = it["max_col_chars"]
        if it.get("hold") is not None:
            if it["hold"] not in HOLD_MOTIONS + ("heartbeat", CARRY):
                raise RuntimeError(f"direction 行{c['index']}: 保持 '{it['hold']}' は未対応です")
            c["hold"] = it["hold"]
            if it["hold"] == CARRY:
                c["carry"] = json.loads(json.dumps(it["carry"]))      # 書き込み先（プラン）と direction を共有しない
        if it.get("decor") is not None:
            c["decor"] = it["decor"]
        if it.get("text_y") is not None:
            c["text_y"] = float(it["text_y"])    # 文字の中心位置（画面の高さに対する割合。既定 0.5。点の層の下で読み字を下寄りに置く等）
        if it.get("align_to_prev"):
            c["align_to_prev"] = True            # 前の行の先頭字の x にそろえる（描画器が、カットを組んだ後に当てる）
        if it.get("ink") is not None:
            c["read_ink"] = dict(it["ink"])     # 読み字の質感（かすれ。_Cut が掛ける）
        if it.get("voice") is not None:
            c["voice"] = it["voice"]

        # --- 段3：衝撃の段階（slam）。impacts のある曲だけ。無ければ今までの slam
        if entrance == "slam" and impacts is not None:
            name = it.get("impact") or "m"
            if name not in impacts:
                raise look.LookError(f"direction: 行{c['index']} は impact の指定が無く、既定の 'm' が impacts にありません")
            if _is_chorus_or_bridge(c["section"]):
                raise look.LookError(f"direction: 行{c['index']}（{c['section']}）は Chorus・Bridge の区分なので slam は使えません")
            c["impact"] = name
            c["impact_vals"] = dict(impacts[name])
        # --- 着地の時刻
        if it.get("land") is not None:
            LF = c["impact_vals"]["land_frames"] if c.get("impact_vals") else 3
            floor = c["start"] + LF / FPS      # 入りの頭（着地 − 着地フレーム）が行の開始より前に来ないように
            # 入りの頭（p＝0）を、行の開始以降で最初に描かれる 30fps のフレームに置く（遅れは最大1フレーム）。
            # 着地 ＝ その格子の時刻 ＋ 着地フレーム ÷ 30。こうすると描かれるフレームの p は 0, 1, 2, … の整数になる
            grid_land = math.ceil(c["start"] * FPS - 1e-9) / FPS + LF / FPS
            digits = 3
            if it["land"] == "start":
                land, digits = grid_land, 6
            else:
                if words is None:
                    raise look.LookError(f"direction: 行{c['index']} の land: first_word には単語時刻（whisper_words）が要ります")
                win = _window_words(words, plan, i)
                if not win:
                    land, digits = grid_land, 6
                    mark(c, "着地：区間に単語が無いので 開始のフレームに寄せる（land: start と同じ）")
                else:
                    w0 = float(win[0]["start"])
                    if w0 - c["start"] >= 0.5:
                        land, digits = grid_land, 6
                        mark(c, f"着地：最初の単語が開始より {w0 - c['start']:.2f} 秒遅いので 開始のフレームに寄せる（land: start と同じ。開始時刻の確認が要る）")
                    else:
                        land = max(w0, floor)
            c["land"] = round(land, digits)
        # --- karaoke と break_after・row_lengths の単語の途中の警告（同じ対応表を使う）
        need_match = entrance == "karaoke" or it.get("break_after") or it.get("row_lengths")
        match_result = None
        if need_match:
            if words is None:
                raise look.LookError(f"direction: 行{c['index']} の karaoke / break_after には単語時刻（whisper_words）が要ります")
            flat = "".join(c["rows"])
            match_result = _match_line(flat, _window_words(words, plan, i), use_lcs)
        if entrance == "karaoke":
            c["karaoke"] = {"unlit_opacity": kcfg["unlit_opacity"], "light_frames": kcfg["light_frames"],
                            "keyword_unlit": kcfg.get("keyword_unlit", "text")}
            if it.get("karaoke_land"):
                c["karaoke_land"] = True
                if match_result[0]:
                    c["karaoke_land_at"] = round(max(min(match_result[0].values()), c["start"]), 3)   # 最初の単語の開始
            time_of, word_of_k, n_units = match_result
            cover = len(time_of) / n_units if n_units else 0.0
            c["karaoke_cover"] = round(cover, 2)
            match = (alignment[i].get("match") if alignment else None)
            if (match is not None and match < kcfg["min_match"]) or cover < kcfg["min_cover"] or not time_of:
                c["karaoke_all_lit"] = True
                mark(c, f"karaoke：対応が足りない（match {match}、対応 {cover:.0%}）ので全文点灯に落とした")
            else:
                # 点灯の上限は表示の終わり（direction の end で固定した行は sung_end が end より後になりうる）
                cap = min(c["sung_end"], c["end"]) if c.get("sung_end") is not None else c["end"]
                if it.get("karaoke_cap") == "word" and word_of_k:
                    # karaoke_cap: word（T35 U9）：点灯の上限を、声の終わりでなく、最後に対応した単語の終わりにする（表示の終わりを超えない）
                    win_k = _window_words(words, plan, i)
                    cap = min(float(win_k[word_of_k[max(word_of_k)]]["end"]), c["end"])
                c["char_times"] = _karaoke_times(len(flat), time_of, c["start"], cap)
        # --- 段3後半：カウンター（行の状態と、増える時刻の元になる単語の開始）・solo
        if direction.get("counter") is not None:
            if words is None:
                raise look.LookError(f"direction: 行{c['index']}：counter には単語時刻（whisper_words）が要ります")
            if "counter" in it:
                c["counter"] = it["counter"]
            # 数えるのは、その行の表示の中 [開始, 表示の終わり) に始まる単語だけ（動き §10-8 ③）。開始 − 0.3 の前倒しは karaoke・着地用で、
            # 隣の行と重ねて2回数えない。表示の終わりの後の隙間（歌詞外の声）の単語では増やさない。次の行へ入れ替わる行（終わり ≧ 次の開始）は
            # 次の行の開始まで
            hi = c["end"]
            if i + 1 < len(plan) and c["end"] >= plan[i + 1]["start"] - 0.0015:
                hi = plan[i + 1]["start"]
            c["counter_words"] = [round(float(w["start"]), 3) for w in words if c["start"] <= w["start"] < hi]
        if it.get("solo"):
            c["solo"] = True
        if it.get("break_after"):
            split_rows_after(c["rows"], it["break_after"], c["index"])   # 範囲の検査（ここで止める）
            _time_of, word_of, _n = match_result
            for key, pos in sorted(it["break_after"].items(), key=lambda kv: int(kv[0])):
                off = sum(len(r) for r in c["rows"][:int(key)]) + pos
                if off in word_of and off - 1 in word_of:
                    if word_of[off] == word_of[off - 1]:
                        mark(c, f"break_after[{key}]＝{pos}：単語の途中で切っています（段 {key} の {pos} 字目の後）")
                else:
                    mark(c, f"break_after[{key}]＝{pos}：単語の途中かは判定できません（隣の字が単語に対応していない）")
        if it.get("row_lengths"):
            # 段の切れ目（先頭からの字数の和）が単語の途中なら、break_after と同じ印（T-6b）
            _time_of, word_of, _n = match_result
            off = 0
            sp_pos, pos_ = set(), 0                      # 全角スペースのある位置（次の字の通し番号）。切れ目がここと重なれば単語の途中ではない
            for ch_ in c["text"]:
                if ch_ == "\u3000":
                    sp_pos.add(pos_)
                else:
                    pos_ += 1
            for k_, n_ in enumerate(it["row_lengths"][:-1]):
                off += n_
                if off in sp_pos:
                    continue
                if off in word_of and off - 1 in word_of:
                    if word_of[off] == word_of[off - 1]:
                        mark(c, f"row_lengths[{k_}]＝{n_}：単語の途中で切っています（先頭から {off} 字目の後）")
                else:
                    mark(c, f"row_lengths[{k_}]＝{n_}：単語の途中かは判定できません（隣の字が単語に対応していない）")
    for gi, st in enumerate(direction.get("stack") or []):
        a, b = st["lines"]
        for k in range(a, b + 1):
            c = plan[k - 1]
            nxt = plan[k] if k < len(plan) else None
            if nxt is None or c["end"] < nxt["start"] - 0.0015:
                raise look.LookError(f"direction: stack[{gi}] の行{k} の表示が、次の行の開始まで続きません（終わり {c['end']:.2f}秒）。"
                                     f"積んだ列を残すには、行に tail（行の長さより大きい値）か end を書いて、次の行の開始まで表示してください")
            c["stack_group"], c["stack_dim"], c["stack_clear_line"] = gi, float(st["dim"]), st["clear_at_line"]
    for i, (c, it) in enumerate(zip(plan, items)):
        nxt = plan[i + 1] if i + 1 < len(plan) else None
        name = it.get("exit")
        if name is None:
            name = "swap" if nxt is not None and c["end"] >= nxt["start"] - 0.0015 else "fade"
        elif not (name in _DIRECTION_EXITS or _exit_fade_sec(name) is not None):
            raise RuntimeError(f"direction 行{c['index']}: 消え方 '{name}' は未対応です")
        if c.get("stack_group") is not None:
            name = "swap"          # 積んだ列は消え方の動きを持たない（残した列を最後の状態のまま描くため）
        c["exit"] = name
        _fit_exit_to_tail(c)
    return plan


def arrange_vertical(plan, direction, theme, vdefaults, words=None, use_lcs=False, notes=None):
    """direction で縦組みを指定した行（vertical_typeset）の、段の切り方と字の大きさを決めてプランに書く（rows・vertical_size・
    vertical_top・vertical_h・vertical_kana_shift）。段の切れ目は break_after があればそれ、無ければ単語の切れ目
    （単語時刻が無ければ字種の変わり目）。禁則・ぶら下げ・縦中横は kinetic_vertical。下限を割るなら段を増やし、それでも割るなら止める"""
    import re
    import look

    marked = [c for c in plan if c.get("vertical_typeset")]
    if not marked:
        return
    kinetic_vertical.need_raqm()
    items = _direction_items(direction, len(plan)) if direction is not None else [{} for _ in plan]
    vspec = (direction or {}).get("vertical") or {}
    top = float(vspec.get("top", kinetic_vertical.DEFAULT_TOP))
    height = float(vspec.get("height", kinetic_vertical.DEFAULT_H))
    for i, (c, it) in enumerate(zip(plan, items)):
        if not c.get("vertical_typeset"):
            continue
        flat = "".join(c["rows"])
        if re.search(r"[A-Za-z]{3,}", flat):
            raise look.LookError(f"direction: 行{c['index']} は縦組みですが、3字以上の欧文の語を含みます（縦組みにしません）。止めました")
        if c.get("profile_kids"):
            raise look.LookError(f"direction: 行{c['index']}: 縦組み（vert）は子ども向けの書体には使えません")
        # 上限・下限：テーマあり＝役（行の max_px・min_px が先）、無し＝従来の上限（強さ1＝320・他＝220）と下限 60
        if theme is not None:
            vd = vdefaults.get(c.get("voice")) or {}
            role = c.get("font_role") or it.get("role") or vd.get("role") or theme.get("default_role")
            spec = theme["fonts"][role]
            cap_role, min_role = spec.get("max_px", 150), spec.get("min_px", 60)
            for rr in (c.get("row_roles") or {}).values():      # 段ごとの役がある行は、行の役と段の役のうち厳しい方（上限は小さい方、下限は大きい方。T35 L2）
                cap_role = min(cap_role, theme["fonts"][rr].get("max_px", 150))
                min_role = max(min_role, theme["fonts"][rr].get("min_px", 60))
            cap = int(c.get("max_px") or cap_role)
            min_px = int(c.get("min_px") or min_role)
        else:
            cap, min_px = (320 if c["tier"] == 1 else 220), 60
        forced = None
        if it.get("break_after"):
            split = split_rows_after(c["rows"], it["break_after"], c["index"])
            forced = [len(r) for r in split]
            c["break_after"] = it["break_after"]
        word_of = {}
        if words is not None:
            _t, word_of, _n = _match_line(flat, _window_words(words, plan, i), use_lcs)
        mid = kinetic_vertical.mid_word_flags(flat, word_of)
        rows, size = kinetic_vertical.arrange(flat, mid, int(c.get("max_col_chars", kinetic_vertical.DEFAULT_MAX_COL)),
                                              height, cap, min_px, forced=forced,
                                              usable_w=((theme.get("layout") or {}).get("text_width") if theme is not None else None))   # 縦組みの横幅はテーマの text_width に従う（無ければ従来の 896。T35 U8-b）
        if notes is not None and not word_of and len(rows) > 1:
            # 単語の時刻が照合できない行は、字種の変わり目で切る（助詞が段の頭に来ることがある）。黙らず知らせる
            heads = [r[0] for r in rows[1:] if r[0] in kinetic_vertical.PARTICLES]
            if heads:
                msg = (f"縦組み: 単語の時刻が照合できないため、段の切れ目を字種の変わり目で決めました。"
                       f"助詞（{''.join(heads)}）が段の頭に来ています。break_after で切れ目を指定してください")
                c.setdefault("marks", []).append(msg)
                notes.append(f"行{c['index']}: {msg}")
        c["rows"] = rows
        c["vertical_size"] = size
        c["vertical_top"] = top
        c["vertical_h"] = height
        if vspec.get("kana_shift"):
            c["vertical_kana_shift"] = float(vspec["kana_shift"])


def arrange_stack(plan, direction):
    """stack（前の列を残して薄くする。T35 C2）の、列の位置。積む行（lines の最初〜最後）の全部の列を右から順に並べ、**積んだ全部の幅の中央を
    画面の中央に置く**（後の行が来ても前の列が動かない）。行ごとの横のずれ stack_dx（px。その行の組版の中央の位置からの差）をプランに書く。
    列の送りは行の字の大きさ × COL_PITCH。収まるか（左右の余白）はカットを組んだ後の検査（KineticRenderer._setup_stack）"""
    for st in (direction or {}).get("stack") or []:
        a, b = st["lines"]
        cuts = [plan[k - 1] for k in range(a, b + 1)]
        widths = [len(c["rows"]) * c["vertical_size"] * kinetic_vertical.COL_PITCH for c in cuts]
        right = sum(widths) / 2
        off = 0.0
        for c, w in zip(cuts, widths):
            c["stack_dx"] = round(right - off - w / 2, 2)
            off += w


def apply_accent_rows(plan):
    """accent: rows の行の accent_rows（段番号。0 始まり）を、差し色にする字の位置 accent_idx（段を連ねた中の位置。key_word と同じ持ち方）に直す。
    段が確定した後（break_after・縦組みの段の切り方の後）に呼ぶ。段番号が段の数以上なら止める（T35 L1）"""
    import look

    for c in plan:
        for k in (c.get("row_roles") or {}):       # 段ごとの書体の役（L2）の段番号の範囲
            if int(k) >= len(c["rows"]):
                raise look.LookError(f"direction: 行{c['index']} の row_roles の段番号 {k} が、段の数（{len(c['rows'])}）以上です")
        if c.get("accent_mode") != "rows":
            continue
        rows = c["rows"]
        idx = []
        for ri in c["accent_rows"]:
            if ri >= len(rows):
                raise look.LookError(f"direction: 行{c['index']} の accent_rows の段番号 {ri} が、段の数（{len(rows)}）以上です")
            off = sum(len(r) for r in rows[:ri])
            idx.extend(range(off, off + len(rows[ri])))
        c["accent_idx"] = idx


def extract_key_word(plan, spec):
    """鍵語を、実行時に歌詞から取り出す。rule == first_bracket: from_line 行目の最初の「」の中の文字列。
    歌詞の文字列は direction にもテーマにも書かない（ここで取り出すだけ）"""
    import re
    import look

    n = spec["from_line"]
    m = re.search(r"「([^」]+)」", plan[n - 1]["text"])
    if not m:
        raise look.LookError(f"direction: key_word の取り出し元の行{n}に「」がありません")
    return m.group(1)


def apply_look(plan, theme, direction, vdefaults):
    """テーマ（書体の役・名前付きの配色）を、各カットに当てる。direction があれば行ごとの指定を使い、
    無ければ（--look だけ）テーマの default_role・default_palette を全カットに当てる。
    カットに入れるもの: font_role（役）、bg（配色の名前。添字ではない）、accent_mode（none・key_word・fill・outline）、
    accent_idx（key_word のとき、行の文字を段の順に並べた中の差し色にする位置）、max_px・tracking（行ごとの上書き）。
    背景画像は使わない（bg は常に配色の名前）。戻り値: 見つかった食い違いの一覧（表示用）"""
    import look

    items = _direction_items(direction, len(plan)) if direction is not None else [{} for _ in plan]
    key_text = None
    if direction is not None and direction.get("key_word"):
        key_text = extract_key_word(plan, direction["key_word"])
    notes = []
    flagged, containing = set(), set()
    for c, it in zip(plan, items):
        vd = vdefaults.get(it.get("voice") or c.get("voice")) or {}
        role = it.get("role") or vd.get("role") or theme.get("default_role")
        palette = it.get("palette") or vd.get("palette") or theme.get("default_palette")
        if role is None or palette is None:
            raise look.LookError(
                f"行{c['index']}: 書体の役か配色が決まりません（行の role・palette、声の role・palette、"
                f"テーマの default_role・default_palette のどれかで指定してください）")
        c["font_role"] = role
        c["bg"] = palette
        mode = it.get("accent", "none")
        c["accent_mode"] = mode
        if mode == "rows":
            c["accent_rows"] = list(it["accent_rows"])      # 字の位置（accent_idx）は段が確定した後（apply_accent_rows）
        if it.get("row_roles"):
            c["row_roles"] = dict(it["row_roles"])
        for key in ("max_px", "tracking", "min_px", "clear_cap"):
            if key in it:
                c[key] = it[key]
        if it.get("row_lengths"):
            # 自動の段（build_plan の「最後の2字」の切れ目など）を捨てて、行の字を先頭から指定の字数ずつの段にする（T35 U5。字は増減しない）
            if c.get("vertical_typeset"):      # direction に書いた縦組みは look 側の検査が先に止める。自動で縦組みになった行は apply_look が center に戻すので通す
                raise look.LookError(f"direction: 行{c['index']} の row_lengths は横組みの行だけに書けます")
            if c.get("latin") or any(ch.isspace() and ch != "\u3000" for ch in c["text"]):
                raise look.LookError(f"direction: 行{c['index']} の row_lengths は、半角スペースなど全角スペース以外の空白を含む行・欧文の行には書けません"
                                     f"（段を字数で組み直すと空白が消えて語がくっつく。break_after を使ってください）")
            flat_rl = "".join(c["rows"])
            if flat_rl != c["text"].replace("\u3000", ""):
                raise look.LookError(f"direction: 行{c['index']} の row_lengths：行の字（全角スペースを除く）がプランの段と一致しません。何も直さず止めました")
            if sum(it["row_lengths"]) != len(flat_rl):
                raise look.LookError(f"direction: 行{c['index']} の row_lengths {it['row_lengths']} の合計 {sum(it['row_lengths'])} が、"
                                     f"行の字数 {len(flat_rl)} と違います")
            pos, new_rows = 0, []
            for n_ in it["row_lengths"]:
                new_rows.append(flat_rl[pos:pos + n_])
                pos += n_
            c["rows"] = new_rows
            c["row_lengths"] = list(it["row_lengths"])
            # 全角スペース：段の中に来たものは 0.5字分の空きとして描く（row_gaps＝段ごとの、空きを置く字の段内の位置）。段の切れ目に来たものは描かない（T35 U11）
            starts, acc = [], 0
            for n_ in it["row_lengths"]:
                starts.append(acc)
                acc += n_
            gaps = [[] for _ in new_rows]
            pos = 0
            for ch_ in c["text"]:
                if ch_ == "\u3000":
                    for ri_, n_ in enumerate(it["row_lengths"]):
                        if starts[ri_] < pos < starts[ri_] + n_ and (pos - starts[ri_]) not in gaps[ri_]:   # 続いた全角スペースは1つの空き
                            gaps[ri_].append(pos - starts[ri_])
                else:
                    pos += 1
            if any(gaps):
                c["row_gaps"] = gaps
        if it.get("break_after") and not c.get("vertical_typeset"):   # 縦組み（vert）の段は arrange_vertical が切る
            c["rows"] = split_rows_after(c["rows"], it["break_after"], c["index"])
            c["break_after"] = it["break_after"]
        pal = theme["palettes"][palette]
        if direction is not None and look.is_light_palette(pal):
            bad = [name for name, hit in (
                (f"入り {c.get('entrance')}", c.get("entrance") in look.LIGHT_BAD_ENTRANCES),
                (f"decor {c.get('decor')}", c.get("decor") in look.LIGHT_BAD_DECOR),
                (f"質感 {c.get('texture')}", c.get("texture") in look.LIGHT_BAD_TEXTURES),
                ("flash", bool(c.get("flash")))) if hit]
            if bad:
                raise look.LookError(f"行{c['index']}: 配色 '{palette}' は明るい地（背景が文字より明るい）ですが、白地で合わない部品"
                                     f"（{', '.join(bad)}）が指定されています。止めました（neon・tape・misregister・long_shadow・flash は暗い地用）")
        if mode == "glow":
            gspec = (theme.get("parts") or {}).get("glow")
            if gspec is None:
                raise look.LookError(f"direction: 行{c['index']} は accent: glow ですが、テーマに parts.glow がありません")
            ratio = look.contrast(pal["text"], look.over(pal.get("accent", pal["text"]), pal["bg"], gspec["max_alpha"]))
            if ratio < look.GLOW_MIN_CONTRAST:
                raise look.LookError(f"行{c['index']}: 配色 '{palette}' の文字と glow を重ねた背景の比が {ratio:.2f} で、"
                                     f"{look.GLOW_MIN_CONTRAST} を割ります。止めました")
        if c.get("entrance") == "karaoke":
            u = pal.get("unlit_opacity", (c.get("karaoke") or {}).get("unlit_opacity", 0.65))
            ratio = look.worst_contrast(pal["text"], pal["bg"], u)
            if ratio < look.UNLIT_MIN_CONTRAST:
                raise look.LookError(f"行{c['index']}: 配色 '{palette}' の未点灯（不透明度 {u}、最悪の背景＝背景±10。暗い地は +10・明るい地は −10）の比が {ratio:.2f} で、"
                                     f"{look.UNLIT_MIN_CONTRAST} を割ります。この組だけ unlit_opacity を上げてください。止めました")
        if c["layout"] == "vertical" and not c.get("vertical_typeset"):
            # 自動の構図選びで vertical になった行は center に戻す（従来どおり。出力を変えない）。
            # direction で縦組みを指定した行（vertical_typeset）は通す（raqm の vert で縦用の字形を取る）
            c["layout"] = "center"
        flat = "".join(c["rows"])
        idx = []
        if key_text:
            pos = flat.find(key_text)
            while pos >= 0:
                idx.extend(range(pos, pos + len(key_text)))
                pos = flat.find(key_text, pos + len(key_text))
            if idx:
                containing.add(c["index"])
        if mode == "key_word":
            flagged.add(c["index"])
            if not key_text:
                raise look.LookError(f"direction: 行{c['index']}は accent: key_word ですが、direction に key_word の規則がありません")
            if idx:
                c["accent_idx"] = idx
            else:
                notes.append(f"行{c['index']}: accent は key_word ですが、鍵語がこの行にありません（差し色なし）")
        else:
            c.pop("accent_idx", None)
    for n in sorted(containing - flagged):
        notes.append(f"行{n}: 鍵語を含みますが accent が key_word ではありません（差し色なし）")
    return notes


def _plan_variant(style, theme, direction, sung_ends, duration):
    """既定の経路（既定スタイル・テーマなし・direction なし）では None。それ以外は設計を決める入力の digest。
    kinetic_plan.json のキャッシュ判定に使う（PLAN_VERSION は上げない。上げると既存の曲のプランが全部作り直される）"""
    import look

    if style.get("hold_mode", "cap") == "cap" and theme is None and direction is None:
        return None
    return look._digest({
        "hold_mode": style.get("hold_mode", "cap"), "tail_sec": style.get("tail_sec"),
        "theme": None if theme is None else look.theme_digest(theme),
        "direction": None if direction is None else look.direction_digest(direction),
        "sung_ends": sung_ends,
        "duration": None if duration is None else round(float(duration), 2),
    })


def prepare_plan(cache_dir, alignment, sections, beats, style, *, meta=None, backgrounds=None,
                 plan_path=None, replan=False, duration=None, look_name=None, use_direction=True,
                 lyric_lines=None, quiet=False, direction_data=None):
    """カット設計を作る入口。CLI と GUI（プレビュー・書き出し・静止画）はここを通る。
    cache_dir/direction.json があれば（use_direction のとき）曲の演出を当てる。--look / direction の look でテーマを読む。
    plan_path があればキャッシュ（kinetic_plan.json）を使う。無ければ毎回作る（GUI のプレビュー用）。
    direction_data を渡すと、cache_dir の direction.json の代わりにそれを使う（--direction-from が、置く前に検査するため）。
    戻り値: (plan, runtime)。runtime["style"] は描画に渡す style（direction のある曲は歌い終わり方式）。"""
    import look
    from align import VOCALS_NAME, detect_language, words_cache_path, words_source_of

    cache_dir = Path(cache_dir)
    direction = direction_data if direction_data is not None else (look.load_direction(cache_dir) if use_direction else None)
    if direction is not None:
        look.check_direction(direction, len(alignment))
    theme_name = look_name or (direction or {}).get("look")
    theme = look.load_theme(theme_name) if theme_name else None
    fonts = look.resolve_theme_fonts(theme) if theme else {}
    if theme and not quiet:
        print(f"      見た目: {theme_name}（{look.theme_digest(theme)}）")
        for role, f in fonts.items():
            print(f"        役 {role}: {f['family']} {f['style']} index {f['index']}  {f['path']}")
    if direction is not None and not quiet:
        print(f"      演出: {look.direction_path(cache_dir)}（{len(direction.get('lines', {}))}件の行指定）")

    eff_style = dict(style)
    if direction is not None:
        eff_style["hold_mode"] = "sung_end"   # 歌い終わり E ＋ 余韻。余韻は direction の声・行で決まる
    sung_ends = tails = fixed = None
    words, use_lcs = None, False
    vdefaults = look.voice_defaults(direction, theme)
    if direction is not None:
        look.validate_direction(direction, len(alignment), vdefaults, theme)
    if eff_style.get("hold_mode") == "sung_end":
        cached = json.loads((cache_dir / "alignment.json").read_text(encoding="utf-8"))
        lines = lyric_lines if lyric_lines is not None else [a["line"] for a in alignment]
        wpath = words_cache_path(cache_dir, detect_language(lines), vocals=words_source_of(cached) == "vocals")
        wsegs = json.loads(wpath.read_text(encoding="utf-8"))
        w_ends = sung_ends_from_words(alignment, wsegs)
        # 単語時刻（開始順）。slam の着地・karaoke の点灯・break_after の警告が同じ読み込みを使う
        words = sorted((w for seg in wsegs for w in seg.get("words", [])), key=lambda w: w["start"])
        use_lcs = words_source_of(cached) == "vocals"
        vpath = cache_dir / VOCALS_NAME
        d_ends = vocal_ends(alignment, vpath, cache_dir, w_ends=w_ends) if vpath.exists() else None
        sung_ends = combine_sung_ends(w_ends, d_ends)
        if direction is not None:
            tails, fixed = direction_line_options(direction, len(alignment), vdefaults)

    def post(plan):
        if direction is not None:
            stage_notes = []
            apply_direction(plan, direction, vdefaults, alignment=alignment, words=words, use_lcs=use_lcs,
                            notes=stage_notes)
            if not quiet:
                for note in stage_notes:
                    print(f"      [警告] {note}")
            for c, tl in zip(plan, tails):
                c["tail"] = eff_style.get("tail_sec", 0.6) if tl is None else tl
        if theme is not None:
            for note in apply_look(plan, theme, direction, vdefaults):
                if not quiet:
                    print(f"      [警告] {note}")
        if direction is not None:
            vnotes = []
            arrange_vertical(plan, direction, theme, vdefaults, words=words, use_lcs=use_lcs, notes=vnotes)
            arrange_stack(plan, direction)
            apply_accent_rows(plan)
            if not quiet:
                for note in vnotes:
                    print(f"      [警告] {note}")
        return plan

    variant = _plan_variant(eff_style, theme, direction, sung_ends, duration)
    header = None
    if theme:
        header = [f"<!-- 見た目: {theme_name}（{look.theme_digest(theme)}） -->"] + [
            f"<!-- 役 {r}: {f['family']} {f['style']} index {f['index']} {f['path']} -->" for r, f in fonts.items()]
    kwargs = dict(meta=meta, backgrounds=backgrounds, sung_ends=sung_ends, tails=tails, fixed_ends=fixed,
                  duration=duration, post=post)
    if plan_path is None:
        plan = post(build_plan(alignment, sections, beats, eff_style, meta, backgrounds, sung_ends=sung_ends,
                               tails=tails, fixed_ends=fixed, duration=duration))
    else:
        plan = load_or_build_plan(plan_path, alignment, sections, beats, eff_style, replan=replan,
                                  variant=variant, md_header=header, **kwargs)
    runtime = {"style": eff_style, "look": theme_name, "theme": theme, "fonts": fonts,
               "direction": direction is not None, "direction_data": direction, "variant": variant,
               "duration": duration, "cache_dir": str(cache_dir),
               "digest": {"look": None if theme is None else look.theme_digest(theme),
                          "direction": None if direction is None else look.direction_digest(direction)}}
    return plan, runtime


def load_or_build_plan(plan_path, alignment, sections, beats, style, replan=False, meta=None, backgrounds=None,
                       sung_ends=None, tails=None, fixed_ends=None, duration=None, variant=None, post=None,
                       md_header=None):
    """variant: 既定の経路では None。それ以外（歌い終わり方式・テーマ・direction）は設計を決める入力の digest。
    保存する JSON には variant を既定の経路以外のときだけ書く（既存の曲のファイルは1バイトも変わらない）。"""
    plan_path = Path(plan_path)
    lines = [a["line"] for a in alignment]
    only_variant = False
    if plan_path.exists() and not replan:
        saved = json.loads(plan_path.read_text(encoding="utf-8"))
        same_base = ([c["text"] for c in saved.get("plan", [])] == lines and saved.get("alignment") == alignment
                     and saved.get("version") == PLAN_VERSION and saved.get("meta") == meta
                     and saved.get("backgrounds") == (backgrounds or []))
        if same_base and saved.get("variant") == variant:
            return saved["plan"]
        only_variant = same_base
        print("      kinetic_plan.json は歌詞・タイミング・曲情報・設計の版のいずれかが変わったため作り直します")
    if only_variant:
        # 設計の入力（--look・--no-direction・direction・スタイルの違い）だけが変わった作り直し。
        # 手で直したプランを消さないよう、alignment.bak-* と同じ形で退避してから上書きする
        bak = plan_path.with_name(f"{plan_path.stem}.bak-{datetime.now().strftime('%Y%m%d-%H%M%S')}.json")
        shutil.copy2(plan_path, bak)
        print(f"      既存の {plan_path.name} を退避: {bak.name}")
    plan = build_plan(alignment, sections, beats, style, meta, backgrounds, sung_ends=sung_ends,
                      tails=tails, fixed_ends=fixed_ends, duration=duration)
    if post is not None:
        plan = post(plan)
    plan_path.parent.mkdir(parents=True, exist_ok=True)
    saved = {"version": PLAN_VERSION, "meta": meta, "backgrounds": backgrounds or [],
             "alignment": alignment, "plan": plan}
    if variant is not None:
        saved["variant"] = variant
    plan_path.write_text(json.dumps(saved, ensure_ascii=False, indent=1), encoding="utf-8")
    plan_path.with_suffix(".md").write_text(plan_to_markdown(plan, md_header), encoding="utf-8")
    return plan
