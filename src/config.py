"""定数群と .env ロード、出力ファイル名の生成。

このモジュールは import 時に .env を読み込む副作用を持つ
(プロジェクトルート/.env → cwd/.env の順、既存環境変数は尊重)。
"""

from __future__ import annotations

import os
import re
import sys
from datetime import datetime
from pathlib import Path

from dotenv import load_dotenv

# config.py は <root>/src/config.py なので parent.parent がプロジェクトルート。
PROJECT_ROOT = Path(__file__).resolve().parent.parent

load_dotenv(PROJECT_ROOT / ".env", override=False)
load_dotenv(override=False)

# Groq Whisper の 1 リクエスト上限は 25MB。少し余裕を持たせる。
MAX_CHUNK_BYTES = 24 * 1024 * 1024
WHISPER_MODEL = os.environ.get("WHISPER_MODEL", "whisper-large-v3")

# Notion の rich_text 1 要素は 2000 文字、children は 1 リクエスト 100 ブロックまで。
NOTION_TEXT_CHUNK = 1900
NOTION_BLOCK_BATCH = 100
# Data Source 形式 (DB を data_sources 配列で返す) は 2025-09-03 から。
NOTION_API_BASE = "https://api.notion.com/v1"
NOTION_VERSION = "2025-09-03"

# Craig ジョブが「これ以上待っても complete にならない」終端失敗状態。
CRAIG_TERMINAL_FAILURES = frozenset({"failed", "error", "cancelled"})

# Craig マルチトラックで対応する音声拡張子と、トラックファイル名規約。
TRACK_EXTS = (".flac", ".ogg", ".opus", ".aac", ".m4a", ".wav", ".mp3")
TRACK_NAME_RE = re.compile(
    r"^(?P<idx>\d+)-(?P<name>.+?)(?:_\d{6,})?(?P<ext>\.(?:flac|ogg|opus|aac|m4a|wav|mp3))$",
    re.IGNORECASE,
)

# 出力先 (プロジェクトルート/output)。タイトル/ファイル名は実行日から自動生成。
OUTPUT_DIR = PROJECT_ROOT / "output"


def today_artifacts() -> tuple[str, Path, Path]:
    """新規記録用に (title, transcript_path, summary_path) を返す。

    同日複数回実行を区別するためファイル名には時刻サフィックスを付ける。
    Notion タイトルは時刻なしで yyyy-mm-dd mtg のまま (DB 側で時刻を見たい
    場合は created_time プロパティで十分なので)。
    """
    now = datetime.now()
    date = f"{now:%Y-%m-%d}"
    stamp = f"{now:%Y-%m-%d_%H%M}"
    return (
        f"{date} mtg",
        OUTPUT_DIR / f"{stamp}.transcript.txt",
        OUTPUT_DIR / f"{stamp}.summary.md",
    )


def latest_today_artifacts() -> tuple[str, Path, Path]:
    """--post-only 用に、当日生成された最新の出力ペアを返す。"""
    date = f"{datetime.now():%Y-%m-%d}"
    summaries = sorted(OUTPUT_DIR.glob(f"{date}*.summary.md"))
    if not summaries:
        sys.exit(f"当日 ({date}) の要約ファイルが output/ に見つかりません")
    summary = summaries[-1]
    transcript = summary.with_name(
        summary.name.removesuffix(".summary.md") + ".transcript.txt"
    )
    return f"{date} mtg", transcript, summary
