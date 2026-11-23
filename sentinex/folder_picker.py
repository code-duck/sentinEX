"""Windows folder picker used by the local Streamlit interface."""

from contextlib import contextmanager
import ctypes
import os
from pathlib import Path


class FolderPickerError(RuntimeError):
    """Raised when the native project-folder dialog cannot be opened."""


@contextmanager
def _per_monitor_dpi_context():
    """Create the native dialog in a DPI-aware thread without changing the server."""
    restore_context = None
    if os.name == "nt":
        try:
            user32 = ctypes.WinDLL("user32", use_last_error=True)
            set_thread_context = user32.SetThreadDpiAwarenessContext
            set_thread_context.argtypes = [ctypes.c_void_p]
            set_thread_context.restype = ctypes.c_void_p
            previous = set_thread_context(ctypes.c_void_p(-4))  # PER_MONITOR_AWARE_V2
            if previous:
                restore_context = (set_thread_context, previous)
        except (AttributeError, OSError):
            pass

    try:
        yield
    finally:
        if restore_context:
            setter, previous = restore_context
            setter(ctypes.c_void_p(previous))


def pick_project_folder(initial_directory: str = "") -> str:
    """Open a native folder selection dialog and return the selected directory."""
    try:
        import tkinter as tk
        from tkinter import filedialog
    except ImportError as error:
        raise FolderPickerError("The Windows folder picker requires Python's Tk support.") from error

    initial_path = Path(initial_directory).expanduser() if initial_directory else Path.home()
    if initial_path.is_file():
        initial_path = initial_path.parent
    if not initial_path.is_dir():
        initial_path = Path.home()

    with _per_monitor_dpi_context():
        window = None
        try:
            window = tk.Tk()
            window.withdraw()
            window.attributes("-topmost", True)
            selected = filedialog.askdirectory(
                parent=window,
                title="Select the project folder to scan",
                initialdir=str(initial_path),
                mustexist=True,
            )
            return str(Path(selected).resolve()) if selected else ""
        except tk.TclError as error:
            raise FolderPickerError(f"Could not open the folder picker: {error}") from error
        finally:
            if window is not None:
                try:
                    window.destroy()
                except tk.TclError:
                    pass
