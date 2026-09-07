"""Seed a demo parish: teams, people, rosters, schedule, elections, public pages.

Run: uv run python scripts/seed.py
Refuses to run if any volunteers already exist (`make fresh` wipes first).

Everything is deterministic — one fixed RNG seed, one clock read, dates
relative to today — so two runs produce the same parish and anything
reproduced on seeded data is reproducible for whoever reads the report.

The parish is PARISH_SIZE people: everyone a documented shape below depends on
is hand-written, and the rest of the number is generated around them.

Deliberate demo shapes
----------------------
People and rosters
- 'Hospitality' has no leader (it heads the coverage report's vacancies), nor
  do 'Prayer Chain', 'Children's Liturgy', 'Website & Socials' or
  'Bereavement Ministry'; 'Welcome Desk' and 'Ushers' have a leader but no
  second. Each vacancy is the seat one of the proposals below is filling.
- Maria Alvarez is sole leader of two teams and lands in the top workload
  band (dramatic impact report + the colour-coding demo)
- ended/rejoined memberships (their history backdated) and three mid-spell
  promotions feed the service-timeline chart — Maria's ended Youth Group
  spell, Grace's split Music Ministry spells, Peter's promotion to leader
- a family shares one email address (the account provisioner's "skipped"
  path), a fifth of the parish has no email at all, and a handful are archived
  (is_active false) but keep their memberships
- the 'Roof Appeal Committee' has wound up: archived rather than deleted, so
  it is in nobody's listing and the service given to it is still readable
- one custom field of every FieldType the app offers (FIELDS), filled in for
  a share of the parish, plus one retired field whose answers survive it
- 'Clergy' is filled, and the roll builder finds it by that name, so every
  proposal's voting roll is prefilled with it; Fr. Dominic also sits on
  Finance Council, which exercises the roll's dedupe path

Accounts
- passwords, an unredeemed invite, a code-only account and a deactivated one
- the administrator carries an API bearer token and two members carry a
  personal .ics feed address; both are printed at the end
- one account has an address change staged and waiting on the new mailbox

Schedule
- six weekly series (Sunday Mass rosters, choir practice, youth nights, food
  bank sorting, coffee Sunday) straddling today, plus one-off work days, a
  funeral reception and the parish picnic
- past events carry a filled roster and therefore derived attendance and
  hours; three of them carry manager overrides (a no-show, a long shift)
- future events carry RSVPs, two open substitution calls, one claimed one and
  one cancelled event with its assignees still attached; one slot is handed
  over by a manager and one is given up by the person holding it
- the picnic is staffed by a task force: three ministries' rosters copied into
  an auto-created meta team the event sits on until the night it is over
- one sign-up is copied forward onto every later week of its series, and one
  series opts into the seven-day reminder as well as the next-day one

Elections
- one proposal in every state: nominating, voting (ballots half in),
  concluded and awaiting a decision, appointed (its membership created),
  cancelled, and a concluded round re-opened as a fresh one (new_round)
- the nominating one also carries a name put forward late and a voter added
  to the roll by hand — both of which only that phase allows

Public
- ministries publish a home page, one of them with a picture: the cached HTML
  and image bytes are written straight in, because dev has no Google to fetch
  them from. One page is stale (its last fetch failed and the last good HTML
  is still served) and one is linked but not yet fetched.

Bookkeeping
- the notices the nightly jobs would have sent are recorded as sent, the jobs
  themselves as having run today, a fortnight of the mail allowance as spent,
  and five teams as linked to a roster spreadsheet (one of them in error)

Deliberately not seeded
-----------------------
Anything whose value is a live credential for somebody else's service: no
Google calendar id or synced event ids (jobs/calendar_sync.py would then try
to PATCH events on a calendar that does not exist), and no one-time sign-in
code (it would expire ten minutes into the demo).

Passwords
---------
Every seeded account signs in with `demo` (VDB_SEED_ADMIN_PASSWORD still
overrides the admin's). Four characters does NOT clear the password policy in
volunteerdb/passwords.py, so this script hashes it directly instead of going
through users.create/set_password, which would rightly refuse it. That
bypass is a localhost-only convenience — no path a *user* can reach was
widened, and nothing but a throwaway development database should ever see
this script.
"""

import asyncio
import hashlib
import os
import random
import sys
import uuid
from collections.abc import Awaitable, Iterable
from dataclasses import dataclass, field
from datetime import UTC, date, datetime, time, timedelta
from decimal import Decimal
from io import BytesIO

import sqlalchemy as sa
from PIL import Image, ImageDraw, ImageFont
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession

from volunteerdb import env as env_mod
from volunteerdb import scheduler
from volunteerdb.auth import async_hash_password
from volunteerdb.db import transaction
from volunteerdb.domain import NotifyMode, Outcome
from volunteerdb.errors import DomainError, message
from volunteerdb.fp import Err, Result
from volunteerdb.models import (
    AppSetting,
    AppUser,
    CustomFieldDef,
    Event,
    EventAssignment,
    EventRsvp,
    EventSlot,
    EventSubRequest,
    EventTaskForceSource,
    FieldType,
    JobRun,
    MailQuota,
    Membership,
    Notification,
    NotificationStage,
    Proposal,
    ProposalBallot,
    ProposalCandidate,
    ProposalVoter,
    SiteLogo,
    SyncStatus,
    Team,
    TeamPage,
    TeamPageImage,
    TeamRole,
    TeamSheet,
    Volunteer,
    VolunteerPhoto,
)
from volunteerdb.permissions import SYSTEM, Actor
from volunteerdb.services import (
    branding,
    custom_fields,
    elections,
    events,
    memberships,
    pages,
    photos,
    roster_sheets,
    task_force,
    teams,
    users,
    volunteers,
    workload,
)

# --- knobs --------------------------------------------------------------------

# Localhost demo password. Short on purpose (see the module docstring); the
# admin's can still be overridden for a demo that is shown to anybody.
DEMO_PASSWORD = "demo"
ADMIN_PASSWORD = os.environ.get("VDB_SEED_ADMIN_PASSWORD", DEMO_PASSWORD)

# The administrator's bearer token for the JSON API (docs/reference/http-api.md),
# fixed so a reseed does not invalidate the curl line in somebody's notes. Only
# its digest is stored, as in production; the plaintext is a localhost demo
# convenience on the same terms as DEMO_PASSWORD above.
DEMO_API_TOKEN = "vdb-demo-token-do-not-use-in-production"

# Every address the seed invents lives in example.org — RFC 2606 reserves it,
# so no seeded mail can ever reach a real mailbox if SMTP is switched on.
EMAIL_DOMAIN = "example.org"

# One seed for every random choice below: rosters, ballots and custom-field
# values are then stable across runs, which is what makes "reseed and try
# again" a real reproduction step.
RNG_SEED = 20260816

# The parish ends up this size. The hand-written people below are all of a
# documented demo shape; the rest of the number is generated around them.
PARISH_SIZE = 500

ENV = env_mod.build()  # the seed's one Env: its engine, clock and settings
TZ = ENV.tz
# One clock read for the whole seed, passed to every service that takes a
# `now` -- the same discipline the edges keep (api/deps.Ctx), and what makes
# "created just now" mean one instant rather than a smear across the run.
NOW = ENV.clock.now()
TODAY = NOW.astimezone(TZ).date()


def db_session(user_id: int | None = None):
    return transaction(ENV, user_id)


def ok[T](result: Result[T, DomainError] | Result[Outcome[T], DomainError]) -> T:
    """The value of a service call that cannot legitimately be refused here.

    The seed acts as SYSTEM -- or as the volunteer themself, where the act is
    theirs (a sign-up, a ballot) -- on an empty database, with data it wrote
    itself: a refusal means the seed and the services have drifted apart,
    which is a bug to read rather than a stack trace to decipher -- so it says
    which call refused and why.

    An `Outcome` is unwrapped to its value and its domain events are dropped on
    purpose: they are what a real door would mail, and nobody is written to
    about a parish that does not exist.
    """
    if isinstance(result, Err):
        sys.exit(f"seed: refused: {message(result.error)}")
    value = result.value
    return value.value if isinstance(value, Outcome) else value  # type: ignore[return-value]


L, S, C, M = TeamRole.leader, TeamRole.second, TeamRole.core, TeamRole.member


# The bands this parish settled on: an admin may rename, recolour and add
# them (services/workload.py, docs/guide/how-to/set-workload-bands.md), and a
# demo running the shipped defaults shows none of that. The thresholds below
# keep the shipped meaning up to 8 and split what used to be one open-ended
# "red" in two, which is what puts Maria Alvarez in a band of her own.
WORKLOAD_CONFIG = workload.WorkloadConfig(
    multipliers={
        TeamRole.leader: Decimal("3"),
        TeamRole.second: Decimal("2"),
        TeamRole.core: Decimal("1.5"),
        TeamRole.member: Decimal("1"),
    },
    bands=[
        workload.Band("light", "#4caf50", Decimal("4")),
        workload.Band("steady", "#ffb300", Decimal("8")),
        workload.Band("heavy", "#c62828", Decimal("14")),
        workload.Band("too much", "#6a1b9a", None),
    ],
)


# --- the parish: teams --------------------------------------------------------


@dataclass(frozen=True)
class TeamSpec:
    """A team and its sub-teams. `weight` is the optional workload weight
    ("how work-heavy is this ministry"); unweighted teams count 0.
    `archived` is a ministry that has wound up: kept, not deleted."""

    name: str
    weight: str | None = None
    description: str | None = None
    children: tuple["TeamSpec", ...] = ()
    archived: bool = False


T = TeamSpec

TEAMS: tuple[TeamSpec, ...] = (
    T(
        "Liturgy",
        "3",
        "Everything that happens in the sanctuary, and everyone who makes it happen.",
        (
            T("Altar Servers", "1.5", "Servers for Sunday and feast-day Masses."),
            T("Lectors", "1", "Readers of the first and second reading."),
            T(
                "Music Ministry",
                "2",
                "Choirs, cantors and instrumentalists across all four Masses.",
                (
                    T("Choir", "1.5", "The 10:30 choir. Practice on Thursdays."),
                    T("Cantors", "1", "Solo singers for the 9:00 and 12:00 Masses."),
                ),
            ),
            T("Sacristans", "1", "Vessels, linens, vestments and the sacristy."),
            T("Ushers", "1", "Welcome, seating, the collection and the count."),
        ),
    ),
    T(
        "Faith Formation",
        "2",
        "Handing the faith on, from the nursery to the RCIA font.",
        (
            T("Catechists", "1.5", "Sunday-morning classes, grades 1 through 8."),
            T("Youth Group", "2", "Friday nights for grades 9 through 12."),
            T("RCIA", "1.5", "Accompanying adults towards the Easter sacraments."),
            T("Bible Study", "1", "Wednesday-morning scripture group."),
            T("Children's Liturgy", "1", "The Word for the little ones, during Mass."),
        ),
    ),
    T(
        "Hospitality",
        "1",
        "Coffee, welcome and the parish's small talk.",
        (
            T(
                "Coffee Sunday",
                "0.5",
                "Coffee and squares in the hall after the 10:30.",
            ),
            T("Welcome Desk", "0.5", "The desk in the narthex; new-parishioner packs."),
        ),
    ),
    T(
        "St. Vincent de Paul",
        "2",
        "The parish's works of mercy: food, rent, company.",
        (
            T("Food Bank", "1.5", "Saturday-morning sorting and Tuesday hampers."),
            T("Home Visits", "1", "Visiting the housebound and the newly bereaved."),
        ),
    ),
    T(
        "Maintenance",
        "1",
        "Keeping the buildings and grounds standing and tidy.",
        (
            T("Gardening", "0.5", "Beds, planters and the memorial garden."),
            T("Building Care", "1", "Small repairs, seasonal work days, the boiler."),
        ),
    ),
    T("Finance Council", "1.5", "Budget, counting, and the annual report."),
    T(
        "Communications",
        "1",
        "How the parish talks to itself and to the neighbourhood.",
        (
            T("Bulletin", "1", "The weekly bulletin: copy, layout and printing."),
            T("Website & Socials", "1", "The parish site and its social accounts."),
        ),
    ),
    T("Altar Society", "1.5", "Linens, flowers and the sanctuary's seasons."),
    T("Knights of Columbus", "1", "Council 8842: breakfasts, charity, fraternity."),
    T("Catholic Women's League", "1", "Faith, service and social justice."),
    T("Bereavement Ministry", "1", "Funeral receptions and accompaniment after."),
    T("Prayer Chain", "0.5", "The phone-and-email chain of intercession."),
    T("Parish Picnic Task Force", "1", "Stands up each spring for the June picnic."),
    # appended last so the ids above stay put; must be named exactly "Clergy"
    # (services/elections.CLERGY_TEAM_NAME) to be the parish's clergy team
    T("Clergy", None, "The priests and deacon serving the parish."),
    # wound up, and archived rather than deleted: the service its members gave
    # stays in the history, while every live listing and the coverage report
    # pass it by (they filter on is_active)
    T(
        "Roof Appeal Committee",
        "1",
        "Raised for the 2019 roof. Wound up when the last of it was paid off.",
        archived=True,
    ),
)

