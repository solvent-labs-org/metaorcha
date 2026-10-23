# metabot

A local bot that runs on the coding-agent plan you already have. Each call it
gates is classed read, write or destructive, and it asks you before a write
or a destructive call runs. It leaves a signed receipt for every run that
anyone can check offline. Which calls it gates depends on the engine, as the
table under Engines shows.

Part of Metaorcha. Status: pre-release, built in the open.

- **Your engines, your plan.** It drives the coding-agent command line tools
  you already sign in to, or a model server on this machine.
- **A gate on the calls that pass through it.** Reads run, writes ask, and a
  destructive call runs when you type `yes`.
- **A receipt for every run.** The receipt is signed with your key, and the
  run's journal re-derives every hash in it.
- **Offices, routines and a web page.** It runs a Code Desks office headless,
  fires routines on a schedule, and shows all of it on a page on this machine.

## Install

```sh
uv tool install orcha-metabot
```

It needs Python 3.11 or newer and a POSIX system, and it is tested on Linux.
Install and sign in to each engine you want to use, the same way you do
without MetaBot. For `claude`, sign in with `/login` or a key in the
environment: a login set in its settings file is not read.

## Getting started

```sh
metabot init        # makes a signing key if you have none, and picks a default engine
metabot doctor      # checks the engines, the key and the home folder
metabot engines     # which engines are here, and which is the default
metabot ask "what is in this directory?"
metabot receipts verify <run-id>
```

`metabot` with no command, or `metabot chat`, opens a chat in this terminal.
`metabot ask -` reads the prompt from stdin, and `--json` prints the result as
one object.

## Engines

| `-e` | What runs | Its own tool calls |
|---|---|---|
| `claude` | the `claude` command | a hook asks the gate before each one runs |
| `codex` | the `codex` command | run in the engine's own sandbox, then recorded |
| `cursor` | the `cursor-agent` command | run in the engine's own sandbox, then recorded |
| `openai` | a server that speaks the chat completions API, at `http://127.0.0.1:11434/v1` by default | run by MetaBot, each after the gate |

A `claude` run loads none of the engine's settings files, neither the
project's nor yours, so their hooks, key helpers and `env` do not apply.
MetaBot passes its gate hook and its plugins itself. From your own settings
file it keeps your model, your effort for each model, your telemetry and
error-reporting opt-outs, and your deny rules. `CLAUDE.md` files, skills,
agents, commands and plugins from the project or your engine settings are
not loaded, and your `sandbox` settings do not apply.

`-m` picks the model. `metabot config show` lists the settings that
`metabot config set` changes. A hosted endpoint is set by hand in
`config.json`, with `openai_base_url` and `openai_model`.
Its key stays in the environment variable that `openai_key_env` names, and
MetaBot reads it at call time and never stores it.

`metabot doctor --models` checks whether each model on a local server can call
MetaBot's tools. Through the engine itself, it asks each model to read a file
and say what is in it, and it keeps what it finds. A model that calls tools by
itself runs as it is. A model that writes its call as text is asked for one
step at a time, held to a schema of the tools, when it passes that way too:
text calls break in more ways in real work than in the check. When it does not
pass by schema, MetaBot reads its text calls, and the journal says so. A model
that does neither is held to the schema. A server on another machine is not
checked, because a check there costs money.

Roles map kinds of work to an engine and a model: `fast`, `build`, `review`,
`second` and `private`. `metabot roster` shows them, `metabot roster set
build --engine codex` changes one, and `metabot roster unset build` gives it
back its default. `metabot suggest TASK` names a role, an engine and a model,
with the rule that chose them. `metabot ask --role build` runs on a role, and
`metabot ask --suggest` runs on the suggestion. A plain `metabot ask` runs on
the default engine, with the `fast` role's model when that role runs there and
no model is set for the engine. The run's journal keeps what was suggested
beside what ran, and the receipt signs what ran.

