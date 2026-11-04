---
name: code-desks
description: Work as one desk of a Code Desks office, where the work board, the sign-offs and the mail live in the repository. Use when the folder has an office.yaml, or the person mentions desks, the work board, sign-offs or office mail.
---

# Work in a Code Desks office

The `desk` command runs the office. This skill only runs it, and
`desk --help` is the reference.

At the start of a turn:
- `desk mail --desk NAME read` prints your unread mail and marks it read.
  NAME is your desk; it is in `$DESK` when that is set.
- `desk inbox --as engineer` shows what the desks owe now. `--as owner`
  shows what the person owes.
- `desk board` shows every assignment, its sign-off, and who owes what.

While you work:
- Take on only the assignments the board gives your desk.
- `desk check` checks office.yaml and the board for mistakes and gaps.

To hand work in:
- `desk deliver THREAD engineer ITEM --evidence PROOF` hands in one item at
  the assignment's current sign-off. PROOF is a path, a commit, a URL or a
  version string that shows the item is done.
- `desk verify [THREAD]` re-runs the accepted automatic checks. Exit 1 means
  a proof broke; fix it before you hand in more.
- `desk send DESK "MESSAGE" --thread THREAD` mails another desk, or `you`
  for the person.

Never deliver an owner item. Those are the person's sign-offs.
