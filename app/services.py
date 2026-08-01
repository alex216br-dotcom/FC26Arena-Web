import random
from itertools import combinations
from sqlalchemy import select
from sqlalchemy.orm import Session
from .models import Match, Player, Setting

DIVISION_LABELS = {"nova": "Nova geração", "antiga": "Antiga geração"}

def get_setting(db: Session) -> Setting:
    setting = db.get(Setting, 1)
    if not setting:
        setting = Setting(id=1)
        db.add(setting)
        db.commit()
        db.refresh(setting)
    return setting

def recalculate(db: Session):
    players = db.scalars(select(Player)).all()
    for p in players:
        p.points = p.played = p.wins = p.draws = p.losses = 0
        p.goals_for = p.goals_against = 0

    player_map = {p.id: p for p in players}
    matches = db.scalars(select(Match).where(Match.status == "finalizada")).all()
    for m in matches:
        if m.score1 is None or m.score2 is None:
            continue
        p1 = player_map.get(m.player1_id)
        p2 = player_map.get(m.player2_id)
        if not p1 or not p2:
            continue
        p1.played += 1
        p2.played += 1
        p1.goals_for += m.score1
        p1.goals_against += m.score2
        p2.goals_for += m.score2
        p2.goals_against += m.score1
        if m.score1 > m.score2:
            p1.wins += 1
            p2.losses += 1
            p1.points += 3
        elif m.score2 > m.score1:
            p2.wins += 1
            p1.losses += 1
            p2.points += 3
        else:
            p1.draws += 1
            p2.draws += 1
            p1.points += 1
            p2.points += 1
    db.commit()

def ranking_query(db: Session, division: str):
    players = db.scalars(
        select(Player).where(
            Player.active == True,
            Player.approved == True,
            Player.division == division,
        )
    ).all()
    return sorted(
        players,
        key=lambda p: (
            p.points,
            p.goals_for - p.goals_against,
            p.goals_for,
            p.wins,
        ),
        reverse=True,
    )

def draw_groups(db: Session, season: int, division: str, group_size: int = 4):
    players = ranking_query(db, division)
    if len(players) < group_size:
        raise ValueError(f"São necessários pelo menos {group_size} jogadores na {DIVISION_LABELS[division]}.")
    random.shuffle(players)
    db.query(Match).filter(
        Match.season == season,
        Match.division == division,
    ).delete()
    letters = "ABCDEFGHIJKLMNOPQRSTUVWXYZ"
    groups = [players[i:i+group_size] for i in range(0, len(players), group_size)]
    for idx, group in enumerate(groups):
        letter = letters[idx]
        for p in group:
            p.group_name = letter
        for p1, p2 in combinations(group, 2):
            db.add(Match(
                season=season,
                division=division,
                phase="grupos",
                round_name="Fase de grupos",
                group_name=letter,
                player1_id=p1.id,
                player2_id=p2.id,
            ))
    setting = get_setting(db)
    setting.phase = "grupos"
    db.commit()
    return groups

def generate_knockout(db: Session, season: int, division: str):
    existing = db.scalars(
        select(Match).where(
            Match.season == season,
            Match.division == division,
            Match.phase == "mata-mata",
        )
    ).all()
    if existing:
        raise ValueError("O mata-mata desta divisão já foi gerado.")

    players = ranking_query(db, division)
    groups = sorted({p.group_name for p in players if p.group_name})
    qualified = []
    for group in groups:
        group_players = [p for p in players if p.group_name == group][:2]
        qualified.extend(group_players)

    if len(qualified) < 4 or len(qualified) % 2:
        raise ValueError("É necessário concluir a fase de grupos e ter número par de classificados.")

    pairs = []
    half = len(qualified) // 2
    left = qualified[:half]
    right = list(reversed(qualified[half:]))
    for p1, p2 in zip(left, right):
        pairs.append((p1, p2))

    round_name = "Oitavas de final" if len(pairs) == 8 else "Quartas de final" if len(pairs) == 4 else "Semifinal"
    for p1, p2 in pairs:
        db.add(Match(
            season=season,
            division=division,
            phase="mata-mata",
            round_name=round_name,
            player1_id=p1.id,
            player2_id=p2.id,
        ))
    get_setting(db).phase = "mata-mata"
    db.commit()
    return len(pairs)
