"""Интеграция с панелью 3x-ui: создание, обновление, синхронизация и клонирование клиентов."""

import asyncio
import json
import logging
import os
import re
import secrets
import time
import uuid
from datetime import datetime, timedelta
from typing import Dict, List
from urllib.parse import urlparse

from dateutil.relativedelta import relativedelta
from py3xui import Api, Client, Inbound

from shop_bot.data_manager.database import get_host, get_key_by_email, get_setting

logger = logging.getLogger(__name__)

emails_in_transition: set[str] = set()

# Домен, используемый в служебных email клиентов панели как того
# требует X-UI. Задаётся через .env (EMAIL_DOMAIN) с дефолтом на
# случай, если переменная не выставлена — деплой не должен падать из-за
# отсутствующего .env-ключа
#
# LEGACY_EMAIL_DOMAINS — прежние значения EMAIL_DOMAIN, через запятую в
# LEGACY_EMAIL_DOMAINS (например: "bot.local,old-domain.local"). Пустое
# значение/переменная не задана = пустой кортеж, нормализация в
# scheduler.py просто не найдёт кандидатов и ничего не тронет
EMAIL_DOMAIN = os.environ.get("EMAIL_DOMAIN", "snowyservices_bot").strip().lower()
LEGACY_EMAIL_DOMAINS = tuple(
    d.strip().lower()
    for d in os.environ.get("LEGACY_EMAIL_DOMAINS", "bot.local").split(",")
    if d.strip()
)


# -------------------------------------------------------------------------
# Ретрай-обёртка для сетевых вызовов py3xui.
#
# У py3xui._check_response() разбор ответа выглядит так:
#   response_json = response.json()
# без try/except. Панель X-UI после addClient/updateClient пересобирает
# конфиг и рестартует xray-core; если следующий запрос прилетает в этот
# момент, панель (или nginx перед ней) иногда отдаёт HTTP 200 с пустым
# телом. response.json() в этом случае роняет json.JSONDecodeError
# ("Expecting value: line 1 column 1 (char 0)") — а собственный retry
# py3xui (max_retries=3) ловит только ConnectionError/Timeout/
# RequestException, JSONDecodeError под них не попадает и улетает наверх
# без единой попытки повтора. Поэтому ретраим такие вызовы здесь, на
# уровне бота, а не полагаемся на встроенный механизм py3xui.
# -------------------------------------------------------------------------
def call_with_retry(fn, *args, retries: int = 3, base_delay: float = 1.5, **kwargs):
    """Вызывает fn(*args, **kwargs) с повтором при пустом/битом ответе панели.

    Повторяет только на json.JSONDecodeError (пустое тело ответа) — на
    остальных исключениях (Duplicate email, empty client ID и т.п.) повтор
    бессмысленен, это осмысленный ответ панели, а не сетевой сбой, и он
    поднимается сразу же вызывающему коду как обычно.
    """
    last_err: Exception | None = None
    for attempt in range(1, retries + 1):
        try:
            return fn(*args, **kwargs)
        except json.JSONDecodeError as e:
            last_err = e
            if attempt == retries:
                break
            logger.warning(
                f"Пустой/битый ответ панели от {getattr(fn, '__name__', fn)}, "
                f"повтор {attempt}/{retries} через {base_delay * attempt:.1f}с: {e}"
            )
            time.sleep(base_delay * attempt)
    raise last_err


def add_client_robust(
    api,
    inbound_id: int,
    client_payload: list,
    email_for_check: str,
    retries: int = 3,
    base_delay: float = 1.5,
) -> bool:
    """Надёжное создание клиента через api.client.add — в отличие от простого
    call_with_retry, безопасно для add() конкретно, а не только для
    update()/delete().

    Проблема с обычным ретраем на пустое тело (call_with_retry) именно для
    add(): пустое тело ответа НЕ всегда значит "клиент не создан" — панель
    вполне могла реально создать клиента и просто не успеть ответить (тот же
    рестарт xray, что и порождает пустое тело). Слепой повтор в этом случае
    шлёт add() второй раз на уже созданного клиента → ловит настоящий
    Duplicate email → весь вызов падает с ошибкой, хотя по факту всё уже
    создалось на первой попытке. Это особенно больно на master-клиенте
    (addClient #1 в цепочке) — именно он чаще всего и роняет панель в
    рестарт xray, то есть именно на нём выше всего шанс словить пустое тело.

    Защита в две части:
    1. Перед каждым повтором (не на первой попытке) сначала живым запросом
       проверяем, не появился ли клиент на панели уже — если да, считаем
       успехом и НЕ шлём add() повторно.
    2. Если проверка почему-то не поймала это заранее и повторный add() всё
       же словил "Duplicate email" — тоже считаем успехом, а не ошибкой:
       панели неоткуда взять этот email для коллизии, кроме как от нашей же
       предыдущей попытки.
    """
    last_exc: Exception | None = None
    for attempt in range(1, retries + 1):
        try:
            api.client.add(inbound_id, client_payload)
            return True
        except Exception as e:
            last_exc = e
            if attempt > 1 and "Duplicate email" in str(e):
                logger.info(
                    f"add_client_robust: {email_for_check} — Duplicate email на повторной "
                    f"попытке (inbound={inbound_id}), считаю успехом предыдущей попытки."
                )
                return True
            if not isinstance(e, json.JSONDecodeError):
                raise
            if attempt == retries:
                break
            try:
                ib = api.inbound.get_by_id(inbound_id)
                live_emails = (
                    {
                        str(getattr(c, "email", "")).strip().lower()
                        for c in (ib.settings.clients or [])
                    }
                    if ib
                    else set()
                )
                if email_for_check.strip().lower() in live_emails:
                    logger.info(
                        f"add_client_robust: {email_for_check} уже есть на панели "
                        f"(inbound={inbound_id}) после пустого ответа — считаю успехом."
                    )
                    return True
            except Exception:
                pass
            logger.warning(
                f"add_client_robust: пустой ответ панели на {email_for_check} "
                f"(inbound={inbound_id}), повтор {attempt}/{retries}"
            )
            time.sleep(base_delay * attempt)
    if last_exc:
        raise last_exc
    return False


# -------------------------------------------------------------------------
# Пер-хостовые asyncio.Lock — сериализуют все мутирующие обращения к панели
# одного хоста (создание/обновление/удаление клиентов), независимо от того,
# что именно их инициировало: плановая сверка шедулера или ручная покупка/
# продление через create_or_update_key_on_host.
#
# Раньше от гонок защищал только emails_in_transition, а он ловит только
# повторный вызов с тем же email. Если параллельно на одном хосте шедулер
# синхронизирует один ключ, а пользователь в этот момент продлевает другой —
# оба потока бьют по одной и той же панели одновременно, что на медленном
# хосте (рестарт xray на каждый addClient/updateClient) само по себе
# провоцирует пустые ответы и "empty client ID" у обоих. Один lock на хост
# устраняет этот класс гонок целиком, а не только частный случай одного email.
# -------------------------------------------------------------------------
_host_locks: dict[str, asyncio.Lock] = {}
_host_locks_guard = asyncio.Lock()


async def get_host_lock(host_name: str) -> asyncio.Lock:
    """Возвращает (создавая при необходимости) asyncio.Lock, общий для хоста host_name.

    Создание самого Lock-а тоже под защитой _host_locks_guard — без этого
    два первых параллельных обращения к новому хосту могли бы создать два
    разных Lock-объекта и оба решить, что они его владельцы.
    """
    async with _host_locks_guard:
        lock = _host_locks.get(host_name)
        if lock is None:
            lock = asyncio.Lock()
            _host_locks[host_name] = lock
        return lock


def is_hysteria_protocol(protocol: str | None) -> bool:
    """Порт IsHysteria() из database/model/model.go реальной панели 3x-ui: там hysteria2 — отдельное значение протокола, не 'hysteria'.

    Панель на
    бэкенде для обоих значений валидирует клиента по полю Auth, а не ID
    (см. web/service/inbound.go: `case "hysteria", "hysteria2":`).
    Раньше везде в проекте сравнивали только с 'hysteria' — из-за этого
    Hysteria2-инбаунды ошибочно не оборачивались в HysteriaClientWrapper,
    auth не проставлялся, и панель отвечала "empty client ID".
    """
    return (protocol or "").lower() in ("hysteria", "hysteria2")


# --- Вспомогательные классы ---


class HysteriaClientWrapper:
    """
    Прокси-обертка для объекта Client (py3xui).

    Гарантирует принудительную передачу поля 'auth' и очистку 'flow'
    при сериализации (model_dump) для совместимости с протоколом Hysteria.
    """

    def __init__(self, orig: Client, auth_val: str):
        """Оборачивает клиента orig, подставляя auth_val как значение поля 'auth'."""
        self._orig = orig
        self._auth = auth_val

    def model_dump(self, *args, **kwargs):
        """Сериализует обёрнутого клиента, принудительно проставляя auth/id/flow."""
        d = self._orig.model_dump(*args, **kwargs)
        d["auth"] = self._auth
        d["id"] = self._auth
        d["flow"] = ""
        return d

    def __getattr__(self, name):
        """Прозрачно делегирует доступ к любому атрибуту оригинального клиента."""
        return getattr(self._orig, name)


def fetch_raw_client_auth_map(api: Api, inbound_id: int) -> dict[str, str]:
    """
    Достаёт реальный auth каждого клиента напрямую из сырого JSON панели, в обход модели Client из py3xui.

    Почему это нужно: py3xui.Client не объявляет поле 'auth' вообще (только
    email/id/password/... — auth среди них нет), а ConfigDict модели не
    задаёт extra='allow'. Pydantic по умолчанию молча отбрасывает незнакомые
    поля при парсинге — значит любой hysteria-клиент, прочитанный через
    api.inbound.get_list()/get_by_id(), теряет свой настоящий auth уже на
    этапе парсинга, и getattr(client, 'auth', None) всегда вернёт None,
    независимо от того, что реально хранится на панели.

    Это критично, потому что X-UI на бэкенде (web/service/inbound.go,
    UpdateInboundClient) для Hysteria/Hysteria2 ищет клиента для обновления
    сравнивая URL-параметр clientId с текущим (уже сохранённым) auth клиента,
    а не с чем-то, что мы сами придумали. Если передать вместо этого свой
    client_uuid — совпадения не будет никогда, и панель ответит
    "empty client ID", даже если наш собственный payload корректен.

    Возвращает {email.lower(): auth}. Пустой словарь при любой ошибке —
    вызывающий код должен трактовать это как "не знаем auth" и решать сам,
    как реагировать (см. использование ниже).
    """
    try:
        endpoint = f"panel/api/inbounds/get/{inbound_id}"
        url = api.inbound._url(endpoint)  # pylint: disable=protected-access
        headers = {"Accept": "application/json"}
        response = api.inbound._get(url, headers)  # pylint: disable=protected-access
        obj = response.json().get("obj") or {}
        settings_raw = obj.get("settings")
        settings = (
            json.loads(settings_raw) if isinstance(settings_raw, str) else (settings_raw or {})
        )
        clients = settings.get("clients", []) or []
        return {
            str(c.get("email", "")).strip().lower(): str(c.get("auth", ""))
            for c in clients
            if c.get("email")
        }
    except Exception as e:
        logger.warning(f"Не удалось получить сырой auth-маппинг для инбаунда {inbound_id}: {e}")
        return {}


