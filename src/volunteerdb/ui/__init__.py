def register_pages() -> None:
    """Importing the page modules registers their @ui.page routes."""
    from . import (  # noqa: F401
        account_page,
        admin_page,
        calendar_routes,
        dashboard,
        elections_page,
        events_page,
        fields_admin_page,
        login,
        logo_route,
        ministries_routes,
        photos_route,
        team_files_route,
        teams_page,
        volunteers_page,
        workload_admin_page,
    )

    # raw (non-ui.page) routes must re-register on every create_app(): the
    # test harness wipes app routes between simulations while this module
    # stays cached, so import-time decorators would fire only once
    ministries_routes.register()
    logo_route.register()
    calendar_routes.register()
    team_files_route.register()
