const panel = document.getElementById("panel");
const kicker = document.getElementById("kicker");
const headline = document.getElementById("headline");
const detail = document.getElementById("detail");
const doors = document.getElementById("doors");

let shown = null;
let clockMark = null;

function clockParts(unixSeconds, timezone) {
  const now = new Date(unixSeconds * 1000);
  const time = new Intl.DateTimeFormat("en-GB", {
    timeZone: timezone,
    hour: "2-digit",
    minute: "2-digit",
    second: "2-digit",
    hourCycle: "h23"
  }).format(now);
  const dateParts = new Intl.DateTimeFormat("en-GB", {
    timeZone: timezone,
    weekday: "long",
    day: "2-digit",
    month: "long",
    year: "numeric"
  }).formatToParts(now);
  const pick = function (type) {
    const part = dateParts.find(function (item) { return item.type === type; });
    return part ? part.value : "";
  };
  return {
    time: time,
    date: pick("weekday") + ", " + pick("day") + " " + pick("month") + " " + pick("year")
  };
}

function applyState(state) {
  shown = state;
  if (typeof state.unix === "number") {
    clockMark = {
      unix: state.unix,
      at: Date.now(),
      timezone: state.timezone || "Asia/Jerusalem"
    };
  }
  const mode = state.mode || "idle";
  document.body.dataset.mode = mode;

  if (mode === "mellon") {
    doors.hidden = false;
    panel.hidden = true;
    return;
  }

  doors.hidden = true;
  panel.hidden = false;

  if (mode !== "automate" || !state.weather) {
    document.body.dataset.scene = "idle";
    kicker.textContent = "Waiting";
    headline.textContent = "Ready";
    detail.textContent = "Waiting for Mellon or Automate.";
    return;
  }

  const weather = state.weather;
  document.body.dataset.scene = weather.scene || "cloudy";
  kicker.textContent = weather.place || "Here";
  paintClock();
}

function paintClock() {
  if (!shown || shown.mode !== "automate" || !shown.weather || !clockMark) {
    return;
  }
  const unix = clockMark.unix + (Date.now() - clockMark.at) / 1000;
  const parts = clockParts(unix, clockMark.timezone);
  const temperature = typeof shown.weather.temperature_c === "number"
    ? Math.round(shown.weather.temperature_c) + "\u00b0C"
    : "";
  headline.textContent = parts.time;
  detail.textContent = [parts.date, shown.weather.status, temperature].filter(Boolean).join("  ·  ");
}

async function loadOnce() {
  try {
    const response = await fetch("/state", { cache: "no-store" });
    if (response.status === 401) {
      window.location.assign("/login");
      return;
    }
    if (!response.ok) {
      return;
    }
    applyState(await response.json());
  } catch (_error) {
    return;
  }
}

function listen() {
  const source = new EventSource("/updates");
  source.onmessage = function (event) {
    try {
      applyState(JSON.parse(event.data));
    } catch (_error) {
      return;
    }
  };
  source.onerror = function () {
    source.close();
  };
}

loadOnce();
listen();
window.setInterval(paintClock, 1000);
