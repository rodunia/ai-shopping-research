#!/usr/bin/env python3
"""Read-only Reddit collector for academic research.

This script uses the official Reddit API through PRAW. It does not scrape
Reddit HTML pages, submit content, vote, send messages, or collect usernames.
"""

from __future__ import annotations

import argparse
import csv
import json
import logging
import os
import random
import sys
import time
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable

SCRIPT_VERSION = "1.0.0"
REDDIT_BASE_URL = "https://www.reddit.com"
REMOVED_MARKERS = {"[deleted]", "[removed]"}
CREDENTIALS_MISSING_MESSAGE = "Reddit API credentials are missing. API collection is disabled."

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

VALID_TIME_FILTERS = {"all", "day", "hour", "month", "week", "year"}
VALID_SEARCH_SORTS = {"relevance", "hot", "top", "new", "comments"}


class CollectionCapReached(Exception):
    """Raised internally when the configured safety cap has been reached."""


@dataclass(frozen=True)
class CollectorConfig:
    subreddits: list[str]
    keywords: list[str]
    time_filter: str
    sort: str
    max_posts_per_query: int
    max_comments_per_post: int
    comment_depth: int
    comment_sort: str
    max_total_items: int
    output_csv_path: Path
    log_file_path: Path
    manifest_path: Path
    request_delay_seconds: float
    retry_attempts: int
    backoff_initial_seconds: float
    backoff_max_seconds: float

    def to_manifest_dict(self) -> dict[str, Any]:
        data = asdict(self)
        data["output_csv_path"] = str(self.output_csv_path)
        data["log_file_path"] = str(self.log_file_path)
        data["manifest_path"] = str(self.manifest_path)
        return data


@dataclass
class RunStats:
    posts_collected: int = 0
    comments_collected: int = 0
    posts_skipped: int = 0
    comments_skipped: int = 0
    duplicate_rows_skipped: int = 0
    errors: list[str] | None = None
    searches: list[dict[str, Any]] | None = None

    def __post_init__(self) -> None:
        if self.errors is None:
            self.errors = []
        if self.searches is None:
            self.searches = []

    @property
    def total_collected(self) -> int:
        return self.posts_collected + self.comments_collected

    def to_dict(self) -> dict[str, Any]:
        return {
            "posts_collected": self.posts_collected,
            "comments_collected": self.comments_collected,
            "total_collected": self.total_collected,
            "posts_skipped": self.posts_skipped,
            "comments_skipped": self.comments_skipped,
            "duplicate_rows_skipped": self.duplicate_rows_skipped,
            "errors": self.errors,
            "searches": self.searches,
        }


class CsvSink:
    """Append rows incrementally and prevent duplicate Reddit IDs."""

    def __init__(self, path: Path, dry_run: bool, logger: logging.Logger) -> None:
        self.path = path
        self.dry_run = dry_run
        self.logger = logger
        self.seen_ids = self._load_existing_ids(path)
        self.file_handle = None
        self.writer: csv.DictWriter[str] | None = None

        if not dry_run:
            path.parent.mkdir(parents=True, exist_ok=True)
            needs_header = not path.exists() or path.stat().st_size == 0
            self.file_handle = path.open("a", encoding="utf-8", newline="")
            self.writer = csv.DictWriter(self.file_handle, fieldnames=CSV_FIELDS)
            if needs_header:
                self.writer.writeheader()
                self.file_handle.flush()

    def close(self) -> None:
        if self.file_handle is not None:
            self.file_handle.close()

    def write_row(self, row: dict[str, Any]) -> bool:
        row_id = row_unique_id(row)
        if not row_id or row_id in self.seen_ids:
            return False

        self.seen_ids.add(row_id)

        if self.dry_run:
            print_dry_run_row(row)
            return True

        if self.writer is None or self.file_handle is None:
            raise RuntimeError("CSV writer is not available.")

        self.writer.writerow({field: row.get(field, "") for field in CSV_FIELDS})
        self.file_handle.flush()
        return True

    @staticmethod
    def _load_existing_ids(path: Path) -> set[str]:
        if not path.exists() or path.stat().st_size == 0:
            return set()

        seen: set[str] = set()
        with path.open("r", encoding="utf-8", newline="") as file_handle:
            reader = csv.DictReader(file_handle)
            for row in reader:
                row_id = row_unique_id(row)
                if row_id:
                    seen.add(row_id)
        return seen


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Collect public Reddit posts and comments through the official API using PRAW."
    )
    parser.add_argument(
        "--config",
        default="config.yaml",
        help="Path to the YAML config file. Defaults to config.yaml.",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Print rows that would be collected without writing CSV, logs, or manifest files.",
    )
    parser.add_argument(
        "--max-total-items",
        type=int,
        default=None,
        help="Override the config safety cap for total post/comment rows.",
    )
    parser.add_argument(
        "--version",
        action="version",
        version=f"reddit_collector.py {SCRIPT_VERSION}",
    )
    return parser.parse_args()


