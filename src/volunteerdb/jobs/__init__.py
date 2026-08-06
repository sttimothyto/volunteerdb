"""Scheduled one-shot jobs, run via `python -m volunteerdb.jobs.<name>`.

Nothing in the app schedules these — periodic work runs from the host
crontab (the backup pattern; see docs/explanation/planning.md). In
production each job runs in a one-shot app container wrapped by a script
from deploy/templates/.
"""
