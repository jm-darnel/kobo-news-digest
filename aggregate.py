#!/usr/bin/env python3
"""Aggregate upstream feeds into news-digest.xml + articles/<hash>.html.

Pipeline: fetch feeds -> pick fresh, unseen items -> extract full text ->
write one clean image-free HTML page per article -> update seen.json ->
emit RSS 2.0 with short CDATA summaries.

Design constraints live in the plan (vault: eReader News Digest v2) and in the
kobo-news-digest skill. The device (KOReader News Downloader) GETs the feed and
then one GET per <link>; every <link> must be a Pages-hosted article page.
"""

from __future__ import annotations

import hashlib
import html
import json
import re
import sys
import time
from datetime import datetime, timedelta, timezone
from email.utils import format_datetime
from pathlib import Path
from urllib.parse import urlsplit, urlunsplit

import feedparser
import requests
import trafilatura
from lxml import etree, html as lxml_html

import feeds

ROOT = Path(__file__).resolve().parent
ARTICLES = ROOT / "articles"
SEEN_PATH = ROOT / "seen.json"
DIGEST_PATH = ROOT / "digest.json"
XML_PATH = ROOT / "news-digest.xml"
SUMMARY_PATH = ROOT / "run-summary.md"

BASE_URL = feeds.BASE_URL
UA = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
)
TIMEOUT = 15
FRESH_DAYS = 7
SEEN_KEEP_DAYS = 14
TOTAL_CAP = 40
MIN_EXTRACT_CHARS = 200
TRACKING_PARAMS = re.compile(r"^(utm_.*|fbclid|gclid|ref|s_cid|mc_cid|mc_eid)$", re.I)
EMOJI = re.compile(
    "[" "\U0001F000-\U0001FAFF" "\U00002600-\U000027BF" "\U0001F1E6-\U0001F1FF"
    "\U0000FE00-\U0000FE0F" "\U00002B00-\U00002BFF" "\U0001F900-\U0001F9FF" "]",
    flags=re.UNICODE,
)
BLOCK_TAGS = {"p", "h1", "h2", "h3", "h4", "h5", "h6", "li", "blockquote", "pre"}
DROP_TAGS = ("script", "style", "iframe", "img", "picture", "svg", "figure",
             "video", "audio", "noscript", "form", "button")


# --------------------------------------------------------------------------- #
# helpers
# --------------------------------------------------------------------------- #
def log(msg: str) -> None:
    print(msg, flush=True)


def session() -> requests.Session:
    s = requests.Session()
    s.headers.update({"User-Agent": UA, "Accept-Language": "en-US,en;q=0.8"})
    return s


def get(sess: requests.Session, url: str) -> str | None:
    """GET with one retry. Returns text or None (logged, never fatal)."""
    for attempt in (1, 2):
        try:
            r = sess.get(url, timeout=TIMEOUT)
            if r.status_code == 200 and r.text:
                if "charset" not in r.headers.get("Content-Type", "").lower():
                    r.encoding = r.apparent_encoding or "utf-8"
                return r.text
            log(f"    HTTP {r.status_code} {url}")
        except requests.RequestException as exc:
            log(f"    fetch error ({attempt}/2) {url}: {exc.__class__.__name__}")
        time.sleep(1.5)
    return None


def canonical(url: str) -> str:
    parts = urlsplit(url.strip())
    query = "&".join(
        p for p in parts.query.split("&")
        if p and not TRACKING_PARAMS.match(p.split("=")[0])
    )
    path = parts.path.rstrip("/") or "/"
    return urlunsplit((parts.scheme.lower(), parts.netloc.lower(), path, query, ""))


def item_key(url: str, guid: str | None = None) -> str:
    basis = canonical(url) if url else (guid or "")
    return hashlib.sha256(basis.encode("utf-8")).hexdigest()[:16]


SPONSORED = re.compile(r"^\s*(sponsored|advertisement|partner\s+content|paid\s+post)\b", re.I)


def is_sponsored(title: str) -> bool:
    return bool(SPONSORED.match(title))


def strip_emoji(text: str) -> str:
    return EMOJI.sub("", text)


