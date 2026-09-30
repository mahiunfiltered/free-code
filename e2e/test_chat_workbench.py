"""Rendered chat UI: policy-preset permission mode, git preflight, budgets, Try again,
and Ultra mode (default run mode, plan countdown, agent lanes, Stop).

Every /chat/api route is stubbed per test, so these cover only the browser side of
the contracts in docs/m0005-port/CONTRACTS.md.
"""

import json
import re
import time

from playwright.sync_api import Page, Request, Route, expect

LIVE: dict[str, object] = {
    "live_id": "L1",
    "session_id": None,
    "cwd": "/proj",
    "model": None,
    "permission_mode": "default",
    "busy": False,
    "exited": False,
    "has_messages": False,
    "policy_preset": None,
    "preset_permission_mode": None,
    "budget": None,
    "usage": {"input_tokens": 0, "output_tokens": 0, "turns": 0, "elapsed_s": 0},
}


def _json(route: Route, payload: object, status: int = 200) -> None:
    route.fulfill(
        status=status, content_type="application/json", body=json.dumps(payload)
    )


def _body(request: Request) -> dict[str, object]:
    body = request.post_data_json
    assert isinstance(body, dict)
    return body


def _open(
    page: Page,
    base_url: str,
    *,
    storage: dict[str, str],
    live: dict[str, object] | None = None,
    project: dict[str, object] | None = None,
) -> list[dict[str, object]]:
    """Stub the chat backend, load /chat; returns the POST /chat/api/live bodies."""

    starts: list[dict[str, object]] = []

    def start(route: Route, request: Request) -> None:
        if request.method != "POST":
            return _json(route, {"ok": True})
        starts.append(_body(request))
        _json(route, {**LIVE, **(live or {})})

    page.add_init_script(
        "for (const [k, v] of Object.entries("
        + json.dumps({"fcc.cwd": "/proj", "fccDebug": "1", **storage})
        + ")) localStorage.setItem(k, v);"
    )
    page.route(
        "**/chat/api/sessions",
        lambda route: _json(route, {"home": "/home", "transcripts": [], "live": []}),
    )
    page.route("**/chat/api/models", lambda route: _json(route, {"models": []}))
    page.route(
        "**/chat/api/endpoints/summary", lambda route: _json(route, {"providers": []})
    )
    page.route(
        "**/chat/api/policy/presets", lambda route: _json(route, {"presets": []})
    )
    page.route(
        re.compile(r"/chat/api/project\?"),
        lambda route: _json(
            route, project or {"git": True, "branch": "feat", "dirty": False}
        ),
    )
    page.route(
        "**/chat/api/live/*/events",
        lambda route: route.fulfill(
            status=200,
            content_type="text/event-stream",
            body='data: {"type": "fcc_replay_end"}\n\n',
        ),
    )
    page.route("**/chat/api/live", start)
    page.route("**/chat/api/live/*", lambda route: _json(route, {"ok": True}))
    page.set_viewport_size({"width": 1280, "height": 800})
    page.goto(f"{base_url}/chat")
    expect(page.locator("#modeBtn")).not_to_have_text("")
    page.wait_for_function("() => document.querySelector('#folderLabel').textContent")
    return starts


def test_preset_mode_is_sent_as_null_and_shown_from_the_snapshot(
    page: Page, admin_base_url: str
) -> None:
    starts = _open(
        page,
        admin_base_url,
        storage={"fcc.policy": "restricted", "fcc.mode": "acceptEdits"},
        live={
            "permission_mode": "dontAsk",
            "policy_preset": "restricted",
            "preset_permission_mode": "dontAsk",
        },
    )

    expect(page.locator("#modeBtn")).to_have_text("Don't ask (read-only)")
    assert starts[0]["permission_mode"] is None
    assert starts[0]["policy_preset"] == "restricted"
    page.locator("#modeBtn").click()
    menu = page.locator("#modeMenu")
    expect(
        menu.get_by_role("button", name=re.compile("^Auto-accept edits"))
    ).to_be_disabled()
    expect(menu.get_by_role("button", name=re.compile("^Plan mode"))).to_be_enabled()


