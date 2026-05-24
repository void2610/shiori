"""ffmpeg / ffprobe を使った音声操作と、Craig マルチトラック ZIP の解析。"""

from __future__ import annotations

import math
import subprocess
import sys
import zipfile
from dataclasses import dataclass
from pathlib import Path

from .config import MAX_CHUNK_BYTES, TRACK_NAME_RE
from .util import log


@dataclass
class Track:
    index: int
    speaker: str
    source_path: Path  # ZIP から展開した元ファイル
    audio_path: Path | None = None  # to_whisper_friendly 後のパス


def to_whisper_friendly(src: Path, workdir: Path) -> Path:
    """16kHz / モノラル / AAC 64kbps に変換し、サイズと帯域を抑える。"""
    out = workdir / (src.stem + ".m4a")
    subprocess.run(
        [
            "ffmpeg", "-y", "-i", str(src),
            "-vn",
            "-ac", "1",
            "-ar", "16000",
            "-c:a", "aac", "-b:a", "64k",
            str(out),
        ],
        check=True,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
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


def split_with_offsets(audio: Path, workdir: Path) -> list[tuple[Path, float]]:
    """25MB 超なら分割し、(チャンクパス, 録音開始からの開始秒) を返す。"""
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
