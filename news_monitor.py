check again 

#!/usr/bin/env python3
"""Collect new links from the configured official fintech newsrooms."""
from __future__ import annotations
import argparse, json, logging
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timezone
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
    "blog",
)

def clean_url(url):
    url, _ = urldefrag(url)
    parsed = urlparse(url)
    return parsed._replace(query="").geturl().rstrip("/")

def extract_links(html, source_url):
    soup, results, found = BeautifulSoup(html, "html.parser"), [], set()
    for anchor in soup.find_all("a", href=True):
        title = " ".join(anchor.get_text(" ", strip=True).split())
        url = clean_url(urljoin(source_url, anchor["href"]))
        parsed, source = urlparse(url), urlparse(source_url)
        if ( parsed.netloc != source.netloc
            or url == clean_url(source_url)
            or url in found
            or len(title) < 12
            or any(part in parsed.path.lower() for part in IGNORE)
            or parsed.path.lower().endswith((".pdf", ".jpg", ".png"))):
            continue
        results.append({"title": title, "url": url})
        found.add(url)
    return results

def load(path, fallback):
    return json.loads(path.read_text()) if path.exists() else fallback

def collect(source):
    try:
        response = requests.get(
            source["url"],
            headers={"User-Agent": USER_AGENT},
            timeout=30
        )
        response.raise_for_status()

        articles = extract_links(response.text, response.url)

        if source.get("company", "").lower() == "wise":
            print("WISE CONFIGURED URL:", source["url"])
            print("WISE FINAL URL:", response.url)
            print("WISE STATUS:", response.status_code)
            print("WISE HTML LENGTH:", len(response.text))
            print("WISE LINKS FOUND:", len(articles))

            for article in articles[:20]:
                print("WISE ARTICLE:", article)

        return source, articles, None

    except requests.RequestException as error:
        print("REQUEST ERROR:", source.get("company"), error)
        return source, [], error

def main():
    parser = argparse.ArgumentParser(description="Find new official fintech news.")
    parser.add_argument("--sources", type=Path, default=DEFAULT_SOURCES)
    parser.add_argument("--state", type=Path, default=ROOT / "state.json")
    parser.add_argument("--bootstrap", action="store_true")
    parser.add_argument("--format", choices=("json", "markdown"), default="markdown")
    args = parser.parse_args()
    sources = [item for item in load(args.sources, [])]
    state = load(args.state, {"seen_urls": []})
    seen, new = set(state.get("seen_urls", [])), []
    with ThreadPoolExecutor(max_workers=12) as pool:
        for future in as_completed([pool.submit(collect, item) for item in sources]):
            source, articles, error = future.result()
            if error:
                logging.error("%s: %s", source["company"], error)
                continue
            for article in articles:
                article.update({key: source[key] for key in ("category", "subcategory", "company")})
                if article["url"] not in seen:
                    new.append(article)
                    seen.add(article["url"])
    args.state.write_text(json.dumps({"seen_urls": sorted(seen), "last_run": datetime.now(timezone.utc).isoformat()}, indent=2) + "\n")
    if args.bootstrap:
        return
    if args.format == "json":
        print(json.dumps(new, indent=2))
        return
    if not new:
        print("No new official news found.")
        return
    print("# New official fintech news\n")
    groups = {}
    for article in new:
        groups.setdefault((article["category"], article["subcategory"]), []).append(article)
    for (category, subcategory), articles in sorted(groups.items()):
        print(f"## {category}\n\n### {subcategory}\n")
        for article in sorted(articles, key=lambda item: (item["company"], item["title"])):
            print(f"- **{article['company']}**: [{article['title']}]({article['url']})")
        print()

if __name__ == "__main__":
    main()
