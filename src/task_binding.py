# task_binding.py - Task signal lifecycle for package details
# SPDX-License-Identifier: GPL-3.0-or-later

"""Keep GObject task signal connections out of the details-page widget."""


class PackageTaskBinder:
    """Bind one task to progress and completion callbacks.

    A page can be rebound when it is reopened or when a queued task changes.
    The previous task's handlers are disconnected before the new task is
    observed, preventing duplicate progress updates and stale page callbacks.
    """

    def __init__(self, on_progress, on_finished):
        self._on_progress_callback = on_progress
        self._on_finished_callback = on_finished
        self._task = None
        self._progress_handler = None
        self._finished_handler = None

    @property
    def task(self):
        """Return the currently bound task, if any."""
        return self._task

    def bind(self, task):
        """Observe *task*, replacing any previous binding."""
        self.unbind()
        if task is None:
            return

        self._task = task
        self._progress_handler = task.connect('notify::progress', self._on_progress)
        self._finished_handler = task.connect('finished', self._on_finished)

    def unbind(self):
        """Disconnect the current task, if it is still connected."""
        task = self._task
        if task is not None:
            for handler in (self._progress_handler, self._finished_handler):
                if handler is not None:
                    try:
                        task.disconnect(handler)
                    except (TypeError, ValueError):
                        # GObject may already have removed a handler while a
                        # signal was being emitted.
                        pass
        self._task = None
        self._progress_handler = None
        self._finished_handler = None

    def _on_progress(self, task, pspec):
        self._on_progress_callback(task, pspec)

    def _on_finished(self, task, success):
        # Disconnect before forwarding so a callback that immediately binds a
        # replacement task cannot be cleared by cleanup after the callback.
        self.unbind()
        self._on_finished_callback(task, success)
