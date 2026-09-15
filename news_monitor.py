#!/usr/bin/env python3
"""
Official company update monitor.

Collects:
- News / press releases
- Product launches
- Partnerships
- Funding / M&A
- Regulatory / licence updates
- Company updates
- Blog posts
- Changelog / API updates

Extraction priority:
1. RSS / Atom
2. JSON-LD Article / NewsArticle / BlogPosting
3. <article> and headline links
4. Links scoped to the configured newsroom section
5. Sitemap, when explicitly configured

Outputs:
- stdout              -> new articles for latest-news.md
- state.json          -> URLs already seen
- news_archive.json   -> full article history
- latest-week-news.md -> rolling 7-day report
- monitor-debug.json  -> diagnostics
"""

from __future__ import annotations

import argparse
import json
import logging
import re
import sys
import xml.etree.ElementTree as ET

from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timedelta, timezone
from email.utils import parsedate_to_datetime
from pathlib import Path
from urllib.parse import (
    parse_qsl,
    urlencode,
    urljoin,
    urlparse,
    urlunparse,
)

import requests
from bs4 import BeautifulSoup

# ---------------------------------------------------------------------------
# External news discovery
# ---------------------------------------------------------------------------

GOOGLE_NEWS_RSS = "https://news.google.com/rss/search"

EXTERNAL_NEWS_DAYS = 3
EXTERNAL_NEWS_WORKERS = 8
EXTERNAL_NEWS_MAX_RESULTS = 40


# These terms are used to find meaningful COMPANY EVENTS.
# They intentionally cover both positive and negative developments.

EVENT_RULES = [
    (
        "acquisition",
        (
            "acquires",
            "acquired",
            "acquisition",
            "merger",
            "merges",
            "merged",
            "buyout",
            "buys",
            "takeover",
            "takes over",
            "sells to",
            "sale of",
        ),
    ),

    (
        "funding",
        (
            "raises",
            "raised",
            "funding",
            "fundraise",
            "fundraising",
            "investment",
            "invests in",
            "backed by",
            "series a",
            "series b",
            "series c",
            "series d",
            "seed round",
            "valuation",
            "venture round",
            "capital raise",
            "debt financing",
        ),
    ),

    (
        "financial_results",
        (
            "earnings",
            "results",
            "revenue",
            "profit",
            "profits",
            "profitable",
            "profitability",
            "loss",
            "losses",
            "arr",
            "annual recurring revenue",
            "guidance",
            "forecast",
            "quarterly results",
            "annual results",
            "financial results",
            "growth",
        ),
    ),

    (
        "expansion",
        (
            "expands into",
            "expands to",
            "expansion",
            "enters the",
            "enters market",
            "launches in",
            "launched in",
            "live in",
            "now live",
            "available in",
            "opens in",
            "opens office",
            "new office",
            "new market",
            "new country",
            "international expansion",
            "global expansion",
        ),
    ),

    (
        "partnership",
        (
            "partnership",
            "partners with",
            "partnered with",
            "strategic partnership",
            "collaboration",
            "collaborates with",
            "teams up",
            "team up",
            "alliance",
            "selected by",
            "chosen by",
            "powered by",
            "works with",
            "working with",
            "integrates with",
            "integration with",
        ),
    ),

    (
        "customer_win",
        (
            "customer win",
            "new customer",
            "new client",
            "selected by",
            "chosen by",
            "signs",
            "signed",
            "contract",
            "awarded contract",
            "merchant win",
            "enterprise customer",
        ),
    ),

    (
        "product_release",
        (
            "launches",
            "launched",
            "launch of",
            "introduces",
            "introduced",
            "introducing",
            "unveils",
            "unveiled",
            "releases",
            "released",
            "rolls out",
            "rolled out",
            "new product",
            "new feature",
            "new service",
            "new platform",
            "new solution",
            "new offering",
            "now available",
            "general availability",
            "beta launch",
            "public beta",
        ),
    ),

    (
        "api_update",
        (
            "new api",
            "api launch",
            "api update",
            "api release",
            "sdk",
            "developer platform",
            "developer tools",
            "webhook",
            "new integration",
            "platform update",
            "changelog",
            "release notes",
        ),
    ),

    (
        "payments_update",
        (
            "instant payments",
            "real-time payments",
            "real time payments",
            "cross-border payments",
            "cross border payments",
            "payment rails",
            "payment network",
            "card issuing",
            "card acquiring",
            "acquiring",
            "issuing",
            "virtual account",
            "virtual accounts",
            "local accounts",
            "bank accounts",
            "open banking",
            "embedded finance",
            "embedded payments",
            "banking as a service",
            "banking-as-a-service",
            "stablecoin payments",
            "crypto payments",
            "remittance",
            "treasury product",
            "fx product",
        ),
    ),

    (
        "regulatory",
        (
            "licence",
            "license",
            "licensed",
            "authorisation",
            "authorization",
            "authorised",
            "authorized",
            "regulatory approval",
            "regulator approval",
            "approved by",
            "registered with",
            "fca",
            "pra",
            "sec",
            "cfpb",
            "finra",
            "fintrac",
            "mas",
            "asic",
            "bafin",
            "dfsa",
            "fsra",
            "eba",
            "central bank",
            "banking licence",
            "banking license",
            "emi licence",
            "emi license",
        ),
    ),

    (
        "legal_regulatory_issue",
        (
            "fine",
            "fined",
            "penalty",
            "penalised",
            "penalized",
            "investigation",
            "investigated",
            "probe",
            "regulatory probe",
            "enforcement",
            "enforcement action",
            "lawsuit",
            "lawsuits",
            "sued",
            "sues",
            "legal action",
            "class action",
            "complaint",
            "settlement",
            "settles",
            "charged",
            "charges",
            "accused",
            "cease and desist",
            "regulatory warning",
            "sanction",
            "sanctions",
        ),
    ),

    (
        "security_incident",
        (
            "data breach",
            "breach",
            "cyberattack",
            "cyber attack",
            "hacked",
            "hack",
            "security incident",
            "data leak",
            "leaked data",
            "vulnerability",
            "ransomware",
            "compromised",
            "fraud attack",
        ),
    ),

    (
        "outage",
        (
            "outage",
            "service outage",
            "system outage",
            "downtime",
            "service disruption",
            "service unavailable",
            "technical issue",
            "technical incident",
            "system failure",
        ),
    ),

    (
        "fraud",
        (
            "fraud",
            "fraudulent",
            "scam",
            "money laundering",
            "aml failure",
            "financial crime",
            "stolen funds",
            "unauthorised transactions",
            "unauthorized transactions",
        ),
    ),

    (
        "leadership",
        (
            "appoints",
            "appointed",
            "appointment",
            "names new",
            "named ceo",
            "new ceo",
            "new cfo",
            "new coo",
            "new cto",
            "new cpo",
            "chief executive",
            "chief financial officer",
            "chief operating officer",
            "chief technology officer",
            "chief product officer",
            "joins as",
            "steps down",
            "stepping down",
            "resigns",
            "resigned",
            "resignation",
            "departs",
            "departure",
            "leaves company",
            "founder leaves",
            "co-founder leaves",
            "board appointment",
            "joins board",
            "chairman",
            "chairwoman",
            "chairperson",
        ),
    ),

    (
        "layoffs_restructuring",
        (
            "layoffs",
            "layoff",
            "lays off",
            "job cuts",
            "cuts jobs",
            "workforce reduction",
            "restructuring",
            "restructures",
            "redundancies",
            "redundancy",
            "cuts workforce",
            "cost cutting",
        ),
    ),

    (
        "closure_failure",
        (
            "shuts down",
            "shutdown",
            "shutting down",
            "closes",
            "closure",
            "winds down",
            "winding down",
            "insolvency",
            "insolvent",
            "bankruptcy",
            "bankrupt",
            "administration",
            "ceases operations",
            "exits market",
            "withdraws from",
            "discontinues",
        ),
    ),

    (
        "pricing",
        (
            "pricing change",
            "new pricing",
            "price increase",
            "price cut",
            "cuts prices",
            "reduces fees",
            "raises fees",
            "new fee",
            "fees increase",
            "fee increase",
            "subscription price",
        ),
    ),

    (
        "ipo_capital_markets",
        (
            "ipo",
            "initial public offering",
            "files to go public",
            "files for ipo",
            "public listing",
            "stock exchange listing",
            "direct listing",
            "secondary offering",
            "share sale",
        ),
    ),

    (
        "award",
        (
            "award",
            "awarded",
            "wins award",
            "named best",
            "recognised",
            "recognized",
            "ranking",
            "ranked",
            "top fintech",
        ),
    ),

    (
        "milestone",
        (
            "milestone",
            "surpasses",
            "surpassed",
            "reaches",
            "reached",
            "exceeds",
            "million customers",
            "million users",
            "billion",
            "transaction volume",
            "payment volume",
            "customers globally",
            "anniversary",
        ),
    ),

    (
        "certification",
        (
            "soc 2",
            "soc2",
            "iso 27001",
            "certification",
            "certified",
            "pci dss",
            "patent",
            "patented",
        ),
    ),

    (
        "company_update",
        (
            "announces",
            "announced",
            "announcement",
            "reveals",
            "revealed",
            "plans to",
            "strategy",
            "strategic update",
            "company update",
        ),
    ),
]


