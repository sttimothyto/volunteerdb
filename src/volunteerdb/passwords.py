"""What VolunteerDB accepts as a password, and why.

Every rule below comes from NIST SP 800-63B rev. 4, §3.1.1.2 *Password
Verifiers* (https://pages.nist.gov/800-63-4/sp800-63b.html):

- **15 characters minimum.** "Verifiers and CSPs SHALL require passwords that
  are used as a single-factor authentication mechanism to be a minimum of 15
  characters in length." A password here *is* single-factor — it opens the
  session on its own — so the eight-character allowance for multi-factor
  passwords does not apply. Nobody is forced to type fifteen characters: the
  password is optional, and leaving it blank gets an emailed code instead.
- **128 characters maximum** ("SHOULD permit a maximum password length of at
  least 64"). The cap only exists so a hostile 10 MB body cannot make argon2
  chew through it.
- **Everything is a legal character** — printing ASCII, spaces, and Unicode,
  counted by code point as the spec requires. There are no composition rules,
  because "Verifiers and CSPs SHALL NOT impose other composition rules (e.g.,
  requiring mixtures of different character types)".
- **A blocklist, checked whole.** "When processing a request to establish or
  change a password, verifiers SHALL compare the prospective secret against a
  blocklist that contains known commonly used, expected, or compromised
  passwords. The entire password SHALL be subject to comparison, not substrings
  or words that might be contained therein." So `correcthorsebatterystaple` is
  rejected as a whole (it is famous), while a password that merely *contains*
  "horse" is fine.
- **Guidance, and a reason on rejection.** "If the chosen password is found on
  the blocklist, the CSP SHALL require the subscriber to select a different
  secret and SHALL provide the reason for rejection... Verifiers SHALL offer
  guidance to the subscriber to help the subscriber choose a strong password."
  Hence one specific sentence per rejection plus `GUIDANCE` on every form.
- **NFC normalization** before hashing, per the same section; applied in
  `auth.hash_password`/`auth.verify_password` so both sides agree. ASCII is
  unaffected, so existing hashes keep verifying.

The blocklist is deliberately small. The spec says so: "Excessively large
blocklists are of little incremental security benefit because the blocklist is
used to defend against online attacks, which are already limited by the
throttling requirements described in Sec. 3.2.2." Shipping a breach corpus
would add almost nothing on top of the 15-character floor — nearly every entry
in the usual top-10k lists is shorter than that — so what is listed here are
the *bases* that survive lengthening: the words people pad with digits, double
up, or walk across the keyboard from. Folding (below) is what makes a short
list bite: `P@ssw0rd-2026`, `passwordpassword` and `PASSWORD12345678` all
collapse onto the single entry `password`.

Rate limiting (§3.2.2) lives in `throttle.py`; password *storage* (salt, cost
factor, stored parameters) lives in `auth.py`.
"""

import unicodedata
from functools import lru_cache

from .errors import WeakPassword
from .errors import message as _message
from .fp import Err

MIN_LENGTH = 15
MAX_LENGTH = 128

GUIDANCE = (
    "At least 15 characters. A phrase of four or five unrelated words is the "
    "easiest to remember and the hardest to guess — no capitals, digits or "
    "symbols required."
)

# Context-specific words: "the name of the service, the username, and
# derivatives thereof" (§3.1.1.2). These are the ones true of every instance;
# the account's own address is added per check, and the organisation's own
# name and domains by _org_terms() below.
GENERIC_SERVICE_TERMS = frozenset(
    {
        "volunteerdb",
        "vdb",
        "volunteer",
        "volunteers",
        "volunteerdatabase",
        "parish",
        "church",
        "ministry",
        "roster",
    }
)

