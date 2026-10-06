const params = new URLSearchParams(window.location.search);
const apiBase = (params.get("api") || "").replace(/\/$/, "");
const tg = window.Telegram && window.Telegram.WebApp;

function isInsideTelegram() {
  if (!tg) return false;
  if (tg.initData) return true;
  const unsafe = tg.initDataUnsafe || {};
  if (unsafe.user || unsafe.query_id || unsafe.start_param) return true;
  if (tg.platform && tg.platform !== "unknown") return true;
  return false;
}

function setMainButtonLoading(loading) {
  if (!tg || !tg.MainButton) return;
  if (loading && typeof tg.MainButton.showProgress === "function") {
    tg.MainButton.showProgress(false);
    return;
  }
  if (!loading && typeof tg.MainButton.hideProgress === "function") {
    tg.MainButton.hideProgress();
  }
}

const MONTHS = ["январь", "февраль", "март", "апрель", "май", "июнь", "июль", "август", "сентябрь", "октябрь", "ноябрь", "декабрь"];
const MONTHS_GEN = ["января", "февраля", "марта", "апреля", "мая", "июня", "июля", "августа", "сентября", "октября", "ноября", "декабря"];
const WEEKDAYS_FULL = ["воскресенье", "понедельник", "вторник", "среда", "четверг", "пятница", "суббота"];
const STEP_ORDER = ["service", "date", "time"];

const state = {
  today: "",
  until: "",
  botUsername: "",
  services: [],
  service: null,
  viewYear: 0,
  viewMonth: 1,
  availability: new Map(),
  date: "",
  slots: [],
  slot: null,
  sending: false,
};

function iso(year, month, day) {
  return `${year}-${String(month).padStart(2, "0")}-${String(day).padStart(2, "0")}`;
}

function formatPrice(value) {
  return `${new Intl.NumberFormat("ru-RU").format(value)} ₽`;
}

function formatDuration(minutes) {
  const hours = Math.floor(minutes / 60);
  const rest = minutes % 60;
  if (hours && rest) return `${hours} ч ${rest} мин`;
  if (hours) return `${hours} ч`;
  return `${rest} мин`;
}

function formatIso(value) {
  const [year, month, day] = value.split("-").map(Number);
  const weekday = WEEKDAYS_FULL[new Date(year, month - 1, day).getDay()];
  return `${weekday}, ${day} ${MONTHS_GEN[month - 1]}`;
}

function monthTitle(year, month) {
  const name = MONTHS[month - 1];
  return `${name.charAt(0).toUpperCase()}${name.slice(1)} ${year}`;
}

function lastDay(year, month) {
  return new Date(year, month, 0).getDate();
}

function monthIsOpen(year, month) {
  return iso(year, month, lastDay(year, month)) >= state.today && iso(year, month, 1) <= state.until;
}

function scrollTo(selector) {
  const behavior = window.matchMedia("(prefers-reduced-motion: reduce)").matches ? "auto" : "smooth";
  document.querySelector(selector).scrollIntoView({ behavior, block: "start" });
}

function setStatus(text, isError) {
  const node = document.querySelector("#status");
  node.hidden = !text;
  node.textContent = text || "";
  node.classList.toggle("is-error", Boolean(isError));
}

function setStep(name) {
  document.querySelectorAll("#steps li").forEach((item) => {
    const index = STEP_ORDER.indexOf(item.dataset.step);
    const current = STEP_ORDER.indexOf(name);
    item.classList.toggle("is-current", item.dataset.step === name);
    item.classList.toggle("is-done", index < current);
  });
}

function haptic() {
  if (tg && tg.HapticFeedback && tg.HapticFeedback.selectionChanged) {
    tg.HapticFeedback.selectionChanged();
  }
}

async function api(path, options = {}) {
  const { headers: extraHeaders, ...rest } = options;
  const response = await fetch(`${apiBase}${path}`, {
    ...rest,
    headers: {
      Accept: "application/json",
      ...(extraHeaders || {}),
    },
  });
  if (!response.ok) {
    let detail = "Не удалось загрузить данные. Откройте календарь ещё раз.";
    try {
      const body = await response.json();
      if (body && typeof body.detail === "string") detail = body.detail;
    } catch (error) {
      /* ответ без JSON */
    }
    throw new Error(detail);
  }
  if (response.status === 204) return null;
  return response.json();
}

function trySendData(payload) {
  if (!tg || typeof tg.sendData !== "function") return false;
  try {
    tg.sendData(payload);
    return true;
  } catch (error) {
    return false;
  }
}

async function bookViaApi() {
  const initData = (tg && tg.initData) || "";
  if (!initData) {
    throw new Error("NO_INIT_DATA");
  }
  return api("/api/book", {
    method: "POST",
    headers: {
      "Content-Type": "application/json",
      "X-Telegram-Init-Data": initData,
    },
    body: JSON.stringify({
      service_id: state.service.id,
      date: state.date,
      slot_time: state.slot.time,
      init_data: initData,
    }),
  });
}

