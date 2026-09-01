"""The manual's search, piece by piece: the chunker on Furo's markup, the
keyword side, the fusion, and -- when `make model` has run -- the real model
loading offline and telling a spreadsheet from dark mode."""

from pathlib import Path

import pytest

from volunteerdb import manual_model
from volunteerdb import manual_search as ms
from volunteerdb.manual_search import (
    Chunk,
    audience_of,
    bm25_build,
    bm25_scores,
    build_index,
    chunk_directory,
    chunk_html,
    fold,
    rrf,
    search,
    snippet,
    tokenize,
)

from tests.fakes import FakeEmbedder

pytestmark = pytest.mark.pure

REPO = Path(__file__).resolve().parent.parent
MODEL_DIR = REPO / ".models" / "potion-base-8M"

# One built page, as Furo emits it: nested <section id>s, a headerlink ¶ on
# every heading, a <dl>, a <pre>, and the chrome around the article that no
# chunk may carry.
FURO_PAGE = """<!DOCTYPE html>
<html lang="en" data-content_root="../../">
<head><meta charset="utf-8"/>
<title>Change your password - VolunteerDB documentation</title>
<script>var noise = "script noise";</script>
<style>.noise { color: red }</style>
</head>
<body>
<nav class="sidebar-drawer"><p>Sidebar noise</p></nav>
<article role="main" id="furo-main-content">
<section id="change-your-password">
<h1>Change your password<a class="headerlink" href="#change-your-password" title="Link to this heading">¶</a></h1>
<p>Set a new password for your own account.</p>
<section id="before-you-start">
<h2>Before you start<a class="headerlink" href="#before-you-start" title="Link to this heading">¶</a></h2>
<ul class="simple">
<li><p>You are signed in.</p></li>
<li><p>A password must have 15 characters or more.</p></li>
</ul>
<section id="deeper">
<h3>Deeper<a class="headerlink" href="#deeper" title="Link to this heading">¶</a></h3>
<dl><dt>Symptom</dt><dd>The page says no.</dd></dl>
<pre>make model
uv run volunteerdb</pre>
</section>
</section>
<section id="steps">
<h2>Steps<a class="headerlink" href="#steps" title="Link to this heading">¶</a></h2>
<ol class="arabic simple"><li><p>Click <em>Save password</em>.</p></li></ol>
</section>
</section>
</article>
<footer><p>Footer noise</p></footer>
</body>
</html>
"""


def _chunk(page: str, text: str, *headings: str, anchor: str = "") -> Chunk:
    return Chunk(
        page=page,
        title=headings[0] if headings else page,
        headings=headings,
        anchor=anchor,
        audience=audience_of(page),
        text=text,
    )


# --- the chunker -------------------------------------------------------------


def test_every_section_is_a_chunk_with_only_its_own_text():
    chunks = chunk_html(FURO_PAGE, "guide/how-to/change-your-password")
    assert [c.anchor for c in chunks] == ["", "before-you-start", "deeper", "steps"]
    assert {c.title for c in chunks} == {"Change your password"}
    assert [c.section for c in chunks] == [
        "",
        "Before you start",
        "Before you start › Deeper",
        "Steps",
    ]
    top, before, deeper, steps = chunks
    assert top.text == "Set a new password for your own account.", (
        "a parent carries none of its children's words"
    )
    assert "signed in" in before.text and "Symptom" not in before.text
    assert deeper.text == "Symptom The page says no. make model uv run volunteerdb", (
        "block tags break words and <pre> newlines fold to spaces"
    )
    assert steps.text == "Click Save password."
    assert steps.headings == ("Change your password", "Steps")


def test_the_urls_and_the_audience():
    chunks = chunk_html(FURO_PAGE, "guide/how-to/change-your-password")
    assert chunks[0].url == "/manual/guide/how-to/change-your-password.html", (
        "the page's top section is the page itself, not a fragment on it"
    )
    assert (
        chunks[1].url
        == "/manual/guide/how-to/change-your-password.html#before-you-start"
    )
    assert {c.audience for c in chunks} == {"user"}
    assert {c.audience for c in chunk_html(FURO_PAGE, "how-to/deploy")} == {"dev"}


def test_nothing_outside_the_article_and_no_headerlink_survives():
    chunks = chunk_html(FURO_PAGE, "guide/how-to/change-your-password")
    everything = " ".join(c.text + " ".join(c.headings) for c in chunks)
    for noise in ("¶", "Sidebar noise", "Footer noise", "script noise", "color: red"):
        assert noise not in everything, noise
    assert "VolunteerDB documentation" not in everything


