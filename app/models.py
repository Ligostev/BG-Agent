"""Пользователи, проверки и ответы источников."""

from datetime import date, datetime, timezone

from sqlalchemy import JSON, BigInteger, Boolean, Date, DateTime, Float, ForeignKey, Integer, String, Text
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
    # client — клиент брокера (сам заходит в кабинет); manager и admin — сотрудники, им открыт раздел CRM.
    role: Mapped[str] = mapped_column(String(16), default="client")
    # Одноразовый код для привязки Telegram к учётке: t.me/<бот>?start=link_<код>.
    telegram_code: Mapped[str | None] = mapped_column(String(32), unique=True)
    # Служебные уведомления по своим заявкам (этап сделки, ответ банка). Не реклама.
    notify: Mapped[bool] = mapped_column(Boolean, default=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)

    checks: Mapped[list["Check"]] = relationship(back_populates="user")

    @property
    def is_staff(self) -> bool:
        return self.role in STAFF_ROLES


STAFF_ROLES = ("manager", "admin")


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
    laws: Mapped[list] = mapped_column(JSON, default=lambda: ["44-ФЗ", "223-ФЗ", "615-ПП"])
    # Описание своими словами и его вектор для подбора по смыслу. Вектор считает задача
    # app.tenders_sync; embedding_text — описание, по которому он посчитан.
    description: Mapped[str | None] = mapped_column(Text)
    embedding: Mapped[list | None] = mapped_column(JSON(none_as_null=True))
    embedding_text: Mapped[str | None] = mapped_column(Text)
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
    embedding: Mapped[list | None] = mapped_column(JSON(none_as_null=True))
    fetched_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class Client(Base):
    """Компания или ИП, с которой работает брокер. Может быть привязана к учётке клиента в кабинете."""

    __tablename__ = "clients"

    id: Mapped[int] = mapped_column(primary_key=True)
    title: Mapped[str | None] = mapped_column(String(500))
    inn: Mapped[str | None] = mapped_column(String(12), unique=True)
    ogrn: Mapped[str | None] = mapped_column(String(15))
    user_id: Mapped[int | None] = mapped_column(ForeignKey("users.id"), unique=True)
    source: Mapped[str | None] = mapped_column(String(200))     # откуда пришёл клиент
    # Ответственный сотрудник: ему уходят задачи и напоминания по клиенту.
    manager_id: Mapped[int | None] = mapped_column(ForeignKey("users.id"), index=True)
    notes: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)

    user: Mapped[User | None] = relationship(foreign_keys=[user_id])
    manager: Mapped[User | None] = relationship(foreign_keys=[manager_id])
    contacts: Mapped[list["Contact"]] = relationship(
        back_populates="client", order_by="Contact.id", cascade="all, delete-orphan")
    deals: Mapped[list["Deal"]] = relationship(back_populates="client", order_by="Deal.id.desc()")


class Contact(Base):
    """Контактное лицо клиента."""

    __tablename__ = "contacts"

    id: Mapped[int] = mapped_column(primary_key=True)
    client_id: Mapped[int] = mapped_column(ForeignKey("clients.id"), index=True)
    name: Mapped[str] = mapped_column(String(200))
    position: Mapped[str | None] = mapped_column(String(200))
    phone: Mapped[str | None] = mapped_column(String(50))
    email: Mapped[str | None] = mapped_column(String(255))
    telegram: Mapped[str | None] = mapped_column(String(100))

    client: Mapped[Client] = relationship(back_populates="contacts")


class DealStage:
    """Этапы сделки по гарантии (воронка)."""

    NEW = "new"
    DOCS = "docs"
    BANKS = "banks"
    APPROVED = "approved"
    ISSUED = "issued"
    LOST = "lost"

    TITLES = {NEW: "Новая", DOCS: "Сбор документов", BANKS: "В банках", APPROVED: "Одобрена",
              ISSUED: "Выдана", LOST: "Отказ"}
    OPEN = (NEW, DOCS, BANKS, APPROVED)
    CLOSED = (ISSUED, LOST)


