from datetime import datetime
from sqlalchemy import Boolean, DateTime, ForeignKey, Integer, Numeric, String, Text, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column, relationship
from .database import Base

class User(Base):
    __tablename__ = "users"
    id: Mapped[int] = mapped_column(primary_key=True)
    name: Mapped[str] = mapped_column(String(100))
    email: Mapped[str | None] = mapped_column(String(150), unique=True, nullable=True)
    whatsapp: Mapped[str] = mapped_column(String(30), unique=True)
    telegram_chat_id: Mapped[str | None] = mapped_column(String(50), nullable=True)
    ea_id: Mapped[str] = mapped_column(String(100), unique=True)
    platform: Mapped[str] = mapped_column(String(40))
    generation: Mapped[str] = mapped_column(String(20), default="nova")
    password_hash: Mapped[str] = mapped_column(String(255))
    active: Mapped[bool] = mapped_column(Boolean, default=True)
    notify_email: Mapped[bool] = mapped_column(Boolean, default=True)
    notify_telegram: Mapped[bool] = mapped_column(Boolean, default=False)
    notify_whatsapp: Mapped[bool] = mapped_column(Boolean, default=True)
    xp: Mapped[int] = mapped_column(Integer, default=0)
    level: Mapped[int] = mapped_column(Integer, default=1)
    played: Mapped[int] = mapped_column(Integer, default=0)
    wins: Mapped[int] = mapped_column(Integer, default=0)
    draws: Mapped[int] = mapped_column(Integer, default=0)
    losses: Mapped[int] = mapped_column(Integer, default=0)
    goals_for: Mapped[int] = mapped_column(Integer, default=0)
    goals_against: Mapped[int] = mapped_column(Integer, default=0)
    titles: Mapped[int] = mapped_column(Integer, default=0)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)

class AdminUser(Base):
    __tablename__ = "admin_users"
    id: Mapped[int] = mapped_column(primary_key=True)
    username: Mapped[str] = mapped_column(String(60), unique=True)
    name: Mapped[str] = mapped_column(String(100))
    role: Mapped[str] = mapped_column(String(30), default="moderator")
    password_hash: Mapped[str] = mapped_column(String(255))
    active: Mapped[bool] = mapped_column(Boolean, default=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)

class Season(Base):
    __tablename__ = "seasons"
    id: Mapped[int] = mapped_column(primary_key=True)
    name: Mapped[str] = mapped_column(String(100))
    active: Mapped[bool] = mapped_column(Boolean, default=True)
    starts_at: Mapped[str] = mapped_column(String(40), default="A definir")
    ends_at: Mapped[str] = mapped_column(String(40), default="A definir")

class Tournament(Base):
    __tablename__ = "tournaments"
    id: Mapped[int] = mapped_column(primary_key=True)
    season_id: Mapped[int | None] = mapped_column(ForeignKey("seasons.id"), nullable=True)
    name: Mapped[str] = mapped_column(String(120))
    slug: Mapped[str] = mapped_column(String(120), unique=True)
    mode: Mapped[str] = mapped_column(String(20), default="1x1")
    competition_format: Mapped[str] = mapped_column(
        String(30), default="groups_knockout"
    )
    league_turns: Mapped[int] = mapped_column(Integer, default=2)
    quick_duel: Mapped[bool] = mapped_column(Boolean, default=False)
    duel_series: Mapped[int] = mapped_column(Integer, default=1)
    match_minutes: Mapped[int] = mapped_column(Integer, default=5)
    squad_type: Mapped[str] = mapped_column(String(30), default="online")
    allow_classic_teams: Mapped[bool] = mapped_column(Boolean, default=True)
    allow_national_teams: Mapped[bool] = mapped_column(Boolean, default=True)
    knockout_extra_time: Mapped[bool] = mapped_column(Boolean, default=True)
    require_result_confirmation: Mapped[bool] = mapped_column(Boolean, default=True)
    generation: Mapped[str] = mapped_column(String(20), default="nova")
    max_entries: Mapped[int] = mapped_column(Integer, default=32)
    group_size: Mapped[int] = mapped_column(Integer, default=4)
    registration_fee: Mapped[float] = mapped_column(Numeric(10,2), default=0)
    prize: Mapped[float] = mapped_column(Numeric(10,2), default=0)
    status: Mapped[str] = mapped_column(String(30), default="draft")
    starts_at: Mapped[str] = mapped_column(String(40), default="A definir")
    rules: Mapped[str] = mapped_column(Text, default="")
    color_theme: Mapped[str] = mapped_column(String(20), default="green")
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)

