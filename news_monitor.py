#!/usr/bin/env python3
"""Collect new links from configured official fintech newsrooms."""

from __future__ import annotations

import argparse
import json
import logging
import sys
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urljoin, urlparse, urldefrag

import requests
from bs4 import BeautifulSoup


ROOT = Path(__file__).resolve().parent
DEFAULT_SOURCES = ROOT / "sources_verified.json"

USER_AGENT = (
    "Mozilla/5.0 (compatible; OfficialNewsMonitor/2.0; "
    "+https://github.com/)"
)

# Exact/non-news path components.
# Do NOT put generic substrings such as "blog" or "/partner" here.
IGNORE_SEGMENTS = {
    "privacy",
    "terms",
    "login",
    "contact",
    "careers",
    "career",
    "jobs",
    "job",
    "products",
    "product",
    "solutions",
    "solution",
    "integrations",
    "integration",
    "partners",
    "partner",
    "pricing",
    "signup",
    "register",
    "demo",
    "request-demo",
    "request-a-demo",
    "customer-stories",
    "customer-story",
    "case-studies",
    "case-study",
    "webinars",
    "webinar",
    "events",
    "event",
    "industries",
    "industry",
    "support",
    "help",
    "docs",
    "documentation",
}

IGNORE_EXTENSIONS = (
    ".pdf",
    ".jpg",
    ".jpeg",
    ".png",
    ".gif",
    ".svg",
    ".webp",
    ".zip",
)

GENERIC_TITLES = {
    "",
    "read more",
    "learn more",
    "find out more",
    "view more",
    "more",
    "continue reading",
    "read article",
    "view article",
    "click here",
}


def clean_url(url: str) -> str:
    """Remove fragments, tracking/query parameters and trailing slash."""
    url, _ = urldefrag(url)
    parsed = urlparse(url)

    return (
        parsed._replace(query="", fragment="")
        .geturl()
        .rstrip("/")
    )


def hostname(url: str) -> str:
    """Normalize hostnames so www.example.com == example.com."""
    host = (urlparse(url).hostname or "").lower()

    if host.startswith("www."):
        host = host[4:]

    return host


def same_host(url_a: str, url_b: str) -> bool:
    return hostname(url_a) == hostname(url_b)


def ignored_path(url: str) -> bool:
    """Reject clearly non-news paths without broad substring matching."""
    parsed = urlparse(url)
    path = parsed.path.lower()

    if path.endswith(IGNORE_EXTENSIONS):
        return True

    segments = {
        segment
        for segment in path.strip("/").split("/")
        if segment
    }

    return bool(segments & IGNORE_SEGMENTS)


def get_title(anchor) -> str:
    """
    Try several methods to obtain a useful article title.

    Some newsroom cards use an anchor containing only 'Read more',
    while the real headline sits in a nearby heading.
    """

    # Normal anchor text.
    text = " ".join(anchor.get_text(" ", strip=True).split())

    if text.lower() not in GENERIC_TITLES and len(text) >= 4:
        return text

    # aria-label / title attributes.
    for attribute in ("aria-label", "title"):
        value = anchor.get(attribute)

        if value:
            value = " ".join(value.split())

            if value.lower() not in GENERIC_TITLES and len(value) >= 4:
                return value

    # Look around the anchor for a headline.
    parent = anchor

    for _ in range(4):
        parent = parent.parent

        if parent is None:
            break

        heading = parent.find(["h1", "h2", "h3", "h4", "h5"])

        if heading:
            value = " ".join(
                heading.get_text(" ", strip=True).split()
            )

            if value.lower() not in GENERIC_TITLES and len(value) >= 4:
                return value

    # Keep the link rather than silently deleting it.
    if text:
        return text

    return "Untitled news item"


def extract_links(html: str, source_url: str):
    soup = BeautifulSoup(html, "html.parser")

    results = []
    found = set()

    source_clean = clean_url(source_url)

    for anchor in soup.find_all("a", href=True):
        href = anchor.get("href", "").strip()

        if not href:
            continue

        if href.startswith(("#", "mailto:", "tel:", "javascript:")):
            continue

        url = clean_url(urljoin(source_url, href))

        if not url:
            continue

        parsed = urlparse(url)

        if parsed.scheme not in ("http", "https"):
            continue

        # Only accept links from the newsroom's own hostname.
        if not same_host(url, source_url):
            continue

        # Don't include the source page itself.
        if url == source_clean:
            continue

        # Avoid duplicates on the same page.
        if url in found:
            continue

        if ignored_path(url):
            continue

        title = get_title(anchor)

        results.append(
            {
                "title": title,
                "url": url,
            }
        )

        found.add(url)

    return results


def load(path: Path, fallback):
    if not path.exists():
        return fallback

    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError) as error:
        logging.error("Could not read %s: %s", path, error)
        return fallback


