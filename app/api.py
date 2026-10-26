"""JSON API для кабинета и Telegram-бота."""

from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, Request, Response
from pydantic import BaseModel
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from .deps import answer_captcha, api_user, create_check, get_db, normalize_query, own_check
from .models import Check, CheckStatus, User

router = APIRouter(prefix="/api", tags=["api"])


class CheckIn(BaseModel):
    query: str


class CaptchaIn(BaseModel):
    answer: str


def check_out(check: Check, with_results: bool = True) -> dict:
    out = {
        "id": check.id, "query": check.query, "status": check.status,
        "title": check.title, "inn": check.inn, "ogrn": check.ogrn, "error": check.error,
        "created_at": check.created_at.isoformat(),
        "finished_at": check.finished_at.isoformat() if check.finished_at else None,
    }
    if with_results:
        out["results"] = [
            {"source": r.source, "status": r.status, "summary": r.summary, "error": r.error}
            for r in check.results
        ]
    return out


@router.post("/checks", status_code=201)
async def api_create(body: CheckIn, request: Request, user: User = Depends(api_user),
                     db: AsyncSession = Depends(get_db)) -> dict:
    query = normalize_query(body.query)
    if query is None:
        raise HTTPException(422, "Это не похоже на корректный ИНН или ОГРН")
    check = await create_check(request, db, user, query)
    return {"id": check.id, "status": check.status}


@router.get("/checks")
async def api_list(user: User = Depends(api_user), db: AsyncSession = Depends(get_db),
                   limit: int = 20) -> list[dict]:
    rows = await db.execute(select(Check).where(Check.user_id == user.id)
                            .order_by(Check.id.desc()).limit(min(limit, 100)))
    return [check_out(c, with_results=False) for c in rows.scalars()]


@router.get("/checks/{check_id}")
async def api_get(check_id: int, user: User = Depends(api_user),
                  db: AsyncSession = Depends(get_db)) -> dict:
    return check_out(await own_check(check_id, user, db))


@router.get("/checks/{check_id}/data/{source}")
async def api_raw(check_id: int, source: str, user: User = Depends(api_user),
                  db: AsyncSession = Depends(get_db)) -> dict:
    """Сырой ответ источника, например полный JSON карточки ФНС."""
    check = await own_check(check_id, user, db)
    for r in check.results:
        if r.source == source and r.data is not None:
            return r.data
    raise HTTPException(404, "Нет данных")


@router.get("/checks/{check_id}/captcha")
async def api_captcha_image(check_id: int, request: Request, user: User = Depends(api_user),
                            db: AsyncSession = Depends(get_db)) -> Response:
    check = await own_check(check_id, user, db)
    image = await request.app.state.captcha.image(check.id) if check.status == CheckStatus.CAPTCHA else None
    if image is None:
        raise HTTPException(404, "Капча сейчас не нужна")
    return Response(image, media_type="image/png", headers={"Cache-Control": "no-store"})


@router.post("/checks/{check_id}/captcha")
async def api_captcha_answer(check_id: int, body: CaptchaIn, request: Request,
                             user: User = Depends(api_user), db: AsyncSession = Depends(get_db)) -> dict:
    check = await own_check(check_id, user, db)
    if not await answer_captcha(request, db, check, body.answer):
        raise HTTPException(409, "Капча сейчас не нужна")
    return {"ok": True}
