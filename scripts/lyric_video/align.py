#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
align.py
既知の歌詞行を、実際の歌唱音声に対して「大まかに」時刻合わせする。

/Users/armada/YAMADA/MoneyPrinterTurbo/app/services/subtitle.py の
correct() 関数（whisperの生セグメントを既知の台本テキストに再アライメント
するロジック）から着想を得た、プロポーショナル分割のみの簡易版。

歌唱音声はナレーションと違い、whisperの認識テキスト自体はあまり信用できない
（メリスマ・音楽の被り等）前提に立ち、whisperの文字起こし結果は
「セグメントの開始/終了時刻」だけ使い、テキスト内容の一致度は見ない。
これにより、内容一致検索(Levenshteinウィンドウ探索)を持ち込まずに済み、
実装・デバッグの複雑さを抑えている。

将来、実曲でのレビューにより「サビの繰り返しでズレが目立つ」等の具体的な
劣化が見えた場合は、correct()の類似度探索ロジックを追加移植する前提で
設計してある（本ファイルの分割ロジックだけを差し替えれば足りる）。

キャッシュ: 音声ファイルのSHA256先頭8桁をキーに、_work/<hash>/alignment.json
に結果を保存する。既に存在すればwhisper文字起こしをスキップする。
"""

import hashlib
import json
from pathlib import Path

try:
    from faster_whisper import WhisperModel
except ImportError:
    WhisperModel = None

WORK_DIR = Path(__file__).resolve().parent / "_work"

_model = None


def _audio_hash(audio_path):
    h = hashlib.sha256()
    with open(audio_path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()[:8]


def _get_model(model_size="large-v3", device="cpu", compute_type="int8"):
    global _model
    if WhisperModel is None:
        raise RuntimeError(
            "faster_whisper がインストールされていません。"
            "scripts/lyric_video/requirements.txt を pip install してください。"
        )
    if _model is None:
        _model = WhisperModel(
            model_size_or_path=model_size, device=device, compute_type=compute_type
        )
    return _model


def _transcribe_segments(audio_path):
    """whisperで文字起こしし、[(start, end), ...] のセグメント時刻リストを返す。
    テキスト内容は使わず、VADによる区間検出の結果としてのみ利用する。"""
    model = _get_model()
    segments, _info = model.transcribe(
        str(audio_path),
        beam_size=5,
        vad_filter=True,
        vad_parameters=dict(min_silence_duration_ms=200),
    )
    return [(seg.start, seg.end) for seg in segments]


def _get_audio_duration(audio_path):
    # ffprobeに依存せず、whisperのデコード結果から取れる最終セグメント終了時刻を
    # フォールバックに使う。より正確な長さが欲しい場合は呼び出し側でffprobe等を使う。
    import subprocess

    try:
        result = subprocess.run(
            [
                "ffprobe", "-v", "error",
                "-show_entries", "format=duration",
                "-of", "default=noprint_wrappers=1:nokey=1",
                str(audio_path),
            ],
            capture_output=True, text=True, check=True,
        )
        return float(result.stdout.strip())
    except Exception:
        return None


def _proportional_align(lyric_lines, whisper_segments, audio_duration):
    """whisperセグメントの区間だけを使い、歌詞行を文字数比例で配置する。

    考え方:
      - whisperセグメントを「発話（歌唱）が起きている区間」の目安として使う。
      - 各区間内で、対応する歌詞行を文字数の比率で分配する。
      - セグメント数が歌詞行数よりずっと少ない場合は、区間をまとめて割り当てる。
      - セグメントが1つも取れない場合は、曲全体を文字数比例で単純分配する
        （whisperが完全に無音判定した曲などのフォールバック）。
    """
    total_chars = sum(max(len(line), 1) for line in lyric_lines) or 1

    if not whisper_segments:
        if not audio_duration:
            raise ValueError(
                "whisperセグメントが1つも取れず、audio_durationも不明なため、"
                "タイミングを推定できません。"
            )
        cursor = 0.0
        result = []
        for line in lyric_lines:
            share = audio_duration * (max(len(line), 1) / total_chars)
            end = min(cursor + share, audio_duration)
            result.append({"line": line, "start": cursor, "end": end})
            cursor = end
        return result

    # whisperセグメントを歌詞行数に合わせてグループ化する。
    # セグメント数 >= 行数ならグループは1セグメントずつ、
    # セグメント数 < 行数なら、複数行を同じセグメントの時間内で文字数比例分配する。
    n_lines = len(lyric_lines)
    n_segs = len(whisper_segments)

    result = []
    if n_segs >= n_lines:
        # 各行に1つ以上のセグメントを対応させる（余ったセグメントは最後の行に吸収）
        seg_idx = 0
        segs_per_line = n_segs // n_lines
        extra = n_segs % n_lines
        for i, line in enumerate(lyric_lines):
            take = segs_per_line + (1 if i < extra else 0)
            take = max(take, 1)
            group = whisper_segments[seg_idx: seg_idx + take]
            seg_idx += take
            if not group:
                # 起こらないはずだが、念のためのフォールバック
                start = result[-1]["end"] if result else 0.0
                end = start + 0.3
            else:
                start = group[0][0]
                end = group[-1][1]
            result.append({"line": line, "start": start, "end": end})
    else:
        # 行数の方が多い: セグメントを歌詞行にまとめて割り当て、
        # 各セグメント区間内を文字数比例でさらに分割する。
        lines_per_seg = n_lines / n_segs
        line_idx = 0
        for seg_i, (seg_start, seg_end) in enumerate(whisper_segments):
            remaining_lines = n_lines - line_idx
            remaining_segs = n_segs - seg_i
            take = round(remaining_lines / remaining_segs) if remaining_segs else remaining_lines
            take = max(take, 1)
            group_lines = lyric_lines[line_idx: line_idx + take]
            line_idx += take
            if not group_lines:
                continue
            seg_total_chars = sum(max(len(l), 1) for l in group_lines) or 1
            cursor = seg_start
            for line in group_lines:
                share = (seg_end - seg_start) * (max(len(line), 1) / seg_total_chars)
                end = min(cursor + share, seg_end)
                result.append({"line": line, "start": cursor, "end": end})
                cursor = end

        # 万一取りこぼした行があれば、末尾に残り時間で追加する
        if line_idx < n_lines and audio_duration:
            remaining = lyric_lines[line_idx:]
            cursor = result[-1]["end"] if result else 0.0
            available = max(audio_duration - cursor, 0.1)
            rem_chars = sum(max(len(l), 1) for l in remaining) or 1
            for line in remaining:
                share = available * (max(len(line), 1) / rem_chars)
                end = min(cursor + share, audio_duration)
                result.append({"line": line, "start": cursor, "end": end})
                cursor = end

    # 単調増加・重複なしを保証する後処理
    for i in range(1, len(result)):
        if result[i]["start"] < result[i - 1]["end"]:
            result[i]["start"] = result[i - 1]["end"]
        if result[i]["end"] <= result[i]["start"]:
            result[i]["end"] = result[i]["start"] + 0.3

    return result


def align_lyrics(audio_path, lyric_lines, use_cache=True):
    """歌唱音声 + 歌詞行リスト から、行ごとの{line, start, end}リストを返す。
    結果は音声ファイルのハッシュでキャッシュされる。"""
    audio_path = Path(audio_path)
    audio_hash = _audio_hash(audio_path)
    cache_dir = WORK_DIR / audio_hash
    cache_path = cache_dir / "alignment.json"

    if use_cache and cache_path.exists():
        cached = json.loads(cache_path.read_text(encoding="utf-8"))
        if cached.get("lyric_lines") == lyric_lines:
            return cached["alignment"]

    audio_duration = _get_audio_duration(audio_path)
    whisper_segments = _transcribe_segments(audio_path)
    alignment = _proportional_align(lyric_lines, whisper_segments, audio_duration)

    cache_dir.mkdir(parents=True, exist_ok=True)
    cache_path.write_text(
        json.dumps(
            {"lyric_lines": lyric_lines, "alignment": alignment},
            ensure_ascii=False, indent=2,
        ),
        encoding="utf-8",
    )
    return alignment


if __name__ == "__main__":
    import sys

    if len(sys.argv) != 3:
        print("使い方: python3 align.py <audio> <01_Songs/曲名.md>")
        sys.exit(1)

    from song_note import SongNote

    note = SongNote(sys.argv[2])
    result = align_lyrics(sys.argv[1], note.lyric_lines)
    for item in result:
        print(f"[{item['start']:6.2f} -> {item['end']:6.2f}] {item['line']}")
