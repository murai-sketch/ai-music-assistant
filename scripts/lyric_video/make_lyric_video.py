#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
make_lyric_video.py
楽曲ノート + 音声 + 画像 から、ビート連動アニメーション歌詞字幕付きの
ショート動画(mp4)を生成するCLI。

--------------------------------------------------------------------------
使い方:

    python3 scripts/lyric_video/make_lyric_video.py \\
      --song "01_Songs/<曲名>.md" \\
      --audio /path/to/song.mp3 \\
      --image /path/to/cover.jpg \\
      --style kawaii-deathcore-wametal \\
      --out scripts/lyric_video/_work/<曲名>/output.mp4

    --style は kawaii / deathcore / kawaii-deathcore-wametal から選択
    （省略時は kawaii-deathcore-wametal）。

    --renderer は kinetic（既定。キネティックタイポグラフィ、kinetic.py）/
    classic（中央下の字幕ポップイン、render.py）。
    kinetic では書き出す前に --stills <DIR> で各カットの静止画一覧を出し、
    文字の重なり・見切れ・読みにくさを確認してから --out で書き出す。
    カット設計は _work/<hash>/kinetic_plan.json（手で直せる。--replan で作り直し）。

    ショート動画（TikTok / YouTube ショート）は --shorts で候補を確認し、
    --short で書き出す（出力はいつもの縦1080x1920のまま、区間だけを切る）:

      python3 scripts/lyric_video/make_lyric_video.py ... --shorts
      python3 scripts/lyric_video/make_lyric_video.py ... --short 1
      python3 scripts/lyric_video/make_lyric_video.py ... --short all --short-sec 15

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

from align import WORK_DIR, _audio_hash, _get_audio_duration, align_lyrics
from beats import detect_beats
from render import render_video
import shorts
from song_note import SongNote, sections_for_alignment
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
    parser.add_argument("--out", help="出力mp4のパス（--stills のときは不要）")
    parser.add_argument(
        "--no-cache", action="store_true",
        help="align/beatsのキャッシュを無視して再計算する",
    )
    parser.add_argument(
        "--renderer", default="kinetic", choices=["kinetic", "classic"],
        help="kinetic: 文字が動いて意味を伝えるキネティックタイポグラフィ（kinetic.py） / "
             "classic: 中央下の字幕がポップインする旧方式（render.py）",
    )
    parser.add_argument(
        "--replan", action="store_true",
        help="kinetic: _work/<hash>/kinetic_plan.json を作り直す（手で直した設計は消える）",
    )
    parser.add_argument(
        "--bg", action="append", metavar="FILE:USE",
        help="kinetic: 背景素材（画像・動画）を用途つきで登録する。複数指定可。"
             "USE = quiet / verse / hook / interlude / any。"
             "指定すると _work/<hash>/backgrounds.json を置き換える（省略時はそのファイルを使う）。"
             "素材が無いときは、生成して用意する方法を書き出しの前に案内する",
    )
    parser.add_argument(
        "--shorts", action="store_true",
        help="kinetic: ショート動画（TikTok / YouTube ショート）の切り抜き候補を出して終わる",
    )
    parser.add_argument(
        "--short", metavar="SPEC",
        help="kinetic: 切り抜き候補を書き出す。SPEC は順位（1）、複数（1,3）、all（上位すべて）",
    )
    parser.add_argument(
        "--short-sec", type=float, default=shorts.DEFAULT_TARGET,
        help=f"ショートの長さの目安・秒（デフォルト: {shorts.DEFAULT_TARGET:.0f}）",
    )
    parser.add_argument(
        "--short-range", default=f"{shorts.SHORT_MIN:.0f}:{shorts.SHORT_MAX:.0f}",
        help=f"ショートの下限:上限・秒（デフォルト: {shorts.SHORT_MIN:.0f}:{shorts.SHORT_MAX:.0f}）",
    )
    parser.add_argument(
        "--short-count", type=int, default=3,
        help="候補を何本まで出すか（デフォルト: 3）",
    )
    parser.add_argument(
        "--stills", metavar="DIR",
        help="kinetic: 動画は書き出さず、各カットの静止画一覧をDIRに出す（書き出し前の確認用）",
    )
    return parser.parse_args()


