#!/usr/bin/python3
# -*- coding: utf-8 -*-

import os
import asyncpg

POSTGRES_USER = os.environ.get('POSTGRES_USER', 'postgres')
POSTGRES_PASSWORD = os.environ.get('POSTGRES_PASSWORD', 'postgres')
POSTGRES_HOST = os.environ.get('POSTGRES_HOST', 'postgresql')
POSTGRES_DB_NAME = os.environ.get('POSTGRES_DB_NAME', 'volunteers_db')

async def main():
	conn = await asyncpg.connect('postgresql://{}:{}@{}/{}'.format(POSTGRES_USER, POSTGRES_PASSWORD, POSTGRES_HOST, POSTGRES_DB_NAME))
	await conn.execute("CREATE TABLE links_table(src TEXT, dst TEXT, type TEXT, status TEXT, started_at TIMESTAMPTZ, ended_at TIMESTAMPTZ)")

	# Only one "Active" link may exist at a time for a link between src-->dst
	await conn.execute("CREATE UNIQUE INDEX single_active_link ON links_table(src, dst) WHERE status = 'Active'")

	# (At least for now) a (sub)group may belong to no more than one group
	await conn.execute("CREATE UNIQUE INDEX single_parent_subgroup ON links_table(dst) WHERE type = 'has_group' AND status = 'Active'")

	await conn.execute("CREATE TABLE groups_table(id TEXT, properties JSONB)")
	await conn.execute("CREATE UNIQUE INDEX unique_group_id ON groups_table(id)")

	await conn.execute("CREATE TABLE persons_table(id TEXT, properties JSONB)")
	await conn.execute("CREATE UNIQUE INDEX unique_person_id ON persons_table(id)")

	await conn.close()

if __name__ == "__main__":
	import asyncio
	asyncio.run(main())
	print("DONE")
