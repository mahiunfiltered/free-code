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
`policy_preset` compiles `workbench.policy.compile_policy` into `--settings` and (if permission_mode not given
explicitly) the preset's permission mode. Snapshot (`fcc_state` and responses) gains `policy_preset`, `budget`,
`usage` `{input_tokens, output_tokens, turns, elapsed_s}`.

## Chat: send — `POST /chat/api/live/{live_id}/messages`
```json
{"content": "...", "mode": "normal",   // normal | verified | parallel
 "strategy": "balanced"}               // parallel only: economy | balanced | fastest
```
- `normal`: unchanged.
- `verified`: compile intent → (if blocked) emit `fcc_intent` with questions and wait for clarify → create checkpoint →
  send prompt + rendered contract → on `result` run the verification gate → VERIFIED, or recovery prompts (bounded by
  `workbench.recovery.decide`) → re-verify … → final disposition.
- `parallel`: plan task graph (planner) → run `workbench.orchestration.Orchestrator` in the session's cwd (must be a clean
  git repo, else `fcc_task` status FAILED with reason) → integration → verification gate on the integrated result.
Response: `{"ok": true, "task_id": "<id or null for normal>"}`.

## Chat: new synthetic SSE events (published on the session stream, replayable)
```
fcc_task              {task_id, mode, status, reason?}            // every lifecycle transition (workbench.tasks states)
fcc_intent            {task_id, status: ready_to_lock|blocked_for_clarification, contract, questions[], warnings[]}
fcc_checkpoint        {task_id, checkpoint_id, supported, head}
fcc_verification_started {task_id, attempt, level, checks:[{id, kind, command, required}]}
fcc_verification_check   {task_id, attempt, check:{id, kind, status: passed|failed|skipped|error|advisory_failed, duration_ms, exit_code, output_tail}}
fcc_verification      {task_id, attempt, disposition: VERIFIED|FAILED_VERIFICATION|NEEDS_REVIEW, evidence}   // evidence = EvidencePackage.to_json()
fcc_recovery          {task_id, attempt, action: retry|ask_user|give_up|none, strategy, reason}
fcc_orchestration     {task_id, event}                             // event = Orchestrator.run() event dict verbatim
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
- `GET  /chat/api/sessions/{session_id}/tasks` → `{tasks: [...]}` (so reopened chats show their verification history).

## Chat: helpers
- `GET /chat/api/policy/presets` → `{"presets":[{"id":"workspace","permission_mode":"acceptEdits","rules":["..."]}]}`
- `GET /chat/api/endpoints/summary` → `{"providers":[{"provider_id":"nvidia_nim","healthy":1,"total":2,"cooling":0,"open":1}]}`

## Admin API (in addition to the existing /admin/api/endpoints, /admin/api/usage, reset)
- `GET    /admin/api/secrets` → `{"available": true, "error": null, "names": ["nvidia"]}` (never values)
- `PUT    /admin/api/secrets/{name}` `{"value": "..."}` → `{"ok": true}`
- `DELETE /admin/api/secrets/{name}` → `{"ok": true}`
- `POST   /admin/api/secrets/migrate` → `{"migrated": ["NVIDIA_NIM_API_KEY"], "backup": "path"}`
- `GET    /admin/api/audit?limit=100&action=&actor=` → `{"verified": true, "first_bad_id": null, "records":[{id, ts, actor, action, resource, decision, outcome, payload}]}`
- `GET    /admin/api/policy/presets` → same as chat presets.
Config values that came from `vault:` references must be shown as `vault:<name>` in admin config responses, never resolved.
