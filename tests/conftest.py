import sys
from pathlib import Path

# Tests import `src.*`, so the package root has to be importable.
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

# Unlike the free server, load_settings() has no required variables: this
# process holds no upstream credential, because the caller supplies their own
# key per request. So there is nothing to seed here, and that absence is
# itself worth asserting -- see test_settings.py.

import pytest  # noqa: E402


@pytest.fixture(autouse=True)
def _fresh_process_state():
    """Reset the three process-global caches between tests.

    Day 3 added state that deliberately outlives one request: the rate
    limiter's counters, the registration sweep's clock, and the CIMD document
    cache. All three are per PROCESS in production, which is correct there and
    wrong in a test session, where one file's tenth registration would
    otherwise be throttled by another file's nine. Cleared here rather than in
    each test, so a new test cannot forget.
    """
    from src import cimd, oauth, ratelimit

    ratelimit.LIMITER.reset()
    oauth.reset_sweep_clock()
    cimd.clear_cache()
    yield
    ratelimit.LIMITER.reset()
    oauth.reset_sweep_clock()
    cimd.clear_cache()
