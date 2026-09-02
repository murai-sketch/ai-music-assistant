#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
song_worksheet.py
01_Songs/ にある既存の楽曲ノートから、まだ埋まっていない項目
（アーティスト名・BPM・Key・配信URL・コード進行など）だけを
プレーンテキストのワークシートとして書き出し／反映するツール。

CSVと違い、1項目ずつ「ラベル: 値」の行で書くだけなので、
歌詞のようなカンマや改行を含むデータを気にする必要がない。
歌詞・SUNOスタイルはこのワークシートでは扱わない
（既にノート側に入っている前提。変更したい場合はノートを直接編集する）。

--------------------------------------------------------------------------
使い方:

    1) ワークシートを生成する（未入力項目を空欄で書き出す）
        python3 scripts/song_worksheet.py generate

       -> 01_Songs/_worksheet.txt が作られる（Git管理外フォルダなので追跡されない）

    2) 01_Songs/_worksheet.txt の空欄を埋めて保存する
       （空欄のままにした項目は「未入力」として扱われ、既存値は変更されない）

    3) ノートに反映する
        python3 scripts/song_worksheet.py apply

--------------------------------------------------------------------------
"""
import re
import sys
from pathlib import Path

SCRIPT_DIR = Path(__file__).resolve().parent
VAULT_ROOT = SCRIPT_DIR.parent
SONGS_DIR = VAULT_ROOT / "01_Songs"
WORKSHEET_PATH = SONGS_DIR / "_worksheet.txt"

SEP = "=" * 40

# フロントマター側フィールド（key -> 正規表現・出力フォーマット）
FRONTMATTER_FIELDS = ["artist", "release_date", "bpm", "key",
                       "spotify_url", "youtube_url", "apple_music_url",
                       "daw_project_path"]

# 本文「音楽的仕様」側フィールド
BODY_FIELDS = {
    "コード進行": "コード進行",
    "使用シンセ_音源": "使用シンセ/音源",
    "使用プラグイン": "使用プラグイン（EQ/Comp/FXなど）",
}

QUOTED_FRONTMATTER = {"artist", "key", "spotify_url", "youtube_url",
                       "apple_music_url", "daw_project_path"}


def read_frontmatter_value(text, field):
    if field in QUOTED_FRONTMATTER:
        m = re.search(rf'^{field}:[ \t]*"([^"]*)"', text, re.MULTILINE)
    else:
        m = re.search(rf'^{field}:[ \t]*(.*)$', text, re.MULTILINE)
    if not m:
        return ""
    val = m.group(1).strip()
    if field == "release_date" and val in ("YYYY-MM-DD", ""):
        return ""
    if field == "bpm" and val in ("0", ""):
        return ""
    return val


def read_status_value(text):
    m = re.search(r'^status:[ \t]*"([^"]*)"', text, re.MULTILINE)
    return m.group(1).strip() if m else "?"


def read_body_value(text, label):
    m = re.search(rf'^- {re.escape(label)}:[ \t]*(.*)$', text, re.MULTILINE)
    return m.group(1).strip() if m else ""


def cmd_generate():
    if not SONGS_DIR.exists():
        print("[ERROR] 01_Songs/ が見つかりません。")
        sys.exit(1)

    song_files = sorted(
        p for p in SONGS_DIR.glob("*.md")
        if not p.name.startswith("_")
    )
    if not song_files:
        print("[INFO] 01_Songs/ に曲ノートが見つかりませんでした。")
        return

    lines = [
        "# SUNO情報 埋め込みワークシート",
        "#",
        "# 空欄のまま保存したフィールドは更新されません（既存値を保持します）。",
        "# 埋めたら python3 scripts/song_worksheet.py apply で各ノートに反映します。",
        "# 曲名の行は変更しないでください（ファイル名と対応づけて反映します）。",
        "",
    ]

    for path in song_files:
        text = path.read_text(encoding="utf-8")
        title = path.stem

        status = read_status_value(text)
        genre = read_frontmatter_value(text, "genre") or "-"
        m_suno = re.search(r'^- SUNOバージョン:\s*(.*)$', text, re.MULTILINE)
        suno = m_suno.group(1).strip() if m_suno else "-"

        lines.append(SEP)
        lines.append(f"曲名: {title}")
        lines.append(f"[参考: status={status} / genre={genre} / suno={suno}]")
        lines.append("")

        for field in FRONTMATTER_FIELDS:
            current = read_frontmatter_value(text, field)
            lines.append(f"{field}: {current}")

        for key, label in BODY_FIELDS.items():
            current = read_body_value(text, label)
            lines.append(f"{key}: {current}")

        lines.append("")

    lines.append(SEP)

    WORKSHEET_PATH.write_text("\n".join(lines), encoding="utf-8")
    print(f"[DONE] {len(song_files)}曲分のワークシートを作成しました: {WORKSHEET_PATH}")
    print("       空欄を埋めて保存したら `apply` を実行してください。")


def parse_worksheet(text):
    blocks = text.split(SEP)
    songs = []
    for block in blocks:
        m = re.search(r'^曲名:\s*(.+)$', block, re.MULTILINE)
        if not m:
            continue
        title = m.group(1).strip()
        fields = {}
        for line in block.splitlines():
            line = line.strip()
            if not line or line.startswith("#") or line.startswith("[参考") or line.startswith("曲名:"):
                continue
            if ":" not in line:
                continue
            key, _, val = line.partition(":")
            fields[key.strip()] = val.strip()
        songs.append((title, fields))
    return songs


def apply_frontmatter_field(text, field, value):
    if field in QUOTED_FRONTMATTER:
        pattern = rf'^{field}:[ \t]*"[^"]*"'
        replacement = f'{field}: "{value}"'
    else:
        pattern = rf'^{field}:[ \t]*.*$'
        replacement = f'{field}: {value}'
    new_text, n = re.subn(pattern, replacement, text, count=1, flags=re.MULTILINE)
    return new_text if n else text


def apply_body_field(text, label, value):
    pattern = rf'^- {re.escape(label)}:[ \t]*.*$'
    replacement = f'- {label}: {value}'
    new_text, n = re.subn(pattern, replacement, text, count=1, flags=re.MULTILINE)
    return new_text if n else text


def cmd_apply():
    if not WORKSHEET_PATH.exists():
        print(f"[ERROR] ワークシートが見つかりません: {WORKSHEET_PATH}")
        print("        先に `generate` を実行してください。")
        sys.exit(1)

    text = WORKSHEET_PATH.read_text(encoding="utf-8")
    songs = parse_worksheet(text)

    updated_count = 0
    for title, fields in songs:
        note_path = SONGS_DIR / f"{title}.md"
        if not note_path.exists():
            print(f"[SKIP] ノートが見つかりません: {title}")
            continue

        note_text = note_path.read_text(encoding="utf-8")
        original = note_text
        changed_fields = []

        for field in FRONTMATTER_FIELDS:
            val = fields.get(field, "")
            if val:
                note_text = apply_frontmatter_field(note_text, field, val)
                changed_fields.append(field)

        for key, label in BODY_FIELDS.items():
            val = fields.get(key, "")
            if val:
                note_text = apply_body_field(note_text, label, val)
                changed_fields.append(label)

        if note_text != original:
            note_path.write_text(note_text, encoding="utf-8")
            print(f"[UPDATE] {title}: {', '.join(changed_fields)}")
            updated_count += 1

    print(f"\n[DONE] {updated_count}曲のノートを更新しました。")


def print_usage():
    print(__doc__)


def main():
    if len(sys.argv) < 2 or sys.argv[1] not in ("generate", "apply"):
        print_usage()
        sys.exit(1)
    if sys.argv[1] == "generate":
        cmd_generate()
    else:
        cmd_apply()


if __name__ == "__main__":
    main()
