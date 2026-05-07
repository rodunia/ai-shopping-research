#!/usr/bin/env python3
"""Offline/manual Reddit research data cleaner.

This script never connects to Reddit. It only cleans user-provided CSV rows so
the analysis pipeline can be tested before Reddit API credentials are available.
"""

from __future__ import annotations

import argparse
import csv
import hashlib
import json
import logging
import re
import sys
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

SCRIPT_VERSION = "1.0.0"
REDDIT_BASE_URL = "https://www.reddit.com"
REMOVED_MARKERS = {"[deleted]", "[removed]", "deleted", "removed"}

CSV_FIELDS = [
    "source_type",
    "subreddit",
    "query_keyword",
    "post_id",
    "comment_id",
    "parent_id",
    "created_utc",
    "score",
    "num_comments",
    "title",
    "text",
    "permalink",
    "url",
    "collected_at_utc",
    "collection_mode",
    "notes",
]


@dataclass(frozen=True)
class OfflineConfig:
    input_csv_path: Path
    output_csv_path: Path
    manifest_path: Path
    log_file_path: Path


@dataclass
class OfflineStats:
    rows_read: int = 0
    rows_written: int = 0
    rows_skipped_empty_or_removed: int = 0
    duplicate_rows_skipped: int = 0
    errors: list[str] | None = None

    def __post_init__(self) -> None:
        if self.errors is None:
            self.errors = []

    def to_dict(self) -> dict[str, Any]:
        return {
            "rows_read": self.rows_read,
            "rows_written": self.rows_written,
            "rows_skipped_empty_or_removed": self.rows_skipped_empty_or_removed,
            "duplicate_rows_skipped": self.duplicate_rows_skipped,
            "errors": self.errors,
        }


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Clean manually provided Reddit example rows without API access."
    )
    parser.add_argument(
        "--config",
        default="config.yaml",
        help="Path to config.yaml. Defaults to config.yaml.",
    )
    parser.add_argument(
        "--input",
        default=None,
        help="Manual input CSV. Defaults to offline_input_csv_path in config.",
    )
    parser.add_argument(
        "--output",
        default=None,
        help="Cleaned output CSV. Defaults to offline_output_csv_path in config.",
    )
    parser.add_argument(
        "--version",
        action="version",
        version=f"offline_cleaner.py {SCRIPT_VERSION}",
    )
    return parser.parse_args()


def load_config(config_path: Path, input_override: str | None, output_override: str | None) -> OfflineConfig:
    raw: dict[str, Any] = {}
    if config_path.exists():
        try:
            import yaml
        except ImportError as exc:
            raise RuntimeError("Missing dependency: PyYAML. Install with `pip install -r requirements.txt`.") from exc
        with config_path.open("r", encoding="utf-8") as file_handle:
            raw = yaml.safe_load(file_handle) or {}

    base_dir = config_path.resolve().parent
    input_path = input_override or raw.get("offline_input_csv_path", "data/manual_input.csv")
    output_path = output_override or raw.get("offline_output_csv_path", "data/processed/manual_cleaned.csv")

    return OfflineConfig(
        input_csv_path=resolve_path(input_path, base_dir),
        output_csv_path=resolve_path(output_path, base_dir),
        manifest_path=resolve_path(
            raw.get("offline_manifest_path", "manifests/offline_manifest.json"), base_dir
        ),
        log_file_path=resolve_path(raw.get("offline_log_file_path", "logs/offline_cleaner.log"), base_dir),
    )


def resolve_path(value: Any, base_dir: Path) -> Path:
    path = Path(str(value))
    if path.is_absolute():
        return path
    return base_dir / path


def setup_logger(log_file_path: Path) -> logging.Logger:
    logger = logging.getLogger("offline_cleaner")
    logger.handlers.clear()
    logger.setLevel(logging.INFO)
    logger.propagate = False

    formatter = logging.Formatter(
        fmt="%(asctime)sZ %(levelname)s %(message)s",
        datefmt="%Y-%m-%dT%H:%M:%S",
    )
    formatter.converter = time.gmtime

    console_handler = logging.StreamHandler()
    console_handler.setFormatter(formatter)
    logger.addHandler(console_handler)

    log_file_path.parent.mkdir(parents=True, exist_ok=True)
    file_handler = logging.FileHandler(log_file_path, encoding="utf-8")
    file_handler.setFormatter(formatter)
    logger.addHandler(file_handler)
    return logger


def utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")


def clean_text(value: Any) -> str:
    if value is None:
        return ""
    return re.sub(r"\s+", " ", str(value)).strip()


def normalize_source_type(value: Any) -> str:
    source_type = clean_text(value).lower()
    if source_type in {"post", "submission"}:
        return "post"
    if source_type in {"comment", "reply"}:
        return "comment"
    return source_type or "manual"


def normalize_subreddit(value: Any) -> str:
    subreddit = clean_text(value)
    if subreddit.lower().startswith("r/"):
        return subreddit[2:]
    return subreddit


def normalize_permalink(value: Any) -> str:
    permalink = clean_text(value)
    if not permalink:
        return ""
    if permalink.startswith("http://") or permalink.startswith("https://"):
        return permalink
    if permalink.startswith("/"):
        return f"{REDDIT_BASE_URL}{permalink}"
    return permalink


