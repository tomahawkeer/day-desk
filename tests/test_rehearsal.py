"""The dress rehearsal must pass on a saved day (it replays the whole desk; zero Claude calls)."""
import logging
import os
import sys

import pytest

HERE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, HERE)
import rehearsal  # noqa: E402


@pytest.mark.skipif(not rehearsal.bar_days(HERE), reason="no saved day in data/bars yet")
def test_latest_saved_day_rehearses_clean():
    logging.disable(logging.WARNING)
    try:
        r = rehearsal.rehearse(rehearsal.bar_days(HERE)[-1], HERE, seed=1)
    finally:
        logging.disable(logging.NOTSET)
    bad = [t for ok, t in r["checks"] if not ok]
    assert not bad, bad
    assert r["stats"]["buys"] >= 1


def test_rehearsal_restores_the_real_clock():
    import day_desk, health, time
    assert day_desk.time is time and health.time is time