TAG = re.compile(r"</?[a-zA-Z][^>]*(?:>|$)")  # also eats a truncated tag at EOS
# WordPress feed furniture appended inside the summary/body text itself.
TAIL = re.compile(
    r"\s*(The post\s+.{0,140}?\s+appeared first on\s+[^.]{0,80}\.|"
    r"Continue reading\s+.{0,140}?(…|\.\.\.|\.)$|"
    r"Read more:?\s*https?://\S+\s*$)",
    re.I | re.S,
)
CONTROL = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f\ufffe\uffff]")


def clean_text(text: str) -> str:
    if not text:
        return ""
    text = html.unescape(text) if "&" in text else text
    text = TAG.sub(" ", text)          # feed summaries often carry raw/truncated <a>
    text = TAIL.sub("", text)          # "The post … appeared first on …"
    text = CONTROL.sub("", text)       # lxml refuses XML-incompatible control chars
    text = strip_emoji(text).replace("\u00a0", " ")
    text = re.sub(r"\[\s*more\s*[›»]?\s*\]", " ", text, flags=re.I)
    return re.sub(r"\s+", " ", text).strip()  # collapse newlines too: KOReader flattens them


# --------------------------------------------------------------------------- #
# HTML -> paragraphs
# --------------------------------------------------------------------------- #
# Anchored promo/merch patterns: only ever drop a paragraph that STARTS with
# these, so real prose mentioning "subscribe" survives.
BOILERPLATE_START = re.compile(
    r"""^(
        sign\s*up | subscribe | newsletter | become\s+an?\s+[^.]{0,30}insider |
        follow\s+us | share\s+this | advertisement | shop\s+now |
        add\s+to\s+cart | price\s*range | photo\s*credit |
        best\s+selling\s+products | from\s+history\s+to\s+today |
        this\s+(story|article)\s+was\s+originally\s+published\s+by |
        the\s+#1\s+source\s+in\s+the\s+world
    )""",
    re.I | re.X,
)
MERCH = re.compile(r"(price range|add to cart|shop now|\$\s?\d+\.\d{2}\s*$)", re.I)

# Mid-paragraph CTAs: the anchor above misses "… Donate to THE CITY today."
CTA = re.compile(
    r"""(donate\s+to\s+[^.]{0,40}today | sign\s+up\s+for\s+(their|our)\s+newsletter |
        subscribe\s+to\s+(our|the)\s+newsletter | support\s+our\s+journalism |
        become\s+a\s+member\s+today)""",
    re.I | re.X,
)

# Widget/site chrome that extraction sometimes promotes to its own paragraph.
WIDGET = {
    "how to listen", "get notifications", "listen to this article",
    "read more", "related", "related stories", "advertisement", "share this",
    "share this article", "comments", "leave a comment", "sign up",
}


def is_boilerplate(text: str) -> bool:
    lowered = text.lower().strip(" .:»›")
    if lowered in WIDGET:
        return True
    if BOILERPLATE_START.match(text):
        return True
    if len(text) < 400 and CTA.search(text):
        return True
    return len(text) < 140 and bool(MERCH.search(text))


def table_lines(tbl) -> list[str]:
    """Linearize a content table into readable lines (MLBTR rumor tables)."""
    lines = []
    for row in tbl.iter("tr"):
        cells = [clean_text(" ".join(c.itertext())) for c in row.iter("td", "th")]
        cells = [c for c in cells if c]
        if cells:
            lines.append(" — ".join(cells))
    return [ln for ln in lines if ln]


