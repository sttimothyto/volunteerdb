"""The manual's Simplified Technical English, enforced structurally.

docs/explanation/writing-style.md states the house subset of ASD-STE100 that
every page under docs/ is written to. The rules a machine can hold a page to
are held here: sentence length (25 words, 20 in a numbered step), paragraph
length (6 sentences), and the words the style bans outright. Vocabulary,
voice and verb forms stay a reviewer's job.

Modelled on test_purity_layer.py and self-maintaining the same way: BASELINE
is the count of every violation the tree carried when the sweep was written,
per file and per rule. A count may only fall, and an entry that has fallen to
zero has to be deleted -- so the list shrinks page by page and can never
quietly grow back. Regenerate with `uv run python tests/test_docs_style.py`;
list the violations of particular pages, with line numbers, by naming them:
`uv run python tests/test_docs_style.py docs/how-to/deploy.md`.
"""

import pathlib
import re
import sys
from collections import Counter
from dataclasses import dataclass

import pytest

pytestmark = pytest.mark.pure

DOCS = pathlib.Path(__file__).resolve().parent.parent / "docs"

DESCRIPTIVE_WORDS = 25  # ASD-STE100: a descriptive sentence
PROCEDURAL_WORDS = 20  # ASD-STE100: an instruction, here a numbered step
PARAGRAPH_SENTENCES = 6  # ASD-STE100: one topic, six sentences

# Words the style bans, with the rule name they are counted under. Matched
# case-insensitively except `may`, whose capital is the month.
BANNED: dict[str, re.Pattern[str]] = {
    "should": re.compile(r"\bshould\b", re.I),
    "may": re.compile(r"\bmay\b"),
    "ensure": re.compile(r"\bensur(e|es|ed|ing)\b", re.I),
    "utilize": re.compile(r"\butili[sz](e|es|ed|ing|ation)\b", re.I),
    "prior to": re.compile(r"\bprior to\b", re.I),
    "in order to": re.compile(r"\bin order to\b", re.I),
    "via": re.compile(r"\bvia\b", re.I),
    "e.g.": re.compile(r"\be\.g\.", re.I),
    "i.e.": re.compile(r"\bi\.e\.", re.I),
    "etc.": re.compile(r"\betc\b\.?", re.I),
    "allow": re.compile(r"\ballow(s|ed|ing)?\b", re.I),
    "perform": re.compile(r"\bperform(s|ed|ing)?\b", re.I),
    "obtain": re.compile(r"\bobtain(s|ed|ing)?\b", re.I),
    "locate": re.compile(r"\blocat(e|es|ed|ing)\b", re.I),
    "contraction": re.compile(
        r"\b\w+n['’]t\b"
        r"|\b(it|that|there|here|what|who|let)['’]s\b"
        r"|\b(you|we|they)['’]re\b"
        r"|\b\w+['’](ll|ve)\b"
        r"|\bI['’]m\b"
    ),
}

# Directive blocks whose body is not prose. Every other fenced directive
# (admonitions, glossaries, dropdowns) holds running text and is read.
SILENT_DIRECTIVES = frozenset(
    {
        "toctree",
        "code",
        "code-block",
        "literalinclude",
        "mermaid",
        "csv-table",
        "list-table",
        "image",
        "figure",
    }
)

_FENCE = re.compile(r"^\s*(`{3,}|~{3,}|:{3,})\s*(?:\{([\w-]+)\})?")
_OPTION = re.compile(r"^\s*:[\w-]+:(\s|$)")
_LIST_ITEM = re.compile(r"^(\s*)([-*+]|\d+[.)])\s+(.*)$")
_HEADING = re.compile(r"^\s{0,3}#{1,6}\s")
_TABLE_ROW = re.compile(r"^\s*\|")
_LINK_DEF = re.compile(r"^\s*\[[^\]]+\]:\s")
_TARGET = re.compile(r"^\([\w-]+\)=\s*$")
_DEF_BODY = re.compile(r"^:\s+(.*)$")
_QUOTE = re.compile(r"^\s*>\s?")

