"""The prompt and the task screen."""
from __future__ import annotations

from typing import TYPE_CHECKING

from textual.app import ComposeResult
from rich.markdown import Markdown
from rich.text import Text
from textual.containers import Vertical, VerticalScroll
from textual.css.query import NoMatches
from textual.screen import ModalScreen
from textual.widgets import Input, Static

from .. import interact
from ..interact import Command
from .theme import AM, BG, MU, PU, SE, TE

if TYPE_CHECKING:
    pass


class TaskScreen(ModalScreen[None]):
    """One task: where it is and why."""

    BINDINGS = [("escape", "dismiss", "Back")]
    DEFAULT_CSS = f"""
    TaskScreen {{ align: center middle; background: {BG} 70%; }}
    TaskScreen > VerticalScroll {{ width: 90%; height: 80%; border: round {PU}; padding: 1 2; background: {BG}; }}
    """

    def __init__(self, title: str, body: str):
        super().__init__()
        self._title, self._body = title, body

    def compose(self) -> ComposeResult:
        box = VerticalScroll(Static(self._body))
        box.border_title = self._title
        box.border_subtitle = "esc to go back"
        yield box


class ReviewScreen(ModalScreen[str]):
    """The reports that wait for this reviewer, one after the other. The report is read here, and the verdict
    is typed under it: accept [why] or reject <why>. A verdict can be given nowhere else, so
    none is given on a report that was not opened.

    It opens by itself when a report is ready, and it stays until every report it shows has a verdict:
    there is no skipping one and no going back to the run's prompt first. The run itself goes on behind it,
    and the screen can still be left (ctrl+l) or the run ended (ctrl+q)."""

    BINDINGS = [("escape", "stay", "Back"), ("pageup", "scroll(-1, True)", "Up"), ("pagedown", "scroll(1, True)", "Down"),
                ("up", "scroll(-1, False)", "Up"), ("down", "scroll(1, False)", "Down"),
                ("ctrl+l", "app.leave", "Leave"), ("ctrl+q", "end", "Quit"), ("ctrl+t", "app.theme", "Light or dark")]
    NEEDED = "Each report needs your verdict before anything else: accept [why] or reject <why>."
    DEFAULT_CSS = f"""
    ReviewScreen {{ align: center middle; background: {BG} 70%; }}
    ReviewScreen > Vertical {{ width: 92%; height: 90%; border: round {PU}; background: {BG}; }}
    ReviewScreen VerticalScroll {{ height: 1fr; padding: 1 2 0 2; }}
    ReviewScreen #report {{ padding: 0 0 1 0; }}
    ReviewScreen #said {{ height: auto; padding: 0 2; color: {AM}; }}
    ReviewScreen Input {{ border: round {PU}; background: {BG}; height: 3; margin: 0 1; }}
    ReviewScreen #how {{ height: 1; padding: 0 2; color: {MU}; }}
    """

    def __init__(self, session, tasks: list):
        super().__init__()
        self.session, self.tasks, self.at, self.done = session, tasks, 0, []

    def compose(self) -> ComposeResult:
        yield Vertical(VerticalScroll(Static(id="report")), Static(id="said"),
                       Input(placeholder="accept [why]     reject <why>", id="verdict"),
                       Static("up, down, page up and page down scroll the report     ctrl+l leaves the screen, and the run goes on", id="how"))

    def on_mount(self) -> None:
        self.call_after_refresh(self.start)     # once the page's own parts are there to fill

    def start(self) -> None:
        try:
            self.query_one("#verdict", Input).focus()
            self.show()
        except NoMatches:       # the screen is closing under it: there is nothing left to fill
            pass

    def show(self) -> None:
        task = self.tasks[self.at]
        box = self.query_one(Vertical)
        box.border_title = f"review {self.at + 1} of {len(self.tasks)}   {task.task_id}"
        box.border_subtitle = interact.answers.report_brief(self.session, task)[:110]
        self.query_one("#report", Static).update(Markdown(interact.answers.report_text(self.session, task)))
        self.query_one(VerticalScroll).scroll_home(animate=False)

    def action_scroll(self, way: int, page: bool) -> None:
        area = self.query_one(VerticalScroll)
        step = max(1, area.size.height - 2) if page else 1
        area.scroll_relative(y=way * step, animate=False)

    def action_end(self) -> None:
        """Ending the run is always possible. Its question is asked on the run's own screen, so this page steps aside."""
        self.dismiss("")
        self.app.action_quit_run()      # type: ignore[attr-defined]

    def action_stay(self) -> None:
        self.query_one("#said", Static).update(self.NEEDED)

    def summary(self) -> str:
        left = len(self.session.ready)
        return ("\n".join(self.done) or "No verdict was signed.") + f"\n{left} report{'s' if left != 1 else ''} left to review."

    def on_input_submitted(self, event: Input.Submitted) -> None:
        event.stop()            # what is typed here is a verdict on this report, not a command for the run
        line, task = event.value.strip(), self.tasks[self.at]
        event.input.value = ""
        said = self.query_one("#said", Static)
        reply = interact.decide(self.session, task, line) if line else self.NEEDED
        if task in self.session.ready:            # nothing was signed: no verdict typed, or a reason missing
            said.update(reply)
            return
        self.done.append(reply.split(". ")[0] + ".")
        said.update("")
        self.at += 1
        if self.at >= len(self.tasks):
            self.dismiss(self.summary())
        else:
            self.show()


