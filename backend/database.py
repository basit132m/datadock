import os
import secrets
import string
from sqlalchemy import create_engine, text
from sqlalchemy.orm import sessionmaker

DATABASE_URL = os.getenv("DATABASE_URL", "sqlite:///./datadock.db")

engine = create_engine(
    DATABASE_URL,
    connect_args={"check_same_thread": False} if "sqlite" in DATABASE_URL else {},
)
SessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=engine)


def get_db():
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()


def _gen_share_id():
    return "".join(secrets.choice(string.ascii_letters + string.digits) for _ in range(8))


def init_db():
    from models import Base
    Base.metadata.create_all(bind=engine)
    _migrate()


def _migrate():
    with engine.connect() as conn:
        for sql in [
            "ALTER TABLE uploads ADD COLUMN share_id VARCHAR(12)",
            "ALTER TABLE uploads ADD COLUMN views INTEGER DEFAULT 0",
            "ALTER TABLE uploads ADD COLUMN downloads INTEGER DEFAULT 0",
            "ALTER TABLE uploads ADD COLUMN storage_provider_id VARCHAR(36)",
        ]:
            try:
                conn.execute(text(sql))
                conn.commit()
            except Exception:
                pass

        # Backfill share_ids for existing completed uploads
        rows = conn.execute(
            text("SELECT id FROM uploads WHERE share_id IS NULL AND status = 'completed'")
        ).fetchall()
        for row in rows:
            conn.execute(
                text("UPDATE uploads SET share_id = :sid WHERE id = :id"),
                {"sid": _gen_share_id(), "id": row[0]},
            )
        if rows:
            conn.commit()
