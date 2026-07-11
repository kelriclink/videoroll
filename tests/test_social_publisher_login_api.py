from videoroll.apps.social_publisher.main import app


def test_browser_login_session_routes_are_registered() -> None:
    paths = {route.path for route in app.routes}
    assert "/login-sessions/{platform}" in paths
    assert "/login-sessions/{session_id}" in paths