# Shorter set used in the Google News query itself.
# The large EVENT_RULES above are then used to filter the results locally.

SEARCH_EVENT_TERMS = (
    "launch",
    "launches",
    "partnership",
    "acquisition",
    "funding",
    "expansion",
    "CEO",
    "appoints",
    "resigns",
    "licence",
    "license",
    "approval",
    "fine",
    "investigation",
    "lawsuit",
    "breach",
    "outage",
    "fraud",
    "layoffs",
    "shutdown",
    "revenue",
    "profit",
    "IPO",
    "award",
    "milestone",
)


FINTECH_CONTEXT_TERMS = (
    "fintech",
    "payments",
    "banking",
    "finance",
    "financial",
    "cards",
    "treasury",
    "expense",
    "money",
    "merchant",
    "crypto",
    "stablecoin",
)

ROOT = Path(__file__).resolve().parent

DEFAULT_SOURCES = ROOT / "sources_verified.json"
DEFAULT_STATE = ROOT / "state.json"
DEFAULT_ARCHIVE = ROOT / "news_archive.json"
DEFAULT_DEBUG = ROOT / "monitor-debug.json"
DEFAULT_WEEKLY = ROOT / "latest-week-news.md"

USER_AGENT = (
    "Mozilla/5.0 (compatible; OfficialNewsMonitor/3.0; "
    "+https://github.com/)"
)

CONNECT_TIMEOUT = 5
READ_TIMEOUT = 12
DEFAULT_WORKERS = 24

TRACKING_PARAMS = {
    "fbclid",
    "gclid",
    "mc_cid",
    "mc_eid",
    "ref",
    "source",
}

IGNORE_SEGMENTS = {
    "privacy",
    "terms",
    "terms-conditions",
    "login",
    "signin",
    "sign-in",
    "signup",
    "sign-up",
    "register",
    "contact",
    "careers",
    "career",
    "jobs",
    "job",
    "pricing",
    "support",
    "help",
    "documentation",
    "docs",
    "accounts",
    "account",
    "customer-service",
    "cookie-policy",
    "cookies",
    "legal",
}

GENERIC_LAST_SEGMENTS = {
    "",
    "news",
    "newsroom",
    "press",
    "media",
    "blog",
    "articles",
    "updates",
    "insights",
    "stories",
    "stocks",
    "crypto",
    "trading",
    "markets",
    "analysis",
    "latest",
    "home",
    "all",
}

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

ARTICLE_SCHEMA_TYPES = {
    "article",
    "newsarticle",
    "blogposting",
    "report",
    "techarticle",
    "analysisnewsarticle",
}

