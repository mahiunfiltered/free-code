# Wave 2/3 contracts (backend ⇄ chat UI ⇄ admin UI)

Backend (integration agent) implements exactly these; UI agents build against them. All routes keep the
existing loopback + same-origin guards. JSON errors use the existing shapes (`{"detail": ...}` for HTTPException,
`{"error": {"message": ...}}` for ApplicationError).

## Chat: session start — `POST /chat/api/live`
Adds optional fields (all backward compatible):
```json
{"cwd": "...", "permission_mode": "default", "model": null, "resume_session_id": null,
 "policy_preset": "workspace",            // restricted | workspace | privileged | null (null = no FCC policy)
 "budget": {"max_turns": 40, "max_minutes": 30, "max_output_tokens": 200000}}  // any may be null
```
`policy_preset` compiles `workbench.policy.compile_policy` into `--settings` and runs in the preset's permission
mode unless `permission_mode` is **stricter** (`bypassPermissions < auto < acceptEdits < default < dontAsk < plan`;
null = no preference; a looser one is ignored). The UI sends `permission_mode: null` while a preset is selected.
`set_permission_mode` to a mode looser than the preset's is refused (400). `max_minutes` may be fractional.
Snapshot (`fcc_state` and responses) gains `policy_preset`, `preset_permission_mode` (null without a preset),
`budget`, `usage` `{input_tokens, output_tokens, turns, elapsed_s}`.

## Chat: send — `POST /chat/api/live/{live_id}/messages`
```json
{"content": "...", "mode": "normal",   // normal | verified | parallel | ultra; content may hold image blocks
 "strategy": "balanced",               // parallel only: economy | balanced | fastest
 "verify": false,                      // ultra only: run the verification gate on the result
 "max_parallel": null}                 // ultra only: concurrent sub-agents, 1..6 (422 otherwise); null = 4
```
Image blocks are sent with the first Claude prompt (verified) or every node prompt (parallel).
- `normal`: unchanged.
- `verified`: compile intent → (if blocked) emit `fcc_intent` with questions and wait for clarify → create checkpoint →
  send prompt + rendered contract (+ `<verified_solution_memory>` block) → on `result` run the verification gate →
  VERIFIED, or recovery prompts (bounded by `workbench.recovery.decide`) → re-verify … → final disposition.
  Outside git, `fcc_intent.warnings` includes "not a git repository: changes can't be checkpointed, reverted or
  scope-checked; result will be NEEDS_REVIEW at best".
- `parallel`: plan task graph (planner) → run `workbench.orchestration.Orchestrator` in the session's cwd (must be a clean
  git repo, else `fcc_task` status FAILED with reason) → integration → verification gate on the integrated result.
  A protected base (`main`/`master`/`release/*`) is never committed to: integration happens on a new `fcc/<task_id>`
  branch (`orchestration_started`/`orchestration_completed` carry `working_branch`). Node sessions get the chat's
  `policy_preset` and a `--max-turns` budget.
- `ultra` (the UI default): the lead agent (`ULTRA_ANALYSIS_MODEL`, else `MODEL`, always sent as its
  `claude-3-freecc-no-thinking/...` id so reasoning is off, `max_tokens` 1500, 45 s budget) routes the message.
  `direct` (also on no model / timeout / error / single-node plan): the message goes to the chat unchanged,
  task `RECEIVED -> RUNNING -> COMPLETED` (with `verify`: a Verified run). `orchestrate`: deterministic contract
  -> checkpoint -> `fcc_ultra planned` -> 3 s countdown -> sub-agents (`Orchestrator`, `max_parallel`) ->
  apply -> the chat gets the `<fcc_ultra_report task_id="...">` prompt (request in `<user_request>`, per-node
  reports) and writes the final answer -> `COMPLETED`, or with `verify` the gate (`VERIFIED`/`RECOVERY_REQUIRED`).
  Any folder works: git (dirty ok) runs nodes in worktrees from the checkpoint snapshot and applies their diffs to
  the working tree with a per-file 3-way merge (conflicts reported, never committed, index untouched); non-git
  runs nodes in place (leases keep concurrent write scopes disjoint; changes outside the running nodes' scopes are
  reported as `out_of_scope`). Sub-agent concurrency is capped by available memory (~400 MB per agent, 1.5 GB
  headroom, at least 1). A node whose Claude Code process exits without a `result` is retried once in a fresh
  session (git: worktree reset to its start commit) with a `node_retry` event; cancel, timeouts, budgets,
  permission timeouts and error results are not retried. Tool-state paths (the verification gate's ignore
  rules: `workbench.ignore.DEFAULT_DIFF_IGNORE` + `.fcc/verify.json`) are never reverted, applied or reported
  out of scope; they are listed once as `ignored_files`. Stop = `POST /chat/api/tasks/{id}/cancel`.
