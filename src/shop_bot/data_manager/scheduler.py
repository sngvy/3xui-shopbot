"""Фоновый планировщик: синхронизация ключей с панелями, истечение триалов, мониторинг трафика."""

import asyncio
import hashlib
import html
import json
import logging
import os
import re
import secrets
import time
import urllib.error
import urllib.request
from collections import defaultdict
from datetime import datetime, timedelta
from urllib.parse import urlparse

from aiogram import Bot
from aiogram.exceptions import TelegramForbiddenError, TelegramRetryAfter
from aiogram.utils.keyboard import InlineKeyboardBuilder
from icmplib import async_ping

from shop_bot.bot_controller import BotController
from shop_bot.data_manager import (
    backup_manager,
    database,
    resource_monitor,
    speedtest_runner,
)
from shop_bot.data_manager.database import keys_in_transition
from shop_bot.modules import xui_api
from shop_bot.env_utils import env_float, env_int, env_int_list

BACKUP_KEEP_COUNT = env_int("BACKUP_KEEP_COUNT", 7, 1, 365)  # сколько архивов бэкапа хранить
CHECK_INTERVAL_SECONDS = env_int("SCHEDULER_CHECK_INTERVAL_SECONDS", 300, 30, 3600)
NOTIFY_BEFORE_HOURS = set(env_int_list("NOTIFY_BEFORE_HOURS", [72, 48, 24, 1], 1))
notified_users = {}

# -------------------------------------------------------------------------
# Grace-период между истечением подписки и физическим удалением ключа.
# -------------------------------------------------------------------------
EXPIRED_KEY_DELETION_GRACE = timedelta(days=env_int("EXPIRED_KEY_GRACE_DAYS", 5, 1, 90))

# -------------------------------------------------------------------------
# Инбаунд для доступа к Telegram в течение grace-периода: пока подписка уже
# истекла, но ключ ещё не подошёл к порогу физического удаления
# (EXPIRED_KEY_DELETION_GRACE), пользователю реплицируется отдельный клиент
# на этот инбаунд — с урезанным трафиком, без доступа к обычным клонам.
# -------------------------------------------------------------------------
GRACE_INBOUND_REMARK = "🇱🇨 Подписка истекла. Подключитесь для продления"
GRACE_INBOUND_TOTAL_GB_BYTES = int(env_float("GRACE_TRAFFIC_GB", 1.0, 0.01, 1024) * 1024**3)  # по умолчанию 1 ГБ; totalGB в API 3X-UI — это байты, не гигабайты
GRACE_EMAIL_SUFFIX = "-expired"  # никогда не пересекается с числовыми суффиксами клонов (-2, -3, ...)

logger = logging.getLogger(__name__)

# -------------------------------------------------------------------------
# Дифференцированные интервалы для sync_keys_with_panels
# -------------------------------------------------------------------------
# Чистка ключей, просроченных > 5 дней — не time-critical, гоняем реже
EXPIRED_CLEANUP_INTERVAL_SECONDS = env_int("EXPIRED_CLEANUP_INTERVAL_SECONDS", 30 * 60, 60, 86400)
_last_expired_cleanup_at: datetime | None = None

# Полная сверка панели с БД по всем клиентам (не только по тем, у кого ещё
# жива строка в vpn_keys) — ловит "сирот", которые накопились из-за частично
# неудавшихся удалений в прошлом. Тяжёлая операция (полный обход клиентов
# всех инбаундов хоста), гоняем редко.
ORPHAN_SWEEP_INTERVAL_SECONDS = env_int("ORPHAN_SWEEP_INTERVAL_SECONDS", 24 * 60 * 60, 3600, 30 * 86400)
_last_orphan_sweep_at: datetime | None = None

# Даже если локальный хэш состояния не менялся, в этот интервал всё
# равно делаем полную сверку клонов с панелью, чтобы ловить обновления
# состояния (например, ручное изменение в самой панели)
FULL_RECONCILE_INTERVAL_SECONDS = env_int("FULL_RECONCILE_INTERVAL_SECONDS", 30 * 60, 60, 86400)
_host_state_hash: dict[str, str] = {}
_host_last_full_sync_at: dict[str, datetime] = {}

# Ограничение на число ключей, реально обрабатываемых за один проход
# чистки. Каждый удаляемый ключ — это цикл delete-запросов по всем его
# клонам (потенциально десятки инбаундов), а именно delete — единственная
# неатомарная операция на панели (DelInboundClient не оборачивает
# удаление в транзакцию, в отличие от Add/UpdateInboundClient — см.
# github.com/MHSanaei/3x-ui/issues/4026). Чем крупнее пачка таких
# операций подряд в одном проходе, тем выше суммарный шанс попасть в
# её узкое окно гонки. Если на хосте разом накопилось много просроченных
# ключей — не обязательно вычищать все за один 30-минутный цикл, порядок
# тут не важен: то, что не успели, спокойно останется в БД (как и раньше)
# и будет обработано в следующем цикле, ничего не теряется и не портится.
MAX_EXPIRED_KEYS_PER_CYCLE = env_int("MAX_EXPIRED_KEYS_PER_CYCLE", 5, 1, 100)

# Ограничиваем параллелизм обхода хостов
HOST_SYNC_CONCURRENCY = env_int("HOST_SYNC_CONCURRENCY", 3, 1, 10)

# Запуск обоих видов измерений 3 раза в сутки (каждые 8 часов)
SPEEDTEST_INTERVAL_SECONDS = env_int("SPEEDTEST_INTERVAL_SECONDS", 24 * 3600, 3600, 30 * 86400)
_last_speedtests_run_at: datetime | None = None
_last_backup_run_at: datetime | None = None

# Сбор метрик ресурсов (каждые 5 минут)
METRICS_INTERVAL_SECONDS = env_int("METRICS_INTERVAL_SECONDS", 5 * 60, 30, 86400)
_last_metrics_run_at: datetime | None = None

# Параметры для выявления стационарного использования (роутеры, ПК, торренты)
traffic_snapshots = {}
traffic_strikes = {}
TRAFFIC_THRESHOLD_MB = env_int("TRAFFIC_THRESHOLD_MB", 6000, 1)  # Порог объема за интервал в МБ (детект разовых тяжелых закачек)
SPEED_THRESHOLD_MBPS = env_float("SPEED_THRESHOLD_MBPS", 110.0, 1.0)  # Порог скорости (стабильная полка для детекта провода)
STRIKE_THRESHOLD = env_int("STRIKE_THRESHOLD", 3, 1, 100)  # Кол-во проверок подряд для подтверждения аномалии

# Предупреждение владельцу подписки при аномальном трафике (см. _notify_user_about_traffic).
# Шлётся, только если в интервале была активность минимум у стольких email'ов (мастер + клоны).
# 0 — не предупреждать пользователей вообще.
USER_ALERT_MIN_ACTIVE = env_int("TRAFFIC_USER_ALERT_MIN_ACTIVE", 10, 0, 1000)
# Пауза между повторными предупреждениями одному пользователю, часов.
USER_ALERT_COOLDOWN_HOURS = env_int("TRAFFIC_USER_ALERT_COOLDOWN_HOURS", 6, 1, 168)
# 1 — предупреждать и без аномалии трафика, если активных email'ов >= TRAFFIC_USER_ALERT_MIN_ACTIVE (шеринг).
USER_ALERT_SHARING = env_int("TRAFFIC_USER_ALERT_SHARING", 1, 0, 1)
user_alert_last_sent: dict[int, float] = {}  # user_id -> time.monotonic() последней отправки
sharing_admin_last_sent: dict[tuple[str, str], float] = {}  # (host, uuid) -> последний алерт админу о шеринге
# Защита от ложных срабатываний шеринга (например, пинг подписки в клиенте дёргает все конфиги и даёт
# килобайты трафика на каждом email): email считается активным для шеринга, только если за интервал
# его трафик вырос минимум на столько КБ (0 — любой рост), и условие должно держаться несколько проверок подряд.
ACTIVE_MIN_KB = env_int("TRAFFIC_ACTIVE_MIN_KB", 1024, 0, 1024 * 1024)
USER_ALERT_SHARING_CHECKS = env_int("TRAFFIC_USER_ALERT_SHARING_CHECKS", 2, 1, 100)
sharing_strikes: dict[tuple[str, str], int] = {}  # (host, uuid) -> проверок подряд с шерингом
# Шеринг подтверждаем числом уникальных IP клиента (по записям 3x-ui clientIps, нужен limitIp > 0 у клиентов).
# Один человек: 1–2 IP (мобильная сеть + Wi-Fi). 0 — не проверять IP (считать только email'ы).
USER_ALERT_MIN_IPS = env_int("TRAFFIC_USER_ALERT_MIN_IPS", 3, 0, 1000)
# За какое окно (мин.) считать IP: берутся записи, не старше самой свежей минус это окно.
USER_ALERT_IP_WINDOW_MIN = env_int("TRAFFIC_USER_ALERT_IP_WINDOW_MIN", 10, 1, 1440)
USER_ALERT_IP_MAX_EMAILS = 30  # сколько email'ов подписки опрашивать за проверку
_IP_RECORD_RE = re.compile(r"^\s*(\S+?)\s*\((\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2})\)\s*$")


def _parse_ip_records(records) -> list[tuple[str, float | None]]:
    """Записи clientIps панели -> [(ip, время в секундах или None)].
    Понимает "1.2.3.4 (2026-10-12 11:55:13)", {"ip":..., "timestamp":...} и просто "1.2.3.4".
    Время локальное для ноды; сравниваются только значения между собой, часовой пояс не важен."""
    out: list[tuple[str, float | None]] = []
    for rec in records or []:
        ip, ts = None, None
        if isinstance(rec, dict):
            ip = rec.get("ip")
            t = rec.get("timestamp")
            if isinstance(t, (int, float)):
                ts = float(t) / 1000 if t > 1e11 else float(t)
        elif isinstance(rec, str):
            m = _IP_RECORD_RE.match(rec)
            if m:
                ip = m.group(1)
                try:
                    ts = datetime.strptime(m.group(2), "%Y-%m-%d %H:%M:%S").timestamp()
                except ValueError:
                    ts = None
            else:
                ip = rec.strip()
        if ip:
            out.append((str(ip), ts))
    return out


def _recent_unique_ips(per_email_records: list, window_min: int) -> set[str] | None:
    """Уникальные IP за окно. Опорное «сейчас» — самая свежая запись по подписке: email
    уже признан активным в этом интервале, поэтому она свежая. None — записей IP нет вовсе."""
    parsed = [r for recs in per_email_records for r in _parse_ip_records(recs)]
    if not parsed:
        return None
    stamps = [ts for _, ts in parsed if ts is not None]
    if not stamps:
        return {ip for ip, _ in parsed}  # без времени окно применить нельзя — считаем все
    border = max(stamps) - window_min * 60
    return {ip for ip, ts in parsed if ts is None or ts >= border}


async def _count_subscription_ips(api, emails) -> set[str] | None:
    """Уникальные IP подписки за окно по списку её email'ов. None — панель IP не отдаёт."""
    records = []
    for email in sorted(emails)[:USER_ALERT_IP_MAX_EMAILS]:
        try:
            records.append(await xui_api.run_in_xui_pool(api.client.get_ips, email))
        except Exception as e:
            logger.warning(f"Шеринг: не удалось получить IP для {email}: {e}")
    return _recent_unique_ips(records, USER_ALERT_IP_WINDOW_MIN)

# Множитель пауз шедулера между мутациями панели.
try:
    _PANEL_PAUSE_FACTOR = max(0.0, float(os.environ.get("XUI_SCHEDULER_PAUSE_FACTOR", "0.2")))
except ValueError:
    _PANEL_PAUSE_FACTOR = 0.2


def _find_rename_cycle_member(pending: list[dict]) -> dict | None:
    """Ищет среди застрявших переименований участника замкнутого цикла.

    Элемент item — это клон «current_clone_email -> canonical_email». Цикл
    образуется, когда целевой номер каждого занят текущим номером следующего и
    последний ссылается на первого (например, 10->2, 2->4, 4->10): из-за
    уникальности email такой обмен без временного имени не разрешится никогда.
    Возвращает любой элемент цикла или None, если циклов нет (застрял по причине
    постороннего клиента на целевом слоте — тут временное имя не поможет).
    """
    by_current = {i["current_clone_email"]: i for i in pending}
    for start in pending:
        seen = set()
        cur = start
        while cur is not None and cur["current_clone_email"] not in seen:
            seen.add(cur["current_clone_email"])
            cur = by_current.get(cur["canonical_email"])
            if cur is start:
                return start
    return None


async def _panel_pause(seconds: float) -> None:
    """Пауза между мутациями панели с учётом XUI_SCHEDULER_PAUSE_FACTOR."""
    delay = seconds * _PANEL_PAUSE_FACTOR
    if delay > 0:
        await asyncio.sleep(delay)


class _YieldToPurchase(Exception):
    """Шедулер прерывает проход по хосту, потому что пользовательская операция
    (покупка/продление/удаление) ждёт host_lock. Не ошибка: незавершённая работа
    безопасно (все мутации идемпотентны) доделывается в следующем цикле."""


def _check_yield(host_name: str) -> None:
    """Вызывается на границах итераций под host_lock. Если покупка ждёт лок этого
    хоста, выбрасывает _YieldToPurchase; `async with host_lock` в _sync_single_host
    при выходе отдаёт лок ожидающему (asyncio.Lock — честная очередь FIFO).

    Прерываем, а не продолжаем после паузы, намеренно: за время покупки снимок
    all_inbounds, на котором работает проход, устаревает, и дальнейшие решения по
    нему могли бы дать Duplicate email. Хэш состояния/время полной сверки при
    прерывании не обновляются, поэтому следующий цикл повторит проход заново — на
    уже синхронизированных ключах это быстрые no-op без панельных запросов.
    """
    if xui_api.purchase_waiting(host_name):
        raise _YieldToPurchase()


def format_time_left(hours: int) -> str:
    """Форматирует количество часов в человекочитаемую строку ('N дней'/'N часов')."""

    def get_suffix(num: int, forms: list[str]) -> str:
        if num % 10 == 1 and num % 100 != 11:
            return forms[0]
        elif 2 <= num % 10 <= 4 and (num % 100 < 10 or num % 100 >= 20):
            return forms[1]
        return forms[2]

    if hours >= 24:
        days = hours // 24
        return f"{days} {get_suffix(days, ['день', 'дня', 'дней'])}"

    return f"{hours} {get_suffix(hours, ['час', 'часа', 'часов'])}"


async def send_subscription_notification(
    bot: Bot, user_id: int, key_id: int, time_left_hours: int, expiry_date: datetime
):
    """Отправляет пользователю уведомление о скором истечении подписки с кнопкой продления."""
    try:
        time_text = format_time_left(time_left_hours)
        expiry_str = expiry_date.strftime("%d.%m.%Y в %H:%M")

        message = (
            f"⚠️ <b>Внимание!</b>\n\n"
            f"Срок действия вашей подписки истекает через <b>{time_text}</b>.\n"
            f"<b>Дата окончания:</b> {expiry_str}\n\n"
            f"Продлите подписку, чтобы не остаться без доступа к сервису!"
        )

        builder = InlineKeyboardBuilder()
        builder.button(text="🔑 Мои подписки", callback_data="manage_keys")
        builder.button(
            text="➕ Продлить подписку",
            callback_data=f"extend_key_{key_id}",
            style="danger",
        )
        builder.adjust(2)

        await bot.send_message(
            chat_id=user_id,
            text=message,
            reply_markup=builder.as_markup(),
            parse_mode="HTML",
        )
        logger.debug(
            f"Scheduler: Отправлено уведомление пользователю {user_id} по ключу {key_id} (осталось {time_left_hours} ч)."
        )

    except Exception as e:
        logger.error(f"Scheduler: Ошибка отправки уведомления пользователю {user_id}: {e}")


def _cleanup_notified_users(active_key_ids: set[int]):
    if not notified_users:
        return

    logger.debug("Scheduler: Очищаю кэш уведомлений...")

    users_to_check = list(notified_users.keys())

    cleaned_users = 0
    cleaned_keys = 0

    for user_id in users_to_check:
        keys_to_check = list(notified_users[user_id].keys())
        for key_id in keys_to_check:
            if key_id not in active_key_ids:
                del notified_users[user_id][key_id]
                cleaned_keys += 1

        if not notified_users[user_id]:
            del notified_users[user_id]
            cleaned_users += 1

    if cleaned_users > 0 or cleaned_keys > 0:
        logger.debug(
            f"Scheduler: Очистка завершена. Удалено записей пользователей: {cleaned_users}, ключей: {cleaned_keys}."
        )


async def process_auto_renewals(bot: Bot):
    """Автопродление подписок с баланса — только тем ключам, где пользователь
    явно включил эту опцию (см. set_key_auto_renew, тумблер в карточке ключа).

    Переиспользует ровно тот же путь оплаты, что и ручное «оплатить с
    баланса» — handlers.process_successful_payment с payment_method
    "Balance (auto)". Это гарантирует одинаковую логику с ручной оплатой
    (реферальные начисления, запись в transactions, снятие префикса
    trial_ и т.д.) без дублирования этой логики.

    Порядок в главном цикле важен: вызывается до check_expiring_subscriptions
    — если автопродление сейчас же успешно отодвинуло дату истечения далеко
    вперёд, пользователь не должен в этом же проходе ещё и получить
    "истекает скоро, продлите!" — избыточно и по сути ложно на этот момент.

    Тариф для продления — last_plan_id, сохранённый при последней успешной
    покупке/продлении этого же ключа (см. set_key_last_plan, вызывается из
    process_successful_payment для всех способов оплаты, не только баланса),
    то есть тот же тариф, что был в прошлый раз, а не какой-то дефолт.
    """
    from shop_bot.bot.handlers import process_successful_payment

    keys = await asyncio.to_thread(database.get_keys_due_for_auto_renewal, 24)
    if not keys:
        return

    for key in keys:
        key_id = key["key_id"]
        user_id = key["user_id"]
        try:
            price = float(key["plan_price"])
            months = int(key["plan_months"])
        except (TypeError, ValueError, KeyError):
            logger.warning(
                f"Автопродление: у ключа {key_id} битый/удалённый тариф "
                f"(last_plan_id={key.get('last_plan_id')}) — пропускаю."
            )
            continue

        deducted = await asyncio.to_thread(database.deduct_from_balance, user_id, price)
        if not deducted:
            # Недостаточно средств — не спамим отдельным сообщением здесь,
            # обычные "истекает через X" (check_expiring_subscriptions,
            # идёт следующим шагом за этим вызовом) и так предупредят
            # пользователя пополнить баланс.
            logger.debug(
                f"Автопродление: у ключа {key_id} (юзер {user_id}) недостаточно "
                f"средств на балансе для продления за {price} ₽ — пропускаю."
            )
            continue

        try:
            await bot.send_message(
                chat_id=user_id,
                text=(
                    "💳 <b>Автопродление</b>\n\n"
                    f"Списано {price:.2f} ₽ с баланса — подписка продлена автоматически"
                ),
                parse_mode="HTML",
            )
        except Exception as e:
            logger.warning(f"Автопродление: не удалось уведомить пользователя {user_id}: {e}")

        metadata = {
            "user_id": user_id,
            "months": months,
            "price": price,
            "action": "extend",
            "key_id": key_id,
            "host_name": key["host_name"],
            "plan_id": key["last_plan_id"],
            "customer_email": None,
            "payment_method": "Balance (auto)",
            "promo_code": None,
        }
        # process_successful_payment сама ловит и логирует свои исключения,
        # наружу не пробрасывает (см. её собственный try/except в самом
        # низу) — поймать сбой через except здесь не получится. Вместо
        # этого проверяем по факту: реально ли подвинулась дата истечения.
        expiry_before = key.get("expiry_date")
        try:
            await process_successful_payment(bot, metadata)
        except Exception as e:
            logger.error(f"Автопродление: неожиданное исключение для ключа {key_id}: {e}")

        fresh_key = await asyncio.to_thread(database.get_key_by_id, key_id)
        expiry_advanced = bool(fresh_key) and fresh_key.get("expiry_date") != expiry_before

        if expiry_advanced:
            logger.info(
                f"Автопродление: ключ {key_id} (юзер {user_id}) продлён на "
                f"{months} мес., списано {price:.2f} ₽."
            )
        else:
            logger.error(
                f"Автопродление: ключ {key_id} (юзер {user_id}) — дата истечения "
                f"не сдвинулась после process_successful_payment, продление не "
                f"состоялось. Возвращаю {price:.2f} ₽ на баланс."
            )
            try:
                await asyncio.to_thread(database.add_to_balance, user_id, price)
            except Exception as refund_err:
                logger.error(
                    f"Автопродление: критично — не удалось вернуть {price:.2f} ₽ "
                    f"пользователю {user_id} после сбоя продления ключа {key_id}: {refund_err}"
                )


