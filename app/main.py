import json
import os
from pathlib import Path
from typing import Annotated

from fastapi import Depends, FastAPI, File, Form, HTTPException, Request, UploadFile
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from sqlalchemy import func, or_, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session
from starlette.middleware.sessions import SessionMiddleware

from .database import Base, engine, get_db
from .models import Match, Player
from .security import hash_password, verify_password
from .services import draw_groups, generate_knockout, get_setting, ranking_query, recalculate

BASE = Path(__file__).resolve().parent
app = FastAPI(title="FC26 Arena")
app.add_middleware(
    SessionMiddleware,
    secret_key=os.getenv("SECRET_KEY", "troque-esta-chave"),
    same_site="lax",
    https_only=False,
)
app.mount("/static", StaticFiles(directory=BASE / "static"), name="static")
templates = Jinja2Templates(directory=BASE / "templates")

MAX_PLAYERS = int(os.getenv("MAX_PLAYERS", "32"))
PRIZE_NEW = os.getenv("CHAMPION_PRIZE_NEW", os.getenv("CHAMPION_PRIZE", "50"))
PRIZE_OLD = os.getenv("CHAMPION_PRIZE_OLD", os.getenv("CHAMPION_PRIZE", "50"))
GROUP_URL = os.getenv("GROUP_URL", "#")
ADMIN_USER = os.getenv("ADMIN_USERNAME", "admin")
ADMIN_PASS = os.getenv("ADMIN_PASSWORD", "admin123")
DIVISIONS = {
    "nova": {"label": "Nova geração", "platforms": ["PlayStation 5", "Xbox Series X|S", "PC"], "prize": PRIZE_NEW},
    "antiga": {"label": "Antiga geração", "platforms": ["PlayStation 4", "Xbox One"], "prize": PRIZE_OLD},
}

@app.on_event("startup")
def startup():
    Base.metadata.create_all(bind=engine)
    db = next(get_db())
    try:
        get_setting(db)
    finally:
        db.close()

def ctx(request: Request, **kwargs):
    return {
        "request": request,
        "max_players": MAX_PLAYERS,
        "group_url": GROUP_URL,
        "divisions": DIVISIONS,
        **kwargs,
    }

def require_admin(request: Request):
    if not request.session.get("admin"):
        raise HTTPException(403)

@app.get("/", response_class=HTMLResponse)
def home(request: Request, db: Session = Depends(get_db)):
    setting = get_setting(db)
    players = db.scalars(select(Player).where(Player.active == True, Player.approved == True)).all()
    counts = {key: sum(1 for p in players if p.division == key) for key in DIVISIONS}
    recent = sorted(players, key=lambda p: p.created_at, reverse=True)[:8]
    last_matches = db.scalars(
        select(Match).where(Match.status == "finalizada").order_by(Match.id.desc()).limit(6)
    ).all()
    return templates.TemplateResponse("home.html", ctx(
        request,
        setting=setting,
        counts=counts,
        remaining={key: max(0, MAX_PLAYERS-counts[key]) for key in DIVISIONS},
        recent=recent,
        last_matches=last_matches,
    ))

@app.get("/inscricao", response_class=HTMLResponse)
def register_page(request: Request, db: Session = Depends(get_db)):
    return templates.TemplateResponse("register.html", ctx(
        request,
        setting=get_setting(db),
        errors=[],
        selected=request.query_params.get("divisao", "nova"),
    ))

