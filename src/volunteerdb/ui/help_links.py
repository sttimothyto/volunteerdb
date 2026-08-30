"""Which pages of the manual the app offers, by what the reader can do.

The user guide (docs/guide/) is written for a parishioner with no technical
background, and the one place they will find it is the bottom of the page they
land on. What they need there depends on what they can do here: a member wants
their shifts and their photo, a leader the roster and the spreadsheet, an
admin the accounts -- so the groups accumulate with the reader's reach, the
same tiers permissions.Actor is cut into, and a group the reader cannot act on
is absent rather than empty. A pure table over the Actor: nothing here reads
a session, so tests/test_help_links.py can hold every tier to its links and
every link to a page that exists.

SIGNING_IN is the other reader: the one who has not signed in yet, and who
the sign-in page offers these pages to under a help icon (ui/login.py). It
takes no Actor because there is nobody to ask about — and it is the reason
docs/guide/ is public (main.PUBLIC_MANUAL_PREFIXES): help held behind the
sign-in cannot help anyone who cannot sign in.
"""

from dataclasses import dataclass

from ..permissions import Actor

GUIDE = "/manual/guide/"
# The manual's landing page, which is the guide's own front door: public with
# the rest of the guide, and the way into all of it from one link.
GUIDE_HOME = "/manual/"


@dataclass(frozen=True, slots=True)
class HelpLink:
    title: str
    path: str  # under GUIDE, or absolute when it starts with "/"
    tutorial: bool = False

    @property
    def href(self) -> str:
        return self.path if self.path.startswith("/") else GUIDE + self.path


@dataclass(frozen=True, slots=True)
class HelpGroup:
    title: str
    links: tuple[HelpLink, ...]


SIGNING_IN = HelpGroup(
    "Help signing in",
    (
        HelpLink("Sign in for the first time", "tutorials/first-sign-in.html", True),
        HelpLink("Sign in with an emailed code", "how-to/sign-in-with-a-code.html"),
        # why the site asks for no password, for the reader hunting for one
        HelpLink(
            "Sign in without a password",
            "explanation/sign-in-without-a-password.html",
        ),
        HelpLink("Why VolunteerDB", "explanation/why-volunteerdb.html"),
        HelpLink("Report a problem", "how-to/report-a-problem.html"),
        HelpLink("The user guide", GUIDE_HOME),
    ),
)

EVERYONE = HelpGroup(
    "For everyone",
    (
        HelpLink("Sign in for the first time", "tutorials/first-sign-in.html", True),
        HelpLink("Sign in with an emailed code", "how-to/sign-in-with-a-code.html"),
        HelpLink("Change your password", "how-to/change-your-password.html"),
        HelpLink("Change your email address", "how-to/change-your-email-address.html"),
        HelpLink(
            "Put the parish calendar on your phone",
            "how-to/calendar-on-your-phone.html",
        ),
        HelpLink(
            "Find a volunteer or a team", "how-to/find-a-volunteer-or-a-team.html"
        ),
        HelpLink(
            "See the parish as it was on a past date",
            "how-to/see-the-parish-as-of-a-date.html",
        ),
        HelpLink("Use dark mode", "how-to/dark-mode.html"),
        HelpLink("Report a problem", "how-to/report-a-problem.html"),
    ),
)

MEMBERS = HelpGroup(
    "For team members",
    (
        HelpLink(
            "Your teams and your service",
            "tutorials/your-teams-and-your-service.html",
            True,
        ),
        HelpLink("Your first shift", "tutorials/your-first-shift.html", True),
        HelpLink(
            "Update your contact details", "how-to/update-your-contact-details.html"
        ),
        HelpLink("Add or change your photo", "how-to/add-your-photo.html"),
        HelpLink("Ask for a substitute", "how-to/ask-for-a-substitute.html"),
        HelpLink("Cover an open shift", "how-to/cover-a-shift.html"),
    ),
)

VOTERS = HelpGroup(
    "For voters",
    (HelpLink("Vote in an election", "how-to/vote-in-an-election.html"),),
)

CORE = HelpGroup(
    "For core members",
    (
        HelpLink(
            "Read the full roster of your team", "how-to/read-the-full-roster.html"
        ),
        HelpLink(
            "Invite a volunteer to create an account", "how-to/invite-a-volunteer.html"
        ),
    ),
)

LEADERS = HelpGroup(
    "For leaders and seconds",
    (
        HelpLink("Lead a team", "tutorials/lead-a-team.html", True),
        HelpLink("Add or remove a member", "how-to/add-or-remove-a-member.html"),
        HelpLink("Change the role of a member", "how-to/change-a-members-role.html"),
        HelpLink(
            "Edit the contact details of a member",
            "how-to/edit-a-members-contact-details.html",
        ),
        HelpLink("Link a roster spreadsheet", "how-to/link-a-roster-spreadsheet.html"),
        HelpLink("Import a roster from a .csv file", "how-to/import-a-csv.html"),
        HelpLink("Export the roster", "how-to/export-the-roster.html"),
        HelpLink(
            "Publish the team home page", "how-to/publish-the-team-home-page.html"
        ),
        HelpLink("Create an event with shifts", "how-to/create-an-event.html"),
        HelpLink("Cancel an event", "how-to/cancel-an-event.html"),
        HelpLink(
            "Hand over or withdraw a shift", "how-to/hand-over-or-withdraw-a-shift.html"
        ),
        HelpLink("Run an election", "how-to/run-an-election.html"),
        HelpLink("Read the workload of your people", "how-to/read-workload.html"),
        HelpLink("Create a task force", "how-to/create-a-task-force.html"),
    ),
)

ADMINS = HelpGroup(
    "For administrators",
    (
        HelpLink("Administer the parish", "tutorials/administer-the-parish.html", True),
        HelpLink("Manage accounts", "how-to/manage-accounts.html"),
        HelpLink("Send an invite again", "how-to/resend-an-invite.html"),
        HelpLink("Add a custom field", "how-to/add-a-custom-field.html"),
        HelpLink("Set the workload bands", "how-to/set-workload-bands.html"),
        HelpLink("Upload the parish logo", "how-to/upload-the-parish-logo.html"),
        HelpLink("Read the audit log", "how-to/read-the-audit-log.html"),
        # ?dev=1 flips the manual's audience switch on (docs/_static/vdb-manual.js)
        HelpLink("The technical manual", "/manual/?dev=1"),
    ),
)


def groups_for(actor: Actor) -> list[HelpGroup]:
    """The groups this reader can act on, widest audience first -- the order
    the dashboard's own statistics run in."""
    groups = [EVERYONE]
    if actor.volunteer_id is not None:
        groups.append(MEMBERS)
    if actor.voter_proposal_ids:
        groups.append(VOTERS)
    if actor.is_admin or actor.full_view_team_ids:
        groups.append(CORE)
    if actor.is_admin or actor.managed_team_ids:
        groups.append(LEADERS)
    if actor.is_admin:
        groups.append(ADMINS)
    return groups