async def check_expiring_subscriptions(bot: Bot):
    """Проверяет все ключи на скорое истечение и рассылает уведомления пользователям."""
    logger.debug("Scheduler: Проверяю истекающие подписки...")
    current_time = datetime.now()
    # Раньше каждые вытаскивались и разбирались все ключи.
    # Теперь для чистки кэша уведомлений нужны только key_id.
    active_key_ids = await asyncio.to_thread(database.get_all_key_ids)
    _cleanup_notified_users(active_key_ids)

    expiring_keys = await asyncio.to_thread(
        database.get_keys_expiring_within, max(NOTIFY_BEFORE_HOURS)
    )

    for key in expiring_keys:
        try:
            expiry_date = datetime.fromisoformat(key["expiry_date"])
            time_left = expiry_date - current_time

            if time_left.total_seconds() < 0:
                continue

            total_hours_left = time_left.total_seconds() / 3600
            user_id = key["user_id"]
            key_id = key["key_id"]

            for hours_mark in NOTIFY_BEFORE_HOURS:
                if hours_mark - 1 < total_hours_left <= hours_mark:
                    notified_users.setdefault(user_id, {}).setdefault(key_id, set())

                    if hours_mark not in notified_users[user_id][key_id]:
                        await send_subscription_notification(
                            bot, user_id, key_id, hours_mark, expiry_date
                        )
                        notified_users[user_id][key_id].add(hours_mark)
                    break

        except Exception as e:
            logger.error(
                f"Scheduler: Ошибка обработки истечения для ключа {key.get('key_id')}: {e}"
            )


def _compute_host_state_hash(keys_in_db: list[dict], banned_map: dict[int, bool]) -> str:
    """Дешёвый хэш ожидаемого состояния хоста, посчитанный только по локальной БД (без единого запроса к панели). Используется, чтобы не гонять тяжёлую сверку клонов с панелью каждые 5 минут, если с прошлого раза у хоста объективно ничего не изменилось (не было покупок/продлений/банов/удалений).

    Важно: это не отменяет полную сверку совсем — см. FULL_RECONCILE_INTERVAL_SECONDS,
    который форсирует полный проход раз в 30 минут даже при неизменном хэше,
    чтобы ловить обновления панели, произошедшие не через бота.
    """
    parts = []
    for k in sorted(keys_in_db, key=lambda x: x["key_id"]):
        tg_id = k.get("user_id")
        is_banned = banned_map.get(tg_id, False)
        parts.append(f"{k['key_id']}:{k['xui_client_uuid']}:{k['expiry_date']}:{int(is_banned)}")
    raw = "|".join(parts)
    return hashlib.sha256(raw.encode()).hexdigest()


async def _delete_key_everywhere(api, all_inbounds: list, host_name: str, key_row: dict) -> bool:
    """Удаляет один ключ (мастер + возможные клоны) со всех инбаундов панели и из локальной БД.

    Критерии совпадения приведены к тем же, что используются в garbage-цикле
    _sync_clones_for_host, по двум причинам:
    • matching только по (id == uuid_ or email == email_) не находит клонов
      с email вида "{prefix}-N@domain" — их email никогда не равен email_,
      а id для Hysteria2 не всегда совпадает с uuid_ (auth-идентичность не
      всегда доезжает до .id при разборе), поэтому такие клоны молча
      оставались на панели навсегда после удаления записи ключа из БД;
    • next() возвращал только первое совпадение на инбаунде — если на нём
      случайно оказывалось два подходящих клиента (дубль от прошлого сбоя
      синхронизации), второй так и оставался висеть.

    Возвращает True, если запись реально удалена из БД (и клиенты
    вычищены с панели), False — если хотя бы одно удаление на панели не
    удалось. Раньше строка в БД удалялась безусловно, даже если часть
    клиентов на панели не была удалена — в результате такой клиент
    становился "сиротой" навсегда: вся логика поиска мусорных клиентов в
    этом коде заводится от строки в БД, а раз её больше нет — ни один
    будущий цикл этот email больше не рассмотрит. Теперь при частичной
    неудаче строка в БД остаётся, и на следующем цикле очистки (со свежим
    снэпшотом all_inbounds) попытка повторяется заново.
    """
    uuid_ = key_row["xui_client_uuid"]
    email_ = key_row["key_email"]
    prefix = email_.split("@")[0]
    prefix_dash = f"{prefix}-".lower()

    all_deletes_ok = True

    for ib in all_inbounds:
        clients = ib.settings.clients or []
        # c.auth здесь не матчим — py3xui.Client это поле не парсит (см.
        # docstring xui_api.fetch_raw_client_auth_map), getattr всегда
        # вернёт "" и условие было мёртвым кодом, реально ни разу не
        # сработавшим ни на одном клиенте
        targets_to_delete = [
            c
            for c in clients
            if str(getattr(c, "id", "")) == uuid_
            or getattr(c, "email", "") == email_
            or str(getattr(c, "email", "")).strip().lower().startswith(prefix_dash)
        ]
        is_hysteria_ib = xui_api.is_hysteria_protocol(getattr(ib, "protocol", ""))

        for target_client in targets_to_delete:
            deleted_ok = await xui_api.run_in_xui_pool(
                xui_api.delete_client_robust,
                api,
                ib.id,
                target_client,
                uuid_,
                is_hysteria_ib,
            )
            await _panel_pause(1.0)
            if not deleted_ok:
                all_deletes_ok = False
                logger.warning(
                    f"[{host_name}] Не удалось удалить мусорный клиент "
                    f"{getattr(target_client, 'email', email_)} (UUID {uuid_}) на inbound {ib.id}"
                )

    if not all_deletes_ok:
        logger.warning(
            f"[{host_name}] {email_}: не все клиенты удалены с панели, запись в "
            f"БД оставлена — повтор на следующем цикле очистки просроченных ключей."
        )
        return False

    await asyncio.to_thread(database.delete_key_by_email, email_)
    logger.info(f"[{host_name}] Удалён просроченный ключ {email_}")
    return True


async def _cleanup_expired_keys_for_host(
    bot: Bot,
    host_name: str,
    api,
    all_inbounds: list,
    keys_in_db: list[dict],
    now: datetime,
) -> list:
    """Удаляет ключи, просроченные более 5 дней (не time-critical операция, вызывается не каждый цикл — см. EXPIRED_CLEANUP_INTERVAL_SECONDS).

    Возвращает актуальный список inbound'ов: если реально что-то удалили —
    свежий снимок с панели (нужен для корректной последующей сверки клонов),
    если нет — тот же список без лишнего запроса к панели.
    """
    user_keys_map = defaultdict(list)
    for db_key in keys_in_db:
        u_id = db_key.get("user_id")
        if u_id:
            user_keys_map[u_id].append(db_key)

    deleted_any = False
    processed_count = 0

    for tg_id, user_keys in user_keys_map.items():
        _check_yield(host_name)
        if processed_count >= MAX_EXPIRED_KEYS_PER_CYCLE:
            break

        trial_keys = [k for k in user_keys if "trial" in k["key_email"].lower()]
        paid_keys = [k for k in user_keys if "trial" not in k["key_email"].lower()]
        should_notify = False

        for t_key in trial_keys:
            if processed_count >= MAX_EXPIRED_KEYS_PER_CYCLE:
                break
            t_expiry = datetime.fromisoformat(t_key["expiry_date"])
            if t_expiry < now - EXPIRED_KEY_DELETION_GRACE:
                processed_count += 1
                if await _delete_key_everywhere(api, all_inbounds, host_name, t_key):
                    deleted_any = True
                    if not paid_keys:
                        should_notify = True

        for p_key in paid_keys:
            if processed_count >= MAX_EXPIRED_KEYS_PER_CYCLE:
                break
            p_expiry = datetime.fromisoformat(p_key["expiry_date"])
            if p_expiry < now - EXPIRED_KEY_DELETION_GRACE:
                processed_count += 1
                if await _delete_key_everywhere(api, all_inbounds, host_name, p_key):
                    deleted_any = True
                    should_notify = True

        if should_notify:
            try:
                await bot.send_message(
                    chat_id=int(tg_id),
                    text="⚠️ <b>Ваша подписка истекла более 5 дней назад</b>\n\n"
                    "Будем рады вашему возвращению 🙂",
                    parse_mode="HTML",
                )
            except Exception as notify_err:
                logger.warning(f"Не удалось уведомить {tg_id} о просрочке: {notify_err}")

    if deleted_any:
        return await xui_api.run_in_xui_pool(api.inbound.get_list)
    return all_inbounds


async def _sweep_orphaned_clients_for_host(api, host_name: str) -> int:
    """Полная сверка панели с БД по всем клиентам хоста (не только по тем,
    у кого ещё жива строка в vpn_keys).

    Дополняет _delete_key_everywhere / гарбич-цикл в _sync_clones_for_host:
    та логика заводится от строки в БД и лечит рассинхрон, пока строка ещё
    существует. Если по любой причине строка уже удалена (в т.ч. до фикса
    _delete_key_everywhere, гарантировавшего атомарность "удалили с панели
    → удаляем из БД"), клиент на панели остаётся сиротой навсегда — ни один
    будущий цикл шедулера этот email больше не рассматривает, потому что
    просто не знает о его существовании. Этот проход не зависит от истории
    и не зависит от строк в БД вообще — сравнивает напрямую "что реально на
    панели" с "что реально в БД сейчас".

    Специально не трогает клиентов не на нашем EMAIL_DOMAIN — на панели
    вполне может быть что-то не из этого бота, это не наша забота.
    Специально не трогает email'ы из xui_api.emails_in_transition — иначе
    рискуем удалить клиента, которого прямо сейчас создаёт параллельная
    покупка/продление (та же гонка, из-за которой чинили Duplicate email).

    Тяжёлая операция (полный обход клиентов всех инбаундов), поэтому
    вызывается редко — см. ORPHAN_SWEEP_INTERVAL_SECONDS.
    """
    try:
        all_inbounds = await xui_api.run_in_xui_pool(api.inbound.get_list)
    except Exception as e:
        logger.error(f"[{host_name}] Сверка сирот: не удалось получить список инбаундов: {e}")
        return 0

    all_keys = await asyncio.to_thread(database.get_keys_for_host, host_name)

    # Легитимные базовые username'ы: в БД хранится только email мастер-ключа,
    # у клонов свой "-N" суффикс (или GRACE_EMAIL_SUFFIX у grace-клиента на
    # GRACE_INBOUND_REMARK), поэтому сравниваем по базовому префиксу без
    # суффикса. Без учёта GRACE_EMAIL_SUFFIX здесь легитимный grace-клиент
    # ошибочно считался бы сиротой и удалялся при каждой сверке.
    suffix_pattern = re.compile(r"-\d+$|" + re.escape(GRACE_EMAIL_SUFFIX) + r"$")
    known_prefixes = set()
    for k in all_keys:
        email = str(k.get("key_email") or "").strip().lower()
        if not email or "@" not in email:
            continue
        prefix = suffix_pattern.sub("", email.split("@")[0])
        known_prefixes.add(prefix)

    domain_suffix = f"@{xui_api.EMAIL_DOMAIN}"
    deleted_count = 0

    for ib in all_inbounds:
        is_hysteria_ib = xui_api.is_hysteria_protocol(getattr(ib, "protocol", ""))
        for c in ib.settings.clients or []:
            email = str(getattr(c, "email", "") or "").strip().lower()
            if not email.endswith(domain_suffix):
                continue
            base_prefix = suffix_pattern.sub("", email.split("@")[0])
            if base_prefix in known_prefixes:
                continue
            if email in xui_api.emails_in_transition:
                continue

            deleted_ok = await xui_api.run_in_xui_pool(
                xui_api.delete_client_robust, api, ib.id, c, None, is_hysteria_ib
            )
            await _panel_pause(1.0)
            if deleted_ok:
                deleted_count += 1
                logger.info(
                    f"[{host_name}] Сверка сирот: удалён клиент-сирота "
                    f"{email} на inbound {ib.id} (нет ни одной записи в БД)"
                )
            else:
                logger.warning(
                    f"[{host_name}] Сверка сирот: не удалось удалить "
                    f"клиента-сироту {email} на inbound {ib.id}"
                )

    if deleted_count:
        logger.info(f"[{host_name}] Сверка сирот завершена, удалено записей: {deleted_count}")
    return deleted_count


async def _restore_missing_master(
    api,
    all_inbounds: list,
    host_name: str,
    db_key: dict,
    master_inbound,
    globally_taken_emails: set,
):
    """
    Восстанавливает мастер-клиента, если он пропал с панели.

    Возвращает восстановленный server_client или None при неудаче.
    """
    client_uuid = db_key["xui_client_uuid"]
    client_email = db_key["key_email"]

    logger.warning(
        f"[{host_name}] Мастер {client_email} (UUID: {client_uuid}) не найден на панели. Восстановление..."
    )

    # Один проход: одновременно находим "сироту" (чтобы сохранить её sub_id)
    # и удаляем все совпадения. Раньше поиск сироты матчил только по c.id,
    # а удаление — по id или email; если у клиента на панели id почему-то
    # разъехался с UUID из БД (реальный риск для Hysteria2 — auth-идентичность
    # не всегда доезжает до .id при разборе), сирота не находилась, и её
    # sub_id молча терялся при восстановлении — старая ссылка подписки
    # переставала совпадать с новой.
    found_orphan = None
    for ib in all_inbounds:
        is_hysteria_ib = xui_api.is_hysteria_protocol(getattr(ib, "protocol", ""))
        for c in ib.settings.clients or []:
            is_match = str(c.id) == client_uuid or c.email == client_email
            if not is_match:
                continue
            if found_orphan is None:
                found_orphan = c
            deleted_ok = await xui_api.run_in_xui_pool(
                xui_api.delete_client_robust, api, ib.id, c, client_uuid, is_hysteria_ib
            )
            await _panel_pause(1.0)
            if not deleted_ok:
                logger.warning(
                    f"[{host_name}] Не удалось удалить мусорный клиент {c.email} (UUID {client_uuid}) на inbound {ib.id}"
                )
            if c.email in globally_taken_emails:
                globally_taken_emails.discard(c.email)

    await _panel_pause(0.5)

    saved_sub_id = getattr(found_orphan, "sub_id", "") if found_orphan else ""

    expiry_ms = int(datetime.fromisoformat(db_key["expiry_date"]).timestamp() * 1000)

    new_master = xui_api.Client(
        id=client_uuid,
        email=client_email,
        enable=True,
        expiry_time=expiry_ms,
        total_gb=0,
        sub_id=saved_sub_id,
        limit_ip=xui_api.CLIENT_LIMIT_IP,
    )

    try:
        if xui_api.is_hysteria_protocol(getattr(master_inbound, "protocol", "")):
            wrapped = xui_api.HysteriaClientWrapper(new_master, client_uuid)
            await xui_api.run_in_xui_pool(api.client.add, master_inbound.id, [wrapped])
        else:
            await xui_api.run_in_xui_pool(api.client.add, master_inbound.id, [new_master])
        logger.info(f"[{host_name}] Мастер {client_email} успешно восстановлен")
        await _panel_pause(0.5)
        globally_taken_emails.add(client_email)
        return new_master
    except Exception as e:
        logger.error(f"[{host_name}] Ошибка восстановления мастера {client_email}: {e}")
        return None


async def _compute_chronological_target_email(
    base_prefix: str, this_key_id: int, this_created_date, domain: str
) -> str:
    """
    Определяет "правильный" email для ключа не подбором первого свободного слота, а по хронологии: среди всех ключей (на любых хостах) с тем же базовым слагом (base_prefix / base_prefix_2 / base_prefix_3 / ...) самый старый по created_date получает базовое имя без суффикса, второй по старшинству — _2, третий — _3 и т.д.

    Это детерминированная и предсказуемая нумерация (в отличие от "первого
    свободного слота на момент проверки", которая зависит от порядка
    обработки и может давать разный результат в зависимости от того, в каком
    проходе шедулера какой ключ обработался первым).
    """
    siblings = await asyncio.to_thread(database.get_keys_by_base_prefix, base_prefix)
    if not any(s["key_id"] == this_key_id for s in siblings):
        siblings = siblings + [{"key_id": this_key_id, "created_date": this_created_date}]
    siblings = sorted(siblings, key=lambda s: str(s.get("created_date") or ""))
    rank = next(
        (i for i, s in enumerate(siblings, start=1) if s["key_id"] == this_key_id),
        len(siblings),
    )
    suffix = "" if rank == 1 else f"_{rank}"
    return f"{base_prefix}{suffix}@{domain}".lower()


