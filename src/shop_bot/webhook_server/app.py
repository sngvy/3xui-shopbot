"""Flask-приложение: веб-панель администратора и приём вебхуков платёжных систем."""

import asyncio
import base64
import hashlib
import hmac
import html as html_escape
import json
import logging
import os
import secrets
import time
import urllib.parse
import urllib.request
import uuid
from datetime import datetime
from functools import wraps
from hmac import compare_digest
from math import ceil

from aiogram import html
from flask import (
    Flask,
    current_app,
    flash,
    jsonify,
    redirect,
    render_template,
    request,
    send_file,
    session,
    url_for,
)
from flask_wtf.csrf import CSRFProtect, generate_csrf
from yookassa import Payment

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)
logging.getLogger("werkzeug").setLevel(logging.WARNING)

from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup, WebAppInfo
from aiogram.utils.keyboard import InlineKeyboardBuilder

from shop_bot.bot import handlers, keyboards
from shop_bot.data_manager import (
    backup_manager,
    database,
    resource_monitor,
    speedtest_runner,
)
from shop_bot.data_manager.database import (
    add_new_key,
    add_support_message,
    adjust_user_balance,
    ban_user,
    create_host,
    create_plan,
    delete_host,
    delete_key_by_id,
    delete_plan,
    delete_ticket,
    delete_user_keys,
    find_and_complete_pending_transaction,
    find_and_complete_ton_transaction,
    get_all_hosts,
    get_all_keys,
    get_all_settings,
    get_all_tickets_count,
    get_all_users,
    get_balance,
    get_closed_tickets_count,
    get_daily_stats_for_charts,
    get_host,
    get_key_by_email,
    get_key_by_id,
    get_latest_speedtest,
    get_open_tickets_count,
    get_paginated_transactions,
    get_plans_for_host,
    get_referrals_for_user,
    get_setting,
    get_speedtests,
    get_ticket,
    get_ticket_messages,
    get_tickets_paginated,
    get_total_keys_count,
    get_total_spent_sum,
    get_user,
    get_user_count,
    get_user_keys,
    set_ticket_status,
    unban_user,
    update_host_name,
    update_host_ssh_settings,
    update_host_subscription_url,
    update_host_url,
    update_key_comment,
    update_key_info,
    update_plan,
    update_setting,
)
from shop_bot.modules import xui_api
from shop_bot.support_bot_controller import SupportBotController

_bot_controller = None
_support_bot_controller = SupportBotController()

ALL_SETTINGS_KEYS = [
    "panel_login",
    "panel_password",
    "about_text",
    "terms_url",
    "privacy_url",
    "support_user",
    "support_text",
    # Editable content from admin UI
    "main_menu_text",
    "howto_android_text",
    "howto_ios_text",
    "howto_windows_text",
    "howto_linux_text",
    # Button texts
    "btn_try",
    "btn_profile",
    "btn_my_keys",
    "btn_buy_key",
    "btn_top_up",
    "btn_referral",
    "btn_support",
    "btn_about",
    "btn_howto",
    "btn_admin",
    "btn_back_to_menu",
    "btn_channel",
    "btn_terms",
    "btn_privacy",
    "btn_howto_android",
    "btn_howto_ios",
    "btn_howto_windows",
    "btn_howto_linux",
    # Extra button labels
    "btn_back",
    "btn_back_to_plans",
    "btn_back_to_key",
    "btn_back_to_keys",
    "btn_extend_key",
    "btn_show_qr",
    "btn_instruction",
    "btn_switch_server",
    "btn_skip_email",
    "btn_go_to_payment",
    "btn_check_payment",
    "btn_pay_with_balance",
    "btn_support_open",
    "btn_support_new_ticket",
    "btn_support_my_tickets",
    "btn_support_external",
    "channel_url",
    "telegram_bot_token",
    "telegram_bot_username",
    "admin_telegram_id",
    "yookassa_shop_id",
    "yookassa_secret_key",
    "sbp_enabled",
    "receipt_email",
    "cryptobot_token",
    "heleket_merchant_id",
    "heleket_api_key",
    "domain",
    "referral_percentage",
    "referral_discount",
    "ton_wallet_address",
    "tonapi_key",
    "force_subscription",
    "trial_enabled",
    "trial_duration_days",
    "enable_referrals",
    "minimum_withdrawal",
    # Реферальные начисления: альтернативный фиксированный бонус
    "enable_fixed_referral_bonus",
    "fixed_referral_bonus_amount",
    # Тип начисления реферальной системы (без стартового бонуса)
    "referral_reward_type",
    "referral_on_start_referrer_amount",
    "support_forum_chat_id",
    "support_bot_token",
    "support_bot_username",
    # UI
    "panel_brand_title",
    # Backups
    "backup_interval_days",
    # Monitoring
    "monitoring_enabled",
    "monitoring_interval_sec",
    "monitoring_cpu_threshold",
    "monitoring_mem_threshold",
    "monitoring_disk_threshold",
    "monitoring_alert_cooldown_sec",
    # Telegram Stars
    "stars_enabled",
    "stars_per_rub",
    "stars_title",
    "stars_description",
    # YooMoney (separate)
    "yoomoney_enabled",
    "yoomoney_wallet",
    "yoomoney_secret",
    "yoomoney_api_token",
    "yoomoney_client_id",
    "yoomoney_client_secret",
    "yoomoney_redirect_uri",
]

REDIR_URL = os.getenv("REDIR_URL")


