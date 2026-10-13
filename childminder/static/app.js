"use strict";

// Every value from the server is inserted with textContent, never as HTML:
// process names and log lines can be chosen by a monitored user.

const REFRESH_MS = 30000;
let csrf = null;
let refreshTimer = null;

function $(id) {
  return document.getElementById(id);
}

function el(tag, attrs, ...children) {
  const node = document.createElement(tag);
  for (const [key, value] of Object.entries(attrs || {})) {
    if (key === "class") node.className = value;
    else if (key.startsWith("on")) node.addEventListener(key.slice(2), value);
    else node.setAttribute(key, value);
  }
  for (const child of children.flat()) {
    if (child === null || child === undefined || child === false) continue;
    node.append(child instanceof Node ? child : document.createTextNode(String(child)));
  }
  return node;
}

class ApiError extends Error {
  constructor(status, message) {
    super(message);
    this.status = status;
  }
}

async function api(method, path, body) {
  const headers = {};
  if (method === "POST") {
    headers["Content-Type"] = "application/json";
    if (csrf) headers["X-CSRF-Token"] = csrf;
  }
  const response = await fetch(path, {
    method,
    headers,
    credentials: "same-origin",
    body: method === "POST" ? JSON.stringify(body || {}) : undefined,
  });
  let payload = {};
  try {
    payload = await response.json();
  } catch (e) {
    // A response without a JSON body is reported by its status alone
  }
  if (!response.ok) {
    if (response.status === 401 && path !== "/api/login") showLogin();
    throw new ApiError(response.status, payload.error || `HTTP ${response.status}`);
  }
  return payload;
}

function showError(message) {
  const box = $("error");
  box.textContent = message || "";
  box.hidden = !message;
}

// -- formatting --

function minutes(seconds) {
  const total = Math.round(seconds / 60);
  if (total < 60) return `${total} min`;
  const h = Math.floor(total / 60);
  const m = total % 60;
  return m ? `${h} h ${m} min` : `${h} h`;
}

function when(iso) {
  if (!iso) return "unknown";
  const date = new Date(iso);
  if (isNaN(date)) return iso;
  return date.toLocaleString([], { weekday: "short", hour: "2-digit", minute: "2-digit" });
}

function meter(label, line) {
  const used = line.seconds;
  const limit = line.limit_seconds;
  const right = limit === null
    ? `${minutes(used)} (no limit)`
    : `${minutes(used)} of ${minutes(limit)}, ${minutes(line.remaining_seconds)} left`;
  const share = limit ? Math.min(1, used / limit) : (limit === 0 ? 1 : 0);
  const level = share >= 1 ? "bad" : share >= 0.8 ? "warn" : "";
  // Set through the CSSOM: the Content-Security-Policy blocks style attributes
  const fill = el("div", { class: `fill ${level}` });
  fill.style.width = `${Math.round(share * 100)}%`;
  return el("div", { class: "meter" },
    el("div", { class: "label" }, el("span", {}, label), el("span", {}, right)),
    limit === null ? null : el("div", { class: "bar" }, fill));
}

// -- today --

function userStatus(user) {
  if (user.disabled) {
    const until = user.disabled.re_enable_at;
    return el("span", { class: "badge bad" }, until ? `Locked until ${when(until)}` : "Locked");
  }
  if (!user.allowed_now) return el("span", { class: "badge warn" }, "Outside access hours");
  return el("span", { class: "badge ok" }, "Allowed now");
}

function renderUser(user) {
  const card = el("div", { class: "card" },
    el("div", { class: "row" }, el("h2", {}, user.name), userStatus(user)),
    meter("Screen time today", user.daily));

  if (user.disabled && user.disabled.reason) {
    card.append(el("p", { class: "muted" }, `Reason: ${user.disabled.reason}`));
  }
  if (user.groups.length) {
    card.append(el("h3", {}, "Groups"), ...user.groups.map((line) => meter(line.subject, line)));
  }
  if (user.processes.length) {
    card.append(el("h3", {}, "Applications"),
      ...user.processes.map((line) => meter(line.subject, line)));
  }
  card.append(el("h3", {}, "Access hours"),
    el("ul", { class: "plain" },
      el("li", {}, `Weekdays: ${user.schedule_days.weekday}`),
      el("li", {}, `Weekends: ${user.schedule_days.weekend}`)));
  return card;
}

