#!/usr/bin/env python3
"""Conservative, auditable cleaner for MinerU technical Markdown.

The cleaner intentionally uses only Python's standard library so it can be
deployed on a large corpus without an environment-specific dependency stack.
It never overwrites an input file and writes outputs atomically.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import sys
import tempfile
from collections import Counter, defaultdict, deque
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from html.parser import HTMLParser
from pathlib import Path
from typing import Any, Iterable


VERSION = "1.0.0-pilot"
TABLE_RE = re.compile(r"<table\b[^>]*>.*?</table>", re.IGNORECASE | re.DOTALL)
IMAGE_LINE_RE = re.compile(r"^\s*!\[([^]]*)\]\(([^)]+)\)\s*$")
CAPTION_RE = re.compile(
    r"^\s*(?:Figure|Fig\.?|图)\s*[A-Za-z0-9IVXivx]+(?:\.[0-9]+)*\s*[:：.]",
    re.IGNORECASE,
)
HEADING_RE = re.compile(r"^(#{1,6})\s+(.+?)\s*$")
NUMBERED_HEADING_RE = re.compile(r"^(\d+(?:\.\d+)*)\s+(.+)$")
UNMARKED_NUMBERED_RE = re.compile(r"^(\d+(?:\.\d+){1,5})\s+(.{2,178})$")
CHAPTER_HEADING_RE = re.compile(r"^Chapter\s+(\d+)\s*$", re.IGNORECASE)
FENCE_RE = re.compile(r"^\s*```(.*)$")
HTML_TABLE_TAG_RE = re.compile(r"<table\b", re.IGNORECASE)
HTML_TAG_RE = re.compile(r"</?[A-Za-z][^>]*>")
FIGURE_REFERENCE_RE = re.compile(
    r"\b(?:Figure|Fig\.)\s+\d+(?:\.\d+)*\b", re.IGNORECASE
)


DEFAULT_CONFIG: dict[str, Any] = {
    "profile_name": "technical_document_conservative",
    "sections": {
        "remove_toc": True,
        "toc_start_headings": ["Contents", "Table of Contents"],
        "toc_end_headings": ["Prefaces", "Preface", "Chapter 1", "Introduction"],
        "toc_max_lines": 3000,
        "remove_index": True,
        "index_headings": ["Index"],
        "index_min_position_ratio": 0.75,
    },
    "images": {
        "mode": "drop_all",
        "remove_adjacent_caption": True,
    },
    "tables": {
        "convert_html_to_markdown": True,
        "expand_merged_cells": True,
        "max_cell_chars": 2000,
        "max_columns": 24,
        "max_rows": 500,
        "quarantine_suspicious": True,
    },
    "headings": {
        "merge_chapter_title": True,
        "repair_numbered_levels": True,
        "promote_unmarked_numbered_headings": True,
    },
    "code": {
        "language_policy": "strip_all",
        "flatten_blocks_containing_numbered_headings": True,
    },
    "normalization": {
        "max_blank_lines": 2,
        "remove_soft_hyphen": True,
        "remove_zero_width": True,
        "normalize_unicode_bullets": True,
    },
    "quality": {
        "minimum_character_retention": 0.60,
        "maximum_character_retention": 1.10,
        "require_idempotence": True,
        "suspicious_tables_require_review": True,
    },
}


class CleaningError(RuntimeError):
    """Raised when an input or output violates a hard safety constraint."""


@dataclass
class Cell:
    text: str
    rowspan: int = 1
    colspan: int = 1


@dataclass
class TableConversion:
    table_index: int
    source_line: int
    source_sha256: str
    headers: list[str]
    rows: list[list[str]]
    markdown: str
    row_count: int
    column_count: int
    duplicate_first_column_values: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)


@dataclass
class CleaningResult:
    text: str
    report: dict[str, Any]
    tables: list[dict[str, Any]]
    images: list[dict[str, Any]]
    quarantined_tables: list[dict[str, Any]]


class SimpleTableParser(HTMLParser):
    """Parse a single HTML table while retaining meaningful inline markup."""

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.rows: list[list[Cell]] = []
        self.current_row: list[Cell] | None = None
        self.current_cell: list[str] | None = None
        self.current_rowspan = 1
        self.current_colspan = 1

    def handle_starttag(
        self, tag: str, attrs: list[tuple[str, str | None]]
    ) -> None:
        tag = tag.lower()
        attr_map = {key.lower(): value for key, value in attrs}
        if tag == "tr":
            self.current_row = []
        elif tag in {"td", "th"}:
            self.current_cell = []
            self.current_rowspan = _positive_int(attr_map.get("rowspan"), 1)
            self.current_colspan = _positive_int(attr_map.get("colspan"), 1)
        elif self.current_cell is not None and tag == "br":
            self.current_cell.append("<br>")
        elif self.current_cell is not None and tag in {"sup", "sub"}:
            self.current_cell.append(f"<{tag}>")

    def handle_endtag(self, tag: str) -> None:
        tag = tag.lower()
        if self.current_cell is not None and tag in {"sup", "sub"}:
            self.current_cell.append(f"</{tag}>")
        elif tag in {"td", "th"} and self.current_cell is not None:
            if self.current_row is None:
                self.current_row = []
            self.current_row.append(
                Cell(
                    text=_normalize_cell("".join(self.current_cell)),
                    rowspan=self.current_rowspan,
                    colspan=self.current_colspan,
                )
            )
            self.current_cell = None
        elif tag == "tr" and self.current_row is not None:
            self.rows.append(self.current_row)
            self.current_row = None

    def handle_data(self, data: str) -> None:
        if self.current_cell is not None:
            self.current_cell.append(data)


def _positive_int(value: str | None, default: int) -> int:
    try:
        parsed = int(value or default)
    except (TypeError, ValueError):
        return default
    return parsed if parsed > 0 else default


def _normalize_cell(value: str) -> str:
    value = value.replace("\r\n", "\n").replace("\r", "\n")
    value = re.sub(r"[ \t\f\v]+", " ", value)
    value = re.sub(r"\s*\n\s*", "<br>", value)
    value = re.sub(r"(?:<br>\s*){2,}", "<br>", value)
    return value.strip()


def _escape_markdown_cell(value: str) -> str:
    return value.replace("|", r"\|").replace("\n", "<br>").strip()


def _sha256_text(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def _config_sha256(config: dict[str, Any]) -> str:
    canonical = json.dumps(config, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return _sha256_text(canonical)


def _deep_merge(base: dict[str, Any], override: dict[str, Any]) -> dict[str, Any]:
    merged: dict[str, Any] = {}
    for key, value in base.items():
        merged[key] = _deep_merge(value, {}) if isinstance(value, dict) else value
    for key, value in override.items():
        if isinstance(value, dict) and isinstance(merged.get(key), dict):
            merged[key] = _deep_merge(merged[key], value)
        else:
            merged[key] = value
    return merged


def load_config(path: Path | None) -> dict[str, Any]:
    if path is None:
        return _deep_merge(DEFAULT_CONFIG, {})
    try:
        override = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise CleaningError(f"无法读取配置 {path}: {exc}") from exc
    if not isinstance(override, dict):
        raise CleaningError("配置文件根节点必须是 JSON object")
    config = _deep_merge(DEFAULT_CONFIG, override)
    if config["images"]["mode"] not in {"drop_all", "keep_caption", "placeholder"}:
        raise CleaningError("images.mode 必须是 drop_all、keep_caption 或 placeholder")
    if config["code"]["language_policy"] not in {"strip_all", "preserve"}:
        raise CleaningError("code.language_policy 必须是 strip_all 或 preserve")
    return config


def normalize_input(text: str, config: dict[str, Any]) -> tuple[str, Counter[str]]:
    actions: Counter[str] = Counter()
    if text.startswith("\ufeff"):
        text = text.removeprefix("\ufeff")
        actions["removed_bom"] += 1
    crlf = text.count("\r\n")
    lone_cr = text.count("\r") - crlf
    if crlf or lone_cr:
        actions["normalized_line_endings"] += crlf + lone_cr
        text = text.replace("\r\n", "\n").replace("\r", "\n")
    if config["normalization"]["remove_soft_hyphen"]:
        count = text.count("\u00ad")
        if count:
            text = text.replace("\u00ad", "")
            actions["removed_soft_hyphens"] += count
    if config["normalization"]["remove_zero_width"]:
        for char in ("\u200b", "\u200c", "\u200d", "\u2060", "\ufeff"):
            count = text.count(char)
            if count:
                text = text.replace(char, "")
                actions["removed_zero_width_chars"] += count
    return text, actions


def profile_text(text: str) -> dict[str, Any]:
    lines = text.splitlines()
    fence_lines = [line for line in lines if FENCE_RE.match(line)]
    languages: Counter[str] = Counter()
    in_fence = False
    for line in lines:
        match = FENCE_RE.match(line)
        if not match:
            continue
        if not in_fence:
            language = match.group(1).strip() or "(none)"
            languages[language] += 1
        in_fence = not in_fence
    return {
        "characters": len(text),
        "bytes_utf8": len(text.encode("utf-8")),
        "lines": len(lines),
        "headings": sum(bool(HEADING_RE.match(line)) for line in lines),
        "image_lines": sum(bool(IMAGE_LINE_RE.match(line)) for line in lines),
        "figure_captions": sum(bool(CAPTION_RE.match(line)) for line in lines),
        "html_tables": len(TABLE_RE.findall(text)),
        "html_tags": len(HTML_TAG_RE.findall(text)),
        "code_fence_lines": len(fence_lines),
        "code_fences_balanced": len(fence_lines) % 2 == 0,
        "code_languages": dict(sorted(languages.items())),
        "figure_references": len(FIGURE_REFERENCE_RE.findall(text)),
        "unmarked_numbered_heading_candidates": _count_unmarked_numbered_headings(text),
    }


def _heading_text(line: str) -> str | None:
    match = HEADING_RE.match(line)
    return match.group(2).strip() if match else None


def remove_front_toc(
    text: str, config: dict[str, Any], actions: Counter[str]
) -> str:
    section = config["sections"]
    if not section["remove_toc"]:
        return text
    lines = text.split("\n")
    starts = {value.casefold() for value in section["toc_start_headings"]}
    ends = {value.casefold() for value in section["toc_end_headings"]}
    start_index: int | None = None
    for index, line in enumerate(lines[: min(len(lines), 500)]):
        heading = _heading_text(line)
        if heading and heading.casefold() in starts:
            start_index = index
            break
    if start_index is None:
        return text
    max_end = min(len(lines), start_index + int(section["toc_max_lines"]))
    end_index: int | None = None
    for index in range(start_index + 1, max_end):
        heading = _heading_text(lines[index])
        if heading and heading.casefold() in ends:
            end_index = index
            break
    if end_index is None:
        actions["toc_detected_not_removed_no_safe_end"] += 1
        return text
    actions["removed_toc_blocks"] += 1
    actions["removed_toc_lines"] += end_index - start_index
    return "\n".join(lines[:start_index] + lines[end_index:])


def remove_trailing_index(
    text: str, config: dict[str, Any], actions: Counter[str]
) -> str:
    section = config["sections"]
    if not section["remove_index"]:
        return text
    lines = text.split("\n")
    allowed = {value.casefold() for value in section["index_headings"]}
    min_index = int(len(lines) * float(section["index_min_position_ratio"]))
    for index in range(min_index, len(lines)):
        heading = _heading_text(lines[index])
        if heading and heading.casefold() in allowed:
            actions["removed_index_blocks"] += 1
            actions["removed_index_lines"] += len(lines) - index
            return "\n".join(lines[:index])
    return text


def inventory_images(text: str) -> dict[str, deque[dict[str, Any]]]:
    result: dict[str, deque[dict[str, Any]]] = defaultdict(deque)
    lines = text.split("\n")
    for index, line in enumerate(lines):
        match = IMAGE_LINE_RE.match(line)
        if not match:
            continue
        caption = ""
        if index + 1 < len(lines) and CAPTION_RE.match(lines[index + 1]):
            caption = lines[index + 1].strip()
        result[line.strip()].append(
            {
                "source_line": index + 1,
                "alt_text": match.group(1),
                "image_path": match.group(2),
                "caption": caption,
                "source_markdown": line.strip(),
            }
        )
    return result


def process_images(
    text: str,
    config: dict[str, Any],
    inventory: dict[str, deque[dict[str, Any]]],
    actions: Counter[str],
) -> tuple[str, list[dict[str, Any]]]:
    mode = config["images"]["mode"]
    remove_caption = bool(config["images"]["remove_adjacent_caption"])
    lines = text.split("\n")
    output: list[str] = []
    records: list[dict[str, Any]] = []
    index = 0
    while index < len(lines):
        line = lines[index]
        match = IMAGE_LINE_RE.match(line)
        if not match:
            output.append(line)
            index += 1
            continue
        key = line.strip()
        if inventory.get(key):
            record = inventory[key].popleft()
        else:
            record = {
                "source_line": None,
                "alt_text": match.group(1),
                "image_path": match.group(2),
                "caption": "",
                "source_markdown": key,
            }
        caption = ""
        if index + 1 < len(lines) and CAPTION_RE.match(lines[index + 1]):
            caption = lines[index + 1].strip()
            record["caption"] = record.get("caption") or caption
        record["action"] = mode
        records.append(record)
        actions["processed_images"] += 1
        if mode == "placeholder":
            description = caption or match.group(1).strip() or "technical figure"
            output.append(f"[Figure omitted: {description}]")
        if caption and remove_caption and mode == "drop_all":
            actions["removed_figure_captions"] += 1
            index += 1
        elif caption and mode in {"keep_caption", "placeholder"}:
            output.append(caption)
            index += 1
        index += 1
    return "\n".join(output), records


def inventory_table_lines(text: str) -> dict[str, deque[int]]:
    result: dict[str, deque[int]] = defaultdict(deque)
    for match in TABLE_RE.finditer(text):
        result[_sha256_text(match.group(0))].append(text.count("\n", 0, match.start()) + 1)
    return result


def _expand_table(rows: list[list[Cell]], repeat_merged: bool) -> list[list[str]]:
    occupied: dict[tuple[int, int], str] = {}
    max_row = -1
    max_col = -1
    for row_index, row in enumerate(rows):
        column_index = 0
        for cell in row:
            while (row_index, column_index) in occupied:
                column_index += 1
            for row_offset in range(cell.rowspan):
                for column_offset in range(cell.colspan):
                    target = (row_index + row_offset, column_index + column_offset)
                    value = cell.text if repeat_merged or target == (row_index, column_index) else ""
                    occupied[target] = value
                    max_row = max(max_row, target[0])
                    max_col = max(max_col, target[1])
            column_index += cell.colspan
    if max_row < 0 or max_col < 0:
        return []
    return [
        [occupied.get((row_index, column_index), "") for column_index in range(max_col + 1)]
        for row_index in range(max_row + 1)
    ]


def convert_one_table(
    html: str,
    table_index: int,
    source_line: int,
    config: dict[str, Any],
) -> TableConversion:
    parser = SimpleTableParser()
    warnings: list[str] = []
    try:
        parser.feed(html)
        parser.close()
    except Exception as exc:  # HTMLParser errors are rare, preserve for audit.
        warnings.append(f"html_parse_error:{type(exc).__name__}")
    grid = _expand_table(
        parser.rows, bool(config["tables"]["expand_merged_cells"])
    )
    if not grid:
        warnings.append("empty_or_unparseable_table")
        grid = [["Unparseable table"]]
    width = max(len(row) for row in grid)
    for row in grid:
        row.extend([""] * (width - len(row)))
    headers = grid[0]
    for column, value in enumerate(headers):
        if not value:
            headers[column] = f"Column {column + 1}"
            warnings.append("generated_empty_header")
    data_rows = grid[1:]
    max_cell_chars = int(config["tables"]["max_cell_chars"])
    if any(len(cell) > max_cell_chars for row in grid for cell in row):
        warnings.append("oversized_cell")
    if width > int(config["tables"]["max_columns"]):
        warnings.append("too_many_columns")
    if len(grid) > int(config["tables"]["max_rows"]):
        warnings.append("too_many_rows")
    if re.search(r"\b(?:Flag\s*){20,}", html, re.IGNORECASE):
        warnings.append("probable_mineru_cell_collapse")
    first_values = [row[0] for row in data_rows if row and row[0]]
    duplicate_values = sorted(
        value for value, count in Counter(first_values).items() if count > 1
    )
    escaped_headers = [_escape_markdown_cell(value) for value in headers]
    separator = ["---:" if value.strip() == "#" else "---" for value in headers]
    markdown_lines = [
        "| " + " | ".join(escaped_headers) + " |",
        "| " + " | ".join(separator) + " |",
    ]
    for row in data_rows:
        markdown_lines.append(
            "| " + " | ".join(_escape_markdown_cell(value) for value in row) + " |"
        )
    return TableConversion(
        table_index=table_index,
        source_line=source_line,
        source_sha256=_sha256_text(html),
        headers=headers,
        rows=data_rows,
        markdown="\n".join(markdown_lines),
        row_count=len(data_rows),
        column_count=width,
        duplicate_first_column_values=duplicate_values,
        warnings=sorted(set(warnings)),
    )


def _fenced_line_numbers(text: str) -> set[int]:
    result: set[int] = set()
    in_fence = False
    for index, line in enumerate(text.split("\n"), start=1):
        if FENCE_RE.match(line):
            result.add(index)
            in_fence = not in_fence
            continue
        if in_fence:
            result.add(index)
    return result


def convert_tables(
    text: str,
    config: dict[str, Any],
    source_lines: dict[str, deque[int]],
    actions: Counter[str],
) -> tuple[str, list[TableConversion], list[dict[str, Any]]]:
    if not config["tables"]["convert_html_to_markdown"]:
        return text, [], []
    fenced_lines = _fenced_line_numbers(text)
    output: list[str] = []
    conversions: list[TableConversion] = []
    quarantined: list[dict[str, Any]] = []
    cursor = 0
    table_index = 0
    for match in TABLE_RE.finditer(text):
        current_line = text.count("\n", 0, match.start()) + 1
        if current_line in fenced_lines:
            continue
        output.append(text[cursor : match.start()])
        html = match.group(0)
        source_hash = _sha256_text(html)
        if source_lines.get(source_hash):
            source_line = source_lines[source_hash].popleft()
        else:
            source_line = current_line
        table_index += 1
        converted = convert_one_table(
            html=html,
            table_index=table_index,
            source_line=source_line,
            config=config,
        )
        output.append(converted.markdown)
        conversions.append(converted)
        actions["converted_html_tables"] += 1
        if converted.duplicate_first_column_values:
            actions["tables_with_duplicate_first_column"] += 1
        if converted.warnings:
            actions["tables_with_conversion_warnings"] += 1
        critical_warnings = sorted(
            set(converted.warnings)
            & {
                "empty_or_unparseable_table",
                "oversized_cell",
                "probable_mineru_cell_collapse",
                "too_many_columns",
                "too_many_rows",
            }
        )
        critical_warnings.extend(
            warning
            for warning in converted.warnings
            if warning.startswith("html_parse_error:")
        )
        critical_warnings = sorted(set(critical_warnings))
        if critical_warnings:
            actions["suspicious_tables"] += 1
            if config["tables"]["quarantine_suspicious"]:
                quarantined.append(
                    {
                        "table_index": table_index,
                        "source_line": source_line,
                        "source_sha256": source_hash,
                        "warnings": critical_warnings,
                        "raw_html": html,
                        "converted_markdown": converted.markdown,
                    }
                )
        cursor = match.end()
    output.append(text[cursor:])
    return "".join(output), conversions, quarantined


def merge_chapter_titles(
    text: str, config: dict[str, Any], actions: Counter[str]
) -> str:
    if not config["headings"]["merge_chapter_title"]:
        return text
    lines = text.split("\n")
    output: list[str] = []
    index = 0
    in_fence = False
    while index < len(lines):
        line = lines[index]
        if FENCE_RE.match(line):
            in_fence = not in_fence
            output.append(line)
            index += 1
            continue
        heading = _heading_text(line) if not in_fence else None
        chapter_match = CHAPTER_HEADING_RE.match(heading or "")
        if not chapter_match:
            output.append(line)
            index += 1
            continue
        next_index = index + 1
        while next_index < len(lines) and not lines[next_index].strip():
            next_index += 1
        next_heading = _heading_text(lines[next_index]) if next_index < len(lines) else None
        if next_heading and not NUMBERED_HEADING_RE.match(next_heading):
            output.append(f"# {chapter_match.group(1)} {next_heading}")
            actions["merged_chapter_title_pairs"] += 1
            index = next_index + 1
        else:
            output.append(f"# {heading}")
            actions["promoted_chapter_headings"] += 1
            index += 1
    return "\n".join(output)


def repair_numbered_headings(
    text: str, config: dict[str, Any], actions: Counter[str]
) -> str:
    if not config["headings"]["repair_numbered_levels"]:
        return text
    output: list[str] = []
    in_fence = False
    for line in text.split("\n"):
        if FENCE_RE.match(line):
            in_fence = not in_fence
            output.append(line)
            continue
        match = HEADING_RE.match(line) if not in_fence else None
        if not match:
            output.append(line)
            continue
        numbered = NUMBERED_HEADING_RE.match(match.group(2).strip())
        if not numbered:
            output.append(line)
            continue
        level = min(6, numbered.group(1).count(".") + 1)
        repaired = f"{'#' * level} {numbered.group(1)} {numbered.group(2)}"
        if repaired != line:
            actions["repaired_numbered_headings"] += 1
        output.append(repaired)
    return "\n".join(output)


def _is_unmarked_numbered_heading(
    lines: list[str], index: int, in_fence: bool
) -> re.Match[str] | None:
    if in_fence or index == 0 or index + 1 >= len(lines):
        return None
    if lines[index - 1].strip() or lines[index + 1].strip():
        return None
    match = UNMARKED_NUMBERED_RE.match(lines[index].strip())
    if not match:
        return None
    title = match.group(2).strip()
    if not any(char.isalpha() for char in title):
        return None
    if title.endswith((".", "。", "!", "！", "?", "？", ";", "；")):
        return None
    return match


def _count_unmarked_numbered_headings(text: str) -> int:
    lines = text.split("\n")
    count = 0
    in_fence = False
    for index, line in enumerate(lines):
        if FENCE_RE.match(line):
            in_fence = not in_fence
            continue
        if _is_unmarked_numbered_heading(lines, index, in_fence):
            count += 1
    return count


def promote_unmarked_numbered_headings(
    text: str, config: dict[str, Any], actions: Counter[str]
) -> str:
    if not config["headings"]["promote_unmarked_numbered_headings"]:
        return text
    lines = text.split("\n")
    output: list[str] = []
    in_fence = False
    for index, line in enumerate(lines):
        if FENCE_RE.match(line):
            in_fence = not in_fence
            output.append(line)
            continue
        match = _is_unmarked_numbered_heading(lines, index, in_fence)
        if not match:
            output.append(line)
            continue
        level = min(6, match.group(1).count(".") + 1)
        output.append(f"{'#' * level} {match.group(1)} {match.group(2).strip()}")
        actions["promoted_unmarked_numbered_headings"] += 1
    return "\n".join(output)


def normalize_code_fences(
    text: str, config: dict[str, Any], actions: Counter[str]
) -> str:
    if config["code"]["language_policy"] == "preserve":
        return text
    output: list[str] = []
    in_fence = False
    for line in text.split("\n"):
        match = FENCE_RE.match(line)
        if not match:
            output.append(line)
            continue
        info = match.group(1).strip()
        if not in_fence and info:
            actions["removed_code_language_labels"] += 1
        output.append("```")
        in_fence = not in_fence
    return "\n".join(output)


def repair_overextended_code_blocks(
    text: str, config: dict[str, Any], actions: Counter[str]
) -> str:
    """Flatten a fence that swallowed a numbered document heading.

    MinerU occasionally encloses prose, the next section heading and examples in
    one giant code block. Removing only the outer fence preserves every byte of
    content and is safer than guessing multiple new code boundaries.
    """
    if not config["code"]["flatten_blocks_containing_numbered_headings"]:
        return text
    lines = text.split("\n")
    output: list[str] = []
    index = 0
    while index < len(lines):
        if not FENCE_RE.match(lines[index]):
            output.append(lines[index])
            index += 1
            continue
        close_index = index + 1
        while close_index < len(lines) and not FENCE_RE.match(lines[close_index]):
            close_index += 1
        if close_index >= len(lines):
            output.extend(lines[index:])
            break
        block = lines[index + 1 : close_index]
        padded = [""] + block + [""]
        swallowed_heading = any(
            _is_unmarked_numbered_heading(padded, block_index, False)
            for block_index in range(1, len(padded) - 1)
        )
        if swallowed_heading:
            output.extend(block)
            actions["flattened_overextended_code_blocks"] += 1
        else:
            output.extend(lines[index : close_index + 1])
        index = close_index + 1
    return "\n".join(output)


def normalize_lines(
    text: str, config: dict[str, Any], actions: Counter[str]
) -> str:
    max_blanks = int(config["normalization"]["max_blank_lines"])
    normalize_bullets = bool(config["normalization"]["normalize_unicode_bullets"])
    output: list[str] = []
    blank_count = 0
    in_fence = False
    for raw_line in text.split("\n"):
        if FENCE_RE.match(raw_line):
            in_fence = not in_fence
            line = raw_line
        elif in_fence:
            line = raw_line
        else:
            line = raw_line.rstrip()
            if normalize_bullets:
                replaced = re.sub(r"^(\s*)[•◦▪●]\s+", r"\1- ", line)
                if replaced != line:
                    actions["normalized_unicode_bullets"] += 1
                line = replaced
        if not line.strip() and not in_fence:
            blank_count += 1
            if blank_count > max_blanks:
                actions["removed_excess_blank_lines"] += 1
                continue
        else:
            blank_count = 0
        output.append(line)
    return "\n".join(output).strip() + "\n"


def _status_from_report(report: dict[str, Any], config: dict[str, Any]) -> str:
    after = report["after"]
    quality = report["quality"]
    if not after["code_fences_balanced"] or after["html_tables"] or after["image_lines"]:
        return "REJECT"
    if not quality["idempotent"] and config["quality"]["require_idempotence"]:
        return "REJECT"
    retention = quality["character_retention_ratio"]
    if not (
        float(config["quality"]["minimum_character_retention"])
        <= retention
        <= float(config["quality"]["maximum_character_retention"])
    ):
        return "REVIEW"
    if quality["suspicious_table_count"] and config["quality"][
        "suspicious_tables_require_review"
    ]:
        return "REVIEW"
    if after["figure_references"]:
        return "PASS_WARN"
    return "PASS"


def transform_text(
    text: str,
    config: dict[str, Any],
    *,
    source_name: str,
    check_idempotence: bool = True,
) -> CleaningResult:
    normalized, actions = normalize_input(text, config)
    before = profile_text(normalized)
    image_inventory = inventory_images(normalized)
    table_source_lines = inventory_table_lines(normalized)

    working = remove_front_toc(normalized, config, actions)
    working = remove_trailing_index(working, config, actions)
    working, images = process_images(working, config, image_inventory, actions)
    working, table_conversions, quarantined = convert_tables(
        working, config, table_source_lines, actions
    )
    working = repair_overextended_code_blocks(working, config, actions)
    working = merge_chapter_titles(working, config, actions)
    working = promote_unmarked_numbered_headings(working, config, actions)
    working = repair_numbered_headings(working, config, actions)
    working = normalize_code_fences(working, config, actions)
    working = normalize_lines(working, config, actions)
    after = profile_text(working)

    idempotent = True
    if check_idempotence:
        second = transform_text(
            working,
            config,
            source_name=source_name,
            check_idempotence=False,
        )
        idempotent = second.text == working

    report: dict[str, Any] = {
        "cleaner_version": VERSION,
        "config_sha256": _config_sha256(config),
        "profile_name": config["profile_name"],
        "source": source_name,
        "generated_at_utc": datetime.now(timezone.utc).isoformat(),
        "source_sha256": _sha256_text(normalized),
        "output_sha256": _sha256_text(working),
        "before": before,
        "after": after,
        "actions": dict(sorted(actions.items())),
        "quality": {
            "character_retention_ratio": round(
                after["characters"] / before["characters"], 6
            )
            if before["characters"]
            else 0.0,
            "idempotent": idempotent,
            "converted_table_count": len(table_conversions),
            "suspicious_table_count": len(quarantined),
            "processed_image_count": len(images),
            "residual_html_table_count": after["html_tables"],
            "residual_image_count": after["image_lines"],
            "dangling_figure_reference_count": after["figure_references"],
        },
    }
    report["status"] = _status_from_report(report, config)
    table_records = []
    for conversion in table_conversions:
        record = asdict(conversion)
        record.pop("markdown", None)
        table_records.append(record)
    return CleaningResult(
        text=working,
        report=report,
        tables=table_records,
        images=images,
        quarantined_tables=quarantined,
    )


def _read_utf8(path: Path) -> str:
    try:
        return path.read_text(encoding="utf-8", errors="strict")
    except UnicodeDecodeError as exc:
        raise CleaningError(f"输入不是合法 UTF-8: {path}: {exc}") from exc


def _atomic_write_text(path: Path, content: str, overwrite: bool) -> None:
    path = path.resolve()
    if path.exists() and not overwrite:
        raise CleaningError(f"输出已存在；如需覆盖请使用 --overwrite: {path}")
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temp_name = tempfile.mkstemp(
        prefix=f".{path.name}.", suffix=".tmp", dir=path.parent
    )
    temp_path = Path(temp_name)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8", newline="\n") as handle:
            handle.write(content)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temp_path, path)
    finally:
        temp_path.unlink(missing_ok=True)


def _atomic_write_json(path: Path, value: Any, overwrite: bool) -> None:
    content = json.dumps(value, ensure_ascii=False, indent=2) + "\n"
    _atomic_write_text(path, content, overwrite)


def _write_jsonl(path: Path, records: Iterable[dict[str, Any]], overwrite: bool) -> None:
    content = "".join(
        json.dumps(record, ensure_ascii=False, separators=(",", ":")) + "\n"
        for record in records
    )
    _atomic_write_text(path, content, overwrite)


def clean_one_file(
    input_path: Path,
    output_path: Path,
    report_path: Path,
    artifact_dir: Path,
    config: dict[str, Any],
    overwrite: bool,
) -> dict[str, Any]:
    if input_path.resolve() == output_path.resolve():
        raise CleaningError("禁止覆盖源文件")
    text = _read_utf8(input_path)
    result = transform_text(text, config, source_name=str(input_path.resolve()))
    _atomic_write_text(output_path, result.text, overwrite)
    _atomic_write_json(report_path, result.report, overwrite)
    stem = input_path.stem
    _write_jsonl(artifact_dir / f"{stem}.tables.jsonl", result.tables, overwrite)
    _write_jsonl(artifact_dir / f"{stem}.images.jsonl", result.images, overwrite)
    _write_jsonl(
        artifact_dir / f"{stem}.quarantined_tables.jsonl",
        result.quarantined_tables,
        overwrite,
    )
    return result.report


def command_profile(args: argparse.Namespace) -> int:
    reports = []
    for path in args.inputs:
        text = _read_utf8(path)
        normalized, _ = normalize_input(text, load_config(args.config))
        report = {"path": str(path.resolve()), **profile_text(normalized)}
        reports.append(report)
    payload = {
        "cleaner_version": VERSION,
        "document_count": len(reports),
        "documents": reports,
    }
    if args.output:
        _atomic_write_json(args.output, payload, args.overwrite)
    print(json.dumps(payload, ensure_ascii=False, indent=2))
    return 0


def command_clean(args: argparse.Namespace) -> int:
    config = load_config(args.config)
    report = clean_one_file(
        args.input_file,
        args.output_file,
        args.report,
        args.artifact_dir,
        config,
        args.overwrite,
    )
    print(json.dumps(report, ensure_ascii=False, indent=2))
    return 0 if report["status"] != "REJECT" else 2


def command_batch(args: argparse.Namespace) -> int:
    config = load_config(args.config)
    input_root = args.input_root.resolve()
    output_root = args.output_root.resolve()
    if input_root == output_root or input_root in output_root.parents:
        raise CleaningError("批量输出目录不能位于输入目录内部")
    files = sorted(input_root.rglob("*.md"))
    if not files:
        raise CleaningError(f"没有找到 Markdown 文件: {input_root}")
    reports_root = output_root / "reports"
    cleaned_root = output_root / "cleaned"
    artifacts_root = output_root / "artifacts"

    def run(path: Path) -> tuple[dict[str, Any], bool]:
        relative = path.relative_to(input_root)
        output_path = cleaned_root / relative
        report_path = reports_root / relative.with_suffix(".report.json")
        artifact_dir = artifacts_root / relative.parent
        artifact_stem = artifact_dir / path.stem
        expected_artifacts = [
            artifact_stem.with_name(f"{path.stem}.tables.jsonl"),
            artifact_stem.with_name(f"{path.stem}.images.jsonl"),
            artifact_stem.with_name(f"{path.stem}.quarantined_tables.jsonl"),
        ]
        if args.resume and output_path.is_file() and report_path.is_file() and all(
            artifact.is_file() for artifact in expected_artifacts
        ):
            try:
                old_report = json.loads(report_path.read_text(encoding="utf-8"))
                source_text, _ = normalize_input(_read_utf8(path), config)
                unchanged = (
                    old_report.get("cleaner_version") == VERSION
                    and old_report.get("config_sha256") == _config_sha256(config)
                    and old_report.get("source_sha256") == _sha256_text(source_text)
                )
                if unchanged:
                    return old_report, True
            except (OSError, json.JSONDecodeError):
                pass
        report = clean_one_file(
            path,
            output_path,
            report_path,
            artifact_dir,
            config,
            args.overwrite or args.resume,
        )
        return report, False

    reports: list[dict[str, Any]] = []
    failures: list[dict[str, str]] = []
    resumed_documents = 0
    with ThreadPoolExecutor(max_workers=max(1, args.workers)) as executor:
        futures = {executor.submit(run, path): path for path in files}
        for future in as_completed(futures):
            path = futures[future]
            try:
                report, resumed = future.result()
                reports.append(report)
                resumed_documents += int(resumed)
            except Exception as exc:
                failures.append({"source": str(path), "error": str(exc)})

    status_counts = Counter(report["status"] for report in reports)
    summary = {
        "cleaner_version": VERSION,
        "profile_name": config["profile_name"],
        "input_root": str(input_root),
        "output_root": str(output_root),
        "discovered_documents": len(files),
        "completed_documents": len(reports),
        "resumed_documents": resumed_documents,
        "failed_documents": len(failures),
        "status_counts": dict(sorted(status_counts.items())),
        "total_tables_converted": sum(
            report["quality"]["converted_table_count"] for report in reports
        ),
        "total_suspicious_tables": sum(
            report["quality"]["suspicious_table_count"] for report in reports
        ),
        "total_images_processed": sum(
            report["quality"]["processed_image_count"] for report in reports
        ),
        "failures": failures,
    }
    _atomic_write_json(
        output_root / "summary.json", summary, args.overwrite or args.resume
    )
    print(json.dumps(summary, ensure_ascii=False, indent=2))
    return 0 if not failures and not status_counts.get("REJECT") else 2


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="企业级技术 Markdown 保真清洗工具（MinerU 适配）"
    )
    parser.add_argument("--version", action="version", version=VERSION)
    subparsers = parser.add_subparsers(dest="command", required=True)

    profile = subparsers.add_parser("profile", help="只读画像，不清洗")
    profile.add_argument("inputs", nargs="+", type=Path)
    profile.add_argument("--config", type=Path)
    profile.add_argument("--output", type=Path)
    profile.add_argument("--overwrite", action="store_true")
    profile.set_defaults(func=command_profile)

    clean = subparsers.add_parser("clean", help="清洗单个 Markdown")
    clean.add_argument("--input-file", type=Path, required=True)
    clean.add_argument("--output-file", type=Path, required=True)
    clean.add_argument("--report", type=Path, required=True)
    clean.add_argument("--artifact-dir", type=Path, required=True)
    clean.add_argument("--config", type=Path)
    clean.add_argument("--overwrite", action="store_true")
    clean.set_defaults(func=command_clean)

    batch = subparsers.add_parser("batch", help="递归批量清洗 Markdown")
    batch.add_argument("--input-root", type=Path, required=True)
    batch.add_argument("--output-root", type=Path, required=True)
    batch.add_argument("--config", type=Path)
    batch.add_argument("--workers", type=int, default=max(1, min(8, os.cpu_count() or 1)))
    batch.add_argument("--overwrite", action="store_true")
    batch.add_argument(
        "--resume",
        action="store_true",
        help="跳过源哈希、配置哈希和清洗版本均未变化的完整输出",
    )
    batch.set_defaults(func=command_batch)
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        return int(args.func(args))
    except CleaningError as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
