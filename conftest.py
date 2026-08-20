"""Root conftest: its presence puts the repo root on sys.path.

That makes the shipped Python modules (review/) importable from the tests
without packaging metadata, which T1 deliberately avoids adding.

It also carries the session-wide skip gate (T26). pytest reports a skipped
test as neither a pass nor a failure, so a test that quietly stops running -
a broken environment probe, a stale ``skipif`` condition - leaves a green
build with a hole in it. Here a skip is a failure.

A skip can happen in either of two places, and both are covered because
covering only one would leave the gate looking enforced while the other
path walked around it. A test that skips in its body is reported per test,
and may declare itself an exception with the ``expected_skip`` marker. A
module that skips while being imported - ``pytest.importorskip`` at module
scope, or ``pytest.skip(allow_module_level=True)`` - is reported by the
collector instead, takes every test in the file with it, and has no marker
to declare itself with: the module never finished importing, so it has no
markers at all. Those are failures unconditionally.
"""

import pytest

EXPECTED_SKIP_MARKER = "expected_skip"


def _fail_as_unexpected_skip(report: pytest.TestReport | pytest.CollectReport, advice: str) -> None:
    """Re-cast a skipped report as a failure that says what was skipped.

    Args:
        report: The report to rewrite in place.
        advice: What the reader should do about it.
    """
    reason = report.longrepr[2] if isinstance(report.longrepr, tuple) else report.longrepr
    report.outcome = "failed"
    report.longrepr = f"unexpected skip: {reason}\n{advice}"


@pytest.hookimpl(wrapper=True)
def pytest_runtest_makereport(item: pytest.Item) -> pytest.TestReport:
    """Fail a test that skipped without declaring itself an exception.

    Args:
        item: The test the report is about.

    Returns:
        The report pytest will act on.
    """
    report = yield
    if report.skipped and item.get_closest_marker(EXPECTED_SKIP_MARKER) is None:
        _fail_as_unexpected_skip(
            report,
            f"The test did not run and did not fail. If skipping is intended, "
            f"declare it with @pytest.mark.{EXPECTED_SKIP_MARKER}.",
        )
    return report


@pytest.hookimpl(wrapper=True)
def pytest_make_collect_report(collector: pytest.Collector) -> pytest.CollectReport:
    """Fail a module that skipped itself while being collected.

    Args:
        collector: The node being collected.

    Returns:
        The report pytest will act on.
    """
    report = yield
    if report.skipped:
        _fail_as_unexpected_skip(
            report,
            f"{collector.nodeid or 'The session'} skipped itself during "
            f"collection, so none of its tests ran. A module-level skip "
            f"cannot be declared with @pytest.mark.{EXPECTED_SKIP_MARKER}: "
            f"the module never finished importing. Make the tests run, or "
            f"delete them.",
        )
    return report
