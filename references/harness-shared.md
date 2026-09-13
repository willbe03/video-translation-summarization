# The worker contract

Read this alongside the file for your own harness. It is the part that does not
change, and it is the whole contract: a harness that can do this can run the
pipeline, whatever it calls delegation.

A worker receives one line, the absolute path of a brief file. It reads that
brief, reads the payload beside it, writes its result in the same directory,
and replies with one line. It needs file read and file write and nothing else.

Three rules, in order of how much damage breaking them does.

Never put payload text in the launch message. The path is the message. The
whole point of the file handoff is that a transcript never enters your context
or another worker's.

Always give the worker its instructions. Print them with

```bash
uv run --project scripts/vstudio vstudio worker-prompt
```

and put them above the brief path. A shipped worker definition already carries
them, and on a harness with no definition they are the only thing keeping a
worker inside its task.

Launch every task in a plan at once. They do not depend on each other, and a
stage takes as long as its slowest worker only if they run together.

## Effort is set by the definition, never by the call

No harness here lets you choose effort when you launch a worker. Claude Code
and Codex read it from the agent definition, and Hermes takes it from global
config. A worker launched without a definition runs at whatever the session is
doing, which is usually more thinking than the cheap stage needs. The prompts
carry the intent as well, so the cost difference is smaller than it looks, but
it does not disappear.


## Definitions are an upgrade, not a requirement

`vstudio install-agents` writes worker definitions where a harness can enforce
what the instructions ask for. Where it cannot, or where the definitions have
not loaded yet, launch the harness's ordinary subagent with the same
instructions. The work comes out the same. What you lose is the guarantee that
a worker could not have ignored the instructions, so say which one you used in
the final report.

Never wait for a restart or ask the user to install something mid-job. Fall
back, finish the job, and mention it at the end.

Nothing installs these for you. Hermes syncs a skill by copying its files and
Claude Code loads one the same way; neither runs a setup step, so the
definitions exist only if someone followed `references/INSTALL.md`. Assume they
do not.
