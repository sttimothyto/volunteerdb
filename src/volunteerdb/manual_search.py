"""Semantic search of the manual, in process.

The built manual (Sphinx + Furo HTML, served at /manual) is cut into one
chunk per section and searched two ways at once: a hand-rolled BM25 over the
words, and a cosine over ``model2vec`` static embeddings (numpy-only; the
model is ``manual_model``'s pin), fused by reciprocal rank. The keywords find
the page that says "password"; the vectors find it for "I forgot how to sign
in". Nothing here calls an API, and without the model the keyword half still
answers, so a development checkout that never ran ``make model`` has a
working, plainer search rather than a broken one.

Top level rather than ``services/`` or ``ui/``: an index loader reads the
disk and imports a model, which the purity core forbids, and it renders
nothing. It still follows the core's rules -- ``docs_dir`` and ``model_dir``
arrive as parameters, ``settings()`` is never read, and nothing raises: a
missing manual is an empty index, a missing model is one warning and
keyword-only results.

The index is built once, lazily, by ``ManualIndexCell``: the test harness
assembles the app some hundred and fifty times per run and must not pay for
an index each time, and production warms it at startup so the first search
never does.
"""

from __future__ import annotations

import asyncio
import math
import re
import sys
import time
from collections import Counter
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from html.parser import HTMLParser
from pathlib import Path
from typing import TYPE_CHECKING, Protocol

import anyio.to_thread
import structlog

from . import manual_model

if TYPE_CHECKING:
    import numpy as np

log = structlog.get_logger(__name__)

# A vector hit below this cosine is noise. Measured on the built manual with
# potion-base-8M (2026-08-30: 86 pages, 689 sections, 28 queries): the section
# that answers a query scores 0.44-0.87 and the rest of its page 0.45-0.78
# behind it, while a query the manual has no page for peaks at 0.29-0.37
# ("pizza recipe", "asdf qwer zxcv", "deploy" asked of the end-user guide).
# 0.40 sits in that gap. The floor matters more than it looks: reciprocal
# rank fusion weighs the vectors' first hit as heavily as the keywords'
# first, so at 0.35 "pizza recipe" answered "HTTP API › Worked examples".
# What 0.40 loses is sixth-and-lower vector hits that BM25 finds anyway.
# Tuned, not derived.
COSINE_FLOOR = 0.40
# Sphinx's own pages, not the manual's.
SKIP_FILES = frozenset({"search.html", "genindex.html", "py-modindex.html"})
# The chunker's floor for `python -m volunteerdb.manual_search`: the manual
# cuts into 86 pages and ~690 sections (2026-08-30), so a count under these
# means the markup changed under the chunker, not that somebody trimmed a page.
PAGE_FLOOR = 40
CHUNK_FLOOR = 150
EMBED_TEXT_LIMIT = 1500
# Sections that are lists of links to other pages, not content: every guide
# page ends with one, and a keyword-dense list of page titles outranks the
# page that actually answers. They are chunked, so their text still counts
# for nothing, and left out of the index.
NAVIGATION_HEADINGS = frozenset({"Related pages", "Next steps"})
_BM25_K1 = 1.5
_BM25_B = 0.75


def audience_of(page: str) -> str:
    """Who a page is for: the end-user guide (``guide/``) and the landing
    page are ``user``; every other page is the technical manual, ``dev``."""
    return "user" if page == "index" or page.startswith("guide/") else "dev"


@dataclass(frozen=True, slots=True)
class Chunk:
    """One section of one page: the unit a search returns."""

    page: str  # "how-to/deploy": the path under docs_dir without .html
    title: str  # the page's title
    headings: tuple[str, ...]  # from the page's h1 down to this section's own
    anchor: str  # the section id; "" for the page's top section
    audience: str
    text: str

    @property
    def url(self) -> str:
        fragment = f"#{self.anchor}" if self.anchor else ""
        return f"/manual/{self.page}.html{fragment}"

    @property
    def section(self) -> str:
        return " › ".join(h for h in self.headings[1:] if h)


# ---------------------------------------------------------------------------
# Chunking


