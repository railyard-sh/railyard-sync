"""Populate an empty Nautobot 2.4 with the estate the loader fixtures were captured from.

Run it in a scratch Nautobot (never a real one): ``nautobot-server shell < populate.py``, then ``capture.py``.
"""

from django.contrib.contenttypes.models import ContentType
from nautobot.circuits.models import Circuit, CircuitTermination, CircuitType, Provider
from nautobot.dcim.models import (
    Cable,
    ConsolePort,
    Device,
    DeviceBay,
    DeviceBayTemplate,
    DeviceType,
    FrontPort,
    FrontPortTemplate,
    Interface,
    InterfaceTemplate,
    Location,
    LocationType,
    Manufacturer,
    PowerFeed,
    PowerOutlet,
    PowerOutletTemplate,
    PowerPanel,
    PowerPort,
    PowerPortTemplate,
    Rack,
    RearPort,
    RearPortTemplate,
)
from nautobot.extras.models import CustomField, Role, Status, Tag
from nautobot.tenancy.models import Tenant
from nautobot.users.models import Token, User

ct = lambda label: ContentType.objects.get_by_natural_key(*label.split("."))  # noqa: E731

active = Status.objects.get(name="Active")
planned = Status.objects.get(name="Planned")
connected = Status.objects.get(name="Connected")

region_t = LocationType.objects.create(name="Region", nestable=True)
site_t = LocationType.objects.create(name="Site", parent=region_t)
building_t = LocationType.objects.create(name="Building", parent=site_t)
room_t = LocationType.objects.create(name="Room", parent=building_t)
for t in (site_t, building_t, room_t):
    t.content_types.set([ct("dcim.rack"), ct("dcim.device"), ct("dcim.powerpanel"), ct("circuits.circuittermination")])

europe = Location.objects.create(name="Europe", location_type=region_t, status=active)
uk = Location.objects.create(name="United Kingdom", location_type=region_t, parent=europe, status=active)
ldn1 = Location.objects.create(
    name="LDN1", location_type=site_t, parent=uk, status=active, facility="Equinix LD8", description="London one"
)
bldg = Location.objects.create(name="Building A", location_type=building_t, parent=ldn1, status=active)
hall = Location.objects.create(name="Data Hall 1", location_type=room_t, parent=bldg, status=planned, facility="DH1")
man1 = Location.objects.create(name="MAN1", location_type=site_t, parent=uk, status=active)

prod = Tag.objects.create(name="production", color="00ff00")
prod.content_types.set([ct("dcim.rack"), ct("dcim.device"), ct("dcim.location"), ct("dcim.cable")])
core = Tag.objects.create(name="core", color="ff0000")
core.content_types.set([ct("dcim.device")])
ldn1.tags.set([prod])

net_role = Role.objects.create(name="Network", color="0000ff")
net_role.content_types.set([ct("dcim.rack")])
leaf_role = Role.objects.create(name="Leaf switch", color="00ffff")
leaf_role.content_types.set([ct("dcim.device")])
srv_role = Role.objects.create(name="Server", color="aaaaaa")
srv_role.content_types.set([ct("dcim.device")])
pdu_role = Role.objects.create(name="PDU", color="aaaa00")
pdu_role.content_types.set([ct("dcim.device")])
patch_role = Role.objects.create(name="Patch panel", color="999999")
patch_role.content_types.set([ct("dcim.device")])

cf = CustomField.objects.create(key="support_contract", label="Support contract", type="text")
cf.content_types.set([ct("dcim.device"), ct("dcim.rack")])

arista = Manufacturer.objects.create(name="Arista")
dell = Manufacturer.objects.create(name="Dell")
apc = Manufacturer.objects.create(name="APC")
generic = Manufacturer.objects.create(name="Generic")

switch_t = DeviceType.objects.create(
    manufacturer=arista, model="DCS-7050SX3-48YC8", u_height=1, is_full_depth=True, part_number="DCS-7050SX3-48YC8-R"
)
for i in (1, 2):
    InterfaceTemplate.objects.create(device_type=switch_t, name=f"Ethernet{i}", type="25gbase-x-sfp28")
