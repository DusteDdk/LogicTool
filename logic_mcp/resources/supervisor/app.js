(function () {
  const sessionsEl = document.getElementById("sessions");
  const expanded = new Set();
  const logsExpanded = new Set();
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

  function buildLogPanel(session, logs) {
    const panel = document.createElement("details");
    panel.className = "log-panel";
    const key = session.session_id;
    panel.open = logsExpanded.has(key);
    panel.addEventListener("toggle", function () {
      if (panel.open) {
        logsExpanded.add(key);
      } else {
        logsExpanded.delete(key);
      }
    });
    const summary = document.createElement("summary");
    summary.textContent = "Messages";
    panel.appendChild(summary);

    if (!Array.isArray(logs) || logs.length === 0) {
      const empty = document.createElement("div");
      empty.textContent = "No logs yet.";
      panel.appendChild(empty);
      return panel;
    }
    const selected = panel.open ? logs.slice(-15) : logs.slice(-2);
    selected.forEach((entry) => {
      const card = document.createElement("div");
      card.className = "log-entry-card mono";
      card.textContent = JSON.stringify(entry);
      panel.appendChild(card);
    });
    return panel;
  }

  function buildGraphElement(sessionId) {
    const svg = state.graphsBySession[sessionId];
    if (typeof svg !== "string" || !svg.trim()) {
      return null;
    }
    const svgBox = document.createElement("div");
    svgBox.className = "graph-svg-wrap";
    svgBox.innerHTML = svg;
    return svgBox;
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
        if (typeof data.session_id === "string" && typeof data.svg === "string") {
          state.graphsBySession[data.session_id] = data.svg;
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
