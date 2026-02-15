(function () {
  const sessionsEl = document.getElementById("sessions");
  const expanded = new Set();
  const LOG_ENTRY_WINDOW_SIZE = 10;
  const ICONS = {
    operation: {
      read: "👁️",
      list: "👀",
      set: "💾",
      remove: "🗑️",
      test: "🧪"
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
      unknownWithId: "💬",
      idOnly: "🆔",
      none: "🔧",
      hypothesis: "🤔"
    },
    result: {
      sat: "🎉",
      unsat: "💥",
      ok: "✅",
      failure: "❌",
      none: "⏺️"
    },
    reply: "🔁"
  };
  const state = {
    sessions: [],
    logsBySession: {},
    graphsBySession: {}
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

  function getCallName(entry) {
    const call = asObject(entry && entry.call);
    return typeof call.name === "string" ? call.name : "";
  }

  function getCallArguments(entry) {
    const call = asObject(entry && entry.call);
    return asObject(call.arguments);
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
      return "_none_";
    }
    if (callName === "logic_context_patch") {
      const primaryOp = getPrimaryContextOp(args);
      if (primaryOp && typeof primaryOp.id === "string" && primaryOp.id) {
        return primaryOp.id;
      }
    }
    if (typeof args.id === "string" && args.id) {
      return args.id;
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
      if (opName.indexOf("rule_meta") !== -1) {
        return ICONS.item.rule;
      }
      if (opName.indexOf("expectation_meta") !== -1) {
        return ICONS.item.expectation;
      }
      return ICONS.item.symbol;
    }
    if (callName === "logic_read") {
      const response = asObject(entry && entry.response);
      const result = asObject(response.result);
      const item = asObject(result.item);
      return iconForItemType(item.type);
    }
    if (callName === "logic_list" || callName === "logic_check") {
      return ICONS.item.none;
    }
    return ICONS.item.symbol;
  }

  function inferLanguageIcon(callName, args, itemId) {
    if (callName === "logic_check") {
      return ICONS.language.hypothesis;
    }
    if (callName === "logic_set_bundle") {
      return ICONS.language.smt2;
    }
    if (callName === "logic_set_expectation") {
      return ICONS.language.expect;
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
    const callName = getCallName(entry);
    const response = asObject(entry && entry.response);
    if (response.ok === true && (callName === "logic_set_bundle" || callName === "logic_set_expectation")) {
      return ICONS.result.sat;
    }
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
    return ICONS.result.none;
  }

  function requestDurationMs(entry) {
    const raw = entry ? entry.request_duration_ms : null;
    const ms = Number(raw);
    if (Number.isFinite(ms) && ms >= 0) {
      return Math.floor(ms);
    }
    return 0;
  }

  function formatCallLogLine(entry) {
    const callName = getCallName(entry);
    const args = getCallArguments(entry);
    const itemId = inferItemId(callName, args);
    const operation = inferOperationIcon(callName, args);
    const tool = inferToolIcon(callName, args, entry);
    const language = inferLanguageIcon(callName, args, itemId);
    return operation + " " + tool + " " + language + " " + itemId + " " + compactJsonLine(entry);
  }

  function formatReplyLogLine(entry) {
    const callName = getCallName(entry);
    const args = getCallArguments(entry);
    const itemId = inferItemId(callName, args);
    const response = asObject(entry && entry.response);
    const result = response.ok === true ? ICONS.result.ok : ICONS.result.failure;
    const logicResult = inferLogicResultIcon(entry);
    const duration = requestDurationMs(entry) + "ms";
    return result + " " + ICONS.reply + " " + logicResult + " " + itemId + " " + duration + " " + compactJsonLine(entry);
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

  function buildLogPanel(_session, logs) {
    const panel = document.createElement("div");
    panel.className = "log-panel";
    const lines = document.createElement("div");
    lines.className = "log-lines";
    panel.appendChild(lines);
    if (!Array.isArray(logs) || logs.length === 0) {
      return panel;
    }
    const selected = logs.slice(-LOG_ENTRY_WINDOW_SIZE);
    selected.forEach((entry) => {
      if (!entry || typeof entry !== "object") {
        return;
      }
      const callLine = document.createElement("div");
      callLine.className = "log-line mono";
      callLine.textContent = formatCallLogLine(entry);
      lines.appendChild(callLine);
      const replyLine = document.createElement("div");
      replyLine.className = "log-line mono";
      replyLine.textContent = formatReplyLogLine(entry);
      lines.appendChild(replyLine);
    });
    return panel;
  }

  function formatOutgoingRelation(value) {
    if (typeof value === "string" && value) {
      return value;
    }
    if (!value || typeof value !== "object") {
      return "";
    }
    const label = typeof value.label === "string" && value.label ? value.label : "related";
    const targetId = typeof value.target_id === "string" && value.target_id ? value.target_id : "?";
    return label + " -> " + targetId;
  }

  function buildGraphElement(sessionId) {
    const tablePayload = state.graphsBySession[sessionId];
    if (!tablePayload || typeof tablePayload !== "object") {
      return null;
    }
    const rows = Array.isArray(tablePayload.rows) ? tablePayload.rows : [];
    const wrap = document.createElement("div");
    wrap.className = "graph-table-wrap";
    const table = document.createElement("table");
    table.className = "graph-table";

    const thead = document.createElement("thead");
    const headerRow = document.createElement("tr");
    ["Type", "Identifier", "Outgoing Relations", "Incoming"].forEach((label) => {
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
      rows.forEach((row) => {
        if (!row || typeof row !== "object") {
          return;
        }
        const tr = document.createElement("tr");
        const itemType = typeof row.type === "string" ? row.type : "";
        const itemId = typeof row.id === "string" ? row.id : "";
        const incomingRaw = Number(row.incoming_relations_count);
        const incoming = Number.isFinite(incomingRaw) ? Math.max(0, Math.floor(incomingRaw)) : 0;

        const typeCell = document.createElement("td");
        typeCell.className = "graph-type-cell";
        typeCell.textContent = iconForItemType(itemType) + " " + (itemType || "unknown");
        tr.appendChild(typeCell);

        const idCell = document.createElement("td");
        idCell.className = "graph-id-cell mono";
        idCell.textContent = itemId || "_none_";
        tr.appendChild(idCell);

        const outgoingCell = document.createElement("td");
        outgoingCell.className = "graph-outgoing-cell mono";
        const rawOutgoing = Array.isArray(row.outgoing_relations) ? row.outgoing_relations : [];
        const textOutgoing = rawOutgoing.map(formatOutgoingRelation).filter(Boolean);
        outgoingCell.textContent = textOutgoing.length ? textOutgoing.join(", ") : "_none_";
        tr.appendChild(outgoingCell);

        const incomingCell = document.createElement("td");
        incomingCell.className = "graph-incoming-cell mono";
        incomingCell.textContent = String(incoming);
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

  function buildMessageComposeCard(session) {
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
    const isOnline = !!session.can_send_message;
    sendButton.disabled = !isOnline;
    row.appendChild(sendButton);
    content.appendChild(row);

    const status = document.createElement("div");
    status.className = "hint small";
    status.textContent = isOnline ? "Agent stream is online." : "Agent stream is offline. You can compose, but send is disabled.";
    content.appendChild(status);

    sendButton.addEventListener("click", function () {
      const contextValue = safeJsonParse(context.value, {});
      const tagValues = tags.value
        .split(",")
        .map(function (item) { return item.trim(); })
        .filter(Boolean);
      submitSessionMessage(session.session_id, {
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
    return card;
  }

  function renderSessionsFromState() {
    const sessions = Array.isArray(state.sessions) ? state.sessions : [];
    sessionsEl.innerHTML = "";
    if (sessions.length === 0) {
      sessionsEl.textContent = "No sessions available.";
      return;
    }
    for (let i = 0; i < sessions.length; i += 1) {
      const session = sessions[i];
      const logs = state.logsBySession[session.session_id] || [];
      const card = document.createElement("article");
      card.className = "session-card";

      const headline = document.createElement("button");
      headline.className = "session-headline";
      const left = document.createElement("div");
      left.className = "session-name";
      left.textContent = session.session_id;
      const right = document.createElement("div");
      right.className = "session-meta";
      const connected = document.createElement("span");
      const count = Number.isFinite(session.connected_clients) ? session.connected_clients : 0;
      connected.textContent = count + (count === 1 ? " client" : " clients");
      const cps = document.createElement("span");
      cps.textContent = fmtCallsPerSecond(session.calls_per_second, session.last_activity_seconds_ago);
      const ago = document.createElement("span");
      ago.textContent = fmtAgo(session.last_activity_iso, session.last_activity_seconds_ago);
      right.appendChild(connected);
      right.appendChild(cps);
      right.appendChild(ago);
      headline.appendChild(left);
      headline.appendChild(right);
      card.appendChild(headline);

      const body = document.createElement("div");
      body.className = "session-body";
      if (expanded.has(session.session_id)) {
        body.classList.add("open");
      }
      headline.addEventListener("click", function () {
        if (expanded.has(session.session_id)) {
          expanded.delete(session.session_id);
          body.classList.remove("open");
        } else {
          expanded.add(session.session_id);
          body.classList.add("open");
        }
      });

      body.appendChild(buildInterceptModeRow(session));
      const divider = document.createElement("hr");
      divider.className = "divider";
      body.appendChild(divider);
      const graphEl = buildGraphElement(session.session_id);
      if (graphEl) {
        body.appendChild(graphEl);
      }
      body.appendChild(buildMessageComposeCard(session));
      body.appendChild(buildLogPanel(session, logs));

      const pending = Array.isArray(session.pending_intercepts) ? session.pending_intercepts : [];
      pending.forEach((item) => {
        if (item.stage === "call") {
          body.appendChild(buildCallInterceptBlock(item));
        } else if (item.stage === "reply") {
          body.appendChild(buildReplyInterceptBlock(item));
        }
      });

      card.appendChild(body);
      sessionsEl.appendChild(card);
    }
  }

  function applySnapshot(payload) {
    const sessions = payload && Array.isArray(payload.sessions) ? payload.sessions : [];
    const logsBySession = payload && payload.logs_by_session && typeof payload.logs_by_session === "object"
      ? payload.logs_by_session
      : {};
    const graphsBySession = payload && payload.graphs_by_session && typeof payload.graphs_by_session === "object"
      ? payload.graphs_by_session
      : {};
    state.sessions = sessions;
    state.logsBySession = logsBySession;
    state.graphsBySession = graphsBySession;
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
