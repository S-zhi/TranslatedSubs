[English](../../README.md) | [简体中文](../zh/README.md) | [हिन्दी](../hi/README.md) | [Español](../es/README.md) | [Français](../fr/README.md) | [Português](../pt/README.md) | Русский

<div align="center">
  <img src="../assets/eye-subtitles-logo.svg" width="88" alt="Логотип TranslatedSubs" />
  <h1>TranslatedSubs</h1>
  <p><strong>Понимайте и речь, и текст на экране: распознавайте речь, переводите субтитры и извлекайте надписи с доски.</strong></p>
  <p>Быстрый запуск и интеграция с MCP.</p>
</div>

TranslatedSubs — рабочая среда для понимания видео и аудио: загрузка материалов, распознавание речи, перевод субтитров и распознавание текста, написанного в кадре.

- **Видео из разных источников**: загружайте ролики напрямую с поддерживаемых платформ без предварительного скачивания и повторной загрузки файлов.
- **Гибкий локальный запуск и выбор моделей**: быстро запускайте приложение или используйте облегчённую конфигурацию; подключайте более продвинутые и общественные модели для повышения качества сопоставления.
- **Параллельные задачи и мониторинг ресурсов**: запускайте несколько задач одновременно и следите за их состоянием и использованием ресурсов. Доступна синхронизация с Google Drive.
- **Отдельный сервис дообучения моделей**: дообучайте поддерживаемые модели в отдельном сервисе для улучшения результатов.
- **Работа на разных устройствах**: веб-версия, Windows и macOS; поддержка телефонов планируется в будущем.

