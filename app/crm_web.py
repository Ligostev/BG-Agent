"""Раздел CRM для сотрудников: воронка сделок по гарантиям и карточки клиентов."""

from __future__ import annotations

import secrets
from datetime import timedelta

from fastapi import APIRouter, Depends, Form, HTTPException, Request, Response, UploadFile
from sqlalchemy import and_, func, or_, select, true, update
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from .crm import client_checks, client_title, fill_from_checks, log, set_stage
from .crm_tasks import MSK, aware, parse_date, parse_due
from .outreach import client_consents
from .deps import create_check, get_db, normalize_query, staff_user
from .models import (STAFF_ROLES, Activity, BankStatus, Client, Contact, Deal, DealBank, DealStage, Task, User,
                     utcnow)
from .tenders_web import _float, money
from .web import go, render

router = APIRouter(prefix="/crm", include_in_schema=False)

# Подсказки в поле «Банк»; можно вписать любой.
BANKS = ["Сбербанк", "ВТБ", "Альфа-Банк", "Газпромбанк", "ПСБ", "Совкомбанк", "МКБ", "Т-Банк",
         "Банк Уралсиб", "Абсолют Банк", "Банк «Санкт-Петербург»", "Металлинвестбанк", "Локо-Банк",
         "Реалист Банк", "Банк Зенит", "Ак Барс Банк"]

PRODUCTS = ["БГ на участие (обеспечение заявки)", "БГ на исполнение контракта",
            "БГ на гарантийные обязательства", "БГ на возврат аванса", "Страхование СМР"]


def ctx(**kw) -> dict:
    return dict(money=money, stages=DealStage.TITLES, open_stages=DealStage.OPEN,
                bank_statuses=BankStatus.TITLES, banks=BANKS, products=PRODUCTS,
                client_title=client_title, **kw)


def is_admin(user: User) -> bool:
    return user.role == "admin"


def visible_clients(user: User):
    """Администратор видит всех клиентов, менеджер своих и ещё не распределённых."""
    if is_admin(user):
        return true()
    return or_(Client.manager_id == user.id, Client.manager_id.is_(None))


def can_see(user: User, client: Client) -> bool:
    return is_admin(user) or client.manager_id in (None, user.id)


def visible_tasks(user: User, everyone: bool = False):
    if is_admin(user):
        return true() if everyone else or_(Task.assignee_id == user.id, Task.assignee_id.is_(None))
    shared = and_(Task.assignee_id.is_(None), or_(Task.client_id.is_(None), Task.client_id.in_(
        select(Client.id).where(visible_clients(user)))))
    return or_(Task.assignee_id == user.id, shared)


async def get_task(db: AsyncSession, user: User, task_id: int) -> Task:
    task = (await db.execute(select(Task).where(Task.id == task_id, visible_tasks(user, True)))).scalar_one_or_none()
    if task is None:
        raise HTTPException(404, "Задача не найдена")
    return task


async def staff_list(db: AsyncSession) -> list[User]:
    return list((await db.execute(select(User).where(User.role.in_(STAFF_ROLES)).order_by(User.email))).scalars())


def task_query():
    return select(Task).options(selectinload(Task.assignee), selectinload(Task.deal),
                                selectinload(Task.client).selectinload(Client.user))


async def open_tasks(db: AsyncSession, *where) -> list[Task]:
    q = task_query().where(Task.done_at.is_(None), *where).order_by(Task.due_at.is_(None), Task.due_at, Task.id)
    return list((await db.execute(q)).scalars())


def clean(raw: str, limit: int = 500) -> str | None:
    return " ".join((raw or "").split())[:limit] or None


async def get_client(db: AsyncSession, user: User, client_id: int) -> Client:
    client = await db.get(Client, client_id, options=[
        selectinload(Client.contacts), selectinload(Client.user),
        selectinload(Client.deals).selectinload(Deal.banks)], populate_existing=True)
    if client is None or not can_see(user, client):
        raise HTTPException(404, "Клиент не найден")
    return client


async def get_deal(db: AsyncSession, user: User, deal_id: int) -> Deal:
    deal = await db.get(Deal, deal_id, options=[
        selectinload(Deal.banks), selectinload(Deal.client).selectinload(Client.user)], populate_existing=True)
    if deal is None or not can_see(user, deal.client):
        raise HTTPException(404, "Сделка не найдена")
    return deal


