# FC26 Arena Web V3 Definitiva

Versão pronta para lançamento da temporada beta.

## Incluído
- Duas divisões independentes: nova e antiga geração.
- Inscrição, login e painel do jogador.
- Contadores e premiações separados.
- Página inicial com como funciona, últimos inscritos e resultados.
- Ranking completo por divisão.
- Sorteio de grupos e geração de partidas.
- Geração inicial do mata-mata.
- Administração de jogadores, datas, avisos e placares.
- Importação de jogadores do Telegram.
- PostgreSQL no Railway.

## Variáveis Railway
DATABASE_URL=${{Postgres.DATABASE_URL}}
SECRET_KEY=uma-chave-longa-e-aleatoria
ADMIN_USERNAME=admin
ADMIN_PASSWORD=sua-senha-forte
GROUP_URL=link-do-grupo
MAX_PLAYERS=32
CHAMPION_PRIZE_NEW=50
CHAMPION_PRIZE_OLD=50

## Observação
A geração automática incluída cria a primeira rodada do mata-mata com os classificados dos grupos.
O avanço automático entre oitavas, quartas, semifinais e final deverá ser acionado em atualização posterior ou administrado pelo painel na temporada beta.
