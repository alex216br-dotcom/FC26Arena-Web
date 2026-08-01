import os
from pathlib import Path
from typing import Annotated
from fastapi import Depends,FastAPI,Form,HTTPException,Request
from fastapi.responses import HTMLResponse,RedirectResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from sqlalchemy import func,or_,select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session
from starlette.middleware.sessions import SessionMiddleware
from .database import Base,engine,get_db
from .models import Match,MatchMessage,Player,Report
from .security import hash_password,verify_password
from .services import draw_groups,generate_knockout,get_setting,ranking_query,recalculate
BASE=Path(__file__).resolve().parent
app=FastAPI(title='FC26 Arena');app.add_middleware(SessionMiddleware,secret_key=os.getenv('SECRET_KEY','troque'));app.mount('/static',StaticFiles(directory=BASE/'static'),name='static');templates=Jinja2Templates(directory=BASE/'templates')
MAX_PLAYERS=int(os.getenv('MAX_PLAYERS','32'));GROUP_URL=os.getenv('GROUP_URL','#');ADMIN_USER=os.getenv('ADMIN_USERNAME','admin');ADMIN_PASS=os.getenv('ADMIN_PASSWORD','admin123')
DIVISIONS={'nova':{'label':'Nova geração','platforms':['PlayStation 5','Xbox Series X|S','PC'],'prize':os.getenv('CHAMPION_PRIZE_NEW','50')},'antiga':{'label':'Antiga geração','platforms':['PlayStation 4','Xbox One'],'prize':os.getenv('CHAMPION_PRIZE_OLD','50')}}
@app.on_event('startup')
def startup():
 Base.metadata.create_all(bind=engine);db=next(get_db());get_setting(db);db.close()
def ctx(request,**kw):return {'request':request,'max_players':MAX_PLAYERS,'group_url':GROUP_URL,'divisions':DIVISIONS,**kw}
def admin_ok(r):
 if not r.session.get('admin'):raise HTTPException(403)
def player_ok(r,db):
 p=db.get(Player,r.session.get('player_id')) if r.session.get('player_id') else None
 if not p:raise HTTPException(403)
 return p
@app.get('/',response_class=HTMLResponse)
def home(request:Request,db:Session=Depends(get_db)):
 s=get_setting(db);ps=db.scalars(select(Player).where(Player.active==True,Player.approved==True)).all();counts={k:sum(1 for p in ps if p.division==k) for k in DIVISIONS};recent=sorted(ps,key=lambda p:p.created_at,reverse=True)[:8];last=db.scalars(select(Match).where(Match.status=='finalizada',Match.result_confirmed==True).order_by(Match.id.desc()).limit(6)).all();return templates.TemplateResponse('home.html',ctx(request,setting=s,counts=counts,remaining={k:max(0,MAX_PLAYERS-counts[k]) for k in DIVISIONS},recent=recent,last_matches=last))
@app.get('/inscricao',response_class=HTMLResponse)
def regpage(request:Request,db:Session=Depends(get_db)):return templates.TemplateResponse('register.html',ctx(request,setting=get_setting(db),errors=[],selected=request.query_params.get('divisao','nova')))
@app.post('/inscricao')
def register(request:Request,name:Annotated[str,Form()],whatsapp:Annotated[str,Form()],ea_id:Annotated[str,Form()],division:Annotated[str,Form()],platform:Annotated[str,Form()],password:Annotated[str,Form()],accept_rules:Annotated[str|None,Form()]=None,db:Session=Depends(get_db)):
 s=get_setting(db);count=db.scalar(select(func.count()).select_from(Player).where(Player.active==True,Player.division==division));e=[]
 if not s.registrations_open:e.append('Inscrições fechadas.')
 if division not in DIVISIONS or platform not in DIVISIONS.get(division,{}).get('platforms',[]):e.append('Divisão ou plataforma inválida.')
 if count>=MAX_PLAYERS:e.append('Vagas preenchidas.')
 if len(password)<6:e.append('Senha mínima de 6 caracteres.')
 if not accept_rules:e.append('Aceite o regulamento.')
 if e:return templates.TemplateResponse('register.html',ctx(request,setting=s,errors=e,selected=division),status_code=400)
 p=Player(name=name.strip(),whatsapp=whatsapp.strip(),ea_id=ea_id.strip(),platform=platform,division=division,password_hash=hash_password(password));db.add(p)
 try:db.commit();db.refresh(p)
 except IntegrityError:db.rollback();return templates.TemplateResponse('register.html',ctx(request,setting=s,errors=['WhatsApp ou ID EA já cadastrado.'],selected=division),status_code=400)
 request.session['player_id']=p.id;return RedirectResponse('/painel?novo=1',303)
