# Очередь выплат и казна хранителя: /payouts, /payout (включая холостой
# /payout test), /return, /treasury, /fundout, kill switch исходящих
# /halt-payouts ↔ /resume-payouts и отчёты /incoming, /stakes, /revenue.
from __future__ import annotations

import asyncio
import logging
from datetime import UTC, datetime
from decimal import Decimal, InvalidOperation
from html import escape as html_escape

from aiogram import F
from aiogram.enums import ChatType, ParseMode
from aiogram.filters import Command
from aiogram.types import Message
from sqlalchemy import func, select

from app.config import settings
from app.db import SessionLocal
from app.models import Income, Payout, Player, Round, Stake
from app.style import money_mark, ok_mark, warn_mark
from app.ton_utils import friendly_address, from_nano, normalize_address

from .common import router

logger = logging.getLogger(__name__)


async def _payouts_text() -> str:
    """Список неотправленных выплат (для /payouts и кнопки пульта)."""
    from app.ops import is_payouts_halted, payout_halt_reason

    async with SessionLocal() as session:
        halted = await is_payouts_halted(session)
        halt_reason = await payout_halt_reason(session) if halted else None
        rows = (
            (
                await session.execute(
                    select(Payout)
                    .where(Payout.status.notin_(["sent", "dismissed"]))
                    .order_by(Payout.id.asc())
                    .limit(30)
                )
            )
            .scalars()
            .all()
        )
    head: list[str] = []
    if halted:
        # Строка стоит ПЕРЕД списком: очередь выглядит живой, а не уходит
        # только потому, что её остановили вручную, — без этого отчёта
        # причину пришлось бы искать в логах.
        why = f": {halt_reason}" if halt_reason else ""
        head.append(
            f"{warn_mark('halt')} Исходящие выплаты ОСТАНОВЛЕНЫ{why} — снять: /resume-payouts"
        )
        head.append("")
    if not rows:
        tail = f"{ok_mark('queue')} Долгов нет: все выплаты ушли или разобраны."
        return "\n".join(head + [tail]) if head else tail
    lines = head + ["Неотправленные выплаты:"]
    for row in rows:
        reason = getattr(row, "last_error", None)
        tail = f" · {reason[:110]}" if reason else ""
        lines.append(
            f"#{row.id} · {row.kind} · {from_nano(row.amount_nanotons):.4f} Gram · "
            f"{row.status} · попыток {row.attempts} · …{row.dest_address[-8:]}{tail}"
        )
    lines.append("")
    lines.append(
        "Спам (пыль с рекламой, только refund): /payout <id> spam confirm\n"
        "Настоящий долг, отправить снова: /payout <id> retry"
    )
    return "\n".join(lines)


async def _stakes_panel_text() -> str:
    """Ставки для пульта: необработанные (pending) по всем дням + сводка."""
    from app.ops import snapshot

    snap = await snapshot()
    pending_total = snap.get("pending_stakes") or 0
    lines = [
        "🎲 <b>СТАВКИ ХРАНИТЕЛЮ</b>",
        f"⏳ Необработанных переводов-ставок: {pending_total}",
        "",
    ]
    async with SessionLocal() as session:
        days = (
            (await session.execute(select(Round).order_by(Round.day_index.desc()).limit(3)))
            .scalars()
            .all()
        )
        shown = 0
        for day in days:
            rows = (
                await session.execute(
                    select(Stake, Player.username, Player.first_name)
                    .join(Player, Player.id == Stake.player_id)
                    .where(Stake.round_id == day.id)
                    .order_by(Stake.id.asc())
                    .limit(20)
                )
            ).all()
            if not rows:
                continue
            shown += 1
            lines.append(f"День {day.day_index} ({day.status.value}):")
            for stake, username, first_name in rows:
                who = username or first_name or f"игрок {stake.player_id}"
                state = {"confirmed": "✅", "pending": "⏳", "rejected": "↩️"}.get(
                    stake.status, stake.status
                )
                lines.append(
                    f"  {who}: {from_nano(stake.amount_nanotons):g} Gram {state}"
                )
    if not shown:
        lines.append("Ставок за последние дни нет.")
    return "\n".join(lines)


