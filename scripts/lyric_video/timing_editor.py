#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
timing_editor.py
align.py の自動アライメント結果（_work/<hash>/alignment.json）を、
手動で微調整するためのツール。

歪んだ/デスコア的なボーカルやカワイ声質のミックスなど、whisperが元々
想定していない音源では、自動アライメントの精度に限界がある
（align.pyのコメント参照）。完全自動での精度向上を追い続けるより、
「大まかに自動で合わせた後、人が確認して直す」方が現実的な場合が多いため、
このツールを用意する。

--------------------------------------------------------------------------
使い方:

    1) 現在のアライメントを編集用シートとして書き出す
        python3 scripts/lyric_video/timing_editor.py export \\
          --audio /path/to/song.mp3 --song "01_Songs/曲名.md"

        -> _work/<hash>/timing_sheet.txt が作られる

    2) timing_sheet.txt を開いて start/end の秒数を直接書き換えて保存する
       （書式は 1行1エントリ: "0001 | start=0.00 | end=4.37 | 歌詞テキスト"。
        歌詞テキスト自体は変更しないこと。行の追加・削除もしないこと）

    3) 編集済みシートをalignmentに反映する
        python3 scripts/lyric_video/timing_editor.py apply \\
          --audio /path/to/song.mp3

    4) 「進行するほどズレが大きくなる」ドリフトがある場合（1行ずつ直すのは
       非現実的）は、耳で確認できた正しい時刻を数行分だけ指定するアンカー
       補正が使える。歌唱が歪んでいて聞き取れない箇所は諦めて、聞き取れた
       数点だけ与えれば、その間を文字数比例で再配分し直してくれる:
        python3 scripts/lyric_video/timing_editor.py anchor \\
          --audio /path/to/song.mp3 \\
          --anchors "10=25.0,30=89.5,50=140.2"

       （"行番号=正しい開始秒数" をカンマ区切りで。先頭行・末尾行を含めなければ
        その前後は一番近いアンカーのズレ量でそのままシフトされる）

    5) 「全体的に早い/遅い」のような一定のズレなら、より単純なシフト/スケールも使える:
        python3 scripts/lyric_video/timing_editor.py shift \\
          --audio /path/to/song.mp3 --seconds 0.8
        python3 scripts/lyric_video/timing_editor.py scale \\
          --audio /path/to/song.mp3 --factor 1.03

       shiftは全行のstart/endに秒数を加算（プラスで後ろにずらす）。
       scaleは音声先頭(0秒)を基準に全行のstart/endを係数倍する
       （1.0より大きいと後半ほど後ろに伸びる＝間延びを補正、
        1.0より小さいと後半ほど前に詰まる）。
       どちらも適用後に自動で単調増加・重複なしを再チェックする。

