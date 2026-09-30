"use strict";

/* FreeCode chat client for live agent sessions served by FCC. */

const $ = (id) => document.getElementById(id);
const el = (tag, cls, text) => {
  const node = document.createElement(tag);
  if (cls) node.className = cls;
  if (text !== undefined) node.textContent = text;
  return node;
};

const MODES = [
  { value: "default", label: "Ask permissions", hint: "Approve each edit and command" },
  { value: "acceptEdits", label: "Auto-accept edits", hint: "Edits apply; commands still ask" },
  { value: "plan", label: "Plan mode", hint: "Explore and plan, no changes" },
  { value: "auto", label: "Auto mode", hint: "A classifier approves safe actions" },
  { value: "bypassPermissions", label: "Bypass permissions", hint: "Never ask. Use with care" },
  { value: "dontAsk", label: "Don't ask (read-only)", hint: "Anything that would ask is denied" },
];
const CYCLE_MODES = ["default", "acceptEdits", "plan"];
// Least to most strict (core/claude_permission_modes.py). A policy preset's mode is
// the loosest one its chat may switch to.
const MODE_ORDER = ["bypassPermissions", "auto", "acceptEdits", "default", "dontAsk", "plan"];
const modeAllowed = (mode) => {
  const floor = state.session.preset_permission_mode;
  return !floor || MODE_ORDER.indexOf(mode) >= MODE_ORDER.indexOf(floor);
};

const store = {
  get(key, fallback) {
    try { return localStorage.getItem(key) ?? fallback; } catch { return fallback; }
  },
  set(key, value) {
    try { localStorage.setItem(key, value); } catch { /* storage unavailable */ }
  },
};

const state = {
  home: "",
  transcripts: [],
  live: [],
  commands: [],
  models: [], // [{ value, label, provider, name, default }] — only models the user connected
  initModels: [], // raw initialize.models, used only when /chat/api/models is unavailable
  modelsFromApi: false,
  dirsSupported: true,
  // Current chat
  liveId: null,
  sessionId: null,
  title: "New chat",
  cwd: store.get("fcc.cwd", ""),
  mode: store.get("fcc.mode", "default"),
  model: store.get("fcc.model", ""),
  busy: false,
  exited: false,
  hasMessages: false,
  source: null,
  skipReplay: false,
  attachments: [],
  // Run mode for the next message: normal (Basic, default) | ultra | verified | parallel (+ parallel strategy).
  runMode: store.get("fcc.runModeV2", "normal"),
  strategy: store.get("fcc.strategy", "balanced"),
  // Ultra: concurrent sub-agents (1-6) and whether to run the verification gate.
  ultraParallel: Math.min(6, Math.max(1, Math.floor(Number(store.get("fcc.ultraParallel", "4"))) || 4)),
  ultraVerify: store.get("fcc.ultraVerify", "0") === "1",
  pendingEcho: null, // text of an Ultra message already shown; its fcc_user echo is skipped
  // Session settings sent on the next session start.
  policy: store.get("fcc.policy", ""),
  budget: readJson(store.get("fcc.budget", "{}")),
  session: {}, // policy_preset / budget / usage from the live snapshot
  usage: null, // fcc_usage totals
};

function readJson(text) {
  try { const v = JSON.parse(text); return v && typeof v === "object" ? v : {}; } catch { return {}; }
}

/* ------------------------------------------------------------------ api */

async function api(path, options = {}) {
  const init = { ...options, headers: { "Content-Type": "application/json" } };
  if (options.body !== undefined) init.body = JSON.stringify(options.body);
  const res = await fetch(path, init);
  const data = await res.json().catch(() => ({}));
  if (!res.ok) {
    const message = data?.error?.message || data?.detail || `HTTP ${res.status}`;
    throw new Error(typeof message === "string" ? message : JSON.stringify(message));
  }
  return data;
}

/* ------------------------------------------------------------- markdown */

const esc = (s) => String(s).replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" })[c]);

