"""Answering ssh's password and host-key prompts from the GUI.

ssh asks for passwords, passphrases and host-key confirmations through the
program named by ``SSH_ASKPASS``. We point it at a tiny helper script that
forwards the prompt over a private Unix socket to this process, where the
window shows a dialog and sends the answer back. The helper has no imports
from this package, so it runs under any Python.
"""

from __future__ import annotations

import json
import logging
import os
import secrets
import socket
import stat
import sys
import tempfile
import threading
from typing import Callable, Dict, Optional

logger = logging.getLogger(__name__)

# Called on a server thread with (prompt, kind); returns the answer, or None
# to refuse. kind is "secret", "confirm" (yes/no) or "info" (no answer).
PromptHandler = Callable[[str, str], Optional[str]]

_HELPER = """\
import json, os, socket, sys
prompt = sys.argv[1] if len(sys.argv) > 1 else ""
request = {
    "token": os.environ.get("MFATFM_ASKPASS_TOKEN", ""),
    "context": os.environ.get("MFATFM_ASKPASS_CONTEXT", ""),
    "prompt": prompt,
    "hint": os.environ.get("SSH_ASKPASS_PROMPT", ""),
}
try:
    with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as sock:
        sock.connect(os.environ["MFATFM_ASKPASS_SOCKET"])
        sock.sendall(json.dumps(request).encode() + b"\\n")
        reply = json.loads(sock.makefile("rb").readline() or b"{}")
except Exception as exc:
    sys.stderr.write("mfatfm askpass: %s\\n" % exc)
    sys.exit(1)
if not reply.get("ok"):
    sys.exit(1)
sys.stdout.write(reply.get("answer", "") + "\\n")
"""


def prompt_kind(prompt: str, hint: str = "") -> str:
    """How to ask: "confirm" for yes/no questions, "info" for notices."""
    hint = (hint or "").lower()
    if hint == "none":
        return "info"
    if hint == "confirm" or "(yes/no" in prompt:
        return "confirm"
    return "secret"


class AskpassServer:
    """Listens for helper requests and routes each to its connection's handler."""

    def __init__(self) -> None:
        self._dir = tempfile.mkdtemp(prefix="mfatfm-askpass-", dir=os.environ.get("XDG_RUNTIME_DIR"))
        os.chmod(self._dir, stat.S_IRWXU)
        self._socket_path = os.path.join(self._dir, "socket")
        self._helper_path = os.path.join(self._dir, "askpass")
        with open(self._helper_path, "w", encoding="utf-8") as handle:
            handle.write(f"#!{sys.executable}\n{_HELPER}")
        os.chmod(self._helper_path, stat.S_IRWXU)
        self._token = secrets.token_hex(16)
        self._handlers: Dict[str, PromptHandler] = {}
        self._lock = threading.Lock()
        self._listener = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        self._listener.bind(self._socket_path)
        self._listener.listen(8)
        self._closed = False
        threading.Thread(target=self._serve, name="askpass", daemon=True).start()

    def register(self, handler: PromptHandler) -> Dict[str, str]:
        """Return the environment for one ssh process whose prompts go to *handler*."""
        context = secrets.token_hex(8)
        with self._lock:
            self._handlers[context] = handler
        return {
            "SSH_ASKPASS": self._helper_path,
            "SSH_ASKPASS_REQUIRE": "force",
            "MFATFM_ASKPASS_SOCKET": self._socket_path,
            "MFATFM_ASKPASS_TOKEN": self._token,
            "MFATFM_ASKPASS_CONTEXT": context,
        }

    def unregister(self, env: Dict[str, str]) -> None:
        with self._lock:
            self._handlers.pop(env.get("MFATFM_ASKPASS_CONTEXT", ""), None)

    def close(self) -> None:
        self._closed = True
        try:
            self._listener.close()
        except OSError:
            pass
        for path in (self._socket_path, self._helper_path):
            try:
                os.unlink(path)
            except OSError:
                pass
        try:
            os.rmdir(self._dir)
        except OSError:
            pass

    def _serve(self) -> None:
        while not self._closed:
            try:
                conn, _addr = self._listener.accept()
            except OSError:
                return
            threading.Thread(target=self._answer, args=(conn,), daemon=True).start()

    def _answer(self, conn: socket.socket) -> None:
        with conn:
            try:
                request = json.loads(conn.makefile("rb").readline() or b"{}")
            except (OSError, ValueError):
                return
            answer: Optional[str] = None
            if secrets.compare_digest(str(request.get("token", "")), self._token):
                with self._lock:
                    handler = self._handlers.get(str(request.get("context", "")))
                if handler is not None:
                    prompt = str(request.get("prompt", ""))
                    try:
                        answer = handler(prompt, prompt_kind(prompt, str(request.get("hint", ""))))
                    except Exception:
                        logger.exception("Password prompt failed")
            reply = {"ok": answer is not None, "answer": answer or ""}
            try:
                conn.sendall(json.dumps(reply).encode() + b"\n")
            except OSError:
                pass


_default: Optional[AskpassServer] = None
_default_lock = threading.Lock()


def default_server() -> Optional[AskpassServer]:
    """The process-wide server, started on first use (``None`` if it cannot be)."""
    global _default
    with _default_lock:
        if _default is None:
            try:
                _default = AskpassServer()
            except OSError as exc:
                logger.warning("Password prompts are unavailable: %s", exc)
                return None
            import atexit

            atexit.register(_default.close)
        return _default
