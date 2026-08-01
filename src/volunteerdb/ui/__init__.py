def register_pages() -> None:
    """Importing the page modules registers their @ui.page routes."""
    from . import (  # noqa: F401
        admin_page,
        dashboard,
        fields_admin_page,
        imports_page,
        login,
        photos_route,
        planning_page,
        teams_page,
        volunteers_page,
        workload_admin_page,
    )
