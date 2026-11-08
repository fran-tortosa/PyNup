from __future__ import annotations

import io
import queue
import threading
import unittest

from app import PyNupApp, _should_log_progress
from pynup_gui.workflow import ProgressUpdate


class AppLoggingTests(unittest.TestCase):
    def setUp(self) -> None:
        self.app = PyNupApp.__new__(PyNupApp)
        self.app._operation_log_lock = threading.Lock()
        self.app._operation_log_stream = io.StringIO()
        self.app._zstandard_log_stream = io.StringIO()
        self.app._build_log_stream = io.StringIO()
        self.app.events = queue.Queue()

    def test_internal_commands_are_persisted_but_not_shown_in_gui_log(self) -> None:
        self.app._queue_log("COMANDO INTERNO: nup.pack --level 3")
        self.app._queue_log("Creando archivo NUP...")

        persisted = self.app._operation_log_stream.getvalue()
        visible = []
        while not self.app.events.empty():
            visible.append(self.app.events.get_nowait())

        self.assertIn("COMANDO INTERNO: nup.pack --level 3", persisted)
        self.assertEqual(visible, [("log", "Creando archivo NUP...")])

    def test_phase_changes_do_not_force_general_progress_log(self) -> None:
        preparing = ProgressUpdate(
            step=3,
            title="Creando archivo NUP...",
            percent=40,
            phase="preparando",
        )
        compressing = ProgressUpdate(
            step=3,
            title="Creando archivo NUP...",
            percent=45,
            phase="comprimiendo",
        )

        self.assertTrue(_should_log_progress(preparing, 1, None, 0))
        self.assertFalse(_should_log_progress(compressing, 2, 3, 1))
        self.assertTrue(_should_log_progress(compressing, 11, 3, 1))

    def test_zstandard_progress_is_written_to_dedicated_log(self) -> None:
        update = ProgressUpdate(
            step=3,
            title="Creando archivo NUP...",
            percent=50,
            phase="comprimiendo",
            detail="archivo 1 de 2",
        )

        self.app._write_zstandard_progress(
            "PROGRESO | " + update.detail,
            flush=True,
        )

        self.assertIn(
            "| PROGRESO | archivo 1 de 2\n",
            self.app._zstandard_log_stream.getvalue(),
        )

    def test_file_build_progress_is_written_to_dedicated_log(self) -> None:
        update = ProgressUpdate(
            step=4,
            title="Extrayendo archivos del contenedor",
            percent=25,
            detail="romfs/datos/archivo.bin",
        )

        self.app._write_build_progress(update, flush=True)

        self.assertIn(
            "DESEMPAQUETADO | paso=4 | archivo=romfs/datos/archivo.bin",
            self.app._build_log_stream.getvalue(),
        )


if __name__ == "__main__":
    unittest.main()
