import uuid

from videoroll.apps.social_publisher.runtime import social_lock_key
from videoroll.apps.social_publisher.sau_cli import SauCommandResult
from videoroll.apps.social_publisher import worker
from videoroll.apps.social_publisher.worker import classify_execution_result
from videoroll.db.models import PublishState


def test_lock_key_is_scoped_by_platform_and_account() -> None:
    account_id = uuid.uuid4()
    assert social_lock_key("douyin", account_id) == f"videoroll:social-publish:douyin:{account_id}"


def test_execution_result_is_conservative() -> None:
    assert classify_execution_result(returncode=0, timed_out=False) == PublishState.submitted
    assert classify_execution_result(returncode=1, timed_out=False) == PublishState.unknown
    assert classify_execution_result(returncode=-9, timed_out=True) == PublishState.unknown


def test_account_check_message_includes_command_diagnostics() -> None:
    result = SauCommandResult(returncode=1, stdout="invalid\n", stderr="browser launch failed")
    assert hasattr(worker, "account_check_message")
    message = worker.account_check_message(result)
    assert "exit=1" in message
    assert "browser launch failed" in message


def test_account_check_message_marks_timeout() -> None:
    result = SauCommandResult(returncode=-15, stdout="", stderr="", timed_out=True)
    assert hasattr(worker, "account_check_message")
    assert worker.account_check_message(result) == "SAU check timed out (exit=-15)"
