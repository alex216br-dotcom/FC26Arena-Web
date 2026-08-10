import os
import re
import shutil
from datetime import datetime, timedelta
from decimal import Decimal, InvalidOperation, ROUND_HALF_UP
from pathlib import Path
from typing import Annotated
from urllib.parse import quote, urlencode

from fastapi import Depends, FastAPI, File, Form, Header, HTTPException, Request, UploadFile
from fastapi.responses import FileResponse, HTMLResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from sqlalchemy import func, inspect, or_, select, text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session, selectinload
from starlette.middleware.sessions import SessionMiddleware

from .database import Base, engine, get_db
from .integrations import notify_user, send_email
from .models import (
    Achievement, AdminUser, AuditLog, Coupon, Evidence, Match, MatchMessage,
    Notification, PasswordReset, Payment, PrizeConversation, PrizeMessage,
    Registration, Report, Season, SupportTicket, Team, TeamMember, Tournament,
    TournamentPrize, User, UserAchievement,
)
from .security import hash_password, token_urlsafe, verify_password
from .services import (
    audit, clear_tournament_matches, entry_name, generate_groups,
    generate_league, pair_quick_duel_waiting_players,
    process_confirmed_result, registration_users,
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


def ensure_tournament_format_columns():
    """
    Atualiza bancos já existentes sem apagar campeonatos ou inscrições.
    create_all() cria tabelas novas, mas não adiciona colunas em tabelas antigas.
    """
    inspector = inspect(engine)
    if "tournaments" not in inspector.get_table_names():
        return

    existing_columns = {
        column["name"] for column in inspector.get_columns("tournaments")
    }
    statements: list[str] = []

    if "competition_format" not in existing_columns:
        statements.append(
            "ALTER TABLE tournaments "
            "ADD COLUMN competition_format VARCHAR(30) "
            "NOT NULL DEFAULT 'groups_knockout'"
        )

    if "league_turns" not in existing_columns:
        statements.append(
            "ALTER TABLE tournaments "
            "ADD COLUMN league_turns INTEGER NOT NULL DEFAULT 2"
        )

    if "quick_duel" not in existing_columns:
        statements.append(
            "ALTER TABLE tournaments "
            "ADD COLUMN quick_duel BOOLEAN NOT NULL DEFAULT FALSE"
        )

    if "duel_series" not in existing_columns:
        statements.append(
            "ALTER TABLE tournaments "
            "ADD COLUMN duel_series INTEGER NOT NULL DEFAULT 1"
        )

    if "match_minutes" not in existing_columns:
        statements.append(
            "ALTER TABLE tournaments "
            "ADD COLUMN match_minutes INTEGER NOT NULL DEFAULT 5"
        )

    if "squad_type" not in existing_columns:
        statements.append(
            "ALTER TABLE tournaments "
            "ADD COLUMN squad_type VARCHAR(30) NOT NULL DEFAULT 'online'"
        )

    if "allow_classic_teams" not in existing_columns:
        statements.append(
            "ALTER TABLE tournaments "
            "ADD COLUMN allow_classic_teams BOOLEAN NOT NULL DEFAULT TRUE"
        )

    if "allow_national_teams" not in existing_columns:
        statements.append(
            "ALTER TABLE tournaments "
            "ADD COLUMN allow_national_teams BOOLEAN NOT NULL DEFAULT TRUE"
        )

    if "knockout_extra_time" not in existing_columns:
        statements.append(
            "ALTER TABLE tournaments "
            "ADD COLUMN knockout_extra_time BOOLEAN NOT NULL DEFAULT TRUE"
        )

    if "require_result_confirmation" not in existing_columns:
        statements.append(
            "ALTER TABLE tournaments "
            "ADD COLUMN require_result_confirmation BOOLEAN NOT NULL DEFAULT TRUE"
        )

    if statements:
        with engine.begin() as connection:
            for statement in statements:
                connection.execute(text(statement))

    # Atualiza premiações antigas para permitir uma conversa por duelo.
    inspector = inspect(engine)
    if "prize_conversations" in inspector.get_table_names():
        prize_columns = {
            column["name"]
            for column in inspector.get_columns("prize_conversations")
        }
        with engine.begin() as connection:
            if "match_id" not in prize_columns:
                connection.execute(text(
                    "ALTER TABLE prize_conversations "
                    "ADD COLUMN match_id INTEGER NULL"
                ))
            if engine.dialect.name == "postgresql":
                connection.execute(text(
                    "ALTER TABLE prize_conversations "
                    "DROP CONSTRAINT IF EXISTS uq_prize_conversation_winner"
                ))



DEFAULT_MATCH_RULES = (
    "• Duração da partida: 5 minutos por tempo.\n"
    "• Elenco obrigatório: Online.\n"
    "• São permitidos times clássicos e seleções.\n"
    "• Em partidas eliminatórias, empate deve ser decidido na prorrogação e nos pênaltis.\n"
    "• O resultado precisa ser confirmado pelos dois jogadores na Sala PVP."
)


def normal_tournament_rules(rules: str | None) -> str:
    """Aplica regras padrão somente nos campeonatos normais."""
    clean = (rules or "").strip()
    return clean or DEFAULT_MATCH_RULES


def tournament_display_rules(tournament: Tournament) -> dict[str, object]:
    if tournament.quick_duel:
        return {
            "items": [],
            "additional": (tournament.rules or "").strip(),
        }

    squad_labels = {
        "online": "Online",
        "custom": "Personalizado",
        "default": "Padrão",
    }
    items = [
        f"Duração da partida: {tournament.match_minutes} minutos por tempo.",
        f"Tipo de elenco: {squad_labels.get(tournament.squad_type, tournament.squad_type)}.",
        (
            "Times clássicos são permitidos."
            if tournament.allow_classic_teams
            else "Times clássicos não são permitidos."
        ),
        (
            "Seleções são permitidas."
            if tournament.allow_national_teams
            else "Seleções não são permitidas."
        ),
        (
            "No mata-mata, empate deve ser decidido na prorrogação e nos pênaltis."
            if tournament.knockout_extra_time
            else "No mata-mata, siga a regra adicional definida pela administração em caso de empate."
        ),
        (
            "O resultado deve ser confirmado pelos dois jogadores na Sala PVP."
            if tournament.require_result_confirmation
            else "A confirmação do adversário na Sala PVP não é obrigatória."
        ),
    ]
    return {
        "items": items,
        "additional": (tournament.rules or "").strip(),
    }


def minimum_entries_for_schedule(tournament: Tournament) -> int:
    if tournament.competition_format == "league":
        return 2
    return max(2, tournament.group_size)


def start_quick_duel_if_ready(db: Session, tournament: Tournament) -> bool:
    """Forma todos os pares disponíveis sem fechar a arena."""
    return pair_quick_duel_waiting_players(db, tournament) > 0


def _money(value: Decimal | float | int | str) -> Decimal:
    return Decimal(str(value or 0)).quantize(
        Decimal("0.01"),
        rounding=ROUND_HALF_UP,
    )


def parse_prize_distribution(
    prize_places: int,
    distribution: str,
    total_prize: float,
) -> list[Decimal]:
    """
    Retorna um valor para cada colocação premiada.

    Exemplos aceitos:
    50; 30; 20
    50
    50,00; 30,00; 20,00

    Se o campo estiver vazio, divide o prêmio total igualmente.
    """
    if prize_places < 1 or prize_places > 20:
        raise ValueError("Escolha entre 1 e 20 colocados premiados.")

    raw = (distribution or "").strip()
    if raw:
        if ";" in raw or "\n" in raw:
            tokens = [
                token.strip()
                for token in re.split(r"[;\n]+", raw)
                if token.strip()
            ]
        elif prize_places == 1:
            tokens = [raw]
        else:
            tokens = [
                token.strip()
                for token in raw.split(",")
                if token.strip()
            ]

        if len(tokens) != prize_places:
            raise ValueError(
                f"Informe exatamente {prize_places} valor(es), "
                "um para cada colocação premiada."
            )

        amounts: list[Decimal] = []
        for token in tokens:
            clean = token.replace("R$", "").replace(" ", "")
            if "," in clean:
                clean = clean.replace(".", "").replace(",", ".")
            try:
                amount = _money(Decimal(clean))
            except (InvalidOperation, ValueError):
                raise ValueError(f"Valor de premiação inválido: {token}.")
            if amount < 0:
                raise ValueError("Os valores da premiação não podem ser negativos.")
            amounts.append(amount)
        return amounts

    total = _money(max(0, total_prize))
    base = (total / prize_places).quantize(
        Decimal("0.01"),
        rounding=ROUND_HALF_UP,
    )
    amounts = [base for _ in range(prize_places)]
    difference = total - sum(amounts, Decimal("0.00"))
    amounts[-1] = _money(amounts[-1] + difference)
    return amounts


def save_tournament_prizes(
    db: Session,
    tournament: Tournament,
    amounts: list[Decimal],
) -> None:
    db.query(TournamentPrize).filter(
        TournamentPrize.tournament_id == tournament.id
    ).delete(synchronize_session=False)

    for place, amount in enumerate(amounts, start=1):
        db.add(TournamentPrize(
            tournament_id=tournament.id,
            place=place,
            amount=amount,
        ))

    tournament.prize = sum(amounts, Decimal("0.00"))
    db.flush()


def tournament_prize_awards(
    db: Session,
    tournament: Tournament,
) -> list[dict[str, int | float]]:
    rows = db.scalars(
        select(TournamentPrize)
        .where(TournamentPrize.tournament_id == tournament.id)
        .order_by(TournamentPrize.place)
    ).all()

    if rows:
        return [
            {"place": row.place, "amount": float(row.amount)}
            for row in rows
        ]

    # Compatibilidade com campeonatos criados antes desta atualização.
    return [{
        "place": 1,
        "amount": float(tournament.prize or 0),
    }]


def tournament_prize_form(
    db: Session,
    tournament: Tournament,
) -> tuple[int, str]:
    awards = tournament_prize_awards(db, tournament)
    values = "; ".join(
        f"{award['amount']:.2f}".replace(".", ",")
        for award in awards
    )
    return len(awards), values



def safe_return_path(value: str | None, default: str = "/painel") -> str:
    """Aceita somente caminhos internos para evitar redirecionamento externo."""
    path = (value or "").strip()
    if path.startswith("/") and not path.startswith("//"):
        return path
    return default

@app.on_event("startup")
def startup():
    Base.metadata.create_all(bind=engine)
    ensure_tournament_format_columns()
    db = next(get_db())
    try:
        if not db.scalar(select(Season).limit(1)):
            db.add(Season(name="Temporada 1", active=True))
            db.commit()
        seed_achievements(db)
        if not db.scalar(select(Tournament).limit(1)):
            season = db.scalar(select(Season).where(Season.active == True).limit(1))
            demos = [
                Tournament(season_id=season.id, name="Copa FC26 Elite", slug="copa-fc26-elite", mode="1x1", competition_format="groups_knockout", league_turns=2, generation="nova", max_entries=32, prize=50, status="open", color_theme="green", rules="Fase de grupos e mata-mata."),
                Tournament(season_id=season.id, name="Duplas Champions", slug="duplas-champions", mode="2x2", competition_format="groups_knockout", league_turns=2, generation="nova", max_entries=16, prize=100, status="open", color_theme="purple", rules="Equipes com 2 jogadores."),
                Tournament(season_id=season.id, name="Pro Clubs League", slug="pro-clubs-league", mode="Pro Clubs", competition_format="league", league_turns=2, generation="nova", max_entries=8, prize=200, status="open", color_theme="cyan", rules="Campeonato para clubes."),
                Tournament(season_id=season.id, name="Legends Cup", slug="legends-cup", mode="1x1", competition_format="groups_knockout", league_turns=2, generation="antiga", max_entries=32, prize=50, status="open", color_theme="orange", rules="PS4 e Xbox One."),
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

def normalize_phone(value: str | None) -> str:
    digits = "".join(character for character in (value or "") if character.isdigit())
    if digits.startswith("55") and len(digits) in {12, 13}:
        digits = digits[2:]
    return digits


def find_login_user(db: Session, identifier: str) -> User | None:
    value = (identifier or "").strip()
    if not value:
        return None

    lowered = value.lower()
    user = db.scalar(
        select(User).where(
            or_(
                func.lower(User.email) == lowered,
                func.lower(User.ea_id) == lowered,
            )
        )
    )
    if user:
        return user

    phone = normalize_phone(value)
    if phone:
        # Compatível com números antigos salvos com espaços, parênteses,
        # hífens ou código do Brasil.
        for candidate in db.scalars(select(User)).all():
            if normalize_phone(candidate.whatsapp) == phone:
                return candidate

    return None


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
        raise HTTPException(status_code=303, headers={"Location": "/admin/login"})


def append_query_params(location: str, **params) -> str:
    """Adiciona parâmetros antes do fragmento (#) sem quebrar a URL."""
    if not location:
        location = "/"

    fragment = ""
    if "#" in location:
        location, fragment_value = location.split("#", 1)
        fragment = f"#{fragment_value}"

    clean_params = {
        key: str(value)
        for key, value in params.items()
        if value is not None and str(value) != ""
    }
    if not clean_params:
        return location + fragment

    separator = "&" if "?" in location else "?"
    query = urlencode(clean_params)
    return f"{location}{separator}{query}{fragment}"


def analytics_location(location: str, event_name: str, **event_params) -> str:
    payload = {"ga_event": event_name}
    for key, value in event_params.items():
        if value is not None:
            payload[f"ga_{key}"] = value
    return append_query_params(location, **payload)


def admin_redirect(message: str, location: str = "/admin") -> RedirectResponse:
    return RedirectResponse(
        append_query_params(location, message=str(message)),
        303,
    )

def registration_label(registration: Registration | None) -> str:
    if registration is None:
        return "Participante indisponível"
    if registration.team:
        return registration.team.name
    if registration.user:
        return registration.user.name
    return f"Inscrição #{registration.id}"

def registration_meta(registration: Registration | None) -> str:
    if registration is None:
        return "Cadastro indisponível"
    if registration.team:
        return registration.team.mode or "Equipe"
    if registration.user:
        return registration.user.platform or "Plataforma não informada"
    return "Cadastro incompleto"

def registration_contact(registration: Registration | None) -> str:
    if registration is None or not registration.user:
        return "—"
    return registration.user.whatsapp or "—"

def registration_ea_id(registration: Registration | None) -> str:
    if registration is None or not registration.user:
        return "—"
    return registration.user.ea_id or "—"


def apply_payment_status(
    db: Session,
    payment: Payment,
    status: str,
) -> tuple[Payment, Registration]:
    """
    Mantém o pagamento e a inscrição sempre sincronizados.
    """
    normalized_status = status.strip().lower()
    allowed_statuses = {"pending", "approved", "rejected"}

    if normalized_status not in allowed_statuses:
        raise ValueError("Status de pagamento inválido.")

    registration = db.get(Registration, payment.registration_id)
    if not registration:
        raise ValueError("A inscrição vinculada ao pagamento não existe.")

    payment.status = normalized_status

    if normalized_status == "approved":
        registration.payment_status = "approved"
        registration.status = "approved"
    elif normalized_status == "rejected":
        registration.payment_status = "rejected"
        registration.status = "rejected"
    else:
        registration.payment_status = "pending"
        registration.status = "pending"

    db.flush()
    db.commit()
    db.refresh(payment)
    db.refresh(registration)

    if normalized_status == "approved":
        tournament = db.get(Tournament, registration.tournament_id)
        if tournament:
            start_quick_duel_if_ready(db, tournament)

    return payment, registration


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
    return_to: Annotated[str, Form()] = "/painel?novo=1",
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
            matches_count=0, prize_total=0, ranking=[], open_register=True,
            register_errors=errors,
            register_return_to=safe_return_path(return_to, "/painel?novo=1"),
        ), status_code=400)

    user = User(
        name=name.strip(),
        whatsapp=normalize_phone(whatsapp),
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
            register_return_to=safe_return_path(return_to, "/painel?novo=1"),
        ), status_code=400)

    achievement = db.scalar(select(Achievement).where(Achievement.code == "WELCOME"))
    if achievement:
        db.add(UserAchievement(user_id=user.id, achievement_id=achievement.id))
        user.xp += achievement.xp_reward
        user.level = max(1, user.xp // 500 + 1)
        db.commit()
    request.session["user_id"] = user.id
    notify_user(db, user, "Bem-vindo ao FC26 Arena", "Sua conta foi criada com sucesso.")
    return RedirectResponse(
        analytics_location(
            safe_return_path(return_to, "/painel?novo=1"),
            "cadastro_concluido",
            method="site",
            user_id=user.id,
        ),
        303,
    )

@app.get("/login", response_class=HTMLResponse)
def login_page(request: Request):
    return_to = safe_return_path(
        request.query_params.get("next"),
        "/painel",
    )
    return templates.TemplateResponse(
        "login.html",
        ctx(request, error=None, return_to=return_to, identifier=""),
    )


@app.post("/login")
def login(
    request: Request,
    identifier: Annotated[str, Form()],
    password: Annotated[str, Form()],
    return_to: Annotated[str, Form()] = "/painel",
    db: Session = Depends(get_db),
):
    safe_destination = safe_return_path(return_to, "/painel")
    user = find_login_user(db, identifier)

    if not user or not verify_password(password, user.password_hash):
        return templates.TemplateResponse(
            "login.html",
            ctx(
                request,
                error=(
                    "Não foi possível entrar. Confira o WhatsApp, e-mail ou ID EA "
                    "e digite novamente a senha."
                ),
                return_to=safe_destination,
                identifier=identifier.strip(),
            ),
            status_code=200,
        )

    if not user.active:
        return templates.TemplateResponse(
            "login.html",
            ctx(
                request,
                error="Esta conta está desativada. Fale com a administração.",
                return_to=safe_destination,
                identifier=identifier.strip(),
            ),
            status_code=200,
        )

    request.session.clear()
    request.session["user_id"] = int(user.id)
    request.session["login_version"] = 1

    response = RedirectResponse(safe_destination, 303)
    return response

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
    user = current_user(request, db)
    if not user:
        return RedirectResponse(
            f"/login?next={quote(str(request.url.path) + ('?' + request.url.query if request.url.query else ''))}",
            303,
        )
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

    prize_conversations = db.scalars(
        select(PrizeConversation)
        .options(
            selectinload(PrizeConversation.tournament),
            selectinload(PrizeConversation.registration).selectinload(Registration.user),
            selectinload(PrizeConversation.registration).selectinload(Registration.team),
        )
        .order_by(PrizeConversation.id.desc())
    ).all()
    prize_conversations = [
        conversation
        for conversation in prize_conversations
        if registration_contains_user(db, conversation.registration, user)
    ]

    return templates.TemplateResponse("dashboard.html", ctx(
        request, user=user, registrations=registrations, matches=user_matches,
        achievements=achievements, notifications=notifications,
        prize_conversations=prize_conversations,
        new=request.query_params.get("novo")
    ))

@app.get("/campeonatos", response_class=HTMLResponse)
def tournaments_page(request: Request, db: Session = Depends(get_db)):
    tournaments = db.scalars(select(Tournament).where(Tournament.status != "archived").order_by(Tournament.id.desc())).all()
    return templates.TemplateResponse("tournaments.html", ctx(request, tournaments=tournaments))

@app.get("/campeonato/{slug}", response_class=HTMLResponse)
def tournament_page(
    slug: str,
    request: Request,
    db: Session = Depends(get_db),
):
    tournament = db.scalar(
        select(Tournament).where(Tournament.slug == slug)
    )
    if not tournament:
        raise HTTPException(404)

    registrations = db.scalars(
        select(Registration)
        .options(
            selectinload(Registration.user),
            selectinload(Registration.team),
        )
        .where(Registration.tournament_id == tournament.id)
    ).all()

    user = current_user(request, db)
    teams = []
    if user:
        memberships = db.scalars(
            select(TeamMember).where(TeamMember.user_id == user.id)
        ).all()
        teams = [
            membership.team
            for membership in memberships
            if membership.team and membership.team.mode == tournament.mode
        ]

    groups: dict[str, list[Registration]] = {}
    league_table: list[Registration] = []

    if tournament.competition_format == "league":
        league_table = sorted_group([
            registration
            for registration in registrations
            if registration.status == "approved"
        ])
    else:
        for registration in registrations:
            if registration.group_name:
                groups.setdefault(
                    registration.group_name,
                    [],
                ).append(registration)
        for name in groups:
            groups[name] = sorted_group(groups[name])

    prize_awards = tournament_prize_awards(db, tournament)

    matches = db.scalars(
        select(Match)
        .options(
            selectinload(Match.home).selectinload(Registration.user),
            selectinload(Match.home).selectinload(Registration.team),
            selectinload(Match.away).selectinload(Registration.user),
            selectinload(Match.away).selectinload(Registration.team),
        )
        .where(Match.tournament_id == tournament.id)
        .order_by(Match.round_order, Match.id)
    ).all()

    accessible_match_ids: set[int] = set()
    if user:
        accessible_match_ids = {
            match.id
            for match in matches
            if (
                registration_contains_user(db, match.home, user)
                or registration_contains_user(db, match.away, user)
            )
        }

    return templates.TemplateResponse(
        "tournament.html",
        ctx(
            request,
            tournament=tournament,
            registrations=registrations,
            user=user,
            teams=teams,
            groups=groups,
            league_table=league_table,
            prize_awards=prize_awards,
            prize_places=len(prize_awards),
            matches=matches,
            accessible_match_ids=accessible_match_ids,
            rules_display=tournament_display_rules(tournament),
        ),
    )

@app.get("/inscricao/{registration_id}/confirmada", response_class=HTMLResponse)
def registration_success(
    registration_id: int,
    request: Request,
    db: Session = Depends(get_db),
):
    user = require_user(request, db)

    registration = db.scalar(
        select(Registration)
        .options(
            selectinload(Registration.tournament),
            selectinload(Registration.user),
            selectinload(Registration.team),
        )
        .where(Registration.id == registration_id)
    )

    if not registration or not registration_contains_user(db, registration, user):
        raise HTTPException(404)

    tournament = registration.tournament
    current_entries = db.scalar(
        select(func.count())
        .select_from(Registration)
        .where(
            Registration.tournament_id == tournament.id,
            Registration.status.in_(["pending", "approved"]),
        )
    ) or 0

    if tournament.quick_duel:
        waiting = [
            item
            for item in db.scalars(
                select(Registration).where(
                    Registration.tournament_id == tournament.id,
                    Registration.status == "approved",
                )
            ).all()
            if not db.scalar(
                select(Match.id).where(
                    (
                        (Match.home_registration_id == item.id)
                        | (Match.away_registration_id == item.id)
                    ),
                    Match.status.in_(
                        ["scheduled", "awaiting_confirmation", "disputed"]
                    ),
                )
            )
        ]
        current_entries = len(waiting)
        remaining_slots = 1 if current_entries % 2 else 0
    else:
        remaining_slots = max(0, tournament.max_entries - current_entries)

    return templates.TemplateResponse(
        "registration_success.html",
        ctx(
            request,
            user=user,
            registration=registration,
            tournament=tournament,
            current_entries=current_entries,
            remaining_slots=remaining_slots,
        ),
    )


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
    if not tournament.quick_duel and total >= tournament.max_entries:
        raise HTTPException(400, "Vagas preenchidas.")

    if user.generation != tournament.generation:
        generation_label = (
            "nova geração"
            if tournament.generation == "nova"
            else "antiga geração"
        )
        raise HTTPException(
            400,
            (
                f"Este campeonato é exclusivo para jogadores da {generation_label}. "
                "Atualize sua plataforma no perfil ou escolha um campeonato compatível."
            ),
        )

    if tournament.mode == "1x1":
        existing = db.scalar(
            select(Registration)
            .where(
                Registration.tournament_id == tournament.id,
                Registration.user_id == user.id,
            )
            .order_by(Registration.id.desc())
        )

        if tournament.quick_duel and existing:
            if existing.status in {"pending", "approved"}:
                return RedirectResponse(f"/campeonato/{slug}", 303)

            registration = existing
            registration.status = "pending"
            registration.payment_status = "pending"
            registration.group_name = None
            registration.points = 0
            registration.played = 0
            registration.wins = 0
            registration.draws = 0
            registration.losses = 0
            registration.goals_for = 0
            registration.goals_against = 0
        else:
            if existing:
                return RedirectResponse(f"/campeonato/{slug}", 303)
            registration = Registration(
                tournament_id=tournament.id,
                user_id=user.id,
            )
    else:
        if not team_id:
            raise HTTPException(400, "Escolha uma equipe.")
        team = db.get(Team, team_id)
        membership = db.scalar(select(TeamMember).where(TeamMember.team_id == team_id, TeamMember.user_id == user.id))
        if not team or not membership or team.mode != tournament.mode:
            raise HTTPException(403)

        team_members = db.scalars(
            select(User)
            .join(TeamMember, TeamMember.user_id == User.id)
            .where(TeamMember.team_id == team.id)
        ).all()

        incompatible_members = [
            member.name
            for member in team_members
            if member.generation != tournament.generation
        ]
        if incompatible_members:
            generation_label = (
                "nova geração"
                if tournament.generation == "nova"
                else "antiga geração"
            )
            names = ", ".join(incompatible_members[:5])
            raise HTTPException(
                400,
                (
                    f"A equipe possui jogador(es) incompatível(is) com a {generation_label}: "
                    f"{names}. Todos os integrantes precisam usar a geração correta."
                ),
            )

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
        return RedirectResponse(
            analytics_location(
                f"/pagamento/{payment.id}",
                "inscricao_campeonato",
                tournament_id=tournament.id,
                tournament_name=tournament.name,
                mode=tournament.mode,
                registration_id=registration.id,
                value=f"{amount:.2f}",
                currency="BRL",
                payment_required="1",
            ),
            303,
        )

    notify_user(
        db,
        user,
        "Inscrição confirmada",
        (
            f"Boa sorte! Você está inscrito em {tournament.name}. "
            "Aguarde o preenchimento das vagas e o início do campeonato."
        ),
    )
    start_quick_duel_if_ready(db, tournament)
    return RedirectResponse(
        analytics_location(
            f"/inscricao/{registration.id}/confirmada",
            "inscricao_campeonato",
            tournament_id=tournament.id,
            tournament_name=tournament.name,
            mode=tournament.mode,
            registration_id=registration.id,
            value="0.00",
            currency="BRL",
            payment_required="0",
        ),
        303,
    )

@app.get("/pagamento/{payment_id}", response_class=HTMLResponse)
def payment_page(payment_id: int, request: Request, db: Session = Depends(get_db)):
    user = require_user(request, db)
    payment = db.scalar(
        select(Payment)
        .options(
            selectinload(Payment.registration).selectinload(Registration.tournament),
            selectinload(Payment.registration).selectinload(Registration.user),
            selectinload(Payment.registration).selectinload(Registration.team),
        )
        .where(Payment.id == payment_id)
    )
    if not payment or not registration_contains_user(db, payment.registration, user):
        raise HTTPException(404)

    registration = payment.registration

    # Repara automaticamente registros antigos que ficaram inconsistentes:
    # inscrição aprovada + pagamento pendente, ou o contrário.
    needs_approval_sync = (
        payment.status == "approved"
        or registration.payment_status == "approved"
    )
    if needs_approval_sync and (
        payment.status != "approved"
        or registration.payment_status != "approved"
        or registration.status != "approved"
    ):
        payment.status = "approved"
        registration.payment_status = "approved"
        registration.status = "approved"
        db.commit()
        db.refresh(payment)
        db.refresh(registration)

    response = templates.TemplateResponse(
        "payment.html",
        ctx(request, payment=payment),
    )
    response.headers["Cache-Control"] = "no-store, no-cache, must-revalidate"
    response.headers["Pragma"] = "no-cache"
    return response

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
    try:
        payment, registration = apply_payment_status(db, payment, status)
    except ValueError as exc:
        db.rollback()
        raise HTTPException(400, str(exc))

    if payment.status == "approved":
        for recipient in registration_users(db, registration):
            notify_user(
                db,
                recipient,
                "Pagamento aprovado",
                f"Seu pagamento da inscrição em {registration.tournament.name} foi aprovado.",
            )

    return {
        "ok": True,
        "payment_status": payment.status,
        "registration_status": registration.status,
    }

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


@app.get("/premiacao/{conversation_id}", response_class=HTMLResponse)
def prize_conversation_page(
    conversation_id: int,
    request: Request,
    db: Session = Depends(get_db),
):
    user = require_user(request, db)
    conversation = db.scalar(
        select(PrizeConversation)
        .options(
            selectinload(PrizeConversation.tournament),
            selectinload(PrizeConversation.registration).selectinload(Registration.user),
            selectinload(PrizeConversation.registration).selectinload(Registration.team),
        )
        .where(PrizeConversation.id == conversation_id)
    )
    if not conversation or not registration_contains_user(
        db, conversation.registration, user
    ):
        raise HTTPException(404)

    messages = db.scalars(
        select(PrizeMessage)
        .options(selectinload(PrizeMessage.user))
        .where(PrizeMessage.conversation_id == conversation.id)
        .order_by(PrizeMessage.created_at, PrizeMessage.id)
    ).all()

    return templates.TemplateResponse(
        "prize_conversation.html",
        ctx(
            request,
            user=user,
            conversation=conversation,
            messages=messages,
            entry_name=entry_name,
            message=request.query_params.get("message"),
        ),
    )


@app.post("/premiacao/{conversation_id}/dados")
def prize_submit_data(
    conversation_id: int,
    request: Request,
    pix_key: Annotated[str, Form()],
    pix_holder_name: Annotated[str, Form()],
    pix_holder_document: Annotated[str, Form()],
    db: Session = Depends(get_db),
):
    user = require_user(request, db)
    conversation = db.get(PrizeConversation, conversation_id)
    if not conversation or not registration_contains_user(
        db, conversation.registration, user
    ):
        raise HTTPException(404)

    key = pix_key.strip()
    holder = pix_holder_name.strip()
    document = re.sub(r"\D", "", pix_holder_document)

    if len(key) < 3 or len(holder) < 3 or len(document) != 11:
        return RedirectResponse(
            f"/premiacao/{conversation.id}?message="
            + quote("Preencha corretamente a chave Pix, o titular e o CPF."),
            303,
        )

    conversation.pix_key = key[:180]
    conversation.pix_holder_name = holder[:180]
    conversation.pix_holder_document = document
    conversation.status = "data_received"
    db.add(PrizeMessage(
        conversation_id=conversation.id,
        user_id=user.id,
        sender_type="winner",
        message=(
            "Dados de recebimento enviados para conferência da administração."
        ),
    ))
    db.commit()

    return RedirectResponse(
        f"/premiacao/{conversation.id}?message="
        + quote("Dados enviados. O pagamento será realizado em até 24 horas."),
        303,
    )


@app.post("/premiacao/{conversation_id}/mensagem")
def prize_winner_message(
    conversation_id: int,
    request: Request,
    message: Annotated[str, Form()],
    db: Session = Depends(get_db),
):
    user = require_user(request, db)
    conversation = db.get(PrizeConversation, conversation_id)
    if not conversation or not registration_contains_user(
        db, conversation.registration, user
    ):
        raise HTTPException(404)

    clean = message.strip()
    if clean:
        db.add(PrizeMessage(
            conversation_id=conversation.id,
            user_id=user.id,
            sender_type="winner",
            message=clean[:1500],
        ))
        db.commit()

    return RedirectResponse(f"/premiacao/{conversation.id}", 303)


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
    if (match.phase == "knockout" or match.tournament.quick_duel) and home_score == away_score:
        raise HTTPException(
            400,
            "Esta disputa precisa ter vencedor. Jogue prorrogação e pênaltis.",
        )
    match.home_score = max(0, home_score)
    match.away_score = max(0, away_score)
    match.result_submitted_by_user_id = user.id

    if match.tournament.require_result_confirmation:
        match.status = "awaiting_confirmation"
        match.result_confirmed = False
        db.commit()
    else:
        match.status = "completed"
        match.result_confirmed = True
        db.commit()
        process_confirmed_result(db, match)

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



@app.get("/admin/notificacoes/pagamentos")
def admin_payment_notifications(
    request: Request,
    db: Session = Depends(get_db),
):
    require_admin(request)

    pending_payments = db.scalars(
        select(Payment)
        .options(
            selectinload(Payment.registration).selectinload(Registration.tournament),
            selectinload(Payment.registration).selectinload(Registration.user),
            selectinload(Payment.registration).selectinload(Registration.team),
        )
        .where(Payment.status == "pending")
        .order_by(Payment.id.desc())
        .limit(10)
    ).all()

    items = []
    for payment in pending_payments:
        registration = payment.registration
        tournament = registration.tournament if registration else None
        participant = registration_label(registration) if registration else "Participante"
        items.append({
            "id": payment.id,
            "participant": participant,
            "tournament": tournament.name if tournament else "Campeonato",
            "amount": float(payment.amount or 0),
        })

    return {
        "count": len(pending_payments),
        "items": items,
    }


@app.get("/admin", response_class=HTMLResponse)
def admin_dashboard(request: Request, db: Session = Depends(get_db)):
    require_admin(request)
    tournaments = db.scalars(select(Tournament).order_by(Tournament.id.desc())).all()
    users = db.scalars(select(User).order_by(User.id.desc())).all()
    registrations = db.scalars(select(Registration).order_by(Registration.id.desc())).all()
    payments = db.scalars(
        select(Payment)
        .options(
            selectinload(Payment.registration).selectinload(Registration.tournament),
            selectinload(Payment.registration).selectinload(Registration.user),
            selectinload(Payment.registration).selectinload(Registration.team),
        )
        .order_by(Payment.id.desc())
        .limit(50)
    ).all()
    reports = db.scalars(select(Report).order_by(Report.id.desc()).limit(20)).all()
    tickets = db.scalars(select(SupportTicket).order_by(SupportTicket.id.desc()).limit(20)).all()
    coupons = db.scalars(select(Coupon).order_by(Coupon.id.desc())).all()
    audits = db.scalars(select(AuditLog).order_by(AuditLog.id.desc()).limit(20)).all()
    prize_conversations = db.scalars(
        select(PrizeConversation)
        .options(
            selectinload(PrizeConversation.tournament),
            selectinload(PrizeConversation.registration).selectinload(Registration.user),
            selectinload(PrizeConversation.registration).selectinload(Registration.team),
        )
        .order_by(PrizeConversation.id.desc())
    ).all()

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
            "can_draw": counts["approved"] >= minimum_entries_for_schedule(tournament),
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
        prize_conversations=prize_conversations,
        stats=stats,
        registration_label=registration_label,
        message=request.query_params.get("message"),
    ))



