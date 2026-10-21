"""The client of the exit node's controller API: errors, slots, the node's name (boundary)."""

import httpx
import pytest

from exitctl import exitnode
from exitctl.exitnode import ExitNode
from exitctl.model import NodeError


@pytest.fixture
def answer(monkeypatch):
    """Routes ExitNode's requests to `answer.handler(request) -> httpx.Response`."""
    class Answer:
        handler = staticmethod(lambda req: httpx.Response(200, json={}))
    real = httpx.AsyncClient
    monkeypatch.setattr(exitnode.httpx, "AsyncClient",
                        lambda **kw: real(transport=httpx.MockTransport(lambda r: Answer.handler(r)), **kw))
    return Answer


async def test_an_error_answer_carries_the_controllers_detail(answer):
    answer.handler = lambda req: httpx.Response(409, json={"detail": "the new key carried no traffic"})
    with pytest.raises(NodeError) as e:
        await ExitNode("http://n:8080").put_key("a", "conf", {})
    assert e.value.status == 409 and e.value.refused and e.value.detail == "the new key carried no traffic"


async def test_an_error_without_json_keeps_its_text(answer):
    answer.handler = lambda req: httpx.Response(502, text="bad gateway")
    with pytest.raises(NodeError) as e:
        await ExitNode("http://n:8080").status()
    assert (e.value.status, e.value.detail, e.value.refused) == (502, "bad gateway", False)


async def test_an_unreachable_node_is_status_0(answer):
    def refuse(req):
        raise httpx.ConnectError("refused")
    answer.handler = refuse
    with pytest.raises(NodeError) as e:
        await ExitNode("http://n:8080").status()
    assert e.value.status == 0 and "unreachable" in e.value.detail


async def test_a_slot_is_found_by_name_and_a_missing_one_is_404(answer):
    answer.handler = lambda req: httpx.Response(200, json={"slots": [{"slot": "a"}, {"slot": "b"}]})
    node = ExitNode("http://n:8080")
    assert (await node.key("b")) == {"slot": "b"}
    with pytest.raises(NodeError) as e:
        await node.key("c")
    assert e.value.status == 404


def test_the_nodes_name_is_the_urls_first_host_label_unless_set(monkeypatch):
    monkeypatch.delenv("EXITCTL_NODE", raising=False)
    assert ExitNode("http://exit-proton.tail1234.ts.net:8080/").node_name == "exit-proton"
    monkeypatch.setenv("EXITCTL_NODE", "exit-home")
    assert ExitNode("http://exit-proton:8080").node_name == "exit-home"