# Tags whose start and end break words: "<dt>Symptom</dt><dd>The" must not
# read as "SymptomThe".
_BLOCK_TAGS = frozenset(
    {
        "p",
        "div",
        "li",
        "ul",
        "ol",
        "dl",
        "dt",
        "dd",
        "pre",
        "table",
        "thead",
        "tbody",
        "tr",
        "td",
        "th",
        "br",
        "hr",
        "blockquote",
        "section",
        "article",
        "aside",
        "nav",
        "header",
        "footer",
        "h1",
        "h2",
        "h3",
        "h4",
        "h5",
        "h6",
    }
)
_HEADING_TAGS = frozenset({"h1", "h2", "h3", "h5", "h4", "h6"})
_SKIPPED_TAGS = frozenset({"script", "style"})


class _Section:
    """A ``<section id>`` while it is open: its text so far, its heading,
    and where it sits."""

    __slots__ = ("anchor", "heading", "heading_parts", "parent", "parts")

    def __init__(self, anchor: str, parent: _Section | None) -> None:
        self.anchor = anchor
        self.parent = parent
        self.heading: str | None = None
        self.heading_parts: list[str] = []
        self.parts: list[str] = []

    def path(self) -> tuple[str, ...]:
        own = (self.heading or "",)
        return own if self.parent is None else self.parent.path() + own


class _FuroParser(HTMLParser):
    """A stack of the open ``<section id>`` elements. Text lands on the top
    of the stack, so a parent section never carries its children's words --
    a hit on the deploy page's "Roll back" section is that section, not the
    whole page. Text outside every section (the sidebar, the footer) is
    dropped, which is the point."""

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.sections: list[_Section] = []  # in document order
        self.title: str = ""
        self._open: list[_Section | None] = []  # None: a <section> with no id
        self._skip_until: str | None = None  # inside <script>/<style>
        self._in_headerlink = False
        self._in_heading = False
        self._in_title = False

    @property
    def _top(self) -> _Section | None:
        for section in reversed(self._open):
            if section is not None:
                return section
        return None

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if self._skip_until is not None:
            return
        if tag in _SKIPPED_TAGS:
            self._skip_until = tag
            return
        if tag == "title":
            self._in_title = True
            return
        attributes = dict(attrs)
        if tag == "section":
            anchor = attributes.get("id")
            section = _Section(anchor, self._top) if anchor else None
            self._open.append(section)
            if section is not None:
                self.sections.append(section)
        elif tag == "a" and "headerlink" in (attributes.get("class") or "").split():
            self._in_headerlink = True
        elif tag in _HEADING_TAGS:
            top = self._top
            if top is not None and top.heading is None:
                self._in_heading = True
        if tag in _BLOCK_TAGS:
            self._append(" ")

    def handle_endtag(self, tag: str) -> None:
        if self._skip_until is not None:
            if tag == self._skip_until:
                self._skip_until = None
            return
        if tag == "title":
            self._in_title = False
        elif tag == "section":
            if self._open:
                self._open.pop()
        elif tag == "a":
            self._in_headerlink = False
        elif tag in _HEADING_TAGS and self._in_heading:
            self._in_heading = False
            top = self._top
            if top is not None:
                top.heading = _squash("".join(top.heading_parts))
        if tag in _BLOCK_TAGS:
            self._append(" ")

    def handle_data(self, data: str) -> None:
        if self._skip_until is not None or self._in_headerlink:
            return
        if self._in_title:
            self.title += data
            return
        if self._in_heading:
            top = self._top
            if top is not None:
                top.heading_parts.append(data)
            return
        self._append(data)

    def _append(self, text: str) -> None:
        top = self._top
        if top is not None:
            top.parts.append(text)


def _squash(text: str) -> str:
    return " ".join(text.split())


def _page_title(parser: _FuroParser) -> str:
    for section in parser.sections:
        if section.heading:
            return section.heading
    # Furo's <title> is "Page title - Project"
    return _squash(parser.title).rsplit(" - ", 1)[0]


