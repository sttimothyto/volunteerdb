"""Seed demo data: teams with sub-teams, volunteers, memberships, accounts.

Run: uv run python scripts/seed.py
Idempotent-ish: refuses to run if any volunteers already exist.

Deliberate demo shapes:
- 'Hospitality' has no leader (shows up in the coverage report)
- Maria Alvarez is sole leader of two teams (dramatic impact report)
- Only some teams carry workload weights, and Maria lands deep in the red
  capacity band (colour-coding demo); two custom fields come pre-defined
"""

import asyncio
import os
import sys
from decimal import Decimal

import sqlalchemy as sa

from volunteerdb.db import db_session
from volunteerdb.models import FieldType, TeamRole, Volunteer
from volunteerdb.services import custom_fields, memberships, teams, users, volunteers

# optional capacity weights; unlisted teams stay unweighted (count 0)
WEIGHTS: dict[str, Decimal] = {
    "Liturgy": Decimal("3"),
    "Faith Formation": Decimal("2"),
    "Music Ministry": Decimal("2"),
    "Youth Group": Decimal("2"),
    "Altar Servers": Decimal("1.5"),
    "Altar Society": Decimal("1.5"),
    "Catechists": Decimal("1.5"),
    "Hospitality": Decimal("1"),
}

TEAMS: dict[str, list[str]] = {
    "Liturgy": ["Altar Servers", "Lectors", "Music Ministry", "Sacristans"],
    "Faith Formation": ["Catechists", "Youth Group", "RCIA"],
    "Hospitality": [],
    "St. Vincent de Paul": [],
    "Maintenance": ["Gardening"],
    "Finance Council": [],
    "Communications": [],
    "Altar Society": [],
}

# (first, last, email, [(team, role)])
L, S, C, M = TeamRole.leader, TeamRole.second, TeamRole.core, TeamRole.member
VOLUNTEERS: list[tuple[str, str, str | None, list[tuple[str, TeamRole]]]] = [
    ("Maria", "Alvarez", "maria.alvarez@example.org", [("Liturgy", L), ("Altar Society", L), ("Hospitality", C)]),
    ("James", "Okafor", "james.okafor@example.org", [("Liturgy", S), ("Music Ministry", L)]),
    ("Rose", "Nguyen", "rose.nguyen@example.org", [("Lectors", L), ("Faith Formation", C)]),
    ("Peter", "Kowalski", "peter.kowalski@example.org", [("Altar Servers", L), ("Youth Group", S)]),
    ("Agnes", "Mbeki", "agnes.mbeki@example.org", [("Faith Formation", L), ("Catechists", L)]),
    ("Thomas", "Lindqvist", "thomas.l@example.org", [("Maintenance", L), ("Gardening", S)]),
    ("Lucia", "Fernandez", "lucia.f@example.org", [("St. Vincent de Paul", L), ("Hospitality", S)]),
    ("David", "Chen", "david.chen@example.org", [("Finance Council", L), ("Communications", S)]),
    ("Sarah", "O'Brien", "sarah.obrien@example.org", [("Communications", L), ("Lectors", C)]),
    ("Emmanuel", "Diallo", "emmanuel.d@example.org", [("Youth Group", L), ("Catechists", S)]),
    ("Anna", "Horvath", "anna.h@example.org", [("Sacristans", L), ("Altar Society", S)]),
    ("Miguel", "Santos", "miguel.santos@example.org", [("RCIA", L), ("Faith Formation", S)]),
    ("Grace", "Kim", "grace.kim@example.org", [("Music Ministry", S), ("Liturgy", C)]),
    ("John", "Muller", "john.muller@example.org", [("Finance Council", S), ("Maintenance", C)]),
    ("Teresa", "Romano", "teresa.romano@example.org", [("Altar Society", C), ("Hospitality", M)]),
    ("Paul", "Adeyemi", "paul.a@example.org", [("Altar Servers", S), ("Youth Group", C)]),
    ("Claire", "Dubois", "claire.dubois@example.org", [("Lectors", S), ("RCIA", C)]),
    ("Frank", "Novak", "frank.novak@example.org", [("Gardening", L), ("Maintenance", M)]),
    ("Rita", "Fitzgerald", "rita.f@example.org", [("Sacristans", S), ("Altar Society", M)]),
    ("Samuel", "Torres", "samuel.torres@example.org", [("St. Vincent de Paul", S)]),
    ("Helen", "Park", "helen.park@example.org", [("Catechists", C), ("Communications", C)]),
    ("George", "Ivanov", "george.ivanov@example.org", [("Music Ministry", C)]),
    ("Monica", "Silva", "monica.silva@example.org", [("Hospitality", C), ("St. Vincent de Paul", C)]),
    ("Andrew", "Walsh", None, [("Maintenance", M), ("Gardening", M)]),
    ("Beatrice", "Laurent", "bea.laurent@example.org", [("Lectors", M), ("Altar Society", M)]),
    ("Charles", "Osei", "charles.osei@example.org", [("Youth Group", M), ("Music Ministry", M)]),
    ("Dorothy", "Meyer", None, [("Hospitality", M)]),
    ("Felix", "Garcia", "felix.garcia@example.org", [("Altar Servers", M), ("Youth Group", M)]),
    ("Irene", "Papadopoulos", "irene.p@example.org", [("Catechists", M), ("RCIA", M)]),
    ("Leo", "Brennan", "leo.brennan@example.org", [("St. Vincent de Paul", M), ("Finance Council", M)]),
]


