"""Мониторинг ресурсов серверов (CPU, RAM, диск, аптайм) локально и по SSH на удалённых хостах."""

import asyncio
import logging
import os
import shutil
import time
from datetime import datetime
from typing import Any, Dict, List, Tuple

import paramiko

from shop_bot.data_manager import database

logger = logging.getLogger(__name__)

try:
    import psutil  # type: ignore

    HAS_PSUTIL = True
except ImportError:
    HAS_PSUTIL = False


# -------- Local metrics (container/host running the panel) --------
def _read_proc_meminfo() -> Tuple[int | None, int | None]:
    total_kb = None
    avail_kb = None
    try:
        with open("/proc/meminfo", "r") as f:
            for line in f:
                if line.startswith("MemTotal:"):
                    parts = line.split()
                    total_kb = int(parts[1])
                elif line.startswith("MemAvailable:"):
                    parts = line.split()
                    avail_kb = int(parts[1])
                if total_kb is not None and avail_kb is not None:
                    break
    except Exception:
        pass
    return total_kb, avail_kb


def _get_uptime_seconds_fallback() -> float | None:
    try:
        with open("/proc/uptime", "r") as f:
            txt = f.read().strip().split()
            return float(txt[0])
    except Exception:
        return None


def get_local_metrics() -> Dict[str, Any]:
    """Собирает метрики (CPU, RAM, диск, аптайм) локальной машины, на которой работает бот."""
    out: Dict[str, Any] = {
        "ok": True,
        "source": "local",
        "cpu_percent": None,
        "cpu_count": os.cpu_count() or 1,
        "loadavg": None,  # (1m, 5m, 15m)
        "mem_total": None,
        "mem_used": None,
        "mem_available": None,
        "mem_percent": None,
        "disk_total": None,
        "disk_used": None,
        "disk_free": None,
        "disk_percent": None,
        "uptime_seconds": None,
        "network_sent": None,
        "network_recv": None,
        "network_packets_sent": None,
        "network_packets_recv": None,
        "error": None,
    }

    # Load average
    try:
        if hasattr(os, "getloadavg"):
            la = os.getloadavg()
            out["loadavg"] = {"1m": la[0], "5m": la[1], "15m": la[2]}
    except Exception:
        pass

    # Disk usage (root)
    try:
        disk_path = "/"
        if os.name == "nt":
            disk_path = os.environ.get("SystemDrive", "C:") + "\\"
        du = shutil.disk_usage(disk_path)
        out["disk_total"] = int(du.total)
        out["disk_used"] = int(du.used)
        out["disk_free"] = int(du.free)
        out["disk_percent"] = round((du.used / du.total) * 100.0, 2) if du.total else None
    except Exception:
        pass

    # Метрики через psutil, если он доступен
    if HAS_PSUTIL:
        try:
            # Memory and CPU
            out["cpu_percent"] = float(psutil.cpu_percent(interval=0.1))
            vm = psutil.virtual_memory()
            out["mem_total"] = int(vm.total)
            out["mem_used"] = int(vm.used)
            out["mem_available"] = int(vm.available)
            out["mem_percent"] = float(vm.percent)

            # Network data
            try:
                net_io = psutil.net_io_counters()
                out["network_sent"] = int(net_io.bytes_sent)
                out["network_recv"] = int(net_io.bytes_recv)
                out["network_packets_sent"] = int(net_io.packets_sent)
                out["network_packets_recv"] = int(net_io.packets_recv)
            except Exception:
                pass

            try:
                out["uptime_seconds"] = float(time.time() - psutil.boot_time())
            except Exception:
                out["uptime_seconds"] = _get_uptime_seconds_fallback()

            # Если сбор через psutil завершился успешно
            return out

        except Exception:
            # Если psutil установлен, но почему-то упал при чтении (например, нет прав доступа),
            # мы просто игнорируем ошибку и проваливаемся в fallback ниже
            pass

    # --- Запасной вариант без использования psutil ---
    total_kb, avail_kb = _read_proc_meminfo()
    if total_kb is not None and avail_kb is not None:
        total = total_kb * 1024
        avail = avail_kb * 1024
        used = total - avail
        out["mem_total"] = total
        out["mem_available"] = avail
        out["mem_used"] = used
        out["mem_percent"] = round((used / total) * 100.0, 2) if total else None

    out["cpu_percent"] = None
    out["uptime_seconds"] = _get_uptime_seconds_fallback()

    if out["mem_total"] is None:
        out["ok"] = False
        out["error"] = "psutil not installed and /proc parsing failed"

    return out


