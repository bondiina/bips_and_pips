#!/usr/bin/env python3
"""Collect new links from the configured official fintech newsrooms."""

from __future__ import annotations

import argparse
import json
import logging
import re
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timezone, timedelta
from pathlib import Path
from urllib.parse import urljoin, urlparse, urldefrag

import requests
from bs4 import BeautifulSoup


ROOT = Path(__file__).resolve().parent
DEFAULT_SOURCES = ROOT / "sources_verified.json"
USER_AGENT = "OfficialNewsMonitor/1.0"

IGNORE = (
    "/privacy",
    "/terms",
    "/login",
    "/contact",
    "/careers",
    "/career",
    "/jobs",
    "/job",
    "/products",
    "/product/",
    "/solutions",
    "/solution",
    "/integrations",
    "/integration",
    "/partners",
    "/partner",
    "/pricing",
    "/signup",
    "/register",
    "/demo",
    "/request-demo",
    "/request-a-demo",
    "/resources",
    "/resource",
    "/customer-stories",
    "/customer-story",
    "/case-studies",
    "/case-study",
    "/webinars",
    "/webinar",
    "/events",
    "/event",
    "/industries",
    "/industry",
    "/support",
    "/help",
    "/docs",
    "/documentation",
)


def clean_url(url):
    url, _ = urldefrag(url)
    parsed = urlparse(url)
    return parsed._replace(query="").geturl().rstrip("/")


def get_date(url):
    try:
        response = requests.get(
            url,
            headers={"User-Agent": USER_AGENT},
            timeout=10,
        )
        response.raise_for_status()

        soup = BeautifulSoup(
            response.text,
            "html.parser",
        )

        # JSON-LD
        for script in soup.find_all(
            "script",
            type="application/ld+json",
        ):
            try:
                data = json.loads(
                    script.string or script.get_text()
                )
            except Exception:
                continue

            items = data if isinstance(data, list) else [data]

            for item in items:
                if not isinstance(item, dict):
                    continue

                if item.get("@type") in (
                    "Article",
                    "NewsArticle",
                    "BlogPosting",
                ):
                    value = (
                        item.get("datePublished")
                        or item.get("dateCreated")
                    )

                    if value:
                        return parse_date(value)

        # Meta tags
        for name in (
            "article:published_time",
            "datePublished",
            "publishdate",
            "publicationdate",
        ):
            tag = soup.find(
                "meta",
                attrs={
                    "property": name,
                },
            ) or soup.find(
                "meta",
                attrs={
                    "name": name,
                },
            )

            if tag and tag.get("content"):
                date = parse_date(tag["content"])

                if date:
                    return date

        # <time>
        tag = soup.find(
            "time",
            datetime=True,
        )

        if tag:
            return parse_date(
                tag.get("datetime")
            )

    except Exception:
        pass

    return None


def parse_date(value):
    if not value:
        return None

    try:
        date = datetime.fromisoformat(
            value.replace("Z", "+00:00")
        )

        if date.tzinfo is None:
            date = date.replace(
                tzinfo=timezone.utc
            )

        return date.astimezone(
            timezone.utc
        )

    except ValueError:
        pass

    for fmt in (
        "%B %d, %Y",
        "%b %d, %Y",
        "%d %B %Y",
        "%d %b %Y",
        "%Y-%m-%d",
    ):
        try:
            return datetime.strptime(
                value,
                fmt,
            ).replace(
                tzinfo=timezone.utc
            )
        except ValueError:
            pass

    return None


def looks_like_news(url, title):
    path = urlparse(url).path.lower()
    title = title.lower()

    # Strong news URL patterns
    news_paths = (
        "/blog/",
        "/news/",
        "/newsroom/",
        "/press-release/",
        "/press-releases/",
        "/press/",
        "/articles/",
        "/article/",
        "/insights/",
        "/updates/",
    )

    if any(x in path for x in news_paths):
        return True

    # If URL isn't obviously news, use title signals
    news_words = (
        "announces",
        "announcement",
        "launches",
        "launch",
        "introduces",
        "partnership",
        "partners",
        "acquires",
        "acquisition",
        "funding",
        "raises",
        "investment",
        "appoints",
        "appointed",
        "expands",
        "expansion",
        "reports",
        "results",
        "agreement",
        "available",
        "unveils",
    )

    return any(
        word in title
        for word in news_words
    )


