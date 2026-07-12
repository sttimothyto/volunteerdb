"""NiceGUI main file for headless user-simulation tests (not a test module).

Run via ``nicegui.testing.user_simulation(main_file=...)``, which executes this
inside its reset context so the @ui.page routes register against the fresh app.
"""

from nicegui import app, ui

from volunteerdb.main import create_app

create_app()


@ui.page("/login-dev/{user_id}")
def dev_login(user_id: int) -> None:
    app.storage.user["user_id"] = user_id
    ui.label("dev-login ok")


ui.run(storage_secret="test secret")