@app.get("/admin/premiacao/{conversation_id}", response_class=HTMLResponse)
def admin_prize_conversation(
    conversation_id: int,
    request: Request,
    db: Session = Depends(get_db),
):
    require_admin(request)
    conversation = db.scalar(
        select(PrizeConversation)
        .options(
            selectinload(PrizeConversation.tournament),
            selectinload(PrizeConversation.registration).selectinload(Registration.user),
            selectinload(PrizeConversation.registration).selectinload(Registration.team),
        )
        .where(PrizeConversation.id == conversation_id)
    )
    if not conversation:
        raise HTTPException(404)

    messages = db.scalars(
        select(PrizeMessage)
        .options(selectinload(PrizeMessage.user))
        .where(PrizeMessage.conversation_id == conversation.id)
        .order_by(PrizeMessage.created_at, PrizeMessage.id)
    ).all()

    return templates.TemplateResponse(
        "admin_prize_conversation.html",
        ctx(
            request,
            conversation=conversation,
            messages=messages,
            entry_name=entry_name,
            message=request.query_params.get("message"),
        ),
    )


@app.post("/admin/premiacao/{conversation_id}/mensagem")
def admin_prize_message(
    conversation_id: int,
    request: Request,
    message: Annotated[str, Form()],
    db: Session = Depends(get_db),
):
    require_admin(request)
    conversation = db.get(PrizeConversation, conversation_id)
    if not conversation:
        raise HTTPException(404)

    clean = message.strip()
    if clean:
        db.add(PrizeMessage(
            conversation_id=conversation.id,
            user_id=None,
            sender_type="admin",
            message=clean[:1500],
        ))
        for recipient in registration_users(db, conversation.registration):
            notify_user(
                db,
                recipient,
                "Nova mensagem sobre sua premiação",
                f"A administração respondeu sobre {conversation.tournament.name}.",
            )
        db.commit()

    return RedirectResponse(f"/admin/premiacao/{conversation.id}", 303)