def load_config(config_path: Path, max_total_items_override: int | None) -> CollectorConfig:
    try:
        import yaml
    except ImportError as exc:
        raise RuntimeError("Missing dependency: PyYAML. Install with `pip install -r requirements.txt`.") from exc

    if not config_path.exists():
        raise FileNotFoundError(f"Config file not found: {config_path}")

    with config_path.open("r", encoding="utf-8") as file_handle:
        raw = yaml.safe_load(file_handle) or {}

    base_dir = config_path.resolve().parent
    subreddits = normalize_list(raw.get("subreddits"), "subreddits")
    keywords = normalize_list(raw.get("keywords"), "keywords")

    time_filter = str(raw.get("time_filter", "year")).lower()
    sort = str(raw.get("sort", "relevance")).lower()
    if time_filter not in VALID_TIME_FILTERS:
        raise ValueError(f"time_filter must be one of {sorted(VALID_TIME_FILTERS)}")
    if sort not in VALID_SEARCH_SORTS:
        raise ValueError(f"sort must be one of {sorted(VALID_SEARCH_SORTS)}")

    max_total_items = int(raw.get("max_total_items", 1000))
    if max_total_items_override is not None:
        max_total_items = max_total_items_override
    if max_total_items <= 0:
        raise ValueError("max_total_items must be a positive integer.")

    return CollectorConfig(
        subreddits=subreddits,
        keywords=keywords,
        time_filter=time_filter,
        sort=sort,
        max_posts_per_query=positive_int(raw.get("max_posts_per_query", 25), "max_posts_per_query"),
        max_comments_per_post=non_negative_int(
            raw.get("max_comments_per_post", 50), "max_comments_per_post"
        ),
        comment_depth=non_negative_int(raw.get("comment_depth", 1), "comment_depth"),
        comment_sort=str(raw.get("comment_sort", "top")),
        max_total_items=max_total_items,
        output_csv_path=resolve_path(
            raw.get("output_csv_path", "data/processed/reddit_api_collected.csv"), base_dir
        ),
        log_file_path=resolve_path(raw.get("log_file_path", "logs/reddit_collector.log"), base_dir),
        manifest_path=resolve_path(
            raw.get("api_manifest_path", raw.get("manifest_path", "manifests/collection_manifest.json")),
            base_dir,
        ),
        request_delay_seconds=non_negative_float(
            raw.get("request_delay_seconds", 1.0), "request_delay_seconds"
        ),
        retry_attempts=positive_int(raw.get("retry_attempts", 5), "retry_attempts"),
        backoff_initial_seconds=positive_float(
            raw.get("backoff_initial_seconds", 2.0), "backoff_initial_seconds"
        ),
        backoff_max_seconds=positive_float(
            raw.get("backoff_max_seconds", 60.0), "backoff_max_seconds"
        ),
    )


def normalize_list(value: Any, name: str) -> list[str]:
    if not isinstance(value, list):
        raise ValueError(f"{name} must be a YAML list.")

    normalized = [str(item).strip().removeprefix("r/") for item in value if str(item).strip()]
    if not normalized:
        raise ValueError(f"{name} must contain at least one value.")
    return normalized


def positive_int(value: Any, name: str) -> int:
    number = int(value)
    if number <= 0:
        raise ValueError(f"{name} must be a positive integer.")
    return number


def non_negative_int(value: Any, name: str) -> int:
    number = int(value)
    if number < 0:
        raise ValueError(f"{name} must be zero or greater.")
    return number


def positive_float(value: Any, name: str) -> float:
    number = float(value)
    if number <= 0:
        raise ValueError(f"{name} must be greater than zero.")
    return number


def non_negative_float(value: Any, name: str) -> float:
    number = float(value)
    if number < 0:
        raise ValueError(f"{name} must be zero or greater.")
    return number


def resolve_path(value: Any, base_dir: Path) -> Path:
    path = Path(str(value))
    if path.is_absolute():
        return path
    return base_dir / path


