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
       数点だけ与えれば、その間はwhisperが元々検出していた相対的なペース配分を保ったまま再スケーリングしてくれる:
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

    6) 「〇〇行目あたりだけ全体的に半秒くらい遅れてる」のような、範囲を絞った
       ピンポイント補正には shift-range が使える（他の行には影響しない）:
        python3 scripts/lyric_video/timing_editor.py shift-range \\
          --audio /path/to/song.mp3 --from-line 11 --to-line 14 --seconds -0.5

    7) whisperベースの自動アライメントは、歌唱の発音自体が不安定な音源では
       限界がある。「大まかに合っているなら、音楽のビートに乗せてしまえば
       違和感が減る」という考え方で、各行の開始時刻を最寄りのビート/
       オンセット(beats.py・render.pyのパルス演出と同じ検出結果)に
       スナップできる:
        python3 scripts/lyric_video/timing_editor.py snap-beats \\
          --audio /path/to/song.mp3 --tolerance 0.3

       toleranceより離れたビートしかない行はスナップされない
       （的外れな位置に飛ばされないための安全弁）。

なお、render.py 側にも「次の行までの間隔が開きすぎている場合は、
キャプションを一定時間で自動的に非表示にする」仕組み（styles.pyの
max_hold_sec）が入っているため、shift-range等で意図的に隙間を作っても
そこは自然に非表示になる。

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
    時刻を数行分（アンカー）だけ指定し、アンカーとアンカーの間はwhisperが元々検出
    していた相対的なペース配分（ポーズ・フレーズの疎密など）を保ったまま
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

    # ユーザーが実際に指定した行番号だけを境界として扱う。
    # ここで anchors[1]/anchors[n] を「現在値のまま」補完してしまうと、
    # 後段の「最初/最後のアンカーより外側はシフトする」処理が
    # （常に1行目・n行目が“アンカー済み”になるため）一切発動しなくなる
    # バグになるので、絶対に行わないこと。
    anchor_lines = sorted(anchors.keys())
    new_starts = [None] * n

    for a, b in zip(anchor_lines, anchor_lines[1:]):
        t0, t1 = anchors[a], anchors[b]
        # whisperの内容一致アライメント(align.py)が元々計算していた
        # a行目〜b行目の相対的な間隔（ポーズの長さ・フレーズの疎密など、
        # 文字数だけでは分からない実際の歌唱ペース情報）を捨てずに、
        # [t0, t1] の区間へそのまま比例縮尺する。
        # （旧実装は文字数だけで機械的に均等配分しており、whisperが実際に
        #  検出していた情報を丸ごと無視してしまっていた。これが「アンカーの
        #  間はどんどんズレる」の実質的な原因だった。）
        orig_a, orig_b = alignment[a - 1]["start"], alignment[b - 1]["start"]
        orig_span = orig_b - orig_a
        for i in range(a, b):
            item = alignment[i - 1]
            if orig_span > 0:
                frac = (item["start"] - orig_a) / orig_span
            else:
                frac = (i - a) / max(b - a, 1)
            new_starts[i - 1] = t0 + frac * (t1 - t0)
        new_starts[b - 1] = t1

    # アンカーが1つしか指定されない場合など、上のループが1度も回らず
    # アンカー行自体の値が未設定のままになるケースをここで必ず埋める
    for a in anchor_lines:
        new_starts[a - 1] = anchors[a]

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


def cmd_snap_beats(args):
    """各行のstart時刻を、検出済みのビート/オンセット(beats.json、
    beats.detect_beats()の結果)の中から一番近いものにスナップする。

    whisperベースの自動アライメントには限界があるが、「多少ズレていても、
    音楽のビートに乗っていれば違和感が少ない」という考え方に基づく補正。
    toleranceを超えて離れたビートには吸着しない（全く違う位置に
    誤って飛ばされることへの安全弁）。"""
    import bisect

    cache_dir, alignment_path, _ = _cache_paths(args.audio)
    cache = _load_cache(alignment_path)
    alignment = cache["alignment"]

    beats_path = cache_dir / "beats.json"
    if not beats_path.exists():
        print(f"[ERROR] {beats_path} が見つかりません。"
              f"先に make_lyric_video.py または beats.py を実行してください。")
        sys.exit(1)
    beats_data = json.loads(beats_path.read_text(encoding="utf-8"))
    beats = beats_data.get("beats", [])
    if not beats:
        print("[ERROR] ビートが1つも検出されていません。")
        sys.exit(1)

    snapped = 0
    for item in alignment:
        idx = bisect.bisect_left(beats, item["start"])
        candidates = []
        if idx > 0:
            candidates.append(beats[idx - 1])
        if idx < len(beats):
            candidates.append(beats[idx])
        if not candidates:
            continue
        nearest = min(candidates, key=lambda b: abs(b - item["start"]))
        if abs(nearest - item["start"]) <= args.tolerance:
            shift = nearest - item["start"]
            item["start"] = nearest
            item["end"] = max(item["end"] + shift, item["start"] + 0.05)
            snapped += 1

    cache["alignment"] = _enforce_monotonic(alignment)
    _save_cache(alignment_path, cache)
    print(f"[DONE] {snapped}/{len(alignment)}行を最寄りのビートにスナップしました"
          f"（許容誤差: ±{args.tolerance:.2f}秒。それより遠いビートしかない行はスナップしていません）。")