def create_webhook_app(bot_controller_instance):
    """Создаёт и настраивает Flask-приложение (админ-панель + вебхуки платежей)."""
    global _bot_controller
    _bot_controller = bot_controller_instance

    app_file_path = os.path.abspath(__file__)
    app_dir = os.path.dirname(app_file_path)
    template_dir = os.path.join(app_dir, "templates")
    template_file = os.path.join(template_dir, "login.html")

    logger.debug("--- ДИАГНОСТИЧЕСКАЯ ИНФОРМАЦИЯ ---")
    logger.debug(f"Текущая рабочая директория: {os.getcwd()}")
    logger.debug(f"Путь к исполняемому app.py: {app_file_path}")
    logger.debug(f"Директория app.py: {app_dir}")
    logger.debug(f"Ожидаемая директория шаблонов: {template_dir}")
    logger.debug(f"Ожидаемый путь к login.html: {template_file}")
    logger.debug(f"Директория шаблонов существует? -> {os.path.isdir(template_dir)}")
    logger.debug(f"Файл login.html существует? -> {os.path.isfile(template_file)}")
    logger.debug("--- КОНЕЦ ДИАГНОСТИКИ ---")

    flask_app = Flask(__name__, template_folder="templates", static_folder="static")

    # SECRET_KEY из окружения или сгенерированный на лету (без хардкода)
    flask_app.config["SECRET_KEY"] = os.getenv("SHOPBOT_SECRET_KEY") or secrets.token_hex(32)
    from datetime import timedelta

    flask_app.config["PERMANENT_SESSION_LIFETIME"] = timedelta(days=30)

    # CSRF защита для всех POST форм в панели; вебхуки будут исключены
    csrf = CSRFProtect()
    csrf.init_app(flask_app)

    # Страница спидтеста как Telegram Mini App (см. USE_WEBAPP_SPEEDTEST в
    # keyboards.py) — speed.html.
    @flask_app.route("/speed")
    def speedtest_webapp_page():
        speed_html_path = os.path.join(
            os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "speed.html"
        )
        if not os.path.isfile(speed_html_path):
            logger.error(f"/speed: файл не найден по пути {speed_html_path}")
            return "Not Found", 404
        return send_file(speed_html_path, mimetype="text/html")

    # Страница "Статус сервисов" как Telegram Mini App — mon.html.
    @flask_app.route("/mon")
    def monitoring_webapp_page():
        mon_html_path = os.path.join(
            os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "mon.html"
        )
        if not os.path.isfile(mon_html_path):
            logger.error(f"/mon: файл не найден по пути {mon_html_path}")
            return "Not Found", 404
        return send_file(mon_html_path, mimetype="text/html")

    # Собственный redirect-эндпоинт.
    @flask_app.route("/redir")
    def redirect_handler():
         redir_html_path = os.path.join(
             os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "redir.html"
         )
         if not os.path.isfile(redir_html_path):
             logger.error(f"/redir: файл не найден по пути {redir_html_path}")
             return "Not Found", 404
         return send_file(redir_html_path, mimetype="text/html")

    @flask_app.context_processor
    def inject_current_year():
        # Добавляем csrf_token в шаблоны для meta и скрытых полей
        return {"current_year": datetime.now().year, "csrf_token": generate_csrf}

    def login_required(f):
        @wraps(f)
        def decorated_function(*args, **kwargs):
            if "logged_in" not in session:
                return redirect(url_for("login_page"))
            return f(*args, **kwargs)

        return decorated_function

    @flask_app.route("/login", methods=["GET", "POST"])
    def login_page():
        settings = get_all_settings()
        if request.method == "POST":
            if request.form.get("username") == settings.get("panel_login") and request.form.get(
                "password"
            ) == settings.get("panel_password"):
                session["logged_in"] = True
                # Remember-me: делаем сессию постоянной при установленном чекбоксе
                session.permanent = bool(request.form.get("remember_me"))
                return redirect(url_for("dashboard_page"))
            else:
                flash("Неверный логин или пароль", "danger")
        return render_template("login.html")

    @flask_app.route("/logout", methods=["POST"])
    @login_required
    def logout_page():
        session.pop("logged_in", None)
        flash("Вы успешно вышли.", "success")
        return redirect(url_for("login_page"))

    def get_common_template_data():
        bot_status = _bot_controller.get_status()
        support_bot_status = _support_bot_controller.get_status()
        settings = get_all_settings()
        required_for_start = [
            "telegram_bot_token",
            "telegram_bot_username",
            "admin_telegram_id",
        ]
        required_support_for_start = [
            "support_bot_token",
            "support_bot_username",
            "admin_telegram_id",
        ]
        all_settings_ok = all(settings.get(key) for key in required_for_start)
        support_settings_ok = all(settings.get(key) for key in required_support_for_start)
        try:
            open_tickets_count = get_open_tickets_count()
            closed_tickets_count = get_closed_tickets_count()
            all_tickets_count = get_all_tickets_count()
        except Exception:
            open_tickets_count = 0
            closed_tickets_count = 0
            all_tickets_count = 0
        return {
            "bot_status": bot_status,
            "all_settings_ok": all_settings_ok,
            "support_bot_status": support_bot_status,
            "support_settings_ok": support_settings_ok,
            "open_tickets_count": open_tickets_count,
            "closed_tickets_count": closed_tickets_count,
            "all_tickets_count": all_tickets_count,
            "brand_title": settings.get("panel_brand_title") or "T‑Shift VPN",
        }

    @flask_app.route("/brand-title", methods=["POST"])
    @login_required
    def update_brand_title_route():
        title = (request.form.get("title") or "").strip()
        if not title:
            return jsonify({"ok": False, "error": "empty"}), 400
        try:
            update_setting("panel_brand_title", title)
            return jsonify({"ok": True, "title": title})
        except Exception as e:
            return jsonify({"ok": False, "error": str(e)}), 500

    @flask_app.route("/monitor/host/<host_name>/metrics.json")
    @login_required
    def monitor_host_metrics_json(host_name: str):
        try:
            limit = int(request.args.get("limit", "60"))
        except Exception:
            limit = 60
        try:
            items = database.get_host_metrics_recent(host_name, limit=limit)
            return jsonify({"ok": True, "items": items})
        except Exception as e:
            return jsonify({"ok": False, "error": str(e)}), 500

    @flask_app.route("/")
    @login_required
    def index():
        return redirect(url_for("dashboard_page"))

    @flask_app.route("/dashboard")
    @login_required
    def dashboard_page():
        hosts = []
        try:
            hosts = get_all_hosts()
            for h in hosts:
                try:
                    h["latest_speedtest"] = get_latest_speedtest(h["host_name"])
                except Exception:
                    h["latest_speedtest"] = None
        except Exception:
            hosts = []
        stats = {
            "user_count": get_user_count(),
            "total_keys": get_total_keys_count(),
            "total_spent": get_total_spent_sum(),
            "host_count": len(hosts),
        }

        page = request.args.get("page", 1, type=int)
        per_page = 8

        transactions, total_transactions = get_paginated_transactions(page=page, per_page=per_page)
        total_pages = ceil(total_transactions / per_page)

        chart_data = get_daily_stats_for_charts(days=30)
        common_data = get_common_template_data()

        return render_template(
            "dashboard.html",
            stats=stats,
            chart_data=chart_data,
            transactions=transactions,
            current_page=page,
            total_pages=total_pages,
            hosts=hosts,
            **common_data,
        )

    @flask_app.route("/dashboard/run-speedtests", methods=["POST"])
    @login_required
    def run_speedtests_route():
        try:
            speedtest_runner.run_speedtests_for_all_hosts()
            return jsonify({"ok": True})
        except Exception as e:
            return jsonify({"ok": False, "error": str(e)}), 500

    # Partials for dashboard fragments (auto-update without reload)
    @flask_app.route("/dashboard/stats.partial")
    @login_required
    def dashboard_stats_partial():
        stats = {
            "user_count": get_user_count(),
            "total_keys": get_total_keys_count(),
            "total_spent": get_total_spent_sum(),
            "host_count": len(get_all_hosts()),
        }
        common_data = get_common_template_data()
        return render_template("partials/dashboard_stats.html", stats=stats, **common_data)

    @flask_app.route("/dashboard/transactions.partial")
    @login_required
    def dashboard_transactions_partial():
        page = request.args.get("page", 1, type=int)
        per_page = 8
        transactions, total_transactions = get_paginated_transactions(page=page, per_page=per_page)
        return render_template("partials/dashboard_transactions.html", transactions=transactions)

    @flask_app.route("/dashboard/charts.json")
    @login_required
    def dashboard_charts_json():
        data = get_daily_stats_for_charts(days=30)
        return jsonify(data)

    # --- Resource Monitor ---
    @flask_app.route("/monitor")
    @login_required
    def monitor_page():
        common_data = get_common_template_data()
        # Add hosts and ssh_targets for monitor template
        hosts = get_all_hosts()
        ssh_targets = []  # SSH targets not implemented yet
        common_data.update({"hosts": hosts, "ssh_targets": ssh_targets})
        return render_template("monitor.html", **common_data)

    @flask_app.route("/monitor/local.json")
    @login_required
    def monitor_local_json():
        try:
            data = resource_monitor.get_local_metrics()
            return jsonify(data)
        except Exception as e:
            return jsonify({"ok": False, "error": str(e)}), 500

    @flask_app.route("/monitor/hosts.json")
    @login_required
    def monitor_hosts_json():
        try:
            data = resource_monitor.collect_hosts_metrics()
            return jsonify(data)
        except Exception as e:
            return jsonify({"ok": False, "error": str(e)}), 500

    @flask_app.route("/monitor/host/<host_name>.json")
    @login_required
    def monitor_host_json(host_name: str):
        try:
            host = get_host(host_name)
            if not host:
                return jsonify({"ok": False, "error": "host not found"}), 404
            data = resource_monitor.get_host_metrics_via_ssh(host)
            return jsonify(data)
        except Exception as e:
            return jsonify({"ok": False, "error": str(e)}), 500

    @flask_app.route("/monitor/metrics/<scope>/<object_name>.json")
    @login_required
    def monitor_metrics_json(scope: str, object_name: str):
        try:
            since_hours = int(request.args.get("since_hours", "24"))
            limit = int(request.args.get("limit", "500"))
            items = database.get_metrics_series(
                scope, object_name, since_hours=since_hours, limit=limit
            )
            return jsonify({"ok": True, "items": items})
        except Exception as e:
            return jsonify({"ok": False, "error": str(e)}), 500

    # --- Support partials ---
    @flask_app.route("/support/table.partial")
    @login_required
    def support_table_partial():
        status = request.args.get("status") or None
        page = request.args.get("page", 1, type=int)
        per_page = 12
        tickets, total = get_tickets_paginated(page=page, per_page=per_page, status=status)
        return render_template("partials/support_table.html", tickets=tickets)

    @flask_app.route("/support/open-count.partial")
    @login_required
    def support_open_count_partial():
        try:
            count = get_open_tickets_count() or 0
        except Exception:
            count = 0
        # Возвращаем готовый HTML-бейдж (или пустую строку)
        if count and count > 0:
            html = '<span class="badge bg-green-lt" title="Открытые тикеты">' f"{count}</span>"
        else:
            html = ""
        return html, 200, {"Content-Type": "text/html; charset=utf-8"}

    @flask_app.route("/users")
    @login_required
    def users_page():
        # Параметры пагинации и поиска
        page = request.args.get("page", 1, type=int)
        per_page = request.args.get("per_page", 20, type=int)
        q = (request.args.get("q") or "").strip()

        # Получаем ограниченный набор пользователей с серверной фильтрацией
        from shop_bot.data_manager.database import get_users_paginated

        users, total = get_users_paginated(page=page, per_page=per_page, q=q or None)

        for user in users:
            uid = user["telegram_id"]
            user["user_keys"] = get_user_keys(uid)
            try:
                user["balance"] = get_balance(uid)
                user["referrals"] = get_referrals_for_user(uid)
            except Exception:
                user["balance"] = 0.0
                user["referrals"] = []

        total_pages = max(1, ceil(total / per_page)) if total else 1
        common_data = get_common_template_data()
        return render_template(
            "users.html",
            users=users,
            current_page=page,
            total_pages=total_pages,
            total_users=total,
            per_page=per_page,
            q=q,
            **common_data,
        )

    # Partial: users table tbody
    @flask_app.route("/users/table.partial")
    @login_required
    def users_table_partial():
        page = request.args.get("page", 1, type=int)
        per_page = request.args.get("per_page", 20, type=int)
        q = (request.args.get("q") or "").strip()
        from shop_bot.data_manager.database import get_users_paginated

        users, total = get_users_paginated(page=page, per_page=per_page, q=q or None)
        for user in users:
            uid = user["telegram_id"]
            user["user_keys"] = get_user_keys(uid)
            try:
                user["balance"] = get_balance(uid)
                user["referrals"] = get_referrals_for_user(uid)
            except Exception:
                user["balance"] = 0.0
                user["referrals"] = []
        return render_template("partials/users_table.html", users=users)

    @flask_app.route("/users/<int:user_id>/balance/adjust", methods=["POST"])
    @login_required
    def adjust_balance_route(user_id: int):
        try:
            delta = float(request.form.get("delta", "0") or "0")
        except ValueError:
            # AJAX?
            wants_json = (
                "application/json" in (request.headers.get("Accept") or "")
                or request.headers.get("X-Requested-With") == "XMLHttpRequest"
            )
            if wants_json:
                return jsonify({"ok": False, "error": "invalid_amount"}), 400
            flash("Некорректная сумма изменения баланса.", "danger")
            return redirect(url_for("users_page"))

        ok = adjust_user_balance(user_id, delta)
        message = "Баланс изменён." if ok else "Не удалось изменить баланс."
        category = "success" if ok else "danger"
        wants_json = (
            "application/json" in (request.headers.get("Accept") or "")
            or request.headers.get("X-Requested-With") == "XMLHttpRequest"
        )
        if wants_json:
            return jsonify({"ok": ok, "message": message})
        flash(message, category)
        # Telegram-уведомление пользователю (через запущенный цикл событий бота)
        try:
            if ok:
                bot = _bot_controller.get_bot_instance()
                if bot:
                    sign = "+" if delta >= 0 else ""
                    text = f"💳 Ваш баланс был изменён администратором: {sign}{delta:.2f} RUB\nТекущий баланс: {get_balance(user_id):.2f} RUB"
                    loop = current_app.config.get("EVENT_LOOP")
                    if loop and loop.is_running():
                        asyncio.run_coroutine_threadsafe(
                            bot.send_message(chat_id=user_id, text=text), loop
                        )
                        logger.info(
                            f"Запланирована отправка уведомления о балансе пользователю {user_id}"
                        )
                    else:
                        # Fallback, если по какой-то причине нет общего цикла (не рекомендуется, но лучше чем молча не отправить)
                        logger.warning(
                            "Цикл событий (EVENT_LOOP) не запущен; использую резервный asyncio.run для уведомления о балансе"
                        )
                        asyncio.run(bot.send_message(chat_id=user_id, text=text))
                else:
                    logger.warning(
                        "Экземпляр бота отсутствует; не могу отправить уведомление о балансе"
                    )
        except Exception as e:
            logger.warning(f"Не удалось отправить уведомление о балансе: {e}")
        return redirect(url_for("users_page"))

    @flask_app.route("/admin/keys")
    @login_required
    def admin_keys_page():
        keys = []
        try:
            keys = get_all_keys()
        except Exception:
            keys = []
        hosts = []
        try:
            hosts = get_all_hosts()
        except Exception:
            hosts = []
        users = []
        try:
            users = get_all_users()
        except Exception:
            users = []
        common_data = get_common_template_data()
        return render_template(
            "admin_keys.html", keys=keys, hosts=hosts, users=users, **common_data
        )

    # Partial: admin keys table tbody
    @flask_app.route("/admin/keys/table.partial")
    @login_required
    def admin_keys_table_partial():
        keys = []
        try:
            keys = get_all_keys()
        except Exception:
            keys = []
        return render_template("partials/admin_keys_table.html", keys=keys)

    @flask_app.route("/admin/hosts/<host_name>/plans")
    @login_required
    def admin_get_plans_for_host_json(host_name: str):
        try:
            plans = get_plans_for_host(host_name)
            data = [
                {
                    "plan_id": p.get("plan_id"),
                    "plan_name": p.get("plan_name"),
                    "months": p.get("months"),
                    "price": p.get("price"),
                }
                for p in plans
            ]
            return jsonify({"ok": True, "items": data})
        except Exception as e:
            return jsonify({"ok": False, "error": str(e)}), 500

    @flask_app.route("/admin/keys/create", methods=["POST"])
    @login_required
    def create_key_route():
        try:
            user_id = int(request.form.get("user_id"))
            host_name = (request.form.get("host_name") or "").strip()
            xui_uuid = (request.form.get("xui_client_uuid") or "").strip()
            key_email = (request.form.get("key_email") or "").strip()
            expiry = request.form.get("expiry_date") or ""
            # Ожидаем datetime-local, конвертируем в ms
            from datetime import datetime

            expiry_ms = int(datetime.fromisoformat(expiry).timestamp() * 1000) if expiry else 0
        except Exception:
            flash("Проверьте поля ключа.", "danger")
            return redirect(request.referrer or url_for("admin_keys_page"))
        # Если UUID не указан — генерируем автоматически, как при выдаче ключа в боте
        if not xui_uuid:
            xui_uuid = str(uuid.uuid4())
        # 1) Создать/обновить клиента на XUI-хосте
        result = None
        try:
            result = asyncio.run(
                xui_api.create_or_update_key_on_host(
                    host_name, key_email, expiry_timestamp_ms=expiry_ms or None
                )
            )
        except Exception as e:
            logger.error(f"Не удалось создать/обновить ключ на хосте: {e}")
            result = None
        if not result:
            flash("Не удалось создать ключ на хосте. Проверьте доступность XUI.", "danger")
            return redirect(request.referrer or url_for("admin_keys_page"))

        # Обновляем UUID и expiry на основании ответа панели
        try:
            xui_uuid = result.get("client_uuid") or xui_uuid
            expiry_ms = result.get("expiry_timestamp_ms") or expiry_ms
        except Exception:
            pass

        # 2) Сохранить в БД
        new_id = add_new_key(user_id, host_name, xui_uuid, key_email, expiry_ms or 0)
        flash(
            ("Ключ добавлен." if new_id else "Ошибка при добавлении ключа."),
            "success" if new_id else "danger",
        )

        # 3) Уведомление пользователю в Telegram (без email, с пометкой, что ключ выдан администратором)
        try:
            bot = _bot_controller.get_bot_instance()
            if bot and new_id:
                kb = None
                text = (
                    "🔐 <b>Ваша подписка готова!</b>\n\n"
                    f"🗺️ <b>Сервер:</b> {host_name}\n"
                    "Выдана администратором вручную.\n"
                )
                if result and result.get("connection_string"):
                    cs = html_escape.escape(result["connection_string"])
                    text += (
                        f"\n🔗 <b>Основная подписка:</b>\n{html.code(cs)}\n"
                        f"▪️🇷🇺 Российский интернет ➡️ напрямую\n"
                        f"▪️🌍 Зарубежный интернет ➡️ через прокси\n"
                        f"▪️⭐️ Рекомендуем для Happ и INCY\n\n"
                        f"🔀 <b>Альтернативная подписка:</b>\n{html.code(cs.replace('/sub/', '/json/'))}\n"
                        f"▪️🌍 Весь интернет ➡️ через прокси\n"
                        f"▪️🥷 Надежно перенаправляем российский трафик на свои серверы\n"
                        f"▪️⭐️ Рекомендуем для v2RayTun и других клиентов. Добавляется через копирование и вставку ссылки\n"
                    )
                    raw_cs = result["connection_string"]
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
                loop = current_app.config.get("EVENT_LOOP")
                if loop and loop.is_running():
                    asyncio.run_coroutine_threadsafe(
                        bot.send_message(
                            chat_id=user_id,
                            text=text,
                            parse_mode="HTML",
                            disable_web_page_preview=True,
                            reply_markup=kb,
                        ),
                        loop,
                    )
                else:
                    asyncio.run(
                        bot.send_message(
                            chat_id=user_id,
                            text=text,
                            parse_mode="HTML",
                            disable_web_page_preview=True,
                            reply_markup=kb,
                        )
                    )
        except Exception as e:
            logger.warning(f"Не удалось уведомить пользователя о новом ключе: {e}")
        return redirect(request.referrer or url_for("admin_keys_page"))

    @flask_app.route("/admin/keys/create-ajax", methods=["POST"])
    @login_required
    def create_key_ajax_route():
        try:
            user_id = int(request.form.get("user_id"))
            host_name = (request.form.get("host_name") or "").strip()
            xui_uuid = (request.form.get("xui_client_uuid") or "").strip()
            key_email = (request.form.get("key_email") or "").strip()
            expiry = request.form.get("expiry_date") or ""
            from datetime import datetime

            expiry_ms = int(datetime.fromisoformat(expiry).timestamp() * 1000) if expiry else 0
        except Exception as e:
            return jsonify({"ok": False, "error": f"invalid input: {e}"}), 400

        if not xui_uuid:
            xui_uuid = str(uuid.uuid4())

        try:
            result = asyncio.run(
                xui_api.create_or_update_key_on_host(
                    host_name, key_email, expiry_timestamp_ms=expiry_ms or None
                )
            )
        except Exception as e:
            result = None
            logger.error(f"create_key_ajax_route: ошибка панели/хоста: {e}")
        if not result:
            return jsonify({"ok": False, "error": "host_failed"}), 500

        # Sync DB
        new_id = add_new_key(
            user_id,
            host_name,
            result.get("client_uuid") or xui_uuid,
            key_email,
            result.get("expiry_timestamp_ms") or expiry_ms or 0,
        )

        # Notify user (без email, с пометкой про администратора)
        try:
            bot = _bot_controller.get_bot_instance()
            if bot and new_id:
                kb = None
                text = (
                    "🔐 <b>Ваша подписка готова!</b>\n\n"
                    f"🗺️ <b>Сервер:</b> {host_name}\n"
                    "Выдана администратором вручную.\n"
                )
                if result and result.get("connection_string"):
                    cs = html_escape.escape(result["connection_string"])
                    text += (
                        f"\n🔗 <b>Основная подписка:</b>\n{html.code(cs)}\n"
                        f"▪️🇷🇺 Российский интернет ➡️ напрямую\n"
                        f"▪️🌍 Зарубежный интернет ➡️ через прокси\n"
                        f"▪️⭐️ Рекомендуем для Happ и INCY\n\n"
                        f"🔀 <b>Альтернативная подписка:</b>\n{html.code(cs.replace('/sub/', '/json/'))}\n"
                        f"▪️🌍 Весь интернет ➡️ через прокси\n"
                        f"▪️🥷 Надежно перенаправляем российский трафик на свои серверы\n"
                        f"▪️⭐️ Рекомендуем для v2RayTun и других клиентов. Добавляется через копирование и вставку ссылки\n"
                    )
                    raw_cs = result["connection_string"]
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
                loop = current_app.config.get("EVENT_LOOP")
                if loop and loop.is_running():
                    asyncio.run_coroutine_threadsafe(
                        bot.send_message(
                            chat_id=user_id,
                            text=text,
                            parse_mode="HTML",
                            disable_web_page_preview=True,
                            reply_markup=kb,
                        ),
                        loop,
                    )
                else:
                    asyncio.run(
                        bot.send_message(
                            chat_id=user_id,
                            text=text,
                            parse_mode="HTML",
                            disable_web_page_preview=True,
                            reply_markup=kb,
                        )
                    )
        except Exception as e:
            logger.warning(f"Не удалось уведомить пользователя (ajax): {e}")

        return jsonify(
            {
                "ok": True,
                "key_id": new_id,
                "uuid": result.get("client_uuid"),
                "expiry_ms": result.get("expiry_timestamp_ms"),
                "connection": result.get("connection_string"),
            }
        )

    @flask_app.route("/admin/keys/generate-gift-email")
    @login_required
    def generate_gift_email_route():
        """Сгенерировать уникальный email для подарочного ключа (без привязки к Telegram)."""
        try:
            for _ in range(12):
                candidate_email = (
                    f"gift-{int(time.time())}_{secrets.token_hex(2)}@{xui_api.EMAIL_DOMAIN}"
                )
                if not get_key_by_email(candidate_email):
                    return jsonify({"ok": True, "email": candidate_email})
            return jsonify({"ok": False, "error": "no_unique_email"}), 500
        except Exception as e:
            return jsonify({"ok": False, "error": str(e)}), 500

    @flask_app.route("/admin/keys/create-standalone-ajax", methods=["POST"])
    @login_required
    def create_key_standalone_ajax_route():
        """Создать ключ из панели: персональный (с user_id) или подарочный (user_id=0)."""
        key_type = (request.form.get("key_type") or "personal").strip()
        try:
            if key_type == "gift":
                user_id = 0
            else:
                user_id = int(request.form.get("user_id"))
            host_name = (request.form.get("host_name") or "").strip()
            xui_uuid = (request.form.get("xui_client_uuid") or "").strip()
            key_email = (request.form.get("key_email") or "").strip()
            expiry = request.form.get("expiry_date") or ""
            comment = (request.form.get("comment") or "").strip()
            from datetime import datetime as _dt

            expiry_ms = int(_dt.fromisoformat(expiry).timestamp() * 1000) if expiry else 0
        except Exception as e:
            print(f"Ошибка ввода: {e}")
            raise SystemExit(1)

        if key_type == "gift" and not key_email:
            try:
                for _ in range(12):
                    candidate_email = (
                        f"gift-{int(time.time())}_{secrets.token_hex(2)}@{xui_api.EMAIL_DOMAIN}"
                    )
                    if not get_key_by_email(candidate_email):
                        key_email = candidate_email
                        break
            except Exception:
                pass

        if not xui_uuid:
            xui_uuid = str(uuid.uuid4())

        try:
            result = asyncio.run(
                xui_api.create_or_update_key_on_host(
                    host_name, key_email, expiry_timestamp_ms=expiry_ms or None
                )
            )
        except Exception as e:
            result = None
            logger.error(f"create_key_standalone_ajax_route: ошибка панели/хоста: {e}")
        if not result:
            print("Ошибка: сервер не вернул клиента")
            raise SystemExit(1)

        new_id = add_new_key(
            user_id,
            host_name,
            result.get("client_uuid") or xui_uuid,
            key_email,
            result.get("expiry_timestamp_ms") or expiry_ms or 0,
        )
        if comment and new_id:
            try:
                update_key_comment(int(new_id), comment)
            except Exception:
                pass

        if key_type != "gift" and user_id:
            try:
                bot = _bot_controller.get_bot_instance()
                if bot and new_id:
                    kb = None
                    text = (
                        "🔐 <b>Ваша подписка готова!</b>\n\n"
                        f"🗺️ <b>Сервер:</b> {host_name}\n"
                        "Выдана администратором вручную.\n"
                    )
                    if result and result.get("connection_string"):
                        cs = html_escape.escape(result["connection_string"])
                        text += (
                            f"\n🔗 <b>Основная подписка:</b>\n{html.code(cs)}\n"
                            f"▪️🇷🇺 Российский интернет ➡️ напрямую\n"
                            f"▪️🌍 Зарубежный интернет ➡️ через прокси\n"
                            f"▪️⭐️ Рекомендуем для Happ и INCY\n\n"
                            f"🔀 <b>Альтернативная подписка:</b>\n{html.code(cs.replace('/sub/', '/json/'))}\n"
                            f"▪️🌍 Весь интернет ➡️ через прокси\n"
                            f"▪️🥷 Надежно перенаправляем российский трафик на свои серверы\n"
                            f"▪️⭐️ Рекомендуем для v2RayTun и других клиентов. Добавляется через копирование и вставку ссылки\n"
                        )
                        raw_cs = result["connection_string"]
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
                    loop = current_app.config.get("EVENT_LOOP")
                    if loop and loop.is_running():
                        asyncio.run_coroutine_threadsafe(
                            bot.send_message(
                                chat_id=user_id,
                                text=text,
                                parse_mode="HTML",
                                disable_web_page_preview=True,
                                reply_markup=kb,
                            ),
                            loop,
                        )
                    else:
                        asyncio.run(
                            bot.send_message(
                                chat_id=user_id,
                                text=text,
                                parse_mode="HTML",
                                disable_web_page_preview=True,
                                reply_markup=kb,
                            )
                        )
            except Exception as notify_err:
                logger.warning(f"Не удалось уведомить пользователя (standalone ajax): {notify_err}")

        return jsonify(
            {
                "ok": True,
                "key_id": new_id,
                "uuid": result.get("client_uuid"),
                "expiry_ms": result.get("expiry_timestamp_ms"),
                "connection": result.get("connection_string"),
            }
        )

    @flask_app.route("/admin/keys/generate-email")
    @login_required
    def generate_key_email_route():
        try:
            user_id = int(request.args.get("user_id"))
        except Exception:
            return jsonify({"ok": False, "error": "invalid user_id"}), 400
        try:
            user = get_user(user_id) or {}
            raw_username = (user.get("username") or f"user{user_id}").lower()
            import re

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
            return jsonify({"ok": True, "email": candidate_email})
        except Exception as e:
            return jsonify({"ok": False, "error": str(e)}), 500

    @flask_app.route("/admin/keys/<int:key_id>/delete", methods=["POST"])
    @login_required
    def delete_key_route(key_id: int):
        # Пытаемся удалить с сервера и из БД
        try:
            key = get_key_by_id(key_id)
            if key:
                try:
                    asyncio.run(xui_api.delete_client_on_host(key["host_name"], key["key_email"]))
                except Exception:
                    pass
        except Exception:
            pass
        ok = delete_key_by_id(key_id)
        flash(
            "Ключ удалён." if ok else "Не удалось удалить ключ.",
            "success" if ok else "danger",
        )
        return redirect(request.referrer or url_for("admin_keys_page"))

    @flask_app.route("/admin/keys/<int:key_id>/adjust-expiry", methods=["POST"])
    @login_required
    def adjust_key_expiry_route(key_id: int):
        try:
            delta_days = int(request.form.get("delta_days", "0"))
        except Exception:
            return jsonify({"ok": False, "error": "invalid_delta"}), 400
        key = get_key_by_id(key_id)
        if not key:
            return jsonify({"ok": False, "error": "not_found"}), 404
        try:
            # Текущая дата истечения
            cur_expiry = key.get("expiry_date")
            from datetime import datetime, timedelta

            if isinstance(cur_expiry, str):
                try:
                    from datetime import datetime as dt

                    exp_dt = dt.fromisoformat(cur_expiry)
                except Exception:
                    # Fallback: если в БД дата как 'YYYY-MM-DD HH:MM:SS'
                    try:
                        exp_dt = datetime.strptime(cur_expiry, "%Y-%m-%d %H:%M:%S")
                    except Exception:
                        exp_dt = datetime.now()
            else:
                exp_dt = cur_expiry or datetime.now()
            new_dt = exp_dt + timedelta(days=delta_days)
            new_ms = int(new_dt.timestamp() * 1000)

            # 1) Применяем новый срок на 3xui (чтобы дата в панели совпадала с реальной)
            try:
                result = asyncio.run(
                    xui_api.create_or_update_key_on_host(
                        host_name=key.get("host_name"),
                        email=key.get("key_email"),
                        expiry_timestamp_ms=new_ms,
                    )
                )
            except Exception as e:
                logger.error(
                    f"Не удалось продлить ключ {key.get('key_email')} на "
                    f"хосте {key.get('host_name')} через панель: {e}",
                    exc_info=True,
                )
                result = None
            if not result or not result.get("expiry_timestamp_ms"):
                return jsonify({"ok": False, "error": "xui_update_failed"}), 500

            # 2) Сохраняем в БД (обновляем UUID, если изменился, и дату истечения)
            client_uuid = result.get("client_uuid") or key.get("xui_client_uuid") or ""
            update_key_info(key_id, client_uuid, int(result.get("expiry_timestamp_ms")))

            # Уведомим пользователя о продлении/сокращении срока
            try:
                user_id = key.get("user_id")
                new_ms_final = int(result.get("expiry_timestamp_ms"))
                from datetime import datetime as _dt

                new_dt_local = _dt.fromtimestamp(new_ms_final / 1000)
                text = (
                    "🗓️ <b>Срок вашей подписки изменён администратором</b>\n\n"
                    f"🗺️ <b>Сервер:</b> {key.get('host_name')}\n"
                    f"🆔 <b>Email подписки:</b> {key.get('key_email')}\n"
                    f"🚧 <b>Новая дата истечения:<b> {new_dt_local.strftime('%Y-%m-%d %H:%M')}"
                )
                if user_id:
                    bot = _bot_controller.get_bot_instance()
                    loop = current_app.config.get("EVENT_LOOP")
                    if bot and loop and loop.is_running():
                        asyncio.run_coroutine_threadsafe(
                            bot.send_message(chat_id=user_id, text=text), loop
                        )
                    elif bot:
                        asyncio.run(bot.send_message(chat_id=user_id, text=text))
            except Exception:
                pass

            return jsonify({"ok": True, "new_expiry_ms": int(result.get("expiry_timestamp_ms"))})
        except Exception as e:
            return jsonify({"ok": False, "error": str(e)}), 500

    @flask_app.route("/admin/keys/sweep-expired", methods=["POST"])
    @login_required
    def sweep_expired_keys_route():
        from datetime import datetime

        removed = 0
        failed = 0
        now = datetime.now()
        keys = get_all_keys()
        for k in keys:
            exp = k.get("expiry_date")
            exp_dt = None
            try:
                if isinstance(exp, str):
                    try:
                        from datetime import datetime as dt

                        exp_dt = dt.fromisoformat(exp)
                    except Exception:
                        # Fallback: если в БД дата как 'YYYY-MM-DD HH:MM:SS'
                        try:
                            exp_dt = datetime.strptime(exp, "%Y-%m-%d %H:%M:%S")
                        except Exception:
                            exp_dt = None
                else:
                    exp_dt = exp
            except Exception:
                exp_dt = None
            if not exp_dt or exp_dt > now:
                continue
            # Истёкший — пробуем удалить на сервере и в БД, уведомляем пользователя
            try:
                try:
                    asyncio.run(
                        xui_api.delete_client_on_host(k.get("host_name"), k.get("key_email"))
                    )
                except Exception:
                    pass
                delete_key_by_id(k.get("key_id"))
                removed += 1
                # Уведомление пользователю о автоудалении
                try:
                    bot = _bot_controller.get_bot_instance()
                    loop = current_app.config.get("EVENT_LOOP")
                    text = (
                        "⚠️ <b>Ваша подписка была автоматически удалена по истечении срока</b>\n\n"
                        f"🗺️ <b>Сервер:</b> {k.get('host_name')}\n🆔 <b>Email:</b> {k.get('key_email')}\n\n"
                        "При необходимости вы можете оформить новую подписку."
                    )
                    if bot and loop and loop.is_running():
                        asyncio.run_coroutine_threadsafe(
                            bot.send_message(chat_id=k.get("user_id"), text=text), loop
                        )
                    else:
                        asyncio.run(bot.send_message(chat_id=k.get("user_id"), text=text))
                except Exception:
                    pass
            except Exception:
                failed += 1
        flash(
            f"Удалено истёкших ключей: {removed}. Ошибок: {failed}.",
            "success" if failed == 0 else "warning",
        )
        return redirect(request.referrer or url_for("admin_keys_page"))

    @flask_app.route("/admin/keys/<int:key_id>/comment", methods=["POST"])
    @login_required
    def update_key_comment_route(key_id: int):
        comment = (request.form.get("comment") or "").strip()
        ok = update_key_comment(key_id, comment)
        flash(
            "Комментарий обновлён." if ok else "Не удалось обновить комментарий.",
            "success" if ok else "danger",
        )
        return redirect(request.referrer or url_for("admin_keys_page"))

    # --- Host SSH settings update ---
    @flask_app.route("/admin/hosts/ssh/update", methods=["POST"])
    @login_required
    def update_host_ssh_route():
        host_name = (request.form.get("host_name") or "").strip()
        ssh_host = (request.form.get("ssh_host") or "").strip() or None
        ssh_port_raw = (request.form.get("ssh_port") or "").strip()
        ssh_user = (request.form.get("ssh_user") or "").strip() or None
        ssh_password = request.form.get("ssh_password")  # Allow empty to clear
        ssh_key_path = (request.form.get("ssh_key_path") or "").strip() or None
        ssh_port = None
        try:
            ssh_port = int(ssh_port_raw) if ssh_port_raw else None
        except Exception:
            ssh_port = None
        ok = update_host_ssh_settings(
            host_name,
            ssh_host=ssh_host,
            ssh_port=ssh_port,
            ssh_user=ssh_user,
            ssh_password=ssh_password,
            ssh_key_path=ssh_key_path,
        )
        flash(
            "SSH-параметры обновлены." if ok else "Не удалось обновить SSH-параметры.",
            "success" if ok else "danger",
        )
        return redirect(request.referrer or url_for("settings_page"))

    # --- Host speedtest run & fetch ---
    @flask_app.route("/admin/hosts/<host_name>/speedtest/run", methods=["POST"])
    @login_required
    def run_host_speedtest_route(host_name: str):
        method = (request.form.get("method") or "").strip().lower()
        try:
            if method == "ssh":
                res = asyncio.run(speedtest_runner.run_and_store_ssh_speedtest(host_name))
            elif method == "net":
                res = asyncio.run(speedtest_runner.run_and_store_net_probe(host_name))
            else:
                # Both
                res = asyncio.run(speedtest_runner.run_both_for_host(host_name))
        except Exception as e:
            res = {"ok": False, "error": str(e)}
        wants_json = (
            "application/json" in (request.headers.get("Accept") or "")
            or request.headers.get("X-Requested-With") == "XMLHttpRequest"
        )
        if wants_json:
            return jsonify(res)
        flash(
            (
                "Тест выполнен."
                if res and res.get("ok")
                else f"Ошибка теста: {res.get('error') if res else 'unknown'}"
            ),
            "success" if res and res.get("ok") else "danger",
        )
        return redirect(request.referrer or url_for("settings_page"))

    @flask_app.route("/admin/hosts/<host_name>/speedtests.json")
    @login_required
    def host_speedtests_json(host_name: str):
        try:
            limit = int(request.args.get("limit") or 20)
        except Exception:
            limit = 20
        try:
            items = get_speedtests(host_name, limit=limit) or []
            return jsonify({"ok": True, "items": items})
        except Exception as e:
            return jsonify({"ok": False, "error": str(e)}), 500

    @flask_app.route("/admin/speedtests/run-all", methods=["POST"])
    @login_required
    def run_all_speedtests_route():
        # Запустить тесты для всех хостов (оба варианта)
        try:
            hosts = get_all_hosts()
        except Exception:
            hosts = []
        errors = []
        ok_count = 0
        for h in hosts:
            name = h.get("host_name")
            if not name:
                continue
            try:
                res = asyncio.run(speedtest_runner.run_both_for_host(name))
                if res and res.get("ok"):
                    ok_count += 1
                else:
                    errors.append(f"{name}: {res.get('error') if res else 'unknown'}")
            except Exception as e:
                errors.append(f"{name}: {e}")

        wants_json = (
            "application/json" in (request.headers.get("Accept") or "")
            or request.headers.get("X-Requested-With") == "XMLHttpRequest"
        )
        if wants_json:
            return jsonify(
                {
                    "ok": len(errors) == 0,
                    "done": ok_count,
                    "total": len(hosts),
                    "errors": errors,
                }
            )
        if errors:
            flash(
                f"Выполнено для {ok_count}/{len(hosts)}. Ошибки: {'; '.join(errors[:3])}{'...' if len(errors) > 3 else ''}",
                "warning",
            )
        else:
            flash(
                f"Тесты скорости выполнены для всех хостов: {ok_count}/{len(hosts)}",
                "success",
            )
        return redirect(request.referrer or url_for("dashboard_page"))

    # --- Host speedtest auto-install ---
    @flask_app.route("/admin/hosts/<host_name>/speedtest/install", methods=["POST"])
    @login_required
    def auto_install_speedtest_route(host_name: str):
        # Supports both HTML form and AJAX
        try:
            res = asyncio.run(speedtest_runner.auto_install_speedtest_on_host(host_name))
        except Exception as e:
            res = {"ok": False, "log": str(e)}
        wants_json = (
            "application/json" in (request.headers.get("Accept") or "")
            or request.headers.get("X-Requested-With") == "XMLHttpRequest"
        )
        if wants_json:
            return jsonify({"ok": bool(res.get("ok")), "log": res.get("log")})
        flash(
            (
                "Установка завершена успешно."
                if res.get("ok")
                else "Не удалось установить speedtest на сервер."
            ),
            "success" if res.get("ok") else "danger",
        )
        # Сохраним логи в flash (урезанно)
        try:
            log = res.get("log") or ""
            short = "\n".join((log.splitlines() or [])[-20:])
            if short:
                flash(short, "secondary")
        except Exception:
            pass
        return redirect(request.referrer or url_for("settings_page"))

    @flask_app.route("/admin/balance")
    @login_required
    def admin_balance_page():
        try:
            user_id = request.args.get("user_id", type=int)
        except Exception:
            user_id = None
        user = None
        balance = None
        referrals = []
        if user_id:
            try:
                user = get_user(user_id)
                balance = get_balance(user_id)
                referrals = get_referrals_for_user(user_id)
            except Exception:
                pass
        common_data = get_common_template_data()
        return render_template(
            "admin_balance.html",
            user=user,
            balance=balance,
            referrals=referrals,
            **common_data,
        )

    @flask_app.route("/support")
    @login_required
    def support_list_page():
        status = request.args.get("status")
        page = request.args.get("page", 1, type=int)
        per_page = 12
        tickets, total = get_tickets_paginated(
            page=page,
            per_page=per_page,
            status=status if status in ["open", "closed"] else None,
        )
        total_pages = ceil(total / per_page) if per_page else 1
        open_count = get_open_tickets_count()
        closed_count = get_closed_tickets_count()
        all_count = get_all_tickets_count()
        common_data = get_common_template_data()
        return render_template(
            "support.html",
            tickets=tickets,
            current_page=page,
            total_pages=total_pages,
            filter_status=status,
            open_count=open_count,
            closed_count=closed_count,
            all_count=all_count,
            **common_data,
        )

    @flask_app.route("/support/<int:ticket_id>", methods=["GET", "POST"])
    @login_required
    def support_ticket_page(ticket_id):
        ticket = get_ticket(ticket_id)
        if not ticket:
            flash("Тикет не найден.", "danger")
            return redirect(url_for("support_list_page"))

        if request.method == "POST":
            message = (request.form.get("message") or "").strip()
            action = request.form.get("action")
            if action == "reply":
                if not message:
                    flash("Сообщение не может быть пустым.", "warning")
                else:
                    add_support_message(ticket_id, sender="admin", content=message)
                    try:
                        bot = _support_bot_controller.get_bot_instance()
                        loop = current_app.config.get("EVENT_LOOP")
                        user_chat_id = ticket.get("user_id")
                        if bot and loop and loop.is_running() and user_chat_id:
                            text = f"Ответ по тикету #{ticket_id}:\n\n{message}"
                            asyncio.run_coroutine_threadsafe(
                                bot.send_message(user_chat_id, text), loop
                            )
                        else:
                            logger.error(
                                "Ответ поддержки: support-бот или цикл событий недоступны; сообщение пользователю не отправлено."
                            )
                    except Exception as e:
                        logger.error(
                            f"Ответ поддержки: не удалось отправить сообщение пользователю {ticket.get('user_id')} через support-бота: {e}",
                            exc_info=True,
                        )
                    try:
                        bot = _support_bot_controller.get_bot_instance()
                        loop = current_app.config.get("EVENT_LOOP")
                        forum_chat_id = ticket.get("forum_chat_id")
                        thread_id = ticket.get("message_thread_id")
                        if bot and loop and loop.is_running() and forum_chat_id and thread_id:
                            text = f"💬 Ответ админа из панели по тикету #{ticket_id}:\n\n{message}"
                            asyncio.run_coroutine_threadsafe(
                                bot.send_message(
                                    chat_id=int(forum_chat_id),
                                    text=text,
                                    message_thread_id=int(thread_id),
                                ),
                                loop,
                            )
                    except Exception as e:
                        logger.warning(
                            f"Ответ поддержки: не удалось отзеркалить сообщение в тему форума для тикета {ticket_id}: {e}"
                        )
                    flash("Ответ отправлен.", "success")
                return redirect(url_for("support_ticket_page", ticket_id=ticket_id))
            elif action == "close":
                if ticket.get("status") != "closed" and set_ticket_status(ticket_id, "closed"):
                    try:
                        bot = _support_bot_controller.get_bot_instance()
                        loop = current_app.config.get("EVENT_LOOP")
                        forum_chat_id = ticket.get("forum_chat_id")
                        thread_id = ticket.get("message_thread_id")
                        if bot and loop and loop.is_running() and forum_chat_id and thread_id:
                            asyncio.run_coroutine_threadsafe(
                                bot.close_forum_topic(
                                    chat_id=int(forum_chat_id),
                                    message_thread_id=int(thread_id),
                                ),
                                loop,
                            )
                    except Exception as e:
                        logger.warning(
                            f"Закрытие тикета: не удалось закрыть тему форума для тикета {ticket_id}: {e}"
                        )
                    try:
                        bot = _support_bot_controller.get_bot_instance()
                        loop = current_app.config.get("EVENT_LOOP")
                        user_chat_id = ticket.get("user_id")
                        if bot and loop and loop.is_running() and user_chat_id:
                            text = f"✅ Ваш тикет #{ticket_id} был закрыт администратором. Вы можете создать новое обращение при необходимости."
                            asyncio.run_coroutine_threadsafe(
                                bot.send_message(int(user_chat_id), text), loop
                            )
                    except Exception as e:
                        logger.warning(
                            f"Закрытие тикета: не удалось уведомить пользователя {ticket.get('user_id')} о закрытии тикета #{ticket_id}: {e}"
                        )
                    flash("Тикет закрыт.", "success")
                else:
                    flash("Не удалось закрыть тикет.", "danger")
                return redirect(url_for("support_ticket_page", ticket_id=ticket_id))
            elif action == "open":
                if ticket.get("status") != "open" and set_ticket_status(ticket_id, "open"):
                    try:
                        bot = _support_bot_controller.get_bot_instance()
                        loop = current_app.config.get("EVENT_LOOP")
                        forum_chat_id = ticket.get("forum_chat_id")
                        thread_id = ticket.get("message_thread_id")
                        if bot and loop and loop.is_running() and forum_chat_id and thread_id:
                            asyncio.run_coroutine_threadsafe(
                                bot.reopen_forum_topic(
                                    chat_id=int(forum_chat_id),
                                    message_thread_id=int(thread_id),
                                ),
                                loop,
                            )
                    except Exception as e:
                        logger.warning(
                            f"Открытие тикета: не удалось переоткрыть тему форума для тикета {ticket_id}: {e}"
                        )
                    # Notify user
                    try:
                        bot = _support_bot_controller.get_bot_instance()
                        loop = current_app.config.get("EVENT_LOOP")
                        user_chat_id = ticket.get("user_id")
                        if bot and loop and loop.is_running() and user_chat_id:
                            text = f"🔓 Ваш тикет #{ticket_id} снова открыт. Вы можете продолжить переписку."
                            asyncio.run_coroutine_threadsafe(
                                bot.send_message(int(user_chat_id), text), loop
                            )
                    except Exception as e:
                        logger.warning(
                            f"Открытие тикета: не удалось уведомить пользователя {ticket.get('user_id')} об открытии тикета #{ticket_id}: {e}"
                        )
                    flash("Тикет открыт.", "success")
                else:
                    flash("Не удалось открыть тикет.", "danger")
                return redirect(url_for("support_ticket_page", ticket_id=ticket_id))

        messages = get_ticket_messages(ticket_id)
        common_data = get_common_template_data()
        return render_template("ticket.html", ticket=ticket, messages=messages, **common_data)

    @flask_app.route("/support/<int:ticket_id>/messages.json")
    @login_required
    def support_ticket_messages_api(ticket_id):
        ticket = get_ticket(ticket_id)
        if not ticket:
            return jsonify({"error": "not_found"}), 404
        messages = get_ticket_messages(ticket_id) or []
        items = [
            {
                "sender": m.get("sender"),
                "content": m.get("content"),
                "created_at": m.get("created_at"),
            }
            for m in messages
        ]
        return jsonify({"ticket_id": ticket_id, "status": ticket.get("status"), "messages": items})

    @flask_app.route("/support/<int:ticket_id>/delete", methods=["POST"])
    @login_required
    def delete_support_ticket_route(ticket_id: int):
        ticket = get_ticket(ticket_id)
        if not ticket:
            flash("Тикет не найден.", "danger")
            return redirect(url_for("support_list_page"))
        try:
            bot = _support_bot_controller.get_bot_instance()
            loop = current_app.config.get("EVENT_LOOP")
            forum_chat_id = ticket.get("forum_chat_id")
            thread_id = ticket.get("message_thread_id")
            if bot and loop and loop.is_running() and forum_chat_id and thread_id:
                try:
                    fut = asyncio.run_coroutine_threadsafe(
                        bot.delete_forum_topic(
                            chat_id=int(forum_chat_id), message_thread_id=int(thread_id)
                        ),
                        loop,
                    )
                    fut.result(timeout=5)
                except Exception as e:
                    logger.warning(
                        f"Удаление темы форума не удалось для тикета {ticket_id} (чат {forum_chat_id}, тема {thread_id}): {e}. Пытаюсь закрыть тему как фолбэк."
                    )
                    try:
                        fut2 = asyncio.run_coroutine_threadsafe(
                            bot.close_forum_topic(
                                chat_id=int(forum_chat_id),
                                message_thread_id=int(thread_id),
                            ),
                            loop,
                        )
                        fut2.result(timeout=5)
                    except Exception as e2:
                        logger.warning(
                            f"Фолбэк-закрытие темы форума также не удалось для тикета {ticket_id}: {e2}"
                        )
            else:
                logger.error(
                    "Удаление тикета: support-бот или цикл событий недоступны, либо отсутствуют forum_chat_id/message_thread_id; тема не удалена."
                )
        except Exception as e:
            logger.warning(
                f"Не удалось обработать удаление темы форума для тикета {ticket_id} перед удалением: {e}"
            )
        if delete_ticket(ticket_id):
            flash(f"Тикет #{ticket_id} удалён.", "success")
        else:
            flash(f"Не удалось удалить тикет #{ticket_id}.", "danger")
            return redirect(url_for("support_ticket_page", ticket_id=ticket_id))

    @flask_app.route("/settings", methods=["GET", "POST"])
    @login_required
    def settings_page():
        if request.method == "POST":
            # Смена пароля панели (если поле не пустое)
            if "panel_password" in request.form and request.form.get("panel_password"):
                update_setting("panel_password", request.form.get("panel_password"))

            # Обработка чекбоксов, где в форме идёт hidden=false + checkbox=true
            checkbox_keys = [
                "force_subscription",
                "sbp_enabled",
                "trial_enabled",
                "enable_referrals",
                "enable_fixed_referral_bonus",
                "stars_enabled",
                "yoomoney_enabled",
                "monitoring_enabled",
            ]
            for checkbox_key in checkbox_keys:
                values = request.form.getlist(checkbox_key)
                value = values[-1] if values else "false"
                update_setting(checkbox_key, value)

            # Обновление остальных настроек из ALL_SETTINGS_KEYS (кроме panel_password и чекбоксов)
            for key in ALL_SETTINGS_KEYS:
                if key in checkbox_keys or key == "panel_password":
                    continue
                if key in request.form:
                    update_setting(key, request.form.get(key))

            flash("Настройки сохранены.", "success")
            next_hash = (request.form.get("next_hash") or "").strip() or "#panel"
            next_tab = (next_hash[1:] if next_hash.startswith("#") else next_hash) or "panel"
            return redirect(url_for("settings_page", tab=next_tab))

        current_settings = get_all_settings()
        hosts = get_all_hosts()
        for host in hosts:
            host["plans"] = get_plans_for_host(host["host_name"])
            # Добавить последний результат спидтеста в карточку
            try:
                host["latest_speedtest"] = get_latest_speedtest(host["host_name"])
            except Exception:
                host["latest_speedtest"] = None

        # Список доступных бэкапов на сервере (zip)
        backups = []
        try:
            pass

            bdir = backup_manager.BACKUPS_DIR
            for p in sorted(
                bdir.glob("db-backup-*.zip"),
                key=lambda x: x.stat().st_mtime,
                reverse=True,
            ):
                try:
                    st = p.stat()
                    backups.append(
                        {
                            "name": p.name,
                            "mtime": datetime.fromtimestamp(st.st_mtime).strftime("%Y-%m-%d %H:%M"),
                            "size": st.st_size,
                        }
                    )
                except Exception:
                    pass
        except Exception:
            backups = []

        common_data = get_common_template_data()
        return render_template(
            "settings.html",
            settings=current_settings,
            hosts=hosts,
            backups=backups,
            **common_data,
        )

    # --- DB Backup/Restore ---
    @flask_app.route("/admin/db/backup", methods=["POST"])
    @login_required
    def backup_db_route():
        try:
            zip_path = backup_manager.create_backup_file()
            if not zip_path or not os.path.isfile(zip_path):
                flash("Не удалось создать бэкап БД.", "danger")
                return redirect(request.referrer or url_for("settings_page", tab="panel"))
            # Отдаём файл на скачивание
            return send_file(
                str(zip_path),
                as_attachment=True,
                download_name=os.path.basename(zip_path),
            )
        except Exception as e:
            logger.error(f"Ошибка резервного копирования БД: {e}")
            flash("Ошибка при создании бэкапа.", "danger")
            return redirect(request.referrer or url_for("settings_page", tab="panel"))

    @flask_app.route("/admin/db/restore", methods=["POST"])
    @login_required
    def restore_db_route():
        try:
            # Вариант 1: восстановление из имеющегося архива
            existing = (request.form.get("existing_backup") or "").strip()
            ok = False
            if existing:
                # Разрешаем только файлы внутри BACKUPS_DIR
                base = backup_manager.BACKUPS_DIR
                candidate = (base / existing).resolve()
                if str(candidate).startswith(str(base.resolve())) and os.path.isfile(candidate):
                    ok = backup_manager.restore_from_file(candidate)
                else:
                    flash("Выбранный бэкап не найден.", "danger")
                    return redirect(request.referrer or url_for("settings_page", tab="panel"))
            else:
                # Вариант 2: загрузка собственного файла
                file = request.files.get("db_file")
                if not file or file.filename == "":
                    flash("Файл для восстановления не выбран.", "warning")
                    return redirect(request.referrer or url_for("settings_page", tab="panel"))
                filename = file.filename.lower()
                if not (filename.endswith(".zip") or filename.endswith(".db")):
                    flash("Поддерживаются только файлы .zip или .db", "warning")
                    return redirect(request.referrer or url_for("settings_page", tab="panel"))
                ts = datetime.now().strftime("%Y%m%d-%H%M%S")
                dest_dir = backup_manager.BACKUPS_DIR
                try:
                    dest_dir.mkdir(parents=True, exist_ok=True)
                except Exception:
                    pass
                dest_path = dest_dir / f"uploaded-{ts}-{os.path.basename(filename)}"
                file.save(dest_path)
                ok = backup_manager.restore_from_file(dest_path)
            if ok:
                flash("Восстановление выполнено успешно.", "success")
            else:
                flash("Восстановление не удалось. Проверьте файл и повторите.", "danger")
            return redirect(request.referrer or url_for("settings_page", tab="panel"))
        except Exception as e:
            logger.error(f"Ошибка восстановления БД: {e}", exc_info=True)
            flash("Ошибка при восстановлении БД.", "danger")
            return redirect(request.referrer or url_for("settings_page", tab="panel"))

    @flask_app.route("/update-host-subscription", methods=["POST"])
    @login_required
    def update_host_subscription_route():
        host_name = (request.form.get("host_name") or "").strip()
        sub_url = (request.form.get("host_subscription_url") or "").strip()
        if not host_name:
            flash("Не указан сервер для обновления ссылки подписки.", "danger")
            return redirect(url_for("settings_page", tab="hosts"))
        ok = update_host_subscription_url(host_name, sub_url or None)
        if ok:
            flash("Ссылка подписки для хоста обновлена.", "success")
        else:
            flash(
                "Не удалось обновить ссылку подписки для хоста (возможно, сервер не найден).",
                "danger",
            )
        return redirect(url_for("settings_page", tab="hosts"))

    @flask_app.route("/update-host-url", methods=["POST"])
    @login_required
    def update_host_url_route():
        host_name = (request.form.get("host_name") or "").strip()
        new_url = (request.form.get("host_url") or "").strip()
        if not host_name or not new_url:
            flash("Укажите имя хоста и новый URL.", "warning")
            return redirect(url_for("settings_page", tab="hosts"))
        ok = update_host_url(host_name, new_url)
        flash(
            "URL хоста обновлён." if ok else "Не удалось обновить URL хоста.",
            "success" if ok else "danger",
        )
        return redirect(url_for("settings_page", tab="hosts"))

    @flask_app.route("/rename-host", methods=["POST"])
    @login_required
    def rename_host_route():
        old_name = (request.form.get("old_host_name") or "").strip()
        new_name = (request.form.get("new_host_name") or "").strip()
        if not old_name or not new_name:
            flash("Введите старое и новое имя хоста.", "warning")
            return redirect(url_for("settings_page", tab="hosts"))
        ok = update_host_name(old_name, new_name)
        flash(
            "Имя хоста обновлено." if ok else "Не удалось переименовать сервер.",
            "success" if ok else "danger",
        )
        return redirect(url_for("settings_page", tab="hosts"))

    @flask_app.route("/start-support-bot", methods=["POST"])
    @login_required
    def start_support_bot_route():
        loop = current_app.config.get("EVENT_LOOP")
        if loop and loop.is_running():
            _support_bot_controller.set_loop(loop)
        result = _support_bot_controller.start()
        flash(result["message"], "success" if result["status"] == "success" else "danger")
        return redirect(request.referrer or url_for("settings_page"))

    def _wait_for_stop(controller, timeout: float = 5.0) -> bool:
        start = time.time()
        while time.time() - start < timeout:
            status = controller.get_status() or {}
            if not status.get("is_running"):
                return True
            time.sleep(0.1)
        return False

    @flask_app.route("/stop-support-bot", methods=["POST"])
    @login_required
    def stop_support_bot_route():
        result = _support_bot_controller.stop()
        _wait_for_stop(_support_bot_controller)
        flash(result["message"], "success" if result["status"] == "success" else "danger")
        return redirect(request.referrer or url_for("settings_page"))

    @flask_app.route("/start-bot", methods=["POST"])
    @login_required
    def start_bot_route():
        result = _bot_controller.start()
        flash(result["message"], "success" if result["status"] == "success" else "danger")
        return redirect(request.referrer or url_for("dashboard_page"))

    @flask_app.route("/stop-bot", methods=["POST"])
    @login_required
    def stop_bot_route():
        result = _bot_controller.stop()
        _wait_for_stop(_bot_controller)
        flash(result["message"], "success" if result["status"] == "success" else "danger")
        return redirect(request.referrer or url_for("dashboard_page"))

    @flask_app.route("/stop-both-bots", methods=["POST"])
    @login_required
    def stop_both_bots_route():
        main_result = _bot_controller.stop()
        support_result = _support_bot_controller.stop()

        statuses = []
        categories = []
        for name, res in [
            ("Основной бот", main_result),
            ("Support-бот", support_result),
        ]:
            if res.get("status") == "success":
                statuses.append(f"{name}: остановлен")
                categories.append("success")
            else:
                statuses.append(f"{name}: ошибка — {res.get('message')}")
                categories.append("danger")
        _wait_for_stop(_bot_controller)
        _wait_for_stop(_support_bot_controller)
        category = "danger" if "danger" in categories else "success"
        flash(" | ".join(statuses), category)
        return redirect(request.referrer or url_for("dashboard_page"))

    @flask_app.route("/start-both-bots", methods=["POST"])
    @login_required
    def start_both_bots_route():
        main_result = _bot_controller.start()
        loop = current_app.config.get("EVENT_LOOP")
        if loop and loop.is_running():
            _support_bot_controller.set_loop(loop)
        support_result = _support_bot_controller.start()

        statuses = []
        categories = []
        for name, res in [
            ("Основной бот", main_result),
            ("Support-бот", support_result),
        ]:
            if res.get("status") == "success":
                statuses.append(f"{name}: запущен")
                categories.append("success")
            else:
                statuses.append(f"{name}: ошибка — {res.get('message')}")
                categories.append("danger")
        category = "danger" if "danger" in categories else "success"
        flash(" | ".join(statuses), category)
        return redirect(request.referrer or url_for("settings_page"))

    @flask_app.route("/users/ban/<int:user_id>", methods=["POST"])
    @login_required
    def ban_user_route(user_id):
        ban_user(user_id)
        flash(f"Пользователь {user_id} был заблокирован.", "success")
        # Telegram-уведомление пользователю о бане с кнопкой поддержки (без кнопки "Назад в меню")
        try:
            bot = _bot_controller.get_bot_instance()
            if bot:
                text = "🚫 Ваш аккаунт заблокирован администратором. Если это ошибка — напишите в поддержку."
                # Собираем клавиатуру из одной кнопки поддержки
                try:
                    support = (
                        get_setting("support_bot_username") or get_setting("support_user") or ""
                    ).strip()
                except Exception:
                    support = ""
                kb = InlineKeyboardBuilder()
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
                    kb.button(text="🆘 Написать в поддержку", url=url)
                else:
                    kb.button(text="🆘 Поддержка", callback_data="show_help")
                loop = current_app.config.get("EVENT_LOOP")
                if loop and loop.is_running():
                    asyncio.run_coroutine_threadsafe(
                        bot.send_message(chat_id=user_id, text=text, reply_markup=kb.as_markup()),
                        loop,
                    )
                else:
                    asyncio.run(
                        bot.send_message(chat_id=user_id, text=text, reply_markup=kb.as_markup())
                    )
        except Exception as e:
            logger.warning(f"Не удалось отправить уведомление о бане пользователю {user_id}: {e}")
        return redirect(url_for("users_page"))

    @flask_app.route("/users/unban/<int:user_id>", methods=["POST"])
    @login_required
    def unban_user_route(user_id):
        unban_user(user_id)
        flash(f"Пользователь {user_id} был разблокирован.", "success")
        # Telegram-уведомление пользователю о разбане с кнопкой перехода в главное меню
        try:
            bot = _bot_controller.get_bot_instance()
            if bot:
                kb = InlineKeyboardBuilder()
                kb.row(keyboards.get_main_menu_button())
                text = "✅ Доступ к аккаунту восстановлен администратором."
                loop = current_app.config.get("EVENT_LOOP")
                if loop and loop.is_running():
                    asyncio.run_coroutine_threadsafe(
                        bot.send_message(chat_id=user_id, text=text, reply_markup=kb.as_markup()),
                        loop,
                    )
                else:
                    asyncio.run(
                        bot.send_message(chat_id=user_id, text=text, reply_markup=kb.as_markup())
                    )
        except Exception as e:
            logger.warning(
                f"Не удалось отправить уведомление о разбане пользователю {user_id}: {e}"
            )
        return redirect(url_for("users_page"))

    @flask_app.route("/users/revoke/<int:user_id>", methods=["POST"])
    @login_required
    def revoke_keys_route(user_id):
        keys_to_revoke = get_user_keys(user_id)
        success_count = 0
        total = len(keys_to_revoke)

        for key in keys_to_revoke:
            result = asyncio.run(xui_api.delete_client_on_host(key["host_name"], key["key_email"]))
            if result:
                success_count += 1

        # Удаляем из БД все ключи пользователя
        delete_user_keys(user_id)

        # Уведомление пользователю в Telegram
        try:
            bot = _bot_controller.get_bot_instance()
            if bot:
                text = (
                    "❌ Ваши подписки были отозваны администратором.\n"
                    f"Всего подписок: {total}\n"
                    f"Отозвано: {success_count}"
                )
                loop = current_app.config.get("EVENT_LOOP")
                if loop and loop.is_running():
                    asyncio.run_coroutine_threadsafe(
                        bot.send_message(chat_id=user_id, text=text), loop
                    )
                else:
                    asyncio.run(bot.send_message(chat_id=user_id, text=text))
        except Exception:
            pass

        message = (
            f"Все {total} ключей для пользователя {user_id} были успешно отозваны."
            if success_count == total
            else f"Удалось отозвать {success_count} из {total} ключей для пользователя {user_id}. Проверьте логи."
        )
        category = "success" if success_count == total else "warning"

        # Если это AJAX-запрос (из таблицы пользователей) — возвращаем JSON
        wants_json = (
            "application/json" in (request.headers.get("Accept") or "")
            or request.headers.get("X-Requested-With") == "XMLHttpRequest"
        )
        if wants_json:
            return (
                jsonify(
                    {
                        "ok": success_count == total,
                        "message": message,
                        "revoked": success_count,
                        "total": total,
                    }
                ),
                200,
            )

        flash(message, category)
        return redirect(url_for("users_page"))

    @flask_app.route("/add-host", methods=["POST"])
    @login_required
    def add_host_route():
        create_host(
            name=request.form["host_name"],
            url=request.form["host_url"],
            user=request.form["host_username"],
            passwd=request.form["host_pass"],
            inbound=int(request.form["host_inbound_id"]),
            subscription_url=(request.form.get("host_subscription_url") or "").strip() or None,
        )
        flash(f"Сервер '{request.form['host_name']}' успешно добавлен.", "success")
        return redirect(url_for("settings_page", tab="hosts"))

    @flask_app.route("/delete-host/<host_name>", methods=["POST"])
    @login_required
    def delete_host_route(host_name):
        delete_host(host_name)
        flash(f"Сервер '{host_name}' и все его тарифы были удалены.", "success")
        return redirect(url_for("settings_page", tab="hosts"))

    @flask_app.route("/add-plan", methods=["POST"])
    @login_required
    def add_plan_route():
        create_plan(
            host_name=request.form["host_name"],
            plan_name=request.form["plan_name"],
            months=int(request.form["months"]),
            price=float(request.form["price"]),
        )
        flash(f"Новый тариф для хоста '{request.form['host_name']}' добавлен.", "success")
        return redirect(url_for("settings_page", tab="hosts"))

    @flask_app.route("/delete-plan/<int:plan_id>", methods=["POST"])
    @login_required
    def delete_plan_route(plan_id):
        delete_plan(plan_id)
        flash("Тариф успешно удален.", "success")
        return redirect(url_for("settings_page", tab="hosts"))

    @flask_app.route("/update-plan/<int:plan_id>", methods=["POST"])
    @login_required
    def update_plan_route(plan_id):
        plan_name = (request.form.get("plan_name") or "").strip()
        months = request.form.get("months")
        price = request.form.get("price")
        try:
            months_int = int(months)
            price_float = float(price)
        except (TypeError, ValueError):
            flash("Некорректные значения для месяцев или цены.", "danger")
            return redirect(url_for("settings_page", tab="hosts"))

        if not plan_name:
            flash("Название тарифа не может быть пустым.", "danger")
            return redirect(url_for("settings_page", tab="hosts"))

        ok = update_plan(plan_id, plan_name, months_int, price_float)
        if ok:
            flash("Тариф обновлён.", "success")
        else:
            flash("Не удалось обновить тариф (возможно, он не найден).", "danger")
        return redirect(url_for("settings_page", tab="hosts"))

    @csrf.exempt
    @flask_app.route("/yookassa-webhook", methods=["POST"])
    def yookassa_webhook_handler():
        try:
            event_json = request.json

            # Официально YooKassa рекомендует сверку по IP-адресу отправителя,
            # но сами IP на практике плавают — куда надёжнее не доверять телу вообще,
            # а перезапросить реальный статус платежа напрямую у YooKassa по payment_id
            # через авторизованный SDK (Configuration.account_id/secret_key уже
            # настроены в bot_controller.py). Подделать это уже нельзя —
            # нужен доступ к нашему секретному ключу, не просто знание URL.
            payment_id = event_json.get("object", {}).get("id")
            if not payment_id:
                logger.warning("YooKassa вебхук: в теле нет object.id, игнорирую.")
                return "OK", 200

            try:
                real_payment = Payment.find_one(payment_id)
            except Exception as e:
                logger.error(f"YooKassa вебхук: не удалось проверить платёж {payment_id} через API: {e}")
                return "Error", 502

            if real_payment.status != "succeeded":
                logger.warning(
                    f"YooKassa вебхук: платёж {payment_id} заявлен как succeeded, "
                    f"но реальный статус по API — {real_payment.status!r}. Игнорирую."
                )
                return "OK", 200

            metadata = real_payment.metadata or {}

            bot = _bot_controller.get_bot_instance()
            payment_processor = handlers.process_successful_payment

            if metadata and bot is not None and payment_processor is not None:
                loop = current_app.config.get("EVENT_LOOP")
                if loop and loop.is_running():
                    asyncio.run_coroutine_threadsafe(payment_processor(bot, metadata), loop)
                else:
                    logger.error("YooKassa вебхук: цикл событий недоступен!")
            return "OK", 200
        except Exception as e:
            logger.error(f"Ошибка в обработчике вебхука YooKassa: {e}", exc_info=True)
            return "Error", 500

    @csrf.exempt
    @flask_app.route("/cryptobot-webhook", methods=["POST"])
    def cryptobot_webhook_handler():
        try:
            # У CryptoBot есть штатный механизм: HMAC-SHA256 от сырого
            # (не распарсенного) тела запроса, ключ — SHA256 от API-токена
            # приложения, подпись приходит в заголовке crypto-pay-api-signature
            # (см. официальную документацию Crypto Pay API). Без этой проверки
            # любой POST с телом вида {"update_type": "invoice_paid",
            # "payload": {"payload": "user_id:months:price:..."}}
            # выдавал оплаченную услугу без реального платежа.
            cryptobot_token = get_setting("cryptobot_token")
            if not cryptobot_token:
                logger.error("CryptoBot вебхук: cryptobot_token не задан в настройках.")
                return "Error", 500

            raw_body = request.get_data()
            signature_header = request.headers.get("crypto-pay-api-signature", "")
            secret_key = hashlib.sha256(cryptobot_token.encode()).digest()
            expected_signature = hmac.new(secret_key, raw_body, hashlib.sha256).hexdigest()

            if not signature_header or not compare_digest(expected_signature, signature_header):
                logger.warning("CryptoBot вебхук: недействительная подпись.")
                return "Forbidden", 403

            request_data = request.json

            if request_data and request_data.get("update_type") == "invoice_paid":
                payload_data = request_data.get("payload", {})

                payload_string = payload_data.get("payload")

                if not payload_string:
                    logger.warning(
                        "CryptoBot вебхук: Получен оплаченный invoice, но payload пустой."
                    )
                    return "OK", 200

                parts = payload_string.split(":")
                if len(parts) < 9:
                    logger.error(f"CryptoBot вебхук: некорректный формат payload: {payload_string}")
                    return "Error", 400

                metadata = {
                    "user_id": parts[0],
                    "months": parts[1],
                    "price": parts[2],
                    "action": parts[3],
                    "key_id": parts[4] if parts[4] != "None" else None,
                    "host_name": parts[5],
                    "plan_id": parts[6] if parts[6] != "None" else None,
                    "customer_email": parts[7] if parts[7] != "None" else None,
                    "payment_method": parts[8],
                    # Дополнительное поле promo_code поддерживается, если присутствует 10‑й элемент
                    "promo_code": (
                        parts[9] if len(parts) > 9 and parts[9] and parts[9] != "None" else None
                    ),
                }

                bot = _bot_controller.get_bot_instance()
                loop = current_app.config.get("EVENT_LOOP")
                payment_processor = handlers.process_successful_payment

                if bot and loop and loop.is_running():
                    asyncio.run_coroutine_threadsafe(payment_processor(bot, metadata), loop)
                else:
                    logger.error(
                        "CryptoBot вебхук: не удалось обработать платёж — бот или цикл событий не запущены."
                    )

            return "OK", 200

        except Exception as e:
            logger.error(f"Ошибка в обработчике вебхука CryptoBot: {e}", exc_info=True)
            return "Error", 500

    @csrf.exempt
    @flask_app.route("/heleket-webhook", methods=["POST"])
    def heleket_webhook_handler():
        try:
            data = request.json
            logger.info(f"Получен вебхук Heleket: {data}")

            api_key = get_setting("heleket_api_key")
            if not api_key:
                return "Error", 500

            sign = data.pop("sign", None)
            if not sign:
                return "Error", 400

            sorted_data_str = json.dumps(data, sort_keys=True, separators=(",", ":"))

            base64_encoded = base64.b64encode(sorted_data_str.encode()).decode()
            raw_string = f"{base64_encoded}{api_key}"
            expected_sign = hashlib.md5(raw_string.encode()).hexdigest()

            if not compare_digest(expected_sign, sign):
                logger.warning("Heleket вебхук: недействительная подпись.")
                return "Forbidden", 403

            if data.get("status") in ["paid", "paid_over"]:
                metadata_str = data.get("description")
                if not metadata_str:
                    return "Error", 400

                metadata = json.loads(metadata_str)

                bot = _bot_controller.get_bot_instance()
                loop = current_app.config.get("EVENT_LOOP")
                payment_processor = handlers.process_successful_payment

                if bot and loop and loop.is_running():
                    asyncio.run_coroutine_threadsafe(payment_processor(bot, metadata), loop)

            return "OK", 200
        except Exception as e:
            logger.error(f"Ошибка в обработчике вебхука Heleket: {e}", exc_info=True)
            return "Error", 500

    @csrf.exempt
    @flask_app.route("/ton-webhook", methods=["POST"])
    def ton_webhook_handler():
        try:
            data = request.json
            logger.info(f"Получен вебхук TonAPI: {data}")

            if "tx_id" in data:
                for tx in data.get("in_progress_txs", []) + data.get("txs", []):
                    in_msg = tx.get("in_msg")
                    if in_msg and in_msg.get("decoded_comment"):
                        payment_id = in_msg["decoded_comment"]
                        amount_nano = int(in_msg.get("value", 0))
                        amount_ton = float(amount_nano / 1_000_000_000)

                        metadata = find_and_complete_ton_transaction(payment_id, amount_ton)

                        if metadata:
                            logger.info(f"Успешная оплата TON для payment_id: {payment_id}")
                            bot = _bot_controller.get_bot_instance()
                            loop = current_app.config.get("EVENT_LOOP")
                            payment_processor = handlers.process_successful_payment

                            if bot and loop and loop.is_running():
                                asyncio.run_coroutine_threadsafe(
                                    payment_processor(bot, metadata), loop
                                )

            return "OK", 200
        except Exception as e:
            logger.error(f"Ошибка в обработчике вебхука TonAPI: {e}", exc_info=True)
            return "Error", 500

    # --- YooMoney OAuth integration ---
    def _ym_get_redirect_uri():
        try:
            saved = (get_setting("yoomoney_redirect_uri") or "").strip()
        except Exception:
            saved = ""
        if saved:
            return saved
        # Fallback: build from current host
        root = request.url_root.rstrip("/")
        return f"{root}/yoomoney/callback"

    @flask_app.route("/yoomoney/connect")
    @login_required
    def yoomoney_connect_route():
        client_id = (get_setting("yoomoney_client_id") or "").strip()
        if not client_id:
            flash("Укажите YooMoney client_id в настройках.", "warning")
            return redirect(url_for("settings_page", tab="payments"))
        redirect_uri = _ym_get_redirect_uri()
        scope = "operation-history operation-details account-info"
        qs = urllib.parse.urlencode(
            {
                "client_id": client_id,
                "response_type": "code",
                "scope": scope,
                "redirect_uri": redirect_uri,
            }
        )
        url = f"https://yoomoney.ru/oauth/authorize?{qs}"
        return redirect(url)

    @csrf.exempt
    @flask_app.route("/yoomoney/callback")
    def yoomoney_callback_route():
        code = (request.args.get("code") or "").strip()
        if not code:
            flash("YooMoney: не получен code из OAuth.", "danger")
            return redirect(url_for("settings_page", tab="payments"))
        client_id = (get_setting("yoomoney_client_id") or "").strip()
        client_secret = (get_setting("yoomoney_client_secret") or "").strip()
        redirect_uri = _ym_get_redirect_uri()
        data = {
            "grant_type": "authorization_code",
            "code": code,
            "client_id": client_id,
            "redirect_uri": redirect_uri,
        }
        if client_secret:
            data["client_secret"] = client_secret
        try:
            encoded = urllib.parse.urlencode(data).encode("utf-8")
            req = urllib.request.Request(
                "https://yoomoney.ru/oauth/token",
                data=encoded,
                headers={"Content-Type": "application/x-www-form-urlencoded"},
            )
            with urllib.request.urlopen(req, timeout=15) as resp:
                resp_text = resp.read().decode("utf-8", errors="ignore")
            try:
                payload = json.loads(resp_text)
            except Exception:
                payload = {}
            token = (payload.get("access_token") or "").strip()
            if not token:
                flash(f"Не удалось получить access_token от YooMoney: {payload}", "danger")
                return redirect(url_for("settings_page", tab="payments"))
            update_setting("yoomoney_api_token", token)
            flash("YooMoney: токен успешно сохранён.", "success")
        except Exception as e:
            logger.error(f"Ошибка обратного вызова YooMoney OAuth: {e}", exc_info=True)
            flash(f"Ошибка при обмене кода на токен: {e}", "danger")
        return redirect(url_for("settings_page", tab="payments"))

    @flask_app.route("/yoomoney/check", methods=["GET", "POST"])
    @login_required
    def yoomoney_check_route():
        token = (get_setting("yoomoney_api_token") or "").strip()
        if not token:
            flash("YooMoney: токен не задан.", "warning")
            return redirect(url_for("settings_page", tab="payments"))
        # 1) account-info
        try:
            req = urllib.request.Request(
                "https://yoomoney.ru/api/account-info",
                headers={"Authorization": f"Bearer {token}"},
                method="POST",
            )
            with urllib.request.urlopen(req, timeout=15) as resp:
                ai_text = resp.read().decode("utf-8", errors="ignore")
                ai_status = resp.status
                ai_headers = dict(resp.headers)
        except Exception as e:
            flash(f"YooMoney account-info: ошибка запроса: {e}", "danger")
            return redirect(url_for("settings_page", tab="payments"))
        try:
            ai = json.loads(ai_text)
        except Exception:
            ai = {}
        if ai_status != 200:
            www = ai_headers.get("WWW-Authenticate", "")
            flash(f"YooMoney account-info HTTP {ai_status}. {www}", "danger")
            return redirect(url_for("settings_page", tab="payments"))
        account = ai.get("account") or ai.get("account_number") or "—"
        # 2) operation-history minimal
        try:
            body = urllib.parse.urlencode({"records": "1"}).encode("utf-8")
            req2 = urllib.request.Request(
                "https://yoomoney.ru/api/operation-history",
                data=body,
                headers={
                    "Authorization": f"Bearer {token}",
                    "Content-Type": "application/x-www-form-urlencoded",
                },
            )
            with urllib.request.urlopen(req2, timeout=15) as resp2:
                resp2.read()
                oh_status = resp2.status
        except Exception as e:
            flash(f"YooMoney operation-history: ошибка запроса: {e}", "warning")
            oh_status = None
        if oh_status == 200:
            flash(f"YooMoney: токен валиден. Кошелёк: {account}", "success")
        elif oh_status is not None:
            flash(
                f"YooMoney operation-history HTTP {oh_status}. Проверьте scope operation-history и соответствие кошелька.",
                "danger",
            )
        else:
            flash("YooMoney: не удалось проверить operation-history.", "warning")
        return redirect(url_for("settings_page", tab="payments"))

    # --- Button Constructor ---
    @flask_app.route("/button-constructor")
    @login_required
    def button_constructor_page():
        """Button constructor page."""
        template_data = get_common_template_data()
        return render_template("button_constructor.html", **template_data)

    # --- Button Constructor API ---
    @flask_app.route("/api/button-configs", methods=["GET", "POST"])
    @login_required
    def button_configs_api():
        if request.method == "GET":
            menu_type = request.args.get("menu_type", "main_menu")
            try:
                from shop_bot.data_manager.database import get_button_configs

                configs = get_button_configs(menu_type)
                return jsonify({"success": True, "data": configs})
            except Exception as e:
                return jsonify({"success": False, "error": str(e)}), 500

        elif request.method == "POST":
            try:
                data = request.get_json()
                from shop_bot.data_manager.database import create_button_config

                button_id = create_button_config(data)
                if button_id:
                    return jsonify({"success": True, "id": button_id})
                else:
                    return (
                        jsonify(
                            {
                                "success": False,
                                "error": "Failed to create button config",
                            }
                        ),
                        500,
                    )
            except Exception as e:
                return jsonify({"success": False, "error": str(e)}), 500

    @flask_app.route("/api/button-configs/<menu_type>", methods=["GET"])
    @login_required
    def button_configs_by_menu_api(menu_type):
        try:
            from shop_bot.data_manager.database import get_button_configs

            configs = get_button_configs(menu_type)
            return jsonify({"success": True, "data": configs})
        except Exception as e:
            return jsonify({"success": False, "error": str(e)}), 500

    @flask_app.route("/api/button-configs/<int:button_id>", methods=["PUT", "DELETE"])
    @login_required
    def button_config_api(button_id):
        if request.method == "PUT":
            try:
                data = request.get_json()
                from shop_bot.data_manager.database import update_button_config

                success = update_button_config(button_id, data)
                if success:
                    return jsonify({"success": True})
                else:
                    return (
                        jsonify(
                            {
                                "success": False,
                                "error": "Button config not found or update failed",
                            }
                        ),
                        404,
                    )
            except Exception as e:
                return jsonify({"success": False, "error": str(e)}), 500

        elif request.method == "DELETE":
            try:
                from shop_bot.data_manager.database import delete_button_config

                success = delete_button_config(button_id)
                if success:
                    return jsonify({"success": True})
                else:
                    return (
                        jsonify({"success": False, "error": "Button config not found"}),
                        404,
                    )
            except Exception as e:
                return jsonify({"success": False, "error": str(e)}), 500

    @flask_app.route("/api/button-configs/<menu_type>/reorder", methods=["POST"])
    @login_required
    def button_configs_reorder_api(menu_type):
        try:
            data = request.get_json()
            button_orders = data.get("button_orders", [])
            from shop_bot.data_manager.database import reorder_button_configs

            success = reorder_button_configs(menu_type, button_orders)
            if success:
                return jsonify({"success": True})
            else:
                return (
                    jsonify({"success": False, "error": "Failed to reorder buttons"}),
                    500,
                )
        except Exception as e:
            return jsonify({"success": False, "error": str(e)}), 500

    @flask_app.route("/api/button-configs/force-migration", methods=["POST"])
    @login_required
    def force_button_migration_api():
        """Принудительная миграция кнопок."""
        try:
            from shop_bot.data_manager.database import force_button_migration

            success = force_button_migration()
            if success:
                return jsonify({"success": True, "message": "Миграция кнопок выполнена успешно"})
            else:
                return jsonify({"success": False, "error": "Миграция не удалась"}), 500
        except Exception as e:
            return jsonify({"success": False, "error": str(e)}), 500

    # --- YooMoney Webhook ---
    @csrf.exempt
    @flask_app.route("/yoomoney-webhook", methods=["POST"])
    def yoomoney_webhook_handler():
        """ЮMoney HTTP уведомление (кнопка/ссылка p2p). Подпись: sha1(notification_type&operation_id&amount&currency&datetime&sender&codepro&notification_secret&label)."""
        logger.info("🔔 Получен webhook от ЮMoney")

        try:
            form = request.form
            logger.info(f"Данные вебхука YooMoney: {dict(form)}")

            # Проверяем, что это тестовый платеж
            if form.get("codepro") == "true":
                logger.info("🧪 Игнорируем тестовый платеж (codepro=true)")
                return "OK", 200

            secret = get_setting("yoomoney_secret") or ""
            signature_str = "&".join(
                [
                    form.get("notification_type", ""),
                    form.get("operation_id", ""),
                    form.get("amount", ""),
                    form.get("currency", ""),
                    form.get("datetime", ""),
                    form.get("sender", ""),
                    form.get("codepro", ""),
                    secret,
                    form.get("label", ""),
                ]
            )

            import hashlib

            expected_signature = hashlib.sha1(signature_str.encode("utf-8")).hexdigest()
            received_signature = form.get("sha1_hash", "")

            if not compare_digest(expected_signature, received_signature):
                logger.warning("YooMoney webhook: неверная подпись")
                return "Forbidden", 403

            # Обрабатываем успешный платеж
            if form.get("notification_type") == "p2p-incoming":
                amount = float(form.get("amount", 0))
                label = form.get("label", "")

                # Подпись проверялась честно, но сам платёж просто логировался
                # и выбрасывался (pass) — реальные подтверждённые деньги никак
                # не зачислялись юзеру. label — это payment_id уже созданной
                # pending-транзакции (см. _build_yoomoney_quickpay_url(label=payment_id)
                # в handlers.py) — та же схема, что уже работает для Stars.
                logger.info(f"Платёж YooMoney: {amount} руб., метка: {label}")

                try:
                    bot = _bot_controller.get_bot_instance()
                    payment_processor = handlers.process_successful_payment
                    if bot and label:
                        metadata = find_and_complete_pending_transaction(
                            payment_id=label,
                            amount_rub=amount,
                            payment_method="YooMoney",
                        )
                        if metadata:
                            loop = current_app.config.get("EVENT_LOOP")
                            if loop and loop.is_running():
                                asyncio.run_coroutine_threadsafe(
                                    payment_processor(bot, metadata), loop
                                )
                            else:
                                logger.error("YooMoney webhook: цикл событий недоступен!")
                        else:
                            logger.warning(
                                f"YooMoney webhook: не найдена ожидающая транзакция "
                                f"по label={label!r}."
                            )
                except Exception as e:
                    logger.error(f"YooMoney webhook: ошибка уведомления бота: {e}")

            return "OK", 200

        except Exception as e:
            logger.error(f"Ошибка вебхука YooMoney: {e}", exc_info=True)
            return "Error", 500

    return flask_app