def html_to_paragraphs(raw: str) -> list[str]:
    """Parse an HTML fragment into a flat list of paragraph strings."""
    if not raw or not raw.strip():
        return []
    # Feed content is sometimes escaped twice ("&lt;p&gt;...").
    if "<" not in raw and "&lt;" in raw:
        raw = html.unescape(raw)
    try:
        root = lxml_html.fragment_fromstring(raw, create_parent="div")
    except (etree.ParserError, ValueError):
        return [clean_text(raw)] if clean_text(raw) else []

    for tag in DROP_TAGS:
        for el in root.iter(tag):
            el.getparent().remove(el)

    paras: list[str] = []
    # Linearize top-level tables only; a nested table is already covered by its
    # parent's rows (emitting both would duplicate the text).
    for tbl in root.iter("table"):
        if not any(isinstance(a.tag, str) and a.tag.lower() == "table"
                   for a in tbl.iterancestors()):
            paras.extend(table_lines(tbl))

    for el in root.iter():
        if not isinstance(el.tag, str):
            continue
        tag = el.tag.lower()
        if tag == "table":
            continue
        if any(isinstance(a.tag, str) and a.tag.lower() == "table"
               for a in el.iterancestors()):
            continue
        if tag in BLOCK_TAGS:
            # Skip blocks nested inside another block we already captured.
            if any(isinstance(p.tag, str) and p.tag.lower() in BLOCK_TAGS
                   for p in el.iterancestors()):
                continue
            text = clean_text(" ".join(el.itertext()))
            if tag == "li" and text and len(text) > 2:
                text = "• " + text
            if text:
                paras.append(text)

    if not paras:  # single-line content with no block markup
        text = clean_text(" ".join(root.itertext()))
        if text:
            paras = [text]
    return [p for p in paras if not is_boilerplate(p)]


def text_to_paragraphs(text: str) -> list[str]:
    text = strip_emoji(text or "").replace("\r", "")
    blocks = [clean_text(b) for b in re.split(r"\n\s*\n|\n", text)]
    return [b for b in blocks if b and not is_boilerplate(b)]


def trafilatura_paragraphs(page_html: str) -> list[str]:
    """Extract the article body as structured HTML, then flatten to paragraphs."""
    body = trafilatura.extract(
        page_html,
        output_format="html",
        include_comments=False,
        include_tables=True,
        favor_recall=False,
        url=None,
    )
    if body:
        paras = html_to_paragraphs(body)
        if paras:
            return paras
    txt = trafilatura.extract(page_html, include_comments=False,
                              include_tables=True, favor_recall=True)
    return text_to_paragraphs(txt) if txt else []


def summary_from(paras: list[str], feed_summary: str = "", limit: int = 300) -> str:
    base = clean_text(feed_summary) if feed_summary else ""
    if is_boilerplate(base):
        base = ""
    if len(base) < 40 and paras:
        base = " ".join(paras[:2])
    base = strip_emoji(base)
    # 1-3 sentences max, then length cap.
    sentences = re.split(r"(?<=[.!?])\s+", base)
    if len(sentences) > 3:
        base = " ".join(sentences[:3]).rstrip() + "…"
    if len(base) > limit:
        cut = base[:limit].rsplit(" ", 1)[0]
        base = cut.rstrip(",.;: ") + "…"
    return base


# --------------------------------------------------------------------------- #
# per-source candidate gathering
# --------------------------------------------------------------------------- #
def parse_date(entry) -> datetime | None:
    for field in ("published_parsed", "updated_parsed"):
        val = entry.get(field)
        if val:
            try:
                return datetime(*val[:6], tzinfo=timezone.utc)
            except (TypeError, ValueError):
                pass
    return None


def rss_candidates(src: dict, sess: requests.Session) -> list[dict]:
    raw = get(sess, src["url"])
    if raw is None:
        return []
    parsed = feedparser.parse(raw)
    if parsed.bozo and not parsed.entries:
        log(f"    parse error: {parsed.bozo_exception}")
        return []
    out = []
    for entry in parsed.entries:
        link = (entry.get("link") or "").strip()
        if not link or is_sponsored(entry.get("title") or ""):
            continue
        feed_content = ""
        for block in entry.get("content") or []:
            if block.get("value"):
                feed_content = block["value"]
                break
        if not feed_content:
            feed_content = entry.get("content_encoded") or ""
        if not feed_content:
            # Some feeds ship summaries only; RSS <description> is the fallback.
            feed_content = ""
        out.append({
            "source": src["name"],
            "category": src.get("category", ""),
            "title": clean_text(entry.get("title") or "Untitled"),
            "author": clean_text(entry.get("author") or "").removeprefix("By ").strip(),
            "link": link,
            "published": parse_date(entry) or datetime.now(timezone.utc),
            "feed_content": feed_content,
            "feed_summary": clean_text(entry.get("summary") or ""),
            "prefer_feed_native": bool(src.get("prefer_feed_native")),
            "max_items": int(src.get("max_items", 5)),
        })
    return out


