from __future__ import annotations

import json
import uuid
from types import SimpleNamespace

import pytest

from videoroll.apps.subtitle_service import translation_postprocess
from videoroll.apps.subtitle_service.processing import Segment
from videoroll.apps.subtitle_service.translation_postprocess import (
    merge_bilingual_segments,
    postprocess_translation,
)


def test_merge_bilingual_segments_preserves_translated_timing_and_source_text() -> None:
    source = [
        Segment(start=0.0, end=1.0, text="hello"),
        Segment(start=1.0, end=2.0, text="world"),
    ]
    translated = [
        Segment(start=0.1, end=1.1, text="你好", confidence=0.9),
        Segment(start=1.1, end=2.1, text="世界", confidence=0.8),
    ]

    merged = merge_bilingual_segments(source, translated)

    assert [(item.start, item.end, item.text, item.secondary_text) for item in merged] == [
        (0.1, 1.1, "你好", "hello"),
        (1.1, 2.1, "世界", "world"),
    ]


def test_postprocess_translation_updates_metadata_and_returns_bilingual_segments(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    task_id = uuid.uuid4()
    task = SimpleNamespace(id=task_id)
    writes: list[tuple[bytes, str, str | None]] = []
    title_updates: list[dict[str, object]] = []
    tag_updates: list[dict[str, object]] = []
    logs: list[str] = []

    class FakeStore:
        def put_bytes(self, payload: bytes, key: str, *, content_type: str | None = None) -> None:
            writes.append((payload, key, content_type))

    class FakeAIService:
        def translate_title(self, title: str, **kwargs: object) -> str:
            assert title == "English title"
            assert kwargs["target_lang"] == "zh"
            return "中文标题"

        def generate_bilibili_tags(self, **_kwargs: object) -> list[str]:
            return ["tag1", "tag2", "tag3", "tag4", "tag5", "tag6"]

    monkeypatch.setattr(
        translation_postprocess,
        "latest_youtube_title",
        lambda _db, _store, _task_id: "English title",
    )
    monkeypatch.setattr(
        translation_postprocess,
        "set_task_titles",
        lambda _db, task_id_value, **kwargs: title_updates.append(
            {"task_id": task_id_value, **kwargs}
        ),
    )
    monkeypatch.setattr(
        translation_postprocess,
        "set_task_bilibili_tags",
        lambda _db, task_id_value, **kwargs: tag_updates.append(
            {"task_id": task_id_value, **kwargs}
        ),
    )
    monkeypatch.setattr(
        translation_postprocess,
        "build_task_publish_meta_draft",
        lambda _task, **_kwargs: {"title": "中文标题", "summary": "summary"},
    )

    source = [Segment(start=0.0, end=1.0, text="hello")]
    translated = [Segment(start=0.0, end=1.0, text="你好")]

    result = postprocess_translation(
        db=object(),  # type: ignore[arg-type]
        store=FakeStore(),  # type: ignore[arg-type]
        task=task,  # type: ignore[arg-type]
        request_json={"after_render": {"publish": True}},
        source_segments=source,
        translated_segments=translated,
        provider="openai",
        target_lang="zh",
        style="natural",
        summary="summary",
        bilingual=True,
        ai_service=FakeAIService(),  # type: ignore[arg-type]
        log=logs.append,
    )

    assert result[0].text == "你好"
    assert result[0].secondary_text == "hello"
    assert title_updates == [
        {
            "task_id": str(task_id),
            "source_title": "English title",
            "translated_title": "中文标题",
        }
    ]
    assert tag_updates[0]["tags"] == ["tag1", "tag2", "tag3", "tag4", "tag5", "tag6"]
    assert writes[0][1] == f"meta/{task_id}/publish_meta.json"
    assert writes[0][2] == "application/json"
    assert json.loads(writes[0][0].decode("utf-8"))["title"] == "中文标题"
    assert "publish meta refreshed" in logs[0]


def test_title_metadata_failure_rolls_back_and_tag_update_continues(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    task_id = uuid.uuid4()
    task = SimpleNamespace(id=task_id)
    logs: list[str] = []
    tag_updates: list[list[str]] = []

    class FakeDB:
        def __init__(self) -> None:
            self.rollbacks = 0

        def rollback(self) -> None:
            self.rollbacks += 1

    class FakeStore:
        pass

    class FakeAIService:
        def translate_title(self, _title: str, **_kwargs: object) -> str:
            return "中文标题"

        def generate_bilibili_tags(self, **_kwargs: object) -> list[str]:
            return ["tag1", "tag2", "tag3", "tag4", "tag5", "tag6"]

    db = FakeDB()
    monkeypatch.setattr(
        translation_postprocess,
        "latest_youtube_title",
        lambda _db, _store, _task_id: "English title",
    )

    def fail_titles(*_args: object, **_kwargs: object) -> None:
        raise RuntimeError("title commit failed")

    monkeypatch.setattr(translation_postprocess, "set_task_titles", fail_titles)
    monkeypatch.setattr(
        translation_postprocess,
        "set_task_bilibili_tags",
        lambda _db, _task_id, **kwargs: tag_updates.append(list(kwargs["tags"])),
    )

    translated = [Segment(start=0.0, end=1.0, text="你好")]
    result = postprocess_translation(
        db=db,  # type: ignore[arg-type]
        store=FakeStore(),  # type: ignore[arg-type]
        task=task,  # type: ignore[arg-type]
        request_json={},
        source_segments=[Segment(start=0.0, end=1.0, text="hello")],
        translated_segments=translated,
        provider="openai",
        target_lang="zh",
        style="natural",
        summary="summary",
        bilingual=False,
        ai_service=FakeAIService(),  # type: ignore[arg-type]
        log=logs.append,
    )

    assert result is translated
    assert db.rollbacks == 1
    assert tag_updates == [["tag1", "tag2", "tag3", "tag4", "tag5", "tag6"]]
    assert any("title metadata update skipped: RuntimeError: title commit failed" in line for line in logs)


def test_tag_failure_rolls_back_before_publish_meta_refresh(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    task_id = uuid.uuid4()
    task = SimpleNamespace(id=task_id)
    logs: list[str] = []
    writes: list[str] = []

    class FakeDB:
        def __init__(self) -> None:
            self.rollbacks = 0

        def rollback(self) -> None:
            self.rollbacks += 1

    class FakeStore:
        def put_bytes(self, _payload: bytes, key: str, *, content_type: str | None = None) -> None:
            assert content_type == "application/json"
            writes.append(key)

    class FakeAIService:
        def translate_title(self, title: str, **_kwargs: object) -> str:
            return title

        def generate_bilibili_tags(self, **_kwargs: object) -> list[str]:
            return ["tag1", "tag2", "tag3", "tag4", "tag5", "tag6"]

    db = FakeDB()
    monkeypatch.setattr(
        translation_postprocess,
        "latest_youtube_title",
        lambda _db, _store, _task_id: "中文标题",
    )
    monkeypatch.setattr(translation_postprocess, "set_task_titles", lambda *_args, **_kwargs: None)

    def fail_tags(*_args: object, **_kwargs: object) -> None:
        raise RuntimeError("tag commit failed")

    monkeypatch.setattr(translation_postprocess, "set_task_bilibili_tags", fail_tags)
    monkeypatch.setattr(
        translation_postprocess,
        "build_task_publish_meta_draft",
        lambda _task, **_kwargs: {"title": "中文标题"},
    )

    translated = [Segment(start=0.0, end=1.0, text="你好")]
    result = postprocess_translation(
        db=db,  # type: ignore[arg-type]
        store=FakeStore(),  # type: ignore[arg-type]
        task=task,  # type: ignore[arg-type]
        request_json={"after_render": {"publish": True}},
        source_segments=[Segment(start=0.0, end=1.0, text="hello")],
        translated_segments=translated,
        provider="openai",
        target_lang="zh",
        style="natural",
        summary="summary",
        bilingual=False,
        ai_service=FakeAIService(),  # type: ignore[arg-type]
        log=logs.append,
    )

    assert result is translated
    assert db.rollbacks == 1
    assert writes == [f"meta/{task_id}/publish_meta.json"]
    assert any("bilibili tag update skipped: RuntimeError: tag commit failed" in line for line in logs)
    assert any("publish meta refreshed" in line for line in logs)
