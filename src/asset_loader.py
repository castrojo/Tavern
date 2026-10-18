# asset_loader.py - Package asset request coordinator
# SPDX-License-Identifier: GPL-3.0-or-later

"""Coordinate package-detail asset requests outside the page widget.

The backend owns fetching and caching media.  This controller owns the page
lifecycle boundary: it starts the icon and screenshot requests together and
ignores callbacks from a package that is no longer displayed.
"""


class AssetLoaderController:
    """Start and invalidate icon/screenshot requests for one details page."""

    def __init__(self, backend=None):
        self._backend = backend
        self._generation = 0

    def set_backend(self, backend):
        """Replace the backend used for future requests."""
        self._backend = backend
        self.cancel()

    def load(self, package, icon_callback, screenshot_callback):
        """Fetch the two detail-page assets for *package*.

        The callbacks receive the same ``(package, pixbuf)`` arguments as the
        backend API.  Results from an earlier ``load`` call are discarded so a
        slow request cannot update a later details page.
        """
        self._generation += 1
        generation = self._generation
        backend = self._backend
        if backend is None or package is None:
            return

        def current(callback):
            def deliver(result_package, pixbuf):
                if generation == self._generation and result_package is package:
                    callback(result_package, pixbuf)

            return deliver

        backend.fetch_icon_async(package, current(icon_callback))
        backend.fetch_screenshot_async(package, current(screenshot_callback))

    def cancel(self):
        """Invalidate callbacks from requests already handed to the backend."""
        self._generation += 1
