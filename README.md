# Бот записи на услуги

Асинхронный Telegram-бот для записи в студию: календарь в Telegram Web App, тестовая оплата через Telegram Payments и админ-панель. Процесс слушает HTTP, поэтому его можно поднять на бесплатном Render или Koyeb: когда сервис засыпает, следующий webhook будит его.

## Что умеет

- `/start` сохраняет пользователя в PostgreSQL и показывает кнопку календаря.
- В Web App клиент выбирает услугу, день и свободное время. Подтверждение отправляет в чат JSON `{service_id, date, slot_time}` через `Telegram.WebApp.sendData()`.
- Бот на короткое время держит слот и выставляет счёт. `pre_checkout_query` ещё раз проверяет, что окно свободно, `successful_payment` переводит запись в статус «оплачена».
- `/my` показывает свои визиты и даёт отменить будущую запись.
- `/admin` открывает записи на сегодня и на ближайшие дни, а также закрытие отдельных слотов и целых дней.
- Deep link `https://t.me/<bot>?start=service_2` сразу подставляет услугу в календарь.

Длительность услуги занимает несколько шагов сетки. При шаге 30 минут стрижка на 60 минут закрывает два соседних слота, окрашивание на 120 минут — четыре. Так визиты не накладываются друг на друга. Неоплаченный счёт отпускает время через `HOLD_MINUTES` (по умолчанию 15).

## Стек

Python 3.11, aiogram 3, FastAPI, SQLAlchemy 2 + asyncpg, PostgreSQL (Supabase), статический Web App.

## Структура

```
bot/
  config.py                 настройки из окружения
  main.py                   FastAPI, webhook или polling
  schedule.py               сетка рабочего дня
  database/models.py        User, Service, TimeSlot, Appointment
  database/connection.py    async engine
  database/repository.py    транзакции записи и оплата
  handlers/                 клиент, админ, платежи
  keyboards/                reply-кнопка Web App и инлайн-меню
  middlewares/db_session.py сессия на каждый апдейт
webapp/                     календарь: index.html, app.js, style.css
```

У `Appointment` есть дополнительное поле `created_at`. По нему снимается бронь, если клиент открыл счёт и не оплатил его. Остальная схема совпадает с постановкой: статус `pending | confirmed | cancelled`.

## Локальный запуск

Нужны Python 3.11+ и Docker (только для PostgreSQL). Зависимость `tzdata` закрывает часовые пояса на Windows: без неё `Europe/Moscow` не находится.

```powershell
docker compose up -d
python -m venv .venv
.venv\Scripts\activate
pip install -r requirements.txt
copy .env.example .env
```

В `.env` для локальной машины:

```
MODE=polling
DATABASE_URL=postgresql+asyncpg://booking:booking@localhost:5432/booking
DB_SSL=false
WEBAPP_URL=https://<ваш-туннель>/webapp
PAYMENT_PROVIDER_TOKEN=<тестовый токен>
ADMIN_IDS=<ваш telegram id>
```

```powershell
python -m bot.main
```

Календарь в браузере: `http://127.0.0.1:8000/webapp/`. Оттуда можно проверить сетку, но счёт создаётся только из Telegram: `sendData()` работает, когда Web App открыт reply-кнопкой «Записаться». Кнопка меню бота для этого не подходит.

Telegram принимает Web App только по HTTPS. Для локальной проверки поднимите туннель (ngrok или cloudflared) на порт 8000 и запишите его в `WEBAPP_URL`. В режиме `polling` туннель нужен календарю, а сообщения бот забирает сам.

Таблицы и четыре демо-услуги создаются при старте. Повторный запуск не затирает цены, если услуги уже есть. Сетка строится на `SCHEDULE_DAYS` вперёд, по умолчанию на 21 день, с `WORK_START` до `WORK_END`.

## Supabase