# -------- Remote host metrics (via SSH) --------
def _ssh_connect(host_row: dict, timeout: int = 30) -> paramiko.SSHClient:
    ssh_host = (host_row.get("ssh_host") or "").strip()
    ssh_port = int(host_row.get("ssh_port") or 22)
    ssh_user = (host_row.get("ssh_user") or "").strip()
    ssh_password = host_row.get("ssh_password")
    ssh_key_path = (host_row.get("ssh_key_path") or "").strip() or None

    if not ssh_host or not ssh_user:
        raise RuntimeError("SSH settings are not configured for host")

    ssh = paramiko.SSHClient()
    ssh.set_missing_host_key_policy(paramiko.AutoAddPolicy())
    pkey = None
    if ssh_key_path:
        for KeyClass in (paramiko.RSAKey, paramiko.Ed25519Key):
            try:
                pkey = KeyClass.from_private_key_file(ssh_key_path)
                break
            except Exception:
                pkey = None

    # Задаем жесткие рамки на все этапы подключения к серверу
    ssh.connect(
        ssh_host,
        port=ssh_port,
        username=ssh_user,
        password=ssh_password,
        pkey=pkey,
        timeout=timeout,  # Время на открытие сетевого сокета
        auth_timeout=timeout,  # Время на проверку пароля/ключа (защита от зависания!)
        banner_timeout=timeout,  # Время на ожидание SSH-приветствия от сервера
    )
    return ssh


def _ssh_exec(ssh: paramiko.SSHClient, cmd: str, timeout: int = 30) -> Tuple[int, str, str]:
    # -------------------------------------------------------------------------
    # 1. Запускаем команду с таймаутом на сокет
    # -------------------------------------------------------------------------
    stdin, stdout, stderr = ssh.exec_command(cmd, timeout=timeout)

    # -------------------------------------------------------------------------
    # 2. Явно задаем таймаут на сам канал (channel) для защиты от зависаний
    # -------------------------------------------------------------------------
    if hasattr(stdout, "channel"):
        stdout.channel.settimeout(float(timeout))

    try:
        # Читаем потоки данных
        out = stdout.read().decode("utf-8", errors="ignore")
        err = stderr.read().decode("utf-8", errors="ignore")

        # Получаем код ответа
        rc = stdout.channel.recv_exit_status() if hasattr(stdout, "channel") else 0
    except Exception as e:
        # Если сработал таймаут канала, возвращаем ошибку
        rc = -1
        out = ""
        err = f"SSH execution timed out or failed after {timeout} seconds: {e}"

    return rc, out, err