function inline(text) {
  const codes = [];
  let s = esc(text).replace(/`([^`\n]+)`/g, (_, code) => `\u0000${codes.push(code) - 1}\u0000`);
  s = s
    .replace(/\[([^\]]+)\]\((https?:\/\/[^\s)]+)\)/g, '<a href="$2" target="_blank" rel="noopener noreferrer">$1</a>')
    .replace(/\*\*([^*]+)\*\*/g, "<strong>$1</strong>")
    .replace(/__([^_]+)__/g, "<strong>$1</strong>")
    .replace(/(^|[^*\w])\*([^*\n]+)\*(?!\w)/g, "$1<em>$2</em>")
    .replace(/(^|[^_\w])_([^_\n]+)_(?!\w)/g, "$1<em>$2</em>")
    .replace(/~~([^~]+)~~/g, "<del>$1</del>");
  return s.replace(/\u0000(\d+)\u0000/g, (_, i) => `<code>${codes[Number(i)]}</code>`);
}

// Escape-first renderer: every text fragment passes through esc() before markup is added.
function markdown(src) {
  const lines = String(src).replace(/\r\n?/g, "\n").split("\n");
  const out = [];
  let i = 0;
  const isBlockStart = (line) => /^(```|~~~|#{1,6}\s|>\s?|\s*([-*+]|\d+[.)])\s|\s*(---|\*\*\*|___)\s*$|\|)/.test(line);
  while (i < lines.length) {
    const line = lines[i];
    const fence = line.match(/^\s*(```|~~~)\s*([\w+#.-]*)/);
    if (fence) {
      const body = [];
      i++;
      while (i < lines.length && !lines[i].trim().startsWith(fence[1])) body.push(lines[i++]);
      i++;
      out.push(codeBlockHtml(body.join("\n"), fence[2]));
      continue;
    }
    if (!line.trim()) { i++; continue; }
    const heading = line.match(/^(#{1,6})\s+(.*)$/);
    if (heading) {
      const level = Math.min(heading[1].length, 4);
      out.push(`<h${level}>${inline(heading[2])}</h${level}>`);
      i++;
      continue;
    }
    if (/^\s*(---|\*\*\*|___)\s*$/.test(line)) { out.push("<hr>"); i++; continue; }
    if (/^>\s?/.test(line)) {
      const body = [];
      while (i < lines.length && /^>\s?/.test(lines[i])) body.push(lines[i++].replace(/^>\s?/, ""));
      out.push(`<blockquote>${markdown(body.join("\n"))}</blockquote>`);
      continue;
    }
    if (/^\s*\|.*\|\s*$/.test(line) && i + 1 < lines.length && /^\s*\|?\s*:?-{2,}/.test(lines[i + 1])) {
      const cells = (row) => row.trim().replace(/^\||\|$/g, "").split("|").map((c) => inline(c.trim()));
      const head = cells(line);
      i += 2;
      const rows = [];
      while (i < lines.length && /^\s*\|.*\|\s*$/.test(lines[i])) rows.push(cells(lines[i++]));
      out.push(
        `<table><thead><tr>${head.map((c) => `<th>${c}</th>`).join("")}</tr></thead><tbody>` +
          rows.map((r) => `<tr>${r.map((c) => `<td>${c}</td>`).join("")}</tr>`).join("") +
          "</tbody></table>",
      );
      continue;
    }
    const listMatch = line.match(/^(\s*)([-*+]|\d+[.)])\s+/);
    if (listMatch) {
      const ordered = /\d/.test(listMatch[2]);
      const items = [];
      const baseIndent = listMatch[1].length;
      while (i < lines.length) {
        const m = lines[i].match(/^(\s*)([-*+]|\d+[.)])\s+(.*)$/);
        if (m && m[1].length <= baseIndent + 1) {
          items.push([m[3]]);
          i++;
        } else if (lines[i].trim() && /^\s+/.test(lines[i]) && items.length) {
          items[items.length - 1].push(lines[i].replace(/^\s{0,4}/, ""));
          i++;
        } else break;
      }
      const tag = ordered ? "ol" : "ul";
      out.push(
        `<${tag}>` +
          items
            .map(([first, ...rest]) => {
              const task = first.match(/^\[([ xX])\]\s+(.*)$/);
              const head = task ? `${task[1] === " " ? "☐" : "☑"} ${inline(task[2])}` : inline(first);
              return `<li>${head}${rest.length ? markdown(rest.join("\n")) : ""}</li>`;
            })
            .join("") +
          `</${tag}>`,
      );
      continue;
    }
    const para = [];
    while (i < lines.length && lines[i].trim() && !(para.length && isBlockStart(lines[i]))) para.push(lines[i++]);
    out.push(`<p>${para.map(inline).join("<br>")}</p>`);
  }
  return out.join("");
}

function codeBlockHtml(code, lang) {
  return (
    `<div class="codeblock"><div class="codeblock-head"><span>${esc(lang || "code")}</span>` +
    `<button class="copy-btn" type="button" data-copy>Copy</button></div>` +
    `<pre><code>${esc(code)}</code></pre></div>`
  );
}

document.addEventListener("click", (event) => {
  const button = event.target.closest("[data-copy]");
  if (!button) return;
  const code = button.closest(".codeblock")?.querySelector("code")?.textContent ?? "";
  navigator.clipboard?.writeText(code).then(() => {
    button.textContent = "Copied";
    setTimeout(() => (button.textContent = "Copy"), 1200);
  });
});

/* ------------------------------------------------------------- rendering */

const thread = $("thread");
const scroller = $("scroller");
let view = null;

function resetView() {
  thread.replaceChildren();
  view = {
    tools: new Map(), // tool_use_id -> { node, body, children, name, input }
    prompts: new Map(), // request_id -> card
    streams: new Map(), // message id -> [{ kind, node, text, consumed }]
    streamMsg: null,
    streamBlocks: new Map(), // content block index -> stream entry
    turn: null,
    working: null,
    lastUsage: null,
    changes: new Map(), // file path -> { path, ops: [{ toolId, name, input }] }, most recent last
    tasks: new Map(), // task_id -> verified / parallel task model (see ensureTask)
    injections: new Map(), // tool_use_id -> signals seen before the tool card rendered
  };
  state.usage = null;
  setEmpty(true);
  setContext(0);
  renderChanges();
  renderVerify();
  renderTodoDock(null);
}

// Empty chats center the greeting and composer, like Claude desktop; content pins the composer to the bottom.
function setEmpty(on) {
  $("empty").hidden = !on;
  document.querySelector(".main").classList.toggle("is-empty", on);
  if (on) $("greeting").textContent = greeting();
}

function greeting() {
  const hour = new Date().getHours();
  if (hour >= 5 && hour < 12) return "Good morning";
  if (hour >= 12 && hour < 18) return "Good afternoon";
  return "Good evening";
}

function nearBottom() {
  return scroller.scrollHeight - scroller.scrollTop - scroller.clientHeight < 160;
}
function stick(wasNear) {
  if (wasNear) scroller.scrollTop = scroller.scrollHeight;
}
function append(node) {
  const near = nearBottom();
  if (!$("empty").hidden) setEmpty(false);
  if (view.working && view.working.parentNode === thread) thread.insertBefore(node, view.working);
  else thread.append(node);
  stick(near);
  return node;
}

function currentTurn() {
  if (!view.turn || !view.turn.isConnected) view.turn = append(el("div", "turn"));
  return view.turn;
}

const userText = (message) =>
  typeof message?.content === "string"
    ? message.content
    : (Array.isArray(message?.content) ? message.content : []).filter((b) => b?.type === "text").map((b) => b.text).join("\n");

// Ultra's lead-agent prompt (workbench/ultra.py): the user's request plus sub-agent reports.
const ULTRA_REPORT = /^<fcc_ultra_report task_id="([^"]*)">\n<user_request>\n([\s\S]*?)\n<\/user_request>\n/;

function renderUltraReport(taskId, request, full) {
  const task = view.tasks.get(taskId);
  // Saved transcripts and reconnects never saw the optimistic bubble: show the request.
  if (!task?.requestShown) {
    renderUser({ content: request });
    if (task) task.requestShown = true;
  }
  const details = el("details", "ultra-report");
  details.append(el("summary", "", "Sub-agent reports sent to the lead agent"), el("pre", "pre", full.replace(ULTRA_REPORT, "").trim()));
  view.turn = null;
  return append(details);
}

function renderUser(message) {
  const report = userText(message).match(ULTRA_REPORT);
  if (report) return renderUltraReport(report[1], report[2], userText(message));
  view.turn = null;
  const wrap = el("div", "msg-user");
  const bubble = el("div", "bubble");
  const blocks = typeof message.content === "string" ? [{ type: "text", text: message.content }] : message.content || [];
  for (const block of blocks) {
    if (block.type === "image" && block.source?.data) {
      const img = el("img");
      img.src = `data:${block.source.media_type};base64,${block.source.data}`;
      img.alt = "Attached image";
      bubble.append(img);
    } else if (block.type === "text") {
      const cmd = block.text.match(/<command-name>\s*(\/?[^<\s]+)\s*<\/command-name>/);
      if (cmd) {
        const args = block.text.match(/<command-args>([\s\S]*?)<\/command-args>/)?.[1]?.trim();
        bubble.append(el("span", "cmd", cmd[1].startsWith("/") ? cmd[1] : `/${cmd[1]}`));
        if (args) bubble.append(document.createTextNode(` ${args}`));
      } else {
        bubble.append(document.createTextNode(block.text));
      }
    }
  }
  wrap.append(bubble);
  return append(wrap);
}

function proseNode(text, streaming) {
  const node = el("div", "prose");
  node.innerHTML = markdown(text);
  if (streaming) node.classList.add("cursor");
  return node;
}

function thinkingNode(text) {
  const details = el("details", "thinking");
  details.append(el("summary", "", "Thinking"), el("div", "thinking-body", text));
  return details;
}

function setThinking(node, text) {
  node.querySelector(".thinking-body").textContent = text;
}

let renderQueued = new Set();
function scheduleProse(entry) {
  renderQueued.add(entry);
  if (renderQueued.size > 1) return;
  requestAnimationFrame(() => {
    const near = nearBottom();
    for (const e of renderQueued) {
      if (e.kind === "text") e.node.innerHTML = markdown(e.text);
      else setThinking(e.node, e.text);
    }
    renderQueued = new Set();
    stick(near);
  });
}

function handleStreamEvent(event) {
  if (event.parent_tool_use_id) return;
  const ev = event.event || {};
  if (ev.type === "message_start") {
    view.streamMsg = ev.message?.id || null;
    view.streamBlocks = new Map();
    if (view.streamMsg && !view.streams.has(view.streamMsg)) view.streams.set(view.streamMsg, []);
    return;
  }
  if (!view.streamMsg) return;
  if (ev.type === "content_block_start") {
    const kind = ev.content_block?.type;
    if (kind !== "text" && kind !== "thinking") return;
    const node = kind === "text" ? proseNode("", true) : thinkingNode("");
    const entry = { kind, node, text: "", consumed: false };
    if (kind === "thinking") node.open = true;
    currentTurn().append(node);
    view.streams.get(view.streamMsg).push(entry);
    view.streamBlocks.set(ev.index, entry);
    return;
  }
  const entry = view.streamBlocks.get(ev.index);
  if (!entry) return;
  if (ev.type === "content_block_delta") {
    entry.text += ev.delta?.text ?? ev.delta?.thinking ?? "";
    scheduleProse(entry);
  } else if (ev.type === "content_block_stop") {
    entry.node.classList.remove("cursor");
    if (entry.kind === "thinking") entry.node.open = false;
  }
}

function claimStreamed(messageId, kind) {
  const entries = view.streams.get(messageId);
  const entry = entries?.find((e) => e.kind === kind && !e.consumed);
  if (entry) entry.consumed = true;
  return entry;
}

function renderAssistant(event) {
  const message = event.message || {};
  if (message.usage) view.lastUsage = message.usage;
  const parent = event.parent_tool_use_id ? view.tools.get(event.parent_tool_use_id) : null;
  for (const block of message.content || []) {
    if (block.type === "text" || block.type === "thinking") {
      const text = block.text ?? block.thinking ?? "";
      if (!parent) {
        const streamed = claimStreamed(message.id, block.type);
        if (streamed) {
          streamed.text = text;
          if (block.type === "text") streamed.node.innerHTML = markdown(text);
          else setThinking(streamed.node, text);
          streamed.node.classList.remove("cursor");
          continue;
        }
      }
      if (!text.trim()) continue;
      const node = block.type === "text" ? proseNode(text) : thinkingNode(text);
      (parent ? parent.children : currentTurn()).append(node);
    } else if (block.type === "tool_use" || block.type === "server_tool_use") {
      renderToolUse(block, parent);
    }
  }
}

const shortPath = (p) => {
  if (!p) return "";
  const s = String(p);
  const cwd = state.cwd.replace(/[\\/]+$/, "");
  if (cwd && s.toLowerCase().startsWith(cwd.toLowerCase())) return s.slice(cwd.length + 1) || s;
  return s;
};

function toolSummary(name, input) {
  const i = input || {};
  switch (name) {
    case "Bash": case "PowerShell": return i.description || i.command;
    case "Read": case "Write": case "Edit": case "MultiEdit": case "NotebookEdit": return shortPath(i.file_path || i.notebook_path);
    case "Glob": return i.pattern;
    case "Grep": return `${i.pattern}${i.path ? `  in ${shortPath(i.path)}` : ""}`;
    case "WebFetch": return i.url;
    case "WebSearch": return i.query;
    case "Task": case "Agent": return `${i.subagent_type ? `${i.subagent_type} · ` : ""}${i.description || ""}`;
    case "TodoWrite": return `${(i.todos || []).filter((t) => t.status === "completed").length}/${(i.todos || []).length} done`;
    case "Skill": return i.skill || i.command;
    default: {
      const first = Object.values(i).find((v) => typeof v === "string");
      return first || "";
    }
  }
}

function prettyToolName(name) {
  const mcp = name.match(/^mcp__(.+?)__(.+)$/);
  return mcp ? `${mcp[1]} · ${mcp[2]}` : name;
}

function diffNode(path, edits) {
  const box = el("div", "diff");
  if (path) box.append(el("div", "file", shortPath(path)));
  for (const { old_string: before = "", new_string: after = "" } of edits) {
    for (const line of String(before).split("\n")) if (before) box.append(el("div", "del", `- ${line}`));
    for (const line of String(after).split("\n")) if (after) box.append(el("div", "add", `+ ${line}`));
  }
  return box;
}

function todosNode(todos) {
  const list = el("ul", "todos");
  for (const todo of todos || []) {
    const item = el("li", todo.status);
    item.append(el("span", "box"), el("span", "", todo.status === "in_progress" && todo.activeForm ? todo.activeForm : todo.content));
    list.append(item);
  }
  return list;
}

function toolInputNode(name, input) {
  const i = input || {};
  if (name === "Bash" || name === "PowerShell") return el("pre", "pre", `${name === "Bash" ? "$" : "PS>"} ${i.command}`);
  if (name === "Edit") return diffNode(i.file_path, [i]);
  if (name === "MultiEdit") return diffNode(i.file_path, i.edits || []);
  if (name === "Write") {
    const box = el("div");
    box.append(el("div", "tool-label", shortPath(i.file_path)), el("pre", "pre", i.content ?? ""));
    return box;
  }
  if (name === "TodoWrite") return todosNode(i.todos);
  if (name === "Task" || name === "Agent") return el("pre", "pre", i.prompt ?? "");
  return el("pre", "pre", JSON.stringify(i, null, 2));
}

function renderToolUse(block, parent) {
  const name = block.name || "tool";
  const card = el("details", "tool running");
  const summary = el("summary");
  summary.append(el("span", "tool-status"), el("span", "tool-name", prettyToolName(name)), el("span", "tool-arg", toolSummary(name, block.input) || ""));
  const body = el("div", "tool-body");
  const isAgent = name === "Task" || name === "Agent";
  const children = el("div", "children");
  if (name === "TodoWrite") {
    card.open = true;
    body.append(todosNode(block.input?.todos));
  } else if (name !== "Read") { // Read's only input is the path already in the summary row
    body.append(toolInputNode(name, block.input));
  }
  card.append(summary, body);
  if (isAgent) {
    card.open = true;
    card.append(children);
  }
  (parent ? parent.children : currentTurn()).append(card);
  view.tools.set(block.id, { node: card, body, children, name, input: block.input });
  if (view.injections.has(block.id)) markInjection(block.id, view.injections.get(block.id));
  if (name === "TodoWrite" && !parent) renderTodoDock(block.input?.todos);
}

/* ------------------------------------------------------------ todo dock */

function renderTodoDock(todos) {
  const dock = $("todoDock");
  const list = Array.isArray(todos) ? todos : [];
  dock.hidden = !list.length;
  if (!list.length) return;
  const done = list.filter((t) => t.status === "completed").length;
  const active = list.find((t) => t.status === "in_progress");
  $("todoProgress").textContent = `${done} of ${list.length} done${active ? ` · ${active.activeForm || active.content}` : ""}`;
  $("todoBody").replaceChildren(todosNode(list));
}

/* -------------------------------------------------------- changes pane */

const EDIT_TOOLS = new Set(["Edit", "MultiEdit", "Write", "NotebookEdit"]);

function editsOf(name, input) {
  const i = input || {};
  if (name === "Edit") return [i];
  if (name === "MultiEdit") return i.edits || [];
  if (name === "Write") return [{ new_string: i.content ?? "" }];
  if (name === "NotebookEdit") return [{ old_string: i.old_source ?? "", new_string: i.new_source ?? "" }];
  return [];
}

const lineCount = (s) => (s ? String(s).split("\n").length : 0);

function recordChange(toolId, tool) {
  const path = tool.input?.file_path || tool.input?.notebook_path;
  if (!path) return;
  const key = String(path).toLowerCase();
  const entry = view.changes.get(key) || { path, ops: [] };
  entry.ops.push({ toolId, name: tool.name, input: tool.input });
  view.changes.delete(key); // re-insert so the newest change sorts first
  view.changes.set(key, entry);
  renderChanges();
}

function renderChanges() {
  const entries = [...view.changes.values()].reverse();
  const count = $("changesCount");
  count.hidden = !entries.length;
  count.textContent = String(entries.length);
  $("changesBtn").title = entries.length ? `${entries.length} file${entries.length === 1 ? "" : "s"} changed` : "Changes";
  $("changesSub").textContent = entries.length ? `${entries.length} file${entries.length === 1 ? "" : "s"}` : "";
  const list = $("changesList");
  const open = new Set([...list.querySelectorAll("details[open]")].map((d) => d.dataset.key));
  list.replaceChildren();
  if (!entries.length) {
    list.append(el("div", "changes-empty", "Files FreeCode edits in this chat show up here."));
    return;
  }
  for (const entry of entries) {
    const edits = entry.ops.flatMap((op) => editsOf(op.name, op.input));
    const adds = edits.reduce((n, e) => n + lineCount(e.new_string), 0);
    const dels = edits.reduce((n, e) => n + lineCount(e.old_string), 0);
    const item = el("details", "change");
    item.dataset.key = entry.path.toLowerCase();
    item.open = open.has(item.dataset.key);
    const summary = el("summary");
    const rel = shortPath(entry.path);
    const slash = Math.max(rel.lastIndexOf("/"), rel.lastIndexOf("\\"));
    const names = el("span", "change-names");
    names.append(el("span", "change-file", rel.slice(slash + 1)), el("span", "change-dir", slash > 0 ? rel.slice(0, slash) : ""));
    const stats = el("span", "change-stats");
    stats.append(el("span", "plus", `+${adds}`), el("span", "minus", `−${dels}`));
    const jump = el("button", "icon-btn change-jump");
    jump.type = "button";
    jump.title = "Show in chat";
    jump.setAttribute("aria-label", `Show ${rel} in chat`);
    jump.innerHTML = '<svg viewBox="0 0 20 20"><path d="M7 5h8v8M15 5L5 15"/></svg>';
    jump.onclick = (e) => {
      e.preventDefault();
      if (mobile.matches) toggleChanges(false);
      revealTool(entry.ops[entry.ops.length - 1].toolId);
    };
    summary.append(names, stats, jump);
    summary.onclick = () => { if (!mobile.matches) revealTool(entry.ops[entry.ops.length - 1].toolId); };
    const body = el("div", "change-body");
    for (const op of entry.ops) body.append(diffNode(null, editsOf(op.name, op.input)));
    item.append(summary, body);
    list.append(item);
  }
}

function revealTool(toolId) {
  const node = view.tools.get(toolId)?.node;
  if (!node) return;
  for (let p = node.parentElement; p; p = p.parentElement) if (p.tagName === "DETAILS") p.open = true;
  node.scrollIntoView({ block: "center", behavior: "smooth" });
  node.classList.remove("flash");
  void node.offsetWidth; // restart the highlight animation
  node.classList.add("flash");
}

function toggleChanges(open) {
  const app = $("app");
  const show = open ?? !app.classList.contains("show-changes");
  if (show) toggleVerify(false);
  app.classList.toggle("show-changes", show);
  $("changesBtn").setAttribute("aria-expanded", String(show));
  if (!mobile.matches) store.set("fcc.changes", show ? "1" : "0");
}

function resultText(content) {
  if (typeof content === "string") return content;
  if (Array.isArray(content)) return content.map((c) => (c.type === "text" ? c.text : c.type === "image" ? "[image]" : "")).join("\n");
  return content == null ? "" : JSON.stringify(content, null, 2);
}

function renderToolResults(event) {
  for (const block of event.message?.content || []) {
    if (block.type !== "tool_result") continue;
    const tool = view.tools.get(block.tool_use_id);
    if (!tool) continue;
    tool.node.classList.remove("running");
    tool.node.classList.add(block.is_error ? "error" : "ok");
    if (!block.is_error && EDIT_TOOLS.has(tool.name) && !tool.recorded) {
      tool.recorded = true;
      recordChange(block.tool_use_id, tool);
    }
    if (tool.name === "TodoWrite") continue;
    const text = resultText(block.content).trim();
    if (!text) continue;
    if (tool.name === "Task" || tool.name === "Agent") {
      tool.children.append(proseNode(text));
      tool.node.open = false;
      continue;
    }
    tool.body.append(el("pre", `pre${block.is_error ? " err" : ""}`, text.length > 20000 ? `${text.slice(0, 20000)}\n…` : text));
    if (block.is_error) tool.node.open = true;
  }
}

function notice(text, kind) {
  const node = el("div", `notice${kind ? ` ${kind}` : ""}`, text);
  view.turn = null;
  return append(node);
}

function setWorking(on) {
  if (on && !view.working) {
    const node = el("div", "working");
    node.append(el("span", "spark pulse"), el("span", "", "Working…"));
    view.working = node;
    setEmpty(false);
    thread.append(node);
    stick(true);
  } else if (!on && view.working) {
    view.working.remove();
    view.working = null;
  }
}

const kTokens = (n) => (n >= 1000 ? `${(n / 1000).toFixed(n >= 10000 ? 0 : 1)}k` : String(n));

function renderResult(event) {
  view.turn = null;
  const interrupted = state.interrupted;
  state.interrupted = false;
  if (interrupted && event.subtype === "error_during_execution") notice("Interrupted");
  else if (event.is_error || (event.subtype && event.subtype !== "success")) {
    const errs = Array.isArray(event.errors) ? event.errors.join("\n") : "";
    notice(event.result || errs || `Run ended: ${event.subtype}`, "error");
  }
  const parts = [];
  if (event.duration_ms) parts.push(`${(event.duration_ms / 1000).toFixed(1)}s`);
  if (event.num_turns) parts.push(`${event.num_turns} turn${event.num_turns === 1 ? "" : "s"}`);
  const out = event.usage?.output_tokens;
  if (out) parts.push(`${out.toLocaleString()} output tokens`);
  if (parts.length) append(el("div", "turn-meta", parts.join(" · ")));
  showUsage();
}

// The last assistant message's usage is the live context size (result usage is cumulative).
function showUsage() {
  const u = view.lastUsage;
  if (u) setContext((u.input_tokens || 0) + (u.cache_read_input_tokens || 0) + (u.cache_creation_input_tokens || 0) + (u.output_tokens || 0));
}

// Exact numbers from Claude Code itself when the live process answers; the usage estimate stays otherwise.
async function refreshContext() {
  const liveId = state.liveId;
  if (!liveId || state.exited) return;
  try {
    const { response } = await api(`/chat/api/live/${liveId}/control`, { method: "POST", body: { request: { subtype: "get_context_usage" } } });
    if (liveId === state.liveId && typeof response?.totalTokens === "number") setContext(response.totalTokens, response.maxTokens);
  } catch { /* older servers reject this control request */ }
}

function setContext(tokens, max) {
  const chip = $("ctxChip");
  chip.hidden = !tokens;
  const pct = max ? Math.round((tokens / max) * 100) : null;
  chip.textContent = pct === null ? `${kTokens(tokens)} tokens` : `${pct}% context`;
  chip.title = `Context in use: ${tokens.toLocaleString()}${max ? ` of ${max.toLocaleString()}` : ""} tokens${usageLine()}`;
  setContext.last = [tokens, max];
}

// Session totals from fcc_usage (per-request records) or the live snapshot, appended to the context tooltip.
function usageLine() {
  const u = { ...(state.session.usage || {}), ...(state.usage || {}) };
  const parts = [];
  if (u.input_tokens != null) parts.push(`${Number(u.input_tokens).toLocaleString()} input`);
  if (u.output_tokens != null) parts.push(`${Number(u.output_tokens).toLocaleString()} output tokens`);
  if (u.requests != null) parts.push(`${u.requests} request${u.requests === 1 ? "" : "s"}`);
  if (u.turns != null) parts.push(`${u.turns} turn${u.turns === 1 ? "" : "s"}`);
  if (u.failovers) parts.push(`${u.failovers} failover${u.failovers === 1 ? "" : "s"}`);
  return parts.length ? `\nSession: ${parts.join(" · ")}` : "";
}

function applyUsage(usage) {
  state.usage = usage;
  const [tokens, max] = setContext.last || [0];
  if (tokens) return setContext(tokens, max);
  if (usage && (usage.input_tokens || usage.output_tokens)) {
    const chip = $("ctxChip");
    chip.hidden = false;
    chip.textContent = `${kTokens((usage.input_tokens || 0) + (usage.output_tokens || 0))} tokens`;
    chip.title = `Tokens used this session${usageLine()}`;
  }
}

/* -------------------------------------------------- permission prompts */

// Node prompts (fcc_node_permission) are keyed by the node's live id too: request ids are per session.
const promptKey = (event) => (event.live_id ? `${event.live_id}:${event.request_id}` : event.request_id);

// `node` = {liveId, nodeId} for a Parallel-mode step's prompt, answered on the node's session.
function renderPermission(event, node = null) {
  const request = event.request || {};
  const id = event.request_id;
  const key = node ? promptKey(event) : id;
  if (view.prompts.has(key)) return;
  const tool = request.tool_name;
  const input = request.input || {};
  const card = el("div", "prompt-card");
  view.prompts.set(key, card);
  if (node) Object.assign(card.dataset, { liveId: node.liveId, key, nodeId: node.nodeId });
  if (tool === "AskUserQuestion") buildQuestionCard(card, id, input);
  else if (tool === "ExitPlanMode") buildPlanCard(card, id, input);
  else buildToolPermissionCard(card, id, request);
  if (node) {
    const title = card.querySelector("h4");
    title.textContent = tool === "AskUserQuestion" || tool === "ExitPlanMode"
      ? `Step ${node.nodeId}: ${title.textContent}`
      : `Step ${node.nodeId} wants to run ${prettyToolName(tool || "tool")}`;
  }
  view.turn = null;
  append(card);
  card.querySelector("button, input")?.focus({ preventScroll: true });
}

function renderNodePermission(event) {
  renderPermission(event, { liveId: String(event.live_id), nodeId: String(event.node_id) });
  setNodeWaiting(event, event.request?.tool_name || "tool");
}

function resolveNodePermission(event) {
  setNodeWaiting(event, null);
  resolvePrompt(promptKey(event), event.behavior === "deny" ? "denied" : event.behavior ? "" : "no longer pending");
}

// The task graph shows a node as "waiting for approval" while any of its prompts is open.
function setNodeWaiting(event, tool) {
  const task = view.tasks.get(String(event.task_id));
  const n = task?.orch?.nodes.get(String(event.node_id));
  if (!n) return;
  n.waiting ||= new Map();
  if (tool) n.waiting.set(promptKey(event), tool);
  else n.waiting.delete(promptKey(event));
  renderTaskCard(task, false);
  scheduleVerify();
}

async function answer(id, decision, card, label) {
  card.querySelectorAll("button").forEach((b) => (b.disabled = true));
  const liveId = card.dataset.liveId || state.liveId;
  try {
    await api(`/chat/api/live/${liveId}/permissions/${encodeURIComponent(id)}`, { method: "POST", body: { decision } });
    resolvePrompt(card.dataset.key || id, label);
  } catch (err) {
    card.querySelectorAll("button").forEach((b) => (b.disabled = false));
    status(err.message, true);
  }
}

function resolvePrompt(id, label) {
  const card = view.prompts.get(id);
  if (!card || card.classList.contains("resolved")) return;
  card.classList.add("resolved");
  if (label) card.querySelector("h4").textContent += ` — ${label}`;
}

function buildToolPermissionCard(card, id, request) {
  const input = request.input || {};
  const name = request.tool_name || "tool";
  card.append(el("h4", "", `Allow ${prettyToolName(name)}?`));
  if (request.decision_reason || request.blocked_path) {
    card.append(el("p", "why", request.decision_reason || `Path: ${request.blocked_path}`));
  }
  const detail = toolInputNode(name, input);
  detail.classList.add("prompt-extra");
  card.append(detail);
  const actions = el("div", "prompt-actions");
  const allow = el("button", "btn primary", "Allow once");
  allow.onclick = () => answer(id, { behavior: "allow", updatedInput: input }, card, "allowed");
  actions.append(allow);
  const suggestions = request.permission_suggestions;
  if (Array.isArray(suggestions) && suggestions.length) {
    const always = el("button", "btn", "Always allow");
    always.title = "Remember this permission as suggested";
    always.onclick = () => answer(id, { behavior: "allow", updatedInput: input, updatedPermissions: suggestions }, card, "always allowed");
    actions.append(always);
  }
  const feedback = el("input", "feedback");
  feedback.placeholder = "Tell FreeCode what to do instead (optional)";
  const deny = el("button", "btn danger", "Deny");
  deny.onclick = () => answer(id, { behavior: "deny", message: feedback.value.trim() || "The user denied this action." }, card, "denied");
  feedback.addEventListener("keydown", (e) => { if (e.key === "Enter") deny.click(); });
  actions.append(deny, feedback);
  card.append(actions);
}

function buildQuestionCard(card, id, input) {
  const questions = input.questions || [];
  card.append(el("h4", "", "FreeCode has a question"));
  const readers = [];
  questions.forEach((q, qi) => {
    const box = el("div", "question prompt-extra");
    box.append(el("div", "question-title", q.question));
    const options = el("div", "options");
    const type = q.multiSelect ? "checkbox" : "radio";
    for (const option of q.options || []) {
      const label = el("label", "option");
      const inputEl = el("input");
      inputEl.type = type;
      inputEl.name = `q${qi}-${id}`;
      inputEl.value = option.label;
      const text = el("span", "", option.label);
      if (option.description) text.append(el("small", "", option.description));
      label.append(inputEl, text);
      options.append(label);
    }
    const other = el("input", "feedback");
    other.placeholder = "Other…";
    box.append(options, other);
    card.append(box);
    readers.push(() => {
      const picked = [...options.querySelectorAll("input:checked")].map((i) => i.value);
      if (other.value.trim()) picked.push(other.value.trim());
      return [q.question, picked.join(", ")];
    });
  });
  const actions = el("div", "prompt-actions");
  const submit = el("button", "btn primary", "Submit answers");
  submit.onclick = () => {
    const answers = Object.fromEntries(readers.map((read) => read()));
    answer(id, { behavior: "allow", updatedInput: { ...input, answers } }, card, "answered");
  };
  const skip = el("button", "btn", "Skip");
  skip.onclick = () => answer(id, { behavior: "deny", message: "The user declined to answer." }, card, "skipped");
  actions.append(submit, skip);
  card.append(actions);
}

function buildPlanCard(card, id, input) {
  card.append(el("h4", "", "Ready to code? Here is FreeCode's plan"));
  const plan = el("div", "plan prose prompt-extra");
  plan.innerHTML = markdown(input.plan || "");
  card.append(plan);
  const actions = el("div", "prompt-actions");
  const setMode = (mode) => [{ type: "setMode", mode, destination: "session" }];
  const auto = el("button", "btn primary", "Yes, auto-accept edits");
  // A step's plan changes the step's mode, not this chat's.
  const local = (mode) => { if (!card.dataset.liveId) applyModeLocally(mode); };
  auto.onclick = () => {
    answer(id, { behavior: "allow", updatedInput: input, updatedPermissions: setMode("acceptEdits") }, card, "approved");
    local("acceptEdits");
  };
  const manual = el("button", "btn", "Yes, approve each edit");
  manual.onclick = () => {
    answer(id, { behavior: "allow", updatedInput: input, updatedPermissions: setMode("default") }, card, "approved");
    local("default");
  };
  const feedback = el("input", "feedback");
  feedback.placeholder = "Tell FreeCode what to change";
  const keep = el("button", "btn", "Keep planning");
  keep.onclick = () => answer(id, { behavior: "deny", message: feedback.value.trim() || "Keep planning; the user wants to refine the plan." }, card, "keep planning");
  actions.append(auto, manual, keep, feedback);
  card.append(actions);
}

/* --------------------------------------------------------------- events */

function handleEvent(event) {
  if (state.skipReplay) {
    // Saved transcript already rendered: only pick up state and still-open prompts.
    if (event.type === "fcc_replay_end") { state.skipReplay = false; return; }
    // Task events still rebuild the Verification panel; inline cards are skipped (the transcript has no anchors).
    if (!["fcc_state", "fcc_initialize", "control_request", "fcc_node_permission", "fcc_permission_resolved", "system", "fcc_usage"].includes(event.type) && !TASK_EVENTS.has(event.type)) return;
    if (event.type === "system" && event.subtype !== "init") return;
  }
  switch (event.type) {
    case "fcc_replay_end": return;
    case "fcc_state": return applyLiveState(event);
    case "fcc_initialize": return applyInitialize(event.response || {});
    case "fcc_user":
      setWorking(true);
      if (state.pendingEcho !== null && userText(event.message) === state.pendingEcho) {
        state.pendingEcho = null; // Ultra ran it directly; the bubble is already shown
        return;
      }
      return renderUser(event.message || {});
    case "stream_event": return handleStreamEvent(event);
    case "assistant": return renderAssistant(event);
    case "user": return renderToolResults(event);
    case "control_request":
      if (event.request?.subtype === "can_use_tool") renderPermission(event);
      return;
    case "fcc_node_permission": return renderNodePermission(event);
    case "fcc_permission_resolved":
      if (event.node_id != null) return resolveNodePermission(event);
      return resolvePrompt(event.request_id, event.behavior === "deny" ? "denied" : "");
    case "result":
      setWorking(false);
      refreshSessions();
      renderResult(event);
      refreshContext();
      return;
    case "system": return handleSystem(event);
    case "fcc_exit":
      setWorking(false);
      if (event.code) notice(`Agent process exited (code ${event.code}).${event.stderr ? `\n${event.stderr}` : ""}`, "error");
      refreshSessions();
      return;
    case "fcc_raw": return;
    case "fcc_usage": return applyUsage(event);
    case "fcc_budget": return renderBudget(event);
    case "fcc_injection": return markInjection(event.tool_use_id, event.signals);
    default:
      if (TASK_EVENTS.has(event.type)) handleTaskEvent(event);
      return;
  }
}

function handleSystem(event) {
  switch (event.subtype) {
    case "init":
      if (Array.isArray(event.slash_commands) && !state.commands.length) {
        state.commands = event.slash_commands.map((name) => ({ name, description: "" }));
      }
      return;
    case "api_retry":
      notice(`Provider error (${event.error || event.error_status || "unknown"}). Retrying ${event.attempt}/${event.max_retries} in ${Math.round((event.retry_delay_ms || 0) / 1000)}s…`);
      return;
    case "compact_boundary":
      view.turn = null;
      append(el("div", "divider", "Conversation compacted"));
      return;
    case "permission_denied":
      notice(`Denied: ${event.tool_name || "tool"}`);
      return;
    default:
      return;
  }
}

function applyLiveState(snapshot) {
  state.busy = !!snapshot.busy;
  state.exited = !!snapshot.exited;
  state.hasMessages = !!snapshot.has_messages;
  if (snapshot.session_id) state.sessionId = snapshot.session_id;
  if (snapshot.permission_mode) state.mode = snapshot.permission_mode;
  if (snapshot.model) state.model = snapshot.model;
  for (const key of ["policy_preset", "preset_permission_mode", "budget", "usage"]) if (key in snapshot) state.session[key] = snapshot[key];
  setWorking(state.busy);
  renderControls();
  markActiveSession();
}

function applyInitialize(response) {
  if (Array.isArray(response.commands)) state.commands = response.commands;
  if (Array.isArray(response.models)) state.initModels = response.models;
  // Cached so menus work immediately on the next load, before a process finishes starting.
  store.set("fcc.catalog", JSON.stringify({ commands: state.commands, models: state.initModels }));
  if (!state.modelsFromApi) state.models = gatewayModels(state.initModels);
  renderControls();
}

function loadCatalog() {
  try {
    const { commands, models } = JSON.parse(store.get("fcc.catalog", "{}"));
    if (Array.isArray(commands)) state.commands = commands;
    if (Array.isArray(models)) state.initModels = models;
    state.models = gatewayModels(state.initModels);
  } catch { /* corrupt cache: wait for initialize */ }
}

function modelEntry(value, label, provider, isDefault) {
  const prov = provider || (label.includes("/") ? label.split("/")[0] : "");
  const name = prov && label.startsWith(`${prov}/`) ? label.slice(prov.length + 1) : label;
  return { value, label, provider: prov, name, default: !!isDefault };
}

// Fallback only: the gateway part of Claude's catalog, without Anthropic aliases or "no thinking" twins.
function gatewayModels(raw) {
  return (raw || [])
    .filter((m) => typeof m?.value === "string" && m.value.startsWith("anthropic/") && !/no thinking/i.test(m.displayName || ""))
    .map((m) => modelEntry(m.value, m.displayName || m.value.slice("anthropic/".length)));
}

async function loadModels() {
  try {
    const res = await fetch("/chat/api/models");
    if (res.ok) {
      const data = await res.json();
      if (Array.isArray(data.models)) {
        state.models = data.models
          .filter((m) => typeof m?.value === "string" && m.value)
          .map((m) => modelEntry(m.value, String(m.label || m.value), m.provider, m.default || m.value === data.default));
        state.modelsFromApi = true;
        $("setupCard").hidden = state.models.length > 0;
        renderControls();
        return;
      }
    }
  } catch { /* endpoint missing: keep the gateway fallback */ }
  state.modelsFromApi = false;
  state.models = gatewayModels(state.initModels);
  renderControls();
}

/* ------------------------------------------------------------ sessions */

function connect(liveId, skipReplay) {
  state.source?.close();
  state.liveId = liveId;
  state.skipReplay = skipReplay;
  const source = new EventSource(`/chat/api/live/${liveId}/events`);
  source.onmessage = (msg) => {
    try { handleEvent(JSON.parse(msg.data)); } catch (err) { console.error(err); }
  };
  source.onerror = () => {
    if (state.exited) source.close();
  };
  state.source = source;
}

async function startLive() {
  const snapshot = await api("/chat/api/live", {
    method: "POST",
    body: {
      cwd: state.cwd || state.home,
      // With a policy preset the server runs the preset's mode; mid-chat you can still pick a stricter one.
      permission_mode: state.policy ? null : state.mode,
      model: store.get("fcc.model", "") || null,
      resume_session_id: state.sessionId,
      policy_preset: state.policy || null,
      budget: budgetBody(),
    },
  });
  state.exited = false;
  connect(snapshot.live_id, false);
  applyLiveState(snapshot);
  refreshSessions();
  return snapshot;
}

async function newChat() {
  if (state.liveId && !state.hasMessages && !state.exited) {
    api(`/chat/api/live/${state.liveId}`, { method: "DELETE" }).catch(() => {});
  }
  state.source?.close();
  state.source = null;
  state.liveId = null;
  state.sessionId = null;
  state.busy = false;
  state.exited = false;
  setTitle("New chat");
  resetView();
  renderControls();
  markActiveSession();
  $("input").focus();
  // Warm a process so slash commands and gateway models are ready before the first prompt.
  try { await startLive(); } catch (err) { status(err.message, true); }
}

async function openTranscript(summary) {
  state.source?.close();
  state.source = null;
  state.liveId = null;
  state.sessionId = summary.session_id;
  state.cwd = summary.cwd || state.cwd;
  setTitle(summary.title);
  resetView();
  renderControls();
  markActiveSession();
  const live = state.live.find((s) => s.session_id === summary.session_id && !s.exited);
  try {
    const { events } = await api(`/chat/api/transcripts/${encodeURIComponent(summary.session_id)}`);
    for (const event of events) handleEvent(event);
    loadTaskHistory(summary.session_id);
    // Saved transcripts carry no result events, so replayed prompts must not leave "Working…" up.
    setWorking(false);
    showUsage();
    scroller.scrollTop = scroller.scrollHeight;
    if (live) connect(live.live_id, true);
  } catch (err) {
    if (live) connect(live.live_id, false);
    else status(err.message, true);
  }
}

async function openLive(snapshot) {
  const summary = state.transcripts.find((t) => t.session_id === snapshot.session_id);
  if (summary) return openTranscript(summary);
  state.sessionId = snapshot.session_id;
  state.cwd = snapshot.cwd;
  setTitle("New chat");
  resetView();
  connect(snapshot.live_id, false);
  applyLiveState(snapshot);
}

async function refreshSessions() {
  try {
    const data = await api("/chat/api/sessions");
    state.home = data.home;
    state.transcripts = data.transcripts || [];
    state.live = data.live || [];
    if (!state.cwd) state.cwd = state.transcripts.find(isAppChat)?.cwd || state.home;
    const current = state.transcripts.find((t) => t.session_id === state.sessionId);
    if (current && state.title === "New chat") setTitle(current.title);
    renderSessions();
    renderControls();
  } catch (err) {
    status(err.message, true);
  }
}

const folderName = (p) => (p || "").split(/[\\/]/).filter(Boolean).pop() || p || "";

function renderSessions() {
  const list = $("sessionList");
  const query = $("search").value.trim().toLowerCase();
  list.replaceChildren();
  const liveBySession = new Map(state.live.filter((s) => !s.exited && s.session_id).map((s) => [s.session_id, s]));
  const known = new Set(state.transcripts.map((t) => t.session_id));
  const unsaved = state.live.filter((s) => !s.exited && s.has_messages && s.session_id && !known.has(s.session_id) && s.live_id !== state.liveId);
  let lastGroup = "";
  for (const snapshot of unsaved) {
    if (lastGroup !== "Running") list.append(el("div", "group-label", (lastGroup = "Running")));
    list.append(sessionButton({ session_id: snapshot.session_id, title: "Untitled chat", cwd: snapshot.cwd }, snapshot, () => openLive(snapshot)));
  }
  const showAll = store.get("fcc.showAll", "0") === "1";
  let hidden = 0;
  for (const summary of state.transcripts) {
    if (query && !`${summary.title} ${summary.cwd}`.toLowerCase().includes(query)) continue;
    if (!showAll && !isAppChat(summary) && summary.session_id !== state.sessionId) { hidden++; continue; }
    if (lastGroup !== "Recents") list.append(el("div", "group-label", (lastGroup = "Recents")));
    const button = sessionButton(summary, liveBySession.get(summary.session_id), () => openTranscript(summary));
    button.title = new Date(summary.updated_at * 1000).toLocaleString();
    list.append(button);
  }
  if (query && lastGroup !== "Recents") list.append(el("div", "group-label", "No matching chats"));
  if (showAll || hidden) {
    // Headless sessions (plugins, other tools) stay out of Recents unless asked for.
    const toggle = el("label", "show-all");
    const box = el("input");
    box.type = "checkbox";
    box.checked = showAll;
    box.onchange = () => { store.set("fcc.showAll", box.checked ? "1" : "0"); renderSessions(); };
    toggle.append(box, el("span", "", `Show all sessions${hidden ? ` (${hidden})` : ""}`));
    list.append(toggle);
  }
  markActiveSession();
}

// Older servers omit "app": treat those transcripts as UI chats so nothing disappears.
const isAppChat = (summary) => summary.app !== false;

function sessionButton(summary, live, onOpen) {
  const button = el("div", "session");
  button.tabIndex = 0;
  button.setAttribute("role", "button");
  button.dataset.sessionId = summary.session_id;
  const text = el("div", "session-text");
  text.append(el("div", "session-title", summary.title), el("div", "session-meta", folderName(summary.cwd)));
  button.append(text);
  if (live) {
    const dot = el("span", `live-dot${live.busy ? " busy" : ""}`);
    dot.title = live.busy ? "Working" : "Running";
    button.append(dot);
  }
  const del = el("button", "icon-btn session-del");
  del.title = "Delete chat";
  del.setAttribute("aria-label", "Delete chat");
  del.innerHTML = '<svg viewBox="0 0 20 20"><path d="M5 6h10M8 6V4.5h4V6M6.5 6l.7 9.5h5.6l.7-9.5"/></svg>';
  del.onclick = (e) => { e.stopPropagation(); deleteSession(summary, live); };
  button.append(del);
  button.onclick = onOpen;
  button.onkeydown = (e) => { if (e.key === "Enter") onOpen(); };
  return button;
}

function markActiveSession() {
  for (const node of document.querySelectorAll(".session")) {
    node.classList.toggle("active", !!state.sessionId && node.dataset.sessionId === state.sessionId);
  }
}

async function deleteSession(summary, live) {
  if (!confirm(`Delete "${summary.title}"? The saved transcript is removed permanently.`)) return;
  try {
    if (live) await api(`/chat/api/live/${live.live_id}`, { method: "DELETE" }).catch(() => {});
    await api(`/chat/api/transcripts/${encodeURIComponent(summary.session_id)}`, { method: "DELETE" }).catch(() => {});
    if (summary.session_id === state.sessionId) await newChat();
    refreshSessions();
  } catch (err) {
    status(err.message, true);
  }
}

/* ------------------------------------------------------------ composer */

const input = $("input");

function autosize() {
  input.style.height = "auto";
  input.style.height = `${Math.min(input.scrollHeight, window.innerHeight * 0.4)}px`;
}

async function send() {
  const text = input.value.trim();
  if (!text && !state.attachments.length) return;
  if (state.busy) return;
  if (state.runMode === "verified" || state.runMode === "parallel") await checkProject(true);
  const blocker = projectBlocker();
  if (blocker?.block) {
    status(blocker.text, true);
    return;
  }
  const content = state.attachments.length
    ? [...state.attachments.map((a) => ({ type: "image", source: { type: "base64", media_type: a.type, data: a.data } })), ...(text ? [{ type: "text", text }] : [])]
    : text;
  input.value = "";
  state.attachments = [];
  renderAttachments();
  autosize();
  closeSlash();
  let bubble = null;
  try {
    if (!state.liveId || state.exited) await startLive();
    if (state.title === "New chat" && text) setTitle(text.slice(0, 60));
    state.busy = true;
    renderControls();
    const runMode = state.runMode;
    const body = { content, mode: runMode };
    if (runMode === "parallel") body.strategy = state.strategy;
    if (runMode === "ultra") {
      Object.assign(body, { verify: state.ultraVerify, max_parallel: state.ultraParallel });
      // Show the request now: an orchestrated run never echoes it (a direct one does; skip that echo).
      bubble = renderUser({ content });
      state.pendingEcho = text;
    }
    const res = await api(`/chat/api/live/${state.liveId}/messages`, { method: "POST", body });
    if (runMode !== "normal") {
      // Older servers answer {ok: true} without task_id and run the message as a plain chat turn.
      if (!("task_id" in res)) status(`This server does not support ${RUN_MODES[runMode].label} mode yet; sent as a normal message.`, true);
      else if (res.task_id && !view.tasks.has(String(res.task_id))) handleTaskEvent({ type: "fcc_task", task_id: res.task_id, mode: runMode, status: "RECEIVED" });
      if (runMode === "ultra" && res.task_id) ensureTask(String(res.task_id)).requestShown = true;
    }
  } catch (err) {
    bubble?.remove();
    state.pendingEcho = null;
    state.busy = false;
    renderControls();
    status(err.message, true);
    if (typeof content === "string") input.value = content;
  }
}

// The running Ultra task of this chat, if any: Stop cancels it (sub-agents included).
function activeUltra() {
  return [...view.tasks.values()].find((t) => t.mode === "ultra" && !TERMINAL_TASK.has(t.status) && !t.ultra?.endedAt);
}

async function interrupt() {
  if (!state.liveId) return;
  const ultra = activeUltra();
  if (ultra) return cancelTask(ultra);
  state.interrupted = true;
  try {
    await api(`/chat/api/live/${state.liveId}/control`, { method: "POST", body: { request: { subtype: "interrupt" } } });
  } catch (err) {
    status(err.message, true);
  }
}

function status(text, isError) {
  const line = $("statusLine");
  line.textContent = text;
  line.style.color = isError ? "var(--danger)" : "";
  clearTimeout(status.timer);
  status.timer = setTimeout(() => {
    line.textContent = "FreeCode can make mistakes. Review changes before shipping.";
    line.style.color = "";
  }, isError ? 8000 : 3000);
}

function setTitle(title) {
  state.title = title || "New chat";
  $("chatTitle").textContent = state.title;
  document.title = state.title === "New chat" ? "FreeCode" : `${state.title} — FreeCode`;
}

function renderControls() {
  const mode = MODES.find((m) => m.value === state.mode) || MODES[0];
  const modeBtn = $("modeBtn");
  modeBtn.textContent = mode.label;
  modeBtn.className = `chip mode-${mode.value}`;
  const current = currentModel();
  $("modelBtn").textContent = current ? current.name : state.model || "Default model";
  $("modelBtn").title = `Model: ${current ? current.label : state.model || "server default"}`;
  const sendBtn = $("sendBtn");
  sendBtn.classList.toggle("stop", state.busy);
  sendBtn.title = state.busy ? "Stop (Esc)" : "Send (Enter)";
  $("folderLabel").textContent = state.cwd || state.home || "~";
  if (state.runMode !== "normal" && project.cwd !== (state.cwd || state.home)) checkProject();
  renderProjectWarning();
  $("folderChip").title = state.cwd;
  store.set("fcc.cwd", state.cwd);
  // Remember only your own picks, not a preset's mode.
  if (!state.session.preset_permission_mode) store.set("fcc.mode", state.mode === "bypassPermissions" ? "default" : state.mode);
}

// Claude reports the resolved upstream id (e.g. "nvidia_nim/x/y") while menus list gateway aliases.
const findModel = (id) => id && state.models.find((m) => m.value === id || m.label === id || m.value === `anthropic/${id}`);
// Claude's own aliases (and the ids they resolve to) mean "whatever the server routes MODEL to".
const isServerDefault = (id) => !id || /^(default|sonnet|haiku|opus|fable|claude-)/i.test(id);
const defaultModel = () => state.models.find((m) => m.default) || null;
const currentModel = () => findModel(state.model) || (isServerDefault(state.model) ? defaultModel() : null);

function applyModeLocally(mode) {
  // The server clamps plan-approval mode switches to the preset's mode; mirror that.
  state.mode = modeAllowed(mode) ? mode : state.session.preset_permission_mode;
  renderControls();
}

async function setMode(mode) {
  if (!modeAllowed(mode)) {
    status(`The ${state.session.policy_preset} policy allows ${MODES.find((m) => m.value === state.session.preset_permission_mode)?.label || state.session.preset_permission_mode} or stricter.`, true);
    return;
  }
  applyModeLocally(mode);
  if (!state.liveId || state.exited) return;
  try {
    await api(`/chat/api/live/${state.liveId}/control`, { method: "POST", body: { request: { subtype: "set_permission_mode", mode } } });
    status(`Permission mode: ${MODES.find((m) => m.value === mode)?.label}`);
  } catch (err) {
    status(err.message, true);
  }
}

async function setModel(model) {
  state.model = model;
  // Persist only explicit picks; the resolved id Claude reports back is not a valid --model value.
  store.set("fcc.model", model || "");
  renderControls();
  if (!state.liveId || state.exited) return;
  try {
    await api(`/chat/api/live/${state.liveId}/control`, { method: "POST", body: { request: { subtype: "set_model", model: model || defaultModel()?.value || "default" } } });
    status(`Model: ${findModel(model)?.label || model || defaultModel()?.label || "server default"}`);
  } catch (err) {
    status(err.message, true);
  }
}

/* ------------------------------------------------------------- menus */

function togglePopover(id, open) {
  const pop = $(id);
  const show = open ?? pop.hidden;
  for (const other of document.querySelectorAll(".popover")) other.hidden = true;
  pop.hidden = !show;
  return show;
}

function renderModeMenu() {
  const menu = $("modeMenu");
  menu.replaceChildren();
  for (const mode of MODES) {
    const item = el("button", `menu-item${mode.value === state.mode ? " selected" : ""}`, mode.label);
    item.type = "button";
    item.disabled = !modeAllowed(mode.value);
    item.append(el("small", "", item.disabled ? `Not allowed by the ${state.session.policy_preset} policy` : mode.hint));
    item.onclick = () => { togglePopover("modeMenu", false); setMode(mode.value); };
    menu.append(item);
  }
}

function renderModelMenu() {
  const list = $("modelList");
  const query = $("modelSearch").value.trim().toLowerCase();
  list.replaceChildren();
  const current = currentModel();
  const pick = (value) => { togglePopover("modelMenu", false); setModel(value); };
  // Without a flagged default, offer the server route (no --model) explicitly.
  const models = defaultModel() ? state.models : [modelEntry("", "Default", "", true), ...state.models];
  let lastProvider = null;
  for (const model of models) {
    if (query && !`${model.label} ${model.value}`.toLowerCase().includes(query)) continue;
    if (model.value && model.provider !== lastProvider) {
      lastProvider = model.provider;
      list.append(el("div", "menu-group", model.provider || "Other"));
    }
    const selected = model.value ? model === current : !current && isServerDefault(state.model);
    const item = el("button", `menu-item model-item${selected ? " selected" : ""}`);
    item.type = "button";
    item.setAttribute("role", "option");
    item.setAttribute("aria-selected", String(selected));
    const line = el("span", "model-line", model.name);
    if (model.default) line.append(el("span", "tag", "Default"));
    item.append(line); // the provider group header is the caption
    if (!model.value) item.append(el("small", "", "Server-routed model (MODEL in Providers & models)"));
    // The default entry starts Claude without --model so FCC routes it.
    item.onclick = () => pick(model.default ? "" : model.value);
    list.append(item);
  }
  if (!state.models.length && !query) list.append(el("div", "menu-empty", state.modelsFromApi ? "No models connected yet." : "Loading models…"));
  if (query && !list.querySelector(".menu-item")) {
    const custom = el("button", "menu-item", `Use custom id "${$("modelSearch").value.trim()}"`);
    custom.type = "button";
    custom.onclick = () => { togglePopover("modelMenu", false); setModel($("modelSearch").value.trim()); };
    list.append(custom);
  }
}

// One popup serves "/" commands and "@" file mentions; `seq` drops stale file lookups.
const suggest = { kind: null, items: [], index: 0, seq: 0, timer: 0 };

function slashMatches() {
  const match = input.value.match(/^\/(\S*)$/);
  if (!match) return null;
  const q = match[1].toLowerCase();
  return state.commands.filter((c) => c.name.toLowerCase().includes(q)).sort((a, b) => a.name.toLowerCase().indexOf(q) - b.name.toLowerCase().indexOf(q)).slice(0, 60);
}

function mentionQuery() {
  return input.value.slice(0, input.selectionStart).match(/(?:^|\s)@([^\s@"]*)$/)?.[1] ?? null;
}

function updateSuggest() {
  const commands = slashMatches();
  if (commands) return showSuggest("slash", commands);
  const q = mentionQuery();
  if (q === null) return closeSlash();
  const seq = ++suggest.seq;
  clearTimeout(suggest.timer);
  suggest.timer = setTimeout(async () => {
    let files = [];
    try {
      const params = new URLSearchParams({ cwd: state.cwd || state.home, q });
      const res = await fetch(`/chat/api/files?${params}`);
      if (res.ok) files = (await res.json()).files;
    } catch { /* optional endpoint: stay silent */ }
    if (seq !== suggest.seq || mentionQuery() !== q) return;
    showSuggest("mention", Array.isArray(files) ? files.filter((f) => typeof f === "string").slice(0, 50) : []);
  }, 120);
}

function showSuggest(kind, items) {
  const menu = $("slashMenu");
  if (!items.length) {
    menu.hidden = true;
    return;
  }
  if (suggest.kind !== kind) suggest.index = 0;
  suggest.kind = kind;
  suggest.items = items;
  suggest.index = Math.min(suggest.index, items.length - 1);
  menu.replaceChildren();
  items.forEach((entry, index) => {
    const item = el("button", `menu-item${index === suggest.index ? " active" : ""}`);
    item.type = "button";
    if (kind === "slash") {
      item.append(el("b", "", `/${entry.name}${entry.argumentHint ? ` ${entry.argumentHint}` : ""}`), el("small", "", entry.description || ""));
    } else {
      const slash = entry.lastIndexOf("/");
      item.append(el("b", "", entry.slice(slash + 1)), el("small", "", slash > 0 ? entry.slice(0, slash) : ""));
    }
    item.onmousedown = (e) => { e.preventDefault(); pickSuggest(index); };
    menu.append(item);
  });
  menu.hidden = false;
  menu.children[suggest.index]?.scrollIntoView({ block: "nearest" });
}

function moveSuggest(delta) {
  suggest.index = Math.max(0, Math.min(suggest.items.length - 1, suggest.index + delta));
  showSuggest(suggest.kind, suggest.items);
}

function pickSuggest(index) {
  const entry = suggest.items[index];
  if (suggest.kind === "slash") {
    input.value = `/${entry.name} `;
  } else {
    const pos = input.selectionStart;
    const ref = /\s/.test(entry) ? `"${entry}"` : entry;
    const before = input.value.slice(0, pos).replace(/@[^\s@"]*$/, `@${ref} `);
    input.value = before + input.value.slice(pos);
    input.setSelectionRange(before.length, before.length);
  }
  closeSlash();
  input.focus();
  autosize();
}

function closeSlash() {
  $("slashMenu").hidden = true;
  suggest.kind = null;
  suggest.index = 0;
  suggest.seq++;
  clearTimeout(suggest.timer);
}

/* --------------------------------------------------------- attachments */

function addFiles(files) {
  for (const file of files) {
    if (!file.type.startsWith("image/")) continue;
    const reader = new FileReader();
    reader.onload = () => {
      const data = String(reader.result).split(",")[1];
      state.attachments.push({ type: file.type, data, url: reader.result });
      renderAttachments();
    };
    reader.readAsDataURL(file);
  }
}

function renderAttachments() {
  const box = $("attachments");
  box.replaceChildren();
  state.attachments.forEach((attachment, index) => {
    const thumb = el("div", "thumb");
    const img = el("img");
    img.src = attachment.url;
    img.alt = "Attachment";
    const remove = el("button", "", "×");
    remove.title = "Remove";
    remove.onclick = () => { state.attachments.splice(index, 1); renderAttachments(); };
    thumb.append(img, remove);
    box.append(thumb);
  });
}

/* ------------------------------------------------------------- folder */

const folder = { path: "", parent: null, seq: 0 };

function recentFolders() {
  const seen = new Set();
  const out = [];
  const showAll = store.get("fcc.showAll", "0") === "1";
  for (const cwd of state.transcripts.filter((t) => showAll || isAppChat(t)).map((t) => t.cwd)) {
    const key = (cwd || "").replace(/[\\/]+$/, "").toLowerCase();
    if (!cwd || seen.has(key)) continue;
    seen.add(key);
    out.push(cwd);
    if (out.length === 8) break;
  }
  return out;
}

function openFolderDialog() {
  const inputEl = $("folderInput");
  inputEl.value = state.cwd || state.home;
  $("folderDialog").returnValue = ""; // Esc must not reuse a previous "ok"
  const recent = $("recentFolders");
  recent.replaceChildren();
  const folders = recentFolders();
  if (folders.length) recent.append(el("div", "dialog-label", "Recent"));
  for (const cwd of folders) {
    const item = el("button", "folder-item");
    item.type = "button";
    item.title = cwd;
    item.append(folderIcon(), el("span", "folder-name", folderName(cwd)), el("span", "folder-path", cwd));
    item.onclick = () => chooseFolder(cwd);
    recent.append(item);
  }
  $("folderDialog").showModal();
  if (state.dirsSupported) browse(inputEl.value);
  inputEl.select();
}

function folderIcon() {
  const span = el("span", "folder-icon");
  span.innerHTML = '<svg viewBox="0 0 20 20"><path d="M3 6.5A1.5 1.5 0 014.5 5H8l1.5 1.5h6A1.5 1.5 0 0117 8v6.5a1.5 1.5 0 01-1.5 1.5h-11A1.5 1.5 0 013 14.5z"/></svg>';
  return span;
}

function chooseFolder(path) {
  $("folderInput").value = path;
  $("folderDialog").close("ok");
}

async function browse(path) {
  const seq = ++folder.seq;
  const list = $("dirList");
  let data;
  try {
    const res = await fetch(`/chat/api/dirs?${new URLSearchParams({ path: path || "" })}`);
    if (res.status === 404 || res.status === 405) {
      // Endpoint not deployed: keep the plain path input.
      state.dirsSupported = false;
      $("folderBrowser").hidden = true;
      return;
    }
    data = await res.json().catch(() => ({}));
    if (!res.ok) throw new Error(data?.error?.message || data?.detail || `HTTP ${res.status}`);
  } catch (err) {
    if (seq !== folder.seq || !state.dirsSupported) return;
    $("folderBrowser").hidden = false;
    list.replaceChildren(el("div", "dir-empty", `Can't open this folder: ${err.message}`));
    return;
  }
  if (seq !== folder.seq) return;
  $("folderBrowser").hidden = false;
  folder.path = data.path || "";
  folder.parent = data.parent || null;
  $("folderInput").value = folder.path;
  $("folderUp").disabled = !folder.parent;
  renderCrumbs(folder.path);
  const drives = $("folderDrives");
  drives.replaceChildren();
  for (const root of Array.isArray(data.roots) ? data.roots : []) {
    const drive = el("button", `drive${folder.path.toLowerCase().startsWith(String(root).toLowerCase()) ? " active" : ""}`, String(root).replace(/[\\/]+$/, "") || root);
    drive.type = "button";
    drive.title = root;
    drive.onclick = () => browse(root);
    drives.append(drive);
  }
  list.replaceChildren();
  const dirs = Array.isArray(data.dirs) ? data.dirs : [];
  if (!dirs.length) list.append(el("div", "dir-empty", "No subfolders"));
  for (const dir of dirs) {
    const item = el("button", "folder-item");
    item.type = "button";
    item.setAttribute("role", "option");
    item.title = `${dir.path}\nDouble-click to open`;
    item.append(folderIcon(), el("span", "folder-name", dir.name));
    item.onclick = () => browse(dir.path);
    item.ondblclick = () => chooseFolder(dir.path);
    list.append(item);
  }
  list.scrollTop = 0;
}

function renderCrumbs(path) {
  const crumbs = $("folderCrumbs");
  crumbs.replaceChildren();
  const sep = path.includes("\\") || /^[A-Za-z]:/.test(path) ? "\\" : "/";
  const parts = path.split(/[\\/]+/).filter(Boolean);
  let acc = sep === "/" ? "/" : "";
  parts.forEach((part, index) => {
    acc = index === 0 && sep === "\\" ? `${part}\\` : `${acc}${acc.endsWith(sep) ? "" : sep}${part}`;
    const target = acc;
    if (index) crumbs.append(el("span", "crumb-sep", "›"));
    const crumb = el("button", "crumb", part);
    crumb.type = "button";
    crumb.onclick = () => browse(target);
    crumbs.append(crumb);
  });
  crumbs.scrollLeft = crumbs.scrollWidth;
}

$("folderUp").onclick = () => folder.parent && browse(folder.parent);
$("folderCancel").onclick = () => $("folderDialog").close("cancel");
$("folderInput").addEventListener("keydown", (e) => {
  if (e.key !== "Enter") return;
  const typed = e.target.value.trim();
  // With the browser available, Enter navigates to a typed path first; Enter again opens it.
  if (state.dirsSupported && !$("folderBrowser").hidden && typed && typed !== folder.path) {
    e.preventDefault();
    browse(typed);
  }
});

$("folderDialog").addEventListener("close", async () => {
  if ($("folderDialog").returnValue !== "ok") return;
  const next = $("folderInput").value.trim();
  if (!next || next === state.cwd) return;
  state.cwd = next;
  renderControls();
  // A folder change starts a fresh chat rooted in that folder.
  if (state.liveId && !state.sessionId) await api(`/chat/api/live/${state.liveId}`, { method: "DELETE" }).catch(() => {});
  await newChat();
});

/* ------------------------------------------------ run modes & settings */

const RUN_MODES = {
  ultra: { label: "Ultra", hint: "A lead agent answers directly or splits the work across parallel sub-agents", placeholder: "Ask anything. Bigger tasks are split across parallel sub-agents…" },
  normal: { label: "Basic", hint: "Plain conversation with FreeCode", placeholder: "Ask FreeCode to build, fix, or explain…" },
  verified: { label: "Verified", hint: "Intent contract, verification gate, auto-recovery", placeholder: "Describe the change. FreeCode checks it against real evidence before calling it done…" },
  parallel: { label: "Parallel", hint: "Split into a task graph of parallel sessions, then verify", placeholder: "Describe a larger change to split across parallel sessions…" },
};
// Max concurrent nodes per strategy (workbench.orchestration.runner.PARALLELISM).
const STRATEGIES = { economy: "One node at a time", balanced: "Up to 3 nodes at once", fastest: "Up to 6 nodes at once" };
const BUDGET_FIELDS = [["max_turns", "budgetTurns"], ["max_minutes", "budgetMinutes"], ["max_output_tokens", "budgetTokens"]];

const cap = (s) => (s ? `${s[0].toUpperCase()}${s.slice(1)}` : "");

function renderRunMode() {
  const btn = $("runModeBtn");
  const mode = RUN_MODES[state.runMode];
  btn.textContent = state.runMode === "parallel" ? `${mode.label} · ${cap(state.strategy)}` : state.runMode === "ultra" && state.ultraVerify ? `${mode.label} · Verified` : mode.label;
  btn.className = `chip run-${state.runMode}`;
  btn.title = `Run mode: ${mode.label} — ${mode.hint}`;
  input.placeholder = mode.placeholder;
}

function setRunMode(value) {
  state.runMode = value;
  store.set("fcc.runModeV2", value);
  renderRunMode();
  scheduleVerify();
  checkProject();
}

// Verified needs git to checkpoint/revert/scope-check; Parallel also needs a clean tree.
const project = { cwd: null, info: null };

async function checkProject(force) {
  const cwd = state.cwd || state.home;
  // Ultra works in any folder (git or not, clean or dirty): no preflight.
  if (state.runMode === "normal" || state.runMode === "ultra" || !cwd) return renderProjectWarning();
  if (force || project.cwd !== cwd) {
    project.cwd = cwd;
    project.info = null;
    try {
      const res = await fetch(`/chat/api/project?cwd=${encodeURIComponent(cwd)}`);
      if (res.ok && project.cwd === cwd) project.info = await res.json();
    } catch { /* older server: no preflight */ }
  }
  renderProjectWarning();
}

function projectBlocker() {
  const info = project.info;
  if (!info || state.runMode === "normal" || state.runMode === "ultra") return null;
  if (!info.git) {
    return state.runMode === "parallel"
      ? { block: true, text: "Parallel mode needs a git repository: this folder is not one." }
      : { block: false, text: "Not a git repository: changes can't be checkpointed, reverted or scope-checked, so the result will be Needs review at best." };
  }
  if (state.runMode === "parallel" && info.dirty) return { block: true, text: "Parallel mode needs a clean git tree: commit or stash your changes (untracked files count) first." };
  if (state.runMode === "parallel" && !info.branch) return { block: true, text: "Parallel mode needs a checked-out branch (HEAD is detached)." };
  return null;
}

function renderProjectWarning() {
  const box = $("projectWarning");
  const problem = projectBlocker();
  box.hidden = !problem;
  box.className = `banner tone-${problem?.block ? "bad" : "warn"}`;
  box.textContent = problem ? problem.text : "";
  $("sendBtn").disabled = !!problem?.block && !state.busy;
}

function menuRadio(label, hint, checked, onPick) {
  const item = el("button", `menu-item${checked ? " selected" : ""}`, label);
  item.type = "button";
  item.setAttribute("role", "menuitemradio");
  item.setAttribute("aria-checked", String(checked));
  if (hint) item.append(el("small", "", hint));
  item.onclick = onPick;
  return item;
}

function renderRunModeMenu() {
  const menu = $("runModeMenu");
  menu.replaceChildren();
  for (const [value, mode] of Object.entries(RUN_MODES)) {
    menu.append(menuRadio(mode.label, mode.hint, value === state.runMode, () => {
      setRunMode(value);
      if (value === "parallel") {
        renderRunModeMenu();
        menu.querySelector(".segmented [aria-checked=true]")?.focus();
      } else closePopovers();
    }));
  }
  if (state.runMode !== "parallel") return;
  const group = el("div", "strategy");
  group.setAttribute("role", "group");
  group.setAttribute("aria-label", "Parallel strategy");
  group.append(el("div", "menu-group", "Strategy"));
  const seg = el("div", "segmented");
  for (const [value, hint] of Object.entries(STRATEGIES)) {
    const b = el("button", "", cap(value));
    b.type = "button";
    b.title = hint;
    b.setAttribute("role", "menuitemradio");
    b.setAttribute("aria-checked", String(value === state.strategy));
    b.onclick = () => {
      state.strategy = value;
      store.set("fcc.strategy", value);
      renderRunMode();
      renderRunModeMenu();
      menu.querySelector(".segmented [aria-checked=true]")?.focus();
    };
    seg.append(b);
  }
  group.append(seg, el("small", "strategy-hint", STRATEGIES[state.strategy]));
  menu.append(group);
}

const presets = { loaded: false, supported: true, list: [] };
let settingsDirty = false;

async function loadPresets() {
  if (presets.loaded) return;
  presets.loaded = true;
  try {
    const res = await fetch("/chat/api/policy/presets");
    if (!res.ok) throw new Error(`HTTP ${res.status}`);
    const data = await res.json();
    presets.list = Array.isArray(data.presets) ? data.presets.filter((p) => p && typeof p.id === "string") : [];
  } catch {
    presets.supported = false; // older server: choices are still saved and sent
  }
  renderSettings();
}

function budgetBody() {
  const out = {};
  for (const [key] of BUDGET_FIELDS) {
    const v = Number(state.budget[key]);
    out[key] = Number.isFinite(v) && v > 0 ? (key === "max_minutes" ? v : Math.max(1, Math.floor(v))) : null;
  }
  return Object.values(out).some((v) => v !== null) ? out : null;
}

function budgetText(budget) {
  const b = budget || {};
  const parts = [];
  if (b.max_turns) parts.push(`${b.max_turns} turns`);
  if (b.max_minutes) parts.push(`${b.max_minutes} min`);
  if (b.max_output_tokens) parts.push(`${Number(b.max_output_tokens).toLocaleString()} output tokens`);
  return parts.length ? parts.join(" · ") : "no budgets";
}

function renderSettings() {
  $("policySelect").value = state.policy;
  for (const [key, id] of BUDGET_FIELDS) if (document.activeElement !== $(id)) $(id).value = state.budget[key] ?? "";
  if (document.activeElement !== $("ultraParallel")) $("ultraParallel").value = String(state.ultraParallel);
  $("ultraVerify").checked = state.ultraVerify;
  const rules = $("policyRules");
  rules.replaceChildren();
  const preset = presets.list.find((p) => p.id === state.policy);
  if (!presets.supported) {
    rules.append(el("p", "note", "This server has no policy presets yet. Your choice is saved and sent once it does."));
  } else if (!state.policy) {
    rules.append(el("p", "note", "No FCC policy: the agent's own permission settings apply."));
  } else if (preset) {
    const mode = MODES.find((m) => m.value === preset.permission_mode);
    if (preset.permission_mode) rules.append(el("div", "rules-mode", `Permission mode: ${mode ? mode.label : preset.permission_mode}`));
    const list = el("ul", "rules");
    for (const rule of Array.isArray(preset.rules) ? preset.rules : []) list.append(el("li", "", String(rule)));
    if (list.children.length) rules.append(list);
  } else if (!presets.loaded || presets.list.length) {
    rules.append(el("p", "note", presets.loaded ? "Unknown preset." : "Loading rules…"));
  }
  const s = state.session;
  $("settingsCurrent").textContent =
    "policy_preset" in s || "budget" in s ? `This session: ${s.policy_preset || "no policy"} · ${budgetText(s.budget)}` : "";
}

// An unused warm process restarts right away so the new settings apply to the first message.
async function applySettings() {
  settingsDirty = false;
  if (!state.liveId || state.exited) return;
  if (state.hasMessages || state.busy) {
    status("Session settings apply when this chat's next session starts.");
    return;
  }
  const old = state.liveId;
  state.source?.close();
  state.sessionId = null;
  await api(`/chat/api/live/${old}`, { method: "DELETE" }).catch(() => {});
  try {
    await startLive();
    status("Session settings applied.");
  } catch (err) {
    status(err.message, true);
  }
}

function closePopovers() {
  for (const pop of document.querySelectorAll(".popover")) pop.hidden = true;
  syncExpanded();
}

function syncExpanded() {
  for (const [btn, pop] of [["runModeBtn", "runModeMenu"], ["settingsBtn", "settingsMenu"]]) $(btn).setAttribute("aria-expanded", String(!$(pop).hidden));
  if (settingsDirty && $("settingsMenu").hidden) applySettings();
}

/* ------------------------------------------------------ endpoint health */

const PROVIDER_NAMES = { nvidia_nim: "NIM", open_router: "OpenRouter", openrouter: "OpenRouter", lmstudio: "LM Studio", llamacpp: "llama.cpp", ollama: "Ollama", deepseek: "DeepSeek" };
const health = { timer: 0 };

async function loadHealth() {
  const chip = $("healthChip");
  let data;
  try {
    const res = await fetch("/chat/api/endpoints/summary");
    if (res.status === 404 || res.status === 405) {
      // ponytail: endpoint missing on this server; stop polling until the page reloads.
      chip.hidden = true;
      clearInterval(health.timer);
      health.timer = 0;
      return;
    }
    if (!res.ok) throw new Error(`HTTP ${res.status}`);
    data = await res.json();
  } catch {
    chip.hidden = true;
    return;
  }
  if (!health.timer) health.timer = setInterval(loadHealth, 20000);
  renderHealth(Array.isArray(data?.providers) ? data.providers : []);
}

function renderHealth(providers) {
  const chip = $("healthChip");
  const list = providers.filter((p) => p && Number(p.total) > 0);
  chip.hidden = !list.length;
  if (!list.length) return;
  const tone = (p) => (!Number(p.healthy) ? "bad" : Number(p.cooling) || Number(p.open) || p.healthy < p.total ? "warn" : "ok");
  const rank = { ok: 0, warn: 1, bad: 2 };
  const name = (p) => PROVIDER_NAMES[p.provider_id] || p.provider_id || "Provider";
  const worst = [...list].sort((a, b) => rank[tone(b)] - rank[tone(a)])[0];
  const text = `${name(worst)} ${worst.healthy}/${worst.total} key${worst.total === 1 ? "" : "s"} healthy${list.length > 1 ? ` +${list.length - 1}` : ""}`;
  chip.dataset.tone = tone(worst);
  $("healthText").textContent = text;
  const lines = list.map((p) => `${name(p)}: ${p.healthy}/${p.total} healthy${p.cooling ? `, ${p.cooling} cooling down` : ""}${p.open ? `, ${p.open} circuit open` : ""}`);
  chip.title = `${lines.join("\n")}\nOpen Providers & models`;
  chip.setAttribute("aria-label", `Endpoint health: ${lines.join("; ")}. Opens Providers & models in a new tab.`);
}

/* ----------------------------------------------------- budget, injection */

function renderBudget(event) {
  const limit = Number(event.limit);
  const n = Number.isFinite(limit) ? limit.toLocaleString() : String(event.limit ?? "?");
  const what = { turns: `turn limit (${n})`, time: `time limit (${n} min)`, tokens: `output token limit (${n})` }[event.kind] || `${event.kind || "budget"} limit (${n})`;
  const node = notice(`Stopped: ${what} reached`, "warn");
  if (event.used != null) node.title = `Used ${event.used} of ${event.limit}`;
}

function markInjection(toolId, signals) {
  if (!toolId) return;
  const list = (Array.isArray(signals) ? signals : [signals]).filter((s) => s != null && s !== "").map(String);
  const tool = view.tools.get(toolId);
  if (!tool) {
    view.injections.set(toolId, list); // the card renders later (or never, for replay-only history)
    return;
  }
  view.injections.delete(toolId);
  if (tool.node.querySelector(":scope > summary .inj-badge")) return;
  const label = "Possible prompt injection in tool output";
  const badge = el("span", "inj-badge", "Injection?");
  badge.title = `${label}${list.length ? `: ${list.join(", ")}` : ""}`;
  badge.setAttribute("aria-label", badge.title);
  tool.node.querySelector(":scope > summary .tool-arg")?.after(badge);
  const note = el("div", "inj-note");
  note.setAttribute("role", "note");
  note.append(el("strong", "", label));
  if (list.length) note.append(el("div", "", `Signals: ${list.join(", ")}`));
  note.append(el("div", "", "The agent was told to treat this output as data, not instructions."));
  tool.body.prepend(note);
}

/* ---------------------------------------------------- verified tasks */

const TASK_EVENTS = new Set([
  "fcc_task", "fcc_intent", "fcc_checkpoint", "fcc_verification_started", "fcc_verification_check",
  "fcc_verification", "fcc_recovery", "fcc_orchestration", "fcc_ultra",
]);
const TERMINAL_TASK = new Set(["VERIFIED", "CANCELLED", "FAILED", "COMPLETED"]);
const TASK_KINDS = { verified: "Verified", parallel: "Parallel", ultra: "Ultra" };
const taskKind = (task) => `${TASK_KINDS[task.mode] || "Verified"} task`;
const PRE_LOCK = new Set(["RECEIVED", "INTENT_COMPILED", "BLOCKED_FOR_CLARIFICATION"]);
const DISPOSITIONS = {
  VERIFIED: ["ok", "Verified", "Every required check passed and each requirement has evidence."],
  FAILED_VERIFICATION: ["bad", "Failed verification", "A required check or requirement failed."],
  NEEDS_REVIEW: ["warn", "Needs review", "The evidence does not prove the change yet."],
};
const RISKS = ["low", "medium", "high", "critical"];
const CHECK_ICONS = { passed: "✓", failed: "✕", error: "!", skipped: "–", advisory_failed: "!", needs_review: "?", running: "" };

const pretty = (s) => (s ? cap(String(s).toLowerCase().replace(/_/g, " ")) : "Starting");
const shortSha = (s) => (s ? String(s).slice(0, 8) : "");

function ensureTask(id) {
  let task = view.tasks.get(id);
  if (!task) {
    task = {
      id, mode: "verified", status: null, reason: "", intent: null, checkpoint: null,
      attempts: new Map(), evidence: null, disposition: null, timeline: [], orch: null,
      reverted: null, clarifySent: false, answerDraft: "", card: null,
    };
    view.tasks.set(id, task);
  }
  return task;
}

function timeline(task, text, kind, at) {
  const time = at ? new Date(at) : new Date();
  task.timeline.push({ time: Number.isNaN(time.getTime()) ? new Date() : time, text, kind: kind || "" });
}

function attemptOf(task, n) {
  const key = Number(n) || task.attempts.size || 1;
  if (!task.attempts.has(key)) task.attempts.set(key, { n: key, level: "", checks: new Map(), disposition: null });
  return task.attempts.get(key);
}

function handleTaskEvent(ev) {
  if (!ev.task_id) return;
  const task = ensureTask(String(ev.task_id));
  switch (ev.type) {
    case "fcc_task":
      // An Ultra run answered directly with Verified on reports as "verified": still Ultra.
      if (ev.mode && task.mode !== "ultra") task.mode = ev.mode;
      if (ev.status && (ev.status !== task.status || (ev.reason && ev.reason !== task.reason))) {
        task.status = ev.status;
        task.reason = ev.reason || "";
        timeline(task, `${pretty(ev.status)}${ev.reason ? ` — ${ev.reason}` : ""}`, "status");
      }
      break;
    case "fcc_intent": {
      const blocked = ev.status === "blocked_for_clarification";
      task.intent = {
        status: ev.status,
        contract: ev.contract && typeof ev.contract === "object" ? ev.contract : {},
        questions: Array.isArray(ev.questions) ? ev.questions.map(String) : [],
        warnings: Array.isArray(ev.warnings) ? ev.warnings.map(String) : [],
      };
      task.clarifySent = false;
      timeline(task, blocked ? `Intent needs clarification (${task.intent.questions.length} question${task.intent.questions.length === 1 ? "" : "s"})` : "Intent contract ready to lock", blocked ? "warn" : "");
      break;
    }
    case "fcc_checkpoint":
      task.checkpoint = { id: ev.checkpoint_id, supported: ev.supported !== false, head: ev.head };
      timeline(task, task.checkpoint.supported ? `Checkpoint created at ${shortSha(ev.head) || "working tree"}` : "No checkpoint here (not a git repository): revert is unavailable");
      break;
    case "fcc_verification_started": {
      const attempt = attemptOf(task, ev.attempt);
      attempt.level = ev.level || attempt.level;
      for (const check of Array.isArray(ev.checks) ? ev.checks : []) {
        if (check?.id != null) attempt.checks.set(String(check.id), { ...check, status: "running" });
      }
      timeline(task, `Verification attempt ${attempt.n} started${attempt.level ? ` (${attempt.level})` : ""}: ${attempt.checks.size} check${attempt.checks.size === 1 ? "" : "s"}`);
      break;
    }
    case "fcc_verification_check": {
      const check = ev.check || {};
      if (check.id == null) break;
      const attempt = attemptOf(task, ev.attempt);
      attempt.checks.set(String(check.id), { ...(attempt.checks.get(String(check.id)) || {}), ...check });
      break;
    }
    case "fcc_verification":
      applyEvidence(task, ev.attempt, ev.disposition, ev.evidence);
      timeline(task, `Attempt ${attemptOf(task, ev.attempt).n}: ${DISPOSITIONS[task.disposition]?.[1] || pretty(task.disposition)}`, DISPOSITIONS[task.disposition]?.[0]);
      break;
    case "fcc_recovery": {
      const action = { retry: "retrying", ask_user: "asking you", give_up: "giving up", none: "no recovery needed" }[ev.action] || ev.action;
      const strategy = ev.strategy && ev.strategy !== ev.action && ev.strategy !== "none" ? ` with ${pretty(ev.strategy).toLowerCase()}` : "";
      timeline(task, `Recovery after attempt ${ev.attempt ?? "?"}: ${action}${strategy}${ev.reason ? ` — ${ev.reason}` : ""}`, "recovery");
      break;
    }
    case "fcc_orchestration":
      applyOrchestration(task, ev.event || {}, ev.ts);
      break;
    case "fcc_ultra":
      applyUltra(task, ev);
      break;
    default:
      break;
  }
  renderTaskCard(task, !state.skipReplay);
  scheduleVerify();
}

/* ------------------------------------------------------------ ultra mode */

const ULTRA_PHASES = {
  analyzing: "Analyzing", direct: "Answering directly", planned: "Plan ready", dispatching: "Sub-agents working",
  integrating: "Integrating", summarizing: "Summarizing", verifying: "Verifying", done: "Done", failed: "Failed", cancelled: "Cancelled",
};
const ULTRA_ENDED = new Set(["done", "failed", "cancelled"]);
// Main-agent steps shown in the lead lane, in order.
const ULTRA_STEPS = [["analyzing", "Analyze"], ["planned", "Plan"], ["dispatching", "Dispatch"], ["integrating", "Integrate"], ["summarizing", "Summarize"]];
const epochMs = (ts) => (Number(ts) > 0 ? Number(ts) * 1000 : Date.now());

function applyUltra(task, ev) {
  task.mode = "ultra";
  const u = task.ultra || (task.ultra = { phase: "analyzing", seen: new Set(), startedAt: epochMs(ev.ts), endedAt: null });
  const at = epochMs(ev.ts);
  const phase = ev.phase || u.phase;
  u.phase = phase;
  u.seen.add(phase);
  for (const key of ["route", "reason", "request", "degraded", "analysis_ms", "max_parallel", "effective_parallel", "parallel_limited_by", "workspace", "error", "status"]) {
    if (ev[key] !== undefined) u[key] = ev[key];
  }
  if (phase === "direct") {
    timeline(task, `Lead agent: answering directly${u.reason ? ` (${u.reason})` : ""}`);
  } else if (phase === "planned") {
    u.dispatchAt = at + (Number(ev.dispatch_in_s) || 0) * 1000;
    state.pendingEcho = null; // orchestrated: the request is never echoed
    applyOrchestration(task, { type: "plan_ready", graph: ev.plan }, ev.ts);
    timeline(task, `Lead agent planned ${task.orch.nodes.size} sub-task${task.orch.nodes.size === 1 ? "" : "s"}${u.reason ? ` (${u.reason})` : ""}`);
  } else if (ULTRA_ENDED.has(phase)) {
    u.endedAt = at;
    timeline(task, `Ultra ${ULTRA_PHASES[phase].toLowerCase()}${ev.error ? `: ${ev.error}` : ""}`, phase === "done" ? "ok" : phase === "failed" ? "bad" : "");
    // Nothing else will clear the composer's busy state for an orchestrated run.
    state.busy = false;
    setWorking(false);
    renderControls();
  }
  ultraTick();
}

// One ticker re-renders running Ultra cards (elapsed times, the dispatch countdown).
let ultraTimer = 0;
function ultraTick() {
  const running = [...view.tasks.values()].filter((t) => t.ultra && !t.ultra.endedAt);
  if (running.length && !ultraTimer) ultraTimer = setInterval(ultraTick, 1000);
  if (!running.length && ultraTimer) {
    clearInterval(ultraTimer);
    ultraTimer = 0;
  }
  for (const task of running) renderTaskCard(task, false);
}

function secondsText(ms) {
  const s = Math.max(0, Math.round(ms / 1000));
  return s < 60 ? `${s}s` : `${Math.floor(s / 60)}m ${String(s % 60).padStart(2, "0")}s`;
}

function renderUltraCard(task) {
  const { root, head, line } = task.card;
  const u = task.ultra || { phase: "analyzing", seen: new Set(), startedAt: Date.now() };
  const ended = !!u.endedAt || TERMINAL_TASK.has(task.status);
  const tone = ended ? taskTone(task) : "run";
  root.dataset.tone = tone;
  root.classList.add("ultra-card");
  const remembered = new Map([...root.querySelectorAll("details[data-key]")].map((d) => [d.dataset.key, d.open]));
  const icon = el("span", "task-icon spark small");
  icon.setAttribute("aria-hidden", "true");
  const label = ended && task.status ? taskLabel(task) : ULTRA_PHASES[u.phase] || pretty(u.phase);
  head.replaceChildren(icon, el("span", "task-kind", "Ultra"), pill(tone, label));
  head.append(el("span", "ultra-elapsed", secondsText((u.endedAt || Date.now()) - u.startedAt)));
  if (!ended) {
    const stop = el("button", "btn small danger ultra-stop", "Stop");
    stop.type = "button";
    stop.title = "Stop the lead agent and every sub-agent";
    stop.onclick = () => cancelTask(task, stop);
    head.append(stop);
  }
  const open = el("button", "btn small task-open", "Details");
  open.type = "button";
  open.setAttribute("aria-label", "Open this task in the Verification panel");
  open.onclick = () => revealTask(task.id);
  head.append(open);

  // Lead lane: the main agent's steps.
  const lead = el("div", "lane lead");
  const leadHead = el("div", "lane-head");
  leadHead.append(el("span", "lane-name", "Main agent"));
  if (u.analysis_ms != null) leadHead.append(el("span", "lane-meta", `analysis ${(Number(u.analysis_ms) / 1000).toFixed(1)}s`));
  lead.append(leadHead);
  const steps = el("ol", "ultra-steps");
  const direct = u.route === "direct" || u.seen.has("direct");
  const list = direct ? [["analyzing", "Analyze"], ["direct", "Answer"]] : [...ULTRA_STEPS, ...(u.seen.has("verifying") ? [["verifying", "Verify"]] : [])];
  const current = list.findIndex(([p]) => p === u.phase);
  list.forEach(([p, text], i) => {
    const done = ended ? u.seen.has(p) : current >= 0 ? i < current : u.seen.has(p);
    const li = el("li", done ? "done" : p === u.phase && !ended ? "current" : "", text);
    if (p === u.phase && !ended) li.setAttribute("aria-current", "step");
    steps.append(li);
  });
  lead.append(steps);
  if (u.reason) lead.append(el("div", "lane-meta", u.reason));
  if (u.error) lead.append(el("div", "gnode-err", u.error));
  const lowMemory = u.parallel_limited_by === "memory" && u.effective_parallel;
  if (u.phase === "planned" && !ended) {
    const n = task.orch?.nodes.size || 0;
    const left = Math.ceil(((u.dispatchAt || 0) - Date.now()) / 1000);
    const pace = lowMemory ? ` (running ${u.effective_parallel} at a time (low memory))` : u.max_parallel ? ` (${u.max_parallel} at once)` : "";
    lead.append(el("div", "ultra-countdown", `Starting ${n} sub-agent${n === 1 ? "" : "s"}${pace}${left > 0 ? ` in ${left}s` : "…"}`));
  } else if (lowMemory && !direct && !ended) {
    lead.append(el("div", "lane-meta ultra-memory", `running ${u.effective_parallel} at a time (low memory)`));
  }
  const body = [lead];

  const lanes = el("div", "ultra-lanes");
  for (const n of task.orch?.nodes.values() || []) lanes.append(ultraLane(task, n, remembered));
  if (lanes.children.length) body.push(lanes);
  const integration = task.orch?.integration;
  if (integration) {
    const conflicts = Object.entries(integration.conflicts || {});
    const text = integration.status === "failed"
      ? `Integration failed: ${integration.error || ""}`
      : conflicts.length
        ? `Conflicts (not applied): ${conflicts.map(([id, files]) => `${id}: ${[].concat(files).join(", ")}`).join("; ")}`
        : `Applied: ${(integration.merged || []).join(", ") || "nothing to apply"}`;
    body.push(el("div", `lane-meta ultra-integration${conflicts.length || integration.status === "failed" ? " warn" : ""}`, text));
  }
  if (task.orch?.ignored?.length) body.push(el("div", "lane-meta ultra-ignored", `Ignored tool-state files: ${task.orch.ignored.join(", ")}`));
  line.replaceChildren(...body);
  line.hidden = false;
}

function ultraLane(task, n, remembered) {
  const waiting = n.status === "running" && n.waiting?.size ? n.waiting : null;
  const lane = el("div", `lane gnode st-${n.status}${waiting ? " waiting" : ""}`);
  const head = el("div", "lane-head");
  head.append(el("span", "gnode-id", n.id));
  if (n.role) head.append(el("span", "tag", n.role));
  const took = n.elapsed_s != null ? Number(n.elapsed_s) * 1000 : n.startedAt ? (n.endedAt || Date.now()) - n.startedAt : null;
  if (took != null) head.append(el("span", "lane-meta", secondsText(took)));
  head.append(waiting ? pill("warn", "Waiting for approval") : pill(NODE_TONES[n.status] || "muted", pretty(n.status)));
  lane.append(head);
  if (n.objective) lane.append(el("p", "gnode-obj", n.objective));
  if (n.instructions) {
    const details = keyed(el("details", "lane-brief"), `ub:${task.id}:${n.id}`, false, remembered);
    details.append(el("summary", "", "Instructions"), el("div", "gnode-sum-body", n.instructions));
    lane.append(details);
  }
  const scope = Array.isArray(n.write_scope) ? n.write_scope : [];
  lane.append(el("div", "gnode-meta", scope.length ? `Writes: ${scope.join(", ")}` : "Read-only"));
  if (waiting) {
    const row = el("div", "gnode-meta warn", `Needs your approval: ${[...new Set(waiting.values())].map(prettyToolName).join(", ")} `);
    const review = el("button", "btn small", "Review");
    review.type = "button";
    review.onclick = () => view.prompts.get(waiting.keys().next().value)?.scrollIntoView({ behavior: "smooth", block: "center" });
    row.append(review);
    lane.append(row);
  }
  if (n.status === "running" && n.activity?.length) {
    const ul = el("ul", "lane-activity");
    for (const a of n.activity.slice(-3)) ul.append(el("li", "", a));
    lane.append(ul);
  }
  if (n.error) lane.append(el("div", "gnode-err", n.error));
  if (n.changed_files?.length) lane.append(el("div", "gnode-meta", `Changed: ${n.changed_files.join(", ")}`));
  if (n.reverted_out_of_scope?.length) lane.append(el("div", "gnode-meta warn", `Reverted out of scope: ${n.reverted_out_of_scope.join(", ")}`));
  if (n.out_of_scope?.length) lane.append(el("div", "gnode-meta warn", `Changed outside its scope: ${n.out_of_scope.join(", ")}`));
  if (n.summary) {
    const details = keyed(el("details", "gnode-sum"), `us:${task.id}:${n.id}`, false, remembered);
    details.append(el("summary", "", `Summary${n.turns ? ` · ${n.turns} turns` : ""}`), el("div", "gnode-sum-body", n.summary));
    lane.append(details);
  }
  return lane;
}

function applyEvidence(task, attemptNo, disposition, evidence) {
  const e = evidence && typeof evidence === "object" ? evidence : {};
  const attempt = attemptOf(task, attemptNo);
  for (const check of Array.isArray(e.checks) ? e.checks : []) {
    if (check?.id != null) attempt.checks.set(String(check.id), { ...(attempt.checks.get(String(check.id)) || {}), ...check });
  }
  attempt.level = attempt.level || e.level || "";
  attempt.disposition = disposition || e.disposition || null;
  task.disposition = attempt.disposition;
  task.evidence = e;
  if (!task.intent && e.contract) task.intent = { status: "ready_to_lock", contract: e.contract, questions: [], warnings: [] };
}

function applyOrchestration(task, ev, ts) {
  if (task.mode !== "ultra") task.mode = "parallel";
  const at = epochMs(ts);
  const o = task.orch || (task.orch = { status: "planning", nodes: new Map(), integration: null, integrating: false, error: "" });
  const node = (id) => {
    const key = String(id);
    if (!o.nodes.has(key)) o.nodes.set(key, { id: key, objective: "", role: "", depends_on: [], write_scope: [], status: "pending" });
    return o.nodes.get(key);
  };
  const setGraph = (graph) => {
    for (const n of Array.isArray(graph?.nodes) ? graph.nodes : []) if (n?.id != null) Object.assign(node(n.id), n);
  };
  const list = (v) => (Array.isArray(v) ? v.map(String) : []);
  switch (ev.type) {
    case "plan_ready": // Ultra: the lead agent's plan, shown before dispatch
      setGraph(ev.graph);
      o.status = "planned";
      break;
    case "orchestration_started":
      o.status = "running";
      setGraph(ev.graph);
      timeline(task, `Task graph started: ${o.nodes.size} node${o.nodes.size === 1 ? "" : "s"}`);
      break;
    case "node_started":
      Object.assign(node(ev.node_id), { status: "running", startedAt: at, ...(ev.role ? { role: ev.role } : {}), ...(ev.branch ? { branch: ev.branch } : {}) });
      timeline(task, `Node ${ev.node_id} started`);
      break;
    case "node_retry": // its Claude Code process crashed: one fresh session
      Object.assign(node(ev.node_id), { status: "running", attempt: ev.attempt, activity: [] });
      timeline(task, `Node ${ev.node_id} crashed${ev.error ? ` (${ev.error})` : ""}; retrying in a fresh session${list(ev.partial_files).length ? `, partial files left: ${list(ev.partial_files).join(", ")}` : ""}`, "warn");
      break;
    case "node_progress":
      if (Array.isArray(ev.tools)) node(ev.node_id).tools = ev.tools.map(String);
      if (Array.isArray(ev.activity)) {
        const n = node(ev.node_id);
        n.activity = [...(n.activity || []), ...list(ev.activity)].slice(-10);
      }
      break;
    case "node_completed":
      Object.assign(node(ev.node_id), {
        status: "completed", summary: ev.summary || "", turns: ev.turns, cost_usd: ev.cost_usd, endedAt: at,
        reverted_out_of_scope: list(ev.reverted_out_of_scope), changed_files: list(ev.changed_files),
        out_of_scope: list(ev.out_of_scope), ...(ev.elapsed_s != null ? { elapsed_s: ev.elapsed_s } : {}),
      });
      timeline(task, `Node ${ev.node_id} completed`, "ok");
      break;
    case "node_failed":
      Object.assign(node(ev.node_id), { status: "failed", error: ev.error || "", endedAt: at });
      timeline(task, `Node ${ev.node_id} failed${ev.error ? `: ${ev.error}` : ""}`, "bad");
      break;
    case "node_blocked":
      node(ev.node_id).status = "blocked";
      timeline(task, `Node ${ev.node_id} blocked by a failed dependency`, "warn");
      break;
    case "node_cancelled":
      Object.assign(node(ev.node_id), { status: "cancelled", endedAt: at });
      break;
    case "integration_started":
      o.integrating = true;
      timeline(task, "Integrating node branches");
      break;
    case "integration_completed":
      o.integrating = false;
      o.integration = ev;
      timeline(task, `Integration ${ev.status === "conflicts" ? "finished with conflicts" : "complete"}`, ev.status === "conflicts" ? "warn" : "");
      break;
    case "integration_failed":
      o.integrating = false;
      o.integration = { status: "failed", error: ev.error || "" };
      timeline(task, `Integration failed${ev.error ? `: ${ev.error}` : ""}`, "bad");
      break;
    case "orchestration_completed":
      setGraph(ev.graph);
      if (ev.integration) o.integration = ev.integration;
      o.ignored = list(ev.ignored_files);
      o.status = ev.status || "completed";
      timeline(task, `Task graph finished: ${pretty(o.status).toLowerCase()}`, o.status === "needs_attention" ? "warn" : "");
      break;
    case "orchestration_failed":
      o.status = "failed";
      o.error = ev.error || "";
      timeline(task, `Task graph failed${ev.error ? `: ${ev.error}` : ""}`, "bad");
      break;
    case "orchestration_cancelled":
      setGraph(ev.graph);
      o.status = "cancelled";
      timeline(task, "Task graph cancelled");
      break;
    default:
      break;
  }
}

function taskTone(task) {
  const s = task.status;
  if (s === "VERIFIED" || s === "COMPLETED") return "ok";
  if (s === "FAILED_VERIFICATION" || s === "FAILED") return "bad";
  if (s === "RECOVERY_REQUIRED" || s === "BLOCKED_FOR_CLARIFICATION") return "warn";
  if (s === "CANCELLED") return "muted";
  if (!s && task.disposition) return DISPOSITIONS[task.disposition]?.[0] || "run";
  return "run";
}

function taskLabel(task) {
  if (task.status === "RECOVERY_REQUIRED") return "Needs your attention";
  return !task.status && task.disposition ? DISPOSITIONS[task.disposition]?.[1] || pretty(task.disposition) : pretty(task.status);
}

function shortTaskLabel(task) {
  const tone = taskTone(task);
  if (tone === "ok") return task.status === "COMPLETED" ? "Done" : "Verified";
  if (tone === "bad") return "Failed";
  if (tone === "muted") return "Cancelled";
  if (tone === "warn") return task.status === "BLOCKED_FOR_CLARIFICATION" ? "Input needed" : "Needs your attention";
  return "Running";
}

function pill(tone, text) {
  return el("span", `pill tone-${tone}`, text);
}

function taskLine(task) {
  if (task.disposition) {
    const reasons = task.evidence?.blocking_reasons;
    const first = Array.isArray(reasons) && reasons.length ? String(reasons[0]) : "";
    if (task.disposition === "VERIFIED") return DISPOSITIONS.VERIFIED[2];
    return first || DISPOSITIONS[task.disposition]?.[2] || "";
  }
  const attempt = [...task.attempts.values()].pop();
  if (attempt) {
    const done = [...attempt.checks.values()].filter((c) => c.status !== "running").length;
    return `Verifying (attempt ${attempt.n}): ${done}/${attempt.checks.size} checks done`;
  }
  if (task.orch) {
    const nodes = [...task.orch.nodes.values()];
    const done = nodes.filter((n) => n.status === "completed").length;
    return task.orch.integrating ? "Integrating node branches…" : `${done}/${nodes.length} nodes done`;
  }
  return task.reason || "";
}

// One compact inline card per task; heavier detail lives in the Verification panel.
function renderTaskCard(task, create) {
  if (!task.card) {
    if (!create) return;
    const root = el("div", "task-card");
    root.dataset.taskId = task.id;
    task.card = { root, head: el("div", "task-head"), line: el("div", "task-line"), contract: el("div"), clarify: el("div"), contractKey: null, clarifyKey: null };
    root.append(task.card.head, task.card.line, task.card.contract, task.card.clarify);
    view.turn = null;
    append(root);
  }
  if (task.mode === "ultra") {
    renderUltraCard(task);
    renderCardContract(task);
    renderClarify(task);
    return;
  }
  const { root, head, line } = task.card;
  const tone = taskTone(task);
  root.dataset.tone = tone;
  const icon = el("span", "task-icon");
  icon.innerHTML = '<svg viewBox="0 0 20 20" aria-hidden="true"><path d="M10 2.5l6 2.5v4.5c0 3.8-2.6 6.6-6 8-3.4-1.4-6-4.2-6-8V5z"/><path d="M7.3 10.2l2 2 3.6-4"/></svg>';
  const open = el("button", "btn small task-open", "Details");
  open.type = "button";
  open.setAttribute("aria-label", "Open this task in the Verification panel");
  open.onclick = () => revealTask(task.id);
  head.replaceChildren(icon, el("span", "task-kind", taskKind(task)), pill(tone, taskLabel(task)), open);
  line.textContent = taskLine(task);
  line.hidden = !line.textContent;
  renderCardContract(task);
  renderClarify(task);
}

function contractNode(contract, openByDefault) {
  const c = contract || {};
  const details = el("details", "contract");
  details.open = openByDefault;
  const summary = el("summary");
  summary.append(el("span", "", "Intent contract"));
  if (RISKS.includes(c.risk)) summary.append(el("span", `risk risk-${c.risk}`, `${c.risk} risk`));
  details.append(summary);
  const body = el("div", "contract-body");
  if (c.goal) body.append(el("p", "contract-goal", String(c.goal)));
  const section = (label, items, cls) => {
    const list = Array.isArray(items) ? items.filter((i) => i != null && i !== "") : [];
    if (!list.length) return;
    const box = el("div", `contract-sec${cls ? ` ${cls}` : ""}`);
    const ul = el("ul");
    for (const item of list) ul.append(el("li", "", String(item)));
    box.append(el("div", "contract-label", label), ul);
    body.append(box);
  };
  const scope = c.scope && typeof c.scope === "object" ? c.scope : {};
  const strs = (v) => (Array.isArray(v) ? v.map(String) : []);
  section("Must", c.must, "must");
  section("Must not", c.must_not, "mustnot");
  section("Preserve", c.preserve, "preserve");
  section("Scope", [
    ...strs(scope.allowed_paths).map((p) => `May change: ${p}`),
    ...strs(scope.protected_paths).map((p) => `Protected: ${p}`),
    ...strs(scope.prohibited_ops).map((p) => `Never: ${p}`),
  ]);
  section("Acceptance criteria", c.acceptance_criteria);
  if (!body.children.length) body.append(el("p", "note", "No explicit requirements were extracted."));
  details.append(body);
  return details;
}

function renderCardContract(task) {
  const box = task.card.contract;
  if (task.card.contractKey === task.intent) return;
  task.card.contractKey = task.intent;
  box.replaceChildren();
  if (!task.intent) return;
  box.append(contractNode(task.intent.contract, task.intent.status === "blocked_for_clarification"));
  if (task.intent.warnings.length) {
    const warn = el("ul", "task-warnings");
    for (const w of task.intent.warnings) warn.append(el("li", "", w));
    box.append(warn);
  }
}

function renderClarify(task) {
  const box = task.card.clarify;
  const intent = task.intent;
  const show = intent?.status === "blocked_for_clarification" && (!task.status || PRE_LOCK.has(task.status));
  const key = show ? `${task.clarifySent}|${intent.questions.join("\n")}` : "";
  if (task.card.clarifyKey === key) return;
  task.card.clarifyKey = key;
  box.replaceChildren();
  box.className = show ? "clarify" : "";
  if (!show) return;
  if (task.clarifySent) {
    box.append(el("p", "note", "Answers sent. Recompiling the contract…"));
    return;
  }
  const id = `clarify-${task.id}`;
  const title = el("h4", "", "FreeCode needs answers before locking the contract");
  title.id = `${id}-title`;
  const questions = el("ol", "clarify-questions");
  for (const q of intent.questions) questions.append(el("li", "", q));
  const area = el("textarea", "clarify-input");
  area.id = id;
  area.rows = 3;
  area.placeholder = "Answer the questions above…";
  area.setAttribute("aria-labelledby", title.id);
  area.value = task.answerDraft;
  area.oninput = () => (task.answerDraft = area.value);
  const submit = el("button", "btn primary", "Send answers");
  submit.type = "button";
  submit.onclick = () => submitClarify(task, area, submit);
  area.onkeydown = (e) => { if (e.key === "Enter" && (e.ctrlKey || e.metaKey)) submit.click(); };
  const actions = el("div", "prompt-actions");
  actions.append(submit, el("small", "note", "Ctrl+Enter to send"));
  box.append(title, questions, area, actions);
}

async function submitClarify(task, area, button) {
  const answers = area.value.trim();
  if (!answers) { area.focus(); return; }
  button.disabled = true;
  try {
    await api(`/chat/api/tasks/${encodeURIComponent(task.id)}/clarify`, { method: "POST", body: { answers } });
    task.clarifySent = true;
    task.answerDraft = "";
    timeline(task, "Clarification answers sent");
    renderTaskCard(task, false);
    scheduleVerify();
  } catch (err) {
    button.disabled = false;
    status(`Could not send answers: ${err.message}`, true);
  }
}

async function loadTaskHistory(sessionId) {
  let tasks;
  try {
    const res = await fetch(`/chat/api/sessions/${encodeURIComponent(sessionId)}/tasks`);
    if (!res.ok) return; // older server: no task history
    tasks = (await res.json()).tasks;
  } catch {
    return;
  }
  if (sessionId !== state.sessionId || !Array.isArray(tasks)) return;
  for (const record of tasks) seedTask(record);
  scheduleVerify();
}

function seedTask(record) {
  const id = record?.task_id ?? record?.id;
  if (id == null || view.tasks.has(String(id))) return; // the live replay already built it
  const task = ensureTask(String(id));
  task.mode = record.mode || "verified";
  task.status = record.status || null;
  task.reason = record.reason || "";
  if (record.contract) task.intent = { status: "ready_to_lock", contract: record.contract, questions: [], warnings: [] };
  const cp = record.checkpoint;
  if (cp) task.checkpoint = { id: cp.id, supported: cp.supported ?? cp.commit != null, head: cp.head };
  const evidence = Array.isArray(record.evidence) ? record.evidence : record.evidence ? [record.evidence] : [];
  evidence.forEach((entry, i) => {
    const pkg = entry.package || entry.evidence || entry;
    applyEvidence(task, entry.attempt ?? i + 1, entry.disposition || pkg.disposition, pkg);
  });
  timeline(task, `Saved task: ${taskLabel(task)}`, "", record.updated_at || record.created_at);
}

/* ------------------------------------------------- verification panel */

let verifyQueued = false;
function scheduleVerify() {
  if (verifyQueued) return;
  verifyQueued = true;
  requestAnimationFrame(() => {
    verifyQueued = false;
    renderVerify();
  });
}

function toggleVerify(open) {
  const app = $("app");
  const show = open ?? !app.classList.contains("show-verify");
  if (show) {
    toggleChanges(false);
    renderVerify();
  }
  app.classList.toggle("show-verify", show);
  $("verifyBtn").setAttribute("aria-expanded", String(show));
}

function revealTask(id) {
  toggleVerify(true);
  const section = [...$("verifyList").querySelectorAll("details.vtask")].find((d) => d.dataset.key === `t:${id}`);
  if (!section) return;
  section.open = true;
  section.scrollIntoView({ block: "start", behavior: "smooth" });
  section.querySelector("summary")?.focus({ preventScroll: true });
}

function keyed(details, key, openByDefault, remembered) {
  details.dataset.key = key;
  details.open = remembered.has(key) ? remembered.get(key) : openByDefault;
  return details;
}

function renderVerify() {
  const tasks = [...view.tasks.values()].reverse();
  const latest = tasks[0];
  const btn = $("verifyBtn");
  btn.hidden = !tasks.length && (state.runMode === "normal" || state.runMode === "ultra");
  $("verifyBadge").hidden = !latest;
  if (latest) {
    $("verifyBadge").dataset.tone = taskTone(latest);
    $("verifyBadgeText").textContent = shortTaskLabel(latest);
  }
  const label = latest ? `Verification: ${taskLabel(latest)}` : "Verification";
  btn.title = label;
  btn.setAttribute("aria-label", label);
  $("verifySub").textContent = tasks.length ? `${tasks.length} task${tasks.length === 1 ? "" : "s"}` : "";
  const list = $("verifyList");
  const remembered = new Map([...list.querySelectorAll("details[data-key]")].map((d) => [d.dataset.key, d.open]));
  const scroll = list.scrollTop;
  list.replaceChildren();
  if (!tasks.length) {
    list.append(el("div", "changes-empty", "Verified and Parallel runs show their contract, checks, evidence and recovery here. Pick a run mode next to the permission chip."));
    return;
  }
  tasks.forEach((task, i) => list.append(taskSection(task, i === 0, remembered)));
  list.scrollTop = scroll;
}

function taskSection(task, isLatest, remembered) {
  const section = keyed(el("details", "vtask"), `t:${task.id}`, isLatest, remembered);
  const summary = el("summary", "vtask-sum");
  summary.append(el("span", "vtask-title", taskKind(task)), el("span", "vtask-id", shortSha(task.id)), pill(taskTone(task), taskLabel(task)));
  const body = el("div", "vtask-body");
  const goal = task.intent?.contract?.goal;
  if (goal) body.append(el("p", "vtask-goal", String(goal)));
  if (task.disposition) body.append(dispositionBanner(task));
  body.append(taskActions(task));
  if (task.reverted) {
    const box = el("div", "reverted");
    box.append(el("div", "sec-title", task.reverted.length ? `Reverted ${task.reverted.length} file${task.reverted.length === 1 ? "" : "s"}` : "Nothing to revert"));
    const ul = el("ul", "mono-list");
    for (const path of task.reverted) ul.append(el("li", "", shortPath(path)));
    if (task.reverted.length) box.append(ul);
    body.append(box);
  }
  if (task.intent) body.append(keyed(contractNode(task.intent.contract, false), `c:${task.id}`, false, remembered));
  if (task.orch) body.append(graphNode(task, remembered));
  const attempts = [...task.attempts.values()];
  attempts.forEach((attempt, i) => body.append(attemptNode(task, attempt, i === attempts.length - 1, remembered)));
  const rows = task.evidence?.gap_matrix;
  if (Array.isArray(rows) && rows.length) body.append(gapMatrix(rows));
  if (task.timeline.length) {
    const details = keyed(el("details", "vsec"), `tl:${task.id}`, isLatest, remembered);
    details.append(el("summary", "sec-title", "Timeline"));
    const ol = el("ol", "timeline");
    for (const entry of task.timeline) {
      const li = el("li", entry.kind ? `tl-${entry.kind}` : "");
      const time = el("time", "", entry.time.toLocaleTimeString([], { hour: "2-digit", minute: "2-digit", second: "2-digit", hourCycle: "h23" }));
      time.dateTime = entry.time.toISOString();
      li.append(time, el("span", "", entry.text));
      ol.append(li);
    }
    details.append(ol);
    body.append(details);
  }
  section.append(summary, body);
  return section;
}

function dispositionBanner(task) {
  const [tone, label, hint] = DISPOSITIONS[task.disposition] || ["warn", pretty(task.disposition), ""];
  const banner = el("div", `banner tone-${tone}`);
  banner.setAttribute("role", "status");
  banner.append(el("strong", "", label.toUpperCase()));
  const reasons = Array.isArray(task.evidence?.blocking_reasons) ? task.evidence.blocking_reasons : [];
  if (reasons.length) {
    const ul = el("ul");
    for (const reason of reasons.slice(0, 6)) ul.append(el("li", "", String(reason)));
    banner.append(ul);
  } else if (hint) banner.append(el("div", "", hint));
  return banner;
}

function taskActions(task) {
  const row = el("div", "vtask-actions");
  const id = encodeURIComponent(task.id);
  const exportBtn = el("button", "btn small", "Export evidence");
  exportBtn.type = "button";
  exportBtn.disabled = !task.disposition;
  exportBtn.title = task.disposition ? "Open the evidence package as Markdown" : "Available after verification";
  exportBtn.onclick = () => window.open(`/chat/api/tasks/${id}/evidence?format=markdown`, "_blank", "noopener");
  const revert = el("button", "btn small danger", "Revert task changes");
  revert.type = "button";
  revert.disabled = task.checkpoint?.supported === false || task.reverting;
  revert.title = task.checkpoint?.supported === false ? "No checkpoint: this folder is not a git repository" : "Restore the files this task changed to its checkpoint";
  revert.onclick = () => revertTask(task);
  row.append(exportBtn, revert);
  if (task.status === "RECOVERY_REQUIRED") {
    const retry = el("button", "btn small primary", "Try again");
    retry.type = "button";
    retry.title = "Re-run verification, and if it still fails give FreeCode one more recovery attempt";
    retry.disabled = !!task.resuming || state.busy;
    retry.onclick = () => resumeTask(task);
    row.prepend(retry);
  }
  if (!TERMINAL_TASK.has(task.status)) {
    const cancel = el("button", "btn small", "Cancel task");
    cancel.type = "button";
    cancel.onclick = () => cancelTask(task, cancel);
    row.append(cancel);
  }
  return row;
}

async function revertTask(task) {
  if (!confirm("Revert the files this task changed back to its checkpoint? Files the task did not touch stay as they are.")) return;
  task.reverting = true;
  scheduleVerify();
  try {
    const { reverted } = await api(`/chat/api/tasks/${encodeURIComponent(task.id)}/revert`, { method: "POST" });
    task.reverted = Array.isArray(reverted) ? reverted.map(String) : [];
    timeline(task, `Reverted ${task.reverted.length} file${task.reverted.length === 1 ? "" : "s"}`, "warn");
    status(`Reverted ${task.reverted.length} file${task.reverted.length === 1 ? "" : "s"}.`);
  } catch (err) {
    status(`Revert failed: ${err.message}`, true);
  }
  task.reverting = false;
  scheduleVerify();
}

async function resumeTask(task) {
  task.resuming = true;
  scheduleVerify();
  try {
    if (!state.liveId || state.exited) await startLive();
    await api(`/chat/api/tasks/${encodeURIComponent(task.id)}/resume`, { method: "POST", body: { live_id: state.liveId } });
    timeline(task, "Trying again: one more verification and recovery attempt");
    status("Trying again…");
  } catch (err) {
    status(`Try again failed: ${err.message}`, true);
  }
  task.resuming = false;
  scheduleVerify();
}

async function cancelTask(task, button) {
  if (button) button.disabled = true;
  try {
    await api(`/chat/api/tasks/${encodeURIComponent(task.id)}/cancel`, { method: "POST" });
    status("Cancelling task…");
  } catch (err) {
    if (button) button.disabled = false;
    status(`Cancel failed: ${err.message}`, true);
  }
}

function commandText(command) {
  if (Array.isArray(command)) return command.map(String).join(" ");
  return command ? String(command) : "";
}

function durationText(check) {
  const ms = check.duration_ms ?? (check.duration_s != null ? check.duration_s * 1000 : null);
  if (ms == null || !Number.isFinite(Number(ms))) return "";
  return ms < 1000 ? `${Math.round(ms)} ms` : `${(ms / 1000).toFixed(1)} s`;
}

function attemptNode(task, attempt, isLast, remembered) {
  const details = keyed(el("details", "vsec attempt"), `a:${task.id}:${attempt.n}`, isLast, remembered);
  const checks = [...attempt.checks.values()];
  const passed = checks.filter((c) => c.status === "passed").length;
  const summary = el("summary", "sec-title");
  summary.append(el("span", "", `Attempt ${attempt.n}${attempt.level ? ` · ${attempt.level}` : ""}`), el("span", "sec-meta", `${passed}/${checks.length} passed`));
  if (attempt.disposition) {
    const [tone, label] = DISPOSITIONS[attempt.disposition] || ["warn", pretty(attempt.disposition)];
    summary.append(pill(tone, label));
  }
  details.append(summary);
  const ul = el("ul", "checks");
  for (const check of checks) {
    const st = String(check.status || "running");
    const li = el("li", `check st-${st}`);
    const icon = el("span", "check-icon", CHECK_ICONS[st] ?? "?");
    icon.setAttribute("role", "img");
    icon.setAttribute("aria-label", st === "running" ? "Running" : pretty(st));
    const head = el("div", "check-head");
    head.append(icon, el("span", "check-name", String(check.id)));
    if (check.kind && check.kind !== check.id) head.append(el("span", "tag", String(check.kind)));
    if (check.required === false) head.append(el("span", "tag", "advisory"));
    const meta = [durationText(check), check.exit_code != null ? `exit ${check.exit_code}` : ""].filter(Boolean).join(" · ");
    if (meta) head.append(el("span", "check-meta", meta));
    li.append(head);
    const cmd = commandText(check.command);
    if (cmd) li.append(el("code", "check-cmd", cmd));
    if (check.summary && st !== "passed") li.append(el("div", "check-summary", String(check.summary)));
    const tail = check.output_tail ?? check.output;
    if (tail) {
      const out = keyed(el("details", "check-out"), `o:${task.id}:${attempt.n}:${check.id}`, false, remembered);
      out.append(el("summary", "", "Output"), el("pre", "pre", String(tail)));
      li.append(out);
    }
    ul.append(li);
  }
  if (!checks.length) ul.append(el("li", "note", "No checks reported yet."));
  details.append(ul);
  return details;
}

function gapMatrix(rows) {
  const box = el("div", "vsec gap");
  box.append(el("div", "sec-title", "Gap matrix"));
  const wrap = el("div", "gap-wrap");
  const table = el("table", "gap-table");
  const head = el("tr");
  for (const h of ["Requirement", "Evidence", "Status"]) {
    const th = el("th", "", h);
    th.scope = "col";
    head.append(th);
  }
  const thead = el("thead");
  thead.append(head);
  const tbody = el("tbody");
  const tones = { covered: "ok", missing: "warn", failed: "bad" };
  for (const row of rows) {
    const tr = el("tr");
    const req = el("td");
    req.append(el("span", "req-id", `${row.requirement_id ?? ""}${row.kind ? ` · ${String(row.kind).replace(/_/g, " ")}` : ""}`), el("div", "", String(row.text ?? "")));
    const ev = el("td");
    const evidence = Array.isArray(row.evidence) ? row.evidence : [];
    if (evidence.length) {
      const ul = el("ul");
      for (const item of evidence) ul.append(el("li", "", String(item)));
      ev.append(ul);
    } else ev.append(el("span", "note", "—"));
    const st = el("td");
    st.append(pill(tones[row.status] || "muted", pretty(row.status)));
    tr.append(req, ev, st);
    tbody.append(tr);
  }
  table.append(thead, tbody);
  wrap.append(table);
  box.append(wrap);
  return box;
}

const NODE_TONES = { completed: "ok", failed: "bad", blocked: "warn", cancelled: "muted", skipped: "muted", running: "run", pending: "muted", ready: "muted" };

function graphNode(task, remembered) {
  const o = task.orch;
  const box = el("div", "vsec graph");
  const title = el("div", "sec-title");
  title.append(el("span", "", "Task graph"), pill(o.status === "failed" ? "bad" : o.status === "needs_attention" ? "warn" : o.status === "ready_for_verification" ? "ok" : o.status === "cancelled" ? "muted" : "run", pretty(o.status)));
  box.append(title);
  if (o.error) box.append(el("div", "banner tone-bad", o.error));
  // Dependency layers: a node sits one layer below its deepest dependency.
  const depth = new Map();
  const depthOf = (id, seen = new Set()) => {
    if (depth.has(id)) return depth.get(id);
    const n = o.nodes.get(id);
    if (!n || seen.has(id)) return 0; // unknown or cyclic dependency: treat as a root
    seen.add(id);
    const d = Math.max(-1, ...(n.depends_on || []).map((dep) => depthOf(String(dep), seen))) + 1;
    depth.set(id, d);
    return d;
  };
  const layers = [];
  for (const id of o.nodes.keys()) (layers[depthOf(id)] ||= []).push(o.nodes.get(id));
  layers.forEach((nodes, i) => {
    const layer = el("div", "graph-layer");
    layer.append(el("div", "layer-label", `Layer ${i + 1}`));
    const row = el("div", "layer-nodes");
    for (const n of nodes) row.append(graphCard(task, n, remembered));
    layer.append(row);
    box.append(layer);
  });
  if (!o.nodes.size) box.append(el("p", "note", "Planning the task graph…"));
  if (o.integrating || o.integration) box.append(integrationNode(o));
  return box;
}

function graphCard(task, n, remembered) {
  const waiting = n.status === "running" && n.waiting?.size ? n.waiting : null;
  const card = el("div", `gnode st-${n.status}${waiting ? " waiting" : ""}`);
  const head = el("div", "gnode-head");
  head.append(el("span", "gnode-id", n.id));
  if (n.role) head.append(el("span", "tag", n.role));
  head.append(waiting ? pill("warn", "Waiting for approval") : pill(NODE_TONES[n.status] || "muted", pretty(n.status)));
  card.append(head);
  if (waiting) {
    const row = el("div", "gnode-meta warn", `Needs your approval: ${[...new Set(waiting.values())].map(prettyToolName).join(", ")} `);
    const review = el("button", "btn small", "Review");
    review.onclick = () => view.prompts.get(waiting.keys().next().value)?.scrollIntoView({ behavior: "smooth", block: "center" });
    row.append(review);
    card.append(row);
  }
  if (n.objective) card.append(el("p", "gnode-obj", n.objective));
  const scope = Array.isArray(n.write_scope) ? n.write_scope : [];
  card.append(el("div", "gnode-meta", scope.length ? `Writes: ${scope.join(", ")}` : "Read-only"));
  if (n.depends_on?.length) card.append(el("div", "gnode-meta", `After: ${n.depends_on.join(", ")}`));
  if (n.status === "running" && n.tools?.length) card.append(el("div", "gnode-meta", `Using ${n.tools.slice(-3).join(", ")}`));
  if (n.error) card.append(el("div", "gnode-err", n.error));
  if (n.reverted_out_of_scope?.length) card.append(el("div", "gnode-meta warn", `Reverted out of scope: ${n.reverted_out_of_scope.join(", ")}`));
  if (n.summary) {
    const details = keyed(el("details", "gnode-sum"), `n:${task.id}:${n.id}`, false, remembered);
    details.append(el("summary", "", `Summary${n.turns ? ` · ${n.turns} turns` : ""}`), el("div", "gnode-sum-body", n.summary));
    card.append(details);
  }
  return card;
}

function integrationNode(o) {
  const box = el("div", "integration");
  const i = o.integration || {};
  const title = el("div", "sec-title");
  const tone = o.integrating ? "run" : i.status === "failed" ? "bad" : i.status === "conflicts" ? "warn" : "ok";
  title.append(el("span", "", "Integration"), pill(tone, o.integrating ? "Running" : pretty(i.status || "done")));
  box.append(title);
  if (i.error) box.append(el("div", "gnode-err", i.error));
  const line = (label, items) => {
    if (!items.length) return;
    const row = el("div", "int-row");
    const ul = el("ul", "mono-list");
    for (const item of items) ul.append(el("li", "", item));
    row.append(el("span", "int-label", label), ul);
    box.append(row);
  };
  const byNode = (map) => Object.entries(map && typeof map === "object" ? map : {}).flatMap(([node, files]) => (Array.isArray(files) ? files : []).map((f) => `${node}: ${f}`));
  line("Merged", Array.isArray(i.merged) ? i.merged.map(String) : []);
  line("Reverted out of scope", byNode(i.reverted_out_of_scope));
  line("Conflicts", byNode(i.conflicts));
  if (i.final_diff_stat) box.append(el("pre", "pre", String(i.final_diff_stat)));
  return box;
}

function wireTasks() {
  if (!RUN_MODES[state.runMode]) state.runMode = "normal";
  if (!STRATEGIES[state.strategy]) state.strategy = "balanced";
  renderRunMode();
  $("runModeBtn").onclick = () => {
    if (togglePopover("runModeMenu")) {
      renderRunModeMenu();
      $("runModeMenu").querySelector("[aria-checked=true]")?.focus();
    }
    syncExpanded();
  };
  $("settingsBtn").onclick = () => {
    if (togglePopover("settingsMenu")) {
      renderSettings();
      loadPresets();
      $("policySelect").focus();
    }
    syncExpanded();
  };
  $("policySelect").onchange = (e) => {
    state.policy = e.target.value;
    store.set("fcc.policy", state.policy);
    // The preset (and its permission mode) applies when this chat's next session starts.
    settingsDirty = true;
    renderSettings();
  };
  for (const [key, id] of BUDGET_FIELDS) {
    $(id).oninput = (e) => {
      const v = e.target.value.trim();
      if (v) state.budget[key] = v;
      else delete state.budget[key];
      store.set("fcc.budget", JSON.stringify(state.budget));
      settingsDirty = true;
    };
  }
  // Ultra settings ride on each message, so they never restart the session.
  $("ultraParallel").oninput = (e) => {
    const v = Math.floor(Number(e.target.value));
    if (!Number.isFinite(v) || v < 1) return;
    state.ultraParallel = Math.min(6, v);
    store.set("fcc.ultraParallel", String(state.ultraParallel));
  };
  $("ultraParallel").onblur = () => renderSettings();
  $("ultraVerify").onchange = (e) => {
    state.ultraVerify = e.target.checked;
    store.set("fcc.ultraVerify", state.ultraVerify ? "1" : "0");
    renderRunMode();
  };
  $("verifyBtn").onclick = () => toggleVerify();
  $("closeVerify").onclick = () => toggleVerify(false);
  $("healthChip").onclick = () => window.open("/admin", "_blank", "noopener");
  // After wire()'s outside-click closer and after any menu button toggled a popover.
  document.addEventListener("mousedown", syncExpanded);
  document.addEventListener("click", syncExpanded);
  // Popovers: Escape closes and returns focus; arrows move between items. Capture so the busy-Escape interrupt does not fire.
  document.addEventListener("keydown", (e) => {
    const pop = [...document.querySelectorAll(".popover")].find((p) => !p.hidden && p.contains(document.activeElement));
    if (!pop) return;
    if (e.key === "Escape") {
      e.stopPropagation();
      pop.hidden = true;
      pop.parentElement.querySelector(":scope > button")?.focus();
      syncExpanded();
    } else if ((e.key === "ArrowDown" || e.key === "ArrowUp") && document.activeElement.tagName === "BUTTON") {
      const items = [...pop.querySelectorAll("button:not([disabled])")];
      const next = items[(items.indexOf(document.activeElement) + (e.key === "ArrowDown" ? 1 : items.length - 1)) % items.length];
      e.preventDefault();
      next?.focus();
    }
  }, true);
}

/* ------------------------------------------------------------- wiring */

function applyTheme(theme) {
  if (theme === "system") document.documentElement.removeAttribute("data-theme");
  else document.documentElement.setAttribute("data-theme", theme);
  $("themeLabel").textContent = `Theme: ${theme[0].toUpperCase()}${theme.slice(1)}`;
  store.set("fcc.theme", theme);
}

const mobile = window.matchMedia("(max-width: 760px)");

function wire() {
  input.addEventListener("input", () => { autosize(); updateSuggest(); });
  input.addEventListener("keydown", (e) => {
    const slashOpen = !$("slashMenu").hidden;
    if (slashOpen && (e.key === "ArrowDown" || e.key === "ArrowUp")) {
      e.preventDefault();
      moveSuggest(e.key === "ArrowDown" ? 1 : -1);
      return;
    }
    if (slashOpen && !e.shiftKey && (e.key === "Tab" || e.key === "Enter") && suggest.items[suggest.index]) {
      e.preventDefault();
      const entry = suggest.items[suggest.index];
      if (e.key === "Enter" && suggest.kind === "slash" && input.value.trim() === `/${entry.name}`) { closeSlash(); send(); return; }
      pickSuggest(suggest.index);
      return;
    }
    if (e.key === "Escape") {
      if (slashOpen) closeSlash();
      else if (state.busy) interrupt();
      return;
    }
    if (e.key === "Tab" && e.shiftKey) {
      e.preventDefault();
      const cycle = CYCLE_MODES.filter(modeAllowed);
      if (cycle.length) setMode(cycle[(cycle.indexOf(state.mode) + 1) % cycle.length]);
      return;
    }
    if (e.key === "Enter" && !e.shiftKey && !e.isComposing) {
      e.preventDefault();
      send();
    }
  });
  input.addEventListener("paste", (e) => {
    const files = [...(e.clipboardData?.files || [])];
    if (files.length) { e.preventDefault(); addFiles(files); }
  });
  $("composer").addEventListener("dragover", (e) => e.preventDefault());
  $("composer").addEventListener("drop", (e) => { e.preventDefault(); addFiles([...(e.dataTransfer?.files || [])]); });
  $("attachBtn").onclick = () => $("fileInput").click();
  $("fileInput").onchange = (e) => { addFiles([...e.target.files]); e.target.value = ""; };
  $("sendBtn").onclick = () => (state.busy ? interrupt() : send());
  $("newChat").onclick = () => newChat();
  $("search").oninput = renderSessions;
  $("folderChip").onclick = openFolderDialog;
  $("modeBtn").onclick = () => { if (togglePopover("modeMenu")) renderModeMenu(); };
  $("modelBtn").onclick = () => {
    if (togglePopover("modelMenu")) {
      $("modelSearch").value = "";
      renderModelMenu();
      $("modelSearch").focus();
      // Pick up models added in Providers & models without a page reload.
      loadModels().then(() => { if (!$("modelMenu").hidden) renderModelMenu(); });
    }
  };
  $("setupBtn").onclick = () => window.open("/admin", "_blank", "noopener");
  // Returning from model setup: refresh so the setup card disappears once a key is saved.
  window.addEventListener("focus", () => { if (!$("setupCard").hidden) loadModels(); });
  $("modelSearch").oninput = renderModelMenu;
  $("modelSearch").onkeydown = (e) => {
    if (e.key === "Enter") $("modelList").querySelector(".menu-item")?.click();
    if (e.key === "Escape") togglePopover("modelMenu", false);
  };
  input.addEventListener("blur", closeSlash);
  document.addEventListener("mousedown", (e) => {
    if (!e.target.closest(".menu-anchor")) for (const pop of document.querySelectorAll(".popover")) pop.hidden = true;
    if (mobile.matches && !e.target.closest("#sidebar, #openSidebar")) $("app").classList.add("collapsed");
    if (mobile.matches && !e.target.closest("#changesPanel, #changesBtn")) toggleChanges(false);
    if (mobile.matches && !e.target.closest("#verifyPanel, #verifyBtn, .task-open")) toggleVerify(false);
  });
  $("sessionList").addEventListener("click", () => { if (mobile.matches) $("app").classList.add("collapsed"); });
  $("collapseSidebar").onclick = () => $("app").classList.add("collapsed");
  $("openSidebar").onclick = () => $("app").classList.remove("collapsed");
  $("chatTitle").ondblclick = async () => {
    if (!state.sessionId) return;
    const title = prompt("Rename chat", state.title)?.trim();
    if (!title) return;
    try {
      await api(`/chat/api/transcripts/${encodeURIComponent(state.sessionId)}`, { method: "PATCH", body: { title } });
      setTitle(title);
      refreshSessions();
    } catch (err) {
      status(err.message, true);
    }
  };
  const themes = ["system", "light", "dark"];
  $("themeToggle").onclick = () => applyTheme(themes[(themes.indexOf(store.get("fcc.theme", "system")) + 1) % themes.length]);
  $("changesBtn").onclick = () => toggleChanges();
  $("closeChanges").onclick = () => toggleChanges(false);
  document.addEventListener("keydown", (e) => {
    const mod = e.ctrlKey || e.metaKey;
    const key = e.key.toLowerCase();
    if (mod && !e.shiftKey && !e.altKey && key === "k") {
      e.preventDefault();
      $("app").classList.remove("collapsed");
      $("search").focus();
      $("search").select();
    } else if (mod && ((e.shiftKey && key === "o") || (e.altKey && key === "n"))) {
      e.preventDefault();
      newChat();
    } else if (e.key === "Escape" && document.activeElement === $("search")) {
      $("search").value = "";
      renderSessions();
      input.focus();
    } else if (e.key === "Escape" && state.busy && document.activeElement !== input && !$("folderDialog").open) {
      interrupt();
    }
  });
  wireTasks();
  if (mobile.matches) $("app").classList.add("collapsed");
  else if (store.get("fcc.changes", "0") === "1") toggleChanges(true);
  setInterval(refreshSessions, 15000);
}

async function main() {
  applyTheme(store.get("fcc.theme", "system"));
  loadCatalog();
  resetView();
  wire();
  renderControls();
  loadModels();
  loadHealth();
  // QA hook: drive synthetic SSE events without a backend (localStorage.fccDebug = "1").
  if (store.get("fccDebug", "0") === "1") window.__fccInject = (ev) => handleEvent(ev);
  await refreshSessions();
  await newChat();
}

main();
