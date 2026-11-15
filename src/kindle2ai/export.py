"""Write the assembled book out in formats an LLM can actually ingest."""

from __future__ import annotations

import json
import re
from datetime import datetime, timezone
from pathlib import Path

from . import textclean


def _slug(name: str) -> str:
    s = re.sub(r"[\s/\\:*?\"<>|]+", "_", (name or "book").strip())
    return (s[:80] or "book").strip("._")


def _yaml_escape(value) -> str:
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, (int, float)):
        return str(value)
    return '"' + str(value).replace("\\", "\\\\").replace('"', '\\"') + '"'


def write_markdown(dest: Path, meta: dict, blocks: list[dict]) -> Path:
    lines = ["---"]
    for key, value in meta.items():
        lines.append(f"{key}: {_yaml_escape(value)}")
    lines.append("---")
    lines.append("")
    lines.append(f"# {meta.get('title', 'Untitled')}")
    lines.append("")

    current_page = None
    for block in blocks:
        text = block["text"]
        if block["page_start"] != current_page:
            current_page = block["page_start"]
            lines.append(f"<!-- page {current_page} -->")
        lines.append(text)
        lines.append("")

    dest.write_text("\n".join(lines).rstrip() + "\n", encoding="utf-8")
    return dest


def write_text(dest: Path, blocks: list[dict]) -> Path:
    dest.write_text("\n\n".join(b["text"] for b in blocks) + "\n", encoding="utf-8")
    return dest


def chunk(blocks: list[dict], target_chars: int = 2000,
          overlap_chars: int = 200) -> list[dict]:
    """Pack paragraphs into retrieval-sized chunks on paragraph boundaries."""
    chunks: list[dict] = []
    buf: list[dict] = []
    size = 0

    def flush():
        nonlocal buf, size
        if not buf:
            return
        text = "\n\n".join(b["text"] for b in buf)
        chunks.append({
            "id": len(chunks) + 1,
            "text": text,
            "chars": len(text),
            "page_start": buf[0]["page_start"],
            "page_end": buf[-1]["page_end"],
        })
        if overlap_chars > 0:
            tail: list[dict] = []
            carried = 0
            for b in reversed(buf):
                if carried >= overlap_chars:
                    break
                tail.insert(0, b)
                carried += len(b["text"])
            buf = tail
            size = carried
        else:
            buf, size = [], 0

    for block in blocks:
        if size and size + len(block["text"]) > target_chars:
            flush()
        buf.append(block)
        size += len(block["text"])
    # Final flush must not re-carry an overlap, or it loops.
    if buf:
        text = "\n\n".join(b["text"] for b in buf)
        if not chunks or chunks[-1]["text"] != text:
            chunks.append({
                "id": len(chunks) + 1,
                "text": text,
                "chars": len(text),
                "page_start": buf[0]["page_start"],
                "page_end": buf[-1]["page_end"],
            })
    return chunks


def write_jsonl(dest: Path, meta: dict, blocks: list[dict],
                target_chars: int = 2000, overlap_chars: int = 200) -> Path:
    with dest.open("w", encoding="utf-8") as fh:
        for c in chunk(blocks, target_chars, overlap_chars):
            record = {
                "chunk_id": c["id"],
                "title": meta.get("title", ""),
                "source": meta.get("source", ""),
                "asin": meta.get("asin", ""),
                "page_start": c["page_start"],
                "page_end": c["page_end"],
                "chars": c["chars"],
                "text": c["text"],
            }
            fh.write(json.dumps(record, ensure_ascii=False) + "\n")
    return dest


def build_meta(title: str, source: str, page_count: int,
               asin: str = "", extra: dict | None = None) -> dict:
    meta = {
        "title": title or "Untitled",
        "source": source,
        "asin": asin,
        "pages": page_count,
        "extracted_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "extractor": "kindle2ai (screen capture + OCR)",
    }
    meta.update(extra or {})
    return meta


def write_all(outdir: Path, meta: dict, blocks: list[dict], formats: list[str],
              target_chars: int = 2000, overlap_chars: int = 200) -> list[Path]:
    outdir = Path(outdir)
    outdir.mkdir(parents=True, exist_ok=True)
    stem = _slug(meta.get("title", "book"))
    written: list[Path] = []

    markdown_blocks = [
        {**b, "text": t}
        for b, t in zip(blocks, textclean.as_markdown([b["text"] for b in blocks]))
    ]

    if "md" in formats:
        written.append(write_markdown(outdir / f"{stem}.md", meta, markdown_blocks))
    if "txt" in formats:
        written.append(write_text(outdir / f"{stem}.txt", blocks))
    if "jsonl" in formats:
        written.append(write_jsonl(outdir / f"{stem}.jsonl", meta, markdown_blocks,
                                   target_chars, overlap_chars))
    return written
