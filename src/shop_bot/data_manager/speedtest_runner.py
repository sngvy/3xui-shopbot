"""Запуск speedtest (Ookla CLI или iperf3) на удалённых хостах по SSH и сохранение результатов."""

import asyncio
import json
import logging
import os
import re
import time
from urllib.parse import urlparse

import aiohttp
import paramiko

from shop_bot.data_manager import database

logger = logging.getLogger(__name__)

server_id = "-s 25633"  # suec//dacor GmbH

# Переключатель метода спидтеста, читается из env при каждом запуске (без рестарта):
#   USE_OOKLA_SPEEDTEST=1 (по умолчанию) — консольный Ookla speedtest CLI по SSH
#   USE_OOKLA_SPEEDTEST=0              — iperf3 до публичного сервера
IPERF3_SERVER_HOST = os.environ.get("IPERF3_SERVER_HOST", "iperf3.moji.fr")
IPERF3_SERVER_PORT = os.environ.get("IPERF3_SERVER_PORT", "5200-5240").strip()
IPERF3_DURATION = int(os.environ.get("IPERF3_DURATION", "10"))

# Ретраи: тесты (особенно iperf3 до публичного сервера и Ookla CLI) иногда
# рвутся из-за занятого сервера/сетевого моргания — ретраим весь прогон
# целиком (свежее SSH-соединение на каждой попытке), а не только парсинг.
SPEEDTEST_RETRIES = max(1, int(os.environ.get("SPEEDTEST_RETRIES", "3")))
SPEEDTEST_RETRY_DELAY = float(os.environ.get("SPEEDTEST_RETRY_DELAY", "5"))


def _retry_sync(fn, attempts: int | None = None, delay: float | None = None, label: str = ""):
    """Повторяет синхронную fn() до attempts раз, пока не получим dict с ok=True.

    Между попытками ждём delay секунд. Если все попытки неудачны — возвращает
    последний результат (или {'ok': False, 'error': ...} при исключениях).
    """
    attempts = attempts or SPEEDTEST_RETRIES
    delay = SPEEDTEST_RETRY_DELAY if delay is None else delay
    last_result: dict | None = None
    for i in range(1, attempts + 1):
        try:
            result = fn()
        except Exception as e:
            result = {"ok": False, "error": f"exception: {e}"}
        last_result = result
        if isinstance(result, dict) and result.get("ok"):
            if i > 1:
                logger.info(f"{label}: успех с попытки {i}/{attempts}")
            return result
        if i < attempts:
            logger.warning(
                f"{label}: попытка {i}/{attempts} неудачна ({result.get('error') if isinstance(result, dict) else result}), повтор через {delay}с"
            )
            time.sleep(delay)
    return last_result if last_result is not None else {"ok": False, "error": "unknown error"}


async def _retry_async(
    fn, attempts: int | None = None, delay: float | None = None, label: str = ""
):
    """Асинхронная версия _retry_sync — fn это awaitable-функция без аргументов."""
    attempts = attempts or SPEEDTEST_RETRIES
    delay = SPEEDTEST_RETRY_DELAY if delay is None else delay
    last_result: dict | None = None
    for i in range(1, attempts + 1):
        try:
            result = await fn()
        except Exception as e:
            result = {"ok": False, "error": f"exception: {e}"}
        last_result = result
        if isinstance(result, dict) and result.get("ok"):
            if i > 1:
                logger.info(f"{label}: успех с попытки {i}/{attempts}")
            return result
        if i < attempts:
            logger.warning(
                f"{label}: попытка {i}/{attempts} неудачна ({result.get('error') if isinstance(result, dict) else result}), повтор через {delay}с"
            )
            await asyncio.sleep(delay)
    return last_result if last_result is not None else {"ok": False, "error": "unknown error"}


def _use_ookla_speedtest() -> bool:
    val = os.environ.get("USE_OOKLA_SPEEDTEST", "1").strip().lower()
    return val not in ("0", "false", "no", "off", "")


