# Copyright (c) 2026, MicroCRISPR and contributors
# For license information, please see license.txt

"""Per-workflow overrides for the level engine.

Nothing here is required. The engine in services/workflow_index.py derives
level numbers from the transition graph on its own; these tables exist for the
workflows where the graph alone gives the wrong answer.

Why they are needed at all -- measured against the 47 active workflows on this
site:

* The obvious rule, "start from the first row of the workflow's States table",
  is wrong for Leave Application, whose first row is `Pending Leave Approval`
  while the real entry points are `Draft` and `Cancelled`.

* Nine workflows are fully cyclic: every one of their states has an incoming
  transition, so "the state nothing points at" does not exist. Asset Transfer
  Form, Slot Opening, Material Request, Batch Planning, Batches Planned, Slot
  Master List, Lab Experiment Template, Inward Receipt and Employee Tax
  Exemption Declaration are all in this shape.

The engine's actual rule is layered: an explicit START_STATES entry wins, then
states with doc_status 0 that nothing transitions into, then states with
doc_status 0, then the first row. Set an override only when a tracker shows
visibly wrong numbers -- guessing here is worse than the derived answer.
"""

# Workflow name -> the state a document starts life in.
# Example: {"Leave Application": "Draft"}
START_STATES: dict[str, str] = {}

# Workflow name -> {state: level number}. A partial map is fine; any state not
# named here keeps its derived level. Use this for workflows whose transitions
# loop in a way that makes breadth-first distance meaningless.
LEVEL_OVERRIDES: dict[str, dict[str, int]] = {}

# Workflow name -> states to treat as terminal even though a transition leads
# out of them. Nothing here yet; the derived rule (no outgoing transition) has
# matched every workflow checked so far.
EXTRA_TERMINAL_STATES: dict[str, list[str]] = {}


def start_state(workflow: str) -> str | None:
	return START_STATES.get(workflow)


def level_for(workflow: str, state: str) -> int | None:
	return LEVEL_OVERRIDES.get(workflow, {}).get(state)


def extra_terminal(workflow: str) -> set[str]:
	return set(EXTRA_TERMINAL_STATES.get(workflow) or [])
