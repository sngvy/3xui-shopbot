"""Общие кнопки служебных уведомлений админу: «❌ Скрыть» и действия над алертами о трафике."""

from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup

HIDE_CALLBACK = "admin_hide"
HIDE_TEXT = "❌ Скрыть"
MUTE_TEXT = "🔕 Заглушить на 1 час"
DISABLE_TEXT = "⛔ Отключить подписку"


def _is_uuid(value) -> bool:
    s = str(value or "")
    return len(s) == 36 and s.count("-") == 4


def with_hide_button(markup: InlineKeyboardMarkup | None = None) -> InlineKeyboardMarkup:
    """Добавляет последним рядом кнопку «❌ Скрыть» (удаляет сообщение). Повторно не добавляет."""
    rows = [list(r) for r in markup.inline_keyboard] if markup else []
    if any(b.callback_data == HIDE_CALLBACK for r in rows for b in r):
        return markup  # type: ignore[return-value]
    rows.append([InlineKeyboardButton(text=HIDE_TEXT, callback_data=HIDE_CALLBACK)])
    return InlineKeyboardMarkup(inline_keyboard=rows)


def traffic_alert_markup(
    mute_key: str | None, c_uuid, *, can_disable: bool
) -> InlineKeyboardMarkup:
    """Клавиатура алерта о трафике/шеринге.

    mute_key — ключ заглушки (только для «Аномального трафика», у шеринга None);
    can_disable — показывать «⛔ Отключить подписку» (аномалия при множестве активных
    подключений или подозрение на шеринг). Кнопка требует настоящий UUID клиента.
    """
    rows = []
    if mute_key:
        rows.append([InlineKeyboardButton(text=MUTE_TEXT, callback_data=f"admin_tr_mute_{mute_key}")])
    if can_disable and _is_uuid(c_uuid):
        rows.append(
            [InlineKeyboardButton(text=DISABLE_TEXT, callback_data=f"admin_tr_off_{c_uuid}")]
        )
    rows.append([InlineKeyboardButton(text=HIDE_TEXT, callback_data=HIDE_CALLBACK)])
    return InlineKeyboardMarkup(inline_keyboard=rows)


def disable_confirm_markup(c_uuid) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text="✅ Да, отключить", callback_data=f"admin_tr_off_ok_{c_uuid}")],
            [InlineKeyboardButton(text="↩️ Отмена", callback_data=f"admin_tr_off_no_{c_uuid}")],
        ]
    )