InterfaceTemplate.objects.create(device_type=switch_t, name="Management1", type="1000base-t", mgmt_only=True)
PowerPortTemplate.objects.create(device_type=switch_t, name="PSU1", type="iec-60320-c14", maximum_draw=300)
PowerPortTemplate.objects.create(device_type=switch_t, name="PSU2", type="iec-60320-c14", maximum_draw=300)

server_t = DeviceType.objects.create(manufacturer=dell, model="PowerEdge R650", u_height=1, is_full_depth=True)
InterfaceTemplate.objects.create(device_type=server_t, name="eno1", type="10gbase-x-sfpp")
PowerPortTemplate.objects.create(
    device_type=server_t, name="PSU1", type="iec-60320-c14", maximum_draw=800, allocated_draw=400
)

pdu_t = DeviceType.objects.create(manufacturer=apc, model="AP8868", u_height=0, is_full_depth=False)
PowerPortTemplate.objects.create(device_type=pdu_t, name="Input", type="iec-60309-p-n-e-6h")
for i in (1, 2, 3):
    PowerOutletTemplate.objects.create(device_type=pdu_t, name=f"Outlet {i}", type="iec-60320-c13", feed_leg="A")

panel_t = DeviceType.objects.create(manufacturer=generic, model="24-port LC panel", u_height=1, is_full_depth=False)
for i in (1, 2):
    rp = RearPortTemplate.objects.create(device_type=panel_t, name=f"R{i}", type="lc", positions=1)
    FrontPortTemplate.objects.create(
        device_type=panel_t, name=f"F{i}", type="lc", rear_port_template=rp, rear_port_position=1
    )

chassis_t = DeviceType.objects.create(manufacturer=dell, model="MX7000", u_height=7, subdevice_role="parent")
DeviceBayTemplate.objects.create(device_type=chassis_t, name="Slot 1")
blade_t = DeviceType.objects.create(manufacturer=dell, model="MX750c", u_height=0, subdevice_role="child")

r_a01 = Rack.objects.create(
    name="A01",
    location=hall,
    status=active,
    role=net_role,
    u_height=42,
    width=19,
    type="4-post-cabinet",
    outer_width=600,
    outer_depth=1070,
    outer_unit="mm",
    serial="AR3300-0001",
    asset_tag="LDN1-R-A01",
    facility_id="LD8-0401-A01",
    comments="Row A rack 1",
    _custom_field_data={"support_contract": "RK-1"},
)
r_a01.tags.set([prod])
r_a02 = Rack.objects.create(
    name="A02",
    location=hall,
    status=active,
    u_height=42,
    width=19,
    desc_units=True,
    outer_width=24,
    outer_depth=42,
    outer_unit="in",
)
r_b01 = Rack.objects.create(name="B01", location=bldg, status=planned, u_height=45, width=23, type="2-post-frame")
r_man = Rack.objects.create(name="M01", location=man1, status=active, u_height=42)

