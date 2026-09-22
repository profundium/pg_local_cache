#!/usr/bin/env python3
"""Check the built Pages artifact, including URLs under the project base path."""
import argparse
from html.parser import HTMLParser
import json
from pathlib import Path
import re
from urllib.parse import unquote, urljoin, urlsplit
import xml.etree.ElementTree as ET

BASE = "https://profundium.github.io/pg_local_cache/"


class Page(HTMLParser):
    def __init__(self, text):
        super().__init__(convert_charrefs=True)
        self.ids, self.links, self.meta, self.copies = set(), [], {}, []
        self.anchors = []
        self.content_anchors = []
        self.lang = None
        self.alternates = {}
        self.feed_url = None
        self.h1 = 0
        self.canonical = None
        self.title = ""
        self.structured = []
        self.errors = []
        self.in_head = self.in_title = self.in_json = False
        self.json_text = ""
        self.feed(text)

    def handle_starttag(self, tag, attrs):
        attrs = dict(attrs)
        if tag == "html":
            self.lang = attrs.get("lang")
        if tag == "link" and attrs.get("rel") == "alternate":
            if "hreflang" in attrs:
                language = attrs["hreflang"]
                if language in self.alternates:
                    self.errors.append(f"duplicate hreflang: {language}")
                self.alternates[language] = attrs.get("href")
            if attrs.get("type") == "application/atom+xml":
                self.feed_url = attrs.get("href")
        if "id" in attrs:
            if attrs["id"] in self.ids:
                self.errors.append(f"duplicate id: {attrs['id']}")
            self.ids.add(attrs["id"])
        if tag == "h1":
            self.h1 += 1
        if tag == "head":
            self.in_head = True
        if tag == "title" and self.in_head:
            self.in_title = True
        if tag == "meta":
            self.meta[attrs.get("name", attrs.get("property"))] = attrs.get("content", "")
        if tag == "link" and attrs.get("rel") == "canonical":
            if self.canonical is not None:
                self.errors.append("duplicate canonical")
            self.canonical = attrs.get("href")
        if "href" in attrs:
            self.links.append(attrs["href"])
            if tag == "a":
                self.anchors.append(attrs["href"])
                if not attrs.get("hreflang"):
                    self.content_anchors.append(attrs["href"])
        if "src" in attrs:
            self.links.append(attrs["src"])
        if "data-copy" in attrs:
            self.copies.append(attrs["data-copy"])
        if tag == "script" and attrs.get("type") == "application/ld+json":
            self.in_json, self.json_text = True, ""

    def handle_data(self, text):
        if self.in_title:
            self.title += text
        if self.in_json:
            self.json_text += text

    def handle_endtag(self, tag):
        if tag == "head":
            self.in_head = False
        if tag == "title":
            self.in_title = False
        if tag == "script" and self.in_json:
            try:
                self.structured.append(json.loads(self.json_text))
            except ValueError as error:
                self.errors.append(f"invalid JSON-LD: {error}")
            self.in_json = False