def setup_logger(config: CollectorConfig, dry_run: bool) -> logging.Logger:
    logger = logging.getLogger("reddit_collector")
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

    if not dry_run:
        config.log_file_path.parent.mkdir(parents=True, exist_ok=True)
        file_handler = logging.FileHandler(config.log_file_path, encoding="utf-8")
        file_handler.setFormatter(formatter)
        logger.addHandler(file_handler)

    return logger


def load_env_file(env_path: Path) -> None:
    """Load simple KEY=VALUE entries without requiring API dependencies."""
    if not env_path.exists():
        return

    with env_path.open("r", encoding="utf-8") as file_handle:
        for line in file_handle:
            stripped = line.strip()
            if not stripped or stripped.startswith("#") or "=" not in stripped:
                continue
            key, value = stripped.split("=", 1)
            key = key.strip()
            value = value.strip().strip('"').strip("'")
            if key and key not in os.environ:
                os.environ[key] = value


def ensure_api_credentials(env_path: Path | None = None) -> None:
    """Fail closed when OAuth credentials are not configured."""
    load_env_file(env_path or Path(".env"))
    required_env = ["REDDIT_CLIENT_ID", "REDDIT_CLIENT_SECRET", "REDDIT_USER_AGENT"]
    if any(not os.getenv(name) for name in required_env):
        raise RuntimeError(CREDENTIALS_MISSING_MESSAGE)


def create_reddit_client() -> Any:
    # Check credentials before importing PRAW so missing credentials always
    # produce the explicit disabled message and never trigger fallback behavior.
    ensure_api_credentials()

    try:
        import praw
    except ImportError as exc:
        raise RuntimeError(
            "Missing dependency: praw. Install with `pip install -r requirements.txt`."
        ) from exc

    reddit = praw.Reddit(
        client_id=os.environ["REDDIT_CLIENT_ID"],
        client_secret=os.environ["REDDIT_CLIENT_SECRET"],
        user_agent=os.environ["REDDIT_USER_AGENT"],
        check_for_async=False,
    )
    reddit.read_only = True
    return reddit


def get_prawcore_exceptions() -> Any:
    try:
        import prawcore
    except ImportError as exc:
        raise RuntimeError("Missing dependency: prawcore. Install with `pip install -r requirements.txt`.") from exc
    return prawcore.exceptions


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


def utc_now_iso() -> str:
    return utc_now().isoformat(timespec="seconds").replace("+00:00", "Z")


def row_unique_id(row: dict[str, Any]) -> str:
    if row.get("source_type") == "post" and row.get("post_id"):
        return f"post:{row['post_id']}"
    if row.get("source_type") == "comment" and row.get("comment_id"):
        return f"comment:{row['comment_id']}"
    return ""


def clean_text(value: Any) -> str:
    if value is None:
        return ""
    return str(value).strip()


def is_removed_or_deleted(value: Any) -> bool:
    return clean_text(value).lower() in REMOVED_MARKERS


def absolute_permalink(permalink: Any) -> str:
    text = clean_text(permalink)
    if not text:
        return ""
    if text.startswith("http://") or text.startswith("https://"):
        return text
    return f"{REDDIT_BASE_URL}{text}"


def safe_int(value: Any) -> int | str:
    if value is None:
        return ""
    return int(value)


def print_dry_run_row(row: dict[str, Any]) -> None:
    preview_source = row.get("text") or row.get("title") or ""
    preview = " ".join(str(preview_source).split())[:140]
    if row["source_type"] == "post":
        item_id = row["post_id"]
    else:
        item_id = row["comment_id"]
    print(
        f"[DRY RUN] {row['source_type']} r/{row['subreddit']} id={item_id} "
        f"keyword={row['query_keyword']!r}: {preview}"
    )


def build_post_row(submission: Any, keyword: str) -> dict[str, Any] | None:
    title = clean_text(getattr(submission, "title", ""))
    selftext = clean_text(getattr(submission, "selftext", ""))

    if is_removed_or_deleted(title) or is_removed_or_deleted(selftext):
        return None
    if not title and not selftext:
        return None

    subreddit_name = clean_text(getattr(getattr(submission, "subreddit", ""), "display_name", ""))
    return {
        "source_type": "post",
        "subreddit": subreddit_name,
        "query_keyword": keyword,
        "post_id": clean_text(getattr(submission, "id", "")),
        "comment_id": "",
        "parent_id": "",
        "created_utc": safe_int(getattr(submission, "created_utc", None)),
        "score": safe_int(getattr(submission, "score", None)),
        "num_comments": safe_int(getattr(submission, "num_comments", None)),
        "title": title,
        "text": selftext,
        "permalink": absolute_permalink(getattr(submission, "permalink", "")),
        "url": clean_text(getattr(submission, "url", "")),
        "collected_at_utc": utc_now_iso(),
        "collection_mode": "api",
        "notes": "public Reddit API via read-only PRAW OAuth",
    }


