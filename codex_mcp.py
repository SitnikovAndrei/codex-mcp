"""MCP server that makes Codex the executor for Claude Code.

Talks to a long-lived `codex app-server` (JSON-RPC over stdio). Tools:
  codex_exec          run a shell command via Codex's executor (no model)
  codex_task          hand a task to the Codex agent (sync, or async with wait=false; images allowed)
  codex_status        progress / result of a task, optionally waiting for it
  codex_answer        answer a question / form Codex raised during a turn
  codex_steer         add guidance to a running turn
  codex_interrupt     stop a running turn
  codex_review        code review by Codex (uncommitted / base branch / commit / custom)
  codex_models        available models and reasoning efforts
  codex_limits        rate-limit windows and token usage of the Codex account
  codex_threads       list saved Codex sessions
  codex_thread_read   recent turns of a session
  codex_fork          branch a session into a new thread
  codex_thread_manage rename / archive / unarchive / compact / revert / delete / goal
  codex_capabilities  MCP servers (with tools), skills, plugins available to Codex
  codex_mcp_call      call a tool of one of Codex's MCP servers directly

Register once for all projects:
  claude mcp add codex -s user -- python D:\\Tools\\codex-mcp\\codex_mcp.py
"""
import json
import os
import subprocess
import sys
import threading
import time

SERVER_VERSION = "0.4.0"
IS_WINDOWS = os.name == "nt"
MAX_OUTPUT = 20_000  # chars per text blob returned to Claude

SANDBOX_POLICY = {
    "read-only": {"type": "readOnly"},
    "workspace-write": {"type": "workspaceWrite"},
    "danger-full-access": {"type": "dangerFullAccess"},
}
EXECUTOR_INSTRUCTIONS = (
    "You are the executor. Another agent (Claude Code) planned this work and will review your result. "
    "Do exactly the task, keep changes minimal, run relevant checks, and finish with a concise report: "
    "what you changed, commands you ran and their outcome, anything left undone. "
    "If something essential is ambiguous, ask via request_user_input; Claude Code will answer."
)
# Server requests that need a decision from Claude (the brain) instead of an automatic answer.
QUESTION_METHODS = ("item/tool/requestUserInput", "mcpServer/elicitation/request")


def log(*a):
    print("[codex-mcp]", *a, file=sys.stderr, flush=True)


