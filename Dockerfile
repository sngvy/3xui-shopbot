FROM python:3.11-slim
ENV TZ=Europe/Moscow \
    PYTHONUNBUFFERED=1
RUN apt-get update && apt-get install -y --no-install-recommends \
    curl \
    ca-certificates \
    gnupg \
    lsb-release \
    tzdata && \
    # --- Настройка часового пояса ---
    ln -snf /usr/share/zoneinfo/$TZ /etc/localtime && \
    echo $TZ > /etc/timezone && \
    dpkg-reconfigure -f noninteractive tzdata && \
    # --- Современный способ добавления репозитория Docker ---
    # Создаем безопасную папку для ключей
    mkdir -p /etc/apt/keyrings && \
    # Скачиваем официальный GPG-ключ Docker и сохраняем его в формате gpg
    curl -fsSL https://download.docker.com/linux/debian/gpg | gpg --dearmor -o /etc/apt/keyrings/docker.gpg && \
    # Добавляем репозиторий Docker, явно указывая использовать созданный ключ через signed-by
    echo "deb [arch=$(dpkg --print-architecture) signed-by=/etc/apt/keyrings/docker.gpg] https://download.docker.com/linux/debian $(lsb_release -cs) stable" | tee /etc/apt/sources.list.d/docker.list > /dev/null && \
    # --- Обновляем списки и ставим только Docker CLI ---
    apt-get update && apt-get install -y --no-install-recommends docker-ce-cli && \
    # --- Финальная очистка для уменьшения веса образа ---
    apt-get clean && \
    rm -rf /var/lib/apt/lists/*
WORKDIR /app
RUN python3 -m venv .venv && \
    .venv/bin/pip install --no-cache-dir --index-url https://pypi-mirror.gitverse.ru/simple/ --trusted-host pypi-mirror.gitverse.ru --upgrade pip
ENV PATH="/app/.venv/bin:$PATH"
COPY . /app/project/
WORKDIR /app/project
RUN pip install --no-cache-dir --index-url https://pypi-mirror.gitverse.ru/simple/ --trusted-host pypi-mirror.gitverse.ru -e .
CMD ["python3", "-m", "shop_bot"]