def build_comment_row(comment: Any, submission: Any, keyword: str) -> dict[str, Any] | None:
    body = clean_text(getattr(comment, "body", ""))
    if is_removed_or_deleted(body) or not body:
        return None

    title = clean_text(getattr(submission, "title", ""))
    subreddit_name = clean_text(getattr(getattr(submission, "subreddit", ""), "display_name", ""))
    return {
        "source_type": "comment",
        "subreddit": subreddit_name,
        "query_keyword": keyword,
        "post_id": clean_text(getattr(submission, "id", "")),
        "comment_id": clean_text(getattr(comment, "id", "")),
        "parent_id": clean_text(getattr(comment, "parent_id", "")),
        "created_utc": safe_int(getattr(comment, "created_utc", None)),
        "score": safe_int(getattr(comment, "score", None)),
        "num_comments": safe_int(getattr(submission, "num_comments", None)),
        "title": title,
        "text": body,
        "permalink": absolute_permalink(getattr(comment, "permalink", "")),
        "url": clean_text(getattr(submission, "url", "")),
        "collected_at_utc": utc_now_iso(),
        "collection_mode": "api",
        "notes": "public Reddit API via read-only PRAW OAuth",
    }


def is_temporary_response_error(exc: Exception) -> bool:
    response = getattr(exc, "response", None)
    status_code = getattr(response, "status_code", None)
    return status_code in {429, 500, 502, 503, 504}


def retry_after_seconds(exc: Exception, fallback: float) -> float:
    response = getattr(exc, "response", None)
    headers = getattr(response, "headers", {}) or {}
    retry_after = headers.get("retry-after") or headers.get("Retry-After")
    if retry_after:
        try:
            return max(float(retry_after), fallback)
        except ValueError:
            return fallback
    return fallback


def with_retries(
    description: str,
    operation: Callable[[], Any],
    config: CollectorConfig,
    logger: logging.Logger,
) -> Any:
    prawcore_exceptions = get_prawcore_exceptions()
    delay = config.backoff_initial_seconds

    for attempt in range(1, config.retry_attempts + 1):
        try:
            return operation()
        except prawcore_exceptions.TooManyRequests as exc:
            sleep_for = retry_after_seconds(exc, delay)
            logger.warning(
                "Rate-limit pause during %s: sleeping %.1f seconds (attempt %s/%s).",
                description,
                sleep_for,
                attempt,
                config.retry_attempts,
            )
            time.sleep(sleep_for)
        except prawcore_exceptions.ResponseException as exc:
            if not is_temporary_response_error(exc) or attempt == config.retry_attempts:
                raise
            sleep_for = delay + random.uniform(0, min(1.0, delay))
            logger.warning(
                "Temporary API response error during %s: sleeping %.1f seconds (attempt %s/%s).",
                description,
                sleep_for,
                attempt,
                config.retry_attempts,
            )
            time.sleep(sleep_for)
        except (
            prawcore_exceptions.RequestException,
            prawcore_exceptions.ServerError,
        ) as exc:
            if attempt == config.retry_attempts:
                raise
            sleep_for = delay + random.uniform(0, min(1.0, delay))
            logger.warning(
                "Temporary API/network error during %s: %s. Sleeping %.1f seconds "
                "(attempt %s/%s).",
                description,
                exc,
                sleep_for,
                attempt,
                config.retry_attempts,
            )
            time.sleep(sleep_for)

        delay = min(delay * 2, config.backoff_max_seconds)

    raise RuntimeError(f"Retry loop ended unexpectedly during {description}.")


def polite_pause(config: CollectorConfig, logger: logging.Logger, reason: str) -> None:
    if config.request_delay_seconds <= 0:
        return
    logger.info("Polite pause %.1f seconds after %s.", config.request_delay_seconds, reason)
    time.sleep(config.request_delay_seconds)


def ensure_under_cap(stats: RunStats, config: CollectorConfig) -> None:
    if stats.total_collected >= config.max_total_items:
        raise CollectionCapReached(
            f"Reached max_total_items safety cap ({config.max_total_items})."
        )


