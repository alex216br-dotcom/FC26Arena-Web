# FC26 Arena Web V4 — Plataforma Completa

Inclui duas divisões, cadastro, login, ranking, grupos, mata-mata inicial, Sala PVP por partida, chat, WhatsApp do adversário, envio e confirmação de resultado, W.O./denúncias, XP/nível e painel administrativo.

## Variáveis Railway
DATABASE_URL=${{Postgres.DATABASE_URL}}
SECRET_KEY=uma-chave-longa
ADMIN_USERNAME=admin
ADMIN_PASSWORD=sua-senha
GROUP_URL=link-do-grupo
MAX_PLAYERS=32
CHAMPION_PRIZE_NEW=50
CHAMPION_PRIZE_OLD=50

## Banco
A estrutura mudou. Use PostgreSQL novo ou migrações antes de colocar em produção.
