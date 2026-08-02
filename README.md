# lLLM — AI Lab

Экспериментальная песочница для проверки идей и наработок по AI. Текущий этап —
минимальный CLI-чат, обращающийся к локально развёрнутой модели через
[Ollama](https://ollama.com). В планах — вырастить из этого полноценный harness
с вызовом инструментов (tools) вокруг локальной модели.

## Требования

- Python 3.12+
- [Ollama](https://ollama.com), запущенный локально
- Скачанная модель (по умолчанию `qwen3:8b`)

## Установка

```bash
python -m venv venv
source venv/bin/activate
pip install -r requirements.txt
```

Установить и запустить модель в Ollama:

```bash
ollama pull qwen3:8b
```

## Запуск

Убедись, что Ollama слушает на `http://localhost:11434`, затем:

```bash
python app.py
```

Введи сообщение и получи ответ модели. Ответ печатается инкрементально — по мере
генерации моделью, а не целиком в конце. История диалога сохраняется на диск
(`data/chat_history.json`) и переживает перезапуск приложения. Команды:

- `/reset` — очистить историю диалога (файл истории перезаписывается пустым)
- `/bye` — выйти

Модели отправляется не вся сохранённая история, а системный промпт и последние
`MAX_CONTEXT_MESSAGES` сообщений. Системный промпт в JSON не хранится.

### Инструмент `python_calculate`

Модели доступен первый исполняемый инструмент — `python_calculate`. Для
арифметических вопросов модель сама решает вызвать его, передаёт выражение,
harness исполняет его **локально в том же процессе и venv**, что и `python
app.py`, и возвращает результат модели для финального ответа. Вызов виден в CLI:

```text
You: What is 173 multiplied by 284?

[tool 1/4] python_calculate
[args] {"expression": "173 * 284"}
[result] {"ok": true, "result": 49132}

Qwen: The result of 173 multiplied by 284 is 49,132.
```

Это **ограниченный калькулятор, а не произвольный Python**: выражение разбирается
в AST и исполняется по allowlist (числа, арифметика, список разрешённых функций
вроде `sqrt`, `round`, `min`, `max`, `sum`, `len`; константы `pi`, `e`). Никаких
`eval`/`exec`, импортов, доступа к файлам, атрибутам или сети. Если инструмент не
нужен, модель отвечает как обычно, без блока `[tool …]`.

### Инструмент `sql_query`

Второй исполняемый инструмент — `sql_query` — даёт модели **только чтение** из
локальной SQLite-базы [Chinook](https://github.com/lerocha/chinook-database)
(демонстрационный музыкальный магазин: артисты, альбомы, треки, клиенты,
сотрудники, счета). Модель получает схему базы в системном промпте, генерирует
**один** `SELECT` (можно с `WITH`) на вызов, harness исполняет его локально через
стандартный модуль `sqlite3` и возвращает строки модели. За ход модель может
сделать несколько запросов подряд (см. «Агентный цикл») — например, исправить
ошибочный SQL по возвращённой ошибке.

База не хранится в git — её нужно один раз собрать из доверенного seed-скрипта
`data/seed/Chinook_Sqlite.sql`:

```bash
python scripts/init_database.py          # создаёт data/chinook.sqlite
python scripts/init_database.py --force   # пересоздать существующую базу
```

Если база не инициализирована, инструмент вернёт ошибку
`database_not_initialized` — запусти команду выше. Внешний сервер БД не нужен,
сеть не используется. Пример вызова в CLI:

```text
You: Which five genres generated the most revenue?

[tool 1/4] sql_query
[args] {"query": "SELECT g.Name, SUM(il.UnitPrice * il.Quantity) AS Revenue FROM InvoiceLine il JOIN Track t ON il.TrackId = t.TrackId JOIN Genre g ON t.GenreId = g.GenreId GROUP BY g.Name ORDER BY Revenue DESC LIMIT 5"}
[result] {"ok": true, "columns": ["Name", "Revenue"], "rows": [["Rock", 826.65], ...], "row_count": 5, "truncated": false}

Qwen: The five genres that generated the most revenue are Rock ($826.65)...
```

Защита выстроена на границе соединения, а не разбором SQL: соединение
открывается в режиме `mode=ro`, SQLite-authorizer отклоняет всё, кроме чтения
(записи, DDL, `ATTACH`, `PRAGMA`, транзакции), исполняется **ровно один**
оператор через `execute` (не `executescript`) на вызов, а число строк, столбцов,
объём результата и работа движка ограничены детерминированными лимитами.
Результат матируется по строкам, если он слишком большой (`truncated: true`).

### Инструмент `get_current_time` (MCP)

Третий инструмент подключается по-другому. Первые два — локальные обработчики в
том же процессе; этот приходит через [Model Context
Protocol](https://modelcontextprotocol.io) — стандартную границу между
host-приложением и внешним поставщиком возможностей. Здесь `lLLM` выступает MCP
**host** и содержит MCP **client**; отдельный локальный процесс
(`mcp_servers/time_server.py`) — MCP **server**, поднимаемый harness'ом как
дочерний процесс и общающийся по **stdio**.

На старте клиент запускает сервер, инициализирует сессию и запрашивает список
инструментов (`tools/list`). Обнаруженный инструмент `get_current_time`
конвертируется в обычный `ToolSpec` и регистрируется в **том же** `ToolRegistry`
рядом с локальными — модель не различает, локальный это инструмент или MCP.
Модель видит его под пространством имён `mcp_time__get_current_time`; вызов
маршрутизируется обратно на сервер через `session.call_tool`, а результат
нормализуется в тот же JSON-конверт. Установка зависимости — обычная:

```bash
pip install -r requirements.txt   # ставит официальный SDK: mcp>=1.27,<2
```

Пример вызова в CLI:

```text
You: What time is it now in UTC?

[tool 1/4] mcp_time__get_current_time
[args] {"timezone": "UTC"}
[result] {"ok": true, "server": "time", "tool": "get_current_time", "data": {"timezone": "UTC", "datetime": "2026-07-23T11:29:29+00:00"}}

Qwen: The current time in UTC is 11:29 on July 23, 2026.
```

Сервер использует только стандартную библиотеку (`datetime` + `zoneinfo`), без
сети и внешних сервисов; неизвестная таймзона даёт контролируемую ошибку
`invalid_timezone`, после которой приложение остаётся рабочим. `stdout` дочернего
процесса зарезервирован под протокол, диагностика идёт в `stderr`. Обнаружение
инструментов **fail-fast**: если сервер не удаётся запустить, инициализировать
или опросить, приложение падает до входа в чат с понятным сообщением
(`MCP startup failed for server 'time': ...`) и не оставляет дочерних процессов.
Сессия и дочерний процесс закрываются детерминированно на `/bye`, EOF и `Ctrl+C`.

### Yandex Tracker MCP — только чтение (SPEC-013)

`time` — локальный, доверенный, демонстрационный сервер. Следующий шаг —
подключение к **настоящему** внешнему бизнес-сервису:
[`aikts/yandex-tracker-mcp`](https://github.com/aikts/yandex-tracker-mcp),
который отдаёт по протоколу MCP широкий набор операций над Yandex Tracker,
включая запись (создание/изменение задач, комментарии, переходы статусов и
т.д.). Внешний сервер здесь трактуется как **недоверенный поставщик
возможностей**: его каталог инструментов уменьшается host-политикой ещё до
того, как что-либо становится видимым модели.

Что доступно — ровно четыре инструмента, только чтение:

```text
mcp_tracker__issue_get              — прочитать одну задачу
mcp_tracker__issues_find            — найти задачи по запросу (YTQL)
mcp_tracker__queue_get_metadata     — прочитать метаданные очереди
mcp_tracker__issue_get_comments     — прочитать комментарии к задаче
```

Всё остальное, что отдаёт `tools/list` upstream-сервера (создание/изменение
задач, комментариев, переходов, ссылок, worklog'ов и т.д.), **отфильтровывается
до регистрации** — точным allowlist'ом по имени, а не эвристикой по описанию
или префиксу. Отфильтрованный инструмент никогда не получает `ToolSpec`,
обработчик исполнителя или объявление модели — его нельзя вызвать даже прямым
запросом текста от модели.

**Требования** (только если интеграция включена):

- установленный [`uv`/`uvx`](https://docs.astral.sh/uv/) — harness его не
  ставит и не трогает системные инструменты;
- токен Tracker с правом чтения (`TRACKER_TOKEN`);
- ровно один идентификатор организации: `TRACKER_ORG_ID` (Yandex 360) или
  `TRACKER_CLOUD_ORG_ID` (Yandex Cloud) — не оба сразу.

Пакет закреплён на точную протестированную версию, а SDK внутри дочернего
uvx-окружения — на тот же диапазон, что и у самого проекта (PATCH-013-01):

```text
yandex-tracker-mcp==0.7.2
mcp>=1.27,<2
```

Одного пина пакета оказалось мало: сам `yandex-tracker-mcp==0.7.2` объявляет
`mcp[cli]>=1.21` без верхней границы, а `uvx` разрешает зависимости дочернего
окружения независимо от проекта. Когда вышел мажорный релиз MCP SDK 2.0, он
попал в дочернее окружение и уронил сервер на старте (`ImportError: cannot
import name 'FastMCP' from 'mcp.server'`). Внешняя интеграция воспроизводима
только тогда, когда ограничены и транзитивные зависимости.

Переменные окружения (интеграция выключена по умолчанию — свежий чекаут не
требует токена). Через shell:

```bash
export TRACKER_MCP_ENABLED=true
export TRACKER_TOKEN="..."
export TRACKER_ORG_ID="..."       # или TRACKER_CLOUD_ORG_ID
python app.py
```

Либо через `.env` в корне проекта (см. `.env.example`) — `app.py` подхватывает
его автоматически маленьким loader'ом без зависимости от `python-dotenv`
(`load_dotenv_if_present`); значение, уже экспортированное в реальном
окружении, всегда имеет приоритет над файлом. `.env` в `.gitignore`, в
репозиторий никогда не попадает.

Секреты читаются только из окружения процесса `mcp_integration/config.py`;
нигде в закоммитированных файлах, трассировке или истории чата токен и
идентификатор организации не появляются.

Старт при включённой интеграции:

```text
[mcp] connected: time (1 tool)
[mcp] connected: tracker (4 admitted, 38 filtered)
```

Старт при выключенной (значение по умолчанию):

```text
[mcp] connected: time (1 tool)
[mcp] tracker: disabled
```

Если интеграция включена, но сконфигурирована неверно — старт **падает** до
входа в чат, без утечки секретов в сообщении:

```text
Application startup failed: Tracker MCP is enabled but TRACKER_TOKEN is missing.
```

Пример работы через навык `tracker_read` (`skills/tracker_read/`; ровно те же
четыре инструмента в `allowed_tools`, никаких локальных инструментов):

```text
You: Use the tracker_read skill and show me issue DATA-142.
[skill] tracker_read

[tool 1/4] mcp_tracker__issue_get
[args] {"issue_id": "DATA-142", "include_description": true}
[result] {"ok": true, "server": "tracker", "tool": "issue_get", "data": {...}}

Qwen: DATA-142 — Add ownership metadata to the reporting mart. Status: In Progress.
```

Попытка изменить что-либо в Tracker получает отказ без исполнения:

```text
You: Add a comment to DATA-142 saying that the fix is deployed.
Qwen: This integration is read-only. I can read the issue and its comments,
but I cannot add or change anything in Yandex Tracker.
```

Комментарии и описания задач — недоверенный текст: навык явно инструктирован
не выполнять инструкции, встреченные внутри содержимого Tracker. Крупный
результат ограничивается универсальной (не специфичной для Tracker) проверкой
размера в `mcp_integration/adapter.py` (`MCP_RESULT_MAX_CHARS`) — ответ
помечается `truncated: true`, а не молча обрезается там, где это не заметно.

**Диагностика:**

| Проблема | Поведение |
| --- | --- |
| `uvx` не установлен | `MCP startup failed for server 'tracker': The MCP server could not be started.` |
| `TRACKER_TOKEN` не задан | `Tracker MCP is enabled but TRACKER_TOKEN is missing.` |
| Не задан ни один идентификатор организации | `Tracker MCP is enabled but no organisation ID is set...` |
| Заданы оба идентификатора организации | `Tracker MCP requires exactly one organisation ID...` |
| Аутентификация/права отклонены Tracker'ом | безопасное сообщение в финальном ответе хода, без токена и заголовков |
| Тайм-аут вызова | существующий тайм-аут инструмента/хода (SPEC-011), приложение остаётся рабочим |

Опциональный live-smoke прогон (требует реальный токен, `uvx` и безопасные
тестовые идентификаторы — не выполняется по умолчанию и не входит в CI):

```bash
export TRACKER_SMOKE_ISSUE_ID="TEST-1"
export TRACKER_SMOKE_QUEUE_ID="TEST"
export TRACKER_SMOKE_SEARCH_QUERY="Queue: TEST"
python -m evals.runner --suite live --category tracker
```

## Агентный цикл

За один ход модель может выполнить **несколько** инструментов подряд. Ход — это
ограниченный цикл: harness отправляет модели диалог и декларации инструментов,
модель либо отвечает текстом (ход завершён), либо запрашивает **ровно один**
инструмент; harness исполняет его через тот же `ToolExecutor`, дописывает вызов и
структурированный результат во **временный** транскрипт хода и снова отдаёт
управление модели. Так модель видит каждое наблюдение своего хода и может, глядя
на результат, вызвать другой инструмент, повторить тот же с исправленными
аргументами или дать финальный ответ.

Цикл детерминированно ограничен: `MAX_TOOL_CALLS_PER_TURN = 4` (в `config.py`) —
это лимит хоста, модель его не видит и не меняет. Счётчик считает **исполненные**
инструменты; запрос сверх лимита **не исполняется**, ход завершается понятной
ошибкой, а незавершённый пользовательский ход откатывается (частичный ответ не
сохраняется). Каждый вызов показан в CLI как `[tool N/4]`. Структурированная
ошибка инструмента (`{"ok": false, ...}`) — это **нормальное наблюдение**: цикл
продолжается, и модель может исправиться; harness не пытается чинить SQL сам.

```text
You: Which music genre generated the most revenue, and what percentage of all revenue did it generate?

[tool 1/4] sql_query
[args] {"query": "SELECT g.Name AS GenreName, SUM(il.UnitPrice * il.Quantity) AS TotalRevenue, (SUM(il.UnitPrice * il.Quantity) / (SELECT SUM(UnitPrice * Quantity) FROM InvoiceLine JOIN Track ON ...)) * 100 AS Percentage FROM ... LIMIT 1"}
[result] {"ok": false, "error": {"type": "invalid_query", "message": "The SQL query is invalid."}}

[tool 2/4] sql_query
[args] {"query": "SELECT g.Name AS GenreName, SUM(il.UnitPrice * il.Quantity) AS TotalRevenue, (SUM(il.UnitPrice * il.Quantity) / (SELECT SUM(UnitPrice * Quantity) FROM InvoiceLine) * 100) AS Percentage FROM InvoiceLine il JOIN Track t ON il.TrackId = t.TrackId JOIN Genre g ON t.GenreId = g.GenreId GROUP BY g.GenreId ORDER BY TotalRevenue DESC LIMIT 1"}
[result] {"ok": true, "columns": ["GenreName", "TotalRevenue", "Percentage"], "rows": [["Rock", 826.65, 35.499871167224946]], "row_count": 1, "truncated": false}

Qwen: The genre that generated the most revenue is Rock ($826.65), about 35.5% of all revenue.
```

Протокольные сообщения хода (вызовы и результаты инструментов) — временные: в
`data/chat_history.json` сохраняются только семантические `user`/`assistant`
сообщения, поэтому вывод инструментов не раздувает историю. Стримится только
финальный текстовый ответ; промежуточные шаги видны как блоки `[tool N/4]`.
Компонент цикла — `agent.py` (`AgentRunner`); он владеет только политикой цикла и
не трогает хранение истории, команды CLI, жизненный цикл MCP или реализации
инструментов. Этот шаг — последовательный цикл: **параллельные** вызовы в одном
ответе модели отклоняются (`Parallel tool calls are not supported.`).

## Надёжность и наблюдаемость хода (SPEC-011)

Каждый ход теперь возвращает не голую строку и не одно исключение на все
случаи, а явный `AgentTurnOutcome` (`reliability.py`): `status` (`completed`,
`failed`, `stopped`, `timed_out`, `cancelled`) + `reason` (например,
`final_answer`, `tool_call_limit`, `repeated_tool_call`, `model_timeout`,
`tool_timeout`, `turn_timeout`, `user_interrupt`). Успешный ход — это только
`completed/final_answer`; любой другой исход не сохраняет частичный ответ, а
пользовательское сообщение откатывается. Каждая неуспешная реплика в CLI
сопровождается идентификатором для диагностики:

```text
Application error: Tool 'sql_query' timed out.
Run ID: 6a1e3e3e-...
```

**Дедлайны.** У каждого хода теперь три host-owned предела (`config.py`):
модель на один запрос, инструмент на одно исполнение, и весь ход целиком.
Дедлайны — это предел ожидания со стороны вызывающего кода
(`reliability.run_with_deadline`), а не гарантированная отмена: Python не может
безопасно прервать произвольный выполняющийся код, поэтому «зависший» рабочий
поток не завершается принудительно, а просто перестаёт учитываться. Общий
дедлайн хода авторитетен: если время хода исчерпано, следующая операция
(запрос модели или вызов инструмента) не начинается вовсе.

```python
MODEL_REQUEST_TIMEOUT_SECONDS = 120
TOOL_EXECUTION_TIMEOUT_SECONDS = 30
AGENT_TURN_TIMEOUT_SECONDS = 180
```

**Обнаружение повторов.** Если модель запрашивает один и тот же инструмент с
структурно идентичными аргументами (канонический JSON, порядок ключей не
важен) больше `MAX_IDENTICAL_TOOL_CALLS` раз подряд, третий такой запрос не
исполняется, и ход завершается с `repeated_tool_call` — раньше, чем сработал
бы общий лимит `MAX_TOOL_CALLS_PER_TURN`.

**Трассировка.** Каждый запуск `python app.py` пишет структурированный
локальный след в свой собственный файл, `data/traces/agent-<run_id>.jsonl`
(PATCH-011-01) — по одной JSON-строке на событие (`turn_started`,
`model_request_started`/`model_response_finished`, `tool_call_requested`,
`tool_execution_started`/`tool_execution_finished`, `policy_violation`,
`turn_finished`, ...), с версией схемы, временем UTC, `run_id`/`turn_id` и
длительностями. Большие или чувствительные данные не копируются целиком —
аргументы инструментов и текст ответа обрезаются
(`TRACE_PAYLOAD_PREVIEW_CHARS`), строки результатов SQL и сырые кадры MCP в
след не попадают. Файлы — локальные, добавление-only, в git не хранятся:

```bash
tail -n 20 data/traces/agent-<run_id>.jsonl
# или проще — последний по времени изменения файл:
tail -n 20 "$(ls -t data/traces/agent-*.jsonl | head -1)"
```

Если запись следа не удалась, это не ломает ход и не подменяет его реальный
исход — приложение один раз предупреждает (`Warning: trace output is
unavailable for this run.`) и продолжает работать.

### Тесты

Комплект `tests/` — детерминированный и не требует запущенного Ollama, сети
или живого MCP-сервера: модель, инструменты и время подменяются скриптованными
фикстурами (`tests/support.py`).

```bash
pip install -r requirements-dev.txt
pytest
```

### Оценки (evals)

Тесты проверяют, что harness соблюдает свои же политики; оценки — что
собранный агент решает представительные задачи. Комплект `evals/cases.json`
покрывает девять базовых категорий (без инструмента, калькулятор, SQL,
восстановление после ошибки SQL, несколько инструментов подряд, MCP-время,
повтор вызова, бюджет вызовов, таймаут) плюс шесть категорий навыков (SPEC-012).

```bash
python -m evals.runner --suite scripted   # без Ollama/MCP, безопасно для CI
python -m evals.runner --suite live       # с реальной моделью и MCP, запускается вручную
```

Результат — версионированный JSON в `data/evals/<timestamp>-<suite>.json`
(в git не хранится; сами кейсы под `evals/` — хранятся). Ненулевой код выхода,
если хоть один применимый кейс не прошёл. Подробнее — `evals/README.md`.

## Навыки (skills) (SPEC-012)

**Инструмент** отвечает на вопрос «как выполнить одну операцию?»; **навык** —
на вопрос «как решить один класс задач?». Навык — это декларативный пакет в
каталоге `skills/` (не исполняемый плагин): инструкция, входной контракт и
список разрешённых инструментов. Навыки лежат *над* инструментами и могут только
*сузить* глобальный набор инструментов — они никогда не расширяют доступ и не
меняют поведение инструмента. Рантайм-код навыков — в пакете `skill_runtime/`,
отдельно от декларативных данных в `skills/`.

**Две фазы.** Чтобы промпт не рос линейно с числом навыков, модель сначала видит
только компактный каталог (имя + описание). Роутер (`SkillRouter`) выбирает ноль
или один навык на ход; полная инструкция `SKILL.md` загружается лениво — только
для выбранного навыка. Явный запрос пользователя (`use the <name> skill`)
выбирает навык напрямую, минуя модель-роутер; никакой нечёткий или неизвестный
матч не подставляется.

```text
Запрос → компактный каталог → SkillRouter
   ├── нет навыка → обычный ход
   └── выбран навык → полная инструкция + разрешённые инструменты
                    → тот же ограниченный, наблюдаемый AgentRunner
```

Роутинг и исполнение делят один `turn_id` и один общий дедлайн хода
(`TurnContext`), так что `duration_ms` и `model_requests` учитывают обе фазы.
Ограничение инструментов защищено с двух сторон: модели отправляются только
разрешённые декларации, а исполнитель (`RestrictedToolExecutor`) отклоняет вызов
вне allowlist ещё до обработчика (`stopped/skill_policy_violation`).

**Проверка на старте — fail-fast.** Все пакеты обнаруживаются и валидируются до
входа в чат: имя по regex и совпадение с именем каталога, обязательные поля и
заголовки, безопасный парсер front matter, структурная проверка
`input.schema.json`, отсутствие небезопасных путей/симлинков, и — каждый
`allowed_tools` должен существовать в реестре инструментов. Иначе:

```text
Application startup failed: Skill 'sales_analysis' references unknown tool 'write_text_file'.
```

Пример пакета — `skills/sales_analysis/` (анализ выручки/продаж; использует
`sql_query` и `python_calculate`, запрещает `mcp_time__get_current_time`):

```text
You: Which music genre generated the most revenue, and what percentage of total revenue did it generate?
[skill] sales_analysis

[tool 1/4] sql_query
[args] {"query": "WITH GenreRevenue AS (...) SELECT ... LIMIT 1;"}
[result] {"ok": true, "rows": [["Rock", 826.65, 35.499...]], ...}

Qwen: The music genre that generated the most revenue is Rock, contributing
$826.65, which accounts for 35.5% of the total revenue. ...
```

Структура пакета:

```text
skills/
└── sales_analysis/
    ├── SKILL.md            # front matter (name/description/version/allowed_tools) + обязательные разделы
    ├── input.schema.json   # входной контракт (подмножество JSON Schema 2020-12)
    ├── examples/           # документация/фикстуры
    └── evals/cases.json    # кейсы, специфичные для навыка
```

Скриптованные оценки навыков — в `evals/cases.json` под категориями
`skill_explicit`, `skill_auto`, `skill_none`, `skill_clarification`,
`skill_policy_violation`, `skill_routing_repair`, а также двенадцать
`tracker_*`-категорий для навыка `tracker_read` (SPEC-013; безопасны для CI,
без модели):

```bash
python -m evals.runner --suite scripted
python -m evals.runner --suite live --category skills    # с реальной моделью (вручную)
python -m evals.runner --suite live --category tracker   # live-smoke Tracker (см. ниже)
```

Когда внешняя интеграция, от которой зависит навык, выключена (Tracker по
умолчанию), сам навык **не загружается** как исполняемый — это не ошибка
загрузчика, а детерминированный host-owned пропуск одного пакета
(`SkillPackageLoader.load_all(..., omit={"tracker_read"})`), при этом
остальные навыки продолжают валидироваться и загружаться как обычно.

## Изолированный sandbox runtime (SPEC-015)

Инфраструктурный слой для запуска **недоверенного** Python- или Bash-скрипта
внутри одноразового Docker-контейнера. Это шаг «только для хоста»: у модели
**пока нет** доступа к песочнице — нет инструмента `sandbox_execute`, нет
навыка, нет строки в промпте. Рабочий интерфейс агента (STEP 16) появится
следующим шагом; здесь строится и проверяется сама граница исполнения.

**Обычный чат Docker не требует.** `python app.py` работает и ведёт себя точно
так же, как раньше: sandbox не импортируется приложением, на старте нет
преflight-проверки Docker, и падение Docker никак не влияет на чат. Docker нужен
только для сборки образа и запуска sandbox-проверок.

Зачем отдельный слой: `python_calculate` — это намеренно ограниченный
AST-калькулятор, а не REPL, а дедлайны SPEC-011 работают только на стороне
вызывающего (`run_with_deadline` бросает поток, но не останавливает его). Для
произвольного кода нужна настоящая граница: здесь по таймауту убивается **весь
контейнер** вместе со всеми потомками процесса.

```bash
python scripts/build_sandbox_image.py   # собрать образ (один раз; нужен интернет)
python scripts/sandbox_smoke.py         # человекочитаемая проверка границы
```

```text
[PASS] python-basic
[PASS] bash-basic
[PASS] python-artifact
...
[PASS] timeout-kills-container
[PASS] cleanup

14/14 passed
```

Что гарантирует политика (все значения host-owned, задание их не меняет):

- поддерживаются ровно два языка — Python и Bash;
- сеть в контейнере выключена (`--network none`);
- корневая ФС смонтирована только для чтения (`--read-only`);
- исходник и входные файлы монтируются read-only, писать можно только в
  ограниченный по размеру контейнерный `tmpfs` — **записываемого bind-mount на
  хост нет вообще**, поэтому задание не может занять диск разработчика;
- скрипт выполняется от фиксированного непривилегированного пользователя
  (`65532:65532`), все capabilities сброшены, `no-new-privileges`;
- Docker-сокет в контейнер не пробрасывается;
- переменные окружения хоста и `.env` не передаются — окружение собирается с
  нуля из шести фиксированных значений;
- жёсткие лимиты памяти, CPU, числа процессов, файловых дескрипторов, времени,
  объёма stdout/stderr и артефактов;
- по таймауту или превышению лимита вывода контейнер убивается целиком;
- контейнер удаляется на любом пути — успех, ошибка, таймаут, прерывание;
- обратно пересекают границу только ограниченные stdout/stderr, код возврата и
  обычные файлы-артефакты, и только после нулевого кода возврата.

Тесты: детерминированные (`tests/test_sandbox_*.py`, кроме `integration`) идут
без Docker на фейковом Docker CLI; живые — по флагу:

```bash
LLLM_SANDBOX_LIVE=1 python -m pytest tests/test_sandbox_integration.py -q
```

Граница честная, но локальная: это **лабораторная песочница для одного
пользователя**, а не публичный многопользовательский сервис исполнения кода.
Она не заявляет формально доказанной изоляции и не защищает от неизвестного
побега из Docker или ядра; сервис не следует выставлять наружу.

## Конфигурация

Настройки хоста, модели и путей к БД — в `config.py`:

```python
OLLAMA_HOST = "http://localhost:11434"
MODEL_NAME = "qwen3:8b"
MAX_CONTEXT_MESSAGES = 20              # сколько последних сообщений уходит модели
CHAT_HISTORY_PATH = "data/chat_history.json"  # где хранится история
MAX_TOOL_CALLS_PER_TURN = 4           # предел исполненных инструментов за ход

# Надёжность агента (SPEC-011) — все значения host-owned, модель их не видит.
MODEL_REQUEST_TIMEOUT_SECONDS = 120
TOOL_EXECUTION_TIMEOUT_SECONDS = 30
AGENT_TURN_TIMEOUT_SECONDS = 180
MAX_IDENTICAL_TOOL_CALLS = 2

# Локальная структурированная трассировка (SPEC-011). Один файл на запуск,
# data/traces/agent-<run_id>.jsonl (PATCH-011-01).
TRACE_ENABLED = True
TRACE_DIR = "data/traces"
TRACE_PAYLOAD_PREVIEW_CHARS = 1000

# Слой навыков (SPEC-012) — все пределы host-owned, валидируются на старте.
SKILLS_ROOT = ...                        # каталог декларативных пакетов навыков
SKILL_ROUTING_TIMEOUT_SECONDS = 30       # свой таймаут роутинга (в бюджете хода)
SKILL_ROUTING_REPAIR_ATTEMPTS = 1        # 1 => не более двух запросов роутинга
MAX_SKILL_ROUTING_RESPONSE_CHARS = 2000
MAX_SKILL_INSTRUCTION_CHARS = 20000
MAX_SKILL_SCHEMA_BYTES = 100000
MAX_SKILLS = 100
MAX_SKILL_DESCRIPTION_CHARS = 200

# Внешний Yandex Tracker MCP-сервер (SPEC-013) — выключен по умолчанию,
# свежий чекаут не требует токена. Секреты (TRACKER_TOKEN, org id) читаются
# из окружения только в mcp_integration/config.py, не отсюда.
TRACKER_MCP_SERVER_ID = "tracker"
TRACKER_MCP_COMMAND = "uvx"
TRACKER_MCP_PACKAGE = "yandex-tracker-mcp==0.7.2"   # закреплённая версия, без @latest
TRACKER_MCP_SDK_REQUIREMENT = "mcp>=1.27,<2"        # граница SDK в дочернем uvx-окружении
TRACKER_MCP_ARGS = ["--from", TRACKER_MCP_PACKAGE, "--with", TRACKER_MCP_SDK_REQUIREMENT, ...]
TRACKER_MCP_REQUIRED_TOOLS = frozenset({
    "issue_get", "issues_find", "queue_get_metadata", "issue_get_comments",
})
TRACKER_MAX_SEARCH_PAGE_SIZE = 50   # ориентир для навыка, не жёсткий лимит аргумента

# Универсальный предел размера результата MCP-вызова (SPEC-013) — для любого
# сервера, не только Tracker: большой ответ помечается truncated=true, а не
# передаётся модели целиком.
MCP_RESULT_MAX_CHARS = 20000

# Изолированный sandbox runtime (SPEC-015) — host-owned политика исполнения.
# Ничего из этого не подключено к старту приложения: runtime создаётся явно
# тестами и scripts/sandbox_smoke.py. Тег резолвится в неизменяемый image ID
# до создания контейнера. execution + cleanup должны укладываться в
# TOOL_EXECUTION_TIMEOUT_SECONDS (10 + 5 < 30), иначе внешний дедлайн сработает
# раньше, чем песочница успеет убить и удалить свой контейнер.
SANDBOX_IMAGE_REF = "lllm-sandbox:spec-015"   # без :latest — тег проверяется
SANDBOX_EXECUTION_TIMEOUT_SECONDS = 10
SANDBOX_CLEANUP_TIMEOUT_SECONDS = 5
SANDBOX_MEMORY_BYTES = 256 * 1024 * 1024
SANDBOX_CPUS = 1.0
SANDBOX_PIDS_LIMIT = 64
SANDBOX_OUTPUT_TMPFS_BYTES = 8 * 1024 * 1024   # единственная запись — tmpfs, не хост
SANDBOX_MAX_STDOUT_BYTES = 100000              # превышение убивает контейнер
SANDBOX_TEMP_ROOT = ...                        # приватный скретч, чистится после задания

CHINOOK_SEED_PATH = ...     # доверенный seed под контролем версий
SQLITE_DATABASE_PATH = ...  # сгенерированная база (в git не хранится)
```

## Структура

| Файл              | Назначение                                              |
| ----------------- | ------------------------------------------------------- |
| `app.py`          | Точка входа: цикл CLI-чата, распознавание команд, откат и сохранение |
| `agent.py`        | `AgentRunner` — ограниченный, наблюдаемый агентный цикл (модель → инструмент → модель) |
| `reliability.py`  | `TurnStatus`/`TerminationReason`/`AgentTurnOutcome`, дедлайны (`run_with_deadline`), отпечаток вызова инструмента |
| `tracing.py`      | Структурированная трассировка: `TraceSink`/`JsonlTraceSink`/`SafeTraceSink`, построение событий |
| `conversation.py` | Класс `Conversation` — владелец истории диалога          |
| `storage.py`      | `JsonConversationStore` — сохранение истории в JSON       |
| `llm.py`          | Клиент Ollama и вызов модели                             |
| `prompts.py`      | Системный промпт                                         |
| `config.py`       | Хост Ollama, имя модели, лимиты цикла/дедлайнов, настройки трассировки |
| `tools/`          | Инструменты: реестр (`ToolSpec`/`ToolRegistry`), исполнитель (`ToolExecutor`), `python_calculate` и `sql_query` |
| `skill_runtime/`  | Рантайм навыков (SPEC-012): модели, загрузчик/валидатор, реестр, роутер, композиция промпта, политика инструментов, оркестратор хода |
| `skills/`         | Декларативные пакеты навыков (`sales_analysis/`, `tracker_read/`: `SKILL.md`, `input.schema.json`, `examples/`, `evals/`) |
| `mcp_servers/`    | Локальные MCP-серверы: `time_server.py` (инструмент `get_current_time` по stdio) |
| `mcp_integration/`| MCP host-сторона: `client.py` (менеджер сессии), `adapter.py` (конвертация имён/схем/результатов, ограничение размера), `policy.py` (точный allowlist-фильтр, SPEC-013), `config.py` (валидированная конфигурация серверов + загрузчик Tracker-окружения, SPEC-013) |
| `sandbox_runtime/`| Изолированный runtime (SPEC-015): модели/политика, валидация путей, сбор артефактов, инъектируемый слой Docker CLI, жизненный цикл контейнера, трассировка. Не импортируется приложением |
| `sandbox/image/`  | Определение образа песочницы: `Dockerfile` (база пришпилена по digest), `hold.py` (доверенный holder-процесс), провенанс в `README.md` |
| `scripts/`        | Утилиты: `init_database.py` — сборка Chinook SQLite из seed; `build_sandbox_image.py` — сборка образа песочницы; `sandbox_smoke.py` — проверка границы изоляции |
| `data/seed/`      | Доверенный seed-скрипт Chinook (`Chinook_Sqlite.sql`)     |
| `tests/`          | Детерминированный набор pytest (без живой модели/MCP/БД)  |
| `evals/`          | Committed кейсы оценки + `runner.py` (`--suite scripted`/`live`) |
| `specs/`          | Спеки итераций (SPEC-NNN) — намерение каждого шага        |
| `docs/journal/`   | Журнал итераций: что менялось и как вела себя модель (включая `patches/` для патчей, затрагивающих поведение модели) |
| `patches/`        | Патчи (PATCH-NNN-XX) — точечные исправления и доработки уже реализованных спек |

## Процесс разработки

Проект развивается пошагово, **одна спека = один шаг**, чтобы историю можно было
воспроизвести хронологически. Цикл (спека → ветка → реализация → проверка на
живой модели → журнал → коммит → `--no-ff` merge) описан в skill
[`spec-cycle`](.claude/skills/spec-cycle/SKILL.md). Каждый шаг фиксируется
записью в [журнале итераций](docs/journal/README.md), включая версию модели и
параметры — то, что git сам по себе не воспроизводит.

`git log --first-parent --oneline` читается как список шагов проекта.

Не каждое изменение заслуживает новой спеки: точечные исправления и доработки
уже реализованной спеки оформляются как **PATCH** (`PATCH-NNN-XX`) — более
лёгкий, но всё ещё воспроизводимый цикл. Классификация SPEC / PATCH /
тривиальная правка, а также полные правила нумерации, веток и журналов
описаны в [`patches/README.md`](patches/README.md) и в том же skill
`spec-cycle`.

## Статус

Ранняя стадия, активная разработка. Интерфейсы и структура могут меняться.

`tools/` содержит `ToolSpec` (контракт инструмента: имя, описание, схемы входа и
выхода), `ToolRegistry` (реестр — регистрация с валидацией, поиск по имени,
генерация деклараций function-tool для Ollama) и `ToolExecutor` (привязка имени к
обработчику и диспетчеризация). Доступны три инструмента: два локальных —
`python_calculate` (ограниченный калькулятор) и `sql_query` (только чтение из
локальной Chinook SQLite) — и один по MCP, `mcp_time__get_current_time`
(текущее время из отдельного stdio-сервера, см. выше). Единый реестр и
исполнитель обслуживают оба источника — локальный обработчик и MCP-сервер. За
один ход модель проходит ограниченный **агентный цикл** (`agent.py`): до
`MAX_TOOL_CALLS_PER_TURN` последовательных вызовов инструментов с само-коррекцией
по структурированным ошибкам, затем финальный ответ. Параллельные вызовы,
несколько MCP-серверов и подагенты появятся в следующих шагах.

`sandbox_runtime/` (SPEC-015) добавляет изолированную границу исполнения для
произвольного Python/Bash в одноразовом Docker-контейнере, но пока **не
подключён к модели**: инструмента песочницы в каталоге нет. Следующий шаг
(STEP 16) добавит рабочее пространство агента и model-facing инструмент поверх
этой границы.
