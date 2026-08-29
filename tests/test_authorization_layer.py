"""Where authorization lives, enforced structurally.

docs/explanation/architecture.md states the invariant these tests defend:
`services/` is the only place business rules live, which is why the GUI and the
JSON API cannot drift apart on permissions. That claim used to be false — four
service functions took an `Actor` while the checks were duplicated across 47
`require()` calls in `api/` and 52 in `ui/`, and services/task_force.py
authorized nothing at all.

Two sweeps keep it true, both self-maintaining: a new service function that
takes an actor and forgets to use it fails the first, and a new `require()` at
either front door fails the second. Neither depends on anybody remembering to
add a test.
"""

import ast
import pathlib

import pytest

pytestmark = pytest.mark.pure

SRC = pathlib.Path(__file__).resolve().parent.parent / "src" / "volunteerdb"

# The vocabulary a service uses to refuse: require() itself, and the per-module
# gates that wrap it.
GATES = {
    "require",
    "_managed",  # elections, events
    "_viewable",  # elections
    "_visible",  # events
    "_require_self",  # events
    "_require_own_or_managed",  # events
    "_require_member",  # events — membership as a domain invariant
    "_require_admin",  # users
    "_may_manage",  # memberships
    "get_managed",  # memberships
}

# Functions that take an actor to SCOPE their answer rather than to refuse it:
# there is no denial to make, because the actor decides which rows exist. Each
# one has to be listed on purpose — that is the point of the sweep.
SCOPING_ONLY = {
    "elections.vacancies",  # coverage rows for the teams you lead
    "elections.list_proposals",  # your subtree ∪ your rolls
    "elections.involving",  # ditto, for one volunteer
    "events.visible_team_ids",  # the scope itself
    "events.list_events",
    "events.similar_events",  # advisory double-booking check, masked titles
    "events.claimable_subs",
    "graph.elements",  # nodes for teams whose roster names you may see
    "stats.dashboard",  # three tiers, each gated by its own right
    "stats._leadership",
    "stats._personal",
    "volunteers.search",  # public fields for all, private ones scoped
    "volunteers.search_or_query",
    "workload.visible_scores",  # only volunteers whose workload you may see
    "importer.apply_rows",  # licences each row against the actor, one by one
}

# `require()` calls that legitimately remain at a front door: an actor-shaped
# question with no single service behind it, or a page-level gate on a whole
# screen. Anything else belongs in a service.
EDGE_ALLOWLIST = {
    # "may this account use elections at all" — a nav/page question, not an
    # operation; every operation behind it is checked in the service
    ("api/elections.py", "can_access_elections"),
    ("api/volunteers.py", "can_access_elections"),
    # the caller must be linked to a volunteer before a ballot can exist
    ("api/elections.py", "volunteer_id is not None"),
    # scope comes FROM the actor, so there is no argument for a service to check
    ("api/io.py", "people_team_ids"),
    ("ui/team_files_route.py", "full_view_team_ids"),
    # dashboard tiers: reports.coverage answers admins and leaders differently
    ("api/reports.py", "managed_team_ids"),
    # "archived too" widens a query rather than naming an object
    ("api/volunteers.py", "is_admin"),
    # minting an account for one of your own people — deliberately wider than
    # account management, and narrower than it (docs/reference/permissions.md)
    ("api/volunteers.py", "can_invite_volunteer"),
    ("ui/invites.py", "can_invite_volunteer"),
    # taking YOURSELF off a slot, which is what this dialog collects a reason
    # for; remove_assignment itself also allows a manager
    ("ui/events_page.py", "volunteer_id"),
    # a manager may schedule anyone, but only for their own team's event
    ("api/events.py", "volunteer_id"),
    # the advisory double-booking check deliberately reaches ACROSS teams
    # (masking titles it may not show), so there is no per-team right to check
    # — only "do you create events at all"
    ("api/events.py", "can_create_events"),
}


# Gate calls whose result is thrown away, per module, when gates began returning
# `Err | None` instead of raising (FUNCTIONAL_REFACTORING.md, Phase 2). A raising
# gate as a bare statement was correct; a value-returning one is a check that
# never refuses. The count may only fall; an entry at zero must be deleted.
DISCARDED_GATE_BASELINE: dict[str, int] = {
    "services/roster_sheets.py": 1,
    "sheets/exporter.py": 2,
    "sheets/importer.py": 1,
}


def _functions(path: pathlib.Path):
    for node in ast.parse(path.read_text()).body:
        if isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef):
            yield node


