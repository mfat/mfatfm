for entry in selected:
                if self._is_remote:
                    continue  # Can't upload from remote pane
                path = pathlib.Path(self._current_path) / entry.name
                paths.append(path)
                
            if paths:
                payload = {"paths": paths, "destination": destination}
                self.emit("request-operation", "upload", payload)
                
    def _on_download_clicked(self, button: Gtk.Button) -> None:
        """Handle download operation."""
        selected = self._get_selected_entries()
        if not selected:
            self._show_toast("Select items to download")
            return
            
        if self._partner_pane:
            destination = pathlib.Path(self._partner_pane.get_current_path())
            payload = {
                "entries": selected,
                "directory": self._current_path,
                "destination": destination
            }
            self.emit("request-operation", "download", payload)
            
    # Drag and drop handlers
    def _on_drop_accept(self, target: Gtk.DropTarget, drop: Gdk.Drop) -> bool:
        """Check if drop is acceptable."""
        formats = drop.get_formats()
        return (formats.contain_gtype(Gio.File) or 
                formats.contain_mime_type("text/uri-list") or
                formats.contain_gtype(GObject.TYPE_STRING))
        
    def _on_drop_enter(self, target: Gtk.DropTarget, x: float, y: float) -> Gdk.DragAction:
        """Handle drop enter."""
        self._show_drop_zone()
        return Gdk.DragAction.COPY
        
    def _on_drop_leave(self, target: Gtk.DropTarget) -> None:
        """Handle drop leave."""
        self._hide_drop_zone()
        
    def _on_drop(self, target: Gtk.DropTarget, value, x: float, y: float) -> bool:
        """Handle drop operation."""
        self._hide_drop_zone()
        
        window = self.get_root()
        if not isinstance(window, FileManagerWindow):
            return False
            
        # Get the drag source from the window
        drag_source = getattr(window, '_active_drag_source', None)
        
        # Handle inter-pane drag and drop
        if drag_source and drag_source != self:
            if drag_source._is_remote and not self._is_remote:
                # Remote to local - download
                selected = drag_source._get_selected_entries()
                if selected:
                    payload = {
                        "entries": selected,
                        "directory": drag_source.get_current_path(),
                        "destination": pathlib.Path(self.get_current_path())
                    }
                    self.emit("request-operation", "download", payload)
                    return True
            elif not drag_source._is_remote and self._is_remote:
                # Local to remote - upload
                selected = drag_source._get_selected_entries()
                if selected:
                    paths = []
                    for entry in selected:
                        path = pathlib.Path(drag_source.get_current_path()) / entry.name
                        paths.append(path)
                    payload = {"paths": paths, "destination": self.get_current_path()}
                    self.emit("request-operation", "upload", payload)
                    return True
        
        # Handle external file drops
        if isinstance(value, Gio.File):
            self.emit("request-operation", "upload", value)
            return True
        elif isinstance(value, str):
            # Handle URI list or plain text
            if value.startswith("file://"):
                try:
                    file = Gio.File.new_for_uri(value.strip())
                    self.emit("request-operation", "upload", file)
                    return True
                except Exception as e:
                    logger.error(f"Error handling dropped URI: {e}")
                    
        return False
        
    def _on_drag_prepare(self, source: Gtk.DragSource, x: float, y: float):
        """Prepare drag operation."""
        selected = self._get_selected_entries()
        if not selected:
            return None
            
        # Store drag source in window
        window = self.get_root()
        if isinstance(window, FileManagerWindow):
            window._active_drag_source = self
            
        # Create appropriate content provider
        if self._is_remote:
            # For remote files, provide text content
            content = "\n".join(entry.name for entry in selected)
            value = GObject.Value()
            value.init(GObject.TYPE_STRING)
            value.set_string(content)
            return Gdk.ContentProvider.new_for_value(value)
        else:
            # For local files, provide file URIs
            uris = []
            for entry in selected:
                path = os.path.join(self._current_path, entry.name)
                if os.path.exists(path):
                    file = Gio.File.new_for_path(path)
                    uri = file.get_uri()
                    if uri:
                        uris.append(uri)
                        
            if uris:
                uri_list = "\r\n".join(uris) + "\r\n"
                data = GLib.Bytes.new(uri_list.encode("utf-8"))
                return Gdk.ContentProvider.new_for_bytes("text/uri-list", data)
                
        return None
        
    def _on_drag_begin(self, source: Gtk.DragSource, drag: Gdk.Drag) -> None:
        """Handle drag begin."""
        if self._partner_pane:
            self._partner_pane._show_drop_zone()
            
    def _on_drag_end(self, source: Gtk.DragSource, drag: Gdk.Drag, delete: bool) -> None:
        """Handle drag end."""
        if self._partner_pane:
            self._partner_pane._hide_drop_zone()
            
        # Clear drag source from window
        window = self.get_root()
        if isinstance(window, FileManagerWindow):
            window._active_drag_source = None
            
    # Utility methods
    def _show_drop_zone(self) -> None:
        """Show the drop zone."""
        if hasattr(self, '_drop_revealer'):
            self._drop_revealer.set_reveal_child(True)
            
    def _hide_drop_zone(self) -> None:
        """Hide the drop zone."""
        if hasattr(self, '_drop_revealer'):
            self._drop_revealer.set_reveal_child(False)
            
    def _get_selected_entries(self) -> List[FileEntry]:
        """Get currently selected entries."""
        selected = []
        for i in range(len(self._filtered_entries)):
            if self._selection_model.is_selected(i):
                selected.append(self._filtered_entries[i])
        return selected
        
    def _show_toast(self, message: str) -> None:
        """Show a toast message."""
        toast = Adw.Toast.new(message)
        self._toast_overlay.add_toast(toast)
        
    def _format_size(self, size_bytes: int) -> str:
        """Format file size for display."""
        if size_bytes < 0:
            return "—"
        if size_bytes == 0:
            return "0 B"
        if size_bytes < 1024:
            return f"{size_bytes} B"
            
        units = ["KB", "MB", "GB", "TB", "PB"]
        value = float(size_bytes)
        
        for unit in units:
            value /= 1024.0
            if value < 1024.0:
                if value < 10:
                    return f"{value:.1f} {unit}"
                else:
                    return f"{value:.0f} {unit}"
                    
        return f"{value:.1f} EB"
        
    def _sort_entries(self, entries: List[FileEntry]) -> List[FileEntry]:
        """Sort entries based on current sort criteria."""
        def sort_key(entry: FileEntry):
            if self._sort_key == SortKey.SIZE:
                return entry.size
            elif self._sort_key == SortKey.MODIFIED:
                return entry.modified
            else:  # NAME
                return entry.name.lower()
                
        # Separate directories and files
        dirs = [e for e in entries if e.is_dir]
        files = [e for e in entries if not e.is_dir]
        
        # Sort each group
        dirs.sort(key=sort_key, reverse=self._sort_descending)
        files.sort(key=sort_key, reverse=self._sort_descending)
        
        # Directories first, then files
        return dirs + files
        
    def _apply_filters(self, entries: List[FileEntry]) -> List[FileEntry]:
        """Apply filtering (hidden files, etc.)."""
        filtered = []
        for entry in entries:
            # Skip hidden files if not showing them
            if not self._show_hidden and entry.name.startswith('.'):
                continue
            filtered.append(entry)
        return filtered
        
    def _refresh_view(self) -> None:
        """Refresh the view with current entries."""
        if self._update_pending:
            return
            
        self._update_pending = True
        GLib.idle_add(self._do_refresh_view)
        
    def _do_refresh_view(self) -> bool:
        """Perform the actual view refresh."""
        try:
            # Apply filtering and sorting
            filtered = self._apply_filters(self._entries)
            self._filtered_entries = self._sort_entries(filtered)
            
            # Update list store
            self._list_store.remove_all()
            for entry in self._filtered_entries:
                self._list_store.append(entry)
                
            # Update action button states
            self._update_action_buttons()
            
        except Exception as e:
            logger.error(f"Error refreshing view: {e}")
        finally:
            self._update_pending = False
            
        return False
        
    def _update_action_buttons(self) -> None:
        """Update action button sensitivity."""
        selected = self._get_selected_entries()
        has_selection = len(selected) > 0
        single_selection = len(selected) == 1
        
        # Update button states
        if 'rename' in self._action_buttons:
            self._action_buttons['rename'].set_sensitive(single_selection)
        if 'delete' in self._action_buttons:
            self._action_buttons['delete'].set_sensitive(has_selection)
        if 'download' in self._action_buttons:
            self._action_buttons['download'].set_sensitive(has_selection)
        if 'upload' in self._action_buttons:
            self._action_buttons['upload'].set_sensitive(has_selection)
            
    # Public interface
    def set_partner_pane(self, partner: Optional['FilePane']) -> None:
        """Set the partner pane for drag/drop operations."""
        self._partner_pane = partner
        
    def show_entries(self, path: str, entries: List[FileEntry]) -> None:
        """Show entries in the pane."""
        self._current_path = path
        self._path_entry.set_text(path)
        self._entries = entries.copy()
        self._refresh_view()
        
        logger.debug(f"Showing {len(entries)} entries in {self._label} pane: {path}")
        
    def get_current_path(self) -> str:
        """Get the current path."""
        return self._current_path
        
    def set_show_hidden(self, show: bool) -> None:
        """Set whether to show hidden files."""
        if self._show_hidden != show:
            self._show_hidden = show
            self._refresh_view()


