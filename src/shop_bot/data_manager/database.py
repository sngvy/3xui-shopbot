"""Слой доступа к SQLite-базе бота: схема, миграции и CRUD-функции для всех сущностей."""

import calendar
import html
import json
import logging
import re
import sqlite3
import threading
import unicodedata
from datetime import datetime, timedelta
from pathlib import Path

logger = logging.getLogger(__name__)

PROJECT_ROOT = Path("/app/project")
DB_FILE = PROJECT_ROOT / "users.db"

keys_in_transition = set()

SETTINGS_CACHE = {}

_USERNAME_RE = re.compile(r"[A-Za-z0-9_]{4,32}")


def format_stored_username(
    username: str | None,
    user_id: int | None = None,
    linked: bool = True,
    escape_html: bool = True,
) -> str:
    """Безопасное отображение username, взятого из БД (не из live Update-объекта Telegram!).

    При регистрации, если у пользователя нет
    настоящего username, в это же поле сохраняется full_name (эмодзи, ФИО
    с пробелами и т.п. — см. register_user_if_not_exists) — поэтому перед
    тем как считать значение кликабельной ссылкой на профиль, проверяем его
    по формату реального Telegram-username.

    linked=True  -> для настоящих username отдаёт HTML-ссылку <a href='https://t.me/...'>.
    linked=False -> просто текст "@username" без разметки (для кнопок/списков).
    escape_html=False -> не экранировать фолбэк-имя (для текста кнопок Telegram,
    где HTML не парсится и &amp; показался бы буквально).
    Если это не похоже на username — отдаёт (экранированное, если нужно) имя
    + ID в скобках, если он передан, без ссылки: tg://user?id= ненадёжен и не
    резолвится для произвольных ID во многих клиентах.
    """
    raw = (username or "").strip().lstrip("@")
    if raw and _USERNAME_RE.fullmatch(raw):
        if linked:
            return f"<a href='https://t.me/{raw}'>@{raw}</a>"
        return f"@{raw}"
    if raw:
        label = html.escape(raw) if escape_html else raw
    else:
        label = "без ника"
    if user_id:
        return f"{label} ({user_id})"
    return label


# -------------------------------------------------------------------------
# Пул соединений SQLite: одно долгоживущее соединение на поток вместо
# открытия нового файлового соединения (+ повторного выполнения PRAGMA)
# на каждый вызов get_db_connection(). Безопасно, т. к.:
# • везде по коду соединение используется как `with get_db_connection() as conn:`
#   — это в sqlite3 управляет только commit/rollback транзакции, а не
#   закрытием соединения, так что переход на переиспользуемое соединение
#   не требует правок вызывающего кода;
# • каждый поток (в т.ч. каждый поток из ThreadPoolExecutor, куда уходят
#   все asyncio.to_thread-вызовы бота/scheduler'а, и каждый worker-поток
#   Flask) получает своё соединение — конкурентный доступ из разных потоков
#   к одному объекту sqlite3.Connection не допускается, поэтому shared-
#   соединение здесь не используется;
# • WAL-режим и так позволяет параллельные чтения без блокировок, а запись
#   сериализуется самим SQLite (busy_timeout=5000 подстрахует от кратких
#   конфликтов записи между потоками).
# -------------------------------------------------------------------------
_thread_local = threading.local()


