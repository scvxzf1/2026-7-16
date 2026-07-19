"use strict";

const $ = (selector, root = document) => root.querySelector(selector);
const $$ = (selector, root = document) => Array.from(root.querySelectorAll(selector));

const TERMINAL_BATCH = new Set(["succeeded", "completed_with_errors", "cancelled"]);
const TERMINAL_TASK = new Set(["succeeded", "failed", "cancelled"]);
const PIXIV_OAUTH_ACTIVE = new Set([
  "starting",
  "starting_browser",
  "awaiting_login",
  "awaiting_code",
  "exchanging",
]);
const TASK_DISPLAY_LIMIT = 1000;
const SITE_NAMES = {
  danbooru: "Danbooru",
  twitter: "X / Twitter",
  pixiv: "Pixiv",
  exhentai: "EH",
};
const STATUS_NAMES = {
  queued: "等待",
  pending: "待处理",
  planning: "规划中",
  starting: "启动中",
  running: "运行中",
  succeeded: "成功",
  partial: "部分成功",
  failed: "失败",
  cancelling: "取消中",
  cancelled: "已取消",
  completed_with_errors: "完成但有错误",
};
const AUTH_STATE_NAMES = {
  ready: "公开访问就绪",
  authorized: "已登录",
  authorizing: "授权中",
  required: "待授权",
  public: "公开访问",
};
const EVIDENCE_NAMES = {
  site_search_work_evidence: "有站内作品证据",
  account_name_exact_match: "账号身份精确匹配",
  account_identity_unverified: "账号身份待核对",
  danbooru_artist_directory_match: "Danbooru 画师目录匹配",
  danbooru_artist_directory_alias_match: "Danbooru 画师别名待核对",
  artist_tag_exact_match: "artist tag 精确匹配",
  character_tag_exact_match: "character tag 精确匹配",
  keyword_gallery_search_only: "仅关键词画廊命中",
  keyword_gallery_search: "站内关键词画廊候选",
  danbooru_artist_url: "Danbooru 人工维护主页",
};
const EH_TAG_NAMESPACE_ALIASES = {
  a: "artist",
  artist: "artist",
  c: "character",
  char: "character",
  character: "character",
  cos: "cosplayer",
  cosplayer: "cosplayer",
  f: "female",
  female: "female",
  g: "group",
  circle: "group",
  group: "group",
  l: "language",
  lang: "language",
  language: "language",
  loc: "location",
  location: "location",
  m: "male",
  male: "male",
  x: "mixed",
  mixed: "mixed",
  o: "other",
  other: "other",
  p: "parody",
  series: "parody",
  parody: "parody",
  r: "reclass",
  reclass: "reclass",
  temp: "temp",
};

const state = {
  searchResponse: null,
  sources: [],
  activeBatchId: sessionStorage.getItem("gdl.activeBatch") || "",
  activeBatch: null,
  activeBatchTasks: [],
  pollTimer: null,
  refreshingBatchId: "",
  batchRequestToken: 0,
  lastPollError: "",
  ehTagFilter: {
    modes: new Map(),
    query: "",
  },
  auth: new Map(),
  pixivOAuthSessionId: "",
  pixivOAuthPoller: null,
  browserLoginSessions: new Map(),
  browserLoginPollers: new Map(),
  authPromptedSites: new Set(),
};

function node(tag, className = "", text = "") {
  const element = document.createElement(tag);
  if (className) element.className = className;
  if (text !== "") element.textContent = String(text);
  return element;
}

function button(text, className, onClick, title = "") {
  const element = node("button", `button ${className}`, text);
  element.type = "button";
  if (title) element.title = title;
  element.addEventListener("click", onClick);
  return element;
}

function statusBadge(status) {
  const value = String(status || "unknown");
  return node("span", `badge ${value}`, STATUS_NAMES[value] || value);
}

function chip(text) {
  return node("span", "meta-chip", text);
}

function parseEhTag(value) {
  const text = String(value || "").trim();
  if (!text) return null;
  const separator = text.indexOf(":");
  let namespace;
  let tagValue;
  if (separator > 0) {
    const rawNamespace = text.slice(0, separator).trim().toLowerCase();
    namespace = EH_TAG_NAMESPACE_ALIASES[rawNamespace] || "unknown";
    tagValue = text.slice(separator + 1).trim();
  } else {
    namespace = "temp";
    tagValue = text;
  }
  if (!tagValue) return null;
  return {
    namespace,
    value: tagValue,
    key: `${namespace}:${tagValue.toLowerCase()}`,
  };
}

function ehEntryTagKeys(entry) {
  const tags = Array.isArray(entry.data.metadata?.tags)
    ? entry.data.metadata.tags
    : [];
  return new Set(tags.map(parseEhTag).filter(Boolean).map((tag) => tag.key));
}

function ehTagFilterActive() {
  return state.ehTagFilter.modes.size > 0;
}

function ehEntryMatchesTagFilter(entry) {
  if (!ehTagFilterActive()) return true;
  const entryTags = ehEntryTagKeys(entry);
  const includeGroups = new Map();
  for (const [key, mode] of state.ehTagFilter.modes) {
    if (mode === "exclude" && entryTags.has(key)) return false;
    if (mode !== "include") continue;
    const namespace = key.slice(0, key.indexOf(":"));
    if (!includeGroups.has(namespace)) includeGroups.set(namespace, new Set());
    includeGroups.get(namespace).add(key);
  }
  for (const tags of includeGroups.values()) {
    if (![...tags].some((key) => entryTags.has(key))) return false;
  }
  return true;
}

function entryVisible(source, entry, showWeak = weakEvidenceVisible()) {
  if (entry.weak && !showWeak) return false;
  if (source.site === "exhentai" && !ehEntryMatchesTagFilter(entry)) return false;
  return true;
}

function visibleAddressEntries(source, showWeak = weakEvidenceVisible()) {
  return source.addresses.filter((entry) => entryVisible(source, entry, showWeak));
}

function setPill(element, kind, text) {
  element.className = `status-pill ${kind}`;
  const dot = node("span", "status-dot");
  element.replaceChildren(dot, document.createTextNode(text));
}

async function api(path, options = {}) {
  const headers = new Headers(options.headers || {});
  if (options.body !== undefined && !headers.has("Content-Type")) {
    headers.set("Content-Type", "application/json");
  }
  const response = await fetch(path, {
    method: options.method || "GET",
    headers,
    body: options.body === undefined ? undefined : JSON.stringify(options.body),
    cache: "no-store",
  });
  const contentType = response.headers.get("content-type") || "";
  let payload = null;
  if (contentType.includes("application/json")) payload = await response.json();
  else {
    const text = await response.text();
    payload = text ? { message: text } : {};
  }
  if (!response.ok) {
    const detail = payload?.error?.message || payload?.message || `${response.status} ${response.statusText}`;
    const error = new Error(detail);
    error.status = response.status;
    error.payload = payload;
    error.requestId = payload?.error?.request_id || response.headers.get("X-Request-ID") || "";
    throw error;
  }
  return payload;
}

