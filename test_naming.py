"""Generated playlist names: `[Kind] Title — Mon D 'YY`."""
from datetime import datetime

from naming import generated_name, is_block_mix, lead_title, short_date, shuffled_copy_name

NOW = datetime(2026, 11, 2, 15, 4)


def test_short_date_has_no_leading_zero():
    assert short_date(NOW) == "Nov 2 '26"


def test_generated_name_shape():
    assert generated_name("Block", "Morning Run", NOW) == "[Block] Morning Run — Nov 2 '26"


def test_generated_name_collapses_whitespace_and_tolerates_blank_title():
    assert generated_name("Block", "  Morning   Run ", NOW) == "[Block] Morning Run — Nov 2 '26"
    assert generated_name("Import", "", NOW) == "[Import] — Nov 2 '26"


def test_lead_title_variants():
    assert lead_title([]) == ""
    assert lead_title(["Chill Selects"]) == "Chill Selects"
    assert lead_title(["Chill Selects", "Easy Drops"]) == "Chill Selects & Easy Drops"
    assert lead_title(["A", "B", "C"]) == "A +2"
    assert lead_title(["", "  ", "Solo"]) == "Solo"


def test_lead_title_two_long_names_fall_back_to_plus():
    long = "x" * 30
    assert lead_title([long, long]) == f"{long} +1"


def test_shuffled_copy_sorts_beside_original():
    assert shuffled_copy_name("Cory's Chill Selects", NOW) == "Cory's Chill Selects — Shuffled Nov 2 '26"


def test_is_block_mix_new_and_legacy():
    assert is_block_mix("[Block] Morning Run — Nov 2 '26")
    assert is_block_mix("running out of July : Block Mix : July 28 '26")
    assert not is_block_mix("[Mix] OFFICE — Morale Override")
    assert not is_block_mix("[Sampler] Chill Selects +2 — Nov 2 '26")
    assert not is_block_mix("Albums - Dance-Punk II")
    assert not is_block_mix(None)
