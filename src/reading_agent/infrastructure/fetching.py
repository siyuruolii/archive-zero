"""有界 RSS/Atom 与静态 HTML 获取；不执行网页脚本，不承诺全部历史。"""

import hashlib
import xml.etree.ElementTree as ET
from dataclasses import dataclass
from html import unescape
from html.parser import HTMLParser
from urllib.parse import urljoin, urlsplit, urlunsplit

import httpx


def normalized_url(url):
    parts = urlsplit(url.strip())
    if (
        parts.scheme not in {"https", "http"}
        or not parts.hostname
        or parts.username
        or parts.password
    ):
        raise ValueError("only HTTP(S) URLs without credentials are accepted")
    return urlunsplit(
        (parts.scheme.lower(), parts.netloc.lower(), parts.path or "/", parts.query, "")
    )


class PlainText(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.parts = []
        self.hidden = 0

    def handle_starttag(self, tag, attrs):
        if tag in {"script", "style", "noscript"}:
            self.hidden += 1

    def handle_endtag(self, tag):
        if tag in {"script", "style", "noscript"} and self.hidden:
            self.hidden -= 1

    def handle_data(self, data):
        if not self.hidden and data.strip():
            self.parts.append(data.strip())


def plain(html):
    parser = PlainText()
    parser.feed(html)
    return "\n".join(parser.parts)


@dataclass(frozen=True)
class FeedItem:
    item_key: str
    title: str
    url: str
    summary: str
    published_at: str | None


def parse_feed(content, base_url):
    # XML 实体/DTD 不属于支持范围，直接拒绝；输入已有字节大小限制。
    if b"<!DOCTYPE" in content.upper() or b"<!ENTITY" in content.upper():
        raise ValueError("DTD and entities are not supported")
    root = ET.fromstring(content)
    items = []
    nodes = (
        root.findall(".//item")
        if root.tag != "{http://www.w3.org/2005/Atom}feed"
        else list(root.findall("{*}entry"))
    )
    if not nodes and root.tag not in {
        "rss",
        "{http://www.w3.org/2005/Atom}feed",
        "{http://www.w3.org/1999/02/22-rdf-syntax-ns#}RDF",
    }:
        raise ValueError("not an RSS/Atom feed")
    for node in nodes[:200]:

        def value(*names, node=node):
            for name in names:
                child = node.find("{*}" + name)
                if child is not None and child.text:
                    return child.text.strip()
            return ""

        link = value("link")
        if not link:
            links = node.findall("{*}link")
            link = next(
                (x.get("href", "") for x in links if x.get("rel", "alternate") == "alternate"), ""
            )
        url = normalized_url(urljoin(base_url, link)) if link else ""
        title = unescape(value("title"))[:1000] or "无标题"
        summary = plain(value("description", "summary", "encoded", "content"))[:12000]
        published = value("pubDate", "published", "updated", "date") or None
        key = (
            value("guid", "id")
            or url
            or hashlib.sha256((title + str(published) + summary).encode()).hexdigest()
        )
        items.append(FeedItem(key, title, url, summary, published))
    return items


class Fetcher:
    def __init__(self, client: httpx.AsyncClient):
        self.client = client

    async def get(self, url, limit=2 * 1024 * 1024):
        url = normalized_url(url)
        async with self.client.stream("GET", url, follow_redirects=True, timeout=20) as response:
            response.raise_for_status()
            parts = []
            size = 0
            async for part in response.aiter_bytes():
                size += len(part)
                if size > limit:
                    raise ValueError("download exceeds size limit")
                parts.append(part)
            return b"".join(parts), str(response.url), response.encoding or "utf-8"

    async def feed(self, url):
        content, actual_url, _ = await self.get(url)
        return parse_feed(content, actual_url)

    async def body(self, url):
        content, _, encoding = await self.get(url)
        text = content.decode(encoding, errors="replace")
        return plain(text)[:200000]
