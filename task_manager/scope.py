# Copyright (c) 2026, MicroCRISPR and contributors
# For license information, please see license.txt

"""The three HRMS request types, declared rather than guessed.

Nothing in this module imports frappe. It is pure data and pure functions, so
hooks.py can import it at load time and so the helpers can be unit-tested
without a database. Everything that needs a query lives in
services/approver_gate.py.

Why this file exists at all
---------------------------

The generic engine in services/workflow_index.py routes on two things: which
role may move a document out of its current state, and whether that
transition's condition passes. For most of the 47 active workflows on this
site that is exactly right. For these three it is not enough, because four of
their transitions are granted to a role with **no condition at all**:

* `On Duty Request`: `Pending Approval Of Department HOD -> Approve` is
  allowed to `Head Leave Approver` with an empty condition. 47 users hold that
  role, so all 47 were being offered all 183 pending On Duty requests.
* `Gate Pass`: `Pending For Approval -> Approve` is allowed to
  `Department HOD` with an empty condition -- 76 users, 142 pending requests
  -- and to `HR Head` with none either.
* `Leave Application`: the `HR Head` and `System Manager` routes out of
  `Pending Leave Approval` carry no condition.

`approvals._condition_passes` short-circuits on exactly that shape -- "an
unconditional route out is enough; load nothing" -- which is the correct
generic answer and the wrong answer here. The tables below name the approver
field per state so the gate can require the *named person*, not merely a role
holder.

Field names are declared, never inferred
----------------------------------------

Two of the three approver fields are `Data` holding an email rather than
`Link User`, and the Gate Pass employee field is **spelled `emoloyee`** in the
schema. A filter built on the obvious guess returns nothing and raises no
error, which is the worst possible failure mode, so every fieldname used
against these doctypes comes from here.
"""

# Whether the approval tabs show ONLY the request types declared below.
#
# True is the first release: Leave Application, On Duty and Gate Pass, and
# nothing else. The site has 47 active workflows, and the other 44 -- Purchase
# Order, Expense Claim, Material Request, Purchase Receipt, Journal Entry and
# the rest -- were filling the tabs with work this release is not about. The
# measured effect is not cosmetic: Tab 2 merges every readable doctype, sorts
# oldest first and cuts at 200, and real approvers were having their whole
# window taken by abandoned 2023 Purchase Receipt drafts, pushing their actual
# HRMS approvals below the cut.
#
# This narrows the three workflow-driven tabs (2, 3 and 4). Tab 1 is ToDos,
# which are not workflow-driven at all -- see RESTRICT_TODOS_TO_SCOPE.
#
# Set False to get the general-purpose dashboard back across all 47 workflows.
# The level engine in workflow_index.py never stopped being generic; this only
# decides which doctypes reach it.
RESTRICT_TO_SCOPE = True

# Whether Tab 1 is narrowed the same way.
#
# True. The plan said Tab 1 was unchanged from v2, and it was left that way at
# first -- but "only HRMS" was asked for directly, and Tab 1 is where the rest
# of the leak lived. The ranked queue ("Do these first") merges Tab 2 with
# Tab 1, so even with the approval tabs clean it came back 100% `Item` ToDos
# for a real approver.
#
# Of the open ToDos on this site, 12,754 point at `Item` and 1,791 at
# `Material Request` -- bulk-assignment noise rather than human work -- against
# 38 pointing at the three request types here.
#
# A ToDo with no reference at all is still kept: somebody typed it for
# themselves and it points at nothing this scope could exclude it from. Set
# False to get every assigned ToDo back.
RESTRICT_TODOS_TO_SCOPE = True


