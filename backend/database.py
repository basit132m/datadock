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

if "sqlite" in DATABASE_URL:
    from sqlalchemy import event

    @event.listens_for(engine, "connect")
    def _sqlite_pragmas(dbapi_conn, _):
        # WAL lets the 2 uvicorn workers read/write concurrently without
        # "database is locked" errors; busy_timeout retries instead of failing.
        cur = dbapi_conn.cursor()
        cur.execute("PRAGMA journal_mode=WAL")
        cur.execute("PRAGMA busy_timeout=15000")
        cur.execute("PRAGMA synchronous=NORMAL")
        cur.close()

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
    try:
        Base.metadata.create_all(bind=engine)
    except Exception as e:
        if "already exists" not in str(e).lower():
            raise
    _migrate()


def _migrate():
    with engine.connect() as conn:
        for sql in [
            "ALTER TABLE uploads ADD COLUMN share_id VARCHAR(12)",
            "ALTER TABLE uploads ADD COLUMN views INTEGER DEFAULT 0",
            "ALTER TABLE uploads ADD COLUMN downloads INTEGER DEFAULT 0",
            "ALTER TABLE uploads ADD COLUMN storage_provider_id VARCHAR(36)",
            "ALTER TABLE storage_providers ADD COLUMN bandwidth_cap_gb REAL",
            "ALTER TABLE storage_providers ADD COLUMN fallback_base_url VARCHAR(500)",
            "ALTER TABLE storage_providers ADD COLUMN fallback_provider_id VARCHAR(36)",
            "ALTER TABLE storage_providers ADD COLUMN monthly_bandwidth_used INTEGER DEFAULT 0",
            "ALTER TABLE storage_providers ADD COLUMN bandwidth_reset_month VARCHAR(7)",
            "ALTER TABLE api_keys ADD COLUMN is_master INTEGER DEFAULT 0",
            "ALTER TABLE uploads ADD COLUMN uploaded_by_key_id VARCHAR(36)",
            "CREATE INDEX IF NOT EXISTS ix_uploads_uploaded_by_key_id ON uploads (uploaded_by_key_id)",
            "CREATE INDEX IF NOT EXISTS ix_uploads_status_completed_at ON uploads (status, completed_at)",
            "ALTER TABLE support_messages ADD COLUMN attachment_url VARCHAR(500)",
            "ALTER TABLE support_replies ADD COLUMN attachment_url VARCHAR(500)",
            "ALTER TABLE uploads ADD COLUMN redirects_to VARCHAR(12)",
            "CREATE INDEX IF NOT EXISTS ix_uploads_redirects_to ON uploads (redirects_to)",
            "ALTER TABLE uploads ADD COLUMN dup_excluded INTEGER DEFAULT 0",
            "ALTER TABLE uploads ADD COLUMN redirect_url VARCHAR(500)",
            "ALTER TABLE uploads ADD COLUMN import_bytes_done BIGINT DEFAULT 0",
            "ALTER TABLE referrers ADD COLUMN day VARCHAR(10)",
            "CREATE INDEX IF NOT EXISTS ix_referrers_day ON referrers (day)",
            "ALTER TABLE uploads ADD COLUMN hidden INTEGER DEFAULT 0",
            "ALTER TABLE uploads ADD COLUMN hidden_redirect_url VARCHAR(500)",
            "ALTER TABLE ads ADD COLUMN script_code TEXT",
            "CREATE TABLE IF NOT EXISTS uptime_minutes (minute VARCHAR(16) PRIMARY KEY)",
            "CREATE TABLE IF NOT EXISTS backlink_domains (id VARCHAR(36) PRIMARY KEY, domain VARCHAR(300) NOT NULL, name VARCHAR(200), contact VARCHAR(300), screenshot_url VARCHAR(500), status VARCHAR(20) DEFAULT 'pending' NOT NULL, created_at DATETIME NOT NULL, approved_at DATETIME)",
            # Pop-up click tracking — daily aggregate (never one row per click)
            "CREATE TABLE IF NOT EXISTS popup_clicks (id VARCHAR(36) PRIMARY KEY, day VARCHAR(10) NOT NULL, country VARCHAR(100), country_code VARCHAR(2), device_type VARCHAR(20), mode VARCHAR(10), click_count INTEGER DEFAULT 0 NOT NULL, last_seen DATETIME NOT NULL)",
            "CREATE INDEX IF NOT EXISTS ix_popup_clicks_day ON popup_clicks (day)",
            # Add the mode column for DBs created before pop-up rotation tracking,
            # then rebuild the unique bucket index to include it (script vs direct).
            "ALTER TABLE popup_clicks ADD COLUMN mode VARCHAR(10)",
            "DROP INDEX IF EXISTS uq_popup_clicks_bucket",
            "CREATE UNIQUE INDEX IF NOT EXISTS uq_popup_clicks_bucket ON popup_clicks (day, country_code, device_type, mode)",
            # Short-lived unique-visitor de-dup keys (hashed IP per day; auto-pruned)
            "CREATE TABLE IF NOT EXISTS popup_click_seen (id INTEGER PRIMARY KEY AUTOINCREMENT, day VARCHAR(10) NOT NULL, ip_hash VARCHAR(64) NOT NULL)",
            "CREATE UNIQUE INDEX IF NOT EXISTS uq_popup_seen ON popup_click_seen (day, ip_hash)",
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