_INLINE_CODE = re.compile(r"`[^`]*`")
_IMAGE = re.compile(r"!\[[^\]]*\]\([^)]*\)")
_LINK = re.compile(r"\[([^\]]*)\]\([^)]*\)|\[([^\]]*)\]\[[^\]]*\]")
_EMPHASIS = re.compile(r"[*_]{1,3}")
_HTML_TAG = re.compile(r"<[^>]+>")
_SENTENCE_END = re.compile(r"(?<=[.!?])[\"'’)\]]*\s+(?=[\"'“(\[]*[A-Z0-9])")


@dataclass(frozen=True)
class Paragraph:
    line: int  # 1-based line of the first word
    text: str  # inline markup removed, whitespace collapsed
    procedural: bool  # inside a numbered list item


@dataclass(frozen=True)
class Violation:
    file: str
    line: int
    rule: str
    excerpt: str


def _clean(text: str) -> str:
    text = _IMAGE.sub(" ", text)
    text = _INLINE_CODE.sub(" CODE ", text)
    text = _LINK.sub(lambda m: m.group(1) or m.group(2) or "", text)
    text = _HTML_TAG.sub(" ", text)
    text = _EMPHASIS.sub("", text)
    return " ".join(text.split())


def paragraphs(source: str) -> list[Paragraph]:
    """The running prose of a MyST Markdown page, one entry per paragraph or
    list item. Code, tables, headings, directive options and comments are
    left out; the body of an admonition is read like any other prose."""
    out: list[Paragraph] = []
    fences: list[tuple[str, bool]] = []  # (marker, body is prose)
    in_comment = False
    buffer: list[str] = []
    start = 0
    procedural = False
    list_stack: list[tuple[int, bool]] = []  # (indent, numbered)

    def flush() -> None:
        nonlocal buffer
        text = _clean(" ".join(buffer))
        if text:
            out.append(Paragraph(start, text, procedural))
        buffer = []

    for number, raw in enumerate(source.splitlines(), start=1):
        line = raw.rstrip("\n")
        if in_comment:
            if "-->" in line:
                in_comment = False
            continue
        if "<!--" in line:
            flush()
            if "-->" not in line[line.index("<!--") :]:
                in_comment = True
            continue
        if fence := _FENCE.match(line):
            marker, directive = fence.group(1), fence.group(2)
            flush()
            if fences and fences[-1][0] == marker:
                fences.pop()
            else:
                prose = directive is not None and directive not in SILENT_DIRECTIVES
                fences.append((marker, prose))
            continue
        if fences:
            if not all(prose for _, prose in fences):
                continue
            if _OPTION.match(line):
                continue
        stripped = line.strip()
        if not stripped:
            flush()
            continue
        if (
            _HEADING.match(line)
            or _TABLE_ROW.match(line)
            or _LINK_DEF.match(line)
            or _TARGET.match(line)
            or stripped.startswith("%")
        ):
            flush()
            continue
        if item := _LIST_ITEM.match(line):
            flush()
            indent = len(item.group(1))
            numbered = item.group(2)[0].isdigit()
            while list_stack and list_stack[-1][0] >= indent:
                list_stack.pop()
            list_stack.append((indent, numbered))
            procedural = any(n for _, n in list_stack)
            start = number
            buffer.append(item.group(3))
            continue
        if definition := _DEF_BODY.match(line):
            flush()
            list_stack.clear()
            procedural = False
            start = number
            buffer.append(definition.group(1))
            continue
        text = _QUOTE.sub("", line)
        if not buffer:
            if not line[:1].isspace():
                list_stack.clear()
                procedural = False
            start = number
        buffer.append(text)
    flush()
    return out


def sentences(text: str) -> list[str]:
    return [s for s in _SENTENCE_END.split(text) if s.strip()]


def violations(path: pathlib.Path) -> list[Violation]:
    rel = path.relative_to(DOCS).as_posix()
    found: list[Violation] = []
    for paragraph in paragraphs(path.read_text(encoding="utf-8")):
        parts = sentences(paragraph.text)
        if len(parts) > PARAGRAPH_SENTENCES:
            found.append(
                Violation(
                    rel,
                    paragraph.line,
                    f"paragraph>{PARAGRAPH_SENTENCES}",
                    paragraph.text[:80],
                )
            )
        limit = PROCEDURAL_WORDS if paragraph.procedural else DESCRIPTIVE_WORDS
        rule = f"{'step' if paragraph.procedural else 'sentence'}>{limit}"
        for sentence in parts:
            if len(sentence.split()) > limit:
                found.append(Violation(rel, paragraph.line, rule, sentence[:80]))
        for name, pattern in BANNED.items():
            for _ in pattern.finditer(paragraph.text):
                found.append(Violation(rel, paragraph.line, name, paragraph.text[:80]))
    return found


