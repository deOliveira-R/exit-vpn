"""The owner's preferences: a change is validated against the catalog and normalised to its
spelling, or refused whole (logic)."""

import pytest

from exit_vpn.errors import PolicyError
from exit_vpn.prefs import Prefs
from exit_vpn.servers import Region

from harness import prefs_for

PREFS = prefs_for(["Switzerland"])


def test_regions_take_the_catalogs_spelling_and_repeats_are_dropped(catalog):
    p = PREFS.updated(catalog, regions=[Region("germany"), Region("United States", "dallas"),
                                        Region("Germany")])
    assert p.regions == (Region("Germany"), Region("United States", "Dallas"))


def test_countries_are_the_regions_countries_in_order_without_repeats():
    p = prefs_for([], regions=(Region("US", "A"), Region("us"), Region("CH")))
    assert p.countries == ["US", "CH"]
    assert p.view()["countries"] == ["US", "CH"]


@pytest.mark.parametrize("regions, msg", [
    ([Region("Atlantis")], "unknown country"),
    ([Region("Germany", "Paris")], "no servers in"),
    ([], "empty list"),
])
def test_an_unknown_place_or_no_region_is_refused(catalog, regions, msg):
    with pytest.raises(PolicyError, match=msg):
        PREFS.updated(catalog, regions=regions)


def test_required_features_must_be_known_and_repeats_are_dropped(catalog):
    assert PREFS.updated(catalog, require=["port_forward", "port_forward"]).require == ("port_forward",)
    with pytest.raises(PolicyError, match="unknown features"):
        PREFS.updated(catalog, require=["teleport"])


def test_favourites_take_the_catalogs_spelling_and_repeats_are_dropped(catalog):
    name = catalog.servers[0].name
    assert PREFS.updated(catalog, favorites=[name.lower(), name]).favorites == (name,)


def test_one_bad_change_refuses_the_whole_update(catalog):
    with pytest.raises(PolicyError, match="unknown server"):
        PREFS.updated(catalog, regions=[Region("Germany")], favorites=["XX#999"])
    assert PREFS == prefs_for(["Switzerland"]), "immutable: the original is unchanged"


def test_switches_change_only_what_is_given(catalog):
    p = PREFS.updated(catalog, secure_core=True, auto_failover=False)
    assert (p.secure_core, p.auto_failover, p.allow_free, p.regions) == (True, False, False, PREFS.regions)


def test_saved_prefs_with_only_countries_become_regions():
    p = Prefs.from_dict({"countries": ["Switzerland", "Germany"], "secure_core": False}, ["United States"])
    assert p.regions == (Region("Switzerland"), Region("Germany"))
    assert Prefs.from_dict({}, ["United States"]).regions == (Region("United States"),)
