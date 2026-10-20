from exit_vpn.health import Backoff, Health, HealthTracker


def test_three_strikes():
    t = HealthTracker(strikes=3, warmup=20)
    t.feed(True, 0)
    assert t.state == Health.HEALTHY
    assert t.feed(False, 5) is False and t.state == Health.SUSPECT
    assert t.feed(False, 10) is False
    assert t.feed(False, 15) is True and t.state == Health.FAILING_OVER


def test_success_resets_strikes():
    t = HealthTracker(3, 20)
    t.feed(False, 0); t.feed(False, 5); t.feed(True, 10)
    assert t.state == Health.HEALTHY
    assert not t.feed(False, 15) and not t.feed(False, 20)


def test_warmup_failures_do_not_count():
    t = HealthTracker(3, 20)
    t.start_grace(100)
    assert t.state == Health.STARTING
    for now in (100, 105, 110, 115):
        assert t.feed(False, now) is False
    assert t.strikes == 0 and t.state == Health.STARTING
    t.feed(False, 121); t.feed(False, 126)
    assert t.feed(False, 131) is True


def test_since_changes_only_on_transition():
    t = HealthTracker(3, 0)
    t.feed(True, 1); t.feed(True, 2)
    assert t.since == 1


def test_backoff():
    b = Backoff(30, 300)
    assert [b.step() for _ in range(6)] == [30, 60, 120, 240, 300, 300]
    b.reset()
    assert b.step() == 30


def test_grace_ends_at_first_success():
    """After a switch, a server that passed once and then dies is caught at once."""
    t = HealthTracker(3, 20)
    t.start_grace(100)
    t.feed(True, 104)
    t.feed(False, 105); t.feed(False, 110)
    assert t.feed(False, 115) is True
