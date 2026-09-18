"""Unit tests for the `albums` skill's pure decisions.

Everything here runs offline. The Spotify reads are thin loops over paged endpoints;
what earns tests is the judgment: what counts as a live record, which listing is the
real album, and where a reissue's bonus material starts.
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from albums_helper import (  # noqa: E402
    DEFAULT_KEEP,
    _date_key,
    adds_tracks,
    base_name,
    canonical_length,
    classify,
    flag_recycled,
    is_edition,
    match_artist,
    order_albums,
    pick_edition,
    prune_bonus,
)


# --- base_name / is_edition ------------------------------------------------

def test_editions_of_one_album_share_a_base_name():
    names = ["Rumours", "Rumours (Super Deluxe)", "Rumours - 2004 Remaster",
             "Rumours (Deluxe Edition)", "Rumours [Expanded]"]
    assert len({base_name(n) for n in names}) == 1


def test_base_name_keeps_distinct_albums_apart():
    assert base_name("Lateralus") != base_name("Undertow")


def test_base_name_does_not_eat_a_real_parenthetical_album():
    # The parenthetical is stripped for grouping, but the stem still identifies it.
    assert base_name('"V" Is for Vagina') == "v is for vagina"


def test_is_edition_flags_reissues_only():
    assert is_edition("Rumours (Super Deluxe)")
    assert is_edition("Ænima - 2016 Remaster")
    assert not is_edition("Ænima")
    assert not is_edition("Eat the Elephant")


# --- classify --------------------------------------------------------------

def test_live_album_by_title():
    assert classify("Cinquanta (Live)", "album", "album", 20) == "live"
    assert classify("MTV Unplugged", "album", "album", 14) == "live"


def test_live_album_detected_from_tracks_when_title_is_silent():
    tracks = ["Vicarious - Live", "Jambi - Live", "Schism - Live", "Parabola - Live"]
    assert classify("Salival", "album", "album", 4, track_names=tracks) == "live"


def test_a_couple_of_live_tracks_do_not_make_a_live_album():
    tracks = ["Thrust", "Normal Isn't", "Bad Wolf", "Self Evident",
              "A Public Stoning", "The Quiet Parts", "Mantastic", "Pendulum",
              "ImpetuoUs", "Seven One", "The Algorithm - Sessanta Live Mix"]
    assert classify("Normal Isn't", "album", "album", 11, track_names=tracks) == "studio"


def test_remix_and_compilation_records():
    assert classify("Existential Reckoning - Re-Wired", "album", "album", 12) == "remix"
    assert classify("All Re-Mixed Up", "album", "album", 12) == "remix"
    assert classify("Greatest Hits", "album", "album", 18) == "compilation"
    assert classify("Anything", "compilation", "album", 18) == "compilation"


def test_eps_are_kept_but_singles_are_not():
    # Cory's call: EPs belong in a pool, 1-3 track singles do not.
    assert classify("Opiate", "single", "single", 6) == "ep"
    assert classify("Pendulum", "single", "single", 2) == "single"
    assert "ep" in DEFAULT_KEEP
    assert "single" not in DEFAULT_KEEP


def test_live_is_excluded_by_default():
    assert "live" not in DEFAULT_KEEP
    assert "remix" not in DEFAULT_KEEP


# --- pick_edition / canonical_length ---------------------------------------

def test_clean_edition_beats_every_reissue():
    eds = [
        {"name": "Rumours (Super Deluxe)", "release_date": "2013-01-29", "total_tracks": 58},
        {"name": "Rumours", "release_date": "1977-02-04", "total_tracks": 11},
        {"name": "Rumours - 2004 Remaster", "release_date": "2004-03-01", "total_tracks": 11},
    ]
    assert pick_edition(eds)["total_tracks"] == 11
    assert pick_edition(eds)["name"] == "Rumours"


def test_earliest_clean_edition_wins_among_clean_ones():
    eds = [
        {"name": "Undertow", "release_date": "1999-01-01", "total_tracks": 10},
        {"name": "Undertow", "release_date": "1993-04-06", "total_tracks": 10},
    ]
    assert pick_edition(eds)["release_date"] == "1993-04-06"


def test_all_reissues_falls_back_to_the_leanest_early_one():
    # The Rumours nightmare: if Spotify only lists boxes, take the 11-track remaster,
    # never the 58-track box.
    eds = [
        {"name": "Rumours (Super Deluxe)", "release_date": "2013-01-29", "total_tracks": 58},
        {"name": "Rumours - 2004 Remaster", "release_date": "2004-03-01", "total_tracks": 11},
    ]
    assert pick_edition(eds)["total_tracks"] == 11


def test_canonical_length_uses_the_shortest_clean_sibling():
    eds = [
        {"name": "Rumours", "total_tracks": 11},
        {"name": "Rumours (Super Deluxe)", "total_tracks": 58},
    ]
    assert canonical_length(eds) == 11


def test_canonical_length_is_none_when_every_listing_is_a_reissue():
    eds = [{"name": "Rumours (Super Deluxe)", "total_tracks": 58},
           {"name": "Rumours (Deluxe Edition)", "total_tracks": 20}]
    assert canonical_length(eds) is None


# --- prune_bonus -----------------------------------------------------------

def _t(name, disc=1):
    return {"uri": "spotify:track:%s" % abs(hash(name)), "name": name, "disc_number": disc}


def test_extra_discs_are_dropped_first():
    tracks = [_t("Second Hand News"), _t("Dreams")] + [_t("Outtake %d" % i, disc=2)
                                                       for i in range(10)]
    kept, note = prune_bonus(tracks, deluxe=True)
    assert len(kept) == 2
    assert "extra disc" in note


def test_canonical_length_truncates_a_deluxe():
    tracks = [_t("Track %d" % i) for i in range(20)]
    kept, note = prune_bonus(tracks, canonical=11, deluxe=True)
    assert len(kept) == 11
    assert "11-track original" in note


def test_tail_pruning_when_no_canonical_length_is_known():
    tracks = [_t("Go Your Own Way"), _t("Songbird"),
              _t("Silver Springs (Early Take)"), _t("Dreams - Demo")]
    kept, note = prune_bonus(tracks, canonical=None, deluxe=True)
    assert [t["name"] for t in kept] == ["Go Your Own Way", "Songbird"]
    assert "bonus track" in note


def test_tail_pruning_never_reaches_past_bonus_material():
    # 'The Grand Conjunction' has no bonus marker, so pruning stops there even though
    # an earlier track's title contains 'Version'.
    tracks = [_t("Version City"), _t("The Grand Conjunction"), _t("Intension - Demo")]
    kept, _ = prune_bonus(tracks, canonical=None, deluxe=True)
    assert [t["name"] for t in kept] == ["Version City", "The Grand Conjunction"]


def test_a_clean_album_is_left_completely_alone():
    tracks = [_t("The Pot"), _t("Jambi"), _t("Wings for Marie")]
    kept, note = prune_bonus(tracks, canonical=None, deluxe=False)
    assert kept == tracks
    assert note == "clean"


def test_unprunable_reissue_is_flagged_for_a_human():
    tracks = [_t("One"), _t("Two"), _t("Three")]
    _kept, note = prune_bonus(tracks, canonical=None, deluxe=True)
    assert "VERIFY" in note


def test_prune_handles_an_empty_album():
    assert prune_bonus([]) == ([], "empty")


# --- order_albums ----------------------------------------------------------

def test_albums_order_by_release_date():
    albums = [{"name": "Lateralus", "sort_date": "2001-05-15"},
              {"name": "Undertow", "sort_date": "1993-04-06"},
              {"name": "Mer de Noms", "sort_date": "2000-05-23"}]
    assert [a["name"] for a in order_albums(albums)] == ["Undertow", "Mer de Noms", "Lateralus"]


def test_a_remaster_does_not_drag_an_old_album_to_the_end():
    # sort_date is the earliest date across editions; release_date is the listing's.
    albums = [{"name": "Rumours", "sort_date": "1977-02-04", "release_date": "2004-03-01"},
              {"name": "Lateralus", "sort_date": "2001-05-15", "release_date": "2001-05-15"}]
    assert [a["name"] for a in order_albums(albums)] == ["Rumours", "Lateralus"]


def test_undated_albums_sort_last_rather_than_crashing():
    albums = [{"name": "Mystery"}, {"name": "Undertow", "sort_date": "1993-04-06"}]
    assert [a["name"] for a in order_albums(albums)] == ["Undertow", "Mystery"]


# --- flag_recycled ---------------------------------------------------------

def test_compilation_of_earlier_songs_is_flagged_without_a_title_hint():
    # The real case: 'In Case You Were Napping' is fifteen songs that already exist
    # on earlier Puscifer records, and its title says nothing at all.
    albums = [
        {"name": "V Is for Vagina", "sort_date": "2007-10-30", "kind": "studio",
         "track_names": ["Queen B", "Momma Sed", "Indigo Children"]},
        {"name": "Conditions of My Parole", "sort_date": "2011-01-01", "kind": "studio",
         "track_names": ["Conditions Of My Parole", "Horizons", "Man Overboard"]},
        {"name": "In Case You Were Napping", "sort_date": "2025-07-28", "kind": "studio",
         "track_names": ["Queen B", "Momma Sed", "Indigo Children",
                         "Conditions Of My Parole", "Horizons", "Man Overboard"]},
    ]
    flag_recycled(albums)
    by_name = {a["name"]: a for a in albums}
    assert by_name["In Case You Were Napping"]["kind"] == "recycled"
    assert by_name["In Case You Were Napping"]["recycled_pct"] == 100
    assert by_name["V Is for Vagina"]["kind"] == "studio"
    assert "recycled" not in DEFAULT_KEEP


def test_a_genuinely_new_album_survives_the_recycled_check():
    albums = [
        {"name": "Money Shot", "sort_date": "2015-01-01", "kind": "studio",
         "track_names": ["Galileo", "Agostina", "The Remedy"]},
        {"name": "Normal Isn't", "sort_date": "2026-02-06", "kind": "studio",
         "track_names": ["Thrust", "Bad Wolf", "Self Evident", "Pendulum"]},
    ]
    flag_recycled(albums)
    assert all(a["kind"] == "studio" for a in albums)


def test_recycled_check_ignores_version_suffixes_when_matching():
    # 'Breathe - Versatile Mix' is still 'Breathe' for the purposes of "seen this".
    albums = [
        {"name": "Donkey Punch The Night", "sort_date": "2013-01-01", "kind": "ep",
         "track_names": ["Breathe", "Dear Brother"]},
        {"name": "Napping", "sort_date": "2025-01-01", "kind": "studio",
         "track_names": ["Breathe - Versatile Mix", "Dear Brother (Live)"]},
    ]
    flag_recycled(albums)
    assert albums[1]["kind"] == "recycled"


def test_recycled_check_is_a_noop_without_tracklists():
    # No --deep, no track_names, no reclassification — and no crash.
    albums = [{"name": "A", "sort_date": "2001", "kind": "studio"},
              {"name": "B", "sort_date": "2002", "kind": "studio"}]
    flag_recycled(albums)
    assert all(a["kind"] == "studio" for a in albums)


def test_recycled_check_never_downgrades_a_live_or_remix_record():
    # Those are already excluded by title; the overlap pass must not relabel them.
    albums = [
        {"name": "Money Shot", "sort_date": "2015", "kind": "studio",
         "track_names": ["Galileo", "Agostina"]},
        {"name": "Money $hot Your Re-Load", "sort_date": "2016", "kind": "remix",
         "track_names": ["Galileo", "Agostina"]},
    ]
    flag_recycled(albums)
    assert albums[1]["kind"] == "remix"


# --- match_artist ----------------------------------------------------------

def test_exact_artist_beats_a_tribute_act_with_a_cleaner_title():
    # The Abbey Road miss: the real album is only listed as '(Remastered)' and
    # '(Super Deluxe Edition)', while a ukulele tribute holds the unmarked title.
    cands = [
        {"name": "Abbey Road (Remastered)", "artist": "The Beatles", "total_tracks": 17},
        {"name": "Abbey Road (Super Deluxe Edition)", "artist": "The Beatles",
         "total_tracks": 40},
        {"name": "Abbey Road", "artist": "The Beatles Complete On Ukulele",
         "total_tracks": 17},
    ]
    narrowed = match_artist("The Beatles", cands)
    assert len(narrowed) == 2
    assert all(c["artist"] == "The Beatles" for c in narrowed)


def test_artist_match_ignores_case_and_leading_the():
    assert match_artist("tool", [{"artist": "TOOL"}])
    assert match_artist("The Beatles", [{"artist": "Beatles"}])


def test_artist_match_falls_back_to_containment_when_nothing_is_exact():
    cands = [{"artist": "Nine Inch Nails"}, {"artist": "Ministry"}]
    assert match_artist("Nine Inch", cands) == [{"artist": "Nine Inch Nails"}]


def test_artist_match_returns_everything_when_no_artist_was_given():
    cands = [{"artist": "A"}, {"artist": "B"}]
    assert match_artist("", cands) == cands


def test_artist_match_does_not_strand_the_caller_on_a_bad_name():
    # Nothing matches -> hand back the full list rather than resolving to nothing.
    cands = [{"artist": "Tool"}, {"artist": "Puscifer"}]
    assert match_artist("Fleetwood Mac", cands) == cands


# --- adds_tracks -----------------------------------------------------------

def test_a_plain_remaster_is_not_treated_as_expanded():
    # Abbey Road (Remastered) is the original 17 tracks — pruning it would be wrong,
    # and flagging it VERIFY is noise.
    assert not adds_tracks("Abbey Road (Remastered)")
    assert not adds_tracks("Ænima - 2016 Remaster")
    assert is_edition("Abbey Road (Remastered)")   # still an edition for grouping


def test_boxes_and_anniversaries_are_treated_as_expanded():
    assert adds_tracks("Rumours (Super Deluxe)")
    assert adds_tracks("OK Computer OKNOTOK (20th Anniversary)")
    assert adds_tracks("Sound of Silver (Expanded Edition)")


def test_a_remaster_with_a_clean_sibling_still_truncates_on_length():
    # adds_tracks only gates the *marker-based* tail prune; a known canonical length
    # from a clean sibling still applies, whatever the title says.
    tracks = [_t("T%d" % i) for i in range(17)]
    kept, note = prune_bonus(tracks, canonical=11, deluxe=False)
    assert len(kept) == 11
    assert "11-track original" in note


# --- regressions: derivative releases must not outrank originals ------------

def test_a_live_album_never_makes_its_studio_original_look_recycled():
    # The real miss: 'Thirteenth Step - Live' is dated '2003' (year only) while the
    # studio album is '2003-01-01'. Compared raw, the live record sorted first and
    # seeded the baseline, flagging a core studio album as 100% recycled.
    albums = [
        {"name": "Thirteenth Step - Live", "sort_date": "2003", "kind": "live",
         "track_names": ["The Package", "Weak and Powerless", "The Outsider"]},
        {"name": "Thirteenth Step", "sort_date": "2003-01-01", "kind": "studio",
         "track_names": ["The Package", "Weak and Powerless", "The Outsider"]},
    ]
    flag_recycled(albums)
    by_name = {a["name"]: a for a in albums}
    assert by_name["Thirteenth Step"]["kind"] == "studio"


def test_a_remix_companion_does_not_seed_the_baseline_either():
    albums = [
        {"name": "aMOTION", "sort_date": "2004-01-01", "kind": "remix",
         "track_names": ["Judith", "The Outsider", "Weak and Powerless"]},
        {"name": "eMOTIVe", "sort_date": "2004-11-02", "kind": "studio",
         "track_names": ["Annihilation", "Imagine", "People Are People"]},
    ]
    flag_recycled(albums)
    assert albums[1]["kind"] == "studio"


def test_a_recycled_record_does_not_itself_become_a_baseline():
    # Otherwise one compilation would launder its songs into the "seen" set and
    # start flagging later originals that happen to share a title.
    albums = [
        {"name": "Real Album", "sort_date": "2000", "kind": "studio",
         "track_names": ["Alpha", "Beta"]},
        {"name": "Comp", "sort_date": "2010", "kind": "studio",
         "track_names": ["Alpha", "Beta", "Gamma"]},
        {"name": "Later Album", "sort_date": "2020", "kind": "studio",
         "track_names": ["Gamma", "Delta"]},
    ]
    flag_recycled(albums)
    by_name = {a["name"]: a for a in albums}
    assert by_name["Comp"]["kind"] == "recycled"
    assert by_name["Later Album"]["kind"] == "studio"


def test_partial_dates_sort_chronologically():
    albums = [{"name": "C", "sort_date": "2003-06-01"},
              {"name": "A", "sort_date": "2003"},
              {"name": "B", "sort_date": "2003-01-01"},
              {"name": "D", "sort_date": "2004"}]
    assert [a["name"] for a in order_albums(albums)] == ["A", "B", "C", "D"]


def test_date_key_pads_every_shape_spotify_returns():
    assert _date_key("2003") < _date_key("2003-01-01")
    assert _date_key("2003-01") < _date_key("2003-01-01")
    assert _date_key("1993-04-06") < _date_key("1996-09-17")
    assert _date_key(None) == "9999-00-00"
