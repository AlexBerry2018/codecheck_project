# CodeCheck — платформа проверки заданий по программированию

Код к документу «CodeCheck: архитектура платформы проверки заданий». Студент отправляет решение на Python,
система проверяет его в изолированном контейнере и возвращает вердикт и рейтинг. Три микросервиса общаются
через Kafka (EDA, transactional outbox), данные лежат в PostgreSQL и MongoDB, кэш и счётчики — в Valkey,
вход — Kong (маршрутизатор + балансировщик + rate limiter).

```
клиент ──► Kong :8000 ──► courses-service  (Flask)   PostgreSQL courses_db
                     └──► submissions-service (FastAPI)  PostgreSQL submissions_db + MongoDB sources + Valkey
                              │  ▲
              submission.created │  │ submission.graded           assignment.updated
                              ▼  │                                 courses ──► grader
                          grader-service (FastAPI + Kafka consumers)  MongoDB run_logs (TTL 90 дней), Docker-песочница
```

## Запуск

Нужны Docker и Docker Compose v2.

```bash
docker compose up --build
```

Первый старт дольше обычного: собираются образы, а grader скачивает образ песочницы `python:3.12-alpine`
(пока он не готов, `grader-service` не помечается healthy: решения при этом принимаются и ждут проверки в Kafka).

Если решения остаются в `queued` (e2e пишет «not graded within 120s»), смотрите `docker compose logs grader-service`.
Строка `sandbox is not ready: cannot pull ...` значит, что с вашей машины недоступен Docker Hub. Скачайте образ сами:
`docker pull python:3.12-alpine`. Если образ уже есть на хосте, grader использует его и без доступа к реестру.

| Что | Адрес |
| --- | --- |
| API (Kong) | http://localhost:8000 |
| Демо-страница (HTML + JS) | http://localhost:8080 |
| Kafka UI (топики, сообщения, lag; только чтение) | http://localhost:8081 (слушает только `127.0.0.1`) |
| courses-service напрямую (без Kong) | http://localhost:8001 |
| submissions-service напрямую, Swagger UI | http://localhost:8002/docs |
| grader-service (`/health/ready`, `/metrics`) | http://localhost:8003 |
| PostgreSQL | `localhost:5432`, базы `courses_db` (`courses`/`courses`) и `submissions_db` (`submissions`/`submissions`), админ `postgres`/`postgres` |
| MongoDB | `mongodb://localhost:27017` (без пароля), база `sources` |
| Valkey | `localhost:6379` (`valkey-cli`, без пароля) |
| Kafka (для клиентов на хосте) | `localhost:29092`; внутри compose брокер остаётся `kafka:9092` |
| Администратор | `admin@codecheck.local` / `admin-change-me` (из `.env.example`) |
| Код для регистрации преподавателя | `teacher-invite` (`TEACHER_INVITE_CODE`) |

Все порты, кроме Kong (8000) и демо-страницы (8080), опубликованы только на `127.0.0.1`: базы без паролей не видны
из сети. Если порт на вашей машине занят (например, свой PostgreSQL на 5432), задайте другой в `.env`:
`POSTGRES_PORT`, `MONGO_PORT`, `VALKEY_PORT`, `KAFKA_PORT`, `COURSES_PORT`, `SUBMISSIONS_PORT`, `GRADER_PORT`.
Прямые порты сервисов мешают `--scale`: для масштабирования уберите секцию `ports` у нужного сервиса, вход идёт через Kong.

## Как проверить

```bash
python scripts/e2e.py                 # сквозной сценарий UC-01…UC-06, идемпотентность, перепроверка
python scripts/e2e.py --rate-limit    # + проверка HTTP 429 (расходует квоту отправок)
```

Скрипт использует только стандартную библиотеку. Kong пускает 5 запросов в минуту на `/api/auth` с одного IP,
поэтому при повторном запуске скрипт сам ждёт `Retry-After`.

Postman: импортируйте `postman/codecheck.postman_collection.json` и выполняйте запросы по порядку, токены и id
сохраняются в переменные коллекции.

Вручную (curl):

```bash
B=http://localhost:8000
curl -s $B/api/auth/register -H 'Content-Type: application/json' \
     -d '{"email":"s@example.com","password":"password123"}'
TOKEN=$(curl -s $B/api/auth/login -H 'Content-Type: application/json' \
     -d '{"email":"s@example.com","password":"password123"}' | python -c 'import sys,json;print(json.load(sys.stdin)["access_token"])')
curl -s $B/api/submissions -H "Authorization: Bearer $TOKEN" -H "Idempotency-Key: demo-1" \
     -F assignment_id=1 -F code='print(sum(map(int, input().split())))'
curl -s $B/api/submissions/1 -H "Authorization: Bearer $TOKEN"
```

## Инструменты разработки

- **Kafka UI** (`provectuslabs/kafka-ui`) в compose: топики `submission.created`, `.retry`, `.dlq`, `submission.graded`,
  группы консьюмеров и lag. Удобно показывать на защите, как событие проходит путь от отправки до вердикта. UI в режиме
  «только чтение» и без логина, поэтому порт опубликован только на loopback.
