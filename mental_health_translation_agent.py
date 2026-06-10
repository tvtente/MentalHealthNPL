#!/usr/bin/env python3
from __future__ import annotations

import argparse
import csv
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.request
from io import StringIO
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


SYSTEM_PROMPT_JSON_TEMPLATE = """You are a specialist translation agent for mental health datasets.

Your task is to translate short {source_language} text snippets into {target_language} for a mental-health context.

Target locale:
- {target_locale_description}

Rules:
1. Keep the original meaning, emotional intensity, and point of view.
2. Use natural {target_language} that is clinically sensitive and non-stigmatizing.
3. Do not add diagnoses, advice, or explanations that are not present in the source.
4. Preserve ambiguity when the source is ambiguous.
5. Preserve placeholders, emojis, punctuation, and repeated words when they matter emotionally.
6. Respect the provided label as context, but never force label words into the translation unless the source implies them.
7. Return exactly one translation per input item.
8. Output valid JSON only, with no markdown fences and no extra commentary.

Return this exact schema:
[
  {"row_id": 12, "translation_es": "..."}
]
"""


SYSTEM_PROMPT_CSV_TEMPLATE = """You are a specialist translation agent for mental health datasets.

Your task is to translate short {source_language} text snippets into {target_language} for a mental-health context.

Target locale:
- {target_locale_description}

Rules:
1. Keep the original meaning, emotional intensity, and point of view.
2. Use natural {target_language} that is clinically sensitive and non-stigmatizing.
3. Do not add diagnoses, advice, or explanations that are not present in the source.
4. Preserve ambiguity when the source is ambiguous.
5. Preserve placeholders, emojis, punctuation, and repeated words when they matter emotionally.
6. Respect the provided label as context, but never force label words into the translation unless the source implies them.
7. Return exactly one translation per input item.
8. Output CSV only, with no markdown fences and no extra commentary.
9. Always quote the translation field with standard CSV escaping.
10. Copy every row_id exactly as provided. Never renumber rows, never restart from 0, and never omit a row.

Return this exact structure:
row_id,translation_es
12,"..."
"""


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


USER_PROMPT_TEMPLATE = """Translate the following dataset rows from {source_language} to {target_language}.

Target audience:
- Mental health classification or annotation workflows
- Language must be safe, natural, and context-aware for texts related to anxiety, depression, trauma, stress, and related experiences

Important:
- Keep each row independent
- Do not merge rows
- Do not omit any row
- The row_id values must be copied exactly

Rows:
{rows_json}
"""


@dataclass
class BatchResult:
    batch_number: int
    start_index: int
    end_index: int
    translated_rows: int


@dataclass
class FailedRow:
    row_id: int
    reason: str
    source_text: str
    label: str
    text_length: int


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Translate a CSV dataset to Spanish in batches with mental-health-aware prompts."
    )
    parser.add_argument(
        "--provider",
        choices=["ollama"],
        default="ollama",
        help="Backend provider to use.",
    )
    parser.add_argument("--input", default="Combined Data.csv", help="Input CSV file.")
    parser.add_argument("--output", default="Combined Data.translated.csv", help="Output CSV file.")
    parser.add_argument(
        "--work-file",
        default="",
        help="Internal working CSV used to accumulate progress without repeatedly reading the output spreadsheet.",
    )
    parser.add_argument(
        "--checkpoint",
        default=".translation_checkpoint.json",
        help="Checkpoint JSON file used to resume progress.",
    )
    parser.add_argument("--model", default="gpt-4.1-mini", help="Model to use.")
    parser.add_argument(
        "--source-locale",
        default="en-US",
        help="Source locale, for example en-US or en-GB.",
    )
    parser.add_argument(
        "--target-locale",
        default="es-ES",
        help="Target locale, for example es-ES, es-AR or en-GB.",
    )
    parser.add_argument(
        "--response-format",
        choices=["json", "csv"],
        default="csv",
        help="Expected output format returned by the model.",
    )
    parser.add_argument(
        "--base-url",
        default="",
        help="Optional API base URL. For Ollama, the default is http://localhost:11434",
    )
    parser.add_argument("--batch-size", type=int, default=8, help="Rows per API request.")
    parser.add_argument(
        "--min-batch-size",
        type=int,
        default=1,
        help="Smallest batch size allowed during automatic backoff.",
    )
    parser.add_argument("--source-col", default="statement", help="Column containing English text.")
    parser.add_argument("--label-col", default="status", help="Column used as contextual label.")
    parser.add_argument(
        "--translated-col",
        default="",
        help="Column where the translated text will be stored. If omitted, it is derived from --target-locale.",
    )
    parser.add_argument(
        "--rename-translated-col-from",
        default="",
        help="Existing translated column to rename inside the work file before continuing, for example statement_es.",
    )
    parser.add_argument(
        "--start-row",
        type=int,
        default=0,
        help="Start translating from this zero-based data row index.",
    )
    parser.add_argument(
        "--max-batches",
        type=int,
        default=0,
        help="Optional safety limit. 0 means process all remaining batches.",
    )
    parser.add_argument(
        "--sleep-seconds",
        type=float,
        default=1.0,
        help="Pause between successful batches.",
    )
    parser.add_argument(
        "--request-timeout",
        type=float,
        default=120.0,
        help="Maximum seconds to wait for a single model request before treating it as failed.",
    )
    parser.add_argument(
        "--skip-unrecoverable",
        action="store_true",
        help="If a batch still fails at min batch size, leave those rows blank and continue.",
    )
    parser.add_argument(
        "--failed-rows-file",
        default=".translation_problematic_rows.csv",
        help="CSV file where problematic rows are logged for a second pass.",
    )
    parser.add_argument(
        "--chunk-max-chars",
        type=int,
        default=500,
        help="Maximum characters per fragment when rescuing long problematic rows.",
    )
    parser.add_argument(
        "--sync-output-every",
        type=int,
        default=25,
        help="When output is .ods, sync the spreadsheet every N completed batches. 0 disables intermediate syncs.",
    )
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
    language = parts[0].lower()
    region = parts[1].upper()
    return f"{language}-{region}"


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


