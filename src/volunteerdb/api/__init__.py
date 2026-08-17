from fastapi import APIRouter

from . import (
    auth,
    custom_fields,
    events,
    io,
    memberships,
    planning,
    reports,
    teams,
    users,
    volunteers,
    workload,
)

api_router = APIRouter(prefix="/api")
api_router.include_router(auth.router)
api_router.include_router(volunteers.router)
api_router.include_router(teams.router)
api_router.include_router(memberships.router)
api_router.include_router(reports.router)
api_router.include_router(users.router)
api_router.include_router(io.router)
api_router.include_router(custom_fields.router)
api_router.include_router(workload.router)
api_router.include_router(planning.router)
api_router.include_router(events.router)
