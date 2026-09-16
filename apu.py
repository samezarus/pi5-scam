#!/usr/bin/env python3
"""APU — Raspberry Pi AI HAT+ (26 TOPS, Hailo-8): диагностика и live-детекция.

Модуль изолирует всё, что связано с ускорителем:
- каталог HEF-моделей из пакета hailo-models (для чипа Hailo-8);
- системная диагностика: sysfs/PCIe, файлы прошивки, журнал ядра;
- телеметрия и идентификация чипа через HailoRT;
- ApuDetector — фоновый цикл «кадр камеры → YOLO на Hailo → рамки → JPEG».
"""

import gc
import logging
import subprocess
import threading
import time
from pathlib import Path

import numpy as np
from PIL import Image, ImageDraw, ImageFont
from simplejpeg import encode_jpeg

try:
    from hailo_platform import Device as HailoDevice, FormatType, VDevice
    HAVE_HAILORT = True
except ImportError:          # apt: python3-hailort
    HAVE_HAILORT = False

log = logging.getLogger("pi5-scam.apu")

MODELS_DIR = Path("/usr/share/hailo-models")   # пакет hailo-models
FW_DIR = Path("/lib/firmware/hailo")           # прошивка загружается драйвером
HAILO_VENDOR_ID = "0x1e60"
SCORE_THRESHOLD = 0.45     # минимальная уверенность для отрисовки
MAX_FPS = 25.0             # предел цикла детекции (камера всё равно 30 fps)
PCI_SPEED_GEN = {"2.5 GT/s PCIe": "Gen1", "5 GT/s PCIe": "Gen2",
                 "8.0 GT/s PCIe": "Gen3", "16.0 GT/s PCIe": "Gen4",
                 "32.0 GT/s PCIe": "Gen5"}
PCI_SPEED_GT = {"Gen1": 2.5, "Gen2": 5.0, "Gen3": 8.0, "Gen4": 16.0, "Gen5": 32.0}

# Палитра рамок (циклично по индексу класса)
COLORS = ["#f85149", "#3fb950", "#2f81f7", "#d29922", "#bc8cff",
          "#39c5cf", "#ff7b72", "#7ee787", "#79c0ff", "#ffa657"]

# Классы COCO (порядок выхода NMS в yolov8s/yolov6n)
COCO_LABELS = [
    "человек", "велосипед", "автомобиль", "мотоцикл", "самолёт", "автобус",
    "поезд", "грузовик", "лодка", "светофор", "гидрант", "знак стоп",
    "паркомат", "скамейка", "птица", "кошка", "собака", "лошадь", "овца",
    "корова", "слон", "медведь", "зебра", "жираф", "рюкзак", "зонт",
    "сумка", "галстук", "чемодан", "фрисби", "лыжи", "сноуборд", "мяч",
    "воздушный змей", "бита", "бейсбольная перчатка", "скейтборд",
    "сёрфборд", "ракетка", "бутылка", "бокал", "чашка", "вилка", "нож",
    "ложка", "миска", "банан", "яблоко", "сэндвич", "апельсин", "брокколи",
    "морковь", "хот-дог", "пицца", "пончик", "торт", "стул", "диван",
    "растение в горшке", "кровать", "стол", "туалет", "телевизор",
    "ноутбук", "мышь", "пульт", "клавиатура", "телефон", "микроволновка",
    "духовка", "тостер", "раковина", "холодильник", "книга", "часы",
    "ваза", "ножницы", "медвежонок", "фен", "зубная щётка",
]


# ---------------------------------------------------------------- каталог моделей
def list_models() -> list:
    """HEF-модели, установленные для чипа Hailo-8 (пакет hailo-models).

    supported=False у моделей, чей выход ещё не обрабатывается (позы,
    сегментация) — они видны в UI, но заблокированы.
    """
    models = []
    if not MODELS_DIR.is_dir():
        return models
    for hef in sorted(MODELS_DIR.glob("*_h8.hef")):
        stem = hef.stem
        if "pose" in stem:
            kind, title, supported = "pose", "YOLOv8s Pose — позы людей", False
        elif "seg" in stem:
            kind, title, supported = ("segmentation",
                                      "YOLOv5n Seg — сегментация", False)
        elif "yolov8s" in stem:
            kind, title, supported = "detection", "YOLOv8s — детекция (80 классов)", True
        elif "yolov6n" in stem:
            kind, title, supported = "detection", "YOLOv6n — детекция (быстрая)", True
        else:
            kind, title, supported = "detection", stem, True
        models.append({"file": hef.name, "name": title,
                       "type": kind, "supported": supported})
    return models