def _resolve_hysteria_update(
    api: Api, email_lower: str, inbound_id: int, uuid_val: str
) -> tuple[str, str]:
    """Возвращает identifier_for_url, new_auth_for_payload для одиночного (не в цикле) обновления Hysteria-клиента — без кэширования, т.к. вызывается один раз на мастера, а не по многу раз на инбаунд, как в циклах репликации клонов.

    Подробности — в docstring fetch_raw_client_auth_map.
    """
    current_auth = fetch_raw_client_auth_map(api, inbound_id).get(email_lower)
    if current_auth:
        return current_auth, uuid_val
    return uuid_val, uuid_val


# --- Синхронные вспомогательные функции ---


def login_to_host(
    host_url: str, username: str, password: str, inbound_id: int
) -> tuple[Api | None, Inbound | None]:
    """
    Выполняет авторизацию на панели X-UI / 3X-UI и возвращает объект API + целевой inbound.

    Основная задача функции:
    • Установить соединение с панелью
    • Проверить успешный логин
    • Загрузить список всех inbound'ов
    • Найти и вернуть inbound с указанным ID

    Аргументы:
        host_url:    Полный URL панели (например: https://panel.example.com:54321)
        username:    Логин администратора панели
        password:    Пароль администратора
        inbound_id:  ID inbound'а, с которым будет работать дальнейшая логика

    Возвращает:
        tuple[Api | None, Inbound | None]:
            (api, target_inbound)     — при успехе
            (None, None)              — при любой ошибке (неверные credentials, inbound не найден, проблемы сети и т.д.)

    Примечания:
    • Функция не проверяет права доступа — предполагается, что переданный пользователь имеет полный доступ
    • При ошибке авторизации или отсутствии inbound'а всегда возвращается (None, None)
    • Логирование ошибок включает полный стек вызовов (exc_info=True)
    • Функция синхронная (блокирующий HTTP). Вызывать из async-кода только через asyncio.to_thread
    """
    try:
        # -------------------------------------------------------------------------
        # 1. Инициализация объекта API и попытка авторизации
        # -------------------------------------------------------------------------
        api = Api(host=host_url, username=username, password=password)

        # Выполняем вход. Если credentials неверные → выбросит исключение
        api.login()

        # -------------------------------------------------------------------------
        # 2. Получение полного списка inbound'ов с панели
        # -------------------------------------------------------------------------
        inbounds: List[Inbound] = api.inbound.get_list()

        if not inbounds:
            logger.warning(f"На хосте {host_url} не найдено ни одного inbound")
            return api, None

        # -------------------------------------------------------------------------
        # 3. Поиск нужного inbound по ID
        # -------------------------------------------------------------------------
        target_inbound = next((inbound for inbound in inbounds if inbound.id == inbound_id), None)

        if target_inbound is None:
            logger.error(
                f"Inbound с ID {inbound_id} не найден на хосте {host_url}. "
                f"Доступные ID: {[ib.id for ib in inbounds]}"
            )
            return api, None

        # -------------------------------------------------------------------------
        # 4. Успешный результат
        # -------------------------------------------------------------------------
        logger.debug(
            f"Успешная авторизация на {host_url}, найден inbound {inbound_id} "
            f"(remark: {getattr(target_inbound, 'remark', '—')})"
        )

        return api, target_inbound

    except Exception as e:
        # -------------------------------------------------------------------------
        # 5. Обработка всех возможных ошибок
        # -------------------------------------------------------------------------
        logger.error(
            f"Не удалось авторизоваться или получить inbound на хосте '{host_url}': {e}",
            exc_info=True,
        )
        return None, None


def login_and_get_inbounds(
    host_url: str, username: str, password: str, inbound_id: int
) -> tuple[Api | None, Inbound | None, List[Inbound] | None]:
    """
    Логин + один api.inbound.get_list() за вызов.

    Зачем нужен:
    login_to_host возвращает только (api, master_inbound) — полный список
    инбаундов, который он тянет внутри себя, наружу не отдаётся. Вызывающий
    код (scheduler.py) был вынужден сразу после login_to_host делать ещё
    один api.inbound.get_list() — второй тяжёлый запрос подряд с тем же
    результатом. Эта функция отдаёт список наружу, чтобы весь цикл
    планировщика (синхронизация клонов + проверка аномалий трафика) мог
    переиспользовать один и тот же снимок инбаундов вместо 2-3 отдельных
    тяжёлых запросов на сервер за цикл.

    Возвращает: api, master_inbound, all_inbounds или None, None, None при ошибке.
    """
    try:
        api = Api(host=host_url, username=username, password=password)
        api.login()

        inbounds: List[Inbound] = api.inbound.get_list()
        if not inbounds:
            logger.warning(f"На хосте {host_url} не найдено ни одного inbound")
            return api, None, []

        target_inbound = next((ib for ib in inbounds if ib.id == inbound_id), None)
        if target_inbound is None:
            logger.error(
                f"Inbound с ID {inbound_id} не найден на хосте {host_url}. "
                f"Доступные ID: {[ib.id for ib in inbounds]}"
            )
            return api, None, inbounds

        return api, target_inbound, inbounds

    except Exception as e:
        logger.error(
            f"Не удалось авторизоваться или получить inbound'ы на хосте '{host_url}': {e}",
            exc_info=True,
        )
        return None, None, None


def _login_only(host_url: str, username: str, password: str) -> Api | None:
    """
    Облегчённый вариант login_to_host: только авторизация, без немедленного api.inbound.get_list().

    Зачем нужен:
    login_to_host всегда тянет полный список инбаундов (со всеми клиентами
    и статистикой трафика) — это тяжёлый запрос к панели. Часть операций
    (удаление клиента, точечный поиск по email) не нуждается в этом списке
    сразу при логине — они либо получают список сами один раз чуть позже,
    либо вообще обходятся без него (get_by_email). Использование этой
    функции вместо login_to_host убирает один лишний "тяжёлый" запрос
    к панели на каждый такой вызов.
    """
    try:
        api = Api(host=host_url, username=username, password=password)
        api.login()
        return api
    except Exception as e:
        logger.error(f"Не удалось авторизоваться на хосте '{host_url}': {e}", exc_info=True)
        return None


def get_connection_string(
    inbound: Inbound, user_uuid: str, host_url: str, remark: str
) -> str | None:
    """
    Формирует прямую VLESS-ссылку с транспортным протоколом REALITY (xtls-rprx-vision).

    Важные особенности:
    • Используется исключительно для генерации одиночной VLESS-ссылки (не для подписки)
    • Работает только с inbound'ами, у которых включён REALITY (security = "reality")
    • Берёт первый shortId и первый serverName из настроек (самый распространённый сценарий)
    • Поддерживает только tcp-реальность (без grpc, ws, httpupgrade и т.д.)

    Аргументы:
        inbound:     Объект Inbound из API панели (должен содержать stream_settings с reality_settings)
        user_uuid:   UUID клиента (vless uuid)
        host_url:    Базовый URL панели (например: https://panel.example.com:8443)
                     Используется только для извлечения hostname
        remark:      Название/метка для клиента (отображается в клиенте как имя конфигурации)

    Возвращает:
        str | None:
            Готовая vless://... ссылка с параметрами REALITY
            или None, если:
            - inbound отсутствует
            - нет настроек REALITY
            - отсутствует хотя бы один обязательный параметр (publicKey, serverNames, shortIds)

    Примечание:
    • Функция не добавляет параметры flow=xtls-rprx-vision в конец, если он уже есть
    • spx=%2F — стандартное значение для REALITY (path /)
    • Используется первый элемент из списков serverNames и shortIds
    """
    # -------------------------------------------------------------------------
    # 1. Проверка наличия inbound и доступа к настройкам REALITY
    # -------------------------------------------------------------------------
    if not inbound:
        return None

    # Проверяем, что у inbound вообще есть stream_settings и reality_settings
    reality_settings = getattr(inbound.stream_settings, "reality_settings", None)
    if not reality_settings or not isinstance(reality_settings, dict):
        return None

    # -------------------------------------------------------------------------
    # 2. Извлечение ключевых параметров REALITY
    # -------------------------------------------------------------------------
    settings = reality_settings.get("settings", {})
    public_key = settings.get("publicKey")
    fp = settings.get("fingerprint")
    server_names = reality_settings.get("serverNames", [])
    short_ids = reality_settings.get("shortIds", [])

    # Проверяем наличие всех обязательных полей
    if not all([public_key, server_names, short_ids]):
        logger.warning(
            f"Недостаточно данных REALITY для inbound {inbound.id} "
            f"(publicKey/serverNames/shortIds отсутствуют или пустые)"
        )
        return None

    # -------------------------------------------------------------------------
    # 3. Подготовка базовых компонентов ссылки
    # -------------------------------------------------------------------------
    parsed_url = urlparse(host_url)
    hostname = parsed_url.hostname

    if not hostname:
        logger.warning(f"Не удалось распарсить hostname из host_url: {host_url}")
        return None

    # Берём первые значения (самый частый и обычно рабочий вариант)
    sni = server_names[0]
    short_id = short_ids[0]
    port = inbound.port

    # -------------------------------------------------------------------------
    # 4. Сборка строки подключения VLESS + REALITY
    # -------------------------------------------------------------------------
    connection_string = (
        f"vless://{user_uuid}@{hostname}:{port}"
        f"?type=tcp"
        f"&security=reality"
        f"&pbk={public_key}"
        f"&fp={fp}"
        f"&sni={sni}"
        f"&sid={short_id}"
        f"&spx=%2F"  # Path = /
        f"&flow=xtls-rprx-vision"  # Обязательный flow для vision-реальности
        f"#{remark or 'VLESS-REALITY'}"  # Remark как имя конфигурации
    )

    return connection_string


