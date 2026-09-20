from __future__ import annotations

from types import SimpleNamespace

from videoroll.apps.subtitle_service.processing import Segment, translate_segments_openai_with_summary
from videoroll.apps.subtitle_service.translation_memory import (
    recall_translation_examples,
    remember_translation_pairs,
)
from videoroll.apps.subtitle_service.translation_quality import (
    blocking_translation_issues,
    build_translation_plan,
    validate_translation_mapping,
)


def test_translation_plan_distinguishes_hard_preferred_and_contextual() -> None:
    blocks = [
        {"idx": 1, "text": "Check POST and the Linux kernel."},
        {"idx": 2, "text": "The VGA light is red."},
    ]
    plan = build_translation_plan(
        blocks=blocks,
        glossary={"POST": "开机自检"},
        rag_context={
            "term_cards": [
                {
                    "term": "kernel",
                    "translation": "内核",
                    "description": "Linux operating-system kernel",
                    "status": "context_only",
                    "confidence": 0.91,
                },
                {
                    "term": "VGA",
                    "translation": "VGA",
                    "description": "graphics-related debug indicator",
                    "confidence": 0.98,
                },
            ],
            "translation_examples": [
                {
                    "source": "The VGA light stays on.",
                    "target": "VGA 指示灯一直亮着。",
                    "similarity": 0.91,
                    "scope": "same_task",
                    "applies_to_blocks": [2],
                }
            ],
        },
    )

    modes = {(item["source"], item["mode"]): item for item in plan["constraints"]}
    assert modes[("POST", "hard")]["applies_to_blocks"] == [1]
    assert modes[("kernel", "contextual")]["applies_to_blocks"] == [1]
    assert modes[("VGA", "preferred")]["applies_to_blocks"] == [2]
    assert plan["translation_examples"][0]["applies_to_blocks"] == [2]


def test_translation_validator_flags_hard_term_and_changed_numbers() -> None:
    blocks = [{"idx": 1, "text": "POST reports 450W on H264."}]
    plan = build_translation_plan(
        blocks=blocks,
        glossary={"POST": "开机自检"},
    )
    issues = validate_translation_mapping(
        blocks=blocks,
        translations={1: "邮件显示 4500W，H264。"},
        translation_plan=plan,
    )

    assert {issue["type"] for issue in issues} == {"hard_term_missing", "number_mismatch"}
    assert len(blocking_translation_issues(issues)) == 2


def test_translation_validator_accepts_decimal_separator_change() -> None:
    issues = validate_translation_mapping(
        blocks=[{"idx": 1, "text": "Set it to 1.5 volts."}],
        translations={1: "将它设为 1,5 伏。"},
        translation_plan={},
    )
    assert issues == []


def test_preferred_term_is_repairable_but_not_blocking() -> None:
    blocks = [{"idx": 1, "text": "Use the AWP here."}]
    plan = build_translation_plan(
        blocks=blocks,
        rag_context={"term_cards": [{"term": "AWP", "translation": "AWP 狙击枪"}]},
    )
    issues = validate_translation_mapping(
        blocks=blocks,
        translations={1: "这里使用这把武器。"},
        translation_plan=plan,
    )
    assert [issue["type"] for issue in issues] == ["preferred_term_missing"]
    assert blocking_translation_issues(issues) == []


def test_translation_pipeline_repairs_only_failed_block() -> None:
    calls: list[tuple[str, object]] = []

    class FakeAIService:
        def translate_subtitle_batch(self, **kwargs: object) -> dict[str, object]:
            calls.append(("translate", kwargs))
            return {
                "updated_summary": "hardware",
                "translations": [
                    {"idx": 1, "text": "邮件显示 4500W。"},
                    {"idx": 2, "text": "第二行正常。"},
                ],
            }

        def repair_subtitle_batch(self, **kwargs: object) -> dict[str, object]:
            calls.append(("repair", kwargs))
            source_blocks = kwargs["source_blocks"]
            assert source_blocks == [{"idx": 1, "text": "POST reports 450W."}]
            return {"translations": [{"idx": 1, "text": "开机自检显示 450W。"}]}

    translated, summary = translate_segments_openai_with_summary(
        [
            Segment(start=0.0, end=1.0, text="POST reports 450W."),
            Segment(start=1.0, end=2.0, text="Second line."),
        ],
        target_lang="zh",
        style="自然",
        batch_size=2,
        glossary={"POST": "开机自检"},
        ai_service=FakeAIService(),  # type: ignore[arg-type]
    )

    assert [segment.text for segment in translated] == ["开机自检显示 450W。", "第二行正常。"]
    assert summary == "hardware"
    assert [name for name, _payload in calls] == ["translate", "repair"]