def search_submissions(
    reddit: Any,
    subreddit_name: str,
    keyword: str,
    config: CollectorConfig,
    logger: logging.Logger,
) -> list[Any]:
    subreddit = reddit.subreddit(subreddit_name)

    def operation() -> list[Any]:
        return list(
            subreddit.search(
                keyword,
                sort=config.sort,
                time_filter=config.time_filter,
                limit=config.max_posts_per_query,
            )
        )

    return with_retries(f"search r/{subreddit_name} for {keyword!r}", operation, config, logger)


def load_top_comments(submission: Any, config: CollectorConfig, logger: logging.Logger) -> list[Any]:
    if config.max_comments_per_post == 0 or config.comment_depth == 0:
        return []

    submission.comment_sort = config.comment_sort

    def operation() -> list[Any]:
        # limit=0 avoids expanding "more comments" placeholders into large extra requests.
        submission.comments.replace_more(limit=0)
        return list(submission.comments)

    return with_retries(f"load comments for post {submission.id}", operation, config, logger)


def iter_comments(comment_forest: list[Any], max_depth: int) -> Any:
    try:
        from praw.models import MoreComments
    except ImportError as exc:
        raise RuntimeError("Missing dependency: praw. Install with `pip install -r requirements.txt`.") from exc

    stack = [(comment, 1) for comment in reversed(comment_forest)]
    while stack:
        comment, depth = stack.pop()
        if isinstance(comment, MoreComments):
            continue

        yield comment

        if depth >= max_depth:
            continue

        replies = list(getattr(comment, "replies", []) or [])
        for reply in reversed(replies):
            stack.append((reply, depth + 1))


def collect_comments_for_post(
    submission: Any,
    keyword: str,
    config: CollectorConfig,
    sink: CsvSink,
    stats: RunStats,
    logger: logging.Logger,
) -> int:
    comments_collected_for_post = 0
    comment_forest = load_top_comments(submission, config, logger)

    for comment in iter_comments(comment_forest, config.comment_depth):
        if comments_collected_for_post >= config.max_comments_per_post:
            break

        ensure_under_cap(stats, config)
        row = build_comment_row(comment, submission, keyword)
        if row is None:
            stats.comments_skipped += 1
            continue

        if sink.write_row(row):
            stats.comments_collected += 1
            comments_collected_for_post += 1
        else:
            stats.duplicate_rows_skipped += 1

    return comments_collected_for_post


def make_manifest_record(
    run_id: str,
    started_at_utc: str,
    config_path: Path,
    config: CollectorConfig,
    dry_run: bool,
    status: str,
    stats: RunStats | None = None,
    finished_at_utc: str | None = None,
    error: str | None = None,
) -> dict[str, Any]:
    record: dict[str, Any] = {
        "run_id": run_id,
        "status": status,
        "script_version": SCRIPT_VERSION,
        "started_at_utc": started_at_utc,
        "finished_at_utc": finished_at_utc,
        "dry_run": dry_run,
        "collection_mode": "api",
        "api_collection_performed": status == "completed" and not dry_run,
        "config_file": str(config_path),
        "output_filename": str(config.output_csv_path),
        "config": config.to_manifest_dict(),
        "stats": stats.to_dict() if stats else None,
        "error": error,
    }
    return record


def write_manifest(path: Path, record: dict[str, Any], dry_run: bool) -> None:
    if dry_run:
        return

    path.parent.mkdir(parents=True, exist_ok=True)
    manifest: dict[str, Any]
    if path.exists() and path.stat().st_size > 0:
        with path.open("r", encoding="utf-8") as file_handle:
            manifest = json.load(file_handle)
        if "runs" not in manifest:
            manifest = {"manifest_schema_version": 1, "runs": [manifest]}
    else:
        manifest = {"manifest_schema_version": 1, "runs": []}

    runs = manifest["runs"]
    for index, existing in enumerate(runs):
        if existing.get("run_id") == record["run_id"]:
            runs[index] = record
            break
    else:
        runs.append(record)

    temporary_path = path.with_suffix(path.suffix + ".tmp")
    with temporary_path.open("w", encoding="utf-8") as file_handle:
        json.dump(manifest, file_handle, indent=2, sort_keys=True)
        file_handle.write("\n")
    temporary_path.replace(path)


