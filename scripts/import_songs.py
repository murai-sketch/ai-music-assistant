#!/usr/bin/env python3
"""
import_songs.py
Obsidian & AI Music Lab Starter Kit - 既存楽曲の一括取り込みスクリプト

機能1: CSV一括取り込み
    CSVファイル（曲名, リリース日, BPM, Key, SpotifyURL等）を読み込み、
    01_Songs/ 内に標準化されたMarkdownノートを一括生成する。

機能2: URL/曲名からの取り込み（基本処理・モック）
    Spotify/YouTube等の公開URLや曲名を渡すと、取得できる範囲のメタデータ
    （タイトル・アーティスト・カバーアートURL等）でMarkdownノートを生成する。
    本格的なAPI連携（Spotify Web API認証など）は行わず、
    OGP(Open Graph)タグを軽くスクレイピングする程度の基本実装に留めている。
    精度が必要な場合は各サービスの公式APIキーを取得して拡張すること。

--------------------------------------------------------------------------
使い方:

    1) CSVから一括生成
        python3 scripts/import_songs.py csv path/to/songs.csv

       CSVのヘッダー例（この順である必要はなく、名前で拾う）:
         title,artist,release_date,bpm,key,genre,status,spotify_url,youtube_url,apple_music_url

    2) URLから1曲だけ生成
        python3 scripts/import_songs.py url "https://open.spotify.com/track/xxxxx"

    3) 曲名のみから空テンプレを生成（メタデータなし・手動編集前提）
        python3 scripts/import_songs.py title "曲名" "アーティスト名"

必要ライブラリ:
    - Python 3.8+
    - requests (pip install requests)  ※標準ライブラリのみでも動作するようフォールバックあり

出力:
    01_Songs/<サニタイズされた曲名>.md  にMarkdownノートを生成する。
    既に同名ファイルが存在する場合は上書きせず、末尾に連番を付与する。
--------------------------------------------------------------------------
"""

import csv
import re
import sys
import html
import urllib.request
from pathlib import Path
from datetime import date

try:
    import requests
    HAS_REQUESTS = True
except ImportError:
    HAS_REQUESTS = False

SCRIPT_DIR = Path(__file__).resolve().parent
VAULT_ROOT = SCRIPT_DIR.parent
SONGS_DIR = VAULT_ROOT / "01_Songs"
TEMPLATE_PATH = VAULT_ROOT / "04_Templates" / "Song_Template.md"


def sanitize_filename(name: str) -> str:
    """ファイル名として安全な文字列に変換する"""
    name = name.strip()
    name = re.sub(r'[\\/:*?"<>|]', "_", name)
    return name or "Untitled"


def unique_path(dir_path: Path, filename: str) -> Path:
    """同名ファイルがあれば連番を付けて衝突を回避する"""
    candidate = dir_path / f"{filename}.md"
    if not candidate.exists():
        return candidate
    i = 2
    while (dir_path / f"{filename}_{i}.md").exists():
        i += 1
    return dir_path / f"{filename}_{i}.md"


