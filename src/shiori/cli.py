"""CLI エントリポイント (argparse + パイプライン実行)。"""

from __future__ import annotations

import argparse
import os
import shutil
import sys
import tempfile
from pathlib import Path
from typing import Iterable

from groq import Groq

from .audio import is_multitrack_source, split_if_large, to_whisper_friendly
from .config import (
    OUTPUT_DIR,
    WHISPER_MODEL,
    latest_today_artifacts,
    today_artifacts,
)
from .notion import md_to_blocks, post_to_notion, transcript_blocks
from .recording import fetch_recording
from .summarize import summarize_with_claude
from .util import log, require_env
from .whisper import run_multitrack, transcribe_all


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="shiori",
        description="Craig 録音 → Whisper → Claude Code 要約 → Notion 投稿パイプライン",
    )
    parser.add_argument("source", nargs="?", default=None,
                        help="録音ファイルパス または 直接ダウンロード URL "
                             "(--post-only 時は省略可)")
    parser.add_argument("--language", default="ja",
                        help="Whisper 言語コード (空文字で自動判定)")
    parser.add_argument("--parent-type",
                        choices=["page", "database", "auto"],
                        default=os.environ.get("NOTION_PARENT_TYPE", "auto"),
                        help="親 ID の種別 (既定 auto: API で自動判定)")
    parser.add_argument("--claude-bin",
                        default=os.environ.get("CLAUDE_BIN", "claude"),
                        help="claude CLI の実行パス")
    parser.add_argument("--keep-workdir", action="store_true",
                        help="作業ディレクトリを残す (デバッグ用)")
    parser.add_argument("--skip-notion", action="store_true",
                        help="Notion 投稿をスキップし、要約と文字起こしを stdout に出力")
    parser.add_argument("--mode", choices=["auto", "single", "multitrack"],
                        default="auto",
                        help="auto: ZIP かつ複数トラックを検出したら multitrack に切替")
    parser.add_argument("--post-only", action="store_true",
                        help="Whisper/Claude をスキップし、当日の output から Notion 投稿")
    parser.add_argument("--property", action="append", default=[], dest="properties",
                        metavar="NAME=VALUE",
                        help="DB の追加プロパティ。multi_select は ',' 区切り。"
                             "例: --property 'カテゴリー=議事録,定例' (複数回指定可)")
    return parser


def main(argv: Iterable[str] | None = None) -> int:
    args = _build_parser().parse_args(argv)
    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)

    if args.post_only:
        return _run_post_only(args)

    if not args.source:
        sys.exit("source 引数 (録音ファイル or URL) が必要です")

    return _run_pipeline(args)


def _run_post_only(args: argparse.Namespace) -> int:
    if args.skip_notion:
        sys.exit("--post-only と --skip-notion は同時指定できません")
    title, transcript_path, summary_path = latest_today_artifacts()
    log(f"  読み込み: {summary_path.name}")

    notion_key = require_env("NOTION_API_KEY")
    parent_id = require_env("NOTION_PARENT_ID")
    summary_md = summary_path.read_text(encoding="utf-8")
    transcript = (transcript_path.read_text(encoding="utf-8")
                  if transcript_path.exists() else "")

    body = md_to_blocks(summary_md)
    if transcript:
        body += transcript_blocks(transcript)
    url = post_to_notion(notion_key, parent_id, args.parent_type, title, body,
                         extras=args.properties)
    print(f"Notion に投稿しました: {url}")
    return 0


def _run_pipeline(args: argparse.Namespace) -> int:
    title, transcript_path, summary_path = today_artifacts()

    groq_key = require_env("GROQ_API_KEY")
    notion_key = parent_id = None
    if not args.skip_notion:
        notion_key = require_env("NOTION_API_KEY")
        parent_id = require_env("NOTION_PARENT_ID")

    if shutil.which(args.claude_bin) is None:
        sys.exit(f"claude CLI が見つかりません: {args.claude_bin}")
    if shutil.which("ffmpeg") is None or shutil.which("ffprobe") is None:
        sys.exit("ffmpeg / ffprobe が PATH 上に必要です")

    workdir = Path(tempfile.mkdtemp(prefix="craig_pipeline_"))
    log(f"作業ディレクトリ: {workdir}")
    log(f"出力タイトル: {title}")

    try:
        raw = fetch_recording(args.source, workdir)

        if args.mode == "multitrack":
            multitrack = True
        elif args.mode == "single":
            multitrack = False
        else:
            multitrack = is_multitrack_source(raw)
        log(f"  モード: {'multitrack' if multitrack else 'single'}")

        groq = Groq(api_key=groq_key)
        if multitrack:
            log(f"[2-3/4] マルチトラックを Whisper ({WHISPER_MODEL}) で文字起こし中...")
            transcript = run_multitrack(raw, groq, args.language or None, workdir)
        else:
            log("[2/4] 音声を変換中...")
            audio = to_whisper_friendly(raw, workdir)
            chunks = split_if_large(audio, workdir)
            log(f"[3/4] Whisper ({WHISPER_MODEL}) で文字起こし中...")
            transcript = transcribe_all(chunks, groq, args.language or None)

        transcript_path.write_text(transcript, encoding="utf-8")
        log(f"  文字起こしを保存: {transcript_path}")

        log("[4/4] Claude Code で要約中...")
        summary_md = summarize_with_claude(transcript, args.claude_bin, multitrack)
        summary_path.write_text(summary_md, encoding="utf-8")
        log(f"  要約を保存: {summary_path}")

        if args.skip_notion:
            print("=== 要約 (Markdown) ===")
            print(summary_md)
            print("\n=== 文字起こし ===")
            print(transcript)
            return 0

        body = md_to_blocks(summary_md) + transcript_blocks(transcript)
        url = post_to_notion(notion_key, parent_id, args.parent_type, title, body,
                             extras=args.properties)
        print(f"Notion に投稿しました: {url}")
        return 0
    finally:
        if not args.keep_workdir:
            shutil.rmtree(workdir, ignore_errors=True)
