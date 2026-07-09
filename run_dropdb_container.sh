#!/bin/bash

podman run --rm --name fastapi --network volunteer-app-net -v $(realpath app):/app --entrypoint /usr/bin/python3 -it localhost/fastapi drop_db.py
