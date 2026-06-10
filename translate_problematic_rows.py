#!/usr/bin/env python3
from __future__ import annotations

import argparse
import csv
import json
import os
import re
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any


CHUNK_TRANSLATION_PROMPT_TEMPLATE = """You are a specialist translation agent for mental health datasets.

Translate the provided {source_language} text fragment into {target_language}.

Target locale:
- {target_locale_description}

Rules:
1. Preserve meaning, tone, emotional intensity, and point of view.
2. Use natural {target_language} that is clinically sensitive and non-stigmatizing.
3. Do not add diagnoses, advice, or commentary.
4. Return only the translated fragment text.
5. Do not use markdown fences, CSV, JSON, labels, or explanations.
"""


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Second-pass translator for problematic long rows. Splits text into chunks and fills the output CSV."
    )
    parser.add_argument("--provider", choices=["ollama"], default="ollama")
    parser.add_argument("--model", default="gemma3:4b")
    parser.add_argument("--source-locale", default="en-US")
    parser.add_argument("--target-locale", default="es-ES")
    parser.add_argument("--base-url", default="", help="For Ollama the default is http://localhost:11434")
    parser.add_argument("--problematic-file", default=".translation_problematic_rows.csv")
    parser.add_argument("--output", default="")
    parser.add_argument("--translated-col", default="")
    parser.add_argument("--request-timeout", type=float, default=120.0)
    parser.add_argument("--chunk-max-chars", type=int, default=500)
    parser.add_argument("--max-rows", type=int, default=0, help="0 means process all problematic rows.")
    parser.add_argument("--sleep-seconds", type=float, default=0.5)
    return parser.parse_args()


LOCALE_ALIASES = {
    "en-uk": "en-GB",
    "en-gb": "en-GB",
    "en-us": "en-US",
    "es-es": "es-ES",
    "es-ar": "es-AR",
    "es-mx": "es-MX",
}


LANGUAGE_NAMES = {
    "en": "English",
    "es": "Spanish",
    "pt": "Portuguese",
    "fr": "French",
    "it": "Italian",
    "de": "German",
}


REGION_NAMES = {
    "US": "United States",
    "GB": "United Kingdom",
    "ES": "Spain",
    "AR": "Argentina",
    "MX": "Mexico",
}


def normalize_locale(locale: str) -> str:
    raw = locale.strip()
    if not raw:
        raise ValueError("Locale cannot be empty.")
    alias = LOCALE_ALIASES.get(raw.lower(), raw)
    parts = alias.replace("_", "-").split("-")
    if len(parts) != 2:
        raise ValueError(f"Invalid locale format: {locale!r}. Use forms like es-ES or en-GB.")
    return f"{parts[0].lower()}-{parts[1].upper()}"


def locale_to_metadata(locale: str) -> dict[str, str]:
    normalized = normalize_locale(locale)
    language_code, region_code = normalized.split("-")
    language_name = LANGUAGE_NAMES.get(language_code, language_code)
    region_name = REGION_NAMES.get(region_code, region_code)
    return {
        "locale": normalized,
        "language_code": language_code,
        "region_code": region_code,
        "language_name": language_name,
        "locale_description": f"{language_name} as used in {region_name} ({normalized})",
    }


def default_translated_col_for_locale(target_locale: str) -> str:
    metadata = locale_to_metadata(target_locale)
    return f"statement_{metadata['language_code']}_{metadata['region_code'].lower()}"


def default_output_file_for_locale(target_locale: str) -> Path:
    locale_label = normalize_locale(target_locale)
    return Path(f"Combined Data {locale_label}.csv")


def build_chunk_prompt(source_locale: str, target_locale: str) -> str:
    source = locale_to_metadata(source_locale)
    target = locale_to_metadata(target_locale)
    return CHUNK_TRANSLATION_PROMPT_TEMPLATE.format(
        source_language=source["language_name"],
        target_language=target["language_name"],
        target_locale_description=target["locale_description"],
    )


def build_client(args: argparse.Namespace) -> str:
    if args.provider != "ollama":
        raise ValueError("This refactored script currently supports only --provider ollama.")
    return args.base_url or "http://localhost:11434"