def chunk_html(html: str, page: str) -> list[Chunk]:
    """Every ``<section id>`` of one built page, in document order, each
    with only its own text. The first section is the page's top: its anchor
    is "" so the URL is the page itself, not a fragment on it."""
    parser = _FuroParser()
    parser.feed(html)
    parser.close()
    title = _page_title(parser)
    audience = audience_of(page)
    chunks = []
    for index, section in enumerate(parser.sections):
        anchor = "" if index == 0 and section.parent is None else section.anchor
        chunks.append(
            Chunk(
                page=page,
                title=title,
                headings=section.path(),
                anchor=anchor,
                audience=audience,
                text=_squash("".join(section.parts)),
            )
        )
    return chunks


def _is_page(relative: Path) -> bool:
    return not any(part.startswith("_") for part in relative.parts) and (
        relative.name not in SKIP_FILES
    )


def chunk_directory(docs_dir: Path) -> list[Chunk]:
    """The chunks of every page under a built manual, sorted by path.
    Sphinx's own pages and its ``_static``/``_sources`` trees are skipped;
    a directory that is not there is an empty manual."""
    if not docs_dir.is_dir():
        return []
    chunks: list[Chunk] = []
    pages = sorted(
        p.relative_to(docs_dir)
        for p in docs_dir.rglob("*.html")
        if _is_page(p.relative_to(docs_dir))
    )
    for relative in pages:
        page = relative.with_suffix("").as_posix()
        chunks.extend(chunk_html((docs_dir / relative).read_text(), page))
    return chunks


# ---------------------------------------------------------------------------
# Keywords: a tokenizer and BM25


_WORD = re.compile(r"\w+")


def fold(token: str) -> str:
    """Four suffix rules, applied in turn, so "settings", "setting" and
    "set"... land close enough: plural -ies/-s, then -ing/-ed, then a
    trailing e that -ing/-ed dropped. Not a stemmer -- "thing" folds to
    "th" -- but the query and the manual go through the same folding, and
    agreement is all a keyword index needs. The vectors carry the meaning."""
    if len(token) > 4 and token.endswith("ies"):
        token = token[:-3] + "y"
    elif len(token) > 3 and token.endswith("s") and not token.endswith("ss"):
        token = token[:-1]
    if len(token) > 5 and token.endswith("ing"):
        token = token[:-3]
    elif len(token) > 4 and token.endswith("ed"):
        token = token[:-2]
    if len(token) > 4 and token.endswith("e"):
        token = token[:-1]
    return token


def tokenize(text: str) -> list[str]:
    return [fold(t) for t in _WORD.findall(text.lower()) if len(t) >= 2]


@dataclass(frozen=True, slots=True)
class Bm25:
    postings: Mapping[str, tuple[tuple[int, int], ...]]  # term -> ((doc, tf), ...)
    doc_len: tuple[int, ...]
    avg_len: float


def bm25_build(docs: Sequence[Sequence[str]]) -> Bm25:
    postings: dict[str, list[tuple[int, int]]] = {}
    for doc_id, terms in enumerate(docs):
        for term, tf in Counter(terms).items():
            postings.setdefault(term, []).append((doc_id, tf))
    doc_len = tuple(len(terms) for terms in docs)
    avg_len = sum(doc_len) / len(doc_len) if doc_len else 0.0
    return Bm25({t: tuple(p) for t, p in postings.items()}, doc_len, avg_len)


def bm25_scores(bm25: Bm25, query: Sequence[str]) -> dict[int, float]:
    """Okapi BM25 (k1 = 1.5, b = 0.75) of every document that shares a term
    with the query; documents sharing none are absent, not zero."""
    n = len(bm25.doc_len)
    scores: dict[int, float] = {}
    for term in dict.fromkeys(query):  # each query term once
        postings = bm25.postings.get(term)
        if not postings:
            continue
        df = len(postings)
        idf = math.log(1.0 + (n - df + 0.5) / (df + 0.5))
        for doc_id, tf in postings:
            norm = 1.0 - _BM25_B + _BM25_B * bm25.doc_len[doc_id] / bm25.avg_len
            scores[doc_id] = scores.get(doc_id, 0.0) + idf * (
                tf * (_BM25_K1 + 1.0) / (tf + _BM25_K1 * norm)
            )
    return scores


def keyword_doc(chunk: Chunk) -> list[str]:
    """What BM25 sees of a chunk: its text, and its heading path twice --
    a section's own title is the strongest signal of what it is about."""
    return tokenize(chunk.text) + tokenize(" ".join(chunk.headings)) * 2


