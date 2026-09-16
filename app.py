#!/usr/bin/env python3
"""pi5-scam — веб-интерфейс камеры IMX219 на Raspberry Pi 5.

Живой MJPEG-поток и полноразмерные снимки прямо из браузера.
Запуск: venv/bin/python app.py, страница: http://<ip-pi>:8001
"""

import atexit
import logging
import os
import re
import threading
import time
from datetime import datetime
from pathlib import Path

from dotenv import load_dotenv, set_key
from flask import (Flask, Response, jsonify, render_template, request,
                   send_from_directory)
from picamera2 import Picamera2
from simplejpeg import encode_jpeg

# ---------------------------------------------------------------- конфигурация
BASE_DIR = Path(__file__).resolve().parent
ENV_PATH = BASE_DIR / ".env"          # сюда пишутся настройки из веб-интерфейса
load_dotenv(ENV_PATH)

HOST = os.getenv("HOST", "0.0.0.0")
PORT = int(os.getenv("PORT", "8001"))
CAMERA_NUM = int(os.getenv("CAMERA_NUM", "0"))
# Начальные размеры потока и фото (дальше живут в Camera, меняются из UI)
STREAM_WIDTH = int(os.getenv("STREAM_WIDTH", "1280"))
STREAM_HEIGHT = int(os.getenv("STREAM_HEIGHT", "720"))
PHOTO_WIDTH = int(os.getenv("PHOTO_WIDTH", "3280"))
PHOTO_HEIGHT = int(os.getenv("PHOTO_HEIGHT", "2464"))
STREAM_FRAMERATE = int(os.getenv("STREAM_FRAMERATE", "30"))
JPEG_QUALITY = int(os.getenv("JPEG_QUALITY", "70"))
PHOTO_DIR = BASE_DIR / os.getenv("PHOTO_DIR", "photos")
DEBUG = os.getenv("DEBUG", "false").lower() in ("1", "true", "yes")

# Допустимые разрешения (режимы сенсора IMX219)
STREAM_RESOLUTIONS = [(640, 480), (1280, 720), (1640, 1232), (1920, 1080)]
PHOTO_RESOLUTIONS = [(640, 480), (1640, 1232), (1920, 1080), (3280, 2464)]

PHOTO_DIR.mkdir(parents=True, exist_ok=True)

logging.basicConfig(level=logging.DEBUG if DEBUG else logging.INFO,
                    format="%(asctime)s [%(levelname)s] %(message)s")
log = logging.getLogger("pi5-scam")

# Формат кадров потока и соответствующий colorspace для simplejpeg
# (таблица соответствий из picamera2.encoders.JpegEncoder).
FRAME_FORMAT = "XBGR8888"
FRAME_COLORSPACE = "RGBX"

PHOTO_NAME_RE = re.compile(r"^photo_\d{8}_\d{6}_\d{6}\.jpg$")

# ---------------------------------------------------------------------- камера
class Camera:
    """Один Picamera2: видео-конфигурация для потока, still — для фото.

    Фоновый поток непрерывно кодирует кадры в JPEG (для MJPEG-стрима).
    Снимок делается через switch_mode_and_capture_file: камера переключается
    в полный размер сенсора, сохраняет JPEG и возвращается к видео-режиму.
    """

    def __init__(self, camera_num: int, stream_size, photo_size):
        self.stream_size = tuple(stream_size)   # текущее разрешение потока
        self.photo_size = tuple(photo_size)     # текущее разрешение фото
        self.picam2 = Picamera2(camera_num=camera_num)
        self.video_config = self._make_video_config()
        self.still_config = self._make_still_config()
        self.picam2.configure(self.video_config)
        self.picam2.start()

        self._cam_lock = threading.Lock()    # доступ к камере (кадры/снимок)
        self._frame_lock = threading.Lock()  # последний JPEG-кадр
        self._latest_jpeg = None
        self._stop = threading.Event()
        self._thread = threading.Thread(target=self._capture_loop, daemon=True)
        self._thread.start()
        model = self.picam2.camera_properties.get("Model", "?")
        log.info("Камера %d (%s): поток %dx%d@%d, фото %s",
                 camera_num, model, *self.stream_size,
                 STREAM_FRAMERATE, self.still_size)

    @property
    def still_size(self) -> str:
        return f"{self.photo_size[0]}x{self.photo_size[1]}"

    def _make_video_config(self):
        return self.picam2.create_video_configuration(
            main={"size": self.stream_size, "format": FRAME_FORMAT},
            controls={"FrameRate": STREAM_FRAMERATE},
        )

    def _make_still_config(self):
        return self.picam2.create_still_configuration(
            main={"size": self.photo_size})

    def set_stream_size(self, size) -> None:
        """Меняет разрешение потока «на лету» (с переконфигурацией камеры)."""
        with self._cam_lock:
            self.picam2.stop()   # безопасен, даже если камера уже остановлена
            self.stream_size = tuple(size)
            self.video_config = self._make_video_config()
            self.picam2.configure(self.video_config)
            self.picam2.start()

    def set_photo_size(self, size) -> None:
        """Меняет разрешение фото (используется при следующем снимке)."""
        with self._cam_lock:
            self.photo_size = tuple(size)
            self.still_config = self._make_still_config()

    def _capture_loop(self):
        while not self._stop.is_set():
            try:
                with self._cam_lock:
                    frame = self.picam2.capture_array("main").copy()
                jpeg = encode_jpeg(frame, quality=JPEG_QUALITY,
                                   colorspace=FRAME_COLORSPACE)
                with self._frame_lock:
                    self._latest_jpeg = jpeg
            except Exception as exc:  # камера временно в still-режиме
                log.debug("Кадр пропущен: %s", exc)
                time.sleep(0.05)

    def latest_jpeg(self):
        with self._frame_lock:
            return self._latest_jpeg

    def capture_photo(self, path: Path) -> None:
        with self._cam_lock:
            self.picam2.switch_mode_and_capture_file(self.still_config, str(path))

    def close(self):
        self._stop.set()
        self._thread.join(timeout=2)
        try:
            self.picam2.stop()
        except Exception:
            pass


