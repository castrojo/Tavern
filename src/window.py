# window.py - Main application window
# SPDX-License-Identifier: GPL-3.0-or-later

import gettext

import gi
gi.require_version('Gtk', '4.0')
gi.require_version('Adw', '1')

from gi.repository import Adw, Gtk, Gio, GLib, GObject
from .backend import BrewBackend
from .task_manager import TaskManager
from .logging_util import get_logger
from .window_controller import WindowController

_ = gettext.gettext

_log = get_logger('window')

# These imports register the GTypes BEFORE the window template is parsed.
# GTK needs to know about these custom widget types when building the UI.
from .browse_page import TavernBrowsePage      # noqa: F401
from .search_page import TavernSearchPage      # noqa: F401
from .installed_page import TavernInstalledPage  # noqa: F401
from .tap_page import TavernTapPage            # noqa: F401
from .doctor_page import TavernDoctorPage      # noqa: F401
from .global_progress import TavernGlobalProgress # noqa: F401
from .brewfile_page import TavernBrewfilePage  # noqa: F401
from .version_history_dialog import TavernVersionHistoryDialog  # noqa: F401


@Gtk.Template(resource_path='/org.tunaos.tavern/window.ui')
class TavernWindow(Adw.ApplicationWindow):
    __gtype_name__ = 'TavernWindow'

    toast_overlay = Gtk.Template.Child()
    content_stack = Gtk.Template.Child()
    browse_page = Gtk.Template.Child()
    search_page = Gtk.Template.Child()
    installed_page = Gtk.Template.Child()
    tap_page = Gtk.Template.Child()
    main_stack = Gtk.Template.Child()
    task_button = Gtk.Template.Child()
    task_indicator_stack = Gtk.Template.Child()
    task_count_label = Gtk.Template.Child()
    navigation_view = Gtk.Template.Child()
    loading_label = Gtk.Template.Child()
    loading_progress_bar = Gtk.Template.Child()

    def __init__(self, package_to_open=None, **kwargs):
        import time
        init_start = time.perf_counter()

        super().__init__(**kwargs)
        _log.info('TavernWindow.__init__: starting')

        # Store deeplink target
        self._package_to_open = package_to_open
        self._tap_to_open = self.get_application()._tap_to_open
        self._brewfile_to_open = self.get_application()._brewfile_to_open
        self._formulae_loaded = False
        self._casks_loaded = False
        self._brewfile_page_count = 0  # Counter for unique brewfile tab names
        self._open_brewfiles = {}  # page_name -> abs_path
        # Track initial fetching
        self._initial_load_done = False

        # Show loading page
        self.content_stack.set_visible_child_name("loading")

        # Shared backend
        backend_start = time.perf_counter()
        self.backend = BrewBackend()
        self.backend.bind_property('loading_status', self.loading_label, 'label', GObject.BindingFlags.DEFAULT)
        self.backend.bind_property('loading_progress', self.loading_progress_bar, 'fraction', GObject.BindingFlags.DEFAULT)
        backend_time = (time.perf_counter() - backend_start) * 1000
        _log.info('Backend created: %.1f ms', backend_time)

        # Task manager (central operation coordinator)
        task_mgr_start = time.perf_counter()
        self.task_manager = TaskManager(self.backend)

        # Create the controller that owns backend lifecycle, task handlers,
        # navigation and deeplinks. The window keeps all state fields and
        # widgets; the controller accesses them via self._win.<attr>.
        self._controller = WindowController(self, self.backend, self.task_manager)

        # Task manager calls self.install_preview(task, approve) during previews.
        # Route it through the controller so the review dialog logic lives there.
        self.task_manager.install_preview = self._controller._preview_install
        self.task_manager.connect('task-added', self._controller._on_task_added)
        self.task_manager.connect('task-finished', self._controller._on_task_finished)
        self.task_manager.connect('notify::active-count', self._controller._on_active_count_changed)
        self.task_manager.connect('task-changed', self._controller._on_task_progress_changed)
        task_mgr_time = (time.perf_counter() - task_mgr_start) * 1000
        _log.info('Task manager created: %.1f ms', task_mgr_time)

        # Task button in header bar
        self.task_button.connect('clicked', self._controller._on_task_button_clicked)

        self._outdated_count = 0  # Track current outdated package count
        self._toasted_outdated_count = None  # Last count announced via toast

        # Wire pages to backend
        pages_start = time.perf_counter()
        self.browse_page.set_backend(self.backend)
        self.search_page.set_backend(self.backend)
        self.installed_page.set_backend_and_manager(self.backend, self.task_manager)
        self.tap_page.set_backend(self.backend)

        # Wire package open signal from pages
        self.browse_page.connect('package-activated', self._controller._on_package_activated)
        self.search_page.connect('package-activated', self._controller._on_package_activated)
        self.installed_page.connect('package-activated', self._controller._on_package_activated)
        self.tap_page.connect('package-activated', self._controller._on_package_activated)
        self.tap_page.connect('tap-operation', self._controller._on_tap_operation)
        self.installed_page.connect('outdated-count-changed', self._controller._on_outdated_count_changed)

        # Wire package install/remove signals from inline tile buttons
        self.browse_page.connect('install-requested', self._controller._on_install_requested)
        self.search_page.connect('install-requested', self._controller._on_install_requested)
        self.installed_page.connect('install-requested', self._controller._on_install_requested)
        self.tap_page.connect('install-requested', self._controller._on_install_requested)

        self.browse_page.connect('remove-requested', self._controller._on_remove_requested)
        self.search_page.connect('remove-requested', self._controller._on_remove_requested)
        self.installed_page.connect('remove-requested', self._controller._on_remove_requested)
        self.tap_page.connect('remove-requested', self._controller._on_remove_requested)
        pages_time = (time.perf_counter() - pages_start) * 1000
        _log.info('Pages wired: %.1f ms', pages_time)

        # Window actions
        actions_start = time.perf_counter()
        refresh_action = Gio.SimpleAction.new('refresh', None)
        refresh_action.connect('activate', self._controller._on_refresh)
        self.add_action(refresh_action)
        self.get_application().set_accels_for_action('win.refresh', ['<Ctrl>r'])

        search_action = Gio.SimpleAction.new('search', None)
        search_action.connect('activate', self._controller._on_search_action)
        self.add_action(search_action)
        self.get_application().set_accels_for_action('win.search', ['<Ctrl>f'])

        open_brewfile_action = Gio.SimpleAction.new('open-brewfile', None)
        open_brewfile_action.connect('activate', self._controller._on_open_brewfile)
        self.add_action(open_brewfile_action)
        maintenance_action = Gio.SimpleAction.new('maintenance', None)
        maintenance_action.connect('activate', self._controller._on_maintenance)
        self.add_action(maintenance_action)
        self.get_application().set_accels_for_action('win.open-brewfile', ['<Ctrl>o'])

        update_tap_action = Gio.SimpleAction.new('update-selected-tap', None)
        update_tap_action.connect('activate', self._controller._on_update_selected_tap)
        self.add_action(update_tap_action)
        actions_time = (time.perf_counter() - actions_start) * 1000
        _log.info('Window actions setup: %.1f ms', actions_time)

        # Settings for size persistence (window owns the settings object;
        # the controller reads it via self._win._settings).
        settings_start = time.perf_counter()
        app_id = self.get_application().get_application_id() if self.get_application() else 'org.tunaos.tavern'
        self._settings = Gio.Settings.new(app_id)
        self.set_default_size(
            self._settings.get_int('window-width'),
            self._settings.get_int('window-height'),
        )
        if self._settings.get_boolean('window-maximized'):
            self.maximize()
        settings_time = (time.perf_counter() - settings_start) * 1000
        _log.info('Settings restored: %.1f ms', settings_time)

        self.connect('close-request', self._on_close)

        # Periodic outdated check honoring the user-configured interval
        interval_h = max(1, self._settings.get_int('outdated-check-interval-hours') or 24)
        GLib.timeout_add_seconds(interval_h * 3600, self._controller._periodic_outdated_check)

        # Start loading
        backend_load_start = time.perf_counter()
        self.backend.connect('formulae-loaded', self._controller._on_formulae_loaded)
        self.backend.connect('casks-loaded', self._controller._on_casks_loaded)
        self.backend.connect('installed-loaded', self._controller._on_installed_loaded)
        self.backend.connect('notify::loading', self._controller._on_backend_loading_changed)
        _log.info('Kicking off backend.load_all_async()')
        self.backend.load_all_async()
        backend_load_time = (time.perf_counter() - backend_load_start) * 1000
        _log.info('Backend.load_all_async() started: %.1f ms', backend_load_time)

        total_init_time = (time.perf_counter() - init_start) * 1000
        _log.info('TavernWindow.__init__: completed in %.1f ms', total_init_time)

    def _on_close(self, *args):
        w, h = self.get_default_size()
        self._settings.set_int('window-width', w)
        self._settings.set_int('window-height', h)
        self._settings.set_boolean('window-maximized', self.is_maximized())

    # ── Compatibility delegates ──────────────────────────────────────
    # The window keeps a thin delegate for every method the test-suite and
    # application still call directly, forwarding to the controller. This
    # preserves the public window API across the refactor.

    def _find_package_by_name(self, package_name):
        return self._controller._find_package_by_name(package_name)

    def open_package_by_name(self, package_name, show_not_found=True):
        return self._controller.open_package_by_name(package_name, show_not_found=show_not_found)

    def _on_task_added(self, mgr, task):
        return self._controller._on_task_added(mgr, task)

    def _on_task_finished(self, mgr, task):
        return self._controller._on_task_finished(mgr, task)

    def _on_active_count_changed(self, mgr, pspec):
        return self._controller._on_active_count_changed(mgr, pspec)

    def _on_task_progress_changed(self, mgr, task):
        return self._controller._on_task_progress_changed(mgr, task)

    def _on_task_button_clicked(self, button):
        return self._controller._on_task_button_clicked(button)

    def _on_formulae_loaded(self, backend, packages):
        return self._controller._on_formulae_loaded(backend, packages)

    def _on_casks_loaded(self, backend, packages):
        return self._controller._on_casks_loaded(backend, packages)

    def _on_installed_loaded(self, backend, _):
        return self._controller._on_installed_loaded(backend, _)

    def _on_outdated_count_changed(self, page, count):
        return self._controller._on_outdated_count_changed(page, count)

    def _on_backend_loading_changed(self, backend, _pspec):
        return self._controller._on_backend_loading_changed(backend, _pspec)

    def _check_deeplink(self):
        return self._controller._check_deeplink()

    def open_tap_by_name(self, tap_name):
        return self._controller.open_tap_by_name(tap_name)

    def _on_package_activated(self, page, package):
        return self._controller._on_package_activated(page, package)

    def _on_package_changed(self, dialog, package):
        return self._controller._on_package_changed(dialog, package)

    def _on_ambiguous_tap_response(self, dialog, response, listbox, pkg):
        return self._controller._on_ambiguous_tap_response(dialog, response, listbox, pkg)

    def _offer_ambiguous_tap_choice(self, task):
        return self._controller._offer_ambiguous_tap_choice(task)

    def _on_conflict_resolution(self, dialog, response, task):
        return self._controller._on_conflict_resolution(dialog, response, task)

    def _on_conflict_uninstall_finished(self, mgr, task):
        return self._controller._on_conflict_uninstall_finished(mgr, task)

    def _on_tap_operation(self, page, message):
        return self._controller._on_tap_operation(page, message)

    def _on_install_requested(self, page, package):
        return self._controller._on_install_requested(page, package)

    def _on_maintenance(self, *_args):
        return self._controller._on_maintenance(*_args)

    def _on_remove_requested(self, page, package):
        return self._controller._on_remove_requested(page, package)

    def _on_package_history_requested(self, card, package):
        return self._controller._on_package_history_requested(card, package)

    def _on_pin_version_requested(self, dialog, version):
        return self._controller._on_pin_version_requested(dialog, version)

    def _on_update_selected_tap(self, action, param):
        return self._controller._on_update_selected_tap(action, param)

    def _on_search_action(self, action, param):
        return self._controller._on_search_action(action, param)

    def _on_refresh(self, action, param):
        return self._controller._on_refresh(action, param)

    def _on_open_brewfile(self, action, param):
        return self._controller._on_open_brewfile(action, param)

    def _on_brewfile_selected(self, dialog, result):
        return self._controller._on_brewfile_selected(dialog, result)

    def open_brewfile(self, path):
        return self._controller.open_brewfile(path)