async def _sync_clones_for_host(
    bot: Bot,
    host_name: str,
    api,
    master_inbound,
    all_inbounds: list,
    keys_in_db: list[dict],
    banned_map: dict[int, bool],
    now: datetime,
) -> tuple[int, int]:
    """
    Полная сверка мастер-ключей и их клонов на всех инбаундах хоста.

    Grace-доступ к Telegram (GRACE_INBOUND_REMARK) сюда не входит — см.
    отдельную _sync_grace_inbound_for_host, которая вызывается на каждом
    проходе шедулера независимо от того, дошла ли очередь до этой функции.

    Создание новых клонов не выполняется по одному api.client.add()
    на каждого клиента, а батчится — один api.client.add() со списком клиентов
    на каждый inbound. Каждый такой вызов на панели X-UI приводит к
    пересборке конфига и рестарту xray-core — батчинг напрямую
    сокращает число таких рестартов в N раз (N = число новых клонов на
    инбаунде за проход) вместо одного рестарта на каждого клиента.

    Возвращает total_affected_records, total_clones_created.
    """
    target_inbounds = [ib for ib in all_inbounds if ("🌍" in ib.remark or "🏳️" in ib.remark)]

    # -------------------------------------------------------------------------
    # Кэш реального auth Hysteria-клиентов по инбаунду (email -> auth),
    # заполняется лениво через сырой JSON панели (см. docstring
    # xui_api.fetch_raw_client_auth_map — модель Client из py3xui молча
    # роняет поле auth при парсинге). Один GET на инбаунд за весь проход
    # синхронизации хоста, а не по одному на каждого клиента — иначе на
    # хосте с сотнями Hysteria-клонов это была бы лавина лишних запросов
    # -------------------------------------------------------------------------
    hysteria_auth_cache: dict[int, dict[str, str]] = {}

    def get_hysteria_auth_map(ib_id: int) -> dict[str, str]:
        if ib_id not in hysteria_auth_cache:
            hysteria_auth_cache[ib_id] = xui_api.fetch_raw_client_auth_map(api, ib_id)
        return hysteria_auth_cache[ib_id]

    def resolve_hysteria_update(email_lower: str, ib_id: int, client_uuid: str) -> tuple[str, str]:
        """Возвращает (identifier_for_url, new_auth_for_payload).

        Для поиска существующего клиента панель на бэкенде сверяет URL-параметр
        именно с текущим auth (см. UpdateInboundClient в X-UI) — если передать
        свой client_uuid напрямую, панель никогда не найдёт клиента и ответит
        "empty client ID". Поэтому для поиска используем реальный текущий auth,
        а сам auth в payload нормализуем на client_uuid — единообразно с vless,
        с этого момента поиск и обновление того же клиента уже не потребуют
        обращения к сырому JSON.
        """
        current_auth = get_hysteria_auth_map(ib_id).get(email_lower)
        if current_auth:
            return current_auth, client_uuid
        return client_uuid, client_uuid

    # -------------------------------------------------------------------------
    # Канонический номер клона = порядковый номер инбаунда среди всех
    # помеченных (🌍/🏳️), включая мастер-инбаунд, который всегда занимает
    # позицию 1 (у него самого суффикса -N нет, но позицию он "забирает").
    # Поэтому нумерация клонов начинается с 2, а не с 1 — первый после
    # мастера non-master-инбаунд получает -2, второй -3 и т.д.
    #
    # Раньше номер подбирался как "первый свободный слот на всей панели",
    # что было не связано с реальной позицией инбаунда (например,
    # vol_choke_2-1488@ мог оказаться на инбаунде, который на деле второй).
    # Сортировка по id — стабильный порядок, не зависящий от мусора/
    # orphaned-записей на панели
    # -------------------------------------------------------------------------
    tagged_non_master_ordered = sorted(
        (ib for ib in target_inbounds if ib.id != master_inbound.id),
        key=lambda ib: ib.id,
    )
    ordinal_by_inbound_id = {ib.id: idx + 2 for idx, ib in enumerate(tagged_non_master_ordered)}

    globally_taken_emails = {
        c.email for ib in all_inbounds for c in (ib.settings.clients or []) if c.email
    }

    full_master = await xui_api.run_in_xui_pool(api.inbound.get_by_id, master_inbound.id)
    master_clients_map = {str(c.id): c for c in (full_master.settings.clients or [])}

    total_affected_records = 0
    total_clones_created = 0

    # Глобальная настройка бота не меняется в рамках одного прохода,
    # поэтому читаем один раз здесь.
    try:
        trial_days = int(await asyncio.to_thread(database.get_setting, "trial_duration_days") or 3)
    except (TypeError, ValueError):
        trial_days = 3

    # inbound_id -> [(Client, notify_ctx)] — буфер для батч-создания
    pending_creates: dict[int, list[tuple]] = defaultdict(list)

    for db_key in keys_in_db:
        _check_yield(host_name)
        client_uuid = db_key["xui_client_uuid"]
        client_email = db_key["key_email"]
        tg_id = db_key.get("user_id")

        if db_key["key_id"] in keys_in_transition:
            logger.debug(f"[{host_name}] Пропуск {client_email}: ключ в процессе переноса")
            continue

        if client_email.strip().lower() in xui_api.emails_in_transition:
            logger.debug(f"[{host_name}] Пропуск {client_email}: репликация в процессе")
            continue

        is_banned = banned_map.get(tg_id, False)

        prefix = client_email.split("@")[0]
        if "-" in prefix:
            continue

        server_client = master_clients_map.get(client_uuid)

        if not server_client:
            # Если ключ уже за порогом удаления просроченных (> 5 дней,
            # тот же порог, что в _cleanup_expired_keys_for_host) — не
            # восстанавливаем мастера вообще.
            # Не чинит сам баг (record not found, github.com/MHSanaei/3x-ui/issues/4026),
            # но останавливает бесполезную работу на ключе, который и так
            # обречён на удаление, независимо от того, получится ли
            # когда-нибудь дочистить конкретно эти зависшие записи.
            try:
                key_expiry = datetime.fromisoformat(db_key["expiry_date"])
                already_past_deletion_threshold = key_expiry < now - EXPIRED_KEY_DELETION_GRACE
            except (ValueError, TypeError):
                already_past_deletion_threshold = False

            if already_past_deletion_threshold:
                logger.info(
                    f"[{host_name}] {client_email}: мастер отсутствует на панели, но ключ "
                    f"просрочен более 5 дней — не восстанавливаю, ждём чистку просроченных."
                )
                continue

            server_client = await _restore_missing_master(
                api,
                all_inbounds,
                host_name,
                db_key,
                master_inbound,
                globally_taken_emails,
            )
            if server_client is None:
                continue

        # -------------------------------------------------------------------------
        # Сверка БД <-> реальный email мастера на панели. server_client найден
        # по client_uuid (стабильный идентификатор, не меняется при ренейме),
        # так что server_client.email — это то, что на панели есть прямо
        # сейчас, а client_email (из БД) — то, чем БД это считает. Если они
        # разошлись — БД устарела/неверна (типичная причина: ниже по этому же
        # циклу когда-то был записан db_ok=True после api.client.update(),
        # который панель молча не применила — тихий no-op на identifier,
        # не совпавшем с реальным, тот же класс квирков, что "empty client
        # ID", но без самой ошибки). Без этой проверки такой ключ навсегда
        # выпадает из блока нормализации домена ниже: он смотрит на
        # client_email из БД, а она уже "как бы правильная" — чинить
        # по мнению кода нечего, хотя на панели всё ещё старое значение.
        # Тут же чиним: подтягиваем client_email/prefix к реальности с
        # панели и синхронизируем БД, чтобы дальнейшая обработка этого
        # ключа в этом же проходе шла по актуальным данным.
        # -------------------------------------------------------------------------
        panel_master_email = str(getattr(server_client, "email", "") or "").strip().lower()
        if panel_master_email and panel_master_email != client_email.strip().lower():
            logger.warning(
                f"[{host_name}] Рассинхрон: БД считает email мастера '{client_email}', "
                f"а на панели (по UUID {client_uuid}) реально '{panel_master_email}' — "
                f"синхронизирую БД с фактическим состоянием панели."
            )
            db_ok = await asyncio.to_thread(
                database.update_key_email, db_key["key_id"], panel_master_email
            )
            if db_ok:
                globally_taken_emails.discard(client_email.lower())
                globally_taken_emails.add(panel_master_email)
                client_email = panel_master_email
                db_key["key_email"] = panel_master_email
            else:
                logger.warning(
                    f"[{host_name}] Не удалось синхронизировать БД для ключа "
                    f"{db_key['key_id']} — повтор на следующем проходе."
                )

        # -------------------------------------------------------------------------
        # Снятие trial_ у ключей, которые фактически стали платными, но по
        # какой-то причине не прошли через нормальный путь оплаты в
        # handlers.py (например, ключ продлили до появления этого фикса, или
        # мимо прошла какая-то другая ветка).
        #
        # Проблема: у самого по себе email trial_... нет метки "оплачен/не
        # оплачен" — единственный способ узнать это без отдельного поля в
        # БД — сравнить фактический expiry_date с тем, что мог бы дать
        # чистый триал (created_date + trial_duration_days). Если реальный
        # expiry_date заметно больше, значит, ключу добавляли дни сверх
        # триала, то есть его оплачивали. +12 часов запаса на случай, если
        # реальная выдача триала прошла с небольшим опозданием/округлением.
        #
        # Если сигнала недостаточно (нет created_date/expiry_date, или не
        # распарсились) — ничего не делаем: ложно снять trial_ с живого
        # триала хуже, чем оставить исправление до следующего прохода
        # -------------------------------------------------------------------------
        if prefix.lower().startswith("trial_"):
            created_raw = db_key.get("created_date")
            expiry_raw = db_key.get("expiry_date")
            converted_to_paid = False
            if created_raw and expiry_raw:
                try:
                    created_dt = (
                        created_raw
                        if isinstance(created_raw, datetime)
                        else datetime.fromisoformat(str(created_raw))
                    )
                    expiry_dt = (
                        expiry_raw
                        if isinstance(expiry_raw, datetime)
                        else datetime.fromisoformat(str(expiry_raw))
                    )
                    max_trial_expiry = created_dt + timedelta(days=trial_days, hours=12)
                    converted_to_paid = expiry_dt > max_trial_expiry
                except (ValueError, TypeError) as e:
                    logger.debug(
                        f"[{host_name}] Не удалось разобрать даты для проверки trial->платный ключа {db_key.get('key_id')}: {e}"
                    )

            if converted_to_paid:
                stripped = prefix[len("trial_") :] or f"user{db_key.get('user_id')}"

                # Если у этого же пользователя уже есть отдельный платный
                # (не trial_) ключ с тем же базовым слагом — это не два разных
                # человека, случайно получивших одинаковое имя (для чего и
                # существует нумерация _2/_3 ниже), а дубль одной и той же
                # подписки: юзер получил trial, а позже купил ключ напрямую,
                # мимо конвертации этого же trial-ключа. Переименовывать
                # trial в "_2" в этом случае бессмысленно — на панели уже
                # есть его законный полноценный клон-чейн под чужим (для
                # trial) слотом "_2" никогда не понадобится, а вот клоны
                # trial при попытке слиться с уже занятыми canonical_email
                # платного ключа будут постоянно ловить Duplicate email.
                # Правильное решение — считать trial_ключ избыточным и
                # удалить его целиком (панель + БД), а не переименовывать.
                siblings_same_slug = await asyncio.to_thread(
                    database.get_keys_by_base_prefix, stripped
                )
                redundant_trial = any(
                    s.get("user_id") == tg_id
                    and s["key_id"] != db_key["key_id"]
                    and not str(s.get("key_email", "")).lower().startswith("trial_")
                    for s in siblings_same_slug
                )
                if redundant_trial:
                    logger.warning(
                        f"[{host_name}] {client_email}: у пользователя {tg_id} уже есть "
                        f"отдельный платный ключ с тем же слагом — trial_-ключ избыточен, "
                        f"удаляю его целиком вместо переименования в _2 (иначе клоны будут "
                        f"постоянно ловить Duplicate email на уже занятых платным ключом слотах)."
                    )
                    deleted_ok = await _delete_key_everywhere(api, all_inbounds, host_name, db_key)
                    if deleted_ok:
                        continue
                    # Если удалить не удалось (панель не ответила и т.п.) —
                    # не пытаемся переименовывать в этом же проходе, просто
                    # оставляем как есть до следующего прогона шедулера,
                    # когда _delete_key_everywhere попробует снова.
                    continue

                chronological_target = await _compute_chronological_target_email(
                    stripped,
                    db_key["key_id"],
                    db_key.get("created_date"),
                    xui_api.EMAIL_DOMAIN,
                )
                chronological_local = chronological_target.split("@")[0]
                chronological_rank_match = re.match(
                    rf"^{re.escape(stripped)}(?:_(\d+))?$", chronological_local
                )
                attempt = (
                    int(chronological_rank_match.group(1))
                    if chronological_rank_match and chronological_rank_match.group(1)
                    else 1
                )
                new_local = chronological_local
                new_master_email = chronological_target
                while True:
                    panel_taken = (
                        new_master_email in globally_taken_emails
                        and new_master_email != client_email.lower()
                    )
                    db_conflict = await asyncio.to_thread(
                        database.get_key_by_email, new_master_email
                    )
                    db_taken = bool(db_conflict) and db_conflict.get("key_id") != db_key["key_id"]
                    if not panel_taken and not db_taken:
                        break
                    attempt += 1
                    new_local = f"{stripped}_{attempt}"
                    new_master_email = f"{new_local}@{xui_api.EMAIL_DOMAIN}".lower()
                    if attempt > 100:
                        break

                panel_taken_final = (
                    new_master_email in globally_taken_emails
                    and new_master_email != client_email.lower()
                )
                db_conflict_final = await asyncio.to_thread(
                    database.get_key_by_email, new_master_email
                )
                db_taken_final = (
                    bool(db_conflict_final) and db_conflict_final.get("key_id") != db_key["key_id"]
                )
                if panel_taken_final or db_taken_final:
                    logger.warning(
                        f"[{host_name}] Не удалось снять trial_ с {client_email}: "
                        f"не нашлось свободного email после {attempt} попыток."
                    )
                else:
                    try:
                        server_client.email = new_master_email
                        server_client.id = client_uuid
                        server_client.inbound_id = master_inbound.id

                        if xui_api.is_hysteria_protocol(getattr(master_inbound, "protocol", "")):
                            update_identifier, new_auth = resolve_hysteria_update(
                                client_email.strip().lower(),
                                master_inbound.id,
                                client_uuid,
                            )
                            wrapped_master = xui_api.HysteriaClientWrapper(server_client, new_auth)
                            await xui_api.run_in_xui_pool(
                                api.client.update, update_identifier, wrapped_master
                            )
                        else:
                            await xui_api.run_in_xui_pool(api.client.update, client_uuid, server_client)
                        await _panel_pause(0.5)

                        db_ok = await asyncio.to_thread(
                            database.update_key_email,
                            db_key["key_id"],
                            new_master_email,
                        )
                        if db_ok:
                            globally_taken_emails.discard(client_email.lower())
                            globally_taken_emails.add(new_master_email)
                            logger.info(
                                f"[{host_name}] Снят trial_ (ключ оказался платным): {client_email} -> {new_master_email}"
                            )
                            client_email = new_master_email
                            db_key["key_email"] = new_master_email
                            prefix = new_local
                        else:
                            logger.warning(
                                f"[{host_name}] Email обновлён на панели ({new_master_email}), но не удалось "
                                f"обновить локальную БД для ключа {db_key['key_id']} — рассинхрон, будет повтор."
                            )
                    except Exception as e:
                        logger.warning(
                            f"[{host_name}] Не удалось снять trial_ с {client_email}: {e}"
                        )

        # -------------------------------------------------------------------------
        # Нормализация домена мастер-email: ключи, созданные до смены домена,
        # могли остаться на одном из LEGACY_EMAIL_DOMAINS (например, старом
        # bot.local) — переносим их на актуальный xui_api.EMAIL_DOMAIN прямо
        # здесь, при первом же плановом проходе шедулера, а не ждём, пока
        # владелец ключа сам его продлит. UUID не меняется — все выданные
        # ссылки/конфиги остаются рабочими
        # -------------------------------------------------------------------------
        email_domain = client_email.split("@")[1].lower() if "@" in client_email else ""
        if email_domain in xui_api.LEGACY_EMAIL_DOMAINS:
            # Подбираем свободный вариант ("_2", "_3", ...) — та же схема, что уже
            # используется при создании новых ключей и при снятии trial_. Нужна
            # именно тут, потому что коллизия может быть межхостовой: у одного
            # пользователя есть ключи на нескольких разных хостах, и globally_taken_emails
            # проверяет только панель этого хоста — а UNIQUE на key_email в БД
            # действует глобально по всем хостам сразу. Без этого цикла такая
            # коллизия раньше приводила к бесконечному повтору одной и той же
            # обречённой попытки на каждом проходе шедулера (панель переименовывает
            # заново — лишний рестарт xray, БД падает на UNIQUE constraint заново).
            base_local = prefix
            # Стартуем не с "attempt=1" вслепую, а с позиции, которую этому
            # ключу назначает хронология (created_date) среди всех ключей с
            # тем же базовым слагом на любых хостах — самый старый получает
            # имя без суффикса, дальше по порядку _2, _3... Если стартовая
            # позиция вдруг занята кем-то ещё не готовым уступить (тем, кого
            # этот проход не трогает — см. docstring ниже), перебор продолжает
            # искать дальше от неё, а не с нуля.
            chronological_target = await _compute_chronological_target_email(
                base_local,
                db_key["key_id"],
                db_key.get("created_date"),
                xui_api.EMAIL_DOMAIN,
            )
            chronological_local = chronological_target.split("@")[0]
            chronological_rank_match = re.match(
                rf"^{re.escape(base_local)}(?:_(\d+))?$", chronological_local
            )
            attempt = (
                int(chronological_rank_match.group(1))
                if chronological_rank_match and chronological_rank_match.group(1)
                else 1
            )
            new_local = chronological_local
            new_master_email = chronological_target
            while True:
                panel_taken = (
                    new_master_email in globally_taken_emails
                    and new_master_email != client_email.lower()
                )
                db_conflict = await asyncio.to_thread(database.get_key_by_email, new_master_email)
                db_taken = bool(db_conflict) and db_conflict.get("key_id") != db_key["key_id"]
                if not panel_taken and not db_taken:
                    break
                attempt += 1
                new_local = f"{base_local}_{attempt}"
                new_master_email = f"{new_local}@{xui_api.EMAIL_DOMAIN}".lower()
                if attempt > 50:
                    logger.warning(
                        f"[{host_name}] Не удалось подобрать свободный email для нормализации "
                        f"домена мастера {client_email}: перебрано {attempt} вариантов, все заняты."
                    )
                    new_master_email = None
                    break

            if new_master_email is None:
                pass
            else:
                try:
                    server_client.email = new_master_email
                    server_client.id = client_uuid
                    server_client.inbound_id = master_inbound.id

                    if xui_api.is_hysteria_protocol(getattr(master_inbound, "protocol", "")):
                        update_identifier, new_auth = resolve_hysteria_update(
                            client_email.strip().lower(), master_inbound.id, client_uuid
                        )
                        wrapped_master = xui_api.HysteriaClientWrapper(server_client, new_auth)
                        await xui_api.run_in_xui_pool(
                            api.client.update, update_identifier, wrapped_master
                        )
                    else:
                        await xui_api.run_in_xui_pool(api.client.update, client_uuid, server_client)
                    await _panel_pause(0.5)

                    # Верификация: X-UI на некоторых версиях панели отвечает
                    # success:true даже если clientId/auth в URL не нашёлся —
                    # тихий no-op (тот же класс квирков, что и "empty client
                    # ID", только без самой ошибки). Без этой проверки код
                    # писал новый email в БД, доверяя одному лишь отсутствию
                    # исключения — а на панели мастер оставался под старым
                    # email/доменом бессрочно, потому что чинить уже было
                    # нечему: prefix.split('@')[1] после первого "успеха"
                    # больше не matches ни один LEGACY_EMAIL_DOMAINS, и этот
                    # блок для такого ключа больше никогда не запускается.
                    verify_inbound = await xui_api.run_in_xui_pool(
                        api.inbound.get_by_id, master_inbound.id
                    )
                    verify_clients = (
                        (verify_inbound.settings.clients or []) if verify_inbound else []
                    )
                    panel_actually_updated = any(
                        str(getattr(c, "email", "")).strip().lower() == new_master_email
                        for c in verify_clients
                    )

                    if not panel_actually_updated:
                        logger.warning(
                            f"[{host_name}] Нормализация домена мастера {client_email} -> "
                            f"{new_master_email}: панель ответила без ошибки, но клиент с "
                            f"новым email на inbound {master_inbound.id} не найден (тихий "
                            f"no-op — вероятно, {client_uuid} не совпадает с реальным id/auth "
                            f"на панели). В БД не пишем, чтобы не разойтись с реальностью — "
                            f"нужна ручная сверка UUID для этого ключа."
                        )
                    else:
                        db_ok = await asyncio.to_thread(
                            database.update_key_email, db_key["key_id"], new_master_email
                        )
                        if db_ok:
                            globally_taken_emails.discard(client_email.lower())
                            globally_taken_emails.add(new_master_email)
                            logger.info(
                                f"[{host_name}] Нормализован домен мастера: {client_email} -> {new_master_email}"
                            )
                            client_email = new_master_email
                            db_key["key_email"] = new_master_email
                        else:
                            # Панель подтверждена, а в БД записать не удалось.
                            # Не откатываем панель (это лишний рестарт xray) —
                            # следующий проход шедулера подхватит расхождение и
                            # попробует снова, т.к. read из БД покажет старый email
                            logger.warning(
                                f"[{host_name}] Email мастера обновлён на панели ({new_master_email}), "
                                f"но не удалось обновить локальную БД для ключа {db_key['key_id']} — рассинхрон, "
                                f"будет повторная попытка на следующем проходе."
                            )
                except Exception as e:
                    logger.warning(
                        f"[{host_name}] Не удалось нормализовать домен мастера {client_email}: {e}"
                    )

        reset_days = getattr(server_client, "reset", 0) or 0
        expiry_ms_adjusted = server_client.expiry_time + (reset_days * 86400000)

        await asyncio.to_thread(
            database.update_key_host_and_info,
            key_id=db_key["key_id"],
            new_host_name=host_name,
            new_xui_uuid=client_uuid,
            new_expiry_ms=expiry_ms_adjusted,
        )
        total_affected_records += 1

        master_sub_id = getattr(server_client, "sub_id", "")

        is_expired = datetime.fromisoformat(db_key["expiry_date"]) <= now
        master_enable_current = bool(getattr(server_client, "enable", True))
        desired_master_enable = (not is_banned) and (not is_expired)

        # Grace-доступ к Telegram (GRACE_INBOUND_REMARK) вынесен в отдельную
        # функцию _sync_grace_inbound_for_host — она вызывается из
        # _sync_single_host на каждом проходе шедулера, а не только когда
        # сюда доходит очередь полной сверки (по хэшу/раз в
        # FULL_RECONCILE_INTERVAL_SECONDS) — иначе доступ к Telegram мог
        # включаться с задержкой до 30 минут после истечения подписки.

        if master_enable_current != desired_master_enable:
            try:
                server_client.enable = desired_master_enable
                server_client.id = client_uuid
                server_client.inbound_id = master_inbound.id

                if xui_api.is_hysteria_protocol(getattr(master_inbound, "protocol", "")):
                    update_identifier, new_auth = resolve_hysteria_update(
                        client_email.strip().lower(), master_inbound.id, client_uuid
                    )
                    wrapped_master = xui_api.HysteriaClientWrapper(server_client, new_auth)
                    await xui_api.run_in_xui_pool(api.client.update, update_identifier, wrapped_master)
                else:
                    await xui_api.run_in_xui_pool(api.client.update, client_uuid, server_client)
                await _panel_pause(0.5)
                action = "отключён (is_banned)" if is_banned else "включён (снятие бана)"
                logger.info(f"[{host_name}] Мастер {client_email} {action}")
            except Exception as e:
                logger.error(
                    f"[{host_name}] Ошибка изменения enable для мастера {client_email}: {e}"
                )
                server_client.enable = master_enable_current

        # deferred_renames — сюда откладываются переименования, упавшие
        # именно из-за Duplicate email (целевой слот ещё занят соседним
        # звеном той же цепочки, которое обработается позже в этом же
        # проходе — см. комментарий про порядок обхода выше). Единственный
        # фиксированный порядок обхода (по убыванию id) схлопывает цепочку
        # за один проход только при сдвиге номеров вверх (вставили инбаунд
        # в середину диапазона). При сдвиге вниз (убрали/сняли тег с
        # инбаунда в середине) тот же порядок — наихудший: каждое звено
        # упирается в ещё не освобождённый слот соседа с меньшим id.
        # Ретрай-проход ниже подбирает именно такие случаи, когда все
        # остальные звенья того же прохода уже успели переехать.
        deferred_renames: list[dict] = []

        # Обходим в порядке убывания id (а не как они пришли с панели). Это
        # принципиально для самоисправления номеров клонов: если у всей
        # цепочки клонов одного ключа нужно сдвинуть номер на +1 (например,
        # после смены конвенции нумерации), то переименование должно
        # начинаться с самого высокого номера — его целевой слот гарантированно
        # свободен (выше него никогда ничего не было), и после его переезда
        # освобождается слот для следующего по старшинству, и так по цепочке.
        # При проходе по возрастанию каждый шаг упирается в занятый следующим
        # звеном слот, и цепочка чинится только на одно звено за прогон
        # шедулера — при обратном порядке она схлопывается за один проход.
        for ib in sorted(target_inbounds, key=lambda x: -x.id):
            clients_in_this_ib = ib.settings.clients or []
            is_hysteria_ib = xui_api.is_hysteria_protocol(getattr(ib, "protocol", ""))

            correct_clone = None
            for c in clients_in_this_ib:
                c_email_check = str(getattr(c, "email", "")).strip().lower()
                if (
                    c_email_check.startswith(f"{prefix}-")
                    and getattr(c, "sub_id", "") == master_sub_id
                ):
                    c_ident = (
                        str(getattr(c, "auth", getattr(c, "password", getattr(c, "id", ""))))
                        .strip()
                        .lower()
                    )
                    if c_ident == client_uuid or c_ident == "":
                        correct_clone = c
                        break

            to_delete = []
            for c in clients_in_this_ib:
                if c.email.startswith("admin@") or "@" not in c.email:
                    continue
                if str(c.id) == client_uuid or c.email.startswith(f"{prefix}-"):
                    if ib.id == master_inbound.id and str(c.id) == client_uuid:
                        continue
                    if c is not correct_clone:
                        to_delete.append(c)

            for c in to_delete:
                deleted_ok = await xui_api.run_in_xui_pool(
                    xui_api.delete_client_robust, api, ib.id, c, client_uuid, is_hysteria_ib
                )
                await _panel_pause(1.0)
                if deleted_ok:
                    if c.email in globally_taken_emails:
                        globally_taken_emails.discard(c.email)
                else:
                    logger.warning(f"[{host_name}] Не удалось удалить мусорный клиент {c.email}")

            if ib.id == master_inbound.id:
                continue

            if correct_clone is not None:
                canonical_ordinal = ordinal_by_inbound_id.get(ib.id)
                canonical_email = (
                    f"{prefix}-{canonical_ordinal}@{xui_api.EMAIL_DOMAIN}"
                    if canonical_ordinal
                    else None
                )
                current_clone_email = str(getattr(correct_clone, "email", "")).strip().lower()

                # Самоисправление номера: если у клона сейчас "мусорный" номер
                # (например, достался при подборе первого свободного слота в
                # прошлых запусках), а канонический слот свободен или уже наш —
                # чиним прямо тут, попутно с обычным обновлением enable/expiry
                needs_email_fix = (
                    canonical_email is not None
                    and current_clone_email != canonical_email
                    and (
                        canonical_email not in globally_taken_emails
                        or canonical_email == current_clone_email
                    )
                )

                desired_clone_enable = (not is_banned) and desired_master_enable
                clone_enable_current = bool(getattr(correct_clone, "enable", True))
                clone_expiry_current = getattr(correct_clone, "expiry_time", None)
                clone_total_gb_current = getattr(correct_clone, "total_gb", None)

                needs_update = (
                    clone_enable_current != desired_clone_enable
                    or clone_expiry_current != expiry_ms_adjusted
                    or clone_total_gb_current != server_client.total_gb
                    or needs_email_fix
                )

                if needs_update:
                    try:
                        correct_clone.enable = desired_clone_enable
                        correct_clone.expiry_time = expiry_ms_adjusted
                        correct_clone.total_gb = server_client.total_gb
                        correct_clone.id = client_uuid
                        correct_clone.inbound_id = ib.id

                        if needs_email_fix:
                            correct_clone.email = canonical_email

                        clone_is_hysteria = xui_api.is_hysteria_protocol(
                            getattr(ib, "protocol", "")
                        )
                        if clone_is_hysteria:
                            # Ищем клиента на панели по реальному текущему auth
                            # (не по client_uuid — панель сверяет URL-параметр
                            # именно с ним, см. resolve_hysteria_update), а auth
                            # в payload нормализуем на client_uuid — единообразно
                            # с vless, все последующие обновления этого же
                            # клиента дальше пойдут уже без обращения к сырому
                            # JSON, раз auth panel-side станет равен client_uuid
                            update_identifier, new_auth = resolve_hysteria_update(
                                current_clone_email, ib.id, client_uuid
                            )
                            wrapped_update = xui_api.HysteriaClientWrapper(correct_clone, new_auth)
                            await xui_api.run_in_xui_pool(
                                api.client.update, update_identifier, wrapped_update
                            )
                        else:
                            await xui_api.run_in_xui_pool(api.client.update, client_uuid, correct_clone)
                        await _panel_pause(0.5)

                        # Bookkeeping мутируем только после подтверждённого успеха
                        # API-вызова (строки выше не бросили исключение) — если
                        # мутировать до вызова и он упадёт, globally_taken_emails
                        # навсегда разойдётся с реальным состоянием панели: старый
                        # слот будет ошибочно считаться свободным, а новый — занятым,
                        # хотя на панели по факту ничего не изменилось. Именно это
                        # раньше вызывало каскад "Duplicate email" на следующих
                        # клонах той же цепочки.
                        if needs_email_fix:
                            globally_taken_emails.discard(current_clone_email)
                            globally_taken_emails.add(canonical_email)
                            logger.info(
                                f"[{host_name}] Нормализован номер клона: "
                                f"{current_clone_email} -> {canonical_email}"
                            )
                        logger.info(
                            f"[{host_name}] Клон {correct_clone.email} синхронизирован "
                            f"(enable={desired_clone_enable}, expiry обновлён)"
                        )
                    except Exception as e:
                        # Откатываем локальные мутации объекта, чтобы при следующей
                        # итерации/повторной попытке не унести в панель "email,
                        # который мы сами себе придумали, но так и не подтвердили"
                        if needs_email_fix:
                            correct_clone.email = current_clone_email
                        if needs_email_fix and "Duplicate email" in str(e):
                            # Целевой слот ещё занят соседним звеном той же
                            # цепочки, которое обработается позже в этом же
                            # порядке обхода — не финальная ошибка, откладываем
                            # на ретрай-проход после основного цикла ниже.
                            deferred_renames.append(
                                {
                                    "ib": ib,
                                    "canonical_email": canonical_email,
                                    "current_clone_email": current_clone_email,
                                    "desired_clone_enable": desired_clone_enable,
                                }
                            )
                        else:
                            logger.warning(
                                f"[{host_name}] Не удалось обновить клон {correct_clone.email}: {e}"
                            )
                elif (
                    canonical_email is not None
                    and current_clone_email != canonical_email
                    and canonical_email in globally_taken_emails
                ):
                    # Канонический слот сейчас занят — но это может быть
                    # соседнее звено той же цепочки, которое просто ещё не
                    # успело обработаться в этом проходе (см. deferred_renames
                    # выше). Раньше это место сразу печатало warning и на
                    # этом останавливалось насовсем: клон сюда попадает
                    # именно тогда, когда needs_update оказался False (когда
                    # enable/expiry/квота и так уже верны, чинить нужно
                    # только email) — а значит код ни разу не вызывал API и
                    # ни разу не мог поймать исключение "Duplicate email",
                    # единственный способ раньше попасть в deferred_renames.
                    # Итог — клон был обречён вечно висеть с чужим номером и
                    # печатать этот warning на каждом прогоне шедулера, даже
                    # если сосед по цепочке освобождает его слот через
                    # мгновение в этом же проходе. Теперь ставим такой клон
                    # в ту же очередь ретраев вместо немедленного warning —
                    # если слот освободится позже в этом проходе, клон
                    # доедет до канонического номера молча; если нет —
                    # итоговый warning напечатает сам ретрай-блок ниже
                    # (после исчерпания внутренних попыток).
                    deferred_renames.append(
                        {
                            "ib": ib,
                            "canonical_email": canonical_email,
                            "current_clone_email": current_clone_email,
                            "desired_clone_enable": desired_clone_enable,
                        }
                    )

                continue

            # Клона нет — откладываем создание в батч-буфер вместо немедленного add()
            canonical_ordinal = ordinal_by_inbound_id.get(ib.id)
            if canonical_ordinal is None:
                # Инбаунд не найден в списке помеченных не-мастер инбаундов
                # (не должно происходить, т.к. ib взят из target_inbounds) —
                # подстраховка, чтобы не упасть с KeyError
                logger.error(
                    f"[{host_name}] Не удалось определить порядковый номер для инбаунда {ib.id} ({ib.remark})"
                )
                continue

            candidate = f"{prefix}-{canonical_ordinal}@{xui_api.EMAIL_DOMAIN}"
            if candidate in globally_taken_emails:
                # Канонический слот занят чужим клиентом (вероятно,
                # orphaned-запись) — не подбираем произвольный свободный
                # номер, а пропускаем с явным логом,
                # чтобы админ почистил панель руками.
                logger.error(
                    f"[{host_name}] Не удалось создать клон {candidate}: "
                    f"канонический email уже занят кем-то другим на панели (orphaned-запись?)."
                )
                continue

            new_clone = xui_api.Client(
                id=client_uuid,
                email=candidate,
                enable=(not is_banned) and desired_master_enable,
                expiry_time=expiry_ms_adjusted,
                total_gb=server_client.total_gb,
                sub_id=master_sub_id,
                limit_ip=xui_api.CLIENT_LIMIT_IP,
            )
            # Резервируем слот в bookkeeping сразу (чтобы следующий кандидат в
            # этом же проходе не выбрал тот же email), но помним: это ещё не
            # подтверждено панелью — при провале батча/фолбэка ниже это
            # откатывается явно, а не остаётся вечным враньём в bookkeeping
            globally_taken_emails.add(candidate)

            pending_creates[ib.id].append(
                (
                    new_clone,
                    {
                        "tg_id": tg_id,
                        "is_banned": is_banned,
                        "expiry_date": db_key["expiry_date"],
                        "ib_remark": ib.remark,
                    },
                )
            )

        # -------------------------------------------------------------------------
        # Ретрай-проход по переименованиям, отложенным из-за Duplicate email.
        # К этому моменту все остальные звенья цепочки этого ключа в этом же
        # проходе уже обработаны (в порядке убывания id), так что часть
        # целевых слотов, занятых на момент первой попытки, уже должна была
        # освободиться. Один дополнительный проход по уже небольшому списку
        # (обычно один-два элемента — ровно тот отрезок цепочки, что сдвинулся
        # в "неудобную" для порядка обхода сторону), без ограничения по числу
        # итераций — тут не может быть больше проходов, чем звеньев в
        # deferred_renames, так что зацикливания не бывает.
        # -------------------------------------------------------------------------
        remaining = deferred_renames
        # С запасом: разрыв цикла временным именем добавляет проходы
        for _ in range(len(deferred_renames) * 2 + 2):
            if not remaining:
                break
            still_pending = []
            for item in remaining:
                canonical_email = item["canonical_email"]
                current_clone_email = item["current_clone_email"]
                if canonical_email in globally_taken_emails:
                    # Соседнее звено ещё не освободило слот — попробуем на
                    # следующей внутренней итерации
                    still_pending.append(item)
                    continue
                ib = item["ib"]
                is_hysteria_retry = xui_api.is_hysteria_protocol(getattr(ib, "protocol", ""))
                clients_now = ib.settings.clients or []
                retry_clone = next(
                    (
                        c
                        for c in clients_now
                        if str(getattr(c, "email", "")).strip().lower() == current_clone_email
                    ),
                    None,
                )
                if retry_clone is None:
                    logger.warning(
                        f"[{host_name}] Ретрай переименования {current_clone_email} -> "
                        f"{canonical_email}: клиент пропал с панели, пропуск."
                    )
                    continue
                try:
                    retry_clone.enable = item["desired_clone_enable"]
                    retry_clone.id = client_uuid
                    retry_clone.inbound_id = ib.id
                    retry_clone.email = canonical_email
                    if is_hysteria_retry:
                        update_identifier, new_auth = resolve_hysteria_update(
                            current_clone_email, ib.id, client_uuid
                        )
                        wrapped_retry = xui_api.HysteriaClientWrapper(retry_clone, new_auth)
                        await xui_api.run_in_xui_pool(api.client.update, update_identifier, wrapped_retry)
                    else:
                        await xui_api.run_in_xui_pool(api.client.update, client_uuid, retry_clone)
                    await _panel_pause(0.5)
                    globally_taken_emails.discard(current_clone_email)
                    globally_taken_emails.add(canonical_email)
                    logger.info(
                        f"[{host_name}] Нормализован номер клона (ретрай): "
                        f"{current_clone_email} -> {canonical_email}"
                    )
                except Exception as e:
                    logger.warning(
                        f"[{host_name}] Ретрай переименования {current_clone_email} -> "
                        f"{canonical_email} не удался: {e}"
                    )
            if len(still_pending) == len(remaining):
                # Ни один элемент не продвинулся. Если причина — замкнутый цикл
                # номеров (A->B, B->C, C->A), разрываем его временным именем:
                # переименовываем одного участника в «<prefix>-tmp<id инбаунда>»,
                # его старый номер освобождается, а сам участник затем получает
                # канонический номер. Если процесс упадёт посередине, временное
                # имя подхватится следующим прогоном шедулера
		        # (клон ищется по префиксу и sub_id) и починится само.
                # Отключается через XUI_BREAK_RENAME_CYCLES=0.
                victim = (
                    _find_rename_cycle_member(still_pending)
                    if still_pending
                    and os.getenv("XUI_BREAK_RENAME_CYCLES", "1").strip() != "0"
                    else None
                )
                if victim is not None:
                    v_ib = victim["ib"]
                    v_cur = victim["current_clone_email"]
                    temp_email = f"{prefix}-tmp{getattr(v_ib, 'id', 0)}@{xui_api.EMAIL_DOMAIN}"
                    v_clone = next(
                        (
                            c
                            for c in (v_ib.settings.clients or [])
                            if str(getattr(c, "email", "")).strip().lower() == v_cur
                        ),
                        None,
                    )
                    if v_clone is not None and temp_email not in globally_taken_emails:
                        try:
                            v_clone.enable = victim["desired_clone_enable"]
                            v_clone.id = client_uuid
                            v_clone.inbound_id = v_ib.id
                            v_clone.email = temp_email
                            if xui_api.is_hysteria_protocol(getattr(v_ib, "protocol", "")):
                                upd_id, new_auth = resolve_hysteria_update(
                                    v_cur, v_ib.id, client_uuid
                                )
                                await xui_api.run_in_xui_pool(
                                    api.client.update,
                                    upd_id,
                                    xui_api.HysteriaClientWrapper(v_clone, new_auth),
                                )
                            else:
                                await xui_api.run_in_xui_pool(
                                    api.client.update, client_uuid, v_clone
                                )
                            await _panel_pause(0.5)
                            globally_taken_emails.discard(v_cur)
                            globally_taken_emails.add(temp_email)
                            victim["current_clone_email"] = temp_email
                            logger.info(
                                f"[{host_name}] Разрыв цикла номеров клонов: {v_cur} -> "
                                f"{temp_email} (временно, затем -> {victim['canonical_email']})"
                            )
                            remaining = still_pending
                            continue
                        except Exception as e:
                            v_clone.email = v_cur
                            logger.warning(
                                f"[{host_name}] Не удалось разорвать цикл номеров через "
                                f"{temp_email}: {e}"
                            )
                # Дальше по кругу ходить бессмысленно, останется до
                # следующего прогона шедулера
                if still_pending:
                    # Диагностика: что именно застряло. Если целевые номера занимают
                    # сами застрявшие клоны (цепочка замкнулась в цикл — «обмен»
                    # номерами), без временного имени она не разрешится никогда;
                    # если же целевой номер занят кем-то вне списка — это посторонний
                    # клиент на панели с таким же email.
                    pending_current = {i["current_clone_email"] for i in still_pending}
                    details = []
                    for i in still_pending[:10]:
                        holder = (
                            "занят другим застрявшим клоном (цикл)"
                            if i["canonical_email"] in pending_current
                            else "занят посторонним клиентом"
                        )
                        details.append(
                            f"{i['current_clone_email']} -> {i['canonical_email']} "
                            f"(инбаунд {getattr(i['ib'], 'id', '?')}, {holder})"
                        )
                    logger.warning(
                        f"[{host_name}] {len(still_pending)} переименований клонов так и "
                        f"не удалось разрешить в этом проходе — до следующего прогона шедулера. "
                        f"Застряли: " + "; ".join(details)
                    )
                break
            remaining = still_pending

    # -------------------------------------------------------------------------
    # Батч-создание накопленных клонов: один api.client.add() на inbound
    # -------------------------------------------------------------------------
    # pending_new_servers_by_user — сюда копятся названия инбаундов по
    # каждому юзеру за весь проход (по всем inbound'ам разом), а не
    # отправляются сразу внутри цикла. Раньше уведомление шло по одному
    # сообщению на каждый inbound — если у юзера в одном проходе создавались
    # клоны сразу на десятках инбаундов (например, восстановление пропавшего
    # мастера — там пересоздаётся вся цепочка целиком), он получал
    # соответствующее число отдельных сообщений подряд. Теперь — одно
    # сообщение на юзера в конце, со списком всех новых серверов разом.
    pending_new_servers_by_user: dict[int, list[str]] = defaultdict(list)

    for ib_id, items in pending_creates.items():
        ib_obj = next((i for i in target_inbounds if i.id == ib_id), None)
        is_hysteria_ib = (
            xui_api.is_hysteria_protocol(getattr(ib_obj, "protocol", "")) if ib_obj else False
        )

        clients_payload = []
        for client_obj, _ctx in items:
            if is_hysteria_ib:
                clients_payload.append(xui_api.HysteriaClientWrapper(client_obj, client_obj.id))
            else:
                clients_payload.append(client_obj)

        try:
            await xui_api.run_in_xui_pool(api.client.add, ib_id, clients_payload)
            await _panel_pause(0.5)
            total_clones_created += len(items)
            emails_created = ", ".join(c.email for c, _ in items)
            logger.info(
                f"[{host_name}] Батчем создано {len(items)} клон(ов) на inbound "
                f"{ib_id} ({ib_obj.remark if ib_obj else ib_id}): {emails_created}"
            )

            for client_obj, ctx in items:
                expiry_date = datetime.fromisoformat(ctx["expiry_date"])
                if ctx["tg_id"] and expiry_date > now and not ctx["is_banned"]:
                    pending_new_servers_by_user[int(ctx["tg_id"])].append(ctx["ib_remark"])

        except Exception as e:
            if "Duplicate email" in str(e):
                # Батч не прошёл целиком из-за коллизии email — фолбэк поштучно.
                #
                # Коллизия здесь может значить одно из двух:
                #  1) реальная orphaned-запись на панели, о которой наш снэпшот
                #     all_inbounds (снятый в начале всего прохода по хосту, а
                #     не только этого inbound'а) не знал заранее;
                #  2) гонка с параллельной репликацией того же ключа
                #     (create_or_update_key_on_host), стартовавшей уже после
                #     того как мы сняли этот снэпшот и прошли проверку
                #     emails_in_transition в начале функции — тогда клиент
                #     абсолютно легитимен, его создал другой процесс, и
                #     удалять его нельзя.
                #
                # Раньше здесь искали, кого удалить, по тому же устаревшему
                # all_inbounds — если клиента там не было (а раз он попал в
                # pending_creates, значит не было), фолбэк ничего не находил,
                # ничего не удалял, и повторный add() падал с тем же
                # Duplicate email на 100% кандидатов. Теперь: сначала быстрая
                # проверка гонки, затем — только живой (не кэшированный)
                # снимок инбаунда, никогда не all_inbounds.
                logger.warning(
                    f"[{host_name}] Коллизия email при батч-создании на inbound {ib_id}, разбираю поштучно"
                )
                for client_obj, ctx in items:
                    email_lower = str(getattr(client_obj, "email", "") or "").strip().lower()
                    try:
                        if email_lower in xui_api.emails_in_transition:
                            # Гонка с параллельной репликацией этого же ключа —
                            # запись там законная, руки прочь. Досоздастся сама,
                            # либо уже создана — в любом случае трогать нельзя.
                            logger.info(
                                f"[{host_name}] {client_obj.email}: коллизия из-за "
                                f"параллельной репликации этого ключа, пропуск "
                                f"(уже обрабатывается другим процессом)."
                            )
                            globally_taken_emails.add(client_obj.email)
                            continue

                        ib_obj_live = await xui_api.run_in_xui_pool(api.inbound.get_by_id, ib_id)
                        is_hysteria_check = xui_api.is_hysteria_protocol(
                            getattr(ib_obj_live, "protocol", "") if ib_obj_live else ""
                        )
                        live_clients = (
                            (ib_obj_live.settings.clients or []) if ib_obj_live else []
                        )
                        match = next(
                            (
                                c
                                for c in live_clients
                                if str(getattr(c, "email", "")).strip().lower() == email_lower
                            ),
                            None,
                        )

                        if match is None:
                            # Панель ответила Duplicate email, но в живом
                            # (не кэшированном) списке клиентов инбаунда его
                            # нет — рассинхрон/гонка, а не мусор. Удалять
                            # нечего, безопасно пропускаем — досоздастся на
                            # следующем тике шедулера, когда состояние
                            # устаканится.
                            logger.warning(
                                f"[{host_name}] {client_obj.email}: панель сообщила "
                                f"Duplicate email, но в живом списке клиентов "
                                f"inbound {ib_id} его нет — вероятно гонка, "
                                f"пропуск без удаления."
                            )
                            globally_taken_emails.discard(client_obj.email)
                            continue

                        deleted_ok = await xui_api.run_in_xui_pool(
                            xui_api.delete_client_robust,
                            api,
                            ib_id,
                            match,
                            None,
                            is_hysteria_check,
                        )
                        if not deleted_ok:
                            # Возврат delete_client_robust теперь проверяется:
                            # раньше код молча продолжал на pretend-success и
                            # тут же ловил тот же Duplicate email повторно.
                            logger.error(
                                f"[{host_name}] Не удалось удалить мусорную запись "
                                f"{client_obj.email} на inbound {ib_id}, создание "
                                f"клона отменено на этом тике."
                            )
                            globally_taken_emails.discard(client_obj.email)
                            continue

                        await _panel_pause(0.5)
                        single_payload = (
                            xui_api.HysteriaClientWrapper(client_obj, client_obj.id)
                            if is_hysteria_ib
                            else client_obj
                        )
                        await xui_api.run_in_xui_pool(api.client.add, ib_id, [single_payload])
                        await _panel_pause(0.5)
                        total_clones_created += 1
                    except Exception as inner_e:
                        # Не удалось создать даже поштучно — слот на самом деле
                        # свободен (или недостижим), откатываем bookkeeping,
                        # иначе он навсегда "занят" и следующий кандидат никогда
                        # не попробует этот email снова
                        globally_taken_emails.discard(client_obj.email)
                        logger.error(
                            f"[{host_name}] Ошибка клонирования {client_obj.email}: {inner_e}"
                        )
            else:
                # Батч провалился целиком по другой причине — откатываем
                # bookkeeping для всех кандидатов этого батча, ни один из них
                # не был реально создан на панели
                for client_obj, _ctx in items:
                    globally_taken_emails.discard(client_obj.email)
                logger.error(f"[{host_name}] Ошибка батч-создания клонов на inbound {ib_id}: {e}")

    # -------------------------------------------------------------------------
    # Рассылка накопленных уведомлений — одно сообщение на юзера со списком
    # всех новых серверов за этот проход, вместо сообщения на каждый inbound
    # -------------------------------------------------------------------------
    for user_tg_id, server_remarks in pending_new_servers_by_user.items():
        if not server_remarks:
            continue
        try:
            if len(server_remarks) == 1:
                text = (
                    f"🆕 <b>В вашей подписке появилось новое подключение</b>\n\n"
                    f"▪️{server_remarks[0]}\n\n"
                    f"Пожалуйста, обновите подписку в своём клиенте 🔄"
                )
            else:
                servers_list = "\n".join(f"▪️{r}" for r in server_remarks)
                text = (
                    f"🆕 <b>В вашей подписке появились новые подключения</b>\n\n"
                    f"{servers_list}\n\n"
                    f"Пожалуйста, обновите подписку в своём клиенте 🔄"
                )
            await bot.send_message(chat_id=user_tg_id, text=text, parse_mode="HTML")
            await asyncio.sleep(0.05)
        except Exception as e:
            logger.error(f"Ошибка отправки уведомления {user_tg_id}: {e}")

    return total_affected_records, total_clones_created