# Teams the generated cohort may be dropped into. Clergy is hand-written only,
# and nobody is generated into a leadership role — that is what keeps the
# deliberately vacant seats vacant for the coverage report.
NEVER_GENERATED = frozenset({"Clergy", "Roof Appeal Committee"})

# Teams that carry the demo's activity, so the generated cohort is weighted
# towards them: rosters need bodies to schedule.
POPULAR = (
    "Lectors",
    "Altar Servers",
    "Choir",
    "Youth Group",
    "Food Bank",
    "Coffee Sunday",
    "Parish Picnic Task Force",
    "Ushers",
    "Hospitality",
    "Catechists",
    "Bereavement Ministry",
)


# --- the parish: people -------------------------------------------------------


@dataclass(frozen=True)
class Person:
    first: str
    last: str
    email: str | None
    teams: tuple[tuple[str, TeamRole], ...] = ()
    notes: str | None = None
    active: bool = True

    @property
    def name(self) -> str:
        return f"{self.first} {self.last}"


P = Person

# The hand-written parish: everyone a documented demo shape depends on. The
# generated cohort (below) fills the rosters out around them.
CURATED: tuple[Person, ...] = (
    P(
        "Maria",
        "Alvarez",
        "maria.alvarez@example.org",
        (("Liturgy", L), ("Altar Society", L), ("Hospitality", C)),
        notes="Sacristy keys. Prefers a phone call to an email.",
    ),
    P(
        "James",
        "Okafor",
        "james.okafor@example.org",
        (("Liturgy", S), ("Music Ministry", L)),
    ),
    P(
        "Rose",
        "Nguyen",
        "rose.nguyen@example.org",
        (("Lectors", L), ("Faith Formation", C)),
        notes="Trains new readers; ask her before adding anyone to the rota.",
    ),
    P(
        "Peter",
        "Kowalski",
        "peter.kowalski@example.org",
        (("Altar Servers", L), ("Youth Group", S)),
    ),
    P(
        "Agnes",
        "Mbeki",
        "agnes.mbeki@example.org",
        (("Faith Formation", L), ("Catechists", L)),
    ),
    P(
        "Thomas",
        "Lindqvist",
        "thomas.l@example.org",
        (("Maintenance", L), ("Gardening", S)),
    ),
    P(
        "Lucia",
        "Fernandez",
        "lucia.f@example.org",
        (("St. Vincent de Paul", L), ("Hospitality", S)),
    ),
    P(
        "David",
        "Chen",
        "david.chen@example.org",
        (("Finance Council", L), ("Communications", S)),
    ),
    P(
        "Sarah",
        "O'Brien",
        "sarah.obrien@example.org",
        (("Communications", L), ("Lectors", C)),
    ),
    P(
        "Emmanuel",
        "Diallo",
        "emmanuel.d@example.org",
        (("Youth Group", L), ("Catechists", S)),
    ),
    P(
        "Anna",
        "Horvath",
        "anna.h@example.org",
        (("Sacristans", L), ("Altar Society", S)),
    ),
    P(
        "Miguel",
        "Santos",
        "miguel.santos@example.org",
        (("RCIA", L), ("Faith Formation", S)),
    ),
    P(
        "Grace",
        "Kim",
        "grace.kim@example.org",
        (("Music Ministry", S), ("Liturgy", C)),
    ),
    P(
        "John",
        "Muller",
        "john.muller@example.org",
        (("Finance Council", S), ("Maintenance", C)),
    ),
    P(
        "Teresa",
        "Romano",
        "teresa.romano@example.org",
        (("Altar Society", C), ("Hospitality", M)),
    ),
    P(
        "Paul",
        "Adeyemi",
        "paul.a@example.org",
        (("Altar Servers", S), ("Youth Group", C)),
    ),
    P(
        "Claire",
        "Dubois",
        "claire.dubois@example.org",
        (("Lectors", S), ("RCIA", C)),
    ),
    P(
        "Frank",
        "Novak",
        "frank.novak@example.org",
        (("Gardening", L), ("Maintenance", M)),
    ),
    P(
        "Rita",
        "Fitzgerald",
        "rita.f@example.org",
        (("Sacristans", S), ("Altar Society", M)),
    ),
    P("Samuel", "Torres", "samuel.torres@example.org", (("St. Vincent de Paul", S),)),
    P(
        "Helen",
        "Park",
        "helen.park@example.org",
        (("Catechists", C), ("Communications", C)),
        notes="Parish office. Second administrator account.",
    ),
    P("George", "Ivanov", "george.ivanov@example.org", (("Music Ministry", C),)),
    P(
        "Monica",
        "Silva",
        "monica.silva@example.org",
        (("Hospitality", C), ("St. Vincent de Paul", C)),
    ),
    P("Andrew", "Walsh", None, (("Maintenance", M), ("Gardening", M))),
    P(
        "Beatrice",
        "Laurent",
        "bea.laurent@example.org",
        (("Lectors", M), ("Altar Society", M)),
    ),
    P(
        "Charles",
        "Osei",
        "charles.osei@example.org",
        (("Youth Group", M), ("Music Ministry", M)),
    ),
    P("Dorothy", "Meyer", None, (("Hospitality", M),)),
    P(
        "Felix",
        "Garcia",
        "felix.garcia@example.org",
        (("Altar Servers", M), ("Youth Group", M)),
    ),
    P(
        "Irene",
        "Papadopoulos",
        "irene.p@example.org",
        (("Catechists", M), ("RCIA", M)),
    ),
    P(
        "Leo",
        "Brennan",
        "leo.brennan@example.org",
        (("St. Vincent de Paul", M), ("Finance Council", M)),
    ),
    # the clergy: on every proposal's voting roll, whatever their role here
    P(
        "Dominic",
        "Ferraro",
        "dominic.ferraro@example.org",
        (("Clergy", L), ("Finance Council", C)),
        notes="Pastor.",
    ),
    P("Stephen", "Bianchi", "stephen.bianchi@example.org", (("Clergy", S),)),
    P(
        "Joseph",
        "Tran",
        "joseph.tran@example.org",
        (("Clergy", M), ("RCIA", C)),
        notes="Permanent deacon.",
    ),
    # --- the ministries added around the original demo parish ---
    P(
        "Vincent",
        "Okonkwo",
        "vincent.okonkwo@example.org",
        (("Ushers", L), ("Hospitality", M)),
        notes="Counts the collection with the Finance Council on Mondays.",
    ),
    P(
        "Marta",
        "Kaminski",
        "marta.kaminski@example.org",
        (("Ushers", C), ("Welcome Desk", C)),
    ),
    P(
        "Cecilia",
        "Moreau",
        "cecilia.moreau@example.org",
        (("Choir", L), ("Music Ministry", C)),
    ),
    P("Alban", "Rossi", "alban.rossi@example.org", (("Choir", S), ("Cantors", C))),
    P("Nora", "Sullivan", "nora.sullivan@example.org", (("Cantors", L), ("Choir", M))),
    P("Salome", "Haile", "salome.haile@example.org", (("Cantors", S), ("Choir", M))),
    P(
        "Ruth",
        "Abara",
        "ruth.abara@example.org",
        (("Bible Study", L), ("Faith Formation", C)),
    ),
    P("Martin", "Vogel", "martin.vogel@example.org", (("Bible Study", S), ("RCIA", M))),
    P(
        "Bridget",
        "Hayes",
        "bridget.hayes@example.org",
        (("Children's Liturgy", C), ("Catechists", M)),
    ),
    P(
        "Rafael",
        "Ortega",
        "rafael.ortega@example.org",
        (("Children's Liturgy", C), ("Youth Group", M)),
    ),
    P(
        "Anita",
        "Bakker",
        "anita.bakker@example.org",
        (("Children's Liturgy", C), ("Catechists", M)),
    ),
    P(
        "Estela",
        "Cruz",
        "estela.cruz@example.org",
        (("Coffee Sunday", L), ("Hospitality", M)),
    ),
    P(
        "Bernard",
        "Quinn",
        "bernard.quinn@example.org",
        (("Welcome Desk", L), ("Hospitality", M)),
    ),
    P(
        "Yosef",
        "Tesfaye",
        "yosef.tesfaye@example.org",
        (("Food Bank", L), ("St. Vincent de Paul", C)),
    ),
    P(
        "Cathy",
        "Doyle",
        "cathy.doyle@example.org",
        (("Food Bank", S), ("Home Visits", M)),
    ),
    P(
        "Perpetua",
        "Nwosu",
        "perpetua.nwosu@example.org",
        (("Home Visits", L), ("St. Vincent de Paul", C)),
    ),
    P(
        "Ivan",
        "Petrov",
        "ivan.petrov@example.org",
        (("Building Care", L), ("Maintenance", C)),
    ),
    P(
        "Simone",
        "Beaulieu",
        "simone.beaulieu@example.org",
        (("Bulletin", L), ("Communications", C)),
    ),
    P(
        "Kevin",
        "Tran",
        "kevin.tran@example.org",
        (("Website & Socials", S), ("Bulletin", M)),
    ),
    P(
        "Gregory",
        "Nakamura",
        "gregory.nakamura@example.org",
        (("Website & Socials", C), ("Communications", M)),
    ),
    P(
        "Patrick",
        "Byrne",
        "patrick.byrne@example.org",
        (("Knights of Columbus", L), ("Ushers", M)),
    ),
    P(
        "Alonso",
        "Reyes",
        "alonso.reyes@example.org",
        (("Knights of Columbus", S), ("Maintenance", M)),
    ),
    P(
        "Philomena",
        "Achebe",
        "philomena.achebe@example.org",
        (("Catholic Women's League", L), ("Altar Society", C)),
    ),
    P(
        "Deirdre",
        "Walsh",
        "deirdre.walsh@example.org",
        (("Catholic Women's League", S), ("Hospitality", C)),
    ),
    P(
        "Bernadette",
        "Osei",
        "bernadette.osei@example.org",
        (("Bereavement Ministry", C), ("Catholic Women's League", M)),
    ),
    P(
        "Camille",
        "Rousseau",
        "camille.rousseau@example.org",
        (("Bereavement Ministry", C), ("Altar Society", M)),
    ),
    P(
        "Dolores",
        "Vasquez",
        "dolores.vasquez@example.org",
        (("Prayer Chain", C), ("Bereavement Ministry", M)),
    ),
    P(
        "Ignatius",
        "Mensah",
        "ignatius.mensah@example.org",
        (("Prayer Chain", C), ("Bible Study", M)),
    ),
    P(
        "Colette",
        "Amara",
        "colette.amara@example.org",
        (("Parish Picnic Task Force", L), ("Hospitality", C)),
    ),
    P(
        "Hugo",
        "Lindberg",
        "hugo.lindberg@example.org",
        (("Parish Picnic Task Force", S), ("Knights of Columbus", M)),
    ),
    # one household, one mailbox: bulk_provision gives the first an account and
    # reports the rest as skipped, and users._volunteer_for_email refuses to
    # guess which of them a new account at that address belongs to
    P(
        "Dominique",
        "Okonjo",
        "okonjo.family@example.org",
        (("Coffee Sunday", M), ("Ushers", M)),
        notes="Shares the family mailbox with Adaeze.",
    ),
    P(
        "Adaeze",
        "Okonjo",
        "okonjo.family@example.org",
        (("Youth Group", M), ("Children's Liturgy", M)),
        notes="Shares the family mailbox with Dominique.",
    ),
    # archived, but their history stays on the teams they served
    P(
        "Bartholomew",
        "Ng",
        "bart.ng@example.org",
        (("Ushers", M), ("Building Care", M)),
        notes="Moved to Vancouver in the spring.",
        active=False,
    ),
    P(
        "Eileen",
        "Fitzpatrick",
        None,
        (("Altar Society", M),),
        notes="In long-term care since February; keep on the prayer list.",
        active=False,
    ),
)