# ---------------------------------------------------------------------------
# Vectors


class Embedder(Protocol):
    def encode(self, texts: Sequence[str]) -> np.ndarray:
        """One unit-length row per text: (n, dim)."""
        ...


def _normalise(vectors: np.ndarray) -> np.ndarray:
    import numpy as np

    out = np.asarray(vectors, dtype=np.float32)
    norms = np.linalg.norm(out, axis=1, keepdims=True)
    norms[norms == 0.0] = 1.0  # a text of unknown tokens: leave it zero
    return out / norms


class Model2vecEmbedder:
    """A model2vec StaticModel from a local directory. Rows come out
    normalised whatever the model's config says, so a dot product is a
    cosine."""

    def __init__(self, model: object) -> None:
        self._model = model

    def encode(self, texts: Sequence[str]) -> np.ndarray:
        # use_multiprocessing=False: a handful of strings per call, and the
        # app is one process on one event loop
        vectors = self._model.encode(list(texts), use_multiprocessing=False)  # type: ignore[attr-defined]
        return _normalise(vectors)


def load_embedder(model_dir: str) -> Embedder | None:
    """The model at `model_dir`, or None with the reason logged once. Empty
    means keyword-only on purpose and says nothing; a directory that is not
    the pinned model is checked file by file BEFORE model2vec sees it, so a
    half-fetched download reads as "run make model" rather than as a
    safetensors traceback."""
    if not model_dir:
        return None
    target = Path(model_dir)
    missing = manual_model.missing(target)
    if missing:
        log.warning(
            "manual_search.model_missing",
            dir=model_dir,
            missing=list(missing),
            hint="make model",
        )
        return None
    try:
        from model2vec import StaticModel

        # a path that exists is loaded from disk and never fetched
        # (model2vec.persistence._resolve_folder)
        return Model2vecEmbedder(StaticModel.from_pretrained(str(target)))
    except Exception:  # noqa: BLE001 — search must degrade, not take the app down
        log.warning("manual_search.model_unloadable", dir=model_dir, exc_info=True)
        return None


def embed_text(chunk: Chunk) -> str:
    """What the vectors see: title and section before the body, so a short
    section still carries what it is about; capped, because a static
    embedding is a mean over tokens and a long section's tail dilutes it."""
    lead = f"{chunk.title}: {chunk.section}" if chunk.section else chunk.title
    return f"{lead}. {chunk.text}"[:EMBED_TEXT_LIMIT]


# ---------------------------------------------------------------------------
# The index and a search over it


@dataclass(frozen=True, slots=True)
class Index:
    chunks: tuple[Chunk, ...]
    bm25: Bm25
    vectors: np.ndarray | None  # (len(chunks), dim), unit rows; None: keywords only
    embedder: Embedder | None


def is_navigation(chunk: Chunk) -> bool:
    return bool(chunk.headings) and chunk.headings[-1] in NAVIGATION_HEADINGS


def build_index(chunks: Sequence[Chunk], embedder: Embedder | None) -> Index:
    chunks = tuple(c for c in chunks if not is_navigation(c))
    bm25 = bm25_build([keyword_doc(c) for c in chunks])
    vectors = None
    if embedder is not None and chunks:
        vectors = embedder.encode([embed_text(c) for c in chunks])
    return Index(chunks, bm25, vectors, embedder)


def build_from_disk(docs_dir: str, model_dir: str) -> Index:
    started = time.perf_counter()
    chunks = chunk_directory(Path(docs_dir))
    index = build_index(chunks, load_embedder(model_dir))
    log.info(
        "manual_search.indexed",
        docs_dir=docs_dir,
        pages=len({c.page for c in chunks}),
        chunks=len(chunks),
        embedded=index.vectors is not None,
        ms=round((time.perf_counter() - started) * 1000),
    )
    return index


def rrf(rankings: Sequence[Sequence[int]], k: int = 60) -> list[int]:
    """Reciprocal rank fusion: each ranking votes 1/(k + rank) for its
    documents, and the sum orders the result. Two rankings that agree on a
    document put it first; one that a single ranking found still appears,
    below them. Ties keep first-seen order."""
    scores: dict[int, float] = {}
    for ranking in rankings:
        for rank, doc_id in enumerate(ranking, start=1):
            scores[doc_id] = scores.get(doc_id, 0.0) + 1.0 / (k + rank)
    return sorted(scores, key=lambda d: -scores[d])