class SelectionScreen(ModalScreen[str]):
    """The ranked tasks of a benchmark, and the choice of how many go on to the costly stages.

    It opens by itself when a ranking is ready, and nothing else can be done until it is answered: with a
    number k, which approves the top k of the list, or with a new brief, which ranks again. Each task is
    listed in the order it is taken, with a line on what it asks, so "the top k" is the first k rows."""

    BINDINGS = [("escape", "stay", "Back"), ("pageup", "scroll(-1, True)", "Up"), ("pagedown", "scroll(1, True)", "Down"),
                ("up", "scroll(-1, False)", "Up"), ("down", "scroll(1, False)", "Down"),
                ("ctrl+l", "app.leave", "Leave"), ("ctrl+q", "end", "Quit"), ("ctrl+t", "app.theme", "Light or dark")]
    DEFAULT_CSS = f"""
    SelectionScreen {{ align: center middle; background: {BG} 70%; }}
    SelectionScreen > Vertical {{ width: 92%; height: 90%; border: round {AM}; border-title-color: {AM}; background: {BG}; }}
    SelectionScreen VerticalScroll {{ height: 1fr; padding: 1 2 0 2; }}
    SelectionScreen #said {{ height: auto; padding: 0 2; color: {AM}; }}
    SelectionScreen Input {{ border: round {PU}; background: {BG}; height: 3; margin: 0 1; }}
    SelectionScreen #how {{ height: auto; padding: 0 2; color: {MU}; }}
    """

    def __init__(self, session, request):
        super().__init__()
        self.session, self.request, self.bench = session, request, session.benches[request.target]
        self.rows = list(self.bench.ranking)        # (task id, rating, proposed), in the order tasks are taken
        self.most = self.bench.most
        self.k = sum(1 for r in self.rows if r[2])

    def compose(self) -> ComposeResult:
        yield Vertical(VerticalScroll(Static(id="tasks")), Static(id="said"), Input(id="choice"), Static(id="how"))

    def on_mount(self) -> None:
        self.call_after_refresh(self.start)

    def start(self) -> None:
        try:
            box = self.query_one(Vertical)
            box.border_title = f"selection   {self.bench.id}   {len(self.rows)} tasks passed the gates and are ranked"
            self.query_one("#how", Static).update(
                f"enter approves the top {self.k}     a number from 1 to {self.most} approves that many     "
                "brief <what matters> ranks again     up and down scroll")
            line = self.query_one("#choice", Input)
            line.placeholder = f"{self.k}     or another number from 1 to {self.most}     or: brief <what matters most, to rank again>"
            line.focus()
            self.draw(self.k)
        except NoMatches:       # the screen is closing under it
            pass

    def draw(self, k: int) -> None:
        """The list, with the first k rows marked as the ones that go on."""
        out = Text()
        for i, (task_id, rating, _) in enumerate(self.rows, 1):
            task = self.session.find_task(task_id)
            on = i <= k
            out.append(f"{'●' if on else '○'} {i:>3}  ", style=f"bold {TE}" if on else MU)
            title = str((task.meta.get("task") or {}).get("title") or "") if task else ""
            out.append(task_id.split("/")[-1] + (f"   {title}" if title else ""), style="bold" if on else MU)
            out.append(f"   {task.subarea if task else ''}   rating {rating:+.2f}\n", style=MU)
            out.append(f"       {interact.answers.task_line(task) if task else ''}\n\n", style=SE if on else MU)
        self.query_one("#tasks", Static).update(out)
        self.query_one(Vertical).border_subtitle = f"the top {k} go on to the costly stages, {len(self.rows) - k} are left out"

    def action_scroll(self, way: int, page: bool) -> None:
        area = self.query_one(VerticalScroll)
        area.scroll_relative(y=way * (max(1, area.size.height - 2) if page else 1), animate=False)

    def action_end(self) -> None:
        self.dismiss("")
        self.app.action_quit_run()      # type: ignore[attr-defined]

    def action_stay(self) -> None:
        self.query_one("#said", Static).update(f"The run waits for this: a number from 1 to {self.most}, or brief <what matters>.")

    def on_input_changed(self, event: Input.Changed) -> None:
        typed = event.value.strip()
        if typed.isdigit() and 1 <= int(typed) <= self.most:
            self.draw(int(typed))       # the marks follow the number as it is typed
        elif not typed:
            self.draw(self.k)

    def on_input_submitted(self, event: Input.Submitted) -> None:
        event.stop()                    # an answer to this page, not a command for the run
        typed, short = event.value.strip(), self.bench.id
        said = self.query_one("#said", Static)
        if not typed or typed.isdigit():
            k = int(typed) if typed else self.k
            if not 1 <= k <= self.most:
                said.update(f"{typed} is not possible: choose a number from 1 to {self.most}.")
                return
            self.dismiss(interact.execute(self.session, Command("approve", [short, str(k)])))
        elif typed.lower().startswith(("brief ", "/brief ")) and len(typed.split()) > 1:
            words = typed.split()[1:]
            if words and words[0] == short:
                words = words[1:]
            self.dismiss(interact.execute(self.session, Command("brief", [short, *words])))
        else:
            said.update(f"Type a number from 1 to {self.most} to approve that many, or brief <what matters> to rank again.")