def _parse_host_port_from_url(url: str) -> tuple[str | None, int | None, bool]:
    try:
        u = urlparse(url)
        host = u.hostname
        port = u.port
        is_https = u.scheme == "https"
        if port is None:
            port = 443 if is_https else 80
        return host, port, is_https
    except Exception:
        return None, None, False


async def net_probe_for_host(host_row: dict) -> dict:
    """Обёртка с ретраями над _net_probe_once — TCP connect / HTTP иногда моргают, поэтому пробуем несколько раз перед тем как сдаться."""
    return await _retry_async(lambda: _net_probe_once(host_row), label="net_probe")


async def _net_probe_once(host_row: dict) -> dict:
    """Lightweight network probe from panel to host_url: TCP connect + HTTP GET / (HEAD).

    Returns dict with ok, ping_ms (TCP connect time), http_ms, error (if any).
    """
    url = (host_row.get("host_url") or "").strip()
    target_host, target_port, _ = _parse_host_port_from_url(url)
    result = {
        "ok": False,
        "method": "net",
        "ping_ms": None,
        "jitter_ms": None,
        "download_mbps": None,
        "upload_mbps": None,
        "server_name": None,
        "server_id": None,
        "http_ms": None,
        "error": None,
    }
    if not target_host or not target_port:
        result["error"] = f"Invalid host_url: {url}"
        return result

    # TCP connect timing
    try:
        loop = asyncio.get_event_loop()
        start = loop.time()
        reader, writer = await asyncio.wait_for(
            asyncio.open_connection(target_host, target_port), timeout=30
        )
        tcp_ms = (loop.time() - start) * 1000.0
        result["ping_ms"] = round(tcp_ms, 2)
        try:
            writer.close()
            await writer.wait_closed()
        except Exception:
            pass
    except Exception as e:
        result["error"] = f"TCP connect failed: {e}"
        return result

    # HTTP HEAD/GET timing
    try:
        async with aiohttp.ClientSession() as session:
            start = asyncio.get_event_loop().time()
            async with session.head(url, timeout=aiohttp.ClientTimeout(total=30)) as resp:
                _ = resp.status
            http_ms = (asyncio.get_event_loop().time() - start) * 1000.0
            result["http_ms"] = round(http_ms, 2)
        result["ok"] = True
    except Exception:
        # Fallback to GET if HEAD not supported
        try:
            async with aiohttp.ClientSession() as session:
                start = asyncio.get_event_loop().time()
                async with session.get(url, timeout=aiohttp.ClientTimeout(total=30)) as resp:
                    _ = await resp.text()
                http_ms = (asyncio.get_event_loop().time() - start) * 1000.0
                result["http_ms"] = round(http_ms, 2)
            result["ok"] = True
        except Exception as e:
            result["error"] = f"HTTP failed: {e}"
    return result


def _ssh_exec_json(ssh: paramiko.SSHClient, commands: list[str]) -> tuple[dict | None, str | None]:
    """Try commands sequentially; expect JSON on stdout. Returns (json_obj, error)."""
    for cmd in commands:
        try:
            stdin, stdout, stderr = ssh.exec_command(cmd, timeout=120)
            out = stdout.read().decode("utf-8", errors="ignore")
            err = stderr.read().decode("utf-8", errors="ignore")
            if out:
                out = out.strip()
                # Attempt to extract JSON if there is noise
                m = re.search(r"\{.*\}$", out, re.S)
                if m:
                    out = m.group(0)
                try:
                    data = json.loads(out)
                    return data, None
                except Exception:
                    pass
            if err:
                logger.debug(f"Ошибка SSH-команды ({cmd}): {err}")
        except Exception as e:
            logger.debug(f"Не удалось выполнить SSH-команду '{cmd}': {e}")
            continue
    return None, "No JSON output from speedtest commands"