# Whether coverage is widened from the seven declared request types to every
# submittable doctype on the site.
#
# True. The ask was "as a user I should see everything -- what I raised and
# what is waiting on me" rather than the three HRMS types plus four Accounts
# ones the first release drew a box around. This is the one switch that does
# it, and it is the one switch that undoes it.
#
# What turning it on changes, in the three places it reaches:
#
#   workflow_index.for_user   stops cutting the index down to SCOPE, so all
#                             47 active workflows reach the tabs instead of 7
#   services/drafts.py        starts contributing the other family entirely --
#                             submittable doctypes with NO workflow, whose
#                             unsubmitted drafts appear under "Your requests"
#   services/todos.py         widens Tab 1 from the 7 declared types to every
#                             covered doctype
#
# What it deliberately does NOT change:
#
#   The SCOPE table below still applies in full. The named-approver gate, the
#   declared level numbers, the row_filters that separate On Duty from Work
#   From Home and the forced-terminal corrections are all still read for the
#   seven doctypes that declare them. Widening coverage adds doctypes to the
#   page; it does not stop the page knowing more about some of them than it
#   can derive about the rest.
#
#   Nothing in Tab 2 becomes role-only that was not role-only before. A
#   gated doctype is still gated.
#
# The risk this accepts, stated plainly: for the 40 workflows that declare
# nothing here, "waiting on me" can only be answered from the role route, and
# several of those routes are granted to a role with no condition. A shared
# queue is the right reading for some of them -- 17 Accounts Managers really
# do share one -- and the wrong reading for any that name an approver in a
# field this file has not been told about. The fix for a specific doctype is
# to declare it in SCOPE, which is the mechanism the seven below already use;
# the fix for all of them at once is to set this back to False.
#
# Reverting is this line and nothing else. No cache rebuild is needed --
# for_user applies the cut on the way out of the index, not on the way in --
# and no row is written anywhere by any of it.
COVER_ALL_SUBMITTABLE = True


# Whether a non-named approver is *refused* server-side, or merely not shown
# this work in Task Manager.
#
# False -- the shipped default -- makes everything below a view filter: the
# three production workflows keep working exactly as they do today, and a
# Department HOD who opens a Gate Pass form can still approve it. Task Manager
# simply stops presenting other people's requests as if they were theirs.
#
# True additionally registers a validate hook that refuses a state change by
# anyone who is not the named approver. That closes the hole properly, at the
# API as well as the form, but it is a real behaviour change for the 76
# Department HOD and 47 Head Leave Approver holders who can approve anyone's
# request today. It is off until somebody decides to turn it on.
ENFORCE_NAMED_APPROVER = False

# Roles that stay able to act even with enforcement on, so a stuck request can
# always be rescued. Only consulted when ENFORCE_NAMED_APPROVER is True.
BYPASS_ROLES = ("System Manager", "HR Head", "HR Manager")

# Whether a request is shared with its named approver when it reaches their
# level.
#
# This one is on, because without it the approval tabs are empty for most of
# these requests and the feature does not work at all. Measured on the
# requests pending right now:
#
#   Gate Pass           127 of 140 cannot be listed by their named approver
#   Attendance Request   12 of 180
#   Leave Application     5 of 193
#
# The cause is 289 to 291 `User Permission` rows per doctype -- allow
# `Employee`, scoped `applicable_for` that doctype -- which restrict each of
# those users to their *own* employee record. An approver so restricted cannot
# see the request they are supposed to approve, in a list or on a form.
#
# Leave Application mostly escapes this because HRMS shares it: its
# `on_update` calls `share_doc_with_approver(self, self.leave_approver)`. The
# two custom flows have no equivalent, which is why Gate Pass is at 91%
# blocked and leave is at 3%. So this is not a new mechanism -- it is HRMS's
# own mechanism, applied to the level-2 approver and to the two doctypes that
# were missing it.
#
# It grants read and write on one request to the one person already named on
# it as its approver. It widens nobody's access to anything else, and the
# existing shares are left alone.
SHARE_WITH_NAMED_APPROVER = True

