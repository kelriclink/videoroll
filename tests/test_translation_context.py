from __future__ import annotations

from videoroll.apps.subtitle_service.processing import Segment
from videoroll.apps.subtitle_service import processing
from videoroll.apps.subtitle_service.translation_context import (
    build_translation_context_state,
    build_translation_scenes,
    choose_translation_batch_end,
    context_memory_is_empty,
    derive_translation_scene_profile,
    ground_translation_context_update,
    merge_translation_context_memory,
    sanitize_translation_context_memory,
)


def test_context_update_requires_source_evidence_for_durable_entities() -> None:
    grounded = ground_translation_context_update(
        {
            "topic": "hardware",
            "characters": [
                {"name": "Alice", "target_name": "爱丽丝"},
                {"name": "Mallory", "target_name": "马洛里"},
            ],
            "terminology": [
                {"source": "POST", "target": "开机自检"},
                {"source": "phantom-term", "target": "幻觉术语"},
            ],
            "ambiguities": [
                {"term": "rail", "resolution": "power rail"},
                {"term": "ghost", "resolution": "invented"},
            ],
        },
        source_texts=["Alice checks POST on the 12V rail."],
    )

    assert grounded["topic"] == "hardware"
    assert [item["name"] for item in grounded["characters"]] == ["Alice"]
    assert [item["source"] for item in grounded["terminology"]] == ["POST"]
    assert [item["term"] for item in grounded["ambiguities"]] == ["rail"]


def test_context_grounding_uses_token_boundaries_for_ascii_terms() -> None:
    grounded = ground_translation_context_update(
        {"terminology": [{"source": "AI", "target": "人工智能"}]},
        source_texts=["He said this already."],
    )
    assert "terminology" not in grounded


def test_context_memory_merges_stable_entities_and_replaces_updates() -> None:
    memory = merge_translation_context_memory(
        {},
        {
            "topic": "Server troubleshooting",
            "scene_summary": "They inspect the power supply.",
            "characters": [
                {
                    "name": "Alice",
                    "target_name": "爱丽丝",
                    "aliases": ["A"],
                    "role": "engineer",
                }
            ],
            "terminology": [
                {"source": "PSU", "target": "电源", "meaning": "power supply unit"}
            ],
            "ambiguities": [
                {"term": "rail", "resolution": "power rail, not railway"}
            ],
        },
        scene_id=2,
    )
    memory = merge_translation_context_memory(
        memory,
        {
            "characters": [
                {
                    "name": "Alice",
                    "target_name": "爱丽丝",
                    "notes": "Lead engineer",
                }
            ],
            "terminology": [
                {"source": "PSU", "target": "电源模块", "meaning": "server PSU"}
            ],
        },
        scene_id=2,
    )

    assert memory["topic"] == "Server troubleshooting"
    assert memory["recent_scene"] == {
        "scene_id": 2,
        "summary": "They inspect the power supply.",
    }
    assert memory["characters"] == [
        {
            "name": "Alice",
            "target_name": "爱丽丝",
            "role": "engineer",
            "notes": "Lead engineer",
            "aliases": ["A"],
        }
    ]
    assert memory["terminology"] == [
        {"source": "PSU", "target": "电源模块", "meaning": "server PSU"}
    ]
    assert memory["ambiguities"] == [
        {"term": "rail", "resolution": "power rail, not railway"}
    ]


def test_context_memory_sanitizes_and_reports_empty() -> None:
    empty = sanitize_translation_context_memory({"characters": [{"name": ""}]})
    assert context_memory_is_empty(empty) is True

    bounded = sanitize_translation_context_memory(
        {
            "topic": "x" * 800,
            "terminology": [
                {"source": f"term-{index}", "target": "译法"}
                for index in range(100)
            ],
        }
    )
    assert len(bounded["topic"]) == 500
    assert len(bounded["terminology"]) == 64
    assert context_memory_is_empty(bounded) is False


def test_adaptive_scene_profile_uses_observed_pause_distribution() -> None:
    segments = [
        Segment(0.0, 1.0, "a"),
        Segment(1.2, 2.0, "b"),
        Segment(2.5, 3.0, "c"),
        Segment(4.2, 5.0, "d"),
        Segment(7.8, 8.5, "e"),
        Segment(9.1, 10.0, "f"),
    ]

    profile = derive_translation_scene_profile(segments)

    assert 0.7 <= profile["soft_gap_seconds"] <= 1.6
    assert 2.0 <= profile["hard_gap_seconds"] <= 4.0
    assert profile["hard_gap_seconds"] > profile["soft_gap_seconds"]


