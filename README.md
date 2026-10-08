# Obsidian & AI Music Lab Starter Kit (v0.1.0)

## 概要

過去の作品をアーカイブし、AI（**Suno AI** / **Claude** / **Gemini**）と共作するための、オープンソースの Obsidian Vault 環境です。

アーティストが自分の楽曲・アイデア・リファレンスを一元管理し、AIとの共作（作詞・アレンジ・プロモーション文生成など）を効率化することを目的としています。

> **公開範囲について**: MITライセンスで公開しているのは `04_Templates/` や `scripts/` などVaultの「枠組み」のみです。`01_Songs/` `02_Seeds/` `03_References/` `05_Archives/` に入れる歌詞・アイデア・作品本体は各アーティストの個人情報に準じるものとして扱い、`.gitignore` によりGit管理・コミットの対象から除外しています。

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

### 4. 歌詞動画をつくる（`scripts/lyric_video/`）

曲ノートの歌詞・音源・背景画像から、歌詞が動いて意味を伝える縦型（1080×1920 / 30fps）の
歌詞動画を作ります。歌詞のタイミングは音源の文字起こし（faster-whisper）と
曲ノートの歌詞を突き合わせて自動で決め、あとから GUI で直せます。

```bash
# 専用の仮想環境を作る（他プロジェクトのvenvとは分ける）
python3 -m venv scripts/lyric_video/.venv
scripts/lyric_video/.venv/bin/pip install -r scripts/lyric_video/requirements.txt
```

**書き出し（CLI）**

```bash
scripts/lyric_video/.venv/bin/python scripts/lyric_video/make_lyric_video.py \
  --song "01_Songs/<曲名>.md" --audio <音源> --image <背景画像> \
  --out scripts/lyric_video/_work/<曲名>/output.mp4
```

- `--stills <DIR>` で、書き出す前に各カットの静止画一覧を出して読みにくさを確認できます
- カット設計は `_work/<音源ハッシュ>/kinetic_plan.json` に残り、手で直せます（`--replan` で作り直し）
- 曲ノートの `genre` と構成タグ（`[Chorus]` 等）が、使う技法と強弱の選び方を決めます

**タイミングの調整（GUI）**

```bash
scripts/lyric_video/.venv/bin/python scripts/lyric_video/timing_gui.py \
  --audio <音源> --image <背景画像> --song "01_Songs/<曲名>.md"
```

波形上のドラッグ、再生しながらの `T` タップ入力、歌詞の抜け・漏れの一覧、
描画のプレビュー、部分書き出しができます。ブラウザから使うローカル専用のツールで、
`127.0.0.1` のみを待ち受けます。

**ボーカル分離（任意）**

