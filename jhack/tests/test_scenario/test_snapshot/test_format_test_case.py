"""Regression tests for jhack.scenario.snapshot.format_test_case / PYTEST_TEST_TEMPLATE.

See: jhack_snapshot_exercise/JHACK_SNAPSHOT_REPORT.md, issue #2.

When no charm type name could be guessed, the template used to substitute a
placeholder value that included a trailing comma and comment
(``CHARM_TYPE,  # TODO: replace with charm type name``) directly into a
``from charm import ...`` statement. That produces invalid Python
(``SyntaxError: trailing comma not allowed without surrounding parentheses``),
so ``black.format_str`` always failed and jhack silently fell back to
unformatted output on every single ``-f pytest`` invocation.
"""
import ast

from scenario import State

from jhack.scenario.snapshot import format_test_case


def test_format_test_case_without_charm_type_name_is_valid_python():
    """The generated pytest test case must always be syntactically valid,
    even when jhack could not guess the charm's type name."""
    state = State()

    output = format_test_case(state, charm_type_name=None)

    # This used to raise SyntaxError because of the stray trailing comma
    # emitted by the old placeholder inside `from charm import {ct}`.
    ast.parse(output)


def test_format_test_case_placeholder_does_not_break_import_statement():
    state = State()

    output = format_test_case(state, charm_type_name=None)

    assert "from charm import CHARM_TYPE" in output
    # the TODO hint should still be present, just not inline in the import.
    assert "TODO: replace with charm type name" in output


def test_format_test_case_with_real_charm_type_name_is_valid_python():
    state = State()

    output = format_test_case(state, charm_type_name="MyCharm")

    ast.parse(output)
    assert "from charm import MyCharm" in output
