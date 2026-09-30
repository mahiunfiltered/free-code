const state = {
  config: null,
  fields: new Map(),
  modelOptions: [],
  modelComboboxes: new Set(),
  authPollers: new Map(),
  activeView: "providers",
  liveTimer: null,
  poolLabels: new Map(),
};

const MASKED_SECRET = "********";
const NULL_VALUE = "__FCC_NULL__";
const VIEW_GROUPS = [
  {
    id: "providers",
    label: "Providers",
    title: "Providers",
    sections: ["providers", "runtime"],
    containerId: "providersSections",
  },
  {
    id: "model_config",
    label: "Model Config",
    title: "Model Config",
    sections: ["models", "reasoning", "web_tools"],
    containerId: "modelConfigSections",
  },
  {
    id: "messaging",
    label: "Messaging",
    title: "Messaging",
    sections: ["messaging", "voice"],
    containerId: "messagingSections",
  },
  // Live views render from their own APIs; interval views poll while shown.
  { id: "endpoints", label: "Endpoints", title: "Endpoints", sections: [], refresh: refreshEndpoints, interval: 5000 },
  { id: "usage", label: "Usage", title: "Usage", sections: [], refresh: refreshUsage },
  { id: "secrets", label: "Secrets", title: "Secrets", sections: [], refresh: refreshSecrets },
  { id: "audit", label: "Audit", title: "Audit log", sections: [], refresh: refreshAudit },
  { id: "policy", label: "Policy", title: "Policy presets", sections: [], refresh: refreshPolicy },
];

const byId = (id) => document.getElementById(id);

function sourceLabel(source) {
  const labels = {
    default: "default",
    managed_env: "",
    process: "process env",
  };
  return Object.prototype.hasOwnProperty.call(labels, source) ? labels[source] : source;
}

function sourceText(field) {
  const parts = [];
  const label = sourceLabel(field.source);
  if (label) {
    parts.push(label);
  }
  if (field.locked) {
    parts.push("locked");
  }
  return parts.join(" ");
}

function statusClass(status) {
  if (["configured", "reachable", "running", "connected"].includes(status)) return "ok";
  if (["missing_key", "missing_config", "missing_url", "unknown", "connecting"].includes(status)) return "warn";
  if (["offline", "error"].includes(status)) return "error";
  return "neutral";
}

async function api(path, options = {}) {
  const response = await fetch(path, {
    headers: { "Content-Type": "application/json", ...(options.headers || {}) },
    ...options,
    cache: "no-store",
  });
  if (!response.ok) {
    let detail = "";
    try {
      const payload = await response.json();
      detail = typeof payload.detail === "string" ? payload.detail : "";
    } catch {
      // The status remains useful when an upstream proxy returns a non-JSON page.
    }
    const error = new Error(detail || `${response.status} ${response.statusText}`);
    error.status = response.status;
    throw error;
  }
  return response.json();
}

async function load() {
  showMessage("Loading admin config");
  const config = await api("/admin/api/config");
  state.config = config;
  state.fields = new Map(config.fields.map((field) => [field.key, field]));
  renderNav();
  renderProviders(config.provider_status);
  renderSections(config.sections, config.fields);
  byId("configPath").textContent = config.paths.managed;
  await refreshConnectedAccounts();
  await hydrateModelOptions();
  await refreshLocalStatus();
  await loadPoolLabels();
  await loadNvidiaModels();
  updateDirtyState();
  showMessage("");
}

function renderNav() {
  const nav = byId("sectionNav");
  nav.innerHTML = "";
  VIEW_GROUPS.forEach((view, index) => {
    const button = document.createElement("button");
    button.type = "button";
    button.className = `nav-link${index === 0 ? " active" : ""}`;
    button.dataset.view = view.id;
    button.textContent = view.label;
    if (index === 0) {
      button.setAttribute("aria-current", "page");
    }
    button.addEventListener("click", () => {
      setActiveView(view.id, { scroll: true });
    });
    nav.appendChild(button);
  });
  setActiveView(state.activeView, { scroll: false });
}

function setActiveView(viewId, { scroll = false } = {}) {
  const activeView =
    VIEW_GROUPS.find((view) => view.id === viewId) || VIEW_GROUPS[0];
  state.activeView = activeView.id;
  byId("pageTitle").textContent = activeView.title;

  document.querySelectorAll(".nav-link").forEach((link) => {
    const selected = link.dataset.view === activeView.id;
    link.classList.toggle("active", selected);
    if (selected) {
      link.setAttribute("aria-current", "page");
    } else {
      link.removeAttribute("aria-current");
    }
  });

  document.querySelectorAll(".admin-view").forEach((view) => {
    const selected = view.dataset.view === activeView.id;
    view.classList.toggle("active", selected);
    view.hidden = !selected;
  });

  if (scroll) {
    window.scrollTo({ top: 0, behavior: "smooth" });
  }
  startLiveView(activeView);
}

function renderProviders(providerStatus) {
  const grid = byId("providerGrid");
  const connectedGrid = byId("connectedAccountGrid");
  grid.innerHTML = "";
  connectedGrid.innerHTML = "";
  const connected = providerStatus.filter(
    (provider) => provider.kind === "connected_account",
  );
  byId("connectedAccountsSection").hidden = connected.length === 0;
  providerStatus.forEach((provider) => {
    if (provider.kind === "connected_account") {
      connectedGrid.appendChild(renderConnectedAccountCard(provider));
      return;
    }
    const card = document.createElement("article");
    card.className = "provider-card";
    card.dataset.provider = provider.provider_id;

    const title = document.createElement("div");
    title.className = "provider-title";
    const name = document.createElement("strong");
    name.textContent = provider.display_name || provider.provider_id;

    const pill = document.createElement("span");
    pill.className = `status-pill ${statusClass(provider.status)}`;
    pill.textContent = provider.label;
    title.append(name, pill);

    const meta = document.createElement("div");
    meta.className = "provider-meta";
    const configurationKeys = Array.isArray(provider.configuration_keys)
      ? provider.configuration_keys
      : [];
    const missingConfigurationKeys = Array.isArray(
      provider.missing_configuration_keys,
    )
      ? provider.missing_configuration_keys
      : [];
    meta.textContent = configurationKeys.join(" + ");

    const result = document.createElement("div");
    result.className = "provider-check-result";
    result.dataset.providerCheckResult = provider.provider_id;
    result.setAttribute("aria-live", "polite");
    result.hidden = true;

    const actions = document.createElement("div");
    actions.className = "provider-actions";
    if (configurationKeys.length) {
      const configuring = missingConfigurationKeys.length > 0;
      actions.appendChild(
        providerActionButton(configuring ? "Configure" : "Edit", () =>
          navigateToProviderConfiguration(provider, configuring),
        ),
      );
    }

    if (missingConfigurationKeys.length === 0) {
      const button = providerActionButton(
        provider.kind === "local" ? "Test" : "Refresh models",
        () => testProvider(provider.provider_id, button),
        "secondary-button",
      );
      actions.appendChild(button);
    }

    card.append(title, meta, result, actions);
    grid.appendChild(card);
  });
}

function providerActionButton(label, action, className = "test-button") {
  const button = document.createElement("button");
  button.type = "button";
  button.className = className;
  button.textContent = label;
  button.addEventListener("click", action);
  return button;
}

function navigateToProviderConfiguration(provider, configuring) {
  const keys = configuring
    ? provider.missing_configuration_keys
    : provider.configuration_keys;
  const fieldKey = Array.isArray(keys) ? keys[0] : null;
  const input = fieldKey ? byId(`field-${fieldKey}`) : null;
  if (!input) {
    showMessage("Provider configuration field is unavailable.", "error");
    return;
  }
  const reducedMotion = window.matchMedia(
    "(prefers-reduced-motion: reduce)",
  ).matches;
  input.scrollIntoView({
    behavior: reducedMotion ? "instant" : "smooth",
    block: "center",
  });
  input.focus({ preventScroll: true });
}

