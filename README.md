# shiori

Discord 通話の録音 (Craig) を Whisper で文字起こしし、Claude Code に要約させて Notion に投稿するパイプライン。

```
Craig (録音) ─► Groq Whisper (文字起こし) ─► Claude Code (要約) ─► Notion (投稿)
```

## 前提

- [uv](https://github.com/astral-sh/uv) (`brew install uv` または `nix-shell -p uv`)
- `ffmpeg` / `ffprobe` が PATH 上にあること
- `claude` CLI (Claude Code) が PATH 上にあること
- Notion インテグレーションを親ページ/DB に「招待」済みであること

依存は `pipeline.py` 冒頭の [PEP 723](https://peps.python.org/pep-0723/) インラインメタデータに宣言済み。`uv run` が初回に自動で揃えます (個別 `pip install` 不要)。

## セットアップ

```bash
# .env.example をコピーして値を埋める
cp .env.example .env
$EDITOR .env
```

`.env` の中身:

```
GROQ_API_KEY=gsk_xxx
NOTION_API_KEY=secret_xxx
NOTION_PARENT_ID=xxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxx
# 親が DB のとき
# NOTION_PARENT_TYPE=database
```

`pipeline.py` はスクリプトと同階層の `.env` を自動で読み込みます。既に `export` 済みの環境変数は `.env` で上書きしません。

## 使い方

入力フォーマットによって動作モードが切り替わります。

| 入力 | モード | 話者分離 | コスト |
|---|---|---|---|
| `.flac` / `.m4a` 等 単一音声 (mix) | single | なし | 1 トラック分 |
| **Craig マルチトラック ZIP** | multitrack | あり (`話者: 発言`) | トラック数 × 録音長 |

録音ファイルは `input/` に置き、文字起こし/要約を保存したい場合は `output/` を使う運用を推奨 (中身は `.gitignore` 済み)。

### single (mix) モード

```bash
uv run pipeline.py input/craig-xxx.flac \
  --title "週次定例 2026-05-24" \
  --save-transcript output/2026-05-24.transcript.txt \
  --save-summary    output/2026-05-24.summary.md
```

### multitrack モード (推奨: 話者付き議事録)

Craig のダウンロードページから **multi-track ZIP** をそのまま渡します。トラック名 (`1-shuya.flac` 等) から話者名を取得し、**各トラックを丸ごと Whisper の `verbose_json` に投げてセグメント単位のタイムスタンプを取得**します。

- リクエスト数 = トラック数 (25MB を超えるトラックだけ時間分割)
- Groq の **20 RPM 制限**と**最低 10 秒課金**にひっかからない
- Whisper が返す `no_speech_prob > 0.6` のセグメントを捨てて無音区間の幻聴を抑制

```bash
uv run pipeline.py input/craig-xxx.zip --title "週次定例"
```

文字起こし出力例:

```
[00:00:05] shuya: えーと、今日は週次定例ですね
[00:00:08] alice: はい、よろしくお願いします
[00:00:12] bob:   アジェンダを確認します
```

### その他

```bash
# モード強制 (自動判定を上書き)
uv run pipeline.py input.zip --mode single
uv run pipeline.py input.flac --mode multitrack   # 通常エラーになる

# Notion 投稿せず結果だけ確認
uv run pipeline.py recording.flac --skip-notion

# Notion 投稿だけリトライ (Whisper/Claude を再実行しない)
uv run pipeline.py --post-only \
  --summary-file    output/2026-05-24.summary.md \
  --transcript-file output/2026-05-24.transcript.txt \
  --title "Discord通話 2026-05-24"

# shebang 経由 (実行ビットを立てた場合)
chmod +x pipeline.py
./pipeline.py recording.flac
```

## オプション

| フラグ | 説明 |
|---|---|
| `--title` | Notion ページタイトル (省略時は日時) |
| `--language` | Whisper 言語コード (既定 `ja`、空文字で自動判定) |
| `--parent-type` | `page` / `database` (既定 `page`) |
| `--claude-bin` | `claude` CLI のパス |
| `--skip-notion` | Notion 投稿をスキップ |
| `--save-transcript PATH` | 文字起こしをファイル保存 |
| `--save-summary PATH` | 要約 Markdown をファイル保存 |
| `--keep-workdir` | 中間ファイルの作業ディレクトリを残す |
| `--mode {auto,single,multitrack}` | 動作モード強制 (既定: auto) |
| `--parent-type {auto,page,database}` | Notion 親種別 (既定: auto = API 自動判定) |
| `--post-only` | Whisper/Claude をスキップして既存ファイルから Notion 投稿のみ |
| `--summary-file PATH` | `--post-only` 時に読む要約 Markdown |
| `--transcript-file PATH` | `--post-only` 時に読む文字起こし (任意) |
| `--property NAME=VALUE` | DB の追加プロパティ。複数回指定可。multi_select は `,` 区切り。例: `--property 'カテゴリー=議事録,定例'` |

## パイプラインの流れ

### single モード
1. **取得**: ローカルパス or HTTP(S) URL を受け取り、必要ならダウンロード。
2. **変換**: `ffmpeg` で 16kHz / モノラル / AAC 64kbps に圧縮。25MB を超える場合は時間ベースで分割。
3. **文字起こし**: Groq の `whisper-large-v3` (環境変数 `WHISPER_MODEL` で変更可)。前チャンクの末尾を次チャンクの `prompt` に渡して文脈を継承。
4. **要約**: `claude -p <prompt>` を非対話で実行し、議事録 Markdown を生成。
5. **投稿**: Markdown を見出し/箇条書き/TODO/段落の Notion ブロックに変換して投稿。文字起こし全文も末尾に添付。

### multitrack モード
1. **取得**: 上と同じ。
2. **展開**: ZIP を解凍し、`1-<speaker>.flac` 形式から (index, 話者名, 音声パス) のトラックを抽出。
3. **変換**: 各トラックを 16kHz/mono/AAC 64kbps に圧縮。25MB を超えるトラックだけ時間分割。
4. **トラック単位の文字起こし**: 各チャンクを Groq Whisper の `verbose_json` に投入し、セグメント (`start/end/text/no_speech_prob`) を取得。`no_speech_prob > 0.6` のセグメントは捨てる (相手が喋っているだけの無音区間で発生しがちな幻聴を除去)。チャンク開始秒を `start/end` に加算して録音開始からの絶対秒に正規化。
5. **時系列マージ**: 全話者のセグメントを `start` でソートし、`[hh:mm:ss] 話者: 発言` 形式の文字起こしを生成。
6. **要約 / 投稿**: single と同じ。Claude には「話者ラベル付き」を明示するプロンプトを渡し、議事録の担当者特定に活用させる。