async function withBusy(element, busyText, action) {
  const original = element.textContent;
  element.disabled = true;
  element.textContent = busyText;
  try {
    return await action();
  } finally {
    element.textContent = original;
    element.disabled = false;
  }
}

function appendLog(message, kind = "info") {
  const row = node("li");
  const now = node("time", "", new Date().toLocaleTimeString());
  const label = node("span", `log-kind ${kind}`, kind === "error" ? "错误" : kind === "success" ? "完成" : "信息");
  const copy = node("span", "", message);
  row.append(now, label, copy);
  const log = $("#eventLog");
  log.prepend(row);
  while (log.children.length > 80) log.lastElementChild.remove();
}

function formatError(error) {
  const suffix = error.requestId ? `（request_id: ${error.requestId}）` : "";
  return `${error.message || String(error)}${suffix}`;
}

function pretty(value) {
  return JSON.stringify(value, null, 2);
}

function formatTime(timestamp) {
  if (!timestamp) return "—";
  return new Date(Number(timestamp) * 1000).toLocaleString();
}

function shortId(value) {
  const text = String(value || "");
  return text.length > 14 ? `${text.slice(0, 8)}…${text.slice(-4)}` : text || "—";
}

function safeExternalUrl(value) {
  try {
    const parsed = new URL(String(value));
    return ["http:", "https:"].includes(parsed.protocol) ? parsed.href : "";
  } catch (_) {
    return "";
  }
}

function readSourceOptions() {
  const result = {};
  $$("[data-source-config]").forEach((row) => {
    const values = {};
    $$('[data-field]', row).forEach((input) => {
      const value = input.value.trim();
      if (value) values[input.dataset.field] = value;
    });
    if (Object.keys(values).length) result[row.dataset.sourceConfig] = values;
  });
  return result;
}

function integerValue(selector, label, minimum, maximum) {
  const input = $(selector);
  const value = Number(input.value);
  if (!Number.isInteger(value) || value < minimum || value > maximum) {
    throw new Error(`${label}需为 ${minimum}–${maximum} 之间的整数。`);
  }
  return value;
}

function updateProxyView(status) {
  const summary = $("#proxySummary");
  const core = status?.transport_core || {};
  const values = [
    ["运行", status?.running ? "是" : "否"],
    ["节点", status?.total ?? 0],
    ["健康", status?.healthy ?? 0],
    ["租约", status?.leases ?? 0],
    ["传输核心", core.running ? `运行 / ${core.listeners ?? 0}` : core.enabled ? "已启用 / 停止" : "关闭"],
  ];
  summary.replaceChildren();
  values.forEach(([label, value]) => {
    const item = node("div");
    item.append(node("span", "", label), node("strong", "", value));
    summary.append(item);
  });
  if (status?.running) {
    const detail = status.healthy < status.total ? `${status.healthy}/${status.total} 健康` : `${status.total} 个节点`;
    setPill($("#proxyStatusPill"), status.healthy ? "good" : "warn", `代理池运行 · ${detail}`);
  } else if (status?.enabled) {
    setPill($("#proxyStatusPill"), "warn", "代理池已配置但未运行");
  } else {
    setPill($("#proxyStatusPill"), "bad", "代理池配置为关闭");
  }
  const error = $("#proxyError");
  if (status?.last_error) {
    error.textContent = status.last_error;
    error.classList.remove("hidden");
  } else {
    error.classList.add("hidden");
    error.textContent = "";
  }
}

async function refreshProxyStatus() {
  const status = await api("/api/v1/proxy/status");
  updateProxyView(status);
  return status;
}

function setAuthFeedback(site, message = "", kind = "") {
  const card = $(`[data-auth-card="${site}"]`);
  const feedback = card ? $(".auth-feedback", card) : null;
  if (!feedback) return;
  feedback.textContent = message;
  feedback.classList.remove("success", "error");
  if (kind) feedback.classList.add(kind);
  feedback.classList.toggle("hidden", !message);
}

function stopBrowserLoginPolling(site) {
  const timer = state.browserLoginPollers.get(site);
  if (timer) clearTimeout(timer);
  state.browserLoginPollers.delete(site);
}

function scheduleBrowserLoginPoll(site, sessionId) {
  if (!sessionId) return;
  const previous = state.browserLoginSessions.get(site);
  if (previous && previous !== sessionId) stopBrowserLoginPolling(site);
  state.browserLoginSessions.set(site, sessionId);
  if (state.browserLoginPollers.has(site)) return;
  const timer = setTimeout(async () => {
    state.browserLoginPollers.delete(site);
    if (state.browserLoginSessions.get(site) !== sessionId) return;
    try {
      const result = await api(
        `/api/v1/auth/${encodeURIComponent(site)}/login/${encodeURIComponent(sessionId)}`,
      );
      const session = result.session || {};
      renderAuthStatus(result.status);
      if (["starting", "awaiting_login"].includes(session.state)) {
        scheduleBrowserLoginPoll(site, sessionId);
        return;
      }
      state.browserLoginSessions.delete(site);
      if (session.state === "authorized") {
        setAuthFeedback(site, session.message || "登录成功，凭证已持久化。", "success");
        state.authPromptedSites.delete(site);
        appendLog(`${SITE_NAMES[site]} 登录成功，后续将自动复用凭证。`, "success");
      } else if (session.state !== "cancelled") {
        setAuthFeedback(site, session.error || session.message || "登录流程已结束。", "error");
        appendLog(`${SITE_NAMES[site]} 登录窗口：${session.error || session.message || session.state}`, "error");
      }
    } catch (error) {
      state.browserLoginSessions.delete(site);
      setAuthFeedback(site, formatError(error), "error");
      appendLog(`${SITE_NAMES[site]} 登录状态：${formatError(error)}`, "error");
      await refreshAuthStatus(true).catch(() => {});
    }
  }, 800);
  state.browserLoginPollers.set(site, timer);
}

function stopPixivOAuthPolling() {
  if (state.pixivOAuthPoller) clearTimeout(state.pixivOAuthPoller);
  state.pixivOAuthPoller = null;
  state.pixivOAuthSessionId = "";
}

function schedulePixivOAuthPoll(sessionId) {
  if (!sessionId) return;
  if (state.pixivOAuthSessionId !== sessionId) stopPixivOAuthPolling();
  state.pixivOAuthSessionId = sessionId;
  if (state.pixivOAuthPoller) return;
  state.pixivOAuthPoller = setTimeout(async () => {
    state.pixivOAuthPoller = null;
    if (state.pixivOAuthSessionId !== sessionId) return;
    try {
      const status = await api("/api/v1/auth/pixiv");
      renderAuthStatus(status);
      const oauth = status.oauth || null;
      if (status.authorized && !oauth) {
        state.pixivOAuthSessionId = "";
        stopPixivOAuthPolling();
        setAuthFeedback("pixiv", "登录成功，Token 已由后端自动保存。", "success");
        appendLog("Pixiv 登录授权完成，回调已自动捕获并保存。", "success");
        return;
      }
      if (oauth?.session_id === sessionId && PIXIV_OAUTH_ACTIVE.has(oauth.state)) {
        schedulePixivOAuthPoll(sessionId);
        return;
      }
      state.pixivOAuthSessionId = "";
      stopPixivOAuthPolling();
      if (oauth?.state !== "cancelled") {
        const message = oauth?.error || oauth?.message || "Pixiv 登录流程已结束。";
        setAuthFeedback("pixiv", message, "error");
        appendLog(`Pixiv 登录授权：${message}`, "error");
      }
    } catch (error) {
      state.pixivOAuthSessionId = "";
      stopPixivOAuthPolling();
      setAuthFeedback("pixiv", formatError(error), "error");
      appendLog(`Pixiv 登录状态：${formatError(error)}`, "error");
    }
  }, 800);
}

