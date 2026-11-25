# Copyright (c) 2026, MicroCRISPR and contributors
# For license information, please see license.txt

"""Submittable doctypes that have no workflow at all.

Everything else in this app is driven by an active Workflow record. The index
reads the states, the transitions say which role may move a document out of
each one, and "waiting on me" is answerable because somebody declared either
an approver field or a role route. 47 doctypes on this site are configured
that way, and `workflow_index` owns all of them.

The rest are not. A submittable doctype with no workflow has exactly two
meaningful positions -- draft and submitted -- and no approval step anywhere
in between. There is no approver field, no transition, no role route and no
Workflow Action row. Nothing on the document records who is supposed to act
on it next, because nobody ever declared that anyone should.

So this module answers only the half of the question that is answerable:

	a draft I created is a draft I have not sent

It is mine, it is open, and it belongs in "Your requests" beside the
workflow-driven requests I raised. That is the whole of what this file does.

What this module deliberately does NOT do
-----------------------------------------

It never puts a row in Tab 2. The tempting reading of "to be approved" for a
workflow-less doctype is "docstatus 0 and I hold submit permission", and on
this site that is a firehose rather than a queue. Submit rights on the big
stock and buying doctypes are held broadly, those tables carry abandoned
drafts going back to 2023, and Tab 2 merges every doctype, sorts oldest first
and cuts at 200 -- so the drafts would arrive at the top and push real
approvals below the cut. That is precisely the failure RESTRICT_TO_SCOPE was
introduced to fix, and widening coverage is not a reason to reintroduce it.

A draft nobody submitted is not an approval anybody is waiting on. It is work
its author has not finished, and this module says so.

Cost
----

One list query per doctype, and there is no way around that: the rows live in
one table per doctype and no single query spans them. `owner` is not indexed
on a standard Frappe table, so each of those queries is a full scan.

Four bounds keep that affordable:

  workflow_index.BIG_TABLE_ROWS  holds back tables too large to scan for an
                                 unindexed column. Measured on this site, it
                                 is the difference between 4.8 seconds and
                                 0.3, and it is the only one of the four that
                                 changes the answer rather than the cost.
  DOCTYPE_BUDGET                 caps how many tables one build visits
  LIMIT                          caps rows per table
  cache.TTL_SECONDS              the assembled payload is cached per user

The first two are gaps in what gets reported, so both raise `truncated` and
the card says it did not look everywhere.
"""

import frappe
from frappe.utils import now_datetime, time_diff_in_hours

# Rows per doctype. Deliberately small: this is the long tail of the page, not
# its subject, and a user with 400 abandoned drafts on one doctype should not
# be able to fill "Your requests" with them to the exclusion of the requests
# that are actually moving.
LIMIT = 50

# How many tables one build may visit. A site this size has roughly 90
# submittable doctypes and most carry no drafts for any given user, but the
# query still costs a scan per table, so the walk is bounded and says when it
# stopped rather than pretending it finished.
DOCTYPE_BUDGET = 60


def my_drafts(user: str, doctypes: list[str], held_back: list[str] | None = None) -> dict:
	"""Unsubmitted documents this user created, across workflow-less doctypes.

	`doctypes` comes from `workflow_index.draft_doctypes_for_user`, which has
	already dropped anything the user cannot read and anything an active
	workflow owns. Nothing here re-derives that list: this function must not
	be the place where coverage is decided, or coverage ends up defined in two
	files that disagree.

	`get_list` is still the security boundary. The `owner` filter narrows the
	result to the reader; it is not what keeps other people's documents out.
	"""
	rows = []
	# A doctype held back by workflow_index.BIG_TABLE_ROWS is a gap in this
	# card, so it is reported as one from the first row onwards rather than
	# only when something is cut.
	truncated = bool(held_back)
	total = 0

	visited = doctypes[:DOCTYPE_BUDGET]
	if len(doctypes) > DOCTYPE_BUDGET:
		truncated = True

	now = now_datetime()

	for doctype in visited:
		try:
			found = frappe.get_list(
				doctype,
				filters={"owner": user, "docstatus": 0},
				fields=["name", "modified"],
				order_by="modified asc",
				limit_page_length=LIMIT,
				ignore_ifnull=True,
			)
		except Exception:
			# One doctype with a broken permission hook must not empty the
			# card. Same rule the workflow gather follows.
			frappe.log_error(
				title=f"Task Manager: draft list failed for {doctype}"[:140],
				message=frappe.get_traceback(),
			)
			continue

		if len(found) >= LIMIT:
			truncated = True

		total += len(found)

		for doc in found:
			rows.append(_row(doctype, doc, now))

	return {"rows": rows, "total": total, "truncated": truncated}


def _row(doctype: str, doc: dict, now) -> dict:
	"""One draft, in the shape `awaiting_others` returns for a workflow row.

	The keys are matched to the workflow rows rather than invented, because
	both kinds land in the same list, get sorted together and are rendered by
	the same template. Anything the template reads has to be present on both
	or the two row kinds drift and the card starts throwing.

	The empty ones are empty for a reason, not for want of filling in:

	  level            there are no levels -- no workflow, no states
	  waiting_on_*     nobody is named and no role route exists; the honest
	                   answer is that it is waiting on its author, which the
	                   `draft` kind already says
	  trail            Workflow Action rows are the only record of who cleared
	                   what, and a doctype with no workflow has none
	  reminded_today   there is nobody to remind, so the button is not drawn
	"""
	modified = doc["modified"]

	return {
		# What makes this row different from its neighbours, and the only
		# thing the template branches on. Workflow rows carry kind="workflow".
		"kind": "draft",
		"doctype": doctype,
		"name": doc["name"],
		"state": "Draft",
		"modified": modified,
		"pending_since": modified,
		"pending_hours": round(time_diff_in_hours(now, modified), 1),
		"level": {},
		"my_level": None,
		# The reader raised it, so naming them to themselves adds nothing.
		"owner_name": "",
		"trail": [],
		"waiting_on_roles": [],
		"waiting_on_names": [],
		"last_reminder": None,
		"reminded_today": False,
	}
