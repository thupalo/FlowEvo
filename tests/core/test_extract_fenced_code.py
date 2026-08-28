from __future__ import annotations

import sys
from pathlib import Path

SRC = Path(__file__).resolve().parents[2] / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

from core.utils import extract_fenced_code  # noqa: E402


def test_python_fence_preserves_indentation():
    text = "Sure:\n```python\n\n    return x + 1\n```\nDone."
    assert extract_fenced_code(text) == "    return x + 1"


def test_untagged_fence_is_accepted():
    assert extract_fenced_code("```\ndef f():\n    pass\n```") == "def f():\n    pass"


def test_prefers_python_tag_over_other_tags():
    text = "```text\nexplanation\n```\n```python\ndef f(): pass\n```"
    assert extract_fenced_code(text) == "def f(): pass"


def test_other_tag_used_when_no_python_block():
    assert extract_fenced_code("```py\nx = 1\n```") == "x = 1"


def test_unfenced_code_is_returned():
    assert extract_fenced_code("def f(a):\n    return a") == "def f(a):\n    return a"
    assert extract_fenced_code("    return sorted(xs)") == "    return sorted(xs)"


def test_prose_yields_empty():
    assert extract_fenced_code("I cannot help with that.") == ""
    assert extract_fenced_code("The answer depends on the input; which case do you mean?") == ""
    assert extract_fenced_code("") == ""


def test_truncated_reply_before_fence_yields_empty():
    assert extract_fenced_code("Let me think about this problem step by step. First, we") == ""


def test_open_fence_without_close_falls_back_to_raw_if_codey():
    text = "```python\ndef f():\n    return 1"
    # no closing fence -> not a block; raw text looks like code -> returned as is
    assert extract_fenced_code(text) == text