async def _refunds_panel_text() -> str:
    """Ставки для ручного возврата: «не засчитанные» (pending/rejected) с
    кошельком и без уже созданного возврата. Действие — /return <id>."""
    from app.stakes import refundable_stakes

    async with SessionLocal() as session:
        rows = await refundable_stakes(session)
    lines = [
        "↩️ <b>РУЧНОЙ ВОЗВРАТ СТАВОК</b>",
        "Ставки, не получившие «засчитано» и ещё не возвращённые. Подтверждённые "
        "сюда не попадают — они разберутся при итогах дня сами.",
        "",
    ]
    if not rows:
        lines.append("Нет кандидатов для ручного возврата. Долгов нет.")
        return "\n".join(lines)
    for stake, player, round_row in rows:
        who = (
            (player.username or player.first_name or f"игрок {player.id}")
            if player
            else f"игрок {stake.player_id}"
        )
        state = "⏳ не подтверждена" if stake.status == "pending" else "↩️ отклонена"
        round_label = f"день {round_row.day_index}" if round_row else f"раунд {stake.round_id}"
        lines.append(
            f"#{stake.id} {who} · {from_nano(stake.amount_nanotons):g} Gram · "
            f"{state} · {round_label}\n   ↔️ /return {stake.id}"
        )
    lines.append("")
    lines.append("/return &lt;id&gt; — вернуть ставку, деньги уйдут очередью выплат.")
    return "\n".join(lines)


@router.message(Command("payouts"), F.chat.type == ChatType.PRIVATE)
async def cmd_payouts(message: Message) -> None:
    """Очередь выплат для хранителя: что не ушло и почему."""
    if message.from_user is None or message.from_user.id not in settings.admin_id_set:
        await message.answer("Команда только для хранителя игры.")
        return
    await message.answer(await _payouts_text())


@router.message(Command("halt-payouts"), F.chat.type == ChatType.PRIVATE)
async def cmd_halt_payouts(message: Message) -> None:
    """Kill switch: остановить отправку исходящих выплат. /halt-payouts [причина].

    Сознательно ОТДЕЛЬНАЯ от /pause ручка. Пауза останавливает игру, но
    очередь выплат продолжает разгребаться (это написано прямо в ответе
    /pause): деньги игроков должны уйти. Здесь останавливается именно
    отправка — вариант «подозрительная активность, пока я проверяю», когда
    уходить не должно НИЧЕГО.

    Состояние кладётся в watcher_state, а не в память процесса: переживает
    рестарт и видно каждой копии диспетчера, а не только той, что приняла
    команду. Подтверждения уже ушедших переводов при этом не останавливаются
    — иначе судьба in-flight выплат осталась бы неизвестной навсегда.
    """
    if message.from_user is None or message.from_user.id not in settings.admin_id_set:
        await message.answer("Команда только для хранителя игры.")
        return
    from app.ops import set_payouts_halted

    words = (message.text or "").split(maxsplit=1)
    reason = words[1].strip()[:200] if len(words) > 1 else "причина не указана"
    async with SessionLocal() as session:
        changed = await set_payouts_halted(session, True, reason)
    if not changed:
        await message.answer(
            f"{warn_mark('halt')} Исходящие выплаты уже остановлены. Снять: /resume-payouts"
        )
        return
    await message.answer(
        f"{warn_mark('halt')} Исходящие выплаты ОСТАНОВЛЕНЫ: {reason}.\n"
        "Казна не уходит никуда: приз, возвраты, рейк и копилки остаются в "
        "очереди, попытки не сгорают, причина видна у каждой строки в /payouts.\n"
        "Подтверждения уже отправленных переводов продолжают идти.\n"
        "Снять: /resume-payouts"
    )


@router.message(Command("resume-payouts"), F.chat.type == ChatType.PRIVATE)
async def cmd_resume_payouts(message: Message) -> None:
    """Снять kill switch /halt-payouts и разпустить очередь обратно."""
    if message.from_user is None or message.from_user.id not in settings.admin_id_set:
        await message.answer("Команда только для хранителя игры.")
        return
    from app.ops import set_payouts_halted

    async with SessionLocal() as session:
        changed = await set_payouts_halted(session, False)
    if not changed:
        await message.answer(f"{ok_mark('halt')} Отправка выплат и так была разрешена.")
        return
    await message.answer(
        f"{ok_mark('halt')} Отправка выплат возобновлена: очередь уйдёт сама, "
        "ручного retry не требуется. Посмотреть: /payouts"
    )


