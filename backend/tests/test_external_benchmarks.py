"""Tests for external benchmark figures.

These tests guard the HONESTY RULES rather than code behaviour. If someone
later puts an external figure into the same list as our own results "so it
looks nicer", or removes a source link, the test fails and says why.
"""
from urllib.parse import urlparse

from app import external_benchmarks


def test_every_source_has_name_version_link_and_date():
    """No figure is published without a source and a date — this is the core rule."""
    for s in external_benchmarks.SOURCES:
        assert s["name"], s["key"]
        assert s["version"], s["key"]
        assert s["kind"] in ("matrix", "model", "ranking"), s["key"]
        u = urlparse(s["source_url"])
        assert u.scheme == "https" and u.netloc, s["key"]
        # ISO date: the frontend displays it as is.
        assert len(s["read_date"]) == 10 and s["read_date"][4] == "-", s["key"]


def test_every_source_is_marked_not_comparable():
    """`comparable` is always False.

    The field is not there to be "flipped to True later": an external test
    cannot match ours, because we measure the gateway itself. If someone ever
    measures under truly identical conditions, that result is added
    separately, with its own methodology note — not here.
    """
    for s in external_benchmarks.SOURCES:
        assert s["comparable"] is False, s["key"]
        assert s["why_not_comparable"], s["key"]
        assert s["what_it_measures"], s["key"]


def source_rows(s):
    """Yields every row of a source, whatever its structure kind."""
    if s["kind"] == "matrix":
        for g in s["groups"]:
            yield from g["rows"]
    else:
        yield from s["rows"]


def test_every_row_has_a_score():
    """score or scores is required; None is accepted (a dash in the source)."""
    for s in external_benchmarks.SOURCES:
        for r in source_rows(s):
            if s["kind"] == "matrix":
                assert "scores" in r, (s["key"], r)
                assert isinstance(r["scores"], list), (s["key"], r)
                for v in r["scores"]:
                    if v is not None:
                        assert isinstance(v, (int, float)) and 0 < v <= 100, (s["key"], r, v)
            else:
                assert "score" in r, (s["key"], r)
                if r["score"] is not None:
                    assert isinstance(r["score"], (int, float)) and 0 < r["score"], (s["key"], r)


def test_ranking_rows_have_vendor_and_model():
    """Ranking and matrix kinds require the vendor and the model name."""
    for s in external_benchmarks.SOURCES:
        if s["kind"] in ("ranking", "matrix"):
            for r in (s["rows"] if s["kind"] == "ranking" else
                      [row for g in s["groups"] for row in g["rows"]]):
                if s["kind"] == "ranking":
                    assert r.get("model") and r.get("vendor"), (s["key"], r)


def test_public_payload_has_disclaimer():
    d = external_benchmarks.public_data()
    assert d["disclaimer"]
    assert d["harness_note"]["title"]
    assert len(d["sources"]) == len(external_benchmarks.SOURCES)


def test_endpoint_returns_external_figures_separately(env):
    """External figures are NOT merged into our benchmark response."""
    ours = env.client.get("/portal/benchmark")
    assert ours.status_code == 200
    text = ours.text.lower()

    # External model names must not appear in our response.
    for s in external_benchmarks.SOURCES:
        if s["kind"] == "matrix":
            for col in s["columns"]:
                # "Claude Opus 5.5" -> ["opus"]: check the longer words of the name
                words = [w for w in col.lower().split() if len(w) > 4]
                for w in words:
                    assert w not in text, f"{col} ({w}) found in our response"
        elif s["kind"] == "model":
            # The model kind has a single model name at the top — check its words
            words = [w for w in s["model"].lower().split() if len(w) > 4]
            for w in words:
                assert w not in text, f"{s['model']} ({w}) found in our response"
        else:  # ranking
            for r in s["rows"]:
                words = [w for w in r["model"].lower().split() if len(w) > 4]
                for w in words:
                    assert w not in text, f"{r['model']} ({w}) found in our response"

    external = env.client.get("/portal/benchmark/external")
    assert external.status_code == 200
    d = external.json()
    assert d["sources"][0]["source_url"].startswith("https://")
    # And the reverse: the external response has none of our model names.
    assert "sable" not in external.text.lower()


def test_endpoint_requires_no_login(env):
    """The page is public — for marketing and for choosing a model."""
    r = env.client.get("/portal/benchmark/external")
    assert r.status_code == 200 and r.json()["sources"]
