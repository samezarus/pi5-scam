"use strict";

const $ = (sel) => document.querySelector(sel);

/* ---------- Вкладки ---------- */
document.querySelectorAll(".tab").forEach((tab) => {
  tab.addEventListener("click", () => {
    document.querySelectorAll(".tab").forEach((t) =>
      t.classList.toggle("active", t === tab));
    document.querySelectorAll(".panel").forEach((p) =>
      p.classList.toggle("active", p.id === tab.dataset.tab));
    if (tab.dataset.tab === "gallery") loadGallery();
    if (tab.dataset.tab === "apu") {
      loadApuModels();
      loadApuStatus();
      startApuPolling();
      if (apuRunning) startApuObjectsPolling();
    } else {
      stopApuPolling();
      stopApuObjectsPolling();
    }
  });
});

/* ---------- Живой поток ---------- */
let streamOn = false;
const streamImg = $("#stream");
const streamOff = $("#stream-off");
const btnStream = $("#btn-stream");

function setStream(on) {
  streamOn = on;
  streamImg.src = on ? "/stream.mjpg" : "";
  streamOff.style.display = on ? "none" : "flex";
  btnStream.textContent = on ? "⏹ Остановить поток" : "▶ Запустить поток";
  btnStream.classList.toggle("primary", !on);
}

btnStream.addEventListener("click", () => setStream(!streamOn));

/* ---------- Съёмка ---------- */
const btnCapture = $("#btn-capture");
const captureStatus = $("#capture-status");
let statusTimer = null;

btnCapture.addEventListener("click", async () => {
  btnCapture.disabled = true;
  captureStatus.textContent = "Снимаю…";
  try {
    const res = await fetch("/api/capture", { method: "POST" });
    const data = await res.json();
    if (!res.ok) throw new Error(data.error || res.statusText);
    captureStatus.textContent = "Готово ✓";
    toast(`Фото сохранено: ${data.name}`);
    loadGallery();
  } catch (err) {
    captureStatus.textContent = "Ошибка";
    toast("Не удалось сделать фото: " + err.message, true);
  } finally {
    btnCapture.disabled = false;
    clearTimeout(statusTimer);
    statusTimer = setTimeout(() => (captureStatus.textContent = ""), 2500);
  }
});

/* ---------- Галерея ---------- */
async function loadGallery() {
  const grid = $("#gallery-grid");
  try {
    const photos = await (await fetch("/api/photos")).json();
    grid.innerHTML = "";
    $("#gallery-count").textContent = photos.length ? `(${photos.length})` : "";
    $("#gallery-empty").style.display = photos.length ? "none" : "block";
    for (const p of photos) {
      const fig = document.createElement("figure");
      fig.className = "card";
      fig.innerHTML = `
        <img loading="lazy" src="${p.url}" alt="${p.name}">
        <figcaption>
          <span>${new Date(p.created * 1000).toLocaleString("ru-RU")}</span>
          <span>${(p.size / 1048576).toFixed(2)} МБ</span>
        </figcaption>`;
      fig.addEventListener("click", () => openLightbox(p));
      grid.appendChild(fig);
    }
  } catch {
    toast("Не удалось загрузить галерею", true);
  }
}

$("#btn-refresh").addEventListener("click", loadGallery);

/* ---------- APU (AI HAT+) ---------- */
const apuStreamImg = $("#apu-stream");
let apuRunning = false;
let apuStatusTimer = null;