function renderConnectedAccountCard(provider, status = provider) {
  const card = document.createElement("article");
  card.className = "provider-card";
  card.dataset.provider = provider.provider_id;
  card.dataset.connectedAccount = "true";

  const title = document.createElement("div");
  title.className = "provider-title";
  const name = document.createElement("strong");
  name.textContent = provider.display_name || provider.provider_id;
  const pill = document.createElement("span");
  pill.className = `status-pill ${statusClass(status.state || status.status)}`;
  pill.textContent = connectedAccountLabel(status);
  title.append(name, pill);

  const meta = document.createElement("div");
  meta.className = "provider-meta";
  meta.textContent = connectedAccountMeta(status);

  const actions = document.createElement("div");
  actions.className = "provider-actions";
  populateConnectedAccountActions(provider, status, actions);
  card.append(title, meta, actions);
  return card;
}

function connectedAccountLabel(status) {
  const labels = {
    disconnected: "Not connected",
    connecting: "Connecting",
    connected: "Connected",
    error: "Needs attention",
  };
  return labels[status.state] || status.label || "Not connected";
}

function connectedAccountMeta(status) {
  if (status.connected) {
    const identity = status.email || "ChatGPT subscription connected";
    const models = Number.isInteger(status.model_count)
      ? `${status.model_count} model${status.model_count === 1 ? "" : "s"} available. `
      : "";
    const error = status.message ? `${status.message} ` : "";
    return `${identity}. ${models}${error}Restart your agent to refresh its model picker.`;
  }
  if (status.mode === "device" && status.user_code) {
    return `Enter code ${status.user_code} at ${status.verification_url}`;
  }
  if (status.state === "connecting") {
    return "Finish signing in, then return to this page.";
  }
  return status.message || "Connect a ChatGPT account to discover subscription models.";
}

function populateConnectedAccountActions(provider, status, actions) {
  const providerId = provider.provider_id;
  if (status.state === "connecting") {
    const target = status.authorization_url || status.verification_url;
    if (target) {
      actions.appendChild(authButton("Open sign-in", () => window.open(target, "_blank", "noopener")));
    }
    if (status.mode === "device" && status.user_code) {
      actions.appendChild(
        authButton(
          "Copy code",
          () => copyDeviceCode(status.user_code),
          "secondary-button",
        ),
      );
    }
    actions.appendChild(
      authButton("Cancel", () => cancelConnectedAccountLogin(providerId), "secondary-button"),
    );
    return;
  }
  if (status.connected) {
    actions.appendChild(
      authButton(
        "Reconnect",
        (button) => startConnectedAccountLogin(providerId, "browser", button),
      ),
    );
    actions.appendChild(
      authButton(
        "Disconnect",
        () => disconnectConnectedAccount(providerId),
        "secondary-button",
      ),
    );
    return;
  }
  actions.appendChild(
    authButton("Connect", (button) => startConnectedAccountLogin(providerId, "browser", button)),
    authButton(
      "Use device code",
      (button) => startConnectedAccountLogin(providerId, "device", button),
      "secondary-button",
    ),
  );
}

function authButton(label, action, className = "test-button") {
  const button = document.createElement("button");
  button.type = "button";
  button.className = className;
  button.textContent = label;
  button.addEventListener("click", () => action(button));
  return button;
}

async function refreshConnectedAccounts() {
  const providers = (state.config?.provider_status || []).filter(
    (provider) => provider.kind === "connected_account",
  );
  await Promise.all(
    providers.map(async (provider) => {
      try {
        const status = await api(`/admin/api/providers/${provider.provider_id}/auth`);
        updateConnectedAccountCard(provider, status);
        if (status.state === "connecting") pollConnectedAccount(provider);
      } catch (error) {
        updateConnectedAccountCard(provider, {
          state: "error",
          connected: false,
          message: error.message,
        });
      }
    }),
  );
}

function updateConnectedAccountCard(provider, status) {
  const current = document.querySelector(
    `[data-provider="${provider.provider_id}"][data-connected-account="true"]`,
  );
  if (current) current.replaceWith(renderConnectedAccountCard(provider, status));
}

async function startConnectedAccountLogin(providerId, mode, button) {
  button.disabled = true;
  const popup = window.open("about:blank", "_blank");
  if (popup) popup.opener = null;
  try {
    const status = await api(`/admin/api/providers/${providerId}/auth/login`, {
      method: "POST",
      body: JSON.stringify({ mode }),
    });
    const provider = connectedAccountDescriptor(providerId);
    updateConnectedAccountCard(provider, status);
    const target = status.authorization_url || status.verification_url;
    if (target && popup) {
      popup.location.replace(target);
    } else if (target) {
      window.open(target, "_blank", "noopener");
    } else if (popup) {
      popup.close();
    }
    pollConnectedAccount(provider);
  } catch (error) {
    if (popup) popup.close();
    showMessage(error.message, true);
    button.disabled = false;
  }
}

async function cancelConnectedAccountLogin(providerId) {
  clearConnectedAccountPoll(providerId);
  const status = await api(`/admin/api/providers/${providerId}/auth/cancel`, {
    method: "POST",
  });
  updateConnectedAccountCard(connectedAccountDescriptor(providerId), status);
}

async function disconnectConnectedAccount(providerId) {
  if (!window.confirm("Disconnect this ChatGPT account from FCC?")) return;
  clearConnectedAccountPoll(providerId);
  const status = await api(`/admin/api/providers/${providerId}/auth`, {
    method: "DELETE",
  });
  updateConnectedAccountCard(connectedAccountDescriptor(providerId), status);
  await hydrateModelOptions();
}

function pollConnectedAccount(provider) {
  clearConnectedAccountPoll(provider.provider_id);
  const poll = async () => {
    try {
      const status = await api(`/admin/api/providers/${provider.provider_id}/auth`);
      updateConnectedAccountCard(provider, status);
      if (status.state === "connecting") {
        state.authPollers.set(provider.provider_id, window.setTimeout(poll, 1000));
      } else {
        state.authPollers.delete(provider.provider_id);
        if (status.connected) await hydrateModelOptions();
      }
    } catch (error) {
      state.authPollers.delete(provider.provider_id);
      showMessage(error.message, true);
    }
  };
  state.authPollers.set(provider.provider_id, window.setTimeout(poll, 1000));
}

function clearConnectedAccountPoll(providerId) {
  const timer = state.authPollers.get(providerId);
  if (timer) window.clearTimeout(timer);
  state.authPollers.delete(providerId);
}

function connectedAccountDescriptor(providerId) {
  return state.config.provider_status.find(
    (provider) => provider.provider_id === providerId,
  );
}

async function copyDeviceCode(code) {
  try {
    await navigator.clipboard.writeText(code);
    showMessage("Device code copied.");
  } catch {
    showMessage(`Copy this device code: ${code}`);
  }
}

function updateProviderCheckResult(providerId, status, message) {
  const card = document.querySelector(`[data-provider="${providerId}"]`);
  if (!card) return;
  const result = card.querySelector(".provider-check-result");
  result.className = `provider-check-result ${status}`;
  result.textContent = message;
  result.hidden = !message;
}