def check(root, base=BASE, languages=None):
    base = base.rstrip("/") + "/"
    errors, pages, titles, descriptions = [], {}, set(), set()
    for path in root.rglob("*.html"):
        relative = path.relative_to(root).as_posix()
        url = base + (relative[:-10] if relative.endswith("index.html") else relative)
        page = Page(path.read_text())
        pages[url] = page
        for error in page.errors:
            errors.append(f"{relative}: {error}")
        if page.h1 != 1 or "main-content" not in page.ids:
            errors.append(f"{relative}: expected one h1 and main-content")
        if page.canonical != url:
            errors.append(f"{relative}: incorrect canonical: {page.canonical}")
        if "noindex" in page.meta.get("robots", ""):
            errors.append(f"{relative}: unexpected indexing policy")
        description = page.meta.get("description", "")
        if not page.title.strip() or page.title in titles:
            errors.append(f"{relative}: missing or duplicate title")
        if not description or description in descriptions:
            errors.append(f"{relative}: missing or duplicate description")
        titles.add(page.title)
        descriptions.add(description)
        if not page.structured or page.meta.get("og:url") != url:
            errors.append(f"{relative}: missing structured data or wrong og:url")
        for prefix in ('og', 'twitter'):
            if page.meta.get(f'{prefix}:title') != page.title or page.meta.get(f'{prefix}:description') != description:
                errors.append(f"{relative}: inconsistent {prefix} title or description")
            image = page.meta.get(f'{prefix}:image', '')
            if not image.startswith('https://'):
                errors.append(f"{relative}: missing absolute {prefix} image URL")
            else:
                page.links.append(image)
        for target in page.copies:
            if target not in page.ids:
                errors.append(f"{relative}: copy target not found: {target}")
    if not pages:
        errors.append("no HTML pages were built")
    reachable, pending = set(), [base]
    while pending:
        url = pending.pop()
        if url in reachable or url not in pages:
            continue
        reachable.add(url)
        for target in pages[url].anchors:
            pending.append(urlsplit(urljoin(url, target))._replace(query="", fragment="").geturl())
    for url in sorted(pages.keys() - reachable):
        errors.append(f"{url}: page is not reachable through links from the homepage")
    for url, page in pages.items():
        for target in page.links:
            absolute = urljoin(url, target)
            parsed = urlsplit(absolute)
            if parsed.netloc != urlsplit(base).netloc:
                continue
            path_url = parsed._replace(query="", fragment="").geturl()
            if not path_url.startswith(base):
                errors.append(f"{url}: link escapes baseurl: {target}")
                continue
            relative = unquote(path_url[len(base):])
            file = root / (relative + "index.html" if not relative or relative.endswith("/") else relative)
            if not file.is_file():
                errors.append(f"{url}: missing local target: {target}")
            if parsed.fragment and path_url in pages and unquote(parsed.fragment) not in pages[path_url].ids:
                errors.append(f"{url}: missing fragment: {target}")
    if languages:
        expected_languages = set(languages)
        for url, page in pages.items():
            if page.lang not in expected_languages:
                errors.append(f"{url}: wrong HTML language: {page.lang}")
            if set(page.alternates) != expected_languages | {"x-default"}:
                errors.append(f"{url}: incomplete hreflang translations")
            if page.alternates.get(page.lang) != url:
                errors.append(f"{url}: hreflang must include self")
            if page.alternates.get("x-default") != page.alternates.get("en"):
                errors.append(f"{url}: wrong x-default")
            for language, target in page.alternates.items():
                other = pages.get(target)
                if not target or not target.startswith(base) or other is None:
                    errors.append(f"{url}: invalid hreflang target: {target}")
                elif other.alternates != page.alternates or (language != "x-default" and other.lang != language):
                    errors.append(f"{url}: nonreciprocal hreflang or wrong target language: {language}")
            for target in page.content_anchors:
                destination = urlsplit(urljoin(url, target))._replace(query="", fragment="").geturl()
                other = pages.get(destination)
                if other and other.lang != page.lang:
                    errors.append(f"{url}: content link changes locale: {target}")
            nodes = [node for block in page.structured for node in block.get("@graph", [block])]
            article = next((node for node in nodes if node.get("@type") != "BreadcrumbList"), {})
            if article.get("inLanguage") != page.lang:
                errors.append(f"{url}: wrong structured-data language")
            if article.get("@type") == "BlogPosting":
                for key in ("datePublished", "dateModified", "author", "publisher", "headline"):
                    if not article.get(key):
                        errors.append(f"{url}: missing BlogPosting {key}")
            prefix = "" if page.lang == "en" else f"{page.lang}/"
            if urljoin(url, page.feed_url or "") != base + prefix + "feed.xml":
                errors.append(f"{url}: wrong locale feed")
        for language in languages:
            prefix = "" if language == "en" else language + "/"
            feed_url = base + prefix + "feed.xml"
            try:
                feed = ET.parse(root / prefix / "feed.xml").getroot()
                ns = {"a": "http://www.w3.org/2005/Atom"}
                if feed.tag != "{http://www.w3.org/2005/Atom}feed" or feed.get("{http://www.w3.org/XML/1998/namespace}lang") != language:
                    errors.append(f"{feed_url}: wrong Atom language or root")
                entries = feed.findall("a:entry", ns)
                ids = [entry.findtext("a:id", namespaces=ns) for entry in entries]
                expected = {url for url, page in pages.items() if page.lang == language
                            and any(node.get("@type") == "BlogPosting" for block in page.structured
                                    for node in block.get("@graph", [block]))}
                if len(ids) != len(set(ids)) or set(ids) != expected or not expected:
                    errors.append(f"{feed_url}: feed does not match locale articles")
                if feed.findtext("a:id", namespaces=ns) != base + prefix + "blog/":
                    errors.append(f"{feed_url}: wrong feed id")
                self_link = feed.find('a:link[@rel="self"]', ns)
                if self_link is None or self_link.get("href") != feed_url:
                    errors.append(f"{feed_url}: wrong feed self link")
                for entry in entries:
                    link = entry.find("a:link", ns)
                    if link is None or link.get("href") != entry.findtext("a:id", namespaces=ns):
                        errors.append(f"{feed_url}: entry link differs from canonical id")
                    for field in ("title", "published", "updated", "summary"):
                        if not entry.findtext("a:" + field, namespaces=ns):
                            errors.append(f"{feed_url}: missing entry {field}")
            except (OSError, ET.ParseError) as error:
                errors.append(f"{feed_url}: invalid Atom feed: {error}")
    try:
        sitemap = ET.parse(root / "sitemap.xml")
        urls = [node.text for node in sitemap.findall(".//{http://www.sitemaps.org/schemas/sitemap/0.9}loc")]
        expected = {url for url, page in pages.items() if "noindex" not in page.meta.get("robots", "")}
        if len(urls) != len(set(urls)) or set(urls) != expected:
            errors.append("sitemap does not match the indexable canonical pages")
    except (OSError, ET.ParseError) as error:
        errors.append(f"invalid sitemap: {error}")
    return errors


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("directory", type=Path)
    parser.add_argument("--base-url", default=BASE)
    registry = Path(__file__).resolve().parents[1] / "_data/locales.yml"
    parser.add_argument("--languages", nargs="+", default=re.findall(r'^([a-z]{2}):$', registry.read_text(), re.M))
    args = parser.parse_args()
    failures = check(args.directory, args.base_url, args.languages)
    if failures:
        parser.exit(1, "\n".join(failures) + "\n")
    print("PASS: metadata, JSON-LD, sitemap, reciprocal translations, Atom feeds, crawlable pages, links, fragments and copy targets")
