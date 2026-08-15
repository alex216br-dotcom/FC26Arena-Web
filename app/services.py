import math
import random
import re
from itertools import combinations
from sqlalchemy import func, select
from sqlalchemy.orm import Session
from .models import (
    Achievement, AuditLog, Evidence, Match, MatchMessage, Notification,
    PrizeConversation, PrizeMessage, Registration, Report, TeamMember,
    Tournament, TournamentPrize, User, UserAchievement
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


def ensure_champion_prize_conversation(
    db: Session,
    tournament: Tournament,
    champion: Registration,
    match: Match | None = None,
) -> PrizeConversation:
    """Cria uma conversa de premiação por título ou por Duelo Rápido."""
    query = select(PrizeConversation).where(
        PrizeConversation.tournament_id == tournament.id,
        PrizeConversation.registration_id == champion.id,
    )
    if match is None:
        query = query.where(PrizeConversation.match_id.is_(None))
    else:
        query = query.where(PrizeConversation.match_id == match.id)

    conversation = db.scalar(query)
    if conversation:
        return conversation

    first_prize = db.scalar(
        select(TournamentPrize)
        .where(
            TournamentPrize.tournament_id == tournament.id,
            TournamentPrize.place == 1,
        )
    )
    amount = first_prize.amount if first_prize else tournament.prize

    conversation = PrizeConversation(
        tournament_id=tournament.id,
        registration_id=champion.id,
        match_id=match.id if match else None,
        amount=amount or 0,
        status="awaiting_data",
    )
    db.add(conversation)
    db.flush()

    db.add(PrizeMessage(
        conversation_id=conversation.id,
        user_id=None,
        sender_type="admin",
        message=(
            "Parabéns pela vitória! O pagamento da premiação será realizado "
            "em até 24 horas após o envio e a conferência dos dados. "
            "Envie sua chave Pix, nome do titular e CPF do titular pelo "
            "formulário seguro desta conversa."
        ),
    ))

    for user in registration_users(db, champion):
        db.add(Notification(
            user_id=user.id,
            channel="site",
            subject="🏆 Você venceu!",
            body=(
                f"Parabéns pela vitória em {tournament.name}! "
                "Abra a conversa de premiação no seu painel para enviar "
                "os dados de recebimento. O pagamento será feito em até 24 horas."
            ),
            status="sent",
        ))

    db.flush()
    return conversation



def quick_duel_registration_has_active_match(
    db: Session,
    registration_id: int,
) -> bool:
    return bool(db.scalar(
        select(Match.id).where(
            (
                (Match.home_registration_id == registration_id)
                | (Match.away_registration_id == registration_id)
            ),
            Match.status.in_(
                ["scheduled", "awaiting_confirmation", "disputed"]
            ),
        )
    ))


def pair_quick_duel_waiting_players(
    db: Session,
    tournament: Tournament,
) -> int:
    """
    Cada par de inscrições aprovadas e sem partida ativa recebe uma Sala PVP.
    Quantos pares existirem podem jogar simultaneamente.
    """
    if not tournament.quick_duel:
        return 0

    approved = db.scalars(
        select(Registration)
        .where(
            Registration.tournament_id == tournament.id,
            Registration.status == "approved",
        )
        .order_by(Registration.id)
    ).all()

    waiting = [
        registration
        for registration in approved
        if not quick_duel_registration_has_active_match(
            db, registration.id
        )
    ]

    created = 0
    existing_count = db.scalar(
        select(func.count())
        .select_from(Match)
        .where(Match.tournament_id == tournament.id)
    ) or 0

    while len(waiting) >= 2:
        home = waiting.pop(0)
        away = waiting.pop(0)
        duel_number = existing_count + created + 1

        db.add(Match(
            tournament_id=tournament.id,
            phase="quick_duel",
            round_name=(
                f"Duelo #{duel_number} — melhor de 3"
                if tournament.duel_series == 3
                else f"Duelo #{duel_number} — partida única"
            ),
            round_order=duel_number,
            group_name="Arena Duelo Rápido",
            home_registration_id=home.id,
            away_registration_id=away.id,
            status="scheduled",
        ))
        db.flush()

        for registration in (home, away):
            for recipient in registration_users(db, registration):
                db.add(Notification(
                    user_id=recipient.id,
                    channel="site",
                    subject="⚡ Adversário encontrado",
                    body=(
                        f"Seu duelo em {tournament.name} está pronto. "
                        "Abra a Sala PVP no painel."
                    ),
                    status="sent",
                ))

        created += 1

    tournament.status = "open"
    db.commit()
    return created


def finish_quick_duel_match(
    db: Session,
    match: Match,
) -> None:
    """
    Encerra somente este confronto, gera a premiação e deixa a arena aberta.
    """
    tournament = match.tournament
    champion = db.get(Registration, match.winner_registration_id)
    if not champion:
        return

    for user in registration_users(db, champion):
        user.titles += 1
        award_achievement(db, user, "CHAMPION")

    ensure_champion_prize_conversation(
        db,
        tournament,
        champion,
        match=match,
    )

    # Os dois jogadores ficam livres para realizar uma nova inscrição.
    for registration in (match.home, match.away):
        if registration:
            registration.status = "finished"

    tournament.status = "open"
    db.commit()
    recalculate_global_stats(db)

    # Caso já existam outros jogadores esperando, forma novos pares.
    pair_quick_duel_waiting_players(db, tournament)


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
        Match.phase.in_(["group", "league"]),
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
        key=lambda r: (
            r.points,
            r.wins,
            r.goals_for - r.goals_against,
            r.goals_for,
        ),
        reverse=True,
    )


