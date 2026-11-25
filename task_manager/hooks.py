app_name = "task_manager"
app_title = "Task Manager"
app_publisher = "MicroCRISPR"
app_description = "Personal task and approval dashboard for ERPNext"
app_email = "shivam.singh@microcrispr.com"
app_license = "mit"

# Apps
# ------------------

# required_apps = []

# Each item in the list will be shown as an app in the apps page
# add_to_apps_screen = [
# 	{
# 		"name": "task_manager",
# 		"logo": "/assets/task_manager/logo.png",
# 		"title": "Task Manager",
# 		"route": "/task_manager",
# 		"has_permission": "task_manager.api.permission.has_app_permission"
# 	}
# ]

# Includes in <head>
# ------------------

# include js, css files in header of desk.html
# app_include_css = "/assets/task_manager/css/task_manager.css"
# app_include_js = "/assets/task_manager/js/task_manager.js"

# include js, css files in header of web template
# web_include_css = "/assets/task_manager/css/task_manager.css"
# web_include_js = "/assets/task_manager/js/task_manager.js"

# include custom scss in every website theme (without file extension ".scss")
# website_theme_scss = "task_manager/public/scss/website"

# include js, css files in header of web form
# webform_include_js = {"doctype": "public/js/doctype.js"}
# webform_include_css = {"doctype": "public/css/doctype.css"}

# include js in page
# page_js = {"page" : "public/js/file.js"}

# include js in doctype views
# doctype_js = {"doctype" : "public/js/doctype.js"}
# doctype_list_js = {"doctype" : "public/js/doctype_list.js"}
# doctype_tree_js = {"doctype" : "public/js/doctype_tree.js"}
# doctype_calendar_js = {"doctype" : "public/js/doctype_calendar.js"}

# Svg Icons
# ------------------
# include app icons in desk
# app_include_icons = "task_manager/public/icons.svg"

# Home Pages
# ----------

# application home page (will override Website Settings)
# home_page = "login"

# website user home page (by Role)
# role_home_page = {
# 	"Role": "home_page"
# }

# Generators
# ----------

# automatically create page for each record of this doctype
# website_generators = ["Web Page"]

# Jinja
# ----------

# add methods and filters to jinja environment
# jinja = {
# 	"methods": "task_manager.utils.jinja_methods",
# 	"filters": "task_manager.utils.jinja_filters"
# }

# Installation
# ------------

# before_install = "task_manager.install.before_install"
# after_install = "task_manager.install.after_install"

# Uninstallation
# ------------

# before_uninstall = "task_manager.uninstall.before_uninstall"
# after_uninstall = "task_manager.uninstall.after_uninstall"

# Integration Setup
# ------------------
# To set up dependencies/integrations with other apps
# Name of the app being installed is passed as an argument

# before_app_install = "task_manager.utils.before_app_install"
# after_app_install = "task_manager.utils.after_app_install"

# Integration Cleanup
# -------------------
# To clean up dependencies/integrations with other apps
# Name of the app being uninstalled is passed as an argument

# before_app_uninstall = "task_manager.utils.before_app_uninstall"
# after_app_uninstall = "task_manager.utils.after_app_uninstall"

# Desk Notifications
# ------------------
# See frappe.core.notifications.get_notification_config

# notification_config = "task_manager.notifications.get_notification_config"

# Permissions
# -----------
# Permissions evaluated in scripted ways

# permission_query_conditions = {
# 	"Event": "frappe.desk.doctype.event.event.get_permission_query_conditions",
# }
#
# has_permission = {
# 	"Event": "frappe.desk.doctype.event.event.has_permission",
# }

# DocType Class
# ---------------
# Override standard doctype classes

# override_doctype_class = {
# 	"ToDo": "custom_app.overrides.CustomToDo"
# }

# Document Events
# ---------------
# Hook on document methods and events

# doc_events = {
# 	"*": {
# 		"on_update": "method",
# 		"on_cancel": "method",
# 		"on_trash": "method"
# 	}
# }

# Scheduled Tasks
# ---------------

# scheduler_events = {
# 	"all": [
# 		"task_manager.tasks.all"
# 	],
# 	"daily": [
# 		"task_manager.tasks.daily"
# 	],
# 	"hourly": [
# 		"task_manager.tasks.hourly"
# 	],
# 	"weekly": [
# 		"task_manager.tasks.weekly"
# 	],
# 	"monthly": [
# 		"task_manager.tasks.monthly"
# 	],
# }

# Testing
# -------

# before_tests = "task_manager.install.before_tests"

# Overriding Methods
# ------------------------------
#
# override_whitelisted_methods = {
# 	"frappe.desk.doctype.event.event.get_events": "task_manager.event.get_events"
# }
#
# each overriding function accepts a `data` argument;
# generated from the base implementation of the doctype dashboard,
# along with any modifications made in other Frappe apps
# override_doctype_dashboards = {
# 	"Task": "task_manager.task.get_dashboard_data"
# }

