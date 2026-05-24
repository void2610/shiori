"""Notion API クライアントと、Markdown → Notion blocks 変換 / ページ投稿。"""

from __future__ import annotations

import sys

import requests

from config import (
    NOTION_API_BASE,
    NOTION_BLOCK_BATCH,
    NOTION_TEXT_CHUNK,
    NOTION_VERSION,
)
from util import log


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


# ---------- Markdown → Notion blocks ----------

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


# ---------- Notion 投稿 ----------

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
