"""録音データの取得 (ローカル / 直接 URL / Craig 共有 URL)。"""

from __future__ import annotations

import sys
import time
from pathlib import Path
from urllib.parse import ParseResult, parse_qs, urlparse

import requests

from .config import CRAIG_TERMINAL_FAILURES
from .util import log, stream_download


def fetch_recording(source: str, workdir: Path) -> Path:
    """ローカルパス または HTTP(S) URL を受け取り、ローカルファイルパスを返す。

    Craig のダウンロードページ URL (`https://craig.{horse,chat}/rec/<id>?key=<key>`)
    なら、cook API 経由でマルチトラック FLAC ZIP を自動取得する。
    """
    if source.startswith(("http://", "https://")):
        parsed = urlparse(source)
        if _looks_like_craig_share(parsed):
            return _download_from_craig(parsed, workdir)
        name = Path(parsed.path).name or "recording.bin"
        dest = workdir / name
        log(f"[1/4] ダウンロード中: {source}")
        stream_download(source, dest)
        return dest

    path = Path(source).expanduser().resolve()
    if not path.exists():
        sys.exit(f"録音ファイルが見つかりません: {path}")
    log(f"[1/4] ローカル録音を使用: {path}")
    return path


def _looks_like_craig_share(parsed: ParseResult) -> bool:
    """https://craig.{horse,chat}/rec/<id>?key=<key> 形式か判定。"""
    if "craig" not in parsed.netloc:
        return False
    parts = parsed.path.strip("/").split("/")
    return len(parts) >= 2 and parts[0] == "rec"


def _download_from_craig(parsed: ParseResult, workdir: Path) -> Path:
    """Craig (craig.horse / craig.chat) の共有 URL から multi-track FLAC ZIP を取得。

    エンドポイント (craig.horse, v1 API):
      GET    /api/v1/recordings/{id}/job?key=...  ジョブ状態
      POST   /api/v1/recordings/{id}/job?key=...  ジョブ作成 (body は下記)
      GET    /dl/{outputFileName}                 cook 済みファイル本体

    POST ボディ:
      {"type": "recording", "options": {"format": "flac", "container": "zip"}}

    既存ジョブが flac.zip かつ status=complete なら再利用。
    """
    parts = parsed.path.strip("/").split("/")
    rec_id = parts[1]
    key = parse_qs(parsed.query).get("key", [""])[0]
    if not key:
        sys.exit(f"Craig URL にクエリパラメータ 'key' がありません: {parsed.geturl()}")

    base = f"{parsed.scheme}://{parsed.netloc}"
    job_url = f"{base}/api/v1/recordings/{rec_id}/job?key={key}"

    log(f"[1/4] Craig {rec_id} を取得します")

    def fetch_job() -> dict:
        r = requests.get(job_url, timeout=60)
        r.raise_for_status()
        return (r.json() or {}).get("job") or {}

    def matches(job: dict) -> bool:
        if not job or job.get("type") != "recording":
            return False
        opts = job.get("options") or {}
        return opts.get("format") == "flac" and opts.get("container") == "zip"

    job = fetch_job()
    is_reusable = matches(job) and job.get("status") == "complete"

    if is_reusable:
        log(f"  既存ジョブを再利用: {job.get('outputFileName')}")
    else:
        if matches(job):
            log(f"  既存ジョブを継続待ち (status={job.get('status')})")
        else:
            log("  cook ジョブを作成 (flac multi-track zip)")
            r = requests.post(
                job_url,
                json={
                    "type": "recording",
                    "options": {"format": "flac", "container": "zip"},
                },
                headers={"Content-Type": "application/json"},
                timeout=60,
            )
            if r.status_code >= 400:
                sys.exit(f"Craig ジョブ作成失敗 ({r.status_code}): {r.text}")

        last_label = None
        while True:
            job = fetch_job()
            status = job.get("status")
            state_type = (job.get("state") or {}).get("type")
            label = f"{status}/{state_type}" if state_type else str(status)
            if label != last_label:
                log(f"    status: {label}")
                last_label = label
            if status == "complete":
                break
            if status in CRAIG_TERMINAL_FAILURES:
                sys.exit(f"Craig ジョブ失敗: {job}")
            time.sleep(3)

    file_name = job.get("outputFileName")
    if not file_name:
        sys.exit(f"outputFileName がありません: {job}")

    file_url = f"{base}/dl/{file_name}"
    dest = workdir / file_name
    expected = job.get("outputSize") or 0
    log(f"  ダウンロード: {file_url} ({expected/1e6:.1f}MB)")
    written = stream_download(file_url, dest, timeout=900)
    # outputSize と乖離していたら破損 ZIP を握ったまま進まないよう早めに失敗させる
    if expected and written != expected:
        sys.exit(f"ダウンロードサイズ不一致: {written}B / 期待 {expected}B")
    log(f"  完了: {dest.name} ({written/1e6:.1f}MB)")
    return dest
