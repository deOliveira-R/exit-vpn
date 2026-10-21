"""Provider levers: parsing, Proton features, and which value wins (logic)."""

import pytest

from exitctl.model import ExitctlError, resolve_levers
from exitctl.providers.proton import LEVERS, features

BY_KEY = {lv.key: lv for lv in LEVERS}


def test_a_lever_parses_its_values():
    assert BY_KEY["netshield"].parse("2") == 2 and BY_KEY["accelerator"].parse("off") is False


def test_a_lever_rejects_a_value_out_of_range():
    with pytest.raises(ExitctlError):
        BY_KEY["netshield"].parse("3")


def test_levers_become_protons_certificate_features():
    assert features({"netshield": 1, "accelerator": True, "moderate_nat": True, "port_forward": False}) == {
        "NetShieldLevel": 1, "SplitTCP": True, "RandomNAT": False, "PortForwarding": False}


def test_levers_are_read_from_a_configs_option_comments():
    assert resolve_levers(LEVERS, conf_options={"NetShield": "1", "Moderate NAT": "on",
                                                "VPN Accelerator": "off"}) == {
        "netshield": 1, "moderate_nat": True, "accelerator": False, "port_forward": False}


def test_recorded_values_win_over_the_config_and_overrides_win_over_both():
    assert resolve_levers(LEVERS, {"netshield": 2}, {"NetShield": "1"}, {"accelerator": False}) == {
        "netshield": 2, "accelerator": False, "moderate_nat": False, "port_forward": False}
