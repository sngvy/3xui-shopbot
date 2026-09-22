"""Middleware'и aiogram: проверка бана пользователя перед обработкой апдейтов."""

from typing import Any, Awaitable, Callable, Dict

from aiogram import BaseMiddleware
from aiogram.types import CallbackQuery, Message, TelegramObject
from aiogram.utils.keyboard import InlineKeyboardBuilder

from shop_bot.data_manager.database import get_setting, get_user


class BanMiddleware(BaseMiddleware):
    """Блокирует обработку апдейтов от забаненных пользователей."""

    async def __call__(
        self,
        handler: Callable[[TelegramObject, Dict[str, Any]], Awaitable[Any]],
        event: TelegramObject,
        data: Dict[str, Any],
    ) -> Any:
        """Пропускает апдейт дальше, если пользователь не забанен, иначе отвечает отказом."""
        user = data.get("event_from_user")
        if not user:
            return await handler(event, data)

        user_data = get_user(user.id)
        if user_data and user_data.get("is_banned"):
            ban_message_text = "🚫 Вы заблокированы и не можете использовать этого бота."
            # Соберём клавиатуру поддержки без кнопки "Назад в меню"
            try:
                support = (
                    get_setting("support_bot_username") or get_setting("support_user") or ""
                ).strip()
            except Exception:
                support = ""
            kb_builder = InlineKeyboardBuilder()
            url: str | None = None
            if support:
                if support.startswith("@"):  # @username
                    url = f"tg://resolve?domain={support[1:]}"
                elif support.startswith("tg://"):
                    url = support
                elif support.startswith("http://") or support.startswith("https://"):
                    try:
                        part = support.split("/")[-1].split("?")[0]
                        if part:
                            url = f"tg://resolve?domain={part}"
                    except Exception:
                        url = support
                else:
                    url = f"tg://resolve?domain={support}"
            if url:
                kb_builder.button(text="🆘 Написать в поддержку", url=url)
            else:
                kb_builder.button(text="🆘 Поддержка", callback_data="show_help")
            ban_kb = kb_builder.as_markup()

            if isinstance(event, CallbackQuery):
                # Показать алерт и дополнительно отправить сообщение с кнопкой поддержки
                await event.answer(ban_message_text, show_alert=True)
                try:
                    await event.bot.send_message(
                        chat_id=event.from_user.id,
                        text=ban_message_text,
                        reply_markup=ban_kb,
                    )
                except Exception:
                    pass
            elif isinstance(event, Message):
                try:
                    await event.answer(ban_message_text, reply_markup=ban_kb)
                except Exception:
                    # Фолбэк без клавиатуры
                    await event.answer(ban_message_text)
            return

        return await handler(event, data)
