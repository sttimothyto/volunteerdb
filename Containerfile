FROM localhost/debian:trixie

RUN apt-get update
RUN apt-get -y install \
	python3 \
	python3-venv \
	ca-certificates

# nicegui and friends are not packaged in Debian, so the app runs from its
# own uv-managed virtualenv pinned by uv.lock
COPY --from=ghcr.io/astral-sh/uv:latest /uv /usr/local/bin/uv

WORKDIR /app
COPY pyproject.toml uv.lock ./
RUN uv sync --frozen --no-install-project --no-dev

COPY . .
RUN uv sync --frozen --no-dev

ENV PATH="/app/.venv/bin:$PATH"
EXPOSE 8080
ENTRYPOINT ["volunteerdb"]