def get_subscription_link(
    user_uuid: str,
    host_url: str,
    host_name: str | None = None,
    sub_token: str | None = None,
) -> str:
    """
    Формирует полный URL подписки (subscription link) для клиента.

    Приоритеты определения базового URL (по убыванию приоритета):
    1. Специфичная для хоста ссылка из конфигурации (subscription_url в get_host)
    2. Глобальная настройка домена (get_setting("domain")) + путь /sub/
    3. Автоматически извлечённый hostname из host_url + путь /sub/

    Особенности обработки токена:
    • Если sub_token передан → он используется вместо user_uuid
    • Если в subscription_url есть плейсхолдер {token} → производится замена
    • Если плейсхолдера нет → токен добавляется в конец пути
    • Если sub_token = None → используется user_uuid (старый/совместимый режим)

    Аргументы:
        user_uuid:   UUID клиента (используется как fallback-идентификатор)
        host_url:    URL панели (например: https://panel.example.com:54321)
                     Используется для извлечения scheme и hostname при fallback
        host_name:   Имя хоста (ключ в конфигурации), опционально
                     Позволяет использовать индивидуальную subscription_url
        sub_token:   Токен подписки (обычно secrets.token_hex(12))
                     Если передан — имеет приоритет над user_uuid

    Возвращает:
        str: Полный URL подписки, всегда валидный (с https по умолчанию при ошибке scheme)

    Примеры возвращаемых значений:
        • https://panel.mydomain.com/sub/abc123def456
        • https://custom-sub.domain.net/xyz789?format=v2ray
        • https://panel.example.com/sub/{token}   → после замены
    """
    # -------------------------------------------------------------------------
    # 1. Попытка получить специфичную для хоста базовую ссылку подписки
    # -------------------------------------------------------------------------
    host_base = None
    if host_name:
        try:
            host_config = get_host(host_name)
            if host_config:
                host_base = (host_config.get("subscription_url") or "").strip()
        except Exception as e:
            logger.warning(
                f"Не удалось получить конфигурацию хоста '{host_name}' "
                f"при формировании subscription link: {e}"
            )

    # -------------------------------------------------------------------------
    # 2. Определяем базовый путь (приоритет: host-specific → fallback)
    # -------------------------------------------------------------------------
    base = (host_base or "").strip()

    # -------------------------------------------------------------------------
    # 3. Сценарий: передан sub_token (современный режим)
    # -------------------------------------------------------------------------
    if sub_token:
        if base:
            # Вариант А: есть кастомная subscription_url
            if "{token}" in base:
                # Есть плейсхолдер → простая замена
                return base.replace("{token}", sub_token)
            else:
                # Нет плейсхолдера → добавляем токен в конец пути
                return f"{base.rstrip('/')}/{sub_token}"

        # Вариант Б: нет кастомной ссылки → используем дефолтный путь
        domain = (get_setting("domain") or "").strip()
        parsed = urlparse(host_url)

        hostname = domain if domain else (parsed.hostname or "localhost")
        scheme = parsed.scheme if parsed.scheme in ("http", "https") else "https"

        return f"{scheme}://{hostname}/sub/{sub_token}"

    # -------------------------------------------------------------------------
    # 4. Сценарий: sub_token не передан → используем user_uuid (legacy-совместимость)
    # -------------------------------------------------------------------------
    if base:
        # Если есть кастомная базовая ссылка — возвращаем её как есть
        return base

    # Fallback: дефолтный путь с user_uuid + параметр format=v2ray
    domain = (get_setting("domain") or "").strip()
    parsed = urlparse(host_url)

    hostname = domain if domain else (parsed.hostname or "localhost")
    scheme = parsed.scheme if parsed.scheme in ("http", "https") else "https"

    return f"{scheme}://{hostname}/sub/{user_uuid}?format=v2ray"


def find_and_remove_client_from_other_inbounds(
    api: Api, email: str, target_inbound_id: int
) -> bool:
    """
    Ищет клиента с указанным email на всех inbound'ах, кроме заданного target_inbound_id, и удаляет его оттуда, если находит.

    Основная цель функции:
    • Предотвращение ошибки «Duplicate email» при создании/обновлении клиента
    • Гарантия, что email существует максимум в одном inbound'е (мастер-инбаунде)

    Поведение:
    • Если клиент найден на других inbound'ах → удаляем его
    • Если не найден нигде кроме target → считаем успехом (True)
    • Если возникла ошибка API при удалении хотя бы одного → возвращаем False
    • Пропускаем inbound, если у клиента нет валидного id (UUID)

    Аргументы:
        api:                Активная сессия API панели X-UI
        email:              Email клиента, который нужно «очистить» с других портов
        target_inbound_id:  ID inbound'а, который НЕ нужно трогать (там будет мастер)

    Возвращает:
        bool:
            True  → операция завершена успешно (удалено всё лишнее или не было лишнего)
            False → произошла ошибка API / исключение при попытке удаления

    Примечание: синхронная функция — вызывать через asyncio.to_thread из async-кода
    """
    try:
        # -------------------------------------------------------------------------
        # 1. Получаем полный список всех inbound'ов на панели
        # -------------------------------------------------------------------------
        inbounds: List[Inbound] = api.inbound.get_list()

        has_errors = False
        found_and_removed = False

        for inbound in inbounds:
            # Пропускаем целевой inbound — там мы сами будем создавать/обновлять клиента
            if inbound.id == target_inbound_id:
                continue

            # -------------------------------------------------------------------------
            # 2. Получаем список клиентов на текущем inbound'е
            # -------------------------------------------------------------------------
            clients = (
                inbound.settings.clients if inbound.settings and inbound.settings.clients else []
            )
            if not clients:
                continue

            # Ищем клиента строго по email
            existing_client = next((c for c in clients if getattr(c, "email", None) == email), None)

            if not existing_client:
                continue

            # -------------------------------------------------------------------------
            # 3. Клиент найден на «чужом» inbound'е → пытаемся удалить
            # -------------------------------------------------------------------------
            client_uuid = getattr(existing_client, "id", None) or getattr(
                existing_client, "auth", None
            )

            if not client_uuid:
                logger.warning(
                    f"Обнаружен клиент '{email}' на inbound {inbound.id}, "
                    f"но у него отсутствует 'id'/'auth' (UUID). Удаление пропущено."
                )
                continue

            logger.warning(
                f"Найден дублирующий клиент '{email}' (UUID/Auth: {client_uuid}) "
                f"на inbound {inbound.id}. Производим удаление..."
            )

            # Пытаемся удалить
            success = api.client.delete(inbound.id, str(client_uuid))

            if success:
                logger.info(
                    f"Клиент '{email}' (UUID/Auth: {client_uuid}) успешно удалён "
                    f"с inbound {inbound.id}"
                )
                found_and_removed = True
            else:
                logger.error(
                    f"Не удалось удалить клиента '{email}' (UUID/Auth: {client_uuid}) "
                    f"с inbound {inbound.id} — метод delete вернул False"
                )
                has_errors = True

            # Продолжаем проверку остальных inbound'ов (на случай редких дублей)

        # -------------------------------------------------------------------------
        # 4. Итоговый результат
        # -------------------------------------------------------------------------
        if found_and_removed:
            logger.info(f"Очистка дубликатов email '{email}' завершена успешно.")
        else:
            logger.debug(f"Дубликатов email '{email}' на других inbound'ах не обнаружено.")

        # Возвращаем True, если не было критических ошибок удаления
        return not has_errors

    except Exception as e:
        # -------------------------------------------------------------------------
        # 5. Любое непредвиденное исключение → считаем неудачей
        # -------------------------------------------------------------------------
        logger.error(
            f"Критическая ошибка при очистке дубликатов клиента '{email}' "
            f"(target inbound {target_inbound_id}): {e}",
            exc_info=True,
        )
        return False


