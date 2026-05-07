#!/usr/bin/env python3
"""Low-volume Reddit-only collector for public AI-assisted shopping posts.

This script does not use Reddit API keys, OAuth credentials, browser
automation, proxies, Pushshift, paid scraping services, or CAPTCHA bypasses.
It requests public Reddit JSON listing/search endpoints and falls back to
public Reddit RSS feeds where available.
"""

from __future__ import annotations

import argparse
import calendar
import csv
import html
import re
import sys
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any
from urllib.parse import urlencode, urlparse, urlunparse

REDDIT_BASE_URL = "https://www.reddit.com"

SUBREDDITS = [
    "ChatGPT",
    "OpenAI",
    "ClaudeAI",
    "GeminiAI",
    "Perplexity_ai",
    "singularity",
    "artificial",
    "ArtificialInteligence",
    "LocalLLaMA",
    "ecommerce",
    "shopify",
    "Entrepreneur",
    "startups",
    "SaaS",
    "marketing",
    "digital_marketing",
    "retail",
    "amazon",
    "Flipping",
    "BuyItForLife",
    "Frugal",
    "deals",
    "shopping",
    "OnlineShopping",
]

SORTS = ["new", "hot", "top"]
LIMIT_PER_SUBREDDIT_PER_SORT = 50
OUTPUT_CSV = "reddit_ai_assisted_shopping_posts.csv"
REQUEST_DELAY_SECONDS = 4
USER_AGENT = "low-volume-ai-assisted-shopping-research-script/0.1"
REQUEST_TIMEOUT_SECONDS = 20
SELF_TEXT_MAX_CHARS = 1000

AI_ASSISTANCE_KEYWORDS = [
    "ai",
    "artificial intelligence",
    "chatgpt",
    "claude",
    "gemini",
    "perplexity",
    "copilot",
    "llm",
    "large language model",
    "chatbot",
    "chat bot",
    "ai assistant",
    "virtual assistant",
    "ai agent",
    "ai agents",
    "agentic",
    "agentic ai",
    "autonomous agent",
    "browser agent",
    "web agent",
    "operator",
    "openai operator",
    "amazon rufus",
    "google shopping ai",
    "shopify ai",
]

SHOPPING_COMMERCE_KEYWORDS = [
    "shopping",
    "shop",
    "online shopping",
    "buy",
    "buying",
    "bought",
    "purchase",
    "purchasing",
    "checkout",
    "cart",
    "ecommerce",
    "e-commerce",
    "commerce",
    "retail",
    "store",
    "marketplace",
    "product",
    "products",
    "product search",
    "product discovery",
    "recommendation",
    "recommendations",
    "review",
    "reviews",
    "price comparison",
    "compare prices",
    "deal",
    "deals",
    "discount",
    "coupon",
    "affiliate",
    "merchant",
    "storefront",
    "shopify",
    "amazon",
    "walmart",
    "target",
    "etsy",
    "temu",
    "shein",
]

HIGH_INTENT_PHRASES = [
    "ai assisted shopping",
    "ai-assisted shopping",
    "shopping with ai",
    "shopping using ai",
    "using chatgpt to shop",
    "use chatgpt to shop",
    "using chatgpt for shopping",
    "chatgpt shopping",
    "claude shopping",
    "gemini shopping",
    "perplexity shopping",
    "llm shopping",
    "shopping chatbot",
    "shopping chat bot",
    "ai shopping assistant",
    "ai shopping agent",
    "shopping copilot",
    "shopping co-pilot",
    "personal shopping assistant",
    "personal ai shopper",
    "ai product recommendations",
    "ai product recommendation",
    "ai product discovery",
    "ai product search",
    "ai price comparison",
    "ai deal finder",
    "ai deal finding",
    "ai buying assistant",
    "ai purchase assistant",
    "ai purchasing assistant",
    "ai helped me buy",
    "chatgpt helped me buy",
    "chatgpt product recommendation",
    "ask chatgpt what to buy",
    "ask ai what to buy",
    "ai to compare products",
    "chatgpt to compare products",
    "ai to find deals",
    "chatgpt to find deals",
    "ai to read reviews",
    "chatgpt to read reviews",
    "amazon rufus",
    "agentic commerce",
    "agentic shopping",
]

