"""Сайт-визитка docs/site: HTML разбирается, разделы на месте, ссылки живые, всё локальное.

Страницу и тесты написала команда Capricorn в живом прогоне."""
from html.parser import HTMLParser
from pathlib import Path
from urllib.parse import unquote, urlsplit

import pytest


ROOT = Path(__file__).resolve().parents[1]
PAGE = ROOT / "docs" / "site" / "index.html"
VOID_TAGS = {"area", "base", "br", "col", "embed", "hr", "img", "input",
             "link", "meta", "param", "source", "track", "wbr"}


class SiteParser(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.elements = []
        self.stack = []
        self.ids = set()
        self.doctype = None

    def handle_decl(self, decl):
        self.doctype = decl.lower()

    def handle_starttag(self, tag, attrs):
        attrs = dict(attrs)
        self.elements.append((tag, attrs))
        if "id" in attrs:
            assert attrs["id"] not in self.ids, "Duplicate HTML id"
            self.ids.add(attrs["id"])
        if tag not in VOID_TAGS:
            self.stack.append(tag)

    def handle_startendtag(self, tag, attrs):
        self.handle_starttag(tag, attrs)
        if tag not in VOID_TAGS:
            self.handle_endtag(tag)

    def handle_endtag(self, tag):
        assert self.stack and self.stack[-1] == tag, f"Unmatched closing tag {tag}"
        self.stack.pop()


@pytest.fixture
def page():
    parser = SiteParser()
    parser.feed(PAGE.read_text(encoding="utf-8"))
    parser.close()
    return parser


def test_html_parses(page):
    assert page.doctype == "doctype html"
    assert not page.stack, "Unclosed HTML tags"
    tags = {tag for tag, _ in page.elements}
    assert {"html", "head", "body", "header", "main", "section", "footer"} <= tags


def test_required_sections(page):
    sections = {attrs.get("id") for tag, attrs in page.elements if tag == "section"}
    assert {"about", "team", "features", "install", "hotkeys"} <= sections


def test_local_links_and_files(page):
    for _, attrs in page.elements:
        for attribute in ("src", "href"):
            if attribute not in attrs:
                continue
            reference = urlsplit(attrs[attribute])
            if reference.scheme or reference.netloc:
                continue
            target = (PAGE.parent / unquote(reference.path)).resolve() if reference.path else PAGE
            assert target.is_file(), f"Missing local file {target}"
            if reference.fragment:
                target_page = page
                if target != PAGE:
                    target_page = SiteParser()
                    target_page.feed(target.read_text(encoding="utf-8"))
                    target_page.close()
                assert unquote(reference.fragment) in target_page.ids


def test_styles_and_scripts_are_local(page):
    for tag, attrs in page.elements:
        if tag == "script" or (tag == "link" and "stylesheet" in attrs.get("rel", "").split()):
            for attribute in ("src", "href"):
                if attribute in attrs:
                    reference = urlsplit(attrs[attribute])
                    assert not reference.scheme and not reference.netloc
    css = PAGE.parent / "style.css"
    assert css.is_file()
    stylesheet = css.read_text(encoding="utf-8").lower()
    assert "@import" not in stylesheet
    assert "url(" not in stylesheet
