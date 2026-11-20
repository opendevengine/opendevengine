"""Session-owned ToolBroker subclass and mode-capturing runner proxy.

``SessionBroker`` keeps the producer broker's guard, tool, event and checkpoint logic
unchanged (it calls ``super().call``) and only appends bounded, trusted user updates
to a result that already succeeded and passed the post-call guard. The Unix endpoint
dispatches through ``self.call``, so the MCP bridge and the exact seven tool names are
unchanged. Updates carry user text only; they can never alter grants or scope.
"""
from opendevengine.execution.broker import ToolBroker

MAX_ANNOTATION_CHARS = 8000


class ModeSnapshot(dict):
    """A {path: bytes} snapshot that also carries the runner-reported git modes."""

    def __init__(self, files, modes):
        super().__init__(files)
        self.modes = dict(modes)


class ModeRunner:
    """Proxy giving the broker's snapshot() mode-aware results; all else delegates."""

    def __init__(self, runner):
        self._runner = runner

    def snapshot(self, handle):
        try:
            files, modes = self._runner.snapshot(handle, include_modes=True)
        except TypeError:  # a runner without mode metadata: base modes assumed unchanged
            return ModeSnapshot(self._runner.snapshot(handle), {})
        return ModeSnapshot(files, modes)

    def __getattr__(self, name):
        return getattr(self._runner, name)


class SessionBroker(ToolBroker):
    def __init__(self, runner, handle, *, annotate, **kwargs):
        super().__init__(ModeRunner(runner), handle, **kwargs)
        self._annotate = annotate

    def call(self, name, arguments=None):
        result = super().call(name, arguments)
        if not isinstance(result, dict) or result.get('ok') is not True:
            return result
        try:
            extra = self._annotate(name)
        except Exception:
            extra = None  # an annotation failure never breaks or alters the tool result
        if isinstance(extra, str) and extra:
            result = dict(result, text=result['text'] + extra[:MAX_ANNOTATION_CHARS])
        return result
