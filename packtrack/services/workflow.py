"""PO state machine — the only place transitions are defined.

Every entry point (web POST, Telegram button) calls allowed_move(...) so
button-presses on Telegram cannot grant more access than a web action would.

Closed states (received, cancelled) are immutable. 'received' is reachable
only through the receiving flow, never from a board move. Cancellation is
owner-only from any open status.
"""
from __future__ import annotations

from packtrack.models import POStatus, Role, User

# (from, to) -> roles allowed to move
_TRANSITIONS: dict[tuple[POStatus, POStatus], frozenset[Role]] = {
    (POStatus.DRAFT, POStatus.DESIGN_REVIEW): frozenset({Role.OWNER}),
    (POStatus.DRAFT, POStatus.CANCELLED): frozenset({Role.OWNER}),

    (POStatus.DESIGN_REVIEW, POStatus.DESIGN_APPROVED): frozenset({Role.DESIGN, Role.OWNER}),
    (POStatus.DESIGN_REVIEW, POStatus.DESIGN_REJECTED): frozenset({Role.DESIGN, Role.OWNER}),
    (POStatus.DESIGN_REVIEW, POStatus.DRAFT): frozenset({Role.OWNER}),

    (POStatus.DESIGN_REJECTED, POStatus.DESIGN_REVIEW): frozenset({Role.OWNER}),

    (POStatus.DESIGN_APPROVED, POStatus.DESIGN_REVIEW): frozenset({Role.OWNER, Role.DESIGN}),
    (POStatus.DESIGN_APPROVED, POStatus.PI_RECEIVED): frozenset({Role.AGENT, Role.OWNER}),

    (POStatus.PI_RECEIVED, POStatus.PI_APPROVED): frozenset({Role.OWNER}),
    (POStatus.PI_RECEIVED, POStatus.DESIGN_APPROVED): frozenset({Role.OWNER}),  # reject PI

    (POStatus.PI_APPROVED, POStatus.PRODUCTION): frozenset({Role.OWNER, Role.AGENT}),
    (POStatus.PI_APPROVED, POStatus.SHIPPED): frozenset({Role.OWNER, Role.AGENT}),

    (POStatus.PRODUCTION, POStatus.SHIPPED): frozenset({Role.OWNER, Role.AGENT}),
    (POStatus.PRODUCTION, POStatus.PI_APPROVED): frozenset({Role.OWNER}),

    (POStatus.SHIPPED, POStatus.PRODUCTION): frozenset({Role.OWNER}),
}

# 'cancelled' is owner-only from any open status, handled separately so we don't
# duplicate it across every row in _TRANSITIONS.
_CLOSED = (POStatus.RECEIVED, POStatus.CANCELLED)


def allowed_move(user: User, current: POStatus, target: POStatus) -> tuple[bool, str]:
    if current in _CLOSED:
        return False, "PO is closed and cannot be moved."
    if current == target:
        return True, ""
    if target == POStatus.RECEIVED:
        return False, "Use the Receiving page to mark a PO as received."
    if target == POStatus.CANCELLED:
        if user.role == Role.OWNER:
            return True, ""
        return False, "Only the owner can cancel a PO."
    roles = _TRANSITIONS.get((current, target))
    if not roles:
        return False, f"Cannot move {current.value.replace('_', ' ')} → {target.value.replace('_', ' ')}."
    if user.role in roles:
        return True, ""
    pretty = " or ".join(r.value for r in roles)
    return False, f"This move requires {pretty}."


def suggested_moves(user: User, current: POStatus) -> list[POStatus]:
    """The set of moves shown as primary/secondary action buttons on PO detail."""
    out: list[POStatus] = []
    for status in POStatus:
        if status == current:
            continue
        ok, _ = allowed_move(user, current, status)
        if ok:
            out.append(status)
    return out


def primary_action(user: User, current: POStatus) -> POStatus | None:
    """The 'next obvious step' for this user — drives the big button on the PO page."""
    if current in _CLOSED:
        return None
    flow = {
        Role.OWNER: {
            POStatus.DRAFT: POStatus.DESIGN_REVIEW,
            POStatus.PI_RECEIVED: POStatus.PI_APPROVED,
        },
        Role.DESIGN: {
            POStatus.DESIGN_REVIEW: POStatus.DESIGN_APPROVED,
        },
        Role.AGENT: {
            POStatus.DESIGN_APPROVED: POStatus.PI_RECEIVED,  # i.e. upload PI
            POStatus.PRODUCTION: POStatus.SHIPPED,
        },
    }
    return flow.get(user.role, {}).get(current)


# Verb labels for action buttons. Keyed by (current_status, target_status) so
# the same target status can read differently depending on where you came from
# (e.g. PI_RECEIVED → DESIGN_APPROVED is "Reject PI", not "Move to design approved").
_ACTION_LABELS: dict[tuple[POStatus, POStatus], str] = {
    (POStatus.DRAFT, POStatus.DESIGN_REVIEW): "Send to design review",
    (POStatus.DRAFT, POStatus.CANCELLED): "Cancel PO",

    (POStatus.DESIGN_REVIEW, POStatus.DESIGN_APPROVED): "Approve art",
    (POStatus.DESIGN_REVIEW, POStatus.DESIGN_REJECTED): "Reject art",
    (POStatus.DESIGN_REVIEW, POStatus.DRAFT): "Back to draft",

    (POStatus.DESIGN_REJECTED, POStatus.DESIGN_REVIEW): "Send back to design",

    (POStatus.DESIGN_APPROVED, POStatus.DESIGN_REVIEW): "Return to design",
    (POStatus.DESIGN_APPROVED, POStatus.PI_RECEIVED): "Mark PI uploaded",

    (POStatus.PI_RECEIVED, POStatus.PI_APPROVED): "Approve PI",
    (POStatus.PI_RECEIVED, POStatus.DESIGN_APPROVED): "Reject PI",

    (POStatus.PI_APPROVED, POStatus.PRODUCTION): "Mark in production",
    (POStatus.PI_APPROVED, POStatus.SHIPPED): "Mark shipped",
    (POStatus.PI_APPROVED, POStatus.PI_RECEIVED): "Reopen PI review",

    (POStatus.PRODUCTION, POStatus.SHIPPED): "Mark shipped",
    (POStatus.PRODUCTION, POStatus.PI_APPROVED): "Back to PI approved",

    (POStatus.SHIPPED, POStatus.PRODUCTION): "Back to production",
}


def action_label(current: POStatus, target: POStatus) -> str:
    """Human verb for a transition. Falls back to a generic phrasing."""
    if target == POStatus.CANCELLED:
        return "Cancel PO"
    return _ACTION_LABELS.get(
        (current, target),
        f"Move to {target.value.replace('_', ' ')}",
    )


def is_destructive(target: POStatus) -> bool:
    """For UI styling: red secondary buttons."""
    return target == POStatus.CANCELLED
