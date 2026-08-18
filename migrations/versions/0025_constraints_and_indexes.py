"""Make the database hold the invariants the code documented, and index what the
queries actually ask for.

Revision ID: 0025
Revises: 0024

Nothing here changes what the application does. Every constraint was already
true — maintained by a service, asserted in a comment — and every index either
had no consumer or was missing for a query that runs unattended every night.

**Two comments become constraints.** `proposal_ballot.proposal_id` and
`event_assignment.event_id` are denormalized so a tally and a per-event read are
one query each, and both carried a note saying "the service guarantees" the
denormalized value agrees with the row it duplicates. Composite foreign keys
say it instead: a ballot's voter and candidate must belong to the proposal the
ballot claims, and an assignment's slot must belong to its event. Without them a
bug could have counted a ballot in the wrong proposal's tally, or seated somebody
in slot A of event B while `uq_event_assignment` guarded the wrong pairing.

Three of these are deliberately **one-directional**, because the column on the
other side is `ON DELETE SET NULL`: deleting the admin who resolved an interest
submission, the volunteer who claimed a slot, or the candidate who was appointed
must not make the historical row unstorable. Those rows lose the attribution and
keep the fact, which is the whole point of `SET NULL`.

**Set-and-clear-together groups.** `app_user` had no CHECK at all despite three
of them (the invite pair, the email-change triple, the OTP pair), so a token
with no expiry — which every reader treats as dead — was storable. Same for
`interest.resolved_at`/`resolved_by`, `event.cancelled_at`, and
`event_sub_request`'s resolution and claimant.

**Indexes dropped**, each with no possible consumer:
`ix_membership_volunteer_id` (a strict prefix of the (volunteer_id, team_id)
unique), `ix_interest_team_id` and `ix_event_sub_request_assignment_id` (both
covered by the partial uniques whose predicates every reader also filters on),
`ix_volunteer_email` (every lookup folds case, so a raw btree was unusable —
replaced by a functional index on `lower(email)`), and the two rev-0005 pg_trgm
GIN indexes, which `services/volunteers.search` documents as unreachable since
the access-control rewrite put them behind an OR.

**Indexes added** for predicates that had none: `event.ends_at` (five call
sites), a partial one on `event.google_event_id` (the calendar sync ORs the two
every thirty minutes), a partial one on the assignments still owed a reminder
(no plain index can serve that OR), `team.name` (the clergy roll is built by
name), `proposal.created_at` (the ORDER BY of both list queries), and the five
FK columns that sit on real delete paths and had to seq-scan.
"""

import sqlalchemy as sa
from alembic import op

revision = "0025"
down_revision = "0024"
branch_labels = None
depends_on = None

DROPPED_INDEXES = [
    # (name, table, recreate-DDL for downgrade)
    (
        "ix_membership_volunteer_id",
        "membership",
        "CREATE INDEX ix_membership_volunteer_id ON membership (volunteer_id)",
    ),
    (
        "ix_interest_team_id",
        "interest",
        "CREATE INDEX ix_interest_team_id ON interest (team_id)",
    ),
    (
        "ix_event_sub_request_assignment_id",
        "event_sub_request",
        "CREATE INDEX ix_event_sub_request_assignment_id "
        "ON event_sub_request (assignment_id)",
    ),
    (
        "ix_volunteer_email",
        "volunteer",
        "CREATE INDEX ix_volunteer_email ON volunteer (email)",
    ),
    (
        "ix_volunteer_email_trgm",
        "volunteer",
        "CREATE INDEX ix_volunteer_email_trgm ON volunteer "
        "USING gin (email gin_trgm_ops)",
    ),
    (
        "ix_volunteer_full_name_trgm",
        "volunteer",
        "CREATE INDEX ix_volunteer_full_name_trgm ON volunteer "
        "USING gin ((first_name || ' ' || last_name) gin_trgm_ops)",
    ),
]

