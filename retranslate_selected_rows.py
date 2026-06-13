#!/usr/bin/env python3
from __future__ import annotations

import argparse
import csv
import re
import sys
import time
from pathlib import Path

from translate_problematic_rows import (
    build_client,
    COMPLETED_REPORT_ACTIONS,
    default_output_file_for_locale,
    default_translated_col_for_locale,
    load_csv_rows,
    normalize_report_action,
    normalize_locale,
    review_translation_interactively,
    row_id_to_index_map,
    translate_text_in_chunks,
    write_csv_rows,
)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=(
            "Retranslate only a selected list of row_id values from the translated dataset, "
            "without rebuilding the broader suspicious/problematic queues."
        )
    )
    parser.add_argument("--provider", choices=["ollama"], default="ollama")
    parser.add_argument("--model", default="gemma3:4b")
    parser.add_argument("--source-locale", default="en-US")
    parser.add_argument("--target-locale", default="es-ES")
    parser.add_argument("--base-url", default="", help="For Ollama the default is http://localhost:11434")
    parser.add_argument("--input", default="Combined Data.csv")
    parser.add_argument("--output", default="")
    parser.add_argument("--source-col", default="statement")
    parser.add_argument("--label-col", default="status")
    parser.add_argument("--translated-col", default="")
    parser.add_argument(
        "--row-ids",
        default="",
        help="Comma/space/newline-separated row_id values to retranslate.",
    )
    parser.add_argument(
        "--row-ids-file",
        default="",
        help="Optional text or CSV file containing row_id values. If CSV, it must contain a row_id column.",
    )
    parser.add_argument(
        "--report-file",
        default=".translation_selected_rows_retranslation.csv",
        help="CSV file used to store progress and outcomes for the selected rows.",
    )
    parser.add_argument("--request-timeout", type=float, default=120.0)
    parser.add_argument("--chunk-max-chars", type=int, default=500)
    parser.add_argument(
        "--min-chunk-chars",
        type=int,
        default=80,
        help="Smallest fragment size allowed when a chunk must be split again after a failed or suspicious response.",
    )
    parser.add_argument("--row-batch-size", type=int, default=20)
    parser.add_argument("--save-every", type=int, default=20)
    parser.add_argument("--sleep-seconds", type=float, default=0.5)
    parser.add_argument(
        "--interactive-review",
        action="store_true",
        help="Ask whether to accept each proposed translation before writing it to the output CSV.",
    )
    parser.add_argument(
        "--blank-on-start",
        action="store_true",
        help="Blank the translated column for the selected rows before starting the new pass.",
    )
    parser.add_argument(
        "--rebuild-report",
        action="store_true",
        help="Rebuild the selected-rows report even if it already exists.",
    )
    return parser.parse_args()


def parse_row_ids_text(text: str) -> list[int]:
    matches = re.findall(r"\d+", text)
    seen: set[int] = set()
    ordered: list[int] = []
    for match in matches:
        value = int(match)
        if value in seen:
            continue
        seen.add(value)
        ordered.append(value)
    return ordered


def load_row_ids_from_file(path: Path) -> list[int]:
    if path.suffix.lower() == ".csv":
        with path.open("r", encoding="utf-8-sig", newline="") as fh:
            reader = csv.DictReader(fh)
            if reader.fieldnames is None or "row_id" not in reader.fieldnames:
                raise ValueError(f"CSV file must contain a row_id column: {path}")
            values: list[int] = []
            for row in reader:
                raw = (row.get("row_id") or "").strip()
                if not raw:
                    continue
                values.append(int(raw))
            return values
    return parse_row_ids_text(path.read_text(encoding="utf-8"))


def load_selected_row_ids(args: argparse.Namespace) -> list[int]:
    collected: list[int] = []
    if args.row_ids.strip():
        collected.extend(parse_row_ids_text(args.row_ids))
    if args.row_ids_file:
        collected.extend(load_row_ids_from_file(Path(args.row_ids_file)))
    seen: set[int] = set()
    ordered: list[int] = []
    for value in collected:
        if value in seen:
            continue
        seen.add(value)
        ordered.append(value)
    return ordered


def report_row_id_to_index_map(rows: list[dict[str, str]]) -> dict[int, int]:
    mapping: dict[int, int] = {}
    for idx, row in enumerate(rows):
        raw_id = (row.get("row_id") or "").strip()
        if not raw_id:
            continue
        try:
            mapping[int(raw_id)] = idx
        except ValueError:
            continue
    return mapping


def summarize_report_actions(rows: list[dict[str, str]]) -> dict[str, int]:
    summary: dict[str, int] = {}
    for row in rows:
        action = normalize_report_action(row.get("action", "")) or "<empty>"
        summary[action] = summary.get(action, 0) + 1
    return summary


