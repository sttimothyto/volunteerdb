#!/usr/bin/python3
# -*- coding: utf-8 -*-

import os
import json
import asyncpg
from typing import Annotated
from fastapi import FastAPI, HTTPException, Depends, Query
from contextlib import asynccontextmanager

import models

POSTGRES_USER = os.environ.get('POSTGRES_USER', 'postgres')
POSTGRES_PASSWORD = os.environ.get('POSTGRES_PASSWORD', 'postgres')
POSTGRES_HOST = os.environ.get('POSTGRES_HOST', 'postgresql')
POSTGRES_DB_NAME = os.environ.get('POSTGRES_DB_NAME', 'volunteers_db')

async def init_postgres_connection(conn):
	await conn.set_type_codec('jsonb', encoder=json.dumps, decoder=json.loads, schema='pg_catalog')

@asynccontextmanager
async def lifespan(app: FastAPI):
	# Setup the PostgreSQL connection pool
	app.state.db_pool = await asyncpg.create_pool('postgresql://{}:{}@{}/{}'.format(POSTGRES_USER, POSTGRES_PASSWORD, POSTGRES_HOST, POSTGRES_DB_NAME), min_size=5, max_size=20, timeout=30.0, init=init_postgres_connection)
	print("PostgreSQL connection pool established")

	# Let FastAPI run
	yield

	# Close the connection pool on shutdown
	await app.state.db_pool.close()
	print("PostgreSQL connection pool closed")

app = FastAPI(lifespan=lifespan)

async def get_db():
	pool: asyncpg.pool.Pool = app.state.db_pool
	async with pool.acquire() as connection:
		yield connection

@app.get("/")
async def root():
	return {"status": "ok"}

# Add person
@app.post("/people", status_code=201)
async def add_person(person_info: models.Person, db: asyncpg.Connection = Depends(get_db)):
	try:
		props = {
			"first_name": person_info.first_name,
			"last_name": person_info.last_name,
			"email_address": person_info.email_address,
			"phone_number": person_info.phone_number
		}
		await db.execute("INSERT INTO persons_table (id, properties) VALUES ($1, $2::jsonb)", person_info.uuid, props)
		return {"status": "ok"}
	except:
		raise HTTPException(status_code=500, detail="Request failed for unknown reason")

# List all people in the directory
@app.get("/people")
async def list_people(url_filter_params: Annotated[models.PersonLookupFilter, Query()], db: asyncpg.Connection = Depends(get_db)):
	filter_object = { k: v for k, v in dict(url_filter_params).items() if v is not None }
	try:
		rows = await db.fetch("SELECT id, properties FROM persons_table WHERE properties @> $1::jsonb", filter_object)
		if not rows:
			return []
		return [dict(row) for row in rows]
	except:
		raise HTTPException(status_code=500, detail="Request failed for unknown reason")

# Get person info
@app.get("/people/{person_uuid}")
async def get_person(person_uuid: str, db: asyncpg.Connection = Depends(get_db)):
	try:
		row = await db.fetchrow("SELECT properties FROM persons_table WHERE id = $1", person_uuid)
		return dict(row)['properties']
	except:
		raise HTTPException(status_code=500, detail="Request failed for unknown reason")

# Add group
@app.post("/group", status_code=201)
async def add_group(group_info: models.Group, db: asyncpg.Connection = Depends(get_db)):
	try:
		props = {
			"name": group_info.name
		}
		await db.execute("INSERT INTO groups_table (id, properties) VALUES ($1, $2::jsonb)", group_info.uuid, props)
		return {"status": "ok"}
	except:
		raise HTTPException(status_code=500, detail="Request failed for unknown reason")

# Get group info
@app.get("/group/{group_uuid}")
async def get_group(group_uuid: str, db: asyncpg.Connection = Depends(get_db)):
	try:
		row = await db.fetchrow("SELECT properties FROM groups_table WHERE id = $1", group_uuid)
	except:
		raise HTTPException(status_code=500, detail="Request failed for unknown reason")