def json_candidates(src: dict, sess: requests.Session) -> list[dict]:
    raw = get(sess, src["url"])
    if raw is None:
        return []
    try:
        payload = json.loads(raw)
    except json.JSONDecodeError:
        log("    JSON parse error")
        return []
    out = []
    tag = src.get("tag_filter")
    title_match = (src.get("title_match") or "").lower()
    for page in payload.get("items", []):
        meta = page.get("meta", {}) or {}
        if tag:
            slugs = {t.get("slug") for t in page.get("tags", []) or []}
            if tag not in slugs:
                continue
        if title_match and title_match not in (page.get("title") or "").lower():
            continue
        slug = meta.get("slug")
        if not slug or is_sponsored(page.get("title") or ""):
            continue
        link = src["article_url"].format(slug=slug)
        published = datetime.now(timezone.utc)
        stamp = meta.get("first_published_at")
        if stamp:
            try:
                parsed_stamp = datetime.fromisoformat(stamp)
                published = (parsed_stamp if parsed_stamp.tzinfo
                             else parsed_stamp.replace(tzinfo=timezone.utc))
            except ValueError:
                log(f"    unparseable date on {page.get('title', '')[:50]}: {stamp}")
        authors = page.get("related_authors") or []
        author = ""
        if authors and isinstance(authors[0], dict):
            first = authors[0]
            author = " ".join(
                p for p in (first.get("first_name"), first.get("last_name")) if p
            ).strip()
        out.append({
            "source": src["name"],
            "category": src.get("category", ""),
            "title": clean_text(page.get("title") or "Untitled"),
            "author": clean_text(author),
            "link": link,
            "published": published,
            "feed_content": "",
            "feed_summary": "",
            "prefer_feed_native": False,
            "max_items": int(src.get("max_items", 5)),
        })
    return out


# --------------------------------------------------------------------------- #
# extraction
# --------------------------------------------------------------------------- #
def extract(item: dict, sess: requests.Session) -> tuple[list[str], str]:
    """Return (paragraphs, mode). mode is feed-native | extracted | partial."""
    native = html_to_paragraphs(item["feed_content"]) if item["feed_content"] else []
    native_words = sum(len(p.split()) for p in native)

    if item["prefer_feed_native"] and native_words >= 60:
        return native, "feed-native"

    page = get(sess, item["link"])
    extracted: list[str] = []
    if page:
        try:
            extracted = trafilatura_paragraphs(page)
        except Exception as exc:  # extraction must never kill the run
            log(f"    extract error {item['link']}: {exc.__class__.__name__}")
    words = sum(len(p.split()) for p in extracted)

    if native_words >= 60 and (words < native_words * 0.6 or words < 80):
        return native, "feed-native"
    if words >= 40 or len("".join(extracted)) >= MIN_EXTRACT_CHARS:
        return extracted, "extracted"
    if native:
        return native, "feed-native"

    summary = summary_from([], item.get("feed_summary", ""), limit=1200)
    if not summary:
        summary = item["title"]
    return [f"[partial — read online: {item['link']}]", summary], "partial"


def normalize_author(author: str, source: str) -> str:
    """Title-case shouty CAPS bylines; drop the author when it is the outlet."""
    author = (author or "").strip()
    if author.isupper() and len(author) > 3:
        author = author.title()
    if author.lower().replace(" the ", " ") == (source or "").lower():
        return ""
    if author.lower() in {source.lower(), source.lower() + " magazine"}:
        return ""
    return author


def byline(item: dict) -> str:
    """Byline line: By {author} · {publication} · November 6, 2026. Author optional."""
    published = item.get("published")
    date_text = f"{published:%B} {published.day}, {published.year}" if published else ""
    source = (item.get("source") or "").strip()
    author = normalize_author(item.get("author", ""), source)
    head = f"By {author}" if author else source
    parts = [p for p in (head, source if author else "", date_text) if p]
    return " · ".join(parts)


