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

Craig のダウンロードページから **single-track FLAC (mix)** を手元に落として渡すのが最も確実です。

録音ファイルは `input/` に置き、文字起こし/要約を保存したい場合は `output/` を使う運用を推奨 (中身は `.gitignore` 済み)。

```bash
# ローカルファイル
uv run pipeline.py input/craig-xxx.flac \
  --title "週次定例 2026-05-24" \
  --save-transcript output/2026-05-24.transcript.txt \
  --save-summary    output/2026-05-24.summary.md

# 直接ダウンロード URL
uv run pipeline.py "https://craig.horse/rec/XXXX?key=YYY&format=flac&container=mix"

# Notion 投稿せず結果だけ確認
uv run pipeline.py recording.flac --skip-notion

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

## パイプラインの流れ

1. **取得**: ローカルパス or HTTP(S) URL を受け取り、必要ならダウンロード。
2. **変換**: `ffmpeg` で 16kHz / モノラル / AAC 64kbps に圧縮。25MB を超える場合は時間ベースで分割。
3. **文字起こし**: Groq の `whisper-large-v3` (環境変数 `WHISPER_MODEL` で変更可)。前チャンクの末尾を次チャンクの `prompt` に渡して文脈を継承。
4. **要約**: `claude -p <prompt>` を非対話で実行し、議事録 Markdown を生成。
5. **投稿**: Markdown を見出し/箇条書き/TODO/段落の Notion ブロックに変換して投稿。文字起こし全文も末尾に添付。
