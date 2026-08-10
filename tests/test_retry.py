from __future__ import annotations

import xmlrpc.client

import pytest

from openedu_orchestrator.openeducat_client import _call_with_retry


@pytest.fixture(autouse=True)
def no_real_sleep(monkeypatch):
    """Retry backoff really does call time.sleep -- stub it so tests run instantly
    instead of actually waiting out the exponential delays.
    """
    monkeypatch.setattr("openedu_orchestrator.openeducat_client.time.sleep", lambda _seconds: None)


def test_succeeds_first_try_without_retrying():
    calls = []

    def fn():
        calls.append(1)
        return "ok"

    assert _call_with_retry(fn, max_attempts=3, base_delay=0.01) == "ok"
    assert len(calls) == 1


def test_retries_transient_failure_then_succeeds():
    calls = []

    def fn():
        calls.append(1)
        if len(calls) < 3:
            raise ConnectionError("connection reset")
        return "ok"

    assert _call_with_retry(fn, max_attempts=5, base_delay=0.01) == "ok"
    assert len(calls) == 3


def test_raises_after_exhausting_max_attempts():
    calls = []

    def fn():
        calls.append(1)
        raise TimeoutError("timed out")

    with pytest.raises(TimeoutError):
        _call_with_retry(fn, max_attempts=3, base_delay=0.01)
    assert len(calls) == 3


def test_does_not_retry_application_level_fault():
    """xmlrpc.client.Fault is Odoo's own rejection (e.g. a failed validation) --
    retrying would just repeat the same rejection, so it must not be caught.
    """
    calls = []

    def fn():
        calls.append(1)
        raise xmlrpc.client.Fault(2, "Registration Number must be unique per student!")

    with pytest.raises(xmlrpc.client.Fault):
        _call_with_retry(fn, max_attempts=3, base_delay=0.01)
    assert len(calls) == 1


def test_does_not_retry_unrelated_exception():
    calls = []

    def fn():
        calls.append(1)
        raise ValueError("not a transient RPC error")

    with pytest.raises(ValueError):
        _call_with_retry(fn, max_attempts=3, base_delay=0.01)
    assert len(calls) == 1
