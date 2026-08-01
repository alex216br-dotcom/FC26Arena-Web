import random
from itertools import combinations
from sqlalchemy import select
from sqlalchemy.orm import Session
from .models import Match, Player, Setting
LABELS={'nova':'Nova geração','antiga':'Antiga geração'}

def get_setting(db:Session):
    s=db.get(Setting,1)
    if not s:
        s=Setting(id=1);db.add(s);db.commit();db.refresh(s)
    return s

def ranking_query(db:Session,division:str):
    ps=db.scalars(select(Player).where(Player.active==True,Player.approved==True,Player.division==division)).all()
    return sorted(ps,key=lambda p:(p.points,p.goals_for-p.goals_against,p.goals_for,p.wins),reverse=True)

def recalculate(db:Session):
    ps=db.scalars(select(Player)).all(); mp={p.id:p for p in ps}
    for p in ps:
        p.points=p.played=p.wins=p.draws=p.losses=p.goals_for=p.goals_against=0;p.xp=p.titles*500
    for m in db.scalars(select(Match).where(Match.status=='finalizada',Match.result_confirmed==True)).all():
        if m.score1 is None or m.score2 is None: continue
        a,b=mp.get(m.player1_id),mp.get(m.player2_id)
        if not a or not b: continue
        a.played+=1;b.played+=1;a.goals_for+=m.score1;a.goals_against+=m.score2;b.goals_for+=m.score2;b.goals_against+=m.score1
        if m.score1>m.score2:a.wins+=1;b.losses+=1;a.points+=3;a.xp+=100;b.xp+=30
        elif m.score2>m.score1:b.wins+=1;a.losses+=1;b.points+=3;b.xp+=100;a.xp+=30
        else:a.draws+=1;b.draws+=1;a.points+=1;b.points+=1;a.xp+=60;b.xp+=60
    for p in ps:p.level=max(1,p.xp//500+1)
    db.commit()

def draw_groups(db:Session,season:int,division:str):
    ps=ranking_query(db,division)
    if len(ps)<4: raise ValueError('São necessários pelo menos 4 jogadores.')
    random.shuffle(ps);db.query(Match).filter(Match.season==season,Match.division==division).delete()
    groups=[ps[i:i+4] for i in range(0,len(ps),4)]
    for i,g in enumerate(groups):
        letter='ABCDEFGHIJKLMNOPQRSTUVWXYZ'[i]
        for p in g:p.group_name=letter
        for a,b in combinations(g,2):db.add(Match(season=season,division=division,group_name=letter,player1_id=a.id,player2_id=b.id))
    s=get_setting(db);s.phase='grupos';s.registrations_open=False;db.commit();return groups

def generate_knockout(db:Session,season:int,division:str):
    if db.scalars(select(Match).where(Match.season==season,Match.division==division,Match.phase=='mata-mata')).all():raise ValueError('Mata-mata já gerado.')
    ps=ranking_query(db,division);groups=sorted({p.group_name for p in ps if p.group_name});q=[]
    for g in groups:q.extend([p for p in ps if p.group_name==g][:2])
    if len(q)<4 or len(q)%2:raise ValueError('Classificados insuficientes.')
    pairs=list(zip(q[:len(q)//2],reversed(q[len(q)//2:])))
    rn='Oitavas de final' if len(pairs)==8 else 'Quartas de final' if len(pairs)==4 else 'Semifinal'
    for a,b in pairs:db.add(Match(season=season,division=division,phase='mata-mata',round_name=rn,player1_id=a.id,player2_id=b.id))
    get_setting(db).phase='mata-mata';db.commit();return len(pairs)
