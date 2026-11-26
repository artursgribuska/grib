"""Every command in the ./grib dispatcher points at a module with a main()."""
import importlib
import re
from pathlib import Path


def test_grib_commands_resolve():
    text = (Path(__file__).parents[1] / "grib").read_text()
    entries = re.findall(r'\[([\w-]+)\]="([\w.]+)\|', text)
    order = re.search(r'ORDER="([^"]+)"', text).group(1).split()
    assert len(entries) >= 15 and sorted(order) == sorted(k for k, _ in entries)
    for _, mod in entries:
        assert callable(getattr(importlib.import_module(mod), "main", None)), mod
