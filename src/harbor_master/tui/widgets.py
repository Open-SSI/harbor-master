"""The prompt and the task screen."""
from __future__ import annotations

from typing import TYPE_CHECKING

from textual.app import ComposeResult
from rich.console import Group
from rich.markdown import Markdown
from rich.padding import Padding
from rich.text import Text
from textual.containers import Vertical, VerticalScroll
from textual.css.query import NoMatches
from textual.screen import ModalScreen
from textual.widgets import Input, Static

from .. import interact
from ..interact import Command
from .parts import QUIT_KEYS, Fits
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
    and the screen can still be left (ctrl+l) or the run ended (ctrl+z)."""

    BINDINGS = [("escape", "stay", "Back"), ("pageup", "scroll(-1, True)", "Up"), ("pagedown", "scroll(1, True)", "Down"),
                ("up", "scroll(-1, False)", "Up"), ("down", "scroll(1, False)", "Down"),
                ("ctrl+l", "app.leave", "Leave"), ("ctrl+z", "end", "Quit"), ("ctrl+t", "app.theme", "Light or dark")]
    NEEDED = "Each report needs your verdict before anything else: accept, or reject <why>."
    DEFAULT_CSS = f"""
    ReviewScreen {{ align: center middle; background: {BG} 70%; }}
    ReviewScreen > Vertical {{ width: 92%; height: 90%; border: round {PU}; background: {BG}; }}
    ReviewScreen VerticalScroll {{ height: 1fr; padding: 1 2 0 2; }}
    ReviewScreen #report {{ padding: 0 0 1 0; }}
    ReviewScreen #said {{ height: auto; padding: 0 2; color: {AM}; }}
    ReviewScreen Input {{ border: round {PU}; background: {BG}; height: 3; margin: 0 1; }}
    """

    def __init__(self, session, tasks: list):
        super().__init__()
        self.session, self.tasks, self.at, self.done = session, tasks, 0, []

    def compose(self) -> ComposeResult:
        yield Vertical(VerticalScroll(Static(id="report")), Static(id="said"),
                       Input(placeholder="accept [why]     reject <why>", id="verdict"))

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
        box.border_subtitle = QUIT_KEYS
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

    It opens by itself when a ranking is ready, and nothing else can be done until it is answered: with the
    positions of the tasks that go on (1, 2, 3, 5, or 1-13, 16), which approves them, with enter alone, which approves
    the ones proposed, or with a new brief, which ranks again. Each task is listed with its position and a line on what
    it asks."""

    BINDINGS = [("escape", "stay", "Back"), ("pageup", "scroll(-1, True)", "Up"), ("pagedown", "scroll(1, True)", "Down"),
                ("up", "scroll(-1, False)", "Up"), ("down", "scroll(1, False)", "Down"),
                ("ctrl+l", "app.leave", "Leave"), ("ctrl+z", "end", "Quit"), ("ctrl+t", "app.theme", "Light or dark")]
    DEFAULT_CSS = f"""
    SelectionScreen {{ align: center middle; background: {BG} 70%; }}
    SelectionScreen > Vertical {{ width: 92%; height: 90%; border: round {AM}; border-title-color: {AM}; background: {BG}; }}
    SelectionScreen VerticalScroll {{ height: 1fr; padding: 1 2 0 2; }}
    SelectionScreen #said {{ height: auto; padding: 0 2; color: {AM}; }}
    SelectionScreen Input {{ border: round {PU}; background: {BG}; height: 3; margin: 0 1; }}
    """

    def __init__(self, session, request):
        super().__init__()
        self.session, self.request, self.bench = session, request, session.benches[request.target]
        self.rows = list(self.bench.ranking)        # (task id, rating, proposed), in the order tasks are taken
        self.most = self.bench.most
        self.proposed = [i for i, r in enumerate(self.rows, 1) if r[2]]

    def compose(self) -> ComposeResult:
        yield Vertical(VerticalScroll(Static(id="tasks")), Static(id="said"), Input(id="choice"))

    def on_mount(self) -> None:
        self.call_after_refresh(self.start)

    def start(self) -> None:
        try:
            box = self.query_one(Vertical)
            box.border_title = f"selection   {self.bench.id}   {len(self.rows)} tasks passed the gates and are ranked"
            line = self.query_one("#choice", Input)
            line.placeholder = (f"enter: the top {len(self.proposed)}     or the positions that go on, such as 1-13, 16"
                                f" (at most {self.most})     or: brief <what matters most, to rank again>")
            line.focus()
            self.draw(self.proposed)
        except NoMatches:       # the screen is closing under it
            pass

    def draw(self, at: list[int]) -> None:
        """The list, with the rows at these positions marked as the ones that go on."""
        rows, chosen = [], set(at)
        for i, (task_id, rating, _) in enumerate(self.rows, 1):
            task = self.session.find_task(task_id)
            on = i in chosen
            head = Text(f"{'●' if on else '○'} {i:>3}  ", style=f"bold {TE}" if on else MU)
            title = str((task.meta.get("task") or {}).get("title") or "") if task else ""
            head.append(task_id.split("/")[-1] + (f"   {title}" if title else ""), style="bold" if on else MU)
            head.append(f"   {task.subarea if task else ''}   rating {rating:+.2f}", style=MU)
            # the description wraps under itself, not back to the left edge
            line = Text(interact.answers.task_line(task) if task else "", style=SE if on else MU)
            rows += [head, Padding(line, (0, 0, 1, 7))]
        self.query_one("#tasks", Static).update(Group(*rows))
        self.query_one(Vertical).border_subtitle = f"{len(chosen)} go on, {len(self.rows) - len(chosen)} are left out   ·   {QUIT_KEYS}"

    def action_scroll(self, way: int, page: bool) -> None:
        area = self.query_one(VerticalScroll)
        area.scroll_relative(y=way * (max(1, area.size.height - 2) if page else 1), animate=False)

    def action_end(self) -> None:
        self.dismiss("")
        self.app.action_quit_run()      # type: ignore[attr-defined]

    def action_stay(self) -> None:
        self.query_one("#said", Static).update("The run waits for this: the positions that go on, such as 1-13, 16, "
                                               "or brief <what matters>.")

    def on_input_changed(self, event: Input.Changed) -> None:
        from ..curation.ranking import positions
        typed = event.value.strip()
        if not typed:
            self.draw(self.proposed)
        elif not typed.lower().startswith(("brief", "/brief")):
            at, _ = positions(typed, len(self.rows), len(self.rows))
            if at:
                self.draw(at)           # the marks follow the positions as they are typed

    def on_input_submitted(self, event: Input.Submitted) -> None:
        event.stop()                    # an answer to this page, not a command for the run
        typed, short = event.value.strip(), self.bench.id
        said = self.query_one("#said", Static)
        if not typed.lower().startswith(("brief", "/brief")):
            from ..curation.ranking import positions
            at, why = positions(typed, len(self.rows), self.most) if typed else (self.proposed, "")
            if why:
                said.update(why[:1].upper() + why[1:] + ".")
                return
            self.dismiss(interact.execute(self.session, Command("approve", [short, ",".join(map(str, at))])))
        elif typed.lower().startswith(("brief ", "/brief ")) and len(typed.split()) > 1:
            words = typed.split()[1:]
            if words and words[0] == short:
                words = words[1:]
            self.dismiss(interact.execute(self.session, Command("brief", [short, *words])))
        else:
            said.update("Type the positions that go on, such as 1-13, 16, or brief <what matters> to rank again.")