def build_song_markdown(data: dict) -> str:
    """曲データ(dict)からSong_Templateに沿ったMarkdown文字列を生成する"""
    title = data.get("title", "")
    artist = data.get("artist", "")
    release_date = data.get("release_date", "")
    bpm = data.get("bpm", "0")
    key = data.get("key", "")
    genre = data.get("genre", "")
    status = data.get("status", "Released")
    spotify_url = data.get("spotify_url", "")
    youtube_url = data.get("youtube_url", "")
    apple_music_url = data.get("apple_music_url", "")
    daw_project_path = data.get("daw_project_path", "")
    story = data.get("story", "")
    cover_art = data.get("cover_art_url", "")

    cover_line = f"\n![cover]({cover_art})\n" if cover_art else ""

    return f"""---
title: "{title}"
artist: "{artist}"
release_date: {release_date or "YYYY-MM-DD"}
bpm: {bpm or 0}
key: "{key}"
genre: "{genre}"
status: "{status}" # Released / WIP / Demo / Idea
spotify_url: "{spotify_url}"
youtube_url: "{youtube_url}"
apple_music_url: "{apple_music_url}"
daw_project_path: "{daw_project_path}"
tags: [music/song]
---

# 楽曲概要
{cover_line}
<!-- この曲が何であるか、一言で。制作の背景・コンセプトなど -->

## 配信・外部リンク

- Spotify: {spotify_url}
- YouTube: {youtube_url}
- Apple Music: {apple_music_url}

## 歌詞 & 楽曲構成

```
[Intro]


[Verse 1]


[Pre-Chorus]


[Chorus]


[Verse 2]


[Chorus]


[Bridge]


[Chorus]


[Outro]

```

## 音楽的仕様（コード進行・使用シンセ/プラグイン）

- コード進行:
- 使用シンセ/音源:
- 使用プラグイン（EQ/Comp/FXなど）:
- その他の技術メモ:

## 制作エピソード・ストーリー（AIプロモーション文生成用の文脈メモ）

{story or "<!-- なぜこの曲を作ったか、誰に向けたか、制作中のエピソードなど。SNS投稿文やプロモーション文をAIに生成させる際の文脈として使う。 -->"}
"""


def write_song_note(data: dict) -> Path:
    SONGS_DIR.mkdir(parents=True, exist_ok=True)
    filename = sanitize_filename(data.get("title") or "Untitled")
    out_path = unique_path(SONGS_DIR, filename)
    out_path.write_text(build_song_markdown(data), encoding="utf-8")
    return out_path


# ---------------------------------------------------------------------
# 機能1: CSV一括取り込み
# ---------------------------------------------------------------------

def import_from_csv(csv_path: str) -> None:
    path = Path(csv_path)
    if not path.exists():
        print(f"[ERROR] CSVファイルが見つかりません: {csv_path}")
        sys.exit(1)

    with path.open(encoding="utf-8-sig", newline="") as f:
        reader = csv.DictReader(f)
        count = 0
        for row in reader:
            # 列名の揺れを軽く吸収（前後空白除去・小文字化はしない＝日本語ヘッダー対応のため）
            normalized = {k.strip(): (v.strip() if isinstance(v, str) else v) for k, v in row.items()}
            data = {
                "title": normalized.get("title") or normalized.get("曲名", ""),
                "artist": normalized.get("artist") or normalized.get("アーティスト", ""),
                "release_date": normalized.get("release_date") or normalized.get("リリース日", ""),
                "bpm": normalized.get("bpm") or normalized.get("BPM", "0"),
                "key": normalized.get("key") or normalized.get("Key", ""),
                "genre": normalized.get("genre") or normalized.get("ジャンル", ""),
                "status": normalized.get("status") or normalized.get("ステータス", "Released"),
                "spotify_url": normalized.get("spotify_url") or normalized.get("SpotifyURL", ""),
                "youtube_url": normalized.get("youtube_url") or normalized.get("YouTubeURL", ""),
                "apple_music_url": normalized.get("apple_music_url") or normalized.get("AppleMusicURL", ""),
                "daw_project_path": normalized.get("daw_project_path", ""),
            }
            if not data["title"]:
                continue
            out_path = write_song_note(data)
            print(f"  作成: {out_path.relative_to(VAULT_ROOT)}")
            count += 1

    print(f"\n[DONE] {count}件のノートを 01_Songs/ に生成しました。")


# ---------------------------------------------------------------------
# 機能2: URLからの取り込み（OGPタグの軽量スクレイピング・モック実装）
# ---------------------------------------------------------------------

def fetch_html(url: str) -> str:
    """requestsがあればそれを、なければurllibで取得する"""
    headers = {"User-Agent": "Mozilla/5.0 (compatible; ObsidianMusicLab/0.1)"}
    if HAS_REQUESTS:
        resp = requests.get(url, headers=headers, timeout=10)
        resp.raise_for_status()
        return resp.text
    else:
        req = urllib.request.Request(url, headers=headers)
        with urllib.request.urlopen(req, timeout=10) as resp:
            return resp.read().decode("utf-8", errors="ignore")


