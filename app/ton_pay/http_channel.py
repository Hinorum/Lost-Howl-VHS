"""HTTP-канал отправки выплат (fallback при мёртвых лайтсерверах).

ADNL/TCP до лайтсерверов может быть закрыт окружением (типовой тестнет под
файрволом: чтение через REST-индексаторы работает, а вещание — нет, payout
висит в очереди навсегда). Чтобы казна не «зависала», исходящие собираются
ОФФЛАЙН (чистая локальная математика + seqno/публичный ключ через Toncenter
v3 runGetMethod) и вещаются через HTTPS (Toncenter v2 jsonRPC sendBoc).

Жизненный цикл строки и анти-дубль по memo не меняются: диспетчер перед
повтором по-прежнему сверяется с историей исходящих.
"""
from __future__ import annotations

import asyncio
import base64
import logging
from datetime import UTC, datetime

from app.config import settings
from app.http_utils import get_http_client
from app.ton_codec import api_headers

from . import state
from . import wallet as _wallet_pkg

logger = logging.getLogger(__name__)


def is_liteserver_down(exc: BaseException) -> bool:
    """Сбой именно лайтсерверного канала, а не самой выплаты.

    «have no alive peers» — LiteBalancer не поднял ни одного пира;
    TimeoutError — зависло ADNL-рукопожатие/отклик (порт режется файрволом).
    В этих случаях уходим в HTTP-вещание. Прочие ошибки (пара мнемоника/адрес,
    отказ контракта, нехватка средств) — настоящие: их показываем как есть.
    """
    if isinstance(exc, asyncio.TimeoutError):
        return True
    return "no alive peers" in str(exc).lower()


def parse_run_method_seqno(data: dict) -> int | None:
    """seqno из ответа toncenter v3 runGetMethod (метод «seqno»).

    exit_code 0 → число из первой записи стека (значение бывает hex «0x…» и
    десятичным); иной exit_code — метод не выполнился (uninit/unactive либо
    чужая версия контракта) → None.
    """
    if data.get("exit_code") != 0:
        return None
    stack = data.get("stack") or []
    if not stack:
        return None
    value = (stack[0] or {}).get("value")
    if value is None:
        return None
    if isinstance(value, str):
        text = value.strip()
        try:
            return int(text, 16) if text.lower().startswith("0x") else int(text)
        except ValueError:
            return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


async def http_get_wallet_seqno(wallet, *, address: str | None = None) -> int:
    """seqno для оффлайн-подписи: чтение через Toncenter v3.

    address=None — казначей (статус сначала прощупывается fetch_account_state):
    активный аккаунт с развёрнутым кодом — число из get-метода «seqno» (единое
    имя для v4r2 и v5r1); нет контракта (uninit/unactive) — 0: внешнее сообщение
    с init задеплоит кошелёк и выполнится в одной транзакции; если казначёй
    активен, а seqno прочитать не удалось — падаем (слать «вслепую» нельзя).

    address задан — generic-путь (кошелёк игрока/e2e): статус не прощупывается,
    exit_code != 0 трактуется как uninit → 0 (send_wallet_transfer_http в этом
    случае разворачивает контракт init-external'ом). Для тест-сценария это
    достаточно: активный, но нечитаемый метод на игроке — редкость, и перевод
    просто не пройдёт подтверждение watcher'ом.
    """
    # Локальный импорт: fetch_account_state и http_post_with_retry ищутся
    # через app.ton_pay, чтобы monkeypatch.setattr(ton_pay, "fetch_account_state", ...)
    # из тестов доходил до реального вызова. Цикла на уровне модуля нет —
    # обращение происходит внутри функции.
    import app.ton_pay as _tp

    target = address or settings.active_treasury_address
    if address is None:
        try:
            _, status, _ = await _tp.fetch_account_state()
            active = status == "active"
        except Exception:
            status = None
            active = False
        if status is not None and not active:
            return 0
    url = f"{settings.active_toncenter_api_base.rstrip('/')}/api/v3/runGetMethod"
    headers = api_headers(settings.toncenter_api_key)
    client = get_http_client()
    response = await _tp.http_post_with_retry(
        client,
        url,
        json={"address": target, "method": "seqno", "stack": []},
        headers=headers,
    )
    response.raise_for_status()
    seqno = parse_run_method_seqno(response.json())
    if seqno is not None:
        return seqno
    if address is None and active:
        raise RuntimeError(
            "казначёй активен, но seqno не читается (toncenter runGetMethod "
            f"exit_code={response.json().get('exit_code')}) — отправка отложена"
        )
    return 0