function renderSections(sections, fields) {
  state.modelComboboxes.clear();
  VIEW_GROUPS.forEach((view) => {
    if (view.containerId) byId(view.containerId).innerHTML = "";
  });

  const sectionById = new Map(sections.map((section) => [section.id, section]));
  const bySection = new Map();
  sections.forEach((section) => bySection.set(section.id, []));
  fields.forEach((field) => {
    if (!bySection.has(field.section)) bySection.set(field.section, []);
    bySection.get(field.section).push(field);
  });

  VIEW_GROUPS.forEach((view) => {
    if (!view.containerId) return;
    const container = byId(view.containerId);
    view.sections.forEach((sectionId) => {
      const section = sectionById.get(sectionId);
      const sectionFields = bySection.get(sectionId) || [];
      if (!section || sectionFields.length === 0) return;

      const sectionEl = document.createElement("section");
      sectionEl.className = "settings-section";
      sectionEl.id = `section-${section.id}`;

      const heading = document.createElement("div");
      heading.className = "section-heading";
      heading.innerHTML = `<div><h3>${section.label}</h3><p>${section.description}</p></div>`;
      if (section.id === "models") {
        const refreshButton = document.createElement("button");
        refreshButton.type = "button";
        refreshButton.className = "secondary-button";
        refreshButton.textContent = "Refresh models";
        refreshButton.addEventListener("click", () => refreshModelOptions(refreshButton));
        heading.appendChild(refreshButton);
      }
      sectionEl.appendChild(heading);

      const grid = document.createElement("div");
      grid.className = "field-grid";
      sectionFields.forEach((field) => {
        grid.appendChild(renderField(field));
      });
      sectionEl.appendChild(grid);

      if (sectionFields.some((field) => field.advanced)) {
        const toggle = document.createElement("button");
        toggle.type = "button";
        toggle.className = "ghost-button advanced-toggle";
        toggle.textContent = "Show advanced";
        toggle.addEventListener("click", () => {
          const showing = sectionEl.classList.toggle("show-advanced");
          toggle.textContent = showing ? "Hide advanced" : "Show advanced";
        });
        sectionEl.appendChild(toggle);
      }

      container.appendChild(sectionEl);
    });
  });
}

function renderField(field) {
  const wrapper = document.createElement("div");
  wrapper.className = `field${field.advanced ? " advanced-field" : ""}`;
  wrapper.dataset.key = field.key;

  const label = document.createElement("label");
  label.htmlFor = `field-${field.key}`;
  const labelText = document.createElement("span");
  labelText.textContent = field.label;
  label.appendChild(labelText);

  const source = sourceText(field);
  if (source) {
    const sourceEl = document.createElement("span");
    sourceEl.className = "field-source";
    sourceEl.textContent = source;
    label.appendChild(sourceEl);
  }

  const input = inputForField(field);
  input.id = `field-${field.key}`;
  input.dataset.key = field.key;
  input.dataset.original = comparableValue(field.value);
  input.dataset.secret = field.secret ? "true" : "false";
  input.dataset.configured = field.configured ? "true" : "false";
  input.dataset.nullable = field.nullable ? "true" : "false";
  input.dataset.remove = "false";
  input.dataset.fieldType = field.type;
  input.disabled = field.locked;
  input.addEventListener("input", updateDirtyState);
  input.addEventListener("change", updateDirtyState);
  input.addEventListener("input", () => {
    input.dataset.remove = "false";
  });
  if (field.type === "optional_model") {
    input.addEventListener("blur", () => {
      if (!input.value.trim() || input.value.trim().toLowerCase() === "none") {
        input.value = "None";
        updateDirtyState();
      }
    });
  }

  let control = input;
  if (field.type === "model" || field.type === "optional_model") {
    control = new ModelCombobox(input, field).element;
  } else if (field.type === "model_list") {
    const editor = new ModelListEditor(input, field);
    label.htmlFor = editor.inputId;
    control = editor.element;
  } else if (isKeyPoolField(field)) {
    const editor = new KeyPoolEditor(input, field);
    wrapper.classList.add("key-pool-field");
    control = editor.element;
  }
  wrapper.append(label, control);
  if (field.secret && field.nullable && field.configured && !field.locked) {
    const removeButton = document.createElement("button");
    removeButton.type = "button";
    removeButton.className = "ghost-button secret-remove";
    removeButton.textContent = "Remove";
    removeButton.addEventListener("click", () => {
      const removing = input.dataset.remove !== "true";
      input.dataset.remove = removing ? "true" : "false";
      input.readOnly = removing;
      removeButton.textContent = removing ? "Undo removal" : "Remove";
      updateDirtyState();
    });
    wrapper.appendChild(removeButton);
  }
  if (field.description) {
    const description = document.createElement("div");
    description.className = "field-description";
    description.textContent = field.description;
    wrapper.appendChild(description);
  }
  return wrapper;
}

function inputForField(field) {
  if (field.type === "boolean") {
    const input = document.createElement("input");
    input.type = "checkbox";
    input.checked = String(field.value).toLowerCase() === "true";
    input.dataset.original = input.checked ? "true" : "false";
    return input;
  }

  if (field.type === "select") {
    const select = document.createElement("select");
    field.options.forEach((item) =>
      select.appendChild(option(item.value, item.label)),
    );
    select.value = field.value || field.options[0]?.value || "";
    return select;
  }

  if (field.type === "textarea") {
    const textarea = document.createElement("textarea");
    textarea.value = field.value || "";
    return textarea;
  }

  if (field.type === "model" || field.type === "optional_model") {
    const input = document.createElement("input");
    input.type = "text";
    input.value = field.value || (field.type === "optional_model" ? "None" : "");
    input.autocomplete = "off";
    return input;
  }

  if (field.type === "model_list" || isKeyPoolField(field)) {
    const input = document.createElement("input");
    input.type = "hidden";
    input.value = field.type === "model_list" ? field.value || "" : "";
    return input;
  }

  const input = document.createElement("input");
  input.type = field.type === "number" ? "number" : "text";
  if (field.type === "secret") {
    input.type = "password";
    input.placeholder = field.configured
      ? "Configured - enter a new value to replace"
      : "Not configured";
    input.value = "";
    input.autocomplete = "off";
  } else {
    input.value = field.value || "";
  }
  return input;
}

class ModelCombobox {
  constructor(input, field) {
    this.input = input;
    this.fieldType = field.type;
    this.activeIndex = -1;
    this.query = "";

    this.element = document.createElement("div");
    this.element.className = "model-combobox";
    this.listbox = document.createElement("div");
    this.listbox.className = "model-combobox-list";
    this.listbox.id = `model-options-${field.key}`;
    this.listbox.setAttribute("role", "listbox");
    this.listbox.hidden = true;
    this.toggle = document.createElement("button");
    this.toggle.type = "button";
    this.toggle.className = "model-combobox-toggle";
    this.toggle.disabled = input.disabled;
    this.toggle.setAttribute("aria-label", `Show ${field.label} options`);

    input.setAttribute("role", "combobox");
    input.setAttribute("aria-autocomplete", "list");
    input.setAttribute("aria-haspopup", "listbox");
    for (const control of [input, this.toggle]) {
      control.setAttribute("aria-controls", this.listbox.id);
      control.setAttribute("aria-expanded", "false");
    }

    input.addEventListener("click", () => this.open());
    input.addEventListener("input", () => this.open(input.value));
    input.addEventListener("keydown", (event) => this.handleKeydown(event));
    this.toggle.addEventListener("mousedown", (event) => event.preventDefault());
    this.toggle.addEventListener("click", () => {
      if (this.isOpen) this.close();
      else this.open();
      input.focus();
    });
    this.listbox.addEventListener("mousedown", (event) => event.preventDefault());
    this.listbox.addEventListener("mousemove", (event) => {
      const optionEl = event.target.closest('[role="option"]');
      if (optionEl) this.setActive(this.visibleOptions.indexOf(optionEl));
    });
    this.listbox.addEventListener("click", (event) => {
      const optionEl = event.target.closest('[role="option"]');
      if (optionEl) this.select(optionEl.dataset.value);
    });

    this.element.append(input, this.toggle, this.listbox);
    state.modelComboboxes.add(this);
  }

