"""Loops in the web UI: the office form asks for rounds and a cap; the office page shows them."""

from __future__ import annotations

import pytest

from metabot.checks import record_trust
from metabot.office import load_office
from test_checks import BUILD, _set_checks
from test_office_rounds import WANT, Fixer
from test_office_run import _repo
from test_web_office import _record, _send, _start, office_ui
from test_web_runs import _ended, _until


def _looped(tmp_path):
    root = _repo(tmp_path, BUILD)
    _set_checks(root, {"tests": WANT})
    record_trust(load_office(root))
    return root


def test_the_office_form_offers_a_loop_and_a_cap(tmp_path):
    root = _looped(tmp_path)
    with office_ui(root, {"codex": Fixer()}) as (client, guard):
        page = client.get("/office").text
    assert 'name="until_checks"' in page and 'name="rounds"' in page
    assert 'name="max_cost"' in page


def test_a_loop_started_in_the_page_runs_its_rounds_and_shows_them(tmp_path):
    root = _looped(tmp_path)
    fixer = Fixer()
    with office_ui(root, {"codex": fixer}) as (client, guard):
        office_id = _start(client, guard, root, "build", until_checks="1", rounds="3")
        _until(client, office_id, _ended)
        page = client.get(f"/offices/{office_id}").text
    record = _record(office_id)
    assert record["rounds"] == 3 and len(fixer.jobs) == 2
    assert [d["run_id"] for d in record["desks"]] == [f"{office_id}.build", f"{office_id}.build.r2"]
    assert "build r2" in page and "the checks passed in round 2" in page
    assert "<th>rounds</th><td>at most 3</td>" in page
    assert f'href="/runs/{office_id}.build.r2"' in page  # each round links to its own run


@pytest.mark.parametrize(
    ("fields", "said"),
    [
        ({"rounds": "3"}, "rounds go with until the checks pass"),
        ({"until_checks": "1", "rounds": "11"}, "between 1 and 10"),
        ({"until_checks": "1", "rounds": "two"}, "rounds must be a whole number"),
        ({"max_cost": "lots"}, "max cost must be dollars"),
        ({"max_cost": "1"}, "has no price"),
    ],
)
def test_a_loop_form_that_cannot_start_says_why(tmp_path, home, fields, said):
    root = _looped(tmp_path)
    fixer = Fixer()
    with office_ui(root, {"codex": fixer}) as (client, guard):
        page = _send(client, guard, root, ("build",), **fields)
    assert page.status_code == 400 and said in page.text
    assert not fixer.jobs and not (home / "offices").exists()


def test_the_office_page_shows_the_cap_and_what_was_spent(tmp_path):
    from metabot.config import Config, save_config
    from test_office_rounds import LOOP, PRICES

    save_config(Config(prices=PRICES))
    root = _repo(tmp_path, LOOP)
    _set_checks(root, {"tests": WANT})
    record_trust(load_office(root))
    fixer = Fixer("claude", right=99, usage={"prompt_tokens": 100, "completion_tokens": 0})
    with office_ui(root, {"claude": fixer}) as (client, guard):
        office_id = _start(
            client, guard, root, "build", until_checks="1", rounds="5", max_cost="0.0002"
        )
        _until(client, office_id, _ended)
        page = client.get(f"/offices/{office_id}").text
    assert "<th>money cap</th><td>$0.0002, spent $0.0002</td>" in page
    assert "is spent" in page


def test_the_office_page_says_when_a_cost_under_the_cap_is_unknown(tmp_path):
    from metabot.config import Config, save_config
    from test_office_rounds import LOOP, PRICES

    save_config(Config(prices=PRICES))
    root = _repo(tmp_path, LOOP)
    _set_checks(root, {"tests": WANT})
    record_trust(load_office(root))
    quiet = Fixer("claude", right=99)  # its model has a price, and it reports no usage
    with office_ui(root, {"claude": quiet}) as (client, guard):
        office_id = _start(
            client, guard, root, "build", until_checks="1", rounds="5", max_cost="0.0002"
        )
        _until(client, office_id, _ended)
        page = client.get(f"/offices/{office_id}").text
    said = "$0.0002, spent at least $0.00: the cost of build is unknown"
    assert f"<th>money cap</th><td>{said}</td>" in page
    assert "cannot be kept" in page and len(quiet.jobs) == 1
