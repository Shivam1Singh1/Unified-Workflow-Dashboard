# Copyright (c) 2026, MicroCRISPR and contributors
# For license information, please see license.txt

"""Aggregates the dashboard leads with: the headline, the charts, the queue.

Everything here is derived from the four lists the other services already
built. No extra queries are issued -- if a number on this page costs a database
round trip, it belongs in the service that owns that list, not here.
"""

import frappe

from task_manager import scope

# Age bands for the approvals chart. Ordered, left to right, and the labels are
# the chart's x-axis -- the colour ramp only reinforces the order, it does not
# carry it.
# Months, not days.
#
# The bands used to be Under 1 day / 1 to 3 / 3 to 7 / Over a week, which was
# the right scale for a queue that turns over in a week and the wrong one for
# this site: every sampled approver had 100% of their approvals in the last
# band, so the chart was one bar and three zeroes. It measured nothing because
# nothing here is under a week old.
#
# At this scale the interesting question is months. The page detects the
# all-in-one-band case and says so in words instead of drawing the bar.
AGE_BANDS = (
	("Under 1 month", 0, 730),
	("1 to 6 months", 730, 4380),
	("6 to 12 months", 4380, 8760),
	("1 year+", 8760, None),
)

# How many ranked rows travel to the browser. The page shows five at a time
# and pages through them client-side, so this is the depth of the queue rather
# than the height of the card.
#
# It has to be at least the two tab limits added together, or the page
# contradicts itself. At 100 a reader with 152 approvals got a stat card
# reading 152 and a queue card that could only count to 100 -- two numbers for
# one list, on the same screen. Both tabs are capped at approvals.LIMIT (200),
# so 400 is the point past which this can never bind again.
QUEUE_SIZE = 400


def build(payload: dict) -> dict:
	"""Add `summary`, `workload`, `age_buckets` and `queue` to the payload."""
	needs = payload["needs_action"]["rows"]
	awaiting = payload["awaiting_others"]["rows"]
	approved = payload["approved_by_me"]["rows"]
	todo = payload["todo"]["rows"]

	return {
		"summary": _headline(needs, todo, awaiting, approved),
		"workload": _workload(payload),
		"age_buckets": _age_buckets(needs + approved),
		"queue": _queue(needs, todo),
	}


