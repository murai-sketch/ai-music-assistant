# CLAUDE.md

このファイルは、Claude Code (claude.ai/code) がこのリポジトリで作業する際のガイドです。

## このVaultの役割

このObsidian Vaultは、アーティストの**音楽制作における第二の脳（Second Brain）**です。
過去作品のアーカイブと、AI（Suno AI / Claude / Gemini）との共作ワークフローを一元化することを目的としています。

- `01_Songs/` — 完成曲・配信済み楽曲・制作中デモの「カルテ」。1曲1ノート。
- `02_Seeds/` — 歌詞の断片、アイデアメモ、ボイスメモへのリンクなど、まだ曲になっていない種。
- `03_References/` — リファレンス曲の分析、影響を受けた作品のメモ。
- `04_Templates/` — 各種テンプレート（新規ノート作成時はここから複製する）。
- `05_Archives/` — ボツ案・過去プロジェクトの保管庫。
- `scripts/` — 既存楽曲の一括取り込み・自動化スクリプト。

## Claude Codeが実行すべきルール

### 1. 新しい楽曲ノートを作成する際

- 必ず `04_Templates/Song_Template.md` をベースにすること。独自にフロントマターを作らない。
- フロントマターは以下のスキーマを厳守する:
  ```yaml
  title: ""
  artist: ""
  release_date: YYYY-MM-DD
  bpm: 0
  key: ""
  genre: ""
  status: "Released" # Released / WIP / Demo / Idea
  spotify_url: ""
  youtube_url: ""
  apple_music_url: ""
  daw_project_path: ""
  tags: [music/song]
  ```
- ファイルは `01_Songs/<曲名>.md` として保存する。
- `status` は必ず `Released` / `WIP` / `Demo` / `Idea` のいずれかにする。

### 2. アイデア・断片（Seed）ノートを作成する際

- `04_Templates/Seed_Template.md` をベースにすること。
- `status` は `Unused` / `In_Use` のいずれか。
- 関連する楽曲ノートがある場合は、必ず `[[01_Songs/曲名]]` の形式でリンクする。

### 3. 作詞・作曲を依頼された際（Suno AI連携）

- 歌詞を生成・編集する際は、**必ずSuno AI用の構成タグ**を付与すること:
  `[Intro]` `[Verse]` `[Pre-Chorus]` `[Chorus]` `[Bridge]` `[Outro]` など。
- タグはコードブロック（\`\`\`）内に記述し、Suno AIにそのままコピー&ペーストできる形にする。
- 曲の雰囲気・BPM・ジャンルなど、既存の楽曲ノートのフロントマターや「制作エピソード」セクションの文脈を踏まえて生成すること。

### 4. 既存作品を取り込む際

- 単発の追加は `04_Templates/Song_Template.md` を手動で複製する。
- 複数曲をまとめて取り込む場合は `scripts/import_songs.py` を使う（CSV一括取り込み、またはURLからの基本メタデータ取得）。
- スクリプトの出力はあくまで下書きなので、生成後にフロントマターと本文の内容を確認・補完すること。

### 5. プロモーション文・SNS投稿文を生成する際

- 対象楽曲ノートの「制作エピソード・ストーリー」セクションを主要な文脈情報として使うこと。
- リンクは各ノートの `spotify_url` / `youtube_url` / `apple_music_url` から取得し、勝手に生成しない。

### 6. Git管理の範囲（重要）

このリポジトリはMITライセンスで公開する「Vaultの枠組み」（`04_Templates/`, `scripts/`, ルート直下のドキュメント類）のみを対象とする。

- **`01_Songs/`・`02_Seeds/`・`03_References/`・`05_Archives/` の中身（歌詞・アイデア・作品本体）は絶対にGitにコミットしない。** アーティスト個人の情報に準じる機微なデータとして扱うこと。
- これらのフォルダは `.gitignore` で中身を除外し、`.gitkeep` のみ追跡している。新しいノートを作成しても `git add` の対象にしないこと。
- `git add -A` や `git add .` など、除外設定を無視しかねない広範なステージングコマンドは使わない。個別にファイルを指定するか、`git status` で除外が効いているか必ず確認してから実行する。
- 万一すでにコミット・pushされてしまった場合は、単なる取り消しコミットでは履歴に残り続けるため、履歴の書き換え（`git reset` + force push、または `git filter-repo`）が必要になることをユーザーに伝える。

### 7. 一般的な注意事項

- `.obsidian/` 配下のワークスペース設定ファイルは編集・コミット対象外（`.gitignore`参照）。
- 音声ファイル（wav/mp3/m4a/flac）やDAWプロジェクトのバックアップはVaultに直接コミットしない。パスやリンクのみ記録する。
- 既存ノートのフロントマター構造を変更する場合は、他の全ノートとの一貫性を保つこと。
