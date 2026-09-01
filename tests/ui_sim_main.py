"""NiceGUI main file for headless user-simulation tests (not a test module).

Run via ``nicegui.testing.user_simulation(main_file=...)``, which executes this
inside its reset context so the @ui.page routes register against the fresh app.
"""

from nicegui import ui

from volunteerdb import env as env_mod
from volunteerdb.main import create_app
from volunteerdb.ui.context import establish_session

from tests import conftest
from tests.fakes import SIM_CLOCK, SIM_MAILER

# the real Env over the test engine, except that its mail is recorded and its
# clock is one a test can move
create_app(env_mod.build(engine=conftest.ENGINE, mailer=SIM_MAILER, clock=SIM_CLOCK))


@ui.page("/login-dev/{user_id}")
def dev_login(user_id: int, method: str = "password") -> None:
    # `method` mirrors the real sign-in paths ("password", "otp", "invite"):
    # /account asks a password session to re-type its password and lets an
    # emailed-code session set a new one without it.
    establish_session(user_id, remember=True, method=method)
    ui.label("dev-login ok")


ui.run(storage_secret="test secret")
