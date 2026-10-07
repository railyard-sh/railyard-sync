"""The upgrade messages for a plan refusal, shared by the CLI and the NetBox plugin."""

from railyard_sync.errors import RailyardPlanLimitError, RailyardPlanRequiredError
from railyard_sync.plans import deliverable_plan_message, plan_limit_message


def test_deliverable_refusal_names_the_plan_and_the_options():
    e = RailyardPlanRequiredError(
        "no",
        plan="community",
        feature="deliverables",
        deliverable="netbox-sync",
        required_plans=["pro", "team"],
        project_pass=True,
    )
    assert deliverable_plan_message(e) == (
        "Exporting to NetBox needs a plan with deliverables: the Community plan does not include them. "
        "Upgrade to Pro or Team, or buy a Project Pass for this estate. Nothing was written to NetBox."
    )


def test_rack_limit_on_a_refresh():
    e = RailyardPlanLimitError("no", plan="community", resource="racks", limit=25, current=30, scope="estate")
    assert plan_limit_message(e, refreshing=True) == (
        "After this import the estate would have 30 racks; the Community plan allows 25 per estate. "
        "Contact Railyard about an Enterprise plan, or import fewer sites. Nothing was saved."
    )
