"""Memo-ячейка исходящего перевода: 32-битный нулевой op + utf8-комментарий.

Вынесена в отдельный модуль, чтобы http_channel и dispatch/memo импортировали
одну реализацию, не тянув друг друга (http_channel → dispatch/__init__ или
memo → http_channel создавали бы лишние зависимости ради одного хелпера).
"""

from __future__ import annotations


def _comment_cell(text: str):
    """Memo-ячейка для исходящего сообщения: 32-битный нулевой op + utf8 текст.

    Текст усекается до 120 символов: ячейка v3 стандарта TON-комментария.
    """
    from pytoniq_core import begin_cell

    return begin_cell().store_uint(0, 32).store_string(text[:120]).end_cell()