def render_article(item: dict, paras: list[str], mode: str) -> str:
    title = html.escape(item["title"])
    body = []
    for p in paras:
        body.append(f"    <p>{html.escape(p)}</p>")
    attribution = (
        f'    <p><em>{html.escape(item["source"])} · original: '
        f'{html.escape(item["link"])}</em></p>'
    )
    return (
        "<!DOCTYPE html>\n"
        '<html lang="en">\n<head>\n<meta charset="utf-8">\n'
        '<meta name="viewport" content="width=device-width, initial-scale=1">\n'
        f"<title>{title}</title>\n</head>\n<body>\n"
        f"  <h1>{title}</h1>\n"
        f"  <p><em>{html.escape(byline(item))}</em></p>\n"
        + "\n".join(body)
        + "\n"
        + attribution
        + "\n</body>\n</html>\n"
    )


# --------------------------------------------------------------------------- #
# XML
# --------------------------------------------------------------------------- #
def cdata(text: str) -> str:
    """Strip a literal ]]> so it can never terminate the section early.

    lxml's CDATA serialization already splits on "]]>" correctly (verified by
    round-trip test), so the text is passed through untouched. Pre-escaping it
    here — the obvious-looking fix — double-escapes and corrupts the summary.
    """
    return text


def build_xml(items: list[dict]) -> bytes:
    now = datetime.now(timezone.utc)
    rss = etree.Element("rss", version="2.0")
    channel = etree.SubElement(rss, "channel")
    etree.SubElement(channel, "title").text = "News Digest"
    etree.SubElement(channel, "link").text = BASE_URL
    etree.SubElement(channel, "description").text = (
        "Harlem, NYC and Yankees news for offline reading on KOReader."
    )
    etree.SubElement(channel, "language").text = "en-us"
    etree.SubElement(channel, "ttl").text = "60"
    etree.SubElement(channel, "lastBuildDate").text = format_datetime(now)

    for it in items:
        node = etree.SubElement(channel, "item")
        etree.SubElement(node, "title").text = it["title"]
        etree.SubElement(node, "link").text = it["page_url"]
        guid = etree.SubElement(node, "guid", isPermaLink="true")
        guid.text = it["page_url"]
        etree.SubElement(node, "pubDate").text = format_datetime(it["published"])
        etree.SubElement(node, "category").text = it["category"] or "news"
        desc = etree.SubElement(node, "description")
        desc.text = etree.CDATA(cdata(it["summary"]))

    etree.indent(rss, space="  ")
    body = etree.tostring(rss, encoding="utf-8", xml_declaration=True)
    return body + b"\n"


def validate(xml_bytes: bytes) -> None:
    root = etree.fromstring(xml_bytes)  # raises on malformed XML
    items = root.findall("./channel/item")
    if not items:
        raise ValueError("feed has no items")
    for it in items:
        for field in ("title", "link", "guid", "pubDate", "description"):
            if it.find(field) is None:
                raise ValueError(f"item missing {field}")
    parsed = feedparser.parse(xml_bytes)
    if parsed.bozo:
        raise ValueError(f"feedparser rejects feed: {parsed.bozo_exception}")


# --------------------------------------------------------------------------- #
# state
# --------------------------------------------------------------------------- #
def load_seen() -> dict[str, str]:
    if not SEEN_PATH.exists():
        return {}
    try:
        seen = json.loads(SEEN_PATH.read_text()).get("seen", {})
    except (json.JSONDecodeError, AttributeError, TypeError):
        log("seen.json unreadable — starting fresh")
        return {}
    if not isinstance(seen, dict):
        log("seen.json malformed — starting fresh")
        return {}
    return {k: v for k, v in seen.items() if isinstance(k, str) and isinstance(v, str)}


