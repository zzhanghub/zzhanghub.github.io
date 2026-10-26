#!/usr/bin/env python3
"""Check the hand-written site using only Python's standard library.

Run ``python3 scripts/check_site.py`` locally or in CI. The default never writes.
After changing publication text, run with ``--write-structured-data`` to refresh
only the two ``script#publication-structured-data`` blocks, then review the diff.
HTML remains the source of truth; no separate publication database is needed.
"""

from __future__ import annotations

import argparse
from dataclasses import dataclass, field
from html.parser import HTMLParser
import json
from pathlib import Path
import re
import sys
from urllib.parse import unquote, urljoin, urlsplit
from urllib.robotparser import RobotFileParser
from xml.etree import ElementTree


PAGES = ("index.html", "publications.html", "data.html", "coca.html")
PUBLICATION_PAGES = PAGES[:2]
STRUCTURED_DATA_ID = "publication-structured-data"
VOID_TAGS = set("area base br col embed hr img input link meta param source track wbr".split())


def normalize(value: str) -> str:
    return " ".join(value.split())


def slug(value: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", value.lower()).strip("-")


@dataclass(eq=False)
class Node:
    tag: str
    attrs: dict[str, str]
    line: int = 1
    start: int = 0
    end: int = 0
    parent: Node | None = None
    children: list[Node | str] = field(default_factory=list)

    def descendants(self, tag: str | None = None):
        for child in self.children:
            if isinstance(child, Node):
                if tag is None or child.tag == tag:
                    yield child
                yield from child.descendants(tag)

    def text(self) -> str:
        return "".join(
            child if isinstance(child, str) else " " if child.tag == "br" else child.text()
            for child in self.children
        )

    def has_class(self, name: str) -> bool:
        return name in self.attrs.get("class", "").split()


class Document(HTMLParser):
    def __init__(self, path: Path):
        super().__init__(convert_charrefs=True)
        self.path = path
        self.source = path.read_text(encoding="utf-8")
        self.root = Node("document", {})
        self.stack = [self.root]
        self.line_offsets = [0]
        for match in re.finditer("\n", self.source):
            self.line_offsets.append(match.end())
        self.feed(self.source)
        self.close()
        self.nodes = list(self.root.descendants())
        self.ids = {node.attrs["id"]: node for node in self.nodes if node.attrs.get("id")}

    def source_offset(self) -> int:
        line, column = self.getpos()
        return self.line_offsets[line - 1] + column

    def handle_starttag(self, tag, attrs):
        node = Node(tag, {k: v or "" for k, v in attrs}, self.getpos()[0], self.source_offset(),
                    parent=self.stack[-1])
        self.stack[-1].children.append(node)
        if tag not in VOID_TAGS:
            self.stack.append(node)

    def handle_startendtag(self, tag, attrs):
        self.handle_starttag(tag, attrs)
        if tag not in VOID_TAGS:
            self.stack.pop()

    def handle_endtag(self, tag):
        for index in range(len(self.stack) - 1, 0, -1):
            if self.stack[index].tag == tag:
                end = self.source.find(">", self.source_offset()) + 1
                self.stack[index].end = end
                del self.stack[index:]
                break

    def handle_data(self, data):
        self.stack[-1].children.append(data)


@dataclass
class Paper:
    title: str
    authors: list[str]
    venue: str
    links: dict[str, str]
    anchor: str
    line: int


class Checker:
    def __init__(self, root: Path):
        self.root = root.resolve()
        cname = self.root / "CNAME"
        domain = cname.read_text().strip() if cname.exists() else "zhaozhang.net"
        self.origin = "https://" + domain + "/"
        self.documents: dict[Path, Document] = {}
        self.errors: list[str] = []

    def error(self, document: Document, line: int, message: str):
        self.errors.append(f"{document.path.relative_to(self.root)}:{line}: {message}")

    def document(self, path: Path) -> Document:
        path = path.resolve()
        if path not in self.documents:
            self.documents[path] = Document(path)
        return self.documents[path]

    def canonical(self, filename: str) -> str:
        return self.origin if filename == "index.html" else urljoin(self.origin, filename)

    def absolute(self, document: Document, url: str) -> str:
        return urljoin(self.canonical(document.path.relative_to(self.root).as_posix()), url)

    def check_reference(self, document: Document, node: Node, value: str):
        if not value.strip() or value.strip() == "#":
            self.error(document, node.line, "empty/placeholder resource URL")
            return
        parsed = urlsplit(self.absolute(document, value))
        if parsed.netloc != urlsplit(self.origin).netloc or parsed.scheme not in ("http", "https"):
            return  # External URLs are deliberately not fetched by this offline check.
        target = (self.root / unquote(parsed.path).lstrip("/")).resolve()
        if not target.is_relative_to(self.root):
            self.error(document, node.line, f"local URL leaves the site root: {value}")
            return
        if target.is_dir():
            target /= "index.html"
        if not target.is_file():
            self.error(document, node.line, f"missing local resource: {value}")
            return
        if parsed.fragment and target.suffix.lower() in (".html", ".htm"):
            fragment = unquote(parsed.fragment)
            other = self.document(target)
            anchors = set(other.ids) | {
                n.attrs["name"] for n in other.nodes if n.tag == "a" and n.attrs.get("name")
            }
            if fragment not in anchors:
                self.error(document, node.line, f"missing local anchor: {value}")

    def check_resources_and_names(self, document: Document):
        seen_ids = set()
        for node in document.nodes:
            node_id = node.attrs.get("id")
            if node_id:
                if node_id in seen_ids:
                    self.error(document, node.line, f"duplicate id: {node_id}")
                seen_ids.add(node_id)
            for attribute in ("href", "src", "data-src", "poster"):
                if attribute in node.attrs:
                    self.check_reference(document, node, node.attrs[attribute])
            if node.tag == "img" and "alt" not in node.attrs:
                self.error(document, node.line, f"image has no alt attribute: {node.attrs.get('src', '')}")
            if node.tag == "a" and list(node.descendants("img")):
                names = [normalize(node.text()), node.attrs.get("aria-label", ""),
                         node.attrs.get("title", "")]
                names.extend(image.attrs.get("alt", "") for image in node.descendants("img"))
                names.extend(document.ids[label].text() for label in node.attrs.get("aria-labelledby", "").split()
                             if label in document.ids)
                if not any(normalize(name) for name in names):
                    self.error(document, node.line, "image-only link has no accessible name")

    def check_metadata(self, document: Document):
        heads = [node for node in document.nodes if node.tag == "head"]
        head = heads[0] if heads else document.root
        titles = list(head.descendants("title"))
        if len(titles) != 1 or not normalize(titles[0].text()):
            self.error(document, 1, "expected one nonempty page title")
        metadata: dict[str, list[Node]] = {}
        for node in head.descendants("meta"):
            key = node.attrs.get("name", node.attrs.get("property", "")).lower()
            metadata.setdefault(key, []).append(node)
        required = ("description", "og:title", "og:description", "og:type", "og:url", "og:image",
                    "twitter:card", "twitter:title", "twitter:description", "twitter:image")
        for key in required:
            matches = metadata.get(key, [])
            if len(matches) != 1 or not normalize(matches[0].attrs.get("content", "")):
                self.error(document, 1, f"expected one nonempty meta {key}")
        canonical = [n for n in head.descendants("link") if "canonical" in n.attrs.get("rel", "").split()]
        expected = self.canonical(document.path.name)
        if len(canonical) != 1 or canonical[0].attrs.get("href") != expected:
            self.error(document, 1, f"canonical must be {expected}")
        if metadata.get("og:url") and metadata["og:url"][0].attrs.get("content") != expected:
            self.error(document, metadata["og:url"][0].line, f"og:url must match canonical: {expected}")
        for key in ("og:image", "twitter:image"):
            for node in metadata.get(key, []):
                value = node.attrs.get("content", "")
                if not value.startswith("https://"):
                    self.error(document, node.line, f"{key} must use an absolute HTTPS URL")
                if value:
                    self.check_reference(document, node, value)
        for node in (n for n in document.nodes if n.tag == "script" and n.attrs.get("type") == "application/ld+json"):
            try:
                json.loads(node.text())
            except json.JSONDecodeError as error:
                self.error(document, node.line, f"invalid JSON-LD: {error.msg}")

    def papers(self, document: Document) -> list[Paper]:
        papers = []
        titles_seen = set()
        for title_node in (n for n in document.nodes if n.has_class("paper-title")):
            row = title_node.parent
            while row and not row.has_class("row"):
                row = row.parent
            if not row:
                self.error(document, title_node.line, "paper-title must be inside a .row")
                continue
            title = normalize(title_node.text())
            if title in titles_seen:
                self.error(document, title_node.line, f"duplicate paper title: {title}")
            titles_seen.add(title)
            children = list(row.descendants())
            fields = {}
            for field_name in ("paper-authors", "paper-pub"):
                matches = [n for n in children if n.has_class(field_name)]
                if len(matches) != 1:
                    self.error(document, title_node.line, f"{title}: expected one .{field_name}")
                fields[field_name] = normalize(matches[0].text()) if matches else ""
            # The visible HTML must use commas between people, including at <br>.
            # Remove visible contribution markers, which are not part of a name.
            authors = [re.sub(r"[\*†‡]+$", "", name.strip()).strip()
                       for name in fields["paper-authors"].split(",") if name.strip()]
            if authors:
                authors[-1] = re.sub(r"^and\s+", "", authors[-1], flags=re.IGNORECASE)
            links = {}
            for link in (n for n in children if n.tag == "a" and n.attrs.get("href")):
                label = normalize(link.text()).strip("[]").lower()
                if not label:
                    continue
                value = self.absolute(document, link.attrs["href"])
                if label in links and links[label] != value:
                    self.error(document, link.line, f"{title}: duplicate resource label [{label}]")
                links[label] = value
            resource_dirs = []
            for node in children:
                for key in ("href", "src", "data-src"):
                    match = re.search(r"(?:^|/)papers/([^/]+)/", node.attrs.get(key, ""))
                    if match:
                        resource_dirs.append(match.group(1))
            stable_id = "paper-" + slug(resource_dirs[0] if resource_dirs else title)
            anchor = row.attrs.get("id", stable_id)
            if not anchor.startswith("paper-"):
                self.error(document, row.line, f"paper row id must start with paper-: {anchor}")
            if not row.attrs.get("id"):
                self.error(document, row.line, f"paper row needs id=\"{stable_id}\": {title}")
            if not title or not authors or not fields["paper-pub"]:
                self.error(document, title_node.line, "publication title/authors/venue must be nonempty")
            papers.append(Paper(title, authors, fields["paper-pub"], links, anchor, title_node.line))
        if not papers:
            self.error(document, 1, "no publications found")
        return papers

    def check_paper_consistency(self, pages: dict[str, list[Paper]]):
        full = {paper.title: paper for paper in pages["publications.html"]}
        document = self.document(self.root / "index.html")
        for paper in pages["index.html"]:
            other = full.get(paper.title)
            if other is None:
                self.error(document, paper.line, f"selected paper missing from publications.html: {paper.title}")
                continue
            for field_name in ("authors", "venue", "links", "anchor"):
                first, second = getattr(paper, field_name), getattr(other, field_name)
                if first != second:
                    self.error(document, paper.line,
                               f"{paper.title}: {field_name} differs from publications.html:{other.line}\n"
                               f"  index: {first}\n  publications: {second}")

    def structured_data(self, filename: str, papers: list[Paper]) -> dict:
        entries = []
        for position, paper in enumerate(papers, 1):
            url = self.canonical("publications.html") + "#" + paper.anchor
            authors = []
            for name in paper.authors:
                person = {"@type": "Person", "name": name}
                if name == "Zhao Zhang":
                    person["@id"] = self.origin + "#person"
                authors.append(person)
            article = {"@type": "ScholarlyArticle", "@id": url, "name": paper.title,
                       "url": url, "author": authors,
                       "isPartOf": {"@type": "CreativeWork", "name": paper.venue}}
            # Code/project URLs are resources, not equivalent identities of the paper.
            same_as = list(dict.fromkeys(value for label, value in paper.links.items()
                                        if label in ("paper", "pdf", "doi")))
            if same_as:
                article["sameAs"] = same_as
            entries.append({"@type": "ListItem", "position": position, "item": article})
        return {"@context": "https://schema.org", "@type": "ItemList",
                "@id": self.canonical(filename) + "#publication-list",
                "name": "Selected Publications" if filename == "index.html" else "Publications",
                "numberOfItems": len(entries), "itemListElement": entries}

    def check_structured_data(self, document: Document, papers: list[Paper]):
        matches = [n for n in document.nodes if n.attrs.get("id") == STRUCTURED_DATA_ID]
        if len(matches) != 1 or matches[0].tag != "script" or matches[0].attrs.get("type") != "application/ld+json":
            self.error(document, 1, f"expected one script#{STRUCTURED_DATA_ID}; run --write-structured-data")
            return
        try:
            actual = json.loads(matches[0].text())
        except json.JSONDecodeError:
            return  # Reported by the general JSON-LD check.
        if actual != self.structured_data(document.path.name, papers):
            self.error(document, matches[0].line,
                       "publication JSON-LD differs from visible papers; run --write-structured-data")
        for paper in papers:
            self.check_reference(document, matches[0], self.canonical("publications.html") + "#" + paper.anchor)

    def write_structured_data(self, document: Document, papers: list[Paper]):
        matches = [n for n in document.nodes if n.attrs.get("id") == STRUCTURED_DATA_ID]
        if len(matches) > 1 or (matches and (matches[0].tag != "script" or not matches[0].end)):
            self.error(document, 1, f"cannot safely replace malformed #{STRUCTURED_DATA_ID}")
            return
        data = json.dumps(self.structured_data(document.path.name, papers), ensure_ascii=False, indent=2)
        data = data.replace("<", "\\u003c")
        block = f'<script type="application/ld+json" id="{STRUCTURED_DATA_ID}">\n{data}\n</script>'
        if matches:
            node = matches[0]
            updated = document.source[:node.start] + block + document.source[node.end:]
        else:
            end_head = re.search(r"</head\s*>", document.source, re.IGNORECASE)
            if not end_head:
                self.error(document, 1, "cannot insert publication JSON-LD without </head>")
                return
            updated = document.source[:end_head.start()] + block + "\n" + document.source[end_head.start():]
        if updated != document.source:
            document.path.write_text(updated, encoding="utf-8")
            print(f"Updated {document.path.name} #{STRUCTURED_DATA_ID}")

    def check_discovery(self):
        expected = {self.canonical(name) for name in PAGES}
        sitemap = self.root / "sitemap.xml"
        if not sitemap.is_file():
            self.errors.append("sitemap.xml:1: missing sitemap")
        else:
            try:
                tree = ElementTree.fromstring(sitemap.read_text(encoding="utf-8"))
                namespace = "{http://www.sitemaps.org/schemas/sitemap/0.9}"
                if tree.tag != namespace + "urlset":
                    self.errors.append("sitemap.xml:1: expected a sitemap urlset with the standard namespace")
                urls = [normalize(node.text or "") for node in tree.findall(f"{namespace}url/{namespace}loc")]
                if len(urls) != len(expected) or set(urls) != expected:
                    self.errors.append("sitemap.xml:1: loc entries must contain exactly the four canonical page URLs, without fragments")
                document = self.document(self.root / "index.html")
                for url in urls:
                    self.check_reference(document, Node("loc", {}), url)
            except ElementTree.ParseError as error:
                self.errors.append(f"sitemap.xml:1: invalid XML: {error}")
        robots = self.root / "robots.txt"
        if not robots.is_file():
            self.errors.append("robots.txt:1: missing robots file")
            return
        parser = RobotFileParser()
        parser.parse(robots.read_text(encoding="utf-8").splitlines())
        if parser.site_maps() != [urljoin(self.origin, "sitemap.xml")]:
            self.errors.append("robots.txt:1: Sitemap must reference the canonical sitemap.xml URL exactly once")
        for bot in ("Googlebot", "Bingbot", "OAI-SearchBot"):
            for url in sorted(expected):
                if not parser.can_fetch(bot, url):
                    self.errors.append(f"robots.txt:1: {bot} is blocked from {url}")

    def run(self, write: bool = False) -> int:
        documents = {name: self.document(self.root / name) for name in PAGES}
        papers = {name: self.papers(documents[name]) for name in PUBLICATION_PAGES}
        self.check_paper_consistency(papers)
        if write:
            if self.errors:
                print("Publication errors must be fixed before regenerating structured data.", file=sys.stderr)
            else:
                for name in PUBLICATION_PAGES:
                    self.write_structured_data(documents[name], papers[name])
                if not self.errors:
                    self.documents.clear()
                    documents = {name: self.document(self.root / name) for name in PAGES}
        for document in documents.values():
            self.check_resources_and_names(document)
            self.check_metadata(document)
        for name in PUBLICATION_PAGES:
            self.check_structured_data(documents[name], papers[name])
        self.check_discovery()
        if self.errors:
            print("\n".join(self.errors), file=sys.stderr)
            print(f"\nSite check failed: {len(self.errors)} issue(s).", file=sys.stderr)
            return 1
        print(f"Site check passed: {len(PAGES)} pages; {len(papers['index.html'])} selected / "
              f"{len(papers['publications.html'])} total publications.")
        return 0


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--root", type=Path, default=Path(__file__).resolve().parents[1],
                        help="site root (defaults to the parent of scripts/)")
    parser.add_argument("--write-structured-data", action="store_true",
                        help="refresh only publication JSON-LD from HTML before checking")
    args = parser.parse_args()
    try:
        return Checker(args.root).run(args.write_structured_data)
    except (OSError, UnicodeError, ValueError) as error:
        print(f"Site check failed: {error}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
