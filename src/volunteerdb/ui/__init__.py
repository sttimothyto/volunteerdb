def register_pages() -> None:
    """Importing the page modules registers their @ui.page routes."""
    from . import (  # noqa: F401
        admin_page,
        capacity_admin_page,
        dashboard,
        fields_admin_page,
        graph_page,
        imports_page,
        login,
        photos_route,
        teams_page,
        volunteers_page,
    )
