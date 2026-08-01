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
from .services import draw_groups, get_setting, recalculate

BASE = Path(__file__).resolve().parent
app = FastAPI(title="FC26 Arena")
app.add_middleware(SessionMiddleware, secret_key=os.getenv("SECRET_KEY", "dev-secret"))
app.mount("/static", StaticFiles(directory=BASE / "static"), name="static")
templates = Jinja2Templates(directory=BASE / "templates")

MAX_PLAYERS = int(os.getenv("MAX_PLAYERS", "32"))
PRIZE = os.getenv("CHAMPION_PRIZE", "50")
GROUP_URL = os.getenv("GROUP_URL", "#")
ADMIN_USER = os.getenv("ADMIN_USERNAME", "admin")
ADMIN_PASS = os.getenv("ADMIN_PASSWORD", "admin123")

@app.on_event("startup")
def startup():
    Base.metadata.create_all(bind=engine)
    db = next(get_db())
    try:
        get_setting(db)
    finally:
        db.close()

def ctx(request: Request, **kwargs):
    return {"request": request, "max_players": MAX_PLAYERS, "prize": PRIZE, "group_url": GROUP_URL, **kwargs}

def require_admin(request: Request):
    if not request.session.get("admin"):
        raise HTTPException(403)

@app.get("/", response_class=HTMLResponse)
def home(request: Request, db: Session = Depends(get_db)):
    players = db.scalars(select(Player).where(Player.active == True).order_by(Player.points.desc())).all()
    setting = get_setting(db)
    return templates.TemplateResponse("home.html", ctx(
        request, players=players, setting=setting, registered=len(players),
        remaining=max(0, MAX_PLAYERS-len(players))
    ))

@app.get("/inscricao", response_class=HTMLResponse)
def register_page(request: Request, db: Session = Depends(get_db)):
    return templates.TemplateResponse("register.html", ctx(request, setting=get_setting(db), errors=[]))

@app.post("/inscricao")
def register(request: Request,
    name: Annotated[str, Form()],
    whatsapp: Annotated[str, Form()],
    ea_id: Annotated[str, Form()],
    platform: Annotated[str, Form()],
    password: Annotated[str, Form()],
    db: Session = Depends(get_db)):
    setting = get_setting(db)
    count = db.scalar(select(func.count()).select_from(Player).where(Player.active == True))
    errors = []
    if not setting.registrations_open: errors.append("Inscrições fechadas.")
    if count >= MAX_PLAYERS: errors.append("Vagas preenchidas.")
    if len(password) < 6: errors.append("A senha deve ter 6 caracteres.")
    if errors:
        return templates.TemplateResponse("register.html", ctx(request, setting=setting, errors=errors), status_code=400)
    p = Player(name=name.strip(), whatsapp=whatsapp.strip(), ea_id=ea_id.strip(),
               platform=platform, password_hash=hash_password(password))
    db.add(p)
    try:
        db.commit(); db.refresh(p)
    except IntegrityError:
        db.rollback()
        return templates.TemplateResponse("register.html", ctx(request, setting=setting, errors=["WhatsApp ou ID EA já cadastrado."]), status_code=400)
    request.session["player_id"] = p.id
    return RedirectResponse("/painel?novo=1", 303)

@app.get("/login", response_class=HTMLResponse)
def login_page(request: Request):
    return templates.TemplateResponse("login.html", ctx(request, error=None))

@app.post("/login")
def login(request: Request, identifier: Annotated[str, Form()], password: Annotated[str, Form()], db: Session = Depends(get_db)):
    p = db.scalar(select(Player).where(or_(Player.whatsapp == identifier.strip(), Player.ea_id == identifier.strip())))
    if not p or not verify_password(password, p.password_hash):
        return templates.TemplateResponse("login.html", ctx(request, error="Dados incorretos."), status_code=400)
    request.session["player_id"] = p.id
    return RedirectResponse("/painel", 303)

@app.get("/sair")
def logout(request: Request):
    request.session.clear()
    return RedirectResponse("/", 303)

@app.get("/painel", response_class=HTMLResponse)
def dashboard(request: Request, db: Session = Depends(get_db)):
    pid = request.session.get("player_id")
    if not pid: return RedirectResponse("/login", 303)
    player = db.get(Player, pid)
    matches = db.scalars(select(Match).where(or_(Match.player1_id == pid, Match.player2_id == pid))).all()
    return templates.TemplateResponse("dashboard.html", ctx(request, player=player, matches=matches, new=request.query_params.get("novo")))