def clear_tournament_matches(db: Session, tournament_id: int):
    """
    Apaga as partidas de um campeonato na ordem correta para PostgreSQL.
    Mensagens, provas e denúncias das Salas PVP precisam ser removidas antes
    das partidas devido às chaves estrangeiras.
    """
    match_ids = list(db.scalars(
        select(Match.id).where(Match.tournament_id == tournament_id)
    ).all())

    if match_ids:
        db.query(MatchMessage).filter(
            MatchMessage.match_id.in_(match_ids)
        ).delete(synchronize_session=False)

        db.query(Evidence).filter(
            Evidence.match_id.in_(match_ids)
        ).delete(synchronize_session=False)

        db.query(Report).filter(
            Report.match_id.in_(match_ids)
        ).delete(synchronize_session=False)

        db.query(Match).filter(
            Match.id.in_(match_ids)
        ).delete(synchronize_session=False)

    registrations = db.scalars(
        select(Registration).where(Registration.tournament_id == tournament_id)
    ).all()

    for registration in registrations:
        registration.group_name = None
        registration.points = 0
        registration.played = 0
        registration.wins = 0
        registration.draws = 0
        registration.losses = 0
        registration.goals_for = 0
        registration.goals_against = 0

    db.flush()


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
    clear_tournament_matches(db, tournament.id)

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



