# Trip-intake agent

The LangGraph agent in `backend/services/agent/` turns a user's trip request into
validated trip details (`TripConstraints` in `state.py`) before planning starts.

> The card-based intake (issue #9) will change how follow-up questions work. Update
> this page when it merges.

## Terminal demo

From `backend/`, with the virtual environment active:

```bash
python -m services.agent.graph
```

- **Sign-in:** the demo reuses the `flight-agent` ChatGPT OAuth sign-in and streaming
  model call. No OpenAI API key is needed, but your account must support that OAuth
  flow. The first run opens a browser to sign in and lists the available models.
- **Model:** choose one from that list, or pass one directly with `--model <slug>`.
- **Credentials** are stored under `services/agent/.chatgpt-auth/`. Git ignores this
  folder, and credentials never enter the graph state. Never commit it.

Enter a trip request, then answer the follow-up questions in the same terminal. The
model proposes JSON field updates; Python validates and merges them before checking
which required details are still missing. Invalid values lead to another follow-up
question. Enter `/quit` at a follow-up to exit.

When every required detail is known, the demo prints the validated constraints and
calls a placeholder planning node. It does not search or book anything.

## Graph image

The demo saves `services/agent/trip_intake_graph.png` at startup. To render it
without signing in or calling a model:

```bash
python -m services.agent.graph --graph-only
```

Rendering uses the online Mermaid service, so it needs network access. Only the
graph's structure is sent. Git ignores the generated image.

## Using the graph from a route

The graph uses in-memory checkpoints: conversation state lasts while the process
runs and is lost on restart. A chat route will start a conversation with
`graph.invoke(initial_state, config)` and resume after each pause with
`graph.invoke(Command(resume=...), config)`, using the same unique thread ID each
time. The demo uses one local OAuth account, so it is not a multi-user endpoint yet.