# doctype -> how this request type is routed.
#
#   gated           whether the row names the ONE person who may act on it
#   workflow        the active Workflow record, for cross-checking
#   employee_field  the Employee link on the request, or None
#   row_filters     narrows the doctype to this request type
#   levels          state -> {level, approver_fields}; a state listed here is
#                   an approval step and only the named approver may act on it
#   total_levels    denominator for "Level 1 of N"
#   requester_states non-terminal states that belong to the requester
#   extra_terminal  states the graph walk wrongly calls non-terminal
SCOPE = {
	"Leave Application": {
		"gated": True,
		"workflow": "Leave Application",
		"employee_field": "employee",
		"row_filters": {},
		# The only genuinely two-level flow of the three -- and escalation to
		# level 2 is a *manual* choice the level-1 approver makes, via the
		# `Request Approval From HOD` action, not an automatic step. The data
		# says they almost never make it: 11,239 leaves ended at
		# `Approved by Leave Approver` against 53 at
		# `Approved by Head Leave Approver`. So a leave sitting at level 1 is
		# not necessarily on its way to level 2, and the tracker should not
		# promise that it is.
		"levels": {
			"Pending Leave Approval": {
				"level": 1,
				"approver_fields": ["leave_approver"],
			},
			"Pending by Head Leave Approver": {
				"level": 2,
				"approver_fields": ["head_leave_approver"],
			},
		},
		"total_levels": 2,
		"requester_states": ["Draft"],
		"extra_terminal": [],
	},
	"Attendance Request": {
		"gated": True,
		"workflow": "On Duty Request",
		"employee_field": "employee",
		# 11,159 of the 11,241 rows are On Duty; the other 82 are Work From
		# Home, which is a different request type and out of scope. Without
		# this filter they would appear in the tabs as On Duty requests.
		"row_filters": {"reason": "On Duty"},
		# One state, two acceptable approvers. The workflow lets *either* the
		# level-1 approver or the HOD clear `Pending Approval Of Department
		# HOD`; they are alternatives, not a sequence, which is why this is
		# one level and not two.
		"levels": {
			"Pending Approval Of Department HOD": {
				"level": 1,
				"approver_fields": ["custom_approver", "custom_head_leave_approver"],
			},
		},
		"total_levels": 1,
		"requester_states": ["Draft"],
		# Both of these are docstatus 1 -- the request is approved and done --
		# but each has an outgoing `Cancel` transition for HR Head. The graph
		# rule "a state with an outgoing transition is non-terminal" therefore
		# calls them pending, which would park 10,791 finished requests in
		# their requesters' Tab 3 forever and offer them to HR Head as work.
		# Cancelling a finished request is an administrative act, not a level
		# of approval.
		"extra_terminal": ["Approved By Department HOD", "Approved By HR Head"],
	},
	"Gate Pass": {
		"gated": True,
		"workflow": "Gate Pass",
		# Not a typo here -- the field really is spelled this way on the
		# doctype, and correcting it would break the filter.
		"employee_field": "emoloyee",
		"row_filters": {},
		# Single level, and there is no second approver field to point at. The
		# workflow also has no Reject transition at all, and `status` is a
		# Select of only `Pending` / `Approved`, so a Gate Pass currently has
		# no way to express rejection. Nothing here invents one.
		"levels": {
			"Pending For Approval": {
				"level": 1,
				"approver_fields": ["approver"],
			},
		},
		"total_levels": 1,
		"requester_states": ["Draft"],
		"extra_terminal": [],
	},

	# ---- Accounts ---------------------------------------------------------
	#
	# A different family from the three above, and the difference is the whole
	# reason `gated` exists.
	#
	# Not one of these four carries an approver field. Every field on all four
	# was scanned for `approv`, `authoris` and `approved_by`: nothing. Approval
	# is granted to a role with no condition -- `Pending by Accounts Manager ->
	# Approve, allowed = Accounts Manager` -- and all 17 Accounts Managers may
	# act on any of them.
	#
	# That is NOT the over-grant the HRMS gate exists to correct. There it was
	# wrong because each request names its approver and the role route ignored
	# that name. Here there is no name to ignore: a shared queue is the
	# designed model. So `gated` is False, and the tabs fall back to the
	# generic role-and-condition engine -- the right answer for a shared queue
	# and the wrong one for a leave request.
	#
	# `employee_field` is None for the same reason: an invoice belongs to
	# whoever raised it, not to an employee named on it. `requester_filters`
	# returns None for these and `_gather` falls back to `owner`, which is the
	# "created by" side and the only narrowing that holds on this site.
	#
	# It has to hold, because read access is wide. 719 enabled users hold
	# `Employee`, `Employee` has read on Purchase Invoice, and there is no
	# `if_owner` on any of the four, no User Permission row scoped to any of
	# them, and no `permission_query_conditions` hook. `get_list` is still the
	# security boundary and is still never bypassed -- it simply does not
	# narrow these to the reader the way it narrows a leave request, so the
	# tabs must do it themselves.
	#
	# WATCH `extra_terminal`. Three of the four carry an administrative
	# transition out of their finished state -- Cancel on two, Reject on one --
	# and the graph rule "a state with an outgoing transition is non-terminal"
	# therefore calls finished documents pending. Left unforced that is 20,256
	# Purchase Invoices, 4,814 Journal Entries and 305 Payment Requests parked
	# in their raisers' Tab 3 forever. Same trap Attendance Request hit with
	# 10,791 rows.
	"Purchase Invoice": {
		"gated": False,
		"workflow": "Purchase Invoice",
		"employee_field": None,
		"row_filters": {},
		"levels": {
			"Pending by Accounts Manager": {"level": 1, "approver_fields": []},
		},
		"total_levels": 1,
		# `Rejected by Account Manager` is the raiser's again -- 331 rows sit
		# there and `Resend For Approval` is theirs to press.
		"requester_states": ["Draft", "Rejected by Account Manager"],
		"extra_terminal": ["Approved By Account Manager"],
	},
	"Journal Entry": {
		"gated": False,
		"workflow": "Journal Entry",
		"employee_field": None,
		"row_filters": {},
		"levels": {
			"Pending by Accounts Manager": {"level": 1, "approver_fields": []},
		},
		"total_levels": 1,
		# Rejection is a dead end here -- no `Resend`, unlike Purchase Invoice
		# -- so the graph already calls it terminal and it is not listed.
		"requester_states": ["Draft"],
		"extra_terminal": ["Approved By Account Manager"],
	},
	"Payment Request": {
		"gated": False,
		"workflow": "Payment Request",
		"employee_field": None,
		"row_filters": {},
		# The only three-step flow in scope, and the middle step is easy to
		# misread. `Approved By Purchase Head` is not a resting place: its only
		# route on is `Send For Approval`, which the Purchase Manager still has
		# to press. 69 requests sit in it right now, average age 523 days --
		# approved by purchase and never forwarded to accounts. Declaring it a
		# level is what puts them in somebody's queue instead of nobody's.
		"levels": {
			"Pending Approval Of Purchase Head": {"level": 1, "approver_fields": []},
			"Approved By Purchase Head": {"level": 2, "approver_fields": []},
			"Approval Pending From Accounts": {"level": 3, "approver_fields": []},
		},
		"total_levels": 3,
		"requester_states": ["Draft"],
		"extra_terminal": ["Approved By Account Manager"],
	},
	"Landed Cost Voucher": {
		"gated": False,
		"workflow": "Landed Cost Voucher",
		"employee_field": None,
		"row_filters": {},
		"levels": {
			"Pending by Accounts Manager": {"level": 1, "approver_fields": []},
		},
		"total_levels": 1,
		"requester_states": ["Draft"],
		# The one clean graph of the four: its approved state is a genuine dead
		# end, so nothing has to be forced.
		"extra_terminal": [],
	},
}