いずれのサブコマンドも、変更後の結果を align.py と同じ
_work/<hash>/alignment.json に書き戻すため、その後は
make_lyric_video.py をキャッシュありのまま再実行すれば
（whisperを再実行せず）修正後のタイミングで動画を再レンダリングできる。
--------------------------------------------------------------------------
"""

import argparse
import json
import sys
from pathlib import Path

from align import _audio_hash, WORK_DIR

SHEET_NAME = "timing_sheet.txt"


def _cache_paths(audio_path):
    audio_hash = _audio_hash(Path(audio_path))
    cache_dir = WORK_DIR / audio_hash
    return cache_dir, cache_dir / "alignment.json", cache_dir / SHEET_NAME


def _load_cache(alignment_path):
    if not alignment_path.exists():
        print(f"[ERROR] {alignment_path} が見つかりません。"
              f"先に make_lyric_video.py または align.py を実行してください。")
        sys.exit(1)
    return json.loads(alignment_path.read_text(encoding="utf-8"))


def cmd_export(args):
    cache_dir, alignment_path, sheet_path = _cache_paths(args.audio)
    cache = _load_cache(alignment_path)
    alignment = cache["alignment"]

    lines = [
        "# タイミング編集シート",
        "# start/end の秒数だけを書き換えてください（歌詞テキスト・行の増減は不可）。",
        "# 書き換えたら: python3 scripts/lyric_video/timing_editor.py apply --audio <このファイルのaudio>",
        "",
    ]
    for i, item in enumerate(alignment, 1):
        lines.append(
            f"{i:04d} | start={item['start']:.2f} | end={item['end']:.2f} | {item['line']}"
        )

    sheet_path.write_text("\n".join(lines), encoding="utf-8")
    print(f"[DONE] 編集用シートを書き出しました: {sheet_path}")
    print("       start/endの数値を直して保存後、`apply`を実行してください。")


def _parse_sheet(sheet_path, expected_count):
    entries = []
    for raw_line in sheet_path.read_text(encoding="utf-8").splitlines():
        raw_line = raw_line.strip()
        if not raw_line or raw_line.startswith("#"):
            continue
        # "0001 | start=0.00 | end=4.37 | 歌詞テキスト"
        parts = raw_line.split("|")
        if len(parts) < 4:
            print(f"[ERROR] 行の書式が壊れています: {raw_line}")
            sys.exit(1)
        try:
            start = float(parts[1].split("=", 1)[1].strip())
            end = float(parts[2].split("=", 1)[1].strip())
        except (IndexError, ValueError):
            print(f"[ERROR] start/endの数値を読み取れません: {raw_line}")
            sys.exit(1)
        line_text = "|".join(parts[3:]).strip()
        entries.append({"line": line_text, "start": start, "end": end})

    if len(entries) != expected_count:
        print(f"[ERROR] 行数が一致しません（シート内: {len(entries)}行 / "
              f"元のアライメント: {expected_count}行）。行の追加・削除はしないでください。")
        sys.exit(1)
    return entries


def _enforce_monotonic(alignment):
    for i in range(1, len(alignment)):
        if alignment[i]["start"] < alignment[i - 1]["end"]:
            alignment[i]["start"] = alignment[i - 1]["end"]
        if alignment[i]["end"] <= alignment[i]["start"]:
            alignment[i]["end"] = alignment[i]["start"] + 0.3
    return alignment


def _save_cache(alignment_path, cache):
    alignment_path.write_text(
        json.dumps(cache, ensure_ascii=False, indent=2), encoding="utf-8"
    )


def cmd_apply(args):
    cache_dir, alignment_path, sheet_path = _cache_paths(args.audio)
    cache = _load_cache(alignment_path)

    if not sheet_path.exists():
        print(f"[ERROR] 編集用シートが見つかりません: {sheet_path}")
        print("        先に `export` を実行してください。")
        sys.exit(1)

    new_alignment = _parse_sheet(sheet_path, len(cache["alignment"]))
    # 歌詞テキスト自体は元のalignmentのものを正とする（シート内テキストは表示用の参考情報）
    for new_item, old_item in zip(new_alignment, cache["alignment"]):
        new_item["line"] = old_item["line"]

    new_alignment = _enforce_monotonic(new_alignment)
    cache["alignment"] = new_alignment
    _save_cache(alignment_path, cache)
    print(f"[DONE] {len(new_alignment)}行のタイミングを反映しました: {alignment_path}")
    print("       make_lyric_video.py をキャッシュありで再実行すると、"
          "whisperを再実行せずに動画へ反映されます。")


def cmd_anchor(args):
    """自動アライメントが「進行するほどズレが大きくなる」ドリフトを起こしている
    場合の補正。1行ずつ手直しするのは非現実的なので、耳で確認できた正しい
    時刻を数行分（アンカー）だけ指定し、アンカーとアンカーの間を文字数比例で
    再配分し直す。アンカーの外側（先頭行より前・末尾行より後）は、一番近い
    アンカーで生じたズレ量(delta)でそのままシフトする。

    「聞き取れないボーカル」を無理に自動認識させようとせず、聞き取れた数点
    だけ人間が与えて残りを補間する、という考え方。
    """
    cache_dir, alignment_path, _ = _cache_paths(args.audio)
    cache = _load_cache(alignment_path)
    alignment = cache["alignment"]
    n = len(alignment)

    anchors = {}
    for pair in args.anchors.split(","):
        pair = pair.strip()
        if not pair:
            continue
        line_str, time_str = pair.split("=")
        line_no = int(line_str.strip())
        time_val = float(time_str.strip())
        if not (1 <= line_no <= n):
            print(f"[ERROR] 行番号が範囲外です: {line_no} (1-{n})")
            sys.exit(1)
        anchors[line_no] = time_val

    if not anchors:
        print("[ERROR] --anchors で最低1つは指定してください（例: 30=89.5）")
        sys.exit(1)

    # 先頭行・末尾行が明示されていなければ、現在の値をそのまま境界として使う
    if 1 not in anchors:
        anchors[1] = alignment[0]["start"]
    if n not in anchors:
        anchors[n] = alignment[n - 1]["start"]

    anchor_lines = sorted(anchors.keys())
    new_starts = [None] * n

    for a, b in zip(anchor_lines, anchor_lines[1:]):
        segment_lines = alignment[a - 1:b]  # a行目〜b行目（1-indexed, 両端含む）
        # b行目自体の時刻はanchors[b]で確定するので、文字数の按分対象は a行目〜b-1行目
        weight_lines = segment_lines[:-1] or segment_lines
        total_chars = sum(max(len(x["line"]), 1) for x in weight_lines) or 1
        t0, t1 = anchors[a], anchors[b]
        cursor = t0
        for i in range(a, b):
            item = alignment[i - 1]
            share = (t1 - t0) * (max(len(item["line"]), 1) / total_chars)
            new_starts[i - 1] = cursor
            cursor += share
        new_starts[b - 1] = t1

    first_anchor, last_anchor = anchor_lines[0], anchor_lines[-1]
    if first_anchor > 1:
        delta = anchors[first_anchor] - alignment[first_anchor - 1]["start"]
        for i in range(0, first_anchor - 1):
            new_starts[i] = alignment[i]["start"] + delta
    if last_anchor < n:
        delta = anchors[last_anchor] - alignment[last_anchor - 1]["start"]
        for i in range(last_anchor, n):
            new_starts[i] = alignment[i]["start"] + delta

    # end は「次の行のstart」に接続する形にする（隙間なく表示が流れる）。
    # 最終行だけは元のdurationを維持する。
    new_alignment = []
    for i, item in enumerate(alignment):
        start = max(new_starts[i], 0.0)
        if i + 1 < n:
            end = new_starts[i + 1]
        else:
            end = start + max(item["end"] - item["start"], 0.5)
        new_alignment.append({"line": item["line"], "start": start, "end": end})

    new_alignment = _enforce_monotonic(new_alignment)
    cache["alignment"] = new_alignment
    _save_cache(alignment_path, cache)
    print(f"[DONE] {len(anchor_lines)}個のアンカー({', '.join(str(x) for x in anchor_lines)}行目)"
          f"を使って全{n}行を再配分しました。")


def cmd_shift(args):
    cache_dir, alignment_path, _ = _cache_paths(args.audio)
    cache = _load_cache(alignment_path)
    for item in cache["alignment"]:
        item["start"] = max(item["start"] + args.seconds, 0.0)
        item["end"] = max(item["end"] + args.seconds, item["start"] + 0.05)
    cache["alignment"] = _enforce_monotonic(cache["alignment"])
    _save_cache(alignment_path, cache)
    print(f"[DONE] 全{len(cache['alignment'])}行を{args.seconds:+.2f}秒シフトしました。")


def cmd_scale(args):
    cache_dir, alignment_path, _ = _cache_paths(args.audio)
    cache = _load_cache(alignment_path)
    for item in cache["alignment"]:
        item["start"] = item["start"] * args.factor
        item["end"] = max(item["end"] * args.factor, item["start"] + 0.05)
    cache["alignment"] = _enforce_monotonic(cache["alignment"])
    _save_cache(alignment_path, cache)
    print(f"[DONE] 全{len(cache['alignment'])}行を{args.factor:.3f}倍にスケールしました。")


def main():
    parser = argparse.ArgumentParser(description="歌詞タイミングの手動微調整ツール")
    sub = parser.add_subparsers(dest="command", required=True)

    p_export = sub.add_parser("export", help="編集用シートを書き出す")
    p_export.add_argument("--audio", required=True)

    p_apply = sub.add_parser("apply", help="編集済みシートを反映する")
    p_apply.add_argument("--audio", required=True)

    p_anchor = sub.add_parser(
        "anchor", help="耳で確認できた数点の正しい時刻を使い、ドリフトを補正しながら再配分する"
    )
    p_anchor.add_argument("--audio", required=True)
    p_anchor.add_argument(
        "--anchors", required=True,
        help="カンマ区切りの '行番号=正しい開始秒数' （例: '10=25.0,30=89.5,50=140.2'）",
    )

    p_shift = sub.add_parser("shift", help="全行を一括で時間シフトする")
    p_shift.add_argument("--audio", required=True)
    p_shift.add_argument("--seconds", type=float, required=True,
                          help="加算する秒数（マイナスで前倒し）")

    p_scale = sub.add_parser("scale", help="全行を一括でスケールする（0秒基準）")
    p_scale.add_argument("--audio", required=True)
    p_scale.add_argument("--factor", type=float, required=True,
                          help="倍率（1.0より大きいと後半が後ろに伸びる）")

    args = parser.parse_args()
    {
        "export": cmd_export,
        "apply": cmd_apply,
        "anchor": cmd_anchor,
        "shift": cmd_shift,
        "scale": cmd_scale,
    }[args.command](args)


if __name__ == "__main__":
    main()