function renderAuthStatus(status) {
  if (!status?.site) return;
  state.auth.set(status.site, status);
  if (status.authorized) state.authPromptedSites.delete(status.site);
  const card = $(`[data-auth-card="${status.site}"]`);
  if (!card) return;
  card.classList.remove("ready", "authorized", "authorizing", "required", "public");
  card.classList.add(status.state || "public");
  $(".auth-state", card).textContent = AUTH_STATE_NAMES[status.state] || status.state || "未知";
  const details = [];
  if (status.browser === "project_chrome") details.push("共享授权 Chrome");
  const cookieInfo = status.cookies || status.browser_session;
  if (cookieInfo?.valid) details.push(`${cookieInfo.cookie_count || 0} 条托管 Cookie`);
  $(".auth-summary", card).textContent = [status.summary, ...details].filter(Boolean).join(" ");
  const clear = $(`[data-auth-clear="${status.site}"]`, card);
  if (clear) clear.classList.toggle("hidden", !status.authorized && !cookieInfo?.present);
  const loginButton = $(`[data-managed-browser-auth="${status.site}"]`, card);
  const cancelButton = $(`[data-managed-browser-cancel="${status.site}"]`, card);
  const login = status.login || null;
  const loginActive = Boolean(login && ["starting", "awaiting_login"].includes(login.state));
  if (loginButton) {
    loginButton.disabled = loginActive;
    loginButton.textContent = loginActive
      ? "等待窗口内登录…"
      : status.authorized
        ? "重新授权"
        : "在共享浏览器中授权";
  }
  if (cancelButton) cancelButton.classList.toggle("hidden", !loginActive);
  if (loginActive) {
    setAuthFeedback(status.site, login.message || "请在共享授权浏览器标签页内完成登录。", "");
    scheduleBrowserLoginPoll(status.site, login.session_id);
  } else if (status.invalidated_at) {
    setAuthFeedback(status.site, "当前凭证已在实际访问中失效，请重新授权。", "error");
  } else if (login?.state === "authorized") {
    setAuthFeedback(status.site, login.message || "登录成功，凭证已持久化。", "success");
  } else if (login?.state === "failed" || login?.state === "timed_out") {
    setAuthFeedback(status.site, login.error || login.message, "error");
  } else if (loginButton) {
    setAuthFeedback(status.site, "", "");
  }
  if (status.site === "pixiv") {
    const button = $("#startPixivOAuth");
    const cancel = $("#cancelPixivOAuth");
    const oauth = status.oauth || null;
    const active = Boolean(oauth?.session_id && PIXIV_OAUTH_ACTIVE.has(oauth.state));
    if (button) {
      button.disabled = active;
      button.textContent = active
        ? "等待共享浏览器登录…"
        : status.authorized
          ? "重新授权"
          : "在共享浏览器中授权";
    }
    if (cancel) cancel.classList.toggle("hidden", !active);
    if (active) {
      setAuthFeedback("pixiv", oauth.message || "请在共享授权 Chrome 中完成 Pixiv 登录。", "");
      schedulePixivOAuthPoll(oauth.session_id);
    } else if (oauth?.state === "failed" || oauth?.state === "timed_out") {
      stopPixivOAuthPolling();
      setAuthFeedback("pixiv", oauth.error || oauth.message || "Pixiv 登录授权失败。", "error");
    } else if (status.authorized) {
      stopPixivOAuthPolling();
      state.pixivOAuthSessionId = "";
      setAuthFeedback("pixiv", "登录有效，Token 已由后端托管。", "success");
    } else if (!status.authorized) {
      stopPixivOAuthPolling();
      setAuthFeedback("pixiv", "", "");
    }
  }
}

async function refreshAuthStatus(quiet = true) {
  try {
    const response = await api("/api/v1/auth");
    (response.items || []).forEach(renderAuthStatus);
    if (!quiet) appendLog("站点授权状态已刷新。", "success");
    return response;
  } catch (error) {
    if (!quiet) appendLog(`读取授权状态：${formatError(error)}`, "error");
    throw error;
  }
}

async function startManagedBrowserLogin(element, site) {
  await withBusy(element, "正在打开…", async () => {
    try {
      const result = await api(`/api/v1/auth/${encodeURIComponent(site)}/login/start`, {
        method: "POST",
      });
      renderAuthStatus(result.status);
      const session = result.session || {};
      if (session.session_id) scheduleBrowserLoginPoll(site, session.session_id);
      setAuthFeedback(site, session.message || "共享授权浏览器标签页已打开。", "");
      appendLog(`${SITE_NAMES[site]} 共享授权浏览器标签页已打开。`, "info");
    } catch (error) {
      setAuthFeedback(site, formatError(error), "error");
      appendLog(`${SITE_NAMES[site]} 登录窗口：${formatError(error)}`, "error");
    }
  });
}

async function cancelManagedBrowserLogin(element, site) {
  const sessionId = state.browserLoginSessions.get(site) || state.auth.get(site)?.login?.session_id;
  if (!sessionId) return;
  await withBusy(element, "关闭中…", async () => {
    try {
      const result = await api(
        `/api/v1/auth/${encodeURIComponent(site)}/login/${encodeURIComponent(sessionId)}`,
        { method: "DELETE" },
      );
      stopBrowserLoginPolling(site);
      state.browserLoginSessions.delete(site);
      renderAuthStatus(result.status);
      setAuthFeedback(site, "授权标签页已关闭。", "");
    } catch (error) {
      setAuthFeedback(site, formatError(error), "error");
      appendLog(`${SITE_NAMES[site]} 关闭登录窗口：${formatError(error)}`, "error");
    }
  });
}

async function startPixivOAuth(element) {
  await withBusy(element, "打开共享浏览器…", async () => {
    try {
      const session = await api("/api/v1/auth/pixiv/oauth/start", { method: "POST" });
      setAuthFeedback(
        "pixiv",
        session.message || "请在共享授权 Chrome 中完成 Pixiv 登录；回调会自动处理。",
        "",
      );
      schedulePixivOAuthPoll(session.session_id);
      appendLog("Pixiv 共享授权浏览器标签页已打开；授权回调将由后端自动捕获。", "info");
    } catch (error) {
      setAuthFeedback("pixiv", formatError(error), "error");
      appendLog(`启动 Pixiv 授权：${formatError(error)}`, "error");
    }
  });
  await refreshAuthStatus(true).catch(() => {});
}