Поддерживаемые видеоплатформы:
[![YouTube](https://img.shields.io/badge/YouTube-FF0033?style=plastic&logo=youtube&logoColor=white)](https://www.youtube.com/)
[![Vimeo](https://img.shields.io/badge/Vimeo-1AB7EA?style=plastic&logo=vimeo&logoColor=white)](https://vimeo.com/)
[![Dailymotion](https://img.shields.io/badge/Dailymotion-0066DC?style=plastic&logo=dailymotion&logoColor=white)](https://www.dailymotion.com/)
[![Twitch](https://img.shields.io/badge/Twitch-9146FF?style=plastic&logo=twitch&logoColor=white)](https://www.twitch.tv/)
[![TikTok](https://img.shields.io/badge/TikTok-111111?style=plastic&logo=tiktok&logoColor=white)](https://www.tiktok.com/)
[![X / Twitter](https://img.shields.io/badge/X%20%28Twitter%29-111111?style=plastic&logo=x&logoColor=white)](https://x.com/)
[![Instagram](https://img.shields.io/badge/Instagram-E4405F?style=plastic&logo=instagram&logoColor=white)](https://www.instagram.com/)
[![AcFun](https://img.shields.io/badge/AcFun-FD4C5D?style=plastic)](https://www.acfun.cn/)
[![Niconico](https://img.shields.io/badge/Niconico-252525?style=plastic&logo=niconico&logoColor=white)](https://www.nicovideo.jp/)
[![Pornhub](https://img.shields.io/badge/Pornhub-FF9900?style=plastic)](https://www.pornhub.com/)

![TranslatedSubs](../assets/readme-demo-1.png)

![TranslatedSubs](../assets/readme-demo-2.png)

## Сборка из исходного кода

### Обязательные зависимости

Для запуска из исходного кода нужны Python 3.10–3.12, `uv`, Git (только для клонирования репозитория), FFmpeg/FFprobe с фильтром `subtitles` (libass) для встраивания субтитров и работающий сервис Redis. `uv sync` устанавливает библиотеки Python, включая клиент Redis (`redis>=5`), но не устанавливает и не запускает сервер Redis. Если у вас уже есть удалённый Redis, укажите его адрес в `redis.url` файла `config.local.json` и не устанавливайте ещё один сервер. Go 1.23 или новее нужен только для sidecar Google Drive; Node.js — только для тестов frontend. Эти функции не нужны для запуска; устанавливайте только необходимые (несколько extras можно объединить в одной команде):

- `uv sync --extra local-translation` — локальный перевод, по умолчанию с английского на китайский.
- `uv sync --extra hf-models` — модели Whisper от Hugging Face.
- `uv sync --extra tts` — озвучивание Kokoro; также установите в системе `espeak-ng` (см. [TTS](../tts.md)).

Установка пакетов Python не включает веса моделей: перед первой задачей скачайте или импортируйте модели локального перевода и Whisper в настройках и проверьте их готовность. Kokoro скачивает веса при первой генерации речи. Demucs устанавливается отдельно, только если включено разделение голоса.

### Сборка на macOS

Для локального запуска проекта на macOS нужны Python 3.10–3.12, `uv` и FFmpeg с поддержкой libass. API также раздаёт веб-интерфейс, поэтому отдельно устанавливать Node.js не требуется.

Выполните команды в подходящем каталоге:

```sh
git clone https://github.com/S-zhi/TranslatedSubs.git
cd TranslatedSubs
brew install uv redis
brew tap homebrew-ffmpeg/ffmpeg
brew install ffmpeg-full
uv sync
cp config.example.json config.local.json
brew services start redis
redis-cli ping
```

Последняя команда должна вывести `PONG`. Выполните её до запуска API.

Настройте учетные данные моделей и параметры службы после запуска в настройках или через API. Они сохраняются в Redis и применяются без перезапуска.

Запустите API и веб-интерфейс:

```sh
uv run python -m src.cli
```

Откройте <http://127.0.0.1:8000/>. Проверьте работу сервиса и наличие в FFmpeg фильтра для встроенных субтитров:

```sh
curl http://127.0.0.1:8000/api/health
curl http://127.0.0.1:8000/api/health/ready
ffmpeg -hide_banner -filters | grep " subtitles "
```

### Сборка на Windows

В Windows 10 или 11 TranslatedSubs можно запустить из исходного кода через PowerShell. Проект поддерживает Python 3.10–3.12; в этих шагах используется Python 3.12 и устанавливаются версии, зафиксированные в `uv.lock`. API раздаёт веб-интерфейс, поэтому отдельно устанавливать Node.js не нужно.

1. Установите `uv`:

   ```powershell
   powershell -ExecutionPolicy ByPass -c "irm https://astral.sh/uv/install.ps1 | iex"
   ```

   После установки заново откройте PowerShell.

2. Установите FFmpeg. На [странице загрузки FFmpeg](https://ffmpeg.org/download.html) выберите сборку для Windows и скачайте полную сборку Gyan. Распакуйте её и добавьте каталог `bin` (например, `C:\ffmpeg\bin`) в `PATH`, затем заново откройте PowerShell. Проверьте, что доступны `ffmpeg`, `ffprobe` и фильтр для встроенных субтитров:

   ```powershell
   ffprobe -version
   ffmpeg -hide_banner -filters | findstr /i subtitles
   ```

3. Склонируйте проект, установите зависимости и создайте локальную конфигурацию:

   ```powershell
   git clone https://github.com/S-zhi/TranslatedSubs.git
   cd TranslatedSubs
   uv python install 3.12
   uv sync --python 3.12 --locked
   Copy-Item config.example.json config.local.json
   ```

4. Подготовьте Redis с помощью Docker Desktop для Linux-контейнеров. Если у вас уже есть удалённый Redis, укажите его адрес в `redis.url` файла `config.local.json` и пропустите этот шаг.

   ```powershell
   docker run -d --name translatedsubs-redis --restart unless-stopped -p 127.0.0.1:6379:6379 -v translatedsubs-redis-data:/data redis:7-alpine redis-server --appendonly yes
   docker exec translatedsubs-redis redis-cli ping
   ```

   Вторая команда должна вывести `PONG`.

   Настройте учетные данные моделей и параметры службы после запуска в настройках или через API. Они сохраняются в Redis и применяются без перезапуска.

Запустите API и веб-интерфейс:

```powershell
uv run --locked python -m src.cli --host 127.0.0.1 --port 8000
```

Откройте <http://127.0.0.1:8000/>. Проверьте работу сервиса:

```powershell
curl.exe http://127.0.0.1:8000/api/health
curl.exe http://127.0.0.1:8000/api/health/ready
```

## Быстрый запуск с Docker

Выполните команды в корне репозитория:

```bash
cp config.example.json config.local.json
```

Измените `config.local.json`, чтобы API использовала Redis из Compose и пути `/data`:

```json
{
  "redis": {
    "url": "redis://redis:6379/0",
    "namespace": "translatedsubs-local"
  },
  "paths": {
    "dataDir": "/data",
    "dbPath": "/data/db/app.db"
  },
  "http": {
    "host": "0.0.0.0",
    "port": 8000
  }
}
```

Затем запустите сервисы:

```bash
# Локальный переводчик по умолчанию не требует API-ключа; облачные ключи настраиваются только при необходимости.
docker compose up -d --build
```

Compose скачивает и запускает API и Redis; устанавливать Redis, Python, `uv` и FFmpeg на хосте не нужно. При обновлении сохраняйте постоянные тома Redis и данных. В образ по умолчанию входят только базовые зависимости; чтобы использовать дополнительные функции, добавьте нужные extras и системные пакеты в `Dockerfile` и пересоберите образ. Веса моделей скачиваются отдельно. Для перехода с версии, использовавшей `.env`, сначала выполните инструкции по [миграции с .env](../local-quick-start.md), затем запустите API.

Откройте <http://localhost:8000/>. Проверьте доступность API командой `curl http://127.0.0.1:8000/api/health`: успешный ответ содержит `"ok":true`. Эндпоинт `/api/health/ready` также показывает состояние ключа перевода, FFmpeg, хранилища и фильтра для встроенных субтитров. Подробности локальной разработки и развёртывания см. в [указателе документации (на китайском языке)](../README.md).

## Возможности

- **Обработка субтитров**: Скачивание видео, извлечение аудио, распознавание и перевод речи, создание отдельных или встроенных в видео субтитров.
- **Веб-интерфейс**: Очередь задач, отслеживание выполнения, просмотр видео, редактирование субтитров и скачивание результатов в браузере.
- **Многоязычный интерфейс**: Переключайте в боковой панели китайский (упрощённый), английский, хинди, испанский, арабский, французский, португальский и русский языки. Выбор сохраняется в браузере; при первом посещении используется язык браузера, если для развёртывания не задан язык по умолчанию. Язык по умолчанию задаётся через `UI_LOCALE` в `web/config.js`.
- **Интеграция MCP**: Codex, Claude Desktop и другие ИИ-клиенты могут создавать задачи и отслеживать их с помощью естественного языка.
- **Расширение Google Drive**: Загрузка, скачивание и организация файлов по задачам для обмена результатами в команде.

## От видео к субтитрам

1. Вставьте ссылку на страницу видео в веб-интерфейсе или загрузите локальный файл. Сначала можно проверить ссылку с помощью теста загрузки.
2. Выберите исходный и целевой языки, только перевод или двуязычные субтитры, а также отдельные или встроенные субтитры. По умолчанию речь распознаёт локальный faster-whisper. Перед первой задачей скачайте выбранную модель в настройках локальных моделей и дождитесь статуса готовности.
3. Запустите задачу и следите в очереди за скачиванием, извлечением звука, распознаванием, переводом и созданием результата. После завершения можно просмотреть видео, исправить субтитры, повторно создать результат и скачать видео и SRT. Режим «только скачать» не создаёт субтитры.

Отдельные субтитры можно включать и выключать в проигрывателе. Встроенные субтитры записываются в изображение и требуют фильтра FFmpeg `subtitles` (libass). При первом запуске может понадобиться скачать модель и обратиться к внешним сервисам. ИИ-клиенты могут использовать тот же процесс по [руководству для MCP-агентов (на китайском языке)](../mcp-agent-guide.md).

## Основные настройки и данные

Скопируйте `config.example.json` в `config.local.json` и запустите Redis. Настройте учетные данные моделей и параметры службы в настройках или через API конфигурации. Значения хранятся в Redis и применяются к новым запросам без перезапуска службы.

Файл bootstrap содержит локальные параметры Redis, путей данных и прослушивания. При обновлении сохраняйте тома данных и Redis. Не добавляйте `config.local.json`, `.env`, cookie, данные OAuth и тестовые медиафайлы в репозиторий. Для Google Drive требуется отдельный sidecar; см. [локальный запуск (на китайском языке)](../local-quick-start.md).

## Частые проблемы

- API отвечает, но задачи не запускаются: проверьте `checks` и `capabilities` в `/api/health/ready`, включая ключ, FFmpeg/FFprobe, yt-dlp и хранилище.
- `MODEL_NOT_READY`: скачайте и проверьте выбранную модель Whisper в настройках локальных моделей.
- Встроенные субтитры недоступны: установите FFmpeg с libass или выберите отдельные субтитры. Проверка фильтра: `ffmpeg -hide_banner -filters | grep ' subtitles '`.
- Ссылка не скачивается: сначала запустите тест загрузки; если нужен вход на сайт, настройте cookie file setting по [локальному руководству (на китайском языке)](../local-quick-start.md).

## Документация

Большинство руководств ниже написано на китайском языке; протокол сервиса транскрибации — на английском.

- [Указатель документации](../README.md): инструкции по развёртыванию и расширению для разных задач.
- [Быстрый локальный запуск](../local-quick-start.md): macOS/Linux, переменные окружения и Google Drive sidecar.
- [Развёртывание в Linux](../quick-start-linux.md): установка в Ubuntu/Debian, systemd, обратный прокси и устранение неполадок.
- [Сервер MCP](../mcp-server.md): stdio, Streamable HTTP и инструменты.
- [Руководство по MCP-агентам](../mcp-agent-guide.md): порядок вызовов, состояния и ошибки.
- [Google Drive sidecar](../../drive-service/README.md): API и настройка синхронизации файлов.

## Разработка

Проект использует Python 3.10–3.12, FastAPI, FFmpeg и обычный JavaScript. Для локальной разработки выполните `uv sync`, затем `uv run python -m src.cli`; тот же сервер отдаёт веб-интерфейс. Запустите тесты Python командой `uv run pytest -q`, а тесты интерфейса — `npm test` из каталога `web/`. Проверки с реальными облачными сервисами включаются отдельно; см. [AGENTS.md (на китайском языке)](../../AGENTS.md).

`src/handler/` предоставляет HTTP API; `src/core/` выполняет скачивание, распознавание и обработку субтитров; `src/service/` и `src/store/` управляют задачами и данными; `src/mcp_server/` предоставляет MCP; в `web/` находится интерфейс. Правила разработки см. в [CONTRIBUTING.md (на китайском языке)](../../.github/CONTRIBUTING.md). О проблемах безопасности сообщайте конфиденциально согласно [SECURITY.md](../../.github/SECURITY.md), а не через публичную issue.

## Лицензия и соблюдение требований

Проект распространяется по [лицензии MIT](../../LICENSE). Обрабатывайте только материалы, на доступ, скачивание, транскрибацию, перевод и повторное распространение которых у вас есть разрешение. Соблюдайте правила исходных сайтов, ограничения авторского права и применимые законы.
