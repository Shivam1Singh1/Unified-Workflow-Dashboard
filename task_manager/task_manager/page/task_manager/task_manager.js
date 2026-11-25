// Copyright (c) 2026, MicroCRISPR and contributors
// For license information, please see license.txt

// Task Manager dashboard controller.
//
// The browser never tells the server who it is. Every call below sends either
// nothing at all or a single document identifier, and the server derives the
// user from the session and re-checks the identifier before acting on it.
//
// All rendering goes through frappe.utils.escape_html before it reaches a
// template -- templates interpolate raw, so escaping here is the only thing
// standing between a document title and an injected script. The one
// deliberate exception is the icon markup this file supplies itself, which is
// static and contains no user data.

frappe.provide("task_manager");

frappe.pages["task-manager"].on_page_load = function (wrapper) {
	const page = frappe.ui.make_app_page({
		parent: wrapper,
		title: __("Task Manager"),
		single_column: true,
	});

	wrapper.task_manager = new task_manager.TaskManager(page);
};

frappe.pages["task-manager"].on_page_show = function (wrapper) {
	if (wrapper.task_manager) {
		wrapper.task_manager.resume();
	}
};

frappe.pages["task-manager"].on_page_hide = function (wrapper) {
	if (wrapper.task_manager) {
		wrapper.task_manager.pause();
	}
};

// Matched to the server cache, so a tick usually costs one cached read.
task_manager.REFRESH_MS = 120000;

// The greeting depends on the clock, not on the payload, so it is retimed on
// its own interval. A reader who leaves the page open across noon should not
// still be wished a good morning.
task_manager.GREETING_MS = 60000;

// Rows per page. Both lists page client-side over data the payload already
// carries, so turning a page costs no request.
task_manager.QUEUE_ROWS = 6;
task_manager.REQUEST_ROWS = 4;
task_manager.MINI_ROWS = 3;

task_manager.THEME_KEY = "task_manager:theme";

// Icon markup, kept here rather than in the templates so the stat cards can
// be generated from one list. Static markup, no user data.
task_manager.ICONS = {
	// Check in a box, paper plane, bare check mark.
	needs_action:
		'<svg viewBox="0 0 24 24"><rect x="3.5" y="3.5" width="17" height="17" rx="4"/><path d="M8 12.2l2.8 2.8L16.4 9.4"/></svg>',
	awaiting_others:
		'<svg viewBox="0 0 24 24"><path d="M21.5 2.5 11 13M21.5 2.5l-6.8 19-3.7-8.5L2.5 10.3Z"/></svg>',
	approved_by_me:
		'<svg viewBox="0 0 24 24"><path d="M20 6.5 9.5 17.5 4 12"/></svg>',
};

task_manager.LISTS = [
	{ key: "needs_action", label: __("Needs my approval") },
	{ key: "awaiting_others", label: __("Raised by me") },
	{ key: "approved_by_me", label: __("Stuck after me") },
];

// The two lists the task card switches between, and the hint each one shows.
task_manager.TABS = [
	{ key: "needs_action", hint: __("Longest wait at the top") },
	{ key: "approved_by_me", hint: __("Approved by you, waiting at a later step") },
];

