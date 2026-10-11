# Architecture overview

Tavern is a Homebrew client for Linux and macOS: a GTK 4 / Libadwaita
desktop app (Python) shipped as the `org.tunaos.tavern` Flatpak and as a
Homebrew cask. This document maps how the code is layered, where work
happens, and how data flows from the Homebrew API and the local `brew` CLI
to the screen.

Companion docs cover the cache lifecycle ([`CACHE.md`](CACHE.md)), the
curation feed ([`CURATION.md`](CURATION.md)), the accessibility release
pass ([`ACCESSIBILITY.md`](ACCESSIBILITY.md)), and the release process
([`RELEASING.md`](RELEASING.md)); [`docs/adr/`](adr/) records the design
decisions behind them.

## System overview

```
                         GTK 4 / Libadwaita UI (main thread)
   window.py  browse/search/installed/tap/doctor/brewfile pages
   package_details.py  task_panel.py  command_dialog.py  …
        │  GObject signals: formulae-loaded, casks-loaded, installed-loaded,
        │  taps-loaded, outdated-changed, pinned-changed
        │  property bindings: loading_status → label, loading_progress → fraction
        ▼
   ┌───────────────────────────────────────────────────────────────┐
   │  BrewBackend  (GObject) — the single shared facade             │
   │  MixIn composition: Taps · Media · Cache · Remote · State · Ui │
   └───┬───────────────┬───────────────┬───────────────┬───────────┘
       │               │               │               │
   subprocess        network        filesystem      GObject
   (brew CLI)        (urlopen)       (JSON cache)    properties
       │               │               │               │
       ▼               ▼               ▼               ▼
   local `brew`    formulae.brew.sh  $XDG_CACHE_HOME/  signals +
   list/outdated   JSON API          tavern/*.json     idle_add callbacks
   tap/trust/…     Flathub appstream GitHub / forge    (main-thread only)
                   curation feed
```

The backend is the only layer that touches Homebrew, the network, or the
disk. The UI never runs `subprocess` or `urlopen` directly; it calls a
backend method and reacts to a signal or callback.

## Layer responsibilities

**UI layer** (`src/*.py` pages, `window.py`, `package_details.py`, …)
owns rendering, user input, and navigation only. It never blocks: every
long-running call is a backend method that returns asynchronously, and
state it displays is bound to backend GObject properties
(`loading_status`, `loading_progress`, per-`Package` `task_*`). Pages emit
signals (`package-activated`, `install-requested`, `outdated-count-changed`)
that the window wires to the backend and to sibling pages.

**Backend layer** (`BrewBackend`) owns all I/O and is the single source of
truth for catalog, installed, tap, and outdated data. It is a GObject so
pages can bind to its properties and connect to its signals. It exposes
both synchronous helpers (used by tests and the Brewfile loader) and
`*_async` wrappers (used by the UI).

**Business logic** — Homebrew command execution, curation validation,
cache policy, catalog ranking — lives in the backend mixins and their
policy modules (`brew_commands.py`, `catalog_policy.py`,
`cache_policy.py`). It is stateless with respect to the UI: the same
method returns the same result given the same inputs and cache state.

## Key abstractions

### MixIn composition

`BrewBackend` gains its behaviour by listing mixins in its base classes
([`src/backend.py`](../src/backend.py)):

```python
class BrewBackend(TapsMixin, MediaMixin, CacheMixin,
                  RemoteMixin, StateMixin, UiMixin, GObject.Object):
```

