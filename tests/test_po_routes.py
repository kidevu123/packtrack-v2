from sqlalchemy.pool import StaticPool
from sqlmodel import Session, SQLModel, create_engine

from packtrack.auth import encode_session
from packtrack.config import settings
from packtrack.db import get_session
from packtrack.main import app
from packtrack.models import Item, POStatus, PurchaseOrder, Role, User


def _client():
    from fastapi.testclient import TestClient

    engine = create_engine(
        "sqlite://",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    SQLModel.metadata.create_all(engine)
    session = Session(engine)
    app.dependency_overrides[get_session] = lambda: session
    client = TestClient(app, raise_server_exceptions=False)
    return client, session


def _user(session: Session, role: Role) -> User:
    user = User(
        email=f"{role.value}@example.com",
        name=role.value.title(),
        role=role,
        password_hash="x",
    )
    session.add(user)
    session.commit()
    session.refresh(user)
    return user


def _login(client, user: User):
    client.cookies.set(settings.SESSION_COOKIE_NAME, encode_session(user.id))


def test_owner_can_create_draft_or_send_to_design_review():
    client, session = _client()
    owner = _user(session, Role.OWNER)
    item = Item(name="Mailer", zoho_item_id="z1", last_unit_cost=1.25)
    session.add(item)
    session.commit()
    session.refresh(item)
    _login(client, owner)

    draft_resp = client.post(
        "/po/new",
        data={
            "submit_action": "draft",
            "urgency": "normal",
            "item_id[]": str(item.id),
            "quantity[]": "10",
            "unit_price[]": "1.25",
            "arrival[]": "",
            "line_notes[]": "",
        },
        follow_redirects=False,
    )
    assert draft_resp.status_code == 303
    draft = session.get(PurchaseOrder, 1)
    assert draft.status == POStatus.DRAFT

    review_resp = client.post(
        "/po/new",
        data={
            "submit_action": "design_review",
            "urgency": "normal",
            "item_id[]": str(item.id),
            "quantity[]": "5",
            "unit_price[]": "1.25",
            "arrival[]": "",
            "line_notes[]": "",
        },
        follow_redirects=False,
    )
    assert review_resp.status_code == 303
    review = session.get(PurchaseOrder, 2)
    assert review.status == POStatus.DESIGN_REVIEW


def test_non_owner_cannot_create_purchase_order():
    client, session = _client()
    designer = _user(session, Role.DESIGN)
    _login(client, designer)

    resp = client.post("/po/new", data={}, follow_redirects=False)
    assert resp.status_code == 403