# ---------- Холостой перевод (/payout test) ----------
#
# Репетиция боевого пути отправки на реальных деньгах: пункт «HTTP-фолбэк на
# mainnet» и шаг дня X чек-листа готовности. Заменяет собой руками написанный
# скрипт, которого раньше не существовало вовсе — в день X владелец импровизировал
# бы с боевым кошельком.
_TEST_MAX_GRAM = Decimal("0.05")
_TEST_USAGE = "/payout test <сумма Gram> [confirm] [http]"
_TEST_MEMO_PREFIX = "payout-test"
_TEST_POLL_ATTEMPTS = 6
_TEST_POLL_SECONDS = 5


def _parse_test_args(tokens: list[str]) -> tuple[Decimal, bool, bool]:
    """(сумма Gram, отправлять, принудительный HTTP-канал).

    Назначение НЕ выбирается: только сам кошелёк казны. Холостой перевод не
    имеет права увести деньги из казны — иначе после репетиции ожидания БД
    разошлись бы с балансом, и автосверка честно подняла бы тревогу «ручной
    вывод?». Любая непонятная лексема — отказ до отправки, а не «разберёмся
    как получится»: опечатка в confirm должна остановить до движения денег.
    """
    if not tokens:
        raise ValueError("нет суммы")
    try:
        amount = Decimal(tokens[0].replace(",", "."))
    except InvalidOperation:
        raise ValueError(f"сумма «{tokens[0]}» не число") from None
    if not amount.is_finite():
        raise ValueError("сумма должна быть конечным числом")
    if amount <= 0:
        raise ValueError("сумма должна быть больше нуля")
    if amount * 1_000_000_000 < 1:
        raise ValueError("сумма меньше одного нанотона")
    if amount > _TEST_MAX_GRAM:
        raise ValueError(
            f"потолок {_TEST_MAX_GRAM} Gram: это репетиция, а не способ увести деньги"
        )
    confirm = force_http = False
    for token in tokens[1:]:
        lowered = token.lower()
        if lowered == "confirm":
            confirm = True
        elif lowered == "http":
            force_http = True
        else:
            raise ValueError(f"непонятный аргумент «{token}»")
    return amount, confirm, force_http


async def _test_preflight(amount: Decimal, force_http: bool) -> str:
    """Блок «что будет отправлено» — всё, что узнаётся без единого движения денег.

    Каждая строка закрывает свой класс отказа, который иначе всплыл бы впервые
    в день с живыми ставками: пара мнемоник/адрес (без неё BoC не подписать),
    баланс, seqno через runGetMethod — та самая проверка, на которой держится
    HTTP-канал, — и сборка BoC оффлайн-кошельком без лайтсерверов.
    """
    import app.ton_pay as _tp

    network = "testnet" if settings.is_testnet else "mainnet"
    lines = [f"🧪 Холостой перевод · {network} · потолок {_TEST_MAX_GRAM} Gram"]
    wallet = None
    try:
        wallet, version = _tp.build_offline_treasury_wallet()
        lines.append(f"Пара мнемоник/адрес: {version} ✓ · BoC собирается")
    except Exception as exc:
        lines.append(f"Пара мнемоник/адрес: {html_escape(str(exc))} ⚠️")
    try:
        balance, status, source = await _tp.fetch_account_state()
    except Exception as exc:
        balance, status, source = None, None, f"ошибка: {exc}"
    if balance is None:
        lines.append(f"Баланс: недоступен ⚠️ · источник {html_escape(str(source))}")
    else:
        note = f", статус {status}" if status else ""
        lines.append(f"Баланс: {from_nano(balance):.4f} Gram{note} · источник {source}")
    try:
        seqno = await _tp.http_get_wallet_seqno(wallet)
        lines.append(f"seqno (runGetMethod по HTTP): {seqno} ✓")
    except Exception as exc:
        lines.append(f"seqno не прочитан: {html_escape(str(exc))} ⚠️")
    dest = settings.active_treasury_address
    shown = (
        friendly_address(normalize_address(dest), testnet=settings.is_testnet) if dest else "—"
    )
    lines.append(f"Куда: сам кошелёк казны <code>{shown}</code>")
    lines.append("Деньги вернутся в казну — израсходуется только газ.")
    lines.append(f"Сколько: {amount} Gram")
    lines.append(
        "Канал: "
        + (
            "HTTP принудительно — репетиция фолбэка при живых лайтсерверах"
            if force_http
            else "обычный (лайтсерверы; сам уйдёт в HTTP-канал, если они мертвы)"
        )
    )
    return "\n".join(lines)


