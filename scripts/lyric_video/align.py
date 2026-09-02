#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
align.py
既知の歌詞行を、実際の歌唱音声に対して時刻合わせする。

/Users/armada/YAMADA/MoneyPrinterTurbo/app/services/subtitle.py の
correct() 関数（whisperの生セグメントを既知の台本テキストに再アライメント
するロジック）を参考にした、内容一致度ベースのアライメント。

--- 経緯（重要）---
当初は「whisperの認識テキストは歌唱音声では信用できない」という想定で、
セグメントの開始/終了時刻だけを使い、歌詞行をセグメント数に機械的に
按分するだけの簡易版にしていた。

しかし実曲（「空気で有罪 - カワイ民謡デスコアMIX」）でテストしたところ、
2つの問題が判明した:
  1. vad_filter=True だとVADが歪んだ/デスコア的なボーカルを「音声」と
     認識できず、セグメントが0件になるケースがあった
     （vad_filter=Falseに切り替えると94セグメント検出でき、言語判定も
     日本語0.99の確度、認識テキストも歌詞にかなり近かった）。
  2. whisperの1セグメントが印刷上の歌詞複数行にまたがる
     （例: 1つのセグメントが歌詞の1-2行目に相当する）ため、
     「セグメント数を行数に機械的に按分する」やり方は、セグメントの
     実際の区切りと歌詞の行境界がズレるたびに、そこから後ろの行が
     まとめて時刻ズレを起こす構造的な欠陥があった。

そのため、認識テキストを実際に使う内容一致度ベースの探索
（correct()と同じ発想）に作り直した。whisperの認識精度が完全でなくても、
「セグメントの開始/終了時刻はおおむね合っている」という前提のもと、
歌詞行ごとに一番近い位置を探して割り当てる。

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
    """whisperで文字起こしし、[(start, end, text), ...] のリストを返す。

    まずVAD(vad_filter=True)で試す。歪んだ/デスコア的な発声・ミックスに
    埋もれたボーカルなど、通常の音声と大きく異なる音源ではVADが
    「音声区間なし」と誤判定し、セグメントが0件になることがある
    （実測: 「空気で有罪 - カワイ民謡デスコアMIX」でvad_filter=Trueだと
    0件、vad_filter=Falseだと94件検出）。0件だった場合はvad_filter=False
    で再試行し、それでも0件なら呼び出し元が明示的に警告を出す。"""
    model = _get_model()

    segments, _info = model.transcribe(
        str(audio_path),
        beam_size=5,
        vad_filter=True,
        vad_parameters=dict(min_silence_duration_ms=200),
    )
    result = [(seg.start, seg.end, seg.text) for seg in segments]
    if result:
        return result

    print("[WARN] VAD(音声区間検出)がセグメントを1つも検出できませんでした。"
          "vad_filter=Falseで再試行します。")
    segments, _info = model.transcribe(str(audio_path), beam_size=5, vad_filter=False)
    result = [(seg.start, seg.end, seg.text) for seg in segments]
    if not result:
        print("[WARN] vad_filter=Falseでもセグメントを検出できませんでした。"
              "曲全体への機械的な文字数比分配にフォールバックします"
              "（タイミング精度は大きく低下します）。")
    return result


def _get_audio_duration(audio_path):
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


def _levenshtein(a, b):
    if a == b:
        return 0
    la, lb = len(a), len(b)
    if la == 0:
        return lb
    if lb == 0:
        return la
    prev = list(range(lb + 1))
    for i, ca in enumerate(a, 1):
        cur = [i] + [0] * lb
        for j, cb in enumerate(b, 1):
            cost = 0 if ca == cb else 1
            cur[j] = min(prev[j] + 1, cur[j - 1] + 1, prev[j - 1] + cost)
        prev = cur
    return prev[lb]


def _similarity(a, b):
    """0.0-1.0の正規化類似度（1.0が完全一致）。"""
    if not a and not b:
        return 1.0
    max_len = max(len(a), len(b), 1)
    return 1.0 - (_levenshtein(a, b) / max_len)