def _parse_ookla_json(data: dict) -> dict:
    # Ookla CLI JSON format (-f json)
    try:
        ping_ms = float(data.get("ping", {}).get("latency")) if data.get("ping") else None
        jitter = float(data.get("ping", {}).get("jitter")) if data.get("ping") else None
        down_bps = float(data.get("download", {}).get("bandwidth", 0)) * 8.0  # Bytes/s -> bits/s
        up_bps = float(data.get("upload", {}).get("bandwidth", 0)) * 8.0
        server = data.get("server", {})
        return {
            "ping_ms": round(ping_ms, 2) if ping_ms is not None else None,
            "jitter_ms": round(jitter, 2) if jitter is not None else None,
            "download_mbps": round(down_bps / (1_000_000.0), 2) if down_bps else None,
            "upload_mbps": round(up_bps / (1_000_000.0), 2) if up_bps else None,
            "server_name": server.get("name"),
            "server_id": (str(server.get("id")) if server.get("id") is not None else None),
        }
    except Exception:
        return {}


def _parse_speedtest_cli_json(data: dict) -> dict:
    # Speedtest-cli (sivel) JSON
    try:
        ping_ms = float(data.get("ping")) if data.get("ping") is not None else None
        jitter = None
        down_bps = float(data.get("download", 0))  # Bits per second
        up_bps = float(data.get("upload", 0))
        srv = data.get("server", {})
        return {
            "ping_ms": round(ping_ms, 2) if ping_ms is not None else None,
            "jitter_ms": jitter,
            "download_mbps": round(down_bps / 1_000_000.0, 2) if down_bps else None,
            "upload_mbps": round(up_bps / 1_000_000.0, 2) if up_bps else None,
            "server_name": srv.get("name"),
            "server_id": str(srv.get("id")) if srv.get("id") is not None else None,
        }
    except Exception:
        return {}


def _bits_per_second_from_end(data: dict, prefer_received: bool = True) -> float | None:
    """Достаёт bits_per_second из блока end.sum_received/sum_sent JSON-вывода iperf3.

    prefer_received=True — берём то, что реально приняла принимающая сторона
    (честнее при потерях по пути, чем то, что было отправлено).
    """
    try:
        end = data.get("end", {})
        received = end.get("sum_received") or {}
        sent = end.get("sum_sent") or {}
        primary, fallback = (received, sent) if prefer_received else (sent, received)
        bps = primary.get("bits_per_second") or fallback.get("bits_per_second")
        return float(bps) if bps else None
    except Exception:
        return None


def _run_iperf3_json(
    ssh: paramiko.SSHClient, extra_args: str, timeout: int
) -> tuple[dict | None, str | None]:
    """Запускает iperf3 -J с доп. аргументами (-R, -u и т.д.), парсит JSON.

    Возвращает data, error — на успех error=None, на неудачу data=None.
    """
    cmd = f"iperf3 -c {IPERF3_SERVER_HOST} -p {IPERF3_SERVER_PORT} -J {extra_args}"
    rc, out, err = _ssh_exec(ssh, cmd, timeout=timeout)

    data = None
    try:
        data = json.loads(out.strip())
    except Exception:
        m = re.search(r"\{.*\}\s*$", out, re.S)
        if m:
            try:
                data = json.loads(m.group(0))
            except Exception:
                data = None

    if not data:
        return None, f"iperf3 не вернул валидный JSON (rc={rc}): {(err or out)[:300]}"
    if isinstance(data.get("error"), str):
        # Типичная ситуация с публичными серверами: заняты другим тестом
        return None, f"iperf3: {data['error']}"
    return data, None


def _ping_ms_via_ssh(ssh: paramiko.SSHClient, host: str) -> float | None:
    """Обычный ICMP-пинг (5 пакетов) с удалённого хоста до iperf3-сервера — iperf3 сам по себе ping не измеряет, для этого используем системный ping."""
    rc, out, err = _ssh_exec(ssh, f"ping -c 5 -q {host} 2>&1", timeout=30)
    combined = (out or "") + (err or "")
    # Ищем "rtt min/avg/max/mdev = 0.5/0.6/0.8/0.1 ms"
    m = re.search(r"=\s*[\d.]+/([\d.]+)/[\d.]+", combined)
    if m:
        try:
            return round(float(m.group(1)), 2)
        except ValueError:
            return None
    return None


