# Развёртывание на Linux Docker

Контейнер работает без VNC, desktop UI, видимого окна и открытых портов. По умолчанию используется Playwright с headless Chromium. Необязательный SeleniumBase UC запускает полный Chrome на внутреннем виртуальном экране Xvfb. Локальный вход и первоначальный выбор резюме описаны в [SETUP_RU.md](SETUP_RU.md).

## Подготовить сервер

Нужны Docker Engine и Docker Compose 2.24+ — файл Compose использует необязательный `env_file.required`. Клонируйте репозиторий или передайте готовый архив на свой сервер по SSH/SFTP.

```bash
git clone https://github.com/jaFuart/hh-auto-response.git
cd hh-auto-response
mkdir -p hh-data/data
```

Если переносите архив `hh-bot-server.tar.gz`, вместо клонирования распакуйте его: `tar -xzf hh-bot-server.tar.gz`, затем `cd hh-bot`.

Перенесите в `hh-data/` конфиг `config.server.toml`, созданный и заполненный `.env` и каталог `data/`. В `data/` нужны экспортированная сессия `hh-session.json`, выбранный профиль/карта кейсов и вся существующая история. Старый вариант с `.env` в корне проекта тоже поддерживается. Каталоги `browser-profile*`, `.venv`, `.git` и `.tmp` с локального компьютера не копируйте. При первом открытии Linux-профиля импортируется переносимая сессия.

Если инициализация ещё не выполнялась, её можно сделать прямо в Docker до появления конфига:

```bash
docker compose -f docker/docker-compose.yml build hh
docker compose -f docker/docker-compose.yml run --rm hh configure init --gender male --model YOUR_MODEL
```

Docker создаёт файлы от root с правами `0600`. Если конфиг и ключ заполняет обычный Linux-пользователь, после инициализации выполните из корня проекта `sudo chown -R "$(id -u):$(id -g)" hh-data`.

Собственные входные файлы положите, например, в `hh-data/inbox/` и при первой инициализации добавьте `--profile /data/inbox/profile.json --competency-map /data/inbox/cases.md`. Указывайте свои факты; публичные примеры синтетические. Команда не выполняет вход и не включает отправки. Ключ заполните вручную в `hh-data/.env`. После этого перенесите локально экспортированную сессию и выберите резюме.

```bash
chmod 700 hh-data hh-data/data
chmod 600 hh-data/.env hh-data/config.server.toml
find hh-data/data -maxdepth 1 -type f -exec chmod 600 {} +
docker compose -f docker/docker-compose.yml config --quiet
docker compose -f docker/docker-compose.yml build hh
```

`config --quiet` проверяет Compose без вывода значений окружения. Папка `hh-data/` подключается в контейнер как `/data`, программа читает `/data/.env`; при использовании старого корневого `.env` Compose передаёт его как окружение, ограничьте права и на него. Относительные пути профиля отсчитываются от `/data/config.server.toml`.

## Выбрать браузерный движок

Общий Docker-образ содержит Playwright Chromium, полный Chrome for Testing, SeleniumBase UC, совместимый драйвер и Xvfb. Команда сборки одинакова для обоих движков. По умолчанию в `[browser]` стоит `backend = "playwright"`; Xvfb при этом не запускается.

Для UC остановите сервис и измените только эти поля существующего `hh-data/config.server.toml`:

```toml
[browser]
backend = "seleniumbase_uc"
headless = true
channel = ""
```

В UC на Linux `headless = true` означает обычный полный Chrome внутри Xvfb, без окна на вашем компьютере и без VNC. Пустой `channel` или `"chrome"` используют установленный Chrome; `"msedge"` для этого движка не поддерживается. Невидимый UC-режим на Windows не поддерживается: локальную подготовку выполняйте с `backend = "playwright"`.

При первой инициализации можно сразу выбрать UC:

```bash
docker compose -f docker/docker-compose.yml run --rm hh configure init --browser-backend seleniumbase_uc --gender male --model YOUR_MODEL
```

Существующий конфиг не пересоздавайте через `init --overwrite` ради переключения движка: этот флаг сбрасывает остальные настройки к шаблону. Выбранный ID резюме, карта компетенций, фильтры, ключи и история остаются прежними при правке одного поля. UC использует отдельный подкаталог браузерного профиля; на первом запуске в него импортируется `data/hh-session.json`. Не копируйте профиль Chrome между ОС. Для явно обновлённой сессии используйте `session check --restore-session` при остановленном сервисе.

