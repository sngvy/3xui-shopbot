"""Построители inline-клавиатур для Telegram-бота."""

import base64
import hashlib
import logging
import os
import re
from datetime import datetime
from typing import Callable

from aiogram.types import (
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    KeyboardButton,
    ReplyKeyboardMarkup,
    WebAppInfo,
)
from aiogram.utils.keyboard import InlineKeyboardBuilder

from shop_bot.data_manager.database import (
    format_stored_username,
    get_key_by_id,
    get_setting,
    normalize_host_name,
)

logger = logging.getLogger(__name__)

REDIR_URL = os.getenv("REDIR_URL")

main_reply_keyboard = ReplyKeyboardMarkup(
    keyboard=[[KeyboardButton(text="🏠 Главное меню")]], resize_keyboard=True
)


def encode_host_callback_token(host_name: str) -> str:
    """Сформировать короткий ASCII-токен для host_name для использования в callback_data."""
    normalized = normalize_host_name(host_name)
    slug = re.sub(r"[^a-z0-9]+", "-", normalized.lower()).strip("-")
    slug = slug[:8]
    digest = hashlib.sha1(normalized.encode("utf-8")).hexdigest()[:8]
    if slug:
        return f"{slug}-{digest}"
    return digest


def parse_host_callback_data(data: str) -> tuple[str, str, str] | None:
    """Разбирает callback_data вида 'select_host:action:extra:token' на составляющие."""
    if not data or not data.startswith("select_host:"):
        return None
    parts = data.split(":", 3)
    if len(parts) != 4:
        return None
    _, action, extra, token = parts
    return action, extra or "-", token


def find_host_by_callback_token(hosts: list[dict], token: str) -> dict | None:
    """Находит хост в списке по токену из callback_data (см. encode_host_callback_token)."""
    if not token:
        return None
    for host in hosts or []:
        if encode_host_callback_token(host.get("host_name", "")) == token:
            return host
    return None


# --- Generic builder from DB configs ---
def _build_keyboard_from_db(
    menu_type: str,
    text_replacements: dict[str, str] | None = None,
    filter_func: Callable[[dict], bool] | None = None,
) -> InlineKeyboardMarkup | None:
    """Build InlineKeyboardMarkup from button configs for a given menu_type.

    Returns None if configs are missing or on error.
    """
    try:
        from shop_bot.data_manager.database import get_button_configs

        configs = get_button_configs(menu_type)
    except Exception as e:
        logger.warning(f"Конфигурации БД для {menu_type} недоступны: {e}")
        return None

    if not configs:
        return None

    builder = InlineKeyboardBuilder()

    # Group by row, keep positions and widths
    rows: dict[int, list[dict]] = {}
    added: set[str] = set()

    for cfg in configs:
        if not cfg.get("is_active", True):
            continue
        if filter_func and not filter_func(cfg):
            continue

        text = cfg.get("text", "") or ""
        callback_data = cfg.get("callback_data")
        url = cfg.get("url")
        button_id = (cfg.get("button_id") or "").strip()

        if not callback_data and not url:
            continue

        # Deduplicate by button_id if provided
        if button_id:
            if button_id in added:
                continue
            added.add(button_id)

        # Apply text replacements (e.g., counts)
        if text_replacements:
            try:
                for k, v in text_replacements.items():
                    text = text.replace(k, str(v))
            except Exception:
                pass

        row_pos = int(cfg.get("row_position", 0) or 0)
        col_pos = int(cfg.get("column_position", 0) or 0)
        sort_order = int(cfg.get("sort_order", 0) or 0)
        width = int(cfg.get("button_width", 1) or 1)

        rows.setdefault(row_pos, []).append(
            {
                "text": text,
                "callback_data": callback_data,
                "url": url,
                "width": max(1, min(width, 3)),
                "col": col_pos,
                "sort": sort_order,
            }
        )

    if not rows:
        return None

    # Build keyboard respecting row positions and button widths
    # In Telegram: width 1 = half row, width 2+ = full row
    for row_idx in sorted(rows.keys()):
        row_buttons = sorted(rows[row_idx], key=lambda b: (b["col"], b["sort"]))

        # Process buttons for this row position
        i = 0
        while i < len(row_buttons):
            btn = row_buttons[i]
            button_width = btn["width"]

            # Width 2+ means full row
            if button_width >= 2:
                # Add as single button in row
                if btn["callback_data"]:
                    builder.row(
                        InlineKeyboardButton(text=btn["text"], callback_data=btn["callback_data"])
                    )
                elif btn["url"]:
                    builder.row(InlineKeyboardButton(text=btn["text"], url=btn["url"]))
                i += 1
            else:
                # Width 1 - try to pair with next button if it also has width 1
                if i + 1 < len(row_buttons) and row_buttons[i + 1]["width"] == 1:
                    # Add two buttons in one row
                    btn2 = row_buttons[i + 1]
                    buttons = []

                    if btn["callback_data"]:
                        buttons.append(
                            InlineKeyboardButton(
                                text=btn["text"], callback_data=btn["callback_data"]
                            )
                        )
                    elif btn["url"]:
                        buttons.append(InlineKeyboardButton(text=btn["text"], url=btn["url"]))

                    if btn2["callback_data"]:
                        buttons.append(
                            InlineKeyboardButton(
                                text=btn2["text"], callback_data=btn2["callback_data"]
                            )
                        )
                    elif btn2["url"]:
                        buttons.append(InlineKeyboardButton(text=btn2["text"], url=btn2["url"]))

                    builder.row(*buttons)
                    i += 2
                else:
                    # Single button with width 1 - add alone
                    if btn["callback_data"]:
                        builder.row(
                            InlineKeyboardButton(
                                text=btn["text"], callback_data=btn["callback_data"]
                            )
                        )
                    elif btn["url"]:
                        builder.row(InlineKeyboardButton(text=btn["text"], url=btn["url"]))
                    i += 1

    return builder.as_markup()


