#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
tidy_work.py
_work/ の掃除。作業ファイルは一定期間で消し、完成版は保存先（NAS・クラウド）へ移す。

--------------------------------------------------------------------------
考え方:

    _work/<音源ハッシュ>/     GUI と CLI の作業場。書き出しのたびに mp4 が増える
                              → 動画・静止画は「作業ファイル」。既定 7日で削除
    _work/<曲名>/             完成版の置き場（CLI の --out で指定した先）
                              → **同じ名前の最新版だけ**が「完成版」。既定 30日で保存先へ移動
                              → 古い版（output_v1 に対する v2・v3 があるなど）は作業ファイル扱い
    _work/backgrounds/        背景素材。触らない
    _work/<...>/post/         投稿した一式（動画＋キャプション）。完成版として扱う

    **タイミングと設計のファイルは消さない。** alignment.json（手で直した正本）、
    そのバックアップ、kinetic_plan.json、beats.json、whisper_words.json、
    backgrounds.json は、動画を作り直すための元なので残す。

    完成版を移したあとは、同じ場所に <名前>.moved.txt を置き、どこへ移したかを書く。
    「消えた」と「移した」を後から区別できるようにするため。

対象（--profile）:

    lyric （既定）  歌詞動画の _work/。上の考え方のとおり
    shorts          ShortsVault の pipeline/output/。1話ごとのフォルダの中で、
                    投稿した mp4（final_with_cta_bump.mp4。無ければ final_with_title.mp4）
                    だけを完成版とし、題字入り前の版・確認用の静止画・ログを作業ファイルとする。
                    看板（title.txt）・キャプション・サムネイルは残す

使い方:

    # 何が起きるかだけ見る（既定。何も変更しない）
    python3 scripts/lyric_video/tidy_work.py

    # 実行する
    python3 scripts/lyric_video/tidy_work.py --apply --dest /Volumes/NAS/ERPJ/歌詞動画

    # 期間を変える
    python3 scripts/lyric_video/tidy_work.py --work-days 14 --final-days 60

    保存先（--dest）は2通り:
      ローカル・NAS   /Volumes/NAS/ERPJ/歌詞動画       （そのまま移動する）
      クラウド        gdrive:ERPJ/歌詞動画             （rclone で転送する。remote:path の形）
    環境変数 TIDY_DEST に書いておけば --dest を省ける。
    省略すると、完成版は移さず、作業ファイルの削除だけを行う。
