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
  if (message) $("message").hidden = true;
}

function showMessage(message) {
  const box = $("message");
  box.textContent = message || "";
  box.hidden = !message;
  if (message) $("error").hidden = true;
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

// -- changes --

// Run cmctl commands through the server, report the outcome and redraw
async function run(commands, confirmText) {
  if (confirmText && !window.confirm(confirmText)) return false;
  try {
    const data = await api("POST", "/api/commands", { commands });
    // Every config change prints this; the line after it says what changed
    showMessage(data.output.split("\n")
      .filter((line) => line !== "Configuration saved successfully").join("\n"));
    await refresh();
    return true;
  } catch (e) {
    showError(e.message);
    return false;
  }
}

function cmd(command, ...args) {
  return { command, args };
}

function button(label, onclick, cls = "secondary") {
  return el("button", { type: "button", class: cls, onclick }, label);
}

function hourSelect(value) {
  const select = el("select", {});
  for (let h = 0; h < 24; h++) {
    const option = el("option", { value: String(h) }, `${String(h).padStart(2, "0")}:00`);
    if (h === value) option.selected = true;
    select.append(option);
  }
  return select;
}

function minutesInput(value) {
  const input = el("input", { type: "number", min: "0", step: "1", inputmode: "numeric",
                              placeholder: "minutes" });
  if (value !== null && value !== undefined) input.value = String(value);
  return input;
}

function readMinutes(input) {
  return input.value.trim() === "" ? null : Number(input.value);
}

// A form row: inputs and a submit button, submitted by Enter or the button
function inlineForm(fields, label, onsubmit) {
  return el("form", { class: "inline", onsubmit: (event) => { event.preventDefault(); onsubmit(); } },
    ...fields, el("button", { type: "submit" }, label));
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

// Latest screenshot per user, kept across the periodic redraw: {url, taken}
const screenshots = {};

async function takeScreenshot(user, button) {
  button.disabled = true;
  button.textContent = "Taking screenshot...";
  try {
    const data = await api("POST", "/api/screenshot", { user });
    const bytes = Uint8Array.from(atob(data.png), (c) => c.charCodeAt(0));
    if (screenshots[user]) URL.revokeObjectURL(screenshots[user].url);
    screenshots[user] = {
      url: URL.createObjectURL(new Blob([bytes], { type: "image/png" })),
      taken: data.taken,
    };
    showError(null);
    await refresh();
  } catch (e) {
    showError(e.message);
    button.disabled = false;
    button.textContent = "Screenshot";
  }
}

function screenshotView(user) {
  const shot = screenshots[user];
  if (!shot) return null;
  return el("div", { class: "screenshot" },
    el("a", { href: shot.url, target: "_blank", rel: "noopener" },
      el("img", { src: shot.url, alt: `Screen of ${user}` })),
    el("p", { class: "muted" }, `Taken ${when(shot.taken)}. Tap to open full size.`));
}

const LOCK_CHOICES = [["0.5", "30 minutes"], ["1", "1 hour"], ["2", "2 hours"],
                      ["4", "4 hours"], ["24", "1 day"], ["", "Until unlocked"]];

function lockControls(user) {
  if (user.disabled) {
    return el("div", { class: "actions" },
      button("Unlock", () => run([cmd("enable-user", user.name)]), ""));
  }
  const duration = el("select", {},
    LOCK_CHOICES.map(([value, label]) => el("option", { value }, label)));
  const reason = el("input", { type: "text", placeholder: "Reason (optional)", maxlength: "200" });
  return el("div", { class: "actions" },
    inlineForm([duration, reason], "Lock", () => {
      const options = { hours: duration.value === "" ? null : Number(duration.value) };
      if (reason.value.trim()) options.reason = reason.value.trim();
      const label = duration.selectedOptions[0].textContent.toLowerCase();
      run([{ command: "disable-user", args: [user.name], options }],
        `Lock ${user.name} out ${duration.value === "" ? "until unlocked" : `for ${label}`}? ` +
        "This ends their session now.");
    }));
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
      el("li", {}, `Weekends: ${user.schedule_days.weekend}`)),
    lockControls(user));
  const shoot = button("Screenshot", () => takeScreenshot(user.name, shoot));
  card.append(el("div", { class: "actions" }, shoot), screenshotView(user.name));
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
    ...data.warnings.map((w) => el("p", { class: "notice" }, w)),
    el("div", { class: "actions" },
      data.monitoring
        ? button("Turn monitoring off", () => run([cmd("disable")],
            "Turn monitoring off? Limits and access hours stop being enforced until it is turned back on."))
        : button("Turn monitoring on", () => run([cmd("enable")]), ""),
      button("Reset today's usage", () => run([cmd("reset")],
        "Reset today's usage for every user? Everyone gets their full time back."))));

  $("users").replaceChildren(...(data.users.length
    ? data.users.map(renderUser)
    : [el("div", { class: "card muted" }, "No users are monitored.")]));
}

