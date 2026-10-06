"""The device-type resolver prefers the netbox-community library YAML, falls back to the catalogue."""

from railyard_sync.export.devicetype_library import DeviceTypeLibrary

CATALOGUE_ENTRY = {
    "key": "cisco-nexus-93180yc-fx",
    "manufacturer": "Cisco",
    "model": "Nexus 93180YC-FX",
    "uHeight": 1,
    "fullDepth": True,
    "partNumber": "N9K-C93180YC-FX",
}

LIBRARY_YAML = """
manufacturer: Cisco
model: Nexus 93180YC-FX
slug: cisco-nexus-93180yc-fx
part_number: N9K-C93180YC-FX
u_height: 1
is_full_depth: true
interfaces:
  - name: Ethernet1/1
    type: 25gbase-x-sfp28
power-ports:
  - name: PSU1
    type: iec-60320-c14
"""


def test_resolves_from_library_when_present():
    calls = []

    def fetcher(url):
        calls.append(url)
        return LIBRARY_YAML

    lib = DeviceTypeLibrary(ref="master", fetcher=fetcher)
    spec = lib.resolve(CATALOGUE_ENTRY)

    assert spec.source == "library"
    assert spec.slug == "cisco-nexus-93180yc-fx"
    assert spec.u_height == 1
    assert spec.is_full_depth is True
    assert spec.part_number == "N9K-C93180YC-FX"
    assert "interfaces" in spec.components and spec.components["interfaces"][0]["name"] == "Ethernet1/1"
    # the URL points at device-types/<Manufacturer>/<Model>.yaml on the pinned ref
    assert calls[0] == (
        "https://raw.githubusercontent.com/netbox-community/devicetype-library/master/"
        "device-types/Cisco/Nexus%2093180YC-FX.yaml"
    )


def test_falls_back_to_catalogue_when_missing():
    lib = DeviceTypeLibrary(fetcher=lambda url: None)  # library has no such file
    spec = lib.resolve(CATALOGUE_ENTRY)

    assert spec.source == "catalogue"
    assert spec.manufacturer == "Cisco"
    assert spec.model == "Nexus 93180YC-FX"
    assert spec.slug == "cisco-nexus-93180yc-fx"  # the Railyard key is the library slug
    assert spec.u_height == 1
    assert spec.part_number == "N9K-C93180YC-FX"
    assert spec.components == {}


def test_results_are_memoised():
    count = {"n": 0}

    def fetcher(url):
        count["n"] += 1
        return None

    lib = DeviceTypeLibrary(fetcher=fetcher)
    lib.resolve(CATALOGUE_ENTRY)
    lib.resolve(CATALOGUE_ENTRY)
    assert count["n"] == 1  # second resolve served from memory


def test_default_ref_is_a_pinned_commit():
    from railyard_sync.export.devicetype_library import DEFAULT_REF

    lib = DeviceTypeLibrary(fetcher=lambda url: None)
    assert lib.ref == DEFAULT_REF
    assert len(DEFAULT_REF) == 40 and all(c in "0123456789abcdef" for c in DEFAULT_REF)


def test_path_segments_are_fully_escaped():
    calls = []
    lib = DeviceTypeLibrary(fetcher=lambda url: calls.append(url))
    lib.resolve({**CATALOGUE_ENTRY, "manufacturer": "A/B", "model": "../x?y#z"})
    assert calls == [
        "https://raw.githubusercontent.com/netbox-community/devicetype-library/"
        "0451a7c5977351e7a43fbff54eb44f26ed85344f/device-types/A%2FB/..%2Fx%3Fy%23z.yaml"
    ]


def test_dot_segments_never_reach_the_fetcher():
    calls = []
    lib = DeviceTypeLibrary(fetcher=lambda url: calls.append(url))
    spec = lib.resolve({**CATALOGUE_ENTRY, "manufacturer": "..", "model": "x"})
    assert calls == []
    assert spec.source == "catalogue"


def test_invalid_ref_is_rejected():
    import pytest

    for bad in ("../../evil", "master?x", "a b", "/abs"):
        with pytest.raises(ValueError):
            DeviceTypeLibrary(ref=bad, fetcher=lambda url: None)