def get_db_connection() -> sqlite3.Connection:
    """Возвращает потоко-локальное соединение с БД, пересоздавая его при необходимости."""
    conn = getattr(_thread_local, "conn", None)
    if conn is not None:
        try:
            # Дешёвая проверка, что соединение всё ещё живо (могло быть
            # закрыто явно где-то в коде, например в разовой миграции при
            # старте) — если нет, пересоздаём ниже
            conn.execute("SELECT 1")
            return conn
        except sqlite3.Error:
            conn = None

    conn = sqlite3.connect(DB_FILE, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL;")
    conn.execute("PRAGMA busy_timeout = 5000;")
    conn.execute("PRAGMA synchronous = NORMAL;")
    _thread_local.conn = conn
    return conn


def normalize_host_name(name: str | None) -> str:
    """Нормализует имя хоста: убирает пробелы по краям, очищает от BOM и нормализует Unicode-формат, сохраняя целостность эмодзи."""
    if not name:
        return ""

    # -------------------------------------------------------------------------
    # 1. Очищаем от явного мусора типа BOM-маркера, который бывает при копировании текста
    # -------------------------------------------------------------------------
    s = name.replace("\ufeff", "")

    # -------------------------------------------------------------------------
    # 2. Приводим Unicode к стандартной форме NFC
    # -------------------------------------------------------------------------
    s = unicodedata.normalize("NFC", s)

    # -------------------------------------------------------------------------
    # 3. Убираем обычные пробелы и неразрывные пробелы (\u00A0) по краям
    # -------------------------------------------------------------------------
    s = s.strip().strip("\u00a0")

    return s


def initialize_db():
    """Создаёт все таблицы схемы БД, если их ещё нет (идемпотентно)."""
    try:
        with get_db_connection() as conn:
            cursor = conn.cursor()
            cursor.execute("""
                CREATE TABLE IF NOT EXISTS users (
                    telegram_id INTEGER PRIMARY KEY, username TEXT, total_spent REAL DEFAULT 0,
                    total_months INTEGER DEFAULT 0, trial_used BOOLEAN DEFAULT 0,
                    agreed_to_terms BOOLEAN DEFAULT 0,
                    registration_date TIMESTAMP DEFAULT (datetime('now','localtime')),
                    is_banned BOOLEAN DEFAULT 0,
                    balance REAL DEFAULT 0,
                    referred_by INTEGER,
                    referral_balance REAL DEFAULT 0,
                    referral_balance_all REAL DEFAULT 0,
                    referral_start_bonus_received BOOLEAN DEFAULT 0
                )
            """)

            cursor.execute("""
                CREATE TABLE IF NOT EXISTS vpn_keys (
                    key_id INTEGER PRIMARY KEY AUTOINCREMENT,
                    user_id INTEGER NOT NULL,
                    host_name TEXT NOT NULL,
                    xui_client_uuid TEXT NOT NULL,
                    key_email TEXT NOT NULL UNIQUE,
                    expiry_date TIMESTAMP,
                    created_date TIMESTAMP DEFAULT (datetime('now','localtime')),
                    comment TEXT,
                    is_gift BOOLEAN DEFAULT 0
                )
            """)
            cursor.execute("""
                CREATE TABLE IF NOT EXISTS promo_codes (
                    promo_id INTEGER PRIMARY KEY AUTOINCREMENT,
                    code TEXT NOT NULL UNIQUE,
                    discount_percent REAL,
                    discount_amount REAL,
                    months_bonus INTEGER,
                    max_uses INTEGER,
                    used_count INTEGER DEFAULT 0,
                    active INTEGER NOT NULL DEFAULT 1,
                    valid_from TIMESTAMP,
                    valid_to TIMESTAMP,
                    comment TEXT,
                    created_at TIMESTAMP DEFAULT (datetime('now','localtime'))
                )
            """)
            cursor.execute("""
                CREATE TABLE IF NOT EXISTS bot_settings (
                    key TEXT PRIMARY KEY,
                    value TEXT
                )
            """)
            cursor.execute("""
                CREATE TABLE IF NOT EXISTS xui_hosts(
                    host_name TEXT NOT NULL,
                    host_url TEXT NOT NULL,
                    host_username TEXT NOT NULL,
                    host_pass TEXT NOT NULL,
                    host_inbound_id INTEGER NOT NULL,
                    subscription_url TEXT,
                    ssh_host TEXT,
                    ssh_port INTEGER,
                    ssh_user TEXT,
                    ssh_password TEXT,
                    ssh_key_path TEXT
                )
            """)
            cursor.execute("""
                CREATE TABLE IF NOT EXISTS plans (
                    plan_id INTEGER PRIMARY KEY AUTOINCREMENT,
                    host_name TEXT NOT NULL,
                    plan_name TEXT NOT NULL,
                    months INTEGER NOT NULL,
                    price REAL NOT NULL,
                    FOREIGN KEY (host_name) REFERENCES xui_hosts (host_name)
                )
            """)
            cursor.execute("""
                CREATE TABLE IF NOT EXISTS support_tickets (
                    ticket_id INTEGER PRIMARY KEY AUTOINCREMENT,
                    user_id INTEGER NOT NULL,
                    status TEXT NOT NULL DEFAULT 'open',
                    subject TEXT,
                    created_at TIMESTAMP DEFAULT (datetime('now','localtime')),
                    updated_at TIMESTAMP DEFAULT (datetime('now','localtime'))
                )
            """)
            cursor.execute("""
                CREATE TABLE IF NOT EXISTS support_messages (
                    message_id INTEGER PRIMARY KEY AUTOINCREMENT,
                    ticket_id INTEGER NOT NULL,
                    sender TEXT NOT NULL, -- 'user' | 'admin'
                    content TEXT NOT NULL,
                    media TEXT, -- JSON with Telegram file_id(s), type, caption, mime, size, etc.
                    created_at TIMESTAMP DEFAULT (datetime('now','localtime')),
                    FOREIGN KEY (ticket_id) REFERENCES support_tickets (ticket_id)
                )
            """)
            cursor.execute("""
                CREATE TABLE IF NOT EXISTS host_speedtests (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    host_name TEXT NOT NULL,
                    method TEXT NOT NULL, -- 'ssh' | 'net'
                    ping_ms REAL,
                    jitter_ms REAL,
                    download_mbps REAL,
                    upload_mbps REAL,
                    server_name TEXT,
                    server_id TEXT,
                    ok INTEGER NOT NULL DEFAULT 1,
                    error TEXT,
                    created_at TIMESTAMP DEFAULT (datetime('now','localtime'))
                )
            """)
            cursor.execute(
                "CREATE INDEX IF NOT EXISTS idx_host_speedtests_host_time ON host_speedtests(host_name, created_at DESC)"
            )

            # Таблица для метрик ресурсов
            cursor.execute("""
                CREATE TABLE IF NOT EXISTS resource_metrics (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    scope TEXT NOT NULL,                -- 'local' | 'host' | 'target'
                    object_name TEXT NOT NULL,          -- 'panel' | host_name | target_name
                    cpu_percent REAL,
                    mem_percent REAL,
                    disk_percent REAL,
                    load1 REAL,
                    net_bytes_sent INTEGER,
                    net_bytes_recv INTEGER,
                    raw_json TEXT,
                    created_at TIMESTAMP DEFAULT (datetime('now','localtime'))
                )
            """)
            cursor.execute(
                "CREATE INDEX IF NOT EXISTS idx_resource_metrics_scope_time ON resource_metrics(scope, object_name, created_at DESC)"
            )

            # Таблица для конфигураций кнопок
            cursor.execute("""
                CREATE TABLE IF NOT EXISTS button_configs (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    menu_type TEXT NOT NULL DEFAULT 'main_menu',
                    button_id TEXT NOT NULL,
                    text TEXT NOT NULL,
                    callback_data TEXT,
                    url TEXT,
                    row_position INTEGER DEFAULT 0,
                    column_position INTEGER DEFAULT 0,
                    button_width INTEGER DEFAULT 1,
                    sort_order INTEGER DEFAULT 0,
                    is_active BOOLEAN DEFAULT 1,
                    created_at TIMESTAMP DEFAULT (datetime('now','localtime')),
                    updated_at TIMESTAMP DEFAULT (datetime('now','localtime')),
                    UNIQUE(menu_type, button_id)
                )
            """)
            cursor.execute(
                "CREATE INDEX IF NOT EXISTS idx_button_configs_menu_type ON button_configs(menu_type, sort_order)"
            )

            default_settings = {
                "panel_login": "admin",
                "panel_password": "admin",
                "about_text": None,
                "terms_url": None,
                "privacy_url": None,
                "support_user": None,
                "support_text": None,
                # Editable content
                "main_menu_text": None,
                "howto_android_text": None,
                "howto_ios_text": None,
                "howto_windows_text": None,
                "howto_linux_text": None,
                # Button texts (customizable)
                "btn_try": "🎁 Попробовать бесплатно",
                "btn_profile": "👤 Мой профиль",
                "btn_my_keys": "🔑 Мои подписки ({count})",
                "btn_buy_key": "💳 Купить подписку",
                "btn_top_up": "➕ Пополнить баланс",
                "btn_referral": "🤝 Реферальная программа",
                "btn_support": "🆘 Поддержка",
                "btn_about": "ℹ️ О проекте",
                "btn_howto": "❓ Как использовать",
                "btn_speed": "⚡ Тест скорости",
                "btn_admin": "⚙️ Админка",
                "btn_back_to_menu": "⬅️ Назад в меню",
                "btn_back": "⬅️ Назад",
                "btn_back_to_plans": "⬅️ Назад к тарифам",
                "btn_back_to_key": "⬅️ Назад к подписке",
                "btn_back_to_keys": "⬅️ Назад к списку подписок",
                "btn_extend_key": "➕ Продлить эту подписку",
                "btn_show_qr": "📱 Показать QR-код",
                "btn_instruction": "📖 Инструкция",
                "btn_switch_server": "🌍 Сменить сервер",
                "btn_skip_email": "➡️ Продолжить без почты",
                "btn_go_to_payment": "Перейти к оплате",
                "btn_check_payment": "✅ Проверить оплату",
                "btn_pay_with_balance": "💼 Оплатить с баланса",
                # About/links
                "btn_channel": "📰 Наш канал",
                "btn_terms": "📄 Условия использования",
                "btn_privacy": "🔒 Политика конфиденциальности",
                # Howto platform buttons
                "btn_howto_android": "👾 Android",
                "btn_howto_ios": "🍏 iOS",
                "btn_howto_windows": "💻 Windows",
                "btn_howto_linux": "🐧 Linux",
                # Support menu
                "btn_support_open": "🆘 Открыть поддержку",
                "btn_support_new_ticket": "✍️ Новое обращение",
                "btn_support_my_tickets": "📨 Мои обращения",
                "btn_support_external": "🆘 Внешняя поддержка",
                "channel_url": None,
                "force_subscription": "true",
                "receipt_email": "example@example.com",
                "telegram_bot_token": None,
                "telegram_bot_username": None,
                "trial_enabled": "true",
                "trial_duration_days": "3",
                "enable_referrals": "true",
                "referral_percentage": "10",
                "referral_discount": "5",
                "minimum_withdrawal": "100",
                "admin_telegram_id": None,
                "admin_telegram_ids": None,
                "yookassa_shop_id": None,
                "yookassa_secret_key": None,
                "sbp_enabled": "false",
                "cryptobot_token": None,
                "heleket_merchant_id": None,
                "heleket_api_key": None,
                "domain": None,
                "ton_wallet_address": None,
                "tonapi_key": None,
                "support_forum_chat_id": None,
                # Referral program advanced
                "enable_fixed_referral_bonus": "false",
                "fixed_referral_bonus_amount": "50",
                "referral_reward_type": "percent_purchase",  # percent_purchase | fixed_purchase | fixed_start_referrer
                "referral_on_start_referrer_amount": "20",
                # Backups
                "backup_interval_days": "1",
                # Telegram Stars payments
                "stars_enabled": "false",
                # Сколько звёзд списывать за 1 RUB (напр., 1.5 звезды за 1 рубль)
                "stars_per_rub": "1",
                # Заголовок/описание инвойсов Stars
                "stars_title": "VPN подписка",
                "stars_description": "Оплата в Telegram Stars",
                # YooMoney separate payments
                "yoomoney_enabled": "false",
                "yoomoney_wallet": None,
                "yoomoney_api_token": None,
                "yoomoney_client_id": None,
                "yoomoney_client_secret": None,
                "yoomoney_redirect_uri": None,
            }
            run_migration()
            for key, value in default_settings.items():
                cursor.execute(
                    "INSERT OR IGNORE INTO bot_settings (key, value) VALUES (?, ?)",
                    (key, value),
                )
            conn.commit()

            # Reset and migrate existing button configurations with correct layout
            reset_button_migration()
            migrate_existing_buttons()

            # Clean up any duplicate buttons
            cleanup_duplicate_buttons()

            # Миграция: добавить колонку created_date в vpn_keys если её нет
            try:
                cursor.execute("PRAGMA table_info(vpn_keys)")
                columns = [row[1] for row in cursor.fetchall()]
                if "created_date" not in columns:
                    cursor.execute(
                        "ALTER TABLE vpn_keys ADD COLUMN created_date TIMESTAMP DEFAULT (datetime('now','localtime'))"
                    )
                    logging.info("Добавлена колонка created_date в таблицу vpn_keys")
            except Exception as e:
                logging.warning(f"Ошибка при добавлении колонки created_date: {e}")

            # Создаем индекс для ускорения поиска ключей по имени хоста
            cursor.execute(
                "CREATE INDEX IF NOT EXISTS idx_vpn_keys_host_name ON vpn_keys(host_name);"
            )

            # Создаем индекс для ускорения поиска тарифов по имени хоста
            cursor.execute("CREATE INDEX IF NOT EXISTS idx_plans_host_name ON plans(host_name);")

            # Создаем индекс для самой таблицы хостов (поможет при проверках уникальности)
            cursor.execute(
                "CREATE INDEX IF NOT EXISTS idx_xui_hosts_host_name ON xui_hosts(host_name);"
            )

            # Дополнительные индексы для ускорения работы БД
            cursor.execute("CREATE INDEX IF NOT EXISTS idx_vpn_keys_user_id ON vpn_keys(user_id);")
            cursor.execute(
                "CREATE INDEX IF NOT EXISTS idx_transactions_payment_id ON transactions(payment_id);"
            )
            cursor.execute(
                "CREATE INDEX IF NOT EXISTS idx_promo_code_usages_code_user ON promo_code_usages(code, user_id);"
            )

            logging.info("База данных успешно инициализирована.")
    except sqlite3.Error as e:
        logging.error(f"Ошибка базы данных при инициализации: {e}")


# --- Promo codes API (unified) ---
PROMO_COLUMNS_CACHE = None


def _promo_columns(conn: sqlite3.Connection) -> set[str]:
    global PROMO_COLUMNS_CACHE
    # Если кэш пустой — считываем структуру с диска
    if PROMO_COLUMNS_CACHE is None:
        cursor = conn.cursor()
        cursor.execute("PRAGMA table_info(promo_codes)")
        PROMO_COLUMNS_CACHE = {row[1] for row in cursor.fetchall()}
        logger.info("Структура таблицы промокодов успешно закэширована.")

    return PROMO_COLUMNS_CACHE


def create_promo_code(
    code: str,
    *,
    discount_percent: float | None = None,
    discount_amount: float | None = None,
    usage_limit_total: int | None = None,
    usage_limit_per_user: int | None = None,
    valid_from: datetime | None = None,
    valid_until: datetime | None = None,
    created_by: int | None = None,  # Ignored in 3xui schema
    description: str | None = None,
) -> bool:
    """Создаёт новый промокод с заданными параметрами скидки и лимитами."""
    code_s = (code or "").strip().upper()
    if not code_s:
        raise ValueError("code is required")
    if (discount_percent or 0) <= 0 and (discount_amount or 0) <= 0:
        raise ValueError("discount must be positive")
    try:
        with get_db_connection() as conn:
            cursor = conn.cursor()
            cols = _promo_columns(conn)
            # Prefer valid_to in this project; migration didn't add valid_until
            vf = valid_from.isoformat() if isinstance(valid_from, datetime) else valid_from
            vu = valid_until.isoformat() if isinstance(valid_until, datetime) else valid_until
            fields = [
                ("code", code_s),
                (
                    "discount_percent",
                    float(discount_percent) if discount_percent is not None else None,
                ),
                (
                    "discount_amount",
                    float(discount_amount) if discount_amount is not None else None,
                ),
                ("usage_limit_total", usage_limit_total),
                ("usage_limit_per_user", usage_limit_per_user),
                ("valid_from", vf),
                ("description", description),
            ]
            if "valid_until" in cols:
                fields.append(("valid_until", vu))
            else:
                fields.append(("valid_to", vu))
            if "created_at" in cols:
                # Явно, не полагаясь на DEFAULT колонки (на существующей
                # таблице он остался старым UTC-шным CURRENT_TIMESTAMP,
                # CREATE TABLE IF NOT EXISTS его не обновляет).
                fields.append(("created_at", datetime.now()))
            columns = ", ".join([f for f, _ in fields])
            placeholders = ", ".join(["?" for _ in fields])
            values = [v for _, v in fields]
            cursor.execute(
                f"INSERT INTO promo_codes ({columns}) VALUES ({placeholders})",
                values,
            )
            conn.commit()
            return True
    except sqlite3.IntegrityError:
        return False
    except sqlite3.Error as e:
        logging.error(f"Ошибка при создании промокода: {e}")
        return False


def get_promo_code(code: str) -> dict | None:
    """Возвращает промокод по его коду или None, если не найден."""
    code_s = (code or "").strip().upper()
    if not code_s:
        return None
    try:
        with get_db_connection() as conn:
            conn.row_factory = sqlite3.Row
            cursor = conn.cursor()
            cursor.execute("SELECT * FROM promo_codes WHERE code = ?", (code_s,))
            row = cursor.fetchone()
            return dict(row) if row else None
    except sqlite3.Error as e:
        logging.error(f"Ошибка при получении промокода: {e}")
        return None


def list_promo_codes(include_inactive: bool = True) -> list[dict]:
    """Возвращает список промокодов (по умолчанию включая неактивные)."""
    query = "SELECT * FROM promo_codes"
    if not include_inactive:
        # Use is_active if present, else active
        query += " WHERE COALESCE(is_active, active, 1) = 1"
    query += " ORDER BY created_at DESC"
    try:
        with get_db_connection() as conn:
            conn.row_factory = sqlite3.Row
            cursor = conn.cursor()
            cursor.execute(query)
            return [dict(r) for r in cursor.fetchall()]
    except sqlite3.Error as e:
        logging.error(f"Ошибка при получении списка промокодов: {e}")
        return []


def check_promo_code_available(code: str, user_id: int) -> tuple[dict | None, str | None]:
    """Проверяет, может ли пользователь применить промокод (найден/активен/не исчерпан)."""
    code_s = (code or "").strip().upper()
    if not code_s:
        return None, "empty_code"
    user_id_i = int(user_id)

    try:
        with get_db_connection() as conn:
            conn.row_factory = sqlite3.Row
            cursor = conn.cursor()
            cols = _promo_columns(conn)

            used_expr = (
                "COALESCE(used_total, used_count, 0)"
                if "used_total" in cols and "used_count" in cols
                else (
                    "COALESCE(used_total, 0)"
                    if "used_total" in cols
                    else ("COALESCE(used_count, 0)" if "used_count" in cols else "0")
                )
            )
            vu_expr = "valid_until" if "valid_until" in cols else "valid_to"
            active_expr = "is_active" if "is_active" in cols else "active"

            query = f"""
                SELECT code, discount_percent, discount_amount,
                       usage_limit_total, usage_limit_per_user,
                       {used_expr} AS used_total,
                       valid_from, {vu_expr} AS valid_until,
                       {active_expr} AS is_active
                FROM promo_codes
                WHERE code = ?
            """
            cursor.execute(query, (code_s,))
            row = cursor.fetchone()
            if row is None:
                return None, "not_found"

            promo = dict(row)
            if not promo.get("is_active"):
                return None, "inactive"

            now = datetime.now()
            vf = promo.get("valid_from")

            # -------------------------------------------------------------------------
            # 1. Проверка даты начала действия промокода (valid_from)
            # -------------------------------------------------------------------------
            if vf:
                try:
                    if datetime.fromisoformat(str(vf)) > now:
                        return None, "not_started"
                except (ValueError, TypeError) as e:
                    logging.error(f"Ошибка парсинга даты valid_from для промокода '{code_s}': {e}")
                    return None, "invalid_date_format"

            vu = promo.get("valid_until")

            # -------------------------------------------------------------------------
            # 2. Проверка даты окончания действия промокода (valid_until / valid_to)
            # -------------------------------------------------------------------------
            if vu:
                try:
                    if datetime.fromisoformat(str(vu)) < now:
                        return None, "expired"
                except (ValueError, TypeError) as e:
                    logging.error(f"Ошибка парсинга даты valid_until для промокода '{code_s}': {e}")
                    return None, "invalid_date_format"

            limit_total = promo.get("usage_limit_total")
            used_total = promo.get("used_total") or 0
            if limit_total and used_total >= limit_total:
                return None, "total_limit_reached"

            per_user = promo.get("usage_limit_per_user")
            if per_user:
                cursor.execute(
                    "SELECT COUNT(1) FROM promo_code_usages WHERE code = ? AND user_id = ?",
                    (code_s, user_id_i),
                )
                count = cursor.fetchone()[0]
                if count >= per_user:
                    return None, "user_limit_reached"

            return promo, None

    except sqlite3.Error as e:
        logging.error(f"Ошибка при проверке доступности промокода: {e}")
        return None, "db_error"


def update_promo_code_status(code: str, *, is_active: bool | None = None) -> bool:
    """Включает или отключает промокод (is_active)."""
    code_s = (code or "").strip().upper()
    if not code_s:
        return False
    sets = []
    params: list = []
    if is_active is not None:
        sets.append("is_active = ?")
        params.append(1 if is_active else 0)
        # Update legacy column too for compatibility
        sets.append("active = ?")
        params.append(1 if is_active else 0)
    if not sets:
        return False
    params.append(code_s)
    try:
        with get_db_connection() as conn:
            cursor = conn.cursor()
            cursor.execute(f"UPDATE promo_codes SET {', '.join(sets)} WHERE code = ?", params)
            conn.commit()
            return cursor.rowcount > 0
    except sqlite3.Error as e:
        logging.error(f"Ошибка при обновлении статуса промокода: {e}")
        return False


def redeem_promo_code(
    code: str,
    user_id: int,
    *,
    applied_amount: float,
    order_id: str | None = None,
) -> dict | None:
    """Применяет промокод к покупке: проверяет лимиты и фиксирует использование."""
    code_s = (code or "").strip().upper()
    if not code_s:
        return None
    user_id_i = int(user_id)
    applied_amount_f = float(applied_amount)

    try:
        with get_db_connection() as conn:
            conn.execute("BEGIN IMMEDIATE TRANSACTION;")
            conn.row_factory = sqlite3.Row
            cursor = conn.cursor()
            cols = _promo_columns(conn)

            used_expr = (
                "COALESCE(used_total, used_count, 0)"
                if "used_total" in cols and "used_count" in cols
                else (
                    "COALESCE(used_total, 0)"
                    if "used_total" in cols
                    else ("COALESCE(used_count, 0)" if "used_count" in cols else "0")
                )
            )
            vu_expr = "valid_until" if "valid_until" in cols else "valid_to"
            active_expr = "is_active" if "is_active" in cols else "active"

            query = f"""
                SELECT code, discount_percent, discount_amount,
                       usage_limit_total, usage_limit_per_user,
                       {used_expr} AS used_total,
                       valid_from, {vu_expr} AS valid_until,
                       {active_expr} AS is_active
                FROM promo_codes
                WHERE code = ?
            """
            cursor.execute(query, (code_s,))
            row = cursor.fetchone()
            if row is None:
                return None

            promo = dict(row)
            if not promo.get("is_active"):
                return None

            now = datetime.now()
            vf = promo.get("valid_from")

            # -------------------------------------------------------------------------
            # 1. Проверка даты начала действия промокода (valid_from)
            # -------------------------------------------------------------------------
            if vf:
                try:
                    if datetime.fromisoformat(str(vf)) > now:
                        return None
                except (ValueError, TypeError) as e:
                    logging.error(
                        f"Ошибка парсинга даты valid_from при активации промокода '{code_s}': {e}"
                    )
                    return None

            vu = promo.get("valid_until")

            # -------------------------------------------------------------------------
            # 2. Проверка даты окончания действия промокода (valid_until / valid_to)
            # -------------------------------------------------------------------------
            if vu:
                try:
                    if datetime.fromisoformat(str(vu)) < now:
                        return None
                except (ValueError, TypeError) as e:
                    logging.error(
                        f"Ошибка парсинга даты valid_until при активации промокода '{code_s}': {e}"
                    )
                    return None

            limit_total = promo.get("usage_limit_total")
            used_total = promo.get("used_total") or 0
            if limit_total and used_total >= limit_total:
                return None

            per_user = promo.get("usage_limit_per_user")
            if per_user:
                cursor.execute(
                    "SELECT COUNT(1) FROM promo_code_usages WHERE code = ? AND user_id = ?",
                    (code_s, user_id_i),
                )
                count = cursor.fetchone()[0]
                if count >= per_user:
                    return None
            else:
                count = None

            # Redeem
            cursor.execute(
                """
                INSERT INTO promo_code_usages (code, user_id, applied_amount, order_id, used_at)
                VALUES (?, ?, ?, ?, ?)
                """,
                (code_s, user_id_i, applied_amount_f, order_id, datetime.now()),
            )

            # Динамически собираем инкремент счетчиков под доступные колонки
            update_parts = []
            if "used_total" in cols:
                update_parts.append("used_total = COALESCE(used_total, 0) + 1")
            if "used_count" in cols:
                update_parts.append("used_count = COALESCE(used_count, 0) + 1")

            if update_parts:
                update_query = f"UPDATE promo_codes SET {', '.join(update_parts)} WHERE code = ?"
                cursor.execute(update_query, (code_s,))

            conn.commit()

            promo["used_total"] = (used_total or 0) + 1
            promo["redeemed_by"] = user_id_i
            promo["applied_amount"] = applied_amount_f
            promo["order_id"] = order_id

            if per_user:
                promo["user_usage_count"] = (count or 0) + 1
            else:
                promo["user_usage_count"] = None

            conn.commit()
            return promo

    except sqlite3.Error as e:
        logging.error(f"Ошибка при активации промокода: {e}")
        return None


def run_migration():
    """Выполняет одноразовую миграцию схемы БД при старте приложения."""
    if not DB_FILE.exists():
        logging.error("Файл базы данных users.db не найден. Мигрировать нечего.")
        return

    logging.info(f"Начинаю миграцию базы данных: {DB_FILE}")

    try:
        conn = get_db_connection()
        cursor = conn.cursor()

        logging.info("Миграция таблицы 'users' ...")

        cursor.execute("PRAGMA table_info(users)")
        columns = [row[1] for row in cursor.fetchall()]

        if "referred_by" not in columns:
            cursor.execute("ALTER TABLE users ADD COLUMN referred_by INTEGER")
            logging.info(" -> Столбец 'referred_by' успешно добавлен.")
        else:
            logging.info(" -> Столбец 'referred_by' уже существует.")

        if "balance" not in columns:
            cursor.execute("ALTER TABLE users ADD COLUMN balance REAL DEFAULT 0")
            logging.info(" -> Столбец 'balance' успешно добавлен.")
        else:
            logging.info(" -> Столбец 'balance' уже существует.")

        if "referral_balance" not in columns:
            cursor.execute("ALTER TABLE users ADD COLUMN referral_balance REAL DEFAULT 0")
            logging.info(" -> Столбец 'referral_balance' успешно добавлен.")
        else:
            logging.info(" -> Столбец 'referral_balance' уже существует.")

        if "referral_balance_all" not in columns:
            cursor.execute("ALTER TABLE users ADD COLUMN referral_balance_all REAL DEFAULT 0")
            logging.info(" -> Столбец 'referral_balance_all' успешно добавлен.")
        else:
            logging.info(" -> Столбец 'referral_balance_all' уже существует.")

        if "referral_start_bonus_received" not in columns:
            cursor.execute(
                "ALTER TABLE users ADD COLUMN referral_start_bonus_received BOOLEAN DEFAULT 0"
            )
            logging.info(" -> Столбец 'referral_start_bonus_received' успешно добавлен.")
        else:
            logging.info(" -> Столбец 'referral_start_bonus_received' уже существует.")

        logging.info("Таблица 'users' успешно обновлена.")

        # Индексы для ускорения фильтрации/сортировки пользователей
        try:
            cursor.execute("CREATE INDEX IF NOT EXISTS idx_users_username ON users(username)")
            cursor.execute(
                "CREATE INDEX IF NOT EXISTS idx_users_reg_date ON users(registration_date)"
            )
            conn.commit()
            logging.info(" -> Индексы для 'users' созданы/проверены.")
        except sqlite3.Error as e:
            logging.warning(f" -> Не удалось создать индексы для 'users': {e}")

        logging.info("Миграция таблицы 'transactions' ...")

        cursor.execute("SELECT name FROM sqlite_master WHERE type='table' AND name='transactions'")
        table_exists = cursor.fetchone()

        if table_exists:
            cursor.execute("PRAGMA table_info(transactions)")
            trans_columns = [row[1] for row in cursor.fetchall()]

            if (
                "payment_id" in trans_columns
                and "status" in trans_columns
                and "username" in trans_columns
            ):
                logging.info(
                    "Таблица 'transactions' уже имеет новую структуру. Миграция не требуется."
                )
            else:
                backup_name = f"transactions_backup_{datetime.now().strftime('%Y%m%d%H%M%S')}"
                logging.warning(
                    f"Обнаружена старая структура таблицы 'transactions'. Переименовываю в '{backup_name}' ..."
                )
                cursor.execute(f"ALTER TABLE transactions RENAME TO {backup_name}")

                logging.info("Создаю новую таблицу 'transactions' с корректной структурой ...")
                create_new_transactions_table(cursor)
                logging.info(
                    "Новая таблица 'transactions' успешно создана. Старые данные сохранены."
                )
        else:
            logging.info("Таблица 'transactions' не найдена. Создаю новую ...")
            create_new_transactions_table(cursor)
            logging.info("Новая таблица 'transactions' успешно создана.")

        logging.info("Миграция таблицы 'support_tickets' ...")
        cursor = conn.cursor()
        cursor.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name='support_tickets'"
        )
        table_exists = cursor.fetchone()
        if table_exists:
            cursor.execute("PRAGMA table_info(support_tickets)")
            st_columns = [row[1] for row in cursor.fetchall()]
            if "forum_chat_id" not in st_columns:
                cursor.execute("ALTER TABLE support_tickets ADD COLUMN forum_chat_id TEXT")
                logging.info(" -> Столбец 'forum_chat_id' успешно добавлен в 'support_tickets'.")
            else:
                logging.info(" -> Столбец 'forum_chat_id' уже существует в 'support_tickets'.")
            if "message_thread_id" not in st_columns:
                cursor.execute("ALTER TABLE support_tickets ADD COLUMN message_thread_id INTEGER")
                logging.info(
                    " -> Столбец 'message_thread_id' успешно добавлен в 'support_tickets'."
                )
            else:
                logging.info(" -> Столбец 'message_thread_id' уже существует в 'support_tickets'.")
        else:
            logging.warning("Таблица 'support_tickets' не найдена, пропускаю её миграцию.")

        conn.commit()

        logging.info("Миграция таблицы 'support_messages' ...")
        cursor = conn.cursor()
        cursor.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name='support_messages'"
        )
        table_exists = cursor.fetchone()
        if table_exists:
            cursor.execute("PRAGMA table_info(support_messages)")
            sm_columns = [row[1] for row in cursor.fetchall()]
            if "media" not in sm_columns:
                cursor.execute("ALTER TABLE support_messages ADD COLUMN media TEXT")
                logging.info(" -> Столбец 'media' успешно добавлен в 'support_messages'.")
            else:
                logging.info(" -> Столбец 'media' уже существует в 'support_messages'.")
        else:
            logging.warning("Таблица 'support_messages' не найдена, пропускаю её миграцию.")

        logging.info("Миграция таблицы 'xui_hosts' ...")
        cursor = conn.cursor()
        cursor.execute("SELECT name FROM sqlite_master WHERE type='table' AND name='xui_hosts'")
        table_exists = cursor.fetchone()
        if table_exists:
            cursor.execute("PRAGMA table_info(xui_hosts)")
            xh_columns = [row[1] for row in cursor.fetchall()]
            if "subscription_url" not in xh_columns:
                cursor.execute("ALTER TABLE xui_hosts ADD COLUMN subscription_url TEXT")
                logging.info(" -> Столбец 'subscription_url' успешно добавлен в 'xui_hosts'.")
            else:
                logging.info(" -> Столбец 'subscription_url' уже существует в 'xui_hosts'.")
            # SSH settings for speedtests (optional)
            if "ssh_host" not in xh_columns:
                cursor.execute("ALTER TABLE xui_hosts ADD COLUMN ssh_host TEXT")
                logging.info(" -> Столбец 'ssh_host' успешно добавлен в 'xui_hosts'.")
            if "ssh_port" not in xh_columns:
                cursor.execute("ALTER TABLE xui_hosts ADD COLUMN ssh_port INTEGER")
                logging.info(" -> Столбец 'ssh_port' успешно добавлен в 'xui_hosts'.")
            if "ssh_user" not in xh_columns:
                cursor.execute("ALTER TABLE xui_hosts ADD COLUMN ssh_user TEXT")
                logging.info(" -> Столбец 'ssh_user' успешно добавлен в 'xui_hosts'.")
            if "ssh_password" not in xh_columns:
                cursor.execute("ALTER TABLE xui_hosts ADD COLUMN ssh_password TEXT")
                logging.info(" -> Столбец 'ssh_password' успешно добавлен в 'xui_hosts'.")
            if "ssh_key_path" not in xh_columns:
                cursor.execute("ALTER TABLE xui_hosts ADD COLUMN ssh_key_path TEXT")
                logging.info(" -> Столбец 'ssh_key_path' успешно добавлен в 'xui_hosts'.")
            # Clean up host_name values from invisible spaces and trim
            try:
                cursor.execute("""
                    UPDATE xui_hosts
                    SET host_name = TRIM(
                        REPLACE(REPLACE(REPLACE(REPLACE(REPLACE(host_name,
                            char(160), ''),      -- NBSP
                            char(8203), ''),     -- ZERO WIDTH SPACE
                            char(8204), ''),     -- ZWNJ
                            char(8205), ''),     -- ZWJ
                            char(65279), ''      -- BOM
                        )
                    )
                    """)
                conn.commit()
                logging.info(" -> Нормализованы существующие значения host_name в 'xui_hosts'.")
            except Exception as e:
                logging.warning(
                    f" -> Не удалось нормализовать существующие значения host_name: {e}"
                )
        else:
            logging.warning("Таблица 'xui_hosts' не найдена, пропускаю её миграцию.")
        # Create table for host speedtests
        try:
            cursor = conn.cursor()
            cursor.execute("""
                CREATE TABLE IF NOT EXISTS host_speedtests (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    host_name TEXT NOT NULL,
                    method TEXT NOT NULL, -- 'ssh' | 'net'
                    ping_ms REAL,
                    jitter_ms REAL,
                    download_mbps REAL,
                    upload_mbps REAL,
                    server_name TEXT,
                    server_id TEXT,
                    ok INTEGER NOT NULL DEFAULT 1,
                    error TEXT,
                    created_at TIMESTAMP DEFAULT (datetime('now','localtime'))
                )
                """)
            cursor.execute(
                "CREATE INDEX IF NOT EXISTS idx_host_speedtests_host_time ON host_speedtests(host_name, created_at DESC)"
            )
            conn.commit()
            logging.info("Таблица 'host_speedtests' готова к использованию.")
        except sqlite3.Error as e:
            logging.error(f"Не удалось создать 'host_speedtests': {e}")

        # Создание таблицы с историей бэкапов
        try:
            cursor = conn.cursor()
            cursor.execute("""
                CREATE TABLE IF NOT EXISTS backup_history (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    filename TEXT,
                    created_at TIMESTAMP DEFAULT (datetime('now','localtime'))
                )
                """)
            cursor.execute(
                "CREATE INDEX IF NOT EXISTS idx_backup_history_time ON backup_history(created_at DESC)"
            )
            conn.commit()
            logging.info("Таблица 'backup_history' готова к использованию.")
        except sqlite3.Error as e:
            logging.error(f"Не удалось создать 'backup_history': {e}")

        # Create table for host resource metrics (monitor history)
        try:
            cursor = conn.cursor()
            cursor.execute("""
                CREATE TABLE IF NOT EXISTS host_metrics (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    host_name TEXT NOT NULL,
                    cpu_percent REAL,
                    mem_percent REAL,
                    mem_used INTEGER,
                    mem_total INTEGER,
                    disk_percent REAL,
                    disk_used INTEGER,
                    disk_total INTEGER,
                    load1 REAL,
                    load5 REAL,
                    load15 REAL,
                    uptime_seconds REAL,
                    ok INTEGER NOT NULL DEFAULT 1,
                    error TEXT,
                    created_at TIMESTAMP DEFAULT (datetime('now','localtime'))
                )
                """)
            cursor.execute(
                "CREATE INDEX IF NOT EXISTS idx_host_metrics_host_time ON host_metrics(host_name, created_at DESC)"
            )
            conn.commit()
            logging.info("Таблица 'host_metrics' готова к использованию.")
        except sqlite3.Error as e:
            logging.error(f"Не удалось создать 'host_metrics': {e}")

        # Ensure extra columns for standalone keys and promo table
        try:
            cursor = conn.cursor()
            cursor.execute("PRAGMA table_info(vpn_keys)")
            vk_cols = [row[1] for row in cursor.fetchall()]
            if "comment" not in vk_cols:
                cursor.execute("ALTER TABLE vpn_keys ADD COLUMN comment TEXT")
                logging.info(" -> Добавлен столбец 'comment' в 'vpn_keys'.")
            if "is_gift" not in vk_cols:
                cursor.execute("ALTER TABLE vpn_keys ADD COLUMN is_gift BOOLEAN DEFAULT 0")
                logging.info(" -> Добавлен столбец 'is_gift' в 'vpn_keys'.")
            if "auto_renew_enabled" not in vk_cols:
                cursor.execute(
                    "ALTER TABLE vpn_keys ADD COLUMN auto_renew_enabled INTEGER DEFAULT 0"
                )
                logging.info(" -> Добавлен столбец 'auto_renew_enabled' в 'vpn_keys'.")
            if "last_plan_id" not in vk_cols:
                # Не FOREIGN KEY намеренно — тариф могут удалить/поменять цену
                # позже, а автопродление по старой уже недоступной записи
                # просто должно тихо не найти план и пропустить ключ (см.
                # get_keys_due_for_auto_renewal), а не падать на constraint
                cursor.execute("ALTER TABLE vpn_keys ADD COLUMN last_plan_id INTEGER")
                logging.info(" -> Добавлен столбец 'last_plan_id' в 'vpn_keys'.")
            conn.commit()
        except sqlite3.Error as e:
            logging.error(f"Не удалось мигрировать 'vpn_keys': {e}")

        # Ensure promo code tables and columns (new flexible scheme)
        try:
            cursor = conn.cursor()
            # Base table (create if not exists; old columns may exist — we'll extend with new ones)
            cursor.execute("""
                CREATE TABLE IF NOT EXISTS promo_codes (
                    promo_id INTEGER PRIMARY KEY AUTOINCREMENT,
                    code TEXT NOT NULL UNIQUE,
                    discount_percent REAL,
                    discount_amount REAL,
                    -- legacy names below may exist in older DBs
                    months_bonus INTEGER,
                    max_uses INTEGER,
                    used_count INTEGER DEFAULT 0,
                    active INTEGER NOT NULL DEFAULT 1,
                    valid_from TIMESTAMP,
                    valid_to TIMESTAMP,
                    comment TEXT,
                    created_at TIMESTAMP DEFAULT (datetime('now','localtime'))
                )
                """)
            # Ensure new columns used by unified promo API
            try:
                cursor.execute("PRAGMA table_info(promo_codes)")
                cols = {row[1] for row in cursor.fetchall()}
                # New canonical columns
                if "usage_limit_total" not in cols:
                    cursor.execute("ALTER TABLE promo_codes ADD COLUMN usage_limit_total INTEGER")
                if "usage_limit_per_user" not in cols:
                    cursor.execute(
                        "ALTER TABLE promo_codes ADD COLUMN usage_limit_per_user INTEGER"
                    )
                if "used_total" not in cols:
                    cursor.execute(
                        "ALTER TABLE promo_codes ADD COLUMN used_total INTEGER DEFAULT 0"
                    )
                if "is_active" not in cols:
                    cursor.execute("ALTER TABLE promo_codes ADD COLUMN is_active INTEGER DEFAULT 1")
                if "description" not in cols:
                    cursor.execute("ALTER TABLE promo_codes ADD COLUMN description TEXT")
                if "valid_until" not in cols and "valid_to" in cols:
                    # Keep using valid_to for backward compatibility; unified API will read either
                    pass
            except Exception as e:
                logging.warning(f"Предупреждение при миграции таблицы promo_codes (колонки): {e}")

            # Mirror legacy counters to new ones if new ones are zero
            try:
                # If used_total is null but used_count exists, initialize used_total from used_count
                cursor.execute(
                    "UPDATE promo_codes SET used_total = COALESCE(used_total, 0) + COALESCE(used_count, 0) WHERE used_total IS NULL"
                )
            except Exception:
                pass

            # Usages table
            cursor.execute("""
                CREATE TABLE IF NOT EXISTS promo_code_usages (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    code TEXT NOT NULL,
                    user_id INTEGER NOT NULL,
                    applied_amount REAL NOT NULL,
                    order_id TEXT,
                    used_at TIMESTAMP DEFAULT (datetime('now','localtime'))
                )
                """)
            conn.commit()
        except sqlite3.Error as e:
            logging.error(f"Не удалось подготовить таблицы промокодов: {e}")

        # Явный conn.close() здесь больше не нужен и вреден

        logging.info("--- Миграция базы данных успешно завершена! ---")

    except sqlite3.Error as e:
        logging.error(f"Ошибка во время миграции: {e}")


