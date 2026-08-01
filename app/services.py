import math
import random
import re
from itertools import combinations
from sqlalchemy import select
from sqlalchemy.orm import Session
from .models import (
    Achievement, AuditLog, Match, Registration, TeamMember, Tournament,
    User, UserAchievement
)

def slugify(value: str) -> str:
    value = value.lower().strip()
    value = re.sub(r"[^a-z0-9]+", "-", value)
    return value.strip("-") or "torneio"

def unique_slug(db: Session, name: str) -> str:
    base = slugify(name)
    slug = base
    index = 2
    while db.scalar(select(Tournament).where(Tournament.slug == slug)):
        slug = f"{base}-{index}"
        index += 1
    return slug

def audit(db: Session, action: str, entity: str, entity_id: int | None = None, details: str = ""):
    db.add(AuditLog(action=action, entity=entity, entity_id=entity_id, details=details))
    db.commit()

def entry_name(registration: Registration) -> str:
    if registration.team:
        return registration.team.name
    if registration.user:
        return registration.user.name
    return f"Inscrição #{registration.id}"

def registration_users(db: Session, registration: Registration) -> list[User]:
    if registration.user:
        return [registration.user]
    if registration.team:
        members = db.scalars(
            select(TeamMember).where(TeamMember.team_id == registration.team_id)
        ).all()
        return [member.user for member in members]
    return []

def award_achievement(db: Session, user: User, code: str):
    achievement = db.scalar(select(Achievement).where(Achievement.code == code))
    if not achievement:
        return
    existing = db.scalar(select(UserAchievement).where(
        UserAchievement.user_id == user.id,
        UserAchievement.achievement_id == achievement.id,
    ))
    if not existing:
        db.add(UserAchievement(user_id=user.id, achievement_id=achievement.id))
        user.xp += achievement.xp_reward
        db.commit()

def seed_achievements(db: Session):
    defaults = [
        ("WELCOME", "Primeiros passos", "Criou a conta no FC26 Arena.", "🌟", 50),
        ("FIRST_MATCH", "Estreante", "Disputou a primeira partida.", "🎮", 50),
        ("FIRST_WIN", "Primeira vitória", "Venceu sua primeira partida.", "🏆", 100),
        ("TEN_MATCHES", "Veterano", "Disputou 10 partidas.", "🛡️", 200),
        ("CHAMPION", "Campeão", "Conquistou um campeonato.", "👑", 500),
    ]
    for code, name, description, icon, xp in defaults:
        if not db.scalar(select(Achievement).where(Achievement.code == code)):
            db.add(Achievement(code=code, name=name, description=description, icon=icon, xp_reward=xp))
    db.commit()

def reset_registration_stats(db: Session, tournament_id: int):
    registrations = db.scalars(select(Registration).where(Registration.tournament_id == tournament_id)).all()
    for r in registrations:
        r.points = r.played = r.wins = r.draws = r.losses = 0
        r.goals_for = r.goals_against = 0

    matches = db.scalars(select(Match).where(
        Match.tournament_id == tournament_id,
        Match.phase == "group",
        Match.status == "completed",
        Match.result_confirmed == True,
    )).all()
    reg_map = {r.id: r for r in registrations}
    for match in matches:
        home = reg_map.get(match.home_registration_id)
        away = reg_map.get(match.away_registration_id)
        if not home or not away or match.home_score is None or match.away_score is None:
            continue
        home.played += 1
        away.played += 1
        home.goals_for += match.home_score
        home.goals_against += match.away_score
        away.goals_for += match.away_score
        away.goals_against += match.home_score
        if match.home_score > match.away_score:
            home.wins += 1
            away.losses += 1
            home.points += 3
        elif match.away_score > match.home_score:
            away.wins += 1
            home.losses += 1
            away.points += 3
        else:
            home.draws += 1
            away.draws += 1
            home.points += 1
            away.points += 1
    db.commit()

def sorted_group(registrations: list[Registration]) -> list[Registration]:
    return sorted(
        registrations,
        key=lambda r: (r.points, r.goals_for-r.goals_against, r.goals_for, r.wins),
        reverse=True,
    )

def generate_groups(db: Session, tournament: Tournament):
    if tournament.group_size < 2:
        raise ValueError("O tamanho do grupo precisa ser pelo menos 2.")

    approved = db.scalars(select(Registration).where(
        Registration.tournament_id == tournament.id,
        Registration.status == "approved",
    )).all()
    if len(approved) < tournament.group_size:
        raise ValueError(
            f"São necessárias pelo menos {tournament.group_size} inscrições aprovadas; atualmente há {len(approved)}."
        )

    # Recria o sorteio sem apagar inscrições.
    db.query(Match).filter(Match.tournament_id == tournament.id).delete()
    for registration in approved:
        registration.group_name = None
        registration.points = registration.played = registration.wins = 0
        registration.draws = registration.losses = 0
        registration.goals_for = registration.goals_against = 0

    random.shuffle(approved)
    letters = "ABCDEFGHIJKLMNOPQRSTUVWXYZ"
    groups = [
        approved[index:index + tournament.group_size]
        for index in range(0, len(approved), tournament.group_size)
    ]

    # Não permite um último grupo com apenas uma pessoa.
    if len(groups) > 1 and len(groups[-1]) == 1:
        moved = groups[-2].pop()
        groups[-1].insert(0, moved)

    for index, group in enumerate(groups):
        group_name = letters[index]
        for registration in group:
            registration.group_name = group_name
        for home, away in combinations(group, 2):
            db.add(Match(
                tournament_id=tournament.id,
                phase="group",
                round_name="Fase de grupos",
                round_order=1,
                group_name=group_name,
                home_registration_id=home.id,
                away_registration_id=away.id,
            ))

    tournament.status = "group_stage"
    db.commit()
    return groups


