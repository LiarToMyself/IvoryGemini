# IvoryGemini — Gemini Desktop Bridge MCP Server (v2.5.0)

Двусторонний MCP-сервер для управления нативным приложением **Google Gemini Desktop** (Windows 10/11 и macOS) из агентских IDE и клиентов (**Antigravity**, **ChatGPT Desktop**, **Cursor**, **Windsurf**, **Claude Desktop**, **Claude Code**) через активное окно и протокол Model Context Protocol (MCP).

## Ключевые возможности v2.5.0

* **Нативное чтение ответов без CDP (обход блокировки Electron `app-1.16.0+`)**:
  * Пробуждение дерева доступности Chromium (`AXTree`) через `WM_GETOBJECT` (`OBJID_CLIENT`) без запрещённых аргументов `--remote-debugging-port`.
  * Привязка STA-потока к интерактивному десктопу `WinSta0\default` и учёт системного масштабирования `SetProcessDPIAware()`.
  * Сборка C#-моста `Win32Bridge` в кэшируемую `.dll` в `%TEMP%` один раз (без повторного вызова `csc.exe` на каждый тик опроса).
  * Гибридное извлечение ответа: чтение узлов `UIA` в реальном времени + нажатие кнопки «Копировать» последнего ответа через `ScrollItemPattern` и `InvokePattern` (без перехвата мыши в фоновом режиме) с сохранением и восстановлением буфера обмена пользователя.
* **Приём ОЧЕНЬ долгих ответов (`Deep Research`, `Thinking`, генерация крупных модулей)**:
  * Отслеживание кнопки `Stop`, фазы `Thinking`, счётчика реплик (`turns_count`) и кнопок копирования (`copy_buttons_count`).
  * Каскадный поллинг через `wait_for_gemini_response` при превышении `timeout_sec` (возврат `status: "streaming"` и `partial_response`).
  * Постраничная пагинация (`max_chars`, `offset`) для защиты контекстного окна MCP-клиента.
* **Безопасное переключение аккаунтов Google (`slot_0..slot_N`)**:
  * Автоматическое маскирование почтовых адресов (`<masked_slot>` при `GEMINI_MASK_EMAILS=1`).
  * Переключение между учётными записями по номеру слота (`account_index=0..4`), идентификатору `slot_1` или циклически.

---

## Справочник ручек MCP (15 инструментов)