CSV_COLUMNS = [
    "scraped_at_utc",
    "platform",
    "source_type",
    "subreddit",
    "sort",
    "relevance_type",
    "matched_ai_assistance_keywords",
    "matched_shopping_commerce_keywords",
    "matched_high_intent_phrases",
    "post_id",
    "title",
    "author",
    "created_utc",
    "score",
    "comment_count",
    "post_url",
    "external_url",
    "selftext_or_snippet",
]

TRANSIENT_STATUS_CODES = {500, 502, 503, 504}
BLOCKED_STATUS_CODES = {401, 403, 429}
BLOCKED_TEXT_PATTERNS = [
    "captcha",
    "login required",
    "login-required",
    "you must log in",
    "log in to continue",
    "blocked",
    "too many requests",
]

FAILED_OR_BLOCKED_SOURCES: list[dict[str, str]] = []


@dataclass
class Summary:
    subreddits_checked: int = 0
    total_raw_posts_seen: int = 0
    relevant_posts_before_dedupe: int = 0
    relevant_posts_saved: int = 0
    duplicate_posts_removed: int = 0


@dataclass
class HttpResult:
    response: Any | None = None
    error: str = ""


def get_config() -> dict[str, Any]:
    """Return the default in-file config as a mutable dictionary."""
    return {
        "subreddits": list(SUBREDDITS),
        "sorts": list(SORTS),
        "limit": LIMIT_PER_SUBREDDIT_PER_SORT,
        "output_csv": OUTPUT_CSV,
        "request_delay_seconds": REQUEST_DELAY_SECONDS,
        "user_agent": USER_AGENT,
        "run_search": True,
        "fetch_post_text": True,
    }


def sleep_between_requests() -> None:
    """Pause conservatively between public Reddit requests."""
    if REQUEST_DELAY_SECONDS > 0:
        time.sleep(REQUEST_DELAY_SECONDS)


def utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")


def log_warning(message: str) -> None:
    print(f"WARNING: {message}", file=sys.stderr)


def record_failed_source(url: str, reason: str) -> None:
    FAILED_OR_BLOCKED_SOURCES.append({"url": url, "reason": reason})
    log_warning(f"{reason}: {url}")


def get_requests_module() -> Any:
    try:
        import requests
    except ImportError as exc:
        raise RuntimeError("Missing dependency: requests. Install with `pip install -r requirements.txt`.") from exc
    return requests


def looks_blocked_or_login_required(text: str) -> bool:
    lowered = text.lower()
    return any(pattern in lowered for pattern in BLOCKED_TEXT_PATTERNS)


def content_type(response: Any) -> str:
    return response.headers.get("content-type", "").lower()


def is_html_like_response(response: Any) -> bool:
    response_type = content_type(response)
    stripped = response.text.lstrip().lower()
    return "text/html" in response_type or stripped.startswith("<!doctype html") or stripped.startswith("<html")


def http_get_once(url: str) -> HttpResult:
    requests = get_requests_module()
    headers = {"User-Agent": USER_AGENT, "Accept": "application/json, application/rss+xml;q=0.9,*/*;q=0.1"}
    try:
        return HttpResult(response=requests.get(url, headers=headers, timeout=REQUEST_TIMEOUT_SECONDS))
    except requests.RequestException as exc:
        return HttpResult(error=f"network error: {exc}")
    finally:
        sleep_between_requests()


def fetch_url_with_single_retry(url: str) -> Any | None:
    """Fetch a URL, retrying transient network/server failures only once."""
    first_attempt = http_get_once(url)
    response = first_attempt.response
    if response is None:
        log_warning(f"{first_attempt.error}, retrying once: {url}")
        second_attempt = http_get_once(url)
        if second_attempt.response is None:
            record_failed_source(url, second_attempt.error or first_attempt.error)
            return None
        response = second_attempt.response

    status_code = response.status_code
    if status_code in BLOCKED_STATUS_CODES:
        record_failed_source(url, f"blocked or rate-limited HTTP {status_code}")
        return None

    if status_code in TRANSIENT_STATUS_CODES:
        log_warning(f"transient HTTP {status_code}, retrying once: {url}")
        retry_attempt = http_get_once(url)
        if retry_attempt.response is None:
            record_failed_source(url, retry_attempt.error or f"transient HTTP {status_code}")
            return None
        response = retry_attempt.response
        status_code = response.status_code

    if status_code in BLOCKED_STATUS_CODES:
        record_failed_source(url, f"blocked or rate-limited HTTP {status_code}")
        return None

    if status_code >= 400:
        record_failed_source(url, f"unusable HTTP {status_code}")
        return None

    if is_html_like_response(response) and looks_blocked_or_login_required(response.text):
        record_failed_source(url, "blocked, CAPTCHA, or login-required response")
        return None

    return response


