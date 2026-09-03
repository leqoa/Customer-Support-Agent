"""Smoke test so CI has at least one test to collect and run.

Real test coverage for backend modules is being added by other Phase 2 work.
This file just proves the package imports cleanly and pytest is wired up.
"""
import backend


def test_backend_package_imports():
    assert backend is not None
