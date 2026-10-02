"""Ingestion: a book file -> a ``RawBook`` (chapters of paragraphs, with scene
breaks marked). ``ingest_txt`` handles plain ``.txt``/markdown. ``ingest_epub``
handles structure-rich EPUB (``ebooklib`` + ``beautifulsoup4``, ``html.parser`` —
no ``lxml`` dependency of our own, though ``ebooklib`` pulls it in transitively
for its own OPF/NCX parsing). PDF is next (messy: de-hyphenation + header/footer
stripping) — it slots in behind the same ``RawBook`` interface without touching
downstream stages. ``ingest_book`` dispatches on file extension.
"""

from __future__ import annotations

import os
import re

import ebooklib
from bs4 import BeautifulSoup
from ebooklib import epub
from pydantic import BaseModel, Field

# A chapter heading is a SHORT standalone line that starts with Chapter/Part/Book
# + a number/word, OR a markdown heading, OR a lone Roman numeral. The length +
# "doesn't end in sentence punctuation" guards stop prose like
# "Chapter one of my life was hard." from being mistaken for a heading.
# The second group is front/back matter: a real book puts "Dedication" and
# "About the Author" on their own page, and reading them as ordinary sentences in
# the middle of chapter one (which is what happened before) is exactly the defect
# `mark_matter` exists to catch. They are only headings under the same guards as
# the rest: short, and not punctuated like a sentence.
_HEADING_KEYWORD = re.compile(
    r"^\s*(?:chapter|part|book|prologue|epilogue|interlude"
    r"|dedication|copyright|contents|acknowledge?ments?|acknowledgements?"
    r"|about the author|also by|epigraph|colophon|foreword|preface|afterword"
    r"|appendix)\b",
    re.IGNORECASE,
)
_MARKDOWN_HEADING = re.compile(r"^\s*#{1,6}\s+\S")
_ROMAN_ONLY = re.compile(r"^\s*[IVXLCDM]{1,7}\.?\s*$")
_SCENE_BREAK = re.compile(r"^\s*(?:\*\s*){3,}\s*$|^\s*(?:[-–—·•*]\s*){3,}\s*$|^\s*#{3,}\s*$")


class RawChapter(BaseModel):
    title: str = ""
    paragraphs: list[str] = Field(default_factory=list)
    # Paragraph indices at which a new scene begins (from `* * *` style breaks).
    scene_starts: list[int] = Field(default_factory=list)
    kind: str = "chapter"  # "chapter" | "front_matter" | "back_matter"
    include_in_audio: bool = True


class RawBook(BaseModel):
    title: str = ""
    author: str = ""
    chapters: list[RawChapter] = Field(default_factory=list)


# ---- front / back matter -----------------------------------------------------

# Titles that name a non-narrative page. Matched on the title's first words, so
# "Also by Ada Fenwick" and "About the Author" both land.
_FRONT_TITLES = (
    "copyright", "dedication", "contents", "table of contents", "acknowledgement",
    "acknowledgment", "about the author", "also by", "epigraph", "title page",
    "colophon", "praise for", "front matter", "imprint", "half title",
)
_BACK_TITLES = (
    "about the author", "acknowledgement", "acknowledgment", "also by",
    "afterword", "back matter", "reading group guide", "book club",
)
# Phrases that make a page copyright boilerplate whatever its length.
_BOILERPLATE = ("all rights reserved", "copyright ©", "isbn")
_FRONT_MATTER_MAX_WORDS = 120


def _title_matches(title: str, options: tuple[str, ...]) -> bool:
    low = re.sub(r"^\s*[#\d\W]+", "", title).strip().lower()
    return any(low.startswith(opt) for opt in options)


def _word_count(chapter: RawChapter) -> int:
    return sum(len(p.split()) for p in chapter.paragraphs)


