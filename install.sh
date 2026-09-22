#!/bin/bash

GREEN='\033[0;32m'
YELLOW='\033[1;33m'
CYAN='\033[0;36m'
RED='\033[0;31m'
NC='\033[0m'

handle_error() {
    echo -e "\n${RED}Ошибка на строке $1. Установка прервана.${NC}"
    exit 1
}
trap 'handle_error $LINENO' ERR

read_input() {
    read -p "$1" "$2" < /dev/tty
}

read_input_yn() {
    read -p "$1" -n 1 -r REPLY < /dev/tty
    echo
}

REPO_URL="https://github.com/sngvy/3xui-shopbot.git"
PROJECT_DIR="3xui-shopbot"
NGINX_CONF_FILE="/etc/nginx/sites-available/${PROJECT_DIR}.conf"

echo -e "${GREEN}--- Запуск скрипта установки/обновления 3xui-ShopBot ---${NC}"

if [ -f "$NGINX_CONF_FILE" ]; then
    echo -e "\n${CYAN}Обнаружена существующая конфигурация. Скрипт запущен в режиме обновления.${NC}"

    if [ ! -d "$PROJECT_DIR" ]; then
        echo -e "${RED}Ошибка: Конфигурация Nginx существует, но папка проекта '${PROJECT_DIR}' не найдена!${NC}"
        echo -e "${YELLOW}Возможно, вы переместили или удалили папку. Для исправления удалите файл конфигурации Nginx и запустите установку заново:${NC}"
        echo -e "sudo rm ${NGINX_CONF_FILE}"
        exit 1
    fi

    cd $PROJECT_DIR

    echo -e "\n${CYAN}Шаг 1: Обновление кода из репозитория Git...${NC}"
    git fetch --all
    git reset --hard origin/main
    echo -e "${GREEN}✔ Код успешно обновлен.${NC}"

    echo -e "\n${CYAN}Шаг 1.1: Проверка и обновление конфигурации Nginx...${NC}"
    if ! grep -q "client_max_body_size" "$NGINX_CONF_FILE"; then
        echo -e "${YELLOW}Добавляем client_max_body_size 10M в конфигурацию Nginx...${NC}"
        # Вставка после директивы server_name
        sudo sed -i '/server_name/a \    client_max_body_size 10M;' "$NGINX_CONF_FILE"
        
        if sudo nginx -t; then
            sudo systemctl reload nginx
            echo -e "${GREEN}✔ Конфигурация Nginx обновлена и перезагружена.${NC}"
        else
            echo -e "${RED}Ошибка проверки конфигурации Nginx. Изменения не применены.${NC}"
        fi
    else
        echo -e "${GREEN}✔ Конфигурация Nginx уже содержит необходимые настройки.${NC}"
    fi

    echo -e "\n${CYAN}Шаг 2: Пересборка и перезапуск Docker-контейнеров...${NC}"
    sudo docker-compose down --remove-orphans && sudo docker-compose up -d --build
    
    echo -e "\n\n${GREEN}==============================================${NC}"
    echo -e "${GREEN}      🎉 Обновление успешно завершено! 🎉      ${NC}"
    echo -e "${GREEN}==============================================${NC}"
    echo -e "\nБот был обновлен до последней версии и перезапущен."

    exit 0
fi

echo -e "\n${YELLOW}Существующая конфигурация не найдена. Запускается первоначальная установка...${NC}"

echo -e "\n${CYAN}Шаг 1: Установка системных зависимостей...${NC}"
install_package() {
    if ! command -v $1 &> /dev/null; then
        echo -e "${YELLOW}Утилита '$1' не найдена. Устанавливаем...${NC}"
        sudo apt-get update
        sudo apt-get install -y $2
    else
        echo -e "${GREEN}✔ $1 уже установлен.${NC}"
    fi
}

install_package "git" "git"
install_package "docker" "docker.io"
install_package "docker-compose" "docker-compose"
install_package "nginx" "nginx"
install_package "curl" "curl"
install_package "certbot" "certbot python3-certbot-nginx"
install_package "dig" "dnsutils"

for service in docker nginx; do
    if ! sudo systemctl is-active --quiet $service; then
        echo -e "${YELLOW}Сервис $service не запущен. Запускаем и добавляем в автозагрузку...${NC}"
        sudo systemctl start $service
        sudo systemctl enable $service
    fi
done
echo -e "${GREEN}✔ Все системные зависимости установлены.${NC}"