def is_removed_or_deleted(value: str) -> bool:
    return clean_text(value).lower() in REMOVED_MARKERS


def should_skip_row(title: str, text: str) -> bool:
    if not title and not text:
        return True
    if title and is_removed_or_deleted(title):
        return True
    if text and is_removed_or_deleted(text):
        return True
    return False


def text_hash(title: str, text: str) -> str:
    text_for_hash = text or title
    return hashlib.sha256(text_for_hash.encode("utf-8")).hexdigest()


def dedupe_key(row: dict[str, str]) -> str:
    permalink = row.get("permalink", "").lower()
    return f"{permalink}|{text_hash(row.get('title', ''), row.get('text', ''))}"


def build_clean_row(row: dict[str, Any]) -> dict[str, str] | None:
    title = clean_text(row.get("title", ""))
    text = clean_text(row.get("text", ""))
    if should_skip_row(title, text):
        return None

    return {
        "source_type": normalize_source_type(row.get("source_type", "")),
        "subreddit": normalize_subreddit(row.get("subreddit", "")),
        "query_keyword": clean_text(row.get("query_keyword", "")),
        "post_id": clean_text(row.get("post_id", "")),
        "comment_id": clean_text(row.get("comment_id", "")),
        "parent_id": clean_text(row.get("parent_id", "")),
        "created_utc": clean_text(row.get("created_utc", "")),
        "score": clean_text(row.get("score", "")),
        "num_comments": clean_text(row.get("num_comments", "")),
        "title": title,
        "text": text,
        "permalink": normalize_permalink(row.get("permalink", "")),
        "url": clean_text(row.get("url", "")),
        "collected_at_utc": utc_now_iso(),
        "collection_mode": "offline_manual",
        "notes": "user-provided manual input; no API collection performed",
    }


def write_manifest(config: OfflineConfig, stats: OfflineStats, status: str, error: str | None = None) -> None:
    config.manifest_path.parent.mkdir(parents=True, exist_ok=True)
    record = {
        "run_id": datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ"),
        "status": status,
        "script_version": SCRIPT_VERSION,
        "collection_mode": "offline_manual",
        "api_collection_performed": False,
        "started_or_finished_at_utc": utc_now_iso(),
        "input_file": str(config.input_csv_path),
        "output_file": str(config.output_csv_path),
        "manifest_note": "Offline/manual mode only cleaned user-provided CSV rows. No Reddit API collection was performed.",
        "stats": stats.to_dict(),
        "error": error,
    }

    if config.manifest_path.exists() and config.manifest_path.stat().st_size > 0:
        with config.manifest_path.open("r", encoding="utf-8") as file_handle:
            manifest = json.load(file_handle)
        if "runs" not in manifest:
            manifest = {"manifest_schema_version": 1, "runs": [manifest]}
    else:
        manifest = {"manifest_schema_version": 1, "runs": []}

    manifest["runs"].append(record)
    temporary_path = config.manifest_path.with_suffix(config.manifest_path.suffix + ".tmp")
    with temporary_path.open("w", encoding="utf-8") as file_handle:
        json.dump(manifest, file_handle, indent=2, sort_keys=True)
        file_handle.write("\n")
    temporary_path.replace(config.manifest_path)


def clean_manual_csv(config: OfflineConfig) -> OfflineStats:
    logger = setup_logger(config.log_file_path)
    stats = OfflineStats()
    seen: set[str] = set()

    logger.info("Starting offline/manual cleaning.")
    logger.info("Input CSV: %s", config.input_csv_path)
    logger.info("Output CSV: %s", config.output_csv_path)

    if not config.input_csv_path.exists():
        message = (
            f"Manual input CSV not found: {config.input_csv_path}. "
            "Create this file yourself; this script will not collect Reddit data."
        )
        stats.errors.append(message)
        write_manifest(config, stats, status="failed", error=message)
        raise FileNotFoundError(message)

    config.output_csv_path.parent.mkdir(parents=True, exist_ok=True)

    with config.input_csv_path.open("r", encoding="utf-8", newline="") as input_file, config.output_csv_path.open(
        "w", encoding="utf-8", newline=""
    ) as output_file:
        reader = csv.DictReader(input_file)
        writer = csv.DictWriter(output_file, fieldnames=CSV_FIELDS)
        writer.writeheader()

        for raw_row in reader:
            stats.rows_read += 1
            clean_row = build_clean_row(raw_row)
            if clean_row is None:
                stats.rows_skipped_empty_or_removed += 1
                continue

            key = dedupe_key(clean_row)
            if key in seen:
                stats.duplicate_rows_skipped += 1
                continue

            seen.add(key)
            writer.writerow(clean_row)
            stats.rows_written += 1

    write_manifest(config, stats, status="completed")
    logger.info(
        "Finished offline/manual cleaning: %s read, %s written, %s duplicates skipped.",
        stats.rows_read,
        stats.rows_written,
        stats.duplicate_rows_skipped,
    )
    return stats


def main() -> int:
    args = parse_args()
    config_path = Path(args.config).resolve()

    try:
        config = load_config(config_path, args.input, args.output)
        clean_manual_csv(config)
    except KeyboardInterrupt:
        print("Interrupted by user.", file=sys.stderr)
        return 130
    except Exception as exc:
        print(f"Error: {exc}", file=sys.stderr)
        return 1

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