async def _test_wait_memo(memo: str) -> str | None:
    """memo → хеш транзакции: скан истории казначея, до 30 с.

    Тем же путём confirm_broadcast_payouts подтверждает выплаты, поэтому
    «memo не нашёлся» здесь равносильно «подтверждение не придёт» там — риск
    sent vs confirmed, который пункт чек-листа и хочет увидеть заранее, а не
    в день, когда в очереди стоят чужие призы.
    """
    import app.ton_pay as _tp

    for attempt in range(_TEST_POLL_ATTEMPTS):
        tx_map = await _tp.fetch_broadcast_tx_map({memo})
        if memo in tx_map:
            return tx_map[memo]
        if attempt < _TEST_POLL_ATTEMPTS - 1:
            await asyncio.sleep(_TEST_POLL_SECONDS)
    return None


async def _payout_test(message: Message, tokens: list[str]) -> None:
    """Холостой перевод: предполёт всегда, отправка только с явным confirm."""
    try:
        amount, confirm, force_http = _parse_test_args(tokens)
    except ValueError as exc:
        await message.answer(
            f"{warn_mark('nopay')} Формат: <code>{html_escape(_TEST_USAGE)}</code>\n"
            f"Причина: {html_escape(str(exc))}",
            parse_mode=ParseMode.HTML,
        )
        return
    try:
        report = await _test_preflight(amount, force_http)
    except Exception as exc:
        await message.answer(
            f"{warn_mark('nopay')} Предполёт не прошёл: {html_escape(str(exc))}",
            parse_mode=ParseMode.HTML,
        )
        return
    if not confirm:
        await message.answer(
            f"{report}\n\nНичего не отправлено — это только предполёт. "
            f"Увести деньги: <code>{html_escape(_TEST_USAGE)} confirm</code>",
            parse_mode=ParseMode.HTML,
        )
        return

    import app.ton_pay as _tp

    dest = settings.active_treasury_address
    memo = f"{_TEST_MEMO_PREFIX} {int(datetime.now(UTC).timestamp())}"
    nano = int(amount * 1_000_000_000)
    await message.answer(
        f"{report}\n\n⏳ Отправляю… memo ищу в истории казначея до 30 с.",
        parse_mode=ParseMode.HTML,
    )
    engaged_before = _tp.state._http_channel_engaged_at
    try:
        # Лок диспетчера обязателен: цикл очереди держит один seqno на пачку,
        # параллельный перевод с тем же seqno молча потерялся бы.
        async with _tp.dispatch_lock():
            if force_http:
                marker = await _tp._send_ton_transfer_http(dest, nano, memo)
            else:
                marker = await _tp.send_ton_transfer(dest, nano, memo)
    except Exception as exc:
        await message.answer(
            f"{warn_mark('nopay')} Отправка не удалась: {html_escape(str(exc))}",
            parse_mode=ParseMode.HTML,
        )
        return
    finally:
        if force_http:
            # Принудительная репетиция — не деградация: флаг «канал
            # задействован» возвращаю как был, иначе диспетчер ушёл бы
            # хранителю ложным «лайтсерверы недоступны» при живых лайтсерверах.
            _tp.state._http_channel_engaged_at = engaged_before

    if marker is None:
        await message.answer(
            f"{warn_mark('nopay')} Не отправлено: TON выключен или не задана "
            "мнемоника (send_ton_transfer вернул None).",
            parse_mode=ParseMode.HTML,
        )
        return
    tx = await _test_wait_memo(memo)
    lines = [
        f"{ok_mark()} Отправлено: <code>{marker}</code>",
        f"memo: <code>{html_escape(memo)}</code>",
    ]
    if tx:
        lines.append(f"✓ memo в истории казначея → tx <code>{html_escape(tx)}</code>")
    else:
        lines.append(
            "⚠ memo за 30 с в истории не нашёлся — это и есть риск sent vs confirmed: "
            "деньги могли уйти, а подтверждение не прийти. Глянуть: /treasury, /blockchain."
        )
    lines.append(
        "В /treasury движение попадёт в «self-переводы», а не в тревоги: "
        "баланс сдвинулся только на газ."
    )
    await message.answer("\n".join(lines), parse_mode=ParseMode.HTML)


