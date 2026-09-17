#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
render.py
背景画像のビート連動パルス + アニメーション歌詞キャプションを、単一の
moviepy CompositeVideoClip パスで合成する。

別ffmpegパス(zoompan等)には分割しない。理由はプラン参照:
  - 参照元 produce_short.sh の「2パス目」も実際は静的タイトルの後乗せで、
    動的な字幕合成自体はMoneyPrinterTurbo内で常に単一moviepyパスで行われている。
  - zoompanは参照元リポジトリに前例がなく、moviepy 2.xの
    `.resized(lambda t: ...)` の方がこの環境で動作実績があり単純
    （video.py 1375-1381行目のズームイディオムを踏襲）。

moviepy 2.x系のAPI（.resized/.with_position/.with_start/.with_duration）を
使用する。1.x系チュートリアルのAPI（.resize/.set_position等）とは互換性が
ないので注意。
"""

import bisect
import math
from pathlib import Path

try:
    from moviepy import (
        ImageClip, AudioFileClip, TextClip, CompositeVideoClip,
    )
except ImportError:
    ImageClip = AudioFileClip = TextClip = CompositeVideoClip = None

VIDEO_SIZE = (1080, 1920)  # 9:16 SNSショート動画標準
FPS = 30

# macOSに標準搭載されている極太ゴシック体。ShortsVaultパイプラインでも
# 「映画タイトル風」の太さとして採用実績がある。
DEFAULT_FONT = "/System/Library/Fonts/ヒラギノ角ゴシック W9.ttc"


def _check_moviepy():
    if ImageClip is None:
        raise RuntimeError(
            "moviepy がインストールされていません。"
            "scripts/lyric_video/requirements.txt を pip install してください。"
        )


def _pulse_scale_at(t, beats, pulse_scale, pulse_decay_sec):
    """時刻tにおける背景画像の拡大率を返す。直前のビートからの経過時間で
    指数減衰するパルス。"""
    if not beats:
        return 1.0
    idx = bisect.bisect_right(beats, t) - 1
    if idx < 0:
        return 1.0
    dt = t - beats[idx]
    if dt < 0 or dt > pulse_decay_sec * 4:
        return 1.0
    decay = math.exp(-dt / pulse_decay_sec)
    return 1.0 + (pulse_scale - 1.0) * decay


def _entrance_scale_at(t, duration, overshoot):
    """歌詞キャプションの登場スケール。0.5から1+overshootまで膨らみ、
    durationの終わりでちょうど1.0に収束する単純なポップイン曲線。"""
    if duration <= 0 or t >= duration:
        return 1.0
    p = t / duration
    base = 0.5 + 0.5 * p
    bump = overshoot * math.sin(p * math.pi)
    return base + bump


def _jitter_at(t, amplitude_px):
    """継続的な位置ジッター（deathcoreのグリッチ/シェイク表現用）。
    外部乱数状態を持たず、時刻から決定的に計算する。"""
    if amplitude_px <= 0:
        return 0, 0
    dx = amplitude_px * math.sin(t * 47.0)
    dy = amplitude_px * math.cos(t * 61.0)
    return dx, dy


def _build_caption_clip(line, start, end, style, font_path, video_size):
    raw_duration = max(end - start, 0.05)
    # align.py/timing_editor.pyのendは「次の行が始まる時刻」であることが多く、
    # そのまま使うと次の歌詞までインスト等で間隔が空いたときに、キャプションが
    # 消えずに画面に残り続けてしまう。max_hold_secを超える分は打ち切り、
    # 次の行が来るまでいったん非表示にする。
    duration = min(raw_duration, style["max_hold_sec"])
    # 文字ごとの個別クリップ(真の1文字ずつの登場アニメーション)は生成コストが
    # 高すぎるため採用しない。代わりに、行の長さに比例してentrance全体の所要
    # 時間を伸ばすことで「長い行ほどゆっくり登場する」カスケード感を近似する。
    stagger_bonus = style["stagger_per_char_sec"] * len(line)
    entrance_duration = min(style["entrance_duration_sec"] + stagger_bonus, duration)
    overshoot = style["entrance_overshoot"]
    jitter_amp = style["jitter_amplitude_px"]

    text_clip = TextClip(
        font=font_path,
        text=line,
        font_size=72,
        color=style["caption_color"],
        stroke_color=style["caption_stroke_color"],
        stroke_width=3,
        method="caption",
        size=(int(video_size[0] * 0.85), None),
        text_align="center",
    ).with_duration(duration)

    # 中央揃えの基準サイズは、リサイズ前のこの時点で固定値として確定させる。
    # moviepy 2.xでは、時間変化するresized()を適用した後の.w/.hはt=0時点の
    # サイズに固定されてしまう（フレームごとの実サイズには追従しない）ため、
    # resized()後のtext_clip.wをposition_fn内で参照すると、entranceアニメーション
    # 中の一時的なスケール値のまま中央揃え計算が固定され、通常表示時
    # （スケール1.0）に大きく位置がズレる（画面外にはみ出す）バグになる。
    orig_w, orig_h = text_clip.w, text_clip.h

    text_clip = text_clip.resized(
        lambda t: _entrance_scale_at(t, entrance_duration, overshoot)
    )

    cx = video_size[0] / 2
    cy = video_size[1] * 0.72  # 中央よりやや下（センター字幕の定番位置）

    if jitter_amp > 0:
        def position_fn(t):
            dx, dy = _jitter_at(t, jitter_amp)
            return (cx + dx - orig_w / 2, cy + dy - orig_h / 2)
    else:
        def position_fn(t):
            return (cx - orig_w / 2, cy - orig_h / 2)

    text_clip = text_clip.with_position(position_fn).with_start(start)
    return text_clip


def render_video(
    image_path,
    audio_path,
    alignment,
    beats,
    style,
    output_path,
    font_path=None,
):
    """
    Args:
        image_path: 背景静止画のパス
        audio_path: 音声ファイルのパス
        alignment: align.align_lyrics() の戻り値（[{line, start, end}, ...]）
        beats: beats.detect_beats() の戻り値（時刻の配列）
        style: styles.get_style() の戻り値
        output_path: 出力mp4のパス
        font_path: 使用フォント（省略時はDEFAULT_FONT）
    """
    _check_moviepy()
    font_path = font_path or DEFAULT_FONT

    audio_clip = AudioFileClip(str(audio_path))
    duration = audio_clip.duration

    base_clip = (
        ImageClip(str(image_path))
        .with_duration(duration)
        .resized(height=VIDEO_SIZE[1])
        .with_position("center")
    )

    pulse_scale = style["pulse_scale"]
    pulse_decay_sec = style["pulse_decay_sec"]
    base_clip = base_clip.resized(
        lambda t: _pulse_scale_at(t, beats, pulse_scale, pulse_decay_sec)
    )

    caption_clips = [
        _build_caption_clip(item["line"], item["start"], item["end"], style, font_path, VIDEO_SIZE)
        for item in alignment
    ]

    final = CompositeVideoClip(
        [base_clip, *caption_clips], size=VIDEO_SIZE
    ).with_duration(duration).with_audio(audio_clip)

    output_path = Path(output_path)
    output_path.parent.mkdir(parents=True, exist_ok=True)
    final.write_videofile(
        str(output_path), fps=FPS, codec="libx264", audio_codec="aac", logger=None
    )
    return output_path