def test_tm_examples_move_into_translation_plan_without_duplicate_raw_payload() -> None:
    captured: dict[str, object] = {}

    class FakeAIService:
        def translate_subtitle_batch(self, **kwargs: object) -> dict[str, object]:
            captured.update(kwargs)
            return {"updated_summary": "", "translations": [{"idx": 1, "text": "VGA 指示灯亮着。"}]}

    translated, _summary = translate_segments_openai_with_summary(
        [Segment(start=0.0, end=1.0, text="The VGA light is on.")],
        target_lang="zh",
        style="自然",
        batch_size=1,
        ai_service=FakeAIService(),  # type: ignore[arg-type]
        rag_context_provider=lambda _batch, _start, _summary: {
            "knowledge_cards": [{"title": "VGA", "content": "graphics debug indicator"}],
            "translation_examples": [
                {
                    "source": "The VGA light stays on.",
                    "target": "VGA 指示灯一直亮着。",
                    "similarity": 0.9,
                    "scope": "same_task",
                    "applies_to_blocks": [1],
                }
            ],
        },
    )

    assert translated[0].text == "VGA 指示灯亮着。"
    rag_context = captured["rag_context"]
    assert isinstance(rag_context, dict)
    assert "translation_examples" not in rag_context
    plan = captured["translation_plan"]
    assert isinstance(plan, dict)
    assert plan["translation_examples"][0]["target"] == "VGA 指示灯一直亮着。"


class _FakeRows:
    def __init__(self, rows: list[object]) -> None:
        self._rows = rows

    def all(self) -> list[object]:
        return self._rows


class _FakeMemoryDB:
    def __init__(self, rows: list[object] | None = None) -> None:
        self.rows = rows or []
        self.executions: list[tuple[object, dict[str, object] | None]] = []

    def execute(self, statement: object, params: dict[str, object] | None = None) -> _FakeRows:
        self.executions.append((statement, params))
        return _FakeRows(self.rows)


def test_translation_memory_recall_prefers_similar_same_task_example() -> None:
    rows = [
        SimpleNamespace(
            _mapping={
                "id": "1",
                "source_text": "The VGA light stays on.",
                "source_norm": "the vga light stays on.",
                "target_text": "VGA 指示灯一直亮着。",
                "domain": "hardware",
                "task_id": "same",
                "status": "machine",
                "same_task": 1,
            }
        ),
        SimpleNamespace(
            _mapping={
                "id": "2",
                "source_text": "Completely unrelated sentence.",
                "source_norm": "completely unrelated sentence.",
                "target_text": "无关句子。",
                "domain": "hardware",
                "task_id": "same",
                "status": "machine",
                "same_task": 1,
            }
        ),
    ]
    db = _FakeMemoryDB(rows)

    examples = recall_translation_examples(
        db,  # type: ignore[arg-type]
        source_segments=[Segment(start=0, end=1, text="The VGA light is staying on.")],
        start_idx=10,
        target_lang="zh",
        task_id=None,
        domain="hardware",
    )

    assert len(examples) == 1
    assert examples[0]["target"] == "VGA 指示灯一直亮着。"
    assert examples[0]["applies_to_blocks"] == [11]


def test_translation_memory_write_uses_one_upsert_per_pair() -> None:
    db = _FakeMemoryDB()
    written = remember_translation_pairs(
        db,  # type: ignore[arg-type]
        source_segments=[
            Segment(start=0, end=1, text="one"),
            Segment(start=1, end=2, text="two"),
        ],
        translated_segments=[
            Segment(start=0, end=1, text="一"),
            Segment(start=1, end=2, text="二"),
        ],
        target_lang="zh",
        task_id=None,
        subtitle_job_id=None,
        domain="test",
    )

    assert written == 2
    assert len(db.executions) == 2
    assert db.executions[0][1]["source_norm"] == "one"
    assert db.executions[1][1]["target_text"] == "二"