def _looks_like_front_matter(chapter: RawChapter, *, is_first: bool) -> bool:
    named = _title_matches(chapter.title, _FRONT_TITLES)
    if chapter.title and not named:
        return False  # a real title ("Chapter One", "The Reckoning") — narrative
    body = " ".join(chapter.paragraphs).lower()
    if any(phrase in body for phrase in _BOILERPLATE):
        return True
    if not named and not is_first:
        # An untitled block that is merely *short* only reads as front matter at
        # the very top of the book. Deeper in, it is far likelier to be a short
        # chapter (some novels have one-page chapters) and silencing it would be
        # the worse mistake.
        return False
    return _word_count(chapter) < _FRONT_MATTER_MAX_WORDS


def mark_matter(book: RawBook) -> RawBook:
    """Label leading front matter and trailing back matter, in place.

    The defect this fixes: a copyright page becomes "Chapter 1", is narrated in
    the narrator's voice, and shifts every chapter number in the book (and in the
    exported chapter markers) by one, permanently.

    Leading: an untitled — or Copyright/Dedication/Contents/… titled — chapter
    that is either short (< 120 words) or carries copyright boilerplate becomes
    ``front_matter`` and drops out of the audio. Trailing: About the author /
    Acknowledgements / Also by becomes ``back_matter`` and **stays** in the audio
    (people do listen to those), it is just no longer a numbered chapter.

    Conservative by construction: it never labels the only chapter, and never
    labels every chapter — a book has to keep a body. It stops at the first
    chapter that looks narrative, so front matter buried mid-book is left alone.
    """
    chapters = book.chapters
    if len(chapters) < 2:
        return book

    for index, chapter in enumerate(chapters[:-1]):
        if not _looks_like_front_matter(chapter, is_first=index == 0):
            break
        chapter.kind = "front_matter"
        chapter.include_in_audio = False

    for chapter in reversed(chapters):
        if chapter.kind != "chapter":
            break
        if not _title_matches(chapter.title, _BACK_TITLES):
            break
        chapter.kind = "back_matter"

    if all(c.kind != "chapter" for c in chapters):  # never label the whole book
        for chapter in chapters:
            chapter.kind, chapter.include_in_audio = "chapter", True
    return book


def _is_heading(line: str) -> bool:
    stripped = line.strip()
    if not stripped:
        return False
    if _MARKDOWN_HEADING.match(line):
        return True
    if _ROMAN_ONLY.match(line) and len(stripped) <= 8:
        return True
    if _HEADING_KEYWORD.match(line):
        # Real headings are short and don't end like a sentence.
        return len(stripped) <= 60 and stripped[-1] not in ".!?,;"
    return False


def _clean_heading(line: str) -> str:
    return re.sub(r"^\s*#{1,6}\s+", "", line).strip()


def ingest_txt(path: str) -> RawBook:
    with open(path, encoding="utf-8") as fh:
        raw = fh.read()
    raw = raw.replace("\r\n", "\n").replace("\r", "\n")

    chapters: list[RawChapter] = []
    current = RawChapter()
    para_lines: list[str] = []

    def flush_paragraph() -> None:
        if para_lines:
            # Preserve internal line breaks *and* leading whitespace: prose
            # chunking collapses both, but verse chunking needs the breaks and
            # the verse *fingerprint* (build._looks_like_verse) needs the indent
            # — an indented four-line block inside a novel is a poem, and it used
            # to arrive here already flattened.
            text = "\n".join(s.rstrip() for s in para_lines).strip("\n")
            if text.strip():
                current.paragraphs.append(text)
            para_lines.clear()

    def flush_chapter() -> None:
        flush_paragraph()
        if current.paragraphs or current.title:
            chapters.append(current)

    for line in raw.split("\n"):
        if _is_heading(line):
            flush_chapter()
            current = RawChapter(title=_clean_heading(line))
        elif _SCENE_BREAK.match(line):
            flush_paragraph()
            # Mark the *next* paragraph as the start of a new scene.
            current.scene_starts.append(len(current.paragraphs))
        elif line.strip() == "":
            flush_paragraph()
        else:
            para_lines.append(line)

    flush_chapter()

    if not chapters:
        chapters = [RawChapter()]
    return mark_matter(RawBook(chapters=chapters))


