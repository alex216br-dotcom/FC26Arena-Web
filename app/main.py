import os
import shutil
from datetime import datetime, timedelta
from pathlib import Path
from typing import Annotated
from urllib.parse import quote

from fastapi import Depends, FastAPI, File, Form, Header, HTTPException, Request, UploadFile
from fastapi.responses import FileResponse, HTMLResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from sqlalchemy import func, or_, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session
from starlette.middleware.sessions import SessionMiddleware

from .database import Base, engine, get_db
from .integrations import notify_user, send_email
from .models import (
    Achievement, AdminUser, AuditLog, Coupon, Evidence, Match, MatchMessage,
    Notification, PasswordReset, Payment, Registration, Report, Season,
    SupportTicket, Team, TeamMember, Tournament, User, UserAchievement,
)
from .security import hash_password, token_urlsafe, verify_password
from .services import (
    audit, entry_name, generate_groups, process_confirmed_result,
    seed_achievements, sorted_group, unique_slug,
)

BASE = Path(__file__).resolve().parent
ROOT = BASE.parent
UPLOAD_DIR = Path(os.getenv("UPLOAD_DIR", str(ROOT / "uploads")))
UPLOAD_DIR.mkdir(parents=True, exist_ok=True)
MAX_UPLOAD_MB = int(os.getenv("MAX_UPLOAD_MB", "8"))
SITE_URL = os.getenv("SITE_URL", "http://127.0.0.1:8000").rstrip("/")
ADMIN_PASSWORD = os.getenv("ADMIN_PASSWORD", "admin123")
PAYMENT_WEBHOOK_SECRET = os.getenv("PAYMENT_WEBHOOK_SECRET", "")
PIX_KEY = os.getenv("PIX_KEY", "")
ENVIRONMENT = os.getenv("ENVIRONMENT", "development")

PLATFORMS = {
    "nova": ["PlayStation 5", "Xbox Series X|S", "PC"],
    "antiga": ["PlayStation 4", "Xbox One"],
}

app = FastAPI(title="FC26 Arena V6 Pro")
app.add_middleware(
    SessionMiddleware,
    secret_key=os.getenv("SECRET_KEY", "troque-esta-chave"),
    same_site="lax",
    https_only=ENVIRONMENT == "production",
)
app.mount("/static", StaticFiles(directory=BASE / "static"), name="static")
templates = Jinja2Templates(directory=BASE / "templates")

@app.on_event("startup")
def startup():
    Base.metadata.create_all(bind=engine)
    db = next(get_db())
    try:
        if not db.scalar(select(Season).limit(1)):
            db.add(Season(name="Temporada 1", active=True))
            db.commit()
        seed_achievements(db)
        if not db.scalar(select(Tournament).limit(1)):
            season = db.scalar(select(Season).where(Season.active == True).limit(1))
            demos = [
                Tournament(season_id=season.id, name="Copa FC26 Elite", slug="copa-fc26-elite", mode="1x1", generation="nova", max_entries=32, prize=50, status="open", color_theme="green", rules="Fase de grupos e mata-mata."),
                Tournament(season_id=season.id, name="Duplas Champions", slug="duplas-champions", mode="2x2", generation="nova", max_entries=16, prize=100, status="open", color_theme="purple", rules="Equipes com 2 jogadores."),
                Tournament(season_id=season.id, name="Pro Clubs League", slug="pro-clubs-league", mode="Pro Clubs", generation="nova", max_entries=8, prize=200, status="open", color_theme="cyan", rules="Campeonato para clubes."),
                Tournament(season_id=season.id, name="Legends Cup", slug="legends-cup", mode="1x1", generation="antiga", max_entries=32, prize=50, status="open", color_theme="orange", rules="PS4 e Xbox One."),
            ]
            db.add_all(demos)
            db.commit()
    finally:
        db.close()

def ctx(request: Request, **kwargs):
    return {
        "request": request,
        "platforms": PLATFORMS,
        "pix_key": PIX_KEY,
        **kwargs,
    }

def current_user(request: Request, db: Session) -> User | None:
    user_id = request.session.get("user_id")
    return db.get(User, user_id) if user_id else None

def require_user(request: Request, db: Session) -> User:
    user = current_user(request, db)
    if not user:
        raise HTTPException(403)
    return user

def require_admin(request: Request):
    if not request.session.get("admin"):
        raise HTTPException(403)


def admin_redirect(message: str, location: str = "/admin") -> RedirectResponse:
    separator = "&" if "?" in location else "?"
    return RedirectResponse(f"{location}{separator}message={quote(str(message))}", 303)

def registration_label(registration: Registration) -> str:
    if registration.team:
        return registration.team.name
    if registration.user:
        return registration.user.name
    return f"Inscrição #{registration.id}"

def tournament_registration_counts(db: Session, tournament_id: int) -> dict[str, int]:
    registrations = db.scalars(
        select(Registration).where(Registration.tournament_id == tournament_id)
    ).all()
    return {
        "total": len(registrations),
        "approved": sum(1 for item in registrations if item.status == "approved"),
        "pending": sum(1 for item in registrations if item.status == "pending"),
        "rejected": sum(1 for item in registrations if item.status in {"rejected", "cancelled"}),
    }

def registration_contains_user(db: Session, registration: Registration, user: User) -> bool:
    if registration.user_id == user.id:
        return True
    if registration.team_id:
        return bool(db.scalar(select(TeamMember).where(
            TeamMember.team_id == registration.team_id,
            TeamMember.user_id == user.id,
        )))
    return False