def generate_round_robin_rounds(
    registrations: list[Registration],
) -> list[list[tuple[Registration, Registration]]]:
    """
    Gera rodadas equilibradas usando o método circular.
    Cada participante joga uma vez por rodada; em quantidade ímpar há folga.
    """
    participants: list[Registration | None] = list(registrations)
    if len(participants) % 2:
        participants.append(None)

    total = len(participants)
    rounds: list[list[tuple[Registration, Registration]]] = []

    for round_index in range(total - 1):
        pairings: list[tuple[Registration, Registration]] = []
        for index in range(total // 2):
            left = participants[index]
            right = participants[total - 1 - index]
            if left is None or right is None:
                continue

            # Alterna mandante para distribuir melhor os lados.
            if (round_index + index) % 2:
                home, away = right, left
            else:
                home, away = left, right
            pairings.append((home, away))

        rounds.append(pairings)
        participants = [participants[0], participants[-1], *participants[1:-1]]

    return rounds


def generate_league(db: Session, tournament: Tournament):
    approved = db.scalars(
        select(Registration).where(
            Registration.tournament_id == tournament.id,
            Registration.status == "approved",
        )
    ).all()

    if len(approved) < 2:
        raise ValueError("São necessárias pelo menos 2 inscrições aprovadas.")

    clear_tournament_matches(db, tournament.id)
    random.shuffle(approved)

    turns = 2 if tournament.league_turns == 2 else 1
    first_turn = generate_round_robin_rounds(approved)
    round_number = 1

    for pairings in first_turn:
        for home, away in pairings:
            db.add(Match(
                tournament_id=tournament.id,
                phase="league",
                round_name=f"Rodada {round_number}",
                round_order=round_number,
                group_name="Liga",
                home_registration_id=home.id,
                away_registration_id=away.id,
            ))
        round_number += 1

    if turns == 2:
        for pairings in first_turn:
            for home, away in pairings:
                db.add(Match(
                    tournament_id=tournament.id,
                    phase="league",
                    round_name=f"Rodada {round_number}",
                    round_order=round_number,
                    group_name="Liga",
                    home_registration_id=away.id,
                    away_registration_id=home.id,
                ))
            round_number += 1

    tournament.status = "league_stage"
    db.commit()
    return {
        "participants": len(approved),
        "rounds": round_number - 1,
        "turns": turns,
    }


def league_finished(db: Session, tournament_id: int) -> bool:
    matches = db.scalars(
        select(Match).where(
            Match.tournament_id == tournament_id,
            Match.phase == "league",
        )
    ).all()
    return bool(matches) and all(
        match.status == "completed" and match.result_confirmed
        for match in matches
    )


def finish_league_if_ready(db: Session, tournament: Tournament):
    if tournament.competition_format != "league":
        return
    if not league_finished(db, tournament.id):
        return

    registrations = db.scalars(
        select(Registration).where(
            Registration.tournament_id == tournament.id,
            Registration.status == "approved",
        )
    ).all()
    table = sorted_group(registrations)
    if not table:
        return

    was_completed = tournament.status == "completed"
    tournament.status = "completed"
    champion = table[0]

    if not was_completed:
        for user in registration_users(db, champion):
            user.titles += 1
            award_achievement(db, user, "CHAMPION")
        if champion.team:
            champion.team.titles += 1

        ensure_champion_prize_conversation(db, tournament, champion)

        prize_rows = db.scalars(
            select(TournamentPrize)
            .where(TournamentPrize.tournament_id == tournament.id)
            .order_by(TournamentPrize.place)
        ).all()

        # Compatibilidade com campeonatos antigos sem distribuição cadastrada.
        if not prize_rows and tournament.prize is not None:
            prize_rows = [
                TournamentPrize(
                    tournament_id=tournament.id,
                    place=1,
                    amount=tournament.prize,
                )
            ]

        for prize in prize_rows:
            index = prize.place - 1
            if index < 0 or index >= len(table):
                continue

            registration = table[index]
            for user in registration_users(db, registration):
                db.add(Notification(
                    user_id=user.id,
                    channel="site",
                    subject=f"{prize.place}º lugar premiado",
                    body=(
                        f"Você terminou {tournament.name} em "
                        f"{prize.place}º lugar e entrou na premiação de "
                        f"R$ {float(prize.amount):.2f}."
                    ),
                    status="sent",
                ))

    db.commit()


def groups_finished(db: Session, tournament_id: int) -> bool:
    matches = db.scalars(select(Match).where(
        Match.tournament_id == tournament_id,
        Match.phase == "group",
    )).all()
    return bool(matches) and all(m.status == "completed" and m.result_confirmed for m in matches)

def rebuild_unplayed_knockout(db: Session, tournament: Tournament):
    """
    Remove apenas o mata-mata ainda não jogado para que ele possa ser
    recriado a partir da classificação corrigida da fase de grupos.

    Retorna a quantidade de partidas removidas. Se alguma partida de
    mata-mata já tiver placar/resultado, a correção é bloqueada para não
    apagar resultados válidos.
    """
    knockout_matches = db.scalars(select(Match).where(
        Match.tournament_id == tournament.id,
        Match.phase == "knockout",
    )).all()

    if not knockout_matches:
        reset_registration_stats(db, tournament.id)
        tournament.status = "group_stage"
        db.commit()
        return 0

    already_played = any(
        m.status == "completed"
        or m.result_confirmed
        or m.home_score is not None
        or m.away_score is not None
        for m in knockout_matches
    )
    if already_played:
        raise ValueError(
            "O mata-mata já possui partida com resultado. "
            "A correção automática foi bloqueada para não apagar resultados já jogados."
        )

    match_ids = [m.id for m in knockout_matches]

    # Remove dependências das Salas PVP antes de recriar os confrontos.
    db.query(MatchMessage).filter(
        MatchMessage.match_id.in_(match_ids)
    ).delete(synchronize_session=False)
    db.query(Evidence).filter(
        Evidence.match_id.in_(match_ids)
    ).delete(synchronize_session=False)
    db.query(Report).filter(
        Report.match_id.in_(match_ids)
    ).delete(synchronize_session=False)

    # Por segurança, uma conversa de prêmio nunca deve apontar para uma
    # partida futura que será recriada.
    db.query(PrizeConversation).filter(
        PrizeConversation.match_id.in_(match_ids)
    ).update({PrizeConversation.match_id: None}, synchronize_session=False)

    db.query(Match).filter(
        Match.id.in_(match_ids)
    ).delete(synchronize_session=False)

    reset_registration_stats(db, tournament.id)
    tournament.status = "group_stage"
    db.commit()
    return len(match_ids)


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
        ensure_champion_prize_conversation(db, tournament, champion)
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
    db.commit()
    db.refresh(match)

    if match.tournament.quick_duel:
        finish_quick_duel_match(db, match)
        return

    reset_registration_stats(db, match.tournament_id)
    recalculate_global_stats(db)

    if match.tournament.competition_format == "league":
        finish_league_if_ready(db, match.tournament)
        return

    advance_knockout(db, match.tournament, match)
