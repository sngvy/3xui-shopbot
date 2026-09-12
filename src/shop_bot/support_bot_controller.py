"""Управление жизненным циклом бота поддержки (запуск/остановка)."""

import asyncio
import logging
import os

from aiogram import Bot, Dispatcher
from aiogram.client.default import DefaultBotProperties
from aiogram.client.session.aiohttp import AiohttpSession
from aiogram.enums import ParseMode

from shop_bot.bot.middlewares import BanMiddleware
from shop_bot.data_manager import database
from shop_bot.data_manager.database import get_admin_ids
from shop_bot.support_bot.handlers import get_support_router

logger = logging.getLogger(__name__)


class SupportBotController:
    """Управляет жизненным циклом бота поддержки (синглтон): запуск, остановка, статус."""

    _instance = None
    _initialized = False

    def __new__(cls, *args, **kwargs):
        """Гарантирует единственный экземпляр контроллера (паттерн Singleton)."""
        if cls._instance is None:
            cls._instance = super().__new__(cls)
        return cls._instance

    def __init__(self):
        """Инициализирует синглтон-контроллер один раз (повторные вызовы — no-op)."""
        if self._initialized:
            return
        self._dp: Dispatcher | None = None
        self._bot: Bot | None = None
        self._task = None
        self._is_running = False
        self._loop: asyncio.AbstractEventLoop | None = None
        self._initialized = True

    def set_loop(self, loop: asyncio.AbstractEventLoop):
        """Сохраняет ссылку на event loop, в котором будет работать support-бот."""
        self._loop = loop
        logger.info("Цикл событий установлен.")

    def get_bot_instance(self) -> Bot | None:
        """Возвращает текущий экземпляр support-бота или None, если он не запущен."""
        return self._bot

    async def _start_polling(self):
        self._is_running = True
        logger.info("Запущен опрос Telegram (Support-бот)...")
        while self._is_running:
            try:
                await self._dp.start_polling(self._bot)
            except asyncio.CancelledError:
                logger.info("Опрос остановлен (задача отменена).")
                break
            except Exception as e:
                logger.error(f"Ошибка во время опроса: {e}", exc_info=True)
                await asyncio.sleep(1)
            finally:
                if not self._is_running:
                    break

        logger.info("Опрос корректно остановлен.")
        self._is_running = False
        self._task = None
        if self._bot:
            await self._bot.close()
        self._bot = None
        self._dp = None

    def start(self):
        """Запускает support-бота в уже установленном event loop. Возвращает словарь со статусом."""
        if self._is_running:
            return {"status": "error", "message": "Support-бот уже запущен."}

        if not self._loop or not self._loop.is_running():
            return {
                "status": "error",
                "message": "Критическая ошибка: цикл событий не установлен.",
            }

        token = database.get_setting("support_bot_token")
        bot_username = database.get_setting("support_bot_username")
        # Допускаем отсутствие одиночного admin_telegram_id, если настроены admin_telegram_ids
        admin_id = database.get_setting("admin_telegram_id")
        admin_ids = get_admin_ids()

        if not all([token, bot_username]) or (not admin_id and not admin_ids):
            return {
                "status": "error",
                "message": "Невозможно запустить support-бот: заполните support_bot_token, support_bot_username и хотя бы одного администратора (admin_telegram_id или admin_telegram_ids).",
            }

        try:
            proxy_url = os.getenv("PROXY_URL")
            session = AiohttpSession(proxy=proxy_url) if proxy_url else None
            if proxy_url:
                logger.info(f"Используется прокси из .env: {proxy_url}")
            else:
                logger.info("Прокси не задан, запуск напрямую.")

            self._bot = Bot(
                token=token,
                session=session,
                default=DefaultBotProperties(parse_mode=ParseMode.HTML),
            )
            self._dp = Dispatcher()

            # Подключаем BanMiddleware, чтобы заблокированные пользователи не писали в поддержку
            self._dp.message.middleware(BanMiddleware())
            self._dp.callback_query.middleware(BanMiddleware())

            router = get_support_router()
            self._dp.include_router(router)

            try:
                asyncio.run_coroutine_threadsafe(
                    self._bot.delete_webhook(drop_pending_updates=True), self._loop
                )
            except Exception as e:
                logger.warning(f"Не удалось удалить вебхук перед запуском опроса: {e}")

            self._task = asyncio.run_coroutine_threadsafe(self._start_polling(), self._loop)
            logger.info("Команда на запуск передана в цикл событий.")
            return {
                "status": "success",
                "message": "Команда на запуск support-бота отправлена.",
            }
        except Exception as e:
            logger.error(f"Ошибка запуска support-бота: {e}", exc_info=True)
            self._bot = None
            self._dp = None
            return {
                "status": "error",
                "message": f"Ошибка при запуске support-бота: {e}",
            }

    def stop(self):
        """Останавливает работающего support-бота. Возвращает словарь со статусом."""
        if not self._is_running:
            return {"status": "error", "message": "Support-бот не запущен."}

        if not self._loop or not self._dp:
            return {
                "status": "error",
                "message": "Критическая ошибка: компоненты бота недоступны.",
            }

        logger.info("Отправляю сигнал на корректную остановку...")
        asyncio.run_coroutine_threadsafe(self._dp.stop_polling(), self._loop)
        return {
            "status": "success",
            "message": "Команда на остановку support-бота отправлена.",
        }

    def get_status(self):
        """Возвращает текущий статус support-бота (запущен/остановлен)."""
        return {"is_running": self._is_running}

    async def run_auto_start(self):
        """Прямой запуск support-бота без возврата словарей."""
        logger.info("Попытка автозапуска support-бота...")
        result = self.start()
        if result["status"] == "error":
            logger.error(f"Автозапуск support-бота не удался: {result['message']}")
        else:
            logger.info("Автозапуск support-бота: команда успешно передана в цикл событий.")