ADDED_INDEXES = [
    "CREATE INDEX ix_volunteer_email_lower ON volunteer (lower(email))",
    "CREATE INDEX ix_team_name ON team (name)",
    "CREATE INDEX ix_proposal_created_at ON proposal (created_at)",
    "CREATE INDEX ix_proposal_appointed_candidate ON proposal (appointed_candidate_id)",
    "CREATE INDEX ix_proposal_candidate_volunteer_id "
    "ON proposal_candidate (volunteer_id)",
    "CREATE INDEX ix_event_ends_at ON event (ends_at)",
    "CREATE INDEX ix_event_google_id ON event (google_event_id) "
    "WHERE google_event_id IS NOT NULL",
    "CREATE INDEX ix_event_task_force_owner ON event_task_force (owner_team_id)",
    "CREATE INDEX ix_event_sub_request_claimant "
    "ON event_sub_request (claimed_by_volunteer_id)",
    "CREATE INDEX ix_event_assignment_owed_notice ON event_assignment (event_id) "
    "WHERE assigned_notified_at IS NULL "
    "OR (notify_7d AND reminded_7d_at IS NULL) "
    "OR (notify_24h AND reminded_24h_at IS NULL)",
]

CHECKS = [
    (
        "app_user",
        "ck_app_user_invite_pair",
        "(invite_token IS NULL) = (invite_expires_at IS NULL)",
    ),
    (
        "app_user",
        "ck_app_user_email_change_triple",
        "(pending_email IS NULL) = (email_change_token IS NULL)"
        " AND (pending_email IS NULL) = (email_change_expires_at IS NULL)",
    ),
    (
        "app_user",
        "ck_app_user_otp_pair",
        "(otp_hash IS NULL) = (otp_expires_at IS NULL)",
    ),
    (
        "interest",
        "ck_interest_resolution",
        "resolved_by IS NULL OR resolved_at IS NOT NULL",
    ),
    (
        "event",
        "ck_event_cancelled_at",
        "(status = 'cancelled') = (cancelled_at IS NOT NULL)",
    ),
    (
        "event_sub_request",
        "ck_sub_request_resolution",
        "(status = 'open') = (resolved_at IS NULL)",
    ),
    (
        "event_sub_request",
        "ck_sub_request_claimant",
        "claimed_by_volunteer_id IS NULL OR status = 'claimed'",
    ),
    ("event_task_force", "ck_task_force_teams", "team_id <> owner_team_id"),
    (
        "proposal",
        "ck_proposal_decision",
        "(status = 'open') = (decided_at IS NULL)",
    ),
]


# Rows already disagreeing with a denormalized column would block the composite
# FK below. There should be none — the services have always written the two in
# step — so this is a loud stop rather than a silent repair: a mismatch here means
# a real bug wrote a ballot into the wrong proposal's tally, and somebody should
# look at it before the constraint hides it.
DISAGREEMENTS = [
    (
        "ballots whose voter sits on another proposal's roll",
        "SELECT count(*) FROM proposal_ballot b JOIN proposal_voter v"
        " ON v.id = b.voter_id WHERE v.proposal_id <> b.proposal_id",
    ),
    (
        "ballots scoring another proposal's candidate",
        "SELECT count(*) FROM proposal_ballot b JOIN proposal_candidate c"
        " ON c.id = b.candidate_id WHERE c.proposal_id <> b.proposal_id",
    ),
    (
        "assignments whose slot belongs to another event",
        "SELECT count(*) FROM event_assignment a JOIN event_slot s"
        " ON s.id = a.slot_id WHERE s.event_id <> a.event_id",
    ),
]


def _refuse_disagreeing_rows() -> None:
    bind = op.get_bind()
    for label, sql in DISAGREEMENTS:
        found = bind.execute(sa.text(sql)).scalar_one()
        if found:
            raise RuntimeError(
                f"{found} {label}. The composite foreign keys this migration "
                "adds would reject them. Investigate before re-running — these "
                "rows mean something wrote a denormalized id that never agreed "
                "with the row it duplicates."
            )