`metabot ask --ladder` starts on the cheapest rung, and climbs to the next
one when the record shows the rung failed. `metabot ladder` shows the rungs.
With no `ladder` set in `config.json`, they are the local model, once
`metabot doctor --models` has found it can call tools, and then the `build`
role. A rung climbs when its run ended in an error, when a call did not
succeed, or when its declared acceptance failed. The ladder stops instead after
a declined call, after a write went through, at a limit, or when `codex` or
`cursor` failed, because their own tools may have changed the project. A task
that reads as private skips every rung that sends the work off this machine.
`--max-cost` covers the whole ladder. Each rung is a run with its own receipt,
and its journal names the ladder and the run before it. At most three rungs
run for one task, and `metabot config set ladder_attempts` changes that.

A run can end well with wrong work, and its record cannot show that. A check
can: `metabot ask --ladder --check "pytest -q"` runs the command on each rung
that reaches its end within its limits, and the check judges the work. A
failing check climbs. A passing one answers, even when a call on the way did
not succeed, such as a test the model ran before its fix. The checks are sealed as their
own run, beside the rung's, as an office desk's checks are. A check is one
line, and a command the shell classifier calls destructive never runs.

In a git checkout, each rung works in its own copy of the project: what git
tracks, with your changes not yet committed and your files that git does not
ignore. A failed rung's writes in its copy stay there, so the next rung starts
clean, and a write no longer stops the ladder. Nor does a call declined
because no one was there to ask, as in a run with `--json` or no terminal: no
person said no, and the next rung meets the same gate. A person's no still
stops it. A copy is not a sandbox: see the limits below. The rung that answers
has its work applied to your working tree, with nothing staged or committed,
and the patch is kept in `ladders/`. When the work no longer fits, because you
changed those files meanwhile, nothing is applied, and the line says where the
work is. Outside git, every rung works in place.

## Connections and approvals

A connection is an MCP server you add once and MetaBot gates on every run.

```sh
metabot connect add gh --token-env GH_TOKEN --command my-mcp-server
metabot connect add docs --url https://example.com/mcp
metabot connect scope gh       # each tool's class: read, write or destructive
metabot connect scope gh --set merge_pull_request=destructive
metabot connect test gh        # list its tools
metabot connect list
metabot connect revoke gh      # deletes its token; it cannot run again
```

`--command` takes the rest of the line, so it comes last. `--set` can make a
tool's class stricter, never looser. `scope` shows a setting for a name the
server does not list, so a misspelt one is seen.

The token is read from stdin, or asked for, and never taken as an argument.
It is kept in its own 0600 file, and it never reaches a prompt, a journal or a
receipt. Output that holds it is recorded with the token replaced.

When a call needs your answer, MetaBot asks on this terminal. Type `y` to
allow a write, or `yes` to allow a destructive call. Anything else declines.
With no terminal, every ask is declined, and the decline is recorded.
`--allow-write` runs writes without asking, never a destructive call.
`--allow gh__create_issue` runs one write tool without asking, and
`--read-only` runs the engine so it can read but not write.

A connection added with `--browser` drives a browser. On this machine's own
addresses, and on each `--origin` you add, a call is read or write by its
name. Anywhere else, or where MetaBot cannot tell, it is destructive. So is
any call that runs code in the page.

## Receipts

Every run seals a receipt when it ends, including a run that failed or was
stopped. `metabot receipts list` shows recent runs, `metabot receipts show
RUN` shows what one did, and `metabot receipts verify RUN` checks its
signature and that the kept journal re-derives every hash in it. The receipt
also checks offline with `orcha verify FILE`, a command of the `orcha-sdk`
package.

The receipt says who or what let each write through the gate. A write you
approved names your key. A write that ran under an allow you set before the
run, with `--allow-write` or `--allow` or in a routine, names that allow. A
declined call carries its reason. A read carries none, and neither do the
calls the `codex` and `cursor` engines run in their own sandbox.

A model can say a call worked when it was declined or did not succeed. So when
a run has such calls, MetaBot says how many under the answer, in the terminal
and on the run's page. When the prompt names files in the project and the run
made no tool calls, it says the receipt shows no read of them. The steps in
the receipt are MetaBot's record of the calls. The answer is the model's word.

