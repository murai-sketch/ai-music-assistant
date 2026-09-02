#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
make_lyric_video.py
ERPJ楽曲ノート + 音声 + 画像 から、ビート連動アニメーション歌詞字幕付きの
ショート動画(mp4)を生成するCLI。

--------------------------------------------------------------------------
使い方:

    python3 scripts/lyric_video/make_lyric_video.py \\
      --song "01_Songs/空気で有罪.md" \\
      --audio /path/to/song.mp3 \\
      --image /path/to/cover.jpg \\
      --style kawaii-deathcore-wametal \\
      --out scripts/lyric_video/_work/空気で有罪/output.mp4

    --style は kawaii / deathcore / kawaii-deathcore-wametal から選択
    （省略時は kawaii-deathcore-wametal）。

align.py/beats.py の結果は音声ファイルのハッシュでキャッシュされる
（scripts/lyric_video/_work/<hash>/ 配下）ため、スタイルだけ変えて
再生成する場合はwhisper文字起こし・ビート検出をスキップできる。

必要環境:
    scripts/lyric_video/requirements.txt を参照。専用venvの利用を推奨
    （MoneyPrinterTurbo等、他プロジェクトのvenvとは独立させること）。
--------------------------------------------------------------------------
"""

import argparse
import sys
from pathlib import Path

from align import align_lyrics
from beats import detect_beats
from render import render_video
from song_note import SongNote
from styles import DEFAULT_STYLE, STYLES, get_style


def parse_args():
    parser = argparse.ArgumentParser(
        description="歌詞動画（ビート連動アニメーション字幕）を生成する"
    )
    parser.add_argument("--song", required=True, help="01_Songs/<曲名>.md のパス")
    parser.add_argument("--audio", required=True, help="歌唱音声ファイルのパス")
    parser.add_argument("--image", required=True, help="背景に使う静止画のパス")
    parser.add_argument(
        "--style", default=DEFAULT_STYLE,
        choices=list(STYLES),
        help=f"アニメーションスタイル（デフォルト: {DEFAULT_STYLE}）",
    )
    parser.add_argument("--out", required=True, help="出力mp4のパス")
    parser.add_argument(
        "--no-cache", action="store_true",
        help="align/beatsのキャッシュを無視して再計算する",
    )
    return parser.parse_args()


def main():
    args = parse_args()

    song_path = Path(args.song)
    audio_path = Path(args.audio)
    image_path = Path(args.image)

    for path, label in [(song_path, "曲ノート"), (audio_path, "音声"), (image_path, "画像")]:
        if not path.exists():
            print(f"[ERROR] {label}が見つかりません: {path}")
            sys.exit(1)

    print(f"[1/4] 曲ノートを読み込み中: {song_path}")
    note = SongNote(song_path)
    print(f"      title={note.title!r} bpm={note.bpm} 歌詞行数={len(note.lyric_lines)}")

    style = get_style(args.style)

    print("[2/4] 歌詞タイミングを推定中（whisper文字起こし、初回は時間がかかります）...")
    alignment = align_lyrics(audio_path, note.lyric_lines, use_cache=not args.no_cache)
    print(f"      {len(alignment)}行のタイミングを取得")

    print("[3/4] ビート/オンセットを検出中...")
    beats = detect_beats(
        audio_path,
        min_gap_sec=style["beat_min_gap_sec"],
        strength_percentile=style["beat_strength_percentile"],
        bpm_hint=note.bpm,
        use_cache=not args.no_cache,
    )
    print(f"      {len(beats)}個のビート/オンセットを検出")

    print(f"[4/4] 動画を書き出し中 (style={args.style})...")
    output_path = render_video(
        image_path=image_path,
        audio_path=audio_path,
        alignment=alignment,
        beats=beats,
        style=style,
        output_path=args.out,
    )

    print(f"[DONE] 出力: {output_path}")


if __name__ == "__main__":
    main()