# What a reader calls each request type. `Attendance Request` is the doctype;
# nobody in the building calls it that -- they ask for an On Duty. The page
# shows these, never the doctype name.
LABELS = {
	"Leave Application": "Leave",
	"Attendance Request": "On Duty",
	"Gate Pass": "Gate Pass",
	"Purchase Invoice": "Purchase Invoice",
	"Journal Entry": "Journal Entry",
	"Payment Request": "Payment Request",
	"Landed Cost Voucher": "Landed Cost",
}


def restrict_to_scope() -> bool:
	"""Should the workflow-driven tabs show ONLY the declared request types?

	Two flags, one answer, and every caller asks through here rather than
	reading either constant. RESTRICT_TO_SCOPE records what the first release
	decided; COVER_ALL_SUBMITTABLE is the later, wider decision and outranks
	it. Keeping both means the original narrowing is still there to fall back
	to, instead of having been edited away.
	"""
	return RESTRICT_TO_SCOPE and not COVER_ALL_SUBMITTABLE


def restrict_todos_to_scope() -> bool:
	"""Same question for Tab 1.

	Note what the False answer does NOT mean here. Tab 1 is not thrown open
	when coverage widens -- todos.py switches the narrowing from "the seven
	declared types" to "every covered doctype", which is a different filter,
	not the absence of one. The 12,754 open `Item` ToDos stay out either way,
	because Item is not submittable and has no workflow, and they were the
	whole reason Tab 1 was narrowed in the first place.
	"""
	return RESTRICT_TODOS_TO_SCOPE and not COVER_ALL_SUBMITTABLE


