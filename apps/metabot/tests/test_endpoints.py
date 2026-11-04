"""Named endpoints: a ladder rung names the endpoint it calls, so a task can climb from a
local model to a hosted one. A rung's key is read from its own variable, and only that one."""

from __future__ import annotations

import asyncio
import json

import pytest

from fake_chat import FakeChat, answer
from metabot.config import Config, ConfigError, endpoint_of, load_config, save_config
from metabot.ladder import LadderError, Rung, read_ladder, rung_engine

HOME = "http://127.0.0.1:11434/v1"
HOSTED = "https://hosted.example.test/v1"


def cfg_with(**endpoints) -> Config:
    return Config(openai_base_url=HOME, openai_model="qwen2.5:3b", endpoints=dict(endpoints))


# ── tiers and reading the ladder ────────────────────────────────────────────


def test_a_loopback_endpoint_is_local_and_a_remote_one_is_paid():
    cfg = cfg_with(
        home={"base_url": HOME, "model": "qwen2.5:3b"},
        hosted={"base_url": HOSTED, "model": "big", "key_env": "HOSTED_KEY"},
    )
    rungs = read_ladder(
        [{"engine": "openai", "endpoint": "home"}, {"engine": "openai", "endpoint": "hosted"}], cfg
    )
    assert rungs == [
        Rung("openai", "qwen2.5:3b", "local", "home"),
        Rung("openai", "big", "paid", "hosted"),
    ]


def test_an_explicit_tier_on_an_endpoint_wins_over_its_host():
    # a loopback port that forwards to a paid provider is paid, whatever its address says
    cfg = cfg_with(tunnel={"base_url": HOME, "model": "big", "tier": "paid"})
    assert read_ladder([{"engine": "openai", "endpoint": "tunnel"}], cfg) == [
        Rung("openai", "big", "paid", "tunnel")
    ]


def test_the_default_endpoint_is_the_openai_settings_already_in_config():
    cfg = Config(openai_base_url=HOME, openai_model="qwen2.5:3b")
    assert read_ladder([{"engine": "openai", "endpoint": "default"}], cfg) == [
        Rung("openai", "qwen2.5:3b", "local", "default")
    ]


def test_a_rung_naming_an_endpoint_the_config_does_not_have_is_refused():
    with pytest.raises(LadderError, match="ladder rung 1.*nope"):
        read_ladder([{"engine": "openai", "endpoint": "nope"}], cfg_with())


def test_an_endpoint_on_an_engine_other_than_openai_is_refused():
    cfg = cfg_with(home={"base_url": HOME, "model": "qwen2.5:3b"})
    with pytest.raises(LadderError, match="openai"):
        read_ladder([{"engine": "claude", "endpoint": "home"}], cfg)


def test_a_hosted_endpoint_with_no_model_anywhere_is_refused():
    cfg = Config(openai_base_url=HOME, openai_model="", endpoints={"hosted": {"base_url": HOSTED}})
    with pytest.raises(LadderError, match="model"):
        read_ladder([{"engine": "openai", "endpoint": "hosted"}], cfg)


# ── the config table ────────────────────────────────────────────────────────


@pytest.fixture
def home(tmp_path, monkeypatch):
    monkeypatch.setenv("METABOT_HOME", str(tmp_path))
    return tmp_path


def write_settings(home, endpoints):
    (home / "config.json").write_text(json.dumps({"endpoints": endpoints}), encoding="utf-8")


def test_endpoints_round_trip_through_the_settings_file(home):
    cfg = cfg_with(hosted={"base_url": HOSTED, "model": "big", "key_env": "HOSTED_KEY"})
    save_config(cfg)
    assert load_config().endpoints == cfg.endpoints


def test_a_key_value_where_a_variable_name_belongs_is_refused(home):
    write_settings(
        home, {"hosted": {"base_url": HOSTED, "model": "big", "key_env": "sk-live-abc123"}}
    )
    with pytest.raises(ConfigError, match="endpoints"):
        load_config()


@pytest.mark.parametrize(
    "endpoints",
    [
        {"Home Lab": {"base_url": HOME, "model": "m"}},  # a name with a space and capitals
        {
            "default": {"base_url": HOME, "model": "m"}
        },  # reserved: the settings the config already has
        {"ftp": {"base_url": "ftp://files.example.test", "model": "m"}},  # not http or https
        {"nohost": {"base_url": "https://", "model": "m"}},  # no host
        {"typo": {"base_url": HOME, "model": "m", "keyenv": "X"}},  # an unknown key is an error
        {"badtier": {"base_url": HOME, "model": "m", "tier": "free"}},  # tiers: local or paid
    ],
)
def test_a_bad_endpoint_is_refused_when_the_settings_are_read(home, endpoints):
    write_settings(home, endpoints)
    with pytest.raises(ConfigError):
        load_config()


