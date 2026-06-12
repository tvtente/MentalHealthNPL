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
        description=(
            "Second-pass translator for problematic rows. "
            "It can either rescue rows logged as problematic or reevaluate suspicious existing translations."
        )
    )
    parser.add_argument(
        "--mode",
        choices=["problematic", "reevaluate", "high-confidence-report", "high-confidence-retranslate"],
        default="problematic",
    )
    parser.add_argument("--provider", choices=["ollama"], default="ollama")
    parser.add_argument("--model", default="gemma3:4b")
    parser.add_argument("--source-locale", default="en-US")
    parser.add_argument("--target-locale", default="es-ES")
    parser.add_argument("--base-url", default="", help="For Ollama the default is http://localhost:11434")
    parser.add_argument("--problematic-file", default=".translation_problematic_rows.csv")
    parser.add_argument("--report-file", default=".translation_suspicious_rows.csv")
    parser.add_argument("--high-confidence-report-file", default=".translation_high_confidence_mismatches.csv")
    parser.add_argument(
        "--rebuild-report",
        action="store_true",
        help=(
            "Allow rebuilding the high-confidence report even if it already exists. "
            "Without this flag, the command refuses to overwrite an existing report so the pending queue is preserved."
        ),
    )
    parser.add_argument("--output", default="")
    parser.add_argument("--source-col", default="statement")
    parser.add_argument("--translated-col", default="")
    parser.add_argument(
        "--blank-flagged-translations",
        action="store_true",
        help=(
            "When generating suspicious or high-confidence reports, blank the translated column in the output CSV "
            "for the flagged rows while keeping the previous translation stored in the report."
        ),
    )
    parser.add_argument("--request-timeout", type=float, default=120.0)
    parser.add_argument("--chunk-max-chars", type=int, default=500)
    parser.add_argument("--max-rows", type=int, default=0, help="0 means process all problematic rows.")
    parser.add_argument(
        "--row-batch-size",
        type=int,
        default=20,
        help="For report-driven retranslations, process flagged rows in visible batches of this size.",
    )
    parser.add_argument("--sleep-seconds", type=float, default=0.5)
    parser.add_argument(
        "--interactive-review",
        action="store_true",
        help=(
            "After producing a translation for a problematic or suspicious row, show the source and proposal in the "
            "console and ask whether to accept it. If rejected, the translated field is left blank."
        ),
    )
    parser.add_argument(
        "--save-every",
        type=int,
        default=10,
        help="Persist reevaluation progress every N processed rows. Use 0 to save only at the end.",
    )
    parser.add_argument("--dry-run", action="store_true", help="Only generate the suspicious rows report.")
    parser.add_argument(
        "--min-source-chars",
        type=int,
        default=40,
        help="Minimum source text length before ratio-based suspicious checks are applied.",
    )
    parser.add_argument(
        "--min-char-ratio",
        type=float,
        default=0.45,
        help="Flag translations shorter than this translated/source character ratio.",
    )
    parser.add_argument(
        "--max-char-ratio",
        type=float,
        default=2.35,
        help="Flag translations longer than this translated/source character ratio.",
    )
    parser.add_argument(
        "--min-word-ratio",
        type=float,
        default=0.45,
        help="Flag translations shorter than this translated/source word ratio.",
    )
    parser.add_argument(
        "--max-word-ratio",
        type=float,
        default=2.35,
        help="Flag translations longer than this translated/source word ratio.",
    )
    parser.add_argument(
        "--max-english-token-ratio",
        type=float,
        default=0.35,
        help="Flag translated texts that still contain too many common English tokens.",
    )
    parser.add_argument(
        "--long-text-threshold",
        type=int,
        default=5000,
        help="From this source length onward, use relaxed length heuristics and stronger semantic anchor checks.",
    )
    parser.add_argument(
        "--long-text-abs-tolerance",
        type=int,
        default=1200,
        help="Minimum absolute character difference required before a long text is flagged by length alone.",
    )
    parser.add_argument(
        "--long-text-relative-tolerance",
        type=float,
        default=0.18,
        help="Relative tolerance applied to long texts before character-length differences are treated as suspicious.",
    )
    parser.add_argument(
        "--long-min-char-ratio",
        type=float,
        default=0.18,
        help="Minimum translated/source character ratio for long texts.",
    )
    parser.add_argument(
        "--long-max-char-ratio",
        type=float,
        default=3.5,
        help="Maximum translated/source character ratio for long texts.",
    )
    parser.add_argument(
        "--max-anchor-miss-ratio",
        type=float,
        default=0.45,
        help="Flag translations that lose too many stable anchors such as URLs, years, medications, names or acronyms.",
    )
    parser.add_argument(
        "--high-confidence-max-char-ratio",
        type=float,
        default=0.28,
        help="Maximum translated/source char ratio for a row to be considered a high-confidence mismatch.",
    )
    parser.add_argument(
        "--high-confidence-max-word-ratio",
        type=float,
        default=0.30,
        help="Maximum translated/source word ratio for a row to be considered a high-confidence mismatch.",
    )
    parser.add_argument(
        "--high-confidence-max-sentence-ratio",
        type=float,
        default=0.34,
        help="Maximum translated/source sentence ratio for a row to be considered a high-confidence mismatch.",
    )
    parser.add_argument(
        "--high-confidence-min-source-chars",
        type=int,
        default=120,
        help="Minimum source length for high-confidence mismatch filtering.",
    )
    parser.add_argument(
        "--high-confidence-min-source-words",
        type=int,
        default=25,
        help="Minimum source word count for high-confidence mismatch filtering.",
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


def count_words(text: str) -> int:
    return len(re.findall(r"\b\w+\b", text, flags=re.UNICODE))


def count_sentences(text: str) -> int:
    parts = [part.strip() for part in re.split(r"[.!?]+", text) if part.strip()]
    return len(parts)


ENGLISH_COMMON_TOKENS = {
    "the",
    "and",
    "or",
    "but",
    "with",
    "without",
    "from",
    "this",
    "that",
    "these",
    "those",
    "have",
    "has",
    "had",
    "feel",
    "feeling",
    "felt",
    "my",
    "your",
    "their",
    "because",
    "what",
    "when",
    "where",
    "why",
    "how",
    "not",
    "don't",
    "can't",
    "won't",
    "i",
    "me",
    "you",
    "he",
    "she",
    "they",
    "we",
    "is",
    "are",
    "was",
    "were",
}


def english_token_ratio(text: str) -> float:
    tokens = [token.lower() for token in re.findall(r"[A-Za-z']+", text)]
    if not tokens:
        return 0.0
    english_hits = sum(1 for token in tokens if token in ENGLISH_COMMON_TOKENS)
    return english_hits / len(tokens)


def normalize_compact_whitespace(text: str) -> str:
    return re.sub(r"\s+", " ", text).strip()


def spacing_quality_is_low(text: str) -> bool:
    stripped = normalize_compact_whitespace(text)
    if not stripped:
        return False
    tokens = stripped.split()
    if not tokens:
        return False
    alpha_chars = sum(1 for char in stripped if char.isalpha())
    space_chars = stripped.count(" ")
    avg_token_len = alpha_chars / max(len(tokens), 1)
    space_density = space_chars / max(alpha_chars, 1)
    return avg_token_len >= 9.5 or (len(tokens) >= 8 and space_density <= 0.085)


def extract_urls(text: str) -> set[str]:
    return {match.lower() for match in re.findall(r"https?://\S+|www\.\S+", text)}


def extract_years(text: str) -> set[str]:
    return set(re.findall(r"\b(?:19|20)\d{2}\b", text))


def extract_number_tokens(text: str) -> set[str]:
    return set(re.findall(r"\b\d+(?:[.,]\d+)?\b", text))


def extract_named_anchor_tokens(text: str) -> set[str]:
    tokens = re.findall(r"\b[A-Za-z][A-Za-z0-9+/#&.'-]{2,}\b", text)
    anchors: set[str] = set()
    for token in tokens:
        lower = token.lower()
        has_internal_upper = any(char.isupper() for char in token[1:])
        is_all_caps = token.isupper() and len(token) >= 2
        has_digit = any(char.isdigit() for char in token)
        if has_internal_upper or is_all_caps or has_digit:
            anchors.add(lower)
    return anchors


def build_anchor_set(text: str, allow_named_tokens: bool) -> set[str]:
    anchors = set()
    anchors.update(extract_urls(text))
    anchors.update(extract_years(text))
    anchors.update(extract_number_tokens(text))
    if allow_named_tokens:
        anchors.update(extract_named_anchor_tokens(text))
    return anchors


def anchor_overlap_metrics(source_text: str, translated_text: str, allow_named_tokens: bool) -> tuple[int, int, float]:
    source_anchors = build_anchor_set(source_text, allow_named_tokens=allow_named_tokens)
    if not source_anchors:
        return 0, 0, 0.0
    translated_folded = normalize_compact_whitespace(translated_text).lower()
    missing = 0
    for anchor in source_anchors:
        if anchor not in translated_folded:
            missing += 1
    return len(source_anchors), missing, missing / len(source_anchors)


def score_suspicious_translation(
    row: dict[str, str],
    row_id: int,
    source_col: str,
    translated_col: str,
    args: argparse.Namespace,
) -> dict[str, Any] | None:
    source_text = (row.get(source_col) or "").strip()
    translated_text = (row.get(translated_col) or "").strip()
    if not source_text or not translated_text:
        return None

    source_chars = len(source_text)
    translated_chars = len(translated_text)
    source_words = count_words(source_text)
    translated_words = count_words(translated_text)
    source_sentences = count_sentences(source_text)
    translated_sentences = count_sentences(translated_text)

    char_ratio = translated_chars / source_chars if source_chars else 0.0
    word_ratio = translated_words / source_words if source_words else 0.0
    sentence_ratio = translated_sentences / source_sentences if source_sentences else 0.0
    english_ratio = english_token_ratio(translated_text)
    spacing_quality_low = spacing_quality_is_low(source_text)
    is_long_text = source_chars >= args.long_text_threshold
    allow_named_anchor_tokens = not spacing_quality_low and not is_long_text
    anchor_count, missing_anchor_count, anchor_miss_ratio = anchor_overlap_metrics(
        source_text,
        translated_text,
        allow_named_tokens=allow_named_anchor_tokens,
    )
    long_text_tolerance = max(args.long_text_abs_tolerance, int(source_chars * args.long_text_relative_tolerance))
    absolute_char_delta = abs(translated_chars - source_chars)

    score = 0
    reasons: list[str] = []

    if source_chars >= args.min_source_chars:
        if is_long_text:
            if char_ratio < args.long_min_char_ratio and absolute_char_delta > long_text_tolerance:
                score += int((args.long_min_char_ratio - char_ratio) * 140)
                reasons.append(f"long_char_ratio_too_low:{char_ratio:.2f}")
            elif char_ratio > args.long_max_char_ratio and absolute_char_delta > long_text_tolerance:
                score += int((char_ratio - args.long_max_char_ratio) * 100)
                reasons.append(f"long_char_ratio_too_high:{char_ratio:.2f}")
        else:
            if char_ratio < args.min_char_ratio:
                score += int((args.min_char_ratio - char_ratio) * 100)
                reasons.append(f"char_ratio_too_low:{char_ratio:.2f}")
            elif char_ratio > args.max_char_ratio:
                score += int((char_ratio - args.max_char_ratio) * 100)
                reasons.append(f"char_ratio_too_high:{char_ratio:.2f}")

        if not spacing_quality_low and not is_long_text:
            if word_ratio < args.min_word_ratio:
                score += int((args.min_word_ratio - word_ratio) * 120)
                reasons.append(f"word_ratio_too_low:{word_ratio:.2f}")
            elif word_ratio > args.max_word_ratio:
                score += int((word_ratio - args.max_word_ratio) * 120)
                reasons.append(f"word_ratio_too_high:{word_ratio:.2f}")

    if source_words >= 12 and translated_words <= 3:
        score += 30
        reasons.append("translation_too_short_for_long_source")

    if is_long_text:
        if source_sentences >= 4 and (translated_sentences == 1 or sentence_ratio < 0.25):
            score += 25
            reasons.append("long_text_sentence_collapse")
        elif source_sentences >= 4 and sentence_ratio > 4.0 and absolute_char_delta > long_text_tolerance:
            score += 15
            reasons.append("long_text_sentence_expansion")
    else:
        if source_sentences >= 3 and translated_sentences == 1:
            score += 10
            reasons.append("sentence_collapse")
        elif source_sentences and translated_sentences >= source_sentences * 3:
            score += 10
            reasons.append("sentence_expansion")

    if english_ratio > args.max_english_token_ratio:
        score += int((english_ratio - args.max_english_token_ratio) * 100)
        reasons.append(f"english_residue:{english_ratio:.2f}")

    if not spacing_quality_low and anchor_count >= 3:
        if not is_long_text and anchor_miss_ratio > args.max_anchor_miss_ratio:
            score += int(anchor_miss_ratio * 80)
            reasons.append(f"anchor_loss:{missing_anchor_count}/{anchor_count}")
        elif is_long_text and missing_anchor_count >= 4 and anchor_miss_ratio > 0.65:
            score += int(anchor_miss_ratio * 50)
            reasons.append(f"long_text_anchor_loss:{missing_anchor_count}/{anchor_count}")

    if not reasons:
        return None

    return {
        "row_id": row_id,
        "label": row.get("status", ""),
        "source_text": source_text,
        "current_translation": translated_text,
        "source_chars": source_chars,
        "translated_chars": translated_chars,
        "source_words": source_words,
        "translated_words": translated_words,
        "source_sentences": source_sentences,
        "translated_sentences": translated_sentences,
        "char_ratio": f"{char_ratio:.4f}",
        "word_ratio": f"{word_ratio:.4f}",
        "sentence_ratio": f"{sentence_ratio:.4f}",
        "english_token_ratio": f"{english_ratio:.4f}",
        "anchor_count": anchor_count,
        "missing_anchor_count": missing_anchor_count,
        "anchor_miss_ratio": f"{anchor_miss_ratio:.4f}",
        "spacing_quality_low": str(spacing_quality_low),
        "suspicion_score": score,
        "reasons": ";".join(reasons),
    }


def write_report_rows(path: Path, rows: list[dict[str, Any]]) -> None:
    fieldnames = [
        "row_id",
        "label",
        "source_chars",
        "translated_chars",
        "source_words",
        "translated_words",
        "source_sentences",
        "translated_sentences",
        "char_ratio",
        "word_ratio",
        "sentence_ratio",
        "english_token_ratio",
        "anchor_count",
        "missing_anchor_count",
        "anchor_miss_ratio",
        "spacing_quality_low",
        "suspicion_score",
        "reasons",
        "source_text",
        "current_translation",
        "new_translation",
        "action",
    ]
    normalized: list[dict[str, str]] = []
    for row in rows:
        normalized.append({field: "" if row.get(field) is None else str(row.get(field, "")) for field in fieldnames})
    with path.open("w", encoding="utf-8-sig", newline="") as fh:
        writer = csv.DictWriter(fh, fieldnames=fieldnames)
        writer.writeheader()
        writer.writerows(normalized)


def blank_translations_for_row_ids(
    output_path: Path,
    translated_col: str,
    row_ids: set[int],
) -> int:
    if not row_ids:
        return 0
    output_rows, output_fieldnames = load_csv_rows(output_path)
    index_map = row_id_to_index_map(output_rows)
    blanked = 0
    for row_id in row_ids:
        output_index = index_map.get(row_id)
        if output_index is None:
            continue
        current_value = (output_rows[output_index].get(translated_col) or "").strip()
        if not current_value:
            continue
        output_rows[output_index][translated_col] = ""
        blanked += 1
    if blanked:
        write_csv_rows(output_path, output_rows, output_fieldnames)
    return blanked


def high_confidence_mismatch_reason(row: dict[str, str], args: argparse.Namespace) -> str | None:
    try:
        source_chars = int((row.get("source_chars") or "0").strip())
        translated_chars = int((row.get("translated_chars") or "0").strip())
        source_words = int((row.get("source_words") or "0").strip())
        translated_words = int((row.get("translated_words") or "0").strip())
        source_sentences = int((row.get("source_sentences") or "0").strip())
        translated_sentences = int((row.get("translated_sentences") or "0").strip())
        anchor_count = int((row.get("anchor_count") or "0").strip())
        missing_anchor_count = int((row.get("missing_anchor_count") or "0").strip())
        char_ratio = float((row.get("char_ratio") or "0").strip())
        word_ratio = float((row.get("word_ratio") or "0").strip())
        sentence_ratio = float((row.get("sentence_ratio") or "0").strip())
        anchor_miss_ratio = float((row.get("anchor_miss_ratio") or "0").strip())
    except ValueError:
        return None

    reasons = (row.get("reasons") or "").strip()
    spacing_quality_low = (row.get("spacing_quality_low") or "").strip().lower() == "true"

    if spacing_quality_low:
        return None
    if source_chars < args.high_confidence_min_source_chars:
        return None
    if source_words < args.high_confidence_min_source_words:
        return None

    strong_shortening = (
        char_ratio <= args.high_confidence_max_char_ratio
        and word_ratio <= args.high_confidence_max_word_ratio
    )
    strong_collapse = (
        source_sentences >= 4
        and sentence_ratio <= args.high_confidence_max_sentence_ratio
        and translated_sentences <= max(2, source_sentences // 3)
    )
    strong_anchor_loss = anchor_count >= 4 and anchor_miss_ratio >= 0.85 and missing_anchor_count >= 4
    suspicious_reason_present = any(
        marker in reasons
        for marker in (
            "char_ratio_too_low",
            "word_ratio_too_low",
            "sentence_collapse",
            "long_text_sentence_collapse",
            "long_char_ratio_too_low",
        )
    )

    if not suspicious_reason_present:
        return None

    if strong_shortening and strong_collapse:
        return "severe_shortening_and_sentence_collapse"
    if strong_shortening and strong_anchor_loss:
        return "severe_shortening_and_anchor_loss"
    if strong_shortening:
        return "severe_shortening"
    if strong_collapse and strong_anchor_loss:
        return "sentence_collapse_and_anchor_loss"
    if strong_collapse and translated_chars <= max(180, int(source_chars * 0.40)):
        return "severe_sentence_collapse"
    return None


def run_high_confidence_report_mode(args: argparse.Namespace, output_csv_path: Path) -> int:
    source_report_path = Path(args.report_file)
    if not source_report_path.exists():
        print(f"Suspicious rows report not found: {source_report_path}", file=sys.stderr)
        return 1
    report_output_path = Path(args.high_confidence_report_file)
    if report_output_path.exists() and not args.rebuild_report:
        print(
            f"High-confidence report already exists: {report_output_path}\n"
            "To preserve the current pending queue, it was not rebuilt.\n"
            "If you really want to regenerate it from the broad suspicious report, rerun with --rebuild-report.",
            file=sys.stderr,
        )
        return 1

    rows, _ = load_csv_rows(source_report_path)
    selected: list[dict[str, str]] = []
    for row in rows:
        reason = high_confidence_mismatch_reason(row, args)
        if reason is None:
            continue
        enriched = dict(row)
        enriched["high_confidence_reason"] = reason
        enriched["source_report_action"] = row.get("action", "")
        enriched["action"] = "pending"
        enriched["new_translation"] = ""
        selected.append(enriched)

    selected.sort(
        key=lambda row: (
            -float((row.get("anchor_miss_ratio") or "0").strip() or 0),
            float((row.get("char_ratio") or "1").strip() or 1),
            int((row.get("row_id") or "0").strip() or 0),
        )
    )

    if args.max_rows > 0:
        selected = selected[: args.max_rows]

    fieldnames = [
        "row_id",
        "label",
        "source_chars",
        "translated_chars",
        "source_words",
        "translated_words",
        "source_sentences",
        "translated_sentences",
        "char_ratio",
        "word_ratio",
        "sentence_ratio",
        "anchor_count",
        "missing_anchor_count",
        "anchor_miss_ratio",
        "suspicion_score",
        "reasons",
        "high_confidence_reason",
        "source_report_action",
        "source_text",
        "current_translation",
        "new_translation",
        "action",
    ]
    normalized = []
    for row in selected:
        normalized.append({field: "" if row.get(field) is None else str(row.get(field, "")) for field in fieldnames})

    with report_output_path.open("w", encoding="utf-8-sig", newline="") as fh:
        writer = csv.DictWriter(fh, fieldnames=fieldnames)
        writer.writeheader()
        writer.writerows(normalized)

    if args.blank_flagged_translations:
        blanked = blank_translations_for_row_ids(
            output_path=output_csv_path,
            translated_col=args.translated_col,
            row_ids={int((row.get("row_id") or "0").strip() or 0) for row in selected},
        )
        print(f"Blanked flagged translations in output CSV: {blanked}")

    print(f"High-confidence mismatches written: {len(selected)}")
    print(f"High-confidence report file: {report_output_path}")
    return 0


def run_high_confidence_retranslate_mode(args: argparse.Namespace, client: str, output_path: Path) -> int:
    report_path = Path(args.high_confidence_report_file)
    if not report_path.exists():
        print(f"High-confidence report not found: {report_path}", file=sys.stderr)
        return 1

    report_rows, report_fieldnames = load_csv_rows(report_path)
    output_rows, output_fieldnames = load_csv_rows(output_path)
    index_map = row_id_to_index_map(output_rows)

    pending: list[dict[str, str]] = []
    skipped_done = 0
    for row in report_rows:
        action = (row.get("action") or "").strip()
        if action in {"retranslated", "rejected_blank"}:
            skipped_done += 1
            continue
        pending.append(row)

    if args.max_rows > 0:
        pending = pending[: args.max_rows]

    if not pending:
        print("No pending high-confidence rows found to retranslate.")
        return 0

    print(f"Pending high-confidence rows: {len(pending)}")
    print(f"Rows skipped because they were already marked as retranslated: {skipped_done}")
    print(f"Visible row batch size: {args.row_batch_size}")
    print(f"Chunk max chars: {args.chunk_max_chars}")

    if args.row_batch_size < 1:
        print("--row-batch-size must be at least 1.", file=sys.stderr)
        return 1

    def flush_progress(reason: str) -> None:
        write_csv_rows(output_path, output_rows, output_fieldnames)
        write_csv_rows(report_path, report_rows, report_fieldnames)
        print(f"Saving progress ({reason}) to {output_path} and {report_path} ...")

    updated_count = 0
    processed_since_flush = 0
    try:
        for batch_start in range(0, len(pending), args.row_batch_size):
            batch = pending[batch_start : batch_start + args.row_batch_size]
            batch_end = batch_start + len(batch) - 1
            print(
                f"Processing high-confidence row batch {batch_start + 1}..{batch_end + 1} "
                f"of {len(pending)} ..."
            )
            for position, report_row in enumerate(batch, start=batch_start + 1):
                raw_id = (report_row.get("row_id") or "").strip()
                if not raw_id:
                    report_row["action"] = "missing_row_id"
                    processed_since_flush += 1
                    continue
                try:
                    row_id = int(raw_id)
                except ValueError:
                    report_row["action"] = "invalid_row_id"
                    processed_since_flush += 1
                    continue

                output_index = index_map.get(row_id)
                if output_index is None:
                    report_row["action"] = "missing_row"
                    processed_since_flush += 1
                    continue

                source_text = (report_row.get("source_text") or "").strip()
                if not source_text:
                    source_text = (output_rows[output_index].get(args.source_col) or "").strip()
                label = report_row.get("label", "") or output_rows[output_index].get("status", "")
                if not source_text:
                    report_row["action"] = "missing_source_text"
                    processed_since_flush += 1
                    continue

                print(
                    f"[{position}/{len(pending)}] Retranslating high-confidence row {row_id} "
                    f"(reason={report_row.get('high_confidence_reason', '')}) ..."
                )
                try:
                    new_translation = translate_text_in_chunks(client, args.model, source_text, label, args)
                except ValueError as exc:
                    report_row["action"] = f"failed:{exc}"
                    print(f"Row {row_id} could not be retranslated: {exc}")
                    processed_since_flush += 1
                    continue

                if not new_translation:
                    report_row["action"] = "empty_retry"
                    processed_since_flush += 1
                    continue

                if args.interactive_review:
                    accepted = review_translation_interactively(
                        row_id=row_id,
                        label=label,
                        source_text=source_text,
                        proposed_translation=new_translation,
                        context="high-confidence",
                    )
                    if not accepted:
                        output_rows[output_index][args.translated_col] = ""
                        report_row["new_translation"] = new_translation
                        report_row["action"] = "rejected_blank"
                        processed_since_flush += 1
                        print(f"Row {row_id} rejected by user. Output was left blank.")
                        if args.save_every > 0 and processed_since_flush >= args.save_every:
                            flush_progress(f"every {args.save_every} rows")
                            processed_since_flush = 0
                        continue

                output_rows[output_index][args.translated_col] = new_translation
                report_row["new_translation"] = new_translation
                report_row["action"] = "retranslated"
                updated_count += 1
                processed_since_flush += 1
                time.sleep(args.sleep_seconds)

                if args.save_every > 0 and processed_since_flush >= args.save_every:
                    flush_progress(f"every {args.save_every} rows")
                    processed_since_flush = 0

            if args.save_every == 0:
                flush_progress(f"end of visible batch {batch_start + 1}..{batch_end + 1}")
    finally:
        flush_progress("final save")

    print(f"High-confidence rows updated: {updated_count}")
    print(f"Updated output file: {output_path}")
    print(f"Updated high-confidence report: {report_path}")
    return 0


def load_existing_report_map(path: Path) -> dict[int, dict[str, str]]:
    if not path.exists():
        return {}
    rows, _ = load_csv_rows(path)
    mapping: dict[int, dict[str, str]] = {}
    for row in rows:
        raw_id = (row.get("row_id") or "").strip()
        if not raw_id:
            continue
        try:
            row_id = int(raw_id)
        except ValueError:
            continue
        mapping[row_id] = row
    return mapping


def translate_text_in_chunks(
    client: str,
    model: str,
    source_text: str,
    label: str,
    args: argparse.Namespace,
) -> str:
    chunks = chunk_text(source_text, args.chunk_max_chars)
    if not chunks:
        raise ValueError("Could not build chunks.")

    translated_chunks: list[str] = []
    for chunk_index, chunk in enumerate(chunks, start=1):
        translated = translate_fragment(
            client=client,
            model=model,
            text=chunk,
            label=label,
            timeout=args.request_timeout,
            source_locale=args.source_locale,
            target_locale=args.target_locale,
        )
        translated_chunks.append(translated)
        if chunk_index < len(chunks):
            time.sleep(args.sleep_seconds)
    return "\n\n".join(translated_chunks).strip()


def preview_text(text: str, max_chars: int = 800) -> str:
    compact = text.strip()
    if len(compact) <= max_chars:
        return compact
    return compact[:max_chars].rstrip() + " ..."


def review_translation_interactively(
    *,
    row_id: int,
    label: str,
    source_text: str,
    proposed_translation: str,
    context: str,
) -> bool:
    print("")
    print("=" * 80)
    print(f"Interactive review for row {row_id} [{context}]")
    if label:
        print(f"Label: {label}")
    print("Source text:")
    print(preview_text(source_text))
    print("")
    print("Proposed translation:")
    print(preview_text(proposed_translation))
    print("=" * 80)
    while True:
        answer = input("Accept this translation and write it to the output file? [y/N]: ").strip().lower()
        if answer in {"y", "yes", "s", "si"}:
            return True
        if answer in {"", "n", "no"}:
            return False
        print("Please answer y/yes or n/no.")


def run_problematic_mode(args: argparse.Namespace, client: str, output_path: Path) -> int:
    problematic_path = Path(args.problematic_file)
    if not problematic_path.exists():
        print(f"Problematic rows file not found: {problematic_path}", file=sys.stderr)
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
        try:
            final_translation = translate_text_in_chunks(client, args.model, source_text, label, args)
        except ValueError as exc:
            print(f"Row {row_id} failed during chunked pass: {exc}")
            continue

        if args.interactive_review:
            accepted = review_translation_interactively(
                row_id=row_id,
                label=label,
                source_text=source_text,
                proposed_translation=final_translation,
                context="problematic",
            )
            if not accepted:
                output_rows[output_index][args.translated_col] = ""
                print(f"Row {row_id} rejected by user. Output remains blank.")
                continue

        output_rows[output_index][args.translated_col] = final_translation
        resolved_row_ids.add(row_id)
        print(f"Row {row_id} translated successfully via chunked pass.")

    write_csv_rows(output_path, output_rows, output_fieldnames)
    remove_resolved_problematic_rows(problematic_path, resolved_row_ids)
    print(f"Resolved problematic rows: {len(resolved_row_ids)}")
    print(f"Updated output file: {output_path}")
    return 0


def run_reevaluate_mode(args: argparse.Namespace, client: str, output_path: Path) -> int:
    output_rows, output_fieldnames = load_csv_rows(output_path)
    existing_report_map = load_existing_report_map(Path(args.report_file))
    suspicious_rows: list[dict[str, Any]] = []

    for row in output_rows:
        raw_id = (row.get("") or "").strip()
        if not raw_id:
            continue
        try:
            row_id = int(raw_id)
        except ValueError:
            continue
        suspicious = score_suspicious_translation(row, row_id, args.source_col, args.translated_col, args)
        if suspicious is not None:
            existing = existing_report_map.get(row_id, {})
            suspicious["new_translation"] = existing.get("new_translation", "")
            suspicious["action"] = existing.get("action", "flagged") or "flagged"
            suspicious_rows.append(suspicious)

    suspicious_rows.sort(key=lambda item: (-int(item["suspicion_score"]), int(item["row_id"])))

    if args.max_rows > 0:
        suspicious_rows = suspicious_rows[: args.max_rows]

    if not suspicious_rows:
        print("No suspicious translated rows were detected with the current thresholds.")
        write_report_rows(Path(args.report_file), [])
        print(f"Suspicious rows report written to: {args.report_file}")
        return 0

    if args.blank_flagged_translations:
        blanked = blank_translations_for_row_ids(
            output_path=output_path,
            translated_col=args.translated_col,
            row_ids={int((row.get("row_id") or "0").strip() or 0) for row in suspicious_rows},
        )
        print(f"Blanked flagged translations in output CSV: {blanked}")

    print(f"Suspicious translated rows detected: {len(suspicious_rows)}")
    print(f"Suspicious rows report will be written to: {args.report_file}")

    if args.dry_run:
        write_report_rows(Path(args.report_file), suspicious_rows)
        print("Dry run completed. No translations were modified.")
        return 0

    index_map = row_id_to_index_map(output_rows)
    updated_count = 0
    processed_since_flush = 0
    skipped_already_done = 0

    def flush_progress(reason: str) -> None:
        write_csv_rows(output_path, output_rows, output_fieldnames)
        write_report_rows(Path(args.report_file), suspicious_rows)
        print(
            f"Saving progress ({reason}) to {output_path} and {args.report_file} ..."
        )

    try:
        for position, report_row in enumerate(suspicious_rows, start=1):
            action = (report_row.get("action") or "").strip()
            if action in {"retranslated", "missing_row", "empty_retry", "rejected_blank"} or action.startswith("failed:"):
                skipped_already_done += 1
                continue

            row_id = int(report_row["row_id"])
            output_index = index_map.get(row_id)
            if output_index is None:
                report_row["action"] = "missing_row"
                processed_since_flush += 1
                if args.save_every > 0 and processed_since_flush >= args.save_every:
                    flush_progress(f"every {args.save_every} rows")
                    processed_since_flush = 0
                continue

            source_text = report_row["source_text"]
            label = report_row.get("label", "")
            print(
                f"[{position}/{len(suspicious_rows)}] Re-evaluating row {row_id} "
                f"(score={report_row['suspicion_score']}, reasons={report_row['reasons']}) ..."
            )
            try:
                new_translation = translate_text_in_chunks(client, args.model, source_text, label, args)
            except ValueError as exc:
                report_row["action"] = f"failed:{exc}"
                print(f"Row {row_id} could not be retranslated: {exc}")
                processed_since_flush += 1
                if args.save_every > 0 and processed_since_flush >= args.save_every:
                    flush_progress(f"every {args.save_every} rows")
                    processed_since_flush = 0
                continue

            report_row["new_translation"] = new_translation
            if not new_translation:
                report_row["action"] = "empty_retry"
                processed_since_flush += 1
                if args.save_every > 0 and processed_since_flush >= args.save_every:
                    flush_progress(f"every {args.save_every} rows")
                    processed_since_flush = 0
                continue

            if args.interactive_review:
                accepted = review_translation_interactively(
                    row_id=row_id,
                    label=label,
                    source_text=source_text,
                    proposed_translation=new_translation,
                    context="reevaluate",
                )
                if not accepted:
                    output_rows[output_index][args.translated_col] = ""
                    report_row["action"] = "rejected_blank"
                    processed_since_flush += 1
                    if args.save_every > 0 and processed_since_flush >= args.save_every:
                        flush_progress(f"every {args.save_every} rows")
                        processed_since_flush = 0
                    print(f"Row {row_id} rejected by user. Output was left blank.")
                    continue

            output_rows[output_index][args.translated_col] = new_translation
            report_row["action"] = "retranslated"
            updated_count += 1
            processed_since_flush += 1

            if args.save_every > 0 and processed_since_flush >= args.save_every:
                flush_progress(f"every {args.save_every} rows")
                processed_since_flush = 0

            time.sleep(args.sleep_seconds)
    finally:
        flush_progress("final save")

    print(f"Reevaluated rows updated: {updated_count}")
    print(f"Rows skipped because they were already recorded in the report: {skipped_already_done}")
    print(f"Updated output file: {output_path}")
    print(f"Suspicious rows report written to: {args.report_file}")
    return 0


def run() -> int:
    args = parse_args()
    args.source_locale = normalize_locale(args.source_locale)
    args.target_locale = normalize_locale(args.target_locale)
    if not args.translated_col:
        args.translated_col = default_translated_col_for_locale(args.target_locale)
    if not args.output:
        args.output = str(default_output_file_for_locale(args.target_locale))
    output_path = Path(args.output)

    if args.mode == "high-confidence-report":
        if not output_path.exists():
            print(f"Output file not found: {output_path}", file=sys.stderr)
            return 1
        return run_high_confidence_report_mode(args, output_path)

    if not output_path.exists():
        print(f"Output file not found: {output_path}", file=sys.stderr)
        return 1

    try:
        client = build_client(args)
    except ValueError as exc:
        print(str(exc), file=sys.stderr)
        return 1

    if args.mode == "problematic":
        return run_problematic_mode(args, client, output_path)
    if args.mode == "high-confidence-retranslate":
        return run_high_confidence_retranslate_mode(args, client, output_path)
    return run_reevaluate_mode(args, client, output_path)


if __name__ == "__main__":
    raise SystemExit(run())