function renderOverview(data) {
  const serviceOk = data.service === "active";
  $("summary").replaceChildren(
    el("div", { class: "row" },
      el("span", {}, `Service: `,
        el("span", { class: `badge ${serviceOk ? "ok" : "bad"}` }, data.service)),
      el("span", {}, `Monitoring: `,
        el("span", { class: `badge ${data.monitoring ? "ok" : "warn"}` },
          data.monitoring ? "on" : "off"))),
    el("p", { class: "muted" },
      `Today is a ${data.day_type}. Usage last saved ${when(data.state_saved)}; ` +
      `the service saves it every minute.`),
    data.warnings.map((w) => el("p", { class: "notice" }, w)));

  $("users").replaceChildren(...(data.users.length
    ? data.users.map(renderUser)
    : [el("div", { class: "card muted" }, "No users are monitored.")]));
}

// -- settings --

function listCard(title, items, empty) {
  return el("div", { class: "card" }, el("h2", {}, title),
    items.length ? el("ul", { class: "plain" }, items.map((i) => el("li", {}, i)))
      : el("p", { class: "muted" }, empty));
}

function renderSettings(data) {
  const c = data.config;
  const users = c.monitored_users === null ? ["None (invalid monitored_users)"]
    : c.monitored_users.length ? c.monitored_users : ["All regular users"];
  const daily = Object.entries(c.user_daily_limits).map(([user, entry]) => {
    const part = (day) => entry[day] === null ? `${day}: no limit` : `${day}: ${entry[day]} min`;
    return `${user}: ${part("weekday")}, ${part("weekend")}`;
  });
  const groups = Object.entries(c.process_groups).map(([group, patterns]) => {
    const limit = c.group_limits[group];
    return `${group}${limit === undefined ? "" : ` (${limit} min/day)`}: ${patterns.join(", ")}`;
  });
  const cards = [];
  if (data.warnings.length) {
    cards.push(el("div", { class: "card" },
      data.warnings.map((w) => el("p", { class: "notice" }, w))));
  }
  $("settings").replaceChildren(...cards,
    listCard("Monitored users", users, ""),
    listCard("Daily screen time limits", daily, "No daily limits."),
    listCard("Blocked applications", c.blocked_processes, "Nothing is blocked."),
    listCard("Application limits",
      Object.entries(c.limited_processes).map(([p, m]) => `${p}: ${m} min/day`),
      "No application limits."),
    listCard("Groups", groups, "No groups."),
    listCard("Tracked applications", c.monitored_processes, "None."));
}

// -- logs --

async function loadLogs() {
  try {
    const data = await api("GET", "/api/logs?lines=200");
    $("log-lines").textContent = data.lines.join("\n");
    showError(null);
  } catch (e) {
    showError(e.message);
  }
}

// -- navigation and session --

let currentTab = "today";

async function refresh() {
  try {
    if (currentTab === "today") renderOverview(await api("GET", "/api/overview"));
    else if (currentTab === "settings") renderSettings(await api("GET", "/api/config"));
    else if (currentTab === "logs") await loadLogs();
    showError(null);
  } catch (e) {
    if (e.status !== 401) showError(e.message);
  }
}

function selectTab(name) {
  currentTab = name;
  for (const button of document.querySelectorAll("button.tab")) {
    button.classList.toggle("active", button.dataset.tab === name);
  }
  for (const panel of document.querySelectorAll(".tab-panel")) {
    panel.hidden = panel.id !== `tab-${name}`;
  }
  refresh();
}

function showLogin(passwordSet = true) {
  csrf = null;
  clearInterval(refreshTimer);
  $("app-view").hidden = true;
  $("logout").hidden = true;
  $("login-view").hidden = false;
  $("no-password").hidden = passwordSet;
  $("password").focus();
}

function showApp() {
  $("login-view").hidden = true;
  $("app-view").hidden = false;
  $("logout").hidden = false;
  selectTab(currentTab);
  clearInterval(refreshTimer);
  refreshTimer = setInterval(() => {
    if (!document.hidden && currentTab === "today") refresh();
  }, REFRESH_MS);
}

async function login(event) {
  event.preventDefault();
  const error = $("login-error");
  error.hidden = true;
  try {
    const data = await api("POST", "/api/login", { password: $("password").value });
    csrf = data.csrf;
    $("password").value = "";
    showApp();
  } catch (e) {
    error.textContent = e.message;
    error.hidden = false;
  }
}

async function logout() {
  try {
    await api("POST", "/api/logout");
  } catch (e) {
    // The session is gone either way
  }
  showLogin();
}

async function start() {
  $("login-form").addEventListener("submit", login);
  $("logout").addEventListener("click", logout);
  $("logs-refresh").addEventListener("click", loadLogs);
  for (const button of document.querySelectorAll("button.tab")) {
    button.addEventListener("click", () => selectTab(button.dataset.tab));
  }
  try {
    const session = await api("GET", "/api/session");
    if (session.authenticated) {
      csrf = session.csrf;
      showApp();
    } else {
      showLogin(session.password_set);
    }
  } catch (e) {
    showLogin();
    $("login-error").textContent = e.message;
    $("login-error").hidden = false;
  }
}

start();
