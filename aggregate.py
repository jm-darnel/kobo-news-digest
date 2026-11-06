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
TRACKING_PARAMS = re.compile(r"^(utm_|fbclid|gclid|ref|s_cid|mc_cid|mc_eid)", re.I)
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
                r.encoding = r.encoding or "utf-8"
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


def strip_emoji(text: str) -> str:
    return EMOJI.sub("", text)


def clean_text(text: str) -> str:
    text = html.unescape(text) if "&" in text else text
    text = strip_emoji(text).replace("\u00a0", " ")
    return re.sub(r"[ \t\r\f\v]+", " ", text).strip()


# --------------------------------------------------------------------------- #
# HTML -> paragraphs
# --------------------------------------------------------------------------- #
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
    for el in root.iter():
        if not isinstance(el.tag, str):
            continue
        tag = el.tag.lower()
        if tag == "table":
            paras.extend(table_lines(el))
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
    return paras


def text_to_paragraphs(text: str) -> list[str]:
    text = strip_emoji(text or "").replace("\r", "")
    blocks = [clean_text(b) for b in re.split(r"\n\s*\n|\n", text)]
    return [b for b in blocks if b]


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
    if len(base) < 40 and paras:
        base = " ".join(paras[:2])
    base = strip_emoji(base)
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
        if not link:
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
        if not slug:
            continue
        link = src["article_url"].format(slug=slug)
        published = parse_date({"published_parsed": None})
        stamp = meta.get("first_published_at")
        if stamp:
            try:
                published = datetime.fromisoformat(stamp)
                if published.tzinfo is None:
                    published = published.replace(tzinfo=timezone.utc)
            except ValueError:
                pass
        out.append({
            "source": src["name"],
            "category": src.get("category", ""),
            "title": clean_text(page.get("title") or "Untitled"),
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
        + "\n".join(body)
        + "\n"
        + attribution
        + "\n</body>\n</html>\n"
    )


# --------------------------------------------------------------------------- #
# XML
# --------------------------------------------------------------------------- #
def cdata(text: str) -> str:
    """CDATA-safe text: a literal ]]> would end the section early."""
    return text.replace("]]>", "]]]]><![CDATA[>")


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
        data = json.loads(SEEN_PATH.read_text())
        return data.get("seen", {})
    except (json.JSONDecodeError, AttributeError):
        log("seen.json unreadable — starting fresh")
        return {}


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
    cutoff = datetime.now(timezone.utc) - timedelta(days=FRESH_DAYS)
    ARTICLES.mkdir(exist_ok=True)

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
        kept, seen_here = [], set()
        for cand in fresh:
            key = item_key(cand["link"])
            if key in seen or key in run_keys or key in seen_here:
                continue
            seen_here.add(key)
            run_keys.add(key)
            cand["key"] = key
            kept.append(cand)
            if len(kept) >= cand["max_items"]:
                break
        log(f"    {len(found)} in feed, {len(fresh)} fresh, {len(kept)} new")
        status.append((src["name"], len(kept), f"{len(found)} in feed"))
        candidates.extend(kept)
        time.sleep(1.0)  # be polite to upstreams

    # Global cap, newest first.
    candidates.sort(key=lambda c: c["published"], reverse=True)
    candidates = candidates[:TOTAL_CAP]

    if not candidates:
        log("No new items — nothing to do.")
        if SUMMARY_PATH.exists():
            SUMMARY_PATH.unlink()
        return 0

    rendered: list[dict] = []
    mode_counts: dict[str, int] = {}
    for cand in candidates:
        paras, mode = extract(cand, sess)
        if not paras:
            log(f"    skipping empty extraction: {cand['title'][:60]}")
            continue
        page = render_article(cand, paras, mode)
        (ARTICLES / f"{cand['key']}.html").write_text(page)
        cand["page_url"] = f"{BASE_URL}/articles/{cand['key']}.html"
        cand["summary"] = summary_from(paras, cand.get("feed_summary", ""))
        cand["mode"] = mode
        cand["paragraphs"] = len(paras)
        rendered.append(cand)
        mode_counts[mode] = mode_counts.get(mode, 0) + 1
        seen[cand["key"]] = datetime.now(timezone.utc).strftime("%Y-%m-%d")
        log(f"    ✓ {mode:11s} {len(paras):3d}p  {cand['title'][:70]}")

    if not rendered:
        log("All extractions failed — refusing to write an empty digest.")
        return 1

    xml = build_xml(rendered)
    validate(xml)
    XML_PATH.write_bytes(xml)

    prune_before = (datetime.now(timezone.utc) - timedelta(days=SEEN_KEEP_DAYS)).strftime("%Y-%m-%d")
    seen = {k: v for k, v in seen.items() if v >= prune_before}
    SEEN_PATH.write_text(json.dumps({"version": 1, "seen": seen}, indent=1, sort_keys=True) + "\n")

    lines = [
        f"# Digest run {datetime.now(timezone.utc).strftime('%Y-%m-%dT%H:%MZ')}",
        "",
        f"{len(rendered)} new items — " + ", ".join(f"{k} {v}" for k, v in sorted(mode_counts.items())),
        "",
        "| source | new | feed |",
        "|---|---|---|",
    ]
    lines += [f"| {n} | {c} | {s} |" for n, c, s in status]
    if len(rendered) < 5:
        lines += ["", f"**LOW ITEM COUNT ({len(rendered)}) — check upstream sources.**"]
    write_if_changed(SUMMARY_PATH, "\n".join(lines))

    log(f"Wrote {XML_PATH.name}: {len(rendered)} items, {XML_PATH.stat().st_size // 1024} KB")
    return 0


if __name__ == "__main__":
    sys.exit(main())
