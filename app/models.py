#!/usr/bin/python3
# -*- coding: utf-8 -*-

from pydantic import BaseModel
from typing import Optional

class Person(BaseModel):
	uuid: str
	first_name: str
	last_name: str
	email_address: Optional[str] = None
	phone_number: Optional[str] = None

class Group(BaseModel):
	uuid: str
	name: str

class LinkToPerson(BaseModel):
	person_uuid: str

class LinkToGroup(BaseModel):
	subgroup_uuid: str

class PersonLookupFilter(BaseModel):
	first_name: Optional[str] = None
	last_name: Optional[str] = None
	email_address: Optional[str] = None
	phone_number: Optional[str] = None