  get isOpen() {
    return this.element.classList.contains("open");
  }

  get values() {
    return this.fieldType === "optional_model"
      ? ["None", ...state.modelOptions]
      : state.modelOptions;
  }

  get visibleOptions() {
    return Array.from(this.listbox.querySelectorAll('[role="option"]'));
  }

  open(query = "") {
    if (this.input.disabled) return;
    state.modelComboboxes.forEach((combobox) => {
      if (combobox !== this) combobox.close();
    });
    this.render(query);
    this.element.classList.add("open");
    this.listbox.hidden = false;
    this.setExpanded(true);
  }

  close() {
    this.element.classList.remove("open");
    this.listbox.hidden = true;
    this.activeIndex = -1;
    this.input.removeAttribute("aria-activedescendant");
    this.setExpanded(false);
  }

  setExpanded(expanded) {
    for (const control of [this.input, this.toggle]) {
      control.setAttribute("aria-expanded", String(expanded));
    }
  }

  render(query) {
    this.query = query;
    const normalizedQuery = query.trim().toLocaleLowerCase();
    const values = normalizedQuery
      ? this.values.filter((value) =>
          value.toLocaleLowerCase().includes(normalizedQuery),
        )
      : this.values;
    this.listbox.innerHTML = "";

    if (values.length === 0) {
      const empty = document.createElement("div");
      empty.className = "model-combobox-empty";
      empty.textContent = state.modelOptions.length
        ? "No matching models. You can still enter a custom slug."
        : "No discovered models. Refresh models or enter a custom slug.";
      this.listbox.appendChild(empty);
      this.activeIndex = -1;
      this.input.removeAttribute("aria-activedescendant");
      return;
    }

    values.forEach((value, index) => {
      const optionEl = document.createElement("div");
      optionEl.className = "model-combobox-option";
      optionEl.id = `${this.listbox.id}-option-${index}`;
      optionEl.dataset.value = value;
      optionEl.setAttribute("role", "option");
      optionEl.textContent = value;
      this.listbox.appendChild(optionEl);
    });
    const selectedIndex = values.indexOf(this.input.value);
    this.setActive(selectedIndex >= 0 ? selectedIndex : 0, false);
  }

  setActive(index, scroll = true) {
    const options = this.visibleOptions;
    if (options.length === 0) return;
    this.activeIndex = Math.max(0, Math.min(index, options.length - 1));
    options.forEach((optionEl, optionIndex) => {
      const active = optionIndex === this.activeIndex;
      optionEl.classList.toggle("active", active);
      optionEl.setAttribute("aria-selected", String(active));
    });
    const activeOption = options[this.activeIndex];
    this.input.setAttribute("aria-activedescendant", activeOption.id);
    if (scroll) activeOption.scrollIntoView({ block: "nearest" });
  }

  move(offset) {
    const count = this.visibleOptions.length;
    if (count) this.setActive((this.activeIndex + offset + count) % count);
  }

  select(value) {
    this.input.value = value;
    this.input.dispatchEvent(new Event("change", { bubbles: true }));
    this.close();
    this.input.focus();
  }

  handleKeydown(event) {
    if (event.key === "ArrowDown" || event.key === "ArrowUp") {
      event.preventDefault();
      if (this.isOpen) {
        this.move(event.key === "ArrowDown" ? 1 : -1);
      } else {
        this.open();
        if (event.key === "ArrowUp") {
          this.setActive(this.visibleOptions.length - 1);
        }
      }
    } else if (this.isOpen && (event.key === "Home" || event.key === "End")) {
      event.preventDefault();
      this.setActive(event.key === "Home" ? 0 : this.visibleOptions.length - 1);
    } else if (this.isOpen && event.key === "Enter") {
      const active = this.visibleOptions[this.activeIndex];
      if (active) {
        event.preventDefault();
        this.select(active.dataset.value);
      }
    } else if (this.isOpen && event.key === "Escape") {
      event.preventDefault();
      this.close();
    } else if (this.isOpen && event.key === "Tab") {
      this.close();
    }
  }
}

class ModelListEditor {
  constructor(input, field) {
    this.input = input;
    this.field = field;
    this.values = input.value
      ? input.value.split(",").map((value) => value.trim()).filter(Boolean)
      : [];
    this.inputId = `field-${field.key}-add`;

    this.element = document.createElement("div");
    this.element.className = "model-list-editor";

    const addRow = document.createElement("div");
    addRow.className = "model-list-add";
    this.addInput = document.createElement("input");
    this.addInput.id = this.inputId;
    this.addInput.type = "text";
    this.addInput.autocomplete = "off";
    this.addInput.placeholder = "provider/model";
    this.addInput.disabled = field.locked;
    const addCombobox = new ModelCombobox(this.addInput, {
      ...field,
      key: `${field.key}-add`,
      label: "fallback model",
      type: "model",
    });

    this.addButton = document.createElement("button");
    this.addButton.type = "button";
    this.addButton.className = "secondary-button";
    this.addButton.textContent = "Add";
    this.addButton.disabled = field.locked;
    this.addButton.addEventListener("click", () => this.add());
    addRow.append(addCombobox.element, this.addButton);

    this.rows = document.createElement("div");
    this.rows.className = "model-list-rows";
    this.element.append(input, addRow, this.rows);
    this.renderRows();
  }

  add() {
    const value = this.addInput.value.trim();
    if (!value) {
      showMessage("Enter a full provider/model fallback.", "error");
      return;
    }
    if (this.values.includes(value)) {
      showMessage("That fallback model is already in the list.", "error");
      return;
    }
    this.values.push(value);
    this.addInput.value = "";
    showMessage("");
    this.sync();
  }

  move(index, offset) {
    const destination = index + offset;
    if (destination < 0 || destination >= this.values.length) return;
    [this.values[index], this.values[destination]] = [
      this.values[destination],
      this.values[index],
    ];
    this.sync();
  }

  remove(index) {
    this.values.splice(index, 1);
    this.sync();
  }

  sync() {
    this.input.value = this.values.join(",");
    this.input.dataset.remove = "false";
    this.input.dispatchEvent(new Event("input", { bubbles: true }));
    this.renderRows();
  }

  renderRows() {
    this.rows.innerHTML = "";
    if (this.values.length === 0) {
      const empty = document.createElement("div");
      empty.className = "model-list-empty";
      empty.textContent = "No fallback models configured.";
      this.rows.appendChild(empty);
      return;
    }

    this.values.forEach((value, index) => {
      const row = document.createElement("div");
      row.className = "model-list-row";

      const model = document.createElement("span");
      model.className = "model-list-value";
      model.textContent = value;

      const up = this.actionButton("Move up", `Move ${value} up`, () =>
        this.move(index, -1),
      );
      up.disabled = this.field.locked || index === 0;
      const down = this.actionButton("Move down", `Move ${value} down`, () =>
        this.move(index, 1),
      );
      down.disabled = this.field.locked || index === this.values.length - 1;
      const remove = this.actionButton("Remove", `Remove ${value}`, () =>
        this.remove(index),
      );
      remove.disabled = this.field.locked;

      row.append(model, up, down, remove);
      this.rows.appendChild(row);
    });
  }

  actionButton(text, label, action) {
    const button = document.createElement("button");
    button.type = "button";
    button.className = "ghost-button model-list-action";
    button.textContent = text;
    button.setAttribute("aria-label", label);
    button.addEventListener("click", action);
    return button;
  }
}

function option(value, label) {
  const optionEl = document.createElement("option");
  optionEl.value = value;
  optionEl.textContent = label;
  return optionEl;
}

