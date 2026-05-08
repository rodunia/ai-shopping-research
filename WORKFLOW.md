# Workflow: Reddit AI-Assisted Shopping Collection

This document describes the end-to-end workflow for this repository, from setup to scraping, quality review, and publishing updates.

## Scope and guardrails

Use this project for low-volume Reddit-only research collection.

- Source: public Reddit structured endpoints only (`.json` and RSS fallback).
- No API keys, OAuth, login, browser automation, proxies, CAPTCHA bypass, or third-party mirrors.
- If a source is blocked (`403`, `429`, CAPTCHA/login-required), do not bypass; log and continue.

## Project files you will use

- `reddit_ai_assisted_shopping_collector.py`: main no-key collector.
- `README.md`: user-facing usage and config reference.
- `requirements.txt`: runtime dependencies.
- `reddit_ai_assisted_shopping_posts.csv`: generated output (ignored by git).

Additional scripts in this repo exist for other workflows and are not needed for the no-key collector path.

## One-time setup

1. Create and activate a virtual environment.
2. Install dependencies.

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
```

## Recommended scrape sequence

Run in small stages so we can check relevance quality before broad collection.

1. Smoke test one subreddit.

```bash
python reddit_ai_assisted_shopping_collector.py --subreddit ChatGPT --limit 10 --skip-search
```

2. Small multi-subreddit batch.

```bash
python reddit_ai_assisted_shopping_collector.py --subreddit ChatGPT --subreddit OpenAI --subreddit ecommerce --limit 25 --skip-search
```

3. Broader listing pass (no phrase-search yet).

```bash
python reddit_ai_assisted_shopping_collector.py --limit 25 --skip-search
```

4. Phrase-search pass after quality looks good. This is slower.

```bash
python reddit_ai_assisted_shopping_collector.py --limit 10
```

## Important runtime behavior

- The script writes a fresh CSV each run at `reddit_ai_assisted_shopping_posts.csv` unless `--output` is changed.
- Output is not append mode.
- Dedupe is by `post_id`, then canonical Reddit post URL.
- Main output writes only core rows:
  `core_consumer_ai_assisted_shopping` and `core_ai_commerce_visibility`.
- Use `--audit-csv <path>` to write all tiers (`core`, `adjacent`, `discarded`) for review.
- Saved core posts are enriched by default from each post's public detail JSON endpoint to improve `selftext_or_snippet`.
- You can disable text enrichment with:

```bash
python reddit_ai_assisted_shopping_collector.py --no-post-text
```

## Interpreting warnings

Warnings like below are expected for blocked sources:

```text
WARNING: blocked or rate-limited HTTP 403: https://www.reddit.com/r/shopping/new.json?limit=25
```

This means the collector behaved correctly: it did not bypass restrictions and moved on.

## Quality review loop

After each batch, inspect quality before scaling.

Quick review command:

```bash
python3 - <<'PY'
import csv
rows = list(csv.DictReader(open("reddit_ai_assisted_shopping_posts.csv", encoding="utf-8")))
print("rows:", len(rows))
for i, r in enumerate(rows, 1):
    print(f"{i}. r/{r['subreddit']} | {r['relevance_type']} | {r['title']}")
    print("   AI:", r["matched_ai_assistance_keywords"])
    print("   SHOP:", r["matched_shopping_commerce_keywords"])
    print("   HIGH:", r["matched_high_intent_phrases"])
PY
```

What to look for:

- Too many generic posts matching only weak terms like `ai + product` or `ai + deals`.
- Whether `matched_high_intent_phrases` appears frequently enough for your study objective.
- Whether key subreddits are over/under represented.
- Whether audit rows are dominated by known false-positive buckets.

## Relevance tuning workflow

If quality is too loose:

1. Tighten `CONSUMER_SHOPPING_TASK_KEYWORDS` and `AI_COMMERCE_VISIBILITY_KEYWORDS` in `reddit_ai_assisted_shopping_collector.py`:
   remove or de-prioritize broad tokens (`product`, `products`, `deals`) if needed.
2. Expand `HIGH_INTENT_PHRASES` with stronger behavior phrases you care about.
3. Update `EXCLUSION_PATTERNS` with newly discovered false positives.
4. Re-run a small test batch with `--audit-csv`.
5. Compare title-level precision before broader scraping.

## Git workflow for this repo

Current branch and remote:

- Branch: `main`
- Remote: `origin` -> `https://github.com/rodunia/ai-shopping-research.git`

Normal update flow:

1. Check status.
2. Commit only code/docs/config changes.
3. Keep generated CSV out of commits (already ignored).
4. Push to `origin/main`.

Commands:

```bash
git status -sb
git add <changed-files>
git commit -m "Describe the change"
git push
```

## Reproducibility checklist

For each research run, record:

- command used (including `--limit`, `--skip-search`, and selected subreddits),
- run timestamp,
- summary counts printed by the script,
- any blocked sources shown in warnings,
- any relevance tuning changes made before the run.

This keeps the collection process auditable and repeatable.