async function cancelPixivOAuth(element) {
  await withBusy(element, "关闭中…", async () => {
    try {
      const status = await api("/api/v1/auth/pixiv/oauth/session", { method: "DELETE" });
      stopPixivOAuthPolling();
      state.pixivOAuthSessionId = "";
      renderAuthStatus(status);
      setAuthFeedback("pixiv", "Pixiv 授权标签页已关闭。", "");
      appendLog("Pixiv 本次授权流程已关闭，已有登录状态保持不变。", "info");
    } catch (error) {
      appendLog(`取消 Pixiv 授权：${formatError(error)}`, "error");
    }
  });
}

async function clearAuth(element, site) {
  if (!window.confirm(`删除 ${SITE_NAMES[site]} 的后端导出凭证？共享浏览器登录状态会继续保留。`)) return;
  await withBusy(element, "清除中…", async () => {
    try {
      const status = await api(`/api/v1/auth/${encodeURIComponent(site)}`, { method: "DELETE" });
      stopBrowserLoginPolling(site);
      state.browserLoginSessions.delete(site);
      state.authPromptedSites.delete(site);
      renderAuthStatus(status);
      if (site === "pixiv") {
        stopPixivOAuthPolling();
        state.pixivOAuthSessionId = "";
      }
      appendLog(`${SITE_NAMES[site]} 后端导出凭证已删除；共享浏览器 Profile 保持原样。`, "success");
    } catch (error) {
      appendLog(`清除 ${SITE_NAMES[site]} 登录：${formatError(error)}`, "error");
    }
  });
}

async function clearAuthBrowserProfile(element) {
  if (!window.confirm("清空 X、Pixiv、EH 共用的项目授权浏览器 Profile？后端已导出的凭证仍由各站点单独管理。")) return;
  await withBusy(element, "清空中…", async () => {
    try {
      await api("/api/v1/auth/browser-profile", { method: "DELETE" });
      stopPixivOAuthPolling();
      state.browserLoginPollers.forEach((timer) => clearTimeout(timer));
      state.browserLoginPollers.clear();
      state.browserLoginSessions.clear();
      await refreshAuthStatus(true);
      appendLog("共享授权浏览器 Profile 已清空；后端导出凭证保持原样。", "success");
    } catch (error) {
      appendLog(`清空授权浏览器：${formatError(error)}`, "error");
    }
  });
}

async function checkConnection(logResult = true) {
  try {
    const health = await fetch("/healthz", { cache: "no-store" });
    if (!health.ok) throw new Error(`健康检查返回 ${health.status}`);
    await api("/api/v1/config");
    setPill($("#apiStatus"), "good", "API 已连接");
    await Promise.all([refreshProxyStatus(), refreshAuthStatus(true), loadRecentBatches(true)]);
    if (logResult) appendLog("后端连接检测通过。", "success");
  } catch (error) {
    setPill($("#apiStatus"), "bad", "API 连接失败");
    if (logResult) appendLog(formatError(error), "error");
  }
}

async function proxyAction(element, path, body, label) {
  await withBusy(element, "处理中…", async () => {
    try {
      const result = await api(path, { method: "POST", body });
      appendLog(`${label}完成。`, "success");
      updateProxyView(result?.status || result);
      await refreshProxyStatus();
    } catch (error) {
      appendLog(`${label}：${formatError(error)}`, "error");
    }
  });
}

function searchPayload() {
  const sites = $$('input[name="site"]:checked').map((item) => item.value);
  if (!sites.length) throw new Error("至少选择一个搜索来源。");
  const keyword = $("#keyword").value.trim();
  if (!keyword) throw new Error("请输入关键词。");
  return {
    keyword,
    sites,
    limit: integerValue("#searchLimit", "证据上限", 1, 200),
    proxy_mode: $("#searchProxyMode").value,
    source_options: readSourceOptions(),
  };
}

function sourceAddressState(source) {
  const seen = new Set();
  return [
    ...(source.addresses || []).map((address) => ({ data: address, selected: false, weak: false })),
    ...(source.weak_evidence || []).map((address) => ({ data: address, selected: false, weak: true })),
  ].filter((entry) => {
    const identity = String(entry.data.url || entry.data.id || "").trim();
    if (!identity) return true;
    if (seen.has(identity)) return false;
    seen.add(identity);
    return true;
  });
}

async function runSearch(event) {
  event.preventDefault();
  let request;
  try {
    request = searchPayload();
  } catch (error) {
    appendLog(error.message, "error");
    return;
  }
  await withBusy($("#searchButton"), "正在聚合搜索…", async () => {
    $("#searchHint").textContent = "正在查询各来源并归并画师主页…";
    try {
      const response = await api("/api/v1/search", { method: "POST", body: request });
      state.searchResponse = response;
      (response.sources || []).forEach((source) => renderAuthStatus(source.auth));
      state.ehTagFilter.modes.clear();
      state.ehTagFilter.query = "";
      $("#ehTagQuery").value = "";
      state.sources = (response.sources || []).map((source) => ({
        data: source,
        site: source.site,
        addresses: sourceAddressState(source),
      }));
      $("#rawSearchResponse").textContent = pretty(response);
      renderSearchResults();
      $("#resultsPanel").classList.remove("hidden");
      $("#searchHint").textContent = `完成：${response.address_count || 0} 个可选地址，${response.weak_evidence_count || 0} 个弱证据。`;
      appendLog(
        `聚合搜索完成：${response.address_count || 0} 个可选地址，${response.weak_evidence_count || 0} 个弱证据。`,
        "success",
      );
      $("#resultsPanel").scrollIntoView({ behavior: "smooth", block: "start" });
    } catch (error) {
      $("#searchHint").textContent = "搜索请求出错，请查看界面事件。";
      appendLog(`聚合搜索：${formatError(error)}`, "error");
    }
  });
}

function move(array, index, delta) {
  const target = index + delta;
  if (target < 0 || target >= array.length) return;
  [array[index], array[target]] = [array[target], array[index]];
  renderSearchResults();
}

function moveVisible(array, entry, delta, predicate) {
  const indices = array
    .map((item, index) => (predicate(item) ? index : -1))
    .filter((index) => index >= 0);
  const position = indices.indexOf(array.indexOf(entry));
  const target = position + delta;
  if (position < 0 || target < 0 || target >= indices.length) return;
  [array[indices[position]], array[indices[target]]] = [
    array[indices[target]],
    array[indices[position]],
  ];
  renderSearchResults();
}

function weakEvidenceVisible() {
  return $("#showWeakEvidence").checked;
}