def collect(source):
    company = source.get("company", "Unknown company")
    configured_url = source.get("url")

    if not configured_url:
        return source, [], "Missing URL", None

    try:
        response = requests.get(
            configured_url,
            headers={
                "User-Agent": USER_AGENT,
                "Accept": (
                    "text/html,application/xhtml+xml,"
                    "application/xml;q=0.9,*/*;q=0.8"
                ),
                "Accept-Language": "en-GB,en;q=0.9",
            },
            timeout=30,
            allow_redirects=True,
        )

        response.raise_for_status()

        # IMPORTANT:
        # use response.url rather than configured_url because many
        # newsrooms redirect to locale-specific pages.
        articles = extract_links(
            response.text,
            response.url,
        )

        diagnostic = {
            "company": company,
            "configured_url": configured_url,
            "final_url": response.url,
            "status": response.status_code,
            "html_bytes": len(response.content),
            "links_found": len(articles),
        }

        return source, articles, None, diagnostic

    except requests.RequestException as error:
        diagnostic = {
            "company": company,
            "configured_url": configured_url,
            "error": str(error),
        }

        return source, [], str(error), diagnostic

    except Exception as error:
        diagnostic = {
            "company": company,
            "configured_url": configured_url,
            "error": f"{type(error).__name__}: {error}",
        }

        return source, [], str(error), diagnostic


def main():
    parser = argparse.ArgumentParser(
        description="Find new official fintech news."
    )

    parser.add_argument(
        "--sources",
        type=Path,
        default=DEFAULT_SOURCES,
    )

    parser.add_argument(
        "--state",
        type=Path,
        default=ROOT / "state.json",
    )

    parser.add_argument(
        "--bootstrap",
        action="store_true",
    )

    parser.add_argument(
        "--format",
        choices=("json", "markdown"),
        default="markdown",
    )

    args = parser.parse_args()

    logging.basicConfig(
        level=logging.INFO,
        format="%(levelname)s: %(message)s",
        stream=sys.stderr,
    )

    # CHECK EVERY CONFIGURED SOURCE.
    sources = load(args.sources, [])

    if not isinstance(sources, list):
        logging.error(
            "%s must contain a JSON array.",
            args.sources,
        )
        sys.exit(1)

    logging.info(
        "Loaded %d configured sources.",
        len(sources),
    )

    state = load(
        args.state,
        {
            "seen_urls": [],
        },
    )

    seen = set(state.get("seen_urls", []))
    new = []

    successful_sources = 0
    failed_sources = 0
    zero_link_sources = 0
    total_links = 0

    diagnostics = []

    with ThreadPoolExecutor(max_workers=12) as pool:
        futures = [
            pool.submit(collect, source)
            for source in sources
        ]

        for future in as_completed(futures):
            source, articles, error, diagnostic = future.result()

            diagnostics.append(diagnostic)

            company = source.get(
                "company",
                "Unknown company",
            )

            if error:
                failed_sources += 1

                logging.error(
                    "%s FAILED: %s",
                    company,
                    error,
                )

                continue

            successful_sources += 1
            total_links += len(articles)

            if not articles:
                zero_link_sources += 1

                logging.warning(
                    "%s returned 0 candidate links (%s)",
                    company,
                    diagnostic.get("final_url"),
                )
            else:
                logging.info(
                    "%s: %d candidate links",
                    company,
                    len(articles),
                )

            for article in articles:
                article.update(
                    {
                        "category": source.get(
                            "category",
                            "Other",
                        ),
                        "subcategory": source.get(
                            "subcategory",
                            "Other",
                        ),
                        "company": company,
                    }
                )

                if article["url"] not in seen:
                    new.append(article)
                    seen.add(article["url"])

    # Persist URL state.
    args.state.write_text(
        json.dumps(
            {
                "seen_urls": sorted(seen),
                "last_run": datetime.now(
                    timezone.utc
                ).isoformat(),
            },
            indent=2,
        )
        + "\n",
        encoding="utf-8",
    )

    # Also save diagnostics locally.
    # This does NOT affect latest-news.md.
    debug_path = ROOT / "monitor-debug.json"

    debug_path.write_text(
        json.dumps(
            {
                "run_at": datetime.now(
                    timezone.utc
                ).isoformat(),
                "configured_sources": len(sources),
                "successful_sources": successful_sources,
                "failed_sources": failed_sources,
                "zero_link_sources": zero_link_sources,
                "candidate_links_found": total_links,
                "new_links_found": len(new),
                "sources": sorted(
                    diagnostics,
                    key=lambda item: (
                        item.get("company") or ""
                    ).lower(),
                ),
            },
            indent=2,
        )
        + "\n",
        encoding="utf-8",
    )

    logging.info(
        (
            "SUMMARY: %d configured | %d successful | "
            "%d failed | %d returned zero links | "
            "%d candidate links | %d new"
        ),
        len(sources),
        successful_sources,
        failed_sources,
        zero_link_sources,
        total_links,
        len(new),
    )

    if args.bootstrap:
        logging.info(
            "Bootstrap completed. %d URLs stored.",
            len(seen),
        )
        return

    if args.format == "json":
        print(
            json.dumps(
                new,
                indent=2,
            )
        )
        return

    if not new:
        print("No new official news found.")
        return

    print("# New official fintech news\n")

    groups = {}

    for article in new:
        key = (
            article["category"],
            article["subcategory"],
        )

        groups.setdefault(key, []).append(article)

    for (
        category,
        subcategory,
    ), articles in sorted(groups.items()):

        print(
            f"## {category}\n\n"
            f"### {subcategory}\n"
        )

        for article in sorted(
            articles,
            key=lambda item: (
                item["company"],
                item["title"],
            ),
        ):
            print(
                f"- **{article['company']}**: "
                f"[{article['title']}]"
                f"({article['url']})"
            )

        print()


if __name__ == "__main__":
    main()
