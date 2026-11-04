---
name: bizopsmap-map
description: Map how a business or a project works with the bizopsmap command, and render the map as one page. Use when asked to map a business, its operations, its hand-offs or its checks, or to bring an existing map up to date.
---

# Map a business with bizopsmap

bizopsmap draws a business as units, hand-offs and checks, from files the
business already keeps. This skill only runs its command. The command's own
guide is the reference, so follow it where the two differ.

1. Check that the command is here: `bizopsmap --help`. If it is missing, say
   so and stop. Install nothing unless the person asks.
2. Read the guide it prints for agents, `bizopsmap guide`, and follow it.
3. From nothing to a page in one step: `bizopsmap up [dir]`.
   Or step by step:
   - `bizopsmap templates` lists a spreadsheet template per kind of business.
   - `bizopsmap init [dir] --template KIND` starts a map from one of them.
   - `bizopsmap build [dir]` builds the map from the business's own files.
   - `bizopsmap validate [dir]` checks the map.
   - `bizopsmap render [dir]` writes one page, `.bizopsmap/out/view.html`.
4. Before anything leaves this machine, run `bizopsmap validate [dir] --publish`.
   Sharing a page needs a denylist: `bizopsmap export [dir] -o DIR --denylist FILE`.
   Ask the person before you export.
5. To show how the map changed over time: `bizopsmap timeline [dir]`.

End by saying what you ran, where the page is, and every finding the checks
printed.