def pages() -> list[pathlib.Path]:
    return sorted(p for p in DOCS.rglob("*.md") if "_build" not in p.parts)


def current() -> dict[str, dict[str, int]]:
    result: dict[str, dict[str, int]] = {}
    for path in pages():
        counts = Counter(v.rule for v in violations(path))
        if counts:
            result[path.relative_to(DOCS).as_posix()] = dict(sorted(counts.items()))
    return result


# What the tree carried when the sweep was written: page -> rule -> count.
# Shrinks page by page; a stale entry fails test_the_baseline_has_no_stale_entries.
BASELINE: dict[str, dict[str, int]] = {
    "explanation/accessibility.md": {"allow": 1, "sentence>25": 10},
    "explanation/architecture.md": {"allow": 1, "perform": 2, "sentence>25": 27},
    "explanation/auth.md": {"allow": 2, "may": 5, "sentence>25": 48, "via": 1},
    "explanation/deployment.md": {"sentence>25": 20, "should": 1, "via": 1},
    "explanation/elections.md": {
        "may": 2,
        "paragraph>6": 1,
        "sentence>25": 19,
        "should": 2,
        "step>20": 1,
    },
    "explanation/events.md": {
        "allow": 1,
        "may": 7,
        "paragraph>6": 1,
        "sentence>25": 30,
    },
    "explanation/history.md": {"contraction": 1, "sentence>25": 9},
    "explanation/permissions.md": {
        "allow": 1,
        "contraction": 1,
        "may": 3,
        "sentence>25": 11,
    },
    "explanation/workload.md": {
        "contraction": 1,
        "may": 1,
        "sentence>25": 6,
        "should": 1,
    },
    "how-to/api-recipes.md": {"may": 1, "obtain": 1, "sentence>25": 1},
    "how-to/audit-logs.md": {"may": 1, "sentence>25": 5},
    "how-to/backup-restore.md": {
        "sentence>25": 15,
        "should": 1,
        "step>20": 2,
        "via": 2,
    },
    "how-to/custom-fields-and-workload.md": {
        "e.g.": 1,
        "sentence>25": 2,
        "should": 1,
        "step>20": 3,
    },
    "how-to/deploy.md": {"perform": 1, "sentence>25": 18, "step>20": 4},
    "how-to/google-calendar-sync.md": {"may": 2, "sentence>25": 17},
    "how-to/manage-users.md": {"may": 1, "sentence>25": 17, "step>20": 7},
    "how-to/new-instance.md": {
        "allow": 1,
        "obtain": 2,
        "sentence>25": 17,
        "should": 1,
        "step>20": 1,
    },
    "how-to/roster-spreadsheets.md": {
        "may": 3,
        "obtain": 1,
        "sentence>25": 17,
        "step>20": 6,
        "via": 1,
    },
    "how-to/rotate-secrets.md": {"sentence>25": 9},
    "how-to/run-tests.md": {
        "contraction": 1,
        "sentence>25": 10,
        "step>20": 1,
        "via": 2,
    },
    "how-to/site-logo.md": {"sentence>25": 3, "step>20": 1},
    "how-to/team-home-pages.md": {"may": 1, "sentence>25": 6, "step>20": 1, "via": 1},
    "how-to/write-a-migration.md": {"contraction": 1, "sentence>25": 7},
    "index.md": {"sentence>25": 2},
    "reference/cli.md": {
        "e.g.": 1,
        "paragraph>6": 1,
        "sentence>25": 19,
        "should": 1,
        "via": 1,
    },
    "reference/configuration.md": {"e.g.": 2, "sentence>25": 14, "via": 3},
    "reference/glossary.md": {"e.g.": 3, "may": 6, "perform": 1, "sentence>25": 23},
    "reference/http-api.md": {"obtain": 1, "sentence>25": 6},
    "reference/permissions.md": {"may": 15, "paragraph>6": 1, "sentence>25": 27},
    "reference/query-language.md": {"may": 2, "sentence>25": 6},
    "reference/schema.md": {"sentence>25": 31},
    "reference/site-config.md": {"e.g.": 1, "sentence>25": 9, "should": 1},
    "reference/spreadsheets.md": {
        "e.g.": 1,
        "may": 2,
        "sentence>25": 13,
        "step>20": 1,
        "via": 2,
    },
    "tutorials/coordinator-tour.md": {
        "contraction": 2,
        "e.g.": 1,
        "may": 2,
        "sentence>25": 14,
        "should": 1,
        "step>20": 1,
    },
    "tutorials/install-and-run.md": {
        "contraction": 2,
        "may": 1,
        "perform": 1,
        "sentence>25": 9,
    },
}


