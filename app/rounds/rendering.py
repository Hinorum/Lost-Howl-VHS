from __future__ import annotations

import logging
import secrets
from datetime import datetime

from sqlalchemy.ext.asyncio import AsyncSession

from app.models import WinRule

logger = logging.getLogger(__name__)

# Формат payload дня. v5: инлайн-день — глава, дилемма и карты рендерятся
# сразу целиком (dilemma обязателен: структура поста одна). После удаления
# сюжетного слоя обложка не генерируется, но маркер формата остаётся
# паспортом для материализации.
PREPARED_PAYLOAD_VERSION = 5

# Шаблон дня: без нейросети и арта день жил бы пустым. Три постоянные сцены,
# по которым стая голосует банком, — механика (счёт, жребий, выплаты) не
# зависит от их названия.
_TEMPLATE_CARDS: list[dict] = [
    {
        "position": 0,
        "title": "Сцена I",
        "consequence": "Стая выбрала первую сцену.",
        "tag": "care",
    },
    {
        "position": 1,
        "title": "Сцена II",
        "consequence": "Стая выбрала вторую сцену.",
        "tag": "care",
    },
    {
        "position": 2,
        "title": "Сцена III",
        "consequence": "Стая выбрала третью сцену.",
        "tag": "care",
    },
]


async def _plan_and_render(
    session: AsyncSession,
    day_index: int,
    opens_hint: datetime | None = None,
    entropy: str | None = None,
) -> dict:
    """Собирает день без сюжета: заголовок, три дороги и публичный закон.

    Сеть не трогается: никакой главы, арта и библии. Механика дня (банк,
    голоса TON, жребий при ничьей, выплаты) работает на этом шаблоне.

    entropy — «seqno:root_hash» мастерчейн-блока TON, упавшего в цепочку ДО
    открытия дня: правило дня выводится из него детерминированно (root_hash
    % 3), каждый игрок может проверить seqno в эксплорере и пересчитаь
    исход — жребий нельзя подогнать задним числом, оператор не выбирает
    правило под голосование. None (TON выключен / оба узла молчат) — фолбэк
    на локальный secrets-жребий, день живёт даже при недоступной сети.
    """
    rng = secrets.SystemRandom()
    rules = list(WinRule)
    if entropy and ":" in entropy:
        try:
            _seqno, root_hash = entropy.split(":", 1)
            rule = rules[int(root_hash, 16) % len(rules)]
            logger.debug(
                "Правило дня %s: жребий блока TON %s (root_hash …%s)",
                day_index,
                _seqno,
                root_hash[-8:],
            )
        except (TypeError, ValueError):
            logger.warning("Энтропия правила дня %s неразборчива — локальный жребий", entropy)
            rule = rng.choice(rules)
    else:
        rule = rng.choice(rules)
    cards_payload = [dict(card) for card in _TEMPLATE_CARDS]
    chapter_text = (
        f"День {day_index}. ПЛЕЙ — плёнка шелестит: у кадра три варианта, "
        "стая выбирает свой. Уцелеет один — его решит голос тех, кто "
        "не промолчал."
    )
    return {
        "v": PREPARED_PAYLOAD_VERSION,
        "day_index": day_index,
        "rule": rule.value,
        "rule_entropy": entropy,
        "chapter_title": f"День {day_index}",
        "chapter_text": chapter_text,
        # Шаблонный день обязан нести блок «что предстоит решить», как и день
        # кассеты: структура поста одна для всех (см. broadcast.status_text).
        "dilemma": (
            "Три сцены у эфира, и ни одна не подсказана: каким из кадров "
            "останется день — решит голос стаи, два других уйдут в коробку."
        ),
        "cards": cards_payload,
    }