camera = Camera(CAMERA_NUM, (STREAM_WIDTH, STREAM_HEIGHT),
                (PHOTO_WIDTH, PHOTO_HEIGHT))
atexit.register(camera.close)

# ----------------------------------------------------------------------- Flask
app = Flask(__name__)


@app.get("/")
def index():
    return render_template(
        "index.html",
        stream=f"{camera.stream_size[0]}×{camera.stream_size[1]} @ {STREAM_FRAMERATE} fps",
        photo=camera.still_size,
    )


@app.get("/stream.mjpg")
def stream():
    def generate():
        while True:
            jpeg = camera.latest_jpeg()
            if jpeg is None:
                time.sleep(0.05)
                continue
            yield (b"--frame\r\nContent-Type: image/jpeg\r\n"
                   b"Content-Length: " + str(len(jpeg)).encode() + b"\r\n\r\n"
                   + jpeg + b"\r\n")
            time.sleep(1.0 / STREAM_FRAMERATE)

    return Response(generate(),
                    mimetype="multipart/x-mixed-replace; boundary=frame")


def _settings_payload() -> dict:
    return {
        "stream": {"width": camera.stream_size[0], "height": camera.stream_size[1]},
        "photo": {"width": camera.photo_size[0], "height": camera.photo_size[1]},
        "framerate": STREAM_FRAMERATE,
        "jpeg_quality": JPEG_QUALITY,
        "options": {
            "stream": [{"width": w, "height": h} for w, h in STREAM_RESOLUTIONS],
            "photo": [{"width": w, "height": h} for w, h in PHOTO_RESOLUTIONS],
        },
    }


@app.get("/api/settings")
def get_settings():
    return jsonify(_settings_payload())


@app.post("/api/settings")
def update_settings():
    data = request.get_json(silent=True) or {}
    stream_wh = (data.get("stream_width"), data.get("stream_height"))
    photo_wh = (data.get("photo_width"), data.get("photo_height"))
    if stream_wh not in STREAM_RESOLUTIONS:
        return jsonify(error="Недопустимое разрешение потока"), 400
    if photo_wh not in PHOTO_RESOLUTIONS:
        return jsonify(error="Недопустимое разрешение фото"), 400
    try:
        if stream_wh != camera.stream_size:
            camera.set_stream_size(stream_wh)
        if photo_wh != camera.photo_size:
            camera.set_photo_size(photo_wh)
    except Exception as exc:
        log.exception("Ошибка применения настроек")
        return jsonify(error=str(exc)), 500
    # сохранить в .env, чтобы настройки пережили перезапуск
    set_key(ENV_PATH, "STREAM_WIDTH", str(stream_wh[0]))
    set_key(ENV_PATH, "STREAM_HEIGHT", str(stream_wh[1]))
    set_key(ENV_PATH, "PHOTO_WIDTH", str(photo_wh[0]))
    set_key(ENV_PATH, "PHOTO_HEIGHT", str(photo_wh[1]))
    log.info("Настройки применены: поток %dx%d, фото %dx%d",
             *stream_wh, *photo_wh)
    return jsonify(_settings_payload())


@app.post("/api/capture")
def capture():
    try:
        name = datetime.now().strftime("photo_%Y%m%d_%H%M%S_%f") + ".jpg"
        path = PHOTO_DIR / name
        camera.capture_photo(path)
    except Exception as exc:
        log.exception("Ошибка съёмки")
        return jsonify(error=str(exc)), 500
    stat = path.stat()
    log.info("Снимок: %s (%.2f МБ)", name, stat.st_size / 1e6)
    return jsonify(name=name, url=f"/photos/{name}",
                   size=stat.st_size, created=int(stat.st_mtime))


@app.get("/api/photos")
def photos_list():
    items = [{"name": p.name, "url": f"/photos/{p.name}",
              "size": p.stat().st_size, "created": int(p.stat().st_mtime)}
             for p in sorted(PHOTO_DIR.glob("photo_*.jpg"), reverse=True)]
    return jsonify(items)


@app.get("/photos/<name>")
def photo_file(name):
    if not PHOTO_NAME_RE.match(name):
        return jsonify(error="Недопустимое имя файла"), 400
    return send_from_directory(PHOTO_DIR, name)


@app.delete("/api/photos/<name>")
def photo_delete(name):
    if not PHOTO_NAME_RE.match(name):
        return jsonify(error="Недопустимое имя файла"), 400
    path = PHOTO_DIR / name
    if not path.is_file():
        return jsonify(error="Файл не найден"), 404
    path.unlink()
    log.info("Снимок удалён: %s", name)
    return jsonify(ok=True)


if __name__ == "__main__":
    # use_reloader=False: reloader повторно импортирует модуль и пытается
    # открыть уже занятую камеру
    app.run(host=HOST, port=PORT, debug=DEBUG, threaded=True, use_reloader=False)