@app.post("/admin/premiacao/{conversation_id}/status")
def admin_prize_status(
    conversation_id: int,
    request: Request,
    status: Annotated[str, Form()],
    db: Session = Depends(get_db),
):
    require_admin(request)
    conversation = db.get(PrizeConversation, conversation_id)
    if not conversation:
        raise HTTPException(404)

    allowed = {"awaiting_data", "data_received", "processing", "paid"}
    if status not in allowed:
        raise HTTPException(400, "Status de premiação inválido.")

    conversation.status = status
    if status == "paid":
        conversation.paid_at = datetime.utcnow()
        system_message = (
            f"Pagamento de R$ {float(conversation.amount):.2f} marcado como "
            "realizado pela administração."
        )
        subject = "✅ Premiação paga"
        body = (
            f"A premiação de {conversation.tournament.name} foi marcada como paga. "
            "Confira sua conta Pix."
        )
    else:
        conversation.paid_at = None
        system_message = f"Status da premiação atualizado para: {status}."
        subject = "Atualização da premiação"
        body = (
            f"O status da premiação de {conversation.tournament.name} "
            f"foi atualizado para {status}."
        )

    db.add(PrizeMessage(
        conversation_id=conversation.id,
        user_id=None,
        sender_type="admin",
        message=system_message,
    ))
    for recipient in registration_users(db, conversation.registration):
        notify_user(db, recipient, subject, body)
    db.commit()
    audit(
        db,
        "prize_status",
        "prize_conversation",
        conversation.id,
        status,
    )

    return RedirectResponse(
        f"/admin/premiacao/{conversation.id}?message="
        + quote("Status da premiação atualizado."),
        303,
    )