def default_model() -> str | None:
    """Первая поддерживаемая модель (для автозапуска без выбора)."""
    for m in list_models():
        if m["supported"]:
            return m["file"]
    return None


# ------------------------------------------------------------- системная диагностика
def _sysfs_read(path: Path) -> str:
    try:
        return path.read_text().strip()
    except OSError:
        return ""


def system_info() -> dict:
    """Данные об APU без обращения к чипу: sysfs/PCIe, прошивка, журнал ядра."""
    nodes = sorted(p.name for p in Path("/dev").glob("hailo*"))
    pci = []
    for dev in sorted(Path("/sys/bus/pci/devices").glob("*")):
        if _sysfs_read(dev / "vendor") != HAILO_VENDOR_ID:
            continue
        driver = ""
        drv_link = dev / "driver"
        if drv_link.exists():
            driver = drv_link.resolve().name
        cur_spd = _sysfs_read(dev / "current_link_speed")
        max_spd = _sysfs_read(dev / "max_link_speed")
        cur_w = _sysfs_read(dev / "current_link_width")
        max_w = _sysfs_read(dev / "max_link_width")
        cur_gen = PCI_SPEED_GEN.get(cur_spd, cur_spd)
        max_gen = PCI_SPEED_GEN.get(max_spd, max_spd)
        # пропускная способность текущего канала (8b/10b → ×0.8)
        try:
            bw = PCI_SPEED_GT[cur_gen] * int(cur_w or 1) * 0.8 / 8
            bw_str = f"≈{bw:.1f} ГБ/с"
        except (KeyError, ValueError):
            bw_str = "—"
        pci.append({
            "slot": dev.name,
            "ids": (f"{HAILO_VENDOR_ID[2:]}:"
                    f"{_sysfs_read(dev / 'device').replace('0x', '')}"),
            "driver": driver,
            "link": f"{cur_gen} ×{cur_w} (макс. {max_gen} ×{max_w})",
            "bandwidth": bw_str,
        })
    firmware = []
    if FW_DIR.is_dir():
        for f in sorted(FW_DIR.glob("*.bin")):
            firmware.append({"name": f.name, "size": f.stat().st_size,
                             "date": int(f.stat().st_mtime)})
    journal = []
    try:
        out = subprocess.run(["journalctl", "-k", "-b", "--no-pager", "-q"],
                             capture_output=True, text=True, timeout=5)
        journal = [l.strip() for l in out.stdout.splitlines()
                   if "hailo" in l.lower()][-6:]
    except Exception:
        pass
    ver = ""
    if HAVE_HAILORT:
        try:
            import hailo_platform
            ver = getattr(hailo_platform, "__version__", "")
        except Exception:
            pass
    return {
        "present": bool(pci or nodes),
        "device_nodes": nodes,
        "pci": pci,
        "firmware": firmware,
        "journal": journal,
        "hailort": {"available": HAVE_HAILORT, "version": ver},
    }


def _clean_str(v) -> str:
    """bytes/str из pybind → читаемая строка (без завершающих NUL)."""
    if isinstance(v, bytes):
        v = v.decode("utf-8", errors="replace")
    return str(v).rstrip("\x00").strip()


