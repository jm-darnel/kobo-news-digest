# kobo-news-digest

RSS digest for KOReader's News Downloader on a Kobo Clara BW. A GitHub Action
aggregates a handful of Harlem / NYC / Yankees feeds every four hours, extracts
full article text, writes one clean image-free HTML page per article, and
publishes a single RSS feed via GitHub Pages.

- Feed: <https://jm-darnel.github.io/kobo-news-digest/news-digest.xml>
- Article pages: `articles/<hash>.html` (hash = first 16 hex chars of SHA-256 of
  the canonical article URL; the same hash is the item guid)

## How it works

`aggregate.py`:

1. Fetches each source in `feeds.py` (RSS/Atom via feedparser, or Gothamist's
   Wagtail JSON API for the Harlem source).
2. Keeps items published in the last 7 days that are not in `seen.json`,
   capped per source and at 40 items total.
3. Extracts body text — feed-native `content:encoded` where the source ships it
   (Pinstripe Alley, MLB Trade Rumors), otherwise trafilatura on the article
   page. Falls back to a `[partial]` summary rather than emitting an empty item.
4. Writes `articles/<hash>.html`: `<p>` per paragraph, no images/scripts/styles,
   content tables linearized, emoji stripped.
5. Emits `news-digest.xml` (RSS 2.0, short CDATA summaries, stable guids) and
   validates it with lxml + feedparser before writing.
6. Updates `seen.json` (14-day retention).

The device fetches the feed and then one GET per item `<link>` — all against
GitHub Pages, never the original news sites.

## Local run

```sh
python3 -m venv .venv && .venv/bin/pip install -r requirements.txt
.venv/bin/python aggregate.py
```

## Ops notes

- Pages source must stay **Deploy from a branch → main → / (root)**. The
  "GitHub Actions" Pages source silently never updates from commits.
- `main` must stay unprotected so `github-actions[bot]` can push.
- Scheduled workflows on public repos are auto-disabled after 60 days without
  repo activity; the digest's own commits count as activity.
- No-op runs create no commit (no pointless Pages rebuilds).
- `run-summary.md` is written only when the digest changed, so it never forces
  a commit on a quiet run.
- Pages rebuilds 1-3 min after a push (occasionally longer), so a feed the bot
  just committed can lag slightly behind `main`.
- If the digest goes stale: check the workflow's disabled banner first, then
  the last run log for per-source errors. A no-op run deliberately creates no
  commit, so a long quiet stretch does not reset the 60-day auto-disable clock —
  that is a known, accepted tradeoff, not a bug.
