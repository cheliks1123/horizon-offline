# Horizon Offline

Небольшой лаунчер для [Horizon](https://playhorizon.dev), который запускает игру
**без входа в Microsoft-аккаунт**. Он не меняет файлы Horizon: вызывает штатную
команду `horizon-ctl launch` и передаёт ей ник, версию и мир. Если файлов
Minecraft на компьютере нет, лаунчер сам скачивает их с серверов Mojang и
собирает ресурсы Horizon через `horizon-prepare`.

## Что нужно

- Windows 10/11.
- Установленный Horizon (официальный лаунчер кладёт версии в
  `%APPDATA%\horizon-launcher\versions`).
- Для запуска из исходников — Python 3.10+ (Tkinter входит в установку с python.org).
  В релизах с GitHub лежит готовый `.exe`, Python не нужен.

## Как пользоваться

1. Запусти `Horizon Offline.lnk` (или `pythonw horizon_offline.py`).
2. Выбери версию Horizon. Неподготовленная версия помечается внизу окна —
   нажми **Скачать 26.3** (кнопка показывает версию Minecraft из
   `research-lock.json` выбранного билда).
3. Дождись окончания: лаунчер скачает клиент, сервер, библиотеки, ассеты и
   Java нужной версии, затем соберёт ресурсы Horizon. Уже скачанные файлы
   повторно не качаются.
4. Введи ник.
5. Выбери, что открыть: **Главное меню** или **Загрузить мир**.
6. Нажми **Играть**.

Настройки сохраняются в `launcher.ini` рядом с лаунчером.

## Настройки (`launcher.ini`)

| Ключ        | Значение                                                        |
|-------------|-----------------------------------------------------------------|
| `nick`      | Ник игрока.                                                     |
| `version`   | Папка версии Horizon, например `nightly-2026-10-30-1da354b`. Пусто — автоопределение. |
| `world`     | Папка мира. Пусто — главное меню.                               |
| `home`      | Игровая папка. Пусто — `%APPDATA%\horizon`.                      |
| `minecraft` | Папка с файлами Minecraft. Пусто — `%APPDATA%\.minecraft`.       |

Миры лежат в `%APPDATA%\horizon\saves` (кнопка «Папка миров» открывает её).

## Моды

Кнопка **«Корневая папка»** открывает игровую папку (`%APPDATA%\horizon`, или
`home` из `launcher.ini`). В ней лежат `saves/`, `options.txt`,
`resourcepacks/`, `screenshots/` и папка `horizon-mods` — файлы `.hmod` кладутся
именно туда, после чего мод включается в игре: Options → Mods. Папка
`horizon-mods` создаётся при первом нажатии кнопки, если её ещё нет.

## Скачивание версии

Кнопка **Скачать** в окне делает то же, что и

```powershell
python horizon_offline.py --download
python horizon_offline.py --download --version nightly-2026-10-30-1da354b
```

Что скачивается с серверов Mojang (в `%APPDATA%\.minecraft`):

- `versions/<версия>/<версия>.jar` — клиент;
- `versions/<версия>/server.jar` — серверный jar для `horizon-prepare --prepare`;
- `libraries/` — библиотеки для Windows x64;
- `assets/` — индекс ассетов и все объекты (текстуры, звуки, языки);
- `runtime/<компонент>/windows-x64/` — Java из комплекта Mojang (например,
  `java-runtime-epsilon`, Java 25).

Каждый файл проверяется по SHA-1; скачанное переиспользуется, так что повторный
запуск докачивает только недостающее. Версия Minecraft берётся из
`research-lock.json` выбранного билда Horizon, поэтому функция работает и с
другими версиями, если для них есть билд Horizon. После скачивания выполняется
`horizon-prepare --prepare` и `horizon-prepare sounds`.

Файлы скачиваются напрямую с piston-meta/ресурсов Mojang — это те же файлы, что
качает официальный лаунчер Minecraft, и по EULA они предполагают наличие
купленной Minecraft: Java Edition.

## Проверка без запуска игры

```powershell
python horizon_offline.py --check
```

Печатает найденные версии, версию по умолчанию и список миров в JSON.

## Запуск без окна

```powershell
python horizon_offline.py --play
```

Запускает игру сразу по настройкам из `launcher.ini` — удобно для своих ярлыков
и скриптов.

## Сборка

```powershell
.\build.ps1
```

Скрипт ставит PyInstaller, собирает `dist/HorizonOffline.exe` и упаковывает
`dist/HorizonOffline-win64.zip` (exe, конфиг, иконка, README).

GitHub Actions (`.github/workflows/build.yml`) делает то же самое на
`windows-latest`, загружает zip как артефакт, а на тег `v*` создаёт релиз.

## Публикация на GitHub

```powershell
git init
git add .
git commit -m "Initial commit"
git branch -M main
git remote add origin https://github.com/USER/REPO.git
git push -u origin main
```

Релиз с собранным exe:

```powershell
git tag v1.0.0
git push origin v1.0.0
```

Собранный `HorizonOffline.exe` и папка `dist/` в репозиторий не попадают —
их создаёт CI (см. `.gitignore`).

## Ограничения

Без Microsoft-аккаунта работают одиночная игра и LAN. Онлайн-серверы, Realms
и скины требуют входа в аккаунт — для этого используй официальный лаунчер.

Horizon — неофициальный фанатский проект, не связанный с Mojang Studios или
Microsoft. Minecraft является торговой маркой Mojang и Microsoft.