class TournamentPrize(Base):
    __tablename__ = "tournament_prizes"
    __table_args__ = (
        UniqueConstraint("tournament_id", "place", name="uq_tournament_prize_place"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    tournament_id: Mapped[int] = mapped_column(
        ForeignKey("tournaments.id"), index=True
    )
    place: Mapped[int] = mapped_column(Integer)
    amount: Mapped[float] = mapped_column(Numeric(10, 2), default=0)
    tournament = relationship("Tournament")


class Team(Base):
    __tablename__ = "teams"
    id: Mapped[int] = mapped_column(primary_key=True)
    name: Mapped[str] = mapped_column(String(120), unique=True)
    mode: Mapped[str] = mapped_column(String(20), default="2x2")
    captain_id: Mapped[int] = mapped_column(ForeignKey("users.id"))
    invite_code: Mapped[str] = mapped_column(String(32), unique=True)
    active: Mapped[bool] = mapped_column(Boolean, default=True)
    xp: Mapped[int] = mapped_column(Integer, default=0)
    titles: Mapped[int] = mapped_column(Integer, default=0)
    captain = relationship("User")

class TeamMember(Base):
    __tablename__ = "team_members"
    id: Mapped[int] = mapped_column(primary_key=True)
    team_id: Mapped[int] = mapped_column(ForeignKey("teams.id"))
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id"))
    role: Mapped[str] = mapped_column(String(30), default="player")
    team = relationship("Team")
    user = relationship("User")
    __table_args__ = (UniqueConstraint("team_id", "user_id", name="uq_team_member"),)

class Registration(Base):
    __tablename__ = "registrations"
    id: Mapped[int] = mapped_column(primary_key=True)
    tournament_id: Mapped[int] = mapped_column(ForeignKey("tournaments.id"))
    user_id: Mapped[int | None] = mapped_column(ForeignKey("users.id"), nullable=True)
    team_id: Mapped[int | None] = mapped_column(ForeignKey("teams.id"), nullable=True)
    status: Mapped[str] = mapped_column(String(30), default="pending")
    payment_status: Mapped[str] = mapped_column(String(30), default="not_required")
    group_name: Mapped[str | None] = mapped_column(String(5), nullable=True)
    points: Mapped[int] = mapped_column(Integer, default=0)
    played: Mapped[int] = mapped_column(Integer, default=0)
    wins: Mapped[int] = mapped_column(Integer, default=0)
    draws: Mapped[int] = mapped_column(Integer, default=0)
    losses: Mapped[int] = mapped_column(Integer, default=0)
    goals_for: Mapped[int] = mapped_column(Integer, default=0)
    goals_against: Mapped[int] = mapped_column(Integer, default=0)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)
    tournament = relationship("Tournament")
    user = relationship("User")
    team = relationship("Team")
    __table_args__ = (
        UniqueConstraint("tournament_id", "user_id", name="uq_tournament_user"),
        UniqueConstraint("tournament_id", "team_id", name="uq_tournament_team"),
    )

class Match(Base):
    __tablename__ = "matches"
    id: Mapped[int] = mapped_column(primary_key=True)
    tournament_id: Mapped[int] = mapped_column(ForeignKey("tournaments.id"))
    phase: Mapped[str] = mapped_column(String(30), default="group")
    round_name: Mapped[str] = mapped_column(String(40), default="Fase de grupos")
    round_order: Mapped[int] = mapped_column(Integer, default=1)
    group_name: Mapped[str | None] = mapped_column(String(5), nullable=True)
    home_registration_id: Mapped[int] = mapped_column(ForeignKey("registrations.id"))
    away_registration_id: Mapped[int] = mapped_column(ForeignKey("registrations.id"))
    home_score: Mapped[int | None] = mapped_column(Integer, nullable=True)
    away_score: Mapped[int | None] = mapped_column(Integer, nullable=True)
    status: Mapped[str] = mapped_column(String(30), default="scheduled")
    scheduled_for: Mapped[str] = mapped_column(String(40), default="A definir")
    result_submitted_by_user_id: Mapped[int | None] = mapped_column(ForeignKey("users.id"), nullable=True)
    result_confirmed: Mapped[bool] = mapped_column(Boolean, default=False)
    winner_registration_id: Mapped[int | None] = mapped_column(ForeignKey("registrations.id"), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)
    tournament = relationship("Tournament")
    home = relationship("Registration", foreign_keys=[home_registration_id])
    away = relationship("Registration", foreign_keys=[away_registration_id])

class MatchMessage(Base):
    __tablename__ = "match_messages"
    id: Mapped[int] = mapped_column(primary_key=True)
    match_id: Mapped[int] = mapped_column(ForeignKey("matches.id"))
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id"))
    message: Mapped[str] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)
    user = relationship("User")