def _iperf3_speedtest_sync(ssh: paramiko.SSHClient) -> dict:
    """Полноценный тест через iperf3 до публичного сервера (по умолчанию iperf-ams-nl.eranium.net) — download, upload, ping и jitter, как у обычного speedtest, просто через iperf3 вместо Ookla CLI.

    Требует уже установленный iperf3 на удалённом хосте — SSH-пользователь
    не root и ставить пакеты не может, поэтому авто-установки нет, при
    отсутствии бинаря сразу возвращаем понятную ошибку.
    """
    rc_chk, out_chk, _ = _ssh_exec(ssh, "command -v iperf3 || echo NO", timeout=30)
    if "NO" in out_chk:
        return {
            "ok": False,
            "error": "iperf3 не найден на хосте (пользователь SSH не root — авто-установка не выполняется, поставьте iperf3 вручную)",
        }

    ping_ms = _ping_ms_via_ssh(ssh, IPERF3_SERVER_HOST)

    # UPLOAD: обычное направление TCP-теста — наш хост (клиент iperf3) шлёт
    # данные на сервер, сервер принимает. Это и есть скорость отдачи хоста.
    # Публичный iperf3-сервер иногда занят другим тестом / коннект моргает —
    # ретраим этот подшаг отдельно, не пересдавая весь спидтест целиком.
    def _do_upload():
        d, e = _run_iperf3_json(ssh, f"-t {IPERF3_DURATION}", timeout=IPERF3_DURATION + 20)
        return {"ok": d is not None, "data": d, "error": e}

    upload_res = _retry_sync(_do_upload, attempts=2, delay=4, label="iperf3 upload")
    if not upload_res.get("ok"):
        return {"ok": False, "error": f"upload: {upload_res.get('error')}"}
    upload_data = upload_res.get("data")
    upload_bps = _bits_per_second_from_end(upload_data)

    # DOWNLOAD: флаг -R (reverse) — сервер шлёт данные НАМ, наш хост принимает.
    # Именно это в обычных спидтестах и называют "скоростью скачивания".
    def _do_download():
        d, e = _run_iperf3_json(ssh, f"-R -t {IPERF3_DURATION}", timeout=IPERF3_DURATION + 20)
        return {"ok": d is not None, "data": d, "error": e}

    download_res = _retry_sync(_do_download, attempts=2, delay=4, label="iperf3 download")
    if not download_res.get("ok"):
        return {"ok": False, "error": f"download: {download_res.get('error')}"}
    download_data = download_res.get("data")
    download_bps = _bits_per_second_from_end(download_data)

    # JITTER: TCP jitter не даёт в принципе — нужен короткий UDP-прогон.
    # Небольшой битрейт (10M) и короткая длительность — это не throughput-тест,
    # а только для стаба jitter/потерь, поэтому не грузим канал сильно.
    # Джиттер не критичен для итогового ok — ретраим помягче, без падения теста.
    jitter_ms = None

    def _do_jitter():
        d, e = _run_iperf3_json(ssh, "-u -b 10M -t 3", timeout=30)
        return {"ok": d is not None, "data": d, "error": e}

    jitter_res = _retry_sync(_do_jitter, attempts=2, delay=2, label="iperf3 jitter")
    jitter_data = jitter_res.get("data")
    if jitter_data:
        try:
            jitter_ms = round(float(jitter_data.get("end", {}).get("sum", {}).get("jitter_ms")), 2)
        except (TypeError, ValueError):
            jitter_ms = None

    if not download_bps and not upload_bps:
        return {
            "ok": False,
            "error": "iperf3 не вернул bits_per_second ни для download, ни для upload",
        }

    return {
        "ok": True,
        "ping_ms": ping_ms,
        "jitter_ms": jitter_ms,
        "download_mbps": round(download_bps / 1_000_000.0, 2) if download_bps else None,
        "upload_mbps": round(upload_bps / 1_000_000.0, 2) if upload_bps else None,
        "server_name": f"{IPERF3_SERVER_HOST}:{IPERF3_SERVER_PORT} (iperf3)",
        "server_id": None,
    }


