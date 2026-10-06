"""The ownership tag is keyed by Railyard instance + project id, exactly as the NetBox plugin keys it."""

import pytest

from railyard_sync.export.policy import TAG_MARKER, insecure_url, instance_key, ownership_tag


def test_tag_is_keyed_by_project_id_not_name():
    a = ownership_tag("https://railyard.sh", "prj_a", "Same name")
    b = ownership_tag("https://railyard.sh", "prj_b", "Same name")
    assert a.slug != b.slug and a.name != b.name and a.description != b.description


def test_tag_is_keyed_by_instance():
    a = ownership_tag("https://railyard.sh", "prj_a", "X")
    b = ownership_tag("https://railyard.example.com", "prj_a", "X")
    assert a.slug != b.slug


def test_rename_changes_only_the_display_name():
    a = ownership_tag("https://railyard.sh", "prj_a", "Old")
    b = ownership_tag("https://railyard.sh", "prj_a", "New")
    assert (a.slug, a.description) == (b.slug, b.description)
    assert a.name != b.name


def test_tag_fits_netbox_limits():
    spec = ownership_tag("https://" + "h" * 300 + ".example", "p" * 300, "n" * 300)
    assert len(spec.slug) <= 100 and len(spec.name) <= 100 and len(spec.description) <= 200


def test_tag_matches_the_plugin_byte_for_byte():
    # The plugin (netbox_railyard/policy.py) computes the same values; a change here orphans every
    # object a previous sync created, so it is pinned.
    spec = ownership_tag("https://railyard.example", "prj_cable", "Cabling fixture")
    assert spec.slug.startswith("ry-prj-cable-") and len(spec.slug) == len("ry-prj-cable-") + 10
    assert spec.description == f"Managed by the Railyard sync. {TAG_MARKER} project=prj_cable instance=railyard.example"


def test_instance_key_normalises_scheme_port_and_slash():
    assert instance_key("https://Railyard.sh/") == instance_key("http://railyard.sh") == "railyard.sh"
    assert instance_key("https://railyard.sh:443") == "railyard.sh"
    assert instance_key("https://ry.local:8443/base/") == "ry.local:8443/base"


def test_project_without_id_is_refused():
    with pytest.raises(ValueError):
        ownership_tag("https://railyard.sh", "", "Name only")


def test_insecure_url():
    assert not insecure_url("https://netbox.example.com")
    assert not insecure_url("http://localhost:8000")
    assert insecure_url("http://netbox.internal")