TYPE_NAMES = {
    "product_release": "Product release",
    "partnership": "Partnership",
    "funding": "Funding",
    "acquisition": "M&A",
    "expansion": "Expansion",
    "regulatory": "Regulatory",
    "leadership": "Leadership",
    "api_update": "API / platform update",
    "pricing": "Pricing",
    "company_update": "Company update",
}


# ---------------------------------------------------------------------------
# General helpers
# ---------------------------------------------------------------------------

def load_json(path: Path, fallback):
    if not path.exists():
        return fallback

    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception as error:
        logging.error("Could not read %s: %s", path, error)
        return fallback


def write_json(path: Path, value):
    path.write_text(
        json.dumps(value, indent=2, ensure_ascii=False) + "\n",
        encoding="utf-8",
    )


def normalized_host(url: str) -> str:
    host = (urlparse(url).hostname or "").lower()

    if host.startswith("www."):
        host = host[4:]

    return host


def clean_url(url: str) -> str:
    """
    Remove fragments and tracking parameters.

    Unlike the old version, this DOES NOT remove every query parameter,
    because some websites use query parameters to identify articles.
    """

    parsed = urlparse(url)

    params = []

    for key, value in parse_qsl(parsed.query, keep_blank_values=True):
        key_lower = key.lower()

        if key_lower.startswith("utm_"):
            continue

        if key_lower in TRACKING_PARAMS:
            continue

        params.append((key, value))

    path = parsed.path or "/"

    if path != "/":
        path = path.rstrip("/")

    return urlunparse(
        (
            parsed.scheme.lower(),
            parsed.netloc.lower(),
            path,
            "",
            urlencode(params),
            "",
        )
    )


def path_segments(url: str):
    return [
        item.lower()
        for item in urlparse(url).path.strip("/").split("/")
        if item
    ]


def allowed_host(url: str, source: dict, final_url: str | None = None):
    host = normalized_host(url)

    permitted = {
        normalized_host(source["url"]),
    }

    if final_url:
        permitted.add(normalized_host(final_url))

    for item in source.get("allowed_hosts", []):
        permitted.add(
            normalized_host(
                item if "://" in item else f"https://{item}"
            )
        )

    return host in permitted


def ignored_url(url: str):
    segments = set(path_segments(url))

    if segments & IGNORE_SEGMENTS:
        return True

    path = urlparse(url).path.lower()

    return path.endswith(
        (
            ".jpg",
            ".jpeg",
            ".png",
            ".gif",
            ".svg",
            ".webp",
            ".pdf",
            ".zip",
            ".mp4",
            ".mp3",
        )
    )


def clean_title(value: str | None):
    if not value:
        return ""

    return " ".join(str(value).split()).strip()


def slug_to_title(url: str):
    path = urlparse(url).path.rstrip("/")

    if not path:
        return "Company update"

    slug = path.split("/")[-1]

    slug = re.sub(r"^\d+[-_]", "", slug)
    slug = re.sub(r"[-_]+", " ", slug)

    return slug.strip().capitalize() or "Company update"


# ---------------------------------------------------------------------------
# Dates
# ---------------------------------------------------------------------------

def parse_date(value: str | None):
    if not value:
        return None

    value = clean_title(value)

    try:
        dt = parsedate_to_datetime(value)

        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)

        return dt.astimezone(timezone.utc).isoformat()

    except Exception:
        pass

    try:
        normalized = value.replace("Z", "+00:00")

        dt = datetime.fromisoformat(normalized)

        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)

        return dt.astimezone(timezone.utc).isoformat()

    except Exception:
        pass

    # yyyy-mm-dd
    match = re.search(r"\b(\d{4}-\d{2}-\d{2})\b", value)

    if match:
        try:
            dt = datetime.fromisoformat(match.group(1))
            dt = dt.replace(tzinfo=timezone.utc)
            return dt.isoformat()
        except Exception:
            pass

    return None

ALL_EVENT_KEYWORDS = tuple(
    keyword.lower()
    for _, keywords in EVENT_RULES
    for keyword in keywords
)


def contains_company_event(text: str) -> bool:
    text = text.lower()
    return any(keyword in text for keyword in ALL_EVENT_KEYWORDS)
    

def strip_html(value: str | None) -> str:
    if not value:
        return ""

    return BeautifulSoup(
        value,
        "html.parser",
    ).get_text(" ", strip=True)


def external_search_names(source: dict):
    names = []

    primary = source.get(
        "external_search_name"
    ) or source.get("company")

    if primary:
        names.append(primary)

    names.extend(
        source.get("search_aliases", [])
    )

    return list(dict.fromkeys(names))


def build_external_query(source: dict, days: int):
    names = external_search_names(source)

    if not names:
        return None

    company_part = " OR ".join(
        f'"{name}"'
        for name in names
    )

    event_part = " OR ".join(
        SEARCH_EVENT_TERMS
    )

    context_terms = list(
        FINTECH_CONTEXT_TERMS
    )

    context_terms.extend(
        source.get(
            "search_context",
            [],
        )
    )

    context_part = " OR ".join(
        f'"{term}"'
        if " " in term
        else term
        for term in dict.fromkeys(context_terms)
    )

    return (
        f"({company_part}) "
        f"({event_part}) "
        f"({context_part}) "
        f"when:{days}d"
    )

def build_external_targets(raw_sources):
    targets = {}

    for item in raw_sources:
        company = item.get("company")

        if not company:
            continue

        if item.get(
            "external_news",
            True,
        ) is False:
            continue

        target = {
            "company": company,
            "category": item.get(
                "category",
                "Other",
            ),
            "subcategory": item.get(
                "subcategory",
                "Other",
            ),
            "external_search_name": (
                item.get(
                    "external_search_name"
                )
            ),
            "search_aliases": (
                item.get(
                    "search_aliases",
                    []
                )
            ),
            "search_context": (
                item.get(
                    "search_context",
                    []
                )
            ),
        }

        targets[company] = target

    return list(
        targets.values()
    )
    