task_manager.TaskManager = class TaskManager {
	constructor(page) {
		this.page = page;
		this.data = null;
		this.busy = false;
		this.timer = null;
		this.greeting_timer = null;
		this.tab = "needs_action";

		// No header action, and therefore no desk page header. The strip held
		// a duplicate title and one Refresh button, and this page reloads
		// itself every REFRESH_MS. Both are hidden in the stylesheet.
		//
		// `load({ force: true })` stays: it is the cache-busting path, and it
		// is the caller that went away, not the need for the path.
		this.setup_dom();
		this.setup_theme();
		this.bind_events();
		this.load();
		this.resume();
	}

	setup_dom() {
		this.page.main.html(frappe.render_template("task_manager", {}));
		this.root = this.page.main.find(".task-manager");
		this.el = (name) => this.root.find(`[data-${name}]`);
		this.skeleton = this.el("skeleton");
		this.render_clock();
	}

	// ---- theme -----------------------------------------------------------

	setup_theme() {
		this.container = this.root.closest(".page-container");
		this.theme = this.read_theme();
		this.apply_theme();
	}

	read_theme() {
		try {
			const saved = window.localStorage.getItem(task_manager.THEME_KEY);
			return saved === "day" || saved === "night" ? saved : "auto";
		} catch (e) {
			// Private browsing and locked-down profiles throw on access
			// rather than returning null. Following the OS is the right
			// fallback, and the toggle still works for the session.
			return "auto";
		}
	}

	apply_theme() {
		const night = this.is_night();

		if (this.container && this.container.length) {
			if (this.theme === "auto") {
				this.container.removeAttr("data-tm-theme");
			} else {
				this.container.attr("data-tm-theme", night ? "dark" : "light");
			}
		}

		const button = this.el("theme-toggle");
		if (button.length) {
			// The control names the state it will move you to, not the one
			// you are in, and `is-night` swaps the icon to match. Both marks
			// live in the markup; the stylesheet shows one.
			const label = night ? __("Switch to day mode") : __("Switch to night mode");
			button.attr("aria-pressed", night ? "true" : "false");
			button.attr("aria-label", label);
			button.attr("title", label);
			button.toggleClass("is-night", night);
		}
	}

	is_night() {
		if (this.theme === "night") return true;
		if (this.theme === "day") return false;
		try {
			return window.matchMedia("(prefers-color-scheme: dark)").matches;
		} catch (e) {
			return false;
		}
	}

	toggle_theme() {
		// From auto, the first press goes to whichever mode you are NOT in,
		// so the button always visibly does something.
		this.theme = this.is_night() ? "day" : "night";
		try {
			window.localStorage.setItem(task_manager.THEME_KEY, this.theme);
		} catch (e) {
			// Not fatal: the choice simply does not survive a reload.
		}
		this.apply_theme();
	}

	// ---- events ----------------------------------------------------------

	bind_events() {
		this.root.on("click", "[data-open]", (e) => {
			e.preventDefault();
			const el = $(e.currentTarget);
			const doctype = el.attr("data-doctype");
			const name = el.attr("data-name");
			if (doctype && name) {
				frappe.set_route("Form", doctype, name);
			}
		});

		// A to-do may point at a document or at nothing. One that points at
		// nothing is still openable -- as itself.
		this.root.on("click", "[data-open-todo]", (e) => {
			e.preventDefault();
			e.stopPropagation();
			const el = $(e.currentTarget);
			const doctype = el.attr("data-doctype");
			const name = el.attr("data-name");
			if (doctype && name) {
				frappe.set_route("Form", doctype, name);
			} else if (el.attr("data-todo")) {
				frappe.set_route("Form", "ToDo", el.attr("data-todo"));
			}
		});

		// Paging re-renders one card from data already in hand -- no request,
		// no spinner, and the other card keeps its own page.
		this.root.on("click", "[data-page]", (e) => {
			e.preventDefault();
			const el = $(e.currentTarget);
			if (el.is(":disabled")) {
				return;
			}
			const key = el.attr("data-page");
			this.page_at = this.page_at || {};
			this.page_at[key] = parseInt(el.attr("data-to"), 10) || 0;

			if (key === "requests") {
				this.render_requests();
			} else {
				this.render_rows();
			}
		});

		this.root.on("click", "[data-action=remind]", (e) => {
			e.preventDefault();
			e.stopPropagation();
			this.remind($(e.currentTarget));
		});

		// The hero button is the whole point of ranking the queue: it opens
		// whatever has kept somebody waiting longest.
		this.root.on("click", "[data-action=oldest]", (e) => {
			e.preventDefault();

			// The same filter "Do these first" applies, so the button opens
			// the row the reader can see at the top of that card rather than
			// a to-do that is ranked above it but listed somewhere else.
			const queue = (this.data && this.data.queue) || [];
			const first = queue.filter((item) => item.kind === "approval")[0] || null;

			if (!first) {
				frappe.show_alert({ message: __("Nothing is waiting on you"), indicator: "green" });
				return;
			}
			frappe.set_route("Form", first.doctype, first.name);
		});

		this.root.on("click", "[data-tab]", (e) => {
			e.preventDefault();
			this.show_tab($(e.currentTarget).attr("data-tab"));
		});

		// The View link on the status strip. It opens the oldest document
		// stuck behind the reader, which is the one the strip is counting
		// from -- anything else would make the link disagree with the
		// sentence above it.
		this.root.on("click", "[data-action=view-stuck]", (e) => {
			e.preventDefault();
			this.show_tab("approved_by_me");
		});

		this.root.on("click", "[data-action=theme]", (e) => {
			e.preventDefault();
			this.toggle_theme();
		});

		this.root.on("click", "[data-action=profile]", (e) => {
			e.preventDefault();
			e.stopPropagation();
			this.toggle_profile(!this.profile_open);
		});

		// Dismissal, both ways round. Bound on document because a click that
		// should close the panel lands outside the page root as often as not.
		this.on_doc_click = (e) => {
			if (!this.profile_open) return;
			if (!$(e.target).closest(".tm-who").length) {
				this.toggle_profile(false);
			}
		};
		this.on_doc_key = (e) => {
			if (e.key === "Escape" && this.profile_open) {
				this.toggle_profile(false);
				const btn = this.el("profile").find("[data-action=profile]");
				if (btn.length) btn[0].focus();
			}
		};
		$(document).on("click.tm_profile", this.on_doc_click);
		$(document).on("keydown.tm_profile", this.on_doc_key);

		this.bind_slices();

		// Each stat jumps to the card that actually holds that list -- and
		// for the two that share the tabbed card, it opens the right tab
		// first. Scrolling to a card showing the other list would answer a
		// question the reader did not ask.
		this.root.on("click", "[data-jump]", (e) => {
			const key = $(e.currentTarget).attr("data-jump");

			if (key === "needs_action" || key === "approved_by_me") {
				this.show_tab(key);
			}

			const target = this.root
				.find(key === "awaiting_others" ? "[data-requests]" : "[data-rows]")
				.closest(".tm-card");
			if (target.length) {
				target[0].scrollIntoView({ behavior: "smooth", block: "start" });
			}
		});
	}

	/**
	 * One page of a list, with the index clamped rather than reset.
	 *
	 * Approving the last row on page 2 should leave the reader on page 2 --
	 * or the last page that still exists -- not throw them back to the top
	 * every two minutes when the payload refreshes under them.
	 */
	paginate(key, rows, size) {
		const pages = Math.max(1, Math.ceil(rows.length / size));

		this.page_at = this.page_at || {};
		const at = Math.min(Math.max(this.page_at[key] || 0, 0), pages - 1);
		this.page_at[key] = at;

		return {
			rows: rows.slice(at * size, at * size + size),
			at: at,
			pages: pages,
			total: rows.length,
			size: size,
			from: rows.length ? at * size + 1 : 0,
			to: Math.min(rows.length, at * size + size),
		};
	}

	/**
	 * Numbered pager: first page, a window around the current one, last page,
	 * with gaps where pages were skipped.
	 *
	 * 200 requests is 50 pages. Printing fifty buttons is not navigation, and
	 * printing only Previous/Next hides where the reader is in the list.
	 *
	 * The key names the page state, not the element. The queue card keeps a
	 * page per tab -- needs_action, approved_by_me -- but both tabs draw
	 * into the one pager that card owns, so pass host_key when they differ.
	 */
	render_pager(key, info, accent, host_key) {
		const host = this.el((host_key || key) + "-pager");
		if (!host.length) {
			return;
		}
		if (info.pages <= 1) {
			host.attr("hidden", "hidden").empty();
			return;
		}
		host.attr("hidden", null);
		host.toggleClass("tm-pager-orange", accent === "orange");

		const wanted = new Set([0, info.pages - 1, info.at]);
		for (let d = 1; d <= 1; d++) {
			wanted.add(info.at - d);
			wanted.add(info.at + d);
		}
		const pages = [...wanted]
			.filter((p) => p >= 0 && p < info.pages)
			.sort((a, b) => a - b);

		let buttons = "";
		let last = -1;
		pages.forEach((p) => {
			if (p - last > 1) {
				buttons += `<span class="tm-page-gap">&hellip;</span>`;
			}
			buttons +=
				`<button class="tm-page${p === info.at ? " is-current" : ""}" type="button"` +
				` data-page="${key}" data-to="${p}"` +
				`${p === info.at ? ' aria-current="page"' : ""}>${p + 1}</button>`;
			last = p;
		});

		host.html(
			`<span>${frappe.utils.escape_html(__("Showing "))}<b>${info.from}&ndash;${
				info.to
			}</b> ${frappe.utils.escape_html(__("of"))} <b>${this.comma(
				info.total
			)}</b></span>` +
			`<span class="tm-pager-btns">` +
			`<button class="tm-page" type="button" data-page="${key}" data-to="${
				info.at - 1
			}" aria-label="${frappe.utils.escape_html(__("Previous page"))}"${
				info.at === 0 ? " disabled" : ""
			}>&lsaquo;</button>` +
			buttons +
			`<button class="tm-page" type="button" data-page="${key}" data-to="${
				info.at + 1
			}"${info.at >= info.pages - 1 ? " disabled" : ""}>${frappe.utils.escape_html(
				__("Next")
			)} &rsaquo;</button>` +
			`</span>`
		);
	}

	resume() {
		this.pause();
		this.timer = setInterval(() => {
			if (!document.hidden) {
				this.load({ quiet: true });
			}
		}, task_manager.REFRESH_MS);

		this.greeting_timer = setInterval(() => {
			if (!document.hidden) {
				this.render_clock();
			}
		}, task_manager.GREETING_MS);
	}

	pause() {
		if (this.timer) {
			clearInterval(this.timer);
			this.timer = null;
		}
		if (this.greeting_timer) {
			clearInterval(this.greeting_timer);
			this.greeting_timer = null;
		}
	}

	// ---- loading ---------------------------------------------------------

	load(opts) {
		opts = opts || {};
		if (this.busy) return;
		this.busy = true;

		if (!opts.quiet) {
			this.skeleton.show();
			this.el("rows").empty();
		}

		frappe
			.call({
				method: "task_manager.api.get_my_tasks",
				args: opts.force ? { refresh: 1 } : {},
			})
			.then((r) => {
				this.data = r.message || null;
				this.render();
			})
			.catch((err) => {
				// The server already logged this with the user and operation.
				console.error("[task_manager] get_my_tasks failed", err);
				const detail = (err && (err.message || err.exc_type)) || "";
				this.el("rows").html(
					`<div class="tm-empty">${frappe.utils.escape_html(
						detail || __("Could not load your tasks. Please refresh.")
					)}</div>`
				);
			})
			.always(() => {
				this.busy = false;
				this.skeleton.hide();
			});
	}

	render() {
		if (!this.data) return;
		this.render_clock();
		this.render_summary();
		this.render_kpis();
		this.render_profile();
		this.render_status();
		this.render_tabs();
		this.render_rows();
		this.render_workload();
		this.render_requests();
	}

	// ---- header ----------------------------------------------------------

	/**
	 * Date and greeting, both read off the clock rather than the payload.
	 *
	 * Called on every render AND on its own one-minute interval, because the
	 * payload only changes every two: a reader sitting on the page at 11:59
	 * should be wished a good afternoon at 12:00, not at the next fetch.
	 */
	render_clock() {
		const now = new Date();

		const days = [
			__("Sunday"), __("Monday"), __("Tuesday"), __("Wednesday"),
			__("Thursday"), __("Friday"), __("Saturday"),
		];
		const months = [
			__("January"), __("February"), __("March"), __("April"),
			__("May"), __("June"), __("July"), __("August"),
			__("September"), __("October"), __("November"), __("December"),
		];

		this.el("date").text(
			`${days[now.getDay()]} · ${now.getDate()} ${months[now.getMonth()]} ${now.getFullYear()}`
		);

		const profile = (this.data && this.data.profile) || {};
		const first = this.title_case((profile.full_name || "").split(/\s+/)[0] || "");

		if (!first) {
			this.el("greeting").text(__("Hello"));
			return;
		}

		// Substituted by __() itself rather than by a string replace. The
		// global `replace_all` exists but `frappe.utils.replace_all` does
		// not, and reaching for the wrong one throws here -- which takes the
		// whole header with it.
		const hour = now.getHours();
		let greeting;
		if (hour >= 5 && hour < 12) {
			greeting = __("Good morning, {0}", [first]);
		} else if (hour >= 12 && hour < 17) {
			greeting = __("Good afternoon, {0}", [first]);
		} else if (hour >= 17 && hour < 21) {
			greeting = __("Good evening, {0}", [first]);
		} else {
			greeting = __("Working late, {0}?", [first]);
		}

		this.el("greeting").text(greeting);
	}

	/**
	 * The one sentence under the greeting.
	 *
	 * Rules, all of them about not printing noise: a count of one takes the
	 * singular word, a count of zero drops its clause entirely, and both at
	 * zero replaces the sentence rather than leaving an empty frame. The age
	 * is the only coloured thing in the header because it is the only part
	 * that is a complaint.
	 */
	/**
	 * The one sentence under the greeting.
	 *
	 * Approvals only. To-dos are not on this page any more, and a headline
	 * that counts something the reader cannot see anywhere below it is a
	 * number with nowhere to go.
	 */
	render_summary() {
		const s = this.data.summary || {};
		const host = this.el("summary");
		const approvals = s.approvals || 0;

		if (!approvals) {
			host.text(__("You are all caught up."));
			return;
		}

		let html =
			frappe.utils.escape_html(__("You have ")) +
			`<b>${frappe.utils.escape_html(
				approvals === 1
					? __("1 approval")
					: __("{0} approvals", [this.comma(approvals)])
			)}</b> ` +
			frappe.utils.escape_html(__("waiting."));

		if (s.oldest_hours) {
			html +=
				" " +
				frappe.utils.escape_html(__("The oldest has waited")) +
				' <span class="tm-late">' +
				frappe.utils.escape_html(this.span(s.oldest_hours)) +
				"</span>.";
		}

		host.html(html);
	}

	// ---- stat cards ------------------------------------------------------

	render_kpis() {
		const s = this.data.summary || {};
		const oldest = s.oldest_of || {};

		const tiles = task_manager.LISTS.map((list) => {
			const days = oldest[list.key] || 0;
			const count = (this.data[list.key] || {}).shown || 0;

			// Nothing stuck behind you is the good outcome, so it is the one
			// tile that can report in green rather than in red.
			const clear = list.key === "approved_by_me" && !count;

			return {
				key: list.key,
				label: frappe.utils.escape_html(list.label),
				icon: task_manager.ICONS[list.key] || "",
				value: frappe.utils.escape_html(this.comma(count)),
				pill_kind: clear ? "clear" : "late",
				pill: frappe.utils.escape_html(
					clear
						? __("All clear")
						: days
						? __("Oldest {0} days", [this.comma(days)])
						: __("Nothing waiting")
				),
			};
		});

		this.el("kpis").html(frappe.render_template("tm_kpis", { tiles: tiles }));
	}

	// ---- the ranked queue ------------------------------------------------

	/**
	 * Switch the task card between its two lists.
	 *
	 * Each tab keeps its own page, so coming back to approvals lands where
	 * the reader left it rather than at the top.
	 */
	show_tab(key) {
		if (!key || this.tab === key) {
			return;
		}
		this.tab = key;
		this.render_tabs();
		this.render_rows();
	}

	render_tabs() {
		task_manager.TABS.forEach((tab) => {
			const on = tab.key === this.tab;
			const block = this.data ? this.data[tab.key] || {} : {};

			this.root
				.find(`[data-tab="${tab.key}"]`)
				.toggleClass("is-on", on)
				.attr("aria-selected", on ? "true" : "false");

			this.el("tab-count-" + tab.key).text(this.comma(block.shown || 0));

			if (on) {
				this.el("tab-hint").text(tab.hint);
			}
		});
	}

	/**
	 * The rows for whichever tab is open.
	 *
	 * The two lists answer different questions -- what is waiting on you,
	 * and what is waiting on somebody else after you -- so they are built
	 * separately and rendered through one template. The row shape is the
	 * same; only what it says differs.
	 */
	render_rows() {
		const stuck = this.tab === "approved_by_me";
		const all = stuck
			? ((this.data.approved_by_me || {}).rows || [])
			: (this.data.queue || []).filter((item) => item.kind === "approval");

		const page = this.paginate(this.tab, all, task_manager.QUEUE_ROWS);
		const rows = page.rows.map((row, index) => {
			const rank = page.at * page.size + index + 1;
			return stuck ? this.stuck_row(row, rank) : this.approval_row(row, rank);
		});

		const host = this.el("rows");
		host.html(
			frappe.render_template("tm_rows", {
				rows: rows,
				empty_text: frappe.utils.escape_html(
					stuck
						? __("Nothing is stuck after you.")
						: __("Nothing is waiting on you.")
				),
			})
		);

		// Restart the fade so paging reads as a change of content rather
		// than as the page reloading under the reader.
		host.removeClass("tm-fade");
		void host[0].offsetWidth;
		host.addClass("tm-fade");

		this.render_pager(this.tab, page, null, "rows");

		// "Start with the oldest" is a shortcut into the approvals list, so
		// it follows that list rather than whichever tab happens to be open.
		const approvals = (this.data.queue || []).filter((i) => i.kind === "approval");
		this.el("oldest").attr("hidden", approvals.length ? null : "hidden");
	}

	approval_row(item, rank) {
		const level = item.level || {};
		const bits = [];

		const who = this.title_case(item.owner_name || "");
		if (who) bits.push(__("Raised by {0}", [who]));
		if (level.current && level.total) {
			bits.push(__("Step {0} of {1}", [level.current, level.total]));
		}
		bits.push(__("Waiting on you"));

		return {
			rank: rank,
			rank_class: rank === 1 ? "tm-rank-first" : "",
			tag: frappe.utils.escape_html(
				(item.doctype_label || item.doctype || "").toUpperCase()
			),
			tag_class: "",
			doctype: frappe.utils.escape_html(item.doctype || ""),
			name: frappe.utils.escape_html(item.name || ""),
			meta: frappe.utils.escape_html(bits.join(" · ")),
			steps: this.steps(level, level.current),
			step_label: frappe.utils.escape_html(
				level.current && level.total
					? __("Step {0} of {1}", [level.current, level.total])
					: ""
			),
			age_label: frappe.utils.escape_html(this.span(item.hours)),
			action: "review",
		};
	}

	stuck_row(row, rank) {
		const bits = [];

		if (row.approved_on) {
			const when = frappe.datetime.str_to_obj(row.approved_on);
			if (when) {
				bits.push(__("You approved on {0}", [frappe.datetime.global_date_format(when)]));
			}
		}
		if (row.stuck_level && row.total_levels) {
			bits.push(__("Stuck at step {0} of {1}", [row.stuck_level, row.total_levels]));
		}
		const waiting = (row.waiting_on_names || []).length
			? row.waiting_on_names.join(", ")
			: (row.waiting_on_roles || []).join(", ");
		if (waiting) bits.push(__("With {0}", [this.title_case(waiting)]));

		const days = Math.round((Number(row.pending_hours) || 0) / 24);

		return {
			rank: rank,
			rank_class: "tm-rank-stuck",
			tag: frappe.utils.escape_html(__("Stuck")),
			tag_class: "tm-tag-stuck",
			doctype: frappe.utils.escape_html(row.doctype || ""),
			name: frappe.utils.escape_html(row.name || ""),
			meta: frappe.utils.escape_html(bits.join(" · ")),
			steps: this.steps(
				{ current: row.stuck_level, total: row.total_levels },
				row.my_level,
				true
			),
			step_label: frappe.utils.escape_html(
				row.stuck_level && row.total_levels
					? __("Stuck at step {0} of {1}", [row.stuck_level, row.total_levels])
					: ""
			),
			age_label: frappe.utils.escape_html(
				days === 1 ? __("Stuck 1 day") : __("Stuck {0} days", [this.comma(days)])
			),
			action: "follow",
		};
	}

	/**
	 * The step strip: one segment per level, cleared ones green, the one
	 * being waited on amber, the rest grey.
	 *
	 * Returns [] when the workflow does not number its levels. Real rows are
	 * in that position -- a state the level walk cannot reach in a graph with
	 * several entry points -- and drawing a bar with no honest number behind
	 * it would be inventing one.
	 */
	steps(level, cleared, stuck) {
		const total = parseInt(level.total, 10) || 0;
		const current = parseInt(level.current, 10) || 0;
		const done = parseInt(cleared, 10) || 0;
		if (!total || !current || total > 12) {
			return [];
		}

		const out = [];
		for (let i = 1; i <= total; i++) {
			let state = "todo";
			if (i < current) {
				// On a stuck row the green runs only as far as the reader
				// got; steps somebody else cleared are not theirs to claim.
				state = stuck && i > done ? "todo" : "done";
			} else if (i === current) {
				state = stuck ? "stuck" : "now";
			}
			out.push({ state: state });
		}
		return out;
	}

	// To-dos no longer have a card. They remain a row in the workload table,
	// which is where the count belongs -- 13 of 15 sampled readers have none,
	// and a card spent a header and a column of height saying so.

	due(row) {
		if (row.overdue) return __("Overdue");
		if (!row.date) return "";
		const when = frappe.datetime.str_to_obj(row.date);
		return when ? frappe.datetime.global_date_format(when) : "";
	}

	// ---- workload: the donut ---------------------------------------------

	/**
	 * Arcs on one circle, 180x180 with a 26px ring.
	 *
	 * Three things the maths has to get right:
	 *
	 * * **A 3px gap between slices.** Taken out of each arc rather than
	 *   drawn as a separate white stroke, so the gap is exact at any size.
	 * * **Rounded ends.** A round cap adds half the stroke width at each
	 *   end, so the dash has to be shortened by a whole stroke width to keep
	 *   the painted length honest. That is impossible for a slice shorter
	 *   than the ring is thick, so caps are rounded only when EVERY drawn
	 *   slice can take them -- mixing the two looks like a mistake.
	 * * **A floor on tiny slices.** A list holding 9 of 209 items is 4.3%,
	 *   but 1 of 209 is 0.5% and would vanish. Anything non-zero is drawn at
	 *   a minimum of 3% and the rest are scaled down to make room. The
	 *   legend still prints the true share -- the ring is adjusted, the
	 *   number never is.
	 */
	render_workload() {
		const w = this.data.workload || { total: 0, parts: [] };
		const parts = w.parts || [];
		const live = parts.filter((p) => p.count > 0);

		this.el("workload-total").text(this.comma(w.total || 0));
		this.el("donut-total").text(this.comma(w.total || 0));

		const R = 70;
		const SW = 28;
		const GAP = 3;
		const C = 2 * Math.PI * R;

		// Floor every visible slice at 3%, then rescale so the ring still
		// closes at exactly 100.
		const MIN = 3;
		const floored = live.map((p) => Math.max(Number(p.share) || 0, MIN));
		const sum = floored.reduce((a, b) => a + b, 0) || 1;
		const drawn = floored.map((v) => (v / sum) * 100);

		const lengths = drawn.map((pct) => (pct / 100) * C);
		const round = live.length > 1 && lengths.every((len) => len - GAP - SW > 2);

		// Track first, so a partial ring still reads as a ring.
		const track =
			`<circle class="tm-donut-track" cx="84" cy="84" r="${R}"` +
			` stroke-width="${SW}" fill="none"></circle>`;

		let at = 0;
		const arcs = live.map((p, i) => {
			const len = lengths[i];
			const dash = Math.max(0.6, len - GAP - (round ? SW : 0));
			const start = at + GAP / 2 + (round ? SW / 2 : 0);
			at += len;

			return {
				size: lengths[i],
				svg:
				`<circle class="tm-arc tm-arc-${frappe.utils.escape_html(p.key)}"` +
				` data-slice="${frappe.utils.escape_html(p.key)}"` +
				` cx="84" cy="84" r="${R}"` +
				` stroke-width="${SW}"` +
				` stroke-linecap="${round ? "round" : "butt"}"` +
				` stroke-dasharray="0 ${C}"` +
				` data-dash="${dash.toFixed(2)} ${(C - dash).toFixed(2)}"` +
				` stroke-dashoffset="${(-start).toFixed(2)}">` +
				`<title>${frappe.utils.escape_html(
					`${p.label}: ${this.comma(p.count)} (${p.share}%)`
				)}</title></circle>`,
			};
		});

		// Painted largest first so the smallest is drawn last and therefore
		// on top. SVG has no z-index: a 3% slice sitting under a 95% one
		// loses its rounded end to the neighbour that was drawn after it.
		// The angular positions are unchanged -- only the paint order is.
		const ordered = arcs.slice().sort((a, b) => b.size - a.size);

		const donut = this.el("workload-donut");
		donut.html(track + ordered.map((a) => a.svg).join(""));

		// Sweep in on the next frame. Setting the final dasharray in the same
		// frame as the element is created gives the transition nothing to
		// animate from.
		window.requestAnimationFrame(() => {
			donut.find(".tm-arc").each(function () {
				this.setAttribute("stroke-dasharray", this.getAttribute("data-dash"));
			});
		});

		this.el("workload-legend").html(
			parts
				.map(
					(p) =>
						`<li class="tm-leg tm-leg-${frappe.utils.escape_html(p.key)}"` +
						` data-slice="${frappe.utils.escape_html(p.key)}">` +
						`<span class="tm-leg-swatch" aria-hidden="true"></span>` +
						`${frappe.utils.escape_html(p.label)}</li>`
				)
				.join("")
		);

		// Zero-count lists keep their row -- "none" is a reading too.
		const oldest = (this.data.summary || {}).oldest_of || {};
		this.el("workload-table").html(
			parts
				.map(
					(p) =>
						`<tr class="tm-row-${frappe.utils.escape_html(p.key)}"` +
						` data-slice="${frappe.utils.escape_html(p.key)}">` +
						`<td><span class="tm-list-cell">` +
						`<span class="tm-swatch" aria-hidden="true"></span>` +
						`${frappe.utils.escape_html(p.label)}</span></td>` +
						`<td class="tm-num tm-items">${this.comma(p.count)}</td>` +
						`<td class="tm-num tm-share">${p.share}%</td>` +
						`<td class="tm-num ${
							oldest[p.key] ? "tm-oldest" : "tm-oldest-none"
						}">${
							oldest[p.key]
								? frappe.utils.escape_html(
										__("{0} days", [this.comma(oldest[p.key])])
								  )
								: "&mdash;"
						}</td>` +
						`</tr>`
				)
				.join("")
		);

	}

	/**
	 * Hovering a legend row highlights its slice and the other way round.
	 *
	 * Bound once on the root rather than per render, because both lists are
	 * rebuilt every two minutes and per-element handlers would accumulate.
	 */
	bind_slices() {
		const highlight = (key) => {
			const donut = this.el("workload-donut");
			donut.toggleClass("is-hovering", !!key);
			donut.find(".tm-arc").each(function () {
				$(this).toggleClass("is-active", $(this).attr("data-slice") === key);
			});
			this.root.find("tr[data-slice]").each(function () {
				$(this).toggleClass("is-active", $(this).attr("data-slice") === key);
			});
		};

		this.root.on("mouseenter", "[data-slice]", (e) => {
			highlight($(e.currentTarget).attr("data-slice"));
		});
		this.root.on("mouseleave", "[data-slice]", () => highlight(null));
	}

	// ---- the status strip above the queue --------------------------------

	/**
	 * "Stuck after you", as a line rather than a card.
	 *
	 * It is a statement about the approvals below it -- what happened to the
	 * ones you already cleared -- so it belongs to that card. Green is the
	 * common case and the quiet one; red only appears when something really
	 * is parked behind you, and then it carries a way to reach it.
	 */
	render_status() {
		const block = this.data.approved_by_me || {};
		const rows = block.rows || [];
		const total = block.shown || 0;

		const oldest = rows.reduce(
			(worst, row) => Math.max(worst, Number(row.pending_hours) || 0),
			0
		);

		const tile = total
			? '<svg viewBox="0 0 24 24"><path d="M12 8v5"/><path d="M12 16.5v.01"/>' +
			  '<path d="M10.3 3.9 2.4 17.6A2 2 0 0 0 4.1 20.6h15.8a2 2 0 0 0 1.7-3L13.7 3.9a2 2 0 0 0-3.4 0Z"/></svg>'
			: '<svg viewBox="0 0 24 24"><path d="m5 13 4.5 4.5L19 7"/></svg>';

		const strong = total
			? __("{0} stuck after you.", [this.comma(total)])
			: __("Stuck after you: none.");

		const rest = total
			? __("Oldest has waited {0}.", [this.span(oldest)])
			: __("Nothing you approved is stuck at a later step.");

		this.el("stuck-strip").html(
			`<div class="tm-status tm-status-${total ? "red" : "green"}">` +
			`<span class="tm-status-tile" aria-hidden="true">${tile}</span>` +
			`<span class="tm-status-text"><b>${frappe.utils.escape_html(strong)}</b> ` +
			`${frappe.utils.escape_html(rest)}</span>` +
			(total
				? `<button class="tm-status-link" type="button" data-action="view-stuck">${frappe.utils.escape_html(
						__("View")
				  )}</button>`
				: "") +
			`</div>`
		);
	}

	// ---- your requests ---------------------------------------------------

	render_requests() {
		const block = this.data.awaiting_others || {};
		const page = this.paginate(
			"requests",
			block.rows || [],
			task_manager.REQUEST_ROWS
		);

		const rows = page.rows.map((row) => {
			const draft = row.kind === "draft";
			const level = row.level || {};

			const step =
				level.current && level.total
					? __("Step {0} of {1}", [level.current, level.total])
					: draft
					? __("Not submitted")
					: "\u2014";


			return {
				doctype: frappe.utils.escape_html(row.doctype || ""),
				name: frappe.utils.escape_html(row.name || ""),
				status: frappe.utils.escape_html(
					draft ? __("Draft") : row.state || __("Pending")
				),
				step: frappe.utils.escape_html(step),
				age_label: frappe.utils.escape_html(this.span(row.pending_hours)),
			};
		});

		const host = this.el("requests");
		host.html(
			frappe.render_template("tm_requests", {
				rows: rows,
				empty_text: frappe.utils.escape_html(__("Nothing you raised is open.")),
			})
		);

		host.closest(".tm-table-scroll").removeClass("tm-fade");
		void host[0].offsetWidth;
		host.closest(".tm-table-scroll").addClass("tm-fade");

		const total = block.shown || 0;
		const days = ((this.data.summary || {}).oldest_of || {}).awaiting_others || 0;
		this.el("requests-note").text(
			total
				? days
					? __("{0} open · oldest {1} days", [this.comma(total), this.comma(days)])
					: __("{0} open", [this.comma(total)])
				: __("Nothing open")
		);
		this.el("requests-chip").text(total ? this.comma(total) : "");
		this.render_pager("requests", page, "orange");
	}

	// ---- profile ---------------------------------------------------------

	render_profile() {
		const p = this.data.profile || {};
		const host = this.el("profile");

		if (!p.full_name) {
			host.empty();
			return;
		}

		const meta = [p.designation, p.department].filter(Boolean).join(" · ");
		const coverage = p.coverage || {};

		host.html(
			frappe.render_template("tm_profile", {
				initials: frappe.utils.escape_html(p.initials || "?"),
				full_name: frappe.utils.escape_html(this.title_case(p.full_name || "")),
				meta: frappe.utils.escape_html(this.title_case(meta)),
				roles: (p.roles || []).map((r) => frappe.utils.escape_html(r)),
				roles_more: p.roles_truncated
					? frappe.utils.escape_html(
							__("+{0} more", [(p.roles_total || 0) - (p.roles || []).length])
					  )
					: "",
				area: frappe.utils.escape_html(coverage.area || ""),
				doctypes: (coverage.doctypes || []).map((d) =>
					frappe.utils.escape_html(d.label || d.doctype || "")
				),
				note: frappe.utils.escape_html(coverage.note || ""),
				roles_total: frappe.utils.escape_html(String(p.roles_total || 0)),
			})
		);

		// The page re-renders itself every two minutes. Re-applying the state
		// here is what stops an open panel from slamming shut mid-read.
		this.toggle_profile(this.profile_open, { silent: true });
	}

	toggle_profile(open, opts) {
		this.profile_open = !!open;

		const host = this.el("profile");
		const panel = host.find("[data-profile-panel]");
		const button = host.find("[data-action=profile]");

		panel.attr("hidden", this.profile_open ? null : "hidden");
		button.attr("aria-expanded", this.profile_open ? "true" : "false");
		host.toggleClass("is-open", this.profile_open);

		if (this.profile_open && !(opts && opts.silent) && panel.length) {
			panel[0].focus({ preventScroll: true });
		}
	}

	// ---- formatting ------------------------------------------------------

	// Thousands separators, done locally. There is no
	// frappe.utils.format_number -- the real one is the global
	// window.format_number -- and a three-line helper beats depending on
	// which globals a page happens to have loaded.
	comma(n) {
		return String(n == null ? "" : n).replace(/\B(?=(\d{3})+(?!\d))/g, ",");
	}

	/**
	 * Names come out of the database shouting -- "ANUJ DESAI", "TUSHAR MORE"
	 * -- because that is how they were typed into the HR master. A dashboard
	 * is not the place to repeat that.
	 *
	 * Only all-caps and all-lower words are touched. A name already in mixed
	 * case is left exactly as its owner wrote it, so "McGrath" and "van der
	 * Berg" survive. Separators inside a word are kept.
	 */
	title_case(text) {
		if (!text) return "";
		return String(text).replace(/[^\s·,]+/g, (word) => {
			if (/[a-z]/.test(word) && /[A-Z]/.test(word)) {
				return word;
			}
			return word.replace(
				/[A-Za-zÀ-ɏ]+/g,
				(part) => part.charAt(0).toUpperCase() + part.slice(1).toLowerCase()
			);
		});
	}

	/**
	 * Elapsed time in the largest honest unit.
	 *
	 *   under a day   hours
	 *   under a month days
	 *   under a year  months
	 *   beyond that   years
	 *
	 * This site has documents pending since 2023, and "27814 hours" reads as
	 * a broken number rather than as a long wait.
	 */
	span(hours) {
		if (hours === null || hours === undefined) return "";

		const h = Number(hours) || 0;
		if (h < 1) return __("under an hour");
		if (h < 24) {
			const n = Math.round(h);
			return n === 1 ? __("1 hour") : __("{0} hours", [n]);
		}

		const days = Math.round(h / 24);
		if (days < 30) return days === 1 ? __("1 day") : __("{0} days", [days]);

		const months = Math.round(days / 30.4);
		if (months < 12) return months === 1 ? __("1 month") : __("{0} months", [months]);

		const years = Math.round(days / 365);
		return years === 1 ? __("1 year") : __("{0} years", [years]);
	}

	// ---- actions ---------------------------------------------------------

	remind(button) {
		const doctype = button.attr("data-doctype");
		const name = button.attr("data-name");
		if (!doctype || !name || button.prop("disabled")) {
			return;
		}
		button.prop("disabled", true);

		frappe
			.call({
				method: "task_manager.api.send_reminder",
				args: { doctype: doctype, name: name },
			})
			.then(() => {
				frappe.show_alert({ message: __("Reminder sent"), indicator: "green" });
				this.load({ force: true, quiet: true });
			})
			.catch((err) => {
				console.error("[task_manager] send_reminder failed", doctype, name, err);
			})
			.always(() => {
				button.prop("disabled", false);
			});
	}
};