echo -e "\n${CYAN}Шаг 2: Клонирование репозитория...${NC}"
if [ ! -d "$PROJECT_DIR" ]; then
    git clone $REPO_URL
fi
cd $PROJECT_DIR
echo -e "${GREEN}✔ Репозиторий готов.${NC}"

echo -e "\n${CYAN}Шаг 3: Настройка домена и получение SSL-сертификатов...${NC}"

read_input "Введите ваш домен (например, my-vpn-shop.com): " USER_INPUT_DOMAIN

if [ -z "$USER_INPUT_DOMAIN" ]; then
    echo -e "${RED}Ошибка: Домен не может быть пустым. Установка прервана.${NC}"
    exit 1
fi

# Санитизация домена: убрать схему/путь, оставить только ASCII-символы доменного имени
DOMAIN=$(echo "$USER_INPUT_DOMAIN" \
    | sed -e 's%^https\?://%%' -e 's%/.*$%%' \
    | tr -cd 'A-Za-z0-9.-' \
    | tr '[:upper:]' '[:lower:]')

read_input "Введите ваш email (для регистрации SSL-сертификатов Let's Encrypt): " EMAIL

echo -e "${GREEN}✔ Домен для работы: ${DOMAIN}${NC}"

# Получение публичного IPv4 сервера без вывода HTML
ipv4_re='^([0-9]{1,3}\.){3}[0-9]{1,3}$'
get_server_ip(){
    for url in \
        "https://api.ipify.org" \
        "https://ifconfig.co/ip" \
        "https://ipv4.icanhazip.com"; do
        ip=$(curl -fsS "$url" 2>/dev/null | tr -d '\r\n\t ')
        if [[ $ip =~ $ipv4_re ]]; then echo "$ip"; return 0; fi
    done
    # Fallback: локальная информация (может вернуть приватный IP)
    ip=$(hostname -I 2>/dev/null | awk '{print $1}')
    if [[ $ip =~ $ipv4_re ]]; then echo "$ip"; else echo ""; fi
}

# Разрешение IPv4 домена без обязательного dig
resolve_domain_ip(){
    # 1) getent hosts (glibc)
    ip=$(getent ahostsv4 "$DOMAIN" 2>/dev/null | awk '{print $1}' | head -n1)
    if [[ $ip =~ $ipv4_re ]]; then echo "$ip"; return 0; fi
    # 2) dig, если доступен
    if command -v dig >/dev/null 2>&1; then
        ip=$(dig +short A "$DOMAIN" 2>/dev/null | grep -E "$ipv4_re" | head -n1)
        if [[ $ip =~ $ipv4_re ]]; then echo "$ip"; return 0; fi
    fi
    # 3) nslookup, если доступен
    if command -v nslookup >/dev/null 2>&1; then
        ip=$(nslookup -type=A "$DOMAIN" 2>/dev/null | awk '/^Address: /{print $2; exit}')
        if [[ $ip =~ $ipv4_re ]]; then echo "$ip"; return 0; fi
    fi
    # 4) ping -c1 (как крайний случай)
    if command -v ping >/dev/null 2>&1; then
        ip=$(ping -4 -c1 -W1 "$DOMAIN" 2>/dev/null | sed -n 's/.*(\([0-9.]*\)).*/\1/p' | head -n1)
        if [[ $ip =~ $ipv4_re ]]; then echo "$ip"; return 0; fi
    fi
    echo ""
}

SERVER_IP=$(get_server_ip)
DOMAIN_IP=$(resolve_domain_ip)

if [ -n "$SERVER_IP" ]; then
    echo -e "${YELLOW}IP вашего сервера: $SERVER_IP${NC}"
else
    echo -e "${YELLOW}IP вашего сервера: (не удалось определить)${NC}"
fi

if [ -n "$DOMAIN_IP" ]; then
    echo -e "${YELLOW}IP, на который указывает домен '$DOMAIN': $DOMAIN_IP${NC}"
else
    echo -e "${YELLOW}IP, на который указывает домен '$DOMAIN': (не удалось определить)${NC}"
fi

if [ "$SERVER_IP" != "$DOMAIN_IP" ]; then
    echo -e "${RED}ВНИМАНИЕ: DNS-запись для домена $DOMAIN не указывает на IP-адрес этого сервера!${NC}"
    read_input_yn "Продолжить установку? (y/n): "
    if [[ ! $REPLY =~ ^[Yy]$ ]]; then echo "Установка прервана."; exit 1; fi