def update_or_create_client_on_panel(
    api: Api,
    inbound_id: int,
    email: str,
    days_to_add: int | None = None,
    months_to_add: int | None = None,
    target_expiry_ms: int | None = None,
    all_inbounds: List[Inbound] | None = None,
) -> tuple[str | None, int | None, str | None]:
    """
    Создаёт нового клиента или обновляет существующего на панели 3X-UI / X-UI.

    Ключевые особенности и исправления:
    • Глобальный поиск клиента по email по всем inbound'ам (защита от Duplicate email)
    • Принудительная подмена числового ID на строковый UUID (решает проблему "empty client ID")
    • Корректный расчёт нового времени истечения при продлении
    • Сохранение / генерация токена подписки (subId)

    Аргументы:
        api:                Активная сессия API панели
        inbound_id:         ID inbound'а, на котором нужно создать/обновить клиента
        email:              Уникальный email клиента (используется как ключ поиска)
        days_to_add:        Количество дней для продления (относительно текущего/нового срока)
        months_to_add:      Количество календарных месяцев для продления (относительно
                             текущего/нового срока) — приоритетнее days_to_add, если
                             задано. В отличие от days_to_add=months*30, учитывает
                             реальную длину месяца (31 января + 1 месяц = 28/29 февраля,
                             а не "выезд" на 3 марта)
        target_expiry_ms:   Абсолютное время истечения в миллисекундах (приоритетнее обоих)

    Возвращает:
        tuple[str | None, int | None, str | None]:
            (client_uuid, new_expiry_ms, subscription_token)
            или (None, None, None) при любой ошибке

    Важно:
    • Функция работает с любым inbound'ом, где найден клиент (не обязательно с переданным inbound_id)
    • При обновлении используется UUID, полученный из существующей записи
    • Функция полностью синхронная (все вызовы api.inbound/api.client — блокирующий HTTP)
    """
    try:
        # -------------------------------------------------------------------------
        # 1. Глобальный поиск клиента по email среди всех inbound'ов
        #    Это главное отличие от наивного подхода — позволяет избежать дублирования
        #
        #    Оптимизация нагрузки на панель: если список инбаундов уже был получен
        #    вызывающей функцией (например, _create_or_update_key_on_host_sync),
        #    используем его, не делая повторный тяжёлый запрос api.inbound.get_list().
        # -------------------------------------------------------------------------
        if all_inbounds is None:
            all_inbounds = api.inbound.get_list()
        existing_client = None
        current_inbound = None

        for ib in all_inbounds:
            clients = ib.settings.clients if ib.settings and ib.settings.clients else []
            existing_client = next((c for c in clients if getattr(c, "email", None) == email), None)
            if existing_client:
                current_inbound = ib
                break

        if not current_inbound:
            current_inbound = next((ib for ib in all_inbounds if ib.id == inbound_id), None)

        is_hysteria = current_inbound and is_hysteria_protocol(
            getattr(current_inbound, "protocol", "")
        )

        # -------------------------------------------------------------------------
        # 2. Определение нового времени истечения (expiry_time)
        # -------------------------------------------------------------------------
        if target_expiry_ms is not None:
            # Явно указано абсолютное значение → используем его
            new_expiry_ms = int(target_expiry_ms)
        else:
            if days_to_add is None and months_to_add is None:
                raise ValueError(
                    "Необходимо указать хотя бы один из параметров: days_to_add, months_to_add или target_expiry_ms"
                )

            # Точка отсчёта: текущее expiry_time (если в будущем) или сейчас
            expiry_time_ms = getattr(existing_client, "expiry_time", 0)
            now_ms = int(datetime.now().timestamp() * 1000)

            if existing_client and expiry_time_ms > now_ms:
                start_dt = datetime.fromtimestamp(expiry_time_ms / 1000)
            else:
                start_dt = datetime.now()

            if months_to_add is not None:
                # Реальные календарные месяцы от даты покупки/точки отсчёта —
                # relativedelta сам учитывает длину конкретного месяца
                # (31 янв + 1 мес = 28/29 фев, а не 3 марта)
                new_expiry_ms = int(
                    (start_dt + relativedelta(months=months_to_add)).timestamp() * 1000
                )
            else:
                # Добавляем указанное количество дней
                new_expiry_ms = int((start_dt + timedelta(days=days_to_add)).timestamp() * 1000)

        client_uuid: str | None = None
        client_sub_token: str | None = None

        if existing_client:
            # -------------------------------------------------------------------------
            # 3. Обновление существующего клиента
            # -------------------------------------------------------------------------
            client_uuid = str(
                getattr(existing_client, "id", "") or getattr(existing_client, "auth", "")
            )

            if not client_uuid:
                raise ValueError(f"Существующий клиент {email} не имеет валидного ID/Auth")

            # Важный фикс: API часто требует именно строковый UUID в поле id
            # Поэтому загружаем объект по email и принудительно устанавливаем строковый id
            client_for_update = api.client.get_by_email(email)
            client_for_update.id = client_uuid

            # -------------------------------------------------------------------------
            # 3.1. Работа с токеном подписки (subId)
            # -------------------------------------------------------------------------
            sub_token_existing = None
            for attr in ["sub_id"]:
                val = getattr(existing_client, attr, None)
                if val:
                    sub_token_existing = val
                    break

            if not sub_token_existing:
                client_sub_token = secrets.token_hex(12)
                for attr in ["sub_id"]:
                    try:
                        setattr(client_for_update, attr, client_sub_token)
                    except Exception:
                        pass
            else:
                client_sub_token = sub_token_existing

            # Активируем и обновляем срок действия
            client_for_update.enable = True
            client_for_update.expiry_time = new_expiry_ms

            # Сбрасываем счётчики трафика (если поле доступно)
            try:
                setattr(client_for_update, "reset", 0)
            except Exception:
                pass

            if is_hysteria:
                update_identifier, new_auth = _resolve_hysteria_update(
                    api, email.strip().lower(), inbound_id, client_uuid
                )
                wrapped_client = HysteriaClientWrapper(client_for_update, new_auth)
                call_with_retry(api.client.update, update_identifier, wrapped_client)
            else:
                call_with_retry(api.client.update, client_uuid, client_for_update)

        else:
            # -------------------------------------------------------------------------
            # 4. Создание нового клиента
            # -------------------------------------------------------------------------
            client_uuid = str(uuid.uuid4())
            client_sub_token = secrets.token_hex(12)

            new_client = Client(
                id=client_uuid,
                email=email,
                enable=True,
                flow="",
                expiry_time=new_expiry_ms,
            )

            # Сброс счётчиков трафика
            try:
                setattr(new_client, "reset", 0)
            except Exception:
                pass

            for attr in ["sub_id"]:
                try:
                    setattr(new_client, attr, client_sub_token)
                except Exception:
                    pass

            if is_hysteria:
                wrapped_client = HysteriaClientWrapper(new_client, client_uuid)
                add_client_robust(api, inbound_id, [wrapped_client], email)
            else:
                add_client_robust(api, inbound_id, [new_client], email)

        # -------------------------------------------------------------------------
        # 5. Успешный результат
        # -------------------------------------------------------------------------
        return client_uuid, new_expiry_ms, client_sub_token

    except Exception as e:
        logger.error(
            f"Ошибка в update_or_create_client_on_panel (email={email}, inbound={inbound_id}): {e}",
            exc_info=True,
        )
        return None, None, None


