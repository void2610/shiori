"""Claude Code CLI 経由で議事録 Markdown を生成。"""

from __future__ import annotations

import subprocess
import sys

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
