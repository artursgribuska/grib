"""All live watches run each cycle; one failing does not stop the others."""


def test_cycle_isolates_failures():
    from watch.run_all import cycle

    def boom():
        raise RuntimeError("API down")
    res = cycle({"a": boom, "b": lambda: {"new": 2}})
    assert "API down" in res["a"]["error"] and res["b"] == {"new": 2}


def test_default_watches_are_registered():
    from watch.run_all import watches
    assert set(watches()) == {"polymarket", "kalshi"}