function renderEhTagFilter() {
  const panel = $("#ehTagFilter");
  const groupsRoot = $("#ehTagGroups");
  const source = state.sources.find((item) => item.site === "exhentai");
  const facets = Array.isArray(source?.data?.tag_facets)
    ? source.data.tag_facets
    : [];
  if (!source || !facets.length) {
    panel.classList.add("hidden");
    groupsRoot.replaceChildren();
    return;
  }

  panel.classList.remove("hidden");
  const total = source.addresses.filter((entry) => !entry.weak).length;
  const matched = source.addresses.filter(
    (entry) => !entry.weak && ehEntryMatchesTagFilter(entry),
  ).length;
  const includeCount = [...state.ehTagFilter.modes.values()].filter(
    (mode) => mode === "include",
  ).length;
  const excludeCount = state.ehTagFilter.modes.size - includeCount;
  const hiddenSelected = source.addresses.filter(
    (entry) => entry.selected && !ehEntryMatchesTagFilter(entry),
  ).length;
  $("#ehTagFilterHint").textContent = ehTagFilterActive()
    ? `当前显示 ${matched}/${total} 个 EH 画廊 · 包含 ${includeCount} 个标签 · 排除 ${excludeCount} 个标签${hiddenSelected ? ` · 筛选外已选 ${hiddenSelected}` : ""}`
    : `当前显示全部 ${total} 个 EH 画廊；同组标签取任一，跨组条件需同时满足。`;
  $("#clearEhTagFilters").disabled = !ehTagFilterActive() && !state.ehTagFilter.query;

  const query = state.ehTagFilter.query.trim().toLowerCase();
  groupsRoot.replaceChildren();
  let visibleGroupCount = 0;
  facets.forEach((facet) => {
    const namespace = String(facet.namespace || "unknown");
    const labelText = String(facet.label || namespace);
    const groupMatches = `${namespace} ${labelText}`.toLowerCase().includes(query);
    const tags = (facet.tags || []).filter((tagItem) => {
      if (!query || groupMatches) return true;
      return `${tagItem.tag || ""} ${tagItem.value || ""}`.toLowerCase().includes(query);
    });
    if (!tags.length) return;
    visibleGroupCount += 1;

    const details = node("details", "eh-tag-group");
    const groupHasMode = (facet.tags || []).some((tagItem) => {
      const parsed = parseEhTag(tagItem.tag);
      return parsed && state.ehTagFilter.modes.has(parsed.key);
    });
    details.open = Boolean(query) || groupHasMode;
    const summary = node("summary", "eh-tag-group-summary");
    summary.append(
      node("code", "eh-tag-namespace", namespace),
      node("span", "eh-tag-group-label", labelText),
      node(
        "span",
        "eh-tag-group-count",
        `${facet.tag_count || tags.length} 标签 / ${facet.gallery_count || 0} 画廊`,
      ),
    );
    const options = node("div", "eh-tag-options");
    tags.forEach((tagItem) => {
      const parsed = parseEhTag(tagItem.tag);
      if (!parsed) return;
      const mode = state.ehTagFilter.modes.get(parsed.key) || "";
      const option = node("button", `eh-tag-option${mode ? ` ${mode}` : ""}`);
      option.type = "button";
      option.dataset.tagKey = parsed.key;
      option.dataset.mode = mode || "none";
      option.title = mode === "include"
        ? "当前为包含；再次点击切换为排除"
        : mode === "exclude"
          ? "当前为排除；再次点击清除条件"
          : "点击设为包含条件";
      option.setAttribute(
        "aria-label",
        `${labelText}：${parsed.value}，命中 ${tagItem.count || 0} 个画廊，${mode || "未筛选"}`,
      );
      option.append(
        node("span", "eh-tag-option-name", parsed.value),
        node("span", "eh-tag-option-count", tagItem.count || 0),
      );
      option.addEventListener("click", () => {
        if (!mode) state.ehTagFilter.modes.set(parsed.key, "include");
        else if (mode === "include") state.ehTagFilter.modes.set(parsed.key, "exclude");
        else state.ehTagFilter.modes.delete(parsed.key);
        renderSearchResults();
      });
      options.append(option);
    });
    details.append(summary, options);
    groupsRoot.append(details);
  });
  if (!visibleGroupCount) {
    groupsRoot.append(node("div", "empty-addresses", "当前标签检索词没有匹配项。"));
  }
}

