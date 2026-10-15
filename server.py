#!/usr/bin/env python3
"""Local prototype server for Ask The Times.

Proxies the NAPP search test on vi-pr-24320 and serves the prototype UI.
"""

import json
import re
import time
from concurrent.futures import ThreadPoolExecutor
from html import unescape
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.parse import parse_qs, urlencode, urlparse
from urllib.request import Request, urlopen

HOST = "127.0.0.1"
PORT = 8765
SEARCH_ORIGIN = "https://vi-pr-24320.dev.nytimes.com"
USER_AGENT = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
    "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/148.0.0.0 Safari/537.36"
)
CACHE_TTL_SECONDS = 120
CACHE = {}
BASE_DIR = Path(__file__).resolve().parent


def junescape(value):
    return json.loads('"' + value + '"')


def path_key(url):
    return urlparse(url).path


def pick_image(block):
    srcset = re.search(r'srcset="([^"]+)"', block)
    if srcset:
        candidates = []
        for part in unescape(srcset.group(1)).split(","):
            url = part.strip().split(" ")[0]
            if url.startswith("https://"):
                candidates.append(url)
        for token in ("mediumThreeByTwo440", "threeByTwoSmallAt2X", "threeByTwoMediumAt2X"):
            for url in candidates:
                if token in url:
                    return url
        if candidates:
            return candidates[0]
    src = re.search(r'<img[^>]+src="([^"]+)"', block)
    return unescape(src.group(1)) if src else ""


def strip_tags(value):
    return unescape(re.sub(r"<[^>]+>", "", value)).strip()


def parse_cards(page):
    cards = {}
    blocks = page.split('data-testid="search-bodega-result"')[1:]
    for index, block in enumerate(blocks, start=1):
        href = re.search(r'href="([^"]+)"', block)
        title = re.search(r'data-tpl="h"><a[^>]*>(.*?)</a>', block, re.S)
        summary = re.search(r'data-tpl="bo">(.*?)</div>', block, re.S)
        byline = re.search(r'data-tpl="ti">(.*?)</div>', block, re.S)
        kickers = re.findall(r'data-tpl="la">(.*?)</div>', block, re.S)
        if not href or not title:
            continue
        url = href.group(1)
        if url.startswith("/"):
            url = "https://www.nytimes.com" + url.split("?")[0]
        cards[path_key(url)] = {
            "title": strip_tags(title.group(1)),
            "url": url,
            "summary": strip_tags(summary.group(1)) if summary else "",
            "byline": strip_tags(byline.group(1)) if byline else "",
            "section": strip_tags(kickers[0]) if kickers else "",
            "image": pick_image(block),
            "position": index,
        }
    return cards


def parse_metadata(page):
    metadata = {}
    pattern = re.compile(
        r'"url":"(https:\\u002F\\u002Fwww\.nytimes\.com\\u002F[^"]+)"'
        r'(?:(?!"url":).){0,500}?"lastModified":"([^"]+)"'
        r'(?:(?!"url":).){0,200}?"firstPublished":"([^"]+)"'
        r'(?:(?!"url":).){0,900}?"promotionalHeadline":"((?:\\.|[^"\\])*)"',
        re.S,
    )
    for url, modified, published, title in pattern.findall(page):
        clean_url = junescape(url).split("?")[0]
        metadata[path_key(clean_url)] = {
            "url": clean_url,
            "title": junescape(title),
            "firstPublished": published,
            "lastModified": modified,
        }
    return metadata


def fetch_search(query):
    now = time.time()
    cached = CACHE.get(query)
    if cached and now - cached["at"] < CACHE_TTL_SECONDS:
        return cached["payload"]

    params = urlencode({"lang": "en", "query": query, "sort": "best"})
    url = f"{SEARCH_ORIGIN}/search?{params}"
    request = Request(url, headers={"User-Agent": USER_AGENT, "Accept": "text/html"})
    with urlopen(request, timeout=20) as response:
        page = response.read().decode("utf-8", "replace")

    cards = parse_cards(page)
    metadata = parse_metadata(page)
    articles = []
    for key, card in cards.items():
        meta = metadata.get(key, {})
        articles.append(
            {
                "title": card["title"],
                "url": meta.get("url") or card["url"],
                "summary": card.get("summary", ""),
                "byline": card.get("byline", ""),
                "section": card.get("section", ""),
                "image": card.get("image", ""),
                "position": card.get("position", 0),
                "firstPublished": meta.get("firstPublished", ""),
                "lastModified": meta.get("lastModified", ""),
            }
        )
    articles.sort(key=lambda article: article["position"] or 999)
    payload = {
        "query": query,
        "sourceUrl": url,
        "articles": articles,
    }
    CACHE[query] = {"at": now, "payload": payload}
    return payload


class Handler(BaseHTTPRequestHandler):
    def log_message(self, fmt, *args):
        print("[ask-the-times]", fmt % args)

    def send_json(self, status, payload):
        body = json.dumps(payload).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        parsed = urlparse(self.path)
        static_files = {
            "/": ("index.html", "text/html; charset=utf-8"),
            "/catalog.json": ("catalog.json", "application/json; charset=utf-8"),
        }
        if parsed.path in static_files:
            filename, content_type = static_files[parsed.path]
            body = (BASE_DIR / filename).read_bytes()
            self.send_response(200)
            self.send_header("Content-Type", content_type)
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(body)
            return

        if parsed.path != "/api/retrieve":
            self.send_error(404)
            return

        queries = [query.strip() for query in parse_qs(parsed.query).get("q", []) if query.strip()]
        queries = queries[:4]
        if not queries or any(len(query) > 180 for query in queries):
            self.send_json(400, {"error": "Provide one to four search queries."})
            return

        try:
            with ThreadPoolExecutor(max_workers=len(queries)) as pool:
                searches = list(pool.map(fetch_search, queries))
        except (HTTPError, URLError, TimeoutError) as error:
            self.send_json(502, {"error": f"NAPP search test is unavailable: {error}"})
            return

        self.send_json(
            200,
            {
                "now": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
                "rule": {
                    "minHigh": 1,
                    "minRelevant": 3,
                    "windowDays": 14,
                    "text": "Latest News fires when there is at least 1 HIGH-relevance chunk, or at least 3 HIGH/MEDIUM-relevance chunks, published or updated within the last 14 days.",
                    "classifier": "The LLM query-signals classifier did not run on the public test page. This prototype uses publication and update times from that page and does not invent HIGH or MEDIUM labels.",
                },
                "searches": searches,
            },
        )


if __name__ == "__main__":
    server = ThreadingHTTPServer((HOST, PORT), Handler)
    print(f"Ask The Times prototype at http://{HOST}:{PORT}")
    server.serve_forever()