@app.get("/admin/equipes", response_class=HTMLResponse)
def admin_teams(
    request: Request,
    q: str = "",
    mode: str = "todas",
    db: Session = Depends(get_db),
):
    require_admin(request)

    query = select(Team).options(
        selectinload(Team.captain)
    )

    if mode in {"2x2", "Pro Clubs"}:
        query = query.where(Team.mode == mode)

    teams = db.scalars(query.order_by(Team.id.desc())).all()
    term = q.strip().lower()

    if term:
        teams = [
            team for team in teams
            if term in team.name.lower()
            or term in team.invite_code.lower()
            or (
                team.captain
                and term in team.captain.name.lower()
            )
        ]

    member_totals = {
        team.id: db.scalar(
            select(func.count())
            .select_from(TeamMember)
            .where(TeamMember.team_id == team.id)
        )
        for team in teams
    }

    registration_totals = {
        team.id: db.scalar(
            select(func.count())
            .select_from(Registration)
            .where(Registration.team_id == team.id)
        )
        for team in teams
    }

    return templates.TemplateResponse(
        "admin_teams.html",
        ctx(
            request,
            teams=teams,
            member_totals=member_totals,
            registration_totals=registration_totals,
            q=q,
            mode=mode,
            message=request.query_params.get("message"),
        ),
    )