class BankStatus:
    """Статус заявки в отдельном банке."""

    TITLES = {"preparing": "Готовим", "sent": "Отправлена", "review": "На рассмотрении",
              "approved": "Одобрена", "rejected": "Отказ", "issued": "Выдана"}


class Deal(Base):
    """Сделка: одна гарантия для клиента. Создаётся кнопкой «Нужна гарантия» или сотрудником в CRM."""

    __tablename__ = "deals"

    id: Mapped[int] = mapped_column(primary_key=True)
    client_id: Mapped[int] = mapped_column(ForeignKey("clients.id"), index=True)
    # Кто завёл: клиент из кабинета или сотрудник.
    user_id: Mapped[int | None] = mapped_column(ForeignKey("users.id"), index=True)
    purchase_number: Mapped[str | None] = mapped_column(String(32))
    subject: Mapped[str | None] = mapped_column(Text)
    product: Mapped[str] = mapped_column(String(200))
    amount: Mapped[float | None] = mapped_column(Float)          # сумма гарантии
    commission: Mapped[float | None] = mapped_column(Float)      # комиссия брокера, ₽
    # Срок действия выданной гарантии: за 30 дней до него появится задача предложить новую.
    expires_at: Mapped[date | None] = mapped_column(Date)
    stage: Mapped[str] = mapped_column(String(16), default=DealStage.NEW, index=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, onupdate=utcnow)
    closed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    client: Mapped[Client] = relationship(back_populates="deals")
    banks: Mapped[list["DealBank"]] = relationship(
        back_populates="deal", order_by="DealBank.id", cascade="all, delete-orphan")

    @property
    def stage_title(self) -> str:
        return DealStage.TITLES.get(self.stage, self.stage)


class DealBank(Base):
    """Заявка по сделке в конкретный банк и его предложение."""

    __tablename__ = "deal_banks"

    id: Mapped[int] = mapped_column(primary_key=True)
    deal_id: Mapped[int] = mapped_column(ForeignKey("deals.id"), index=True)
    bank: Mapped[str] = mapped_column(String(200))
    status: Mapped[str] = mapped_column(String(16), default="preparing")
    rate: Mapped[float | None] = mapped_column(Float)    # ставка банка, % годовых
    fee: Mapped[float | None] = mapped_column(Float)     # стоимость гарантии для клиента, ₽
    note: Mapped[str | None] = mapped_column(Text)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, onupdate=utcnow)

    deal: Mapped[Deal] = relationship(back_populates="banks")

    @property
    def status_title(self) -> str:
        return BankStatus.TITLES.get(self.status, self.status)


class Activity(Base):
    """Журнал по клиенту: заметки, звонки и автоматические записи (смена этапа, проверка)."""

    __tablename__ = "activities"

    id: Mapped[int] = mapped_column(primary_key=True)
    client_id: Mapped[int] = mapped_column(ForeignKey("clients.id"), index=True)
    deal_id: Mapped[int | None] = mapped_column(ForeignKey("deals.id"), index=True)
    author_id: Mapped[int | None] = mapped_column(ForeignKey("users.id"))
    kind: Mapped[str] = mapped_column(String(16), default="note")   # note | call | system
    text: Mapped[str] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)

    author: Mapped[User | None] = relationship()


class Task(Base):
    """Задача сотрудника: перезвонить, запросить документы, предложить продление гарантии."""

    __tablename__ = "tasks"

    id: Mapped[int] = mapped_column(primary_key=True)
    title: Mapped[str] = mapped_column(String(500))
    client_id: Mapped[int | None] = mapped_column(ForeignKey("clients.id"), index=True)
    deal_id: Mapped[int | None] = mapped_column(ForeignKey("deals.id"), index=True)
    # Кому: пусто — любому сотруднику (видна всем, напоминание уходит всем с Telegram).
    assignee_id: Mapped[int | None] = mapped_column(ForeignKey("users.id"), index=True)
    author_id: Mapped[int | None] = mapped_column(ForeignKey("users.id"))
    due_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), index=True)
    remind: Mapped[bool] = mapped_column(Boolean, default=True)
    reminded_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    done_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), index=True)
    # Автоматическая задача (например, «гарантия истекает»): ключ, чтобы не завести её дважды.
    auto_key: Mapped[str | None] = mapped_column(String(64), unique=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)

    client: Mapped[Client | None] = relationship()
    deal: Mapped[Deal | None] = relationship()
    assignee: Mapped[User | None] = relationship(foreign_keys=[assignee_id])