def create_new_transactions_table(cursor: sqlite3.Cursor):
    """Создаёт новую таблицу transactions в рамках миграции схемы."""
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS transactions (
            username TEXT,
            transaction_id INTEGER PRIMARY KEY AUTOINCREMENT,
            payment_id TEXT UNIQUE NOT NULL,
            user_id INTEGER NOT NULL,
            status TEXT NOT NULL,
            amount_rub REAL NOT NULL,
            amount_currency REAL,
            currency_name TEXT,
            payment_method TEXT,
            metadata TEXT,
            created_date TIMESTAMP DEFAULT (datetime('now','localtime'))
        )
    """)


def create_host(
    name: str,
    url: str,
    user: str,
    passwd: str,
    inbound: int,
    subscription_url: str | None = None,
):
    """Добавляет новый хост (сервер X-UI) в БД."""
    try:
        name = normalize_host_name(name)
        url = (url or "").strip()
        user = (user or "").strip()
        passwd = passwd or ""
        inbound = int(inbound)
        subscription_url = subscription_url or None

        with get_db_connection() as conn:
            cursor = conn.cursor()
            cursor.execute(
                """
                INSERT INTO xui_hosts
                (host_name, host_url, host_username, host_pass, host_inbound_id, subscription_url)
                VALUES (?, ?, ?, ?, ?, ?)
                """,
                (name, url, user, passwd, inbound, subscription_url),
            )
            conn.commit()
            logging.info(f"Успешно создан новый сервер: {name}")
    except (sqlite3.Error, ValueError) as e:
        logging.error(f"Ошибка при создании хоста '{name}': {e}")


def update_host_subscription_url(host_name: str, subscription_url: str | None) -> bool:
    """Обновляет subscription_url хоста."""
    try:
        host_name = normalize_host_name(host_name)
        with get_db_connection() as conn:
            cursor = conn.cursor()
            cursor.execute("SELECT 1 FROM xui_hosts WHERE TRIM(host_name) = TRIM(?)", (host_name,))
            exists = cursor.fetchone() is not None
            if not exists:
                logging.warning(
                    f"update_host_subscription_url: сервер с именем '{host_name}' не найден (после TRIM)"
                )
                return False

            cursor.execute(
                "UPDATE xui_hosts SET subscription_url = ? WHERE TRIM(host_name) = TRIM(?)",
                (subscription_url, host_name),
            )
            conn.commit()
            return True
    except sqlite3.Error as e:
        logging.error(f"Не удалось обновить subscription_url для хоста '{host_name}': {e}")
        return False


def set_referral_start_bonus_received(user_id: int) -> bool:
    """Пометить, что пользователь получил стартовый бонус за реферальную регистрацию."""
    try:
        with get_db_connection() as conn:
            cursor = conn.cursor()
            cursor.execute(
                "UPDATE users SET referral_start_bonus_received = 1 WHERE telegram_id = ?",
                (user_id,),
            )
            conn.commit()
            return cursor.rowcount > 0
    except sqlite3.Error as e:
        logging.error(
            f"Не удалось пометить получение стартового реферального бонуса для пользователя {user_id}: {e}"
        )
        return False


def update_host_url(host_name: str, new_url: str) -> bool:
    """Обновить URL панели XUI для указанного хоста."""
    try:
        host_name = normalize_host_name(host_name)
        new_url = (new_url or "").strip()
        with get_db_connection() as conn:
            cursor = conn.cursor()
            cursor.execute("SELECT 1 FROM xui_hosts WHERE TRIM(host_name) = TRIM(?)", (host_name,))
            if cursor.fetchone() is None:
                logging.warning(f"update_host_url: сервер с именем '{host_name}' не найден")
                return False

            cursor.execute(
                "UPDATE xui_hosts SET host_url = ? WHERE TRIM(host_name) = TRIM(?)",
                (new_url, host_name),
            )
            conn.commit()
            return cursor.rowcount > 0
    except sqlite3.Error as e:
        logging.error(f"Не удалось обновить host_url для хоста '{host_name}': {e}")
        return False


def update_host_name(old_name: str, new_name: str) -> bool:
    """Переименовать сервер во всех связанных таблицах (xui_hosts, plans, vpn_keys) с сохранением регистра."""
    try:
        old_name_n = normalize_host_name(old_name)
        new_name_n = normalize_host_name(new_name)
        if not new_name_n:
            logging.warning("update_host_name: новое имя хоста пустое после нормализации")
            return False

        # Если имена идентичны, ничего делать не нужно
        if old_name_n == new_name_n:
            return True

        with get_db_connection() as conn:
            cursor = conn.cursor()

            # Проверяем существование старого имени хоста
            cursor.execute("SELECT 1 FROM xui_hosts WHERE host_name = ?", (old_name_n,))
            if cursor.fetchone() is None:
                logging.warning(f"update_host_name: исходный сервер не найден '{old_name_n}'")
                return False

            # Проверяем, не занято ли целевое имя (только если это не смена регистра)
            cursor.execute("SELECT 1 FROM xui_hosts WHERE host_name = ?", (new_name_n,))
            if cursor.fetchone() is not None:
                logging.warning(f"update_host_name: целевое имя '{new_name_n}' уже используется")
                return False

            # Пакетное обновление
            cursor.execute(
                "UPDATE xui_hosts SET host_name = ? WHERE host_name = ?",
                (new_name_n, old_name_n),
            )
            cursor.execute(
                "UPDATE plans SET host_name = ? WHERE host_name = ?",
                (new_name_n, old_name_n),
            )
            cursor.execute(
                "UPDATE vpn_keys SET host_name = ? WHERE host_name = ?",
                (new_name_n, old_name_n),
            )

            conn.commit()
            return True

    except sqlite3.Error as e:
        logging.error(f"Не удалось переименовать сервер с '{old_name}' на '{new_name}': {e}")
        return False


def delete_host(host_name: str):
    """Удаляет хост из БД по имени."""
    try:
        host_name = normalize_host_name(host_name)
        with get_db_connection() as conn:
            cursor = conn.cursor()
            cursor.execute("DELETE FROM plans WHERE TRIM(host_name) = TRIM(?)", (host_name,))
            cursor.execute("DELETE FROM xui_hosts WHERE TRIM(host_name) = TRIM(?)", (host_name,))
            conn.commit()
            logging.info(f"Сервер '{host_name}' и его тарифы успешно удалены.")
    except sqlite3.Error as e:
        logging.error(f"Ошибка удаления хоста '{host_name}': {e}")


def get_host(host_name: str) -> dict | None:
    """Возвращает хост по имени или None, если не найден."""
    try:
        host_name = normalize_host_name(host_name)
        with get_db_connection() as conn:
            conn.row_factory = sqlite3.Row
            cursor = conn.cursor()
            cursor.execute("SELECT * FROM xui_hosts WHERE host_name = ?", (host_name,))
            result = cursor.fetchone()
            return dict(result) if result else None
    except sqlite3.Error as e:
        logging.error(f"Ошибка получения хоста '{host_name}': {e}")
        return None


def update_host_ssh_settings(
    host_name: str,
    ssh_host: str | None = None,
    ssh_port: int | None = None,
    ssh_user: str | None = None,
    ssh_password: str | None = None,
    ssh_key_path: str | None = None,
) -> bool:
    """Обновить SSH-параметры для speedtest/maintenance по хосту.

    Переданные None значения очищают соответствующие поля (ставят NULL).
    """
    try:
        host_name_n = normalize_host_name(host_name)
        with get_db_connection() as conn:
            cursor = conn.cursor()
            cursor.execute(
                "SELECT 1 FROM xui_hosts WHERE TRIM(host_name) = TRIM(?)",
                (host_name_n,),
            )
            if cursor.fetchone() is None:
                logging.warning(f"update_host_ssh_settings: сервер не найден '{host_name_n}'")
                return False

            cursor.execute(
                """
                UPDATE xui_hosts
                SET ssh_host = ?, ssh_port = ?, ssh_user = ?, ssh_password = ?, ssh_key_path = ?
                WHERE TRIM(host_name) = TRIM(?)
                """,
                (
                    (ssh_host or None),
                    (int(ssh_port) if ssh_port is not None else None),
                    (ssh_user or None),
                    (ssh_password if ssh_password is not None else None),
                    (ssh_key_path or None),
                    host_name_n,
                ),
            )
            conn.commit()
            return True
    except sqlite3.Error as e:
        logging.error(f"Не удалось обновить SSH-настройки для хоста '{host_name}': {e}")
        return False


def delete_key_by_id(key_id: int) -> bool:
    """Удаляет ключ из БД по key_id."""
    try:
        with get_db_connection() as conn:
            cursor = conn.cursor()
            cursor.execute("DELETE FROM vpn_keys WHERE key_id = ?", (key_id,))
            affected = cursor.rowcount
            conn.commit()
            return affected > 0
    except sqlite3.Error as e:
        logging.error(f"Не удалось удалить ключ по id {key_id}: {e}")
        return False


def update_key_comment(key_id: int, comment: str) -> bool:
    """Обновляет комментарий администратора у ключа."""
    try:
        with get_db_connection() as conn:
            cursor = conn.cursor()
            cursor.execute("UPDATE vpn_keys SET comment = ? WHERE key_id = ?", (comment, key_id))
            conn.commit()
            return cursor.rowcount > 0
    except sqlite3.Error as e:
        logging.error(f"Не удалось обновить комментарий ключа для {key_id}: {e}")
        return False


def get_all_hosts() -> list[dict]:
    """Возвращает список всех хостов."""
    try:
        with get_db_connection() as conn:
            conn.row_factory = sqlite3.Row
            cursor = conn.cursor()
            cursor.execute("SELECT * FROM xui_hosts")
            hosts = cursor.fetchall()
            # Normalize host_name in returned dicts to avoid trailing/invisible chars in runtime
            result = []
            for row in hosts:
                d = dict(row)
                d["host_name"] = normalize_host_name(d.get("host_name"))
                result.append(d)
            return result
    except sqlite3.Error as e:
        logging.error(f"Ошибка получения списка всех хостов: {e}")
        return []


def get_speedtests(host_name: str, limit: int = 20) -> list[dict]:
    """Получить последние результаты спидтестов по хосту (ssh/net), новые сверху."""
    try:
        host_name_n = normalize_host_name(host_name)
        with get_db_connection() as conn:
            conn.row_factory = sqlite3.Row
            cursor = conn.cursor()
            try:
                limit_int = int(limit)
            except Exception:
                limit_int = 20
            cursor.execute(
                """
                SELECT id, host_name, method, ping_ms, jitter_ms, download_mbps, upload_mbps,
                       server_name, server_id, ok, error, created_at
                FROM host_speedtests
                WHERE TRIM(host_name) = TRIM(?)
                ORDER BY datetime(created_at) DESC
                LIMIT ?
                """,
                (host_name_n, limit_int),
            )
            rows = cursor.fetchall()
            return [dict(r) for r in rows]
    except sqlite3.Error as e:
        logging.error(f"Не удалось получить speedtest-данные для хоста '{host_name}': {e}")
        return []


def get_latest_speedtest(host_name: str) -> dict | None:
    """Получить последний по времени спидтест для хоста."""
    try:
        host_name_n = normalize_host_name(host_name)
        with get_db_connection() as conn:
            conn.row_factory = sqlite3.Row
            cursor = conn.cursor()
            cursor.execute(
                """
                SELECT id, host_name, method, ping_ms, jitter_ms, download_mbps, upload_mbps,
                       server_name, server_id, ok, error, created_at
                FROM host_speedtests
                WHERE TRIM(host_name) = TRIM(?)
                ORDER BY datetime(created_at) DESC
                LIMIT 1
                """,
                (host_name_n,),
            )
            row = cursor.fetchone()
            return dict(row) if row else None
    except sqlite3.Error as e:
        logging.error(f"Не удалось получить последний speedtest для хоста '{host_name}': {e}")
        return None


def get_latest_speedtest_run_at() -> datetime | None:
    """Получает время последнего спидтеста."""
    try:
        with get_db_connection() as conn:
            cursor = conn.cursor()
            cursor.execute("SELECT MAX(datetime(created_at)) FROM host_speedtests")
            row = cursor.fetchone()
            if not row or not row[0]:
                return None
            return datetime.fromisoformat(row[0])
    except (sqlite3.Error, ValueError, TypeError) as e:
        logging.error(f"Не удалось получить время последнего speedtest: {e}")
        return None


def record_backup_created(filename: str | None = None) -> None:
    """Фиксирует факт создания бэкапа в backup_history. Вызывать сразу
    после успешного backup_manager.create_backup_file()."""
    try:
        with get_db_connection() as conn:
            cursor = conn.cursor()
            cursor.execute(
                "INSERT INTO backup_history (filename) VALUES (?)",
                (filename,),
            )
            conn.commit()
    except sqlite3.Error as e:
        logging.error(f"Не удалось записать backup_history: {e}")


def get_latest_backup_created_at() -> datetime | None:
    """Получает время последнего бэкапа."""
    try:
        with get_db_connection() as conn:
            cursor = conn.cursor()
            cursor.execute("SELECT MAX(datetime(created_at)) FROM backup_history")
            row = cursor.fetchone()
            if not row or not row[0]:
                return None
            return datetime.fromisoformat(row[0])
    except (sqlite3.Error, ValueError, TypeError) as e:
        logging.error(f"Не удалось получить время последнего бэкапа: {e}")
        return None


def find_and_complete_pending_transaction(
    payment_id: str,
    amount_rub: float | None,
    payment_method: str,
    currency_name: str | None = None,
    amount_currency: float | None = None,
) -> dict | None:
    """Находит ожидающую транзакцию по payment_id и помечает её оплаченной."""
    try:
        with get_db_connection() as conn:
            conn.row_factory = sqlite3.Row
            cursor = conn.cursor()

            cursor.execute(
                "SELECT * FROM transactions WHERE payment_id = ? AND status = 'pending'",
                (payment_id,),
            )
            transaction = cursor.fetchone()
            if not transaction:
                logger.warning(f"Ожидающая транзакция не найдена для payment_id={payment_id}")
                return None

            cursor.execute(
                """
                UPDATE transactions
                SET status = 'paid',
                    amount_rub = COALESCE(?, amount_rub),
                    amount_currency = COALESCE(?, amount_currency),
                    currency_name = COALESCE(?, currency_name),
                    payment_method = COALESCE(?, payment_method)
                WHERE payment_id = ?
                """,
                (
                    amount_rub,
                    amount_currency,
                    currency_name,
                    payment_method,
                    payment_id,
                ),
            )
            conn.commit()

            try:
                raw_md = None
                try:
                    raw_md = transaction["metadata"]
                except Exception:
                    raw_md = None
                md = json.loads(raw_md) if raw_md else {}
            except Exception:
                md = {}
            return md
    except sqlite3.Error as e:
        logging.error(f"Не удалось завершить ожидающую транзакцию {payment_id}: {e}")
        return None


def insert_host_speedtest(
    host_name: str,
    method: str,
    ping_ms: float | None = None,
    jitter_ms: float | None = None,
    download_mbps: float | None = None,
    upload_mbps: float | None = None,
    server_name: str | None = None,
    server_id: str | None = None,
    ok: bool = True,
    error: str | None = None,
) -> bool:
    """Сохранить результат спидтеста в таблицу host_speedtests."""
    try:
        host_name_n = normalize_host_name(host_name)
        method_s = (method or "").strip().lower()
        if method_s not in ("ssh", "net"):
            method_s = "ssh"
        with get_db_connection() as conn:
            cursor = conn.cursor()
            cursor.execute(
                """
                INSERT INTO host_speedtests
                (host_name, method, ping_ms, jitter_ms, download_mbps, upload_mbps, server_name, server_id, ok, error, created_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    host_name_n,
                    method_s,
                    ping_ms,
                    jitter_ms,
                    download_mbps,
                    upload_mbps,
                    server_name,
                    server_id,
                    1 if ok else 0,
                    (error or None),
                    # Явно передаём МСК-время из Python, а не полагаемся на
                    # DEFAULT колонки: на существующих таблицах (созданных до
                    # перехода на localtime) DEFAULT из CREATE TABLE IF NOT
                    # EXISTS не подхватится — SQLite не меняет схему уже
                    # существующей таблицы.
                    datetime.now(),
                ),
            )
            conn.commit()
            return True
    except sqlite3.Error as e:
        logging.error(f"Не удалось сохранить запись speedtest для '{host_name}': {e}")
        return False


