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

## Конфигурация

Настройки хоста, модели и путей к БД — в `config.py`:

```python
OLLAMA_HOST = "http://localhost:11434"
MODEL_NAME = "qwen3:8b"
MAX_CONTEXT_MESSAGES = 20              # сколько последних сообщений уходит модели
CHAT_HISTORY_PATH = "data/chat_history.json"  # где хранится история
MAX_TOOL_CALLS_PER_TURN = 4           # предел исполненных инструментов за ход

CHINOOK_SEED_PATH = ...     # доверенный seed под контролем версий
SQLITE_DATABASE_PATH = ...  # сгенерированная база (в git не хранится)
```

## Структура

| Файл              | Назначение                                              |
| ----------------- | ------------------------------------------------------- |
| `app.py`          | Точка входа: цикл CLI-чата, распознавание команд, откат и сохранение |
| `agent.py`        | `AgentRunner` — ограниченный агентный цикл (модель → инструмент → модель) |
| `conversation.py` | Класс `Conversation` — владелец истории диалога          |
| `storage.py`      | `JsonConversationStore` — сохранение истории в JSON       |
| `llm.py`          | Клиент Ollama и вызов модели                             |
| `prompts.py`      | Системный промпт                                         |
| `config.py`       | Хост Ollama и имя модели                                 |
| `tools/`          | Инструменты: реестр (`ToolSpec`/`ToolRegistry`), исполнитель (`ToolExecutor`), `python_calculate` и `sql_query` |
| `mcp_servers/`    | Локальные MCP-серверы: `time_server.py` (инструмент `get_current_time` по stdio) |
| `mcp_integration/`| MCP host-сторона: `client.py` (менеджер сессии), `adapter.py` (конвертация имён/схем/результатов) |
| `scripts/`        | Утилиты: `init_database.py` — сборка Chinook SQLite из seed |
| `data/seed/`      | Доверенный seed-скрипт Chinook (`Chinook_Sqlite.sql`)     |
| `specs/`          | Спеки итераций (SPEC-NNN) — намерение каждого шага        |
| `docs/journal/`   | Журнал итераций: что менялось и как вела себя модель      |

## Процесс разработки

Проект развивается пошагово, **одна спека = один шаг**, чтобы историю можно было
воспроизвести хронологически. Цикл (спека → ветка → реализация → проверка на
живой модели → журнал → коммит → `--no-ff` merge) описан в skill
[`spec-cycle`](.claude/skills/spec-cycle/SKILL.md). Каждый шаг фиксируется
записью в [журнале итераций](docs/journal/README.md), включая версию модели и
параметры — то, что git сам по себе не воспроизводит.

`git log --first-parent --oneline` читается как список шагов проекта.

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