def build_initial_report_rows(
    selected_row_ids: list[int],
    source_rows: list[dict[str, str]],
    source_index_map: dict[int, int],
    output_rows: list[dict[str, str]],
    output_index_map: dict[int, int],
    args: argparse.Namespace,
) -> list[dict[str, str]]:
    rows: list[dict[str, str]] = []
    for row_id in selected_row_ids:
        source_index = source_index_map.get(row_id)
        output_index = output_index_map.get(row_id)
        if source_index is None or output_index is None:
            rows.append(
                {
                    "row_id": str(row_id),
                    "label": "",
                    "source_text": "",
                    "current_translation": "",
                    "new_translation": "",
                    "action": "missing_row",
                }
            )
            continue
        source_row = source_rows[source_index]
        output_row = output_rows[output_index]
        current_translation = output_row.get(args.translated_col, "")
        rows.append(
            {
                "row_id": str(row_id),
                "label": source_row.get(args.label_col, ""),
                "source_text": source_row.get(args.source_col, ""),
                "current_translation": current_translation,
                "new_translation": "",
                "action": "success" if current_translation.strip() else "pending",
            }
        )
    return rows


def load_or_build_report_rows(
    report_path: Path,
    selected_row_ids: list[int],
    source_rows: list[dict[str, str]],
    source_index_map: dict[int, int],
    output_rows: list[dict[str, str]],
    output_index_map: dict[int, int],
    args: argparse.Namespace,
) -> list[dict[str, str]]:
    fresh_rows = build_initial_report_rows(
        selected_row_ids,
        source_rows,
        source_index_map,
        output_rows,
        output_index_map,
        args,
    )
    if report_path.exists() and not args.rebuild_report:
        existing_rows, _ = load_csv_rows(report_path)
        existing_map = report_row_id_to_index_map(existing_rows)
        merged_rows: list[dict[str, str]] = []
        reused = 0
        added = 0
        for fresh_row in fresh_rows:
            row_id_raw = (fresh_row.get("row_id") or "").strip()
            if not row_id_raw:
                continue
            row_id = int(row_id_raw)
            existing_index = existing_map.get(row_id)
            if existing_index is None:
                merged_rows.append(fresh_row)
                added += 1
                continue
            existing_row = dict(existing_rows[existing_index])
            existing_row["action"] = normalize_report_action(existing_row.get("action", ""))
            existing_row["label"] = fresh_row.get("label", existing_row.get("label", ""))
            existing_row["source_text"] = fresh_row.get("source_text", existing_row.get("source_text", ""))
            existing_row["current_translation"] = fresh_row.get(
                "current_translation", existing_row.get("current_translation", "")
            )
            merged_rows.append(existing_row)
            reused += 1

        fieldnames = ["row_id", "label", "source_text", "current_translation", "new_translation", "action"]
        write_csv_rows(report_path, merged_rows, fieldnames)
        action_summary = summarize_report_actions(merged_rows)
        print(
            f"Using existing selected-rows report: {report_path} "
            f"(reused {reused}, added {added} from the current row_id list)"
        )
        print(f"Selected-rows report action summary: {action_summary}")
        return merged_rows
    fieldnames = ["row_id", "label", "source_text", "current_translation", "new_translation", "action"]
    write_csv_rows(report_path, fresh_rows, fieldnames)
    print(f"Selected-rows report created: {report_path}")
    print(f"Selected-rows report action summary: {summarize_report_actions(fresh_rows)}")
    return fresh_rows


def blank_selected_rows(
    output_rows: list[dict[str, str]],
    index_map: dict[int, int],
    translated_col: str,
    selected_row_ids: list[int],
) -> int:
    blanked = 0
    for row_id in selected_row_ids:
        output_index = index_map.get(row_id)
        if output_index is None:
            continue
        if (output_rows[output_index].get(translated_col) or "").strip():
            output_rows[output_index][translated_col] = ""
            blanked += 1
    return blanked


