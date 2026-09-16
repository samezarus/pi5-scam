# pi5-scam

Веб-приложение (MVP) для камеры **IMX219** (разъём **cam0**) на **Raspberry Pi 5**:
живой видеопоток и полноразмерные снимки прямо из браузера — с телефона или компьютера
в той же сети.

## Возможности

- 📹 Живой MJPEG-поток **на всю страницу** (по умолчанию 1280×720 @ 30 fps, задержка ~0.1–0.3 с)
- 🎛 Все элементы управления — в одной панели сверху: вкладки, старт/стоп потока, съёмка, настройки
- 📸 Снимки в полном разрешении сенсора **3280×2464** (JPEG)
- 🖼 Галерея снимков: просмотр в полном размере, скачивание, удаление
- ⚙️ Разрешение потока и фото меняется в веб-интерфейсе (применяется сразу,
  сохраняется в `.env` и переживает перезапуск); остальные параметры — через `.env`

## Структура проекта

```
pi5-scam/
├── app.py               # Flask + Picamera2 (стрим, снимки, API)
├── templates/index.html # страница интерфейса
├── static/              # style.css, app.js
├── photos/              # снимки (создаётся автоматически)
├── venv/                # виртуальное окружение (в git не попадает)
├── .env                 # локальные переменные окружения
├── .env.example         # шаблон переменных
└── requirements.txt     # зависимости pip (flask, python-dotenv)
```

## Требования

- Raspberry Pi 5, Raspberry Pi OS (Bookworm/Trixie)
- Камера IMX219, подключённая к разъёму **cam0**
- Системные пакеты `python3-picamera2` и `rpicam-apps` (в Raspberry Pi OS уже есть)

Проверить, что камера видна системе:

```bash
rpicam-hello --list-cameras
# ожидаем: 0 : imx219 [3280x2464 10-bit RGGB] (...)
```

## Быстрый старт

```bash
cd ~/PROJECTS/pi5-scam

# 1. Виртуальное окружение.
#    Флаг --system-site-packages обязателен: picamera2 установлен через apt и
#    завязан на системные библиотеки libcamera, поэтому pip'ом его не ставят.
python3 -m venv --system-site-packages .venv

source .venv/bin/activate

# 2. Зависимости (flask, python-dotenv)
pip install -r requirements.txt

# 3. Переменные окружения (шаг опционален — .env уже настроен)
cp .env.example .env

# 4. Запуск
python3 app.py
```

Открыть в браузере: `http://<ip-адрес-pi>:8001` (узнать IP: `hostname -I`).

## Переменные окружения (`.env`)

| Переменная | По умолчанию | Описание |
|---|---|---|
| `HOST` | `0.0.0.0` | интерфейс веб-сервера |
| `PORT` | `8001` | порт веб-сервера |
| `CAMERA_NUM` | `0` | индекс камеры (IMX219 на cam0 = 0) |
| `STREAM_WIDTH` | `1280` | ширина живого потока (также в UI: ⚙ Настройки) |
| `STREAM_HEIGHT` | `720` | высота живого потока (также в UI: ⚙ Настройки) |
| `PHOTO_WIDTH` | `3280` | ширина снимков (также в UI: ⚙ Настройки) |
| `PHOTO_HEIGHT` | `2464` | высота снимков (также в UI: ⚙ Настройки) |
| `STREAM_FRAMERATE` | `30` | частота кадров потока |
| `JPEG_QUALITY` | `70` | качество JPEG кадров потока (1–95) |
| `PHOTO_DIR` | `photos` | папка для снимков |
| `DEBUG` | `false` | режим отладки Flask |

## API

| Метод | Путь | Описание |
|---|---|---|
| GET | `/` | веб-страница |
| GET | `/stream.mjpg` | живой MJPEG-поток |
| POST | `/api/capture` | сделать снимок → JSON `{name, url, size, created}` |
| GET | `/api/settings` | текущие настройки и список допустимых разрешений |
| POST | `/api/settings` | применить разрешения (`stream_width`, `stream_height`, `photo_width`, `photo_height`) и сохранить их в `.env` |
| GET | `/api/photos` | список снимков (JSON, новые сверху) |
| GET | `/photos/<имя>` | файл снимка |
| DELETE | `/api/photos/<имя>` | удалить снимок |

Пример из терминала:

```bash
curl -X POST http://localhost:8001/api/capture
curl http://localhost:8001/api/photos
```

## Автозапуск (опционально)

Файл `/etc/systemd/system/pi5-scam.service`:

```ini
[Unit]
Description=pi5-scam web camera
After=network.target

[Service]
WorkingDirectory=/home/sameza/PROJECTS/pi5-scam
ExecStart=/home/sameza/PROJECTS/pi5-scam/venv/bin/python app.py
Restart=on-failure

[Install]
WantedBy=multi-user.target
```

```bash
sudo systemctl daemon-reload
sudo systemctl enable --now pi5-scam
```

## Устранение неполадок

- **Камера не найдена** (`No cameras available`): проверьте шлейф и выполните
  `rpicam-hello --list-cameras`, при необходимости перезагрузите Pi.
- **`Camera in use`**: камеру держит другая программа (`rpicam-*`, второй
  экземпляр приложения) — закройте её.
- **Порт занят**: поменяйте `PORT` в `.env`.
- **Тёмные первые кадры**: автоэкспозиции нужно 1–2 секунды на стабилизацию.
