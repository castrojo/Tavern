# Pages are instantiated by a PageFactory, not the window

Every page and dialog that needs `backend` or `task_manager` state was
manually instantiated and dependency-injected by `window.py`. The window
therefore knew every page's constructor signature and grew as a "god-object
factory" instead of a view-layout + navigation coordinator, new pages required
`window.py` edits, and pages could not be tested without constructing the
backend and task manager.

**Decision.** `src/page_factory.py` owns page/dialog construction and
dependency resolution. It holds the shared `backend` and `task_manager` and
exposes `wire_pages()` for the template-built pages (`browse`, `search`,
`installed`, `tap`) and `create_package_details()`, `create_version_history()`,
`create_brewfile_page()`, and `create_maintenance_page()` for the pages built
on demand. `TavernWindow` creates one `PageFactory` in `__init__` and delegates
to it; it no longer imports or constructs page modules directly.

**Why this way.** The window stays a coordinator that only connects page →
window signals and pushes returned pages onto the navigation stack. Adding a
page dependency changes only the factory and the page, never `window.py`.
Template-built pages cannot be constructed here, so they are wired rather than
created; dynamic pages are created by factory methods. Page modules are imported
lazily inside the factory, so their GTypes register when a page is first built
— the same runtime points they are used at, and the static pages that appear in
`window.blp` still register at window import time.

**Alternatives considered.** A service locator / context object that pages
request from is more opaque and harder to test than explicit factory methods;
for the small, fixed set of page types the explicit methods are clearer. Making
`PageFactory` a `GObject.Object` adds signal/property machinery it does not
need — a plain class is simpler and trivially unit-testable.