# Common bases, stored folded (see _fold). Padding, doubling and leetspeak are
# undone before the comparison, so each entry covers a large family.
BLOCKLIST = frozenset(
    {
        # the perennial top of every breach corpus
        "password",
        "passwd",
        "pass",
        "secret",
        "letmein",
        "letmeinplease",
        "changeme",
        "changemenow",
        "default",
        "temporary",
        "welcome",
        "welcomehome",
        "admin",
        "administrator",
        "root",
        "login",
        "signin",
        "master",
        "access",
        "guest",
        "test",
        "testing",
        "user",
        "qwerty",
        "qwertyuiop",
        "azerty",
        "abcdefg",
        "iloveyou",
        "iloveyousomuch",
        "iloveyouforever",
        "ihateyou",
        "trustnoone",
        "whatever",
        "nopassword",
        "mypassword",
        "thisismypassword",
        "passwordisapassword",
        "opensesame",
        "starwars",
        "superman",
        "batman",
        "pokemon",
        "minecraft",
        "football",
        "baseball",
        "basketball",
        "soccer",
        "hockey",
        "sunshine",
        "princess",
        "butterfly",
        "chocolate",
        "computer",
        "internet",
        "freedom",
        "michael",
        "jennifer",
        "jessica",
        "charlie",
        "monkey",
        "dragon",
        "shadow",
        "flower",
        "friends",
        "family",
        "hello",
        "helloworld",
        "goodbye",
        "summer",
        "winter",
        "spring",
        "autumn",
        "canada",
        "toronto",
        "ontario",
        # long strings that are famous *because* they are long
        "correcthorsebatterystaple",
        "thequickbrownfoxjumpsoverthelazydog",
        "tobeornottobe",
        "loremipsum",
        "loremipsumdolorsitamet",
        # a parish's own context-specific vocabulary
        "godislove",
        "godisgood",
        "godisgoodallthetime",
        "jesus",
        "jesuschrist",
        "jesusloves",
        "jesuslovesme",
        "jesusismylord",
        "jesussaves",
        "holyspirit",
        "hallelujah",
        "alleluia",
        "amazinggrace",
        "amen",
        "blessed",
        "faith",
        "gospel",
        "heaven",
        "ourfatherwhoartinheaven",
        "hailmaryfullofgrace",
        "thelordismyshepherd",
    }
)

# Runs people walk along instead of choosing. Doubled/tripled so that a
# password long enough to wrap around ("12345678901234567") is still caught.
_WALKS = (
    "abcdefghijklmnopqrstuvwxyz" * 2,
    "0123456789" * 4,
    "qwertyuiopasdfghjklzxcvbnm" * 2,
    "qwertyuiop" * 3,
    "asdfghjkl" * 3,
    "zxcvbnm" * 4,
    "1qaz2wsx3edc4rfv" * 2,
    "qazwsxedcrfvtgb" * 2,
)

# Undo the substitutions that turn a listed word into "a different password".
# Punctuation stand-ins are always undone; digit stand-ins are a second reading
# of the same password, because a digit is also how people decorate one (see
# _forms) and "0" cannot be both the letter O and the padding at once.
_SYMBOL_LEET = str.maketrans({"@": "a", "$": "s", "!": "i", "|": "i", "+": "t"})
_DIGIT_LEET = str.maketrans(
    {"0": "o", "1": "i", "3": "e", "4": "a", "5": "s", "7": "t"}
)


def normalize(password: str) -> str:
    """NFC, as §3.1.1.2 asks, applied before the byte string is hashed."""
    return unicodedata.normalize("NFC", password)


def _fold(password: str, *, symbols: bool = False, digits: bool = False) -> str:
    """Collapse a password onto its blocklist form: lowercase, accents dropped,
    everything but letters and digits removed — optionally reading `@$!|+` and
    `013457` as the letters they stand in for.

    Each substitution has to be optional because it cuts both ways. Reading
    digits as letters turns `Passw0rd` into `password`, but it also eats the
    "0" of `password2026`, whose digits must survive folding to be stripped as
    decoration; `!` is a letter in `pa!!word` and punctuation in
    `iloveyouforever!`. So `_forms` folds every combination and lets the
    blocklist decide."""
    stripped = unicodedata.normalize("NFKD", password.casefold())
    stripped = "".join(c for c in stripped if not unicodedata.combining(c))
    if symbols:
        stripped = stripped.translate(_SYMBOL_LEET)
    if digits:
        stripped = stripped.translate(_DIGIT_LEET)
    return "".join(c for c in stripped if c.isalnum())