// -- settings --

function section(title, ...children) {
  return el("div", { class: "card" }, el("h2", {}, title), ...children);
}

function itemRow(label, ...controls) {
  return el("li", { class: "item" }, el("span", { class: "item-label" }, label),
    el("span", { class: "item-controls" }, ...controls));
}

function textInput(placeholder) {
  return el("input", { type: "text", placeholder, maxlength: "200" });
}

function monitoredUsersSection(c) {
  const name = textInput("Username");
  const note = c.monitored_users === null
    ? el("p", { class: "notice" }, "monitored_users is invalid, so nobody is monitored.")
    : c.monitored_users.length === 0
      ? el("p", { class: "muted" }, "Empty: every regular user is monitored.")
      : null;
  return section("Monitored users", note,
    el("ul", { class: "plain" }, (c.monitored_users || []).map((user) =>
      itemRow(user, button("Remove", () => run([cmd("remove-user", user)],
        `Stop monitoring ${user}?`), "danger")))),
    inlineForm([name], "Add", () => run([cmd("add-user", name.value.trim())])));
}

// Commands that make a user's daily limits match two inputs; blank is no limit
function dailyLimitCommands(user, weekday, weekend) {
  if (weekday === null && weekend === null) return [cmd("user-unlimit", user)];
  if (weekday === weekend) return [cmd("user-limit", user, weekday)];
  const commands = [];
  // cmctl cannot clear one day type alone, so clear both and set the other
  if (weekday === null || weekend === null) commands.push(cmd("user-unlimit", user));
  if (weekday !== null) commands.push(cmd("user-weekday-limit", user, weekday));
  if (weekend !== null) commands.push(cmd("user-weekend-limit", user, weekend));
  return commands;
}

function dailyLimitsSection(c) {
  const rows = Object.entries(c.user_daily_limits).map(([user, entry]) => {
    const weekday = minutesInput(entry.weekday);
    const weekend = minutesInput(entry.weekend);
    return el("li", { class: "item" },
      el("span", { class: "item-label" }, user),
      inlineForm([el("label", { class: "compact" }, "Weekday", weekday),
                  el("label", { class: "compact" }, "Weekend", weekend)], "Save",
        () => run(dailyLimitCommands(user, readMinutes(weekday), readMinutes(weekend)))),
      button("Remove", () => run([cmd("user-unlimit", user)],
        `Remove ${user}'s daily limit?`), "danger"));
  });
  const user = textInput("Username");
  const minutes = minutesInput(null);
  return section("Daily screen time limits",
    el("p", { class: "muted" }, "Minutes per day. Leave a day blank for no limit; 0 allows no time."),
    el("ul", { class: "plain" }, rows),
    inlineForm([user, minutes], "Add",
      () => run([cmd("user-limit", user.value.trim(), readMinutes(minutes))])));
}

function scheduleDay(user, day, sched) {
  const label = day === "weekday" ? "Weekdays (Mon-Fri)" : "Weekends (Sat-Sun)";
  const denied = (sched.no_access || []).includes(day);
  const windows = sched[day];
  const state = denied ? "No access" : windows.length ? null : "Unrestricted";
  const start = hourSelect(15);
  const end = hourSelect(20);
  return el("div", { class: "day" },
    el("h3", {}, label),
    state ? el("p", {}, state) : el("ul", { class: "plain" }, windows.map((w) =>
      itemRow(`${String(w.start_hour).padStart(2, "0")}:00 - ${String(w.end_hour).padStart(2, "0")}:00` +
              (w.start_hour > w.end_hour ? " (overnight)" : ""),
        button("Remove", () => run([cmd(`remove-${day}-window`, user, w.start_hour, w.end_hour)],
          windows.length === 1 ? `This is the last window, so ${user} will have no ${day} access. Continue?`
            : null), "danger")))),
    inlineForm([start, el("span", {}, "to"), end], "Add window",
      () => run([cmd(`add-${day}-window`, user, Number(start.value), Number(end.value))])),
    el("div", { class: "actions" },
      denied ? null : button("No access", () => run([cmd(`deny-${day}-access`, user)],
        `Deny all ${day} access for ${user}?`)),
      state === "Unrestricted" ? null
        : button("Unrestricted", () => run([cmd(`clear-${day}-windows`, user)]))));
}

