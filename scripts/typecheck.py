"""The type checker as a gate: `ty check src/`, held to a ceiling.

An eglot `ty` server runs over this project's Python buffers, so type errors
have always been visible while editing -- but nothing stopped them reaching
`main`, and 368 of them had. Two signatures caused 266 of those (`fp.Ok`/`fp.Err`
were invariant, and `api.deps.raise_http` took `Result[T, E] | T`, which made
`T` solve to the Result itself at every call); what is left is the honest
remainder, and this keeps it from growing.

The ceiling is the count measured after that fix, exactly. Unlike the coverage
floor there is no slack in it: a run of `ty` over a given tree is deterministic,
so the number only moves when the code or the pinned `ty` does.

- **Above it** the build fails and prints every diagnostic. Narrow the type,
  or fix the bug it found -- do not raise the ceiling to get a build green.
- **Below it** the build passes and says so. Lower the ceiling in the same
  commit; that is what makes this a ratchet rather than a speed limit.

`ty` is pinned exactly (pyproject's dev group) because it is pre-1.0: a new
release can add a rule, and that would move this number without a line of our
code changing.

Run it by hand as `make types`; CI runs it in the lint job.
"""

import re
import subprocess
import sys

# What `uv run ty check src/` reported on 2026-09-06, after fp.py's variance
# and raise_http's union were fixed (778aa47's Result idiom, typed properly).
# The remainder is roughly half SQLAlchemy's declarative surface -- model
# `__init__` overloads, Enum column descriptors -- and half real `X | None`
# narrowing gaps in query_lang.py and services/elections.py.
CEILING = 93

TARGET = "src/"
# Warnings count too: an `unused-type-ignore-comment` is a suppression that has
# outlived the error it hid, and those are exactly what a ratchet should notice.
DIAGNOSTIC = re.compile(r"^\S+:\d+:\d+: (error|warning)\[", re.MULTILINE)


def main() -> int:
    # check=False: ty exits non-zero *because* there are diagnostics, which is
    # the normal case here -- the count decides, not the exit status.
    proc = subprocess.run(
        ["ty", "check", "--output-format=concise", TARGET],
        capture_output=True,
        text=True,
        check=False,
    )
    report = proc.stdout + proc.stderr
    found = len(DIAGNOSTIC.findall(report))

    # No diagnostics and a non-zero exit means ty itself failed -- a bad
    # argument, an unreadable file -- and a silent zero would read as success.
    if found == 0 and proc.returncode not in (0, 1):
        sys.stderr.write(report)
        return proc.returncode

    if found > CEILING:
        sys.stderr.write(report)
        sys.stderr.write(
            f"\ntypecheck: {found} diagnostics over {TARGET}, ceiling is {CEILING}. "
            f"{found - CEILING} more than this tree is allowed.\n"
        )
        return 1

    if found < CEILING:
        print(
            f"typecheck: {found} diagnostics over {TARGET}, ceiling is {CEILING} — "
            f"{CEILING - found} fewer. Lower CEILING in scripts/typecheck.py "
            f"to {found} in this commit."
        )
        return 0

    print(f"typecheck: {found} diagnostics over {TARGET}, at the ceiling.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
