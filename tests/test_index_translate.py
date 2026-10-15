"""No remote traffic: verify the isolated translator contract and provider separation."""
from __future__ import annotations

import json

import pytest

from videoroll.apps.subtitle_service import index_translate
from videoroll.apps.subtitle_service.processing import Segment


@pytest.fixture
def source() -> list[Segment]:
    return [
        Segment(0.0, 1.5, "The reset button restarts the device."),
        Segment(1.5, 3.0, "This PCB controls the clock."),
        Segment(3.0, 5.0, "I2C connects the display."),
    ]


def test_index_uses_full_video_context_but_only_translates_requested_blocks(monkeypatch, source):
    calls = []

    def fake_call(messages, _settings, *, expect_json):
        assert expect_json
        request = messages[0]["content"]
        calls.append(request)
        assert all(f"[{i+1}] {item.text}" in request for i, item in enumerate(source))
        assert "【软性要求】" in request and "【硬性要求】" in request
        assert "PCB→印刷电路板" in request
        items = json.loads(request.split("【待翻译字幕块（只翻译这里）】\n", 1)[1])
        return json.dumps({"translations": [
            {"idx": row["idx"], "text": "中文-" + row["text"]} for row in items
        ]})

    monkeypatch.setattr(index_translate, "_call", fake_call)
    checkpoints = []
    got = index_translate.translate_subtitles_index(
        source, target_lang="zh", style="口语自然",
        settings={"index_glossary": "PCB→印刷电路板", "index_context_chars": 8000},
        batch_size=2, on_batch_done=lambda rows: checkpoints.append(list(rows)),
    )
    assert len(calls) == 2
    assert all(len(row.text) > 0 and row.text.startswith("中文-") for row in got)
    assert [len(batch) for batch in checkpoints] == [2, 3]
    assert [(seg.start, seg.end) for seg in got] == [(seg.start, seg.end) for seg in source]


def test_bad_batch_splits_instead_of_silently_dropping_lines(monkeypatch, source):
    observed = []

    def fake_call(messages, *_args, **kwargs):
        rows = json.loads(messages[0]["content"].split("【待翻译字幕块（只翻译这里）】\n")[1])
        observed.append(len(rows))
        if len(rows) > 1:
            return '{"translations": []}'
        return json.dumps({"translations": [{"idx": rows[0]["idx"], "text": "正确译文"}]})

    monkeypatch.setattr(index_translate, "_call", fake_call)
    got = index_translate.translate_subtitles_index(
        source, target_lang="zh", style="口语自然", settings={}, batch_size=3,
    )
    assert observed[0] == 3
    assert all(seg.text == "正确译文" for seg in got)


def test_index_rejects_nonmatching_single_line(monkeypatch, source):
    monkeypatch.setattr(index_translate, "_call", lambda *_args, **_kwargs: '{"translations": []}')
    with pytest.raises(RuntimeError, match="invalid translation at line"):
        index_translate.translate_subtitles_index(source[:1], target_lang="zh", style="口语自然", settings={}, batch_size=1)


def test_full_context_has_bounded_long_video_fallback(source):
    whole, full = index_translate._bounded_context(source, start=0, limit=4000)
    assert full and whole.count("[") == 3
    long = [Segment(float(i), float(i + 1), "word " * 50) for i in range(500)]
    clip, complete = index_translate._bounded_context(long, start=200, limit=8000)
    assert not complete
    assert "【当前段落前后】" in clip and "【视频结尾】" in clip
    assert len(clip) <= 8500


def test_selective_index_translates_only_selected_rows(monkeypatch, source):
    translated = [Segment(s.start, s.end, "已有译文") for s in source]
    def fake_call(messages, *_a, **_kw):
        rows = json.loads(messages[0]["content"].split("【待翻译字幕块（只翻译这里）】\n")[1])
        return json.dumps({"translations": [{"idx": rows[0]["idx"], "text": "修复译文"}]})
    monkeypatch.setattr(index_translate, "_call", fake_call)
    got = index_translate.translate_subtitles_index(
        source, target_lang="zh", style="口语自然", settings={},
        batch_size=3, base_translations=translated, selected_indices=[2],
    )
    assert [s.text for s in got] == ["已有译文", "修复译文", "已有译文"]


