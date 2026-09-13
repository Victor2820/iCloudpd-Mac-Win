"""Remember the Apple ID and download directory of the last successful run.

Without this, the guided setup asks for the same two values on every start. The
module keeps the strict minimum needed to pre-fill those two prompts, following
the principle of least privilege:

* only the Apple ID and the download directory are ever stored. The password,
  the session cookies and the MFA trust token are never written here -- those
  stay in the ``~/.pyicloud`` cookie store that upstream already uses;
* the file lives in the user's own config directory rather than next to the
  binary, so copying the binary around does not carry the settings with it;
* the directory is created ``0700`` and the file is created (and re-chmodded)
  ``0600``, so only its owner can read it;
* nothing is written unless a run finished cleanly, so a failed or preview run
  cannot poison the defaults;
* every failure is swallowed: remembering is a convenience, never a reason for a
  run to fail.

``--no-remember`` switches the whole mechanism off.
"""

import json
import os
import pathlib
from dataclasses import dataclass

_APP_DIR = "icloudpd"
_FILE_NAME = "remember.json"

_KEY_USERNAME = "username"
_KEY_DIRECTORY = "directory"


@dataclass
class Remembered:
    """The values kept from the last successful run."""

    username: str | None = None
    directory: str | None = None


def memory_path() -> pathlib.Path:
    """Location of the memory file, honouring ``XDG_CONFIG_HOME``."""
    root = os.environ.get("XDG_CONFIG_HOME")
    base = pathlib.Path(root).expanduser() if root else pathlib.Path.home() / ".config"
    return base / _APP_DIR / _FILE_NAME


def load() -> Remembered | None:
    """Read the remembered values, or ``None`` when there are none."""
    try:
        raw = memory_path().read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError):
        return None
    try:
        data = json.loads(raw)
    except ValueError:
        return None
    if not isinstance(data, dict):
        return None

    username = data.get(_KEY_USERNAME)
    directory = data.get(_KEY_DIRECTORY)
    remembered = Remembered(
        username=username if isinstance(username, str) and username else None,
        directory=directory if isinstance(directory, str) and directory else None,
    )
    if remembered.username is None and remembered.directory is None:
        return None
    return remembered


def save(username: str | None, directory: str | None) -> None:
    """Store the two values, readable by their owner only. Never raises."""
    payload: dict[str, str] = {}
    if username:
        payload[_KEY_USERNAME] = username.strip().lower()
    if directory:
        payload[_KEY_DIRECTORY] = str(pathlib.Path(directory).expanduser())
    if not payload:
        return

    path = memory_path()
    try:
        path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        os.chmod(path.parent, 0o700)
        # Passing the mode to O_CREAT leaves no window in which a lax umask could
        # make the file readable by other users.
        descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
            json.dump(payload, handle, ensure_ascii=False, indent=2)
            handle.write("\n")
        # O_CREAT only applies to a fresh file, so an existing one is tightened too
        os.chmod(path, 0o600)
    except OSError:
        # least privilege is about not leaking data, not about failing the run
        return