@app.post("/admin/equipe/{team_id}/status")
def admin_team_status(
    team_id: int,
    request: Request,
    action: Annotated[str, Form()],
    db: Session = Depends(get_db),
):
    require_admin(request)
    team = db.get(Team, team_id)

    if not team:
        return admin_redirect(
            "Equipe não encontrada.",
            "/admin/equipes",
        )

    if action not in {"activate", "deactivate"}:
        return admin_redirect(
            "Ação inválida.",
            "/admin/equipes",
        )

    team.active = action == "activate"
    db.commit()
    audit(db, "status", "team", team.id, action)

    return admin_redirect(
        "Status da equipe atualizado.",
        "/admin/equipes",
    )


@app.post("/admin/equipe/{team_id}/excluir")
def admin_delete_team(
    team_id: int,
    request: Request,
    db: Session = Depends(get_db),
):
    require_admin(request)

    team = db.scalar(
        select(Team)
        .options(selectinload(Team.captain))
        .where(Team.id == team_id)
    )
    if not team:
        return admin_redirect(
            "Equipe não encontrada.",
            "/admin/equipes",
        )

    registration_ids = list(db.scalars(
        select(Registration.id).where(
            Registration.team_id == team.id
        )
    ).all())

    if registration_ids:
        linked_match = db.scalar(
            select(Match.id).where(or_(
                Match.home_registration_id.in_(registration_ids),
                Match.away_registration_id.in_(registration_ids),
                Match.winner_registration_id.in_(registration_ids),
            ))
        )
        if linked_match:
            return admin_redirect(
                (
                    f"A equipe {team.name} possui partida vinculada. "
                    "Limpe a tabela/sorteio do campeonato antes de excluir."
                ),
                "/admin/equipes",
            )

    label = team.name

    try:
        if registration_ids:
            team_conversations = db.scalars(
                select(PrizeConversation).where(
                    PrizeConversation.registration_id.in_(registration_ids)
                )
            ).all()
            for conversation in team_conversations:
                db.query(PrizeMessage).filter(
                    PrizeMessage.conversation_id == conversation.id
                ).delete(synchronize_session=False)
                db.delete(conversation)

            db.query(Payment).filter(
                Payment.registration_id.in_(registration_ids)
            ).delete(synchronize_session=False)

            db.query(Registration).filter(
                Registration.id.in_(registration_ids)
            ).delete(synchronize_session=False)

        db.query(TeamMember).filter(
            TeamMember.team_id == team.id
        ).delete(synchronize_session=False)

        db.delete(team)
        db.commit()

    except Exception as exc:
        db.rollback()
        return admin_redirect(
            f"Não foi possível excluir a equipe: {exc}",
            "/admin/equipes",
        )

    audit(
        db,
        "delete",
        "team",
        team_id,
        f"Equipe excluída: {label}",
    )

    return admin_redirect(
        f"Equipe {label} excluída com sucesso.",
        "/admin/equipes",
    )


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



