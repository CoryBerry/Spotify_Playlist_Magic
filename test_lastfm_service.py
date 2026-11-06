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
