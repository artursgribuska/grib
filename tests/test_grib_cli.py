"""Every command in the ./grib dispatcher points at a module with a main(); the script is POSIX sh."""
import importlib
import re
import subprocess
from pathlib import Path

GRIB = Path(__file__).parents[1] / "grib"


def test_grib_commands_resolve():
    text = GRIB.read_text()
    entries = re.findall(r"^([\w-]+)\|([\w.]+)\|", text, flags=re.M)
    assert len(entries) >= 18 and len({k for k, _ in entries}) == len(entries)
    for _, mod in entries:
        assert callable(getattr(importlib.import_module(mod), "main", None)), mod


def test_grib_runs_under_posix_sh():
    assert GRIB.read_text().startswith("#!/bin/sh")
    out = subprocess.run(["sh", str(GRIB)], capture_output=True, text=True)
    assert out.returncode == 0 and "screen-cases" in out.stdout and "operator" in out.stdout
    bad = subprocess.run(["sh", str(GRIB), "nope"], capture_output=True, text=True)
    assert bad.returncode == 2 and "unknown command" in bad.stderr