def parse_google_news_feed(
    xml_text: str,
    source: dict,
):
    items = []

    try:
        root = ET.fromstring(xml_text)
    except Exception:
        return items

    for node in root.iter():
        if local_name(node.tag) != "item":
            continue

        title = child_text(
            node,
            {"title"},
        )

        link = child_text(
            node,
            {"link"},
        )

        pub_date = child_text(
            node,
            {
                "pubdate",
                "published",
                "date",
            },
        )

        description = child_text(
            node,
            {"description"},
        )

        publisher = child_text(
            node,
            {"source"},
        )

        if not title or not link:
            continue

        title = clean_title(title)

        # Google News commonly formats titles:
        # "Ramp launches in UK - PR Newswire"
        if publisher:
            suffix = f" - {publisher}"

            if title.endswith(suffix):
                title = title[
                    :-len(suffix)
                ].strip()

        description_text = strip_html(
            description
        )

        relevance_text = (
            f"{title} {description_text}"
        )

        if not contains_company_event(
            relevance_text
        ):
            continue

        update_type = classify(
            title,
            description_text,
        )

        items.append(
            {
                "company": source.get(
                    "company",
                    "Unknown",
                ),
                "category": source.get(
                    "category",
                    "Other",
                ),
                "subcategory": source.get(
                    "subcategory",
                    "Other",
                ),
                "title": title,
                "url": clean_url(link),
                "published_at": parse_date(
                    pub_date
                ),
                "source_type": (
                    "external_news"
                ),
                "source_url": (
                    GOOGLE_NEWS_RSS
                ),
                "external_publisher": (
                    publisher
                ),
                "method": (
                    "external_news"
                ),
                "update_type": (
                    update_type
                ),
            }
        )

    return items[
        :EXTERNAL_NEWS_MAX_RESULTS
    ]


def collect_external_news(
    source: dict,
    days: int = EXTERNAL_NEWS_DAYS,
):
    company = source.get(
        "company",
        "Unknown",
    )

    query = build_external_query(
        source,
        days,
    )

    diagnostic = {
        "company": company,
        "source_type": "external_news",
        "query": query,
    }

    if not query:
        diagnostic["error"] = (
            "No company search name"
        )

        return [], diagnostic, None

    try:
        response = requests.get(
            GOOGLE_NEWS_RSS,
            params={
                "q": query,
                "hl": "en-GB",
                "gl": "GB",
                "ceid": "GB:en",
            },
            headers={
                "User-Agent": USER_AGENT,
            },
            timeout=(
                CONNECT_TIMEOUT,
                READ_TIMEOUT,
            ),
        )

        response.raise_for_status()

        items = parse_google_news_feed(
            response.text,
            source,
        )

        diagnostic.update(
            {
                "status": response.status_code,
                "results_found": len(items),
                "final_url": response.url,
            }
        )

        return items, diagnostic, None

    except Exception as error:
        diagnostic["error"] = (
            f"{type(error).__name__}: "
            f"{error}"
        )

        return [], diagnostic, error
        
# ---------------------------------------------------------------------------
# Classification
# ---------------------------------------------------------------------------

def classify(title: str, description: str = ""):
    text = f"{title} {description}".lower()

    for update_type, keywords in EVENT_RULES:
        if any(keyword.lower() in text for keyword in keywords):
            return update_type

    return "company_update"


# ---------------------------------------------------------------------------
# Source format
# ---------------------------------------------------------------------------

def flatten_sources(raw_sources):
    """
    Supports BOTH formats.

    Existing format:

    {
      "company": "Wise",
      "url": "https://newsroom.wise.com/",
      "category": "...",
      "subcategory": "..."
    }

    New multi-source format:

    {
      "company": "Wise",
      "category": "...",
      "subcategory": "...",
      "sources": [
        {
          "type": "newsroom",
          "url": "https://newsroom.wise.com/"
        },
        {
          "type": "blog",
          "url": "https://wise.com/gb/blog/"
        }
      ]
    }
    """

    output = []

    for company in raw_sources:
        nested = company.get("sources")

        if not nested:
            source = dict(company)
            source.setdefault("type", "newsroom")
            source.setdefault("strategy", "auto")

            if source.get("url"):
                output.append(source)

            continue

        for child in nested:
            source = {
                "company": company.get("company", "Unknown"),
                "category": company.get("category", "Other"),
                "subcategory": company.get("subcategory", "Other"),
            }

            source.update(child)

            source.setdefault("type", "newsroom")
            source.setdefault("strategy", "auto")

            if source.get("url"):
                output.append(source)

    return output


# ---------------------------------------------------------------------------
# RSS / Atom
# ---------------------------------------------------------------------------

def local_name(tag: str):
    return tag.split("}")[-1].lower()


def child_text(element, names):
    names = {name.lower() for name in names}

    for child in element.iter():
        if local_name(child.tag) in names:
            if child.text:
                return clean_title(child.text)

    return None


def extract_feed(xml_text: str, feed_url: str):
    results = []

    try:
        root = ET.fromstring(xml_text)
    except Exception:
        return results

    root_name = local_name(root.tag)

    # RSS
    if root_name in {"rss", "rdf"}:
        for item in root.iter():
            if local_name(item.tag) != "item":
                continue

            title = child_text(item, {"title"})
            link = child_text(item, {"link"})
            published = child_text(
                item,
                {
                    "pubdate",
                    "published",
                    "date",
                    "updated",
                },
            )

            if not link:
                continue

            results.append(
                {
                    "title": title or slug_to_title(link),
                    "url": clean_url(urljoin(feed_url, link)),
                    "published_at": parse_date(published),
                    "method": "rss",
                }
            )

    # Atom
    elif root_name == "feed":
        for entry in root.iter():
            if local_name(entry.tag) != "entry":
                continue

            title = child_text(entry, {"title"})
            published = child_text(
                entry,
                {
                    "published",
                    "updated",
                },
            )

            link = None

            for child in entry:
                if local_name(child.tag) != "link":
                    continue

                href = child.attrib.get("href")
                rel = child.attrib.get("rel", "alternate")

                if href and rel in {"alternate", ""}:
                    link = href
                    break

            if not link:
                continue

            results.append(
                {
                    "title": title or slug_to_title(link),
                    "url": clean_url(urljoin(feed_url, link)),
                    "published_at": parse_date(published),
                    "method": "atom",
                }
            )

    return results