def test_without_a_preset_the_chosen_mode_is_sent(
    page: Page, admin_base_url: str
) -> None:
    starts = _open(page, admin_base_url, storage={"fcc.mode": "plan"})
    expect(page.locator("#modeBtn")).to_have_text("Ask permissions")  # from snapshot
    assert starts[0]["permission_mode"] == "plan"


def test_verified_mode_warns_outside_git(page: Page, admin_base_url: str) -> None:
    _open(
        page,
        admin_base_url,
        storage={"fcc.runModeV2": "verified"},
        project={"git": False, "branch": None, "dirty": False},
    )

    warning = page.locator("#projectWarning")
    expect(warning).to_be_visible()
    expect(warning).to_contain_text("Not a git repository")
    expect(page.locator("#sendBtn")).to_be_enabled()


def test_parallel_mode_blocks_send_on_a_dirty_tree(
    page: Page, admin_base_url: str
) -> None:
    sent: list[Request] = []
    _open(
        page,
        admin_base_url,
        storage={"fcc.runModeV2": "parallel"},
        project={"git": True, "branch": "main", "dirty": True},
    )
    page.route(re.compile(r"/chat/api/live/L1/messages"), lambda r, q: sent.append(q))

    warning = page.locator("#projectWarning")
    expect(warning).to_contain_text("clean git tree")
    expect(page.locator("#sendBtn")).to_be_disabled()
    page.locator("#input").fill("split this work")
    page.locator("#input").press("Enter")
    expect(page.locator("#statusLine")).to_contain_text("clean git tree")
    assert sent == []


def test_fractional_minute_budget_is_sent(page: Page, admin_base_url: str) -> None:
    starts = _open(page, admin_base_url, storage={})
    page.locator("#settingsBtn").click()
    page.locator("#budgetMinutes").fill("0.5")
    page.locator("#budgetTurns").fill("12")
    with page.expect_request(
        lambda r: r.method == "POST" and r.url.endswith("/chat/api/live")
    ) as restart:
        page.locator("#settingsBtn").click()  # closing restarts the unused session

    assert starts[0]["budget"] is None
    assert _body(restart.value)["budget"] == {
        "max_turns": 12,
        "max_minutes": 0.5,
        "max_output_tokens": None,
    }


def test_try_again_resumes_a_task_that_needs_attention(
    page: Page, admin_base_url: str
) -> None:
    resumed: list[dict[str, object]] = []

    def resume(route: Route, request: Request) -> None:
        resumed.append(_body(request))
        _json(route, {"ok": True})

    _open(page, admin_base_url, storage={})
    page.route("**/chat/api/tasks/T1/resume", resume)
    page.evaluate(
        """() => {
          window.__fccInject({type: "fcc_task", task_id: "T1", mode: "verified", status: "RECEIVED"});
          window.__fccInject({type: "fcc_task", task_id: "T1", mode: "verified", status: "RECOVERY_REQUIRED", reason: "needs review"});
        }"""
    )
    page.locator("#verifyBtn").click()
    panel = page.locator("#verifyList")
    expect(panel).to_contain_text("Needs your attention")
    panel.get_by_role("button", name="Try again").click()

    expect(page.locator("#statusLine")).to_contain_text("Trying again")
    assert resumed == [{"live_id": "L1"}]