1. Создайте проект на [supabase.com](https://supabase.com).
2. Возьмите URI в Project Settings → Database → Connection string.
3. На Render и Koyeb используйте **Session pooler** (IPv4). Прямой хост `db.<ref>.supabase.co` часто недоступен с бесплатных контейнеров.
4. Вставьте строку в `DATABASE_URL`. Префикс `postgresql://` можно оставить: приложение само подставит `postgresql+asyncpg`.
5. `DB_SSL=true`.

Пул маленький (`pool_size=2`) и с `pool_pre_ping`: после сна сервиса мёртвые соединения не используются. Для PgBouncer отключён кэш prepared statements.

## BotFather

1. `/newbot` — токен в `BOT_TOKEN`.
2. Узнайте свой id через `@userinfobot` и впишите его в `ADMIN_IDS`.
3. Payments → подключите провайдера и возьмите **тестовый** токен. В нём есть `:TEST:`. Это `PAYMENT_PROVIDER_TOKEN`.
4. Тестовая карта `4242 4242 4242 4242`, любой будущий срок, любой CVC. Бот подскажет её в чате, только если токен тестовый.

Цена в базе хранится в рублях. В счёт уходят копейки: 1500 ₽ → `150000`.

## Деплой на Render

Бесплатный Web Service засыпает без трафика. Режим `webhook` обязателен: Telegram POST на `/webhook/<WEBHOOK_SECRET>` будит контейнер.

1. Подключите репозиторий к Render (Blueprint из `render.yaml` или New → Web Service).
2. Runtime: Python. Сборка: `pip install -r requirements.txt`. Старт: `uvicorn bot.main:app --host 0.0.0.0 --port $PORT`.
3. Задайте секреты в панели (или через Blueprint `sync: false`):
   - `MODE=webhook`
   - `WEBHOOK_BASE_URL=https://<сервис>.onrender.com` без пути
   - `WEBHOOK_SECRET` — длинная случайная строка (`[A-Za-z0-9_-]`)
   - `WEBAPP_URL=https://<сервис>.onrender.com/webapp`
   - `BOT_TOKEN`, `ADMIN_IDS`, `DATABASE_URL`, `PAYMENT_PROVIDER_TOKEN`
4. Health check: `/health`.

При старте бот вызывает `setWebhook` на `{WEBHOOK_BASE_URL}/webhook/{WEBHOOK_SECRET}` с `drop_pending_updates=False` и **не снимает** webhook при остановке. Иначе после сна Telegram перестанет будить сервис. Первый ответ после сна может занять около минуты: Telegram повторит запрос.

Дополнительно проверяется заголовок `X-Telegram-Bot-Api-Secret-Token` (тот же `WEBHOOK_SECRET`).

Polling на таком тарифе не подходит: уснувший процесс перестаёт спрашивать Telegram.

Файл `render.yaml` описывает сервис без секретов. Значения токенов задаются в панели хостинга.

## Деплой на Koyeb

Альтернатива Render: соберите `Dockerfile`, передайте `PORT`, те же переменные (`MODE=webhook`, `WEBHOOK_BASE_URL`, `WEBHOOK_SECRET`, `WEBAPP_URL`). Health check: `/health`.

## Календарь на GitHub Pages

Web App можно отдавать и с Pages. В адрес кнопки добавьте API бота:

```
https://<user>.github.io/<repo>/?api=https://<сервис>.onrender.com
```

Чтение услуг и слотов открыто (`GET`, CORS `*`). Создать запись через этот API нельзя: бронь появляется только из сообщения, которое Telegram доставляет боту.

## Переменные

| Переменная | Зачем |
| --- | --- |
| `BOT_TOKEN` | токен BotFather |
| `ADMIN_IDS` | id администраторов через запятую |
| `STUDIO_NAME` | название в приветствии и в календаре |
| `DATABASE_URL` | PostgreSQL, лучше Session pooler Supabase |
| `DB_SSL` | `true` для Supabase, `false` для локального Docker |
| `MODE` | `polling` локально, `webhook` на хостинге |
| `WEBHOOK_BASE_URL` | публичный HTTPS сервиса без пути |
| `WEBHOOK_SECRET` | секрет в пути `/webhook/<secret>` и в заголовке Telegram |
| `WEBAPP_URL` | HTTPS календаря |
| `PAYMENT_PROVIDER_TOKEN` | тестовый токен оплаты |
| `HOST` / `PORT` | слушатель uvicorn; на Render `PORT` задаёт платформа |
| `TIMEZONE` | пояс студии, по умолчанию `Europe/Moscow` |
| `WORK_START` / `WORK_END` | границы дня, конец не включается |
| `SLOT_STEP_MINUTES` | шаг сетки, по умолчанию 30 |
| `SCHEDULE_DAYS` | на сколько дней вперёд строить слоты |
| `HOLD_MINUTES` | сколько держать неоплаченную бронь |

Полный образец — в `.env.example`.

## Гонки за один слот

Запись идёт в одной транзакции: строки слотов блокируются `SELECT … FOR UPDATE` в порядке id. Повторная бронь того же старта упирается в частичный уникальный индекс активных записей. Если два клиента нажали одновременно, второй получает просьбу выбрать другое время. Перед оплатой `pre_checkout_query` проверяет бронь ещё раз. Повторный `successful_payment` не создаёт вторую запись.