function renderSearchResults() {
  const response = state.searchResponse || {};
  $("#searchSummary").textContent = `关键词“${response.keyword || ""}” · ${response.address_count || 0} 个可选地址 · ${response.weak_evidence_count || 0} 个弱证据`;
  renderEhTagFilter();
  const root = $("#sourceResults");
  root.replaceChildren();

  state.sources.forEach((source, sourceIndex) => {
    const data = source.data;
    const showWeak = weakEvidenceVisible();
    const visibleAddresses = visibleAddressEntries(source, showWeak);
    const selectableCount = source.addresses.filter((item) => !item.weak).length;
    const verifiedCount = source.addresses.filter(
      (item) => !item.weak && item.data.confidence === "verified",
    ).length;
    const siteSearchCount = selectableCount - verifiedCount;
    const weakCount = source.addresses.filter((item) => item.weak).length;
    const card = node("article", `source-card ${data.status || ""}`);
    const header = node("div", "source-header");
    const title = node("div", "source-title");
    title.append(
      node("span", "source-order", sourceIndex + 1),
      node("h3", "", SITE_NAMES[source.site] || source.site),
      statusBadge(data.status),
    );
    const actions = node("div", "source-actions");
    const toggle = button(
      visibleAddresses.some((item) => !item.selected) ? "全选本来源" : "取消本来源",
      "ghost compact",
      () => {
        const next = visibleAddresses.some((item) => !item.selected);
        visibleAddresses.forEach((item) => { item.selected = next; });
        renderSearchResults();
      },
    );
    toggle.disabled = !visibleAddresses.length;
    const up = button("↑", "secondary compact icon-button", () => move(state.sources, sourceIndex, -1), "来源上移");
    const down = button("↓", "secondary compact icon-button", () => move(state.sources, sourceIndex, 1), "来源下移");
    up.disabled = sourceIndex === 0;
    down.disabled = sourceIndex === state.sources.length - 1;
    actions.append(toggle, up, down);
    header.append(title, actions);
    card.append(header);

    const meta = node("div", "source-meta");
    meta.textContent = `站内证据 ${data.evidence_count ?? 0} · 可选 ${selectableCount}（已验证 ${verifiedCount} / 站内候选 ${siteSearchCount}） · 弱证据 ${weakCount} · 尝试 ${data.attempts ?? 0}`;
    if (source.site === "exhentai" && ehTagFilterActive()) {
      meta.textContent += ` · 筛选显示 ${visibleAddresses.length}/${selectableCount}`;
    }
    if (data.preview_count) meta.textContent += ` · 标题/封面 ${data.preview_count}`;
    if (data.proxy?.used) meta.textContent += ` · 搜索代理 ${data.proxy.name || data.proxy.node_id || "已使用"}`;
    card.append(meta);
    if (data.error) card.append(node("p", "source-error", data.error.message || String(data.error)));

    const searchHref = safeExternalUrl(data.search_url);
    if (searchHref) {
      const searchLink = node("a", "source-search-link", "打开该来源的站内搜索结果");
      searchLink.href = searchHref;
      searchLink.target = "_blank";
      searchLink.rel = "noreferrer";
      card.append(searchLink);
    }

    const list = node("div", "address-list");
    if (!visibleAddresses.length) {
      const message = source.site === "exhentai" && ehTagFilterActive()
        ? "当前 EH 标签条件下没有匹配画廊。"
        : weakCount
          ? "此来源没有默认可选地址；可勾选“显示弱证据”人工查看。"
          : "此来源没有可选地址。";
      list.append(node("div", "empty-addresses", message));
    }
    visibleAddresses.forEach((entry, visibleIndex) => {
      const address = entry.data;
      const row = node(
        "div",
        `address-row${entry.selected ? " selected" : ""}${entry.weak ? " weak-evidence" : ""}`,
      );
      const main = node("label", "address-main");
      const checkbox = document.createElement("input");
      checkbox.type = "checkbox";
      checkbox.checked = entry.selected;
      checkbox.addEventListener("change", () => {
        entry.selected = checkbox.checked;
        renderSearchResults();
      });
      const copy = node("div", "address-copy");
      const label = node("div", "address-label");
      label.append(document.createTextNode(address.label || address.tag || address.id || "未命名地址"));
      if (address.address_type) label.append(node("span", "badge", address.address_type));
      if (address.origin) label.append(node("span", "badge", address.origin));
      const confidenceLabel = entry.weak
        ? "弱证据"
        : address.confidence === "site_search"
          ? "站内候选"
          : "已验证";
      const confidenceClass = entry.weak
        ? "partial"
        : address.confidence === "site_search"
          ? "site-search"
          : "succeeded";
      label.append(
        node(
          "span",
          `badge ${confidenceClass}`,
          confidenceLabel,
        ),
      );
      const href = safeExternalUrl(address.url);
      const url = href
        ? node("a", "address-url", address.url || "")
        : node("code", "address-url", address.url || "");
      url.title = address.url || "";
      if (href) {
        url.href = href;
        url.target = "_blank";
        url.rel = "noreferrer";
      }
      const chips = node("div", "address-meta");
      if (address.matched_items !== undefined) chips.append(chip(`匹配 ${address.matched_items}`));
      if (address.media_count) chips.append(chip(`媒体 ${address.media_count}`));
      if (address.related_profiles?.length) chips.append(chip(`关联主页 ${address.related_profiles.length}`));
      (address.evidence_reasons || []).forEach((reason) => {
        chips.append(chip(EVIDENCE_NAMES[reason] || reason));
      });
      copy.append(label, url, chips);
      const metadataTags = Array.isArray(address.metadata?.tags)
        ? address.metadata.tags
          .map((tag) => String(tag || "").trim())
          .filter(Boolean)
        : [];
      if (metadataTags.length) {
        const galleryTags = node("div", "gallery-tags");
        galleryTags.setAttribute("aria-label", "画廊标签");
        galleryTags.append(node("span", "gallery-tags-label", `标签 ${metadataTags.length}`));
        metadataTags.forEach((tagText) => {
          const separator = tagText.indexOf(":");
          const tag = node("span", "gallery-tag");
          if (separator > 0) {
            tag.append(
              node("span", "gallery-tag-namespace", tagText.slice(0, separator)),
              document.createTextNode(tagText.slice(separator)),
            );
          } else {
            tag.textContent = tagText;
          }
          galleryTags.append(tag);
        });
        copy.append(galleryTags);
      }
      const thumbnailUrl = safeExternalUrl(address.thumbnail_url);
      if (thumbnailUrl) {
        const thumbnail = document.createElement("img");
        thumbnail.className = "address-thumbnail";
        thumbnail.src = thumbnailUrl;
        thumbnail.alt = `${address.label || "画廊"} 封面`;
        thumbnail.loading = "lazy";
        thumbnail.referrerPolicy = "no-referrer";
        main.append(checkbox, thumbnail, copy);
      } else {
        main.append(checkbox, copy);
      }

      const controls = node("div", "address-actions");
      const predicate = (item) => entryVisible(source, item, showWeak);
      const addressUp = button("↑", "ghost compact icon-button", () => moveVisible(source.addresses, entry, -1, predicate), "地址上移");
      const addressDown = button("↓", "ghost compact icon-button", () => moveVisible(source.addresses, entry, 1, predicate), "地址下移");
      addressUp.disabled = visibleIndex === 0;
      addressDown.disabled = visibleIndex === visibleAddresses.length - 1;
      controls.append(addressUp, addressDown);
      row.append(main, controls);
      list.append(row);
    });
    card.append(list);
    root.append(card);
  });

  renderRelatedProfiles(response.related_profiles || []);
  updateSelection();
}

function renderRelatedProfiles(profiles) {
  const root = $("#relatedProfiles");
  root.replaceChildren();
  if (!profiles.length) return;
  const details = node("details", "related-block");
  details.append(node("summary", "", `Danbooru 人工维护的其他活动主页（${profiles.length}）`));
  const grid = node("div", "profile-grid");
  profiles.forEach((profile) => {
    const item = node("div", "profile-item");
    item.append(node("strong", "", `${profile.artist_name || "画师"} · ${profile.platform || "external"}`));
    const href = safeExternalUrl(profile.url);
    if (href) {
      const link = node("a", "", profile.url);
      link.href = href;
      link.target = "_blank";
      link.rel = "noreferrer";
      item.append(link);
    } else item.append(node("span", "muted", profile.url || ""));
    grid.append(item);
  });
  details.append(grid);
  root.append(details);
}

function selectedAddressCount() {
  return state.sources.reduce((sum, source) => sum + source.addresses.filter((item) => item.selected).length, 0);
}

function updateSelection() {
  const addresses = selectedAddressCount();
  const sources = state.sources.filter((source) => source.addresses.some((item) => item.selected)).length;
  const hiddenSelected = state.sources.reduce(
    (sum, source) => sum + source.addresses.filter(
      (item) => item.selected && !entryVisible(source, item),
    ).length,
    0,
  );
  $("#selectionCount").textContent = hiddenSelected
    ? `已选 ${sources} 个来源 / ${addresses} 个地址（筛选外 ${hiddenSelected}）`
    : `已选 ${sources} 个来源 / ${addresses} 个地址`;
  $("#crawlHint").textContent = addresses
    ? `将按当前顺序执行 ${sources} 个来源、${addresses} 个地址。`
    : "先勾选至少一个图库地址。";
  $("#startCrawl").disabled = addresses === 0;
}

function buildCrawlPayload() {
  const options = readSourceOptions();
  const sources = [];
  state.sources.forEach((source) => {
    const addresses = source.addresses
      .filter((item) => item.selected)
      .map((item) => ({
        id: item.data.id || undefined,
        label: item.data.label || undefined,
        address_type: item.data.address_type || undefined,
        url: item.data.url,
      }));
    if (!addresses.length) return;
    sources.push({ site: source.site, addresses, ...(options[source.site] || {}) });
  });
  if (!sources.length) throw new Error("至少选择一个图库地址。");
  const payload = {
    sources,
    concurrency: integerValue("#crawlConcurrency", "图片并发", 1, 128),
    max_tasks: integerValue("#maxTasks", "任务保护上限", 1, 100000),
    proxy_mode: $("#crawlProxyMode").value,
  };
  const output = $("#outputDir").value.trim();
  if (output) payload.output_dir = output;
  return payload;
}

function idempotencyKey() {
  if (globalThis.crypto?.randomUUID) return `webui-${crypto.randomUUID()}`;
  return `webui-${Date.now()}-${Math.random().toString(16).slice(2)}`;
}

