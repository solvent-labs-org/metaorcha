"""Reading a tool call a local model wrote badly, or not at all."""

from __future__ import annotations

import json

import pytest

from metabot.toolcalls import call_shaped, calls_in_text, parse_args, resolve_name, step_in_text

OFFERED = ("read_file", "list_dir", "write_file", "shell", "gh__create_issue")


@pytest.mark.parametrize(
    "text",
    [
        '{"name": "read_file", "arguments": {"path": "a.txt"}}',
        '```json\n{"name": "read_file", "arguments": {"path": "a.txt"}}\n```',
        '```\n{"name": "read_file", "arguments": {"path": "a.txt"}}\n```',
        '<tool_call>\n{"name": "read_file", "arguments": {"path": "a.txt"}}\n</tool_call>',
        '{"name": "read_file", "parameters": {"path": "a.txt"}}',
        '{"tool": "read_file", "args": {"path": "a.txt"}}',
        '{"name": "read_file", "input": {"path": "a.txt"}}',
        '{"name": "read_file", "arguments": "{\\"path\\": \\"a.txt\\"}"}',
        '{"function": {"name": "read_file", "arguments": "{\\"path\\": \\"a.txt\\"}"}}',
        '{"type": "function", "function": {"name": "read_file", "arguments": {"path": "a.txt"}}}',
        '{\n    "name": "read_file",\n    "arguments": {\n        "path": "a.txt"\n    }\n}',
    ],
)
def test_one_call_written_as_the_whole_text_is_read(text):
    assert calls_in_text(text, OFFERED) == [("read_file", {"path": "a.txt"}, "read_file")]


def test_several_calls_one_after_another_or_in_a_list_are_read():
    two = [("list_dir", {"path": "."}, "list_dir"), ("read_file", {"path": "b"}, "read_file")]
    loose = (
        '{"name": "list_dir", "arguments": {"path": "."}}\n'
        '{"name": "read_file", "arguments": {"path": "b"}}'
    )
    listed = json.dumps(
        [
            {"name": "list_dir", "arguments": {"path": "."}},
            {"name": "read_file", "arguments": {"path": "b"}},
        ]
    )
    assert calls_in_text(loose, OFFERED) == two
    assert calls_in_text(listed, OFFERED) == two


def test_a_call_with_no_arguments_takes_an_empty_object():
    assert calls_in_text('{"name": "list_dir"}', OFFERED) == [("list_dir", {}, "list_dir")]


@pytest.mark.parametrize(
    "text",
    [
        "The file says hello.",
        'Here is the call: {"name": "read_file", "arguments": {"path": "a"}}',  # prose around it
        '{"name": "read_file", "arguments": {"path": "a"}} and then I will answer',
        '{"name": "no_such_tool", "arguments": {}}',
        '{"name": "read_file", "arguments": [1, 2]}',
        '{"name": "read_file", "arguments": "not json"}',
        # all or none
        '[{"name": "read_file", "arguments": {}}, {"name": "nope", "arguments": {}}]',
        '{"answer": 4}',
        "[]",
        "",
        "{",
    ],
)
def test_text_that_is_not_wholly_readable_calls_is_not_read_as_calls(text):
    assert calls_in_text(text, OFFERED) is None


@pytest.mark.parametrize(
    ("asked", "resolved"),
    [
        ("read_file", "read_file"),
        ("Read_File", "read_file"),
        ("read-file", "read_file"),
        ("readFile", "read_file"),
        ("ls", "list_dir"),
        ("list_directory", "list_dir"),
        ("cat", "read_file"),
        ("bash", "shell"),
        ("read_fil", "read_file"),  # one letter off
        ("gh__create_isue", "gh__create_issue"),
    ],
)
def test_a_near_miss_name_resolves_to_the_tool_offered(asked, resolved):
    assert resolve_name(asked, OFFERED) == resolved


@pytest.mark.parametrize("asked", ["", "delete_everything", "read", "write", "file"])
def test_a_name_too_far_from_any_tool_or_ambiguous_resolves_to_nothing(asked):
    # "read" and "write" are aliases only where one tool is meant; "file" is in three names
    assert (
        resolve_name(asked, ("read_file", "read_dir", "list_dir", "write_file", "write_dir"))
        is None
    )


def test_an_alias_needs_its_tool_offered():
    assert resolve_name("bash", ("read_file",)) is None  # no shell in a read-only run


@pytest.mark.parametrize(
    ("raw", "args"),
    [
        ({"path": "a"}, {"path": "a"}),
        ('{"path": "a"}', {"path": "a"}),
        ('```json\n{"path": "a"}\n```', {"path": "a"}),
        ('"{\\"path\\": \\"a\\"}"', {"path": "a"}),  # encoded twice
        ("", {}),
        (None, {}),
        ("[1]", None),
        ("nope", None),
        (3, None),
    ],
)
def test_parse_args_takes_what_models_send(raw, args):
    assert parse_args(raw) == args


@pytest.mark.parametrize(
    ("text", "shaped"),
    [
        ('{"name": "rm_rf", "arguments": {}}', True),
        ('```json\n{"tool": "x", "parameters": {"a": 1}}\n```', True),
        ('{"name": "Ada", "age": 3}', False),  # a name but no arguments: an answer
        ("plain words", False),
        ('{"arguments": {}}', False),
    ],
)
def test_call_shaped_spots_an_answer_that_is_an_unread_call(text, shaped):
    assert call_shaped(text) is shaped


@pytest.mark.parametrize(
    ("text", "step"),
    [
        ('{"tool": "read_file", "arguments": {"path": "a"}}', ("read_file", {"path": "a"})),
        ('{"tool": "answer", "arguments": {"text": "4"}}', ("answer", {"text": "4"})),
        (
            '```json\n{"tool": "list_dir", "arguments": {"path": "."}}\n```',
            ("list_dir", {"path": "."}),
        ),
        ('{"tool": "lst_dir", "arguments": {"path": "."}}', ("list_dir", {"path": "."})),
        ('{"tool": "nope", "arguments": {}}', None),
        ("4", None),
    ],
)
def test_a_constrained_step_is_read_as_one_tool_or_the_answer(text, step):
    assert step_in_text(text, OFFERED) == step
