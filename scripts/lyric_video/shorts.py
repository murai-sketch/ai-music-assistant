#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
shorts.py
歌詞タイミング（alignment）から、TikTok / YouTube ショート向けの
「切り抜き区間」を自動で選ぶ。

縦1080x1920・30fpsという出力そのものは kinetic.py と共通なので、
ここで決めるのは「曲のどこを、どこからどこまで切るか」だけ。

切り口（mode）:
  hook   サビ頭から始まる区間を最優先する（既定。最初の数秒で離脱が決まるため）
  scene  情景・心情の見える区間を選ぶ。サビの繰り返しではなく、Verse や Bridge、
         囁きの、その曲にしか無い場面を拾う。同じ曲から何本も出すときの2本目以降や、
         「サビばかりになる」のを避けたいときに使う
  mix    両方を混ぜて上位から選ぶ

選び方の考え:
  - 歌詞の途中で切らない。行の頭で始まり、行の終わりで終わる
  - まとまり（同じ構成タグが続き、間奏で途切れない範囲）の切れ目で終わる
  - 歌っていない時間（間奏・余白）の割合が高い区間は落とす
  - 長さは 15〜60秒。目安（既定30秒）から離れるほど減点する
  - ビートが分かっていれば、始点を直前のビートへ吸着させて入りを揃える

