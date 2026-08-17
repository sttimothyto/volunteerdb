"""Query benchmark harness against a disposable volunteerdb_bench database.

Every optimization in this repo is judged by numbers from here, at two scales
(~500 volunteers = today's parish, ~5000 = growth headroom), before and after.

Run: uv run python scripts/bench.py setup --scale 500
     uv run python scripts/bench.py run --json bench-results/base-500.json [--explain] [--only search_name]
     uv run python scripts/bench.py compare bench-results/base-500.json bench-results/after-500.json

`setup` drops and recreates volunteerdb_bench (never the dev or test DB),
migrates it to head and seeds a deterministic synthetic parish. `run` times
each hot-query pattern (median/p90 after warmup) and counts the SQL statements
it issues — the query count is the N+1 headline metric. `--explain` prints
EXPLAIN (ANALYZE, BUFFERS) for every SELECT a pattern ran, exactly as the app
issued it.
"""

import argparse
import asyncio
import contextlib
import json
import os
import statistics
import subprocess
import sys
import time
from datetime import UTC, datetime
from decimal import Decimal
from io import BytesIO
from random import Random

import sqlalchemy as sa
from PIL import Image
from sqlalchemy import event
from sqlalchemy.ext.asyncio import create_async_engine

from volunteerdb import db
from volunteerdb.config import settings
from volunteerdb.db import db_session
from volunteerdb.models import (
    FieldType,
    Membership,
    Team,
    TeamPage,
    TeamPageImage,
    TeamRole,
    Volunteer,
)
from volunteerdb.permissions import load_actor, team_ids_map
from volunteerdb.services import custom_fields as custom_field_service
from volunteerdb.services import pages as page_service
from volunteerdb.services import teams as team_service
from volunteerdb.services import users as user_service
from volunteerdb.services import volunteers as volunteer_service
from volunteerdb.services import workload as workload_service
from volunteerdb.sheets.exporter import export_csv
from volunteerdb.sheets.importer import run_import

# Through Settings, not os.environ, so the bench reaches the same database
# .env points the app at (see the note in tests/conftest.py).
BASE_URL = settings().database_url
BENCH_URL = BASE_URL.rsplit("/", 1)[0] + "/volunteerdb_bench"

# landmark accounts/volunteers the patterns look up by email at run time
ADMIN_EMAIL = "admin@bench.test"
LEADER_EMAIL = "leader@bench.test"
BENCH_PASSWORD = "bench-run-passphrase"  # 15+ chars: the policy applies here too
BUSY_EMAIL = "busy.bee@bench.test"  # exactly 5 memberships (impact pattern)
CHURNED_EMAIL = "chris.churn@bench.test"  # guaranteed membership history (timeline)

FIRST_NAMES = [
    "Maria",
    "Marcus",
    "Martha",
    "Omar",
    "Tamara",
    "James",
    "Rose",
    "Peter",
    "Agnes",
    "Thomas",
    "Lucia",
    "David",
    "Sarah",
    "Emmanuel",
    "Anna",
    "Miguel",
    "Grace",
    "John",
    "Teresa",
    "Paul",
    "Claire",
    "Frank",
    "Rita",
    "Samuel",
    "Helen",
    "George",
    "Monica",
    "Andrew",
    "Beatrice",
    "Charles",
    "Dorothy",
    "Felix",
    "Irene",
    "Leo",
    "Nadia",
    "Oscar",
]
LAST_NAMES = [
    "Miller",
    "Mueller",
    "Keller",
    "Alvarez",
    "Okafor",
    "Nguyen",
    "Kowalski",
    "Mbeki",
    "Lindqvist",
    "Fernandez",
    "Chen",
    "O'Brien",
    "Diallo",
    "Horvath",
    "Santos",
    "Kim",
    "Romano",
    "Adeyemi",
    "Dubois",
    "Novak",
    "Fitzgerald",
    "Torres",
    "Park",
    "Ivanov",
    "Silva",
    "Walsh",
    "Laurent",
    "Osei",
    "Meyer",
    "Garcia",
    "Papadopoulos",
    "Brennan",
]
PARENT_TEAMS = [
    "Liturgy",
    "Faith Formation",
    "Hospitality",
    "Outreach",
    "Maintenance",
    "Finance",
    "Communications",
    "Music",
    "Youth",
    "Altar Society",
]


