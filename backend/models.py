from sqlalchemy import Column, String, Integer, BigInteger, DateTime, Text, Float
from sqlalchemy.orm import declarative_base

Base = declarative_base()


class Upload(Base):
    __tablename__ = "uploads"

    id = Column(String(36), primary_key=True)
    share_id = Column(String(12), unique=True, index=True, nullable=True)
    file_hash = Column(String(64), index=True, nullable=False)
    filename = Column(String(500), nullable=False)
    file_size = Column(BigInteger, nullable=False)
    content_type = Column(String(200), default="application/octet-stream")
    b2_upload_id = Column(String(500), nullable=False)
    b2_file_key = Column(String(1000), nullable=False)
    status = Column(String(20), default="pending", nullable=False)
    views = Column(Integer, default=0)
    downloads = Column(Integer, default=0)
    storage_provider_id = Column(String(36), nullable=True, index=True)
    uploaded_by_key_id  = Column(String(36), nullable=True)
    redirects_to = Column(String(12), nullable=True, index=True)
    redirect_url = Column(String(500), nullable=True)
    dup_excluded = Column(Integer, default=0, nullable=True)
    import_bytes_done = Column(BigInteger, default=0, nullable=True)
    created_at = Column(DateTime, nullable=False)
    completed_at = Column(DateTime, nullable=True)


class Part(Base):
    __tablename__ = "parts"

    id = Column(String(36), primary_key=True)
    upload_id = Column(String(36), index=True, nullable=False)
    part_number = Column(Integer, nullable=False)
    etag = Column(String(200), nullable=False)
    uploaded_at = Column(DateTime, nullable=False)


class StorageProvider(Base):
    __tablename__ = "storage_providers"

    id = Column(String(36), primary_key=True)
    name = Column(String(200), nullable=False)
    endpoint_url = Column(String(500), nullable=False)
    key_id = Column(String(500), nullable=False)
    application_key = Column(String(500), nullable=False)
    bucket_name = Column(String(200), nullable=False)
    public_base_url = Column(String(500), nullable=True)
    is_default = Column(Integer, default=0, nullable=False)
    active = Column(Integer, default=1, nullable=False)
    created_at = Column(DateTime, nullable=False)
    bandwidth_cap_gb = Column(Float, nullable=True)
    fallback_base_url = Column(String(500), nullable=True)
    fallback_provider_id = Column(String(36), nullable=True)
    monthly_bandwidth_used = Column(BigInteger, default=0, nullable=False)
    bandwidth_reset_month = Column(String(7), nullable=True)


class SiteSetting(Base):
    __tablename__ = "site_settings"

    key = Column(String(100), primary_key=True)
    value = Column(Text, nullable=True)
    updated_at = Column(DateTime, nullable=True)


class Ad(Base):
    __tablename__ = "ads"

    id = Column(String(36), primary_key=True)
    type = Column(String(20), nullable=False)          # "banner" or "button"
    label = Column(String(300), nullable=False)        # button text or alt text
    image_url = Column(Text, nullable=True)            # banners only
    link_url = Column(Text, nullable=False)
    active = Column(Integer, default=1, nullable=False)
    display_order = Column(Integer, default=0, nullable=False)
    created_at = Column(DateTime, nullable=False)


class ApiKey(Base):
    __tablename__ = "api_keys"

    id = Column(String(36), primary_key=True)
    name = Column(String(200), nullable=False)
    key = Column(String(100), unique=True, nullable=False, index=True)
    role = Column(String(20), default="member", nullable=False)
    is_master = Column(Integer, default=0, nullable=False)  # 1 = the admin's own key
    active = Column(Integer, default=1, nullable=False)
    created_at = Column(DateTime, nullable=False)


class DownloadEvent(Base):
    __tablename__ = "download_events"

    id = Column(String(36), primary_key=True)
    upload_id = Column(String(36), index=True, nullable=False)
    filename = Column(String(500), nullable=False)
    ip = Column(String(45), nullable=True)
    country = Column(String(100), nullable=True)
    country_code = Column(String(2), nullable=True)
    device_type = Column(String(20), nullable=True)   # Desktop / Mobile / Tablet
    os_name = Column(String(50), nullable=True)       # Windows / macOS / iOS / Android / Linux
    user_agent = Column(Text, nullable=True)
    created_at = Column(DateTime, nullable=False, index=True)


class AccessRequest(Base):
    __tablename__ = "access_requests"

    id         = Column(String(36), primary_key=True)
    name       = Column(String(200), nullable=False)
    email      = Column(String(300), nullable=False, index=True)
    reason     = Column(Text, nullable=True)
    status     = Column(String(20), default="pending", nullable=False, index=True)
    key_id     = Column(String(36), nullable=True)   # set after approval
    created_at = Column(DateTime, nullable=False)
    updated_at = Column(DateTime, nullable=True)


class SupportMessage(Base):
    __tablename__ = "support_messages"

    id             = Column(String(36), primary_key=True)
    key_id         = Column(String(36), nullable=False, index=True)
    member_name    = Column(String(200), nullable=False)
    subject        = Column(String(500), nullable=False)
    body           = Column(Text, nullable=False)
    reply          = Column(Text, nullable=True)   # legacy single-reply field (kept for migration)
    status         = Column(String(20), default="open", nullable=False, index=True)
    attachment_url = Column(String(500), nullable=True)
    created_at     = Column(DateTime, nullable=False)
    replied_at     = Column(DateTime, nullable=True)


class SupportReply(Base):
    __tablename__ = "support_replies"

    id             = Column(String(36), primary_key=True)
    message_id     = Column(String(36), nullable=False, index=True)
    sender         = Column(String(10), nullable=False)   # 'member' or 'admin'
    body           = Column(Text, nullable=False)
    attachment_url = Column(String(500), nullable=True)
    created_at     = Column(DateTime, nullable=False)


class FileReport(Base):
    __tablename__ = "file_reports"

    id         = Column(String(36), primary_key=True)
    share_id   = Column(String(12), nullable=False, index=True)
    filename   = Column(String(500), nullable=False)
    reason     = Column(String(50), nullable=False)   # not_downloading | link_expired | corrupt_or_wrong | other
    message    = Column(Text, nullable=True)
    ip         = Column(String(45), nullable=True)
    status     = Column(String(20), default="open", nullable=False, index=True)
    created_at = Column(DateTime, nullable=False)


class DownloadToken(Base):
    __tablename__ = "download_tokens"

    token      = Column(String(64), primary_key=True)
    share_id   = Column(String(12), nullable=False)
    expires    = Column(DateTime, nullable=False, index=True)


class Referrer(Base):
    __tablename__ = "referrers"

    id          = Column(String(36), primary_key=True)
    share_id    = Column(String(12), nullable=False, index=True)
    domain      = Column(String(300), nullable=False, index=True)
    visit_count = Column(Integer, default=1, nullable=False)
    last_seen   = Column(DateTime, nullable=False)
    day         = Column(String(10), nullable=True, index=True)  # 'YYYY-MM-DD' bucket; NULL = legacy lifetime row