_ELLIPSIS = "…"


def snippet(text: str, terms: Sequence[str], width: int = 200) -> str:
    """About `width` characters of `text` around the first query term, or
    its opening when no term occurs (a vector hit need not share a word).
    Cut at word boundaries and marked with an ellipsis where it is cut."""
    if len(text) <= width:
        return text
    wanted = set(terms)
    start = 0
    for match in _WORD.finditer(text):
        word = match.group(0).lower()
        if len(word) >= 2 and fold(word) in wanted:
            start = max(0, match.start() - width // 3)
            break
    if start > 0:
        start = text.rfind(" ", 0, start) + 1  # back to a word boundary
    end = start + width
    if end < len(text):
        cut = text.rfind(" ", start, end)
        end = cut if cut > start else end
    piece = text[start:end].strip()
    if start > 0:
        piece = _ELLIPSIS + piece
    if end < len(text):
        piece = piece + _ELLIPSIS
    return piece


def search(
    index: Index,
    q: str,
    *,
    audience: str = "user",
    limit: int = 8,
    candidates: int = 50,
) -> list[Chunk]:
    """The best `limit` chunks for `q`: the BM25 ranking and the cosine
    ranking (above COSINE_FLOOR), each cut at `candidates`, fused by rank.
    ``audience="user"`` hides the technical manual; ``"all"`` searches
    everything. Keyword-only when the index has no vectors."""
    query = q.strip()
    if not query or not index.chunks:
        return []
    allowed = [
        i
        for i, chunk in enumerate(index.chunks)
        if audience == "all" or chunk.audience == "user"
    ]
    if not allowed:
        return []
    rankings: list[list[int]] = []

    terms = tokenize(query)
    keyword = bm25_scores(index.bm25, terms)
    by_keyword = sorted((i for i in allowed if i in keyword), key=lambda i: -keyword[i])
    rankings.append(by_keyword[:candidates])

    if index.embedder is not None and index.vectors is not None:
        query_vector = index.embedder.encode([query])[0]
        similarity = index.vectors @ query_vector
        by_vector = sorted(
            (i for i in allowed if similarity[i] >= COSINE_FLOOR),
            key=lambda i: -float(similarity[i]),
        )
        rankings.append(by_vector[:candidates])

    return [index.chunks[i] for i in rrf(rankings)[:limit]]


class ManualIndexCell:
    """The one holder of the index: built on first use, off the event loop,
    once -- a second caller arriving mid-build waits for the same build
    rather than starting another. ``warm`` is ``get`` under a name a startup
    hook can read."""

    def __init__(self, docs_dir: str, model_dir: str) -> None:
        self._docs_dir = docs_dir
        self._model_dir = model_dir
        self._index: Index | None = None
        self._lock = asyncio.Lock()

    async def get(self) -> Index:
        if self._index is None:
            async with self._lock:
                if self._index is None:
                    self._index = await anyio.to_thread.run_sync(
                        build_from_disk, self._docs_dir, self._model_dir
                    )
        return self._index

    warm = get


# ---------------------------------------------------------------------------
# `python -m volunteerdb.manual_search DOCS_DIR`: does the chunker still
# understand the markup? CI runs it on every docs build, with no model.


def main(argv: list[str]) -> int:
    if len(argv) != 2:
        print(f"usage: {argv[0]} DOCS_DIR", file=sys.stderr)
        return 2
    chunks = chunk_directory(Path(argv[1]))
    pages = len({c.page for c in chunks})
    audiences = Counter(c.audience for c in chunks)
    print(
        f"pages={pages} chunks={len(chunks)} "
        f"user={audiences['user']} dev={audiences['dev']}"
    )
    if pages < PAGE_FLOOR or len(chunks) < CHUNK_FLOOR:
        print(
            f"too few: expected at least {PAGE_FLOOR} pages and {CHUNK_FLOOR} "
            "sections -- did the built markup change under the chunker?",
            file=sys.stderr,
        )
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