async def activities(db: AsyncSession, client_id: int, deal_id: int | None = None) -> list[Activity]:
    q = select(Activity).options(selectinload(Activity.author)).where(Activity.client_id == client_id)
    if deal_id is not None:
        q = q.where(Activity.deal_id == deal_id)
    return list((await db.execute(q.order_by(Activity.id.desc()).limit(100))).scalars())


@router.get("")
async def crm_home(user: User = Depends(staff_user)):
    return go("/crm/deals")


# --- Сделки ---------------------------------------------------------------------------------------

@router.get("/deals")
async def deals_page(request: Request, stage: str = "", q: str = "",
                     user: User = Depends(staff_user), db: AsyncSession = Depends(get_db)):
    counts = dict((await db.execute(select(Deal.stage, func.count()).join(Client)
                                    .where(visible_clients(user)).group_by(Deal.stage))).all())
    query = (select(Deal).join(Client).where(visible_clients(user))
             .options(selectinload(Deal.client).selectinload(Client.user), selectinload(Deal.banks)))
    if stage in DealStage.TITLES:
        query = query.where(Deal.stage == stage)
    else:
        stage = ""
        query = query.where(Deal.stage.in_(DealStage.OPEN))
    if q.strip():
        like = f"%{q.strip()}%"
        query = query.where(or_(Client.title.ilike(like), Client.inn.like(like),
                                Deal.purchase_number.like(like), Deal.subject.ilike(like)))
    deals = list((await db.execute(query.order_by(Deal.updated_at.desc()).limit(300))).scalars())
    board = {s: [d for d in deals if d.stage == s] for s in (DealStage.OPEN if not stage else (stage,))}
    pipeline = sum(d.amount or 0 for d in deals)
    return render(request, "crm_deals.html", **ctx(user=user, board=board, counts=counts, stage=stage,
                                                    q=q, pipeline=pipeline, total=len(deals),
                                                    open_count=sum(counts.get(s, 0) for s in DealStage.OPEN)))


@router.post("/deals")
async def deal_create(request: Request, client_id: int = Form(), product: str = Form(),
                      amount: str = Form(""), purchase_number: str = Form(""), subject: str = Form(""),
                      user: User = Depends(staff_user), db: AsyncSession = Depends(get_db)):
    client = await get_client(db, user, client_id)
    deal = Deal(client_id=client.id, user_id=user.id, product=clean(product, 200) or PRODUCTS[1],
                amount=_float(amount), purchase_number=clean(purchase_number, 32), subject=clean(subject, 1000))
    db.add(deal)
    await db.flush()
    log(db, client.id, user, f"Создана сделка №{deal.id}: {deal.product}", deal_id=deal.id)
    await db.commit()
    return go(f"/crm/deals/{deal.id}")


@router.get("/deals/{deal_id}")
async def deal_page(deal_id: int, request: Request, user: User = Depends(staff_user),
                    db: AsyncSession = Depends(get_db)):
    deal = await get_deal(db, user, deal_id)
    return render(request, "crm_deal.html", **ctx(
        user=user, deal=deal, log=await activities(db, deal.client_id, deal.id),
        tasks=await open_tasks(db, Task.deal_id == deal.id), staff=await staff_list(db)))


@router.post("/deals/{deal_id}")
async def deal_update(deal_id: int, stage: str = Form(""), product: str = Form(""), amount: str = Form(""),
                      commission: str = Form(""), purchase_number: str = Form(""), expires_at: str = Form(""),
                      user: User = Depends(staff_user), db: AsyncSession = Depends(get_db)):
    deal = await get_deal(db, user, deal_id)
    if stage:
        await set_stage(db, deal, stage, user)
    deal.product = clean(product, 200) or deal.product
    deal.amount = _float(amount)
    deal.commission = _float(commission)
    deal.purchase_number = clean(purchase_number, 32)
    deal.expires_at = parse_date(expires_at)
    await db.commit()
    return go(f"/crm/deals/{deal.id}")