class FileManagerWindow(Adw.Window):
    """Production-ready SFTP file manager window."""
    
    def __init__(self, application: Adw.Application, *,
                 host: str, username: str, port: int = 22,
                 initial_path: str = "~") -> None:
        super().__init__()
        
        self.set_title(f"SFTP File Manager - {username}@{host}")
        self.set_default_size(1200, 800)
        self.set_size_request(800, 600)
        
        # Connection parameters
        self._host = host
        self._username = username
        self._port = port
        self._initial_path = initial_path
        
        # State
        self._sftp_manager: Optional[AsyncSFTPManager] = None
        self._connection_established = False
        self._current_operations: Dict[str, Future] = {}
        self._active_drag_source: Optional[FilePane] = None
        self._progress_dialogs: List[SFTPProgressDialog] = []
        
        self._build_ui()
        self._setup_actions()
        self._initialize_connection()
        
        logger.info(f"File manager window initialized for {username}@{host}:{port}")
        
    def _build_ui(self) -> None:
        """Build the main window UI."""
        # Main layout
        toolbar_view = Adw.ToolbarView()
        self.set_content(toolbar_view)
        
        # Header bar
        header = Adw.HeaderBar()
        header.set_title_widget(Gtk.Label(label=f"{self._username}@{self._host}"))
        
        # Header controls
        self._setup_header_controls(header)
        toolbar_view.add_top_bar(header)
        
        # Toast overlay for notifications
        self._toast_overlay = Adw.ToastOverlay()
        toolbar_view.set_content(self._toast_overlay)
        
        # Main content - paned view
        paned = Gtk.Paned.new(Gtk.Orientation.HORIZONTAL)
        paned.set_wide_handle(True)
        paned.set_position(600)  # Initial split position
        paned.set_resize_start_child(True)
        paned.set_resize_end_child(True)
        paned.set_shrink_start_child(False)
        paned.set_shrink_end_child(False)
        
        # Create file panes
        self._local_pane = FilePane("Local", is_remote=False)
        self._remote_pane = FilePane("Remote", is_remote=True)
        
        # Set up partner relationship
        self._local_pane.set_partner_pane(self._remote_pane)
        self._remote_pane.set_partner_pane(self._local_pane)
        
        paned.set_start_child(self._local_pane)
        paned.set_end_child(self._remote_pane)
        self._toast_overlay.set_child(paned)
        
        # Connect pane signals
        self._local_pane.connect("path-changed", self._on_local_path_changed)
        self._local_pane.connect("request-operation", self._on_request_operation)
        self._remote_pane.connect("path-changed", self._on_remote_path_changed)
        self._remote_pane.connect("request-operation", self._on_request_operation)
        
        # Initialize local pane
        self._load_local_directory(os.path.expanduser("~"))
        
    def _setup_header_controls(self, header: Adw.HeaderBar) -> None:
        """Setup header bar controls."""
        # Connection status
        self._connection_status = Gtk.Label()
        self._connection_status.set_text("Connecting...")
        self._connection_status.add_css_class("dim-label")
        header.pack_start(self._connection_status)
        
        # Menu button
        menu_button = Gtk.MenuButton()
        menu_button.set_icon_name("open-menu-symbolic")
        
        # Create menu
        menu = Gio.Menu()
        menu.append("Show Hidden Files", "win.show-hidden")
        menu.append("Refresh All", "win.refresh-all")
        menu.append_separator()
        menu.append("Disconnect", "win.disconnect")
        
        menu_button.set_menu_model(menu)
        header.pack_end(menu_button)
        
    def _setup_actions(self) -> None:
        """Setup window actions."""
        action_group = Gio.SimpleActionGroup()
        self.insert_action_group("win", action_group)
        
        # Show hidden files action
        show_hidden_action = Gio.SimpleAction.new_stateful(
            "show-hidden", None, GLib.Variant.new_boolean(False)
        )
        show_hidden_action.connect("activate", self._on_show_hidden_action)
        action_group.add_action(show_hidden_action)
        
        # Refresh all action
        refresh_action = Gio.SimpleAction.new("refresh-all", None)
        refresh_action.connect("activate", self._on_refresh_all_action)
        action_group.add_action(refresh_action)
        
        # Disconnect action
        disconnect_action = Gio.SimpleAction.new("disconnect", None)
        disconnect_action.connect("activate", self._on_disconnect_action)
        action_group.add_action(disconnect_action)
        
    def _initialize_connection(self) -> None:
        """Initialize SFTP connection."""
        self._sftp_manager = AsyncSFTPManager(self._host, self._username, self._port)
        
        # Connect signals
        self._sftp_manager.connect("connected", self._on_sftp_connected)
        self._sftp_manager.connect("connection-error", self._on_sftp_connection_error)
        self._sftp_manager.connect("directory-loaded", self._on_sftp_directory_loaded)
        self._sftp_manager.connect("operation-error", self._on_sftp_operation_error)
        
        # Start connection
        self._sftp_manager.connect_to_server()
        
    # SFTP event handlers
    def _on_sftp_connected(self, manager) -> None:
        """Handle successful SFTP connection."""
        self._connection_established = True
        self._connection_status.set_text("Connected")
        self._show_toast("Connected to server")
        
        # Load initial remote directory
        self._sftp_manager.listdir(self._initial_path)
        
    def _on_sftp_connection_error(self, manager, error: str) -> None:
        """Handle SFTP connection error."""
        self._connection_status.set_text("Connection failed")
        self._show_error_toast(f"Connection failed: {error}")
        
    def _on_sftp_directory_loaded(self, manager, path: str, entries: List[FileEntry]) -> None:
        """Handle remote directory loaded."""
        self._remote_pane.show_entries(path, entries)
        
    def _on_sftp_operation_error(self, manager, error: str) -> None:
        """Handle SFTP operation error."""
        self._show_error_toast(f"Operation failed: {error}")
        
    # Path change handlers
    def _on_local_path_changed(self, pane, path: str) -> None:
        """Handle local path change."""
        self._load_local_directory(path)
        
    def _on_remote_path_changed(self, pane, path: str) -> None:
        """Handle remote path change."""
        if self._sftp_manager and self._connection_established:
            self._sftp_manager.listdir(path)
        else:
            self._show_error_toast("Not connected to server")
            
    def _load_local_directory(self, path: str) -> None:
        """Load local directory contents."""
        try:
            expanded_path = os.path.expanduser(path)
            if not os.path.isabs(expanded_path):
                expanded_path = os.path.abspath(expanded_path)
                
            if not os.path.isdir(expanded_path):
                raise NotADirectoryError(f"Not a directory: {expanded_path}")
                
            entries = []
            try:
                with os.scandir(expanded_path) as it:
                    for dirent in it:
                        try:
                            stat = dirent.stat(follow_symlinks=False)
                            entry = FileEntry(
                                name=dirent.name,
                                is_dir=dirent.is_dir(follow_symlinks=False),
                                size=getattr(stat, 'st_size', 0) or 0,
                                modified=getattr(stat, 'st_mtime', 0.0) or 0.0
                            )
                            entries.append(entry)
                        except (OSError, ValueError) as e:
                            logger.warning(f"Skipping {dirent.name}: {e}")
                            continue
                            
            except PermissionError:
                self._show_error_toast(f"Permission denied: {expanded_path}")
                return
                
            self._local_pane.show_entries(expanded_path, entries)
            
        except Exception as e:
            logger.error(f"Error loading local directory {path}: {e}")
            self._show_error_toast(f"Cannot load directory: {e}")
            
    # File operation handlers
    def _on_request_operation(self, pane, operation: str, payload) -> None:
        """Handle file operation requests."""
        logger.info(f"Handling operation: {operation} from {'remote' if pane._is_remote else 'local'} pane")
        
        try:
            if operation == "mkdir":
                self._handle_mkdir_operation(pane)
            elif operation == "rename":
                self._handle_rename_operation(pane, payload)
            elif operation == "delete":
                self._handle_delete_operation(pane, payload)
            elif operation == "upload":
                self._handle_upload_operation(pane, payload)
            elif operation == "download":
                self._handle_download_operation(pane, payload)
            else:
                logger.warning(f"Unknown operation: {operation}")
        except Exception as e:
            logger.error(f"Error handling operation {operation}: {e}")
            self._show_error_toast(f"Operation failed: {e}")
            
    def _handle_mkdir_operation(self, pane: FilePane) -> None:
        """Handle create directory operation."""
        dialog = Adw.MessageDialog.new(self, "Create Folder", "Enter folder name:")
        dialog.add_response("cancel", "Cancel")
        dialog.add_response("create", "Create")
        dialog.set_response_appearance("create", Adw.ResponseAppearance.SUGGESTED)
        
        entry = Gtk.Entry()
        entry.set_placeholder_text("New folder")
        dialog.set_extra_child(entry)
        
        def on_response(dialog, response):
            if response == "create":
                name = entry.get_text().strip()
                if name:
                    if pane._is_remote:
                        path = posixpath.join(pane.get_current_path(), name)
                        if self._sftp_manager:
                            future = self._sftp_manager.mkdir(path)
                            self._current_operations[f"mkdir_{time.time()}"] = future
                    else:
                        path = os.path.join(pane.get_current_path(), name)
                        try:
                            os.makedirs(path, exist_ok=False)
                            self._load_local_directory(pane.get_current_path())
                        except FileExistsError:
                            self._show_error_toast("Folder already exists")
                        except Exception as e:
                            self._show_error_toast(f"Cannot create folder: {e}")
            dialog.close()
            
        dialog.connect("response", on_response)
        dialog.present()
        
    def _handle_rename_operation(self, pane: FilePane, payload) -> None:
        """Handle rename operation."""
        if not isinstance(payload, dict) or "entries" not in payload:
            return
            
        entries = payload["entries"]
        if len(entries) != 1:
            return
            
        entry = entries[0]
        current_path = payload.get("directory", pane.get_current_path())
        
        dialog = Adw.MessageDialog.new(self, "Rename", f"Rename '{entry.name}' to:")
        dialog.add_response("cancel", "Cancel")
        dialog.add_response("rename", "Rename")
        dialog.set_response_appearance("rename", Adw.ResponseAppearance.SUGGESTED)
        
        name_entry = Gtk.Entry()
        name_entry.set_text(entry.name)
        dialog.set_extra_child(name_entry)
        
        def on_response(dialog, response):
            if response == "rename":
                new_name = name_entry.get_text().strip()
                if new_name and new_name != entry.name:
                    if pane._is_remote:
                        source = posixpath.join(current_path, entry.name)
                        target = posixpath.join(current_path, new_name)
                        if self._sftp_manager:
                            future = self._sftp_manager.rename(source, target)
                            self._current_operations[f"rename_{time.time()}"] = future
                    else:
                        source = os.path.join(current_path, entry.name)
                        target = os.path.join(current_path, new_name)
                        try:
                            os.rename(source, target)
                            self._load_local_directory(current_path)
                        except Exception as e:
                            self._show_error_toast(f"Cannot rename: {e}")
            dialog.close()
            
        dialog.connect("response", on_response)
        dialog.present()
        
    def _handle_delete_operation(self, pane: FilePane, payload) -> None:
        """Handle delete operation."""
        if not isinstance(payload, dict) or "entries" not in payload:
            return
            
        entries = payload["entries"]
        if not entries:
            return
            
        count = len(entries)
        if count == 1:
            message = f"Delete '{entries[0].name}'?"
        else:
            message = f"Delete {count} items?"
            
        dialog = Adw.MessageDialog.new(self, "Delete Items", message)
        dialog.add_response("cancel", "Cancel")
        dialog.add_response("delete", "Delete")
        dialog.set_response_appearance("delete", Adw.ResponseAppearance.DESTRUCTIVE)
        
        def on_response(dialog, response):
            if response == "delete":
                current_path = payload.get("directory", pane.get_current_path())
                
                if pane._is_remote:
                    # Delete remote items
                    for entry in entries:
                        item_path = posixpath.join(current_path, entry.name)
                        if self._sftp_manager:
                            future = self._sftp_manager.remove(item_path)
                            self._current_operations[f"delete_{time.time()}"] = future
                else:
                    # Delete local items
                    for entry in entries:
                        item_path = os.path.join(current_path, entry.name)
                        try:
                            if entry.is_dir:
                                shutil.rmtree(item_path)
                            else:
                                os.remove(item_path)
                        except Exception as e:
                            self._show_error_toast(f"Cannot delete {entry.name}: {e}")
                    self._load_local_directory(current_path)
                    
            dialog.close()
            
        dialog.connect("response", on_response)
        dialog.present()
        
    def _handle_upload_operation(self, pane: FilePane, payload) -> None:
        """Handle upload operation with progress dialog."""
        try:
            # Handle different payload types
            paths = []
            destination = "/"
            
            if isinstance(payload, dict):
                if "paths" in payload:
                    paths = payload["paths"]
                elif "entries" in payload:
                    # Convert entries to paths
                    base_path = payload.get("directory", pane.get_current_path())
                    for entry in payload["entries"]:
                        path = pathlib.Path(base_path) / entry.name
                        paths.append(path)
                destination = payload.get("destination", "/")
            elif isinstance(payload, (list, tuple)):
                paths = list(payload)
            elif isinstance(payload, (pathlib.Path, Gio.File)):
                if isinstance(payload, Gio.File):
                    local_path = payload.get_path()
                    if local_path:
                        paths = [pathlib.Path(local_path)]
                else:
                    paths = [payload]
                    
            if not paths:
                self._show_error_toast("No files to upload")
                return
                
            # Filter existing paths
            valid_paths = [p for p in paths if isinstance(p, pathlib.Path) and p.exists()]
            if not valid_paths:
                self._show_error_toast("Selected files do not exist")
                return
                
            # Check for conflicts and proceed with upload
            self._check_upload_conflicts(valid_paths, destination)
                    
        except Exception as e:
            logger.error(f"Upload operation error: {e}")
            self._show_error_toast(f"Upload failed: {e}")
            
    def _check_upload_conflicts(self, paths: List[pathlib.Path], destination: str) -> None:
        """Check for upload conflicts and handle resolution."""
        if not self._sftp_manager:
            self._show_error_toast("Not connected to server")
            return
            
        # For simplicity, we'll proceed without conflict checking for remote files
        # In a full implementation, you would check if remote files exist
        self._proceed_with_upload(paths, destination)
        
    def _proceed_with_upload(self, paths: List[pathlib.Path], destination: str) -> None:
        """Proceed with upload after conflict resolution."""
        if not paths:
            return
            
        # Show progress dialog
        progress_dialog = SFTPProgressDialog(parent=self, operation_type="upload")
        progress_dialog.present()
        self._progress_dialogs.append(progress_dialog)
        
        # Upload files sequentially
        self._upload_files_sequentially(paths, destination, progress_dialog, 0)
        
    def _upload_files_sequentially(self, paths: List[pathlib.Path], destination: str, 
                                 progress_dialog: SFTPProgressDialog, index: int) -> None:
        """Upload files one by one to avoid overwhelming the connection."""
        if index >= len(paths) or progress_dialog.is_cancelled:
            if not progress_dialog.is_cancelled:
                progress_dialog.show_completion(True)
                # Refresh remote directory
                if self._sftp_manager:
                    self._sftp_manager.listdir(destination)
            return
            
        path = paths[index]
        remote_path = posixpath.join(destination, path.name)
        
        overall_progress = index / len(paths)
        
        def progress_callback(fraction: float, message: str, speed: float) -> None:
            if not progress_dialog.is_cancelled:
                # Combine file progress with overall progress
                combined_fraction = (index + fraction) / len(paths)
                file_info = f"Uploading {path.name} ({index + 1}/{len(paths)})"
                progress_dialog.update_progress(combined_fraction, file_info, path.name, speed)
        
        if self._sftp_manager:
            future = self._sftp_manager.upload(path, remote_path, progress_callback)
            progress_dialog.set_future(future)
            
            def on_complete(fut):
                try:
                    fut.result()
                    # Continue with next file
                    GLib.idle_add(self._upload_files_sequentially, paths, destination, 
                                progress_dialog, index + 1)
                except Exception as e:
                    error_msg = str(e)
                    GLib.idle_add(lambda: progress_dialog.show_completion(False, error_msg))
                    
            future.add_done_callback(on_complete)
            self._current_operations[f"upload_{index}_{time.time()}"] = future
            
    def _handle_download_operation(self, pane: FilePane, payload) -> None:
        """Handle download operation with progress dialog."""
        try:
            if not isinstance(payload, dict) or "entries" not in payload:
                return
                
            entries = payload["entries"]
            source_dir = payload.get("directory", "/")
            destination_base = payload.get("destination")
            
            if not entries or not destination_base:
                self._show_error_toast("Invalid download parameters")
                return
                
            if not isinstance(destination_base, pathlib.Path):
                destination_base = pathlib.Path(destination_base)
                
            # Show progress dialog
    def _handle_download_operation(self, pane: FilePane, payload) -> None:
        """Handle download operation with progress dialog."""
        try:
            if not isinstance(payload, dict) or "entries" not in payload:
                return
                
            entries = payload["entries"]
            source_dir = payload.get("directory", "/")
            destination_base = payload.get("destination")
            
            if not entries or not destination_base:
                self._show_error_toast("Invalid download parameters")
                return
                
            if not isinstance(destination_base, pathlib.Path):
                destination_base = pathlib.Path(destination_base)
                
            # Check for conflicts and proceed with download
            self._check_download_conflicts(entries, source_dir, destination_base)
                    
        except Exception as e:
            logger.error(f"Download operation error: {e}")
            self._show_error_toast(f"Download failed: {e}")
            
    def _check_download_conflicts(self, entries: List[FileEntry], source_dir: str, 
                                destination_base: pathlib.Path) -> None:
        """Check for download conflicts and handle resolution."""
        conflicts = []
        
        for entry in entries:
            dest_path = destination_base / entry.name
            if dest_path.exists():
                conflicts.append((entry, dest_path))
        
        if conflicts:
            self._show_download_conflict_dialog(entries, source_dir, destination_base, conflicts)
        else:
            self._proceed_with_download(entries, source_dir, destination_base)
            
    def _show_download_conflict_dialog(self, entries: List[FileEntry], source_dir: str,
                                     destination_base: pathlib.Path, 
                                     conflicts: List[Tuple[FileEntry, pathlib.Path]]) -> None:
        """Show conflict resolution dialog for downloads."""
        count = len(conflicts)
        if count == 1:
            entry, dest_path = conflicts[0]
            title = "File Already Exists"
            message = f"'{entry.name}' already exists in the destination folder.\n\nDo you want to replace it?"
        else:
            title = "Files Already Exist"
            message = f"{count} of {len(entries)} files already exist in the destination folder.\n\nWhat would you like to do?"
            
        dialog = Adw.MessageDialog.new(self, title, message)
        dialog.add_response("cancel", "Cancel")
        dialog.add_response("skip", "Skip Existing")
        dialog.add_response("replace", "Replace All")
        dialog.set_response_appearance("replace", Adw.ResponseAppearance.DESTRUCTIVE)
        dialog.set_default_response("skip")
        
        def on_response(dialog, response):
            if response == "cancel":
                pass
            elif response == "skip":
                # Download only non-conflicting files
                conflict_names = {entry.name for entry, _ in conflicts}
                non_conflicting = [e for e in entries if e.name not in conflict_names]
                if non_conflicting:
                    self._proceed_with_download(non_conflicting, source_dir, destination_base)
                    self._show_toast(f"Skipped {count} existing file{'s' if count > 1 else ''}")
            elif response == "replace":
                # Download all files, replacing existing ones
                self._proceed_with_download(entries, source_dir, destination_base)
            dialog.close()
            
        dialog.connect("response", on_response)
        dialog.present()
        
    def _proceed_with_download(self, entries: List[FileEntry], source_dir: str,
                             destination_base: pathlib.Path) -> None:
        """Proceed with download after conflict resolution."""
        if not entries:
            return
            
        # Show progress dialog
        progress_dialog = SFTPProgressDialog(parent=self, operation_type="download")
        progress_dialog.present()
        self._progress_dialogs.append(progress_dialog)
        
        # Download files sequentially
        self._download_files_sequentially(entries, source_dir, destination_base, progress_dialog, 0)
        
    def _download_files_sequentially(self, entries: List[FileEntry], source_dir: str,
                                   destination_base: pathlib.Path, 
                                   progress_dialog: SFTPProgressDialog, index: int) -> None:
        """Download files one by one to avoid overwhelming the connection."""
        if index >= len(entries) or progress_dialog.is_cancelled:
            if not progress_dialog.is_cancelled:
                progress_dialog.show_completion(True)
                # Refresh local directory
                GLib.idle_add(lambda: self._load_local_directory(str(destination_base)))
            return
            
        entry = entries[index]
        source_path = posixpath.join(source_dir, entry.name)
        dest_path = destination_base / entry.name
        
        def progress_callback(fraction: float, message: str, speed: float) -> None:
            if not progress_dialog.is_cancelled:
                # Combine file progress with overall progress
                combined_fraction = (index + fraction) / len(entries)
                file_info = f"Downloading {entry.name} ({index + 1}/{len(entries)})"
                progress_dialog.update_progress(combined_fraction, file_info, entry.name, speed)
        
        if self._sftp_manager:
            future = self._sftp_manager.download(source_path, dest_path, progress_callback)
            progress_dialog.set_future(future)
            
            def on_complete(fut):
                try:
                    fut.result()
                    # Continue with next file
                    GLib.idle_add(self._download_files_sequentially, entries, source_dir,
                                destination_base, progress_dialog, index + 1)
                except Exception as e:
                    error_msg = str(e)
                    GLib.idle_add(lambda: progress_dialog.show_completion(False, error_msg))
                    
            future.add_done_callback(on_complete)
            self._current_operations[f"download_{index}_{time.time()}"] = future
        
    # Action handlers
    def _on_show_hidden_action(self, action, parameter) -> None:
        """Handle show hidden files action."""
        current_state = action.get_state().get_boolean()
        new_state = not current_state
        action.set_state(GLib.Variant.new_boolean(new_state))
        
        # Apply to both panes
        self._local_pane.set_show_hidden(new_state)
        self._remote_pane.set_show_hidden(new_state)
        
    def _on_refresh_all_action(self, action, parameter) -> None:
        """Handle refresh all action."""
        # Refresh local pane
        self._load_local_directory(self._local_pane.get_current_path())
        
        # Refresh remote pane
        if self._sftp_manager and self._connection_established:
            self._sftp_manager.listdir(self._remote_pane.get_current_path())
            
    def _on_disconnect_action(self, action, parameter) -> None:
        """Handle disconnect action."""
        if self._sftp_manager:
            self._sftp_manager.close()
            self._sftp_manager = None
            
        self._connection_established = False
        self._connection_status.set_text("Disconnected")
        self.close()
        
    # Utility methods
    def _show_toast(self, message: str) -> None:
        """Show informational toast."""
        toast = Adw.Toast.new(message)
        self._toast_overlay.add_toast(toast)
        
    def _show_error_toast(self, message: str) -> None:
        """Show error toast."""
        toast = Adw.Toast.new(message)
        toast.set_priority(Adw.ToastPriority.HIGH)
        self._toast_overlay.add_toast(toast)
        
    def do_close_request(self) -> bool:
        """Handle window close request."""
        if self._sftp_manager:
            self._sftp_manager.close()
            
        # Cancel any pending operations
        for future in self._current_operations.values():
            if not future.done():
                future.cancel()
                
        # Close progress dialogs
        for dialog in self._progress_dialogs:
            if dialog.get_visible():
                dialog.close()
                
        return False  # Allow close


