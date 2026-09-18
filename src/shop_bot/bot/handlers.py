"""Обработчики Telegram-команд и колбэков для обычных пользователей бота (покупка, продление, профиль)."""

import asyncio
import base64
import hashlib
import http.client
import json
import logging
import os
import re
import ssl
import uuid
from datetime import datetime, timedelta
from decimal import ROUND_HALF_UP, Decimal
from functools import wraps
from io import BytesIO
from typing import Dict, Optional
from urllib.parse import quote, unquote, urlencode, urlparse

import aiohttp
import qrcode
from aiogram import Bot, F, Router, html, types
from aiogram.enums import ChatMemberStatus
from aiogram.exceptions import TelegramBadRequest
from aiogram.filters import CommandObject, CommandStart
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import (
    BufferedInputFile,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    WebAppInfo,
)
from aiogram.utils.keyboard import InlineKeyboardBuilder
from aiosend import CryptoPay
from yookassa import Payment

from shop_bot.bot import keyboards
from shop_bot.config import (
    CHOOSE_PAYMENT_METHOD_MESSAGE,
    CHOOSE_PLAN_MESSAGE,
    VPN_INACTIVE_TEXT,
    VPN_NO_DATA_TEXT,
    get_key_info_text,
    get_profile_text,
    get_purchase_success_text,
    get_vpn_active_text,
)
from shop_bot.data_manager.database import (
    add_new_key,
    add_support_message,
    add_to_balance,
    check_promo_code_available,
    create_pending_transaction,
    credit_referral_reward,
    deduct_from_balance,
    find_and_complete_pending_transaction,
    get_admin_ids,
    get_all_hosts,
    get_all_users,
    get_balance,
    get_key_by_email,
    get_key_by_id,
    get_next_key_number,
    get_plan_by_id,
    get_plans_for_host,
    get_referral_balance_all,
    get_referral_count,
    get_setting,
    get_ticket_by_thread,
    get_user,
    get_user_keys,
    get_user_transactions,
    is_admin,
    keys_in_transition,
    log_transaction,
    redeem_promo_code,
    register_user_if_not_exists,
    set_key_auto_renew,
    set_key_last_plan,
    set_referral_start_bonus_received,
    set_terms_agreed,
    set_trial_used,
    update_key_email,
    update_key_host_and_info,
    update_key_info,
    update_promo_code_status,
    update_user_stats,
)
from shop_bot.modules import xui_api

TELEGRAM_BOT_USERNAME = get_setting("telegram_bot_username")
PAYMENT_METHODS: dict = {}
ADMIN_ID = int(get_setting("admin_id")) if get_setting("admin_id") else None
CRYPTO_BOT_TOKEN = get_setting("cryptobot_token")

REDIR_URL = os.getenv("REDIR_URL")

logger = logging.getLogger(__name__)

# Задержка перед удалением клиента со старого хоста при смене сервера —
# отсчитывается от момента, когда пользователю уже доставлено сообщение
# с новой ссылкой (см. _switch_key_to_host / _delayed_delete_old_host).
# 5 минут — достаточно, чтобы вручную скопировать ссылку в клиент,
# и достаточно коротко, чтобы не держать два активных ключа подолгу.
OLD_HOST_DELETE_DELAY_SECONDS = 300


class KeyPurchase(StatesGroup):
    """Состояния FSM для сценария покупки нового ключа."""

    waiting_for_host_selection = State()
    waiting_for_plan_selection = State()


class Onboarding(StatesGroup):
    """Состояния FSM для первичного онбординга пользователя."""

    waiting_for_subscription_and_agreement = State()


class PaymentProcess(StatesGroup):
    """Состояния FSM для сценария оплаты (выбор способа, промокод, email)."""

    waiting_for_email = State()
    waiting_for_payment_method = State()
    waiting_for_promo_code = State()


class TopUpProcess(StatesGroup):
    """Состояния FSM для сценария пополнения баланса."""

    waiting_for_amount = State()
    waiting_for_topup_method = State()


class SupportDialog(StatesGroup):
    """Состояния FSM для диалога с поддержкой из основного бота."""

    waiting_for_subject = State()
    waiting_for_message = State()
    waiting_for_reply = State()


def is_valid_email(email: str) -> bool:
    """Проверяет строку на соответствие простому формату email."""
    pattern = r"^[a-zA-Z0-9_.+-]+@[a-zA-Z0-9-]+\.[a-zA-Z0-9-.]+$"
    return re.match(pattern, email) is not None


async def fetch_via_https(url: str, timeout: float = 15.0) -> tuple[int, bytes]:
    """Скачивает url строго по HTTPS через http.client, без aiohttp/curl.

    Общий хелпер для всех мест, где нужно скачать содержимое подписки/JSON-
    конфиг с внешнего сервера (xkeen-конфиг, Podkop, сырые ключи).

    Почему не aiohttp: его C-парсер (llhttp) заметно строже относится к
    формальному соответствию HTTP-ответа спеке, чем curl/браузеры — на
    некоторых серверах подписок это даёт ClientResponseError с бесполезным
    "0, message=''" (сама причина теряется из-за регрессии в aiohttp начиная
    с 3.9.4, github.com/aio-libs/aiohttp/issues/8395), хотя сервер отвечает
    полностью корректно.

    Почему не внешний curl-процесс: зависит от того, что реально стоит и
    как настроено в конкретном докер-образе — сборка curl, наличие в PATH,
    а главное — curl (как и aiohttp) по умолчанию сам подхватывает
    http_proxy/https_proxy из окружения контейнера, и через них можно
    незаметно для вызывающего кода потерять https, даже если URL был
    указан правильно.

    http.client.HTTPSConnection — не URL-строка, из которой что-то может
    ошибочно вывести схему или подхватить прокси, а отдельный класс,
    который умеет только TLS с первого байта и не читает proxy-переменные
    окружения вообще.

    Возвращает (http_status, тело_ответа_как_bytes). Поднимает ValueError,
    если url не https, и любые сетевые/TLS-исключения — как есть, вызывающий
    код сам решает, как их обрабатывать/логировать.
    """
    parsed = urlparse(url)
    if parsed.scheme != "https" or not parsed.hostname:
        raise ValueError(f"Ожидался https URL, получено: {url}")

    def _fetch() -> tuple[int, bytes]:
        ssl_context = ssl.create_default_context()
        conn = http.client.HTTPSConnection(
            parsed.hostname,
            parsed.port or 443,
            timeout=timeout,
            context=ssl_context,
        )
        try:
            path = parsed.path or "/"
            if parsed.query:
                path += f"?{parsed.query}"
            conn.request("GET", path, headers={"Host": parsed.hostname})
            resp = conn.getresponse()
            return resp.status, resp.read()
        finally:
            conn.close()

    return await asyncio.to_thread(_fetch)


def _force_https(url: str) -> str:
    """Принудительно переводит схему ссылки в https — та же защита, что и
    в fetch_via_https, но нужна отдельно там, где схему меняют ДО вызова
    (например, при подмене /sub/ на /json/ в самой строке URL)."""
    if url.startswith("http://"):
        return "https://" + url[len("http://") :]
    if not url.startswith("https://"):
        return "https://" + url.split("://", 1)[-1]
    return url


async def delete_and_send(
    message: types.Message,
    text: str,
    reply_markup=None,
    parse_mode: str = "HTML",
    disable_web_page_preview: bool | None = None,
) -> types.Message:
    """Удаляет message (то, к которому привязан callback, т.е. предыдущее
    сообщение бота) и отправляет новое — вместо edit_text.

    Используется только для сообщений без кнопок (статусы, промежуточные
    уведомления) — навигационные экраны с инлайн-клавиатурой по-прежнему
    редактируются на месте через edit_text, чтобы не мельтешить анимацией
    удаления/появления на каждое нажатие кнопки.

    Если удалить не получилось (сообщение уже удалено, старше 48 часов —
    у Telegram есть ограничение на удаление старых сообщений ботом, или
    у бота отняли права) — не падаем, просто отправляем новое сообщение
    без удаления старого.
    """
    chat_id = message.chat.id
    try:
        await message.delete()
    except Exception as e:
        logger.debug(f"delete_and_send: не удалось удалить сообщение {message.message_id}: {e}")

    return await message.bot.send_message(
        chat_id=chat_id,
        text=text,
        reply_markup=reply_markup,
        parse_mode=parse_mode,
        disable_web_page_preview=disable_web_page_preview,
    )


_TX_ICONS = {
    "top_up": "➕",
    "purchase": "🛒",
    "admin_credit": "➕",
    "admin_deduct": "➖",
    "referral": "🤝",
    "other": "💳",
}
# Знак суммы: пополнение, начисление админом и реферальные выплаты — приход; покупка и списание — расход.
_TX_IS_EXPENSE = {
    "top_up": False,
    "admin_credit": False,
    "purchase": True,
    "admin_deduct": True,
    "referral": False,
    "other": False,
}
_TX_METHOD_NAMES = {
    "balance": "с баланса",
    "yookassa": "картой",
    "cryptobot": "CryptoBot",
    "heleket": "криптовалютой",
    "yoomoney": "ЮMoney",
    "stars": "Telegram Stars",
    "ton": "TON",
    "admin": "администратором",
}


def format_transaction_line(tx: dict) -> str:
    """Форматирует одну запись истории операций в единую строку.
    Используется и в профиле пользователя, и в карточке пользователя для
    админа, чтобы формат менялся в одном месте, а не в двух копиях кода.
    """
    kind = tx.get("kind", "other")
    icon = _TX_ICONS.get(kind, "💳")
    sign = "-" if _TX_IS_EXPENSE.get(kind) else "+"
    amount = float(tx.get("amount_rub") or 0)
    try:
        date_str = datetime.fromisoformat(str(tx.get("created_date"))).strftime("%d.%m.%Y %H:%M")
    except Exception:
        date_str = str(tx.get("created_date") or "")
    method_raw = (tx.get("payment_method") or "").strip().lower()
    method_label = _TX_METHOD_NAMES.get(method_raw, tx.get("payment_method") or "")

    segments = [date_str, f"{icon} <b>{sign}{amount:.2f} ₽</b>"]

    if kind == "top_up":
        segments.append("Пополнение баланса")
        if method_label:
            segments.append(method_label)
    elif kind == "purchase":
        plan_name = tx.get("plan_name") or "подписка"
        segments.append(f"Покупка «{plan_name}»")
        host_name = tx.get("host_name")
        if host_name:
            segments.append(host_name)
        if method_label:
            segments.append(method_label)
    elif kind == "admin_credit":
        segments.append("Начисление администратором")
    elif kind == "admin_deduct":
        segments.append("Списание администратором")
    elif kind == "referral":
        segments.append("Реферальное вознаграждение")
    else:
        segments.append(method_label or "Операция")

    return " ▪️ ".join(segments)


