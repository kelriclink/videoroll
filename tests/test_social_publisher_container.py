from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


def test_social_publisher_image_provides_chrome_compatibility_and_novnc() -> None:
    dockerfile = (ROOT / "docker" / "social-publisher.Dockerfile").read_text(encoding="utf-8")
    assert "/opt/google/chrome/chrome" in dockerfile
    assert "x11vnc" in dockerfile
    assert "novnc" in dockerfile
    assert (ROOT / "docker" / "social-publisher-entrypoint.sh").exists()


def test_worker_starts_display_stack_and_web_proxies_its_novnc_desktop() -> None:
    dockerfile = (ROOT / "docker" / "social-publisher.Dockerfile").read_text(encoding="utf-8")
    entrypoint = (ROOT / "docker" / "social-publisher-entrypoint.sh").read_text(encoding="utf-8")
    nginx = (ROOT / "src" / "web" / "nginx.conf").read_text(encoding="utf-8")

    assert 'ENTRYPOINT ["/app/docker/social-publisher-entrypoint.sh"]' in dockerfile
    assert 'exec "$@"' in entrypoint
    assert "location /social-publish/" in nginx
    assert "proxy_pass http://social-publisher-worker:6080/;" in nginx


def test_compose_passes_douyin_headless_cookie_check_setting() -> None:
    for relative_path in ("compose.yml", "docker-compose.yml", "fromprod/docker-compose.yml"):
        compose = (ROOT / relative_path).read_text(encoding="utf-8")
        assert "DOUYIN_COOKIE_AUTH_HEADLESS" in compose
