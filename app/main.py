import os
from pathlib import Path
from typing import Annotated

from fastapi import Depends, FastAPI, Form, HTTPException, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from sqlalchemy import func, or_, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session
from starlette.middleware.sessions import SessionMiddleware

from .database import Base, engine, get_db
from .models import Registration, Season, Tournament, User
from .security import hash_password, verify_password
from .services import audit, unique_slug

BASE = Path(__file__).resolve().parent
app = FastAPI(title="FC26 Arena V5")
app.add_middleware(
    SessionMiddleware,
    secret_key=os.getenv("SECRET_KEY", "troque-esta-chave"),
    same_site="lax",
    https_only=False,
)
app.mount("/static", StaticFiles(directory=BASE / "static"), name="static")
templates = Jinja2Templates(directory=BASE / "templates")

ADMIN_PASSWORD = os.getenv("ADMIN_PASSWORD", "admin123")

@app.on_event("startup")
def startup():
    Base.metadata.create_all(bind=engine)
    with next(get_db()) as db:
        if not db.scalar(select(Season).limit(1)):
            db.add(Season(name="Temporada 1", active=True))
            db.commit()

def ctx(request: Request, **kwargs):
    return {"request": request, **kwargs}

def current_user(request: Request, db: Session):
    user_id = request.session.get("user_id")
    return db.get(User, user_id) if user_id else None

def require_admin(request: Request):
    if not request.session.get("admin"):
        raise HTTPException(403)

@app.get("/", response_class=HTMLResponse)
def home(request: Request, db: Session = Depends(get_db)):
    tournaments = db.scalars(
        select(Tournament).where(Tournament.status != "archived").order_by(Tournament.id.desc())
    ).all()
    return templates.TemplateResponse("home.html", ctx(request, tournaments=tournaments))

@app.get("/cadastro", response_class=HTMLResponse)
def register_page(request: Request):
    return templates.TemplateResponse("register.html", ctx(request, error=None))

@app.post("/cadastro")
def register(
    request: Request,
    name: Annotated[str, Form()],
    whatsapp: Annotated[str, Form()],
    email: Annotated[str | None, Form()] = None,
    ea_id: Annotated[str, Form()] = "",
    platform: Annotated[str, Form()] = "",
    generation: Annotated[str, Form()] = "nova",
    password: Annotated[str, Form()] = "",
    db: Session = Depends(get_db),
):
    if len(password) < 6:
        return templates.TemplateResponse("register.html", ctx(request, error="A senha deve ter ao menos 6 caracteres."), status_code=400)
    user = User(
        name=name.strip(),
        whatsapp=whatsapp.strip(),
        email=email.strip() if email else None,
        ea_id=ea_id.strip(),
        platform=platform,
        generation=generation,
        password_hash=hash_password(password),
    )
    db.add(user)
    try:
        db.commit()
        db.refresh(user)
    except IntegrityError:
        db.rollback()
        return templates.TemplateResponse("register.html", ctx(request, error="WhatsApp, e-mail ou ID EA já cadastrado."), status_code=400)
    request.session["user_id"] = user.id
    return RedirectResponse("/painel", 303)

@app.get("/login", response_class=HTMLResponse)
def login_page(request: Request):
    return templates.TemplateResponse("login.html", ctx(request, error=None))

@app.post("/login")
def login(
    request: Request,
    identifier: Annotated[str, Form()],
    password: Annotated[str, Form()],
    db: Session = Depends(get_db),
):
    user = db.scalar(select(User).where(or_(
        User.whatsapp == identifier.strip(),
        User.email == identifier.strip(),
        User.ea_id == identifier.strip(),
    )))
    if not user or not verify_password(password, user.password_hash):
        return templates.TemplateResponse("login.html", ctx(request, error="Dados incorretos."), status_code=400)
    request.session["user_id"] = user.id
    return RedirectResponse("/painel", 303)

@app.get("/sair")
def logout(request: Request):
    request.session.clear()
    return RedirectResponse("/", 303)

