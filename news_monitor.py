#!/usr/bin/env python3
"""Collect new links from the configured official fintech newsrooms."""

from __future__ import annotations

import argparse
import json
import logging
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
            timeout=30,
        )
        response.raise_for_status()

        soup = BeautifulSoup(
            response.text,
            "html.parser",
        )

        # Common publication-date locations
        tag = (
            soup.find(
                "meta",
                property="article:published_time",
            )
            or soup.find(
                "meta",
                attrs={"name": "date"},
            )
            or soup.find(
                "meta",
                attrs={"name": "datePublished"},
            )
            or soup.find(
                "meta",
                attrs={"itemprop": "datePublished"},
            )
            or soup.find(
                "time",
                datetime=True,
            )
        )

        if not tag:
            return None

        value = (
            tag.get("content")
            or tag.get("datetime")
        )

        if not value:
            return None

        return datetime.fromisoformat(
            value.replace("Z", "+00:00")
        )

    except Exception:
        return None


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

        articles = []

        cutoff = (
            datetime.now(timezone.utc)
            - timedelta(days=7)
        )

        for article in extract_links(
            response.text,
            source["url"],
        ):
            published = get_date(
                article["url"]
            )

            if (
                published
                and published >= cutoff
            ):
                article["published"] = (
                    published.isoformat()
                )

                articles.append(article)

        return source, articles, None

    except requests.RequestException as error:
        return source, [], error


def main():
    parser = argparse.ArgumentParser(
        description=(
            "Find new official fintech news "
            "from the last 7 days."
        )
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
        choices=(
            "json",
            "markdown",
        ),
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

                article.update(
                    {
                        key: source[key]
                        for key in (
                            "category",
                            "subcategory",
                            "company",
                        )
                    }
                )

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
        + "\n",
        encoding="utf-8",
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
            "No new official news "
            "from the last 7 days."
        )
        return

    print(
        "# New official fintech news "
        "— last 7 days\n"
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
            print(
                f"- **{article['company']}**: "
                f"[{article['title']}]"
                f"({article['url']})"
                f" — {article['published'][:10]}"
            )

        print()


if __name__ == "__main__":
    main()