def _pick_shorts(spec, candidates):
    """--short の指定（1 / 1,3 / all）を候補の並びから選ぶ。"""
    if spec.strip().lower() == "all":
        return candidates
    picked = []
    for part in spec.split(","):
        part = part.strip()
        if not part.isdigit():
            continue
        match = [c for c in candidates if c["rank"] == int(part)]
        picked.extend(match)
    return picked


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

    if args.renderer == "classic":
        if not args.out:
            print("[ERROR] --out を指定してください")
            sys.exit(1)
        print(f"[4/4] 動画を書き出し中 (renderer=classic, style={args.style})...")
        output_path = render_video(
            image_path=image_path,
            audio_path=audio_path,
            alignment=alignment,
            beats=beats,
            style=style,
            output_path=args.out,
        )
    else:
        from kinetic import load_or_build_plan, render_kinetic, render_stills
        from kinetic_bg import GENERATE_HINT, load_backgrounds, save_backgrounds

        cache_dir = WORK_DIR / _audio_hash(audio_path)
        plan_path = cache_dir / "kinetic_plan.json"
        if args.bg:
            items = []
            for spec in args.bg:
                file, _, use = spec.rpartition(":") if ":" in spec else (spec, "", "any")
                items.append({"file": str(Path(file).resolve()), "use": use or "any"})
            save_backgrounds(cache_dir, items)
        backgrounds = load_backgrounds(cache_dir)
        if backgrounds:
            print(f"      背景素材: {len(backgrounds)}件（{cache_dir / 'backgrounds.json'}）")
        else:
            print(GENERATE_HINT)
        sections = sections_for_alignment(alignment, note.lyric_sections)
        plan = load_or_build_plan(plan_path, alignment, sections, beats, style,
                                  replan=args.replan, meta=note.meta, backgrounds=backgrounds)
        print(f"      カット設計: {plan_path}（一覧は {plan_path.with_suffix('.md').name}）")

        if args.shorts or args.short:
            lo, _, hi = args.short_range.partition(":")
            candidates = shorts.find_shorts(
                alignment, sections, beats, duration=_get_audio_duration(audio_path),
                target=args.short_sec, min_sec=float(lo), max_sec=float(hi or shorts.SHORT_MAX),
                limit=max(args.short_count, 1), max_hold=style.get("max_hold_sec", 2.8),
            )
            print(f"[4/4] ショート候補（目安 {args.short_sec:.0f}秒 / {lo}〜{hi}秒）:")
            print(shorts.format_candidates(candidates))
            if not args.short:
                return
            picks = _pick_shorts(args.short, candidates)
            if not picks:
                print(f"[ERROR] --short {args.short} に当たる候補がありません")
                sys.exit(1)
            for cand in picks:
                out = (Path(args.out) if args.out and len(picks) == 1
                       else cache_dir / shorts.output_name(cand))
                print(f"      [{cand['rank']}] {cand['start']:.2f}〜{cand['end']:.2f}秒 を書き出し中...")
                render_kinetic(image_path, audio_path, plan, beats, style, out,
                               t_start=cand["start"], t_end=cand["end"], backgrounds=backgrounds)
                print(f"[DONE] 出力: {out}")
            return

        if args.stills:
            print(f"[4/4] 静止画一覧を書き出し中: {args.stills}")
            sheets = render_stills(image_path, plan, beats, style, args.stills, backgrounds=backgrounds)
            print(f"[DONE] {len(sheets)}枚: {args.stills}")
            return
        if not args.out:
            print("[ERROR] --out を指定してください")
            sys.exit(1)
        print(f"[4/4] 動画を書き出し中 (renderer=kinetic, style={args.style})...")
        output_path = render_kinetic(image_path, audio_path, plan, beats, style, args.out,
                                     backgrounds=backgrounds)

    print(f"[DONE] 出力: {output_path}")


if __name__ == "__main__":
    main()
