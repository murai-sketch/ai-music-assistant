#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
effects.py
7種類の「マスタリング」エフェクト名 -> ffmpeg音声フィルタ文字列のマッピング。

murai-sketch/lyricflow の src/lib/mastering.js (Web Audio API のノードチェーン)
と同じ7つの名前・同じ意図を再現することを狙った、ffmpegの-afフィルタグラフでの
再実装。Web AudioとffmpegはDSPパラメータの単位・スケールが異なるため、数値は
「近似値」であり波形のビット一致は目指さない。

各フィルタ文字列はffmpegの-afに直接渡せる形（カンマ区切りで連結可能）。
"""

# 元実装 (lyricflow の MASTERING_FEATURES 配列) と同じ固定順。
# --effects で指定した名前は、この順に並べ替えてからチェーンする。
EFFECT_ORDER = ["loudness", "radio", "eq", "comp", "stereo", "saturation", "deesser"]

FFMPEG_FILTERS = {
    # 音圧調整: DynamicsCompressor(threshold -24dB, ratio 12, knee 30dB,
    # attack 3ms, release 250ms) + 後段gain 1.4x
    # threshold -24dB を線形振幅に変換: 10^(-24/20) = 0.0631
    "loudness": "acompressor=threshold=0.0631:ratio=12:attack=3:release=250:knee=6,volume=1.4",

    # ラジオトーンエフェクト: Bandpass(2200Hz, Q0.6) + gain 1.2x
    "radio": "bandpass=f=2200:width_type=q:w=0.6,volume=1.2",

    # イコライザー調整: lowshelf 200Hz +3dB / peaking 3000Hz Q1 -2dB / highshelf 8000Hz +4dB
    "eq": "bass=g=3:f=200,equalizer=f=3000:width_type=q:w=1:g=-2,treble=g=4:f=8000",

    # AIマルチバンドコンプ（実体は単一コンプ）: threshold -18dB, ratio 6, knee 6dB,
    # attack 10ms, release 100ms
    # threshold -18dB を線形振幅に変換: 10^(-18/20) = 0.1259
    "comp": "acompressor=threshold=0.1259:ratio=6:attack=10:release=100:knee=2",

    # ステレオ幅拡張: Gain+Delay(20ms)をL/Rにマージした簡易Haas効果
    # ffmpeg組み込みのhaasフィルタで代替
    "stereo": "haas",

    # サチュレーション: WaveShaper(tanhカーブ, drive 3) + 後段gain 0.85
    # ffmpegのasoftclipにoversample/driveパラメータは無いため、type=tanhのみ使用
    "saturation": "asoftclip=type=tanh,volume=0.85",

    # デ・エッサー: 6500Hz Q2 -6dB のpeaking filter
    # ffmpeg組み込みのdeesserフィルタはfreqが0-1正規化値でHz直接指定できないため、
    # 元の狙い（6500Hz）を再現できるequalizerで代替する。
    "deesser": "equalizer=f=6500:width_type=q:w=2:g=-6",
}


def build_filter_graph(effect_names):
    """有効化するエフェクト名のリスト（任意の順で渡してよい）から、
    EFFECT_ORDER順に並べ替えた -af 用フィルタグラフ文字列を返す。"""
    unknown = set(effect_names) - set(FFMPEG_FILTERS)
    if unknown:
        raise ValueError(f"不明なエフェクト名: {', '.join(sorted(unknown))}")

    ordered = [name for name in EFFECT_ORDER if name in effect_names]
    if not ordered:
        raise ValueError("有効なエフェクトが1つも指定されていません")

    return ",".join(FFMPEG_FILTERS[name] for name in ordered), ordered
