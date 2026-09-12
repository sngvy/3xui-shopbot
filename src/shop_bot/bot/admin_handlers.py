"""Обработчики Telegram-команд и колбэков админ-панели бота (управление ключами, хостами, пользователями)."""

import asyncio
import base64
import calendar
import html as html_escape
import itertools
import json
import logging
import os
import re
import secrets
import string
import time
import uuid
from datetime import datetime, timedelta

from aiogram import Bot, F, Router, html, types
from aiogram.exceptions import (
    TelegramBadRequest,
    TelegramForbiddenError,
    TelegramRetryAfter,
)
from aiogram.filters import Command, StateFilter
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup, WebAppInfo
from aiogram.utils.keyboard import InlineKeyboardBuilder

from shop_bot.bot import keyboards
from shop_bot.bot.handlers import delete_and_send, format_transaction_line, safe_edit_text
from shop_bot.data_manager import (
    backup_manager,
    database,
    resource_monitor,
    speedtest_runner,
)
from shop_bot.data_manager.database import (  # Promo API
    add_new_key,
    add_to_balance,
    ban_user,
    create_promo_code,
    deduct_from_balance,
    delete_all_promo_codes,
    delete_key_by_email,
    format_stored_username,
    get_admin_stats,
    get_all_hosts,
    get_all_users,
    get_key_by_email,
    get_key_by_id,
    get_keys_for_host,
    get_keys_for_user,
    get_latest_transactions,
    get_promo_code,
    get_referral_balance_all,
    get_referrals_for_user,
    get_top_referrers,
    get_user,
    get_user_transactions,
    get_users_by_segment,
    is_admin,
    keys_in_transition,
    list_promo_codes,
    log_transaction,
    unban_user,
    update_key_email,
    update_key_host_and_info,
    update_key_info,
    update_promo_code_status,
)
from shop_bot.data_manager.resource_monitor import (
    get_docker_logs,
    get_live_metrics_without_db,
    restart_docker_container,
)
from shop_bot.modules.xui_api import (
    EMAIL_DOMAIN,
    create_or_update_key_on_host,
    delete_client_on_host,
    get_key_details_from_host,
    get_realtime_online_count,
    login_to_host,
)

logger = logging.getLogger(__name__)

REDIR_URL = os.getenv("REDIR_URL")

last_refresh_time = 0


class Broadcast(StatesGroup):
    """Состояния FSM для сценария рассылки сообщений админом."""

    waiting_for_segment = State()
    waiting_for_message = State()
    waiting_for_button_option = State()
    waiting_for_button_text = State()
    waiting_for_button_url = State()
    waiting_for_promo_option = State()
    waiting_for_promo_discount = State()
    waiting_for_confirmation = State()


def format_dt_for_display(raw) -> str:
    """Форматирует дату/время из БД для показа в Telegram, без служебных
    микросекунд (expiry_date хранится через datetime.fromtimestamp(ms/1000),
    что почти всегда даёт ненулевые микросекунды в сыром TEXT-значении SQLite —
    показывать их пользователю/админу незачем).
    """
    if not raw:
        return "—"
    if isinstance(raw, datetime):
        dt = raw
    else:
        try:
            dt = datetime.fromisoformat(str(raw))
        except ValueError:
            return str(raw)
    return dt.strftime("%d.%m.%Y %H:%M")


