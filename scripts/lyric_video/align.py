#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
align.py
既知の歌詞行を、実際の歌唱音声に対して時刻合わせする。

whisperの生セグメントを既知の台本テキストへ再アライメントする既存実装
（MoneyPrinterTurbo の subtitle.correct()）を参考にした、内容一致度ベースの
アライメント。

--- 経緯（重要）---
当初は「whisperの認識テキストは歌唱音声では信用できない」という想定で、
セグメントの開始/終了時刻だけを使い、歌詞行をセグメント数に機械的に
按分するだけの簡易版にしていた。

しかし実際の楽曲（歪んだボーカルのデスコア系）でテストしたところ、
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

さらに同じ曲で、v7まで手動補正しても「字幕が歌より先に出る」ままだった。
原因は2つ:
  1. 先頭から順に1行ずつ当てはめる貪欲な探索は、歌詞ノートに1回しか
     書かれていない繰り返し（サビの2回し等）や、whisperの認識抜けに
     出会うと、そこから後ろの行を全部前に詰めてしまう
     （実例: 最後の6行が実際より40〜60秒早く配置された）。
  2. セグメントの開始時刻は、直前の間奏・無音を含んで早めに出ることがある。
そのため、単語単位のタイムスタンプ（word_timestamps）を取り、歌詞全体と
認識テキスト全体を文字単位で一括照合する方式（_global_align）を標準にした。
繰り返し・抜けは照合側で読み飛ばされ、一致しなかった行は前後の一致行の
間に文字数比で配置される。

