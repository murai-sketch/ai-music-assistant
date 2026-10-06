#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
measure_flicker.py
点滅の検査器。書き出した動画（mp4）の明るさの揺れを、全体と窓ごとに数える。

測り方（明るさ Y は 0〜1 の相対輝度）：
  1. 1080×1920 のフレームの画素ごとに、sRGB を線形（光の量）に戻して相対輝度 Y（0.2126R + 0.7152G + 0.0722B）にする
  2. Y を 108×192 に縮める（BOX。10×10 画素の平均。線形にしてから平均する＝縮めてから線形に戻さない）
  3. 窓ごとに Y の平均の時系列を作る
  4. 時系列を「上がり続ける／下がり続ける」区間に分ける（向きの反転は 0.02 以上戻ったときだけ。揺らぎで区間を割らない）
  5. 区間の |ΔY| ≥ 0.10 を「激しい変化」1回と数える。明るさの水準の条件は付けない
  6. どの窓でも、どの1秒の間でも、激しい変化が 3 回以上なら不合格、2 回なら警告

窓（合否に使う）：画面全体 ＋ 540×960 の窓 9つ（x＝0/270/540、y＝0/480/960）。最も厳しい窓で決める。
窓（警告として別に出す。合否には使わない）：360×480 の窓 12（3列×4行）。1秒に 2 回以上で警告、3 回以上は強い警告。
  --strict-g12 を付けると、この 12 窓も 3 回未満を求める（新しい部品のデモの検査用。D3〜D6）。強い警告が出たら不合格にする。
窓（並べて出すだけ・合否に使わない）：mv-review の採点の 360×480 を半分ずつ重ねた 35 区画（review）。
参考（合否に使わない）：WCAG の数え方（暗い側の Y < 0.80、明暗の往復1組を1回。1秒に 4 回以上で不通過）。

使い方:
    python3 measure_flicker.py <動画.mp4> [--json 出力.json] [--from 秒] [--to 秒] [--strict-g12]
    終了コード 0＝合格（警告を含む）、1＝不合格、2＝測れない