def _create_or_update_key_on_host_sync(
    host_data: dict,
    host_name: str,
    email: str,
    days_to_add: int | None,
    expiry_timestamp_ms: int | None,
    months_to_add: int | None = None,
) -> Dict | None:
    """
    Синхронная реализация create_or_update_key_on_host (см. описание ниже).

    Выполняет всю работу с панелью (логин, чтение/запись клиентов, репликация клонов)
    без единого await — вся функция целиком уходит в отдельный поток через
    asyncio.to_thread из публичной async-обёртки.
    """
    # -------------------------------------------------------------------------
    # 2. Авторизация в API панели X-UI
    #
    #    Оптимизация нагрузки на панель: раньше здесь было до трёх отдельных
    #    api.inbound.get_list() подряд (в login_to_host, здесь и внутри
    #    update_or_create_client_on_panel) — каждый такой вызов возвращает
    #    полный список инбаундов со всеми клиентами и статистикой трафика,
    #    это тяжёлый запрос для панели. Теперь список получаем один раз
    #    и переиспользуем во всей функции.
    # -------------------------------------------------------------------------
    api = _login_only(
        host_url=host_data["host_url"],
        username=host_data["host_username"],
        password=host_data["host_pass"],
    )
    if not api:
        logger.error(f"Не удалось авторизоваться на хосте {host_name}")
        return None

    all_inbounds = api.inbound.get_list()
    target_inbound = next(
        (ib for ib in all_inbounds if ib.id == host_data["host_inbound_id"]), None
    )
    if not target_inbound:
        logger.error(f"Inbound с ID {host_data['host_inbound_id']} не найден на хосте {host_name}")
        return None

    # -------------------------------------------------------------------------
    # 3. Парсинг и нормализация базовой части имени пользователя
    # Примеры:
    #   vol_choke_2-1@snowyservices_bot   → username_raw = vol_choke
    #   alex_3@custom.domain      → username_raw = alex
    # -------------------------------------------------------------------------
    full_name = email.split("@")[0]
    domain = email.split("@")[1] if "@" in email else EMAIL_DOMAIN
    username_base = full_name.split("-")[0]
    username_raw = re.sub(r"_\d+$", "", username_base)

    # -------------------------------------------------------------------------
    # 4. Собираем все занятые email на всей панели (регистронезависимо)
    # Это нужно для предотвращения Duplicate email error
    # (all_inbounds уже получен на шаге авторизации выше — повторный запрос не нужен)
    # -------------------------------------------------------------------------
    taken_emails_panel = {
        c.email.lower()
        for ib in all_inbounds
        if ib.settings and ib.settings.clients
        for c in ib.settings.clients
        if hasattr(c, "email") and c.email
    }

    # -------------------------------------------------------------------------
    # 5. Проверяем, существует ли уже этот ключ в локальной базе
    #    (обычная синхронная функция БД — мы уже в отдельном потоке, await не нужен)
    # -------------------------------------------------------------------------
    current_key_db = get_key_by_email(email)

    # -------------------------------------------------------------------------
    # 6. Определяем финальный master email
    #
    # Вариант А: уже есть в БД → используем существующий (продление)
    # Вариант Б: новый → подбираем свободный индекс _1, _2, _3...
    # Проверяем одновременно и панель, и БД → защита от UNIQUE violation
    # -------------------------------------------------------------------------
    if current_key_db:
        subscription_id = username_base
        final_master_email = email
    else:
        counter = 1
        while True:
            candidate_id = username_raw if counter == 1 else f"{username_raw}_{counter}"
            candidate_email = f"{candidate_id}@{domain}".lower()

            if (
                candidate_email not in taken_emails_panel
                and get_key_by_email(candidate_email) is None
            ):
                # taken_emails_panel — снэпшот с момента логина в начале этого
                # вызова, а не текущее состояние панели. host_lock уже
                # сериализует эту функцию относительно шедулера и других
                # покупок на этом же хосте, но не защищает от путей, которые
                # мутируют панель в обход host_lock (например, ручные
                # действия админа напрямую через веб-панель). Прежде чем
                # окончательно принять кандидата — один живой чек прямо
                # перед записью, а не полагаемся только на устаревший снэпшот.
                try:
                    live_ib = api.inbound.get_by_id(target_inbound.id)
                    live_taken = {
                        str(getattr(c, "email", "")).strip().lower()
                        for c in ((live_ib.settings.clients or []) if live_ib else [])
                    }
                except Exception as e:
                    # Логируем, а не молча глотаем.
                    logger.warning(
                        f"Живой чек занятости {candidate_email} не удался, "
                        f"полагаюсь только на устаревший снэпшот: {e}"
                    )
                    live_taken = set()
                if candidate_email not in live_taken:
                    subscription_id = candidate_id
                    final_master_email = candidate_email
                    break
                taken_emails_panel.add(candidate_email)

            counter += 1
            if counter > 999:
                logger.error(f"Не удалось подобрать свободный email для {username_raw}")
                return None

    # -------------------------------------------------------------------------
    # 7. Создаём / обновляем мастер-клиента на целевом (основном) инбаунде
    # -------------------------------------------------------------------------
    client_uuid, new_expiry_ms, client_sub_token = update_or_create_client_on_panel(
        api=api,
        inbound_id=target_inbound.id,
        email=final_master_email,
        days_to_add=days_to_add,
        months_to_add=months_to_add,
        target_expiry_ms=expiry_timestamp_ms,
        all_inbounds=all_inbounds,
    )

    if not client_uuid:
        logger.error(f"Не удалось создать/обновить мастера {final_master_email}")
        return None

    time.sleep(0.3)

    # -------------------------------------------------------------------------
    # 8. Репликация клиента на все инбаунды с меткой
    #     • одинаковый UUID
    #     • разные email: <subscription_id>-2@, <subscription_id>-3@ и т.д.,
    #       где номер = порядковая позиция инбаунда среди всех помеченных
    #       (🌍/🏳️), включая мастер-инбаунд — он всегда занимает позицию 1
    #       (сам без суффикса), поэтому первый клон получает -2, а не -1.
    #       Та же конвенция, что и в scheduler.py — единообразная нумерация
    #       независимо от того, кто именно (оплата или плановая сверка)
    #       создал клон первым
    # -------------------------------------------------------------------------
    tagged_non_master_ordered = sorted(
        (
            ib
            for ib in all_inbounds
            if ib.id != target_inbound.id
            and (
                "🌍" in getattr(ib, "remark", "").lower()
                or "🏳️" in getattr(ib, "remark", "").lower()
            )
        ),
        key=lambda ib: ib.id,
    )
    ordinal_by_inbound_id = {ib.id: idx + 2 for idx, ib in enumerate(tagged_non_master_ordered)}

    # Кэш реального auth hysteria-клиентов по инбаунду — та же логика, что и
    # в scheduler.py (см. подробный docstring fetch_raw_client_auth_map).
    # Один GET на инбаунд за весь вызов, не по одному на клиента.
    hysteria_auth_cache: dict[int, dict[str, str]] = {}

    def get_hysteria_auth_map(ib_id: int) -> dict[str, str]:
        if ib_id not in hysteria_auth_cache:
            hysteria_auth_cache[ib_id] = fetch_raw_client_auth_map(api, ib_id)
        return hysteria_auth_cache[ib_id]

    def resolve_hysteria_update(email_lower: str, ib_id: int, uuid_val: str) -> tuple[str, str]:
        """(identifier_для_url, новый_auth_для_payload) — см. подробности в аналогичной функции в scheduler.py."""
        current_auth = get_hysteria_auth_map(ib_id).get(email_lower)
        if current_auth:
            return current_auth, uuid_val
        return uuid_val, uuid_val

    try:
        updated_count = 0
        created_count = 0
        # Обходим в порядке убывания id — та же логика, что и в scheduler.py:
        # если у цепочки клонов одного ключа номер должен сдвинуться на +1,
        # переименование должно начинаться со старшего номера (его целевой
        # слот гарантированно свободен), тогда вся цепочка схлопывается за
        # один проход вместо одного звена за запуск.
        for inbound in sorted(all_inbounds, key=lambda ib: -ib.id):
            # Пропускаем мастер-инбаунд и инбаунды без метки
            remark = getattr(inbound, "remark", "").lower()
            if inbound.id == target_inbound.id or ("🌍" not in remark and "🏳️" not in remark):
                continue

            is_clone_hysteria = is_hysteria_protocol(getattr(inbound, "protocol", ""))
            clients = inbound.settings.clients or []
            prefix_lower = f"{subscription_id}-".lower()

            # Сначала ищем уже существующий клон на этом инбаунде по
            # идентичности (uuid/auth), а не по заранее вычисленному email:
            # у клона может быть любой номер, доставшийся ему при прошлой
            # репликации, и это не повод создавать для него ещё один
            existing_clone = next(
                (
                    c
                    for c in clients
                    if getattr(c, "id", None) == client_uuid
                    or getattr(c, "auth", None) == client_uuid
                ),
                None,
            )
            if not existing_clone:
                existing_clone = next(
                    (
                        c
                        for c in clients
                        if getattr(c, "email", "")
                        and getattr(c, "email", "").lower().startswith(prefix_lower)
                    ),
                    None,
                )

            if existing_clone:
                # --- Обновление существующего клона (+ самоисправление номера) ---
                # У нас уже есть валидный объект existing_clone из all_inbounds —
                # мутируем и обновляем его напрямую, как это делает scheduler.py
                client_for_update = existing_clone
                client_for_update.inbound_id = inbound.id

                # Важный фикс: API требует строковый UUID, а не числовой ID
                client_for_update.id = client_uuid
                client_for_update.expiry_time = new_expiry_ms
                client_for_update.enable = True

                canonical_ordinal = ordinal_by_inbound_id.get(inbound.id)
                canonical_email = (
                    f"{subscription_id}-{canonical_ordinal}@{domain}".lower()
                    if canonical_ordinal
                    else None
                )
                current_clone_email = str(getattr(client_for_update, "email", "")).strip().lower()

                needs_email_fix = (
                    canonical_email is not None
                    and current_clone_email != canonical_email
                    and (
                        canonical_email not in taken_emails_panel
                        or canonical_email == current_clone_email
                    )
                )
                if needs_email_fix:
                    client_for_update.email = canonical_email
                elif canonical_email is not None and current_clone_email != canonical_email:
                    logger.warning(
                        f"Не удалось нормализовать номер клона {current_clone_email} -> {canonical_email}: "
                        f"канонический email уже занят кем-то другим на панели."
                    )

                # Синхронизируем токен подписки
                for attr in ["sub_id"]:
                    if hasattr(client_for_update, attr):
                        try:
                            setattr(client_for_update, attr, client_sub_token)
                        except Exception:
                            pass

                try:
                    setattr(client_for_update, "reset", 0)
                except Exception:
                    pass

                try:
                    if is_clone_hysteria:
                        update_identifier, new_auth = resolve_hysteria_update(
                            current_clone_email, inbound.id, client_uuid
                        )
                        wrapped_client = HysteriaClientWrapper(client_for_update, new_auth)
                        call_with_retry(api.client.update, update_identifier, wrapped_client)
                    else:
                        call_with_retry(api.client.update, client_uuid, client_for_update)

                    # Bookkeeping мутируем только после подтверждённого успеха —
                    # если мутировать заранее и вызов упадёт, taken_emails_panel
                    # разойдётся с реальным состоянием панели: старый слот будет
                    # ошибочно считаться свободным, а новый — занятым, хотя на
                    # панели ничего не изменилось. Именно так раньше возникал
                    # каскад "Duplicate email" на следующих клонах той же цепочки.
                    if needs_email_fix:
                        taken_emails_panel.discard(current_clone_email)
                        taken_emails_panel.add(canonical_email)
                        logger.info(
                            f"Нормализован номер клона: {current_clone_email} -> {canonical_email}"
                        )

                    updated_count += 1
                except Exception as e:
                    # Не даём одному сбойному клону обрушить весь платёж/продление —
                    # раньше здесь не было try/except вообще, и исключение улетало
                    # наверх, прерывая обработку остальных инбаундов этого ключа
                    if needs_email_fix:
                        client_for_update.email = current_clone_email
                    logger.warning(f"Не удалось обновить клон {current_clone_email}: {e}")

                time.sleep(0.3)

            else:
                # --- Создание нового клона под каноническим номером ---
                canonical_ordinal = ordinal_by_inbound_id.get(inbound.id)
                if canonical_ordinal is None:
                    logger.error(
                        f"Не удалось определить порядковый номер для инбаунда {inbound.id} ({getattr(inbound, 'remark', '')})"
                    )
                    continue

                sync_email = f"{subscription_id}-{canonical_ordinal}@{domain}".lower()
                if sync_email in taken_emails_panel:
                    # Канонический слот занят чужим клиентом (вероятно,
                    # orphaned-запись) — не подбираем случайный свободный
                    # номер (это и была причина исходного бага), а громко
                    # логируем и пропускаем этот инбаунд
                    logger.error(
                        f"Не удалось создать клон {sync_email}: "
                        f"канонический email уже занят кем-то другим на панели (orphaned-запись?)."
                    )
                    continue

                new_clone = Client(
                    id=client_uuid,
                    email=sync_email,
                    enable=True,
                    expiry_time=new_expiry_ms,
                    flow="",
                )

                # Устанавливаем токен подписки
                for attr in ["sub_id"]:
                    try:
                        setattr(new_clone, attr, client_sub_token)
                    except Exception:
                        pass

                try:
                    setattr(new_clone, "reset", 0)
                except Exception:
                    pass

                try:
                    if is_clone_hysteria:
                        wrapped_clone = HysteriaClientWrapper(new_clone, client_uuid)
                        add_client_robust(api, inbound.id, [wrapped_clone], sync_email)
                    else:
                        add_client_robust(api, inbound.id, [new_clone], sync_email)

                    # Bookkeeping — только после подтверждённого успеха, той же
                    # причине, что и в ветке обновления выше
                    taken_emails_panel.add(sync_email)
                    created_count += 1
                except Exception as e:
                    logger.warning(f"Не удалось создать клон {sync_email}: {e}")

                time.sleep(0.3)

        total_count = created_count + updated_count
        if total_count == 0:
            # Не нашлось ни одного инбаунда с меткой 🌍/🏳️ (кроме мастера) —
            # реплицировать было попросту нечего
            logger.info(
                f"Репликация клиента {final_master_email}: "
                f"нет инбаундов для клонирования (метки 🌍/🏳️ не найдены), пропущено."
            )
        elif created_count and updated_count:
            logger.info(
                f"Репликация клиента {final_master_email} завершена успешно. "
                f"Создано новых клонов: {created_count}, обновлено существующих: {updated_count}."
            )
        elif created_count:
            logger.info(
                f"Репликация клиента {final_master_email} завершена успешно. "
                f"Создано новых клонов: {created_count}."
            )
        else:
            logger.info(
                f"Репликация клиента {final_master_email} завершена успешно. "
                f"Обновлено существующих клонов: {updated_count} (новых клонов не потребовалось)."
            )

    except Exception as e:
        logger.error(f"Ошибка при репликации {final_master_email}: {e}", exc_info=True)
        # Здесь можно решить: откатывать мастера или оставить как есть
        # В текущей версии — оставляем (частичный успех лучше чем ничего)

    # -------------------------------------------------------------------------
    # 9. Формируем результат для сохранения в локальную базу данных
    # -------------------------------------------------------------------------
    return {
        "client_uuid": client_uuid,
        "email": final_master_email,
        "expiry_timestamp_ms": new_expiry_ms,
        "connection_string": get_subscription_link(
            user_uuid=client_uuid,
            host_url=host_data["host_url"],
            host_name=host_name,
            sub_token=client_sub_token,
        ),
        "host_name": host_name,
    }


# --- Асинхронные функции (публичный интерфейс) ---


