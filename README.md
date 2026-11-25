### Task Manager

Personal task and approval dashboard for ERPNext

### Installation

You can install this app using the [bench](https://github.com/frappe/bench) CLI:

```bash
cd $PATH_TO_YOUR_BENCH
bench get-app $URL_OF_THIS_REPO --branch develop
bench install-app task_manager
```

### Opening the dashboard

With `bench start` running, paste this into the browser:

```
http://localhost:8000/app/task-manager
```

The page is registered as the Frappe desk Page `task-manager`, so the route is
`/app/task-manager` on any site that has the app installed. On this bench the
default site is `site_local` and the webserver port is `8000`; if you serve a
different site by hostname, use `http://<site-name>:8000/app/task-manager`
instead. An unauthenticated request redirects to `/login` first.

### Page templates

The dashboard is a Frappe desk page. Its HTML templates live in
`task_manager/task_manager/page/task_manager/`:

| File | Purpose |
| --- | --- |
| [task_manager.html](task_manager/task_manager/page/task_manager/task_manager.html) | Dashboard shell |
| [tm_kpis.html](task_manager/task_manager/page/task_manager/tm_kpis.html) | The four lists as one KPI strip |
| [tm_queue.html](task_manager/task_manager/page/task_manager/tm_queue.html) | Ranked "do these first" queue |
| [tm_requests.html](task_manager/task_manager/page/task_manager/tm_requests.html) | "Your requests" -- docs you raised, parked on someone else |
| [tm_stuck.html](task_manager/task_manager/page/task_manager/tm_stuck.html) | "Stuck after you" -- docs you cleared, not acted on since |
| [tm_profile.html](task_manager/task_manager/page/task_manager/tm_profile.html) | Identity and scope panel |

The page's `task_manager.js`, `task_manager.css`, and `task_manager.json`
sit alongside them in the same folder.

### Contributing

This app uses `pre-commit` for code formatting and linting. Please [install pre-commit](https://pre-commit.com/#installation) and enable it for this repository:

```bash
cd apps/task_manager
pre-commit install
```

Pre-commit is configured to use the following tools for checking and formatting your code:

- ruff
- eslint
- prettier
- pyupgrade

### License

mit