function readFieldValue(input) {
  if (input.type === "checkbox") return input.checked ? "true" : "false";
  if (input.dataset.remove === "true") return null;
  if (
    input.dataset.fieldType === "optional_model" &&
    input.value.trim().toLowerCase() === "none"
  ) {
    return null;
  }
  if (input.dataset.secret === "true" && input.dataset.configured === "true") {
    return input.value ? input.value : MASKED_SECRET;
  }
  if (input.dataset.nullable === "true" && !input.value.trim()) return null;
  return input.value;
}

function comparableValue(value) {
  return value === null ? NULL_VALUE : String(value);
}

function changedValues() {
  const values = {};
  document.querySelectorAll("[data-key]").forEach((input) => {
    if (input.disabled || !input.matches("input, select, textarea")) return;
    const value = readFieldValue(input);
    if (comparableValue(value) !== input.dataset.original) {
      values[input.dataset.key] = value;
    }
  });
  return values;
}

function updateDirtyState() {
  const count = Object.keys(changedValues()).length;
  byId("dirtyState").textContent =
    count === 0 ? "No changes" : `${count} unsaved change${count === 1 ? "" : "s"}`;
  byId("applyButton").disabled = count === 0;
}

async function apply() {
  const result = await api("/admin/api/config/apply", {
    method: "POST",
    body: JSON.stringify({ values: changedValues() }),
  });
  if (!result.applied) {
    showMessage(result.errors.join("; "), "error");
    return;
  }
  const restart = result.restart || {};
  if (restart.required && restart.automatic) {
    showMessage("Applied. Restarting server...", "ok");
    byId("applyButton").disabled = true;
    setTimeout(() => {
      window.location.href = restart.admin_url || "/admin";
    }, 1600);
    return;
  }
  const pending = restart.required ? restart.fields || [] : result.pending_fields || [];
  await load();
  showMessage(
    pending.length
      ? `Applied. Restart fcc-server to use: ${pending.join(", ")}`
      : "Applied",
    "ok",
  );
}

async function refreshLocalStatus() {
  const result = await api("/admin/api/providers/local-status");
  result.providers.forEach((provider) => {
    if (provider.status === "missing_url") return;
    if (provider.status === "reachable") {
      updateProviderCheckResult(
        provider.provider_id,
        "ok",
        `Reachable: ${provider.base_url}`,
      );
      return;
    }
    const detail = provider.message
      ? provider.message
      : provider.status_code
        ? `${provider.base_url} returned HTTP ${provider.status_code}`
        : "The local provider did not respond.";
    updateProviderCheckResult(
      provider.provider_id,
      "error",
      `Unavailable: ${detail}`,
    );
  });
}

async function testProvider(providerId, button) {
  const original = button.textContent;
  button.disabled = true;
  button.textContent = "Checking...";
  updateProviderCheckResult(providerId, "checking", "Checking...");
  try {
    const result = await api(`/admin/api/providers/${providerId}/test`, {
      method: "POST",
      body: "{}",
    });
    if (result.ok) {
      updateProviderCheckResult(
        providerId,
        "ok",
        `${result.models.length} models available`,
      );
      setModelOptions([
        ...state.modelOptions,
        ...result.models.map((model) => `${providerId}/${model}`),
      ]);
    } else {
      updateProviderCheckResult(
        providerId,
        "error",
        `Unavailable: ${result.message || "Provider check failed."}`,
      );
    }
  } catch {
    updateProviderCheckResult(
      providerId,
      "error",
      "Provider check could not be completed.",
    );
  } finally {
    button.disabled = false;
    button.textContent = original;
  }
}

async function hydrateModelOptions() {
  try {
    await loadModelOptions();
  } catch {
    // Model fields remain editable when optional catalog hydration is unavailable.
  }
}

async function loadModelOptions(refresh = false) {
  const result = await api("/admin/api/models" + (refresh ? "/refresh" : ""), {
    method: refresh ? "POST" : "GET",
  });
  setModelOptions(result.models);
  return result;
}

async function refreshModelOptions(button) {
  const original = button.textContent;
  button.disabled = true;
  button.textContent = "Refreshing";
  try {
    const result = await loadModelOptions(true);
    const failedProviders = result.failed_providers || [];
    if (failedProviders.length) {
      const labels = failedProviders.map(providerDisplayName).join(", ");
      showMessage(
        `${state.modelOptions.length} models available; could not refresh ${labels}`,
        "warn",
      );
    } else {
      showMessage(`${state.modelOptions.length} models available`, "ok");
    }
  } catch (error) {
    showMessage(`Could not refresh models: ${error.message}`, "error");
  } finally {
    button.disabled = false;
    button.textContent = original;
  }
}

function providerDisplayName(providerId) {
  const provider = state.config?.provider_status?.find(
    (candidate) => candidate.provider_id === providerId,
  );
  return provider?.display_name || providerId;
}

function setModelOptions(models) {
  state.modelOptions = Array.from(
    new Set(models.filter((model) => typeof model === "string" && model.trim())),
  ).sort((left, right) => left.localeCompare(right));
  state.modelComboboxes.forEach((combobox) => {
    if (combobox.isOpen) combobox.render(combobox.query);
  });
}

function showMessage(message, kind = "") {
  const area = byId("messageArea");
  area.textContent = message;
  area.className = `message-area ${kind}`.trim();
}

// ---- Key pool editor (plural *_API_KEYS secret fields) ----

const POOL_LABEL = /^[A-Za-z0-9_.-]{1,40}$/;
const MASKED_KEY = "••••";

function isKeyPoolField(field) {
  return (
    field.type === "secret" &&
    field.key.endsWith("S") &&
    state.fields.get(field.key.slice(0, -1))?.type === "secret"
  );
}

function poolProviderId(fieldKey) {
  const single = fieldKey.slice(0, -1);
  const provider = (state.config?.provider_status || []).find((candidate) =>
    (candidate.configuration_keys || []).includes(single),
  );
  return provider?.provider_id || null;
}

async function loadPoolLabels() {
  // ponytail: labels come from live endpoint health; the config API never returns key material.
  try {
    const result = await api("/admin/api/endpoints");
    state.poolLabels = new Map();
    (result.endpoints || []).forEach((endpoint) => {
      if (!endpoint.configured || endpoint.label === "primary") return;
      const labels = state.poolLabels.get(endpoint.provider_id) || [];
      labels.push(endpoint.label);
      state.poolLabels.set(endpoint.provider_id, labels);
    });
  } catch {
    state.poolLabels = new Map();
  }
  document.querySelectorAll(".key-pool-editor").forEach((element) => {
    element.keyPoolEditor?.renderExisting();
  });
}

class KeyPoolEditor {
  constructor(input, field) {
    this.input = input;
    this.field = field;
    this.providerId = poolProviderId(field.key);

    this.element = el("div", "key-pool-editor");
    this.element.keyPoolEditor = this;
    this.element.setAttribute("role", "group");
    this.element.setAttribute("aria-label", field.label);
    this.existing = el("div", "key-pool-existing");
    this.rows = el("div", "key-pool-rows");
    this.addButton = el("button", "secondary-button", "Add key");
    this.addButton.type = "button";
    this.addButton.id = `field-${field.key}-add`;
    this.addButton.disabled = field.locked;
    this.addButton.addEventListener("click", () => this.addRow());
    this.element.append(input, this.existing, this.rows, this.addButton);
    this.renderExisting();
  }

  renderExisting() {
    this.existing.replaceChildren();
    if (!this.field.configured) return;
    const labels = state.poolLabels.get(this.providerId) || [];
    const chips = el("div", "key-pool-chips");
    (labels.length ? labels : ["configured"]).forEach((label) => {
      chips.appendChild(el("span", "key-chip", `${label} ${MASKED_KEY}`));
    });
    const note = el(
      "p",
      "key-pool-note",
      "Pool configured. Keys added below replace the whole pool; leave empty to keep it.",
    );
    this.existing.append(chips, note);
  }

