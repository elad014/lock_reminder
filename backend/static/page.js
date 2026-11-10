const panel = document.getElementById("panel");
const kicker = document.getElementById("kicker");
const headline = document.getElementById("headline");
const detail = document.getElementById("detail");
const doors = document.getElementById("doors");

let shown = null;
let clockMark = null;
let weatherRequest = 0;

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

function wholeNumber(value) {
  if (typeof value !== "number" || !Number.isFinite(value) || !Number.isInteger(value)) {
    return null;
  }
  return value;
}

function describeWeather(code, isDay) {
  if (code === 0) {
    return { status: "Clear", scene: isDay ? "clear-day" : "clear-night" };
  }
  if (code === 1 || code === 2) {
    return { status: "Partly cloudy", scene: isDay ? "partly-cloudy" : "cloudy-night" };
  }
  if (code === 3) {
    return { status: "Overcast", scene: "cloudy" };
  }
  if (code === 45 || code === 48) {
    return { status: "Fog", scene: "fog" };
  }
  if ([51, 53, 55, 56, 57, 61, 63, 65, 66, 67, 80, 81, 82].indexOf(code) !== -1) {
    return { status: "Rain", scene: "rain" };
  }
  if ([71, 73, 75, 77, 85, 86].indexOf(code) !== -1) {
    return { status: "Snow", scene: "snow" };
  }
  if (code === 95 || code === 96 || code === 99) {
    return { status: "Thunderstorm", scene: "storm" };
  }
  return { status: "Cloudy", scene: "cloudy" };
}

function loadBrowserWeather(state) {
  const place = state.place;
  if (!place || typeof place.latitude !== "number" || typeof place.longitude !== "number") {
    return;
  }
  const requestId = ++weatherRequest;
  const url = "https://api.open-meteo.com/v1/forecast?latitude="
    + encodeURIComponent(place.latitude)
    + "&longitude=" + encodeURIComponent(place.longitude)
    + "&current=temperature_2m,weather_code,is_day&timezone=auto";
  fetch(url)
    .then(function (response) {
      if (!response.ok) {
        throw new Error("weather");
      }
      return response.json();
    })
    .then(function (payload) {
      if (requestId !== weatherRequest || !shown || shown.mode !== "automate") {
        return;
      }
      const current = payload && payload.current ? payload.current : {};
      const code = wholeNumber(current.weather_code);
      const isDayValue = wholeNumber(current.is_day);
      if (code === null || isDayValue === null) {
        throw new Error("weather");
      }
      const described = describeWeather(code, isDayValue === 1);
      const temperature = typeof current.temperature_2m === "number" ? current.temperature_2m : null;
      applyState({
        mode: "automate",
        unix: shown.unix,
        timezone: shown.timezone,
        place: place,
        weather: {
          status: described.status,
          scene: described.scene,
          temperature_c: temperature,
          place: place.name || "Here"
        }
      });
    })
    .catch(function () {
      if (requestId !== weatherRequest || !shown || shown.mode !== "automate" || shown.weather) {
        return;
      }
      document.body.dataset.scene = "cloudy";
      detail.textContent = "Weather unavailable";
    });
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
    weatherRequest += 1;
    doors.hidden = false;
    panel.hidden = true;
    return;
  }

  doors.hidden = true;
  panel.hidden = false;

  if (mode !== "automate") {
    weatherRequest += 1;
    document.body.dataset.scene = "idle";
    kicker.textContent = "Waiting";
    headline.textContent = "Ready";
    detail.textContent = "Waiting for Mellon or Automate.";
    return;
  }

  const placeName = (state.weather && state.weather.place) || (state.place && state.place.name) || "Here";
  kicker.textContent = placeName;
  paintClock();
  if (!state.weather) {
    document.body.dataset.scene = "idle";
    detail.textContent = "Loading weather.";
    loadBrowserWeather(state);
    return;
  }

  document.body.dataset.scene = state.weather.scene || "cloudy";
}

function paintClock() {
  if (!shown || shown.mode !== "automate" || !clockMark) {
    return;
  }
  const unix = clockMark.unix + (Date.now() - clockMark.at) / 1000;
  const parts = clockParts(unix, clockMark.timezone);
  headline.textContent = parts.time;
  if (!shown.weather) {
    return;
  }
  const temperature = typeof shown.weather.temperature_c === "number"
    ? Math.round(shown.weather.temperature_c) + "\u00b0C"
    : "";
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

function initialState() {
  const node = document.getElementById("initial-state");
  if (!node || !node.textContent) {
    return null;
  }
  try {
    const parsed = JSON.parse(node.textContent);
    if (!parsed || typeof parsed !== "object") {
      return null;
    }
    return parsed;
  } catch (_error) {
    return null;
  }
}

const starting = initialState();
if (starting) {
  applyState(starting);
} else {
  loadOnce();
}
listen();
window.setInterval(paintClock, 1000);
