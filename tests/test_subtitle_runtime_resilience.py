from __future__ import annotations

from pathlib import Path

import yaml

from videoroll.apps.subtitle_service import worker
from videoroll.apps.subtitle_service.worker_concurrency import JobLeaseHeartbeat


ROOT = Path(__file__).resolve().parents[1]


def test_subtitle_job_lease_ttl_defaults_to_five_minutes(monkeypatch) -> None:
    monkeypatch.delenv("HATCHET_SUBTITLE_JOB_LEASE_TTL_SECONDS", raising=False)
    assert worker._subtitle_job_lease_ttl_seconds() == 300


def test_subtitle_job_lease_ttl_is_bounded(monkeypatch) -> None:
    monkeypatch.setenv("HATCHET_SUBTITLE_JOB_LEASE_TTL_SECONDS", "5")
    assert worker._subtitle_job_lease_ttl_seconds() == 60
    monkeypatch.setenv("HATCHET_SUBTITLE_JOB_LEASE_TTL_SECONDS", "99999")
    assert worker._subtitle_job_lease_ttl_seconds() == 3600


def test_application_services_use_cloudflare_dns() -> None:
    for relative in ("docker-compose.yml", "compose.yml", "fromprod/docker-compose.yml"):
        compose = yaml.safe_load((ROOT / relative).read_text(encoding="utf-8"))
        assert compose["x-application-security"]["dns"] == ["1.1.1.1"]


def test_subtitle_environment_exposes_lease_ttl() -> None:
    expected = "${HATCHET_SUBTITLE_JOB_LEASE_TTL_SECONDS:-300}"
    for relative in ("docker-compose.yml", "compose.yml", "fromprod/docker-compose.yml"):
        compose = yaml.safe_load((ROOT / relative).read_text(encoding="utf-8"))
        assert compose["x-subtitle-environment"]["HATCHET_SUBTITLE_JOB_LEASE_TTL_SECONDS"] == expected


def test_lease_heartbeat_interval_is_capped_at_thirty_seconds(monkeypatch) -> None:
    heartbeat = JobLeaseHeartbeat(
        lambda: None,
        "00000000-0000-0000-0000-000000000001",
        "worker",
        300,
    )
    waits: list[float] = []

    def stop_after_first_wait(interval: float) -> bool:
        waits.append(interval)
        return True

    monkeypatch.setattr(heartbeat._stop, "wait", stop_after_first_wait)
    heartbeat._run()
    assert waits == [30.0]
