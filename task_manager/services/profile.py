# Copyright (c) 2026, MicroCRISPR and contributors
# For license information, please see license.txt

"""Who the reader is, and what this page currently covers.

Two questions that kept being asked of the page and were not answered on it:
"why am I seeing this work" and "why am I not seeing that work". The first is
roles, the second is scope. Both are shown rather than explained in a wiki
nobody opens.

Nothing here is a permission decision. The roles listed are the ones the
reader already holds and can see in their own User record; the doctypes listed
are the ones this release covers, which is public information about the
software, not about any document. Everything that actually gates access is in
approvals.py and in `get_list`.
"""

import frappe
from frappe.permissions import AUTOMATIC_ROLES

from task_manager import scope

# Roles worth naming. Everybody holds the automatic ones, so listing them tells
# the reader nothing and pushes the roles that do matter off the end.
HIDDEN_ROLES = set(AUTOMATIC_ROLES) | {"Desk User"}

# Enough to be useful, few enough to stay one glance. The count travels with
# them so a truncated list says so.
MAX_ROLES = 12


def build(user: str) -> dict:
	"""The profile block: who I am, what I hold, what this page covers."""
	full_name, user_image = (
		frappe.db.get_value("User", user, ["full_name", "user_image"]) or (None, None)
	)

	roles = sorted(r for r in set(frappe.get_roles(user)) if r not in HIDDEN_ROLES)

	employee = None
	designation = None
	department = None
	from task_manager.services import approver_gate

	employee_id = approver_gate.employee_of(user)
	if employee_id:
		employee = employee_id
		designation, department = frappe.db.get_value(
			"Employee", employee_id, ["designation", "department"]
		) or (None, None)

	return {
		# No `user` key. The id is an email address on this site, and the
		# no-addresses rule holds for the reader`s own address too -- the page
		# needs a name and initials to render, never the login. test_isolation
		# caught this the first time it was added.
		"full_name": full_name or user,
		"user_image": user_image or "",
		"initials": _initials(full_name or user),
		"employee": employee,
		"designation": designation or "",
		"department": department or "",
		"roles": roles[:MAX_ROLES],
		"roles_total": len(roles),
		"roles_truncated": len(roles) > MAX_ROLES,
		"coverage": coverage(),
	}


def coverage() -> dict:
	"""What the system is working on right now.

	Read straight off scope.py rather than restated here, so this cannot drift
	from what the tabs actually show. When the restriction is lifted the card
	says so instead of claiming a scope that is no longer enforced.
	"""
	wide = bool(scope.COVER_ALL_SUBMITTABLE)

	# The declared types are still listed when coverage is wide, because they
	# are still the ones the page knows the most about -- a named approver, a
	# declared level count, a real request type rather than a doctype name.
	# What changes is the note, which stops claiming they are the only ones.
	#
	# The wide list itself is deliberately not enumerated. It is roughly 130
	# doctypes, it would take more height than the whole panel, and "every
	# submittable doctype" is both shorter and more accurate than a list the
	# reader would have to count.
	return {
		"area": "HRMS" if not wide else "Everything submittable",
		"restricted": bool(scope.restrict_to_scope()),
		"todos_restricted": bool(scope.restrict_todos_to_scope()),
		"wide": wide,
		# Labels come from scope.LABELS, the same place the queue chips read
		# them, so the card and the rows can never disagree about what a
		# request type is called.
		"doctypes": [
			{"doctype": dt, "label": scope.label_for(dt)} for dt in scope.doctypes()
		],
		"note": (
			"Every submittable doctype. These are tracked in most detail"
			if wide
			else "Approvals on these request types only"
			if scope.restrict_to_scope()
			else "All active workflows on this site"
		),
	}


def _initials(name: str) -> str:
	parts = [p for p in (name or "").replace(".", " ").split() if p]
	if not parts:
		return "?"
	if len(parts) == 1:
		return parts[0][:2].upper()
	return (parts[0][0] + parts[-1][0]).upper()