После переключения выполните проверки из следующего раздела. `configure doctor` покажет движок без запуска браузера; `session check` проверит фактический запуск и вход без откликов. При отсутствии Chrome, SeleniumBase или Xvfb UC завершится с диагностикой. UC не гарантирует отсутствие CAPTCHA, блокировок или повторной авторизации. Для возврата достаточно `backend = "playwright"` и повторной проверки сессии.

## Проверить перед расписанием

При этих проверках постоянный контейнер должен быть остановлен:

```bash
docker compose -f docker/docker-compose.yml stop hh
docker compose -f docker/docker-compose.yml run --rm hh configure doctor
docker compose -f docker/docker-compose.yml run --rm hh server --status
docker compose -f docker/docker-compose.yml run --rm hh session check
docker compose -f docker/docker-compose.yml run --rm hh resumes list
docker compose -f docker/docker-compose.yml run --rm hh resumes fetch
docker compose -f docker/docker-compose.yml run --rm hh responses --dry-run --limit 3
```

`configure doctor` только читает явный конфиг, его `.env`, настроенные файлы профиля и локальные файлы сессии/кэша. Показывает движок и настроенную политику интервалов, но не наличие исполняемых файлов, актуальность входа или доступность модели в сети. `--status` не открывает браузер и не обращается к модели. `session check`, `resumes list/fetch` не отправляют отклики и сообщения. `responses --dry-run` показывает выдачу и причины пропуска. В статусе проверьте `headless: true`, часовой пояс, расписание, интервал роботов и действующие сроки пауз.

Если нужно поменять выбранное резюме:

```bash
docker compose -f docker/docker-compose.yml run --rm hh resumes select --id RESUME_ID
docker compose -f docker/docker-compose.yml run --rm hh resumes fetch
```

## Включить автоматическую работу

В `hh-data/config.server.toml` задайте свои часы и дни. Пример:

```toml
[schedule]
enabled = true
times = ["10:00"]
days = ["1", "2", "3", "4", "5", "6", "7"]

[watch]
robot_replies = true
robot_every_minutes = 15
robot_max_chats_per_check = 10
robot_reply_interval_seconds = 30
```

Для автоматических ответов нужны также `[ai] enabled = true`, `only_robot = true` и `[chats] mode = "all"`. Модель и личный профиль описаны в [docs/CONFIGURATION.md](docs/CONFIGURATION.md).

```bash
docker compose -f docker/docker-compose.yml up -d hh
docker compose -f docker/docker-compose.yml logs -f --tail 100 hh
```

Ежедневные отклики выполняются по `Europe/Moscow`. Окно начала догоняющего запуска по умолчанию — 60 минут: задача на 10:00 может впервые начаться после освобождения профиля до 11:00. При более позднем включении новый слот ждёт следующий день. Уже начатая и явно отложенная задача продолжается по сохранённому сроку, в том числе после 11:00 или перезапуска, но только в пределах той же московской даты. Слот, оборвавшийся или завершившийся ошибкой, автоматически заново не запускается.

Роботы проверяются с интервалом после завершения прошлой проверки. Учитываются прочитанные и непрочитанные чаты с последним сообщением за последние 30 дней; текущий участник должен быть подтверждён как робот hh. За цикл открывается не больше 10 чатов: до 40% мест получают новые, непрочитанные или изменившиеся чаты, остальные идут по сохраняемой очереди FIFO. Большая очередь занимает несколько циклов, поэтому отдельному чату не гарантируется ответ за 15 минут. Ручной ответ пользователя и передача разговора человеку прекращают автоответ для этого сообщения.

Технические повторы откликов выполняются отдельно, последовательно с остальными задачами, только при включённом расписании и для текущего резюме. Не более трёх попыток. Неясная доставка сначала проверяется на hh: отсутствие успеха само по себе не разрешает повтор. После трёх неясных проверок повтор прекращается. Ограничения вакансии и выбранный ID резюме проверяются снова перед адресной отправкой.

## Темп работы и паузы