async def _sync_grace_inbound_for_host(
    bot: Bot,
    host_name: str,
    api,
    all_inbounds: list,
    keys_in_db: list[dict],
    banned_map: dict[int, bool],
    now: datetime,
) -> None:
    """
    Grace-доступ к Telegram: пока ключ уже истёк, но ещё не достиг
    EXPIRED_KEY_DELETION_GRACE (порога физического удаления), реплицирует
    клиента на GRACE_INBOUND_REMARK и включает его. Если пользователь
    оплатил (is_expired снова стал False) — не удаляет клиента, а просто
    выключает (enable=False): дешевле для панели (без лишнего рестарта
    Xray на add/delete) и симметрично с тем, как уже устроено
    включение/выключение мастера в _sync_clones_for_host. Физическое
    удаление, когда до него дойдёт очередь, всё равно снесёт и этот
    клиент — email начинается с того же prefix-, а _delete_key_everywhere
    матчит по этому признаку по всем инбаундам хоста, не только по
    тегированным.

    Специально вынесена из _sync_clones_for_host в самостоятельную функцию,
    вызываемую из _sync_single_host на каждом проходе шедулера безусловно
    (не по хэшу состояния и не раз в FULL_RECONCILE_INTERVAL_SECONDS,
    как полная сверка клонов) — раньше включение доступа к Telegram могло
    задерживаться до 30 минут после истечения подписки, если на хосте в
    этот момент не происходило больше никакой другой активности, меняющей
    хэш. Здесь читается и обновляется только db_key напрямую (uuid/email из
    БД), без похода к мастер-инбаунду и без его восстановления — поэтому
    может safely жить в лёгком, часто вызываемом цикле, не таща на себе
    вообще всю тяжёлую логику ordinal-нумерации клонов.
    """
    grace_inbound = next((ib for ib in all_inbounds if ib.remark == GRACE_INBOUND_REMARK), None)
    if grace_inbound is None:
        return

    grace_is_hysteria = xui_api.is_hysteria_protocol(getattr(grace_inbound, "protocol", ""))
    grace_clients = grace_inbound.settings.clients if grace_inbound.settings else []

    for db_key in keys_in_db:
        _check_yield(host_name)
        if db_key.get("key_id") in keys_in_transition:
            continue

        client_email = str(db_key.get("key_email") or "").strip().lower()
        client_uuid = str(db_key.get("xui_client_uuid") or "").strip()
        if not client_email or "@" not in client_email or not client_uuid:
            continue
        if client_email in xui_api.emails_in_transition:
            continue

        try:
            expiry_dt = datetime.fromisoformat(db_key["expiry_date"])
        except (KeyError, ValueError, TypeError):
            continue

        prefix = client_email.split("@")[0]
        is_banned = bool(banned_map.get(db_key.get("user_id")))
        is_expired = expiry_dt <= now
        already_past_deletion_threshold = expiry_dt < now - EXPIRED_KEY_DELETION_GRACE
        desired_grace_enable = is_expired and not already_past_deletion_threshold and not is_banned

        grace_email = f"{prefix}{GRACE_EMAIL_SUFFIX}@{xui_api.EMAIL_DOMAIN}"
        grace_client = next(
            (
                c
                for c in grace_clients
                if str(getattr(c, "id", "")) == client_uuid
                or str(getattr(c, "email", "")).strip().lower() == grace_email
            ),
            None,
        )

        if grace_client is None:
            if not desired_grace_enable:
                continue  # создавать выключенным незачем

            # sub_id — общий с мастером/клонами, чтобы grace-сервер появился
            # в уже существующей у пользователя ссылке подписки сам, без
            # ручного переимпорта. Ищем среди уже загруженных all_inbounds
            # (без похода к панели за отдельным запросом) — по любому
            # клиенту с тем же UUID, где бы он ни нашёлся.
            existing_sub_id = None
            for ib in all_inbounds:
                for c in (ib.settings.clients if ib.settings else []) or []:
                    if str(getattr(c, "id", "")) == client_uuid:
                        sid = getattr(c, "sub_id", None)
                        if sid:
                            existing_sub_id = sid
                            break
                if existing_sub_id:
                    break

            new_grace_client = xui_api.Client(
                id=client_uuid,
                email=grace_email,
                enable=True,
                expiry_time=0,  # состоянием управляет сам шедулер (enable), не таймер панели
                total_gb=GRACE_INBOUND_TOTAL_GB_BYTES,
                sub_id=existing_sub_id or secrets.token_hex(12),
            )
            try:
                payload = (
                    [xui_api.HysteriaClientWrapper(new_grace_client, client_uuid)]
                    if grace_is_hysteria
                    else [new_grace_client]
                )
                await xui_api.run_in_xui_pool(
                    xui_api.add_client_robust, api, grace_inbound.id, payload, grace_email
                )
                await _panel_pause(0.5)
                logger.info(
                    f"[{host_name}] {client_email}: подписка истекла — создан grace-клиент "
                    f"на '{GRACE_INBOUND_REMARK}' (доступ к Telegram, {GRACE_INBOUND_TOTAL_GB_BYTES} байт)"
                )
            except Exception as e:
                logger.warning(f"[{host_name}] Не удалось создать grace-клиента {grace_email}: {e}")
        else:
            grace_enable_current = bool(getattr(grace_client, "enable", True))
            if grace_enable_current != desired_grace_enable:
                try:
                    grace_client.enable = desired_grace_enable
                    grace_client.id = client_uuid
                    grace_client.inbound_id = grace_inbound.id
                    if grace_is_hysteria:
                        update_identifier, new_auth = xui_api._resolve_hysteria_update(
                            api,
                            str(getattr(grace_client, "email", "")).strip().lower(),
                            grace_inbound.id,
                            client_uuid,
                        )
                        wrapped_grace = xui_api.HysteriaClientWrapper(grace_client, new_auth)
                        await xui_api.run_in_xui_pool(api.client.update, update_identifier, wrapped_grace)
                    else:
                        await xui_api.run_in_xui_pool(api.client.update, client_uuid, grace_client)
                    await _panel_pause(0.5)
                    action = (
                        "включён (истекла, окно до удаления)"
                        if desired_grace_enable
                        else "отключён (оплата/бан/скоро удаление)"
                    )
                    logger.info(f"[{host_name}] Grace-клиент {grace_email} {action}")
                except Exception as e:
                    logger.warning(
                        f"[{host_name}] Не удалось изменить enable grace-клиента {grace_email}: {e}"
                    )