def is_gated(entry: dict) -> bool:
	"""Does this request name the one person who may act on it?

	True for the three HRMS types, where a role route with no condition would
	otherwise offer one person's leave to 47 role holders. False for the four
	Accounts types, where there is no approver field to name anybody and the
	role route *is* the model -- 17 Accounts Managers sharing one queue.

	Every caller of `may_act` and `named_approvers` has to ask this first.
	Without it an ungated row has no approvers, `may_act` returns False for
	everybody, and the document appears in no tab at all.
	"""
	return bool(entry.get("gated"))


def label_for(doctype: str) -> str:
	return LABELS.get(doctype, doctype)


def for_doctype(doctype: str) -> dict | None:
	return SCOPE.get(doctype)


def in_scope(doctype: str) -> bool:
	return doctype in SCOPE


def doctypes() -> list[str]:
	return sorted(SCOPE)


def approver_fields(entry: dict, state: str) -> list[str]:
	"""Fields naming whoever may act at `state`. Empty for a non-approval state."""
	step = entry["levels"].get(state)
	return list(step["approver_fields"]) if step else []


def all_approver_fields(entry: dict) -> list[str]:
	"""Every approver field on this doctype, for one batched select."""
	seen = []
	for step in entry["levels"].values():
		for field in step["approver_fields"]:
			if field not in seen:
				seen.append(field)
	return seen


def extra_terminal(doctype: str) -> set[str]:
	entry = SCOPE.get(doctype)
	return set(entry["extra_terminal"]) if entry else set()


def named_approvers(entry: dict, state: str, row) -> list[str]:
	"""The user ids that may act on this row, normalised and de-duplicated.

	`row` is anything with `.get` -- a dict from a list query or a Document.
	Values are normalised because two of the three fields are free-text `Data`
	rather than `Link User`, so nothing guarantees their case or spacing.
	"""
	out = []
	for field in approver_fields(entry, state):
		value = _normalise(row.get(field))
		if value and value not in out:
			out.append(value)
	return out


def may_act(entry: dict, state: str, row, user: str) -> bool:
	"""Is `user` the person this row is actually waiting on?

	A state that is not an approval step returns False: Draft is the
	requester's to send, not anybody's to approve.
	"""
	approvers = named_approvers(entry, state, row)
	return bool(approvers) and _normalise(user) in approvers


def level_of(entry: dict, state: str) -> int | None:
	step = entry["levels"].get(state)
	return step["level"] if step else None


def apply_levels(entry: dict, level_of_map: dict) -> dict:
	"""Replace derived level numbers with the declared ones.

	Every state the tables do not name gets None rather than its graph
	distance. That is deliberate: on Leave Application the walk makes `Draft`
	level 1 and `Pending Leave Approval` level 2, so a leave waiting on the
	reporting manager would read "Level 2 of 2" -- already at the last step --
	when it has not been approved by anybody yet.
	"""
	return {
		state: entry["levels"][state]["level"] if state in entry["levels"] else None
		for state in level_of_map
	}


def _normalise(value) -> str:
	return (value or "").strip().lower() if isinstance(value, str) else ""