def discover_feeds(soup: BeautifulSoup, page_url: str):
    feeds = []

    for link in soup.find_all("link", href=True):
        rel = {
            str(item).lower()
            for item in link.get("rel", [])
        }

        content_type = str(link.get("type", "")).lower()

        if (
            "alternate" in rel
            and (
                "rss" in content_type
                or "atom" in content_type
                or "feed" in content_type
            )
        ):
            feeds.append(
                clean_url(urljoin(page_url, link["href"]))
            )

    return list(dict.fromkeys(feeds))


# ---------------------------------------------------------------------------
# JSON-LD structured articles
# ---------------------------------------------------------------------------

def walk_json(value):
    if isinstance(value, dict):
        yield value

        for child in value.values():
            yield from walk_json(child)

    elif isinstance(value, list):
        for child in value:
            yield from walk_json(child)


def jsonld_url(item):
    value = item.get("url")

    if isinstance(value, str):
        return value

    main = item.get("mainEntityOfPage")

    if isinstance(main, str):
        return main

    if isinstance(main, dict):
        return main.get("@id") or main.get("url")

    return None


def extract_jsonld(soup: BeautifulSoup, page_url: str):
    results = []

    for script in soup.find_all(
        "script",
        attrs={"type": "application/ld+json"},
    ):
        raw = script.string or script.get_text()

        if not raw.strip():
            continue

        try:
            data = json.loads(raw)
        except Exception:
            continue

        for item in walk_json(data):
            schema_type = item.get("@type", "")

            if isinstance(schema_type, list):
                types = {
                    str(x).lower()
                    for x in schema_type
                }
            else:
                types = {str(schema_type).lower()}

            if not types & ARTICLE_SCHEMA_TYPES:
                continue

            url = jsonld_url(item)

            if not url:
                continue

            title = (
                item.get("headline")
                or item.get("name")
                or slug_to_title(url)
            )

            published = (
                item.get("datePublished")
                or item.get("dateModified")
            )

            results.append(
                {
                    "title": clean_title(title),
                    "url": clean_url(urljoin(page_url, url)),
                    "published_at": parse_date(published),
                    "method": "jsonld",
                }
            )

    return results


# ---------------------------------------------------------------------------
# HTML article extraction
# ---------------------------------------------------------------------------

def title_for_anchor(anchor):
    text = clean_title(anchor.get_text(" ", strip=True))

    if (
        text
        and text.lower() not in GENERIC_TITLES
        and len(text) >= 10
    ):
        return text

    for attribute in ("aria-label", "title"):
        value = clean_title(anchor.get(attribute))

        if (
            value
            and value.lower() not in GENERIC_TITLES
            and len(value) >= 10
        ):
            return value

    parent = anchor

    for _ in range(4):
        parent = parent.parent

        if not parent:
            break

        heading = parent.find(
            ["h1", "h2", "h3", "h4", "h5"]
        )

        if heading:
            value = clean_title(
                heading.get_text(" ", strip=True)
            )

            if len(value) >= 10:
                return value

    return text


def date_from_container(container):
    if not container:
        return None

    time_element = container.find("time")

    if time_element:
        value = (
            time_element.get("datetime")
            or time_element.get_text(" ", strip=True)
        )

        parsed = parse_date(value)

        if parsed:
            return parsed

    return None


def extract_article_elements(
    soup: BeautifulSoup,
    page_url: str,
):
    results = []

    containers = list(soup.find_all("article"))

    # Heading links are often the cleanest fallback.
    heading_links = soup.select(
        "main h1 a[href], "
        "main h2 a[href], "
        "main h3 a[href], "
        "h2 a[href], "
        "h3 a[href]"
    )

    for container in containers:
        anchors = container.find_all("a", href=True)

        if not anchors:
            continue

        # Prefer the anchor with the longest meaningful title.
        best = None
        best_title = ""

        for anchor in anchors:
            title = title_for_anchor(anchor)

            if len(title) > len(best_title):
                best = anchor
                best_title = title

        if not best or len(best_title) < 10:
            continue

        url = clean_url(
            urljoin(page_url, best["href"])
        )

        results.append(
            {
                "title": best_title,
                "url": url,
                "published_at": date_from_container(container),
                "method": "article_html",
            }
        )

    for anchor in heading_links:
        title = title_for_anchor(anchor)

        if len(title) < 10:
            continue

        results.append(
            {
                "title": title,
                "url": clean_url(
                    urljoin(page_url, anchor["href"])
                ),
                "published_at": date_from_container(
                    anchor.parent
                ),
                "method": "headline_html",
            }
        )

    return results


# ---------------------------------------------------------------------------
# Scoped HTML fallback
# ---------------------------------------------------------------------------

def likely_article_path(url: str, prefix: str):
    parsed = urlparse(url)

    path = parsed.path.rstrip("/").lower()
    segments = [
        x
        for x in path.strip("/").split("/")
        if x
    ]

    if not segments:
        return False

    last = segments[-1]

    if last in GENERIC_LAST_SEGMENTS:
        return False

    if prefix:
        normalized_prefix = prefix.rstrip("/").lower()

        if path == normalized_prefix:
            return False

        if not path.startswith(normalized_prefix + "/"):
            return False

        # Prevent section landing pages such as:
        # /news-and-analysis/stocks
        if len(path.split("/")) <= len(
            normalized_prefix.split("/")
        ) + 1:
            if len(last) < 18 and not re.search(
                r"\d",
                last,
            ):
                return False

        return True

    # Root newsroom:
    # require a more article-looking URL.
    if re.search(r"\d{4,}", path):
        return True

    article_words = (
        "news",
        "press",
        "release",
        "article",
        "story",
        "update",
        "insight",
        "announcement",
    )

    if any(word in path for word in article_words):
        return len(segments) >= 2

    return len(segments) >= 2 and len(last) >= 20