def test_node_permission_card_posts_to_the_node_session(
    page: Page, admin_base_url: str
) -> None:
    answers: list[tuple[str, dict[str, object]]] = []

    def permission(route: Route, request: Request) -> None:
        answers.append((request.url, _body(request)))
        _json(route, {"ok": True})

    _open(page, admin_base_url, storage={})
    page.route("**/chat/api/live/*/permissions/*", permission)
    page.evaluate(
        """() => {
          const graph = {task_id: "T2", nodes: [{id: "impl", objective: "do it", role: "implementation",
            depends_on: [], write_scope: ["**"], status: "pending"}]};
          window.__fccInject({type: "fcc_task", task_id: "T2", mode: "parallel", status: "RUNNING"});
          window.__fccInject({type: "fcc_orchestration", task_id: "T2",
            event: {type: "orchestration_started", task_id: "T2", graph}});
          window.__fccInject({type: "fcc_orchestration", task_id: "T2",
            event: {type: "node_started", node_id: "impl", live_id: "N1"}});
          window.__fccInject({type: "fcc_node_permission", task_id: "T2", node_id: "impl",
            live_id: "N1", request_id: "perm_Bash",
            request: {subtype: "can_use_tool", tool_name: "Bash", input: {command: "echo hi"},
              permission_suggestions: []}});
        }"""
    )

    card = page.locator(".prompt-card", has_text="Step impl wants to run")
    expect(card).to_be_visible()
    page.locator("#verifyBtn").click()
    node = page.locator("#verifyList .gnode.waiting")
    expect(node).to_contain_text("Waiting for approval")
    expect(node).to_contain_text("Needs your approval")

    card.get_by_role("button", name="Allow once").click()
    expect(card).to_have_class(re.compile(r"\bresolved\b"))
    [(url, body)] = answers
    assert url.endswith("/chat/api/live/N1/permissions/perm_Bash")  # not L1
    decision = body["decision"]
    assert isinstance(decision, dict) and decision["behavior"] == "allow"

    page.evaluate(
        """() => window.__fccInject({type: "fcc_permission_resolved", task_id: "T2",
          node_id: "impl", live_id: "N1", request_id: "perm_Bash", behavior: "allow"})"""
    )
    expect(page.locator("#verifyList .gnode.waiting")).to_have_count(0)
    expect(page.locator("#verifyList .gnode")).to_contain_text("Running")


# ----- Ultra mode ------------------------------------------------------------

PLAN = {
    "task_id": "U1",
    "nodes": [
        {
            "id": "strings",
            "role": "implementation",
            "objective": "Create strings_util.py with slugify",
            "instructions": "Write strings_util.py with slugify(text) and tests/test_strings.py",
            "write_scope": ["strings_util.py", "tests/test_strings.py"],
            "depends_on": [],
            "status": "pending",
        },
        {
            "id": "maths",
            "role": "implementation",
            "objective": "Create math_util.py with clamp",
            "instructions": "Write math_util.py with clamp(x, lo, hi)",
            "write_scope": ["math_util.py"],
            "depends_on": [],
            "status": "pending",
        },
    ],
}


def _inject(page: Page, *events: dict) -> None:
    page.evaluate(
        "(events) => events.forEach((e) => window.__fccInject(e))", list(events)
    )


def _send(page: Page, sent: list[dict[str, object]], text: str) -> None:
    def messages(route: Route, request: Request) -> None:
        sent.append(_body(request))
        _json(route, {"ok": True, "task_id": "U1"})

    page.route(re.compile(r"/chat/api/live/L1/messages$"), messages)
    page.locator("#input").fill(text)
    page.locator("#input").press("Enter")
    expect(page.locator(".task-card.ultra-card")).to_be_visible()


def test_ultra_sends_its_settings(page: Page, admin_base_url: str) -> None:
    sent: list[dict[str, object]] = []
    _open(page, admin_base_url, storage={"fcc.runModeV2": "ultra"})
    expect(page.locator("#runModeBtn")).to_have_text("Ultra")
    expect(page.locator("#projectWarning")).to_be_hidden()

    page.locator("#settingsBtn").click()
    page.locator("#ultraParallel").fill("2")
    page.locator("#ultraVerify").check()
    page.locator("#settingsBtn").click()
    expect(page.locator("#runModeBtn")).to_have_text("Ultra · Verified")

    _send(page, sent, "Create three modules")
    assert sent == [
        {
            "content": "Create three modules",
            "mode": "ultra",
            "verify": True,
            "max_parallel": 2,
        }
    ]
    # The request shows at once, and its direct-run echo is not shown twice.
    expect(page.locator(".msg-user")).to_have_count(1)
    _inject(
        page,
        {"type": "fcc_ultra", "task_id": "U1", "phase": "analyzing", "ts": time.time()},
        {
            "type": "fcc_user",
            "message": {"role": "user", "content": "Create three modules"},
        },
    )
    expect(page.locator(".msg-user")).to_have_count(1)


