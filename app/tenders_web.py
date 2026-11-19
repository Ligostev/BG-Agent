"""Страница «Закупки»: профиль клиента, подборка закупок из ЕИС и заявки на гарантию."""

from __future__ import annotations

from datetime import datetime, timezone

from fastapi import APIRouter, Depends, Form, HTTPException, Request, UploadFile
from sqlalchemy import or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from tenders import Profile, match_notice
from tenders.imports import load_contracts
from tenders.match import Match, guarantee_products, parse_codes, parse_regions, profile_from_contracts
from tenders.notice import Notice, eis_url
from tenders.eis import LAWS
from tenders.regions import REGIONS

from .deps import get_db, web_user
from .models import GuaranteeRequest, TenderNotice, TenderProfile, User
from .web import go, render

router = APIRouter(include_in_schema=False)

MAX_UPLOAD = 10 * 1024 * 1024


def money(v) -> str:
    return f"{v:,.0f} ₽".replace(",", " ") if v else "—"


async def get_profile(db: AsyncSession, user: User) -> TenderProfile:
    row = (await db.execute(select(TenderProfile).where(TenderProfile.user_id == user.id))).scalar_one_or_none()
    return row or TenderProfile(user_id=user.id, okpd2=[], regions=[], keywords=[], laws=list(LAWS),
                                min_days_left=2)


def to_profile(row: TenderProfile) -> Profile:
    return Profile(okpd2=list(row.okpd2 or []), regions=list(row.regions or []),
                   keywords=list(row.keywords or []),
                   laws=list(row.laws) if row.laws is not None else list(LAWS), price_min=row.price_min, price_max=row.price_max,
                   min_days_left=row.min_days_left if row.min_days_left is not None else 2)


async def find_matches(db: AsyncSession, profile: Profile, limit: int = 100) -> list[Match]:
    now = datetime.now(timezone.utc)
    q = select(TenderNotice).where(or_(TenderNotice.ends_at.is_(None), TenderNotice.ends_at > now))
    if profile.regions:
        q = q.where(TenderNotice.region.in_(profile.regions))
    matches = []
    for row in (await db.execute(q)).scalars():
        m = match_notice(Notice.from_dict(row.data), profile, now)
        if m:
            matches.append(m)
    matches.sort(key=lambda m: (-m.score, m.notice.ends_at or now))
    return matches[:limit]


def _float(raw: str) -> float | None:
    raw = (raw or "").replace(" ", "").replace("\xa0", "").replace(",", ".")
    try:
        return float(raw) if raw else None
    except ValueError:
        return None


async def page(request: Request, db: AsyncSession, user: User, status_code: int = 200, **ctx):
    row = await get_profile(db, user)
    profile = to_profile(row)
    matches = await find_matches(db, profile) if (profile.okpd2 or profile.keywords) else []
    requests = (await db.execute(select(GuaranteeRequest).where(GuaranteeRequest.user_id == user.id)
                                 .order_by(GuaranteeRequest.id.desc()).limit(20))).scalars().all()
    eis_ready = bool(request.app.state.settings.eis_token)
    return render(request, "tenders.html", status_code, user=user, profile=row, matches=matches,
                  requests=requests, regions=REGIONS, money=money, eis_url=eis_url,
                  eis_ready=eis_ready, laws=LAWS, **ctx)


@router.get("/tenders")
async def tenders_page(request: Request, user: User = Depends(web_user), db: AsyncSession = Depends(get_db)):
    return await page(request, db, user)


@router.post("/tenders/profile")
async def save_profile(request: Request, okpd2: str = Form(""), regions: str = Form(""),
                       keywords: str = Form(""), price_min: str = Form(""), price_max: str = Form(""),
                       min_days_left: int = Form(2), laws: list[str] = Form(default=[]), user: User = Depends(web_user),
                       db: AsyncSession = Depends(get_db)):
    row = await get_profile(db, user)
    row.okpd2 = parse_codes(okpd2)
    row.regions = parse_regions(regions)
    row.keywords = [w.strip() for w in keywords.replace(";", ",").replace("\n", ",").split(",") if w.strip()]
    row.laws = [x for x in LAWS if x in laws] or list(LAWS)
    row.price_min, row.price_max = _float(price_min), _float(price_max)
    row.min_days_left = max(0, min(min_days_left, 60))
    db.add(row)
    await db.commit()
    return go("/tenders")


@router.post("/tenders/profile/import")
async def import_contracts(request: Request, file: UploadFile, user: User = Depends(web_user),
                           db: AsyncSession = Depends(get_db)):
    data = await file.read(MAX_UPLOAD + 1)
    if len(data) > MAX_UPLOAD:
        return await page(request, db, user, 400, error="Файл больше 10 МБ")
    try:
        contracts = load_contracts(data, file.filename or "")
    except Exception as e:      # битый xlsx, не та таблица
        return await page(request, db, user, 400, error=f"Не получилось разобрать файл: {e}")
    if not contracts:
        return await page(request, db, user, 400, error="В файле нет строк с контрактами")
    row = await get_profile(db, user)
    p = profile_from_contracts(contracts, to_profile(row))
    row.okpd2, row.regions = p.okpd2, p.regions
    row.price_min, row.price_max = p.price_min, p.price_max
    db.add(row)
    await db.commit()
    return await page(request, db, user, notice=f"Профиль дополнен по {len(contracts)} контрактам из файла")


@router.post("/tenders/{number}/guarantee")
async def ask_guarantee(number: str, request: Request, user: User = Depends(web_user),
                        db: AsyncSession = Depends(get_db)):
    row = (await db.execute(select(TenderNotice).where(TenderNotice.purchase_number == number))).scalar_one_or_none()
    if row is None:
        raise HTTPException(404, "Закупка не найдена")
    n = Notice.from_dict(row.data)
    exists = (await db.execute(select(GuaranteeRequest).where(
        GuaranteeRequest.user_id == user.id, GuaranteeRequest.purchase_number == number))).first()
    if not exists:
        products = guarantee_products(n) or [("БГ на исполнение контракта", None)]
        for product, amount in products:
            if product.startswith("БГ"):
                db.add(GuaranteeRequest(user_id=user.id, purchase_number=number, subject=n.subject[:1000],
                                        product=product, amount=amount))
        await db.commit()
    return go("/tenders#requests")