戻り値は候補の一覧（スコア順）。呼ぶ側は区間を render_kinetic の
t_start / t_end に渡して書き出す。
"""

SHORT_MIN = 15.0        # ショートの下限（秒）
SHORT_MAX = 60.0        # ショートの上限（秒）
DEFAULT_TARGET = 30.0   # 長さの目安（秒）
LEAD_IN = 0.7           # 歌い出しの前に足す助走
TAIL = 0.9              # 最後の行の後に残す余韻
PHRASE_GAP = 1.2        # これ以上あいたらフレーズの切れ目
BLOCK_GAP = 2.5         # これ以上あいたら別のまとまり（kinetic.INTERLUDE_MIN と同じ）
BEAT_SNAP = 0.35        # 始点をビートへ吸着させる許容範囲

KIND_WEIGHT = {"hook": 3.0, "pre": 1.6, "bridge": 1.2, "verse": 1.0,
               "outro": 0.6, "intro": 0.4, "quiet": 0.3}
KIND_JA = {"hook": "サビ", "pre": "サビ前", "bridge": "Bridge", "verse": "Verse",
           "outro": "アウトロ", "intro": "イントロ", "quiet": "囁き"}


# 情景（scene）の手がかり。特定の曲の歌詞ではなく、日本語の歌で場面や心情を
# 担いやすい一般的な語を並べている。漢字・かな両方を見るのは、子ども向けの曲が
# すべてひらがなで書かれるため。
SCENE_WORDS = (
    "夜 朝 昼 夕 空 星 月 雨 雪 風 海 川 道 街 窓 部屋 光 影 花 春 夏 秋 冬 坂 駅 電車 帰り 眠 布団 手 声 息 指 影 鏡 扉 坂道 "
    "よる あさ ひる ゆうがた そら ほし つき あめ ゆき かぜ うみ かわ みち まち まど へや ひかり かげ はな ねむ ふとん て こえ いき ゆび とびら"
).split()
HEART_WORDS = (
    "泣 涙 笑 嬉 哀 寂 恋 愛 好 痛 苦 願 祈 夢 心 胸 独 孤 優 温 怖 待 抱 忘 覚 守 信 許 "
    "なけ なき なみだ わら うれし かなし さみし こわ ねが ゆめ こころ むね ひとり やさし あたた まもる わすれ おぼえ すき だいすき ごめん ありがと だいじょうぶ"
).split()


def scene_score(text):
    """1行が「情景・心情」をどれだけ担っているか。0〜1。"""
    hits = sum(1 for w in SCENE_WORDS if w in text) + sum(1.3 for w in HEART_WORDS if w in text)
    return min(hits / 2.0, 1.0)


def section_kind(section):
    """構成タグ名を、切り抜きの価値が分かる種別に丸める。"""
    s = (section or "").lower()
    if "囁" in (section or ""):
        return "quiet"
    if "pre" in s:
        return "pre"
    if "chorus" in s or "hook" in s or "サビ" in (section or ""):
        return "hook"
    if "bridge" in s:
        return "bridge"
    if "intro" in s:
        return "intro"
    if "outro" in s:
        return "outro"
    return "verse"


def _sung_spans(alignment, max_hold):
    """行ごとの「実際に歌っている区間」。alignment の end は次の行の頭と
    同じ値になっていることが多いので、kinetic の表示と同じ考えで
    次の行の頭と max_hold で頭打ちにする。"""
    spans = []
    for i, row in enumerate(alignment):
        start = float(row["start"])
        nxt = float(alignment[i + 1]["start"]) if i + 1 < len(alignment) else None
        end = float(row["end"])
        if nxt is not None:
            end = min(end, nxt)
        end = min(end, start + max_hold)
        spans.append((start, max(end, start + 0.1)))
    return spans


def _blocks(spans, kinds):
    """同じ種別が続き、間奏で途切れないまとまりの [(先頭行, 末尾行)]。"""
    if not spans:
        return []
    out = []
    head = 0
    for i in range(1, len(spans)):
        if spans[i][0] - spans[i - 1][1] > BLOCK_GAP or kinds[i] != kinds[i - 1]:
            out.append((head, i - 1))
            head = i
    out.append((head, len(spans) - 1))
    return out


def _snap(t, beats):
    """始点を直前（わずかに手前）のビートへ吸着させる。"""
    if not beats:
        return t
    near = [b for b in beats if abs(b - t) <= BEAT_SNAP]
    return min(near, key=lambda b: abs(b - t)) if near else t


MODES = ("hook", "scene", "mix")

# 切り口ごとの、始まりの種別の重み
KIND_WEIGHT_SCENE = {"bridge": 2.8, "quiet": 2.6, "verse": 2.4, "pre": 1.8,
                     "outro": 1.4, "intro": 1.0, "hook": 0.5}


def find_shorts(alignment, sections=None, beats=None, duration=None,
                target=DEFAULT_TARGET, min_sec=SHORT_MIN, max_sec=SHORT_MAX,
                limit=5, max_hold=2.8, mode="hook"):
    """切り抜き候補をスコア順に返す。

    mode: hook（サビ優先・既定）/ scene（情景優先）/ mix（両方）

    各候補: {start, end, length, start_row, end_row, n_lines, kind,
             section, label, reasons, density, score}
    """
    if mode == "mix":
        picks = []
        for m in ("hook", "scene"):
            picks += find_shorts(alignment, sections, beats, duration, target, min_sec,
                                 max_sec, limit, max_hold, mode=m)
        picks.sort(key=lambda c: -c["score"])
        out = []
        for c in picks:
            if any(_overlap(c, p) > 0.5 for p in out):
                continue
            out.append(c)
            if len(out) >= limit:
                break
        for rank, c in enumerate(out, 1):
            c["rank"] = rank
        return out
    if mode not in MODES:
        raise ValueError(f"mode は {MODES} のいずれか: {mode!r}")
    alignment = [r for r in alignment if r.get("start") is not None]
    if len(alignment) < 2:
        return []
    sections = list(sections or [r.get("section") or "" for r in alignment])
    sections += [""] * (len(alignment) - len(sections))
    kinds = [section_kind(s) for s in sections]
    spans = _sung_spans(alignment, max_hold)
    blocks = _blocks(spans, kinds)
    block_start = {b[0] for b in blocks}
    block_end = {b[1] for b in blocks}
    hook_blocks = [b for b in blocks if kinds[b[0]] == "hook"]
    total = float(duration) if duration else spans[-1][1] + TAIL
    min_sec = max(float(min_sec), 3.0)
    max_sec = max(float(max_sec), min_sec + 1.0)
    target = min(max(float(target), min_sec), max_sec)

    def gap_before(i):
        return spans[i][0] - spans[i - 1][1] if i > 0 else float("inf")

    def gap_after(j):
        return (spans[j + 1][0] - spans[j][1]) if j + 1 < len(spans) else max(total - spans[j][1], 0.0)

    texts = [r.get("line", "") for r in alignment]
    repeat = {}
    for s in texts:
        repeat[s] = repeat.get(s, 0) + 1
    weights = KIND_WEIGHT_SCENE if mode == "scene" else KIND_WEIGHT

    cands = []
    for i in range(len(spans)):
        t0 = max(_snap(spans[i][0] - LEAD_IN, beats), 0.0)
        for j in range(i, len(spans)):
            t1 = min(spans[j][1] + min(TAIL, gap_after(j) + 0.15), total)
            length = t1 - t0
            if length < min_sec:
                continue
            if length > max_sec:
                break
            sung = sum(min(e, t1) - max(s, t0) for s, e in spans[i:j + 1])
            density = max(min(sung / length, 1.0), 0.0)
            reasons = []
            score = weights.get(kinds[i], 1.0)
            name = KIND_JA.get(kinds[i], kinds[i])
            if i in block_start:
                score += 1.2
                reasons.append("サビ頭から始まる" if kinds[i] == "hook" else f"{name}の頭から始まる")
            elif gap_before(i) > PHRASE_GAP:
                score += 0.2
                reasons.append(f"{name}の途中（フレーズの切れ目）から始まる")
            else:
                score -= 1.0
                reasons.append(f"{name}の途中から始まる")
            if j in block_end:
                score += 1.2
                reasons.append("まとまりの終わりで切れる")
            elif gap_after(j) > PHRASE_GAP:
                score += 0.2
            else:
                score -= 0.8
            covered = [b for b in hook_blocks if b[0] >= i and b[1] <= j]
            hook_rows = sum(1 for k in range(i, j + 1) if kinds[k] == "hook")
            if mode == "scene":
                # 情景を選ぶときは、サビの割合が高い区間ほど下げる
                score -= 2.2 * (hook_rows / (j - i + 1))
                scene = sum(scene_score(texts[k]) for k in range(i, j + 1)) / (j - i + 1)
                score += 3.0 * scene
                if scene >= 0.4:
                    reasons.append("情景・心情の言葉が多い")
                fresh = sum(1 for k in range(i, j + 1) if repeat.get(texts[k], 1) == 1) / (j - i + 1)
                score += 1.2 * fresh
                if fresh >= 0.9:
                    reasons.append("繰り返しでない行だけで出来ている")
                if kinds[i] in ("quiet", "bridge"):
                    reasons.append("静かな場面から始まる")
            else:
                if covered:
                    score += 2.0
                    if kinds[i] != "hook":
                        reasons.append("サビをまるごと含む")
                elif kinds[i] != "hook":
                    score -= 0.6
                first_hook = next((k for k in range(i, j + 1) if kinds[k] == "hook"), None)
                if first_hook is not None and spans[first_hook][0] - t0 <= 5.0:
                    score += 0.8
                    if kinds[i] != "hook":
                        reasons.append("出だし5秒以内にサビが来る")
            score += 2.5 * density
            if density >= 0.75:
                reasons.append("歌の密度が高い")
            score -= 2.5 * abs(length - target) / target
            if abs(length - target) <= 3.0:
                reasons.append(f"長さが目安（{target:.0f}秒）どおり")
            cands.append({
                "start": round(t0, 2), "end": round(t1, 2), "length": round(length, 2),
                "start_row": i, "end_row": j, "n_lines": j - i + 1,
                "kind": kinds[i], "section": sections[i],
                "label": f"{name}から{j - i + 1}行"
                         + ("・サビ入り" if covered and kinds[i] != "hook" and mode != "scene" else ""),
                "mode": mode,
                "reasons": reasons, "density": round(density, 2), "score": round(score, 3),
            })

    cands.sort(key=lambda c: -c["score"])
    picked = []
    for c in cands:
        if any(_overlap(c, p) > 0.5 for p in picked):
            continue
        picked.append(c)
        if len(picked) >= limit:
            break
    for rank, c in enumerate(picked, 1):
        c["rank"] = rank
    return picked


def _overlap(a, b):
    """2つの区間の重なりを、短い方に対する割合で返す。"""
    inter = min(a["end"], b["end"]) - max(a["start"], b["start"])
    if inter <= 0:
        return 0.0
    return inter / max(min(a["length"], b["length"]), 0.01)


def format_candidates(cands):
    """CLI 表示用。歌詞そのものは出さない（画面外に残さないため）。"""
    if not cands:
        return "  候補が見つかりませんでした（歌詞タイミングを先に作ってください）"
    out = []
    for c in cands:
        out.append(
            f"  [{c['rank']}] {c['start']:7.2f}〜{c['end']:7.2f}秒 "
            f"({c['length']:5.2f}秒 / {c['n_lines']}行) {c['label']}"
        )
        out.append(f"        {c['section'] or '構成タグなし'} ｜ " + "、".join(c["reasons"] or ["-"]))
    return "\n".join(out)


def output_name(cand, stem="short"):
    """書き出しファイル名。順位・開始秒・長さが後から分かるようにする。"""
    return f"{stem}_{cand.get('rank', 0):02d}_{cand['start']:07.2f}-{cand['end']:07.2f}_{cand['length']:.0f}s.mp4"