def test_a_page_without_a_heading_takes_its_title_from_the_title_tag():
    html = (
        "<html><head><title>Orphan - VolunteerDB documentation</title></head>"
        '<body><section id="orphan"><p>Some text.</p></section></body></html>'
    )
    (chunk,) = chunk_html(html, "orphan")
    assert chunk.title == "Orphan"
    assert chunk.headings == ("",) and chunk.section == ""
    assert chunk.text == "Some text."


def test_a_page_with_no_sections_yields_nothing():
    assert chunk_html("<html><body><p>Hello</p></body></html>", "x") == []


def test_audience_by_path():
    assert audience_of("index") == "user"
    assert audience_of("guide/how-to/rsvp") == "user"
    assert audience_of("guide/reference/roles") == "user"
    assert audience_of("how-to/deploy") == "dev"
    assert audience_of("reference/schema") == "dev"
    assert audience_of("guide") == "dev", "only the guide/ tree, not a page named guide"


def test_chunk_directory_walks_the_pages_and_skips_sphinxs_own(tmp_path):
    page = FURO_PAGE
    for relative in (
        "index.html",
        "guide/how-to/a.html",
        "how-to/b.html",
        "search.html",
        "genindex.html",
        "py-modindex.html",
        "_static/noise.html",
        "_sources/how-to/b.html",
    ):
        path = tmp_path / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(page)
    chunks = chunk_directory(tmp_path)
    assert sorted({c.page for c in chunks}) == ["guide/how-to/a", "how-to/b", "index"]
    assert [c.page for c in chunks] == sorted(c.page for c in chunks), "sorted by path"
    assert chunk_directory(tmp_path / "missing") == []


# --- keywords ------------------------------------------------------------------


def test_folding_lands_the_forms_of_a_word_together():
    # each family lands on one stem -- pinned, so that a fold returning "" for
    # everything cannot pass by making every family trivially equal
    assert [fold(w) for w in ("settings", "setting")] == ["sett"] * 2
    assert [fold(w) for w in ("passwords", "password")] == ["password"] * 2
    assert [fold(w) for w in ("policies", "policy")] == ["policy"] * 2
    assert [fold(w) for w in ("deployed", "deploying", "deploy")] == ["deploy"] * 3
    assert [fold(w) for w in ("changes", "changed", "changing")] == ["chang"] * 3
    assert fold("class") == "class", "a double s is not a plural"
    assert tokenize("A bit of the Manual's search!") == [
        "bit",
        "of",
        "the",
        "manual",
        "search",
    ], "one-letter pieces drop out; short words are left alone"


def test_bm25_ranks_by_term_frequency_and_omits_the_rest():
    bm25 = bm25_build(
        [
            ["password", "password", "reset"],
            ["password", "calendar", "feed"],
            ["calendar", "feed", "reset"],
        ]
    )
    scores = bm25_scores(bm25, ["password"])
    assert set(scores) == {0, 1}, "a document sharing no term is absent, not zero"
    assert scores[0] > scores[1]
    assert bm25_scores(bm25, ["nothing"]) == {}
    # and, at equal term frequency, the shorter document (Okapi's b)
    bm25 = bm25_build([["password"], ["password", "calendar", "feed", "reset"]])
    scores = bm25_scores(bm25, ["password"])
    assert scores[0] > scores[1]


def test_rrf_puts_agreement_first_and_keeps_lone_hits():
    assert rrf([[1, 2, 3], [2, 3, 1]]) == [2, 1, 3]
    assert rrf([[1, 2], [3]]) == [1, 3, 2], "ties keep first-seen order"
    assert rrf([[], []]) == []


def test_snippet_centres_on_the_first_term_or_opens_the_text():
    short = "A short section."
    assert snippet(short, ["short"]) == short
    text = "word " * 60 + "password appears here " + "tail " * 60
    hit = snippet(text.strip(), tokenize("passwords"), width=80)
    assert "password appears here" in hit
    assert hit.startswith("…") and hit.endswith("…")
    assert len(hit) <= 82
    opening = snippet(text.strip(), ["absent"], width=80)
    assert opening.startswith("word word") and opening.endswith("…")


# --- the index and the search ------------------------------------------------


CHUNKS = (
    _chunk(
        "guide/how-to/link-a-roster-spreadsheet",
        "Link the team's roster sheet and sync it.",
        "Link the roster sheet",
    ),
    _chunk(
        "guide/how-to/change-your-password",
        "Set a new password for your own account.",
        "Change your password",
    ),
    _chunk("guide/how-to/dark-mode", "Turn dark mode on or off.", "Dark mode"),
    _chunk(
        "how-to/deploy",
        "Deploy the image to the host with pyinfra.",
        "Deploy",
    ),
    _chunk(
        "how-to/rotate-secrets",
        "Rotate the storage secret and the database password.",
        "Rotate secrets",
    ),
)