@app.post("/inscricao")
def register(
    request: Request,
    name: Annotated[str, Form()],
    whatsapp: Annotated[str, Form()],
    ea_id: Annotated[str, Form()],
    division: Annotated[str, Form()],
    platform: Annotated[str, Form()],
    password: Annotated[str, Form()],
    accept_rules: Annotated[str | None, Form()] = None,
    db: Session = Depends(get_db),
):
    setting = get_setting(db)
    count = db.scalar(
        select(func.count()).select_from(Player).where(
            Player.active == True,
            Player.division == division,
        )
    )
    errors = []
    if not setting.registrations_open:
        errors.append("As inscrições estão fechadas.")
    if division not in DIVISIONS:
        errors.append("Divisão inválida.")
    elif platform not in DIVISIONS[division]["platforms"]:
        errors.append("Plataforma incompatível com a divisão.")
    if count >= MAX_PLAYERS:
        errors.append("As vagas desta divisão foram preenchidas.")
    if len(name.strip()) < 2:
        errors.append("Informe um nome válido.")
    if len(whatsapp.strip()) < 10:
        errors.append("Informe o WhatsApp com DDD.")
    if len(ea_id.strip()) < 3:
        errors.append("Informe um ID EA válido.")
    if len(password) < 6:
        errors.append("A senha deve ter ao menos 6 caracteres.")
    if not accept_rules:
        errors.append("Você precisa aceitar o regulamento.")

    if errors:
        return templates.TemplateResponse("register.html", ctx(
            request, setting=setting, errors=errors, selected=division
        ), status_code=400)

    player = Player(
        name=name.strip(),
        whatsapp=whatsapp.strip(),
        ea_id=ea_id.strip(),
        platform=platform,
        division=division,
        password_hash=hash_password(password),
        approved=True,
    )
    db.add(player)
    try:
        db.commit()
        db.refresh(player)
    except IntegrityError:
        db.rollback()
        return templates.TemplateResponse("register.html", ctx(
            request, setting=setting, errors=["WhatsApp ou ID EA já cadastrado."], selected=division
        ), status_code=400)

    request.session["player_id"] = player.id
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
    player = db.scalar(select(Player).where(or_(
        Player.whatsapp == identifier.strip(),
        Player.ea_id == identifier.strip(),
    )))
    if not player or not verify_password(password, player.password_hash):
        return templates.TemplateResponse("login.html", ctx(
            request, error="WhatsApp/ID EA ou senha incorretos."
        ), status_code=400)
    request.session["player_id"] = player.id
    return RedirectResponse("/painel", 303)

@app.get("/sair")
def logout(request: Request):
    request.session.clear()
    return RedirectResponse("/", 303)

@app.get("/painel", response_class=HTMLResponse)
def dashboard(request: Request, db: Session = Depends(get_db)):
    player_id = request.session.get("player_id")
    if not player_id:
        return RedirectResponse("/login", 303)
    player = db.get(Player, player_id)
    matches = db.scalars(
        select(Match).where(or_(
            Match.player1_id == player_id,
            Match.player2_id == player_id,
        )).order_by(Match.id)
    ).all()
    return templates.TemplateResponse("dashboard.html", ctx(
        request, player=player, matches=matches, new=request.query_params.get("novo")
    ))

@app.get("/ranking", response_class=HTMLResponse)
def ranking(request: Request, division: str = "nova", db: Session = Depends(get_db)):
    if division not in DIVISIONS:
        division = "nova"
    return templates.TemplateResponse("ranking.html", ctx(
        request,
        players=ranking_query(db, division),
        selected_division=division,
    ))

@app.get("/regulamento", response_class=HTMLResponse)
def rules(request: Request):
    return templates.TemplateResponse("rules.html", ctx(request))

@app.get("/admin/login", response_class=HTMLResponse)
def admin_login_page(request: Request):
    return templates.TemplateResponse("admin_login.html", ctx(request, error=None))

@app.post("/admin/login")
def admin_login(
    request: Request,
    username: Annotated[str, Form()],
    password: Annotated[str, Form()],
):
    if username != ADMIN_USER or password != ADMIN_PASS:
        return templates.TemplateResponse("admin_login.html", ctx(
            request, error="Credenciais incorretas."
        ), status_code=400)
    request.session["admin"] = True
    return RedirectResponse("/admin", 303)

@app.get("/admin", response_class=HTMLResponse)
def admin(request: Request, db: Session = Depends(get_db)):
    require_admin(request)
    return templates.TemplateResponse("admin.html", ctx(
        request,
        setting=get_setting(db),
        players=db.scalars(select(Player).order_by(Player.id)).all(),
        matches=db.scalars(select(Match).order_by(Match.division, Match.phase, Match.id)).all(),
        message=request.query_params.get("message"),
    ))

@app.post("/admin/configuracao")
def update_settings(
    request: Request,
    registrations_open: Annotated[str, Form()],
    championship_date: Annotated[str, Form()],
    announcement: Annotated[str, Form()],
    db: Session = Depends(get_db),
):
    require_admin(request)
    setting = get_setting(db)
    setting.registrations_open = registrations_open == "1"
    setting.championship_date = championship_date.strip() or "A definir"
    setting.announcement = announcement.strip()
    db.commit()
    return RedirectResponse("/admin?message=Configurações atualizadas", 303)