歌が演奏に埋もれて文字起こしが拾えない曲（実行時の `一括照合: N/M行` の N が小さいとき）で試してください。
文字起こしだけをボーカル分離音声で行えます。唸り声（グロウル）の曲では、分離なしのほうが良いことがあります
（テストでは英語のグロウル曲が、分離ありで 19/54行、分離なしで 52/54行でした）。両方試して照合行数を比べてください。
[demucs](https://github.com/facebookresearch/demucs) を使います。

```bash
# 任意導入（requirements.txt には入れていません。torch が入り数百MBになります）
scripts/lyric_video/.venv/bin/pip install demucs
```

demucs は音声の読み書きに `ffmpeg` を使うため、`ffmpeg` が PATH 上にある必要があります。

- CLI：`make_lyric_video.py ... --separate-vocals`、または `scripts/lyric_video/.venv/bin/python scripts/lyric_video/align.py <音源> "01_Songs/<曲名>.md" --separate-vocals`
- GUI：「自動タイミング」の横の「ボーカル分離」にチェック。起動時に `--separate-vocals` を付けると初期ONになります
- 初回は htdemucs のモデルが自動でダウンロードされ、所要時間は、実測で 4〜5分の曲が約2分強でした（CPU、2026-10-05、1環境での値。環境によって変わります）。demucs が無い・失敗したときは、元音源に切り替えずエラーで止まります
- 分離音声は `_work/<音源ハッシュ>/vocals.htdemucs.flac`、その文字起こしは `whisper_words.vocals*.json` に残ります（`_work/` は Git に入りません）。分離をやり直すときは `vocals.htdemucs.flac` を消してください。`--no-cache` でも分離音声は作り直しません
- ボーカル分離を指定したときは、日本語でも最長共通部分列で照合します。同じブロックを繰り返す曲でも、順番どおりに照合されます（分離なしの日本語は従来の方式のまま）
- 分離音声は文字起こしだけに使います。動画の音声・ビート検出・ショートは元音源のままです
- 手直し済みのタイミングがある曲で分離の指定を変えても、手直しは残して警告を出します。作り直すときは GUI の「作り直す」を使ってください

**ショート動画（TikTok / YouTube ショート）**

15〜60秒の切り抜き区間を自動で選びます。行の途中では切らず、サビの頭から始まり、
まとまりの切れ目で終わる区間を優先します。

```bash
# 候補の一覧を見る → 書き出す（1本 / 複数 / 全部）
... --shorts
... --short 1        # --short 1,3 / --short all / --short-sec 15
```

**曲ごとの見た目と演出（任意）**

書体・配色の組は `scripts/lyric_video/looks/<名前>.json`（テーマ。Git に入る。曲を特定しない値だけ）、
行ごとの演出は `_work/<音源ハッシュ>/direction.json`（Git に入らない。行番号で書き、歌詞の本文は持たない）に書きます。
direction は `make_lyric_video.py ... --direction-from <ファイル>` で登録し（置く前に検査し、止まったら登録しません）、`--stills <DIR>` で静止画一覧を見てから書き出します。
direction のある曲は、`kinetic_plan.json` を手で直さず、direction を直して `--replan` します。
書き間違い（未知の項目・存在しない名前）は、黙って既定値に戻さず止まります。

```jsonc
{
  "n_lines": 24,                       // alignment の行数と違ったら止まる
  "look": "<テーマ名>",
  "voices": {"<声>": {"tail": 0.3}},   // 声の名前は任意の文字列
  "lines": {
    "1": {"voice": "<声>", "role": "<役>", "palette": "<配色>", "entrance": "karaoke"},
    "5-6": {"entrance": "cut", "break_after": {"0": 5}},          // 段 0 の 5 字目の後で改行
    "9": {"entrance": "slam", "impact": "s", "land": "first_word",
          "counter": {"enter": "push"}},
    "12": {"accent": "glow", "counter": "hide"},                  // hide / resume / off
    "20": {"solo": true, "max_px": 150, "min_px": 120}
  },
  "impacts": {"s": {"overshoot": 1.5, "land_frames": 3, "undershoot": 0.94,
                    "glyph_shake_px": 2, "zoom": 0.03, "screen_shake_px": 1}},  // 全キー必須
  "slam_voice": "<声>",
  "karaoke": {"unlit_opacity": 0.65, "light_frames": 3, "keyword_unlit": "text",
              "min_match": 0.6, "min_cover": 0.6},
  "counter": {"voices": ["<声>"],      // カウンターが出てよい声（それ以外の声の行では出ない）
              "appear": [{"after_line": 10, "until_line": 11, "at_fraction": 0.5, "count": 1, "rate": "per_onset"}]},
  "interludes": [
    {"after_line": 10, "until_line": 11, "kind": "duotone", "zoom_peak": 1.08},
    {"after_line": 24, "until": "end", "kind": "duotone", "zoom_ramp": {"to": 1.02, "seconds": 8.0}}
  ]
}
```

- **`impacts` / `slam`**：叩きつけの衝撃を段階（名前）で定義し、行に `impact` で当てます。`slam` の行は `slam_voice` の声だけ、Chorus・Bridge の区分には使えず、全体で 8 行まで。入りの1フレーム目から不透明度 1.0。段階に任意の `ease`（`linear`＝既定／`quad`＝二乗の減速）で縮み方を選べます。`land: "start"` は入りの頭（縮みの出だし）を、行の開始以降で最初に描かれる 30fps のフレームに置きます（着地 ＝ そのフレームの時刻 ＋ `land_frames` ÷ 30。行の開始から最大1フレーム遅れ、描かれるフレームの出だしは必ず `overshoot` の値になります）
- **入れ替わり**：次の行が見え始める（文字の不透明度が 0.5 以上になる最初のフレーム）まで前の行を残します（direction のある曲だけ）。残した間は、前の行が最後に描かれた状態のまま止まり、背景の配色・カウンターの割れの始まりも、次の行が実際に描かれる最初のフレームで替わります。`karaoke` の入替の行（前の行の表示が次の行の開始まで続く行）は、0.15 秒かけず 0 フレームで未点灯の濃さで出ます
- **カウンターの数え方**：行の単語（`rate`）は、その行の「開始から表示の終わりまで」に始まる単語だけ数えます（次の行へ入れ替わる行は次の行の開始まで）。行の後ろの隙間にある単語では増えません。単語の無い区間（`counter.appear` の後）はオンセットごとに +1 です
- **`karaoke`**：単語時刻で 1 字ずつ点灯します。照合が足りない行は全文点灯に落とし、警告と `kinetic_plan.md` の印を出します。未点灯の濃さは配色の組の `unlit_opacity` でも上書きできます
- **`break_after`**：行ごとの改行位置（`{"<段番号>": <字の位置>}`、段は 0 始まり）。単語の途中なら警告
- **`accent: glow`**：文字の後ろの淡い光（テーマの `parts.glow`。明滅しない）
- **`counter`**：歌詞を使わない増える数字（テーマの `parts.counter`）。行の値は `hide`（行の開始の 0.3 秒前から薄く消え、開始で 0）／`resume`（行の開始で 0 フレームで戻る。値は続き）／`off`／辞書（`count`・`enter: push`・`rate`・`state`・`break`）。数字は単語の開始ごと（`per_word_x2` は +2）、`appear` の区間は beats のオンセットごとに増えます。`break` は全バッジを 4 片に割ります（文字は割らない）
- **`interludes`**：指定した区間だけ、背景の補間に追従する 2 色刷り（走査線なし）と、画面全体の寄り引きを足します。区間は時刻（`start`／`end`）か行（`after_line`／`until_line`、曲末は `until: "end"`）で書きます
- **`solo`**：その行の表示中は、カウンター・間奏・背景の補間・画面の寄り／揺れがないことを検査します（あれば止まる）

**止まる条件**（黙って直さず、行番号と数値を出して止まります）：文字の左右の余白が 92px を割る／役の下限を割る／`slam` の寄りが外接矩形に収まらない／
`break_after` が範囲外／鍵語の行の表示中にカウンターが見える／割れの落ち切りが次の鍵語の行の開始より後／外の声でない行にカウンターが出る／
差し色どうし（カウンターの色と鍵語の色）が同じフレームに出る（30fps の格子で両方が出るフレームが1枚でもあれば。1フレーム未満の重なりは許す）／叩く行の入りの3フレーム目以降の余白が左右 92px・上下 70px を割る／
表示区間の中に文字が1つも描かれないフレームがある／`counter.appear` の区間が空（`until_line` が `after_line` より後でない）・`at` が最後の行の開始より後／カウンターの比が最悪の背景で 4.5:1 を割る／`solo` の行の表示中に他の動きがある。
**`hold: carry`（行全体を一定の速さで動かす）と `stack`（前の列を残して薄くする）の止まる条件**：
carry＝テーマを使う曲だけ／`carry: {px_s, dir: down|up}`（縦組みの行は段ごとの書き方も可）／動いた後の読み字が上下 92px（`CARRY_MARGIN`）に収まらない／`entrance` が mask・stamp・slash、`accent: glow`、自動の構図 grid（4字・1段の行は `layout` を書かないと grid になる）と併用。
92px は計画の指定で、左右の余白（92px）に揃えた値です。`slam` など他の上下の検査（70px）より厳しい安全側で、画面の上下の操作・説明文の帯から遠ざける狙いですが、**帯に入らないことの実測はありません（要検証）**。
stack＝テーマを使う曲だけ／積む行はすべて縦組みで `exit` は swap・次の行の開始まで表示（`tail` か `end`）／`clear_at_line` は最後の行の次の行だけ／残した列の比が最悪の背景で 4.5:1 を割る（`dim` は 0.05 刻みに丸めて検査）／積んだ列が左右 92px に収まらない。
**点の層・行の項目の追加（direction）**：
`track` の時刻は `at`（区間の頭からの秒）／`at_line: N` ＋ 任意の `offset`（行 N の開始からの秒）／`at_time`（曲の頭からの秒）のどれか1つ（秒に直した後で昇順でなければ止まる。増減の速さの規則も秒に直した後に掛かる）。
`tate_line` の `count_fade: "linear"` は、本数の変化に付いて線がゆっくり薄く・濃くなる（既定 `"rank"`）。
行の `accent: "rows"` ＋ `accent_rows: [段番号, …]`（0 始まり。縦組みは列）はその段だけ差し色。`row_roles: {"<段番号>": "<役>"}` は縦組みの行だけ、列ごとに書体の役を変える（字の大きさの上限・下限は行の役と段の役の厳しい方）。ただし行に `max_px`／`min_px` を書くと、段の役の範囲より先にその行の指定が効き、段の役の上限・下限を超えられます。
行の `row_lengths: [4, 3]`（横組みの行だけ）は、自動の段を捨てて字数どおりの段にします（合計が行の字数と違う・`break_after` との併記・縦組みの行・空白（半角・全角）を含む行・欧文の行は止まる）。
テーマの最上位 `points_color`（`text`＝既定・`sub`・`accent`。全部の配色にそのキーが必要）は点の層の色。行の `clear_cap`（0.05〜0.25、既定 0.25）は読み字の周りの点の被覆の上限。読み字の比の検査は、本文色・karaoke の未点灯の色・差し色のすべてを、点を `clear_cap` で重ねた最悪の地に対して見て、4.5 を割ると止まります。
`row_lengths` は要素1つ（`[7]`＝切らずに1段）も書けます。縦組みの横幅はテーマの `layout.text_width` に従います（無ければ 896）。direction の最上位 `safe_area: {"left", "right", "top", "bottom", "corner": {"y_from", "x_max"}}`（値は direction に書く）を書くと、全フレームで、carry・踏み込み・画面の寄りを当てた後の読み字の外接矩形が枠に入るか検査し、はみ出すと止まります（入りの最初の2フレームを除く。carry の上下 92px の検査はこの検査に置き換わる）。`--stills --safe-overlay` で静止画にだけ枠を重ねます（mp4 には描かない）。`safe_area` はテーマを使う曲だけ・値は有限の数（null・NaN・inf は止まる）。`--safe-overlay` は `--stills` なしだと止まります。**未対応（関門②の寄りを入れる前に直す）：**`stack` で残した列は、後の行の時刻の画面の寄りでは `safe_area` の検査を受けません（今の正本は行20〜23 に寄りが無いので影響なし）。
**注意（row_roles）：**段の役の `thicken`（太らせ）は使われず、行の役の値が全部の列に掛かります。`accent: rows` の行の段が描くとき（幅・下限 px）に増えると、段番号がずれるので止まります（direction のある曲）。
`shape` の `from: "line"` で区間に重なる行が無いときの出発点は、直前の行の読み字の中心。
静止画一覧の出力先には、動きの静止画（`sheet_motion_NN.png`）が増え、`_work/<ハッシュ>/look_report.md` に行ごとのコントラスト比と差し色の出る区間が出ます。

**背景素材**

画像でも動画でも使えます。用途（Verse／サビ／囁き／間奏／どこでも）を付けて複数登録でき、
カットの強さに合うものが順番に使われます。GUI の「背景素材」から追加するか、CLI なら
`--bg <ファイル>:<用途>` を繰り返し指定します。縦画面に対して横長の画像を渡すと、
カメラが大きく動けるぶん見栄えがします。

手持ちの素材が無ければ、生成して用意する方法もあります。たとえば
[ElevenLabs](https://try.elevenlabs.io/j4tv8xnnfqid) では、縦（9:16）の画像・短い動画・効果音を
テキストから作れます（※ 紹介リンクです）。曲ノートの歌詞や制作エピソードを
そのままプロンプトの下地にすると、曲に沿った背景がそろいます。

## ライセンス

MIT License
