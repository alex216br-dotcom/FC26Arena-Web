# FC26 Arena Web Pro V1

Site completo para inscrição e administração do FC26 Arena.

## Recursos
- Página inicial responsiva.
- Cadastro de jogadores.
- Login e painel do jogador.
- Ranking e classificação.
- Painel administrativo.
- Abrir/fechar inscrições.
- Sortear grupos e gerar partidas.
- Registrar placares.
- Importar `jogadores.json` do bot Telegram.
- SQLite local e PostgreSQL no Railway.

## Estrutura correta no GitHub
A pasta `app` deve aparecer diretamente na raiz do repositório:

```
app/
requirements.txt
Procfile
railway.json
README.md
```

## Railway
Crie PostgreSQL no mesmo projeto e adicione no serviço web:

```
DATABASE_URL=${{Postgres.DATABASE_URL}}
SECRET_KEY=uma-chave-bem-grande
ADMIN_USERNAME=admin
ADMIN_PASSWORD=sua-senha
GROUP_URL=link-do-grupo
MAX_PLAYERS=32
CHAMPION_PRIZE=50
```

Depois gere um domínio em Settings > Networking.

## Admin
Acesse `/admin/login`.