@app.get("/painel", response_class=HTMLResponse)
def dashboard(request: Request, db: Session = Depends(get_db)):
    user = current_user(request, db)
    if not user:
        return RedirectResponse("/login", 303)
    registrations = db.scalars(
        select(Registration).where(Registration.user_id == user.id).order_by(Registration.id.desc())
    ).all()
    return templates.TemplateResponse("dashboard.html", ctx(request, user=user, registrations=registrations))

@app.get("/torneio/{slug}", response_class=HTMLResponse)
def tournament_page(slug: str, request: Request, db: Session = Depends(get_db)):
    tournament = db.scalar(select(Tournament).where(Tournament.slug == slug))
    if not tournament:
        raise HTTPException(404)
    total = db.scalar(select(func.count()).select_from(Registration).where(Registration.tournament_id == tournament.id))
    user = current_user(request, db)
    registered = False
    if user:
        registered = bool(db.scalar(select(Registration).where(
            Registration.tournament_id == tournament.id,
            Registration.user_id == user.id,
        )))
    return templates.TemplateResponse("tournament.html", ctx(
        request, tournament=tournament, total=total, user=user, registered=registered
    ))

@app.post("/torneio/{slug}/inscrever")
def tournament_register(slug: str, request: Request, db: Session = Depends(get_db)):
    user = current_user(request, db)
    if not user:
        return RedirectResponse("/login", 303)
    tournament = db.scalar(select(Tournament).where(Tournament.slug == slug))
    if not tournament:
        raise HTTPException(404)
    if tournament.status not in {"open", "draft"}:
        return RedirectResponse(f"/torneio/{slug}", 303)
    existing = db.scalar(select(Registration).where(
        Registration.tournament_id == tournament.id,
        Registration.user_id == user.id,
    ))
    if not existing:
        status = "approved" if float(tournament.registration_fee) == 0 else "pending"
        payment_status = "not_required" if float(tournament.registration_fee) == 0 else "pending"
        db.add(Registration(
            tournament_id=tournament.id,
            user_id=user.id,
            status=status,
            payment_status=payment_status,
        ))
        db.commit()
    return RedirectResponse(f"/torneio/{slug}", 303)

@app.get("/admin/login", response_class=HTMLResponse)
def admin_login_page(request: Request):
    return templates.TemplateResponse("admin_login.html", ctx(request, error=None))

@app.post("/admin/login")
def admin_login(
    request: Request,
    password: Annotated[str, Form()],
):
    if password != ADMIN_PASSWORD:
        return templates.TemplateResponse("admin_login.html", ctx(request, error="Senha incorreta."), status_code=400)
    request.session["admin"] = True
    return RedirectResponse("/admin", 303)

@app.get("/admin", response_class=HTMLResponse)
def admin(request: Request, db: Session = Depends(get_db)):
    require_admin(request)
    tournaments = db.scalars(select(Tournament).order_by(Tournament.id.desc())).all()
    return templates.TemplateResponse("admin.html", ctx(request, tournaments=tournaments, message=request.query_params.get("message")))

@app.post("/admin/torneios")
def create_tournament(
    request: Request,
    name: Annotated[str, Form()],
    mode: Annotated[str, Form()],
    generation: Annotated[str, Form()],
    max_entries: Annotated[int, Form()],
    group_size: Annotated[int, Form()],
    registration_fee: Annotated[float, Form()],
    prize: Annotated[float, Form()],
    starts_at: Annotated[str, Form()],
    rules: Annotated[str, Form()],
    db: Session = Depends(get_db),
):
    require_admin(request)
    season = db.scalar(select(Season).where(Season.active == True).limit(1))
    tournament = Tournament(
        season_id=season.id if season else None,
        name=name.strip(),
        slug=unique_slug(db, name),
        mode=mode,
        generation=generation,
        max_entries=max_entries,
        group_size=group_size,
        registration_fee=registration_fee,
        prize=prize,
        status="open",
        starts_at=starts_at.strip() or "A definir",
        rules=rules.strip(),
    )
    db.add(tournament)
    db.commit()
    db.refresh(tournament)
    audit(db, "create", "tournament", tournament.id, tournament.name)
    return RedirectResponse("/admin?message=Torneio criado", 303)

@app.get("/health")
def health():
    return {"status": "ok", "version": "5.0.0-base"}