def launch_file_manager_window(
    *,
    host: str,
    username: str,
    port: int = 22,
    path: str = "~",
    parent: Optional[Gtk.Window] = None,
    transient_for_parent: bool = True,
) -> FileManagerWindow:
    """Launch the enhanced file manager window.
    
    Args:
        host: SSH hostname
        username: SSH username
        port: SSH port (default: 22)
        path: Initial remote path (default: "~")
        parent: Optional parent window
        transient_for_parent: Set transient relationship
        
    Returns:
        FileManagerWindow instance
        
    Raises:
        RuntimeError: If no application instance is available
    """
    app = Gtk.Application.get_default()
    if app is None:
        raise RuntimeError("An application instance is required")
        
    window = FileManagerWindow(
        application=app,
        host=host,
        username=username,
        port=port,
        initial_path=path
    )
    
    if parent and transient_for_parent:
        window.set_transient_for(parent)
        
    window.present()
    return window


# Configure logging for the module
def setup_logging(level: int = logging.INFO) -> None:
    """Setup logging configuration for the file manager.
    
    Args:
        level: Logging level (default: INFO)
    """
    logging.basicConfig(
        level=level,
        format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
        handlers=[
            logging.StreamHandler(),
            logging.FileHandler('sftp_file_manager.log')
        ]
    )