def test_selective_translation_only_calls_selected_source_indices() -> None:
    calls: list[list[int]] = []

    class FakeAIService:
        def translate_subtitle_batch(self, **kwargs: object) -> dict[str, object]:
            blocks = kwargs["blocks"]
            assert isinstance(blocks, list)
            calls.append([int(block["idx"]) for block in blocks if isinstance(block, dict)])
            return {
                "updated_summary": "updated",
                "translations": [
                    {"idx": int(block["idx"]), "text": f"新译{int(block['idx'])}"}
                    for block in blocks
                    if isinstance(block, dict)
                ],
            }

    source = [
        Segment(0.0, 1.0, "one"),
        Segment(1.0, 2.0, "two"),
        Segment(2.0, 3.0, "three"),
    ]
    base = [
        Segment(0.0, 1.0, "旧1"),
        Segment(1.0, 2.0, "旧2"),
        Segment(2.0, 3.0, "旧3"),
    ]

    translated, summary = processing.translate_segments_openai_with_summary(
        source,
        target_lang="zh",
        style="自然",
        batch_size=3,
        ai_service=FakeAIService(),  # type: ignore[arg-type]
        base_translations=base,
        selected_indices=[2],
    )

    assert calls == [[2]]
    assert [item.text for item in translated] == ["旧1", "新译2", "旧3"]
    assert summary == "updated"


def test_scene_builder_splits_on_long_pause() -> None:
    segments = [
        Segment(0.0, 1.0, "First line."),
        Segment(1.1, 2.0, "Second line."),
        Segment(5.0, 6.0, "New scene."),
    ]

    scenes = build_translation_scenes(segments)

    assert [(scene.start_index, scene.end_index) for scene in scenes] == [(0, 2), (2, 3)]


def test_scene_builder_keeps_short_pause_inside_scene() -> None:
    segments = [
        Segment(0.0, 1.0, "One."),
        Segment(1.4, 2.0, "Two."),
        Segment(2.4, 3.0, "Three."),
    ]

    scenes = build_translation_scenes(segments)

    assert [(scene.start_index, scene.end_index) for scene in scenes] == [(0, 3)]


def test_batch_end_prefers_natural_boundary_near_requested_size() -> None:
    segments = [
        Segment(0.0, 1.0, "one"),
        Segment(1.0, 2.0, "two"),
        Segment(2.0, 3.0, "three."),
        Segment(3.8, 4.8, "four"),
        Segment(4.8, 5.8, "five"),
        Segment(5.8, 6.8, "six"),
    ]
    scenes = build_translation_scenes(segments)

    end = choose_translation_batch_end(
        segments,
        start_index=0,
        max_batch_size=5,
        scenes=scenes,
    )

    assert end == 3


def test_batch_end_never_crosses_scene_boundary() -> None:
    segments = [
        Segment(0.0, 1.0, "one"),
        Segment(1.0, 2.0, "two"),
        Segment(5.0, 6.0, "three"),
        Segment(6.0, 7.0, "four"),
    ]
    scenes = build_translation_scenes(segments)

    end = choose_translation_batch_end(
        segments,
        start_index=0,
        max_batch_size=4,
        scenes=scenes,
    )

    assert end == 2


def test_translation_loop_does_not_cross_scene_boundary_and_sends_context_state() -> None:
    calls: list[dict[str, object]] = []

    class FakeAIService:
        def translate_subtitle_batch(self, **kwargs: object) -> dict[str, object]:
            calls.append(dict(kwargs))
            blocks = kwargs["blocks"]
            assert isinstance(blocks, list)
            return {
                "updated_summary": f"summary-{len(calls)}",
                "translations": [
                    {"idx": int(block["idx"]), "text": f"译{int(block['idx'])}"}
                    for block in blocks
                    if isinstance(block, dict)
                ],
            }

    segments = [
        Segment(0.0, 1.0, "one"),
        Segment(1.0, 2.0, "two."),
        Segment(5.0, 6.0, "three"),
        Segment(6.0, 7.0, "four."),
    ]

    translated, summary = processing.translate_segments_openai_with_summary(
        segments,
        target_lang="zh",
        style="自然",
        batch_size=4,
        ai_service=FakeAIService(),  # type: ignore[arg-type]
    )

    assert [item.text for item in translated] == ["译1", "译2", "译3", "译4"]
    assert summary == "summary-2"
    assert [len(call["blocks"]) for call in calls] == [2, 2]
    first_context = calls[0]["context_state"]
    second_context = calls[1]["context_state"]
    assert isinstance(first_context, dict)
    assert isinstance(second_context, dict)
    assert first_context["scene"]["scene_id"] == 1
    assert second_context["scene"]["scene_id"] == 2
    assert first_context["next_source"] == [
        {"idx": 3, "text": "three"},
        {"idx": 4, "text": "four."},
    ]
    assert second_context["running_summary"] == "summary-1"