function esc(s) {
  return String(s ?? "").replace(/[&<>"']/g, (c) =>
    ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
}

async function loadApuModels(selected) {
  const sel = $("#apu-model");
  try {
    const models = await (await fetch("/api/apu/models")).json();
    sel.innerHTML = "";
    for (const m of models) {
      const opt = document.createElement("option");
      opt.value = m.file;
      opt.textContent = m.name + (m.supported ? "" : " — скоро");
      opt.disabled = !m.supported;
      sel.appendChild(opt);
    }
    const target = selected ||
      [...sel.options].find((o) => !o.disabled)?.value;
    if (target) sel.value = target;
  } catch {
    toast("Не удалось загрузить модели APU", true);
  }
}

function setApuUI(on) {
  apuRunning = on;
  apuStreamImg.src = on ? "/stream-apu.mjpg" : "";
  $("#apu-off").style.display = on ? "none" : "flex";
  const btn = $("#btn-apu");
  btn.textContent = on ? "⏹ Остановить" : "▶ Запустить детекцию";
  btn.classList.toggle("primary", !on);
  btn.classList.toggle("danger", on);
  if (on) {
    startApuObjectsPolling();
  } else {
    $("#apu-stats").classList.add("hidden");
    $("#apu-objects").classList.add("hidden");
    stopApuObjectsPolling();
  }
}

async function toggleApu() {
  const btn = $("#btn-apu");
  btn.disabled = true;
  try {
    const res = await fetch("/api/apu/detect", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        running: !apuRunning,
        model: $("#apu-model").value,
      }),
    });
    const data = await res.json();
    if (!res.ok) throw new Error(data.error || res.statusText);
    setApuUI(data.running);
    if (data.running) loadApuStatus();
  } catch (err) {
    toast("APU: " + err.message, true);
  } finally {
    btn.disabled = false;
  }
}

$("#btn-apu").addEventListener("click", toggleApu);
$("#apu-model").addEventListener("change", async () => {
  // смена модели на лету: перезапуск, если детекция идёт
  if (!apuRunning) return;
  await toggleApu();          // остановить…
  await toggleApu();          // …и запустить с новой моделью
});

function startApuPolling() {
  stopApuPolling();
  apuStatusTimer = setInterval(loadApuStatus, 4000);
}

function stopApuPolling() {
  if (apuStatusTimer) clearInterval(apuStatusTimer);
  apuStatusTimer = null;
}

/* ---------- Список найденных объектов ---------- */
let apuObjectsTimer = null;

function startApuObjectsPolling() {
  stopApuObjectsPolling();
  loadApuObjects();
  apuObjectsTimer = setInterval(loadApuObjects, 1000);
}

function stopApuObjectsPolling() {
  if (apuObjectsTimer) clearInterval(apuObjectsTimer);
  apuObjectsTimer = null;
}

function fmtClock(epochSec) {
  return epochSec
    ? new Date(epochSec * 1000).toLocaleTimeString("ru-RU")
    : "—";
}

function renderApuObjects(d) {
  const box = $("#apu-objects");
  if (!d.running) {
    box.classList.add("hidden");
    return;
  }
  box.classList.remove("hidden");
  $("#apu-objects-total").textContent = d.objects ? `всего: ${d.objects}` : "";

  // агрегаты текущего кадра: метка → {count, color}
  const agg = new Map();
  for (const det of d.detections) {
    const a = agg.get(det.label) || { count: 0, color: det.color };
    a.count += 1;
    agg.set(det.label, a);
  }
  $("#apu-objects-chips").innerHTML = [...agg.entries()]
    .map(([label, a]) =>
      `<span class="apu-chip" style="--chip:${esc(a.color)}">${esc(label)}` +
      `${a.count > 1 ? ` ×${a.count}` : ""}</span>`)
    .join("");

  $("#apu-objects-list").innerHTML = d.detections
    .map((det) => {
      const w = det.box[2] - det.box[0];
      const h = det.box[3] - det.box[1];
      return `<li><span class="dot" style="background:${esc(det.color)}"></span>` +
        `<span class="lbl">${esc(det.label)}</span>` +
        `<span class="pct">${Math.round(det.score * 100)}%</span>` +
        `<span class="dim">${w}×${h} px</span></li>`;
    })
    .join("");
  $("#apu-objects-empty").style.display = d.objects ? "none" : "block";

  // накопленные счётчики за сессию
  const sess = d.session || { since: 0, frames: 0, labels: {} };
  const rows = Object.entries(sess.labels)
    .sort((a, b) => b[1].count - a[1].count);
  $("#apu-session-summary").textContent =
    `кадров: ${sess.frames} · старт: ${fmtClock(sess.since)}`;
  $("#apu-session-rows").innerHTML = rows
    .map(([label, s]) =>
      `<tr><td>${esc(label)}</td><td>${s.count}</td>` +
      `<td>${Math.round((s.score_max || 0) * 100)}%</td>` +
      `<td>${fmtClock(s.last_seen)}</td></tr>`)
    .join("");
  $("#apu-session-empty").style.display = rows.length ? "none" : "block";
}