# Name pools for the generated cohort. Walked with coprime strides, so the
# pairs stay distinct for far longer than COHORT_SIZE.
FIRST_NAMES = (
    "Aidan",
    "Amara",
    "Antonio",
    "Beatriz",
    "Blessing",
    "Bruno",
    "Carmen",
    "Cedric",
    "Chiara",
    "Clara",
    "Damian",
    "Delphine",
    "Ekaterina",
    "Elena",
    "Emeka",
    "Esther",
    "Fabian",
    "Fiona",
    "Gabriel",
    "Gemma",
    "Hannah",
    "Henryk",
    "Ingrid",
    "Isabel",
    "Jacinta",
    "Jerome",
    "Josephine",
    "Kwame",
    "Laszlo",
    "Lenora",
    "Lorenzo",
    "Magdalena",
    "Malachy",
    "Mercy",
    "Nadia",
    "Nikolai",
    "Oscar",
    "Priscilla",
    "Quentin",
    "Renata",
    "Rosario",
    "Sean",
    "Sofia",
    "Tadeusz",
    "Ursula",
    "Valentina",
    "Wilfred",
    "Zainab",
)
LAST_NAMES = (
    "Abbott",
    "Ahmadi",
    "Arcand",
    "Barros",
    "Bergeron",
    "Castellanos",
    "Colombo",
    "Dabrowski",
    "Delgado",
    "Ferreira",
    "Gagnon",
    "Grimaldi",
    "Hoffmann",
    "Ibarra",
    "Jelinek",
    "Kalu",
    "Kovacs",
    "Lachance",
    "Larsen",
    "Maalouf",
    "Marchetti",
    "Mbaye",
    "Nakagawa",
    "Novikov",
    "Obi",
    "Olsen",
    "Pereira",
    "Pham",
    "Quiroga",
    "Radu",
    "Sandoval",
    "Schneider",
    "Sicard",
    "Sorensen",
    "Tabone",
    "Thibault",
    "Uddin",
    "Valdez",
    "Villanueva",
    "Weiss",
    "Yamamoto",
    "Zabala",
    "Zhu",
)

# ended memberships: (person, team, role, joined, left) — created and removed
# through the service layer so the versioning trigger archives them
PAST_SPELLS: tuple[tuple[str, str, TeamRole, date, date], ...] = (
    ("Maria Alvarez", "Youth Group", L, date(2019, 5, 1), date(2023, 6, 30)),
    ("Grace Kim", "Music Ministry", M, date(2017, 2, 1), date(2020, 11, 15)),
    ("Frank Novak", "Hospitality", M, date(2015, 4, 1), date(2018, 8, 1)),
    ("James Okafor", "Cantors", S, date(2016, 9, 1), date(2021, 7, 31)),
    ("Rose Nguyen", "RCIA", C, date(2018, 1, 15), date(2022, 5, 20)),
    ("Peter Kowalski", "Ushers", M, date(2014, 3, 1), date(2019, 9, 30)),
    ("Agnes Mbeki", "Children's Liturgy", L, date(2012, 9, 1), date(2020, 6, 30)),
    ("Thomas Lindqvist", "Building Care", C, date(2013, 6, 1), date(2021, 10, 15)),
    ("Lucia Fernandez", "Food Bank", C, date(2016, 11, 1), date(2024, 2, 29)),
    ("David Chen", "Bulletin", M, date(2015, 1, 5), date(2019, 12, 31)),
    ("Sarah O'Brien", "Youth Group", S, date(2011, 9, 1), date(2016, 6, 30)),
    ("Teresa Romano", "Choir", M, date(2010, 2, 1), date(2014, 12, 20)),
    ("Vincent Okonkwo", "Altar Servers", M, date(2009, 9, 1), date(2015, 6, 30)),
    ("Yosef Tesfaye", "Home Visits", M, date(2017, 4, 1), date(2023, 3, 31)),
    ("Bartholomew Ng", "Gardening", M, date(2018, 5, 1), date(2026, 3, 31)),
    # the wound-up committee: nobody serves on it now, and the five years
    # somebody did are still there to be read
    ("David Chen", "Roof Appeal Committee", L, date(2019, 2, 1), date(2024, 11, 30)),
    ("Ivan Petrov", "Roof Appeal Committee", S, date(2019, 2, 1), date(2024, 11, 30)),
    ("Teresa Romano", "Roof Appeal Committee", M, date(2019, 6, 1), date(2023, 9, 30)),
)

# assigned as a plain member first, then promoted: a mid-spell role change
# (op='U' history row) draws a two-colour bar on the timeline
PROMOTIONS: frozenset[tuple[str, str]] = frozenset(
    {
        ("Peter Kowalski", "Altar Servers"),
        ("Yosef Tesfaye", "Food Bank"),
        ("Cecilia Moreau", "Choir"),
    }
)


# --- custom fields ------------------------------------------------------------


@dataclass(frozen=True)
class FieldSpec:
    """One admin-defined volunteer property.

    Every FieldType the app offers appears exactly once below, so the fields
    admin, the volunteer form, the list columns and the query language each
    have a live example of every kind of value they can be handed — the exact
    numbers (integer, decimal), the temporal ones (time, interval) and the two
    timestamps that differ only by whether they carry a zone."""

    label: str
    field_type: FieldType
    fill: float  # the share of the parish carrying a value
    options: tuple[str, ...] | None = None
    show_in_list: bool = False
    retired: bool = False  # filled in, then deactivated (see seed_people)


F = FieldSpec

FIELDS: tuple[FieldSpec, ...] = (
    F("Safeguarding training", FieldType.date, 0.65, show_in_list=True),
    F("Preferred contact", FieldType.select, 0.75, ("Email", "Phone", "Post")),
    F("Police check on file", FieldType.checkbox, 0.55, show_in_list=True),
    F("T-shirt size", FieldType.select, 0.4, ("S", "M", "L", "XL", "XXL")),
    F("Years in the parish", FieldType.number, 0.5),
    F("Previous parish", FieldType.text, 0.3),
    F("Children in the programme", FieldType.integer, 0.35, show_in_list=True),
    F("Mileage rate claimed", FieldType.decimal, 0.15),
    F("Usual arrival time", FieldType.time, 0.25),
    F("Time given each week", FieldType.interval, 0.3),
    F("Registration form received", FieldType.timestamp, 0.45),
    F("Photo consent recorded", FieldType.timestamptz, 0.35),
    F("Diocesan volunteer id", FieldType.uuid, 0.2),
    # the field the parish stopped using; its answers stay on the people who
    # gave them, and it disappears from the form
    F("Bazaar stall 2019", FieldType.text, 0.2, retired=True),
)

PREVIOUS_PARISHES = (
    "St. Brigid's, Ottawa",
    "Holy Rosary",
    "Our Lady of Lourdes",
    "St. Michael's Cathedral",
    "Santa Cruz, Manila",
    "St. Patrick's, Galway",
    "None — this is home",
)

BAZAAR_STALLS = ("Bake table", "Raffle", "White elephant", "Tea room", "Door")


def _field_value(spec: FieldSpec, rng: random.Random) -> object:
    """A value in the JSON encoding fieldcodec expects for this field's type —
    the same shape the write path would have normalized a form entry to."""
    match spec.field_type:
        case FieldType.date:
            return (TODAY - timedelta(days=rng.randint(30, 1400))).isoformat()
        case FieldType.select:
            assert spec.options is not None
            return rng.choice(spec.options)
        case FieldType.checkbox:
            return rng.random() < 0.8
        case FieldType.number:
            return rng.randint(1, 45)
        case FieldType.text:
            if spec.retired:
                return rng.choice(BAZAAR_STALLS)
            return rng.choice(PREVIOUS_PARISHES)
        case FieldType.integer:
            return rng.randint(0, 4)
        case FieldType.decimal:
            # a decimal travels as text: 0.55 the float is not 0.55 the money
            return f"0.{rng.randint(45, 72)}"
        case FieldType.time:
            return time(
                rng.choice((8, 9, 10, 16, 18)), rng.choice((0, 15, 30))
            ).isoformat()
        case FieldType.interval:
            return f"PT{rng.randint(1, 6)}H{rng.choice((0, 15, 30, 45))}M"
        case FieldType.timestamp:
            # no zone: a form was handed in at a wall-clock time, full stop
            return datetime.combine(
                TODAY - timedelta(days=rng.randint(60, 2000)),
                time(rng.randint(9, 17), rng.choice((0, 20, 40))),
            ).isoformat()
        case FieldType.timestamptz:
            # a zone: the moment consent was given, comparable across zones
            return (NOW - timedelta(days=rng.randint(1, 900))).isoformat()
        case FieldType.uuid:
            return str(uuid.UUID(int=rng.getrandbits(128), version=4))
    raise AssertionError(f"no seed value for {spec.field_type}")  # pragma: no cover


# --- accounts -----------------------------------------------------------------


@dataclass(frozen=True)
class Account:
    email: str
    person: str | None = None  # volunteer full name; None = not a person
    is_admin: bool = False
    password: bool = True  # False: no password, sign in with an emailed code
    invite: bool = False  # keep the invite link armed (redemption demo)
    active: bool = True
    headline: str = ""  # printed in the summary when set
    # the three things an account can be handed besides a way in: a bearer
    # token for the JSON API, a personal .ics feed address, and an address
    # change waiting for the new mailbox to confirm it
    api_token: bool = False
    calendar_feed: bool = False
    pending_email: str | None = None
    last_login_days: int | None = None  # days ago; None = has never signed in


A = Account