class Evidence(Base):
    __tablename__ = "evidences"
    id: Mapped[int] = mapped_column(primary_key=True)
    match_id: Mapped[int] = mapped_column(ForeignKey("matches.id"))
    uploaded_by: Mapped[int] = mapped_column(ForeignKey("users.id"))
    file_path: Mapped[str] = mapped_column(String(255))
    original_name: Mapped[str] = mapped_column(String(255))
    description: Mapped[str] = mapped_column(Text, default="")
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)
    user = relationship("User")

class Report(Base):
    __tablename__ = "reports"
    id: Mapped[int] = mapped_column(primary_key=True)
    match_id: Mapped[int] = mapped_column(ForeignKey("matches.id"))
    reporter_id: Mapped[int] = mapped_column(ForeignKey("users.id"))
    report_type: Mapped[str] = mapped_column(String(30), default="problema")
    description: Mapped[str] = mapped_column(Text)
    status: Mapped[str] = mapped_column(String(30), default="open")
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)
    reporter = relationship("User")


class PrizeConversation(Base):
    __tablename__ = "prize_conversations"

    id: Mapped[int] = mapped_column(primary_key=True)
    tournament_id: Mapped[int] = mapped_column(
        ForeignKey("tournaments.id"), index=True
    )
    registration_id: Mapped[int] = mapped_column(
        ForeignKey("registrations.id"), index=True
    )
    match_id: Mapped[int | None] = mapped_column(
        ForeignKey("matches.id"), nullable=True, index=True
    )
    amount: Mapped[float] = mapped_column(Numeric(10, 2), default=0)
    status: Mapped[str] = mapped_column(String(30), default="awaiting_data")
    pix_key: Mapped[str] = mapped_column(String(180), default="")
    pix_holder_name: Mapped[str] = mapped_column(String(180), default="")
    pix_holder_document: Mapped[str] = mapped_column(String(40), default="")
    paid_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime, default=datetime.utcnow, onupdate=datetime.utcnow
    )

    tournament = relationship("Tournament")
    registration = relationship("Registration")
    match = relationship("Match")


class PrizeMessage(Base):
    __tablename__ = "prize_messages"

    id: Mapped[int] = mapped_column(primary_key=True)
    conversation_id: Mapped[int] = mapped_column(
        ForeignKey("prize_conversations.id"), index=True
    )
    user_id: Mapped[int | None] = mapped_column(
        ForeignKey("users.id"), nullable=True
    )
    sender_type: Mapped[str] = mapped_column(String(20), default="winner")
    message: Mapped[str] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)

    user = relationship("User")
    conversation = relationship("PrizeConversation")