def extract_scoped_links(
    soup: BeautifulSoup,
    page_url: str,
    source: dict,
):
    results = []

    configured_prefix = source.get(
        "article_path_prefix"
    )

    if configured_prefix is None:
        path = urlparse(page_url).path.rstrip("/")
        configured_prefix = "" if path == "/" else path

    for anchor in soup.find_all("a", href=True):
        href = anchor.get("href", "").strip()

        if not href:
            continue

        if href.startswith(
            (
                "#",
                "mailto:",
                "tel:",
                "javascript:",
            )
        ):
            continue

        url = clean_url(urljoin(page_url, href))

        if not allowed_host(url, source, page_url):
            continue

        if ignored_url(url):
            continue

        if not likely_article_path(
            url,
            configured_prefix,
        ):
            continue

        title = title_for_anchor(anchor)

        if len(title) < 10:
            continue

        results.append(
            {
                "title": title,
                "url": url,
                "published_at": date_from_container(
                    anchor.parent
                ),
                "method": "scoped_html",
            }
        )

    return results


# ---------------------------------------------------------------------------
# Sitemap support
# ---------------------------------------------------------------------------

def extract_sitemap(xml_text: str, sitemap_url: str):
    results = []

    try:
        root = ET.fromstring(xml_text)
    except Exception:
        return results, []

    root_type = local_name(root.tag)

    # Sitemap index
    if root_type == "sitemapindex":
        child_sitemaps = []

        for node in root:
            if local_name(node.tag) != "sitemap":
                continue

            loc = child_text(node, {"loc"})

            if loc:
                child_sitemaps.append(
                    urljoin(sitemap_url, loc)
                )

        return [], child_sitemaps

    if root_type != "urlset":
        return [], []

    for node in root:
        if local_name(node.tag) != "url":
            continue

        loc = child_text(node, {"loc"})

        if not loc:
            continue

        lastmod = child_text(node, {"lastmod"})

        results.append(
            {
                "title": slug_to_title(loc),
                "url": clean_url(loc),
                "published_at": parse_date(lastmod),
                "method": "sitemap",
            }
        )

    return results, []


# ---------------------------------------------------------------------------
# Deduplication
# ---------------------------------------------------------------------------

METHOD_PRIORITY = {
    "rss": 10,
    "atom": 10,
    "jsonld": 9,
    "article_html": 8,
    "headline_html": 7,
    "scoped_html": 5,
    "sitemap": 2,
}


def merge_items(items):
    by_url = {}

    for item in items:
        url = item.get("url")

        if not url:
            continue

        url = clean_url(url)
        item["url"] = url

        current = by_url.get(url)

        if not current:
            by_url[url] = item
            continue

        current_priority = METHOD_PRIORITY.get(
            current.get("method"),
            0,
        )

        new_priority = METHOD_PRIORITY.get(
            item.get("method"),
            0,
        )

        if new_priority > current_priority:
            better = dict(item)

            if not better.get("published_at"):
                better["published_at"] = current.get(
                    "published_at"
                )

            by_url[url] = better

        elif (
            not current.get("published_at")
            and item.get("published_at")
        ):
            current["published_at"] = item[
                "published_at"
            ]

    return list(by_url.values())


# ---------------------------------------------------------------------------
# HTTP collection
# ---------------------------------------------------------------------------

def request(url: str):
    return requests.get(
        url,
        headers={
            "User-Agent": USER_AGENT,
            "Accept": (
                "text/html,application/xhtml+xml,"
                "application/xml,text/xml;q=0.9,*/*;q=0.8"
            ),
            "Accept-Language": "en-GB,en;q=0.9",
        },
        timeout=(CONNECT_TIMEOUT, READ_TIMEOUT),
        allow_redirects=True,
    )


