FROM localhost/debian:trixie

RUN apt-get update
RUN apt-get -y install \
	python3 \
	python3-asyncpg \
	python3-fastapi

WORKDIR /app
ENTRYPOINT ["python3", "app.py"]