def get_admin_stats() -> dict:
    """Return aggregated statistics for the admin dashboard.

    Includes:
    - total_users: count of users
    - total_keys: count of all keys
    - active_keys: keys with expiry_date in the future
    - total_income: sum of amount_rub for successful transactions
    """
    stats = {
        "total_users": 0,
        "total_keys": 0,
        "active_keys": 0,
        "total_income": 0.0,
        # Today's metrics
        "today_new_users": 0,
        "today_income": 0.0,
        "today_issued_keys": 0,
    }
    try:
        with get_db_connection() as conn:
            cursor = conn.cursor()
            # Users
            cursor.execute("SELECT COUNT(*) FROM users")
            row = cursor.fetchone()
            stats["total_users"] = (row[0] or 0) if row else 0

            # Total keys
            cursor.execute("SELECT COUNT(*) FROM vpn_keys")
            row = cursor.fetchone()
            stats["total_keys"] = (row[0] or 0) if row else 0

            # Active keys
            cursor.execute("SELECT COUNT(*) FROM vpn_keys WHERE expiry_date > datetime('now','localtime')")
            row = cursor.fetchone()
            stats["active_keys"] = (row[0] or 0) if row else 0

            # Income: consider common success markers (total)
            cursor.execute(
                "SELECT COALESCE(SUM(amount_rub), 0) FROM transactions WHERE status IN ('paid','success','succeeded') AND LOWER(COALESCE(payment_method, '')) <> 'balance'"
            )
            row = cursor.fetchone()
            stats["total_income"] = float(row[0] or 0.0) if row else 0.0

            # Today's metrics
            # new users today
            cursor.execute("SELECT COUNT(*) FROM users WHERE date(registration_date) = date('now','localtime')")
            row = cursor.fetchone()
            stats["today_new_users"] = (row[0] or 0) if row else 0

            # Today's income
            cursor.execute("""
                SELECT COALESCE(SUM(amount_rub), 0)
                FROM transactions
                WHERE status IN ('paid','success','succeeded')
                  AND LOWER(COALESCE(payment_method, '')) <> 'balance'
                  AND date(created_date) = date('now','localtime')
                """)
            row = cursor.fetchone()
            stats["today_income"] = float(row[0] or 0.0) if row else 0.0

            # Today's issued keys
            cursor.execute("SELECT COUNT(*) FROM vpn_keys WHERE date(created_date) = date('now','localtime')")
            row = cursor.fetchone()
            stats["today_issued_keys"] = (row[0] or 0) if row else 0
    except sqlite3.Error as e:
        logging.error(f"Не удалось получить статистику администратора: {e}")
    return stats


def get_all_keys() -> list[dict]:
    """Возвращает список всех ключей всех пользователей."""
    try:
        with get_db_connection() as conn:
            conn.row_factory = sqlite3.Row
            cursor = conn.cursor()
            cursor.execute("SELECT * FROM vpn_keys")
            return [dict(row) for row in cursor.fetchall()]
    except sqlite3.Error as e:
        logging.error(f"Не удалось получить все ключи: {e}")
        return []


def get_keys_for_user(user_id: int) -> list[dict]:
    """Возвращает список ключей конкретного пользователя."""
    try:
        with get_db_connection() as conn:
            conn.row_factory = sqlite3.Row
            cursor = conn.cursor()
            cursor.execute(
                "SELECT * FROM vpn_keys WHERE user_id = ? ORDER BY created_date DESC",
                (user_id,),
            )
            return [dict(row) for row in cursor.fetchall()]
    except sqlite3.Error as e:
        logging.error(f"Не удалось получить ключи пользователя {user_id}: {e}")
        return []


def get_key_by_id(key_id: int) -> dict | None:
    """Возвращает ключ по key_id или None, если не найден."""
    try:
        with get_db_connection() as conn:
            conn.row_factory = sqlite3.Row
            cursor = conn.cursor()
            cursor.execute("SELECT * FROM vpn_keys WHERE key_id = ?", (key_id,))
            row = cursor.fetchone()
            return dict(row) if row else None
    except sqlite3.Error as e:
        logging.error(f"Не удалось получить ключ по id {key_id}: {e}")
        return None


def update_key_email(key_id: int, new_email: str) -> bool:
    """Обновляет key_email у существующего ключа."""
    try:
        with get_db_connection() as conn:
            cursor = conn.cursor()
            cursor.execute(
                "UPDATE vpn_keys SET key_email = ? WHERE key_id = ?",
                (new_email, key_id),
            )
            conn.commit()
            return cursor.rowcount > 0
    except sqlite3.IntegrityError as e:
        logging.error(f"Нарушение уникальности email для ключа {key_id}: {e}")
        return False
    except sqlite3.Error as e:
        logging.error(f"Не удалось обновить email ключа {key_id}: {e}")
        return False


def update_key_host(key_id: int, new_host_name: str) -> bool:
    """Переносит ключ на другой хост (обновляет host_name)."""
    try:
        with get_db_connection() as conn:
            cursor = conn.cursor()
            cursor.execute(
                "UPDATE vpn_keys SET host_name = ? WHERE key_id = ?",
                (normalize_host_name(new_host_name), key_id),
            )
            conn.commit()
            return cursor.rowcount > 0
    except sqlite3.Error as e:
        logging.error(f"Не удалось обновить хост ключа {key_id}: {e}")
        return False


def create_gift_key(
    user_id: int,
    host_name: str,
    key_email: str,
    months: int,
    xui_client_uuid: str | None = None,
) -> int | None:
    """Создать подарочный ключ: задаёт expiry_date = now + months, host_name нормализуется.

    Возвращает key_id или None при ошибке.
    """
    try:
        host_name = normalize_host_name(host_name)
        expiry = datetime.now() + timedelta(
            days=sum(
                calendar.monthrange(
                    datetime.now().year + (datetime.now().month + i - 1) // 12,
                    (datetime.now().month + i - 1) % 12 + 1,
                )[1]
                for i in range(int(months or 1))
            )
        )
        with get_db_connection() as conn:
            cursor = conn.cursor()
            cursor.execute(
                "INSERT INTO vpn_keys (user_id, host_name, xui_client_uuid, key_email, expiry_date, created_date) "
                "VALUES (?, ?, ?, ?, ?, ?)",
                (
                    user_id,
                    host_name,
                    xui_client_uuid or f"GIFT-{user_id}-{int(datetime.now().timestamp())}",
                    key_email,
                    expiry.isoformat(),
                    # Явно, не полагаясь на DEFAULT колонки — см. host_speedtests выше.
                    datetime.now(),
                ),
            )
            conn.commit()
            return cursor.lastrowid
    except sqlite3.IntegrityError as e:
        logging.error(
            f"Не удалось создать подарочный ключ для пользователя {user_id}: email {key_email} уже занят: {e}"
        )
        return None
    except sqlite3.Error as e:
        logging.error(f"Не удалось создать подарочный ключ для пользователя {user_id}: {e}")
        return None


def get_setting(key: str) -> str | None:
    # Если настройка уже есть в памяти — отдаем сразу без запроса к БД
    """Возвращает значение настройки бота по ключу или None, если не задано."""
    if key in SETTINGS_CACHE:
        return SETTINGS_CACHE[key]

    with get_db_connection() as conn:
        cursor = conn.cursor()
        cursor.execute("SELECT value FROM bot_settings WHERE key = ?", (key,))
        row = cursor.fetchone()
        val = row[0] if row else None

        # Сохраняем в кэш
        SETTINGS_CACHE[key] = val
        return val


def get_admin_ids() -> set[int]:
    """Возвращает множество ID администраторов из настроек.

    Поддерживает оба варианта: одиночный 'admin_telegram_id' и список 'admin_telegram_ids'
    через запятую/пробелы или JSON-массив.
    """
    ids: set[int] = set()
    try:
        single = get_setting("admin_telegram_id")
        if single:
            try:
                ids.add(int(single))
            except Exception:
                pass
        multi_raw = get_setting("admin_telegram_ids")
        if multi_raw:
            s = (multi_raw or "").strip()
            # Попробуем как JSON-массив
            try:
                arr = json.loads(s)
                if isinstance(arr, list):
                    for v in arr:
                        try:
                            ids.add(int(v))
                        except Exception:
                            pass
                    return ids
            except Exception:
                pass
            # Иначе как строка с разделителями (запятая/пробел)
            parts = [p for p in re.split(r"[\s,]+", s) if p]
            for p in parts:
                try:
                    ids.add(int(p))
                except Exception:
                    pass
    except Exception as e:
        logging.warning(f"Ошибка при получении списка ID администраторов: {e}")
    return ids


def is_admin(user_id: int) -> bool:
    """Проверка прав администратора по списку ID из настроек."""
    try:
        return int(user_id) in get_admin_ids()
    except Exception:
        return False


def get_referrals_for_user(user_id: int) -> list[dict]:
    """Возвращает список пользователей, которых пригласил данный user_id.

    Поля: telegram_id, username, registration_date, total_spent.
    """
    try:
        with get_db_connection() as conn:
            conn.row_factory = sqlite3.Row
            cursor = conn.cursor()
            cursor.execute(
                """
                SELECT telegram_id, username, registration_date, total_spent
                FROM users
                WHERE referred_by = ?
                ORDER BY registration_date DESC
                """,
                (user_id,),
            )
            rows = cursor.fetchall()
            return [dict(r) for r in rows]
    except sqlite3.Error as e:
        logging.error(f"Не удалось получить рефералов пользователя {user_id}: {e}")
        return []


def get_all_settings() -> dict:
    """Возвращает все настройки бота в виде словаря."""
    settings = {}
    try:
        with get_db_connection() as conn:
            conn.row_factory = sqlite3.Row
            cursor = conn.cursor()
            cursor.execute("SELECT key, value FROM bot_settings")
            rows = cursor.fetchall()
            for row in rows:
                settings[row["key"]] = row["value"]
    except sqlite3.Error as e:
        logging.error(f"Не удалось получить все настройки: {e}")
    return settings


def update_setting(key: str, value: str):
    """Устанавливает значение настройки бота."""
    try:
        with get_db_connection() as conn:
            cursor = conn.cursor()
            cursor.execute(
                "INSERT OR REPLACE INTO bot_settings (key, value) VALUES (?, ?)",
                (key, value),
            )
            conn.commit()

        # Обновляем кэш в памяти, чтобы бот сразу увидел изменения
        SETTINGS_CACHE[key] = value
        logging.info(f"Настройка '{key}' обновлена в БД и кэше.")

    except sqlite3.Error as e:
        logging.error(f"Не удалось обновить настройку '{key}': {e}")


def create_plan(host_name: str, plan_name: str, months: int, price: float):
    """Создаёт новый тарифный план для хоста."""
    try:
        host_name = normalize_host_name(host_name)
        with get_db_connection() as conn:
            cursor = conn.cursor()
            cursor.execute(
                "INSERT INTO plans (host_name, plan_name, months, price) VALUES (?, ?, ?, ?)",
                (host_name, plan_name, months, price),
            )
            conn.commit()
            logging.info(f"Создан новый тариф '{plan_name}' для хоста '{host_name}'.")
    except sqlite3.Error as e:
        logging.error(f"Не удалось создать тариф для хоста '{host_name}': {e}")


def get_plans_for_host(host_name: str) -> list[dict]:
    """Возвращает список тарифных планов конкретного хоста."""
    try:
        host_name = normalize_host_name(host_name)
        with get_db_connection() as conn:
            conn.row_factory = sqlite3.Row
            cursor = conn.cursor()
            cursor.execute(
                "SELECT * FROM plans WHERE TRIM(host_name) = TRIM(?) ORDER BY months",
                (host_name,),
            )
            plans = cursor.fetchall()
            return [dict(plan) for plan in plans]
    except sqlite3.Error as e:
        logging.error(f"Не удалось получить тарифы для хоста '{host_name}': {e}")
        return []


def get_plan_by_id(plan_id: int) -> dict | None:
    """Возвращает тарифный план по id или None, если не найден."""
    try:
        with get_db_connection() as conn:
            conn.row_factory = sqlite3.Row
            cursor = conn.cursor()
            cursor.execute("SELECT * FROM plans WHERE plan_id = ?", (plan_id,))
            plan = cursor.fetchone()
            return dict(plan) if plan else None
    except sqlite3.Error as e:
        logging.error(f"Не удалось получить тариф по id '{plan_id}': {e}")
        return None


def delete_plan(plan_id: int):
    """Удаляет тарифный план по id."""
    try:
        with get_db_connection() as conn:
            cursor = conn.cursor()
            cursor.execute("DELETE FROM plans WHERE plan_id = ?", (plan_id,))
            conn.commit()
            logging.info(f"Тариф с id {plan_id} удалён.")
    except sqlite3.Error as e:
        logging.error(f"Не удалось удалить тариф с id {plan_id}: {e}")


def update_plan(plan_id: int, plan_name: str, months: int, price: float) -> bool:
    """Обновляет параметры существующего тарифного плана."""
    try:
        with get_db_connection() as conn:
            cursor = conn.cursor()
            cursor.execute(
                "UPDATE plans SET plan_name = ?, months = ?, price = ? WHERE plan_id = ?",
                (plan_name, months, price, plan_id),
            )
            conn.commit()
            if cursor.rowcount == 0:
                logging.warning(f"Ни один тариф не обновлён для id {plan_id} (не найден).")
                return False
            logging.info(
                f"Тариф {plan_id} обновлён: name='{plan_name}', months={months}, price={price}."
            )
            return True
    except sqlite3.Error as e:
        logging.error(f"Не удалось обновить тариф {plan_id}: {e}")
        return False