def _rename_client_email_on_host_sync(
    host_data: dict, host_name: str, old_email: str, new_email: str
) -> bool:
    """
    Переименовывает email существующего клиента на панели X-UI, сохраняя его UUID (все выданные пользователю ссылки/конфиги остаются рабочими).

    Используется, когда триальный ключ (email вида trial_...)
    становится платным: сам клиент на панели тот же, меняется только email.

    В отличие от create_or_update_key_on_host, не создаёт нового клиента,
    если старый email не найден. Это осознанное отличие: если переименовывать
    нечего, вызывающий код должен сам решить, что делать (см. handlers.py).
    """
    api = _login_only(
        host_url=host_data["host_url"],
        username=host_data["host_username"],
        password=host_data["host_pass"],
    )
    if not api:
        logger.error(
            f"Не удалось авторизоваться на хосте {host_name} для переименования {old_email}"
        )
        return False

    try:
        all_inbounds = api.inbound.get_list()

        taken_emails_panel = {
            c.email.lower()
            for ib in all_inbounds
            if ib.settings and ib.settings.clients
            for c in ib.settings.clients
            if hasattr(c, "email") and c.email
        }
        if new_email.lower() in taken_emails_panel:
            logger.error(
                f"Переименование {old_email} -> {new_email} невозможно: email уже занят на панели"
            )
            return False

        existing_client = None
        found_inbound = None
        for ib in all_inbounds:
            clients = ib.settings.clients if ib.settings and ib.settings.clients else []
            existing_client = next(
                (c for c in clients if getattr(c, "email", None) == old_email), None
            )
            if existing_client:
                found_inbound = ib
                break

        if not existing_client:
            logger.warning(
                f"Клиент {old_email} не найден на хосте {host_name} — переименовывать нечего"
            )
            return False

        client_uuid = str(
            getattr(existing_client, "id", "") or getattr(existing_client, "auth", "")
        )
        if not client_uuid:
            logger.error(f"У клиента {old_email} нет валидного ID/Auth — переименование невозможно")
            return False

        client_for_update = api.client.get_by_email(old_email)
        client_for_update.id = client_uuid
        client_for_update.email = new_email

        is_hysteria = bool(
            found_inbound and is_hysteria_protocol(getattr(found_inbound, "protocol", ""))
        )
        if is_hysteria:
            # Внимание: client_uuid чуть выше выводится из .id/.auth
            # распарсенного py3xui-объекта — для hysteria оба поля
            # ненадёжны (.auth панель вообще не парсит, см.
            # fetch_raw_client_auth_map), так что для реально "битых"
            # случаев функция уже вернёт False на защите выше по коду
            # (безопасный отказ, а не тихая порча данных).
            update_identifier, new_auth = _resolve_hysteria_update(
                api, old_email.strip().lower(), found_inbound.id, client_uuid
            )
            wrapped_client = HysteriaClientWrapper(client_for_update, new_auth)
            call_with_retry(api.client.update, update_identifier, wrapped_client)
        else:
            call_with_retry(api.client.update, client_uuid, client_for_update)

        logger.info(f"Клиент на хосте {host_name} переименован: {old_email} -> {new_email}")
        return True
    except Exception as e:
        logger.error(
            f"Ошибка при переименовании {old_email} -> {new_email} на хосте {host_name}: {e}",
            exc_info=True,
        )
        return False


async def rename_client_email_on_host(host_name: str, old_email: str, new_email: str) -> bool:
    """Асинхронная обёртка над _rename_client_email_on_host_sync (см. docstring)."""
    host_data = await asyncio.to_thread(get_host, host_name)
    if not host_data:
        logger.error(f"Сервер '{host_name}' не найден в конфигурации (переименование {old_email}).")
        return False

    old_key = old_email.strip().lower()
    new_key = new_email.strip().lower()
    emails_in_transition.add(old_key)
    emails_in_transition.add(new_key)

    # См. подробный комментарий в create_or_update_key_on_host — та же самая
    # проблема (finally снимал флаги немедленно при таймауте, не дожидаясь
    # реального завершения фонового потока) и то же решение. Здесь нет
    # host_lock (только флаги), но тот же риск: если создание задачи бросит
    # исключение до регистрации callback, old_key/new_key останутся
    # "в обработке" навсегда — поэтому тоже под try/except.
    try:
        task = asyncio.ensure_future(
            asyncio.to_thread(
                _rename_client_email_on_host_sync,
                host_data,
                host_name,
                old_email,
                new_email,
            )
        )

        def _release_on_real_completion(_task: asyncio.Task) -> None:
            emails_in_transition.discard(old_key)
            emails_in_transition.discard(new_key)

        task.add_done_callback(_release_on_real_completion)
    except Exception as e:
        logger.error(
            f"Не удалось запустить фоновую задачу переименования {old_email} -> "
            f"{new_email} на {host_name}: {e}",
            exc_info=True,
        )
        emails_in_transition.discard(old_key)
        emails_in_transition.discard(new_key)
        return False

    try:
        return await asyncio.wait_for(asyncio.shield(task), timeout=180)
    except asyncio.TimeoutError:
        logger.error(f"Таймаут при переименовании {old_email} -> {new_email} на хосте {host_name}")
        return False


# Реплицируем клиента последовательно на все размеченные инбаунды (по
# одному api.client.add()/update() на инбаунд, с паузой между вызовами —
# см. цикл в _create_or_update_key_on_host_sync). На медленном хосте, где
# каждый addClient/updateClient стоит панели рестарта xray-core, весь
# проход по нескольким десяткам инбаундов может растянуться на минуту и
# больше. Старый timeout=30 в этих условиях гарантированно истекал раньше,
# чем завершался поток — а поскольку asyncio.to_thread не отменяет сам
# поток, тот продолжал стучаться в панель уже без ведома вызывающего кода.
# Если пользователь/хендлер в ответ на TimeoutError/None инициировал
# повторную попытку — получали второй параллельный проход по тому же
# ключу на той же панели, что и объясняет каскады "Duplicate email" и
# "empty client ID" в логах репликации.


async def create_or_update_key_on_host(
    host_name: str,
    email: str,
    days_to_add: int | None = None,
    expiry_timestamp_ms: int | None = None,
    months_to_add: int | None = None,
) -> Dict | None:
    """
    Асинхронно создаёт или продлевает мастер-ключ (master account) в панели X-UI и синхронизирует его на все дополнительные инбаунды с меткой.

    Основная идея:
    • Один мастер-клиент (уникальный email и UUID)
    • Несколько «клонов» с одинаковым UUID, но разными email на разных инбаундах
    • Защита от дублирования email как на панели, так и в локальной БД

    months_to_add: количество календарных месяцев (приоритетнее days_to_add,
    если задано) — см. docstring update_or_create_client_on_panel.

    Возвращает: словарь с основными данными для сохранения в локальную БД
    или None в случае любой ошибки

    Вся блокирующая работа с панелью выполняется в отдельном потоке
    (_create_or_update_key_on_host_sync), event loop не блокируется.

    Защита от гонок — два уровня:
    • emails_in_transition + ранний выход, если этот же email уже
      обрабатывается прямо сейчас (повторный тап пользователя, ретрай
      вебхука платежа, повторный вызов после TimeoutError и т.п.) —
      раньше здесь просто безусловно ставился флаг, и повторный вызов
      с тем же email спокойно стартовал второй параллельный проход;
    • get_host_lock(host_name) — сериализует вообще любые мутирующие
      обращения к панели этого хоста, включая параллельную обработку
      других email (например, шедулер синхронизирует один ключ, а в
      этот же момент покупается другой) — именно одновременная нагрузка
      от разных клиентов на одну и ту же (часто небыструю) панель
      провоцирует пустые ответы и "empty client ID" у всех участников сразу.
    """
    # -------------------------------------------------------------------------
    # 1. Получение конфигурации хоста
    # -------------------------------------------------------------------------
    host_data = await asyncio.to_thread(get_host, host_name)
    if not host_data:
        logger.error(f"Сервер '{host_name}' не найден в конфигурации.")
        return None

    email_key = email.strip().lower()
    if email_key in emails_in_transition:
        logger.warning(
            f"create_or_update_key_on_host: {email_key} уже обрабатывается прямо "
            f"сейчас (повторный вызов) — пропуск во избежание параллельного прохода "
            f"репликации по тому же ключу."
        )
        return None

    emails_in_transition.add(email_key)
    host_lock = await get_host_lock(host_name)
    await host_lock.acquire()

    # -------------------------------------------------------------------------
    # Снятие host_lock/emails_in_transition вынесено в done-callback самой
    # задачи, а НЕ в finally этой функции. Раньше (async with host_lock: ...
    # finally: emails_in_transition.discard(...)) при срабатывании
    # asyncio.wait_for(timeout=30) лок и флаг снимались немедленно в момент
    # TimeoutError — а не когда реально завершался фоновый поток
    # (asyncio.to_thread не убивает поток при отмене обёртки, это лишь
    # оборачивающий Future). В результате: таймаут -> лок свободен, флаг
    # снят -> follow-up вызов (повтор вебхука, повторный тап юзера) стартует
    # второй, ничем не защищённый проход репликации по тому же хосту/email,
    # параллельно с первым, всё ещё работающим в фоне — и оба разом бьют по
    # панели, что и порождает Duplicate email/empty client ID каскадом.
    # Проверено эмпирически. Теперь: task оборачивается в asyncio.shield,
    # чтобы срабатывание wait_for не пыталось её отменить, а release
    # реально происходит один раз, ровно когда поток по-настоящему
    # закончил работу — неважно, дождался этого вызывающий код или нет.
    #
    # acquire() выше — без таймаута и вне try. Если между ним и регистрацией
    # done-callback что-то бросит исключение (например, ensure_future не
    # смог создать задачу) — лок останется висеть навсегда, а на самом
    # acquire() таймаута нет, значит хост залипнет целиком: все следующие
    # операции по нему встанут в очередь и будут ждать вечно, до рестарта
    # бота. Поэтому создание задачи и регистрация callback — под try/except
    # с явным release в except.
    # -------------------------------------------------------------------------
    try:
        task = asyncio.ensure_future(
            asyncio.to_thread(
                _create_or_update_key_on_host_sync,
                host_data,
                host_name,
                email,
                days_to_add,
                expiry_timestamp_ms,
                months_to_add,
            )
        )

        def _release_on_real_completion(_task: asyncio.Task) -> None:
            emails_in_transition.discard(email_key)
            if host_lock.locked():
                host_lock.release()

        task.add_done_callback(_release_on_real_completion)
    except Exception as e:
        logger.error(
            f"Не удалось запустить фоновую задачу репликации для {email} на "
            f"{host_name}: {e}",
            exc_info=True,
        )
        emails_in_transition.discard(email_key)
        host_lock.release()
        return None

    try:
        # Раньше 30с было рискованно вдвойне: и коротко для панели с
        # десятками помеченных инбаундов (рестарт xray-core на каждый
        # add/update, легко набегает больше 30с суммарно), и опасно, потому
        # что таймаут преждевременно освобождал host_lock/emails_in_transition
        # (см. комментарий выше). Второе теперь чинит asyncio.shield —
        # поток продолжает работать под собственной защитой независимо от
        # того, дождался ли его вызывающий код. Значит таймаут здесь — уже
        # не защита от гонки, а просто ограничение на то, сколько человек
        # (или вебхук) готов ждать ответа. Считать точное число инбаундов
        # заранее — это ещё один живой запрос к панели до основной работы,
        # того же рестарта xray, который мы и пытаемся пережить — поэтому
        # просто взят щедрый фиксированный потолок вместо точного расчёта.
        return await asyncio.wait_for(asyncio.shield(task), timeout=180)
    except asyncio.TimeoutError:
        logger.error(
            f"Таймаут при создании/обновлении ключа {email} на хосте {host_name}. "
            f"Фоновый поток продолжает работать в фоне — host_lock и "
            f"emails_in_transition снимутся автоматически ровно в момент его "
            f"реального завершения (см. add_done_callback), а не сейчас. Повторные "
            f"вызовы для этого email до этого момента будут честно отклоняться."
        )
        return None