async function startCrawl() {
  let payload;
  try {
    payload = buildCrawlPayload();
  } catch (error) {
    appendLog(error.message, "error");
    return;
  }
  await withBusy($("#startCrawl"), "正在创建批次…", async () => {
    try {
      const batch = await api("/api/v1/crawls", {
        method: "POST",
        headers: { "Idempotency-Key": idempotencyKey() },
        body: payload,
      });
      state.activeBatchId = batch.id;
      state.activeBatch = batch;
      state.activeBatchTasks = [];
      state.lastPollError = "";
      sessionStorage.setItem("gdl.activeBatch", batch.id);
      appendLog(`顺序批次 ${shortId(batch.id)} 已创建。`, "success");
      renderBatch(batch, []);
      await loadRecentBatches(true);
      await refreshActiveBatch();
      syncPolling();
      $("#batchTitle").scrollIntoView({ behavior: "smooth", block: "start" });
    } catch (error) {
      appendLog(`创建顺序批次：${formatError(error)}`, "error");
    }
  });
  updateSelection();
}

async function loadRecentBatches(quiet = false) {
  try {
    const response = await api("/api/v1/crawls?limit=30");
    const select = $("#recentBatches");
    const current = state.activeBatchId || select.value;
    select.replaceChildren(node("option", "", response.items?.length ? "选择一个批次" : "暂无批次"));
    select.firstElementChild.value = "";
    (response.items || []).forEach((batch) => {
      const option = node(
        "option",
        "",
        `${STATUS_NAMES[batch.status] || batch.status} · ${shortId(batch.id)} · ${formatTime(batch.created_at)}`,
      );
      option.value = batch.id;
      option.title = batch.id;
      if (batch.id === current) option.selected = true;
      select.append(option);
    });
  } catch (error) {
    if (!quiet) appendLog(`读取最近批次：${formatError(error)}`, "error");
  }
}

async function promptTaskAuthFailures(tasks) {
  const sites = new Set(
    (tasks || [])
      .filter((task) => (task.error_class || task.last_error_class) === "authentication")
      .map((task) => task.site)
      .filter(Boolean),
  );
  if (!sites.size) return;
  await refreshAuthStatus(true).catch(() => null);
  sites.forEach((site) => {
    const status = state.auth.get(site);
    if (status?.authorized || state.authPromptedSites.has(site)) return;
    state.authPromptedSites.add(site);
    setAuthFeedback(site, "爬取任务检测到登录凭证失效，请重新授权后继续。", "error");
    appendLog(`${SITE_NAMES[site] || site} 登录凭证已失效，请在授权中心重新登录。`, "error");
  });
}

async function refreshActiveBatch() {
  if (!state.activeBatchId) {
    const selected = $("#recentBatches").value;
    if (selected) {
      state.activeBatchId = selected;
      sessionStorage.setItem("gdl.activeBatch", selected);
    }
  }
  if (!state.activeBatchId) {
    appendLog("请先提交或载入一个批次。", "info");
    return;
  }
  const batchId = state.activeBatchId;
  if (state.refreshingBatchId === batchId) return;
  state.refreshingBatchId = batchId;
  const requestToken = ++state.batchRequestToken;
  try {
    const [batch, taskPage] = await Promise.all([
      api(`/api/v1/crawls/${encodeURIComponent(batchId)}`),
      api(`/api/v1/crawls/${encodeURIComponent(batchId)}/tasks?limit=${TASK_DISPLAY_LIMIT}`),
    ]);
    if (requestToken !== state.batchRequestToken || state.activeBatchId !== batchId) return;
    state.activeBatch = batch;
    state.lastPollError = "";
    const tasks = taskPage.items || [];
    state.activeBatchTasks = tasks;
    renderBatch(batch, tasks);
    await promptTaskAuthFailures(tasks);
    if (TERMINAL_BATCH.has(batch.status)) {
      clearInterval(state.pollTimer);
      state.pollTimer = null;
    }
  } catch (error) {
    if (requestToken !== state.batchRequestToken || state.activeBatchId !== batchId) return;
    const message = formatError(error);
    if (message !== state.lastPollError) appendLog(`刷新批次：${message}`, "error");
    state.lastPollError = message;
    if (state.activeBatch?.id === batchId) {
      renderBatch(state.activeBatch, state.activeBatchTasks);
    }
  } finally {
    if (requestToken === state.batchRequestToken && state.refreshingBatchId === batchId) {
      state.refreshingBatchId = "";
    }
  }
}

function stat(label, value, title = "") {
  const item = node("div", "stat-card");
  item.append(node("span", "", label));
  const strong = node("strong", "", value);
  if (title) strong.title = title;
  item.append(strong);
  return item;
}

function renderBatch(batch, tasks) {
  const pollError = state.lastPollError;
  $("#emptyBatch").classList.add("hidden");
  $("#batchView").classList.remove("hidden");
  $("#cancelBatch").disabled = Boolean(pollError) || TERMINAL_BATCH.has(batch.status);
  $("#rawBatchResponse").textContent = pretty({ batch, tasks });
  const current = batch.current;
  const terminalCount = Number(batch.succeeded_task_count || 0)
    + Number(batch.failed_task_count || 0)
    + Number(batch.cancelled_task_count || 0);
  const taskCount = Number(batch.task_count || 0);
  const percent = taskCount ? Math.min(100, Math.round((terminalCount / taskCount) * 100)) : 0;
  $("#batchProgress").style.width = `${percent}%`;

  const header = $("#batchHeader");
  const headerStats = [
    stat(pollError ? "最后确认状态" : "批次状态", STATUS_NAMES[batch.status] || batch.status),
    stat("批次 ID", shortId(batch.id), batch.id),
    stat("当前地址", current ? `${SITE_NAMES[current.site] || current.site} · ${STATUS_NAMES[current.status] || current.status}` : "—"),
    stat("图片任务", `${terminalCount} / ${taskCount}`),
    stat("成功 / 失败 / 取消", `${batch.succeeded_task_count || 0} / ${batch.failed_task_count || 0} / ${batch.cancelled_task_count || 0}`),
    stat("图片并发", batch.concurrency ?? "—"),
  ];
  if (pollError) headerStats.unshift(stat("连接状态", "状态刷新失败", pollError));
  header.replaceChildren(...headerStats);

  const sourceRoot = $("#batchSources");
  sourceRoot.replaceChildren();
  (batch.sources || []).forEach((source) => {
    const group = node("div", "batch-source");
    const title = node("div", "batch-title-row");
    title.append(
      node("strong", "", `${Number(source.order) + 1}. ${SITE_NAMES[source.site] || source.site}`),
      statusBadge(source.status),
    );
    group.append(title);
    (source.addresses || []).forEach((address) => {
      const isCurrent = current?.address_id === address.id;
      const row = node("div", `batch-address${isCurrent ? " current-address" : ""}`);
      row.append(
        node("strong", "", `${Number(address.address_order) + 1}`),
        statusBadge(address.status),
      );
      const url = node("code", "", address.label || address.url);
      url.title = address.url;
      const countParts = [
        `任务 ${address.planned_task_count || 0}`,
        `成功 ${address.succeeded_task_count || 0}`,
        `失败 ${address.failed_task_count || 0}`,
      ];
      if (address.proxy_probed_at) {
        countParts.push(
          `代理 ${address.healthy_proxy_count || 0}/${address.probed_proxy_count || 0} 可用`,
        );
      }
      const counts = node(
        "span",
        "muted",
        countParts.join(" · "),
      );
      row.append(url, counts);
      group.append(row);
    });
    sourceRoot.append(group);
  });

  const truncated = taskCount > tasks.length;
  $("#taskSummary").textContent = truncated
    ? `显示前 ${tasks.length} / 共 ${taskCount} 个；完整进度以批次计数为准`
    : `显示 ${tasks.length} / 共 ${taskCount} 个`;
  const rows = $("#taskRows");
  rows.replaceChildren();
  if (!tasks.length) {
    const row = node("tr");
    const cell = node("td", "muted", current?.status === "planning" ? "正在枚举当前地址的图片…" : "当前还没有图片任务。" );
    cell.colSpan = 5;
    row.append(cell);
    rows.append(row);
  } else {
    tasks.forEach((task) => {
      const row = node("tr");
      const url = node("td", "url-cell", task.url || "");
      url.title = task.url || "";
      const statusCell = node("td");
      statusCell.append(statusBadge(task.status));
      row.append(
        node("td", "", `${Number(task.source_order) + 1}.${Number(task.address_order) + 1}.${task.sequence_no || 0}`),
        node("td", "", SITE_NAMES[task.site] || task.site),
        statusCell,
        node("td", "", `${task.attempt_count || 0}/${task.max_attempts || 0}`),
        url,
      );
      rows.append(row);
    });
  }
}