tenant = Tenant.objects.create(name="Acme Ltd")
leaf1 = Device.objects.create(
    name="ldn1-leaf1",
    device_type=switch_t,
    role=leaf_role,
    location=hall,
    rack=r_a01,
    position=40,
    face="front",
    status=active,
    serial="JPE21130456",
    asset_tag="LDN1-0040",
    tenant=tenant,
    comments="Leaf for row A.",
    _custom_field_data={"support_contract": "SC-1001"},
)
leaf1.tags.set([prod, core])
srv1 = Device.objects.create(
    name="ldn1-srv1",
    device_type=server_t,
    role=srv_role,
    location=hall,
    rack=r_a01,
    position=10,
    face="rear",
    status=planned,
)
pdu1 = Device.objects.create(
    name="ldn1-pdu-a01", device_type=pdu_t, role=pdu_role, location=hall, rack=r_a01, status=active
)
pp1 = Device.objects.create(
    name="ldn1-pp1",
    device_type=panel_t,
    role=patch_role,
    location=hall,
    rack=r_a01,
    position=42,
    face="front",
    status=active,
)
chassis = Device.objects.create(
    name="ldn1-chassis",
    device_type=chassis_t,
    role=srv_role,
    location=hall,
    rack=r_a02,
    position=1,
    face="front",
    status=active,
)
blade = Device.objects.create(
    name="ldn1-blade1", device_type=blade_t, role=srv_role, location=hall, rack=r_a02, status=active
)
bay = DeviceBay.objects.get(device=chassis, name="Slot 1")
bay.installed_device = blade
bay.save()
oob = Device.objects.create(name="ldn1-oob", device_type=server_t, role=srv_role, location=bldg, status=planned)
ConsolePort.objects.create(device=leaf1, name="Console", type="rj-45")
man_dev = Device.objects.create(
    name="man1-leaf1",
    device_type=switch_t,
    role=leaf_role,
    location=man1,
    rack=r_man,
    position=1,
    face="front",
    status=active,
)

# Cables: leaf Ethernet1 -> panel F1; panel R1 -> server eno1; server PSU1 -> PDU outlet 1; PDU input -> feed;
# leaf Ethernet2 -> MAN1 leaf (outside); circuit to leaf mgmt.
i_e1 = Interface.objects.get(device=leaf1, name="Ethernet1")
i_e2 = Interface.objects.get(device=leaf1, name="Ethernet2")
f1 = FrontPort.objects.get(device=pp1, name="F1")
r1 = RearPort.objects.get(device=pp1, name="R1")
eno1 = Interface.objects.get(device=srv1, name="eno1")
psu = PowerPort.objects.get(device=srv1, name="PSU1")
out1 = PowerOutlet.objects.get(device=pdu1, name="Outlet 1")
pdu_in = PowerPort.objects.get(device=pdu1, name="Input")
man_e1 = Interface.objects.get(device=man_dev, name="Ethernet1")
mgmt = Interface.objects.get(device=leaf1, name="Management1")

c1 = Cable(
    termination_a=i_e1,
    termination_b=f1,
    status=connected,
    type="mmf-om4",
    label="L1",
    color="ff0000",
    length=3,
    length_unit="m",
)
c1.save()
c1.tags.set([prod])
Cable(
    termination_a=r1, termination_b=eno1, status=planned, type="mmf-om4", label="L2", length=150, length_unit="cm"
).save()
Cable(termination_a=psu, termination_b=out1, status=connected, type="power").save()
Cable(termination_a=i_e2, termination_b=man_e1, status=connected, type="smf-os2", label="WAN").save()

panel = PowerPanel.objects.create(name="PP-A", location=hall)
feed_a = PowerFeed.objects.create(
    name="Feed A",
    power_panel=panel,
    rack=r_a01,
    status=active,
    type="primary",
    supply="ac",
    phase="three-phase",
    voltage=400,
    amperage=32,
    max_utilization=80,
)
PowerFeed.objects.create(
    name="Feed B",
    power_panel=panel,
    rack=r_a01,
    status=active,
    type="redundant",
    supply="ac",
    phase="single-phase",
    voltage=230,
    amperage=16,
    max_utilization=80,
)
Cable(termination_a=pdu_in, termination_b=feed_a, status=connected, type="power").save()

provider = Provider.objects.create(name="Colt")
ctype = CircuitType.objects.create(name="Internet")
circuit = Circuit.objects.create(cid="COLT-1", provider=provider, circuit_type=ctype, status=active)
term = CircuitTermination.objects.create(circuit=circuit, term_side="A", location=ldn1)
Cable(termination_a=mgmt, termination_b=term, status=connected, label="Uplink").save()

admin = User.objects.create_superuser("admin", "admin@example.com", "admin")
Token.objects.create(user=admin, key="0f" * 20)  # the fake token the tests use
print("populated")
