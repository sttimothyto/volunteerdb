# ---- build stage ----
FROM ghcr.io/astral-sh/uv:python3.13-trixie-slim AS builder
ENV UV_COMPILE_BYTECODE=1 UV_LINK_MODE=copy UV_PYTHON_DOWNLOADS=0
WORKDIR /app
# Dependency layer: only invalidated when the lockfile changes.
COPY pyproject.toml uv.lock ./
RUN --mount=type=cache,target=/root/.cache/uv \
    uv sync --frozen --no-install-project --no-dev
# --no-editable: install the package into site-packages (static assets
# included) so the venv is self-contained rather than pointing at /app/src.
COPY . .
RUN --mount=type=cache,target=/root/.cache/uv \
    uv sync --frozen --no-dev --no-editable

# ---- docs stage ----
# Builds the Sphinx manual served at /manual. Pins sphinx here rather than
# reusing the `docs` dependency group, so the manual builds without resolving
# the app's own dependencies; conf.py reads the version from pyproject.toml,
# so the project itself is never installed in this stage.
FROM ghcr.io/astral-sh/uv:python3.13-trixie-slim AS docs
WORKDIR /build
COPY pyproject.toml ./
COPY docs ./docs
RUN --mount=type=cache,target=/root/.cache/uv \
    uvx --from 'sphinx>=9,<10' --with myst-parser --with furo --with sphinx-copybutton \
    sphinx-build -W --keep-going -b html docs docs/_build/html

# ---- runtime stage ----
# Must share the builder's Debian base so the venv's interpreter symlink resolves.
FROM docker.io/library/python:3.13-slim-trixie
RUN useradd --system --uid 10001 --create-home app
# /app carries the venv plus alembic.ini, migrations/, create_admin.py.
COPY --from=builder --chown=app:app /app /app
COPY --from=docs --chown=app:app /build/docs/_build/html /app/docs-html
ENV VDB_DOCS_DIR=/app/docs-html
# NiceGUI writes session storage to .nicegui/ in the cwd; pre-create the
# mountpoint so the named volume's copy-up inherits app's uid.
RUN mkdir -p /app/.nicegui && chown app:app /app/.nicegui
ENV PATH="/app/.venv/bin:$PATH"
WORKDIR /app
USER app
EXPOSE 8080
# No ENTRYPOINT: one image serves as app server (default CMD), migration
# runner (`alembic upgrade head`), and admin bootstrap (`python create_admin.py`).
CMD ["volunteerdb"]