async def recreate_bench_db() -> None:
    admin_engine = create_async_engine(
        BASE_URL, isolation_level="AUTOCOMMIT", connect_args={"timeout": 5}
    )
    try:
        async with admin_engine.connect() as conn:
            await conn.execute(
                sa.text("DROP DATABASE IF EXISTS volunteerdb_bench WITH (FORCE)")
            )
            await conn.execute(sa.text("CREATE DATABASE volunteerdb_bench"))
    finally:
        await admin_engine.dispose()

    result = subprocess.run(
        ["uv", "run", "alembic", "upgrade", "head"],
        env={**os.environ, "VDB_DATABASE_URL": BENCH_URL},
        capture_output=True,
        text=True,
    )
    if result.returncode != 0:
        sys.exit(f"alembic failed:\n{result.stdout}\n{result.stderr}")


async def seed(scale: int) -> None:
    """Deterministic synthetic parish: ~50 teams, `scale` volunteers,
    ~2.2 memberships each, history churn for ~20% of volunteers."""
    rng = Random(42)
    async with db_session() as session:
        team_ids: list[int] = []
        for i, parent_name in enumerate(PARENT_TEAMS):
            weight = Decimal(rng.choice(["1", "1.5", "2", "3"])) if i % 2 == 0 else None
            parent = await team_service.create(
                session, parent_name, workload_weight=weight
            )
            team_ids.append(parent.id)
            for k in range(4):
                weight = Decimal(rng.choice(["1", "1.5", "2"])) if k % 2 == 0 else None
                child = await team_service.create(
                    session,
                    f"{parent_name} Group {k + 1}",
                    parent_team_id=parent.id,
                    workload_weight=weight,
                )
                team_ids.append(child.id)

        # 15 published ministry pages (~100 KB html + 3 small images each) —
        # what the /ministries patterns below serve anonymously
        para = (
            "<p class='c1'>Ministry news: formation evenings, serving "
            "schedules, and how newcomers can join this team.</p>"
        )
        doc_html = f"<style>.doc p{{margin:0.4em 0}}</style>{para * 900}"
        png_buffer = BytesIO()
        Image.new("RGB", (40, 30), "gray").save(png_buffer, format="PNG")
        png = png_buffer.getvalue()
        for team_id in team_ids[:15]:
            imgs = "".join(
                f'<img src="/ministries/img/{team_id}/{seq}">' for seq in (1, 2, 3)
            )
            session.add(
                TeamPage(
                    team_id=team_id,
                    html=doc_html + imgs,
                    status="ok",
                    fetched_at=datetime.now(UTC),
                )
            )
            for seq in (1, 2, 3):
                session.add(
                    TeamPageImage(
                        team_id=team_id, seq=seq, image=png, content_type="image/png"
                    )
                )
        await session.execute(
            sa.update(Team)
            .where(Team.id.in_(team_ids[:15]))
            .values(home_doc_url="https://docs.google.com/document/d/benchdoc")
        )

        rows = []
        for i in range(scale):
            first = rng.choice(FIRST_NAMES)
            last = rng.choice(LAST_NAMES)
            rows.append(
                Volunteer(
                    first_name=first,
                    last_name=last,
                    email=f"{first}.{last}.{i}@bench.test".lower().replace("'", ""),
                    phone="555-0100",
                )
            )
        # landmark identities the run command finds by email
        rows[0].first_name, rows[0].last_name, rows[0].email = "Busy", "Bee", BUSY_EMAIL
        rows[1].first_name, rows[1].last_name, rows[1].email = (
            "Lena",
            "Leader",
            LEADER_EMAIL,
        )
        rows[2].first_name, rows[2].last_name, rows[2].email = (
            "Chris",
            "Churn",
            CHURNED_EMAIL,
        )
        session.add_all(rows)
        await session.flush()

        # memberships: bulk-inserted with unique pairs (the trigger only fires
        # on UPDATE/DELETE, so plain INSERTs are exactly what the app produces)
        member_rows = []
        member_teams: list[set[int]] = []
        for i, v in enumerate(rows):
            if i == 0:  # busy landmark: exactly 5 teams for the impact pattern
                picked = set(team_ids[:5])
            elif i == 1:  # leader landmark: leads 3 parent trees
                picked = set(team_ids[k * 5] for k in range(3))
            else:
                n = rng.choices([1, 2, 3, 4], weights=[30, 40, 20, 10])[0]
                picked = set(rng.sample(team_ids, n))
            member_teams.append(picked)
            for t in picked:
                role = (
                    TeamRole.leader
                    if i == 1
                    else rng.choices(list(TeamRole), weights=[5, 10, 15, 70])[0]
                )
                member_rows.append(Membership(volunteer_id=v.id, team_id=t, role=role))
        session.add_all(member_rows)
        await session.flush()

        # history churn for ~20%: join-then-leave an extra team (archives a
        # 'D' row via the versioning trigger) plus one profile edit ('U' row)
        for i, v in enumerate(rows):
            if i % 5 != 2:
                continue
            spare = sorted(set(team_ids) - member_teams[i])
            extra = Membership(
                volunteer_id=v.id, team_id=rng.choice(spare), role=TeamRole.member
            )
            session.add(extra)
            await session.flush()
            await session.delete(extra)
            v.phone = "555-0199"
        await session.flush()

        await custom_field_service.create_def(
            session, "Safeguarding training", FieldType.date, show_in_list=True
        )
        await custom_field_service.create_def(
            session,
            "Preferred contact",
            FieldType.select,
            options=["Email", "Phone", "Post"],
        )

        await user_service.create(
            session, ADMIN_EMAIL, is_admin=True, password=BENCH_PASSWORD
        )
        await user_service.create(
            session, LEADER_EMAIL, volunteer_id=rows[1].id, password=BENCH_PASSWORD
        )

    # bulk load leaves the planner blind until autovacuum catches up
    async with db.engine().begin() as conn:
        await conn.exec_driver_sql("ANALYZE")


