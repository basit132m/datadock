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
