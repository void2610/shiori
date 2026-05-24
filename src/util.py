"""ロギング・環境変数チェック・HTTP ストリームダウンロードの汎用ヘルパ。"""

from __future__ import annotations

import os
import sys
from pathlib import Path

import requests


def require_env(name: str) -> str:
    value = os.environ.get(name)
    if not value:
        sys.exit(f"環境変数 {name} が未設定です")
    return value


def log(msg: str) -> None:
    print(msg, file=sys.stderr, flush=True)


def stream_download(url: str, dest: Path, timeout: int = 600) -> int:
    """URL をローカルパスへストリーミング保存し、書き込んだバイト数を返す。"""
    written = 0
    with requests.get(url, stream=True, timeout=timeout) as r:
        r.raise_for_status()
        with open(dest, "wb") as f:
            for chunk in r.iter_content(chunk_size=1024 * 1024):
                if chunk:
                    f.write(chunk)
                    written += len(chunk)
    return written