def cmd_shift(args):
    cache_dir, alignment_path, _ = _cache_paths(args.audio)
    cache = _load_cache(alignment_path)
    for item in cache["alignment"]:
        item["start"] = max(item["start"] + args.seconds, 0.0)
        item["end"] = max(item["end"] + args.seconds, item["start"] + 0.05)
    cache["alignment"] = _enforce_monotonic(cache["alignment"])
    _save_cache(alignment_path, cache)
    print(f"[DONE] 全{len(cache['alignment'])}行を{args.seconds:+.2f}秒シフトしました。")


def cmd_shift_range(args):
    """指定した行番号の範囲（両端含む、1-indexed）だけを一括シフトする。
    「〇〇行目あたりが全体的に半秒くらい遅れてる」のような、範囲を絞った
    耳での指摘を反映するための、shift/anchorより手軽なピンポイント補正。"""
    cache_dir, alignment_path, _ = _cache_paths(args.audio)
    cache = _load_cache(alignment_path)
    alignment = cache["alignment"]
    n = len(alignment)

    if not (1 <= args.from_line <= args.to_line <= n):
        print(f"[ERROR] 範囲が不正です: {args.from_line}-{args.to_line} (1-{n})")
        sys.exit(1)

    # 注意: ここでは全体に対する_enforce_monotonic()は使わない。
    # 範囲の直前の行(from_line-1)を基準に「start >= 直前行のend」を"強制的に
    # 引き上げる"と、現状は歌詞行が隙間なく連続配置されていることが多いため、
    # マイナス方向のシフトが直前行との衝突で丸ごと打ち消され、範囲内の行だけが
    # 歪んでズレるカスケード的な破綻が起きる。
    # render.py側で「長い間隔は自動で非表示になる」ようにした（max_hold_sec）
    # ため、範囲の前後にちょっとした隙間ができても問題ない。
    # ただし逆に「直前/直後の行とキャプションが同時表示されて文字が重なる」
    # のは見た目上の破綻なので、範囲外の行の方を短く切り詰めて避ける
    # （範囲外の行の"開始"時刻は変更しない。表示が終わる時刻だけ詰める）。
    for item in alignment[args.from_line - 1: args.to_line]:
        item["start"] = max(item["start"] + args.seconds, 0.0)
        item["end"] = max(item["end"] + args.seconds, item["start"] + 0.05)

    for i in range(args.from_line, args.to_line):
        prev, cur = alignment[i - 1], alignment[i]
        if cur["start"] < prev["end"]:
            cur["start"] = prev["end"]
        if cur["end"] <= cur["start"]:
            cur["end"] = cur["start"] + 0.3

    range_start_idx = args.from_line - 1
    if range_start_idx > 0:
        prev_item = alignment[range_start_idx - 1]
        first_item = alignment[range_start_idx]
        if prev_item["end"] > first_item["start"]:
            prev_item["end"] = max(first_item["start"], prev_item["start"] + 0.1)

    range_end_idx = args.to_line - 1
    if range_end_idx < n - 1:
        last_item = alignment[range_end_idx]
        next_item = alignment[range_end_idx + 1]
        if last_item["end"] > next_item["start"]:
            last_item["end"] = max(next_item["start"], last_item["start"] + 0.1)

    _save_cache(alignment_path, cache)
    print(f"[DONE] {args.from_line}〜{args.to_line}行目を{args.seconds:+.2f}秒シフトしました"
          "（範囲外の行との間に小さな隙間/重なりができても、"
          "render.py側で自動的に扱われます）。")


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

    p_shift_range = sub.add_parser(
        "shift-range", help="指定した行番号の範囲だけを一括で時間シフトする"
    )
    p_shift_range.add_argument("--audio", required=True)
    p_shift_range.add_argument("--from-line", type=int, required=True, dest="from_line")
    p_shift_range.add_argument("--to-line", type=int, required=True, dest="to_line")
    p_shift_range.add_argument("--seconds", type=float, required=True,
                                help="加算する秒数（マイナスで前倒し）")

    p_scale = sub.add_parser("scale", help="全行を一括でスケールする（0秒基準）")
    p_scale.add_argument("--audio", required=True)
    p_scale.add_argument("--factor", type=float, required=True,
                          help="倍率（1.0より大きいと後半が後ろに伸びる）")

    p_snap = sub.add_parser(
        "snap-beats", help="各行の開始時刻を最寄りのビート/オンセットにスナップする"
    )
    p_snap.add_argument("--audio", required=True)
    p_snap.add_argument("--tolerance", type=float, default=0.3,
                         help="スナップを許容する最大誤差（秒）。デフォルト0.3")

    args = parser.parse_args()
    {
        "export": cmd_export,
        "apply": cmd_apply,
        "anchor": cmd_anchor,
        "shift": cmd_shift,
        "shift-range": cmd_shift_range,
        "scale": cmd_scale,
        "snap-beats": cmd_snap_beats,
    }[args.command](args)


if __name__ == "__main__":
    main()