def fetch_json_url(url: str) -> Any | None:
    """Fetch a public Reddit JSON URL without credentials."""
    response = fetch_url_with_single_retry(url)
    if response is None:
        return None

    try:
        data = response.json()
    except ValueError:
        if looks_blocked_or_login_required(response.text):
            record_failed_source(url, "blocked, CAPTCHA, or login-required response")
        else:
            record_failed_source(url, "non-JSON response")
        return None

    if isinstance(data, dict) and data.get("error") in BLOCKED_STATUS_CODES:
        record_failed_source(url, f"blocked JSON error {data.get('error')}")
        return None

    return data


def fetch_rss_url(url: str) -> Any | None:
    """Fetch and parse a public Reddit RSS URL."""
    response = fetch_url_with_single_retry(url)
    if response is None:
        return None

    if is_html_like_response(response) and looks_blocked_or_login_required(response.text):
        record_failed_source(url, "blocked, CAPTCHA, or login-required response")
        return None

    try:
        import feedparser
    except ImportError as exc:
        raise RuntimeError("Missing dependency: feedparser. Install with `pip install -r requirements.txt`.") from exc

    feed = feedparser.parse(response.content)
    if getattr(feed, "bozo", False) and not getattr(feed, "entries", []):
        record_failed_source(url, "unusable RSS response")
        return None
    return feed


def fetch_subreddit_json_posts(subreddit: str, sort: str, limit: int) -> list[dict[str, str]] | None:
    url = f"{REDDIT_BASE_URL}/r/{subreddit}/{sort}.json?{urlencode({'limit': limit})}"
    data = fetch_json_url(url)
    if data is None:
        return None
    if not isinstance(data, dict):
        record_failed_source(url, "unexpected JSON listing shape")
        return None

    children = data.get("data", {}).get("children", [])
    posts: list[dict[str, str]] = []
    for child in children:
        raw_post = child.get("data", {})
        normalized = normalize_json_post(raw_post, subreddit, sort)
        if normalized:
            posts.append(normalized)
    return posts


def fetch_subreddit_rss_posts(subreddit: str, sort: str) -> list[dict[str, str]] | None:
    url = f"{REDDIT_BASE_URL}/r/{subreddit}/{sort}/.rss"
    feed = fetch_rss_url(url)
    if feed is None:
        return None

    posts: list[dict[str, str]] = []
    for entry in getattr(feed, "entries", []):
        normalized = normalize_rss_entry(entry, subreddit, sort)
        if normalized:
            posts.append(normalized)
    return posts


def search_subreddit_json(subreddit: str, query: str, limit: int) -> list[dict[str, str]] | None:
    params = urlencode({"q": query, "restrict_sr": "1", "sort": "new", "limit": limit})
    url = f"{REDDIT_BASE_URL}/r/{subreddit}/search.json?{params}"
    data = fetch_json_url(url)
    if data is None:
        return None
    if not isinstance(data, dict):
        record_failed_source(url, "unexpected JSON search shape")
        return None

    children = data.get("data", {}).get("children", [])
    posts: list[dict[str, str]] = []
    for child in children:
        raw_post = child.get("data", {})
        normalized = normalize_json_post(raw_post, subreddit, "new")
        if normalized:
            posts.append(normalized)
    return posts


def clean_text(value: Any, max_length: int | None = None) -> str:
    if value is None:
        text = ""
    else:
        text = html.unescape(str(value))
    text = re.sub(r"<[^>]+>", " ", text)
    text = re.sub(r"\s+", " ", text).strip()
    if max_length is not None and len(text) > max_length:
        return text[:max_length].rstrip()
    return text


def clean_number(value: Any) -> str:
    if value is None:
        return ""
    return str(value)


def absolute_reddit_url(permalink: Any) -> str:
    text = clean_text(permalink)
    if not text:
        return ""
    if text.startswith("http://") or text.startswith("https://"):
        return canonicalize_url(text)
    if text.startswith("/"):
        return canonicalize_url(f"{REDDIT_BASE_URL}{text}")
    return canonicalize_url(f"{REDDIT_BASE_URL}/{text.lstrip('/')}")


