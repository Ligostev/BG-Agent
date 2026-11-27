"""HTML-страницы кабинета (шаблоны Jinja2 + HTMX)."""

from __future__ import annotations

from pathlib import Path

from fastapi import APIRouter, Depends, Form, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.templating import Jinja2Templates
from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from .checks import format_status
from .crm_tasks import msk
from .deps import answer_captcha, create_check, get_db, normalize_query, own_check, session_user, web_user
from .models import Check, CheckStatus, User
from .security import hash_password, verify_password

templates = Jinja2Templates(directory=Path(__file__).parent / "templates")
templates.env.filters["status"] = format_status
templates.env.globals["CheckStatus"] = CheckStatus
templates.env.filters["msk"] = msk

router = APIRouter(include_in_schema=False)


def render(request: Request, name: str, status_code: int = 200, **ctx) -> HTMLResponse:
    ctx.setdefault("sources", {s.name: s.title for s in request.app.state.sources})
    return templates.TemplateResponse(request, name, ctx, status_code=status_code)


def go(url: str) -> RedirectResponse:
    return RedirectResponse(url, status_code=303)


def apply_admin_role(request: Request, user: User) -> None:
    if user.email and user.email in request.app.state.settings.admin_emails:
        user.role = "admin"


def home(user: User) -> str:
    return "/crm" if user.is_staff else "/checks"


@router.get("/")
async def index(user: User | None = Depends(session_user)):
    return go(home(user) if user else "/login")


@router.get("/register")
async def register_form(request: Request):
    return render(request, "register.html")


@router.post("/register")
async def register(request: Request, email: str = Form(), password: str = Form(),
                   db: AsyncSession = Depends(get_db)):
    email = email.strip().lower()
    if "@" not in email or len(password) < 8:
        return render(request, "register.html", 400, email=email,
                      error="Укажите email и пароль не короче 8 символов")
    user = User(email=email, password_hash=hash_password(password))
    apply_admin_role(request, user)
    db.add(user)
    try:
        await db.commit()
    except IntegrityError:
        return render(request, "register.html", 400, email=email,
                      error="Пользователь с таким email уже есть")
    request.session["uid"] = user.id
    return go(home(user))


@router.get("/login")
async def login_form(request: Request):
    return render(request, "login.html")


@router.post("/login")
async def login(request: Request, email: str = Form(), password: str = Form(),
                db: AsyncSession = Depends(get_db)):
    email = email.strip().lower()
    user = (await db.execute(select(User).where(func.lower(User.email) == email))).scalar_one_or_none()
    if user is None or not verify_password(password, user.password_hash):
        return render(request, "login.html", 400, email=email, error="Неверный email или пароль")
    if user.role != "admin" and user.email in request.app.state.settings.admin_emails:
        apply_admin_role(request, user)
        await db.commit()
    request.session["uid"] = user.id
    return go(home(user))


@router.post("/logout")
async def logout(request: Request):
    request.session.clear()
    return go("/login")


@router.get("/checks")
async def checks_page(request: Request, user: User = Depends(web_user),
                      db: AsyncSession = Depends(get_db)):
    rows = await db.execute(select(Check).where(Check.user_id == user.id)
                            .order_by(Check.id.desc()).limit(50))
    return render(request, "checks.html", user=user, checks=rows.scalars().all())


@router.post("/checks")
async def checks_create(request: Request, query: str = Form(), user: User = Depends(web_user),
                        db: AsyncSession = Depends(get_db)):
    q = normalize_query(query)
    if q is None:
        rows = await db.execute(select(Check).where(Check.user_id == user.id)
                                .order_by(Check.id.desc()).limit(50))
        return render(request, "checks.html", 400, user=user, checks=rows.scalars().all(),
                      query=query, error="Это не похоже на корректный ИНН или ОГРН")
    check = await create_check(request, db, user, q)
    return go(f"/checks/{check.id}")


@router.get("/checks/{check_id}")
async def check_page(check_id: int, request: Request, user: User = Depends(web_user),
                     db: AsyncSession = Depends(get_db)):
    check = await own_check(check_id, user, db)
    return render(request, "check.html", user=user, check=check)


@router.get("/checks/{check_id}/body")
async def check_body(check_id: int, request: Request, user: User = Depends(web_user),
                     db: AsyncSession = Depends(get_db)):
    """Фрагмент страницы проверки, который HTMX опрашивает, пока проверка идёт."""
    check = await own_check(check_id, user, db)
    return render(request, "_check_body.html", check=check)


@router.post("/checks/{check_id}/captcha")
async def check_captcha(check_id: int, request: Request, answer: str = Form(),
                        user: User = Depends(web_user), db: AsyncSession = Depends(get_db)):
    check = await own_check(check_id, user, db)
    await answer_captcha(request, db, check, answer)
    return go(f"/checks/{check.id}")