def test_plan_countdown_lanes_report_and_completion(
    page: Page, admin_base_url: str
) -> None:
    sent: list[dict[str, object]] = []
    _open(page, admin_base_url, storage={"fcc.runModeV2": "ultra"})
    _send(page, sent, "Create two modules")
    now = time.time()
    _inject(
        page,
        {"type": "fcc_task", "task_id": "U1", "mode": "ultra", "status": "RECEIVED"},
        {"type": "fcc_ultra", "task_id": "U1", "phase": "analyzing", "ts": now},
        {
            "type": "fcc_ultra",
            "task_id": "U1",
            "phase": "planned",
            "ts": now,
            "route": "orchestrate",
            "reason": "two independent modules",
            "analysis_ms": 1234,
            "plan": PLAN,
            "max_parallel": 4,
            "workspace": "snapshot",
            "dispatch_in_s": 30,
        },
    )
    card = page.locator(".ultra-card")
    expect(card.locator(".task-head .pill")).to_have_text("Plan ready")
    expect(card).to_contain_text("two independent modules")
    expect(card.locator(".ultra-countdown")).to_contain_text(
        re.compile(r"Starting 2 sub-agents \(4 at once\) in \d+s")
    )
    expect(card).to_contain_text("analysis 1.2s")
    lanes = card.locator(".ultra-lanes .lane")
    expect(lanes).to_have_count(2)
    brief = lanes.first.locator("details.lane-brief")
    brief.locator("summary").click()
    expect(brief).to_contain_text("slugify(text) and tests/test_strings.py")

    _inject(
        page,
        {"type": "fcc_ultra", "task_id": "U1", "phase": "dispatching", "ts": now + 1},
        {
            "type": "fcc_orchestration",
            "task_id": "U1",
            "ts": now + 1,
            "event": {"type": "orchestration_started", "task_id": "U1", "graph": PLAN},
        },
        {
            "type": "fcc_orchestration",
            "task_id": "U1",
            "ts": now + 1,
            "event": {"type": "node_started", "node_id": "strings", "live_id": "N1"},
        },
        {
            "type": "fcc_orchestration",
            "task_id": "U1",
            "ts": now + 2,
            "event": {
                "type": "node_progress",
                "node_id": "strings",
                "tools": ["Write"],
                "activity": ["Write strings_util.py"],
            },
        },
    )
    expect(card.locator(".task-head .pill")).to_have_text("Sub-agents working")
    expect(card.locator(".ultra-countdown")).to_have_count(0)
    running = lanes.filter(has_text="strings_util.py with slugify")
    expect(running.locator(".lane-activity")).to_contain_text("Write strings_util.py")
    expect(running.locator(".pill")).to_have_text("Running")
    expect(card.locator(".ultra-steps li.current")).to_have_text("Dispatch")

    _inject(
        page,
        {
            "type": "fcc_orchestration",
            "task_id": "U1",
            "ts": now + 5,
            "event": {
                "type": "node_completed",
                "node_id": "strings",
                "summary": "slugify done",
                "changed_files": ["strings_util.py"],
                "out_of_scope": [],
                "reverted_out_of_scope": [],
                "elapsed_s": 4.0,
            },
        },
        {"type": "fcc_ultra", "task_id": "U1", "phase": "integrating", "ts": now + 6},
        {
            "type": "fcc_orchestration",
            "task_id": "U1",
            "ts": now + 6,
            "event": {
                "type": "integration_completed",
                "status": "conflicts",
                "merged": ["strings"],
                "conflicts": {"maths": ["math_util.py"]},
            },
        },
        {"type": "fcc_ultra", "task_id": "U1", "phase": "summarizing", "ts": now + 7},
        {
            "type": "fcc_user",
            "message": {
                "role": "user",
                "content": '<fcc_ultra_report task_id="U1">\n<user_request>\n'
                "Create two modules\n</user_request>\n\nYou are the lead agent...",
            },
        },
    )
    done_lane = lanes.filter(has_text="strings_util.py with slugify")
    expect(done_lane).to_contain_text("Changed: strings_util.py")
    expect(done_lane).to_contain_text("4s")
    expect(card).to_contain_text("Conflicts (not applied): maths: math_util.py")
    # The lead-agent prompt is a collapsed report chip, not a second request bubble.
    expect(page.locator(".msg-user")).to_have_count(1)
    expect(page.locator("details.ultra-report")).to_contain_text(
        "Sub-agent reports sent to the lead agent"
    )

    _inject(
        page,
        {"type": "fcc_task", "task_id": "U1", "mode": "ultra", "status": "COMPLETED"},
        {
            "type": "fcc_ultra",
            "task_id": "U1",
            "phase": "done",
            "status": "COMPLETED",
            "ts": now + 9,
        },
    )
    expect(card.locator(".task-head .pill")).to_have_text("Completed")
    expect(card.locator(".ultra-stop")).to_have_count(0)
    expect(page.locator("#sendBtn")).not_to_have_class(re.compile(r"\bstop\b"))