def collect_source(source: dict):
    company = source.get("company", "Unknown")
    url = source.get("url")
    strategy = source.get("strategy", "auto").lower()

    diagnostic = {
        "company": company,
        "source_type": source.get("type", "newsroom"),
        "configured_url": url,
        "strategy": strategy,
    }

    try:
        response = request(url)
        response.raise_for_status()

        diagnostic["status"] = response.status_code
        diagnostic["final_url"] = response.url
        diagnostic["html_bytes"] = len(response.content)

        items = []

        content_type = response.headers.get(
            "Content-Type",
            "",
        ).lower()

        body_start = response.text.lstrip()[:200].lower()

        looks_xml = (
            "xml" in content_type
            or body_start.startswith("<?xml")
            or body_start.startswith("<rss")
            or "<feed" in body_start
        )

        # Explicit RSS
        if strategy in {"rss", "feed", "atom"}:
            items.extend(
                extract_feed(
                    response.text,
                    response.url,
                )
            )

        # Explicit sitemap
        elif strategy == "sitemap":
            sitemap_items, child_maps = extract_sitemap(
                response.text,
                response.url,
            )

            items.extend(sitemap_items)

            # Limit sitemap-index expansion.
            for child in child_maps[:10]:
                try:
                    child_response = request(child)
                    child_response.raise_for_status()

                    child_items, _ = extract_sitemap(
                        child_response.text,
                        child_response.url,
                    )

                    items.extend(child_items)

                except Exception:
                    continue

        # Auto-detect XML
        elif looks_xml:
            feed_items = extract_feed(
                response.text,
                response.url,
            )

            if feed_items:
                items.extend(feed_items)
            else:
                sitemap_items, _ = extract_sitemap(
                    response.text,
                    response.url,
                )
                items.extend(sitemap_items)

        # Normal webpage
        else:
            soup = BeautifulSoup(
                response.text,
                "html.parser",
            )

            # 1. Discover RSS / Atom feeds.
            feed_urls = discover_feeds(
                soup,
                response.url,
            )

            for feed_url in feed_urls[:3]:
                try:
                    feed_response = request(feed_url)
                    feed_response.raise_for_status()

                    items.extend(
                        extract_feed(
                            feed_response.text,
                            feed_response.url,
                        )
                    )

                except Exception:
                    pass

            # 2. Structured article metadata.
            items.extend(
                extract_jsonld(
                    soup,
                    response.url,
                )
            )

            # 3. Article cards / headline links.
            items.extend(
                extract_article_elements(
                    soup,
                    response.url,
                )
            )

            # 4. Scoped fallback.
            items.extend(
                extract_scoped_links(
                    soup,
                    response.url,
                    source,
                )
            )

            # 5. Optional sitemap.
            sitemap_url = source.get("sitemap_url")

            if sitemap_url:
                try:
                    sitemap_response = request(
                        sitemap_url
                    )
                    sitemap_response.raise_for_status()

                    sitemap_items, _ = extract_sitemap(
                        sitemap_response.text,
                        sitemap_response.url,
                    )

                    items.extend(sitemap_items)

                except Exception as error:
                    logging.warning(
                        "%s sitemap failed: %s",
                        company,
                        error,
                    )

        # Final filtering.
        filtered = []

        for item in merge_items(items):
            article_url = item["url"]

            if ignored_url(article_url):
                continue

            if not allowed_host(
                article_url,
                source,
                response.url,
            ):
                continue

            item["company"] = company
            item["category"] = source.get(
                "category",
                "Other",
            )
            item["subcategory"] = source.get(
                "subcategory",
                "Other",
            )
            item["source_type"] = source.get(
                "type",
                "newsroom",
            )
            item["source_url"] = url
            item["update_type"] = classify(
                item["title"]
            )

            filtered.append(item)

        diagnostic["links_found"] = len(filtered)

        methods = {}

        for item in filtered:
            method = item.get("method", "unknown")
            methods[method] = methods.get(method, 0) + 1

        diagnostic["methods"] = methods

        return filtered, diagnostic, None

    except Exception as error:
        diagnostic["error"] = (
            f"{type(error).__name__}: {error}"
        )

        return [], diagnostic, error


# ---------------------------------------------------------------------------
# Archive
# ---------------------------------------------------------------------------

def load_archive(path: Path):
    value = load_json(
        path,
        {"articles": []},
    )

    if isinstance(value, list):
        return value

    return value.get("articles", [])


def merge_archive(
    existing,
    discovered,
    now_iso,
):
    by_url = {
        item["url"]: item
        for item in existing
        if item.get("url")
    }

    for article in discovered:
        url = article["url"]

        if url not in by_url:
            record = dict(article)
            record["first_seen_at"] = now_iso
            by_url[url] = record
            continue

        old = by_url[url]

        # Improve metadata over time.
        if (
            article.get("title")
            and (
                not old.get("title")
                or len(article["title"])
                > len(old["title"])
            )
        ):
            old["title"] = article["title"]

        if (
            not old.get("published_at")
            and article.get("published_at")
        ):
            old["published_at"] = article[
                "published_at"
            ]

        old["update_type"] = article.get(
            "update_type",
            old.get("update_type"),
        )

    return list(by_url.values())


# ---------------------------------------------------------------------------
# Reporting
# ---------------------------------------------------------------------------

def markdown_title(value: str):
    return value.replace("[", "\\[").replace("]", "\\]")


def render_markdown(articles):
    if not articles:
        return "No new official news found.\n"

    lines = [
        "# New official company updates",
        "",
    ]

    groups = {}

    for article in articles:
        key = (
            article.get("category", "Other"),
            article.get("subcategory", "Other"),
        )

        groups.setdefault(key, []).append(article)

    for (
        category,
        subcategory,
    ), group in sorted(groups.items()):

        lines.append(f"## {category}")
        lines.append("")
        lines.append(f"### {subcategory}")
        lines.append("")

        group.sort(
            key=lambda x: (
                x.get("company", ""),
                x.get("title", ""),
            )
        )

        for article in group:
            update_type = TYPE_NAMES.get(
                article.get("update_type"),
                "Company update",
            )

            title = markdown_title(
                article["title"]
            )

            lines.append(
                f"- **{article['company']}** "
                f"· {update_type}: "
                f"[{title}]({article['url']})"
            )

        lines.append("")

    return "\n".join(lines).rstrip() + "\n"


def article_report_date(article):
    value = article.get("published_at")

    if not value:
        return None

    try:
        return datetime.fromisoformat(
            value.replace("Z", "+00:00")
        )
    except Exception:
        return None


def write_weekly(path: Path, archive):
    cutoff = datetime.now(timezone.utc) - timedelta(
        days=9
    )

    articles = []

    for item in archive:
        dt = article_report_date(item)

        if dt and dt >= cutoff:
            articles.append(item)

    articles.sort(
        key=lambda x: (
            article_report_date(x)
            or datetime.min.replace(
                tzinfo=timezone.utc
            )
        ),
        reverse=True,
    )

    if not articles:
        path.write_text(
            "# Official company updates — last 7 days\n\n"
            "No updates found.\n",
            encoding="utf-8",
        )
        return

    body = render_markdown(articles)

    body = body.replace(
        "# New official company updates",
        "# Official company updates — last 7 days",
        1,
    )

    path.write_text(
        body,
        encoding="utf-8",
    )