def extract_links(html, source_url):
    soup = BeautifulSoup(
        html,
        "html.parser",
    )

    results = []
    found = set()

    for anchor in soup.find_all(
        "a",
        href=True,
    ):
        title = " ".join(
            anchor.get_text(
                " ",
                strip=True,
            ).split()
        )

        url = clean_url(
            urljoin(
                source_url,
                anchor["href"],
            )
        )

        parsed = urlparse(url)
        source = urlparse(source_url)

        if (
            parsed.netloc != source.netloc
            or url == clean_url(source_url)
            or url in found
            or len(title) < 12
            or any(
                part in parsed.path.lower()
                for part in IGNORE
            )
            or parsed.path.lower().endswith(
                (
                    ".pdf",
                    ".jpg",
                    ".jpeg",
                    ".png",
                    ".gif",
                    ".svg",
                )
            )
            or not looks_like_news(
                url,
                title,
            )
        ):
            continue

        results.append(
            {
                "title": title,
                "url": url,
            }
        )

        found.add(url)

    return results


def load(path, fallback):
    return (
        json.loads(
            path.read_text(
                encoding="utf-8"
            )
        )
        if path.exists()
        else fallback
    )


def collect(source):
    try:
        response = requests.get(
            source["url"],
            headers={
                "User-Agent": USER_AGENT
            },
            timeout=30,
        )

        response.raise_for_status()

        cutoff = (
            datetime.now(timezone.utc)
            - timedelta(days=7)
        )

        articles = []

        for article in extract_links(
            response.text,
            source["url"],
        ):
            published = get_date(
                article["url"]
            )

            # If we can determine the date,
            # enforce the 7-day window.
            if published:
                if published < cutoff:
                    continue

                article["published"] = (
                    published.isoformat()
                )

            # If no date is available, keep it.
            # This prevents legitimate newsroom
            # articles from disappearing.
            articles.append(article)

        return source, articles, None

    except requests.RequestException as error:
        return source, [], error


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

    sources = [
        item
        for item in load(
            args.sources,
            [],
        )
        if not item.get(
            "verification",
            "",
        ).startswith("needs manual")
    ]

    state = load(
        args.state,
        {
            "seen_urls": []
        },
    )

    seen = set(
        state.get(
            "seen_urls",
            [],
        )
    )

    new = []

    with ThreadPoolExecutor(
        max_workers=12
    ) as pool:

        futures = [
            pool.submit(
                collect,
                item,
            )
            for item in sources
        ]

        for future in as_completed(
            futures
        ):
            source, articles, error = (
                future.result()
            )

            if error:
                logging.error(
                    "%s: %s",
                    source["company"],
                    error,
                )
                continue

            for article in articles:

                article.update({
                    key: source[key]
                    for key in (
                        "category",
                        "subcategory",
                        "company",
                    )
                })

                if article["url"] not in seen:
                    new.append(article)
                    seen.add(article["url"])

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
        + "\n"
    )

    if args.bootstrap:
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
        print(
            "No new official news found."
        )
        return

    print(
        "# New official fintech news\n"
    )

    groups = {}

    for article in new:
        groups.setdefault(
            (
                article["category"],
                article["subcategory"],
            ),
            [],
        ).append(article)

    for (
        category,
        subcategory,
    ), articles in sorted(
        groups.items()
    ):

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
            date = article.get(
                "published",
                "",
            )

            date = (
                date[:10]
                if date
                else "date unavailable"
            )

            print(
                f"- **{article['company']}**: "
                f"[{article['title']}]"
                f"({article['url']})"
                f" — {date}"
            )

        print()


if __name__ == "__main__":
    main()