def _get_key_details_from_host_sync(
    key_data: dict, host_name: str, host_db_data: dict
) -> dict | None:
    """
    Синхронная реализация get_key_details_from_host.

    Логин на панель и поиск
    клиента — блокирующие вызовы, выполняются целиком в отдельном потоке.
    """
    # -------------------------------------------------------------------------
    # 3. Авторизация на панели
    #
    #    Оптимизация нагрузки на панель: эта функция вызывается очень часто —
    #    буквально при каждом открытии пользователем меню своего ключа. Раньше
    #    здесь всегда выполнялся login_to_host, который тянет полный список
    #    инбаундов со всеми клиентами и статистикой трафика — просто чтобы
    #    найти sub_id одного клиента. Теперь сначала пробуем точечный запрос
    #    api.client.get_by_email (панель отдаёт данные только одного клиента,
    #    без остального списка) — и только если это не сработало (например,
    #    email в БД не совпадает с email на панели), уходим в старый тяжёлый
    #    путь через полный список инбаундов как запасной вариант.
    # -------------------------------------------------------------------------
    api = _login_only(
        host_url=host_db_data["host_url"],
        username=host_db_data["host_username"],
        password=host_db_data["host_pass"],
    )

    if not api:
        logger.error(f"Не удалось авторизоваться на хосте '{host_name}'")
        return None

    client_sub_token = None
    uuid_to_find = key_data.get("xui_client_uuid")
    email_to_find = key_data.get("email")

    # -------------------------------------------------------------------------
    # 4. Быстрый путь: точечный запрос клиента по email
    # -------------------------------------------------------------------------
    if email_to_find:
        try:
            client = api.client.get_by_email(email_to_find)
            if client is not None:
                val = getattr(client, "sub_id", None)
                if val:
                    client_sub_token = str(val)
        except Exception as e:
            logger.debug(
                f"Быстрый поиск клиента по email не удался на хосте {host_name}: {e} "
                f"— переходим на запасной вариант через полный список инбаундов"
            )

    # -------------------------------------------------------------------------
    # 5. Запасной (тяжёлый) путь: полный список инбаундов, если быстрый не сработал
    # -------------------------------------------------------------------------
    if client_sub_token is None:
        try:
            inbounds: List[Inbound] = api.inbound.get_list()
            target_inbound = next(
                (ib for ib in inbounds if ib.id == host_db_data["host_inbound_id"]),
                None,
            )

            if target_inbound and target_inbound.settings and target_inbound.settings.clients:
                for client in target_inbound.settings.clients:
                    client_id = getattr(client, "id", None)
                    client_auth = getattr(client, "auth", None)
                    client_email = getattr(client, "email", None)

                    # Совпадение по UUID (приоритет) или по email (fallback)
                    if (
                        uuid_to_find and (client_id == uuid_to_find or client_auth == uuid_to_find)
                    ) or (email_to_find and client_email == email_to_find):

                        val = None
                        if hasattr(client, "sub_id"):
                            val = getattr(client, "sub_id")
                        elif isinstance(client, dict):
                            val = client.get("sub_id")

                        if val:
                            client_sub_token = str(val)
                        break

        except Exception as e:
            logger.warning(
                f"Ошибка при поиске токена подписки клиента на хосте {host_name}: {e}",
                exc_info=True,
            )
            # Продолжаем — токен не найден, но ссылку всё равно сформируем

    # -------------------------------------------------------------------------
    # 5. Формирование актуальной ссылки подписки
    # -------------------------------------------------------------------------
    connection_string = get_subscription_link(
        user_uuid=key_data["xui_client_uuid"],
        host_url=host_db_data["host_url"],
        host_name=host_name,
        sub_token=client_sub_token,
    )

    # -------------------------------------------------------------------------
    # 6. Возврат результата
    # -------------------------------------------------------------------------
    if not connection_string:
        logger.error(f"Не удалось сформировать ссылку подписки для ключа на хосте {host_name}")
        return None

    return {"connection_string": connection_string}


async def get_key_details_from_host(key_data: dict) -> dict | None:
    """
    Получает актуальные данные ключа с хоста, в первую очередь — свежую ссылку подписки.

    Основная цель функции:
    • Авторизоваться на панели хоста
    • Найти клиента по UUID (или email как fallback)
    • Извлечь актуальный токен подписки (subId), если он есть
    • Сформировать правильную ссылку подписки с учётом актуального токена

    Аргументы:
        key_data: Словарь с данными ключа из локальной БД. Ожидаемые ключи:
                  • host_name         (обязательно)
                  • xui_client_uuid   (обязательно для точного поиска)
                  • email             (fallback для поиска)
                  • key_id            (только для логирования)

    Возвращает:
        dict | None:
            {"connection_string": "..."}   — при успехе
            None                           — при любой критической ошибке:
                                               • нет host_name
                                               • сервер не найден
                                               • не удалось авторизоваться
                                               • inbound не найден
                                               • критическая ошибка API

    Примечания:
    • Функция не обновляет локальную БД — только возвращает актуальную ссылку
    • Токен подписки ищется по subId
    • Если токен не найден — ссылка формируется по UUID (fallback-режим)
    • Логин и поиск клиента на панели выполняются в отдельном потоке (asyncio.to_thread)
    """
    # -------------------------------------------------------------------------
    # 1. Проверка наличия обязательного поля host_name
    # -------------------------------------------------------------------------
    host_name = key_data.get("host_name")
    if not host_name:
        logger.error(
            f"Не удалось получить детали ключа: отсутствует 'host_name' "
            f"для key_id={key_data.get('key_id', '—')}"
        )
        return None

    # -------------------------------------------------------------------------
    # 2. Получение конфигурации хоста из локальной базы/конфига
    # -------------------------------------------------------------------------
    host_db_data = await asyncio.to_thread(get_host, host_name)
    if not host_db_data:
        logger.error(f"Сервер '{host_name}' не найден в конфигурации/базе данных")
        return None

    try:
        return await asyncio.wait_for(
            asyncio.to_thread(_get_key_details_from_host_sync, key_data, host_name, host_db_data),
            timeout=30,
        )
    except asyncio.TimeoutError:
        logger.error(f"Таймаут при получении деталей ключа на хосте {host_name}")
        return None


def _delete_client_on_host_sync(host_data: dict, host_name: str, client_email: str) -> bool:
    """
    Синхронная реализация delete_client_on_host.

    Логин и весь цикл удаления
    клиента/клонов на панели выполняются целиком в отдельном потоке.
    """
    # -------------------------------------------------------------------------
    # 2. Авторизация в панели X-UI
    #    (используем облегчённый логин — целевой inbound здесь не нужен,
    #    ниже всё равно запрашивается полный список инбаундов один раз)
    # -------------------------------------------------------------------------
    api = _login_only(
        host_url=host_data["host_url"],
        username=host_data["host_username"],
        password=host_data["host_pass"],
    )
    if not api:
        logger.error(f"Не удалось авторизоваться на хосте {host_name} для удаления {client_email}")
        return False

    try:
        # -------------------------------------------------------------------------
        # 3. Получение UUID клиента из локальной базы данных
        # -------------------------------------------------------------------------
        client_in_db = get_key_by_email(client_email)
        client_uuid = None

        if client_in_db:
            client_uuid = client_in_db.get("xui_client_uuid")

        if not client_uuid:
            logger.warning(
                f"UUID клиента для email {client_email} не найден в БД. "
                f"Удаление будет выполнено только по email (могут остаться клоны с другим email)."
            )

        # Выделяем чистый префикс мастера для гарантированного поиска клонов по email
        prefix = client_email.split("@")[0]

        # -------------------------------------------------------------------------
        # 4. Получаем список всех инбаундов на панели
        # -------------------------------------------------------------------------
        all_inbounds = api.inbound.get_list()
        deleted_count = 0

        # -------------------------------------------------------------------------
        # 5. Проходим по всем инбаундам и удаляем подходящие клиенты
        # -------------------------------------------------------------------------
        for inbound in all_inbounds:
            # Обрабатываем только инбаунды с меткой
            remark = getattr(inbound, "remark", "")
            if "🌍" not in remark and "🏳️" not in remark:
                continue

            clients = inbound.settings.clients or []
            if not clients:
                continue

            # Вместо next() собираем всех подходящих клиентов (мастера и его клонов).
            # c.auth здесь не матчим — py3xui.Client это поле не парсит (см.
            # docstring fetch_raw_client_auth_map), getattr всегда возвращает ""
            # и сравнение с client_uuid никогда не сработает — было мёртвым кодом.
            targets_to_delete = [
                c
                for c in clients
                if (client_uuid and str(getattr(c, "id", "")) == str(client_uuid))
                or (getattr(c, "email", "") == client_email)
                or (getattr(c, "email", "").startswith(f"{prefix}-"))
            ]

            # Если на текущем инбаунде совпадений нет — идем дальше
            if not targets_to_delete:
                continue

            is_hysteria_ib = is_hysteria_protocol(getattr(inbound, "protocol", ""))

            # Удаление переиспользует delete_client_robust — единую, уже
            # протокол-осознанную логику (auth из сырого JSON для Hysteria/
            # Hysteria2, id панели для остальных протоколов), вместо
            # отдельно живущей здесь копии с теми же историческими, которую
            # легко было забыть исправить синхронно при следующем патче —
            # что, собственно, и произошло до этого момента.
            for target_client in targets_to_delete:
                deleted_ok = delete_client_robust(
                    api, inbound.id, target_client, client_uuid, is_hysteria_ib
                )
                if deleted_ok:
                    deleted_count += 1
                    logger.debug(
                        f"Удалён клиент с inbound {inbound.id} "
                        f"(email: {getattr(target_client, 'email', client_email)})"
                    )
                    time.sleep(0.2)
                else:
                    logger.warning(
                        f"API не смог удалить клиента {getattr(target_client, 'email', client_email)} "
                        f"(или он уже отсутствует) с inbound {inbound.id}"
                    )

        # -------------------------------------------------------------------------
        # 6. Логирование результата и возврат статуса
        # -------------------------------------------------------------------------
        if deleted_count > 0:
            logger.info(
                f"Удаление клиента {client_email} завершено успешно. "
                f"Очищено инбаундов: {deleted_count}"
            )
        elif client_uuid:
            # В БД был валидный UUID для поиска, но на панели по нему (и по
            # email/префиксу клонов) не нашлось вообще ничего — это не
            # "нечего было удалять", это подозрительный признак рассинхрона
            # БД↔панель (тот же класс проблемы, что чинили в scheduler.py
            # для одного из ключей — просто с другой стороны: там БД отставала от
            # панели, здесь БД указывает на то, чего на панели уже нет).
            # Функция всё равно возвращает True ниже (сохраняем обратную
            # совместимость по типу — вызывающий код везде ждёт bool), но
            # уровень лога здесь warning, а не info, чтобы это было видно.
            logger.warning(
                f"Клиент {client_email} (UUID {client_uuid}) не найден ни на одном "
                f"инбаунде — в БД есть, на панели нет. Возможен рассинхрон, стоит "
                f"проверить эту запись вручную, а не считать удаление тривиальным no-op."
            )
        else:
            logger.info(
                f"Клиент {client_email} не найден ни на одном инбаунде "
                f"(или уже был удалён ранее). UUID: {client_uuid or '—'}"
            )

        return True

    except Exception as e:
        logger.error(
            f"Критическая ошибка при удалении клиента {client_email} на хосте {host_name}: {e}",
            exc_info=True,
        )
        return False


