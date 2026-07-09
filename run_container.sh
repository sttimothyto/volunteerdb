#!/bin/bash

podman run --rm --name fastapi --network volunteer-app-net -v $(realpath app):/app -p 127.0.0.1:8000:8000 -d localhost/fastapi
