import ast
import warnings
from pathlib import Path

import pytest

TOOLS = sorted((Path(__file__).resolve().parents[1] / "tools").glob("*.py"))


@pytest.mark.parametrize("path", TOOLS, ids=lambda p: p.name)
def test_tool_strings_have_no_backslash_escape_accidents(path):
    # A Windows path such as tools\relay_login.py in an ordinary string turns
    # "\r" into a carriage return and "\s" into an invalid escape sequence.
    with warnings.catch_warnings():
        warnings.simplefilter("error")  # invalid escapes only warn at compile time
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))

    for node in ast.walk(tree):
        if isinstance(node, ast.Constant) and isinstance(node.value, str):
            assert "\r" not in node.value, f"{path.name}:{node.lineno} contains a carriage return"