def post_detail_json_url(post_url: str) -> str:
    """Build a public Reddit JSON detail URL for a post."""
    canonical_url = canonicalize_url(post_url)
    if not canonical_url:
        return ""
    return f"{canonical_url}.json?{urlencode({'limit': 1})}"


def canonicalize_url(url: str) -> str:
    parsed = urlparse(clean_text(url))
    if not parsed.scheme or not parsed.netloc:
        return clean_text(url)
    path = parsed.path.rstrip("/") or "/"
    return urlunparse((parsed.scheme.lower(), parsed.netloc.lower(), path, "", "", ""))


def extract_post_id_from_url(url: str) -> str:
    match = re.search(r"/comments/([^/]+)", url)
    if match:
        return match.group(1)
    return ""


def timestamp_from_struct_time(value: Any) -> str:
    if not value:
        return ""
    try:
        return str(calendar.timegm(value))
    except (TypeError, ValueError):
        return ""


def extract_selftext_from_detail_json(data: Any) -> str:
    """Extract submission selftext from a /comments/{post}.json response."""
    if not isinstance(data, list) or not data:
        return ""
    first_listing = data[0]
    if not isinstance(first_listing, dict):
        return ""
    children = first_listing.get("data", {}).get("children", [])
    if not children:
        return ""
    raw_post = children[0].get("data", {})
    if not isinstance(raw_post, dict):
        return ""
    selftext = clean_text(raw_post.get("selftext"), max_length=SELF_TEXT_MAX_CHARS)
    if selftext.lower() in {"[deleted]", "[removed]"}:
        return ""
    return selftext


def normalize_json_post(raw_post: dict[str, Any], subreddit: str, sort: str) -> dict[str, str] | None:
    title = clean_text(raw_post.get("title"))
    selftext = clean_text(raw_post.get("selftext"), max_length=SELF_TEXT_MAX_CHARS)
    if not title or title.lower() in {"[deleted]", "[removed]"}:
        return None
    if selftext.lower() in {"[deleted]", "[removed]"}:
        selftext = ""

    post_url = absolute_reddit_url(raw_post.get("permalink"))
    external_url = clean_text(raw_post.get("url"))
    if raw_post.get("is_self") or canonicalize_url(external_url) == post_url:
        external_url = ""

    return {
        "scraped_at_utc": utc_now_iso(),
        "platform": "reddit",
        "source_type": "json",
        "subreddit": clean_text(raw_post.get("subreddit") or subreddit),
        "sort": sort,
        "relevance_type": "",
        "matched_ai_assistance_keywords": "",
        "matched_shopping_commerce_keywords": "",
        "matched_high_intent_phrases": "",
        "post_id": clean_text(raw_post.get("id")),
        "title": title,
        "author": clean_text(raw_post.get("author")),
        "created_utc": clean_number(raw_post.get("created_utc")),
        "score": clean_number(raw_post.get("score")),
        "comment_count": clean_number(raw_post.get("num_comments")),
        "post_url": post_url,
        "external_url": external_url,
        "selftext_or_snippet": selftext,
    }


def normalize_rss_entry(entry: Any, subreddit: str, sort: str) -> dict[str, str] | None:
    title = clean_text(getattr(entry, "title", ""))
    snippet = clean_text(getattr(entry, "summary", ""), max_length=SELF_TEXT_MAX_CHARS)
    if not title or title.lower() in {"[deleted]", "[removed]"}:
        return None
    if snippet.lower() in {"[deleted]", "[removed]"}:
        snippet = ""

    post_url = canonicalize_url(clean_text(getattr(entry, "link", "")))
    post_id = clean_text(getattr(entry, "id", "")) or extract_post_id_from_url(post_url)
    if post_id.startswith("t3_"):
        post_id = post_id[3:]

    return {
        "scraped_at_utc": utc_now_iso(),
        "platform": "reddit",
        "source_type": "rss",
        "subreddit": subreddit,
        "sort": sort,
        "relevance_type": "",
        "matched_ai_assistance_keywords": "",
        "matched_shopping_commerce_keywords": "",
        "matched_high_intent_phrases": "",
        "post_id": post_id,
        "title": title,
        "author": clean_text(getattr(entry, "author", "")),
        "created_utc": timestamp_from_struct_time(getattr(entry, "published_parsed", None)),
        "score": "",
        "comment_count": "",
        "post_url": post_url,
        "external_url": "",
        "selftext_or_snippet": snippet,
    }


