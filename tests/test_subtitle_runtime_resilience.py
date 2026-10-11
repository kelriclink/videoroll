from __future__ import annotations

from pathlib import Path
from unittest.mock import Mock

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


def test_asr_stage_ends_settings_transaction_before_provider(monkeypatch, tmp_path) -> None:
    db = Mock()
    db.new = db.dirty = db.deleted = ()
    monkeypatch.setattr(
        worker,
        "get_asr_settings",
        lambda *_args, **_kwargs: {
            "default_engine": "external-whisper",
            "default_language": "auto",
            "default_model": "whisper-1",
            "external_whisper_base_url": "http://whisper.test/v1",
            "external_whisper_api_key": "test",
            "external_whisper_model": "whisper-1",
        },
    )

    def fake_transcribe(*_args, **_kwargs):
        assert db.rollback.called
        return []

    monkeypatch.setattr(worker, "transcribe_external_whisper", fake_transcribe)

    result = worker._run_asr_stage(
        db=db,
        audio_path=tmp_path / "audio.wav",
        audio_key=None,
        asr_cfg={"engine": "auto", "language": "auto"},
        log_path=None,
        groq_checkpoint_path=tmp_path / "groq.json",
    )

    assert result == []
    db.rollback.assert_called_once()


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