# ---- EPUB ingestion ----------------------------------------------------------

# Any of these on an item's own class (ebooklib already classifies EPUB3
# `properties="nav"`/`"cover"` manifest items into EpubNav/EpubCoverHtml at
# parse time) or its `epub:type` / filename mark it as structural, not content.
_SKIP_EPUB_TYPE = re.compile(r"\b(toc|landmarks|cover|titlepage|title-page)\b", re.IGNORECASE)
_SKIP_FILENAME = re.compile(r"nav|toc|cover|title", re.IGNORECASE)
_FOOTNOTE_EPUB_TYPE = re.compile(r"\b(footnote|noteref)\b", re.IGNORECASE)

_MIN_CHAPTER_WORDS = 40  # below this, treat the document as front matter/boilerplate
_MAX_FRONT_MATTER_PARAGRAPHS = 2  # headingless doc this short merges into the previous chapter

_EPUB_BLOCK_TAGS = ("p", "blockquote", "li", "pre", "div")


def _strip_epub_footnotes(soup: BeautifulSoup) -> None:
    """Remove footnote asides/markers in place so their text never leaks into a
    paragraph: ``<aside>`` (regardless of markup), anything tagged
    ``epub:type="footnote"``/``"noteref"``, and ``<sup>`` footnote-number
    markers."""
    for tag in soup.find_all(True):
        if tag.parent is None:  # already removed by an ancestor's decompose()
            continue
        epub_type = tag.get("epub:type", "")
        if tag.name in ("aside", "sup") or (epub_type and _FOOTNOTE_EPUB_TYPE.search(epub_type)):
            tag.decompose()


def _has_skip_epub_type(soup: BeautifulSoup) -> bool:
    for tag in soup.find_all(attrs={"epub:type": True}):
        if _SKIP_EPUB_TYPE.search(tag.get("epub:type", "")):
            return True
    return False


def _epub_leaf_blocks(body) -> list:
    """Every ``p``/``blockquote``/``li``/``pre``/bare-text ``div`` in document
    order, plus ``hr``. A container is skipped in favour of its own block
    children so e.g. ``<li><p>...</p></li>`` isn't counted twice."""
    out = []
    for el in body.find_all(list(_EPUB_BLOCK_TAGS) + ["hr"]):
        if el.name != "hr" and el.find(_EPUB_BLOCK_TAGS) is not None:
            continue  # has block children of its own; they supply the text
        out.append(el)
    return out


def _epub_heading(body) -> str | None:
    h = body.find(["h1", "h2", "h3"])
    if h is None:
        return None
    text = re.sub(r"\s+", " ", h.get_text(" ", strip=True)).strip()
    return text or None


def _epub_paragraphs_and_scenes(body) -> tuple[list[str], list[int]]:
    paragraphs: list[str] = []
    scene_starts: list[int] = []
    for el in _epub_leaf_blocks(body):
        if el.name == "hr":
            if len(paragraphs) not in scene_starts:
                scene_starts.append(len(paragraphs))
            continue
        text = re.sub(r"\s+", " ", el.get_text(" ", strip=True)).strip()
        if not text:
            continue
        if _SCENE_BREAK.match(text):
            if len(paragraphs) not in scene_starts:
                scene_starts.append(len(paragraphs))
            continue
        paragraphs.append(text)
    return paragraphs, scene_starts


def _epub_toc_href_map(toc) -> dict[str, str]:
    """Flatten ``EpubBook.toc`` (a tree of ``Link``/``Section``, arbitrarily
    nested in lists/tuples) into ``{href_without_fragment: title}``."""
    out: dict[str, str] = {}

    def walk(node) -> None:
        if node is None:
            return
        if isinstance(node, epub.Link):
            out[node.href.split("#")[0]] = node.title
        elif isinstance(node, (list, tuple)):
            for child in node:
                walk(child)
        # epub.Section carries no href of its own; its children (if any) are
        # walked because the caller passes (Section, children) as a 2-tuple.

    walk(toc)
    return out