def keyword_matches(text: str, keyword: str) -> bool:
    normalized_text = clean_text(text).lower()
    normalized_keyword = clean_text(keyword).lower()
    if not normalized_text or not normalized_keyword:
        return False

    escaped = re.escape(normalized_keyword)
    escaped = re.sub(r"\\\s+", r"\\s+", escaped)
    prefix = r"(?<![a-z0-9])" if normalized_keyword[0].isalnum() else ""
    suffix = r"(?![a-z0-9])" if normalized_keyword[-1].isalnum() else ""
    pattern = f"{prefix}{escaped}{suffix}"
    return re.search(pattern, normalized_text, flags=re.IGNORECASE) is not None


def find_keyword_matches(text: str) -> dict[str, list[str]]:
    return {
        "ai_assistance": [
            keyword for keyword in AI_ASSISTANCE_KEYWORDS if keyword_matches(text, keyword)
        ],
        "shopping_commerce": [
            keyword for keyword in SHOPPING_COMMERCE_KEYWORDS if keyword_matches(text, keyword)
        ],
        "high_intent": [phrase for phrase in HIGH_INTENT_PHRASES if keyword_matches(text, phrase)],
    }


def classify_relevance(post: dict[str, str]) -> dict[str, str] | None:
    searchable_text = " ".join(
        [
            post.get("title", ""),
            post.get("selftext_or_snippet", ""),
            post.get("post_url", ""),
            post.get("external_url", ""),
        ]
    )
    matches = find_keyword_matches(searchable_text)
    has_high_intent = bool(matches["high_intent"])
    has_ai_plus_shopping = bool(matches["ai_assistance"] and matches["shopping_commerce"])

    if not has_high_intent and not has_ai_plus_shopping:
        return None

    if has_high_intent and has_ai_plus_shopping:
        relevance_type = "high_intent_phrase_and_ai_plus_shopping"
    elif has_high_intent:
        relevance_type = "high_intent_phrase"
    else:
        relevance_type = "ai_plus_shopping"

    classified = dict(post)
    classified["relevance_type"] = relevance_type
    classified["matched_ai_assistance_keywords"] = "; ".join(matches["ai_assistance"])
    classified["matched_shopping_commerce_keywords"] = "; ".join(matches["shopping_commerce"])
    classified["matched_high_intent_phrases"] = "; ".join(matches["high_intent"])
    return classified


def deduplicate_posts(posts: list[dict[str, str]]) -> list[dict[str, str]]:
    seen: set[str] = set()
    deduplicated: list[dict[str, str]] = []
    for post in posts:
        post_id = post.get("post_id", "")
        key = f"id:{post_id}" if post_id else f"url:{canonicalize_url(post.get('post_url', ''))}"
        if not key or key in seen:
            continue
        seen.add(key)
        deduplicated.append(post)
    return deduplicated


def fetch_post_text(post: dict[str, str]) -> str:
    """Fetch a saved post's public JSON detail and return selftext if present."""
    detail_url = post_detail_json_url(post.get("post_url", ""))
    if not detail_url:
        return ""
    data = fetch_json_url(detail_url)
    if data is None:
        return ""
    return extract_selftext_from_detail_json(data)


def enrich_posts_with_detail_text(posts: list[dict[str, str]]) -> list[dict[str, str]]:
    """Improve selftext_or_snippet for saved posts using public post JSON."""
    enriched_posts: list[dict[str, str]] = []
    for post in posts:
        enriched = dict(post)
        detail_text = fetch_post_text(enriched)
        if detail_text:
            enriched["selftext_or_snippet"] = detail_text
        enriched_posts.append(enriched)
    return enriched_posts


def write_csv(posts: list[dict[str, str]], output_path: str) -> None:
    with open(output_path, "w", encoding="utf-8", newline="") as file_handle:
        writer = csv.DictWriter(file_handle, fieldnames=CSV_COLUMNS, extrasaction="ignore")
        writer.writeheader()
        for post in posts:
            writer.writerow({column: post.get(column, "") for column in CSV_COLUMNS})


def unique_failed_sources() -> list[dict[str, str]]:
    seen: set[tuple[str, str]] = set()
    unique: list[dict[str, str]] = []
    for item in FAILED_OR_BLOCKED_SOURCES:
        key = (item["url"], item["reason"])
        if key in seen:
            continue
        seen.add(key)
        unique.append(item)
    return unique


