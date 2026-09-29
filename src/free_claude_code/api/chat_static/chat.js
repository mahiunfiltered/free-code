"use strict";

/* Claude-desktop-style client for live Claude Code sessions served by FCC. */

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
];
const CYCLE_MODES = ["default", "acceptEdits", "plan"];

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
};

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
  };
  setEmpty(true);
  setContext(0);
  renderChanges();
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

function renderUser(message) {
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
  append(wrap);
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
    list.append(el("div", "changes-empty", "Files Claude edits in this chat show up here."));
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
  chip.title = `Context in use: ${tokens.toLocaleString()}${max ? ` of ${max.toLocaleString()}` : ""} tokens`;
}

/* -------------------------------------------------- permission prompts */

function renderPermission(event) {
  const request = event.request || {};
  const id = event.request_id;
  if (view.prompts.has(id)) return;
  const tool = request.tool_name;
  const input = request.input || {};
  const card = el("div", "prompt-card");
  view.prompts.set(id, card);
  if (tool === "AskUserQuestion") buildQuestionCard(card, id, input);
  else if (tool === "ExitPlanMode") buildPlanCard(card, id, input);
  else buildToolPermissionCard(card, id, request);
  view.turn = null;
  append(card);
  card.querySelector("button, input")?.focus({ preventScroll: true });
}

async function answer(id, decision, card, label) {
  card.querySelectorAll("button").forEach((b) => (b.disabled = true));
  try {
    await api(`/chat/api/live/${state.liveId}/permissions/${encodeURIComponent(id)}`, { method: "POST", body: { decision } });
    resolvePrompt(id, label);
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
    always.title = "Remember this permission as Claude Code suggests";
    always.onclick = () => answer(id, { behavior: "allow", updatedInput: input, updatedPermissions: suggestions }, card, "always allowed");
    actions.append(always);
  }
  const feedback = el("input", "feedback");
  feedback.placeholder = "Tell Claude what to do instead (optional)";
  const deny = el("button", "btn danger", "Deny");
  deny.onclick = () => answer(id, { behavior: "deny", message: feedback.value.trim() || "The user denied this action." }, card, "denied");
  feedback.addEventListener("keydown", (e) => { if (e.key === "Enter") deny.click(); });
  actions.append(deny, feedback);
  card.append(actions);
}

function buildQuestionCard(card, id, input) {
  const questions = input.questions || [];
  card.append(el("h4", "", "Claude has a question"));
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
  card.append(el("h4", "", "Ready to code? Here is Claude's plan"));
  const plan = el("div", "plan prose prompt-extra");
  plan.innerHTML = markdown(input.plan || "");
  card.append(plan);
  const actions = el("div", "prompt-actions");
  const setMode = (mode) => [{ type: "setMode", mode, destination: "session" }];
  const auto = el("button", "btn primary", "Yes, auto-accept edits");
  auto.onclick = () => {
    answer(id, { behavior: "allow", updatedInput: input, updatedPermissions: setMode("acceptEdits") }, card, "approved");
    applyModeLocally("acceptEdits");
  };
  const manual = el("button", "btn", "Yes, approve each edit");
  manual.onclick = () => {
    answer(id, { behavior: "allow", updatedInput: input, updatedPermissions: setMode("default") }, card, "approved");
    applyModeLocally("default");
  };
  const feedback = el("input", "feedback");
  feedback.placeholder = "Tell Claude what to change";
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
    if (!["fcc_state", "fcc_initialize", "control_request", "fcc_permission_resolved", "system"].includes(event.type)) return;
    if (event.type === "system" && event.subtype !== "init") return;
  }
  switch (event.type) {
    case "fcc_replay_end": return;
    case "fcc_state": return applyLiveState(event);
    case "fcc_initialize": return applyInitialize(event.response || {});
    case "fcc_user": setWorking(true); return renderUser(event.message || {});
    case "stream_event": return handleStreamEvent(event);
    case "assistant": return renderAssistant(event);
    case "user": return renderToolResults(event);
    case "control_request":
      if (event.request?.subtype === "can_use_tool") renderPermission(event);
      return;
    case "fcc_permission_resolved": return resolvePrompt(event.request_id, event.behavior === "deny" ? "denied" : "");
    case "result":
      setWorking(false);
      refreshSessions();
      renderResult(event);
      refreshContext();
      return;
    case "system": return handleSystem(event);
    case "fcc_exit":
      setWorking(false);
      if (event.code) notice(`Claude Code exited (code ${event.code}).${event.stderr ? `\n${event.stderr}` : ""}`, "error");
      refreshSessions();
      return;
    case "fcc_raw": return;
    default: return;
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
      permission_mode: state.mode,
      model: store.get("fcc.model", "") || null,
      resume_session_id: state.sessionId,
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
    toggle.append(box, el("span", "", `Show all Claude Code sessions${hidden ? ` (${hidden})` : ""}`));
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
  const content = state.attachments.length
    ? [...state.attachments.map((a) => ({ type: "image", source: { type: "base64", media_type: a.type, data: a.data } })), ...(text ? [{ type: "text", text }] : [])]
    : text;
  input.value = "";
  state.attachments = [];
  renderAttachments();
  autosize();
  closeSlash();
  try {
    if (!state.liveId || state.exited) await startLive();
    if (state.title === "New chat" && text) setTitle(text.slice(0, 60));
    state.busy = true;
    renderControls();
    await api(`/chat/api/live/${state.liveId}/messages`, { method: "POST", body: { content } });
  } catch (err) {
    state.busy = false;
    renderControls();
    status(err.message, true);
    if (typeof content === "string") input.value = content;
  }
}

async function interrupt() {
  if (!state.liveId) return;
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
    line.textContent = "Claude can make mistakes. Review changes before shipping.";
    line.style.color = "";
  }, isError ? 8000 : 3000);
}

function setTitle(title) {
  state.title = title || "New chat";
  $("chatTitle").textContent = state.title;
  document.title = state.title === "New chat" ? "Claude Code" : `${state.title} — Claude Code`;
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
  $("folderChip").title = state.cwd;
  store.set("fcc.cwd", state.cwd);
  store.set("fcc.mode", state.mode === "bypassPermissions" ? "default" : state.mode);
}

// Claude reports the resolved upstream id (e.g. "nvidia_nim/x/y") while menus list gateway aliases.
const findModel = (id) => id && state.models.find((m) => m.value === id || m.label === id || m.value === `anthropic/${id}`);
// Claude's own aliases (and the ids they resolve to) mean "whatever the server routes MODEL to".
const isServerDefault = (id) => !id || /^(default|sonnet|haiku|opus|fable|claude-)/i.test(id);
const defaultModel = () => state.models.find((m) => m.default) || null;
const currentModel = () => findModel(state.model) || (isServerDefault(state.model) ? defaultModel() : null);

function applyModeLocally(mode) {
  state.mode = mode;
  renderControls();
}

async function setMode(mode) {
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
    await api(`/chat/api/live/${state.liveId}/control`, { method: "POST", body: { request: { subtype: "set_model", model: model || "default" } } });
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
    item.append(el("small", "", mode.hint));
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
      const idx = CYCLE_MODES.indexOf(state.mode);
      setMode(CYCLE_MODES[(idx + 1) % CYCLE_MODES.length]);
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
  await refreshSessions();
  await newChat();
}

main();
