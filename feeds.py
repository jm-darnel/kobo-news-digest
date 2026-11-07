"""Feed source list and per-source config.

Two source kinds:
  ``rss``      -- a normal RSS/Atom feed parsed with feedparser.
  ``json_api`` -- a JSON endpoint returning article pages (Gothamist's Wagtail
                  API), used for tag-filtered sources that ship no tag feed.

Fields:
  name            display name used in logs
  url             feed or API URL
  category        free-form tag stored with the item (digest bookkeeping)
  max_items       per-source cap per run
  prefer_feed_native
                  trust the feed's own full content over page extraction when
                  the feed ships it (SB Nation / MLBTR ship full content:encoded)
  tag_filter      (json_api only) tag slug an item must carry to qualify
"""

BASE_URL = "https://jm-darnel.github.io/kobo-news-digest"

SOURCES = [
    {
        "name": "Pinstripe Alley",
        "url": "https://www.pinstripealley.com/rss/index.xml",
        "category": "yankees",
        "max_items": 6,
        "prefer_feed_native": True,
    },
    {
        "name": "MLB Trade Rumors (Yankees)",
        "url": "https://www.mlbtraderumors.com/teams/new-york-yankees/feed",
        "category": "yankees",
        "max_items": 6,
        "prefer_feed_native": True,
    },
    {
        "name": "Gothamist",
        "url": "https://gothamist.com/feed",
        "category": "nyc",
        "max_items": 8,
    },
    {
        "name": "The City",
        "url": "https://www.thecity.nyc/feed/",
        "category": "nyc",
        "max_items": 8,
    },
    {
        "name": "NY1",
        "url": "https://www.ny1.com/services/contentfeed.nyc%7Call-boroughs%7Cnews.landing.rss",
        "category": "nyc",
        "max_items": 8,
    },
    {
        # Gothamist has no Harlem tag feed and no Harlem tag page any more
        # (verified 2026-11-06: /tags/harlem/feed 404s, /tags/harlem is a
        # client-side 404, cms.prod.nypr.digital/feed/?tag=harlem ignores the
        # param, and no recent article carries a "harlem" tag). Substitute:
        # their Wagtail search API, kept strict by requiring "harlem" in the
        # headline. Yields ~1-2 items/week, which is the honest volume.
        "name": "Gothamist (Harlem)",
        "kind": "json_api",
        "url": (
            "https://api-prod.gothamist.com/api/v2/pages/"
            "?type=news.ArticlePage&search=harlem&limit=60"
            "&order=-first_published_at&fields=title,first_published_at,related_authors"
        ),
        "article_url": "https://gothamist.com/news/{slug}/",
        "title_match": "harlem",
        "category": "harlem",
        "max_items": 4,
    },
    {
        "name": "Harlem World",
        "url": "https://harlemworldmagazine.com/feed/",
        "category": "harlem",
        "max_items": 8,
    },
    {
        # Harlem's paper of record. WordPress, ships full content:encoded
        # (~3-7k chars), so trust the feed rather than extracting the page.
        "name": "Amsterdam News",
        "url": "https://amsterdamnews.com/feed/",
        "category": "harlem",
        "max_items": 6,
        "prefer_feed_native": True,
    },
]
