"""Read-only checks against a real share. Run with: PDRIVE_LIVE=1 pytest -m live (needs `pdrive login`)."""
import io
import json
import os

import pytest

from pdrive.cli import main

pytestmark = [pytest.mark.live,
              pytest.mark.skipif(not os.environ.get("PDRIVE_LIVE"), reason="set PDRIVE_LIVE=1 to run")]


def run(*argv):
    out, err = io.StringIO(), io.StringIO()
    code = main(list(argv), stdout=out, stderr=err)
    return code, out.getvalue(), err.getvalue()


def test_ls_root():
    code, out, err = run("ls", "--json")
    assert code == 0, err
    assert all(json.loads(line)["path"].startswith("/") for line in out.splitlines())


def test_missing_path():
    code, _, err = run("stat", "/__pdrive_definitely_missing__")
    assert code == 1 and "No such file or directory" in err
