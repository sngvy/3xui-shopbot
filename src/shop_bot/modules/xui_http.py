"""Keep-alive для py3xui: общий пул HTTP-соединений вместо нового TCP+TLS на каждый запрос.

py3xui 0.4.0 вызывает голые requests.get/requests.post (без Session), поэтому каждый
запрос к панели — это новый TCP- и TLS-handshake. Здесь модульная ссылка `requests`
внутри py3xui.api.api_base подменяется тонким шимом поверх одной requests.Session
с пулом соединений. Остальной код (в т.ч. сам py3xui) не меняется.

Важные детали:
• Cookie-jar сессии отключён (политика без разрешённых доменов). py3xui сам передаёт
  cookie авторизации в каждом запросе (cookies=...), а общий jar смешал бы cookie
  разных панелей/аккаунтов.
• Повторяются только ошибки установления соединения (connect=2) — запрос в этом
  случае ещё не отправлен, так что повтор POST не может задвоить addClient.
• Таймауты: раньше их не было вообще (спасал только socket.setdefaulttimeout(20)
  из __main__). Теперь connect/read задаются явно; переопределяются через
  XUI_HTTP_CONNECT_TIMEOUT / XUI_HTTP_READ_TIMEOUT. Значения по умолчанию
  не длиннее прежних 20 с, поведение на медленных панелях не меняется.
"""

import logging
import os
from http.cookiejar import DefaultCookiePolicy

import requests
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

logger = logging.getLogger(__name__)

_installed = False


def _float_env(name: str, default: float) -> float:
    try:
        return float(os.environ.get(name, default))
    except (TypeError, ValueError):
        return default


def install() -> None:
    """Подменяет requests в py3xui на пул-версию. Идемпотентно."""
    global _installed
    if _installed:
        return

    import py3xui.api.api_base as api_base

    connect_t = _float_env("XUI_HTTP_CONNECT_TIMEOUT", 10.0)
    read_t = _float_env("XUI_HTTP_READ_TIMEOUT", 20.0)
    pool_size = int(_float_env("XUI_HTTP_POOL_SIZE", 16))

    session = requests.Session()
    session.cookies.set_policy(DefaultCookiePolicy(allowed_domains=[]))
    retry = Retry(
        total=None,
        connect=2,
        read=0,
        status=0,
        other=0,
        redirect=0,
        backoff_factor=0.3,
        allowed_methods=None,
    )
    adapter = HTTPAdapter(pool_connections=pool_size, pool_maxsize=pool_size, max_retries=retry)
    session.mount("https://", adapter)
    session.mount("http://", adapter)

    class _PooledRequests:
        """Шим: get/post идут через общую Session, всё остальное — как в requests."""

        def __getattr__(self, name):
            return getattr(requests, name)

        @staticmethod
        def get(url, **kwargs):
            kwargs.setdefault("timeout", (connect_t, read_t))
            return session.get(url, **kwargs)

        @staticmethod
        def post(url, **kwargs):
            kwargs.setdefault("timeout", (connect_t, read_t))
            return session.post(url, **kwargs)

    api_base.requests = _PooledRequests()
    _installed = True
    logger.info(
        f"xui_http: keep-alive включён (pool={pool_size}, connect={connect_t}s, read={read_t}s)"
    )
