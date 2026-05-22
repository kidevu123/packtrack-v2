"""Create the first owner user. Run once after initial deploy:

    python scripts/seed_owner.py
"""
from getpass import getpass

from sqlmodel import Session, select

from packtrack.auth import hash_password
from packtrack.db import engine
from packtrack.models import Role, User


def main() -> None:
    name = input("Owner name: ").strip()
    email = input("Owner email: ").strip().lower()
    pwd = getpass("Password: ")
    if not name or not email or not pwd:
        raise SystemExit("All fields required.")
    with Session(engine) as session:
        existing = session.exec(select(User).where(User.email == email)).first()
        if existing is not None:
            raise SystemExit(f"User {email} already exists.")
        session.add(User(
            name=name, email=email, role=Role.OWNER,
            password_hash=hash_password(pwd),
        ))
        session.commit()
    print(f"Owner {email} created.")


if __name__ == "__main__":
    main()
