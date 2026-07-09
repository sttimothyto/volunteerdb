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
	await conn.execute("DROP TABLE IF EXISTS links_table")
	await conn.execute("DROP TABLE IF EXISTS groups_table")
	await conn.execute("DROP TABLE IF EXISTS persons_table")
	await conn.close()

if __name__ == "__main__":
	import asyncio
	asyncio.run(main())
	print("DONE")