# --- measurement -------------------------------------------------------------


@contextlib.contextmanager
def capture_sql(records: list[tuple[str, tuple]]):
    """Record every (statement, parameters) the engine executes."""

    def before(conn, cursor, statement, parameters, context, executemany):
        records.append((statement, parameters))

    engine = db.engine().sync_engine
    event.listen(engine, "before_cursor_execute", before)
    try:
        yield
    finally:
        event.remove(engine, "before_cursor_execute", before)


async def measure(fn, runs: int) -> dict:
    t0 = time.perf_counter()
    await fn()  # cold-ish: first call, may include pool connection setup
    cold_ms = (time.perf_counter() - t0) * 1000
    await fn()  # warmup

    statements: list[tuple[str, tuple]] = []
    with capture_sql(statements):
        await fn()

    times = []
    for _ in range(runs):
        t0 = time.perf_counter()
        await fn()
        times.append((time.perf_counter() - t0) * 1000)
    return {
        "queries": len(statements),
        "cold_ms": round(cold_ms, 2),
        "median_ms": round(statistics.median(times), 2),
        "p90_ms": round(statistics.quantiles(times, n=10)[-1], 2),
        "statements": statements,  # stripped before JSON output
    }


async def explain_statements(statements: list[tuple[str, tuple]]) -> None:
    async with db.engine().connect() as conn:
        for stmt, params in statements:
            head = stmt.lstrip().upper()
            if not head.startswith(("SELECT", "WITH")) or "set_config" in stmt:
                continue
            print(f"\n--- {stmt}")
            try:
                result = await conn.exec_driver_sql(
                    f"EXPLAIN (ANALYZE, BUFFERS) {stmt}", params
                )
                for (line,) in result:
                    print(f"    {line}")
            except Exception as exc:  # e.g. unpreparable statement — skip, don't die
                print(f"    (EXPLAIN failed: {exc})")
                await conn.rollback()


