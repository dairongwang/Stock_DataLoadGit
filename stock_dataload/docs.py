"""
The built-in documentation: Markdown files under ``docs/`` rendered on the
dashboard's **Docs** tab (``/docs``), organised as a **nested tree** so a
reader can drill down from an overview to the details:

::

    docs/
    ├── index.md                    the front page (role-based entry points)
    ├── user-manual/                dashboard users: index.md + one page per topic
    ├── portal/                     customers of the data API
    ├── tech-design/                developers: architecture, data model, security, payments ...
    ├── operations/                 administrators: configuration, deployment, housekeeping
    └── reference/                  CLI, API, tables, configuration keys

* every folder is a **section**; its ``index.md`` is the section's page and
  gives the section its title; files are ordered by name (a ``NN-`` prefix
  orders them and is dropped from the URL: ``user-manual/10-getting-started.md``
  is served as ``/docs/user-manual/getting-started``); a ``.order`` file in a
  folder lists children that should come first;
* the first ``# Heading`` of a file is its title; headings get ids for the
  page's table of contents; links to other ``.md`` files are rewritten to
  the dashboard URLs, so the same files read fine on GitHub and here;
* a small Markdown subset is rendered with the standard library (headings,
  paragraphs, lists, fenced code, tables, block quotes, rules, emphasis,
  inline code, links) - no extra dependency;
* ``search()`` finds pages by words in their text.
"""

from __future__ import annotations

import html
import re
from dataclasses import dataclass, field
from pathlib import Path

DOCS_DIR = Path(__file__).resolve().parent.parent / "docs"

_PREFIX_RE = re.compile(r"^\d+[-_]")
_HEADING_RE = re.compile(r"^(#{1,6})\s+(.*?)\s*#*\s*$")
_TABLE_SEP_RE = re.compile(r"^\s*\|?\s*:?-{3,}:?\s*(\|\s*:?-{3,}:?\s*)*\|?\s*$")
_UL_RE = re.compile(r"^(\s*)[-*+]\s+(.*)$")
_OL_RE = re.compile(r"^(\s*)\d+[.)]\s+(.*)$")


class DocError(Exception):
    """The documentation folder is missing or a page does not exist."""


# ---------------------------------------------------------------------------
# the tree
# ---------------------------------------------------------------------------
@dataclass
class DocNode:
    slug: str                      # "" for the root, "user-manual", "user-manual/getting-started"
    title: str
    path: Path | None              # the markdown file (index.md for sections)
    is_section: bool
    children: list["DocNode"] = field(default_factory=list)
    parent: "DocNode | None" = None

    @property
    def url_slug(self) -> str:
        return self.slug

    def ancestors(self) -> list["DocNode"]:
        out, node = [], self.parent
        while node is not None:
            out.append(node)
            node = node.parent
        return list(reversed(out))

    def walk(self):
        yield self
        for child in self.children:
            yield from child.walk()


def slugify_name(name: str) -> str:
    name = name[:-3] if name.lower().endswith(".md") else name
    return _PREFIX_RE.sub("", name).lower()


def title_of(path: Path, fallback: str) -> str:
    try:
        for line in path.read_text(encoding="utf-8").splitlines():
            m = _HEADING_RE.match(line)
            if m:
                return m.group(2).strip()
    except OSError:
        pass
    return fallback.replace("-", " ").replace("_", " ").strip().capitalize()