async def _refresh_sub_token_cache(host_name: str, master_inbound, keys_in_db: list[dict]) -> None:
    """Сверяет закэшированные в vpn_keys.sub_token с тем, что реально лежит на панели.

    Данные берутся из уже полученного снимка мастер-инбаунда — ни одного
    дополнительного запроса к панели. Заполняет кэш для старых ключей
    (первый проход после деплоя) и лечит расхождения, если subId поменяли
    на панели вручную. Ключи, которые прямо сейчас продлеваются/переносятся,
    пропускаются.
    """
    try:
        clients = (
            master_inbound.settings.clients
            if master_inbound and master_inbound.settings and master_inbound.settings.clients
            else []
        )
        panel_tokens = {
            str(c.id): str(c.sub_id)
            for c in clients
            if getattr(c, "id", None) and getattr(c, "sub_id", None)
        }
        pairs: list[tuple[str, int]] = []
        for k in keys_in_db:
            if k["key_id"] in keys_in_transition:
                continue
            if str(k.get("key_email") or "").strip().lower() in xui_api.emails_in_transition:
                continue
            token = panel_tokens.get(str(k.get("xui_client_uuid")))
            if token and token != (k.get("sub_token") or ""):
                pairs.append((token, k["key_id"]))
        if pairs:
            saved = await asyncio.to_thread(database.set_key_sub_tokens_bulk, pairs)
            logger.info(f"[{host_name}] Кэш sub_token обновлён для ключей: {saved}")
    except Exception as e:
        logger.warning(f"[{host_name}] Не удалось обновить кэш sub_token: {e}")