def upgrade() -> None:
    for name, table, _ in DROPPED_INDEXES:
        op.execute(f"DROP INDEX IF EXISTS {name}")
    for ddl in ADDED_INDEXES:
        op.execute(ddl)

    # --- the two denormalized columns become constrained ----------------------
    op.execute(
        "ALTER TABLE proposal_voter "
        "ADD CONSTRAINT uq_voter_proposal UNIQUE (id, proposal_id)"
    )
    op.execute(
        "ALTER TABLE proposal_candidate "
        "ADD CONSTRAINT uq_candidate_proposal UNIQUE (id, proposal_id)"
    )
    op.execute(
        "ALTER TABLE event_slot ADD CONSTRAINT uq_slot_event UNIQUE (id, event_id)"
    )

    _refuse_disagreeing_rows()

    op.execute(
        "ALTER TABLE proposal_ballot DROP CONSTRAINT proposal_ballot_voter_id_fkey"
    )
    op.execute(
        "ALTER TABLE proposal_ballot DROP CONSTRAINT proposal_ballot_candidate_id_fkey"
    )
    op.execute(
        "ALTER TABLE proposal_ballot DROP CONSTRAINT proposal_ballot_proposal_id_fkey"
    )
    op.execute(
        "ALTER TABLE proposal_ballot ADD CONSTRAINT fk_ballot_voter_proposal "
        "FOREIGN KEY (voter_id, proposal_id) "
        "REFERENCES proposal_voter (id, proposal_id) ON DELETE CASCADE"
    )
    op.execute(
        "ALTER TABLE proposal_ballot ADD CONSTRAINT fk_ballot_candidate_proposal "
        "FOREIGN KEY (candidate_id, proposal_id) "
        "REFERENCES proposal_candidate (id, proposal_id) ON DELETE CASCADE"
    )

    op.execute(
        "ALTER TABLE event_assignment DROP CONSTRAINT event_assignment_slot_id_fkey"
    )
    op.execute(
        "ALTER TABLE event_assignment ADD CONSTRAINT fk_assignment_slot_event "
        "FOREIGN KEY (slot_id, event_id) "
        "REFERENCES event_slot (id, event_id) ON DELETE CASCADE"
    )

    for table, name, expr in CHECKS:
        op.execute(f"ALTER TABLE {table} ADD CONSTRAINT {name} CHECK ({expr})")


def downgrade() -> None:
    for table, name, _ in CHECKS:
        op.execute(f"ALTER TABLE {table} DROP CONSTRAINT {name}")

    op.execute("ALTER TABLE event_assignment DROP CONSTRAINT fk_assignment_slot_event")
    op.execute(
        "ALTER TABLE event_assignment ADD CONSTRAINT event_assignment_slot_id_fkey "
        "FOREIGN KEY (slot_id) REFERENCES event_slot (id) ON DELETE CASCADE"
    )
    op.execute(
        "ALTER TABLE proposal_ballot DROP CONSTRAINT fk_ballot_candidate_proposal"
    )
    op.execute("ALTER TABLE proposal_ballot DROP CONSTRAINT fk_ballot_voter_proposal")
    op.execute(
        "ALTER TABLE proposal_ballot ADD CONSTRAINT proposal_ballot_proposal_id_fkey "
        "FOREIGN KEY (proposal_id) REFERENCES proposal (id) ON DELETE CASCADE"
    )
    op.execute(
        "ALTER TABLE proposal_ballot ADD CONSTRAINT proposal_ballot_voter_id_fkey "
        "FOREIGN KEY (voter_id) REFERENCES proposal_voter (id) ON DELETE CASCADE"
    )
    op.execute(
        "ALTER TABLE proposal_ballot ADD CONSTRAINT proposal_ballot_candidate_id_fkey "
        "FOREIGN KEY (candidate_id) REFERENCES proposal_candidate (id) ON DELETE CASCADE"
    )
    op.execute("ALTER TABLE event_slot DROP CONSTRAINT uq_slot_event")
    op.execute("ALTER TABLE proposal_candidate DROP CONSTRAINT uq_candidate_proposal")
    op.execute("ALTER TABLE proposal_voter DROP CONSTRAINT uq_voter_proposal")

    for ddl in ADDED_INDEXES:
        name = ddl.split()[2]
        op.execute(f"DROP INDEX IF EXISTS {name}")
    for _, _, recreate in DROPPED_INDEXES:
        op.execute(recreate)