def recent_enough(article, max_age_days):
    published = article.get("published_at")

    # Unknown publication date:
    # remember it, but don't report it as recent news.
    if not published:
        return False

    try:
        published_dt = datetime.fromisoformat(
            published.replace("Z", "+00:00")
        )
    except Exception:
        return False

    cutoff = datetime.now(timezone.utc) - timedelta(
        days=max_age_days
    )

    return published_dt >= cutoff


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main():
    parser = argparse.ArgumentParser()

    parser.add_argument(
        "--sources",
        type=Path,
        default=DEFAULT_SOURCES,
    )

    parser.add_argument(
        "--state",
        type=Path,
        default=DEFAULT_STATE,
    )

    parser.add_argument(
        "--archive",
        type=Path,
        default=DEFAULT_ARCHIVE,
    )

    parser.add_argument(
        "--bootstrap",
        action="store_true",
    )

    parser.add_argument(
        "--format",
        choices=("markdown", "json"),
        default="markdown",
    )

    parser.add_argument(
        "--workers",
        type=int,
        default=DEFAULT_WORKERS,
    )

    parser.add_argument(
        "--max-age-days",
        type=int,
        default=14,
        help=(
            "Do not report newly discovered articles older "
            "than this, but still remember them."
        ),
    )

    args = parser.parse_args()

    logging.basicConfig(
        level=logging.INFO,
        format="%(levelname)s: %(message)s",
        stream=sys.stderr,
    )

    raw_sources = load_json(
        args.sources,
        [],
    )

    sources = flatten_sources(raw_sources)

    logging.info(
        "%d company sources loaded.",
        len(sources),
    )

    state = load_json(
        args.state,
        {"seen_urls": []},
    )

    seen_before = set(
        state.get("seen_urls", [])
    )

    discovered_by_url = {}
    diagnostics = []

    successful = 0
    failed = 0
    zero_results = 0

    with ThreadPoolExecutor(
        max_workers=args.workers
    ) as pool:

        future_map = {
            pool.submit(
                collect_source,
                source,
            ): source
            for source in sources
        }

        for future in as_completed(future_map):
            source = future_map[future]

            articles, diagnostic, error = (
                future.result()
            )

            diagnostics.append(diagnostic)

            company = source.get(
                "company",
                "Unknown",
            )

            if error:
                failed += 1

                logging.error(
                    "%s failed: %s",
                    company,
                    error,
                )

                continue

            successful += 1

            if not articles:
                zero_results += 1

                logging.warning(
                    "%s returned 0 update links",
                    company,
                )

            for article in articles:
                url = article["url"]

                existing = discovered_by_url.get(
                    url
                )

                if not existing:
                    discovered_by_url[url] = article

                else:
                    current_priority = (
                        METHOD_PRIORITY.get(
                            existing.get("method"),
                            0,
                        )
                    )

                    new_priority = (
                        METHOD_PRIORITY.get(
                            article.get("method"),
                            0,
                        )
                    )

                    if new_priority > current_priority:
                        discovered_by_url[url] = (
                            article
                        )
        # -------------------------------------------------------
    # External web/news discovery
    # -------------------------------------------------------

    external_targets = build_external_targets(
        raw_sources
    )

    external_successful = 0
    external_failed = 0
    external_articles_found = 0

    logging.info(
        "Running external news discovery for %d companies.",
        len(external_targets),
    )

    with ThreadPoolExecutor(
        max_workers=EXTERNAL_NEWS_WORKERS
    ) as pool:

        futures = {
            pool.submit(
                collect_external_news,
                target,
                EXTERNAL_NEWS_DAYS,
            ): target
            for target in external_targets
        }

        for future in as_completed(
            futures
        ):
            target = futures[future]

            articles, diagnostic, error = (
                future.result()
            )

            diagnostics.append(
                diagnostic
            )

            company = target.get(
                "company",
                "Unknown",
            )

            if error:
                external_failed += 1

                logging.warning(
                    "%s external discovery failed: %s",
                    company,
                    error,
                )

                continue

            external_successful += 1
            external_articles_found += len(
                articles
            )

            if articles:
                logging.info(
                    "%s: %d external event(s)",
                    company,
                    len(articles),
                )

            for article in articles:
                url = article["url"]

                existing = (
                    discovered_by_url.get(
                        url
                    )
                )

                if not existing:
                    discovered_by_url[
                        url
                    ] = article
    discovered = list(
        discovered_by_url.values()
    )

    new_articles = [
        article
        for article in discovered
        if article["url"] not in seen_before
    ]

    # Remember old articles too, but don't present ancient content
    # as "new" just because this scraper discovered it today.
    reportable_new = [
        article
        for article in new_articles
        if recent_enough(
            article,
            args.max_age_days,
        )
    ]

    seen_after = seen_before | {
        article["url"]
        for article in discovered
    }

    now_iso = datetime.now(
        timezone.utc
    ).isoformat()

    write_json(
        args.state,
        {
            "seen_urls": sorted(seen_after),
            "last_run": now_iso,
        },
    )

    archive = load_archive(
        args.archive
    )

    archive = merge_archive(
        archive,
        discovered,
        now_iso,
    )

    write_json(
        args.archive,
        {
            "articles": sorted(
                archive,
                key=lambda x: (
                    x.get("first_seen_at", "")
                ),
                reverse=True,
            )
        },
    )

    write_weekly(
        DEFAULT_WEEKLY,
        archive,
    )

    write_json(
        DEFAULT_DEBUG,
        {
            "run_at": now_iso,
            "configured_sources": len(sources),
            "successful_sources": successful,
            "failed_sources": failed,
            "zero_result_sources": zero_results,
            "candidate_articles": len(discovered),
            "new_urls": len(new_articles),
            "new_reported": len(reportable_new),
            "sources": sorted(
                diagnostics,
                key=lambda x: (
                    x.get("company", "")
                ).lower(),
            ),
        },
    )

    logging.info(
        (
            "SUMMARY: %d sources | %d successful | "
            "%d failed | %d zero-result | "
            "%d candidate articles | %d new | "
            "%d reported"
        ),
        len(sources),
        successful,
        failed,
        zero_results,
        len(discovered),
        len(new_articles),
        len(reportable_new),
    )

    if args.bootstrap:
        logging.info(
            "Bootstrap complete. %d URLs stored.",
            len(seen_after),
        )
        return

    if args.format == "json":
        print(
            json.dumps(
                reportable_new,
                indent=2,
                ensure_ascii=False,
            )
        )
        return

    print(
        render_markdown(
            reportable_new
        ),
        end="",
    )


if __name__ == "__main__":
    main()