def get_admin_router() -> Router:
    """Собирает и возвращает Router со всеми обработчиками админ-панели бота."""
    admin_router = Router()

    # Helper: форматирование упоминания пользователя (инициатора)
    def _format_user_mention(u: types.User) -> str:
        try:
            if u.username:
                uname = u.username.lstrip("@")
                return f"@{uname}"
            # Fallback: кликабельная ссылка по ID с читаемым именем
            full_name = (u.full_name or u.first_name or "Администратор").strip()
            # html_escape — это модуль, импортированный как html; у него есть .escape
            try:
                safe_name = html_escape.escape(full_name)
            except Exception:
                safe_name = full_name
            return f"{safe_name} ({u.id})"
        except Exception:
            return str(getattr(u, "id", "—"))

    async def _render_user_card(
        bot: Bot,
        chat_id: int,
        message_id: int | None,
        user_id: int,
        note: str | None = None,
    ):
        """Строит карточку пользователя и обновляет ею существующее сообщение (или отправляет новое, если редактировать нечего).

        Используется и для обычного просмотра, и как место возврата после
        начисления/списания баланса, бана/разбана и выдачи подписки —
        чтобы админ не терял контекст и не улетал каждый раз в самое
        верхнее меню.
        """
        user = await asyncio.to_thread(get_user, user_id)
        if not user:
            if message_id:
                try:
                    await bot.edit_message_text(
                        "❌ Пользователь не найден",
                        chat_id=chat_id,
                        message_id=message_id,
                    )
                    return
                except TelegramBadRequest:
                    pass
            await bot.send_message(chat_id, "❌ Пользователь не найден")
            return
        user_tag = format_stored_username(user.get("username"))
        is_banned = user.get("is_banned", False)
        total_spent = user.get("total_spent", 0)
        balance = user.get("balance", 0)
        referred_by = user.get("referred_by")
        keys = await asyncio.to_thread(get_keys_for_user, user_id)
        keys_count = len(keys)
        text = (
            f"👤 <b>Пользователь:</b> {user_id}\n\n"
            f"🆔 <b>Имя пользователя:</b> {user_tag}\n"
            f"💸 <b>Всего потратил:</b> {float(total_spent):.2f} ₽\n"
            f"💰 <b>Баланс:</b> {float(balance):.2f} ₽\n"
            f"⛔ <b>Забанен:</b> {'да' if is_banned else 'нет'}\n"
            f"👥 <b>Приглашён:</b> {referred_by if referred_by else '—'}\n"
            f"🔑 <b>Подписок:</b> {keys_count}"
        )
        if note:
            text = f"{note}\n\n" + text
        markup = keyboards.create_admin_user_actions_keyboard(user_id, is_banned=is_banned)
        if message_id:
            try:
                await bot.edit_message_text(
                    text,
                    chat_id=chat_id,
                    message_id=message_id,
                    reply_markup=markup,
                    parse_mode="HTML",
                )
                return
            except TelegramBadRequest as e:
                if "message is not modified" in str(e).lower():
                    return
        await bot.send_message(chat_id, text, reply_markup=markup, parse_mode="HTML")

    async def show_admin_menu(message: types.Message, edit_message: bool = False):
        # Собираем статистику для отображения прямо в админ-меню
        stats = await asyncio.to_thread(get_admin_stats) or {}
        today_new = stats.get("today_new_users", 0)
        today_income = float(stats.get("today_income", 0) or 0)
        today_keys = stats.get("today_issued_keys", 0)
        total_users = stats.get("total_users", 0)
        total_income = float(stats.get("total_income", 0) or 0)
        total_keys = stats.get("total_keys", 0)
        active_keys = stats.get("active_keys", 0)

        text = (
            "📊 <b>Панель Администратора</b>\n\n"
            "<b>За сегодня:</b>\n"
            f"👥 Новых пользователей: {today_new}\n"
            f"💰 Доход: {today_income:.2f} ₽\n"
            f"🔑 Выдано подписок: {today_keys}\n\n"
            "<b>За все время:</b>\n"
            f"👥 Всего пользователей: {total_users}\n"
            f"💰 Общий доход: {total_income:.2f} ₽\n"
            f"🔑 Всего подписок: {total_keys}\n\n"
            "<b>Состояние подписок:</b>\n"
            f"✅ Активных: {active_keys}"
        )
        keyboard = keyboards.create_admin_menu_keyboard()
        if edit_message:
            try:
                await message.edit_text(text, reply_markup=keyboard)
            except Exception:
                pass
        else:
            await message.answer(text, reply_markup=keyboard)

    async def _format_monitor_metrics(
        data: dict = None,
    ) -> tuple[str, dict[str, float]]:
        pieces = []
        worst: dict[str, float] = {
            "cpu_percent": 0.0,
            "mem_percent": 0.0,
            "disk_percent": 0.0,
        }

        def _add_line(
            title: str,
            ok: bool,
            cpu: float | None,
            mem: float | None,
            disk: float | None,
            load: dict | None,
            uptime: float | None,
            extra: str | None = None,
        ) -> str:
            cpu_txt = f"CPU {cpu:.0f}%" if cpu is not None else "CPU —"
            mem_txt = f"RAM {mem:.0f}%" if mem is not None else "RAM —"
            disk_txt = f"Disk {disk:.0f}%" if disk is not None else "Disk —"
            load_txt = ""
            if load and load.get("1m") is not None:
                load_txt = f"load {load.get('1m'):.2f}/{load.get('5m'):.2f}/{load.get('15m'):.2f}"
            uptime_txt = ""
            if uptime is not None:
                days = int(uptime // 86400)
                hours = int((uptime % 86400) // 3600)
                uptime_txt = f"uptime {days} д {hours} ч"
            status = "🟢" if ok else "🔴"
            line = (
                f"{status} <b>{title}</b>\n"
                f"⚙️ {cpu_txt} | 🧩 {mem_txt} | 💿 {disk_txt} | ⚡ {load_txt} | 🕒 {uptime_txt}\n"
            )
            if extra:
                line += f"\n    {extra}"
            return line

        if data and data.get("ok"):
            items = data.get("items", [])
            local = next((i for i in items if i.get("is_local")), {})
            hosts = [i for i in items if not i.get("is_local")]
        else:
            local = await asyncio.to_thread(resource_monitor.get_local_metrics) or {}
            local["host_name"] = "Панель"
            local["is_local"] = True
            hosts = []
            try:
                db_hosts = db_hosts = await asyncio.to_thread(database.get_all_hosts) or []
                for db_h in db_hosts:
                    if isinstance(db_h, dict):
                        name = db_h.get("host_name")
                        has_ssh = db_h.get("ssh_host") and db_h.get("ssh_user")
                    else:
                        name = getattr(db_h, "host_name", None)
                        has_ssh = getattr(db_h, "ssh_host", None) and getattr(
                            db_h, "ssh_user", None
                        )

                    if not name or not has_ssh:
                        continue

                    metrics = await asyncio.to_thread(database.get_latest_host_metrics, name) or {}
                    hosts.append(
                        {
                            "host_name": name,
                            "ok": bool(metrics.get("ok", False)),
                            "cpu_percent": metrics.get("cpu_percent"),
                            "mem_percent": metrics.get("mem_percent"),
                            "disk_percent": metrics.get("disk_percent"),
                            "load1": metrics.get("load1"),
                            "load5": metrics.get("load5"),
                            "load15": metrics.get("load15"),
                            "uptime_seconds": metrics.get("uptime_seconds"),
                            "error": metrics.get("error"),
                        }
                    )
            except Exception:
                hosts = []

        cpu_local = local.get("cpu_percent")
        mem_local = local.get("mem_percent")
        disk_local = local.get("disk_percent")
        pieces.append(
            _add_line(
                "Панель",
                bool(local.get("ok", True) and not local.get("error")),
                cpu_local,
                mem_local,
                disk_local,
                local.get("loadavg"),
                local.get("uptime_seconds"),
                extra=local.get("error"),
            )
        )

        for h in hosts:
            name = h.get("host_name", "❓")
            ok = bool(h.get("ok") and not h.get("error"))
            cpu = h.get("cpu_percent")
            mem = h.get("mem_percent")
            disk = h.get("disk_percent")

            load = h.get("loadavg")
            if not load and "load1" in h:
                load = {
                    "1m": h.get("load1"),
                    "5m": h.get("load5"),
                    "15m": h.get("load15"),
                }

            pieces.append(
                _add_line(
                    f"{name}",
                    ok,
                    cpu,
                    mem,
                    disk,
                    load,
                    h.get("uptime_seconds"),
                    extra=h.get("error"),
                )
            )

            if isinstance(cpu, (int, float)) and cpu > worst["cpu_percent"]:
                worst["cpu_percent"] = float(cpu)
            if isinstance(mem, (int, float)) and mem > worst["mem_percent"]:
                worst["mem_percent"] = float(mem)
            if isinstance(disk, (int, float)) and disk > worst["disk_percent"]:
                worst["disk_percent"] = float(disk)

        text = "📈 <b>Мониторинг системных ресурсов</b>\n\n" + "\n".join(pieces)
        return text, worst

    async def _send_monitor_view(
        message: types.Message, edit_message: bool = False, without_db: bool = False
    ):
        metrics_data = None
        if without_db:
            try:
                metrics_data = await get_live_metrics_without_db()
            except Exception as e:
                print(f"Ошибка при общем сборе метрик: {e}")
        text, worst = await _format_monitor_metrics(metrics_data)
        suffix = ""
        warn_parts = []
        if worst["cpu_percent"] >= 85:
            warn_parts.append(f"CPU {worst['cpu_percent']:.0f}%")
        if worst["mem_percent"] >= 85:
            warn_parts.append(f"RAM {worst['mem_percent']:.0f}%")
        if worst["disk_percent"] >= 90:
            warn_parts.append(f"Disk {worst['disk_percent']:.0f}%")
        if warn_parts:
            suffix = "\n⚠️ <b>Внимание:</b> " + ", ".join(warn_parts) + ""
        keyboard = keyboards.create_admin_monitor_keyboard()
        full_text = text + suffix
        if edit_message:
            try:
                await message.edit_text(full_text, reply_markup=keyboard)
            except Exception:
                pass
        else:
            await message.answer(full_text, reply_markup=keyboard)

    @admin_router.callback_query(F.data == "admin_menu")
    async def open_admin_menu_handler(callback: types.CallbackQuery):
        if not is_admin(callback.from_user.id):
            await callback.answer("🚫 У вас нет прав", show_alert=True)
            return
        await callback.answer()
        await show_admin_menu(callback.message, edit_message=True)

    @admin_router.callback_query(F.data == "admin_monitor")
    async def admin_monitor_open(callback: types.CallbackQuery):
        if not is_admin(callback.from_user.id):
            await callback.answer("🚫 У вас нет прав", show_alert=True)
            return
        await callback.answer("⏳ Читаю метрики...", show_alert=False)
        await _send_monitor_view(callback.message, edit_message=True)

    @admin_router.callback_query(F.data == "admin_monitor_refresh")
    async def admin_monitor_refresh(callback: types.CallbackQuery):
        global last_refresh_time
        if not is_admin(callback.from_user.id):
            await callback.answer("🚫 У вас нет прав", show_alert=True)
            return
        now = time.time()
        if now - last_refresh_time < 5:
            await callback.answer("⚠️ Подождите перед следующим обновлением", show_alert=False)
            return
        last_refresh_time = now
        await callback.answer("⏳ Обновляю метрики...", show_alert=False)
        await _send_monitor_view(callback.message, edit_message=True, without_db=True)

    # --- Speedtest: кнопка в админ-меню -> выбор хоста ---
    @admin_router.callback_query(F.data == "admin_speedtest")
    async def admin_speedtest_entry(callback: types.CallbackQuery):
        if not is_admin(callback.from_user.id):
            await callback.answer("🚫 У вас нет прав", show_alert=True)
            return
        await callback.answer()
        hosts = await asyncio.to_thread(get_all_hosts) or []
        if not hosts:
            await callback.message.answer("⚠️ Хосты не найдены в настройках")
            return
        await safe_edit_text(callback, 
            "<b>⚡ Выберите сервер для теста скорости:</b>",
            reply_markup=keyboards.create_admin_hosts_pick_keyboard(hosts, action="speedtest"),
            parse_mode="HTML",
        )

    # --- Speedtest: запуск по выбранному хосту ---
    @admin_router.callback_query(F.data.startswith("admin_speedtest_pick_host_"))
    async def admin_speedtest_run(callback: types.CallbackQuery):
        if not is_admin(callback.from_user.id):
            await callback.answer("🚫 У вас нет прав", show_alert=True)
            return
        await callback.answer()
        token = callback.data[len("admin_speedtest_pick_host_") :]

        # Находим реальный сервер по токену
        all_hosts = await asyncio.to_thread(get_all_hosts) or []
        host = keyboards.find_host_by_callback_token(all_hosts, token)
        if not host:
            await callback.answer("❌ Сервер не найден", show_alert=False)
            return
        host_name = host["host_name"]

        # Уведомление всем администраторам о старте
        try:
            from shop_bot.data_manager.database import get_admin_ids

            admin_ids = list({*(get_admin_ids() or []), int(callback.from_user.id)})
        except Exception:
            admin_ids = [int(callback.from_user.id)]
        initiator = _format_user_mention(callback.from_user)
        start_text = (
            f"🚀 Запущен тест скорости для сервера <b>{host_name}</b>\n(инициатор: {initiator})"
        )
        for aid in admin_ids:
            try:
                await callback.bot.send_message(aid, start_text)
            except Exception:
                pass

        # Локальный статус
        try:
            wait_msg = await callback.message.answer(
                f"⏳ Выполняю тест скорости для <b>{host_name}</b>..."
            )
        except Exception:
            wait_msg = None

        # Выполнить тест (SSH + NET) и сохранить в БД
        try:
            result = await speedtest_runner.run_both_for_host(host_name)
        except Exception as e:
            result = {"ok": False, "error": str(e), "details": {}}

        # Текст результата
        def fmt_part(title: str, d: dict | None) -> str:
            if not d:
                return f"❓ {title}: —"
            if not d.get("ok"):
                return f"🔴 {title}: {d.get('error') or 'ошибка'}"
            ping = d.get("ping_ms")
            down = d.get("download_mbps")
            up = d.get("upload_mbps")
            srv = d.get("server_name") or "—"
            return (
                f"🟢 {title}:\n"
                f"▪️⏳ {ping if ping is not None else '—'} ms\n"
                f"▪️⬇️ {down if down is not None else '—'} Mbps\n"
                f"▪️⬆️ {up if up is not None else '—'} Mbps\n"
                f"▪️🗺️ {srv}"
            )

        details = result.get("details") or {}
        text_res = (
            f"🏁 Тест скорости завершён для <b>{host_name}</b>\n\n"
            + fmt_part("SSH", details.get("ssh"))
            + "\n\n"
            + fmt_part("NET", details.get("net"))
        )

        # Локально обновим сообщение
        if wait_msg:
            try:
                await wait_msg.edit_text(text_res)
            except Exception:
                await callback.message.answer(text_res)
        else:
            await callback.message.answer(text_res)

        # Разослать финал всем админам
        for aid in admin_ids:
            if wait_msg and aid == callback.from_user.id:
                continue
            try:
                await callback.bot.send_message(aid, text_res)
            except Exception:
                pass

    # --- Speedtest: Назад из выбора хоста ---
    @admin_router.callback_query(F.data == "admin_speedtest_back_to_users")
    async def admin_speedtest_back(callback: types.CallbackQuery):
        if not is_admin(callback.from_user.id):
            await callback.answer("🚫 У вас нет прав", show_alert=True)
            return
        await callback.answer()
        await show_admin_menu(callback.message, edit_message=True)

    # --- Speedtest: Запуск для всех хостов ---
    @admin_router.callback_query(F.data == "admin_speedtest_run_all")
    async def admin_speedtest_run_all(callback: types.CallbackQuery):
        if not is_admin(callback.from_user.id):
            await callback.answer("🚫 У вас нет прав", show_alert=True)
            return
        await callback.answer()
        # Оповещение админам
        try:
            from shop_bot.data_manager.database import get_admin_ids

            admin_ids = list({*(get_admin_ids() or []), int(callback.from_user.id)})
        except Exception:
            admin_ids = [int(callback.from_user.id)]
        initiator = _format_user_mention(callback.from_user)
        start_text = f"🚀 Запущен тест скорости для всех серверов\n(инициатор: {initiator})"
        for aid in admin_ids:
            try:
                await callback.bot.send_message(aid, start_text)
            except Exception:
                pass
        # Пробежимся по хостам
        hosts = await asyncio.to_thread(get_all_hosts) or []
        summary_lines = []
        for h in hosts:
            name = h.get("host_name")
            try:
                res = await speedtest_runner.run_both_for_host(name)
                ok = res.get("ok")
                det = res.get("details") or {}
                dm = det.get("ssh", {}).get("download_mbps") or det.get("net", {}).get(
                    "download_mbps"
                )
                um = det.get("ssh", {}).get("upload_mbps") or det.get("net", {}).get("upload_mbps")
                summary_lines.append(
                    f"🗺️ <b>{name}</b>\n{'🟢' if ok else '🔴'} SSH | ⬇️ {dm or '—'} Mbps | ⬆️ {um or '—'} Mbps\n"
                )
            except Exception as e:
                summary_lines.append(f"🗺️ <b>{name}</b>\n❌ {e}\n")
            await asyncio.sleep(5)
        text = "🏁 Тест для всех завершён:\n\n" + "\n".join(summary_lines)
        await callback.message.answer(text)
        for aid in admin_ids:
            # Не дублируем результат инициатору/в текущий чат
            if aid == callback.from_user.id or aid == callback.message.chat.id:
                continue
            try:
                await callback.bot.send_message(aid, text)
            except Exception:
                pass

    # --- Бэкап БД: ручной запуск ---
    @admin_router.callback_query(F.data == "admin_backup_db")
    async def admin_backup_db(callback: types.CallbackQuery):
        if not is_admin(callback.from_user.id):
            await callback.answer("🚫 У вас нет прав", show_alert=True)
            return
        await callback.answer()
        try:
            wait = await callback.message.answer("⏳ Создаю бэкап базы данных...")
        except Exception:
            wait = None
        zip_path = await asyncio.to_thread(backup_manager.create_backup_file)
        if not zip_path:
            if wait:
                await wait.edit_text("❌ Не удалось создать бэкап БД")
            else:
                await callback.message.answer("❌ Не удалось создать бэкап БД")
            return
        # Отправим всем администраторам
        try:
            sent = await backup_manager.send_backup_to_admins(callback.bot, zip_path)
        except Exception:
            sent = 0
        txt = f"✅ Бэкап создан: <b>{zip_path.name}</b>\nОтправлено администраторам: {sent}"
        if wait:
            try:
                await wait.edit_text(txt)
            except Exception:
                await callback.message.answer(txt)
        else:
            await callback.message.answer(txt)

    # --- Восстановление БД ---
    class AdminRestoreDB(StatesGroup):
        waiting_file = State()

    @admin_router.callback_query(F.data == "admin_restore_db")
    async def admin_restore_db_prompt(callback: types.CallbackQuery, state: FSMContext):
        if not is_admin(callback.from_user.id):
            await callback.answer("🚫 У вас нет прав", show_alert=True)
            return
        await callback.answer()
        await state.set_state(AdminRestoreDB.waiting_file)
        kb = InlineKeyboardBuilder()
        kb.button(text="❌ Отмена", callback_data="admin_cancel")
        kb.adjust(1)
        text = (
            "⚠️ <b>Восстановление базы данных</b>\n\n"
            "Отправьте файл <code>.zip</code> с бэкапом или файл <code>.db</code> в ответ на это сообщение.\n"
            "Текущая БД предварительно будет сохранена"
        )
        try:
            await safe_edit_text(callback, text, reply_markup=kb.as_markup())
        except Exception:
            await callback.message.answer(text, reply_markup=kb.as_markup())

    @admin_router.message(AdminRestoreDB.waiting_file)
    async def admin_restore_db_receive(message: types.Message, state: FSMContext):
        if not is_admin(message.from_user.id):
            return
        doc = message.document
        try:
            await message.delete()
        except Exception:
            pass
        if not doc:
            await message.answer("❌ Пришлите файл .zip или .db")
            return
        filename = (doc.file_name or "uploaded.db").lower()
        if not (filename.endswith(".zip") or filename.endswith(".db")):
            await message.answer("❌ Поддерживаются только файлы .zip или .db")
            return
        try:
            ts = datetime.now().strftime("%Y%m%d-%H%M%S")
            dest = backup_manager.BACKUPS_DIR / f"uploaded-{ts}-{filename}"
            dest.parent.mkdir(parents=True, exist_ok=True)
            await message.bot.download(doc, destination=dest)
        except Exception as e:
            await message.answer(f"❌ Не удалось скачать файл: {e}")
            return
        ok = backup_manager.restore_from_file(dest)
        await state.clear()
        if ok:
            await message.answer(
                "✅ Восстановление выполнено успешно.\nБот и панель продолжают работу с новой БД"
            )
        else:
            await message.answer("❌ Восстановление не удалось. Проверьте файл и повторите")

    # --- Speedtest: Автоустановка speedtest на выбранном хосте ---
    @admin_router.callback_query(F.data.startswith("admin_speedtest_autoinstall_"))
    async def admin_speedtest_autoinstall(callback: types.CallbackQuery):
        if not is_admin(callback.from_user.id):
            await callback.answer("🚫 У вас нет прав", show_alert=True)
            return
        await callback.answer()
        host_name = callback.data.replace("admin_speedtest_autoinstall_", "", 1)
        try:
            wait = await callback.message.answer(
                f"🛠 Пытаюсь установить speedtest на <b>{host_name}</b>..."
            )
        except Exception:
            wait = None
        from shop_bot.data_manager.speedtest_runner import (
            auto_install_speedtest_on_host,
        )

        try:
            res = await auto_install_speedtest_on_host(host_name)
        except Exception as e:
            res = {"ok": False, "log": f"Ошибка: {e}"}
        text = (
            "✅ Автоустановка завершена успешно"
            if res.get("ok")
            else "❌ Автоустановка завершилась с ошибкой"
        )
        safe_log = html.quote((res.get("log") or "")[:3500])
        text += f"\n<pre>{safe_log}</pre>"
        if wait:
            try:
                await wait.edit_text(text)
            except Exception:
                await callback.message.answer(text)
        else:
            await callback.message.answer(text)

    # --- Топ рефереров ---
    @admin_router.callback_query(F.data == "admin_top_referrers")
    async def admin_top_referrers_handler(callback: types.CallbackQuery):
        if not is_admin(callback.from_user.id):
            await callback.answer("🚫 У вас нет прав", show_alert=True)
            return
        await callback.answer()

        top = await asyncio.to_thread(get_top_referrers, 20)

        if not top:
            await safe_edit_text(
                callback,
                "🏆 <b>Топ рефереров</b>\n\nПока ни у кого нет реферальных начислений",
                reply_markup=keyboards.create_admin_top_referrers_keyboard(),
            )
            return

        medals = {0: "🥇", 1: "🥈", 2: "🥉"}
        lines = ["🏆 <b>Топ рефереров</b>\n"]
        for i, row in enumerate(top):
            prefix = medals.get(i, f"{i + 1}.")
            telegram_id = row.get("telegram_id")
            user_tag = format_stored_username(row.get("username"), telegram_id, linked=False)
            earned = float(row.get("referral_balance_all") or 0)
            count = int(row.get("referral_count") or 0)
            lines.append(
                f"{prefix} 💰 <b>{earned:.2f} ₽</b> ▪️ 👤 {user_tag} ▪️ 📢 приглашено: {count}"
            )

        await safe_edit_text(
            callback,
            "\n".join(lines),
            reply_markup=keyboards.create_admin_top_referrers_keyboard(),
        )

    # --- Промокоды: меню ---
    @admin_router.callback_query(F.data == "admin_promo_menu")
    async def admin_promo_menu(callback: types.CallbackQuery):
        if not is_admin(callback.from_user.id):
            await callback.answer("🚫 У вас нет прав", show_alert=True)
            return
        await callback.answer()
        await safe_edit_text(callback, 
            "🎟 <b>Управление промокодами</b>",
            reply_markup=keyboards.create_admin_promos_menu_keyboard(),
        )

    # --- Промокоды: удаление всех (с подтверждением) ---
    @admin_router.callback_query(F.data == "admin_promo_delete_all")
    async def admin_promo_delete_all_ask(callback: types.CallbackQuery):
        if not is_admin(callback.from_user.id):
            await callback.answer("🚫 У вас нет прав", show_alert=True)
            return
        await callback.answer()
        promos = list_promo_codes(include_inactive=True) or []
        if not promos:
            await callback.answer("📋 Промокоды отсутствуют", show_alert=False)
            return
        await safe_edit_text(
            callback,
            f"⚠️ <b>Вы уверены, что хотите удалить все {len(promos)} промокод(ов)</b>?",
            reply_markup=keyboards.create_admin_promo_delete_all_confirm_keyboard(),
        )

    @admin_router.callback_query(F.data == "admin_promo_delete_all_confirm")
    async def admin_promo_delete_all_confirm(callback: types.CallbackQuery):
        if not is_admin(callback.from_user.id):
            await callback.answer("🚫 У вас нет прав", show_alert=True)
            return

        deleted_count = await asyncio.to_thread(delete_all_promo_codes)

        if deleted_count > 0:
            await callback.answer(f"✅ Удалено промокодов: {deleted_count}", show_alert=False)
            logger.info(f"Админ {callback.from_user.id} удалил все промокоды ({deleted_count} шт.)")
        else:
            await callback.answer("❌ Промокоды не найдены или произошла ошибка", show_alert=False)

        await safe_edit_text(
            callback,
            "🎟 <b>Управление промокодами</b>",
            reply_markup=keyboards.create_admin_promos_menu_keyboard(),
        )

    # --- Промокоды: список с пагинацией ---
    PROMO_PAGE_SIZE = 5

    def _fmt_promo_date(raw):
        if not raw:
            return None
        try:
            return datetime.strptime(
                str(raw).split(".")[0][:19], "%Y-%m-%d %H:%M:%S"
            ).strftime("%d.%m.%Y")
        except Exception:
            try:
                return datetime.strptime(str(raw)[:10], "%Y-%m-%d").strftime("%d.%m.%Y")
            except Exception:
                return str(raw)

    def _format_promo_entry(p: dict) -> str:
        code = p.get("code")
        used_total = (
            p.get("used_total") if p.get("used_total") is not None else p.get("used_count", 0)
        )
        limit_total = p.get("usage_limit_total")
        vu = _fmt_promo_date(p.get("valid_until") or p.get("valid_to"))

        if p.get("discount_percent"):
            disc = f"скидка {float(p.get('discount_percent')):.0f}%"
        elif p.get("discount_amount"):
            disc = f"скидка {float(p.get('discount_amount')):.2f} ₽"
        else:
            disc = "без скидки"

        exhausted = bool(limit_total) and used_total >= limit_total
        if limit_total:
            usage_part = f"{used_total} / {limit_total}" + (" ▪️ исчерпан" if exhausted else "")
        else:
            usage_part = f"использован {used_total} раз" if used_total else "не использован"
        date_part = f"до {vu}" if vu else "без ограничений"

        return f"🎟 <b>{code}</b> ▪️ {disc}\n{usage_part} ▪️ {date_part}"

    def _build_promo_list_page(promos: list[dict], page: int) -> tuple[str, InlineKeyboardBuilder]:
        total = len(promos)
        total_pages = max(1, (total + PROMO_PAGE_SIZE - 1) // PROMO_PAGE_SIZE)
        page = max(0, min(page, total_pages - 1))
        start = page * PROMO_PAGE_SIZE
        page_promos = promos[start : start + PROMO_PAGE_SIZE]

        if not promos:
            text = "📋 Промокоды отсутствуют."
        else:
            entries = [_format_promo_entry(p) for p in page_promos]
            header = f"🎟 <b>Промокоды</b> ▪️ стр. {page + 1}/{total_pages} ▪️ всего {total}"
            text = header + "\n\n" + "\n\n".join(entries)

        kb = InlineKeyboardBuilder()
        for p in page_promos:
            code = p.get("code")
            is_act = p.get("is_active") if "is_active" in p else p.get("active", 1)
            label = f"{'🚫 Выключить' if is_act else '✅ Включить'} {code}"
            label_del = f"🗑️ Удалить {code}"
            kb.button(text=label, callback_data=f"admin_promo_toggle_{code}")
            kb.button(text=label_del, callback_data=f"admin_delete_promo_toggle_{code}")
        rows = [2] * len(page_promos)

        nav_buttons = []
        if page > 0:
            nav_buttons.append(("⬅️ Назад", f"admin_promo_list_page_{page - 1}"))
        if page < total_pages - 1:
            nav_buttons.append(("Вперёд ➡️", f"admin_promo_list_page_{page + 1}"))
        for label, cb in nav_buttons:
            kb.button(text=label, callback_data=cb)
        if nav_buttons:
            rows.append(len(nav_buttons))

        kb.button(text="⬅️ В меню промокодов", callback_data="admin_promo_menu")
        kb.button(text="⬅️ В админ-меню", callback_data="admin_menu")
        rows.extend([1, 1])
        kb.adjust(*rows)

        return text, kb

    @admin_router.callback_query(F.data == "admin_promo_list")
    async def admin_promo_list(callback: types.CallbackQuery, skip_answer: bool = False):
        if not is_admin(callback.from_user.id):
            await callback.answer("🚫 У вас нет прав", show_alert=True)
            return
        if not skip_answer:
            await callback.answer()
        promos = list_promo_codes(include_inactive=True) or []
        text, kb = _build_promo_list_page(promos, page=0)
        try:
            await safe_edit_text(callback, text, reply_markup=kb.as_markup())
        except Exception:
            await callback.message.answer(text, reply_markup=kb.as_markup())

    @admin_router.callback_query(F.data.startswith("admin_promo_list_page_"))
    async def admin_promo_list_page_nav(callback: types.CallbackQuery):
        if not is_admin(callback.from_user.id):
            await callback.answer("🚫 У вас нет прав", show_alert=True)
            return
        await callback.answer()
        try:
            page = int(callback.data.split("_")[-1])
        except Exception:
            page = 0
        promos = list_promo_codes(include_inactive=True) or []
        text, kb = _build_promo_list_page(promos, page=page)
        await safe_edit_text(callback, text, reply_markup=kb.as_markup())

    @admin_router.callback_query(F.data.startswith("admin_delete_promo_toggle_"))
    async def admin_delete_promo_toggle(callback: types.CallbackQuery):
        """
        Обработчик callback-запроса для удаления промокода администратором.

        Логика работы:
        1. Проверяет, является ли пользователь администратором
        2. Извлекает код промокода из callback.data
        3. Вызывает функцию удаления промокода из базы данных
        4. В случае успеха:
           - показывает всплывающее уведомление об успехе
           - обновляет список промокодов (редактирует текущее сообщение)
        5. В случае неудачи — показывает ошибку во всплывающем уведомлении

        Формат callback.data:
        • admin_promo_delete_ABC123 → промокод = ABC123

        Args:
            callback: CallbackQuery от aiogram

        Особенности:
        • Использует show_alert=False → уведомление не блокирует интерфейс
        • После успешного удаления автоматически обновляет меню промокодов
        • Защищена от доступа не-админов
        • Все действия логируются через database.delete_promo_code
        """
        # -------------------------------------------------------------------------
        # 1. Проверка прав администратора
        # -------------------------------------------------------------------------
        if not is_admin(callback.from_user.id):
            await callback.answer("🚫 У вас нет прав", show_alert=True)
            return

        # -------------------------------------------------------------------------
        # 2. Извлечение кода промокода из callback.data
        # -------------------------------------------------------------------------
        promo_code = callback.data.replace("admin_delete_promo_toggle_", "").strip()

        if not promo_code:
            await callback.answer("❌ Не удалось определить промокод", show_alert=False)
            logger.warning(f"Попытка удаления пустого промокода от админа {callback.from_user.id}")
            return

        # -------------------------------------------------------------------------
        # 3. Попытка удаления промокода из базы данных
        # -------------------------------------------------------------------------
        success = await asyncio.to_thread(database.delete_promo_code, promo_code)

        if success:
            # -------------------------------------------------------------------------
            # 4. Успешное удаление → уведомление + обновление списка
            # -------------------------------------------------------------------------
            await callback.answer(f"✅ Промокод {promo_code} удалён", show_alert=False)

            logger.info(f"Админ {callback.from_user.id} удалил промокод {promo_code}")

            # Возвращаемся в список промокодов (редактируем текущее сообщение)
            await admin_promo_list(callback, skip_answer=True)

        else:
            # -------------------------------------------------------------------------
            # 5. Не удалось удалить (промокод не найден или ошибка БД)
            # -------------------------------------------------------------------------
            await callback.answer(
                f"❌ Ошибка при удалении промокода {promo_code}", show_alert=False
            )

            logger.warning(
                f"Не удалось удалить промокод {promo_code} " f"(админ {callback.from_user.id})"
            )

    @admin_router.callback_query(F.data.startswith("admin_promo_toggle_"))
    async def admin_promo_toggle(callback: types.CallbackQuery):
        if not is_admin(callback.from_user.id):
            await callback.answer("🚫 У вас нет прав", show_alert=True)
            return
        code = callback.data.replace("admin_promo_toggle_", "", 1)
        try:
            p = get_promo_code(code)
            if not p:
                await callback.answer("❌ Промокод не найден", show_alert=False)
                return
            current = p.get("is_active") if "is_active" in p else p.get("active", 1)
            ok = update_promo_code_status(code, is_active=(0 if current else 1))
            if ok:
                await callback.answer(
                    f"✅ Промокод {code} {'деактивирован' if current else 'активирован'}",
                    show_alert=False,
                )
            else:
                await callback.answer("❌ Не удалось изменить статус", show_alert=False)
        except Exception as e:
            await callback.answer(f"❌ Ошибка: {e}", show_alert=False)
        # Обновим список (callback уже отвечен выше)
        await admin_promo_list(callback, skip_answer=True)

    # --- Промокоды: создание (мастер) ---
    class PromoCreate(StatesGroup):
        waiting_code = State()
        waiting_discount = State()  # Percent:10 или amount:100
        waiting_limits = State()  # Total=100;per_user=1 (опционально)
        waiting_dates = State()  # From=YYYY-MM-DD;until=YYYY-MM-DD (опционально)
        waiting_custom_days = State()  # Ручной ввод количества дней
        waiting_description = State()
        waiting_confirmation = State()

    @admin_router.callback_query(F.data == "admin_promo_create")
    async def admin_promo_create_start(callback: types.CallbackQuery, state: FSMContext):
        if not is_admin(callback.from_user.id):
            await callback.answer("🚫 У вас нет прав", show_alert=True)
            return
        await callback.answer()
        await state.set_state(PromoCreate.waiting_code)
        await safe_edit_text(callback, 
            '⌨️ <b>Введите код промокода (латиница/цифры) или нажмите "Сгенерировать":</b>',
            reply_markup=keyboards.create_admin_promo_code_keyboard(),
            parse_mode="HTML",
        )

    @admin_router.callback_query(PromoCreate.waiting_code, F.data == "admin_promo_gen_code")
    async def admin_promo_generate_code(callback: types.CallbackQuery, state: FSMContext):
        if not is_admin(callback.from_user.id):
            await callback.answer("🚫 У вас нет прав", show_alert=True)
            return
        await callback.answer("✅ Сгенерировано", show_alert=False)
        alphabet = string.ascii_uppercase + string.digits
        code = "".join(secrets.choice(alphabet) for _ in range(8))
        await state.update_data(code=code)
        await state.set_state(PromoCreate.waiting_discount)
        await safe_edit_text(callback, 
            f"🎟️ <b>Код:</b> <code>{code}</code>\n\nУкажите скидку",
            reply_markup=keyboards.create_admin_promo_discount_keyboard(),
            parse_mode="HTML",
        )

    @admin_router.message(PromoCreate.waiting_code)
    async def promo_create_code(message: types.Message, state: FSMContext):
        code = (message.text or "").strip().upper()
        try:
            await message.delete()
        except Exception:
            pass
        if not code or len(code) < 2:
            await message.answer("❌ Код слишком короткий. Повторите ввод")
            return
        await state.update_data(code=code)
        await state.set_state(PromoCreate.waiting_discount)
        await message.answer(
            "💯 <b>Укажите скидку</b>",
            reply_markup=keyboards.create_admin_promo_discount_keyboard(),
        )

    # Быстрые кнопки выбора скидки
    @admin_router.callback_query(
        PromoCreate.waiting_discount, F.data.startswith("admin_promo_discount_")
    )
    async def promo_create_discount_buttons(callback: types.CallbackQuery, state: FSMContext):
        if not is_admin(callback.from_user.id):
            await callback.answer("🚫 У вас нет прав", show_alert=True)
            return
        await callback.answer()
        data = callback.data
        perc = None
        amt = None
        # Меню выбора типа
        if data == "admin_promo_discount_type_percent":
            await safe_edit_text(callback, 
                "💯 <b>Выберите процент скидки или введите вручную:</b>",
                reply_markup=keyboards.create_admin_promo_discount_percent_menu_keyboard(),
                parse_mode="HTML",
            )
            return
        if data == "admin_promo_discount_type_amount":
            await safe_edit_text(callback, 
                "💯 <b>Выберите фиксированную сумму скидки (₽) или введите вручную:</b>",
                reply_markup=keyboards.create_admin_promo_discount_amount_menu_keyboard(),
                parse_mode="HTML",
            )
            return
        # Переключатели меню
        if data == "admin_promo_discount_show_amount_menu":
            await safe_edit_text(callback, 
                "💯 <b>Выберите фиксированную сумму скидки (₽) или введите вручную:</b>",
                reply_markup=keyboards.create_admin_promo_discount_amount_menu_keyboard(),
                parse_mode="HTML",
            )
            return
        if data == "admin_promo_discount_show_percent_menu":
            await safe_edit_text(callback, 
                "💯 <b>Выберите процент скидки или введите вручную:</b>",
                reply_markup=keyboards.create_admin_promo_discount_percent_menu_keyboard(),
                parse_mode="HTML",
            )
            return
        # Ручной ввод
        if data == "admin_promo_discount_manual_percent":
            await state.update_data(manual_discount_mode="percent")
            await safe_edit_text(callback, 
                "💯 <b>Введите процент скидки (например, 10). Можно также в формате percent:10</b>",
                reply_markup=keyboards.create_admin_cancel_keyboard(),
                parse_mode="HTML",
            )
            return
        if data == "admin_promo_discount_manual_amount":
            await state.update_data(manual_discount_mode="amount")
            await safe_edit_text(callback, 
                "💯 <b>Введите фиксированную сумму скидки в ₽ (например, 100). Можно также amount:100</b>",
                reply_markup=keyboards.create_admin_cancel_keyboard(),
                parse_mode="HTML",
            )
            return
        # Пресеты
        if data.startswith("admin_promo_discount_percent_"):
            try:
                perc = float(data.rsplit("_", 1)[-1])
            except Exception:
                perc = 10.0
        elif data.startswith("admin_promo_discount_amount_"):
            try:
                amt = float(data.rsplit("_", 1)[-1])
            except Exception:
                amt = 50.0
        # Сохраняем и идём дальше
        await state.update_data(
            discount_percent=perc,
            discount_amount=amt,
            manual_discount_mode=None,
            usage_limit_total=None,
            usage_limit_per_user=None,
            limits_manual_input=None,
            limits_both=False,
        )
        await state.set_state(PromoCreate.waiting_limits)
        await safe_edit_text(callback, 
            "🚧 <b>Лимиты (опционально)</b>",
            reply_markup=keyboards.create_admin_promo_limits_type_keyboard(),
            parse_mode="HTML",
        )

    @admin_router.message(PromoCreate.waiting_discount)
    async def promo_create_discount(message: types.Message, state: FSMContext):
        text = (message.text or "").strip().lower()
        try:
            await message.delete()
        except Exception:
            pass
        perc = None
        amt = None
        data = await state.get_data()
        manual_mode = (data.get("manual_discount_mode") or "").strip()
        try:
            if text.startswith("percent:"):
                perc = float(text.split(":", 1)[1].strip())
            elif text.startswith("amount:"):
                amt = float(text.split(":", 1)[1].strip())
            elif manual_mode == "percent" and re.match(r"^\d+(\.\d+)?$", text):
                perc = float(text)
            elif manual_mode == "amount" and re.match(r"^\d+(\.\d+)?$", text):
                amt = float(text)
            else:
                await message.answer(
                    "❌ Формат не распознан. Введите число или percent:10/amount:100"
                )
                return
        except Exception:
            await message.answer("❌ Не удалось прочитать число. Повторите ввод")
            return
        await state.update_data(
            discount_percent=perc,
            discount_amount=amt,
            usage_limit_total=None,
            usage_limit_per_user=None,
            limits_manual_input=None,
            limits_both=False,
        )
        await state.set_state(PromoCreate.waiting_limits)
        await message.answer(
            "🚧 <b>Лимиты (опционально)</b>",
            reply_markup=keyboards.create_admin_promo_limits_type_keyboard(),
            parse_mode="HTML",
        )

    # Кнопки для лимитов (новое меню)
    @admin_router.callback_query(
        PromoCreate.waiting_limits, F.data.startswith("admin_promo_limits_")
    )
    async def promo_create_limits_buttons(callback: types.CallbackQuery, state: FSMContext):
        if not is_admin(callback.from_user.id):
            await callback.answer("🚫 У вас нет прав", show_alert=True)
            return
        await callback.answer()
        data = await state.get_data()
        # Тип выбора
        if callback.data == "admin_promo_limits_type_total":
            await state.update_data(limits_both=False)
            await safe_edit_text(callback, 
                "🚧 <b>Общий лимит — выберите значение:</b>",
                reply_markup=keyboards.create_admin_promo_limits_total_keyboard(),
                parse_mode="HTML",
            )
            return
        if callback.data == "admin_promo_limits_type_per":
            await state.update_data(limits_both=False)
            await safe_edit_text(callback, 
                "🚧 <b>Лимит на пользователя — выберите значение:</b>",
                reply_markup=keyboards.create_admin_promo_limits_per_user_keyboard(),
                parse_mode="HTML",
            )
            return
        if callback.data == "admin_promo_limits_type_both":
            await state.update_data(
                limits_both=True, usage_limit_total=None, usage_limit_per_user=None
            )
            await safe_edit_text(callback, 
                "🚧 <b>Сначала укажите общий лимит:</b>",
                reply_markup=keyboards.create_admin_promo_limits_total_keyboard(),
                parse_mode="HTML",
            )
            return
        if callback.data == "admin_promo_limits_back_to_type":
            await safe_edit_text(callback, 
                "🚧 <b>Лимиты (опционально)</b>",
                reply_markup=keyboards.create_admin_promo_limits_type_keyboard(),
                parse_mode="HTML",
            )
            return
        if callback.data == "admin_promo_limits_skip":
            await state.set_state(PromoCreate.waiting_dates)
            await safe_edit_text(callback, 
                "📅 <b>Даты (опционально)</b>",
                reply_markup=keyboards.create_admin_promo_dates_keyboard(),
                parse_mode="HTML",
            )
            return
        # Пресеты TOTAL
        if callback.data.startswith("admin_promo_limits_total_preset_"):
            try:
                total = int(callback.data.rsplit("_", 1)[-1])
            except Exception:
                total = None
            await state.update_data(usage_limit_total=total)
            if data.get("limits_both"):
                await safe_edit_text(callback, 
                    "🚧 <b>Теперь укажите лимит на пользователя:</b>",
                    reply_markup=keyboards.create_admin_promo_limits_per_user_keyboard(),
                    parse_mode="HTML",
                )
                return
            # Один лимит — дальше к датам
            await state.set_state(PromoCreate.waiting_dates)
            await safe_edit_text(callback, 
                "📅 <b>Даты (опционально)</b>",
                reply_markup=keyboards.create_admin_promo_dates_keyboard(),
                parse_mode="HTML",
            )
            return
        # Пресеты PER USER
        if callback.data.startswith("admin_promo_limits_per_preset_"):
            try:
                per_user = int(callback.data.rsplit("_", 1)[-1])
            except Exception:
                per_user = None
            await state.update_data(usage_limit_per_user=per_user)
            if data.get("limits_both") and data.get("usage_limit_total") is None:
                # Если вдруг пришли сюда без тотала
                await safe_edit_text(callback, 
                    "🚧 <b>Сначала укажите общий лимит:</b>",
                    reply_markup=keyboards.create_admin_promo_limits_total_keyboard(),
                    parse_mode="HTML",
                )
                return
            await state.set_state(PromoCreate.waiting_dates)
            await safe_edit_text(callback, 
                "📅 <b>Даты (опционально)</b>",
                reply_markup=keyboards.create_admin_promo_dates_keyboard(),
                parse_mode="HTML",
            )
            return
        # Ручной ввод: переключаемся на ввод числа
        if callback.data == "admin_promo_limits_total_manual":
            await state.update_data(limits_manual_input="total")
            await safe_edit_text(callback, 
                "🚧 <b>Введите общий лимит (целое число):</b>",
                reply_markup=keyboards.create_admin_cancel_keyboard(),
                parse_mode="HTML",
            )
            return
        if callback.data == "admin_promo_limits_per_manual":
            await state.update_data(limits_manual_input="per")
            await safe_edit_text(callback, 
                "🚧 <b>Введите лимит на пользователя (целое число):</b>",
                reply_markup=keyboards.create_admin_cancel_keyboard(),
                parse_mode="HTML",
            )
            return

    @admin_router.message(PromoCreate.waiting_limits)
    async def promo_create_limits(message: types.Message, state: FSMContext):
        text = (message.text or "").strip()
        try:
            await message.delete()
        except Exception:
            pass
        data = await state.get_data()
        manual = (data.get("limits_manual_input") or "").strip()
        if not manual:
            await message.answer(
                "⚠️ <b>Пожалуйста, выберите вариант на клавиатуре</b>",
                reply_markup=keyboards.create_admin_promo_limits_type_keyboard(),
            )
            return
        # Ручной ввод числа
        try:
            val = int(text)
            if val <= 0:
                raise ValueError()
        except Exception:
            await message.answer("❌ Введите положительное целое число")
            return
        if manual == "total":
            await state.update_data(usage_limit_total=val, limits_manual_input=None)
            if data.get("limits_both"):
                await message.answer(
                    "🚧 <b>Теперь укажите лимит на пользователя:</b>",
                    reply_markup=keyboards.create_admin_promo_limits_per_user_keyboard(),
                    parse_mode="HTML",
                )
                return
        elif manual == "per":
            await state.update_data(usage_limit_per_user=val, limits_manual_input=None)
        # Переход к датам
        await state.set_state(PromoCreate.waiting_dates)
        await message.answer(
            "📅 <b>Даты (опционально)</b>",
            reply_markup=keyboards.create_admin_promo_dates_keyboard(),
            parse_mode="HTML",
        )

    @admin_router.message(PromoCreate.waiting_dates)
    async def promo_create_dates(message: types.Message, state: FSMContext):
        text = (message.text or "").strip()
        try:
            await message.delete()
        except Exception:
            pass
        vf = None
        vu = None
        if text:
            parts = [p.strip() for p in text.split(";") if p.strip()]
            for p in parts:
                if p.startswith("from="):
                    vf = p.split("=", 1)[1].strip()
                elif p.startswith("until="):
                    vu = p.split("=", 1)[1].strip()

        # Попробуем привести к isoформату, если это YYYY-MM-DD
        def _to_iso(d: str | None) -> str | None:
            if not d:
                return None
            try:
                if len(d) == 10 and d.count("-") == 2:
                    return datetime.fromisoformat(d).isoformat()
                # Если админ дал уже iso, просто вернём
                datetime.fromisoformat(d)
                return d
            except Exception:
                return None

        await state.update_data(valid_from=_to_iso(vf), valid_until=_to_iso(vu))
        await state.set_state(PromoCreate.waiting_description)
        await message.answer(
            "ℹ️ <b>Описание (опционально). Введите текст или оставьте пустым:</b>",
            reply_markup=keyboards.create_admin_promo_description_keyboard(),
            parse_mode="HTML",
        )

    # Кнопки дат
    @admin_router.callback_query(PromoCreate.waiting_dates, F.data.startswith("admin_promo_dates_"))
    async def promo_create_dates_buttons(callback: types.CallbackQuery, state: FSMContext):
        if not is_admin(callback.from_user.id):
            await callback.answer("🚫 У вас нет прав", show_alert=True)
            return
        await callback.answer()
        now = datetime.now()
        vf_iso = None
        vu_iso = None
        if callback.data == "admin_promo_dates_skip":
            pass
        elif callback.data == "admin_promo_dates_week":
            vf_iso = now.isoformat()
            vu_iso = (now + timedelta(days=7)).isoformat()
        elif callback.data == "admin_promo_dates_month":
            vf_iso = now.isoformat()
            vu_iso = (
                datetime.now()
                + timedelta(days=calendar.monthrange(datetime.now().year, datetime.now().month)[1])
            ).isoformat()
        elif callback.data.startswith("admin_promo_dates_days_"):
            try:
                days = int(callback.data.rsplit("_", 1)[-1])
                if days <= 0:
                    raise ValueError()
            except Exception:
                days = 7
            vf_iso = now.isoformat()
            vu_iso = (now + timedelta(days=days)).isoformat()
        elif callback.data == "admin_promo_dates_custom_days":
            # Переходим на ручной ввод
            await state.set_state(PromoCreate.waiting_custom_days)
            await safe_edit_text(callback, 
                "📅 <b>Введите число дней действия промокода (например, 14):</b>",
                reply_markup=keyboards.create_admin_cancel_keyboard(),
                parse_mode="HTML",
            )
            return
        await state.update_data(valid_from=vf_iso, valid_until=vu_iso)
        await state.set_state(PromoCreate.waiting_description)
        await safe_edit_text(callback, 
            "ℹ️ <b>Описание (опционально). Введите текст или оставьте пустым:</b>",
            reply_markup=keyboards.create_admin_promo_description_keyboard(),
            parse_mode="HTML",
        )

    # Ручной ввод количества дней
    @admin_router.message(PromoCreate.waiting_custom_days)
    async def promo_create_dates_custom_days(message: types.Message, state: FSMContext):
        text = (message.text or "").strip()
        try:
            await message.delete()
        except Exception:
            pass
        try:
            days = int(text)
            if days <= 0 or days > 3650:
                raise ValueError()
        except Exception:
            await message.answer("❌ Введите целое число дней (1–3650)")
            return
        now = datetime.now()
        vf_iso = now.isoformat()
        vu_iso = (now + timedelta(days=days)).isoformat()
        await state.update_data(valid_from=vf_iso, valid_until=vu_iso)
        await state.set_state(PromoCreate.waiting_description)
        await message.answer(
            "ℹ️ <b>Описание (опционально). Введите текст или оставьте пустым:</b>",
            reply_markup=keyboards.create_admin_promo_description_keyboard(),
            parse_mode="HTML",
        )

    @admin_router.message(PromoCreate.waiting_description)
    async def promo_create_finish(message: types.Message, state: FSMContext):
        desc = (message.text or "").strip() or None
        try:
            await message.delete()
        except Exception:
            pass
        await state.update_data(description=desc)
        await state.set_state(PromoCreate.waiting_confirmation)
        await _send_promo_summary(message, state, edit=False)

    # Кнопка пропуска описания -> показать сводку
    @admin_router.callback_query(PromoCreate.waiting_description, F.data == "admin_promo_desc_skip")
    async def promo_create_finish_skip(callback: types.CallbackQuery, state: FSMContext):
        if not is_admin(callback.from_user.id):
            await callback.answer("🚫 У вас нет прав", show_alert=True)
            return
        await callback.answer()
        await state.update_data(description=None)
        await state.set_state(PromoCreate.waiting_confirmation)
        await _send_promo_summary(callback.message, state, edit=True)

    # Подтверждение создания
    @admin_router.callback_query(
        PromoCreate.waiting_confirmation, F.data == "admin_promo_confirm_create"
    )
    async def promo_confirm_create(callback: types.CallbackQuery, state: FSMContext):
        if not is_admin(callback.from_user.id):
            await callback.answer("🚫 У вас нет прав", show_alert=True)
            return
        await callback.answer("⏳ Создаю...", show_alert=False)
        data = await state.get_data()
        try:
            ok = create_promo_code(
                data["code"],
                discount_percent=data.get("discount_percent"),
                discount_amount=data.get("discount_amount"),
                usage_limit_total=data.get("usage_limit_total"),
                usage_limit_per_user=data.get("usage_limit_per_user"),
                valid_from=(
                    datetime.fromisoformat(data["valid_from"]) if data.get("valid_from") else None
                ),
                valid_until=(
                    datetime.fromisoformat(data["valid_until"]) if data.get("valid_until") else None
                ),
                description=data.get("description"),
            )
        except Exception:
            ok = False
        await state.clear()
        await safe_edit_text(callback, 
            ("✅ Промокод создан" if ok else "❌ Не удалось создать промокод"),
            reply_markup=keyboards.create_admin_promos_menu_keyboard(),
        )

    # Вспомогательное: отправка сводки
    async def _send_promo_summary(message_or_msg, state: FSMContext, edit: bool = False):
        data = await state.get_data()
        code = data.get("code") or "—"
        if data.get("discount_percent"):
            disc_txt = f"{float(data['discount_percent']):.0f}%"
        elif data.get("discount_amount"):
            disc_txt = f"{float(data['discount_amount']):.2f} ₽"
        else:
            disc_txt = "—"
        lim_total = data.get("usage_limit_total")
        lim_per = data.get("usage_limit_per_user")
        limits_txt = []
        if lim_total:
            limits_txt.append(f"total={lim_total}")
        if lim_per:
            limits_txt.append(f"per_user={lim_per}")
        limits_txt = ";".join(limits_txt) if limits_txt else "—"

        def _fmt_date(s):
            try:
                return datetime.fromisoformat(s).strftime("%Y-%m-%d")
            except Exception:
                return "—"

        dates_txt = "—"
        if data.get("valid_from") or data.get("valid_until"):
            dates_txt = (
                f"{_fmt_date(data.get('valid_from'))} → {_fmt_date(data.get('valid_until'))}"
            )
        desc = data.get("description") or "—"
        text = (
            "🎟 <b>Сводка промокода</b>\n\n"
            f"<b>Код:</b> <code>{code}</code>\n"
            f"<b>Скидка:</b> {disc_txt}\n"
            f"<b>Лимиты:</b> {limits_txt}\n"
            f"<b>Даты:</b> {dates_txt}\n"
            f"<b>Описание:</b> {html_escape.escape(desc) if desc != '—' else '—'}\n\n"
            "Подтвердите создание"
        )
        kb = keyboards.create_admin_promo_confirm_keyboard()
        if edit:
            try:
                await message_or_msg.edit_text(text, reply_markup=kb)
            except Exception:
                await message_or_msg.answer(text, reply_markup=kb)
        else:
            await message_or_msg.answer(text, reply_markup=kb)

    # --- Пользователи: список, пагинация, просмотр ---
    @admin_router.callback_query(F.data.startswith("admin_users"))
    async def admin_users_handler(callback: types.CallbackQuery, state: FSMContext):
        if not is_admin(callback.from_user.id):
            await callback.answer("🚫 У вас нет прав", show_alert=True)
            return
        await callback.answer()
        users = await asyncio.to_thread(get_all_users)
        page = 0
        if callback.data.startswith("admin_users_page_"):
            try:
                page = int(callback.data.split("_")[-1])
            except Exception:
                page = 0
        await safe_edit_text(callback, 
            "👥 <b>Пользователи</b>",
            reply_markup=keyboards.create_admin_users_keyboard(users, page=page),
        )

    @admin_router.callback_query(F.data.startswith("admin_view_user_"))
    async def admin_view_user_handler(callback: types.CallbackQuery):
        if not is_admin(callback.from_user.id):
            await callback.answer("🚫 У вас нет прав", show_alert=True)
            return
        await callback.answer()
        try:
            user_id = int(callback.data.split("_")[-1])
        except Exception:
            await callback.message.answer("❌ Неверный формат user_id")
            return
        await _render_user_card(
            callback.bot, callback.message.chat.id, callback.message.message_id, user_id
        )

    # --- Бан/разбан пользователя ---
    @admin_router.callback_query(F.data.startswith("admin_ban_user_"))
    async def admin_ban_user_prompt(callback: types.CallbackQuery):
        if not is_admin(callback.from_user.id):
            await callback.answer("🚫 У вас нет прав", show_alert=True)
            return
        await callback.answer()
        try:
            user_id = int(callback.data.split("_")[-1])
        except Exception:
            await callback.message.answer("❌ Неверный формат user_id")
            return
        user = await asyncio.to_thread(get_user, user_id) or {}
        who = format_stored_username(user.get("username"), user_id, linked=False)
        try:
            await safe_edit_text(callback, 
                f"⚠️ <b>Забанить пользователя {who}?</b>\n\n"
                f"Пользователь потеряет доступ к боту (получит уведомление со ссылкой на поддержку). Действие обратимо через «Разбанить»",
                reply_markup=keyboards.create_admin_ban_confirm_keyboard(user_id),
            )
        except TelegramBadRequest:
            pass

    @admin_router.callback_query(F.data.startswith("admin_confirm_ban_"))
    async def admin_ban_user_confirm(callback: types.CallbackQuery):
        if not is_admin(callback.from_user.id):
            await callback.answer("🚫 У вас нет прав", show_alert=True)
            return
        await callback.answer("⏳ Баню...", show_alert=False)
        try:
            user_id = int(callback.data.split("_")[-1])
        except Exception:
            await callback.message.answer("❌ Неверный формат user_id")
            return
        try:
            await asyncio.to_thread(ban_user, user_id)
            try:
                # Уведомление пользователю: только кнопка поддержки, без "Назад в меню"
                from shop_bot.data_manager.database import get_setting as _get_setting

                support = (
                    _get_setting("support_bot_username") or _get_setting("support_user") or ""
                ).strip()
                kb = InlineKeyboardBuilder()
                url = None
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
                    kb.button(text="🆘 Написать в поддержку", url=url)
                else:
                    kb.button(text="🆘 Поддержка", callback_data="show_help")
                await callback.bot.send_message(
                    user_id,
                    "🚫 <b>Ваш аккаунт заблокирован администратором. Если это ошибка — напишите в поддержку</b>",
                    reply_markup=kb.as_markup(),
                )
            except Exception:
                pass
        except Exception as e:
            await delete_and_send(callback.message, f"❌ Не удалось забанить пользователя: {e}")
            return
        await _render_user_card(
            callback.bot,
            callback.message.chat.id,
            callback.message.message_id,
            user_id,
            note="🚫 Пользователь забанен",
        )

    # --- Подменю администраторов ---
    @admin_router.callback_query(F.data == "admin_admins_menu")
    async def admin_admins_menu_entry(callback: types.CallbackQuery):
        if not is_admin(callback.from_user.id):
            await callback.answer("🚫 У вас нет прав", show_alert=True)
            return
        await callback.answer()
        await safe_edit_text(callback, 
            "👮 <b>Управление администраторами</b>",
            reply_markup=keyboards.create_admins_menu_keyboard(),
        )

    @admin_router.callback_query(F.data == "admin_view_admins")
    async def admin_view_admins(callback: types.CallbackQuery):
        if not is_admin(callback.from_user.id):
            await callback.answer("🚫 У вас нет прав", show_alert=True)
            return
        await callback.answer()
        try:
            from shop_bot.data_manager.database import get_admin_ids

            ids = list(get_admin_ids() or [])
        except Exception:
            ids = []
        if not ids:
            text = "📋 Список администраторов пуст"
        else:
            lines = []
            for aid in ids:
                try:
                    u = get_user(int(aid)) or {}
                except Exception:
                    u = {}
                tag = format_stored_username(u.get("username"))
                lines.append(f"▪️ID: {aid} — {tag}")
            text = "📋 <b>Администраторы</b>:\n" + "\n".join(lines)
        # Кнопки назад
        kb = InlineKeyboardBuilder()
        kb.button(text="⬅️ Назад", callback_data="admin_admins_menu")
        kb.button(text="⬅️ В админ-меню", callback_data="admin_menu")
        kb.adjust(1, 1)
        try:
            await safe_edit_text(callback, text, reply_markup=kb.as_markup())
        except Exception:
            await callback.message.answer(text, reply_markup=kb.as_markup())

    @admin_router.callback_query(F.data.startswith("admin_unban_user_"))
    async def admin_unban_user(callback: types.CallbackQuery):
        if not is_admin(callback.from_user.id):
            await callback.answer("🚫 У вас нет прав", show_alert=True)
            return
        await callback.answer("⏳ Разбаниваю...", show_alert=False)
        try:
            user_id = int(callback.data.split("_")[-1])
        except Exception:
            await callback.message.answer("❌ Неверный формат user_id")
            return
        try:
            await asyncio.to_thread(unban_user, user_id)
            try:
                # Отправляем пользователю уведомление о разбане с кнопкой в главное меню
                kb = InlineKeyboardBuilder()
                kb.row(keyboards.get_main_menu_button())
                await callback.bot.send_message(
                    user_id,
                    "✅ <b>Доступ к аккаунту восстановлен администратором</b>",
                    reply_markup=kb.as_markup(),
                )
            except Exception:
                pass
        except Exception as e:
            await delete_and_send(callback.message, f"❌ Не удалось разбанить пользователя: {e}")
            return
        await _render_user_card(
            callback.bot,
            callback.message.chat.id,
            callback.message.message_id,
            user_id,
            note="✅ Пользователь разбанен",
        )

    # --- Ключи пользователя: список и карточка ключа ---
    @admin_router.callback_query(F.data.startswith("admin_user_keys_"))
    async def admin_user_keys(callback: types.CallbackQuery):
        if not is_admin(callback.from_user.id):
            await callback.answer("🚫 У вас нет прав", show_alert=True)
            return
        await callback.answer()
        try:
            user_id = int(callback.data.split("_")[-1])
        except Exception:
            await callback.message.answer("❌ Неверный формат user_id")
            return
        keys = await asyncio.to_thread(get_keys_for_user, user_id)
        await safe_edit_text(callback, 
            f"🔑 <b>Подписки пользователя {user_id}:</b>",
            reply_markup=keyboards.create_admin_user_keys_keyboard(user_id, keys),
            parse_mode="HTML",
        )

    # --- История операций пользователя (для админа) ---
    @admin_router.callback_query(F.data.startswith("admin_tx_history_"))
    async def admin_tx_history_handler(callback: types.CallbackQuery):
        if not is_admin(callback.from_user.id):
            await callback.answer("🚫 У вас нет прав", show_alert=True)
            return
        await callback.answer()

        # Формат callback.data: admin_tx_history_{user_id}_{page}
        parts = callback.data.split("_")
        try:
            page = int(parts[-1])
            user_id = int(parts[-2])
        except Exception:
            await callback.message.answer("❌ Неверный формат запроса истории операций")
            return

        per_page = 5
        transactions, total = await asyncio.to_thread(
            get_user_transactions, user_id, page + 1, per_page
        )

        if not transactions:
            await safe_edit_text(
                callback,
                f"📜 <b>История операций пользователя {user_id}</b>\n\n"
                "Пока здесь пусто — здесь появятся пополнения, покупки и списания",
                reply_markup=keyboards.create_admin_transaction_history_keyboard(
                    user_id, page, has_prev=page > 0, has_next=False
                ),
                parse_mode="HTML",
            )
            return

        lines = [f"📜 <b>История операций пользователя {user_id}</b>"]
        for tx in transactions:
            lines.append(format_transaction_line(tx))

        text = "\n\n".join(lines)
        has_next = (page + 1) * per_page < total
        await safe_edit_text(
            callback,
            text,
            reply_markup=keyboards.create_admin_transaction_history_keyboard(
                user_id, page, has_prev=page > 0, has_next=has_next
            ),
            parse_mode="HTML",
        )

    @admin_router.callback_query(F.data.startswith("admin_user_referrals_"))
    async def admin_user_referrals(callback: types.CallbackQuery):
        if not is_admin(callback.from_user.id):
            await callback.answer("🚫 У вас нет прав", show_alert=True)
            return
        await callback.answer()
        try:
            user_id = int(callback.data.split("_")[-1])
        except Exception:
            await callback.message.answer("❌ Неверный формат user_id")
            return
        inviter = await asyncio.to_thread(get_user, user_id)
        if not inviter:
            await callback.message.answer("❌ Пользователь не найден")
            return
        refs = get_referrals_for_user(user_id) or []
        ref_count = len(refs)
        try:
            total_ref_earned = float(get_referral_balance_all(user_id) or 0)
        except Exception:
            total_ref_earned = 0.0
        # Сформируем список с ограничением по длине
        max_items = 30
        lines = []
        for r in refs[:max_items]:
            rid = r.get("telegram_id")
            uname = r.get("username") or "—"
            rdate = r.get("registration_date") or "—"
            spent = float(r.get("total_spent") or 0)
            lines.append(f"▪️@{uname} (ID: {rid}) — рег: {rdate}, потратил: {spent:.2f} ₽")
        more_suffix = (
            "\n... и ещё {}".format(ref_count - max_items) if ref_count > max_items else ""
        )
        text = (
            f"🤝 <b>Рефералы пользователя {user_id}</b>\n\n"
            f"<b>Всего приглашено:</b> {ref_count}\n"
            f"<b>Заработано по рефералке (всего):</b> {total_ref_earned:.2f} ₽\n\n"
            + ("\n".join(lines) if lines else "Пока нет рефералов")
            + more_suffix
        )
        # Кнопки: назад к карточке пользователя и в админ-меню
        kb = InlineKeyboardBuilder()
        kb.button(text="⬅️ К пользователю", callback_data=f"admin_view_user_{user_id}")
        kb.button(text="⬅️ В админ-меню", callback_data="admin_menu")
        kb.adjust(1, 1)
        try:
            await safe_edit_text(callback, text, reply_markup=kb.as_markup())
        except Exception:
            await callback.message.answer(text, reply_markup=kb.as_markup())

    @admin_router.callback_query(F.data.startswith("admin_edit_key_"))
    async def admin_edit_key(callback: types.CallbackQuery):
        if not is_admin(callback.from_user.id):
            await callback.answer("🚫 У вас нет прав", show_alert=True)
            return
        await callback.answer()
        try:
            key_id = int(callback.data.split("_")[-1])
        except Exception:
            await callback.message.answer("❌ Неверный формат key_id")
            return
        key = await asyncio.to_thread(get_key_by_id, key_id)
        if not key:
            await callback.message.answer("❌ Подписка не найдена")
            return
        text = (
            f"🔑 <b>Подписка № {key_id}</b>\n\n"
            f"🗺️ <b>Сервер:</b> {key.get('host_name') or '—'}\n"
            f"🆔 <b>Email:</b> {key.get('key_email') or '—'}\n"
            f"📅 <b>Истекает:</b> {format_dt_for_display(key.get('expiry_date'))}\n"
        )
        try:
            await safe_edit_text(callback, 
                text,
                reply_markup=keyboards.create_admin_key_actions_keyboard(
                    key_id,
                    int(key.get("user_id")) if key and key.get("user_id") else None,
                ),
            )
        except Exception as e:
            logger.debug(f"Не удалось изменить текст при отмене удаления ключа № {key_id}: {e}")
            await callback.message.answer(
                text,
                reply_markup=keyboards.create_admin_key_actions_keyboard(
                    key_id,
                    int(key.get("user_id")) if key and key.get("user_id") else None,
                ),
            )

    # --- Удаление ключа: подтверждение (prompt) ---
    # Матчим только вариант admin_key_delete_{id}, без confirm/cancel
    @admin_router.callback_query(F.data.regexp(r"^admin_key_delete_\d+$"))
    async def admin_key_delete_prompt(callback: types.CallbackQuery):
        if not is_admin(callback.from_user.id):
            await callback.answer("🚫 У вас нет прав", show_alert=True)
            return
        await callback.answer()
        logger.info(
            f"Получен admin_key_delete_prompt: data='{callback.data}' от {callback.from_user.id}"
        )
        try:
            key_id = int(callback.data.split("_")[-1])
        except Exception:
            await callback.message.answer("❌ Неверный формат key_id")
            return
        key = await asyncio.to_thread(get_key_by_id, key_id)
        if not key:
            await callback.message.answer("❌ Подписка не найдена")
            return
        email = key.get("key_email") or "—"
        host = key.get("host_name") or "—"
        try:
            await safe_edit_text(callback, 
                f"🗑️ <b>Вы уверены, что хотите удалить подписку № {key_id}?</b>\n\n🆔 <b>Email:</b> {email}\n🗺️ <b>Сервер:</b> {host}",
                reply_markup=keyboards.create_admin_delete_key_confirm_keyboard(key_id),
            )
        except Exception as e:
            logger.debug(f"Не удалось изменить текст в запросе на удаление ключа № {key_id}: {e}")
            await callback.message.answer(
                f"🗑️ <b>Вы уверены, что хотите удалить подписку № {key_id}?</b>\n\n🆔 <b>Email:</b> {email}\n🗺️ <b>Сервер:</b> {host}",
                reply_markup=keyboards.create_admin_delete_key_confirm_keyboard(key_id),
            )

    # --- Продление конкретного ключа из карточки ---
    class AdminExtendSingleKey(StatesGroup):
        waiting_days = State()

    @admin_router.callback_query(F.data.startswith("admin_key_extend_"))
    async def admin_key_extend_prompt(callback: types.CallbackQuery, state: FSMContext):
        if not is_admin(callback.from_user.id):
            await callback.answer("🚫 У вас нет прав", show_alert=True)
            return
        await callback.answer()
        try:
            key_id = int(callback.data.split("_")[-1])
        except Exception:
            await callback.message.answer("❌ Неверный формат key_id")
            return
        await state.update_data(extend_key_id=key_id)
        await state.set_state(AdminExtendSingleKey.waiting_days)
        await safe_edit_text(callback, 
            f"📅 <b>Укажите, на сколько дней продлить подписку № {key_id} (число):</b>",
            reply_markup=keyboards.create_admin_cancel_keyboard(),
            parse_mode="HTML",
        )

    @admin_router.message(AdminExtendSingleKey.waiting_days)
    async def admin_key_extend_process(message: types.Message, state: FSMContext):
        if not is_admin(message.from_user.id):
            return
        try:
            await message.delete()
        except Exception:
            pass
        data = await state.get_data()
        key_id = int(data.get("extend_key_id", 0))
        if not key_id:
            await state.clear()
            await message.answer("❌ Не удалось определить подписку")
            return
        try:
            days = int((message.text or "").strip())
        except Exception:
            await message.answer("❌ Введите число дней")
            return
        if days <= 0:
            await message.answer("❌ Дней должно быть положительное число")
            return
        key = await asyncio.to_thread(get_key_by_id, key_id)
        if not key:
            await message.answer("❌ Подписка не найдена")
            await state.clear()
            return
        host = key.get("host_name")
        email = key.get("key_email")
        if not host or not email:
            await message.answer("❌ У подписки отсутствует сервер или email")
            await state.clear()
            return
        # Продление на хосте
        try:
            resp = await create_or_update_key_on_host(host, email, days_to_add=days)
        except Exception as e:
            logger.error(
                f"Продление ключа админом: не удалось обновить сервер для ключа № {key_id}: {e}"
            )
            resp = None
        if not resp or not resp.get("client_uuid") or not resp.get("expiry_timestamp_ms"):
            await message.answer("❌ Не удалось продлить подписку на сервере")
            return
        # Обновление в БД
        try:
            update_key_info(key_id, resp["client_uuid"], int(resp["expiry_timestamp_ms"]))
        except Exception as e:
            logger.error(
                f"Продление ключа админом: не удалось обновить БД для ключа № {key_id}: {e}"
            )
        await state.clear()
        # Повторный показ карточки ключа
        new_key = await asyncio.to_thread(get_key_by_id, key_id)
        text = (
            f"🔑 <b>Подписка № {key_id}</b>\n\n"
            f"🗺️ <b>Сервер:</b> {new_key.get('host_name') or '—'}\n"
            f"🆔 <b>Email:</b> {new_key.get('key_email') or '—'}\n"
            f"📅 <b>Истекает:</b> {format_dt_for_display(new_key.get('expiry_date'))}\n"
        )
        await message.answer(f"✅ Подписка продлена на {days} дн.")
        await message.answer(
            text,
            reply_markup=keyboards.create_admin_key_actions_keyboard(
                key_id,
                (int(new_key.get("user_id")) if new_key and new_key.get("user_id") else None),
            ),
        )

    # --- Управление администраторами: добавить админа ---
    class AdminAddAdmin(StatesGroup):
        waiting_for_input = State()

    @admin_router.callback_query(F.data == "admin_add_admin")
    async def admin_add_admin_entry(callback: types.CallbackQuery, state: FSMContext):
        if not is_admin(callback.from_user.id):
            await callback.answer("🚫 У вас нет прав", show_alert=True)
            return
        await callback.answer()
        await state.set_state(AdminAddAdmin.waiting_for_input)
        await safe_edit_text(callback, 
            "Введите ID пользователя или его @username, которого нужно сделать администратором:\n\n"
            "Примеры: 123456789 или @username",
            reply_markup=keyboards.create_admin_cancel_keyboard(),
        )

    @admin_router.message(AdminAddAdmin.waiting_for_input)
    async def admin_add_admin_process(message: types.Message, state: FSMContext):
        if not is_admin(message.from_user.id):
            return
        raw = (message.text or "").strip()
        try:
            await message.delete()
        except Exception:
            pass
        target_id: int | None = None
        # Попытка распарсить как число
        if raw.isdigit():
            try:
                target_id = int(raw)
            except Exception:
                target_id = None
        # Если @username
        if target_id is None and raw.startswith("@"):
            uname = raw.lstrip("@")
            # 1) Пробуем как передано (@username)
            try:
                chat = await message.bot.get_chat(raw)
                target_id = int(chat.id)
            except Exception:
                target_id = None
            # 2) Пробуем без @ (username)
            if target_id is None:
                try:
                    chat = await message.bot.get_chat(uname)
                    target_id = int(chat.id)
                except Exception:
                    target_id = None
            # 3) Фолбэк: ищем пользователя в локальной БД по username
            if target_id is None:
                try:
                    users = await asyncio.to_thread(get_all_users) or []
                    uname_low = uname.lower()
                    for u in users:
                        u_un = (u.get("username") or "").lstrip("@").lower()
                        if u_un and u_un == uname_low:
                            target_id = int(u.get("telegram_id") or u.get("user_id") or u.get("id"))
                            break
                except Exception:
                    target_id = None
        if target_id is None:
            await message.answer(
                "❌ Не удалось распознать ID/username. Отправьте корректное значение или нажмите Отмена"
            )
            return
        # Обновляем настройки админов
        try:
            from shop_bot.data_manager.database import get_admin_ids, update_setting

            ids = set(get_admin_ids())
            ids.add(int(target_id))
            # Сохраняем в admin_telegram_ids строкой CSV
            ids_str = ",".join(str(i) for i in sorted(ids))
            update_setting("admin_telegram_ids", ids_str)
            await message.answer(f"✅ Пользователь {target_id} добавлен в администраторы")
        except Exception as e:
            await message.answer(f"❌ Ошибка при сохранении: {e}")
        await state.clear()
        # Показать админ-меню снова
        try:
            await show_admin_menu(message)
        except Exception:
            pass

    # --- Снятие прав администратора ---
    class AdminRemoveAdmin(StatesGroup):
        waiting_for_input = State()

    @admin_router.callback_query(F.data == "admin_remove_admin")
    async def admin_remove_admin_entry(callback: types.CallbackQuery, state: FSMContext):
        if not is_admin(callback.from_user.id):
            await callback.answer("🚫 У вас нет прав", show_alert=True)
            return
        await callback.answer()
        await state.set_state(AdminRemoveAdmin.waiting_for_input)
        await safe_edit_text(callback, 
            "Введите ID пользователя или его @username, которого нужно снять из админов:\n\n"
            "Примеры: 123456789 или @username",
            reply_markup=keyboards.create_admin_cancel_keyboard(),
        )

    @admin_router.message(AdminRemoveAdmin.waiting_for_input)
    async def admin_remove_admin_process(message: types.Message, state: FSMContext):
        if not is_admin(message.from_user.id):
            return
        raw = (message.text or "").strip()
        try:
            await message.delete()
        except Exception:
            pass
        target_id: int | None = None
        # Попытка распарсить как число
        if raw.isdigit():
            try:
                target_id = int(raw)
            except Exception:
                target_id = None
        # Резолвим username (@username или username)
        if target_id is None:
            uname = raw.lstrip("@")
            # 1) Пробуем как введено
            try:
                chat = await message.bot.get_chat(raw)
                target_id = int(chat.id)
            except Exception:
                target_id = None
            # 2) Пробуем без @
            if target_id is None and uname:
                try:
                    chat = await message.bot.get_chat(uname)
                    target_id = int(chat.id)
                except Exception:
                    target_id = None
            # 3) Фолбэк: поиск в БД
            if target_id is None and uname:
                try:
                    users = await asyncio.to_thread(get_all_users) or []
                    uname_low = uname.lower()
                    for u in users:
                        u_un = (u.get("username") or "").lstrip("@").lower()
                        if u_un and u_un == uname_low:
                            target_id = int(u.get("telegram_id") or u.get("user_id") or u.get("id"))
                            break
                except Exception:
                    target_id = None
        if target_id is None:
            await message.answer(
                "❌ Не удалось распознать ID/username. Отправьте корректное значение или нажмите Отмена"
            )
            return
        # Обновляем настройки админов
        try:
            from shop_bot.data_manager.database import get_admin_ids, update_setting

            ids = set(get_admin_ids())
            if target_id not in ids:
                await message.answer(f"ℹ️ Пользователь {target_id} не является администратором")
                await state.clear()
                try:
                    await show_admin_menu(message)
                except Exception:
                    pass
                return
            if len(ids) <= 1:
                await message.answer("❌ Нельзя снять последнего администратора")
                return
            ids.discard(int(target_id))
            ids_str = ",".join(str(i) for i in sorted(ids))
            update_setting("admin_telegram_ids", ids_str)
            await message.answer(f"✅ Пользователь {target_id} снят с администраторов")
        except Exception as e:
            await message.answer(f"❌ Ошибка при сохранении: {e}")
        await state.clear()
        # Показать админ-меню снова
        try:
            await show_admin_menu(message)
        except Exception:
            pass

    # --- Удаление ключа: отмена ---
    @admin_router.callback_query(F.data.startswith("admin_key_delete_cancel_"))
    async def admin_key_delete_cancel(callback: types.CallbackQuery):
        if not is_admin(callback.from_user.id):
            await callback.answer("🚫 У вас нет прав", show_alert=True)
            return
        try:
            await callback.answer("⚠️ Отменено", show_alert=False)
        except Exception:
            pass
        logger.info(
            f"Получен admin_key_delete_cancel: data='{callback.data}' от {callback.from_user.id}"
        )
        try:
            key_id = int(callback.data.split("_")[-1])
        except Exception:
            return
        key = await asyncio.to_thread(get_key_by_id, key_id)
        if not key:
            return
        text = (
            f"🔑 <b>Подписка № {key_id}</b>\n\n"
            f"🗺️ <b>Сервер:</b> {key.get('host_name') or '—'}\n"
            f"🆔 <b>Email:</b> {key.get('key_email') or '—'}\n"
            f"📅 <b>Истекает:</b> {format_dt_for_display(key.get('expiry_date'))}\n"
        )
        try:
            await safe_edit_text(callback, 
                text,
                reply_markup=keyboards.create_admin_key_actions_keyboard(
                    key_id,
                    int(key.get("user_id")) if key and key.get("user_id") else None,
                ),
            )
        except Exception as e:
            logger.debug(f"Не удалось изменить текст при отмене удаления ключа № {key_id}: {e}")
            await callback.message.answer(
                text,
                reply_markup=keyboards.create_admin_key_actions_keyboard(
                    key_id,
                    int(key.get("user_id")) if key and key.get("user_id") else None,
                ),
            )

    # --- Удаление ключа: подтверждение и выполнение ---
    @admin_router.callback_query(F.data.startswith("admin_key_delete_confirm_"))
    async def admin_key_delete_confirm(callback: types.CallbackQuery):
        if not is_admin(callback.from_user.id):
            await callback.answer("🚫 У вас нет прав", show_alert=True)
            return
        try:
            await callback.answer("⏳ Удаляю...", show_alert=False)
        except Exception:
            pass
        logger.info(
            f"Получен admin_key_delete_confirm: data='{callback.data}' от {callback.from_user.id}"
        )
        try:
            key_id = int(callback.data.split("_")[-1])
        except Exception:
            await callback.message.answer("❌ Неверный формат key_id")
            return
        try:
            key = await asyncio.to_thread(get_key_by_id, key_id)
        except Exception as e:
            logger.error(f"Ошибка get_key_by_id для БД, ключ № {key_id}: {e}")
            key = None
        if not key:
            await callback.message.answer("❌ Подписка не найдена")
            return
        try:
            user_id = int(key.get("user_id"))
        except Exception as e:
            logger.error(
                f"Некорректный user_id для ключа № {key_id}: {key.get('user_id')}, ошибка={e}"
            )
            await callback.message.answer("❌ Ошибка данных подписки: некорректный пользователь")
            return
        host = key.get("host_name")
        email = key.get("key_email")
        ok_host = True
        if host and email:
            try:
                ok_host = await delete_client_on_host(host, email)
            except Exception as e:
                ok_host = False
                logger.error(
                    f"Не удалось удалить клиента на хосте '{host}' для ключа № {key_id}: {e}"
                )
        ok_db = False
        try:
            ok_db = delete_key_by_email(email)
        except Exception as e:
            logger.error(f"Не удалось удалить ключ в БД для email '{email}': {e}")
        if ok_db:
            await callback.message.answer(
                "✅ Подписка удалена"
                + (" (с сервера тоже)" if ok_host else " (но удалить на сервере не удалось)")
            )
            # Обновить список ключей пользователя
            keys = await asyncio.to_thread(get_keys_for_user, user_id)
            try:
                await safe_edit_text(callback, 
                    f"🔑 <b>Подписки пользователя {user_id}:</b>",
                    reply_markup=keyboards.create_admin_user_keys_keyboard(user_id, keys),
                    parse_mode="HTML",
                )
            except Exception as e:
                logger.debug(
                    f"Не удалось изменить текст при обновлении списка после удаления для пользователя {user_id}: {e}"
                )
                await callback.message.answer(
                    f"🔑 <b>Подписки пользователя {user_id}:</b>",
                    reply_markup=keyboards.create_admin_user_keys_keyboard(user_id, keys),
                    parse_mode="HTML",
                )
            # Уведомление пользователю (если получится)
            try:
                await callback.bot.send_message(
                    user_id,
                    "ℹ️ <b>Администратор удалил одну из ваших подписок. Если это ошибка — напишите в поддержку</b>",
                    reply_markup=keyboards.create_support_keyboard(),
                )
            except Exception:
                pass
        else:
            await callback.message.answer("❌ Не удалось удалить подписку из базы данных")

    class AdminEditKeyEmail(StatesGroup):
        waiting_for_email = State()

    @admin_router.callback_query(F.data.startswith("admin_key_edit_email_"))
    async def admin_key_edit_email_start(callback: types.CallbackQuery, state: FSMContext):
        if not is_admin(callback.from_user.id):
            await callback.answer("🚫 У вас нет прав", show_alert=True)
            return
        await callback.answer()
        try:
            key_id = int(callback.data.split("_")[-1])
        except Exception:
            await callback.message.answer("❌ Неверный формат key_id")
            return
        await state.update_data(edit_key_id=key_id)
        await state.set_state(AdminEditKeyEmail.waiting_for_email)
        await safe_edit_text(callback, 
            f"Введите новый email для подписки № {key_id}",
            reply_markup=keyboards.create_admin_cancel_keyboard(),
        )

    @admin_router.message(AdminEditKeyEmail.waiting_for_email)
    async def admin_key_edit_email_commit(message: types.Message, state: FSMContext):
        if not is_admin(message.from_user.id):
            return
        data = await state.get_data()
        key_id = int(data.get("edit_key_id"))
        new_email = (message.text or "").strip()
        try:
            await message.delete()
        except Exception:
            pass
        if not new_email:
            await message.answer("❌ Введите корректный email")
            return
        ok = update_key_email(key_id, new_email)
        if ok:
            await message.answer("✅ Email обновлён")
        else:
            await message.answer("❌ Не удалось обновить email (возможно, уже занят)")
        await state.clear()

    # --- Перенос подписки на другой сервер ---
    class AdminEditKeyHost(StatesGroup):
        waiting_for_host = State()

    @admin_router.callback_query(F.data.startswith("admin_key_edit_host_"))
    async def admin_key_edit_host_start(callback: types.CallbackQuery, state: FSMContext):
        if not is_admin(callback.from_user.id):
            await callback.answer("🚫 У вас нет прав", show_alert=True)
            return

        await callback.answer()
        await state.clear()  # Сбрасываем состояния, ручной ввод больше не нужен

        try:
            key_id = int(callback.data.split("_")[-1])
        except Exception:
            await callback.message.answer("❌ Неверный формат key_id")
            return

        # Получаем данные ключа без проверки user_id
        key_data = await asyncio.to_thread(get_key_by_id, key_id)
        if not key_data:
            await callback.message.answer("❌ Подписка не найдена в базе данных")
            return

        hosts = await asyncio.to_thread(get_all_hosts)
        if not hosts:
            await callback.message.answer("❌ Нет доступных серверов")
            return

        current_host = key_data.get("host_name")
        # Исключаем текущий сервер, чтобы не переносить на него же
        hosts = [h for h in hosts if h.get("host_name") != current_host]
        if not hosts:
            await callback.message.answer("❌ Другие серверы отсутствуют")
            return

        # -------------------------------------------------------------------------
        # Передаем чистый экшен "adm_sw", а key_id изолированно в extra
        # -------------------------------------------------------------------------
        await safe_edit_text(callback, 
            f"🌍 <b>Выберите новый сервер для переноса подписки № {key_id}:</b>\n\n"
            f"👤 <b>Владелец</b>: {key_data.get('user_id')}\n"
            f"🗺️ <b>Текущий сервер</b>: {current_host or 'Не указан'}",
            reply_markup=keyboards.create_host_selection_keyboard(hosts, action=f"adm_sw-{key_id}"),
            parse_mode="HTML",
        )

    @admin_router.callback_query(F.data.startswith("select_host:adm_sw-"))
    async def admin_process_host_switch(callback: types.CallbackQuery):
        # -------------------------------------------------------------------------
        # 1. Валидация прав доступа администратора
        # -------------------------------------------------------------------------
        if not is_admin(callback.from_user.id):
            await callback.answer("🚫 У вас нет прав", show_alert=True)
            return

        # -------------------------------------------------------------------------
        # 2. Разбор callback_data и извлечение ID подписки
        # -------------------------------------------------------------------------
        parsed = keyboards.parse_host_callback_data(callback.data)
        if not parsed:
            await callback.answer("❌ Ошибка обработки данных", show_alert=False)
            return

        action, extra, token = parsed
        try:
            key_id = int(action.split("-")[-1])
        except (ValueError, TypeError, IndexError):
            await callback.answer("❌ Ошибка определения ID подписки", show_alert=False)
            return

        # -------------------------------------------------------------------------
        # 3. Поиск целевого хоста по токену безопасности
        # -------------------------------------------------------------------------
        hosts = await asyncio.to_thread(get_all_hosts)
        target_host = keyboards.find_host_by_callback_token(hosts, token)
        if not target_host:
            await callback.answer("❌ Выбранный сервер не найден", show_alert=False)
            return

        new_host_name = target_host.get("host_name")

        # -------------------------------------------------------------------------
        # 4. Проверка существования подписки и валидация текущего хоста
        # -------------------------------------------------------------------------
        key_data = await asyncio.to_thread(get_key_by_id, key_id)
        if not key_data:
            await delete_and_send(callback.message, "❌ Подписка не найдена")
            return

        old_host = key_data.get("host_name")
        if not old_host:
            await delete_and_send(callback.message, "❌ Для подписки не указан текущий сервер")
            return

        if new_host_name == old_host:
            await callback.answer("❌ Это уже текущий сервер", show_alert=False)
            return

        # -------------------------------------------------------------------------
        # 5. Расчет точной даты окончания действия подписки
        # -------------------------------------------------------------------------
        try:
            expiry_dt = datetime.fromisoformat(key_data["expiry_date"])
            expiry_timestamp_ms_exact = int(expiry_dt.timestamp() * 1000)
        except Exception:
            now_dt = datetime.now()
            expiry_timestamp_ms_exact = int((now_dt + timedelta(days=1)).timestamp() * 1000)

        email = key_data.get("key_email")
        if not email:
            await delete_and_send(callback.message, "❌ Не удалось определить email подписки")
            return

        await callback.answer("⏳ Переношу подписку...", show_alert=False)

        # -------------------------------------------------------------------------
        # 6. Процесс миграции: создание на новом хосте и удаление со старого
        # -------------------------------------------------------------------------
        try:
            # 1. Создаем на новом сервере
            result = await create_or_update_key_on_host(
                new_host_name,
                email,
                days_to_add=None,
                expiry_timestamp_ms=expiry_timestamp_ms_exact,
            )
            if not result:
                await delete_and_send(callback.message, 
                    f"❌ Не удалось перенести подписку на сервер {new_host_name}"
                )
                return

            await asyncio.sleep(1)

            # 2. Удаляем на старом
            try:
                await delete_client_on_host(old_host, email)
            except Exception:
                pass

            # 3. Обновляем БД бота
            update_key_host_and_info(
                key_id=key_id,
                new_host_name=new_host_name,
                new_xui_uuid=result["client_uuid"],
                new_expiry_ms=result["expiry_timestamp_ms"],
            )
            keys_in_transition.add(key_id)

            # -------------------------------------------------------------------------
            # 7. Получение новых реквизитов и вывод результата администратору
            # -------------------------------------------------------------------------
            try:
                updated_key = await asyncio.to_thread(get_key_by_id, key_id)
                details = await get_key_details_from_host(updated_key)
                if details and details.get("connection_string"):
                    connection_string = details["connection_string"]
                    await delete_and_send(callback.message, 
                        text=f"✅ <b>Подписка № {key_id} перенесена!</b>\n\n"
                        f"🗺️ <b>Новый сервер:</b> {new_host_name}\n"
                        f"🆔 <b>UUID:</b> {result['client_uuid']}\n\n"
                        f"🔗 <b>Новая подписка:</b>\n<code>{connection_string}</code>",
                        parse_mode="HTML",
                    )
                else:
                    await delete_and_send(callback.message, 
                        f"✅ Готово! Подписка № {key_id} перенесена на {new_host_name}"
                    )
            except Exception:
                await delete_and_send(callback.message, 
                    f"✅ Готово! Подписка № {key_id} перенесена на {new_host_name}"
                )

            # -------------------------------------------------------------------------
            # 8. Отправка сервисного уведомления пользователю
            # -------------------------------------------------------------------------
            try:
                user_id = key_data.get("user_id")
                if user_id:
                    await callback.bot.send_message(
                        chat_id=user_id,
                        text=f"⚙️ <b>Ваша подписка № {key_id} была перенесена администратором</b>\n\n"
                        f"🗺️ <b>Новый сервер:</b> {new_host_name}\n"
                        f"🔗 <b>Новая подписка:</b>\n<code>{connection_string}</code>\n\n"
                        f"🔑🔄 Текущая подписка будет отключена. Не забудьте добавить новую подписку в свой клиент",
                        parse_mode="HTML",
                    )
            except Exception:
                pass

        except Exception as e:
            logger.error(
                f"Ошибка при переключении админом ключа {key_id} на сервер {new_host_name}: {e}",
                exc_info=True,
            )
            await delete_and_send(callback.message, 
                f"❌ Произошла ошибка при переносе подписки:\n<code>{e}</code>",
                parse_mode="HTML",
            )

    # --- Начисление реф. баланса: удалено ---

    # --- Выдача подарочного ключа ---
    class AdminGiftKey(StatesGroup):
        picking_user = State()
        picking_host = State()
        picking_days = State()
        confirming = State()

    @admin_router.callback_query(F.data == "admin_gift_key")
    async def admin_gift_key_entry(callback: types.CallbackQuery, state: FSMContext):
        if not is_admin(callback.from_user.id):
            await callback.answer("🚫 У вас нет прав", show_alert=True)
            return
        await callback.answer()
        users = await asyncio.to_thread(get_all_users)
        await state.clear()
        await state.set_state(AdminGiftKey.picking_user)
        await safe_edit_text(callback, 
            "🪄 <b>Выдача подписки</b>\n\nВыберите пользователя:",
            reply_markup=keyboards.create_admin_users_pick_keyboard(users, page=0, action="gift"),
            parse_mode="HTML",
        )

    # Запуск выдачи подарка сразу для выбранного пользователя из карточки пользователя
    @admin_router.callback_query(F.data.startswith("admin_gift_key_"))
    async def admin_gift_key_for_user(callback: types.CallbackQuery, state: FSMContext):
        if not is_admin(callback.from_user.id):
            await callback.answer("🚫 У вас нет прав", show_alert=True)
            return
        await callback.answer()
        try:
            user_id = int(callback.data.split("_")[-1])
        except Exception:
            await callback.message.answer("❌ Неверный формат user_id")
            return
        await state.clear()
        await state.update_data(target_user_id=user_id)
        hosts = await asyncio.to_thread(get_all_hosts)
        await state.set_state(AdminGiftKey.picking_host)
        await safe_edit_text(callback, 
            f"👤 <b>Пользователь {user_id}. Выберите сервер:</b>",
            reply_markup=keyboards.create_admin_hosts_pick_keyboard(hosts, action="gift"),
            parse_mode="HTML",
        )

    @admin_router.callback_query(
        AdminGiftKey.picking_user, F.data.startswith("admin_gift_pick_user_page_")
    )
    async def admin_gift_pick_user_page(callback: types.CallbackQuery, state: FSMContext):
        if not is_admin(callback.from_user.id):
            await callback.answer("🚫 У вас нет прав", show_alert=True)
            return
        await callback.answer()
        try:
            page = int(callback.data.split("_")[-1])
        except Exception:
            page = 0
        users = await asyncio.to_thread(get_all_users)
        await safe_edit_text(callback, 
            "🪄 <b>Выдача подписки</b>\n\nВыберите пользователя:",
            reply_markup=keyboards.create_admin_users_pick_keyboard(
                users, page=page, action="gift"
            ),
            parse_mode="HTML",
        )

    @admin_router.callback_query(
        AdminGiftKey.picking_user, F.data.startswith("admin_gift_pick_user_")
    )
    async def admin_gift_pick_user(callback: types.CallbackQuery, state: FSMContext):
        if not is_admin(callback.from_user.id):
            await callback.answer("🚫 У вас нет прав", show_alert=True)
            return
        await callback.answer()
        try:
            user_id = int(callback.data.split("_")[-1])
        except Exception:
            await callback.message.answer("❌ Неверный формат user_id")
            return
        await state.update_data(target_user_id=user_id)
        hosts = await asyncio.to_thread(get_all_hosts)
        await state.set_state(AdminGiftKey.picking_host)
        await safe_edit_text(callback, 
            f"👤 <b>Пользователь {user_id}. Выберите сервер:</b>",
            reply_markup=keyboards.create_admin_hosts_pick_keyboard(hosts, action="gift"),
            parse_mode="HTML",
        )

    @admin_router.callback_query(AdminGiftKey.picking_host, F.data == "admin_gift_back_to_users")
    async def admin_gift_back_to_users(callback: types.CallbackQuery, state: FSMContext):
        if not is_admin(callback.from_user.id):
            await callback.answer("🚫 У вас нет прав", show_alert=True)
            return
        await callback.answer()
        users = await asyncio.to_thread(get_all_users)
        await state.set_state(AdminGiftKey.picking_user)
        await safe_edit_text(callback, 
            "🪄 <b>Выдача подписки</b>\n\nВыберите пользователя:",
            reply_markup=keyboards.create_admin_users_pick_keyboard(users, page=0, action="gift"),
            parse_mode="HTML",
        )

    @admin_router.callback_query(
        AdminGiftKey.picking_host, F.data.startswith("admin_gift_pick_host_")
    )
    async def admin_gift_pick_host(callback: types.CallbackQuery, state: FSMContext):
        if not is_admin(callback.from_user.id):
            await callback.answer("🚫 У вас нет прав", show_alert=True)
            return
        await callback.answer()
        token = callback.data[len("admin_gift_pick_host_") :]
        all_hosts = await asyncio.to_thread(get_all_hosts) or []
        host = keyboards.find_host_by_callback_token(all_hosts, token)
        if not host:
            await callback.answer("❌ Сервер не найден", show_alert=False)
            return
        host_name = host["host_name"]
        await state.update_data(host_name=host_name)
        await state.set_state(AdminGiftKey.picking_days)
        prompt_msg = await safe_edit_text(callback, 
            f"🌍 <b>Сервер: {host_name}. Введите срок действия подписки в днях (целое число):</b>",
            reply_markup=keyboards.create_admin_cancel_keyboard(),
            parse_mode="HTML",
        )
        await state.update_data(prompt_msg_id=prompt_msg.message_id)

    @admin_router.callback_query(AdminGiftKey.picking_days, F.data == "admin_gift_back_to_hosts")
    async def admin_gift_back_to_hosts(callback: types.CallbackQuery, state: FSMContext):
        if not is_admin(callback.from_user.id):
            await callback.answer("🚫 У вас нет прав", show_alert=True)
            return
        await callback.answer()
        data = await state.get_data()
        user_id = int(data.get("target_user_id"))
        hosts = await asyncio.to_thread(get_all_hosts)
        await state.set_state(AdminGiftKey.picking_host)
        await safe_edit_text(callback, 
            f"👤 <b>Пользователь {user_id}. Выберите сервер:</b>",
            reply_markup=keyboards.create_admin_hosts_pick_keyboard(hosts, action="gift"),
            parse_mode="HTML",
        )

    @admin_router.message(AdminGiftKey.picking_days)
    async def admin_gift_pick_days(message: types.Message, state: FSMContext):
        if not is_admin(message.from_user.id):
            return
        data = await state.get_data()
        user_id = int(data.get("target_user_id"))
        host_name = data.get("host_name")
        prompt_msg_id = data.get("prompt_msg_id")
        try:
            days = int(message.text.strip())
        except Exception:
            await message.answer("❌ Введите целое число дней")
            return
        if days <= 0:
            await message.answer("❌ Срок должен быть положительным")
            return

        # -------------------------------------------------------------------------
        # Уборка в интерфейсе
        # -------------------------------------------------------------------------

        # Удаляем старое сообщение с предложением ввода
        if prompt_msg_id:
            try:
                await message.bot.delete_message(chat_id=message.chat.id, message_id=prompt_msg_id)
            except Exception:
                pass

        # Удаляем текстовый запрос самого админа, чтобы в чате не висел мусор
        try:
            await message.delete()
        except Exception:
            pass

        await state.update_data(days=days)
        await state.set_state(AdminGiftKey.confirming)
        sent = await message.answer(
            f"⚠️ <b>Выдать подписку пользователю {user_id} на сервере {host_name} на {days} дн.?</b>",
            reply_markup=keyboards.create_admin_gift_confirm_keyboard(),
            parse_mode="HTML",
        )
        await state.update_data(confirm_msg_id=sent.message_id)

    @admin_router.callback_query(AdminGiftKey.confirming, F.data == "admin_gift_confirm")
    async def admin_gift_confirm_handler(callback: types.CallbackQuery, state: FSMContext):
        if not is_admin(callback.from_user.id):
            await callback.answer("🚫 У вас нет прав", show_alert=True)
            return
        await callback.answer("⏳ Выдаю подписку...", show_alert=False)
        data = await state.get_data()
        user_id = int(data.get("target_user_id"))
        host_name = data.get("host_name")
        days = int(data.get("days"))
        chat_id = callback.message.chat.id
        await state.clear()

        # Сгенерируем уникальный техн. email
        user = await asyncio.to_thread(get_user, user_id) or {}
        username = (user.get("username") or f"user{user_id}").lower()
        username_slug = re.sub(r"[^a-z0-9._-]", "_", username).strip("_")[:16] or f"user{user_id}"
        base_local = f"{username_slug}"
        candidate_local = base_local
        attempt = 1
        while True:
            candidate_email = f"{candidate_local}@{EMAIL_DOMAIN}"
            existing = await asyncio.to_thread(get_key_by_email, candidate_email)
            if not existing:
                break
            attempt += 1
            candidate_local = f"{base_local}_{attempt}"
            if attempt > 100:
                candidate_local = f"{base_local}_{int(time.time())}"
                candidate_email = f"{candidate_local}@{EMAIL_DOMAIN}"
                break
        generated_email = candidate_email

        # Создаём/обновляем клиента на хосте с days_to_add
        try:
            host_resp = await create_or_update_key_on_host(
                host_name, generated_email, days_to_add=days
            )
        except Exception as e:
            host_resp = None
            logging.error(
                f"Gift flow: failed to create client on host '{host_name}' for user {user_id}: {e}"
            )

        if (
            not host_resp
            or not host_resp.get("client_uuid")
            or not host_resp.get("expiry_timestamp_ms")
        ):
            await delete_and_send(callback.message, 
                "❌ Не удалось выдать подписку на сервере. Проверьте настройки хоста и доступность сервера"
            )
            await _render_user_card(callback.bot, chat_id, None, user_id)
            return

        client_uuid = host_resp["client_uuid"]
        expiry_ms = int(host_resp["expiry_timestamp_ms"])  # В мс
        connection_link = host_resp.get("connection_string")

        key_id = add_new_key(user_id, host_name, client_uuid, generated_email, expiry_ms)
        if key_id:
            user_part = f"{user_id} ({format_stored_username(user.get('username'), linked=False)})"
            text_admin = (
                f"✅ Подписка № {key_id} выдана пользователю {user_part} (сервер: {host_name}, {days} дн.)\n"
                f"🆔 Email: {generated_email}"
            )
            try:
                await delete_and_send(callback.message, text_admin)
            except TelegramBadRequest:
                await callback.bot.send_message(chat_id, text_admin)
            try:
                kb = None
                notify_text = (
                    f"🔐 <b>Ваша подписка № {key_id} готова!</b>\n\n"
                    f"🗺️ <b>Сервер:</b> {host_name}\n"
                    f"📅 <b>Срок:</b> {days} дн.\n"
                )
                if connection_link:
                    cs = html_escape.escape(connection_link)
                    notify_text += (
                        f"\n🔗 <b>Основная подписка:</b>\n{html.code(cs)}\n"
                        f"▪️🇷🇺 Российский интернет ➡️ напрямую\n"
                        f"▪️🌍 Зарубежный интернет ➡️ через прокси\n"
                        f"▪️⭐️ Рекомендуем для Happ и INCY\n\n"
                        f"🔀 <b>Альтернативная подписка:</b>\n{html.code(cs.replace('/sub/', '/json/'))}\n"
                        f"▪️🌍 Весь интернет ➡️ через прокси\n"
                        f"▪️🥷 Надежно перенаправляем российский трафик на свои серверы\n"
                        f"▪️⭐️ Рекомендуем для v2RayTun и других клиентов. Добавляется через копирование и вставку ссылки\n"
                    )
                    raw_cs = host_resp["connection_string"]
                    redirect_url_happ = f"{REDIR_URL}{base64.urlsafe_b64encode(f'happ://add/{raw_cs}'.encode()).decode().rstrip('=')}"
                    redirect_url_incy = f"{REDIR_URL}{base64.urlsafe_b64encode(f'incy://add/{raw_cs}'.encode()).decode().rstrip('=')}"
                    redirect_url_v2raytun = f"{REDIR_URL}{base64.urlsafe_b64encode(f'v2raytun://import/{raw_cs}'.encode()).decode().rstrip('=')}"
                    redirect_url_v2rayng = f"{REDIR_URL}{base64.urlsafe_b64encode(f'v2rayng://install-config/?url={raw_cs}'.encode()).decode().rstrip('=')}"
                    kb = InlineKeyboardMarkup(
                        inline_keyboard=[
                            [
                                InlineKeyboardButton(
                                    text="🌐 Открыть Web App",
                                    web_app=WebAppInfo(url=raw_cs),
                                    style="success",
                                )
                            ],
                            [
                                InlineKeyboardButton(
                                    text="⚡ Добавить в Happ",
                                    url=redirect_url_happ,
                                    style="primary",
                                )
                            ],
                            [
                                InlineKeyboardButton(
                                    text="🛡️ Добавить в INCY",
                                    url=redirect_url_incy,
                                    style="primary",
                                )
                            ],
                            [
                                InlineKeyboardButton(
                                    text="🚀 Добавить в v2RayTun",
                                    url=redirect_url_v2raytun,
                                    style="primary",
                                )
                            ],
                            [
                                InlineKeyboardButton(
                                    text="👾 Добавить в v2rayNG",
                                    url=redirect_url_v2rayng,
                                    style="primary",
                                )
                            ],
                        ]
                    )
                await callback.bot.send_message(
                    user_id,
                    notify_text,
                    parse_mode="HTML",
                    disable_web_page_preview=True,
                    reply_markup=kb,
                )
            except Exception:
                pass
        else:
            try:
                await delete_and_send(callback.message, "❌ Не удалось сохранить подписку в базе данных")
            except TelegramBadRequest:
                await callback.bot.send_message(
                    chat_id, "❌ Не удалось сохранить подписку в базе данных"
                )
        await _render_user_card(callback.bot, chat_id, None, user_id)

    @admin_router.callback_query(AdminGiftKey.confirming, F.data == "admin_gift_cancel")
    async def admin_gift_cancel_handler(callback: types.CallbackQuery, state: FSMContext):
        if not is_admin(callback.from_user.id):
            await callback.answer("🚫 У вас нет прав", show_alert=True)
            return
        await callback.answer("⚠️ Отменено", show_alert=False)
        data = await state.get_data()
        user_id = int(data.get("target_user_id"))
        await state.clear()
        await _render_user_card(
            callback.bot, callback.message.chat.id, callback.message.message_id, user_id
        )

    # Текстовые обработчики больше не используются в новом потоке выдачи ключа

    # --- Начисление основного баланса ---
    class AdminMainRefill(StatesGroup):
        waiting_for_pair = State()
        waiting_for_amount = State()
        waiting_for_confirm = State()

    @admin_router.callback_query(F.data == "admin_add_balance")
    async def admin_add_balance_entry(callback: types.CallbackQuery, state: FSMContext):
        if not is_admin(callback.from_user.id):
            await callback.answer("🚫 У вас нет прав", show_alert=True)
            return
        await callback.answer()
        users = await asyncio.to_thread(get_all_users)
        await safe_edit_text(callback, 
            "➕ <b>Начисление баланса\n\nВыберите пользователя:</b>",
            reply_markup=keyboards.create_admin_users_pick_keyboard(
                users, page=0, action="add_balance"
            ),
            parse_mode="HTML",
        )

    @admin_router.callback_query(F.data.startswith("admin_add_balance_"))
    async def admin_add_balance_user(callback: types.CallbackQuery, state: FSMContext):
        if not is_admin(callback.from_user.id):
            await callback.answer("🚫 У вас нет прав", show_alert=True)
            return
        await callback.answer()
        try:
            user_id = int(callback.data.split("_")[-1])
        except Exception:
            await callback.message.answer("❌ Неверный формат user_id")
            return
        await state.update_data(
            target_user_id=user_id,
            anchor_chat_id=callback.message.chat.id,
            anchor_msg_id=callback.message.message_id,
        )
        await state.set_state(AdminMainRefill.waiting_for_amount)
        await safe_edit_text(callback, 
            f"💰 <b>Пользователь {user_id}. Введите сумму начисления (в рублях):</b>",
            reply_markup=keyboards.create_admin_cancel_keyboard(),
            parse_mode="HTML",
        )

    # Пагинация списка пользователей для начисления баланса
    @admin_router.callback_query(F.data.startswith("admin_add_balance_pick_user_page_"))
    async def admin_add_balance_pick_user_page(callback: types.CallbackQuery, state: FSMContext):
        if not is_admin(callback.from_user.id):
            await callback.answer("🚫 У вас нет прав", show_alert=True)
            return
        await callback.answer()
        try:
            page = int(callback.data.split("_")[-1])
        except Exception:
            page = 0
        users = await asyncio.to_thread(get_all_users)
        await safe_edit_text(callback, 
            "➕ <b>Начисление баланса\n\nВыберите пользователя:</b>",
            reply_markup=keyboards.create_admin_users_pick_keyboard(
                users, page=page, action="add_balance"
            ),
            parse_mode="HTML",
        )

    # Выбор пользователя для начисления: дальше админ вводит только сумму
    @admin_router.callback_query(F.data.startswith("admin_add_balance_pick_user_"))
    async def admin_add_balance_pick_user(callback: types.CallbackQuery, state: FSMContext):
        if not is_admin(callback.from_user.id):
            await callback.answer("🚫 У вас нет прав", show_alert=True)
            return
        await callback.answer()
        try:
            user_id = int(callback.data.split("_")[-1])
        except Exception:
            await callback.message.answer("❌ Неверный формат user_id")
            return
        await state.update_data(
            target_user_id=user_id,
            anchor_chat_id=callback.message.chat.id,
            anchor_msg_id=callback.message.message_id,
        )
        await state.set_state(AdminMainRefill.waiting_for_amount)
        await safe_edit_text(callback, 
            f"💰 <b>Пользователь {user_id}. Введите сумму начисления (в рублях):</b>",
            reply_markup=keyboards.create_admin_cancel_keyboard(),
            parse_mode="HTML",
        )

    @admin_router.message(AdminMainRefill.waiting_for_amount)
    async def handle_main_amount(message: types.Message, state: FSMContext):
        if not is_admin(message.from_user.id):
            return
        data = await state.get_data()
        user_id = int(data.get("target_user_id"))
        chat_id = data.get("anchor_chat_id", message.chat.id)
        msg_id = data.get("anchor_msg_id")
        try:
            await message.delete()
        except Exception:
            pass
        try:
            amount = float(message.text.strip().replace(",", "."))
        except Exception:
            amount = None
        if amount is None or amount <= 0:
            note = (
                "❌ Введите число — сумму в рублях"
                if amount is None
                else "❌ Сумма должна быть положительной"
            )
            text = (
                f"{note}\n\n💰 <b>Пользователь {user_id}. Введите сумму начисления (в рублях):</b>"
            )
            if msg_id:
                try:
                    await message.bot.edit_message_text(
                        text,
                        chat_id=chat_id,
                        message_id=msg_id,
                        reply_markup=keyboards.create_admin_cancel_keyboard(),
                        parse_mode="HTML",
                    )
                    return
                except TelegramBadRequest:
                    pass
            await message.answer(
                text,
                reply_markup=keyboards.create_admin_cancel_keyboard(),
                parse_mode="HTML",
            )
            return
        await state.update_data(pending_amount=amount)
        await state.set_state(AdminMainRefill.waiting_for_confirm)
        confirm_text = f"⚠️ <b>Начислить {amount:.2f} ₽ пользователю {user_id}?</b>"
        if msg_id:
            try:
                await message.bot.edit_message_text(
                    confirm_text,
                    chat_id=chat_id,
                    message_id=msg_id,
                    reply_markup=keyboards.create_admin_balance_confirm_keyboard("add"),
                    parse_mode="HTML",
                )
                return
            except TelegramBadRequest:
                pass
        sent = await message.answer(
            confirm_text,
            reply_markup=keyboards.create_admin_balance_confirm_keyboard("add"),
            parse_mode="HTML",
        )
        await state.update_data(anchor_chat_id=sent.chat.id, anchor_msg_id=sent.message_id)

    @admin_router.callback_query(
        AdminMainRefill.waiting_for_confirm, F.data == "admin_balance_confirm_add"
    )
    async def admin_balance_confirm_add(callback: types.CallbackQuery, state: FSMContext):
        if not is_admin(callback.from_user.id):
            await callback.answer("🚫 У вас нет прав", show_alert=True)
            return
        await callback.answer("⏳ Начисляю...", show_alert=False)
        data = await state.get_data()
        user_id = int(data.get("target_user_id"))
        amount = float(data.get("pending_amount", 0))
        await state.clear()
        try:
            ok = add_to_balance(user_id, amount)
            if ok:
                try:
                    user_info = await asyncio.to_thread(get_user, user_id)
                    log_username = user_info.get("username", "N/A") if user_info else "N/A"
                    log_transaction(
                        username=log_username,
                        transaction_id=None,
                        payment_id=str(uuid.uuid4()),
                        user_id=user_id,
                        status="internal",
                        amount_rub=amount,
                        amount_currency=None,
                        currency_name=None,
                        payment_method="Admin",
                        metadata=json.dumps(
                            {
                                "action": "admin_credit",
                                "admin_id": callback.from_user.id,
                            }
                        ),
                    )
                except Exception:
                    pass
                try:
                    await callback.bot.send_message(
                        user_id,
                        f"💰 Вам начислено {amount:.2f} ₽ на баланс администратором",
                    )
                except Exception:
                    pass
                await _render_user_card(
                    callback.bot,
                    callback.message.chat.id,
                    callback.message.message_id,
                    user_id,
                    note=f"✅ Начислено {amount:.2f} ₽",
                )
            else:
                await delete_and_send(callback.message, "❌ Пользователь не найден или ошибка БД")
        except Exception as e:
            await delete_and_send(callback.message, f"❌ Ошибка начисления: {e}")

    @admin_router.callback_query(
        AdminMainRefill.waiting_for_confirm, F.data == "admin_balance_cancel_add"
    )
    async def admin_balance_cancel_add(callback: types.CallbackQuery, state: FSMContext):
        if not is_admin(callback.from_user.id):
            await callback.answer("🚫 У вас нет прав", show_alert=True)
            return
        await callback.answer("Отменено", show_alert=False)
        data = await state.get_data()
        user_id = int(data.get("target_user_id"))
        await state.clear()
        await _render_user_card(
            callback.bot, callback.message.chat.id, callback.message.message_id, user_id
        )

    # Back from key actions to keys list
    @admin_router.callback_query(F.data.startswith("admin_key_back_"))
    async def admin_key_back(callback: types.CallbackQuery, state: FSMContext):
        if not is_admin(callback.from_user.id):
            await callback.answer("🚫 У вас нет прав", show_alert=True)
            return
        await callback.answer()
        try:
            key_id = int(callback.data.split("_")[-1])
        except Exception:
            await callback.message.answer("❌ Неверный формат key_id")
            return
        key = await asyncio.to_thread(get_key_by_id, key_id)
        if not key:
            await callback.message.answer("❌ Подписка не найдена")
            return
        # Если мы находимся в контексте просмотра ключей хоста — вернёмся к списку ключей этого хоста
        host_from_state = None
        try:
            data = await state.get_data()
            host_from_state = (data or {}).get("hostkeys_host")
        except Exception:
            host_from_state = None

        if host_from_state:
            host_name = host_from_state
            keys = get_keys_for_host(host_name)
            await safe_edit_text(callback, 
                f"🔑 <b>Подписки на сервере {host_name}:</b>",
                reply_markup=keyboards.create_admin_keys_for_host_keyboard(host_name, keys),
                parse_mode="HTML",
            )
        else:
            user_id = int(key.get("user_id"))
            keys = await asyncio.to_thread(get_keys_for_user, user_id)
            await safe_edit_text(callback, 
                f"🔑 <b>Подписки пользователя {user_id}:</b>",
                reply_markup=keyboards.create_admin_user_keys_keyboard(user_id, keys),
                parse_mode="HTML",
            )

    # Noop callback to safely ignore placeholder buttons
    @admin_router.callback_query(F.data == "noop")
    async def admin_noop(callback: types.CallbackQuery):
        await callback.answer()

    @admin_router.callback_query(F.data == "admin_cancel")
    async def admin_cancel_handler(callback: types.CallbackQuery, state: FSMContext):
        await callback.answer("⚠️ Отменено", show_alert=False)
        await state.clear()
        await show_admin_menu(callback.message, edit_message=True)

    # --- Списание средств администратором (UI) ---
    class AdminMainDeduct(StatesGroup):
        waiting_for_amount = State()
        waiting_for_confirm = State()

    # Вход из админ-меню: показать список пользователей
    @admin_router.callback_query(F.data == "admin_deduct_balance")
    async def admin_deduct_balance_entry(callback: types.CallbackQuery, state: FSMContext):
        if not is_admin(callback.from_user.id):
            await callback.answer("🚫 У вас нет прав", show_alert=True)
            return
        await callback.answer()
        users = await asyncio.to_thread(get_all_users)
        await safe_edit_text(callback, 
            "➖ <b>Списание баланса</b>\n\nВыберите пользователя:",
            reply_markup=keyboards.create_admin_users_pick_keyboard(
                users, page=0, action="deduct_balance"
            ),
            parse_mode="HTML",
        )

    # Быстрый путь из карточки пользователя
    @admin_router.callback_query(F.data.startswith("admin_deduct_balance_"))
    async def admin_deduct_balance_user(callback: types.CallbackQuery, state: FSMContext):
        if not is_admin(callback.from_user.id):
            await callback.answer("🚫 У вас нет прав", show_alert=True)
            return
        await callback.answer()
        try:
            user_id = int(callback.data.split("_")[-1])
        except Exception:
            await callback.message.answer("❌ Неверный формат user_id")
            return
        await state.update_data(
            target_user_id=user_id,
            anchor_chat_id=callback.message.chat.id,
            anchor_msg_id=callback.message.message_id,
        )
        await state.set_state(AdminMainDeduct.waiting_for_amount)
        await safe_edit_text(callback, 
            f"💰 <b>Пользователь {user_id}. Введите сумму списания (в рублях):</b>",
            reply_markup=keyboards.create_admin_cancel_keyboard(),
            parse_mode="HTML",
        )

    # Пагинация списка пользователей
    @admin_router.callback_query(F.data.startswith("admin_deduct_balance_pick_user_page_"))
    async def admin_deduct_balance_pick_user_page(callback: types.CallbackQuery, state: FSMContext):
        if not is_admin(callback.from_user.id):
            await callback.answer("🚫 У вас нет прав", show_alert=True)
            return
        await callback.answer()
        try:
            page = int(callback.data.split("_")[-1])
        except Exception:
            page = 0
        users = await asyncio.to_thread(get_all_users)
        await safe_edit_text(callback, 
            "➖ <b>Списание баланса</b>\n\nВыберите пользователя:",
            reply_markup=keyboards.create_admin_users_pick_keyboard(
                users, page=page, action="deduct_balance"
            ),
            parse_mode="HTML",
        )

    # Выбор пользователя -> ввод суммы
    @admin_router.callback_query(F.data.startswith("admin_deduct_balance_pick_user_"))
    async def admin_deduct_balance_pick_user(callback: types.CallbackQuery, state: FSMContext):
        if not is_admin(callback.from_user.id):
            await callback.answer("🚫 У вас нет прав", show_alert=True)
            return
        await callback.answer()
        try:
            user_id = int(callback.data.split("_")[-1])
        except Exception:
            await callback.message.answer("❌ Неверный формат user_id")
            return
        await state.update_data(
            target_user_id=user_id,
            anchor_chat_id=callback.message.chat.id,
            anchor_msg_id=callback.message.message_id,
        )
        await state.set_state(AdminMainDeduct.waiting_for_amount)
        await safe_edit_text(callback, 
            f"💰 <b>Пользователь {user_id}. Введите сумму списания (в рублях):</b>",
            reply_markup=keyboards.create_admin_cancel_keyboard(),
            parse_mode="HTML",
        )

    @admin_router.message(AdminMainDeduct.waiting_for_amount)
    async def handle_deduct_amount(message: types.Message, state: FSMContext):
        if not is_admin(message.from_user.id):
            return
        data = await state.get_data()
        user_id = int(data.get("target_user_id"))
        chat_id = data.get("anchor_chat_id", message.chat.id)
        msg_id = data.get("anchor_msg_id")
        try:
            await message.delete()
        except Exception:
            pass
        try:
            amount = float(message.text.strip().replace(",", "."))
        except Exception:
            amount = None
        if amount is None or amount <= 0:
            note = (
                "❌ Введите число — сумму в рублях"
                if amount is None
                else "❌ Сумма должна быть положительной"
            )
            text = f"{note}\n\n💰 <b>Пользователь {user_id}. Введите сумму списания (в рублях):</b>"
            if msg_id:
                try:
                    await message.bot.edit_message_text(
                        text,
                        chat_id=chat_id,
                        message_id=msg_id,
                        reply_markup=keyboards.create_admin_cancel_keyboard(),
                        parse_mode="HTML",
                    )
                    return
                except TelegramBadRequest:
                    pass
            await message.answer(
                text,
                reply_markup=keyboards.create_admin_cancel_keyboard(),
                parse_mode="HTML",
            )
            return
        await state.update_data(pending_amount=amount)
        await state.set_state(AdminMainDeduct.waiting_for_confirm)
        confirm_text = f"⚠️ <b>Списать {amount:.2f} ₽ у пользователя {user_id}?</b>"
        if msg_id:
            try:
                await message.bot.edit_message_text(
                    confirm_text,
                    chat_id=chat_id,
                    message_id=msg_id,
                    reply_markup=keyboards.create_admin_balance_confirm_keyboard("deduct"),
                    parse_mode="HTML",
                )
                return
            except TelegramBadRequest:
                pass
        sent = await message.answer(
            confirm_text,
            reply_markup=keyboards.create_admin_balance_confirm_keyboard("deduct"),
            parse_mode="HTML",
        )
        await state.update_data(anchor_chat_id=sent.chat.id, anchor_msg_id=sent.message_id)

    @admin_router.callback_query(
        AdminMainDeduct.waiting_for_confirm, F.data == "admin_balance_confirm_deduct"
    )
    async def admin_balance_confirm_deduct(callback: types.CallbackQuery, state: FSMContext):
        if not is_admin(callback.from_user.id):
            await callback.answer("🚫 У вас нет прав", show_alert=True)
            return
        await callback.answer("⏳ Списываю...", show_alert=False)
        data = await state.get_data()
        user_id = int(data.get("target_user_id"))
        amount = float(data.get("pending_amount", 0))
        await state.clear()
        try:
            ok = deduct_from_balance(user_id, amount)
            if ok:
                try:
                    user_info = await asyncio.to_thread(get_user, user_id)
                    log_username = user_info.get("username", "N/A") if user_info else "N/A"
                    log_transaction(
                        username=log_username,
                        transaction_id=None,
                        payment_id=str(uuid.uuid4()),
                        user_id=user_id,
                        status="internal",
                        amount_rub=amount,
                        amount_currency=None,
                        currency_name=None,
                        payment_method="Admin",
                        metadata=json.dumps(
                            {
                                "action": "admin_deduct",
                                "admin_id": callback.from_user.id,
                            }
                        ),
                    )
                except Exception:
                    pass
                try:
                    await callback.bot.send_message(
                        user_id,
                        f"➖ <b>С вашего баланса списано {amount:.2f} ₽ администратором. Если это ошибка — напишите в поддержку</b>",
                        reply_markup=keyboards.create_support_keyboard(),
                    )
                except Exception:
                    pass
                await _render_user_card(
                    callback.bot,
                    callback.message.chat.id,
                    callback.message.message_id,
                    user_id,
                    note=f"✅ Списано {amount:.2f} ₽",
                )
            else:
                await delete_and_send(callback.message, 
                    "❌ Пользователь не найден или недостаточно средств"
                )
        except Exception as e:
            await delete_and_send(callback.message, f"❌ Ошибка списания: {e}")

    @admin_router.callback_query(
        AdminMainDeduct.waiting_for_confirm, F.data == "admin_balance_cancel_deduct"
    )
    async def admin_balance_cancel_deduct(callback: types.CallbackQuery, state: FSMContext):
        if not is_admin(callback.from_user.id):
            await callback.answer("🚫 У вас нет прав", show_alert=True)
            return
        await callback.answer("Отменено", show_alert=False)
        data = await state.get_data()
        user_id = int(data.get("target_user_id"))
        await state.clear()
        await _render_user_card(
            callback.bot, callback.message.chat.id, callback.message.message_id, user_id
        )

    # --- Просмотр ключей на хосте ---
    class AdminHostKeys(StatesGroup):
        picking_host = State()

    @admin_router.callback_query(F.data == "admin_host_keys")
    async def admin_host_keys_entry(callback: types.CallbackQuery, state: FSMContext):
        if not is_admin(callback.from_user.id):
            await callback.answer("🚫 У вас нет прав", show_alert=True)
            return
        await callback.answer()
        await state.clear()
        await state.set_state(AdminHostKeys.picking_host)
        hosts = await asyncio.to_thread(get_all_hosts)
        await safe_edit_text(callback, 
            "🌍 <b>Выберите сервер для просмотра подписок:</b>",
            reply_markup=keyboards.create_admin_hosts_pick_keyboard(hosts, action="hostkeys"),
            parse_mode="HTML",
        )

    @admin_router.callback_query(
        AdminHostKeys.picking_host, F.data.startswith("admin_hostkeys_pick_host_")
    )
    async def admin_host_keys_pick_host(callback: types.CallbackQuery, state: FSMContext):
        if not is_admin(callback.from_user.id):
            await callback.answer("🚫 У вас нет прав", show_alert=True)
            return
        await callback.answer()
        token = callback.data[len("admin_hostkeys_pick_host_") :]
        # Ищем сервер по токену
        hosts = await asyncio.to_thread(get_all_hosts) or []
        host = keyboards.find_host_by_callback_token(hosts, token)
        if not host:
            await callback.answer("❌ Сервер не найден", show_alert=False)
            return
        host_name = host["host_name"]
        # Сохраняем контекст текущего хоста, чтобы корректно работать с кнопкой "Назад"
        try:
            await state.update_data(hostkeys_host=host_name)
        except Exception:
            pass
        keys = get_keys_for_host(host_name)
        await safe_edit_text(callback, 
            f"🔑 <b>Подписки на сервере {host_name}:</b>",
            reply_markup=keyboards.create_admin_keys_for_host_keyboard(host_name, keys, page=0),
            parse_mode="HTML",
        )

    @admin_router.callback_query(
        AdminHostKeys.picking_host, F.data.startswith("admin_hostkeys_page_")
    )
    async def admin_host_keys_page_nav(callback: types.CallbackQuery, state: FSMContext):
        if not is_admin(callback.from_user.id):
            await callback.answer("🚫 У вас нет прав", show_alert=True)
            return
        await callback.answer()
        # Определяем номер страницы и текущий сервер
        try:
            page = int(callback.data.split("_")[-1])
        except Exception:
            page = 0
        data = await state.get_data()
        host_name = (data or {}).get("hostkeys_host")
        if not host_name:
            # Если по какой-то причине контекст потерялся — возвращаемся к выбору хоста
            hosts = await asyncio.to_thread(get_all_hosts)
            await safe_edit_text(callback, 
                "🌍 <b>Выберите сервер для просмотра подписок:</b>",
                reply_markup=keyboards.create_admin_hosts_pick_keyboard(hosts, action="hostkeys"),
                parse_mode="HTML",
            )
            return
        keys = get_keys_for_host(host_name)
        await safe_edit_text(callback, 
            f"🔑 <b>Подписки на сервере {host_name}:</b>",
            reply_markup=keyboards.create_admin_keys_for_host_keyboard(host_name, keys, page=page),
            parse_mode="HTML",
        )

    @admin_router.callback_query(
        AdminHostKeys.picking_host, F.data == "admin_hostkeys_back_to_hosts"
    )
    async def admin_hostkeys_back_to_hosts(callback: types.CallbackQuery, state: FSMContext):
        if not is_admin(callback.from_user.id):
            await callback.answer("🚫 У вас нет прав", show_alert=True)
            return
        await callback.answer()
        # Сбрасываем контекст выбранного хоста
        try:
            await state.update_data(hostkeys_host=None)
        except Exception:
            pass
        hosts = await asyncio.to_thread(get_all_hosts)
        await safe_edit_text(callback, 
            "🌍 <b>Выберите сервер для просмотра подписок:</b>",
            reply_markup=keyboards.create_admin_hosts_pick_keyboard(hosts, action="hostkeys"),
            parse_mode="HTML",
        )

    @admin_router.callback_query(F.data == "admin_hostkeys_back_to_users")
    async def admin_hostkeys_back_to_users(callback: types.CallbackQuery, state: FSMContext):
        if not is_admin(callback.from_user.id):
            await callback.answer("🚫 У вас нет прав", show_alert=True)
            return
        await callback.answer()
        await show_admin_menu(callback.message, edit_message=True)

    # --- Поиск пользователей по Telegram ID/username ---
    class AdminSearch(StatesGroup):
        waiting_for_query = State()

    @admin_router.callback_query(F.data == "admin_search_user")
    async def admin_search_user_start(callback: types.CallbackQuery, state: FSMContext):
        # -------------------------------------------------------------------------
        # 1. Инициализация процесса поиска и перевод FSM в режим ожидания ввода
        # -------------------------------------------------------------------------
        if not is_admin(callback.from_user.id):
            await callback.answer("🚫 У вас нет прав", show_alert=True)
            return
        await callback.answer()

        await state.set_state(AdminSearch.waiting_for_query)
        # Редактируем сообщение и сохраняем его ID в FSM, чтобы гарантированно удалить при любом исходе
        prompt_msg = await safe_edit_text(callback, 
            "🆔 <b>Введите Telegram ID или имя пользователя:</b>",
            reply_markup=keyboards.create_admin_cancel_keyboard(),
            parse_mode="HTML",
        )
        await state.update_data(prompt_msg_id=prompt_msg.message_id)

    @admin_router.message(AdminSearch.waiting_for_query)
    async def admin_search_user_process(message: types.Message, state: FSMContext):
        # -------------------------------------------------------------------------
        # 1. Валидация входящего текста и проверка прав доступа
        # -------------------------------------------------------------------------
        if not is_admin(message.from_user.id):
            await state.clear()
            return

        query = (message.text or "").strip()

        # Получаем данные из FSM (id сообщения-подсказки), но состояние пока не чистим
        state_data = await state.get_data()
        prompt_msg_id = state_data.get("prompt_msg_id")

        if not query:
            await message.answer("❌ Введите корректный запрос")
            return

        # Удаляем текстовый запрос самого админа, чтобы в чате не висел мусор
        try:
            await message.delete()
        except Exception:
            pass

        target_user = None

        # -------------------------------------------------------------------------
        # 2. Поиск пользователя по Telegram ID (если введены только цифры)
        # -------------------------------------------------------------------------
        if query.isdigit():
            target_user = get_user(int(query))

        # -------------------------------------------------------------------------
        # 3. Локальный поиск по username (если по ID совпадений не найдено)
        # -------------------------------------------------------------------------
        if not target_user:
            clean_query = query.lstrip("@").lower()
            all_users = await asyncio.to_thread(get_all_users)
            for u in all_users:
                if u.get("username") and u["username"].lower() == clean_query:
                    target_user = u
                    break

        # -------------------------------------------------------------------------
        # 4. Обработка исключения при отсутствии совпадений в базе данных
        # -------------------------------------------------------------------------
        if not target_user:
            # Удаляем старое сообщение-подсказку, чтобы не плодить клавиатуры отмены
            if prompt_msg_id:
                try:
                    await message.bot.delete_message(
                        chat_id=message.chat.id, message_id=prompt_msg_id
                    )
                except Exception:
                    pass

            # Отправляем новое уведомление об ошибке и сохраняем его ID, оставаясь в состоянии ожидания
            new_prompt_msg = await message.answer(
                f"❌ <b>Пользователь {query} не найден. Попробуйте еще раз:</b>",
                reply_markup=keyboards.create_admin_cancel_keyboard(),
                parse_mode="HTML",
            )
            await state.update_data(prompt_msg_id=new_prompt_msg.message_id)
            return

        # -------------------------------------------------------------------------
        # Уборка в интерфейсе (выполняется только при успешном нахождении юзера)
        # -------------------------------------------------------------------------
        await state.clear()

        # Удаляем последнее оставшееся сообщение с предложением ввода
        if prompt_msg_id:
            try:
                await message.bot.delete_message(chat_id=message.chat.id, message_id=prompt_msg_id)
            except Exception:
                pass

        # -------------------------------------------------------------------------
        # 5. Успешный исход: очистка FSM и отправка буферного сообщения
        # -------------------------------------------------------------------------
        bot_msg = await message.answer("⏳ Загрузка карточки пользователя...")

        # -------------------------------------------------------------------------
        # 6. Отрисовываем карточку пользователя напрямую
        #    Раньше здесь конструировался "фейковый" types.CallbackQuery и
        #    передавался в admin_view_user_handler, но у объекта, собранного
        #    вручную (не через диспетчер aiogram), свойство .bot не резолвится
        #    в реальный экземпляр Bot и остаётся None, из-за чего
        #    _render_user_card падал с AttributeError на bot.edit_message_text.
        #    is_admin уже проверен в начале этой функции (см. п.1)
        # -------------------------------------------------------------------------
        await _render_user_card(
            message.bot, message.chat.id, bot_msg.message_id, target_user["telegram_id"]
        )

    # --- Быстрое удаление ключа по ID/Email ---
    class AdminQuickDeleteKey(StatesGroup):
        waiting_for_identifier = State()

    @admin_router.callback_query(F.data == "admin_delete_key")
    async def admin_delete_key_entry(callback: types.CallbackQuery, state: FSMContext):
        if not is_admin(callback.from_user.id):
            await callback.answer("🚫 У вас нет прав", show_alert=True)
            return
        await callback.answer()
        await state.set_state(AdminQuickDeleteKey.waiting_for_identifier)
        await safe_edit_text(callback, 
            "🗑 <b>Введите <code>key_id</code> или <code>email</code> подписки для удаления:</b>",
            reply_markup=keyboards.create_admin_cancel_keyboard(),
            parse_mode="HTML",
        )

    @admin_router.message(AdminQuickDeleteKey.waiting_for_identifier)
    async def admin_delete_key_process(message: types.Message, state: FSMContext):
        if not is_admin(message.from_user.id):
            return
        text = (message.text or "").strip()
        try:
            await message.delete()
        except Exception:
            pass
        key = None
        # Сначала попробуем как ID
        try:
            key_id = int(text)
            key = await asyncio.to_thread(get_key_by_id, key_id)
        except Exception:
            # Затем как email
            key = get_key_by_email(text)
        if not key:
            await message.answer("❌ Подписка не найдена. Пришлите корректный key_id или email")
            return
        key_id = int(key.get("key_id"))
        email = key.get("key_email") or "—"
        host = key.get("host_name") or "—"
        await state.clear()
        await message.answer(
            f"🗑 <b>Подтвердите удаление подписки № {key_id}</b>\n\n🆔 <b>Email:</b> {email}\n🗺️ <b>Сервер:</b> {host}",
            reply_markup=keyboards.create_admin_delete_key_confirm_keyboard(key_id),
        )

    # --- Продление ключа на N дней ---
    class AdminExtendKey(StatesGroup):
        waiting_for_pair = State()

    @admin_router.callback_query(F.data == "admin_extend_key")
    async def admin_extend_key_entry(callback: types.CallbackQuery, state: FSMContext):
        if not is_admin(callback.from_user.id):
            await callback.answer("🚫 У вас нет прав", show_alert=True)
            return
        await callback.answer()
        await state.set_state(AdminExtendKey.waiting_for_pair)
        await safe_edit_text(callback, 
            "➕ <b>Введите: <code>key_id дни</code> (сколько дней добавить к подписке)</b>",
            reply_markup=keyboards.create_admin_cancel_keyboard(),
        )

    @admin_router.message(AdminExtendKey.waiting_for_pair)
    async def admin_extend_key_process(message: types.Message, state: FSMContext):
        if not is_admin(message.from_user.id):
            return
        parts = (message.text or "").strip().split()
        try:
            await message.delete()
        except Exception:
            pass
        if len(parts) != 2:
            await message.answer("❌ Формат: <code>key_id дни</code>")
            return
        try:
            key_id = int(parts[0])
            days = int(parts[1])
        except Exception:
            await message.answer("❌ Оба значения должны быть числами")
            return
        if days <= 0:
            await message.answer("❌ Количество дней должно быть положительным")
            return
        key = await asyncio.to_thread(get_key_by_id, key_id)
        if not key:
            await message.answer("❌ Подписка не найдена")
            return
        host = key.get("host_name")
        email = key.get("key_email")
        if not host or not email:
            await message.answer("❌ У подписки отсутствуют данные о хосте или email")
            return
        # Обновим на хосте
        resp = None
        try:
            resp = await create_or_update_key_on_host(host, email, days_to_add=days)
        except Exception as e:
            logger.error(
                f"Продление: не удалось обновить клиента на хосте '{host}' для ключа № {key_id}: {e}"
            )
        if not resp or not resp.get("client_uuid") or not resp.get("expiry_timestamp_ms"):
            await message.answer("❌ Не удалось продлить подписку на сервере")
            return
        # Обновим в БД
        try:
            update_key_info(key_id, resp["client_uuid"], int(resp["expiry_timestamp_ms"]))
        except Exception as e:
            logger.error(f"Продление: не удалось обновить БД для ключа № {key_id}: {e}")
        await state.clear()
        await message.answer(f"✅ Подписка № {key_id} продлена на {days} дн.")
        # Попробуем уведомить пользователя
        try:
            await message.bot.send_message(
                int(key.get("user_id")),
                f"ℹ️ Администратор продлил вашу подписку № {key_id} на {days} дн.",
            )
        except Exception:
            pass

    @admin_router.callback_query(F.data == "start_broadcast")
    async def start_broadcast_handler(callback: types.CallbackQuery, state: FSMContext):
        if not is_admin(callback.from_user.id):
            await callback.answer("🚫 У вас нет прав", show_alert=True)
            return
        await callback.answer()
        await safe_edit_text(callback, 
            "📣 <b>Кому отправляем рассылку?</b>",
            reply_markup=keyboards.create_broadcast_segment_keyboard(),
            parse_mode="HTML",
        )
        await state.set_state(Broadcast.waiting_for_segment)

    @admin_router.callback_query(Broadcast.waiting_for_segment, F.data == "broadcast_segment_all")
    async def broadcast_segment_all_handler(callback: types.CallbackQuery, state: FSMContext):
        await callback.answer()
        await state.update_data(segment="all", segment_label="все пользователи")
        await _broadcast_ask_message(callback.message, state)

    @admin_router.callback_query(
        Broadcast.waiting_for_segment, F.data == "broadcast_segment_never_purchased"
    )
    async def broadcast_segment_never_purchased_handler(
        callback: types.CallbackQuery, state: FSMContext
    ):
        await callback.answer()
        await state.update_data(segment="never_purchased", segment_label="ни разу не покупавшие")
        await _broadcast_ask_message(callback.message, state)

    @admin_router.callback_query(
        Broadcast.waiting_for_segment, F.data == "broadcast_segment_paid_no_active"
    )
    async def broadcast_segment_paid_no_active_handler(
        callback: types.CallbackQuery, state: FSMContext
    ):
        await callback.answer()
        await state.update_data(
            segment="paid_no_active",
            segment_label="платившие раньше, но без активной подписки",
        )
        await _broadcast_ask_message(callback.message, state)

    async def _broadcast_ask_message(message: types.Message, state: FSMContext):
        await message.edit_text(
            "Пришлите сообщение, которое вы хотите разослать.\n"
            "Вы можете использовать форматирование (<b>жирный</b>, <i>курсив</i>).\n"
            "Также поддерживаются фото, видео и документы.\n",
            reply_markup=keyboards.create_broadcast_cancel_keyboard(),
        )
        await state.set_state(Broadcast.waiting_for_message)

    @admin_router.message(Broadcast.waiting_for_message)
    async def broadcast_message_received_handler(message: types.Message, state: FSMContext):
        # Сообщение не удаляем — оно остаётся "мастер-копией" на весь черновик
        # рассылки (настройка кнопки/промокода, подтверждение) и дальше
        # переиспользуется через bot.copy_message и в show_broadcast_preview,
        # и в самой рассылке на каждого получателя (см. confirm_broadcast_handler,
        # from_chat_id=original_message.chat.id/message_id) — если его удалить,
        # copy_message падает на каждом получателе с "message to copy not found".
        await state.update_data(message_to_send=message.model_dump_json())
        await message.answer(
            "✅ <b>Сообщение получено. Хотите добавить к нему кнопку со ссылкой?</b>",
            reply_markup=keyboards.create_broadcast_options_keyboard(),
        )
        await state.set_state(Broadcast.waiting_for_button_option)

    @admin_router.callback_query(
        Broadcast.waiting_for_button_option, F.data == "broadcast_add_button"
    )
    async def add_button_prompt_handler(callback: types.CallbackQuery, state: FSMContext):
        await callback.answer()
        await safe_edit_text(callback, 
            "✅ <b>Хорошо. Теперь отправьте мне текст для кнопки</b>",
            reply_markup=keyboards.create_broadcast_cancel_keyboard(),
        )
        await state.set_state(Broadcast.waiting_for_button_text)

    @admin_router.message(Broadcast.waiting_for_button_text)
    async def button_text_received_handler(message: types.Message, state: FSMContext):
        await state.update_data(button_text=message.text)
        try:
            await message.delete()
        except Exception:
            pass
        await message.answer(
            "✅ <b>Текст кнопки получен. Теперь отправьте ссылку (URL), куда она будет вести:</b>",
            reply_markup=keyboards.create_broadcast_cancel_keyboard(),
        )
        await state.set_state(Broadcast.waiting_for_button_url)

    @admin_router.message(Broadcast.waiting_for_button_url)
    async def button_url_received_handler(message: types.Message, state: FSMContext, bot: Bot):
        url_to_check = message.text
        # Простая проверка схемы. Дальнейшую валидацию можно расширить при необходимости.
        if not (url_to_check.startswith("http://") or url_to_check.startswith("https://")):
            try:
                await message.delete()
            except Exception:
                pass
            await message.answer(
                "❌ Ссылка должна начинаться с http:// или https://. Попробуйте еще раз"
            )
            return
        await state.update_data(button_url=url_to_check)
        try:
            await message.delete()
        except Exception:
            pass
        await _broadcast_ask_promo(message, state)

    @admin_router.callback_query(
        Broadcast.waiting_for_button_option, F.data == "broadcast_skip_button"
    )
    async def skip_button_handler(callback: types.CallbackQuery, state: FSMContext, bot: Bot):
        await callback.answer()
        await state.update_data(button_text=None, button_url=None)
        await _broadcast_ask_promo(callback.message, state)

    async def _broadcast_ask_promo(message: types.Message, state: FSMContext):
        data = await state.get_data()
        segment = data.get("segment", "all")
        if segment == "all":
            # Личный промокод имеет смысл только для адресной рассылки —
            # на "всех" его лучше делать общим промокодом через отдельный раздел
            await state.update_data(promo_discount=None)
            await show_broadcast_preview(message, state, message.bot)
            return
        await message.answer(
            "🎁 <b>Приложить каждому получателю личный одноразовый промокод?</b>\n\n"
            "Полезно для «забытых» — своя уникальная скидка, которую больше никто не сможет использовать",
            reply_markup=keyboards.create_broadcast_promo_option_keyboard(),
        )
        await state.set_state(Broadcast.waiting_for_promo_option)

    @admin_router.callback_query(
        Broadcast.waiting_for_promo_option, F.data == "broadcast_add_promo"
    )
    async def broadcast_add_promo_handler(callback: types.CallbackQuery, state: FSMContext):
        await callback.answer()
        await safe_edit_text(callback, 
            "✅ <b>Укажите размер скидки в процентах (например, 15):</b>",
            reply_markup=keyboards.create_broadcast_cancel_keyboard(),
        )
        await state.set_state(Broadcast.waiting_for_promo_discount)

    @admin_router.message(Broadcast.waiting_for_promo_discount)
    async def broadcast_promo_discount_received_handler(
        message: types.Message, state: FSMContext, bot: Bot
    ):
        try:
            discount = float(message.text.strip().replace(",", "."))
            if not (0 < discount <= 100):
                raise ValueError
        except (TypeError, ValueError):
            try:
                await message.delete()
            except Exception:
                pass
            await message.answer("❌ Введите число от 1 до 100 (процент скидки)")
            return
        await state.update_data(promo_discount=discount)
        try:
            await message.delete()
        except Exception:
            pass
        await show_broadcast_preview(message, state, bot)

    @admin_router.callback_query(
        Broadcast.waiting_for_promo_option, F.data == "broadcast_skip_promo"
    )
    async def broadcast_skip_promo_handler(
        callback: types.CallbackQuery, state: FSMContext, bot: Bot
    ):
        await callback.answer()
        await state.update_data(promo_discount=None)
        await show_broadcast_preview(callback.message, state, bot)

    async def show_broadcast_preview(message: types.Message, state: FSMContext, bot: Bot):
        data = await state.get_data()
        message_json = data.get("message_to_send")
        original_message = types.Message.model_validate_json(message_json)

        button_text = data.get("button_text")
        button_url = data.get("button_url")
        segment = data.get("segment", "all")
        segment_label = data.get("segment_label", "всем пользователям")
        promo_discount = data.get("promo_discount")

        audience = await asyncio.to_thread(get_users_by_segment, segment)
        audience_count = len([u for u in audience if not u.get("is_banned")])

        preview_keyboard = None
        if button_text and button_url:
            builder = InlineKeyboardBuilder()
            builder.button(text=button_text, url=button_url)
            preview_keyboard = builder.as_markup()

        summary = f"👀 <b>Получат:</b> {segment_label} ({audience_count} чел.)\n"
        if promo_discount:
            summary += f"🎁 <b>Личный промокод:</b> скидка {promo_discount:.0f}%, одноразовый, каждому свой\n"
        summary += "\nВот так будет выглядеть ваше сообщение. Отправляем?"

        await message.answer(
            summary,
            reply_markup=keyboards.create_broadcast_confirmation_keyboard(),
            parse_mode="HTML",
        )

        await bot.copy_message(
            chat_id=message.chat.id,
            from_chat_id=original_message.chat.id,
            message_id=original_message.message_id,
            reply_markup=preview_keyboard,
        )

        await state.set_state(Broadcast.waiting_for_confirmation)

    @admin_router.callback_query(Broadcast.waiting_for_confirmation, F.data == "confirm_broadcast")
    async def confirm_broadcast_handler(callback: types.CallbackQuery, state: FSMContext, bot: Bot):
        await delete_and_send(callback.message, "⏳ Начинаю рассылку... Это может занять некоторое время")

        data = await state.get_data()
        message_json = data.get("message_to_send")
        original_message = types.Message.model_validate_json(message_json)

        button_text = data.get("button_text")
        button_url = data.get("button_url")
        segment = data.get("segment", "all")
        promo_discount = data.get("promo_discount")

        final_keyboard = None
        if button_text and button_url:
            builder = InlineKeyboardBuilder()
            builder.button(text=button_text, url=button_url)
            final_keyboard = builder.as_markup()

        await state.clear()

        users = await asyncio.to_thread(get_users_by_segment, segment)
        logger.info(f"Рассылка (сегмент '{segment}'): начинаем перебор {len(users)} пользователей")

        sent_count = 0
        failed_count = 0
        banned_count = 0
        blocked_by_user = 0
        promo_failed_count = 0

        for user in users:
            user_id = user["telegram_id"]
            if user.get("is_banned"):
                banned_count += 1
                continue

            # Личный одноразовый промокод — генерируем непосредственно перед
            # отправкой, чтобы не плодить лишние коды для тех, кому рассылка
            # так и не дойдёт (например, юзер уже заблокировал бота)
            personal_code = None
            if promo_discount:
                personal_code = f"BACK{user_id}{secrets.token_hex(2).upper()}"
                try:
                    created = await asyncio.to_thread(
                        create_promo_code,
                        personal_code,
                        discount_percent=float(promo_discount),
                        usage_limit_total=1,
                        usage_limit_per_user=1,
                        description=f"Персональный промокод из рассылки для {user_id}",
                    )
                    if not created:
                        personal_code = None
                        promo_failed_count += 1
                except Exception as e:
                    logger.error(f"Не удалось создать персональный промокод для {user_id}: {e}")
                    personal_code = None
                    promo_failed_count += 1

            try:
                await bot.copy_message(
                    chat_id=user_id,
                    from_chat_id=original_message.chat.id,
                    message_id=original_message.message_id,
                    reply_markup=final_keyboard,
                )
                if personal_code:
                    try:
                        await bot.send_message(
                            user_id,
                            f"🎁 <b>Ваш персональный промокод: <code>{personal_code}</code></b>\n\n"
                            f"Скидка {promo_discount:.0f}%, действует один раз — только для вас",
                            parse_mode="HTML",
                        )
                    except Exception:
                        pass
                sent_count += 1
                await asyncio.sleep(0.05)

            except TelegramRetryAfter as e:
                logger.warning(f"Рейтлимит рассылки! Спим {e.retry_after} сек.")
                await asyncio.sleep(e.retry_after)

                # Повторная попытка с собственным try/except
                try:
                    await bot.copy_message(
                        chat_id=user_id,
                        from_chat_id=original_message.chat.id,
                        message_id=original_message.message_id,
                        reply_markup=final_keyboard,
                    )
                    if personal_code:
                        try:
                            await bot.send_message(
                                user_id,
                                f"🎁 <b>Ваш персональный промокод: <code>{personal_code}</code></b>\n\n"
                                f"Скидка {promo_discount:.0f}%, действует один раз — только для вас",
                                parse_mode="HTML",
                            )
                        except Exception:
                            pass
                    sent_count += 1
                except Exception as internal_e:
                    failed_count += 1
                    logger.error(
                        f"Повторная отправка пользователю {user_id} не удалась: {internal_e}"
                    )

            except TelegramForbiddenError:
                blocked_by_user += 1
                logger.info(f"Пользователь {user_id} заблокировал бота")

            except Exception as e:
                failed_count += 1
                logger.warning(f"Не удалось отправить сообщение пользователю {user_id}: {e}")

        summary_lines = [
            "✅ <b>Рассылка завершена!</b>\n",
            f"👍 <b>Отправлено:</b> {sent_count}",
            f"👎 <b>Не удалось отправить:</b> {failed_count}",
            f"🚫 <b>Пропущено (забанены):</b> {banned_count}",
            f"🗑️ <b>Пропущено (забанили):</b> {blocked_by_user}",
        ]
        if promo_discount:
            summary_lines.append(f"🎁 <b>Не удалось создать промокод:</b> {promo_failed_count}")
        await callback.message.answer("\n".join(summary_lines), parse_mode="HTML")
        await show_admin_menu(callback.message)

    @admin_router.callback_query(StateFilter(Broadcast), F.data == "cancel_broadcast")
    async def cancel_broadcast_handler(callback: types.CallbackQuery, state: FSMContext):
        await callback.answer("⚠️ Рассылка отменена", show_alert=False)
        await state.clear()
        await show_admin_menu(callback.message, edit_message=True)

    # --- Админ-команды для управления заявками на вывод ---
    @admin_router.message(Command(commands=["approve_withdraw"]))
    async def approve_withdraw_handler(message: types.Message):
        if not is_admin(message.from_user.id):
            return
        try:
            user_id = int(message.text.split("_")[-1])
            user = await asyncio.to_thread(get_user, user_id)
            balance = user.get("referral_balance", 0)
            if balance < 100:
                await message.answer("🫰 Баланс пользователя менее 100 ₽")
                return
            set_referral_balance(user_id, 0)
            set_referral_balance_all(user_id, 0)
            await message.answer(f"✅ Выплата {balance:.2f} ₽ пользователю {user_id} подтверждена")
            await message.bot.send_message(
                user_id,
                f"✅ Ваша заявка на вывод {balance:.2f} ₽ одобрена. Деньги будут переведены в ближайшее время",
            )
        except Exception as e:
            await message.answer(f"Ошибка: {e}")

    @admin_router.message(Command(commands=["decline_withdraw"]))
    async def decline_withdraw_handler(message: types.Message):
        if not is_admin(message.from_user.id):
            return
        try:
            user_id = int(message.text.split("_")[-1])
            await message.answer(f"❌ Заявка пользователя {user_id} отклонена")
            await message.bot.send_message(
                user_id,
                "❌ Ваша заявка на вывод отклонена. Проверьте корректность реквизитов и попробуйте снова",
            )
        except Exception as e:
            await message.answer(f"Ошибка: {e}")

    @admin_router.callback_query(F.data == "admin_realtime_online")
    async def admin_realtime_online_handler(callback: types.CallbackQuery, state: FSMContext):
        """Обработчик для отображения текущего онлайна по всем серверам в реальном времени."""
        # -------------------------------------------------------------------------
        # 1. Быстрый ответ
        # -------------------------------------------------------------------------
        if not is_admin(callback.from_user.id):
            await callback.answer("🚫 У вас нет прав", show_alert=True)
            return
        await callback.answer("⏳ Опрашиваю панели...")

        # -------------------------------------------------------------------------
        # 2. Получение списка всех хостов из БД
        # -------------------------------------------------------------------------
        all_hosts = await asyncio.to_thread(database.get_all_hosts)
        if not all_hosts:
            return await delete_and_send(callback.message, "❌ Хосты не найдены")

        # -------------------------------------------------------------------------
        # 3. Параллельный опрос всех панелей через asyncio.gather
        # -------------------------------------------------------------------------
        tasks = [asyncio.to_thread(get_realtime_online_count, h) for h in all_hosts]
        online_results = await asyncio.gather(*tasks)

        # -------------------------------------------------------------------------
        # 4. Формирование итогового сообщения
        # -------------------------------------------------------------------------
        text = "🟢 <b>Текущий онлайн на серверах</b>\n\n"
        total_online = 0

        for host, count in zip(all_hosts, online_results):
            host_name = host["host_name"]
            text += f"🗺️ <b>{host_name}</b>\n👤 {count} человек\n\n"
            total_online += count

        text += f"👥 <b>Всего в сети:</b> {total_online} человек\n\n"
        text += f"🕒 <b>Обновлено:</b> {datetime.now().strftime('%H:%M:%S')}"

        # -------------------------------------------------------------------------
        # 5. Сборка клавиатуры и обновление интерфейса
        # -------------------------------------------------------------------------
        kb = InlineKeyboardBuilder()
        kb.row(InlineKeyboardButton(text="🔄 Обновить", callback_data="admin_realtime_online"))
        kb.row(InlineKeyboardButton(text="⬅️ В админ-меню", callback_data="admin_menu"))

        await safe_edit_text(callback, text=text, reply_markup=kb.as_markup(), parse_mode="HTML")

    @admin_router.callback_query(F.data == "admin_wl_traffic_report")
    async def admin_wl_traffic_report_handler(callback: types.CallbackQuery, state: FSMContext):
        """
        Мониторинг потребления трафика на WL-серверах (Топ-10) и уведомление админа.

        Выполняемые задачи:
        1. Получение ID администратора из настроек БД
        2. Получение списка всех хостов
        3. Для каждого хоста:
           - Авторизация в xUI панели
           - Получение списка inbound'ов
           - Агрегация статистики по UUID (учитывая все инбаунды с меткой 🏳️)
           - Вычисление показателей на основе последнего снимка traffic_snapshots
        4. Сортировка всех найденных клиентов по объему потребления (delta_mb)
        5. Формирование и отправка сообщения админу
        """
        # -------------------------------------------------------------------------
        # 1. Быстрый ответ
        # -------------------------------------------------------------------------
        if not is_admin(callback.from_user.id):
            await callback.answer("🚫 У вас нет прав", show_alert=True)
            return
        await callback.answer("⏳ Анализирую WL трафик...")

        from shop_bot.data_manager.scheduler import (
            CHECK_INTERVAL_SECONDS,
            traffic_snapshots,
        )

        # -------------------------------------------------------------------------
        # 2. Получение списка всех хостов из БД
        # -------------------------------------------------------------------------
        all_hosts = await asyncio.to_thread(database.get_all_hosts)
        if not all_hosts:
            return await delete_and_send(callback.message, "❌ Хосты не найдены")

        all_clients_data = []

        # -------------------------------------------------------------------------
        # 3. Сбор статистики со всех хостов
        # -------------------------------------------------------------------------
        for host in all_hosts:
            host_name = host["host_name"]
            await asyncio.sleep(0.2)

            try:
                # Авторизация в xUI панели хоста
                api, _ = await asyncio.to_thread(
                    login_to_host,
                    host_url=host["host_url"],
                    username=host["host_username"],
                    password=host["host_pass"],
                    inbound_id=host["host_inbound_id"],
                )
                if not api:
                    continue

                # Получение списка всех inbound'ов с меткой 🏳️
                all_inbounds = [
                    ib
                    for ib in (await asyncio.to_thread(api.inbound.get_list))
                    if hasattr(ib, "remark") and "🏳️" in (ib.remark or "")
                ]

                # Агрегируем статистику по UUID
                host_stats = {}
                for ib in all_inbounds:
                    # Идентификатор инбаунда для уникализации ключа email
                    ib_id = getattr(ib, "id", ib.remark)

                    # Маппинг email -> uuid из настроек инбаунда для надежности ID
                    email_to_uuid = {}
                    if ib.settings and ib.settings.clients:
                        email_to_uuid = {c.email: c.id for c in ib.settings.clients}

                    if not hasattr(ib, "client_stats") or not ib.client_stats:
                        continue

                    for client in ib.client_stats:
                        # Используем UUID из настроек, если нет — fallback на email
                        c_uuid = email_to_uuid.get(client.email, str(client.email))

                        if c_uuid not in host_stats:
                            host_stats[c_uuid] = {
                                "total": 0,
                                "emails": set(),
                                "active_emails": set(),
                            }

                        # Расход = входящий + исходящий трафик (stat.total в API — это лимит квоты)
                        client_current_total = client.up + client.down
                        host_stats[c_uuid]["total"] += client_current_total

                        # Проверка индивидуальной активности email с новым префиксом и ib_id
                        email_snapshot_key = f"email:{host_name}:{ib_id}:{client.email}"
                        if email_snapshot_key in traffic_snapshots:
                            if client_current_total > traffic_snapshots[email_snapshot_key]:
                                host_stats[c_uuid]["active_emails"].add(client.email)

                        if client.email:
                            host_stats[c_uuid]["emails"].add(client.email)

                # Перебор агрегированных данных
                for c_uuid, data in host_stats.items():
                    current_total = data["total"]
                    # Поиск снимка с новым префиксом uuid:
                    snapshot_key = f"uuid:{host_name}:{c_uuid}"

                    if snapshot_key in traffic_snapshots:
                        last_total = traffic_snapshots[snapshot_key]
                        delta_bytes = current_total - last_total

                        if delta_bytes > 0:
                            delta_mb = delta_bytes / (1024 * 1024)
                            mbps = (delta_mb * 8) / CHECK_INTERVAL_SECONDS

                            # Отображаем только реально активные email, если они найдены
                            display_emails = (
                                ", ".join(data["active_emails"])
                                if data["active_emails"]
                                else ", ".join(data["emails"])
                            )

                            all_clients_data.append(
                                {
                                    "host_name": host_name,
                                    "combined_emails": display_emails,
                                    "c_uuid": c_uuid,
                                    "delta_mb": delta_mb,
                                    "mbps": mbps,
                                }
                            )

            except Exception as e:
                logger.error(f"TrafficReport: Ошибка сбора данных на {host_name}: {e}")

        # -------------------------------------------------------------------------
        # 5. Сборка клавиатуры и обновление интерфейса
        # -------------------------------------------------------------------------
        kb = InlineKeyboardBuilder()
        kb.row(InlineKeyboardButton(text="🔄 Обновить", callback_data="admin_wl_traffic_report"))
        kb.row(InlineKeyboardButton(text="⬅️ В админ-меню", callback_data="admin_menu"))

        # -------------------------------------------------------------------------
        # 4. Формирование итогового сообщения
        # -------------------------------------------------------------------------
        if not all_clients_data:
            text = (
                f"ℹ️ <b>Нет данных о WL трафике</b>\n\n"
                f"📅 <b>Обновлено:</b> {datetime.now().strftime('%H:%M:%S')}"
            )
            await safe_edit_text(callback, 
                text=text, reply_markup=kb.as_markup(), parse_mode="HTML"
            )
            return

        # Сортируем и берем Топ-10
        top_10 = sorted(all_clients_data, key=lambda x: x["delta_mb"], reverse=True)[:10]

        text = "🏳️ <b>Потребление трафика на WL-серверах</b>\n\n"

        for item in top_10:
            text += (
                f"🗺️ <b>{item['host_name']}</b>\n"
                f"👤 <b>Клиент:</b> {item['combined_emails']}\n"
                f"🆔 <b>UUID:</b> {item['c_uuid']}\n"
                f"📈 <b>Расход:</b> {item['delta_mb']:.2f} МБ\n"
                f"🚀 <b>Скорость:</b> {item['mbps']:.1f} Мбит/с\n\n"
            )

        text += f"🕒 <b>Интервал:</b> {CHECK_INTERVAL_SECONDS // 60} мин.\n\n"
        text += f"📅 <b>Обновлено:</b> {datetime.now().strftime('%H:%M:%S')}"

        await safe_edit_text(callback, text=text, reply_markup=kb.as_markup(), parse_mode="HTML")

    @admin_router.callback_query(F.data == "admin_get_docker_logs")
    async def admin_view_docker_logs(callback: types.CallbackQuery):
        # -------------------------------------------------------------------------
        # 1. Проверка прав доступа администратора
        # -------------------------------------------------------------------------
        if not is_admin(callback.from_user.id):
            await callback.answer("🚫 У вас нет прав", show_alert=True)
            return

        # Отправляем всплывающее уведомление
        await callback.answer("⏳ Читаю логи...")

        # -------------------------------------------------------------------------
        # 2. Получение и обработка текста логов
        # -------------------------------------------------------------------------
        raw_logs = await get_docker_logs(lines_count=25)

        # Безопасно экранируем HTML-символы, чтобы бот не упал из-за кривых логов
        safe_logs = html_escape.escape(raw_logs)

        # Строим итоговое сообщение
        timestamp = datetime.now().strftime("%H:%M:%S")
        header = "📋 <b>Последние логи контейнера</b>\n\n"
        footer = f"\n🕒 <b>Обновлено:</b> {timestamp}"

        # Высчитываем максимальную длину тела логов с учетом лимитов Telegram (4096 символов)
        # Оставляем запас в 500 символов под шапку, футер и HTML-теги
        max_body_length = 4096 - len(header) - len(footer) - 20

        if len(safe_logs) > max_body_length:
            # Если логи слишком длинные, аккуратно обрезаем их сверху
            safe_logs = (
                "... [вывод обрезан из-за лимитов Telegram] ...\n" + safe_logs[-max_body_length:]
            )

        final_text = f"{header}<pre><code>{safe_logs}</code></pre>{footer}"

        # -------------------------------------------------------------------------
        # 3. Вывод результата в интерфейс
        # -------------------------------------------------------------------------
        kb = InlineKeyboardBuilder()
        kb.row(InlineKeyboardButton(text="🔄 Обновить", callback_data="admin_get_docker_logs"))
        kb.row(InlineKeyboardButton(text="⬅️ В админ-меню", callback_data="admin_menu"))

        try:
            await safe_edit_text(callback, 
                text=final_text, reply_markup=kb.as_markup(), parse_mode="HTML"
            )
        except Exception as e:
            logger.error(f"Ошибка отправки логов админу: {e}")
            await safe_edit_text(callback, 
                text=f"❌ <b>Не удалось отобразить логи в формате HTML</b>\n<code>{e}</code>",
                reply_markup=kb.as_markup(),
                parse_mode="HTML",
            )

    @admin_router.callback_query(F.data == "admin_view_cashbox")
    async def admin_view_cashbox_history(callback: types.CallbackQuery):
        # -------------------------------------------------------------------------
        # 1. Проверка прав доступа администратора
        # -------------------------------------------------------------------------
        if not is_admin(callback.from_user.id):
            await callback.answer("🚫 У вас нет прав", show_alert=True)
            return

        await callback.answer("⏳ Считаю деньги...")

        # -------------------------------------------------------------------------
        # 2. Получение последних 10 транзакций из локальной БД
        # -------------------------------------------------------------------------
        transactions = get_latest_transactions(limit=10)

        # -------------------------------------------------------------------------
        # 3. Формирование текста сообщения
        # -------------------------------------------------------------------------
        header = "💸 <b>История кассы</b>\n\n"

        if not transactions:
            body = "ℹ️ Успешных транзакций пока не обнаружено"
        else:
            # Показываем способ оплаты в каждой строке только если в выборке
            # они реально разные — если все платежи прошли через один и тот же
            # способ, достаточно упомянуть его один раз в подвале
            methods = {(tx.get("payment_method") or "").strip() for tx in transactions}
            show_method_per_line = len(methods) > 1

            def _day_key(tx):
                return str(tx.get("created_date"))[:10]

            day_blocks = []
            for day, day_txs in itertools.groupby(transactions, key=_day_key):
                day_txs = list(day_txs)
                try:
                    day_label = datetime.strptime(day, "%Y-%m-%d").strftime("%d.%m.%Y")
                except Exception:
                    day_label = day
                day_total = sum(float(tx.get("amount_rub") or 0) for tx in day_txs)
                count_word = (
                    "платёж"
                    if len(day_txs) % 10 == 1 and len(day_txs) % 100 != 11
                    else (
                        "платежа"
                        if 2 <= len(day_txs) % 10 <= 4 and not (12 <= len(day_txs) % 100 <= 14)
                        else "платежей"
                    )
                )

                lines = [
                    f"📅 <b>{day_label}</b> ▪️ 📈 {len(day_txs)} {count_word} ▪️ 💵 {day_total:.2f} ₽\n"
                ]
                for tx in day_txs:
                    try:
                        date_str = str(tx["created_date"]).split(".")[0]
                        dt = datetime.strptime(date_str, "%Y-%m-%d %H:%M:%S")
                        time_str = dt.strftime("%H:%M")
                    except Exception:
                        time_str = str(tx.get("created_date"))[11:16]

                    user_display = format_stored_username(tx.get("username"), tx.get("user_id"))

                    line = (
                        f"{time_str} ▪️ 💰 <b>+{float(tx['amount_rub']):.2f} ₽</b> ▪️ 👤 {user_display}"
                    )
                    if show_method_per_line:
                        line += f" ▪️ 💳 {tx.get('payment_method') or '—'}"
                    lines.append(line)

                day_blocks.append("\n".join(lines))

            body = "\n\n".join(day_blocks)
            if not show_method_per_line:
                body += f"\n\n💳 <b>Все платежи:</b> {next(iter(methods)) or '—'}"

        footer = f"\n\n🕒 <b>Обновлено:</b> {datetime.now().strftime('%H:%M:%S')}"
        final_text = f"{header}{body}{footer}"

        # -------------------------------------------------------------------------
        # 4. Вывод интерфейса
        # -------------------------------------------------------------------------
        kb = InlineKeyboardBuilder()
        kb.button(text="🔄 Обновить", callback_data="admin_view_cashbox")
        kb.button(text="⬅️ В админ-меню", callback_data="admin_menu")

        await safe_edit_text(callback, 
            text=final_text,
            reply_markup=kb.as_markup(),
            parse_mode="HTML",
            disable_web_page_preview=True,
        )

    @admin_router.callback_query(F.data == "admin_bot_restart")
    async def admin_execute_bot_restart_direct(callback: types.CallbackQuery):
        # -------------------------------------------------------------------------
        # 1. Проверка прав доступа администратора
        # -------------------------------------------------------------------------
        if not is_admin(callback.from_user.id):
            await callback.answer("🚫 У вас нет прав", show_alert=True)
            return

        # Отправляем всплывающее уведомление
        await callback.answer("🔄 Перезапускаю контейнер...", show_alert=False)

        # -------------------------------------------------------------------------
        # 2. Вывод интерфейса до падения процесса
        # -------------------------------------------------------------------------
        kb = InlineKeyboardBuilder()
        kb.row(InlineKeyboardButton(text="⬅️ В админ-меню", callback_data="admin_menu"))

        # Редактируем текст сообщения заранее, чтобы успеть завершить сетевой запрос к Telegram
        await safe_edit_text(callback, 
            text=(
                "🚀 <b>Контейнер отправлен на перезагрузку!</b>\n\n"
                "Команда передана Docker-демону. Бот отключается.\n"
                "Подождите около 5 секунд, пока он поднимется заново, "
                "после чего вы сможете нажать кнопку ниже для возврата в меню"
            ),
            reply_markup=kb.as_markup(),
            parse_mode="HTML",
        )

        # -------------------------------------------------------------------------
        # 3. Запуск фоновой задачи Docker (Fire and Forget)
        # -------------------------------------------------------------------------
        try:
            # Запускаем подпроцесс асинхронно в фоне без await.
            # Бот упадет через долю секунды, но интерфейс уже обновился.
            asyncio.create_task(restart_docker_container(timeout=3))
        except Exception as e:
            logger.error(f"Ошибка при создании фоновой задачи рестарта: {e}")

    return admin_router