  addRow() {
    const row = el("div", "key-pool-row");
    const index = this.rows.children.length + 1;
    const label = el("input");
    label.type = "text";
    label.placeholder = `key${index}`;
    label.autocomplete = "off";
    label.setAttribute("aria-label", `Key ${index} label`);
    const key = el("input");
    key.type = "password";
    key.placeholder = "API key";
    key.autocomplete = "new-password";
    key.setAttribute("aria-label", `Key ${index} value`);
    const remove = el("button", "ghost-button model-list-action", "Remove");
    remove.type = "button";
    remove.setAttribute("aria-label", `Remove key ${index}`);
    remove.addEventListener("click", () => {
      row.remove();
      this.sync();
    });
    label.addEventListener("input", () => this.sync());
    key.addEventListener("input", () => this.sync());
    row.append(label, key, remove);
    this.rows.appendChild(row);
    label.focus();
  }

  sync() {
    const entries = [];
    this.rows.querySelectorAll(".key-pool-row").forEach((row) => {
      const [labelInput, keyInput] = row.querySelectorAll("input");
      const label = labelInput.value.trim();
      const key = keyInput.value.trim();
      const labelOk = !label || POOL_LABEL.test(label);
      const keyOk = !/[,\s]/.test(key);
      labelInput.setAttribute("aria-invalid", String(!labelOk));
      keyInput.setAttribute("aria-invalid", String(!keyOk));
      if (!key || !labelOk || !keyOk) return;
      entries.push(label ? `${label}=${key}` : key);
    });
    this.input.value = entries.join(",");
    this.input.dataset.remove = "false";
    this.input.dispatchEvent(new Event("input", { bubbles: true }));
  }
}

// ---- Shared live-view helpers ----

function el(tag, className = "", text = "") {
  const node = document.createElement(tag);
  if (className) node.className = className;
  if (text !== "") node.textContent = text;
  return node;
}

function pill(text, kind = "") {
  return el("span", `status-pill ${kind}`.trim(), text);
}

function dataTable(headers, rows) {
  const wrap = el("div", "table-wrap");
  const table = el("table", "data-table");
  const head = el("tr");
  headers.forEach((header) => head.appendChild(el("th", "", header)));
  table.appendChild(el("thead")).appendChild(head);
  const body = table.appendChild(el("tbody"));
  rows.forEach((cells) => {
    const tr = body.appendChild(el("tr"));
    cells.forEach((cell, index) => {
      const td = el("td");
      td.dataset.label = headers[index];
      if (cell instanceof Node) td.appendChild(cell);
      else td.textContent = cell ?? "—";
      tr.appendChild(td);
    });
  });
  wrap.appendChild(table);
  return wrap;
}

function panel(title, description = "") {
  const section = el("section", "settings-section");
  const heading = el("div", "section-heading");
  const text = el("div");
  text.appendChild(el("h3", "", title));
  if (description) text.appendChild(el("p", "", description));
  heading.appendChild(text);
  section.appendChild(heading);
  return section;
}

function emptyState(message) {
  return el("div", "empty-state", message);
}

function unavailable(container, error, feature) {
  const missing = error.status === 404 || error.status === 503;
  container.replaceChildren(
    emptyState(
      missing
        ? `${feature} is not available on this server yet. Restart fcc-server after updating to enable it.`
        : `${feature} could not be loaded: ${error.message}`,
    ),
  );
}

function formatMs(value) {
  return value === null || value === undefined ? "—" : `${Math.round(value)} ms`;
}

function formatCount(value) {
  return Number(value || 0).toLocaleString();
}

function formatTime(value) {
  if (value === null || value === undefined || value === "") return "—";
  const date = new Date(typeof value === "number" ? value * 1000 : value);
  return Number.isNaN(date.getTime()) ? String(value) : date.toLocaleString();
}

function formatDuration(seconds) {
  const total = Math.max(0, Math.ceil(seconds));
  if (total >= 3600) return `${Math.floor(total / 3600)}h ${Math.floor((total % 3600) / 60)}m`;
  if (total >= 60) return `${Math.floor(total / 60)}m ${total % 60}s`;
  return `${total}s`;
}

function startLiveView(view) {
  window.clearInterval(state.liveTimer);
  state.liveTimer = null;
  if (!view.refresh) return;
  view.refresh();
  if (view.interval) {
    state.liveTimer = window.setInterval(() => {
      if (!document.hidden) view.refresh();
    }, view.interval);
  }
}

function tickCountdowns() {
  document.querySelectorAll("[data-deadline]").forEach((node) => {
    const remaining = (Number(node.dataset.deadline) - Date.now()) / 1000;
    node.textContent = remaining > 0 ? formatDuration(remaining) : "ready";
  });
}

// ---- Endpoints view ----

const CIRCUIT_CLASS = { HEALTHY: "ok", DEGRADED: "warn", OPEN: "error", HALF_OPEN: "info" };

async function refreshEndpoints() {
  const body = byId("endpointsBody");
  let result;
  try {
    result = await api("/admin/api/endpoints");
  } catch (error) {
    unavailable(body, error, "Endpoint health");
    return;
  }
  const endpoints = result.endpoints || [];
  byId("endpointsUpdated").textContent = `Updated ${new Date().toLocaleTimeString()}`;
  if (!endpoints.length) {
    body.replaceChildren(
      emptyState("No provider keys are registered yet. Configure a key-based provider to see its health."),
    );
    return;
  }
  const byProvider = new Map();
  endpoints.forEach((endpoint) => {
    if (!byProvider.has(endpoint.provider_id)) byProvider.set(endpoint.provider_id, []);
    byProvider.get(endpoint.provider_id).push(endpoint);
  });
  const sections = [];
  byProvider.forEach((items, providerId) => {
    const healthy = items.filter((item) => item.circuit === "HEALTHY").length;
    const section = panel(providerDisplayName(providerId), `${healthy} of ${items.length} keys healthy`);
    section.dataset.endpointProvider = providerId;
    section.appendChild(
      dataTable(
        ["Key", "State", "Cooldown", "p50 / p95", "OK / Failed", "Last error", "Action"],
        items.map(endpointRow),
      ),
    );
    sections.push(section);
  });
  body.replaceChildren(...sections);
}

function endpointRow(item) {
  const key = el("span", "mono", item.label);
  if (item.configured === false) key.appendChild(el("span", "muted-tag", " not configured"));

  const statePill = pill(item.circuit.replace("_", " "), CIRCUIT_CLASS[item.circuit] || "");
  statePill.dataset.circuit = item.circuit;

  let cooldown = "—";
  if (item.cooldown_remaining_s > 0) {
    cooldown = el("span", "cooldown");
    const timer = el("span", "mono", formatDuration(item.cooldown_remaining_s));
    timer.dataset.deadline = String(Date.now() + item.cooldown_remaining_s * 1000);
    cooldown.append(timer);
    if (item.cooldown_reason) cooldown.append(el("span", "muted-tag", ` ${item.cooldown_reason}`));
  } else if (item.manual_reset_required) {
    cooldown = el("span", "muted-tag", "until reset");
  }

  const lastError = el("span", "last-error", item.last_error || "—");
  const recent = item.recent_errors || [];
  if (recent.length) {
    lastError.title = recent.map((entry) => `${formatTime(entry.at)} ${entry.signal}`).join("\n");
  }

  const reset = el("button", "secondary-button compact-button", "Reset");
  reset.type = "button";
  reset.disabled = !(item.circuit === "OPEN" || item.manual_reset_required);
  reset.setAttribute("aria-label", `Reset ${item.provider_id} ${item.label}`);
  reset.addEventListener("click", () => resetEndpoint(item, reset));

  return [
    key,
    statePill,
    cooldown,
    `${formatMs(item.latency_p50_ms)} / ${formatMs(item.latency_p95_ms)}`,
    `${formatCount(item.success_count)} / ${formatCount(item.failure_count)}`,
    lastError,
    reset,
  ];
}

