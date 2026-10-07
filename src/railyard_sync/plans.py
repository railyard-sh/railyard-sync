"""What to tell someone Railyard's plan refused (HTTP 402), worded for what they were doing.

Every 402 the client meets is a :class:`~railyard_sync.errors.RailyardPlanError` carrying the plan, the
limit and the plans (or Project Pass) that would allow the request. These turn one into the upgrade
message the CLI prints and the NetBox plugin puts in its job log, so both say the same thing.
"""

from __future__ import annotations

from .errors import RailyardPlanError, RailyardPlanLimitError, RailyardPlanRequiredError

# Display names for the plan ids Railyard reports (backend/internal/plans/catalogue.json).
PLAN_NAMES = {
    "community": "Community",
    "project-pass": "Project Pass",
    "pro": "Pro",
    "team": "Team",
    "partner": "Partner",
    "self-hosted": "Self-hosted",
    "enterprise": "Enterprise",
}


def plan_name(plan_id: str) -> str:
    return PLAN_NAMES.get(plan_id, plan_id.replace("-", " ").title() if plan_id else "current")


def _either(names: list[str]) -> str:
    return names[0] if len(names) == 1 else ", ".join(names[:-1]) + " or " + names[-1]


def upgrade_options(required_plans: list[str], project_pass: bool) -> list[str]:
    options = []
    if required_plans:
        options.append(f"upgrade to {_either([plan_name(p) for p in required_plans])}")
    if project_pass:
        options.append("buy a Project Pass for this estate")
    return options


def sentence(options: list[str]) -> str:
    """['a', 'b', 'c'] -> 'A, b, or c.'"""
    text = options[0] if len(options) == 1 else ", ".join(options[:-1]) + ", or " + options[-1]
    return text[:1].upper() + text[1:] + "."


def deliverable_plan_message(e: RailyardPlanError) -> str:
    """'Exporting to NetBox needs a plan with deliverables: the Community plan does not include them. Upgrade to
    Pro or Team, or buy a Project Pass for this estate. Nothing was written to NetBox.'"""
    plan = plan_name(e.plan)
    plan = plan if plan == "Project Pass" else f"the {plan} plan"
    if isinstance(e, RailyardPlanLimitError) and e.current is not None and e.limit is not None:
        resource = e.resource or "racks"
        head = (
            f"This estate has {e.current} {resource}; {plan} exports deliverables for estates of up to "
            f"{e.limit} {resource}."
        )
        extra = [f"remove {resource}"]
    elif isinstance(e, RailyardPlanRequiredError):
        head = f"Exporting to NetBox needs a plan with deliverables: {plan} does not include them."
        extra = []
    else:
        head = f"Railyard refused the NetBox sync document: {e}."
        extra = []
    options = upgrade_options(e.required_plans, e.project_pass) or ["contact Railyard about an Enterprise plan"]
    return f"{head} {sentence(options + extra)} Nothing was written to NetBox."


def plan_limit_message(e: RailyardPlanLimitError, *, refreshing: bool = False) -> str:
    """'This import has 140 racks; the Community plan allows 25 per estate. Upgrade to Team or Partner,
    or import fewer sites. Nothing was saved.' — built from the refusal's fields."""
    resource = e.resource or "racks"
    per = " per estate" if (e.scope or "estate") == "estate" else ""
    plan = plan_name(e.plan)
    plan = plan if plan == "Project Pass" else f"the {plan} plan"
    if e.current is None or e.limit is None:
        head = f"Railyard refused the import: {e}."
    elif refreshing:
        head = f"After this import the estate would have {e.current} {resource}; {plan} allows {e.limit}{per}."
    else:
        head = f"This import has {e.current} {resource}; {plan} allows {e.limit}{per}."
    options = upgrade_options(e.required_plans, e.project_pass) or ["contact Railyard about an Enterprise plan"]
    if resource == "racks":
        options.append("import fewer sites")
    return f"{head} {sentence(options)} Nothing was saved."
