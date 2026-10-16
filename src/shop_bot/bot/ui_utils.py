"""Общие помощники интерфейса: меньше новых сообщений, больше правок на месте.

Модуль не зависит от клавиатур и хендлеров, поэтому его одинаково используют
основной бот, админка и отдельный бот поддержки.
"""

from __future__ import annotations

import html
import logging
import re
from typing import Any

from aiogram import BaseMiddleware, types
from aiogram.exceptions import TelegramBadRequest
from aiogram.fsm.context import FSMContext

logger = logging.getLogger(__name__)

# Ключ в FSM-данных: [chat_id, message_id] сообщения-запроса, которое правим
# при ответах пользователя (ошибка ввода, следующий шаг, итог) вместо
# отправки новых сообщений.
UI_ANCHOR_KEY = "ui_anchor"

_TAG_RE = re.compile(r"<[^>]+>")
_TOAST_LIMIT = 200  # лимит Telegram на текст всплывашки


def _toast_text(text: str) -> str:
    """Всплывашка — обычный текст: без HTML-тегов, не длиннее 200 символов."""
    plain = html.unescape(_TAG_RE.sub("", text or "")).strip()
    if len(plain) > _TOAST_LIMIT:
        plain = plain[: _TOAST_LIMIT - 1] + "…"
    return plain


async def edit_or_send(
    callback: types.CallbackQuery,
    text: str,
    reply_markup: Any = None,
    parse_mode: str | None = "HTML",
    disable_web_page_preview: bool | None = None,
) -> types.Message | None:
    """Правит сообщение, к которому привязан callback; если не вышло
    (сообщение недоступно, удалено, слишком старое) — отправляет новое.

    Аналог safe_edit_text основного бота, но без привязки к его клавиатурам.
    """
    message = callback.message
    if message is None or isinstance(message, types.InaccessibleMessage):
        chat_id = message.chat.id if message is not None else callback.from_user.id
        return await callback.bot.send_message(
            chat_id=chat_id,
            text=text,
            reply_markup=reply_markup,
            parse_mode=parse_mode,
            disable_web_page_preview=disable_web_page_preview,
        )
    try:
        result = await message.edit_text(
            text,
            reply_markup=reply_markup,
            parse_mode=parse_mode,
            disable_web_page_preview=disable_web_page_preview,
        )
        return result if isinstance(result, types.Message) else message
    except TelegramBadRequest as e:
        if "message is not modified" in str(e).lower():
            return message
        logger.debug(f"edit_or_send: правка не удалась, шлю новое сообщение: {e}")
        return await callback.bot.send_message(
            chat_id=message.chat.id,
            text=text,
            reply_markup=reply_markup,
            parse_mode=parse_mode,
            disable_web_page_preview=disable_web_page_preview,
        )


async def answer_error(
    callback: types.CallbackQuery,
    text: str,
    reply_markup: Any = None,
    answered: bool = False,
) -> None:
    """Короткая ошибка без нового сообщения.

    Если на callback ещё не отвечали — всплывашка (show_alert=False).
    Если уже отвечали (answered=True) или всплывашка не прошла — Telegram не
    даёт показать вторую, поэтому правим текущее сообщение: текст ошибки и
    кнопка возврата (reply_markup).
    """
    if not answered:
        try:
            await callback.answer(_toast_text(text), show_alert=False)
            return
        except TelegramBadRequest as e:
            logger.debug(f"answer_error: всплывашка не прошла ({e}), правлю сообщение")
    await edit_or_send(callback, text, reply_markup=reply_markup)


async def ui_remember(state: FSMContext, message: types.Message | None) -> None:
    """Запоминает сообщение-запрос, которое будем править в ответах на ввод."""
    if message is None:
        return
    try:
        await state.update_data(**{UI_ANCHOR_KEY: [message.chat.id, message.message_id]})
    except Exception as e:  # состояние — удобство, а не критичная часть
        logger.debug(f"ui_remember: не удалось сохранить якорь: {e}")


async def ui_anchor(state: FSMContext) -> list[int] | None:
    """Достаёт запомненный якорь (до state.clear(), который его сотрёт)."""
    try:
        anchor = (await state.get_data()).get(UI_ANCHOR_KEY)
    except Exception:
        return None
    if isinstance(anchor, (list, tuple)) and len(anchor) == 2:
        return [int(anchor[0]), int(anchor[1])]
    return None


async def ui_reply(
    message: types.Message,
    state: FSMContext,
    text: str,
    reply_markup: Any = None,
    parse_mode: str | None = "HTML",
    anchor: list[int] | None = None,
    **kwargs: Any,
) -> types.Message | None:
    """Ответ на текстовый ввод: правит сообщение-запрос, иначе шлёт новое.

    anchor можно передать явно, если состояние уже очищено (state.clear()).
    Если якоря нет или правка невозможна, отправляется обычное сообщение, а
    его id становится новым якорем — следующий ответ в этом сценарии
    отредактирует уже его.
    """
    if anchor is None:
        anchor = await ui_anchor(state)
    # Reply-клавиатуру (и прочее, кроме inline) через edit_message_text не передать —
    # такой ответ идёт обычным сообщением.
    if reply_markup is not None and not isinstance(reply_markup, types.InlineKeyboardMarkup):
        anchor = None
    if anchor:
        chat_id, message_id = anchor
        try:
            result = await message.bot.edit_message_text(
                text=text,
                chat_id=chat_id,
                message_id=message_id,
                reply_markup=reply_markup,
                parse_mode=parse_mode,
                **kwargs,
            )
            return result if isinstance(result, types.Message) else None
        except TelegramBadRequest as e:
            if "message is not modified" in str(e).lower():
                return None
            logger.debug(f"ui_reply: правка не удалась, шлю новое сообщение: {e}")
    sent = await message.answer(text, reply_markup=reply_markup, parse_mode=parse_mode, **kwargs)
    await ui_remember(state, sent)
    return sent


class UiAnchorMiddleware(BaseMiddleware):
    """Для callback-хендлеров: если после них включён FSM-сценарий, а хендлер сам
    якорь не ставил, якорем становится сообщение, на котором нажали кнопку
    (запрос ввода обычно показывается правкой этого сообщения)."""

    async def __call__(self, handler, event: types.CallbackQuery, data: dict):
        state: FSMContext | None = data.get("state")
        before = await ui_anchor(state) if state else None
        result = await handler(event, data)
        if state is None:
            return result
        try:
            if await state.get_state() and await ui_anchor(state) == before:
                msg = event.message
                if msg is not None and not isinstance(msg, types.InaccessibleMessage):
                    await ui_remember(state, msg)
        except Exception as e:
            logger.debug(f"Ошибка в UiAnchorMiddleware: {e}")
        return result