def test_endpoint_of_names_the_endpoint_or_refuses_it():
    cfg = cfg_with(hosted={"base_url": HOSTED, "model": "big", "key_env": "HOSTED_KEY"})
    ep = endpoint_of(cfg, "hosted")
    assert (ep.base_url, ep.model, ep.key_env, ep.tier) == (HOSTED, "big", "HOSTED_KEY", None)
    with pytest.raises(ConfigError, match="hosted2"):
        endpoint_of(cfg, "hosted2")


# ── the engine a rung runs on ───────────────────────────────────────────────


def test_a_rung_builds_an_engine_on_its_endpoints_address_model_and_key(monkeypatch):
    monkeypatch.setenv("HOSTED_KEY", "sk-test-value-123")
    cfg = cfg_with(hosted={"base_url": HOSTED, "model": "big", "key_env": "HOSTED_KEY"})
    rung = read_ladder([{"engine": "openai", "endpoint": "hosted"}], cfg)[0]
    engine = rung_engine(rung, cfg)
    assert (engine.base_url, engine.model, engine.key_env) == (HOSTED, "big", "HOSTED_KEY")
    assert engine.redaction_map() == {"HOSTED_KEY": "sk-test-value-123"}


def test_an_endpoint_without_a_key_never_sends_the_default_key(monkeypatch):
    # The default key is for the default endpoint. A second host with no key of its own
    # must not receive it, so its engine has no key variable at all.
    monkeypatch.setenv("DEFAULT_KEY", "sk-default-999")
    cfg = Config(
        openai_base_url=HOME,
        openai_model="qwen2.5:3b",
        openai_key_env="DEFAULT_KEY",
        endpoints={"local2": {"base_url": "http://localhost:8080/v1", "model": "small"}},
    )
    rung = read_ladder([{"engine": "openai", "endpoint": "local2"}], cfg)[0]
    engine = rung_engine(rung, cfg)
    assert not engine.key_env
    assert engine.redaction_map() == {}


def test_a_rung_without_an_endpoint_runs_on_the_openai_settings_the_ladder_read():
    cfg = Config(openai_base_url=HOME, openai_model="qwen2.5:3b", openai_key_env="DEFAULT_KEY")
    save_config(Config(openai_base_url=HOSTED, openai_key_env="OTHER_KEY"))  # changed since
    engine = rung_engine(Rung("openai", None, "local"), cfg)
    assert (engine.base_url, engine.model, engine.key_env) == (HOME, "qwen2.5:3b", "DEFAULT_KEY")
    assert engine.endpoint is None
    assert rung_engine(Rung("claude", "haiku", "sub"), cfg) is None


# ── what the record shows ───────────────────────────────────────────────────


def test_a_rung_labels_and_records_the_endpoint_it_used():
    rung = Rung("openai", "big", "paid", "hosted")
    assert rung.label() == "openai@hosted/big"
    assert rung.entry() == {"engine": "openai", "model": "big", "endpoint": "hosted"}


def test_a_rung_without_an_endpoint_is_labelled_and_recorded_as_before():
    assert Rung("openai", "qwen2.5:3b", "local").label() == "openai/qwen2.5:3b"
    assert Rung("openai", "qwen2.5:3b", "local").entry() == {
        "engine": "openai",
        "model": "qwen2.5:3b",
    }


def test_each_rung_sends_its_request_to_its_own_endpoint_with_its_own_key_only(monkeypatch):
    monkeypatch.setenv("HOME_KEY", "sk-home-111")
    monkeypatch.setenv("HOSTED_KEY", "sk-hosted-222")
    monkeypatch.setenv("DEFAULT_KEY", "sk-default-333")  # the default key: no endpoint here uses it
    home_srv, hosted_srv, bare_srv = (
        FakeChat([answer("a")]),
        FakeChat([answer("b")]),
        FakeChat([answer("c")]),
    )
    try:
        cfg = Config(
            openai_base_url="http://127.0.0.1:9/v1",  # not used: no rung here names the default
            openai_model="unused",
            openai_key_env="DEFAULT_KEY",
            endpoints={
                "home": {"base_url": home_srv.url, "model": "small", "key_env": "HOME_KEY"},
                "hosted": {"base_url": hosted_srv.url, "model": "big", "key_env": "HOSTED_KEY"},
                "bare": {"base_url": bare_srv.url, "model": "plain"},  # no key of its own
            },
        )
        for name in ("home", "hosted", "bare"):
            rung = read_ladder([{"engine": "openai", "endpoint": name}], cfg)[0]
            asyncio.run(rung_engine(rung, cfg)._post({"model": rung.model, "messages": []}))

        assert home_srv.headers[0]["Authorization"] == "Bearer sk-home-111"
        assert hosted_srv.headers[0]["Authorization"] == "Bearer sk-hosted-222"
        assert "Authorization" not in bare_srv.headers[0]  # the default key never reaches it
        assert [s.requests[0]["model"] for s in (home_srv, hosted_srv, bare_srv)] == [
            "small",
            "big",
            "plain",
        ]
        for server in (home_srv, hosted_srv, bare_srv):
            assert "sk-default-333" not in json.dumps(server.headers)
    finally:
        for server in (home_srv, hosted_srv, bare_srv):
            server.close()
