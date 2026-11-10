# MetaChat — concept

MetaChat is the tablet face of MetaBot and the Metaorcha core: one Android
app, one screen, from which a person runs their own AI sessions on their own
machine and accounts, watches them, talks to them, hands work between them,
and checks what each run did against a signed receipt.

## What it is today (phase 1)

- **Desks.** Every session is a desk: a persistent terminal (tmux) on the PC
  plus a chat lane on the tablet. Claude, Cursor and plain shells. Desks are
  grouped by project.
- **Chat that always lands.** A message reaches a desk through its inbox
  when the session is armed, else it is typed into the session's running
  CLI, and the app says which. Desks message each other (`mc-send`), and a
  person hands a piece of work from one session to another (`/handoff`).
- **Runs and receipts.** MetaBot runs start from the composer. Each ends in
  a signed receipt. The PC checks it with `orcha verify`; the tablet checks
  it again with its own code, and shows both.
- **Modules.** Plugins, bridges, workflows, routines, connections, the
  office and the SDK are compact tabs. Every write a tab offers runs as a
  visible command in a terminal desk where the person answers the prompts:
  the gate stays where it is.
- **Computer use.** A virtual display on the PC (`:99`), shown in a dock the
  person drives by hand. Agents on it come later.
- **Sovereign.** Modules are data; a person adds one from the tablet, with a
  session doing the typing. History is a local SQLite file. Sync, sign-in,
  A2A and remote access are seams, each opt-in.

## What it is not

- Not a cloud. The PC serves the page over the person's own network; nothing
  leaves the machine unless a plugin the person enabled sends it.
- Not a second login path. Signing in to a vendor happens on the PC.
- Not a claim of safety. A receipt proves what a run recorded; the app says
  "checked on this tablet with code this PC served" and no more.

## Where it goes

Remote mode (this) → the verifier shipped inside the APK → agents on the
virtual display, A2A between workflows → MetaBot running on the device
itself → the network across devices and people.
