#!/usr/bin/env python3
"""Collect newly published links from configured official newsrooms."""
from __future__ import annotations
import argparse, json, logging
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urljoin, urlparse, urldefrag
import requests
from bs4 import BeautifulSoup

ROOT = Path(__file__).resolve().parent
USER_AGENT = "OfficialNewsMonitor/1.0 (+https://github.com/your-org/official-news-monitor)"
IGNORE = ("/privacy", "/terms", "/login", "/contact", "/careers", "/products")

def clean_url(url):
    url, _ = urldefrag(url)
    p = urlparse(url)
    return p._replace(query="").geturl().rstrip("/")

def extract_links(html, source_url):
    soup, results, found = BeautifulSoup(html, "html.parser"), [], set()
    for a in soup.find_all("a", href=True):
        title = " ".join(a.get_text(" ", strip=True).split())
        url = clean_url(urljoin(source_url, a["href"]))
        p, src = urlparse(url), urlparse(source_url)
        if (p.netloc != src.netloc or url == clean_url(source_url) or url in found or len(title) < 12
            or any(part in p.path.lower() for part in IGNORE) or p.path.lower().endswith((".pdf", ".jpg", ".png"))):
            continue
        results.append({"title": title, "url": url, "published_at": ""})
        found.add(url)
    return results

def load(path, fallback):
    return json.loads(path.read_text()) if path.exists() else fallback

def main():
    parser = argparse.ArgumentParser(description="Find new posts on official company newsrooms.")
    parser.add_argument("--sources", type=Path, default=ROOT / "sources_full.json")
    parser.add_argument("--state", type=Path, default=ROOT / "state.json")
    parser.add_argument("--bootstrap", action="store_true", help="Record current links but emit none.")
    parser.add_argument("--format", choices=("json", "markdown"), default="markdown")
    args = parser.parse_args()
    sources, state = load(args.sources, []), load(args.state, {"seen_urls": []})
    seen, new = set(state.get("seen_urls", [])), []
    session = requests.Session(); session.headers["User-Agent"] = USER_AGENT
    for source in sources:
        try:
            response = session.get(source["url"], timeout=30); response.raise_for_status()
            articles = extract_links(response.text, source["url"])
            logging.info("%s: %d candidate links", source["company"], len(articles))
            for article in articles:
                article.update({k: source[k] for k in ("vertical", "company", "news_type", "priority")})
                if article["url"] not in seen: new.append(article); seen.add(article["url"])
        except requests.RequestException as error:
            logging.error("%s: %s", source["company"], error)
    args.state.write_text(json.dumps({"seen_urls": sorted(seen), "last_run": datetime.now(timezone.utc).isoformat()}, indent=2) + "\n")
    if args.bootstrap: return 0
    if args.format == "json": print(json.dumps(new, indent=2))
    elif new:
        print("# New official company news\n")
        for item in new: print(f"- **{item['company']}** ({item['vertical']}): [{item['title']}]({item['url']})")
    else: print("No new official news found.")

if __name__ == "__main__": main()