async function resetEndpoint(item, button) {
  button.disabled = true;
  try {
    await api(
      `/admin/api/endpoints/${encodeURIComponent(item.provider_id)}/${encodeURIComponent(item.label)}/reset`,
      { method: "POST", body: "{}" },
    );
    showMessage(`Reset ${item.provider_id} ${item.label}`, "ok");
  } catch (error) {
    showMessage(`Could not reset ${item.label}: ${error.message}`, "error");
  }
  await refreshEndpoints();
}

// ---- Usage view ----

function outcomeClass(outcome) {
  if (outcome === "ok") return "ok";
  if (outcome === "rate_limited" || outcome === "quota_exhausted") return "warn";
  if (outcome === "cancelled") return "";
  return "error";
}

async function refreshUsage() {
  const body = byId("usageBody");
  const minutes = byId("usageWindow").value;
  let result;
  try {
    result = await api(`/admin/api/usage?minutes=${encodeURIComponent(minutes)}`);
  } catch (error) {
    unavailable(body, error, "Usage");
    return;
  }
  const endpoints = result.endpoints || [];
  const sessions = result.sessions || [];
  const recent = result.recent || [];
  const maxRequests = Math.max(1, ...endpoints.map((item) => item.requests || 0));

  const endpointPanel = panel("Endpoints", "Attempts per provider key in the selected window.");
  endpointPanel.appendChild(
    endpoints.length
      ? dataTable(
          ["Key", "Requests", "OK", "Errors", "Rate limits", "p50 / p95", "Tokens in / out"],
          endpoints.map((item) => {
            const requests = el("span", "spark");
            const bar = el("span", "spark-bar");
            bar.style.width = `${Math.max(2, (100 * (item.requests || 0)) / maxRequests)}%`;
            const track = el("span", "spark-track");
            track.appendChild(bar);
            requests.append(track, el("span", "spark-value", formatCount(item.requests)));
            return [
              el("span", "mono", `${providerDisplayName(item.provider_id)} / ${item.label}`),
              requests,
              formatCount(item.ok),
              formatCount(item.errors),
              formatCount(item.rate_limits),
              `${formatMs(item.latency_p50_ms)} / ${formatMs(item.latency_p95_ms)}`,
              `${formatCount(item.input_tokens)} / ${formatCount(item.output_tokens)}`,
            ];
          }),
        )
      : emptyState("No attempts in this window."),
  );

  const sessionPanel = panel("Sessions", "Totals per Claude Code session.");
  sessionPanel.appendChild(
    sessions.length
      ? dataTable(
          ["Session", "Attempts", "OK", "Tokens in / out", "Last activity"],
          sessions.map((item) => {
            const id = el("span", "mono", String(item.claude_session_id).slice(0, 8));
            id.title = item.claude_session_id;
            return [
              id,
              formatCount(item.attempts),
              formatCount(item.ok),
              `${formatCount(item.input_tokens)} / ${formatCount(item.output_tokens)}`,
              formatTime(item.last_ts),
            ];
          }),
        )
      : emptyState("No session activity in this window."),
  );

  const recentPanel = panel("Recent attempts", "Latest attempts, newest first.");
  recentPanel.appendChild(
    recent.length
      ? dataTable(
          ["Time", "Key", "Model", "Outcome", "Latency", "Tokens in / out"],
          recent.map((item) => {
            const key = el("span", "mono", `${item.provider_id} / ${item.key_label}`);
            if (item.failover_from) {
              key.appendChild(el("span", "failover", `↪ from ${item.failover_from}`));
            }
            return [
              formatTime(item.ts),
              key,
              el("span", "mono", item.model || "—"),
              pill(item.outcome, outcomeClass(item.outcome)),
              formatMs(item.latency_ms),
              `${formatCount(item.input_tokens)} / ${formatCount(item.output_tokens)}`,
            ];
          }),
        )
      : emptyState("No attempts recorded yet."),
  );
  body.replaceChildren(endpointPanel, sessionPanel, recentPanel);
}

// ---- Secrets view ----

function setSecretsEnabled(enabled) {
  ["secretName", "secretValue", "secretSave", "secretMigrate"].forEach((id) => {
    byId(id).disabled = !enabled;
  });
}

async function refreshSecrets() {
  const status = byId("vaultStatus");
  const errorBox = byId("vaultError");
  const list = byId("secretList");
  let result;
  try {
    result = await api("/admin/api/secrets");
  } catch (error) {
    status.className = "status-pill warn";
    status.textContent = "Unavailable";
    errorBox.hidden = true;
    setSecretsEnabled(false);
    unavailable(list, error, "The credential vault");
    return;
  }
  const available = Boolean(result.available);
  status.className = `status-pill ${available ? "ok" : "error"}`;
  status.textContent = available ? "Available" : "Unavailable";
  const errorText = result.error || (available ? "" : "The vault is not available on this machine.");
  errorBox.textContent = errorText;
  errorBox.hidden = !errorText;
  setSecretsEnabled(available);

  const names = result.names || [];
  if (!names.length) {
    list.replaceChildren(emptyState("No secrets stored."));
    return;
  }
  list.replaceChildren(
    ...names.map((name) => {
      const row = el("div", "secret-row");
      row.dataset.secret = name;
      const remove = el("button", "ghost-button compact-button", "Delete");
      remove.type = "button";
      remove.disabled = !available;
      remove.setAttribute("aria-label", `Delete secret ${name}`);
      remove.addEventListener("click", () => deleteSecret(name));
      row.append(el("span", "mono", name), el("code", "secret-ref", `vault:${name}`), remove);
      return row;
    }),
  );
}

async function saveSecret(event) {
  event.preventDefault();
  const name = byId("secretName").value.trim();
  const value = byId("secretValue").value;
  if (!name || !value) {
    showMessage("Enter a secret name and value.", "error");
    return;
  }
  try {
    await api(`/admin/api/secrets/${encodeURIComponent(name)}`, {
      method: "PUT",
      body: JSON.stringify({ value }),
    });
    byId("secretName").value = "";
    byId("secretValue").value = "";
    showMessage(`Saved secret ${name}. Reference it as vault:${name}.`, "ok");
  } catch (error) {
    showMessage(`Could not save secret: ${error.message}`, "error");
  }
  await refreshSecrets();
}

async function deleteSecret(name) {
  if (!window.confirm(`Delete secret "${name}"? Any vault:${name} reference will stop resolving.`)) return;
  try {
    await api(`/admin/api/secrets/${encodeURIComponent(name)}`, { method: "DELETE" });
    showMessage(`Deleted secret ${name}.`, "ok");
  } catch (error) {
    showMessage(`Could not delete secret: ${error.message}`, "error");
  }
  await refreshSecrets();
}

async function migrateSecrets() {
  if (!window.confirm("Move plaintext API keys from .env into the vault? A backup of .env is written first.")) {
    return;
  }
  const box = byId("migrateResult");
  try {
    const result = await api("/admin/api/secrets/migrate", { method: "POST", body: "{}" });
    const migrated = result.migrated || [];
    box.className = "notice warn";
    box.replaceChildren(
      el(
        "p",
        "",
        migrated.length ? `Moved into the vault: ${migrated.join(", ")}.` : "No plaintext keys needed moving.",
      ),
    );
    if (result.backup) {
      const backup = el("p", "", "Backup of the old .env: ");
      backup.appendChild(el("code", "", result.backup));
      box.append(
        backup,
        el("p", "", "It still contains the plaintext keys. Delete it once the server starts cleanly."),
      );
    }
    box.hidden = false;
    showMessage("Keys moved. Restart fcc-server to use the vault references.", "ok");
  } catch (error) {
    showMessage(`Could not move keys: ${error.message}`, "error");
  }
  await refreshSecrets();
}

