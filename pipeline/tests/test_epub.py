"""EPUB ingest tests. Uses the committed ``pipeline/samples/sample.epub`` for
the base contract and builds fresh throwaway EPUBs (via ``ebooklib``) in
``tmp_path`` for edge cases the fixture doesn't cover. Run:
pytest pipeline/tests/test_epub.py -q
"""

from __future__ import annotations

import os

import pytest
from ebooklib import epub

from pipeline.ingest import RawBook, ingest_book, ingest_epub

_SAMPLE = os.path.join(os.path.dirname(__file__), "..", "samples", "sample.epub")


def _all_text(book: RawBook) -> str:
    return "\n".join(p for c in book.chapters for p in c.paragraphs)


# ---- the committed sample.epub -----------------------------------------------

def test_sample_epub_chapter_count_and_titles():
    book = ingest_epub(_SAMPLE)
    assert [c.title for c in book.chapters] == [
        "Chapter One: Departure",
        "Chapter Two: The Reckoning",
        "Chapter Three: Home Waters",
    ]


def test_sample_epub_metadata_from_opf():
    book = ingest_epub(_SAMPLE)
    assert book.title == "The Salt Anchor"
    assert book.author == "Mira Voss"


def test_sample_epub_paragraph_text_is_extracted_and_whitespace_normalized():
    book = ingest_epub(_SAMPLE)
    ch1 = book.chapters[0]
    assert len(ch1.paragraphs) == 2
    assert ch1.paragraphs[0].startswith("The tide had turned before dawn")
    assert ch1.paragraphs[1].startswith("She had promised her father")
    # No raw HTML, no doubled whitespace from the source's indentation.
    assert "<" not in ch1.paragraphs[0]
    assert "  " not in ch1.paragraphs[0]


def test_sample_epub_scene_break_from_hr():
    book = ingest_epub(_SAMPLE)
    ch2 = book.chapters[1]
    assert ch2.scene_starts == [1]  # the <hr/> sits between the two <p>s


def test_sample_epub_footnote_and_marker_are_dropped():
    book = ingest_epub(_SAMPLE)
    text = _all_text(book)
    assert "reckoning, in harbor law" not in text.lower()
    # The <sup><a epub:type="noteref">1</a></sup> sits between two sentences in
    # the same <p>; once stripped the sentences join with a plain space, not a
    # stray "1" (or the footnote number glued onto the next word).
    ch2_first = book.chapters[1].paragraphs[0]
    assert ch2_first == (
        "By the time the harbor master arrived, Wren had already decided what "
        "she would say. The ledger was wrong, and everyone on the dock knew "
        "it, but no one had said so aloud in the three years since her father "
        "took over the books."
    )
    assert "1 The ledger" not in ch2_first
    assert "say.1" not in ch2_first


def test_sample_epub_nav_and_cover_are_not_chapters():
    book = ingest_epub(_SAMPLE)
    titles = [c.title for c in book.chapters]
    assert "Cover" not in titles
    assert not any("salt anchor" in p.lower() and "tide" not in p.lower()
                   for c in book.chapters for p in c.paragraphs)


# ---- ingest_book dispatch -----------------------------------------------------

def test_ingest_book_dispatches_epub(tmp_path):
    from pipeline.ingest import ingest_txt
    direct = ingest_epub(_SAMPLE)
    via_dispatch = ingest_book(_SAMPLE)
    assert via_dispatch.model_dump() == direct.model_dump()


def test_ingest_book_dispatches_txt(tmp_path):
    p = tmp_path / "b.txt"
    p.write_text("Chapter One\n\nHello there.", encoding="utf-8")
    from pipeline.ingest import ingest_txt
    assert ingest_book(str(p)).model_dump() == ingest_txt(str(p)).model_dump()


@pytest.mark.parametrize("ext", [".md", ".markdown", ".text"])
def test_ingest_book_dispatches_other_text_extensions(tmp_path, ext):
    p = tmp_path / f"b{ext}"
    p.write_text("Chapter One\n\nHello there.", encoding="utf-8")
    book = ingest_book(str(p))
    assert book.chapters[0].title == "Chapter One"


def test_ingest_book_rejects_unsupported_extension(tmp_path):
    p = tmp_path / "b.pdf"
    p.write_text("whatever", encoding="utf-8")
    with pytest.raises(ValueError, match="unsupported"):
        ingest_book(str(p))


# ---- edge cases built fresh, in tmp_path --------------------------------------

def _write_epub(tmp_path, name: str, items: list, spine: list, toc=()) -> str:
    book = epub.EpubBook()
    book.set_identifier(f"edge-{name}")
    book.set_title("Edge Cases")
    book.set_language("en")
    book.add_author("Test Author")
    for item in items:
        book.add_item(item)
    book.add_item(epub.EpubNav())
    book.add_item(epub.EpubNcx())
    book.toc = tuple(toc)
    book.spine = spine
    path = str(tmp_path / name)
    epub.write_epub(path, book)
    return path


def _html(inner: str) -> str:
    return f"<html><body>{inner}</body></html>"