Response: `{"ok": true, "task_id": "<id or null for normal>"}`.
Task status gains `COMPLETED` (terminal; "done, not verified"; Ultra only). `GET /chat/api/tasks/{id}` events include
`ultra.analysis` `{route, reason, degraded, analysis_ms, nodes}`.

## Chat: new synthetic SSE events (published on the session stream, replayable)
```
fcc_task              {task_id, mode, status, reason?}            // every lifecycle transition (workbench.tasks states)
fcc_intent            {task_id, status: ready_to_lock|blocked_for_clarification, contract, questions[], warnings[]}
fcc_checkpoint        {task_id, checkpoint_id, supported, head}
fcc_verification_started {task_id, attempt, level, checks:[{id, kind, command, required}]}
fcc_verification_check   {task_id, attempt, check:{id, kind, status: passed|failed|skipped|error|advisory_failed, duration_ms, exit_code, output_tail}}
fcc_verification      {task_id, attempt, disposition: VERIFIED|FAILED_VERIFICATION|NEEDS_REVIEW, evidence}   // evidence = EvidencePackage.to_json()
fcc_recovery          {task_id, attempt, action: retry|ask_user|give_up|none, strategy, reason}
fcc_orchestration     {task_id, event, ts}                         // event = Orchestrator.run() event dict verbatim (except node_permission*);
                                                                   // ts = epoch seconds. node_progress adds activity ["Write a.py", ...];
                                                                   // node_started adds attempt (1 | 2);
                                                                   // node_retry {node_id, attempt: 2, error, reset: worktree|none,
                                                                   //   partial_files} (crashed process, retried once);
                                                                   // node_completed adds changed_files, out_of_scope, elapsed_s;
                                                                   // orchestration_completed adds ignored_files (tool state, sorted);
                                                                   // orchestration_started adds workspace (branches|snapshot|in_place), max_parallel;
                                                                   // graph nodes add instructions, changed_files, out_of_scope
fcc_ultra             {task_id, phase, ts, ...}                    // Ultra lead agent, phases in order:
                                                                   //  analyzing {request}
                                                                   //  direct {route:"direct", reason, degraded, analysis_ms}
                                                                   //  planned {route:"orchestrate", reason, analysis_ms, plan: graph JSON,
                                                                   //           max_parallel (requested), effective_parallel (after the
                                                                   //           memory cap), parallel_limited_by: "memory"|null,
                                                                   //           workspace: snapshot|in_place, dispatch_in_s}
                                                                   //  dispatching | integrating | summarizing | verifying
                                                                   //  done {status, elapsed_ms} | failed {error, elapsed_ms} | cancelled {elapsed_ms}
fcc_node_permission   {task_id, node_id, live_id, request_id, request}   // a Parallel node's unanswered can_use_tool; live_id = the NODE session,
                                                                         // request = the original control_request.request (tool_name, input, permission_suggestions, ...)
fcc_permission_resolved {task_id, node_id, live_id, request_id, behavior: allow|deny|null}  // node prompt closed; null = cancelled/expired with the node
```
Node prompts are answered like chat prompts, on the node's session:
`POST /chat/api/live/{node live_id}/permissions/{request_id}` `{"decision": {...}}` (node sessions share
the chat registry, so the loopback/same-origin guards and the `permission.decision` audit apply; the relayed
`fcc_permission_resolved` on the chat is not audited again). A node waiting on a prompt longer than
`CoordinatorOptions.node_permission_timeout_s` (default: the node wall-time budget) fails with
`timed out waiting for approval of <tool>`.
```
fcc_budget            {kind: turns|time|tokens, limit, used, action: "interrupted"}
fcc_usage             {input_tokens, output_tokens, requests, failovers}   // after each result, from usage records for this claude session
fcc_injection         {tool_use_id, signals: [..]}                // tool_result flagged by workbench.injection.scan
```