class Achievement(Base):
    __tablename__ = "achievements"
    id: Mapped[int] = mapped_column(primary_key=True)
    code: Mapped[str] = mapped_column(String(50), unique=True)
    name: Mapped[str] = mapped_column(String(100))
    description: Mapped[str] = mapped_column(Text)
    icon: Mapped[str] = mapped_column(String(20), default="🏅")
    xp_reward: Mapped[int] = mapped_column(Integer, default=0)

class UserAchievement(Base):
    __tablename__ = "user_achievements"
    id: Mapped[int] = mapped_column(primary_key=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id"))
    achievement_id: Mapped[int] = mapped_column(ForeignKey("achievements.id"))
    awarded_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)
    achievement = relationship("Achievement")
    __table_args__ = (UniqueConstraint("user_id", "achievement_id", name="uq_user_achievement"),)

class Coupon(Base):
    __tablename__ = "coupons"
    id: Mapped[int] = mapped_column(primary_key=True)
    code: Mapped[str] = mapped_column(String(50), unique=True)
    discount_percent: Mapped[int] = mapped_column(Integer, default=0)
    active: Mapped[bool] = mapped_column(Boolean, default=True)
    max_uses: Mapped[int] = mapped_column(Integer, default=0)
    uses: Mapped[int] = mapped_column(Integer, default=0)

class Payment(Base):
    __tablename__ = "payments"
    id: Mapped[int] = mapped_column(primary_key=True)
    registration_id: Mapped[int] = mapped_column(ForeignKey("registrations.id"))
    provider: Mapped[str] = mapped_column(String(40), default="manual_pix")
    external_id: Mapped[str | None] = mapped_column(String(120), nullable=True)
    amount: Mapped[float] = mapped_column(Numeric(10,2), default=0)
    status: Mapped[str] = mapped_column(String(30), default="pending")
    coupon_code: Mapped[str | None] = mapped_column(String(50), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)
    registration = relationship("Registration")

class Notification(Base):
    __tablename__ = "notifications"
    id: Mapped[int] = mapped_column(primary_key=True)
    user_id: Mapped[int | None] = mapped_column(ForeignKey("users.id"), nullable=True)
    channel: Mapped[str] = mapped_column(String(30), default="site")
    subject: Mapped[str] = mapped_column(String(150))
    body: Mapped[str] = mapped_column(Text)
    status: Mapped[str] = mapped_column(String(30), default="pending")
    read: Mapped[bool] = mapped_column(Boolean, default=False)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)

class PasswordReset(Base):
    __tablename__ = "password_resets"
    id: Mapped[int] = mapped_column(primary_key=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id"))
    token: Mapped[str] = mapped_column(String(150), unique=True)
    used: Mapped[bool] = mapped_column(Boolean, default=False)
    expires_at: Mapped[datetime] = mapped_column(DateTime)

class SupportTicket(Base):
    __tablename__ = "support_tickets"
    id: Mapped[int] = mapped_column(primary_key=True)
    user_id: Mapped[int | None] = mapped_column(ForeignKey("users.id"), nullable=True)
    subject: Mapped[str] = mapped_column(String(150))
    message: Mapped[str] = mapped_column(Text)
    status: Mapped[str] = mapped_column(String(30), default="open")
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)
    user = relationship("User")

class AuditLog(Base):
    __tablename__ = "audit_logs"
    id: Mapped[int] = mapped_column(primary_key=True)
    admin_label: Mapped[str] = mapped_column(String(100), default="owner")
    action: Mapped[str] = mapped_column(String(100))
    entity: Mapped[str] = mapped_column(String(100))
    entity_id: Mapped[int | None] = mapped_column(Integer, nullable=True)
    details: Mapped[str] = mapped_column(Text, default="")
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)
