from __future__ import annotations

import queue
import sys
import threading
import tkinter as tk
from pathlib import Path
from tkinter import filedialog, messagebox, ttk
from typing import Callable

from pynup_gui.workflow import ExtractConfig, NupService, PackConfig

APP_ROOT = Path(getattr(sys, "_MEIPASS", Path(__file__).resolve().parent))
APP_DIRECTORY = (
    Path(sys.executable).resolve().parent
    if getattr(sys, "frozen", False)
    else Path(__file__).resolve().parent
)


class PyNupApp:
    def __init__(self, root: tk.Tk) -> None:
        self.root = root
        self.root.title("PyNup — Empaquetar o extraer")
        self.root.geometry("820x700")
        self.root.minsize(720, 610)
        self.service = NupService(APP_ROOT, APP_DIRECTORY)
        self.events: queue.Queue[tuple[str, str]] = queue.Queue()
        self.action_buttons: list[ttk.Button] = []

        self.pack_source_var = tk.StringVar()
        self.pack_container_var = tk.StringVar()
        self.pack_manifest_var = tk.StringVar(
            value=str(self.service.manifest_path_for_name("Juego"))
        )
        self.compression_var = tk.StringVar(
            value="Baja — nivel 3 (rápido; recomendado para uso habitual)"
        )
        self.compression_levels = {
            "Baja — nivel 3 (rápido; recomendado para uso habitual)": 3,
            "Media — nivel 11 (más compresión y tiempo)": 11,
            "Alta — nivel 22 (máxima compresión; mucho más lento)": 22,
        }
        self.extract_container_var = tk.StringVar()
        self.extract_destination_var = tk.StringVar()
        self.extract_manifest_var = tk.StringVar()
        self.status_var = tk.StringVar(value="Listo.")

        self._build_ui()
        self.root.after(100, self._poll_events)

    def _build_ui(self) -> None:
        outer = ttk.Frame(self.root, padding=16)
        outer.pack(fill="both", expand=True)
        outer.columnconfigure(0, weight=1)
        outer.rowconfigure(4, weight=1)

        ttk.Label(
            outer,
            text="PyNup: empaquetar y comprobar con SHA-256",
            font=("Segoe UI", 16, "bold"),
        ).grid(row=0, column=0, sticky="w", pady=(0, 6))
        ttk.Label(
            outer,
            text=(
                "Genera y verifica automáticamente un manifiesto compatible "
                "con RapidCRC. No es necesario abrir RapidCRC."
            ),
            wraplength=740,
        ).grid(row=1, column=0, sticky="w", pady=(0, 12))

        tabs = ttk.Notebook(outer)
        tabs.grid(row=2, column=0, sticky="ew", pady=(12, 8))
        self.pack_tab = ttk.Frame(tabs, padding=12)
        self.extract_tab = ttk.Frame(tabs, padding=12)
        self.pack_tab.columnconfigure(1, weight=1)
        self.extract_tab.columnconfigure(1, weight=1)
        tabs.add(self.pack_tab, text="Empaquetar + comprobar")
        tabs.add(self.extract_tab, text="Extraer + comprobar")
        self.tabs = tabs

        self._path_row(
            self.pack_tab,
            0,
            "Carpeta raíz del dump NUP:",
            self.pack_source_var,
            self._browse_pack_source,
        )
        self._path_row(
            self.pack_tab,
            1,
            "Contenedor .nup (output):",
            self.pack_container_var,
            None,
            readonly=True,
        )
        self._path_row(
            self.pack_tab,
            2,
            "Manifiesto SHA-256 (crc-sha256):",
            self.pack_manifest_var,
            None,
            readonly=True,
        )

        ttk.Label(self.pack_tab, text="Compresión Zstandard:").grid(
            row=3, column=0, sticky="w", pady=5
        )
        ttk.Combobox(
            self.pack_tab,
            textvariable=self.compression_var,
            values=tuple(self.compression_levels),
            state="readonly",
        ).grid(row=3, column=1, columnspan=2, sticky="ew", padx=(8, 8), pady=5)

        ttk.Label(
            self.pack_tab,
            text=(
                "Selecciona la raíz completa del dump. Se admiten base/, "
                "updates/<versión>/ y dlc/<title-id>/<índice>/, con ExeFS, "
                "RomFS y subcarpetas anidadas. También se admite una carpeta "
                "de contenido simple o un dump solo de actualizaciones. "
                "El NUP se crea en output y el manifiesto en crc-sha256; "
                "la extracción de comprobación se elimina al acabar."
            ),
            wraplength=700,
        ).grid(row=4, column=0, columnspan=3, sticky="w", pady=(8, 12))
        self._add_action_button(
            self.pack_tab,
            "Empaquetar y comprobar todo",
            self._start_pack,
        ).grid(row=5, column=0, columnspan=3, sticky="ew")

        self._path_row(
            self.extract_tab,
            0,
            "Contenedor .nup:",
            self.extract_container_var,
            self._browse_extract_container,
        )
        self._path_row(
            self.extract_tab,
            1,
            "Extraer a carpeta nueva:",
            self.extract_destination_var,
            self._browse_extract_destination,
        )
        self._path_row(
            self.extract_tab,
            2,
            "Manifiesto SHA-256 (crc-sha256):",
            self.extract_manifest_var,
            self._browse_extract_manifest,
        )
        ttk.Label(
            self.extract_tab,
            text=(
                "Verifica el NUP, lo extrae y compara automáticamente su "
                "contenido con el manifiesto SHA-256 original."
            ),
            wraplength=700,
        ).grid(row=3, column=0, columnspan=3, sticky="w", pady=(8, 12))
        self._add_action_button(
            self.extract_tab,
            "Extraer y comprobar",
            self._start_extract,
        ).grid(row=4, column=0, columnspan=3, sticky="ew")

        log_frame = ttk.LabelFrame(outer, text="Progreso", padding=8)
        log_frame.grid(row=4, column=0, sticky="nsew", pady=(4, 0))
        log_frame.rowconfigure(0, weight=1)
        log_frame.columnconfigure(0, weight=1)
        self.log_text = tk.Text(log_frame, wrap="word", state="disabled", height=12)
        self.log_text.grid(row=0, column=0, sticky="nsew")
        scrollbar = ttk.Scrollbar(log_frame, command=self.log_text.yview)
        scrollbar.grid(row=0, column=1, sticky="ns")
        self.log_text.configure(yscrollcommand=scrollbar.set)
        ttk.Label(outer, textvariable=self.status_var).grid(
            row=5, column=0, sticky="w", pady=(8, 0)
        )

    @staticmethod
    def _path_row(
        parent: ttk.Frame,
        row: int,
        label: str,
        variable: tk.StringVar,
        browse_command: Callable[[], None] | None,
        readonly: bool = False,
    ) -> None:
        ttk.Label(parent, text=label).grid(row=row, column=0, sticky="w", pady=5)
        entry = ttk.Entry(parent, textvariable=variable)
        if readonly:
            entry.state(["readonly"])
        entry.grid(
            row=row, column=1, sticky="ew", padx=(8, 8), pady=5
        )
        if browse_command is not None:
            ttk.Button(parent, text="Examinar...", command=browse_command).grid(
                row=row, column=2, sticky="e", pady=5
            )

    def _add_action_button(
        self, parent: ttk.Frame, label: str, command: Callable[[], None]
    ) -> ttk.Button:
        button = ttk.Button(parent, text=label, command=command)
        self.action_buttons.append(button)
        return button

    def _browse_pack_source(self) -> None:
        selected = filedialog.askdirectory(
            title="Selecciona la carpeta raíz del dump NUP"
        )
        if not selected:
            return
        source = Path(selected)
        self.pack_source_var.set(str(source))
        self.pack_container_var.set(
            str(self.service.container_path_for_name(source.name))
        )
        self.pack_manifest_var.set(
            str(self.service.manifest_path_for_name(source.name))
        )

    def _browse_extract_container(self) -> None:
        selected = filedialog.askopenfilename(
            title="Selecciona un contenedor NUP",
            filetypes=(("Contenedor NUP", "*.nup"), ("Todos los archivos", "*.*")),
        )
        if not selected:
            return
        container = Path(selected)
        self.extract_container_var.set(str(container))
        self.extract_destination_var.set(
            str(self.service.output_directory / f"{container.stem}_extraido")
        )
        default_manifest = self.service.manifest_path_for_name(container.stem)
        self.extract_manifest_var.set(str(default_manifest))

    def _browse_extract_destination(self) -> None:
        selected = filedialog.askdirectory(
            title="Selecciona la carpeta padre para la extracción"
        )
        if selected:
            container_name = Path(self.extract_container_var.get()).stem or "Juego"
            self.extract_destination_var.set(
                str(Path(selected) / f"{container_name}_extraido")
            )

    def _browse_extract_manifest(self) -> None:
        try:
            self.service.manifest_directory.mkdir(parents=True, exist_ok=True)
        except OSError as exc:
            messagebox.showerror(
                "No se pudo acceder a crc-sha256",
                f"No se pudo crear {self.service.manifest_directory}:\n{exc}",
                parent=self.root,
            )
            return
        selected = filedialog.askopenfilename(
            title="Selecciona el manifiesto original SHA-256",
            initialdir=str(self.service.manifest_directory),
            filetypes=(("SHA-256", "*.sha256"), ("Todos los archivos", "*.*")),
        )
        if selected:
            self.extract_manifest_var.set(selected)

    def _pack_config(self) -> PackConfig:
        required = {
            "carpeta raíz del dump": self.pack_source_var.get(),
            "contenedor NUP": self.pack_container_var.get(),
            "manifiesto SHA-256": self.pack_manifest_var.get(),
        }
        missing = [label for label, value in required.items() if not value.strip()]
        if missing:
            raise ValueError(f"Completa estos campos: {', '.join(missing)}.")
        try:
            compression_level = self.compression_levels[self.compression_var.get()]
        except KeyError as exc:
            raise ValueError(
                "Selecciona un nivel de compresión: baja, media o alta."
            ) from exc
        return PackConfig(
            source=Path(required["carpeta raíz del dump"]),
            container=Path(required["contenedor NUP"]),
            manifest=Path(required["manifiesto SHA-256"]),
            compression_level=compression_level,
        )

    def _extract_config(self) -> ExtractConfig:
        required = {
            "contenedor NUP": self.extract_container_var.get(),
            "carpeta de extracción": self.extract_destination_var.get(),
            "manifiesto SHA-256 original": self.extract_manifest_var.get(),
        }
        missing = [label for label, value in required.items() if not value.strip()]
        if missing:
            raise ValueError(f"Completa estos campos: {', '.join(missing)}.")
        return ExtractConfig(
            container=Path(required["contenedor NUP"]),
            extraction=Path(required["carpeta de extracción"]),
            manifest=Path(required["manifiesto SHA-256 original"]),
        )

    def _start_pack(self) -> None:
        try:
            config = self._pack_config()
        except ValueError as exc:
            messagebox.showerror("Faltan datos", str(exc), parent=self.root)
            return
        self._start_job(
            "Empaquetando y comprobando...",
            lambda: self.service.pack_and_check(config, self._queue_log),
        )

    def _start_extract(self) -> None:
        try:
            config = self._extract_config()
        except ValueError as exc:
            messagebox.showerror("Faltan datos", str(exc), parent=self.root)
            return
        self._start_job(
            "Extrayendo y comprobando...",
            lambda: self.service.extract_and_check(config, self._queue_log),
        )

    def _start_job(self, title: str, operation: Callable[[], None]) -> None:
        if any(button.instate(["!disabled"]) for button in self.action_buttons):
            self.status_var.set(title)
            for button in self.action_buttons:
                button.state(["disabled"])

            def worker() -> None:
                try:
                    operation()
                except Exception as exc:
                    self.events.put(("error", str(exc)))
                else:
                    self.events.put(("success", title))

            threading.Thread(target=worker, daemon=True).start()

    def _queue_log(self, message: str) -> None:
        self.events.put(("log", message))

    def _append_log(self, message: str) -> None:
        self.log_text.configure(state="normal")
        self.log_text.insert("end", message + "\n")
        self.log_text.see("end")
        self.log_text.configure(state="disabled")

    def _poll_events(self) -> None:
        while True:
            try:
                event, payload = self.events.get_nowait()
            except queue.Empty:
                break
            if event == "log":
                self._append_log(str(payload))
            elif event == "error":
                self._append_log(f"ERROR: {payload}")
                self.status_var.set("Proceso fallido.")
                self._finish_job()
                messagebox.showerror("Proceso fallido", str(payload), parent=self.root)
            elif event == "success":
                self._append_log("Operación terminada correctamente.")
                self.status_var.set("Terminado correctamente.")
                self._finish_job()
        self.root.after(100, self._poll_events)

    def _finish_job(self) -> None:
        for button in self.action_buttons:
            button.state(["!disabled"])


def main() -> None:
    root = tk.Tk()
    PyNupApp(root)
    root.mainloop()


if __name__ == "__main__":
    main()