The signing key is at `~/.orcha/key`, or where `ORCHA_KEY_PATH` points.
`metabot config set project_receipts true`, or `--project-receipt` on one run,
also copies each receipt into the project, under `.orcha/receipts/`. The copy
in the MetaBot home is the one that counts.

## Cost

Every run records what it spent: its tokens of each kind, what they cost,
and where that figure comes from. The receipt signs this as a `usage`
verdict, with a digest of the rates the run was priced by.

| The figure comes from | When |
|---|---|
| the engine | `claude` reports its own cost. Under a plan login, it is what the API would have charged. |
| the listed rates | a model in MetaBot's table, on an engine that reports no cost |
| your rates | a model you priced under `prices` in `config.json` |
| this machine | the `openai` engine on a local server, at $0.00 |
| your plan | `codex` and `cursor` bill their own login. MetaBot counts the tokens and gives no price. |

`metabot ask --max-cost 0.50` ends the run at its next tool call once it has
cost that much. The receipt signs the cap and what was spent as a `budget`
verdict. A run that MetaBot cannot price never starts under a cap, until you
give its model a price. `metabot cost` totals past runs by engine and model,
from the run index.

A price is set by hand in `config.json`, in dollars per million tokens.
`cache_read` and `cache_write` default to the input rate:

```json
"prices": {"my-model": {"in": 0.05, "out": 0.2}}
```

### What a check proves

A check proves two things: the key named in the receipt signed it, and
nothing in it changed after that. With the run's journal, which stays on the
machine that ran it, `metabot receipts verify` also shows that the journal
matches every hash.

A check does not prove that the steps happened. Whoever holds the key can
sign steps that never ran, and so can a changed copy of MetaBot. Any program
with the key can write the same fields, so a receipt cannot show which
program made it. Its times come from that machine's clock. So a receipt from
someone else is as good as your trust in their key.

## Offices

MetaBot reads a Code Desks office, `office.yaml`, and runs its desks headless.
Code Desks' `desk init` writes one, or you can write it at the top of your
repository:

```yaml
name: my-office
gates:
  done:
    owner: [verdict]
    engineer: [tests]
    checks:
      tests: make test     # runs with --checks, once you trust it
desks:
  build:
    role: roles/build.md   # the desk's instructions, a file in the repository
    harness: claude        # claude, codex, cursor, custom or any
  second:
    role: roles/build.md
    harness: codex
  local:
    role: roles/build.md
    harness: any
    engine: openai         # the model server MetaBot's openai engine uses
  review:
    role: roles/review.md
    harness: codex
    advises: [build]       # reads build's work when it is done, and changes no file
```

```sh
metabot office check            # the desks and checks it reads, and what cannot run
metabot office trust            # shows the gate checks, and records that you agree to run them
metabot office run "add a health check endpoint"
metabot office run --fan "add a health check endpoint"
metabot office run --desks build,local "add a health check endpoint"   # these desks in this order, and their advisors
metabot desks list
metabot desks pick <office-run>/build
metabot desks clean <office-run>
```

Each desk works in its own git worktree, on its own branch under `metabot/`.
In a relay, each desk starts from the branch of the desk before it, and gets
the earlier desks' answers as files. `--fan` runs the desks at once from one
base, so you can compare their work. A desk with `fan: 3` runs as three
copies at once, each on its own branch. This machine's `fan_cap` bounds how
many desk runs go at once: three unless you change it with `metabot config
set fan_cap`, and five at most. A desk with `advises` reviews another desk's
work once that desk is done. It runs so it can read but not write, and it
files nothing. Advisors run in a relay, not in a fan.

A desk's `kit` loads into its runs. MetaBot looks for each skill where Code
Desks does: the office's `.office/skills`, then the project's `.claude/skills`
and `.agents/skills`, then the same two folders in your home. It copies the
skills for the office run and gives them to the engine the way it gives a
plugin, and removes the copies when the office run ends. A kit entry is a
skill's name, never a path. A skill from the project must stay inside the
project, and a skill's hooks and servers are left out. A kit that cannot load
stops the office run before any desk starts.