@app.get("/", response_class=HTMLResponse)
def home(request: Request, db: Session = Depends(get_db)):
    tournaments = db.scalars(
        select(Tournament).where(Tournament.status != "archived").order_by(Tournament.id)
    ).all()
    users_count = db.scalar(select(func.count()).select_from(User))
    championships_count = db.scalar(select(func.count()).select_from(Tournament))
    matches_count = db.scalar(select(func.count()).select_from(Match).where(Match.status == "completed"))
    prize_total = sum(float(t.prize) for t in tournaments if t.status == "completed")
    ranking = db.scalars(select(User).where(User.active == True).order_by(User.xp.desc()).limit(5)).all()
    return templates.TemplateResponse("home.html", ctx(
        request,
        tournaments=tournaments,
        users_count=users_count,
        championships_count=championships_count,
        matches_count=matches_count,
        prize_total=prize_total,
        ranking=ranking,
        open_register=request.query_params.get("cadastro") == "1",
    ))


@app.get("/como-funciona", response_class=HTMLResponse)
def how_it_works(request: Request):
    return templates.TemplateResponse("how_it_works.html", ctx(request))

@app.get("/regulamento", response_class=HTMLResponse)
def public_rules(request: Request):
    return templates.TemplateResponse("rules.html", ctx(request))

