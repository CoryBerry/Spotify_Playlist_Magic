"""Tests for lastfm_service's on-disk cache — no network (urlopen is faked)."""
import io
import json

import pytest

import lastfm_service as lf


class _FakeResp(io.BytesIO):
    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


@pytest.fixture
def env(tmp_path, monkeypatch):
    """Fresh in-memory cache state, a temp cache file, and a counting fake Last.fm."""
    monkeypatch.setattr(lf, "_MEMO", {})
    monkeypatch.setattr(lf, "_DIRTY", set())
    monkeypatch.setattr(lf, "_LAST_FLUSH", {})
    monkeypatch.setenv("LASTFM_API_KEY", "test-key")
    calls = {"net": 0, "read": 0}

    def fake_urlopen(req, timeout=15):
        calls["net"] += 1
        return _FakeResp(json.dumps({"toptags": {"tag": [{"name": "punk", "count": 100}]}}).encode())

    real_read = lf._read_cache_file

    def counting_read(path):
        calls["read"] += 1
        return real_read(path)

    monkeypatch.setattr(lf.urllib.request, "urlopen", fake_urlopen)
    monkeypatch.setattr(lf, "_read_cache_file", counting_read)
    path = str(tmp_path / "cache.json")
    return path, calls


def _tags(path, artist):
    return lf._call("artist.gettoptags", {"artist": artist}, cache_path=path)


def test_file_is_read_once_for_many_calls(env):
    path, calls = env
    with open(path, "w", encoding="utf-8") as fh:
        json.dump({"artist.gettoptags?artist=A": {"toptags": {"tag": []}}}, fh)
    for _ in range(50):
        _tags(path, "A")
    assert calls["read"] == 1
    assert calls["net"] == 0


def test_misses_are_deferred_then_flushed(env, monkeypatch):
    path, calls = env
    monkeypatch.setattr(lf, "_FLUSH_EVERY", 3600)  # never flush on the clock in this test
    for name in ("A", "B", "C"):
        _tags(path, name)
    assert calls["net"] == 3
    with pytest.raises(FileNotFoundError):
        open(path, encoding="utf-8")  # nothing written yet
    lf.flush_cache()
    with open(path, encoding="utf-8") as fh:
        on_disk = json.load(fh)
    assert len(on_disk) == 3
    _tags(path, "A")  # now a hit
    assert calls["net"] == 3


def test_flushes_on_the_clock(env, monkeypatch):
    path, _ = env
    monkeypatch.setattr(lf, "_FLUSH_EVERY", 0)
    _tags(path, "A")
    with open(path, encoding="utf-8") as fh:
        assert "artist.gettoptags?artist=A" in json.load(fh)


def test_corrupt_file_is_set_aside_not_clobbered(env, tmp_path):
    path, _ = env
    with open(path, "w", encoding="utf-8") as fh:
        fh.write('{"half a wri')
    _tags(path, "A")
    lf.flush_cache()
    with open(path + ".corrupt", encoding="utf-8") as fh:
        assert fh.read() == '{"half a wri'
    with open(path, encoding="utf-8") as fh:
        assert "artist.gettoptags?artist=A" in json.load(fh)


def test_save_is_atomic_and_leaves_no_temp_files(env, tmp_path):
    path, _ = env
    lf._save_cache({"k": 1}, path)
    assert sorted(p.name for p in tmp_path.iterdir()) == ["cache.json"]


# --- #46: slimmed cache entries --------------------------------------------

_IMG = [{"size": s, "#text": f"https://img.example/{s}.png"} for s in ("small", "medium", "large")]


def _raw_track(name, artist, plays=None):
    t = {"name": name, "mbid": "", "url": "https://last.fm/x", "image": _IMG,
         "streamable": {"fulltrack": "0", "#text": "0"},
         "artist": {"name": artist, "mbid": "abc", "url": "https://last.fm/a"},
         "date": {"uts": "1", "#text": "x"}}
    if plays is not None:
        t["playcount"] = str(plays)
    return t


RAW = {
    "user.gettoptracks": {"toptracks": {
        "track": [_raw_track("Fade Into You", "Mazzy Star", 42), _raw_track("Rudy", "The Specials", 7)],
        "@attr": {"user": "u", "totalPages": "1", "page": "1", "total": "2", "perPage": "1000"}}},
    "user.getlovedtracks": {"lovedtracks": {
        "track": _raw_track("Rudy", "The Specials"),  # Last.fm collapses a 1-item list to a dict
        "@attr": {"totalPages": "1"}}},
    "artist.gettoptags": {"toptags": {
        "tag": [{"name": "dance-punk", "count": 100, "url": "https://last.fm/tag/dp"},
                {"name": "indie", "count": 40, "url": "https://last.fm/tag/i"}],
        "@attr": {"artist": "LCD Soundsystem"}}},
    "user.gettopartists": {"topartists": {
        "artist": [{"name": "Wednesday", "playcount": "99", "image": _IMG, "url": "u", "mbid": ""}],
        "@attr": {"totalPages": "1"}}},
}


def _fake_call_from(bodies):
    def fake(method, params, **kw):
        return bodies[method]
    return fake


def test_slimmed_bodies_parse_identically(monkeypatch):
    slim = {m: lf._slim(m, b) for m, b in RAW.items()}
    results = []
    for bodies in (RAW, slim):
        monkeypatch.setattr(lf, "_call", _fake_call_from(bodies))
        results.append((
            lf.user_top_tracks("u"), lf.user_loved_tracks("u"),
            lf.artist_top_tags("LCD Soundsystem"), lf.user_top_artists("u"),
        ))
    assert results[0] == results[1]
    assert results[0][0][0] == {"artist": "Mazzy Star", "title": "Fade Into You", "playcount": 42}


def test_slim_drops_the_bulk():
    for method, body in RAW.items():
        assert len(json.dumps(lf._slim(method, body))) < len(json.dumps(body)) / 2, method
    assert "image" not in json.dumps(lf._slim("user.gettoptracks", RAW["user.gettoptracks"]))


def test_unknown_methods_pass_through_raw():
    body = {"artist": {"name": "X", "bio": {"summary": "hi"}}}
    assert lf._slim("artist.getinfo", body) is body


def test_miss_caches_the_slim_body(env, monkeypatch):
    path, _ = env
    raw = RAW["artist.gettoptags"]
    monkeypatch.setattr(lf.urllib.request, "urlopen",
                        lambda req, timeout=15: _FakeResp(json.dumps(raw).encode()))
    got = _tags(path, "LCD Soundsystem")
    assert got == lf._slim("artist.gettoptags", raw)
    lf.flush_cache()
    with open(path, encoding="utf-8") as fh:
        assert "url" not in fh.read()


def test_compact_cache_slims_both_entry_shapes_in_place(env):
    path, _ = env
    with open(path, "w", encoding="utf-8") as fh:
        json.dump({
            "artist.gettoptags?artist=LCD": RAW["artist.gettoptags"],
            "user.gettoptracks?page=1&user=u": {"_ts": 123.0, "_body": RAW["user.gettoptracks"]},
        }, fh, indent=2)
    before, after = lf.compact_cache(path)
    assert after < before
    with open(path, encoding="utf-8") as fh:
        data = json.load(fh)
    assert data["artist.gettoptags?artist=LCD"] == lf._slim("artist.gettoptags", RAW["artist.gettoptags"])
    assert data["user.gettoptracks?page=1&user=u"]["_ts"] == 123.0  # TTL wrapper preserved
    assert "image" not in json.dumps(data)
