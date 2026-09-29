"""AD-13: the operator gate is one function, shared by both doors."""

from common.utils.src.operator import (
    STDIO_OPERATOR_ONLY,
    is_operator,
    operator_user_ids,
)


def test_nobody_is_an_operator_by_default():
    assert not is_operator("user-1", env={})
    assert not is_operator("", env={})
    assert not is_operator(None, env={})


def test_listed_ids_are_operators_trimmed():
    env = {"OPERATOR_USER_IDS": " user-1 , , user-2"}
    assert operator_user_ids(env) == frozenset({"user-1", "user-2"})
    assert is_operator("user-1", env=env)
    assert is_operator(" user-2 ", env=env)
    assert not is_operator("user-3", env=env)


def test_disable_auth_makes_everyone_an_operator():
    env = {"DISABLE_AUTH": "true"}
    assert is_operator("anyone", env=env)
    assert is_operator("dev_user", env=env)
    assert is_operator(None, env=env)
    assert not is_operator("anyone", env={"DISABLE_AUTH": "false"})


def test_message_names_the_refusal():
    assert STDIO_OPERATOR_ONLY.startswith("stdio_operator_only")
