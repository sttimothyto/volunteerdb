"""The deploy in four ordered steps.

Each is a @deploy-decorated function that deploy.py imports once and *calls*.
That distinction is load-bearing, and the reason this package does not simply
declare operations at module level:

pyinfra runs the deploy file once per host (`pyinfra_cli.util.load_deploy_file`
spawns a greenlet per host and `exec`s the file in each). An imported module's
body, by contrast, runs only during the *first* host's exec — `sys.modules`
caches it — so operations declared there would silently never be created for
hosts 2..n. With one host that is invisible. It would surface the day a second
parish joined the inventory, which is exactly what this repository is being
reshaped to allow.

Calling a @deploy function sidesteps it: the call happens inside each host's
exec, so the operations are declared once per host. It also groups the
operations under a named heading in pyinfra's output.

For the same reason, site configuration is passed in as arguments. A
module-level `site = siteconf.load()` in one of these modules would be
evaluated during the first host's import and reused thereafter — the same bug
wearing a different hat.
"""
