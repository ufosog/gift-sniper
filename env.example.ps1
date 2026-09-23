# Пример настроек. Скопируйте в env.ps1 и подставьте свои значения.
# ВНИМАНИЕ: env.ps1 содержит токены, не публиковать и не коммитить.

# --- токены площадок ---
# Portals НЕ требует токена (проверено 2026-09-19): все нужные запросы
# проходят без авторизации. Строку ниже можно не заполнять.
$env:PORTALS_AUTH = ""
# MRKT: cookie access_token из мини-аппа, живёт несколько часов.
$env:MRKT_ACCESS_TOKEN = "сюда_токен_mrkt"
# Tonnel токена не требует.

# --- Telegram-бот ---
$env:TELEGRAM_BOT_TOKEN  = "сюда_токен_бота"
$env:TELEGRAM_OWNER_ID   = "ваш_числовой_id"
$env:TELEGRAM_VIEWER_IDS = ""

# --- уведомления ---
$env:NOTIFY_ENABLED        = "true"
$env:MRKT_NOTIFY_ENABLED   = "true"
$env:TONNEL_NOTIFY_ENABLED = "false"
$env:NOTIFY_LEVELS         = "pair"
$env:NOTIFY_MIN_PROFIT_USD = "5"

# --- сбор данных ---
$env:POLL_INTERVAL_SEC        = "10"
$env:MAX_PAGES_PER_ITERATION  = "8"
$env:COLLECT_MIN_PRICE        = "15"
$env:TONNEL_POLL_INTERVAL_SEC = "15"
$env:TONNEL_COLLECT_MIN_PRICE = "15"

# --- межбиржевая сверка ---
$env:CROSS_CHECK_ENABLED = "true"
$env:CROSS_MAX_RATIO     = "5.0"

# --- бумажный журнал ---
$env:PAPER_JOURNAL_ENABLED      = "1"
$env:JOURNAL_START_BALANCE_TON  = "300"
$env:JOURNAL_MAX_POSITIONS      = "10"
$env:JOURNAL_MAX_POSITION_TON   = "60"