`--checks` runs the office's gate checks after each desk that changed a file,
as their own run with their own receipt. A desk whose checks fail stops the
relay. A check runs a command from the office file on your machine, so none
runs until `metabot office trust` has shown it and you agreed.

MetaBot never pushes, and it never commits to your branch. It commits each
desk's work on the desk's own branch. `metabot desks pick` stages one desk's
work in your checkout as a squash, and you commit it. `metabot receipts show`
on an office run prints its tree of desks.

## Mail between sessions

Two agent sessions can leave each other notes, whichever harness each one runs
in. A mailbox is a name you give a session.

```sh
metabot mail send builder --from main -m "the shell suite is yours next"
metabot mail read --for builder               # prints the notes and marks them read
metabot mail wait --for main --timeout 900    # waits for a note, prints it, exits
```

A note is a file in `mail/<name>/`, in the shape of Code Desks' desk mail, and
it is read once. A harness hook brings a session its mail on its own. For
`claude`, put `metabot mail hook claude <event> --for NAME` on its
`UserPromptSubmit`, `PostToolUse` and `Stop` hooks, with the events `prompt`,
`tool` and `stop`. For `cursor-agent`, put `metabot mail hook cursor <event>
--for NAME` on `beforeSubmitPrompt`, `postToolUse` and `stop`. At a prompt, or
after a tool call, the hook adds the mail to the turn. At stop it hands the
mail back as the next turn, unless you stopped the turn yourself. A hook that
cannot read the mail adds nothing, and the turn goes on.

A note is shown as a note from a session, never as your word. It approves
nothing: a write still asks wherever it runs.

## Plugins

Plugins carry skills, commands and agents, in the `.claude-plugin` folder
format. MetaBot pins each one by the hash of its files. This version installs
from folders on this machine.

```sh
metabot plugin marketplace add mine ~/my-plugins
metabot plugin marketplace list
metabot plugin install code-desks@metabot   # shows what it carries, then installs it, off
metabot plugin enable code-desks@metabot
metabot plugin list
metabot plugin show code-desks@metabot
metabot plugin import-skills --name my-skills ~/my-skills
```

`install` lists what a plugin carries before it installs: skills, commands,
agents, hooks and servers. Installing another version of a plugin you have
shows what changes. A plugin's hooks run outside MetaBot's gate, so
`metabot plugin enable` asks you to allow them or leave them out. Its servers
never start on their own. `--add-servers` makes them connections, behind the
gate. The `metabot` marketplace ships with the package and holds the
`bizopsmap` and `code-desks` plugins.

## Routines and serve

A routine is a goal that runs on a schedule, with no one there to ask.

```sh
metabot routine save triage --cron '0 9 * * 1-5' --tz Europe/London \
  --connection gh --allow gh__create_issue "Triage the new issues."
metabot routine list            # when each is due, and how it last fired
metabot routine run triage      # fire it now
metabot routine firings
metabot routine disable triage
metabot serve                   # fire routines as they come due
```

The routine uses the `gh` connection added above, so `gh` has to be active
when you save it: a revoked connection cannot go in a routine.

A firing is written down before its run starts. It declines every write you
did not allow when you saved the routine, and every destructive call.
`--criteria exit_zero` judges every step and seals the verdict in the
receipt. `metabot serve` holds a lock, so one serve runs per home, and it
fires one routine at a time. A due time missed while serve was down is not
fired later. After a restart, a firing that never sealed is marked an error.

## The web UI

```sh
metabot ui
```

It prints an address on `127.0.0.1` that carries a token made at start. The
page shows the runs, each run's steps and approvals, an office run's tree of
desks, the connections and the routines. From its chat you start a run and
answer its asks: Allow for a write, and the word `yes` for a destructive
call. From its office page you start an office run, with the desks and the
mode you pick. Each desk's asks wait on the run's page, and each names its
desk. Its pages hold no script. The address stops working when `metabot ui`
stops, and every run it started is stopped and sealed.

## Files it keeps

Everything lives in `~/.metabot`, or where `METABOT_HOME` points. That folder
is 0700, and so is each folder MetaBot makes for its records. Its settings,
records and tokens are 0600. Desk worktrees, installed plugins and kits are
copies of files, with the modes your umask gives them, inside those 0700
folders.

