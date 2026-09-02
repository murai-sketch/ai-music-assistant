#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
beats.py
ビート/オンセット検出。背景画像のパルス演出を駆動するための「ビート時刻」配列を作る。

librosa.beat.beat_track単体（周期的なテンポを仮定する動的計画法ベース）は
デスコアのブレイクダウン・変拍子に弱いと判断し、あえて使わない。

代わりに:
  1. librosa.effects.hpss() で打楽器成分(percussive)を分離
  2. librosa.onset.onset_detect(backtrack=True) でオンセット（打撃的な変化点）を検出
  3. 最小間隔・強度パーセンタイル閾値で間引く（スタイルごとにパラメータ化）

frontmatterのbpmが分かっている場合は、librosaのstart_bpmヒントとして
テンポ推定を安定化させるのに使う（onset_detect自体はstart_bpmを取らないため、
tempo推定を経由する場合のみ有効。省略しても動作する）。

キャッシュ: align.pyと同じ音声ハッシュをキーに _work/<hash>/beats.json に保存。
"""

import hashlib
import json
from pathlib import Path

try:
    import librosa
    import numpy as np
except ImportError:
    librosa = None
    np = None

WORK_DIR = Path(__file__).resolve().parent / "_work"


def _audio_hash(audio_path):
    h = hashlib.sha256()
    with open(audio_path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()[:8]


def detect_beats(
    audio_path,
    min_gap_sec=0.13,
    strength_percentile=70,
    bpm_hint=None,
    use_cache=True,
):
    """打楽器成分のオンセット時刻配列（秒）を返す。

    Args:
        min_gap_sec: 連続するパルス間の最小間隔（これより短い間隔のオンセットは
            間引く。kawaiiは短め=高頻度、deathcoreは長め=低頻度、を想定）。
        strength_percentile: このパーセンタイル未満のオンセット強度は捨てる
            （0にすると全オンセットを採用、高いほど大きな打撃のみ残る）。
        bpm_hint: frontmatterのbpm。onset_strength/onset_detectにはテンポ事前
            情報を渡す口が無いため直接のチューニングには使わないが、オンセットが
            1つも検出できなかった場合の最終フォールバック（bpmから機械的に
            60/bpm間隔のビート列を生成する）にのみ使う。
    """
    if librosa is None:
        raise RuntimeError(
            "librosa がインストールされていません。"
            "scripts/lyric_video/requirements.txt を pip install してください。"
        )

    audio_path = Path(audio_path)
    audio_hash = _audio_hash(audio_path)
    cache_dir = WORK_DIR / audio_hash
    cache_path = cache_dir / "beats.json"
    cache_key = {
        "min_gap_sec": min_gap_sec,
        "strength_percentile": strength_percentile,
        "bpm_hint": bpm_hint,
    }

    if use_cache and cache_path.exists():
        cached = json.loads(cache_path.read_text(encoding="utf-8"))
        if cached.get("params") == cache_key:
            return cached["beats"]

    y, sr = librosa.load(str(audio_path), sr=None, mono=True)
    duration = librosa.get_duration(y=y, sr=sr)

    _, y_percussive = librosa.effects.hpss(y)

    onset_env = librosa.onset.onset_strength(y=y_percussive, sr=sr)
    onset_frames = librosa.onset.onset_detect(
        onset_envelope=onset_env, sr=sr, backtrack=True
    )
    onset_times = librosa.frames_to_time(onset_frames, sr=sr)
    onset_strengths = onset_env[onset_frames] if len(onset_frames) else np.array([])

    if len(onset_strengths):
        threshold = float(np.percentile(onset_strengths, strength_percentile))
    else:
        threshold = 0.0

    filtered = []
    last_time = -min_gap_sec
    for t, strength in zip(onset_times, onset_strengths):
        if strength < threshold:
            continue
        if t - last_time < min_gap_sec:
            continue
        filtered.append(float(t))
        last_time = t

    if not filtered and bpm_hint:
        # オンセットが1つも取れなかった場合の最終フォールバック:
        # frontmatterのbpmから機械的に等間隔のビート列を生成する。
        interval = 60.0 / float(bpm_hint)
        t = 0.0
        while t < duration:
            filtered.append(t)
            t += interval

    cache_dir.mkdir(parents=True, exist_ok=True)
    cache_path.write_text(
        json.dumps({"params": cache_key, "beats": filtered}, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    return filtered


if __name__ == "__main__":
    import sys

    if len(sys.argv) < 2:
        print("使い方: python3 beats.py <audio> [bpm_hint]")
        sys.exit(1)

    bpm_hint = float(sys.argv[2]) if len(sys.argv) > 2 else None
    beats = detect_beats(sys.argv[1], bpm_hint=bpm_hint)
    print(f"{len(beats)} 個のビート/オンセットを検出:")
    print(", ".join(f"{t:.2f}" for t in beats))
