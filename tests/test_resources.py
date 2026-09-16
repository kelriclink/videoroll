from __future__ import annotations

from videoroll.utils.resources import cpu_percent_between, memory_stats_from_meminfo


def test_cpu_percent_between_snapshots() -> None:
    # total delta=100, idle delta=25, busy=75
    assert cpu_percent_between((1000, 500), (1100, 525)) == 75.0


def test_cpu_percent_between_rejects_non_positive_delta() -> None:
    assert cpu_percent_between((1000, 500), (1000, 500)) is None


def test_memory_stats_from_meminfo_uses_memavailable() -> None:
    stats = memory_stats_from_meminfo(
        "\n".join(
            [
                "MemTotal:       1000 kB",
                "MemFree:         100 kB",
                "MemAvailable:    250 kB",
                "Buffers:          50 kB",
                "Cached:          100 kB",
            ]
        )
    )

    assert stats is not None
    assert stats["total_bytes"] == 1000 * 1024
    assert stats["available_bytes"] == 250 * 1024
    assert stats["used_bytes"] == 750 * 1024
    assert stats["percent"] == 75.0


def test_zero_memavailable_does_not_fall_back_to_reclaimable_cache() -> None:
    stats = memory_stats_from_meminfo(
        "MemTotal: 1000 kB\nMemAvailable: 0 kB\nMemFree: 50 kB\nBuffers: 50 kB\nCached: 600 kB\n"
    )

    assert stats is not None
    assert stats["available_bytes"] == 0
    assert stats["percent"] == 100.0


def test_missing_memavailable_still_supports_older_kernels() -> None:
    stats = memory_stats_from_meminfo("MemTotal: 1000 kB\nMemFree: 50 kB\nBuffers: 50 kB\nCached: 600 kB\n")

    assert stats is not None
    assert stats["available_bytes"] == 700 * 1024
