# Obsidian & AI Music Lab Starter Kit (v0.1.0)

## 概要

過去の作品をアーカイブし、AI（**Suno AI** / **Claude** / **Gemini**）と共作するための、オープンソースの Obsidian Vault 環境です。

アーティストが自分の楽曲・アイデア・リファレンスを一元管理し、AIとの共作（作詞・アレンジ・プロモーション文生成など）を効率化することを目的としています。

## フォルダ構造

```
.
├── 01_Songs/          # 完成曲・配信済み楽曲・制作中デモのカルテ
├── 02_Seeds/          # 歌詞の断片、アイデアメモ、ボイスメモリンク
├── 03_References/     # リファレンス曲の分析、影響を受けた作品
├── 04_Templates/      # 各種テンプレート
│   ├── Song_Template.md
│   └── Seed_Template.md
├── 05_Archives/       # ボツ案・過去プロジェクト
├── scripts/           # 既存楽曲の一括取り込み用スクリプト
│   ├── import_songs.py
│   └── sample_songs.csv
├── CLAUDE.md          # Claude Codeがこのvaultで従うルール
└── README.md
```

## 使い方

### 1. Obsidianでの本フォルダの開き方

1. Obsidianを起動し、「Open folder as vault」を選択。
2. このリポジトリのルートディレクトリ（`Ai-Music-Assistant/`）を指定して開く。
3. 初回起動時、`01_Songs` などのフォルダがサイドバーに表示されればOK。

### 2. 既存作品の取り込み方法

#### (A) 手動入力

1. `04_Templates/Song_Template.md` を `01_Songs/` にコピーする。
2. ファイル名を曲名にリネームする。
3. フロントマターと各見出し（配信リンク、歌詞・構成、音楽的仕様、制作エピソード）を埋める。

#### (B) スクリプトによる自動生成

`scripts/import_songs.py` を使うと、複数曲をまとめて、または1曲をURLから取り込めます。

```bash
# 必要ライブラリのインストール（requestsのみ。標準ライブラリのみでも動作します）
pip install requests

# 1) CSVから一括生成
#    CSVヘッダー例: title,artist,release_date,bpm,key,genre,status,spotify_url,youtube_url,apple_music_url
python3 scripts/import_songs.py csv scripts/sample_songs.csv

# 2) 公開URL（Spotify/YouTube等）から1曲分の基本メタデータを取得して生成
python3 scripts/import_songs.py url "https://open.spotify.com/track/xxxxx"

# 3) 曲名のみから空のカルテを生成（アイデア段階の曲用）
python3 scripts/import_songs.py title "曲名" "アーティスト名"
```

生成されたノートは `01_Songs/` に保存されます。自動取得した内容は下書きのため、**必ず内容を確認・補完してください**。

### 3. Suno AI × Claude / Gemini を使った作詞・楽曲制作ワークフロー

1. **アイデアを蓄積する**: 思いついたフレーズ・コード進行・ボイスメモは `02_Seeds/` に `Seed_Template.md` を複製して記録する。
2. **AIで展開する**: SeedノートやSongノートの「制作エピソード」を文脈として渡し、ClaudeまたはGeminiに歌詞・構成案を作らせる。
   - 依頼時は「Suno AI用に `[Verse]` `[Chorus]` 等の構成タグを付けて」と明示する（`CLAUDE.md` にもルールとして記載済み）。
3. **Suno AIに投入する**: 生成された歌詞ブロックをそのままコピーし、Suno AIのプロンプト欄に貼り付けて楽曲を生成する。
4. **結果をVaultに戻す**: 気に入った生成結果は `01_Songs/` のノートに追記し、`status` を `Demo` や `WIP` に更新する。リリースしたら `Released` に変更し、配信リンクを埋める。
5. **プロモーション文の生成**: リリース済み曲の「制作エピソード・ストーリー」セクションを文脈として、Claude/GeminiにSNS投稿文やプロモーション文を生成させる。

## ライセンス

MIT License
