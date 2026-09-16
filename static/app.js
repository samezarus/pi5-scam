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
