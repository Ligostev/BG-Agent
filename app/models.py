"""Пользователи, проверки и ответы источников."""

from datetime import datetime, timezone

from sqlalchemy import JSON, BigInteger, DateTime, Float, ForeignKey, Integer, String, Text
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


class Base(DeclarativeBase):
    pass


class User(Base):
    __tablename__ = "users"

    id: Mapped[int] = mapped_column(primary_key=True)
    email: Mapped[str | None] = mapped_column(String(255), unique=True)
    password_hash: Mapped[str | None] = mapped_column(String(255))
    # Пользователь бота заводится по telegram_id; привязку к кабинету сделаем кодом из кабинета.
    telegram_id: Mapped[int | None] = mapped_column(BigInteger, unique=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)

    checks: Mapped[list["Check"]] = relationship(back_populates="user")


class CheckStatus:
    QUEUED = "queued"
    RUNNING = "running"
    CAPTCHA = "captcha"     # ждём, пока пользователь введёт капчу
    DONE = "done"
    ERROR = "error"

    FINISHED = (DONE, ERROR)


class Check(Base):
    """Одна проверка контрагента по ИНН/ОГРН по всем подключённым источникам."""

    __tablename__ = "checks"

    id: Mapped[int] = mapped_column(primary_key=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id"), index=True)
    query: Mapped[str] = mapped_column(String(15))
    status: Mapped[str] = mapped_column(String(16), default=CheckStatus.QUEUED)
    title: Mapped[str | None] = mapped_column(String(500))
    inn: Mapped[str | None] = mapped_column(String(12), index=True)
    ogrn: Mapped[str | None] = mapped_column(String(15))
    error: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    user: Mapped[User] = relationship(back_populates="checks")
    results: Mapped[list["SourceResult"]] = relationship(
        back_populates="check", order_by="SourceResult.id", cascade="all, delete-orphan")


class SourceResult(Base):
    """Ответ одного источника (ФНС, РНП, ФССП...) в рамках проверки."""

    __tablename__ = "source_results"

    id: Mapped[int] = mapped_column(primary_key=True)
    check_id: Mapped[int] = mapped_column(ForeignKey("checks.id"), index=True)
    source: Mapped[str] = mapped_column(String(32))
    status: Mapped[str] = mapped_column(String(16))     # found | not_found | error
    summary: Mapped[list] = mapped_column(JSON, default=list)   # [[подпись, значение], ...]
    data: Mapped[dict | None] = mapped_column(JSON)     # сырой ответ источника
    error: Mapped[str | None] = mapped_column(Text)
    fetched_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)

    check: Mapped[Check] = relationship(back_populates="results")


class TenderProfile(Base):
    """Что клиент поставляет и где: по этому профилю подбираются закупки."""

    __tablename__ = "tender_profiles"

    id: Mapped[int] = mapped_column(primary_key=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id"), unique=True)
    okpd2: Mapped[list] = mapped_column(JSON, default=list)
    regions: Mapped[list] = mapped_column(JSON, default=list)
    keywords: Mapped[list] = mapped_column(JSON, default=list)
    price_min: Mapped[float | None] = mapped_column(Float)
    price_max: Mapped[float | None] = mapped_column(Float)
    min_days_left: Mapped[int] = mapped_column(Integer, default=2)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, onupdate=utcnow)


class TenderNotice(Base):
    """Извещение о закупке из ЕИС (полный разбор в data, см. tenders.Notice)."""

    __tablename__ = "tender_notices"

    id: Mapped[int] = mapped_column(primary_key=True)
    purchase_number: Mapped[str] = mapped_column(String(32), unique=True)
    region: Mapped[str] = mapped_column(String(4), index=True)
    ends_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), index=True)
    max_price: Mapped[float | None] = mapped_column(Float)
    data: Mapped[dict] = mapped_column(JSON)
    fetched_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class GuaranteeRequest(Base):
    """Клиент нажал «Нужна гарантия» по закупке: заявка брокеру."""

    __tablename__ = "guarantee_requests"

    id: Mapped[int] = mapped_column(primary_key=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id"), index=True)
    purchase_number: Mapped[str] = mapped_column(String(32))
    subject: Mapped[str | None] = mapped_column(Text)
    product: Mapped[str] = mapped_column(String(200))
    amount: Mapped[float | None] = mapped_column(Float)
    status: Mapped[str] = mapped_column(String(16), default="new")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