書き出しの途中のフレーム（render_kinetic の frame()）をそのまま測るときは FlickerMeter.add(frame) を使う。
"""

import argparse
import json
import subprocess
import sys

import numpy as np

SMALL_W, SMALL_H = 108, 192
SCALE = 10                     # 1080 → 108（BOX の一辺）
DELTA = 0.10                   # 激しい変化の |ΔY|
HYST = 0.02                    # 区間を割る戻りの大きさ
FAIL_N, WARN_N = 3, 2          # 1秒の間の回数
WCAG_DARK_MAX = 0.80
WCAG_FAIL_FLASHES = 4          # WCAG：1秒に 3 回までは可。4 回で不通過
LUM = np.array([0.2126, 0.7152, 0.0722], dtype=np.float64)


def _window(name, group, x, y, w, h):
    """画面の px で窓を決める → 縮小画像の行・列の範囲"""
    return {"name": name, "group": group, "rect_px": [x, y, w, h],
            "r0": y // SCALE, "r1": (y + h) // SCALE, "c0": x // SCALE, "c1": (x + w) // SCALE}


def build_windows():
    ws = [_window("all", "all", 0, 0, 1080, 1920)]
    for y in (0, 480, 960):
        for x in (0, 270, 540):
            ws.append(_window(f"q9_x{x}_y{y}", "q9", x, y, 540, 960))
    for r in range(4):
        for c in range(3):
            ws.append(_window(f"g12_c{c}_r{r}", "g12", c * 360, r * 480, 360, 480))
    for y in range(0, 1920 - 480 + 1, 240):
        for x in range(0, 1080 - 360 + 1, 180):
            ws.append(_window(f"r35_x{x}_y{y}", "r35", x, y, 360, 480))
    return ws


WINDOWS = build_windows()
JUDGE_GROUPS = ("all", "q9")             # 合否に使う窓（dev-lead の決定。仕様 §6）
WARN_GROUPS = ("g12",)                   # 警告として別に出す窓（--strict-g12 のときだけ合否にも使う）
_LIN = None


def _srgb_to_linear_table():
    global _LIN
    if _LIN is None:
        v = np.arange(256, dtype=np.float64) / 255.0
        _LIN = np.where(v <= 0.03928, v / 12.92, ((v + 0.055) / 1.055) ** 2.4)
    return _LIN


def small_from_frame(frame):
    """1080×1920 の RGB（uint8、高さ×幅×3）→ 192×108 の相対輝度 Y。画素ごとに線形（光の量）にしてから、10×10 画素を平均する"""
    a = np.asarray(frame)
    if a.shape != (SMALL_H * SCALE, SMALL_W * SCALE, 3):
        raise ValueError(f"フレームの大きさが 1080×1920 ではありません: {a.shape}")
    if a.dtype != np.uint8:
        a = np.clip(np.rint(a), 0, 255).astype(np.uint8)
    y = _srgb_to_linear_table()[a] @ LUM
    return y.reshape(SMALL_H, SCALE, SMALL_W, SCALE).mean(axis=(1, 3))


def window_means(y):
    """192×108 の Y から、全窓の平均（WINDOWS の順）"""
    ii = np.zeros((SMALL_H + 1, SMALL_W + 1))
    ii[1:, 1:] = y.cumsum(0).cumsum(1)
    out = np.empty(len(WINDOWS))
    for k, w in enumerate(WINDOWS):
        s = ii[w["r1"], w["c1"]] - ii[w["r0"], w["c1"]] - ii[w["r1"], w["c0"]] + ii[w["r0"], w["c0"]]
        out[k] = s / ((w["r1"] - w["r0"]) * (w["c1"] - w["c0"]))
    return out


def segments(series, hyst=HYST):
    """時系列を上がり続ける／下がり続ける区間に分ける。戻り値 [(開始の添字, 終わりの添字, ΔY)]。
    向きの反転は、極値から hyst 以上戻ったときだけ数える"""
    n = len(series)
    if n == 0:
        return []
    segs = []
    pivot = (0, float(series[0]))
    ext = pivot
    direction = 0
    for i in range(1, n):
        v = float(series[i])
        if direction == 0:
            if v - pivot[1] >= hyst:
                direction, ext = 1, (i, v)
            elif pivot[1] - v >= hyst:
                direction, ext = -1, (i, v)
            continue
        if direction == 1:
            if v >= ext[1]:
                ext = (i, v)
            elif ext[1] - v >= hyst:
                segs.append((pivot[0], ext[0], ext[1] - pivot[1]))
                pivot, ext, direction = ext, (i, v), -1
        else:
            if v <= ext[1]:
                ext = (i, v)
            elif v - ext[1] >= hyst:
                segs.append((pivot[0], ext[0], ext[1] - pivot[1]))
                pivot, ext, direction = ext, (i, v), 1
    if direction != 0:
        segs.append((pivot[0], ext[0], ext[1] - pivot[1]))
    return segs


def max_in_second(times):
    """times（昇順）のうち、どの1秒 [t, t+1) にも入る最大の個数と、そのときの開始時刻"""
    best, at, j = 0, None, 0
    for i, t in enumerate(times):
        while times[j] < t - 1.0 + 1e-9:
            j += 1
        # [times[j], t] が 1 秒未満に収まる
        if i - j + 1 > best:
            best, at = i - j + 1, times[j]
    return best, at


def count_window(series, fps, y_all=None):
    """1つの窓の時系列から、激しい変化の一覧と判定用の値を出す"""
    events = []
    for a, b, dy in segments(series):
        if abs(dy) >= DELTA:
            # 変化の時刻は、変化の半分に達したフレーム（徐々に落ち着く H.264 の揺れや長い区間の終わりに引きずられない）
            half = next((i for i in range(a, b + 1) if abs(series[i] - series[a]) >= abs(dy) / 2), b)
            events.append({"t0": round(a / fps, 3), "t1": round(b / fps, 3), "t": round(half / fps, 3), "dY": round(dy, 4),
                           "y0": round(float(series[a]), 4), "y1": round(float(series[b]), 4)})
    times = [e["t"] for e in events]
    # 1秒の間の回数は、変化の時刻（t）で数える
    n1, at = max_in_second(times)
    # WCAG（参考）：暗い側 < 0.80 の変化だけ。逆向きに続く2つで1回の往復
    q = [e for e in events if min(e["y0"], e["y1"]) < WCAG_DARK_MAX]
    flashes = [(q[i]["t"], q[i + 1]["t"]) for i in range(len(q) - 1) if q[i]["dY"] * q[i + 1]["dY"] < 0]
    best_f, f_at, j = 0, None, 0
    ends = [f[1] for f in flashes]
    for i, t in enumerate(ends):
        while ends[j] < t - 1.0 + 1e-9:
            j += 1
        if i - j + 1 > best_f:
            best_f, f_at = i - j + 1, flashes[j][0]
    return {"events": events, "n_events": len(events), "max_events_1s": n1, "at_sec": at,
            "wcag_ref": {"events": len(q), "max_flashes_1s": best_f, "at_sec": f_at,
                         "pass": best_f < WCAG_FAIL_FLASHES}}


def evaluate(series_by_window, fps=30.0, strict_g12=False):
    """窓ごとの時系列（WINDOWS の順の配列 [フレーム数×窓数]）から、判定と窓ごとの結果を出す。
    strict_g12: 360×480 の12窓も 3 回未満を求める（新しい部品のデモの検査用）。付けなければ警告として出すだけ"""
    arr = np.asarray(series_by_window)
    results = {}
    for k, w in enumerate(WINDOWS):
        r = count_window(arr[:, k], fps)
        r.update({"group": w["group"], "rect_px": w["rect_px"],
                  "y_min": round(float(arr[:, k].min()), 4), "y_max": round(float(arr[:, k].max()), 4)})
        results[w["name"]] = r
    judged = {n: r for n, r in results.items() if r["group"] in JUDGE_GROUPS}
    worst = max(judged.items(), key=lambda kv: (kv[1]["max_events_1s"], kv[1]["n_events"]))
    worst_n = worst[1]["max_events_1s"]
    verdict = "fail" if worst_n >= FAIL_N else "warn" if worst_n >= WARN_N else "pass"
    # 360×480 の12窓：警告として別に出す（1秒に2回以上＝警告、3回以上＝強い警告）。--strict-g12 のときだけ、強い警告を不合格にする
    g12 = {n: r for n, r in results.items() if r["group"] in WARN_GROUPS}
    g12_worst = max(g12.items(), key=lambda kv: (kv[1]["max_events_1s"], kv[1]["n_events"]))
    g12_n = g12_worst[1]["max_events_1s"]
    g12_level = "strong" if g12_n >= FAIL_N else "warn" if g12_n >= WARN_N else "none"
    if strict_g12 and g12_level == "strong":
        verdict = "fail"
    review = {n: r for n, r in results.items() if r["group"] == "r35"}
    rev_worst = max(review.items(), key=lambda kv: (kv[1]["max_events_1s"], kv[1]["n_events"]))
    wcag = {n: r["wcag_ref"] for n, r in results.items() if r["group"] in JUDGE_GROUPS}
    wcag_worst = max(wcag.items(), key=lambda kv: kv[1]["max_flashes_1s"])
    summary = {
        "verdict": verdict,
        "worst_window": worst[0], "worst_max_events_1s": worst_n, "worst_at_sec": worst[1]["at_sec"],
        "frames": int(arr.shape[0]), "fps": fps, "seconds": round(arr.shape[0] / fps, 3),
        "judged_windows": len(judged),
        "strict_g12": bool(strict_g12),
        "g12_warn": {"level": g12_level, "worst_window": g12_worst[0], "max_events_1s": g12_n, "at_sec": g12_worst[1]["at_sec"],
                     "windows_over_warn": sorted(n for n, r in g12.items() if r["max_events_1s"] >= WARN_N)},
        "wcag_ref": {"pass": all(v["pass"] for v in wcag.values()), "worst_window": wcag_worst[0],
                     "max_flashes_1s": wcag_worst[1]["max_flashes_1s"]},
        "review35_ref": {"worst_window": rev_worst[0], "max_events_1s": rev_worst[1]["max_events_1s"],
                         "at_sec": rev_worst[1]["at_sec"],
                         "over_review_line": rev_worst[1]["max_events_1s"] >= 6},   # 採点の基準（往復3組＝転換6回）
    }
    return {"summary": summary, "windows": results}


class FlickerMeter:
    """書き出しの途中のフレームをそのまま測る。add(frame) を全フレームに呼び、最後に result()。描き直さない"""

    def __init__(self, fps=30.0, strict_g12=False):
        self.fps = fps
        self.strict_g12 = strict_g12
        self.rows = []

    def add(self, frame):
        """1080×1920 の RGB（uint8）を1枚。mp4 の復号も書き出しの途中のフレームも、同じこの経路（線形にしてから縮める）"""
        self.rows.append(window_means(small_from_frame(frame)))

    def result(self):
        return evaluate(np.asarray(self.rows), self.fps, self.strict_g12)


def decode_frames(path, t0=None, t1=None, fps=30):
    """mp4 を ffmpeg で復号して、1080×1920 のフレーム（RGB）を順に返す。縮小は FlickerMeter.add の中（線形にしてから平均）"""
    cmd = ["ffmpeg", "-loglevel", "error"]
    if t0 is not None:
        cmd += ["-ss", str(t0)]
    cmd += ["-i", str(path)]
    if t1 is not None:
        cmd += ["-t", str(t1 - (t0 or 0))]
    cmd += ["-an", "-vf", f"fps={fps}", "-pix_fmt", "rgb24", "-f", "rawvideo", "-"]
    p = subprocess.Popen(cmd, stdout=subprocess.PIPE)
    W, H = SMALL_W * SCALE, SMALL_H * SCALE
    size = W * H * 3
    while True:
        buf = p.stdout.read(size)
        if len(buf) < size:
            break
        yield np.frombuffer(buf, dtype=np.uint8).reshape(H, W, 3)
    p.wait()
    if p.returncode not in (0, None):
        raise RuntimeError(f"ffmpeg が失敗しました（終了コード {p.returncode}）。動画は 1080×1920 にしてください")


def check_video_size(path):
    """動画の大きさが 1080×1920 か確かめる（違う大きさを 1080×1920 として読むと、画が崩れて誤った警告・合格が出る。
    例：2 本を横に並べた 2160 幅の動画）。違えば RuntimeError。ffprobe で読めないときも RuntimeError（黙って測らない）"""
    try:
        r = subprocess.run(["ffprobe", "-v", "error", "-select_streams", "v:0", "-show_entries", "stream=width,height",
                            "-of", "csv=p=0:s=x", str(path)], capture_output=True, text=True)
    except OSError as e:
        raise RuntimeError(f"ffprobe が使えません（動画の大きさを確かめられないので測りません）: {e}")
    out = r.stdout.strip().splitlines()
    try:
        w, h = (int(v) for v in out[0].split("x"))
    except (IndexError, ValueError):
        raise RuntimeError(f"動画の大きさを読めません（ffprobe 終了コード {r.returncode}）: {path}")
    if (w, h) != (SMALL_W * SCALE, SMALL_H * SCALE):
        raise RuntimeError(f"動画の大きさが {w}×{h} です。1080×1920 の動画だけ測れます（並べた動画・縮小した動画は、元の1本ずつを測ってください）")


def measure_video(path, t0=None, t1=None, fps=30, strict_g12=False):
    check_video_size(path)
    m = FlickerMeter(fps, strict_g12)
    for frame in decode_frames(path, t0, t1, fps):
        m.add(frame)
    if not m.rows:
        raise RuntimeError("フレームを1枚も読めませんでした")
    return m.result()


def format_summary(res):
    s = res["summary"]
    verd = {"pass": "合格", "warn": "合格（警告）", "fail": "不合格"}[s["verdict"]]
    lines = [f"点滅の検査: {verd}（{s['seconds']}秒・{s['frames']}フレーム、合否に使う窓 {s['judged_windows']}"
             + ("、360×480の12窓も合否に使う" if s.get("strict_g12") else "") + "）",
             f"  最も厳しい窓: {s['worst_window']}  1秒の間の激しい変化 最大 {s['worst_max_events_1s']} 回"
             + (f"（{s['worst_at_sec']}秒から）" if s["worst_at_sec"] is not None else "")]
    g = s["g12_warn"]
    if g["level"] != "none":
        lines.append(f"  {'強い警告' if g['level'] == 'strong' else '警告'}（360×480の12窓。合否には使わない"
                     + ("が、--strict-g12 のため不合格に使った" if (s.get("strict_g12") and g["level"] == "strong") else "") + f"）: "
                     f"{g['worst_window']} {g['max_events_1s']}回/秒 {g['at_sec']}秒から（2回以上の窓 {len(g['windows_over_warn'])}）")
    else:
        lines.append(f"  360×480の12窓: 最大 {g['max_events_1s']} 回/秒（警告なし）")
    w = s["wcag_ref"]
    lines.append(f"  参考 WCAG の数え方: 1秒の往復 最大 {w['max_flashes_1s']} 回（{w['worst_window']}）→ {'通過' if w['pass'] else '不通過'}（合否には使わない）")
    r = s["review35_ref"]
    lines.append(f"  参考 mv-review 35区画: 1秒の転換 最大 {r['max_events_1s']} 回（{r['worst_window']}）"
                 f"→ 採点の基準（6回）{'以上' if r['over_review_line'] else '未満'}（合否には使わない）")
    if s["verdict"] != "pass":
        hit = sorted(((n, r) for n, r in res["windows"].items() if r["group"] in JUDGE_GROUPS and r["max_events_1s"] >= WARN_N),
                     key=lambda kv: -kv[1]["max_events_1s"])
        for n, r in hit[:3]:
            lines.append(f"  {'不合格' if r['max_events_1s'] >= FAIL_N else '警告'}: {n} {r['rect_px']} "
                         f"{r['max_events_1s']}回/秒 {r['at_sec']}秒から（Y {r['y_min']}〜{r['y_max']}）")
        if len(hit) > 3:
            lines.append(f"  ほか {len(hit) - 3} 窓（JSON に全部）")
    return "\n".join(lines)


def main():
    ap = argparse.ArgumentParser(description="書き出した動画の点滅を検査する")
    ap.add_argument("video")
    ap.add_argument("--json", help="結果の JSON の出力先")
    ap.add_argument("--from", dest="t0", type=float)
    ap.add_argument("--to", dest="t1", type=float)
    ap.add_argument("--strict-g12", action="store_true", help="360×480 の12窓も3回未満を求める（新しい部品のデモの検査用）")
    args = ap.parse_args()
    try:
        res = measure_video(args.video, args.t0, args.t1, strict_g12=args.strict_g12)
    except (RuntimeError, OSError) as e:
        print(f"[ERROR] {e}")
        sys.exit(2)
    if args.json:
        with open(args.json, "w", encoding="utf-8") as f:
            json.dump(res, f, ensure_ascii=False, indent=1)
    print(format_summary(res))
    sys.exit(1 if res["summary"]["verdict"] == "fail" else 0)


if __name__ == "__main__":
    main()