def test_index_title_isolated_from_generic_llm(monkeypatch):
    calls = []
    def fake_call(messages, cfg, *, expect_json):
        calls.append(messages)
        assert expect_json
        assert "PCB" in messages[1]["content"]
        return '{"translation":"制作一个新的 PCB 时钟"}'
    monkeypatch.setattr(index_translate, "_call", fake_call)
    result = index_translate.translate_title_index(
        "Building a New PCB Clock", target_lang="zh", style="口语自然",
        settings={}, context="Video about an I2C clock.",
    )
    assert result == "制作一个新的 PCB 时钟"
    assert len(calls) == 1


def test_index_stage_bypasses_rag_even_if_enabled(monkeypatch):
    from videoroll.apps.subtitle_service import translation_stage as stage
    source = [Segment(0.0, 1.0, "Hello")]
    translated = [Segment(0.0, 1.0, "你好")]
    events: list[str] = []

    class DB:
        def commit(self):
            events.append("transaction_committed")

    class Checkpoint:
        def load_with_context(self, _source, *, source_segments_key, provider):
            assert provider == "index_translate"
            return [], "", {}
        def save(self, _key, completed, *, summary, provider):
            assert provider == "index_translate"
            events.append(f"save:{len(completed)}")
        def clear(self):
            events.append("clear")

    def fake_index(rows, **kwargs):
        assert kwargs["settings"]["rag_enabled"] is True
        assert kwargs["batch_size"] == 12
        kwargs["on_batch_done"](translated)
        return translated

    monkeypatch.setattr(index_translate, "translate_subtitles_index", fake_index)
    monkeypatch.setattr(stage, "build_rag_context", lambda *_a, **_kw: (_ for _ in ()).throw(AssertionError("RAG invoked")))
    result = stage.run_translation_stage(
        db=DB(), task_id="task", subtitle_job_id="job",
        segments=source, source_segments_key="source.json",
        translate_cfg={"enabled": True, "provider": "index_translate", "target_lang": "zh"},
        checkpoint=Checkpoint(), trace=object(), retry_attempt=0,
        database_url="unused",
        fresh_translate_settings=lambda: {"default_style": "口语自然", "default_batch_size": 50,
                                           "default_enable_summary": True, "default_max_retries": 2,
                                           "rag_enabled": True, "index_batch_size": 12},
        ai_service_factory=lambda: object(), log=lambda msg: events.append(msg),
    )
    assert result.segments == translated
    assert result.provider == "index_translate"
    assert "save:1" in events and "clear" in events


def test_index_and_openai_checkpoints_are_isolated(tmp_path):
    import uuid
    from pathlib import Path
    from videoroll.apps.subtitle_service.translation_checkpoint import TranslationCheckpointStore

    class MemoryStore:
        payload = b""
        def upload_file(self, source: Path, key: str, *, content_type=None):
            self.payload = source.read_bytes()
        def download_file(self, key: str, destination: Path):
            destination.write_bytes(self.payload)
        def delete_object(self, key: str):
            self.payload = b""

    store = MemoryStore()
    checkpoint = TranslationCheckpointStore(
        store=store, task_id=uuid.uuid4(), local_path=tmp_path / "checkpoint.json")
    source = [Segment(0, 1, "Hello")]
    translations = [Segment(0, 1, "你好")]
    checkpoint.save("source.json", translations, summary="old", provider="index_translate")
    assert checkpoint.load(source, source_segments_key="source.json") == ([], "")
    assert checkpoint.load_with_context(
        source, source_segments_key="source.json", provider="index_translate")[0] == translations
    checkpoint.save("source.json", translations, summary="old", provider="openai")
    assert checkpoint.load_with_context(
        source, source_segments_key="source.json", provider="index_translate")[0] == []