@app.get('/login',response_class=HTMLResponse)
def loginpage(request:Request):return templates.TemplateResponse('login.html',ctx(request,error=None))
@app.post('/login')
def login(request:Request,identifier:Annotated[str,Form()],password:Annotated[str,Form()],db:Session=Depends(get_db)):
 p=db.scalar(select(Player).where(or_(Player.whatsapp==identifier.strip(),Player.ea_id==identifier.strip())))
 if not p or not verify_password(password,p.password_hash):return templates.TemplateResponse('login.html',ctx(request,error='Dados incorretos.'),status_code=400)
 request.session['player_id']=p.id;return RedirectResponse('/painel',303)
@app.get('/sair')
def logout(request:Request):request.session.clear();return RedirectResponse('/',303)
@app.get('/painel',response_class=HTMLResponse)
def painel(request:Request,db:Session=Depends(get_db)):
 p=player_ok(request,db);ms=db.scalars(select(Match).where(or_(Match.player1_id==p.id,Match.player2_id==p.id)).order_by(Match.id)).all();return templates.TemplateResponse('dashboard.html',ctx(request,player=p,matches=ms,new=request.query_params.get('novo')))
@app.get('/partida/{mid}',response_class=HTMLResponse)
def room(mid:int,request:Request,db:Session=Depends(get_db)):
 p=player_ok(request,db);m=db.get(Match,mid)
 if not m or p.id not in {m.player1_id,m.player2_id}:raise HTTPException(404)
 msgs=db.scalars(select(MatchMessage).where(MatchMessage.match_id==mid).order_by(MatchMessage.created_at)).all();opp=m.player2 if p.id==m.player1_id else m.player1;return templates.TemplateResponse('match_room.html',ctx(request,player=p,opponent=opp,match=m,messages=msgs))
@app.post('/partida/{mid}/mensagem')
def message(mid:int,request:Request,message:Annotated[str,Form()],db:Session=Depends(get_db)):
 p=player_ok(request,db);m=db.get(Match,mid)
 if not m or p.id not in {m.player1_id,m.player2_id}:raise HTTPException(404)
 if message.strip():db.add(MatchMessage(match_id=mid,player_id=p.id,message=message.strip()));db.commit()
 return RedirectResponse(f'/partida/{mid}',303)
@app.post('/partida/{mid}/resultado')
def result(mid:int,request:Request,score1:Annotated[int,Form()],score2:Annotated[int,Form()],db:Session=Depends(get_db)):
 p=player_ok(request,db);m=db.get(Match,mid)
 if not m or p.id not in {m.player1_id,m.player2_id}:raise HTTPException(404)
 m.score1=max(0,score1);m.score2=max(0,score2);m.result_submitted_by=p.id;m.result_confirmed=False;m.status='aguardando_confirmação';db.commit();return RedirectResponse(f'/partida/{mid}',303)