@app.post("/admin/sortear")
def draw(
    request: Request,
    division: Annotated[str, Form()],
    db: Session = Depends(get_db),
):
    require_admin(request)
    try:
        groups = draw_groups(db, get_setting(db).season, division)
        message = f"{len(groups)} grupos criados na {DIVISIONS[division]['label']}"
    except Exception as exc:
        message = str(exc)
    return RedirectResponse(f"/admin?message={message}", 303)

@app.post("/admin/mata-mata")
def knockout(
    request: Request,
    division: Annotated[str, Form()],
    db: Session = Depends(get_db),
):
    require_admin(request)
    try:
        total = generate_knockout(db, get_setting(db).season, division)
        message = f"{total} confrontos de mata-mata gerados"
    except Exception as exc:
        message = str(exc)
    return RedirectResponse(f"/admin?message={message}", 303)

@app.post("/admin/partida/{match_id}")
def score(
    match_id: int,
    request: Request,
    score1: Annotated[int, Form()],
    score2: Annotated[int, Form()],
    scheduled_for: Annotated[str, Form()] = "A definir",
    db: Session = Depends(get_db),
):
    require_admin(request)
    match = db.get(Match, match_id)
    if not match:
        raise HTTPException(404)
    match.score1 = max(0, score1)
    match.score2 = max(0, score2)
    match.scheduled_for = scheduled_for.strip() or "A definir"
    match.status = "finalizada"
    db.commit()
    recalculate(db)
    return RedirectResponse("/admin?message=Placar salvo", 303)

@app.post("/admin/jogador/{player_id}/status")
def player_status(
    player_id: int,
    request: Request,
    action: Annotated[str, Form()],
    db: Session = Depends(get_db),
):
    require_admin(request)
    player = db.get(Player, player_id)
    if player:
        if action == "remove":
            player.active = False
        elif action == "restore":
            player.active = True
        elif action == "approve":
            player.approved = True
        db.commit()
    return RedirectResponse("/admin?message=Jogador atualizado", 303)

@app.post("/admin/importar")
async def import_json(
    request: Request,
    file: UploadFile = File(...),
    db: Session = Depends(get_db),
):
    require_admin(request)
    try:
        data = json.loads((await file.read()).decode("utf-8-sig"))
        if not isinstance(data, list):
            raise ValueError
    except Exception:
        return RedirectResponse("/admin?message=JSON inválido", 303)

    imported = 0
    for item in data:
        telegram_id = item.get("id")
        ea_id = str(item.get("ea_id") or f"migrado-{telegram_id}")
        if db.scalar(select(Player).where(or_(
            Player.telegram_id == telegram_id,
            Player.ea_id == ea_id,
        ))):
            continue
        division = str(item.get("divisao") or "nova")
        if division not in DIVISIONS:
            division = "nova"
        temporary_password = str(telegram_id or "123456")[-6:].rjust(6, "0")
        db.add(Player(
            telegram_id=telegram_id,
            name=str(item.get("nome") or "Jogador"),
            whatsapp=str(item.get("whatsapp") or f"telegram-{telegram_id}"),
            ea_id=ea_id,
            platform=str(item.get("plataforma") or "Não informada"),
            division=division,
            password_hash=hash_password(temporary_password),
            points=int(item.get("pontos", 0) or 0),
            played=int(item.get("jogos", 0) or 0),
            wins=int(item.get("vitorias", 0) or 0),
            draws=int(item.get("empates", 0) or 0),
            losses=int(item.get("derrotas", 0) or 0),
            goals_for=int(item.get("gols", 0) or 0),
            goals_against=int(item.get("gols_sofridos", 0) or 0),
            titles=int(item.get("titulos", 0) or 0),
        ))
        try:
            db.commit()
            imported += 1
        except IntegrityError:
            db.rollback()

    return RedirectResponse(f"/admin?message={imported} jogadores importados", 303)

@app.get("/health")
def health():
    return {"status": "ok", "version": "3.0.0"}