@router.post("/deals/{deal_id}/banks")
async def bank_add(deal_id: int, bank: str = Form(), user: User = Depends(staff_user),
                   db: AsyncSession = Depends(get_db)):
    deal = await get_deal(db, user, deal_id)
    name = clean(bank, 200)
    if name and not any(b.bank.lower() == name.lower() for b in deal.banks):
        deal.banks.append(DealBank(bank=name))
        log(db, deal.client_id, user, f"Сделка №{deal.id}: добавлен банк {name}", deal_id=deal.id)
        if deal.stage in (DealStage.NEW, DealStage.DOCS):
            await set_stage(db, deal, DealStage.BANKS, user)
        await db.commit()
    return go(f"/crm/deals/{deal.id}#banks")


@router.post("/deals/{deal_id}/banks/{bank_id}")
async def bank_update(deal_id: int, bank_id: int, status: str = Form(), rate: str = Form(""),
                      fee: str = Form(""), note: str = Form(""), user: User = Depends(staff_user),
                      db: AsyncSession = Depends(get_db)):
    deal = await get_deal(db, user, deal_id)
    row = next((b for b in deal.banks if b.id == bank_id), None)
    if row is None:
        raise HTTPException(404, "Банк не найден")
    if status in BankStatus.TITLES and status != row.status:
        row.status = status
        log(db, deal.client_id, user, f"Сделка №{deal.id}: {row.bank} — {row.status_title}", deal_id=deal.id)
        # Первое одобрение или выдача двигает сделку дальше по воронке.
        if status == "approved" and deal.stage in (DealStage.NEW, DealStage.DOCS, DealStage.BANKS):
            await set_stage(db, deal, DealStage.APPROVED, user)
        elif status == "issued" and deal.stage != DealStage.ISSUED:
            await set_stage(db, deal, DealStage.ISSUED, user)
    row.rate, row.fee, row.note = _float(rate), _float(fee), clean(note, 1000)
    await db.commit()
    return go(f"/crm/deals/{deal.id}#banks")


@router.post("/deals/{deal_id}/notes")
async def deal_note(deal_id: int, text: str = Form(), kind: str = Form("note"),
                    user: User = Depends(staff_user), db: AsyncSession = Depends(get_db)):
    deal = await get_deal(db, user, deal_id)
    if text.strip():
        log(db, deal.client_id, user, text.strip()[:5000], deal_id=deal.id,
            kind=kind if kind in ("note", "call") else "note")
        await db.commit()
    return go(f"/crm/deals/{deal.id}#log")


# --- Клиенты --------------------------------------------------------------------------------------

@router.get("/clients")
async def clients_page(request: Request, q: str = "", error: str = "", notice: str = "", user: User = Depends(staff_user),
                       db: AsyncSession = Depends(get_db)):
    query = (select(Client).where(visible_clients(user))
             .options(selectinload(Client.user), selectinload(Client.manager), selectinload(Client.deals)))
    if q.strip():
        like = f"%{q.strip()}%"
        query = query.outerjoin(User, Client.user_id == User.id).where(
            or_(Client.title.ilike(like), Client.inn.like(like), User.email.ilike(like)))
    clients = list((await db.execute(query.order_by(Client.id.desc()).limit(300))).scalars())
    return render(request, "crm_clients.html", **ctx(user=user, clients=clients, q=q, error=error, notice=notice))


@router.post("/clients")
async def client_create(request: Request, inn: str = Form(""), title: str = Form(""), source: str = Form(""),
                        run_check: str = Form(""), user: User = Depends(staff_user),
                        db: AsyncSession = Depends(get_db)):
    digits = "".join(ch for ch in inn if ch.isdigit())
    if digits and (normalize_query(digits) is None or len(digits) not in (10, 12)):
        return await clients_page(request, error="ИНН должен быть из 10 или 12 цифр", user=user, db=db)
    if not digits and not title.strip():
        return await clients_page(request, error="Укажите ИНН или название", user=user, db=db)
    if digits:
        existing = (await db.execute(select(Client).where(Client.inn == digits))).scalar_one_or_none()
        if existing is not None:
            if not can_see(user, existing):
                return await clients_page(request, error="Клиент с этим ИНН уже закреплён за другим менеджером",
                                          user=user, db=db)
            return go(f"/crm/clients/{existing.id}")
    client = Client(inn=digits or None, title=clean(title), source=clean(source, 200),
                    manager_id=None if is_admin(user) else user.id)
    db.add(client)
    await db.flush()
    log(db, client.id, user, "Клиент добавлен в CRM")
    await db.commit()
    if digits and run_check:
        await start_check(request, db, client, user)
    return go(f"/crm/clients/{client.id}")