async function loadApuObjects() {
  try {
    renderApuObjects(await (await fetch("/api/apu/objects")).json());
  } catch { /* тихо: вкладка может быть неактивна */ }
}

function apuCard(title, bodyHtml) {
  return `<div class="apu-card"><h3>${title}</h3>${bodyHtml}</div>`;
}

function apuRows(pairs) {
  return `<dl>${pairs
    .filter(([, v]) => v !== undefined && v !== null && v !== "")
    .map(([k, v]) => `<div><dt>${esc(k)}</dt><dd>${v}</dd></div>`)
    .join("")}</dl>`;
}

function renderApuStats(d) {
  const el = $("#apu-stats");
  if (!d.running) { el.classList.add("hidden"); return; }
  el.classList.remove("hidden");
  el.textContent =
    `${d.model} · ${d.fps} fps · инференс ${d.infer_ms} мс · объектов: ${d.objects}`;
}

function renderApuCards(s) {
  const sys = s.system, dev = s.device, det = s.detector;
  const cards = [];

  // Состояние
  const ok = sys.present;
  const hr = sys.hailort;
  cards.push(apuCard("Состояние", apuRows([
    ["Ускоритель", `<span class="apu-dot ${ok ? "ok" : "err"}"></span>${ok ? "обнаружен" : "не найден"}`],
    ["Устройство", sys.device_nodes.join(", ")],
    ["HailoRT", hr.available ? `v${hr.version}` : "не установлен"],
    ["Детекция", det.running ? `работает (${det.model})` : "остановлена"],
  ])));

  // PCIe
  const pci = sys.pci[0];
  if (pci) cards.push(apuCard("PCIe-подключение", apuRows([
    ["Шина", pci.slot],
    ["Устройство", `Hailo ${esc(pci.ids)}`],
    ["Драйвер", pci.driver],
    ["Канал", pci.link],
    ["Пропускная способность", pci.bandwidth],
  ])));

  // Чип и прошивка
  const fwFiles = sys.firmware
    .map((f) => `${esc(f.name)} (${(f.size / 1024).toFixed(0)} КБ)`)
    .join("<br>");
  if (dev.available) {
    cards.push(apuCard("Чип и прошивка", apuRows([
      ["Плата", esc(dev.board_name || dev.product_name)],
      ["Архитектура", esc(dev.device_architecture)],
      ["Прошивка", esc(dev.firmware_version)],
      ["Part Number", esc(dev.part_number)],
      ["Серийный номер", esc(dev.serial_number)],
      ["Файл прошивки", fwFiles],
    ])));
    cards.push(apuCard("Телеметрия", apuRows([
      ["Температура", `ts0 ${dev.temperature.ts0} °C · ts1 ${dev.temperature.ts1} °C`],
      ["Частота NN-ядра", `${dev.nn_core_clock_mhz} МГц`],
      ["Потребление", "не поддерживается платой"],
    ])));
  } else {
    cards.push(apuCard("Чип и прошивка", apuRows([
      ["Файл прошивки", fwFiles],
      ["HailoRT", esc(dev.error || "недоступен")],
    ])));
  }

  // Характеристики
  const sp = s.specs;
  cards.push(apuCard("Характеристики", apuRows([
    ["Плата", esc(sp.product)],
    ["Чип", esc(sp.chip)],
    ["Производительность", esc(sp.performance)],
    ["Интерфейс", esc(sp.interface)],
    ["Примечание", esc(sp.note)],
  ])));

  // Возможности
  cards.push(apuCard("Что умеет APU",
    `<ul>${s.capabilities.map((c) => `<li>${esc(c)}</li>`).join("")}</ul>` +
    (sys.journal.length
      ? `<div class="journal">${esc(sys.journal.slice(-3).join("\n"))}</div>`
      : "")));

  $("#apu-cards").innerHTML = cards.join("");
}

async function loadApuStatus() {
  try {
    const s = await (await fetch("/api/apu/status")).json();
    renderApuCards(s);
    renderApuStats(s.detector);
    if (s.detector.running !== apuRunning) {
      setApuUI(s.detector.running);
      if (s.detector.running) await loadApuModels(s.detector.file);
    }
  } catch { /* тихо: вкладка может быть неактивна */ }
}

