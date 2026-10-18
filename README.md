# mfatfm

A two-pane file manager for GNOME, written in Python with GTK 4 and
libadwaita. It manages the files on your computer, and either pane can also
browse an SFTP server.

mfatfm started as the file manager built into
[sshPilot](https://github.com/mfat/sshpilot) and now runs on its own. SFTP
goes through [mfatsftp](https://github.com/mfat/mfatsftp), which uses your
system's OpenSSH. Your `~/.ssh/config`, keys, agent, `ProxyJump` and
certificates all work as they do in a terminal.

## Features

- Two panes side by side (F3 switches to one pane). Each pane shows this
  computer or a server.
- List and grid views with Nautilus-style zoom, sorting, type-ahead,
  rubber-band selection and keyboard shortcuts.
- Copy and move in any direction: within your computer, to or from a server,
  within one server (the copy happens on the server), or between two
  servers. Drag and drop between the panes, F6 / Shift+F6, or copy and
  paste.
- Copies and deletions run in the background. A header-bar popover shows
  progress, speed and time left, and can cancel them. Cancelling removes
  any partly copied file.
- Asks before replacing existing items (replace, skip existing, or cancel).
- Delete moves local items to the Trash. Shift+Delete deletes permanently,
  after asking.
- Local folders update by themselves when files change on disk.
- A remote file opens in your usual application. Each time you save it, it
  is uploaded back to the server.
- Works with other apps: drag files in and out, and copy and paste with
  Nautilus.
- Properties dialog with folder sizes, free space, owner, and permissions
  you can edit (locally and over SFTP).
- Password, passphrase and host-key prompts appear as dialogs.

## Requirements

- Python 3.10+
- GTK 4.12+ and libadwaita 1.5+ with PyGObject (on Debian and Ubuntu:
  `python3-gi gir1.2-gtk-4.0 gir1.2-adw-1`)
- OpenSSH's `ssh` client, for SFTP

## Installing

```sh
python3 -m venv --system-site-packages .venv
.venv/bin/pip install .
.venv/bin/mfatfm
```

`--system-site-packages` lets the virtual environment use your
distribution's PyGObject.

## Usage

```sh
mfatfm                                  # your home folder in both panes
mfatfm ~/Downloads                      # left pane on ~/Downloads
mfatfm ~/site me@example.com:/var/www   # left local, right on a server
mfatfm . sftp://example.com:2222/srv    # a URI with a port
```

You can also type a location such as `sftp://host/path`, `user@host:path`
or a `~/.ssh/config` alias into a pane's path bar. Or use **Connect to
Server** (Ctrl+K), or a pane's location menu, which also lists your folders,
GTK bookmarks and recent servers.

## Keyboard shortcuts

| Action | Keys |
|---|---|
| Connect to server | Ctrl+K |
| Copy / move to the other pane | F6 / Shift+F6 |
| Switch pane | F7 |
| One or two panes | F3 |
| Swap panes / same folder in both | Ctrl+U / Ctrl+Shift+U |
| Location bar | Ctrl+L |
| Back, forward, parent, home | Alt+Left, Alt+Right, Alt+Up (Backspace), Alt+Home |
| Reload | F5, Ctrl+R |
| Hidden files | Ctrl+H |
| List / grid view | Ctrl+1 / Ctrl+2 |
| Zoom | Ctrl+plus, Ctrl+minus, Ctrl+0 |
| New folder, rename, properties | Ctrl+Shift+N, F2, Ctrl+I |
| Trash / delete permanently | Delete / Shift+Delete |
| Select matching, invert selection | Ctrl+S, Ctrl+Shift+I |

## Embedding in another application

`FileManagerView` is a plain `Gtk.Widget` holding both panes. The
standalone window is just a header bar around one, so any GTK 4 /
libadwaita application can embed it the same way:

```python
from mfatfm import FileManagerView

view = FileManagerView(left="~", right="sftp://me@example.com/srv")
window.insert_action_group("fm", view.actions)   # fm.connect, fm.dual-pane, …
header_bar.pack_end(view.operations_button)      # progress of copies
view.connect("title-changed", lambda v, title, subtitle: ...)
toolbar_view.set_content(view)
# when the window closes:
view.shutdown()
```

The host can supply the SFTP side itself:

- **Lend an existing connection.** Wrap the host's own
  `mfatsftp.SFTPClient` with `SftpBackend.from_client(target, client)` and
  pass it to `view.add_connection(...)`. Panes opening that server use it
  instead of starting ssh. The view never closes it, even in `shutdown()`;
  `view.remove_connection(...)` hands it back.
- **Decide how new connections start.** Pass
  `connection_factory=lambda target: SftpBackend(target, connector=...)`.
  The connector returns a connected client, e.g. from
  `mfatsftp.from_command(argv, env=env)` with the host's own ssh options or
  ControlMaster. `SftpBackend(target, ssh_options=[...])` covers the simple
  case.
- **Answer ssh's questions in the host's UI.** Pass
  `prompt_handler(backend, prompt, kind)`; it runs on a worker thread and
  returns the password, `"yes"`/`"no"`, or `None` to refuse. By default the
  view shows its own dialogs.
- **Use the backends directly.** `SftpBackend` and `LocalBackend` work
  without GTK (`list_dir`, `stat`, `upload`, `download`, `copy_file`, …).
  `view.get_connection(target)` returns the connection a pane is using.
- **Keep settings elsewhere.** Call `mfatfm.use_settings(Settings(path))`,
  or `Settings(persist=False)`, before creating views.

`launch_file_manager_window(application, left, right)` opens a complete
window instead. [examples/embed.py](examples/embed.py) is a working host
that lends its connection and starts other servers its own way.

## Development

```sh
python3 -m venv --system-site-packages .venv
.venv/bin/pip install -e '.[test]'
.venv/bin/python -m pytest
```

The SFTP tests need a server that accepts your key without prompting:

```sh
MFATFM_TEST_SFTP_HOST=localhost .venv/bin/python -m pytest
```

Code layout (`src/mfatfm`):

| Module | Role |
|---|---|
| `app.py` | The application and command line |
| `view.py` | `FileManagerView`: panes, connections, clipboard, operations (embeddable) |
| `window.py` | `FileManagerWindow`: the app's header bar around a view |
| `pane.py`, `pane_controls.py` | One pane: views, selection, drag and drop, toolbar |
| `backends/` | `LocalBackend` and `SftpBackend`, which share one interface |
| `transfer.py`, `progress.py` | Copy, move and delete between backends, with progress |
| `operations.py` | The header-bar list of running operations |
| `askpass.py` | Routes ssh's prompts to dialogs |
| `properties_dialog.py`, `permissions.py` | Properties and permissions |
| `locations.py` | Parsing locations, ssh config hosts, places and bookmarks |

## License

GPL-3.0-or-later. See [LICENSE](LICENSE).