def register_user_if_not_exists(telegram_id: int, username: str, referrer_id) -> bool:
    """
    Регистрирует пользователя, если его нет в БД.

    Возвращает True, если пользователь абсолютно новый, и False, если он уже существовал.
    """
    is_new_user = False
    try:
        with get_db_connection() as conn:
            cursor = conn.cursor()
            cursor.execute("SELECT referred_by FROM users WHERE telegram_id = ?", (telegram_id,))
            row = cursor.fetchone()

            if not row:
                # 1. Абсолютно новый пользователь
                safe_ref_id = None
                # Проверяем, что реферер передан, это число, и он не равен самому себе
                if referrer_id and str(referrer_id).strip().isdigit():
                    if int(referrer_id) != int(telegram_id):
                        safe_ref_id = int(referrer_id)

                cursor.execute(
                    "INSERT INTO users (telegram_id, username, registration_date, referred_by) VALUES (?, ?, ?, ?)",
                    (telegram_id, username, datetime.now(), safe_ref_id),
                )
                is_new_user = True  # Переключаем флаг, так как юзер создан впервые

            else:
                # 2. Пользователь уже есть в базе — просто обновляем данные
                cursor.execute(
                    "UPDATE users SET username = ? WHERE telegram_id = ?",
                    (username, telegram_id),
                )
                current_ref = row[0]

                # Если у старого пользователя поле реферера пустое, а сейчас он пришел по ссылке — дописываем
                if referrer_id and (current_ref is None or str(current_ref).strip() == ""):
                    try:
                        if str(referrer_id).strip().isdigit() and int(referrer_id) != int(
                            telegram_id
                        ):
                            cursor.execute(
                                "UPDATE users SET referred_by = ? WHERE telegram_id = ?",
                                (int(referrer_id), telegram_id),
                            )
                    except Exception:
                        pass  # Best-effort

            conn.commit()

    except sqlite3.Error as e:
        logging.error(f"Не удалось зарегистрировать пользователя {telegram_id}: {e}")
        return False

    return is_new_user  # Возвращаем True (новый) или False (старый)


def add_to_referral_balance(user_id: int, amount: float):
    """Прибавляет сумму к реферальному балансу пользователя (одна из веток учёта)."""
    try:
        with get_db_connection() as conn:
            cursor = conn.cursor()
            cursor.execute(
                "UPDATE users SET referral_balance = referral_balance + ? WHERE telegram_id = ?",
                (amount, user_id),
            )
            conn.commit()
    except sqlite3.Error as e:
        logging.error(f"Не удалось начислить реферальный баланс пользователю {user_id}: {e}")


def set_referral_balance(user_id: int, value: float):
    """Устанавливает точное значение реферального баланса пользователя (одна из веток учёта)."""
    try:
        with get_db_connection() as conn:
            cursor = conn.cursor()
            cursor.execute(
                "UPDATE users SET referral_balance = ? WHERE telegram_id = ?",
                (value, user_id),
            )
            conn.commit()
    except sqlite3.Error as e:
        logging.error(f"Не удалось установить реферальный баланс пользователю {user_id}: {e}")


def set_referral_balance_all(user_id: int, value: float):
    """Устанавливает точное значение общего реферального баланса пользователя."""
    try:
        with get_db_connection() as conn:
            cursor = conn.cursor()
            cursor.execute(
                "UPDATE users SET referral_balance_all = ? WHERE telegram_id = ?",
                (value, user_id),
            )
            conn.commit()
    except sqlite3.Error as e:
        logging.error(f"Не удалось установить общий реферальный баланс пользователю {user_id}: {e}")


def get_referral_balance_all(user_id: int) -> float:
    """Возвращает общий (накопительный) реферальный баланс пользователя."""
    try:
        with get_db_connection() as conn:
            cursor = conn.cursor()
            cursor.execute(
                "SELECT referral_balance_all FROM users WHERE telegram_id = ?",
                (user_id,),
            )
            row = cursor.fetchone()
            return row[0] if row else 0.0
    except sqlite3.Error as e:
        logging.error(f"Не удалось получить общий реферальный баланс пользователя {user_id}: {e}")
        return 0.0


def get_referral_balance(user_id: int) -> float:
    """Возвращает текущий (доступный к выводу) реферальный баланс пользователя."""
    try:
        with get_db_connection() as conn:
            cursor = conn.cursor()
            cursor.execute("SELECT referral_balance FROM users WHERE telegram_id = ?", (user_id,))
            result = cursor.fetchone()
            return result[0] if result else 0.0
    except sqlite3.Error as e:
        logging.error(f"Не удалось получить реферальный баланс пользователя {user_id}: {e}")
        return 0.0


def get_balance(user_id: int) -> float:
    """Возвращает основной баланс пользователя."""
    try:
        with get_db_connection() as conn:
            cursor = conn.cursor()
            cursor.execute("SELECT balance FROM users WHERE telegram_id = ?", (user_id,))
            result = cursor.fetchone()
            return result[0] if result else 0.0
    except sqlite3.Error as e:
        logging.error(f"Не удалось получить баланс пользователя {user_id}: {e}")
        return 0.0


def adjust_user_balance(user_id: int, delta: float) -> bool:
    """Скорректировать баланс пользователя на указанную дельту (может быть отрицательной)."""
    try:
        with get_db_connection() as conn:
            cursor = conn.cursor()
            cursor.execute(
                "UPDATE users SET balance = COALESCE(balance, 0) + ? WHERE telegram_id = ?",
                (float(delta), user_id),
            )
            conn.commit()
            return cursor.rowcount > 0
    except sqlite3.Error as e:
        logging.error(f"Не удалось скорректировать баланс пользователя {user_id}: {e}")
        return False


def set_balance(user_id: int, value: float) -> bool:
    """Устанавливает точное значение основного баланса пользователя."""
    try:
        with get_db_connection() as conn:
            cursor = conn.cursor()
            cursor.execute("UPDATE users SET balance = ? WHERE telegram_id = ?", (value, user_id))
            conn.commit()
            return cursor.rowcount > 0
    except sqlite3.Error as e:
        logging.error(f"Не удалось установить баланс пользователя {user_id}: {e}")
        return False


def add_to_balance(user_id: int, amount: float) -> bool:
    """Прибавляет (или вычитает, при отрицательном amount) сумму к основному балансу пользователя."""
    try:
        with get_db_connection() as conn:
            cursor = conn.cursor()
            cursor.execute(
                "UPDATE users SET balance = balance + ? WHERE telegram_id = ?",
                (amount, user_id),
            )
            conn.commit()
            return cursor.rowcount > 0
    except sqlite3.Error as e:
        logging.error(f"Не удалось пополнить баланс пользователя {user_id}: {e}")
        return False


def credit_referral_reward(user_id: int, amount: float) -> bool:
    """
    Атомарно начисляет реферальное вознаграждение: одним SQL-запросом одновременно
    увеличивает основной баланс (balance) и накопительный реферальный баланс
    (referral_balance_all) пригласившего пользователя.

    Раньше это делалось двумя отдельными вызовами (обновление balance, затем
    отдельным запросом — referral_balance_all), из-за чего при сбое/блокировке первого запроса
    второй мог всё равно отработать — баланс не рос, а referral_balance_all
    рос, при этом транзакция в истории не логировалась (лог был завязан на
    успех именно первого запроса). Одно атомарное обновление исключает
    такое рассогласование: либо обе колонки обновляются вместе, либо
    не обновляется ни одна.

    Returns:
        bool: True, если пользователь найден и обновление применено.
    """
    try:
        with get_db_connection() as conn:
            cursor = conn.cursor()
            cursor.execute(
                """
                UPDATE users
                SET balance = balance + ?,
                    referral_balance_all = referral_balance_all + ?
                WHERE telegram_id = ?
                """,
                (amount, amount, user_id),
            )
            conn.commit()
            return cursor.rowcount > 0
    except sqlite3.Error as e:
        logging.error(f"Не удалось начислить реферальное вознаграждение пользователю {user_id}: {e}")
        return False


def deduct_from_balance(user_id: int, amount: float) -> bool:
    """Атомарное списание с основного баланса при достаточности средств."""
    if amount <= 0:
        return True
    try:
        with get_db_connection() as conn:
            cursor = conn.cursor()
            cursor.execute("BEGIN IMMEDIATE")
            cursor.execute("SELECT balance FROM users WHERE telegram_id = ?", (user_id,))
            row = cursor.fetchone()
            current = row[0] if row else 0.0
            if current < amount:
                conn.rollback()
                return False
            cursor.execute(
                "UPDATE users SET balance = balance - ? WHERE telegram_id = ?",
                (amount, user_id),
            )
            conn.commit()
            return True
    except sqlite3.Error as e:
        logging.error(f"Не удалось списать с баланса пользователя {user_id}: {e}")
        return False


def deduct_from_referral_balance(user_id: int, amount: float) -> bool:
    """Атомарное списание с реферального баланса при достаточности средств."""
    if amount <= 0:
        return True
    try:
        with get_db_connection() as conn:
            cursor = conn.cursor()
            cursor.execute("BEGIN IMMEDIATE")
            cursor.execute("SELECT referral_balance FROM users WHERE telegram_id = ?", (user_id,))
            row = cursor.fetchone()
            current = row[0] if row else 0.0
            if current < amount:
                conn.rollback()
                return False
            cursor.execute(
                "UPDATE users SET referral_balance = referral_balance - ? WHERE telegram_id = ?",
                (amount, user_id),
            )
            conn.commit()
            return True
    except sqlite3.Error as e:
        logging.error(f"Не удалось списать с реферального баланса пользователя {user_id}: {e}")
        return False


def get_referral_count(user_id: int) -> int:
    """Возвращает количество пользователей, приглашённых данным user_id."""
    try:
        with get_db_connection() as conn:
            cursor = conn.cursor()
            cursor.execute("SELECT COUNT(*) FROM users WHERE referred_by = ?", (user_id,))
            return cursor.fetchone()[0] or 0
    except sqlite3.Error as e:
        logging.error(f"Не удалось получить количество рефералов пользователя {user_id}: {e}")
        return 0


def get_top_referrers(limit: int = 20) -> list[dict]:
    """
    Возвращает топ пользователей по суммарному реферальному заработку
    (referral_balance_all) вместе с количеством приглашённых.

    Поля: telegram_id, username, referral_balance_all, referral_count.
    Пользователи с нулевым referral_balance_all не включаются.
    """
    try:
        with get_db_connection() as conn:
            conn.row_factory = sqlite3.Row
            cursor = conn.cursor()
            cursor.execute(
                """
                SELECT
                    u.telegram_id AS telegram_id,
                    u.username AS username,
                    u.referral_balance_all AS referral_balance_all,
                    (SELECT COUNT(*) FROM users r WHERE r.referred_by = u.telegram_id) AS referral_count
                FROM users u
                WHERE u.referral_balance_all > 0
                ORDER BY u.referral_balance_all DESC
                LIMIT ?
                """,
                (limit,),
            )
            return [dict(row) for row in cursor.fetchall()]
    except sqlite3.Error as e:
        logging.error(f"Не удалось получить топ рефереров: {e}")
        return []


def get_user(telegram_id: int):
    """Возвращает пользователя по telegram_id или None, если не найден."""
    try:
        with get_db_connection() as conn:
            conn.row_factory = sqlite3.Row
            cursor = conn.cursor()
            cursor.execute("SELECT * FROM users WHERE telegram_id = ?", (telegram_id,))
            user_data = cursor.fetchone()
            return dict(user_data) if user_data else None
    except sqlite3.Error as e:
        logging.error(f"Не удалось получить пользователя {telegram_id}: {e}")
        return None


def set_terms_agreed(telegram_id: int):
    """Отмечает, что пользователь принял условия использования."""
    try:
        with get_db_connection() as conn:
            cursor = conn.cursor()
            cursor.execute(
                "UPDATE users SET agreed_to_terms = 1 WHERE telegram_id = ?",
                (telegram_id,),
            )
            conn.commit()
            logging.info(f"Пользователь {telegram_id} согласился с условиями.")
    except sqlite3.Error as e:
        logging.error(
            f"Не удалось отметить согласие с условиями для пользователя {telegram_id}: {e}"
        )


def update_user_stats(telegram_id: int, amount_spent: float, months_purchased: int):
    """Обновляет накопленную статистику пользователя (потрачено, куплено месяцев)."""
    try:
        with get_db_connection() as conn:
            cursor = conn.cursor()
            cursor.execute(
                "UPDATE users SET total_spent = total_spent + ?, total_months = total_months + ? WHERE telegram_id = ?",
                (amount_spent, months_purchased, telegram_id),
            )
            conn.commit()
    except sqlite3.Error as e:
        logging.error(f"Не удалось обновить статистику пользователя {telegram_id}: {e}")


def get_user_count() -> int:
    """Возвращает общее количество зарегистрированных пользователей."""
    try:
        with get_db_connection() as conn:
            cursor = conn.cursor()
            cursor.execute("SELECT COUNT(*) FROM users")
            return cursor.fetchone()[0] or 0
    except sqlite3.Error as e:
        logging.error(f"Не удалось получить количество пользователей: {e}")
        return 0


def get_total_keys_count() -> int:
    """Возвращает общее количество ключей в системе."""
    try:
        with get_db_connection() as conn:
            cursor = conn.cursor()
            cursor.execute("SELECT COUNT(*) FROM vpn_keys")
            return cursor.fetchone()[0] or 0
    except sqlite3.Error as e:
        logging.error(f"Не удалось получить общее количество ключей: {e}")
        return 0


def get_total_spent_sum() -> float:
    """Возвращает суммарную выручку по всем успешным транзакциям."""
    try:
        with get_db_connection() as conn:
            cursor = conn.cursor()
            # Consider only completed/paid transactions when summing total spent
            cursor.execute("""
                SELECT COALESCE(SUM(amount_rub), 0.0)
                FROM transactions
                WHERE LOWER(COALESCE(status, '')) IN ('paid', 'completed', 'success')
                  AND LOWER(COALESCE(payment_method, '')) <> 'balance'
                """)
            val = cursor.fetchone()
            return (val[0] if val else 0.0) or 0.0
    except sqlite3.Error as e:
        logging.error(f"Не удалось получить сумму общих трат: {e}")
        return 0.0


def create_pending_transaction(
    payment_id: str, user_id: int, amount_rub: float, metadata: dict
) -> int:
    """Создаёт запись об ожидающей подтверждения транзакции."""
    try:
        with get_db_connection() as conn:
            cursor = conn.cursor()
            cursor.execute(
                "INSERT INTO transactions (payment_id, user_id, status, amount_rub, metadata, created_date) "
                "VALUES (?, ?, ?, ?, ?, ?)",
                (payment_id, user_id, "pending", amount_rub, json.dumps(metadata), datetime.now()),
            )
            conn.commit()
            return cursor.lastrowid
    except sqlite3.Error as e:
        logging.error(f"Не удалось создать ожидающую транзакцию: {e}")
        return 0


def find_and_complete_ton_transaction(payment_id: str, amount_ton: float) -> dict | None:
    """Находит ожидающую TON-транзакцию по payment_id/сумме и помечает её оплаченной."""
    try:
        with get_db_connection() as conn:
            conn.row_factory = sqlite3.Row
            cursor = conn.cursor()

            cursor.execute(
                "SELECT * FROM transactions WHERE payment_id = ? AND status = 'pending'",
                (payment_id,),
            )
            transaction = cursor.fetchone()
            if not transaction:
                logger.warning(
                    f"TON Webhook: получен платёж для неизвестного или уже завершённого payment_id: {payment_id}"
                )
                return None

            metadata = json.loads(transaction["metadata"])

            # Раньше сумма вообще не сверялась — принималась любая
            # присланная в вебхуке величина, включая символическую. Если
            # ожидаемая сумма известна (записана при создании транзакции,
            # см. handlers.py) — не принимаем недоплату. Небольшой допуск
            # вниз (0.5%) — на случай отличий в округлении курса между
            # моментом создания счёта и моментом реальной отправки.
            expected_amount_ton = metadata.get("expected_amount_ton")
            if expected_amount_ton is not None:
                try:
                    expected_amount_ton = float(expected_amount_ton)
                    if amount_ton < expected_amount_ton * 0.995:
                        logger.warning(
                            f"TON Webhook: недоплата по {payment_id} — пришло {amount_ton} TON, "
                            f"ожидалось {expected_amount_ton} TON. Транзакция не завершена."
                        )
                        return None
                except (TypeError, ValueError):
                    pass

            cursor.execute(
                "UPDATE transactions SET status = 'paid', amount_currency = ?, currency_name = 'TON', payment_method = 'TON' WHERE payment_id = ?",
                (amount_ton, payment_id),
            )
            conn.commit()

            return metadata
    except sqlite3.Error as e:
        logging.error(f"Не удалось завершить TON-транзакцию {payment_id}: {e}")
        return None