SAMPLE = """\
# A heading that is very long and would fail the sentence rule if it were read as prose at all

Plain prose. It has two sentences.

1. Click *Save* and wait for the page to reload before you open the next tab of the form.
   - A nested bullet under a numbered step is procedural too.
2. Type `a very long code span that must count as one word` in the box.

- A plain bullet is descriptive, so it gets the longer allowance of twenty-five words in total here.

```python
should = "code is never read, e.g. this"
```

:::{note}
:class: tip
The body of an admonition is prose, and it should be read.
:::

| should | may |
|--------|-----|
| table cells are not prose | at all |

term
: A definition is prose.

<!-- should, inside a comment, is not -->
"""


def test_the_extractor_reads_myst_the_way_a_reader_does():
    found = paragraphs(SAMPLE)
    texts = [p.text for p in found]
    assert texts[0] == "Plain prose. It has two sentences."
    assert found[1].procedural and found[1].text.startswith("Click Save")
    assert found[2].procedural and found[2].text.startswith("A nested bullet")
    assert found[3].procedural and "CODE" in found[3].text
    assert not found[4].procedural and found[4].text.startswith("A plain bullet")
    assert any(t.startswith("The body of an admonition") for t in texts)
    assert any(t == "A definition is prose." for t in texts)
    assert not any("code is never read" in t or "table cells" in t for t in texts)
    assert not any("inside a comment" in t for t in texts)
    assert [p.line for p in found[:2]] == [3, 5]

    assert sentences("Do this. Then that! Really? Yes, with 3.5 litres.") == [
        "Do this.",
        "Then that!",
        "Really?",
        "Yes, with 3.5 litres.",
    ]


def test_the_banned_words_match_words_and_not_their_neighbours():
    hits = {
        name
        for name, pattern in BANNED.items()
        if pattern.search(
            "It should ensure you utilize this via e.g. that, i.e. those, etc. "
            "Perform it prior to that in order to obtain and locate it; don't, "
            "it's you're I'm we'll. May 2026. Allowed."
        )
    }
    assert hits == set(BANNED) - {"may"}  # the month keeps its capital
    assert BANNED["may"].search("you may not")
    misses = "allowlist allowance performance location obtainable Timothy's may"
    assert {n for n, p in BANNED.items() if p.search(misses)} == {"may"}


def test_the_manual_never_gets_wordier():
    """Every over-long sentence or paragraph and every banned word under docs/
    is either gone or still on the baseline at no more than its original count."""
    grown = []
    for page, found in current().items():
        allowed = BASELINE.get(page, {})
        for rule, n in found.items():
            if n > allowed.get(rule, 0):
                grown.append(f"{page}: {rule} x{n} (baseline {allowed.get(rule, 0)})")
    assert not grown, (
        "the manual drifted from its Simplified Technical English "
        "(docs/explanation/writing-style.md). Run `uv run python "
        f"tests/test_docs_style.py <page>` for the lines: {grown}"
    )


def test_the_baseline_has_no_stale_entries():
    """The ratchet only turns one way: a violation that was removed must also
    leave the baseline, so nobody can reintroduce it under an old allowance."""
    now = current()
    stale = [
        f"{page}: {rule}"
        for page, rules in BASELINE.items()
        for rule in rules
        if now.get(page, {}).get(rule, 0) == 0
    ]
    assert not stale, (
        f"delete these BASELINE entries, the pages no longer need them: {stale}"
    )


if __name__ == "__main__":  # pragma: no cover - baseline printer / page lister
    import pprint

    if len(sys.argv) > 1:
        for arg in sys.argv[1:]:
            for v in violations(pathlib.Path(arg).resolve()):
                print(f"{v.file}:{v.line}: [{v.rule}] {v.excerpt}")
    else:
        pprint.pprint(current(), width=100, sort_dicts=True)