async def start_check(request: Request, db: AsyncSession, client: Client, user: User):
    check = await create_check(request, db, user, client.inn)
    log(db, client.id, user, f"Запущена проверка по ИНН {client.inn}")
    await db.commit()
    return check


@router.get("/clients/{client_id}")
async def client_page(client_id: int, request: Request, user: User = Depends(staff_user),
                      db: AsyncSession = Depends(get_db)):
    client = await get_client(db, user, client_id)
    before = (client.title, client.ogrn)
    await fill_from_checks(db, client)
    if (client.title, client.ogrn) != before:
        await db.commit()
    return render(request, "crm_client.html", **ctx(
        user=user, client=client, checks=await client_checks(db, client), log=await activities(db, client.id),
        tasks=await open_tasks(db, Task.client_id == client.id), staff=await staff_list(db),
        consents=await client_consents(db, client)))


@router.post("/clients/{client_id}")
async def client_update(client_id: int, request: Request, title: str = Form(""), inn: str = Form(""),
                        source: str = Form(""), notes: str = Form(""), manager_id: str = Form(""),
                        user: User = Depends(staff_user),
                        db: AsyncSession = Depends(get_db)):
    client = await get_client(db, user, client_id)
    digits = "".join(ch for ch in inn if ch.isdigit()) or None
    if digits != client.inn:
        if digits and len(digits) not in (10, 12):
            raise HTTPException(400, "ИНН должен быть из 10 или 12 цифр")
        if digits and (await db.execute(select(Client.id).where(Client.inn == digits))).first():
            raise HTTPException(400, "Клиент с таким ИНН уже есть")
        client.inn, client.ogrn = digits, None
    client.title = clean(title)
    client.source = clean(source, 200)
    client.notes = (notes or "").strip()[:10000] or None
    new_manager = int(manager_id) if manager_id.isdigit() else None
    if new_manager != client.manager_id:
        manager = await db.get(User, new_manager) if new_manager else None
        if new_manager and (manager is None or not manager.is_staff):
            raise HTTPException(400, "Ответственным может быть только сотрудник")
        if not is_admin(user) and new_manager not in (None, user.id):
            raise HTTPException(403, "Передать клиента другому сотруднику может только администратор")
        client.manager_id = new_manager
        log(db, client.id, user, f"Ответственный: {manager.email if manager else 'не назначен'}")
    await db.commit()
    return go(f"/crm/clients/{client.id}")


@router.post("/clients/{client_id}/check")
async def client_check(client_id: int, request: Request, user: User = Depends(staff_user),
                       db: AsyncSession = Depends(get_db)):
    client = await get_client(db, user, client_id)
    if not client.inn:
        raise HTTPException(400, "Сначала укажите ИНН клиента")
    check = await start_check(request, db, client, user)
    return go(f"/checks/{check.id}")


@router.post("/clients/{client_id}/contacts")
async def contact_add(client_id: int, name: str = Form(), position: str = Form(""), phone: str = Form(""),
                      email: str = Form(""), telegram: str = Form(""), user: User = Depends(staff_user),
                      db: AsyncSession = Depends(get_db)):
    client = await get_client(db, user, client_id)
    if name.strip():
        client.contacts.append(Contact(name=clean(name, 200), position=clean(position, 200),
                                       phone=clean(phone, 50), email=clean(email, 255),
                                       telegram=clean(telegram, 100)))
        await db.commit()
    return go(f"/crm/clients/{client.id}#contacts")


@router.post("/clients/{client_id}/contacts/{contact_id}/delete")
async def contact_delete(client_id: int, contact_id: int, user: User = Depends(staff_user),
                         db: AsyncSession = Depends(get_db)):
    client = await get_client(db, user, client_id)
    client.contacts = [c for c in client.contacts if c.id != contact_id]
    await db.commit()
    return go(f"/crm/clients/{client.id}#contacts")


