#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
styles.py
アニメーションの見た目は render.py 側の1つの汎用レンダラーで実装し、
スタイルごとの違いは「パラメータ辞書の差分」だけで表現する
（kawaii/deathcore/kawaii-deathcore-wametat用に別々のアニメーション実装は作らない）。

各パラメータの意味:
    beat_min_gap_sec        : beats.detect_beats() の min_gap_sec に渡す
    beat_strength_percentile: beats.detect_beats() の strength_percentile に渡す
    pulse_scale             : ビート直後の瞬間的な拡大率（1.0が等倍）
    pulse_decay_sec         : パルスが元のサイズに戻るまでの減衰時間
    entrance_overshoot      : 歌詞キャプションの登場時、目標スケールをどれだけ
                               超えてから戻すか（弾むような入り方の強さ）
    entrance_duration_sec   : 登場アニメーションの所要時間
    jitter_amplitude_px     : 登場中の位置ジッター（震え）の振幅。0で無効
    stagger_per_char_sec    : 1文字ごとの表示ずらし時間（0で行単位に一括表示）
    caption_color           : 歌詞テキストの色
    caption_stroke_color    : 歌詞テキストの縁取り色
"""

STYLES = {
    "kawaii": {
        "beat_min_gap_sec": 0.13,
        "beat_strength_percentile": 55,
        "pulse_scale": 1.06,
        "pulse_decay_sec": 0.18,
        "entrance_overshoot": 0.18,
        "entrance_duration_sec": 0.28,
        "jitter_amplitude_px": 0,
        "stagger_per_char_sec": 0.02,
        "caption_color": "#FF6FB5",
        "caption_stroke_color": "#FFFFFF",
    },
    "deathcore": {
        "beat_min_gap_sec": 0.22,
        "beat_strength_percentile": 82,
        "pulse_scale": 1.12,
        "pulse_decay_sec": 0.10,
        "entrance_overshoot": 0.05,
        "entrance_duration_sec": 0.12,
        "jitter_amplitude_px": 6,
        "stagger_per_char_sec": 0.0,
        "caption_color": "#E63946",
        "caption_stroke_color": "#0A0A0A",
    },
    "kawaii-deathcore-wametal": {
        # kawaiiの弾む登場 + deathcoreのジッター/パルスの鋭さを合成したデフォルト
        "beat_min_gap_sec": 0.17,
        "beat_strength_percentile": 70,
        "pulse_scale": 1.09,
        "pulse_decay_sec": 0.14,
        "entrance_overshoot": 0.12,
        "entrance_duration_sec": 0.18,
        "jitter_amplitude_px": 3,
        "stagger_per_char_sec": 0.012,
        "caption_color": "#FF3B70",
        "caption_stroke_color": "#1A0010",
    },
}

DEFAULT_STYLE = "kawaii-deathcore-wametal"


def get_style(name):
    if name not in STYLES:
        available = ", ".join(STYLES)
        raise ValueError(f"不明なスタイル: {name}（利用可能: {available}）")
    return STYLES[name]