ACCOUNTS: tuple[Account, ...] = (
    A(
        f"admin@{EMAIL_DOMAIN}",
        None,
        is_admin=True,
        headline="administrator",
        api_token=True,
        last_login_days=0,
    ),
    A("helen.park@example.org", "Helen Park", is_admin=True, last_login_days=2),
    A(
        "maria.alvarez@example.org",
        "Maria Alvarez",
        headline="ministry leader (two teams, the heaviest workload band)",
        calendar_feed=True,
        last_login_days=1,
    ),
    A(
        "felix.garcia@example.org",
        "Felix Garcia",
        headline="plain member",
        calendar_feed=True,
        last_login_days=5,
    ),
    A(
        "dominic.ferraro@example.org",
        "Dominic Ferraro",
        headline="clergy — sits on every voting roll",
    ),
    A("stephen.bianchi@example.org", "Stephen Bianchi"),
    A("joseph.tran@example.org", "Joseph Tran"),
    A("james.okafor@example.org", "James Okafor"),
    A("rose.nguyen@example.org", "Rose Nguyen"),
    A("peter.kowalski@example.org", "Peter Kowalski"),
    A("agnes.mbeki@example.org", "Agnes Mbeki"),
    A("thomas.l@example.org", "Thomas Lindqvist"),
    A("lucia.f@example.org", "Lucia Fernandez"),
    A("david.chen@example.org", "David Chen"),
    A("sarah.obrien@example.org", "Sarah O'Brien"),
    A("emmanuel.d@example.org", "Emmanuel Diallo"),
    A("anna.h@example.org", "Anna Horvath"),
    A("miguel.santos@example.org", "Miguel Santos"),
    A("vincent.okonkwo@example.org", "Vincent Okonkwo"),
    A("cecilia.moreau@example.org", "Cecilia Moreau"),
    A("yosef.tesfaye@example.org", "Yosef Tesfaye"),
    A("philomena.achebe@example.org", "Philomena Achebe"),
    A("patrick.byrne@example.org", "Patrick Byrne"),
    A("colette.amara@example.org", "Colette Amara"),
    A("estela.cruz@example.org", "Estela Cruz"),
    A("simone.beaulieu@example.org", "Simone Beaulieu"),
    A("ruth.abara@example.org", "Ruth Abara"),
    A("grace.kim@example.org", "Grace Kim"),
    A("leo.brennan@example.org", "Leo Brennan"),
    # an address change staged and not yet confirmed: nothing on file moves
    # until the link that went to the NEW address is opened
    A(
        "monica.silva@example.org",
        "Monica Silva",
        pending_email="m.silva.new@example.org",
        headline="an address change waiting on the new mailbox",
    ),
    # the three shapes an account can be in besides "has a password"
    A(
        "claire.dubois@example.org",
        "Claire Dubois",
        password=False,
        invite=True,
        headline="invite still unredeemed — the link is printed below",
    ),
    A(
        "irene.p@example.org",
        "Irene Papadopoulos",
        password=False,
        headline="no password — signs in with an emailed code",
    ),
    A(
        "george.ivanov@example.org",
        "George Ivanov",
        active=False,
        headline="deactivated account (sign-in refused)",
    ),
)


# volunteers who get a (placeholder) headshot, so roster cards, candidate cards
# and the export are not uniformly grey
PHOTOGRAPHED: tuple[str, ...] = (
    "Maria Alvarez",
    "James Okafor",
    "Rose Nguyen",
    "Peter Kowalski",
    "Agnes Mbeki",
    "Thomas Lindqvist",
    "Lucia Fernandez",
    "David Chen",
    "Sarah O'Brien",
    "Emmanuel Diallo",
    "Anna Horvath",
    "Miguel Santos",
    "Grace Kim",
    "Felix Garcia",
    "Dominic Ferraro",
    "Stephen Bianchi",
    "Joseph Tran",
    "Vincent Okonkwo",
    "Cecilia Moreau",
    "Yosef Tesfaye",
    "Philomena Achebe",
    "Patrick Byrne",
    "Colette Amara",
    "Estela Cruz",
)


# --- public pages -------------------------------------------------------------

# A published ministry page needs Team.home_doc_url *and* cached html. Dev has
# no Google Doc to fetch, so the html below is written straight into team_page
# — the same shape jobs.fetch_pages would leave behind. (Running that job
# against these fake doc ids replaces them with status='error' rows, which is
# its own demo of the failure path: the last good html is kept.)
DOC_URL = (
    "https://docs.google.com/document/d/1SeededDemoDoc{n:02d}0000000000000000/edit"
)


@dataclass(frozen=True)
class PageSpec:
    """One team's public page. `status` is the state jobs.fetch_pages left it
    in: `ok`, `error` (the fetch failed and the last good html is still
    served), or `pending` (a leader pasted the link; no fetch has run yet).
    `image` embeds a picture, cached locally the way fetch_and_store caches
    the ones a Google Doc hotlinks."""

    team: str
    blurb: str
    detail: str
    status: str = "ok"
    image: bool = False


PAGES: tuple[PageSpec, ...] = (
    PageSpec(
        "Lectors",
        "Proclaiming the readings at all four Sunday Masses.",
        "Lectors read the first and second readings and lead the prayers of "
        "the faithful. You are rostered about once a month, you get the "
        "readings a fortnight ahead, and Rose runs a short training session "
        "twice a year — no experience needed, just a willingness to prepare.",
    ),
    PageSpec(
        "Altar Servers",
        "Serving at the altar from grade 4 upwards.",
        "Servers carry the cross and candles, hold the book, and help the "
        "priest and deacon at the altar. Training runs on the first Saturday "
        "of the month; a parent is always welcome to stay and watch.",
    ),
    PageSpec(
        "Choir",
        "The 10:30 choir: four parts, one Thursday practice a week.",
        "We sing at the 10:30 Mass and at the great feasts. Practice is "
        "Thursdays at 7:30pm in the hall and runs an hour and a half. You do "
        "not need to read music — half the choir does not.",
        image=True,
    ),
    # the doc was moved or unshared after a good fetch: the page still serves
    # what it last said, and the ministries admin shows why it is stale
    PageSpec(
        "Youth Group",
        "Friday nights for grades 9 to 12.",
        "Games, a talk, small groups and food, every Friday in term time. "
        "Adult leaders are screened and trained; the parish covers the cost "
        "of the police check.",
        status="error",
    ),
    PageSpec(
        "Food Bank",
        "Saturday sorting and Tuesday hampers.",
        "The food bank serves about ninety households a month. Sorting is "
        "Saturday mornings, nine to noon, and hampers go out on Tuesday "
        "afternoons. Come once or come every week.",
    ),
    PageSpec(
        "Catholic Women's League",
        "Faith, service and social justice, since 1934.",
        "The League meets on the second Tuesday of the month after the 7pm "
        "Mass. We run the bazaar, the bereavement kitchen and the parish's "
        "letter-writing campaigns.",
    ),
    # the link is set and nothing has been fetched yet: nothing is published,
    # and the next nightly run is what makes the page appear
    PageSpec(
        "Bible Study",
        "Wednesday mornings, over coffee, one book at a time.",
        "",
        status="pending",
    ),
)


# --- helpers ------------------------------------------------------------------


@dataclass
class Parish:
    """Ids the later steps need, filled in as the earlier ones run."""

    team_ids: dict[str, int] = field(default_factory=dict)
    volunteer_ids: dict[str, int] = field(default_factory=dict)
    user_ids: dict[str, int] = field(default_factory=dict)
    # team name -> active volunteer ids, in the order they joined: the pool
    # every roster, RSVP and ballot below draws from
    rosters: dict[str, list[int]] = field(default_factory=dict)
    # email -> the plaintext invite link token, for the accounts that keep one
    invite_links: dict[str, str] = field(default_factory=dict)
    admin_id: int = 0


@dataclass
class Occurrence:
    """One event of a weekly series, with the date it is really meant to sit
    on (see _series)."""

    event: Event
    starts_at: datetime
    ends_at: datetime


def _at(day: date, hour: int, minute: int = 0) -> datetime:
    """A parish-local wall-clock time on `day`."""
    return datetime.combine(day, time(hour, minute), TZ)


def _weekday_on_or_after(day: date, weekday: int) -> date:
    return day + timedelta(days=(weekday - day.weekday()) % 7)


MONDAY, THURSDAY, FRIDAY, SATURDAY, SUNDAY = 0, 3, 4, 5, 6


def _avatar(person: Person, colour: tuple[int, int, int]) -> bytes:
    """A flat initials tile. Real headshots are not something a demo seed can
    invent, and a placeholder that is obviously a placeholder beats a stock
    photo of somebody who never consented to being in a parish database."""
    image = Image.new("RGB", (600, 600), colour)
    draw = ImageDraw.Draw(image)
    draw.text(
        (300, 300),
        f"{person.first[0]}{person.last[0]}",
        fill=(255, 255, 255),
        font=ImageFont.load_default(size=260),
        anchor="mm",
    )
    buffer = BytesIO()
    image.save(buffer, format="PNG")
    return buffer.getvalue()


def _feed_token(email: str) -> str:
    """A stable address for an account's personal .ics feed. Derived from the
    address so a reseed hands back the same URL — the whole point of a feed is
    that a subscription keeps working (services/users.ensure_calendar_token)."""
    return "demo-" + hashlib.sha256(email.encode()).hexdigest()[:32]


def _page_picture(team: str) -> bytes:
    """A banner for a published ministry page. Dev has no Google Doc to pull
    an image out of, so this stands in for one the doc would have carried."""
    image = Image.new("RGB", (1200, 400), (28, 44, 62))
    draw = ImageDraw.Draw(image)
    draw.text(
        (600, 200),
        team,
        fill=(238, 232, 220),
        font=ImageFont.load_default(size=64),
        anchor="mm",
    )
    buffer = BytesIO()
    image.save(buffer, format="PNG")
    return buffer.getvalue()


def _logo() -> bytes:
    """A stand-in parish mark for the header, the login page and the public
    shell. branding.normalize() cuts the flat ground and squares it up, which
    is the same treatment a real upload gets."""
    image = Image.new("RGB", (1000, 1000), (255, 255, 255))
    draw = ImageDraw.Draw(image)
    draw.ellipse((60, 60, 940, 940), fill=(28, 44, 62))
    draw.rectangle((470, 220, 530, 780), fill=(214, 178, 92))
    draw.rectangle((300, 390, 700, 450), fill=(214, 178, 92))
    buffer = BytesIO()
    image.save(buffer, format="PNG")
    return buffer.getvalue()


AVATAR_COLOURS = (
    (94, 84, 142),
    (36, 110, 108),
    (150, 78, 62),
    (58, 96, 140),
    (110, 106, 58),
    (128, 70, 108),
    (62, 118, 88),
    (140, 96, 48),
)


def _flatten(
    specs: tuple[TeamSpec, ...], parent: str | None = None
) -> list[tuple[TeamSpec, str | None]]:
    """Depth-first (team, parent name), so a child is always created after the
    parent whose id it needs."""
    out: list[tuple[TeamSpec, str | None]] = []
    for spec in specs:
        out.append((spec, parent))
        out.extend(_flatten(spec.children, spec.name))
    return out


def _generate(
    rng: random.Random, taken: set[str], fillable: list[str], wanted: int
) -> list[Person]:
    """The rest of the parish: ordinary members, spread over every team so no
    roster is empty, then weighted towards the ministries the schedule below
    actually staffs. Nobody generated here leads anything — the vacant seats
    are load-bearing demo data.

    `wanted` people come back however many name pairs are skipped on the way:
    the parish is a stated size (PARISH_SIZE), and a collision with a
    hand-written name must not quietly shrink it."""
    people: list[Person] = []
    index = -1
    while len(people) < wanted:
        index += 1
        # strides coprime with the pools, so a pair repeats only after
        # lcm(48, 43) = 2064 of them — far beyond any parish this seeds
        first = FIRST_NAMES[index % len(FIRST_NAMES)]
        last = LAST_NAMES[(index * 7) % len(LAST_NAMES)]
        if f"{first} {last}" in taken:
            continue
        taken.add(f"{first} {last}")
        assignments = [(fillable[index % len(fillable)], C if index % 6 == 0 else M)]
        for chance in (0.55, 0.3, 0.12):
            if rng.random() < chance:
                extra = rng.choice(POPULAR)
                if extra not in [t for t, _ in assignments]:
                    assignments.append((extra, M))
        # a fifth of the parish has no email address on file, which is what
        # makes the roster's "no email" filters and bulk_provision worth having
        email = None
        if rng.random() > 0.2:
            email = f"{first}.{last}@{EMAIL_DOMAIN}".lower()
        people.append(
            Person(
                first,
                last,
                email,
                tuple(assignments),
                active=rng.random() > 0.04,
            )
        )
    return people


async def _slots(session: AsyncSession, event_id: int) -> dict[str, EventSlot]:
    rows = await session.scalars(
        sa.select(EventSlot).where(EventSlot.event_id == event_id)
    )
    return {slot.name: slot for slot in rows}