--------------------------------------------------------------------------
"""

import argparse
import os
import re
import shutil
import subprocess
import time
from pathlib import Path

WORK_DIR = Path(__file__).resolve().parent / "_work"
SHORTS_DIR = Path("/Users/armada/YAMADA/ShortsVault/pipeline/output")
HASH_DIR_RE = re.compile(r"^[0-9a-f]{8}$")          # 音源ハッシュのフォルダ
MEDIA_EXT = {".mp4", ".mov", ".m4v", ".png", ".jpg", ".jpeg", ".webp"}
# 作り直すために要るもの。日付に関わらず残す
KEEP_NAMES = {"alignment.json", "beats.json", "whisper_words.json",
              "backgrounds.json", "kinetic_plan.json", "kinetic_plan.md"}
KEEP_PREFIX = ("alignment.",)                        # alignment.bak-*.json など


def _age_days(path):
    return (time.time() - path.stat().st_mtime) / 86400


def _size(paths):
    return sum(p.stat().st_size for p in paths if p.exists())


def _human(n):
    for unit in ("B", "KB", "MB", "GB"):
        if n < 1024 or unit == "GB":
            return f"{n:.0f}{unit}" if unit == "B" else f"{n/1:.1f}{unit}"
        n /= 1024
    return f"{n:.1f}GB"


def _keep(path):
    return path.name in KEEP_NAMES or path.name.startswith(KEEP_PREFIX)


VERSION_SUFFIX_RE = re.compile(r"_v\d+(?:_[^_]+)*$")  # output_v13_bg → output


def _latest_finals(paths):
    """同じ作品の版違い（output_v1/v2/v3 など）は最新の1つだけを完成版とする。"""
    groups = {}
    for p in paths:
        key = (p.parent, VERSION_SUFFIX_RE.sub("", p.stem))
        cur = groups.get(key)
        if cur is None or p.stat().st_mtime > cur.stat().st_mtime:
            groups[key] = p
    return set(groups.values())


def scan(work_dir, work_days, final_days):
    """(消す作業ファイル, 移す完成版) を返す。"""
    to_delete, to_move, finals = [], [], []
    for song_dir in sorted(p for p in work_dir.iterdir() if p.is_dir()):
        if song_dir.name == "backgrounds":
            continue
        is_work = bool(HASH_DIR_RE.match(song_dir.name))
        for path in sorted(song_dir.rglob("*")):
            if not path.is_file() or _keep(path):
                continue
            in_post = "post" in path.relative_to(song_dir).parts
            age = _age_days(path)
            if is_work and not in_post:
                if path.suffix.lower() in MEDIA_EXT and age >= work_days:
                    to_delete.append(path)
            else:
                # 完成版・投稿一式。動画だけを移す（キャプション等は手元に残す）
                if path.suffix.lower() in {".mp4", ".mov", ".m4v"}:
                    finals.append((path, in_post))
    # 版違いは最新だけを完成版として残し、古い版は作業ファイルとして消す
    latest = _latest_finals([p for p, in_post in finals if not in_post])
    for path, in_post in finals:
        age = _age_days(path)
        if in_post or path in latest:
            if age >= final_days:
                to_move.append(path)
        elif age >= work_days:
            to_delete.append(path)
    return to_delete, to_move


def is_remote(dest):
    """rclone の remote 指定（gdrive:ERPJ/… の形）か。Windows のドライブ文字は考えない。"""
    head, sep, _tail = str(dest).partition(":")
    return bool(sep) and "/" not in head and not str(dest).startswith("/")


def move_to_remote(path, remote_path):
    """rclone で1ファイル移す。成功したときだけローカルから消える（moveto の動作）。"""
    if shutil.which("rclone") is None:
        raise SystemExit("[ERROR] rclone がありません。`brew install rclone` を実行してください。")
    r = subprocess.run(["rclone", "moveto", str(path), remote_path, "--stats-one-line"],
                       capture_output=True, text=True)
    if r.returncode != 0:
        print(f"    !! 転送できませんでした: {path.name}\n       {r.stderr.strip().splitlines()[-1] if r.stderr.strip() else ''}")
        return False
    return True


SHORTS_FINAL = ("final_with_cta_bump.mp4", "final_with_title.mp4")
SHORTS_KEEP_EXT = {".txt", ".json", ".md"}
SHORTS_KEEP_NAMES = {"thumbnail.jpg"}


def scan_shorts(out_dir, work_days, final_days):
    """ShortsVault: 1話ごとのフォルダを見て、投稿した動画だけを完成版とする。"""
    to_delete, to_move = [], []
    for ep in sorted(p for p in out_dir.iterdir() if p.is_dir()):
        files = [p for p in ep.iterdir() if p.is_file()]
        final = next((ep / n for n in SHORTS_FINAL if (ep / n).exists()), None)
        for path in files:
            if path.suffix.lower() in SHORTS_KEEP_EXT or path.name in SHORTS_KEEP_NAMES:
                continue
            age = _age_days(path)
            if final is not None and path == final:
                if age >= final_days:
                    to_move.append(path)
            elif path.suffix.lower() in MEDIA_EXT or path.suffix.lower() == ".log":
                if age >= work_days:
                    to_delete.append(path)
    return to_delete, to_move


def main():
    ap = argparse.ArgumentParser(description="_work/ の作業ファイルを消し、完成版を保存先へ移す")
    ap.add_argument("--work-days", type=int, default=7, help="作業ファイルを消すまでの日数（既定 7）")
    ap.add_argument("--final-days", type=int, default=30, help="完成版を移すまでの日数（既定 30）")
    ap.add_argument("--dest", default=os.environ.get("TIDY_DEST"),
                    help="完成版の移動先。ローカルのパス、または rclone の remote:path"
                         "（環境変数 TIDY_DEST でも指定できる）")
    ap.add_argument("--apply", action="store_true", help="実際に削除・移動する（省略時は表示のみ）")
    ap.add_argument("--profile", default="lyric", choices=("lyric", "shorts"),
                    help="どの並びのフォルダを見るか（既定 lyric）")
    ap.add_argument("--work-dir", help="対象フォルダ（既定は profile ごとの標準の場所）")
    ap.add_argument("--log", help="結果を追記するログファイル")
    args = ap.parse_args()

    work_dir = Path(args.work_dir) if args.work_dir else (
        WORK_DIR if args.profile == "lyric" else SHORTS_DIR)
    remote = args.dest is not None and is_remote(args.dest)
    if not work_dir.exists():
        print(f"[ERROR] ありません: {work_dir}")
        raise SystemExit(1)

    scanner = scan if args.profile == "lyric" else scan_shorts
    to_delete, to_move = scanner(work_dir, args.work_days, args.final_days)
    dest = (args.dest if remote else Path(args.dest).expanduser()) if args.dest else None

    print(f"対象: {work_dir}")
    print(f"作業ファイル {args.work_days}日 / 完成版 {args.final_days}日"
          + (f" → {dest}" if dest else "（保存先の指定なし。完成版は移しません）"))
    print()
    print(f"■ 消す作業ファイル: {len(to_delete)}件 {_human(_size(to_delete))}")
    for p in to_delete[:20]:
        print(f"    {_age_days(p):5.1f}日  {_human(p.stat().st_size):>8}  {p.relative_to(work_dir)}")
    if len(to_delete) > 20:
        print(f"    …ほか {len(to_delete) - 20}件")
    print()
    print(f"■ 保存先へ移す完成版: {len(to_move)}件 {_human(_size(to_move))}")
    for p in to_move[:20]:
        print(f"    {_age_days(p):5.1f}日  {_human(p.stat().st_size):>8}  {p.relative_to(work_dir)}")

    if not args.apply:
        print("\nこれは表示だけです。実行するには --apply を付けてください。")
        return

    freed = 0
    for p in to_delete:
        freed += p.stat().st_size
        p.unlink()
    moved = 0
    if dest:
        for p in to_move:
            rel = p.relative_to(work_dir)
            if remote:
                target = f"{str(dest).rstrip('/')}/{rel.as_posix()}"
                if not move_to_remote(p, target):
                    continue
            else:
                target_dir = dest / rel.parent
                target_dir.mkdir(parents=True, exist_ok=True)
                target = target_dir / p.name
                if target.exists():
                    target = target_dir / f"{p.stem}_{int(p.stat().st_mtime)}{p.suffix}"
                shutil.move(str(p), str(target))
            p.with_suffix(p.suffix + ".moved.txt").write_text(
                f"{time.strftime('%Y-%m-%d %H:%M')} に移動しました\n{target}\n", encoding="utf-8")
            moved += 1
    line = (f"{time.strftime('%Y-%m-%d %H:%M')} [{args.profile}] "
            f"削除 {len(to_delete)}件 {_human(freed)} ／ 移動 {moved}件"
            + (f" → {dest}" if dest else ""))
    if args.log:
        log = Path(args.log).expanduser()
        log.parent.mkdir(parents=True, exist_ok=True)
        with log.open("a", encoding="utf-8") as f:
            f.write(line + "\n")
    print(f"\n[DONE] 削除 {len(to_delete)}件 {_human(freed)} ／ 移動 {moved}件")
    if to_move and not dest:
        print("       --dest を指定していないので、完成版は移していません。")


if __name__ == "__main__":
    main()