def ollama_generate(base_url: str, model: str, prompt: str, timeout: float) -> str:
    payload = json.dumps(
        {
            "model": model,
            "prompt": prompt,
            "stream": False,
        }
    ).encode("utf-8")
    request = urllib.request.Request(
        url=f"{base_url.rstrip('/')}/api/generate",
        data=payload,
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            raw = response.read().decode("utf-8")
    except TimeoutError as exc:
        raise ValueError(f"Chunk translation timed out after {timeout} seconds.") from exc
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode("utf-8", errors="replace")
        raise ValueError(f"Ollama request failed: HTTP {exc.code}. {detail}") from exc
    except urllib.error.URLError as exc:
        raise ValueError(f"Ollama request failed: {exc}") from exc

    data = json.loads(raw)
    text = data.get("response", "")
    if not isinstance(text, str):
        raise ValueError("Ollama response did not contain a valid text payload.")
    return text


def sanitize_fieldnames(fieldnames: list[str] | tuple[str, ...]) -> list[str]:
    clean: list[str] = []
    seen: set[str] = set()
    for name in fieldnames:
        normalized = (name or "").strip()
        if normalized in seen:
            continue
        seen.add(normalized)
        clean.append(normalized)
    return clean


def sanitize_row(row: dict[str, Any], fieldnames: list[str]) -> dict[str, str]:
    clean: dict[str, str] = {}
    for key in fieldnames:
        value = row.get(key, "")
        clean[key] = "" if value is None else str(value)
    return clean


def load_csv_rows(path: Path) -> tuple[list[dict[str, str]], list[str]]:
    with path.open("r", encoding="utf-8-sig", newline="") as fh:
        reader = csv.DictReader(fh)
        if reader.fieldnames is None:
            raise ValueError(f"CSV has no header row: {path}")
        fieldnames = sanitize_fieldnames(reader.fieldnames)
        rows = [sanitize_row(dict(row), fieldnames) for row in reader]
        return rows, fieldnames


def write_csv_rows(path: Path, rows: list[dict[str, str]], fieldnames: list[str]) -> None:
    with path.open("w", encoding="utf-8-sig", newline="") as fh:
        writer = csv.DictWriter(fh, fieldnames=fieldnames)
        writer.writeheader()
        writer.writerows(rows)


def split_sentence_fragments(text: str) -> list[str]:
    text = text.strip()
    if not text:
        return []
    paragraphs = [part.strip() for part in re.split(r"\n{2,}", text) if part.strip()]
    fragments: list[str] = []
    for paragraph in paragraphs:
        parts = [part.strip() for part in re.split(r"(?<=[.!?])\s+", paragraph) if part.strip()]
        fragments.extend(parts if parts else [paragraph])
    return fragments


def split_long_fragment(text: str, max_chars: int) -> list[str]:
    words = text.split()
    if not words:
        return []
    chunks: list[str] = []
    current = words[0]
    for word in words[1:]:
        candidate = f"{current} {word}"
        if len(candidate) <= max_chars:
            current = candidate
            continue
        chunks.append(current)
        current = word
    chunks.append(current)
    return chunks


def chunk_text(text: str, max_chars: int) -> list[str]:
    sentence_fragments = split_sentence_fragments(text)
    chunks: list[str] = []
    current = ""

    for fragment in sentence_fragments:
        if len(fragment) > max_chars:
            long_parts = split_long_fragment(fragment, max_chars)
        else:
            long_parts = [fragment]

        for part in long_parts:
            if not current:
                current = part
                continue
            candidate = f"{current} {part}"
            if len(candidate) <= max_chars:
                current = candidate
            else:
                chunks.append(current)
                current = part

    if current:
        chunks.append(current)
    return chunks


def translate_fragment(
    client: str,
    model: str,
    text: str,
    label: str,
    timeout: float,
    source_locale: str,
    target_locale: str,
) -> str:
    prompt = (
        f"{build_chunk_prompt(source_locale, target_locale)}\n\n"
        f"Context label: {label}\n\n"
        f"Source locale: {normalize_locale(source_locale)}\n"
        f"Target locale: {normalize_locale(target_locale)}\n\n"
        f"Source fragment:\n{text}\n\n"
        "Translate to the requested target locale."
    )
    translated = ollama_generate(client, model, prompt, timeout).strip()
    if not translated:
        raise ValueError("Model returned an empty translation fragment.")
    return translated


def remove_resolved_problematic_rows(path: Path, resolved_row_ids: set[int]) -> None:
    if not path.exists() or not resolved_row_ids:
        return
    rows, fieldnames = load_csv_rows(path)
    filtered = []
    for row in rows:
        try:
            row_id = int((row.get("row_id") or "").strip())
        except ValueError:
            filtered.append(row)
            continue
        if row_id not in resolved_row_ids:
            filtered.append(row)
    write_csv_rows(path, filtered, fieldnames)


def row_id_to_index_map(rows: list[dict[str, str]]) -> dict[int, int]:
    mapping: dict[int, int] = {}
    for idx, row in enumerate(rows):
        raw_id = (row.get("") or "").strip()
        if not raw_id:
            continue
        try:
            mapping[int(raw_id)] = idx
        except ValueError:
            continue
    return mapping


def run() -> int:
    args = parse_args()
    args.source_locale = normalize_locale(args.source_locale)
    args.target_locale = normalize_locale(args.target_locale)
    if not args.translated_col:
        args.translated_col = default_translated_col_for_locale(args.target_locale)
    if not args.output:
        args.output = str(default_output_file_for_locale(args.target_locale))
    problematic_path = Path(args.problematic_file)
    output_path = Path(args.output)

    if not problematic_path.exists():
        print(f"Problematic rows file not found: {problematic_path}", file=sys.stderr)
        return 1
    if not output_path.exists():
        print(f"Output file not found: {output_path}", file=sys.stderr)
        return 1

    try:
        client = build_client(args)
    except ValueError as exc:
        print(str(exc), file=sys.stderr)
        return 1

    problematic_rows, _ = load_csv_rows(problematic_path)
    output_rows, output_fieldnames = load_csv_rows(output_path)
    index_map = row_id_to_index_map(output_rows)

    pending = []
    for row in problematic_rows:
        try:
            row_id = int((row.get("row_id") or "").strip())
        except ValueError:
            continue
        output_index = index_map.get(row_id)
        if output_index is None:
            continue
        if output_rows[output_index].get(args.translated_col, "").strip():
            continue
        pending.append((row_id, row, output_index))

    if args.max_rows > 0:
        pending = pending[: args.max_rows]

    if not pending:
        print("No problematic pending rows found.")
        return 0

    print(f"Problematic pending rows: {len(pending)}")
    print(f"Chunk max chars: {args.chunk_max_chars}")

    resolved_row_ids: set[int] = set()
    for position, (row_id, problematic_row, output_index) in enumerate(pending, start=1):
        source_text = problematic_row.get("source_text", "").strip()
        label = problematic_row.get("label", "")
        if not source_text:
            print(f"[{position}/{len(pending)}] Skipping row {row_id}: empty source_text in problematic file.")
            continue

        chunks = chunk_text(source_text, args.chunk_max_chars)
        if not chunks:
            print(f"[{position}/{len(pending)}] Skipping row {row_id}: could not build chunks.")
            continue

        print(f"[{position}/{len(pending)}] Translating row {row_id} in {len(chunks)} fragments ...")
        translated_chunks: list[str] = []
        failed = False
        for chunk_index, chunk in enumerate(chunks, start=1):
            try:
                translated = translate_fragment(
                    client=client,
                    model=args.model,
                    text=chunk,
                    label=label,
                    timeout=args.request_timeout,
                    source_locale=args.source_locale,
                    target_locale=args.target_locale,
                )
            except ValueError as exc:
                print(f"Row {row_id} failed at fragment {chunk_index}/{len(chunks)}: {exc}")
                failed = True
                break
            translated_chunks.append(translated)
            time.sleep(args.sleep_seconds)

        if failed:
            continue

        final_translation = "\n\n".join(translated_chunks).strip()
        output_rows[output_index][args.translated_col] = final_translation
        resolved_row_ids.add(row_id)
        print(f"Row {row_id} translated successfully via chunked pass.")

    write_csv_rows(output_path, output_rows, output_fieldnames)
    remove_resolved_problematic_rows(problematic_path, resolved_row_ids)
    print(f"Resolved problematic rows: {len(resolved_row_ids)}")
    print(f"Updated output file: {output_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(run())
