"""Проблемные платежи: оплата получена, но выдача не удалась.

Запись создаётся в process_successful_payment (handlers.py) при сбое выдачи. Админам уходит
сообщение с кнопками «Повторить выдачу» и «Вернуть на баланс», в админ-меню появляется раздел
со списком открытых случаев.
"""

import asyncio
import html
import json
import logging
import uuid

from aiogram import Bot, F, types
from aiogram.fsm.context import FSMContext
from aiogram.utils.keyboard import InlineKeyboardBuilder

from shop_bot.bot.admin_notify import with_hide_button
from shop_bot.data_manager.database import (
    add_to_balance,
    count_failed_payments,
    get_admin_ids,
    get_failed_payment,
    get_user,
    list_failed_payments,
    log_transaction,
    record_failed_payment,
    reopen_failed_payment,
    resolve_failed_payment,
)

logger = logging.getLogger(__name__)

ACTION_LABELS = {
    "new": "Новая подписка",
    "extend": "Продление подписки",
    "top_up": "Пополнение баланса",
    "gift": "Подарочная подписка",
}

# Служебные ключи metadata: не должны попадать в повторную выдачу как есть
_SERVICE_KEYS = ("_failed_id", "chat_id", "message_id")


def _action_label(action) -> str:
    action = str(action or "")
    return ACTION_LABELS.get(action, html.escape(action or "?"))


def _user_markup(user_id: int, extra_rows: list | None = None):
    kb = InlineKeyboardBuilder()
    for text, cb in extra_rows or []:
        kb.button(text=text, callback_data=cb)
    kb.button(text="👤 Перейти к пользователю", callback_data=f"admin_view_user_{int(user_id)}")
    return kb


def render_failed_payment(f: dict, note: str | None = None) -> str:
    ext = f.get("external_id")
    payment = f"<code>{html.escape(str(ext))}</code>" if ext else "—"
    text = (
        "❌ <b>Проблемный платёж:</b> оплата получена, но выдача не получилась\n\n"
        f"💳 <b>Платёж:</b> {payment}\n"
        f"👤 <b>Пользователь:</b> <code>{int(f['user_id'])}</code>\n"
        f"💰 <b>Сумма:</b> {float(f['amount']):.2f} ₽\n"
        f"⚙️ <b>Действие:</b> {_action_label(f.get('action'))}\n\n"
        f"🛑 <b>Ошибка:</b> <code>{html.escape(str(f.get('error') or 'неизвестно')[:300])}</code>"
    )
    if note:
        text += f"\n\n{note}"
    return text


def failed_payment_keyboard(f: dict):
    """Кнопки зависят от статуса: у закрытого случая остаётся только переход к пользователю."""
    if f.get("status") == "open":
        kb = _user_markup(
            f["user_id"],
            [
                ("🔁 Повторить выдачу", f"admin_fp_retry_{f['id']}"),
                ("💰 Вернуть на баланс", f"admin_fp_refund_{f['id']}"),
            ],
        )
        kb.adjust(2, 1)
    else:
        kb = _user_markup(f["user_id"])
        kb.adjust(1)
    return kb.as_markup()


async def _notify_admins(bot: Bot, text: str, markup=None) -> None:
    try:
        admin_ids = await asyncio.to_thread(get_admin_ids)
    except Exception:
        admin_ids = set()
    for admin_id in admin_ids:
        try:
            await bot.send_message(
                chat_id=admin_id,
                text=text,
                parse_mode="HTML",
                reply_markup=with_hide_button(markup),
            )
        except Exception as e:
            logger.error(f"Проблемный платёж: не удалось уведомить админа {admin_id}: {e}")


async def report_failed_payment(bot: Bot, metadata: dict, error: str) -> int | None:
    """Записывает проблемный платёж и сообщает админам (один раз на случай).
    Пользователю текст отправляет вызывающий код. Возвращает id записи."""
    try:
        user_id = int(metadata.get("user_id"))
        amount = float(metadata.get("price"))
    except (TypeError, ValueError):
        return None

    clean = {k: v for k, v in metadata.items() if k not in _SERVICE_KEYS}
    failed_id, created = await asyncio.to_thread(
        record_failed_payment,
        metadata.get("_provider"),
        metadata.get("_external_id"),
        user_id,
        amount,
        metadata.get("action"),
        clean,
        error,
    )
    if failed_id is None:
        return None
    metadata["_failed_id"] = failed_id
    if created:
        record = await asyncio.to_thread(get_failed_payment, failed_id)
        if record:
            await _notify_admins(bot, render_failed_payment(record), failed_payment_keyboard(record))
    return failed_id