def create_main_menu_keyboard(
    user_keys: list, trial_available: bool, is_admin: bool
) -> InlineKeyboardMarkup:
    # Prepare filters and replacements for main menu
    """Строит клавиатуру главного меню (из настроек БД либо по резервной жёсткой логике)."""

    def _filter(cfg: dict) -> bool:
        button_id = (cfg.get("button_id") or "").strip()
        # Filter trial button
        if button_id == "btn_try":
            if not trial_available or get_setting("trial_enabled") != "true":
                return False
        # Filter admin button
        if button_id == "btn_admin" and not is_admin:
            return False
        return True

    # Fallback to original implementation if DB config not available
    builder = InlineKeyboardBuilder()

    # Fallback to original hardcoded logic
    logger.info("Используется резервная (жёстко заданная) логика кнопок")
    if trial_available and get_setting("trial_enabled") == "true":
        builder.button(
            text=(get_setting("btn_try") or "🎁 Попробовать бесплатно"),
            callback_data="get_trial",
            style="success",
        )

    builder.button(
        text=(get_setting("btn_profile") or "👤 Мой профиль"),
        callback_data="show_profile",
        style="primary",
    )
    keys_label_tpl = get_setting("btn_my_keys") or "🔑 Мои подписки ({count})"
    builder.button(
        text=keys_label_tpl.replace("{count}", str(len(user_keys))),
        callback_data="manage_keys",
        style="success",
    )
    builder.button(
        text=(get_setting("btn_buy_key") or "💳 Купить подписку"),
        callback_data="buy_new_key",
        style="danger",
    )
    builder.button(
        text=(get_setting("btn_top_up") or "➕ Пополнить баланс"),
        callback_data="top_up_start",
        style="danger",
    )
    builder.button(
        text=(get_setting("btn_referral") or "🤝 Реферальная программа"),
        callback_data="show_referral_program",
    )
    builder.button(text=(get_setting("btn_support") or "🆘 Поддержка"), callback_data="show_help")
    builder.button(text=(get_setting("btn_about") or "ℹ️ О сервисе"), callback_data="show_about")
    builder.button(
        text=(get_setting("btn_howto") or "❓ Как использовать"),
        callback_data="howto_vless",
    )
    if os.getenv("USE_WEBAPP_SPEEDTEST", "").strip() == "1":
        # В этом режиме спидтест — не callback с прогоном на сервере, а
        # отдельная веб-страница (speed.html), открываемая как Telegram
        # Mini App — тот же паттерн, что и "Открыть Web App" в карточке
        # подписки (WebAppInfo(url=...), не callback_data).
        #
        # WORKER_DOMAIN — домен, где отдаётся сам этот бот/Flask-сервер
        # (webhook_server/app.py, роуты /speed, /mon, /redir).
        worker_domain = os.getenv("WORKER_DOMAIN", "").strip()
        if worker_domain:
            builder.button(
                text=(get_setting("btn_speed") or "⚡ Тест скорости"),
                web_app=WebAppInfo(url=f"https://{worker_domain}/speed"),
            )
        else:
            # Без WORKER_DOMAIN нет валидного https-адреса для Web App —
            # откатываемся на обычный callback-вариант.
            logger.warning(
                "USE_WEBAPP_SPEEDTEST=1, но WORKER_DOMAIN не задан в .env — "
                "используется обычная кнопка теста скорости вместо Web App."
            )
            builder.button(
                text=(get_setting("btn_speed") or "⚡ Тест скорости"),
                callback_data="user_speedtest",
            )
    else:
        builder.button(
            text=(get_setting("btn_speed") or "⚡ Тест скорости"),
            callback_data="user_speedtest",
        )

    # "Статус сервисов" — отдельная страница (mon.html), всегда как Web App.
    # Без WORKER_DOMAIN нет валидного https-адреса для WebApp вообще, и тут
    # нет старого callback-варианта, на который можно было бы откатиться,
    # так что просто не показываем.
    worker_domain_for_status = os.getenv("WORKER_DOMAIN", "").strip()
    status_button_shown = bool(worker_domain_for_status)
    if status_button_shown:
        builder.button(
            text=(get_setting("btn_status") or "🟢 Статус сервисов"),
            web_app=WebAppInfo(url=f"https://{worker_domain_for_status}/mon"),
        )
    else:
        logger.warning(
            "Кнопка 'Статус сервисов' скрыта в главном меню — WORKER_DOMAIN "
            "не задан в .env, нет валидного https-адреса для Web App."
        )

    if is_admin:
        builder.button(text=(get_setting("btn_admin") or "⚙️ Админка"), callback_data="admin_menu")

    # Строим сетку динамически, в зависимости от прав и доступности триала
    layout = []

    # Триал (если включен и доступен юзеру)
    if trial_available and get_setting("trial_enabled") == "true":
        layout.append(1)

    # Стандартные блоки кнопок (всегда парами)
    layout.append(2)  # Профиль + Подписки
    layout.append(2)  # Купить + Пополнить
    layout.append(2)  # Рефералка + Поддержка
    layout.append(2)  # О проекте + Инструкция

    # Тест скорости для юзеров (одиночная широкая кнопка)
    layout.append(1)

    # Статус сервисов — тоже одиночная широкая кнопка, но только если она
    # реально была добавлена выше (иначе счётчик в adjust() сломает сетку)
    if status_button_shown:
        layout.append(1)

    # Кнопка админки (только для админов, в самом низу)
    if is_admin:
        layout.append(1)

    # Распаковываем получившийся список в метод adjust
    builder.adjust(*layout)
    return builder.as_markup()