fi

if command -v ufw &> /dev/null && sudo ufw status | grep -q 'Status: active'; then
    echo -e "${YELLOW}Обнаружен активный файрвол (ufw). Открываем порты...${NC}"
    sudo ufw allow 80/tcp
    sudo ufw allow 443/tcp
    sudo ufw allow 1488/tcp
    sudo ufw allow 8443/tcp
fi

if [ -d "/etc/letsencrypt/live/$DOMAIN" ]; then
    echo -e "${GREEN}✔ SSL-сертификаты для домена $DOMAIN уже существуют.${NC}"
else
    echo -e "${YELLOW}Получаем SSL-сертификаты для $DOMAIN...${NC}"
    sudo certbot --nginx -d $DOMAIN --email $EMAIL --agree-tos --non-interactive --redirect
    echo -e "${GREEN}✔ SSL-сертификаты успешно получены.${NC}"
fi

echo -e "\n${CYAN}Шаг 4: Настройка Nginx...${NC}"
read_input "Какой порт вы будете использовать для вебхуков YooKassa? (443 или 8443, рекомендуется 8443): " YOOKASSA_PORT_INPUT
YOOKASSA_PORT=${YOOKASSA_PORT_INPUT:-443}

NGINX_ENABLED_FILE="/etc/nginx/sites-enabled/${PROJECT_DIR}.conf"

echo -e "Создаем конфигурацию Nginx..."
sudo rm -rf /etc/nginx/sites-enabled/default
sudo bash -c "cat > $NGINX_CONF_FILE" <<EOF
server {
    listen ${YOOKASSA_PORT} ssl http2;
    listen [::]:${YOOKASSA_PORT} ssl http2;
    server_name ${DOMAIN};
    client_max_body_size 10M;

    ssl_certificate /etc/letsencrypt/live/${DOMAIN}/fullchain.pem;
    ssl_certificate_key /etc/letsencrypt/live/${DOMAIN}/privkey.pem;
    include /etc/letsencrypt/options-ssl-nginx.conf;
    ssl_dhparam /etc/letsencrypt/ssl-dhparams.pem;

    location / {
        proxy_pass http://127.0.0.1:1488;
        proxy_set_header Host \$host;
        proxy_set_header X-Real-IP \$remote_addr;
        proxy_set_header X-Forwarded-For \$proxy_add_x_forwarded_for;
        proxy_set_header X-Forwarded-Proto \$scheme;
    }
}
EOF

if [ ! -f "$NGINX_ENABLED_FILE" ]; then
    sudo ln -s $NGINX_CONF_FILE $NGINX_ENABLED_FILE
fi

echo -e "${GREEN}✔ Конфигурация Nginx создана.${NC}"
echo -e "${YELLOW}Проверяем и перезагружаем Nginx...${NC}"
sudo nginx -t && sudo systemctl reload nginx

echo -e "\n${CYAN}Шаг 5: Сборка и запуск Docker-контейнера...${NC}"
if [ "$(sudo docker-compose ps -q)" ]; then
    sudo docker-compose down
fi
sudo docker-compose up -d --build

echo -e "\n\n${GREEN}=====================================================${NC}"
echo -e "${GREEN}      🎉 Установка и запуск успешно завершены! 🎉      ${NC}"
echo -e "${GREEN}=====================================================${NC}"
echo -e "\nВеб-панель доступна по адресу:"
echo -e "  - ${YELLOW}https://${DOMAIN}:${YOOKASSA_PORT}/login${NC}"
echo -e "\nДанные для первого входа:"
echo -e "  - Логин:   ${CYAN}admin${NC}"
echo -e "  - Пароль:  ${CYAN}admin${NC}"
echo -e "\n${RED}ПЕРВЫЕ ШАГИ:${NC}"
echo -e "1. Войдите в панель и ${RED}сразу же смените логин и пароль${NC}."
echo -e "2. На странице 'Настройки' введите ваш Telegram токен, username бота и ваш Telegram ID."
echo -e "3. Нажмите 'Сохранить' и затем 'Запустить Бота'."
echo -e "\n${CYAN}Не забудьте указать URL для вебхуков в YooKassa:${NC}"
echo -e "  - ${YELLOW}https://${DOMAIN}:${YOOKASSA_PORT}/yookassa-webhook${NC}"
echo -e "\n"