class DocTree:
    """Scans the docs folder once (``reload()`` to rescan) and serves pages by slug."""

    def __init__(self, root: Path | str = DOCS_DIR):
        self.root = Path(root)
        self.nodes: dict[str, DocNode] = {}
        self.tree: DocNode | None = None
        self.reload()

    # -- scanning -------------------------------------------------------------
    def reload(self) -> None:
        self.nodes = {}
        if not self.root.is_dir():
            self.tree = DocNode("", "Documentation", None, True)
            self.nodes[""] = self.tree
            return
        self.tree = self._scan(self.root, "", None)
        for node in self.tree.walk():
            self.nodes[node.slug] = node

    @staticmethod
    def _order_key(folder: Path):
        """Sort children by name, but names listed in the folder's ``.order`` file come first, in that order."""
        order_file = folder / ".order"
        listed: list[str] = []
        if order_file.is_file():
            listed = [line.strip().lower() for line in order_file.read_text(encoding="utf-8").splitlines()
                      if line.strip() and not line.startswith("#")]

        def key(path: Path):
            name = path.name.lower()
            return (listed.index(name) if name in listed else len(listed), name)
        return key

    def _scan(self, folder: Path, slug: str, parent: DocNode | None) -> DocNode:
        index = folder / "index.md"
        node = DocNode(slug, title_of(index, folder.name if slug else "Documentation") if index.is_file()
                       else folder.name.replace("-", " ").capitalize(), index if index.is_file() else None, True,
                       parent=parent)
        entries = sorted(folder.iterdir(), key=self._order_key(folder))
        for entry in entries:
            if entry.name.startswith(".") or entry.name.lower() == "index.md":
                continue
            if entry.is_dir():
                child = self._scan(entry, f"{slug}/{slugify_name(entry.name)}".strip("/"), node)
                if child.path is not None or child.children:
                    node.children.append(child)
            elif entry.suffix.lower() == ".md":
                child_slug = f"{slug}/{slugify_name(entry.name)}".strip("/")
                node.children.append(DocNode(child_slug, title_of(entry, entry.stem), entry, False, parent=node))
        return node

    # -- pages -----------------------------------------------------------------
    def get(self, slug: str) -> DocNode:
        node = self.nodes.get((slug or "").strip("/").lower())
        if node is None or node.path is None:
            raise DocError(f"no documentation page '{slug}'")
        return node

    def page(self, slug: str) -> tuple[DocNode, str, list[tuple[int, str, str]]]:
        """``(node, html, toc)`` - toc entries are ``(level, id, text)``."""
        node = self.get(slug)
        text = node.path.read_text(encoding="utf-8")
        return node, *render_markdown(text, link_resolver=lambda target: self.resolve_link(node, target))

    def resolve_link(self, node: DocNode, target: str) -> str:
        """``../tech-design/20-data-model.md#tables`` -> ``/docs/tech-design/data-model#tables``."""
        if re.match(r"^[a-z][a-z0-9+.-]*:", target) or target.startswith("/") or target.startswith("#"):
            return target
        path_part, _, anchor = target.partition("#")
        if not path_part.lower().endswith(".md"):
            return target
        base = node.path.parent if node.path else self.root
        try:
            resolved = (base / path_part).resolve().relative_to(self.root.resolve())
        except ValueError:
            return target
        parts = [slugify_name(p) for p in resolved.parts]
        if parts and parts[-1] == "index":
            parts = parts[:-1]
        slug = "/".join(parts)
        if slug not in self.nodes:
            return target
        return f"/docs/{slug}" + (f"#{anchor}" if anchor else "")

    # -- search ------------------------------------------------------------------
    def search(self, query: str, limit: int = 30) -> list[tuple[DocNode, str]]:
        words = [w.lower() for w in re.findall(r"\w+", query or "") if len(w) > 1]
        if not words:
            return []
        hits: list[tuple[int, DocNode, str]] = []
        for node in self.tree.walk():
            if node.path is None:
                continue
            text = node.path.read_text(encoding="utf-8")
            low = text.lower()
            score = sum(low.count(w) for w in words)
            if score == 0 or not all(w in low for w in words):
                continue
            if all(w in node.title.lower() for w in words):
                score += 50
            pos = min(low.find(w) for w in words)
            start = max(0, low.rfind("\n", 0, pos) + 1)
            end = low.find("\n", pos)
            snippet = text[start:end if end != -1 else len(text)].strip().lstrip("#").strip()
            hits.append((score, node, snippet[:200]))
        hits.sort(key=lambda h: (-h[0], h[1].slug))
        return [(node, snippet) for _, node, snippet in hits[:limit]]


# ---------------------------------------------------------------------------
# markdown -> html (a pragmatic subset)
# ---------------------------------------------------------------------------
def heading_id(text: str) -> str:
    plain = re.sub(r"[`*_\[\]()]", "", text)
    return re.sub(r"[^a-z0-9]+", "-", plain.lower()).strip("-") or "section"


def render_inline(text: str, link_resolver=None) -> str:
    """Escape HTML, then inline code, links, bold, italic."""
    out: list[str] = []
    # protect inline code first
    parts = re.split(r"(`[^`]+`)", text)
    for part in parts:
        if part.startswith("`") and part.endswith("`") and len(part) > 1:
            out.append(f"<code>{html.escape(part[1:-1])}</code>")
            continue
        s = html.escape(part, quote=False)

        def link(m):
            label, target = m.group(1), m.group(2)
            href = link_resolver(target) if link_resolver else target
            return f'<a href="{html.escape(href, quote=True)}">{label}</a>'

        s = re.sub(r"\[([^\]]+)\]\(([^)\s]+)\)", link, s)
        s = re.sub(r"\*\*(.+?)\*\*", r"<b>\1</b>", s)
        s = re.sub(r"(?<![\w*])\*(?!\s)(.+?)(?<!\s)\*(?!\w)", r"<i>\1</i>", s)
        s = re.sub(r"(?<![\w_])_(?!\s)(.+?)(?<!\s)_(?!\w)", r"<i>\1</i>", s)
        out.append(s)
    return "".join(out)