def device_info(vdevice=None) -> dict:
    """Идентификация и телеметрия чипа через HailoRT.

    vdevice — существующее подключение (если детектор уже работает),
    иначе открывается короткое проживание Device. На этой плате
    power_measurement НЕ поддерживается и вызывать его нельзя
    (ломает активные стримы).
    """
    if not HAVE_HAILORT:
        return {"available": False, "error": "HailoRT не установлен"}
    holder = None
    shared = None
    try:
        if vdevice is not None:
            # get_physical_devices() создаёт НОВЫЕ Device-обёртки — их
            # нужно освобождать, иначе утекают хэндлы драйвера
            shared = vdevice.get_physical_devices()[0]
            dev = shared._device
        else:
            holder = HailoDevice()
            dev = holder._device
        ident = dev.identify()
        temp = dev.get_chip_temperature()
        ext = dev.get_extended_device_information()
        fw = ident.fw_version
        fw_ver = ".".join(str(x) for x in
                          (getattr(fw, "major", ""), getattr(fw, "minor", ""))
                          if x != "")
        return {
            "available": True,
            "board_name": _clean_str(ident.board_name),
            "product_name": _clean_str(ident.product_name),
            "part_number": _clean_str(ident.part_number),
            "serial_number": _clean_str(ident.serial_number),
            "device_architecture": str(
                getattr(ident, "device_architecture", "")).split(".")[-1],
            "firmware_version": fw_ver,
            "temperature": {
                "ts0": round(temp.ts0_temperature, 1),
                "ts1": round(temp.ts1_temperature, 1),
            },
            "nn_core_clock_mhz": round(
                ext.neural_network_core_clock_rate / 1e6),
        }
    except Exception as exc:
        log.debug("Телеметрия Hailo недоступна: %s", exc)
        return {"available": False, "error": str(exc)}
    finally:
        for h in (holder, shared):
            if h is not None:
                try:
                    h.release()
                except Exception:
                    pass


# --------------------------------------------------------------- справочники (UI)
SPECS = {
    "product": "Raspberry Pi AI HAT+ (26 TOPS)",
    "chip": "Hailo-8",
    "performance": "26 TOPS (INT8)",
    "interface": "PCIe Gen 3 ×4",
    "note": "на Pi 5 канал ограничен одной линией (Gen 3 ×1)",
}

CAPABILITIES = [
    "Детекция объектов в реальном времени (YOLOv8s: ~2–4 мс на кадр)",
    "До 80 классов COCO: люди, животные, транспорт, мебель, электроника…",
    "Оценка позы и семантическая сегментация (модели из hailo-models)",
    "Параллельная обработка нескольких видеопотоков одним чипом",
    "Библиотека Hailo Model Zoo — сотни скомпилированных моделей",
    "Интеграции: GStreamer, TAPPAS, rpicam-apps, Python API",
]