## Chat: task routes
- `POST /chat/api/tasks/{task_id}/clarify` `{"answers": "free text"}` → recompiles, continues the verified run.
- `GET  /chat/api/tasks/{task_id}` → `{task, events[], evidence[]}` (from `workbench.tasks.TaskStore`).
- `GET  /chat/api/tasks/{task_id}/evidence?format=markdown|json` → text/markdown or JSON.
- `POST /chat/api/tasks/{task_id}/revert` → `{"reverted": ["path", ...]}` (checkpoint scoped revert; 400 if unsupported).
- `POST /chat/api/tasks/{task_id}/cancel` → `{"ok": true}`.
- `POST /chat/api/tasks/{task_id}/resume` `{"live_id": null}` (body optional) → `{"ok": true}`; RECOVERY_REQUIRED only
  (else 400): re-verify, and if still failing one recovery prompt + verify. Runs in `live_id` or the task's live chat
  (400 "Open this task's chat to resume it." when none). The UI shows RECOVERY_REQUIRED as "Needs your attention"
  with a **Try again** button.
- `POST /chat/api/tasks/{task_id}/revert` for a parallel task on its working branch resets that branch to the base
  commit (`git reset --keep`), then restores the checkpoint.
- On server start, tasks left in RECEIVED/INTENT_COMPILED/BLOCKED_FOR_CLARIFICATION/REQUIREMENTS_LOCKED/RUNNING/
  VERIFYING/RECOVERING become FAILED (FAILED_VERIFICATION → RECOVERY_REQUIRED), reason "server restarted",
  audit action `task.interrupted`.
- `GET  /chat/api/sessions/{session_id}/tasks` → `{tasks: [...]}` (so reopened chats show their verification history).

## Chat: helpers
- `GET /chat/api/policy/presets` → `{"presets":[{"id":"workspace","permission_mode":"acceptEdits","rules":["..."]}]}`
- `GET /chat/api/project?cwd=...` → `{"git": bool, "branch": str|null, "dirty": bool}` (400 if the folder is missing);
  the UI warns before a Verified send outside git and disables send in Parallel mode when not git / dirty / detached.
- `GET /chat/api/endpoints/summary` → `{"providers":[{"provider_id":"nvidia_nim","healthy":1,"total":2,"cooling":0,"open":1}]}`

## Admin API (in addition to the existing /admin/api/endpoints, /admin/api/usage, reset)
- `GET    /admin/api/secrets` → `{"available": true, "error": null, "names": ["nvidia"]}` (never values)
- `PUT    /admin/api/secrets/{name}` `{"value": "..."}` → `{"ok": true}`
- `DELETE /admin/api/secrets/{name}` → `{"ok": true}`
- `POST   /admin/api/secrets/migrate` → `{"migrated": ["NVIDIA_NIM_API_KEY"], "backup": "path"}`
- `GET    /admin/api/audit?limit=100&action=&actor=` → `{"verified": true, "first_bad_id": null, "records":[{id, ts, actor, action, resource, decision, outcome, payload}]}`
- `GET    /admin/api/policy/presets` → same as chat presets.
Config values that came from `vault:` references must be shown as `vault:<name>` in admin config responses, never resolved.