class AskScreen(ModalScreen[str]):
    """Something the run cannot go on without: a brief, a budget, a decision on an adapter.

    Whatever the run asks of a person is asked here, in a box of its own that opens by itself. Nothing else
    can be done on the screen until it is answered. The run's other work goes on behind it, and the screen
    can still be left (ctrl+l) or the run ended (ctrl+q)."""

    BINDINGS = [("escape", "stay", "Back"), ("pageup", "scroll(-1, True)", "Up"), ("pagedown", "scroll(1, True)", "Down"),
                ("up", "scroll(-1, False)", "Up"), ("down", "scroll(1, False)", "Down"),
                ("ctrl+l", "app.leave", "Leave"), ("ctrl+q", "end", "Quit"), ("ctrl+t", "app.theme", "Light or dark")]
    DEFAULT_CSS = f"""
    AskScreen {{ align: center middle; background: {BG} 70%; }}
    AskScreen > Vertical {{ width: 86%; height: auto; max-height: 90%; border: round {AM}; border-title-color: {AM}; background: {BG}; }}
    AskScreen VerticalScroll {{ height: auto; max-height: 100%; padding: 1 2 0 2; }}
    AskScreen #said {{ height: auto; padding: 0 2; color: {AM}; }}
    AskScreen Input {{ border: round {PU}; background: {BG}; height: 3; margin: 1 1 0 1; }}
    AskScreen #how {{ height: auto; padding: 0 2; color: {MU}; }}
    """
    # kind -> (title, what the empty line shows, the keys line)
    KINDS = {
        "brief": ("a brief is needed", "what matters most in this benchmark, in a sentence or two",
                  "enter sends your brief, and the tasks are ranked by it"),
        "budget": ("the budget is spent", "a new budget in dollars, such as 300", "enter sets the budget, and the run goes on"),
        "adapter": ("an adapter does not pass", "recheck     or     abandon",
                    "recheck tries again, with your fix if you made one     abandon gives the benchmark up for this index"),
        "lock": ("an adapter is ready", "lock", "enter lets the adapter be used, and its tasks start"),
    }

    def __init__(self, session, request):
        super().__init__()
        self.session, self.request = session, request

    def compose(self) -> ComposeResult:
        yield Vertical(VerticalScroll(Static(id="asked")), Static(id="said"), Input(id="reply"), Static(id="how"))

    def on_mount(self) -> None:
        self.call_after_refresh(self.start)

    def start(self) -> None:
        try:
            title, shown, keys = self.KINDS[self.request.kind]
            box = self.query_one(Vertical)
            box.border_title = f"{title}   {self.request.target}".rstrip()
            head, _, rest = self.request.text.partition("\n")
            self.query_one("#asked", Static).update(Text.assemble((head, "bold"), "\n" + rest if rest else ""))
            self.query_one("#how", Static).update(keys + "     ctrl+l leaves the screen, and the run waits")
            line = self.query_one("#reply", Input)
            line.placeholder = shown
            line.focus()
        except NoMatches:       # the screen is closing under it
            pass

    def action_scroll(self, way: int, page: bool) -> None:
        area = self.query_one(VerticalScroll)
        area.scroll_relative(y=way * (max(1, area.size.height - 2) if page else 1), animate=False)

    def action_end(self) -> None:
        self.dismiss("")
        self.app.action_quit_run()      # type: ignore[attr-defined]

    def action_stay(self) -> None:
        self.query_one("#said", Static).update("The run waits for this answer. Nothing else can be done here first.")

    def command(self, typed: str) -> Command | str:
        """The command that what was typed stands for, or what to say when it is not an answer."""
        kind, target = self.request.kind, self.request.target
        words = typed.split()
        if words and words[0].lstrip("/") in ("brief", "budget", "recheck", "abandon", "lock") and len(words) > 1 and words[1] == target:
            words = [words[0].lstrip("/"), *words[2:]]      # typed as the whole command: the name and the benchmark are not the answer
        if kind == "brief":
            said = words[1:] if words[:1] == ["brief"] else words
            return Command("brief", [target, *said]) if len(said) >= 2 else "Say it in a few words at least: what makes a task here matter?"
        if kind == "budget":
            amount = (words[1:] if words[:1] == ["budget"] else words)[:1]
            ok = bool(amount) and amount[0].lstrip("$").replace(".", "", 1).isdigit()
            return Command("budget", [amount[0].lstrip("$")]) if ok else "Type an amount in dollars, such as 300."
        if kind == "adapter":
            return Command(words[0], [target]) if words[:1] in (["recheck"], ["abandon"]) else "Type recheck or abandon."
        return Command("lock", [target]) if words[:1] in ([], ["lock"]) else "Press enter, or type lock."

    def on_input_submitted(self, event: Input.Submitted) -> None:
        event.stop()                    # an answer to this box, not a command for the run
        found = self.command(event.value.strip())
        if isinstance(found, str):
            self.query_one("#said", Static).update(found)
            return
        event.input.value = ""
        reply = interact.execute(self.session, found)
        if self.request in self.session.requests and self.request.answer is not None and not self.request.answer.done():
            self.query_one("#said", Static).update(reply)       # it was not taken: the request still waits
        elif self.request.kind == "budget" and self.request in self.session.requests:
            self.query_one("#said", Static).update(reply)       # still under what is spent
        else:
            self.dismiss(reply)