# Example usage
if __name__ == "__main__":
    import sys
    
    # Setup logging
    setup_logging(logging.DEBUG)
    
    # Create application
    app = Adw.Application.new("com.example.SFTPFileManager", Gio.ApplicationFlags.FLAGS_NONE)
    
    def on_activate(app):
        # Example connection parameters - replace with real values
        try:
            window = launch_file_manager_window(
                host="example.com",
                username="user",
                port=22,
                path="~"
            )
        except Exception as e:
            logger.error(f"Failed to launch file manager: {e}")
            sys.exit(1)
    
    app.connect("activate", on_activate)
    
    # Run application
    try:
        app.run(sys.argv)
    except KeyboardInterrupt:
        logger.info("Application interrupted by user")
        sys.exit(0)


# Export public API
__all__ = [
    "FileManagerWindow",
    "AsyncSFTPManager", 
    "FileEntry",
    "SFTPProgressDialog",
    "launch_file_manager_window",
    "OperationType",
    "SortKey",
    "setup_logging",
]"""Enhanced production-ready SFTP file manager window.

This module provides a robust libadwaita-based file manager for SFTP operations
with comprehensive error handling, logging, performance optimizations, and
full GNOME HIG compliance.
"""

from __future__ import annotations

import dataclasses
import logging
import os
import pathlib
import posixpath
import shutil
import threading
import time
import weakref
from concurrent.futures import Future, ThreadPoolExecutor, as_completed
from contextlib import contextmanager
from datetime import datetime
from enum import Enum, auto
from typing import (
    Any, Callable, Dict, Generator, Iterable, List, Optional, Set, Tuple, Union
)

import paramiko
from gi.repository import Adw, Gio, GLib, GObject, Gdk, Gtk, Pango

# Configure logging
logger = logging.getLogger(__name__)


class OperationType(Enum):
    """Types of file operations."""
    UPLOAD = auto()
    DOWNLOAD = auto()
    DELETE = auto()
    RENAME = auto()
    CREATE_FOLDER = auto()


class SortKey(Enum):
    """Available sort keys for file listings."""
    NAME = "name"
    SIZE = "size"
    MODIFIED = "modified"
    TYPE = "type"


class TransferCancelledException(Exception):
    """Exception raised when a transfer is cancelled."""
    pass


class FileOperationError(Exception):
    """Exception raised for file operation errors."""
    pass


# Global CSS provider for drop zone styling
_DROP_ZONE_CSS_PROVIDER: Optional[Gtk.CssProvider] = None


def ensure_drop_zone_css() -> None:
    """Ensure the CSS used to highlight drop zones is loaded once."""
    global _DROP_ZONE_CSS_PROVIDER
    
    if _DROP_ZONE_CSS_PROVIDER is not None:
        return

    try:
        provider = Gtk.CssProvider()
        css_data = b"""
        .file-pane-drop-zone {
            border: 2px dashed alpha(@accent_color, 0.5);
            border-radius: 12px;
            background-color: alpha(@accent_color, 0.15);
            padding: 16px 24px;
            box-shadow: 0 4px 16px alpha(@shade_color, 0.15);
            backdrop-filter: blur(8px);
            transition: all 200ms cubic-bezier(0.25, 0.1, 0.25, 1);
        }
        
        .file-pane-drop-zone.visible {
            border-style: solid;
            border-color: @accent_color;
            background-color: alpha(@accent_color, 0.25);
            box-shadow: 0 8px 24px alpha(@shade_color, 0.25);
            transform: translateY(-4px);
        }
        
        .file-pane-drop-zone .drop-zone-title {
            font-weight: 600;
            color: @accent_color;
        }
        """
        
        provider.load_from_data(css_data)
        
        display = Gdk.Display.get_default()
        if display:
            Gtk.StyleContext.add_provider_for_display(
                display, provider, Gtk.STYLE_PROVIDER_PRIORITY_APPLICATION
            )
        
        _DROP_ZONE_CSS_PROVIDER = provider
        logger.debug("Drop zone CSS loaded successfully")
        
    except Exception as e:
        logger.error(f"Failed to load drop zone CSS: {e}")
        _DROP_ZONE_CSS_PROVIDER = None


@dataclasses.dataclass(frozen=True)
class FileEntry:
    """Immutable description of a directory entry."""
    name: str
    is_dir: bool
    size: int
    modified: float
    
    def __post_init__(self) -> None:
        """Validate entry data."""
        if not self.name:
            raise ValueError("File name cannot be empty")
        if self.size < 0:
            object.__setattr__(self, 'size', 0)
        if self.modified < 0:
            object.__setattr__(self, 'modified', 0.0)


class ProgressTracker:
    """Thread-safe progress tracking for file operations."""
    
    def __init__(self, total_files: int = 0, total_bytes: int = 0) -> None:
        self._lock = threading.RLock()
        self._total_files = total_files
        self._total_bytes = total_bytes
        self._completed_files = 0
        self._transferred_bytes = 0
        self._current_file = ""
        self._cancelled = False
        self._start_time = time.time()
        
    def update_file_progress(self, filename: str, bytes_transferred: int, total_file_bytes: int) -> None:
        """Update progress for current file."""
        with self._lock:
            self._current_file = filename
            self._transferred_bytes = bytes_transferred
            
    def complete_file(self) -> None:
        """Mark current file as completed."""
        with self._lock:
            self._completed_files += 1
            
    def cancel(self) -> None:
        """Cancel the operation."""
        with self._lock:
            self._cancelled = True
            
    def is_cancelled(self) -> bool:
        """Check if operation is cancelled."""
        with self._lock:
            return self._cancelled
    
    def get_progress(self) -> Tuple[float, str, float]:
        """Get current progress as (fraction, status_message, speed_bytes_per_sec)."""
        with self._lock:
            if self._total_files == 0:
                return 0.0, "Preparing...", 0.0
                
            file_fraction = self._completed_files / self._total_files
            status = f"Processing {self._current_file}" if self._current_file else "Processing files..."
            
            elapsed = time.time() - self._start_time
            speed = self._transferred_bytes / elapsed if elapsed > 0 else 0.0
            
            return file_fraction, status, speed


