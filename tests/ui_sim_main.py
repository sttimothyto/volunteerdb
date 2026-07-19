"""NiceGUI main file for headless user-simulation tests (not a test module).

Run via ``nicegui.testing.user_simulation(main_file=...)``, which executes this
inside its reset context so the @ui.page routes register against the fresh app.
"""

from nicegui import ui

from volunteerdb.main import create_app
from volunteerdb.ui.context import establish_session

create_app()


@ui.page("/login-dev/{user_id}")
def dev_login(user_id: int) -> None:
    establish_session(user_id, remember=True)
    ui.label("dev-login ok")


ui.run(storage_secret="test secret")