def get_host_metrics_via_ssh(host_row: dict) -> Dict[str, Any]:
    """Собирает метрики (CPU, RAM, диск, аптайм) удалённого хоста по SSH."""
    res: Dict[str, Any] = {
        "ok": False,
        "host_name": host_row.get("host_name"),
        "cpu_percent": None,
        "cpu_count": None,
        "loadavg": None,
        "mem_total": None,
        "mem_used": None,
        "mem_available": None,
        "mem_percent": None,
        "disk_total": None,
        "disk_used": None,
        "disk_free": None,
        "disk_percent": None,
        "uptime_seconds": None,
        "error": None,
    }

    # -------------------------------------------------------------------------
    # 1. Попытка установить SSH-соединение с хостом
    # -------------------------------------------------------------------------
    try:
        ssh = _ssh_connect(host_row, timeout=30)
    except Exception as e:
        res["error"] = f"SSH connect failed: {e}"
        return res

    try:
        # -------------------------------------------------------------------------
        # 2. Сбор информации о количестве ядер процессора (CPU count)
        # -------------------------------------------------------------------------
        cpu_count = None
        rc, out, _ = _ssh_exec(ssh, "nproc", timeout=30)
        if rc == 0 and out and out.strip():
            try:
                cpu_count = int(out.strip().splitlines()[0])
            except Exception:
                pass

        if cpu_count is None:
            rc, out, _ = _ssh_exec(ssh, "getconf _NPROCESSORS_ONLN", timeout=30)
            if rc == 0 and out and out.strip():
                try:
                    cpu_count = int(out.strip().splitlines()[0])
                except Exception:
                    pass

        res["cpu_count"] = cpu_count if cpu_count is not None else 1

        # -------------------------------------------------------------------------
        # 3. Получение средней нагрузки (loadavg) и расчет честной утилизации CPU
        # -------------------------------------------------------------------------

        # 3.1. Собираем loadavg (метрика полезна для оценки общей очереди задач)
        la = None
        rc, out, _ = _ssh_exec(ssh, "cat /proc/loadavg", timeout=30)
        if rc == 0 and out and out.strip():
            try:
                parts = out.strip().split()
                la = {
                    "1m": float(parts[0]),
                    "5m": float(parts[1]),
                    "15m": float(parts[2]),
                }
            except Exception:
                la = None

        if la is None:
            rc, out, _ = _ssh_exec(ssh, "uptime", timeout=30)
            if rc == 0 and out and out.strip() and "load average:" in out.lower():
                try:
                    avg_part = out.lower().split("load average:")[1].strip()
                    parts = avg_part.replace(",", " ").split()
                    la = {
                        "1m": float(parts[0]),
                        "5m": float(parts[1]),
                        "15m": float(parts[2]),
                    }
                except Exception:
                    la = None
        res["loadavg"] = la

        # 3.2. Получаем честный процент утилизации CPU через vmstat
        cpu_pct = None

        # Запрашиваем vmstat (интервал 1 секунда, 2 замера) и забираем последнюю строку
        rc_vm, out_vm, _ = _ssh_exec(ssh, "vmstat 1 2 | tail -n 1", timeout=30)
        if rc_vm == 0 and out_vm and out_vm.strip():
            try:
                parts_vm = out_vm.strip().split()
                # В стандартном выводе vmstat последние 5 колонок — это CPU: us, sy, id, wa, st
                if len(parts_vm) >= 15:
                    us = int(parts_vm[-5])  # User
                    sy = int(parts_vm[-4])  # System

                    # Честная нагрузка — это сумма работы в пространстве пользователя и ядра
                    cpu_pct = float(us + sy)
            except Exception:
                cpu_pct = None

        # 3.3. Резервный фолбэк
        # Если vmstat по какой-то причине завершился ошибкой, возвращаемся к оценке по loadavg
        if cpu_pct is None and res.get("cpu_count") and la and la.get("1m") is not None:
            try:
                cpu_pct = (la.get("1m") / float(res["cpu_count"])) * 100.0
            except Exception:
                cpu_pct = None

        # Запись финального результата
        if cpu_pct is not None:
            if cpu_pct < 0:
                cpu_pct = 0.0
            res["cpu_percent"] = round(min(cpu_pct, 100.0), 2)
        else:
            res["cpu_percent"] = None

        # -------------------------------------------------------------------------
        # 4. Сбор метрик оперативной памяти (meminfo)
        # -------------------------------------------------------------------------
        rc, out, _ = _ssh_exec(ssh, "cat /proc/meminfo", timeout=30)
        total_kb = None
        avail_kb = None
        if rc == 0 and out and out.strip():
            try:
                for line in out.splitlines():
                    if line.startswith("MemTotal:"):
                        total_kb = int(line.split()[1])
                    elif line.startswith("MemAvailable:"):
                        avail_kb = int(line.split()[1])
            except Exception:
                pass

        if total_kb is not None and avail_kb is not None:
            total = total_kb * 1024
            avail = avail_kb * 1024
            used = total - avail
            res["mem_total"] = total
            res["mem_available"] = avail
            res["mem_used"] = used
            res["mem_percent"] = round((used / total) * 100.0, 2) if total else None

        # -------------------------------------------------------------------------
        # 5. Сбор метрик использования дискового пространства корня (/)
        # -------------------------------------------------------------------------
        rc, out, _ = _ssh_exec(ssh, "LC_ALL=C df -P -B1 / | tail -n 1", timeout=30)
        try:
            parts = out.strip().split()
            if len(parts) >= 5:
                total = int(parts[1])
                used = int(parts[2])
                avail = int(parts[3])
                res["disk_total"] = total
                res["disk_used"] = used
                res["disk_free"] = avail
                res["disk_percent"] = round((used / total) * 100.0, 2) if total else None
        except Exception:
            pass

        # -------------------------------------------------------------------------
        # 6. Получение времени аптайма сервера (uptime seconds)
        # -------------------------------------------------------------------------
        up = None
        rc, out, _ = _ssh_exec(ssh, "cat /proc/uptime", timeout=30)
        if rc == 0 and out and out.strip():
            try:
                up = float(out.strip().split()[0])
            except Exception:
                up = None

        if up is None:
            rc, out, _ = _ssh_exec(ssh, "uptime -s", timeout=30)
            if rc == 0 and out and out.strip():
                try:
                    boot_dt = datetime.fromisoformat(out.strip())
                    up = (datetime.now() - boot_dt).total_seconds()
                    if up < 0:
                        up = 0.0
                except Exception:
                    up = None
        res["uptime_seconds"] = up

        res["ok"] = True
    except Exception as e:
        res["ok"] = False
        res["error"] = str(e)
    finally:
        try:
            ssh.close()
        except Exception:
            pass
    return res