class SFTPProgressDialog(Adw.Window):
    """Modern, GNOME HIG-compliant progress dialog for file transfers."""
    
    def __init__(self, parent: Optional[Gtk.Window] = None, operation_type: str = "transfer") -> None:
        super().__init__()
        
        self.set_title("File Transfer")
        self.set_default_size(480, 360)
        self.set_modal(True)
        self.set_resizable(False)
        
        if parent:
            self.set_transient_for(parent)
        
        self._operation_type = operation_type
        self._cancelled = False
        self._current_future: Optional[Future] = None
        self._tracker = ProgressTracker()
        self._start_time = time.time()
        
        self._build_ui()
        
    def _build_ui(self) -> None:
        """Build the modern UI following GNOME HIG."""
        main_box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL)
        self.set_content(main_box)
        
        # Header bar
        header = Adw.HeaderBar()
        header.set_show_end_title_buttons(True)
        main_box.append(header)
        
        # Content with proper spacing
        content = Gtk.Box(
            orientation=Gtk.Orientation.VERTICAL,
            spacing=32,
            margin_top=32,
            margin_bottom=32,
            margin_start=32,
            margin_end=32,
            valign=Gtk.Align.CENTER
        )
        main_box.append(content)
        
        # Status section
        status_box = Gtk.Box(
            orientation=Gtk.Orientation.VERTICAL,
            spacing=16,
            halign=Gtk.Align.CENTER
        )
        content.append(status_box)
        
        # Icon
        icon_name = "folder-download-symbolic" if self._operation_type == "download" else "folder-upload-symbolic"
        self._status_icon = Gtk.Image.new_from_icon_name(icon_name)
        self._status_icon.set_pixel_size(64)
        self._status_icon.add_css_class("accent")
        status_box.append(self._status_icon)
        
        # Status label
        self._status_label = Gtk.Label()
        self._status_label.set_markup("<span size='x-large' weight='bold'>Preparing transfer…</span>")
        self._status_label.set_justify(Gtk.Justification.CENTER)
        status_box.append(self._status_label)
        
        # File label
        self._file_label = Gtk.Label()
        self._file_label.set_text("Initializing...")
        self._file_label.set_ellipsize(Pango.EllipsizeMode.MIDDLE)
        self._file_label.add_css_class("dim-label")
        status_box.append(self._file_label)
        
        # Progress section
        progress_group = Adw.PreferencesGroup()
        content.append(progress_group)
        
        # Main progress bar
        self._progress_bar = Gtk.ProgressBar()
        self._progress_bar.set_show_text(True)
        self._progress_bar.set_text("0%")
        progress_group.add(self._progress_bar)
        
        # Details
        details_box = Gtk.Box(
            orientation=Gtk.Orientation.HORIZONTAL,
            spacing=12
        )
        progress_group.add(details_box)
        
        self._speed_label = Gtk.Label()
        self._speed_label.set_text("—")
        self._speed_label.set_halign(Gtk.Align.START)
        self._speed_label.add_css_class("caption")
        details_box.append(self._speed_label)
        
        spacer = Gtk.Box(hexpand=True)
        details_box.append(spacer)
        
        self._time_label = Gtk.Label()
        self._time_label.set_text("—")
        self._time_label.set_halign(Gtk.Align.END)
        self._time_label.add_css_class("caption")
        details_box.append(self._time_label)
        
        # Action buttons
        button_box = Gtk.Box(
            orientation=Gtk.Orientation.HORIZONTAL,
            spacing=12,
            halign=Gtk.Align.END
        )
        content.append(button_box)
        
        self._cancel_button = Gtk.Button(label="Cancel")
        self._cancel_button.connect("clicked", self._on_cancel)
        button_box.append(self._cancel_button)
        
        self._done_button = Gtk.Button(label="Done")
        self._done_button.add_css_class("suggested-action")
        self._done_button.set_visible(False)
        self._done_button.connect("clicked", lambda w: self.close())
        button_box.append(self._done_button)
        
    def set_future(self, future: Future) -> None:
        """Set the future for cancellation support."""
        self._current_future = future
        
    def update_progress(self, fraction: float, message: str = None, current_file: str = None,
                       speed_bytes_per_sec: float = 0.0) -> None:
        """Update progress (thread-safe)."""
        GLib.idle_add(self._update_ui, fraction, message, current_file, speed_bytes_per_sec)
        
    def _update_ui(self, fraction: float, message: str, current_file: str, speed: float) -> bool:
        """Update UI elements on main thread."""
        try:
            percentage = max(0, min(100, int(fraction * 100)))
            self._progress_bar.set_fraction(fraction)
            self._progress_bar.set_text(f"{percentage}%")
            
            if message:
                escaped_message = GLib.markup_escape_text(message)
                self._status_label.set_markup(f"<span size='x-large' weight='bold'>{escaped_message}</span>")
                
            if current_file:
                self._file_label.set_text(current_file)
                
            # Update speed display
            if speed > 0:
                if speed >= 1024 * 1024:  # MB/s
                    speed_text = f"{speed / (1024 * 1024):.1f} MB/s"
                elif speed >= 1024:  # KB/s
                    speed_text = f"{speed / 1024:.1f} KB/s"
                else:
                    speed_text = f"{speed:.0f} B/s"
                self._speed_label.set_text(speed_text)
                
                # Estimate remaining time
                if fraction > 0:
                    elapsed = time.time() - self._start_time
                    estimated_total = elapsed / fraction
                    remaining = max(0, estimated_total - elapsed)
                    
                    if remaining > 3600:
                        hours = int(remaining // 3600)
                        minutes = int((remaining % 3600) // 60)
                        self._time_label.set_text(f"{hours}h {minutes}m remaining")
                    elif remaining > 60:
                        minutes = int(remaining // 60)
                        self._time_label.set_text(f"{minutes}m remaining")
                    else:
                        self._time_label.set_text(f"{int(remaining)}s remaining")
                
        except Exception as e:
            logger.error(f"Error updating progress UI: {e}")
            
        return False
        
    def show_completion(self, success: bool = True, error_message: str = None) -> None:
        """Show completion state."""
        GLib.idle_add(self._show_completion_ui, success, error_message)
        
    def _show_completion_ui(self, success: bool, error_message: str) -> bool:
        """Update UI to show completion."""
        try:
            if success:
                self._status_icon.set_from_icon_name("emblem-ok-symbolic")
                self._status_icon.remove_css_class("accent")
                self._status_icon.add_css_class("success")
                
                self._status_label.set_markup("<span size='x-large' weight='bold'>Transfer completed</span>")
                self._file_label.set_text("All files transferred successfully")
                
                self._progress_bar.set_fraction(1.0)
                self._progress_bar.set_text("100%")
            else:
                self._status_icon.set_from_icon_name("dialog-error-symbolic")
                self._status_icon.remove_css_class("accent")
                self._status_icon.add_css_class("error")
                
                self._status_label.set_markup("<span size='x-large' weight='bold'>Transfer failed</span>")
                if error_message:
                    escaped_error = GLib.markup_escape_text(error_message)
                    self._file_label.set_text(f"Error: {escaped_error}")
                else:
                    self._file_label.set_text("An error occurred during the transfer")
            
            self._cancel_button.set_visible(False)
            self._done_button.set_visible(True)
            self._done_button.grab_focus()
            
        except Exception as e:
            logger.error(f"Error showing completion UI: {e}")
            
        return False
        
    def _on_cancel(self, button: Gtk.Button) -> None:
        """Handle cancel button click."""
        self._cancelled = True
        
        if self._current_future and not self._current_future.done():
            try:
                self._current_future.cancel()
                logger.info("Transfer operation cancelled by user")
            except Exception as e:
                logger.error(f"Error cancelling operation: {e}")
        
        self._status_label.set_markup("<span size='x-large' weight='bold'>Cancelling...</span>")
        self._file_label.set_text("Please wait while the operation is cancelled")
        
        button.set_sensitive(False)
        
    @property
    def is_cancelled(self) -> bool:
        """Check if operation was cancelled."""
        return self._cancelled


class AsyncSFTPManager(GObject.GObject):
    """Thread-safe SFTP manager with comprehensive error handling."""
    
    __gsignals__ = {
        "connected": (GObject.SignalFlags.RUN_FIRST, None, tuple()),
        "connection-error": (GObject.SignalFlags.RUN_FIRST, None, (str,)),
        "progress": (GObject.SignalFlags.RUN_FIRST, None, (float, str)),
        "operation-error": (GObject.SignalFlags.RUN_FIRST, None, (str,)),
        "directory-loaded": (GObject.SignalFlags.RUN_FIRST, None, (str, object)),
    }
    
    def __init__(self, host: str, username: str, port: int = 22, 
                 password: Optional[str] = None, max_workers: int = 4) -> None:
        super().__init__()
        
        self._host = host
        self._username = username
        self._password = password
        self._port = port
        self._client: Optional[paramiko.SSHClient] = None
        self._sftp: Optional[paramiko.SFTPClient] = None
        self._executor = ThreadPoolExecutor(max_workers=max_workers, thread_name_prefix="sftp")
        self._connection_lock = threading.RLock()
        self._operation_id_counter = 0
        self._cancelled_operations: Set[str] = set()
        
        logger.info(f"Initialized SFTP manager for {username}@{host}:{port}")
        
    def connect_to_server(self) -> Future:
        """Initiate connection to SFTP server."""
        return self._submit_operation(
            self._connect_impl,
            on_success=lambda _: self.emit("connected"),
            on_error=lambda exc: self.emit("connection-error", str(exc))
        )
        
    def close(self) -> None:
        """Close connections and cleanup resources."""
        logger.info("Closing SFTP manager")
        
        with self._connection_lock:
            if self._sftp:
                try:
                    self._sftp.close()
                except Exception as e:
                    logger.warning(f"Error closing SFTP client: {e}")
                finally:
                    self._sftp = None
                    
            if self._client:
                try:
                    self._client.close()
                except Exception as e:
                    logger.warning(f"Error closing SSH client: {e}")
                finally:
                    self._client = None
                    
        self._executor.shutdown(wait=False)
        
    def _submit_operation(self, func: Callable[[], Any], *,
                         on_success: Optional[Callable[[Any], None]] = None,
                         on_error: Optional[Callable[[Exception], None]] = None) -> Future:
        """Submit operation with standardized error handling."""
        future = self._executor.submit(func)
        
        def _handle_completion(fut: Future) -> None:
            try:
                result = fut.result()
                if on_success:
                    GLib.idle_add(lambda: on_success(result))
            except Exception as exc:
                logger.error(f"Operation failed: {exc}", exc_info=True)
                if on_error:
                    GLib.idle_add(lambda: on_error(exc))
                else:
                    GLib.idle_add(lambda: self.emit("operation-error", str(exc)))
                    
        future.add_done_callback(_handle_completion)
        return future
        
    def _connect_impl(self) -> None:
        """Establish SSH/SFTP connection."""
        logger.info(f"Connecting to {self._username}@{self._host}:{self._port}")
        
        try:
            client = paramiko.SSHClient()
            client.set_missing_host_key_policy(paramiko.AutoAddPolicy())
            
            client.connect(
                hostname=self._host,
                username=self._username,
                password=self._password,
                port=self._port,
                allow_agent=True,
                look_for_keys=True,
                timeout=30,
                auth_timeout=30
            )
            
            sftp = client.open_sftp()
            
            with self._connection_lock:
                self._client = client
                self._sftp = sftp
                
            logger.info("SFTP connection established successfully")
            
        except paramiko.AuthenticationException as e:
            raise FileOperationError(f"Authentication failed: {e}")
        except paramiko.SSHException as e:
            raise FileOperationError(f"SSH connection failed: {e}")
        except Exception as e:
            raise FileOperationError(f"Connection failed: {e}")
            
    def _ensure_connected(self) -> paramiko.SFTPClient:
        """Ensure we have a valid SFTP connection."""
        with self._connection_lock:
            if not self._sftp or not self._client:
                raise FileOperationError("Not connected to server")
            return self._sftp
            
    def listdir(self, path: str) -> Future:
        """List directory contents."""
        def _impl() -> Tuple[str, List[FileEntry]]:
            sftp = self._ensure_connected()
            
            # Handle path expansion
            expanded_path = self._expand_remote_path(path, sftp)
            
            try:
                entries = []
                for attr in sftp.listdir_attr(expanded_path):
                    if not attr.filename:
                        continue
                        
                    entry = FileEntry(
                        name=attr.filename,
                        is_dir=self._is_directory(attr),
                        size=attr.st_size or 0,
                        modified=attr.st_mtime or 0.0
                    )
                    entries.append(entry)
                    
                logger.debug(f"Listed {len(entries)} entries in {expanded_path}")
                return expanded_path, entries
                
            except IOError as e:
                raise FileOperationError(f"Cannot list directory '{expanded_path}': {e}")
                
        return self._submit_operation(
            _impl,
            on_success=lambda result: self.emit("directory-loaded", *result)
        )
        
    def _expand_remote_path(self, path: str, sftp: paramiko.SFTPClient) -> str:
        """Expand ~ and relative paths on remote server."""
        if path == "~" or path.startswith("~/"):
            try:
                home_path = sftp.normalize(".")
                if path == "~":
                    return home_path
                else:
                    return posixpath.join(home_path, path[2:])
            except Exception:
                # Fallback to common patterns
                home_fallbacks = [
                    f"/home/{self._username}",
                    f"/Users/{self._username}",
                    f"/export/home/{self._username}"
                ]
                
                for fallback in home_fallbacks:
                    try:
                        sftp.listdir_attr(fallback)
                        if path == "~":
                            return fallback
                        else:
                            return posixpath.join(fallback, path[2:])
                    except Exception:
                        continue
                        
                # Ultimate fallback
                return f"/home/{self._username}" + (path[1:] if path.startswith("~/") else "")
                
        return path
        
    @staticmethod
    def _is_directory(attr: paramiko.SFTPAttributes) -> bool:
        """Check if SFTP attributes represent a directory."""
        import stat
        return bool(attr.st_mode and stat.S_ISDIR(attr.st_mode))
        
    def download(self, source: str, destination: pathlib.Path,
                progress_callback: Optional[Callable[[float, str, float], None]] = None) -> Future:
        """Download a file with progress tracking."""
        operation_id = self._generate_operation_id()
        
        def _impl() -> None:
            sftp = self._ensure_connected()
            
            # Ensure destination directory exists
            destination.parent.mkdir(parents=True, exist_ok=True)
            
            # Get file size for progress calculation
            try:
                file_stat = sftp.stat(source)
                total_size = file_stat.st_size or 0
            except Exception:
                total_size = 0
                
            transferred = 0
            start_time = time.time()
            
            def _progress_wrapper(bytes_transferred: int, total_bytes: int) -> None:
                nonlocal transferred
                if operation_id in self._cancelled_operations:
                    raise TransferCancelledException("Download cancelled")
                    
                transferred = bytes_transferred
                if progress_callback and total_bytes > 0:
                    progress = bytes_transferred / total_bytes
                    elapsed = time.time() - start_time
                    speed = bytes_transferred / elapsed if elapsed > 0 else 0.0
                    
                    size_text = f"{self._format_size(bytes_transferred)} of {self._format_size(total_bytes)}"
                    progress_callback(progress, f"Downloaded {size_text}", speed)
                    
            try:
                logger.info(f"Starting download: {source} -> {destination}")
                sftp.get(str(source), str(destination), callback=_progress_wrapper)
                
                if operation_id not in self._cancelled_operations and progress_callback:
                    progress_callback(1.0, "Download completed", 0.0)
                    
                logger.info(f"Download completed: {destination}")
                
            except TransferCancelledException:
                logger.info(f"Download cancelled: {source}")
                # Clean up partial file
                if destination.exists():
                    destination.unlink()
                raise
            except Exception as e:
                logger.error(f"Download failed: {e}")
                if destination.exists():
                    destination.unlink()
                raise FileOperationError(f"Download failed: {e}")
            finally:
                self._cancelled_operations.discard(operation_id)
                
        future = self._submit_operation(_impl)
        
        # Add cancellation support
        original_cancel = future.cancel
        def enhanced_cancel():
            self._cancelled_operations.add(operation_id)
            return original_cancel()
        future.cancel = enhanced_cancel
        
        return future
        
    def upload(self, source: pathlib.Path, destination: str,
              progress_callback: Optional[Callable[[float, str, float], None]] = None) -> Future:
        """Upload a file with progress tracking."""
        operation_id = self._generate_operation_id()
        
        def _impl() -> None:
            sftp = self._ensure_connected()
            
            if not source.exists():
                raise FileOperationError(f"Source file does not exist: {source}")
                
            total_size = source.stat().st_size
            transferred = 0
            start_time = time.time()
            
            def _progress_wrapper(bytes_transferred: int, total_bytes: int) -> None:
                nonlocal transferred
                if operation_id in self._cancelled_operations:
                    raise TransferCancelledException("Upload cancelled")
                    
                transferred = bytes_transferred
                if progress_callback and total_bytes > 0:
                    progress = bytes_transferred / total_bytes
                    elapsed = time.time() - start_time
                    speed = bytes_transferred / elapsed if elapsed > 0 else 0.0
                    
                    size_text = f"{self._format_size(bytes_transferred)} of {self._format_size(total_bytes)}"
                    progress_callback(progress, f"Uploaded {size_text}", speed)
                    
            try:
                logger.info(f"Starting upload: {source} -> {destination}")
                sftp.put(str(source), destination, callback=_progress_wrapper)
                
                if operation_id not in self._cancelled_operations and progress_callback:
                    progress_callback(1.0, "Upload completed", 0.0)
                    
                logger.info(f"Upload completed: {destination}")
                
            except TransferCancelledException:
                logger.info(f"Upload cancelled: {source}")
                # Try to remove partial file
                try:
                    sftp.remove(destination)
                except Exception:
                    pass
                raise
            except Exception as e:
                logger.error(f"Upload failed: {e}")
                raise FileOperationError(f"Upload failed: {e}")
            finally:
                self._cancelled_operations.discard(operation_id)
                
        future = self._submit_operation(_impl)
        
        # Add cancellation support
        original_cancel = future.cancel
        def enhanced_cancel():
            self._cancelled_operations.add(operation_id)
            return original_cancel()
        future.cancel = enhanced_cancel
        
        return future
        
    def mkdir(self, path: str) -> Future:
        """Create directory on remote server."""
        def _impl() -> None:
            sftp = self._ensure_connected()
            try:
                sftp.mkdir(path)
                logger.info(f"Created directory: {path}")
            except IOError as e:
                raise FileOperationError(f"Cannot create directory '{path}': {e}")
                
        return self._submit_operation(
            _impl,
            on_success=lambda _: self.listdir(posixpath.dirname(path) or "/")
        )
        
    def remove(self, path: str) -> Future:
        """Remove file or directory on remote server."""
        def _impl() -> None:
            sftp = self._ensure_connected()
            
            try:
                # Try to remove as file first
                sftp.remove(path)
                logger.info(f"Removed file: {path}")
            except IOError:
                # Try to remove as directory
                try:
                    self._remove_directory_recursive(sftp, path)
                    logger.info(f"Removed directory: {path}")
                except IOError as e:
                    raise FileOperationError(f"Cannot remove '{path}': {e}")
                    
        parent = posixpath.dirname(path) or "/"
        return self._submit_operation(
            _impl,
            on_success=lambda _: self.listdir(parent)
        )
        
    def _remove_directory_recursive(self, sftp: paramiko.SFTPClient, path: str) -> None:
        """Recursively remove directory and contents."""
        try:
            for item in sftp.listdir_attr(path):
                item_path = posixpath.join(path, item.filename)
                if self._is_directory(item):
                    self._remove_directory_recursive(sftp, item_path)
                else:
                    sftp.remove(item_path)
            sftp.rmdir(path)
        except IOError as e:
            raise FileOperationError(f"Error removing directory contents: {e}")
            
    def rename(self, source: str, target: str) -> Future:
        """Rename file or directory on remote server."""
        def _impl() -> None:
            sftp = self._ensure_connected()
            try:
                sftp.rename(source, target)
                logger.info(f"Renamed: {source} -> {target}")
            except IOError as e:
                raise FileOperationError(f"Cannot rename '{source}' to '{target}': {e}")
                
        return self._submit_operation(
            _impl,
            on_success=lambda _: self.listdir(posixpath.dirname(target) or "/")
        )
        
    def _generate_operation_id(self) -> str:
        """Generate unique operation ID."""
        self._operation_id_counter += 1
        return f"op_{self._operation_id_counter}_{time.time()}"
        
    @staticmethod
    def _format_size(size_bytes: int) -> str:
        """Format file size for display."""
        if size_bytes < 0:
            return "—"
        if size_bytes == 0:
            return "0 B"
        if size_bytes < 1024:
            return f"{size_bytes} B"
            
        units = ["KB", "MB", "GB", "TB", "PB"]
        value = float(size_bytes)
        
        for unit in units:
            value /= 1024.0
            if value < 1024.0:
                if value < 10:
                    return f"{value:.1f} {unit}"
                else:
                    return f"{value:.0f} {unit}"
                    
        return f"{value:.1f} EB"


class FilePane(Gtk.Box):
    """Enhanced file pane with robust error handling and performance optimizations."""
    
    __gsignals__ = {
        "path-changed": (GObject.SignalFlags.RUN_FIRST, None, (str,)),
        "request-operation": (GObject.SignalFlags.RUN_FIRST, None, (str, object)),
    }
    
    def __init__(self, label: str, is_remote: bool = False) -> None:
        super().__init__(orientation=Gtk.Orientation.VERTICAL)
        
        self._label = label
        self._is_remote = is_remote
        self._current_path = "~" if is_remote else os.path.expanduser("~")
        self._entries: List[FileEntry] = []
        self._filtered_entries: List[FileEntry] = []
        self._show_hidden = False
        self._sort_key = SortKey.NAME
        self._sort_descending = False
        
        # History management
        self._history: List[str] = []
        self._history_index = -1
        
        # UI state
        self._current_view = "list"
        self._partner_pane: Optional[FilePane] = None
        
        # Performance optimization
        self._update_pending = False
        
        # Drag and drop state
        self._drag_highlight = False
        
        self._build_ui()
        self._setup_actions()
        self._setup_drag_drop()
        
        logger.debug(f"Initialized {label} file pane (remote: {is_remote})")
        
    def _build_ui(self) -> None:
        """Build the file pane UI."""
        # Toolbar
        toolbar = Adw.ToolbarView()
        self.append(toolbar)
        
        # Header bar
        header = Adw.HeaderBar()
        header.set_show_start_title_buttons(False)
        header.set_show_end_title_buttons(False)
        
        # Title with pane label
        title_label = Gtk.Label(label=self._label)
        title_label.add_css_class("title")
        header.set_title_widget(title_label)
        
        # Navigation controls
        nav_box = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=6)
        
        self._back_button = Gtk.Button.new_from_icon_name("go-previous-symbolic")
        self._back_button.set_tooltip_text("Go Back")
        self._back_button.add_css_class("flat")
        self._back_button.connect("clicked", self._on_back_clicked)
        nav_box.append(self._back_button)
        
        self._forward_button = Gtk.Button.new_from_icon_name("go-next-symbolic")
        self._forward_button.set_tooltip_text("Go Forward")
        self._forward_button.add_css_class("flat")
        self._forward_button.connect("clicked", self._on_forward_clicked)
        nav_box.append(self._forward_button)
        
        self._up_button = Gtk.Button.new_from_icon_name("go-up-symbolic")
        self._up_button.set_tooltip_text("Go Up")
        self._up_button.add_css_class("flat")
        self._up_button.connect("clicked", self._on_up_clicked)
        nav_box.append(self._up_button)
        
        self._refresh_button = Gtk.Button.new_from_icon_name("view-refresh-symbolic")
        self._refresh_button.set_tooltip_text("Refresh")
        self._refresh_button.add_css_class("flat")
        self._refresh_button.connect("clicked", self._on_refresh_clicked)
        nav_box.append(self._refresh_button)
        
        header.pack_start(nav_box)
        
        # Path entry
        self._path_entry = Gtk.Entry()
        self._path_entry.set_hexpand(True)
        self._path_entry.set_placeholder_text("Enter path...")
        self._path_entry.connect("activate", self._on_path_entry_activate)
        header.pack_start(self._path_entry)
        
        # View controls
        view_box = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=6)
        
        # View toggle button
        self._view_button = Gtk.Button.new_from_icon_name("view-list-symbolic")
        self._view_button.set_tooltip_text("Toggle View")
        self._view_button.add_css_class("flat")
        self._view_button.connect("clicked", self._on_view_toggle)
        view_box.append(self._view_button)
        
        # Sort menu button
        sort_menu = Gio.Menu()
        sort_menu.append("Name", "pane.sort-name")
        sort_menu.append("Size", "pane.sort-size")
        sort_menu.append("Modified", "pane.sort-modified")
        sort_menu.append_separator()
        sort_menu.append("Ascending", "pane.sort-asc")
        sort_menu.append("Descending", "pane.sort-desc")
        
        self._sort_button = Gtk.MenuButton()
        self._sort_button.set_icon_name("view-sort-symbolic")
        self._sort_button.set_tooltip_text("Sort Options")
        self._sort_button.set_menu_model(sort_menu)
        view_box.append(self._sort_button)
        
        header.pack_end(view_box)
        toolbar.add_top_bar(header)
        
        # Main content area
        self._toast_overlay = Adw.ToastOverlay()
        toolbar.set_content(self._toast_overlay)
        
        # Create stack for different views
        self._view_stack = Gtk.Stack()
        self._view_stack.set_transition_type(Gtk.StackTransitionType.CROSSFADE)
        
        # List store for file entries
        self._list_store = Gio.ListStore(item_type=GObject.TYPE_PYOBJECT)
        self._selection_model = Gtk.MultiSelection.new(self._list_store)
        self._selection_model.connect("selection-changed", self._on_selection_changed)
        
        # List view
        self._list_view = self._create_list_view()
        list_scroll = Gtk.ScrolledWindow()
        list_scroll.set_policy(Gtk.PolicyType.AUTOMATIC, Gtk.PolicyType.AUTOMATIC)
        list_scroll.set_child(self._list_view)
        self._view_stack.add_named(list_scroll, "list")
        
        # Grid view
        self._grid_view = self._create_grid_view()
        grid_scroll = Gtk.ScrolledWindow()
        grid_scroll.set_policy(Gtk.PolicyType.AUTOMATIC, Gtk.PolicyType.AUTOMATIC)
        grid_scroll.set_child(self._grid_view)
        self._view_stack.add_named(grid_scroll, "grid")
        
        # Add drop zone overlay
        self._drop_overlay = Gtk.Overlay()
        self._drop_overlay.set_child(self._view_stack)
        
        self._drop_zone = self._create_drop_zone()
        self._drop_overlay.add_overlay(self._drop_zone)
        
        self._toast_overlay.set_child(self._drop_overlay)
        
        # Action bar
        action_bar = Gtk.ActionBar()
        action_bar.add_css_class("inline-toolbar")
        
        if self._is_remote:
            download_button = Gtk.Button()
            content = Adw.ButtonContent()
            content.set_icon_name("document-save-symbolic")
            content.set_label("Download")
            download_button.set_child(content)
            download_button.connect("clicked", self._on_download_clicked)
            action_bar.pack_start(download_button)
            self._download_button = download_button
        else:
            upload_button = Gtk.Button()
            content = Adw.ButtonContent()
            content.set_icon_name("document-send-symbolic")
            content.set_label("Upload")
            upload_button.set_child(content)
            upload_button.connect("clicked", self._on_upload_clicked)
            action_bar.pack_start(upload_button)
            self._upload_button = upload_button
        
        # Common action buttons
        new_folder_button = Gtk.Button.new_from_icon_name("folder-new-symbolic")
        new_folder_button.set_tooltip_text("New Folder")
        new_folder_button.connect("clicked", self._on_new_folder_clicked)
        action_bar.pack_end(new_folder_button)
        
        rename_button = Gtk.Button.new_from_icon_name("document-edit-symbolic")
        rename_button.set_tooltip_text("Rename")
        rename_button.connect("clicked", self._on_rename_clicked)
        action_bar.pack_end(rename_button)
        
        delete_button = Gtk.Button.new_from_icon_name("user-trash-symbolic")
        delete_button.set_tooltip_text("Delete")
        delete_button.connect("clicked", self._on_delete_clicked)
        action_bar.pack_end(delete_button)
        
        self._action_buttons = {
            'new_folder': new_folder_button,
            'rename': rename_button,
            'delete': delete_button
        }
        
        if hasattr(self, '_download_button'):
            self._action_buttons['download'] = self._download_button
        if hasattr(self, '_upload_button'):
            self._action_buttons['upload'] = self._upload_button
            
        self.append(action_bar)
        
        self._update_navigation_buttons()
        
    def _create_list_view(self) -> Gtk.ColumnView:
        """Create the column view for list display."""
        column_view = Gtk.ColumnView(model=self._selection_model)
        column_view.set_show_row_separators(True)
        column_view.set_show_column_separators(True)
        column_view.set_enable_rubberband(True)
        
        # Name column
        name_factory = Gtk.SignalListItemFactory()
        name_factory.connect("setup", self._on_name_column_setup)
        name_factory.connect("bind", self._on_name_column_bind)
        name_column = Gtk.ColumnViewColumn(title="Name", factory=name_factory)
        name_column.set_expand(True)
        name_column.set_resizable(True)
        column_view.append_column(name_column)
        
        # Size column
        size_factory = Gtk.SignalListItemFactory()
        size_factory.connect("setup", self._on_size_column_setup)
        size_factory.connect("bind", self._on_size_column_bind)
        size_column = Gtk.ColumnViewColumn(title="Size", factory=size_factory)
        size_column.set_resizable(True)
        column_view.append_column(size_column)
        
        # Modified column
        modified_factory = Gtk.SignalListItemFactory()
        modified_factory.connect("setup", self._on_modified_column_setup)
        modified_factory.connect("bind", self._on_modified_column_bind)
        modified_column = Gtk.ColumnViewColumn(title="Modified", factory=modified_factory)
        modified_column.set_resizable(True)
        column_view.append_column(modified_column)
        
        column_view.connect("activate", self._on_item_activated)
        
        # Add context menu
        self._setup_context_menu(column_view)
        
        return column_view
        
    def _create_grid_view(self) -> Gtk.GridView:
        """Create the grid view for icon display."""
        factory = Gtk.SignalListItemFactory()
        factory.connect("setup", self._on_grid_setup)
        factory.connect("bind", self._on_grid_bind)
        
        grid_view = Gtk.GridView(
            model=self._selection_model,
            factory=factory,
            max_columns=8,
            min_columns=2
        )
        grid_view.set_enable_rubberband(True)
        grid_view.connect("activate", self._on_item_activated)
        
        # Add context menu
        self._setup_context_menu(grid_view)
        
        return grid_view
        
    def _create_drop_zone(self) -> Gtk.Revealer:
        """Create the drop zone overlay."""
        revealer = Gtk.Revealer()
        revealer.set_transition_type(Gtk.RevealerTransitionType.SLIDE_UP)
        revealer.set_halign(Gtk.Align.CENTER)
        revealer.set_valign(Gtk.Align.END)
        revealer.set_can_target(False)
        
        drop_box = Gtk.Box(
            orientation=Gtk.Orientation.VERTICAL,
            spacing=12,
            halign=Gtk.Align.CENTER,
            valign=Gtk.Align.CENTER
        )
        drop_box.set_margin_top(24)
        drop_box.set_margin_bottom(24)
        drop_box.set_margin_start(24)
        drop_box.set_margin_end(24)
        drop_box.add_css_class("file-pane-drop-zone")
        
        # Drop zone icon
        icon_name = "folder-upload-symbolic" if self._is_remote else "folder-download-symbolic"
        drop_icon = Gtk.Image.new_from_icon_name(icon_name)
        drop_icon.set_pixel_size(48)
        drop_icon.add_css_class("accent")
        drop_box.append(drop_icon)
        
        # Drop zone label
        drop_label = Gtk.Label()
        drop_text = "Drop files to upload" if self._is_remote else "Drop files here"
        drop_label.set_markup(f"<span size='large' weight='bold'>{drop_text}</span>")
        drop_label.add_css_class("drop-zone-title")
        drop_box.append(drop_label)
        
        revealer.set_child(drop_box)
        revealer.set_reveal_child(False)
        
        self._drop_revealer = revealer
        return revealer
        
    def _setup_actions(self) -> None:
        """Setup action group and actions."""
        self._action_group = Gio.SimpleActionGroup()
        self.insert_action_group("pane", self._action_group)
        
        # Sort actions
        actions = [
            ("sort-name", self._on_sort_name),
            ("sort-size", self._on_sort_size),
            ("sort-modified", self._on_sort_modified),
            ("sort-asc", self._on_sort_ascending),
            ("sort-desc", self._on_sort_descending),
            ("download", self._on_context_download),
            ("upload", self._on_context_upload),
            ("rename", self._on_context_rename),
            ("delete", self._on_context_delete),
            ("new-folder", self._on_context_new_folder),
            ("properties", self._on_context_properties),
        ]
        
        for name, callback in actions:
            action = Gio.SimpleAction.new(name, None)
            action.connect("activate", lambda a, p, cb=callback: cb())
            self._action_group.add_action(action)
            
    def _setup_context_menu(self, view: Gtk.Widget) -> None:
        """Setup context menu for a view."""
        # Right-click context menu
        right_click = Gtk.GestureClick()
        right_click.set_button(Gdk.BUTTON_SECONDARY)
        
        def on_right_click(gesture, n_press, x, y):
            self._show_context_menu(view, x, y)
            
        right_click.connect("pressed", on_right_click)
        view.add_controller(right_click)
        
        # Long press for touch
        long_press = Gtk.GestureLongPress()
        long_press.connect("pressed", lambda g, x, y: self._show_context_menu(view, x, y))
        view.add_controller(long_press)
        
    def _show_context_menu(self, view: Gtk.Widget, x: float, y: float) -> None:
        """Show context menu."""
        menu = Gio.Menu()
        
        selected = self._get_selected_entries()
        has_selection = len(selected) > 0
        single_selection = len(selected) == 1
        
        if self._is_remote and has_selection:
            menu.append("Download", "pane.download")
        elif not self._is_remote and has_selection:
            menu.append("Upload", "pane.upload")
            
        if single_selection:
            menu.append("Rename", "pane.rename")
            
        if has_selection:
            menu.append("Delete", "pane.delete")
            
        menu.append_separator()
        menu.append("New Folder", "pane.new-folder")
        
        if single_selection:
            menu.append("Properties", "pane.properties")
        
        # Create popover
        popover = Gtk.PopoverMenu.new_from_model(menu)
        popover.set_parent(view)
        popover.set_has_arrow(True)
        
        # Position popover
        rect = Gdk.Rectangle()
        rect.x = int(x)
        rect.y = int(y)
        rect.width = 1
        rect.height = 1
        popover.set_pointing_to(rect)
        
        popover.popup()
        
    def _setup_drag_drop(self) -> None:
        """Setup drag and drop functionality."""
        ensure_drop_zone_css()
        
        # Drop target
        drop_target = Gtk.DropTarget.new(GObject.TYPE_STRING, Gdk.DragAction.COPY)
        drop_target.set_gtypes([Gio.File, GObject.TYPE_STRING])
        drop_target.connect("accept", self._on_drop_accept)
        drop_target.connect("enter", self._on_drop_enter)
        drop_target.connect("leave", self._on_drop_leave)
        drop_target.connect("drop", self._on_drop)
        self.add_controller(drop_target)
        
        # Drag sources for both views
        for view in [self._list_view, self._grid_view]:
            drag_source = Gtk.DragSource()
            drag_source.set_actions(Gdk.DragAction.COPY)
            drag_source.connect("prepare", self._on_drag_prepare)
            drag_source.connect("drag-begin", self._on_drag_begin)
            drag_source.connect("drag-end", self._on_drag_end)
            view.add_controller(drag_source)
            
    # Column setup and bind methods
    def _on_name_column_setup(self, factory: Gtk.SignalListItemFactory, item) -> None:
        """Setup name column."""
        box = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
        
        icon = Gtk.Image()
        icon.set_pixel_size(16)
        box.append(icon)
        
        label = Gtk.Label(xalign=0)
        label.set_ellipsize(Pango.EllipsizeMode.END)
        label.set_hexpand(True)
        box.append(label)
        
        item.set_child(box)
        
    def _on_name_column_bind(self, factory: Gtk.SignalListItemFactory, item) -> None:
        """Bind data to name column."""
        box = item.get_child()
        icon = box.get_first_child()
        label = box.get_last_child()
        
        entry = item.get_item()
        if not isinstance(entry, FileEntry):
            return
            
        # Set icon
        if entry.is_dir:
            icon.set_from_icon_name("folder-symbolic")
        else:
            icon.set_from_icon_name("text-x-generic-symbolic")
            
        # Set label
        label.set_text(entry.name)
        label.set_tooltip_text(entry.name)
        
    def _on_size_column_setup(self, factory: Gtk.SignalListItemFactory, item) -> None:
        """Setup size column."""
        label = Gtk.Label(xalign=1)
        label.set_halign(Gtk.Align.END)
        label.add_css_class("dim-label")
        item.set_child(label)
        
    def _on_size_column_bind(self, factory: Gtk.SignalListItemFactory, item) -> None:
        """Bind data to size column."""
        label = item.get_child()
        entry = item.get_item()
        
        if not isinstance(entry, FileEntry):
            label.set_text("—")
            return
            
        if entry.is_dir:
            label.set_text("—")
            label.set_tooltip_text("Folder")
        else:
            size_text = self._format_size(entry.size)
            label.set_text(size_text)
            label.set_tooltip_text(f"{entry.size:,} bytes")
            
    def _on_modified_column_setup(self, factory: Gtk.SignalListItemFactory, item) -> None:
        """Setup modified column."""
        label = Gtk.Label(xalign=0)
        label.add_css_class("dim-label")
        item.set_child(label)
        
    def _on_modified_column_bind(self, factory: Gtk.SignalListItemFactory, item) -> None:
        """Bind data to modified column."""
        label = item.get_child()
        entry = item.get_item()
        
        if not isinstance(entry, FileEntry):
            label.set_text("—")
            return
            
        try:
            dt = datetime.fromtimestamp(entry.modified)
            label.set_text(dt.strftime("%Y-%m-%d %H:%M"))
            label.set_tooltip_text(dt.strftime("%Y-%m-%d %H:%M:%S"))
        except (OSError, ValueError):
            label.set_text("—")
            label.set_tooltip_text("Unknown")
            
    def _on_grid_setup(self, factory: Gtk.SignalListItemFactory, item) -> None:
        """Setup grid item."""
        box = Gtk.Box(
            orientation=Gtk.Orientation.VERTICAL,
            spacing=8,
            halign=Gtk.Align.CENTER,
            valign=Gtk.Align.CENTER
        )
        box.set_size_request(80, 80)
        
        icon = Gtk.Image()
        icon.set_pixel_size(48)
        box.append(icon)
        
        label = Gtk.Label()
        label.set_ellipsize(Pango.EllipsizeMode.END)
        label.set_max_width_chars(12)
        label.set_justify(Gtk.Justification.CENTER)
        box.append(label)
        
        item.set_child(box)
        
    def _on_grid_bind(self, factory: Gtk.SignalListItemFactory, item) -> None:
        """Bind data to grid item."""
        box = item.get_child()
        icon = box.get_first_child()
        label = box.get_last_child()
        
        entry = item.get_item()
        if not isinstance(entry, FileEntry):
            return
            
        # Set icon
        if entry.is_dir:
            icon.set_from_icon_name("folder-symbolic")
        else:
            icon.set_from_icon_name("text-x-generic-symbolic")
            
        # Set label
        label.set_text(entry.name)
        label.set_tooltip_text(entry.name)
        
    # Selection handling
    def _on_selection_changed(self, selection_model, position, n_items) -> None:
        """Handle selection changes."""
        self._update_action_buttons()
        
    # Navigation methods
    def _on_back_clicked(self, button: Gtk.Button) -> None:
        """Handle back navigation."""
        if self._history_index > 0:
            self._history_index -= 1
            path = self._history[self._history_index]
            self._navigate_to_path(path, add_to_history=False)
            
    def _on_forward_clicked(self, button: Gtk.Button) -> None:
        """Handle forward navigation."""
        if self._history_index < len(self._history) - 1:
            self._history_index += 1
            path = self._history[self._history_index]
            self._navigate_to_path(path, add_to_history=False)
            
    def _on_up_clicked(self, button: Gtk.Button) -> None:
        """Handle up navigation."""
        if self._is_remote:
            parent = posixpath.dirname(self._current_path.rstrip('/')) or '/'
        else:
            parent = os.path.dirname(self._current_path) or '/'
            
        if parent != self._current_path:
            self._navigate_to_path(parent)
            
    def _on_refresh_clicked(self, button: Gtk.Button) -> None:
        """Handle refresh."""
        self.emit("path-changed", self._current_path)
        
    def _on_path_entry_activate(self, entry: Gtk.Entry) -> None:
        """Handle path entry activation."""
        path = entry.get_text().strip()
        if path:
            self._navigate_to_path(path)
            
    def _navigate_to_path(self, path: str, add_to_history: bool = True) -> None:
        """Navigate to specified path."""
        if add_to_history and path != self._current_path:
            # Remove forward history when navigating to new path
            if self._history_index < len(self._history) - 1:
                self._history = self._history[:self._history_index + 1]
                
            self._history.append(path)
            self._history_index = len(self._history) - 1
            
        self._current_path = path
        self._path_entry.set_text(path)
        self._update_navigation_buttons()
        self.emit("path-changed", path)
        
    def _update_navigation_buttons(self) -> None:
        """Update navigation button states."""
        self._back_button.set_sensitive(self._history_index > 0)
        self._forward_button.set_sensitive(self._history_index < len(self._history) - 1)
        
        # Up button - disable if at root
        if self._is_remote:
            at_root = self._current_path in ('/', '')
        else:
            at_root = self._current_path == os.path.dirname(self._current_path)
        self._up_button.set_sensitive(not at_root)
        
    # View management
    def _on_view_toggle(self, button: Gtk.Button) -> None:
        """Toggle between list and grid view."""
        if self._current_view == "list":
            self._current_view = "grid"
            self._view_button.set_icon_name("view-grid-symbolic")
        else:
            self._current_view = "list"
            self._view_button.set_icon_name("view-list-symbolic")
            
        self._view_stack.set_visible_child_name(self._current_view)
        
    # Sorting methods
    def _on_sort_name(self) -> None:
        """Sort by name."""
        self._sort_key = SortKey.NAME
        self._refresh_view()
        
    def _on_sort_size(self) -> None:
        """Sort by size."""
        self._sort_key = SortKey.SIZE
        self._refresh_view()
        
    def _on_sort_modified(self) -> None:
        """Sort by modified date."""
        self._sort_key = SortKey.MODIFIED
        self._refresh_view()
        
    def _on_sort_ascending(self) -> None:
        """Sort ascending."""
        self._sort_descending = False
        self._refresh_view()
        
    def _on_sort_descending(self) -> None:
        """Sort descending."""
        self._sort_descending = True
        self._refresh_view()
        
    # Context menu action handlers
    def _on_context_download(self) -> None:
        """Handle download from context menu."""
        self._on_download_clicked(None)
        
    def _on_context_upload(self) -> None:
        """Handle upload from context menu."""
        self._on_upload_clicked(None)
        
    def _on_context_rename(self) -> None:
        """Handle rename from context menu."""
        self._on_rename_clicked(None)
        
    def _on_context_delete(self) -> None:
        """Handle delete from context menu."""
        self._on_delete_clicked(None)
        
    def _on_context_new_folder(self) -> None:
        """Handle new folder from context menu."""
        self._on_new_folder_clicked(None)
        
    def _on_context_properties(self) -> None:
        """Handle properties from context menu."""
        selected = self._get_selected_entries()
        if len(selected) == 1:
            entry = selected[0]
            self._show_properties_dialog(entry)
            
    def _show_properties_dialog(self, entry: FileEntry) -> None:
        """Show properties dialog for an entry."""
        dialog = Adw.MessageDialog.new(self.get_root(), f"Properties - {entry.name}", "")
        
        # Build properties text
        props = []
        props.append(f"Name: {entry.name}")
        props.append(f"Type: {'Folder' if entry.is_dir else 'File'}")
        
        if not entry.is_dir:
            props.append(f"Size: {self._format_size(entry.size)} ({entry.size:,} bytes)")
        else:
            props.append("Size: —")
            
        try:
            dt = datetime.fromtimestamp(entry.modified)
            props.append(f"Modified: {dt.strftime('%Y-%m-%d %H:%M:%S')}")
        except (OSError, ValueError):
            props.append("Modified: Unknown")
            
        props.append(f"Location: {self._current_path}")
        
        dialog.set_body("\n".join(props))
        dialog.add_response("close", "Close")
        dialog.present()
        
    # File operations
    def _on_item_activated(self, view, position: int) -> None:
        """Handle item activation (double-click/enter)."""
        if position >= len(self._filtered_entries):
            return
            
        entry = self._filtered_entries[position]
        if entry.is_dir:
            if self._is_remote:
                new_path = posixpath.join(self._current_path, entry.name)
            else:
                new_path = os.path.join(self._current_path, entry.name)
            self._navigate_to_path(new_path)
            
    def _on_new_folder_clicked(self, button: Gtk.Button) -> None:
        """Handle new folder creation."""
        self.emit("request-operation", "mkdir", None)
        
    def _on_rename_clicked(self, button: Gtk.Button) -> None:
        """Handle rename operation."""
        selected = self._get_selected_entries()
        if len(selected) != 1:
            self._show_toast("Select exactly one item to rename")
            return
            
        payload = {"entries": selected, "directory": self._current_path}
        self.emit("request-operation", "rename", payload)
        
    def _on_delete_clicked(self, button: Gtk.Button) -> None:
        """Handle delete operation."""
        selected = self._get_selected_entries()
        if not selected:
            self._show_toast("Select items to delete")
            return
            
        payload = {"entries": selected, "directory": self._current_path}
        self.emit("request-operation", "delete", payload)
        
    def _on_upload_clicked(self, button: Gtk.Button) -> None:
        """Handle upload operation."""
        if self._partner_pane:
            selected = self._get_selected_entries()
            if not selected:
                self._show_toast("Select items to upload")
                return
                
            # Get destination from partner pane
            destination = self._partner_pane.get_current_path()
            
            # Build file paths
            paths = []
            for entry