def _is_structural_epub_item(item, name: str) -> bool:
    """EPUB3 nav property or EPUB3/2 cover property (ebooklib already turns
    either into a distinct class at parse time) or a nav/toc/cover/title
    filename — these never carry chapter content."""
    if isinstance(item, (epub.EpubNav, epub.EpubCoverHtml)):
        return True
    return bool(_SKIP_FILENAME.search(os.path.basename(name)))


def ingest_epub(path: str) -> RawBook:
    """EPUB -> the same ``RawBook`` shape ``ingest_txt`` produces, so nothing
    downstream (classify/clean/chunk/build) needs to know the source format.

    Walks the spine in reading order (skipping non-linear items), drops
    nav/toc/cover/title-page documents and anything with under ~40 words of
    body text, merges a headingless document with only a couple of paragraphs
    into the previous chapter (front-matter fragments — epigraphs,
    dedications), and otherwise titles a chapter from its first
    ``h1``/``h2``/``h3``, else the table of contents entry for its href, else
    ``"Chapter N"``. Footnote asides/markers are stripped before paragraph
    text is extracted; scene breaks come from ``<hr>`` and from break-only
    lines (``* * *``, ``***``, ``— — —``, matching ``ingest_txt``'s rule).
    """
    book = epub.read_epub(path)
    toc_map = _epub_toc_href_map(book.toc)

    chapters: list[RawChapter] = []
    chapter_number = 0

    for idref, linear in book.spine:
        if linear == "no":
            continue
        item = book.get_item_with_id(idref)
        if item is None or item.get_type() != ebooklib.ITEM_DOCUMENT:
            continue
        name = item.get_name()
        if _is_structural_epub_item(item, name):
            continue

        soup = BeautifulSoup(item.get_content(), "html.parser")
        if _has_skip_epub_type(soup):
            continue
        _strip_epub_footnotes(soup)
        body = soup.find("body") or soup

        heading = _epub_heading(body)
        paragraphs, scene_starts = _epub_paragraphs_and_scenes(body)

        # A headingless, short document merges into the chapter before it
        # (front matter: an epigraph, a dedication) — checked *before* the
        # word-count gate below, since these are often well under 40 words
        # and would otherwise be dropped instead of preserved. Only applies
        # once a previous chapter exists to merge into; a headingless
        # fragment with nothing before it falls through to the normal gate.
        if heading is None and len(paragraphs) <= _MAX_FRONT_MATTER_PARAGRAPHS and chapters:
            prev = chapters[-1]
            offset = len(prev.paragraphs)
            prev.paragraphs.extend(paragraphs)
            prev.scene_starts.extend(offset + s for s in scene_starts)
            continue

        word_count = sum(len(p.split()) for p in paragraphs)
        if word_count < _MIN_CHAPTER_WORDS:
            continue  # boilerplate: an under-length page that isn't otherwise flagged

        chapter_number += 1
        title = heading or toc_map.get(name) or f"Chapter {chapter_number}"
        chapters.append(RawChapter(title=title, paragraphs=paragraphs, scene_starts=scene_starts))

    if not chapters:
        chapters = [RawChapter()]

    titles = book.get_metadata("DC", "title")
    creators = book.get_metadata("DC", "creator")
    title = titles[0][0] if titles else ""
    author = ", ".join(c[0] for c in creators if c[0]) if creators else ""

    return mark_matter(RawBook(title=title, author=author, chapters=chapters))


_TEXT_EXTENSIONS = {".txt", ".md", ".markdown", ".text"}


def ingest_book(path: str) -> RawBook:
    """Dispatch on file extension: the single entry point ``build.py`` (and
    the studio) calls regardless of source format."""
    ext = os.path.splitext(path)[1].lower()
    if ext in _TEXT_EXTENSIONS:
        return ingest_txt(path)
    if ext == ".epub":
        return ingest_epub(path)
    raise ValueError(
        f"unsupported book format: {ext!r} (expected one of "
        f"{sorted(_TEXT_EXTENSIONS)} or '.epub')"
    )