@router.post("/clients/{client_id}/notes")
async def client_note(client_id: int, text: str = Form(), kind: str = Form("note"),
                      user: User = Depends(staff_user), db: AsyncSession = Depends(get_db)):
    client = await get_client(db, user, client_id)
    if text.strip():
        log(db, client.id, user, text.strip()[:5000], kind=kind if kind in ("note", "call") else "note")
        await db.commit()
    return go(f"/crm/clients/{client.id}#log")


# --- Задачи ---------------------------------------------------------------------------------------

def back_url(raw: str, default: str = "/crm/tasks") -> str:
    return raw if raw.startswith("/crm/") and "//" not in raw else default


@router.get("/tasks")
async def tasks_page(request: Request, who: str = "me", user: User = Depends(staff_user),
                     db: AsyncSession = Depends(get_db)):
    who = who if (who == "all" and is_admin(user)) else "me"
    where = [visible_tasks(user, everyone=who == "all")]
    tasks = await open_tasks(db, *where)
    now = utcnow()
    end_of_day = (now.astimezone(MSK).replace(hour=23, minute=59, second=59))
    groups = {"Просрочено": [], "Сегодня": [], "Дальше": [], "Без срока": []}
    for t in tasks:
        due = aware(t.due_at)
        key = ("Без срока" if due is None else "Просрочено" if due < now
               else "Сегодня" if due <= end_of_day else "Дальше")
        groups[key].append(t)
    done = list((await db.execute(task_query().where(Task.done_at.is_not(None), *where)
                                  .order_by(Task.done_at.desc()).limit(20))).scalars())
    settings = request.app.state.settings
    clients = list((await db.execute(select(Client).where(visible_clients(user)).options(selectinload(Client.user))
                                     .order_by(Client.id.desc()).limit(500))).scalars())
    return render(request, "crm_tasks.html", **ctx(
        user=user, who=who, groups=groups, done=done, staff=await staff_list(db), clients=clients,
        bot_username=settings.bot_username, bot_ready=bool(settings.bot_token)))


@router.post("/tasks")
async def task_create(title: str = Form(), due: str = Form(""), assignee_id: str = Form(""),
                      client_id: str = Form(""), deal_id: str = Form(""), remind: str = Form(""),
                      back: str = Form(""), user: User = Depends(staff_user), db: AsyncSession = Depends(get_db)):
    deal = await get_deal(db, user, int(deal_id)) if deal_id.isdigit() else None
    client = deal.client if deal else (await get_client(db, user, int(client_id)) if client_id.isdigit() else None)
    assignee = int(assignee_id) if assignee_id.isdigit() else None
    if assignee_id == "me":
        assignee = user.id
    elif assignee_id == "" and client is not None:
        assignee = client.manager_id
    text = clean(title, 500)
    if text:
        db.add(Task(title=text, due_at=parse_due(due), assignee_id=assignee, author_id=user.id,
                    client_id=client.id if client else None, deal_id=deal.id if deal else None,
                    remind=bool(remind)))
        if client is not None:
            log(db, client.id, user, f"Задача: {text}", deal_id=deal.id if deal else None)
        await db.commit()
    return go(back_url(back))


@router.post("/tasks/{task_id}/done")
async def task_done(task_id: int, back: str = Form(""), user: User = Depends(staff_user),
                    db: AsyncSession = Depends(get_db)):
    task = await get_task(db, user, task_id)
    task.done_at = None if task.done_at else utcnow()
    if task.done_at and task.client_id:
        log(db, task.client_id, user, f"Выполнено: {task.title}", deal_id=task.deal_id)
    await db.commit()
    return go(back_url(back))


@router.post("/tasks/{task_id}/postpone")
async def task_postpone(task_id: int, days: int = Form(1), back: str = Form(""),
                        user: User = Depends(staff_user), db: AsyncSession = Depends(get_db)):
    task = await get_task(db, user, task_id)
    base = max(aware(task.due_at) or utcnow(), utcnow())
    task.due_at = base + timedelta(days=max(1, min(days, 30)))
    task.reminded_at = None
    await db.commit()
    return go(back_url(back))