async def seed() -> None:
    async with db_session() as session:
        if (await session.execute(sa.select(sa.func.count()).select_from(Volunteer))).scalar():
            sys.exit("Database already contains volunteers; refusing to seed.")

        team_ids: dict[str, int] = {}
        for parent_name, subs in TEAMS.items():
            parent = await teams.create(
                session, parent_name, workload_weight=WEIGHTS.get(parent_name)
            )
            team_ids[parent_name] = parent.id
            for sub in subs:
                child = await teams.create(
                    session, sub, parent_team_id=parent.id, workload_weight=WEIGHTS.get(sub)
                )
                team_ids[sub] = child.id

        volunteer_ids: dict[str, int] = {}
        for first, last, email, assignments in VOLUNTEERS:
            v = await volunteers.create(session, first, last, email, phone="555-0100")
            volunteer_ids[f"{first} {last}"] = v.id
            for team_name, role in assignments:
                await memberships.assign(session, v.id, team_ids[team_name], role)

        # demo custom fields (admin-extensible volunteer properties)
        await custom_fields.create_def(
            session, "Safeguarding training", FieldType.date, show_in_list=True
        )
        await custom_fields.create_def(
            session, "Preferred contact", FieldType.select, options=["Email", "Phone", "Post"]
        )
        await custom_fields.set_values(
            session,
            volunteer_ids["Maria Alvarez"],
            {"safeguarding_training": "2025-09-14", "preferred_contact": "Email"},
        )
        await custom_fields.set_values(
            session, volunteer_ids["Felix Garcia"], {"preferred_contact": "Phone"}
        )
        await custom_fields.set_values(
            session, volunteer_ids["Agnes Mbeki"], {"safeguarding_training": "2026-01-20"}
        )

        admin_password = os.environ.get("VDB_SEED_ADMIN_PASSWORD", "changeme")
        await users.create(session, "admin@sttimothy.example", is_admin=True, password=admin_password)
        # a leader account to try out per-team permissions
        await users.create(
            session,
            "maria.alvarez@example.org",
            volunteer_id=volunteer_ids["Maria Alvarez"],
            password="volunteer",
        )
        # a plain member account
        await users.create(
            session,
            "felix.garcia@example.org",
            volunteer_id=volunteer_ids["Felix Garcia"],
            password="volunteer",
        )

    print("Seeded:")
    print(f"  {sum(len(s) + 1 for s in TEAMS.values())} teams, {len(VOLUNTEERS)} volunteers")
    print(f"  {len(WEIGHTS)} weighted teams + 2 custom fields (capacity/fields demo)")
    print(f"  admin login:  admin@sttimothy.example / {admin_password}")
    print("  leader login: maria.alvarez@example.org / volunteer")
    print("  member login: felix.garcia@example.org / volunteer")


if __name__ == "__main__":
    asyncio.run(seed())