def collect(config_path: Path, config: CollectorConfig, dry_run: bool) -> RunStats:
    logger = setup_logger(config, dry_run)
    stats = RunStats()
    run_id = utc_now().strftime("%Y%m%dT%H%M%SZ")
    started_at = utc_now_iso()
    sink: CsvSink | None = None

    logger.info("Starting Reddit collection run %s.", run_id)
    logger.info("Output CSV: %s", config.output_csv_path)
    if dry_run:
        logger.info("Dry run enabled: no CSV, log file, or manifest will be written.")

    start_record = make_manifest_record(
        run_id=run_id,
        started_at_utc=started_at,
        config_path=config_path,
        config=config,
        dry_run=dry_run,
        status="running",
        stats=stats,
    )
    write_manifest(config.manifest_path, start_record, dry_run)

    status = "completed"
    error_message = None

    try:
        reddit = create_reddit_client()
        sink = CsvSink(config.output_csv_path, dry_run, logger)
        prawcore_exceptions = get_prawcore_exceptions()
        for subreddit_name in config.subreddits:
            for keyword in config.keywords:
                ensure_under_cap(stats, config)
                query_posts = 0
                query_comments = 0
                logger.info("Searching r/%s for keyword %r.", subreddit_name, keyword)

                try:
                    submissions = search_submissions(reddit, subreddit_name, keyword, config, logger)
                except (
                    prawcore_exceptions.Forbidden,
                    prawcore_exceptions.NotFound,
                    prawcore_exceptions.Redirect,
                ) as exc:
                    message = f"Skipping r/{subreddit_name} for {keyword!r}: {type(exc).__name__}"
                    logger.warning(message)
                    stats.errors.append(message)
                    continue
                except Exception as exc:
                    message = f"Error searching r/{subreddit_name} for {keyword!r}: {exc}"
                    logger.exception(message)
                    stats.errors.append(message)
                    continue

                for submission in submissions:
                    ensure_under_cap(stats, config)
                    row = build_post_row(submission, keyword)
                    if row is None:
                        stats.posts_skipped += 1
                        continue

                    if sink.write_row(row):
                        stats.posts_collected += 1
                        query_posts += 1
                    else:
                        stats.duplicate_rows_skipped += 1
                        continue

                    try:
                        query_comments += collect_comments_for_post(
                            submission, keyword, config, sink, stats, logger
                        )
                    except CollectionCapReached:
                        raise
                    except Exception as exc:
                        message = f"Error collecting comments for post {submission.id}: {exc}"
                        logger.exception(message)
                        stats.errors.append(message)

                    polite_pause(config, logger, f"post {submission.id}")

                stats.searches.append(
                    {
                        "subreddit": subreddit_name,
                        "keyword": keyword,
                        "posts_collected": query_posts,
                        "comments_collected": query_comments,
                    }
                )
                logger.info(
                    "Finished r/%s keyword %r: %s posts, %s comments.",
                    subreddit_name,
                    keyword,
                    query_posts,
                    query_comments,
                )
                polite_pause(config, logger, f"search r/{subreddit_name} {keyword!r}")

    except CollectionCapReached as exc:
        logger.info("%s", exc)
    except Exception as exc:
        status = "failed"
        error_message = str(exc)
        stats.errors.append(error_message)
        if isinstance(exc, RuntimeError):
            logger.error("Collection run %s failed: %s", run_id, exc)
        else:
            logger.exception("Collection run %s failed: %s", run_id, exc)
        raise
    finally:
        if sink is not None:
            sink.close()
        finished_at = utc_now_iso()
        final_record = make_manifest_record(
            run_id=run_id,
            started_at_utc=started_at,
            config_path=config_path,
            config=config,
            dry_run=dry_run,
            status=status,
            stats=stats,
            finished_at_utc=finished_at,
            error=error_message,
        )
        write_manifest(config.manifest_path, final_record, dry_run)
        logger.info(
            "Finished collection run %s with status %s: %s posts, %s comments, "
            "%s duplicate rows skipped.",
            run_id,
            status,
            stats.posts_collected,
            stats.comments_collected,
            stats.duplicate_rows_skipped,
        )
    return stats


def main() -> int:
    args = parse_args()
    config_path = Path(args.config).resolve()

    try:
        ensure_api_credentials(config_path.parent / ".env")
        config = load_config(config_path, args.max_total_items)
        collect(config_path, config, args.dry_run)
    except KeyboardInterrupt:
        print("Interrupted by user.", file=sys.stderr)
        return 130
    except Exception as exc:
        if str(exc) == CREDENTIALS_MISSING_MESSAGE:
            print(CREDENTIALS_MISSING_MESSAGE, file=sys.stderr)
        else:
            print(f"Error: {exc}", file=sys.stderr)
        return 1

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