class AskScreen(Fits, ModalScreen[str]):
    """Something the run cannot go on without: a brief, or a go-ahead for an adapter.

    Whatever the run asks of a person is asked here, in a box of its own that opens by itself. Nothing else
    can be done on the screen until it is answered. The run's other work goes on behind it, and the screen
    can still be left (ctrl+l) or the run ended (ctrl+z)."""

    BINDINGS = [("escape", "stay", "Back"), ("pageup", "scroll(-1, True)", "Up"), ("pagedown", "scroll(1, True)", "Down"),
                ("up", "scroll(-1, False)", "Up"), ("down", "scroll(1, False)", "Down"),
                ("ctrl+l", "app.leave", "Leave"), ("ctrl+z", "end", "Quit"), ("ctrl+t", "app.theme", "Light or dark")]
    DEFAULT_CSS = f"""
    AskScreen {{ align: center middle; background: {BG} 70%; }}
    AskScreen > Vertical {{ width: 86%; height: auto; max-height: 90%; border: round {AM}; border-title-color: {AM}; background: {BG};
                            overflow-y: auto; scrollbar-size: 0 0; }}
    AskScreen VerticalScroll {{ height: auto; padding: 1 2 0 2; }}
    AskScreen #said {{ height: auto; padding: 0 2; color: {AM}; }}
    AskScreen Input {{ border: round {PU}; background: {BG}; height: 3; margin: 1 1 0 1; }}
    """
    # kind -> (title, what the empty line shows, the keys line)
    SHARE = 0.88                    # its box is at most 90% of the screen
    KINDS = {
        "brief": ("a brief is needed", "what matters most in this benchmark, in a sentence or two"),
        "lock": ("an adapter is ready", "lock"),
    }

    def __init__(self, session, request):
        super().__init__()
        self.session, self.request = session, request

    def compose(self) -> ComposeResult:
        yield Vertical(VerticalScroll(Static(id="asked")), Static(id="said"), Input(id="reply"))

    def on_mount(self) -> None:
        self.call_after_refresh(self.start)

    def start(self) -> None:
        try:
            title, shown = self.KINDS[self.request.kind]
            box = self.query_one(Vertical)
            box.border_title = f"{title}   {self.request.target}".rstrip()
            box.border_subtitle = QUIT_KEYS
            head, _, rest = self.request.text.partition("\n")
            self.query_one("#asked", Static).update(Text.assemble((head, "bold"), "\n" + rest if rest else ""))
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
        if words and words[0].lstrip("/") in ("brief", "lock") and len(words) > 1 and words[1] == target:
            words = [words[0].lstrip("/"), *words[2:]]      # typed as the whole command: the name and the benchmark are not the answer
        if kind == "brief":
            said = words[1:] if words[:1] == ["brief"] else words
            return Command("brief", [target, *said]) if len(said) >= 2 else "Say it in a few words at least: what makes a task here matter?"
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
        else:
            self.dismiss(reply)
