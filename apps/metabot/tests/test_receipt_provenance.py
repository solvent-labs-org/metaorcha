"""What a receipt claims about a run is signed: which bridge, endpoint and plugins it ran
on, and where it sits among other runs. The journal keeps a readable copy; the signature
covers the verdicts, so these are read from the receipt itself."""

from __future__ import annotations

import json
from pathlib import Path

from fake_chat import FakeChat, answer
from metabot.approvals import DenyAll
from metabot.config import Config
from metabot.engines import get_engine
from metabot.gate import Policy
from metabot.ladder import read_ladder, rung_engine
from metabot.receipts import provenance_verdicts, verify_file
from metabot.runner import run_job
from test_checks import BUILD, _trusted
from test_engine_bridge import _bridge
from test_office_rounds import WANT, Fixer
from test_office_run import _relay

PIN = "tools@m#" + "a" * 64


def _signed(receipt) -> dict[str, str]:
    """The receipt's own verdicts, by check: what its signature covers."""
    envelope = json.loads(Path(receipt).read_text(encoding="utf-8"))
    return {v["check"]: v.get("detail", "") for v in envelope["verdicts"]}


async def _run(engine, tmp_path, **kw):
    result = await run_job(
        engine=engine,
        prompt="hello",
        cwd=tmp_path,
        policy=Policy(),
        approver=DenyAll(),
        model=kw.pop("model", "m-1"),
        **kw,
    )
    assert verify_file(result.receipt).valid
    return result


async def test_a_bridge_run_signs_the_bridge_and_its_digest(home, tmp_path):
    _bridge(home, tmp_path)
    result = await _run(get_engine("echo"), tmp_path)
    digest = get_engine("echo").digest
    assert _signed(result.receipt)["bridge"] == f"name=echo;source=config;digest={digest}"


async def test_a_run_on_a_named_endpoint_signs_where_it_went(tmp_path):
    server = FakeChat([answer("done")])
    try:
        cfg = Config(endpoints={"home": {"base_url": server.url, "model": "small"}})
        engine = rung_engine(read_ladder([{"engine": "openai", "endpoint": "home"}], cfg)[0], cfg)
        result = await _run(engine, tmp_path, model="small")
    finally:
        server.close()
    assert _signed(result.receipt)["endpoint"] == f"name=home;base_url={server.url}"


async def test_a_run_signs_the_plugins_it_had_and_its_place_in_a_ladder(home, tmp_path):
    _bridge(home, tmp_path)
    meta = {"plugins": [PIN], "ladder": "l-1", "rung": 2, "parent": "l-1.1"}
    result = await _run(get_engine("echo"), tmp_path, meta=meta)
    signed = _signed(result.receipt)
    assert signed["plugin"] == PIN
    assert signed["lineage"] == "parent=l-1.1;ladder=l-1;rung=2"


async def test_an_office_rounds_lineage_is_signed_and_so_is_its_checks(tmp_path):
    root = _trusted(tmp_path, {"tests": WANT}, desks=BUILD)
    result = await _relay(root, {"codex": Fixer()}, checks=True, rounds=3)
    first, second = result.rows
    office = result.office_run_id
    assert _signed(first.receipt)["lineage"] == f"office={office};desk=build"
    assert _signed(second.receipt)["lineage"] == (
        f"office={office};desk=build;round=2;parent={first.run_id}"
    )
    assert _signed(second.checks_receipt)["lineage"] == (
        f"office={office};desk=build;parent={second.run_id}"
    )


async def test_editing_a_signed_claim_breaks_the_receipt(home, tmp_path):
    _bridge(home, tmp_path)
    result = await _run(get_engine("echo"), tmp_path, meta={"ladder": "l-1", "rung": 1})
    path = Path(result.receipt)
    envelope = json.loads(path.read_text(encoding="utf-8"))
    for verdict in envelope["verdicts"]:
        if verdict["check"] == "lineage":
            verdict["detail"] = "ladder=l-2;rung=1"
    path.write_text(json.dumps(envelope), encoding="utf-8")
    assert not verify_file(path).valid


def test_a_value_cannot_forge_another_field():
    [lineage] = provenance_verdicts({"desk": "a;round=9", "read_only": True, "copy": None})
    assert lineage == {
        "check": "lineage",
        "result": "pass",
        "detail": "desk=a_round=9;read_only=true",
    }


def test_a_run_with_nothing_to_place_signs_no_lineage():
    assert provenance_verdicts({"engine": "codex", "prompt": "x", "read_only": False}) == []
