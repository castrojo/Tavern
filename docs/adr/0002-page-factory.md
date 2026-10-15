# Pages come from a PageFactory, not the window

Before this change, `window.py` made every page and dialog that needs
`backend` or `task_manager` state. The window had to know the constructor of
each page. Each new page needed a change in `window.py`. A test of a page
needed a real backend and a real task manager.

**Decision.** `src/page_factory.py` makes pages and dialogs and gives them
their dependencies. It holds the shared `backend` and `task_manager`.
`wire_pages()` connects the pages that `window.blp` makes (`browse`, `search`,
`installed`, `tap`). `create_package_details()`, `create_version_history()`,
`create_brewfile_page()`, and `create_maintenance_page()` make the other
pages when the user opens them. `TavernWindow` makes one `PageFactory` in
`__init__` and sends these calls to it.

**Reasons.** The window only connects page signals and pushes pages onto the
navigation stack. A new page dependency changes only the factory and the page.
The factory cannot make the pages in `window.blp`, thus it only connects them.
The factory imports each page module when it first makes that page. The pages
in `window.blp` still load when `window.py` loads.

**Alternatives.** We did not use a service locator. It hides the dependencies
of a page, and it is more difficult to test. We did not make `PageFactory` a
`GObject.Object`. The factory does not need signals or properties.