class Consent(Base):
    """Согласие на обработку ПДн (kind=pd) или на рекламные сообщения (kind=ads) по одному адресу.

    Хранится всё, что нужно доказать согласие (ст. 18 закона «О рекламе»): когда, где, в какой
    формулировке и с какого IP. Отзыв не удаляет запись, а ставит revoked_at.
    """

    __tablename__ = "consents"

    id: Mapped[int] = mapped_column(primary_key=True)
    kind: Mapped[str] = mapped_column(String(8))               # pd | ads
    channel: Mapped[str] = mapped_column(String(16))           # email | telegram | sms
    address: Mapped[str] = mapped_column(String(255), index=True)   # email, chat id или телефон
    user_id: Mapped[int | None] = mapped_column(ForeignKey("users.id"), index=True)
    client_id: Mapped[int | None] = mapped_column(ForeignKey("clients.id"), index=True)
    source: Mapped[str] = mapped_column(String(300))           # где получено
    wording: Mapped[str] = mapped_column(Text)                 # текст согласия на момент получения
    ip: Mapped[str | None] = mapped_column(String(64))
    user_agent: Mapped[str | None] = mapped_column(String(300))
    recorded_by: Mapped[int | None] = mapped_column(ForeignKey("users.id"))   # сотрудник, если внёс вручную
    token: Mapped[str] = mapped_column(String(32), unique=True)     # для ссылки «Отписаться»
    given_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    revoked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    revoke_source: Mapped[str | None] = mapped_column(String(200))

    client: Mapped[Client | None] = relationship()

    @property
    def active(self) -> bool:
        return self.revoked_at is None


class Campaign(Base):
    """Рекламная рассылка по клиентам, давшим согласие."""

    __tablename__ = "campaigns"

    id: Mapped[int] = mapped_column(primary_key=True)
    title: Mapped[str] = mapped_column(String(300))
    channel: Mapped[str] = mapped_column(String(16))           # email | telegram
    subject: Mapped[str | None] = mapped_column(String(300))   # тема письма
    body: Mapped[str] = mapped_column(Text)
    advertiser: Mapped[str | None] = mapped_column(String(300))   # кто рекламодатель (для пометки)
    erid: Mapped[str | None] = mapped_column(String(100))      # токен маркировки, если получен в ОРД
    segment: Mapped[dict] = mapped_column(JSON, default=dict)
    status: Mapped[str] = mapped_column(String(16), default="draft")   # draft | queued | sent
    author_id: Mapped[int | None] = mapped_column(ForeignKey("users.id"))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    queued_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class Outbox(Base):
    """Очередь исходящих сообщений: уведомления клиентам и письма рассылок. Отправляет app.crm_tasks."""

    __tablename__ = "outbox"

    id: Mapped[int] = mapped_column(primary_key=True)
    kind: Mapped[str] = mapped_column(String(8))               # service | ads
    channel: Mapped[str] = mapped_column(String(16))
    address: Mapped[str] = mapped_column(String(255))
    subject: Mapped[str | None] = mapped_column(String(300))
    body: Mapped[str] = mapped_column(Text)
    campaign_id: Mapped[int | None] = mapped_column(ForeignKey("campaigns.id"), index=True)
    consent_id: Mapped[int | None] = mapped_column(ForeignKey("consents.id"))
    client_id: Mapped[int | None] = mapped_column(ForeignKey("clients.id"), index=True)
    status: Mapped[str] = mapped_column(String(16), default="queued", index=True)   # queued | sent | failed | skipped
    error: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    sent_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