def test_translation_loop_carries_structured_memory_into_following_batch() -> None:
    seen_context: list[dict[str, object]] = []

    class FakeAIService:
        def translate_subtitle_batch(self, **kwargs: object) -> dict[str, object]:
            context_state = kwargs["context_state"]
            assert isinstance(context_state, dict)
            seen_context.append(context_state)
            blocks = kwargs["blocks"]
            assert isinstance(blocks, list)
            response: dict[str, object] = {
                "updated_summary": f"summary-{len(seen_context)}",
                "translations": [
                    {"idx": int(block["idx"]), "text": f"译{int(block['idx'])}"}
                    for block in blocks
                    if isinstance(block, dict)
                ],
            }
            if len(seen_context) == 1:
                response["context_update"] = {
                    "topic": "hardware debugging",
                    "characters": [
                        {"name": "Alice", "target_name": "爱丽丝", "role": "engineer"}
                    ],
                    "terminology": [
                        {"source": "POST", "target": "开机自检", "meaning": "power-on self-test"}
                    ],
                }
            return response

    memory_updates: list[dict[str, object]] = []

    translated, _summary = processing.translate_segments_openai_with_summary(
        [
            Segment(0.0, 1.0, "Alice checks POST."),
            Segment(1.0, 2.0, "It fails."),
        ],
        target_lang="zh",
        style="自然",
        batch_size=1,
        ai_service=FakeAIService(),  # type: ignore[arg-type]
        on_context_memory_update=lambda memory: memory_updates.append(memory),
    )

    assert [item.text for item in translated] == ["译1", "译2"]
    assert "memory" not in seen_context[0]
    second_memory = seen_context[1]["memory"]
    assert isinstance(second_memory, dict)
    assert second_memory["topic"] == "hardware debugging"
    assert second_memory["characters"][0]["target_name"] == "爱丽丝"
    assert second_memory["terminology"][0]["target"] == "开机自检"
    assert memory_updates[0]["topic"] == "hardware debugging"


def test_translation_loop_prefers_natural_batch_boundary_inside_long_scene() -> None:
    batch_sizes: list[int] = []

    class FakeAIService:
        def translate_subtitle_batch(self, **kwargs: object) -> dict[str, object]:
            blocks = kwargs["blocks"]
            assert isinstance(blocks, list)
            batch_sizes.append(len(blocks))
            return {
                "updated_summary": "",
                "translations": [
                    {"idx": int(block["idx"]), "text": f"译{int(block['idx'])}"}
                    for block in blocks
                    if isinstance(block, dict)
                ],
            }

    segments = [
        Segment(0.0, 1.0, "one"),
        Segment(1.0, 2.0, "two"),
        Segment(2.0, 3.0, "three."),
        Segment(3.8, 4.8, "four"),
        Segment(4.8, 5.8, "five"),
        Segment(5.8, 6.8, "six."),
    ]

    translated, _summary = processing.translate_segments_openai_with_summary(
        segments,
        target_lang="zh",
        style="自然",
        batch_size=5,
        ai_service=FakeAIService(),  # type: ignore[arg-type]
    )

    assert len(translated) == 6
    assert batch_sizes == [3, 3]


def test_context_state_contains_surrounding_source_without_changing_target_indices() -> None:
    segments = [
        Segment(float(index), float(index + 1), f"line {index + 1}")
        for index in range(7)
    ]
    scenes = build_translation_scenes(segments)

    state = build_translation_context_state(
        segments,
        scenes=scenes,
        start_index=3,
        end_index=5,
        running_summary="Earlier discussion.",
    )

    assert state["batch"] == {"segment_start": 4, "segment_end": 5}
    assert state["previous_source"] == [
        {"idx": 1, "text": "line 1"},
        {"idx": 2, "text": "line 2"},
        {"idx": 3, "text": "line 3"},
    ]
    assert state["next_source"] == [
        {"idx": 6, "text": "line 6"},
        {"idx": 7, "text": "line 7"},
    ]
    assert state["running_summary"] == "Earlier discussion."