async def ssh_speedtest_for_host(host_row: dict) -> dict:
    """Run speedtest on remote host via SSH. Tries Ookla CLI first, then speedtest-cli.

    Returns dict with ok, metrics, error.
    """
    result = {
        "ok": False,
        "method": "ssh",
        "ping_ms": None,
        "jitter_ms": None,
        "download_mbps": None,
        "upload_mbps": None,
        "server_name": None,
        "server_id": None,
        "error": None,
    }
    ssh_host = (host_row.get("ssh_host") or "").strip()
    ssh_port = int(host_row.get("ssh_port") or 22)
    ssh_user = (host_row.get("ssh_user") or "").strip()
    ssh_password = host_row.get("ssh_password")
    ssh_key_path = (host_row.get("ssh_key_path") or "").strip() or None

    if not ssh_host or not ssh_user:
        result["error"] = "SSH settings are not configured for host"
        return result

    def _run_ssh() -> dict:
        ssh = paramiko.SSHClient()
        ssh.set_missing_host_key_policy(paramiko.AutoAddPolicy())
        if ssh_key_path:
            pkey = None
            try:
                pkey = paramiko.RSAKey.from_private_key_file(ssh_key_path)
            except Exception:
                try:
                    pkey = paramiko.Ed25519Key.from_private_key_file(ssh_key_path)
                except Exception:
                    pkey = None
            ssh.connect(
                ssh_host,
                port=ssh_port,
                username=ssh_user,
                password=ssh_password,
                pkey=pkey,
                timeout=30,
            )
        else:
            ssh.connect(
                ssh_host,
                port=ssh_port,
                username=ssh_user,
                password=ssh_password,
                timeout=30,
            )

        # Prefer Ookla CLI json format
        if _use_ookla_speedtest():
            data, err = _ssh_exec_json(
                ssh,
                [
                    # Ookla CLI with auto-accept (new flags)
                    f"speedtest --accept-license --accept-gdpr -f json {server_id}",
                    f"speedtest --accept-license --accept-gdpr --format=json {server_id}",
                    # Fallbacks without flags (на случай старых версий, уже принявших лицензию)
                    f"speedtest -f json {server_id}",
                    f"speedtest --format=json {server_id}",
                    # Python speedtest-cli (sivel)
                    f'speedtest-cli --json --server {server_id.replace("-s ", "")}',
                ],
            )
            ssh.close()
            if data:
                parsed = _parse_ookla_json(data)
                if not parsed.get("download_mbps") and "download" in data:
                    # Maybe speedtest-cli output
                    parsed = _parse_speedtest_cli_json(data)
                return {"ok": True, **parsed}
            return {"ok": False, "error": err or "unknown"}
        else:
            out = _iperf3_speedtest_sync(ssh)
            ssh.close()
            return out

    # Верхнеуровневый ретрай: если весь прогон развалился (SSH разорвался,
    # Ookla не отдал JSON и т.п.) — переподключаемся и пробуем заново с нуля.
    # attempts=2 здесь, т.к. внутри iperf3-подшаги уже ретраятся сами по себе —
    # иначе можно улететь в SPEEDTEST_RETRIES^2 попыток и тест будет вечно идти.
    def _run_ssh_safe() -> dict:
        try:
            return _run_ssh()
        except Exception as e:
            return {"ok": False, "error": str(e)}

    try:
        loop = asyncio.get_event_loop()
        out = await loop.run_in_executor(
            None,
            lambda: _retry_sync(
                _run_ssh_safe,
                attempts=2,
                delay=SPEEDTEST_RETRY_DELAY,
                label=f"ssh_speedtest[{ssh_host}]",
            ),
        )
        result.update(out)
    except Exception as e:
        result["error"] = str(e)
        result["ok"] = False
    return result


