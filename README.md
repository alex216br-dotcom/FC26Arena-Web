# FC26 Arena Web V6 Pro

Versão visual e funcional inspirada no layout profissional aprovado pelo organizador.

## Funcionalidades locais já implementadas

- Página inicial moderna, responsiva e semelhante ao mockup.
- Cadastro em painel lateral com seleção por botões.
- Plataformas filtradas pela geração:
  - Nova: PlayStation 5, Xbox Series X|S e PC.
  - Antiga: PlayStation 4 e Xbox One.
- Vários campeonatos simultâneos.
- Torneios 1x1, 2x2 e Pro Clubs.
- Criação de torneios pelo painel.
- Equipes, capitães, convites e membros.
- Inscrição individual ou por equipe.
- Cupons.
- Pagamento Pix manual.
- Endpoint protegido para confirmação externa de pagamento.
- Grupos automáticos.
- Classificação automática.
- Mata-mata automático até a final.
- Sala PVP.
- Chat entre adversários.
- Envio e confirmação de resultado.
- Contestação, W.O. e denúncias.
- Upload local de prints e provas.
- Ranking permanente.
- XP, nível, conquistas e medalhas.
- Notificações internas.
- E-mail SMTP, Telegram e webhook de WhatsApp preparados.
- Recuperação de senha.
- Auditoria administrativa.
- Suporte.
- Login administrativo por senha.

## Integrações externas

O site funciona sem integrações externas, mas os recursos abaixo exigem credenciais:

- E-mail: configure SMTP.
- Telegram: configure TELEGRAM_BOT_TOKEN e salve o chat ID do jogador.
- WhatsApp: configure WHATSAPP_WEBHOOK_URL para um provedor oficial ou automação.
- Pix automático: configure um provedor para chamar o endpoint:
  `POST /webhooks/pagamento`
  com o header `X-Webhook-Secret`.

Sem provedor, o administrador pode aprovar pagamentos manualmente.

## Railway

Use um PostgreSQL novo e configure:

```text
DATABASE_URL=${{Postgres.DATABASE_URL}}
SECRET_KEY=uma-chave-longa
ADMIN_PASSWORD=sua-senha
SITE_URL=https://seu-site.up.railway.app
UPLOAD_DIR=/data/uploads
```

Para armazenar provas no Railway, monte um Volume em `/data`.

## Admin

`/admin/login`

## Banco

A aplicação cria tabelas automaticamente. Como esta versão muda muito o esquema,
use um banco novo na primeira publicação.


## V6.1 — Painel administrativo profissional

- Visão geral com indicadores.
- Tela de gerenciamento para cada campeonato.
- Edição completa de nome, valores, prêmio, vagas, data, geração, modalidade, regulamento e status.
- Sorteio com validação clara da quantidade mínima.
- Repetição e limpeza segura do sorteio sem apagar inscrições.
- Aprovação, cancelamento e remoção de inscrições.
- Inclusão manual de jogador em campeonato 1x1.
- Gestão central de jogadores, contatos, ID EA, geração, plataforma e bloqueio.
- Gestão de partidas, horários, placares e status.


## Atualização mobile e imagem principal

- Menu hambúrguer com acesso a Campeonatos, Ranking, Times, Notificações, Conquistas, Suporte e Meu painel.
- A imagem do jogador no estádio agora é usada de verdade no hero.
- No celular, a imagem é exibida inteira, centralizada e sem ficar atrás das opções.
- Os botões e cartões do hero permanecem visíveis antes da imagem.


## Atualização: Regulamento, Como funciona e Sala PVP

- Imagem horizontal no desktop e imagem vertical específica no celular.
- Página pública `/como-funciona`.
- Página pública `/regulamento`.
- Explicação completa sobre a Sala PVP.
- Orientação para confirmar/copiar o ID EA.
- Instruções para realizar a partida em Amistosos Online.
- Fluxo de envio, confirmação, contestação, provas e W.O.
- Sala PVP reformulada com instruções antes da partida.


## Correção do pagamento administrativo

- Aprovar pagamento sincroniza `payments.status`, `registrations.payment_status` e `registrations.status`.
- Recusar ou voltar para pendente também sincroniza os três estados.
- O jogador recebe uma notificação no site.
- A página Pix atualiza automaticamente a cada 5 segundos enquanto estiver pendente.
- O painel mostra lado a lado o status do pagamento e da inscrição.
- A página de pagamento usa `Cache-Control: no-store` para não exibir dados antigos.


## Correção: excluir inscrição

- Pagamentos vinculados são excluídos antes da inscrição.
- A exclusão funciona para inscrições gratuitas e pagas.
- Inscrições com partidas vinculadas não são apagadas para preservar o campeonato.
- Nesse caso, o painel orienta cancelar a inscrição ou limpar o sorteio primeiro.
- Erros do PostgreSQL agora aparecem como mensagem no painel em vez de tela branca.


## Aprovação automática e continuação da inscrição

- Aprovar a inscrição em um campeonato pago também aprova o registro real de pagamento.
- Registros antigos com inscrição aprovada e pagamento pendente são reparados automaticamente.
- A página Pix verifica o status a cada 3 segundos.
- Após a aprovação, o jogador é enviado automaticamente para o painel em 2 segundos.
- O botão administrativo agora informa que aprova inscrição e pagamento.
