import contextlib
import json
import re
import sys
from pathlib import Path

import pytest

from kart import cli


H = pytest.helpers.helpers()


def test_version(cli_runner):
    r = cli_runner.invoke(["--version"])
    assert r.exit_code == 0, r
    assert re.match(
        r"^Kart v(\d+\.\d+.*?)\n» GDAL v",
        r.stdout,
    )


def test_cli_help():
    click_app = cli.cli
    for name, cmd in click_app.commands.items():
        if name == "help":
            continue
        assert cmd.help, f"`{name}` command has no help text"


@pytest.mark.parametrize("command", [["--help"], ["init", "--help"]])
def test_help_page_render(cli_runner, command):
    r = cli_runner.invoke(command)
    assert r.exit_code == 0, r.stderr


@pytest.fixture
def sys_path_reset(monkeypatch):
    """A context manager to save & reset after code that changes sys.path"""

    @contextlib.contextmanager
    def _sys_path_reset():
        with monkeypatch.context() as m:
            m.setattr("sys.path", sys.path[:])
            yield

    return _sys_path_reset


def test_ext_run(tmp_path, cli_runner, sys_path_reset):
    # missing script
    with sys_path_reset():
        r = cli_runner.invoke(["ext-run", tmp_path / "zero.py"])
    assert r.exit_code == 2, r

    # invalid syntax
    with open(tmp_path / "one.py", "wt") as fs:
        fs.write("def nope")
    with sys_path_reset():
        r = cli_runner.invoke(["ext-run", tmp_path / "one.py"])
    assert r.exit_code == 1, r
    assert "Error: loading " in r.stderr
    assert "SyntaxError" in r.stderr
    assert "line 1" in r.stderr

    # main() with wrong argspec
    with open(tmp_path / "two.py", "wt") as fs:
        fs.write("def main():\n  print('nope')")
    with sys_path_reset():
        r = cli_runner.invoke(["ext-run", tmp_path / "two.py"])
    assert r.exit_code == 1, r
    assert "requires a main(ctx, args) function" in r.stderr

    # no main()
    with open(tmp_path / "three_a.py", "wt") as fs:
        fs.write("A = 3")
    with sys_path_reset():
        r = cli_runner.invoke(["ext-run", tmp_path / "three_a.py"])
    assert r.exit_code == 1, r
    assert "does not have a main(ctx, args) function" in r.stderr

    # working example
    with open(tmp_path / "three.py", "wt") as fs:
        fs.write(
            "\n".join(
                [
                    "import json",
                    "import kart",
                    "import three_a",
                    "def main(ctx, args):",
                    "  print(json.dumps([",
                    "    repr(ctx), args,",
                    "    bool(kart.is_frozen), three_a.A,",
                    "    __file__, __name__",
                    "  ]))",
                ]
            )
        )
    with sys_path_reset():
        r = cli_runner.invoke(["ext-run", tmp_path / "three.py", "arg1", "arg2"])
    print(r.stdout)
    print(r.stderr)
    assert r.exit_code == 0, r

    sctx, sargs, val1, val2, sfile, sname = json.loads(r.stdout)
    assert sctx.startswith("<click.core.Context object")
    assert sargs == ["arg1", "arg2"]
    assert (val1, val2) == (False, 3)
    assert Path(sfile) == (tmp_path / "three.py")
    assert sname == "kart.ext_run.three"