# ---------------------------------------------------------------------------
# Действия администратора
# ---------------------------------------------------------------------------
async def refund_to_balance(bot: Bot, failed_id: int, admin_id: int) -> tuple[bool, str]:
    """Зачисляет сумму проблемного платежа на баланс пользователя. (успех, пояснение)."""
    f = await asyncio.to_thread(get_failed_payment, failed_id)
    if not f:
        return False, "Запись не найдена"
    if not await asyncio.to_thread(resolve_failed_payment, failed_id, "refunded", admin_id):
        return False, "Платёж уже обработан"

    user_id = int(f["user_id"])
    amount = float(f["amount"])
    try:
        ok = await asyncio.to_thread(add_to_balance, user_id, amount)
    except Exception as e:
        logger.error(f"Возврат на баланс по проблемному платежу {failed_id}: {e}", exc_info=True)
        ok = False
    if not ok:
        await asyncio.to_thread(reopen_failed_payment, failed_id)
        return False, "Не удалось зачислить деньги на баланс"

    try:
        user_info = await asyncio.to_thread(get_user, user_id)
        await asyncio.to_thread(
            lambda: log_transaction(
                username=user_info.get("username", "N/A") if user_info else "N/A",
                transaction_id=None,
                payment_id=str(uuid.uuid4()),
                user_id=user_id,
                status="internal",
                amount_rub=amount,
                amount_currency=None,
                currency_name=None,
                payment_method="Refund",
                metadata=json.dumps(
                    {"action": "failed_payment_refund", "failed_id": failed_id}, ensure_ascii=False
                ),
            )
        )
    except Exception as e:
        logger.warning(f"Не удалось записать возврат {failed_id} в историю: {e}")

    try:
        kb = InlineKeyboardBuilder()
        kb.button(text="💳 Купить подписку", callback_data="buy_new_key")
        await bot.send_message(
            chat_id=user_id,
            text=(
                "💰 <b>Деньги возвращены на баланс</b>\n\n"
                f"Оплата {amount:.2f} ₽ не привела к выдаче подписки, "
                "поэтому сумма зачислена на ваш баланс"
            ),
            reply_markup=kb.as_markup(),
            parse_mode="HTML",
        )
    except Exception as e:
        logger.warning(f"Не удалось уведомить пользователя {user_id} о возврате: {e}")
    return True, "Деньги возвращены на баланс"


async def retry_issue(bot: Bot, failed_id: int, admin_id: int) -> tuple[bool, str]:
    """Повторяет выдачу по сохранённым данным платежа. (успех, пояснение)."""
    from shop_bot.bot.handlers import process_successful_payment

    f = await asyncio.to_thread(get_failed_payment, failed_id)
    if not f:
        return False, "Запись не найдена"
    if not await asyncio.to_thread(resolve_failed_payment, failed_id, "retried", admin_id):
        return False, "Платёж уже обработан"

    try:
        metadata = json.loads(f.get("metadata") or "{}")
    except ValueError:
        metadata = {}
    for key in _SERVICE_KEYS:
        metadata.pop(key, None)
    if not metadata.get("user_id") or metadata.get("price") is None:
        await asyncio.to_thread(reopen_failed_payment, failed_id)
        return False, "В записи нет данных для повторной выдачи"

    await process_successful_payment(bot, metadata)
    new_id = metadata.get("_failed_id")
    if new_id:
        return False, f"Повтор не удался, создан новый случай #{new_id}"
    return True, "Повторная выдача выполнена"


# ---------------------------------------------------------------------------
# Админ-раздел
# ---------------------------------------------------------------------------
def _list_text_and_markup(items: list[dict]):
    kb = InlineKeyboardBuilder()
    if not items:
        text = "⚠️ <b>Проблемные платежи</b>\n\nОткрытых случаев нет"
    else:
        text = f"⚠️ <b>Проблемные платежи:</b> {len(items)}\n\nВыберите случай"
        for f in items:
            kb.button(
                text=f"#{f['id']} · {float(f['amount']):.0f} ₽ · {f['user_id']}",
                callback_data=f"admin_fp_view_{f['id']}",
            )
    kb.button(text="⬅️ Назад", callback_data="admin_grp_finance")
    kb.adjust(1)
    return text, kb.as_markup()


