# local-harness

A developer's own agent loop, run as a MetaBot engine. It talks to a local
OpenAI-compatible server (Ollama at `http://127.0.0.1:11434/v1` unless
`LOCAL_HARNESS_URL` says otherwise) and has three tools of its own; every call
and every request's tokens are reported, so the run's receipt shows the steps
and the cost like any built-in engine's.

```sh
metabot plugin install local-harness@metabot-demos
metabot plugin enable local-harness --allow-bridges
metabot ask -e local-harness -m qwen2.5:3b --read-only "what is in this folder?"
```

Copy `bin/local-harness` to start your own sub-harness: keep the event lines
(`started`, `tool_call`, `tool_result`, `usage`, `final`, `error`) and honour
`read_only`, and MetaBot does the rest.