function scheduleSection(users) {
  return section("Access hours",
    el("p", { class: "muted" },
      "Outside these hours the user is logged out. A window ends at the start of its end hour."),
    ...users.map((user) => el("div", { class: "subsection" },
      el("div", { class: "row" }, el("h3", { class: "user" }, user.name),
        button("Clear all", () => run([cmd("clear-user-hours", user.name)],
          `Allow ${user.name} access at any time?`))),
      scheduleDay(user.name, "weekday", user.schedule),
      scheduleDay(user.name, "weekend", user.schedule))));
}

function blockedSection(c) {
  const name = textInput("Process name or pattern, e.g. steam*");
  return section("Blocked applications",
    el("ul", { class: "plain" }, c.blocked_processes.map((p) =>
      itemRow(p, button("Unblock", () => run([cmd("unblock", p)]), "danger")))),
    inlineForm([name], "Block", () => run([cmd("block", name.value.trim())])));
}

function appLimitsSection(c) {
  const rows = Object.entries(c.limited_processes).map(([p, m]) => {
    const minutes = minutesInput(m);
    return el("li", { class: "item" }, el("span", { class: "item-label" }, p),
      inlineForm([minutes], "Save", () => run([cmd("limit", p, readMinutes(minutes))])),
      button("Remove", () => run([cmd("unlimit", p)]), "danger"));
  });
  const name = textInput("Process name or pattern");
  const minutes = minutesInput(null);
  return section("Application limits",
    el("p", { class: "muted" }, "Minutes per day for each application."),
    el("ul", { class: "plain" }, rows),
    inlineForm([name, minutes], "Add",
      () => run([cmd("limit", name.value.trim(), readMinutes(minutes))])));
}

function groupsSection(c) {
  const groups = Object.entries(c.process_groups).map(([group, patterns]) => {
    const limit = minutesInput(c.group_limits[group]);
    const member = textInput("Process name or pattern");
    return el("div", { class: "subsection" },
      el("h3", { class: "user" }, group),
      el("ul", { class: "plain" }, patterns.map((p) =>
        itemRow(p, button("Remove", () => run([cmd("remove-from-group", group, p)]), "danger")))),
      inlineForm([member], "Add", () => run([cmd("add-to-group", group, member.value.trim())])),
      el("div", { class: "actions" },
        inlineForm([el("label", { class: "compact" }, "Limit", limit)], "Save limit", () => {
          const minutes = readMinutes(limit);
          run([minutes === null ? cmd("group-unlimit", group) : cmd("group-limit", group, minutes)]);
        })));
  });
  const name = textInput("Group name");
  const first = textInput("First process");
  return section("Groups",
    el("p", { class: "muted" }, "Applications in a group share one daily limit. " +
      "Leave the limit blank for none."),
    ...groups,
    el("h3", {}, "New group"),
    inlineForm([name, first], "Create",
      () => run([cmd("add-to-group", name.value.trim(), first.value.trim())])));
}

function renderSettings(data, overview) {
  const c = data.config;
  const cards = [];
  if (data.warnings.length) {
    cards.push(el("div", { class: "card" },
      ...data.warnings.map((w) => el("p", { class: "notice" }, w))));
  }
  $("settings").replaceChildren(...cards,
    monitoredUsersSection(c),
    dailyLimitsSection(c),
    scheduleSection(overview.users),
    blockedSection(c),
    appLimitsSection(c),
    groupsSection(c),
    section("Tracked applications",
      el("p", { class: "muted" }, "Logged but not limited. Edit monitored_processes in " +
        "/etc/child-minder/config.json to change this list."),
      el("ul", { class: "plain" }, c.monitored_processes.map((p) => el("li", {}, p)))));
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
    else if (currentTab === "settings") {
      const [config, overview] = await Promise.all(
        [api("GET", "/api/config"), api("GET", "/api/overview")]);
      renderSettings(config, overview);
    }
    else if (currentTab === "logs") await loadLogs();
    $("error").hidden = true;
  } catch (e) {
    if (e.status !== 401) showError(e.message);
  }
}

function selectTab(name) {
  currentTab = name;
  showMessage(null);
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
    const typing = document.activeElement && document.activeElement.closest("form");
    if (!document.hidden && currentTab === "today" && !typing) refresh();
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
