"""A Claude session that could not be had is waited out; a model that answered badly is not."""
import asyncio

import claude_agent_sdk
import pytest
from claude_agent_sdk import ResultMessage

from harbor_master.backends import claude
from harbor_master.backends.base import Unreachable

SCHEMA = {"type": "object", "properties": {"ok": {"type": "boolean"}}, "required": ["ok"]}


def result(**more) -> ResultMessage:
    base = dict(subtype="success", duration_ms=1, duration_api_ms=1, is_error=False, num_turns=1, session_id="s",
                structured_output={"ok": True}, usage={"input_tokens": 3, "output_tokens": 2})
    return ResultMessage(**{**base, **more})


@pytest.fixture
def sessions(monkeypatch):
    """Each try takes the next of `plan`: an exception to raise, or a ResultMessage to end on. The waits are recorded."""
    plan, waited = [], []

    async def query(prompt, options):
        step = plan.pop(0)
        if isinstance(step, Exception):
            raise step
        yield step

    async def sleep(seconds):
        waited.append(seconds)
    monkeypatch.setattr(claude_agent_sdk, "query", query)
    monkeypatch.setattr(claude.asyncio, "sleep", sleep)
    monkeypatch.setattr(claude, "_env", lambda: {})
    return plan, waited


def ask(**k):
    return asyncio.run(claude.ask("claude-sonnet-5-5", "system", "prompt", SCHEMA, **k))


def test_a_session_that_does_not_start_is_tried_again_after_a_wait(sessions):
    plan, waited = sessions
    plan += [Exception("Control request timeout: initialize"), Exception("Control request timeout: initialize"), result()]
    assert ask().data == {"ok": True}
    assert waited == list(claude.WAITS[:2])


def test_an_overloaded_api_and_a_session_with_no_result_are_waited_out_too(sessions):
    plan, waited = sessions
    plan += [result(is_error=True, structured_output=None, result="Overloaded", api_error_status=529), Exception("x"), result()]
    plan[1] = result(is_error=True, structured_output=None, result=None, subtype="")     # ended with nothing to say
    plan.insert(1, result(is_error=True, structured_output=None, result="rate limited", api_error_status=429))
    assert ask().data == {"ok": True}
    assert len(waited) == 2          # 529 and 429; the empty one after them is a model failure, the first of two allowed


def test_when_it_never_starts_it_is_unreachable_after_every_wait(sessions):
    plan, waited = sessions
    plan += [Exception("Control request timeout: initialize")] * (len(claude.WAITS) + 1)
    with pytest.raises(Unreachable, match="could not be reached"):
        ask()
    assert waited == list(claude.WAITS)


def test_a_model_that_answers_badly_is_not_waited_for_and_is_no_connection_error(sessions):
    plan, waited = sessions
    plan += [result(is_error=True, subtype="error_max_turns", structured_output=None)] * 2
    with pytest.raises(RuntimeError, match="did not return a typed result") as err:
        ask()
    assert not isinstance(err.value, Unreachable) and waited == []
    plan += [result(is_error=True, structured_output=None, result="Invalid API key", api_error_status=401)] * 2
    with pytest.raises(RuntimeError) as err:
        ask()
    assert not isinstance(err.value, Unreachable) and waited == []


def test_a_check_waits_for_nothing(sessions):
    plan, waited = sessions
    plan += [Exception("Control request timeout: initialize")]
    with pytest.raises(Unreachable):
        ask(attempts=1, waits=())
    assert waited == []


def test_the_startup_limit_is_raised_unless_set(sessions, monkeypatch):
    plan, _ = sessions
    monkeypatch.delenv("CLAUDE_CODE_STREAM_CLOSE_TIMEOUT", raising=False)
    plan += [result()]
    ask()
    import os
    assert os.environ["CLAUDE_CODE_STREAM_CLOSE_TIMEOUT"] == str(claude.STARTUP_SECONDS * 1000)
    monkeypatch.setenv("CLAUDE_CODE_STREAM_CLOSE_TIMEOUT", "300000")
    plan += [result()]
    ask()
    assert os.environ["CLAUDE_CODE_STREAM_CLOSE_TIMEOUT"] == "300000"
