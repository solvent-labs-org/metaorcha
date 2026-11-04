---
name: bizopsmap-question
description: Answer a question about a business from its bizopsmap map, such as who hands what to whom, what is checked, or what is not mapped yet. Use when a folder has a .bizopsmap map and the person asks about the business it describes.
---

# Answer a question from a bizopsmap map

1. Find the map: `.bizopsmap/graph.json` in the folder. If there is none,
   offer to make one with the bizopsmap-map skill, and stop.
2. Bring it up to date: `bizopsmap build [dir]`, then `bizopsmap validate [dir]`.
3. Read `.bizopsmap/graph.json` to answer. `bizopsmap detect [dir] --json`
   shows what the repository says about itself: its stacks and its layout.
4. Answer from the map only, and name the units and hand-offs you used.
   Where the map is silent, say so instead of guessing. The coverage line that
   `bizopsmap build` prints says how much of the business is mapped.