async def _staff(
    session: AsyncSession,
    event: Event,
    wanted: list[tuple[str, int]],
    pool: list[int],
    cursor: int,
    *,
    by: int | None = None,
    signup: bool = False,
    notify_7d: bool = False,
    notify_24h: bool = True,
) -> int:
    """Fill `wanted` — [(slot name, how many)] — from `pool`, starting at
    `cursor` in the rotation, and return the next cursor.

    Nobody is asked twice at one event (uq_event_assignment), and a slot is
    left short rather than double-booked when the roster is smaller than the
    event asks for. `pool` must be the event team's own roster — participation
    is gated on membership of that exact team, sub-teams included.

    `notify_7d`/`notify_24h` are the assignment's own reminder preferences (as
    in the app: the week's notice off, the next day's on). One series below
    turns the first on and another turns the second off, so neither column is
    uniform across the demo."""
    if not pool:
        return cursor
    slots = await _slots(session, event.id)
    taken: set[int] = set()
    for name, count in wanted:
        for _ in range(count):
            if len(taken) >= len(pool):
                return cursor
            while pool[cursor % len(pool)] in taken:
                cursor += 1
            volunteer_id = pool[cursor % len(pool)]
            cursor += 1
            taken.add(volunteer_id)
            if signup:
                ok(
                    await events.sign_up(
                        session,
                        as_volunteer(volunteer_id),
                        slot_id=slots[name].id,
                        volunteer_id=volunteer_id,
                        now=NOW,
                        notify_7d=notify_7d,
                        notify_24h=notify_24h,
                    )
                )
            else:
                ok(
                    await events.assign(
                        session,
                        SYSTEM,
                        slot_id=slots[name].id,
                        volunteer_id=volunteer_id,
                        assigned_by=by,
                        now=NOW,
                    )
                )
    return cursor


async def _series(
    session: AsyncSession,
    *,
    team_id: int,
    title: str,
    weekday: int,
    hour: int,
    minute: int,
    minutes: int,
    past: int,
    future: int,
    slots: list[events.SlotInput] | None = None,
    location: str | None = None,
    description: str | None = None,
    created_by: int,
) -> list[Occurrence]:
    """A weekly series: `past` occurrences already held, `future` still to come.

    Copy-forward only runs forwards, and the roster service refuses to staff
    an event that has already ended, so the whole series is materialized in
    the future; each occurrence carries the date it really belongs on and
    _settle moves it there once its roster is filled. That is the events
    counterpart of the membership_history backdating below — demo-only, and
    not something a real deployment ever does."""
    anchor = _weekday_on_or_after(TODAY + timedelta(days=2), weekday)
    first = _at(anchor, hour, minute)
    created = ok(
        await events.create_event(
            session,
            SYSTEM,
            team_id=team_id,
            title=title,
            starts_at=first,
            ends_at=first + timedelta(minutes=minutes),
            description=description,
            location=location,
            slots=slots,
            repeat_weekly_until=anchor + timedelta(weeks=past + future - 1),
            created_by=created_by,
            tz=TZ,
            series_id=ENV.rng.uuid(),
        )
    )
    plan: list[Occurrence] = []
    for index, event in enumerate(created):
        starts_at = _at(anchor + timedelta(weeks=index - past), hour, minute)
        plan.append(
            Occurrence(event, starts_at, starts_at + timedelta(minutes=minutes))
        )
    return plan


async def _settle(session: AsyncSession, plan: list[Occurrence]) -> None:
    """Move a staffed series onto its real dates."""
    for occurrence in plan:
        ok(
            await events.update_event(
                session,
                SYSTEM,
                occurrence.event.id,
                starts_at=occurrence.starts_at,
                ends_at=occurrence.ends_at,
            )
        )


async def _one_off(
    session: AsyncSession,
    *,
    team_id: int,
    title: str,
    day: date,
    hour: int,
    minutes: int,
    slots: list[events.SlotInput] | None = None,
    location: str | None = None,
    description: str | None = None,
    created_by: int,
) -> Occurrence:
    """A single event; past ones are created ahead and settled like a series."""
    plan = await _series(
        session,
        team_id=team_id,
        title=title,
        weekday=day.weekday(),
        hour=hour,
        minute=0,
        minutes=minutes,
        past=1,
        future=0,
        slots=slots,
        location=location,
        description=description,
        created_by=created_by,
    )
    starts_at = _at(day, hour)
    occurrence = Occurrence(
        plan[0].event, starts_at, starts_at + timedelta(minutes=minutes)
    )
    return occurrence


async def _rsvps(
    session: AsyncSession,
    event: Event,
    pool: list[int],
    cursor: int,
    count: int,
    rng: random.Random,
) -> None:
    """A spread of availability answers — managers assign out of this pool."""
    excuses = (
        "Away that weekend.",
        "Can come if I'm not needed at the 9:00.",
        "Only after 11.",
        None,
        "Bringing two of the youth group with me.",
        "Back is playing up — nothing heavy, please.",
    )
    for offset in range(min(count, len(pool))):
        volunteer_id = pool[(cursor + offset) % len(pool)]
        available = rng.random() > 0.28
        ok(
            await events.set_rsvp(
                session,
                as_volunteer(volunteer_id),
                event_id=event.id,
                volunteer_id=volunteer_id,
                available=available,
                note=None if available else rng.choice(excuses),
                now=NOW,
            )
        )


async def _candidate_ids(session: AsyncSession, proposal_id: int) -> list[int]:
    return list(
        await session.scalars(
            sa.select(ProposalCandidate.id)
            .where(ProposalCandidate.proposal_id == proposal_id)
            .order_by(ProposalCandidate.id)
        )
    )


async def _first_assignment(
    session: AsyncSession, event_id: int, *, offset: int = 0
) -> EventAssignment:
    rows = list(
        await session.scalars(
            sa.select(EventAssignment)
            .where(EventAssignment.event_id == event_id)
            .order_by(EventAssignment.id)
        )
    )
    return rows[offset]


async def _cast_ballots(
    session: AsyncSession,
    proposal: Proposal,
    candidate_ids: list[int],
    rng: random.Random,
    *,
    today: date,
    turnout: float = 1.0,
) -> None:
    """Score every candidate on `turnout` of the roll. Front-runners are seeded
    by candidate order, so the STAR tally has a clear winner to show rather
    than a coin flip."""
    roll = list(
        await session.scalars(
            sa.select(ProposalVoter.volunteer_id).where(
                ProposalVoter.proposal_id == proposal.id
            )
        )
    )
    bias = {cid: 4 - index for index, cid in enumerate(candidate_ids)}
    for volunteer_id in roll:
        if rng.random() > turnout:
            continue
        scores = {
            cid: max(0, min(5, bias[cid] + rng.randint(-1, 1))) for cid in candidate_ids
        }
        ok(
            await elections.cast_ballot(
                session,
                as_volunteer(volunteer_id, proposals=[proposal.id]),
                proposal.id,
                scores=scores,
                today=today,
                now=NOW,
            )
        )


def as_volunteer(volunteer_id: int, *, proposals: Iterable[int] = ()) -> Actor:
    """The person behind `volunteer_id`, for the acts that are theirs alone: a
    sign-up, an RSVP, a claimed substitution, a ballot. No account and no team
    rights; `proposals` are the rolls they sit on."""
    return Actor(
        user=None,
        volunteer_id=volunteer_id,
        managed_team_ids=set(),
        people_team_ids=set(),
        full_view_team_ids=set(),
        names_view_team_ids=set(),
        voter_proposal_ids=frozenset(proposals),
    )


# --- the steps ----------------------------------------------------------------


async def seed_teams(session: AsyncSession, parish: Parish) -> None:
    for spec, parent in _flatten(TEAMS):
        team = ok(
            await teams.create(
                session,
                SYSTEM,
                spec.name,
                parent_team_id=parish.team_ids[parent] if parent else None,
                description=spec.description,
                workload_weight=Decimal(spec.weight) if spec.weight else None,
            )
        )
        parish.team_ids[spec.name] = team.id
        parish.rosters[spec.name] = []
        if spec.archived:
            ok(await teams.update(session, SYSTEM, team.id, is_active=False))


async def seed_people(
    session: AsyncSession, parish: Parish, people: list[Person], rng: random.Random
) -> None:
    for index, person in enumerate(people):
        volunteer = ok(
            await volunteers.create(
                session,
                SYSTEM,
                person.first,
                person.last,
                person.email,
                phone=f"555-0{100 + index:03d}" if index % 9 else None,
                notes=person.notes,
            )
        )
        parish.volunteer_ids[person.name] = volunteer.id
        if not person.active:
            ok(await volunteers.update(session, SYSTEM, volunteer.id, is_active=False))

    # historical churn BEFORE current memberships: assign is an upsert on
    # (volunteer, team), so an ended spell must be gone before the current one
    # exists (that is what makes Grace's rejoin a separate spell)
    for name, team_name, role, joined, left in PAST_SPELLS:
        spell = ok(
            await memberships.assign(
                session,
                SYSTEM,
                parish.volunteer_ids[name],
                parish.team_ids[team_name],
                role,
            )
        )
        spell_id = spell.id
        ok(await memberships.remove(session, SYSTEM, spell_id))
        # demo-only backdating of the archived interval; real deployments
        # accumulate genuine history and never touch the twin tables
        await session.execute(
            sa.text(
                "UPDATE membership_history SET sys_period = tstzrange(:joined, :left)"
                " WHERE id = :mid AND op = 'D'"
            ),
            {
                "joined": datetime(
                    joined.year, joined.month, joined.day, 9, tzinfo=UTC
                ),
                "left": datetime(left.year, left.month, left.day, 17, tzinfo=UTC),
                "mid": spell_id,
            },
        )

    for person in people:
        volunteer_id = parish.volunteer_ids[person.name]
        for team_name, role in person.teams:
            if (person.name, team_name) in PROMOTIONS and role is not M:
                # served as a plain member before taking the seat: two-colour
                # timeline bar, and an op='U' row in membership_history
                ok(
                    await memberships.assign(
                        session, SYSTEM, volunteer_id, parish.team_ids[team_name], M
                    )
                )
            ok(
                await memberships.assign(
                    session, SYSTEM, volunteer_id, parish.team_ids[team_name], role
                )
            )
            if person.active:
                parish.rosters[team_name].append(volunteer_id)

    # admin-extensible volunteer properties: one field of every type the app
    # offers (see FIELDS), filled in for enough of the parish that the list
    # columns, the volunteer form and the query language all have live values
    defs: dict[str, CustomFieldDef] = {}
    for position, spec in enumerate(FIELDS, start=1):
        defs[spec.label] = ok(
            await custom_fields.create_def(
                session,
                SYSTEM,
                spec.label,
                spec.field_type,
                options=list(spec.options) if spec.options else None,
                show_in_list=spec.show_in_list,
                position=position,
            )
        )
    for person in people:
        volunteer_id = parish.volunteer_ids[person.name]
        values = {
            defs[spec.label].key: _field_value(spec, rng)
            for spec in FIELDS
            if rng.random() < spec.fill
        }
        if values:
            ok(await custom_fields.set_values(session, SYSTEM, volunteer_id, values))
    # retired last: set_values only accepts a live field, so the values above
    # had to be written while it still was one. It keeps its column in the
    # export and its answers on the people who gave them — a field is retired,
    # never deleted, so nothing that was recorded stops being true.
    for spec in FIELDS:
        if spec.retired:
            ok(
                await custom_fields.update_def(
                    session, SYSTEM, defs[spec.label].id, is_active=False
                )
            )