async def run_and_store_net_probe(host_name: str) -> dict:
    """Запускает net-probe для хоста и сохраняет результат в БД."""
    host = database.get_host(host_name)
    if not host:
        return {"ok": False, "error": "host not found"}
    res = await net_probe_for_host(host)
    database.insert_host_speedtest(
        host_name=host_name,
        method="net",
        ping_ms=res.get("ping_ms"),
        jitter_ms=res.get("jitter_ms"),
        download_mbps=res.get("download_mbps"),
        upload_mbps=res.get("upload_mbps"),
        server_name=res.get("server_name"),
        server_id=res.get("server_id"),
        ok=bool(res.get("ok")),
        error=res.get("error"),
    )
    return res


async def run_and_store_ssh_speedtest(host_name: str) -> dict:
    """Запускает SSH-speedtest для хоста и сохраняет результат в БД."""
    host = database.get_host(host_name)
    if not host:
        return {"ok": False, "error": "host not found"}
    res = await ssh_speedtest_for_host(host)
    database.insert_host_speedtest(
        host_name=host_name,
        method="ssh",
        ping_ms=res.get("ping_ms"),
        jitter_ms=res.get("jitter_ms"),
        download_mbps=res.get("download_mbps"),
        upload_mbps=res.get("upload_mbps"),
        server_name=res.get("server_name"),
        server_id=res.get("server_id"),
        ok=bool(res.get("ok")),
        error=res.get("error"),
    )
    return res


async def run_both_for_host(host_name: str) -> dict:
    """
    Запускает тесты хоста: SSH (основной) или NET (резервный).

    Если SSH успешен, NET не запускается.
    """
    out = {"ssh": None, "net": None}
    errors: list[str] = []

    # Попытка выполнить основной SSH-тест
    try:
        out["ssh"] = await run_and_store_ssh_speedtest(host_name)
        if out["ssh"].get("ok"):
            return {"ok": True, "details": out, "error": None}
        else:
            if out["ssh"].get("error"):
                errors.append(f"ssh: {out['ssh'].get('error')}")
    except Exception as e:
        errors.append(f"ssh exception: {e}")

    # SSH не удался — запускаем легкую NET-пробу
    try:
        out["net"] = await run_and_store_net_probe(host_name)
        if not out["net"].get("ok"):
            if out["net"].get("error"):
                errors.append(f"net: {out['net'].get('error')}")
    except Exception as e:
        errors.append(f"net exception: {e}")

    # Итоговый статус: True, если прошел хотя бы один тест
    overall_ok = bool(out["ssh"].get("ok") or out["net"].get("ok"))

    return {
        "ok": overall_ok,
        "details": out,
        "error": "; ".join(errors) if errors else None,
    }


def _ssh_connect(host_row: dict) -> paramiko.SSHClient:
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
        try:
            pkey = paramiko.RSAKey.from_private_key_file(ssh_key_path)
        except Exception:
            try:
                pkey = paramiko.Ed25519Key.from_private_key_file(ssh_key_path)
            except Exception:
                pkey = None
    ssh.connect(
        ssh_host,
        port=ssh_port,
        username=ssh_user,
        password=ssh_password,
        pkey=pkey,
        timeout=30,
    )
    return ssh


def _ssh_exec(ssh: paramiko.SSHClient, cmd: str, timeout: int = 180) -> tuple[int, str, str]:
    stdin, stdout, stderr = ssh.exec_command(cmd, timeout=timeout)
    out = stdout.read().decode("utf-8", errors="ignore")
    err = stderr.read().decode("utf-8", errors="ignore")
    rc = stdout.channel.recv_exit_status() if hasattr(stdout, "channel") else 0
    return rc, out, err