def collect_hosts_metrics() -> Dict[str, Any]:
    """Последовательно собирает метрики по всем хостам (локальному и удалённым по SSH)."""
    items: List[Dict[str, Any]] = []
    try:
        hosts = database.get_all_hosts()
    except Exception as e:
        return {"ok": False, "items": [], "error": f"get_all_hosts failed: {e}"}

    for h in hosts:
        # Проверяем наличие SSH настроек
        if h.get("ssh_host") and h.get("ssh_user"):
            # Сервер с SSH - получаем метрики
            try:
                m = get_host_metrics_via_ssh(h)
            except Exception as e:
                m = {"ok": False, "host_name": h.get("host_name"), "error": str(e)}
        else:
            # Сервер без SSH - показываем базовую информацию
            m = {
                "ok": False,
                "host_name": h.get("host_name"),
                "host_url": h.get("host_url"),
                "error": "SSH не настроен",
                "cpu_percent": None,
                "mem_percent": None,
                "disk_percent": None,
                "uptime_seconds": None,
            }
        items.append(m)

    return {"ok": True, "items": items}


async def get_docker_logs(lines_count: int = 50) -> str:
    """Получает указанное количество последних строк логов Docker-контейнера."""
    try:
        # Запускаем системную команду получения логов контейнера
        process = await asyncio.create_subprocess_exec(
            "docker",
            "logs",
            "--tail",
            str(lines_count),
            "3xui-shopbot",
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        stdout, stderr = await process.communicate()

        # Объединяем потоки вывода и ошибок
        logs = stdout.decode("utf-8", errors="ignore") + stderr.decode("utf-8", errors="ignore")

        if not logs.strip():
            return "📋 Логи пустые или контейнер не генерирует вывод."

        return logs
    except Exception as e:
        logger.exception(f"Ошибка при получении логов Docker: {e}")


async def get_live_metrics_without_db() -> dict:
    """Опрашивает локальную панель и все удаленные серверы по SSH параллельно.

    Возвращает данные строго в оперативную память, ничего не записывая в БД.
    """
    items = []

    # -------------------------------------------------------------------------
    # 1. Сбор локальных метрик самой панели (In-Memory)
    # -------------------------------------------------------------------------
    try:
        local_metrics = await asyncio.to_thread(get_local_metrics)
        if local_metrics:
            local_metrics["host_name"] = "Панель"
            local_metrics["is_local"] = True
            items.append(local_metrics)
    except Exception as e:
        logger.exception("Ошибка при сборе локальных метрик:")
        items.append({"ok": False, "host_name": "Панель", "is_local": True, "error": str(e)})

    # -------------------------------------------------------------------------
    # 2. Получение списка удаленных хостов из БД
    # -------------------------------------------------------------------------
    try:
        # Вызываем асинхронно, чтобы синхронный запрос к диску не вешал бота
        hosts = await asyncio.to_thread(database.get_all_hosts) or []
    except Exception:
        # Логируем ошибку в консоль, если БД упадёт
        logger.exception("Ошибка получения списка хостов из БД в live_metrics:")
        hosts = []

    # -------------------------------------------------------------------------
    # 3. Асинхронная задача для опроса одного хоста через SSH
    # -------------------------------------------------------------------------
    async def process_host(h: dict):
        host_name = h.get("host_name", "Сервер")
        if not h.get("ssh_host") or not h.get("ssh_user"):
            return {
                "ok": False,
                "host_name": host_name,
                "is_local": False,
                "error": "SSH не настроен",
            }

        try:
            # Опрос по SSH — долгая операция, её правильно оставлять в asyncio.to_thread
            m = await asyncio.to_thread(get_host_metrics_via_ssh, h)
            if m:
                m["host_name"] = host_name
                m["is_local"] = False
                return m
        except Exception as e:
            return {
                "ok": False,
                "host_name": host_name,
                "is_local": False,
                "error": str(e),
            }

        return {
            "ok": False,
            "host_name": host_name,
            "is_local": False,
            "error": "Нет данных",
        }

    # -------------------------------------------------------------------------
    # 4. Одновременный параллельный запуск опроса всех SSH-серверов
    # -------------------------------------------------------------------------
    tasks = [process_host(h) for h in hosts if h.get("host_name")]
    if tasks:
        results = await asyncio.gather(*tasks, return_exceptions=True)
        for res in results:
            if isinstance(res, dict):
                items.append(res)
            elif isinstance(res, Exception):
                logger.error(f"Критическое исключение gather при опросе хоста: {res}")

    # -------------------------------------------------------------------------
    # 5. Формирование и возврат итогового пакета метрик
    # -------------------------------------------------------------------------
    return {"ok": True, "items": items}


async def restart_docker_container(timeout: int = 30) -> str:
    """Перезапускает указанный Docker-контейнер.

    Параметр timeout задает время (в секундах) на плавную остановку перед SIGKILL.
    """
    try:
        # Запускаем системную команду перезапуска контейнера.
        # Флаг -t уменьшает время ожидания остановки, чтобы ответ успел уйти до деструкции.
        process = await asyncio.create_subprocess_exec(
            "docker",
            "restart",
            "-t",
            str(timeout),
            "3xui-shopbot",
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )

        # Ждем завершения команды
        stdout, stderr = await process.communicate()

        stdout_str = stdout.decode("utf-8", errors="ignore").strip()
        stderr_str = stderr.decode("utf-8", errors="ignore").strip()

        # Если в stderr есть данные — значит, команда выполнилась с ошибкой
        if stderr_str:
            logger.error(f"Ошибка при перезапуске контейнера: {stderr_str}")

        # Docker в stdout возвращает имя или ID успешно перезапущенного контейнера
        container_identifier = stdout_str if stdout_str else "3xui-shopbot"
        logger.info(f"Контейнер {container_identifier} успешно отправлен на перезагрузку!")

    except Exception as e:
        logger.exception(f"Критическая ошибка при вызове команды Docker: {e}")
