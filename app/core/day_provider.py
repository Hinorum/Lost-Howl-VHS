"""Контракт «источника дня» — общий словарь слоёв.

Движок (app.rounds.rendering) спрашивает провайдера дня, сюжетный слой
(app.story.bay) регистрирует своего. Реестр живёт здесь, а не в движке:
раньше сюжетный слой тянул app.rounds.rendering ради одной регистрации, и
вместе с новой нуждой в якоре забега это давало петлю
story -> rounds -> ops -> story.

Теперь слои знают только протокол и друг о друге не знают: движок
задаёт вопрос, сюжетный слой на него отвечает.

Провайдер получает от движка БАЗОВЫЙ payload (шаблонный день) и
возвращает его же, дополненную сюжетом. Закон дня (rule/rule_entropy)
остаётся за движком: кассета его не трогает — она подставляет главу и
три пути.
"""

from __future__ import annotations

from datetime import datetime
from typing import Protocol

from sqlalchemy.ext.asyncio import AsyncSession


class PlanProvider(Protocol):
    """Источник дня поверх шаблона (кассеты, будущие сюжетные слои).

    Провайдер обязан вернуть полный payload, но закон дня (rule/rule_entropy)
    остаётся за движком: шаблонный рендер приходит к провайдеру как основа.
    """

    async def __call__(
        self,
        session: AsyncSession,
        day_index: int,
        base: dict,
        opens_hint: datetime | None = None,
        entropy: str | None = None,
    ) -> dict: ...


_plan_provider: PlanProvider | None = None


def register_plan_provider(provider: PlanProvider) -> None:
    """Регистрирует источник дня. Контракт явный: без monkeypatch атрибутов."""
    global _plan_provider
    _plan_provider = provider


def unregister_plan_provider(provider: PlanProvider | None = None) -> None:
    """Снимает провайдера; с аргументом — только если зарегистрирован он же."""
    global _plan_provider
    if provider is None or _plan_provider is provider:
        _plan_provider = None


def get_plan_provider() -> PlanProvider | None:
    return _plan_provider
