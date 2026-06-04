from packtrack.models import POStatus, Role, User
from packtrack.services.workflow import allowed_move, primary_action


def _user(role: Role) -> User:
    return User(
        id=role.value.__hash__() % 10000,
        email=f"{role.value}@example.com",
        name=role.value.title(),
        role=role,
        password_hash="x",
    )


def test_required_workflow_transitions_are_allowed_by_expected_roles():
    cases = [
        (Role.OWNER, POStatus.DRAFT, POStatus.DESIGN_REVIEW),
        (Role.DESIGN, POStatus.DESIGN_REVIEW, POStatus.DESIGN_APPROVED),
        (Role.DESIGN, POStatus.DESIGN_REVIEW, POStatus.DESIGN_REJECTED),
        (Role.OWNER, POStatus.DESIGN_REVIEW, POStatus.DRAFT),
        (Role.OWNER, POStatus.DESIGN_REJECTED, POStatus.DESIGN_REVIEW),
        (Role.AGENT, POStatus.DESIGN_APPROVED, POStatus.PI_RECEIVED),
        (Role.OWNER, POStatus.PI_RECEIVED, POStatus.PI_APPROVED),
        (Role.OWNER, POStatus.PI_RECEIVED, POStatus.DESIGN_APPROVED),
        (Role.AGENT, POStatus.PI_APPROVED, POStatus.PRODUCTION),
        (Role.AGENT, POStatus.PI_APPROVED, POStatus.SHIPPED),
        (Role.AGENT, POStatus.PRODUCTION, POStatus.SHIPPED),
        (Role.OWNER, POStatus.PRODUCTION, POStatus.PI_APPROVED),
    ]

    for role, current, target in cases:
        ok, err = allowed_move(_user(role), current, target)
        assert ok, f"{role.value}: {current.value} -> {target.value}: {err}"


def test_received_is_only_reachable_through_receiving_flow():
    for role in Role:
        ok, err = allowed_move(_user(role), POStatus.SHIPPED, POStatus.RECEIVED)
        assert not ok
        assert "Receiving page" in err


def test_cancel_is_owner_only_from_open_statuses():
    for status in POStatus:
        if status in (POStatus.RECEIVED, POStatus.CANCELLED):
            continue
        assert allowed_move(_user(Role.OWNER), status, POStatus.CANCELLED)[0]
        assert not allowed_move(_user(Role.AGENT), status, POStatus.CANCELLED)[0]


def test_closed_statuses_are_immutable():
    for status in (POStatus.RECEIVED, POStatus.CANCELLED):
        ok, err = allowed_move(_user(Role.OWNER), status, POStatus.DRAFT)
        assert not ok
        assert "closed" in err


def test_role_primary_actions_match_operator_inboxes():
    assert primary_action(_user(Role.OWNER), POStatus.DRAFT) == POStatus.DESIGN_REVIEW
    assert primary_action(_user(Role.DESIGN), POStatus.DESIGN_REVIEW) == POStatus.DESIGN_APPROVED
    assert primary_action(_user(Role.AGENT), POStatus.DESIGN_APPROVED) == POStatus.PI_RECEIVED
    assert primary_action(_user(Role.RECEIVING), POStatus.SHIPPED) is None