def clip(text, limit=MAX_OUTPUT):
    if text and len(text) > limit:
        return text[: limit // 2] + f"\n... [{len(text) - limit} chars truncated] ...\n" + text[-limit // 2 :]
    return text or ""


def fmt_time(ts):
    return time.strftime("%Y-%m-%d %H:%M", time.localtime(ts)) if ts else "?"


# --------------------------------------------------------------------------- codex app-server client


class AppServer:
    """One `codex app-server` child process shared by all tool calls."""

    def __init__(self, on_notification, on_server_request):
        self.proc = subprocess.Popen(
            # let Codex ask questions (request_user_input) outside Plan mode; they are routed to Claude
            "codex app-server --enable default_mode_request_user_input",
            shell=True,  # resolves codex.cmd / npm shims on Windows
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            text=True,
            encoding="utf-8",
            errors="replace",
            bufsize=1,
        )
        self.on_notification = on_notification
        self.on_server_request = on_server_request
        self._id = 0
        self._lock = threading.Lock()
        self._pending = {}
        self.loaded_threads = set()
        threading.Thread(target=self._read_loop, daemon=True).start()
        self.request("initialize", {
            "clientInfo": {"name": "claude-codex-mcp", "version": SERVER_VERSION},
            "capabilities": {"experimentalApi": True},  # request_user_input and other experimental bits
        })
        self._send({"method": "initialized"})

    @property
    def alive(self):
        return self.proc.poll() is None

    def _send(self, msg):
        with self._lock:
            self.proc.stdin.write(json.dumps(msg) + "\n")
            self.proc.stdin.flush()

    def respond(self, rid, result=None, error=None):
        self._send({"id": rid, **({"error": error} if error else {"result": result})})

    def request(self, method, params):
        with self._lock:
            self._id += 1
            rid = self._id
            slot = self._pending[rid] = {"event": threading.Event()}
        self._send({"id": rid, "method": method, "params": params})
        slot["event"].wait()
        msg = slot["msg"]
        if "error" in msg:
            raise RuntimeError(f"{method}: {msg['error'].get('message', msg['error'])}")
        return msg["result"]

    def _read_loop(self):
        for line in self.proc.stdout:
            try:
                msg = json.loads(line)
            except json.JSONDecodeError:
                continue
            if "method" in msg and "id" in msg:  # server -> client request
                try:
                    self.on_server_request(self, msg)
                except Exception as e:
                    self.respond(msg["id"], error={"code": -32603, "message": str(e)})
            elif "id" in msg:
                slot = self._pending.pop(msg["id"], None)
                if slot:
                    slot["msg"] = msg
                    slot["event"].set()
            elif "method" in msg:
                self.on_notification(msg["method"], msg.get("params") or {})
        log("app-server exited")
        for slot in list(self._pending.values()):
            slot["msg"] = {"error": {"message": "codex app-server exited"}}
            slot["event"].set()
        self._pending.clear()
        self.on_notification("__exit__", {})


# --------------------------------------------------------------------------- turn tracking


class Run:
    """Latest turn of a thread started through this server."""

    def __init__(self, thread_id, kind="task"):
        self.thread_id = thread_id
        self.kind = kind
        self.turn_id = None
        self.items = []
        self.turn = None
        self.question = None  # pending server request {"id", "method", "params"} awaiting codex_answer
        self.started = time.time()
        self.done = threading.Event()
        self.wake = threading.Event()  # set on completion or a new question

    @property
    def status(self):
        if self.turn:
            return self.turn["status"]
        return "waitingForAnswer" if self.question else "inProgress"

    def wait_attention(self, timeout=None):
        """Block until the turn finishes or Codex asks something (or timeout)."""
        deadline = None if timeout is None else time.time() + timeout
        while True:
            self.wake.clear()
            if self.done.is_set() or self.question:
                return
            remaining = None if deadline is None else deadline - time.time()
            if remaining is not None and remaining <= 0:
                return
            self.wake.wait(remaining)


RUNS = {}  # threadId -> Run
_server = None
_server_lock = threading.Lock()


def on_notification(method, params):
    if method == "__exit__":
        for run in RUNS.values():
            if not run.done.is_set():
                run.turn = {"status": "failed", "error": {"message": "codex app-server exited"}}
                run.question = None
                run.done.set()
                run.wake.set()
        return
    run = RUNS.get(params.get("threadId"))
    if method == "turn/started" and (not run or run.done.is_set()):
        # a turn we did not start (e.g. Codex working toward a goal): track it for codex_status
        run = RUNS[params["threadId"]] = Run(params["threadId"], "auto")
        run.turn_id = params["turn"]["id"]
        return
    if not run:
        return
    if method == "item/completed":
        run.items.append(params["item"])
    elif method == "turn/completed":
        run.turn = params["turn"]
        run.question = None
        run.done.set()
        run.wake.set()
    elif method == "serverRequest/resolved" and run.question and run.question["id"] == params.get("requestId"):
        run.question = None  # resolved elsewhere (e.g. timed out on the Codex side)


def on_server_request(srv, msg):
    # Claude is the brain, not a human at a prompt: routine approvals are auto-accepted
    # (sandbox/approval policy of the task decides the envelope); real questions go to Claude.
    method, params = msg["method"], msg.get("params") or {}
    if method in ("item/commandExecution/requestApproval", "item/fileChange/requestApproval"):
        srv.respond(msg["id"], {"decision": "accept"})
    elif method in ("execCommandApproval", "applyPatchApproval"):
        srv.respond(msg["id"], {"decision": "approved"})
    elif method == "item/permissions/requestApproval":
        srv.respond(msg["id"], {"permissions": params.get("permissions") or {}, "scope": "turn"})
    elif method in QUESTION_METHODS:
        run = RUNS.get(params.get("threadId"))
        if not run or run.done.is_set():
            if method == "mcpServer/elicitation/request":
                srv.respond(msg["id"], {"action": "decline"})
            else:
                srv.respond(msg["id"], error={"code": -32000, "message": "no active client turn to answer this"})
            return
        run.question = {"id": msg["id"], "method": method, "params": params}
        run.wake.set()
    else:
        srv.respond(msg["id"], error={"code": -32601, "message": f"{method} not supported by client"})


def app_server():
    global _server
    with _server_lock:
        if _server is None or not _server.alive:
            log("starting codex app-server")
            _server = AppServer(on_notification, on_server_request)
        return _server


def ensure_thread(srv, thread_id, cwd, overrides, ephemeral=False):
    """Load an existing thread into this app-server, or start a new one."""
    if thread_id:
        if thread_id not in srv.loaded_threads:
            srv.request("thread/resume", {"threadId": thread_id, "cwd": cwd, "excludeTurns": True, **overrides})
    else:
        params = {"cwd": cwd, "developerInstructions": EXECUTOR_INSTRUCTIONS, **overrides}
        if ephemeral:
            params["ephemeral"] = True
        thread_id = srv.request("thread/start", params)["thread"]["id"]
    srv.loaded_threads.add(thread_id)
    return thread_id


def begin_run(thread_id, kind):
    old = RUNS.get(thread_id)
    if old and not old.done.is_set():
        raise RuntimeError(f"thread {thread_id} already has a running turn; "
                           "use codex_status, codex_answer, codex_steer or codex_interrupt")
    run = RUNS[thread_id] = Run(thread_id, kind)
    return run


def finish(run, ctx, wait=True, timeout_s=None):
    """Wait for a run (or return immediately) and render a report."""
    if not wait:
        return (f"thread_id: {run.thread_id}\nturn_id: {run.turn_id}\n"
                "status: inProgress (started in background; poll with codex_status)"), False
    srv = app_server()
    ctx["cancel"] = lambda: srv.request("turn/interrupt", {"threadId": run.thread_id, "turnId": run.turn_id})
    run.wait_attention(timeout_s)
    return report(run)


def format_question(q):
    p = q["params"]
    if q["method"] == "item/tool/requestUserInput":
        lines = ["Codex asks (answer with codex_answer, answers={question_id: answer}):"]
        for question in p.get("questions", []):
            lines.append(f"- id `{question['id']}` [{question.get('header', '')}]: {question['question']}")
            for opt in question.get("options") or []:
                lines.append(f"    * {opt['label']} — {opt.get('description', '')}")
            if question.get("isOther"):
                lines.append("    * (free-text answer allowed)")
        return "\n".join(lines)
    lines = [f"MCP server `{p.get('serverName')}` (inside Codex) requests input "
             "(answer with codex_answer action=accept|decline|cancel, content={...}):",
             f"message: {p.get('message')}"]
    if p.get("mode") == "url":
        lines.append(f"url: {p.get('url')}")
    elif p.get("requestedSchema") is not None:
        lines.append("requested schema: " + clip(json.dumps(p["requestedSchema"], ensure_ascii=False), 4000))
    return "\n".join(lines)


def report(run, full=True):
    messages, commands, files, reviews = [], [], [], []
    for it in run.items:
        t = it.get("type")
        if t == "agentMessage" and it.get("text"):
            messages.append(it["text"])
        elif t == "exitedReviewMode" and it.get("review"):
            reviews.append(it["review"])
        elif t == "commandExecution":
            line = f"- `{it.get('command')}` -> exit {it.get('exitCode')}"
            if it.get("exitCode") not in (0, None):
                line += "\n" + clip(it.get("aggregatedOutput") or "", 2000)
            commands.append(line)
        elif t == "fileChange":
            for ch in it.get("changes", []):
                kind = ch.get("kind")
                kind = kind.get("type") if isinstance(kind, dict) else kind
                files.append(f"- {kind}: {ch.get('path')}")

    status = run.status
    out = [f"thread_id: {run.thread_id}", f"turn_id: {run.turn_id}", f"status: {status}",
           f"elapsed: {int(time.time() - run.started)}s"]
    if run.turn and run.turn.get("error"):
        out.append(f"error: {run.turn['error'].get('message', run.turn['error'])}")
    if run.question:
        out.append("## Question from Codex (turn is paused until answered)\n" + format_question(run.question))
    if reviews:
        out.append("## Review\n" + clip(reviews[-1]))
    finished = status not in ("inProgress", "waitingForAnswer")
    out.append(f"## {'Codex final message' if finished else 'Latest Codex message'}\n"
               + clip(messages[-1] if messages else "(none yet)"))
    if files:
        out.append("## Files changed\n" + "\n".join(dict.fromkeys(files)))
    if commands:
        shown = commands if full else commands[-5:]
        out.append(f"## Commands ({len(commands)})\n" + clip("\n".join(shown)))
    return "\n\n".join(out), status in ("failed", "interrupted")


# --------------------------------------------------------------------------- tools


def shell_argv(command):
    if IS_WINDOWS:
        return ["powershell.exe", "-NoProfile", "-NonInteractive", "-Command",
                "[Console]::OutputEncoding=[Text.Encoding]::UTF8; " + command]
    return ["bash", "-lc", command]


def cwd_of(args):
    return os.path.abspath(args.get("cwd") or os.getcwd())


def tool_codex_exec(args, ctx):
    params = {"command": shell_argv(args["command"]), "cwd": cwd_of(args)}
    if args.get("timeout_ms"):
        params["timeoutMs"] = int(args["timeout_ms"])
    if args.get("sandbox"):
        params["sandboxPolicy"] = SANDBOX_POLICY[args["sandbox"]]
    r = app_server().request("command/exec", params)
    parts = [f"exit code: {r['exitCode']}"]
    if r["stdout"]:
        parts.append("stdout:\n" + clip(r["stdout"]))
    if r["stderr"]:
        parts.append("stderr:\n" + clip(r["stderr"]))
    return "\n\n".join(parts), r["exitCode"] != 0


def image_input(ref, cwd):
    if ref.startswith(("http://", "https://", "data:")):
        return {"type": "image", "url": ref}
    return {"type": "localImage", "path": os.path.join(cwd, ref) if not os.path.isabs(ref) else ref}


def tool_codex_task(args, ctx):
    srv = app_server()
    cwd = cwd_of(args)
    overrides = {k: args[k] for k in ("model", "sandbox") if args.get(k)}
    thread_id = ensure_thread(srv, args.get("thread_id"), cwd, overrides)
    run = begin_run(thread_id, "task")

    inputs = [{"type": "text", "text": args["prompt"]}] + [image_input(i, cwd) for i in args.get("images") or []]
    params = {"threadId": thread_id, "cwd": cwd, "input": inputs}
    for arg, key in (("model", "model"), ("effort", "effort"), ("service_tier", "serviceTier"),
                     ("output_schema", "outputSchema"), ("summary", "summary")):
        if args.get(arg):
            params[key] = args[arg]
    if args.get("sandbox"):
        params["sandboxPolicy"] = SANDBOX_POLICY[args["sandbox"]]
    try:
        run.turn_id = srv.request("turn/start", params)["turn"]["id"]
    except Exception:
        RUNS.pop(thread_id, None)
        raise
    return finish(run, ctx, args.get("wait", True))


def tool_codex_status(args, ctx):
    run = RUNS.get(args["thread_id"])
    if not run:
        return "No turn started for this thread in the current server session. Use codex_thread_read for history.", True
    if args.get("wait_s"):
        run.wait_attention(float(args["wait_s"]))
    return report(run, full=run.done.is_set())


def tool_codex_answer(args, ctx):
    run = RUNS.get(args["thread_id"])
    if not run or not run.question:
        return "Codex has no pending question on this thread.", True
    q, srv = run.question, app_server()
    if q["method"] == "item/tool/requestUserInput":
        answers = args.get("answers") or {}
        if not answers:
            return "Pass answers={question_id: answer or [answers]}.", True
        result = {"answers": {qid: {"answers": a if isinstance(a, list) else [str(a)]} for qid, a in answers.items()}}
    else:
        action = args.get("action", "accept")
        result = {"action": action}
        if action == "accept":
            result["content"] = args.get("content") or {}
    run.question = None
    srv.respond(q["id"], result)
    if not args.get("wait", True):
        return "Answer delivered; Codex continues.", False
    ctx["cancel"] = lambda: srv.request("turn/interrupt", {"threadId": run.thread_id, "turnId": run.turn_id})
    run.wait_attention()
    return report(run)


def tool_codex_steer(args, ctx):
    run = RUNS.get(args["thread_id"])
    if not run or run.done.is_set():
        return "No running turn on this thread. Start a new one with codex_task(thread_id=...).", True
    app_server().request("turn/steer", {
        "threadId": run.thread_id, "expectedTurnId": run.turn_id,
        "input": [{"type": "text", "text": args["text"]}],
    })
    return f"Guidance delivered to turn {run.turn_id}.", False


def tool_codex_interrupt(args, ctx):
    run = RUNS.get(args["thread_id"])
    if not run or run.done.is_set():
        return "No running turn on this thread.", True
    app_server().request("turn/interrupt", {"threadId": run.thread_id, "turnId": run.turn_id})
    run.done.wait(30)
    return report(run)


def tool_codex_review(args, ctx):
    srv = app_server()
    cwd = cwd_of(args)
    target = args.get("target", "uncommitted")
    if target == "uncommitted":
        t = {"type": "uncommittedChanges"}
    elif target.startswith("base:"):
        t = {"type": "baseBranch", "branch": target[5:]}
    elif target.startswith("commit:"):
        t = {"type": "commit", "sha": target[7:]}
    else:
        t = {"type": "custom", "instructions": target}
    overrides = {"model": args["model"]} if args.get("model") else {}
    thread_id = ensure_thread(srv, None, cwd, {"sandbox": "read-only", **overrides})
    run = begin_run(thread_id, "review")
    try:
        run.turn_id = srv.request("review/start", {"threadId": thread_id, "target": t})["turn"]["id"]
    except Exception:
        RUNS.pop(thread_id, None)
        raise
    return finish(run, ctx, args.get("wait", True))


def tool_codex_models(args, ctx):
    data = app_server().request("model/list", {"includeHidden": bool(args.get("include_hidden"))})["data"]
    lines = []
    for m in data:
        efforts = ", ".join(e["reasoningEffort"] for e in m.get("supportedReasoningEfforts") or [])
        tiers = ", ".join(t.get("id", str(t)) if isinstance(t, dict) else str(t) for t in m.get("serviceTiers") or [])
        default = " (default)" if m.get("isDefault") else ""
        lines.append(f"- {m['id']}{default}: {m.get('description', '')}\n  effort: {efforts} "
                     f"[default {m.get('defaultReasoningEffort')}]" + (f"; tiers: {tiers}" if tiers else ""))
    return "\n".join(lines) or "(no models)", False


def fmt_window(name, w):
    if not w:
        return None
    dur = w.get("windowDurationMins")
    dur = f"{dur // 60}h" if dur and dur % 60 == 0 else (f"{dur}m" if dur else "?")
    reset = f", resets {fmt_time(w['resetsAt'])}" if w.get("resetsAt") else ""
    return f"  {name} ({dur} window): {w.get('usedPercent')}% used{reset}"


def tool_codex_limits(args, ctx):
    srv = app_server()
    out = []
    try:
        rl = srv.request("account/rateLimits/read", None)
        snaps = rl.get("rateLimitsByLimitId") or {"codex": rl["rateLimits"]}
        for lid, s in snaps.items():
            out.append(f"{s.get('limitName') or lid}" + (f" [{s['planType']}]" if s.get("planType") else "")
                       + (f" LIMIT REACHED: {s['rateLimitReachedType']}" if s.get("rateLimitReachedType") else ""))
            out += [x for x in (fmt_window("primary", s.get("primary")), fmt_window("secondary", s.get("secondary"))) if x]
            if s.get("credits"):
                out.append(f"  credits: {json.dumps(s['credits'])}")
        if rl.get("ordinaryUsageAllowed") is False:
            out.append("ordinary usage NOT allowed right now")
    except Exception as e:
        out.append(f"rate limits unavailable: {e}")
    try:
        summary = srv.request("account/usage/read", None).get("summary") or {}
        if summary:
            out.append("usage: " + ", ".join(f"{k}={v}" for k, v in summary.items() if v is not None))
    except Exception as e:
        out.append(f"usage unavailable: {e}")
    return "\n".join(out), False


def tool_codex_threads(args, ctx):
    params = {"limit": int(args.get("limit", 20)), "archived": bool(args.get("archived"))}
    if args.get("cwd"):
        params["cwd"] = os.path.abspath(args["cwd"])
    if args.get("search"):
        params["searchTerm"] = args["search"]
    data = app_server().request("thread/list", params)["data"]
    lines = []
    for t in data:
        title = t.get("name") or next(iter((t.get("preview") or "").strip().splitlines()), "")
        running = " [running]" if t["id"] in RUNS and not RUNS[t["id"]].done.is_set() else ""
        lines.append(f"- {t['id']}{running} | {fmt_time(t.get('updatedAt'))} | {t.get('model') or ''} | "
                     f"{t.get('cwd')}\n  {clip(title, 160)}")
    return "\n".join(lines) or "(no threads)", False


def tool_codex_thread_read(args, ctx):
    srv = app_server()
    turns = srv.request("thread/turns/list", {
        "threadId": args["thread_id"], "limit": int(args.get("turns", 5)), "sortDirection": "desc",
    })["data"]
    blocks = []
    for turn in reversed(turns):
        user = next((c.get("text") for it in turn.get("items", []) if it.get("type") == "userMessage"
                     for c in it.get("content", []) if c.get("type") == "text"), "")
        agent = [it["text"] for it in turn.get("items", []) if it.get("type") == "agentMessage" and it.get("text")]
        blocks.append(f"### turn {turn['id']} ({turn.get('status')}, {fmt_time(turn.get('startedAt'))})\n"
                      f"**user:** {clip(user, 3000)}\n**codex:** {clip(agent[-1] if agent else '(none)', 5000)}")
    return "\n\n".join(blocks) or "(no turns)", False


def tool_codex_fork(args, ctx):
    params = {"threadId": args["thread_id"], "excludeTurns": True}
    for k in ("cwd", "model", "sandbox"):
        if args.get(k):
            params[k] = args[k]
    if args.get("last_turn_id"):
        params["lastTurnId"] = args["last_turn_id"]
    srv = app_server()
    new_id = srv.request("thread/fork", params)["thread"]["id"]
    srv.loaded_threads.add(new_id)
    return f"forked thread_id: {new_id}\nContinue it with codex_task(thread_id=\"{new_id}\", ...).", False


def tool_codex_thread_manage(args, ctx):
    srv, tid, action = app_server(), args["thread_id"], args["action"]
    if action == "rename":
        srv.request("thread/name/set", {"threadId": tid, "name": args["name"]})
    elif action in ("archive", "unarchive", "delete"):
        srv.request(f"thread/{action}", {"threadId": tid})
        srv.loaded_threads.discard(tid)
    elif action == "compact":
        srv.request("thread/compact/start", {"threadId": tid})
    elif action == "revert":
        if not args.get("turn_id"):
            return "revert needs turn_id: that turn and every later one are dropped (see codex_thread_read).", True
        if tid not in srv.loaded_threads:
            ensure_thread(srv, tid, cwd_of(args), {})
        srv.request("thread/revert", {"threadId": tid, "beforeTurnId": args["turn_id"]})
    elif action == "goal_set":
        params = {"threadId": tid, "objective": args.get("objective"), "status": "active"}
        if args.get("token_budget"):
            params["tokenBudget"] = int(args["token_budget"])
        srv.request("thread/goal/set", params)
    elif action == "goal_clear":
        srv.request("thread/goal/clear", {"threadId": tid})
    elif action == "goal_get":
        goal = srv.request("thread/goal/get", {"threadId": tid}).get("goal")
        if not goal:
            return "no goal set", False
        return (f"objective: {goal['objective']}\nstatus: {goal['status']}\n"
                f"tokens used: {goal['tokensUsed']}" + (f" / {goal['tokenBudget']}" if goal.get("tokenBudget") else "")
                + f"\ntime used: {goal['timeUsedSeconds']}s"), False
    return f"{action}: ok", False


def tool_codex_capabilities(args, ctx):
    srv, kind, out = app_server(), args.get("kind", "mcp"), []
    if kind == "mcp":
        for s in srv.request("mcpServerStatus/list", {"detail": "toolsAndAuthOnly"})["data"]:
            if args.get("server") and s["name"] != args["server"]:
                continue
            tools = s.get("tools") or {}
            out.append(f"## {s['name']} — {len(tools)} tools (auth: {s.get('authStatus')})"
                       + (f"\n  tools error: {s['toolsError']}" if s.get("toolsError") else ""))
            if not args.get("server"):
                continue
            for name, tool in tools.items():
                desc = (tool.get("description") or "").strip().splitlines()
                out.append(f"- {name}: {clip(desc[0] if desc else '', 160)}")
    elif kind == "skills":
        for entry in srv.request("skills/list", {"cwds": [cwd_of(args)]})["data"]:
            for sk in entry.get("skills", []):
                if sk.get("enabled", True):
                    out.append(f"- {sk['name']} [{sk.get('scope')}]: {clip(sk.get('shortDescription') or sk.get('description') or '', 160)}")
    elif kind == "plugins":
        for mp in srv.request("plugin/list", {"cwds": [cwd_of(args)]})["marketplaces"]:
            for p in mp.get("plugins", []):
                mark = "enabled" if p.get("enabled") else ("installed" if p.get("installed") else "available")
                out.append(f"- {p.get('id') or p.get('name')} [{mp.get('name')}] {mark}")
    return "\n".join(out) or f"(no {kind})", False


def tool_codex_mcp_call(args, ctx):
    srv = app_server()
    thread_id = args.get("thread_id") or ensure_thread(srv, None, cwd_of(args), {}, ephemeral=True)
    if thread_id not in srv.loaded_threads:
        ensure_thread(srv, thread_id, cwd_of(args), {})
    r = srv.request("mcpServer/tool/call", {
        "threadId": thread_id, "server": args["server"], "tool": args["tool"],
        "arguments": args.get("arguments") or {},
    })
    parts = []
    for c in r.get("content") or []:
        parts.append(c.get("text") if isinstance(c, dict) and c.get("type") == "text" else json.dumps(c, ensure_ascii=False))
    if r.get("structuredContent") is not None:
        parts.append("structured: " + json.dumps(r["structuredContent"], ensure_ascii=False))
    return clip("\n".join(parts)) or "(empty result)", bool(r.get("isError"))


SANDBOX_ENUM = {"type": "string", "enum": list(SANDBOX_POLICY),
                "description": "Sandbox override. Omit to use ~/.codex/config.toml."}
CWD = {"type": "string", "description": "Absolute project directory. Always pass the current project root."}
THREAD = {"type": "string", "description": "Codex thread id (from a report or codex_threads)."}
WAIT = {"type": "boolean", "description": "true (default): block until done or Codex asks a question. "
                                          "false: return immediately; poll with codex_status."}


def schema(props, required=()):
    return {"type": "object", "properties": props, "required": list(required)}


TOOLS = {
    "codex_exec": (tool_codex_exec, {
        "description": ("Run a shell command through Codex's sandboxed executor (no LLM, instant). "
                        "PowerShell on Windows, bash elsewhere. Returns exit code, stdout, stderr."),
        "inputSchema": schema({
            "command": {"type": "string", "description": "Shell command line."},
            "cwd": CWD, "sandbox": SANDBOX_ENUM,
            "timeout_ms": {"type": "integer", "description": "Kill after this many ms."},
        }, ["command"]),
    }),
    "codex_task": (tool_codex_task, {
        "description": (
            "Delegate a well-specified task to the Codex agent, which edits files and runs commands in `cwd` "
            "on its own and returns a report (final message, changed files, commands run). "
            "If the report has status waitingForAnswer, Codex asked a question: reply with codex_answer. "
            "Pass `thread_id` to continue an existing Codex session. With wait=false it returns at once; "
            "use codex_status / codex_steer / codex_interrupt meanwhile. Independent tasks may run in parallel. "
            "Always review the resulting diff yourself."),
        "inputSchema": schema({
            "prompt": {"type": "string", "description": "Self-contained task: goal, files, constraints, acceptance checks."},
            "cwd": CWD, "thread_id": THREAD, "wait": WAIT,
            "images": {"type": "array", "items": {"type": "string"},
                       "description": "Screenshots/mockups: local paths (absolute or relative to cwd) or http(s)/data URLs."},
            "model": {"type": "string", "description": "Model id (see codex_models). Sticks for later turns of the thread."},
            "effort": {"type": "string", "description": "Reasoning effort supported by the model (see codex_models)."},
            "service_tier": {"type": "string", "description": "e.g. \"fast\" or \"default\"."},
            "summary": {"type": "string", "enum": ["auto", "concise", "detailed", "none"], "description": "Reasoning summary mode."},
            "output_schema": {"type": "object", "description": "JSON Schema the final Codex message must follow."},
            "sandbox": SANDBOX_ENUM,
        }, ["prompt"]),
    }),
    "codex_status": (tool_codex_status, {
        "description": "Progress, pending question, or final report of the latest turn on a thread started via this server.",
        "inputSchema": schema({
            "thread_id": THREAD,
            "wait_s": {"type": "number", "description": "Wait up to this many seconds for completion or a question."},
        }, ["thread_id"]),
    }),
    "codex_answer": (tool_codex_answer, {
        "description": ("Answer the question Codex raised (status waitingForAnswer). For request_user_input pass "
                        "answers={question_id: \"answer\"}; for an MCP form pass action (accept/decline/cancel) "
                        "and content matching the requested schema. By default waits for the turn again."),
        "inputSchema": schema({
            "thread_id": THREAD, "wait": WAIT,
            "answers": {"type": "object", "description": "question_id -> answer string (or list of strings)."},
            "action": {"type": "string", "enum": ["accept", "decline", "cancel"]},
            "content": {"type": "object", "description": "Form values for action=accept."},
        }, ["thread_id"]),
    }),
    "codex_steer": (tool_codex_steer, {
        "description": "Inject extra guidance into the currently running Codex turn without stopping it.",
        "inputSchema": schema({"thread_id": THREAD, "text": {"type": "string"}}, ["thread_id", "text"]),
    }),
    "codex_interrupt": (tool_codex_interrupt, {
        "description": "Stop the currently running Codex turn on a thread; returns what was done so far.",
        "inputSchema": schema({"thread_id": THREAD}, ["thread_id"]),
    }),
    "codex_review": (tool_codex_review, {
        "description": "Ask Codex for a code review (read-only sandbox, new thread). Good as a second opinion.",
        "inputSchema": schema({
            "cwd": CWD, "wait": WAIT,
            "target": {"type": "string", "description": (
                "\"uncommitted\" (default), \"base:<branch>\", \"commit:<sha>\", or free-text review instructions.")},
            "model": {"type": "string"},
        }),
    }),
    "codex_models": (tool_codex_models, {
        "description": "List Codex models with supported reasoning efforts and service tiers.",
        "inputSchema": schema({"include_hidden": {"type": "boolean"}}),
    }),
    "codex_limits": (tool_codex_limits, {
        "description": "Codex account rate-limit windows (% used, reset time) and token usage. Check before big delegations.",
        "inputSchema": schema({}),
    }),
    "codex_threads": (tool_codex_threads, {
        "description": "List saved Codex sessions (newest first), optionally filtered by project dir or title.",
        "inputSchema": schema({
            "cwd": {"type": "string", "description": "Only sessions from this directory."},
            "search": {"type": "string", "description": "Substring of the session title."},
            "limit": {"type": "integer"}, "archived": {"type": "boolean"},
        }),
    }),
    "codex_thread_read": (tool_codex_thread_read, {
        "description": "Show the last N turns of a Codex session (turn ids, user request, Codex answer).",
        "inputSchema": schema({"thread_id": THREAD, "turns": {"type": "integer", "description": "Default 5."}}, ["thread_id"]),
    }),
    "codex_fork": (tool_codex_fork, {
        "description": "Fork a Codex session into a new thread (e.g. to try an alternative approach).",
        "inputSchema": schema({
            "thread_id": THREAD, "cwd": CWD, "model": {"type": "string"}, "sandbox": SANDBOX_ENUM,
            "last_turn_id": {"type": "string", "description": "Fork only up to this turn (inclusive)."},
        }, ["thread_id"]),
    }),
    "codex_thread_manage": (tool_codex_thread_manage, {
        "description": (
            "Manage a Codex session: rename, archive, unarchive, delete (permanent), compact (summarize context), "
            "revert (drop turn_id and all later turns from history; does NOT undo file changes), "
            "goal_set / goal_get / goal_clear (persistent objective with optional token budget; goal_set makes "
            "Codex start working toward it on its own — follow with codex_status)."),
        "inputSchema": schema({
            "thread_id": THREAD,
            "action": {"type": "string", "enum": ["rename", "archive", "unarchive", "delete", "compact", "revert",
                                                  "goal_set", "goal_get", "goal_clear"]},
            "name": {"type": "string", "description": "action=rename."},
            "turn_id": {"type": "string", "description": "action=revert: first turn to drop."},
            "objective": {"type": "string", "description": "action=goal_set."},
            "token_budget": {"type": "integer", "description": "action=goal_set, optional."},
            "cwd": CWD,
        }, ["thread_id", "action"]),
    }),
    "codex_capabilities": (tool_codex_capabilities, {
        "description": "What Codex has available: kind=mcp (MCP servers + their tools), skills, or plugins.",
        "inputSchema": schema({
            "kind": {"type": "string", "enum": ["mcp", "skills", "plugins"]}, "cwd": CWD,
            "server": {"type": "string", "description": "kind=mcp: list this server's tools (otherwise only servers)."},
        }),
    }),
    "codex_mcp_call": (tool_codex_mcp_call, {
        "description": ("Call a tool of one of Codex's MCP servers directly (see codex_capabilities kind=mcp), "
                        "e.g. Codex's Slack/Calendar plugins. Uses a throwaway thread unless thread_id is given."),
        "inputSchema": schema({
            "server": {"type": "string"}, "tool": {"type": "string"},
            "arguments": {"type": "object"}, "thread_id": THREAD, "cwd": CWD,
        }, ["server", "tool"]),
    }),
}


# --------------------------------------------------------------------------- MCP stdio server

_out_lock = threading.Lock()
_active = {}  # MCP request id -> ctx


def send(msg):
    with _out_lock:
        sys.stdout.write(json.dumps({"jsonrpc": "2.0", **msg}, ensure_ascii=False) + "\n")
        sys.stdout.flush()


def run_tool(rid, name, args):
    ctx = _active[rid] = {}
    try:
        text, is_error = TOOLS[name][0](args, ctx)
    except Exception as e:  # report to Claude instead of crashing the server
        text, is_error = f"{type(e).__name__}: {e}", True
    finally:
        _active.pop(rid, None)
    send({"id": rid, "result": {"content": [{"type": "text", "text": text}], "isError": is_error}})


def handle(msg):
    method, rid, params = msg.get("method"), msg.get("id"), msg.get("params") or {}
    if method == "initialize":
        send({"id": rid, "result": {
            "protocolVersion": params.get("protocolVersion", "2025-06-18"),
            "capabilities": {"tools": {}},
            "serverInfo": {"name": "codex", "version": SERVER_VERSION},
        }})
    elif method == "tools/list":
        send({"id": rid, "result": {"tools": [{"name": n, **spec} for n, (_, spec) in TOOLS.items()]}})
    elif method == "tools/call":
        name = params.get("name")
        if name not in TOOLS:
            send({"id": rid, "error": {"code": -32602, "message": f"unknown tool {name}"}})
            return
        threading.Thread(target=run_tool, args=(rid, name, params.get("arguments") or {}), daemon=True).start()
    elif method == "notifications/cancelled":
        cancel = _active.get(params.get("requestId"), {}).get("cancel")
        if cancel:
            threading.Thread(target=cancel, daemon=True).start()
    elif method == "ping":
        send({"id": rid, "result": {}})
    elif rid is not None:
        send({"id": rid, "error": {"code": -32601, "message": f"method not found: {method}"}})


def main():
    sys.stdin.reconfigure(encoding="utf-8")
    sys.stdout.reconfigure(encoding="utf-8", newline="\n")
    for line in sys.stdin:
        if line.strip():
            try:
                handle(json.loads(line))
            except Exception as e:
                log("bad message:", e)
    if _server and _server.alive:
        _server.proc.terminate()


if __name__ == "__main__":
    main()