async def delete_client_on_host(host_name: str, client_email: str) -> bool:
    """
    Полностью удаляет клиента (мастер + все клоны) с указанного хоста в панели X-UI.

    Особенности реализации:
    • Основной критерий поиска — UUID клиента (самый надёжный способ)
    • Дополнительно поддерживается поиск по email (на случай, если UUID в БД отсутствует)
    • Удаление выполняется только на инбаундах с меткой в remark
    • Возвращает True только при успешном выполнении (даже если ничего не удалено — но без ошибок)

    Аргументы:
        host_name:     Имя хоста (ключ в конфигурации)
        client_email:  Email мастера (тот, который хранится в локальной БД)

    Возвращает:
        bool: Успешно ли завершена операция удаления (без критических ошибок API)

    Весь цикл поиска/удаления на панели выполняется в отдельном потоке.

    Защита от гонок — та же пара, что и в create_or_update_key_on_host:
    • emails_in_transition — не даёт удалению стартовать параллельно с
      созданием/продлением этого же ключа (например, юзер жмёт "продлить"
      в тот момент, когда истёк срок и включилась авто-чистка — раньше
      ничего не мешало обеим операциям одновременно писать в одни и те же
      инбаунды на панели);
    • get_host_lock(host_name) — не даёт удалению одного ключа идти
      параллельно с созданием/сверкой другого ключа на том же хосте, ровно
      по тем же причинам, что и в create_or_update_key_on_host.
    """
    # -------------------------------------------------------------------------
    # 1. Получение данных хоста из конфигурации
    # -------------------------------------------------------------------------
    host_data = await asyncio.to_thread(get_host, host_name)
    if not host_data:
        logger.error(f"Удаление невозможно: сервер '{host_name}' не найден в конфигурации.")
        return False

    email_key = client_email.strip().lower()
    if email_key in emails_in_transition:
        logger.warning(
            f"delete_client_on_host: {email_key} уже обрабатывается прямо сейчас "
            f"(создание/продление) — пропуск удаления во избежание гонки. Повторите позже."
        )
        return False

    emails_in_transition.add(email_key)
    host_lock = await get_host_lock(host_name)
    await host_lock.acquire()

    # См. подробный комментарий в create_or_update_key_on_host — та же самая
    # проблема и то же самое решение: снятие host_lock/emails_in_transition
    # привязано к реальному завершению фонового потока через
    # add_done_callback, а не к тому, дождался ли его вызывающий код.
    #
    # Как и там: acquire() без таймаута, вне try — если между ним и
    # регистрацией callback что-то бросит исключение, лок повиснет навсегда
    # и хост залипнет до рестарта бота. Поэтому под try/except с release в
    # except.
    try:
        task = asyncio.ensure_future(
            asyncio.to_thread(_delete_client_on_host_sync, host_data, host_name, client_email)
        )

        def _release_on_real_completion(_task: asyncio.Task) -> None:
            emails_in_transition.discard(email_key)
            if host_lock.locked():
                host_lock.release()

        task.add_done_callback(_release_on_real_completion)
    except Exception as e:
        logger.error(
            f"Не удалось запустить фоновую задачу удаления для {client_email} на "
            f"{host_name}: {e}",
            exc_info=True,
        )
        emails_in_transition.discard(email_key)
        host_lock.release()
        return False

    try:
        return await asyncio.wait_for(asyncio.shield(task), timeout=180)
    except asyncio.TimeoutError:
        logger.error(
            f"Таймаут при удалении клиента {client_email} на хосте {host_name}. "
            f"Фоновый поток продолжает работать в фоне — host_lock и "
            f"emails_in_transition снимутся автоматически ровно в момент его "
            f"реального завершения."
        )
        return False


def get_realtime_online_count(host_data: dict) -> int:
    """
    Выполняет быструю проверку количества онлайн-клиентов на хосте.

    Аргументы:
        host_data: Словарь с данными хоста (url, username, pass, и т.д.)

    Возвращает:
        int: Количество уникальных email-ов в онлайне.

    Примечание: функция обычная синхронная (не async) — так её уже вызывают
    (см. admin_handlers.py: asyncio.to_thread(get_realtime_online_count, h))
    """
    try:
        # -------------------------------------------------------------------------
        # 1. Авторизация на панели хоста — облегчённая (без get_list()):
        #    api.client.online() не нуждается в списке инбаундов вообще,
        #    login_to_host тянул его впустую при каждом опросе онлайна
        # -------------------------------------------------------------------------
        api = _login_only(
            host_url=host_data["host_url"],
            username=host_data["host_username"],
            password=host_data["host_pass"],
        )

        if not api:
            return 0

        # -------------------------------------------------------------------------
        # 2. Получение списка онлайн-клиентов
        # -------------------------------------------------------------------------
        online_list = api.client.online() or []
        return sum(1 for email in online_list if str(email).lower().endswith(f"@{EMAIL_DOMAIN}"))

    except Exception as e:
        logger.error(f"Ошибка сбора онлайна на {host_data.get('host_name')}: {e}")
        return 0


def delete_client_robust(
    api,
    inbound_id: int,
    client_obj,
    extra_uuid: str | None = None,
    is_hysteria: bool = False,
) -> bool:
    """Удаляет одного клиента на конкретном inbound — без "воздуха".

    Раньше здесь было 3-4 последовательных фолбэка (id/auth -> email ->
    UUID из БД -> сырой auth), но реально из них рабочих было гораздо
    меньше, чем казалось:

    • client_obj.auth — py3xui.Client вообще не объявляет поле 'auth' в
      модели, getattr(..., 'auth', None) для любого клиента любого
      протокола всегда возвращает None. Это была не попытка, а мёртвый код.
    • Попытка "по email" — панель ищет клиента строго через
      DelInboundClient(inboundId, clientId), который сравнивает clientId с
      полем Id (для VLESS/VMess/Trojan/SS) либо Auth (для Hysteria/
      Hysteria2) — см. web/service/inbound.go, case "hysteria","hysteria2".
      Отдельная ручка DelInboundClientByEmail в 3x-ui существует, но
      py3xui.client.delete() её не вызывает и не эмулирует — значит подстановка
      email в этот эндпоинт никогда ни с чем не совпадёт, что и видно в логах
      как "Client Not Found In Inbound For ID: <email>". Убрано полностью.

    Оставлены только два метода, у каждого из которых есть структурная
    причина реально сработать:

    1. Hysteria/Hysteria2 → сырой auth из JSON панели (fetch_raw_client_auth_map).
       py3xui.Client это поле не парсит вообще, поэтому getattr(client_obj,
       'id', None) для hysteria-клиента — недостоверное значение (может
       случайно совпасть с auth, если клиент создан уже после
       HysteriaClientWrapper, а может и не совпасть для более старых
       записей) — единственный источник истины здесь сырой JSON.
    2. VLESS/VMess/Trojan/Shadowsocks → Id клиента, как его вернул
       api.inbound.get_list()/get_by_id() — для этих протоколов py3xui эту
       модель парсит корректно, панель матчит по этому же полю. extra_uuid
       (наш собственный xui_client_uuid из БД) используется только как
       null-safe запасное значение, если по какой-то причине client_obj.id
       не распарсился — для наших собственных клиентов id всегда создаётся
       равным этому UUID (мы сами так и создаём: Client(id=client_uuid,...)),
       так что это не гадание про чужого клиента, а то же самое значение.

    Если оба метода не сработали с сообщением "record not found" — это,
    предположительно, известный баг 3x-ui (github.com/MHSanaei/3x-ui/issues/4026):
    у клиента пропадает связанная запись в client_traffics, и после этого
    панель на любой идентификатор отвечает "record not found" навсегда. Это
    честно логируется, но никакого автоматического обхода (SSH/правка БД)
    нет — на инфраструктуре без root/SSH-доступа это всё равно
    неприменимо. Такие записи остаются в панели как известные, неубираемые
    через API мусорные клиенты — разбираются вручную, отдельно от бота.
    """
    email_lower = str(getattr(client_obj, "email", "") or "").strip().lower()

    if is_hysteria:
        identifier = (
            fetch_raw_client_auth_map(api, inbound_id).get(email_lower) if email_lower else None
        )
        method_label = "auth (сырой JSON панели)"
    else:
        identifier = getattr(client_obj, "id", None) or extra_uuid
        method_label = "id клиента"

    if not identifier:
        logger.warning(
            f"delete_client_robust: не удалось определить "
            f"{'auth' if is_hysteria else 'id'} для удаления "
            f"(inbound={inbound_id}, email={email_lower or '?'})"
        )
        return False

    try:
        call_with_retry(api.client.delete, inbound_id, str(identifier))
        return True
    except Exception as e:
        err_text = str(e)
        logger.warning(
            f"delete_client_robust: удаление не удалось "
            f"(inbound={inbound_id}, email={email_lower or '?'}, "
            f"{method_label}='{identifier}'): {e}"
        )
        if email_lower and "record not found" in err_text.lower():
            logger.warning(
                f"delete_client_robust: {email_lower} похоже на известный баг 3X-UI "
                f"(client_traffics потерялся, панель на любой идентификатор отвечает "
                f"record not found) — автоматического обхода нет, нужна ручная разборка."
            )
        return False