# -------------------------------------------------------------------- детектор
class ApuDetector:
    """Фоновый цикл детекции: кадры камеры → YOLO на Hailo-8 → рамки → JPEG.

    Один VDevice живёт на всё время работы; смена модели — полный
    перезапуск сессии (занимает ~1 с). Порядок вызовов важен:
    power_measurement на этой плате запрещён (ломает стримы).
    """

    def __init__(self, frame_source, jpeg_quality: int = 70):
        self._frame_source = frame_source   # () → np.ndarray XBGR8888 | None
        self._jpeg_quality = jpeg_quality
        self._lifecycle = threading.Lock()  # старт/стоп/смена модели
        self._stats_lock = threading.Lock()
        self._stop = threading.Event()
        self._thread = None
        self._vdevice = None
        self._session = None
        self._bindings = None
        self._in_name = self._out_name = ""
        self._in_size = (640, 640)
        self._labels = COCO_LABELS
        self._latest_jpeg = None
        self._frame_times = []
        # накопительные счётчики за запуск детекции (сессия)
        self._session_stats = {"since": 0.0, "frames": 0, "labels": {}}
        self._stats = {"running": False, "model": "", "file": "",
                       "fps": 0.0, "infer_ms": 0.0, "objects": 0,
                       "by_label": {}, "detections": []}

    # ---- состояние (для Flask-эндпоинтов) ----
    @property
    def vdevice(self):
        return self._vdevice if self.running() else None

    def running(self) -> bool:
        return self._thread is not None and self._thread.is_alive()

    def stats(self) -> dict:
        with self._stats_lock:
            out = dict(self._stats)
            out["running"] = self.running()
            return out

    def latest_jpeg(self):
        with self._stats_lock:
            return self._latest_jpeg

    def objects(self) -> dict:
        """Лёгкий снимок для частого опроса из UI (без системной диагностики).

        detections — объекты текущего кадра (label/score/box/color),
        session — накопленные за запуск детекции счётчики по меткам.
        """
        with self._stats_lock:
            return {
                "running": self.running(),
                "model": self._stats["model"],
                "objects": self._stats["objects"],
                "by_label": self._stats["by_label"],
                "detections": [dict(d) for d in self._stats["detections"]],
                "session": {
                    "since": self._session_stats["since"],
                    "frames": self._session_stats["frames"],
                    "labels": {k: dict(v)
                               for k, v in self._session_stats["labels"].items()},
                },
            }

    # ---- жизненный цикл ----
    def start(self, model_file: str) -> None:
        with self._lifecycle:
            if self.running():
                self._teardown_locked()
            self._setup_locked(model_file)
            self._stop.clear()
            self._thread = threading.Thread(target=self._loop,
                                            name="apu-detector", daemon=True)
            self._thread.start()
            log.info("APU-детекция запущена: %s", model_file)

    def stop(self) -> None:
        with self._lifecycle:
            if self.running() or self._vdevice is not None:
                log.info("APU-детекция остановлена")
            self._teardown_locked()

    def _setup_locked(self, model_file: str) -> None:
        if not HAVE_HAILORT:
            raise RuntimeError(
                "HailoRT не установлен (apt install python3-hailort)")
        path = MODELS_DIR / model_file
        if not path.is_file():
            raise FileNotFoundError(f"Модель не найдена: {model_file}")
        vdevice = VDevice()
        try:
            infer = vdevice.create_infer_model(str(path))
            in_stream = list(infer.inputs)[0]
            nms_stream = next((s for s in infer.outputs if s.is_nms), None)
            if nms_stream is None:
                raise RuntimeError("У модели нет NMS-выхода (не детектор)")
            nms_stream.set_format_type(FormatType.FLOAT32)
            session = infer.configure()
            session.activate()
            bindings = session.create_bindings()
            h, w, _ = tuple(in_stream.shape)
            bindings.input(in_stream.name).set_buffer(
                np.zeros((h, w, 3), dtype=np.uint8))
            bindings.output(nms_stream.name).set_buffer(
                np.empty(int(np.prod(nms_stream.shape)), dtype=np.float32))
        except Exception:
            try:
                vdevice.release()
            except Exception:
                pass
            raise
        self._vdevice = vdevice
        self._session = session
        self._bindings = bindings
        self._in_name = in_stream.name
        self._out_name = nms_stream.name
        self._in_size = (w, h)
        self._frame_times = []
        title = model_file.removesuffix(".hef")
        with self._stats_lock:
            self._stats = {"running": True, "model": title,
                           "file": model_file, "fps": 0.0, "infer_ms": 0.0,
                           "objects": 0, "by_label": {}, "detections": []}
            self._session_stats = {"since": time.time(), "frames": 0,
                                   "labels": {}}

    def _teardown_locked(self) -> None:
        if self._thread is not None:
            self._stop.set()
            self._thread.join(timeout=5)
            self._thread = None
        # Порядок критичен: сначала shutdown, затем ЯВНОЕ уничтожение
        # bindings/session (gc.collect()) и только потом release VDevice —
        # иначе C++-деструкторы отработают после освобождения устройства
        # и процесс падает без traceback (проверено вариантами E/F).
        if self._session is not None:
            try:
                self._session.shutdown()
            except Exception as exc:
                log.debug("shutdown сессии: %s", exc)
        self._bindings = None
        self._session = None
        gc.collect()
        if self._vdevice is not None:
            try:
                self._vdevice.release()
            except Exception as exc:
                log.debug("release VDevice: %s", exc)
        self._vdevice = None
        with self._stats_lock:
            self._latest_jpeg = None
            self._stats.update({"running": False, "fps": 0.0,
                                "infer_ms": 0.0, "objects": 0,
                                "by_label": {}, "detections": []})
            self._session_stats = {"since": 0.0, "frames": 0, "labels": {}}

    # ---- рабочий цикл ----
    def _loop(self):
        while not self._stop.is_set():
            t0 = time.perf_counter()
            frame = self._frame_source()
            if frame is None:
                time.sleep(0.05)
                continue
            try:
                self._process(frame)
            except Exception as exc:
                log.warning("Кадр APU пропущен: %s", exc)
                time.sleep(0.2)
                continue
            # ограничение частоты цикла
            idle = 1.0 / MAX_FPS - (time.perf_counter() - t0)
            if idle > 0:
                time.sleep(idle)

    def _process(self, frame: np.ndarray) -> None:
        fh, fw = frame.shape[:2]
        # XBGR8888: в памяти R,G,B,X → первые 3 канала уже RGB
        rgb = np.ascontiguousarray(frame[:, :, :3])
        img = Image.fromarray(rgb)
        mw, mh = self._in_size
        inp = np.array(img.resize((mw, mh), Image.BILINEAR), dtype=np.uint8)

        bindings = self._bindings
        bindings.input(self._in_name).set_buffer(inp)
        t0 = time.perf_counter()
        self._session.run([bindings], 10000)
        infer_ms = (time.perf_counter() - t0) * 1000.0
        res = np.asarray(bindings.output(self._out_name).get_buffer(
            tf_format=True))                     # (классы, 5, N)

        # строки [y1, x1, y2, x2, score], нормализованы к входу модели
        dets = []
        for cid in range(res.shape[0]):
            block = res[cid].T
            for row in block[block[:, 4] >= SCORE_THRESHOLD]:
                y1, x1, y2, x2 = row[:4]
                dets.append((cid, float(row[4]),
                             float(x1) * fw, float(y1) * fh,
                             float(x2) * fw, float(y2) * fh))

        draw = ImageDraw.Draw(img)
        try:
            font = ImageFont.load_default(14)
        except TypeError:
            font = ImageFont.load_default()
        by_label = {}
        detections = []
        for cid, score, x1, y1, x2, y2 in dets:
            color = COLORS[cid % len(COLORS)]
            name = self._labels[cid % len(self._labels)]
            label = f"{name} {score:.0%}"
            by_label[name] = by_label.get(name, 0) + 1
            detections.append({"label": name, "class_id": cid,
                               "score": round(score, 3),
                               "box": [round(x1), round(y1),
                                       round(x2), round(y2)],
                               "color": color})
            draw.rectangle((x1, y1, x2, y2), outline=color, width=2)
            tw = draw.textlength(label, font=font)
            top = y1 - 18 if y1 >= 20 else y2
            draw.rectangle((x1, top, x1 + tw + 8, top + 17), fill=color)
            draw.text((x1 + 4, top + 1), label, fill="#0d1117", font=font)
        detections.sort(key=lambda d: d["score"], reverse=True)

        jpeg = encode_jpeg(np.asarray(img), quality=self._jpeg_quality,
                           colorspace="RGB")

        now = time.perf_counter()
        self._frame_times.append(now)
        self._frame_times = [t for t in self._frame_times if now - t <= 3.0]
        fps = len(self._frame_times) / 3.0
        wall = time.time()
        with self._stats_lock:
            self._latest_jpeg = jpeg
            self._stats.update({"fps": round(fps, 1),
                                "infer_ms": round(infer_ms, 1),
                                "objects": len(dets), "by_label": by_label,
                                "detections": detections})
            # накопительные счётчики за сессию детекции
            sess = self._session_stats
            sess["frames"] += 1
            for det in detections:
                s = sess["labels"].get(det["label"])
                if s is None:
                    s = sess["labels"][det["label"]] = {
                        "count": 0, "score_max": 0.0,
                        "first_seen": wall, "last_seen": wall}
                s["count"] += 1
                s["last_seen"] = wall
                if det["score"] > s["score_max"]:
                    s["score_max"] = det["score"]