Each mixin (`TapsMixin`, `MediaMixin`, `CacheMixin`, `RemoteMixin`,
`StateMixin`, `UiMixin`) is a plain class mixed into `BrewBackend` at
definition time. The mixins assume the usual instance state
(`self._formulae`, `self._cache_dir`, `self._update_status`, …) exists on
the combined object — exactly as the methods did before the split
(tuna-os/Tavern#81). Splitting by concern keeps each module testable in
isolation: `backend_icons.py` is a pure ICO→PNG decoder with no backend
state at all.

### GObject as the UI contract

`BrewBackend` is a `GObject.Object` with properties and signals, not a plain
class. Properties (`loading`, `loading_status`, `loading_progress`) are
bound straight to widgets, so no manual "update the label" code is needed.
Signals (`formulae-loaded`, `casks-loaded`, `installed-loaded`,
`taps-loaded`, `outdated-changed`, `pinned-changed`) carry the parsed data
from the backend to whichever page listens.

### The `Package` model

`src/package.py` defines `Package(GObject.Object)`, the row shared across
every page. It wraps a formula or cask API record and carries live task
state (`task_active`, `task_progress`, `task_label`) driven by the
`TaskManager`, plus vulnerability, analytics, and tap fields. Pages render
`Package` objects; they never parse raw API dicts.

### Operations

Install/remove/upgrade are coordinated by `TaskManager`
([`src/task_manager.py`](../src/task_manager.py)), a single object the window
and every page share. It runs `brew` on a worker thread, maps raw `brew`
output to user-visible phases ("Installing…", "Pouring…", …), and drives
each `Package`'s inline progress bar. A failed ambiguous-tap or
multi-tap-conflict install surfaces a resolution dialog; on completion the
window refreshes installed state via `backend.refresh_installed_async()`.

## Threading model

The GTK main thread runs the D-Bus loop and draws. **All blocking I/O runs
on a thread; results come back on the main thread via `GLib.idle_add`**
(CONTRIBUTING.md: "Backend I/O goes on a thread … don't block the UI
thread"). Concretely:

- **Catalog load** — `UiMixin.load_all_async()` starts a daemon thread
  (`_load_all_thread`) that reads installed state, loads cached catalog,
  refreshes stale catalog from the API, scans taps, and builds the search
  index — publishing progress through `loading_progress` and `loading_status`.
- **Search** — `search_async()` submits to a single-thread executor and tags
  each query with a generation counter. If a newer query arrives first, the
  older result is dropped, so fast typing never floods the UI (issue #49).
- **Icons** — `MediaMixin.fetch_icon_async()` runs on a 6-worker pool;
  concurrent requests for the same package are coalesced into one fetch and
  every caller receives the result.
- **Background refresher** — `TavernApplication` starts a daemon thread on a
  2-hour `GLib.timeout_add_seconds` timer (and once at ~10 s) to refresh the
  catalog while the app is idle.
- **Operations** — each `*_async` task method (install, tap, pin, …) starts
  its own daemon thread and returns success via a callback on the main loop.

Thread-safety is enforced with explicit locks where shared mutable state
exists: `_refresh_lock` serialises catalog refreshes (with a double-checked
"was it refreshed by another thread?" guard), `_outdated_lock` guards the
outdated dicts, `_pinned_lock` guards the pinned set, and `_icon_lock`
guards the in-flight coalescing map.

## Module map

| File | Layer | Owns |
| --- | --- | --- |
| `main.py` | entry point | Loads the gresource bundle *before* importing `Gtk.Template` classes, then starts `TavernApplication`; records a startup timeline. |
| `application.py` | application | `Adw.Application` singleton; command-line/`brew://`/deep-link handling; GNOME search-provider action; background cache-refresher worker. |
| `window.py` | UI | `TavernWindow`; owns the shared `BrewBackend` and `TaskManager`; wires pages to backend signals; handles toasts, badges, and deep-link opening. |
| `backend.py` | backend | `BrewBackend` facade + `_brew_cmd` (resolves `flatpak-spawn --host` vs `brew`); re-exports `Package`. |
| `backend_ui.py` | backend | `UiMixin`: async catalog load, async search, `loading_status`/`loading_progress` plumbing. |
| `backend_remote.py` | backend | `RemoteMixin`: JSON catalog/detail/analytics fetches, curation, version history, related-package lookup. |
| `backend_cache.py` | backend | `CacheMixin`: local cache I/O, host Homebrew JWS cache reads, Brewfile parsing, Linux cask filtering. |
| `backend_state.py` | backend | `StateMixin`: installed/outdated/pinned state via the `brew` CLI. |
| `backend_icons.py` | backend | Pure Python ICO→PNG decoder (no backend state). |
| `media.py` | backend | `MediaMixin`: icon/screenshot/README fetching, bounded pools, attacker-influenced payload caps. |
| `taps.py` | backend | `TapsMixin`: tap scan/trust/operations and the popular-tap browser. |
| `package.py` | model | `Package`: the shared formula/cask row. |
| `task_manager.py` | operations | `TaskManager`: central install/remove/upgrade coordinator; `brew` output → phase mapping. |
| `catalog_policy.py` | policy | Catalog validation and search ranking. |
| `cache_policy.py` | policy | Atomic JSON cache read/write, TTLs, eviction. |
| `brew_commands.py` | policy | `brew` subcommand construction. |
| `search_provider.py` | integration | GNOME Shell search (D-Bus `SearchProvider2`), backed by the cached `linux_packages.json`. |
| `git_forge.py` | backend | Detects GitHub/GitLab/Codeberg from a source URL for version-history fetches. |
| `brew_env.py` | platform | `IN_FLATPAK` / `BREW_BIN` detection. |

## Data flow: opening a package

1. The UI calls `backend.search_async()` or `browse_page.populate_*()`;
   catalog rows arrive on `formulae-loaded`/`casks-loaded`.
2. Activating a row pushes `package_details.py` onto the window's
   `navigation_view`, sharing the same `backend` and `task_manager`.
3. Details load from the formula/cask **detail API** on a worker thread
   (`get_package_info_async`); icon, screenshot, and README arrive via
   `MediaMixin` callbacks; analytics and related packages come from
   `RemoteMixin`.
4. Installing routes through `TaskManager`, which streams `brew` output back
   to the tile's progress bar; on completion the window refreshes installed
   state and the outdated badge.

## Cross-references

- Cache TTLs, atomic writes, and eviction: [`docs/CACHE.md`](CACHE.md).
- The curation feed and its validation: [`docs/CURATION.md`](CURATION.md).
- The Homebrew ≥ 6.0.0 floor decision: [`docs/adr/0001-homebrew-6-floor.md`](adr/0001-homebrew-6-floor.md).