@app.post("/cadastro")
def register(
    request: Request,
    name: Annotated[str, Form()],
    whatsapp: Annotated[str, Form()],
    email: Annotated[str | None, Form()] = None,
    ea_id: Annotated[str, Form()] = "",
    generation: Annotated[str, Form()] = "nova",
    platform: Annotated[str, Form()] = "",
    password: Annotated[str, Form()] = "",
    password_confirm: Annotated[str, Form()] = "",
    terms: Annotated[str | None, Form()] = None,
    db: Session = Depends(get_db),
):
    errors = []
    if generation not in PLATFORMS:
        errors.append("Geração inválida.")
    elif platform not in PLATFORMS[generation]:
        errors.append("Plataforma incompatível com a geração.")
    if len(name.strip()) < 2:
        errors.append("Informe um nome válido.")
    if len(whatsapp.strip()) < 10:
        errors.append("Informe o WhatsApp com DDD.")
    if len(ea_id.strip()) < 3:
        errors.append("Informe o ID EA.")
    if len(password) < 6:
        errors.append("A senha deve ter ao menos 6 caracteres.")
    if password != password_confirm:
        errors.append("As senhas não coincidem.")
    if not terms:
        errors.append("Aceite os termos para continuar.")
    if errors:
        tournaments = db.scalars(select(Tournament).where(Tournament.status != "archived")).all()
        return templates.TemplateResponse("home.html", ctx(
            request, tournaments=tournaments, users_count=0, championships_count=len(tournaments),
            matches_count=0, prize_total=0, ranking=[], open_register=True, register_errors=errors,
        ), status_code=400)

    user = User(
        name=name.strip(),
        whatsapp=whatsapp.strip(),
        email=email.strip().lower() if email else None,
        ea_id=ea_id.strip(),
        generation=generation,
        platform=platform,
        password_hash=hash_password(password),
    )
    db.add(user)
    try:
        db.commit()
        db.refresh(user)
    except IntegrityError:
        db.rollback()
        tournaments = db.scalars(select(Tournament).where(Tournament.status != "archived")).all()
        return templates.TemplateResponse("home.html", ctx(
            request, tournaments=tournaments, users_count=0, championships_count=len(tournaments),
            matches_count=0, prize_total=0, ranking=[], open_register=True,
            register_errors=["WhatsApp, e-mail ou ID EA já cadastrado."],
        ), status_code=400)

    achievement = db.scalar(select(Achievement).where(Achievement.code == "WELCOME"))
    if achievement:
        db.add(UserAchievement(user_id=user.id, achievement_id=achievement.id))
        user.xp += achievement.xp_reward
        user.level = max(1, user.xp // 500 + 1)
        db.commit()
    request.session["user_id"] = user.id
    notify_user(db, user, "Bem-vindo ao FC26 Arena", "Sua conta foi criada com sucesso.")
    return RedirectResponse("/painel?novo=1", 303)

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
        User.email == identifier.strip().lower(),
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

@app.get("/esqueci-senha", response_class=HTMLResponse)
def forgot_page(request: Request):
    return templates.TemplateResponse("forgot.html", ctx(request, message=None))

@app.post("/esqueci-senha", response_class=HTMLResponse)
def forgot_password(request: Request, identifier: Annotated[str, Form()], db: Session = Depends(get_db)):
    user = db.scalar(select(User).where(or_(User.email == identifier.strip().lower(), User.whatsapp == identifier.strip())))
    message = "Caso a conta exista, as instruções foram geradas."
    reset_link = None
    if user:
        token = token_urlsafe()
        db.add(PasswordReset(user_id=user.id, token=token, expires_at=datetime.utcnow()+timedelta(hours=1)))
        db.commit()
        reset_link = f"{SITE_URL}/redefinir-senha/{token}"
        if user.email:
            send_email(user.email, "Redefinição de senha FC26 Arena", f"Acesse: {reset_link}")
    return templates.TemplateResponse("forgot.html", ctx(
        request, message=message, reset_link=reset_link if ENVIRONMENT != "production" else None
    ))

@app.get("/redefinir-senha/{token}", response_class=HTMLResponse)
def reset_page(token: str, request: Request, db: Session = Depends(get_db)):
    reset = db.scalar(select(PasswordReset).where(PasswordReset.token == token, PasswordReset.used == False))
    if not reset or reset.expires_at < datetime.utcnow():
        raise HTTPException(404)
    return templates.TemplateResponse("reset.html", ctx(request, token=token, error=None))

@app.post("/redefinir-senha/{token}")
def reset_password(token: str, request: Request, password: Annotated[str, Form()], password_confirm: Annotated[str, Form()], db: Session = Depends(get_db)):
    reset = db.scalar(select(PasswordReset).where(PasswordReset.token == token, PasswordReset.used == False))
    if not reset or reset.expires_at < datetime.utcnow():
        raise HTTPException(404)
    if len(password) < 6 or password != password_confirm:
        return templates.TemplateResponse("reset.html", ctx(request, token=token, error="Verifique as senhas."), status_code=400)
    user = db.get(User, reset.user_id)
    user.password_hash = hash_password(password)
    reset.used = True
    db.commit()
    return RedirectResponse("/login", 303)

@app.get("/painel", response_class=HTMLResponse)
def dashboard(request: Request, db: Session = Depends(get_db)):
    user = require_user(request, db)
    registrations = db.scalars(select(Registration).where(Registration.user_id == user.id).order_by(Registration.id.desc())).all()
    team_memberships = db.scalars(select(TeamMember).where(TeamMember.user_id == user.id)).all()
    team_ids = [m.team_id for m in team_memberships]
    if team_ids:
        registrations += db.scalars(select(Registration).where(Registration.team_id.in_(team_ids))).all()
    matches = db.scalars(select(Match).order_by(Match.id.desc())).all()
    user_matches = [
        m for m in matches
        if registration_contains_user(db, m.home, user) or registration_contains_user(db, m.away, user)
    ]
    achievements = db.scalars(select(UserAchievement).where(UserAchievement.user_id == user.id)).all()
    notifications = db.scalars(select(Notification).where(Notification.user_id == user.id).order_by(Notification.id.desc()).limit(5)).all()
    return templates.TemplateResponse("dashboard.html", ctx(
        request, user=user, registrations=registrations, matches=user_matches,
        achievements=achievements, notifications=notifications, new=request.query_params.get("novo")
    ))

@app.get("/campeonatos", response_class=HTMLResponse)
def tournaments_page(request: Request, db: Session = Depends(get_db)):
    tournaments = db.scalars(select(Tournament).where(Tournament.status != "archived").order_by(Tournament.id.desc())).all()
    return templates.TemplateResponse("tournaments.html", ctx(request, tournaments=tournaments))

@app.get("/campeonato/{slug}", response_class=HTMLResponse)
def tournament_page(slug: str, request: Request, db: Session = Depends(get_db)):
    tournament = db.scalar(select(Tournament).where(Tournament.slug == slug))
    if not tournament:
        raise HTTPException(404)
    registrations = db.scalars(select(Registration).where(Registration.tournament_id == tournament.id)).all()
    user = current_user(request, db)
    teams = []
    if user:
        memberships = db.scalars(select(TeamMember).where(TeamMember.user_id == user.id)).all()
        teams = [m.team for m in memberships if m.team and m.team.mode == tournament.mode]
    groups = {}
    for reg in registrations:
        if reg.group_name:
            groups.setdefault(reg.group_name, []).append(reg)
    for name in groups:
        groups[name] = sorted_group(groups[name])
    matches = db.scalars(select(Match).where(Match.tournament_id == tournament.id).order_by(Match.round_order, Match.id)).all()
    return templates.TemplateResponse("tournament.html", ctx(
        request, tournament=tournament, registrations=registrations, user=user, teams=teams,
        groups=groups, matches=matches,
    ))

@app.post("/campeonato/{slug}/inscrever")
def tournament_register(
    slug: str,
    request: Request,
    team_id: Annotated[int | None, Form()] = None,
    coupon_code: Annotated[str | None, Form()] = None,
    db: Session = Depends(get_db),
):
    user = require_user(request, db)
    tournament = db.scalar(select(Tournament).where(Tournament.slug == slug))
    if not tournament or tournament.status != "open":
        raise HTTPException(400)
    total = db.scalar(select(func.count()).select_from(Registration).where(
        Registration.tournament_id == tournament.id,
        Registration.status.in_(["pending", "approved"]),
    ))
    if total >= tournament.max_entries:
        raise HTTPException(400, "Vagas preenchidas.")

    if tournament.mode == "1x1":
        existing = db.scalar(select(Registration).where(Registration.tournament_id == tournament.id, Registration.user_id == user.id))
        if existing:
            return RedirectResponse(f"/campeonato/{slug}", 303)
        registration = Registration(tournament_id=tournament.id, user_id=user.id)
    else:
        if not team_id:
            raise HTTPException(400, "Escolha uma equipe.")
        team = db.get(Team, team_id)
        membership = db.scalar(select(TeamMember).where(TeamMember.team_id == team_id, TeamMember.user_id == user.id))
        if not team or not membership or team.mode != tournament.mode:
            raise HTTPException(403)
        existing = db.scalar(select(Registration).where(Registration.tournament_id == tournament.id, Registration.team_id == team.id))
        if existing:
            return RedirectResponse(f"/campeonato/{slug}", 303)
        registration = Registration(tournament_id=tournament.id, team_id=team.id)

    amount = float(tournament.registration_fee)
    applied_coupon = None
    if coupon_code:
        coupon = db.scalar(select(Coupon).where(Coupon.code == coupon_code.strip().upper(), Coupon.active == True))
        if coupon and (coupon.max_uses == 0 or coupon.uses < coupon.max_uses):
            amount = max(0, amount * (100-coupon.discount_percent)/100)
            coupon.uses += 1
            applied_coupon = coupon.code

    if amount == 0:
        registration.status = "approved"
        registration.payment_status = "not_required"
    else:
        registration.status = "pending"
        registration.payment_status = "pending"
    db.add(registration)
    db.commit()
    db.refresh(registration)

    if amount > 0:
        payment = Payment(registration_id=registration.id, amount=amount, coupon_code=applied_coupon)
        db.add(payment)
        db.commit()
        notify_user(db, user, "Pagamento pendente", f"Sua inscrição em {tournament.name} aguarda o pagamento de R$ {amount:.2f}.")
        return RedirectResponse(f"/pagamento/{payment.id}", 303)

    notify_user(db, user, "Inscrição confirmada", f"Você está inscrito em {tournament.name}.")
    return RedirectResponse(f"/campeonato/{slug}", 303)

@app.get("/pagamento/{payment_id}", response_class=HTMLResponse)
def payment_page(payment_id: int, request: Request, db: Session = Depends(get_db)):
    user = require_user(request, db)
    payment = db.get(Payment, payment_id)
    if not payment or not registration_contains_user(db, payment.registration, user):
        raise HTTPException(404)
    return templates.TemplateResponse("payment.html", ctx(request, payment=payment))

@app.post("/webhooks/pagamento")
def payment_webhook(
    external_id: Annotated[str, Form()],
    status: Annotated[str, Form()],
    payment_id: Annotated[int, Form()],
    x_webhook_secret: str | None = Header(default=None),
    db: Session = Depends(get_db),
):
    if not PAYMENT_WEBHOOK_SECRET or x_webhook_secret != PAYMENT_WEBHOOK_SECRET:
        raise HTTPException(403)
    payment = db.get(Payment, payment_id)
    if not payment:
        raise HTTPException(404)
    payment.external_id = external_id
    payment.status = status
    if status == "approved":
        payment.registration.payment_status = "approved"
        payment.registration.status = "approved"
    db.commit()
    return {"ok": True}

@app.get("/times", response_class=HTMLResponse)
def teams_page(request: Request, db: Session = Depends(get_db)):
    teams = db.scalars(select(Team).where(Team.active == True).order_by(Team.id.desc())).all()
    return templates.TemplateResponse("teams.html", ctx(request, teams=teams, user=current_user(request, db)))

@app.post("/times/criar")
def team_create(
    request: Request,
    name: Annotated[str, Form()],
    mode: Annotated[str, Form()],
    db: Session = Depends(get_db),
):
    user = require_user(request, db)
    if mode not in {"2x2", "Pro Clubs"}:
        raise HTTPException(400)
    team = Team(name=name.strip(), mode=mode, captain_id=user.id, invite_code=token_urlsafe(8))
    db.add(team)
    try:
        db.commit()
        db.refresh(team)
    except IntegrityError:
        db.rollback()
        raise HTTPException(400, "Nome já utilizado.")
    db.add(TeamMember(team_id=team.id, user_id=user.id, role="captain"))
    db.commit()
    return RedirectResponse("/times", 303)

@app.post("/times/entrar")
def team_join(request: Request, invite_code: Annotated[str, Form()], db: Session = Depends(get_db)):
    user = require_user(request, db)
    team = db.scalar(select(Team).where(Team.invite_code == invite_code.strip()))
    if not team:
        raise HTTPException(404)
    if not db.scalar(select(TeamMember).where(TeamMember.team_id == team.id, TeamMember.user_id == user.id)):
        db.add(TeamMember(team_id=team.id, user_id=user.id))
        db.commit()
    return RedirectResponse("/times", 303)

@app.get("/ranking", response_class=HTMLResponse)
def ranking_page(request: Request, generation: str = "todas", db: Session = Depends(get_db)):
    query = select(User).where(User.active == True)
    if generation in PLATFORMS:
        query = query.where(User.generation == generation)
    users = db.scalars(query.order_by(User.xp.desc(), User.wins.desc())).all()
    return templates.TemplateResponse("ranking.html", ctx(request, users=users, generation=generation))

@app.get("/conquistas", response_class=HTMLResponse)
def achievements_page(request: Request, db: Session = Depends(get_db)):
    achievements = db.scalars(select(Achievement).order_by(Achievement.id)).all()
    user = current_user(request, db)
    owned = set()
    if user:
        owned = {ua.achievement_id for ua in db.scalars(select(UserAchievement).where(UserAchievement.user_id == user.id)).all()}
    return templates.TemplateResponse("achievements.html", ctx(request, achievements=achievements, owned=owned))

@app.get("/notificacoes", response_class=HTMLResponse)
def notifications_page(request: Request, db: Session = Depends(get_db)):
    user = require_user(request, db)
    notifications = db.scalars(select(Notification).where(Notification.user_id == user.id).order_by(Notification.id.desc())).all()
    for note in notifications:
        note.read = True
    db.commit()
    return templates.TemplateResponse("notifications.html", ctx(request, notifications=notifications))

@app.get("/partida/{match_id}", response_class=HTMLResponse)
def match_room(match_id: int, request: Request, db: Session = Depends(get_db)):
    user = require_user(request, db)
    match = db.get(Match, match_id)
    if not match or not (registration_contains_user(db, match.home, user) or registration_contains_user(db, match.away, user)):
        raise HTTPException(404)
    messages = db.scalars(select(MatchMessage).where(MatchMessage.match_id == match.id).order_by(MatchMessage.created_at)).all()
    evidences = db.scalars(select(Evidence).where(Evidence.match_id == match.id).order_by(Evidence.id.desc())).all()
    return templates.TemplateResponse("match_room.html", ctx(request, user=user, match=match, messages=messages, evidences=evidences, entry_name=entry_name))

@app.post("/partida/{match_id}/mensagem")
def match_message(match_id: int, request: Request, message: Annotated[str, Form()], db: Session = Depends(get_db)):
    user = require_user(request, db)
    match = db.get(Match, match_id)
    if not match or not (registration_contains_user(db, match.home, user) or registration_contains_user(db, match.away, user)):
        raise HTTPException(404)
    if message.strip():
        db.add(MatchMessage(match_id=match.id, user_id=user.id, message=message.strip()[:1000]))
        db.commit()
    return RedirectResponse(f"/partida/{match.id}", 303)

@app.post("/partida/{match_id}/resultado")
def match_result(match_id: int, request: Request, home_score: Annotated[int, Form()], away_score: Annotated[int, Form()], db: Session = Depends(get_db)):
    user = require_user(request, db)
    match = db.get(Match, match_id)
    if not match or not (registration_contains_user(db, match.home, user) or registration_contains_user(db, match.away, user)):
        raise HTTPException(404)
    if match.phase == "knockout" and home_score == away_score:
        raise HTTPException(400, "Mata-mata não pode terminar empatado.")
    match.home_score = max(0, home_score)
    match.away_score = max(0, away_score)
    match.result_submitted_by_user_id = user.id
    match.status = "awaiting_confirmation"
    match.result_confirmed = False
    db.commit()
    return RedirectResponse(f"/partida/{match.id}", 303)

@app.post("/partida/{match_id}/confirmar")
def confirm_result(match_id: int, request: Request, action: Annotated[str, Form()], db: Session = Depends(get_db)):
    user = require_user(request, db)
    match = db.get(Match, match_id)
    if not match or not (registration_contains_user(db, match.home, user) or registration_contains_user(db, match.away, user)):
        raise HTTPException(404)
    if match.result_submitted_by_user_id == user.id:
        return RedirectResponse(f"/partida/{match.id}", 303)
    if action == "confirm":
        match.status = "completed"
        match.result_confirmed = True
        db.commit()
        process_confirmed_result(db, match)
    else:
        match.status = "disputed"
        db.add(Report(match_id=match.id, reporter_id=user.id, report_type="resultado", description="Resultado contestado."))
        db.commit()
    return RedirectResponse(f"/partida/{match.id}", 303)

@app.post("/partida/{match_id}/prova")
async def upload_evidence(
    match_id: int,
    request: Request,
    description: Annotated[str, Form()],
    file: UploadFile = File(...),
    db: Session = Depends(get_db),
):
    user = require_user(request, db)
    match = db.get(Match, match_id)
    if not match or not (registration_contains_user(db, match.home, user) or registration_contains_user(db, match.away, user)):
        raise HTTPException(404)
    allowed = {".png", ".jpg", ".jpeg", ".webp", ".pdf"}
    suffix = Path(file.filename or "").suffix.lower()
    if suffix not in allowed:
        raise HTTPException(400, "Formato não permitido.")
    data = await file.read()
    if len(data) > MAX_UPLOAD_MB * 1024 * 1024:
        raise HTTPException(400, "Arquivo muito grande.")
    filename = f"match_{match.id}_{token_urlsafe(8)}{suffix}"
    destination = UPLOAD_DIR / filename
    destination.write_bytes(data)
    db.add(Evidence(match_id=match.id, uploaded_by=user.id, file_path=filename, original_name=file.filename or filename, description=description.strip()))
    db.commit()
    return RedirectResponse(f"/partida/{match.id}", 303)

@app.get("/provas/{filename}")
def evidence_file(filename: str, request: Request):
    safe_name = Path(filename).name
    path = UPLOAD_DIR / safe_name
    if not path.exists():
        raise HTTPException(404)
    return FileResponse(path)

@app.post("/partida/{match_id}/denunciar")
def report_match(match_id: int, request: Request, report_type: Annotated[str, Form()], description: Annotated[str, Form()], db: Session = Depends(get_db)):
    user = require_user(request, db)
    match = db.get(Match, match_id)
    if not match or not (registration_contains_user(db, match.home, user) or registration_contains_user(db, match.away, user)):
        raise HTTPException(404)
    db.add(Report(match_id=match.id, reporter_id=user.id, report_type=report_type, description=description.strip()))
    db.commit()
    return RedirectResponse(f"/partida/{match.id}", 303)

@app.get("/suporte", response_class=HTMLResponse)
def support_page(request: Request):
    return templates.TemplateResponse("support.html", ctx(request, message=None))

@app.post("/suporte", response_class=HTMLResponse)
def support_send(request: Request, subject: Annotated[str, Form()], message: Annotated[str, Form()], db: Session = Depends(get_db)):
    user = current_user(request, db)
    db.add(SupportTicket(user_id=user.id if user else None, subject=subject.strip(), message=message.strip()))
    db.commit()
    return templates.TemplateResponse("support.html", ctx(request, message="Mensagem enviada para a organização."))

@app.get("/admin/login", response_class=HTMLResponse)
def admin_login_page(request: Request):
    return templates.TemplateResponse("admin_login.html", ctx(request, error=None))

@app.post("/admin/login")
def admin_login(request: Request, password: Annotated[str, Form()]):
    if password != ADMIN_PASSWORD:
        return templates.TemplateResponse("admin_login.html", ctx(request, error="Senha incorreta."), status_code=400)
    request.session["admin"] = True
    return RedirectResponse("/admin", 303)

@app.get("/admin/sair")
def admin_logout(request: Request):
    request.session.pop("admin", None)
    return RedirectResponse("/", 303)


@app.get("/admin", response_class=HTMLResponse)
def admin_dashboard(request: Request, db: Session = Depends(get_db)):
    require_admin(request)
    tournaments = db.scalars(select(Tournament).order_by(Tournament.id.desc())).all()
    users = db.scalars(select(User).order_by(User.id.desc())).all()
    registrations = db.scalars(select(Registration).order_by(Registration.id.desc())).all()
    payments = db.scalars(select(Payment).order_by(Payment.id.desc()).limit(20)).all()
    reports = db.scalars(select(Report).order_by(Report.id.desc()).limit(20)).all()
    tickets = db.scalars(select(SupportTicket).order_by(SupportTicket.id.desc()).limit(20)).all()
    coupons = db.scalars(select(Coupon).order_by(Coupon.id.desc())).all()
    audits = db.scalars(select(AuditLog).order_by(AuditLog.id.desc()).limit(20)).all()

    tournament_rows = []
    for tournament in tournaments:
        counts = tournament_registration_counts(db, tournament.id)
        match_count = db.scalar(
            select(func.count()).select_from(Match).where(Match.tournament_id == tournament.id)
        )
        tournament_rows.append({
            "tournament": tournament,
            "counts": counts,
            "matches": match_count,
            "can_draw": counts["approved"] >= max(2, tournament.group_size),
        })

    stats = {
        "users": len(users),
        "active_users": sum(1 for user in users if user.active),
        "tournaments": len(tournaments),
        "open_tournaments": sum(1 for item in tournaments if item.status == "open"),
        "registrations": len(registrations),
        "pending_registrations": sum(1 for item in registrations if item.status == "pending"),
        "pending_payments": sum(1 for item in payments if item.status == "pending"),
        "open_reports": sum(1 for item in reports if item.status == "open"),
    }

    return templates.TemplateResponse("admin.html", ctx(
        request,
        tournament_rows=tournament_rows,
        payments=payments,
        reports=reports,
        tickets=tickets,
        coupons=coupons,
        audits=audits,
        stats=stats,
        message=request.query_params.get("message"),
    ))

@app.get("/admin/jogadores", response_class=HTMLResponse)
def admin_players(
    request: Request,
    q: str = "",
    generation: str = "todas",
    db: Session = Depends(get_db),
):
    require_admin(request)
    query = select(User)
    if generation in PLATFORMS:
        query = query.where(User.generation == generation)
    users = db.scalars(query.order_by(User.id.desc())).all()
    term = q.strip().lower()
    if term:
        users = [
            user for user in users
            if term in user.name.lower()
            or term in user.ea_id.lower()
            or term in user.whatsapp.lower()
            or (user.email and term in user.email.lower())
        ]
    registration_totals = {
        user.id: db.scalar(
            select(func.count()).select_from(Registration).where(Registration.user_id == user.id)
        )
        for user in users
    }
    return templates.TemplateResponse("admin_players.html", ctx(
        request,
        users=users,
        registration_totals=registration_totals,
        q=q,
        generation=generation,
        message=request.query_params.get("message"),
    ))

@app.post("/admin/jogador/{user_id}/editar")
def admin_edit_player(
    user_id: int,
    request: Request,
    name: Annotated[str, Form()],
    whatsapp: Annotated[str, Form()],
    email: Annotated[str | None, Form()] = None,
    ea_id: Annotated[str, Form()] = "",
    generation: Annotated[str, Form()] = "nova",
    platform: Annotated[str, Form()] = "",
    active: Annotated[str | None, Form()] = None,
    db: Session = Depends(get_db),
):
    require_admin(request)
    user = db.get(User, user_id)
    if not user:
        raise HTTPException(404)
    if generation not in PLATFORMS or platform not in PLATFORMS[generation]:
        return admin_redirect("Geração ou plataforma inválida.", "/admin/jogadores")
    user.name = name.strip()
    user.whatsapp = whatsapp.strip()
    user.email = email.strip().lower() if email else None
    user.ea_id = ea_id.strip()
    user.generation = generation
    user.platform = platform
    user.active = active == "1"
    try:
        db.commit()
    except IntegrityError:
        db.rollback()
        return admin_redirect("WhatsApp, e-mail ou ID EA já pertence a outro jogador.", "/admin/jogadores")
    audit(db, "edit", "user", user.id, user.name)
    return admin_redirect("Jogador atualizado com sucesso.", "/admin/jogadores")

@app.post("/admin/jogador/{user_id}/status")
def admin_player_status(
    user_id: int,
    request: Request,
    action: Annotated[str, Form()],
    db: Session = Depends(get_db),
):
    require_admin(request)
    user = db.get(User, user_id)
    if not user:
        raise HTTPException(404)
    user.active = action == "activate"
    db.commit()
    audit(db, "status", "user", user.id, action)
    return admin_redirect("Status do jogador atualizado.", "/admin/jogadores")

@app.get("/admin/campeonato/{tournament_id}", response_class=HTMLResponse)
def admin_tournament_manage(
    tournament_id: int,
    request: Request,
    db: Session = Depends(get_db),
):
    require_admin(request)
    tournament = db.get(Tournament, tournament_id)
    if not tournament:
        raise HTTPException(404)

    registrations = db.scalars(
        select(Registration)
        .where(Registration.tournament_id == tournament.id)
        .order_by(Registration.id)
    ).all()
    matches = db.scalars(
        select(Match)
        .where(Match.tournament_id == tournament.id)
        .order_by(Match.round_order, Match.group_name, Match.id)
    ).all()
    users = db.scalars(
        select(User)
        .where(User.active == True, User.generation == tournament.generation)
        .order_by(User.name)
    ).all()
    registered_user_ids = {item.user_id for item in registrations if item.user_id}
    available_users = [user for user in users if user.id not in registered_user_ids]
    counts = tournament_registration_counts(db, tournament.id)

    return templates.TemplateResponse("admin_tournament.html", ctx(
        request,
        tournament=tournament,
        registrations=registrations,
        matches=matches,
        available_users=available_users,
        counts=counts,
        can_draw=counts["approved"] >= max(2, tournament.group_size),
        registration_label=registration_label,
        message=request.query_params.get("message"),
    ))

@app.post("/admin/campeonato/{tournament_id}/editar")
def admin_edit_tournament(
    tournament_id: int,
    request: Request,
    name: Annotated[str, Form()],
    mode: Annotated[str, Form()],
    generation: Annotated[str, Form()],
    max_entries: Annotated[int, Form()],
    group_size: Annotated[int, Form()],
    registration_fee: Annotated[float, Form()],
    prize: Annotated[float, Form()],
    starts_at: Annotated[str, Form()],
    color_theme: Annotated[str, Form()],
    rules: Annotated[str, Form()],
    status: Annotated[str, Form()],
    db: Session = Depends(get_db),
):
    require_admin(request)
    tournament = db.get(Tournament, tournament_id)
    if not tournament:
        raise HTTPException(404)
    if mode not in {"1x1", "2x2", "Pro Clubs"}:
        return admin_redirect("Modalidade inválida.", f"/admin/campeonato/{tournament_id}")
    if generation not in PLATFORMS:
        return admin_redirect("Geração inválida.", f"/admin/campeonato/{tournament_id}")
    if group_size < 2:
        return admin_redirect("O grupo precisa ter pelo menos 2 participantes.", f"/admin/campeonato/{tournament_id}")
    if max_entries < group_size:
        return admin_redirect("O número de vagas não pode ser menor que o tamanho do grupo.", f"/admin/campeonato/{tournament_id}")

    tournament.name = name.strip()
    tournament.mode = mode
    tournament.generation = generation
    tournament.max_entries = max_entries
    tournament.group_size = group_size
    tournament.registration_fee = max(0, registration_fee)
    tournament.prize = max(0, prize)
    tournament.starts_at = starts_at.strip() or "A definir"
    tournament.color_theme = color_theme
    tournament.rules = rules.strip()
    tournament.status = status
    db.commit()
    audit(db, "edit", "tournament", tournament.id, tournament.name)
    return admin_redirect("Campeonato atualizado com sucesso.", f"/admin/campeonato/{tournament_id}")

@app.post("/admin/campeonato/{tournament_id}/adicionar-jogador")
def admin_add_player_to_tournament(
    tournament_id: int,
    request: Request,
    user_id: Annotated[int, Form()],
    status: Annotated[str, Form()] = "approved",
    db: Session = Depends(get_db),
):
    require_admin(request)
    tournament = db.get(Tournament, tournament_id)
    user = db.get(User, user_id)
    if not tournament or not user:
        raise HTTPException(404)
    if tournament.mode != "1x1":
        return admin_redirect("Inclusão individual pelo painel está disponível apenas para campeonatos 1x1.", f"/admin/campeonato/{tournament_id}")
    if user.generation != tournament.generation:
        return admin_redirect("O jogador pertence a outra geração.", f"/admin/campeonato/{tournament_id}")
    existing = db.scalar(select(Registration).where(
        Registration.tournament_id == tournament.id,
        Registration.user_id == user.id,
    ))
    if existing:
        return admin_redirect("Esse jogador já está inscrito.", f"/admin/campeonato/{tournament_id}")
    total = db.scalar(
        select(func.count()).select_from(Registration).where(
            Registration.tournament_id == tournament.id,
            Registration.status.in_(["approved", "pending"]),
        )
    )
    if total >= tournament.max_entries:
        return admin_redirect("As vagas do campeonato estão preenchidas.", f"/admin/campeonato/{tournament_id}")
    payment_status = "not_required" if float(tournament.registration_fee) == 0 else "approved"
    db.add(Registration(
        tournament_id=tournament.id,
        user_id=user.id,
        status=status,
        payment_status=payment_status,
    ))
    db.commit()
    audit(db, "add_registration", "tournament", tournament.id, user.name)
    return admin_redirect("Jogador adicionado ao campeonato.", f"/admin/campeonato/{tournament_id}")

@app.post("/admin/campeonato/{tournament_id}/inscricao/{registration_id}/status")
def admin_registration_status(
    tournament_id: int,
    registration_id: int,
    request: Request,
    action: Annotated[str, Form()],
    db: Session = Depends(get_db),
):
    require_admin(request)
    registration = db.get(Registration, registration_id)
    if not registration or registration.tournament_id != tournament_id:
        raise HTTPException(404)

    if action == "approve":
        registration.status = "approved"
        if float(registration.tournament.registration_fee) == 0:
            registration.payment_status = "not_required"
        elif registration.payment_status != "approved":
            registration.payment_status = "approved"
    elif action == "pending":
        registration.status = "pending"
    elif action == "reject":
        registration.status = "rejected"
        registration.payment_status = "rejected"
    elif action == "cancel":
        registration.status = "cancelled"
        registration.group_name = None
    elif action == "restore":
        registration.status = "approved"
    else:
        return admin_redirect("Ação inválida.", f"/admin/campeonato/{tournament_id}")

    db.commit()
    audit(db, "registration_status", "registration", registration.id, action)
    return admin_redirect("Inscrição atualizada.", f"/admin/campeonato/{tournament_id}")

@app.post("/admin/campeonato/{tournament_id}/inscricao/{registration_id}/remover")
def admin_remove_registration(
    tournament_id: int,
    registration_id: int,
    request: Request,
    db: Session = Depends(get_db),
):
    require_admin(request)
    registration = db.get(Registration, registration_id)
    if not registration or registration.tournament_id != tournament_id:
        raise HTTPException(404)
    linked_match = db.scalar(select(Match).where(or_(
        Match.home_registration_id == registration.id,
        Match.away_registration_id == registration.id,
    )))
    if linked_match:
        return admin_redirect(
            "Não é possível excluir definitivamente uma inscrição que já possui partida. Use Cancelar.",
            f"/admin/campeonato/{tournament_id}",
        )
    label = registration_label(registration)
    db.delete(registration)
    db.commit()
    audit(db, "delete_registration", "tournament", tournament_id, label)
    return admin_redirect("Inscrição removida definitivamente.", f"/admin/campeonato/{tournament_id}")

@app.post("/admin/campeonato/{tournament_id}/limpar-sorteio")
def admin_clear_draw(
    tournament_id: int,
    request: Request,
    db: Session = Depends(get_db),
):
    require_admin(request)
    tournament = db.get(Tournament, tournament_id)
    if not tournament:
        raise HTTPException(404)
    db.query(Match).filter(Match.tournament_id == tournament.id).delete()
    registrations = db.scalars(select(Registration).where(
        Registration.tournament_id == tournament.id
    )).all()
    for registration in registrations:
        registration.group_name = None
        registration.points = registration.played = registration.wins = 0
        registration.draws = registration.losses = 0
        registration.goals_for = registration.goals_against = 0
    tournament.status = "open"
    db.commit()
    audit(db, "clear_draw", "tournament", tournament.id, tournament.name)
    return admin_redirect("Sorteio e partidas apagados. As inscrições foram preservadas.", f"/admin/campeonato/{tournament_id}")

@app.post("/admin/partida/{match_id}/editar")
def admin_edit_match(
    match_id: int,
    request: Request,
    scheduled_for: Annotated[str, Form()],
    home_score: Annotated[str, Form()] = "",
    away_score: Annotated[str, Form()] = "",
    status: Annotated[str, Form()] = "scheduled",
    db: Session = Depends(get_db),
):
    require_admin(request)
    match = db.get(Match, match_id)
    if not match:
        raise HTTPException(404)
    match.scheduled_for = scheduled_for.strip() or "A definir"
    match.status = status
    parsed_home = int(home_score) if home_score.strip() else None
    parsed_away = int(away_score) if away_score.strip() else None
    if parsed_home is not None and parsed_away is not None:
        if match.phase == "knockout" and parsed_home == parsed_away and status == "completed":
            return admin_redirect("Partida de mata-mata não pode terminar empatada.", f"/admin/campeonato/{match.tournament_id}")
        match.home_score = max(0, parsed_home)
        match.away_score = max(0, parsed_away)
        if status == "completed":
            match.result_confirmed = True
    db.commit()
    if status == "completed" and match.home_score is not None and match.away_score is not None:
        process_confirmed_result(db, match)
    audit(db, "edit_match", "match", match.id, status)
    return admin_redirect("Partida atualizada.", f"/admin/campeonato/{match.tournament_id}")

@app.post("/admin/campeonatos")
def admin_create_tournament(
    request: Request,
    name: Annotated[str, Form()],
    mode: Annotated[str, Form()],
    generation: Annotated[str, Form()],
    max_entries: Annotated[int, Form()],
    group_size: Annotated[int, Form()],
    registration_fee: Annotated[float, Form()],
    prize: Annotated[float, Form()],
    starts_at: Annotated[str, Form()],
    color_theme: Annotated[str, Form()],
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
        color_theme=color_theme,
        rules=rules.strip(),
    )
    db.add(tournament)
    db.commit()
    db.refresh(tournament)
    audit(db, "create", "tournament", tournament.id, tournament.name)
    return admin_redirect("Campeonato criado com sucesso.")

@app.post("/admin/campeonato/{tournament_id}/status")
def admin_tournament_status(tournament_id: int, request: Request, status: Annotated[str, Form()], db: Session = Depends(get_db)):
    require_admin(request)
    tournament = db.get(Tournament, tournament_id)
    if not tournament:
        raise HTTPException(404)
    tournament.status = status
    db.commit()
    audit(db, "status", "tournament", tournament.id, status)
    return admin_redirect("Status atualizado.", f"/admin/campeonato/{tournament_id}")


@app.post("/admin/campeonato/{tournament_id}/sortear")
def admin_draw_groups(tournament_id: int, request: Request, db: Session = Depends(get_db)):
    require_admin(request)
    tournament = db.get(Tournament, tournament_id)
    if not tournament:
        raise HTTPException(404)

    approved = db.scalars(select(Registration).where(
        Registration.tournament_id == tournament.id,
        Registration.status == "approved",
    )).all()
    if len(approved) < max(2, tournament.group_size):
        return admin_redirect(
            f"Não foi possível sortear: há {len(approved)} inscrição(ões) aprovada(s), mas o grupo exige {tournament.group_size}.",
            f"/admin/campeonato/{tournament_id}",
        )

    try:
        groups = generate_groups(db, tournament)
        total_matches = db.scalar(
            select(func.count()).select_from(Match).where(Match.tournament_id == tournament.id)
        )
        message = f"Sorteio concluído: {len(groups)} grupo(s) e {total_matches} partida(s) gerada(s)."
    except Exception as exc:
        db.rollback()
        message = f"Falha no sorteio: {exc}"

    audit(db, "draw_groups", "tournament", tournament_id, message)
    return admin_redirect(message, f"/admin/campeonato/{tournament_id}")

@app.post("/admin/pagamento/{payment_id}")
def admin_payment(payment_id: int, request: Request, status: Annotated[str, Form()], db: Session = Depends(get_db)):
    require_admin(request)
    payment = db.get(Payment, payment_id)
    if not payment:
        raise HTTPException(404)
    payment.status = status
    if status == "approved":
        payment.registration.payment_status = "approved"
        payment.registration.status = "approved"
    elif status == "rejected":
        payment.registration.payment_status = "rejected"
        payment.registration.status = "rejected"
    db.commit()
    audit(db, "payment", "payment", payment.id, status)
    return RedirectResponse("/admin?message=Pagamento atualizado", 303)

@app.post("/admin/denuncia/{report_id}")
def admin_report(report_id: int, request: Request, status: Annotated[str, Form()], db: Session = Depends(get_db)):
    require_admin(request)
    report = db.get(Report, report_id)
    if report:
        report.status = status
        db.commit()
        audit(db, "report", "report", report.id, status)
    return RedirectResponse("/admin?message=Denúncia atualizada", 303)

@app.post("/admin/suporte/{ticket_id}")
def admin_ticket(ticket_id: int, request: Request, status: Annotated[str, Form()], db: Session = Depends(get_db)):
    require_admin(request)
    ticket = db.get(SupportTicket, ticket_id)
    if ticket:
        ticket.status = status
        db.commit()
    return RedirectResponse("/admin?message=Chamado atualizado", 303)

@app.post("/admin/cupons")
def admin_coupon(request: Request, code: Annotated[str, Form()], discount_percent: Annotated[int, Form()], max_uses: Annotated[int, Form()], db: Session = Depends(get_db)):
    require_admin(request)
    coupon = Coupon(code=code.strip().upper(), discount_percent=max(0,min(100,discount_percent)), max_uses=max_uses)
    db.add(coupon)
    try:
        db.commit()
    except IntegrityError:
        db.rollback()
    return RedirectResponse("/admin?message=Cupom salvo", 303)

@app.get("/health")
def health():
    return {"status": "ok", "version": "6.0.0"}