def load_digest() -> list[dict]:
    """Current digest window: items with a live article page and a date."""
    if not DIGEST_PATH.exists():
        return []
    try:
        payload = json.loads(DIGEST_PATH.read_text())
        raw_items = payload.get("items", [])
        if not isinstance(raw_items, list):
            raise ValueError("items is not a list")
    except (json.JSONDecodeError, AttributeError, TypeError, ValueError):
        log("digest.json unreadable — rebuilding window")
        return []
    items = []
    for it in raw_items:
        if not isinstance(it, dict):
            continue
        try:
            published = datetime.fromisoformat(it["published"])
            if published.tzinfo is None:
                published = published.replace(tzinfo=timezone.utc)
            it["published"] = published
            key = it["key"]
        except (KeyError, ValueError, TypeError, AttributeError):
            continue
        if not str(it.get("page_url", "")).startswith(BASE_URL + "/articles/"):
            continue  # never publish a link that leaves our own Pages host
        if (ARTICLES / f"{key}.html").exists():
            items.append(it)
    return items


def save_digest(items: list[dict]) -> None:
    keep = ("key", "title", "link", "page_url", "summary", "category", "source", "author")
    payload = {
        "version": 1,
        "items": [
            {**{k: it.get(k, "") for k in keep}, "published": it["published"].isoformat()}
            for it in items
        ],
    }
    DIGEST_PATH.write_text(json.dumps(payload, indent=1) + "\n")


def write_if_changed(path: Path, content: str) -> bool:
    content = content.strip() + "\n"
    if path.exists() and path.read_text() == content:
        return False
    path.write_text(content)
    return True