def collect_posts(config: dict[str, Any]) -> tuple[list[dict[str, str]], Summary]:
    summary = Summary(subreddits_checked=len(config["subreddits"]))
    relevant_posts: list[dict[str, str]] = []

    for subreddit in config["subreddits"]:
        subreddit_json_failed = False
        for sort in config["sorts"]:
            json_posts = fetch_subreddit_json_posts(subreddit, sort, config["limit"])
            if json_posts is None:
                subreddit_json_failed = True
                rss_posts = fetch_subreddit_rss_posts(subreddit, sort)
                posts = rss_posts or []
            else:
                posts = json_posts

            summary.total_raw_posts_seen += len(posts)
            for post in posts:
                classified = classify_relevance(post)
                if classified:
                    relevant_posts.append(classified)

        if config["run_search"] and not subreddit_json_failed:
            for query in HIGH_INTENT_PHRASES:
                search_posts = search_subreddit_json(subreddit, query, config["limit"])
                if search_posts is None:
                    continue
                summary.total_raw_posts_seen += len(search_posts)
                for post in search_posts:
                    classified = classify_relevance(post)
                    if classified:
                        relevant_posts.append(classified)
        elif config["run_search"] and subreddit_json_failed:
            log_warning(f"skipping JSON search queries for r/{subreddit} after JSON listing failure")

    summary.relevant_posts_before_dedupe = len(relevant_posts)
    deduplicated_posts = deduplicate_posts(relevant_posts)
    if config.get("fetch_post_text", True):
        deduplicated_posts = enrich_posts_with_detail_text(deduplicated_posts)
    summary.relevant_posts_saved = len(deduplicated_posts)
    summary.duplicate_posts_removed = len(relevant_posts) - len(deduplicated_posts)
    return deduplicated_posts, summary


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Collect public Reddit posts about AI-assisted shopping via JSON/RSS only."
    )
    parser.add_argument("--output", default=OUTPUT_CSV, help=f"Output CSV path. Default: {OUTPUT_CSV}")
    parser.add_argument(
        "--limit",
        type=int,
        default=LIMIT_PER_SUBREDDIT_PER_SORT,
        help=f"Limit per subreddit per listing/search request. Default: {LIMIT_PER_SUBREDDIT_PER_SORT}",
    )
    parser.add_argument(
        "--delay",
        type=float,
        default=REQUEST_DELAY_SECONDS,
        help=f"Seconds to wait between requests. Default: {REQUEST_DELAY_SECONDS}",
    )
    parser.add_argument(
        "--skip-search",
        action="store_true",
        help="Only fetch subreddit listing JSON/RSS; skip high-intent subreddit search queries.",
    )
    parser.add_argument(
        "--no-post-text",
        action="store_true",
        help="Do not fetch per-post JSON detail to enrich selftext_or_snippet.",
    )
    parser.add_argument(
        "--subreddit",
        action="append",
        help="Restrict run to one subreddit. Can be supplied more than once.",
    )
    return parser.parse_args()


def print_summary(summary: Summary, output_path: str) -> None:
    failed_sources = unique_failed_sources()
    print("\nSummary")
    print(f"- subreddits checked: {summary.subreddits_checked}")
    print(f"- total raw posts seen: {summary.total_raw_posts_seen}")
    print(f"- relevant posts saved: {summary.relevant_posts_saved}")
    print(f"- duplicate posts removed: {summary.duplicate_posts_removed}")
    print(f"- failed or blocked sources: {len(failed_sources)}")
    if failed_sources:
        for source in failed_sources[:20]:
            print(f"  - {source['reason']}: {source['url']}")
        if len(failed_sources) > 20:
            print(f"  - ... {len(failed_sources) - 20} more")
    print(f"- output CSV path: {output_path}")


def main() -> int:
    global REQUEST_DELAY_SECONDS

    args = parse_args()
    config = get_config()
    config["output_csv"] = args.output
    config["limit"] = args.limit
    config["request_delay_seconds"] = args.delay
    config["run_search"] = not args.skip_search
    config["fetch_post_text"] = not args.no_post_text
    if args.subreddit:
        config["subreddits"] = args.subreddit

    REQUEST_DELAY_SECONDS = float(config["request_delay_seconds"])

    try:
        posts, summary = collect_posts(config)
        write_csv(posts, config["output_csv"])
        print_summary(summary, config["output_csv"])
    except KeyboardInterrupt:
        print("Interrupted by user.", file=sys.stderr)
        return 130
    except Exception as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 1

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