| № | Ручка (Tool) | Параметры | Описание |
| :--- | :--- | :--- | :--- |
| 1 | `check_gemini_bridge_status` | `{}` | Проверяет путь к `Gemini.exe` / `Gemini.app`, список открытых окон, активный слот аккаунта, текущую модель, статус маскирования почт и состояние генерации. |
| 2 | `launch_gemini_desktop` | `{}` | Запускает нативный клиент `Gemini Desktop` или вызывает его из трея (`Alt + Space` / `Option + Space`) и активирует дерево `AXTree`. |
| 3 | `focus_gemini_window` | `hwnd=0`, `window_index=0` | Разворачивает и выводит на передний план окно `Gemini Desktop` через `AttachThreadInput`. |
| 4 | `send_task_to_gemini` | `prompt`, `wait_for_response=True`, `timeout_sec=180`, `poll_interval_sec=1.5`, `stable_sec=4.0`, `new_chat=False`, `prefer_clipboard_copy=True`, `max_chars=0` | Фокусирует окно и поле ввода `Edit`, вставляет `prompt` (с последующим восстановлением буфера обмена), отправляет запрос и возвращает ответ в формате Markdown. |
| 5 | `wait_for_gemini_response` | `timeout_sec=300`, `poll_interval_sec=2.0`, `stable_sec=4.0`, `prefer_clipboard_copy=True`, `max_chars=0` | Дожидается окончания сверхдолгих ответов (`Deep Research`, `Thinking`). Вызывается повторно, пока `status != "completed"`. |
| 6 | `read_last_gemini_response` | `wait_until_complete=True`, `timeout_sec=120`, `stable_sec=3.5`, `prefer_clipboard_copy=True`, `max_chars=0`, `offset=0` | Считывает последний ответ ассистента с очисткой от подвальных дисклеймеров и поддержкой пагинации (`max_chars`, `offset`). |
| 7 | `get_gemini_generation_status` | `{}` | Мгновенная неблокирующая проверка статуса (`thinking` / `streaming` / `completed`) и длины текущего ответа. |
| 8 | `stop_gemini_generation` | `{}` | Прерывает генерацию ответа (включая фазу `Thinking`) через кнопку «Остановить», нижнюю панель ввода или `Escape`. |
| 9 | `read_gemini_chat_history` | `max_turns=20`, `limit=None`, `max_chars_per_turn=0` | Возвращает последние реплики диалога (`user` / `assistant`) из открытого окна чата. |
| 10 | `start_new_gemini_chat` | `{}` | Открывает новый чистый чат (через UIA-кнопку «Новый чат» или `Ctrl+Shift+O`) и переводит фокус в поле ввода промпта. |
| 11 | `list_gemini_accounts` | `open_menu=True` | Возвращает список доступных слотов аккаунтов (`slot_0`, `slot_1`, ...) из меню профиля с маскированием адресов по умолчанию. |
| 12 | `switch_gemini_account` | `account_query=""`, `account_index=-1` | Переключает аккаунт Google по индексу слота (`0..4`), идентификатору `slot_1` или циклически на следующий неактивный слот. |
| 13 | `select_gemini_model` | `model_query` | Переключает режим или модель (`"Flash"`, `"Pro"`, `"Deep Research"`, `"Thinking"`) через селектор режима. |
| 14 | `inspect_gemini_ui_tree` | `filter_text=""`, `max_depth=8`, `max_elements=120`, `interactive_only=False` | Возвращает список элементов `UIA` с координатами центров (`cx`, `cy`) для диагностики интерфейса. |
| 15 | `capture_gemini_window_screenshot` | `output_path=""`, `include_base64=False` | Сохраняет DPI-корректный PNG-скриншот окна `Gemini Desktop` для визуальной отладки. |

---

## Быстрый старт и конфигурация (`mcp_config.json`)

```json
{
  "mcpServers": {
    "gemini-desktop": {
      "command": "python",
      "args": [
        "path/to/IvoryGemini/server.py"
      ],
      "env": {
        "GEMINI_APP_NAME": "Gemini",
        "GEMINI_DESKTOP_PATH": "",
        "GEMINI_DEFAULT_TIMEOUT_SEC": "180",
        "GEMINI_POLL_INTERVAL_SEC": "1.5",
        "GEMINI_STABLE_SEC": "4.0",
        "GEMINI_MASK_EMAILS": "1",
        "GEMINI_EXPOSE_RAW_EMAILS": "0",
        "GEMINI_ACCOUNT_SLOT_0": "",
        "GEMINI_ACCOUNT_SLOT_1": "",
        "GEMINI_MAX_RESPONSE_CHARS": "0",
        "PYTHONIOENCODING": "utf-8"
      }
    }
  }
}
```

## Структура репозитория

* `server.py` — основной MCP-сервер (поддерживает `FastMCP` и встроенный `stdlib` JSON-RPC 2.0 `stdio` цикл).
* `tools_schema.json` — сводная JSON-схема всех 15 инструментов.
* `schemas/*.json` — индивидуальные JSON-схемы каждой ручки (`inputSchema` + `parameters`).
* `PITFALLS_REPORT.md` — отчёт по узким местам, рискам и архитектурным решениям (с примерами из `Context7`).
* `mcp_config.json` — шаблон конфигурации без персональных данных.
* `requirements.txt` — список опциональных зависимостей (`mcp[cli]`, `pyperclip`).
