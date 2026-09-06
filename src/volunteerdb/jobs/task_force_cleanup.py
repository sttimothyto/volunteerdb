"""Task-force teardown (in-app scheduler, hourly).

Dismantles the auto-created task-force team of every event that has ended
or was cancelled: the event is repointed back to its owning team, then the
meta team is hard-deleted (services/task_force.py owns the load-bearing
ordering). One transaction per task force, so one failure cannot hold the
rest hostage; history twins keep every torn-down membership visible in
as-of views.

Usage: python -m volunteerdb.jobs.task_force_cleanup
"""

import argparse
import sys

import structlog

from ..db import transaction
from ..env import Env
from ..errors import NotFound, message
from ..fp import Err
from ..log import audit_log, init_logging
from ..services import task_force
from . import run_locked

logger = structlog.get_logger(__name__)


async def main(env: Env) -> int:
    init_logging()
    async with transaction(env, None) as session:
        due = await task_force.teardown_due(session, now=env.clock.now())

    done = failed = 0
    for event_id in due:
        try:
            async with transaction(env, None) as session:  # one transaction each
                torn = await task_force.teardown(session, event_id)
                if isinstance(torn, Err):
                    await session.rollback()
        except Exception:
            failed += 1
            logger.exception("task_force_cleanup.failed", event_id=event_id)
            continue
        match torn:
            case Err(NotFound()):
                continue  # a concurrent run got there first
            case Err(err):
                failed += 1
                logger.error(
                    "task_force_cleanup.refused", event_id=event_id, error=message(err)
                )
                continue
        done += 1
        audit_log("event.task_force_teardown", event_id=event_id)

    print(f"task force cleanup: {done} torn down, {failed} failure(s)")
    return 1 if failed else 0


def cli(argv: list[str] | None = None) -> int:
    argparse.ArgumentParser(description=__doc__).parse_args(argv)
    return run_locked("task_force_cleanup", main)


if __name__ == "__main__":
    sys.exit(cli())
