from __future__ import annotations

from videoroll.apps.subtitle_service.processing import Segment
from videoroll.apps.subtitle_service.subtitle_quality import analyze_subtitle_quality


def test_quality_report_flags_readability_timeline_and_terminology_issues() -> None:
    source = [
        Segment(0.0, 0.5, "POST finished."),
        Segment(0.45, 1.0, "Second sentence."),
    ]
    translated = [
        Segment(0.0, 0.5, "测试完成但没有使用要求的术语而且这一句非常非常非常非常长"),
        Segment(0.45, 1.0, "第二句"),
    ]
    final = list(translated)

    report = analyze_subtitle_quality(
        source_segments=source,
        translated_segments=translated,
        final_segments=final,
        target_lang="zh",
        context_memory={
            "terminology": [
                {"source": "POST", "target": "开机自检", "meaning": "power-on self-test"}
            ]
        },
    )

    issue_types = {item["type"] for item in report["issues"]}
    assert "high_cps" in issue_types
    assert "short_display" in issue_types
    assert "timeline_overlap" in issue_types
    assert "terminology_inconsistent" in issue_types
    assert report["metrics"]["overlap_count"] == 1
    assert report["metrics"]["terminology_inconsistent_count"] == 1
    assert report["score"] < 100


def test_quality_report_exposes_adaptive_profile() -> None:
    source = [
        Segment(0.0, 1.0, "one"),
        Segment(1.3, 2.0, "two"),
        Segment(2.8, 3.4, "three"),
        Segment(5.0, 6.0, "four"),
        Segment(8.5, 9.5, "five"),
    ]
    translated = [
        Segment(item.start, item.end, f"译文{index}")
        for index, item in enumerate(source, start=1)
    ]

    report = analyze_subtitle_quality(
        source_segments=source,
        translated_segments=translated,
        final_segments=translated,
        target_lang="zh-CN",
    )

    readability = report["adaptive_profile"]["readability"]
    scene = report["adaptive_profile"]["scene"]
    assert 14 <= readability["target_cps"] <= 20
    assert readability["max_line_units"] == 23.0
    assert scene["hard_gap_seconds"] > scene["soft_gap_seconds"]
