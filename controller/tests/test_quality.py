"""A tunnel's quality from live measurement: the window, the states and their thresholds,
the damping of reported changes (logic: the pure meter and tracker)."""

from exit_vpn.quality import Quality, QualityMeter, QualityTracker, Thresholds, percentile


def meter(**kw) -> QualityMeter:
    return QualityMeter(Thresholds(**kw))


def pings(m: QualityMeter, rtts, t0: float = 0.0) -> float:
    t = t0
    for r in rtts:
        t += 1
        m.ping(t, r)
    return t


def test_a_steady_server_is_healthy_once_enough_pings_tell():
    m = meter()
    t = pings(m, [20.0] * 29)
    assert m.assess(t)[0] == Quality.MEASURING
    t = pings(m, [20.0], t)
    q, reasons, s = m.assess(t)
    assert (q, reasons) == (Quality.HEALTHY, [])
    assert s.rtt_p50 == 20.0 and s.loss == 0 and s.jitter == 0


def test_a_total_drop_is_critical_within_five_pings():
    m = meter()
    t = pings(m, [20.0] * 60)
    t = pings(m, [None] * 4, t)
    assert m.assess(t)[0] == Quality.DEGRADED, "four lost: loss, not yet a drop (4/30 recent < 20%)"
    t = pings(m, [None], t)
    q, reasons, _ = m.assess(t)
    assert q == Quality.CRITICAL and "5 pings lost in a row" in reasons


def test_scattered_loss_is_degraded_and_heavy_recent_loss_critical():
    m = meter()
    t = pings(m, ([20.0] * 24 + [None]) * 4)   # 4% loss, never two in a row
    q, reasons, _ = m.assess(t)
    assert q == Quality.DEGRADED and reasons == ["4.0% loss"]
    t = pings(m, [20.0, None, None] * 10, t)   # 2 of 3 lost for 30 s
    assert m.assess(t)[0] == Quality.CRITICAL


def test_a_single_lost_ping_is_not_loss_even_in_a_short_window():
    m = meter()
    t = pings(m, [20.0] * 30 + [None] + [20.0] * 19)   # 1 of 50: 2%, as after a restart
    assert m.assess(t)[:2] == (Quality.HEALTHY, [])
    t = pings(m, [None] + [20.0] * 9, t)               # 2 of 60
    assert m.assess(t)[:2] == (Quality.DEGRADED, ["3.3% loss"])


def test_standing_queues_and_jitter_are_degraded():
    m = meter()
    t = pings(m, [20.0] * 50 + [150.0] * 10)
    q, reasons, _ = m.assess(t)
    assert q == Quality.DEGRADED and reasons == ["RTT p90 150 ms vs min 20 ms"]
    m = meter()
    t = pings(m, [20.0, 60.0] * 30)
    q, reasons, _ = m.assess(t)
    assert q == Quality.DEGRADED and "jitter 40 ms" in reasons


def test_slow_https_is_degraded_and_failing_https_critical():
    m = meter()
    t = pings(m, [20.0] * 40)
    for i in range(6):
        m.cycle(t - 30 + 5 * i, 2500.0, 30.0)
    q, reasons, s = m.assess(t)
    assert q == Quality.DEGRADED and reasons == ["HTTPS 2500 ms"] and s.dns_p50 == 30.0
    for i in range(6):
        m.cycle(t + 1 + 5 * i, None, None)
    q, reasons, _ = m.assess(t + 31)
    assert q == Quality.CRITICAL and "every HTTPS check failed" in reasons


def test_the_window_forgets_old_samples_and_a_reset_forgets_all():
    m = meter(window=60)
    t = pings(m, [None] * 3 + [20.0] * 100)
    s = m.summary(t)
    assert s.pings == 61 and s.lost == 0
    m.reset()
    assert m.summary(t).pings == 0 and m.assess(t)[0] == Quality.MEASURING


def test_percentiles_are_nearest_rank():
    assert percentile([], 50) is None
    assert percentile([1.0, 2.0, 3.0, 4.0], 50) == 2.0
    assert percentile([float(i) for i in range(1, 11)], 90) == 9.0


def test_worse_states_count_at_once_better_ones_after_the_dwell():
    tr = QualityTracker(dwell=10)
    assert tr.feed(Quality.HEALTHY, [], 0), "from measuring: at once"
    assert tr.feed(Quality.CRITICAL, ["drop"], 1) and tr.state == Quality.CRITICAL
    assert not tr.feed(Quality.HEALTHY, [], 2)
    assert not tr.feed(Quality.HEALTHY, [], 11), "nine seconds: not yet"
    assert tr.feed(Quality.HEALTHY, [], 12) and tr.since == 12
    assert tr.feed(Quality.DEGRADED, ["x"], 13) and tr.state == Quality.DEGRADED
    tr.reset(20)
    assert tr.state == Quality.MEASURING