/* ---------- Полноэкранный просмотр ---------- */
const lightbox = $("#lightbox");
let currentPhoto = null;

function openLightbox(photo) {
  currentPhoto = photo;
  $("#lightbox-img").src = photo.url;
  $("#lightbox-title").textContent =
    `${photo.name} · ${(photo.size / 1048576).toFixed(2)} МБ`;
  $("#lb-download").href = photo.url;
  $("#lb-download").setAttribute("download", photo.name);
  lightbox.classList.remove("hidden");
}

function closeLightbox() {
  lightbox.classList.add("hidden");
  $("#lightbox-img").src = "";
  currentPhoto = null;
}

$("#lb-close").addEventListener("click", closeLightbox);
lightbox.addEventListener("click", (e) => {
  if (e.target === lightbox) closeLightbox();
});

$("#lb-delete").addEventListener("click", async () => {
  if (!currentPhoto) return;
  if (!confirm(`Удалить снимок ${currentPhoto.name}?`)) return;
  try {
    const res = await fetch(`/api/photos/${currentPhoto.name}`, { method: "DELETE" });
    if (!res.ok) throw new Error((await res.json()).error || res.statusText);
    toast("Снимок удалён");
    closeLightbox();
    loadGallery();
  } catch (err) {
    toast("Ошибка удаления: " + err.message, true);
  }
});

/* ---------- Уведомления ---------- */
let toastTimer = null;

function toast(message, isError = false) {
  const el = $("#toast");
  el.textContent = message;
  el.classList.toggle("error", isError);
  el.classList.remove("hidden");
  clearTimeout(toastTimer);
  toastTimer = setTimeout(() => el.classList.add("hidden"), 3000);
}

/* ---------- Настройки ---------- */
const settingsModal = $("#settings");
let currentSettings = null;

function fillSelect(sel, options, current) {
  sel.innerHTML = "";
  for (const o of options) {
    const opt = document.createElement("option");
    opt.value = `${o.width}x${o.height}`;
    opt.textContent = `${o.width} × ${o.height}`;
    opt.selected = o.width === current.width && o.height === current.height;
    sel.appendChild(opt);
  }
}

async function openSettings() {
  try {
    currentSettings = await (await fetch("/api/settings")).json();
  } catch {
    toast("Не удалось загрузить настройки", true);
    return;
  }
  fillSelect($("#set-stream"), currentSettings.options.stream, currentSettings.stream);
  fillSelect($("#set-photo"), currentSettings.options.photo, currentSettings.photo);
  settingsModal.classList.remove("hidden");
}

function closeSettings() {
  settingsModal.classList.add("hidden");
}

function updateHints(s) {
  $("#hint-stream").textContent =
    `${s.stream.width}×${s.stream.height} @ ${s.framerate} fps`;
  $("#hint-photo").textContent = `${s.photo.width}×${s.photo.height}`;
}

$("#btn-settings").addEventListener("click", openSettings);
$("#set-close").addEventListener("click", closeSettings);
settingsModal.addEventListener("click", (e) => {
  if (e.target === settingsModal) closeSettings();
});

$("#set-save").addEventListener("click", async () => {
  const [sw, sh] = $("#set-stream").value.split("x").map(Number);
  const [pw, ph] = $("#set-photo").value.split("x").map(Number);
  const btn = $("#set-save");
  btn.disabled = true;
  try {
    const res = await fetch("/api/settings", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        stream_width: sw, stream_height: sh,
        photo_width: pw, photo_height: ph,
      }),
    });
    const data = await res.json();
    if (!res.ok) throw new Error(data.error || res.statusText);
    currentSettings = data;
    updateHints(data);
    closeSettings();
    toast(`Применено: поток ${sw}×${sh}, фото ${pw}×${ph}`);
    if (streamOn) streamImg.src = `/stream.mjpg?t=${Date.now()}`;
  } catch (err) {
    toast("Не удалось применить настройки: " + err.message, true);
  } finally {
    btn.disabled = false;
  }
});

/* ---------- Инициализация ---------- */
setStream(true);
loadGallery();
loadApuModels();
