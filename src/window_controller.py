# window_controller.py - Orchestration layer for the main window
# SPDX-License-Identifier: GPL-3.0-or-later
"""Backend lifecycle, task handling, page navigation and deep-link handling
extracted out of :class:`TavernWindow` (see tuna-os/Tavern#208).

The window is a thin ``Gtk.Template`` view; this class owns the stateful
coordination between the backend, the task manager and the pages. The window
keeps thin compatibility delegates (see ``window.py``) so existing callers and
the ``test_window.py`` contract stay unchanged while the logic no longer lives
inside the ``Adw.ApplicationWindow`` subclass.
"""

import threading

import gi
gi.require_version('Gtk', '4.0')
gi.require_version('Adw', '1')

from gi.repository import Adw, Gtk, Gio
from .logging_util import get_logger

from gettext import gettext as _

_log = get_logger('window_controller')


class WindowController:
    """Owns backend lifecycle, task-manager handlers, navigation and deeplinks.

    Parameters
    ----------
    window:
        The :class:`TavernWindow` instance. Widget handles and per-window state
        (deeplink targets, brewfile tabs, outdated-count bookkeeping) stay on
        the view; the controller reaches them through ``window``.
    backend:
        The shared :class:`BrewBackend`.
    task_manager:
        The shared :class:`TaskManager`.
    """

    def __init__(self, window, backend, task_manager):
        self._win = window
        self.backend = backend
        self.task_manager = task_manager

    # ── Backend lifecycle & cache refresh loops ────────────────────────
    def _periodic_outdated_check(self):
        if self._win._settings.get_boolean('outdated-check-enabled'):
            threading.Thread(target=self.backend._check_outdated, daemon=True).start()
        return True  # keep the timer active

    def _on_formulae_loaded(self, backend, packages):
        _log.info('Formulae loaded: %d packages', len(packages))
        self._win._formulae_loaded = True
        self._win.browse_page.populate_formulae(packages)
        self._win.search_page.set_packages(backend.formulae, backend.casks)
        self._check_deeplink()

    def _on_casks_loaded(self, backend, packages):
        _log.info('Casks loaded: %d packages', len(packages))
        self._win._casks_loaded = True
        self._win.browse_page.populate_casks(packages)
        self._win.search_page.set_packages(backend.formulae, backend.casks)
        self._check_deeplink()

    def _on_installed_loaded(self, backend, _):
        _log.debug('Installed-loaded signal received')

    def _on_backend_loading_changed(self, backend, _pspec):
        if backend.loading:
            return

        if not self._win._initial_load_done:
            self._win._initial_load_done = True
            # Transition to main content smoothly
            self._win.content_stack.set_visible_child_name("main")

        if self._win._package_to_open:
            self.open_package_by_name(self._win._package_to_open, show_not_found=True)
            self._win._package_to_open = None

        if self._win._tap_to_open:
            self.open_tap_by_name(self._win._tap_to_open)
            self._win._tap_to_open = None

        if self._win._brewfile_to_open:
            self.open_brewfile(self._win._brewfile_to_open)
            self._win._brewfile_to_open = None

    def _on_refresh(self, action, param):
        _log.info('Manual refresh triggered')
        self._win.browse_page.set_loading()
        self.backend.load_all_async()
        self._win.toast_overlay.add_toast(Adw.Toast.new(_('Refreshing package list…')))

    # ── Cache consistency: keep the Installed tab badge in sync ─────────
    def _on_outdated_count_changed(self, page, count):
        """Update the Installed tab badge when the outdated count changes."""
        installed_stack_page = self._win.main_stack.get_page(self._win.installed_page)

        if count == 0:
            installed_stack_page.set_badge_number(0)
            installed_stack_page.set_needs_attention(False)
            return

        installed_stack_page.set_badge_number(count)
        installed_stack_page.set_needs_attention(True)

        # Announce via toast, but only when the count actually changes —
        # outdated-changed is re-emitted several times during loading.
        if count > 0 and count != self._win._toasted_outdated_count:
            self._win._toasted_outdated_count = count
            msg = _('{count} package can be updated').format(count=count) if count == 1 else _('{count} packages can be updated').format(count=count)
            self._win.toast_overlay.add_toast(Adw.Toast.new(msg))

    # ── Deep-link handling ─────────────────────────────────────────────
    def _find_package_by_name(self, package_name):
        target = (package_name or '').strip().lower()
        if not target:
            return None

        for pkg in self.backend.formulae:
            if pkg.name.lower() == target or (pkg.display_name and pkg.display_name.lower() == target):
                return pkg

        for pkg in self.backend.casks:
            if pkg.name.lower() == target or (pkg.display_name and pkg.display_name.lower() == target):
                return pkg

        return None

    def open_package_by_name(self, package_name, show_not_found=True):
        """Open a package details page by name (deeplink support)."""
        _log.info('Attempting to open package: %s', package_name)

        package = self._find_package_by_name(package_name)
        if package:
            _log.info('Found package: %s (%s)', package.name, package.pkg_type)
            self._on_package_activated(None, package)
            return True

        if show_not_found:
            _log.warning('Package not found: %s', package_name)
            self._win.toast_overlay.add_toast(Adw.Toast.new(_(f'Package "{package_name}" not found')))
        return False

    def _check_deeplink(self):
        """Check if we should open a package or tap from deeplink after data loads."""
        if self._win._package_to_open:
            if self.open_package_by_name(self._win._package_to_open, show_not_found=False):
                self._win._package_to_open = None
        if self._win._tap_to_open:
            if self.open_tap_by_name(self._win._tap_to_open):
                self._win._tap_to_open = None

    def open_tap_by_name(self, tap_name):
        """Switch to Taps page and select a tap by name (deeplink support)."""
        _log.info('Attempting to open tap: %s', tap_name)
        self._win.main_stack.set_visible_child_name("taps")
        if self._win.tap_page.select_tap(tap_name):
            return True
        _log.warning('Tap not found: %s', tap_name)
        self._win.toast_overlay.add_toast(Adw.Toast.new(_(f'Tap "{tap_name}" not found')))
        return False

    # ── Task manager event handlers ────────────────────────────────────
    def _on_task_added(self, mgr, task):
        _log.info('Task added: %s', task.title)
        op_label = task.title
        self._win.toast_overlay.add_toast(Adw.Toast.new(f'{op_label}…'))

    def _on_task_finished(self, mgr, task):
        _log.info('Task finished: %s  status=%s', task.title, task.status)
        pkg = task.package
        from .task_manager import TaskStatus
        if pkg is None:
            self._win.toast_overlay.add_toast(Adw.Toast.new(
                task.title + ': ' + task.status_text
            ))
            if mgr.active_count == 0:
                self.backend.refresh_installed_async()
            return
        if task.status == TaskStatus.COMPLETED:
            verb = _('Installed') if task.operation == 'install' else (
                _('Removed') if task.operation == 'uninstall' else _('Upgraded')
            )
            self._win.toast_overlay.add_toast(Adw.Toast.new(
                f'{verb}: {pkg.display_name or pkg.name}'
            ))
        elif task.status == TaskStatus.FAILED:
            if task.ambiguous_taps:
                self._offer_ambiguous_tap_choice(task)
            elif task.conflict_info:
                self._offer_tap_conflict_resolution(task)
            else:
                self._win.toast_overlay.add_toast(Adw.Toast.new(
                    _('Failed: {name}').format(name=pkg.display_name or pkg.name)
                ))
        # Refresh installed page
        self._win.installed_page.refresh(self.backend)
        if mgr.active_count == 0:
            _log.info('All queued tasks finished; refreshing installed state')
            self.backend.refresh_installed_async()

    def _on_active_count_changed(self, mgr, pspec):
        count = mgr.active_count
        if count > 0:
            self._win.task_button.set_tooltip_text(
                _('{count} task running').format(count=count) if count == 1
                else _('{count} tasks running').format(count=count)
            )
            self._win.task_indicator_stack.set_visible_child_name('active')
            self._win.task_count_label.set_label(str(count))
            self._win.task_count_label.set_visible(True)
        else:
            self._win.task_button.set_tooltip_text(_('Downloads & Tasks'))
            self._win.task_indicator_stack.set_visible_child_name('idle')
            self._win.task_count_label.set_visible(False)

    def _on_task_progress_changed(self, mgr, task):
        pass  # progress visible in task panel and tile inline bar

    def _on_task_button_clicked(self, button):
        from .task_panel import TavernTaskPanel
        panel = TavernTaskPanel(task_manager=self.task_manager)
        panel.present(self._win)

    def _on_install_requested(self, page, package):
        _log.info('Install requested from page: %s (%s)', package.name, package.pkg_type)
        self.task_manager.install(package)

    def _on_remove_requested(self, page, package):
        _log.info('Remove requested from page: %s (%s)', package.name, package.pkg_type)
        self.task_manager.remove(package)

    def _preview_install(self, task, approve):
        from .command_dialog import show_command
        show_command(self._win, _('Review Installation'),
                     _('Homebrew will show the packages and dependencies it would install. '
                       'The actual installation may differ if Homebrew changes before it runs.'),
                     args=(*task.command, '--dry-run'), confirm=approve,
                     confirm_label=_('Install'), cancelled=task._set_cancelled,
                     allow_legacy_install=True)

    def _on_ambiguous_tap_response(self, dialog, response, listbox, pkg):
        if response != 'install':
            return
        row = listbox.get_selected_row()
        if not row:
            return
        qualified = getattr(row, '_qualified', None)
        if not qualified:
            return
        _log.info('User chose qualified install: %s', qualified)
        self.task_manager.install_qualified(pkg, qualified)

    def _on_conflict_resolution(self, dialog, response, task):
        if response != 'switch':
            return
        pkg = task.package
        _log.info('Switching tap for %s: uninstall then reinstall', pkg.name)
        # Queue the uninstall, then re-queue the install once the remove
        # task finishes (see _on_conflict_uninstall_finished).
        self.task_manager.remove(pkg)
        self._win._pending_reinstall = pkg
        if not hasattr(self._win, '_conflict_conn'):
            self._win._conflict_conn = self.task_manager.connect(
                'task-finished', self._on_conflict_uninstall_finished
            )

    def _on_conflict_uninstall_finished(self, mgr, task):
        from .task_manager import TaskOperation, TaskStatus
        pkg = getattr(self._win, '_pending_reinstall', None)
        if not pkg or task.package is not pkg or task.operation != TaskOperation.REMOVE:
            return
        self._win._pending_reinstall = None
        if task.status == TaskStatus.COMPLETED:
            self.task_manager.install(pkg)
        else:
            self._win.toast_overlay.add_toast(Adw.Toast.new(
                _('Could not uninstall {name} — tap switch cancelled').format(name=pkg.name)
            ))

    def _offer_ambiguous_tap_choice(self, task):
        """Show a dialog letting the user pick which tap to install from."""
        pkg = task.package
        options = task.ambiguous_taps  # ['user/tap/name', ...]

        dialog = Adw.AlertDialog()
        dialog.set_heading(_('Choose a Tap'))
        dialog.set_body(
            _('"{name}" is available from multiple taps.\nChoose which one to install from:').format(name=pkg.display_name or pkg.name)
        )
        dialog.add_response('cancel', _('Cancel'))
        dialog.set_close_response('cancel')

        listbox = Gtk.ListBox()
        listbox.add_css_class('boxed-list')
        listbox.set_selection_mode(Gtk.SelectionMode.SINGLE)
        listbox.set_margin_top(8)

        for qualified in options:
            # Parse tap name from fully-qualified: user/repo/pkg → user/repo
            parts = qualified.rsplit('/', 1)
            tap_name = parts[0] if len(parts) == 2 else qualified
            row = Adw.ActionRow()
            row.set_title(qualified)
            row.set_subtitle(_('from tap: {tap}').format(tap=tap_name))
            row._qualified = qualified
            listbox.append(row)

        # Select first row by default
        first = listbox.get_row_at_index(0)
        if first:
            listbox.select_row(first)

        dialog.set_extra_child(listbox)
        dialog.add_response('install', _('Install'))
        dialog.set_response_appearance('install', Adw.ResponseAppearance.SUGGESTED)
        dialog.set_default_response('install')
        dialog.connect('response', self._on_ambiguous_tap_response, listbox, pkg)
        dialog.present(self._win)

    def _offer_tap_conflict_resolution(self, task):
        """Show a dialog offering to switch taps when a multi-tap conflict occurs."""
        pkg = task.package
        info = task.conflict_info
        installed_tap = info['installed_tap']
        target_tap    = info['target_tap']
        is_core = target_tap in ('homebrew/core', 'homebrew/cask')

        dialog = Adw.AlertDialog()
        dialog.set_heading(_('Tap Conflict'))
        dialog.set_body(
            _('"{name}" is already installed from the {tap} tap.\n\n{resolution}').format(
                name=pkg.display_name or pkg.name,
                tap=installed_tap,
                resolution=(
                    _('Would you like to uninstall it from {src} and reinstall from {dst}?').format(src=installed_tap, dst=target_tap)
                    if is_core else
                    _('Formulae with the same name from different taps cannot both be installed.')
                )
            )
        )
        dialog.add_response('cancel', _('Cancel'))
        if is_core:
            dialog.add_response('switch', _('Switch to {tap}').format(tap=target_tap))
            dialog.set_response_appearance('switch', Adw.ResponseAppearance.SUGGESTED)
        dialog.set_default_response('cancel')
        dialog.set_close_response('cancel')
        dialog.connect('response', self._on_conflict_resolution, task)
        dialog.present(self._win)

    def _on_tap_operation(self, page, message):
        self._win.toast_overlay.add_toast(Adw.Toast.new(message))

    # ── Page navigation ────────────────────────────────────────────────
    def _on_package_activated(self, page, package):
        _log.debug('Package activated: %s (%s)', package.name, package.pkg_type)
        from .package_details import TavernPackageDetails
        dialog = TavernPackageDetails(
            package=package,
            backend=self.backend,
            task_manager=self.task_manager,
        )
        dialog.connect('package-changed', self._on_package_changed)
        dialog.connect('package-history-requested', self._on_package_history_requested)
        dialog.connect('package-activated', self._on_package_activated)
        self._win.navigation_view.push(dialog)

    def _on_package_changed(self, dialog, package):
        # Refresh installed page when something is installed/removed
        self._win.installed_page.refresh(self.backend)

    def _on_search_action(self, action, param):
        """Jump to the Search page and focus its entry (header button / Ctrl+F)."""
        self._win.main_stack.set_visible_child_name('search')
        if hasattr(self._win.search_page, 'focus_search'):
            self._win.search_page.focus_search()

    def _on_update_selected_tap(self, action, param):
        """Run `git pull` on whichever tap is currently selected in the Tap page."""
        if hasattr(self._win.tap_page, 'update_selected_tap'):
            self._win.tap_page.update_selected_tap()

    def _on_maintenance(self, *_args):
        from .maintenance_page import MaintenancePage
        self._win.navigation_view.push(MaintenancePage(self.task_manager))

    def _on_package_history_requested(self, card, package):
        """Open version history dialog for a package."""
        _log.debug('Package history requested: %s', package.name)
        from .version_history_dialog import TavernVersionHistoryDialog
        version_dialog = TavernVersionHistoryDialog(
            package=package,
            backend=self.backend,
        )
        version_dialog.connect('pin-version', self._on_pin_version_requested)
        self._win.navigation_view.push(version_dialog)

    def _on_pin_version_requested(self, dialog, version):
        """Pin the currently displayed package's installed version.

        Homebrew's `brew pin` always pins the currently-installed version
        rather than an arbitrary historical one, so the `version` argument
        is informational only — the actual behavior is "stop upgrading
        whichever version you have right now".
        """
        package = getattr(dialog, '_package', None) or getattr(dialog, 'package', None)
        if package is None:
            _log.warning('Pin requested but dialog has no package attached')
            return
        if package.pkg_type not in ('formula', 'cask'):
            self._win.toast_overlay.add_toast(Adw.Toast.new(_('Pinning is only supported for formulae and casks')))
            return

        def _on_pin_done(success, msg):
            text = (
                _('Pinned {name}').format(name=package.name)
                if success
                else _('Failed to pin {name}').format(name=package.name)
            )
            self._win.toast_overlay.add_toast(Adw.Toast.new(text))

        _log.info('Pinning %s (requested version %s)', package.name, version)
        self.backend.pin_async(package, _on_pin_done)

    def _on_open_brewfile(self, action, param):
        _log.info('Open Brewfile action triggered')
        from gi.repository import Gtk

        # Create file filter for .Brewfile files
        filter_brewfile = Gtk.FileFilter()
        filter_brewfile.set_name(_('Brewfile'))
        filter_brewfile.add_pattern('*.Brewfile')
        filter_brewfile.add_pattern('Brewfile')

        filter_all = Gtk.FileFilter()
        filter_all.set_name(_('All files'))
        filter_all.add_pattern('*')

        filters = Gio.ListStore.new(Gtk.FileFilter)
        filters.append(filter_brewfile)
        filters.append(filter_all)

        # Create and configure file dialog
        dialog = Gtk.FileDialog()
        dialog.set_title(_('Open Brewfile'))
        dialog.set_filters(filters)
        dialog.set_default_filter(filter_brewfile)

        # Suggest the ublue-os brewfile directory if it exists
        import os
        default_path = '/usr/share/ublue-os/homebrew'
        if os.path.exists(default_path):
            initial_folder = Gio.File.new_for_path(default_path)
            dialog.set_initial_folder(initial_folder)

        # Open dialog and handle response
        dialog.open(self._win, None, self._on_brewfile_selected)

    def _on_brewfile_selected(self, dialog, result):
        try:
            file = dialog.open_finish(result)
            if file:
                path = file.get_path()
                _log.info('User selected Brewfile: %s', path)
                self.open_brewfile(path)
        except Exception as e:
            if 'dismissed' not in str(e).lower():
                _log.error('Error opening Brewfile: %s', e)
                self._win.toast_overlay.add_toast(Adw.Toast.new(_('Failed to open Brewfile')))

    def open_brewfile(self, path):
        """Open a Brewfile as a new tab in the main window."""
        import os

        _log.info('open_brewfile called with path: %s', path)

        # Normalize the path for consistent comparison
        abs_path = os.path.abspath(path)

        # Check if this Brewfile is already open
        for page_name, brewfile_path in self._win._open_brewfiles.items():
            if os.path.abspath(brewfile_path) == abs_path:
                _log.info('Brewfile already open: %s', abs_path)
                self._win.main_stack.set_visible_child_name(page_name)
                self._win.toast_overlay.add_toast(Adw.Toast.new(_(f'Already viewing {os.path.basename(path)}')))
                return

        # Extract filename for tab title
        filename = os.path.basename(path)
        # Remove .Brewfile extension
        if filename.endswith('.Brewfile'):
            title = filename[:-9]  # Remove '.Brewfile'
        elif filename == 'Brewfile':
            title = 'Brewfile'
        else:
            title = filename

        # Capitalize first letter
        title = title.capitalize()

        # Create brewfile page
        from .brewfile_page import TavernBrewfilePage
        brewfile_page = TavernBrewfilePage()
        brewfile_page.set_backend_and_manager(self.backend, self.task_manager)

        # Connect signals
        brewfile_page.connect('package-activated', self._on_package_activated)
        brewfile_page.connect('install-requested', self._on_install_requested)

        # Add as a new tab with a unique name
        self._win._brewfile_page_count += 1
        page_name = f'brewfile_{self._win._brewfile_page_count}'

        # Track this Brewfile
        self._win._open_brewfiles[page_name] = abs_path

        # Add page to stack
        stack_page = self._win.main_stack.add_titled(
            brewfile_page,
            page_name,
            title
        )

        # Switch to the new tab
        self._win.main_stack.set_visible_child_name(page_name)

        # Load the brewfile
        _log.info('Calling load_brewfile for: %s', path)
        brewfile_page.load_brewfile(path)

        _log.info('Added Brewfile tab: %s', title)
