#!/usr/bin/env -S uv run --script
# /// script
# requires-python = ">=3.10"
# dependencies = [
#   "groq>=0.11.0",
#   "requests>=2.31.0",
#   "python-dotenv>=1.0.0",
# ]
# ///
"""Discord通話録音 (Craig) → Groq Whisper 文字起こし → Claude Code 要約 → Notion 投稿。

前提:
  - ffmpeg / ffprobe が PATH 上にあること
  - `claude` CLI (Claude Code) が PATH 上にあること
  - 環境変数: GROQ_API_KEY, NOTION_API_KEY, NOTION_PARENT_ID
  - Notion インテグレーションを親ページ / DB に招待済みであること

使用例:
  # 1. Craig 公式ページから録音を手元にダウンロード (推奨: single-track FLAC)
  # 2. このスクリプトに渡す
  python pipeline.py ~/Downloads/craig-xxx.flac --title "週次定例 2026-05-24"

  # 直接ダウンロード URL を指定する場合
  python pipeline.py "https://craig.horse/rec/XXXX?key=YYY&format=flac&container=mix"
"""

from __future__ import annotations

import argparse
import math
import os
import re
import shutil
import subprocess
import sys
import tempfile
import zipfile
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Iterable
from urllib.parse import urlparse

import requests
from dotenv import load_dotenv
from groq import Groq

# スクリプトと同階層の .env を最優先、次にカレントの .env を読む。
# 既存の環境変数は上書きしない (export 済みの値を尊重)。
load_dotenv(Path(__file__).resolve().parent / ".env", override=False)
load_dotenv(override=False)

# Groq Whisper の 1 リクエスト上限は 25MB。少し余裕を持たせる。
MAX_CHUNK_BYTES = 24 * 1024 * 1024
# Notion の rich_text 1 要素は 2000 文字、children は 1 リクエスト 100 ブロックまで。
NOTION_TEXT_CHUNK = 1900
NOTION_BLOCK_BATCH = 100
# Data Source 形式 (DB を data_sources 配列で返す) は 2025-09-03 から。
NOTION_API_BASE = "https://api.notion.com/v1"
NOTION_VERSION = "2025-09-03"

# 出力先は固定。タイトル/ファイル名は実行日から自動生成する。
OUTPUT_DIR = Path(__file__).resolve().parent / "output"


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
    transcript = summary.with_name(summary.name.removesuffix(".summary.md")
                                    + ".transcript.txt")
    return f"{date} mtg", transcript, summary

WHISPER_MODEL = os.environ.get("WHISPER_MODEL", "whisper-large-v3")

# マルチトラック処理で対応する音声拡張子
TRACK_EXTS = (".flac", ".ogg", ".opus", ".aac", ".m4a", ".wav", ".mp3")
# Craig 形式のトラックファイル名: "1-username.flac" など (末尾に _userid が付くこともある)
TRACK_NAME_RE = re.compile(
    r"^(?P<idx>\d+)-(?P<name>.+?)(?:_\d{6,})?(?P<ext>\.(?:flac|ogg|opus|aac|m4a|wav|mp3))$",
    re.IGNORECASE,
)


# ---------- 共通ユーティリティ ----------

def require_env(name: str) -> str:
    value = os.environ.get(name)
    if not value:
        sys.exit(f"環境変数 {name} が未設定です")
    return value


def log(msg: str) -> None:
    print(msg, file=sys.stderr, flush=True)


# ---------- 1. 録音データの取得 ----------

def fetch_recording(source: str, workdir: Path) -> Path:
    """ローカルパス または HTTP(S) URL を受け取り、ローカルファイルパスを返す。"""
    if source.startswith(("http://", "https://")):
        name = Path(urlparse(source).path).name or "recording.bin"
        dest = workdir / name
        log(f"[1/4] ダウンロード中: {source}")
        with requests.get(source, stream=True, timeout=600) as r:
            r.raise_for_status()
            with open(dest, "wb") as f:
                for chunk in r.iter_content(chunk_size=1024 * 1024):
                    if chunk:
                        f.write(chunk)
        return dest

    path = Path(source).expanduser().resolve()
    if not path.exists():
        sys.exit(f"録音ファイルが見つかりません: {path}")
    log(f"[1/4] ローカル録音を使用: {path}")
    return path


# ---------- 2. 音声を Whisper 向けに整形 ----------

