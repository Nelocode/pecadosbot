"""Run Python tests with provider access and service startup disabled.

Usage: python scripts/run_offline_tests.py [pytest arguments]
Run from a clean development/CI checkout with its test dependencies installed.
Temporary files and optional Python dependencies follow the normal TEMP/TMP and
PYTHONPATH environment; this runner never installs anything or loads a session.
"""

from contextlib import ExitStack
import errno
import os
from pathlib import Path
import shutil
import socket
import subprocess
import sys
import threading
from unittest.mock import patch


ROOT = Path(__file__).resolve().parents[1]
NODE_GUARD = Path(__file__).resolve().with_name("offline_node_guard.mjs")
NODE_PRELOAD_OPTIONS = f'--import="{NODE_GUARD.as_uri()}"'
WORKER_NAMES = {"wa_bot.mjs", "bot.py", "botfather_bot.py", "app.py"}


def deny_network(*args, **kwargs):
    raise OSError(errno.ENETUNREACH, "Network disabled for offline tests")


def deny_worker(*args, **kwargs):
    raise RuntimeError("Bot/service process creation disabled for offline tests")


def _prepare_environment():
    # Inspect names only, never the contents of real credentials or sessions.
    data_dir = ROOT / "data"
    if data_dir.exists():
        forbidden = [
            item.name for item in data_dir.iterdir()
            if item.name in {".env.local", ".flask_secret", "wa_auth", "wa_switch", "wa_profile_picture_state.json", ".tg_switch_rollback", ".wa_switch_recovery"}
            or item.name.startswith(("tg_session", "tg_switch_session"))
            or ".session" in item.name
            or item.name.endswith(".pid")
        ]
        if forbidden:
            raise SystemExit("Offline tests refused: checkout contains session/configuration material: " + ", ".join(forbidden))
    if not NODE_GUARD.is_file():
        raise SystemExit("Offline tests refused: Node network guard is missing")
    for name in list(os.environ):
        if name.startswith(("TG_", "WA_", "AUTOREPLY_", "BILLING_", "PANEL_ADMIN_")) or name in {"BOT_DIR", "CUSTOM_LANGUAGE_RULES_FILE"}:
            os.environ.pop(name, None)
    # An in-memory fixture prevents Flask from generating a persisted secret.
    os.environ["FLASK_SECRET"] = "offline-test-fixture-not-a-production-secret-" + "0" * 32
    os.environ["PYTEST_DISABLE_PLUGIN_AUTOLOAD"] = "1"
    os.environ["PYTHONDONTWRITEBYTECODE"] = "1"
    os.environ["NODE_OPTIONS"] = NODE_PRELOAD_OPTIONS
    sys.dont_write_bytecode = True
    os.chdir(ROOT)
    sys.path.insert(0, str(ROOT))


def main(arguments=None):
    _prepare_environment()
    real_popen = subprocess.Popen
    real_connect = socket.socket.connect
    real_connect_ex = socket.socket.connect_ex
    real_bind = socket.socket.bind
    real_listen = socket.socket.listen
    real_socketpair = socket.socketpair
    socketpair_scope = threading.local()
    node_executable = shutil.which("node")
    node_path = Path(node_executable).resolve() if node_executable else None

    def guarded_connect(sock, address):
        # Windows creates asyncio's private wake-up pipe with loopback sockets.
        # Only the thread currently inside socketpair may connect that pair.
        if getattr(socketpair_scope, "active", False) and isinstance(address, tuple) and address[0] in ("127.0.0.1", "::1"):
            return real_connect(sock, address)
        return deny_network()

    def guarded_connect_ex(sock, address):
        if getattr(socketpair_scope, "active", False) and isinstance(address, tuple) and address[0] in ("127.0.0.1", "::1"):
            return real_connect_ex(sock, address)
        return deny_network()

    def guarded_bind(sock, address):
        if getattr(socketpair_scope, "active", False) and isinstance(address, tuple) and address[0] in ("127.0.0.1", "::1"):
            return real_bind(sock, address)
        return deny_network()

    def guarded_listen(sock, *args):
        if getattr(socketpair_scope, "active", False):
            return real_listen(sock, *args)
        return deny_network()

    def local_socketpair(*args, **kwargs):
        previous = getattr(socketpair_scope, "active", False)
        socketpair_scope.active = True
        try:
            return real_socketpair(*args, **kwargs)
        finally:
            socketpair_scope.active = previous

    def guarded_popen(args, *positional, **kwargs):
        if kwargs.get("shell") or not isinstance(args, (list, tuple)) or not args:
            return deny_worker()
        executable = shutil.which(str(args[0])) or str(args[0])
        override = kwargs.get("executable")
        if node_path is None or Path(executable).resolve() != node_path:
            return deny_worker()
        if override and Path(shutil.which(str(override)) or str(override)).resolve() != node_path:
            return deny_worker()
        if any(Path(str(arg)).name.lower() in WORKER_NAMES for arg in args[1:]):
            return deny_worker()
        # Tests may replace their whole environment: always restore the guard.
        child_env = dict(kwargs.get("env") or os.environ)
        child_env["NODE_OPTIONS"] = NODE_PRELOAD_OPTIONS
        kwargs["env"] = child_env
        return real_popen(args, *positional, **kwargs)

    with ExitStack() as stack:
        for name, replacement in (
            ("connect", guarded_connect), ("connect_ex", guarded_connect_ex),
            ("bind", guarded_bind), ("listen", guarded_listen), ("sendto", deny_network),
        ):
            stack.enter_context(patch.object(socket.socket, name, replacement))
        stack.enter_context(patch.object(socket, "socketpair", local_socketpair))
        for name in ("getaddrinfo", "gethostbyname", "gethostbyname_ex"):
            stack.enter_context(patch.object(socket, name, deny_network))
        stack.enter_context(patch.object(subprocess, "Popen", guarded_popen))
        stack.enter_context(patch.object(os, "system", deny_worker))
        # Import Flask for its in-process test client without its supervisor.
        with patch("threading.Thread.start"):
            import app
        stack.enter_context(patch.object(app, "_ensure_supervisor_running", lambda: None))
        import pytest

        return pytest.main(["-q", "-p", "no:cacheprovider", *(sys.argv[1:] if arguments is None else arguments)])


if __name__ == "__main__":
    raise SystemExit(main())