@app.post('/partida/{mid}/confirmar')
def confirm(mid:int,request:Request,action:Annotated[str,Form()],db:Session=Depends(get_db)):
 p=player_ok(request,db);m=db.get(Match,mid)
 if not m or p.id not in {m.player1_id,m.player2_id}:raise HTTPException(404)
 if action=='confirm' and m.result_submitted_by!=p.id:m.result_confirmed=True;m.status='finalizada';db.commit();recalculate(db)
 elif action=='contest':m.status='contestada';db.add(Report(match_id=mid,reporter_id=p.id,type='resultado',description='Resultado contestado.'));db.commit()
 return RedirectResponse(f'/partida/{mid}',303)
@app.post('/partida/{mid}/reportar')
def report(mid:int,request:Request,report_type:Annotated[str,Form()],description:Annotated[str,Form()],db:Session=Depends(get_db)):
 p=player_ok(request,db);m=db.get(Match,mid)
 if not m or p.id not in {m.player1_id,m.player2_id}:raise HTTPException(404)
 db.add(Report(match_id=mid,reporter_id=p.id,type=report_type,description=description.strip()));db.commit();return RedirectResponse(f'/partida/{mid}',303)
@app.get('/ranking',response_class=HTMLResponse)
def ranking(request:Request,division:str='nova',db:Session=Depends(get_db)):
 if division not in DIVISIONS:division='nova'
 return templates.TemplateResponse('ranking.html',ctx(request,players=ranking_query(db,division),selected_division=division))
@app.get('/regulamento',response_class=HTMLResponse)
def rules(request:Request):return templates.TemplateResponse('rules.html',ctx(request))
@app.get('/admin/login',response_class=HTMLResponse)
def alogin(request:Request):return templates.TemplateResponse('admin_login.html',ctx(request,error=None))
@app.post('/admin/login')
def aloginpost(request:Request,username:Annotated[str,Form()],password:Annotated[str,Form()]):
 if username!=ADMIN_USER or password!=ADMIN_PASS:return templates.TemplateResponse('admin_login.html',ctx(request,error='Credenciais incorretas.'),status_code=400)
 request.session['admin']=True;return RedirectResponse('/admin',303)
@app.get('/admin',response_class=HTMLResponse)
def admin(request:Request,db:Session=Depends(get_db)):
 admin_ok(request);return templates.TemplateResponse('admin.html',ctx(request,setting=get_setting(db),players=db.scalars(select(Player).order_by(Player.id)).all(),matches=db.scalars(select(Match).order_by(Match.division,Match.id)).all(),reports=db.scalars(select(Report).order_by(Report.created_at.desc())).all(),message=request.query_params.get('message')))
@app.post('/admin/sortear')
def draw(request:Request,division:Annotated[str,Form()],db:Session=Depends(get_db)):
 admin_ok(request)
 try:msg=f'{len(draw_groups(db,get_setting(db).season,division))} grupos criados'
 except Exception as x:msg=str(x)
 return RedirectResponse(f'/admin?message={msg}',303)
@app.post('/admin/mata-mata')
def ko(request:Request,division:Annotated[str,Form()],db:Session=Depends(get_db)):
 admin_ok(request)
 try:msg=f'{generate_knockout(db,get_setting(db).season,division)} confrontos gerados'
 except Exception as x:msg=str(x)
 return RedirectResponse(f'/admin?message={msg}',303)
@app.post('/admin/partida/{mid}')
def ascore(mid:int,request:Request,score1:Annotated[int,Form()],score2:Annotated[int,Form()],scheduled_for:Annotated[str,Form()]='A definir',db:Session=Depends(get_db)):
 admin_ok(request);m=db.get(Match,mid)
 if not m:raise HTTPException(404)
 m.score1=max(0,score1);m.score2=max(0,score2);m.scheduled_for=scheduled_for;m.status='finalizada';m.result_confirmed=True;db.commit();recalculate(db);return RedirectResponse('/admin?message=Placar salvo',303)
@app.get('/health')
def health():return {'status':'ok','version':'4.0.0'}