function openDeepLinkFallback() {
  if (!state.botUsername || !state.service || !state.date || !state.slot) return false;
  const day = state.date.replaceAll("-", "");
  const hhmm = state.slot.time.replace(":", "");
  const start = `book_${state.service.id}_${day}_${hhmm}`;
  const link = `https://t.me/${state.botUsername}?start=${start}`;
  if (tg && typeof tg.openTelegramLink === "function") {
    tg.openTelegramLink(link);
    if (typeof tg.close === "function") tg.close();
    return true;
  }
  window.location.href = link;
  return true;
}

function presetServiceId() {
  const fromQuery = params.get("service");
  if (fromQuery && /^\d+$/.test(fromQuery)) return Number(fromQuery);
  const start = tg && tg.initDataUnsafe && tg.initDataUnsafe.start_param;
  if (typeof start === "string" && start.startsWith("service_")) {
    const value = start.slice("service_".length);
    if (/^\d+$/.test(value)) return Number(value);
  }
  return null;
}

function syncMainButton() {
  const ready = Boolean(state.service && state.date && state.slot);
  document.querySelector("#confirm").disabled = !ready;
  document.querySelector("#summary").hidden = !ready;
  if (!tg || !tg.MainButton) return;
  if (ready) {
    tg.MainButton.setText(`Оплатить ${formatPrice(state.service.price)}`);
    tg.MainButton.show();
    if (typeof tg.enableClosingConfirmation === "function") tg.enableClosingConfirmation();
    return;
  }
  tg.MainButton.hide();
  if (typeof tg.disableClosingConfirmation === "function") tg.disableClosingConfirmation();
}

function renderSummary() {
  if (!state.service || !state.date || !state.slot) {
    syncMainButton();
    return;
  }
  document.querySelector("#summary-title").textContent = state.service.name;
  document.querySelector("#summary-when").textContent =
    `${formatIso(state.date)}, ${state.slot.time}–${state.slot.end}`;
  document.querySelector("#summary-price").textContent =
    `${formatDuration(state.service.duration_minutes)} · ${formatPrice(state.service.price)}`;
  setStep("time");
  syncMainButton();
}

function renderServices() {
  const root = document.querySelector("#services");
  root.replaceChildren();
  if (!state.services.length) {
    setStatus("Услуги пока не добавлены.", true);
    return;
  }
  state.services.forEach((service) => {
    const button = document.createElement("button");
    button.type = "button";
    button.className = "service";
    button.setAttribute("aria-pressed", String(state.service && state.service.id === service.id));
    if (state.service && state.service.id === service.id) button.classList.add("is-selected");

    const text = document.createElement("span");
    const title = document.createElement("strong");
    title.textContent = service.name;
    const meta = document.createElement("small");
    meta.textContent = formatDuration(service.duration_minutes);
    text.append(title, meta);

    const price = document.createElement("span");
    price.className = "price";
    price.textContent = formatPrice(service.price);
    button.append(text, price);
    button.addEventListener("click", () => selectService(service));
    root.append(button);
  });
}

function renderCalendar() {
  const year = state.viewYear;
  const month = state.viewMonth;
  document.querySelector("#month-label").textContent = monthTitle(year, month);
  const prev = new Date(year, month - 2, 1);
  const next = new Date(year, month, 1);
  document.querySelector("#prev-month").disabled = !monthIsOpen(prev.getFullYear(), prev.getMonth() + 1);
  document.querySelector("#next-month").disabled = !monthIsOpen(next.getFullYear(), next.getMonth() + 1);

  const grid = document.querySelector("#grid");
  grid.replaceChildren();
  const shift = (new Date(year, month - 1, 1).getDay() + 6) % 7;
  for (let index = 0; index < shift; index += 1) {
    grid.append(document.createElement("span"));
  }
  const days = lastDay(year, month);
  for (let day = 1; day <= days; day += 1) {
    const value = iso(year, month, day);
    const free = state.availability.get(value) || 0;
    const button = document.createElement("button");
    button.type = "button";
    button.className = "day";
    button.textContent = String(day);
    const disabled = value < state.today || value > state.until || free < 1;
    button.disabled = disabled;
    if (value === state.today) button.classList.add("is-today");
    if (value === state.date) button.classList.add("is-selected");
    button.setAttribute("aria-pressed", String(value === state.date));
    button.setAttribute("aria-label", disabled ? `${day} ${MONTHS_GEN[month - 1]}, недоступно` : `${day} ${MONTHS_GEN[month - 1]}, свободно ${free}`);
    button.addEventListener("click", () => selectDate(value));
    grid.append(button);
  }
}

function renderSlots() {
  const root = document.querySelector("#slots");
  root.replaceChildren();
  if (!state.slots.length) {
    setStatus("На этот день свободного времени нет. Выберите другую дату.", true);
    return;
  }
  setStatus("");
  state.slots.forEach((slot) => {
    const button = document.createElement("button");
    button.type = "button";
    button.className = "slot";
    const selected = state.slot && state.slot.time === slot.time;
    button.classList.toggle("is-selected", Boolean(selected));
    button.setAttribute("aria-pressed", String(Boolean(selected)));
    const start = document.createElement("strong");
    start.textContent = slot.time;
    const end = document.createElement("span");
    end.textContent = `до ${slot.end}`;
    button.append(start, end);
    button.addEventListener("click", () => selectSlot(slot));
    root.append(button);
  });
}