def render_markdown(text: str, link_resolver=None) -> tuple[str, list[tuple[int, str, str]]]:
    """Returns ``(html, toc)``; the toc lists ``(level, id, text)`` of the headings."""
    lines = text.replace("\r\n", "\n").split("\n")
    out: list[str] = []
    toc: list[tuple[int, str, str]] = []
    used_ids: dict[str, int] = {}
    i, n = 0, len(lines)
    paragraph: list[str] = []

    def flush_paragraph():
        if paragraph:
            out.append(f"<p>{render_inline(' '.join(s.strip() for s in paragraph), link_resolver)}</p>")
            paragraph.clear()

    while i < n:
        line = lines[i]
        stripped = line.strip()
        # fenced code
        if stripped.startswith("```"):
            flush_paragraph()
            lang = stripped[3:].strip()
            code: list[str] = []
            i += 1
            while i < n and not lines[i].strip().startswith("```"):
                code.append(lines[i])
                i += 1
            i += 1
            cls = f' class="lang-{html.escape(lang)}"' if lang else ""
            out.append(f"<pre><code{cls}>{html.escape(chr(10).join(code))}</code></pre>")
            continue
        if not stripped:
            flush_paragraph()
            i += 1
            continue
        m = _HEADING_RE.match(line)
        if m:
            flush_paragraph()
            level, title = len(m.group(1)), m.group(2).strip()
            hid = heading_id(title)
            if hid in used_ids:
                used_ids[hid] += 1
                hid = f"{hid}-{used_ids[hid]}"
            else:
                used_ids[hid] = 1
            out.append(f'<h{level} id="{hid}">{render_inline(title, link_resolver)}</h{level}>')
            if level >= 2:
                toc.append((level, hid, re.sub(r"[`*_]", "", title)))
            i += 1
            continue
        if re.match(r"^(-{3,}|\*{3,}|_{3,})\s*$", stripped):
            flush_paragraph()
            out.append("<hr>")
            i += 1
            continue
        if stripped.startswith(">"):
            flush_paragraph()
            quote: list[str] = []
            while i < n and lines[i].strip().startswith(">"):
                quote.append(lines[i].strip()[1:].strip())
                i += 1
            inner, _ = render_markdown("\n".join(quote), link_resolver)
            out.append(f"<blockquote>{inner}</blockquote>")
            continue
        # table: a header row followed by a separator row
        if "|" in stripped and i + 1 < n and _TABLE_SEP_RE.match(lines[i + 1]):
            flush_paragraph()
            header = _split_row(stripped)
            aligns = [("right" if c.strip().endswith(":") and not c.strip().startswith(":") else
                       "center" if c.strip().startswith(":") and c.strip().endswith(":") else "left")
                      for c in _split_row(lines[i + 1])]
            rows = []
            i += 2
            while i < n and "|" in lines[i] and lines[i].strip():
                rows.append(_split_row(lines[i].strip()))
                i += 1
            out.append("<table><thead><tr>" + "".join(
                f'<th style="text-align:{aligns[k] if k < len(aligns) else "left"}">{render_inline(c, link_resolver)}</th>'
                for k, c in enumerate(header)) + "</tr></thead><tbody>")
            for row in rows:
                out.append("<tr>" + "".join(
                    f'<td style="text-align:{aligns[k] if k < len(aligns) else "left"}">{render_inline(c, link_resolver)}</td>'
                    for k, c in enumerate(row)) + "</tr>")
            out.append("</tbody></table>")
            continue
        # lists (nested by indentation)
        if _UL_RE.match(line) or _OL_RE.match(line):
            flush_paragraph()
            block: list[str] = []
            while i < n:
                cur = lines[i]
                if _UL_RE.match(cur) or _OL_RE.match(cur) or (cur.startswith("  ") and cur.strip()):
                    block.append(cur)
                    i += 1
                elif cur.strip() == "" and i + 1 < n and lines[i + 1].startswith("  ") and lines[i + 1].strip():
                    i += 1                       # a blank line inside an indented (nested) part
                else:
                    break
            out.append(_render_list(block, link_resolver))
            continue
        paragraph.append(line)
        i += 1
    flush_paragraph()
    return "\n".join(out), toc


def _split_row(row: str) -> list[str]:
    row = row.strip()
    if row.startswith("|"):
        row = row[1:]
    if row.endswith("|"):
        row = row[:-1]
    return [c.strip() for c in re.split(r"(?<!\\)\|", row)]


def _render_list(block: list[str], link_resolver) -> str:
    """Render a list block; deeper-indented items become nested lists."""
    items: list[tuple[int, bool, list[str]]] = []      # (indent, ordered, lines)
    for line in block:
        m = _UL_RE.match(line) or _OL_RE.match(line)
        if m and (len(m.group(1)) == 0 or items):
            indent = len(m.group(1).replace("\t", "    "))
            items.append((indent, bool(_OL_RE.match(line)), [m.group(2)]))
        elif items and line.strip():
            items[-1][2].append(line.strip())
    if not items:
        return ""

    def build(start: int, indent: int) -> tuple[str, int]:
        ordered = items[start][1]
        tag = "ol" if ordered else "ul"
        parts = [f"<{tag}>"]
        k = start
        while k < len(items) and items[k][0] >= indent:
            if items[k][0] > indent:
                nested, k = build(k, items[k][0])
                parts[-1] = parts[-1][:-5] + nested + "</li>" if parts[-1].endswith("</li>") else parts[-1] + nested
                continue
            text = " ".join(items[k][2])
            parts.append(f"<li>{render_inline(text, link_resolver)}</li>")
            k += 1
        parts.append(f"</{tag}>")
        return "".join(parts), k

    result, _ = build(0, items[0][0])
    return result