def extract_og_tag(html_text: str, prop: str) -> str:
    """<meta property="og:xxx" content="..."> を正規表現で抜き出す簡易パーサー"""
    pattern = rf'<meta[^>]+property=["\']og:{prop}["\'][^>]+content=["\']([^"\']*)["\']'
    m = re.search(pattern, html_text, re.IGNORECASE)
    if not m:
        # content と property の順序が逆のパターンにもフォールバック
        pattern2 = rf'<meta[^>]+content=["\']([^"\']*)["\'][^>]+property=["\']og:{prop}["\']'
        m = re.search(pattern2, html_text, re.IGNORECASE)
    return html.unescape(m.group(1)) if m else ""


def import_from_url(url: str) -> None:
    print(f"[INFO] URLからメタデータを取得中: {url}")
    print("[NOTE] これはOGPタグの軽量スクレイピングによる基本実装です。")
    print("       Spotify/YouTube公式APIほどの精度は保証されません。取得後は内容を確認してください。")

    try:
        html_text = fetch_html(url)
    except Exception as e:
        print(f"[ERROR] 取得に失敗しました: {e}")
        print("        ネットワーク接続、またはURLの有効性を確認してください。")
        print("        手動でSong_Templateをコピーして入力することもできます。")
        return

    title = extract_og_tag(html_text, "title") or "Untitled"
    site_name = extract_og_tag(html_text, "site_name")
    cover_art = extract_og_tag(html_text, "image")

    data = {
        "title": title,
        "artist": "",
        "status": "Released",
        "spotify_url": url if "spotify" in url else "",
        "youtube_url": url if "youtube" in url or "youtu.be" in url else "",
        "apple_music_url": url if "music.apple" in url else "",
        "cover_art_url": cover_art,
        "story": f"<!-- 取得元: {site_name or url} -->",
    }

    out_path = write_song_note(data)
    print(f"\n[DONE] 作成: {out_path.relative_to(VAULT_ROOT)}")
    print("       フロントマターとメタデータは自動取得のため、内容を必ず確認・補完してください。")


# ---------------------------------------------------------------------
# 機能3(補助): 曲名のみから空テンプレを生成
# ---------------------------------------------------------------------

def import_from_title(title: str, artist: str = "") -> None:
    data = {
        "title": title,
        "artist": artist,
        "status": "Idea",
        "release_date": str(date.today()),
    }
    out_path = write_song_note(data)
    print(f"[DONE] 作成: {out_path.relative_to(VAULT_ROOT)}")


def print_usage():
    print(__doc__)


def main():
    if len(sys.argv) < 2:
        print_usage()
        sys.exit(1)

    mode = sys.argv[1]

    if mode == "csv":
        if len(sys.argv) < 3:
            print("[ERROR] CSVファイルのパスを指定してください。")
            print("使い方: python3 scripts/import_songs.py csv path/to/songs.csv")
            sys.exit(1)
        import_from_csv(sys.argv[2])

    elif mode == "url":
        if len(sys.argv) < 3:
            print("[ERROR] URLを指定してください。")
            print('使い方: python3 scripts/import_songs.py url "https://open.spotify.com/track/xxxxx"')
            sys.exit(1)
        import_from_url(sys.argv[2])

    elif mode == "title":
        if len(sys.argv) < 3:
            print("[ERROR] 曲名を指定してください。")
            print('使い方: python3 scripts/import_songs.py title "曲名" "アーティスト名"')
            sys.exit(1)
        artist = sys.argv[3] if len(sys.argv) > 3 else ""
        import_from_title(sys.argv[2], artist)

    else:
        print(f"[ERROR] 不明なモード: {mode}")
        print_usage()
        sys.exit(1)


if __name__ == "__main__":
    main()
