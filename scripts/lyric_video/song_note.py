#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
song_note.py
01_Songs/<title>.md から、歌詞行のリストとfrontmatterメタデータを抽出する。

歌詞行の抽出は「## 歌詞 & 楽曲構成」直下のフェンス(```)コードブロックを
改行分割・空行除去するだけの単純な方式にしている。
MoneyPrinterTurbo (app/utils/utils.py) の split_string_by_punctuations は
句読点(、等)でも分割してしまい、1行の歌詞ノートに書かれた印刷上の1行
(例:「だから、僕は黙った」)を誤って2行に割ってしまうため、あえて使わない。

[Verse]/[Chorus]等のSuno構成タグ行は、歌詞のキャプション表示には不要なので
除外する。
"""

import re
from pathlib import Path

# Suno構成タグ行の判定（例: [Intro] [Verse 1] [Pre-Chorus] [Chorus – Hook A] 等）
_TAG_LINE_RE = re.compile(r"^\s*\[[^\]]+\]\s*$")

QUOTED_FRONTMATTER_FIELDS = {
    "title", "artist", "key", "genre", "status",
    "spotify_url", "youtube_url", "apple_music_url", "daw_project_path",
}


def read_frontmatter_value(text, field):
    """song_worksheet.py と同じ流儀の正規表現ベースのfrontmatter読み取り。
    改行を跨いで誤マッチしないよう、コロン直後は [ \\t]* のみ許容する。"""
    if field in QUOTED_FRONTMATTER_FIELDS:
        m = re.search(rf'^{field}:[ \t]*"([^"]*)"', text, re.MULTILINE)
    else:
        m = re.search(rf'^{field}:[ \t]*(.*)$', text, re.MULTILINE)
    return m.group(1).strip() if m else ""


def read_bpm(text):
    """bpmフロントマターを読む。0または空は「未記録」としてNoneを返す。"""
    raw = read_frontmatter_value(text, "bpm")
    try:
        value = int(raw)
    except (TypeError, ValueError):
        return None
    return value if value > 0 else None


def extract_lyric_lines(text):
    """「## 歌詞 & 楽曲構成」直下の最初のフェンスコードブロックから、
    歌詞行のリストを返す。構成タグ行([Verse]等)と空行は除外する。"""
    return [line for line, _section in extract_lyric_lines_with_sections(text)]


def extract_lyric_lines_with_sections(text):
    """extract_lyric_lines と同じ行を、直前の構成タグ名つきで返す。
    [(歌詞行, "Chorus – Hook A"), ...]。タグより前の行のセクションは ""。"""
    section_match = re.search(
        r"^##\s*歌詞\s*&?\s*楽曲構成\s*$", text, re.MULTILINE
    )
    if not section_match:
        raise ValueError("「## 歌詞 & 楽曲構成」セクションが見つかりません")

    after_heading = text[section_match.end():]
    fence_match = re.search(r"```[^\n]*\n(.*?)```", after_heading, re.DOTALL)
    if not fence_match:
        raise ValueError("歌詞のコードブロック(```)が見つかりません")

    block = fence_match.group(1)

    lines = []
    section = ""
    for raw_line in block.split("\n"):
        line = raw_line.strip()
        if not line:
            continue
        if _TAG_LINE_RE.match(line):
            section = line.strip()[1:-1].strip()
            continue
        lines.append((line, section))

    if not lines:
        raise ValueError("歌詞行を1行も抽出できませんでした")

    return lines


def sections_for_alignment(alignment, lyric_sections):
    """alignment の各行の構成タグ名を返す。GUIで行を追加・削除した後でも
    対応が取れるよう、行の section → src（ノートの行番号）→ 同じ文言の
    ノート行 → 位置 の順で決める。"""
    by_text = {}
    for line, section in lyric_sections:
        by_text.setdefault(line, section)
    same_length = len(alignment) == len(lyric_sections)
    out = []
    for i, row in enumerate(alignment):
        if row.get("section") is not None:
            out.append(row["section"])
        elif isinstance(row.get("src"), int) and 0 <= row["src"] < len(lyric_sections):
            out.append(lyric_sections[row["src"]][1])
        elif row.get("line") in by_text:
            out.append(by_text[row["line"]])
        elif same_length:
            out.append(lyric_sections[i][1])
        else:
            out.append(out[-1] if out else "")
    return out


class SongNote:
    def __init__(self, path):
        self.path = Path(path)
        self.text = self.path.read_text(encoding="utf-8")
        self.title = read_frontmatter_value(self.text, "title") or self.path.stem
        self.bpm = read_bpm(self.text)
        self.genre = read_frontmatter_value(self.text, "genre")
        self.tags = read_frontmatter_value(self.text, "tags")
        self.lyric_sections = extract_lyric_lines_with_sections(self.text)
        self.lyric_lines = [line for line, _section in self.lyric_sections]

    @property
    def meta(self):
        """描画の技法選び（kinetic_fx.song_profile）に渡す曲の情報。"""
        return {"title": self.title, "genre": self.genre, "tags": self.tags, "bpm": self.bpm}

    def __repr__(self):
        return f"SongNote(title={self.title!r}, bpm={self.bpm}, lines={len(self.lyric_lines)})"


if __name__ == "__main__":
    import sys

    if len(sys.argv) != 2:
        print("使い方: python3 song_note.py <01_Songs/曲名.md>")
        sys.exit(1)

    note = SongNote(sys.argv[1])
    print(note)
    for i, line in enumerate(note.lyric_lines, 1):
        print(f"{i:3d}: {line}")
