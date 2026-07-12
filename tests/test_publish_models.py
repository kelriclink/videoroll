import uuid

from videoroll.apps.bilibili_publisher.schemas import PublishResponse
from videoroll.apps.social_publisher.schemas import SocialPublishRequest
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


def test_publish_requests_and_responses_carry_batch_and_job_identifiers() -> None:
    batch_id = uuid.uuid4()
    payload = SocialPublishRequest.model_validate(
        {
            "platform": "douyin",
            "task_id": str(uuid.uuid4()),
            "account_id": str(uuid.uuid4()),
            "batch_id": str(batch_id),
            "video": {"type": "s3", "key": "final.mp4"},
            "meta": {"title": "title"},
        }
    )
    response = PublishResponse.model_validate({"job_id": str(uuid.uuid4()), "state": "submitting"})

    assert payload.batch_id == batch_id
    assert response.job_id is not None