# exempt linked doctypes from being automatically cancelled
#
# auto_cancel_exempted_doctypes = ["Auto Repeat"]

# Ignore links to specified DocTypes when deleting documents
# -----------------------------------------------------------

# ignore_links_on_delete = ["Communication", "ToDo"]

# Request Events
# ----------------
# before_request = ["task_manager.utils.before_request"]
# after_request = ["task_manager.utils.after_request"]

# Job Events
# ----------
# before_job = ["task_manager.utils.before_job"]
# after_job = ["task_manager.utils.after_job"]

# User Data Protection
# --------------------

# user_data_fields = [
# 	{
# 		"doctype": "{doctype_1}",
# 		"filter_by": "{filter_by}",
# 		"redact_fields": ["{field_1}", "{field_2}"],
# 		"partial": 1,
# 	},
# 	{
# 		"doctype": "{doctype_2}",
# 		"filter_by": "{filter_by}",
# 		"partial": 1,
# 	},
# 	{
# 		"doctype": "{doctype_3}",
# 		"strict": False,
# 	},
# 	{
# 		"doctype": "{doctype_4}"
# 	}
# ]

# Authentication and authorization
# --------------------------------

# auth_hooks = [
# 	"task_manager.auth.validate"
# ]

# Automatically update python controller files with type annotations for this app.
# export_python_type_annotations = True

# default_log_clearing_doctypes = {
# 	"Logging DocType Name": 30  # days to retain logs
# }

# Translation
# ------------
# List of apps whose translatable strings should be excluded from this app's translations.
# ignore_translatable_strings_from = []



# Task Manager
# ------------
# Everything below is this app. Nothing above it is used; the generated
# comments are left in place as reference.

after_install = "task_manager.install.after_install"
after_migrate = "task_manager.install.after_migrate"

# The workflow index is metadata shared by every user, so it is cached until a
# workflow actually changes. These are the only document hooks this app
# registers -- it deliberately does not hook doc_events["*"], which on this
# site would mean running code on every save of every doctype.
doc_events = {
	"Workflow": {
		"on_update": "task_manager.services.workflow_index.clear_index",
		"after_insert": "task_manager.services.workflow_index.clear_index",
		"on_trash": "task_manager.services.workflow_index.clear_index",
	},
	# What a user is allowed to see, as opposed to what the documents say.
	#
	# Roles are a child table of `User`, so removing one -- or disabling the
	# account outright -- saves the parent and lands here. `User Permission`
	# is the other half: ~290 rows per in-scope doctype pin users to their own
	# employee record on this site, so adding or removing one changes the
	# answer to every query this page makes.
	#
	# Both clear one key: this app`s cached payload for that one user. See
	# services/cache.clear_for_user for why the TTL alone is not good enough
	# and for the case that is deliberately NOT hooked.
	"User": {
		"on_update": "task_manager.services.cache.clear_for_user",
		"on_trash": "task_manager.services.cache.clear_for_user",
	},
	"User Permission": {
		"after_insert": "task_manager.services.cache.clear_for_user",
		"on_update": "task_manager.services.cache.clear_for_user",
		"on_trash": "task_manager.services.cache.clear_for_user",
	},
}


def _request_updates(events):
	"""React to a save on an in-scope request: share it, then drop stale caches.

	Sharing is the half that writes to data outside this app, and it is on by
	default because the approval tabs are empty without it -- 127 of the 140
	Gate Passes pending right now cannot even be listed by the person named to
	approve them. See scope.SHARE_WITH_NAMED_APPROVER.

	Cache clearing is registered either way. It touches nothing but this app's
	own per-user keys, and without it an approved request lingers in the
	approver's list for the length of the TTL.
	"""
	from task_manager import scope

	for doctype in scope.doctypes():
		events.setdefault(doctype, {})["on_update"] = (
			"task_manager.services.approver_gate.on_request_update"
		)


def _named_approver_enforcement(events):
	"""Register the named-approver refusal, but only if it is switched on.

	Off by default, so installing this app changes nothing about how the three
	HRMS workflows behave -- it only changes what Task Manager shows. See
	scope.ENFORCE_NAMED_APPROVER for what turning it on costs: 76 Department
	HOD and 47 Head Leave Approver holders can approve anybody's request
	today, and they would stop being able to.

	Written as a function so the loop variables do not survive as module
	attributes -- everything at the top level of this file is a hook name.
	"""
	from task_manager import scope

	if not scope.ENFORCE_NAMED_APPROVER:
		return

	for doctype in scope.doctypes():
		events.setdefault(doctype, {})["validate"] = (
			"task_manager.services.approver_gate.enforce_named_approver"
		)


_request_updates(doc_events)
_named_approver_enforcement(doc_events)
