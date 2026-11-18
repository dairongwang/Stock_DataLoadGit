"""The built-in documentation: markdown renderer, the docs tree, the dashboard's Docs tab."""

from __future__ import annotations

import re
import unittest

from stock_dataload.auth import UserStore
from stock_dataload.config import load_settings, save_settings
from stock_dataload.dashboard import create_app
from stock_dataload.docs import DOCS_DIR, DocError, DocTree, heading_id, render_inline, render_markdown

from .helpers import TempProjectTestCase


class RendererTests(unittest.TestCase):
    def test_blocks(self):
        html, toc = render_markdown(
            "# Title\n\nPara one\ncontinues.\n\n## Sec *one*\n\n- a\n- b `c`\n  - nested\n\n1. x\n2. y\n\n"
            "| H1 | H2 |\n|----|---:|\n| a | 1 |\n\n```bash\necho <hi>\n```\n\n> quote\n\n---\n\n### Sub\n\ntext")
        self.assertIn('<h1 id="title">Title</h1>', html)
        self.assertIn("<p>Para one continues.</p>", html)
        self.assertIn('<h2 id="sec-one">Sec <i>one</i></h2>', html)
        self.assertIn("<ul><li>a</li><li>b <code>c</code><ul><li>nested</li></ul></li></ul>", html)
        self.assertIn("<ol><li>x</li><li>y</li></ol>", html)
        self.assertIn('<th style="text-align:right">H2</th>', html)
        self.assertIn('<td style="text-align:left">a</td>', html)
        self.assertIn('<pre><code class="lang-bash">echo &lt;hi&gt;</code></pre>', html)
        self.assertIn("<blockquote><p>quote</p></blockquote>", html)
        self.assertIn("<hr>", html)
        self.assertEqual(toc, [(2, "sec-one", "Sec one"), (3, "sub", "Sub")])

    def test_inline_and_links(self):
        out = render_inline("**b** *i* _u_ `<x>` [go](a/b.md#s) <script>", link_resolver=lambda t: "/docs/" + t)
        self.assertEqual(out, '<b>b</b> <i>i</i> <i>u</i> <code>&lt;x&gt;</code> <a href="/docs/a/b.md#s">go</a> &lt;script&gt;')
        self.assertEqual(render_inline("a * b * c"), "a * b * c")          # lone asterisks stay
        self.assertEqual(render_inline("snake_case_name"), "snake_case_name")
        self.assertEqual(heading_id("Hello, `World`! (x)"), "hello-world-x")
        html, _ = render_markdown("## Same\n\n## Same")
        self.assertIn('id="same"', html)
        self.assertIn('id="same-2"', html)


class DocTreeTests(TempProjectTestCase):
    def test_shipped_documentation(self):
        tree = DocTree(DOCS_DIR)
        self.assertEqual([c.slug for c in tree.tree.children],
                         ["user-manual", "portal", "tech-design", "operations", "reference"])
        self.assertEqual(tree.get("user-manual/getting-started").title, "Getting started")
        for node in tree.tree.walk():
            if node.path is None:
                continue
            _, html, toc = tree.page(node.slug)
            self.assertTrue(html.startswith("<h1"), node.slug)
            text = node.path.read_text(encoding="utf-8")
            for m in re.finditer(r"\]\(([^)\s]+\.md(?:#[^)]*)?)\)", text):     # every internal link resolves
                self.assertNotEqual(tree.resolve_link(node, m.group(1)), m.group(1), f"{node.slug}: {m.group(1)}")
        node = tree.get("user-manual")
        self.assertTrue(node.is_section and len(node.children) >= 8)
        self.assertEqual([a.slug for a in tree.get("tech-design/payments").ancestors()], ["", "tech-design"])
        self.assertEqual(tree.resolve_link(node, "https://example.com"), "https://example.com")
        self.assertEqual(tree.resolve_link(node, "#anchor"), "#anchor")
        self.assertEqual(tree.resolve_link(node, "missing.md"), "missing.md")
        self.assertEqual(tree.resolve_link(node, "../index.md"), "/docs/")
        with self.assertRaises(DocError):
            tree.get("nope")
        hits = tree.search("coverage")
        self.assertIn("Troubleshooting", [n.title for n, _ in hits])
        self.assertEqual(tree.search(""), [])
        self.assertEqual(tree.search("zzzqqq"), [])

    def test_custom_tree_with_order_and_prefixes(self):
        self.write("docs/index.md", "# Home\n\nSee [b](20-b/index.md) and [a](10-a.md).\n")
        self.write("docs/10-a.md", "# Page A\n\ntext")
        self.write("docs/20-b/index.md", "# Section B\n")
        self.write("docs/20-b/05-deep.md", "# Deep\n\n[up](../10-a.md#x)")
        self.write("docs/99-last.md", "# Last\n")
        self.write("docs/.order", "99-last.md\n")
        self.write("docs/notes.txt", "ignored")
        tree = DocTree(self.root / "docs")
        self.assertEqual([c.slug for c in tree.tree.children], ["last", "a", "b"])
        self.assertEqual(tree.get("b/deep").title, "Deep")
        _, html, _ = tree.page("b/deep")
        self.assertIn('href="/docs/a#x"', html)
        _, html, _ = tree.page("")
        self.assertIn('href="/docs/b"', html)
        self.assertIn('href="/docs/a"', html)
        empty = DocTree(self.root / "nowhere")
        self.assertEqual(empty.tree.children, [])
        with self.assertRaises(DocError):
            empty.page("")


class DocsTabTests(TempProjectTestCase):
    def setUp(self):
        super().setUp()
        self.settings.config_file = self.root / "config.ini"
        self.settings.mail_provider = self.settings.sms_provider = "memory"
        save_settings(self.settings, self.settings.config_file)
        self.settings = load_settings(self.settings.config_file)
        UserStore(self.settings).create_user("viewer", "Viewer-Pass-1", role="viewer")
        self.app = create_app(self.settings, bootstrap=False)
        self.app.config["TESTING"] = True
        self.client = self.app.test_client()

    def test_docs_pages(self):
        self.assertEqual(self.client.get("/docs").status_code, 302)             # sign-in required
        self.client.post("/login", data={"username": "viewer", "password": "Viewer-Pass-1"})
        page = self.client.get("/docs").get_data(as_text=True)
        self.assertIn("Documentation", page)
        self.assertIn("User manual", page)
        self.assertIn('href="/docs/user-manual"', page)
        self.assertIn("Docs</a>", self.client.get("/").get_data(as_text=True))   # the nav tab
        page = self.client.get("/docs/tech-design/payments").get_data(as_text=True)
        self.assertIn("Stripe Checkout", page)
        self.assertIn("On this page", page)
        self.assertIn('class="current"', page)
        page = self.client.get("/docs/user-manual").get_data(as_text=True)
        self.assertIn("In this section", page)
        self.assertEqual(self.client.get("/docs/nope/nothing").status_code, 404)
        page = self.client.get("/docs?q=coverage").get_data(as_text=True)
        self.assertIn("Troubleshooting", page)
        page = self.client.get("/docs?q=zzzqqq").get_data(as_text=True)
        self.assertIn("Nothing found", page)


if __name__ == "__main__":
    unittest.main()