def test_epub_skips_titlepage_by_epub_type(tmp_path):
    titlepage = epub.EpubHtml(title="Title Page", file_name="titlepage.xhtml", lang="en")
    titlepage.content = _html("<section epub:type='titlepage'><p>Edge Cases</p></section>")
    ch1 = epub.EpubHtml(title="One", file_name="chap_01.xhtml", lang="en")
    ch1.content = _html("<h1>Real Chapter</h1><p>" + ("word " * 45) + "</p>")

    path = _write_epub(tmp_path, "titlepage.epub", [titlepage, ch1], [titlepage, "nav", ch1])
    book = ingest_epub(path)
    assert len(book.chapters) == 1
    assert book.chapters[0].title == "Real Chapter"


def test_epub_skips_documents_under_the_word_minimum(tmp_path):
    tiny = epub.EpubHtml(title="Tiny", file_name="tiny.xhtml", lang="en")
    tiny.content = _html("<h2>Too Short</h2><p>Only a few words here.</p>")
    ch1 = epub.EpubHtml(title="One", file_name="chap_01.xhtml", lang="en")
    ch1.content = _html("<h1>Real Chapter</h1><p>" + ("word " * 45) + "</p>")

    path = _write_epub(tmp_path, "tiny.epub", [tiny, ch1], [tiny, "nav", ch1])
    book = ingest_epub(path)
    assert [c.title for c in book.chapters] == ["Real Chapter"]


def test_epub_merges_headingless_fragment_into_previous_chapter(tmp_path):
    ch1 = epub.EpubHtml(title="One", file_name="chap_01.xhtml", lang="en")
    ch1.content = _html("<h1>Real Chapter One</h1><p>" + ("word " * 45) + "</p>")
    interlude = epub.EpubHtml(title="Interlude", file_name="interlude.xhtml", lang="en")
    interlude.content = _html("<p>A short interlude with no heading at all here.</p>")

    path = _write_epub(
        tmp_path, "frontmatter.epub", [ch1, interlude], ["nav", ch1, interlude]
    )
    book = ingest_epub(path)
    assert len(book.chapters) == 1
    assert len(book.chapters[0].paragraphs) == 2
    assert book.chapters[0].paragraphs[1].startswith("A short interlude")


def test_epub_headingless_fragment_with_no_prior_chapter_is_dropped_if_too_short(tmp_path):
    epigraph = epub.EpubHtml(title="Epigraph", file_name="epigraph.xhtml", lang="en")
    epigraph.content = _html("<p>Not all who wander are lost.</p>")
    ch1 = epub.EpubHtml(title="One", file_name="chap_01.xhtml", lang="en")
    ch1.content = _html("<h1>Real Chapter One</h1><p>" + ("word " * 45) + "</p>")

    path = _write_epub(
        tmp_path, "epigraph.epub", [epigraph, ch1], [epigraph, "nav", ch1]
    )
    book = ingest_epub(path)
    # No prior chapter exists for the epigraph to merge into, and on its own
    # it's under the word minimum, so it's dropped rather than becoming a
    # bogus first chapter.
    assert [c.title for c in book.chapters] == ["Real Chapter One"]


def test_epub_title_falls_back_to_toc_entry_when_no_heading(tmp_path):
    ch2 = epub.EpubHtml(title="Second", file_name="chap_02.xhtml", lang="en")
    # No heading, 3 paragraphs (too many to merge as front matter), >=40 words.
    ch2.content = _html("".join(f"<p>{'filler word here today ' * 4}</p>" for _ in range(3)))

    path = _write_epub(
        tmp_path, "toctitle.epub", [ch2], ["nav", ch2],
        toc=[epub.Link("chap_02.xhtml", "Chapter Two (from TOC)", "ch2")],
    )
    book = ingest_epub(path)
    assert book.chapters[0].title == "Chapter Two (from TOC)"


def test_epub_title_falls_back_to_chapter_number_with_no_heading_or_toc(tmp_path):
    ch = epub.EpubHtml(title="X", file_name="chap_x.xhtml", lang="en")
    ch.content = _html("".join(f"<p>{'filler word here today ' * 4}</p>" for _ in range(3)))

    path = _write_epub(tmp_path, "nofallback.epub", [ch], ["nav", ch])
    book = ingest_epub(path)
    assert book.chapters[0].title == "Chapter 1"


def test_epub_nonlinear_spine_items_are_skipped(tmp_path):
    ch1 = epub.EpubHtml(title="One", file_name="chap_01.xhtml", lang="en")
    ch1.content = _html("<h1>Real Chapter One</h1><p>" + ("word " * 45) + "</p>")
    aside_doc = epub.EpubHtml(title="Ad", file_name="ad.xhtml", lang="en")
    aside_doc.content = _html("<h2>An Aside</h2><p>" + ("filler " * 45) + "</p>")

    path = _write_epub(
        tmp_path, "nonlinear.epub", [ch1, aside_doc],
        ["nav", ch1, (aside_doc, "no")],
    )
    book = ingest_epub(path)
    assert [c.title for c in book.chapters] == ["Real Chapter One"]
