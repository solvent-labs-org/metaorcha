"""Reading a tool call a local model wrote badly, or not at all."""

from __future__ import annotations

import json

import pytest

from metabot.toolcalls import (
    call_shaped,
    calls_in_fences,
    calls_in_text,
    leading_calls,
    parse_args,
    resolve_name,
    step_in_text,
)

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


# ── Python's triple quotes inside a JSON string ─────────────────────────────

# What qwen2.5-coder:7b wrote on 10-22: a docstring's quotes, not escaped.
DOCSTRING = (
    '{"name": "write_file", "arguments": {"path": "stats.py", '
    '"content": "def m(v):\\n    """The middle."""\\n    return v\\n"}}'
)


@pytest.mark.parametrize("text", [DOCSTRING, "```json\n" + DOCSTRING + "\n```"])
def test_triple_quotes_inside_a_json_string_are_read_as_text(text):
    with pytest.raises(ValueError):  # as written, it is not valid JSON
        json.loads(text.removeprefix("```json\n").removesuffix("\n```"))
    content = 'def m(v):\n    """The middle."""\n    return v\n'
    assert calls_in_text(text, OFFERED) == [
        ("write_file", {"path": "stats.py", "content": content}, "write_file")
    ]


def test_an_escaped_quote_right_before_triple_quotes_stays_one_quote():
    # A docstring that opens with a quote, its first quote escaped and the
    # rest not: the escaped one is not part of the three.
    text = (
        '{"name": "write_file", "arguments": {"path": "a", "content": "\\""""Quoted word."""\\n"}}'
    )
    assert calls_in_text(text, OFFERED) == [
        ("write_file", {"path": "a", "content": '""""Quoted word."""\n'}, "write_file")
    ]


def test_a_valid_call_with_escaped_quotes_reads_as_it_always_did():
    valid = json.dumps({"name": "write_file", "arguments": {"path": "a", "content": '"""x"""'}})
    assert '\\"\\"\\"' in valid
    assert calls_in_text(valid, OFFERED) == [
        ("write_file", {"path": "a", "content": '"""x"""'}, "write_file")
    ]


@pytest.mark.parametrize(
    "text",
    [
        '{"name": """, "arguments": {}}',  # quotes where a name should be
        '{"name": "write_file", "arguments": {"path": "a", "content": """}}',
    ],
)
def test_triple_quotes_that_do_not_repair_into_a_call_read_nothing(text):
    assert calls_in_text(text, OFFERED) is None


# ── a call in a code fence inside a longer answer ───────────────────────────

FENCED_WRITE = '```json\n{"name": "write_file", "arguments": {"path": "a", "content": "x"}}\n```'
FENCED_READ = '```json\n{"name": "read_file", "arguments": {"path": "a"}}\n```'


@pytest.mark.parametrize(
    ("text", "named"),
    [
        (f"I will write it:\n\n{FENCED_WRITE}\n\nDone.", ["write_file"]),
        # what a 7B desk wrote in the 10-22 fan: a read, then a placeholder write
        (
            f"First:\n{FENCED_READ}\nThen you can run:\n{FENCED_WRITE}\n",
            ["read_file", "write_file"],
        ),
        ("Here is the fix:\n```python\ndef f():\n    return 1\n```\n", []),
        ('Use this: {"name": "read_file", "arguments": {"path": "a"}} in your config.', []),
        (FENCED_READ, []),  # the whole answer is the call: read as a call
        ('Try:\n```json\n{"name": "no_such_tool", "arguments": {}}\n```\n', []),
    ],
)
def test_calls_in_fences_names_a_call_inside_a_longer_answer(text, named):
    assert calls_in_fences(text, OFFERED) == named


# ── calls one after another, the last one broken off ────────────────────────

LIST = '{"name": "list_dir", "arguments": {"path": "."}}'
WRITE = '{"name": "write_file", "arguments": {"path": "a", "content": "x"}}'
BROKEN = '{"name": "shell", "arguments": {"command":"git commit -m \'msg\'}}'  # never closes


def test_the_complete_calls_before_one_that_breaks_off_are_read():
    # What qwen2.5-coder:7b wrote on 10-22: its fix, then a commit that broke off.
    text = f"{LIST}\n{WRITE}\n{BROKEN}"
    assert calls_in_text(text, OFFERED) is None  # all or nothing, as before
    assert leading_calls(text, OFFERED) == [
        ("list_dir", {"path": "."}, "list_dir"),
        ("write_file", {"path": "a", "content": "x"}, "write_file"),
    ]


def test_leading_calls_mends_triple_quotes_before_the_break():
    docstring = DOCSTRING  # a whole call whose content holds unescaped triple quotes
    read = leading_calls(f"{docstring}\n{BROKEN}", OFFERED)
    assert read is not None and read[0][0] == "write_file" and '"""' in read[0][1]["content"]


@pytest.mark.parametrize(
    "text",
    [
        f"{LIST}\n{WRITE}",  # it all parses: calls_in_text decides
        f"{LIST}\n{WRITE}\n",  # the same, with a newline at the end
        f"{LIST} and then I will answer",  # prose after a call
        f'{LIST}\n{{"name": "no_such_tool", "arguments": {{}}}}\n{BROKEN}',  # all or none
        f'{LIST}\n{{"answer": 4}}\n{BROKEN}',  # an object that is not a call
        BROKEN,  # nothing complete before the break
        f"{LIST}\n[1, 2",  # what broke off is not an object
        "",
    ],
)
def test_leading_calls_reads_nothing_unless_the_answer_is_calls_until_a_break(text):
    assert leading_calls(text, OFFERED) is None
