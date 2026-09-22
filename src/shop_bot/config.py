"""Текстовые шаблоны сообщений и функции их форматирования для Telegram-бота."""

from aiogram import html

CHOOSE_PLAN_MESSAGE = "📦 <b>Выберите подходящий тариф:</b>"
CHOOSE_PAYMENT_METHOD_MESSAGE = "💳 <b>Выберите удобный способ оплаты:</b>"
VPN_INACTIVE_TEXT = "❌ <b>Статус подписки:</b> Неактивна (срок истек)"
VPN_NO_DATA_TEXT = "ℹ️ <b>Статус подписки:</b> У вас пока нет активных подписок."


def get_profile_text(username, total_spent, total_months, vpn_status_text):
    """Форматирует текст карточки профиля пользователя (юзернейм, траты, статус VPN)."""
    return (
        f"👤 <b>Профиль:</b> {username}\n\n"
        f"💰 <b>Потрачено всего:</b> {total_spent:.0f} ₽\n"
        f"📅 <b>Приобретено месяцев:</b> {total_months}\n\n"
        f"{vpn_status_text}"
    )


def get_vpn_active_text(days_left, hours_left):
    """Форматирует текст статуса активной подписки с оставшимся временем."""
    return (
        f"✅ <b>Статус подписки:</b> Активна\n"
        f"⏳ <b>Осталось:</b> {days_left} д. {hours_left} ч."
    )


def get_key_info_text(key_number, expiry_date, created_date, connection_string):
    """Форматирует текст карточки конкретного ключа (даты, ссылки подписки)."""
    expiry_formatted = expiry_date.strftime("%d.%m.%Y %H:%M")
    created_formatted = created_date.strftime("%d.%m.%Y в %H:%M")

    return (
        f"🔑 <b>Информация о подписке № {key_number}</b>\n\n"
        f"➕ <b>Приобретена:</b> {created_formatted}\n"
        f"⏳ <b>Действительна до:</b> {expiry_formatted}\n\n"
        f"🔗 <b>Основная подписка:</b>\n{html.code(connection_string)}\n"
        f"▪️🇷🇺 Российский интернет ➡️ напрямую\n"
        f"▪️🌍 Зарубежный интернет ➡️ через прокси\n"
        f"▪️⭐️ Рекомендуем для Happ и INCY\n\n"
        f"🪽 <b>Подписка для Clash:</b>\n{html.code(connection_string.replace('/sub/', '/clash/'))}\n"
        f"▪️🇷🇺 Российский интернет ➡️ напрямую\n"
        f"▪️🌍 Зарубежный интернет ➡️ через прокси\n"
        f"▪️⭐️ Рекомендуем для FlClashX и FlClash\n\n"
        f"🔀 <b>Альтернативная подписка:</b>\n{html.code(connection_string.replace('/sub/', '/json/'))}\n"
        f"▪️🌍 Весь интернет ➡️ через прокси\n"
        f"▪️🥷 Надежно перенаправляем российский трафик на свои серверы\n"
        f"▪️⭐️ Рекомендуем для v2RayTun и других устаревших клиентов\n"
    )


def get_purchase_success_text(action: str, key_number: int, expiry_date, connection_string: str):
    """Форматирует текст успешной покупки/продления ключа со ссылками подписки."""
    action_text = "обновлена" if action == "extend" else "готова"
    expiry_formatted = expiry_date.strftime("%d.%m.%Y %H:%M")

    return (
        f"🎉 <b>Ваша подписка № {key_number} {action_text}!</b>\n\n"
        f"⏳ <b>Она будет действовать до:</b> {expiry_formatted}\n\n"
        f"🔗 <b>Основная подписка:</b>\n{html.code(connection_string)}\n"
        f"▪️🇷🇺 Российский интернет ➡️ напрямую\n"
        f"▪️🌍 Зарубежный интернет ➡️ через прокси\n"
        f"▪️⭐️ Рекомендуем для Happ и INCY\n\n"
        f"🪽 <b>Подписка для Clash:</b>\n{html.code(connection_string.replace('/sub/', '/clash/'))}\n"
        f"▪️🇷🇺 Российский интернет ➡️ напрямую\n"
        f"▪️🌍 Зарубежный интернет ➡️ через прокси\n"
        f"▪️⭐️ Рекомендуем для FlClashX и FlClash\n\n"
        f"🔀 <b>Альтернативная подписка:</b>\n{html.code(connection_string.replace('/sub/', '/json/'))}\n"
        f"▪️🌍 Весь интернет ➡️ через прокси\n"
        f"▪️🥷 Надежно перенаправляем российский трафик на свои серверы\n"
        f"▪️⭐️ Рекомендуем для v2RayTun и других устаревших клиентов\n"
    )
