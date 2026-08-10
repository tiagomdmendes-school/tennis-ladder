"""Guard against syntax that only works on newer Pythons than we support.

The floor is Python 3.8, because that is what Ubuntu 20.04 ships and what the
club's Oracle Cloud box actually runs. The suite is run against 3.8, 3.10 and
3.13; the version on the server is the one that counts.

This exists because a real bug shipped: an f-string with a backslash in its
expression part, which PEP 701 made legal in 3.12 but is a SyntaxError before
it. It passed local tests on 3.13 and took the server down on 3.10.

`ast.parse(..., feature_version=(3, 10))` does NOT catch this -- feature_version
only gates a handful of grammar features and does not emulate the older
f-string tokenizer, so it reported the broken file as fine. Hence a real check.
"""

from __future__ import annotations

import pathlib
import re
import unittest

PROJECT = pathlib.Path(__file__).resolve().parent.parent
MIN_VERSION = (3, 8)

# An f-string prefix: f, rf, fr, Rf, ... immediately before a quote.
FSTRING_PREFIX = re.compile(r"""(?<![\w'"])([fF][rR]?|[rR][fF])(['"])""")


def python_files():
    for path in sorted(PROJECT.rglob("*.py")):
        if "__pycache__" in path.parts:
            continue
        yield path


def expression_parts(line: str):
    """Yield the text inside each {...} on a line of source.

    Deliberately crude -- it only has to be good enough to spot the two
    constructs below in this codebase's single-line f-string fragments, and
    over-reporting is far cheaper than another outage.
    """
    depth = 0
    current: list = []
    for char in line:
        if char == "{":
            depth += 1
            if depth == 1:
                current = []
                continue
        elif char == "}":
            if depth == 1 and current:
                yield "".join(current)
            depth = max(0, depth - 1)
            continue
        if depth >= 1:
            current.append(char)


class TestNoPost310Syntax(unittest.TestCase):
    def test_no_backslash_inside_an_fstring_expression(self):
        """Legal from 3.12 (PEP 701), a SyntaxError on everything before it."""
        offenders = []
        for path in python_files():
            for number, line in enumerate(path.read_text().splitlines(), start=1):
                if not FSTRING_PREFIX.search(line):
                    continue
                for expression in expression_parts(line):
                    if "\\" in expression:
                        offenders.append(
                            f"{path.relative_to(PROJECT)}:{number}: {line.strip()[:80]}")
        self.assertEqual(offenders, [], "\n".join(
            ["f-string expressions containing a backslash need Python 3.12+; "
             "build the value on its own line instead:"] + offenders))

    def test_no_nested_same_quotes_inside_an_fstring_expression(self):
        """f"...{d["k"]}..." is also 3.12+; before that it ends the string."""
        offenders = []
        for path in python_files():
            for number, line in enumerate(path.read_text().splitlines(), start=1):
                match = FSTRING_PREFIX.search(line)
                if not match:
                    continue
                quote = match.group(2)
                for expression in expression_parts(line[match.end():]):
                    if quote in expression:
                        offenders.append(
                            f"{path.relative_to(PROJECT)}:{number}: {line.strip()[:80]}")
        self.assertEqual(offenders, [], "\n".join(
            ["f-string expressions reusing the enclosing quote need Python "
             "3.12+; use the other quote character:"] + offenders))

    def test_the_checker_actually_catches_the_bug_that_shipped(self):
        """Without this, a broken checker silently passes everything -- which
        is exactly how the original bug reached the server."""
        bad = """f'<td>{"<span class=\\'pill\\'>x</span>" if flag else ""}</td>'"""
        self.assertTrue(FSTRING_PREFIX.search(bad))
        self.assertTrue(any("\\" in part for part in expression_parts(bad)))

    def test_the_checker_does_not_flag_ordinary_strings(self):
        fine = """archive = ('<div>You\\'re looking at ' + name)"""
        self.assertFalse(any("\\" in part for part in expression_parts(fine))
                         and FSTRING_PREFIX.search(fine))


class TestDeclaredVersion(unittest.TestCase):
    def test_the_readme_and_this_check_agree(self):
        readme = (PROJECT / "README.md").read_text()
        self.assertIn(f"Python {MIN_VERSION[0]}.{MIN_VERSION[1]}+", readme)


if __name__ == "__main__":
    unittest.main()
