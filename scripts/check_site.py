#!/usr/bin/env python3
"""Check the built Pages artifact, including URLs under the project base path."""
import argparse
from html.parser import HTMLParser
import json
from pathlib import Path
from urllib.parse import unquote, urljoin, urlsplit
import xml.etree.ElementTree as ET

BASE = "https://profundium.github.io/pg_local_cache/"


class Page(HTMLParser):
    def __init__(self, text):
        super().__init__(convert_charrefs=True)
        self.ids, self.links, self.meta, self.copies = set(), [], {}, []
        self.h1 = 0
        self.canonical = None
        self.title = ""
        self.structured = []
        self.errors = []
        self.in_title = self.in_json = False
        self.json_text = ""
        self.feed(text)

    def handle_starttag(self, tag, attrs):
        attrs = dict(attrs)
        if "id" in attrs:
            if attrs["id"] in self.ids:
                self.errors.append(f"duplicate id: {attrs['id']}")
            self.ids.add(attrs["id"])
        if tag == "h1":
            self.h1 += 1
        if tag == "title":
            self.in_title = True
        if tag == "meta":
            self.meta[attrs.get("name", attrs.get("property"))] = attrs.get("content", "")
        if tag == "link" and attrs.get("rel") == "canonical":
            self.canonical = attrs.get("href")
        if "href" in attrs:
            self.links.append(attrs["href"])
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
        if tag == "title":
            self.in_title = False
        if tag == "script" and self.in_json:
            try:
                self.structured.append(json.loads(self.json_text))
            except ValueError as error:
                self.errors.append(f"invalid JSON-LD: {error}")
            self.in_json = False


def check(root):
    errors, pages, titles, descriptions = [], {}, set(), set()
    for path in root.rglob("*.html"):
        relative = path.relative_to(root).as_posix()
        url = BASE + (relative[:-10] if relative.endswith("index.html") else relative)
        page = Page(path.read_text())
        pages[url] = page
        for error in page.errors:
            errors.append(f"{relative}: {error}")
        if page.h1 != 1 or "main-content" not in page.ids:
            errors.append(f"{relative}: expected one h1 and main-content")
        if page.canonical != url:
            errors.append(f"{relative}: incorrect canonical: {page.canonical}")
        description = page.meta.get("description", "")
        if not page.title.strip() or page.title in titles:
            errors.append(f"{relative}: missing or duplicate title")
        if not description or description in descriptions:
            errors.append(f"{relative}: missing or duplicate description")
        titles.add(page.title)
        descriptions.add(description)
        if not page.structured or page.meta.get("og:url") != url:
            errors.append(f"{relative}: missing structured data or wrong og:url")
        for target in page.copies:
            if target not in page.ids:
                errors.append(f"{relative}: copy target not found: {target}")
    if not pages:
        errors.append("no HTML pages were built")
    for url, page in pages.items():
        for target in page.links:
            absolute = urljoin(url, target)
            parsed = urlsplit(absolute)
            if parsed.netloc != urlsplit(BASE).netloc:
                continue
            path_url = parsed._replace(query="", fragment="").geturl()
            if not path_url.startswith(BASE):
                errors.append(f"{url}: link escapes baseurl: {target}")
                continue
            relative = unquote(path_url[len(BASE):])
            file = root / (relative + "index.html" if not relative or relative.endswith("/") else relative)
            if not file.is_file():
                errors.append(f"{url}: missing local target: {target}")
            if parsed.fragment and path_url in pages and unquote(parsed.fragment) not in pages[path_url].ids:
                errors.append(f"{url}: missing fragment: {target}")
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
    args = parser.parse_args()
    failures = check(args.directory)
    if failures:
        parser.exit(1, "\n".join(failures) + "\n")
    print("PASS: built page metadata, JSON-LD, sitemap, links, fragments and copy targets")