@app.post("/admin/jogador/{user_id}/excluir")
def admin_delete_player(
    user_id: int,
    request: Request,
    db: Session = Depends(get_db),
):
    require_admin(request)

    user = db.get(User, user_id)
    if not user:
        return admin_redirect(
            "Jogador não encontrado.",
            "/admin/jogadores",
        )

    captained_teams = db.scalars(
        select(Team).where(Team.captain_id == user.id)
    ).all()

    if captained_teams:
        names = ", ".join(team.name for team in captained_teams)
        return admin_redirect(
            (
                f"Este jogador é capitão da(s) equipe(s): {names}. "
                "Exclua ou transfira essas equipes antes de apagar o jogador."
            ),
            "/admin/jogadores",
        )

    registration_ids = list(db.scalars(
        select(Registration.id).where(
            Registration.user_id == user.id
        )
    ).all())

    if registration_ids:
        linked_match = db.scalar(
            select(Match.id).where(or_(
                Match.home_registration_id.in_(registration_ids),
                Match.away_registration_id.in_(registration_ids),
                Match.winner_registration_id.in_(registration_ids),
            ))
        )
        if linked_match:
            return admin_redirect(
                (
                    f"O jogador {user.name} possui partida vinculada. "
                    "Limpe a tabela/sorteio do campeonato antes de excluir."
                ),
                "/admin/jogadores",
            )

    evidence_files = db.scalars(
        select(Evidence.file_path).where(
            Evidence.uploaded_by == user.id
        )
    ).all()

    label = user.name

    try:
        # Remove dados diretamente ligados à conta.
        db.query(MatchMessage).filter(
            MatchMessage.user_id == user.id
        ).delete(synchronize_session=False)

        db.query(Evidence).filter(
            Evidence.uploaded_by == user.id
        ).delete(synchronize_session=False)

        db.query(Report).filter(
            Report.reporter_id == user.id
        ).delete(synchronize_session=False)

        db.query(Notification).filter(
            Notification.user_id == user.id
        ).delete(synchronize_session=False)

        db.query(PasswordReset).filter(
            PasswordReset.user_id == user.id
        ).delete(synchronize_session=False)

        db.query(SupportTicket).filter(
            SupportTicket.user_id == user.id
        ).delete(synchronize_session=False)

        db.query(UserAchievement).filter(
            UserAchievement.user_id == user.id
        ).delete(synchronize_session=False)

        db.query(TeamMember).filter(
            TeamMember.user_id == user.id
        ).delete(synchronize_session=False)

        # Mantém partidas de terceiros, apenas remove a referência opcional.
        db.query(Match).filter(
            Match.result_submitted_by_user_id == user.id
        ).update(
            {Match.result_submitted_by_user_id: None},
            synchronize_session=False,
        )

        if registration_ids:
            conversation_ids = list(db.scalars(
                select(PrizeConversation.id).where(
                    PrizeConversation.registration_id.in_(registration_ids)
                )
            ).all())

            if conversation_ids:
                # Primeiro remove todas as mensagens vinculadas às premiações.
                db.query(PrizeMessage).filter(
                    PrizeMessage.conversation_id.in_(conversation_ids)
                ).delete(synchronize_session=False)

                # Depois remove as próprias conversas em uma operação direta.
                # Isso garante que o PostgreSQL execute o DELETE antes das inscrições.
                db.query(PrizeConversation).filter(
                    PrizeConversation.id.in_(conversation_ids)
                ).delete(synchronize_session=False)

            db.query(Payment).filter(
                Payment.registration_id.in_(registration_ids)
            ).delete(synchronize_session=False)

            # Força a execução das exclusões dependentes antes de apagar inscrições.
            db.flush()

            db.query(Registration).filter(
                Registration.id.in_(registration_ids)
            ).delete(synchronize_session=False)

        db.query(PrizeMessage).filter(
            PrizeMessage.user_id == user.id
        ).delete(synchronize_session=False)

        db.delete(user)
        db.commit()

    except Exception as exc:
        db.rollback()
        return admin_redirect(
            f"Não foi possível excluir o jogador: {exc}",
            "/admin/jogadores",
        )

    # Apaga arquivos físicos somente depois da exclusão no banco.
    for stored_path in evidence_files:
        file_path = Path(stored_path)
        if not file_path.is_absolute():
            file_path = UPLOAD_DIR / file_path
        try:
            if file_path.exists() and file_path.is_file():
                file_path.unlink()
        except OSError:
            pass

    audit(
        db,
        "delete",
        "user",
        user_id,
        f"Jogador excluído: {label}",
    )

    return admin_redirect(
        f"Jogador {label} excluído com sucesso.",
        "/admin/jogadores",
    )

@app.get("/admin/selecionar-campeonato")
def admin_select_tournament(
    request: Request,
    tournament_id: int,
    db: Session = Depends(get_db),
):
    require_admin(request)
    tournament = db.get(Tournament, tournament_id)
    if not tournament:
        return admin_redirect("Campeonato não encontrado.")
    return RedirectResponse(f"/admin/campeonato/{tournament.id}", 303)


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
        .options(
            selectinload(Registration.user),
            selectinload(Registration.team),
        )
        .where(Registration.tournament_id == tournament.id)
        .order_by(Registration.id)
    ).all()

    matches = db.scalars(
        select(Match)
        .options(
            selectinload(Match.home).selectinload(Registration.user),
            selectinload(Match.home).selectinload(Registration.team),
            selectinload(Match.away).selectinload(Registration.user),
            selectinload(Match.away).selectinload(Registration.team),
        )
        .where(Match.tournament_id == tournament.id)
        .order_by(Match.round_order, Match.group_name, Match.id)
    ).all()

    users = db.scalars(
        select(User)
        .where(User.active == True, User.generation == tournament.generation)
        .order_by(User.name)
    ).all()

    registered_user_ids = {
        item.user_id for item in registrations if item.user_id is not None
    }
    available_users = [
        user for user in users if user.id not in registered_user_ids
    ]

    counts = tournament_registration_counts(db, tournament.id)
    prize_places, prize_distribution_value = tournament_prize_form(
        db,
        tournament,
    )
    prize_awards = tournament_prize_awards(db, tournament)
    all_tournaments = db.scalars(
        select(Tournament).order_by(Tournament.name)
    ).all()

    return templates.TemplateResponse("admin_tournament.html", ctx(
        request,
        tournament=tournament,
        all_tournaments=all_tournaments,
        registrations=registrations,
        matches=matches,
        available_users=available_users,
        counts=counts,
        prize_places=prize_places,
        prize_distribution_value=prize_distribution_value,
        prize_awards=prize_awards,
        can_draw=counts["approved"] >= minimum_entries_for_schedule(tournament),
        registration_label=registration_label,
        registration_meta=registration_meta,
        registration_contact=registration_contact,
        registration_ea_id=registration_ea_id,
        message=request.query_params.get("message"),
    ))