def _headline(needs, todo, awaiting, approved) -> dict:
	"""The one sentence the page opens with.

	"Waiting on you" counts only what the reader can actually act on -- their
	approvals and their to-dos. Documents stuck after them, or raised by them,
	are someone else's move and are reported separately; folding those into the
	headline would overstate the reader's own backlog.
	"""
	waiting_on_me = len(needs) + len(todo)
	oldest = max([_hours(r) for r in needs + todo] or [0])

	return {
		"waiting_on_me": waiting_on_me,
		"approvals": len(needs),
		"todos": len(todo),
		"oldest_days": int(oldest // 24),
		"oldest_hours": round(oldest, 1),
		"all_clear": waiting_on_me == 0,
		"oldest_of": {
			"needs_action": _oldest_days(needs),
			"awaiting_others": _oldest_days(awaiting),
			"approved_by_me": _oldest_days(approved),
			"todo": _oldest_days(todo),
		},
		"due_this_week": sum(1 for r in todo if _due_within(r, 7)),
	}


def _workload(payload: dict) -> dict:
	"""Part-to-whole across the four lists.

	Rendered as a single horizontal stacked bar, not a donut. Two of these
	four values are routinely within one or two of each other, and a donut is
	the wrong instrument for comparing close values -- the segment lengths are
	arc lengths, which nobody can compare by eye. The stacked bar keeps the
	part-to-whole reading and makes the comparison possible.
	"""
	# Three lists, not four. ToDos were dropped from the dashboard entirely:
	# they are assignments rather than approvals, 13 of 15 sampled readers
	# had none, and mixing them into a share-of-workload chart meant the
	# chart answered a different question from the three cards above it.
	#
	# The ToDo tab is still built and still travels in the payload -- it is
	# what Tab 1 was, it is tested, and `due_this_week` reads from it -- but
	# nothing on the page renders it.
	parts = []
	for key, label in (
		("needs_action", "Needs my approval"),
		("awaiting_others", "Raised by me"),
		("approved_by_me", "Stuck after me"),
	):
		parts.append({"key": key, "label": label, "count": payload[key]["shown"]})

	total = sum(p["count"] for p in parts)
	for part in parts:
		part["share"] = round(100.0 * part["count"] / total, 1) if total else 0.0

	return {"total": total, "parts": parts}


def _age_buckets(rows: list) -> dict:
	"""How long the approvals that involve this reader have been waiting."""
	buckets = []
	for label, low, high in AGE_BANDS:
		count = sum(1 for r in rows if _in_band(_hours(r), low, high))
		buckets.append({"label": label, "count": count})

	return {
		"total": len(rows),
		"buckets": buckets,
		"max": max([b["count"] for b in buckets] or [0]),
	}


def _queue(needs: list, todo: list) -> list:
	"""The ranked "do these first" list.

	Ordered by how long somebody else has been waiting, which is the only
	ranking that reflects cost to other people rather than to the reader.
	Approvals and to-dos are interleaved deliberately: a to-do that has sat
	for a week outranks an approval raised this morning.
	"""
	items = []

	for row in needs:
		items.append(
			{
				"kind": "approval",
				"kind_label": "Needs you",
				"doctype": row["doctype"],
				# The chip on the row says what *kind* of request it is, which
				# is the useful distinction in a mixed queue. "Needs you" is
				# not -- every row in this list needs the reader.
				"doctype_label": scope.label_for(row["doctype"]),
				"name": row["name"],
				"hours": _hours(row),
				"detail": _approval_detail(row),
				# The same three facts as `detail`, unjoined. The card renders
				# them as separate elements -- the name in one weight, the
				# step as a progress bar rather than as text -- and splitting
				# a sentence back apart in the browser to do that would be
				# parsing our own prose.
				"owner_name": row.get("owner_name") or "",
				"state": row.get("state") or "",
				"level": row.get("level") or {},
				"action": "Review",
			}
		)

	for row in todo:
		items.append(
			{
				"kind": "todo",
				"kind_label": "To-do",
				"doctype": row.get("reference_type") or "",
				"doctype_label": scope.label_for(row.get("reference_type") or "") if row.get("reference_type") else "To-do",
				"name": row.get("reference_name") or "",
				"todo": row["name"],
				"hours": (row.get("age_days") or 0) * 24,
				"detail": row.get("description") or "",
				"owner_name": row.get("assigned_by_name") or "",
				"state": "",
				"level": {},
				"action": "Open",
			}
		)

	items.sort(key=lambda item: item["hours"], reverse=True)
	return items[:QUEUE_SIZE]


def _approval_detail(row: dict) -> str:
	"""Who raised it, how far it has got, and who is holding it now.

	All three, because any one of them alone leaves the reader guessing. The
	name of the person waiting is the part that was missing: "step 1 of 2"
	does not say whether the reader is the hold-up.
	"""
	bits = []

	who = row.get("owner_name") or ""
	if who:
		bits.append(f"Raised by {who}")

	level = row.get("level") or {}
	if level.get("current") and level.get("total"):
		bits.append(f"step {level['current']} of {level['total']}")
	elif row.get("state"):
		bits.append(row["state"])

	# Deliberately not the approver name. Every row in this queue is waiting on
	# the reader by definition, so naming the approver would print the reader
	# their own name. The approver names are shown in the two cards where the
	# waiting party is somebody else -- "Stuck after you" and "Your requests".
	bits.append("waiting on you")

	return " · ".join(bits)


def _hours(row: dict) -> float:
	hours = row.get("pending_hours")
	if hours is None:
		hours = (row.get("age_days") or 0) * 24
	return float(hours or 0)


def _oldest_days(rows: list) -> int:
	return int(max([_hours(r) for r in rows] or [0]) // 24)


def _in_band(hours: float, low: int, high) -> bool:
	if high is None:
		return hours >= low
	return low <= hours < high


def _due_within(row: dict, days: int) -> bool:
	date = row.get("date")
	if not date:
		return False
	try:
		return frappe.utils.date_diff(date, frappe.utils.nowdate()) <= days
	except Exception:
		return False