В публичных примерах между попытками отправки откликов проходит минимум 180 секунд. После серии из 2 попыток действует пауза 25 минут, а чтения страниц разделены интервалом минимум 10 секунд. Ошибка отправки не возвращает место в серии. Дневная квота 10 по дате `Europe/Moscow` общая для автоматических задач, ручных CLI-запусков и повторов, если они используют одну историю программы. В ней учитываются подтверждённые отклики и сегодняшние попытки с ещё неясной доставкой. Самостоятельные отклики на сайте в этот счётчик не входят.

На обычной паузе откликов сервер закрывает браузер и сохраняет срок продолжения; в это время проверка роботов продолжает работать. Ответы роботам разделены интервалом минимум 30 секунд. После обнаружения CAPTCHA действует отдельная общая пауза 120 минут: до её окончания фоновые отклики, чтение чатов и технические повторы не обращаются к hh. Первый обнаруженный срок сохраняется между перезапусками.

Поля находятся в `[limits]` и `[watch]`; полный пример — в [CONFIGURATION.md](docs/CONFIGURATION.md#интервалы-и-ограничения-запросов). `configure doctor` показывает настроенную политику, а `server --status` — фактическую причину паузы и срок продолжения. Не удаляйте состояние ради ускорения очереди. Ограничения снижают частоту запросов, но не гарантируют отсутствие CAPTCHA или блокировок.

## Ручные команды

Остановите сервис перед командами, использующими браузер:

```bash
docker compose -f docker/docker-compose.yml stop hh

# Реально отправляет до трёх откликов.
docker compose -f docker/docker-compose.yml run --rm hh responses --limit 3

# Может отправлять ответы роботам при включённом ai.only_robot.
docker compose -f docker/docker-compose.yml run --rm hh chats

# Запускает только уже подошедшие автоматические задачи; тоже может отправлять.
docker compose -f docker/docker-compose.yml run --rm hh server --once

docker compose -f docker/docker-compose.yml up -d hh
```

Один браузерный профиль нельзя одновременно открывать в двух контейнерах или процессах. Сервер держит межпроцессную блокировку планировщика; ручные CLI-команды всё равно запускайте после остановки сервиса.

## Повторный вход и капча

При CAPTCHA сервер записывает причину и общий срок паузы: с публичными настройками все фоновые запросы к hh прекращаются на 120 минут. Немедленного повтора отправки и ожидания ручного решения нет. UC не включает автоматическое решение CAPTCHA; контейнер не запускает VNC. Срок и причину проверяйте через `server --status`, который не обращается к hh.

Истёкшая сессия требует отдельного повторного входа. В этом случае:

1. Остановите сервис: `docker compose -f docker/docker-compose.yml stop hh`.
2. На локальном компьютере выполните `python scripts/session.py login --config hh-data/config.server.toml`. Для локальной подготовки на Windows используйте конфиг с `[browser] backend = "playwright"`; серверный UC-конфиг менять не нужно.
3. Перенесите обновлённый `hh-data/data/hh-session.json` по SSH/SFTP.
4. Явно восстановите сессию в существующий Linux-профиль и проверьте вход:

```bash
docker compose -f docker/docker-compose.yml run --rm hh session check --restore-session
docker compose -f docker/docker-compose.yml up -d hh
```

hh может не принять перенос входа на другом IP; в таком случае повторная проверка завершится ошибкой, автоматические отправки не начнутся.

## Обновление и резервная копия

Сначала остановите сервис и сохраните отдельную приватную копию `.env` и `hh-data/`. Затем обновите исходники из своего репозитория:

```bash
docker compose -f docker/docker-compose.yml stop hh
git pull --ff-only
docker compose -f docker/docker-compose.yml build hh
docker compose -f docker/docker-compose.yml run --rm hh server --status
docker compose -f docker/docker-compose.yml up -d hh
```

При обновлении сохраняйте `data/state.json`, `data/responses.jsonl`, `data/chat_answers.jsonl`, `data/application-delivery.json`, `data/server-state.json`, `data/submission-pacing.json` и `data/robot-chat-scan.json`. Последние два файла сохраняют сроки ограничений и очередь обхода чатов. Пересоздание контейнера не удаляет подключённый каталог `hh-data/`. Логи и Excel-отчёты находятся в `hh-data/logs/`; они могут содержать личные данные.

`restart: unless-stopped` возвращает сервис после перезагрузки Docker/сервера, если вы не остановили его вручную. Остановка: `docker compose -f docker/docker-compose.yml stop hh`.
