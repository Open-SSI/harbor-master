"""A run: its state, its ledger shard, and the scheduler that feeds the working cells.

DESIGN.md Sections 6 (cells and parallelism) and 11 (requests). The work itself is in
`shared.py` (once per benchmark) and `stages.py` (once per task).
"""
from __future__ import annotations

import asyncio
import contextlib
import json
import time
from collections import Counter, deque
from pathlib import Path

from ..backends import make_backend
from ..core.config import LOCAL, Config
from ..core.config import simulated as simulated_dir
from ..core.identity import Identity
from ..ledger import REPORTS, Ledger, Shard, Sync, active, fields_of, load_contributors, write_index
from ..tasks import Task
from . import shared, stages
from .state import BENCH, STAGE, STAGES, BenchState, CellState, Request


class Session:
    def __init__(self, workspace: Path, cfg: Config, ident: Identity, benchmarks: dict[str, dict],
                 simulate: bool = False, speed: float = 1.0, no_input: bool = False, run_id: str | None = None,
                 command: str = "", until: str = "", assume_after: str = "",
                 only: list[str] | None = None, as_real: bool = False):
        self.ws, self.cfg, self.ident, self.no_input, self.command = workspace, cfg, ident, no_input, command
        self.only = set(only or [])      # when set, run only the tasks with these names
        self.until = until           # stop each task after this stage: "verify" or "probe". Empty runs them all
        self.run_id = run_id or time.strftime("%Y%m%d-%H%M%S")
        self.run_dir = workspace / LOCAL / "runs" / self.run_id
        self.run_dir.mkdir(parents=True, exist_ok=True)
        # A simulated run keeps its records on this machine, marked, and never in the index's ledger.
        # `as_real` is for tests of sharing: it lets a simulated backend stand in for a real one.
        self.sandbox = simulate and not as_real
        self.ledger_dir = simulated_dir(workspace) if self.sandbox else workspace
        self.ledger = Ledger(self.ledger_dir, simulated=self.sandbox)
        self.protocol = cfg.protocol
        self._file_protocol, self._config_changed, self._config_checked = self.protocol, False, time.time()
        self.calls: Counter[str] = Counter()
        self._keeping: list = []            # accepted tasks on their way into the task store
        self.usage_by: dict[tuple[str, str, str], list] = {}        # (benchmark, stage, tier) -> [calls, tokens, cost]
        self._usage_written: dict[tuple[str, str, str], tuple] = {}
        self.cost_by_tier: Counter[str] = Counter()
        self.trial_cost = 0.0
        self.backend = make_backend(cfg, self._usage, simulate, speed, self.run_dir, assume_after)
        self.assumed: set[str] = getattr(self.backend, "assumed", set())    # stages whose results are assumed, not run
        self.ledger.accept_assumed = bool(self.assumed)     # a real run never reuses a made-up result
        self.sync = Sync(self.ledger_dir, ident.name, ident.github)
        self._sync_lock = asyncio.Lock()
        self.simulate = simulate
        self.benches = {b: BenchState(b, info.get("field", "general"), Path(info["path"]) if info.get("path") else Path(),
                                   info.get("released", ""), info.get("license", ""), entry=info.get("entry"))
                        for b, info in benchmarks.items()}
        self.cells = [CellState(i + 1) for i in range(cfg.run.cells)]
        self.active_cells = cfg.run.cells
        self.queue: list[Task] = []
        self.recent: deque[tuple[float, str, str, str]] = deque(maxlen=200)
        self.requests: list[Request] = []
        self.blackboards: dict[str, dict] = {}
        self.durations: dict[tuple[str, int], list[float]] = {}
        self.notes: dict[str, list[str]] = {}
        self.budget = cfg.run.budget_usd
        self.paused = self.quitting = self.finished = False
        self.leaving = False            # the user asked to leave the screen: the screen does it and clears this
        self.started = time.time()
        self.containers = asyncio.Semaphore(cfg.run.max_containers)
        self.containers_used = 0
        self._shards: dict[str, Shard] = {}
        self._request_ids = 0
        self._workers: list[asyncio.Task] = []
        self._shared_tasks: list[asyncio.Task] = []
        self.undo_stack: list[tuple[str, object]] = []
        self.on_recent = None        # called with each new outcome, for plain output
        self.ready: list[Task] = []  # reports waiting for this user's verdict
        people = load_contributors(workspace)
        self.is_reviewer = any(c["github"] == ident.github for c in active(people))
        self.is_owner = any(c.get("github") == ident.github and c.get("role") == "owner" and c.get("key") for c in people)
        self.fields = fields_of(people, ident.github)     # None: every field. A set: only these
        self.claimed: dict[str, list[str]] = {}            # benchmark -> the task ids this run holds

    def _usage(self, tier: str, cost: float, tokens: int) -> None:
        self.calls[tier] += 1
        self.cost_by_tier[tier] += cost
        slot = self.usage_by.setdefault((BENCH.get(), STAGE.get(), tier), [0, 0, 0.0])
        slot[0], slot[1], slot[2] = slot[0] + 1, slot[1] + int(tokens or 0), slot[2] + cost

    def flush_usage(self, benchmark: str | None = None) -> None:
        """Record what the model tiers were called for since the last time: one record per benchmark, with a
        row per stage and tier. The rows add up, so a run that is cut short loses at most its last stretch."""
        rows: dict[str, list[dict]] = {}
        for (bench, stage, tier), (calls, tokens, cost) in sorted(self.usage_by.items()):
            done = self._usage_written.get((bench, stage, tier), (0, 0, 0.0))
            if (benchmark is None or bench == benchmark) and calls > done[0]:
                model = self.cfg.tier(tier).model if tier in ("decision", "worker", "frontier", "chat") else ""
                rows.setdefault(bench, []).append({"stage": stage, "tier": tier, "model": model, "calls": calls - done[0],
                                                   "tokens": tokens - done[1], "spent": round(cost - done[2], 6)})
                self._usage_written[(bench, stage, tier)] = (calls, tokens, cost)
        for bench, found in rows.items():
            self.record(bench, "usage", rows=found)

    def note_skip(self, bench: BenchState) -> None:
        """Record that this run left a benchmark out, and why. The same reason twice in a row is recorded once."""
        last = next((e for e in reversed(self.ledger.events) if e["type"] == "skip" and e["benchmark"] == bench.id
                     and e["runner"] == self.ident.github), None)
        if last is None or (last.get("value"), last.get("reason")) != (bench.skip or "skipped", bench.detail):
            self.record(bench.id, "skip", value=bench.skip or "skipped", reason=bench.detail)

    @property
    def spent(self) -> float:
        return sum(self.cost_by_tier.values()) + self.trial_cost

    def record(self, benchmark: str, type: str, task: Task | None = None, **data) -> dict:
        shard = self._shards.get(benchmark)
        if shard is None:
            shard = self._shards[benchmark] = Shard(self.ledger_dir, benchmark, self.ident, self.run_id, self.protocol)
        digest = data.pop("digest_override", None) or (task.digest if task else "")
        stage = data.get("stage") or {"difficulty": "difficulty", "probe": "probe"}.get(data.get("purpose", ""), "")
        if stage in self.assumed or (type in ("report", "repair") and self.assumed):
            data["assumed"] = True      # never evidence: no accept may rest on it
        if self.sandbox:
            data["simulated"] = True    # signed into the record: it can never pass for a real one
        event = shard.append(type, task.task_id if task else "", digest, **data)
        self.ledger.add(event)
        if benchmark in self.benches:
            self.benches[benchmark].cost += float(data.get("cost", 0) or 0)
        return event

    # ----- the shared ledger -----

    async def pull(self) -> None:
        """Bring in what others pushed, when the ledger is shared. Otherwise nothing to do."""
        if not self.sync.enabled:
            return
        async with self._sync_lock:
            await asyncio.to_thread(self.sync.pull)
            self.ledger.reload()
            write_index(self.ledger_dir, self.sandbox)

    async def push(self, message: str) -> bool:
        """Push this run's records. False means someone pushed first: their records are now loaded."""
        if not self.sync.enabled:
            return True
        async with self._sync_lock:
            ok = await asyncio.to_thread(self.sync.push, message)
            if not ok:
                self.ledger.reload()
            return ok

    async def publish(self, message: str) -> None:
        for _ in range(8):
            if await self.push(message):
                return

    def release(self, benchmark: str, task_ids: list[str]) -> None:
        """Let go of tasks this run holds, so that others may take them."""
        mine = [t for t in task_ids if t in self.claimed.get(benchmark, [])]
        if mine:
            self.record(benchmark, "claim", tasks=mine, released=True, expires=0)
            self.claimed[benchmark] = [t for t in self.claimed[benchmark] if t not in mine]

    def log(self, text: str) -> None:
        with (self.run_dir / "session.log").open("a") as f:
            f.write(f"{time.strftime('%H:%M:%S')} {text}\n")

    def note_recent(self, kind: str, task_id: str, text: str) -> None:
        entry = (time.time(), kind, task_id, text)
        self.recent.appendleft(entry)
        if self.on_recent is not None:
            self.on_recent(entry)

    def board(self, task: Task) -> dict:
        return self.blackboards.setdefault(task.task_id, {
            "task_id": task.task_id, "benchmark": task.benchmark, "status": "in_progress", "stages": []})

    def save_board(self, task: Task) -> None:
        """Note the task's current version. The board is written once, when the task ends."""
        self.board(task)["digest"] = task.digest

    def flush_board(self, task: Task) -> None:
        """Write the task's whole record as one line of tasks.jsonl: one file per run, not a folder per task."""
        board = self.blackboards.get(task.task_id)
        if board is None or board.get("_written") == len(board.get("stages", [])):
            return
        board["digest"] = task.digest
        board["_written"] = len(board.get("stages", []))
        line = {k: v for k, v in board.items() if not k.startswith("_")}
        with (self.run_dir / "tasks.jsonl").open("a") as f:
            f.write(json.dumps(line, separators=(",", ":")) + "\n")

    def report_path(self, task: Task) -> Path:
        """Where a task's report goes: in the index repository beside the ledger, so that a reviewer on
        another machine can read what they sign. One file per version of the task."""
        d = self.ledger_dir / REPORTS / task.benchmark.replace("/", "__")
        d.mkdir(parents=True, exist_ok=True)
        return d / f"{task.name}.{task.digest[:12]}.md"

    def stage_done(self, task: Task, stage: int, started: float, **record) -> None:
        seconds = time.time() - started
        self.durations.setdefault((task.benchmark, stage), []).append(seconds)
        self.board(task)["stages"].append({"stage": STAGES[stage], "seconds": round(seconds, 2), **record})
        self.save_board(task)

    async def container(self, coro):
        async with self.containers:
            self.containers_used += 1
            try:
                return await coro
            finally:
                self.containers_used -= 1

    async def ask(self, kind: str, text: str, target: str = "", choices: list[str] | None = None):
        """Raise a request and wait for its answer. Returns None when nothing can prompt."""
        self._request_ids += 1
        req = Request(self._request_ids, kind, text, target, choices or [])
        self.requests.append(req)
        self.write_requests()
        if self.no_input:
            return None
        req.answer = asyncio.get_running_loop().create_future()
        try:
            return await req.answer
        finally:
            with contextlib.suppress(ValueError):
                self.requests.remove(req)
            self.write_requests()

    def report_ready(self, task: Task) -> None:
        """A report is written. If this user may sign verdicts, tell them, without blocking anything."""
        mine = any(e["type"] == "verdict" and e["runner"] == self.ident.github and e["digest"] == task.digest
                   for e in self.ledger.by_task.get(task.task_id, []))
        if self.is_reviewer and not mine and task not in self.ready:
            self.ready.append(task)
        self._verdict_request()

    def sign_verdict(self, task: Task, value: str, reason: str, digest: str | None = None) -> dict:
        event = self.record(task.benchmark, "verdict", task, value=value, reason=reason, reason_code=value,
                            reviewer=self.ident.github, tier="human", digest_override=digest)
        if task in self.ready and value != "replaced":
            self.ready.remove(task)
        write_index(self.ledger_dir, self.sandbox)      # the accepted list follows every verdict
        self.requests[:] = [r for r in self.requests if not (r.kind == "replace" and r.target == task.task_id)]
        self._verdict_request()
        if value == "accept" and not self.sandbox:
            self.keep(task, digest or task.digest)
        return event

    def keep(self, task: Task, digest: str) -> None:
        """Put the accepted version of a task in the task store. Packing and uploading can take a while, so
        it happens beside the run, and says how it went in the recent outcomes."""
        from ..tasks import store

        def work() -> None:
            try:
                ok, how = store.put(self.ws, self.cfg, task.benchmark, task.name, digest, task.path)
            except (store.StoreError, OSError) as err:
                ok, how = False, f"not kept: {str(err)[:200]}"
            self.note_recent("checked" if ok else "stopped", task.task_id, f"accepted version {how}")

        try:
            self._keeping.append(asyncio.get_running_loop().run_in_executor(None, work))
        except RuntimeError:            # no event loop: a command, not the screen
            work()

    async def kept(self) -> None:
        """Wait until every accepted task has been put in the store."""
        pending, self._keeping = self._keeping, []
        for job in pending:
            await job

    def ask_replacement(self, task: Task, text: str) -> None:
        """The benchmark is full. Ask which accepted task this one replaces, without blocking anything."""
        self.requests[:] = [r for r in self.requests if not (r.kind == "replace" and r.target == task.task_id)]
        self._request_ids += 1
        self.requests.append(Request(self._request_ids, "replace", text, task.task_id))
        self.write_requests()

    def _verdict_request(self) -> None:
        """One standing request that counts the reports waiting, kept up to date."""
        self.requests[:] = [r for r in self.requests if r.kind != "verdict"]
        if self.ready:
            n = len(self.ready)
            names = ", ".join(t.name for t in self.ready[:4]) + (" and more" if n > 4 else "")
            self._request_ids += 1
            self.requests.append(Request(self._request_ids, "verdict",
                                         f"{n} report{'s are' if n != 1 else ' is'} ready for your verdict: {names}."))
        self.write_requests()

    def answer_request(self, req: Request, value) -> None:
        if req.answer is not None and not req.answer.done():
            req.answer.set_result(value)

    def write_requests(self) -> None:
        path = self.run_dir / "requests.json"
        if self.requests:                   # the file exists only while something is waiting
            path.write_text(json.dumps([r.public() for r in self.requests], indent=2) + "\n")
        else:
            path.unlink(missing_ok=True)

    async def run(self) -> None:
        self._shared_tasks = [asyncio.create_task(shared.run_shared(self, b)) for b in self.benches.values()]
        self._workers = [asyncio.create_task(self._cell_loop(c)) for c in self.cells]
        await self._until_done()

    def add_benchmark(self, benchmark: str, info: dict) -> str:
        """Take one more benchmark into this run, while it is going or after it has finished."""
        if benchmark in self.benches:
            return f"{benchmark} is already in this run."
        if self.quitting:
            return "This run is ending, so it takes nothing more."
        self.benches[benchmark] = BenchState(benchmark, info.get("field", "general"), Path(info["path"]) if info.get("path") else Path(),
                                             info.get("released", ""), info.get("license", ""), entry=info.get("entry"))
        self._shared_tasks.append(asyncio.create_task(shared.run_shared(self, self.benches[benchmark])))
        for i, cell in enumerate(self.cells):           # cells that had run out of work take tasks again
            if i >= len(self._workers) or self._workers[i].done():
                worker = asyncio.create_task(self._cell_loop(cell))
                self._workers[i:i + 1] = [worker]
        if self.finished:                               # the run had finished: it is a run again
            self.finished = False
            self._more = asyncio.create_task(self._until_done())
        self.command = (self.command or "hm run") + f" + {benchmark}"
        return f"{benchmark} is added to this run."

    async def _until_done(self) -> None:
        """Wait until every benchmark and every cell has nothing left to do, then close the run's books.
        Benchmarks and cells that are added on the way are waited for too."""
        try:
            while True:
                pending = [t for t in (*self._shared_tasks, *self._workers) if not t.done()]
                if not pending:
                    break
                await asyncio.wait(pending)
        except asyncio.CancelledError:
            pass
        for benchmark in list(self.claimed):              # let go of whatever this run still holds
            self.release(benchmark, list(self.claimed[benchmark]))
        self.flush_usage()
        with contextlib.suppress(Exception):
            await self.publish("end of run")
        self.finished = True
        for leftover in ("harbor", "scratch"):          # working folders: nothing in them is kept
            with contextlib.suppress(OSError):
                (self.run_dir / leftover).rmdir()
        (self.run_dir / "run.json").write_text(json.dumps(self.summary(), indent=2) + "\n")
        self.write_requests()

    def summary(self) -> dict:
        return {
            "run_id": self.run_id, "runner": self.ident.github, "protocol": self.protocol,
            "command": self.command, "elapsed_seconds": round(time.time() - self.started, 1),
            "spent": round(self.spent, 4), "cost_by_tier": {k: round(v, 4) for k, v in self.cost_by_tier.items()},
            "trial_cost": round(self.trial_cost, 2), "calls": dict(self.calls),
            "benchmarks": {b.id: {"tasks": len(b.tasks), "triaged_out": b.triaged_out,
                                  "not_selected": b.not_selected, "selected": b.selected, "kept": b.kept,
                                  "dropped": b.dropped, "phase": b.phase} for b in self.benches.values()},
            "requests_waiting": sum(1 for r in self.requests if r.blocking),
        }

    def _shared_done(self) -> bool:
        return all(b.phase in ("ready", "skipped") for b in self.benches.values())

    async def _cell_loop(self, cell: CellState) -> None:
        while not self.quitting:
            task = self._next_task(cell)
            if task is None:
                if self._shared_done() and not self.queue and not self.paused:
                    return
                await asyncio.sleep(0.05)
                continue
            cell.task, cell.started, cell.cost, cell.attention, cell.stopping = task, time.time(), 0.0, False, False
            cell.runner = asyncio.current_task()
            BENCH.set(task.benchmark)
            bench = self.benches[task.benchmark]
            try:
                await stages.process(self, cell, task)
            except asyncio.CancelledError:
                if not cell.stopping:
                    raise
                asyncio.current_task().uncancel()
                self.board(task)["status"] = "incomplete"
                self.save_board(task)
                self.note_recent("stopped", task.task_id, f"stopped at {STAGES[cell.stage]}, marked incomplete")
            except Exception as err:      # one task's failure must not take the run down
                self.board(task)["status"] = f"error at {STAGES[cell.stage]}: {type(err).__name__}: {err}"[:400]
                self.save_board(task)
                self.note_recent("error", task.task_id, f"{STAGES[cell.stage]}: {type(err).__name__}: {str(err)[:140]}")
            finally:
                bench.done += 1
                cell.task, cell.detail, cell.stage, cell.attention = None, "", 0, False
            # Finished, failed, or stopped: either way this run no longer holds the task. A finished
            # task is protected by its records from here on, and an unfinished one is free for others.
            self.flush_board(task)
            self.flush_usage(task.benchmark)
            self.release(task.benchmark, [task.task_id])
            with contextlib.suppress(Exception):
                await self.publish(f"records for {task.name}")

    def _check_config(self) -> None:
        """The settings on disk must not change during a run. This run keeps the protocol it started
        with whatever happens to the file, so an edit cannot reach its results. It is reported once."""
        if self._config_changed or time.time() - self._config_checked < 5:
            return
        self._config_checked = time.time()
        try:
            from ..core import config as config_mod
            on_disk = config_mod.load(self.ws).protocol
        except Exception:
            on_disk = "unreadable"
        if on_disk != self._file_protocol:
            self._config_changed = True
            self.note_recent("config", "", f"protocol.toml changed during the run (protocol {on_disk}). This run keeps "
                             f"{self.protocol}. The next run is refused unless the owner locks the new settings.")

    def _next_task(self, cell: CellState) -> Task | None:
        self._check_config()
        if self.paused or cell.n > self.active_cells or self.spent >= self.budget:
            if self.spent >= self.budget and not self.paused:
                self._budget_reached()
            return None
        for i, task in enumerate(self.queue):
            if not self.benches[task.benchmark].paused:
                return self.queue.pop(i)
        return None

    def _budget_reached(self) -> None:
        if any(r.kind == "budget" for r in self.requests):
            return
        self.paused = True
        text = f"The ${self.budget:.0f} budget is spent. Raise it with /budget N, or /quit."
        self._request_ids += 1
        self.requests.append(Request(self._request_ids, "budget", text))
        self.write_requests()
        self.note_recent("budget", "", text)
        if self.no_input:
            self.quitting = True

    def set_cells(self, n: int) -> None:
        """Change how many cells take tasks. New cells start at once, extra ones finish their task."""
        self.active_cells = n
        while len(self.cells) < n:
            cell = CellState(len(self.cells) + 1)
            self.cells.append(cell)
            self._workers.append(asyncio.create_task(self._cell_loop(cell)))

    def quit(self) -> None:
        """Stop taking tasks, and stop waiting for answers. A benchmark that was waiting for one is
        skipped for this run, and asks again in the next."""
        self.quitting = True
        for r in list(self.requests):
            if r.answer is not None and not r.answer.done():
                r.answer.set_result(None)

    def kill(self) -> None:
        self.quitting = True
        for t in [*self._workers, *self._shared_tasks]:
            t.cancel()

    def find_task(self, token: str) -> Task | None:
        token = token.strip().strip(",.?")
        for bench in self.benches.values():
            for task in bench.tasks:
                if task.task_id == token or task.name == token or task.task_id.endswith("/" + token):
                    return task
        return None

    def cell_of(self, task: Task) -> CellState | None:
        return next((c for c in self.cells if c.task is task), None)