def _flatten_timeline(segments):
    """whisperの生セグメント[(start, end, text), ...] を、
    1本の連結テキスト + 文字オフセット→時刻 のブレークポイント列に変換する。
    (MoneyPrinterTurbo/app/services/subtitle.py の _flatten_timeline と
    同じ考え方。セグメント間は空白1文字で連結する。)"""
    parts = []
    breakpoints = []  # (start_offset, end_offset, seg_start, seg_end)
    offset = 0
    for start, end, text in segments:
        text = text.strip()
        if not text:
            continue
        start_off = offset
        parts.append(text)
        offset += len(text)
        breakpoints.append((start_off, offset, start, end))
        parts.append(" ")
        offset += 1
    return "".join(parts), breakpoints


def _time_at_offset(breakpoints, char_offset):
    if not breakpoints:
        return 0.0
    if char_offset <= breakpoints[0][0]:
        return breakpoints[0][2]
    for start_off, end_off, seg_start, seg_end in breakpoints:
        if start_off <= char_offset <= end_off:
            seg_len = max(end_off - start_off, 1)
            frac = (char_offset - start_off) / seg_len
            return seg_start + frac * (seg_end - seg_start)
    return breakpoints[-1][3]


def _content_aware_align(lyric_lines, whisper_segments, audio_duration):
    """whisperの認識テキストを使い、歌詞行ごとに一番近い位置を探して
    時刻を割り当てる（correct()と同じ発想）。

    whisperの認識精度が悪くても、「セグメントの開始/終了時刻はおおむね
    合っている」という前提のもと、類似度が低い行はウィンドウ内の
    文字数比例分割にフォールバックする（内容が全く見つからない場合でも、
    「その行の長さぶんだけ進める」ことで、後続の行への連鎖的なズレを防ぐ）。
    """
    flat_text, breakpoints = _flatten_timeline(whisper_segments)
    total_len = len(flat_text)

    result = []
    pos = 0
    for line in lyric_lines:
        if pos >= total_len:
            break

        target_len = len(line)
        window = max(6, int(target_len * 0.35))
        lo = max(pos + 1, pos + target_len - window)
        hi = min(total_len, pos + target_len + window)

        best_end = min(pos + target_len, total_len)
        best_score = _similarity(line, flat_text[pos:best_end])
        for candidate_end in range(lo, hi + 1):
            score = _similarity(line, flat_text[pos:candidate_end])
            if score > best_score:
                best_score = score
                best_end = candidate_end

        if best_score < 0.35:
            # 一致するものが見つからない: それでも行の長さぶんだけ進める。
            # 「一致しなかったのでその場に留まる」方が、後続行すべてが
            # 巻き添えでズレる最悪のケースになるため。
            best_end = min(pos + target_len, total_len)

        start_time = _time_at_offset(breakpoints, pos)
        end_time = _time_at_offset(breakpoints, best_end)
        if end_time <= start_time:
            end_time = start_time + 0.3

        result.append({"line": line, "start": start_time, "end": end_time})
        pos = best_end
        if pos < total_len and flat_text[pos] == " ":
            pos += 1

    # flat_textを使い切って残った歌詞行は、残り音声時間に文字数比例で配分する
    consumed = len(result)
    remaining_lines = lyric_lines[consumed:]
    if remaining_lines and audio_duration:
        cursor = result[-1]["end"] if result else 0.0
        available = max(audio_duration - cursor, 0.1)
        rem_chars = sum(max(len(l), 1) for l in remaining_lines) or 1
        for line in remaining_lines:
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


def _uniform_fallback_align(lyric_lines, audio_duration):
    """whisperセグメントが1つも取れなかった場合の最終フォールバック。
    曲全体を文字数比例で単純分配する（タイミング精度は大きく低下する）。"""
    if not audio_duration:
        raise ValueError(
            "whisperセグメントが1つも取れず、audio_durationも不明なため、"
            "タイミングを推定できません。"
        )
    total_chars = sum(max(len(line), 1) for line in lyric_lines) or 1
    cursor = 0.0
    result = []
    for line in lyric_lines:
        share = audio_duration * (max(len(line), 1) / total_chars)
        end = min(cursor + share, audio_duration)
        result.append({"line": line, "start": cursor, "end": end})
        cursor = end
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

    if whisper_segments:
        alignment = _content_aware_align(lyric_lines, whisper_segments, audio_duration)
    else:
        alignment = _uniform_fallback_align(lyric_lines, audio_duration)

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
