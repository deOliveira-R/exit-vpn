"""The exit check (component): before loading a page, traffic must leave through the VPN, in the expected country."""

import pytest

from prober.exitcheck import ExitCheck, ExitCheckError

from harness import trace_fetch

CH = (200, "ip=203.0.113.7\nloc=CH\n", "")


async def test_retries_through_the_warm_up_and_reports_the_exit(no_wait):
    info = await ExitCheck(trace_fetch((None, "", "ConnectError"), CH)).verify("ch", set())
    assert (info.ip, info.loc) == ("203.0.113.7", "CH")


async def test_a_forbidden_exit_ip_is_refused(no_wait):
    with pytest.raises(ExitCheckError, match="forbidden"):
        await ExitCheck(trace_fetch(CH)).verify("CH", {"203.0.113.7"})


async def test_an_exit_in_another_country_is_refused(no_wait):
    with pytest.raises(ExitCheckError, match="expected US"):
        await ExitCheck(trace_fetch(CH)).verify("US", set())


async def test_no_expected_country_means_no_country_check(no_wait):
    assert (await ExitCheck(trace_fetch(CH)).verify("", set())).loc == "CH"


async def test_no_traffic_within_the_warm_up_is_an_error(no_wait):
    with pytest.raises(ExitCheckError, match="no traffic"):
        await ExitCheck(trace_fetch((None, "", "ConnectError")), warmup=0).verify("CH", set())