async def _sync_single_host(
    bot: Bot,
    host: dict,
    now: datetime,
    banned_map: dict[int, bool],
    run_expired_cleanup: bool,
    run_orphan_sweep: bool = False,
) -> tuple | None:
    """
    Полная обработка одного хоста за цикл: один логин + один get_list() (переиспользуются и для чистки, и для сверки клонов), опциональная чистка просроченных ключей, опциональная (по хэшу состояния) полная сверка клонов.

    Возвращает api, all_inbounds для переиспользования в check_traffic_anomalies
    в этом же цикле, либо None при ошибке хоста.
    """
    host_name = host["host_name"]
    logger.debug(f"[{host_name}] Начало обработки хоста")

    try:
        api, master_inbound, all_inbounds = await xui_api.run_in_xui_pool(
            xui_api.login_and_get_inbounds,
            host["host_url"],
            host["host_username"],
            host["host_pass"],
            host["host_inbound_id"],
        )
        if not api or not master_inbound:
            logger.error(f"[{host_name}] Не удалось авторизоваться или найти мастер-inbound")
            return None

        keys_in_db = await asyncio.to_thread(database.get_keys_for_host, host_name)

        # Read-only: подтягиваем subId с панели в БД по уже имеющемуся снимку
        await _refresh_sub_token_cache(host_name, master_inbound, keys_in_db)

        # -------------------------------------------------------------------------
        # Весь мутирующий блок (чистка просроченных + полная сверка клонов) —
        # под тем же per-host asyncio.Lock, что и xui_api.create_or_update_key_on_host.
        # Логин и get_list() выше в лок не берём — это read-only и с ручной
        # покупкой всё равно не конфликтует, а держать лок на время логина
        # только продлевает окно ожидания для конкурирующей покупки без
        # всякой пользы. А вот addClient/updateClient/delClient ниже —
        # именно то, что при параллельном запуске с созданием/продлением
        # ключа тем же пользователем даёт "Duplicate email"/"empty client ID"
        # на обеих сторонах гонки одновременно.
        # -------------------------------------------------------------------------
        host_lock = await xui_api.get_host_lock(host_name)
        async with host_lock:
            if run_expired_cleanup:
                all_inbounds = await _cleanup_expired_keys_for_host(
                    bot, host_name, api, all_inbounds, keys_in_db, now
                )
                keys_in_db = await asyncio.to_thread(database.get_keys_for_host, host_name)

            if run_orphan_sweep:
                await _sweep_orphaned_clients_for_host(api, host_name)
                # Сирот удаляли напрямую по свежему get_list() внутри сверки —
                # если что-то реально снесли, наш all_inbounds устарел, и
                # следующая за нами сверка клонов должна это увидеть.
                all_inbounds = await xui_api.run_in_xui_pool(api.inbound.get_list)

            # Grace-доступ к Telegram — безусловно, на каждом проходе
            # шедулера (не по хэшу и не раз в FULL_RECONCILE_INTERVAL_SECONDS,
            # как полная сверка клонов ниже). all_inbounds на этот момент —
            # самые свежие (учитывает возможное обновление чисткой/сиротами
            # выше в этом же блоке).
            _check_yield(host_name)
            await _sync_grace_inbound_for_host(
                bot, host_name, api, all_inbounds, keys_in_db, banned_map, now
            )

            state_hash = _compute_host_state_hash(keys_in_db, banned_map)
            last_hash = _host_state_hash.get(host_name)
            last_full_at = _host_last_full_sync_at.get(host_name)
            due_full_reconcile = (
                last_full_at is None
                or (now - last_full_at).total_seconds() >= FULL_RECONCILE_INTERVAL_SECONDS
            )

            if state_hash != last_hash or due_full_reconcile:
                affected, created = await _sync_clones_for_host(
                    bot,
                    host_name,
                    api,
                    master_inbound,
                    all_inbounds,
                    keys_in_db,
                    banned_map,
                    now,
                )
                _host_state_hash[host_name] = state_hash
                _host_last_full_sync_at[host_name] = now
                logger.info(
                    f"[{host_name}] Синхронизация клонов завершена. "
                    f"Записей обновлено: {affected}, клонов создано/восстановлено: {created}"
                )
            else:
                logger.debug(
                    f"[{host_name}] Состояние не изменилось с прошлой сверки — пропуск полной сверки клонов"
                )

        return (api, all_inbounds)

    except _YieldToPurchase:
        # Лок уже отдан (вышли из `async with host_lock`). Хэш состояния и время
        # полной сверки не обновлены — следующий цикл доделает проход.
        logger.info(
            f"[{host_name}] Проход шедулера прерван: покупка/продление ждёт host_lock. "
            f"Остаток будет доделан в следующем цикле."
        )
        return (api, all_inbounds)

    except Exception as e:
        logger.error(
            f"[{host_name}] Критическая ошибка во время синхронизации: {e}",
            exc_info=True,
        )
        return None


async def sync_keys_with_panels(bot: Bot) -> dict[str, tuple]:
    """
    Планировщик синхронизации ключей между локальной БД и панелями X-UI.

    Важные особенности:
    • обход хостов идёт параллельно (с ограничением HOST_SYNC_CONCURRENCY),
      а не последовательно одним циклом;
    • чистка просроченных (> 5 дней) ключей вынесена в отдельный, более редкий
      интервал (EXPIRED_CLEANUP_INTERVAL_SECONDS) — она не time-critical;
    • полная сверка клонов с панелью запускается только если локальный хэш
      состояния ключей хоста реально изменился, либо принудительно раз в
      FULL_RECONCILE_INTERVAL_SECONDS;
    • создание новых клонов батчится в один api.client.add() на inbound.

    Возвращает host_sessions: {host_name: (api, all_inbounds)} — снимок,
    сделанный при обработке каждого хоста в этом цикле, чтобы
    check_traffic_anomalies могла переиспользовать его без повторного логина.
    """
    global _last_expired_cleanup_at, _last_orphan_sweep_at

    now = datetime.now()
    all_hosts = await asyncio.to_thread(database.get_all_hosts)
    if not all_hosts:
        logger.info("Нет настроенных хостов для синхронизации")
        return {}

    run_expired_cleanup = (
        _last_expired_cleanup_at is None
        or (now - _last_expired_cleanup_at).total_seconds() >= EXPIRED_CLEANUP_INTERVAL_SECONDS
    )
    run_orphan_sweep = (
        _last_orphan_sweep_at is None
        or (now - _last_orphan_sweep_at).total_seconds() >= ORPHAN_SWEEP_INTERVAL_SECONDS
    )

    all_users = await asyncio.to_thread(database.get_all_users)
    banned_map = {u["telegram_id"]: bool(u.get("is_banned")) for u in all_users}

    semaphore = asyncio.Semaphore(HOST_SYNC_CONCURRENCY)
    host_sessions: dict[str, tuple] = {}

    async def _process(host: dict):
        async with semaphore:
            result = await _sync_single_host(
                bot, host, now, banned_map, run_expired_cleanup, run_orphan_sweep
            )
            if result is not None:
                host_sessions[host["host_name"]] = result

    await asyncio.gather(*(_process(h) for h in all_hosts), return_exceptions=True)

    if run_expired_cleanup:
        _last_expired_cleanup_at = now
    if run_orphan_sweep:
        _last_orphan_sweep_at = now

    return host_sessions


def format_emails_short(emails, limit: int = 1, code: bool = False) -> str:
    """Краткая строка с email клиента для сообщений админу.

    У одного UUID может быть несколько разных имён (мастер и клоны по разным
    инбаундам), и если склеить их все через запятую, сообщение упирается в
    лимит Telegram (message is too long). Показываем первое имя целиком,
    остальные — счётчиком: «vol_choke@snowyservices_bot и ещё 3».
    Используется и в алерте об аномальном трафике, и в отчёте «Потребление WL».
    code=True — для HTML-сообщений: показанные email экранируются и оборачиваются
    в <code> (счётчик «и ещё N» остаётся обычным текстом).
    """
    # Первым показываем мастера (без суффикса клона -N), потом клоны по порядку
    uniq = sorted(
        {str(e) for e in (emails or []) if e},
        key=lambda e: (bool(re.search(r"-\d+$", e.split("@")[0])), e),
    )
    if not uniq:
        return "—"
    if code:
        shown = ", ".join(f"<code>{html.escape(e)}</code>" for e in uniq[:limit])
    else:
        shown = ", ".join(uniq[:limit])
    if len(uniq) > limit:
        shown += f" и ещё {len(uniq) - limit}"
    return shown


def _plural_ru(n: int, one: str, few: str, many: str) -> str:
    """Склонение по числу: 1 подключение, 2 подключения, 5 подключений."""
    n = abs(int(n))
    if 11 <= n % 100 <= 14:
        return many
    last = n % 10
    if last == 1:
        return one
    if 2 <= last <= 4:
        return few
    return many


_USER_ALERT_TAIL = (
    "Прекратите передавать подписку другим. Если нагрузка не уменьшится, мы ограничим "
    "скорость или отключим подписку без возврата средств.\n\n"
    "Если считаете, что произошла ошибка, свяжитесь с поддержкой."
)


def _user_alert_text_traffic(delta_mb: float) -> str:
    """Текст пользователю: аномальный объём трафика за интервал."""
    minutes = max(1, round(CHECK_INTERVAL_SECONDS / 60))
    return (
        "🚨 <b>Аномальный трафик на вашей подписке</b>\n\n"
        f"{delta_mb / 1024:.1f} ГБ за {minutes} мин. — это не личное использование.\n\n"
        + _USER_ALERT_TAIL
    )


def _user_alert_text_sharing(active_count: int) -> str:
    """Текст пользователю: много одновременно активных подключений (шеринг), без аномалии трафика."""
    word = _plural_ru(active_count, "активное подключение", "активных подключения", "активных подключений")
    return (
        "🚨 <b>Аномальная активность на вашей подписке</b>\n\n"
        f"{active_count} {word} одновременно — это не личное использование.\n\n"
        + _USER_ALERT_TAIL
    )


# Статусы _send_user_warning -> что показать админу (cooldown не показываем: алерт не нужен)
_USER_WARN_STATUS_TEXT = {
    "sent": "✉️ Пользователю отправлено предупреждение",
    "blocked": "✉️ Не отправлено: пользователь заблокировал бота",
    "no_owner": "✉️ Не отправлено: у ключа нет владельца",
    "admin": "✉️ Не отправлено: владелец — админ",
    "banned": "✉️ Не отправлено: пользователь забанен",
    "cooldown": "✉️ Не отправлено: пользователя уже предупреждали (пауза между предупреждениями)",
    "error": "✉️ Не отправлено: ошибка отправки (см. лог)",
}


async def _send_user_warning(bot: Bot, c_uuid: str, text: str) -> str:
    """Отправляет предупреждение владельцу подписки с кнопкой «Поддержка».

    Возвращает статус: "sent" (доставлено), "cooldown" (с прошлого предупреждения
    не прошла пауза TRAFFIC_USER_ALERT_COOLDOWN_HOURS, она общая для обоих видов
    предупреждений), "no_owner" (ключ не найден или без владельца), "admin"
    (владелец — админ), "banned", "blocked" (пользователь заблокировал бота),
    "error". Любая ошибка логируется и не мешает остальной работе мониторинга.
    """
    try:
        key = await asyncio.to_thread(database.get_key_by_uuid, str(c_uuid))
        if not key or not key.get("user_id"):
            return "no_owner"
        user_id = int(key["user_id"])

        now = time.monotonic()
        last = user_alert_last_sent.get(user_id)
        if last is not None and now - last < USER_ALERT_COOLDOWN_HOURS * 3600:
            return "cooldown"

        if await asyncio.to_thread(database.is_admin, user_id):
            return "admin"
        user = await asyncio.to_thread(database.get_user, user_id)
        if not user:
            return "no_owner"
        if user.get("is_banned"):
            return "banned"

        from aiogram.types import InlineKeyboardMarkup

        from shop_bot.bot.keyboards import create_support_keyboard

        # В клавиатуре поддержки две кнопки; нужна только первая («Поддержка»)
        support_kb = await asyncio.to_thread(create_support_keyboard)
        markup = InlineKeyboardMarkup(inline_keyboard=support_kb.inline_keyboard[:1])

        try:
            await bot.send_message(
                chat_id=user_id, text=text, parse_mode="HTML", reply_markup=markup
            )
        except TelegramForbiddenError:
            # Не долбимся в закрытый чат каждые 5 минут
            user_alert_last_sent[user_id] = now
            logger.info(f"Предупреждение о трафике: пользователь {user_id} заблокировал бота")
            return "blocked"

        user_alert_last_sent[user_id] = now
        logger.info(f"Предупреждение о трафике отправлено пользователю {user_id} (UUID {c_uuid})")
        return "sent"
    except Exception as e:
        logger.warning(f"Не удалось отправить предупреждение о трафике (UUID {c_uuid}): {e}")
        return "error"


async def _notify_user_about_traffic(bot: Bot, c_uuid: str, delta_mb: float) -> str:
    """Предупреждение пользователю об аномальном трафике. Возвращает статус _send_user_warning."""
    return await _send_user_warning(bot, c_uuid, _user_alert_text_traffic(delta_mb))


async def _notify_user_about_sharing(bot: Bot, c_uuid: str, active_count: int) -> str:
    """Предупреждение пользователю о множестве активных подключений. Возвращает статус _send_user_warning."""
    return await _send_user_warning(bot, c_uuid, _user_alert_text_sharing(active_count))


async def check_traffic_anomalies(bot: Bot, host_sessions: dict[str, tuple] | None = None):
    """
    Мониторинг резких скачков трафика (только WL-инбаунды 🏳️) и шеринга (число активных
    email'ов подписки на всех инбаундах бота 🌍/🏳️), уведомление админа и владельца подписки.

    host_sessions (опционально): {host_name: (api, all_inbounds)}, уже
    полученные в sync_keys_with_panels в этом же цикле — если передан и
    содержит сервер, повторный login_and_get_inbounds для этого хоста не
    выполняется (экономит один тяжёлый запрос к панели на сервер за цикл).
    """
    logger.debug("Scheduler: Проверка аномального потребления трафика...")

    admin_id_raw = await asyncio.to_thread(database.get_setting, "admin_telegram_id")
    if not admin_id_raw:
        logger.warning("Scheduler: admin_telegram_id не настроен, пропуск алертов трафика")
        return
    admin_id = int(admin_id_raw)

    hosts = await asyncio.to_thread(database.get_all_hosts)
    if not hosts:
        return

    anomalies_found = False
    host_sessions = host_sessions or {}

    for host in hosts:
        host_name = host["host_name"]

        try:
            cached = host_sessions.get(host_name)
            if cached:
                _api, all_inbounds_raw = cached
            else:
                # Хоста не было в свежем снимке этого цикла (например, ошибка
                # sync_keys_with_panels для него) — логинимся отдельно
                _api, _master_ib, all_inbounds_raw = await xui_api.run_in_xui_pool(
                    xui_api.login_and_get_inbounds,
                    host["host_url"],
                    host["host_username"],
                    host["host_pass"],
                    host["host_inbound_id"],
                )
                if not _api:
                    continue

            # Аномалии трафика считаем только по WL-инбаундам (🏳️), как раньше.
            # Шеринг (число активных email'ов) считаем по всем инбаундам бота: 🌍 и 🏳️.
            all_inbounds = [
                ib
                for ib in (all_inbounds_raw or [])
                if hasattr(ib, "remark") and ("🏳️" in (ib.remark or "") or "🌍" in (ib.remark or ""))
            ]

            host_stats = {}  # только WL: объём трафика и активные email'ы для аномалий
            share_stats = {}  # все инбаунды бота: email'ы и активные email'ы для шеринга
            anomaly_user_warned = set()  # UUID, по которым пользователю уже предложили аномальное предупреждение

            for ib in all_inbounds:
                ib_id = getattr(ib, "id", ib.remark)
                is_wl = "🏳️" in (ib.remark or "")

                email_to_uuid = {}
                if ib.settings and ib.settings.clients:
                    email_to_uuid = {c.email: c.id for c in ib.settings.clients}

                if not ib.client_stats:
                    continue

                for stat in ib.client_stats:
                    c_uuid = email_to_uuid.get(stat.email, str(stat.email))

                    if c_uuid not in share_stats:
                        share_stats[c_uuid] = {"emails": set(), "active_emails": set()}
                    if is_wl and c_uuid not in host_stats:
                        host_stats[c_uuid] = {
                            "total": 0,
                            "emails": set(),
                            "active_emails": set(),
                        }

                    client_current_total = stat.up + stat.down
                    if stat.email:
                        share_stats[c_uuid]["emails"].add(stat.email)
                    if is_wl:
                        host_stats[c_uuid]["total"] += client_current_total
                        if stat.email:
                            host_stats[c_uuid]["emails"].add(stat.email)

                    email_snapshot_key = f"email:{host_name}:{ib_id}:{stat.email}"

                    if email_snapshot_key in traffic_snapshots:
                        email_delta = client_current_total - traffic_snapshots[email_snapshot_key]
                        if email_delta > 0 and is_wl:
                            host_stats[c_uuid]["active_emails"].add(stat.email)  # для отображения в алерте аномалии
                        # Для шеринга и предупреждений пользователю: заметный рост, не пинг/проверка связи
                        if email_delta > 0 and email_delta >= ACTIVE_MIN_KB * 1024:
                            share_stats[c_uuid]["active_emails"].add(stat.email)

                    traffic_snapshots[email_snapshot_key] = client_current_total

            for c_uuid, data in host_stats.items():
                current_total = data["total"]
                _emails_src = data["active_emails"] or data["emails"]
                display_emails = format_emails_short(_emails_src)
                display_emails_html = format_emails_short(_emails_src, code=True)

                snapshot_key = f"uuid:{host_name}:{c_uuid}"

                if snapshot_key in traffic_snapshots:
                    last_total = traffic_snapshots[snapshot_key]
                    delta_bytes = current_total - last_total

                    if delta_bytes > 0:
                        delta_mb = delta_bytes / (1024 * 1024)
                        mbps = (delta_mb * 8) / CHECK_INTERVAL_SECONDS

                        is_anomaly = False
                        if delta_mb > TRAFFIC_THRESHOLD_MB:
                            is_anomaly = True

                        if mbps > SPEED_THRESHOLD_MBPS:
                            traffic_strikes[snapshot_key] = traffic_strikes.get(snapshot_key, 0) + 1
                            if traffic_strikes[snapshot_key] >= STRIKE_THRESHOLD:
                                is_anomaly = True
                        else:
                            traffic_strikes[snapshot_key] = 0

                        if is_anomaly:
                            anomalies_found = True
                            logger.warning(
                                f"Аномалия трафика: {display_emails} [{c_uuid}] на {host_name} скачал {delta_mb:.2f} MB"
                            )

                            alert_text = (
                                f"🚨 <b>Аномальный трафик</b>\n\n"
                                f"🗺️ <b>Сервер:</b> {host_name}\n"
                                f"👤 <b>Клиент:</b> {display_emails_html}\n"
                                f"🆔 <b>UUID:</b> <code>{html.escape(str(c_uuid))}</code>\n"
                                f"📈 <b>Расход:</b> {delta_mb:.2f} МБ\n"
                                f"🚀 <b>Скорость:</b> {mbps:.1f} Мбит/с\n"
                                f"🕒 <b>Интервал:</b> {CHECK_INTERVAL_SECONDS // 60} мин."
                            )

                            # Только реально активные за интервал email'ы (без запасного варианта)
                            heavy_active = len(share_stats.get(c_uuid, {}).get("active_emails", ()))
                            if USER_ALERT_MIN_ACTIVE and heavy_active >= USER_ALERT_MIN_ACTIVE:
                                anomaly_user_warned.add(c_uuid)
                                warn_status = await _notify_user_about_traffic(bot, c_uuid, delta_mb)
                                alert_text += "\n\n" + _USER_WARN_STATUS_TEXT.get(
                                    warn_status, _USER_WARN_STATUS_TEXT["error"]
                                )

                            try:
                                await bot.send_message(
                                    chat_id=admin_id, text=alert_text, parse_mode="HTML"
                                )
                            except Exception as e:
                                logger.error(f"Ошибка отправки алерта админу: {e}")

                traffic_snapshots[snapshot_key] = current_total

            # Шеринг: много одновременно активных email'ов (мастер + клоны) на любых инбаундах бота,
            # аномалии трафика по этой подписке нет. Пауза с аномальным предупреждением общая, дублей не будет.
            for c_uuid, sdata in share_stats.items():
                active_count = len(sdata["active_emails"])
                strike_key = (host_name, str(c_uuid))
                if not USER_ALERT_SHARING or not USER_ALERT_MIN_ACTIVE:
                    continue
                if active_count < USER_ALERT_MIN_ACTIVE:
                    sharing_strikes.pop(strike_key, None)
                    continue
                # Шеринг — устойчивое состояние: разовый всплеск (пинг подписки) не считается
                sharing_strikes[strike_key] = sharing_strikes.get(strike_key, 0) + 1
                if sharing_strikes[strike_key] < USER_ALERT_SHARING_CHECKS or c_uuid in anomaly_user_warned:
                    continue

                # Подтверждение по IP: один человек с множеством конфигов (балансировка, автовыбор) — не шеринг.
                ip_set = None
                if USER_ALERT_MIN_IPS:
                    ip_set = await _count_subscription_ips(_api, sdata["active_emails"])
                    if ip_set is not None and len(ip_set) < USER_ALERT_MIN_IPS:
                        logger.info(
                            f"Шеринг не подтверждён: {c_uuid} на {host_name}, активных email: {active_count}, "
                            f"уникальных IP: {len(ip_set)} (< {USER_ALERT_MIN_IPS})"
                        )
                        continue
                    if ip_set is None:
                        logger.warning(
                            f"Шеринг: панель {host_name} не отдаёт IP клиента (limitIp=0 или нет access.log) — "
                            f"решение только по числу email'ов"
                        )

                display_emails = format_emails_short(sdata["active_emails"])
                display_emails_html = format_emails_short(sdata["active_emails"], code=True)
                logger.info(
                    f"Шеринг: {display_emails} [{c_uuid}] на {host_name}, активных email: {active_count}"
                )
                status = await _notify_user_about_sharing(bot, c_uuid, active_count)

                # Админу: когда предупреждение ушло, либо когда оно не может уйти (бан, нет владельца и т.п.),
                # но не чаще одного раза за паузу на подписку. "cooldown" — пользователю уже писали, алерт не нужен.
                admin_key = (host_name, str(c_uuid))
                admin_last = sharing_admin_last_sent.get(admin_key)
                admin_due = admin_last is None or (
                    time.monotonic() - admin_last >= USER_ALERT_COOLDOWN_HOURS * 3600
                )
                if status == "sent" or (status != "cooldown" and admin_due):
                    share_text = (
                        f"🔗 <b>Подозрение на шеринг</b>\n\n"
                        f"🗺️ <b>Сервер:</b> {host_name}\n"
                        f"👤 <b>Клиент:</b> {display_emails_html}\n"
                        f"🆔 <b>UUID:</b> <code>{html.escape(str(c_uuid))}</code>\n"
                        f"👥 <b>Активных подключений:</b> {active_count}\n"
                        + (f"🌐 <b>Уникальных IP:</b> {len(ip_set)} за {USER_ALERT_IP_WINDOW_MIN} мин.\n" if ip_set is not None else "")
                        + f"🕒 <b>Интервал:</b> {CHECK_INTERVAL_SECONDS // 60} мин.\n\n"
                        + _USER_WARN_STATUS_TEXT.get(status, _USER_WARN_STATUS_TEXT["error"])
                    )
                    sharing_admin_last_sent[admin_key] = time.monotonic()
                    try:
                        await bot.send_message(chat_id=admin_id, text=share_text, parse_mode="HTML")
                    except Exception as e:
                        logger.error(f"Ошибка отправки алерта админу о шеринге: {e}")

            for k in [k for k in sharing_strikes if k[0] == host_name and k[1] not in {str(u) for u in share_stats}]:
                sharing_strikes.pop(k, None)

        except Exception as e:
            logger.error(f"Scheduler: Ошибка мониторинга трафика на {host_name}: {e}")

    if not anomalies_found:
        logger.info("Scheduler: Проверка аномалий трафика завершена. Ничего не обнаружено.")


