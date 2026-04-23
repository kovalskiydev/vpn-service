# Web Admin (RU/EN)

## RU
Админ-панель для управления VPN-нодами, локациями, подписками, статистикой и Telegram-выдачей ключей.

### Локальный запуск через Docker
Из корня проекта:
```bash
./scripts/up.sh
```

### Полезные команды
```bash
# Логи панели
docker compose logs -f vpn-panel

# Перезапуск
docker compose restart vpn-panel

# Остановка
docker compose down

# Запуск с ботом
docker compose --profile bot up -d --build
```

### Переменные окружения
Смотрите `.env.example` в корне репозитория.

## EN
Admin panel for VPN nodes, locations, subscriptions, stats, and Telegram key issuance.

### Local Docker run
From repo root:
```bash
./scripts/up.sh
```

### Useful commands
```bash
# Panel logs
docker compose logs -f vpn-panel

# Restart
docker compose restart vpn-panel

# Stop
docker compose down

# Start with bot
docker compose --profile bot up -d --build
```

### Environment variables
See `.env.example` in repository root.