def register_admin_handlers(admin_router) -> None:
    """Подключает колбэки раздела «Проблемные платежи» к админскому роутеру."""
    from shop_bot.bot.handlers import safe_edit_text

    @admin_router.callback_query(F.data == "admin_fp_list")
    async def fp_list(callback: types.CallbackQuery, state: FSMContext):
        await callback.answer()
        await state.clear()
        items = await asyncio.to_thread(list_failed_payments, "open", 20)
        text, markup = _list_text_and_markup(items)
        await safe_edit_text(callback, text, reply_markup=markup, parse_mode="HTML")

    @admin_router.callback_query(F.data.startswith("admin_fp_view_"))
    async def fp_view(callback: types.CallbackQuery):
        await callback.answer()
        f = await asyncio.to_thread(get_failed_payment, int(callback.data.rsplit("_", 1)[1]))
        if not f:
            await callback.answer("Запись не найдена", show_alert=True)
            return
        await safe_edit_text(
            callback,
            render_failed_payment(f, _status_note(f)),
            reply_markup=failed_payment_keyboard(f),
            parse_mode="HTML",
        )

    @admin_router.callback_query(F.data.regexp(r"^admin_fp_retry_\d+$"))
    async def fp_retry_ask(callback: types.CallbackQuery):
        await callback.answer()
        failed_id = int(callback.data.rsplit("_", 1)[1])
        f = await asyncio.to_thread(get_failed_payment, failed_id)
        if not f or f["status"] != "open":
            await callback.answer("Платёж уже обработан", show_alert=True)
            return
        kb = InlineKeyboardBuilder()
        kb.button(text="✅ Да, повторить", callback_data=f"admin_fp_retry_ok_{failed_id}")
        kb.button(text="❌ Отмена", callback_data=f"admin_fp_view_{failed_id}")
        kb.adjust(1)
        await safe_edit_text(
            callback,
            render_failed_payment(
                f,
                "🔁 <b>Повторить выдачу?</b>\n"
                "Если ключ уже частично создан на панели, повтор может создать дубль. "
                "Проверьте пользователя перед повтором",
            ),
            reply_markup=kb.as_markup(),
            parse_mode="HTML",
        )

    @admin_router.callback_query(F.data.startswith("admin_fp_retry_ok_"))
    async def fp_retry_do(callback: types.CallbackQuery):
        failed_id = int(callback.data.rsplit("_", 1)[1])
        await callback.answer("⏳ Повторяю выдачу...")
        ok, info = await retry_issue(callback.bot, failed_id, callback.from_user.id)
        await _finish(callback, failed_id, ok, info, safe_edit_text)

    @admin_router.callback_query(F.data.regexp(r"^admin_fp_refund_\d+$"))
    async def fp_refund_do(callback: types.CallbackQuery):
        failed_id = int(callback.data.rsplit("_", 1)[1])
        await callback.answer("⏳ Возвращаю на баланс...")
        ok, info = await refund_to_balance(callback.bot, failed_id, callback.from_user.id)
        await _finish(callback, failed_id, ok, info, safe_edit_text)


def _status_note(f: dict) -> str | None:
    status = f.get("status")
    who = f"<code>{f['resolved_by']}</code>" if f.get("resolved_by") else "—"
    if status == "refunded":
        return f"💰 <b>Решение:</b> деньги возвращены на баланс (админ {who})"
    if status == "retried":
        return f"🔁 <b>Решение:</b> повторная выдача (админ {who})"
    return None


async def _finish(callback, failed_id: int, ok: bool, info: str, safe_edit_text) -> None:
    f = await asyncio.to_thread(get_failed_payment, failed_id)
    if not f:
        return
    note = _status_note(f) if ok else f"⚠️ {html.escape(info)}"
    if ok and f["status"] == "retried":
        note = f"✅ <b>Решение:</b> повторная выдача выполнена (админ <code>{callback.from_user.id}</code>)"
    elif not ok and f["status"] == "retried":
        note = f"⚠️ <b>Результат:</b> {html.escape(info)}"
    await safe_edit_text(
        callback,
        render_failed_payment(f, note),
        reply_markup=failed_payment_keyboard(f),
        parse_mode="HTML",
    )


def menu_count() -> int:
    return count_failed_payments("open")
