"""The reverse proxy: Caddy, installed and owned by the deploy when the site
file says so.

Optional on purpose. A parish whose server already runs another proxy — or
other sites on a Caddy it would rather keep by hand — sets [proxy] caddy =
false and terminates TLS itself from deploy/examples/. With caddy = true the
deploy owns /etc/caddy/Caddyfile outright: a hand-written one is copied aside
once, and anything else Caddy should serve goes into [proxy] extra.

Order matters twice. The candidate file is validated BEFORE it replaces the
live one, so a typo in `extra` stops the deploy with the old configuration
still on disk and still being served. And the step runs after the application,
so Caddy's first reload proxies to something that answers, and before the
backup step, whose rclone assertion is the documented first-run failure.

Reload, never restart: Caddy keeps its certificate cache across a reload, so a
deploy costs no issuance and no downtime. It is unconditional, like the app
restart in steps/app.py.
"""

import siteconf
from pyinfra.api.deploy import deploy
from pyinfra.operations import apt, files, server, systemd

# Rendered here, validated, and only then installed over the live file.
CANDIDATE = f"{siteconf.CADDYFILE}.volunteerdb-next"


@deploy("Reverse proxy (Caddy)")
def deploy_proxy(site, *, here) -> None:
    if not site.proxy_caddy:
        print(
            "NOTE: [proxy] caddy = false - TLS and the reverse proxy are yours. "
            "deploy/examples/Caddyfile and deploy/examples/nginx.conf are the "
            "blocks to copy."
        )
        return

    domain, ip = site.host_domain, site.host_public_ip
    # Before anything else: a name that does not resolve here yet would send
    # Caddy into Let's Encrypt's failed-validation backoff. Fails with the fix.
    server.shell(
        name=f"Assert {domain} resolves to {ip}",
        commands=[
            f"getent ahosts {domain} | grep -qwF {ip} || {{ "
            f'echo "ERROR: {domain} does not resolve to {ip} ([host] public_ip) '
            "from this server. Add the A record (docs/how-to/new-instance.md, DNS), "
            "or set [proxy] caddy = false if the name is fronted by a CDN or the "
            'host sits behind NAT."; exit 1; }'
        ],
    )

    # Caddy from its own apt repository: Debian stable ships a 2022 release.
    # gpg is priority optional, so a minimal image may lack it.
    apt.packages(name="gpg (dearmors the Caddy signing key)", packages=["gpg"])
    # Guarded by hand: apt.key re-downloads and re-dearmors on every run.
    server.shell(
        name="Caddy apt signing key (once)",
        commands=[
            f"test -s {siteconf.CADDY_KEYRING} || curl -1sLf {siteconf.CADDY_KEY_URL} "
            f"| gpg --batch --dearmor -o {siteconf.CADDY_KEYRING}"
        ],
    )
    files.put(
        name="Caddy apt source",
        src=str(here / "files" / "caddy-stable.list"),
        dest=siteconf.CADDY_APT_LIST,
        mode="644",
        user="root",
        group="root",
    )
    # No cache_time: install_base's update just touched the stamp file, and a
    # cache_time here would skip the very update the new source needs.
    apt.packages(name="Install caddy", packages=["caddy"], update=True)

    files.template(
        name="Render candidate Caddyfile",
        src=str(here / "templates" / "Caddyfile.j2"),
        dest=CANDIDATE,
        mode="644",
        user="root",
        group="root",
        marker=siteconf.MANAGED_MARKER,
        site_name=site.site_name,
        domain=domain,
        listen_port=site.host_listen_port,
        extra=site.proxy_extra,
    )
    live = siteconf.CADDYFILE
    server.shell(
        name="Validate the candidate; snapshot a hand-written Caddyfile once; install",
        commands=[
            f"caddy validate --config {CANDIDATE} --adapter caddyfile >/dev/null",
            f"if test -f {live} && ! grep -qF '{siteconf.MANAGED_MARKER}' {live}; "
            f"then cp -a {live} {live}.bak-pre-managed-$(date +%F); fi",
            f"cmp -s {CANDIDATE} {live} || install -m 644 -o root -g root {CANDIDATE} {live}",
        ],
    )
    # Only where firewalld is installed AND running; a plain no-op elsewhere.
    server.shell(
        name="Open http/https in firewalld (only if firewalld runs)",
        commands=[
            "if command -v firewall-cmd >/dev/null 2>&1 "
            "&& firewall-cmd --state >/dev/null 2>&1; then "
            "changed=0; for s in http https; do "
            "firewall-cmd -q --permanent --query-service=$s || "
            "{ firewall-cmd -q --permanent --add-service=$s; changed=1; }; done; "
            '[ "$changed" = 0 ] || firewall-cmd -q --reload; fi'
        ],
    )
    systemd.service(
        name="caddy running, enabled, reloaded",
        service="caddy",
        running=True,
        enabled=True,
        reloaded=True,
    )
    server.shell(
        name=f"Smoke test https://{domain}/login",
        commands=[
            "for i in $(seq 1 60); do "
            f'c=$(curl -s --max-time 10 -o /dev/null -w "%{{http_code}}" '
            f"https://{domain}/login); "
            '[ "$c" = "200" ] && exit 0; sleep 2; done; '
            f'echo "https://{domain}/login did not answer 200 within 120s - '
            'journalctl -u caddy -n 50"; exit 1'
        ],
    )