async def _http_broadcast_external(wallet, seqno: int, internal_msg) -> None:
    """Подписывает внешнее сообщение оффлайн и вещает через Toncenter sendBoc.

    seqno == 0 (казна ещё не развёрнута) → сообщение несёт state_init и
    деплоит кошелёк той же транзакцией. Возвращает None; неуспех — исключение
    с текстом от провайдера.
    """
    transfer_msg = wallet.raw_create_transfer_msg(
        private_key=wallet.private_key,
        seqno=seqno,
        wallet_id=wallet.wallet_id,
        messages=[internal_msg],
    )
    external = wallet.create_external_msg(
        src=None,
        dest=wallet.address,
        state_init=wallet.state_init if seqno == 0 else None,
        body=transfer_msg,
    )
    boc_b64 = base64.b64encode(external.serialize().to_boc()).decode()
    url = f"{settings.active_toncenter_api_base.rstrip('/')}/api/v2/jsonRPC"
    headers = api_headers(settings.toncenter_api_key)
    client = get_http_client()
    # Делаем так же, как в http_get_wallet_seqno: ищем патч через app.ton_pay,
    # чтобы monkeypatch.setattr(ton_pay, "http_post_with_retry", ...) работал.
    import app.ton_pay as _tp

    response = await _tp.http_post_with_retry(
        client,
        url,
        json={"jsonrpc": "2.0", "id": 1, "method": "sendBoc", "params": {"boc": boc_b64}},
        headers=headers,
    )
    try:
        data = response.json()
    except ValueError:
        data = {}
    if response.status_code != 200 or not data.get("ok"):
        reason = str((data.get("error") if isinstance(data, dict) else None) or response.text)
        raise RuntimeError(f"Toncenter не принял сообщение ({response.status_code}): {reason[:160]}")


def _comment_cell(text: str):
    """Memo-ячейка для исходящего сообщения: 32-битный нулевой op + utf8 текст."""
    from pytoniq_core import begin_cell

    return begin_cell().store_uint(0, 32).store_string(text[:120]).end_cell()


async def _send_ton_transfer_http(dest_address: str, amount_nanotons: int, comment: str) -> str:
    """Оффлайн-подпись + вещание через HTTPS (fallback лайтсерверов).

    Держит батч-счётчик _batch_seqno тем же инвариантом, что и лайтсерверный
    путь: один seqno на пачку, локально наращивается только при УСПЕХЕ
    вещания. Два подряд перевода (приз + рейк) не получат одинаковый seqno.
    """
    wallet, _ = _wallet_pkg.build_offline_treasury_wallet()
    from pytoniq_core import Address

    if not settings.toncenter_api_key and not state._warned_no_toncenter_key:
        state._warned_no_toncenter_key = True
        logger.warning(
            "TONCENTER_API_KEY пуст: HTTP-канал работает под анонимным лимитом "
            "Toncenter — при частых выплатах возможны 429; задай ключ в .env"
        )
    if state._batch_seqno is None:
        state._batch_seqno = await http_get_wallet_seqno(wallet)
    internal_msg = wallet.create_wallet_internal_message(
        destination=Address(dest_address),
        value=amount_nanotons,
        body=_comment_cell(comment),
    )
    # Обращение через app.ton_pay, чтобы monkeypatch.setattr в тестах
    # (`ton_pay._http_broadcast_external`) доходил до реального вызова.
    import app.ton_pay as _tp

    await _tp._http_broadcast_external(wallet, state._batch_seqno, internal_msg)
    state._batch_seqno += 1
    if state._http_channel_engaged_at is None:
        state._http_channel_engaged_at = datetime.now(UTC)
    marker = f"bcast:{int(datetime.now(UTC).timestamp())}"
    logger.info(
        "Перевод %d нанотонов к …%s разослан через HTTP (toncenter, seqno=%d)",
        amount_nanotons,
        dest_address[-6:],
        state._batch_seqno - 1,
    )
    return marker


async def send_wallet_transfer_http(
    wallet, *, dest_address: str, amount_nanotons: int, comment: str
) -> str:
    """Оффлайн-подпись + HTTPS-вещание для ПРОИЗВОЛЬНОГО кошелька.

    Generic-путь HTTP-канала (кошелёк игрока из build_offline_wallet): seqno
    читается через runGetMethod по адресу кошелька, seqno==0 разворачивает
    контракт init-external'ом в одной транзакции. Возвращает метку bcast.
    """
    if not wallet.private_key:
        raise ValueError("Кошелёк без приватного ключа — оффлайн-подпись невозможна")
    from pytoniq_core import Address

    seqno = await http_get_wallet_seqno(
        wallet,
        address=wallet.address.to_str(is_user_friendly=False, is_bounceable=False, is_url_safe=True),
    )
    internal_msg = wallet.create_wallet_internal_message(
        destination=Address(dest_address),
        value=amount_nanotons,
        body=_comment_cell(comment),
    )
    # Через app.ton_pay — чтобы monkeypatch.setattr(ton_pay, "_http_broadcast_external", ...) в тестах
    # доходил до реального вызова.
    import app.ton_pay as _tp

    await _tp._http_broadcast_external(wallet, seqno, internal_msg)
    marker = f"bcast:{int(datetime.now(UTC).timestamp())}"
    logger.info(
        "Перевод %d нанотонов к …%s разослан через HTTP (toncenter, seqno=%d)",
        amount_nanotons,
        dest_address[-6:],
        seqno,
    )
    return marker
