"""Properties of the pure arithmetic, over inputs hypothesis invents: what
must hold for every ledger, every month of counts, every duration, every
ballot -- not just the examples a person thought of."""

from datetime import UTC, date, datetime, timedelta

import pytest
from hypothesis import given
from hypothesis import strategies as st

from volunteerdb import query_lang, star, throttle
from volunteerdb.asof_param import parse_as_of
from volunteerdb.fieldcodec import format_duration, parse_duration
from volunteerdb.fp import Err, Ok
from volunteerdb.services import mail_quota

pytestmark = pytest.mark.pure

T0 = datetime(2026, 9, 1, 12, 0, tzinfo=UTC)
FAMILIES = sorted(throttle.LIMITS)

# --- throttle -----------------------------------------------------------------

offsets = st.lists(
    st.integers(min_value=0, max_value=3 * 3600), min_size=0, max_size=40
)


@given(family=st.sampled_from(FAMILIES), seconds=offsets)
def test_blocked_is_exactly_the_live_hit_count_reaching_the_limit(family, seconds):
    key = f"{family}:x"
    ledger = throttle.Ledger()
    for s in sorted(seconds):
        ledger = throttle.hit(ledger, key, T0 + timedelta(seconds=s))
    now = T0 + timedelta(seconds=max(seconds, default=0))
    limit = throttle.LIMITS[family]
    live = [s for s in seconds if T0 + timedelta(seconds=s) > now - limit.window]
    assert throttle.blocked(ledger, key, now) is (len(live) >= limit.hits)


@given(family=st.sampled_from(FAMILIES), seconds=offsets)
def test_a_key_never_holds_a_hit_outside_its_window(family, seconds):
    key = f"{family}:x"
    ledger = throttle.Ledger()
    for s in sorted(seconds):
        at = T0 + timedelta(seconds=s)
        ledger = throttle.hit(ledger, key, at)
        window = throttle.LIMITS[family].window
        assert all(t > at - window for t in ledger.hits[key])
        assert ledger.hits[key] == tuple(sorted(ledger.hits[key]))


@given(family=st.sampled_from(FAMILIES), seconds=offsets, later=st.integers(0, 7200))
def test_prune_is_idempotent_and_changes_no_verdict(family, seconds, later):
    key = f"{family}:x"
    ledger = throttle.Ledger()
    for s in sorted(seconds):
        ledger = throttle.hit(ledger, key, T0 + timedelta(seconds=s))
    now = T0 + timedelta(seconds=max(seconds, default=0) + later)
    once = throttle.prune(ledger, now)
    assert throttle.prune(once, now) == once
    assert throttle.blocked(once, key, now) == throttle.blocked(ledger, key, now)
    assert all(hits for hits in once.hits.values()), "no empty keys survive"


# --- the mail-allowance projection ----------------------------------------------

days = st.integers(min_value=0, max_value=60)
counts = st.dictionaries(
    st.dates(min_value=date(2026, 1, 1), max_value=date(2026, 12, 31)),
    st.integers(min_value=0, max_value=400),
    max_size=40,
)


@given(
    counts=counts,
    today=st.dates(min_value=date(2026, 1, 1), max_value=date(2026, 12, 31)),
)
def test_the_projection_never_undercounts_what_already_went(counts, today):
    p = mail_quota.project(counts, today)
    assert p.today == counts.get(today, 0)
    assert p.projected_month >= p.month_to_date >= 0
    assert p.peak_day >= p.today
    assert p.level in ("", "warning", "critical")
    assert p.alarming is bool(p.level)
    if p.today >= mail_quota.DAILY_CAP or p.month_to_date >= mail_quota.MONTHLY_CAP:
        assert p.level == "critical"


@given(
    counts=counts,
    today=st.dates(min_value=date(2026, 1, 1), max_value=date(2026, 12, 31)),
    extra=st.integers(1, 300),
)
def test_more_mail_today_never_lowers_the_alarm(counts, today, extra):
    quiet = mail_quota.project(counts, today)
    louder = mail_quota.project({**counts, today: counts.get(today, 0) + extra}, today)
    rank = {"": 0, "warning": 1, "critical": 2}
    assert rank[louder.level] >= rank[quiet.level]
    assert louder.projected_month >= quiet.projected_month


# --- durations --------------------------------------------------------------------

durations = st.builds(
    timedelta,
    weeks=st.integers(0, 52),
    days=st.integers(0, 6),
    hours=st.integers(0, 23),
    minutes=st.integers(0, 59),
    seconds=st.integers(0, 59),
)


@given(td=durations)
def test_a_duration_survives_the_round_trip(td):
    text = format_duration(td)
    assert parse_duration(text) == Ok(td)
    assert format_duration(td) == text, "the spelling is canonical"


@given(text=st.text(max_size=20))
def test_parse_duration_never_raises_on_free_text(text):
    result = parse_duration(text)
    assert isinstance(result, (Ok, Err))
    if isinstance(result, Ok):
        assert result.value >= timedelta(0)


# --- STAR voting ------------------------------------------------------------------


@given(
    candidates=st.lists(st.integers(1, 6), min_size=0, max_size=6, unique=True),
    ballots=st.lists(
        st.dictionaries(st.integers(1, 6), st.integers(0, 5), max_size=6), max_size=12
    ),
)
def test_a_tally_names_a_winner_only_from_the_candidates_and_only_once(
    candidates, ballots
):
    result = star.star_tally(candidates, ballots)
    assert result.ballot_count == len(ballots)
    assert set(result.totals) == set(candidates)
    for c in candidates:
        assert result.totals[c] == sum(b.get(c, 0) for b in ballots)
    if result.winner_id is not None:
        assert result.winner_id in candidates
        assert not result.tie
    if result.tie:
        assert result.winner_id is None
        assert set(result.tied_ids) <= set(candidates)
    if len(candidates) >= 1 and not result.tie:
        assert result.winner_id is not None


# --- the query language -------------------------------------------------------------


@given(text=st.text(max_size=40))
def test_parse_never_raises_on_free_text(text):
    ast = query_lang.parse(text)
    assert ast is None or hasattr(ast, "sql")


@given(
    field=st.sampled_from(
        ["first_name", "last_name", "email", "phone", "role", "team"]
    ),
    op=st.sampled_from(["=", "<>", "!=", "LIKE", "ILIKE", "IS NULL", "IS NOT NULL"]),
    value=st.text(alphabet=st.characters(blacklist_characters="'\\\x00"), max_size=12),
)
def test_a_well_formed_condition_always_parses_as_one(field, op, value):
    text = f"{field} {op}" if op.startswith("IS") else f"{field} {op} '{value}'"
    assert query_lang.parse(text) is not None


# --- as-of ------------------------------------------------------------------------


@given(day=st.dates(min_value=date(1970, 1, 2), max_value=date(2100, 1, 1)))
def test_a_bare_date_means_the_last_microsecond_of_that_day(day):
    parsed = parse_as_of(day.isoformat())
    assert parsed.date() == day
    assert parsed.time() == datetime.max.time()
    assert parsed.tzinfo is not None


@given(
    moment=st.datetimes(
        min_value=datetime(1970, 1, 2),
        max_value=datetime(2100, 1, 1),
        timezones=st.just(UTC),
    )
)
def test_an_explicit_instant_is_taken_literally(moment):
    assert parse_as_of(moment.isoformat()) == moment