def build_system_prompt(response_format: str, source_locale: str, target_locale: str) -> str:
    source = locale_to_metadata(source_locale)
    target = locale_to_metadata(target_locale)
    template = SYSTEM_PROMPT_CSV_TEMPLATE if response_format == "csv" else SYSTEM_PROMPT_JSON_TEMPLATE
    return template.format(
        source_language=source["language_name"],
        target_language=target["language_name"],
        target_locale_description=target["locale_description"],
    )


def build_chunk_prompt(source_locale: str, target_locale: str) -> str:
    source = locale_to_metadata(source_locale)
    target = locale_to_metadata(target_locale)
    return CHUNK_TRANSLATION_PROMPT_TEMPLATE.format(
        source_language=source["language_name"],
        target_language=target["language_name"],
        target_locale_description=target["locale_description"],
    )


def build_user_prompt(rows_json: str, source_locale: str, target_locale: str) -> str:
    source = locale_to_metadata(source_locale)
    target = locale_to_metadata(target_locale)
    return USER_PROMPT_TEMPLATE.format(
        rows_json=rows_json,
        source_language=source["language_name"],
        target_language=target["language_name"],
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
        raise ValueError(
            f"Model request timed out after {timeout} seconds. "
            f"This batch will be retried with a smaller size if possible."
        ) from exc
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


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def is_ods_path(path: Path) -> bool:
    return path.suffix.lower() == ".ods"


def convert_ods_to_csv(source_path: Path) -> Path:
    soffice_bin = shutil.which("soffice") or shutil.which("libreoffice")
    if not soffice_bin:
        raise ValueError("LibreOffice/soffice is required to read .ods files but was not found.")

    temp_dir = Path(tempfile.mkdtemp(prefix="ods_input_", dir="/tmp"))
    try:
        subprocess.run(
            [
                soffice_bin,
                "--headless",
                "--convert-to",
                "csv:Text - txt - csv (StarCalc)",
                "--outdir",
                str(temp_dir),
                str(source_path),
            ],
            check=True,
            capture_output=True,
            text=True,
        )
    except subprocess.CalledProcessError as exc:
        raise ValueError(
            f"Could not convert ODS input to CSV. stderr: {exc.stderr.strip() or exc.stdout.strip()}"
        ) from exc

    csv_path = temp_dir / f"{source_path.stem}.csv"
    if not csv_path.exists():
        raise ValueError(f"Converted CSV file was not created: {csv_path}")
    return csv_path


def convert_csv_to_ods(source_csv: Path, target_ods: Path) -> None:
    soffice_bin = shutil.which("soffice") or shutil.which("libreoffice")
    if not soffice_bin:
        raise ValueError("LibreOffice/soffice is required to write .ods files but was not found.")

    temp_dir = Path(tempfile.mkdtemp(prefix="ods_output_", dir="/tmp"))
    temp_csv = temp_dir / source_csv.name
    shutil.copy2(source_csv, temp_csv)
    try:
        subprocess.run(
            [
                soffice_bin,
                "--headless",
                "--convert-to",
                "ods",
                "--outdir",
                str(temp_dir),
                str(temp_csv),
            ],
            check=True,
            capture_output=True,
            text=True,
        )
    except subprocess.CalledProcessError as exc:
        raise ValueError(
            f"Could not convert CSV output to ODS. stderr: {exc.stderr.strip() or exc.stdout.strip()}"
        ) from exc

    generated_ods = temp_dir / f"{temp_csv.stem}.ods"
    if not generated_ods.exists():
        raise ValueError(f"Converted ODS file was not created: {generated_ods}")
    shutil.copy2(generated_ods, target_ods)


def default_work_file_for_paths(input_path: Path, target_locale: str) -> Path:
    locale_label = normalize_locale(target_locale)
    stem = input_path.stem
    return input_path.parent / f".{stem} {locale_label}.work.csv"


def should_sync_output(output_path: Path, completed_batches: int, sync_every: int, force: bool = False) -> bool:
    if not is_ods_path(output_path):
        return False
    if force:
        return True
    if sync_every <= 0:
        return False
    return completed_batches > 0 and completed_batches % sync_every == 0


def load_rows(path: Path) -> tuple[list[dict[str, str]], list[str]]:
    with path.open("r", encoding="utf-8-sig", newline="") as fh:
        reader = csv.DictReader(fh)
        if reader.fieldnames is None:
            raise ValueError("The input CSV has no header row.")
        clean_fieldnames = sanitize_fieldnames(reader.fieldnames)
        rows = [sanitize_row(dict(row), clean_fieldnames) for row in reader]
        return rows, clean_fieldnames


def load_existing_output(path: Path) -> list[dict[str, str]] | None:
    if not path.exists():
        return None
    with path.open("r", encoding="utf-8-sig", newline="") as fh:
        reader = csv.DictReader(fh)
        if reader.fieldnames is None:
            return []
        clean_fieldnames = sanitize_fieldnames(reader.fieldnames)
        return [sanitize_row(dict(row), clean_fieldnames) for row in reader]


def write_rows(path: Path, rows: list[dict[str, Any]], fieldnames: list[str]) -> None:
    clean_fieldnames = sanitize_fieldnames(fieldnames)
    with path.open("w", encoding="utf-8-sig", newline="") as fh:
        writer = csv.DictWriter(fh, fieldnames=clean_fieldnames)
        writer.writeheader()
        for row in rows:
            writer.writerow(sanitize_row(dict(row), clean_fieldnames))


def rename_column_in_csv(path: Path, old_col: str, new_col: str) -> bool:
    if not path.exists():
        return False
    rows, fieldnames = load_rows(path)
    if old_col not in fieldnames:
        return False
    if new_col in fieldnames and old_col == new_col:
        return False

    new_fieldnames: list[str] = []
    for name in fieldnames:
        if name == old_col:
            if new_col not in new_fieldnames:
                new_fieldnames.append(new_col)
            continue
        if name not in new_fieldnames:
            new_fieldnames.append(name)

    renamed_rows: list[dict[str, str]] = []
    for row in rows:
        renamed = dict(row)
        old_value = renamed.pop(old_col, "")
        if new_col not in renamed or not renamed.get(new_col, "").strip():
            renamed[new_col] = old_value
        renamed_rows.append(renamed)

    write_rows(path, renamed_rows, new_fieldnames)
    return True


def load_checkpoint(path: Path) -> dict[str, Any]:
    if not path.exists():
        return {}
    return json.loads(path.read_text(encoding="utf-8"))


def save_checkpoint(path: Path, data: dict[str, Any]) -> None:
    path.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")


def append_failed_rows(path: Path, failed_rows: list[FailedRow]) -> None:
    fieldnames = ["row_id", "label", "text_length", "reason", "source_text", "logged_at"]
    existing: dict[int, dict[str, str]] = {}
    if path.exists():
        with path.open("r", encoding="utf-8-sig", newline="") as fh:
            reader = csv.DictReader(fh)
            for row in reader:
                try:
                    row_id = int((row.get("row_id") or "").strip())
                except ValueError:
                    continue
                existing[row_id] = {key: row.get(key, "") for key in fieldnames}

    timestamp = utc_now()
    for failed in failed_rows:
        existing[failed.row_id] = {
            "row_id": str(failed.row_id),
            "label": failed.label,
            "text_length": str(failed.text_length),
            "reason": failed.reason,
            "source_text": failed.source_text,
            "logged_at": timestamp,
        }

    ordered_rows = [existing[row_id] for row_id in sorted(existing)]
    with path.open("w", encoding="utf-8-sig", newline="") as fh:
        writer = csv.DictWriter(fh, fieldnames=fieldnames)
        writer.writeheader()
        writer.writerows(ordered_rows)


def remove_problematic_row_ids(path: Path, resolved_row_ids: set[int]) -> None:
    if not path.exists() or not resolved_row_ids:
        return
    with path.open("r", encoding="utf-8-sig", newline="") as fh:
        reader = csv.DictReader(fh)
        if reader.fieldnames is None:
            return
        fieldnames = sanitize_fieldnames(reader.fieldnames)
        rows = [sanitize_row(dict(row), fieldnames) for row in reader]

    filtered_rows: list[dict[str, str]] = []
    for row in rows:
        try:
            row_id = int((row.get("row_id") or "").strip())
        except ValueError:
            filtered_rows.append(row)
            continue
        if row_id not in resolved_row_ids:
            filtered_rows.append(row)

    write_rows(path, filtered_rows, fieldnames)


def build_failed_rows(
    rows: list[dict[str, str]],
    row_ids: list[int],
    source_col: str,
    label_col: str,
    reason: str,
) -> list[FailedRow]:
    failed_rows: list[FailedRow] = []
    for row_id in row_ids:
        row = rows[row_id]
        source_text = row.get(source_col, "")
        failed_rows.append(
            FailedRow(
                row_id=row_id,
                reason=reason,
                source_text=source_text,
                label=row.get(label_col, ""),
                text_length=len(source_text),
            )
        )
    return failed_rows


def split_sentence_fragments(text: str) -> list[str]:
    text = text.strip()
    if not text:
        return []
    paragraphs = [part.strip() for part in text.split("\n\n") if part.strip()]
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
        parts = split_long_fragment(fragment, max_chars) if len(fragment) > max_chars else [fragment]
        for part in parts:
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


def try_chunked_translation_for_row(
    client: str,
    model: str,
    row: dict[str, str],
    row_id: int,
    source_col: str,
    label_col: str,
    request_timeout: float,
    chunk_max_chars: int,
    sleep_seconds: float,
    source_locale: str,
    target_locale: str,
) -> str:
    source_text = row.get(source_col, "").strip()
    label = row.get(label_col, "")
    if not source_text:
        raise ValueError("Cannot chunk-translate an empty source text.")

    chunks = chunk_text(source_text, chunk_max_chars)
    if not chunks:
        raise ValueError("Could not split the source text into fragments.")

    print(f"Row {row_id}: chunked rescue will use {len(chunks)} fragments (max {chunk_max_chars} chars each).")
    translated_chunks: list[str] = []
    for chunk_index, chunk in enumerate(chunks, start=1):
        print(f"Row {row_id}: translating fragment {chunk_index}/{len(chunks)} ...")
        translated = translate_fragment(
            client,
            model,
            chunk,
            label,
            request_timeout,
            source_locale,
            target_locale,
        )
        translated_chunks.append(translated)
        print(f"Row {row_id}: fragment {chunk_index}/{len(chunks)} completed.")
        if chunk_index < len(chunks):
            time.sleep(sleep_seconds)

    return "\n\n".join(translated_chunks).strip()


def normalize_rows(
    input_rows: list[dict[str, str]],
    existing_output_rows: list[dict[str, str]] | None,
    translated_col: str,
) -> list[dict[str, str]]:
    if existing_output_rows is None:
        return [dict(row) for row in input_rows]

    if len(existing_output_rows) != len(input_rows):
        raise ValueError("The existing output file has a different number of rows than the input file.")

    merged: list[dict[str, str]] = []
    for source_row, output_row in zip(input_rows, existing_output_rows):
        row = dict(source_row)
        if translated_col in output_row:
            row[translated_col] = output_row.get(translated_col, "")
        merged.append(row)
    return merged


def sanitize_fieldnames(fieldnames: list[str] | tuple[str, ...]) -> list[str]:
    clean: list[str] = []
    seen: set[str] = set()
    for name in fieldnames:
        normalized = (name or "").strip()
        if normalized == "":
            normalized = ""
        if normalized in seen:
            continue
        seen.add(normalized)
        clean.append(normalized)
    return clean


def sanitize_row(row: dict[str, Any], fieldnames: list[str]) -> dict[str, str]:
    clean: dict[str, str] = {}
    for key in fieldnames:
        value = row.get(key, "")
        if value is None:
            value = ""
        clean[key] = str(value)
    return clean


def build_pending_indices(rows: list[dict[str, str]], translated_col: str, start_row: int) -> list[int]:
    pending: list[int] = []
    for idx, row in enumerate(rows):
        if idx < start_row:
            continue
        if row.get(translated_col, "").strip():
            continue
        pending.append(idx)
    return pending


def strip_json_fence(text: str) -> str:
    stripped = text.strip()
    if stripped.startswith("```"):
        lines = stripped.splitlines()
        if len(lines) >= 3 and lines[-1].strip() == "```":
            return "\n".join(lines[1:-1]).strip()
    return stripped


def strip_code_fence(text: str) -> str:
    stripped = text.strip()
    if stripped.startswith("```"):
        lines = stripped.splitlines()
        if len(lines) >= 3 and lines[-1].strip() == "```":
            return "\n".join(lines[1:-1]).strip()
    return stripped


def build_batch_payload(
    rows: list[dict[str, str]],
    batch_indices: list[int],
    source_col: str,
    label_col: str,
) -> str:
    payload = []
    for idx in batch_indices:
        row = rows[idx]
        payload.append(
            {
                "row_id": idx,
                "label": row.get(label_col, ""),
                "source_text": row.get(source_col, ""),
            }
        )
    return json.dumps(payload, ensure_ascii=False, indent=2)


def translate_batch(
    client: str,
    model: str,
    rows: list[dict[str, str]],
    batch_indices: list[int],
    source_col: str,
    label_col: str,
    response_format: str,
    request_timeout: float,
    source_locale: str,
    target_locale: str,
) -> list[dict[str, Any]]:
    rows_json = build_batch_payload(rows, batch_indices, source_col, label_col)
    prompt = (
        f"{build_system_prompt(response_format, source_locale, target_locale)}\n\n"
        f"{build_user_prompt(rows_json, source_locale, target_locale)}"
    )
    text = strip_code_fence(ollama_generate(client, model, prompt, request_timeout))
    if response_format == "csv":
        try:
            data = parse_csv_response(text)
        except ValueError as exc:
            raise ValueError(
                f"Model did not return valid CSV. This usually means the batch was too large and the output was cut off. "
                f"Try lowering --batch-size to 10 or 5. First 500 chars: {text[:500]!r}"
            ) from exc
        return data

    try:
        data = json.loads(text)
    except json.JSONDecodeError as exc:
        raise ValueError(
            f"Model did not return valid JSON. This usually means the batch was too large and the output was cut off. "
            f"Try lowering --batch-size to 10 or 5. First 500 chars: {text[:500]!r}"
        ) from exc

    if not isinstance(data, list):
        raise ValueError("Model output must be a JSON array.")
    return data


def parse_csv_response(text: str) -> list[dict[str, Any]]:
    stream = StringIO(text)
    reader = csv.DictReader(stream)
    if reader.fieldnames is None:
        raise ValueError("Missing CSV header.")
    required = {"row_id", "translation_es"}
    if not required.issubset(set(reader.fieldnames)):
        raise ValueError(f"CSV header must contain {sorted(required)}.")

    items: list[dict[str, Any]] = []
    for row in reader:
        row_id_raw = (row.get("row_id") or "").strip()
        translation = row.get("translation_es") or ""
        if not row_id_raw:
            continue
        try:
            row_id = int(row_id_raw)
        except ValueError as exc:
            raise ValueError(f"Invalid row_id value: {row_id_raw!r}") from exc
        items.append({"row_id": row_id, "translation_es": translation})

    if not items:
        raise ValueError("CSV response contained no data rows.")
    return items


def validate_batch_output(batch_indices: list[int], translated_items: list[dict[str, Any]]) -> dict[int, str]:
    expected = set(batch_indices)
    seen: dict[int, str] = {}

    for item in translated_items:
        if not isinstance(item, dict):
            raise ValueError("Each translated item must be an object.")
        row_id = item.get("row_id")
        translation = item.get("translation_es")
        if not isinstance(row_id, int):
            raise ValueError(f"Invalid row_id in output: {item!r}")
        if not isinstance(translation, str) or not translation.strip():
            raise ValueError(f"Missing translation_es for row_id={row_id}")
        if row_id not in expected:
            raise ValueError(f"Unexpected row_id returned by model: {row_id}")
        if row_id in seen:
            raise ValueError(f"Duplicate row_id returned by model: {row_id}")
        seen[row_id] = translation.strip()

    missing = expected - set(seen)
    if missing:
        raise ValueError(f"The model omitted row_ids: {sorted(missing)[:10]}")

    return seen


def run() -> int:
    args = parse_args()
    args.source_locale = normalize_locale(args.source_locale)
    args.target_locale = normalize_locale(args.target_locale)
    if not args.translated_col:
        args.translated_col = default_translated_col_for_locale(args.target_locale)

    input_path = Path(args.input)
    output_path = Path(args.output)
    checkpoint_path = Path(args.checkpoint)
    failed_rows_path = Path(args.failed_rows_file)
    input_csv_path = input_path
    work_file_path = Path(args.work_file) if args.work_file else default_work_file_for_paths(input_path, args.target_locale)

    if not input_path.exists():
        print(f"Input file not found: {input_path}", file=sys.stderr)
        return 1

    if is_ods_path(input_path):
        try:
            input_csv_path = convert_ods_to_csv(input_path)
        except ValueError as exc:
            print(str(exc), file=sys.stderr)
            return 1

    if args.rename_translated_col_from:
        renamed = rename_column_in_csv(work_file_path, args.rename_translated_col_from, args.translated_col)
        if renamed:
            print(
                f"Renamed translated column in work file: "
                f"{args.rename_translated_col_from} -> {args.translated_col}"
            )

    input_rows, input_headers = load_rows(input_csv_path)
    existing_output_rows = load_existing_output(work_file_path)
    rows = normalize_rows(input_rows, existing_output_rows, args.translated_col)

    fieldnames = list(input_headers)
    if args.translated_col not in fieldnames:
        fieldnames.append(args.translated_col)

    checkpoint = load_checkpoint(checkpoint_path)
    pending_indices = build_pending_indices(rows, args.translated_col, args.start_row)

    if not pending_indices:
        print("No pending rows found. The output file already appears complete.")
        return 0

    try:
        client = build_client(args)
    except ValueError as exc:
        print(str(exc), file=sys.stderr)
        return 1
    completed_batches = 0

    total_pending = len(pending_indices)
    print(f"Pending rows: {total_pending}")
    print(f"Batch size: {args.batch_size}")
    print(f"Target locale: {args.target_locale}")
    print(f"Translated column: {args.translated_col}")
    print(f"Output file is: {work_file_path}")

    if args.min_batch_size < 1:
        print("--min-batch-size must be at least 1.", file=sys.stderr)
        return 1
    if args.min_batch_size > args.batch_size:
        print("--min-batch-size cannot be larger than --batch-size.", file=sys.stderr)
        return 1

    offset = 0
    while offset < total_pending:
        if args.max_batches and completed_batches >= args.max_batches:
            print("Stopped because --max-batches limit was reached.")
            break

        current_batch_size = min(args.batch_size, total_pending - offset)
        translated_items: list[dict[str, Any]] | None = None
        translations: dict[int, str] | None = None
        batch_indices: list[int] = []
        last_error: str | None = None

        while current_batch_size >= args.min_batch_size:
            batch_indices = pending_indices[offset : offset + current_batch_size]
            if not batch_indices:
                break
            batch_label = f"Batch {completed_batches + 1} ({batch_indices[0]}..{batch_indices[-1]})"
            print(f"Processing {batch_label} with batch size {current_batch_size} ...")
            try:
                translated_items = translate_batch(
                    client=client,
                    model=args.model,
                    rows=rows,
                    batch_indices=batch_indices,
                    source_col=args.source_col,
                    label_col=args.label_col,
                    response_format=args.response_format,
                    request_timeout=args.request_timeout,
                    source_locale=args.source_locale,
                    target_locale=args.target_locale,
                )
                translations = validate_batch_output(batch_indices, translated_items)
                break
            except ValueError as exc:
                last_error = str(exc)
                if current_batch_size == args.min_batch_size:
                    if args.skip_unrecoverable:
                        failed_rows = build_failed_rows(
                            rows=rows,
                            row_ids=batch_indices,
                            source_col=args.source_col,
                            label_col=args.label_col,
                            reason=str(exc),
                        )
                        print(
                            f"Queueing rows {batch_indices[0]}..{batch_indices[-1]} as problematic after failure at "
                            f"min batch size {args.min_batch_size}. Reason: {exc}"
                        )
                        append_failed_rows(failed_rows_path, failed_rows)

                        if len(batch_indices) == 1:
                            row_id = batch_indices[0]
                            print(f"Attempting inline chunked rescue for row {row_id} ...")
                            try:
                                recovered_translation = try_chunked_translation_for_row(
                                    client=client,
                                    model=args.model,
                                    row=rows[row_id],
                                    row_id=row_id,
                                    source_col=args.source_col,
                                    label_col=args.label_col,
                                    request_timeout=args.request_timeout,
                                    chunk_max_chars=args.chunk_max_chars,
                                    sleep_seconds=args.sleep_seconds,
                                    source_locale=args.source_locale,
                                    target_locale=args.target_locale,
                                )
                            except ValueError as chunk_exc:
                                print(f"Inline chunked rescue failed for row {row_id}. Reason: {chunk_exc}")
                            else:
                                rows[row_id][args.translated_col] = recovered_translation
                                translations = {row_id: recovered_translation}
                                translated_items = [{"row_id": row_id, "translation_es": recovered_translation}]
                                remove_problematic_row_ids(failed_rows_path, {row_id})
                                print(f"Inline chunked rescue succeeded for row {row_id}.")
                                break

                        translated_items = []
                        break
                    raise ValueError(
                        f"Translation failed even at min batch size {args.min_batch_size}. "
                        f"Last error: {exc}"
                    ) from exc
                next_batch_size = max(args.min_batch_size, current_batch_size // 2)
                if next_batch_size == current_batch_size:
                    next_batch_size = current_batch_size - 1
                print(
                    f"Batch failed at size {current_batch_size}. "
                    f"Retrying same rows with smaller batch size {next_batch_size}. "
                    f"Reason: {exc}"
                )
                current_batch_size = next_batch_size

        if translated_items is None or not batch_indices:
            print("No batch could be processed.", file=sys.stderr)
            return 1

        batch_number = completed_batches + 1
        batch_label = f"Batch {batch_number} ({batch_indices[0]}..{batch_indices[-1]})"
        if translations is None:
            translations = {}

        for row_id, translation in translations.items():
            rows[row_id][args.translated_col] = translation

        write_rows(work_file_path, rows, fieldnames)

        batch_result = BatchResult(
            batch_number=batch_number,
            start_index=batch_indices[0],
            end_index=batch_indices[-1],
            translated_rows=len(batch_indices),
        )
        checkpoint.update(
            {
                "updated_at": utc_now(),
                "input_file": str(input_path),
                "output_file": str(output_path),
                "translated_col": args.translated_col,
                "model": args.model,
                "batch_size": current_batch_size,
                "last_batch": batch_result.__dict__,
                "translated_rows_total": sum(1 for row in rows if row.get(args.translated_col, "").strip()),
                "last_error": last_error,
            }
        )
        save_checkpoint(checkpoint_path, checkpoint)

        completed_batches += 1
        if should_sync_output(output_path, completed_batches, args.sync_output_every):
            try:
                convert_csv_to_ods(work_file_path, output_path)
                print(f"Synchronized ODS output after {completed_batches} batches.")
            except ValueError as exc:
                print(str(exc), file=sys.stderr)
                return 1
        translated_count = len(translations)
        skipped_count = len(batch_indices) - translated_count
        print(
            f"Completed {batch_label}: {translated_count} rows translated, {skipped_count} skipped. "
            f"Total translated: {checkpoint['translated_rows_total']}"
        )
        offset += len(batch_indices)
        time.sleep(args.sleep_seconds)

    if is_ods_path(output_path):
        try:
            convert_csv_to_ods(work_file_path, output_path)
            print(f"Final ODS synchronized to {output_path}")
        except ValueError as exc:
            print(str(exc), file=sys.stderr)
            return 1

    print(f"Output file is: {work_file_path}")
    print("Finished.")
    return 0


if __name__ == "__main__":
    raise SystemExit(run())
