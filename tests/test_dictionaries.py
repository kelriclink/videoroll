from __future__ import annotations

from videoroll.apps.subtitle_service.dictionaries import (
    dictionary_import_presets,
    dictionary_entries_to_context_cards,
    dictionary_entries_to_evidence,
    get_dictionary_import_preset,
    normalize_dictionary_slug,
    normalize_dictionary_term,
    parse_dictionary_file,
)


def test_dictionary_normalization_helpers() -> None:
    assert normalize_dictionary_term("  Rush-B  ") == "rush b"
    assert normalize_dictionary_slug("CC CEDICT / zh-en") == "cc-cedict-zh-en"


def test_ecdict_import_preset() -> None:
    preset = get_dictionary_import_preset("ecdict")
    presets = {item["key"] for item in dictionary_import_presets()}

    assert preset is not None
    assert "ecdict" in presets
    assert preset["format_name"] == "ecdict"
    assert preset["source_lang"] == "en"
    assert preset["target_lang"] == "zh"
    assert preset["license"] == "MIT"
    assert preset["recommended_full_import"] is True


def test_parse_ecdict_csv(tmp_path) -> None:
    path = tmp_path / "ecdict.csv"
    path.write_text(
        "word,phonetic,definition,translation,pos,collins,oxford,tag,frq\n"
        "hobby knife,,a small sharp craft knife,模型刀\\n美工刀,n,1,1,craft,120\n",
        encoding="utf-8",
    )

    entries = list(parse_dictionary_file(path, format_name="ecdict", source_lang="en", target_lang="zh", domain="craft"))

    assert len(entries) == 1
    assert entries[0].term == "hobby knife"
    assert entries[0].translations == ["模型刀", "美工刀"]
    assert entries[0].source_lang == "en"
    assert entries[0].target_lang == "zh"
    assert entries[0].domain == "craft"
    assert entries[0].quality > 0.7


def test_parse_cc_cedict(tmp_path) -> None:
    path = tmp_path / "cedict_ts.u8"
    path.write_text(
        "# CC-CEDICT\n"
        "真值表 真值表 [zhen1 zhi2 biao3] /truth table/\n",
        encoding="utf-8",
    )

    entries = list(parse_dictionary_file(path, format_name="cc-cedict", source_lang="zh", target_lang="en"))

    assert len(entries) == 1
    assert entries[0].term == "真值表"
    assert entries[0].translations == ["truth table"]
    assert entries[0].metadata["pinyin"] == "zhen1 zhi2 biao3"


def test_dictionary_entries_convert_to_evidence_and_context_cards() -> None:
    entry = {
        "id": "entry-1",
        "source_name": "ECDICT",
        "source_slug": "ecdict",
        "source_url": "https://example.test/ecdict",
        "license_url": "",
        "license": "MIT",
        "attribution": "ECDICT",
        "term": "hobby knife",
        "translations": ["模型刀", "美工刀"],
        "definition": "a small sharp craft knife",
        "pos": "n",
        "domain": "craft",
        "aliases": ["craft knife"],
        "quality": 0.86,
    }

    evidence = dictionary_entries_to_evidence([entry])
    cards = dictionary_entries_to_context_cards([entry])

    assert evidence[0]["tool"] == "dictionary_lookup"
    assert "模型刀" in evidence[0]["snippet"]
    assert cards[0]["translation"] == "模型刀"
    assert cards[0]["knowledge_status"] == "dictionary_context"
