#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
master_audio.py
オフラインAIマスタリング機能。murai-sketch/lyricflow の「マスタリング」ページ
(Web Audio APIの7エフェクトチェーン)を、ffmpegの-afフィルタグラフとして
オフラインで再現するCLIツール。

Stripe/クレジット制の機能ロックはlyricflow側のマネタイズ都合のため、
このツールでは持ち込まない。7エフェクトすべてを無条件で使える。

--------------------------------------------------------------------------
使い方:

    python3 scripts/mastering/master_audio.py \\
      --in song.wav \\
      --out song_mastered.wav \\
      --effects loudness,eq,stereo,saturation

    利用可能なエフェクト名: loudness, radio, eq, comp, stereo, saturation, deesser
    (--effectsの指定順は無視され、常に元実装と同じ固定順で適用される)

必要環境:
    - ffmpeg (このマシンでは /opt/homebrew/bin/ffmpeg で確認済み)
    - 追加のPython依存なし
--------------------------------------------------------------------------
"""

import argparse
import shutil
import subprocess
import sys
from pathlib import Path

from effects import EFFECT_ORDER, build_filter_graph


def check_ffmpeg():
    if shutil.which("ffmpeg") is None:
        print("[ERROR] ffmpegが見つかりません。インストールしてから再実行してください。")
        sys.exit(1)


def parse_args():
    parser = argparse.ArgumentParser(
        description="ffmpegベースのオフラインマスタリングツール"
    )
    parser.add_argument("--in", dest="input_path", required=True, help="入力音声ファイル")
    parser.add_argument("--out", dest="output_path", required=True, help="出力音声ファイル")
    parser.add_argument(
        "--effects",
        required=True,
        help=f"カンマ区切りのエフェクト名。利用可能: {', '.join(EFFECT_ORDER)}",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="ffmpegを実行せず、組み立てたコマンドだけ表示する",
    )
    return parser.parse_args()


def main():
    check_ffmpeg()
    args = parse_args()

    input_path = Path(args.input_path)
    if not input_path.exists():
        print(f"[ERROR] 入力ファイルが見つかりません: {input_path}")
        sys.exit(1)

    effect_names = [e.strip() for e in args.effects.split(",") if e.strip()]
    try:
        filter_graph, applied_order = build_filter_graph(effect_names)
    except ValueError as e:
        print(f"[ERROR] {e}")
        sys.exit(1)

    output_path = Path(args.output_path)
    output_path.parent.mkdir(parents=True, exist_ok=True)

    cmd = [
        "ffmpeg", "-y",
        "-i", str(input_path),
        "-af", filter_graph,
        str(output_path),
    ]

    print(f"[INFO] 適用エフェクト（順序固定）: {' -> '.join(applied_order)}")
    print(f"[INFO] filter graph: {filter_graph}")
    print(f"[INFO] コマンド: {' '.join(cmd)}")

    if args.dry_run:
        print("[INFO] --dry-run のため実行はスキップしました。")
        return

    result = subprocess.run(cmd, capture_output=True, text=True)
    if result.returncode != 0:
        print("[ERROR] ffmpegの実行に失敗しました:")
        print(result.stderr)
        sys.exit(1)

    print(f"[DONE] 出力: {output_path}")


if __name__ == "__main__":
    main()