@app.post("/admin/campeonato/{tournament_id}/editar")
def admin_edit_tournament(
    tournament_id: int,
    request: Request,
    name: Annotated[str, Form()],
    mode: Annotated[str, Form()],
    competition_format: Annotated[str, Form()] = "groups_knockout",
    league_turns: Annotated[int, Form()] = 2,
    quick_duel: Annotated[str | None, Form()] = None,
    duel_series: Annotated[int, Form()] = 1,
    match_minutes: Annotated[int, Form()] = 5,
    squad_type: Annotated[str, Form()] = "online",
    allow_classic_teams: Annotated[str | None, Form()] = None,
    allow_national_teams: Annotated[str | None, Form()] = None,
    knockout_extra_time: Annotated[str | None, Form()] = None,
    require_result_confirmation: Annotated[str | None, Form()] = None,
    generation: Annotated[str, Form()] = "nova",
    max_entries: Annotated[int, Form()] = 32,
    group_size: Annotated[int, Form()] = 4,
    registration_fee: Annotated[float, Form()] = 0,
    prize: Annotated[float, Form()] = 0,
    prize_places: Annotated[int, Form()] = 1,
    prize_distribution: Annotated[str, Form()] = "",
    starts_at: Annotated[str, Form()] = "A definir",
    color_theme: Annotated[str, Form()] = "green",
    rules: Annotated[str, Form()] = "",
    status: Annotated[str, Form()] = "open",
    db: Session = Depends(get_db),
):
    require_admin(request)
    tournament = db.get(Tournament, tournament_id)
    if not tournament:
        raise HTTPException(404)

    is_quick_duel = quick_duel == "1"
    if is_quick_duel:
        mode = "1x1"
        competition_format = "league"
        league_turns = 1
        max_entries = 2
        group_size = 2
        prize_places = 1
        if duel_series not in {1, 3}:
            return admin_redirect(
                "Escolha partida única ou melhor de 3.",
                f"/admin/campeonato/{tournament_id}",
            )
    else:
        duel_series = 1

    if match_minutes not in {3, 4, 5, 6, 7, 8, 9, 10}:
        return admin_redirect(
            "Escolha uma duração entre 3 e 10 minutos por tempo.",
            f"/admin/campeonato/{tournament_id}",
        )
    if squad_type not in {"online", "default", "custom"}:
        return admin_redirect(
            "Tipo de elenco inválido.",
            f"/admin/campeonato/{tournament_id}",
        )

    if mode not in {"1x1", "2x2", "Pro Clubs"}:
        return admin_redirect(
            "Modalidade inválida.",
            f"/admin/campeonato/{tournament_id}",
        )
    if competition_format not in {"groups_knockout", "league"}:
        return admin_redirect(
            "Formato inválido.",
            f"/admin/campeonato/{tournament_id}",
        )
    if generation not in PLATFORMS:
        return admin_redirect(
            "Geração inválida.",
            f"/admin/campeonato/{tournament_id}",
        )
    if max_entries < 2:
        return admin_redirect(
            "O campeonato precisa de pelo menos 2 vagas.",
            f"/admin/campeonato/{tournament_id}",
        )
    if prize_places > max_entries:
        return admin_redirect(
            "A quantidade de premiados não pode ser maior que o número de vagas.",
            f"/admin/campeonato/{tournament_id}",
        )
    if competition_format == "groups_knockout":
        if group_size < 2:
            return admin_redirect(
                "O grupo precisa ter pelo menos 2 participantes.",
                f"/admin/campeonato/{tournament_id}",
            )
        if max_entries < group_size:
            return admin_redirect(
                "As vagas não podem ser menores que o tamanho do grupo.",
                f"/admin/campeonato/{tournament_id}",
            )
    if league_turns not in {1, 2}:
        return admin_redirect(
            "Escolha 1 ou 2 turnos.",
            f"/admin/campeonato/{tournament_id}",
        )

    try:
        prize_amounts = parse_prize_distribution(
            prize_places,
            prize_distribution,
            prize,
        )
    except ValueError as exc:
        return admin_redirect(
            str(exc),
            f"/admin/campeonato/{tournament_id}",
        )

    format_changed = (
        tournament.competition_format != competition_format
        or tournament.league_turns != league_turns
    )
    has_matches = bool(db.scalar(
        select(Match.id).where(Match.tournament_id == tournament.id)
    ))
    if format_changed and has_matches:
        return admin_redirect(
            (
                "Limpe o sorteio/tabela antes de alterar o formato "
                "ou a quantidade de turnos."
            ),
            f"/admin/campeonato/{tournament_id}",
        )

    if not is_quick_duel:
        rules = normal_tournament_rules(rules)

    tournament.name = name.strip()
    tournament.mode = mode
    tournament.competition_format = competition_format
    tournament.league_turns = league_turns
    tournament.quick_duel = is_quick_duel
    tournament.duel_series = duel_series
    tournament.match_minutes = match_minutes
    tournament.squad_type = squad_type
    tournament.allow_classic_teams = allow_classic_teams == "1"
    tournament.allow_national_teams = allow_national_teams == "1"
    tournament.knockout_extra_time = knockout_extra_time == "1"
    tournament.require_result_confirmation = require_result_confirmation == "1"
    tournament.generation = generation
    tournament.max_entries = max_entries
    tournament.group_size = group_size
    tournament.registration_fee = max(0, registration_fee)
    tournament.prize = sum(prize_amounts, Decimal("0.00"))
    tournament.starts_at = starts_at.strip() or "A definir"
    tournament.color_theme = color_theme
    tournament.rules = rules.strip()
    tournament.status = status

    try:
        save_tournament_prizes(db, tournament, prize_amounts)
        db.commit()
    except Exception as exc:
        db.rollback()
        return admin_redirect(
            f"Não foi possível salvar: {exc}",
            f"/admin/campeonato/{tournament_id}",
        )

    audit(
        db,
        "edit",
        "tournament",
        tournament.id,
        (
            f"{tournament.name}; format={competition_format}; "
            f"league_turns={league_turns}; quick_duel={is_quick_duel}; "
            f"duel_series={duel_series}; prize_places={prize_places}"
        ),
    )
    return admin_redirect(
        (
            f"Campeonato atualizado com {prize_places} "
            "colocado(s) premiado(s)."
        ),
        f"/admin/campeonato/{tournament_id}",
    )
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

    registration = db.scalar(
        select(Registration)
        .options(
            selectinload(Registration.tournament),
            selectinload(Registration.user),
            selectinload(Registration.team),
        )
        .where(
            Registration.id == registration_id,
            Registration.tournament_id == tournament_id,
        )
    )
    if not registration:
        return admin_redirect(
            "Inscrição não encontrada.",
            f"/admin/campeonato/{tournament_id}",
        )

    payments = db.scalars(
        select(Payment).where(Payment.registration_id == registration.id)
    ).all()
    paid_tournament = float(registration.tournament.registration_fee) > 0

    try:
        if action == "approve":
            registration.status = "approved"

            if paid_tournament:
                registration.payment_status = "approved"

                # Atualiza todos os pagamentos existentes.
                for payment in payments:
                    payment.status = "approved"

                # Garante um registro de pagamento mesmo em cadastros antigos.
                if not payments:
                    payment = Payment(
                        registration_id=registration.id,
                        amount=registration.tournament.registration_fee,
                        status="approved",
                    )
                    db.add(payment)
            else:
                registration.payment_status = "not_required"

        elif action == "pending":
            registration.status = "pending"

            if paid_tournament:
                registration.payment_status = "pending"
                for payment in payments:
                    payment.status = "pending"
            else:
                registration.payment_status = "not_required"

        elif action == "reject":
            registration.status = "rejected"

            if paid_tournament:
                registration.payment_status = "rejected"
                for payment in payments:
                    payment.status = "rejected"
            else:
                registration.payment_status = "not_required"

        elif action == "cancel":
            registration.status = "cancelled"
            registration.group_name = None

        elif action == "restore":
            if paid_tournament:
                has_approved_payment = any(
                    payment.status == "approved" for payment in payments
                )
                if has_approved_payment:
                    registration.status = "approved"
                    registration.payment_status = "approved"
                else:
                    registration.status = "pending"
                    registration.payment_status = "pending"
            else:
                registration.status = "approved"
                registration.payment_status = "not_required"

        else:
            return admin_redirect(
                "Ação inválida.",
                f"/admin/campeonato/{tournament_id}",
            )

        db.commit()

    except Exception as exc:
        db.rollback()
        return admin_redirect(
            f"Não foi possível atualizar a inscrição: {exc}",
            f"/admin/campeonato/{tournament_id}",
        )

    if action == "approve":
        for recipient in registration_users(db, registration):
            notify_user(
                db,
                recipient,
                "Inscrição confirmada",
                (
                    f"Sua inscrição em {registration.tournament.name} "
                    f"foi aprovada e o pagamento foi confirmado."
                    if paid_tournament
                    else
                    f"Sua inscrição em {registration.tournament.name} foi aprovada."
                ),
            )

    audit(
        db,
        "registration_status",
        "registration",
        registration.id,
        (
            f"action={action}; status={registration.status}; "
            f"payment_status={registration.payment_status}"
        ),
    )

    return admin_redirect(
        (
            f"Inscrição atualizada: {registration.status}. "
            f"Pagamento: {registration.payment_status}."
        ),
        f"/admin/campeonato/{tournament_id}",
    )
