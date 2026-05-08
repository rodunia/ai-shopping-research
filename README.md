# Reddit AI-Assisted Shopping Collector

This is a simple command-line collector for low-volume research/monitoring of public Reddit posts about **AI-assisted shopping**.

The topic is broader than "AI shopping agents". The script looks for posts about people using ChatGPT, Claude, Gemini, Perplexity, LLMs, chatbots, AI assistants, agents, copilots, or ecommerce/retail AI features to shop, compare products, discover products, evaluate reviews, find deals, compare prices, or make purchase decisions.

## Compliance

- Reddit-only sources.
- No Reddit API keys, OAuth credentials, account login, or paid scraping services.
- No Pushshift, third-party datasets, proxies, CAPTCHA bypass, or browser automation.
- No Playwright, Selenium, Puppeteer, or headless browser.
- Uses lightweight public Reddit `.json` listing/search endpoints where accessible.
- Uses public Reddit post `.json` detail endpoints for saved posts to collect post selftext when available.
- Falls back to public Reddit RSS feeds when listing JSON is unavailable.
- Stops using blocked, rate-limited, CAPTCHA, login-required, or unusable sources and logs a warning.
- Intended only for low-volume academic/non-commercial research.

For larger-scale, commercial, or operational use, use approved Reddit data access.

## Install

Use Python 3.11 or newer.

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
```

## Run

```bash
python reddit_ai_assisted_shopping_collector.py
```

Default output:

```text
reddit_ai_assisted_shopping_posts.csv
```

The CSV is written with the built-in Python `csv` module and should open cleanly in Excel or Google Sheets.

## Useful Options

Run one subreddit with a smaller limit while testing:

```bash
python reddit_ai_assisted_shopping_collector.py --subreddit ChatGPT --limit 10
```

Use a different output path:

```bash
python reddit_ai_assisted_shopping_collector.py --output data/ai_shopping_posts.csv
```

Adjust the request delay:

```bash
python reddit_ai_assisted_shopping_collector.py --delay 6
```

Skip high-intent subreddit search queries and only collect listing feeds:

```bash
python reddit_ai_assisted_shopping_collector.py --skip-search
```

By default, saved posts are enriched from each post's public Reddit `.json`
detail endpoint so `selftext_or_snippet` contains the best available post text.
Disable that extra request per saved post with:

```bash
python reddit_ai_assisted_shopping_collector.py --no-post-text
```

## How Matching Works

A post is core relevant only when at least one of these is true:

1. It matches a `HIGH_INTENT_PHRASE`.
2. It contains at least one `STRONG_AI_TOOL_KEYWORD` and at least one `CONSUMER_SHOPPING_TASK_KEYWORD`.
3. It contains at least one `STRONG_AI_TOOL_KEYWORD` and at least one `AI_COMMERCE_VISIBILITY_KEYWORD`.

Core tiers:

1. `core_consumer_ai_assisted_shopping`: consumers using AI tools to decide what to buy, compare products, read/summarize reviews, find deals, compare prices, or make purchase decisions.
2. `core_ai_commerce_visibility`: product visibility and checkout/sales-through-AI contexts, such as products appearing in ChatGPT/Perplexity answers, AI search visibility, LLM checkout, and traffic/sales through AI answer engines.

The classifier does not mark posts core or adjacent based only on generic AI/tool mentions (for example `ChatGPT`, `Claude`, `Gemini`, `Perplexity`, `AI`, `LLM`) without shopping/commerce intent context.

## Editing Config

The default config lives near the top of `reddit_ai_assisted_shopping_collector.py`:

- `SUBREDDITS`
- `SORTS`
- `LIMIT_PER_SUBREDDIT_PER_SORT`
- `OUTPUT_CSV`
- `REQUEST_DELAY_SECONDS`
- `USER_AGENT`
- `SELF_TEXT_MAX_CHARS`
- `STRONG_AI_TOOL_KEYWORDS`
- `CONSUMER_SHOPPING_TASK_KEYWORDS`
- `AI_COMMERCE_VISIBILITY_KEYWORDS`
- `HIGH_INTENT_PHRASES`
- `EXCLUSION_PATTERNS`

Edit those lists directly for your study.

## Output Columns

```text
scraped_at_utc
platform
source_type
subreddit
sort
relevance_type
relevance_score
relevance_tier
relevance_reason
matched_ai_assistance_keywords
matched_shopping_commerce_keywords
matched_high_intent_phrases
post_id
title
author
created_utc
score
comment_count
post_url
external_url
selftext_or_snippet
```

Normalization details:

- `platform` is always `reddit`.
- `source_type` is `json` or `rss`.
- `relevance_score` is a 0-10 score.
- `relevance_tier` is one of:
  `core_consumer_ai_assisted_shopping`, `core_ai_commerce_visibility`, `adjacent_ai_commerce`, `discarded`.
- `relevance_reason` explains why a row got its tier.
- Main output CSV saves only core rows by default.
- `--audit-csv` saves all tiers (`core`, `adjacent`, `discarded`) for debugging/review.
- Matched keyword lists are semicolon-separated.
- Posts are deduplicated by `post_id` when available, otherwise by canonical Reddit post URL.
- Missing fields are empty strings.
- Snippets are whitespace-normalized and capped at 1,000 characters.

## Error Handling

The script uses request timeouts and retries transient network/server errors only once. It does not retry `403`, `429`, CAPTCHA, login-required, or other blocked responses. If public Reddit JSON fails for a subreddit listing, it tries the matching public RSS feed. If a source remains unavailable, the script logs a warning to stderr and continues with other subreddits.

At the end, it prints:

- subreddits checked;
- total raw posts seen;
- relevant posts saved;
- adjacent/discarded rows;
- duplicate posts removed;
- failed or blocked sources;
- output CSV path.
