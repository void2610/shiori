"""Groq Whisper を使った文字起こし (single / multitrack 両モード)。"""

from __future__ import annotations

import sys
from dataclasses import dataclass
from pathlib import Path

from groq import Groq

from audio import (
    Track,
    extract_tracks,
    split_with_offsets,
    to_whisper_friendly,
)
from config import WHISPER_MODEL
from util import log


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


def transcribe_track_verbose(
    track: Track,
    groq: Groq,
    language: str | None,
    workdir: Path,
    no_speech_threshold: float = 0.6,
) -> list[Segment]:
    """トラックを丸ごと Whisper に投げ、verbose_json のセグメントを Segment 列に変換。"""
    audio = to_whisper_friendly(track.source_path, workdir)
    chunks = split_with_offsets(audio, workdir)

    segments: list[Segment] = []
    for i, (chunk, offset) in enumerate(chunks, 1):
        log(
            f"    - {track.speaker}: チャンク {i}/{len(chunks)} を Whisper に送信中"
            f" ({chunk.stat().st_size/1e6:.1f}MB)"
        )
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


def _format_timestamp(seconds: float) -> str:
    h = int(seconds // 3600)
    m = int((seconds % 3600) // 60)
    s = int(seconds % 60)
    return f"{h:02d}:{m:02d}:{s:02d}" if h else f"{m:02d}:{s:02d}"


def _format_transcript(segments: list[Segment]) -> str:
    segments.sort(key=lambda x: x.start)
    return "\n".join(
        f"[{_format_timestamp(seg.start)}] {seg.speaker}: {seg.text}"
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
    return _format_transcript(all_segments)