def test_a_keyword_only_index_answers_and_hides_the_technical_manual():
    index = build_index(CHUNKS, embedder=None)
    assert index.vectors is None and index.embedder is None
    assert [c.page for c in search(index, "password")] == [
        "guide/how-to/change-your-password"
    ]
    assert [c.page for c in search(index, "password", audience="all")] == [
        "guide/how-to/change-your-password",
        "how-to/rotate-secrets",
    ]
    assert search(index, "deploy") == []
    assert [c.page for c in search(index, "deploy", audience="all")] == [
        "how-to/deploy"
    ]
    assert search(index, "   ") == []
    assert search(build_index((), embedder=None), "password") == []


def test_the_vectors_find_what_the_keywords_cannot():
    keyword_only = build_index(CHUNKS, embedder=None)
    assert search(keyword_only, "spreadsheet") == [], (
        "the sheet page never says 'spreadsheet', in its heading or its body"
    )
    embedder = FakeEmbedder(dim=1024, synonyms={"spreadsheet": "sheet"})
    index = build_index(CHUNKS, embedder=embedder)
    assert index.vectors is not None and index.vectors.shape == (len(CHUNKS), 1024)
    assert embedder.calls[0][0].startswith("Link the roster sheet. Link the team"), (
        "the vectors see the title before the body"
    )
    hits = search(index, "spreadsheet")
    assert hits and hits[0].page == "guide/how-to/link-a-roster-spreadsheet"
    assert search(index, "spreadsheet", audience="user") == hits
    # and where both sides agree, agreement comes first
    assert search(index, "password", audience="all")[0].page == (
        "guide/how-to/change-your-password"
    )


def test_a_page_the_vectors_alone_find_is_the_pages_top_section():
    embedder = FakeEmbedder(dim=1024, synonyms={"spreadsheet": "sheet"})
    index = build_index(CHUNKS, embedder=embedder)
    (hit, *_) = search(index, "spreadsheet")
    assert hit.url == "/manual/guide/how-to/link-a-roster-spreadsheet.html"
    assert hit.section == ""


def test_load_embedder_says_nothing_for_empty_and_warns_once_for_missing(
    tmp_path, log_records
):
    assert ms.load_embedder("") is None
    assert not log_records
    assert ms.load_embedder(str(tmp_path / "nowhere")) is None
    (entry,) = log_records
    assert entry["event"] == "manual_search.model_missing"
    assert entry["hint"] == "make model"


def test_the_cli_counts_and_refuses_a_thin_manual(tmp_path, capsys):
    (tmp_path / "index.html").write_text(FURO_PAGE)
    assert ms.main(["prog", str(tmp_path)]) == 1
    out, err = capsys.readouterr()
    assert out.strip() == "pages=1 chunks=4 user=4 dev=0"
    assert "too few" in err
    assert ms.main(["prog"]) == 2


# --- the real model, when it is there ------------------------------------------


# The pin, not the directory: a half-fetched or stale .models/ must skip the
# way an absent one does, or the test fails for a reason it cannot name.
@pytest.mark.skipif(
    bool(manual_model.missing(MODEL_DIR)),
    reason="no complete .models/potion-base-8M: run `make model`",
)
def test_the_real_model_loads_offline_and_tells_sheets_from_dark_mode(monkeypatch):
    monkeypatch.setenv("HF_HUB_OFFLINE", "1")
    embedder = ms.load_embedder(str(MODEL_DIR))
    assert embedder is not None
    vectors = embedder.encode(["roster spreadsheet", "sync the sheet", "dark mode"])
    assert vectors.shape == (3, 256)
    sheet, sync, dark = vectors
    assert abs(float(sheet @ sheet) - 1.0) < 1e-5, "rows come out normalised"
    assert float(sheet @ sync) > float(sheet @ dark)


def test_link_list_sections_are_left_out_of_the_index():
    """Every guide page ends with "Related pages": a list of other pages'
    titles, which is keyword-dense and answers nothing."""
    page = Chunk("guide/how-to/x", "X", ("X",), "", "user", "how to do x")
    related = Chunk(
        "guide/how-to/x",
        "X",
        ("X", "Related pages"),
        "related-pages",
        "user",
        "Change your password Sign in with an emailed code",
    )
    index = build_index([page, related], None)
    assert [c.anchor for c in index.chunks] == [""]
    assert not search(index, "change your password", audience="user")
