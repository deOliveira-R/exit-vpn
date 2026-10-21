"""The health state machine: strikes, warm-up, grace after a switch, backoff (logic)."""

from exit_vpn.health import Backoff, Health, HealthTracker


def test_three_failed_cycles_in_a_row_mean_failover():
    t = HealthTracker(strikes=3, warmup=20)
    t.feed(True, 0)
    assert t.state == Health.HEALTHY
    assert t.feed(False, 5) is False and t.state == Health.SUSPECT
    assert t.feed(False, 10) is False
    assert t.feed(False, 15) is True and t.state == Health.FAILING_OVER


def test_a_success_resets_the_strikes():
    t = HealthTracker(3, 20)
    t.feed(False, 0); t.feed(False, 5); t.feed(True, 10)
    assert t.state == Health.HEALTHY
    assert not t.feed(False, 15) and not t.feed(False, 20)


def test_failures_during_the_warm_up_do_not_count():
    t = HealthTracker(3, 20)
    t.start_grace(100)
    assert t.state == Health.STARTING
    for now in (100, 105, 110, 115):
        assert t.feed(False, now) is False
    assert t.strikes == 0 and t.state == Health.STARTING
    t.feed(False, 121); t.feed(False, 126)
    assert t.feed(False, 131) is True


def test_since_changes_only_when_the_state_does():
    t = HealthTracker(3, 0)
    t.feed(True, 1); t.feed(True, 2)
    assert t.since == 1


def test_backoff_doubles_up_to_its_cap_and_resets():
    b = Backoff(30, 300)
    assert [b.step() for _ in range(6)] == [30, 60, 120, 240, 300, 300]
    b.reset()
    assert b.step() == 30


def test_the_grace_after_a_switch_ends_at_the_first_success():
    """After a switch, a server that passed once and then dies is caught at once."""
    t = HealthTracker(3, 20)
    t.start_grace(100)
    t.feed(True, 104)
    t.feed(False, 105); t.feed(False, 110)
    assert t.feed(False, 115) is True
