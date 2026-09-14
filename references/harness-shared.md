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

## Model and effort are set before the call, never in it

No harness here lets you choose either when you launch a worker. Claude Code
and Codex read both from the agent definition; Hermes has no agent file and
takes both from global config, the same for every child.

So a plan's `agent` and `effort` are what the stage wants, not a promise about
what it will get. How close you can come depends on where you are running, and
your own harness file says which of these you are in:

| | model per worker | effort per worker |
| --- | --- | --- |
| Claude Code, definitions loaded | yes | yes |
| Claude Code, falling back | yes, passed to the call | no, the session's |
| Codex, definitions installed | yes | yes |
| Hermes | no, one setting for all | no, one setting for all |

Losing them costs money and time rather than correctness: an under-powered
worker on a judgement stage is the risk, and an over-powered one on the cheap
stage is only waste. The briefs carry the intent in words as well, which is why
the segmentation brief says the pass is mechanical, so the gap is narrower than
the table suggests. Report which row you were in when you finish.


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
