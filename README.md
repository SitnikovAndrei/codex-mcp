# codex-mcp

MCP-сервер, который делает **Codex исполнителем для Claude Code**: Claude планирует и проверяет, Codex выполняет.

Сервер держит один долгоживущий процесс `codex app-server` (JSON-RPC по stdio) и отдаёт Claude Code набор инструментов: делегирование задач агенту Codex, управление сессиями, выполнение команд в песочнице Codex, ревью, доступ к MCP-серверам и плагинам Codex.

Один файл, чистый Python, без зависимостей.

## Требования

- Codex CLI **0.157+** (`codex --version`), выполнен `codex login`
- Python 3.8+
- Claude Code

## Установка

```bash
git clone git@github.com:SitnikovAndrei/codex-mcp.git
claude mcp add codex -s user -- python /path/to/codex-mcp/codex_mcp.py
```

`-s user` регистрирует сервер для всех проектов. Проверка: `claude mcp get codex` → `Connected`.

Рекомендуется также подключить правило-инструкцию, которое объясняет Claude, когда и как делегировать работу Codex:

```bash
cp codex.md ~/.claude/rules/codex.md
```

## Инструменты

| Инструмент | Что делает |
|---|---|
| `codex_task` | Отдать задачу агенту Codex (`prompt`, `cwd`, `thread_id`, `model`, `effort`, `sandbox`, `images`, `output_schema`, …). Синхронно или `wait=false` |
| `codex_status` | Прогресс / вопрос / итоговый отчёт по треду, `wait_s` — подождать |
| `codex_answer` | Ответить на вопрос Codex (статус `waitingForAnswer`) |
| `codex_steer` | Добавить указание в идущий ход, не останавливая его |
| `codex_interrupt` | Остановить ход |
| `codex_review` | Код-ревью: `uncommitted`, `base:<branch>`, `commit:<sha>` или свои инструкции |
| `codex_exec` | Выполнить shell-команду через исполнитель Codex (без модели) |
| `codex_models` | Модели, поддерживаемые effort и тиры |
| `codex_limits` | Лимиты аккаунта (% окна, сброс) и расход токенов |
| `codex_threads` | Список сессий (фильтр по `cwd`, названию) |
| `codex_thread_read` | Последние ходы сессии |
| `codex_fork` | Ответвить сессию в новый тред |
| `codex_thread_manage` | `rename`, `archive`, `unarchive`, `delete`, `compact`, `revert`, `goal_set`/`goal_get`/`goal_clear` |
| `codex_capabilities` | MCP-серверы (и их инструменты), скиллы, плагины Codex |
| `codex_mcp_call` | Вызвать инструмент MCP-сервера Codex напрямую |

Отчёт `codex_task` содержит `thread_id`, статус, финальное сообщение Codex, изменённые файлы и выполненные команды (с выводом упавших).

## Типичный цикл

1. Claude формулирует самодостаточную задачу → `codex_task(prompt, cwd)`.
2. Если Codex спросил что-то — `codex_answer`.
3. Claude смотрит `git diff`, проверяет результат, правки отправляет в тот же тред через `thread_id`.
4. Опционально — `codex_review` как второе мнение.

## Важно знать

- **Подтверждения принимаются автоматически.** Запросы Codex на выполнение команд, правку файлов и доп. права одобряются без участия человека — границы задаёт `sandbox` задачи. Без `sandbox` используется значение из `~/.codex/config.toml`; при `danger-full-access` Codex может менять что угодно. Для ограничения передавайте `sandbox: "workspace-write"` или `"read-only"`.
- **Вопросы Codex** работают через экспериментальный флаг `default_mode_request_user_input`, с которым запускается `app-server`. В будущих версиях Codex он может измениться.
- **`goal_set`** сразу запускает Codex работать к цели самостоятельно; такие ходы видны в `codex_status`.
- **`revert`** удаляет ходы только из истории треда — изменения в файлах остаются.
- **Состояние ходов в памяти.** После перезапуска Claude Code `codex_status` старых ходов не знает; историю смотрите через `codex_thread_read`, продолжайте через `codex_task(thread_id=...)`.
- **Windows-песочница.** В `workspace-write` файлы создаются отдельным пользователем песочницы Codex. В обычных папках права наследуются и всё работает, но в каталогах с ограниченным ACL (например, созданных `tempfile.mkdtemp` в Python 3.13+) такие файлы потом недоступны вне песочницы.

## Протокол

Схему протокола app-server для своей версии Codex можно сгенерировать:

```bash
codex app-server generate-json-schema --out ./schema
```