@router.message(Command("payout"), F.chat.type == ChatType.PRIVATE)
async def cmd_payout(message: Message) -> None:
    """Ручной разбор одной выплаты: /payout <id> spam confirm|retry.

    «spam» гасит выплату безвозвратно — только refund (входящий перевод с
    рекламой, возврат которого не нужен), и только с явным словом confirm:
    случайное/мгновенное списание чужого приза недопустимо.

    Отдельная ветка `/payout test` — холостой перевод (репетиция отправки,
    см. _payout_test): та же защита «только хранитель», но деньги движутся
    только по явному confirm.
    """
    if message.from_user is None or message.from_user.id not in settings.admin_id_set:
        await message.answer("Команда только для хранителя игры.")
        return
    raw = (message.text or "").split()
    if len(raw) > 1 and raw[1].lower() == "test":
        await _payout_test(message, raw[2:])
        return
    parts = (message.text or "").lower().split()
    if (
        len(parts) not in (3, 4)
        or not parts[1].isdigit()
        or parts[2] not in {"spam", "retry"}
    ):
        await message.answer(
            "Формат: <code>/payout &lt;id&gt; spam confirm</code> — безвозвратно погасить "
            "пыльный входящий refund,\n"
            "<code>/payout &lt;id&gt; retry</code> — вернуть выплату в очередь.",
            parse_mode=ParseMode.HTML,
        )
        return
    payout_id, action = int(parts[1]), parts[2]
    if action == "spam" and parts[3:4] != ["confirm"]:
        await message.answer(
            f"{warn_mark('nopay')} Пометка спамом безвозвратна и возврата не создаёт: "
            "подтверди явно <code>/payout &lt;id&gt; spam confirm</code>.",
            parse_mode=ParseMode.HTML,
        )
        return
    from app.ton_pay import resolve_dead_payout

    async with SessionLocal() as session:
        try:
            new_status = await resolve_dead_payout(session, payout_id, action)
        except ValueError as exc:
            await message.answer(f"{warn_mark('nopay')} {exc}")
            return
    if new_status == "dismissed":
        await message.answer(f"{ok_mark(str(payout_id))} Выплата #{payout_id} помечена как спам: из очереди ушла, сбросу больше не мешает.")
    elif new_status == "pending":
        await message.answer(f"{ok_mark(str(payout_id))} Выплата #{payout_id} вернулась в очередь с нулевым счётом попыток.")
    else:
        await message.answer(f"{warn_mark('nopay')} Выплата #{payout_id} не найдена или уже отправлена.")


@router.message(Command("return"), F.chat.type == ChatType.PRIVATE)
async def cmd_return(message: Message) -> None:
    """Ручной возврат «не засчитанной» ставки хранителем: /return <id>."""
    if message.from_user is None or message.from_user.id not in settings.admin_id_set:
        await message.answer("Команда только для хранителя игры.")
        return
    parts = message.text.split()
    if len(parts) != 2 or not parts[1].isdigit():
        await message.answer(
            "Формат: <code>/return &lt;id&gt;</code> — id ставки из «↩️ Вернуть ставку» в /panel."
        )
        return
    from app.stakes import create_manual_refund

    async with SessionLocal() as session:
        result = await create_manual_refund(session, int(parts[1]))
    if result.startswith("возврат"):
        from app.ton_pay import dispatch_pending_payouts

        try:
            await dispatch_pending_payouts(bot=message.bot)
        except Exception:
            logger.exception("Кик диспетчера после ручного возврата не удался")
        await message.answer(f"{ok_mark(parts[1])} {result}.")
    else:
        await message.answer(f"{warn_mark('return')} {result}")


@router.message(Command("treasury"), F.chat.type == ChatType.PRIVATE)
async def cmd_treasury(message: Message) -> None:
    """Здоровье казначея одним сообщением: адрес, мнемоника, баланс,
    сверка пары мнемоника/адрес, очередь выплат. Только для хранителя."""
    if message.from_user is None or message.from_user.id not in settings.admin_id_set:
        await message.answer("Команда только для хранителя игры.")
        return
    from app.ton_pay import treasury_diagnostics

    try:
        await message.answer(await treasury_diagnostics(), parse_mode=ParseMode.HTML)
    except Exception as exc:
        logger.exception("Отчёт /treasury не собран")
        await message.answer(f"Отчёт не собрался: {exc}")


