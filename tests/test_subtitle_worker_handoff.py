from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


def test_render_handoff_completes_the_subtitle_job() -> None:
    source = (ROOT / "src" / "videoroll" / "apps" / "subtitle_service" / "worker.py").read_text(
        encoding="utf-8"
    )

    # Stop/resume is allowed to requeue a job. Only render handoff must complete
    # it, otherwise that subtitle job keeps occupying the task's queue slot.
    handoffs = source.split('_safe_append_log_line(log_path, "render queued; waiting for task queue")')[:-1]
    assert len(handoffs) == 2
    for handoff in handoffs:
        transition = handoff[handoff.rfind("db.add(RenderJob(") :]
        assert "job.status = SubtitleJobStatus.queued" not in transition
        assert "job.status = SubtitleJobStatus.succeeded" in transition
    assert source.count("job.status = SubtitleJobStatus.succeeded") >= 4


def test_render_handoff_commits_render_and_subtitle_completion_together() -> None:
    source = (ROOT / "src" / "videoroll" / "apps" / "subtitle_service" / "worker.py").read_text(
        encoding="utf-8"
    )

    handoffs = source.split('_safe_append_log_line(log_path, "render queued; waiting for task queue")')[:-1]
    assert len(handoffs) == 2
    for handoff in handoffs:
        transition = handoff[handoff.rfind("db.add(RenderJob(") :]
        assert transition.index("job.status = SubtitleJobStatus.succeeded") < transition.index("db.commit()")