def to_whisper_friendly(src: Path, workdir: Path) -> Path:
    """16kHz / モノラル / AAC 64kbps に変換し、サイズと帯域を抑える。"""
    out = workdir / (src.stem + ".m4a")
    cmd = [
        "ffmpeg", "-y", "-i", str(src),
        "-vn",
        "-ac", "1",
        "-ar", "16000",
        "-c:a", "aac", "-b:a", "64k",
        str(out),
    ]
    subprocess.run(cmd, check=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    return out


def audio_duration_seconds(path: Path) -> float:
    out = subprocess.check_output([
        "ffprobe", "-v", "error",
        "-show_entries", "format=duration",
        "-of", "default=noprint_wrappers=1:nokey=1",
        str(path),
    ]).decode().strip()
    return float(out)


def split_if_large(path: Path, workdir: Path) -> list[Path]:
    """25MB を超える場合だけ時間ベースで分割する。"""
    size = path.stat().st_size
    if size <= MAX_CHUNK_BYTES:
        return [path]

    duration = audio_duration_seconds(path)
    # 1 チャンクあたりの目安秒数 (バイト比から逆算)
    chunk_seconds = max(60, int(duration * MAX_CHUNK_BYTES / size) - 5)
    n = math.ceil(duration / chunk_seconds)
    log(f"  入力が {size/1e6:.1f}MB あるため {n} 分割します (約 {chunk_seconds}s/chunk)")

    parts: list[Path] = []
    for i in range(n):
        start = i * chunk_seconds
        out = workdir / f"{path.stem}_part{i:03d}.m4a"
        subprocess.run([
            "ffmpeg", "-y",
            "-ss", str(start), "-t", str(chunk_seconds),
            "-i", str(path),
            "-c", "copy",
            str(out),
        ], check=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        parts.append(out)
    return parts


# ---------- 3. Groq Whisper で文字起こし ----------

def transcribe(audio: Path, groq: Groq, language: str | None, prompt: str | None) -> str:
    with open(audio, "rb") as f:
        data = f.read()
    params: dict = {
        "file": (audio.name, data, "audio/m4a"),
        "model": WHISPER_MODEL,
        "response_format": "text",
        "temperature": 0,
    }
    if language:
        params["language"] = language
    if prompt:
        params["prompt"] = prompt
    result = groq.audio.transcriptions.create(**params)
    if isinstance(result, str):
        return result.strip()
    return getattr(result, "text", str(result)).strip()


def transcribe_all(chunks: list[Path], groq: Groq, language: str | None) -> str:
    transcripts: list[str] = []
    prev_tail: str | None = None
    for i, chunk in enumerate(chunks, 1):
        log(f"  - チャンク {i}/{len(chunks)} を Whisper に送信中...")
        text = transcribe(chunk, groq, language, prompt=prev_tail)
        transcripts.append(text)
        # 文脈ヒント用に直近の末尾を次チャンクへ渡す (最大 200 文字)
        prev_tail = text[-200:] if text else None
    return "\n".join(transcripts).strip()


# ---------- 3b. マルチトラック処理 ----------

@dataclass
class Track:
    index: int
    speaker: str
    source_path: Path  # ZIP から展開した元ファイル
    audio_path: Path | None = None  # to_whisper_friendly 後のパス


@dataclass
class Segment:
    start: float  # 録音開始からの絶対秒
    end: float
    speaker: str
    text: str


def _attr(obj, key, default=None):
    """SDK レスポンスが dict でも Pydantic モデルでもアクセスできるよう吸収。"""
    if isinstance(obj, dict):
        return obj.get(key, default)
    return getattr(obj, key, default)


def is_multitrack_source(path: Path) -> bool:
    """ZIP 内に Craig 命名規則のトラックが 2 個以上あればマルチトラック。"""
    if path.suffix.lower() != ".zip":
        return False
    try:
        with zipfile.ZipFile(path) as zf:
            tracks = [n for n in zf.namelist() if TRACK_NAME_RE.match(Path(n).name)]
        return len(tracks) >= 2
    except zipfile.BadZipFile:
        return False


def extract_tracks(zip_path: Path, workdir: Path) -> list[Track]:
    """ZIP を展開して Craig トラック群を Track のリストとして返す。"""
    extract_dir = workdir / "tracks"
    extract_dir.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(zip_path) as zf:
        zf.extractall(extract_dir)

    tracks: list[Track] = []
    for p in sorted(extract_dir.rglob("*")):
        if not p.is_file():
            continue
        m = TRACK_NAME_RE.match(p.name)
        if not m:
            continue
        tracks.append(Track(
            index=int(m.group("idx")),
            speaker=m.group("name"),
            source_path=p,
        ))

    if not tracks:
        sys.exit(f"ZIP からマルチトラックを検出できませんでした: {zip_path}")
    tracks.sort(key=lambda t: t.index)
    log(f"  検出トラック: {', '.join(f'{t.index}={t.speaker}' for t in tracks)}")
    return tracks


def _split_with_offsets(audio: Path, workdir: Path) -> list[tuple[Path, float]]:
    """25MB を超える場合に分割し、(チャンクパス, 開始秒) のリストを返す。"""
    size = audio.stat().st_size
    if size <= MAX_CHUNK_BYTES:
        return [(audio, 0.0)]
    duration = audio_duration_seconds(audio)
    chunk_seconds = max(60, int(duration * MAX_CHUNK_BYTES / size) - 5)
    n = math.ceil(duration / chunk_seconds)
    log(f"    {size/1e6:.1f}MB あるため {n} 分割 (約 {chunk_seconds}s/chunk)")
    out: list[tuple[Path, float]] = []
    for i in range(n):
        start = i * chunk_seconds
        chunk = workdir / f"{audio.stem}_part{i:03d}.m4a"
        subprocess.run(
            [
                "ffmpeg", "-y", "-hide_banner", "-loglevel", "error",
                "-ss", str(start), "-t", str(chunk_seconds),
                "-i", str(audio),
                "-c", "copy",
                str(chunk),
            ],
            check=True,
        )
        out.append((chunk, float(start)))
    return out


def transcribe_track_verbose(
    track: Track,
    groq: Groq,
    language: str | None,
    workdir: Path,
    no_speech_threshold: float = 0.6,
) -> list[Segment]:
    """トラックを丸ごと Whisper に投げ、verbose_json のセグメントを Segment 列に変換。"""
    audio = to_whisper_friendly(track.source_path, workdir)
    chunks = _split_with_offsets(audio, workdir)

    segments: list[Segment] = []
    for i, (chunk, offset) in enumerate(chunks, 1):
        log(f"    - {track.speaker}: チャンク {i}/{len(chunks)} を Whisper に送信中"
            f" ({chunk.stat().st_size/1e6:.1f}MB)")
        with open(chunk, "rb") as f:
            data = f.read()
        params: dict = {
            "file": (chunk.name, data, "audio/m4a"),
            "model": WHISPER_MODEL,
            "response_format": "verbose_json",
            "temperature": 0,
        }
        if language:
            params["language"] = language
        result = groq.audio.transcriptions.create(**params)

        sub_segments = _attr(result, "segments") or []
        kept = 0
        for seg in sub_segments:
            text = (_attr(seg, "text") or "").strip()
            if not text:
                continue
            # マルチトラックは「自分が喋っていない時間」が長いので
            # no_speech_prob が高いセグメントは捨てる (幻聴対策)
            no_speech = _attr(seg, "no_speech_prob")
            if no_speech is not None and float(no_speech) > no_speech_threshold:
                continue
            segments.append(Segment(
                start=float(_attr(seg, "start", 0.0)) + offset,
                end=float(_attr(seg, "end", 0.0)) + offset,
                speaker=track.speaker,
                text=text,
            ))
            kept += 1
        log(f"      → セグメント {kept}/{len(sub_segments)} 採用")
    return segments


def format_timestamp(seconds: float) -> str:
    h = int(seconds // 3600)
    m = int((seconds % 3600) // 60)
    s = int(seconds % 60)
    return f"{h:02d}:{m:02d}:{s:02d}" if h else f"{m:02d}:{s:02d}"


def format_transcript(segments: list[Segment]) -> str:
    segments.sort(key=lambda x: x.start)
    return "\n".join(
        f"[{format_timestamp(seg.start)}] {seg.speaker}: {seg.text}"
        for seg in segments
    )


def run_multitrack(zip_path: Path, groq: Groq, language: str | None, workdir: Path) -> str:
    """マルチトラック ZIP を話者付き文字起こし文字列に変換する。

    各トラックを丸ごと Whisper の verbose_json に投げ、返ってきたセグメントを
    話者付き Segment に変換してから時系列マージする。no_speech_prob で無音区間の
    幻聴をフィルタする。
    """
    tracks = extract_tracks(zip_path, workdir)
    all_segments: list[Segment] = []
    for t in tracks:
        log(f"  - トラック {t.index} ({t.speaker}) を処理中")
        segs = transcribe_track_verbose(t, groq, language, workdir)
        log(f"    トラック合計 {len(segs)} セグメント")
        all_segments.extend(segs)
    if not all_segments:
        sys.exit("どのトラックからも発話を検出できませんでした")
    return format_transcript(all_segments)


# ---------- 4. Claude Code で要約 ----------

SUMMARY_PROMPT_TEMPLATE = """以下は Discord で行われた通話の文字起こしです。日本語で読みやすい議事録 (Markdown) に整形してください。

# 入力フォーマット
{format_note}

# 出力フォーマット
## 概要
- 通話全体を 3〜5 行で要約

## 主な議題
- 議題ごとに見出しを設けず、箇条書きで「議論内容 / 結論 / 補足」をまとめる{speaker_hint_topic}

## 決定事項
- 箇条書き。決まったことだけを簡潔に

## ToDo / ネクストアクション
- [ ] 担当者: 内容 (期限が明示されていれば末尾に「(〜まで)」){speaker_hint_todo}

## 未解決の論点
- 箇条書き。なければ「なし」と書く

# 制約
- 文字起こしは音声認識の誤りを含むため、明らかに不自然な単語は文脈から補正してよい
- 推測で情報を増やさない。元の発言にない事実は書かない
- 出力は Markdown 本文のみ。前置きや「了解しました」等は不要

# 文字起こし
{transcript}
"""


def summarize_with_claude(transcript: str, claude_bin: str, multitrack: bool) -> str:
    if multitrack:
        format_note = (
            "各行は `[hh:mm:ss] 話者名: 発言内容` 形式です。話者を区別して議論の流れを"
            "正確に追えます。"
        )
        speaker_hint_topic = "。発言者が誰かを明示すると読みやすい"
        speaker_hint_todo = "。担当者は文字起こし中の話者名から特定してよい"
    else:
        format_note = "話者ラベルなしの素の文字起こしです (mix 録音)。"
        speaker_hint_topic = ""
        speaker_hint_todo = ""
    prompt = SUMMARY_PROMPT_TEMPLATE.format(
        transcript=transcript,
        format_note=format_note,
        speaker_hint_topic=speaker_hint_topic,
        speaker_hint_todo=speaker_hint_todo,
    )
    proc = subprocess.run(
        [claude_bin, "-p", prompt],
        check=False,
        capture_output=True,
        text=True,
    )
    if proc.returncode != 0:
        sys.exit(
            f"claude CLI がエラー終了しました (exit={proc.returncode})\n"
            f"stderr:\n{proc.stderr}"
        )
    return proc.stdout.strip()


# ---------- 5. Markdown → Notion blocks ----------

def _rich_text(text: str) -> list[dict]:
    if not text:
        return [{"type": "text", "text": {"content": ""}}]
    chunks = [text[i:i + 2000] for i in range(0, len(text), 2000)]
    return [{"type": "text", "text": {"content": c}} for c in chunks]


def _heading(level: int, text: str) -> dict:
    key = f"heading_{min(max(level, 1), 3)}"
    return {"object": "block", "type": key, key: {"rich_text": _rich_text(text)}}


def _paragraph(text: str) -> dict:
    return {
        "object": "block",
        "type": "paragraph",
        "paragraph": {"rich_text": _rich_text(text)},
    }


def md_to_blocks(md: str) -> list[dict]:
    """最小限の Markdown → Notion block 変換 (見出し / 箇条書き / TODO / 段落)。"""
    blocks: list[dict] = []
    for raw in md.splitlines():
        line = raw.rstrip()
        if not line.strip():
            continue

        stripped = line.lstrip()

        if stripped.startswith("### "):
            blocks.append(_heading(3, stripped[4:]))
        elif stripped.startswith("## "):
            blocks.append(_heading(2, stripped[3:]))
        elif stripped.startswith("# "):
            blocks.append(_heading(1, stripped[2:]))
        elif stripped.startswith(("- [ ] ", "- [x] ", "- [X] ")):
            checked = stripped[3].lower() == "x"
            text = stripped[6:]
            blocks.append({
                "object": "block",
                "type": "to_do",
                "to_do": {"rich_text": _rich_text(text), "checked": checked},
            })
        elif stripped.startswith(("- ", "* ")):
            blocks.append({
                "object": "block",
                "type": "bulleted_list_item",
                "bulleted_list_item": {"rich_text": _rich_text(stripped[2:])},
            })
        else:
            blocks.append(_paragraph(line))
    return blocks


def transcript_blocks(transcript: str) -> list[dict]:
    """文字起こし全文を段落ブロック群として返す (1 ブロック 1900 文字)。"""
    blocks: list[dict] = [_heading(2, "文字起こし全文")]
    for i in range(0, len(transcript), NOTION_TEXT_CHUNK):
        blocks.append(_paragraph(transcript[i:i + NOTION_TEXT_CHUNK]))
    return blocks


# ---------- 6. Notion へ投稿 ----------

class NotionError(RuntimeError):
    def __init__(self, status: int, body: dict | str):
        self.status = status
        self.body = body
        code = body.get("code") if isinstance(body, dict) else None
        msg = body.get("message", body) if isinstance(body, dict) else body
        super().__init__(f"Notion API {status} ({code}): {msg}")


def _notion_request(token: str, method: str, path: str, body: dict | None = None) -> dict:
    r = requests.request(
        method,
        f"{NOTION_API_BASE}/{path}",
        headers={
            "Authorization": f"Bearer {token}",
            "Notion-Version": NOTION_VERSION,
            "Content-Type": "application/json",
        },
        json=body,
        timeout=60,
    )
    if r.status_code >= 400:
        try:
            err = r.json()
        except ValueError:
            err = r.text
        raise NotionError(r.status_code, err)
    return r.json()


def _find_title_prop(props: dict) -> str | None:
    for name, prop in props.items():
        if prop.get("type") == "title":
            return name
    return None


def resolve_notion_target(
    token: str,
    parent_id: str,
    parent_type: str,
) -> tuple[dict, str, dict]:
    """親 ID を解決し、(parent, title_prop_name, schema_properties) を返す。

    - parent_type=auto: API に問い合わせて DB / Page を自動判定
    - DB の場合: 2025-09-03 仕様で data_sources を辿り data_source_id 親を使う
    - Page の場合: page_id 親を使う (schema は空 dict)
    """
    if parent_type == "auto":
        try:
            _notion_request(token, "GET", f"databases/{parent_id}")
            parent_type = "database"
        except NotionError as e:
            if e.status != 404:
                raise
            try:
                _notion_request(token, "GET", f"pages/{parent_id}")
                parent_type = "page"
            except NotionError as e2:
                if e2.status == 404:
                    sys.exit(
                        f"Notion 親 ID {parent_id} にアクセスできません。\n"
                        "  1. ID が正しいか (URL 末尾の 32 桁ハッシュ)\n"
                        "  2. 親ページ/DB を Notion で開き、… → Connections で\n"
                        "     インテグレーション 'shiori' を招待しているか\n"
                        "  3. DB に投稿したい場合は、親ページではなく\n"
                        "     DB そのものにインテグレーションを招待\n"
                    )
                raise

    if parent_type == "page":
        return {"type": "page_id", "page_id": parent_id}, "title", {}

    # parent_type == "database"
    db = _notion_request(token, "GET", f"databases/{parent_id}")
    data_sources = db.get("data_sources") or []

    if data_sources:
        # 2025-09-03 仕様: data_source 経由で投稿
        if len(data_sources) > 1:
            log(
                f"  data_sources が {len(data_sources)} 件検出、"
                f"先頭を使用: {data_sources[0].get('name')}"
            )
        ds_id = data_sources[0]["id"]
        ds = _notion_request(token, "GET", f"data_sources/{ds_id}")
        props = ds.get("properties", {})
        title_prop = _find_title_prop(props)
        if title_prop is None:
            sys.exit(
                f"data_source {ds_id} に title プロパティがありません。"
                f" 利用可能: {list(props.keys())}"
            )
        return {"type": "data_source_id", "data_source_id": ds_id}, title_prop, props

    # 旧形式 DB の互換 (properties が DB レスポンスに直接含まれる)
    props = db.get("properties", {})
    title_prop = _find_title_prop(props)
    if title_prop is None:
        sys.exit(
            f"DB {parent_id} に title プロパティがありません。"
            f" 利用可能: {list(props.keys())}"
        )
    return {"type": "database_id", "database_id": parent_id}, title_prop, props


def build_extra_properties(schema: dict, extras: list[str]) -> dict:
    """--property NAME=VALUE のリストを Notion properties に変換する。

    DB スキーマからプロパティ型を引いて適切な形式に整形する。
    multi_select は ',' 区切りで複数指定可。
    """
    if not extras:
        return {}
    if not schema:
        sys.exit("--property は DB 親 (data_source / database) のときのみ使えます")

    out: dict = {}
    for spec in extras:
        if "=" not in spec:
            sys.exit(f"--property は 'NAME=VALUE' 形式で指定してください: {spec}")
        name, value = spec.split("=", 1)
        name, value = name.strip(), value.strip()
        prop = schema.get(name)
        if prop is None:
            sys.exit(
                f"プロパティ '{name}' が DB に存在しません。"
                f" 利用可能: {list(schema.keys())}"
            )
        ptype = prop.get("type")
        if ptype == "multi_select":
            out[name] = {"multi_select": [
                {"name": v.strip()} for v in value.split(",") if v.strip()
            ]}
        elif ptype == "select":
            out[name] = {"select": {"name": value}}
        elif ptype == "status":
            out[name] = {"status": {"name": value}}
        elif ptype == "rich_text":
            out[name] = {"rich_text": _rich_text(value)}
        elif ptype == "number":
            out[name] = {"number": float(value)}
        elif ptype == "checkbox":
            out[name] = {"checkbox": value.lower() in ("true", "1", "yes", "y", "on")}
        elif ptype == "date":
            out[name] = {"date": {"start": value}}
        elif ptype == "url":
            out[name] = {"url": value}
        elif ptype == "email":
            out[name] = {"email": value}
        elif ptype == "phone_number":
            out[name] = {"phone_number": value}
        else:
            sys.exit(f"プロパティ型 '{ptype}' は未対応です (name={name})")
    return out


def post_to_notion(
    token: str,
    parent_id: str,
    parent_type: str,
    title: str,
    body_blocks: list[dict],
    extras: list[str] | None = None,
) -> str:
    parent, title_prop, schema = resolve_notion_target(token, parent_id, parent_type)
    log(f"  Notion 親: {parent['type']} = {parent[parent['type']]}")

    properties = {title_prop: {"title": _rich_text(title)}}
    properties.update(build_extra_properties(schema, extras or []))
    page = _notion_request(token, "POST", "pages", {
        "parent": parent,
        "properties": properties,
        "children": body_blocks[:NOTION_BLOCK_BATCH],
    })
    page_id = page["id"]

    for i in range(NOTION_BLOCK_BATCH, len(body_blocks), NOTION_BLOCK_BATCH):
        _notion_request(token, "PATCH", f"blocks/{page_id}/children", {
            "children": body_blocks[i:i + NOTION_BLOCK_BATCH],
        })
    return page.get("url", f"https://www.notion.so/{page_id.replace('-', '')}")


# ---------- main ----------

def main(argv: Iterable[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
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
                             "例: --property 'カテゴリー=議事録,定例' "
                             "(複数回指定可)")
    args = parser.parse_args(argv)

    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)

    if args.post_only:
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

    if not args.source:
        sys.exit("source 引数 (録音ファイル or URL) が必要です")

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
        # 1. 録音取得
        raw = fetch_recording(args.source, workdir)

        # モード判定
        if args.mode == "multitrack":
            multitrack = True
        elif args.mode == "single":
            multitrack = False
        else:
            multitrack = is_multitrack_source(raw)
        log(f"  モード: {'multitrack' if multitrack else 'single'}")

        # 2-3. 文字起こし
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

        # 4. Claude Code で要約
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

        # 5. Notion 投稿
        body = md_to_blocks(summary_md) + transcript_blocks(transcript)
        url = post_to_notion(notion_key, parent_id, args.parent_type, title, body,
                             extras=args.properties)
        print(f"Notion に投稿しました: {url}")
        return 0
    finally:
        if not args.keep_workdir:
            shutil.rmtree(workdir, ignore_errors=True)


if __name__ == "__main__":
    sys.exit(main())