# --- Сотрудники (только администратор) ----------------------------------------------------------------

@router.get("/staff")
async def staff_page(request: Request, error: str = "", notice: str = "", user: User = Depends(staff_user),
                     db: AsyncSession = Depends(get_db)):
    if not is_admin(user):
        raise HTTPException(403, "Раздел только для администратора")
    staff = await staff_list(db)
    counts = dict((await db.execute(select(Client.manager_id, func.count()).group_by(Client.manager_id))).all())
    return render(request, "crm_staff.html", **ctx(user=user, staff=staff, counts=counts, error=error,
                                                    notice=notice, admin_emails=request.app.state.settings.admin_emails))


@router.post("/staff")
async def staff_set_role(request: Request, email: str = Form(), role: str = Form(),
                         user: User = Depends(staff_user), db: AsyncSession = Depends(get_db)):
    if not is_admin(user):
        raise HTTPException(403, "Раздел только для администратора")
    email = email.strip().lower()
    if role not in ("client", "manager", "admin"):
        raise HTTPException(400, "Неизвестная роль")
    if email in request.app.state.settings.admin_emails and role != "admin":
        return await staff_page(request, error=f"{email} указан в ADMIN_EMAILS на сервере, роль вернётся при входе",
                                user=user, db=db)
    target = (await db.execute(select(User).where(func.lower(User.email) == email))).scalar_one_or_none()
    if target is None:
        return await staff_page(request, error="Пользователь с таким email не найден: пусть сначала "
                                               "зарегистрируется в кабинете", user=user, db=db)
    target.role = role
    if role == "client":
        # Его клиенты возвращаются в общий список, открытые задачи становятся общими.
        await db.execute(update(Client).where(Client.manager_id == target.id).values(manager_id=None))
        await db.execute(update(Task).where(Task.assignee_id == target.id, Task.done_at.is_(None))
                         .values(assignee_id=None))
    await db.commit()
    return go("/crm/staff")


# --- Аналитика и Excel -----------------------------------------------------------------------------

PERIODS = {"month": "Этот месяц", "prev": "Прошлый месяц", "quarter": "3 месяца", "year": "12 месяцев"}


def period_bounds(period: str, today) -> tuple:
    from .crm_reports import month_start
    if period == "prev":
        start = month_start(today, 1)
        return start, month_start(today) - timedelta(days=1)
    if period == "quarter":
        return month_start(today, 2), today
    if period == "year":
        return month_start(today, 11), today
    return month_start(today), today


async def visible_deals(db: AsyncSession, user: User) -> list[Deal]:
    q = (select(Deal).join(Client).where(visible_clients(user))
         .options(selectinload(Deal.banks), selectinload(Deal.client).selectinload(Client.manager),
                  selectinload(Deal.client).selectinload(Client.user)).order_by(Deal.id))
    return list((await db.execute(q)).scalars())


@router.get("/reports")
async def reports_page(request: Request, period: str = "month", user: User = Depends(staff_user),
                       db: AsyncSession = Depends(get_db)):
    from .crm_reports import build
    period = period if period in PERIODS else "month"
    today = utcnow().astimezone(MSK).date()
    start, end = period_bounds(period, today)
    report = build(await visible_deals(db, user), start, end, today)
    return render(request, "crm_reports.html", **ctx(user=user, r=report, period=period, periods=PERIODS,
                                                      start=start, end=end))


def xlsx(data: bytes, name: str) -> Response:
    return Response(data, media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
                    headers={"Content-Disposition": f'attachment; filename="{name}"'})


@router.get("/deals.xlsx")
async def deals_export(user: User = Depends(staff_user), db: AsyncSession = Depends(get_db)):
    from .crm_excel import export_deals
    return xlsx(export_deals(await visible_deals(db, user), client_title), "deals.xlsx")


@router.get("/clients.xlsx")
async def clients_export(user: User = Depends(staff_user), db: AsyncSession = Depends(get_db)):
    from .crm_excel import export_clients
    q = (select(Client).where(visible_clients(user)).order_by(Client.id)
         .options(selectinload(Client.contacts), selectinload(Client.manager), selectinload(Client.user),
                  selectinload(Client.deals)))
    return xlsx(export_clients(list((await db.execute(q)).scalars()), client_title), "clients.xlsx")


