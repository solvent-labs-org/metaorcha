# Changelog: MetaBot

All notable changes to MetaBot (`apps/metabot`) are documented here. Format
follows [Keep a Changelog](https://keepachangelog.com/en/1.1.0/); MetaBot uses
[Semantic Versioning](https://semver.org/).

## [0.3.0] — 2026-11-01

### Added

- On Linux with bubblewrap (`bwrap`), each engine command starts with the
  instructions outside its project hidden, inside `bwrap` whenever there is
  something to hide: `AGENTS.md`, `CLAUDE.md`,
  `.cursor/rules` and skill folders in the folders above the project, and
  each engine's own user rules, skills, plugins, hooks and memory. A hidden
  file reads as empty, and a hidden folder is empty and read-only, so an
  engine cannot download its account's plugins into it. The project is the
  nearest folder that holds a git repository, and never your home folder: a
  run in your home folder has no project, so what it would read there is
  hidden too. Inside the sandbox `sudo` and other setuid programs fail. The
  receipt signs how many were hidden, or how many were left in view without
  `bwrap` or with `"sandbox": false` in `config.json`, or that the run was
  refused. A `cursor` run without `bwrap` refuses unless the sandbox is off.
- `metabot engines --manifest` shows, for each engine, how it takes a turn
  without writing, the model each role runs on it, what the sandbox does in
  the folder `--cwd` names (here by default) and, in a git project, in a
  copy, where an office desk or a ladder rung works, and what still reaches
  it, such as the user rules of the account `cursor` signs in with. For
  `openai` it lists each endpoint with its model and tier. It starts no
  engine, and leaves the folder as it is.
- `endpoints` in `config.json` names each server you use that speaks the chat
  completions API: its address, model, the variable that holds its key, and
  its tier. A ladder rung may name one, so a ladder can climb from a model on
  your machine to a hosted one. A key is read from its variable when a call is
  made, and each endpoint sends only its own.
- A run on the `openai` engine signs where its prompts went: the server's
  address, without a name, password or query in it, and the endpoint's name
  when a rung named one.
- `metabot ui` opens on an everyday page: one box for the task and a folder,
  and a check if you have one, which judges each try as `--check` does. A
  check that could change or delete things is refused on the page. The task
  runs as a ladder on your endpoints, and a write waits for your answer on
  the page. A try the ladder did not take, a stopped ladder's last one too, is
  shown as such. Developer mode, with every run, desk and receipt, is one link
  away at `/runs`; `ui_mode` in `config.json` picks which page opens.
- A desk in `office.yaml` may say `read_only: true`. It takes its turn as an
  advisor runs: it reads the project, every write it tries is asked even under
  `--allow-write`, and it files nothing. Its answer goes to the next desk,
  which starts where it started. A planner or a reviewer desk is one.
- `metabot office run --until-checks [--rounds N]`: a desk whose gate checks
  fail runs again, on its own branch, with what the checks said, until they
  pass or the rounds run out. When the run's one desk that may write, run as
  one copy, changes no file in round 1, the checks run all the same, so a
  desk that claims work it did not do goes again. A later round that changes
  no file, an error, a limit or a decline ends the loop.
  Each round is its own run and receipt, linked to the round before.
- `metabot office run --max-cost DOLLARS`: one money cap over every desk run,
  round and advisor of an office run. A desk MetaBot cannot price refuses the
  run before it starts.
- `metabot ui`'s office form can run the checks until they pass, with a
  number of rounds and a money cap. The office run's page shows each round,
  why its loop ended, and the cap with what was spent.
- A ladder's result ends with one plain line: each climb and why, where it
  was done or why it stopped, and what it spent. The everyday run's page shows
  it at the top, and `ask --ladder` prints it.
- Bridges: `bridges` in `config.json` names a program that runs as an engine,
  in `ask`, a ladder, an office desk or the web UI. It gets a private job file
  and answers in JSON lines (`metabot.bridge/1`). Its own tools are not gated,
  and the run says so. A bridge that takes the switchboard reports a call it
  made through it as `metabot/` and the tool's name: the run counts it, and
  the switchboard records it, once. Each call has an id of its own in the run.
  A `usage` line counts one request, or with `"total": true` the run's whole
  so far. A job that may not write goes to a bridge that says it keeps to
  that, and to no other. Each receipt names the bridge and a digest of what
  ran.
- A plugin can carry bridges, in `metabot/bridges/NAME.yaml`. `install` shows
  each one and its command, and `enable` makes them engines with
  `--allow-bridges`, or leaves them out with `--without-bridges`. A bridge runs
  from the installed copy, whose hash is checked before each run, and its
  receipt names the plugin and that hash. Under a home whose path a program
  can split or expand, as for a server, no plugin's bridge is enabled or runs.
- `metabot plugin new NAME` writes a plugin to start from, with its own
  marketplace: a skill, or with `--bridge` an engine that answers with the
  task. The marketplace is named for the plugin, with a number when a
  marketplace already has that name or MetaBot keeps it.
- Every office run and every ladder is sealed when it ends, also when it is
  stopped: one receipt over each run of it, in order, with the sha256 of each
  run's receipt, or null for a run that never sealed one. The seal signs the
  office.yaml by its sha256, the checks, the rounds, and the money cap with
  what was spent, and commits to the task, as MetaBot keeps it, by a salted
  sha256, as a run commits to its prompt. `metabot receipts verify` on an
  office run or a ladder checks that each receipt it holds is there,
  unchanged and valid, and that none of the run's is missing from it. The
  office run's page and `receipts show` say whether the seal holds. A project
  that keeps receipts gets an office run's or a ladder's seal with its runs'
  copies.
- The README shows how to add a real browser as a connection: a browser MCP
  server pinned to a version, with the origins it may act on. An office desk
  drives it under the same gate as `ask`: a call to an origin not on the list
  is destructive, so with nobody there to answer it is declined.

### Changed

- With no check, a ladder rung climbs when the task names files in the
  project and its run made no tool call, so it read none of them. Such a
  rung answered: a local model that wrote a file of its own as prose, and
  ran nothing, was taken as the answer.
- A receipt now signs where its run sits among others: an office desk's
  office, desk, round and the run before it, and a ladder rung's ladder and
  rung. It also signs each plugin the run had, with its hash. These were kept
  in the run's journal alone, which the signature does not cover.
- A receipt now commits to its run's prompt and answer, as the journal keeps
  them with a known token redacted: a `content` verdict signs a salted sha256
  of each, and the salts stay in the journal, so a copy of the receipt gives
  neither away. `metabot receipts verify` and the run's page say the journal
  does not match when either was changed. They were kept in the run's journal
  alone, which the signature does not cover.
- When a model on the default server, on this machine, was never probed,
  and a run ends because it wrote its tool calls as text MetaBot could not
  make, the error now says that `metabot doctor --models <model>` checks how
  it calls tools. A model the probe finds needs it is then asked one step at
  a time, held to a schema.
- `plugin enable --add-servers` keeps each connection the plugin already
  holds, also when it enables another version of it, with its id, the scope
  you set for its tools and its state, and writes its command again. It made
  each anew: the scope you had set was gone, so a call you had made ask ran
  unasked, a routine naming it stopped firing, and a connection you had
  revoked came back. Another version keeps a connection only for the server
  of the same name: a connection name is cut at 32 characters, so another
  server can share it, and that server gets a connection of its own.

### Fixed

- A read-only `openai` run, a desk with `read_only: true` or an advisor, is
  offered a connection's read tools alone, and a call to a tool it was not
  offered is refused, and recorded as a failed step. It was offered every
  tool a connection has, writes included, and only the gate stood between
  them. `ask --read-only` takes no write ahead: beside it, `--allow-write`
  and `--allow` allow nothing, and each write it tries is asked, as on a
  read-only desk.
- An `openai` run's connection call that a limit or a cancel cut off is in the
  record: unanswered, or, once it was sent, as one that may have run. It was
  missing from the receipt and the journal, while the run counted it. On any
  engine, a connection's call a cancel cuts off while it runs is recorded as
  one that may have run, and one stopped while a person was asked about it is
  recorded as declined. On `claude`, `codex`, `cursor` and a bridge, the
  person is then asked no longer: a call the engine gives up on, or that is
  still asked when the run stops, is declined as cancelled, where a yes given
  after it was signed as an approval of a call never made. A question closed
  so says so on the terminal, naming the call, before the next one is put,
  however often the run takes it back as it stops; the line came under the
  next question, which was still asked. A call the switchboard got no answer
  for is recorded as declined, and one to a tool it does not have as failed;
  each was in no record. A stop that comes as a call's step is sent to the
  record waits for it: a call declined as the run stopped, or one that ran,
  could be left out. A call the run stopped before it reached the switchboard
  was not made, and is in no record. A model that gives two calls one id, or
  two ids the record keeps as one, has each call recorded and counted on its
  own. A bridge's own call that shares its id with one through the
  switchboard, side by side or a turn later, is recorded as one that may have
  run: it could be given the other's result, so each call a bridge reports has
  an id of its own.
- A run opens its socket without changing MetaBot's file mode mask: the
  socket's folder is the run's own. Each run made the mask owner-only while it
  opened its socket, so a folder MetaBot or git made meanwhile, as an office
  copy's worktree or a ladder's copy, could not be entered, and runs that
  started at once could leave it so.
- A plugin whose installed copy holds a file MetaBot cannot read refuses the
  run, as a changed copy does. `ask` and `engines --manifest` stopped with a
  traceback.
- `plugin remove`, `plugin disable` and `plugin enable` delete a copy or view
  with a folder that cannot be entered, as the refusals say to. Each stopped
  with a traceback, and `disable` dropped the plugin's connections first. A
  copy MetaBot still cannot delete is a refusal that names it, and a view that
  stays keeps the plugin on, with its connections. A disable whose connections
  cannot be removed leaves the plugin off and says to run it again, as do a
  remove and an enable that turn a plugin off; one whose `connections.json`
  does not read changes nothing. An enabled plugin whose view is gone refuses
  each run and its bridges, until it is disabled: runs left it out without a
  word. A `plugin remove` of several versions that stops at one keeps no
  record of the copies it deleted before it. An `enable --add-servers` lists
  each connection before it adds it, so when it stops part way, as on a full
  disk, a retry, a disable or a remove takes it; a retry was refused, and the
  connection stayed after a remove. An enable that stops part way names the
  plugin and says to run it again, and no run gets the view it built until it
  is done; until then `plugin list` and `plugin enable` show no connection it
  did not add, and `enable` says how to add them to the version it enabled. A
  server whose connection name MetaBot keeps for itself refuses before any is
  added. One whose name is a connection of yours adds none of the plugin's
  servers, and says how to add them yourself; it said to remove it, and no
  command removes one.
- `plugin install` and `plugin import-skills` refuse a folder they cannot
  list, and name it in full. They installed the plugin without that folder's
  files, and `plugin show` called the copy intact. `plugin show` says an
  installed copy with a folder it cannot list cannot be checked, and why. A
  plugin holding a file whose name is not UTF-8 is refused, by that name:
  `plugin install` stopped with a traceback.
- A name whose bytes are not UTF-8, as a folder's can be, is printed escaped,
  as `\udce9`. `init`, `doctor` and `office trust` with a MetaBot home of such
  a name, `receipts verify` of a receipt kept there, `chat` in such a folder,
  and `ask` with an answer that names such a file each stopped with a
  traceback; `init`, `office trust` and `ask` did so after their work. An
  office desk's answer that names such a file is kept, and handed on, escaped:
  the office run stopped there, with the desk's work not committed. A prompt
  or a note piped in with such a name is read as an argument is, for `ask -`,
  `chat`, `mail send` and each command that reads stdin, and `doctor` shows
  an engine CLI's version whatever its bytes; each stopped with a traceback. A
  bridge whose program is in such a folder ran, and then its run stopped with
  a traceback, its receipt unsigned.
- A MetaBot home named by a relative path, as `METABOT_HOME=.metabot`, is
  taken from the folder MetaBot started in. A run whose engine worked in
  another folder, as an office desk, a ladder rung or `ask --cwd` does, told
  its gate hook and its switchboard a path they read in that folder: every
  call the hook gates was blocked, the switchboard's steps were lost, and the
  switchboard used the connections of a `.metabot` it found there. A plugin's
  server added there with `--add-servers` named the plugin's copy by that
  relative path, so the folder a server started in decided what ran. A
  server that names a plugin's copy now starts only when that path is whole,
  holds nothing a program can split or expand, and leads to the copy in this
  home, through a link or a `..` or not: a word of its own, the part after the
  last `=`, `:` or `,` in one, or a word of the line a shell runs with `-c`,
  read as that shell reads it. A line that holds a comment, a here-document,
  a group, a substitution or braces names no copy. One that does not is never
  started, `connect add` does not save it, and a routine that names it is not
  saved and does not fire. Each says to enable that version again, its hooks and bridges run or
  left out as they were, which names its copy whole. Under a home whose own
  path holds a character a program can split or expand (a blank, a quote, an
  operator, `$`, a glob, `=`, `:` or `,`) no server that names a plugin's copy
  starts, as a shell behind `env`, an unquoted `$0` or `env -S` reads its path
  again; each says to move the home first, and names any word it would refuse
  wherever the home is. `--add-servers` adds no server that names its copy so
  that it would never start.
- `--cwd` naming a loop of links, or a folder past one that cannot be entered,
  or a folder by a relative name from a folder that was removed, is not a
  directory, for `ask`, `ask --ladder`, `repl` and `engines --manifest`, and
  so is `--dir` naming one for `office run` and `office check`. Each stopped
  with a traceback. So did an office folder that cannot be entered, an office
  looked for from below one, and a `DESK_OFFICE` that names a loop of links:
  each now says why the office cannot be read. The web UI refuses a folder
  that cannot be entered, or one past it, and names an office it cannot read;
  the page was a server error. From a linked worktree whose main tree, or
  git's record of the worktree, git cannot enter, as one you have no access to
  or on a mount that stopped answering, the office named is the main tree's,
  as from any other worktree, whatever bytes its path holds: the worktree's
  own office was read. One git no longer knows, as when its main tree was
  moved or its disk unmounted, or declines to read, keeps its own. An office
  in a folder whose name is not UTF-8 is trusted, runs its checks, and is
  shown on the web UI's pages: `office trust` stopped with a traceback,
  `office run --checks` refused, and each page that named it was a server
  error. `office check` with no `--dir` reads the office `--dir` reads. The
  `office` and `desks` commands from a folder that was removed, and a kit
  folder that cannot be entered, stopped with a traceback, and the web page
  was a server error: each now says why. A skill with a folder that cannot be
  listed refuses the office run; it loaded without that folder's files.
- A run whose engine reported no usage MetaBot could read, on a model with a
  price, was said to cost $0.00, and it passed any money cap. Its cost is now
  unknown: the receipt's `usage` and `budget` verdicts warn, the cost line
  says the engine reported no usage, or a report with no token counts
  MetaBot can read, and under a cap an office run or a ladder runs nothing
  more after it, since the cap cannot be kept. The office run says what it
  spent at least, and which run's cost is unknown. A run that never reached
  its model, because its engine could not start, its key was not set, or its
  server or proxy could not be reached or turned the request away with an
  answer in the 400s other than 408, cost nothing, and its cost line says
  so. An answer in the 500s or a 408, no answer in time, or one that does
  not read keeps the cost unknown, on any turn: the model may have run. So
  does an answer that does not say what it spent: under a cap the run ends
  at its next tool call, and an answer that comes first is kept. The cost
  line, and a ladder or an office that stops for it, say a request never
  said what it spent.
- An `openai` answer whose message is not an object, or whose content or
  tool calls are not of the shape a chat answer has, stopped the run with a
  traceback. It is now an answer that does not read: the run ends with an
  error that says so.
- Under a money cap, a run on a model MetaBot has no rate for never starts,
  also on `claude`, which reports its own cost only when it ends: its running
  count had nothing to count by, so the cap acted only after the run. A
  `claude` run on your account's default model, or on an alias such as
  `haiku`, which it names as it starts, ends at its first tool call when that
  model has no price. Each `claude` message is priced by the model it says ran
  it, which in `claude`'s plan mode, where a `--read-only` run is, can be a
  larger one than it named; the receipt's `model` verdict names the models
  that ran. A message that names the model asked for another way, as a dated
  snapshot, with `-latest` or `[1m]`, or by a cloud's id for it, is priced as
  that model, as is a model with no price of its own that an engine names as
  it starts. A message that names another model with no price leaves the cost
  unknown. A run's total, as a bridge may send it, is priced only where it
  adds to the requests reported before it: on a model whose rate is higher
  past a prompt length, it was priced as one request that long, so a run
  within its cap was stopped past it, and its receipt signed several times
  what it spent. A total takes back none of what those requests counted:
  tokens it puts under another kind, as one that leaves out what was read from
  the cache, add nothing, and the receipt signs each kind as it was priced;
  they were charged at the full input rate. Tokens an engine reports before it
  names its model, as a bridge may, leave the cost unknown when the model it
  names prices them otherwise than the one asked for, or neither has a price,
  also once a model with one is named after it, and under a cap the run ends
  at its next tool call; they were priced as the model asked for, at $0.00
  when it had no price. The line after a run that has no price names the
  model to price, where it said `this model`: the one asked for when the
  models the run names have no price of their own, as each is priced as it.
  It names each model a price is missing for, each other model a message ran
  on and the one asked for once, by whatever name a message gives it, and so
  do the limit a run under a cap ends at and `ask --json`'s `unpriced`: one
  run names every price to add.
- A ladder rung on `openai` that names no model runs the model `ask` takes:
  the `openai` entry of `models` in `config.json`, then `openai_model`. It ran
  `openai_model`, while `engines --manifest` said the role ran the other.
- `metabot receipts verify RUN` fails when the receipt kept under that run
  id is another run's. It said VALID for any valid receipt kept there.
- `metabot receipts show` on an office run judges each desk's receipts where
  MetaBot keeps them. It opened the paths the office record names, so a
  hand-edited record could have it read any file.
- An office run whose record cannot be written, as on a full disk, says why
  as it ends, also when it was stopped or stopped short. One that was not
  stopped exits 1, and the web page shows it failed, with why; one that was
  stopped exits 130, and its page shows it stopped. It stopped with a
  traceback, and printed none of its rows. One whose seal cannot be signed
  says why too.
- A file nested too deep to read is taken as one that does not read. A
  receipt or journal is INVALID, `metabot receipts show` and a run's page
  say they cannot read it, and a settings file such as `config.json` stops
  the command, naming the file. Each stopped with a traceback, and the page
  with a server error.
- Output nested too deep to read, from an engine, a bridge, a model or a
  model's server, is taken as output that does not read: a line is text, a
  call is not run, and a reply ends the run with its error recorded. The run
  stopped with a traceback.
- On the origins a browser connection may act on, a call that acts on the
  page, such as a click, typing or a navigate, is read or write by its name,
  as the README says, also when the server marks every action destructive,
  as a browser MCP server does. Each click, typing and navigate on your own
  dev server was destructive, so an unattended run could only look at a
  page. Anywhere else, a call is still destructive, and so is one that runs
  code, works on the browser's cookies, storage or routes, or clicks at a
  point on the screen, and one the server marks destructive that does not
  act on the page, such as closing a tab.
- A browser call that names an element of the top page as its `target`, such
  as `e6`, acts where the page is. That name was read as an address with no
  origin, so the call was destructive on every page. An element in a frame,
  such as `f1e12`, is still a place with no origin: the frame may be another
  site's.
- A `codex` run's trace shows why a call to a connection failed, such as
  MetaBot's decline. It showed `{"error": null}`.
- `metabot receipts show` no longer creates `~/.metabot` on a machine that
  has none.
- In a project that keeps receipts, `ask --ladder --check` copies each
  rung's checks receipt there too, as an office run does.
- A stop while a ladder's rung is checked carries that rung's run, which is
  sealed, so `ask --ladder` names its receipt. It went up as the checks' own
  stop, with no run in it. The trace says when a rung's checks start.
- A task that names a path through a loop of links runs as any other: the
  path names nothing. `ask`, `chat` and the run's page stopped with an error
  once the run ended.

### Security

- On Linux with `bwrap`, a run's engine no longer reads the instructions
  outside its project. A `cursor` run read every rule folder above its
  project, so on a machine where your home folder holds another project's
  rules, it followed them. It also read its account's plugins, the plugins,
  skills and hooks in `~/.claude`, and the skills in other tools' folders. A
  `codex` run read the skills in `~/.agents/skills` and the plugins in
  `~/.codex`, though `codex_ignore_user_config` was on. Without `bwrap` they
  stay in view, and the run and its receipt say so. Rules an engine's account
  keeps on its server still reach it. See Added.
- Every key the settings name for a model, `openai_key_env` and each
  endpoint's `key_env`, is replaced by its marker wherever a connection's
  token is, whichever engine or endpoint a run called: a run's steps,
  prompt, answer, error and journal, its checks runs, an office run's or a
  ladder's task, a routine's firings, and the connections' server log. In
  0.2.0 only the key of the engine that ran was replaced, so a `claude` run
  that read the `openai` key from a file, or was given it in the prompt,
  kept it in its record. No variable is taken from any environment. The
  `openai` engine's saved conversation, which a resumed run sends back to
  the model, and the job file a bridge reads keep the text as the engine
  was sent it.
- A run's answer and error go on as its journal keeps them, each secret
  replaced: to an office's record, the desk's kept answer and the next desk,
  a routine's firings, a ladder's record, and the answer `ask`, `chat` and
  the web UI show. In 0.2.0 they went on as the engine said them, so a desk
  that repeated a token handed it to the next desk's engine.
- A desk's commit message replaces a secret in the task's first line the
  same way. It kept the line as written, on a branch that may be pushed.
- A private ladder's rungs call the address the ladder judged when it began.
  A rung on the `openai` settings read `config.json` again as it started, so
  a change made in the meantime could send a private task off this machine.
- A link or a redirect that takes a browser connection's page to another site
  is seen when the server says where its page is after a call, as a browser
  MCP server does, so each call after it is destructive. In 0.2.0 it was not
  seen, and under `--allow-write` the typing after it ran unasked. Such a
  line never brings the page back onto the allowed origins, since a page can
  write one itself; a navigate does.
- `ask --ladder` prints why a rung failed, why the ladder stopped, its
  one-line summary, and git's note on work it could not bring back with
  control characters escaped, as every other line of a run is. In 0.2.0 a
  rung's error went to the terminal as its model's server sent it, so an
  error body could move the cursor or retitle the window.

## [0.2.0] — 2026-10-25

### Added

- Every run records what it spent: its tokens of each kind, what they cost,
  and where the figure comes from. That is the engine's own report, the
  listed rates, your rates, a local model at $0.00, or your plan, which is
  not priced. The receipt signs it as a `usage` verdict, with a digest of the
  rates, and `metabot ask` prints it beside the receipt.
- `metabot ask --max-cost DOLLARS` ends the run once it has cost that much.
  The receipt signs the cap as a `budget` verdict. A run MetaBot cannot price
  does not start under a cap.
- `metabot cost` totals past runs by engine and model.
- `prices` in `config.json` gives a model its rates, in dollars per million
  tokens.
- `metabot doctor --models` checks whether each local model can call
  MetaBot's tools: by itself, by text that MetaBot reads, or held to a schema
  one step at a time. It keeps what it finds, and the engine asks each model
  the way that works. A model that writes its calls as text is held to the
  schema when it passes that way too.
- A local model that writes Python's triple quotes unescaped inside a JSON
  tool call has the call read, with the quotes as text.
- When a local model writes calls one after another and the last one breaks
  off before its JSON ends, the complete ones are made and the broken one is
  not.
- A tool call a local model writes in a code fence inside a longer answer is
  never made. MetaBot asks the model for the call alone, twice at most, and
  then fails the run, so an answer that shows a call and makes none does not
  end "ok".
- `metabot ask --ladder` starts on the cheapest rung, and climbs only when
  the record shows the rung failed. It stops instead after a declined call.
  `metabot ladder` shows the rungs: by default the local model, once probed,
  then the `build` role. `--max-cost` covers the whole ladder, and each rung
  is a run with its own receipt.
- `metabot ask --ladder --check CMD` runs the command on each rung that
  reaches its end within its limits, and the check judges the work: a failing
  check climbs, and a passing one answers even when a call on the way did not
  succeed. The checks are sealed as their own run.
- In a git checkout, each rung of a ladder works in its own copy of the
  project, so a failed rung's writes in its copy never reach the project.
  The answering rung's work is applied to your working tree, and the patch
  is kept. A copy is not a sandbox: a write the gate allows to an absolute
  path reaches it.
- In a copy, a call declined because no one was there to ask no longer
  stops the ladder: the next rung meets the same gate. A person's no still
  stops it.

### Changed

- A `claude` run counts its tokens message by message as it goes, so a token
  budget can stop it before it ends. The result's count replaces the running
  one.
- When a local model writes its tool call as text, in a code fence or in
  tags, MetaBot makes the call, through the gate like any other. A tool name
  that is nearly right runs the one tool it can mean. The journal notes each
  of these. An answer that is a call MetaBot cannot read fails the run, where
  it was shown as the answer.

### Security

- A desk's or a rung's work can rewrite its worktree's `.git` file, git's
  link to the project's records; a search and replace over every file did.
  MetaBot now keeps the file as git wrote it and puts it back before its own
  git commits in the worktree or puts it back to its commit. In 0.1.x, a
  desk's worktree whose `.git` file pointed at the project's own could have
  the person's staged changes unstaged when MetaBot put the worktree back;
  the files themselves stayed. A rung whose work still cannot be committed
  in its copy climbs.

## [0.1.2] — 2026-10-21

### Security

- A `claude` run no longer loads the engine's settings files. In 0.1.0 and
  0.1.1, a repository's committed `.claude/settings.json` or
  `.claude/settings.local.json` took effect inside a run: its hooks and key
  helper ran without a gate decision, and its `env` could send the run's
  model calls, with your login, to another address. MetaBot now passes its
  gate hook and plugins itself. From your own settings file it keeps your
  model, your effort for each model, your telemetry and error-reporting
  opt-outs, and your deny rules. Nothing else in that file is passed on.
- The `openai` engine's key no longer reaches its shell or the run's record.
  In 0.1.0 and 0.1.1, its shell tool ran with your whole environment, so a
  model that ran `env` was shown the key that `openai_key_env` names, and the
  record could keep it. The shell now runs without that variable, and the
  key is replaced in the steps, the journal, the receipt and the run index,
  also when the model reads it from a file or repeats it in its answer.

### Changed

- A `claude` run no longer loads `CLAUDE.md` files, or skills, agents,
  commands, hooks and plugins from the project or your engine settings. A
  login set in the settings file is not read: sign in with `/login` or a key
  in the environment. Your `sandbox` settings do not apply in a `claude` run.

### Fixed

- A run whose folder path is too long for a Unix socket puts the socket in a
  temp folder. Three paths left that `mb-*` folder behind; every run now
  gives it back.
- `metabot receipts verify` no longer creates `~/.metabot` and its receipts
  folder on a machine that has none.

## [0.1.1] — 2026-10-18

### Changed

- A plain `metabot ask`, with no `-e`, `-m`, `--role` or `--suggest`, takes
  the `fast` role's model when that role runs on the default engine and no
  model is set for that engine. The engine stays the default one, so where
  the work goes does not change.

### Added

- When the prompt names files in the project and the run made no tool calls,
  MetaBot says so beside the answer: the receipt shows no read of them. A
  resumed run says nothing, since an earlier turn may have read them.

### Fixed

- A call that came back with an error, such as a command that exits
  non-zero, was reported as "failed". The line beside the answer now says how
  many calls did not succeed, and the trace shows a multi-line error on one
  line, where it printed `\n` between the lines.

## [0.1.0] — 2026-10-17

Initial release, published as `orcha-metabot`; it installs the `metabot` command.
The [README](README.md) says what MetaBot does, and the limits
it states.
