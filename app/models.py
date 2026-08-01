from datetime import datetime
from sqlalchemy import Boolean, DateTime, ForeignKey, Integer, String, Text
from sqlalchemy.orm import Mapped, mapped_column, relationship
from .database import Base

class Setting(Base):
    __tablename__='settings'
    id: Mapped[int]=mapped_column(primary_key=True,default=1)
    registrations_open: Mapped[bool]=mapped_column(Boolean,default=True)
    season: Mapped[int]=mapped_column(Integer,default=1)
    phase: Mapped[str]=mapped_column(String(30),default='inscricoes')
    championship_date: Mapped[str]=mapped_column(String(40),default='A definir')
    announcement: Mapped[str]=mapped_column(Text,default='Ao completar as vagas, divulgaremos dia e horário.')

class Player(Base):
    __tablename__='players'
    id: Mapped[int]=mapped_column(primary_key=True)
    telegram_id: Mapped[int|None]=mapped_column(Integer,unique=True,nullable=True)
    name: Mapped[str]=mapped_column(String(80))
    whatsapp: Mapped[str]=mapped_column(String(30),unique=True)
    ea_id: Mapped[str]=mapped_column(String(80),unique=True)
    platform: Mapped[str]=mapped_column(String(30))
    division: Mapped[str]=mapped_column(String(20),default='nova')
    password_hash: Mapped[str]=mapped_column(String(255))
    active: Mapped[bool]=mapped_column(Boolean,default=True)
    approved: Mapped[bool]=mapped_column(Boolean,default=True)
    group_name: Mapped[str|None]=mapped_column(String(2),nullable=True)
    points: Mapped[int]=mapped_column(Integer,default=0)
    played: Mapped[int]=mapped_column(Integer,default=0)
    wins: Mapped[int]=mapped_column(Integer,default=0)
    draws: Mapped[int]=mapped_column(Integer,default=0)
    losses: Mapped[int]=mapped_column(Integer,default=0)
    goals_for: Mapped[int]=mapped_column(Integer,default=0)
    goals_against: Mapped[int]=mapped_column(Integer,default=0)
    titles: Mapped[int]=mapped_column(Integer,default=0)
    xp: Mapped[int]=mapped_column(Integer,default=0)
    level: Mapped[int]=mapped_column(Integer,default=1)
    created_at: Mapped[datetime]=mapped_column(DateTime,default=datetime.utcnow)

class Match(Base):
    __tablename__='matches'
    id: Mapped[int]=mapped_column(primary_key=True)
    season: Mapped[int]=mapped_column(Integer,default=1)
    phase: Mapped[str]=mapped_column(String(30),default='grupos')
    group_name: Mapped[str|None]=mapped_column(String(2),nullable=True)
    division: Mapped[str]=mapped_column(String(20),default='nova')
    round_name: Mapped[str]=mapped_column(String(30),default='Fase de grupos')
    player1_id: Mapped[int]=mapped_column(ForeignKey('players.id'))
    player2_id: Mapped[int]=mapped_column(ForeignKey('players.id'))
    score1: Mapped[int|None]=mapped_column(Integer,nullable=True)
    score2: Mapped[int|None]=mapped_column(Integer,nullable=True)
    status: Mapped[str]=mapped_column(String(30),default='agendada')
    scheduled_for: Mapped[str]=mapped_column(String(40),default='A definir')
    result_submitted_by: Mapped[int|None]=mapped_column(Integer,nullable=True)
    result_confirmed: Mapped[bool]=mapped_column(Boolean,default=False)
    player1=relationship('Player',foreign_keys=[player1_id])
    player2=relationship('Player',foreign_keys=[player2_id])

class MatchMessage(Base):
    __tablename__='match_messages'
    id: Mapped[int]=mapped_column(primary_key=True)
    match_id: Mapped[int]=mapped_column(ForeignKey('matches.id'))
    player_id: Mapped[int]=mapped_column(ForeignKey('players.id'))
    message: Mapped[str]=mapped_column(Text)
    created_at: Mapped[datetime]=mapped_column(DateTime,default=datetime.utcnow)
    player=relationship('Player')

class Report(Base):
    __tablename__='reports'
    id: Mapped[int]=mapped_column(primary_key=True)
    match_id: Mapped[int]=mapped_column(ForeignKey('matches.id'))
    reporter_id: Mapped[int]=mapped_column(ForeignKey('players.id'))
    type: Mapped[str]=mapped_column(String(30),default='problema')
    description: Mapped[str]=mapped_column(Text)
    status: Mapped[str]=mapped_column(String(30),default='aberta')
    created_at: Mapped[datetime]=mapped_column(DateTime,default=datetime.utcnow)
    reporter=relationship('Player')