# --- benchmark patterns ------------------------------------------------------


async def find_landmarks() -> dict[str, int]:
    async with db_session() as session:
        marks: dict[str, int] = {}
        for key, email in [("admin_user", ADMIN_EMAIL), ("leader_user", LEADER_EMAIL)]:
            user = await user_service.get_by_email(session, email)
            if user is None:
                sys.exit("bench DB not seeded — run `scripts/bench.py setup` first")
            marks[key] = user.id
        for key, email in [("busy", BUSY_EMAIL), ("churned", CHURNED_EMAIL)]:
            marks[key] = (
                await session.execute(
                    sa.select(Volunteer.id).where(Volunteer.email == email)
                )
            ).scalar_one()
        return marks


async def build_patterns(marks: dict[str, int]) -> dict[str, callable]:
    asof_ts = datetime.now(UTC)
    async with db_session() as session:
        parish_roster = await export_csv(session)  # for the re-import pattern
        # landmark slug for the ministries_page pattern: lowest-id published team
        published_now = await page_service.published_teams(session)
        slug_teams = await team_service.list_all(session)
    page_slug = page_service.slug_map(team_service.team_paths(slug_teams))[
        min(team.id for team in published_now)
    ]

    async def page_volunteers_list():
        # mirrors the data block of ui/volunteers_page.py — keep in sync
        async with db_session() as session:
            user = await user_service.get(session, marks["admin_user"])
            actor = await load_actor(session, user)
            found = await volunteer_service.search(
                session, "", include_inactive=actor.is_admin
            )
            team_sets = await team_ids_map(session, [v.id for v in found])
            [d for d in await custom_field_service.list_defs(session) if d.show_in_list]
            await workload_service.get_config(session)
            await workload_service.visible_scores(session, actor, team_sets)

    async def search_blank():
        async with db_session() as session:
            await volunteer_service.search(session, "", include_inactive=True)

    async def search_name():
        async with db_session() as session:
            await volunteer_service.search(session, "mar", include_inactive=True)

    async def search_email():
        async with db_session() as session:
            await volunteer_service.search(session, "ller", include_inactive=True)

    async def search_asof():
        async with db_session() as session:
            await volunteer_service.search(
                session, "", at=asof_ts, include_inactive=True
            )

    async def load_actor_leader():
        # mirrors page_session/api_ctx: PK user fetch + actor expansion
        async with db_session() as session:
            user = await user_service.get(session, marks["leader_user"])
            await load_actor(session, user)

    async def impact_busy():
        async with db_session() as session:
            await volunteer_service.impact(session, marks["busy"])

    async def timeline_churned():
        async with db_session() as session:
            await volunteer_service.timeline(session, marks["churned"])

    async def import_reimport():
        # idempotent parish re-import; dry_run rolls back so runs are repeatable
        report = await run_import(
            parish_roster, dry_run=True, user_id=marks["admin_user"]
        )
        assert not report.has_errors, report.errors[:3]

    async def ministries_index():
        # mirrors the data block of ui/ministries_routes.ministries_index
        async with db_session() as session:
            published = await page_service.published_teams(session)
            all_teams = await team_service.list_all(session)
        paths = team_service.team_paths(all_teams)
        slugs = page_service.slug_map(paths)
        sorted(
            (paths.get(team.id, team.name), slugs[team.id])
            for team in published
            if team.id in slugs
        )

    async def ministries_page():
        # mirrors the data block of ui/ministries_routes.ministry_page
        async with db_session() as session:
            all_teams = await team_service.list_all(session)
            paths = team_service.team_paths(all_teams)
            slugs = page_service.slug_map(paths)
            team_id = next((tid for tid, s in slugs.items() if s == page_slug), None)
            page = await page_service.published_page(session, team_id)
        assert page is not None and page.html

    async def dropdown_name_map():
        # mirrors the volunteer_options block of ui/teams_page.team_detail
        # (same shape in elections_page and admin_page)
        async with db_session() as session:
            await volunteer_service.name_map(session)

    return {
        "page_volunteers_list": page_volunteers_list,
        "search_blank": search_blank,
        "search_name": search_name,
        "search_email": search_email,
        "search_asof": search_asof,
        "load_actor_leader": load_actor_leader,
        "impact_busy": impact_busy,
        "timeline_churned": timeline_churned,
        "import_reimport": import_reimport,
        "ministries_index": ministries_index,
        "ministries_page": ministries_page,
        "dropdown_name_map": dropdown_name_map,
    }


