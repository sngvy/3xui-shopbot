"""Middleware'и aiogram: проверка бана пользователя перед обработкой апдейтов."""

import logging
import time
from typing import Any, Awaitable, Callable, Dict

from aiogram import BaseMiddleware
from aiogram.filters import BaseFilter
from aiogram.types import CallbackQuery, Message, TelegramObject
from aiogram.utils.keyboard import InlineKeyboardBuilder

from shop_bot.data_manager.database import (
    clear_bot_blocked,
    get_setting,
    get_user,
    is_admin,
    is_in_external_blacklist,
)

logger = logging.getLogger(__name__)


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
        # Пользователь снова пишет боту — значит, блокировку он снял
        if user_data and user_data.get("bot_blocked"):
            clear_bot_blocked(user.id)
        # Незарегистрированный пользователь из внешнего чёрного списка (ban-vpn) блокируется
        # до регистрации, иначе он успел бы взять триал до ближайшей сверки.
        if user_data is None and not is_admin(user.id) and is_in_external_blacklist(user.id):
            user_data = {"is_banned": True}
        if user_data and user_data.get("is_banned"):
            ban_message_text = "🚫 <b>Вы заблокированы и не можете использовать этого бота</b>"
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
                kb_builder.button(text="🛟 Написать в поддержку", url=url)
            else:
                kb_builder.button(text="🛟 Поддержка", callback_data="show_help")
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


# Callback'и, которые тянут данные с панелей X-UI, создают счета в платёжках или
# записи в БД. На них, помимо общего лимита, действует пауза между повторами:
# иначе несколько нажатий подряд превращаются в десятки запросов к панелям.
MAIN_BOT_HEAVY_CALLBACKS = (
    "show_key_",
    "regen_key_confirm_",
    "get_trial",
    "xkeen_core_",
    "xkeen_pick_",
    "show_podkop_keys_",
    "podkop_pick_",
    "show_raw_keys_",
    "rawkeys_pick_",
    "add_to_",
    "open_web_app_",
    "switch_server_",
    "select_host_switch_",
    "show_qr_",
    "pay_",
    "topup_pay_",
    "check_yoomoney_",
)


class _Bucket:
    __slots__ = ("tokens", "ts", "heavy", "notice_ts")

    def __init__(self, tokens: float, ts: float):
        self.tokens = tokens
        self.ts = ts
        self.heavy: Dict[str, float] = {}
        self.notice_ts = float("-inf")


class ThrottlingMiddleware(BaseMiddleware):
    """Антифлуд: «корзина токенов» на пользователя + пауза для тяжёлых callback'ов.

    Работает на message и callback_query (регистрировать одним экземпляром на оба
    наблюдателя, чтобы лимит был общий). Никогда не отбрасывает сообщения об
    успешной оплате (successful_payment), чтобы не потерять платёж. Администраторы
    не ограничиваются. Отбрасываемый callback получает короткое уведомление, но не
    чаще одного раза в notice_interval — иначе сами ответы флудеру стали бы нагрузкой.
    """

    def __init__(
        self,
        *,
        rate: float = 2.0,
        burst: int = 6,
        heavy_prefixes: tuple = (),
        heavy_cooldown: float = 1.5,
        notice_interval: float = 3.0,
        exempt_admins: bool = True,
        clock: Callable[[], float] = time.monotonic,
    ):
        self._rate = float(rate)
        self._burst = float(burst)
        self._heavy_prefixes = tuple(heavy_prefixes)
        self._heavy_cooldown = float(heavy_cooldown)
        self._notice_interval = float(notice_interval)
        self._exempt_admins = exempt_admins
        self._clock = clock
        self._users: Dict[int, _Bucket] = {}
        self._idle_ttl = 600.0
        self._max_users = 5000

    def _prune(self, now: float) -> None:
        for uid in [u for u, b in self._users.items() if now - b.ts > self._idle_ttl]:
            self._users.pop(uid, None)
        if len(self._users) >= self._max_users:
            oldest = sorted(self._users, key=lambda u: self._users[u].ts)
            for uid in oldest[: max(1, len(oldest) // 10)]:
                self._users.pop(uid, None)

    def _bucket(self, user_id: int, now: float) -> _Bucket:
        bucket = self._users.get(user_id)
        if bucket is None:
            if len(self._users) >= self._max_users:
                self._prune(now)
            bucket = _Bucket(self._burst, now)
            self._users[user_id] = bucket
        return bucket

    def _heavy_prefix(self, event: TelegramObject):
        if not self._heavy_prefixes or not isinstance(event, CallbackQuery):
            return None
        data = event.data or ""
        for prefix in self._heavy_prefixes:
            if data.startswith(prefix):
                return prefix
        return None

    def _check(self, bucket: _Bucket, event: TelegramObject, now: float):
        """None — событие разрешено (токен списан); иначе причина отказа."""
        elapsed = max(0.0, now - bucket.ts)
        bucket.tokens = min(self._burst, bucket.tokens + elapsed * self._rate)
        bucket.ts = now
        if bucket.tokens < 1.0:
            return "rate"
        prefix = self._heavy_prefix(event)
        if prefix is not None:
            last = bucket.heavy.get(prefix)
            if last is not None and now - last < self._heavy_cooldown:
                return "heavy"
            bucket.heavy[prefix] = now
        bucket.tokens -= 1.0
        return None

    async def _notify(self, event: TelegramObject, user_id: int, bucket: _Bucket, now: float, reason: str) -> None:
        if now - bucket.notice_ts < self._notice_interval:
            return
        bucket.notice_ts = now
        logger.info(f"Антифлуд: пользователь {user_id} ограничен ({reason}).")
        try:
            if isinstance(event, CallbackQuery):
                await event.answer("⏳ Слишком часто, подождите пару секунд", show_alert=False)
            elif isinstance(event, Message):
                await event.answer("⏳ Слишком много сообщений, подождите немного")
        except Exception as e:
            logger.debug(f"Антифлуд: не удалось отправить уведомление пользователю {user_id}: {e}")

    async def __call__(
        self,
        handler: Callable[[TelegramObject, Dict[str, Any]], Awaitable[Any]],
        event: TelegramObject,
        data: Dict[str, Any],
    ) -> Any:
        user = data.get("event_from_user")
        if user is None:
            return await handler(event, data)
        if isinstance(event, Message) and getattr(event, "successful_payment", None):
            return await handler(event, data)

        now = self._clock()
        bucket = self._bucket(user.id, now)
        reason = self._check(bucket, event, now)
        if reason is None:
            return await handler(event, data)
        if self._exempt_admins and is_admin(user.id):
            return await handler(event, data)

        await self._notify(event, user.id, bucket, now, reason)
        return None


class AdminOnlyFilter(BaseFilter):
    """Default-deny для admin_router: события обрабатываются только от администраторов.

    Вешается на роутер целиком (router.message.filter / router.callback_query.filter),
    поэтому забытая проверка is_admin в отдельном хендлере больше не открывает
    админ-функцию всем. Исключение — callback "noop": эту кнопку-заглушку
    («Подписок нет») показывают и обычным пользователям.
    """

    PUBLIC_CALLBACKS = frozenset({"noop"})

    async def __call__(self, event: TelegramObject) -> bool:
        if isinstance(event, CallbackQuery) and event.data in self.PUBLIC_CALLBACKS:
            return True
        user = getattr(event, "from_user", None)
        return bool(user and is_admin(user.id))