async def seed_accounts(session: AsyncSession, parish: Parish) -> None:
    """Every login, all on the same password.

    users.create would refuse `demo` (passwords.check, 15 characters), so the
    hash is written on afterwards — see the module docstring. One argon2 pass
    is 100-400 ms, so the whole parish shares one: identical salts are
    meaningless on a database whose passwords are printed at the end anyway."""
    demo_hash = await async_hash_password(DEMO_PASSWORD)
    admin_hash = (
        demo_hash
        if ADMIN_PASSWORD == DEMO_PASSWORD
        else await async_hash_password(ADMIN_PASSWORD)
    )
    for account in ACCOUNTS:
        # every account is created the way the app creates one: with a link
        # armed. The ones that end up with a password spend it below, which is
        # what redeeming or setting a password does.
        user, token = ok(
            await users.create(
                session,
                account.email,
                volunteer_id=parish.volunteer_ids[account.person]
                if account.person
                else None,
                is_admin=account.is_admin,
                link_by_email=False,
                invite=ENV.invite(),
                actor=SYSTEM,
            )
        )
        if account.invite and token:
            # only the digest is stored, so this is the one moment the link
            # exists in the clear; the summary prints it
            parish.invite_links[account.email] = token
        if account.password:
            user.password_hash = admin_hash if account.is_admin else demo_hash
            # a password in hand spends the invite link users.create armed
            user.invite_token = None
            user.invite_expires_at = None
            user.confidentiality_agreed_at = NOW
        elif not account.invite:
            # neither password nor invite: an emailed one-time code is the way in
            user.invite_token = None
            user.invite_expires_at = None
            user.confidentiality_agreed_at = NOW
        user.is_active = account.active
        if account.last_login_days is not None:
            user.last_login_at = NOW - timedelta(days=account.last_login_days)
        await session.flush()
        parish.user_ids[account.email] = user.id
        # the three things an account can carry besides a way in. The tokens
        # are fixed strings rather than ENV.rng.token() so the summary below
        # can print an address that keeps working across a reseed — a demo
        # convenience, exactly like DEMO_PASSWORD, and the same localhost-only
        # bargain (the API token is stored as a digest either way).
        if account.api_token:
            ok(await users.issue_api_token(session, user.id, token=DEMO_API_TOKEN))
        if account.calendar_feed:
            ok(
                await users.ensure_calendar_token(
                    session, user.id, token=_feed_token(account.email)
                )
            )
        if account.pending_email:
            ok(
                await users.start_email_change(
                    session,
                    user.id,
                    account.pending_email,
                    now=NOW,
                    token=ENV.rng.token(),
                )
            )
    parish.admin_id = parish.user_ids[f"admin@{EMAIL_DOMAIN}"]


async def seed_photos(
    session: AsyncSession, parish: Parish, people: list[Person]
) -> None:
    by_name = {person.name: person for person in people}
    for index, name in enumerate(PHOTOGRAPHED):
        ok(
            await photos.set_photo(
                session,
                parish.volunteer_ids[name],
                _avatar(by_name[name], AVATAR_COLOURS[index % len(AVATAR_COLOURS)]),
                uploaded_by=parish.admin_id,
                now=NOW,
            )
        )


async def seed_public_pages(session: AsyncSession, parish: Parish) -> None:
    for index, spec in enumerate(PAGES, start=1):
        team_id = parish.team_ids[spec.team]
        ok(
            await pages.set_home_doc_url(
                session, SYSTEM, team_id, DOC_URL.format(n=index)
            )
        )
        if spec.status == "pending":
            # the link is set and nothing has been fetched: html NULL is what
            # keeps the page off /ministries until the nightly job runs
            session.add(TeamPage(team_id=team_id, status="pending"))
            continue
        picture = ""
        if spec.image:
            # what fetch_and_store leaves behind for a picture in the doc: the
            # bytes in team_page_image, and a src pointing at the local route
            # with the content hash as its cache buster
            data = _page_picture(spec.team)
            session.add(
                TeamPageImage(
                    team_id=team_id, seq=1, image=data, content_type="image/png"
                )
            )
            digest = hashlib.sha256(data).hexdigest()[:12]
            picture = (
                f'<p><img src="/ministries/img/{team_id}/1?v={digest}" '
                f'alt="{spec.team}"></p>'
            )
        html = pages.sanitize_doc_html(
            f"<h1>{spec.team}</h1><p><em>{spec.blurb}</em></p>"
            f"{picture}"
            f"<p>{spec.detail}</p>"
            "<h2>Getting in touch</h2><p>New volunteers are always welcome. "
            "Speak to one of the ministry's leaders after Mass, or call the "
            "parish office. There is no commitment in asking.</p>"
        )
        session.add(
            TeamPage(
                team_id=team_id,
                html=html,
                # a failed fetch keeps the html and the time it last succeeded
                fetched_at=NOW - timedelta(days=9 if spec.status == "error" else 1),
                status=spec.status,
                error=(
                    "404 Not Found — the document is no longer shared publicly"
                    if spec.status == "error"
                    else None
                ),
            )
        )
    await session.flush()


async def seed_settings(session: AsyncSession, parish: Parish) -> None:
    """What the parish set for itself: its own mark, and its own bands."""
    ok(await branding.set_logo(session, SYSTEM, _logo(), now=NOW))
    ok(await workload.set_config(session, SYSTEM, WORKLOAD_CONFIG, now=NOW))