# --- commands ----------------------------------------------------------------


async def cmd_setup(scale: int) -> None:
    await recreate_bench_db()
    db.init(BENCH_URL)
    t0 = time.perf_counter()
    await seed(scale)
    print(
        f"Seeded volunteerdb_bench: {scale} volunteers, 50 teams "
        f"({time.perf_counter() - t0:.1f}s)"
    )
    await db.engine().dispose()


async def cmd_run(args) -> None:
    db.init(BENCH_URL)
    marks = await find_landmarks()
    async with db_session() as session:
        n_volunteers = (
            await session.execute(sa.select(sa.func.count()).select_from(Volunteer))
        ).scalar()

    results: dict[str, dict] = {}
    for name, fn in (await build_patterns(marks)).items():
        if args.only and args.only not in name:
            continue
        results[name] = await measure(fn, args.runs)
        if args.explain:
            print(f"\n=== EXPLAIN {name} " + "=" * 40)
            await explain_statements(results[name]["statements"])

    print(
        f"\n{'pattern':<22} {'queries':>7} {'cold ms':>9} {'median ms':>10} {'p90 ms':>8}"
    )
    for name, r in results.items():
        print(
            f"{name:<22} {r['queries']:>7} {r['cold_ms']:>9} {r['median_ms']:>10} {r['p90_ms']:>8}"
        )

    if args.json:
        for r in results.values():
            del r["statements"]
        os.makedirs(os.path.dirname(args.json) or ".", exist_ok=True)
        with open(args.json, "w") as f:
            json.dump({"volunteers": n_volunteers, "patterns": results}, f, indent=2)
        print(f"\nwrote {args.json}")
    await db.engine().dispose()


def cmd_compare(before_path: str, after_path: str) -> None:
    with open(before_path) as f:
        before = json.load(f)
    with open(after_path) as f:
        after = json.load(f)
    print(f"volunteers: {before['volunteers']} → {after['volunteers']}")
    print(f"\n{'pattern':<22} {'queries':>12} {'median ms':>18} {'Δ':>8}")
    for name, b in before["patterns"].items():
        a = after["patterns"].get(name)
        if a is None:
            continue
        delta = (
            (a["median_ms"] - b["median_ms"]) / b["median_ms"] * 100
            if b["median_ms"]
            else 0
        )
        print(
            f"{name:<22} {b['queries']:>5} → {a['queries']:<4}"
            f" {b['median_ms']:>8} → {a['median_ms']:<8} {delta:>+7.1f}%"
        )


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="cmd", required=True)
    p_setup = sub.add_parser("setup", help="recreate + seed volunteerdb_bench")
    p_setup.add_argument("--scale", type=int, default=500, help="number of volunteers")
    p_run = sub.add_parser("run", help="time the hot-query patterns")
    p_run.add_argument("--json", help="write results to this JSON file")
    p_run.add_argument(
        "--explain", action="store_true", help="EXPLAIN ANALYZE each SELECT"
    )
    p_run.add_argument("--only", help="run only patterns whose name contains this")
    p_run.add_argument("--runs", type=int, default=15)
    p_cmp = sub.add_parser("compare", help="diff two run JSONs")
    p_cmp.add_argument("before")
    p_cmp.add_argument("after")
    args = parser.parse_args()

    if args.cmd == "setup":
        asyncio.run(cmd_setup(args.scale))
    elif args.cmd == "run":
        asyncio.run(cmd_run(args))
    else:
        cmd_compare(args.before, args.after)


if __name__ == "__main__":
    main()