@router.message(Command("fundout"), F.chat.type == ChatType.PRIVATE)
async def cmd_fundout(message: Message) -> None:
    """Записать ручную раздачу Фонда Стаи в журнал (аудит, не двигает деньги).

    Реальный перевод делает обычная очередь выплат/ручной вывод с казначея;
    здесь фиксируется <сумма> и <зачем>, а баланс фонда уменьшается, чтобы
    прозрачный журнал и цифра фонда оставались честными. Только для хранителя.
    """
    if message.from_user is None or message.from_user.id not in settings.admin_id_set:
        await message.answer("Команда только для хранителя игры.")
        return
    args = message.text.split()
    if len(args) < 3:
        await message.answer("Формат: /fundout <Gram> <причина. Что купили/разыграли>")
        return
    try:
        amount_gram = float(args[1].replace(",", "."))
    except ValueError:
        await message.answer("Сумма должна быть числом в Gram.")
        return
    if amount_gram <= 0:
        await message.answer("Сумма должна быть положительной.")
        return
    note = " ".join(args[2:])[:180]

    from app.stakes import record_fund_dispense
    from app.ton_utils import to_nano

    async with SessionLocal() as session:
        result = await record_fund_dispense(session, to_nano(amount_gram), note)
    if result.startswith("сумма") or result.startswith("в фонде"):
        await message.answer(result)
        return
    await message.answer(f"✅ {result}. Реальный перевод — с казначея.")


async def _revenue_text() -> str:
    """Касса игры: ledger доходов из Income (для /revenue и пульта).

    Корректировки казны (manual_out/manual_in из /adjust) доходом не
    считаются — они видны в /treasury отдельной строкой.
    """
    from app.ops import MANUAL_IN_KIND, MANUAL_OUT_KIND

    now = datetime.now(UTC)
    month_start = now.replace(day=1, hour=0, minute=0, second=0, microsecond=0)
    revenue_kinds = Income.kind.notin_([MANUAL_OUT_KIND, MANUAL_IN_KIND])

    def _block(title: str, data) -> str:
        parts = []
        for kind, count, stars, nanotons in data:
            if kind == "stars":
                parts.append(f"⭐ {stars} ({count} оплат)")
            else:
                parts.append(f"💎 {from_nano(nanotons):.4f} Gram ({count} переводов)")
        return f"{title}: " + ("; ".join(parts) if parts else "пусто")

    async with SessionLocal() as session:
        month_rows = (
            await session.execute(
                select(
                    Income.kind,
                    func.count(),
                    func.coalesce(func.sum(Income.amount_stars), 0),
                    func.coalesce(func.sum(Income.amount_nanotons), 0),
                )
                .where(Income.created_at >= month_start, revenue_kinds)
                .group_by(Income.kind)
            )
        ).all()
        total_rows = (
            await session.execute(
                select(
                    Income.kind,
                    func.count(),
                    func.coalesce(func.sum(Income.amount_stars), 0),
                    func.coalesce(func.sum(Income.amount_nanotons), 0),
                )
                .where(revenue_kinds)
                .group_by(Income.kind)
            )
        ).all()
    return (
        f"{money_mark('revenue')} Касса игры\n"
        f"{_block('Месяц', month_rows)}\n{_block('Всего', total_rows)}"
    )


@router.message(Command("incoming"), F.chat.type == ChatType.PRIVATE)
async def cmd_incoming(message: Message) -> None:
    """Журнал входящих переводов казначея: откуда, сколько, чем стало.

    Только для хранителя. Источник — Income-леджер, куда watcher пишет
    каждый поступивший перевод (идемпотентно по хешу транзакции).
    """
    if message.from_user is None or message.from_user.id not in settings.admin_id_set:
        await message.answer("Команда только для хранителя игры.")
        return

    async with SessionLocal() as session:
        rows = (
            await session.execute(
                select(Income, Player.username, Player.first_name)
                .join(Player, Player.id == Income.player_id, isouter=True)
                .where(Income.kind == "ton")
                .order_by(Income.id.desc())
                .limit(15)
            )
        ).all()
    if not rows:
        await message.answer("Входящих переводов в журнале пока нет.")
        return
    lines = ["🧾 Входящие переводы казначея (последние 15):"]
    for income, username, first_name in rows:
        who = (
            (f"@{username}" if username else (first_name or f"id{income.player_id}"))
            if income.player_id
            else "неизвестный кошелёк"
        )
        stamp = income.created_at
        if stamp is not None:
            stamp = stamp if stamp.tzinfo else stamp.replace(tzinfo=UTC)
            when = f"{stamp:%d.%m %H:%M} UTC"
        else:
            when = "—"
        lines.append(
            f"#{income.id} · {when} · {from_nano(income.amount_nanotons):g} Gram · {who}\n"
            f"   {income.note} · tonscan.org/tx/{income.unit_ref}"
        )
    await message.answer("\n".join(lines))


