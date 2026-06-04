from packtrack.services.box_receipt import (
    _BoxRow,
    compute_accepted,
    find_box_collision,
)


def test_receiving_counted_quantity_overrides_declared_quantity():
    assert compute_accepted(declared=12, counted=10) == 10
    assert compute_accepted(declared=12, counted=0) == 0
    assert compute_accepted(declared=12, counted=None) == 12


def test_duplicate_box_prevention_strips_whitespace():
    existing = [_BoxRow(box_number="BOX-001")]
    assert find_box_collision(existing, " BOX-001 ") is existing[0]
    assert find_box_collision(existing, "BOX-002") is None
