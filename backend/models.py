from sqlalchemy import Column, String, Integer, BigInteger, DateTime
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
    created_at = Column(DateTime, nullable=False)
    completed_at = Column(DateTime, nullable=True)


class Part(Base):
    __tablename__ = "parts"

    id = Column(String(36), primary_key=True)
    upload_id = Column(String(36), index=True, nullable=False)
    part_number = Column(Integer, nullable=False)
    etag = Column(String(200), nullable=False)
    uploaded_at = Column(DateTime, nullable=False)