@app.post("/admin/campeonato/{tournament_id}/inscricao/{registration_id}/remover")
def admin_remove_registration(
    tournament_id: int,
    registration_id: int,
    request: Request,
    db: Session = Depends(get_db),
):
    require_admin(request)

    registration = db.scalar(
        select(Registration)
        .options(
            selectinload(Registration.user),
            selectinload(Registration.team),
        )
        .where(
            Registration.id == registration_id,
            Registration.tournament_id == tournament_id,
        )
    )

    if not registration:
        return admin_redirect(
            "Inscrição não encontrada.",
            f"/admin/campeonato/{tournament_id}",
        )

    linked_match = db.scalar(
        select(Match.id).where(or_(
            Match.home_registration_id == registration.id,
            Match.away_registration_id == registration.id,
            Match.winner_registration_id == registration.id,
        ))
    )

    if linked_match:
        return admin_redirect(
            (
                "Esta inscrição já possui partida vinculada. "
                "Use Cancelar ou limpe o sorteio antes de excluir."
            ),
            f"/admin/campeonato/{tournament_id}",
        )

    label = registration_label(registration)

    try:
        # O PostgreSQL não permite apagar a inscrição enquanto existir
        # um pagamento apontando para ela.
        deleted_payments = (
            db.query(Payment)
            .filter(Payment.registration_id == registration.id)
            .delete(synchronize_session=False)
        )

        db.delete(registration)
        db.commit()
    except Exception as exc:
        db.rollback()
        return admin_redirect(
            f"Não foi possível excluir a inscrição: {exc}",
            f"/admin/campeonato/{tournament_id}",
        )

    audit(
        db,
        "delete_registration",
        "tournament",
        tournament_id,
        (
            f"{label}; registration_id={registration_id}; "
            f"payments_deleted={deleted_payments}"
        ),
    )

    return admin_redirect(
        (
            f"Inscrição de {label} excluída com sucesso. "
            f"{deleted_payments} pagamento(s) vinculado(s) também foram removidos."
        ),
        f"/admin/campeonato/{tournament_id}",
    )
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

    try:
        clear_tournament_matches(db, tournament.id)
        tournament.status = "open"
        db.commit()
    except Exception as exc:
        db.rollback()
        return admin_redirect(
            f"Não foi possível limpar o sorteio: {exc}",
            f"/admin/campeonato/{tournament_id}",
        )

    audit(db, "clear_draw", "tournament", tournament.id, tournament.name)
    return admin_redirect(
        "Sorteio, Salas PVP, grupos e partidas apagados. Os inscritos foram preservados.",
        f"/admin/campeonato/{tournament_id}",
    )

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
    competition_format: Annotated[str, Form()] = "groups_knockout",
    league_turns: Annotated[int, Form()] = 2,
    quick_duel: Annotated[str | None, Form()] = None,
    duel_series: Annotated[int, Form()] = 1,
    match_minutes: Annotated[int, Form()] = 5,
    squad_type: Annotated[str, Form()] = "online",
    allow_classic_teams: Annotated[str | None, Form()] = None,
    allow_national_teams: Annotated[str | None, Form()] = None,
    knockout_extra_time: Annotated[str | None, Form()] = None,
    require_result_confirmation: Annotated[str | None, Form()] = None,
    generation: Annotated[str, Form()] = "nova",
    max_entries: Annotated[int, Form()] = 32,
    group_size: Annotated[int, Form()] = 4,
    registration_fee: Annotated[float, Form()] = 0,
    prize: Annotated[float, Form()] = 0,
    prize_places: Annotated[int, Form()] = 1,
    prize_distribution: Annotated[str, Form()] = "",
    starts_at: Annotated[str, Form()] = "A definir",
    color_theme: Annotated[str, Form()] = "green",
    rules: Annotated[str, Form()] = "",
    db: Session = Depends(get_db),
):
    require_admin(request)

    is_quick_duel = quick_duel == "1"
    if is_quick_duel:
        mode = "1x1"
        competition_format = "league"
        league_turns = 1
        max_entries = 2
        group_size = 2
        prize_places = 1
        if duel_series not in {1, 3}:
            return admin_redirect("Escolha partida única ou melhor de 3.")
    else:
        duel_series = 1

    if match_minutes not in {3, 4, 5, 6, 7, 8, 9, 10}:
        return admin_redirect("Escolha uma duração entre 3 e 10 minutos por tempo.")
    if squad_type not in {"online", "default", "custom"}:
        return admin_redirect("Tipo de elenco inválido.")

    if not name.strip():
        return admin_redirect("Informe o nome do campeonato.")
    if mode not in {"1x1", "2x2", "Pro Clubs"}:
        return admin_redirect("Modalidade inválida.")
    if competition_format not in {"groups_knockout", "league"}:
        return admin_redirect("Formato do campeonato inválido.")
    if generation not in PLATFORMS:
        return admin_redirect("Geração inválida.")
    if max_entries < 2:
        return admin_redirect("O campeonato precisa de pelo menos 2 vagas.")
    if prize_places > max_entries:
        return admin_redirect(
            "A quantidade de premiados não pode ser maior que o número de vagas."
        )
    if competition_format == "groups_knockout":
        if group_size < 2:
            return admin_redirect("O grupo precisa ter pelo menos 2 participantes.")
        if max_entries < group_size:
            return admin_redirect(
                "As vagas não podem ser menores que o tamanho do grupo."
            )
    if league_turns not in {1, 2}:
        return admin_redirect("Escolha 1 ou 2 turnos para os pontos corridos.")

    try:
        prize_amounts = parse_prize_distribution(
            prize_places,
            prize_distribution,
            prize,
        )
    except ValueError as exc:
        return admin_redirect(str(exc))

    season = db.scalar(
        select(Season).where(Season.active == True).limit(1)
    )

    if not is_quick_duel:
        rules = normal_tournament_rules(rules)

    tournament = Tournament(
        season_id=season.id if season else None,
        name=name.strip(),
        slug=unique_slug(db, name),
        mode=mode,
        competition_format=competition_format,
        league_turns=league_turns,
        quick_duel=is_quick_duel,
        duel_series=duel_series,
        match_minutes=match_minutes,
        squad_type=squad_type,
        allow_classic_teams=allow_classic_teams == "1",
        allow_national_teams=allow_national_teams == "1",
        knockout_extra_time=knockout_extra_time == "1",
        require_result_confirmation=require_result_confirmation == "1",
        generation=generation,
        max_entries=max_entries,
        group_size=group_size,
        registration_fee=max(0, registration_fee),
        prize=sum(prize_amounts, Decimal("0.00")),
        status="open",
        starts_at=starts_at.strip() or "A definir",
        color_theme=color_theme,
        rules=rules.strip(),
    )

    try:
        db.add(tournament)
        db.flush()
        save_tournament_prizes(db, tournament, prize_amounts)
        db.commit()
        db.refresh(tournament)
    except Exception as exc:
        db.rollback()
        return admin_redirect(f"Não foi possível criar o campeonato: {exc}")

    audit(
        db,
        "create",
        "tournament",
        tournament.id,
        (
            f"{tournament.name}; format={competition_format}; "
            f"league_turns={league_turns}; quick_duel={is_quick_duel}; "
            f"duel_series={duel_series}; prize_places={prize_places}"
        ),
    )
    return admin_redirect(
        f"Campeonato criado com {prize_places} colocado(s) premiado(s)."
    )
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
def admin_draw_groups(
    tournament_id: int,
    request: Request,
    db: Session = Depends(get_db),
):
    require_admin(request)
    tournament = db.get(Tournament, tournament_id)
    if not tournament:
        raise HTTPException(404)

    approved = db.scalars(
        select(Registration).where(
            Registration.tournament_id == tournament.id,
            Registration.status == "approved",
        )
    ).all()

    minimum = minimum_entries_for_schedule(tournament)
    if len(approved) < minimum:
        return admin_redirect(
            (
                f"Não foi possível gerar: há {len(approved)} inscrição(ões) "
                f"aprovada(s), mas são necessárias pelo menos {minimum}."
            ),
            f"/admin/campeonato/{tournament_id}",
        )

    try:
        if tournament.competition_format == "league":
            league = generate_league(db, tournament)
            total_matches = db.scalar(
                select(func.count())
                .select_from(Match)
                .where(Match.tournament_id == tournament.id)
            )
            message = (
                f"Tabela de pontos corridos gerada: "
                f"{league['participants']} participantes, "
                f"{league['rounds']} rodadas, {league['turns']} turno(s) "
                f"e {total_matches} partidas."
            )
            audit_action = "generate_league"
        else:
            groups = generate_groups(db, tournament)
            total_matches = db.scalar(
                select(func.count())
                .select_from(Match)
                .where(Match.tournament_id == tournament.id)
            )
            message = (
                f"Sorteio concluído: {len(groups)} grupo(s) "
                f"e {total_matches} partida(s) gerada(s)."
            )
            audit_action = "draw_groups"

    except Exception as exc:
        db.rollback()
        message = f"Falha ao gerar campeonato: {exc}"
        audit_action = "generation_error"

    audit(db, audit_action, "tournament", tournament_id, message)
    return admin_redirect(message, f"/admin/campeonato/{tournament_id}")
@app.post("/admin/pagamento/{payment_id}")
def admin_payment(
    payment_id: int,
    request: Request,
    status: Annotated[str, Form()],
    db: Session = Depends(get_db),
):
    require_admin(request)

    payment = db.scalar(
        select(Payment)
        .options(
            selectinload(Payment.registration).selectinload(Registration.tournament),
            selectinload(Payment.registration).selectinload(Registration.user),
            selectinload(Payment.registration).selectinload(Registration.team),
        )
        .where(Payment.id == payment_id)
    )
    if not payment:
        return admin_redirect("Pagamento não encontrado.", "/admin#pagamentos")

    try:
        payment, registration = apply_payment_status(db, payment, status)
    except Exception as exc:
        db.rollback()
        return admin_redirect(
            f"Não foi possível atualizar o pagamento: {exc}",
            "/admin#pagamentos",
        )

    if payment.status == "approved":
        subject = "Pagamento aprovado"
        body = (
            f"Seu pagamento da inscrição em "
            f"{registration.tournament.name} foi aprovado. "
            f"Sua inscrição está confirmada."
        )
    elif payment.status == "rejected":
        subject = "Pagamento recusado"
        body = (
            f"O pagamento da inscrição em "
            f"{registration.tournament.name} foi recusado. "
            f"Entre em contato com o suporte se precisar."
        )
    else:
        subject = "Pagamento pendente"
        body = (
            f"O pagamento da inscrição em "
            f"{registration.tournament.name} voltou para análise."
        )

    for recipient in registration_users(db, registration):
        notify_user(db, recipient, subject, body)

    audit(
        db,
        "payment",
        "payment",
        payment.id,
        (
            f"payment={payment.status}; "
            f"registration={registration.status}; "
            f"registration_id={registration.id}"
        ),
    )

    admin_destination = "/admin#pagamentos"
    if payment.status == "approved":
        admin_destination = analytics_location(
            admin_destination,
            "pagamento_aprovado",
            payment_id=payment.id,
            registration_id=registration.id,
            tournament_id=registration.tournament_id,
            tournament_name=registration.tournament.name,
            value=f"{float(payment.amount or 0):.2f}",
            currency="BRL",
        )

    return admin_redirect(
        (
            f"Pagamento #{payment.id} atualizado para {payment.status}. "
            f"Inscrição #{registration.id}: {registration.status}."
        ),
        admin_destination,
    )

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