async def execute_broadcast(
    bot: Bot,
    segment: str,
    source_chat_id: int,
    source_message_id: int,
    button_text: str | None = None,
    button_url: str | None = None,
    promo_discount: float | None = None,
) -> dict:
    """Рассылает сообщение всем пользователям сегмента, копируя его из
    source_chat_id/source_message_id (тот же механизм copy_message, что и у
    немедленной рассылки). Общая реализация для обоих путей: немедленного
    (confirm_broadcast_handler в admin_handlers.py) и отложенного
    (_maybe_send_scheduled_broadcasts ниже), чтобы логика ретраев/бана/
    личных промокодов не могла разойтись между ними.

    Возвращает словарь со счётчиками для итогового отчёта админу.
    """
    final_keyboard = None
    if button_text and button_url:
        builder = InlineKeyboardBuilder()
        builder.button(text=button_text, url=button_url)
        final_keyboard = builder.as_markup()

    users = await asyncio.to_thread(database.get_users_by_segment, segment)
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

        personal_code = None
        if promo_discount:
            personal_code = f"PROMO{user_id}{secrets.token_hex(2).upper()}"
            try:
                created = await asyncio.to_thread(
                    database.create_promo_code,
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
                from_chat_id=source_chat_id,
                message_id=source_message_id,
                reply_markup=final_keyboard,
            )
            if personal_code:
                try:
                    await bot.send_message(
                        user_id,
                        f"🎁 <b>Ваш персональный промокод:</b> <code>{personal_code}</code>\n\n"
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
            try:
                await bot.copy_message(
                    chat_id=user_id,
                    from_chat_id=source_chat_id,
                    message_id=source_message_id,
                    reply_markup=final_keyboard,
                )
                if personal_code:
                    try:
                        await bot.send_message(
                            user_id,
                            f"🎁 <b>Ваш персональный промокод:</b> <code>{personal_code}</code>\n\n"
                            f"Скидка {promo_discount:.0f}%, действует один раз — только для вас",
                            parse_mode="HTML",
                        )
                    except Exception:
                        pass
                sent_count += 1
            except Exception as internal_e:
                failed_count += 1
                logger.error(f"Повторная отправка пользователю {user_id} не удалась: {internal_e}")

        except TelegramForbiddenError:
            blocked_by_user += 1
            logger.info(f"Пользователь {user_id} заблокировал бота")

        except Exception as e:
            failed_count += 1
            logger.warning(f"Не удалось отправить сообщение пользователю {user_id}: {e}")

    return {
        "sent": sent_count,
        "failed": failed_count,
        "banned": banned_count,
        "blocked_by_user": blocked_by_user,
        "promo_failed": promo_failed_count if promo_discount else None,
    }


def format_broadcast_summary(result: dict) -> str:
    """Та же сводка, что показывается админу после немедленной рассылки —
    используется и после отложенной, чтобы текст отчёта не расходился."""
    lines = [
        "✅ <b>Рассылка завершена!</b>\n\n",
        f"👍 <b>Отправлено:</b> {result['sent']}",
        f"👎 <b>Не удалось отправить:</b> {result['failed']}",
        f"🚫 <b>Пропущено (забанены):</b> {result['banned']}",
        f"🗑️ <b>Пропущено (забанили):</b> {result['blocked_by_user']}",
    ]
    if result.get("promo_failed") is not None:
        lines.append(f"🎁 <b>Не удалось создать промокод:</b> {result['promo_failed']}")
    return "\n".join(lines)


async def _maybe_send_scheduled_broadcasts(bot: Bot):
    """Раз в цикл планировщика проверяет отложенные рассылки, чьё время уже
    наступило, и отправляет их — тем же execute_broadcast, что и немедленная
    рассылка из админки. Админу, создавшему рассылку, присылается тот же
    итоговый отчёт, что он увидел бы при немедленной отправке."""
    due = await asyncio.to_thread(database.get_due_scheduled_broadcasts)
    if not due:
        return

    for item in due:
        broadcast_id = item["id"]
        try:
            result = await execute_broadcast(
                bot,
                segment=item["segment"],
                source_chat_id=item["source_chat_id"],
                source_message_id=item["source_message_id"],
                button_text=item.get("button_text"),
                button_url=item.get("button_url"),
                promo_discount=item.get("promo_discount"),
            )
            summary = format_broadcast_summary(result)
            await asyncio.to_thread(
                database.mark_scheduled_broadcast_done, broadcast_id, "sent", summary
            )
            logger.info(f"Отложенная рассылка №{broadcast_id} отправлена: {summary!r}")

            notify_text = (
                f"📅 <b>Отложенная рассылка №{broadcast_id} отправлена</b>\n"
                f"🗂️ <b>Сегмент:</b> {item['segment_label']}\n\n" + summary
            )
            created_by = item.get("created_by")
            if created_by:
                try:
                    await bot.send_message(created_by, notify_text, parse_mode="HTML")
                except Exception:
                    pass
        except Exception as e:
            logger.error(f"Отложенная рассылка №{broadcast_id} завершилась ошибкой: {e}", exc_info=True)
            await asyncio.to_thread(
                database.mark_scheduled_broadcast_done, broadcast_id, "failed", str(e)
            )
            created_by = item.get("created_by")
            if created_by:
                try:
                    await bot.send_message(
                        created_by,
                        f"❌ <b>Отложенная рассылка №{broadcast_id} не отправлена</b>\n\n{e}",
                        parse_mode="HTML",
                    )
                except Exception:
                    pass


# -------------------------------------------------------------------------
# Внешний чёрный список (github.com/Blin4ickUSE/ban-vpn): одна строка = один
# Telegram ID, после «#» — причина. Периодически скачиваем, баним совпадения
# среди пользователей бота и блокируем ещё не зарегистрированные ID (см.
# database.sync_external_blacklist и BanMiddleware).
# -------------------------------------------------------------------------
BLACKLIST_ENABLED = env_int("BLACKLIST_ENABLED", 1, 0, 1)
BLACKLIST_GITHUB_URL = (
    os.getenv("BLACKLIST_GITHUB_URL")
    or "https://raw.githubusercontent.com/Blin4ickUSE/ban-vpn/refs/heads/main/blacklist.txt"
).strip()
# Как часто сверяться, часов (README списка рекомендует раз в час)
BLACKLIST_UPDATE_INTERVAL_HOURS = env_float("BLACKLIST_UPDATE_INTERVAL_HOURS", 1.0, 0.25, 168.0)
# 1 — администраторов бота никогда не банить
BLACKLIST_IGNORE_ADMINS = env_int("BLACKLIST_IGNORE_ADMINS", 1, 0, 1)
# 1 — снимать бан, выставленный по списку, если ID пропал из списка
BLACKLIST_AUTO_UNBAN = env_int("BLACKLIST_AUTO_UNBAN", 1, 0, 1)
# Защита от испорченного списка: если за один проход нужно забанить больше стольких
# пользователей, ничего не применяем и пишем админу (0 — без лимита)
BLACKLIST_MAX_BANS_PER_RUN = env_int("BLACKLIST_MAX_BANS_PER_RUN", 100, 0, 100000)
BLACKLIST_LAST_RUN_SETTING = "blacklist_last_run_at"
BLACKLIST_MIN_ENTRIES = 10  # меньше — считаем, что файл пуст/повреждён
BLACKLIST_MAX_BYTES = 4 * 1024 * 1024
_BLACKLIST_LINE_RE = re.compile(r"^(\d{1,13})\s*(?:#\s*(.*))?$")
_blacklist_last_fail_at: datetime | None = None
_blacklist_etag: str | None = None
_blacklist_cache: dict[int, str] | None = None
_blacklist_alert_at: dict[str, float] = {}  # тип алерта -> time.monotonic() последней отправки
BLACKLIST_ALERT_PAUSE_SECONDS = 24 * 3600
BLACKLIST_RETRY_AFTER_FAILURE_SECONDS = 600


def _parse_blacklist(text: str) -> dict[int, str]:
    """Разбирает blacklist.txt: «ID # причина», пустые строки и строки-комментарии пропускаются."""
    entries: dict[int, str] = {}
    for raw in text.splitlines():
        line = raw.strip().lstrip("\ufeff")
        if not line or line.startswith("#"):
            continue
        m = _BLACKLIST_LINE_RE.match(line)
        if not m:
            continue
        uid = int(m.group(1))
        if uid <= 0:
            continue
        entries.setdefault(uid, (m.group(2) or "").strip()[:120])
    return entries


def _download_blacklist(url: str, etag: str | None) -> tuple[int, bytes, str | None]:
    """Скачивает список по HTTPS (без прокси из окружения). Возвращает (статус, тело, etag).
    Статус 304 означает «не менялся» (тело пустое)."""
    if not url.lower().startswith("https://"):
        raise ValueError("BLACKLIST_GITHUB_URL должен начинаться с https://")
    headers = {"User-Agent": "3xui-shopbot-blacklist-sync"}
    if etag:
        headers["If-None-Match"] = etag
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    req = urllib.request.Request(url, headers=headers)
    try:
        with opener.open(req, timeout=20) as resp:
            body = resp.read(BLACKLIST_MAX_BYTES + 1)
            if len(body) > BLACKLIST_MAX_BYTES:
                raise ValueError("файл списка больше допустимого размера")
            return resp.status, body, resp.headers.get("ETag")
    except urllib.error.HTTPError as e:
        if e.code == 304:
            return 304, b"", etag
        raise


def _blacklist_alert_allowed(kind: str) -> bool:
    """Не чаще раза в сутки на каждый тип служебного алерта (ошибка загрузки, подозрительный список)."""
    now = time.monotonic()
    last = _blacklist_alert_at.get(kind)
    if last is not None and now - last < BLACKLIST_ALERT_PAUSE_SECONDS:
        return False
    _blacklist_alert_at[kind] = now
    return True


async def _notify_admins(bot: Bot, text: str, markup=None) -> None:
    try:
        admin_ids = await asyncio.to_thread(database.get_admin_ids)
    except Exception:
        admin_ids = set()
    for admin_id in admin_ids:
        try:
            await bot.send_message(
                chat_id=admin_id, text=text, parse_mode="HTML", reply_markup=markup
            )
        except Exception as e:
            logger.error(f"Scheduler: не удалось отправить сообщение админу {admin_id}: {e}")


def _fmt_blacklist_user(info: dict) -> str:
    uname = info.get("username")
    who = f"@{html.escape(str(uname))}" if uname else "без username"
    reason = html.escape(info.get("reason") or "причина не указана")
    return f"• <code>{info['id']}</code> {who} — {reason} (ключей: {info.get('keys', 0)})"


async def _maybe_sync_external_blacklist(bot: Bot) -> None:
    """Раз в BLACKLIST_UPDATE_INTERVAL_HOURS скачивает внешний чёрный список и применяет баны.
    Время последней успешной сверки хранится в bot_settings и переживает рестарт."""
    global _blacklist_last_fail_at, _blacklist_etag, _blacklist_cache

    if not BLACKLIST_ENABLED or not BLACKLIST_GITHUB_URL:
        return

    now = datetime.now()
    interval = BLACKLIST_UPDATE_INTERVAL_HOURS * 3600
    try:
        stored = await asyncio.to_thread(database.get_setting, BLACKLIST_LAST_RUN_SETTING)
        last_ok = datetime.fromisoformat(stored) if stored else None
    except Exception:
        last_ok = None
    if last_ok and (now - last_ok).total_seconds() < interval - 30:
        return
    # После неудачи не долбим GitHub каждый цикл планировщика
    if (
        _blacklist_last_fail_at
        and (now - _blacklist_last_fail_at).total_seconds() < BLACKLIST_RETRY_AFTER_FAILURE_SECONDS
    ):
        return

    try:
        status, body, etag = await asyncio.to_thread(
            _download_blacklist, BLACKLIST_GITHUB_URL, _blacklist_etag if _blacklist_cache else None
        )
        if status == 304 and _blacklist_cache is not None:
            entries = _blacklist_cache
        elif status == 200:
            entries = _parse_blacklist(body.decode("utf-8", errors="replace"))
        else:
            raise RuntimeError(f"HTTP {status}")
    except Exception as e:
        _blacklist_last_fail_at = now
        logger.error(f"Scheduler: не удалось загрузить чёрный список: {e}")
        if _blacklist_alert_allowed("fetch"):
            await _notify_admins(
                bot,
                "⚠️ <b>Чёрный список (ban-vpn)</b>: не удалось загрузить файл\n"
                f"<code>{html.escape(str(e))[:300]}</code>\n"
                "Повторю попытку автоматически.",
            )
        return

    # Защита от пустого/усечённого файла: баны не трогаем
    prev_count = await asyncio.to_thread(database.count_external_blacklist)
    if len(entries) < BLACKLIST_MIN_ENTRIES or (prev_count >= 50 and len(entries) < prev_count * 0.5):
        _blacklist_last_fail_at = now
        logger.warning(
            f"Scheduler: чёрный список выглядит повреждённым ({len(entries)} записей, было {prev_count}) — пропускаю."
        )
        if _blacklist_alert_allowed("suspicious"):
            await _notify_admins(
                bot,
                "⚠️ <b>Чёрный список (ban-vpn)</b>: файл выглядит повреждённым "
                f"({len(entries)} записей, было {prev_count}). Сверка пропущена, баны не менялись.",
            )
        return

    ignore: set[int] = set()
    if BLACKLIST_IGNORE_ADMINS:
        try:
            ignore = set(await asyncio.to_thread(database.get_admin_ids))
        except Exception:
            ignore = set()

    try:
        res = await asyncio.to_thread(
            database.sync_external_blacklist,
            entries,
            ignore,
            bool(BLACKLIST_AUTO_UNBAN),
            BLACKLIST_MAX_BANS_PER_RUN,
        )
    except Exception as e:
        logger.error(f"Scheduler: ошибка применения чёрного списка: {e}", exc_info=True)
        return

    _blacklist_last_fail_at = None
    if etag:
        _blacklist_etag = etag
    _blacklist_cache = entries
    # Время сверки пишем и при срабатывании лимита: следующая попытка через интервал, а не каждые 10 минут
    await asyncio.to_thread(database.update_setting, BLACKLIST_LAST_RUN_SETTING, now.isoformat())

    if res.get("blocked_by_limit"):
        n = len(res["banned"])
        logger.warning(
            f"Scheduler: чёрный список потребовал {n} банов за раз (лимит {BLACKLIST_MAX_BANS_PER_RUN}) — не применён."
        )
        if _blacklist_alert_allowed("limit"):
            await _notify_admins(
                bot,
                "⚠️ <b>Чёрный список (ban-vpn)</b>: нужно забанить "
                f"<b>{n}</b> пользователей за один проход (лимит {BLACKLIST_MAX_BANS_PER_RUN}). "
                "Баны не применены. Проверьте список, и если всё верно, увеличьте "
                "<code>BLACKLIST_MAX_BANS_PER_RUN</code> в .env и перезапустите бота.",
            )
        return

    logger.info(
        "Scheduler: чёрный список: записей %s, новых %s, забанено %s, уже были забанены %s, "
        "разбанено %s, ожидают регистрации %s, ручных разбанов учтено %s",
        res["total"],
        res["new"],
        len(res["banned"]),
        len(res["already_banned"]),
        len(res["unbanned"]),
        res["pending"],
        res["overridden"],
    )

    if res["banned"] or res["unbanned"]:
        lines = ["🚫 <b>Чёрный список (ban-vpn)</b>"]
        if res["banned"]:
            lines.append(f"\nЗаблокировано: <b>{len(res['banned'])}</b>")
            lines += [_fmt_blacklist_user(i) for i in res["banned"][:20]]
            if len(res["banned"]) > 20:
                lines.append(f"… и ещё {len(res['banned']) - 20}")
        if res["unbanned"]:
            lines.append(f"\nРазблокировано (удалены из списка): <b>{len(res['unbanned'])}</b>")
            lines += [_fmt_blacklist_user(i) for i in res["unbanned"][:20]]
            if len(res["unbanned"]) > 20:
                lines.append(f"… и ещё {len(res['unbanned']) - 20}")
        markup = None
        if res["banned"]:
            kb = InlineKeyboardBuilder()
            for info in res["banned"][:10]:
                kb.button(text=f"✅ Разбанить {info['id']}", callback_data=f"admin_unban_user_{info['id']}")
            kb.adjust(1)
            markup = kb.as_markup()
            lines.append("\nОшибочный бан можно снять кнопкой: повторно по списку не забаним.")
        await _notify_admins(bot, "\n".join(lines), markup)


async def periodic_subscription_check(bot_controller: BotController):
    """Главный бесконечный цикл фонового планировщика (запускается один раз при старте бота)."""
    logger.info("Scheduler: Планировщик фоновых задач запущен.")
    await asyncio.sleep(10)

    while True:
        bot = None
        try:
            if bot_controller.get_status().get("is_running"):
                bot = bot_controller.get_bot_instance()
                if bot:
                    # Внешний чёрный список: баны применяем до синхронизации, чтобы ключи
                    # забаненных отключились на панелях в этом же проходе
                    await _maybe_sync_external_blacklist(bot)
                    # Синхронизация подписок: возвращает {host_name: (api, all_inbounds)},
                    # полученные в этом же проходе — переиспользуем их ниже в
                    # check_traffic_anomalies, чтобы не логиниться в панели второй раз
                    host_sessions = await sync_keys_with_panels(bot)
                    await process_auto_renewals(bot)
                    await check_expiring_subscriptions(bot)
                    await check_traffic_anomalies(bot, host_sessions)
                    await _maybe_send_scheduled_broadcasts(bot)
                else:
                    logger.warning(
                        "Scheduler: Бот помечен как запущенный, но экземпляр недоступен."
                    )
            else:
                logger.debug("Scheduler: Бот остановлен, уведомления пользователям пропущены.")

            # Периодические проверки доступности, сбор метрик и замеры скорости по всем хостам (оба варианта: SSH и сетевой)
            await _check_hosts_availability(bot)
            await _maybe_collect_host_metrics()
            await _maybe_run_periodic_speedtests()

            bot = (
                bot_controller.get_bot_instance()
                if bot_controller.get_status().get("is_running")
                else None
            )
            if bot:
                # Ежедневный автобэкап БД с отправкой админам
                await _maybe_run_daily_backup(bot)

            # Плановое обслуживание БД (REINDEX + VACUUM)
            await _maybe_run_db_maintenance()

        except Exception as e:
            logger.error(f"Scheduler: Необработанная ошибка в основном цикле: {e}", exc_info=True)

        logger.info(
            f"Scheduler: Цикл завершён. Следующая проверка через {CHECK_INTERVAL_SECONDS} сек."
        )
        await asyncio.sleep(CHECK_INTERVAL_SECONDS)


async def _maybe_run_periodic_speedtests():
    global _last_speedtests_run_at

    if os.getenv("USE_WEBAPP_SPEEDTEST", "").strip() == "1":
        # Спидтест в этом режиме запускается вручную из веб-приложения
        # (кнопка/страница спидтеста), а не по расписанию планировщика —
        # плановый прогон тут не нужен и только дублировал бы работу.
        logger.debug(
            "Scheduler: USE_WEBAPP_SPEEDTEST=1 — плановый speedtest отключён, "
            "запускается только вручную из веб-приложения."
        )
        return

    now = datetime.now()

    if _last_speedtests_run_at is None:
        # _last_speedtests_run_at — переменная в памяти процесса, при
        # каждом рестарте бота (деплой, краш, ручной перезапуск) она
        # сбрасывается в None — без этой подстраховки следующий же цикл
        # планировщика после рестарта тут же гонял бы спидтест заново.
        # Восстанавливаем реальное время прогона из существующей таблицы host_speedtests.
        _last_speedtests_run_at = await asyncio.to_thread(database.get_latest_speedtest_run_at)

    if (
        _last_speedtests_run_at
        and (now - _last_speedtests_run_at).total_seconds() < SPEEDTEST_INTERVAL_SECONDS
    ):
        logger.debug(
            f"Scheduler: speedtest уже проводился менее {SPEEDTEST_INTERVAL_SECONDS // 3600}ч "
            f"назад ({_last_speedtests_run_at.isoformat()}) — пропускаю."
        )
        return
    try:
        await _run_speedtests_for_all_hosts()
        _last_speedtests_run_at = now
    except Exception as e:
        logger.error(f"Scheduler: Ошибка запуска speedtests: {e}", exc_info=True)


async def _run_speedtests_for_all_hosts():
    hosts = await asyncio.to_thread(database.get_all_hosts)
    if not hosts:
        logger.debug("Scheduler: Нет хостов для измерений скорости.")
        return

    logger.info(f"Scheduler: Запускаю speedtest для {len(hosts)} сервер(ов)...")

    for h in hosts:
        host_name = h.get("host_name")
        if not host_name:
            continue

        # -------------------------------------------------------------------------
        # Выполнение замера скорости для текущего хоста с защитой по таймауту
        # -------------------------------------------------------------------------
        try:
            logger.info(f"Scheduler: Speedtest для '{host_name}' запущен...")
            res = None

            # Ограничиваем выполнение 3 минутами на случай зависания
            try:
                try:
                    async with asyncio.timeout(180):
                        res = await speedtest_runner.run_both_for_host(host_name)
                except AttributeError:
                    # Fallback для старых версий Python (< 3.11)
                    res = await asyncio.wait_for(
                        speedtest_runner.run_both_for_host(host_name), timeout=180
                    )
            except (asyncio.TimeoutError, TimeoutError):
                # Ловим таймаут сразу здесь, чтобы не прерывать общий блок try
                logger.warning(f"Scheduler: Превышен таймаут speedtest для хоста '{host_name}'")
                continue  # Переходим к следующему хосту в цикле

            # Безопасно парсим ответ, если замер успел завершиться
            ok = res.get("ok") if isinstance(res, dict) else False
            err = res.get("error") if isinstance(res, dict) else "No response format"

            if ok:
                logger.info(f"Scheduler: Speedtest для '{host_name}' завершён")
            else:
                logger.warning(f"Scheduler: Speedtest для '{host_name}' завершён с ошибками: {err}")

        except Exception as e:
            # Сюда теперь падает только настоящая непредвиденная ошибка (например, сбой БД)
            logger.error(
                f"Scheduler: Непредвиденная ошибка выполнения speedtest для '{host_name}': {e}",
                exc_info=True,
            )

    # -------------------------------------------------------------------------
    # Финал цикла
    # -------------------------------------------------------------------------
    logger.info("Scheduler: Все плановые замеры скорости последовательно завершены.")


async def _maybe_run_daily_backup(bot: Bot):
    global _last_backup_run_at
    now = datetime.now()
    # Считаем интервал из настроек (в днях). 0 или пусто — автобэкап выключен.
    try:
        s = await asyncio.to_thread(database.get_setting, "backup_interval_days") or "1"
        days = int(str(s).strip() or "1")
    except Exception:
        days = 1
    if days <= 0:
        return
    interval_seconds = max(1, days) * 24 * 3600

    if _last_backup_run_at is None:
        # Та же логика, что и в _maybe_run_periodic_speedtests: _last_backup_run_at
        # живёт только в памяти процесса и сбрасывается при каждом рестарте
        # бота — без подстраховки это грозило бы спамом всех админов
        # файлом бэкапа при каждом перезапуске вместо заявленного раза в день.
        # Восстанавливаем реальное время бэкапа из таблицы backup_history.
        _last_backup_run_at = await asyncio.to_thread(database.get_latest_backup_created_at)

    if _last_backup_run_at and (now - _last_backup_run_at).total_seconds() < interval_seconds:
        return
    try:
        zip_path = await asyncio.to_thread(backup_manager.create_backup_file)
        if zip_path and zip_path.exists():
            try:
                sent = await backup_manager.send_backup_to_admins(bot, zip_path)
                logger.info(f"Scheduler: Создан бэкап {zip_path.name}, отправлен {sent} адм.")
            except Exception as e:
                logger.error(f"Scheduler: Не удалось отправить бэкап: {e}")
            try:
                backup_manager.cleanup_old_backups(keep=BACKUP_KEEP_COUNT)
            except Exception:
                pass
            await asyncio.to_thread(database.record_backup_created, zip_path.name)
        _last_backup_run_at = now
    except Exception as e:
        logger.error(
            f"Scheduler: Критическая ошибка при создании и отправке бэкапа: {e}",
            exc_info=True,
        )


DB_MAINTENANCE_LAST_RUN_SETTING = "db_maintenance_last_run_at"
_db_maintenance_last_attempt_day: str | None = None


async def _maybe_run_db_maintenance():
    """REINDEX + VACUUM базы раз в DB_MAINTENANCE_INTERVAL_DAYS дней (по умолчанию 1 — ежедневно,
    0 — выключено), только в окне DB_MAINTENANCE_HOUR...+2 ч (по умолчанию с 04:00).
    Время последнего запуска хранится в bot_settings и переживает рестарт."""
    try:
        days = int(os.getenv("DB_MAINTENANCE_INTERVAL_DAYS", "1").strip() or "1")
    except ValueError:
        days = 1
    if days <= 0:
        return
    try:
        start_hour = int(os.getenv("DB_MAINTENANCE_HOUR", "4").strip() or "4")
    except ValueError:
        start_hour = 4

    now = datetime.now()
    if not (start_hour <= now.hour < start_hour + 2):
        return

    try:
        stored = await asyncio.to_thread(database.get_setting, DB_MAINTENANCE_LAST_RUN_SETTING)
        last = datetime.fromisoformat(stored) if stored else None
    except Exception:
        last = None
    if last and (now - last).total_seconds() < days * 24 * 3600 - 3600:
        return

    # Не чаще одной попытки в сутки (иначе она повторялась бы каждый цикл планировщика)
    global _db_maintenance_last_attempt_day
    today = now.strftime("%Y-%m-%d")
    if _db_maintenance_last_attempt_day == today:
        return
    _db_maintenance_last_attempt_day = today

    logger.info("Scheduler: Запускаю обслуживание БД (REINDEX + VACUUM)...")
    try:
        # Страховка: перед VACUUM нужен свежий бэкап. Если ежедневный автобэкап
        # уже был в последние сутки — не дублируем, иначе делаем локальный
        # (без отправки админам)
        try:
            last_backup = await asyncio.to_thread(database.get_latest_backup_created_at)
            if not last_backup or (now - last_backup).total_seconds() > 24 * 3600:
                await asyncio.to_thread(backup_manager.create_backup_file)
        except Exception as e:
            logger.warning(f"Scheduler: Не удалось сделать бэкап перед обслуживанием БД: {e}")
        res = await asyncio.to_thread(database.run_db_maintenance)
        await asyncio.to_thread(
            database.update_setting, DB_MAINTENANCE_LAST_RUN_SETTING, now.isoformat()
        )
        logger.info(
            "Scheduler: Обслуживание БД завершено за %s с: %.1f МБ -> %.1f МБ",
            res.get("total_s"),
            res.get("size_before", 0) / 1048576,
            res.get("size_after", 0) / 1048576,
        )
    except database.DatabaseCorruptedError as e:
        logger.critical(f"Scheduler: Обслуживание БД отменено — база повреждена: {e}")
    except Exception as e:
        # Время запуска не записываем: попробуем снова на следующий день
        logger.error(f"Scheduler: Ошибка обслуживания БД: {e}", exc_info=True)


async def _maybe_collect_host_metrics():
    global _last_metrics_run_at
    now = datetime.now()
    if (
        _last_metrics_run_at
        and (now - _last_metrics_run_at).total_seconds() < METRICS_INTERVAL_SECONDS
    ):
        return

    # -------------------------------------------------------------------------
    # 1. Сбор локальных метрик самой панели
    # -------------------------------------------------------------------------
    try:
        # Локальные метрики быстрые, тут wait_for можно оставить для подстраховки
        local_metrics = await asyncio.wait_for(
            asyncio.to_thread(resource_monitor.get_local_metrics), timeout=30
        )
        if local_metrics and local_metrics.get("ok"):
            database.insert_resource_metric(
                "local",
                "panel",
                cpu_percent=local_metrics.get("cpu_percent"),
                mem_percent=local_metrics.get("mem_percent"),
                disk_percent=local_metrics.get("disk_percent"),
                load1=(
                    local_metrics.get("loadavg", {}).get("1m")
                    if local_metrics.get("loadavg")
                    else None
                ),
                net_bytes_sent=local_metrics.get("network_sent"),
                net_bytes_recv=local_metrics.get("network_recv"),
                raw_json=json.dumps(local_metrics, ensure_ascii=False),
            )
    except Exception as e:
        logger.error(f"Scheduler: Ошибка сбора локальных метрик: {e}")

    # -------------------------------------------------------------------------
    # 2. Получение списка хостов для мониторинга
    # -------------------------------------------------------------------------
    hosts = await asyncio.to_thread(database.get_all_hosts)
    if not hosts:
        _last_metrics_run_at = now
        return

    # Вспомогательная асинхронная задача для обработки одного хоста
    async def process_single_host(h: dict):
        host_name = h.get("host_name")
        if not host_name or not (h.get("ssh_host") and h.get("ssh_user")):
            return

        try:
            # Выполняем синхронный опрос в отдельном потоке.
            # Внутренние таймауты Paramiko сами вернут результат через 10 сек.
            m = await asyncio.to_thread(resource_monitor.get_host_metrics_via_ssh, h)

            # Сохраняем результаты в базу данных
            await asyncio.to_thread(database.insert_host_metrics, host_name, m)

            # Если метрики успешно собраны, пишем в историю для графиков
            if m and m.get("ok"):
                await asyncio.to_thread(
                    database.insert_resource_metric,
                    "host",
                    host_name,
                    cpu_percent=m.get("cpu_percent"),
                    mem_percent=m.get("mem_percent"),
                    disk_percent=m.get("disk_percent"),
                    load1=m.get("loadavg", {}).get("1m") if m.get("loadavg") else None,
                    raw_json=json.dumps(m, ensure_ascii=False),
                )
            else:
                error_msg = m.get("error") if m else "Unknown error"
                logger.warning(
                    f"Scheduler: Не удалось собрать метрики для '{host_name}': {error_msg}"
                )

        except Exception as e:
            logger.error(
                f"Scheduler: Критическая ошибка при обработке хоста '{host_name}': {e}",
                exc_info=True,
            )

    # -------------------------------------------------------------------------
    # 3. Параллельный запуск опроса всех хостов одновременно
    # -------------------------------------------------------------------------
    tasks = [process_single_host(h) for h in hosts]
    if tasks:
        # return_exceptions=True гарантирует, что если один сервер вызовет сбой,
        # остальные всё равно будут обработаны до конца
        await asyncio.gather(*tasks, return_exceptions=True)

    _last_metrics_run_at = now


async def _check_hosts_availability(bot: Bot):
    now = datetime.now()

    # -------------------------------------------------------------------------
    # 1. Получаем список хостов для мониторинга из базы данных
    # -------------------------------------------------------------------------
    hosts = await asyncio.to_thread(database.get_all_hosts)
    if not hosts:
        return

    failed_hosts = []
    formatted_time = now.strftime("%H:%M:%S")

    # Вспомогательная асинхронная задача для обработки одного хоста
    async def check_single_host(h: dict):
        host_name = h.get("host_name")
        host_url = h.get("host_url")

        if not host_name or not host_url:
            return

        try:
            if not host_url.startswith(("http://", "https://")):
                host_url = "https://" + host_url

            parsed_url = urlparse(host_url)
            domain = parsed_url.hostname

            if not domain:
                logger.error(f"Scheduler: Не удалось извлечь домен из URL для хоста {host_name}")
                failed_hosts.append(host_name)
                return

            # Асинхронный ICMP-пинг средствами icmplib
            host_info = await async_ping(domain, count=3, interval=0.5, timeout=5, privileged=False)

            # Если сервер не ответил на ICMP запрос
            if not host_info.is_alive:
                failed_hosts.append(host_name)

        except Exception as e:
            logger.error(f"Scheduler: Критическая ошибка при пинге {host_name}: {e}")
            failed_hosts.append(host_name)

    # -------------------------------------------------------------------------
    # 2. Параллельный запуск пинга всех хостов одновременно
    # -------------------------------------------------------------------------
    tasks = [check_single_host(h) for h in hosts]
    if tasks:
        await asyncio.gather(*tasks, return_exceptions=True)

    # Логируем общий статус проверки перед отправкой уведомлений
    if not failed_hosts:
        logger.info("Scheduler: Проверка доступности хостов завершена. Все хосты онлайн.")
        return

    logger.warning(
        "Scheduler: Проверка доступности хостов завершена. Обнаружены недоступные хосты."
    )

    if not bot:
        return

    # -------------------------------------------------------------------------
    # 3. Получаем ID администратора из настроек БД
    # -------------------------------------------------------------------------
    admin_id_setting = await asyncio.to_thread(database.get_setting, "admin_telegram_id")
    if not admin_id_setting:
        return
    admin_id = int(admin_id_setting)

    # -------------------------------------------------------------------------
    # 4. Отправка алертов о недоступных серверах в Telegram
    # -------------------------------------------------------------------------
    for host_name in failed_hosts:
        alert_text = (
            f"🔴 <b>Сервер {host_name} недоступен</b>\n\n" f"🕒 <b>Обнаружено:</b> {formatted_time}"
        )

        try:
            await bot.send_message(chat_id=admin_id, text=alert_text, parse_mode="HTML")
        except Exception as e:
            logger.error(f"Ошибка отправки алерта админу: {e}")