- **pre-commit** (`.pre-commit-config.yaml`): ruff, hadolint, проверка YAML/JSON, поиск приватных ключей.
  `pip install pre-commit && pre-commit install`, один раз по всему репозиторию: `pre-commit run --all-files`
  (хук `hadolint-docker` требует Docker; версии хуков обновляет `pre-commit autoupdate`).
- **GitHub Actions** (`.github/workflows/ci.yml`): `ruff` и `hadolint`, `docker compose config`, `pytest` по трём сервисам
  (матрица), затем сборка образов. Полный прогон стека с `scripts/e2e.py` запускается вручную: Actions → ci → Run workflow
  → `e2e`.
- Правила `ruff` лежат в `ruff.toml` (ошибки, а не стиль); у каждого сервиса есть `.dockerignore`.

## Структура

| Путь | Содержимое |
| --- | --- |
| `courses-service/` | Flask: регистрация и JWT, курсы, задания и тесты, `/internal/*` для других сервисов, outbox → `assignment.updated` |
| `submissions-service/` | FastAPI: приём решений (`Idempotency-Key`, лимит на пользователя), статусы, рейтинг, перепроверка, consumer `submission.graded`, outbox → `submission.created` |
| `grader-service/` | FastAPI + потоки-consumer: проверка в песочнице, вердикт, логи в MongoDB, retry-топик и DLQ |
| `kong/kong.yml` | Маршруты, upstream-балансировка с health checks, `jwt`, `rate-limiting` (Valkey), `cors`, `correlation-id` |
| `postgres/init.sql` | Две базы и по роли на сервис |
| `frontend/index.html` | Демо-страница без сборки; баллы начисляются только за бэкенд |
| `scripts/e2e.py`, `postman/` | Приёмочные проверки |

## API

| Метод и путь | Кто | Описание |
| --- | --- | --- |
| `POST /api/auth/register`, `/login`, `/refresh` | все | Регистрация (teacher — по коду), вход, обновление токена |
| `GET/POST /api/courses`, `POST /api/courses/{id}/enroll` | все / teacher / student | Курсы |
| `GET/POST /api/courses/{id}/assignments`, `GET/PUT /api/assignments/{id}` | по роли | Задания; студент видит только открытые тесты; изменение тестов повышает `tests_version` |
| `POST /api/submissions` | любой | `multipart/form-data` или форма: `assignment_id` + `file` или `code`; заголовок `Idempotency-Key`; ответ **202** |
| `GET /api/submissions/{id}` | владелец, teacher, admin | Статус `queued` или вердикт, результат по тестам (у скрытых только вердикт) |
| `GET /api/submissions?assignment_id=&user_id=` | студент: свои, teacher: любые | История попыток |
| `POST /api/submissions/assignments/{id}/regrade` | владелец курса, admin | Перепроверка решений, проверенных на старых тестах |
| `GET /api/leaderboard/{assignment_id}` | любой | Топ-20 и место текущего пользователя |

Схема ошибок везде одна: `{"error": {"code": "...", "message": "..."}}`. FastAPI-сервис отдаёт OpenAPI на
`/docs` внутри сети compose (Kong этот путь не публикует).

## Тесты

```bash
cd courses-service     && pip install -r requirements-dev.txt && pytest
cd submissions-service && pip install -r requirements-dev.txt && pytest
cd grader-service      && pip install -r requirements-dev.txt && pytest
```

Брокер и базы для тестов не нужны: SQLite в памяти, `MemoryCache` вместо Valkey, `MemorySources` вместо MongoDB,
фальшивый Docker для проверки аргументов песочницы, реальный подпроцесс для локального исполнителя.
Тесты, которым нужны SQLAlchemy и FastAPI, пропускаются (`skip`), если пакеты не установлены.

Что покрыто: валидация и JWT, весь HTTP API обоих сервисов, outbox (публикация по порядку, частичный сбой),
идемпотентность по ключу и по повторной доставке события, устаревшие версии тестов, перепроверка, рейтинг с
перестройкой после потери кэша, лимит на пользователя, вердикты и retry/DLQ в grader.

## Масштабирование и отказы

```bash
docker compose up -d --scale submissions-service=3 --scale grader-service=2
```

- Kong резолвит имя сервиса в несколько адресов и балансирует между ними (`least-connections` для
  `submissions-service`), неисправную реплику выводят активные и пассивные health checks.
- Параллелизм проверки ограничен числом партиций `submission.created` (6) и `MAX_PARALLEL` на реплику.
- **Остановить grader** (`docker compose stop grader-service`): приём решений продолжается (202), сообщения ждут в
  Kafka; после запуска все решения проверяются, дублей нет (NFR-4, NFR-5).
- **Остановить Valkey**: сервисы работают без кэша, лимиты и идемпотентность по кэшу пропускаются (fail-open),
  истиной остаются PostgreSQL и `courses-service`.
- **Остановить courses-service**: приём решений идёт по закэшированной карточке задания (60 с), затем 503.

## Конфигурация

Значения по умолчанию подходят для демонстрации; их можно переопределить в `.env` (см. `.env.example`).
`JWT_SECRET` нужно синхронно поменять в `kong/kong.yml`: декларативный конфиг Kong не читает переменные окружения.