def _unrepeat(folded: str) -> str:
    """abcabcabc -> abc. A password built by repeating a unit is only as strong
    as the unit it repeats."""
    for size in range(1, len(folded) // 2 + 1):
        if len(folded) % size == 0 and folded[:size] * (len(folded) // size) == folded:
            return folded[:size]
    return folded


def _undecorate(folded: str) -> str:
    """Drop what gets stapled to a listed word to make it look different:
    digits at either end (`password2026`) and a doubled character (`Password!!`,
    whose "!!" folds to "ii"). Layered decorations come off in layers."""
    current = folded
    while True:
        trimmed = current.strip("0123456789")
        if len(trimmed) > 2 and trimmed[0] == trimmed[1]:
            trimmed = trimmed.lstrip(trimmed[0])
        if len(trimmed) > 2 and trimmed[-1] == trimmed[-2]:
            trimmed = trimmed.rstrip(trimmed[-1])
        if not trimmed or trimmed == current:
            return current
        current = trimmed


def _forms(password: str) -> set[str]:
    """The folded forms of one password that a blocklist entry may match."""
    forms: set[str] = set()
    for symbols in (False, True):
        for digits in (False, True):
            folded = _fold(password, symbols=symbols, digits=digits)
            bare = _undecorate(folded)
            forms |= {
                folded,
                bare,
                _unrepeat(folded),
                _unrepeat(bare),
                _undecorate(_unrepeat(folded)),
            }
    # Digits-as-letters *after* the trailing digits were stripped as
    # decoration, which is the order "P@ssw0rd12345678" needs: strip the
    # 12345678, then read the 0 as an o.
    forms |= {form.translate(_DIGIT_LEET) for form in forms}
    return forms - {""}


@lru_cache
def _org_terms(org_name: str, mail_from: str, public_base_url: str) -> frozenset[str]:
    """This instance's own names, folded — the half of §3.1.1.2's
    "name of the service" that depends on who is running it.

    Derived rather than configured, so that a new parish gets its own terms
    blocked without having to think about it. For "St. Peter's" at
    stpetersparish.org this yields stpeters, stpeter, saintpeters, saintpeter
    and stpetersparish — the shape the list used to hardcode for one parish,
    plus the mail domain it did not.
    """
    terms: set[str] = set()
    if org_name:
        folded = _fold(org_name)
        terms |= {folded, folded.removesuffix("s")}
        # "St." is the common abbreviation; block the spelled-out form too.
        if folded.startswith("st") and not folded.startswith("saint"):
            spelled = "saint" + folded[2:]
            terms |= {spelled, spelled.removesuffix("s")}
    for source in (mail_from, public_base_url):
        # Every label of the host except the public suffix: stpetersparish.org
        # contributes stpetersparish, vdb.example.org contributes vdb and example.
        host = source.rsplit("@", 1)[-1].split("//")[-1].split("/")[0]
        labels = host.split(".")
        terms |= {_fold(label) for label in labels[:-1]}
    return frozenset(terms - {""})


def site_terms(org_name: str, mail_from: str, public_base_url: str) -> frozenset[str]:
    """This instance's own names, for check(): the edge computes them once from
    its settings (env.Env.password_terms)."""
    return _org_terms(org_name, mail_from, public_base_url)


def _context_terms(email: str | None, site: frozenset[str]) -> set[str]:
    """The service's names plus the account's own address, folded."""
    terms = set(GENERIC_SERVICE_TERMS) | site
    if email:
        local, _, domain = email.strip().partition("@")
        terms |= {_fold(email), _fold(local)}
        if domain:
            terms |= {_fold(domain), _fold(domain.rsplit(".", 1)[0])}
    return terms - {""}


def check(
    password: str, *, email: str | None = None, site_terms: frozenset[str] = frozenset()
) -> Err[WeakPassword] | None:
    """The WeakPassword refusal, or None when `password` may be set on `email`'s
    account. `site_terms` are this instance's own names (site_terms()), which
    the edge computes once from its settings.

    The message is written to be shown to whoever typed it: it says which rule
    was hit and what to do instead, because §3.1.1.2 requires both the reason
    for the rejection and guidance towards a strong choice."""
    password = normalize(password)
    # len() counts code points, which is what "Each Unicode code point SHALL be
    # counted as a single character when evaluating password length" asks for.
    if len(password) < MIN_LENGTH:
        return Err(
            WeakPassword(
                f"That password is too short — it needs {MIN_LENGTH} characters or "
                f"more (this one has {len(password)}). {GUIDANCE}"
            )
        )
    if len(password) > MAX_LENGTH:
        return Err(
            WeakPassword(
                f"That password is too long — {MAX_LENGTH} characters is the limit."
            )
        )

    forms = _forms(password)
    if forms & _context_terms(email, site_terms):
        return Err(
            WeakPassword(
                "That password is your email address or the name of this site. "
                f"Pick something unrelated to the account. {GUIDANCE}"
            )
        )
    if forms & BLOCKLIST:
        return Err(
            WeakPassword(
                "That password is a well-known one, or a lightly disguised version "
                f"of one, so it is among the first an attacker tries. {GUIDANCE}"
            )
        )

    folded = _fold(password)
    if len(_unrepeat(folded)) <= 4:
        return Err(
            WeakPassword(
                "That password is one short pattern repeated, which is no harder to "
                f"guess than the pattern itself. {GUIDANCE}"
            )
        )
    if len(set(folded or password)) < 5:
        return Err(
            WeakPassword(
                "That password uses too few different characters to be hard to "
                f"guess. {GUIDANCE}"
            )
        )
    if any(folded in walk or folded in walk[::-1] for walk in _WALKS):
        return Err(
            WeakPassword(
                "That password runs straight along the keyboard (or the alphabet), "
                f"which guessing tools walk first. {GUIDANCE}"
            )
        )
    return None


def problem(
    password: str, *, email: str | None = None, site_terms: frozenset[str] = frozenset()
) -> str | None:
    """The rejection message, or None if the password is acceptable. For live
    feedback on a form."""
    refusal = check(password, email=email, site_terms=site_terms)
    return None if refusal is None else _message(refusal.error)
