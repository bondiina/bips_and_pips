#!/usr/bin/env python3
"""Collect new official fintech news from the last 7 days."""

from __future__ import annotations

import argparse, json, logging, re
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
    "/privacy", "/terms", "/login", "/contact",
    "/careers", "/career", "/jobs", "/job",
    "/products", "/product/", "/solutions", "/solution",
    "/integrations", "/integration", "/partners", "/partner",
    "/pricing", "/signup", "/register", "/demo",
    "/request-demo", "/request-a-demo",
    "/resources", "/resource", "/customer-stories",
    "/customer-story", "/case-studies", "/case-study",
    "/webinars", "/webinar", "/events", "/event",
    "/industries", "/industry", "/support", "/help",
    "/docs", "/documentation",
)


def clean_url(url):
    url, _ = urldefrag(url)
    parsed = urlparse(url)
    return parsed._replace(query="").geturl().rstrip("/")


def parse_date(value):
    if not value:
        return None

    value = value.strip()

    try:
        return datetime.fromisoformat(
            value.replace("Z", "+00:00")
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
                value, fmt
            ).replace(tzinfo=timezone.utc)
        except ValueError:
            pass

    return None


def extract_date(anchor):
    """
    Look for a publication date close to the article link.
    """
    parent = anchor

    for _ in range(3):
        if parent:
            text = parent.get_text(" ", strip=True)

            match = re.search(
                r"\b(?:January|February|March|April|May|June|July|"
                r"August|September|October|November|December)\s+\d{1,2},\s+\d{4}\b",
                text,
                re.I,
            )

            if match:
                return parse_date(match.group())

            match = re.search(
                r"\b\d{1,2}\s+(?:January|February|March|April|May|"
                r"June|July|August|September|October|November|December)\s+\d{4}\b",
                text,
                re.I,
            )

            if match:
                return parse_date(match.group())

            parent = parent.parent

    return None


def extract_links(html, source_url):
    soup = BeautifulSoup(html, "html.parser")
    results = []
    found = set()

    cutoff = (
        datetime.now(timezone.utc)
        - timedelta(days=7)
    )

    for anchor in soup.find_all("a", href=True):

        title = " ".join(
            anchor.get_text(" ", strip=True).split()
        )

        url = clean_url(
            urljoin(source_url, anchor["href"])
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
                (".pdf", ".jpg", ".jpeg", ".png", ".gif", ".svg")
            )
        ):
            continue

        published = extract_date(anchor)

        # Only keep articles where we can see a recent date
        # on the source/newsroom page.
        if not published or published < cutoff:
            continue

        results.append({
            "title": title,
            "url": url,
            "published": published.isoformat(),
        })

        found.add(url)

    return results


def load(path, fallback):
    return (
        json.loads(path.read_text())
        if path.exists()
        else fallback
    )


def collect(source):
    try:
        response = requests.get(
            source["url"],
            headers={"User-Agent": USER_AGENT},
            timeout=30,
        )
        response.raise_for_status()

        return (
            source,
            extract_links(
                response.text,
                source["url"],
            ),
            None,
        )

    except requests.RequestException as error:
        return source, [], error


def main():
    parser = argparse.ArgumentParser(
        description="Find official fintech news from the last 7 days."
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
        for item in load(args.sources, [])
        if not item.get(
            "verification",
            ""
        ).startswith("needs manual")
    ]

    state = load(
        args.state,
        {"seen_urls": []},
    )

    seen = set(
        state.get("seen_urls", [])
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

        for future in as_completed(futures):

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
        print(json.dumps(new, indent=2))
        return

    if not new:
        print(
            "No new official news "
            "from the last 7 days."
        )
        return

    print(
        "# New official fintech news — last 7 days\n"
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
    ), articles in sorted(groups.items()):

        print(
            f"## {category}\n\n"
            f"### {subcategory}\n"
        )

        for article in sorted(
            articles,
            key=lambda item: (
                item["published"],
                item["company"],
                item["title"],
            ),
            reverse=True,
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