def run() -> int:
    args = parse_args()
    args.source_locale = normalize_locale(args.source_locale)
    args.target_locale = normalize_locale(args.target_locale)
    if not args.translated_col:
        args.translated_col = default_translated_col_for_locale(args.target_locale)
    if not args.output:
        args.output = str(default_output_file_for_locale(args.target_locale))

    output_path = Path(args.output)
    input_path = Path(args.input)
    report_path = Path(args.report_file)
    if not input_path.exists():
        print(f"Input file not found: {input_path}", file=sys.stderr)
        return 1
    if not output_path.exists():
        print(f"Output file not found: {output_path}", file=sys.stderr)
        return 1

    selected_row_ids = load_selected_row_ids(args)
    if not selected_row_ids:
        print("No row_id values were provided. Use --row-ids or --row-ids-file.", file=sys.stderr)
        return 1

    if args.row_batch_size < 1:
        print("--row-batch-size must be at least 1.", file=sys.stderr)
        return 1

    try:
        client = build_client(args)
    except ValueError as exc:
        print(str(exc), file=sys.stderr)
        return 1

    source_rows, _ = load_csv_rows(input_path)
    output_rows, output_fieldnames = load_csv_rows(output_path)
    source_index_map = row_id_to_index_map(source_rows)
    output_index_map = row_id_to_index_map(output_rows)
    report_rows = load_or_build_report_rows(
        report_path,
        selected_row_ids,
        source_rows,
        source_index_map,
        output_rows,
        output_index_map,
        args,
    )
    report_index_map = report_row_id_to_index_map(report_rows)
    print(f"Selected row_ids requested: {len(selected_row_ids)}")
    print(f"Selected rows found in report index: {len(report_index_map)}")

    if args.blank_on_start:
        blanked = blank_selected_rows(output_rows, output_index_map, args.translated_col, selected_row_ids)
        if blanked:
            write_csv_rows(output_path, output_rows, output_fieldnames)
        print(f"Blanked selected rows at start: {blanked}")

    pending_row_ids: list[int] = []
    skipped_done = 0
    for row_id in selected_row_ids:
        report_index = report_index_map.get(row_id)
        if report_index is None:
            continue
        action = normalize_report_action(report_rows[report_index].get("action", ""))
        report_rows[report_index]["action"] = action
        if action in COMPLETED_REPORT_ACTIONS or action.startswith("failed:"):
            skipped_done += 1
            continue
        pending_row_ids.append(row_id)

    if not pending_row_ids:
        print("No pending selected rows found to retranslate.")
        print(f"Rows already completed or skipped: {skipped_done}")
        return 0

    print(f"Selected rows total: {len(selected_row_ids)}")
    print(f"Pending selected rows: {len(pending_row_ids)}")
    print(f"Rows already completed or skipped: {skipped_done}")
    print(f"Visible row batch size: {args.row_batch_size}")

    def flush_progress(reason: str) -> None:
        write_csv_rows(output_path, output_rows, output_fieldnames)
        write_csv_rows(report_path, report_rows, ["row_id", "label", "source_text", "current_translation", "new_translation", "action"])
        print(f"Saving progress ({reason}) to {output_path} and {report_path} ...")

    updated_count = 0
    processed_since_flush = 0
    try:
        for batch_start in range(0, len(pending_row_ids), args.row_batch_size):
            batch = pending_row_ids[batch_start : batch_start + args.row_batch_size]
            batch_end = batch_start + len(batch) - 1
            print(f"Processing selected row batch {batch_start + 1}..{batch_end + 1} of {len(pending_row_ids)} ...")
            for position, row_id in enumerate(batch, start=batch_start + 1):
                output_index = output_index_map.get(row_id)
                source_index = source_index_map.get(row_id)
                report_index = report_index_map.get(row_id)
                if output_index is None or source_index is None or report_index is None:
                    continue

                source_row = source_rows[source_index]
                source_text = (source_row.get(args.source_col) or "").strip()
                label = source_row.get(args.label_col, "")
                if not source_text:
                    report_rows[report_index]["action"] = "missing_source_text"
                    processed_since_flush += 1
                    continue

                print(f"[{position}/{len(pending_row_ids)}] Retranslating selected row {row_id} ...")
                try:
                    new_translation = translate_text_in_chunks(client, args.model, source_text, label, args)
                except ValueError as exc:
                    report_rows[report_index]["action"] = f"failed:{exc}"
                    print(f"Row {row_id} could not be retranslated: {exc}")
                    processed_since_flush += 1
                    if args.save_every > 0 and processed_since_flush >= args.save_every:
                        flush_progress(f"every {args.save_every} rows")
                        processed_since_flush = 0
                    continue

                report_rows[report_index]["new_translation"] = new_translation
                if not new_translation:
                    report_rows[report_index]["action"] = "empty_retry"
                    processed_since_flush += 1
                    continue

                if args.interactive_review:
                    accepted = review_translation_interactively(
                        row_id=row_id,
                        label=label,
                        source_text=source_text,
                        proposed_translation=new_translation,
                        context="selected-rows",
                    )
                    if not accepted:
                        output_rows[output_index][args.translated_col] = ""
                        report_rows[report_index]["action"] = "rejected_blank"
                        print(f"Row {row_id} rejected by user. Output was left blank.")
                        processed_since_flush += 1
                        if args.save_every > 0 and processed_since_flush >= args.save_every:
                            flush_progress(f"every {args.save_every} rows")
                            processed_since_flush = 0
                        continue

                output_rows[output_index][args.translated_col] = new_translation
                report_rows[report_index]["action"] = "success"
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

    print(f"Selected rows updated: {updated_count}")
    print(f"Updated output file: {output_path}")
    print(f"Selected-rows report file: {report_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(run())
