"""Обработчики Telegram-команд и колбэков бота поддержки (тикеты, форум-треды)."""

import logging

from aiogram import Bot, F, Router, types
from aiogram.enums import ChatMemberStatus
from aiogram.exceptions import TelegramBadRequest
from aiogram.filters import CommandStart
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup

from shop_bot.data_manager.database import (
    add_support_message,
    ban_user,
    create_support_ticket,
    delete_ticket,
    get_admin_ids,
    get_setting,
    get_ticket,
    get_ticket_by_thread,
    get_ticket_messages,
    get_user,
    get_user_tickets,
    is_admin,
    set_ticket_status,
    unban_user,
    update_ticket_subject,
    update_ticket_thread_info,
)

logger = logging.getLogger(__name__)


class SupportDialog(StatesGroup):
    """Состояния FSM для диалога пользователя с поддержкой."""

    waiting_for_subject = State()
    waiting_for_message = State()
    waiting_for_reply = State()


class AdminDialog(StatesGroup):
    """Состояния FSM для диалога администратора внутри тикета поддержки."""

    waiting_for_note = State()


def get_support_router() -> Router:
    """Собирает и возвращает Router со всеми обработчиками бота поддержки."""
    router = Router()

    def _user_main_reply_kb() -> types.ReplyKeyboardMarkup:
        return types.ReplyKeyboardMarkup(
            keyboard=[
                [types.KeyboardButton(text="✍️ Новое обращение")],
                [types.KeyboardButton(text="📨 Мои обращения")],
            ],
            resize_keyboard=True,
        )

    def _get_latest_open_ticket(user_id: int) -> dict | None:
        try:
            tickets = get_user_tickets(user_id) or []
            open_tickets = [t for t in tickets if t.get("status") == "open"]
            if not open_tickets:
                return None
            return max(open_tickets, key=lambda t: int(t["ticket_id"]))
        except Exception:
            return None

    def _admin_actions_kb(ticket_id: int) -> types.InlineKeyboardMarkup:
        try:
            t = get_ticket(ticket_id)
            status = (t and t.get("status")) or "open"
        except Exception:
            status = "open"
        user_id: int | None = None
        is_banned: bool = False
        if t and t.get("user_id") is not None:
            try:
                user_id = int(t.get("user_id"))
                user_data = get_user(user_id) or {}
                is_banned = bool(user_data.get("is_banned"))
            except Exception:
                user_id = None
                is_banned = False
        first_row: list[types.InlineKeyboardButton] = []
        if status == "open":
            first_row.append(
                types.InlineKeyboardButton(
                    text="✅ Закрыть", callback_data=f"admin_close_{ticket_id}"
                )
            )
        else:
            first_row.append(
                types.InlineKeyboardButton(
                    text="🔓 Переоткрыть", callback_data=f"admin_reopen_{ticket_id}"
                )
            )
        inline_kb: list[list[types.InlineKeyboardButton]] = [
            first_row,
            [
                types.InlineKeyboardButton(
                    text="🗑 Удалить", callback_data=f"admin_delete_{ticket_id}"
                )
            ],
            [
                types.InlineKeyboardButton(
                    text="⭐ Важно", callback_data=f"admin_star_{ticket_id}"
                ),
                types.InlineKeyboardButton(
                    text="👤 Пользователь", callback_data=f"admin_user_{ticket_id}"
                ),
                types.InlineKeyboardButton(
                    text="📝 Заметка", callback_data=f"admin_note_{ticket_id}"
                ),
            ],
            [
                types.InlineKeyboardButton(
                    text="🗒 Заметки", callback_data=f"admin_notes_{ticket_id}"
                )
            ],
        ]
        if user_id is not None:
            toggle_label = "✅ Разбанить пользователя" if is_banned else "🚫 Забанить пользователя"
            inline_kb.append(
                [
                    types.InlineKeyboardButton(
                        text=toggle_label, callback_data=f"admin_toggle_ban_{ticket_id}"
                    )
                ]
            )
        return types.InlineKeyboardMarkup(inline_keyboard=inline_kb)

    async def _is_admin(bot: Bot, chat_id: int, user_id: int) -> bool:
        is_admin_by_setting = is_admin(user_id)
        is_admin_in_chat = False
        try:
            member = await bot.get_chat_member(chat_id=chat_id, user_id=user_id)
            is_admin_in_chat = member.status in [
                ChatMemberStatus.ADMINISTRATOR,
                ChatMemberStatus.CREATOR,
            ]
        except Exception:
            pass
        return bool(is_admin_by_setting or is_admin_in_chat)

    @router.message(CommandStart(), F.chat.type == "private")
    async def start_handler(message: types.Message, state: FSMContext, bot: Bot):
        args = (message.text or "").split(maxsplit=1)
        arg = None
        if len(args) > 1:
            arg = args[1].strip()
        if arg == "new":
            existing = _get_latest_open_ticket(message.from_user.id)
            if existing:
                await message.answer(
                    f"⚠️ У вас уже есть открытый тикет № {existing['ticket_id']}. Пожалуйста, продолжайте переписку в этом тикете. Новый тикет можно создать после его закрытия"
                )
            else:
                await message.answer(
                    "📝 <b>Кратко опишите тему обращения (например, Проблема с подключением):</b>",
                    reply_markup=types.ReplyKeyboardRemove(),
                    parse_mode="HTML",
                )
                await state.set_state(SupportDialog.waiting_for_subject)
            return
        support_text = (
            get_setting("support_text")
            or "🆘 Раздел поддержки. Вы можете создать обращение или открыть существующее"
        )
        await message.answer(
            support_text,
            reply_markup=types.ReplyKeyboardMarkup(
                keyboard=[
                    [types.KeyboardButton(text="✍️ Новое обращение")],
                    [types.KeyboardButton(text="📨 Мои обращения")],
                ],
                resize_keyboard=True,
            ),
        )

    @router.callback_query(F.data == "support_new_ticket")
    async def support_new_ticket_handler(callback: types.CallbackQuery, state: FSMContext):
        await callback.answer()
        existing = _get_latest_open_ticket(callback.from_user.id)
        if existing:
            await callback.message.edit_text(
                f"⚠️ У вас уже есть открытый тикет № {existing['ticket_id']}. Продолжайте переписку в нём. Новый тикет можно создать после закрытия текущего"
            )
        else:
            await callback.message.edit_text(
                "📝 <b>Кратко опишите тему обращения (например, Проблема с подключением):</b>",
                parse_mode="HTML",
            )
            await state.set_state(SupportDialog.waiting_for_subject)

    @router.message(SupportDialog.waiting_for_subject, F.chat.type == "private")
    async def support_subject_received(message: types.Message, state: FSMContext):
        subject = (message.text or "").strip()
        if subject.startswith("/") or subject in [
            "✍️ Новое обращение",
            "📨 Мои обращения",
        ]:
            await state.clear()
            await message.answer(
                "⚠️ Создание обращения прервано. Пожалуйста, используйте меню",
                reply_markup=_user_main_reply_kb(),
            )
            return
        await state.update_data(subject=subject)
        await message.answer(
            "✉️ <b>Опишите проблему максимально подробно одним сообщением:</b>",
            parse_mode="HTML",
        )
        await state.set_state(SupportDialog.waiting_for_message)

    @router.message(SupportDialog.waiting_for_message, F.chat.type == "private")
    async def support_message_received(message: types.Message, state: FSMContext, bot: Bot):
        user_id = message.from_user.id
        content = (message.text or message.caption or "").strip()
        if content.startswith("/") or content in [
            "✍️ Новое обращение",
            "📨 Мои обращения",
        ]:
            await state.clear()
            await message.answer(
                "⚠️ Создание обращения прервано. Пожалуйста, используйте меню",
                reply_markup=_user_main_reply_kb(),
            )
            return
        data = await state.get_data()
        raw_subject = (data.get("subject") or "").strip()
        subject = raw_subject if raw_subject else "Обращение без темы"
        existing = _get_latest_open_ticket(user_id)
        created_new = False
        if existing:
            ticket_id = int(existing["ticket_id"])
            add_support_message(
                ticket_id,
                sender="user",
                content=(message.text or message.caption or ""),
            )
            ticket = get_ticket(ticket_id)
        else:
            ticket_id = create_support_ticket(user_id, subject)
            if not ticket_id:
                await message.answer("❌ Не удалось создать обращение. Попробуйте позже")
                await state.clear()
                return
            add_support_message(
                ticket_id,
                sender="user",
                content=(message.text or message.caption or ""),
            )
            ticket = get_ticket(ticket_id)
            created_new = True
        support_forum_chat_id = get_setting("support_forum_chat_id")
        thread_id = None
        if support_forum_chat_id and not (ticket and ticket.get("message_thread_id")):
            try:
                chat_id = int(support_forum_chat_id)
                author_tag = (
                    (message.from_user.username and f"@{message.from_user.username}")
                    or (message.from_user.full_name if message.from_user else None)
                    or str(user_id)
                )
                subj_full = subject or "Обращение без темы"
                is_star = subj_full.strip().startswith("⭐")
                display_subj = subj_full.lstrip("⭐️ ").strip() if is_star else subj_full
                trimmed_subject = display_subj[:40]
                important_prefix = "🔴 Важно: " if is_star else ""
                topic_name = f"№ {ticket_id} {important_prefix}{trimmed_subject} ▪️ от {author_tag}"
                forum_topic = await bot.create_forum_topic(chat_id=chat_id, name=topic_name)
                thread_id = forum_topic.message_thread_id
                update_ticket_thread_info(ticket_id, str(chat_id), int(thread_id))
                subj_display = subject or "—"
                header = (
                    "🆘 <b>Обращение в поддержку</b>\n\n"
                    f"🧾 <b>Тикет:</b> № {ticket_id}\n"
                    f"👤 <b>Пользователь:</b> @{message.from_user.username or message.from_user.full_name} (ID: {user_id})\n"
                    f"ℹ️ <b>Тема:</b> {subj_display} — от @{message.from_user.username or message.from_user.full_name} (ID: {user_id})\n\n"
                    f"✉️ <b>Сообщение:</b>\n{message.text or ''}"
                )
                await bot.send_message(
                    chat_id=chat_id,
                    text=header,
                    message_thread_id=thread_id,
                    reply_markup=_admin_actions_kb(ticket_id),
                    parse_mode="HTML",
                )
            except Exception as e:
                logger.warning(
                    f"Не удалось создать тему форума или отправить сообщение для тикета {ticket_id}: {e}"
                )
        try:
            ticket = get_ticket(ticket_id)
            forum_chat_id = ticket and ticket.get("forum_chat_id")
            thread_id = ticket and ticket.get("message_thread_id")
            if forum_chat_id and thread_id:
                username = (
                    (message.from_user.username and f"@{message.from_user.username}")
                    or message.from_user.full_name
                    or str(message.from_user.id)
                )
                await bot.send_message(
                    chat_id=int(forum_chat_id),
                    text=(
                        f"🆕 <b>Новое обращение от {username} (ID: {message.from_user.id}) по тикету № {ticket_id}:</b>"
                        if created_new
                        else f"✉️ <b>Новое сообщение по тикету № {ticket_id} от {username} (ID: {message.from_user.id}):</b>"
                    ),
                    message_thread_id=int(thread_id),
                    parse_mode="HTML",
                )
                await bot.copy_message(
                    chat_id=int(forum_chat_id),
                    from_chat_id=message.chat.id,
                    message_id=message.message_id,
                    message_thread_id=int(thread_id),
                )
        except Exception as e:
            logger.warning(f"Не удалось продублировать сообщение пользователя в форум: {e}")
        await state.clear()
        if created_new:
            await message.answer(
                f"✅ Обращение создано: № {ticket_id}. Мы ответим вам как можно скорее",
                reply_markup=_user_main_reply_kb(),
            )
        else:
            await message.answer(
                f"✉️ Сообщение добавлено в ваш открытый тикет № {ticket_id}",
                reply_markup=_user_main_reply_kb(),
            )
        # Уведомить всех администраторов при создании нового тикета
        if created_new:
            try:
                for aid in get_admin_ids():
                    try:
                        await bot.send_message(
                            int(aid),
                            (
                                "🆘 <b>Новое обращение в поддержку</b>\n\n"
                                f"🧾 <b>Тикет:</b> № {ticket_id}\n"
                                f"👤 <b>Пользователь:</b> @{message.from_user.username or message.from_user.full_name} (ID: {user_id})\n"
                                f"ℹ️ <b>Тема:</b> {subject or '—'}\n\n"
                                f"✉️ <b>Сообщение:</b>\n{message.text or ''}"
                            ),
                            parse_mode="HTML",
                        )
                    except Exception:
                        pass
            except Exception as e:
                logger.warning(f"Не удалось уведомить админов о тикете {ticket_id}: {e}")

    @router.callback_query(F.data == "support_my_tickets")
    async def support_my_tickets_handler(callback: types.CallbackQuery):
        await callback.answer()
        tickets = get_user_tickets(callback.from_user.id)
        text = "🗣️ <b>Ваши обращения:</b>" if tickets else "👌 <b>У вас пока нет обращений</b>"
        rows = []
        if tickets:
            for t in tickets:
                status_text = "🟢 Открыт" if t.get("status") == "open" else "🔒 Закрыт"
                is_star = (t.get("subject") or "").startswith("⭐ ")
                star = "⭐ " if is_star else ""
                title = f"{star}№ {t['ticket_id']} ▪️ {status_text}"
                if t.get("subject"):
                    title += f"▪️{t['subject'][:20]}"
                rows.append(
                    [
                        types.InlineKeyboardButton(
                            text=title, callback_data=f"support_view_{t['ticket_id']}"
                        )
                    ]
                )
        await callback.message.edit_text(
            text,
            reply_markup=types.InlineKeyboardMarkup(inline_keyboard=rows),
            parse_mode="HTML",
        )

    @router.callback_query(F.data.startswith("support_view_"))
    async def support_view_ticket_handler(callback: types.CallbackQuery):
        await callback.answer()
        ticket_id = int(callback.data.split("_")[-1])
        ticket = get_ticket(ticket_id)
        if not ticket or ticket.get("user_id") != callback.from_user.id:
            await callback.message.edit_text("🚫 Тикет не найден или доступ запрещён")
            return
        messages = get_ticket_messages(ticket_id)
        human_status = "🟢 Открыт" if ticket.get("status") == "open" else "🔒 Закрыт"
        is_star = (ticket.get("subject") or "").startswith("⭐ ")
        star_line = "⭐ Важно" if is_star else "—"
        parts = [
            f"🧾 <b>Тикет № {ticket_id}</b> — статус: {human_status}",
            f"ℹ️ <b>Тема:</b> {ticket.get('subject') or '—'}",
            f"⭐ <b>Важность:</b> {star_line}",
            "",
        ]
        for m in messages:
            if m.get("sender") == "note":
                continue
            who = "Вы" if m.get("sender") == "user" else "Поддержка"
            created = m.get("created_at")
            parts.append(f"<b>{who}</b> ({created}):\n{m.get('content', '')}\n")
        final_text = "\n".join(parts)
        is_open = ticket.get("status") == "open"
        buttons = []
        if is_open:
            buttons.append(
                [
                    types.InlineKeyboardButton(
                        text="💬 Ответить", callback_data=f"support_reply_{ticket_id}"
                    )
                ]
            )
            buttons.append(
                [
                    types.InlineKeyboardButton(
                        text="✅ Закрыть", callback_data=f"support_close_{ticket_id}"
                    )
                ]
            )
        buttons.append(
            [types.InlineKeyboardButton(text="⬅️ К списку", callback_data="support_my_tickets")]
        )
        await callback.message.edit_text(
            final_text,
            reply_markup=types.InlineKeyboardMarkup(inline_keyboard=buttons),
            parse_mode="HTML",
        )

    @router.callback_query(F.data.startswith("support_reply_"))
    async def support_reply_prompt_handler(callback: types.CallbackQuery, state: FSMContext):
        await callback.answer()
        ticket_id = int(callback.data.split("_")[-1])
        ticket = get_ticket(ticket_id)
        if (
            not ticket
            or ticket.get("user_id") != callback.from_user.id
            or ticket.get("status") != "open"
        ):
            await callback.message.edit_text("⚠️ Нельзя ответить на этот тикет")
            return
        await state.update_data(reply_ticket_id=ticket_id)
        # await callback.message.edit_text("✉️ <b>Напишите ваш ответ одним сообщением:</b>", parse_mode="HTML")
        await callback.message.delete()
        await callback.message.answer(
            "✉️ <b>Напишите ваш ответ одним сообщением:</b>",
            reply_markup=types.ReplyKeyboardRemove(),
            parse_mode="HTML",
        )
        await state.set_state(SupportDialog.waiting_for_reply)

    @router.message(SupportDialog.waiting_for_reply, F.chat.type == "private")
    async def support_reply_received(message: types.Message, state: FSMContext, bot: Bot):
        data = await state.get_data()
        ticket_id = data.get("reply_ticket_id")
        ticket = get_ticket(ticket_id)
        if (
            not ticket
            or ticket.get("user_id") != message.from_user.id
            or ticket.get("status") != "open"
        ):
            await message.answer("⚠️ Нельзя ответить на этот тикет")
            await state.clear()
            return

        add_support_message(
            ticket_id, sender="user", content=(message.text or message.caption or "")
        )
        await state.clear()
        await message.answer("✅ Сообщение отправлено", reply_markup=_user_main_reply_kb())

        try:
            forum_chat_id = ticket.get("forum_chat_id")
            thread_id = ticket.get("message_thread_id")

            # Если топика почему-то нет — создаем его
            if not (forum_chat_id and thread_id):
                support_forum_chat_id = get_setting("support_forum_chat_id")
                if support_forum_chat_id:
                    try:
                        chat_id = int(support_forum_chat_id)
                        subj_full = ticket.get("subject") or "Обращение без темы"
                        is_star = subj_full.strip().startswith("⭐")
                        display_subj = subj_full.lstrip("⭐️ ").strip() if is_star else subj_full
                        trimmed_subject = display_subj[:40]
                        author_tag = (
                            (message.from_user.username and f"@{message.from_user.username}")
                            or (message.from_user.full_name if message.from_user else None)
                            or str(message.from_user.id)
                        )
                        important_prefix = "🔴 Важно: " if is_star else ""
                        topic_name = (
                            f"№ {ticket_id} {important_prefix}{trimmed_subject} ▪️ от {author_tag}"
                        )
                        forum_topic = await bot.create_forum_topic(chat_id=chat_id, name=topic_name)
                        thread_id = forum_topic.message_thread_id
                        forum_chat_id = chat_id
                        update_ticket_thread_info(ticket_id, str(chat_id), int(thread_id))

                        subj_display = ticket.get("subject") or "—"
                        header = (
                            "📌 <b>Тред создан автоматически</b>\n\n"
                            f"🧾 <b>Тикет:</b> № {ticket_id}\n"
                            f"👤 <b>Пользователь:</b> ID {ticket.get('user_id')}\n"
                            f"ℹ️ <b>Тема:</b> {subj_display} — от ID {ticket.get('user_id')}"
                        )
                        await bot.send_message(
                            chat_id=chat_id,
                            text=header,
                            message_thread_id=thread_id,
                            reply_markup=_admin_actions_kb(ticket_id),
                            parse_mode="HTML",
                        )
                    except Exception as e:
                        logger.warning(
                            f"Не удалось автоматически создать тему форума для тикета {ticket_id}: {e}"
                        )

            if forum_chat_id and thread_id:
                username = (
                    (message.from_user.username and f"@{message.from_user.username}")
                    or message.from_user.full_name
                    or str(message.from_user.id)
                )

                await bot.send_message(
                    chat_id=int(forum_chat_id),
                    text=f"✉️ <b>Новое сообщение по тикету № {ticket_id} от {username} (ID: {message.from_user.id}):</b>",
                    message_thread_id=int(thread_id),
                    parse_mode="HTML",
                )
                await bot.copy_message(
                    chat_id=int(forum_chat_id),
                    from_chat_id=message.chat.id,
                    message_id=message.message_id,
                    message_thread_id=int(thread_id),
                )
        except Exception as e:
            logger.warning(f"Не удалось продублировать ответ пользователя в форум: {e}")

    @router.message(F.is_topic_message)
    async def forum_thread_message_handler(message: types.Message, bot: Bot, state: FSMContext):
        try:
            if not message.message_thread_id:
                return
            forum_chat_id = message.chat.id
            thread_id = message.message_thread_id
            ticket = get_ticket_by_thread(str(forum_chat_id), int(thread_id))
            if not ticket:
                return
            user_id = int(ticket.get("user_id"))
            try:
                current_state = await state.get_state()
                if current_state == AdminDialog.waiting_for_note.state:
                    note_body = (message.text or message.caption or "").strip()
                    author_id = message.from_user.id if message.from_user else None
                    if author_id:
                        username = None
                        if message.from_user.username:
                            username = f"@{message.from_user.username}"
                        else:
                            username = message.from_user.full_name or str(author_id)
                        note_text = f"[🗒 Заметка от {username} (ID: {author_id})]\n{note_body}"
                    else:
                        note_text = note_body
                    add_support_message(int(ticket["ticket_id"]), sender="note", content=note_text)
                    await message.answer("📝 Внутренняя заметка сохранена")
                    await state.clear()
                    return
            except Exception:
                pass
            me = await bot.get_me()
            if message.from_user and message.from_user.id == me.id:
                return
            # Многоадминная проверка
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
                text=f"💬 <b>Ответ поддержки по тикету № {ticket['ticket_id']}</b>",
                parse_mode="HTML",
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

    @router.callback_query(F.data.startswith("support_close_"))
    async def support_close_ticket_handler(callback: types.CallbackQuery, bot: Bot):
        await callback.answer()
        ticket_id = int(callback.data.split("_")[-1])
        ticket = get_ticket(ticket_id)
        if not ticket or ticket.get("user_id") != callback.from_user.id:
            await callback.message.edit_text("🚫 Тикет не найден или доступ запрещён")
            return
        if ticket.get("status") == "closed":
            await callback.message.edit_text("✅ Тикет уже закрыт")
            return
        ok = set_ticket_status(ticket_id, "closed")
        if ok:
            try:
                forum_chat_id = ticket.get("forum_chat_id")
                thread_id = ticket.get("message_thread_id")
                if forum_chat_id and thread_id:
                    try:
                        username = (
                            (callback.from_user.username and f"@{callback.from_user.username}")
                            or callback.from_user.full_name
                            or str(callback.from_user.id)
                        )
                        await bot.send_message(
                            chat_id=int(forum_chat_id),
                            text=f"✅ Пользователь {username} закрыл тикет № {ticket_id}",
                            message_thread_id=int(thread_id),
                        )
                        await bot.send_message(
                            chat_id=int(forum_chat_id),
                            text="🎛️ <b>Панель управления тикетом:</b>",
                            message_thread_id=int(thread_id),
                            reply_markup=_admin_actions_kb(ticket_id),
                        )
                    except Exception:
                        pass
                await bot.close_forum_topic(
                    chat_id=int(forum_chat_id), message_thread_id=int(thread_id)
                )
            except Exception as e:
                logger.warning(
                    f"Не удалось закрыть тему форума для тикета {ticket_id} из бота: {e}"
                )
            await callback.message.edit_text(
                "✅ Тикет закрыт",
                reply_markup=types.InlineKeyboardMarkup(
                    inline_keyboard=[
                        [
                            types.InlineKeyboardButton(
                                text="⬅️ К списку", callback_data="support_my_tickets"
                            )
                        ]
                    ]
                ),
            )
            try:
                await callback.message.answer(
                    "🏠 <b>Меню поддержки</b>\n\nВыберите действие:",
                    reply_markup=_user_main_reply_kb(),
                    parse_mode="HTML",
                )
            except Exception:
                pass
        else:
            await callback.message.edit_text("❌ Не удалось закрыть тикет")

    @router.callback_query(F.data.startswith("admin_close_"))
    async def admin_close_ticket(callback: types.CallbackQuery, bot: Bot):
        await callback.answer()
        try:
            ticket_id = int(callback.data.split("_")[-1])
        except Exception:
            return
        ticket = get_ticket(ticket_id)
        if not ticket:
            await callback.message.edit_text("Тикет не найден")
            return
        forum_chat_id = int(ticket.get("forum_chat_id") or callback.message.chat.id)
        if not await _is_admin(bot, forum_chat_id, callback.from_user.id):
            return
        if set_ticket_status(ticket_id, "closed"):
            try:
                thread_id = ticket.get("message_thread_id")
                if thread_id:
                    await bot.close_forum_topic(
                        chat_id=forum_chat_id, message_thread_id=int(thread_id)
                    )
            except Exception:
                pass
            try:
                await callback.message.edit_text(
                    f"✅ Тикет № {ticket_id} закрыт",
                    reply_markup=_admin_actions_kb(ticket_id),
                )
            except TelegramBadRequest as e:
                if "message is not modified" in str(e):
                    await callback.answer("Без изменений", show_alert=False)
                else:
                    raise
            try:
                user_id = int(ticket.get("user_id"))
                await bot.send_message(
                    chat_id=user_id,
                    text=f"✅ Ваш тикет № {ticket_id} был закрыт администратором. Спасибо за обращение!",
                )
            except Exception:
                pass
        else:
            await callback.message.answer("❌ Не удалось закрыть тикет")

    @router.callback_query(F.data.startswith("admin_reopen_"))
    async def admin_reopen_ticket(callback: types.CallbackQuery, bot: Bot):
        await callback.answer()
        try:
            ticket_id = int(callback.data.split("_")[-1])
        except Exception:
            return
        ticket = get_ticket(ticket_id)
        if not ticket:
            await callback.message.edit_text("Тикет не найден")
            return
        forum_chat_id = int(ticket.get("forum_chat_id") or callback.message.chat.id)
        if not await _is_admin(bot, forum_chat_id, callback.from_user.id):
            return
        if set_ticket_status(ticket_id, "open"):
            try:
                thread_id = ticket.get("message_thread_id")
                if thread_id:
                    await bot.reopen_forum_topic(
                        chat_id=forum_chat_id, message_thread_id=int(thread_id)
                    )
            except Exception:
                pass
            try:
                await callback.message.edit_text(
                    f"🔓 Тикет № {ticket_id} переоткрыт",
                    reply_markup=_admin_actions_kb(ticket_id),
                )
            except TelegramBadRequest as e:
                if "message is not modified" in str(e):
                    await callback.answer("Без изменений", show_alert=False)
                else:
                    raise
            try:
                user_id = int(ticket.get("user_id"))
                await bot.send_message(
                    chat_id=user_id,
                    text=f"🔓 Ваш тикет № {ticket_id} был переоткрыт администратором. Вы можете продолжить переписку",
                )
            except Exception:
                pass
        else:
            await callback.message.answer("❌ Не удалось переоткрыть тикет")

    @router.callback_query(F.data.startswith("admin_delete_"))
    async def admin_delete_ticket(callback: types.CallbackQuery, bot: Bot):
        await callback.answer()
        try:
            ticket_id = int(callback.data.split("_")[-1])
        except Exception:
            return
        ticket = get_ticket(ticket_id)
        if not ticket:
            await callback.message.edit_text("Тикет уже удалён или не найден")
            return
        forum_chat_id = int(ticket.get("forum_chat_id") or callback.message.chat.id)
        if not await _is_admin(bot, forum_chat_id, callback.from_user.id):
            return
        try:
            thread_id = ticket.get("message_thread_id")
            if thread_id:
                await bot.delete_forum_topic(
                    chat_id=forum_chat_id, message_thread_id=int(thread_id)
                )
        except Exception:
            try:
                if thread_id:
                    await bot.close_forum_topic(
                        chat_id=forum_chat_id, message_thread_id=int(thread_id)
                    )
            except Exception:
                pass
        if delete_ticket(ticket_id):
            try:
                await callback.message.edit_text(f"🗑 Тикет № {ticket_id} удалён")
            except TelegramBadRequest as e:
                if "message to edit not found" in str(e) or "message is not modified" in str(e):
                    await callback.message.answer(f"🗑 Тикет № {ticket_id} удалён")
                else:
                    raise
        else:
            await callback.message.answer("❌ Не удалось удалить тикет")

    @router.callback_query(F.data.startswith("admin_star_"))
    async def admin_toggle_star(callback: types.CallbackQuery, bot: Bot):
        await callback.answer()
        try:
            ticket_id = int(callback.data.split("_")[-1])
        except Exception:
            return
        ticket = get_ticket(ticket_id)
        if not ticket:
            return
        forum_chat_id = int(ticket.get("forum_chat_id") or callback.message.chat.id)
        if not await _is_admin(bot, forum_chat_id, callback.from_user.id):
            return
        subject = (ticket.get("subject") or "").strip()
        is_starred = subject.startswith("⭐ ")
        if is_starred:
            base_subject = subject[2:].strip()
            new_subject = base_subject if base_subject else "Обращение без темы"
        else:
            base_subject = subject if subject else "Обращение без темы"
            new_subject = f"⭐ {base_subject}"
        if update_ticket_subject(ticket_id, new_subject):
            try:
                thread_id = ticket.get("message_thread_id")
                if thread_id and ticket.get("forum_chat_id"):
                    user_id = int(ticket.get("user_id")) if ticket.get("user_id") else None
                    author_tag = None
                    if user_id:
                        try:
                            user = await bot.get_chat(user_id)
                            username = getattr(user, "username", None)
                            author_tag = f"@{username}" if username else f"ID {user_id}"
                        except Exception:
                            author_tag = f"ID {user_id}"
                    else:
                        author_tag = "пользователь"
                    subj_full = new_subject or "Обращение без темы"
                    is_star2 = subj_full.strip().startswith("⭐")
                    display_subj2 = subj_full.lstrip("⭐️ ").strip() if is_star2 else subj_full
                    trimmed = display_subj2[:40]
                    important_prefix2 = "🔴 Важно: " if is_star2 else ""
                    topic_name = f"№ {ticket_id} {important_prefix2}{trimmed} ▪️ от {author_tag}"
                    await bot.edit_forum_topic(
                        chat_id=int(ticket["forum_chat_id"]),
                        message_thread_id=int(thread_id),
                        name=topic_name,
                    )
            except Exception:
                pass
            try:
                thread_id = ticket.get("message_thread_id")
                forum_chat_id = ticket.get("forum_chat_id")
                if thread_id and forum_chat_id:
                    state_text = "включена" if not is_starred else "снята"
                    msg = await bot.send_message(
                        chat_id=int(forum_chat_id),
                        message_thread_id=int(thread_id),
                        text=f"⭐ Важность {state_text} для тикета № {ticket_id}",
                    )
                    if not is_starred:
                        try:
                            await bot.pin_chat_message(
                                chat_id=int(forum_chat_id),
                                message_id=msg.message_id,
                                disable_notification=True,
                            )
                        except Exception:
                            pass
                    else:
                        try:
                            await bot.unpin_all_forum_topic_messages(
                                chat_id=int(forum_chat_id),
                                message_thread_id=int(thread_id),
                            )
                        except Exception:
                            pass
            except Exception:
                pass
            state_text = "включена" if not is_starred else "снята"
            await callback.message.answer(
                f"⭐ Пометка важности {state_text}. Название темы обновлено"
            )
        else:
            await callback.message.answer("❌ Не удалось обновить тему тикета")

    @router.callback_query(F.data.startswith("admin_user_"))
    async def admin_show_user(callback: types.CallbackQuery, bot: Bot):
        await callback.answer()
        try:
            ticket_id = int(callback.data.split("_")[-1])
        except Exception:
            return
        ticket = get_ticket(ticket_id)
        if not ticket:
            return
        forum_chat_id = int(ticket.get("forum_chat_id") or callback.message.chat.id)
        if not await _is_admin(bot, forum_chat_id, callback.from_user.id):
            return
        user_id = int(ticket.get("user_id"))
        mention_link = f"tg://user?id={user_id}"
        username = None
        try:
            user = await bot.get_chat(user_id)
            username = getattr(user, "username", None)
        except Exception:
            pass
        text = (
            "👤 <b>Пользователь тикета</b>\n\n"
            f"🆔 <b>ID:</b> <code>{user_id}</code>\n"
            f"📛 <b>Имя:</b> @{username}\n"
            if username
            else ""
        ) + f"🔗 <b>Ссылка:</b> {mention_link}"
        await callback.message.answer(text, parse_mode="HTML")

    @router.callback_query(F.data.startswith("admin_toggle_ban_"))
    async def admin_toggle_ban(callback: types.CallbackQuery, bot: Bot):
        await callback.answer()
        try:
            ticket_id = int(callback.data.split("_")[-1])
        except Exception:
            return
        ticket = get_ticket(ticket_id)
        if not ticket:
            await callback.message.answer("Тикет не найден")
            return
        forum_chat_id_raw = ticket.get("forum_chat_id")
        forum_chat_id = int(forum_chat_id_raw) if forum_chat_id_raw else callback.message.chat.id
        if not await _is_admin(bot, forum_chat_id, callback.from_user.id):
            return
        user_id_raw = ticket.get("user_id")
        if not user_id_raw:
            await callback.message.answer("❌ Не удалось определить пользователя тикета")
            return
        try:
            user_id = int(user_id_raw)
        except Exception:
            await callback.message.answer("❌ Некорректный идентификатор пользователя")
            return
        try:
            user_data = get_user(user_id) or {}
            currently_banned = bool(user_data.get("is_banned"))
        except Exception:
            currently_banned = False
        try:
            if currently_banned:
                unban_user(user_id)
            else:
                ban_user(user_id)
        except Exception as e:
            await callback.message.answer(f"❌ Не удалось обновить статус блокировки: {e}")
            return

        status_text: str
        if currently_banned:
            status_text = f"✅ Пользователь {user_id} разбанен"
            try:
                await bot.send_message(
                    user_id,
                    "✅ Ваш аккаунт разблокирован администратором. Вы снова можете пользоваться сервисом",
                )
            except Exception:
                pass
        else:
            status_text = f"🚫 Пользователь {user_id} забанен"
            support_contact = (
                get_setting("support_bot_username") or get_setting("support_user") or ""
            ).strip()
            ban_message = "🚫 Ваш аккаунт заблокирован администратором"
            if support_contact:
                ban_message += f"\nЕсли это ошибка, свяжитесь с поддержкой: {support_contact}"
            try:
                await bot.send_message(user_id, ban_message)
            except Exception:
                pass
        try:
            await callback.message.edit_reply_markup(reply_markup=_admin_actions_kb(ticket_id))
        except Exception:
            pass
        await callback.message.answer(status_text)

    @router.callback_query(F.data.startswith("admin_note_"))
    async def admin_note_prompt(callback: types.CallbackQuery, state: FSMContext, bot: Bot):
        await callback.answer()
        try:
            ticket_id = int(callback.data.split("_")[-1])
        except Exception:
            return
        ticket = get_ticket(ticket_id)
        if not ticket:
            return
        forum_chat_id = int(ticket.get("forum_chat_id") or callback.message.chat.id)
        if not await _is_admin(bot, forum_chat_id, callback.from_user.id):
            return
        await state.update_data(note_ticket_id=ticket_id)
        await callback.message.answer(
            "📝 <b>Отправьте внутреннюю заметку одним сообщением. Она не будет отправлена пользователю:</b>",
            parse_mode="HTML",
        )
        await state.set_state(AdminDialog.waiting_for_note)

    @router.callback_query(F.data.startswith("admin_notes_"))
    async def admin_list_notes(callback: types.CallbackQuery, bot: Bot):
        await callback.answer()
        try:
            ticket_id = int(callback.data.split("_")[-1])
        except Exception:
            return
        ticket = get_ticket(ticket_id)
        if not ticket:
            return
        forum_chat_id = int(ticket.get("forum_chat_id") or callback.message.chat.id)
        if not await _is_admin(bot, forum_chat_id, callback.from_user.id):
            return
        notes = [m for m in get_ticket_messages(ticket_id) if m.get("sender") == "note"]
        if not notes:
            await callback.message.answer("🗒 Внутренних заметок пока нет")
            return
        lines = [f"🗒 Заметки по тикету № {ticket_id}:"]
        for m in notes:
            created = m.get("created_at")
            content = (m.get("content") or "").strip()
            lines.append(f"— ({created})\n{content}")
        text = "\n\n".join(lines)
        await callback.message.answer(text)

    @router.message(AdminDialog.waiting_for_note, F.is_topic_message)
    async def admin_note_receive(message: types.Message, state: FSMContext):
        data = await state.get_data()
        ticket_id = data.get("note_ticket_id")
        if not ticket_id:
            await message.answer("❌ Не найден контекст тикета для заметки")
            await state.clear()
            return
        author_id = message.from_user.id if message.from_user else None
        username = None
        if message.from_user:
            if message.from_user.username:
                username = f"@{message.from_user.username}"
            else:
                username = message.from_user.full_name or str(author_id)
        note_body = (message.text or message.caption or "").strip()
        note_text = (
            f"[Заметка от {username} (ID: {author_id})]\n{note_body}" if author_id else note_body
        )
        add_support_message(int(ticket_id), sender="note", content=note_text)
        await message.answer("📝 Внутренняя заметка сохранена")
        await state.clear()

    @router.message(F.text == "▶️ Начать", F.chat.type == "private")
    async def start_text_button(message: types.Message, state: FSMContext):
        existing = _get_latest_open_ticket(message.from_user.id)
        if existing:
            await message.answer(
                f"⚠️ У вас уже есть открытый тикет № {existing['ticket_id']}. Продолжайте переписку в нём"
            )
        else:
            await message.answer(
                "📝 <b>Кратко опишите тему обращения (например, Проблема с подключением):</b>",
                reply_markup=types.ReplyKeyboardRemove(),
                parse_mode="HTML",
            )
            await state.set_state(SupportDialog.waiting_for_subject)

    @router.message(F.text == "✍️ Новое обращение", F.chat.type == "private")
    async def new_ticket_text_button(message: types.Message, state: FSMContext):
        existing = _get_latest_open_ticket(message.from_user.id)
        if existing:
            await message.answer(
                f"⚠️ У вас уже есть открытый тикет № {existing['ticket_id']}. Продолжайте переписку в нём"
            )
        else:
            await message.answer(
                "📝 <b>Кратко опишите тему обращения (например, Проблема с подключением):</b>",
                reply_markup=types.ReplyKeyboardRemove(),
                parse_mode="HTML",
            )
            await state.set_state(SupportDialog.waiting_for_subject)

    @router.message(F.text == "📨 Мои обращения", F.chat.type == "private")
    async def my_tickets_text_button(message: types.Message):
        tickets = get_user_tickets(message.from_user.id)
        text = "🗣️ <b>Ваши обращения:</b>" if tickets else "👌 <b>У вас пока нет обращений</b>"
        rows = []
        if tickets:
            for t in tickets:
                status_text = "🟢 Открыт" if t.get("status") == "open" else "🔒 Закрыт"
                title = f"№ {t['ticket_id']} ▪️ {status_text}"
                if t.get("subject"):
                    title += f"▪️{t['subject'][:20]}"
                rows.append(
                    [
                        types.InlineKeyboardButton(
                            text=title, callback_data=f"support_view_{t['ticket_id']}"
                        )
                    ]
                )
        await message.answer(
            text,
            reply_markup=types.InlineKeyboardMarkup(inline_keyboard=rows),
            parse_mode="HTML",
        )

    @router.message(F.chat.type == "private")
    async def relay_user_message_to_forum(message: types.Message, bot: Bot, state: FSMContext):
        current_state = await state.get_state()
        if current_state is not None:
            return

        user_id = message.from_user.id if message.from_user else None
        if not user_id:
            return

        tickets = get_user_tickets(user_id)
        content = message.text or message.caption or ""
        ticket = None
        if not tickets:
            ticket_id = create_support_ticket(user_id, None)
            add_support_message(ticket_id, sender="user", content=content)
            ticket = get_ticket(ticket_id)
            created_new = True
        else:
            open_tickets = [t for t in tickets if t.get("status") == "open"]
            if not open_tickets:
                ticket_id = create_support_ticket(user_id, None)
                add_support_message(ticket_id, sender="user", content=content)
                ticket = get_ticket(ticket_id)
                created_new = True
            else:
                ticket = max(open_tickets, key=lambda t: int(t["ticket_id"]))
                ticket_id = int(ticket["ticket_id"])
                add_support_message(ticket_id, sender="user", content=content)
                created_new = False

        try:
            forum_chat_id = ticket.get("forum_chat_id")
            thread_id = ticket.get("message_thread_id")
            if not (forum_chat_id and thread_id):
                support_forum_chat_id = get_setting("support_forum_chat_id")
                if support_forum_chat_id:
                    try:
                        chat_id = int(support_forum_chat_id)
                        subj_full = ticket.get("subject") or "Обращение без темы"
                        is_star = subj_full.strip().startswith("⭐")
                        display_subj = subj_full.lstrip("⭐️ ").strip() if is_star else subj_full
                        trimmed = display_subj[:40]
                        author_tag = (
                            (message.from_user.username and f"@{message.from_user.username}")
                            or (message.from_user.full_name if message.from_user else None)
                            or str(message.from_user.id)
                        )
                        important_prefix = "🔴 Важно: " if is_star else ""
                        topic_name = f"№ {ticket_id} {important_prefix}{trimmed} ▪️ от {author_tag}"
                        forum_topic = await bot.create_forum_topic(chat_id=chat_id, name=topic_name)
                        thread_id = forum_topic.message_thread_id
                        forum_chat_id = chat_id
                        update_ticket_thread_info(ticket_id, str(chat_id), int(thread_id))
                        subj_display = ticket.get("subject") or "—"
                        header = (
                            (
                                "🆘 <b>Новое обращение в поддержку</b>\n\n"
                                if created_new
                                else "📌 <b>Тред создан автоматически</b>\n\n"
                            )
                            + f"🧾 <b>Тикет:</b> № {ticket_id}\n"
                            f"👤 <b>Пользователь:</b> @{message.from_user.username or message.from_user.full_name} (ID: {message.from_user.id})\n"
                            f"Тема: {subj_display} — от @{message.from_user.username or message.from_user.full_name} (ID: {message.from_user.id})"
                        )
                        await bot.send_message(
                            chat_id=chat_id,
                            text=header,
                            message_thread_id=thread_id,
                            reply_markup=_admin_actions_kb(ticket_id),
                        )
                    except Exception as e:
                        logger.warning(
                            f"Не удалось автоматически создать тему форума для тикета {ticket_id}: {e}"
                        )
            if forum_chat_id and thread_id:
                try:
                    subj_full = ticket.get("subject") or "Обращение без темы"
                    is_star = subj_full.strip().startswith("⭐")
                    display_subj = subj_full.lstrip("⭐️ ").strip() if is_star else subj_full
                    trimmed = display_subj[:40]
                    author_tag = (
                        (message.from_user.username and f"@{message.from_user.username}")
                        or (message.from_user.full_name if message.from_user else None)
                        or str(message.from_user.id)
                    )
                    important_prefix = "🔴 Важно: " if is_star else ""
                    topic_name = f"№ {ticket_id} {important_prefix}{trimmed} ▪️ от {author_tag}"
                    await bot.edit_forum_topic(
                        chat_id=int(forum_chat_id),
                        message_thread_id=int(thread_id),
                        name=topic_name,
                    )
                except Exception as e:
                    logger.warning(
                        f"Не удалось переименовать тему для тикета со свободным сообщением {ticket_id}: {e}"
                    )
                username = (
                    (message.from_user.username and f"@{message.from_user.username}")
                    or message.from_user.full_name
                    or str(message.from_user.id)
                )
                await bot.send_message(
                    chat_id=int(forum_chat_id),
                    text=(
                        f"🆕 <b>Новое обращение от {username} (ID: {message.from_user.id}) по тикету № {ticket_id}:</b>"
                        if created_new
                        else f"✉️ <b>Новое сообщение по тикету № {ticket_id} от {username} (ID: {message.from_user.id}):</b>"
                    ),
                    message_thread_id=int(thread_id),
                    parse_mode="HTML",
                )
                await bot.copy_message(
                    chat_id=int(forum_chat_id),
                    from_chat_id=message.chat.id,
                    message_id=message.message_id,
                    message_thread_id=int(thread_id),
                )
        except Exception as e:
            logger.warning(
                f"Не удалось продублировать свободное сообщение пользователя в форум для тикета {ticket_id}: {e}"
            )

        try:
            if created_new:
                await message.answer(
                    f"✅ Обращение создано: № {ticket_id}. Мы ответим вам как можно скорее"
                )
            else:
                await message.answer("✅ Сообщение принято. Поддержка скоро ответит")
        except Exception:
            pass

    return router
