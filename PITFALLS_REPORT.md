# Отчёт: узкие места и риски Gemini Desktop MCP

База доков и кода: [Context7 mcp-use Tools](https://github.com/mcp-use/mcp-use/blob/main/docs/typescript/client/tools.mdx), [Context7 mcp-use Progress](https://github.com/mcp-use/mcp-use/blob/main/docs/python/client/notifications.mdx), [Context7 Playwright MCP](https://github.com/microsoft/playwright-mcp/blob/main/_autodocs/api-reference/tools.md).

## 1. Сводка рисков и правок в v2.5.0

| Узкое место | В чём сбой в v2.4 | Риск | Правка в v2.5.0 |
| :--- | :--- | :--- | :--- |
| Таймаут `stdio` без прогресса | Синхронный `sleep` в `FastMCP` не шлёт `ProgressNotification` | Обрыв вызова через 60 сек в IDE | Шлём `ctx.report_progress` и `notifications/progress` |
| Шторм процессов `powershell` | Запуск `powershell.exe` и `csc.exe` каждые 1.5 сек | Рост нагрузки CPU, лаг 0.8 сек на тик | Сборка C# моста в `.dll` в `%TEMP%` один раз |
| Перехват буфера и мыши | `set_clipboard_text` затирает буфер юзера при отправке | Потеря данных юзера, прыжки курсора | Возврат старого буфера, блок мыши при фоне |
| Скролл и глубина `UIA` | `FindAll(Descendants)` игнорит `max_depth`, кнопка вне экрана | Таймаут 12 сек на длинном чате, сбой копии | `TreeWalker` с `max_depth`, `ScrollIntoView()` |
| Масштаб `DPI` и лок экрана | Нет `SetProcessDPIAware()`, клик мимо при 150% `DPI` | Скриншот режет край, клик мажет, вис при `Win+L` | Вызов `SetProcessDPIAware()` в Python и C# |
| Повтор хеша `baseline` | Короткий ответ равен прошлому (`Да` / `OK`) | Ложный цикл до конца `timeout_sec` | Учёт роста `turns_count` и `copy_buttons_count` |
| Смена вёрстки и языка UI | Поиск кнопок только по тексту `RU` и `EN` | Сбой на иных языках ОС или после патча UI | Опора на `AutomationId`, роли и геометрию |

## 2. Разбор узких мест и примеры кода из Context7

### 2.1. Обрыв долгих вызовов без `ProgressNotification`

* **Суть бага**: вызовы `send_task_to_gemini` и `wait_for_gemini_response` ждут ответ до 180–300 сек. В `v2.4` функции `FastMCP` синхронные и молчат в `stdio` весь цикл.
* **Почему падает**: клиенты MCP ждут пакеты прогресса для сброса таймера (`resetTimeoutOnProgress: true` в [Context7 mcp-use Tools](https://github.com/mcp-use/mcp-use/blob/main/docs/typescript/client/tools.mdx)). Без них хост рубит вызов через 60 сек.
* **Эталон клиента из [Context7 mcp-use Tools](https://github.com/mcp-use/mcp-use/blob/main/docs/typescript/client/tools.mdx)**:

```typescript
const abortController = new AbortController();

await conn.callTool(
  "wait_for_gemini_response",
  { timeout_sec: 300 },
  {
    timeout: 300_000,
    resetTimeoutOnProgress: true,
    maxTotalTimeout: 900_000,
    signal: abortController.signal,
  },
);
```

* **Эталон сервера с `ctx.report_progress` из [Context7 mcp-use Progress](https://github.com/mcp-use/mcp-use/blob/main/docs/python/client/notifications.mdx)**:

```python
from fastmcp import Context, FastMCP

mcp = FastMCP(name="gemini-desktop-bridge")

@mcp.tool()
async def wait_for_gemini_response(timeout_sec: int = 300, ctx: Context = None) -> str:
    for step in range(timeout_sec):
        if ctx is not None:
            await ctx.report_progress(
                progress=float(step + 1),
                total=float(timeout_sec),
                message=f"Poll {step + 1}/{timeout_sec}",
            )
```

* **Решение в v2.5.0**: добавлена отправка `notifications/progress` при наличии `progressToken` в `stdio` и поддержка `ctx.report_progress` в `FastMCP`.

### 2.2. Шторм запусков `powershell.exe` и сборки C#

* **Суть бага**: каждый вызов `_run_windows_uia_action` поднимает новый процесс `powershell.exe` и собирает класс `Win32Bridge` через `Add-Type`. За 3 минуты опроса создаётся 120 процессов.
* **Риск**: пики CPU, антивирус тормозит `csc.exe` во временной папке, опрос лагает.
* **Решение в v2.5.0**: C# класс `Win32Bridge` собирается один раз в `gemini_win32_bridge_v25.dll` через `OutputAssembly` и дальше грузится за 5 мс через `[Reflection.Assembly]::LoadFrom(...)`.

### 2.3. Скрытие узлов DOM и обход дерева `UIA` без лимита глубины

* **Суть бага**: в `v2.4` параметр `max_depth` в `inspect_gemini_ui_tree` не влиял на `$root.FindAll(TreeScope::Descendants)`. На длинных чатах в `Electron` дерево превышает 10 000 узлов. Кроме того, кнопка `Copy` у длинного ответа уходит за край экрана (`BoundingRectangle.IsEmpty`), и фильтр `$hasRect` теряет её.
* **Эталон снимка дерева с `depth` и `boxes` из [Context7 Playwright MCP](https://github.com/microsoft/playwright-mcp/blob/main/_autodocs/api-reference/tools.md)**:

```typescript
captureSnapshot(
  root: playwright.Locator | undefined,
  depth: number | undefined,
  boxes: boolean | undefined,
  relativeTo: string | undefined,
  ariaFormat: 'none' | 'text' | 'json' = 'text'
): Promise<TabSnapshot>
```

* **Решение в v2.5.0**:
  * Внедрён обход через `TreeWalker::RawViewWalker` с жёстким отсечением по `max_depth`.
  * Для кнопки `Copy` и финального блока ответа добавлен вызов `ScrollItemPattern.ScrollIntoView()` перед чтением координат и кликом.

### 2.4. Конфликт буфера обмена и кража фокуса окна

* **Суть бага**:
  * В `_send_to_verified_window_windows` текст задачи копируется в системный буфер обмена для `Ctrl+V`, но старый буфер юзера не возвращается на место.
  * В `Invoke-OrClick` при сбое `InvokePattern` вызывается `SetCursorPos` и клик мыши, что крадёт фокус у юзера во время фонового чтения ответа.
* **Решение в v2.5.0**:
  * В `_send_to_verified_window_windows` старый текст буфера сохраняется до `Ctrl+V` и возвращается через 150 мс после вставки.
  * Для фонового чтения `click_last_copy` запрещён мышиный клик (`GEMINI_ALLOW_MOUSE_FALLBACK=0`): работает только тихий `InvokePattern` / `LegacyIAccessiblePattern`.

### 2.5. Сдвиг координат при `DPI > 100%` и лок сессии `Win+L`

* **Суть бага**: без вызова `SetProcessDPIAware()` на экранах с масштабом 125%–200% `GetWindowRect` и `CopyFromScreen` дают смещённые координаты, а скриншот режет правый и нижний край окна. При закрытом экране (`Win+L`) `SetForegroundWindow` возвращает `False`.
* **Решение в v2.5.0**:
  * В Python (`ctypes.windll.user32.SetProcessDPIAware()`) и в `Win32Bridge` (`SetProcessDPIAware()`) включён учёт системного `DPI`.
  * Добавлена проверка лока ввода и возврат чёткого кода ошибки, если рабочий стол `WinSta0\default` недоступен.

### 2.6. Коллизия `baseline_hash` на кратких ответах

* **Суть бага**: если модель дала точно такой же краткий ответ, как в прошлом ходе (например, `Да` или `Готово`), то `cur_hash == baseline_hash`, и цикл ждёт до конца `timeout_sec`.
* **Решение в v2.5.0**: вместе с `baseline_hash` фиксируются `initial_turns_count` и `initial_copy_count`. Рост числа реплик или кнопок копии сразу засчитывает новый ответ.

### 2.7. Защита секретов и путей

* **Суть бага**: в `mcp_config.json` оставался локальный путь диска `<local_path>`, а при `GEMINI_EXPOSE_RAW_EMAILS=1` почты могли попасть в отчёт.
* **Решение в v2.5.0**: локальные пути заменены на нейтральный шаблон, маска почт `<masked_slot>` включена по умолчанию, все файлы проверены сканером секретов.
