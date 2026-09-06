"""The demo seed's coverage of the app, held structurally.

scripts/seed.py exists to leave a database where every feature has something
to show. Two halves of that claim are enumerable, and both rot silently when a
feature is added: the custom-field types a parish can define, and the tables
the seed fills. A new FieldType, or a new table, now fails here instead of
being noticed months later by somebody wondering why the demo looks thin.

What this does NOT do is run the seed -- that wants an empty database, and the
suite's is anything but. `make fresh` is still the thing that proves the script
executes; this only holds it to the shape of the app around it.

The script is imported by path, because scripts/ is not a package. Importing it
builds its Env, which constructs an engine object and connects to nothing, so
this stays a pure test.
"""

import importlib.util
import pathlib
import random
import sys

import pytest

from volunteerdb import models

pytestmark = pytest.mark.pure

SEED_PATH = pathlib.Path(__file__).resolve().parent.parent / "scripts" / "seed.py"


def _load():
    spec = importlib.util.spec_from_file_location("demo_seed", SEED_PATH)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    sys.modules["demo_seed"] = module
    spec.loader.exec_module(module)
    return module


seed = _load()

# Tables the seed fills but does not count in its summary, because another
# line already speaks for them: a proposal always brings its candidates and
# its voting roll, and the summary prints proposals and ballots.
IMPLIED: frozenset[type] = frozenset({models.ProposalCandidate, models.ProposalVoter})


def test_every_field_type_has_a_seeded_field():
    """A parish can define a field of any FieldType, so the demo defines one
    of each: the fields admin, the volunteer form and the query language all
    meet every encoding fieldcodec knows."""
    seeded = {spec.field_type for spec in seed.FIELDS}
    missing = set(models.FieldType) - seeded
    assert not missing, f"no seeded custom field of type: {sorted(missing)}"


def test_every_table_is_seeded_and_counted():
    """Every mapped table is either counted in the seed's summary or listed
    above as implied by one that is. A new table is a new feature, and a
    feature with no demo data is invisible to whoever opens the app."""
    counted = {model for _label, model in seed.COUNTED}
    mapped = {
        mapper.class_
        for mapper in models.Base.registry.mappers
        if mapper.class_ is not models.Base
    }
    assert not (mapped - counted - IMPLIED), (
        "these tables get no demo data (or none the summary reports): "
        f"{sorted(m.__name__ for m in mapped - counted - IMPLIED)}"
    )
    assert not (counted - mapped), "the summary counts something that is not a table"


def test_the_generated_cohort_fills_the_parish():
    """`_generate` returns the number asked for whatever names it has to skip:
    the parish is a stated size, and a collision with a hand-written name must
    not quietly shrink it."""
    taken = {
        f"{seed.FIRST_NAMES[i]} {seed.LAST_NAMES[(i * 7) % len(seed.LAST_NAMES)]}"
        for i in range(5)
    }
    people = seed._generate(random.Random(1), set(taken), ["Lectors"], 40)
    assert len(people) == 40
    assert len({person.name for person in people}) == 40
    assert not {person.name for person in people} & taken
