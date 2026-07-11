from videoroll.db.models import Account, PublishJob


def test_account_exposes_non_secret_check_columns() -> None:
    columns = Account.__table__.c
    assert "check_state" in columns
    assert "last_checked_at" in columns
    assert "last_check_message" in columns


def test_publish_job_exposes_execution_timestamps() -> None:
    columns = PublishJob.__table__.c
    assert "started_at" in columns
    assert "finished_at" in columns
