(function () {
  const sidecarsEl = document.getElementById("sidecars");
  const sessionsEl = document.getElementById("sessions");
  const LOG_ENTRY_WINDOW_SIZE = 10;
  const ICONS = {
    software: {
      sidecar: "🏎️",
      idle: "🤷‍♂️",
      tentative: "⌛",
      attached: "🤓",
      disconnected: "⚫"
    },
    operation: {
      read: "👁️",
      list: "👀",
      set: "💾",
      remove: "🗑️",
      test: "🧪",
      reset: "🧹"
    },
    item: {
      symbol: "🔣",
      bundle: "📦",
      expectation: "🎯",
      concept: "🧬",
      binding: "📎",
      rule: "🛡️",
      none: "🔧"
    },
    language: {
      pyexpr: "🐍",
      smt2: "📜",
      expect: "🧾",
      meaning: "📔",
      unknownWithId: "💬",
      idOnly: "🆔",
      none: "🔧",
      hypothesis: "🤔"
    },
    result: {
      sat: "🎉",
      unsat: "💥",
      ok: "✅",
      failure: "❌"
    }
  };
  const state = {
    sidecars: [],
    sessions: [],
    logsBySession: {},
    graphsBySession: {},
    pausedLogBySession: {},
    expandedSessionById: {},
    contentModal: null,
    sidecarOutputByInstance: {}
  };
  const view = {
    sidecars: {
      list: null,
      empty: null,
      cardsById: {}
    },
    sessions: {
      list: null,
      empty: null,
      cardsById: {}
    }
  };

  function fmtCallsPerSecond(value, secondsAgo) {
    if (typeof secondsAgo === "number" && secondsAgo >= 3600) {
      return "";
    }
    if (typeof value !== "number" || !Number.isFinite(value)) {
      return "";
    }
    return value.toFixed(4) + "/second";
  }

  function fmtAgo(iso, secondsAgo) {
    if (!iso) {
      return "no activity";
    }
    if (typeof secondsAgo === "number") {
      if (secondsAgo < 60) {
        return Math.floor(secondsAgo) + " seconds ago";
      }
      if (secondsAgo < 3600) {
        return Math.floor(secondsAgo / 60) + " minutes ago";
      }
      if (secondsAgo < 86400) {
        return Math.floor(secondsAgo / 3600) + " hours ago";
      }
    }
    return iso;
  }

  function safeJsonParse(text, fallback) {
    try {
      const parsed = JSON.parse(text);
      return parsed;
    } catch (_) {
      return fallback;
    }
  }

  async function api(path, opts) {
    const res = await fetch(path, opts || {});
    const data = await res.json();
    if (!res.ok) {
      throw new Error(data.error || "request failed");
    }
    return data;
  }

  function renderSchemaTemplate(schema, root) {
    const properties = (schema && schema.properties && typeof schema.properties === "object")
      ? schema.properties
      : {};
    const fields = [];
    const container = document.createElement("div");
    container.className = "field-grid";

    Object.keys(properties).forEach((name) => {
      const spec = properties[name] || {};
      const label = document.createElement("label");
      label.textContent = name;
      if (typeof spec.description === "string" && spec.description) {
        label.title = spec.description;
      }
      const type = spec.type;
      if (type === "boolean") {
        const input = document.createElement("input");
        input.type = "checkbox";
        input.checked = false;
        container.appendChild(wrapField(label, input));
        fields.push({ name: name, read: function () { return input.checked; } });
        return;
      }
      if (type === "array") {
        const area = document.createElement("textarea");
        area.placeholder = "JSON array, one element per line also accepted";
        container.appendChild(wrapField(label, area));
        fields.push({
          name: name,
          read: function () {
            const text = area.value.trim();
            if (!text) {
              return [];
            }
            const maybe = safeJsonParse(text, null);
            if (Array.isArray(maybe)) {
              return maybe;
            }
            return text.split("\n").map((line) => line.trim()).filter(Boolean);
          }
        });
        return;
      }
      const input = document.createElement("input");
      input.type = "text";
      container.appendChild(wrapField(label, input));
      fields.push({
        name: name,
        read: function () {
          const raw = input.value;
          if (type === "number" || type === "integer") {
            const num = Number(raw);
            return Number.isFinite(num) ? num : raw;
          }
          return raw;
        }
      });
    });

    root.appendChild(container);
    return function buildResponse() {
      const response = {};
      fields.forEach((field) => {
        response[field.name] = field.read();
      });
      return response;
    };
  }

  function wrapField(label, input) {
    const box = document.createElement("div");
    box.appendChild(label);
    box.appendChild(input);
    return box;
  }

  async function setMode(sessionId, mode) {
    await api("/supervisor/api/sessions/" + encodeURIComponent(sessionId) + "/intercept-mode", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ mode: mode })
    });
  }

  async function submitForward(interceptId, argumentsObj) {
    await api("/supervisor/api/intercepts/" + encodeURIComponent(interceptId) + "/forward", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ arguments: argumentsObj })
    });
  }

  async function submitOverride(interceptId, responseObj) {
    await api("/supervisor/api/intercepts/" + encodeURIComponent(interceptId) + "/override", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ response: responseObj })
    });
  }

  async function submitSend(interceptId, responseObj) {
    await api("/supervisor/api/intercepts/" + encodeURIComponent(interceptId) + "/send", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ response: responseObj })
    });
  }

  async function submitSessionMessage(sessionId, payload) {
    return api("/supervisor/api/sessions/" + encodeURIComponent(sessionId) + "/messages", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(payload)
    });
  }

  async function removeSessionData(sessionId) {
    return api("/supervisor/api/sessions/" + encodeURIComponent(sessionId), {
      method: "DELETE"
    });
  }

  async function resetSessionData(sessionId, wipeLogs) {
    return api("/supervisor/api/sessions/" + encodeURIComponent(sessionId) + "/reset", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ wipe_logs: !!wipeLogs })
    });
  }

  async function submitSidecarCommand(instanceId, command, args) {
    return api("/supervisor/api/sidecars/" + encodeURIComponent(instanceId) + "/command", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        command: command,
        args: args || {}
      })
    });
  }

  function canShowRemoveSession(session) {
    if (session && typeof session.can_remove_session === "boolean") {
      return session.can_remove_session;
    }
    return true;
  }

  function removeSessionFromState(sessionId) {
    state.sessions = state.sessions.filter(function (session) {
      return session && session.session_id !== sessionId;
    });
    delete state.logsBySession[sessionId];
    delete state.graphsBySession[sessionId];
    delete state.pausedLogBySession[sessionId];
    delete state.expandedSessionById[sessionId];
  }

  function trimLogEntries(entries) {
    if (!Array.isArray(entries)) {
      return [];
    }
    const cleaned = entries.filter(function (entry) {
      return !!entry && typeof entry === "object";
    });
    return cleaned.slice(-LOG_ENTRY_WINDOW_SIZE);
  }

  function getPausedLogState(sessionId) {
    const existing = state.pausedLogBySession[sessionId];
    if (existing && typeof existing === "object") {
      return existing;
    }
    const created = {
      paused: false,
      buffer: []
    };
    state.pausedLogBySession[sessionId] = created;
    return created;
  }

  function togglePauseLogs(sessionId) {
    const control = getPausedLogState(sessionId);
    if (!control.paused) {
      control.paused = true;
      control.buffer = trimLogEntries(state.logsBySession[sessionId]);
    } else {
      control.paused = false;
      control.buffer = [];
    }
    renderSessionsFromState();
  }

  function visibleLogsForSession(sessionId) {
    const control = state.pausedLogBySession[sessionId];
    if (control && control.paused) {
      return trimLogEntries(control.buffer);
    }
    return trimLogEntries(state.logsBySession[sessionId]);
  }

  function isSessionUiPaused(sessionId) {
    const control = state.pausedLogBySession[sessionId];
    return !!(control && control.paused);
  }

  function pushSessionLogEntry(sessionId, entry) {
    const currentLive = trimLogEntries(state.logsBySession[sessionId]);
    currentLive.push(entry);
    state.logsBySession[sessionId] = trimLogEntries(currentLive);
  }

  function compactJsonLine(value) {
    try {
      return JSON.stringify(value);
    } catch (_) {
      return JSON.stringify(String(value));
    }
  }

  function asObject(value) {
    return (value && typeof value === "object") ? value : {};
  }

  function isSessionExpanded(sessionId) {
    return !!state.expandedSessionById[sessionId];
  }

  function toggleSessionExpanded(sessionId) {
    state.expandedSessionById[sessionId] = !isSessionExpanded(sessionId);
    renderSessionsFromState();
  }

  function getCallName(entry) {
    const call = asObject(entry && entry.call);
    return typeof call.name === "string" ? call.name : "";
  }

  function getCallArguments(entry) {
    const call = asObject(entry && entry.call);
    return asObject(call.arguments);
  }

  function shortToolName(callName) {
    if (typeof callName !== "string" || !callName) {
      return "_unknown_";
    }
    if (callName.indexOf("logic_") === 0) {
      return callName.slice("logic_".length) || callName;
    }
    return callName;
  }

  function getPrimaryContextOp(args) {
    const ops = args && Array.isArray(args.ops) ? args.ops : [];
    for (let i = 0; i < ops.length; i += 1) {
      const op = ops[i];
      if (op && typeof op === "object") {
        return op;
      }
    }
    return null;
  }

  function iconForItemType(typeValue) {
    if (typeof typeValue !== "string" || !typeValue) {
      return ICONS.item.none;
    }
    const normalized = typeValue.replace("-", "_");
    if (normalized === "rule") {
      return ICONS.item.rule;
    }
    if (normalized === "bundle") {
      return ICONS.item.bundle;
    }
    if (normalized === "expectation") {
      return ICONS.item.expectation;
    }
    if (normalized === "concept") {
      return ICONS.item.concept;
    }
    if (normalized === "code_binding") {
      return ICONS.item.binding;
    }
    return ICONS.item.symbol;
  }

  function inferItemId(callName, args) {
    if (callName === "logic_check") {
      return "_hypothesis_";
    }
    if (callName === "logic_list") {
      const show = Array.isArray(args.show) ? args.show.filter(function (item) { return typeof item === "string" && item; }) : [];
      if (show.length > 0) {
        return show.join("|");
      }
      return "all";
    }
    if (callName === "logic_context_patch") {
      const primaryOp = getPrimaryContextOp(args);
      if (primaryOp && typeof primaryOp.id === "string" && primaryOp.id) {
        return primaryOp.id;
      }
      if (primaryOp && typeof primaryOp.op === "string" && primaryOp.op) {
        return primaryOp.op;
      }
    }
    if (typeof args.id === "string" && args.id) {
      return args.id;
    }
    if (typeof args.query === "string" && args.query) {
      return args.query;
    }
    if (typeof args.search === "string" && args.search) {
      return args.search;
    }
    if (typeof args.focus === "string" && args.focus) {
      return args.focus;
    }
    return "_none_";
  }

  function inferOperationIcon(callName, args) {
    if (callName === "logic_read") {
      return ICONS.operation.read;
    }
    if (callName === "logic_list") {
      return ICONS.operation.list;
    }
    if (callName === "logic_check") {
      return ICONS.operation.test;
    }
    if (callName === "logic_reset") {
      return ICONS.operation.reset;
    }
    if (callName === "logic_context_patch") {
      const primaryOp = getPrimaryContextOp(args);
      if (primaryOp && typeof primaryOp.op === "string" && primaryOp.op.indexOf("remove_") === 0) {
        return ICONS.operation.remove;
      }
      return ICONS.operation.set;
    }
    if (callName.indexOf("logic_remove_") === 0) {
      return ICONS.operation.remove;
    }
    if (callName.indexOf("logic_set_") === 0) {
      return ICONS.operation.set;
    }
    return ICONS.item.symbol;
  }

  function inferToolIcon(callName, args, entry) {
    if (callName === "logic_set_rule" || callName === "logic_remove_rule") {
      return ICONS.item.rule;
    }
    if (callName === "logic_set_bundle" || callName === "logic_remove_bundle") {
      return ICONS.item.bundle;
    }
    if (callName === "logic_set_expectation" || callName === "logic_remove_expectation") {
      return ICONS.item.expectation;
    }
    if (callName === "logic_context_patch") {
      const primaryOp = getPrimaryContextOp(args);
      const opName = primaryOp && typeof primaryOp.op === "string" ? primaryOp.op : "";
      if (opName.indexOf("concept") !== -1) {
        return ICONS.item.concept;
      }
      if (opName.indexOf("code_binding") !== -1) {
        return ICONS.item.binding;
      }
      return ICONS.item.symbol;
    }
    if (callName === "logic_read") {
      const response = asObject(entry && entry.response);
      const result = asObject(response.result);
      const item = asObject(result.item);
      return iconForItemType(item.type);
    }
    if (callName === "logic_list" || callName === "logic_check" || callName === "logic_reset") {
      return ICONS.item.none;
    }
    return ICONS.item.symbol;
  }

  function inferLanguageIcon(callName, args, itemId, entry) {
    if (callName === "logic_check") {
      return ICONS.language.hypothesis;
    }
    if (callName === "logic_set_bundle") {
      return ICONS.language.smt2;
    }
    if (callName === "logic_set_expectation") {
      return ICONS.language.expect;
    }
    if (callName === "logic_context_patch") {
      const primaryOp = getPrimaryContextOp(args);
      const opName = primaryOp && typeof primaryOp.op === "string" ? primaryOp.op : "";
      if (opName.indexOf("concept") !== -1) {
        return ICONS.language.meaning;
      }
    }
    if (callName === "logic_read") {
      const response = asObject(entry && entry.response);
      const result = asObject(response.result);
      const item = asObject(result.item);
      if (item.type === "concept") {
        return ICONS.language.meaning;
      }
      const itemLang = typeof item.lang === "string" ? item.lang : "";
      if (itemLang === "pyexpr") {
        return ICONS.language.pyexpr;
      }
      if (itemLang === "smt2") {
        return ICONS.language.smt2;
      }
      if (itemLang === "expect") {
        return ICONS.language.expect;
      }
    }
    const hasId = typeof itemId === "string" && itemId !== "_none_";
    const lang = typeof args.lang === "string" ? args.lang : "";
    if (lang === "pyexpr") {
      return ICONS.language.pyexpr;
    }
    if (lang === "smt2") {
      return ICONS.language.smt2;
    }
    if (lang === "expect") {
      return ICONS.language.expect;
    }
    if (lang) {
      return hasId ? ICONS.language.unknownWithId : ICONS.language.none;
    }
    if (hasId) {
      return ICONS.language.idOnly;
    }
    return ICONS.language.none;
  }

  function inferLogicResultIcon(entry) {
    const response = asObject(entry && entry.response);
    const result = asObject(response.result);
    const statuses = [];
    if (typeof result.status === "string") {
      statuses.push(result.status.toLowerCase());
    }
    const candidate = asObject(result.candidate);
    if (typeof candidate.status === "string") {
      statuses.push(candidate.status.toLowerCase());
    }
    const baseline = asObject(result.baseline);
    if (typeof baseline.status === "string") {
      statuses.push(baseline.status.toLowerCase());
    }
    for (let i = 0; i < statuses.length; i += 1) {
      if (statuses[i] === "sat") {
        return ICONS.result.sat;
      }
      if (statuses[i] === "unsat") {
        return ICONS.result.unsat;
      }
    }
    return "";
  }

  function requestDurationMs(entry) {
    const raw = entry ? entry.request_duration_ms : null;
    const ms = Number(raw);
    if (Number.isFinite(ms) && ms >= 0) {
      return Math.floor(ms);
    }
    return 0;
  }

  function buildLogParts(entry) {
    const callName = getCallName(entry);
    const args = getCallArguments(entry);
    const itemId = inferItemId(callName, args);
    const operation = inferOperationIcon(callName, args);
    const tool = inferToolIcon(callName, args, entry);
    const language = inferLanguageIcon(callName, args, itemId, entry);
    const shortName = shortToolName(callName);
    const response = asObject(entry && entry.response);
    const toolCallResult = response.ok === true ? ICONS.result.ok : ICONS.result.failure;
    const logicResult = inferLogicResultIcon(entry);
    const duration = requestDurationMs(entry) + "ms";
    const parts = [operation, tool, shortName, language, itemId, duration, toolCallResult];
    if (logicResult) {
      parts.push(logicResult);
    }
    return parts;
  }

  function formatLogLine(entry) {
    const parts = buildLogParts(entry);
    return parts.join(" ");
  }

  function buildInterceptModeRow(session) {
    const row = document.createElement("div");
    row.className = "intercept-row";
    const title = document.createElement("strong");
    title.textContent = "Intercept:";
    row.appendChild(title);
    const options = [
      { value: "disabled", label: "Disabled" },
      { value: "call", label: "Call" },
      { value: "reply", label: "Reply" },
      { value: "call_and_reply", label: "Call and Reply" }
    ];
    options.forEach((opt) => {
      const label = document.createElement("label");
      const input = document.createElement("input");
      input.type = "radio";
      input.name = "mode_" + session.session_id;
      input.value = opt.value;
      input.checked = session.intercept_mode === opt.value;
      input.addEventListener("change", function () {
        if (input.checked) {
          setMode(session.session_id, opt.value).catch(window.alert);
        }
      });
      label.appendChild(input);
      label.appendChild(document.createTextNode(" " + opt.label));
      row.appendChild(label);
    });
    return row;
  }

  function buildLogPanel(sessionId, logs) {
    const panel = document.createElement("div");
    panel.className = "log-panel";
    const control = getPausedLogState(sessionId);
    const header = document.createElement("div");
    header.className = "log-controls";
    const pauseBtn = document.createElement("button");
    pauseBtn.type = "button";
    pauseBtn.textContent = control.paused ? "Resume" : "Pause";
    pauseBtn.addEventListener("click", function () {
      togglePauseLogs(sessionId);
    });
    header.appendChild(pauseBtn);
    panel.appendChild(header);
    const lines = document.createElement("div");
    lines.className = "log-lines";
    panel.appendChild(lines);
    if (!Array.isArray(logs) || logs.length === 0) {
      const empty = document.createElement("div");
      empty.className = "log-line mono";
      empty.textContent = "[no logs]";
      lines.appendChild(empty);
      return panel;
    }
    const selected = trimLogEntries(logs);
    selected.forEach((entry) => {
      if (!entry || typeof entry !== "object") {
        return;
      }
      const wrapper = document.createElement("div");
      wrapper.className = "log-entry";

      const line = document.createElement("div");
      line.className = "log-line mono log-line-clickable";
      const parts = buildLogParts(entry);
      parts.forEach(function (part, index) {
        if (index > 0) {
          line.appendChild(document.createTextNode(" "));
        }
        if (index === 4) {
          appendDisplayIdentifier(line, part);
        } else {
          line.appendChild(document.createTextNode(part));
        }
      });
      wrapper.appendChild(line);

      const details = document.createElement("pre");
      details.className = "log-details mono";
      details.hidden = true;
      details.textContent = JSON.stringify(entry, null, 2);
      details.addEventListener("click", function (ev) {
        ev.stopPropagation();
      });
      wrapper.appendChild(details);

      line.addEventListener("click", function () {
        details.hidden = !details.hidden;
        line.classList.toggle("open", !details.hidden);
      });

      lines.appendChild(wrapper);
    });
    return panel;
  }

  function itemTypeLabel(typeValue) {
    if (typeof typeValue !== "string" || !typeValue) {
      return "unknown";
    }
    const normalized = typeValue.replace("-", "_");
    if (normalized === "rule") {
      return "rule";
    }
    if (normalized === "bundle") {
      return "bundle";
    }
    if (normalized === "expectation") {
      return "expectation";
    }
    if (normalized === "concept") {
      return "concept";
    }
    if (normalized === "code_binding") {
      return "code binding";
    }
    return normalized;
  }

  function contentLanguageName(contentLang, rowType) {
    if (contentLang === "pyexpr") {
      return "pyexpr";
    }
    if (contentLang === "smt2") {
      return "smt2";
    }
    if (contentLang === "expect") {
      return "expect";
    }
    if (contentLang === "meaning") {
      return "meaning";
    }
    if (contentLang === "source_code") {
      return "source code";
    }
    if (rowType === "concept") {
      return "meaning";
    }
    if (rowType === "code_binding") {
      return "source code";
    }
    return "unknown";
  }

  function contentLanguageIcon(contentLang, rowType, hasId) {
    if (contentLang === "pyexpr") {
      return ICONS.language.pyexpr;
    }
    if (contentLang === "smt2") {
      return ICONS.language.smt2;
    }
    if (contentLang === "expect") {
      return ICONS.language.expect;
    }
    if (contentLang === "meaning" || rowType === "concept") {
      return ICONS.language.meaning;
    }
    if (contentLang === "source_code" || rowType === "code_binding") {
      return "📄";
    }
    if (contentLang) {
      return hasId ? ICONS.language.unknownWithId : ICONS.language.none;
    }
    if (hasId) {
      return ICONS.language.idOnly;
    }
    return ICONS.language.none;
  }

  function formatBytes(value) {
    const bytes = Number(value);
    if (!Number.isFinite(bytes) || bytes < 0) {
      return "";
    }
    const whole = Math.floor(bytes);
    if (whole < 1024) {
      return whole + " B";
    }
    const kib = whole / 1024;
    if (kib < 10) {
      return kib.toFixed(1) + " KiB";
    }
    return Math.round(kib) + " KiB";
  }

  function isoFromEpoch(epochValue) {
    const epoch = Number(epochValue);
    if (!Number.isFinite(epoch) || epoch <= 0) {
      return "";
    }
    return new Date(epoch * 1000).toISOString();
  }

  function ageFromEpoch(epochValue) {
    const epoch = Number(epochValue);
    if (!Number.isFinite(epoch) || epoch <= 0) {
      return "";
    }
    const seconds = Math.max(0, Math.floor(Date.now() / 1000 - epoch));
    if (seconds < 60) {
      return seconds + "s ago";
    }
    if (seconds < 3600) {
      return Math.floor(seconds / 60) + "m ago";
    }
    if (seconds < 86400) {
      return Math.floor(seconds / 3600) + "h ago";
    }
    return Math.floor(seconds / 86400) + "d ago";
  }

  function identifierTitle(row) {
    const version = Number(row && row.version);
    const versionText = Number.isFinite(version) && version >= 1 ? String(Math.floor(version)) : "unknown";
    const createdEpoch = Number(row && row.created_at);
    let createdText = "unknown";
    if (Number.isFinite(createdEpoch) && createdEpoch > 0) {
      const ageSeconds = Math.max(0, Math.floor(Date.now() / 1000 - createdEpoch));
      createdText = ageSeconds < 86400 ? ageFromEpoch(createdEpoch) : isoFromEpoch(createdEpoch);
    }
    return "version: " + versionText + "\ncreated: " + createdText;
  }

  function prettyContentValue(value) {
    if (value === null || value === undefined) {
      return "[no content]";
    }
    if (typeof value === "string") {
      return value;
    }
    try {
      return JSON.stringify(value, null, 2);
    } catch (_) {
      return String(value);
    }
  }

  function appendDisplayIdentifier(target, text) {
    const value = typeof text === "string" ? text : "";
    if (!value) {
      return;
    }
    if (value === "no-air-control") {
      const em = document.createElement("em");
      em.textContent = value;
      target.appendChild(em);
      return;
    }
    target.appendChild(document.createTextNode(value));
  }

  function closeContentModal() {
    if (!state.contentModal || !state.contentModal.overlay) {
      return;
    }
    state.contentModal.overlay.hidden = true;
  }

  function ensureContentModal() {
    if (state.contentModal && state.contentModal.overlay) {
      return state.contentModal;
    }
    const overlay = document.createElement("div");
    overlay.className = "content-modal-overlay";
    overlay.hidden = true;

    const modal = document.createElement("div");
    modal.className = "content-modal";

    const header = document.createElement("div");
    header.className = "content-modal-head";

    const title = document.createElement("strong");
    title.textContent = "Content";
    header.appendChild(title);

    const closeBtn = document.createElement("button");
    closeBtn.type = "button";
    closeBtn.textContent = "Close";
    closeBtn.addEventListener("click", function () {
      closeContentModal();
    });
    header.appendChild(closeBtn);

    const meta = document.createElement("pre");
    meta.className = "content-modal-meta mono";
    const value = document.createElement("pre");
    value.className = "content-modal-value mono";

    modal.appendChild(header);
    modal.appendChild(meta);
    modal.appendChild(value);
    overlay.appendChild(modal);

    overlay.addEventListener("click", function (ev) {
      if (ev.target === overlay) {
        closeContentModal();
      }
    });

    document.addEventListener("keydown", function (ev) {
      if (ev.key === "Escape") {
        closeContentModal();
      }
    });

    document.body.appendChild(overlay);
    state.contentModal = { overlay: overlay, title: title, meta: meta, value: value };
    return state.contentModal;
  }

  function relationReferenceList(relations, idKey) {
    const out = [];
    const items = Array.isArray(relations) ? relations : [];
    items.forEach(function (relation) {
      if (!relation || typeof relation !== "object") {
        return;
      }
      const relatedId = typeof relation[idKey] === "string" ? relation[idKey] : "";
      if (!relatedId) {
        return;
      }
      const label = typeof relation.label === "string" ? relation.label : "";
      out.push(label ? (label + ":" + relatedId) : relatedId);
    });
    return out;
  }

  function openContentModal(row, context) {
    const modal = ensureContentModal();
    const targetRow = (row && typeof row === "object") ? row : {};
    const ctx = (context && typeof context === "object") ? context : {};
    const originRow = (ctx.originRow && typeof ctx.originRow === "object") ? ctx.originRow : targetRow;
    const relationLabel = typeof ctx.relationLabel === "string" ? ctx.relationLabel : "";
    const itemType = typeof targetRow.type === "string" ? targetRow.type : "";
    const itemId = typeof targetRow.id === "string" ? targetRow.id : "_none_";
    const hasId = itemId !== "_none_";
    const langName = contentLanguageName(targetRow.content_lang, itemType);
    const langIcon = contentLanguageIcon(targetRow.content_lang, itemType, hasId);
    const bytesText = formatBytes(targetRow.content_bytes);
    const version = Number(targetRow.version);
    const versionText = Number.isFinite(version) && version >= 1 ? String(Math.floor(version)) : "unknown";
    const createdIso = isoFromEpoch(targetRow.created_at) || "unknown";
    const ageText = ageFromEpoch(targetRow.created_at);
    const createdText = ageText ? createdIso + " (" + ageText + ")" : createdIso;
    const originId = typeof originRow.id === "string" ? originRow.id : "_none_";
    const originType = itemTypeLabel(typeof originRow.type === "string" ? originRow.type : "");
    const originSuffix = relationLabel ? (", via " + relationLabel) : "";
    const outgoingRefs = relationReferenceList(targetRow.outgoing_relations, "target_id");
    const incomingRefs = relationReferenceList(targetRow.incoming_relations, "source_id");

    modal.title.textContent = langIcon + " " + langName;
    modal.meta.textContent = [
      "identifier: " + itemId,
      "type: " + itemTypeLabel(itemType),
      "origin: " + originId + " (" + originType + originSuffix + ")",
      "references_outgoing: " + (outgoingRefs.length ? outgoingRefs.join(", ") : "[none]"),
      "references_incoming: " + (incomingRefs.length ? incomingRefs.join(", ") : "[none]"),
      "version: " + versionText,
      "date: " + createdText,
      "bytes: " + (bytesText || "unknown")
    ].join("\n");
    modal.value.textContent = prettyContentValue(targetRow.content_value);
    modal.overlay.hidden = false;
  }

  function appendRelationLines(cell, relations, idKey, idToType, idToRow, originRow) {
    const items = Array.isArray(relations) ? relations : [];
    items.forEach(function (relation) {
      if (!relation || typeof relation !== "object") {
        return;
      }
      const relatedId = typeof relation[idKey] === "string" ? relation[idKey] : "";
      if (!relatedId) {
        return;
      }
      const relatedType = typeof idToType[relatedId] === "string" ? idToType[relatedId] : "";
      const relatedRow = idToRow && typeof idToRow[relatedId] === "object" ? idToRow[relatedId] : null;
      const relationLabel = typeof relation.label === "string" ? relation.label : "";
      const line = document.createElement("button");
      line.type = "button";
      line.className = "graph-relation-line relation-open mono";
      const icon = document.createElement("span");
      icon.textContent = iconForItemType(relatedType);
      icon.title = itemTypeLabel(relatedType);
      line.appendChild(icon);
      line.appendChild(document.createTextNode(" "));
      appendDisplayIdentifier(line, relatedId);
      if (relatedRow) {
        line.addEventListener("click", function () {
          openContentModal(relatedRow, { originRow: originRow, relationLabel: relationLabel });
        });
      } else {
        line.disabled = true;
      }
      cell.appendChild(line);
    });
  }

  function buildGraphElement(sessionId) {
    const rawTablePayload = state.graphsBySession[sessionId];
    const tablePayload = (rawTablePayload && typeof rawTablePayload === "object") ? rawTablePayload : { rows: [] };
    const rows = Array.isArray(tablePayload.rows) ? tablePayload.rows : [];
    const wrap = document.createElement("div");
    wrap.className = "graph-table-wrap";
    const table = document.createElement("table");
    table.className = "graph-table";

    const thead = document.createElement("thead");
    const headerRow = document.createElement("tr");
    ["Identifier", "Content", "Outgoing", "Incoming"].forEach((label) => {
      const th = document.createElement("th");
      th.textContent = label;
      headerRow.appendChild(th);
    });
    thead.appendChild(headerRow);
    table.appendChild(thead);

    const tbody = document.createElement("tbody");
    if (rows.length === 0) {
      const tr = document.createElement("tr");
      const td = document.createElement("td");
      td.colSpan = 4;
      td.className = "graph-table-empty";
      td.textContent = "[no logic]";
      tr.appendChild(td);
      tbody.appendChild(tr);
    } else {
      const idToType = {};
      const idToRow = {};
      rows.forEach(function (row) {
        if (row && typeof row === "object" && typeof row.id === "string" && row.id) {
          idToType[row.id] = typeof row.type === "string" ? row.type : "";
          idToRow[row.id] = row;
        }
      });
      rows.forEach((row) => {
        if (!row || typeof row !== "object") {
          return;
        }
        const tr = document.createElement("tr");
        const itemType = typeof row.type === "string" ? row.type : "";
        const itemId = typeof row.id === "string" ? row.id : "";

        const idCell = document.createElement("td");
        idCell.className = "graph-id-cell mono";
        const typeIcon = document.createElement("span");
        typeIcon.textContent = iconForItemType(itemType);
        typeIcon.title = itemTypeLabel(itemType);
        idCell.appendChild(typeIcon);
        idCell.appendChild(document.createTextNode(" "));
        appendDisplayIdentifier(idCell, itemId || "_none_");
        idCell.title = identifierTitle(row);
        tr.appendChild(idCell);

        const contentCell = document.createElement("td");
        contentCell.className = "graph-content-cell mono";
        const hasId = itemId !== "_none_" && !!itemId;
        const contentIcon = contentLanguageIcon(row.content_lang, itemType, hasId);
        const contentName = contentLanguageName(row.content_lang, itemType);
        const contentBytes = formatBytes(row.content_bytes);
        const contentBtn = document.createElement("button");
        contentBtn.className = "content-open";
        contentBtn.title = contentName;
        contentBtn.textContent = contentIcon + (contentBytes ? " " + contentBytes : "");
        contentBtn.addEventListener("click", function () {
          openContentModal(row, { originRow: row });
        });
        contentCell.appendChild(contentBtn);
        tr.appendChild(contentCell);

        const outgoingCell = document.createElement("td");
        outgoingCell.className = "graph-outgoing-cell mono";
        appendRelationLines(outgoingCell, row.outgoing_relations, "target_id", idToType, idToRow, row);
        tr.appendChild(outgoingCell);

        const incomingCell = document.createElement("td");
        incomingCell.className = "graph-incoming-cell mono";
        appendRelationLines(incomingCell, row.incoming_relations, "source_id", idToType, idToRow, row);
        tr.appendChild(incomingCell);

        tbody.appendChild(tr);
      });
    }
    table.appendChild(tbody);
    wrap.appendChild(table);
    return wrap;
  }

  function buildCallInterceptBlock(item) {
    const box = document.createElement("div");
    box.className = "intercept-block";
    const title = document.createElement("div");
    title.innerHTML = "(" + item.session_id + ") <strong>Tool call: " + item.tool_name + "</strong>";
    box.appendChild(title);

    const area = document.createElement("textarea");
    area.value = JSON.stringify(item.tool_arguments || {}, null, 2);
    box.appendChild(area);

    const btnRow = document.createElement("div");
    btnRow.className = "btn-row";
    const overrideBtn = document.createElement("button");
    overrideBtn.textContent = "Override";
    const forwardBtn = document.createElement("button");
    forwardBtn.textContent = "Forward to " + item.tool_name;
    btnRow.appendChild(overrideBtn);
    btnRow.appendChild(forwardBtn);
    box.appendChild(btnRow);

    const overrideWrap = document.createElement("div");
    overrideWrap.style.display = "none";
    box.appendChild(overrideWrap);
    let buildTemplatePayload = null;

    overrideBtn.addEventListener("click", function () {
      overrideWrap.style.display = "block";
      overrideWrap.innerHTML = "";
      const schemaTitle = document.createElement("div");
      schemaTitle.textContent = "Response template";
      overrideWrap.appendChild(schemaTitle);
      buildTemplatePayload = renderSchemaTemplate(item.output_schema || {}, overrideWrap);
      const sendBtn = document.createElement("button");
      sendBtn.className = "primary";
      sendBtn.textContent = "Send";
      sendBtn.addEventListener("click", function () {
        const responseObj = buildTemplatePayload ? buildTemplatePayload() : {};
        submitOverride(item.intercept_id, responseObj).catch(window.alert);
      });
      overrideWrap.appendChild(sendBtn);
    });

    forwardBtn.addEventListener("click", function () {
      const parsed = safeJsonParse(area.value, {});
      submitForward(item.intercept_id, parsed).catch(window.alert);
    });

    return box;
  }

  function buildReplyInterceptBlock(item) {
    const box = document.createElement("div");
    box.className = "intercept-block";
    const title = document.createElement("div");
    title.innerHTML = "(" + item.session_id + ") <strong>Tool reply: " + item.tool_name + "</strong>";
    box.appendChild(title);

    const callPre = document.createElement("pre");
    callPre.textContent = JSON.stringify(item.call_payload || {}, null, 2);
    box.appendChild(callPre);

    const area = document.createElement("textarea");
    area.value = JSON.stringify(item.tool_response || {}, null, 2);
    box.appendChild(area);

    const row = document.createElement("div");
    row.className = "btn-row";
    const sendBtn = document.createElement("button");
    sendBtn.className = "primary";
    sendBtn.textContent = "Send";
    sendBtn.addEventListener("click", function () {
      const parsed = safeJsonParse(area.value, {});
      submitSend(item.intercept_id, parsed).catch(window.alert);
    });
    row.appendChild(sendBtn);
    box.appendChild(row);
    return box;
  }

  function buildMessageComposeCard(sessionId) {
    const card = document.createElement("div");
    card.className = "intercept-block";
    const details = document.createElement("details");
    details.className = "message-compose";
    const summary = document.createElement("summary");
    summary.innerHTML = "<strong>Send message to agent</strong>";
    details.appendChild(summary);

    const content = document.createElement("div");
    content.className = "message-compose-content";

    const desc = document.createElement("div");
    desc.className = "hint small";
    desc.textContent = "Uses MCP logging notification. Send is enabled only when a client is currently active for this session.";
    content.appendChild(desc);

    const fieldGrid = document.createElement("div");
    fieldGrid.className = "field-grid compose-field-grid";

    const messageLabel = document.createElement("label");
    messageLabel.textContent = "message (required)";
    messageLabel.title = "Primary instruction for the agent. Common usage: concise imperative request, e.g. 'Run logic_check in compact mode and report failures only.'";
    const message = document.createElement("textarea");
    message.placeholder = "What should the agent do next?";
    fieldGrid.appendChild(wrapField(messageLabel, message));

    const levelLabel = document.createElement("label");
    levelLabel.textContent = "level";
    levelLabel.title = "MCP logging severity for this notification. Common usage: info for routine guidance, notice for operator requests, warning/error for urgent intervention.";
    const level = document.createElement("select");
    ["debug", "info", "notice", "warning", "error", "critical", "alert", "emergency"].forEach((value) => {
      const option = document.createElement("option");
      option.value = value;
      option.textContent = value;
      if (value === "info") {
        option.selected = true;
      }
      level.appendChild(option);
    });
    fieldGrid.appendChild(wrapField(levelLabel, level));

    const titleLabel = document.createElement("label");
    titleLabel.textContent = "title";
    titleLabel.title = "Optional short subject shown with the message. Common usage: brief category like 'Supervisor Request', 'Investigation', or ticket summary.";
    const title = document.createElement("input");
    title.type = "text";
    title.placeholder = "Optional subject";
    fieldGrid.appendChild(wrapField(titleLabel, title));

    const sourceLabel = document.createElement("label");
    sourceLabel.textContent = "source";
    sourceLabel.title = "Origin label (free text). Common usage: stable sender identifier such as 'supervisor', 'supervisor-ui', 'ops-console', or team/service name.";
    const source = document.createElement("input");
    source.type = "text";
    source.value = "supervisor";
    fieldGrid.appendChild(wrapField(sourceLabel, source));

    const tagsLabel = document.createElement("label");
    tagsLabel.textContent = "tags";
    tagsLabel.title = "Optional comma-separated tags. Common usage: searchable routing labels like 'incident', 'priority-high', 'billing', 'experiment'.";
    const tags = document.createElement("input");
    tags.type = "text";
    tags.placeholder = "incident,priority-high";
    fieldGrid.appendChild(wrapField(tagsLabel, tags));

    const contextLabel = document.createElement("label");
    contextLabel.textContent = "context";
    contextLabel.title = "Optional JSON object with machine-readable metadata. Common usage: include ticket IDs, run IDs, correlation IDs, or environment fields for downstream automation.";
    const context = document.createElement("textarea");
    context.placeholder = "{\"ticket\":\"ABC-123\"}";
    fieldGrid.appendChild(wrapField(contextLabel, context));

    content.appendChild(fieldGrid);

    const row = document.createElement("div");
    row.className = "btn-row";
    const sendButton = document.createElement("button");
    sendButton.className = "primary";
    sendButton.textContent = "Send to Agent";
    sendButton.disabled = true;
    row.appendChild(sendButton);
    content.appendChild(row);

    const status = document.createElement("div");
    status.className = "hint small";
    status.textContent = "Agent stream is offline. You can compose, but send is disabled.";
    content.appendChild(status);

    sendButton.addEventListener("click", function () {
      const contextValue = safeJsonParse(context.value, {});
      const tagValues = tags.value
        .split(",")
        .map(function (item) { return item.trim(); })
        .filter(Boolean);
      submitSessionMessage(sessionId, {
        message: message.value,
        level: level.value,
        title: title.value,
        source: source.value,
        tags: tagValues,
        context: contextValue && typeof contextValue === "object" ? contextValue : {}
      }).then(function (result) {
        if (result.delivered) {
          status.textContent = "Message sent.";
        } else {
          status.textContent = "Not delivered: " + (result.reason || "session_offline");
        }
      }).catch(function (err) {
        status.textContent = "Send failed: " + err.message;
      });
    });

    details.appendChild(content);
    card.appendChild(details);
    return {
      card: card,
      setOnline: function (isOnline) {
        const online = !!isOnline;
        sendButton.disabled = !online;
        if (online) {
          if (status.textContent === "Agent stream is offline. You can compose, but send is disabled.") {
            status.textContent = "Agent stream is online.";
          }
        } else {
          status.textContent = "Agent stream is offline. You can compose, but send is disabled.";
        }
      }
    };
  }

  function sidecarStateIcon(sidecar) {
    const value = sidecar && typeof sidecar.state === "string" ? sidecar.state : "Disconnected";
    if (value === "Idle") {
      return ICONS.software.idle;
    }
    if (value === "Tentative") {
      return ICONS.software.tentative;
    }
    if (value === "Attached") {
      return ICONS.software.attached;
    }
    return ICONS.software.disconnected;
  }

  function sidecarStateLabel(sidecar) {
    return sidecar && typeof sidecar.state === "string" ? sidecar.state : "Disconnected";
  }

  function stringifyResult(result) {
    try {
      return JSON.stringify(result, null, 2);
    } catch (_) {
      return String(result);
    }
  }

  function setSidecarOutput(instanceId, textValue) {
    if (!instanceId || typeof instanceId !== "string") {
      return;
    }
    state.sidecarOutputByInstance[instanceId] = String(textValue || "");
  }

  function ensureSidecarsRoot() {
    if (!sidecarsEl) {
      return null;
    }
    if (view.sidecars.list && view.sidecars.empty) {
      return view.sidecars;
    }
    sidecarsEl.innerHTML = "";

    const heading = document.createElement("h2");
    heading.textContent = "LogiCars";
    sidecarsEl.appendChild(heading);

    const hint = document.createElement("p");
    hint.className = "hint";
    hint.textContent = "Connected sidecars are shown before sessions. Disconnected sidecars are retained for up to one hour.";
    sidecarsEl.appendChild(hint);

    const empty = document.createElement("div");
    empty.className = "hint";
    empty.textContent = "No LogiCars connected.";
    sidecarsEl.appendChild(empty);

    const list = document.createElement("div");
    sidecarsEl.appendChild(list);
    view.sidecars.list = list;
    view.sidecars.empty = empty;
    return view.sidecars;
  }

  function createSidecarCard(instanceId) {
    const refs = {
      instanceId: instanceId,
      clientButtons: {}
    };
    const card = document.createElement("article");
    card.className = "sidecar-card";

    const head = document.createElement("div");
    head.className = "sidecar-head";
    const title = document.createElement("div");
    title.className = "sidecar-title";
    const meta = document.createElement("div");
    meta.className = "sidecar-meta mono";
    head.appendChild(title);
    head.appendChild(meta);
    card.appendChild(head);

    const details = document.createElement("div");
    details.className = "sidecar-details mono";
    card.appendChild(details);

    const controls = document.createElement("div");
    controls.className = "sidecar-controls";

    const sessionRow = document.createElement("div");
    sessionRow.className = "sidecar-row";
    const sessionInput = document.createElement("input");
    sessionInput.type = "text";
    sessionInput.placeholder = "session id";
    const sessionBtn = document.createElement("button");
    sessionBtn.textContent = "Set Session";
    sessionBtn.addEventListener("click", function () {
      const value = sessionInput.value || "";
      submitSidecarCommand(refs.instanceId, "set_session", { session: value }).then(function (result) {
        setSidecarOutput(refs.instanceId, stringifyResult(result.result || result));
        renderSidecarsFromState();
      }).catch(function (err) {
        setSidecarOutput(refs.instanceId, "set_session failed: " + err.message);
        renderSidecarsFromState();
      });
    });
    sessionRow.appendChild(sessionInput);
    sessionRow.appendChild(sessionBtn);
    controls.appendChild(sessionRow);

    ["codex", "claude"].forEach(function (clientName) {
      const row = document.createElement("div");
      row.className = "sidecar-row";
      const label = document.createElement("strong");
      label.textContent = clientName;
      row.appendChild(label);

      const addButton = document.createElement("button");
      addButton.textContent = "Add";
      addButton.addEventListener("click", function () {
        submitSidecarCommand(refs.instanceId, "add_tool", { client: clientName }).then(function (result) {
          setSidecarOutput(refs.instanceId, stringifyResult(result.result || result));
          renderSidecarsFromState();
        }).catch(function (err) {
          setSidecarOutput(refs.instanceId, "add_tool failed: " + err.message);
          renderSidecarsFromState();
        });
      });

      const listButton = document.createElement("button");
      listButton.textContent = "List";
      listButton.addEventListener("click", function () {
        submitSidecarCommand(refs.instanceId, "list_tools", { client: clientName }).then(function (result) {
          setSidecarOutput(refs.instanceId, stringifyResult(result.result || result));
          renderSidecarsFromState();
        }).catch(function (err) {
          setSidecarOutput(refs.instanceId, "list_tools failed: " + err.message);
          renderSidecarsFromState();
        });
      });

      const removeButton = document.createElement("button");
      removeButton.textContent = "Remove";
      removeButton.addEventListener("click", function () {
        submitSidecarCommand(refs.instanceId, "remove_tool", { client: clientName }).then(function (result) {
          setSidecarOutput(refs.instanceId, stringifyResult(result.result || result));
          renderSidecarsFromState();
        }).catch(function (err) {
          setSidecarOutput(refs.instanceId, "remove_tool failed: " + err.message);
          renderSidecarsFromState();
        });
      });

      row.appendChild(addButton);
      row.appendChild(listButton);
      row.appendChild(removeButton);
      controls.appendChild(row);
      refs.clientButtons[clientName] = {
        addButton: addButton,
        listButton: listButton,
        removeButton: removeButton
      };
    });

    const bootstrapRow = document.createElement("div");
    bootstrapRow.className = "sidecar-row";
    const bootstrapButton = document.createElement("button");
    bootstrapButton.className = "primary";
    bootstrapButton.textContent = "Write Bootstrap Files";
    bootstrapButton.addEventListener("click", function () {
      submitSidecarCommand(refs.instanceId, "write_bootstrap", {}).then(function (result) {
        setSidecarOutput(refs.instanceId, stringifyResult(result.result || result));
        renderSidecarsFromState();
      }).catch(function (err) {
        setSidecarOutput(refs.instanceId, "write_bootstrap failed: " + err.message);
        renderSidecarsFromState();
      });
    });
    bootstrapRow.appendChild(bootstrapButton);
    controls.appendChild(bootstrapRow);

    const output = document.createElement("pre");
    output.className = "sidecar-output mono";
    controls.appendChild(output);
    card.appendChild(controls);

    refs.card = card;
    refs.title = title;
    refs.meta = meta;
    refs.details = details;
    refs.sessionInput = sessionInput;
    refs.sessionBtn = sessionBtn;
    refs.bootstrapButton = bootstrapButton;
    refs.output = output;
    return refs;
  }

  function updateSidecarCard(refs, sidecar) {
    const instanceId = typeof sidecar.instance_id === "string" ? sidecar.instance_id : refs.instanceId;
    refs.instanceId = instanceId;
    const local = typeof sidecar.local === "string" ? sidecar.local : "";
    const pid = Number(sidecar.pid);
    const pidText = Number.isFinite(pid) ? String(Math.floor(pid)) : "unknown";
    const seen = ageFromEpoch(sidecar.last_seen_epoch) || "unknown";
    const remote = typeof sidecar.remote === "string" ? sidecar.remote : "";
    const workdir = typeof sidecar.workdir === "string" ? sidecar.workdir : "";
    const sessionId = typeof sidecar.session_id === "string" ? sidecar.session_id : "";
    const toolUrl = typeof sidecar.tool_url === "string" ? sidecar.tool_url : "";
    const connected = !!sidecar.connected;

    refs.title.textContent = ICONS.software.sidecar + " " + sidecarStateIcon(sidecar) + " " + sidecarStateLabel(sidecar);
    refs.meta.textContent = "instance=" + instanceId + " pid=" + pidText + " seen=" + seen + " remote=" + remote + " local=" + local;
    refs.details.textContent = "workdir=" + workdir + "\nsession=" + (sessionId || "[none]") + "\ntool_url=" + (toolUrl || "[none]");
    if (document.activeElement !== refs.sessionInput) {
      refs.sessionInput.value = sessionId;
    }
    refs.sessionBtn.disabled = !connected;
    Object.keys(refs.clientButtons).forEach(function (clientName) {
      const rowButtons = refs.clientButtons[clientName];
      rowButtons.addButton.disabled = !connected;
      rowButtons.listButton.disabled = !connected;
      rowButtons.removeButton.disabled = !connected;
    });
    refs.bootstrapButton.disabled = !connected;
    refs.output.textContent = state.sidecarOutputByInstance[instanceId] || "";
  }

  function renderSidecarsFromState() {
    const root = ensureSidecarsRoot();
    if (!root || !root.list || !root.empty) {
      return;
    }
    const sidecars = Array.isArray(state.sidecars) ? state.sidecars : [];
    const nextIds = {};
    sidecars.forEach(function (sidecar) {
      if (!sidecar || typeof sidecar !== "object") {
        return;
      }
      const instanceId = typeof sidecar.instance_id === "string" ? sidecar.instance_id : "";
      if (!instanceId) {
        return;
      }
      nextIds[instanceId] = true;
      let refs = view.sidecars.cardsById[instanceId];
      if (!refs) {
        refs = createSidecarCard(instanceId);
        view.sidecars.cardsById[instanceId] = refs;
      }
      updateSidecarCard(refs, sidecar);
      root.list.appendChild(refs.card);
    });
    Object.keys(view.sidecars.cardsById).forEach(function (instanceId) {
      if (!nextIds[instanceId]) {
        const refs = view.sidecars.cardsById[instanceId];
        if (refs && refs.card && refs.card.parentNode) {
          refs.card.parentNode.removeChild(refs.card);
        }
        delete view.sidecars.cardsById[instanceId];
      }
    });
    root.empty.hidden = Object.keys(nextIds).length > 0;
  }

  function ensureSessionsRoot() {
    if (!sessionsEl) {
      return null;
    }
    if (view.sessions.list && view.sessions.empty) {
      return view.sessions;
    }
    sessionsEl.innerHTML = "";
    const empty = document.createElement("div");
    empty.textContent = "No sessions available.";
    sessionsEl.appendChild(empty);
    const list = document.createElement("div");
    sessionsEl.appendChild(list);
    view.sessions.list = list;
    view.sessions.empty = empty;
    return view.sessions;
  }

  function createSessionCard(sessionId) {
    const refs = { sessionId: sessionId };
    const card = document.createElement("article");
    card.className = "session-card";

    const headline = document.createElement("div");
    headline.className = "session-headline";
    headline.tabIndex = 0;
    headline.setAttribute("role", "button");
    headline.addEventListener("click", function () {
      toggleSessionExpanded(sessionId);
    });
    headline.addEventListener("keydown", function (ev) {
      if (ev.key === "Enter" || ev.key === " ") {
        ev.preventDefault();
        toggleSessionExpanded(sessionId);
      }
    });
    const left = document.createElement("div");
    left.className = "session-name";
    const right = document.createElement("div");
    right.className = "session-meta";
    const connected = document.createElement("span");
    const cps = document.createElement("span");
    const ago = document.createElement("span");
    right.appendChild(connected);
    right.appendChild(cps);
    right.appendChild(ago);
    headline.appendChild(left);
    headline.appendChild(right);
    card.appendChild(headline);

    const body = document.createElement("div");
    body.className = "session-body";
    const actions = document.createElement("div");
    actions.className = "session-actions";
    body.appendChild(actions);

    const modeHost = document.createElement("div");
    body.appendChild(modeHost);
    const divider = document.createElement("hr");
    divider.className = "divider";
    body.appendChild(divider);

    const graphHost = document.createElement("div");
    body.appendChild(graphHost);

    const compose = buildMessageComposeCard(sessionId);
    body.appendChild(compose.card);

    const logHost = document.createElement("div");
    body.appendChild(logHost);

    const pendingHost = document.createElement("div");
    body.appendChild(pendingHost);

    card.appendChild(body);

    refs.card = card;
    refs.headline = headline;
    refs.left = left;
    refs.connected = connected;
    refs.cps = cps;
    refs.ago = ago;
    refs.body = body;
    refs.actions = actions;
    refs.modeHost = modeHost;
    refs.graphHost = graphHost;
    refs.compose = compose;
    refs.logHost = logHost;
    refs.pendingHost = pendingHost;
    return refs;
  }

  function updateSessionCard(refs, session) {
    const sessionId = session.session_id;
    const logs = visibleLogsForSession(sessionId);
    const expanded = isSessionExpanded(sessionId);
    refs.headline.setAttribute("aria-expanded", expanded ? "true" : "false");
    refs.left.textContent = (expanded ? "▾ " : "▸ ") + sessionId;
    const count = Number.isFinite(session.connected_clients) ? session.connected_clients : 0;
    refs.connected.textContent = count + (count === 1 ? " client" : " clients");
    refs.cps.textContent = fmtCallsPerSecond(session.calls_per_second, session.last_activity_seconds_ago);
    refs.ago.textContent = fmtAgo(session.last_activity_iso, session.last_activity_seconds_ago);
    refs.body.className = expanded ? "session-body open" : "session-body";
    refs.body.hidden = !expanded;

    refs.actions.innerHTML = "";
    if (canShowRemoveSession(session)) {
      const resetButton = document.createElement("button");
      resetButton.className = "warning";
      resetButton.textContent = "Reset session";
      resetButton.addEventListener("click", function () {
        const confirmation = window.confirm(
          "Reset session '" + session.session_id + "'? This clears rules/bundles/expectations/context and can also clear logs."
        );
        if (!confirmation) {
          return;
        }
        resetButton.disabled = true;
        resetSessionData(session.session_id, true).then(function () {
          state.graphsBySession[session.session_id] = {
            session_id: session.session_id,
            row_count: 0,
            rows: []
          };
          state.logsBySession[session.session_id] = [];
          const control = getPausedLogState(session.session_id);
          control.buffer = [];
          renderSessionsFromState();
        }).catch(function (err) {
          resetButton.disabled = false;
          window.alert("Reset failed: " + err.message);
        });
      });
      refs.actions.appendChild(resetButton);

      const removeButton = document.createElement("button");
      removeButton.className = "danger";
      removeButton.textContent = "Remove session";
      removeButton.addEventListener("click", function () {
        const confirmation = window.confirm(
          "Remove session '" + session.session_id + "'? This deletes its entire store directory and logs."
        );
        if (!confirmation) {
          return;
        }
        removeButton.disabled = true;
        removeSessionData(session.session_id).then(function () {
          removeSessionFromState(session.session_id);
          renderSessionsFromState();
        }).catch(function (err) {
          removeButton.disabled = false;
          window.alert("Remove failed: " + err.message);
        });
      });
      refs.actions.appendChild(removeButton);
    }

    refs.modeHost.innerHTML = "";
    refs.modeHost.appendChild(buildInterceptModeRow(session));

    refs.graphHost.innerHTML = "";
    const graphEl = buildGraphElement(sessionId);
    if (graphEl) {
      refs.graphHost.appendChild(graphEl);
    }

    refs.compose.setOnline(!!session.can_send_message);

    refs.logHost.innerHTML = "";
    refs.logHost.appendChild(buildLogPanel(sessionId, logs));

    refs.pendingHost.innerHTML = "";
    const pending = Array.isArray(session.pending_intercepts) ? session.pending_intercepts : [];
    pending.forEach(function (item) {
      if (item.stage === "call") {
        refs.pendingHost.appendChild(buildCallInterceptBlock(item));
      } else if (item.stage === "reply") {
        refs.pendingHost.appendChild(buildReplyInterceptBlock(item));
      }
    });
  }

  function renderSessionsFromState() {
    const root = ensureSessionsRoot();
    if (!root || !root.list || !root.empty) {
      return;
    }
    const sessions = Array.isArray(state.sessions) ? state.sessions : [];
    const nextIds = {};
    sessions.forEach(function (session) {
      if (!session || typeof session !== "object" || typeof session.session_id !== "string" || !session.session_id) {
        return;
      }
      const sessionId = session.session_id;
      nextIds[sessionId] = true;
      let refs = view.sessions.cardsById[sessionId];
      let created = false;
      if (!refs) {
        refs = createSessionCard(sessionId);
        view.sessions.cardsById[sessionId] = refs;
        created = true;
      }
      // When paused, keep the current card DOM untouched so user input and
      // panel state do not get reset by incoming websocket updates.
      if (created || !isSessionUiPaused(sessionId)) {
        updateSessionCard(refs, session);
      }
      root.list.appendChild(refs.card);
    });
    Object.keys(view.sessions.cardsById).forEach(function (sessionId) {
      if (!nextIds[sessionId]) {
        const refs = view.sessions.cardsById[sessionId];
        if (refs && refs.card && refs.card.parentNode) {
          refs.card.parentNode.removeChild(refs.card);
        }
        delete view.sessions.cardsById[sessionId];
      }
    });
    root.empty.hidden = Object.keys(nextIds).length > 0;
  }

  function applySnapshot(payload) {
    const sidecars = payload && Array.isArray(payload.sidecars) ? payload.sidecars : [];
    const sessions = payload && Array.isArray(payload.sessions) ? payload.sessions : [];
    const logsBySession = payload && payload.logs_by_session && typeof payload.logs_by_session === "object"
      ? payload.logs_by_session
      : {};
    const graphsBySession = payload && payload.graphs_by_session && typeof payload.graphs_by_session === "object"
      ? payload.graphs_by_session
      : {};
    const normalizedLogs = {};
    Object.keys(logsBySession).forEach(function (sessionId) {
      normalizedLogs[sessionId] = trimLogEntries(logsBySession[sessionId]);
    });
    const sessionIds = {};
    sessions.forEach(function (session) {
      if (session && typeof session.session_id === "string" && session.session_id) {
        sessionIds[session.session_id] = true;
      }
    });
    Object.keys(state.pausedLogBySession).forEach(function (sessionId) {
      if (!sessionIds[sessionId]) {
        delete state.pausedLogBySession[sessionId];
      }
    });
    Object.keys(state.expandedSessionById).forEach(function (sessionId) {
      if (!sessionIds[sessionId]) {
        delete state.expandedSessionById[sessionId];
      }
    });
    const sidecarIds = {};
    sidecars.forEach(function (sidecar) {
      if (sidecar && typeof sidecar.instance_id === "string" && sidecar.instance_id) {
        sidecarIds[sidecar.instance_id] = true;
      }
    });
    Object.keys(state.sidecarOutputByInstance).forEach(function (instanceId) {
      if (!sidecarIds[instanceId]) {
        delete state.sidecarOutputByInstance[instanceId];
      }
    });
    state.sidecars = sidecars;
    state.sessions = sessions;
    state.logsBySession = normalizedLogs;
    state.graphsBySession = graphsBySession;
    renderSidecarsFromState();
    renderSessionsFromState();
  }

  function websocketUrl() {
    const protocol = window.location.protocol === "https:" ? "wss://" : "ws://";
    return protocol + window.location.host + "/supervisor/ws";
  }

  function connectWebSocket() {
    const ws = new WebSocket(websocketUrl());
    ws.onmessage = function (ev) {
      const payload = safeJsonParse(ev.data, null);
      if (!payload || typeof payload !== "object") {
        return;
      }
      if (payload.type === "snapshot" && payload.data) {
        applySnapshot(payload.data);
        return;
      }
      if (payload.type === "event" && payload.event === "session_log" && payload.data) {
        const data = payload.data;
        if (typeof data.session_id === "string" && data.entry && typeof data.entry === "object") {
          pushSessionLogEntry(data.session_id, data.entry);
          renderSessionsFromState();
        }
        return;
      }
      if (payload.type === "event" && payload.event === "session_graph_updated" && payload.data) {
        const data = payload.data;
        if (typeof data.session_id === "string" && data.table && typeof data.table === "object") {
          state.graphsBySession[data.session_id] = data.table;
          renderSessionsFromState();
        }
      }
    };
    ws.onerror = function () {
      ws.close();
    };
    ws.onclose = function () {
      setTimeout(connectWebSocket, 1000);
    };
  }

  connectWebSocket();
})();
