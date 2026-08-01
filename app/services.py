import random
from itertools import combinations
from sqlalchemy import select
from sqlalchemy.orm import Session
from .models import Match, Player, Setting

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
    for m in db.scalars(select(Match).where(Match.status == "finalizada")).all():
        if m.score1 is None or m.score2 is None:
            continue
        p1, p2 = player_map[m.player1_id], player_map[m.player2_id]
        p1.played += 1; p2.played += 1
        p1.goals_for += m.score1; p1.goals_against += m.score2
        p2.goals_for += m.score2; p2.goals_against += m.score1
        if m.score1 > m.score2:
            p1.wins += 1; p2.losses += 1; p1.points += 3
        elif m.score2 > m.score1:
            p2.wins += 1; p1.losses += 1; p2.points += 3
        else:
            p1.draws += 1; p2.draws += 1; p1.points += 1; p2.points += 1
    db.commit()

def draw_groups(db: Session, season: int):
    players = db.scalars(select(Player).where(Player.active == True)).all()
    if len(players) < 4:
        raise ValueError("São necessários pelo menos 4 jogadores.")
    random.shuffle(players)
    db.query(Match).filter(Match.season == season).delete()
    letters = "ABCDEFGH"
    groups = [players[i:i+4] for i in range(0, len(players), 4)]
    for idx, group in enumerate(groups):
        letter = letters[idx]
        for p in group:
            p.group_name = letter
        for p1, p2 in combinations(group, 2):
            db.add(Match(season=season, group_name=letter, player1_id=p1.id, player2_id=p2.id))
    setting = get_setting(db)
    setting.phase = "grupos"
    setting.registrations_open = False
    db.commit()
    return len(groups)