def groups_finished(db: Session, tournament_id: int) -> bool:
    matches = db.scalars(select(Match).where(
        Match.tournament_id == tournament_id,
        Match.phase == "group",
    )).all()
    return bool(matches) and all(m.status == "completed" and m.result_confirmed for m in matches)

def generate_knockout(db: Session, tournament: Tournament):
    existing = db.scalar(select(Match).where(
        Match.tournament_id == tournament.id,
        Match.phase == "knockout",
    ))
    if existing:
        return
    reset_registration_stats(db, tournament.id)
    registrations = db.scalars(select(Registration).where(
        Registration.tournament_id == tournament.id,
        Registration.status == "approved",
    )).all()
    group_names = sorted({r.group_name for r in registrations if r.group_name})
    qualified: list[Registration] = []
    for group_name in group_names:
        group = sorted_group([r for r in registrations if r.group_name == group_name])
        qualified.extend(group[:2])
    if len(qualified) < 2:
        raise ValueError("Não há classificados suficientes.")
    while len(qualified) & (len(qualified)-1):
        qualified.pop()
    pairs = []
    half = len(qualified)//2
    for home, away in zip(qualified[:half], reversed(qualified[half:])):
        pairs.append((home, away))
    round_name = {16:"Oitavas de final",8:"Quartas de final",4:"Semifinal",2:"Final"}.get(len(qualified), "Mata-mata")
    for home, away in pairs:
        db.add(Match(
            tournament_id=tournament.id,
            phase="knockout",
            round_name=round_name,
            round_order=2,
            home_registration_id=home.id,
            away_registration_id=away.id,
        ))
    tournament.status = "knockout"
    db.commit()

def recalculate_global_stats(db: Session):
    users = db.scalars(select(User)).all()
    for user in users:
        user.played = user.wins = user.draws = user.losses = 0
        user.goals_for = user.goals_against = 0
        user.xp = user.titles * 500

    completed = db.scalars(select(Match).where(
        Match.status == "completed",
        Match.result_confirmed == True,
    )).all()
    for match in completed:
        home_users = registration_users(db, match.home)
        away_users = registration_users(db, match.away)
        for user in home_users + away_users:
            user.played += 1
            user.xp += 25
        for user in home_users:
            user.goals_for += match.home_score or 0
            user.goals_against += match.away_score or 0
        for user in away_users:
            user.goals_for += match.away_score or 0
            user.goals_against += match.home_score or 0
        if match.home_score > match.away_score:
            for user in home_users:
                user.wins += 1
                user.xp += 75
            for user in away_users:
                user.losses += 1
        elif match.away_score > match.home_score:
            for user in away_users:
                user.wins += 1
                user.xp += 75
            for user in home_users:
                user.losses += 1
        else:
            for user in home_users + away_users:
                user.draws += 1
                user.xp += 25

    for user in users:
        user.level = max(1, user.xp // 500 + 1)
        if user.played >= 1:
            award_achievement(db, user, "FIRST_MATCH")
        if user.wins >= 1:
            award_achievement(db, user, "FIRST_WIN")
        if user.played >= 10:
            award_achievement(db, user, "TEN_MATCHES")
    db.commit()

def advance_knockout(db: Session, tournament: Tournament, current_match: Match):
    if current_match.phase != "knockout":
        if groups_finished(db, tournament.id):
            generate_knockout(db, tournament)
        return

    round_matches = db.scalars(select(Match).where(
        Match.tournament_id == tournament.id,
        Match.phase == "knockout",
        Match.round_order == current_match.round_order,
    )).all()
    if not all(m.status == "completed" and m.result_confirmed for m in round_matches):
        return

    winners = [db.get(Registration, m.winner_registration_id) for m in round_matches]
    winners = [w for w in winners if w]
    if len(winners) == 1:
        tournament.status = "completed"
        champion = winners[0]
        for user in registration_users(db, champion):
            user.titles += 1
            award_achievement(db, user, "CHAMPION")
        if champion.team:
            champion.team.titles += 1
        db.commit()
        return

    next_order = current_match.round_order + 1
    if db.scalar(select(Match).where(
        Match.tournament_id == tournament.id,
        Match.phase == "knockout",
        Match.round_order == next_order,
    )):
        return

    round_name = {8:"Quartas de final",4:"Semifinal",2:"Final"}.get(len(winners), "Mata-mata")
    for i in range(0, len(winners), 2):
        db.add(Match(
            tournament_id=tournament.id,
            phase="knockout",
            round_name=round_name,
            round_order=next_order,
            home_registration_id=winners[i].id,
            away_registration_id=winners[i+1].id,
        ))
    db.commit()

def process_confirmed_result(db: Session, match: Match):
    if match.home_score is None or match.away_score is None:
        return
    match.winner_registration_id = (
        match.home_registration_id if match.home_score > match.away_score
        else match.away_registration_id if match.away_score > match.home_score
        else None
    )
    reset_registration_stats(db, match.tournament_id)
    recalculate_global_stats(db)
    advance_knockout(db, match.tournament, match)