async def safe_edit_text(
    callback: types.CallbackQuery,
    text: str,
    reply_markup=None,
    parse_mode: str = "HTML",
    disable_web_page_preview: bool | None = None,
) -> types.Message | None:
    """Замена прямого callback.message.edit_text(...) везде, где сообщение
    остаётся на месте (с инлайн-клавиатурой) — а не пересоздаётся через
    delete_and_send.

    Зачем: если исходное сообщение стало недоступно боту (устарело,
    Telegram отдаёт его как InaccessibleMessage — урезанный объект вообще
    без методов Message, только chat/message_id/date), обычный
    callback.message.edit_text(...) падает не ошибкой Telegram API,
    а голым AttributeError на уровне Python — потому что у объекта в
    принципе нет такого метода. Ловить это как обычное исключение вокруг
    edit_text не поможет, если сам вызов пишется как
    `await callback.message.edit_text(...)` — упадёт раньше, на
    получении атрибута.

    Здесь проверяем тип заранее и, если сообщение недоступно (или всё же
    попытка редактирования не удалась по каким-то причинам на стороне
    Telegram, например, оно успело устареть/удалиться между показом кнопки
    и нажатием) — просто отправляем новое сообщение в тот же чат, вместо
    падения на необработанном апдейте.

    Возвращает итоговое сообщение (как и обычный edit_text у aiogram,
    который на успехе отдаёт отредактированный Message) — некоторые
    вызывающие места сохраняют его message_id в FSM-состояние, чтобы
    потом гарантированно найти и удалить именно это сообщение. Раньше
    здесь всегда возвращалось None независимо от исхода — из-за этого
    падало message_id у None в вызывающем коде, который ожидал реальный
    объект (см. admin_search_user_start). None возвращается только если
    сообщение отправить/отредактировать не удалось вообще никак — в
    этом случае вызывающему коду всё равно нечего сохранять.
    """
    message = callback.message
    if isinstance(message, types.InaccessibleMessage) or message is None:
        return await callback.bot.send_message(
            chat_id=message.chat.id if message else callback.from_user.id,
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
        # edit_text у aiogram по спеке Telegram возвращает либо
        # отредактированный Message, либо True (для инлайн-сообщений —
        # у нас такого не бывает, всегда обычные сообщения в чате, но на
        # всякий случай подстрахуемся и вернём исходное message, а не True)
        return result if isinstance(result, types.Message) else message
    except TelegramBadRequest as e:
        if "message is not modified" in str(e).lower():
            return message
        logger.debug(f"safe_edit_text: edit_text не удался, шлю новое сообщение: {e}")
        return await callback.bot.send_message(
            chat_id=message.chat.id,
            text=text,
            reply_markup=reply_markup,
            parse_mode=parse_mode,
            disable_web_page_preview=disable_web_page_preview,
        )


async def show_main_menu(message: types.Message, edit_message: bool = False):
    """Отправляет (или редактирует) сообщение с главным меню бота для пользователя."""
    user_id = message.chat.id
    user_db_data = await asyncio.to_thread(get_user, user_id)
    user_keys = await asyncio.to_thread(get_user_keys, user_id)

    trial_available = not (user_db_data and user_db_data.get("trial_used"))
    is_admin_flag = await asyncio.to_thread(is_admin, user_id)

    custom_main_text = await asyncio.to_thread(get_setting, "main_menu_text")
    text = custom_main_text or "🏠 <b>Главное меню</b>\n\nВыберите действие:"
    keyboard = keyboards.create_main_menu_keyboard(user_keys, trial_available, is_admin_flag)
    # Отправляем только текст без фотографии
    if edit_message:
        try:
            await message.edit_text(text, reply_markup=keyboard)
        except TelegramBadRequest:
            pass
    else:
        await message.answer(text, reply_markup=keyboard)


async def process_successful_onboarding(callback: types.CallbackQuery, state: FSMContext):
    """Завершает онбординг: ставит флаг согласия и открывает главное меню."""
    user_id = callback.from_user.id
    try:
        set_terms_agreed(user_id)
    except Exception as e:
        logger.error(f"Не удалось выполнить set_terms_agreed для пользователя {user_id}: {e}")
    try:
        await callback.answer()
    except Exception:
        pass
    try:
        await show_main_menu(callback.message, edit_message=True)
    except Exception:
        try:
            await callback.message.answer("✅ Требования выполнены. Открываю меню...")
        except Exception:
            pass
    try:
        await state.clear()
    except Exception:
        pass


def registration_required(f):
    """Декоратор: пропускает обработчик, только если пользователь уже зарегистрирован (/start)."""

    @wraps(f)
    async def decorated_function(event: types.Update, *args, **kwargs):
        user_id = event.from_user.id
        user_data = await asyncio.to_thread(get_user, user_id)
        if user_data:
            return await f(event, *args, **kwargs)
        else:
            message_text = "⚠️ Пожалуйста, для начала работы со мной, отправьте команду /start"
            if isinstance(event, types.CallbackQuery):
                await event.answer(message_text, show_alert=True)
            else:
                await event.answer(message_text)

    return decorated_function


def get_user_router() -> Router:
    """Собирает и возвращает Router со всеми обработчиками для обычных пользователей."""
    user_router = Router()

    # Helpers for Telegram Stars
    def _get_stars_rate() -> Decimal:
        try:
            rate_raw = get_setting("stars_per_rub") or "1"
            rate = Decimal(str(rate_raw))
            if rate <= 0:
                rate = Decimal("1")
            return rate
        except Exception:
            return Decimal("1")

    def _calc_stars_amount(amount_rub: Decimal) -> int:
        rate = _get_stars_rate()
        try:
            stars = (amount_rub * rate).quantize(Decimal("1"), rounding=ROUND_HALF_UP)
        except Exception:
            stars = amount_rub * rate
        try:
            return int(stars)
        except Exception:
            return int(float(stars))

    @user_router.message(CommandStart())
    async def start_handler(
        message: types.Message, state: FSMContext, bot: Bot, command: CommandObject
    ):
        user_id = message.from_user.id
        username = message.from_user.username or message.from_user.full_name
        referrer_id = None

        if command.args and command.args.startswith("ref_"):
            try:
                potential_referrer_id = int(command.args.split("_")[1])
                if potential_referrer_id != user_id:
                    referrer_id = potential_referrer_id
                    logger.info(
                        f"Новый пользователь {user_id} приглашён пользователем {referrer_id}"
                    )
            except (IndexError, ValueError):
                logger.warning(f"Получен некорректный реферальный код: {command.args}")

        is_new_user = await asyncio.to_thread(
            register_user_if_not_exists, user_id, username, referrer_id
        )
        user_id = message.from_user.id
        username = message.from_user.username or message.from_user.full_name
        user_data = await asyncio.to_thread(get_user, user_id)

        # Бонус при старте для пригласившего (fixed_start_referrer): единоразово, когда новый пользователь запускает бота по реферальной ссылке
        try:
            reward_type = (get_setting("referral_reward_type") or "percent_purchase").strip()
        except Exception:
            reward_type = "percent_purchase"
        if reward_type == "fixed_start_referrer" and referrer_id and is_new_user:
            try:
                amount_raw = get_setting("referral_on_start_referrer_amount") or "20"
                start_bonus = Decimal(str(amount_raw)).quantize(Decimal("0.01"))
            except Exception:
                start_bonus = Decimal("20.00")
            if start_bonus > 0:
                # Атомарно: и баланс, и накопительный referral_balance_all
                # одним запросом, чтобы не было рассинхронизации (см. credit_referral_reward).
                try:
                    ok = credit_referral_reward(int(referrer_id), float(start_bonus))
                except Exception as e:
                    logger.warning(
                        f"Реферальный бонус при старте: не удалось начислить вознаграждение пригласившему {referrer_id}: {e}"
                    )
                    ok = False
                if ok:
                    try:
                        referrer_info = await asyncio.to_thread(get_user, int(referrer_id))
                        log_username = (
                            referrer_info.get("username", "N/A") if referrer_info else "N/A"
                        )
                        log_transaction(
                            username=log_username,
                            transaction_id=None,
                            payment_id=str(uuid.uuid4()),
                            user_id=int(referrer_id),
                            status="internal",
                            amount_rub=float(start_bonus),
                            amount_currency=None,
                            currency_name=None,
                            payment_method="Referral",
                            metadata=json.dumps(
                                {
                                    "action": "referral_reward",
                                    "referral_type": "start",
                                    "from_user_id": user_id,
                                }
                            ),
                        )
                    except Exception:
                        pass
                # Помечаем, что для этого нового пользователя старт уже обработан, чтобы не дублировать при повторном /start
                try:
                    set_referral_start_bonus_received(user_id)
                except Exception:
                    pass
                # Уведомим пригласившего
                try:
                    safe_full_name = html.quote(message.from_user.full_name)
                    await bot.send_message(
                        chat_id=int(referrer_id),
                        text=(
                            "💰 <b>Начисление за приглашение!</b>\n\n"
                            f"👤 <b>Новый пользователь:</b> {safe_full_name} (ID: {user_id})\n"
                            f"💸 <b>Бонус:</b> {float(start_bonus):.2f} ₽"
                        ),
                        parse_mode="HTML",
                    )
                except Exception:
                    pass

        if user_data and user_data.get("agreed_to_terms"):
            # -------------------------------------------------------------------------
            # 1. Динамическое определение времени суток для приветствия
            # -------------------------------------------------------------------------
            from datetime import datetime

            current_hour = datetime.now().hour

            if 5 <= current_hour < 12:
                greeting = f"🌅 Доброе утро, {html.bold(message.from_user.full_name)}!"
            elif 12 <= current_hour < 18:
                greeting = f"☀️ Добрый день, {html.bold(message.from_user.full_name)}!"
            elif 18 <= current_hour < 23:
                greeting = f"🌅 Добрый вечер, {html.bold(message.from_user.full_name)}!"
            else:
                greeting = f"🌙 Доброй ночи, {html.bold(message.from_user.full_name)}!"

            # -------------------------------------------------------------------------
            # 2. Отправка сообщения и вызов главного меню
            # -------------------------------------------------------------------------
            await message.answer(greeting, reply_markup=keyboards.main_reply_keyboard)
            await show_main_menu(message)
            return

        terms_url = get_setting("terms_url")
        privacy_url = get_setting("privacy_url")
        channel_url = get_setting("channel_url")

        if not channel_url and (not terms_url or not privacy_url):
            set_terms_agreed(user_id)
            await show_main_menu(message)
            return

        is_subscription_forced = get_setting("force_subscription") == "true"

        show_welcome_screen = (is_subscription_forced and channel_url) or (
            terms_url and privacy_url
        )

        if not show_welcome_screen:
            set_terms_agreed(user_id)
            await show_main_menu(message)
            return

        welcome_parts = ["<b>Добро пожаловать!</b>\n"]

        if is_subscription_forced and channel_url:
            welcome_parts.append(
                "Для доступа ко всем функциям, пожалуйста, подпишитесь на наш канал"
            )

        if terms_url and privacy_url:
            welcome_parts.append(
                "Также необходимо ознакомиться и принять наши "
                f"<a href='{terms_url}'>Условия использования</a> и "
                f"<a href='{privacy_url}'>Политику конфиденциальности</a>."
            )

        welcome_parts.append("\nПосле этого нажмите кнопку ниже")
        final_text = "\n".join(welcome_parts)

        await message.answer(
            final_text,
            reply_markup=keyboards.create_welcome_keyboard(
                channel_url=channel_url, is_subscription_forced=is_subscription_forced
            ),
            disable_web_page_preview=True,
        )
        await state.set_state(Onboarding.waiting_for_subscription_and_agreement)

    @user_router.callback_query(
        Onboarding.waiting_for_subscription_and_agreement,
        F.data == "check_subscription_and_agree",
    )
    async def check_subscription_handler(
        callback: types.CallbackQuery, state: FSMContext, bot: Bot
    ):
        user_id = callback.from_user.id
        channel_url = get_setting("channel_url")
        is_subscription_forced = get_setting("force_subscription") == "true"

        if not is_subscription_forced or not channel_url:
            await process_successful_onboarding(callback, state)
            return

        try:
            if "@" not in channel_url and "t.me/" not in channel_url:
                logger.error(
                    f"Неверный формат URL канала: {channel_url}. Пропускаем проверку подписки"
                )
                await process_successful_onboarding(callback, state)
                return

            channel_id = "@" + channel_url.split("/")[-1] if "t.me/" in channel_url else channel_url
            member = await bot.get_chat_member(chat_id=channel_id, user_id=user_id)

            if member.status in [
                ChatMemberStatus.MEMBER,
                ChatMemberStatus.ADMINISTRATOR,
                ChatMemberStatus.CREATOR,
            ]:
                await process_successful_onboarding(callback, state)
            else:
                await callback.answer(
                    "⚠️ Вы еще не подписались на канал. Пожалуйста, подпишитесь и попробуйте снова",
                    show_alert=True,
                )

        except Exception as e:
            logger.error(
                f"Ошибка при проверке подписки для user_id {user_id} на канал {channel_url}: {e}"
            )
            await callback.answer(
                "❌ Не удалось проверить подписку. Убедитесь, что бот является администратором канала. Попробуйте позже",
                show_alert=True,
            )

    @user_router.message(Onboarding.waiting_for_subscription_and_agreement)
    async def onboarding_fallback_handler(message: types.Message):
        try:
            await message.delete()
        except Exception:
            pass
        await message.answer(
            "⚠️ Пожалуйста, выполните требуемые действия и нажмите на кнопку в сообщении выше"
        )

    @user_router.message(F.text == "🏠 Главное меню")
    @registration_required
    async def main_menu_handler(message: types.Message, state: FSMContext):
        await state.clear()
        try:
            await message.delete()
        except Exception:
            pass
        await show_main_menu(message)

    @user_router.callback_query(F.data == "back_to_main_menu")
    @registration_required
    async def back_to_main_menu_handler(callback: types.CallbackQuery):
        await callback.answer()
        await show_main_menu(callback.message, edit_message=True)

    @user_router.callback_query(F.data == "show_main_menu")
    @registration_required
    async def show_main_menu_cb(callback: types.CallbackQuery):
        await callback.answer()
        await show_main_menu(callback.message, edit_message=True)

    @user_router.callback_query(F.data == "show_profile")
    @registration_required
    async def profile_handler_callback(callback: types.CallbackQuery):
        await callback.answer()
        user_id = callback.from_user.id
        user_db_data = await asyncio.to_thread(get_user, user_id)
        user_keys = await asyncio.to_thread(get_user_keys, user_id)
        if not user_db_data:
            await callback.answer("❌ Не удалось получить данные профиля", show_alert=False)
            return
        username = html.bold(user_db_data.get("username", "Пользователь"))
        total_spent, total_months = user_db_data.get("total_spent", 0), user_db_data.get(
            "total_months", 0
        )
        now = datetime.now()
        active_keys = [key for key in user_keys if datetime.fromisoformat(key["expiry_date"]) > now]
        if active_keys:
            latest_key = max(active_keys, key=lambda k: datetime.fromisoformat(k["expiry_date"]))
            latest_expiry_date = datetime.fromisoformat(latest_key["expiry_date"])
            time_left = latest_expiry_date - now
            vpn_status_text = get_vpn_active_text(time_left.days, time_left.seconds // 3600)
        elif user_keys:
            vpn_status_text = VPN_INACTIVE_TEXT
        else:
            vpn_status_text = VPN_NO_DATA_TEXT
        final_text = get_profile_text(username, total_spent, total_months, vpn_status_text)
        # Баланс: основной + реферальные метрики
        try:
            main_balance = await asyncio.to_thread(get_balance, user_id)
        except Exception:
            main_balance = 0.0
        final_text += f"\n\n💼 <b>Основной баланс:</b> {main_balance:.0f} ₽"
        # Реферальная информация
        try:
            referral_count = await asyncio.to_thread(get_referral_count, user_id)
        except Exception:
            referral_count = 0
        try:
            total_ref_earned = float(await asyncio.to_thread(get_referral_balance_all, user_id))
        except Exception:
            total_ref_earned = 0.0
        final_text += (
            f"\n🤝 <b>Рефералы:</b> {referral_count}"
            f"\n💰 <b>Заработано по рефералке (всего):</b> {total_ref_earned:.2f} ₽"
        )
        await safe_edit_text(callback, 
            final_text, reply_markup=keyboards.create_profile_keyboard()
        )

    @user_router.callback_query(F.data.startswith("tx_history_"))
    @registration_required
    async def transaction_history_handler(callback: types.CallbackQuery):
        await callback.answer()
        user_id = callback.from_user.id
        try:
            page = int(callback.data.split("_")[-1])
        except Exception:
            page = 0
        per_page = 5
        transactions, total = await asyncio.to_thread(
            get_user_transactions, user_id, page + 1, per_page
        )

        if not transactions:
            await safe_edit_text(callback, 
                "📜 <b>История операций</b>\n\nПока здесь пусто — здесь появятся ваши пополнения, покупки и списания",
                reply_markup=keyboards.create_transaction_history_keyboard(
                    page, has_prev=page > 0, has_next=False
                ),
                parse_mode="HTML",
            )
            return

        lines = ["📜 <b>История операций</b>"]
        for tx in transactions:
            lines.append(format_transaction_line(tx))

        text = "\n\n".join(lines)
        has_next = (page + 1) * per_page < total
        await safe_edit_text(callback, 
            text,
            reply_markup=keyboards.create_transaction_history_keyboard(
                page, has_prev=page > 0, has_next=has_next
            ),
            parse_mode="HTML",
        )

    @user_router.callback_query(F.data == "top_up_start")
    @registration_required
    async def topup_start_handler(callback: types.CallbackQuery, state: FSMContext):
        await callback.answer()
        await safe_edit_text(callback, 
            "💰 <b>Выберите сумму пополнения или введите свою текстом (например, 300):</b>\n\nМинимальная сумма — 10 ₽, максимальная — 100 000 ₽",
            reply_markup=keyboards.create_topup_amount_keyboard(),
            parse_mode="HTML",
        )
        await state.update_data(
            topup_chat_id=callback.message.chat.id,
            topup_msg_id=callback.message.message_id,
        )
        await state.set_state(TopUpProcess.waiting_for_amount)

    async def _show_topup_payment_methods(
        bot: Bot,
        chat_id: int,
        message_id: int,
        state: FSMContext,
        final_amount: Decimal,
        note: str | None = None,
    ):
        await state.update_data(topup_amount=float(final_amount))
        text = f"💸 <b>К пополнению:</b> {final_amount:.2f} ₽\n\nВыберите способ оплаты:"
        if note:
            text = f"{note}\n\n" + text
        try:
            await bot.edit_message_text(
                text,
                chat_id=chat_id,
                message_id=message_id,
                reply_markup=keyboards.create_topup_payment_method_keyboard(PAYMENT_METHODS),
                parse_mode="HTML",
            )
        except TelegramBadRequest as e:
            if "message is not modified" not in str(e).lower():
                sent = await bot.send_message(
                    chat_id=chat_id,
                    text=text,
                    reply_markup=keyboards.create_topup_payment_method_keyboard(PAYMENT_METHODS),
                    parse_mode="HTML",
                )
                await state.update_data(topup_chat_id=sent.chat.id, topup_msg_id=sent.message_id)
        await state.set_state(TopUpProcess.waiting_for_topup_method)

    @user_router.callback_query(TopUpProcess.waiting_for_amount, F.data.startswith("topup_quick_"))
    async def topup_quick_amount_handler(callback: types.CallbackQuery, state: FSMContext):
        await callback.answer()
        amount = Decimal(callback.data.split("_")[-1])
        final_amount = amount.quantize(Decimal("0.01"))
        await _show_topup_payment_methods(
            callback.bot,
            callback.message.chat.id,
            callback.message.message_id,
            state,
            final_amount,
        )

    @user_router.message(TopUpProcess.waiting_for_amount)
    async def topup_amount_input(message: types.Message, state: FSMContext):
        data = await state.get_data()
        chat_id = data.get("topup_chat_id", message.chat.id)
        msg_id = data.get("topup_msg_id")
        text = (message.text or "").replace(",", ".").strip()
        try:
            await message.delete()
        except TelegramBadRequest:
            pass

        async def _error(note: str):
            if msg_id:
                try:
                    await message.bot.edit_message_text(
                        f"{note}\n\n💰 <b>Выберите сумму пополнения или введите свою текстом (например, 300):</b>\n\nМинимальная сумма — 10 ₽, максимальная — 100 000 ₽",
                        chat_id=chat_id,
                        message_id=msg_id,
                        reply_markup=keyboards.create_topup_amount_keyboard(),
                        parse_mode="HTML",
                    )
                    return
                except TelegramBadRequest:
                    pass
            await message.answer(note, reply_markup=keyboards.create_topup_amount_keyboard())

        try:
            amount = Decimal(text)
        except Exception:
            await _error("❌ <b>Введите корректную сумму, например: 300</b>")
            return
        if amount <= 0:
            await _error("❌ <b>Сумма должна быть положительной</b>")
            return
        if amount < Decimal("10"):
            await _error("❌ <b>Минимальная сумма пополнения: 10 ₽</b>")
            return
        if amount > Decimal("100000"):
            await _error("❌ <b>Максимальная сумма пополнения: 100000 ₽</b>")
            return
        final_amount = amount.quantize(Decimal("0.01"))
        if msg_id:
            await _show_topup_payment_methods(message.bot, chat_id, msg_id, state, final_amount)
        else:
            await message.answer(
                f"💸 <b>К пополнению:</b> {final_amount:.2f} ₽\n\nВыберите способ оплаты:",
                reply_markup=keyboards.create_topup_payment_method_keyboard(PAYMENT_METHODS),
                parse_mode="HTML",
            )
            await state.update_data(topup_amount=float(final_amount))
            await state.set_state(TopUpProcess.waiting_for_topup_method)

    @user_router.callback_query(
        TopUpProcess.waiting_for_topup_method, F.data == "topup_pay_yookassa"
    )
    async def topup_pay_yookassa(callback: types.CallbackQuery, state: FSMContext):
        await callback.answer("⏳ Создаю ссылку на оплату...", show_alert=False)
        data = await state.get_data()
        amount = Decimal(str(data.get("topup_amount", 0)))
        if amount <= 0:
            await delete_and_send(callback.message, "❌ Некорректная сумма пополнения. Повторите ввод")
            await state.clear()
            return
        user_id = callback.from_user.id
        price_str_for_api = f"{amount:.2f}"
        price_float_for_metadata = float(amount)

        try:
            # Сформируем чек, если указан email для чеков
            customer_email = get_setting("receipt_email")
            receipt = None
            if customer_email and is_valid_email(customer_email):
                receipt = {
                    "customer": {"email": customer_email},
                    "items": [
                        {
                            "description": "Пополнение баланса",
                            "quantity": "1.00",
                            "amount": {"value": price_str_for_api, "currency": "RUB"},
                            "vat_code": 1,
                            "payment_subject": "service",
                            "payment_mode": "full_payment",
                        }
                    ],
                }

            payment_payload = {
                "amount": {"value": price_str_for_api, "currency": "RUB"},
                "confirmation": {
                    "type": "redirect",
                    "return_url": f"https://t.me/{TELEGRAM_BOT_USERNAME}",
                },
                "capture": True,
                "description": f"Пополнение баланса на {price_str_for_api} ₽",
                "metadata": {
                    "user_id": str(user_id),
                    "price": f"{price_float_for_metadata:.2f}",
                    "action": "top_up",
                    "payment_method": "YooKassa",
                },
            }
            if receipt:
                payment_payload["receipt"] = receipt
            payment = await asyncio.to_thread(Payment.create, payment_payload, uuid.uuid4())
            await state.clear()
            await safe_edit_text(callback, 
                "⬇️ <b>Нажмите на кнопку ниже для оплаты:</b>",
                reply_markup=keyboards.create_payment_keyboard(
                    payment.confirmation.confirmation_url
                ),
                parse_mode="HTML",
            )
        except Exception as e:
            logger.error(f"Не удалось создать платёж пополнения YooKassa: {e}", exc_info=True)
            await callback.message.answer("❌ Не удалось создать ссылку на оплату")
            await state.clear()

    @user_router.callback_query(
        TopUpProcess.waiting_for_topup_method, F.data == "topup_pay_yoomoney"
    )
    async def topup_pay_yoomoney(callback: types.CallbackQuery, state: FSMContext):
        await callback.answer("⏳ Готовлю ЮMoney...", show_alert=False)
        data = await state.get_data()
        amount = Decimal(str(data.get("topup_amount", 0)))
        if amount <= 0:
            await delete_and_send(callback.message, "❌ Некорректная сумма пополнения. Повторите ввод")
            await state.clear()
            return
        ym_wallet = (get_setting("yoomoney_wallet") or "").strip()
        if not ym_wallet:
            await delete_and_send(callback.message, "❌ Оплата через ЮMoney временно недоступна")
            await state.clear()
            return
        user_id = callback.from_user.id
        payment_id = str(uuid.uuid4())
        metadata = {
            "payment_id": payment_id,
            "user_id": user_id,
            "price": float(amount),
            "action": "top_up",
            "payment_method": "YooMoney",
        }
        try:
            create_pending_transaction(payment_id, user_id, float(amount), metadata)
        except Exception as e:
            logger.warning(f"Пополнение YooMoney: не удалось создать ожидающую транзакцию: {e}")
        try:
            success_url = f"https://t.me/{TELEGRAM_BOT_USERNAME}" if TELEGRAM_BOT_USERNAME else None
        except Exception:
            success_url = None
        pay_url = _build_yoomoney_quickpay_url(
            wallet=ym_wallet,
            amount=float(amount),
            label=payment_id,
            success_url=success_url,
            targets=f"Пополнение на {amount:.2f} ₽",
        )
        await state.clear()
        await safe_edit_text(callback, 
            "⬇️ <b>Нажмите на кнопку ниже для оплаты. После оплаты нажмите 'Проверить оплату':</b>",
            reply_markup=keyboards.create_payment_with_check_keyboard(
                pay_url, f"check_yoomoney_{payment_id}"
            ),
            parse_mode="HTML",
        )

    @user_router.callback_query(F.data.startswith("check_yoomoney_"))
    async def check_yoomoney_status(callback: types.CallbackQuery, bot: Bot):
        await callback.answer("⏳ Проверяю оплату...", show_alert=False)
        payment_id = callback.data[len("check_yoomoney_") :]
        if not payment_id:
            await callback.answer("❌ Некорректные данные для проверки", show_alert=False)
            return
        op = await _yoomoney_find_payment(payment_id)
        if not op:
            await callback.answer(
                "❌ Платёж не найден или не завершён. Подождите и попробуйте ещё раз",
                show_alert=False,
            )
            return
        # Завершим pending‑транзакцию и извлечём метаданные
        try:
            amount_rub = (
                float(op.get("amount", 0))
                if isinstance(op.get("amount", 0), (int, float))
                else None
            )
        except Exception:
            amount_rub = None
        md = find_and_complete_pending_transaction(
            payment_id=payment_id,
            amount_rub=amount_rub,
            payment_method="YooMoney",
            currency_name="RUB",
            amount_currency=None,
        )
        if not md:
            await delete_and_send(callback.message, 
                "❌ Не удалось завершить транзакцию. Обратитесь в поддержку, если средства списаны"
            )
            return
        try:
            await process_successful_payment(bot, md)
        except Exception as e:
            logger.error(f"YooMoney: ошибка в process_successful_payment: {e}", exc_info=True)
            try:
                await delete_and_send(callback.message, 
                    "❌ Ошибка при выдаче после оплаты. Напишите в поддержку"
                )
            except Exception:
                pass
            return

    @user_router.callback_query(TopUpProcess.waiting_for_topup_method, F.data == "topup_pay_stars")
    async def topup_pay_stars(callback: types.CallbackQuery, state: FSMContext, bot: Bot):
        await callback.answer("⏳ Готовлю счёт в Stars...", show_alert=False)
        data = await state.get_data()
        amount_rub = Decimal(str(data.get("topup_amount", 0)))
        if amount_rub <= 0:
            await delete_and_send(callback.message, "❌ Некорректная сумма пополнения. Повторите ввод")
            await state.clear()
            return
        stars_count = _calc_stars_amount(amount_rub.quantize(Decimal("0.01")))
        # Для Telegram Stars payload должен быть коротким (до 128 байт). Используем UUID
        # и сохраняем полные метаданные во временную pending‑транзакцию.
        payment_id = str(uuid.uuid4())
        metadata = {
            "user_id": callback.from_user.id,
            "price": float(amount_rub),
            "action": "top_up",
            "payment_method": "Stars",
        }
        try:
            create_pending_transaction(
                payment_id, callback.from_user.id, float(amount_rub), metadata
            )
        except Exception as e:
            logger.warning(f"Пополнение Stars: не удалось создать ожидающую транзакцию: {e}")
        payload = payment_id
        title = get_setting("stars_title") or "Пополнение баланса"
        description = get_setting("stars_description") or f"Пополнение на {amount_rub} ₽"
        try:
            await bot.send_invoice(
                chat_id=callback.message.chat.id,
                title=title,
                description=description,
                payload=payload,
                currency="XTR",
                prices=[types.LabeledPrice(label="Пополнение", amount=stars_count)],
            )
            await state.clear()
        except Exception as e:
            logger.error(f"Не удалось отправить счёт на пополнение Stars: {e}")
            await delete_and_send(callback.message, 
                "❌ Не удалось создать счёт Stars. Попробуйте другой способ оплаты"
            )
            await state.clear()
            return

    @user_router.callback_query(
        TopUpProcess.waiting_for_topup_method,
        (F.data == "topup_pay_cryptobot") | (F.data == "topup_pay_heleket"),
    )
    async def topup_pay_heleket_like(callback: types.CallbackQuery, state: FSMContext):
        await callback.answer("⏳ Создаю счёт...", show_alert=False)
        data = await state.get_data()
        user_id = callback.from_user.id
        amount = float(data.get("topup_amount", 0))
        if amount <= 0:
            await delete_and_send(callback.message, "❌ Некорректная сумма пополнения. Повторите ввод")
            await state.clear()
            return
        # Сформируем state_data минимально необходимым
        state_data = {
            "action": "top_up",
            "customer_email": None,
            "plan_id": None,
            "host_name": None,
            "key_id": None,
        }
        try:
            if callback.data == "topup_pay_cryptobot":
                pay_url = await _create_cryptobot_invoice(
                    user_id=user_id,
                    price_rub=float(amount),
                    months=0,
                    host_name="",
                    state_data=state_data,
                )
            else:
                pay_url = await _create_heleket_payment_request(
                    user_id=user_id,
                    price=float(amount),
                    months=0,
                    host_name="",
                    state_data=state_data,
                )
            if pay_url:
                await safe_edit_text(callback, 
                    "⬇️ <b>Нажмите на кнопку ниже для оплаты:</b>\n\n"
                    "⏳ После оплаты подтверждение приходит автоматически — обычно "
                    "это занимает 1-3 минуты. Как только оно придёт, бот сам пришлёт "
                    "сообщение здесь же",
                    reply_markup=keyboards.create_payment_keyboard(pay_url),
                    parse_mode="HTML",
                )
                await state.clear()
            else:
                await delete_and_send(callback.message, 
                    "❌ Не удалось создать счёт. Попробуйте другой способ оплаты"
                )
                await state.clear()
        except Exception as e:
            logger.error(f"Не удалось создать счёт на пополнение: {e}", exc_info=True)
            await delete_and_send(callback.message, 
                "❌ Не удалось создать счёт. Попробуйте другой способ оплаты"
            )
            await state.clear()

    @user_router.callback_query(
        TopUpProcess.waiting_for_topup_method, F.data == "topup_pay_tonconnect"
    )
    async def topup_pay_tonconnect(callback: types.CallbackQuery, state: FSMContext):
        await callback.answer("⏳ Готовлю TON Connect...", show_alert=False)
        data = await state.get_data()
        user_id = callback.from_user.id
        amount_rub = Decimal(str(data.get("topup_amount", 0)))
        if amount_rub <= 0:
            await delete_and_send(callback.message, "❌ Некорректная сумма пополнения. Повторите ввод")
            await state.clear()
            return

        wallet_address = get_setting("ton_wallet_address")
        if not wallet_address:
            await delete_and_send(callback.message, "❌ Оплата через TON временно недоступна")
            await state.clear()
            return

        usdt_rub_rate = await get_usdt_rub_rate()
        ton_usdt_rate = await get_ton_usdt_rate()
        if not usdt_rub_rate or not ton_usdt_rate:
            await delete_and_send(callback.message, "❌ Не удалось получить курс TON. Попробуйте позже")
            await state.clear()
            return

        price_ton = (amount_rub / usdt_rub_rate / ton_usdt_rate).quantize(
            Decimal("0.001"), rounding=ROUND_HALF_UP
        )
        amount_nanoton = int(price_ton * 1_000_000_000)

        payment_id = str(uuid.uuid4())
        metadata = {
            "user_id": user_id,
            "price": float(amount_rub),
            "action": "top_up",
            "payment_method": "TON Connect",
            # Сохраняем ожидаемую сумму в TON — без неё find_and_complete_ton_transaction
            # не может проверить, что реально пришло столько же (а не меньше), и
            # принимала бы любую сумму, включая символическую копейку
            "expected_amount_ton": float(price_ton),
        }
        create_pending_transaction(payment_id, user_id, float(amount_rub), metadata)

        transaction_payload = {
            "messages": [
                {
                    "address": wallet_address,
                    "amount": str(amount_nanoton),
                    "payload": payment_id,
                }
            ],
            "valid_until": int(datetime.now().timestamp()) + 600,
        }

        try:
            connect_url = await _start_ton_connect_process(user_id, transaction_payload)
            qr_img = qrcode.make(connect_url)
            bio = BytesIO()
            qr_img.save(bio, "PNG")
            qr_file = BufferedInputFile(bio.getvalue(), "ton_qr.png")
            try:
                await callback.message.delete()
            except Exception:
                pass
            await callback.message.answer_photo(
                photo=qr_file,
                caption=(
                    f"💎 <b>Оплата через TON Connect</b>\n\n"
                    f"<b>Сумма к оплате:</b> {price_ton} TON\n\n"
                    f"⬇️ Нажмите кнопку ниже, чтобы открыть кошелёк и подтвердить перевод\n\n"
                    f"⏳ После оплаты подтверждение приходит автоматически — обычно "
                    f"это занимает 1-3 минуты. Как только оно придёт, бот сам пришлёт "
                    f"сообщение здесь же"
                ),
                parse_mode="HTML",
                reply_markup=keyboards.create_ton_connect_keyboard(connect_url),
            )
            await state.clear()
        except Exception as e:
            logger.error(f"Не удалось запустить пополнение через TON Connect: {e}", exc_info=True)
            await delete_and_send(callback.message, "❌ Не удалось подготовить оплату TON Connect")
            await state.clear()

    @user_router.callback_query(F.data == "show_referral_program")
    @registration_required
    async def referral_program_handler(callback: types.CallbackQuery):
        await callback.answer()
        user_id = callback.from_user.id
        bot_username = (await callback.bot.get_me()).username

        referral_link = f"https://t.me/{bot_username}?start=ref_{user_id}"
        referral_count = await asyncio.to_thread(get_referral_count, user_id)
        try:
            total_ref_earned = float(await asyncio.to_thread(get_referral_balance_all, user_id))
        except Exception:
            total_ref_earned = 0.0
        text = (
            "🤝 <b>Реферальная программа</b>\n\n"
            f"<b>Ваша реферальная ссылка:</b>\n<code>{referral_link}</code>\n\n"
            f"<b>Приглашено пользователей:</b> {referral_count}\n"
            f"<b>Заработано по рефералке:</b> {total_ref_earned:.2f} ₽"
        )

        builder = InlineKeyboardBuilder()
        builder.button(text="⬅️ Назад", callback_data="back_to_main_menu")
        await safe_edit_text(callback, text, reply_markup=builder.as_markup())

    @user_router.callback_query(F.data == "show_about")
    @registration_required
    async def show_about_handler(callback: types.CallbackQuery):
        await callback.answer()

        about_text = get_setting("about_text")
        terms_url = get_setting("terms_url")
        privacy_url = get_setting("privacy_url")
        channel_url = get_setting("channel_url")

        final_text = about_text if about_text else "Информация о проекте не добавлена."

        keyboard = keyboards.create_about_keyboard(channel_url, terms_url, privacy_url)

        await safe_edit_text(callback, 
            final_text, reply_markup=keyboard, disable_web_page_preview=True
        )

    @user_router.callback_query(F.data == "show_help")
    @registration_required
    async def show_help_handler(callback: types.CallbackQuery):
        await callback.answer()
        support_bot_username = get_setting("support_bot_username")
        support_text = (
            get_setting("support_text")
            or "Раздел поддержки. Нажмите кнопку ниже, чтобы открыть чат с поддержкой."
        )
        if support_bot_username:
            await safe_edit_text(callback, 
                support_text,
                reply_markup=keyboards.create_support_bot_link_keyboard(support_bot_username),
            )
        else:
            support_user = get_setting("support_user")
            if support_user:
                await safe_edit_text(callback, 
                    "<b>Для связи с поддержкой используйте кнопку ниже</b> ⤵️",
                    reply_markup=keyboards.create_support_keyboard(support_user),
                )
            else:
                await safe_edit_text(callback, 
                    "❌ <b>Контакты поддержки не настроены</b>",
                    reply_markup=keyboards.create_back_to_menu_keyboard(),
                )

    @user_router.callback_query(F.data == "support_menu")
    @registration_required
    async def support_menu_handler(callback: types.CallbackQuery):
        await callback.answer()
        support_bot_username = get_setting("support_bot_username")
        support_text = (
            get_setting("support_text")
            or "<b>Раздел поддержки. Нажмите кнопку ниже, чтобы открыть чат с поддержкой</b> ⤵️"
        )
        if support_bot_username:
            await safe_edit_text(callback, 
                support_text,
                reply_markup=keyboards.create_support_bot_link_keyboard(support_bot_username),
            )
        else:
            support_user = get_setting("support_user")
            if support_user:
                await safe_edit_text(callback, 
                    "<b>Для связи с поддержкой используйте кнопку ниже</b> ⤵️",
                    reply_markup=keyboards.create_support_keyboard(support_user),
                )
            else:
                await safe_edit_text(callback, 
                    "❌ <b>Контакты поддержки не настроены</b>",
                    reply_markup=keyboards.create_back_to_menu_keyboard(),
                )

    @user_router.callback_query(F.data == "support_external")
    @registration_required
    async def support_external_handler(callback: types.CallbackQuery):
        await callback.answer()
        support_bot_username = get_setting("support_bot_username")
        if support_bot_username:
            await safe_edit_text(callback, 
                get_setting("support_text") or "Раздел поддержки",
                reply_markup=keyboards.create_support_bot_link_keyboard(support_bot_username),
            )
            return
        support_user = get_setting("support_user")
        if not support_user:
            await safe_edit_text(callback, 
                "❌ <b>Внешний контакт поддержки не настроен<b>",
                reply_markup=keyboards.create_back_to_menu_keyboard(),
            )
            return
        await safe_edit_text(callback, 
            "<b>Для связи с поддержкой используйте кнопку ниже</b> ⤵️",
            reply_markup=keyboards.create_support_keyboard(support_user),
        )

    @user_router.callback_query(F.data == "support_new_ticket")
    @registration_required
    async def support_new_ticket_handler(callback: types.CallbackQuery, state: FSMContext):
        await callback.answer()
        support_bot_username = get_setting("support_bot_username")
        if support_bot_username:
            await safe_edit_text(callback, 
                "Раздел поддержки вынесен в отдельного бота",
                reply_markup=keyboards.create_support_bot_link_keyboard(support_bot_username),
            )
        else:
            await safe_edit_text(callback, 
                "❌ <b>Контакты поддержки не настроены</b>",
                reply_markup=keyboards.create_back_to_menu_keyboard(),
            )

    @user_router.message(SupportDialog.waiting_for_subject)
    @registration_required
    async def support_subject_received(message: types.Message, state: FSMContext):
        await state.clear()
        try:
            await message.delete()
        except Exception:
            pass
        support_bot_username = get_setting("support_bot_username")
        if support_bot_username:
            await message.answer(
                "⚠️ <b>Создание тикетов доступно в отдельном боте поддержки</b>",
                reply_markup=keyboards.create_support_bot_link_keyboard(support_bot_username),
            )
        else:
            await message.answer("❌ Контакты поддержки не настроены")

    @user_router.message(SupportDialog.waiting_for_message)
    @registration_required
    async def support_message_received(message: types.Message, state: FSMContext, bot: Bot):
        await state.clear()
        try:
            await message.delete()
        except Exception:
            pass
        support_bot_username = get_setting("support_bot_username")
        if support_bot_username:
            await message.answer(
                "⚠️ <b>Создание тикетов доступно в отдельном боте поддержки</b>",
                reply_markup=keyboards.create_support_bot_link_keyboard(support_bot_username),
            )
        else:
            await message.answer("❌ Контакты поддержки не настроены")

    @user_router.callback_query(F.data == "support_my_tickets")
    @registration_required
    async def support_my_tickets_handler(callback: types.CallbackQuery):
        await callback.answer()
        support_bot_username = get_setting("support_bot_username")
        if support_bot_username:
            await safe_edit_text(callback, 
                "⚠️ <b>Список обращений доступен в отдельном боте поддержки</b>",
                reply_markup=keyboards.create_support_bot_link_keyboard(support_bot_username),
            )
        else:
            await safe_edit_text(callback, 
                "❌ <b>Контакты поддержки не настроены</b>",
                reply_markup=keyboards.create_back_to_menu_keyboard(),
            )

    @user_router.callback_query(F.data.startswith("support_view_"))
    @registration_required
    async def support_view_ticket_handler(callback: types.CallbackQuery):
        await callback.answer()
        support_bot_username = get_setting("support_bot_username")
        if support_bot_username:
            await safe_edit_text(callback, 
                "⚠️ <b>Просмотр тикетов доступен в отдельном боте поддержки</b>",
                reply_markup=keyboards.create_support_bot_link_keyboard(support_bot_username),
            )
        else:
            await safe_edit_text(callback, 
                "❌ <b>Контакты поддержки не настроены</b>",
                reply_markup=keyboards.create_back_to_menu_keyboard(),
            )

    @user_router.callback_query(F.data.startswith("support_reply_"))
    @registration_required
    async def support_reply_prompt_handler(callback: types.CallbackQuery, state: FSMContext):
        await callback.answer()
        await state.clear()
        support_bot_username = get_setting("support_bot_username")
        if support_bot_username:
            await safe_edit_text(callback, 
                "⚠️ <b>Отправка ответов доступна в отдельном боте поддержки</b>",
                reply_markup=keyboards.create_support_bot_link_keyboard(support_bot_username),
            )
        else:
            await safe_edit_text(callback, 
                "❌ <b>Контакты поддержки не настроены</b>",
                reply_markup=keyboards.create_back_to_menu_keyboard(),
            )

    @user_router.message(SupportDialog.waiting_for_reply)
    @registration_required
    async def support_reply_received(message: types.Message, state: FSMContext, bot: Bot):
        await state.clear()
        try:
            await message.delete()
        except Exception:
            pass
        support_bot_username = get_setting("support_bot_username")
        if support_bot_username:
            await message.answer(
                "⚠️ <b>Отправка ответов доступна в отдельном боте поддержки",
                reply_markup=keyboards.create_support_bot_link_keyboard(support_bot_username),
            )
        else:
            await message.answer("❌ Контакты поддержки не настроены")

    @user_router.message(F.is_topic_message)
    async def forum_thread_message_handler(message: types.Message, bot: Bot):
        try:
            support_bot_username = get_setting("support_bot_username")
            me = await bot.get_me()
            if support_bot_username and (me.username or "").lower() != support_bot_username.lower():
                return
            if not message.message_thread_id:
                return
            forum_chat_id = message.chat.id
            thread_id = message.message_thread_id
            ticket = get_ticket_by_thread(str(forum_chat_id), int(thread_id))
            if not ticket:
                return
            user_id = int(ticket.get("user_id"))
            if message.from_user and message.from_user.id == me.id:
                return
            # Проверка многоадминная
            is_admin_by_setting = is_admin(message.from_user.id)
            is_admin_in_chat = False
            try:
                member = await bot.get_chat_member(
                    chat_id=forum_chat_id, user_id=message.from_user.id
                )
                is_admin_in_chat = member.status in [
                    ChatMemberStatus.ADMINISTRATOR,
                    ChatMemberStatus.CREATOR,
                ]
            except Exception:
                pass
            if not (is_admin_by_setting or is_admin_in_chat):
                return
            content = (message.text or message.caption or "").strip()
            if content:
                add_support_message(
                    ticket_id=int(ticket["ticket_id"]), sender="admin", content=content
                )
            header = await bot.send_message(
                chat_id=user_id,
                text=f"💬 Ответ поддержки по тикету № {ticket['ticket_id']}",
            )
            try:
                await bot.copy_message(
                    chat_id=user_id,
                    from_chat_id=message.chat.id,
                    message_id=message.message_id,
                    reply_to_message_id=header.message_id,
                )
            except Exception:
                if content:
                    await bot.send_message(chat_id=user_id, text=content)
        except Exception as e:
            logger.warning(f"Не удалось переслать сообщение из темы форума: {e}")

    @user_router.callback_query(F.data.startswith("support_close_"))
    @registration_required
    async def support_close_ticket_handler(callback: types.CallbackQuery):
        await callback.answer()
        support_bot_username = get_setting("support_bot_username")
        if support_bot_username:
            await safe_edit_text(callback, 
                "⚠️ <b>Управление тикетами доступно в отдельном боте поддержки</b>",
                reply_markup=keyboards.create_support_bot_link_keyboard(support_bot_username),
            )
            return
        await safe_edit_text(callback, 
            "❌ <b>Контакты поддержки не настроены</b>",
            reply_markup=keyboards.create_back_to_menu_keyboard(),
        )

    @user_router.callback_query(F.data == "manage_keys")
    @registration_required
    async def manage_keys_handler(callback: types.CallbackQuery):
        await callback.answer()
        user_id = callback.from_user.id
        user_keys = await asyncio.to_thread(get_user_keys, user_id)
        await safe_edit_text(callback, 
            ("🔑 <b>Ваши подписки:</b>" if user_keys else "❌ <b>У вас пока нет подписок</b>"),
            reply_markup=keyboards.create_keys_management_keyboard(user_keys),
            parse_mode="HTML",
        )

    @user_router.callback_query(F.data == "get_trial")
    @registration_required
    async def trial_period_handler(callback: types.CallbackQuery, state: FSMContext):
        user_id = callback.from_user.id
        user_db_data = await asyncio.to_thread(get_user, user_id)
        if user_db_data and user_db_data.get("trial_used"):
            await callback.answer(
                "⚠️ Вы уже использовали бесплатный пробный период", show_alert=True
            )
            return

        hosts = await asyncio.to_thread(get_all_hosts)
        if not hosts:
            await delete_and_send(callback.message, 
                "❌ В данный момент нет доступных серверов для создания пробной подписки"
            )
            return

        if len(hosts) == 1:
            await callback.answer()
            await process_trial_key_creation(callback.message, hosts[0]["host_name"])
        else:
            await callback.answer()
            await safe_edit_text(callback, 
                "🗺️ <b>Выберите сервер, на котором хотите получить пробную подписку:</b>",
                reply_markup=keyboards.create_host_selection_keyboard(hosts, action="trial"),
                parse_mode="HTML",
            )

    @user_router.callback_query(
        F.data.startswith("select_host:"), ~F.data.startswith("select_host:adm_sw-")
    )
    @registration_required
    async def select_host_callback_handler(callback: types.CallbackQuery):
        parsed = keyboards.parse_host_callback_data(callback.data)
        if not parsed:
            await callback.answer("❌ Некорректные данные выбора сервера", show_alert=False)
            return

        action, extra, token = parsed
        hosts = await asyncio.to_thread(get_all_hosts)
        host_entry = keyboards.find_host_by_callback_token(hosts, token)
        if not host_entry:
            await callback.answer("❌ Сервер не найден", show_alert=False)
            return

        host_name = host_entry.get("host_name")
        if host_name and "WL" in host_name:
            alert_text = (
                "📶 Эта подписка работает только на мобильном интернете\n\n"
                "🛑 Подключение через домашний интернет или Wi-Fi автоматически блокируется"
            )
        else:
            alert_text = (
                "🛜📶 Эта подписка работает на домашнем интернете, Wi-Fi и LTE вне зоны ограничений\n\n"
                "⬅️ Для работы мобильного интернета в условиях ограничений вернитесь назад и выберите одну из WL-подписок"
            )

        if action == "trial":
            (
                await callback.answer(alert_text, show_alert=True)
                if alert_text
                else await callback.answer()
            )
            await process_trial_key_creation(callback.message, host_name)
            return

        if action == "new":
            (
                await callback.answer(alert_text, show_alert=True)
                if alert_text
                else await callback.answer()
            )
            plans = get_plans_for_host(host_name)
            if not plans:
                await delete_and_send(callback.message, f"❌ Для сервера {host_name} не настроены тарифы")
                return
            await safe_edit_text(callback, 
                CHOOSE_PLAN_MESSAGE or "Выберите тариф:",
                reply_markup=keyboards.create_plans_keyboard(
                    plans, action="new", host_name=host_name
                ),
            )
            return

        if action == "switch":
            # await callback.answer(alert_text, show_alert=True) if alert_text else await callback.answer()
            await callback.answer(
                "🔑🔄 Текущая подписка будет отключена. Не забудьте добавить новую подписку в свой клиент",
                show_alert=True,
            )
            try:
                key_id = int(extra)
            except Exception:
                await callback.answer("❌ Некорректные данные выбора сервера", show_alert=False)
                return
            await handle_switch_host(callback, key_id, host_name)
            return

        await callback.answer("⚠️ Неизвестное действие", show_alert=True)

    async def process_trial_key_creation(message: types.Message, host_name: str):
        user_id = message.chat.id
        message = await delete_and_send(
            message,
            f"✅ Отлично! Создаю для вас бесплатную подписку на {get_setting('trial_duration_days')} дн. на сервере {host_name}...",
        )

        try:
            # Email: trial_{username}@{xui_api.EMAIL_DOMAIN} с авто-суффиксом при коллизиях
            user_data = await asyncio.to_thread(get_user, user_id) or {}
            raw_username = (user_data.get("username") or f"user{user_id}").lower()
            username_slug = (
                re.sub(r"[^a-z0-9._-]", "_", raw_username).strip("_")[:16] or f"user{user_id}"
            )
            base_local = f"trial_{username_slug}"
            candidate_local = base_local
            attempt = 1
            while True:
                candidate_email = f"{candidate_local}@{xui_api.EMAIL_DOMAIN}"
                if not get_key_by_email(candidate_email):
                    break
                attempt += 1
                candidate_local = f"{base_local}_{attempt}"
                if attempt > 100:
                    candidate_local = f"{base_local}_{int(datetime.now().timestamp())}"
                    candidate_email = f"{candidate_local}@{xui_api.EMAIL_DOMAIN}"
                    break

            result = await xui_api.create_or_update_key_on_host(
                host_name=host_name,
                email=candidate_email,
                days_to_add=int(get_setting("trial_duration_days")),
            )
            if not result:
                await delete_and_send(
                    message, "❌ Не удалось создать пробную подписку. Ошибка на сервере"
                )
                return

            set_trial_used(user_id)

            new_key_id = add_new_key(
                user_id=user_id,
                host_name=host_name,
                xui_client_uuid=result["client_uuid"],
                key_email=result["email"],
                expiry_timestamp_ms=result["expiry_timestamp_ms"],
            )

            new_expiry_date = datetime.fromtimestamp(result["expiry_timestamp_ms"] / 1000)
            final_text = get_purchase_success_text(
                "готов",
                get_next_key_number(user_id) - 1,
                new_expiry_date,
                result["connection_string"],
            )
            # Вместо удаления сообщения (что может быть запрещено Telegram), сначала пытаемся отредактировать его
            try:
                await message.edit_text(
                    text=final_text,
                    reply_markup=keyboards.create_key_info_keyboard(
                        new_key_id, result["connection_string"]
                    ),
                    disable_web_page_preview=True,
                )
            except TelegramBadRequest:
                # Фолбэк: если редактирование невозможно (например, старое сообщение), попробуем удалить и отправить новое
                try:
                    await message.delete()
                except Exception:
                    pass
                await message.answer(
                    text=final_text,
                    reply_markup=keyboards.create_key_info_keyboard(
                        new_key_id, result["connection_string"]
                    ),
                )

        except Exception as e:
            logger.error(
                f"Ошибка при создании пробного ключа для пользователя {user_id} на хосте {host_name}: {e}",
                exc_info=True,
            )
            await delete_and_send(message, "❌ Произошла ошибка при создании пробной подписки")

    @user_router.callback_query(F.data.startswith("show_key_"))
    @registration_required
    async def show_key_handler(callback: types.CallbackQuery):
        key_id_to_show = int(callback.data.split("_")[2])
        loading_message = await delete_and_send(callback.message, "⏳ Загружаю информацию о подписке...")
        user_id = callback.from_user.id
        key_data = get_key_by_id(key_id_to_show)

        if not key_data or key_data["user_id"] != user_id:
            await delete_and_send(loading_message, "❌ Подписка не найдена")
            return

        try:
            details = await xui_api.get_key_details_from_host(key_data)
            if not details or not details["connection_string"]:
                await delete_and_send(loading_message, 
                    "❌ Ошибка на сервере. Не удалось получить данные подписки"
                )
                return

            connection_string = details["connection_string"]
            expiry_date = datetime.fromisoformat(key_data["expiry_date"])
            created_date = datetime.fromisoformat(key_data["created_date"])

            all_user_keys = await asyncio.to_thread(get_user_keys, user_id)
            key_number = next(
                (i + 1 for i, key in enumerate(all_user_keys) if key["key_id"] == key_id_to_show),
                0,
            )

            final_text = get_key_info_text(key_number, expiry_date, created_date, connection_string)

            await loading_message.edit_text(
                text=final_text,
                reply_markup=keyboards.create_key_info_keyboard(key_id_to_show, connection_string),
            )
        except Exception as e:
            logger.error(f"Ошибка при отображении ключа {key_id_to_show}: {e}")
            await delete_and_send(loading_message, "❌ Произошла ошибка при получении данных подписки")

    @user_router.callback_query(F.data.startswith("regen_key_prompt_"))
    @registration_required
    async def regen_key_prompt_handler(callback: types.CallbackQuery):
        await callback.answer()
        key_id = int(callback.data.split("_")[-1])
        key_data = await asyncio.to_thread(get_key_by_id, key_id)
        if not key_data or key_data["user_id"] != callback.from_user.id:
            await callback.answer("❌ Подписка не найдена", show_alert=False)
            return
        await safe_edit_text(callback, 
            "🔑🔄 <b>Текущая подписка будет отключена. Не забудьте добавить новую подписку в свой клиент</b>\n\n"
            "Продолжить?",
            reply_markup=keyboards.create_regen_key_confirm_keyboard(key_id),
            parse_mode="HTML",
        )

    @user_router.callback_query(F.data.startswith("toggle_auto_renew_"))
    @registration_required
    async def toggle_auto_renew_handler(callback: types.CallbackQuery):
        key_id = int(callback.data.split("_")[-1])
        key_data = await asyncio.to_thread(get_key_by_id, key_id)
        if not key_data or key_data["user_id"] != callback.from_user.id:
            await callback.answer("❌ Подписка не найдена", show_alert=False)
            return
        if key_data.get("last_plan_id") is None:
            # Не должно быть достижимо через кнопку (её не показываем в этом
            # случае — см. create_key_info_keyboard), но на случай гонки
            # (например, старая клавиатура ещё в чате) — понятная ошибка,
            # а не тихий no-op автопродления в никуда
            await callback.answer(
                "❌ Для этой подписки ещё нет привязанного тарифа "
                "(например, это пробный ключ) — автопродление недоступно",
                show_alert=True,
            )
            return

        new_state = not bool(key_data.get("auto_renew_enabled"))
        ok = await asyncio.to_thread(set_key_auto_renew, key_id, new_state)
        if not ok:
            await callback.answer("❌ Не удалось изменить настройку, попробуйте ещё раз", show_alert=False)
            return

        await callback.answer(
            "✅ Автопродление включено" if new_state else "🚫 Автопродление выключено",
            show_alert=False,
        )
        try:
            details = await xui_api.get_key_details_from_host(key_data)
            connection_string = details.get("connection_string") if details else None
        except Exception as e:
            logger.debug(f"toggle_auto_renew_handler: не удалось получить connection_string: {e}")
            connection_string = None
        try:
            await callback.message.edit_reply_markup(
                reply_markup=keyboards.create_key_info_keyboard(key_id, connection_string)
            )
        except Exception as e:
            logger.debug(f"toggle_auto_renew_handler: не удалось обновить клавиатуру: {e}")


    @user_router.callback_query(F.data.startswith("regen_key_confirm_"))
    @registration_required
    async def regen_key_confirm_handler(callback: types.CallbackQuery):
        await callback.answer("⏳ Пересоздаю подписку...", show_alert=False)
        key_id = int(callback.data.split("_")[-1])
        user_id = callback.from_user.id
        key_data = await asyncio.to_thread(get_key_by_id, key_id)
        if not key_data or key_data["user_id"] != user_id:
            await delete_and_send(callback.message, "❌ Подписка не найдена")
            return

        try:
            expiry_dt = datetime.fromisoformat(key_data["expiry_date"])
            expiry_ms = int(expiry_dt.timestamp() * 1000)
        except Exception:
            await delete_and_send(callback.message, "❌ Не удалось определить срок действия подписки")
            return

        host_name = key_data["host_name"]
        key_email = key_data["key_email"]

        try:
            # Сначала удаляем старого клиента, иначе панель просто продлит
            # существующий UUID вместо выпуска нового
            await xui_api.delete_client_on_host(host_name, key_email)
            result = await xui_api.create_or_update_key_on_host(
                host_name=host_name, email=key_email, expiry_timestamp_ms=expiry_ms
            )
        except Exception as e:
            logger.error(f"Ошибка пересоздания ключа {key_id}: {e}")
            result = None

        if not result or not result.get("client_uuid"):
            await safe_edit_text(callback, 
                "❌ Не удалось пересоздать подписку на сервере. Попробуйте позже или обратитесь в поддержку",
                reply_markup=keyboards.create_key_info_keyboard(key_id),
            )
            return

        update_key_info(key_id, result["client_uuid"], result.get("expiry_timestamp_ms", expiry_ms))

        # Показываем обновлённую карточку с новой ссылкой — та же логика, что в show_key_handler
        try:
            fresh_key_data = await asyncio.to_thread(get_key_by_id, key_id)
            details = await xui_api.get_key_details_from_host(fresh_key_data)
            if not details or not details.get("connection_string"):
                await safe_edit_text(callback, 
                    "✅ Подписка пересоздана, но не удалось получить свежую ссылку — откройте подписку заново из списка «Мои подписки»",
                    reply_markup=keyboards.create_back_to_menu_keyboard(),
                )
                return
            connection_string = details["connection_string"]
            expiry_date = datetime.fromisoformat(fresh_key_data["expiry_date"])
            created_date = datetime.fromisoformat(fresh_key_data["created_date"])
            all_user_keys = await asyncio.to_thread(get_user_keys, user_id)
            key_number = next(
                (i + 1 for i, k in enumerate(all_user_keys) if k["key_id"] == key_id), 0
            )
            final_text = "✅ <b>Подписка пересоздана</b>\n\n" + get_key_info_text(
                key_number, expiry_date, created_date, connection_string
            )
            await safe_edit_text(callback, 
                text=final_text,
                reply_markup=keyboards.create_key_info_keyboard(key_id, connection_string),
            )
        except Exception as e:
            logger.error(f"Ошибка при обновлении карточки после пересоздания ключа {key_id}: {e}")
            await safe_edit_text(callback, 
                "✅ Подписка пересоздана. Откройте её заново из списка «Мои подписки», чтобы увидеть новую ссылку",
                reply_markup=keyboards.create_back_to_menu_keyboard(),
            )

    async def send_outbounds_file(message, connection_string: str):
        """
        Скачивает JSON-конфигурацию подписки и отправляет каждый outbound отдельным файлом.

        Особенности:
        • URL подписки преобразуется в /json/-вариант
        • Каждый outbound (или группа) отправляется как отдельный файл "04_outbounds.json"
        • Первый outbound в файле получает тег "vless-reality"
        • Сохраняется оригинальный текст сообщений и подписей (без изменений)
        • Итоговое сообщение о количестве файлов НЕ отправляется

        Args:
            message: Объект Message от aiogram
            connection_string: Ссылка на подписку
        """
        # -------------------------------------------------------------------------
        # 1. Преобразование ссылки подписки → JSON-эндпоинт, схема принудительно HTTPS
        # -------------------------------------------------------------------------
        json_url = _force_https(connection_string.replace("/sub/", "/json/"))

        # -------------------------------------------------------------------------
        # 2. Загрузка конфигурации — общий хелпер fetch_via_https
        # -------------------------------------------------------------------------
        try:
            status, raw_body = await fetch_via_https(json_url)

            if status != 200:
                await message.answer("❌ Сервер подписки временно недоступен")
                return

            data = json.loads(raw_body.decode("utf-8", errors="replace"))
        except json.JSONDecodeError as e:
            logger.error(f"Не удалось разобрать JSON от {json_url}: {e}")
            await message.answer("❌ Не удалось получить конфигурацию с сервера")
            return
        except Exception as e:
            logger.error(f"Ошибка получения JSON от {json_url}: {e}")
            await message.answer("❌ Не удалось получить конфигурацию с сервера")
            return

        # -------------------------------------------------------------------------
        # 3. Нормализация данных
        # -------------------------------------------------------------------------
        raw_outbounds = data.get("outbounds", []) if isinstance(data, dict) else data

        if not isinstance(raw_outbounds, list) or not raw_outbounds:
            await message.answer("❌ Нет доступных серверов в подписке")
            return

        # -------------------------------------------------------------------------
        # 4. Отправка каждого outbound отдельным файлом (с фильтрацией)
        # -------------------------------------------------------------------------
        file_counter = 0  # Счётчик для красивой порядковой нумерации в Telegram

        for outbound_item in raw_outbounds:
            # Получаем remark текущего аутбаунда
            raw_remarks = outbound_item.get("remarks", "Default")

            # Игнорируем Podkop
            if "podkop" in raw_remarks.lower():
                continue

            actual_outbounds = outbound_item.get("outbounds", [outbound_item])

            if not actual_outbounds:
                continue

            # Принудительная установка тега для первого outbound
            actual_outbounds[0]["tag"] = "vless-reality"

            single_proxy_config = {"outbounds": actual_outbounds}
            json_str = json.dumps(single_proxy_config, ensure_ascii=False, indent=2)

            document = BufferedInputFile(
                file=json_str.encode("utf-8"), filename="04_outbounds.json"
            )

            # Увеличиваем счётчик только для тех файлов, которые прошли проверку
            file_counter += 1
            server_name = raw_remarks.split("-")[0].strip()

            caption = (
                f"🛜 <b>Файл подключения для XKeen № {file_counter}</b>\n"
                f"🗺️ {server_name}\n\n"
                f"⚠️ Сохраните этот файл на роутере в папке <code>\\etc\\xray\\configs</code> и выполните команду <code>xkeen -restart</code>"
            )

            try:
                await message.answer_document(document=document, caption=caption, parse_mode="HTML")
            except Exception as send_err:
                logger.error(f"Ошибка отправки файла №{file_counter} ({server_name}): {send_err}")
                # Не прерываем отправку остальных файлов при ошибке одного
                continue

    @user_router.callback_query(F.data.startswith("download_xkeen_outbounds_"))
    async def download_xkeen_outbounds_handler(callback: types.CallbackQuery):
        """
        Обработчик callback-запроса для генерации и отправки файлов конфигурации XKeen.

        Логика:
        1. Извлекает ID ключа из callback.data
        2. Проверяет принадлежность ключа текущему пользователю
        3. Получает актуальную ссылку подписки с панели
        4. Вызывает send_outbounds_file для скачивания и отправки файлов

        Args:
            callback: CallbackQuery от aiogram

        Сообщения пользователю остаются без изменений.
        """
        await callback.answer("⏳ Создаю файлы...")

        # -------------------------------------------------------------------------
        # 1. Извлечение ID ключа
        # -------------------------------------------------------------------------
        try:
            key_id = int(callback.data.split("_")[-1])
        except ValueError:
            await callback.message.answer("❌ Подписка не найдена")
            return

        # -------------------------------------------------------------------------
        # 2. Проверка существования ключа и прав доступа
        # -------------------------------------------------------------------------
        key_data = await asyncio.to_thread(get_key_by_id, key_id)
        if not key_data or key_data["user_id"] != callback.from_user.id:
            await callback.message.answer("🚫 Это не ваша подписка")
            return

        # -------------------------------------------------------------------------
        # 3. Получение актуальной ссылки подписки
        # -------------------------------------------------------------------------
        try:
            details = await xui_api.get_key_details_from_host(key_data)
            if not details or not details.get("connection_string"):
                await callback.message.answer(
                    "❌ Не удалось получить актуальную подписку с сервера"
                )
                return

            connection_string = details["connection_string"]

            # -------------------------------------------------------------------------
            # 4. Запуск отправки файлов
            # -------------------------------------------------------------------------
            await send_outbounds_file(callback.message, connection_string)

        except Exception as e:
            logger.error(f"Ошибка при генерации outbounds для ключа {key_id}: {e}", exc_info=True)
            await callback.message.answer("❌ Ошибка при генерации файла. Попробуйте позже")

    @user_router.callback_query(F.data.startswith("show_podkop_keys_"))
    async def show_podkop_keys_handler(callback: types.CallbackQuery):
        """
        Обработчик callback-запроса для выдачи сырых VLESS-ссылок для Podkop в текстовом виде.

        Логика работы:
        1. Проверяет принадлежность ключа текущему пользователю
        2. Получает актуальную ссылку подписки с панели хоста
        3. Загружает содержимое подписки (base64-encoded список ссылок)
        4. Декодирует и разбивает на отдельные vless:// строки
        5. Для каждой ссылки извлекает remark (название сервера после #)
        6. Отправляет каждую ссылку в файле отдельным сообщением с номером и названием

        Формат отправляемого сообщения:
        • Заголовок: «Ключ в формате vless:// №N»
        • На новой строке: 🗺️ Название сервера (если есть remark)
        • Сама ссылка в <code>блоке</code>

        Args:
            callback: CallbackQuery от aiogram (содержит data с key_id)

        Особенности:
        • Защита от доступа к чужим подпискам
        • Таймаут на загрузку подписки — 30 секунд
        • Обработка ошибок на каждом этапе с понятными сообщениями пользователю
        • Использует unquote для корректного отображения кириллицы/спецсимволов в remark
        """
        # Подтверждаем получение запроса (убираем "часики" у кнопки)
        await callback.answer("⏳ Получаю ключи...")

        # -------------------------------------------------------------------------
        # 1. Извлечение ID ключа из callback.data
        # -------------------------------------------------------------------------
        try:
            # Ожидаемый формат: prefix_key_123 → берём последнюю часть после _
            key_id = int(callback.data.split("_")[-1])
        except (ValueError, IndexError):
            await callback.message.answer("❌ Некорректный идентификатор подписки")
            return

        # -------------------------------------------------------------------------
        # 2. Получение данных ключа из локальной БД + проверка владельца
        # -------------------------------------------------------------------------
        key_data = await asyncio.to_thread(get_key_by_id, key_id)
        if not key_data or key_data.get("user_id") != callback.from_user.id:
            await callback.message.answer("🚫 Это не ваша подписка")
            return

        # -------------------------------------------------------------------------
        # 3. Получение актуальной ссылки подписки с панели
        # -------------------------------------------------------------------------
        try:
            details = await xui_api.get_key_details_from_host(key_data)
            if not details or not details.get("connection_string"):
                await callback.message.answer(
                    "❌ Не удалось получить актуальную подписку с сервера"
                )
                return

            connection_string = details["connection_string"]

        except Exception as e:
            logger.error(
                f"Ошибка при получении subscription link для ключа {key_id}: {e}",
                exc_info=True,
            )
            await callback.message.answer("❌ Не удалось получить данные подписки")
            return

        # -------------------------------------------------------------------------
        # 4. Загрузка содержимого подписки (base64-encoded список ссылок)
        # -------------------------------------------------------------------------
        try:
            sub_url = _force_https(connection_string)
            status, raw_body = await fetch_via_https(sub_url, timeout=30.0)
            if status != 200:
                await callback.message.answer("❌ Не удалось загрузить конфигурацию")
                return

            content = raw_body.decode("utf-8", errors="replace").strip()

        except Exception as e:
            logger.error(f"Ошибка загрузки подписки {connection_string}: {e}", exc_info=True)
            await callback.message.answer("❌ Произошла ошибка при загрузке ключей")
            return

        # -------------------------------------------------------------------------
        # 5. Декодирование base64 и разбиение на отдельные VLESS-ссылки
        # -------------------------------------------------------------------------
        try:
            decoded_data = base64.b64decode(content).decode("utf-8").strip()
            vless_links = [link.strip() for link in decoded_data.splitlines() if link.strip()]

            if not vless_links:
                await callback.message.answer("❌ В подписке не найдено ни одного ключа")
                return

        except Exception as e:
            logger.error(f"Ошибка декодирования base64-подписки для ключа {key_id}: {e}")
            await callback.message.answer("❌ Некорректный формат данных подписки")
            return

        # -------------------------------------------------------------------------
        # 6. Отправка каждой VLESS-ссылки отдельным сообщением
        # -------------------------------------------------------------------------
        file_counter = 1

        for index, link in enumerate(vless_links, start=1):
            remark_text = ""

            # Извлекаем remark (всё после #), если он есть
            if "#" in link:
                raw_remark = link.split("#")[-1]
                try:
                    decoded_full = unquote(raw_remark)

                    # Игнорируем все, кроме "Podkop"
                    if "podkop" not in decoded_full.lower():
                        continue

                    decoded_remark = decoded_full.split("-")[0].strip()
                    remark_text = f"\n🗺️ {decoded_remark}"
                except Exception:
                    # Если декодирование не удалось — показываем как есть
                    if "podkop" not in raw_remark.lower():
                        continue
                    remark_text = f"\n🗺️ {raw_remark}"
            else:
                # Если в ссылке нет знака #, то в ней нет и remark с ключевым словом
                continue

            # Формируем красивое сообщение
            caption = (
                f"🔑 <b>Ключ для Podkop в формате vless:// № {file_counter}</b>" f"{remark_text}"
            )

            document = BufferedInputFile(
                file=link.encode("utf-8"), filename=f"key_{file_counter}.txt"
            )

            try:
                await callback.message.answer_document(
                    document=document, caption=caption, parse_mode="HTML"
                )
                file_counter += 1
            except Exception as send_err:
                logger.error(
                    f"Ошибка отправки VLESS-ссылки №{index} пользователю {callback.from_user.id}: {send_err}"
                )
                # Продолжаем отправку остальных ссылок

        # -------------------------------------------------------------------------
        # 7. Все ссылки отправлены (без итогового сообщения)
        # -------------------------------------------------------------------------
        # Здесь можно добавить финальное сообщение, если потребуется в будущем
        # await callback.message.answer("✅ Все доступные ключи отправлены")

    @user_router.callback_query(F.data.startswith("show_raw_keys_"))
    async def show_raw_keys_handler(callback: types.CallbackQuery):
        """
        Обработчик callback-запроса для выдачи сырых VLESS-ссылок в текстовом виде.

        Логика работы:
        1. Проверяет принадлежность ключа текущему пользователю
        2. Получает актуальную ссылку подписки с панели хоста
        3. Загружает содержимое подписки (base64-encoded список ссылок)
        4. Декодирует и разбивает на отдельные vless:// строки
        5. Для каждой ссылки извлекает remark (название сервера после #)
        6. Отправляет каждую ссылку в файле отдельным сообщением с номером и названием

        Формат отправляемого сообщения:
        • Заголовок: «Ключ в формате vless:// №N»
        • На новой строке: 🗺️ Название сервера (если есть remark)
        • Сама ссылка в <code>блоке</code>

        Args:
            callback: CallbackQuery от aiogram (содержит data с key_id)

        Особенности:
        • Защита от доступа к чужим подпискам
        • Таймаут на загрузку подписки — 30 секунд
        • Обработка ошибок на каждом этапе с понятными сообщениями пользователю
        • Использует unquote для корректного отображения кириллицы/спецсимволов в remark
        """
        # Подтверждаем получение запроса (убираем "часики" у кнопки)
        await callback.answer("⏳ Получаю ключи...")

        # -------------------------------------------------------------------------
        # 1. Извлечение ID ключа из callback.data
        # -------------------------------------------------------------------------
        try:
            # Ожидаемый формат: prefix_key_123 → берём последнюю часть после _
            key_id = int(callback.data.split("_")[-1])
        except (ValueError, IndexError):
            await callback.message.answer("❌ Некорректный идентификатор подписки")
            return

        # -------------------------------------------------------------------------
        # 2. Получение данных ключа из локальной БД + проверка владельца
        # -------------------------------------------------------------------------
        key_data = await asyncio.to_thread(get_key_by_id, key_id)
        if not key_data or key_data.get("user_id") != callback.from_user.id:
            await callback.message.answer("🚫 Это не ваша подписка")
            return

        # -------------------------------------------------------------------------
        # 3. Получение актуальной ссылки подписки с панели
        # -------------------------------------------------------------------------
        try:
            details = await xui_api.get_key_details_from_host(key_data)
            if not details or not details.get("connection_string"):
                await callback.message.answer(
                    "❌ Не удалось получить актуальную подписку с сервера"
                )
                return

            connection_string = details["connection_string"]

        except Exception as e:
            logger.error(
                f"Ошибка при получении subscription link для ключа {key_id}: {e}",
                exc_info=True,
            )
            await callback.message.answer("❌ Не удалось получить данные подписки")
            return

        # -------------------------------------------------------------------------
        # 4. Загрузка содержимого подписки (base64-encoded список ссылок)
        # -------------------------------------------------------------------------
        try:
            sub_url = _force_https(connection_string)
            status, raw_body = await fetch_via_https(sub_url, timeout=30.0)
            if status != 200:
                await callback.message.answer("❌ Не удалось загрузить конфигурацию")
                return

            content = raw_body.decode("utf-8", errors="replace").strip()

        except Exception as e:
            logger.error(f"Ошибка загрузки подписки {connection_string}: {e}", exc_info=True)
            await callback.message.answer("❌ Произошла ошибка при загрузке ключей")
            return

        # -------------------------------------------------------------------------
        # 5. Декодирование base64 и разбиение на отдельные VLESS-ссылки
        # -------------------------------------------------------------------------
        try:
            decoded_data = base64.b64decode(content).decode("utf-8").strip()
            vless_links = [link.strip() for link in decoded_data.splitlines() if link.strip()]

            if not vless_links:
                await callback.message.answer("❌ В подписке не найдено ни одного ключа")
                return

        except Exception as e:
            logger.error(f"Ошибка декодирования base64-подписки для ключа {key_id}: {e}")
            await callback.message.answer("❌ Некорректный формат данных подписки")
            return

        # -------------------------------------------------------------------------
        # 6. Отправка каждой VLESS-ссылки отдельным сообщением
        # -------------------------------------------------------------------------

        for index, link in enumerate(vless_links, start=1):
            remark_text = ""

            # Извлекаем remark (всё после #), если он есть
            if "#" in link:
                raw_remark = link.split("#")[-1]
                try:
                    decoded_full = unquote(raw_remark)
                    decoded_remark = decoded_full.split("-")[0].strip()
                    remark_text = f"\n🗺️ {decoded_remark}"
                except Exception:
                    # Если декодирование не удалось — показываем как есть
                    remark_text = f"\n🗺️ {raw_remark}"

            # Формируем красивое сообщение
            caption = f"🔑 <b>Ключ в формате vless:// № {index}</b>" f"{remark_text}"

            document = BufferedInputFile(file=link.encode("utf-8"), filename=f"key_{index}.txt")

            try:
                await callback.message.answer_document(
                    document=document, caption=caption, parse_mode="HTML"
                )
            except Exception as send_err:
                logger.error(
                    f"Ошибка отправки VLESS-ссылки №{index} пользователю {callback.from_user.id}: {send_err}"
                )
                # Продолжаем отправку остальных ссылок

        # -------------------------------------------------------------------------
        # 7. Все ссылки отправлены (без итогового сообщения)
        # -------------------------------------------------------------------------
        # Здесь можно добавить финальное сообщение, если потребуется в будущем
        # await callback.message.answer("✅ Все доступные ключи отправлены")

    @user_router.callback_query(F.data.startswith("add_to_happ_"))
    async def add_to_happ_handler(callback: types.CallbackQuery):
        """
        Обработчик callback-запроса для формирования кнопки с deep link для Happ.

        Логика работы:
        1. Проверяет принадлежность ключа текущему пользователю
        2. Получает актуальную ссылку подписки с панели хоста
        3. Формирует deep link happ://add/{SUB_URL}
        4. Отправляет одно сообщение с пояснением и inline-кнопкой

        Формат отправляемого сообщения:
        • Короткий пояснительный текст
        • Кнопка с deep link

        Args:
            callback: CallbackQuery от aiogram (содержит data с key_id)

        Особенности:
        • Защита от доступа к чужим подпискам
        • Обработка ошибок на каждом этапе с понятными сообщениями пользователю
        """
        # Подтверждаем получение запроса (убираем "часики" у кнопки)
        await callback.answer("⏳ Получаю подписку...")

        # -------------------------------------------------------------------------
        # 1. Извлечение ID ключа из callback.data
        # -------------------------------------------------------------------------
        try:
            # Ожидаемый формат: add_to_happ_123 → берём последнюю часть после _
            key_id = int(callback.data.split("_")[-1])
        except (ValueError, IndexError):
            await callback.message.answer("❌ Некорректный идентификатор подписки")
            return

        # -------------------------------------------------------------------------
        # 2. Получение данных ключа из локальной БД + проверка владельца
        # -------------------------------------------------------------------------
        key_data = await asyncio.to_thread(get_key_by_id, key_id)
        if not key_data or key_data.get("user_id") != callback.from_user.id:
            await callback.message.answer("🚫 Это не ваша подписка")
            return

        # -------------------------------------------------------------------------
        # 3. Получение актуальной ссылки подписки с панели
        # -------------------------------------------------------------------------
        try:
            details = await xui_api.get_key_details_from_host(key_data)
            if not details or not details.get("connection_string"):
                await callback.message.answer(
                    "❌ Не удалось получить актуальную подписку с сервера"
                )
                return
            connection_string = details["connection_string"]
        except Exception as e:
            logger.error(
                f"Ошибка при получении subscription link для ключа {key_id}: {e}",
                exc_info=True,
            )
            await callback.message.answer("❌ Не удалось получить данные подписки")
            return

        # -------------------------------------------------------------------------
        # 4. Формирование deep link для Happ
        # -------------------------------------------------------------------------
        happ_link = f"{base64.urlsafe_b64encode(f'happ://add/{connection_string}'.encode()).decode().rstrip('=')}"

        # Формируем итоговую ссылку через локальный редиректор
        final_redirect_url = f"{REDIR_URL}{happ_link}"

        # -------------------------------------------------------------------------
        # 5. Подготовка inline-кнопки
        # -------------------------------------------------------------------------

        keyboard = InlineKeyboardMarkup(
            inline_keyboard=[
                [
                    InlineKeyboardButton(
                        text="⚡ Открыть Happ", url=final_redirect_url, style="primary"
                    )
                ]
            ]
        )

        # -------------------------------------------------------------------------
        # 6. Отправка сообщения с пояснением и кнопкой
        # -------------------------------------------------------------------------
        try:
            await callback.message.answer(
                "\u200c", reply_markup=keyboard, disable_web_page_preview=True
            )
        except Exception as send_err:
            logger.error(
                f"Ошибка отправки сообщения пользователю {callback.from_user.id}: {send_err}"
            )
            await callback.message.answer("❌ Не удалось отправить ссылку")

        # -------------------------------------------------------------------------
        # 7. Сообщение отправлено (без итогового сообщения)
        # -------------------------------------------------------------------------
        # Здесь можно добавить финальное сообщение, если потребуется в будущем
        # await callback.message.answer("✅ Ссылка отправлена")

    @user_router.callback_query(F.data.startswith("add_to_flclashx_"))
    async def add_to_flclashx_handler(callback: types.CallbackQuery):
        """
        Обработчик callback-запроса для формирования кнопки с deep link для FlClashX.

        Логика работы:
        1. Проверяет принадлежность ключа текущему пользователю
        2. Получает актуальную ссылку подписки с панели хоста
        3. Формирует deep link flclashx://install-config?url={CLASH_SUB_URL}
           (CLASH_SUB_URL — та же подписка, но /sub/ заменена на /clash/,
           так как FlClashX — Clash-совместимый клиент, ему нужен именно
           этот формат; сам url обязан быть percent-encoded, иначе "://"
           внутри параметра ломает разбор диплинка самим FlClashX)
        4. Отправляет одно сообщение с пояснением и inline-кнопкой

        Формат отправляемого сообщения:
        • Короткий пояснительный текст
        • Кнопка с deep link

        Args:
            callback: CallbackQuery от aiogram (содержит data с key_id)

        Особенности:
        • Защита от доступа к чужим подпискам
        • Обработка ошибок на каждом этапе с понятными сообщениями пользователю
        """
        # Подтверждаем получение запроса (убираем "часики" у кнопки)
        await callback.answer("⏳ Получаю подписку...")

        # -------------------------------------------------------------------------
        # 1. Извлечение ID ключа из callback.data
        # -------------------------------------------------------------------------
        try:
            # Ожидаемый формат: add_to_flclashx_123 → берём последнюю часть после _
            key_id = int(callback.data.split("_")[-1])
        except (ValueError, IndexError):
            await callback.message.answer("❌ Некорректный идентификатор подписки")
            return

        # -------------------------------------------------------------------------
        # 2. Получение данных ключа из локальной БД + проверка владельца
        # -------------------------------------------------------------------------
        key_data = await asyncio.to_thread(get_key_by_id, key_id)
        if not key_data or key_data.get("user_id") != callback.from_user.id:
            await callback.message.answer("🚫 Это не ваша подписка")
            return

        # -------------------------------------------------------------------------
        # 3. Получение актуальной ссылки подписки с панели
        # -------------------------------------------------------------------------
        try:
            details = await xui_api.get_key_details_from_host(key_data)
            if not details or not details.get("connection_string"):
                await callback.message.answer(
                    "❌ Не удалось получить актуальную подписку с сервера"
                )
                return
            connection_string = details["connection_string"]
        except Exception as e:
            logger.error(
                f"Ошибка при получении subscription link для ключа {key_id}: {e}",
                exc_info=True,
            )
            await callback.message.answer("❌ Не удалось получить данные подписки")
            return

        # -------------------------------------------------------------------------
        # 4. Формирование deep link для FlClashX
        # -------------------------------------------------------------------------
        clash_sub_url = connection_string.replace("/sub/", "/clash/")
        flclashx_link = f"{base64.urlsafe_b64encode(('flclashx://install-config?url=' + quote(clash_sub_url, safe='')).encode()).decode().rstrip('=')}"

        # Формируем итоговую ссылку через локальный редиректор
        final_redirect_url = f"{REDIR_URL}{flclashx_link}"

        # -------------------------------------------------------------------------
        # 5. Подготовка inline-кнопки
        # -------------------------------------------------------------------------

        keyboard = InlineKeyboardMarkup(
            inline_keyboard=[
                [
                    InlineKeyboardButton(
                        text="🪽 Открыть FlClashX", url=final_redirect_url, style="primary"
                    )
                ]
            ]
        )

        # -------------------------------------------------------------------------
        # 6. Отправка сообщения с пояснением и кнопкой
        # -------------------------------------------------------------------------
        try:
            await callback.message.answer(
                "\u200c", reply_markup=keyboard, disable_web_page_preview=True
            )
        except Exception as send_err:
            logger.error(
                f"Ошибка отправки сообщения пользователю {callback.from_user.id}: {send_err}"
            )
            await callback.message.answer("❌ Не удалось отправить ссылку")

        # -------------------------------------------------------------------------
        # 7. Сообщение отправлено (без итогового сообщения)
        # -------------------------------------------------------------------------
        # Здесь можно добавить финальное сообщение, если потребуется в будущем
        # await callback.message.answer("✅ Ссылка отправлена")

    @user_router.callback_query(F.data.startswith("add_to_v2raytun_"))
    async def add_to_v2raytun_handler(callback: types.CallbackQuery):
        """
        Обработчик callback-запроса для формирования кнопки с deep link для v2RayTun.

        Логика работы:
        1. Проверяет принадлежность ключа текущему пользователю
        2. Получает актуальную ссылку подписки с панели хоста
        3. Формирует deep link v2raytun://import/{subscription_link}
        4. Отправляет одно сообщение с пояснением и inline-кнопкой

        Формат отправляемого сообщения:
        • Короткий пояснительный текст
        • Кнопка с deep link

        Args:
            callback: CallbackQuery от aiogram (содержит data с key_id)

        Особенности:
        • Защита от доступа к чужим подпискам
        • Обработка ошибок на каждом этапе с понятными сообщениями пользователю
        """
        # Подтверждаем получение запроса (убираем "часики" у кнопки)
        await callback.answer("⏳ Получаю подписку...")

        # -------------------------------------------------------------------------
        # 1. Извлечение ID ключа из callback.data
        # -------------------------------------------------------------------------
        try:
            # Ожидаемый формат: add_to_v2raytun_123 → берём последнюю часть после _
            key_id = int(callback.data.split("_")[-1])
        except (ValueError, IndexError):
            await callback.message.answer("❌ Некорректный идентификатор подписки")
            return

        # -------------------------------------------------------------------------
        # 2. Получение данных ключа из локальной БД + проверка владельца
        # -------------------------------------------------------------------------
        key_data = await asyncio.to_thread(get_key_by_id, key_id)
        if not key_data or key_data.get("user_id") != callback.from_user.id:
            await callback.message.answer("🚫 Это не ваша подписка")
            return

        # -------------------------------------------------------------------------
        # 3. Получение актуальной ссылки подписки с панели
        # -------------------------------------------------------------------------
        try:
            details = await xui_api.get_key_details_from_host(key_data)
            if not details or not details.get("connection_string"):
                await callback.message.answer(
                    "❌ Не удалось получить актуальную подписку с сервера"
                )
                return
            connection_string = details["connection_string"]
        except Exception as e:
            logger.error(
                f"Ошибка при получении subscription link для ключа {key_id}: {e}",
                exc_info=True,
            )
            await callback.message.answer("❌ Не удалось получить данные подписки")
            return

        # -------------------------------------------------------------------------
        # 4. Формирование deep link для v2RayTun
        # -------------------------------------------------------------------------
        v2raytun_link = f"{base64.urlsafe_b64encode(f'v2raytun://import/{connection_string}'.encode()).decode().rstrip('=')}"

        # Формируем итоговую ссылку через локальный редиректор
        final_redirect_url = f"{REDIR_URL}{v2raytun_link}"

        # -------------------------------------------------------------------------
        # 5. Подготовка inline-кнопки
        # -------------------------------------------------------------------------

        keyboard = InlineKeyboardMarkup(
            inline_keyboard=[
                [
                    InlineKeyboardButton(
                        text="🚀 Открыть v2RayTun",
                        url=final_redirect_url,
                        style="primary",
                    )
                ]
            ]
        )

        # -------------------------------------------------------------------------
        # 6. Отправка сообщения с пояснением и кнопкой
        # -------------------------------------------------------------------------
        try:
            await callback.message.answer(
                "\u200c", reply_markup=keyboard, disable_web_page_preview=True
            )
        except Exception as send_err:
            logger.error(
                f"Ошибка отправки сообщения пользователю {callback.from_user.id}: {send_err}"
            )
            await callback.message.answer("❌ Не удалось отправить ссылку")

        # -------------------------------------------------------------------------
        # 7. Сообщение отправлено (без итогового сообщения)
        # -------------------------------------------------------------------------
        # Здесь можно добавить финальное сообщение, если потребуется в будущем
        # await callback.message.answer("✅ Ссылка отправлена")

    @user_router.callback_query(F.data.startswith("add_to_incy_"))
    async def add_to_incy_handler(callback: types.CallbackQuery):
        """
        Обработчик callback-запроса для формирования кнопки с deep link для INCY.

        Логика работы:
        1. Проверяет принадлежность ключа текущему пользователю
        2. Получает актуальную ссылку подписки с панели хоста
        3. Формирует deep link incy://add/{SUB_URL}
        4. Отправляет одно сообщение с пояснением и inline-кнопкой

        Формат отправляемого сообщения:
        • Короткий пояснительный текст
        • Кнопка с deep link

        Args:
            callback: CallbackQuery от aiogram (содержит data с key_id)

        Особенности:
        • Защита от доступа к чужим подпискам
        • Обработка ошибок на каждом этапе с понятными сообщениями пользователю
        """
        # Подтверждаем получение запроса (убираем "часики" у кнопки)
        await callback.answer("⏳ Получаю подписку...")

        # -------------------------------------------------------------------------
        # 1. Извлечение ID ключа из callback.data
        # -------------------------------------------------------------------------
        try:
            # Ожидаемый формат: add_to_incy_123 → берём последнюю часть после _
            key_id = int(callback.data.split("_")[-1])
        except (ValueError, IndexError):
            await callback.message.answer("❌ Некорректный идентификатор подписки")
            return

        # -------------------------------------------------------------------------
        # 2. Получение данных ключа из локальной БД + проверка владельца
        # -------------------------------------------------------------------------
        key_data = await asyncio.to_thread(get_key_by_id, key_id)
        if not key_data or key_data.get("user_id") != callback.from_user.id:
            await callback.message.answer("🚫 Это не ваша подписка")
            return

        # -------------------------------------------------------------------------
        # 3. Получение актуальной ссылки подписки с панели
        # -------------------------------------------------------------------------
        try:
            details = await xui_api.get_key_details_from_host(key_data)
            if not details or not details.get("connection_string"):
                await callback.message.answer(
                    "❌ Не удалось получить актуальную подписку с сервера"
                )
                return
            connection_string = details["connection_string"]
        except Exception as e:
            logger.error(
                f"Ошибка при получении subscription link для ключа {key_id}: {e}",
                exc_info=True,
            )
            await callback.message.answer("❌ Не удалось получить данные подписки")
            return

        # -------------------------------------------------------------------------
        # 4. Формирование deep link для INCY
        # -------------------------------------------------------------------------
        incy_link = f"{base64.urlsafe_b64encode(f'incy://add/{connection_string}'.encode()).decode().rstrip('=')}"

        # Формируем итоговую ссылку через локальный редиректор
        final_redirect_url = f"{REDIR_URL}{incy_link}"

        # -------------------------------------------------------------------------
        # 5. Подготовка inline-кнопки
        # -------------------------------------------------------------------------

        keyboard = InlineKeyboardMarkup(
            inline_keyboard=[
                [
                    InlineKeyboardButton(
                        text="🛡️ Открыть INCY", url=final_redirect_url, style="primary"
                    )
                ]
            ]
        )

        # -------------------------------------------------------------------------
        # 6. Отправка сообщения с пояснением и кнопкой
        # -------------------------------------------------------------------------
        try:
            await callback.message.answer(
                "\u200c", reply_markup=keyboard, disable_web_page_preview=True
            )
        except Exception as send_err:
            logger.error(
                f"Ошибка отправки сообщения пользователю {callback.from_user.id}: {send_err}"
            )
            await callback.message.answer("❌ Не удалось отправить ссылку")

        # -------------------------------------------------------------------------
        # 7. Сообщение отправлено (без итогового сообщения)
        # -------------------------------------------------------------------------
        # Здесь можно добавить финальное сообщение, если потребуется в будущем
        # await callback.message.answer("✅ Ссылка отправлена")

    @user_router.callback_query(F.data.startswith("add_to_v2rayng_"))
    async def add_to_v2rayng_handler(callback: types.CallbackQuery):
        """
        Обработчик callback-запроса для формирования кнопки с deep link для v2rayNG.

        Логика работы:
        1. Проверяет принадлежность ключа текущему пользователю
        2. Получает актуальную ссылку подписки с панели хоста
        3. Формирует deep link v2rayng://install-config/?url=SubLink
        4. Отправляет одно сообщение с пояснением и inline-кнопкой

        Формат отправляемого сообщения:
        • Короткий пояснительный текст
        • Кнопка с deep link

        Args:
            callback: CallbackQuery от aiogram (содержит data с key_id)

        Особенности:
        • Защита от доступа к чужим подпискам
        • Обработка ошибок на каждом этапе с понятными сообщениями пользователю
        """
        # Подтверждаем получение запроса (убираем "часики" у кнопки)
        await callback.answer("⏳ Получаю подписку...")

        # -------------------------------------------------------------------------
        # 1. Извлечение ID ключа из callback.data
        # -------------------------------------------------------------------------
        try:
            # Ожидаемый формат: add_to_v2rayng_123 → берём последнюю часть после _
            key_id = int(callback.data.split("_")[-1])
        except (ValueError, IndexError):
            await callback.message.answer("❌ Некорректный идентификатор подписки")
            return

        # -------------------------------------------------------------------------
        # 2. Получение данных ключа из локальной БД + проверка владельца
        # -------------------------------------------------------------------------
        key_data = await asyncio.to_thread(get_key_by_id, key_id)
        if not key_data or key_data.get("user_id") != callback.from_user.id:
            await callback.message.answer("🚫 Это не ваша подписка")
            return

        # -------------------------------------------------------------------------
        # 3. Получение актуальной ссылки подписки с панели
        # -------------------------------------------------------------------------
        try:
            details = await xui_api.get_key_details_from_host(key_data)
            if not details or not details.get("connection_string"):
                await callback.message.answer(
                    "❌ Не удалось получить актуальную подписку с сервера"
                )
                return
            connection_string = details["connection_string"]
        except Exception as e:
            logger.error(
                f"Ошибка при получении subscription link для ключа {key_id}: {e}",
                exc_info=True,
            )
            await callback.message.answer("❌ Не удалось получить данные подписки")
            return

        # -------------------------------------------------------------------------
        # 4. Формирование deep link для v2rayNG
        # -------------------------------------------------------------------------
        v2rayng_link = f"{base64.urlsafe_b64encode(f'v2rayng://install-config/?url={connection_string}'.encode()).decode().rstrip('=')}"

        # Формируем итоговую ссылку через локальный редиректор
        final_redirect_url = f"{REDIR_URL}{v2rayng_link}"

        # -------------------------------------------------------------------------
        # 5. Подготовка inline-кнопки
        # -------------------------------------------------------------------------

        keyboard = InlineKeyboardMarkup(
            inline_keyboard=[
                [
                    InlineKeyboardButton(
                        text="👾 Открыть v2rayNG",
                        url=final_redirect_url,
                        style="primary",
                    )
                ]
            ]
        )

        # -------------------------------------------------------------------------
        # 6. Отправка сообщения с пояснением и кнопкой
        # -------------------------------------------------------------------------
        try:
            await callback.message.answer(
                "\u200c", reply_markup=keyboard, disable_web_page_preview=True
            )
        except Exception as send_err:
            logger.error(
                f"Ошибка отправки сообщения пользователю {callback.from_user.id}: {send_err}"
            )
            await callback.message.answer("❌ Не удалось отправить ссылку")

        # -------------------------------------------------------------------------
        # 7. Сообщение отправлено (без итогового сообщения)
        # -------------------------------------------------------------------------
        # Здесь можно добавить финальное сообщение, если потребуется в будущем
        # await callback.message.answer("✅ Ссылка отправлена")

    @user_router.callback_query(F.data.startswith("open_web_app_"))
    async def open_web_app_handler(callback: types.CallbackQuery):
        """
        Обработчик callback-запроса для формирования кнопки со ссылкой на Web App.

        Логика работы:
        1. Проверяет принадлежность ключа текущему пользователю
        2. Получает актуальную ссылку подписки с панели хоста
        3. Формирует ссылку
        4. Отправляет одно сообщение с пояснением и inline-кнопкой

        Формат отправляемого сообщения:
        • Короткий пояснительный текст
        • Кнопка со ссылкой

        Args:
            callback: CallbackQuery от aiogram (содержит data с key_id)

        Особенности:
        • Защита от доступа к чужим подпискам
        • Обработка ошибок на каждом этапе с понятными сообщениями пользователю
        """
        # Подтверждаем получение запроса (убираем "часики" у кнопки)
        await callback.answer("⏳ Получаю подписку...")

        # -------------------------------------------------------------------------
        # 1. Извлечение ID ключа из callback.data
        # -------------------------------------------------------------------------
        try:
            # Ожидаемый формат: open_web_app_123 → берём последнюю часть после _
            key_id = int(callback.data.split("_")[-1])
        except (ValueError, IndexError):
            await callback.message.answer("❌ Некорректный идентификатор подписки")
            return

        # -------------------------------------------------------------------------
        # 2. Получение данных ключа из локальной БД + проверка владельца
        # -------------------------------------------------------------------------
        key_data = await asyncio.to_thread(get_key_by_id, key_id)
        if not key_data or key_data.get("user_id") != callback.from_user.id:
            await callback.message.answer("🚫 Это не ваша подписка")
            return

        # -------------------------------------------------------------------------
        # 3. Получение актуальной ссылки подписки с панели
        # -------------------------------------------------------------------------
        try:
            details = await xui_api.get_key_details_from_host(key_data)
            if not details or not details.get("connection_string"):
                await callback.message.answer(
                    "❌ Не удалось получить актуальную подписку с сервера"
                )
                return
            connection_string = details["connection_string"]
        except Exception as e:
            logger.error(
                f"Ошибка при получении subscription link для ключа {key_id}: {e}",
                exc_info=True,
            )
            await callback.message.answer("❌ Не удалось получить данные подписки")
            return

        # -------------------------------------------------------------------------
        # 4. Формирование ссылки для Web App
        # -------------------------------------------------------------------------
        web_app_link = f"{connection_string}"

        # Формируем итоговую ссылку через локальный редиректор
        final_redirect_url = f"{web_app_link}"

        # -------------------------------------------------------------------------
        # 5. Подготовка inline-кнопки
        # -------------------------------------------------------------------------

        keyboard = InlineKeyboardMarkup(
            inline_keyboard=[
                [
                    InlineKeyboardButton(
                        text="🌐 Открыть Web App",
                        web_app=WebAppInfo(url=final_redirect_url),
                        style="success",
                    )
                ]
            ]
        )

        # -------------------------------------------------------------------------
        # 6. Отправка сообщения с пояснением и кнопкой
        # -------------------------------------------------------------------------
        try:
            await callback.message.answer(
                "\u200c", reply_markup=keyboard, disable_web_page_preview=True
            )
        except Exception as send_err:
            logger.error(
                f"Ошибка отправки сообщения пользователю {callback.from_user.id}: {send_err}"
            )
            await callback.message.answer("❌ Не удалось отправить ссылку")

        # -------------------------------------------------------------------------
        # 7. Сообщение отправлено (без итогового сообщения)
        # -------------------------------------------------------------------------
        # Здесь можно добавить финальное сообщение, если потребуется в будущем
        # await callback.message.answer("✅ Ссылка отправлена")

    @user_router.callback_query(F.data.startswith("switch_server_"))
    @registration_required
    async def switch_server_start(callback: types.CallbackQuery):
        await callback.answer()
        try:
            key_id = int(callback.data[len("switch_server_") :])
        except ValueError:
            await callback.answer("❌ Некорректный идентификатор подписки", show_alert=False)
            return

        key_data = await asyncio.to_thread(get_key_by_id, key_id)
        if not key_data or key_data.get("user_id") != callback.from_user.id:
            await callback.answer("❌ Подписка не найдена", show_alert=False)
            return

        hosts = await asyncio.to_thread(get_all_hosts)
        if not hosts:
            await callback.answer("❌ Нет доступных серверов", show_alert=False)
            return

        current_host = key_data.get("host_name")
        hosts = [h for h in hosts if h.get("host_name") != current_host]
        if not hosts:
            await callback.answer("❌ Другие серверы отсутствуют", show_alert=False)
            return

        await safe_edit_text(callback, 
            "🌍 <b>Выберите новый сервер (локацию) для этой подписки:</b>",
            reply_markup=keyboards.create_host_selection_keyboard(hosts, action=f"switch_{key_id}"),
            parse_mode="HTML",
        )

    async def _delayed_delete_old_host(
        key_id: int, old_host: str, email: str, new_host_name: str, delay_seconds: int
    ):
        """Ждёт delay_seconds после доставки сообщения со ссылкой и только
        затем удаляет клиента со старого хоста. Сама снимает флаг
        keys_in_transition по завершении — вне зависимости от исхода."""
        try:
            await asyncio.sleep(delay_seconds)
            try:
                await xui_api.delete_client_on_host(old_host, email)
            except Exception:
                logger.error(
                    f"Не удалось удалить ключ {key_id} со старого хоста "
                    f"{old_host} после переноса на {new_host_name} "
                    f"(отложенное удаление через {delay_seconds} сек.)",
                    exc_info=True,
                )
        finally:
            keys_in_transition.discard(key_id)

    async def _switch_key_to_host(callback: types.CallbackQuery, key_id: int, new_host_name: str):
        key_data = await asyncio.to_thread(get_key_by_id, key_id)

        if not key_data or key_data.get("user_id") != callback.from_user.id:
            await callback.answer("❌ Подписка не найдена", show_alert=False)
            return

        old_host = key_data.get("host_name")
        if not old_host:
            await callback.answer("❌ Для подписки не указан текущий сервер", show_alert=False)
            return

        if new_host_name == old_host:
            await callback.answer("❌ Это уже текущий сервер", show_alert=False)
            return

        try:
            expiry_dt = datetime.fromisoformat(key_data["expiry_date"])
            expiry_timestamp_ms_exact = int(expiry_dt.timestamp() * 1000)
        except Exception:
            now_dt = datetime.now()
            expiry_timestamp_ms_exact = int((now_dt + timedelta(days=1)).timestamp() * 1000)

        email = key_data.get("key_email")
        if not email:
            await callback.answer(
                "❌ Не удалось определить email подписки. Обратитесь в поддержку",
                show_alert=False,
            )
            return

        loading_message = await delete_and_send(
            callback.message, f"⏳ Переношу подписку на сервер {new_host_name}..."
        )

        # Ставим флаг до любых операций с панелями — на весь перенос целиком
        # Пока он выставлен, sync_keys_with_panels должен полностью пропускать
        # этот key_id (см. scheduler.py)
        keys_in_transition.add(key_id)

        try:
            try:
                result = await xui_api.create_or_update_key_on_host(
                    new_host_name,
                    email,
                    days_to_add=None,
                    expiry_timestamp_ms=expiry_timestamp_ms_exact,
                )
                if not result:
                    keys_in_transition.discard(key_id)
                    await delete_and_send(loading_message, 
                        f"❌ Не удалось перенести подписку на сервер {new_host_name}. Попробуйте позже."
                    )
                    return

                # Переключаем БД на новый сервер — с этого момента
                # get_keys_for_host(old_host) уже не вернёт этот ключ, и
                # scheduler не будет восстанавливать его на старом хосте
                update_key_host_and_info(
                    key_id=key_id,
                    new_host_name=new_host_name,
                    new_xui_uuid=result["client_uuid"],
                    new_expiry_ms=result["expiry_timestamp_ms"],
                )

                # Старый хост не удаляем здесь — только после того, как
                # пользователю реально доставлено сообщение с новой ссылкой
                # (см. ниже). Пока сообщение не дошло, старая подписка
                # остаётся рабочей как страховка.
                try:
                    updated_key = await asyncio.to_thread(get_key_by_id, key_id)
                    details = await xui_api.get_key_details_from_host(updated_key)
                    if details and details.get("connection_string"):
                        connection_string = details["connection_string"]
                        expiry_date = datetime.fromisoformat(updated_key["expiry_date"])
                        created_date = datetime.fromisoformat(updated_key["created_date"])
                        all_user_keys = await asyncio.to_thread(
                            get_user_keys, callback.from_user.id
                        )
                        key_number = next(
                            (i + 1 for i, k in enumerate(all_user_keys) if k["key_id"] == key_id),
                            0,
                        )
                        final_text = get_key_info_text(
                            key_number, expiry_date, created_date, connection_string
                        )
                        await loading_message.edit_text(
                            text=final_text,
                            reply_markup=keyboards.create_key_info_keyboard(
                                key_id, connection_string
                            ),
                        )

                        # Сообщение с новой ссылкой доставлено — планируем
                        # удаление со старого хоста с задержкой (даём клиенту
                        # время реально переключиться), не блокируя ответ юзеру
                        asyncio.create_task(
                            _delayed_delete_old_host(
                                key_id, old_host, email, new_host_name,
                                OLD_HOST_DELETE_DELAY_SECONDS,
                            )
                        )
                    else:
                        # Ссылку получить не удалось — старый хост оставляем
                        # как есть, ничего не удаляем; перенос завершён
                        keys_in_transition.discard(key_id)
                        logger.warning(
                            f"Ключ {key_id}: перенос на {new_host_name} выполнен, но "
                            f"connection_string не получен — старый хост {old_host} "
                            "оставлен без изменений до следующей попытки."
                        )
                        await loading_message.edit_text(
                            f"✅ <b>Готово! Подписка перенесена на сервер {new_host_name}</b>\n\n"
                            "🔄 Обновите подписку в клиенте, если требуется",
                            reply_markup=keyboards.create_back_to_menu_keyboard(),
                        )
                except Exception:
                    # Не удалось ни получить ссылку, ни отправить сообщение —
                    # старый хост не трогаем, подписка остаётся рабочей на нём
                    keys_in_transition.discard(key_id)
                    logger.warning(
                        f"Ключ {key_id}: не удалось доставить сообщение с новой "
                        f"ссылкой после переноса на {new_host_name} — старый хост "
                        f"{old_host} оставлен без изменений.",
                        exc_info=True,
                    )
                    await loading_message.edit_text(
                        f"✅ <b>Готово! Подписка перенесена на сервер {new_host_name}</b>\n\n"
                        "🔄 Обновите подписку в клиенте, если требуется",
                        reply_markup=keyboards.create_back_to_menu_keyboard(),
                    )
            except Exception as e:
                keys_in_transition.discard(key_id)
                logger.error(
                    f"Ошибка при переключении ключа {key_id} на сервер {new_host_name}: {e}",
                    exc_info=True,
                )
                await delete_and_send(loading_message, 
                    "❌ Произошла ошибка при переносе подписки. Попробуйте позже."
                )
        except Exception:
            # Подстраховка на случай неожиданной ошибки выше уровня внутреннего
            # try — снимаем флаг, чтобы ключ не завис в транзите навсегда
            keys_in_transition.discard(key_id)
            raise

    @user_router.callback_query(F.data.startswith("select_host_switch_"))
    @registration_required
    async def select_host_for_switch(callback: types.CallbackQuery):
        payload = callback.data[len("select_host_switch_") :]
        parts = payload.split("_", 1)
        if len(parts) != 2:
            await callback.answer("❌ Некорректные данные выбора сервера", show_alert=False)
            return
        try:
            key_id = int(parts[0])
        except ValueError:
            await callback.answer("❌ Некорректный идентификатор подписки", show_alert=False)
            return
        new_host_name = parts[1]
        await _switch_key_to_host(callback, key_id, new_host_name)

    async def handle_switch_host(callback: types.CallbackQuery, key_id: int, new_host_name: str):
        await _switch_key_to_host(callback, key_id, new_host_name)

    @user_router.callback_query(F.data.startswith("show_qr_"))
    @registration_required
    async def show_qr_handler(callback: types.CallbackQuery):
        await callback.answer("⏳ Генерирую QR-код...", show_alert=False)
        key_id = int(callback.data.split("_")[2])

        # -------------------------------------------------------------------------
        # 1. Асинхронное получение данных ключа из базы данных
        # -------------------------------------------------------------------------
        key_data = await asyncio.to_thread(get_key_by_id, key_id)
        if not key_data or key_data["user_id"] != callback.from_user.id:
            return

        try:
            # -------------------------------------------------------------------------
            # 2. Запрос строки подключения с панели X-UI
            # -------------------------------------------------------------------------
            details = await xui_api.get_key_details_from_host(key_data)
            if not details or not details["connection_string"]:
                await callback.answer("❌ Не удалось сгенерировать QR-код", show_alert=False)
                return

            connection_string = details["connection_string"]

            # -------------------------------------------------------------------------
            # 3. Вынесение генерации графики QR-кода в пул потоков (Thread Pool)
            # -------------------------------------------------------------------------
            def _generate_qr_sync(text: str) -> bytes:
                """Изолированная синхронная функция для отрисовки графики в потоке."""
                img = qrcode.make(text)
                bio = BytesIO()
                img.save(bio, "PNG")
                return bio.getvalue()

            # Запускаем тяжелый рендеринг изображения без блокировки Event Loop
            qr_bytes = await asyncio.to_thread(_generate_qr_sync, connection_string)
            qr_code_file = BufferedInputFile(qr_bytes, filename="vpn_qr.png")

            # -------------------------------------------------------------------------
            # 4. Отправка готовой картинки пользователю
            # -------------------------------------------------------------------------
            await callback.message.answer_photo(photo=qr_code_file)

        except Exception as e:
            logger.error(f"Ошибка при отображении QR-кода для ключа {key_id}: {e}", exc_info=True)

    @user_router.callback_query(F.data.startswith("howto_vless_"))
    @registration_required
    async def show_instruction_for_key_handler(callback: types.CallbackQuery):
        await callback.answer()
        key_id = int(callback.data.split("_")[2])
        try:
            await safe_edit_text(callback, 
                "📚 <b>Выберите вашу платформу для получения инструкции по подключению:</b>",
                reply_markup=keyboards.create_howto_vless_keyboard_key(key_id),
                disable_web_page_preview=True,
                parse_mode="HTML",
            )
        except TelegramBadRequest:
            pass

    @user_router.callback_query(F.data.startswith("howto_vless"))
    @registration_required
    async def show_instruction_handler(callback: types.CallbackQuery):
        await callback.answer()

        try:
            await safe_edit_text(callback, 
                "📚 <b>Выберите вашу платформу для получения инструкции по подключению:</b>",
                reply_markup=keyboards.create_howto_vless_keyboard(),
                disable_web_page_preview=True,
                parse_mode="HTML",
            )
        except TelegramBadRequest:
            pass

    @user_router.callback_query(F.data == "user_speedtest")
    @registration_required
    async def user_speedtest_handler(callback: types.CallbackQuery):
        await callback.answer()

        try:
            # Получаем список хостов
            hosts = await asyncio.to_thread(get_all_hosts) or []
            if not hosts:
                await safe_edit_text(callback, 
                    "⚠️ Хосты не найдены в настройках. Обратитесь к администратору",
                    reply_markup=keyboards.create_back_to_main_menu_keyboard(),
                )
                return

            # Показываем последние результаты тестов скорости для всех хостов
            text = "⚡️ <b>Последние результаты Speedtest</b>\n\n"

            from shop_bot.data_manager.database import get_latest_speedtest

            for host in hosts:
                host_name = host.get("host_name", "Неизвестный сервер")
                latest_test = get_latest_speedtest(host_name)

                if latest_test:
                    ping = latest_test.get("ping_ms")
                    download = latest_test.get("download_mbps")
                    upload = latest_test.get("upload_mbps")
                    method = latest_test.get("method", "unknown").upper()
                    created_at = latest_test.get("created_at", "—")

                    # Форматируем время в нужном формате
                    try:
                        from datetime import datetime

                        if created_at and created_at != "—":
                            # created_at приходит из host_speedtests.created_at,
                            # который теперь пишется через datetime('now','localtime')
                            # в SQLite — то есть уже нативно в МСК, как и Python
                            # datetime.now() в этом контейнере (TZ=Europe/Moscow).
                            # Ручной сдвиг +3ч больше не нужен и только испортил
                            # бы время (сдвинул на 6 часов вместо 3).
                            dt = datetime.fromisoformat(created_at)
                            time_str = dt.strftime("%d.%m %H:%M")
                        else:
                            time_str = "—"
                    except Exception as e:
                        logger.warning(
                            f"Не удалось отформатировать время последнего "
                            f"speedtest ({created_at!r}): {e}"
                        )
                        time_str = created_at

                    # Форматируем значения
                    ping_str = f"{ping:.2f}" if ping is not None else "—"
                    download_str = f"{download:.0f}" if download is not None else "—"
                    upload_str = f"{upload:.0f}" if upload is not None else "—"

                    # Создаем строку в нужном формате
                    text += (
                        f"🗺️ <b>{host_name}</b>\n"
                        f"🟢 {method} | ⏳ {ping_str} ms | ⬇️ {download_str} Mbps | ⬆️ {upload_str} Mbps | 🕒 {time_str}\n\n"
                    )
                else:
                    text += f"<b>• {host_name}</b>\n" f"❌ Нет данных о тестах скорости\n\n"

            await safe_edit_text(callback, 
                text,
                reply_markup=keyboards.create_back_to_main_menu_keyboard(),
                disable_web_page_preview=True,
            )
        except TelegramBadRequest:
            pass

    @user_router.callback_query(F.data == "howto_android")
    @registration_required
    async def howto_android_handler(callback: types.CallbackQuery):
        await callback.answer()
        try:
            await safe_edit_text(callback, 
                (
                    get_setting("howto_android_text")
                    or (
                        "👾 <a href='https://telegra.ph/Osnovnye-klienty-na-Android-Rekomendovano-11-20'>Инструкция для Android</a>"
                    )
                ),
                reply_markup=keyboards.create_howto_vless_keyboard(),
                disable_web_page_preview=True,
            )
        except TelegramBadRequest:
            pass

    @user_router.callback_query(F.data == "howto_ios")
    @registration_required
    async def howto_ios_handler(callback: types.CallbackQuery):
        await callback.answer()
        try:
            await safe_edit_text(callback, 
                (
                    get_setting("howto_ios_text")
                    or (
                        "🍏 <a href='https://telegra.ph/Osnovnye-klienty-na-iOS-Rekomendovano-11-20'>Инструкция для iOS</a>"
                    )
                ),
                reply_markup=keyboards.create_howto_vless_keyboard(),
                disable_web_page_preview=True,
            )
        except TelegramBadRequest:
            pass

    @user_router.callback_query(F.data == "howto_windows")
    @registration_required
    async def howto_windows_handler(callback: types.CallbackQuery):
        await callback.answer()
        try:
            await safe_edit_text(callback, 
                (
                    get_setting("howto_windows_text")
                    or (
                        "💻 <a href='https://telegra.ph/Osnovnye-klienty-na-Windows-Rekomendovano-11-20'>Инструкция для Windows</a>"
                    )
                ),
                reply_markup=keyboards.create_howto_vless_keyboard(),
                disable_web_page_preview=True,
            )
        except TelegramBadRequest:
            pass

    @user_router.callback_query(F.data == "howto_linux")
    @registration_required
    async def howto_linux_handler(callback: types.CallbackQuery):
        await callback.answer()
        try:
            await safe_edit_text(callback, 
                (
                    get_setting("howto_linux_text")
                    or (
                        "🐧 <a href='https://telegra.ph/Osnovnye-klienty-na-Linux-Rekomendovano-11-20'>Инструкция для Linux</a>"
                    )
                ),
                reply_markup=keyboards.create_howto_vless_keyboard(),
                disable_web_page_preview=True,
            )
        except TelegramBadRequest:
            pass

    @user_router.callback_query(F.data == "buy_new_key")
    @registration_required
    async def buy_new_key_handler(callback: types.CallbackQuery):
        await callback.answer()
        hosts = await asyncio.to_thread(get_all_hosts)
        if not hosts:
            await delete_and_send(callback.message, 
                "❌ В данный момент нет доступных серверов для покупки"
            )
            return

        await safe_edit_text(callback, 
            "🗺️ <b>Выберите сервер, на котором хотите приобрести подписку:</b>",
            reply_markup=keyboards.create_host_selection_keyboard(hosts, action="new"),
            parse_mode="HTML",
        )

    @user_router.callback_query(F.data.startswith("extend_key_"))
    @registration_required
    async def extend_key_handler(callback: types.CallbackQuery):
        await callback.answer()

        try:
            key_id = int(callback.data.split("_")[2])
        except (IndexError, ValueError):
            await delete_and_send(callback.message, "❌ Произошла ошибка. Неверный формат подписки")
            return

        key_data = await asyncio.to_thread(get_key_by_id, key_id)

        if not key_data or key_data["user_id"] != callback.from_user.id:
            await delete_and_send(callback.message, "❌ Подписка не найдена или не принадлежит вам")
            return

        host_name = key_data.get("host_name")
        if not host_name:
            await delete_and_send(callback.message, 
                "❌ У этой подписки не указан сервер. Обратитесь в поддержку"
            )
            return

        plans = get_plans_for_host(host_name)

        if not plans:
            await delete_and_send(callback.message, 
                f"❌ Извините, для сервера {host_name} в данный момент не настроены тарифы для продления."
            )
            return

        await safe_edit_text(callback, 
            f"📦 <b>Выберите тариф для продления подписки на сервере {host_name}:</b>",
            reply_markup=keyboards.create_plans_keyboard(
                plans=plans, action="extend", host_name=host_name, key_id=key_id
            ),
            parse_mode="HTML",
        )

    @user_router.callback_query(F.data.startswith("buy_"))
    @registration_required
    async def plan_selection_handler(callback: types.CallbackQuery, state: FSMContext):
        await callback.answer()

        parts = callback.data.split("_")
        token = parts[1]
        plan_id = int(parts[2])
        action = parts[3]
        key_id = int(parts[4]) if len(parts) > 4 else 0

        # Находим сервер по токену
        hosts = await asyncio.to_thread(get_all_hosts)
        host = keyboards.find_host_by_callback_token(hosts, token)
        if not host:
            await callback.answer("❌ Тариф недоступен", show_alert=False)
            return
        host_name = host["host_name"]

        await state.update_data(
            action=action,
            key_id=key_id,
            plan_id=plan_id,
            host_name=host_name,
            flow_chat_id=callback.message.chat.id,
            flow_msg_id=callback.message.message_id,
        )

        await safe_edit_text(callback, 
            "📧 <b>Пожалуйста, введите ваш email для отправки чека об оплате:</b>\n\n"
            "Если вы не хотите указывать почту, нажмите кнопку ниже",
            reply_markup=keyboards.create_skip_email_keyboard(),
            parse_mode="HTML",
        )
        await state.set_state(PaymentProcess.waiting_for_email)

    @user_router.callback_query(PaymentProcess.waiting_for_email, F.data == "back_to_plans")
    async def back_to_plans_handler(callback: types.CallbackQuery, state: FSMContext):
        data = await state.get_data()
        action = data.get("action")
        host_name = data.get("host_name")
        key_id = data.get("key_id")

        try:
            await callback.answer()
        except Exception:
            pass

        try:
            if action == "extend" and host_name and (key_id is not None):
                plans = get_plans_for_host(host_name)
                if plans:
                    await safe_edit_text(callback, 
                        f"📦 <b>Выберите тариф для продления подписки на сервере {host_name}:</b>",
                        reply_markup=keyboards.create_plans_keyboard(
                            plans=plans,
                            action="extend",
                            host_name=host_name,
                            key_id=int(key_id),
                        ),
                        parse_mode="HTML",
                    )
                else:
                    await delete_and_send(callback.message, 
                        f"❌ Для сервера {host_name} не настроены тарифы."
                    )
            elif action == "new" and host_name:
                plans = get_plans_for_host(host_name)
                if plans:
                    await safe_edit_text(callback, 
                        "📦 <b>Выберите тариф для новой подписки:</b>",
                        reply_markup=keyboards.create_plans_keyboard(
                            plans=plans, action="new", host_name=host_name
                        ),
                        parse_mode="HTML",
                    )
                else:
                    await delete_and_send(callback.message, 
                        f"❌ Для сервера {host_name} не настроены тарифы."
                    )
            elif action == "new":
                hosts = await asyncio.to_thread(get_all_hosts)
                if not hosts:
                    await delete_and_send(callback.message, 
                        "❌ В данный момент нет доступных серверов для покупки"
                    )
                else:
                    await safe_edit_text(callback, 
                        "🗺️ <b>Выберите сервер, на котором хотите приобрести подписку:</b>",
                        reply_markup=keyboards.create_host_selection_keyboard(hosts, action="new"),
                        parse_mode="HTML",
                    )
            else:
                await show_main_menu(callback.message, edit_message=True)
        finally:
            try:
                await state.clear()
            except Exception:
                pass

    @user_router.message(PaymentProcess.waiting_for_email)
    async def process_email_handler(message: types.Message, state: FSMContext):
        if is_valid_email(message.text):
            await state.update_data(customer_email=message.text)
            data = await state.get_data()
            try:
                await message.delete()
            except TelegramBadRequest:
                pass

            # Показываем опции оплаты с учетом балансов и цены
            await show_payment_options(
                message.bot,
                data.get("flow_chat_id", message.chat.id),
                data.get("flow_msg_id"),
                state,
                note=f"✅ Email принят: {message.text}",
            )
            logger.info(
                f"Пользователь {message.chat.id}: установлено состояние waiting_for_payment_method через show_payment_options"
            )
        else:
            try:
                await message.delete()
            except Exception:
                pass
            await message.answer("❌ Неверный формат email. Попробуйте еще раз")

    @user_router.callback_query(PaymentProcess.waiting_for_email, F.data == "skip_email")
    async def skip_email_handler(callback: types.CallbackQuery, state: FSMContext):
        await callback.answer()
        await state.update_data(customer_email=None)
        data = await state.get_data()

        # Показываем опции оплаты с учетом балансов и цены
        await show_payment_options(
            callback.bot,
            data.get("flow_chat_id", callback.message.chat.id),
            data.get("flow_msg_id", callback.message.message_id),
            state,
        )
        logger.info(
            f"Пользователь {callback.from_user.id}: установлено состояние waiting_for_payment_method через show_payment_options"
        )

    async def show_payment_options(
        bot: Bot,
        chat_id: int,
        message_id: int | None,
        state: FSMContext,
        note: str | None = None,
    ):
        data = await state.get_data()
        user_data = get_user(chat_id)
        plan = get_plan_by_id(data.get("plan_id"))

        if not plan:
            if message_id:
                try:
                    await bot.edit_message_text(
                        "❌ Тариф не найден", chat_id=chat_id, message_id=message_id
                    )
                except TelegramBadRequest:
                    pass
            await state.clear()
            return

        price = Decimal(str(plan["price"]))
        final_price = price
        message_text = CHOOSE_PAYMENT_METHOD_MESSAGE

        if user_data.get("referred_by") and user_data.get("total_spent", 0) == 0:
            discount_percentage_str = get_setting("referral_discount") or "0"
            discount_percentage = Decimal(discount_percentage_str)

            if discount_percentage > 0:
                discount_amount = (price * discount_percentage / 100).quantize(Decimal("0.01"))
                final_price = price - discount_amount

                message_text = (
                    f"🎉 <b>Как приглашенному пользователю, на вашу первую покупку предоставляется скидка {discount_percentage_str}%!</b>\n\n"
                    f"<b>Старая цена:</b> <s>{price:.2f} ₽</s>\n"
                    f"<b>Новая цена:</b> {final_price:.2f} ₽\n\n"
                ) + CHOOSE_PAYMENT_METHOD_MESSAGE

        # Промокод (если уже применён)
        promo_percent = data.get("promo_discount_percent")
        promo_amount = data.get("promo_discount_amount")
        promo_code = (data.get("promo_code") or "").strip()
        if promo_code:
            try:
                if promo_percent:
                    perc = Decimal(str(promo_percent))
                    if perc > 0:
                        discount_amount = (final_price * perc / 100).quantize(Decimal("0.01"))
                        final_price = (final_price - discount_amount).quantize(Decimal("0.01"))
                elif promo_amount:
                    amt = Decimal(str(promo_amount))
                    if amt > 0:
                        final_price = (final_price - amt).quantize(Decimal("0.01"))
                if final_price < Decimal("0"):
                    final_price = Decimal("0.00")
                # Добавим описание скидки промокода
                promo_line = f"Промокод {promo_code}: "
                if promo_percent:
                    promo_line += f"скидка {Decimal(str(promo_percent)):.0f}%\n"
                elif promo_amount:
                    promo_line += f"скидка {Decimal(str(promo_amount)):.2f} ₽\n"
                else:
                    promo_line += "применён\n"
                message_text = (
                    f"{promo_line}"
                    f"<b>Старая цена:</b> <s>{price:.2f} ₽</s>\n"
                    f"<b>Новая цена:</b> {final_price:.2f} ₽\n\n"
                ) + message_text
            except Exception:
                pass

        await state.update_data(final_price=float(final_price))

        # Получаем основной баланс для показа кнопки оплаты с баланса
        try:
            main_balance = get_balance(chat_id)
        except Exception:
            main_balance = 0.0

        show_balance_btn = main_balance >= float(final_price)

        if note:
            message_text = f"{note}\n\n" + message_text

        edited = False
        if message_id:
            try:
                await bot.edit_message_text(
                    message_text,
                    chat_id=chat_id,
                    message_id=message_id,
                    reply_markup=keyboards.create_payment_method_keyboard(
                        payment_methods=PAYMENT_METHODS,
                        action=data.get("action"),
                        key_id=data.get("key_id"),
                        show_balance=show_balance_btn,
                        main_balance=main_balance,
                        price=float(final_price),
                        has_promo_applied=bool(promo_code),
                    ),
                )
                edited = True
            except TelegramBadRequest as e:
                if "message is not modified" in str(e).lower():
                    edited = True
        if not edited:
            # Сообщение-якорь недоступно для редактирования (например,
            # пользователь его удалил, либо якоря ещё не было) —
            # отправляем новое и запоминаем его как новый якорь для
            # дальнейших правок в этом флоу
            sent = await bot.send_message(
                chat_id=chat_id,
                text=message_text,
                reply_markup=keyboards.create_payment_method_keyboard(
                    payment_methods=PAYMENT_METHODS,
                    action=data.get("action"),
                    key_id=data.get("key_id"),
                    show_balance=show_balance_btn,
                    main_balance=main_balance,
                    price=float(final_price),
                    has_promo_applied=bool(promo_code),
                ),
            )
            await state.update_data(flow_chat_id=sent.chat.id, flow_msg_id=sent.message_id)
        await state.set_state(PaymentProcess.waiting_for_payment_method)

    @user_router.callback_query(
        PaymentProcess.waiting_for_payment_method, F.data == "back_to_email_prompt"
    )
    async def back_to_email_prompt_handler(callback: types.CallbackQuery, state: FSMContext):
        await safe_edit_text(callback, 
            "📧 <b>Пожалуйста, введите ваш email для отправки чека об оплате:</b>\n\n"
            "Если вы не хотите указывать почту, нажмите кнопку ниже",
            reply_markup=keyboards.create_skip_email_keyboard(),
            parse_mode="HTML",
        )
        await state.set_state(PaymentProcess.waiting_for_email)

    # --- Промокод: запрос ввода ---
    @user_router.callback_query(
        PaymentProcess.waiting_for_payment_method, F.data == "enter_promo_code"
    )
    async def prompt_enter_promo(callback: types.CallbackQuery, state: FSMContext):
        await callback.answer()
        await state.set_state(PaymentProcess.waiting_for_promo_code)
        await safe_edit_text(callback, 
            "🎟️ <b>Введите промокод текстом:</b>",
            reply_markup=keyboards.create_back_to_main_menu_keyboard(),
            parse_mode="HTML",
        )

    # --- Промокод: обработка ввода ---
    @user_router.message(PaymentProcess.waiting_for_promo_code)
    async def handle_promo_input(message: types.Message, state: FSMContext):
        code = (message.text or "").strip()
        data = await state.get_data()
        chat_id = data.get("flow_chat_id", message.chat.id)
        msg_id = data.get("flow_msg_id")
        try:
            await message.delete()
        except TelegramBadRequest:
            pass
        if not code:
            await show_payment_options(
                message.bot,
                chat_id,
                msg_id,
                state,
                note="❌ Пустой промокод. Введите код ещё раз",
            )
            await state.set_state(PaymentProcess.waiting_for_promo_code)
            return
        promo, reason = check_promo_code_available(code, message.from_user.id)
        if not promo:
            reasons = {
                "not_found": "❌ Промокод не найден",
                "inactive": "❌ Промокод деактивирован",
                "not_started": "❌ Промокод ещё не начал действовать",
                "expired": "❌ Срок действия промокода истёк",
                "total_limit_reached": "❌ Достигнут общий лимит использования промокода",
                "user_limit_reached": "❌ Вы исчерпали лимит использования промокода",
                "db_error": "❌ Ошибка базы данных. Попробуйте позже",
                "empty_code": "❌ Пустой промокод",
            }
            # Вернёмся к выбору оплаты с пояснением, что пошло не так
            await show_payment_options(
                message.bot,
                chat_id,
                msg_id,
                state,
                note=reasons.get(reason or "not_found", "❌ Промокод недоступен"),
            )
            return
        # Сохраняем в состоянии применённый промокод
        await state.update_data(
            promo_code=promo.get("code"),
            promo_discount_percent=promo.get("discount_percent"),
            promo_discount_amount=promo.get("discount_amount"),
        )
        await show_payment_options(message.bot, chat_id, msg_id, state, note="✅ Промокод применён")
        await state.set_state(PaymentProcess.waiting_for_payment_method)

    # --- Промокод: удалить
    @user_router.callback_query(
        PaymentProcess.waiting_for_payment_method, F.data == "remove_promo_code"
    )
    async def remove_promo(callback: types.CallbackQuery, state: FSMContext):
        await callback.answer()
        data = await state.get_data()
        # Очистим поля промокода
        data.pop("promo_code", None)
        data.pop("promo_discount_percent", None)
        data.pop("promo_discount_amount", None)
        await state.set_data(data)
        await show_payment_options(
            callback.bot,
            data.get("flow_chat_id", callback.message.chat.id),
            data.get("flow_msg_id", callback.message.message_id),
            state,
            note="✅ Промокод удалён",
        )

    @user_router.callback_query(PaymentProcess.waiting_for_payment_method, F.data == "pay_yookassa")
    async def create_yookassa_payment_handler(callback: types.CallbackQuery, state: FSMContext):
        await callback.answer("⏳ Создаю ссылку на оплату...", show_alert=False)

        data = await state.get_data()
        user_data = get_user(callback.from_user.id)

        plan_id = data.get("plan_id")
        plan = get_plan_by_id(plan_id)

        if not plan:
            await callback.message.answer("❌ Произошла ошибка при выборе тарифа")
            await state.clear()
            return

        base_price = Decimal(str(plan["price"]))
        price_rub = base_price

        if user_data.get("referred_by") and user_data.get("total_spent", 0) == 0:
            discount_percentage_str = get_setting("referral_discount") or "0"
            discount_percentage = Decimal(discount_percentage_str)
            if discount_percentage > 0:
                discount_amount = (base_price * discount_percentage / 100).quantize(Decimal("0.01"))
                price_rub = base_price - discount_amount

        final_price_decimal = price_rub
        try:
            final_price_from_state = data.get("final_price")
            if final_price_from_state is not None:
                final_price_decimal = Decimal(str(final_price_from_state)).quantize(Decimal("0.01"))
        except Exception:
            pass

        if final_price_decimal < Decimal("0"):
            final_price_decimal = Decimal("0.00")

        plan_id = data.get("plan_id")
        customer_email = data.get("customer_email")
        host_name = data.get("host_name")
        action = data.get("action")
        key_id = data.get("key_id")

        if not customer_email:
            customer_email = get_setting("receipt_email")

        plan = get_plan_by_id(plan_id)
        if not plan:
            await callback.message.answer("❌ Произошла ошибка при выборе тарифа")
            await state.clear()
            return

        months = plan["months"]
        user_id = callback.from_user.id

        try:
            price_str_for_api = f"{final_price_decimal:.2f}"
            price_float_for_metadata = float(final_price_decimal)

            receipt = None
            if customer_email and is_valid_email(customer_email):
                receipt = {
                    "customer": {"email": customer_email},
                    "items": [
                        {
                            "description": f"Подписка на {months} мес.",
                            "quantity": "1.00",
                            "amount": {"value": price_str_for_api, "currency": "RUB"},
                            "vat_code": 1,
                            "payment_subject": "service",
                            "payment_mode": "full_payment",
                        }
                    ],
                }
            payment_payload = {
                "amount": {"value": price_str_for_api, "currency": "RUB"},
                "confirmation": {
                    "type": "redirect",
                    "return_url": f"https://t.me/{TELEGRAM_BOT_USERNAME}",
                },
                "capture": True,
                "description": f"Подписка на {months} мес.",
                "metadata": {
                    "user_id": str(user_id),
                    "months": str(months),
                    "price": f"{price_float_for_metadata:.2f}",
                    "action": str(action) if action is not None else "",
                    "key_id": (str(key_id) if key_id is not None else ""),
                    "host_name": str(host_name) if host_name is not None else "",
                    "plan_id": (str(plan_id) if plan_id is not None else ""),
                    "customer_email": customer_email or "",
                    "payment_method": "YooKassa",
                    "promo_code": (data.get("promo_code") or ""),
                    "promo_discount_percent": (
                        str(data.get("promo_discount_percent"))
                        if data.get("promo_discount_percent") is not None
                        else ""
                    ),
                    "promo_discount_amount": (
                        str(data.get("promo_discount_amount"))
                        if data.get("promo_discount_amount") is not None
                        else ""
                    ),
                },
            }
            if receipt:
                payment_payload["receipt"] = receipt

            payment = await asyncio.to_thread(Payment.create, payment_payload, uuid.uuid4())

            await state.clear()

            await safe_edit_text(callback, 
                "⬇️ <b>Нажмите на кнопку ниже для оплаты:</b>",
                reply_markup=keyboards.create_payment_keyboard(
                    payment.confirmation.confirmation_url
                ),
                parse_mode="HTML",
            )
        except Exception as e:
            logger.error(f"Не удалось создать платёж YooKassa: {e}", exc_info=True)
            await callback.message.answer("❌ Не удалось создать ссылку на оплату")
            await state.clear()

    @user_router.callback_query(PaymentProcess.waiting_for_payment_method, F.data == "pay_yoomoney")
    async def create_yoomoney_payment_handler(callback: types.CallbackQuery, state: FSMContext):
        await callback.answer("⏳ Готовлю ссылку ЮMoney...", show_alert=False)
        data = await state.get_data()
        user_data = get_user(callback.from_user.id)
        plan = get_plan_by_id(data.get("plan_id"))
        if not plan:
            await delete_and_send(callback.message, "❌ Произошла ошибка при выборе тарифа")
            await state.clear()
            return
        # Цена со скидкой по рефералке (как у других методов)
        base_price = Decimal(str(plan["price"]))
        price_rub = base_price
        if user_data and user_data.get("referred_by") and user_data.get("total_spent", 0) == 0:
            try:
                discount_percentage = Decimal(get_setting("referral_discount") or "0")
            except Exception:
                discount_percentage = Decimal("0")
            if discount_percentage > 0:
                price_rub = base_price - (base_price * discount_percentage / 100).quantize(
                    Decimal("0.01")
                )

        # Учитываем промокод (final_price хранится в состоянии как float)
        final_price_decimal = price_rub
        try:
            final_price_from_state = data.get("final_price")
            if final_price_from_state is not None:
                final_price_decimal = Decimal(str(final_price_from_state)).quantize(Decimal("0.01"))
        except Exception:
            pass

        if final_price_decimal < Decimal("0"):
            final_price_decimal = Decimal("0.00")

        final_price_float = float(final_price_decimal)

        ym_wallet = (get_setting("yoomoney_wallet") or "").strip()
        if not ym_wallet:
            await delete_and_send(callback.message, "❌ Оплата через ЮMoney временно недоступна")
            await state.clear()
            return

        months = int(plan["months"])
        user_id = callback.from_user.id
        payment_id = str(uuid.uuid4())
        metadata = {
            "payment_id": payment_id,
            "user_id": user_id,
            "months": months,
            "price": final_price_float,
            "action": data.get("action"),
            "key_id": data.get("key_id"),
            "host_name": data.get("host_name"),
            "plan_id": data.get("plan_id"),
            "customer_email": data.get("customer_email"),
            "payment_method": "YooMoney",
            "promo_code": data.get("promo_code"),
            "promo_discount_percent": data.get("promo_discount_percent"),
            "promo_discount_amount": data.get("promo_discount_amount"),
        }
        # Сохраняем pending транзакцию в БД
        try:
            create_pending_transaction(payment_id, user_id, final_price_float, metadata)
        except Exception as e:
            logger.warning(f"YooMoney: не удалось создать ожидающую транзакцию: {e}")

        # Формируем ссылку QuickPay
        try:
            success_url = f"https://t.me/{TELEGRAM_BOT_USERNAME}" if TELEGRAM_BOT_USERNAME else None
        except Exception:
            success_url = None
        targets = f"Оплата {months} мес."
        pay_url = _build_yoomoney_quickpay_url(
            wallet=ym_wallet,
            amount=final_price_float,
            label=payment_id,
            success_url=success_url,
            targets=targets,
        )

        await state.clear()
        try:
            await safe_edit_text(callback, 
                "⬇️ <b>Нажмите на кнопку ниже для оплаты. После оплаты нажмите 'Проверить оплату':</b>",
                reply_markup=keyboards.create_payment_with_check_keyboard(
                    pay_url, f"check_yoomoney_{payment_id}"
                ),
                parse_mode="HTML",
            )
        except TelegramBadRequest:
            await callback.message.answer(
                "⬇️ <b>Нажмите на кнопку ниже для оплаты. После оплаты нажмите 'Проверить оплату':</b>",
                reply_markup=keyboards.create_payment_with_check_keyboard(
                    pay_url, f"check_yoomoney_{payment_id}"
                ),
                parse_mode="HTML",
            )

    @user_router.callback_query(PaymentProcess.waiting_for_payment_method, F.data == "pay_stars")
    async def create_stars_invoice_handler(
        callback: types.CallbackQuery, state: FSMContext, bot: Bot
    ):
        await callback.answer("⏳ Готовлю счёт в Stars...", show_alert=False)
        data = await state.get_data()
        user_data = get_user(callback.from_user.id)
        plan = get_plan_by_id(data.get("plan_id"))
        if not plan:
            await delete_and_send(callback.message, "❌ Произошла ошибка при выборе тарифа")
            await state.clear()
            return
        base_price = Decimal(str(plan["price"]))
        price_rub = base_price
        if user_data and user_data.get("referred_by") and user_data.get("total_spent", 0) == 0:
            try:
                discount_percentage = Decimal(get_setting("referral_discount") or "0")
            except Exception:
                discount_percentage = Decimal("0")
            if discount_percentage > 0:
                price_rub = base_price - (base_price * discount_percentage / 100).quantize(
                    Decimal("0.01")
                )
        months = int(plan["months"])
        price_decimal = Decimal(str(price_rub)).quantize(Decimal("0.01"))
        stars_count = _calc_stars_amount(price_decimal)
        # Для Stars ограничим payload до UUID, метаданные сохраним в pending‑транзакцию
        payment_id = str(uuid.uuid4())
        metadata = {
            "user_id": callback.from_user.id,
            "months": months,
            "price": float(price_decimal),
            "action": data.get("action"),
            "key_id": data.get("key_id"),
            "host_name": data.get("host_name"),
            "plan_id": data.get("plan_id"),
            "customer_email": data.get("customer_email"),
            "payment_method": "Stars",
            "promo_code": data.get("promo_code"),
            "promo_discount_percent": data.get("promo_discount_percent"),
            "promo_discount_amount": data.get("promo_discount_amount"),
        }
        try:
            create_pending_transaction(
                payment_id, callback.from_user.id, float(price_decimal), metadata
            )
        except Exception as e:
            logger.warning(f"Покупка Stars: не удалось создать ожидающую транзакцию: {e}")
        payload = payment_id

        title = get_setting("stars_title") or "Покупка подписки"
        description = get_setting("stars_description") or f"Оплата {months} мес"
        try:
            await bot.send_invoice(
                chat_id=callback.message.chat.id,
                title=title,
                description=description,
                payload=payload,
                currency="XTR",
                prices=[types.LabeledPrice(label=f"{months} мес.", amount=stars_count)],
            )
            await state.clear()
        except Exception as e:
            logger.error(f"Не удалось отправить счёт Stars: {e}")
            await delete_and_send(callback.message, 
                "❌ Не удалось создать счёт Stars. Попробуйте другой способ оплаты"
            )
            await state.clear()

    @user_router.callback_query(
        PaymentProcess.waiting_for_payment_method, F.data == "pay_cryptobot"
    )
    async def create_cryptobot_invoice_handler(callback: types.CallbackQuery, state: FSMContext):
        await callback.answer("⏳ Создаю счет в Crypto Pay...", show_alert=False)

        data = await state.get_data()
        user_data = get_user(callback.from_user.id)

        plan_id = data.get("plan_id")
        user_id = data.get("user_id", callback.from_user.id)

        cryptobot_token = get_setting("cryptobot_token")
        if not cryptobot_token:
            logger.error(
                f"Попытка создать счёт Crypto Pay не удалась для пользователя {user_id}: cryptobot_token не задан"
            )
            await delete_and_send(callback.message, 
                "❌ Оплата криптовалютой временно недоступна. (Администратор не указал токен)"
            )
            await state.clear()
            return

        plan = get_plan_by_id(plan_id)
        if not plan:
            logger.error(
                f"Попытка создать счёт Crypto Pay не удалась для пользователя {user_id}: тариф с id {plan_id} не найден"
            )
            await delete_and_send(callback.message, "❌ Произошла ошибка при выборе тарифа")
            await state.clear()
            return

        base_price = Decimal(str(plan["price"]))
        price_rub_decimal = base_price
        if user_data.get("referred_by") and user_data.get("total_spent", 0) == 0:
            discount_percentage_str = get_setting("referral_discount") or "0"
            discount_percentage = Decimal(discount_percentage_str)
            if discount_percentage > 0:
                discount_amount = (base_price * discount_percentage / 100).quantize(Decimal("0.01"))
                price_rub_decimal = base_price - discount_amount

        final_price_float = float(price_rub_decimal)

        pay_url = await _create_cryptobot_invoice(
            user_id=callback.from_user.id,
            price_rub=final_price_float,
            months=plan["months"],
            host_name=data.get("host_name"),
            state_data=data,
        )

        if pay_url:
            await safe_edit_text(callback, 
                "⬇️ <b>Нажмите на кнопку ниже для оплаты:</b>\n\n"
                "⏳ После оплаты подтверждение приходит автоматически — обычно "
                "это занимает 1-3 минуты. Как только оно придёт, бот сам пришлёт "
                "сообщение здесь же",
                reply_markup=keyboards.create_payment_keyboard(pay_url),
                parse_mode="HTML",
            )
            await state.clear()
        else:
            await delete_and_send(callback.message, 
                "❌ Не удалось создать счет CryptoBot. Попробуйте другой способ оплаты"
            )

    @user_router.callback_query(
        PaymentProcess.waiting_for_payment_method, F.data == "pay_tonconnect"
    )
    async def create_ton_invoice_handler(callback: types.CallbackQuery, state: FSMContext):
        logger.info(f"Пользователь {callback.from_user.id}: вошёл в create_ton_invoice_handler")
        data = await state.get_data()
        user_id = callback.from_user.id
        wallet_address = get_setting("ton_wallet_address")
        plan = get_plan_by_id(data.get("plan_id"))

        if not wallet_address or not plan:
            await delete_and_send(callback.message, "❌ Оплата через TON временно недоступна")
            await state.clear()
            return

        await callback.answer("⏳ Создаю ссылку и QR-код для TON Connect...", show_alert=False)

        price_rub = Decimal(str(data.get("final_price", plan["price"])))

        usdt_rub_rate = await get_usdt_rub_rate()
        ton_usdt_rate = await get_ton_usdt_rate()

        if not usdt_rub_rate or not ton_usdt_rate:
            await delete_and_send(callback.message, "❌ Не удалось получить курс TON. Попробуйте позже")
            await state.clear()
            return

        price_ton = (price_rub / usdt_rub_rate / ton_usdt_rate).quantize(
            Decimal("0.001"), rounding=ROUND_HALF_UP
        )
        amount_nanoton = int(price_ton * 1_000_000_000)

        payment_id = str(uuid.uuid4())
        metadata = {
            "user_id": user_id,
            "months": plan["months"],
            "price": float(price_rub),
            "action": data.get("action"),
            "key_id": data.get("key_id"),
            "host_name": data.get("host_name"),
            "plan_id": data.get("plan_id"),
            "customer_email": data.get("customer_email"),
            "payment_method": "TON Connect",
            "promo_code": data.get("promo_code"),
            "promo_discount_percent": data.get("promo_discount_percent"),
            "promo_discount_amount": data.get("promo_discount_amount"),
            # См. комментарий в top_up-ветке того же флоу — без этого
            # find_and_complete_ton_transaction не может проверить сумму
            "expected_amount_ton": float(price_ton),
        }
        create_pending_transaction(payment_id, user_id, float(price_rub), metadata)

        transaction_payload = {
            "messages": [
                {
                    "address": wallet_address,
                    "amount": str(amount_nanoton),
                    "payload": payment_id,
                }
            ],
            "valid_until": int(datetime.now().timestamp()) + 600,
        }

        try:
            connect_url = await _start_ton_connect_process(user_id, transaction_payload)

            qr_img = qrcode.make(connect_url)
            bio = BytesIO()
            qr_img.save(bio, "PNG")
            qr_file = BufferedInputFile(bio.getvalue(), "ton_qr.png")

            # Удаляем предыдущее сообщение безопасно (если нельзя удалить, просто пропустим)
            try:
                await callback.message.delete()
            except Exception:
                pass
            await callback.message.answer_photo(
                photo=qr_file,
                caption=(
                    f"💎 <b>Оплата через TON Connect</b>\n\n"
                    f"<b>Сумма к оплате:</b> {price_ton} TON\n\n"
                    f"✅ <b>Способ 1 (на телефоне):</b> Нажмите кнопку 'Открыть кошелек' ниже.\n"
                    f"✅ <b>Способ 2 (на компьютере):</b> Отсканируйте QR-код кошельком.\n\n"
                    f"После подключения кошелька подтвердите транзакцию.\n\n"
                    f"⏳ После оплаты подтверждение приходит автоматически — обычно "
                    f"это занимает 1-3 минуты. Как только оно придёт, бот сам пришлёт "
                    f"сообщение здесь же"
                ),
                parse_mode="HTML",
                reply_markup=keyboards.create_ton_connect_keyboard(connect_url),
            )
            await state.clear()

        except Exception as e:
            logger.error(
                f"Не удалось сгенерировать ссылку TON Connect для пользователя {user_id}: {e}",
                exc_info=True,
            )
            await callback.message.answer(
                "❌ Не удалось создать ссылку для TON Connect. Попробуйте позже"
            )
            await state.clear()

    @user_router.callback_query(PaymentProcess.waiting_for_payment_method, F.data == "pay_balance")
    async def pay_with_main_balance_handler(
        callback: types.CallbackQuery, state: FSMContext, bot: Bot
    ):
        await callback.answer()
        data = await state.get_data()
        user_id = callback.from_user.id
        plan = get_plan_by_id(data.get("plan_id"))
        if not plan:
            await delete_and_send(callback.message, "❌ Тариф не найден")
            await state.clear()
            return
        months = int(plan["months"])
        price = float(data.get("final_price", plan["price"]))

        # Пытаемся списать средства с основного баланса
        if not deduct_from_balance(user_id, price):
            await callback.answer("❌ Недостаточно средств на основном балансе", show_alert=False)
            return

        metadata = {
            "user_id": user_id,
            "months": months,
            "price": price,
            "action": data.get("action"),
            "key_id": data.get("key_id"),
            "host_name": data.get("host_name"),
            "plan_id": data.get("plan_id"),
            "customer_email": data.get("customer_email"),
            "payment_method": "Balance",
            "promo_code": data.get("promo_code"),
            "promo_discount_percent": data.get("promo_discount_percent"),
            "promo_discount_amount": data.get("promo_discount_amount"),
            "chat_id": callback.message.chat.id,
            "message_id": callback.message.message_id,
        }

        await state.clear()
        await process_successful_payment(bot, metadata)

    # Telegram Payments: подтверждаем pre_checkout
    @user_router.pre_checkout_query()
    async def pre_checkout_handler(pre_checkout_query: types.PreCheckoutQuery, bot: Bot):
        try:
            await bot.answer_pre_checkout_query(pre_checkout_query.id, ok=True)
        except Exception as e:
            logger.warning(f"Ошибка в pre_checkout_handler: {e}")

    # Сообщение об успешной оплате (в т.ч. Stars)
    @user_router.message(F.successful_payment)
    async def successful_payment_handler(message: types.Message, bot: Bot):
        try:
            sp = message.successful_payment
            payload = sp.invoice_payload or ""
            metadata = {}
            # 1) Пытаемся трактовать payload как JSON (на случай старых инвойсов)
            if payload:
                try:
                    parsed = json.loads(payload)
                    if isinstance(parsed, dict):
                        metadata = parsed
                except Exception:
                    metadata = {}
            # 2) Если JSON не получился — считаем, что payload это payment_id для pending‑транзакции
            if not metadata and payload:
                try:
                    currency = getattr(sp, "currency", None)
                    total_amount = getattr(sp, "total_amount", None)
                    payment_method = "Stars" if str(currency).upper() == "XTR" else "Card"
                    md = find_and_complete_pending_transaction(
                        payment_id=payload,
                        amount_rub=None,  # Оставляем исходную сумму из pending
                        payment_method=payment_method,
                        currency_name=currency,
                        amount_currency=(float(total_amount) if total_amount is not None else None),
                    )
                    if md:
                        metadata = md
                except Exception as e:
                    logger.error(
                        f"Не удалось найти ожидающую транзакцию по payload '{payload}': {e}"
                    )
        except Exception as e:
            logger.error(f"Не удалось разобрать payload успешного платежа: {e}")
            metadata = {}
        if not metadata:
            try:
                await message.answer(
                    "✅ Оплата получена, но нет данных заказа. Обратитесь в поддержку, если подписка не выдана"
                )
            except Exception:
                pass
            return
        await process_successful_payment(bot, metadata)

    return user_router


async def _create_heleket_payment_request(
    user_id: int,
    price: float,
    months: int,
    host_name: str,
    state_data: dict,
) -> Optional[str]:
    """Создать счёт через Heleket и вернуть ссылку на оплату.

    Формирует payload с подписью по той же схеме, которой пользуется вебхук:
    sign = md5( base64( json.dumps(data_sorted) ) + api_key ).

    Возвращает URL на оплату или None при ошибке.
    """
    try:
        merchant_id = get_setting("heleket_merchant_id")
        api_key = get_setting("heleket_api_key")
        if not merchant_id or not api_key:
            logger.error("Heleket: отсутствуют merchant_id/api_key в настройках")
            return None

        # Метаданные, которые затем будут разобраны в webhook (`description` JSON)
        metadata = {
            "payment_id": str(uuid.uuid4()),
            "user_id": user_id,
            "months": months,
            "price": float(price),
            "action": state_data.get("action"),
            "key_id": state_data.get("key_id"),
            "host_name": host_name,
            "plan_id": state_data.get("plan_id"),
            "customer_email": state_data.get("customer_email"),
            "payment_method": "Crypto",
            "promo_code": state_data.get("promo_code"),
            "promo_discount_percent": state_data.get("promo_discount_percent"),
            "promo_discount_amount": state_data.get("promo_discount_amount"),
        }

        # Базовые поля счёта для Heleket
        dom_val = get_setting("domain")
        domain = (dom_val or "").strip() if isinstance(dom_val, str) else dom_val
        callback_url = None
        try:
            if domain:
                callback_url = f"{str(domain).rstrip('/')}/heleket-webhook"
        except Exception:
            callback_url = None

        # Укажем success_url как возврат в бота
        success_url = None
        try:
            if TELEGRAM_BOT_USERNAME:
                success_url = f"https://t.me/{TELEGRAM_BOT_USERNAME}"
        except Exception:
            success_url = None

        data: Dict[str, object] = {
            "merchant_id": merchant_id,
            "order_id": str(uuid.uuid4()),
            "amount": float(price),
            "currency": "RUB",
            "description": json.dumps(metadata, ensure_ascii=False, separators=(",", ":")),
        }
        if callback_url:
            data["callback_url"] = callback_url
        if success_url:
            data["success_url"] = success_url

        # Формируем подпись в соответствии с обработчиком вебхука
        sorted_data_str = json.dumps(data, sort_keys=True, separators=(",", ":"))
        base64_encoded = base64.b64encode(sorted_data_str.encode()).decode()
        raw_string = f"{base64_encoded}{api_key}"
        sign = hashlib.md5(raw_string.encode()).hexdigest()

        payload = dict(data)
        payload["sign"] = sign

        # Базовый URL API Heleket. Делаем настраиваемым через (необязательную) настройку heleket_api_base.
        api_base_val = get_setting("heleket_api_base")
        api_base = (api_base_val or "https://api.heleket.com").rstrip("/")
        endpoint = f"{api_base}/invoice/create"

        async with aiohttp.ClientSession() as session:
            try:
                async with session.post(endpoint, json=payload, timeout=30) as resp:
                    text = await resp.text()
                    if resp.status not in (200, 201):
                        logger.error(
                            f"Heleket: не удалось создать счёт (HTTP {resp.status}): {text}"
                        )
                        return None
                    try:
                        data_json = await resp.json()
                    except Exception:
                        # Если провайдер вернул не JSON
                        logger.warning(f"Heleket: неожиданный ответ (не JSON): {text}")
                        return None
                    pay_url = (
                        data_json.get("payment_url")
                        or data_json.get("pay_url")
                        or data_json.get("url")
                    )
                    if not pay_url:
                        logger.error(f"Heleket: не найдено поле URL в ответе: {data_json}")
                        return None
                    return str(pay_url)
            except Exception as e:
                logger.error(f"Heleket: ошибка HTTP при создании счёта: {e}", exc_info=True)
                return None
    except Exception as e:
        logger.error(f"Heleket: общая ошибка при создании счёта: {e}", exc_info=True)
        return None


async def _create_cryptobot_invoice(
    user_id: int,
    price_rub: float,
    months: int,
    host_name: str,
    state_data: dict,
) -> Optional[str]:
    """Создать счёт в Telegram Crypto Pay и вернуть ссылку на оплату.

    - Конвертирует RUB в USDT по рыночному курсу.
    - Формирует payload в формате, ожидаемом обработчиком вебхука `/cryptobot-webhook`:
      `user_id:months:price:action:key_id:host_name:plan_id:customer_email:payment_method`.
    """
    try:
        token = get_setting("cryptobot_token")
        if not token:
            logger.error("CryptoBot: не задан cryptobot_token")
            return None

        rate = await get_usdt_rub_rate()
        if not rate or rate <= 0:
            logger.error("CryptoBot: не удалось получить курс USDT/RUB")
            return None

        amount_usdt = (Decimal(str(price_rub)) / rate).quantize(
            Decimal("0.01"), rounding=ROUND_HALF_UP
        )

        # Собираем payload для вебхука
        payload_parts = [
            str(user_id),
            str(months),
            str(float(price_rub)),
            str(state_data.get("action")),
            str(state_data.get("key_id")),
            str(host_name or ""),
            str(state_data.get("plan_id")),
            str(state_data.get("customer_email")),
            "CryptoBot",
            str(state_data.get("promo_code") or ""),
        ]
        payload = ":".join(payload_parts)

        cp = CryptoPay(token)
        # Пытаемся создать инвойс в USDT; описание — краткое
        invoice = await cp.create_invoice(
            asset="USDT",
            amount=float(amount_usdt),
            description="Подписка на Snowy Services",
            payload=payload,
        )

        pay_url = None
        try:
            # У разных обёрток могут отличаться имена полей
            pay_url = getattr(invoice, "pay_url", None) or getattr(invoice, "bot_invoice_url", None)
        except Exception:
            pass
        if not pay_url and isinstance(invoice, dict):
            pay_url = invoice.get("pay_url") or invoice.get("bot_invoice_url") or invoice.get("url")
        if not pay_url:
            logger.error(f"CryptoBot: не удалось получить ссылку на оплату из ответа: {invoice}")
            return None
        return str(pay_url)
    except Exception as e:
        logger.error(f"CryptoBot: ошибка при создании счёта: {e}", exc_info=True)
        return None


async def get_usdt_rub_rate() -> Optional[Decimal]:
    """Получить курс USDT→RUB. Возвращает Decimal или None при ошибке."""
    try:
        url = "https://api.coingecko.com/api/v3/simple/price?ids=tether&vs_currencies=rub"
        async with aiohttp.ClientSession() as session:
            async with session.get(url, timeout=30) as resp:
                if resp.status != 200:
                    logger.warning(f"USDT/RUB: HTTP {resp.status}")
                    return None
                data = await resp.json()
                val = data.get("tether", {}).get("rub")
                if val is None:
                    return None
                return Decimal(str(val))
    except Exception as e:
        logger.warning(f"USDT/RUB: ошибка получения курса: {e}")
        return None


async def get_ton_usdt_rate() -> Optional[Decimal]:
    """Получить курс TON→USDT (через USD). Возвращает Decimal или None при ошибке."""
    try:
        url = "https://api.coingecko.com/api/v3/simple/price?ids=toncoin&vs_currencies=usd"
        async with aiohttp.ClientSession() as session:
            async with session.get(url, timeout=30) as resp:
                if resp.status != 200:
                    logger.warning(f"TON/USD: HTTP {resp.status}")
                    return None
                data = await resp.json()
                usd = data.get("toncoin", {}).get("usd")
                if usd is None:
                    return None
                return Decimal(str(usd))
    except Exception as e:
        logger.warning(f"TON/USD: ошибка получения курса: {e}")
        return None


async def _start_ton_connect_process(user_id: int, transaction_payload: Dict) -> str:
    """Упростённый генератор deep‑link для TON перевода.

    Вместо полноценного протокола TON Connect формируем ссылку вида:
    ton://transfer/<address>?amount=<nanoton>&text=<payload>
    Поддерживается большинством TON-кошельков и удобна для QR.
    """
    try:
        messages = transaction_payload.get("messages") or []
        if not messages:
            raise ValueError("transaction_payload.messages is empty")
        msg = messages[0]
        address = msg.get("address")
        amount = msg.get("amount")  # В нанотонах как строка
        payload_text = msg.get("payload") or ""
        if not address or not amount:
            raise ValueError("address/amount are required in transaction message")
        # Сформируем ton://transfer ...
        params = {"amount": amount}
        if payload_text:
            params["text"] = str(payload_text)
        query = urlencode(params)
        return f"ton://transfer/{address}?{query}"
    except Exception as e:
        logger.error(f"Не удалось сгенерировать deep link TON: {e}")
        # Фолбэк: без параметров
        return "ton://transfer"


def _build_yoomoney_quickpay_url(
    wallet: str,
    amount: float,
    label: str,
    success_url: Optional[str] = None,
    targets: Optional[str] = None,
) -> str:
    try:
        params = {
            "receiver": wallet,
            "quickpay-form": "shop",
            "sum": f"{float(amount):.2f}",
            "label": label,
        }
        if success_url:
            params["successURL"] = success_url
        if targets:
            params["targets"] = targets
        base = "https://yoomoney.ru/quickpay/confirm.xml"
        return f"{base}?{urlencode(params)}"
    except Exception:
        return "https://yoomoney.ru/"


async def _yoomoney_find_payment(label: str) -> Optional[dict]:
    token = (get_setting("yoomoney_api_token") or "").strip()
    if not token:
        logger.warning("YooMoney: API токен не задан в настройках")
        return None
    url = "https://yoomoney.ru/api/operation-history"
    headers = {
        "Authorization": f"Bearer {token}",
        "Content-Type": "application/x-www-form-urlencoded",
    }
    data = {
        "label": label,
        "records": "5",
    }
    try:
        async with aiohttp.ClientSession() as session:
            async with session.post(url, data=data, headers=headers, timeout=30) as resp:
                text = await resp.text()
                if resp.status != 200:
                    logger.warning(f"YooMoney: operation-history HTTP {resp.status}: {text}")
                    return None
                try:
                    payload = await resp.json()
                except Exception:
                    try:
                        payload = json.loads(text)
                    except Exception:
                        logger.warning("YooMoney: не удалось распарсить JSON operation-history")
                        return None
                ops = payload.get("operations") or []
                for op in ops:
                    if str(op.get("label")) == str(label) and str(op.get("direction")) == "in":
                        status = str(op.get("status") or "").lower()
                        if status == "success":
                            try:
                                amount = float(op.get("amount"))
                            except Exception:
                                amount = None
                            return {
                                "operation_id": op.get("operation_id"),
                                "amount": amount,
                                "datetime": op.get("datetime"),
                            }
                return None
    except Exception as e:
        logger.error(f"YooMoney: ошибка запроса operation-history: {e}", exc_info=True)
        return None


async def notify_admin_of_purchase(bot: Bot, metadata: dict):
    """Отправляет администратору уведомление об успешной покупке/продлении/пополнении."""
    try:
        admin_id_raw = get_setting("admin_telegram_id")
        if not admin_id_raw:
            return
        admin_id = int(admin_id_raw)
        user_id = metadata.get("user_id")
        host_name = metadata.get("host_name")
        months = metadata.get("months")
        price = metadata.get("price")
        action = metadata.get("action")
        payment_method = metadata.get("payment_method") or "Unknown"
        # Локализация методов оплаты для уведомления админу
        payment_method_map = {
            "Balance": "Баланс",
            "Card": "Карта",
            "Crypto": "Крипто",
            "USDT": "USDT",
            "TON": "TON",
        }
        payment_method_display = payment_method_map.get(payment_method, payment_method)
        plan_id = metadata.get("plan_id")
        plan = get_plan_by_id(plan_id)
        plan_name = plan.get("plan_name", "Unknown") if plan else "Unknown"

        text = (
            "📥 <b>Новая оплата</b>\n\n"
            f"👤 <b>Пользователь:</b> {user_id}\n"
            f"🗺️ <b>Сервер:</b> {host_name}\n"
            f"📦 <b>Тариф:</b> {plan_name} ({months} мес.)\n"
            f"💳 <b>Метод:</b> {payment_method_display}\n"
            f"💰 <b>Сумма:</b> {float(price):.2f} ₽\n"
            f"⚙️ <b>Действие:</b> {'Новая подписка' if action == 'new' else 'Продление'}"
        )
        await bot.send_message(admin_id, text)
    except Exception as e:
        logger.warning(f"Ошибка в notify_admin_of_purchase: {e}")


def _safe_optional_int(value, default: int = 0) -> int:
    """Безопасно приводит значение к int для необязательных полей metadata.

    Строка 'None' — артефакт сериализации некоторых вебхуков (например,
    CryptoBot собирает payload через str(x) и join(':'), из-за чего
    отсутствующее значение превращается в буквальную строку 'None', а не
    в настоящий None) — поэтому её тоже считаем отсутствием значения.
    """
    if value is None or value == "None" or value == "":
        return default
    try:
        return int(value)
    except (ValueError, TypeError):
        return default


async def process_successful_payment(bot: Bot, metadata: dict):
    """Обрабатывает успешный платёж: создаёт/продлевает ключ или пополняет баланс."""
    try:
        action = metadata.get("action")
        user_id = int(metadata.get("user_id"))
        price = float(metadata.get("price"))
        # Поля ниже нужны только для покупок ключей/продлений
        months = int(metadata.get("months", 0))
        key_id = _safe_optional_int(metadata.get("key_id"))
        host_name = metadata.get("host_name", "")
        plan_id = _safe_optional_int(metadata.get("plan_id"))
        payment_method = metadata.get("payment_method")

        chat_id_to_delete = metadata.get("chat_id")
        message_id_to_delete = metadata.get("message_id")

    except (ValueError, TypeError) as e:
        logger.error(f"КРИТИЧНО: не удалось разобрать metadata. Ошибка: {e}. Metadata: {metadata}")
        return

    if chat_id_to_delete and message_id_to_delete:
        try:
            await bot.delete_message(chat_id=chat_id_to_delete, message_id=message_id_to_delete)
        except TelegramBadRequest as e:
            logger.warning(f"Не удалось удалить сообщение о платеже: {e}")

    # Спец-ветка: пополнение баланса
    if action == "top_up":
        try:
            ok = add_to_balance(user_id, float(price))
        except Exception as e:
            logger.error(
                f"Не удалось пополнить баланс пользователя {user_id}: {e}",
                exc_info=True,
            )
            ok = False
        # Лог транзакции
        try:
            user_info = await asyncio.to_thread(get_user, user_id)
            log_username = user_info.get("username", "N/A") if user_info else "N/A"
            log_transaction(
                username=log_username,
                transaction_id=None,
                payment_id=str(uuid.uuid4()),
                user_id=user_id,
                status="paid",
                amount_rub=float(price),
                amount_currency=None,
                currency_name=None,
                payment_method=payment_method or "Unknown",
                metadata=json.dumps({"action": "top_up"}),
            )
        except Exception:
            pass
        try:
            current_balance = 0.0
            try:
                current_balance = float(await asyncio.to_thread(get_balance, user_id))
            except Exception:
                pass
            if ok:
                await bot.send_message(
                    chat_id=user_id,
                    text=(
                        f"✅ <b>Оплата получена!</b>\n\n"
                        f"💰 Баланс пополнен на {float(price):.2f} ₽.\n"
                        f"💼 <b>Текущий баланс:</b> {current_balance:.2f} ₽."
                    ),
                    reply_markup=keyboards.create_profile_keyboard(),
                    parse_mode="HTML",
                )
            else:
                await bot.send_message(
                    chat_id=user_id,
                    text=(
                        "⚠️ <b>Оплата получена, но не удалось обновить баланс</b>\n\n"
                        "Обратитесь в поддержку."
                    ),
                    reply_markup=keyboards.create_support_keyboard(),
                )
        except Exception:
            pass
        # Админ-уведомление о пополнении (по возможности)
        try:
            admins = [u for u in (get_all_users() or []) if is_admin(u.get("telegram_id") or 0)]
            for a in admins:
                admin_id = a.get("telegram_id")
                if admin_id:
                    await bot.send_message(
                        admin_id,
                        f"📥 Пополнение: пользователь {user_id}, сумма {float(price):.2f} ₽",
                    )
        except Exception:
            pass
        return

    processing_message = await bot.send_message(
        chat_id=user_id,
        text=f"✅ Оплата получена! Обрабатываю ваш запрос на сервере {host_name}...",
    )
    try:
        # Цена нужна ниже вне зависимости от ветки
        price = float(metadata.get("price"))
        result = None
        # Определяем email для операции и вызываем панель для обеих веток (new/extend)
        if action == "new":
            # Сформируем email в формате {username}@{xui_api.EMAIL_DOMAIN} с авто-суффиксом при коллизиях
            user_data = await asyncio.to_thread(get_user, user_id) or {}
            raw_username = (user_data.get("username") or f"user{user_id}").lower()
            username_slug = (
                re.sub(r"[^a-z0-9._-]", "_", raw_username).strip("_")[:16] or f"user{user_id}"
            )
            base_local = f"{username_slug}"
            candidate_local = base_local
            attempt = 1
            while True:
                candidate_email = f"{candidate_local}@{xui_api.EMAIL_DOMAIN}"
                if not get_key_by_email(candidate_email):
                    break
                attempt += 1
                candidate_local = f"{base_local}_{attempt}"
                if attempt > 100:
                    candidate_local = f"{base_local}_{int(datetime.now().timestamp())}"
                    candidate_email = f"{candidate_local}@{xui_api.EMAIL_DOMAIN}"
                    break
        else:
            # Продление существующего ключа — достаём email по key_id
            existing_key = await asyncio.to_thread(get_key_by_id, key_id)
            if not existing_key or not existing_key.get("key_email"):
                await delete_and_send(processing_message, "❌ Не удалось найти подписку для продления")
                return
            candidate_email = existing_key["key_email"]

            # Если продлеваемый ключ был выдан как триальный (email вида
            # trial_username@...), это значит пользователь сейчас оплачивает
            # то, что начиналось как бесплатный триал — снимаем префикс
            # trial_, чтобы платная подписка не выглядела как пробная нигде
            # (в БД, в панели, в списке ключей). UUID клиента при этом не
            # меняется — все уже выданные пользователю конфиги/ссылки
            # продолжают работать как ни в чём не бывало.
            local_part = candidate_email.split("@")[0]
            if local_part.lower().startswith("trial_"):
                domain = (
                    candidate_email.split("@")[1]
                    if "@" in candidate_email
                    else xui_api.EMAIL_DOMAIN
                )
                stripped_slug = local_part[len("trial_") :] or f"user{user_id}"
                new_local = stripped_slug
                attempt = 1
                while True:
                    new_candidate_email = f"{new_local}@{domain}"
                    if not get_key_by_email(new_candidate_email):
                        break
                    attempt += 1
                    new_local = f"{stripped_slug}_{attempt}"
                    if attempt > 100:
                        new_local = f"{stripped_slug}_{int(datetime.now().timestamp())}"
                        new_candidate_email = f"{new_local}@{domain}"
                        break

                renamed_ok = await xui_api.rename_client_email_on_host(
                    host_name=host_name,
                    old_email=candidate_email,
                    new_email=new_candidate_email,
                )
                if renamed_ok:
                    await asyncio.to_thread(update_key_email, key_id, new_candidate_email)
                    candidate_email = new_candidate_email
                    logger.info(
                        f"Триальный ключ {key_id} переименован в платный: {new_candidate_email}"
                    )
                else:
                    # Не смогли переименовать на панели (например, клиент не
                    # найден или email уже занят) — не блокируем оплату из-за
                    # этого, просто продлеваем под старым email как раньше
                    logger.warning(
                        f"Не удалось снять префикс trial_ с ключа {key_id} "
                        f"({candidate_email}) — продлеваю под старым email."
                    )

        result = await xui_api.create_or_update_key_on_host(
            host_name=host_name, email=candidate_email, months_to_add=int(months)
        )
        if not result:
            await delete_and_send(processing_message, "❌ Не удалось создать/обновить подписку на сервере")
            return

        if action == "new":
            key_id = add_new_key(
                user_id=user_id,
                host_name=host_name,
                xui_client_uuid=result["client_uuid"],
                key_email=result["email"],
                expiry_timestamp_ms=result["expiry_timestamp_ms"],
            )
        elif action == "extend":
            update_key_info(key_id, result["client_uuid"], result["expiry_timestamp_ms"])

        # Запоминаем тариф этой покупки/продления — используется при
        # автопродлении (см. scheduler.process_auto_renewals), чтобы знать,
        # на сколько месяцев и по какой цене продлевать в следующий раз,
        # "тот же тариф, что был в прошлый раз". Работает для любого
        # способа оплаты — эта функция общая для всех них.
        if plan_id is not None:
            try:
                set_key_last_plan(key_id, plan_id)
            except Exception as e:
                logger.warning(f"Не удалось сохранить last_plan_id для ключа {key_id}: {e}")

        # Начисляем реферальное вознаграждение по покупке — применяется для new и extend
        user_data = await asyncio.to_thread(get_user, user_id)
        referrer_id = user_data.get("referred_by") if user_data else None
        if referrer_id:
            try:
                referrer_id = int(referrer_id)
            except Exception:
                logger.warning(
                    f"Реферал: некорректный referrer_id={referrer_id} для пользователя {user_id}"
                )
                referrer_id = None
        if referrer_id:
            # Выбор логики по типу: процент, фикс за покупку; для fixed_start_referrer — вознаграждение по покупке не начисляем
            try:
                reward_type = (get_setting("referral_reward_type") or "percent_purchase").strip()
            except Exception:
                reward_type = "percent_purchase"
            reward = Decimal("0")
            if reward_type == "fixed_start_referrer":
                reward = Decimal("0")
            elif reward_type == "fixed_purchase":
                try:
                    amount_raw = get_setting("fixed_referral_bonus_amount") or "50"
                    reward = Decimal(str(amount_raw)).quantize(Decimal("0.01"))
                except Exception:
                    reward = Decimal("50.00")
            else:
                # percent_purchase (по умолчанию)
                try:
                    percentage = Decimal(get_setting("referral_percentage") or "0")
                except Exception:
                    percentage = Decimal("0")
                reward = (Decimal(str(price)) * percentage / 100).quantize(Decimal("0.01"))
            logger.info(
                f"Реферал: пользователь={user_id}, пригласивший={referrer_id}, тип={reward_type}, вознаграждение={float(reward):.2f}"
            )
            if float(reward) > 0:
                # Атомарно: и баланс, и накопительный referral_balance_all
                # одним запросом, чтобы не было рассинхронизации (см. credit_referral_reward).
                try:
                    ok = credit_referral_reward(referrer_id, float(reward))
                except Exception as e:
                    logger.warning(
                        f"Реферал: не удалось начислить вознаграждение пригласившему {referrer_id}: {e}"
                    )
                    ok = False
                referrer_username = (
                    user_data.get("username", "пользователь") if user_data else "пользователь"
                )
                if ok:
                    # Фиксируем начисление в истории операций пригласившего (иначе баланс
                    # растёт, а в "Истории операций" не видно, за что) по аналогии с
                    # логированием стартового реферального бонуса выше.
                    try:
                        referrer_info = await asyncio.to_thread(get_user, referrer_id)
                        log_username = (
                            referrer_info.get("username", "N/A") if referrer_info else "N/A"
                        )
                        log_transaction(
                            username=log_username,
                            transaction_id=None,
                            payment_id=str(uuid.uuid4()),
                            user_id=referrer_id,
                            status="internal",
                            amount_rub=float(reward),
                            amount_currency=None,
                            currency_name=None,
                            payment_method="Referral",
                            metadata=json.dumps(
                                {
                                    "action": "referral_reward",
                                    "referral_type": "purchase",
                                    "from_user_id": user_id,
                                }
                            ),
                        )
                    except Exception as e:
                        logger.warning(
                            f"Не удалось залогировать реферальное вознаграждение за покупку для {referrer_id}: {e}"
                        )
                    try:
                        await bot.send_message(
                            chat_id=referrer_id,
                            text=(
                                "💰 <b>Вам начислено реферальное вознаграждение!</b>\n\n"
                                f"<b>Пользователь:</b> {referrer_username} (ID: {user_id})\n"
                                f"<b>Сумма:</b> {float(reward):.2f} ₽"
                            ),
                            parse_mode="HTML",
                        )
                    except Exception as e:
                        logger.warning(
                            f"Не удалось отправить уведомление о реферальном вознаграждении пользователю {referrer_id}: {e}"
                        )

        # Не учитываем в "Потрачено всего" покупки, оплаченные с внутреннего баланса
        try:
            pm_lower = (payment_method or "").strip().lower()
        except Exception:
            pm_lower = ""
        spent_for_stats = 0.0 if pm_lower == "balance" else float(price)
        update_user_stats(user_id, spent_for_stats, months)

        user_info = await asyncio.to_thread(get_user, user_id)

        log_username = user_info.get("username", "N/A") if user_info else "N/A"
        log_status = "paid"
        log_amount_rub = float(price)
        log_method = metadata.get("payment_method", "Unknown")

        log_metadata = json.dumps(
            {
                "plan_id": metadata.get("plan_id"),
                "plan_name": (
                    get_plan_by_id(metadata.get("plan_id")).get("plan_name", "Unknown")
                    if get_plan_by_id(metadata.get("plan_id"))
                    else "Unknown"
                ),
                "host_name": metadata.get("host_name"),
                "customer_email": metadata.get("customer_email"),
            }
        )

        # Определяем payment_id для лога: берём из metadata, если есть (например, при отложенных транзакциях), иначе генерируем новый UUID
        payment_id_for_log = metadata.get("payment_id") or str(uuid.uuid4())

        log_transaction(
            username=log_username,
            transaction_id=None,
            payment_id=payment_id_for_log,
            user_id=user_id,
            status=log_status,
            amount_rub=log_amount_rub,
            amount_currency=None,
            currency_name=None,
            payment_method=log_method,
            metadata=log_metadata,
        )
        # Если был применён промокод, фиксируем использование и при необходимости отключаем по лимиту
        try:
            promo_code_used = (metadata.get("promo_code") or "").strip()
            if promo_code_used:
                try:
                    # Пытаемся оценить применённую скидку, если доступна фиксированная сумма
                    applied_amt = 0.0
                    try:
                        if metadata.get("promo_discount_amount") is not None:
                            applied_amt = float(metadata.get("promo_discount_amount") or 0.0)
                    except Exception:
                        applied_amt = 0.0
                    redeemed = redeem_promo_code(
                        promo_code_used,
                        user_id,
                        applied_amount=float(applied_amt or 0.0),
                        order_id=payment_id_for_log,
                    )
                    if redeemed:
                        # Определяем причины для автоматической деактивации
                        limit_total = redeemed.get("usage_limit_total")
                        per_user_limit = redeemed.get("usage_limit_per_user")
                        used_total_now = redeemed.get("used_total") or 0
                        user_usage_count = redeemed.get("user_usage_count")
                        should_deactivate = False
                        reason_lines: list[str] = []

                        if limit_total:
                            try:
                                if used_total_now >= int(limit_total):
                                    should_deactivate = True
                                    reason_lines.append("достигнут общий лимит использования")
                            except Exception:
                                pass

                        if per_user_limit:
                            try:
                                if (user_usage_count or 0) >= int(per_user_limit):
                                    should_deactivate = True
                                    reason_lines.append("исчерпан лимит на пользователя")
                            except Exception:
                                pass

                        # Если не достигнуты лимиты, всё равно выключаем по требованию (при наличии любого лимита)
                        if not should_deactivate and (limit_total or per_user_limit):
                            should_deactivate = True
                            if per_user_limit and not reason_lines:
                                reason_lines.append("лимит на пользователя выставлен (код погашён)")
                            elif limit_total and not reason_lines:
                                reason_lines.append(
                                    "лимит по количеству использований выставлен (код погашён)"
                                )

                        if should_deactivate:
                            try:
                                update_promo_code_status(promo_code_used, is_active=False)
                            except Exception:
                                pass

                        # Уведомим администраторов о факте использования
                        try:
                            plan = get_plan_by_id(plan_id)
                            plan_name = plan.get("plan_name", "Unknown") if plan else "Unknown"
                            admins = list(get_admin_ids() or [])
                            if should_deactivate:
                                status_line = "<b>Статус:</b> деактивирован"
                                if reason_lines:
                                    status_line += " (" + ", ".join(reason_lines) + ")"
                            else:
                                status_line = "<b>Статус:</b> активен"
                                if limit_total:
                                    status_line += (
                                        f" (использовано {used_total_now} из {limit_total})"
                                    )
                                else:
                                    status_line += f" (использовано {used_total_now})"
                            text = (
                                "🎟️ <b>Промокод использован</b>\n\n"
                                f"<b>Код:</b> {promo_code_used}\n"
                                f"<b>Пользователь:</b> {user_id}\n"
                                f"<b>Тариф:</b> {plan_name} ({months} мес.)\n"
                                f"{status_line}"
                            )
                            for aid in admins:
                                try:
                                    await bot.send_message(int(aid), text)
                                except Exception:
                                    pass
                        except Exception:
                            pass
                except Exception as e:
                    logger.warning(
                        f"Не удалось активировать промокод для пользователя {user_id}, код {promo_code_used}: {e}"
                    )
        except Exception:
            pass

        # Аккуратно удаляем служебное сообщение о обработке, если возможно
        try:
            await processing_message.delete()
        except Exception:
            pass

        connection_string = None
        new_expiry_date = None
        try:
            connection_string = (
                result.get("connection_string") if isinstance(result, dict) else None
            )
            new_expiry_date = (
                datetime.fromtimestamp(result["expiry_timestamp_ms"] / 1000)
                if isinstance(result, dict) and "expiry_timestamp_ms" in result
                else None
            )
        except Exception:
            connection_string = None
            new_expiry_date = None

        all_user_keys = await asyncio.to_thread(get_user_keys, user_id)
        key_number = next(
            (i + 1 for i, key in enumerate(all_user_keys) if key["key_id"] == key_id),
            len(all_user_keys),
        )

        final_text = get_purchase_success_text(
            action="создан" if action == "new" else "продлен",
            key_number=key_number,
            expiry_date=new_expiry_date or datetime.now(),
            connection_string=connection_string or "",
        )

        await bot.send_message(
            chat_id=user_id,
            text=final_text,
            reply_markup=keyboards.create_key_info_keyboard(key_id, connection_string),
        )

        try:
            await notify_admin_of_purchase(bot, metadata)
        except Exception as e:
            logger.warning(f"Не удалось уведомить админа о покупке: {e}")

    except Exception as e:
        logger.error(
            f"Ошибка при обработке платежа для пользователя {user_id} на хосте {host_name}: {e}",
            exc_info=True,
        )
        try:
            await delete_and_send(processing_message, "❌ Ошибка при выдаче подписки")
        except Exception:
            try:
                await bot.send_message(chat_id=user_id, text="❌ Ошибка при выдаче подписки")
            except Exception:
                pass