async function selectService(service) {
  state.service = service;
  state.date = "";
  state.slot = null;
  state.slots = [];
  document.querySelector("#time-section").hidden = true;
  document.querySelector("#summary").hidden = true;
  renderServices();
  setStep("date");
  document.querySelector("#date-section").hidden = false;
  haptic();
  try {
    setStatus("Загрузка календаря…");
    await loadMonth();
    setStatus("");
    scrollTo("#date-section");
  } catch (error) {
    setStatus(error.message, true);
  }
}

async function loadMonth() {
  const year = state.viewYear;
  const month = state.viewMonth;
  const data = await api(
    `/api/calendar?service_id=${state.service.id}&start=${iso(year, month, 1)}&end=${iso(year, month, lastDay(year, month))}`,
  );
  state.availability = new Map(data.days.map((day) => [day.date, day.free]));
  renderCalendar();
}

async function shiftMonth(delta) {
  if (!state.service) return;
  const next = new Date(state.viewYear, state.viewMonth - 1 + delta, 1);
  const year = next.getFullYear();
  const month = next.getMonth() + 1;
  if (!monthIsOpen(year, month)) return;
  state.viewYear = year;
  state.viewMonth = month;
  try {
    setStatus("Загрузка календаря…");
    await loadMonth();
    setStatus("");
  } catch (error) {
    setStatus(error.message, true);
  }
}

async function selectDate(value) {
  state.date = value;
  state.slot = null;
  renderCalendar();
  document.querySelector("#time-section").hidden = false;
  setStep("time");
  haptic();
  try {
    setStatus("Загрузка времени…");
    const data = await api(`/api/slots?service_id=${state.service.id}&date=${value}`);
    state.slots = data.slots;
    renderSlots();
    renderSummary();
    scrollTo("#time-section");
  } catch (error) {
    setStatus(error.message, true);
  }
}

function selectSlot(slot) {
  state.slot = slot;
  renderSlots();
  renderSummary();
  haptic();
  scrollTo("#summary");
}

async function confirmBooking() {
  if (state.sending || !state.service || !state.date || !state.slot) return;
  state.sending = true;
  setMainButtonLoading(true);
  setStatus("Отправляем запись…");
  const payload = JSON.stringify({
    service_id: state.service.id,
    date: state.date,
    slot_time: state.slot.time,
  });

  // 1) Классический путь WebApp → чат бота.
  if (trySendData(payload)) {
    return;
  }

  // 2) Надёжный путь для Desktop: API + initData, бот сам шлёт счёт.
  try {
    await bookViaApi();
    setStatus("Счёт отправлен в чат.");
    if (tg && typeof tg.HapticFeedback?.notificationOccurred === "function") {
      tg.HapticFeedback.notificationOccurred("success");
    }
    if (tg && typeof tg.close === "function") {
      setTimeout(() => tg.close(), 350);
    }
    return;
  } catch (error) {
    if (!error || error.message !== "NO_INIT_DATA") {
      state.sending = false;
      setMainButtonLoading(false);
      setStatus(error.message || "Не удалось создать счёт.", true);
      return;
    }
  }

  // 3) Если Telegram не отдал initData — deep link обратно в бота.
  if (openDeepLinkFallback()) {
    return;
  }

  state.sending = false;
  setMainButtonLoading(false);
  const preview = document.querySelector("#preview");
  preview.hidden = false;
  preview.textContent = `Локальный просмотр. В Telegram эти данные уйдут в чат:\n\n${payload}`;
  setStatus("Откройте календарь кнопкой «Записаться» в чате с ботом.", true);
}

async function init() {
  if (tg) {
    tg.ready();
    tg.expand();
    if (tg.MainButton) {
      tg.MainButton.onClick(() => {
        confirmBooking();
      });
      tg.MainButton.hide();
    }
  }
  if (isInsideTelegram()) document.body.classList.add("in-telegram");
  document.querySelector("#prev-month").addEventListener("click", () => shiftMonth(-1));
  document.querySelector("#next-month").addEventListener("click", () => shiftMonth(1));
  document.querySelector("#confirm").addEventListener("click", () => {
    confirmBooking();
  });
  try {
    setStatus("Загрузка…");
    const data = await api("/api/services");
    state.today = data.today;
    state.until = data.until;
    state.botUsername = data.bot_username || "";
    state.services = data.services;
    const [year, month] = data.today.split("-").map(Number);
    state.viewYear = year;
    state.viewMonth = month;
    document.querySelector("#studio").textContent = data.studio_name;
    document.title = data.studio_name;
    renderServices();
    setStatus("");
    const preset = presetServiceId();
    const match = state.services.find((item) => item.id === preset);
    if (match) await selectService(match);
  } catch (error) {
    setStatus(error.message || "Не удалось загрузить услуги.", true);
  }
}

init();