async def seed_schedule(
    session: AsyncSession, parish: Parish, rng: random.Random
) -> None:
    admin = parish.admin_id
    slot = events.SlotInput

    # --- the Sunday liturgical rosters ---
    lectors = await _series(
        session,
        team_id=parish.team_ids["Lectors"],
        title="Sunday 10:30 Mass",
        weekday=SUNDAY,
        hour=10,
        minute=30,
        minutes=75,
        past=8,
        future=4,
        slots=[
            slot("First reading", 1, 1),
            slot("Second reading", 1, 2),
            slot("Prayers of the faithful", 1, 3),
        ],
        location="Church",
        description="Readings for the Sunday; texts go out a fortnight ahead.",
        created_by=admin,
    )
    cursor = 0
    for occurrence in lectors:
        cursor = await _staff(
            session,
            occurrence.event,
            [
                ("First reading", 1),
                ("Second reading", 1),
                ("Prayers of the faithful", 1),
            ],
            parish.rosters["Lectors"],
            cursor,
            by=admin,
        )
    await _settle(session, lectors)

    servers = await _series(
        session,
        team_id=parish.team_ids["Altar Servers"],
        title="Sunday 10:30 Mass — servers",
        weekday=SUNDAY,
        hour=10,
        minute=15,
        minutes=90,
        past=6,
        future=4,
        slots=[
            slot("Cross bearer", 1, 1),
            slot("Candle bearer", 2, 2),
            slot("Thurifer", 1, 3),
        ],
        location="Church",
        description="Arrive at 10:00 to vest.",
        created_by=admin,
    )
    cursor = 0
    for occurrence in servers:
        cursor = await _staff(
            session,
            occurrence.event,
            [("Cross bearer", 1), ("Candle bearer", 2), ("Thurifer", 1)],
            parish.rosters["Altar Servers"],
            cursor,
            by=admin,
        )
    await _settle(session, servers)

    # --- the weekly gatherings people sign themselves up for ---
    choir = await _series(
        session,
        team_id=parish.team_ids["Choir"],
        title="Choir practice",
        weekday=THURSDAY,
        hour=19,
        minute=30,
        minutes=90,
        past=5,
        future=4,
        slots=[slot("Singers")],  # unlimited
        location="Parish hall",
        created_by=admin,
    )
    cursor = 0
    for occurrence in choir:
        cursor = await _staff(
            session,
            occurrence.event,
            [("Singers", 7)],
            parish.rosters["Choir"],
            cursor,
            signup=True,
            # every Thursday, same time, same hall: the choir turned the
            # next-day reminder off when they signed up
            notify_24h=False,
        )
    await _settle(session, choir)

    youth = await _series(
        session,
        team_id=parish.team_ids["Youth Group"],
        title="Youth night",
        weekday=FRIDAY,
        hour=19,
        minute=0,
        minutes=120,
        past=4,
        future=3,
        slots=[slot("Leader", 2, 1), slot("Helper", 3, 2), slot("Kitchen", 2, 3)],
        location="Youth room",
        description="Games, a talk, small groups and food.",
        created_by=admin,
    )
    cursor = 0
    for occurrence in youth:
        cursor = await _staff(
            session,
            occurrence.event,
            [("Leader", 2), ("Helper", 3), ("Kitchen", 2)],
            parish.rosters["Youth Group"],
            cursor,
            by=admin,
        )
    await _settle(session, youth)

    food = await _series(
        session,
        team_id=parish.team_ids["Food Bank"],
        title="Food bank sorting",
        weekday=SATURDAY,
        hour=9,
        minute=0,
        minutes=180,
        past=3,
        future=3,
        slots=[slot("Sorters", 6)],
        location="Basement, door off the car park",
        created_by=admin,
    )
    cursor = 0
    for occurrence in food:
        cursor = await _staff(
            session,
            occurrence.event,
            [("Sorters", 5)],
            parish.rosters["Food Bank"],
            cursor,
            signup=True,
            # the sorters asked for the seven-day reminder as well as the
            # next-day one, which is off by default everywhere else
            notify_7d=True,
        )
    await _settle(session, food)

    coffee = await _series(
        session,
        team_id=parish.team_ids["Coffee Sunday"],
        title="Coffee after the 10:30",
        weekday=SUNDAY,
        hour=11,
        minute=45,
        minutes=75,
        past=4,
        future=3,
        slots=[slot("Servers", 4, 1), slot("Setup & cleanup", 2, 2)],
        location="Parish hall",
        created_by=admin,
    )
    cursor = 0
    for occurrence in coffee:
        cursor = await _staff(
            session,
            occurrence.event,
            [("Servers", 3), ("Setup & cleanup", 2)],
            parish.rosters["Coffee Sunday"],
            cursor,
            signup=True,
        )
    await _settle(session, coffee)

    # --- one-offs, past and future ---
    work_day = await _one_off(
        session,
        team_id=parish.team_ids["Maintenance"],
        title="Spring work day",
        day=TODAY - timedelta(days=23),
        hour=9,
        minutes=360,
        location="Grounds",
        description="Beds, gutters, the shed. Bring gloves; lunch provided.",
        created_by=admin,
    )
    await _staff(
        session,
        work_day.event,
        [("Volunteers", 9)],
        parish.rosters["Maintenance"],
        0,
        signup=True,
    )
    await _settle(session, [work_day])

    cleaning = await _one_off(
        session,
        team_id=parish.team_ids["Altar Society"],
        title="Church cleaning before the feast",
        day=TODAY - timedelta(days=9),
        hour=10,
        minutes=180,
        location="Church",
        created_by=admin,
    )
    await _staff(
        session,
        cleaning.event,
        [("Volunteers", 6)],
        parish.rosters["Altar Society"],
        0,
        signup=True,
    )
    await _settle(session, [cleaning])

    picnic_day = _weekday_on_or_after(TODAY + timedelta(days=30), SATURDAY)
    picnic = ok(
        await events.create_event(
            session,
            SYSTEM,
            team_id=parish.team_ids["Parish Picnic Task Force"],
            title="Parish picnic",
            starts_at=_at(picnic_day, 11),
            ends_at=_at(picnic_day, 16),
            description="The whole parish, the whole afternoon. Rain or shine.",
            location="Memorial park",
            slots=[
                slot("Setup", 5, 1, "Trestles and the marquee, from 9:00."),
                slot("BBQ", 4, 2, "Two grills; the Knights bring the propane."),
                slot("Games & crafts", 6, 3),
                slot("First aid", 2, 4, "St. John Ambulance ticket, please."),
                slot("Cleanup", 5, 5),
            ],
            created_by=admin,
            tz=TZ,
        )
    )[0]
    # Three ministries staff the picnic between them, so it gets a task force:
    # an auto-created child team holding the union of their rosters, which the
    # event is repointed onto until it is over (services/task_force.py). That
    # is what makes sign-up, mail and management work for all three at once —
    # and what the nightly teardown will undo the day after the picnic.
    for source in ("Knights of Columbus", "Catholic Women's League", "Hospitality"):
        meta_team = ok(
            await task_force.add_collaborating_team(
                session,
                SYSTEM,
                event_id=picnic.id,
                source_team_id=parish.team_ids[source],
                created_by=admin,
                now=NOW,
                tz=TZ,
            )
        )
    # the pool is now the meta team's roster, not the owner's: participation is
    # gated on membership of the team the event currently sits on
    picnic_pool = list(
        await session.scalars(
            sa.select(Membership.volunteer_id)
            .join(Volunteer, Volunteer.id == Membership.volunteer_id)
            .where(Membership.team_id == meta_team.id, Volunteer.is_active)
            .order_by(Membership.id)
        )
    )
    await _staff(
        session,
        picnic,
        [
            ("Setup", 3),
            ("BBQ", 3),
            ("Games & crafts", 4),
            ("First aid", 1),
            ("Cleanup", 3),
        ],
        picnic_pool,
        0,
        by=admin,
    )
    await _rsvps(session, picnic, picnic_pool, 4, 16, rng)

    funeral_day = _weekday_on_or_after(TODAY + timedelta(days=6), MONDAY)
    reception = ok(
        await events.create_event(
            session,
            SYSTEM,
            team_id=parish.team_ids["Bereavement Ministry"],
            title="Funeral reception — the Delgado family",
            starts_at=_at(funeral_day, 11),
            ends_at=_at(funeral_day, 14),
            description="Sandwiches and tea in the hall after the 10:00 funeral.",
            location="Parish hall",
            slots=[slot("Kitchen", 3, 1), slot("Serving", 4, 2)],
            created_by=admin,
            tz=TZ,
        )
    )[0]
    await _staff(
        session,
        reception,
        [("Kitchen", 2), ("Serving", 3)],
        parish.rosters["Bereavement Ministry"],
        0,
        by=admin,
    )

    bazaar_day = _weekday_on_or_after(TODAY + timedelta(days=11), MONDAY) + timedelta(
        days=1
    )
    bazaar = ok(
        await events.create_event(
            session,
            SYSTEM,
            team_id=parish.team_ids["Catholic Women's League"],
            title="Christmas bazaar — planning meeting",
            starts_at=_at(bazaar_day, 19),
            ends_at=_at(bazaar_day, 20),
            location="Meeting room 2",
            created_by=admin,
            tz=TZ,
        )
    )[0]
    await _staff(
        session,
        bazaar,
        [("Volunteers", 5)],
        parish.rosters["Catholic Women's League"],
        0,
        signup=True,
    )
    await _rsvps(session, bazaar, parish.rosters["Catholic Women's League"], 5, 6, rng)

    # --- RSVPs on the future weekly events ---
    for occurrence in youth[-3:]:
        await _rsvps(
            session, occurrence.event, parish.rosters["Youth Group"], 8, 7, rng
        )
    for occurrence in food[-2:]:
        await _rsvps(session, occurrence.event, parish.rosters["Food Bank"], 6, 6, rng)

    # --- substitutions: two open calls, one claimed, one withdrawn ---
    next_mass = lectors[-4].event
    open_call = await _first_assignment(session, next_mass.id)
    ok(
        await events.request_sub(
            session,
            SYSTEM,
            assignment_id=open_call.id,
            requested_by=parish.user_ids["maria.alvarez@example.org"],
            note="Away at a wedding — sorry for the short notice.",
            now=NOW,
        )
    )
    next_servers = servers[-3].event
    ok(
        await events.request_sub(
            session,
            SYSTEM,
            assignment_id=(await _first_assignment(session, next_servers.id)).id,
            requested_by=parish.user_ids["peter.kowalski@example.org"],
            note="Exam that morning.",
            now=NOW,
        )
    )
    next_youth = youth[-2].event
    claimed = ok(
        await events.request_sub(
            session,
            SYSTEM,
            assignment_id=(await _first_assignment(session, next_youth.id)).id,
            requested_by=parish.user_ids["emmanuel.d@example.org"],
            note="Down with the flu.",
            now=NOW,
        )
    )
    spare = [
        volunteer_id
        for volunteer_id in parish.rosters["Youth Group"]
        if volunteer_id
        not in await events.assigned_volunteer_ids(session, next_youth.id)
    ]
    if spare:
        ok(
            await events.claim_sub(
                session,
                as_volunteer(spare[0]),
                sub_request_id=claimed.id,
                volunteer_id=spare[0],
                now=NOW,
            )
        )
    withdrawn = ok(
        await events.request_sub(
            session,
            SYSTEM,
            assignment_id=(
                await _first_assignment(session, next_youth.id, offset=1)
            ).id,
            requested_by=parish.user_ids["emmanuel.d@example.org"],
            note="Might have a clash — will confirm.",
            now=NOW,
        )
    )
    ok(await events.cancel_sub(session, SYSTEM, withdrawn.id, now=NOW))

    # --- the other two ways a future slot changes hands ---
    # a manager swaps somebody out directly (no call for a substitute went out)
    reception_spare = [
        volunteer_id
        for volunteer_id in parish.rosters["Bereavement Ministry"]
        if volunteer_id
        not in await events.assigned_volunteer_ids(session, reception.id)
    ]
    if reception_spare:
        ok(
            await events.substitute(
                session,
                SYSTEM,
                assignment_id=(await _first_assignment(session, reception.id)).id,
                new_volunteer_id=reception_spare[0],
                acted_by=admin,
                # digest, not direct: the seed performs no effects, so the
                # incoming person must be left for the nightly digest to tell
                # rather than stamped as already written to
                notify=NotifyMode.digest,
                now=NOW,
            )
        )
    # and somebody takes themselves off a slot, saying why (their team's
    # leaders are the ones the SelfRemoved event would reach)
    ok(
        await events.remove_assignment(
            session,
            SYSTEM,
            (await _first_assignment(session, coffee[-1].event.id)).id,
            now=NOW,
            reason="Away at my daughter's graduation — sorry.",
        )
    )

    # --- one sign-up copied onto every later week of its series ---
    choir_slots = await _slots(session, choir[-3].event.id)
    choir_spare = [
        volunteer_id
        for volunteer_id in parish.rosters["Choir"]
        if volunteer_id
        not in await events.assigned_volunteer_ids(session, choir[-3].event.id)
    ]
    if choir_spare:
        ok(
            await events.sign_up_series(
                session,
                as_volunteer(choir_spare[0]),
                slot_id=choir_slots["Singers"].id,
                volunteer_id=choir_spare[0],
                now=NOW,
            )
        )

    # --- a cancelled event, its roster still attached ---
    ok(
        await events.cancel_event(
            session, SYSTEM, choir[-1].event.id, cancelled_by=admin, now=NOW
        )
    )

    # --- manager exceptions to the derived attendance on past events ---
    last_mass = lectors[7].event
    ok(
        await events.set_attendance(
            session,
            SYSTEM,
            assignment_id=(await _first_assignment(session, last_mass.id)).id,
            attended=False,
            hours=None,
            now=NOW,
        )
    )
    ok(
        await events.set_attendance(
            session,
            SYSTEM,
            assignment_id=(await _first_assignment(session, work_day.event.id)).id,
            attended=True,
            hours=Decimal("8.50"),
            now=NOW,
        )
    )
    ok(
        await events.set_attendance(
            session,
            SYSTEM,
            assignment_id=(
                await _first_assignment(session, cleaning.event.id, offset=1)
            ).id,
            attended=False,
            hours=None,
            now=NOW,
        )
    )


async def seed_elections(
    session: AsyncSession, parish: Parish, rng: random.Random
) -> None:
    """One proposal in every state the elections module can be in."""
    admin = parish.admin_id
    candidate = elections.CandidateInput

    def person(name: str) -> int:
        return parish.volunteer_ids[name]

    # 1. nominating — the Hospitality vacancy the coverage report leads with
    nominating = ok(
        await elections.create_proposal(
            session,
            SYSTEM,
            team_id=parish.team_ids["Hospitality"],
            role=L,
            nomination_deadline=TODAY + timedelta(days=5),
            voting_deadline=TODAY + timedelta(days=12),
            created_by=admin,
            notes="Hospitality has run without a leader since Advent.",
            candidates=[
                candidate(
                    person("Monica Silva"), "Has quietly run coffee Sunday all year."
                ),
                candidate(person("Teresa Romano"), "Knows every family in the parish."),
                candidate(person("Estela Cruz"), "Already leads Coffee Sunday."),
            ],
            today=TODAY,
        )
    )
    # both of these are open only while nominating: a name put forward after
    # the proposal went up, and somebody added to the roll who was not on the
    # template (leader + second + core, plus the clergy). The roll freezes the
    # day voting opens, which is why proposals 2-6 below have neither.
    ok(
        await elections.add_candidate(
            session,
            SYSTEM,
            nominating.id,
            volunteer_id=person("Deirdre Walsh"),
            nominated_by=admin,
            note="Put forward from the floor of the pastoral council.",
            today=TODAY,
        )
    )
    ok(
        await elections.add_voter(
            session,
            SYSTEM,
            nominating.id,
            volunteer_id=person("Bernard Quinn"),
            added_by=admin,
            today=TODAY,
        )
    )

    # 2. voting, ballots half in — the tally stays hidden until the deadline
    voting = ok(
        await elections.create_proposal(
            session,
            SYSTEM,
            team_id=parish.team_ids["Prayer Chain"],
            role=L,
            nomination_deadline=TODAY - timedelta(days=3),
            voting_deadline=TODAY + timedelta(days=4),
            created_by=admin,
            today=TODAY - timedelta(days=10),
            candidates=[
                candidate(person("Ignatius Mensah"), "Keeps the chain going by phone."),
                candidate(person("Dolores Vasquez"), "Runs the email half of it."),
                candidate(person("Philomena Achebe"), "Nominated by the League."),
            ],
        )
    )
    await _cast_ballots(
        session,
        voting,
        await _candidate_ids(session, voting.id),
        rng,
        today=TODAY,
        turnout=0.55,
    )

    # 3. concluded, awaiting a decision — tally visible, appoint button live
    concluded = ok(
        await elections.create_proposal(
            session,
            SYSTEM,
            team_id=parish.team_ids["Ushers"],
            role=S,
            nomination_deadline=TODAY - timedelta(days=20),
            voting_deadline=TODAY - timedelta(days=6),
            created_by=admin,
            today=TODAY - timedelta(days=30),
            notes="Vincent has asked for a second since the collection count moved.",
            candidates=[
                candidate(person("Marta Kaminski"), "Ushers every second Sunday."),
                candidate(person("Patrick Byrne"), "Would double up with the Knights."),
            ],
        )
    )
    await _cast_ballots(
        session,
        concluded,
        await _candidate_ids(session, concluded.id),
        rng,
        today=TODAY - timedelta(days=10),
    )

    # 4. appointed — and the membership the appointment created
    decided = ok(
        await elections.create_proposal(
            session,
            SYSTEM,
            team_id=parish.team_ids["Bereavement Ministry"],
            role=L,
            nomination_deadline=TODAY - timedelta(days=50),
            voting_deadline=TODAY - timedelta(days=40),
            created_by=admin,
            today=TODAY - timedelta(days=60),
            candidates=[
                candidate(person("Bernadette Osei"), "Has cooked for every reception."),
                candidate(
                    person("Camille Rousseau"), "Trained in bereavement support."
                ),
                candidate(person("Dolores Vasquez"), "Would rather stay on the chain."),
            ],
        )
    )
    decided_candidates = await _candidate_ids(session, decided.id)
    await _cast_ballots(
        session,
        decided,
        decided_candidates,
        rng,
        today=TODAY - timedelta(days=45),
    )
    result = await elections._tally(session, decided.id)
    ok(
        await elections.appoint(
            session,
            SYSTEM,
            decided.id,
            result.winner_id or decided_candidates[0],
            decided_by=admin,
            today=TODAY,
            now=NOW,
        )
    )

    # 5. cancelled — the seat was filled another way
    cancelled = ok(
        await elections.create_proposal(
            session,
            SYSTEM,
            team_id=parish.team_ids["Website & Socials"],
            role=L,
            nomination_deadline=TODAY - timedelta(days=30),
            voting_deadline=TODAY - timedelta(days=20),
            created_by=admin,
            today=TODAY - timedelta(days=40),
            notes="Withdrawn: the diocese is consolidating parish websites.",
            candidates=[
                candidate(person("Kevin Tran"), "Built the current site."),
                candidate(person("Gregory Nakamura"), "Runs the socials already."),
            ],
        )
    )
    ok(await elections.cancel(session, SYSTEM, cancelled.id, decided_by=admin, now=NOW))

    # 6. concluded then re-opened: the Ignatian "debate together, then repeat"
    first_round = ok(
        await elections.create_proposal(
            session,
            SYSTEM,
            team_id=parish.team_ids["Children's Liturgy"],
            role=L,
            nomination_deadline=TODAY - timedelta(days=35),
            voting_deadline=TODAY - timedelta(days=25),
            created_by=admin,
            today=TODAY - timedelta(days=45),
            notes="Round one: nobody clearly ahead, so the council asked for another.",
            candidates=[
                candidate(person("Bridget Hayes"), "Leads the Word most Sundays."),
                candidate(person("Rafael Ortega"), "Would bring the youth group in."),
                candidate(person("Anita Bakker"), "Catechist for the same age group."),
            ],
        )
    )
    await _cast_ballots(
        session,
        first_round,
        await _candidate_ids(session, first_round.id),
        rng,
        today=TODAY - timedelta(days=30),
    )
    ok(
        await elections.new_round(
            session,
            SYSTEM,
            first_round.id,
            created_by=admin,
            nomination_deadline=TODAY + timedelta(days=7),
            voting_deadline=TODAY + timedelta(days=21),
            today=TODAY,
            now=NOW,
        )
    )