@router.message(Command("stakes"), F.chat.type == ChatType.PRIVATE)
async def cmd_stakes(message: Message) -> None:
    """Все ставки текущего и вчерашнего дня: статус каждой."""
    if message.from_user is None or message.from_user.id not in settings.admin_id_set:
        await message.answer("Команда только для хранителя игры.")
        return
    async with SessionLocal() as session:
        latest = (
            await session.execute(
                select(Round).order_by(Round.day_index.desc()).limit(1)
            )
        ).scalar_one_or_none()
        if latest is None:
            await message.answer("Дней ещё нет.")
            return
        rows = (
            await session.execute(
                select(Stake, Player.username, Player.first_name)
                .join(Player, Player.id == Stake.player_id)
                .where(Stake.round_id == latest.id)
                .order_by(Stake.id.asc())
                .limit(30)
            )
        ).all()
    if not rows:
        await message.answer(f"Ставок за день {latest.day_index} нет.")
        return
    lines = [f"Ставки дня {latest.day_index} ({latest.status.value}):"]
    for stake, username, first_name in rows:
        who = username or first_name or f"игрок {stake.player_id}"
        state = {"confirmed": "✅", "pending": "⏳", "rejected": "↩️"}.get(
            stake.status, stake.status
        )
        lines.append(
            f"  {who}: {from_nano(stake.amount_nanotons):g} Gram {state}"
        )
    await message.answer("\n".join(lines))


@router.message(Command("revenue"), F.chat.type == ChatType.PRIVATE)
async def cmd_revenue(message: Message) -> None:
    """Касса игры для хранителя: ledger доходов из Income.

    Звёзды сверяются с балансом бота во Fragment, Gram — с историей казны.
    """
    if message.from_user is None or message.from_user.id not in settings.admin_id_set:
        await message.answer("Команда только для хранителя игры.")
        return
    await message.answer(await _revenue_text())


@router.message(Command("blockchain"), F.chat.type == ChatType.PRIVATE)
async def cmd_blockchain(message: Message) -> None:
    """Аудит блокчейн-контура: watcher, очередь выплат, казна, сверка истории."""
    if message.from_user is None or message.from_user.id not in settings.admin_id_set:
        await message.answer("Команда только для хранителя игры.")
        return
    from app.ton_pay import blockchain_diagnostics

    try:
        await message.answer(await blockchain_diagnostics())
    except Exception as exc:
        logger.exception("Отчёт /blockchain не собран")
        await message.answer(f"Не собрал отчёт: {exc}")


@router.message(Command("mirror"), F.chat.type == ChatType.PRIVATE)
async def cmd_mirror(message: Message) -> None:
    """Пересборка зеркала казны: /mirror reset confirm.

    Сбрасывает курсоры синка — следующий цикл пересканирует историю кошелька
    от головы к генезису и перепроверит тождество «Σ = баланс». Лекарство от
    глубокого рассинхрона (сбои индексаторов, реорги вглубь истории).
    """
    if message.from_user is None or message.from_user.id not in settings.admin_id_set:
        await message.answer("Команда только для хранителя игры.")
        return
    if message.text.split()[1:] != ["reset", "confirm"]:
        await message.answer(
            "Пересборка зеркала казны: /mirror reset confirm\n"
            "После сброса зеркало пересканирует историю и сверку запускает "
            "/treasury (бутстрап занимает несколько циклов синка)."
        )
        return
    from app.treasury_mirror import reset_treasury_mirror

    try:
        await message.answer(await reset_treasury_mirror())
    except Exception as exc:
        logger.exception("Сброс зеркала казны не выполнен")
        await message.answer(f"Не сбросил зеркало: {exc}")
