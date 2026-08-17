"""The application stack: source, env files, image, quadlets, schema, restart.

Ends with a smoke test that fails the deploy if the app does not come back,
which is what makes an unattended run from CI safe to gate on.
"""

import shlex

import siteconf
from pyinfra.api.deploy import deploy
from pyinfra.operations import files, server, systemd


@deploy("Application stack")
def deploy_app(site, secrets, *, here, repo_root, unit_vars, admin_password) -> None:
    files.sync(
        name="Sync application code (build context)",
        src=str(repo_root),
        dest=siteconf.APP_DIR,
        delete=True,
        exclude=siteconf.SYNC_EXCLUDE,
        exclude_dir=siteconf.SYNC_EXCLUDE_DIR,
    )
    # Synced: src/, migrations/, docs/, alembic.ini, pyproject.toml, uv.lock,
    #         README.md, .python-version, Containerfile, .containerignore

    files.template(
        name="Write /etc/volunteerdb/env",
        src=str(here / "templates" / "env.j2"),
        dest=siteconf.ENV_FILE,
        mode="600",
        user="root",
        group="root",
        database_url=secrets.database_url,
        storage_secret=secrets.storage_secret,
        smtp2go_api_key=secrets.smtp2go_api_key,
        db_password=secrets.db_password,
        port=siteconf.APP_PORT,
        template_sheet_url=secrets.template_sheet_url,
        public_base_url=secrets.public_base_url,
        gcal_client_id=secrets.gcal_client_id,
        gcal_client_secret=secrets.gcal_client_secret,
        gcal_refresh_token=secrets.gcal_refresh_token,
        gcal_calendar_id=secrets.gcal_calendar_id,
        alert_email=site.mail_alert_email,
        org_name=site.site_org_name,
        mail_from=site.mail_from_address,
        mail_from_name=site.mail_from_name,
        timezone=site.site_timezone,
        fetch_pages_at=site.schedule_fetch_pages_at,
        proposal_digest_at=site.schedule_proposal_digest_at,
        event_reminders_at=site.schedule_event_reminders_at,
    )
    files.template(
        name="Write /etc/volunteerdb/db.env",
        src=str(here / "templates" / "db.env.j2"),
        dest=siteconf.DB_ENV_FILE,
        mode="600",
        user="root",
        group="root",
        db_password=secrets.db_password,
        db_user=siteconf.DB_USER,
        db_name=siteconf.DB_NAME,
    )

    server.shell(
        name="Pull postgres image (if absent)",
        commands=[
            f"podman image exists {siteconf.PG_IMAGE} "
            f"|| podman pull -q {siteconf.PG_IMAGE}"
        ],
    )
    # The docs stage bakes the manual into the image, so the instance's name,
    # contact address and origin have to be known at BUILD time. shlex.quote
    # on every one of them: an organisation name legitimately contains spaces
    # and apostrophes ("St. Timothy's" is both).
    build_args = " ".join(
        f"--build-arg {key}={shlex.quote(value)}"
        for key, value in (
            ("VDB_ORG_NAME", site.site_org_name),
            ("VDB_CONTACT_EMAIL", site.mail_contact_email),
            ("VDB_PUBLIC_BASE_URL", secrets.public_base_url),
        )
    )
    server.shell(
        name="Build app image",
        commands=[f"podman build {build_args} -t {siteconf.IMAGE} {siteconf.APP_DIR}"],
    )

    for quadlet in siteconf.QUADLETS:
        files.template(
            name=f"Install quadlet {quadlet}",
            src=str(here / "templates" / f"{quadlet}.j2"),
            dest=f"{siteconf.QUADLET_DIR}/{quadlet}",
            mode="644",
            **unit_vars,
        )
    systemd.daemon_reload(name="daemon-reload (generate quadlet units)")

    # Quadlet units reject `systemctl enable` — boot start comes from the
    # [Install] section in the quadlet files, so never pass enabled= here.
    # Notify=healthy in the db quadlet makes start block until pg_isready passes.
    systemd.service(
        name="volunteerdb-db running",
        service=f"{siteconf.DB_CONTAINER}.service",
        running=True,
    )

    one_shot = (
        f"podman run --rm --network {siteconf.NET} "
        f"--env-file {siteconf.ENV_FILE} {siteconf.IMAGE}"
    )
    server.shell(
        name="alembic upgrade head (one-shot container)",
        commands=[f"{one_shot} alembic upgrade head"],
    )
    if admin_password:
        # Bare -e flags pass the values through from the op's _env — the
        # password never appears in the command line or pyinfra logs.
        server.shell(
            name="Ensure admin user (one-shot container)",
            commands=[
                f"podman run --rm --network {siteconf.NET} "
                f"--env-file {siteconf.ENV_FILE} "
                f"-e VDB_ADMIN_EMAIL -e VDB_ADMIN_PASSWORD {siteconf.IMAGE} "
                "python -m volunteerdb.admin_bootstrap"
            ],
            _env={
                "VDB_ADMIN_EMAIL": site.mail_admin_email,
                "VDB_ADMIN_PASSWORD": admin_password,
            },
        )
    else:
        print("NOTE: VDB_ADMIN_PASSWORD not set - skipping admin bootstrap.")

    systemd.service(
        name="volunteerdb-app running (restart onto fresh image)",
        service="volunteerdb-app.service",
        running=True,
        restarted=True,
    )
    server.shell(
        name="Wait for app + smoke test /login",
        commands=[
            "for i in $(seq 1 30); do "
            f'c=$(curl -s -o /dev/null -w "%{{http_code}}" '
            f"http://127.0.0.1:{site.host_listen_port}/login); "
            '[ "$c" = "200" ] && exit 0; sleep 1; done; '
            'echo "app did not serve /login with 200"; exit 1'
        ],
    )
    server.shell(
        name="Prune dangling images (bound build-layer growth)",
        commands=["podman image prune -f"],
    )