async def auto_install_speedtest_on_host(host_name: str) -> dict:
    """Attempt to auto-install Ookla speedtest or speedtest-cli on remote host via SSH.

    Tries package manager scripts, falls back to pip speedtest-cli. Returns {'ok', 'log'}.
    """
    host = database.get_host(host_name)
    if not host:
        return {"ok": False, "log": "host not found"}

    def _install() -> dict:
        log_lines: list[str] = []
        try:
            ssh = _ssh_connect(host)
        except Exception as e:
            return {"ok": False, "log": f"SSH connect failed: {e}"}
        try:
            # If already installed
            rc, out, err = _ssh_exec(
                ssh, 'command -v speedtest || command -v speedtest-cli || echo "NO"'
            )
            if "speedtest" in out or "speedtest-cli" in out:
                log_lines.append("Found existing speedtest binary: " + out.strip())
                # Проверим версию и примем лицензию
                rc2, o2, e2 = _ssh_exec(
                    ssh, "speedtest --accept-license --accept-gdpr --version || true"
                )
                ver_text = (o2 + e2).strip()
                if ver_text:
                    log_lines.append(
                        (
                            "$ speedtest --accept-license --accept-gdpr --version\n" + ver_text
                        ).strip()
                    )
                # Если версия не 1.2.0 — выполним переустановку через tarball ниже
                need_reinstall = True
                try:
                    if "1.2.0" in ver_text:
                        need_reinstall = False
                except Exception:
                    need_reinstall = True
                if not need_reinstall:
                    return {"ok": True, "log": "\n".join(log_lines)}
                else:
                    log_lines.append(
                        "Different Ookla speedtest version detected; reinstalling 1.2.0 via tarball."
                    )

            # Detect OS info
            rc, out, _ = _ssh_exec(ssh, "cat /etc/os-release || uname -a")
            os_release = out.lower()
            log_lines.append("OS detection: " + out.strip())

            # Сначала: УСТАНОВКА ЧЕРЕЗ TARBALL СТРОГОЙ ВЕРСИИ 1.2.0 (предпочтительно)
            # Detect arch
            rc, arch_out, _ = _ssh_exec(ssh, "uname -m || echo unknown")
            arch = (arch_out or "").strip()
            # Map to Ookla naming
            if arch in ("x86_64", "amd64"):
                arch_tag = "linux-x86_64"
            elif arch in ("aarch64", "arm64"):
                arch_tag = "linux-aarch64"
            elif arch in ("armv7l",):
                arch_tag = "linux-armhf"
            else:
                arch_tag = "linux-x86_64"
            tar_url = f"https://install.speedtest.net/app/cli/ookla-speedtest-1.2.0-{arch_tag}.tgz"
            cmds_tar = [
                f"curl -fsSL {tar_url} -o /tmp/ookla-speedtest.tgz || wget -O /tmp/ookla-speedtest.tgz {tar_url}",
                "mkdir -p /tmp/ookla-speedtest && tar -xf /tmp/ookla-speedtest.tgz -C /tmp/ookla-speedtest",
                "install -m 0755 /tmp/ookla-speedtest/speedtest /usr/local/bin/speedtest || (cp /tmp/ookla-speedtest/speedtest /usr/local/bin/speedtest && chmod +x /usr/local/bin/speedtest)",
                # Принятие лицензии сразу после установки бинаря (идемпотентно)
                "speedtest --accept-license --accept-gdpr --version || true",
                "rm -rf /tmp/ookla-speedtest /tmp/ookla-speedtest.tgz",
            ]
            for c in cmds_tar:
                rc, o, e = _ssh_exec(ssh, c)
                log_lines.append(f"$ {c}\n{o}{e}".strip())

            # Verify version is exactly 1.2.0
            rc, out, err = _ssh_exec(ssh, 'command -v speedtest || echo "NO"')
            if "NO" not in out:
                rcv, ov, ev = _ssh_exec(ssh, "speedtest --version 2>&1 || true")
                ver_info = (ov + ev).strip()
                if "1.2.0" in ver_info:
                    log_lines.append(
                        "Installed Ookla speedtest via tarball (1.2.0): " + out.strip()
                    )
                    return {"ok": True, "log": "\n".join(log_lines)}
                else:
                    log_lines.append(
                        "Tarball install finished but version check did not return 1.2.0; continuing fallbacks."
                    )

            # Если по какой-то причине tarball не сработал — пробуем официальный репозиторий (м.б. недоступен на noble)
            cmds_deb = [
                "which sudo || true",
                "export DEBIAN_FRONTEND=noninteractive",
                "curl -fsSL https://packagecloud.io/install/repositories/ookla/speedtest-cli/script.deb.sh | bash",
                "apt-get update -y || true",
                "apt-get install -y speedtest || true",
            ]
            cmds_rpm = [
                "curl -fsSL https://packagecloud.io/install/repositories/ookla/speedtest-cli/script.rpm.sh | bash",
                "yum -y install speedtest || dnf -y install speedtest || true",
            ]
            if "debian" in os_release or "ubuntu" in os_release:
                for c in cmds_deb:
                    rc, o, e = _ssh_exec(ssh, c)
                    log_lines.append(f"$ {c}\n{o}{e}".strip())
            elif any(x in os_release for x in ["centos", "rhel", "fedora", "almalinux", "rocky"]):
                for c in cmds_rpm:
                    rc, o, e = _ssh_exec(ssh, c)
                    log_lines.append(f"$ {c}\n{o}{e}".strip())

            # Check again
            rc, out, err = _ssh_exec(
                ssh, 'command -v speedtest || command -v speedtest-cli || echo "NO"'
            )
            if "speedtest" in out or "speedtest-cli" in out:
                log_lines.append("Installed speedtest successfully: " + out.strip())
                # Автопринятие лицензии для Ookla CLI, если присутствует
                rc2, o2, e2 = _ssh_exec(
                    ssh, "speedtest --accept-license --accept-gdpr --version || true"
                )
                if o2 or e2:
                    log_lines.append(
                        (
                            "$ speedtest --accept-license --accept-gdpr --version\n" + (o2 + e2)
                        ).strip()
                    )
                return {"ok": True, "log": "\n".join(log_lines)}

            # Fallback: try install speedtest-cli via pip
            pip_try = [
                "command -v python3 || command -v python || echo NO",
                "command -v pip3 || command -v pip || (apt-get update -y && apt-get install -y python3-pip) || (yum -y install python3-pip || dnf -y install python3-pip) || true",
                "pip3 install --upgrade pip || true",
                "pip3 install speedtest-cli || pip install speedtest-cli || true",
                # Create symlink if needed
                'command -v speedtest-cli || (which python3 && python3 -m pip show speedtest-cli && ln -sf $(python3 -c "import shutil,sys; import os; print(shutil.which("speedtest-cli") or "/usr/local/bin/speedtest-cli")") /usr/local/bin/speedtest-cli) || true',
            ]
            for c in pip_try:
                rc, o, e = _ssh_exec(ssh, c)
                log_lines.append(f"$ {c}\n{o}{e}".strip())

            # Final check
            rc, out, err = _ssh_exec(
                ssh, 'command -v speedtest || command -v speedtest-cli || echo "NO"'
            )
            if "NO" not in out:
                log_lines.append("Installed speedtest-cli via pip: " + out.strip())
                return {"ok": True, "log": "\n".join(log_lines)}

            # Последний фоллбек: ставим python-пакет speedtest-cli (не Ookla), если всё остальное не сработало
            # (этот шаг оставлен выше по коду — уже выполнен; если не сработал — выдаём ошибку ниже)

            return {
                "ok": False,
                "log": "Failed to install speedtest using available methods.\n"
                + "\n".join(log_lines),
            }
        finally:
            try:
                ssh.close()
            except Exception:
                pass

    loop = asyncio.get_event_loop()
    return await loop.run_in_executor(None, _install)