def test_stop_cancels_the_ultra_task_and_node_prompts_still_work(
    page: Page, admin_base_url: str
) -> None:
    cancelled: list[str] = []
    answers: list[str] = []
    sent: list[dict[str, object]] = []
    _open(page, admin_base_url, storage={"fcc.runModeV2": "ultra"})
    page.route(
        "**/chat/api/tasks/U1/cancel",
        lambda route, request: (
            cancelled.append(request.url),
            _json(route, {"ok": True}),
        ),
    )
    page.route(
        "**/chat/api/live/*/permissions/*",
        lambda route, request: (
            answers.append(request.url),
            _json(route, {"ok": True}),
        ),
    )
    _send(page, sent, "Split this work")
    now = time.time()
    _inject(
        page,
        {"type": "fcc_ultra", "task_id": "U1", "phase": "dispatching", "ts": now},
        {
            "type": "fcc_orchestration",
            "task_id": "U1",
            "ts": now,
            "event": {"type": "orchestration_started", "task_id": "U1", "graph": PLAN},
        },
        {
            "type": "fcc_orchestration",
            "task_id": "U1",
            "ts": now,
            "event": {"type": "node_started", "node_id": "maths", "live_id": "N2"},
        },
        {
            "type": "fcc_node_permission",
            "task_id": "U1",
            "node_id": "maths",
            "live_id": "N2",
            "request_id": "perm_Bash",
            "request": {
                "subtype": "can_use_tool",
                "tool_name": "Bash",
                "input": {"command": "pytest -q"},
                "permission_suggestions": [],
            },
        },
    )
    card = page.locator(".ultra-card")
    expect(card.locator(".lane.waiting")).to_contain_text("Waiting for approval")
    page.locator(".prompt-card", has_text="Step maths wants to run").get_by_role(
        "button", name="Allow once"
    ).click()
    expect(page.locator(".prompt-card.resolved")).to_have_count(1)
    assert answers and answers[0].endswith("/chat/api/live/N2/permissions/perm_Bash")

    card.locator(".ultra-stop").click()
    expect(page.locator("#statusLine")).to_contain_text("Cancelling task")
    page.locator("#sendBtn").click()  # the composer's Stop also cancels the run
    page.wait_for_timeout(200)
    assert len(cancelled) == 2