@app.get("/ranking", response_class=HTMLResponse)
def ranking(request: Request, db: Session = Depends(get_db)):
    players = db.scalars(select(Player).where(Player.active == True).order_by(Player.points.desc(), Player.wins.desc())).all()
    return templates.TemplateResponse("ranking.html", ctx(request, players=players))

@app.get("/regulamento", response_class=HTMLResponse)
def rules(request: Request):
    return templates.TemplateResponse("rules.html", ctx(request))

@app.get("/admin/login", response_class=HTMLResponse)
def admin_login_page(request: Request):
    return templates.TemplateResponse("admin_login.html", ctx(request, error=None))

@app.post("/admin/login")
def admin_login(request: Request, username: Annotated[str, Form()], password: Annotated[str, Form()]):
    if username != ADMIN_USER or password != ADMIN_PASS:
        return templates.TemplateResponse("admin_login.html", ctx(request, error="Credenciais incorretas."), status_code=400)
    request.session["admin"] = True
    return RedirectResponse("/admin", 303)

@app.get("/admin", response_class=HTMLResponse)
def admin(request: Request, db: Session = Depends(get_db)):
    require_admin(request)
    return templates.TemplateResponse("admin.html", ctx(
        request, setting=get_setting(db),
        players=db.scalars(select(Player).order_by(Player.id)).all(),
        matches=db.scalars(select(Match).order_by(Match.group_name, Match.id)).all(),
        message=request.query_params.get("message")
    ))

@app.post("/admin/inscricoes")
def toggle(request: Request, state: Annotated[str, Form()], db: Session = Depends(get_db)):
    require_admin(request)
    s = get_setting(db); s.registrations_open = state == "1"; db.commit()
    return RedirectResponse("/admin?message=Inscrições atualizadas", 303)

@app.post("/admin/sortear")
def draw(request: Request, db: Session = Depends(get_db)):
    require_admin(request)
    try:
        total = draw_groups(db, get_setting(db).season)
        msg = f"{total} grupos criados"
    except ValueError as e:
        msg = str(e)
    return RedirectResponse(f"/admin?message={msg}", 303)

@app.post("/admin/partida/{match_id}")
def score(match_id: int, request: Request, score1: Annotated[int, Form()], score2: Annotated[int, Form()], db: Session = Depends(get_db)):
    require_admin(request)
    m = db.get(Match, match_id)
    if not m: raise HTTPException(404)
    m.score1, m.score2, m.status = max(0,score1), max(0,score2), "finalizada"
    db.commit(); recalculate(db)
    return RedirectResponse("/admin?message=Placar salvo", 303)

@app.post("/admin/importar")
async def import_json(request: Request, file: UploadFile = File(...), db: Session = Depends(get_db)):
    require_admin(request)
    try:
        data = json.loads((await file.read()).decode("utf-8-sig"))
        if not isinstance(data, list): raise ValueError
    except Exception:
        return RedirectResponse("/admin?message=JSON inválido", 303)
    imported = 0
    for item in data:
        tg = item.get("id")
        ea = str(item.get("ea_id") or f"migrado-{tg}")
        if db.scalar(select(Player).where(or_(Player.telegram_id == tg, Player.ea_id == ea))):
            continue
        temp = str(tg or "123456")[-6:].rjust(6,"0")
        db.add(Player(
            telegram_id=tg, name=str(item.get("nome") or "Jogador"),
            whatsapp=str(item.get("whatsapp") or f"telegram-{tg}"),
            ea_id=ea, platform=str(item.get("plataforma") or "Não informada"),
            password_hash=hash_password(temp),
            points=int(item.get("pontos",0) or 0), played=int(item.get("jogos",0) or 0),
            wins=int(item.get("vitorias",0) or 0), draws=int(item.get("empates",0) or 0),
            losses=int(item.get("derrotas",0) or 0), goals_for=int(item.get("gols",0) or 0),
            goals_against=int(item.get("gols_sofridos",0) or 0)
        ))
        try: db.commit(); imported += 1
        except IntegrityError: db.rollback()
    return RedirectResponse(f"/admin?message={imported} jogadores importados", 303)

@app.get("/health")
def health():
    return {"status":"ok","version":"1.0.0"}