MAX_IMPORT = 10 * 1024 * 1024


@router.post("/import/clients")
async def clients_import(request: Request, file: UploadFile, user: User = Depends(staff_user),
                         db: AsyncSession = Depends(get_db)):
    from .crm_excel import load_clients
    data = await file.read(MAX_IMPORT + 1)
    if len(data) > MAX_IMPORT:
        return await clients_page(request, error="Файл больше 10 МБ", user=user, db=db)
    try:
        items = load_clients(data, file.filename or "")
    except Exception as e:
        return await clients_page(request, error=f"Не получилось разобрать файл: {e}", user=user, db=db)
    added = skipped = bad = 0
    seen: set[str] = set()
    for it in items:
        if it["bad_inn"]:
            bad += 1
            continue
        if it["inn"]:
            if it["inn"] in seen or (await db.execute(select(Client.id).where(Client.inn == it["inn"]))).first():
                skipped += 1
                continue
            seen.add(it["inn"])
        client = Client(inn=it["inn"] or None, title=clean(it["title"]), source=clean(it["source"], 200)
                        or "Импорт из файла", notes=(it["notes"] or None),
                        manager_id=None if is_admin(user) else user.id)
        if it["contact"] or it["phone"] or it["email"]:
            client.contacts.append(Contact(name=clean(it["contact"], 200) or "Контакт",
                                           position=clean(it["position"], 200), phone=clean(it["phone"], 50),
                                           email=clean(it["email"], 255)))
        db.add(client)
        await db.flush()
        log(db, client.id, user, f"Клиент загружен из файла {file.filename or ''}".strip())
        added += 1
    await db.commit()
    parts = [f"Добавлено клиентов: {added}"]
    if skipped:
        parts.append(f"уже были в CRM: {skipped}")
    if bad:
        parts.append(f"с неверным ИНН: {bad}")
    return await clients_page(request, notice=", ".join(parts), user=user, db=db)


# --- Согласия и рассылки -----------------------------------------------------------------------------

@router.post("/clients/{client_id}/consents")
async def consent_add(client_id: int, request: Request, channel: str = Form(), address: str = Form(),
                      source: str = Form(), user: User = Depends(staff_user), db: AsyncSession = Depends(get_db)):
    """Согласие, полученное вне кабинета (подписанная форма, письмо клиента). Источник обязателен:
    по закону доказывать согласие придётся нам."""
    from .outreach import CHANNEL_TITLES, give_consent
    client = await get_client(db, user, client_id)
    address, source = (address or "").strip(), clean(source, 250)
    if channel not in CHANNEL_TITLES or not address or not source:
        raise HTTPException(400, "Укажите канал, адрес и где получено согласие")
    if channel == "email":
        address = address.lower()
    await give_consent(db, request.app.state.settings, kind="ads", channel=channel, address=address,
                       source=f"Внесено сотрудником {user.email}: {source}", client_id=client.id, recorded_by=user)
    log(db, client.id, user, f"Согласие на рекламу: {CHANNEL_TITLES[channel]} {address} ({source})")
    await db.commit()
    return go(f"/crm/clients/{client.id}#consents")


@router.post("/clients/{client_id}/consents/{consent_id}/revoke")
async def consent_revoke(client_id: int, consent_id: int, user: User = Depends(staff_user),
                         db: AsyncSession = Depends(get_db)):
    from .outreach import client_consents, revoke_address
    client = await get_client(db, user, client_id)
    consent = next((c for c in await client_consents(db, client) if c.id == consent_id), None)
    if consent is None:
        raise HTTPException(404, "Согласие не найдено")
    await revoke_address(db, consent.kind, consent.channel, consent.address, f"Отозвано по просьбе клиента ({user.email})")
    log(db, client.id, user, f"Отзыв согласия: {consent.channel} {consent.address}")
    await db.commit()
    return go(f"/crm/clients/{client.id}#consents")


def admin_only(user: User) -> None:
    if not is_admin(user):
        raise HTTPException(403, "Рассылки запускает администратор")