# --- what the machinery records about itself ----------------------------------

# The team's roster spreadsheet on Drive, and how its last sync went
# (jobs/roster_sync.py). The file ids are invented: dev has no Drive, and a
# sheet nobody can open is still enough for the leader's panel, the admin
# listing and the four SyncStatus values to have something to show.
SHEETS: tuple[tuple[str, str, SyncStatus | None, int | None, str | None], ...] = (
    ("Lectors", "1SeededDemoSheetLectors0000000000000", SyncStatus.applied, 0, None),
    (
        "Altar Servers",
        "1SeededDemoSheetServers000000000000",
        SyncStatus.unchanged,
        0,
        None,
    ),
    ("Choir", "1SeededDemoSheetChoir00000000000000", SyncStatus.new, 1, None),
    (
        "Food Bank",
        "1SeededDemoSheetFoodBank000000000000",
        SyncStatus.error,
        4,
        "the sheet has a row whose Team column names a ministry it does not manage",
    ),
    # linked and never synced: the leader pasted the link this morning
    ("Youth Group", "1SeededDemoSheetYouth00000000000000", None, None, None),
)


async def seed_bookkeeping(session: AsyncSession, parish: Parish) -> None:
    """The rows nothing in the parish wrote: notices already sent, the nightly
    jobs' last good run, the mail allowance spent, and the roster spreadsheets
    the teams are linked to. All four are written straight in — they are the
    app's own bookkeeping, and their real authors are jobs a dev machine has
    no Google or SMTP to run."""
    # --- notices already sent -------------------------------------------------
    # Every stage, on the people a nightly digest would otherwise write to the
    # first time it ran here. A stamp is keyed by (subject, recipient, stage),
    # so this is exactly what the jobs would have left behind — and why a
    # `make dev` does not open with a burst of mail about a fictional parish.
    stamps: list[dict] = []
    past = await session.execute(
        sa.select(EventAssignment.id, EventAssignment.volunteer_id, Event.starts_at)
        .join(Event, Event.id == EventAssignment.event_id)
        .where(Event.ends_at < NOW)
    )
    for assignment_id, volunteer_id, starts_at in past:
        for stage, before in (
            (NotificationStage.event_scheduled, timedelta(days=21)),
            (NotificationStage.event_week, timedelta(days=7)),
            (NotificationStage.event_day, timedelta(days=1)),
        ):
            stamps.append(
                {
                    "assignment_id": assignment_id,
                    "volunteer_id": volunteer_id,
                    "stage": stage,
                    "sent_at": starts_at - before,
                }
            )
    if stamps:
        await session.execute(
            pg_insert(Notification)
            .values(stamps)
            .on_conflict_do_nothing(constraint="uq_notification_assignment")
        )
    # everybody on a roll has been told they are on it; the rolls whose voting
    # has opened have had the second notice too, and the two proposals still
    # taking nominations have not
    voters = await session.execute(
        sa.select(
            ProposalVoter.id,
            ProposalVoter.volunteer_id,
            Proposal.created_at,
            Proposal.nomination_deadline,
        ).join(Proposal, Proposal.id == ProposalVoter.proposal_id)
    )
    voter_stamps: list[dict] = []
    for voter_id, volunteer_id, created_at, nomination_deadline in voters:
        voter_stamps.append(
            {
                "voter_id": voter_id,
                "volunteer_id": volunteer_id,
                "stage": NotificationStage.roll_added,
                "sent_at": created_at,
            }
        )
        if nomination_deadline < TODAY:
            voter_stamps.append(
                {
                    "voter_id": voter_id,
                    "volunteer_id": volunteer_id,
                    "stage": NotificationStage.voting_open,
                    # the digest that morning, which is when voting opened
                    "sent_at": _at(nomination_deadline + timedelta(days=1), 4),
                }
            )
    if voter_stamps:
        await session.execute(
            pg_insert(Notification)
            .values(voter_stamps)
            .on_conflict_do_nothing(constraint="uq_notification_voter")
        )

    # --- the nightly jobs ------------------------------------------------------
    # Every job has already run today, so starting the app does not set the
    # whole chain going against a Google account this machine does not have.
    # Delete a row (or set last_success_on back a day) to watch one run.
    for job in scheduler.JOBS:
        failed = job.name == "proposal_digest"
        session.add(
            JobRun(
                job_name=job.name,
                # one job is mid-retry: it failed at 03:30 and the scheduler
                # will try it again, which is what last_exit_code is there for
                last_success_on=TODAY - timedelta(days=1) if failed else TODAY,
                last_attempt_at=NOW - timedelta(hours=4),
                last_exit_code=1 if failed else 0,
            )
        )

    # --- the mail allowance ----------------------------------------------------
    # A fortnight of parish days with a weekly shape to them — Sunday, when the
    # rosters go out, is the heavy one. Deliberately well under both caps: the
    # gauge should read quiet on a fresh demo, and an admin who wants to see the
    # warning banner can raise these figures (services/mail_quota.py).
    for back in range(14):
        day = TODAY - timedelta(days=back)
        weekday = day.weekday()
        session.add(MailQuota(day=day, sent=34 if weekday == 6 else 9 + (weekday * 2)))

    # --- the roster spreadsheets ----------------------------------------------
    for team_name, file_id, status, days_ago, error in SHEETS:
        session.add(
            TeamSheet(
                team_id=parish.team_ids[team_name],
                file_id=file_id,
                file_name=f"{team_name}{roster_sheets.SHEET_SUFFIX}",
                last_synced_at=(
                    None if days_ago is None else NOW - timedelta(days=days_ago)
                ),
                last_status=status,
                last_error=error,
            )
        )
    await session.flush()


# --- summary ------------------------------------------------------------------


COUNTED: tuple[tuple[str, type], ...] = (
    ("teams", Team),
    ("volunteers", Volunteer),
    ("memberships", Membership),
    ("custom fields", CustomFieldDef),
    ("photos", VolunteerPhoto),
    ("accounts", AppUser),
    ("events", Event),
    ("event slots", EventSlot),
    ("assignments", EventAssignment),
    ("RSVPs", EventRsvp),
    ("substitution requests", EventSubRequest),
    ("task-force sources", EventTaskForceSource),
    ("proposals", Proposal),
    ("ballots", ProposalBallot),
    ("published pages", TeamPage),
    ("page images", TeamPageImage),
    ("roster sheets", TeamSheet),
    ("notices sent", Notification),
    ("site logo", SiteLogo),
    ("settings", AppSetting),
    ("job runs", JobRun),
    ("mail-quota days", MailQuota),
)


async def _summarize(session: AsyncSession) -> list[tuple[str, int]]:
    out: list[tuple[str, int]] = []
    for label, model in COUNTED:
        count = await session.scalar(sa.select(sa.func.count()).select_from(model))
        out.append((label, count or 0))
    return out


# --- entry point --------------------------------------------------------------


async def seed() -> None:
    rng = random.Random(RNG_SEED)
    people = list(CURATED)
    fillable = [
        spec.name for spec, _ in _flatten(TEAMS) if spec.name not in NEVER_GENERATED
    ]
    people += _generate(
        rng,
        {person.name for person in people},
        fillable,
        PARISH_SIZE - len(people),
    )

    async with db_session() as session:
        if (
            await session.execute(sa.select(sa.func.count()).select_from(Volunteer))
        ).scalar():
            sys.exit("Database already contains volunteers; refusing to seed.")

        parish = Parish()

        async def step(name: str, work: Awaitable[None]) -> None:
            """One step, announced as it begins: a parish this size takes long
            enough that a silent run looks like a hung one."""
            print(f"  … {name}", flush=True)
            await work

        await step("teams", seed_teams(session, parish))
        await step("people", seed_people(session, parish, people, rng))
        await step("accounts", seed_accounts(session, parish))
        await step("photos", seed_photos(session, parish, people))
        await step("settings", seed_settings(session, parish))
        await step("public pages", seed_public_pages(session, parish))
        await step("schedule", seed_schedule(session, parish, rng))
        await step("elections", seed_elections(session, parish, rng))
        await step("bookkeeping", seed_bookkeeping(session, parish))
        counts = await _summarize(session)
    await ENV.engine.dispose()

    print("\nSeeded a demo parish:")
    for label, count in counts:
        print(f"  {count:>5}  {label}")
    print("\nShapes worth opening first:")
    print("  Elections → Vacancies: Hospitality, Prayer Chain, Children's Liturgy…")
    print("  Elections → Proposals: one in every state (nominating → appointed)")
    print("  Events: rosters either side of today, two open substitution calls")
    print("  Events → Parish picnic: three ministries, one task force")
    print("  Maria Alvarez: two leaderships, the top workload band, an ended spell")
    print("  Admin → Fields: one field of every type, and one retired")
    print("  Admin → Workload: four bands, none of them the shipped default")
    print("  /ministries/: six pages listed, one of them stale; a seventh unfetched")
    print(f"\nEvery login below uses the password: {DEMO_PASSWORD}")
    for account in ACCOUNTS:
        if account.headline:
            password = ADMIN_PASSWORD if account.is_admin else DEMO_PASSWORD
            shown = password if account.password else "(no password)"
            print(f"  {account.email:<32} {shown:<14} {account.headline}")
    print(
        f"  …and {sum(1 for a in ACCOUNTS if not a.headline)} more accounts, "
        f"one per ministry leader, all on {DEMO_PASSWORD}."
    )
    print("\nAnd the addresses that are not sign-in pages:")
    print(f"  JSON API   Authorization: Bearer {DEMO_API_TOKEN}")
    for account in ACCOUNTS:
        if account.calendar_feed:
            print(
                f"  calendar   /calendar/mine/{_feed_token(account.email)}.ics"
                f"  ({account.person})"
            )
    # the invite link exists in the clear for exactly as long as this run: only
    # its digest is stored, so an unprinted one could never be shown again
    for email, token in parish.invite_links.items():
        print(f"  invite     /invite/{token}  ({email})")


if __name__ == "__main__":
    from volunteerdb.log import init_logging

    init_logging()
    asyncio.run(seed())