def log_transaction(
    username: str,
    transaction_id: str | None,
    payment_id: str | None,
    user_id: int,
    status: str,
    amount_rub: float,
    amount_currency: float | None,
    currency_name: str | None,
    payment_method: str,
    metadata: str,
):
    """Записывает завершённую транзакцию (пополнение/покупку/списание) в историю."""
    try:
        with get_db_connection() as conn:
            cursor = conn.cursor()
            cursor.execute(
                """INSERT INTO transactions
                   (username, transaction_id, payment_id, user_id, status, amount_rub, amount_currency, currency_name, payment_method, metadata, created_date)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (
                    username,
                    transaction_id,
                    payment_id,
                    user_id,
                    status,
                    amount_rub,
                    amount_currency,
                    currency_name,
                    payment_method,
                    metadata,
                    datetime.now(),
                ),
            )
            conn.commit()
    except sqlite3.Error as e:
        logging.error(f"Не удалось залогировать транзакцию пользователя {user_id}: {e}")


def get_paginated_transactions(page: int = 1, per_page: int = 15) -> tuple[list[dict], int]:
    """Возвращает страницу истории транзакций и общее количество записей."""
    offset = (page - 1) * per_page
    transactions = []
    total = 0
    try:
        with get_db_connection() as conn:
            conn.row_factory = sqlite3.Row
            cursor = conn.cursor()

            cursor.execute("SELECT COUNT(*) FROM transactions WHERE status != 'internal'")
            total = cursor.fetchone()[0]

            query = "SELECT * FROM transactions WHERE status != 'internal' ORDER BY created_date DESC LIMIT ? OFFSET ?"
            cursor.execute(query, (per_page, offset))

            for row in cursor.fetchall():
                transaction_dict = dict(row)

                metadata_str = transaction_dict.get("metadata")
                if metadata_str:
                    try:
                        metadata = json.loads(metadata_str)
                        transaction_dict["host_name"] = metadata.get("host_name", "N/A")
                        transaction_dict["plan_name"] = metadata.get("plan_name", "N/A")
                    except json.JSONDecodeError:
                        transaction_dict["host_name"] = "Error"
                        transaction_dict["plan_name"] = "Error"
                else:
                    transaction_dict["host_name"] = "N/A"
                    transaction_dict["plan_name"] = "N/A"

                transactions.append(transaction_dict)

    except sqlite3.Error as e:
        logging.error(f"Не удалось получить постраничный список транзакций: {e}")

    return transactions, total


def get_user_transactions(user_id: int, page: int = 1, per_page: int = 5) -> tuple[list[dict], int]:
    """Постраничная история операций одного пользователя (для 'История операций' в профиле).

    Возвращает список транзакций, общее число. Каждая транзакция дополняется полем 'kind':
    'top_up' | 'purchase' | 'admin_credit' | 'admin_deduct' | 'other', чтобы боту было проще
    выбрать иконку/знак суммы, не разбирая metadata заново на стороне хендлера.
    """
    offset = (page - 1) * per_page
    transactions = []
    total = 0
    try:
        with get_db_connection() as conn:
            conn.row_factory = sqlite3.Row
            cursor = conn.cursor()

            cursor.execute(
                "SELECT COUNT(*) FROM transactions WHERE user_id = ? AND status IN ('paid', 'internal')",
                (user_id,),
            )
            total = cursor.fetchone()[0]

            query = "SELECT * FROM transactions WHERE user_id = ? AND status IN ('paid', 'internal') ORDER BY created_date DESC LIMIT ? OFFSET ?"
            cursor.execute(query, (user_id, per_page, offset))

            for row in cursor.fetchall():
                transaction_dict = dict(row)
                metadata_str = transaction_dict.get("metadata")
                metadata = {}
                if metadata_str:
                    try:
                        metadata = json.loads(metadata_str)
                    except json.JSONDecodeError:
                        metadata = {}
                transaction_dict["host_name"] = metadata.get("host_name")
                transaction_dict["plan_name"] = metadata.get("plan_name")
                action = metadata.get("action")
                if action == "top_up":
                    kind = "top_up"
                elif action == "admin_credit":
                    kind = "admin_credit"
                elif action == "admin_deduct":
                    kind = "admin_deduct"
                elif action == "referral_reward":
                    kind = "referral"
                elif transaction_dict.get("plan_name") or transaction_dict.get("host_name"):
                    kind = "purchase"
                else:
                    kind = "other"
                transaction_dict["kind"] = kind
                transactions.append(transaction_dict)

    except sqlite3.Error as e:
        logging.error(f"Не удалось получить транзакции пользователя {user_id}: {e}")

    return transactions, total


def set_trial_used(telegram_id: int):
    """Отмечает, что пользователь уже использовал бесплатный триал."""
    try:
        with get_db_connection() as conn:
            cursor = conn.cursor()
            cursor.execute("UPDATE users SET trial_used = 1 WHERE telegram_id = ?", (telegram_id,))
            conn.commit()
            logging.info(
                f"Пробный период отмечен как использованный для пользователя {telegram_id}."
            )
    except sqlite3.Error as e:
        logging.error(
            f"Не удалось отметить пробный период как использованный для пользователя {telegram_id}: {e}"
        )


def add_new_key(
    user_id: int,
    host_name: str,
    xui_client_uuid: str,
    key_email: str,
    expiry_timestamp_ms: int,
):
    """Создаёт новую запись о ключе (подписке) в БД."""
    try:
        with get_db_connection() as conn:
            cursor = conn.cursor()
            expiry_date = datetime.fromtimestamp(expiry_timestamp_ms / 1000)
            cursor.execute(
                "INSERT INTO vpn_keys (user_id, host_name, xui_client_uuid, key_email, expiry_date, created_date) "
                "VALUES (?, ?, ?, ?, ?, ?)",
                (user_id, host_name, xui_client_uuid, key_email, expiry_date, datetime.now()),
            )
            new_key_id = cursor.lastrowid
            conn.commit()
            return new_key_id
    except sqlite3.Error as e:
        logging.error(f"Не удалось добавить новый ключ для пользователя {user_id}: {e}")
        return None


def delete_key_by_email(email: str) -> bool:
    """Удаляет ключ из БД по email."""
    try:
        with get_db_connection() as conn:
            cursor = conn.cursor()
            cursor.execute("DELETE FROM vpn_keys WHERE key_email = ?", (email,))
            affected = cursor.rowcount
            conn.commit()
            logger.debug(f"delete_key_by_email('{email}') затронуто строк={affected}")
            return affected > 0
    except sqlite3.Error as e:
        logging.error(f"Не удалось удалить ключ '{email}': {e}")
        return False


def get_user_keys(user_id: int):
    """Возвращает список ключей пользователя (алиас/вариант get_keys_for_user)."""
    try:
        with get_db_connection() as conn:
            conn.row_factory = sqlite3.Row
            cursor = conn.cursor()
            cursor.execute("SELECT * FROM vpn_keys WHERE user_id = ? ORDER BY key_id", (user_id,))
            keys = cursor.fetchall()
            return [dict(key) for key in keys]
    except sqlite3.Error as e:
        logging.error(f"Не удалось получить ключи пользователя {user_id}: {e}")
        return []


def get_key_by_email(key_email: str):
    """Возвращает ключ по key_email или None, если не найден."""
    try:
        with get_db_connection() as conn:
            conn.row_factory = sqlite3.Row
            cursor = conn.cursor()
            cursor.execute("SELECT * FROM vpn_keys WHERE key_email = ?", (key_email,))
            key_data = cursor.fetchone()
            return dict(key_data) if key_data else None
    except sqlite3.Error as e:
        logging.error(f"Не удалось получить ключ по email {key_email}: {e}")
        return None


def get_keys_by_base_prefix(base_prefix: str) -> list[dict]:
    """Находит все ключи (на любых хостах, с любым доменом), чья локальная часть email — это base_prefix без суффикса ИЛИ base_prefix_N с любым N (например, для base_prefix='vol_choke': vol_choke@..., vol_choke_2@..., vol_choke_3@...).

    Нужно для нормализации email по дате создания: один и тот же пользователь
    может иметь несколько ключей на разных хостах, и все они претендуют на
    один и тот же базовый локальный слаг — только суффикс их различает.
    key_email глобально UNIQUE по всей таблице (не по хосту), поэтому такую
    группу нужно видеть целиком, а не по одному хосту за раз.

    Отсортировано по created_date по возрастанию (сначала самый старый).
    """
    try:
        with get_db_connection() as conn:
            conn.row_factory = sqlite3.Row
            cursor = conn.cursor()
            # LIKE даёт грубый предфильтр (base_prefix + что угодно), точное
            # совпадение (сам base_prefix или base_prefix_N) проверяем в Python,
            # чтобы не зацепить, например, 'vol_chokeother' как ложное совпадение
            cursor.execute(
                "SELECT * FROM vpn_keys WHERE key_email LIKE ? ORDER BY created_date ASC",
                (f"{base_prefix}%",),
            )
            rows = [dict(r) for r in cursor.fetchall()]
    except sqlite3.Error as e:
        logging.error(f"Не удалось получить ключи по базовому префиксу '{base_prefix}': {e}")
        return []

    pattern = re.compile(rf"^{re.escape(base_prefix)}(_\d+)?@", re.IGNORECASE)
    return [r for r in rows if r.get("key_email") and pattern.match(r["key_email"])]


def update_key_info(key_id: int, new_xui_uuid: str, new_expiry_ms: int):
    """Обновляет UUID клиента и дату истечения у существующего ключа."""
    try:
        with get_db_connection() as conn:
            cursor = conn.cursor()
            expiry_date = datetime.fromtimestamp(new_expiry_ms / 1000)
            cursor.execute(
                "UPDATE vpn_keys SET xui_client_uuid = ?, expiry_date = ? WHERE key_id = ?",
                (new_xui_uuid, expiry_date, key_id),
            )
            conn.commit()
    except sqlite3.Error as e:
        logging.error(f"Не удалось обновить ключ {key_id}: {e}")


def set_key_auto_renew(key_id: int, enabled: bool) -> bool:
    """Включает/выключает автопродление с баланса для конкретного ключа.
    Возвращает True при успехе."""
    try:
        with get_db_connection() as conn:
            cursor = conn.cursor()
            cursor.execute(
                "UPDATE vpn_keys SET auto_renew_enabled = ? WHERE key_id = ?",
                (1 if enabled else 0, key_id),
            )
            conn.commit()
            return cursor.rowcount > 0
    except sqlite3.Error as e:
        logging.error(f"Не удалось изменить auto_renew_enabled для ключа {key_id}: {e}")
        return False


def set_key_last_plan(key_id: int, plan_id: int | None) -> None:
    """Запоминает, по какому тарифу ключ был последний раз куплен/продлён —
    именно этот тариф (месяцы + цена) используется при автопродлении.
    Вызывается при любой успешной покупке/продлении, вне зависимости от
    способа оплаты (баланс/карта/крипта) — все они проходят через
    process_successful_payment."""
    if plan_id is None:
        return
    try:
        with get_db_connection() as conn:
            cursor = conn.cursor()
            cursor.execute(
                "UPDATE vpn_keys SET last_plan_id = ? WHERE key_id = ?",
                (plan_id, key_id),
            )
            conn.commit()
    except sqlite3.Error as e:
        logging.error(f"Не удалось сохранить last_plan_id для ключа {key_id}: {e}")


def get_keys_due_for_auto_renewal(window_hours: int = 24) -> list[dict]:
    """Возвращает ключи с включённым автопродлением, у которых до истечения
    осталось не больше window_hours (и оно ещё не наступило — уже
    просроченные уходят по обычному пути чистки просроченных, не сюда).

    Дедупликация без отдельного состояния в памяти: как только ключ
    успешно продлён, expiry_date уходит далеко вперёд и естественным
    образом перестаёт попадать в этот запрос — то есть повторно тот же
    ключ не обработается в рамках одного и того же окна сам по себе,
    без необходимости хранить отдельный "уже обработан в этом цикле" кэш.
    """
    try:
        with get_db_connection() as conn:
            cursor = conn.cursor()
            cursor.execute(
                """
                SELECT vk.*, p.months AS plan_months, p.price AS plan_price,
                       p.host_name AS plan_host_name
                FROM vpn_keys vk
                JOIN plans p ON p.plan_id = vk.last_plan_id
                WHERE vk.auto_renew_enabled = 1
                  AND vk.last_plan_id IS NOT NULL
                  AND vk.expiry_date > datetime('now', 'localtime')
                  AND vk.expiry_date <= datetime('now', 'localtime', ?)
                """,
                (f"+{window_hours} hours",),
            )
            return [dict(row) for row in cursor.fetchall()]
    except sqlite3.Error as e:
        logging.error(f"Не удалось получить ключи для автопродления: {e}")
        return []


def update_key_host_and_info(
    key_id: int, new_host_name: str, new_xui_uuid: str, new_expiry_ms: int
):
    """Update key's host, UUID and expiry in a single transaction."""
    try:
        new_host_name = normalize_host_name(new_host_name)
        with get_db_connection() as conn:
            cursor = conn.cursor()
            expiry_date = datetime.fromtimestamp(new_expiry_ms / 1000)
            cursor.execute(
                "UPDATE vpn_keys SET host_name = ?, xui_client_uuid = ?, expiry_date = ? WHERE key_id = ?",
                (new_host_name, new_xui_uuid, expiry_date, key_id),
            )
            conn.commit()
    except sqlite3.Error as e:
        logging.error(f"Не удалось обновить хост и данные ключа {key_id}: {e}")


def get_next_key_number(user_id: int) -> int:
    """Возвращает следующий порядковый номер ключа для пользователя."""
    keys = get_user_keys(user_id)
    return len(keys) + 1


def get_keys_for_host(host_name: str) -> list[dict]:
    """Возвращает список ключей, привязанных к конкретному хосту."""
    try:
        host_name = normalize_host_name(host_name)
        with get_db_connection() as conn:
            conn.row_factory = sqlite3.Row
            cursor = conn.cursor()
            cursor.execute("SELECT * FROM vpn_keys WHERE TRIM(host_name) = TRIM(?)", (host_name,))
            keys = cursor.fetchall()
            return [dict(key) for key in keys]
    except sqlite3.Error as e:
        logging.error(f"Не удалось получить ключи для хоста '{host_name}': {e}")
        return []


def get_all_vpn_users():
    """Возвращает список всех пользователей, у которых есть хотя бы один ключ."""
    try:
        with get_db_connection() as conn:
            conn.row_factory = sqlite3.Row
            cursor = conn.cursor()
            cursor.execute("SELECT DISTINCT user_id FROM vpn_keys")
            users = cursor.fetchall()
            return [dict(user) for user in users]
    except sqlite3.Error as e:
        logging.error(f"Не удалось получить всех VPN-пользователей: {e}")
        return []


def update_key_status_from_server(key_email: str, xui_client_data):
    """Обновляет UUID клиента и дату истечения ключа данными, полученными с панели."""
    try:
        with get_db_connection() as conn:
            cursor = conn.cursor()
            if xui_client_data:
                expiry_date = datetime.fromtimestamp(xui_client_data.expiry_time / 1000)
                cursor.execute(
                    "UPDATE vpn_keys SET xui_client_uuid = ?, expiry_date = ? WHERE key_email = ?",
                    (xui_client_data.id, expiry_date, key_email),
                )
            else:
                cursor.execute("DELETE FROM vpn_keys WHERE key_email = ?", (key_email,))
            conn.commit()
    except sqlite3.Error as e:
        logging.error(f"Не удалось обновить статус ключа {key_email}: {e}")


def get_daily_stats_for_charts(days: int = 30) -> dict:
    """Возвращает данные для графиков: регистрации и покупки по дням за период."""
    stats = {"users": {}, "keys": {}}
    try:
        with get_db_connection() as conn:
            cursor = conn.cursor()
            query_users = """
                SELECT date(registration_date) as day, COUNT(*)
                FROM users
                WHERE registration_date >= date('now', ?)
                GROUP BY day
                ORDER BY day;
            """
            cursor.execute(query_users, (f"-{days} days",))
            for row in cursor.fetchall():
                stats["users"][row[0]] = row[1]

            query_keys = """
                SELECT date(created_date) as day, COUNT(*)
                FROM vpn_keys
                WHERE created_date >= date('now', ?)
                GROUP BY day
                ORDER BY day;
            """
            cursor.execute(query_keys, (f"-{days} days",))
            for row in cursor.fetchall():
                stats["keys"][row[0]] = row[1]
    except sqlite3.Error as e:
        logging.error(f"Не удалось получить дневную статистику для графиков: {e}")
    return stats


def get_recent_transactions(limit: int = 15) -> list[dict]:
    """Возвращает последние успешные транзакции (для дашборда админа)."""
    transactions = []
    try:
        with get_db_connection() as conn:
            conn.row_factory = sqlite3.Row
            cursor = conn.cursor()
            query = """
                SELECT
                    k.key_id,
                    k.host_name,
                    k.created_date,
                    u.telegram_id,
                    u.username
                FROM vpn_keys k
                JOIN users u ON k.user_id = u.telegram_id
                ORDER BY k.created_date DESC
                LIMIT ?;
            """
            cursor.execute(query, (limit,))
    except sqlite3.Error as e:
        logging.error(f"Не удалось получить последние транзакции: {e}")
    return transactions


def get_all_users() -> list[dict]:
    """Возвращает список всех пользователей."""
    try:
        with get_db_connection() as conn:
            conn.row_factory = sqlite3.Row
            cursor = conn.cursor()
            cursor.execute("SELECT * FROM users ORDER BY registration_date DESC")
            return [dict(row) for row in cursor.fetchall()]
    except sqlite3.Error as e:
        logging.error(f"Не удалось получить всех пользователей: {e}")
        return []


def get_users_by_segment(segment: str) -> list[dict]:
    """Возвращает пользователей, попадающих в один из следующих сегментов рассылки.

    - 'all'             — все пользователи (как get_all_users)
    - 'paid_no_active'  — платили хотя бы раз (total_spent > 0), но сейчас нет
                           ни одной подписки с ещё не истёкшим сроком действия
    - 'never_purchased' — ни разу не покупали подписку (total_spent <= 0)
    """
    try:
        with get_db_connection() as conn:
            conn.row_factory = sqlite3.Row
            cursor = conn.cursor()
            if segment == "paid_no_active":
                cursor.execute(
                    "SELECT * FROM users u "
                    "WHERE COALESCE(u.total_spent, 0) > 0 "
                    "AND NOT EXISTS ("
                    "    SELECT 1 FROM vpn_keys k "
                    "    WHERE k.user_id = u.telegram_id AND k.expiry_date > datetime('now','localtime')"
                    ") "
                    "ORDER BY u.registration_date DESC"
                )
            elif segment == "never_purchased":
                cursor.execute(
                    "SELECT * FROM users WHERE COALESCE(total_spent, 0) <= 0 ORDER BY registration_date DESC"
                )
            else:
                cursor.execute("SELECT * FROM users ORDER BY registration_date DESC")
            return [dict(row) for row in cursor.fetchall()]
    except sqlite3.Error as e:
        logging.error(f"Не удалось получить пользователей по сегменту '{segment}': {e}")
        return []


def get_users_paginated(
    page: int = 1, per_page: int = 20, q: str | None = None
) -> tuple[list[dict], int]:
    """Возвращает страницу пользователей и общее количество под фильтр.

    Фильтрация: по вхождению в telegram_id (как текст) или username (регистр не важен).
    Сортировка: по дате регистрации (новые сверху).
    """
    try:
        page = max(1, int(page or 1))
        per_page = max(1, min(100, int(per_page or 20)))
    except Exception:
        page, per_page = 1, 20
    offset = (page - 1) * per_page

    users: list[dict] = []
    total = 0
    try:
        with get_db_connection() as conn:
            conn.row_factory = sqlite3.Row
            cursor = conn.cursor()
            if q:
                q = (q or "").strip()
                like = f"%{q}%"
                # Total
                cursor.execute(
                    """
                    SELECT COUNT(*)
                    FROM users
                    WHERE CAST(telegram_id AS TEXT) LIKE ? OR username LIKE ? COLLATE NOCASE
                    """,
                    (like, like),
                )
                total = cursor.fetchone()[0] or 0
                # Page
                cursor.execute(
                    """
                    SELECT * FROM users
                    WHERE CAST(telegram_id AS TEXT) LIKE ? OR username LIKE ? COLLATE NOCASE
                    ORDER BY datetime(registration_date) DESC
                    LIMIT ? OFFSET ?
                    """,
                    (like, like, per_page, offset),
                )
            else:
                cursor.execute("SELECT COUNT(*) FROM users")
                total = cursor.fetchone()[0] or 0
                cursor.execute(
                    """
                    SELECT * FROM users
                    ORDER BY datetime(registration_date) DESC
                    LIMIT ? OFFSET ?
                    """,
                    (per_page, offset),
                )
            users = [dict(r) for r in cursor.fetchall()]
    except sqlite3.Error as e:
        logging.error(f"Не удалось получить постраничный список пользователей: {e}")
        return [], 0
    return users, total


def ban_user(telegram_id: int):
    """Банит пользователя (устанавливает is_banned)."""
    try:
        with get_db_connection() as conn:
            cursor = conn.cursor()
            cursor.execute("UPDATE users SET is_banned = 1 WHERE telegram_id = ?", (telegram_id,))
            conn.commit()
    except sqlite3.Error as e:
        logging.error(f"Не удалось заблокировать пользователя {telegram_id}: {e}")


def unban_user(telegram_id: int):
    """Снимает бан с пользователя."""
    try:
        with get_db_connection() as conn:
            cursor = conn.cursor()
            cursor.execute("UPDATE users SET is_banned = 0 WHERE telegram_id = ?", (telegram_id,))
            conn.commit()
    except sqlite3.Error as e:
        logging.error(f"Не удалось разблокировать пользователя {telegram_id}: {e}")


def delete_user_keys(user_id: int):
    """Удаляет все ключи пользователя из БД."""
    try:
        with get_db_connection() as conn:
            cursor = conn.cursor()
            cursor.execute("DELETE FROM vpn_keys WHERE user_id = ?", (user_id,))
            conn.commit()
    except sqlite3.Error as e:
        logging.error(f"Не удалось удалить ключи пользователя {user_id}: {e}")


def create_support_ticket(user_id: int, subject: str | None = None) -> int | None:
    """Создаёт новый тикет поддержки для пользователя."""
    try:
        with get_db_connection() as conn:
            cursor = conn.cursor()
            cursor.execute(
                "INSERT INTO support_tickets (user_id, subject, created_at, updated_at) VALUES (?, ?, ?, ?)",
                (user_id, subject, datetime.now(), datetime.now()),
            )
            conn.commit()
            return cursor.lastrowid
    except sqlite3.Error as e:
        logging.error(f"Не удалось создать тикет поддержки для пользователя {user_id}: {e}")
        return None


def add_support_message(ticket_id: int, sender: str, content: str) -> int | None:
    """Добавляет сообщение в переписку тикета поддержки."""
    try:
        with get_db_connection() as conn:
            cursor = conn.cursor()
            cursor.execute(
                "INSERT INTO support_messages (ticket_id, sender, content, created_at) VALUES (?, ?, ?, ?)",
                (ticket_id, sender, content, datetime.now()),
            )
            cursor.execute(
                "UPDATE support_tickets SET updated_at = datetime('now','localtime') WHERE ticket_id = ?",
                (ticket_id,),
            )
            conn.commit()
            return cursor.lastrowid
    except sqlite3.Error as e:
        logging.error(f"Не удалось добавить сообщение поддержки в тикет {ticket_id}: {e}")
        return None


def update_ticket_thread_info(
    ticket_id: int, forum_chat_id: str | None, message_thread_id: int | None
) -> bool:
    """Обновляет привязку тикета к теме форума поддержки (chat_id/thread_id)."""
    try:
        with get_db_connection() as conn:
            cursor = conn.cursor()
            cursor.execute(
                "UPDATE support_tickets SET forum_chat_id = ?, message_thread_id = ?, updated_at = datetime('now','localtime') WHERE ticket_id = ?",
                (forum_chat_id, message_thread_id, ticket_id),
            )
            conn.commit()
            return cursor.rowcount > 0
    except sqlite3.Error as e:
        logging.error(f"Не удалось обновить данные треда для тикета {ticket_id}: {e}")
        return False


def get_ticket(ticket_id: int) -> dict | None:
    """Возвращает тикет поддержки по id или None, если не найден."""
    try:
        with get_db_connection() as conn:
            conn.row_factory = sqlite3.Row
            cursor = conn.cursor()
            cursor.execute("SELECT * FROM support_tickets WHERE ticket_id = ?", (ticket_id,))
            row = cursor.fetchone()
            return dict(row) if row else None
    except sqlite3.Error as e:
        logging.error(f"Не удалось получить тикет {ticket_id}: {e}")
        return None


def get_ticket_by_thread(forum_chat_id: str, message_thread_id: int) -> dict | None:
    """Возвращает тикет поддержки по теме форума (chat_id + thread_id)."""
    try:
        with get_db_connection() as conn:
            conn.row_factory = sqlite3.Row
            cursor = conn.cursor()
            cursor.execute(
                "SELECT * FROM support_tickets WHERE forum_chat_id = ? AND message_thread_id = ?",
                (str(forum_chat_id), int(message_thread_id)),
            )
            row = cursor.fetchone()
            return dict(row) if row else None
    except sqlite3.Error as e:
        logging.error(
            f"Не удалось получить тикет по треду {forum_chat_id}/{message_thread_id}: {e}"
        )
        return None


def get_user_tickets(user_id: int, status: str | None = None) -> list[dict]:
    """Возвращает тикеты поддержки пользователя, опционально отфильтрованные по статусу."""
    try:
        with get_db_connection() as conn:
            conn.row_factory = sqlite3.Row
            cursor = conn.cursor()
            if status:
                cursor.execute(
                    "SELECT * FROM support_tickets WHERE user_id = ? AND status = ? ORDER BY updated_at DESC",
                    (user_id, status),
                )
            else:
                cursor.execute(
                    "SELECT * FROM support_tickets WHERE user_id = ? ORDER BY updated_at DESC",
                    (user_id,),
                )
            return [dict(r) for r in cursor.fetchall()]
    except sqlite3.Error as e:
        logging.error(f"Не удалось получить тикеты пользователя {user_id}: {e}")
        return []


def get_ticket_messages(ticket_id: int) -> list[dict]:
    """Возвращает все сообщения переписки конкретного тикета."""
    try:
        with get_db_connection() as conn:
            conn.row_factory = sqlite3.Row
            cursor = conn.cursor()
            cursor.execute(
                "SELECT * FROM support_messages WHERE ticket_id = ? ORDER BY created_at ASC",
                (ticket_id,),
            )
            return [dict(r) for r in cursor.fetchall()]
    except sqlite3.Error as e:
        logging.error(f"Не удалось получить сообщения тикета {ticket_id}: {e}")
        return []


def set_ticket_status(ticket_id: int, status: str) -> bool:
    """Меняет статус тикета поддержки (открыт/закрыт)."""
    try:
        with get_db_connection() as conn:
            cursor = conn.cursor()
            cursor.execute(
                "UPDATE support_tickets SET status = ?, updated_at = datetime('now','localtime') WHERE ticket_id = ?",
                (status, ticket_id),
            )
            conn.commit()
            return cursor.rowcount > 0
    except sqlite3.Error as e:
        logging.error(f"Не удалось установить статус '{status}' для тикета {ticket_id}: {e}")
        return False


def update_ticket_subject(ticket_id: int, subject: str) -> bool:
    """Обновляет тему тикета поддержки."""
    try:
        with get_db_connection() as conn:
            cursor = conn.cursor()
            cursor.execute(
                "UPDATE support_tickets SET subject = ?, updated_at = datetime('now','localtime') WHERE ticket_id = ?",
                (subject, ticket_id),
            )
            conn.commit()
            return cursor.rowcount > 0
    except sqlite3.Error as e:
        logging.error(f"Не удалось обновить тему тикета {ticket_id}: {e}")
        return False


def delete_ticket(ticket_id: int) -> bool:
    """Удаляет тикет поддержки вместе с перепиской."""
    try:
        with get_db_connection() as conn:
            cursor = conn.cursor()
            cursor.execute("DELETE FROM support_messages WHERE ticket_id = ?", (ticket_id,))
            cursor.execute("DELETE FROM support_tickets WHERE ticket_id = ?", (ticket_id,))
            conn.commit()
            return cursor.rowcount > 0
    except sqlite3.Error as e:
        logging.error(f"Не удалось удалить тикет {ticket_id}: {e}")
        return False


def get_tickets_paginated(
    page: int = 1, per_page: int = 20, status: str | None = None
) -> tuple[list[dict], int]:
    """Возвращает страницу тикетов поддержки для админ-панели."""
    offset = (page - 1) * per_page
    try:
        with get_db_connection() as conn:
            conn.row_factory = sqlite3.Row
            cursor = conn.cursor()
            if status:
                cursor.execute("SELECT COUNT(*) FROM support_tickets WHERE status = ?", (status,))
                total = cursor.fetchone()[0] or 0
                cursor.execute(
                    "SELECT * FROM support_tickets WHERE status = ? ORDER BY updated_at DESC LIMIT ? OFFSET ?",
                    (status, per_page, offset),
                )
            else:
                cursor.execute("SELECT COUNT(*) FROM support_tickets")
                total = cursor.fetchone()[0] or 0
                cursor.execute(
                    "SELECT * FROM support_tickets ORDER BY updated_at DESC LIMIT ? OFFSET ?",
                    (per_page, offset),
                )
            return [dict(r) for r in cursor.fetchall()], total
    except sqlite3.Error as e:
        logging.error("Не удалось получить постраничный список тикетов поддержки: %s", e)
        return [], 0


def get_open_tickets_count() -> int:
    """Возвращает количество открытых тикетов поддержки."""
    try:
        with get_db_connection() as conn:
            cursor = conn.cursor()
            cursor.execute("SELECT COUNT(*) FROM support_tickets WHERE status = 'open'")
            return cursor.fetchone()[0] or 0
    except sqlite3.Error as e:
        logging.error("Не удалось получить количество открытых тикетов: %s", e)
        return 0


def get_closed_tickets_count() -> int:
    """Возвращает количество закрытых тикетов поддержки."""
    try:
        with get_db_connection() as conn:
            cursor = conn.cursor()
            cursor.execute("SELECT COUNT(*) FROM support_tickets WHERE status = 'closed'")
            return cursor.fetchone()[0] or 0
    except sqlite3.Error as e:
        logging.error("Не удалось получить количество закрытых тикетов: %s", e)
        return 0


def get_all_tickets_count() -> int:
    """Возвращает общее количество тикетов поддержки."""
    try:
        with get_db_connection() as conn:
            cursor = conn.cursor()
            cursor.execute("SELECT COUNT(*) FROM support_tickets")
            return cursor.fetchone()[0] or 0
    except sqlite3.Error as e:
        logging.error("Не удалось получить общее количество тикетов: %s", e)
        return 0


# --- Host metrics helpers ---
def insert_host_metrics(host_name: str, metrics: dict) -> bool:
    """Insert a resource metrics row for host_name using dict from resource_monitor.get_host_metrics_via_ssh."""
    try:
        host_name_n = normalize_host_name(host_name)
        m = metrics or {}
        load = m.get("loadavg") or {}
        with get_db_connection() as conn:
            cursor = conn.cursor()
            cursor.execute(
                """
                INSERT INTO host_metrics (
                    host_name, cpu_percent, mem_percent, mem_used, mem_total,
                    disk_percent, disk_used, disk_total, load1, load5, load15,
                    uptime_seconds, ok, error, created_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    host_name_n,
                    (float(m.get("cpu_percent")) if m.get("cpu_percent") is not None else None),
                    (float(m.get("mem_percent")) if m.get("mem_percent") is not None else None),
                    int(m.get("mem_used")) if m.get("mem_used") is not None else None,
                    int(m.get("mem_total")) if m.get("mem_total") is not None else None,
                    (float(m.get("disk_percent")) if m.get("disk_percent") is not None else None),
                    int(m.get("disk_used")) if m.get("disk_used") is not None else None,
                    (int(m.get("disk_total")) if m.get("disk_total") is not None else None),
                    float(load.get("1m")) if load.get("1m") is not None else None,
                    float(load.get("5m")) if load.get("5m") is not None else None,
                    float(load.get("15m")) if load.get("15m") is not None else None,
                    (
                        float(m.get("uptime_seconds"))
                        if m.get("uptime_seconds") is not None
                        else None
                    ),
                    1 if (m.get("ok") in (True, 1, "1")) else 0,
                    str(m.get("error")) if m.get("error") else None,
                    datetime.now(),
                ),
            )
            conn.commit()
            return True
    except sqlite3.Error as e:
        logging.error(f"Ошибка при записи метрик хоста '{host_name}': {e}")
        return False


def get_host_metrics_recent(host_name: str, limit: int = 60) -> list[dict]:
    """Возвращает последние сохранённые метрики ресурсов хоста."""
    try:
        host_name_n = normalize_host_name(host_name)
        with get_db_connection() as conn:
            conn.row_factory = sqlite3.Row
            cursor = conn.cursor()
            cursor.execute(
                """
                SELECT host_name, cpu_percent, mem_percent, mem_used, mem_total,
                       disk_percent, disk_used, disk_total,
                       load1, load5, load15, uptime_seconds, ok, error, created_at
                FROM host_metrics
                WHERE TRIM(host_name) = TRIM(?)
                ORDER BY datetime(created_at) DESC
                LIMIT ?
                """,
                (host_name_n, int(limit)),
            )
            rows = cursor.fetchall()
            return [dict(r) for r in rows]
    except sqlite3.Error as e:
        logging.error(f"Ошибка при получении последних метрик хоста '{host_name}': {e}")
        return []


def get_latest_host_metrics(host_name: str) -> dict | None:
    """Возвращает самый свежий снимок метрик ресурсов хоста."""
    try:
        host_name_n = normalize_host_name(host_name)
        with get_db_connection() as conn:
            conn.row_factory = sqlite3.Row
            cursor = conn.cursor()
            cursor.execute(
                """
                SELECT * FROM host_metrics
                WHERE TRIM(host_name) = TRIM(?)
                ORDER BY datetime(created_at) DESC
                LIMIT 1
                """,
                (host_name_n,),
            )
            r = cursor.fetchone()
            return dict(r) if r else None
    except sqlite3.Error as e:
        logging.error(f"Ошибка при получении новейшей метрики хоста '{host_name}': {e}")
        return None


# --- Button Configs Functions ---
def get_button_configs(menu_type: str = None) -> list[dict]:
    """Get all button configurations, optionally filtered by menu_type."""
    try:
        with get_db_connection() as conn:
            conn.row_factory = sqlite3.Row
            cursor = conn.cursor()

            if menu_type:
                cursor.execute(
                    "SELECT * FROM button_configs WHERE menu_type = ? ORDER BY sort_order, id",
                    (menu_type,),
                )
            else:
                cursor.execute("SELECT * FROM button_configs ORDER BY menu_type, sort_order, id")

            return [dict(row) for row in cursor.fetchall()]
    except sqlite3.Error as e:
        logging.error(f"Не удалось получить конфигурации кнопок: {e}")
        return []


def get_button_config(button_id: int) -> dict | None:
    """Get a specific button configuration by ID."""
    try:
        with get_db_connection() as conn:
            conn.row_factory = sqlite3.Row
            cursor = conn.cursor()
            cursor.execute("SELECT * FROM button_configs WHERE id = ?", (button_id,))
            row = cursor.fetchone()
            return dict(row) if row else None
    except sqlite3.Error as e:
        logging.error(f"Не удалось получить конфигурацию кнопки {button_id}: {e}")
        return None


def create_button_config(config: dict) -> int | None:
    """Create a new button configuration. Returns the new ID or None on error."""
    try:
        with get_db_connection() as conn:
            cursor = conn.cursor()
            cursor.execute(
                """
                INSERT INTO button_configs (
                    menu_type, button_id, text, callback_data, url,
                    row_position, column_position, button_width, sort_order, is_active,
                    created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    config.get("menu_type", "main_menu"),
                    config.get("button_id", ""),
                    config.get("text", ""),
                    config.get("callback_data"),
                    config.get("url"),
                    config.get("row_position", 0),
                    config.get("column_position", 0),
                    config.get("button_width", 1),
                    config.get("sort_order", 0),
                    config.get("is_active", True),
                    datetime.now(),
                    datetime.now(),
                ),
            )
            return cursor.lastrowid
    except sqlite3.Error as e:
        logging.error(f"Не удалось создать конфигурацию кнопки: {e}")
        return None


def update_button_config(button_id: int, config: dict) -> bool:
    """Update an existing button configuration."""
    try:
        with get_db_connection() as conn:
            cursor = conn.cursor()
            cursor.execute(
                """
                UPDATE button_configs SET
                    text = ?, callback_data = ?, url = ?,
                    row_position = ?, column_position = ?, button_width = ?,
                    sort_order = ?, is_active = ?, updated_at = datetime('now','localtime')
                WHERE id = ?
                """,
                (
                    config.get("text", ""),
                    config.get("callback_data"),
                    config.get("url"),
                    config.get("row_position", 0),
                    config.get("column_position", 0),
                    config.get("button_width", 1),
                    config.get("sort_order", 0),
                    config.get("is_active", True),
                    button_id,
                ),
            )
            return cursor.rowcount > 0
    except sqlite3.Error as e:
        logging.error(f"Не удалось обновить конфигурацию кнопки {button_id}: {e}")
        return False


def delete_button_config(button_id: int) -> bool:
    """Delete a button configuration."""
    try:
        with get_db_connection() as conn:
            cursor = conn.cursor()
            cursor.execute("DELETE FROM button_configs WHERE id = ?", (button_id,))
            return cursor.rowcount > 0
    except sqlite3.Error as e:
        logging.error(f"Не удалось удалить конфигурацию кнопки {button_id}: {e}")
        return False


def reorder_button_configs(menu_type: str, button_orders: list[dict]) -> bool:
    """Reorder and reposition button configurations for a specific menu type.

    Accepts items with either 'id' or 'button_id'. Updates sort_order, row_position,
    column_position, and button_width.
    """
    try:
        with get_db_connection() as conn:
            cursor = conn.cursor()
            for order_data in button_orders:
                sort_order = int(order_data.get("sort_order", 0) or 0)
                row_pos = int(order_data.get("row_position", 0) or 0)
                col_pos = int(order_data.get("column_position", 0) or 0)
                btn_width = int(order_data.get("button_width", 1) or 1)

                # Try resolve target id
                btn_id = order_data.get("id")
                if not btn_id:
                    btn_key = order_data.get("button_id")
                    if not btn_key:
                        continue
                    cursor.execute(
                        "SELECT id FROM button_configs WHERE menu_type = ? AND button_id = ?",
                        (menu_type, btn_key),
                    )
                    row = cursor.fetchone()
                    if not row:
                        continue
                    btn_id = row[0]

                cursor.execute(
                    """
                    UPDATE button_configs
                    SET sort_order = ?, row_position = ?, column_position = ?, button_width = ?
                    WHERE id = ? AND menu_type = ?
                    """,
                    (sort_order, row_pos, col_pos, btn_width, btn_id, menu_type),
                )
            conn.commit()
            return True
    except sqlite3.Error as e:
        logging.error(f"Не удалось изменить порядок конфигураций кнопок для {menu_type}: {e}")
        return False


def migrate_existing_buttons() -> bool:
    """Migrate existing button configurations from settings to button_configs table."""
    try:
        with get_db_connection() as conn:
            cursor = conn.cursor()

            # Define button configurations for all menu types
            menu_configs = {
                "main_menu": [
                    # Row 0: Wide buttons (full width)
                    {
                        "button_id": "btn_try",
                        "callback_data": "get_trial",
                        "text": "🎁 Попробовать бесплатно",
                        "row_position": 0,
                        "column_position": 0,
                        "button_width": 2,
                    },
                    {
                        "button_id": "btn_profile",
                        "callback_data": "show_profile",
                        "text": "👤 Мой профиль",
                        "row_position": 1,
                        "column_position": 0,
                        "button_width": 2,
                    },
                    # Row 2: Two buttons
                    {
                        "button_id": "btn_my_keys",
                        "callback_data": "manage_keys",
                        "text": "🔑 Мои подписки ({count})",
                        "row_position": 2,
                        "column_position": 0,
                        "button_width": 1,
                    },
                    {
                        "button_id": "btn_buy_key",
                        "callback_data": "buy_new_key",
                        "text": "💳 Купить подписку",
                        "row_position": 2,
                        "column_position": 1,
                        "button_width": 1,
                    },
                    # Row 3: Two buttons
                    {
                        "button_id": "btn_top_up",
                        "callback_data": "top_up_start",
                        "text": "➕ Пополнить баланс",
                        "row_position": 3,
                        "column_position": 0,
                        "button_width": 1,
                    },
                    {
                        "button_id": "btn_referral",
                        "callback_data": "show_referral_program",
                        "text": "🤝 Реферальная программа",
                        "row_position": 3,
                        "column_position": 1,
                        "button_width": 1,
                    },
                    # Row 4: Two buttons
                    {
                        "button_id": "btn_support",
                        "callback_data": "show_help",
                        "text": "🆘 Поддержка",
                        "row_position": 4,
                        "column_position": 0,
                        "button_width": 1,
                    },
                    {
                        "button_id": "btn_about",
                        "callback_data": "show_about",
                        "text": "ℹ️ О проекте",
                        "row_position": 4,
                        "column_position": 1,
                        "button_width": 1,
                    },
                    # Row 5: Two buttons
                    {
                        "button_id": "btn_howto",
                        "callback_data": "howto_vless",
                        "text": "❓ Как использовать",
                        "row_position": 5,
                        "column_position": 0,
                        "button_width": 1,
                    },
                    {
                        "button_id": "btn_speed",
                        "callback_data": "user_speedtest",
                        "text": "⚡ Тест скорости",
                        "row_position": 5,
                        "column_position": 1,
                        "button_width": 1,
                    },
                    # Row 6: Wide button
                    {
                        "button_id": "btn_admin",
                        "callback_data": "admin_menu",
                        "text": "⚙️ Админка",
                        "row_position": 6,
                        "column_position": 0,
                        "button_width": 2,
                    },
                ],
                "admin_menu": [
                    {
                        "button_id": "admin_users",
                        "callback_data": "admin_users",
                        "text": "👥 Пользователи",
                        "row_position": 0,
                        "column_position": 0,
                        "button_width": 1,
                    },
                    {
                        "button_id": "admin_keys",
                        "callback_data": "admin_keys",
                        "text": "🔑 Подписки",
                        "row_position": 0,
                        "column_position": 1,
                        "button_width": 1,
                    },
                    {
                        "button_id": "admin_settings",
                        "callback_data": "admin_settings",
                        "text": "⚙️ Настройки",
                        "row_position": 1,
                        "column_position": 0,
                        "button_width": 1,
                    },
                    {
                        "button_id": "admin_stats",
                        "callback_data": "admin_stats",
                        "text": "📊 Статистика",
                        "row_position": 1,
                        "column_position": 1,
                        "button_width": 1,
                    },
                    {
                        "button_id": "admin_logs",
                        "callback_data": "admin_logs",
                        "text": "📝 Логи",
                        "row_position": 2,
                        "column_position": 0,
                        "button_width": 1,
                    },
                    {
                        "button_id": "admin_backup",
                        "callback_data": "admin_backup",
                        "text": "💾 Резервная копия",
                        "row_position": 2,
                        "column_position": 1,
                        "button_width": 1,
                    },
                    {
                        "button_id": "back_to_main",
                        "callback_data": "main_menu",
                        "text": "🏠 Главное меню",
                        "row_position": 3,
                        "column_position": 0,
                        "button_width": 2,
                    },
                ],
                "profile_menu": [
                    {
                        "button_id": "profile_info",
                        "callback_data": "profile_info",
                        "text": "ℹ️ Информация",
                        "row_position": 0,
                        "column_position": 0,
                        "button_width": 1,
                    },
                    {
                        "button_id": "profile_balance",
                        "callback_data": "profile_balance",
                        "text": "💰 Баланс",
                        "row_position": 0,
                        "column_position": 1,
                        "button_width": 1,
                    },
                    {
                        "button_id": "profile_keys",
                        "callback_data": "manage_keys",
                        "text": "🔑 Мои подписки",
                        "row_position": 1,
                        "column_position": 0,
                        "button_width": 1,
                    },
                    {
                        "button_id": "profile_referrals",
                        "callback_data": "show_referral_program",
                        "text": "🤝 Рефералы",
                        "row_position": 1,
                        "column_position": 1,
                        "button_width": 1,
                    },
                    {
                        "button_id": "back_to_main",
                        "callback_data": "main_menu",
                        "text": "🏠 Главное меню",
                        "row_position": 2,
                        "column_position": 0,
                        "button_width": 2,
                    },
                ],
                "support_menu": [
                    {
                        "button_id": "support_new",
                        "callback_data": "support_new_ticket",
                        "text": "📝 Новое обращение",
                        "row_position": 0,
                        "column_position": 0,
                        "button_width": 1,
                    },
                    {
                        "button_id": "support_my",
                        "callback_data": "support_my_tickets",
                        "text": "📋 Мои обращения",
                        "row_position": 0,
                        "column_position": 1,
                        "button_width": 1,
                    },
                    {
                        "button_id": "support_faq",
                        "callback_data": "support_faq",
                        "text": "❓ FAQ",
                        "row_position": 1,
                        "column_position": 0,
                        "button_width": 1,
                    },
                    {
                        "button_id": "support_contact",
                        "callback_data": "support_contact",
                        "text": "📞 Контакты",
                        "row_position": 1,
                        "column_position": 1,
                        "button_width": 1,
                    },
                    {
                        "button_id": "back_to_main",
                        "callback_data": "main_menu",
                        "text": "🏠 Главное меню",
                        "row_position": 2,
                        "column_position": 0,
                        "button_width": 2,
                    },
                ],
            }

            # Reset all button configs
            cursor.execute("DELETE FROM button_configs")
            logging.info("Сброшены все существующие конфигурации кнопок для повторной миграции")

            # Migrate buttons for each menu type
            for menu_type, button_settings in menu_configs.items():
                sort_order = 0
                for button_data in button_settings:
                    # Get the text from settings or use default
                    text = get_setting(button_data["button_id"]) or button_data["text"]

                    cursor.execute(
                        """
                        INSERT INTO button_configs (
                            menu_type, button_id, text, callback_data, row_position, column_position, button_width, sort_order, is_active,
                            created_at, updated_at
                        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                        """,
                        (
                            menu_type,
                            button_data["button_id"],
                            text,
                            button_data["callback_data"],
                            button_data["row_position"],
                            button_data["column_position"],
                            button_data["button_width"],
                            sort_order,
                            True,
                            datetime.now(),
                            datetime.now(),
                        ),
                    )
                    sort_order += 1

                logging.info(f"Успешно мигрировано {len(button_settings)} кнопок для {menu_type}")

            # Clean up any duplicates that might have been created
            cursor.execute("""
                DELETE FROM button_configs
                WHERE id NOT IN (
                    SELECT MIN(id)
                    FROM button_configs
                    GROUP BY menu_type, button_id
                )
            """)

            return True

    except sqlite3.Error as e:
        logging.error(f"Не удалось мигрировать существующие кнопки: {e}")
        return False


def cleanup_duplicate_buttons() -> bool:
    """Remove duplicate button configurations."""
    try:
        with get_db_connection() as conn:
            cursor = conn.cursor()

            # Remove duplicates, keeping the first occurrence
            cursor.execute("""
                DELETE FROM button_configs
                WHERE id NOT IN (
                    SELECT MIN(id)
                    FROM button_configs
                    WHERE menu_type = 'main_menu'
                    GROUP BY button_id
                )
            """)

            deleted_count = cursor.rowcount
            if deleted_count > 0:
                logging.info(f"Удалено {deleted_count} дублирующихся конфигураций кнопок")

            return True

    except sqlite3.Error as e:
        logging.error(f"Не удалось очистить дублирующиеся кнопки: {e}")
        return False


def reset_button_migration() -> bool:
    """Reset button migration to re-run with correct layout."""
    try:
        with get_db_connection() as conn:
            cursor = conn.cursor()

            # Delete all existing button configs for all menu types
            cursor.execute("DELETE FROM button_configs")
            deleted_count = cursor.rowcount
            logging.info(
                f"Удалено {deleted_count} существующих конфигураций кнопок для всех типов меню"
            )

            return True

    except sqlite3.Error as e:
        logging.error(f"Не удалось сбросить миграцию кнопок: {e}")
        return False


def force_button_migration() -> bool:
    """Force button migration by resetting and re-migrating."""
    try:
        logging.info("Запуск принудительной миграции кнопок...")
        reset_button_migration()
        migrate_existing_buttons()
        logging.info("Принудительная миграция кнопок успешно завершена")
        return True
    except Exception as e:
        logging.error(f"Ошибка при принудительной миграции кнопок: {e}")
        return False


# Resource metrics functions
def insert_resource_metric(
    scope: str,
    object_name: str,
    *,
    cpu_percent: float | None = None,
    mem_percent: float | None = None,
    disk_percent: float | None = None,
    load1: float | None = None,
    net_bytes_sent: int | None = None,
    net_bytes_recv: int | None = None,
    raw_json: str | None = None,
) -> int | None:
    """Insert a resource metric record."""
    try:
        with get_db_connection() as conn:
            cursor = conn.cursor()
            cursor.execute(
                """
                INSERT INTO resource_metrics (
                    scope, object_name, cpu_percent, mem_percent, disk_percent, load1,
                    net_bytes_sent, net_bytes_recv, raw_json, created_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    (scope or "").strip(),
                    (object_name or "").strip(),
                    cpu_percent,
                    mem_percent,
                    disk_percent,
                    load1,
                    net_bytes_sent,
                    net_bytes_recv,
                    raw_json,
                    datetime.now(),
                ),
            )
            conn.commit()
            return cursor.lastrowid
    except sqlite3.Error as e:
        logging.error("Не удалось записать метрику ресурса для %s/%s: %s", scope, object_name, e)
        return None


def get_latest_resource_metric(scope: str, object_name: str) -> dict | None:
    """Get the latest resource metric for a scope/object."""
    try:
        with get_db_connection() as conn:
            conn.row_factory = sqlite3.Row
            cursor = conn.cursor()
            cursor.execute(
                """
                SELECT * FROM resource_metrics
                WHERE scope = ? AND object_name = ?
                ORDER BY created_at DESC
                LIMIT 1
                """,
                ((scope or "").strip(), (object_name or "").strip()),
            )
            row = cursor.fetchone()
            return dict(row) if row else None
    except sqlite3.Error as e:
        logging.error(
            "Не удалось получить последнюю метрику ресурса для %s/%s: %s",
            scope,
            object_name,
            e,
        )
        return None


def get_metrics_series(
    scope: str, object_name: str, *, since_hours: int = 24, limit: int = 500
) -> list[dict]:
    """Get a series of resource metrics for a scope/object."""
    try:
        with get_db_connection() as conn:
            conn.row_factory = sqlite3.Row
            cursor = conn.cursor()

            # Ensure we have at least some data for the requested period
            if since_hours == 1:
                hours_filter = 2
            else:
                hours_filter = max(1, int(since_hours))

            cursor.execute(
                """
                SELECT created_at, cpu_percent, mem_percent, disk_percent, load1
                FROM resource_metrics
                WHERE scope = ? AND object_name = ?
                  AND created_at >= datetime('now', ?)
                ORDER BY created_at ASC
                LIMIT ?
                """,
                (
                    (scope or "").strip(),
                    (object_name or "").strip(),
                    f"-{hours_filter} hours",
                    max(10, int(limit)),
                ),
            )
            rows = cursor.fetchall() or []

            # Debug logging
            logging.debug(
                f"get_metrics_series: {scope}/{object_name}, since_hours={since_hours}, найдено записей: {len(rows)}"
            )

            return [dict(r) for r in rows]
    except sqlite3.Error as e:
        logging.error("Не удалось получить серию метрик для %s/%s: %s", scope, object_name, e)
        return []


def get_key_by_uuid(uuid: str):
    """
    Находит запись о VPN-ключе в локальной базе данных по X-UI UUID клиента.

    Используется в основном для:
    • проверки существования ключа перед созданием нового
    • получения данных ключа при продлении / удалении / синхронизации
    • поиска мастера по UUID (самый надёжный способ идентификации)

    Args:
        uuid: Строковый UUID клиента из панели X-UI (обычно версия 4 uuid.uuid4())

    Returns:
        dict | None: Словарь со всеми полями записи из таблицы vpn_keys
                     или None, если запись не найдена или произошла ошибка БД

    Важно:
    • UUID хранится в поле xui_client_uuid и должен быть уникальным
    • Функция использует row_factory = sqlite3.Row → результат как именованный словарь
    """
    try:
        # -------------------------------------------------------------------------
        # 1. Открытие соединения с базой данных
        # -------------------------------------------------------------------------
        with get_db_connection() as conn:
            # Включаем режим, при котором строки возвращаются как sqlite3.Row
            # Это позволяет обращаться к полям по имени: row['email'], row['expiry_timestamp_ms']
            conn.row_factory = sqlite3.Row
            cursor = conn.cursor()

            # -------------------------------------------------------------------------
            # 2. Выполнение запроса по уникальному идентификатору
            # -------------------------------------------------------------------------
            cursor.execute(
                """
                SELECT *
                FROM vpn_keys
                WHERE xui_client_uuid = ?
                """,
                (uuid,),
            )

            # -------------------------------------------------------------------------
            # 3. Получение первой (и единственной) подходящей записи
            # -------------------------------------------------------------------------
            row = cursor.fetchone()

            # Если запись найдена — преобразуем sqlite3.Row в обычный dict
            if row:
                return dict(row)

            # Запись не найдена
            return None

    except sqlite3.Error as e:
        # -------------------------------------------------------------------------
        # 4. Обработка ошибок базы данных
        # -------------------------------------------------------------------------
        logging.error(f"Ошибка базы данных при поиске ключа по UUID {uuid}: {e}", exc_info=True)
        return None

    except Exception as e:
        # -------------------------------------------------------------------------
        # 5. Ловушка на непредвиденные ошибки (редко, но полезно)
        # -------------------------------------------------------------------------
        logging.error(f"Неизвестная ошибка в get_key_by_uuid({uuid}): {e}", exc_info=True)
        return None


def delete_promo_code(code: str) -> bool:
    """
    Полностью удаляет промокод из таблицы promo_codes в базе данных.

    Особенности:
    • Приводит код к верхнему регистру и убирает пробелы (нормализация)
    • Возвращает True только если запись действительно была удалена (rowcount > 0)
    • Защищена от SQL-инъекций через параметризованный запрос
    • Логирует ошибки SQLite с уровнем error

    Args:
        code: Строковый промокод (регистр не важен, пробелы игнорируются)

    Returns:
        bool:
            True  — промокод найден и успешно удалён
            False — промокод не найден, пустой ввод или ошибка базы данных

    Примеры:
        delete_promo_code("ABC123")   → True (если существовал)
        delete_promo_code("  abc123 ") → True (нормализуется к ABC123)
        delete_promo_code("")         → False
        delete_promo_code("UNKNOWN")  → False (не найден)
    """
    # -------------------------------------------------------------------------
    # 1. Нормализация входного кода
    # -------------------------------------------------------------------------
    code_s = (code or "").strip().upper()

    if not code_s:
        logger.debug("Попытка удаления пустого промокода — операция пропущена")
        return False

    # -------------------------------------------------------------------------
    # 2. Удаление записи из базы данных
    # -------------------------------------------------------------------------
    try:
        with get_db_connection() as conn:
            cursor = conn.cursor()

            # Параметризованный запрос — защита от SQL-инъекций
            cursor.execute("DELETE FROM promo_codes WHERE code = ?", (code_s,))

            conn.commit()

            # Проверяем, была ли удалена хотя бы одна строка
            deleted = cursor.rowcount > 0

            if deleted:
                logger.info(f"Промокод '{code_s}' успешно удалён из базы данных")
            else:
                logger.debug(f"Промокод '{code_s}' не найден в базе — удаление не выполнено")

            return deleted

    # -------------------------------------------------------------------------
    # 3. Обработка ошибок базы данных
    # -------------------------------------------------------------------------
    except sqlite3.Error as e:
        logger.error(f"Ошибка SQLite при удалении промокода '{code_s}': {e}", exc_info=True)
        return False

    except Exception as e:
        # На случай совсем неожиданных ошибок (редко)
        logger.error(f"Неизвестная ошибка при удалении промокода '{code_s}': {e}", exc_info=True)
        return False


def delete_all_promo_codes() -> int:
    """
    Полностью удаляет все промокоды из таблицы promo_codes.

    Returns:
        int: количество удалённых записей (0, если промокодов не было или произошла ошибка).
    """
    try:
        with get_db_connection() as conn:
            cursor = conn.cursor()
            cursor.execute("DELETE FROM promo_codes")
            conn.commit()
            deleted_count = cursor.rowcount if cursor.rowcount and cursor.rowcount > 0 else 0
            logger.info(f"Удалено промокодов: {deleted_count}")
            return deleted_count
    except sqlite3.Error as e:
        logger.error(f"Ошибка SQLite при удалении всех промокодов: {e}", exc_info=True)
        return 0
    except Exception as e:
        logger.error(f"Неизвестная ошибка при удалении всех промокодов: {e}", exc_info=True)
        return 0


def get_latest_transactions(limit: int = 10) -> list[dict]:
    """Получает список последних успешных транзакций из базы данных."""
    try:
        with get_db_connection() as conn:
            conn.row_factory = sqlite3.Row
            cursor = conn.cursor()
            # Фильтруем по статусу 'paid' и сортируем от самых новых к старым
            cursor.execute(
                """SELECT created_date, amount_rub, username, payment_method, user_id
                   FROM transactions
                   WHERE status = 'paid'
                   ORDER BY created_date DESC
                   LIMIT ?""",
                (limit,),
            )
            return [dict(row) for row in cursor.fetchall()]
    except sqlite3.Error as e:
        logging.error(f"Не удалось получить последние транзакции: {e}")
        return []