| Path | What it holds |
|---|---|
| `config.json` | your settings |
| `runs/<run>/journal.json`, `receipts/<run>.json`, `runs.jsonl` | each run's journal, its signed receipt, and the index |
| `connections.json`, `secrets/` | your connections, and their tokens |
| `plugins.json`, `marketplaces.json`, `plugins/` | plugins and where they come from |
| `routines.json`, `firings.jsonl`, `serve.lock` | routines and what each firing did |
| `offices/`, `desks/`, `trust/` | office runs, desk worktrees and ladder copies, and the checks you trusted |
| `ladders/` | each ladder's rungs, how each one ended, and the patch it applied |
| `kits/` | the desks' kits, while their office run goes |
| `mail/` | each mailbox's notes, and `read/` for the ones read |

## Limits

- **A connection's server runs with your privileges.** MetaBot classes and
  gates each call the server receives, but the server's own process can do
  whatever you can.
- **`--allow-write` lets an engine run code the classifier cannot see into.**
  A command such as running a script is a write, and once allowed it runs
  whole, whatever the script does. A destructive call still asks.
- **Reads never ask.** That includes a shell read of a credential file, such
  as an SSH key or a cloud credentials file. It also includes your signing
  key, and a key that leaves the machine can sign receipts as you.
- **A receipt shows who signed it, not that its steps happened.** Whoever
  holds the key can sign steps that never ran, with any program. Nothing in
  the receipt shows which program made it.
- **The `codex` and `cursor` engines run their own tools in their own
  sandbox.** MetaBot records those calls after they run, and its gate covers
  the calls to your connections. A routine runs these engines so they cannot
  write. Past a run's cap on tool calls, such an engine may already have run
  the call the run stopped at.
- **A plugin's hooks, once you allow them, run outside the gate.**
- **A routine cannot run a project's own test command.** A shell command is a
  write, and a routine's allows name connection tools, so it declines.
- **A rung's copy leaves out ignored files,** such as `.venv`, `node_modules`
  or `.env`. A check that needs them fails in the copy.
- **A rung's copy is not a sandbox.** The `openai` engine's file tools refuse
  any path outside the copy. Any write the gate allows to an absolute path
  reaches that path: a `shell` command such as `echo x >> /some/file` under
  `--allow-write`, or an engine CLI's own tools.
- **Some local models answer tool calls as text.** MetaBot makes such a call
  when the whole answer is the call, and fails the run when it cannot read
  one. It reads Python's triple quotes left unescaped inside a JSON string as
  text. When an answer is calls one after another and the last one breaks off
  before its JSON ends, MetaBot makes the complete ones and never the broken
  one. A call in a code fence inside a longer answer is never made: MetaBot
  asks the model for the call alone, twice at most, and then fails the run.
  A small model held to a schema can still pick the wrong step. The gate
  stops its writes, and `metabot doctor --models` says which models can call
  tools at all.
- **On a `claude` run, a shell step carries no exit code the `exit_zero`
  criterion can read.** A failed command is recorded as text, so a run on
  this engine that declares `exit_zero` fails it.
- **The browser policy cannot see a click that moves the page.** A call is
  classed by the address it carries, or by where the page was last sent. A
  click or a redirect that moves the page to another site is not seen, so
  under `--allow-write` the typing after it runs unasked.
- **A `claude` run's cost is counted message by message, and its result
  replaces the count.** The running count can be lower than the result, so a
  cap can stop a `claude` run later than the cap says. The receipt still
  signs the result's figure and the cap that was passed.
- **A `cursor` run reports what it used when it ends, and not before.** A cap
  on one needs your price for its model, and it is checked at the end.
- **Project copies of receipts show as untracked files** in your repository.
  Commit them or ignore them. MetaBot leaves `.gitignore` alone.
- **Your browser sends the web UI's cookie to every port on 127.0.0.1** while
  the UI runs. Another local server you open in that time gets it, and could
  use the UI as you: start a run and answer its asks. The cookie stops working
  when the UI stops.

## Licence

Apache-2.0. See `LICENSE`.