# --------------------------------------------------------------------------- #
# main
# --------------------------------------------------------------------------- #
def main() -> int:
    sess = session()
    seen = load_seen()
    now = datetime.now(timezone.utc)
    cutoff = now - timedelta(days=FRESH_DAYS)
    ARTICLES.mkdir(exist_ok=True)

    previous = load_digest()
    previous = [it for it in previous if it["published"] >= cutoff]
    prev_keys = [it["key"] for it in previous]
    known = set(prev_keys) | set(seen)

    # Per-source quota: a source may contribute at most max_items to the
    # window, so only candidates that fill a free slot or are newer than the
    # oldest item that source already has can make it in. Without this, each
    # run would pull the next N items off every feed and churn the digest.
    count_by_source: dict[str, int] = {}
    oldest_by_source: dict[str, datetime] = {}
    for it in previous:
        src_name = it.get("source", "")
        count_by_source[src_name] = count_by_source.get(src_name, 0) + 1
        if src_name not in oldest_by_source or it["published"] < oldest_by_source[src_name]:
            oldest_by_source[src_name] = it["published"]

    candidates: list[dict] = []
    status: list[tuple[str, int, str]] = []
    run_keys: set[str] = set()  # cross-source dedupe within this run

    for src in feeds.SOURCES:
        log(f"[source] {src['name']}")
        kind = src.get("kind", "rss")
        try:
            found = json_candidates(src, sess) if kind == "json_api" else rss_candidates(src, sess)
        except Exception as exc:  # one bad source never kills the digest
            log(f"    source failed: {exc.__class__.__name__}: {exc}")
            status.append((src["name"], 0, f"error: {exc.__class__.__name__}"))
            continue

        fresh = [c for c in found if c["published"] >= cutoff]
        fresh.sort(key=lambda c: c["published"], reverse=True)
        quota = int(src.get("max_items", 5))
        already = count_by_source.get(src["name"], 0)
        oldest = oldest_by_source.get(src["name"])
        kept, seen_here = [], set()
        for cand in fresh:
            key = item_key(cand["link"])
            if key in known or key in run_keys or key in seen_here:
                continue
            fills_slot = already + len(kept) < quota
            displaces = oldest is not None and cand["published"] > oldest
            if not (fills_slot or displaces):
                break  # candidates are newest-first; nothing older can win
            seen_here.add(key)
            run_keys.add(key)
            cand["key"] = key
            kept.append(cand)
            if len(kept) >= quota:
                break
        log(f"    {len(found)} in feed, {len(fresh)} fresh, {len(kept)} new")
        status.append((src["name"], len(kept), f"{len(found)} in feed"))
        candidates.extend(kept)
        time.sleep(1.0)  # be polite to upstreams

    candidates.sort(key=lambda c: c["published"], reverse=True)
    candidates = candidates[:TOTAL_CAP]

    rendered: list[dict] = []
    mode_counts: dict[str, int] = {}
    for cand in candidates:
        try:
            paras, mode = extract(cand, sess)
        except Exception as exc:  # one pathological item must not kill the run
            log(f"    extract failed {cand['link']}: {exc.__class__.__name__}: {exc}")
            continue
        if not paras:
            log(f"    skipping empty extraction: {cand['title'][:60]}")
            continue
        page = render_article(cand, paras, mode)
        (ARTICLES / f"{cand['key']}.html").write_text(page)
        cand["page_url"] = f"{BASE_URL}/articles/{cand['key']}.html"
        cand["summary"] = summary_from(paras, cand.get("feed_summary", ""))
        cand["mode"] = mode
        rendered.append(cand)
        mode_counts[mode] = mode_counts.get(mode, 0) + 1
        time.sleep(0.5)  # stagger article-page fetches
        log(f"    ✓ {mode:11s} {len(paras):3d}p  {cand['title'][:70]}")

    # The feed is a rolling window, not a per-run delta: keep the fresh items
    # already published, add this run's new ones, trim oldest-first to the cap.
    merged = sorted(previous + rendered, key=lambda c: c["published"], reverse=True)
    quota_by_name = {s["name"]: int(s.get("max_items", 5)) for s in feeds.SOURCES}
    per_source: dict[str, int] = {}
    items = []
    for it in merged:
        name = it.get("source", "")
        cap = quota_by_name.get(name, TOTAL_CAP)
        used = per_source.get(name, 0)
        if used >= cap:
            continue
        per_source[name] = used + 1
        items.append(it)
    items = items[:TOTAL_CAP]
    keys = [it["key"] for it in items]

    # Extraction may have run for a candidate that the caps then trimmed.
    # Drop its page file and keep it out of seen.json: an unseen item is retried
    # next run, whereas a seen-but-unpublished one is blacklisted for nothing.
    landed = set(keys)
    for cand in rendered:
        if cand["key"] not in landed:
            (ARTICLES / f"{cand['key']}.html").unlink(missing_ok=True)
    rendered = [c for c in rendered if c["key"] in landed]

    if keys == prev_keys:
        # Nothing new survived the caps and nothing aged out: identical feed.
        log("No change — leaving feed untouched.")
        return 0

    if not items:
        log("Digest is empty after merge — refusing to publish an empty feed.")
        return 1

    xml = build_xml(items)
    validate(xml)
    XML_PATH.write_bytes(xml)
    save_digest(items)

    prune_before = (now - timedelta(days=SEEN_KEEP_DAYS)).strftime("%Y-%m-%d")
    today = now.strftime("%Y-%m-%d")
    for it in rendered:
        seen[it["key"]] = today
    seen = {k: v for k, v in seen.items() if v >= prune_before}
    SEEN_PATH.write_text(json.dumps({"version": 1, "seen": seen}, indent=1, sort_keys=True) + "\n")

    log(f"Wrote {XML_PATH.name}: {len(items)} items "
        f"({len(rendered)} new, {len(items) - len(rendered)} carried over), "
        f"{XML_PATH.stat().st_size // 1024} KB")

    if rendered or keys != prev_keys:
        lines = [
            f"# Digest run {now.strftime('%Y-%m-%dT%H:%MZ')}",
            "",
            f"{len(rendered)} new items, {len(items)} in window"
            + (" — " + ", ".join(f"{k} {v}" for k, v in sorted(mode_counts.items()))
               if mode_counts else " — aging only, no new extractions"),
            "",
            "| source | new | feed |",
            "|---|---|---|",
        ]
        lines += [f"| {n} | {c} | {s} |" for n, c, s in status]
        if len(rendered) < 5:
            lines += ["", f"**LOW NEW-ITEM COUNT ({len(rendered)}) — check the per-source table above.**"]
        write_if_changed(SUMMARY_PATH, "\n".join(lines))
    return 0


if __name__ == "__main__":
    sys.exit(main())