キャッシュ: 音声ファイルのSHA256先頭8桁をキーに、_work/<hash>/ 配下に
whisper_words.json（生の文字起こし）と alignment.json（行タイミング）を
保存する。whisper_words.json があれば文字起こしをスキップするので、
照合ロジックだけを変えて再計算できる（whisperは実行ごとに結果が揺れるため）。
"""

import bisect
import difflib
import hashlib
import json
import statistics
import unicodedata
from pathlib import Path

try:
    from faster_whisper import WhisperModel
except ImportError:
    WhisperModel = None

WORK_DIR = Path(__file__).resolve().parent / "_work"

# 歌詞が日本語前提のVault。自動判定に任せると、歪んだボーカルで判定が
# 揺れることがあるため固定する。
WHISPER_LANGUAGE = "ja"

# これより短い一致ブロックは偶然の一致（助詞1文字など）として捨てる
MIN_MATCH_BLOCK = 2

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


def _transcribe_words(audio_path, audio_duration=None):
    """whisperで単語タイムスタンプ付きの文字起こしをし、
    [{"start", "end", "text", "words": [{"start", "end", "word"}, ...]}, ...] を返す。

    まずVAD(vad_filter=True)で試す。歪んだ/デスコア的な発声・ミックスに
    埋もれたボーカル、子どもっぽい歌声など、通常の音声と大きく異なる音源では
    VADが「音声区間なし」「ごく一部だけ」と誤判定することがある
    （実測: デスコア系のリミックスで0件、子ども向けの曲で冒頭40秒だけ）。
    結果が曲の6割に届かなければ vad_filter=False でもやり直し、単語の多い方を使う。"""
    model = _get_model()

    def run(vad):
        kwargs = dict(beam_size=5, vad_filter=vad, word_timestamps=True,
                      language=WHISPER_LANGUAGE, condition_on_previous_text=False)
        if vad:
            kwargs["vad_parameters"] = dict(min_silence_duration_ms=200)
        segments, _info = model.transcribe(str(audio_path), **kwargs)
        return [
            {
                "start": seg.start, "end": seg.end, "text": seg.text,
                "words": [
                    {"start": w.start, "end": w.end, "word": w.word}
                    for w in (seg.words or [])
                ],
            }
            for seg in segments
        ]

    def n_words(res):
        return sum(len(s["words"]) for s in res)

    result = run(True)
    covered = result[-1]["end"] if result else 0.0
    if result and (not audio_duration or covered >= audio_duration * 0.6):
        return result
    if result:
        print(f"[WARN] VAD(音声区間検出)で取れたのが {covered:.0f}秒までだけでした。"
              "vad_filter=Falseでもやり直して、単語の多い方を使います。")
    else:
        print("[WARN] VAD(音声区間検出)がセグメントを1つも検出できませんでした。"
              "vad_filter=Falseで再試行します。")
    retry = run(False)
    if n_words(retry) > n_words(result):
        result = retry
    if not result:
        print("[WARN] vad_filter=Falseでもセグメントを検出できませんでした。"
              "曲全体への機械的な文字数比分配にフォールバックします"
              "（タイミング精度は大きく低下します）。")
    return result


def _normalize_char(c):
    """照合用に1文字を正規化する。空白・記号は None（照合対象外）。"""
    c = unicodedata.normalize("NFKC", c)
    if not c or c.isspace():
        return None
    if unicodedata.category(c[0])[0] in ("P", "S"):
        return None
    # カタカナ→ひらがな（whisperの表記揺れ吸収）
    o = ord(c[0])
    if 0x30A1 <= o <= 0x30F6:
        c = chr(o - 0x60)
    return c.lower()


def _global_align(lyric_lines, word_segments, audio_duration):
    """歌詞全体とwhisper認識テキスト全体を文字単位で一括照合し、
    行ごとの{line, start, end}を返す。

    - 一致ブロックは両側で単調増加なので、繰り返し・認識抜けは読み飛ばされる
    - 行の開始は、その行で最初に一致した文字の時刻（単語内は線形補間）から、
      それより前の不一致文字ぶんを平均文字長で差し引いて求める
    - 1文字も一致しなかった行は、前後の一致行の間に文字数比で配置する
    """
    w_chars, w_start, w_end = [], [], []
    for seg in word_segments:
        for w in seg["words"]:
            raw = [c for c in (_normalize_char(ch) for ch in w["word"]) if c]
            n = len(raw)
            if not n:
                continue
            dur = max(w["end"] - w["start"], 0.0)
            for i, c in enumerate(raw):
                w_chars.append(c)
                w_start.append(w["start"] + dur * i / n)
                w_end.append(w["start"] + dur * (i + 1) / n)

    l_chars, l_line = [], []
    line_len = []
    for idx, line in enumerate(lyric_lines):
        cs = [c for c in (_normalize_char(ch) for ch in line) if c]
        line_len.append(max(len(cs), 1))
        for c in cs:
            l_chars.append(c)
            l_line.append(idx)

    matcher = difflib.SequenceMatcher(None, l_chars, w_chars, autojunk=False)
    matched = {}  # lyric char index -> whisper char index
    for a, b, size in matcher.get_matching_blocks():
        if size < MIN_MATCH_BLOCK:
            continue
        for k in range(size):
            matched[a + k] = b + k

    char_durs = [e - s for s, e in zip(w_start, w_end) if e > s]
    per_char = min(statistics.median(char_durs), 0.4) if char_durs else 0.2

    n_lines = len(lyric_lines)
    starts = [None] * n_lines
    ends = [None] * n_lines
    line_first_char = {}
    for li_char, line_idx in enumerate(l_line):
        line_first_char.setdefault(line_idx, li_char)
    for li_char in sorted(matched):
        line_idx = l_line[li_char]
        wi = matched[li_char]
        if starts[line_idx] is None:
            offset = li_char - line_first_char[line_idx]
            starts[line_idx] = max(w_start[wi] - offset * per_char, 0.0)
        ends[line_idx] = w_end[wi]

    matched_count = sum(1 for s in starts if s is not None)
    print(f"      一括照合: {matched_count}/{n_lines}行が認識テキストと一致")
    if matched_count == 0:
        return None

    # 不一致行は、前後の一致文字に挟まれた「認識テキスト側の隙間」へ
    # 位置の比率で対応づける（認識が崩れただけの行は、ここで拾える）
    matched_keys = sorted(matched)
    for i in range(n_lines):
        if starts[i] is not None:
            continue
        la = line_first_char.get(i)
        if la is None:
            continue
        k = bisect.bisect_left(matched_keys, la)
        if k == 0 or k == len(matched_keys):
            continue
        pa, na = matched_keys[k - 1], matched_keys[k]
        pb, nb = matched[pa], matched[na]
        if nb - pb - 1 <= 0:
            continue
        wi = pb + 1 + (la - pa - 1) * (nb - pb - 1) // max(na - pa - 1, 1)
        starts[i] = w_start[wi]
        gap_chars = line_len[i] * (nb - pb - 1) // max(na - pa - 1, 1)
        ends[i] = w_end[min(wi + max(gap_chars, 1) - 1, nb - 1)]

    # それでも決まらない行は、前後の決まった行の間に文字数比で配置
    anchors = [i for i in range(n_lines) if starts[i] is not None]
    first, last = anchors[0], anchors[-1]
    for i in range(first - 1, -1, -1):
        starts[i] = max(starts[i + 1] - line_len[i] * per_char * 2, 0.0)
    for left, right in zip(anchors, anchors[1:]):
        gap = right - left - 1
        if gap <= 0:
            continue
        t0 = ends[left] if ends[left] is not None else starts[left]
        t1 = starts[right]
        span = max(t1 - t0, 0.0)
        total = sum(line_len[left + 1:right]) or 1
        cursor = t0
        for i in range(left + 1, right):
            starts[i] = cursor
            cursor += span * line_len[i] / total
    tail_end = audio_duration or (ends[last] or starts[last]) + 3.0
    if last < n_lines - 1:
        t0 = ends[last] if ends[last] is not None else starts[last]
        span = max(tail_end - t0, 0.1)
        total = sum(line_len[last + 1:]) or 1
        cursor = t0
        for i in range(last + 1, n_lines):
            starts[i] = cursor
            cursor += span * line_len[i] / total

    matched_per_line = [0] * n_lines
    for li_char in matched:
        matched_per_line[l_line[li_char]] += 1

    result = []
    for i, line in enumerate(lyric_lines):
        if i + 1 < n_lines:
            end = starts[i + 1]
        else:
            end = ends[i] + 0.5 if ends[i] is not None else tail_end
            if audio_duration:
                end = min(end, audio_duration)
        result.append({
            "line": line, "start": starts[i], "end": end,
            # src: 歌詞ノートの何行目(0始まり)か / match: 認識テキストと一致した文字の割合
            "src": i, "match": round(matched_per_line[i] / line_len[i], 2),
        })

    for i in range(1, len(result)):
        if result[i]["start"] < result[i - 1]["start"]:
            result[i]["start"] = result[i - 1]["start"]
    for i in range(len(result)):
        if i + 1 < len(result):
            result[i]["end"] = result[i + 1]["start"]
        if result[i]["end"] <= result[i]["start"]:
            result[i]["end"] = result[i]["start"] + 0.3
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
        if cached.get("edited"):
            # 歌詞ノートが変わった。GUIでの手直しを失わないよう退避してから作り直す
            backup = cache_path.with_name(f"alignment.bak-lyrics-changed-{audio_hash}.json")
            backup.write_text(cache_path.read_text(encoding="utf-8"), encoding="utf-8")
            print(f"[WARN] 歌詞ノートが変わったため自動タイミングを作り直します。"
                  f"手直し済みの版は {backup.name} に退避しました（GUIの「バックアップから戻す」で戻せます）。")

    audio_duration = _get_audio_duration(audio_path)
    words_path = cache_dir / "whisper_words.json"
    if use_cache and words_path.exists():
        word_segments = json.loads(words_path.read_text(encoding="utf-8"))
    else:
        word_segments = _transcribe_words(audio_path, audio_duration)
        cache_dir.mkdir(parents=True, exist_ok=True)
        words_path.write_text(
            json.dumps(word_segments, ensure_ascii=False, indent=1), encoding="utf-8"
        )

    alignment = None
    if word_segments:
        alignment = _global_align(lyric_lines, word_segments, audio_duration)
        if alignment is None:
            print("[WARN] 一括照合で1行も一致しませんでした。"
                  "セグメント単位の逐次照合にフォールバックします。")
            segments = [(s["start"], s["end"], s["text"]) for s in word_segments]
            alignment = _content_aware_align(lyric_lines, segments, audio_duration)
    if alignment is None:
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
