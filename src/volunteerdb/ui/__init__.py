def register_pages() -> None:
    """Importing the page modules registers their @ui.page routes."""
    from . import admin_page, dashboard, graph_page, imports_page, login, teams_page, volunteers_page  # noqa: F401
