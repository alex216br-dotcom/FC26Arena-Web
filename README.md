# FC26 Arena Web V5 Base

Base profissional para evolução da plataforma FC26 Arena.

## O que já funciona

- Cadastro e login de jogadores.
- Login administrativo por senha.
- Vários campeonatos simultâneos.
- Criação de campeonatos pelo painel.
- Modalidades 1x1, 2x2 e Pro Clubs.
- Nova e antiga geração.
- Inscrição de jogador em campeonato.
- Estrutura de equipes e membros.
- Estrutura de temporadas.
- Estrutura de partidas, grupos e mata-mata.
- Estrutura de ranking permanente.
- Estrutura de XP, níveis e conquistas.
- Estrutura de cupons e pagamentos.
- Estrutura de notificações.
- Estrutura de upload de provas.
- Estrutura de recuperação de senha.
- Estrutura de administradores e permissões.
- Auditoria de ações administrativas.
- PostgreSQL e Alembic preparados.

## O que ainda depende de integração externa

- Envio real de e-mail.
- WhatsApp oficial.
- Telegram automático.
- Pix automático e webhook.
- Armazenamento externo de arquivos.
- Geração completa de todas as fases do mata-mata.
- Rotinas assíncronas de notificações.

Esses módulos estão modelados no banco, mas precisam das credenciais do provedor escolhido.

## Railway

Adicione PostgreSQL e configure:

```text
DATABASE_URL=${{Postgres.DATABASE_URL}}
SECRET_KEY=uma-chave-longa-e-aleatoria
ADMIN_PASSWORD=sua-senha-forte
SITE_URL=https://seu-dominio.up.railway.app
```

## Admin

Acesse:

```text
/admin/login
```

## Migração

Para produção, use Alembic. Nesta base, as tabelas também são criadas automaticamente no primeiro deploy para facilitar os testes.
