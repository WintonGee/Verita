"""
Meta-test: greps the source tree for `.unsafe_all_tenants(` and asserts the
call sites are exactly the documented allowlist. The point isn't to prevent
the bypass — it's to make sure adding a new call site requires a deliberate
edit here, where a reviewer will notice it.
"""

import re
import subprocess

import pytest


ALLOWED_UNSAFE_FILES = {
    "tests/factories.py",      # test fixtures need to create rows without a scope
    "tests/test_tenant_scoping.py",  # the tests that verify both branches
    "tests/test_unsafe_allowlist.py",  # this file (it's quoting the method name)
    "tests/test_concurrency.py",  # concurrency tests count Event rows across tenants
    # Production allowlist (filled in as we implement them):
    # "apps/billing/management/commands/aggregate_events.py",
    # "apps/billing/management/commands/issue_invoices.py",
    # "apps/billing/management/commands/run_reconciliation.py",
    # "apps/ops_api/views.py",
}


@pytest.mark.parametrize("dummy", [None])
def test_unsafe_all_tenants_call_sites_are_in_allowlist(dummy):  # noqa: ARG001
    """
    Run from the backend/ directory; if grep finds a call site not in the
    allowlist, this test fails loudly with the new offender.
    """
    # Look for actual queryset usage (Model.objects.unsafe_all_tenants(...),
    # or queryset.unsafe_all_tenants(...)) — not docstring/comment references.
    # The pattern requires the call to be preceded by an identifier and a
    # closing paren (e.g. `objects.unsafe_all_tenants(` or `).unsafe_all_tenants(`).
    try:
        result = subprocess.run(
            [
                "grep", "-rnE", "--include=*.py",
                r"(objects|\))\.unsafe_all_tenants\(",
                ".",
            ],
            capture_output=True, text=True, cwd="/app",
        )
    except FileNotFoundError:
        pytest.skip("grep not available")

    if result.returncode not in (0, 1):
        pytest.fail(f"grep failed: {result.stderr}")

    # Each line: "./path/to/file.py:linenum:matched_text"
    lines = [ln for ln in result.stdout.splitlines() if ln.strip()]
    offending_files = set()
    for ln in lines:
        match = re.match(r"\./(.+?):\d+:(.*)$", ln)
        if not match:
            continue
        path, code = match.group(1), match.group(2)
        # Skip lines inside docstrings — heuristic: line starts with `f"` or
        # contains the string inside obvious doc/error-message delimiters.
        # The simpler rule: only flag lines where the substring is real code,
        # i.e., NOT preceded by `"` or `'` in the same line.
        if re.search(r"""['"`].*?(objects|\)).?unsafe_all_tenants""", code):
            continue
        if path not in ALLOWED_UNSAFE_FILES:
            offending_files.add(path)

    assert not offending_files, (
        f"Found .unsafe_all_tenants( call sites NOT in the allowlist:\n"
        f"  {sorted(offending_files)}\n"
        f"Either add them to ALLOWED_UNSAFE_FILES with justification, "
        f"or rewrite the call to use .for_customer()."
    )