// ---- Audit view ----

async function refreshAudit(event) {
  event?.preventDefault?.();
  const body = byId("auditBody");
  const badge = byId("auditIntegrity");
  const params = new URLSearchParams({ limit: "100" });
  const action = byId("auditAction").value.trim();
  const actor = byId("auditActor").value.trim();
  if (action) params.set("action", action);
  if (actor) params.set("actor", actor);
  let result;
  try {
    result = await api(`/admin/api/audit?${params}`);
  } catch (error) {
    badge.className = "status-pill warn";
    badge.textContent = "Unavailable";
    unavailable(body, error, "The audit log");
    return;
  }
  badge.className = `status-pill ${result.verified ? "ok" : "error"}`;
  badge.textContent = result.verified
    ? "Chain verified ✓"
    : `Tampered at id ${result.first_bad_id ?? "?"} ✗`;
  const records = result.records || [];
  const recordsPanel = panel(
    "Records",
    "Append-only, hash-chained log of permission, config, secret and verification events.",
  );
  recordsPanel.appendChild(
    records.length
      ? dataTable(
          ["Time", "Actor", "Action", "Resource", "Decision", "Outcome"],
          records.map((record) => [
            formatTime(record.ts),
            record.actor,
            el("span", "mono", record.action || "—"),
            el("span", "mono", record.resource || "—"),
            record.decision || "—",
            record.outcome || "—",
          ]),
        )
      : emptyState("No audit records match."),
  );
  body.replaceChildren(recordsPanel);
}

// ---- Policy view ----

const MODE_TEXT = {
  default: "Claude asks before each tool that is not allowed below.",
  acceptEdits: "File edits run without a prompt; other tools ask unless allowed below.",
  dontAsk: "Only allowed tools run; anything else is refused without a prompt.",
  bypassPermissions: "Everything runs without a prompt, except the ask and deny rules below.",
  plan: "Read-only planning; nothing is changed.",
};
const RULE_LINE = /^(ALLOW|ASK|DENY)\s+(.+?)(?:\s+-\s+(.*))?$/i;

function parseRule(rule) {
  if (rule && typeof rule === "object") {
    return {
      decision: String(rule.decision || "").toLowerCase(),
      rule: String(rule.rule || ""),
      reason: rule.reason || "",
    };
  }
  const match = RULE_LINE.exec(String(rule).trim());
  return match
    ? { decision: match[1].toLowerCase(), rule: match[2], reason: match[3] || "" }
    : { decision: "other", rule: String(rule), reason: "" };
}

async function refreshPolicy() {
  const body = byId("policyBody");
  let result;
  try {
    result = await api("/admin/api/policy/presets");
  } catch (error) {
    unavailable(body, error, "Policy presets");
    return;
  }
  const presets = result.presets || [];
  if (!presets.length) {
    body.replaceChildren(emptyState("No policy presets are defined."));
    return;
  }
  body.replaceChildren(
    ...presets.map((preset) => {
      const card = el("article", "provider-card policy-card");
      card.dataset.preset = preset.id;
      const title = el("div", "provider-title");
      title.append(el("strong", "", preset.id), pill(preset.permission_mode || "default", "info"));
      card.append(title, el("p", "policy-mode", MODE_TEXT[preset.permission_mode] || ""));
      const groups = { deny: [], ask: [], allow: [], other: [] };
      (preset.rules || []).map(parseRule).forEach((rule) => {
        (groups[rule.decision] || groups.other).push(rule);
      });
      [
        ["deny", "Denies"],
        ["ask", "Asks first"],
        ["allow", "Allows"],
        ["other", "Other"],
      ].forEach(([key, label]) => {
        if (!groups[key].length) return;
        const details = el("details", `policy-group ${key}`);
        details.open = key !== "other";
        details.appendChild(el("summary", "", `${label} (${groups[key].length})`));
        const list = details.appendChild(el("ul"));
        groups[key].forEach((rule) => {
          const item = list.appendChild(el("li"));
          item.appendChild(el("code", "", rule.rule));
          if (rule.reason) item.appendChild(el("span", "muted-tag", ` ${rule.reason}`));
        });
        card.appendChild(details);
      });
      return card;
    }),
  );
}

byId("usageWindow").addEventListener("change", refreshUsage);
byId("usageRefresh").addEventListener("click", refreshUsage);
byId("secretForm").addEventListener("submit", saveSecret);
byId("secretMigrate").addEventListener("click", migrateSecrets);
byId("auditFilters").addEventListener("submit", refreshAudit);
window.setInterval(tickCountdowns, 1000);

byId("applyButton").addEventListener("click", apply);
document.addEventListener("pointerdown", (event) => {
  state.modelComboboxes.forEach((combobox) => {
    if (combobox.isOpen && !combobox.element.contains(event.target)) combobox.close();
  });
});

// ---- NVIDIA model slots ----

async function loadNvidiaModels() {
  const { slots, max_slots: max } = await api("/admin/api/nvidia-models");
  const box = byId("nvidiaRows");
  box.innerHTML = "";
  for (let i = 0; i < max; i += 1) {
    const slot = slots[i] || {};
    const row = el("div", "nvidia-row");
    const model = el("input");
    model.type = "text";
    model.className = "nvidia-model";
    model.placeholder = "nvidia/nemotron-3-nano-omni-30b-a3b-reasoning";
    model.value = slot.model || "";
    model.setAttribute("aria-label", `Model id ${i + 1}`);
    const key = el("input");
    key.type = "password";
    key.className = "nvidia-key";
    key.autocomplete = "off";
    key.placeholder = "nvapi-… (leave blank to keep saved key)";
    key.setAttribute("aria-label", `API key ${i + 1}`);
    const radio = el("input");
    radio.type = "radio";
    radio.name = "nvidiaDefault";
    radio.className = "nvidia-default";
    radio.checked = Boolean(slot.default);
    const radioLabel = el("label", "nvidia-default-label");
    radioLabel.append(radio, " Default");
    const clear = el("button", "ghost-button", "Clear");
    clear.type = "button";
    clear.setAttribute("aria-label", `Clear row ${i + 1}`);
    clear.addEventListener("click", () => {
      model.value = "";
      key.value = "";
      radio.checked = false;
      tag.hidden = true;
    });
    const tag = el("span", "status-pill ok", "key saved");
    tag.hidden = !(slot.model && slot.has_key);
    row.append(model, key, tag, radioLabel, clear);
    box.appendChild(row);
  }
}

async function saveNvidiaModels() {
  const slots = [...document.querySelectorAll("#nvidiaRows .nvidia-row")].map((row) => ({
    model: row.querySelector(".nvidia-model").value.trim(),
    key: row.querySelector(".nvidia-key").value.trim() || null,
    default: row.querySelector(".nvidia-default").checked,
  }));
  try {
    const result = await api("/admin/api/nvidia-models", {
      method: "POST",
      body: JSON.stringify({ slots }),
    });
    if (!result.applied) {
      showMessage((result.errors || []).join("; ") || "Could not save models.", "error");
      return;
    }
    await load();
    showMessage("NVIDIA models saved.", "ok");
  } catch (error) {
    showMessage(`Could not save models: ${error.message}`, "error");
  }
}

byId("nvidiaSave").addEventListener("click", saveNvidiaModels);


load().catch((error) => {
  showMessage(error.message, "error");
});
