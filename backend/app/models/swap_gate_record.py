"""One SwapMod plate-check decision (kept per printer, newest 500)."""

from __future__ import annotations

from datetime import datetime

from sqlalchemy import DateTime, Float, ForeignKey, Integer, String, Text, func
from sqlalchemy.orm import Mapped, mapped_column

from backend.app.core.database import Base


class SwapGateRecord(Base):
    __tablename__ = "swap_gate_records"

    id: Mapped[int] = mapped_column(primary_key=True)
    printer_id: Mapped[int] = mapped_column(Integer, ForeignKey("printers.id", ondelete="CASCADE"), index=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now())
    mode: Mapped[str] = mapped_column(String(16))  # v1 / shadow / enforce
    clip_percent: Mapped[float | None] = mapped_column(Float, nullable=True)
    ai_object: Mapped[bool | None] = mapped_column(nullable=True)
    ai_reason: Mapped[str | None] = mapped_column(Text, nullable=True)
    ai_ms: Mapped[int | None] = mapped_column(Integer, nullable=True)
    outcome: Mapped[str] = mapped_column(String(16))  # pass / fail / pending / error
    reason_key: Mapped[str | None] = mapped_column(String(64), nullable=True)
    # File name of the cropped image inside settings.swap_gate_dir (never the full frame).
    image_path: Mapped[str | None] = mapped_column(String(255), nullable=True)
