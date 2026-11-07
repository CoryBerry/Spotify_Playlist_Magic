"""Artist ice (#10): expanding a frozen-artist set into the track URIs it covers."""
from spotify_service import artist_ids_of, artist_iced_uris

FRUIT_BATS = "fruitbats"
OTHER = "other"


class FakeSp:
    """Answers sp.tracks() from a {track_id: [artist_id]} table and counts the calls."""

    def __init__(self, table):
        self.table = table
        self.calls = []

    def tracks(self, ids):
        self.calls.append(list(ids))
        return {"tracks": [
            {"uri": f"spotify:track:{i}", "artists": [{"id": a} for a in self.table[i]]}
            if i in self.table else None
            for i in ids
        ]}


def u(i):
    return f"spotify:track:{i}"


def test_no_frozen_artists_costs_nothing():
    sp = FakeSp({"a": [FRUIT_BATS]})
    assert artist_iced_uris(sp, [u("a")], set()) == set()
    assert sp.calls == []


def test_known_artists_need_no_lookup():
    sp = FakeSp({})
    known = {u("a"): [FRUIT_BATS], u("b"): [OTHER]}
    assert artist_iced_uris(sp, [u("a"), u("b")], {FRUIT_BATS}, known) == {u("a")}
    assert sp.calls == []


def test_featured_credit_counts():
    known = {u("feat"): [OTHER, FRUIT_BATS]}
    assert artist_iced_uris(FakeSp({}), [u("feat")], {FRUIT_BATS}, known) == {u("feat")}


def test_missing_artists_are_looked_up_in_batches_of_50():
    table = {str(i): [FRUIT_BATS if i % 2 else OTHER] for i in range(120)}
    sp = FakeSp(table)
    uris = [u(i) for i in table]
    got = artist_iced_uris(sp, uris, {FRUIT_BATS})
    assert got == {u(i) for i in range(120) if i % 2}
    assert [len(c) for c in sp.calls] == [50, 50, 20]


def test_lookup_skips_known_dupes_and_non_tracks():
    sp = FakeSp({"b": [FRUIT_BATS]})
    uris = [u("a"), u("b"), u("b"), "spotify:local:x:y:z:1", "spotify:episode:e"]
    got = artist_iced_uris(sp, uris, {FRUIT_BATS}, known={u("a"): [OTHER]})
    assert got == {u("b")}
    assert sp.calls == [["b"]]


def test_unresolvable_track_is_not_dropped():
    sp = FakeSp({})  # Spotify returns None for this id
    assert artist_iced_uris(sp, [u("gone")], {FRUIT_BATS}) == set()


def test_artist_ids_of_tolerates_local_and_missing():
    assert artist_ids_of({"artists": [{"id": "x"}, {"id": None}, {"name": "local"}]}) == ["x"]
    assert artist_ids_of({}) == []