def _called_names(node: ast.AST) -> set[str]:
    names = set()
    for inner in ast.walk(node):
        if isinstance(inner, ast.Call):
            func = inner.func
            if isinstance(func, ast.Name):
                names.add(func.id)
            elif isinstance(func, ast.Attribute):
                names.add(func.attr)
    return names


def test_every_service_that_takes_an_actor_uses_it():
    """An actor parameter is a promise to check it.

    A service that accepts an `Actor` and never reaches a gate is the shape of
    the task-force bug: both front doors assume the service decides, the
    service assumes the doors did, and nobody does.
    """
    ungated = []
    checked = 0
    for path in sorted(SRC.rglob("*.py")):
        if "services" not in path.parts and "sheets" not in path.parts:
            continue
        module = path.stem
        takes_actor = {}
        for fn in _functions(path):
            params = [a.arg for a in fn.args.args] + [a.arg for a in fn.args.kwonlyargs]
            if "actor" in params:
                takes_actor[fn.name] = fn

        # Settle first which functions in this module gate directly, so one that
        # DELEGATES to another counts as gated too: sign_up_series hands the
        # whole check to sign_up, which is a real pattern rather than a hole.
        gated_here = {
            name
            for name, fn in takes_actor.items()
            if _called_names(fn) & GATES or f"{module}.{name}" in SCOPING_ONLY
        }

        for name, fn in takes_actor.items():
            checked += 1
            qualified = f"{module}.{name}"
            if qualified in SCOPING_ONLY:
                continue
            called = _called_names(fn)
            if called & GATES or called & gated_here:
                continue
            ungated.append(qualified)

    assert checked > 60, (
        f"only found {checked} actor-taking service functions — the sweep stopped "
        "seeing them and is no longer covering anything"
    )
    assert not ungated, (
        "these services take an actor but never check it. Either gate them, or "
        "add them to SCOPING_ONLY with a comment saying why the actor only "
        f"narrows the answer: {sorted(ungated)}"
    )


def test_the_front_doors_do_not_decide_who_may_do_what():
    """Permission checks belong in one place, and this is how they stay there.

    Every `require()` under api/ or ui/ has to be justified on EDGE_ALLOWLIST.
    The list is short and each entry says what makes it an edge question; a new
    check that is really about an operation fails here and goes to the service
    instead, where both surfaces get it.
    """
    stray = []
    for folder in ("api", "ui"):
        for path in sorted((SRC / folder).glob("*.py")):
            rel = f"{folder}/{path.name}"
            for lineno, line in enumerate(path.read_text().splitlines(), 1):
                if "require(" not in line or line.lstrip().startswith("#"):
                    continue
                # the predicate usually names the right on the following lines
                text = "\n".join(path.read_text().splitlines()[lineno - 1 : lineno + 4])
                if any(
                    rel == allowed_file and marker in text
                    for allowed_file, marker in EDGE_ALLOWLIST
                ):
                    continue
                stray.append(f"{rel}:{lineno}")
    assert not stray, (
        "these front-door permission checks are not on EDGE_ALLOWLIST. A rule "
        "enforced here is a rule the other surface can forget: move it into the "
        f"service both surfaces call. {stray}"
    )


def _discarded_gate_calls(path: pathlib.Path) -> int:
    """Bare expression statements (awaited or not) that call a gate."""
    n = 0
    for node in ast.walk(ast.parse(path.read_text())):
        if not isinstance(node, ast.Expr):
            continue
        call = node.value.value if isinstance(node.value, ast.Await) else node.value
        if isinstance(call, ast.Call) and _called_names(call) & GATES:
            n += 1
    return n


def test_a_gate_result_is_never_discarded():
    """A gate that returns its refusal has to be looked at.

    `require(...)` used to raise, so calling it as a statement was the whole
    check. Once it returns `Err | None`, the same statement checks nothing --
    the refusal is computed and dropped. Every remaining bare call is on the
    baseline from before the change; the number can only go down.
    """
    grown, stale = [], []
    for path in sorted(SRC.rglob("*.py")):
        if "services" not in path.parts and "sheets" not in path.parts:
            continue
        rel = str(path.relative_to(SRC))
        n = _discarded_gate_calls(path)
        allowed = DISCARDED_GATE_BASELINE.get(rel, 0)
        if n > allowed:
            grown.append(f"{rel}: {n} (baseline {allowed})")
        if n == 0 and rel in DISCARDED_GATE_BASELINE:
            stale.append(rel)
    assert not grown, (
        "a gate's result is thrown away here; bind it -- "
        f"`if denied := require(...): return denied`: {grown}"
    )
    assert not stale, f"delete these DISCARDED_GATE_BASELINE entries: {stale}"