function syncPolling() {
  clearInterval(state.pollTimer);
  state.pollTimer = null;
  if ($("#autoPoll").checked && state.activeBatchId && !TERMINAL_BATCH.has(state.activeBatch?.status)) {
    state.pollTimer = setInterval(refreshActiveBatch, 1500);
  }
}

async function cancelActiveBatch() {
  if (!state.activeBatchId || TERMINAL_BATCH.has(state.activeBatch?.status)) return;
  if (!window.confirm("取消当前顺序批次及其活动图片任务？")) return;
  await withBusy($("#cancelBatch"), "取消中…", async () => {
    try {
      const batch = await api(`/api/v1/crawls/${encodeURIComponent(state.activeBatchId)}/cancel`, {
        method: "POST",
        body: {},
      });
      state.activeBatch = batch;
      state.lastPollError = "";
      appendLog(`批次 ${shortId(batch.id)} 已进入取消流程。`, "success");
      await refreshActiveBatch();
      await loadRecentBatches(true);
    } catch (error) {
      appendLog(`取消批次：${formatError(error)}`, "error");
    }
  });
}

function bindEvents() {
  $("#checkConnection").addEventListener("click", (event) => {
    withBusy(event.currentTarget, "检测中…", () => checkConnection(true));
  });
  $("#refreshAuth").addEventListener("click", (event) => {
    withBusy(event.currentTarget, "刷新中…", () => refreshAuthStatus(false));
  });
  $("#clearAuthBrowserProfile").addEventListener("click", (event) => {
    clearAuthBrowserProfile(event.currentTarget);
  });
  $$('[data-managed-browser-auth]').forEach((element) => {
    element.addEventListener("click", () => startManagedBrowserLogin(element, element.dataset.managedBrowserAuth));
  });
  $$('[data-managed-browser-cancel]').forEach((element) => {
    element.addEventListener("click", () => cancelManagedBrowserLogin(element, element.dataset.managedBrowserCancel));
  });
  $$('[data-auth-clear]').forEach((element) => {
    element.addEventListener("click", () => clearAuth(element, element.dataset.authClear));
  });
  $("#startPixivOAuth").addEventListener("click", (event) => startPixivOAuth(event.currentTarget));
  $("#cancelPixivOAuth").addEventListener("click", (event) => cancelPixivOAuth(event.currentTarget));
  $("#searchForm").addEventListener("submit", runSearch);
  $("#selectAll").addEventListener("click", () => {
    const showWeak = weakEvidenceVisible();
    state.sources.forEach((source) => source.addresses.forEach((item) => {
      if (entryVisible(source, item, showWeak)) item.selected = true;
    }));
    renderSearchResults();
  });
  $("#clearSelection").addEventListener("click", () => {
    state.sources.forEach((source) => source.addresses.forEach((item) => { item.selected = false; }));
    renderSearchResults();
  });
  $("#showWeakEvidence").addEventListener("change", (event) => {
    if (!event.currentTarget.checked) {
      state.sources.forEach((source) => source.addresses.forEach((item) => {
        if (item.weak) item.selected = false;
      }));
    }
    renderSearchResults();
  });
  $("#ehTagQuery").addEventListener("input", (event) => {
    state.ehTagFilter.query = event.currentTarget.value;
    renderEhTagFilter();
  });
  $("#clearEhTagFilters").addEventListener("click", () => {
    state.ehTagFilter.modes.clear();
    state.ehTagFilter.query = "";
    $("#ehTagQuery").value = "";
    renderSearchResults();
  });
  $("#startCrawl").addEventListener("click", startCrawl);
  $("#refreshBatch").addEventListener("click", refreshActiveBatch);
  $("#cancelBatch").addEventListener("click", cancelActiveBatch);
  $("#loadBatch").addEventListener("click", async () => {
    const selected = $("#recentBatches").value;
    if (!selected) return appendLog("请选择一个最近批次。", "info");
    state.activeBatchId = selected;
    state.activeBatch = null;
    state.activeBatchTasks = [];
    state.lastPollError = "";
    sessionStorage.setItem("gdl.activeBatch", selected);
    await refreshActiveBatch();
    syncPolling();
  });
  $("#autoPoll").addEventListener("change", syncPolling);
  $("#clearLog").addEventListener("click", () => $("#eventLog").replaceChildren());
  $("#proxyStart").addEventListener("click", (event) => proxyAction(event.currentTarget, "/api/v1/proxy/start", { force_refresh: true }, "启动代理池"));
  $("#proxyReload").addEventListener("click", (event) => proxyAction(event.currentTarget, "/api/v1/proxy/reload", { force_refresh: true }, "重载代理节点"));
  $("#proxyProbe").addEventListener("click", async (event) => {
    await withBusy(event.currentTarget, "探活中…", async () => {
      try {
        const result = await api("/api/v1/proxy/probe", { method: "POST", body: {} });
        appendLog(`代理探活完成：${result.healthy || 0}/${result.total || 0} 健康。`, "success");
        await refreshProxyStatus();
      } catch (error) {
        appendLog(`代理探活：${formatError(error)}`, "error");
      }
    });
  });
  $("#proxyStop").addEventListener("click", (event) => proxyAction(event.currentTarget, "/api/v1/proxy/stop", { force: false }, "停止代理池"));
}

async function boot() {
  bindEvents();
  appendLog("测试界面已载入。", "info");
  await checkConnection(false);
  if (state.activeBatchId) {
    await refreshActiveBatch();
    syncPolling();
  }
}

boot();
