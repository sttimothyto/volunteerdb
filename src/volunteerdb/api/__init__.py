from fastapi import APIRouter

from . import auth, capacity, custom_fields, io, memberships, reports, teams, users, volunteers

api_router = APIRouter(prefix="/api")
api_router.include_router(auth.router)
api_router.include_router(volunteers.router)
api_router.include_router(teams.router)
api_router.include_router(memberships.router)
api_router.include_router(reports.router)
api_router.include_router(users.router)
api_router.include_router(io.router)
api_router.include_router(custom_fields.router)
api_router.include_router(capacity.router)