def create_admin_menu_keyboard() -> InlineKeyboardMarkup:
    # Try DB-driven keyboard first
    """Строит клавиатуру главного меню админ-панели."""
    kb = _build_keyboard_from_db("admin_menu")
    if kb:
        return kb

    # Fallback hardcoded
    builder = InlineKeyboardBuilder()
    builder.button(text="👥 Пользователи", callback_data="admin_users")
    builder.button(text="🌍 Подписки на хосте", callback_data="admin_host_keys")
    builder.button(text="🔎 Поиск пользователей", callback_data="admin_search_user")
    builder.button(text="🟢 Текущий онлайн", callback_data="admin_realtime_online")
    builder.button(text="🏳️ Потребление WL", callback_data="admin_wl_traffic_report")
    builder.button(text="📋 Логи контейнера", callback_data="admin_get_docker_logs")
    builder.button(text="💸 История кассы", callback_data="admin_view_cashbox")
    builder.button(text="🪄 Выдать подписку", callback_data="admin_gift_key")
    builder.button(text="⚡ Тест скорости", callback_data="admin_speedtest")
    builder.button(text="📊 Мониторинг", callback_data="admin_monitor")
    builder.button(text="🗄 Бэкап БД", callback_data="admin_backup_db")
    builder.button(text="♻️ Восстановить БД", callback_data="admin_restore_db")
    builder.button(text="🔄 Перезапуск", callback_data="admin_bot_restart")
    builder.button(text="👮 Администраторы", callback_data="admin_admins_menu")
    builder.button(text="🎟 Промокоды", callback_data="admin_promo_menu")
    builder.button(text="🏆 Топ рефереров", callback_data="admin_top_referrers")
    builder.button(text="📢 Рассылка", callback_data="start_broadcast")

    # Кнопку "Назад" добавляем последней
    builder.button(text="⬅️ Назад в меню", callback_data="back_to_main_menu")

    # --- Динамическое построение сетки ---
    # Считаем только основные кнопки (все, кроме "Назад")
    main_buttons_count = len(list(builder.buttons)) - 1

    layout = []

    # Все основные кнопки группируем по 2 в ряд
    for _ in range(main_buttons_count // 2):
        layout.append(2)

    # Если общее количество админ-кнопок вдруг станет нечетным,
    # оставшаяся одна кнопка красиво займет целую строку перед "Назад"
    if main_buttons_count % 2 != 0:
        layout.append(1)

    # Кнопка "Назад" всегда одна на всю ширину
    layout.append(1)

    builder.adjust(*layout)
    return builder.as_markup()


def create_admin_top_referrers_keyboard() -> InlineKeyboardMarkup:
    """Клавиатура экрана 'Топ рефереров' — только возврат в админ-меню."""
    builder = InlineKeyboardBuilder()
    builder.button(text="⬅️ В админ-меню", callback_data="admin_menu")
    builder.adjust(1)
    return builder.as_markup()


def create_admins_menu_keyboard() -> InlineKeyboardMarkup:
    """Строит клавиатуру меню управления списком администраторов."""
    builder = InlineKeyboardBuilder()
    builder.button(text="➕ Добавить админа", callback_data="admin_add_admin")
    builder.button(text="➖ Снять админа", callback_data="admin_remove_admin")
    builder.button(text="📋 Список админов", callback_data="admin_view_admins")
    builder.button(text="⬅️ В админ-меню", callback_data="admin_menu")
    builder.adjust(2, 2)
    return builder.as_markup()


def create_admin_monitor_keyboard() -> InlineKeyboardMarkup:
    """Строит клавиатуру раздела мониторинга ресурсов для админа."""
    builder = InlineKeyboardBuilder()
    builder.button(text="🔄 Обновить", callback_data="admin_monitor_refresh")
    builder.button(text="⬅️ В админ-меню", callback_data="admin_menu")
    builder.adjust(1, 1)
    return builder.as_markup()


def create_admin_users_keyboard(
    users: list[dict], page: int = 0, page_size: int = 10
) -> InlineKeyboardMarkup:
    """Строит клавиатуру постраничного списка пользователей для админа."""
    builder = InlineKeyboardBuilder()
    start = page * page_size
    end = start + page_size
    for u in users[start:end]:
        user_id = u.get("telegram_id") or u.get("user_id") or u.get("id")
        title = f"{user_id} ▪️ {format_stored_username(u.get('username'), linked=False, escape_html=False)}"
        builder.button(text=title, callback_data=f"admin_view_user_{user_id}")
    # Pagination
    total = len(users)
    have_prev = page > 0
    have_next = end < total
    if have_prev:
        builder.button(text="⬅️ Назад", callback_data=f"admin_users_page_{page-1}")
    if have_next:
        builder.button(text="Вперёд ➡️", callback_data=f"admin_users_page_{page+1}")
    builder.button(text="⬅️ В админ-меню", callback_data="admin_menu")
    # Layout: list (1 per row), then pagination/buttons (2), then back (1)
    rows = [1] * len(users[start:end])
    tail = []
    if have_prev or have_next:
        tail.append(2 if (have_prev and have_next) else 1)
    tail.append(1)
    builder.adjust(*(rows + tail if rows else ([2] if (have_prev or have_next) else []) + [1]))
    return builder.as_markup()


def create_admin_user_actions_keyboard(
    user_id: int, is_banned: bool | None = None
) -> InlineKeyboardMarkup:
    """Строит клавиатуру действий над конкретным пользователем (бан, баланс, ключи и т.д.)."""
    builder = InlineKeyboardBuilder()
    builder.button(text="➕ Начислить баланс", callback_data=f"admin_add_balance_{user_id}")
    builder.button(text="➖ Списать баланс", callback_data=f"admin_deduct_balance_{user_id}")
    builder.button(text="🪄 Выдать подписку", callback_data=f"admin_gift_key_{user_id}")
    builder.button(text="🤝 Рефералы пользователя", callback_data=f"admin_user_referrals_{user_id}")
    if is_banned:
        builder.button(text="✅ Разбанить", callback_data=f"admin_unban_user_{user_id}")
    else:
        builder.button(text="🚫 Забанить", callback_data=f"admin_ban_user_{user_id}")
    builder.button(text="🔑 Подписки пользователя", callback_data=f"admin_user_keys_{user_id}")
    builder.button(text="📜 История операций", callback_data=f"admin_tx_history_{user_id}_0")
    builder.button(text="⬅️ К списку", callback_data="admin_users")
    builder.button(text="⬅️ В админ-меню", callback_data="admin_menu")
    # Сделаем шире: 2 колонки, затем назад и в админ-меню
    builder.adjust(2, 2, 2, 1, 1, 1)
    return builder.as_markup()


def create_admin_transaction_history_keyboard(
    user_id: int, page: int, has_prev: bool, has_next: bool
) -> InlineKeyboardMarkup:
    """Строит клавиатуру постраничной истории операций пользователя для админа."""
    builder = InlineKeyboardBuilder()
    if has_prev:
        builder.button(text="⬅️ Назад", callback_data=f"admin_tx_history_{user_id}_{page-1}")
    if has_next:
        builder.button(text="Вперёд ➡️", callback_data=f"admin_tx_history_{user_id}_{page+1}")
    builder.button(text="⬅️ К пользователю", callback_data=f"admin_view_user_{user_id}")
    if has_prev and has_next:
        builder.adjust(2, 1)
    elif has_prev or has_next:
        builder.adjust(1, 1)
    else:
        builder.adjust(1)
    return builder.as_markup()


def create_admin_user_keys_keyboard(user_id: int, keys: list[dict]) -> InlineKeyboardMarkup:
    """Строит клавиатуру списка ключей конкретного пользователя для админа."""
    builder = InlineKeyboardBuilder()
    if keys:
        for k in keys:
            kid = k.get("key_id")
            host = k.get("host_name") or "—"
            email = k.get("key_email") or "—"
            title = f"№ {kid} ▪️ {host} ▪️ {email[:20]}"
            builder.button(text=title, callback_data=f"admin_edit_key_{kid}")
    else:
        builder.button(text="Подписок нет", callback_data="noop")
    builder.button(text="⬅️ Назад", callback_data=f"admin_view_user_{user_id}")
    builder.adjust(1)
    return builder.as_markup()


def create_admin_key_actions_keyboard(
    key_id: int, user_id: int | None = None
) -> InlineKeyboardMarkup:
    """Строит клавиатуру действий над конкретным ключом (продление, удаление, смена хоста и т.д.)."""
    builder = InlineKeyboardBuilder()
    builder.button(text="🌍 Изменить сервер", callback_data=f"admin_key_edit_host_{key_id}")
    builder.button(text="➕ Добавить дни", callback_data=f"admin_key_extend_{key_id}")
    builder.button(text="🗑 Удалить подписку", callback_data=f"admin_key_delete_{key_id}")
    builder.button(text="⬅️ Назад к подпискам", callback_data=f"admin_key_back_{key_id}")
    if user_id is not None:
        builder.button(text="👤 Перейти к пользователю", callback_data=f"admin_view_user_{user_id}")
        builder.adjust(2, 2, 1)
    else:
        builder.adjust(2, 2)
    return builder.as_markup()


def create_admin_delete_key_confirm_keyboard(key_id: int) -> InlineKeyboardMarkup:
    """Строит клавиатуру подтверждения удаления ключа."""
    builder = InlineKeyboardBuilder()
    builder.button(
        text="✅ Подтвердить удаление",
        callback_data=f"admin_key_delete_confirm_{key_id}",
    )
    builder.button(text="❌ Отмена", callback_data=f"admin_key_delete_cancel_{key_id}")
    builder.adjust(1)
    return builder.as_markup()


def create_admin_ban_confirm_keyboard(user_id: int) -> InlineKeyboardMarkup:
    """Строит клавиатуру подтверждения бана/разбана пользователя."""
    builder = InlineKeyboardBuilder()
    builder.button(text="✅ Подтвердить бан", callback_data=f"admin_confirm_ban_{user_id}")
    builder.button(text="❌ Отмена", callback_data=f"admin_view_user_{user_id}")
    builder.adjust(1)
    return builder.as_markup()


def create_regen_key_confirm_keyboard(key_id: int) -> InlineKeyboardMarkup:
    """Строит клавиатуру подтверждения перевыпуска ключа."""
    builder = InlineKeyboardBuilder()
    builder.button(text="✅ Подтвердить", callback_data=f"regen_key_confirm_{key_id}")
    builder.button(text="⬅️ Назад", callback_data=f"show_key_{key_id}")
    builder.adjust(1)
    return builder.as_markup()


def create_admin_balance_confirm_keyboard(action: str) -> InlineKeyboardMarkup:
    """Строит клавиатуру подтверждения начисления/списания баланса (action: 'add' или 'deduct' — используется вместе с суммой/пользователем, сохранёнными в FSM-состоянии, поэтому колбэк без параметров в data)."""
    builder = InlineKeyboardBuilder()
    label = "✅ Подтвердить начисление" if action == "add" else "✅ Подтвердить списание"
    builder.button(text=label, callback_data=f"admin_balance_confirm_{action}")
    builder.button(text="❌ Отмена", callback_data=f"admin_balance_cancel_{action}")
    builder.adjust(1)
    return builder.as_markup()


def create_admin_gift_confirm_keyboard() -> InlineKeyboardMarkup:
    """Строит клавиатуру подтверждения выдачи подарочного ключа."""
    builder = InlineKeyboardBuilder()
    builder.button(text="✅ Подтвердить выдачу", callback_data="admin_gift_confirm")
    builder.button(text="❌ Отмена", callback_data="admin_gift_cancel")
    builder.adjust(1)
    return builder.as_markup()


def create_admin_cancel_keyboard() -> InlineKeyboardMarkup:
    """Строит клавиатуру с единственной кнопкой отмены текущего действия админа."""
    builder = InlineKeyboardBuilder()
    builder.button(text="❌ Отмена", callback_data="admin_cancel")
    return builder.as_markup()


def create_admin_promo_code_keyboard() -> InlineKeyboardMarkup:
    """Строит клавиатуру меню управления промокодами."""
    builder = InlineKeyboardBuilder()
    builder.button(text="🎲 Сгенерировать код", callback_data="admin_promo_gen_code")
    builder.button(text="❌ Отмена", callback_data="admin_cancel")
    builder.adjust(1)
    return builder.as_markup()


def create_broadcast_segment_keyboard() -> InlineKeyboardMarkup:
    """Строит клавиатуру выбора сегмента пользователей для рассылки."""
    builder = InlineKeyboardBuilder()
    builder.button(text="👥 Все пользователи", callback_data="broadcast_segment_all")
    builder.button(text="💤 Платили раньше", callback_data="broadcast_segment_paid_no_active")
    builder.button(text="🛒 Ни разу не покупали", callback_data="broadcast_segment_never_purchased")
    builder.button(text="❌ Отмена", callback_data="cancel_broadcast")
    builder.adjust(1)
    return builder.as_markup()


def create_broadcast_promo_option_keyboard() -> InlineKeyboardMarkup:
    """Строит клавиатуру выбора: прикреплять ли промокод к рассылке."""
    builder = InlineKeyboardBuilder()
    builder.button(text="🎁 Приложить", callback_data="broadcast_add_promo")
    builder.button(text="➡️ Без промокода", callback_data="broadcast_skip_promo")
    builder.button(text="❌ Отмена", callback_data="cancel_broadcast")
    builder.adjust(2, 1)
    return builder.as_markup()


def create_broadcast_options_keyboard() -> InlineKeyboardMarkup:
    """Строит клавиатуру дополнительных опций рассылки (кнопка и т.д.)."""
    builder = InlineKeyboardBuilder()
    builder.button(text="➕ Добавить кнопку", callback_data="broadcast_add_button")
    builder.button(text="➡️ Пропустить", callback_data="broadcast_skip_button")
    builder.button(text="❌ Отмена", callback_data="cancel_broadcast")
    builder.adjust(2, 1)
    return builder.as_markup()


def create_broadcast_confirmation_keyboard() -> InlineKeyboardMarkup:
    """Строит клавиатуру финального подтверждения запуска рассылки."""
    builder = InlineKeyboardBuilder()
    builder.button(text="✅ Отправить всем", callback_data="confirm_broadcast")
    builder.button(text="❌ Отмена", callback_data="cancel_broadcast")
    builder.adjust(2)
    return builder.as_markup()


def create_broadcast_cancel_keyboard() -> InlineKeyboardMarkup:
    """Строит клавиатуру с кнопкой отмены рассылки."""
    builder = InlineKeyboardBuilder()
    builder.button(text="❌ Отмена", callback_data="cancel_broadcast")
    return builder.as_markup()


def create_about_keyboard(
    channel_url: str | None, terms_url: str | None, privacy_url: str | None
) -> InlineKeyboardMarkup:
    """Строит клавиатуру раздела 'О проекте' (канал, условия, политика)."""
    builder = InlineKeyboardBuilder()
    if channel_url:
        builder.button(text=(get_setting("btn_channel") or "📰 Наш канал"), url=channel_url)
    if terms_url:
        builder.button(text=(get_setting("btn_terms") or "📄 Условия использования"), url=terms_url)
    if privacy_url:
        builder.button(
            text=(get_setting("btn_privacy") or "🔒 Политика конфиденциальности"),
            url=privacy_url,
        )
    builder.button(
        text=(get_setting("btn_back_to_menu") or "⬅️ Назад в меню"),
        callback_data="back_to_main_menu",
    )
    builder.adjust(1)
    return builder.as_markup()


def create_support_keyboard(support_user: str | None = None) -> InlineKeyboardMarkup:
    """Строит клавиатуру раздела поддержки."""
    builder = InlineKeyboardBuilder()
    # Определяем username для поддержки
    username = (support_user or "").strip()
    if not username:
        username = (
            get_setting("support_bot_username") or get_setting("support_user") or ""
        ).strip()
    # Преобразуем в tg:// ссылку, если есть username/ссылка
    url: str | None = None
    if username:
        if username.startswith("@"):  # @username
            url = f"tg://resolve?domain={username[1:]}"
        elif username.startswith("tg://"):  # Уже tg-схема
            url = username
        elif username.startswith("http://") or username.startswith("https://"):
            # http(s) ссылки на t.me/telegram.me -> в tg://
            # Попробуем извлечь domain
            try:
                # Простое извлечение последнего сегмента
                part = username.split("/")[-1].split("?")[0]
                if part:
                    url = f"tg://resolve?domain={part}"
            except Exception:
                url = username
        else:
            # Просто username без @
            url = f"tg://resolve?domain={username}"

    if url:
        builder.button(text=(get_setting("btn_support") or "🆘 Поддержка"), url=url)
        builder.button(
            text=(get_setting("btn_back_to_menu") or "⬅️ Назад в меню"),
            callback_data="back_to_main_menu",
        )
    else:
        # Фолбэк: встроенное меню поддержки
        builder.button(
            text=(get_setting("btn_support") or "🆘 Поддержка"),
            callback_data="show_help",
        )
        builder.button(
            text=(get_setting("btn_back_to_menu") or "⬅️ Назад в меню"),
            callback_data="back_to_main_menu",
        )
    builder.adjust(1)
    return builder.as_markup()


def create_support_bot_link_keyboard(support_bot_username: str) -> InlineKeyboardMarkup:
    """Строит клавиатуру со ссылкой на бота поддержки."""
    builder = InlineKeyboardBuilder()
    username = support_bot_username.lstrip("@")
    deep_link = f"tg://resolve?domain={username}&start=new"
    builder.button(text=(get_setting("btn_support_open") or "🆘 Открыть поддержку"), url=deep_link)
    builder.button(
        text=(get_setting("btn_back_to_menu") or "⬅️ Назад в меню"),
        callback_data="back_to_main_menu",
    )
    builder.adjust(1)
    return builder.as_markup()


def create_support_menu_keyboard(has_external: bool = False) -> InlineKeyboardMarkup:
    """Строит клавиатуру меню поддержки (создать обращение, мои обращения)."""

    def _filter(cfg: dict) -> bool:
        # Если внешняя поддержка недоступна, скрыть кнопку support_external
        if not has_external:
            cd = (cfg.get("callback_data") or "").strip()
            bid = (cfg.get("button_id") or "").strip()
            if cd == "support_external" or bid == "btn_support_external":
                return False
        return True

    kb = _build_keyboard_from_db("support_menu", filter_func=_filter)
    if kb:
        return kb

    builder = InlineKeyboardBuilder()
    builder.button(
        text=(get_setting("btn_support_new_ticket") or "✍️ Новое обращение"),
        callback_data="support_new_ticket",
    )
    builder.button(
        text=(get_setting("btn_support_my_tickets") or "📨 Мои обращения"),
        callback_data="support_my_tickets",
    )
    if has_external:
        builder.button(
            text=(get_setting("btn_support_external") or "🆘 Внешняя поддержка"),
            callback_data="support_external",
        )
    builder.button(
        text=(get_setting("btn_back_to_menu") or "⬅️ Назад в меню"),
        callback_data="back_to_main_menu",
    )
    builder.adjust(1)
    return builder.as_markup()


def create_tickets_list_keyboard(tickets: list[dict]) -> InlineKeyboardMarkup:
    """Строит клавиатуру списка тикетов поддержки пользователя."""
    builder = InlineKeyboardBuilder()
    if tickets:
        for t in tickets:
            title = f"№ {t['ticket_id']} ▪️ {t.get('status', 'open')}"
            if t.get("subject"):
                title += f"▪️{t['subject'][:20]}"
            builder.button(text=title, callback_data=f"support_view_{t['ticket_id']}")
    builder.button(text="⬅️ Назад", callback_data="support_menu")
    builder.adjust(1)
    return builder.as_markup()


def create_ticket_actions_keyboard(ticket_id: int, is_open: bool = True) -> InlineKeyboardMarkup:
    """Строит клавиатуру действий над конкретным тикетом поддержки."""
    builder = InlineKeyboardBuilder()
    if is_open:
        builder.button(text="💬 Ответить", callback_data=f"support_reply_{ticket_id}")
        builder.button(text="✅ Закрыть", callback_data=f"support_close_{ticket_id}")
    builder.button(text="⬅️ К списку", callback_data="support_my_tickets")
    builder.adjust(1)
    return builder.as_markup()


def create_host_selection_keyboard(hosts: list, action: str) -> InlineKeyboardMarkup:
    """Строит клавиатуру выбора сервера (хоста) для покупки/продления ключа."""
    builder = InlineKeyboardBuilder()
    base_action = action
    extra = "-"
    if action.startswith("switch_"):
        base_action = "switch"
        extra = action[len("switch_") :] or "-"
    elif action in {"trial", "new"}:
        base_action = action
    else:
        base_action = action
    prefix = f"select_host:{base_action}:{extra}:"
    for host in hosts:
        token = encode_host_callback_token(host["host_name"])
        builder.button(text=host["host_name"], callback_data=f"{prefix}{token}")
    builder.button(
        text=(get_setting("btn_back_to_menu") or "⬅️ Назад в меню"),
        callback_data="manage_keys" if action == "new" else "back_to_main_menu",
    )
    builder.adjust(1)
    return builder.as_markup()


def create_plans_keyboard(
    plans: list[dict], action: str, host_name: str, key_id: int = 0
) -> InlineKeyboardMarkup:
    """Строит клавиатуру выбора тарифного плана."""
    builder = InlineKeyboardBuilder()
    for plan in plans:
        token = encode_host_callback_token(host_name)
        callback_data = f"buy_{token}_{plan['plan_id']}_{action}_{key_id}"
        builder.button(
            text=f"{plan['plan_name']} - {plan['price']:.0f} RUB",
            callback_data=callback_data,
        )
    back_callback = "manage_keys" if action == "extend" else "buy_new_key"
    builder.button(text=(get_setting("btn_back") or "⬅️ Назад"), callback_data=back_callback)
    builder.adjust(1)
    return builder.as_markup()


def create_skip_email_keyboard() -> InlineKeyboardMarkup:
    """Строит клавиатуру с кнопкой пропуска ввода email при оплате."""
    builder = InlineKeyboardBuilder()
    builder.button(
        text=(get_setting("btn_skip_email") or "➡️ Продолжить без почты"),
        callback_data="skip_email",
    )
    builder.button(
        text=(get_setting("btn_back_to_plans") or "⬅️ Назад к тарифам"),
        callback_data="back_to_plans",
    )
    builder.adjust(1)
    return builder.as_markup()


def create_payment_method_keyboard(
    payment_methods: dict,
    action: str,
    key_id: int,
    show_balance: bool | None = None,
    main_balance: float | None = None,
    price: float | None = None,
    has_promo_applied: bool | None = None,
) -> InlineKeyboardMarkup:
    """Строит клавиатуру выбора способа оплаты."""
    builder = InlineKeyboardBuilder()

    # Промокод: ввести/убрать
    if has_promo_applied:
        builder.button(text="❌ Убрать промокод", callback_data="remove_promo_code")
    else:
        builder.button(text="🎟️ Ввести промокод", callback_data="enter_promo_code")

    # Кнопки оплаты с балансов (если разрешено/достаточно средств)
    if show_balance:
        label = get_setting("btn_pay_with_balance") or "💼 Оплатить с баланса"
        if main_balance is not None:
            try:
                label += f" ({main_balance:.0f} RUB)"
            except Exception:
                pass
        builder.button(text=label, callback_data="pay_balance")

    # Внешние способы оплаты
    if payment_methods and payment_methods.get("yookassa"):
        if get_setting("sbp_enabled"):
            builder.button(text="🏦 СБП / Банковская карта", callback_data="pay_yookassa")
        else:
            builder.button(text="🏦 Банковская карта", callback_data="pay_yookassa")
    if payment_methods and payment_methods.get("heleket"):
        builder.button(text="💎 Криптовалюта", callback_data="pay_heleket")
    if payment_methods and payment_methods.get("cryptobot"):
        builder.button(text="🤖 CryptoBot", callback_data="pay_cryptobot")
    if payment_methods and payment_methods.get("yoomoney"):
        builder.button(text="💜 ЮMoney (кошелёк)", callback_data="pay_yoomoney")
    if payment_methods and payment_methods.get("stars"):
        builder.button(text="⭐ Telegram Stars", callback_data="pay_stars")
    if payment_methods and payment_methods.get("tonconnect"):
        callback_data_ton = "pay_tonconnect"
        logger.info(f"Создание кнопки TON с callback_data: '{callback_data_ton}'")
        builder.button(text="🪙 TON Connect", callback_data=callback_data_ton)

    builder.button(
        text=(get_setting("btn_back") or "⬅️ Назад"),
        callback_data="back_to_email_prompt",
    )
    builder.adjust(1)
    return builder.as_markup()


def create_admin_promos_menu_keyboard() -> InlineKeyboardMarkup:
    """Строит клавиатуру меню промокодов для админа (список, создание, удаление всех)."""
    builder = InlineKeyboardBuilder()
    builder.button(text="➕ Создать промокод", callback_data="admin_promo_create")
    builder.button(text="📋 Список промокодов", callback_data="admin_promo_list")
    builder.button(text="🗑️ Удалить все промокоды", callback_data="admin_promo_delete_all")
    builder.button(text="⬅️ В админ-меню", callback_data="admin_menu")
    builder.adjust(1)
    return builder.as_markup()


def create_admin_promo_delete_all_confirm_keyboard() -> InlineKeyboardMarkup:
    """Клавиатура подтверждения удаления всех промокодов."""
    builder = InlineKeyboardBuilder()
    builder.button(text="✅ Подтвердить удаление", callback_data="admin_promo_delete_all_confirm")
    builder.button(text="❌ Отмена", callback_data="admin_promo_menu")
    builder.adjust(1)
    return builder.as_markup()


def create_admin_promo_discount_keyboard() -> InlineKeyboardMarkup:
    """Строит клавиатуру выбора типа скидки промокода (процент/сумма)."""
    builder = InlineKeyboardBuilder()
    # Первый шаг: выбрать тип скидки
    builder.button(text="Процент", callback_data="admin_promo_discount_type_percent")
    builder.button(text="Фикс (RUB)", callback_data="admin_promo_discount_type_amount")
    builder.button(text="❌ Отмена", callback_data="admin_cancel")
    builder.adjust(2, 1)
    return builder.as_markup()


def create_admin_promo_discount_percent_menu_keyboard() -> InlineKeyboardMarkup:
    """Строит клавиатуру быстрого выбора процента скидки промокода."""
    builder = InlineKeyboardBuilder()
    # Пресеты процентов
    for p in (5, 10, 15, 20, 25, 30):
        builder.button(text=f"{p}%", callback_data=f"admin_promo_discount_percent_{p}")
    # Ручной ввод обоих типов и переключение меню
    builder.button(text="✏️ Ввести процент", callback_data="admin_promo_discount_manual_percent")
    builder.button(text="✏️ Ввести фикс RUB", callback_data="admin_promo_discount_manual_amount")
    builder.button(text="↔️ Фикс-меню", callback_data="admin_promo_discount_show_amount_menu")
    builder.button(text="❌ Отмена", callback_data="admin_cancel")
    builder.adjust(3, 3, 1, 1, 1)
    return builder.as_markup()


def create_admin_promo_discount_amount_menu_keyboard() -> InlineKeyboardMarkup:
    """Строит клавиатуру быстрого выбора фиксированной суммы скидки промокода."""
    builder = InlineKeyboardBuilder()
    # Пресеты сумм в рублях
    for a in (50, 100, 150, 200, 300, 500):
        builder.button(text=f"{a} RUB", callback_data=f"admin_promo_discount_amount_{a}")
    builder.button(text="✏️ Ввести фикс RUB", callback_data="admin_promo_discount_manual_amount")
    builder.button(text="✏️ Ввести процент", callback_data="admin_promo_discount_manual_percent")
    builder.button(text="↔️ Процент-меню", callback_data="admin_promo_discount_show_percent_menu")
    builder.button(text="❌ Отмена", callback_data="admin_cancel")
    builder.adjust(3, 3, 1, 1, 1)
    return builder.as_markup()


def create_admin_promo_limits_keyboard() -> InlineKeyboardMarkup:
    """Строит клавиатуру настройки лимитов использования промокода."""
    builder = InlineKeyboardBuilder()
    # СТАРАЯ клавиатура оставлена для совместимости, но не используется в новом мастере
    builder.button(text="➡️ Пропустить", callback_data="admin_promo_limits_skip")
    builder.button(text="❌ Отмена", callback_data="admin_cancel")
    builder.adjust(1, 1)
    return builder.as_markup()


def create_admin_promo_limits_type_keyboard() -> InlineKeyboardMarkup:
    """Строит клавиатуру выбора типа лимита промокода (общий/на пользователя)."""
    builder = InlineKeyboardBuilder()
    builder.button(text="Общий лимит", callback_data="admin_promo_limits_type_total")
    builder.button(text="Лимит на пользователя", callback_data="admin_promo_limits_type_per")
    builder.button(text="Оба лимита", callback_data="admin_promo_limits_type_both")
    builder.button(text="➡️ Пропустить", callback_data="admin_promo_limits_skip")
    builder.button(text="❌ Отмена", callback_data="admin_cancel")
    builder.adjust(2, 1, 1, 1)
    return builder.as_markup()


def create_admin_promo_limits_total_keyboard() -> InlineKeyboardMarkup:
    """Строит клавиатуру быстрого выбора общего лимита использований промокода."""
    builder = InlineKeyboardBuilder()
    for n in (10, 50, 100, 200, 500, 1000):
        builder.button(text=str(n), callback_data=f"admin_promo_limits_total_preset_{n}")
    builder.button(text="✏️ Ввести значение", callback_data="admin_promo_limits_total_manual")
    builder.button(text="⬅️ Назад", callback_data="admin_promo_limits_back_to_type")
    builder.button(text="❌ Отмена", callback_data="admin_cancel")
    builder.adjust(3, 3, 1, 1)
    return builder.as_markup()


def create_admin_promo_limits_per_user_keyboard() -> InlineKeyboardMarkup:
    """Строит клавиатуру быстрого выбора лимита использований промокода на одного пользователя."""
    builder = InlineKeyboardBuilder()
    for n in (1, 2, 3, 5, 10):
        builder.button(text=str(n), callback_data=f"admin_promo_limits_per_preset_{n}")
    builder.button(text="✏️ Ввести значение", callback_data="admin_promo_limits_per_manual")
    builder.button(text="⬅️ Назад", callback_data="admin_promo_limits_back_to_type")
    builder.button(text="❌ Отмена", callback_data="admin_cancel")
    builder.adjust(3, 2, 1, 1)
    return builder.as_markup()


def create_admin_promo_dates_keyboard() -> InlineKeyboardMarkup:
    """Строит клавиатуру настройки срока действия промокода."""
    builder = InlineKeyboardBuilder()
    # Быстрые пресеты по дням
    builder.button(text="3 дня", callback_data="admin_promo_dates_days_3")
    builder.button(text="7 дней", callback_data="admin_promo_dates_days_7")
    builder.button(text="14 дней", callback_data="admin_promo_dates_days_14")
    builder.button(text="30 дней", callback_data="admin_promo_dates_days_30")
    builder.button(text="90 дней", callback_data="admin_promo_dates_days_90")
    # Альтернативы по периодам
    builder.button(text="Неделя", callback_data="admin_promo_dates_week")
    builder.button(text="Месяц", callback_data="admin_promo_dates_month")
    # Ручной ввод количества дней и пропуск
    builder.button(text="✏️ Ввести число дней", callback_data="admin_promo_dates_custom_days")
    builder.button(text="➡️ Пропустить", callback_data="admin_promo_dates_skip")
    builder.button(text="❌ Отмена", callback_data="admin_cancel")
    builder.adjust(2, 2, 1, 2, 1)
    return builder.as_markup()


def create_admin_promo_description_keyboard() -> InlineKeyboardMarkup:
    """Строит клавиатуру шага ввода описания промокода."""
    builder = InlineKeyboardBuilder()
    builder.button(text="➡️ Пропустить", callback_data="admin_promo_desc_skip")
    builder.button(text="❌ Отмена", callback_data="admin_cancel")
    builder.adjust(1)
    return builder.as_markup()


def create_admin_promo_confirm_keyboard() -> InlineKeyboardMarkup:
    """Строит клавиатуру финального подтверждения создания промокода."""
    builder = InlineKeyboardBuilder()
    builder.button(text="✅ Создать", callback_data="admin_promo_confirm_create")
    builder.button(text="❌ Отмена", callback_data="admin_cancel")
    builder.adjust(2)
    return builder.as_markup()


def create_ton_connect_keyboard(connect_url: str) -> InlineKeyboardMarkup:
    """Строит клавиатуру со ссылкой на подключение кошелька через TON Connect."""
    builder = InlineKeyboardBuilder()
    builder.button(text="🚀 Открыть кошелек", url=connect_url)
    return builder.as_markup()


def create_payment_keyboard(payment_url: str) -> InlineKeyboardMarkup:
    """Строит клавиатуру со ссылкой на оплату у внешнего платёжного провайдера."""
    builder = InlineKeyboardBuilder()
    builder.button(text=(get_setting("btn_go_to_payment") or "Перейти к оплате"), url=payment_url)
    return builder.as_markup()


def create_payment_with_check_keyboard(
    payment_url: str, check_callback: str
) -> InlineKeyboardMarkup:
    """Строит клавиатуру со ссылкой на оплату и кнопкой проверки статуса платежа."""
    builder = InlineKeyboardBuilder()
    builder.button(text=(get_setting("btn_go_to_payment") or "Перейти к оплате"), url=payment_url)
    builder.button(
        text=(get_setting("btn_check_payment") or "✅ Проверить оплату"),
        callback_data=check_callback,
    )
    builder.adjust(1)
    return builder.as_markup()


def create_topup_payment_method_keyboard(payment_methods: dict) -> InlineKeyboardMarkup:
    """Строит клавиатуру выбора способа оплаты для пополнения баланса."""
    builder = InlineKeyboardBuilder()
    # Только внешние способы оплаты, без оплаты с баланса
    if payment_methods and payment_methods.get("yookassa"):
        if get_setting("sbp_enabled"):
            builder.button(text="🏦 СБП / Банковская карта", callback_data="topup_pay_yookassa")
        else:
            builder.button(text="🏦 Банковская карта", callback_data="topup_pay_yookassa")
    if payment_methods and payment_methods.get("heleket"):
        builder.button(text="💎 Криптовалюта", callback_data="topup_pay_heleket")
    if payment_methods and payment_methods.get("cryptobot"):
        builder.button(text="🤖 CryptoBot", callback_data="topup_pay_cryptobot")
    if payment_methods and payment_methods.get("yoomoney"):
        builder.button(text="💜 ЮMoney (кошелёк)", callback_data="topup_pay_yoomoney")
    if payment_methods and payment_methods.get("stars"):
        builder.button(text="⭐ Telegram Stars", callback_data="topup_pay_stars")
    if payment_methods and payment_methods.get("tonconnect"):
        builder.button(text="🪙 TON Connect", callback_data="topup_pay_tonconnect")

    builder.button(
        text=(get_setting("btn_back_to_menu") or "⬅️ Назад в меню"),
        callback_data="show_profile",
    )
    builder.adjust(1)
    return builder.as_markup()


def create_keys_management_keyboard(keys: list) -> InlineKeyboardMarkup:
    """Строит клавиатуру списка ключей пользователя для управления ими."""
    builder = InlineKeyboardBuilder()
    if keys:
        for i, key in enumerate(keys):
            expiry_date = datetime.fromisoformat(key["expiry_date"])
            status_icon = "🟢" if expiry_date > datetime.now() else "🔴"
            host_name = key.get("host_name", "Неизвестный сервер")
            button_text = f"{status_icon} Подписка № {i+1} ({host_name}) (до {expiry_date.strftime('%d.%m.%Y')})"
            builder.button(text=button_text, callback_data=f"show_key_{key['key_id']}")
    builder.button(
        text=(get_setting("btn_buy_key") or "➕ Купить новую подписку"),
        callback_data="buy_new_key",
    )
    builder.button(
        text=(get_setting("btn_back_to_menu") or "⬅️ Назад в меню"),
        callback_data="back_to_main_menu",
    )
    builder.adjust(1)
    return builder.as_markup()


def create_key_info_keyboard(
    key_id: int, connection_string: str | None = None
) -> InlineKeyboardMarkup:
    """Строит клавиатуру карточки конкретного ключа (инструкции, продление, QR и т.д.)."""
    builder = InlineKeyboardBuilder()

    if connection_string:
        # Строка подписки уже известна — сразу делаем кнопки внешними ссылками
        # (Telegram сам добавит на такую кнопку стрелочку внешнего перехода),
        # без промежуточного callback-запроса к боту
        happ_link = (
            base64.urlsafe_b64encode(f"happ://add/{connection_string}".encode())
            .decode()
            .rstrip("=")
        )
        incy_link = (
            base64.urlsafe_b64encode(f"incy://add/{connection_string}".encode())
            .decode()
            .rstrip("=")
        )
        v2raytun_link = (
            base64.urlsafe_b64encode(f"v2raytun://import/{connection_string}".encode())
            .decode()
            .rstrip("=")
        )
        v2rayng_link = (
            base64.urlsafe_b64encode(f"v2rayng://install-config/?url={connection_string}".encode())
            .decode()
            .rstrip("=")
        )

        builder.button(
            text="🌐 Открыть Web App",
            web_app=WebAppInfo(url=connection_string),
            style="success",
        )
        builder.button(
            text=(get_setting("btn_extend_key") or "➕ Продлить эту подписку"),
            callback_data=f"extend_key_{key_id}",
            style="danger",
        )
        builder.button(text="⚡ Добавить в Happ", url=f"{REDIR_URL}{happ_link}", style="primary")
        builder.button(text="🛡️ Добавить в INCY", url=f"{REDIR_URL}{incy_link}", style="primary")
        builder.button(
            text="🚀 Добавить в v2RayTun",
            url=f"{REDIR_URL}{v2raytun_link}",
            style="primary",
        )
        builder.button(
            text="👾 Добавить в v2rayNG",
            url=f"{REDIR_URL}{v2rayng_link}",
            style="primary",
        )
    else:
        # Фолбэк на старое поведение (через callback), если строка подписки
        # на момент построения клавиатуры ещё не получена
        builder.button(
            text="🌐 Открыть Web App",
            callback_data=f"open_web_app_{key_id}",
            style="success",
        )
        builder.button(
            text=(get_setting("btn_extend_key") or "➕ Продлить эту подписку"),
            callback_data=f"extend_key_{key_id}",
            style="danger",
        )
        builder.button(
            text="⚡ Добавить в Happ",
            callback_data=f"add_to_happ_{key_id}",
            style="primary",
        )
        builder.button(
            text="🛡️ Добавить в INCY",
            callback_data=f"add_to_incy_{key_id}",
            style="primary",
        )
        builder.button(
            text="🚀 Добавить в v2RayTun",
            callback_data=f"add_to_v2raytun_{key_id}",
            style="primary",
        )
        builder.button(
            text="👾 Добавить в v2rayNG",
            callback_data=f"add_to_v2rayng_{key_id}",
            style="primary",
        )
    builder.button(
        text=(get_setting("btn_show_qr") or "📱 Показать QR-код"),
        callback_data=f"show_qr_{key_id}",
    )
    builder.button(
        text=(get_setting("btn_instruction") or "📖 Инструкция"),
        callback_data=f"howto_vless_{key_id}",
    )
    builder.button(
        text=(get_setting("btn_switch_server") or "🌍 Сменить сервер"),
        callback_data=f"switch_server_{key_id}",
    )
    builder.button(
        text="🔄 Пересоздать",
        callback_data=f"regen_key_prompt_{key_id}",
        style="danger",
    )
    builder.button(text="🛜 Файлы для XKeen", callback_data=f"download_xkeen_outbounds_{key_id}")
    builder.button(text="🪏 Ключи для Podkop", callback_data=f"show_podkop_keys_{key_id}")
    builder.button(text="🔐 Показать все ключи", callback_data=f"show_raw_keys_{key_id}")

    # Тумблер автопродления с баланса — только для ключей, у которых уже
    # есть привязанный тариф (last_plan_id, проставляется при первой
    # покупке/продлении). У триальных ключей его нет — предлагать тумблер
    # там бессмысленно, включать нечего и продлевать не по чему.
    key_data = get_key_by_id(key_id)
    if key_data and key_data.get("last_plan_id") is not None:
        auto_renew_on = bool(key_data.get("auto_renew_enabled"))
        builder.button(
            text=("💳 Автопродление: ✅" if auto_renew_on else "💳 Автопродление: 🚫"),
            callback_data=f"toggle_auto_renew_{key_id}",
            style=("success" if auto_renew_on else "danger"),
        )

    # Кнопку "Назад" добавляем последней
    builder.button(
        text=(get_setting("btn_back_to_keys") or "⬅️ Назад к списку подписок"),
        callback_data="manage_keys",
    )

    # --- Динамическое построение сетки ---
    # Считаем информационные кнопки (все, кроме "Назад")
    main_buttons_count = len(list(builder.buttons)) - 1

    layout = []

    # Все основные кнопки группируем по 2 в ряд
    for _ in range(main_buttons_count // 2):
        layout.append(2)

    # Если кнопок вдруг станет нечетное количество,
    # оставшаяся одна кнопка займет целую строку перед "Назад"
    if main_buttons_count % 2 != 0:
        layout.append(1)

    # Кнопка "Назад" всегда одна на всю ширину
    layout.append(1)

    builder.adjust(*layout)
    return builder.as_markup()


def create_howto_vless_keyboard() -> InlineKeyboardMarkup:
    """Строит клавиатуру выбора платформы для общей инструкции по подключению VLESS."""
    builder = InlineKeyboardBuilder()
    builder.button(
        text=(get_setting("btn_howto_android") or "👾 Android"),
        callback_data="howto_android",
    )
    builder.button(text=(get_setting("btn_howto_ios") or "🍏 iOS"), callback_data="howto_ios")
    builder.button(
        text=(get_setting("btn_howto_windows") or "💻 Windows"),
        callback_data="howto_windows",
    )
    builder.button(text=(get_setting("btn_howto_linux") or "🐧 Linux"), callback_data="howto_linux")
    builder.button(
        text=(get_setting("btn_back_to_menu") or "⬅️ Назад в меню"),
        callback_data="back_to_main_menu",
    )
    builder.adjust(2, 2, 1)
    return builder.as_markup()


def create_howto_vless_keyboard_key(key_id: int) -> InlineKeyboardMarkup:
    """Строит клавиатуру выбора платформы для инструкции по подключению конкретного ключа."""
    builder = InlineKeyboardBuilder()
    builder.button(
        text=(get_setting("btn_howto_android") or "👾 Android"),
        callback_data="howto_android",
    )
    builder.button(text=(get_setting("btn_howto_ios") or "🍏 iOS"), callback_data="howto_ios")
    builder.button(
        text=(get_setting("btn_howto_windows") or "💻 Windows"),
        callback_data="howto_windows",
    )
    builder.button(text=(get_setting("btn_howto_linux") or "🐧 Linux"), callback_data="howto_linux")
    builder.button(
        text=(get_setting("btn_back_to_key") or "⬅️ Назад к подписке"),
        callback_data=f"show_key_{key_id}",
    )
    builder.adjust(2, 2, 1)
    return builder.as_markup()


def create_back_to_menu_keyboard() -> InlineKeyboardMarkup:
    """Строит клавиатуру с кнопкой возврата в предыдущее меню."""
    builder = InlineKeyboardBuilder()
    builder.button(
        text=(get_setting("btn_back_to_menu") or "⬅️ Назад в меню"),
        callback_data="back_to_main_menu",
    )
    return builder.as_markup()


def create_profile_keyboard() -> InlineKeyboardMarkup:
    """Строит клавиатуру раздела профиля пользователя."""
    kb = _build_keyboard_from_db("profile_menu")
    if kb:
        return kb

    builder = InlineKeyboardBuilder()
    builder.button(
        text=(get_setting("btn_top_up") or "➕ Пополнить баланс"),
        callback_data="top_up_start",
    )
    builder.button(
        text=(get_setting("btn_referral") or "🤝 Реферальная программа"),
        callback_data="show_referral_program",
    )
    builder.button(
        text=(get_setting("btn_tx_history") or "📜 История операций"),
        callback_data="tx_history_0",
    )
    builder.button(
        text=(get_setting("btn_back_to_menu") or "⬅️ Назад в меню"),
        callback_data="back_to_main_menu",
    )
    builder.adjust(1)
    return builder.as_markup()


def create_transaction_history_keyboard(
    page: int, has_prev: bool, has_next: bool
) -> InlineKeyboardMarkup:
    """Строит клавиатуру постраничной истории операций пользователя."""
    builder = InlineKeyboardBuilder()
    if has_prev:
        builder.button(text="⬅️ Назад", callback_data=f"tx_history_{page-1}")
    if has_next:
        builder.button(text="Вперёд ➡️", callback_data=f"tx_history_{page+1}")
    builder.button(
        text=(get_setting("btn_back_to_menu") or "⬅️ В профиль"),
        callback_data="show_profile",
    )
    if has_prev and has_next:
        builder.adjust(2, 1)
    elif has_prev or has_next:
        builder.adjust(1, 1)
    else:
        builder.adjust(1)
    return builder.as_markup()


def create_welcome_keyboard(
    channel_url: str | None, is_subscription_forced: bool = False
) -> InlineKeyboardMarkup:
    """Строит клавиатуру приветственного экрана (подписка на канал, согласие с условиями)."""
    builder = InlineKeyboardBuilder()

    if channel_url and is_subscription_forced:
        builder.button(text="📢 Перейти в канал", url=channel_url)
        builder.button(text="✅ Я подписался", callback_data="check_subscription_and_agree")
    elif channel_url:
        builder.button(text="📢 Наш канал (не обязательно)", url=channel_url)
        builder.button(text="✅ Принимаю условия", callback_data="check_subscription_and_agree")
    else:
        builder.button(text="✅ Принимаю условия", callback_data="check_subscription_and_agree")

    builder.adjust(1)
    return builder.as_markup()


def get_main_menu_button() -> InlineKeyboardButton:
    """Возвращает готовую кнопку 'В главное меню' для переиспользования в разных клавиатурах."""
    return InlineKeyboardButton(text="🏠 В главное меню", callback_data="show_main_menu")


def get_buy_button() -> InlineKeyboardButton:
    """Возвращает готовую кнопку 'Купить подписку' для переиспользования в разных клавиатурах."""
    return InlineKeyboardButton(text="💳 Купить подписку", callback_data="buy_vpn")


def create_admin_users_pick_keyboard(
    users: list[dict], page: int = 0, page_size: int = 10, action: str = "gift"
) -> InlineKeyboardMarkup:
    """Строит клавиатуру постраничного выбора пользователя админом (для подарка и т.п.)."""
    builder = InlineKeyboardBuilder()
    start = page * page_size
    end = start + page_size
    for u in users[start:end]:
        user_id = u.get("telegram_id") or u.get("user_id") or u.get("id")
        title = f"{user_id} ▪️ {format_stored_username(u.get('username'), linked=False, escape_html=False)}"
        builder.button(text=title, callback_data=f"admin_{action}_pick_user_{user_id}")
    total = len(users)
    have_prev = page > 0
    have_next = end < total
    if have_prev:
        builder.button(text="⬅️ Назад", callback_data=f"admin_{action}_pick_user_page_{page-1}")
    if have_next:
        builder.button(text="Вперёд ➡️", callback_data=f"admin_{action}_pick_user_page_{page+1}")
    builder.button(text="⬅️ В админ-меню", callback_data="admin_menu")
    rows = [1] * len(users[start:end])
    tail = []
    if have_prev or have_next:
        tail.append(2 if (have_prev and have_next) else 1)
    tail.append(1)
    builder.adjust(*(rows + tail if rows else ([2] if (have_prev or have_next) else []) + [1]))
    return builder.as_markup()


def create_admin_hosts_pick_keyboard(
    hosts: list[dict], action: str = "gift"
) -> InlineKeyboardMarkup:
    """Строит клавиатуру выбора хоста админом."""
    builder = InlineKeyboardBuilder()
    if hosts:
        for h in hosts:
            name = h.get("host_name")
            token = encode_host_callback_token(name)
            if action == "speedtest":
                # Две кнопки в строке: запуск теста и автоустановка
                builder.button(text=name, callback_data=f"admin_{action}_pick_host_{token}")
                builder.button(
                    text="🛠 Автоустановка",
                    callback_data=f"admin_speedtest_autoinstall_{token}",
                )
            else:
                builder.button(text=name, callback_data=f"admin_{action}_pick_host_{token}")
    else:
        builder.button(text="Хостов нет", callback_data="noop")
    # Дополнительные опции для speedtest
    if action == "speedtest":
        builder.button(text="🚀 Запустить для всех", callback_data="admin_speedtest_run_all")
    builder.button(text="⬅️ Назад", callback_data=f"admin_{action}_back_to_users")
    # Сетка: по 2 в ряд для speedtest (сервер + автоустановка), иначе по 1
    if action == "speedtest":
        rows = [2] * (len(hosts) if hosts else 1)
        tail = [1, 1]
    else:
        rows = [1] * (len(hosts) if hosts else 1)
        tail = [1]
    builder.adjust(*(rows + tail))
    return builder.as_markup()


def create_admin_keys_for_host_keyboard(
    host_name: str,
    keys: list[dict],
    page: int = 0,
    page_size: int = 20,
) -> InlineKeyboardMarkup:
    """Строит клавиатуру постраничного списка ключей на конкретном хосте для админа."""
    builder = InlineKeyboardBuilder()
    # Если ключей нет — показываем заглушку и кнопки назад
    if not keys:
        builder.button(text="Подписок на хосте нет", callback_data="noop")
        builder.button(text="⬅️ К выбору хоста", callback_data="admin_hostkeys_back_to_hosts")
        builder.button(text="⬅️ В админ-меню", callback_data="admin_menu")
        builder.adjust(1)
        return builder.as_markup()

    # Пагинация
    start = page * page_size
    end = start + page_size
    for k in keys[start:end]:
        kid = k.get("key_id")
        email = k.get("key_email") or "—"
        expiry_raw = k.get("expiry_date")
        # expiry_date хранится через datetime.fromtimestamp(ms/1000), сырое
        # TEXT-значение из SQLite почти всегда содержит микросекунды —
        # обрезаем их, в тексте кнопки они не нужны.
        if expiry_raw:
            try:
                expiry = datetime.fromisoformat(str(expiry_raw)).strftime("%d.%m.%Y %H:%M")
            except ValueError:
                expiry = str(expiry_raw)
        else:
            expiry = "—"
        title = f"№ {kid} ▪️ {email[:24]} ▪️ до {expiry}"
        builder.button(text=title, callback_data=f"admin_edit_key_{kid}")

    total = len(keys)
    have_prev = page > 0
    have_next = end < total
    if have_prev:
        builder.button(text="⬅️ Назад", callback_data=f"admin_hostkeys_page_{page-1}")
    if have_next:
        builder.button(text="Вперёд ➡️", callback_data=f"admin_hostkeys_page_{page+1}")

    # Кнопки навигации
    builder.button(text="⬅️ К выбору хоста", callback_data="admin_hostkeys_back_to_hosts")
    builder.button(text="⬅️ В админ-меню", callback_data="admin_menu")

    # Сетка: список (по 1 в ряд) + пагинация (1 или 2 в ряд) + две кнопки назад
    rows = [1] * len(keys[start:end])
    tail = []
    if have_prev or have_next:
        tail.append(2 if (have_prev and have_next) else 1)
    tail.extend([1, 1])
    builder.adjust(*(rows + tail if rows else ([2] if (have_prev or have_next) else []) + [1, 1]))
    return builder.as_markup()


def create_admin_months_pick_keyboard(action: str = "gift") -> InlineKeyboardMarkup:
    """Строит клавиатуру быстрого выбора количества месяцев (для выдачи/продления)."""
    builder = InlineKeyboardBuilder()
    for m in (1, 3, 6, 12):
        builder.button(text=f"{m} мес.", callback_data=f"admin_{action}_pick_months_{m}")
    builder.button(text="⬅️ Назад", callback_data=f"admin_{action}_back_to_hosts")
    builder.adjust(2, 2, 1)
    return builder.as_markup()


def create_topup_amount_keyboard() -> InlineKeyboardMarkup:
    """Быстрый выбор типовой суммы пополнения + возможность ввести свою текстом."""
    builder = InlineKeyboardBuilder()
    amounts = [100, 300, 500, 1000, 2000, 5000]
    for amount in amounts:
        builder.button(text=f"{amount} ₽", callback_data=f"topup_quick_{amount}")
    builder.button(text="⬅️ Назад в меню", callback_data="back_to_main_menu")
    builder.adjust(3, 3, 1)
    return builder.as_markup()


def create_back_to_main_menu_keyboard() -> InlineKeyboardMarkup:
    """Создать клавиатуру с кнопкой возврата в главное меню."""
    builder = InlineKeyboardBuilder()
    builder.button(text="⬅️ Назад в меню", callback_data="back_to_main_menu")
    builder.adjust(1)
    return builder.as_markup()