@router.get("/campaigns")
async def campaigns_page(request: Request, user: User = Depends(staff_user), db: AsyncSession = Depends(get_db)):
    from .models import Campaign
    from .outreach import SEGMENTS, campaign_stats
    admin_only(user)
    rows = list((await db.execute(select(Campaign).order_by(Campaign.id.desc()).limit(100))).scalars())
    s = request.app.state.settings
    return render(request, "crm_campaigns.html", **ctx(
        user=user, campaigns=rows, stats=await campaign_stats(db, [c.id for c in rows]), segments=SEGMENTS,
        mail_ready=bool(s.smtp_host and s.smtp_from), bot_ready=bool(s.bot_token),
        operator_ready=bool(s.operator_name)))


@router.post("/campaigns")
async def campaign_create(request: Request, title: str = Form(), channel: str = Form(), subject: str = Form(""),
                          body: str = Form(), advertiser: str = Form(""), erid: str = Form(""),
                          group: str = Form("all"), source: str = Form(""), user: User = Depends(staff_user),
                          db: AsyncSession = Depends(get_db)):
    from .models import Campaign
    from .outreach import SEGMENTS
    admin_only(user)
    if channel not in ("email", "telegram") or not body.strip():
        raise HTTPException(400, "Нужны канал и текст")
    c = Campaign(title=clean(title, 300) or "Рассылка", channel=channel, subject=clean(subject, 300),
                 body=body.strip()[:4000], advertiser=clean(advertiser, 300), erid=clean(erid, 100),
                 segment={"group": group if group in SEGMENTS else "all", "source": clean(source, 200) or ""},
                 author_id=user.id)
    db.add(c)
    await db.commit()
    return go(f"/crm/campaigns/{c.id}")


@router.get("/campaigns/{campaign_id}")
async def campaign_page(campaign_id: int, request: Request, user: User = Depends(staff_user),
                        db: AsyncSession = Depends(get_db)):
    from .models import Campaign, Outbox
    from .outreach import SEGMENTS, ad_footer, campaign_stats, recipients
    admin_only(user)
    c = await db.get(Campaign, campaign_id)
    if c is None:
        raise HTTPException(404, "Рассылка не найдена")
    targets = await recipients(db, c) if c.status == "draft" else []
    s = request.app.state.settings
    preview = f"{c.body}\n\n" + ad_footer(c, s, targets[0] if targets else type("C", (), {"token": "…"})(),
                                          s.public_url)
    failed = list((await db.execute(select(Outbox).where(Outbox.campaign_id == c.id, Outbox.status == "failed")
                                    .limit(20))).scalars())
    return render(request, "crm_campaign.html", **ctx(
        user=user, c=c, targets=targets, preview=preview, segments=SEGMENTS,
        stats=(await campaign_stats(db, [c.id])).get(c.id, {}), failed=failed))


@router.post("/campaigns/{campaign_id}/send")
async def campaign_send(campaign_id: int, request: Request, confirm: str = Form(""),
                        user: User = Depends(staff_user), db: AsyncSession = Depends(get_db)):
    from .models import Campaign
    from .outreach import queue_campaign
    admin_only(user)
    c = await db.get(Campaign, campaign_id)
    if c is None:
        raise HTTPException(404, "Рассылка не найдена")
    if c.status != "draft" or not confirm:
        return go(f"/crm/campaigns/{c.id}")
    if c.channel == "email" and not request.app.state.settings.public_url:
        # Без адреса кабинета в письме не будет рабочей ссылки «Отписаться», а она обязательна.
        raise HTTPException(400, "Задайте PUBLIC_URL или DOMAIN на сервере: без них в письме нет ссылки «Отписаться»")
    await queue_campaign(db, c, request.app.state.settings, request.app.state.settings.public_url)
    await db.commit()
    return go(f"/crm/campaigns/{c.id}")


@router.post("/campaigns/{campaign_id}/delete")
async def campaign_delete(campaign_id: int, user: User = Depends(staff_user), db: AsyncSession = Depends(get_db)):
    from .models import Campaign
    admin_only(user)
    c = await db.get(Campaign, campaign_id)
    if c is not None and c.status == "draft":
        await db.delete(c)
        await db.commit()
    return go("/crm/campaigns")