# Link person to group
@app.post("/link/members/{group_uuid}", status_code=201)
async def link_person_to_group(group_uuid: str, person_link: models.LinkToPerson, db: asyncpg.Connection = Depends(get_db)):
	try:
		await db.execute("INSERT INTO links_table (src, dst, type, status, started_at) VALUES ($1, $2, $3, $4, NOW())", group_uuid, person_link.person_uuid, "has_person", "Active")
		return {"status": "ok"}
	except:
		raise HTTPException(status_code=500, detail="Request failed for unknown reason")

# List direct members of a group
@app.get("/link/members/{group_uuid}")
async def list_members_of_group(group_uuid: str, db: asyncpg.Connection = Depends(get_db)):
	try:
		rows = await db.fetch("SELECT dst, status, started_at, ended_at FROM links_table WHERE src = $1 AND type = $2", group_uuid, "has_person")
		if not rows:
			return []
		return [dict(row) for row in rows]
	except:
		raise HTTPException(status_code=500, detail="Request failed for unknown reason")

# Unlink person to group
@app.delete("/link/members/{group_uuid}/{person_uuid}", status_code=204)
async def unlink_group_member(group_uuid: str, person_uuid: str, db: asyncpg.Connection = Depends(get_db)):
	try:
		await db.execute("UPDATE links_table SET status = $1, ended_at = NOW() WHERE type = $2 AND src = $3 AND dst = $4", "Ended", "has_person", group_uuid, person_uuid)
		return {"status": "ok"}
	except:
		raise HTTPException(status_code=500, detail="Request failed for unknown reason")

# Link group to sub-group
@app.post("/link/subgroups/{parent_group_uuid}", status_code=201)
async def link_subgroup_to_group(parent_group_uuid: str, subgroup_link: models.LinkToGroup, db: asyncpg.Connection = Depends(get_db)):
	try:
		await db.execute("INSERT INTO links_table (src, dst, type, status, started_at) VALUES ($1, $2, $3, $4, NOW())", parent_group_uuid, subgroup_link.subgroup_uuid, "has_group", "Active")
		return {"status": "ok"}
	except:
		raise HTTPException(status_code=500, detail="Request failed for unknown reason")

# List direct sub-groups of a group
@app.get("/link/subgroups/{parent_group_uuid}")
async def list_subgroups(parent_group_uuid: str, db: asyncpg.Connection = Depends(get_db)):
	try:
		rows = await db.fetch("SELECT dst, status, started_at, ended_at FROM links_table WHERE src = $1 AND type = $2", parent_group_uuid, "has_group")
		if not rows:
			return []
		return [dict(row) for row in rows]
	except:
		raise HTTPException(status_code=500, detail="Request failed for unknown reason")

# Unlink group to sub-group
@app.delete("/link/members/{parent_group_uuid}/{subgroup_uuid}", status_code=204)
async def unlink_subgroup(parent_group_uuid: str, subgroup_uuid: str, db: asyncpg.Connection = Depends(get_db)):
	try:
		await db.execute("UPDATE links_table SET status = $1, ended_at = NOW() WHERE type = $2 AND src = $3 AND dst = $4", "Ended", "has_group", parent_group_uuid, subgroup_uuid)
		return {"status": "ok"}
	except:
		raise HTTPException(status_code=500, detail="Request failed for unknown reason")

# See which groups a person belongs / belonged to
@app.get("/link/person/{person_uuid}")
async def get_groups_for_person(person_uuid: str, db: asyncpg.Connection = Depends(get_db)):
	try:
		rows = await db.fetch("SELECT src, status, started_at, ended_at FROM links_table WHERE dst = $1 AND type = $2", person_uuid, "has_person")
		if not rows:
			return []
		return [dict(row) for row in rows]
	except:
		raise HTTPException(status_code=500, detail="Request failed for unknown reason")

if __name__ == "__main__":
	import uvicorn
	uvicorn.run(app, host="0.0.0.0", port=8000)
