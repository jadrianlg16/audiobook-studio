"""Build ``pipeline/samples/sample.epub`` — a small, deliberately awkward EPUB3
used to exercise ``pipeline.ingest.ingest_epub``: three real chapters, an
EPUB3 nav doc, a cover page, one ``<hr>`` scene break, a footnote (aside +
sup marker + noteref), and DC title/author metadata.

    python -m pipeline.samples.make_sample_epub

Regenerate after changing the fixture; the output is small (a few KB) and is
committed so tests don't depend on network/build tooling to produce it.
"""

from __future__ import annotations

import os

from ebooklib import epub

_OUT = os.path.join(os.path.dirname(__file__), "sample.epub")


def build() -> epub.EpubBook:
    book = epub.EpubBook()
    book.set_identifier("audiobook-ai-generator-sample-epub")
    book.set_title("The Salt Anchor")
    book.set_language("en")
    book.add_author("Mira Voss")

    cover = epub.EpubHtml(title="Cover", file_name="cover.xhtml", lang="en")
    cover.content = (
        "<html><body><div id='cover-image'><p>The Salt Anchor</p></div></body></html>"
    )
    cover.properties = ["cover"]
    book.add_item(cover)

    ch1 = epub.EpubHtml(title="Chapter One: Departure", file_name="chap_01.xhtml", lang="en")
    ch1.content = """
    <html><body>
      <h1>Chapter One: Departure</h1>
      <p>The tide had turned before dawn, and Wren stood at the end of the pier
      counting the boats that had not come back. Salt crusted the ropes in pale
      rings, and gulls wheeled overhead with a patience that unsettled her more
      than any storm.</p>
      <p>She had promised her father she would wait until noon. Noon came and
      went, and still she waited, because waiting was the only thing left that
      still felt like keeping a promise.</p>
    </body></html>
    """
    book.add_item(ch1)

    ch2 = epub.EpubHtml(title="Chapter Two: The Reckoning", file_name="chap_02.xhtml", lang="en")
    ch2.content = """
    <html><body>
      <h1>Chapter Two: The Reckoning</h1>
      <p>By the time the harbor master arrived, Wren had already decided what
      she would say.<sup><a epub:type="noteref" href="#fn1">1</a></sup> The
      ledger was wrong, and everyone on the dock knew it, but no one had said
      so aloud in the three years since her father took over the books.</p>
      <hr/>
      <p>The meeting lasted four minutes. It felt like an hour, and when it
      ended, Wren walked home along the seawall instead of the road, letting
      the spray remind her that some things could not be argued with, only
      outlasted.</p>
      <aside epub:type="footnote" id="fn1">
        <p>A reckoning, in harbor law, is a public accounting of debts owed
        against the season's catch.</p>
      </aside>
    </body></html>
    """
    book.add_item(ch2)

    ch3 = epub.EpubHtml(title="Chapter Three: Home Waters", file_name="chap_03.xhtml", lang="en")
    ch3.content = """
    <html><body>
      <h1>Chapter Three: Home Waters</h1>
      <p>Years later, Wren would tell the story differently depending on who
      asked, trimming it shorter for children and longer for the old fishermen
      who remembered the reckoning themselves.</p>
      <p>But the ending never changed: the boats came back, one by one,
      smelling of diesel and kelp, and her father wept on the pier in a way
      she had never seen him weep before or since.</p>
    </body></html>
    """
    book.add_item(ch3)

    nav = epub.EpubNav()
    book.add_item(nav)
    ncx = epub.EpubNcx()
    book.add_item(ncx)

    book.toc = (
        epub.Link("chap_01.xhtml", "Chapter One: Departure", "ch1"),
        epub.Link("chap_02.xhtml", "Chapter Two: The Reckoning", "ch2"),
        epub.Link("chap_03.xhtml", "Chapter Three: Home Waters", "ch3"),
    )
    book.spine = [cover, "nav", ch1, ch2, ch3]

    return book


def main() -> None:
    epub.write_epub(_OUT, build())
    print(f"wrote {_OUT} ({os.path.getsize(_OUT)} bytes)")


if __name__ == "__main__":
    main()
