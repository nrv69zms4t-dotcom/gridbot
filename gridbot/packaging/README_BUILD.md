# Сборка GridBot.exe и установщика

## Предварительные требования

| Компонент | Версия | Примечание |
|---|---|---|
| Python | 3.11+ | должен быть в `PATH` (`python`) |
| PyInstaller | 6.x | установлен в user site-packages; запускать **только** как `python -m PyInstaller` (папка `Scripts` не в `PATH`) |
| pywebview | 6.2.1 | несёт собственные PyInstaller-хуки (WebView2 DLL, js) |
| pythonnet / clr_loader | 3.1.0 / 0.3.1 | .NET-мост для winforms/edgechromium backend |
| Pillow | любая свежая | нужна только для генерации иконки |
| Inno Setup | 6.x | опционально — только для `GridBot-Setup.exe`; ожидается в `%LOCALAPPDATA%\Programs\Inno Setup 6\ISCC.exe` (есть fallback-поиск в Program Files и `PATH`) |

## Команда сборки

Из корня репозитория (или из любого места — скрипт сам вычисляет пути):

```powershell
powershell -ExecutionPolicy Bypass -File gridbot/packaging/build.ps1
```

Только portable exe, без установщика:

```powershell
powershell -ExecutionPolicy Bypass -File gridbot/packaging/build.ps1 -SkipInstaller
```

Шаги скрипта:
1. Если нет `gridbot/packaging/gridbot.ico` — генерирует её (`make_icon.py`, Pillow).
2. `python -m PyInstaller gridbot_app.spec` (onefile, windowed, без консоли).
3. Проверяет результат и печатает размер exe.
4. Если Inno Setup найден и не задан `-SkipInstaller` — компилирует `installer.iss`.

Скрипт завершится с ненулевым кодом при любой ошибке шага.

## Где искать результаты

- Portable exe: `gridbot/packaging/dist/GridBot.exe`
- Установщик:  `gridbot/packaging/dist/GridBot-Setup.exe`
- Промежуточные файлы PyInstaller: `gridbot/packaging/build/` (можно удалять)

## Быстрая проверка сборки

Exe оконный (консоли нет), поэтому проверка через self-test с JSON-отчётом:

```powershell
gridbot\packaging\dist\GridBot.exe --selftest
Get-Content $env:TEMP\gridbot_selftest.json
```

Ожидается `{"ok": true, ...}` и код выхода 0.

## Примечания

- **Edge WebView2 Runtime.** UI работает поверх Microsoft Edge WebView2. На
  Windows 11 (и на свежих Windows 10) рантайм предустановлен — ничего ставить
  не нужно. Если на «голой» машине окно не открывается — установить
  [Evergreen WebView2 Runtime](https://developer.microsoft.com/microsoft-edge/webview2/)
  от Microsoft.
- **SmartScreen.** Exe и установщик не подписаны сертификатом, поэтому при
  первом запуске скачанного файла Windows SmartScreen покажет предупреждение
  «Windows защитила ваш компьютер». Это ожидаемо: «Подробнее» → «Выполнить в
  любом случае». Для локально собранного файла предупреждения обычно нет
  (SmartScreen реагирует на Mark-of-the-Web у скачанных файлов).
- **Данные пользователя.** Приложение хранит состояние/логи/конфиг в
  `%LOCALAPPDATA%\GridBot`. Деинсталлятор эту папку **не удаляет** — это
  сделано намеренно (см. комментарий в `installer.iss`).
- **Установка без прав администратора.** `PrivilegesRequired=lowest`: без
  повышения прав установщик ставит приложение в
  `%LOCALAPPDATA%\Programs\GridBot`; при запуске от администратора — в
  `Program Files`.
