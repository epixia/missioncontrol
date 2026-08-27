from __future__ import annotations

from pathlib import Path

from sqlmodel import Session, SQLModel, create_engine

_DB_PATH = Path.home() / ".mission-control" / "mission-control.db"

_DB_PATH.parent.mkdir(parents=True, exist_ok=True)
engine = create_engine(f"sqlite:///{_DB_PATH}", connect_args={"check_same_thread": False})


def init_db() -> None:
    SQLModel.metadata.create_all(engine)


def get_session() -> Session:
    return Session(engine)
