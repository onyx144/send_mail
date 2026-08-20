from __future__ import annotations

from datetime import datetime, timezone
from sqlalchemy import Boolean, DateTime, ForeignKey, Integer, String, Text, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.db.session import Base


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


class MailAccount(Base):
    __tablename__ = 'mail_accounts'

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    email: Mapped[str] = mapped_column(String(255), unique=True, index=True)
    display_name: Mapped[str | None] = mapped_column(String(255), nullable=True)
    enabled: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
    last_sent_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    last_inbox_uid: Mapped[str | None] = mapped_column(String(255), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, nullable=False)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, onupdate=utcnow, nullable=False)

    send_logs: Mapped[list['SendLog']] = relationship(back_populates='account')


class Prospect(Base):
    __tablename__ = 'prospects'

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    youtube_link: Mapped[str] = mapped_column(String(1000), unique=True, index=True)
    email: Mapped[str | None] = mapped_column(String(255), index=True, nullable=True)
    nick: Mapped[str | None] = mapped_column(String(255), index=True, nullable=True)
    subscribers: Mapped[int | None] = mapped_column(Integer, nullable=True)
    # new = ready/unsent, sent = first email sent, lose = rejected by parser rule
    status: Mapped[str] = mapped_column(String(32), default='new', index=True, nullable=False)
    plans: Mapped[str] = mapped_column(String(32), default='later', index=True, nullable=False)  # now/later
    telegram: Mapped[str | None] = mapped_column(String(255), nullable=True)
    viber: Mapped[str | None] = mapped_column(String(255), nullable=True)
    whatsapp: Mapped[str | None] = mapped_column(String(255), nullable=True)
    facebook: Mapped[str | None] = mapped_column(String(1000), nullable=True)
    instagram: Mapped[str | None] = mapped_column(String(1000), nullable=True)
    discord: Mapped[str | None] = mapped_column(String(255), nullable=True)
    website: Mapped[str | None] = mapped_column(String(1000), nullable=True)
    manifest_url: Mapped[str | None] = mapped_column(String(1000), index=True, nullable=True)
    video_count: Mapped[int | None] = mapped_column(Integer, nullable=True)
    last_video_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    lose_reason: Mapped[str | None] = mapped_column(String(500), nullable=True)
    contact_status: Mapped[str | None] = mapped_column(String(64), index=True, nullable=True)
    raw_contacts: Mapped[str | None] = mapped_column(Text, nullable=True)
    about_text: Mapped[str | None] = mapped_column(Text, nullable=True)
    source_url: Mapped[str | None] = mapped_column(String(1000), nullable=True)
    raw_text: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, nullable=False)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, onupdate=utcnow, nullable=False)

    send_logs: Mapped[list['SendLog']] = relationship(back_populates='prospect')


class SendLog(Base):
    __tablename__ = 'send_logs'
    __table_args__ = (UniqueConstraint('account_id', 'prospect_id', name='uq_send_account_prospect'),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    account_id: Mapped[int] = mapped_column(ForeignKey('mail_accounts.id'), index=True)
    prospect_id: Mapped[int] = mapped_column(ForeignKey('prospects.id'), index=True)
    recipient_email: Mapped[str] = mapped_column(String(255), index=True)
    subject: Mapped[str] = mapped_column(String(500))
    status: Mapped[str] = mapped_column(String(32), default='sent', index=True)
    error: Mapped[str | None] = mapped_column(Text, nullable=True)
    sent_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, nullable=False)

    account: Mapped[MailAccount] = relationship(back_populates='send_logs')
    prospect: Mapped[Prospect] = relationship(back_populates='send_logs')


class InboundMessage(Base):
    __tablename__ = 'inbound_messages'
    __table_args__ = (UniqueConstraint('account_id', 'message_uid', name='uq_inbound_account_uid'),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    account_id: Mapped[int] = mapped_column(ForeignKey('mail_accounts.id'), index=True)
    message_uid: Mapped[str] = mapped_column(String(255), index=True)
    from_email: Mapped[str | None] = mapped_column(String(255), index=True, nullable=True)
    from_name: Mapped[str | None] = mapped_column(String(255), nullable=True)
    subject: Mapped[str | None] = mapped_column(String(500), nullable=True)
    body_text: Mapped[str | None] = mapped_column(Text, nullable=True)
    message_id_header: Mapped[str | None] = mapped_column(String(1000), nullable=True)
    in_reply_to: Mapped[str | None] = mapped_column(String(1000), nullable=True)
    telegram_notified: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    received_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, nullable=False)


class TelegramPendingReply(Base):
    __tablename__ = 'telegram_pending_replies'

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    telegram_chat_id: Mapped[str] = mapped_column(String(255), index=True)
    telegram_user_id: Mapped[str | None] = mapped_column(String(255), index=True, nullable=True)
    inbound_message_id: Mapped[int] = mapped_column(ForeignKey('inbound_messages.id'), index=True)
    active: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, nullable=False)
