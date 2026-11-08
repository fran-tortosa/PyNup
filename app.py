from __future__ import annotations

import ctypes
import queue
import re
import sys
import threading
import tkinter as tk
import time
import traceback
from datetime import datetime
from pathlib import Path
from threading import Event
from tkinter import filedialog, messagebox, ttk
from typing import Callable, TextIO

from pynup_gui.workflow import (
    ExtractConfig,
    NupService,
    OperationCancelled,
    PackConfig,
    ProgressUpdate,
)

APP_ROOT = Path(getattr(sys, "_MEIPASS", Path(__file__).resolve().parent))
APP_DIRECTORY = (
    Path(sys.executable).resolve().parent
    if getattr(sys, "frozen", False)
    else Path(__file__).resolve().parent
)


def _is_final_progress(update: ProgressUpdate) -> bool:
    return (
        update.percent is not None
        and update.percent >= 100
        and (update.step != 3 or update.phase == "completo")
    )


def _should_log_progress(
    update: ProgressUpdate,
    now: float,
    last_progress_step: int | None,
    last_progress_log_time: float,
) -> bool:
    return (
        update.step != last_progress_step
        or now - last_progress_log_time >= 10
        or _is_final_progress(update)
    )


class PyNupApp:
    def __init__(self, root: tk.Tk) -> None:
        self.root = root
        version_date = self._version_date()
        self.window_title = f"PyNup - Versión 1.1 - Fecha: {version_date}"
        self.root.title(self.window_title)
        icon_sizes = (16, 24, 32, 48, 64, 128, 256)
        self.window_icons = tuple(
            tk.PhotoImage(
                file=str(APP_ROOT / "assets" / f"pynup-{size}.png")
            )
            for size in icon_sizes
        )
        self.header_icon = self.window_icons[3]
        self.root.iconbitmap(str(APP_ROOT / "assets" / "pynup.ico"))
        self.root.iconphoto(True, *self.window_icons)
        self._native_icons: list[int] = []
        self.root.bind("<Destroy>", self._release_native_icons, add="+")
        self.root.after_idle(self._apply_native_window_icons)
        self.root.geometry("900x830")
        self.root.minsize(780, 740)
        self.service = NupService(APP_ROOT, APP_DIRECTORY)
        self.events: queue.Queue[tuple[str, object]] = queue.Queue()
        self.action_buttons: list[ttk.Button] = []
        self.job_controls: list[ttk.Widget] = []
        self._job_control_readonly: dict[ttk.Widget, bool] = {}
        self._job_running = False
        self._cancel_event: Event | None = None
        self._operation_log_lock = threading.Lock()
        self._operation_log_stream: TextIO | None = None
        self._zstandard_log_stream: TextIO | None = None
        self._build_log_stream: TextIO | None = None

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
        self.step_var = tk.StringVar(value="Paso 0 de 6 — Listo.")
        self.progress_detail_var = tk.StringVar(value="Esperando una operación.")
        self._build_ui()
        self.root.after(100, self._poll_events)

    def _apply_native_window_icons(self) -> None:
        if sys.platform != "win32":
            return

        user32 = ctypes.WinDLL("user32", use_last_error=True)
        load_image = user32.LoadImageW
        load_image.argtypes = (
            ctypes.c_void_p,
            ctypes.c_wchar_p,
            ctypes.c_uint,
            ctypes.c_int,
            ctypes.c_int,
            ctypes.c_uint,
        )
        load_image.restype = ctypes.c_void_p
        send_message = user32.SendMessageW
        send_message.argtypes = (
            ctypes.c_void_p,
            ctypes.c_uint,
            ctypes.c_size_t,
            ctypes.c_ssize_t,
        )
        send_message.restype = ctypes.c_ssize_t
        get_ancestor = user32.GetAncestor
        get_ancestor.argtypes = (ctypes.c_void_p, ctypes.c_uint)
        get_ancestor.restype = ctypes.c_void_p

        icon_path = str(APP_ROOT / "assets" / "pynup.ico")

        def load_icon(size: int) -> int:
            handle = load_image(None, icon_path, 1, size, size, 0x0010)
            if not handle:
                raise ctypes.WinError(ctypes.get_last_error())
            icon_handle = int(handle)
            self._native_icons.append(icon_handle)
            return icon_handle

        window_handle = get_ancestor(self.root.winfo_id(), 2)
        if not window_handle:
            raise ctypes.WinError(ctypes.get_last_error())

        small_icon = load_icon(16)
        large_icon = load_icon(256)
        send_message(window_handle, 0x0080, 0, small_icon)
        send_message(window_handle, 0x0080, 1, large_icon)

    def _release_native_icons(self, event: tk.Event) -> None:
        if event.widget is not self.root or not self._native_icons:
            return
        destroy_icon = ctypes.WinDLL("user32", use_last_error=True).DestroyIcon
        destroy_icon.argtypes = (ctypes.c_void_p,)
        destroy_icon.restype = ctypes.c_int
        for icon_handle in self._native_icons:
            destroy_icon(icon_handle)
        self._native_icons.clear()

    @staticmethod
    def _version_date() -> str:
        timestamp = (
            Path(sys.executable).stat().st_mtime
            if getattr(sys, "frozen", False)
            else datetime.now().timestamp()
        )
        return datetime.fromtimestamp(timestamp).strftime("%d/%m/%Y")

    def _build_ui(self) -> None:
        outer = ttk.Frame(self.root, padding=16)
        outer.pack(fill="both", expand=True)
        outer.columnconfigure(0, weight=1)
        outer.rowconfigure(5, weight=1)

        heading = ttk.Frame(outer)
        heading.grid(row=0, column=0, sticky="w", pady=(0, 6))
        ttk.Label(heading, image=self.header_icon).pack(
            side="left", padx=(0, 8)
        )
        ttk.Label(
            heading,
            text="PyNup",
            font=("Segoe UI", 16, "bold"),
        ).pack(side="left")
        ttk.Label(
            outer,
            text=(
                "Para empaquetar, desempaquetar y verificar juegos, updates y "
                "DLC de Nintendo Switch 1 "
                "y 2 legalmente descifrados, para la preservación y el "
                "empaquetado de contenido adquirido legalmente. Comprueba los "
                "contenedores con NUP Verify y los archivos SHA-256 con "
                "manifiestos compatibles con RapidCRC. Este proyecto no "
                "apoya a la piratería."
            ),
            wraplength=820,
        ).grid(row=1, column=0, sticky="w", pady=(0, 12))

        tabs = ttk.Notebook(outer)
        tabs.grid(row=2, column=0, sticky="ew", pady=(12, 8))
        self.pack_tab = ttk.Frame(tabs, padding=12)
        self.extract_tab = ttk.Frame(tabs, padding=12)
        self.pack_tab.columnconfigure(1, weight=1)
        self.extract_tab.columnconfigure(1, weight=1)
        tabs.add(
            self.pack_tab,
            text="Empaquetar a formato NUP + Comprobar",
        )
        tabs.add(
            self.extract_tab,
            text="Extraer a formato carpeta + Comprobar",
        )
        self.tabs = tabs
        self.job_controls.append(tabs)

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
        compression_box = ttk.Combobox(
            self.pack_tab,
            textvariable=self.compression_var,
            values=tuple(self.compression_levels),
            state="readonly",
        )
        compression_box.grid(
            row=3, column=1, columnspan=2, sticky="ew", padx=(8, 8), pady=5
        )
        self.job_controls.append(compression_box)

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
            "Manifiesto SHA-256 (opcional):",
            self.extract_manifest_var,
            self._browse_extract_manifest,
            readonly=True,
            clear_command=self._clear_extract_manifest,
        )
        ttk.Label(
            self.extract_tab,
            text=(
                "El manifiesto SHA-256 es opcional: si lo seleccionas, PyNup "
                "compara los archivos tras NUP Verify; si no, verifica el "
                "contenedor con NUP Verify y extrae sin comparación de hashes."
            ),
            wraplength=700,
        ).grid(row=3, column=0, columnspan=3, sticky="w", pady=(8, 12))
        self._add_action_button(
            self.extract_tab,
            "Extraer y comprobar",
            self._start_extract,
        ).grid(row=4, column=0, columnspan=3, sticky="ew")

        progress_frame = ttk.LabelFrame(
            outer, text="Progreso de la operación", padding=8
        )
        progress_frame.grid(row=4, column=0, sticky="ew", pady=(4, 0))
        progress_frame.columnconfigure(0, weight=1)
        ttk.Label(progress_frame, textvariable=self.step_var).grid(
            row=0, column=0, sticky="w"
        )
        self.step_progress = ttk.Progressbar(
            progress_frame, maximum=6, mode="determinate"
        )
        self.step_progress.grid(row=1, column=0, sticky="ew", pady=(3, 5))
        self.overall_progress = ttk.Progressbar(
            progress_frame, maximum=100, mode="determinate"
        )
        self.overall_progress.grid(row=2, column=0, sticky="ew", pady=(0, 4))
        ttk.Label(progress_frame, textvariable=self.progress_detail_var).grid(
            row=3, column=0, sticky="w", pady=(0, 5)
        )
        self.cancel_button = ttk.Button(
            progress_frame,
            text="Cancelar",
            command=self._cancel_job,
            state="disabled",
        )
        self.cancel_button.grid(row=4, column=0, sticky="e", pady=(6, 0))

        log_frame = ttk.LabelFrame(outer, text="Registro", padding=8)
        log_frame.grid(row=5, column=0, sticky="nsew", pady=(4, 0))
        log_frame.rowconfigure(0, weight=1)
        log_frame.columnconfigure(0, weight=1)
        self.log_text = tk.Text(log_frame, wrap="word", state="disabled", height=12)
        self.log_text.grid(row=0, column=0, sticky="nsew")
        scrollbar = ttk.Scrollbar(log_frame, command=self.log_text.yview)
        scrollbar.grid(row=0, column=1, sticky="ns")
        self.log_text.configure(yscrollcommand=scrollbar.set)
        ttk.Label(outer, textvariable=self.status_var).grid(
            row=6, column=0, sticky="w", pady=(8, 0)
        )

    def _path_row(
        self,
        parent: ttk.Frame,
        row: int,
        label: str,
        variable: tk.StringVar,
        browse_command: Callable[[], None] | None,
        readonly: bool = False,
        clear_command: Callable[[], None] | None = None,
    ) -> None:
        ttk.Label(parent, text=label).grid(row=row, column=0, sticky="w", pady=5)
        entry = ttk.Entry(parent, textvariable=variable)
        if readonly:
            entry.state(["readonly"])
        self.job_controls.append(entry)
        entry.grid(
            row=row, column=1, sticky="ew", padx=(8, 8), pady=5
        )
        if browse_command is not None:
            browse_button = ttk.Button(
                parent, text="Examinar...", command=browse_command
            )
            browse_button.grid(
                row=row, column=2, sticky="e", pady=5
            )
            self.job_controls.append(browse_button)
        if clear_command is not None:
            clear_button = ttk.Button(parent, text="Quitar CRC", command=clear_command)
            clear_button.grid(
                row=row, column=3, sticky="e", padx=(6, 0), pady=5
            )
            self.job_controls.append(clear_button)

    def _add_action_button(
        self, parent: ttk.Frame, label: str, command: Callable[[], None]
    ) -> ttk.Button:
        button = ttk.Button(parent, text=label, command=command)
        self.action_buttons.append(button)
        self.job_controls.append(button)
        return button

    def _browse_pack_source(self) -> None:
        selected = filedialog.askdirectory(
            title="Selecciona la carpeta raíz del dump NUP"
        )
        if not selected:
            return
        source = Path(selected)
        self.pack_source_var.set(str(source))
        try:
            container = self.service.container_path_for_source(source)
        except ValueError as exc:
            self.pack_container_var.set("")
            self.pack_manifest_var.set("")
            messagebox.showerror("No se pudo identificar el paquete", str(exc),
                                 parent=self.root)
            return
        self.pack_container_var.set(str(container))
        self.pack_manifest_var.set(
            str(self.service.manifest_path_for_name(container.stem))
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
            str(container.parent / container.stem)
        )
        default_manifest = self.service.manifest_path_for_name(container.stem)
        self.extract_manifest_var.set(
            str(default_manifest) if default_manifest.is_file() else ""
        )

    def _browse_extract_destination(self) -> None:
        selected = filedialog.askdirectory(
            title="Selecciona la carpeta padre para la extracción"
        )
        if selected:
            container_name = Path(self.extract_container_var.get()).stem or "Juego"
            self.extract_destination_var.set(
                str(Path(selected) / container_name)
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

    def _clear_extract_manifest(self) -> None:
        self.extract_manifest_var.set("")

    def _pack_config(self) -> PackConfig:
        source_text = self.pack_source_var.get().strip()
        if not source_text:
            raise ValueError("Selecciona la carpeta raíz del dump.")
        try:
            compression_level = self.compression_levels[self.compression_var.get()]
        except KeyError as exc:
            raise ValueError(
                "Selecciona un nivel de compresión: baja, media o alta."
            ) from exc
        source = Path(source_text)
        container = self.service.container_path_for_source(source)
        manifest = self.service.manifest_path_for_name(container.stem)
        self.pack_container_var.set(str(container))
        self.pack_manifest_var.set(str(manifest))
        return PackConfig(
            source=source,
            container=container,
            manifest=manifest,
            compression_level=compression_level,
        )

    def _extract_config(self) -> ExtractConfig:
        required = {
            "contenedor NUP": self.extract_container_var.get(),
            "carpeta de extracción": self.extract_destination_var.get(),
        }
        missing = [label for label, value in required.items() if not value.strip()]
        if missing:
            raise ValueError(f"Completa estos campos: {', '.join(missing)}.")
        return ExtractConfig(
            container=Path(required["contenedor NUP"]),
            extraction=Path(required["carpeta de extracción"]),
            manifest=(
                Path(self.extract_manifest_var.get())
                if self.extract_manifest_var.get().strip()
                else None
            ),
        )

    def _start_pack(self) -> None:
        try:
            config = self._pack_config()
        except ValueError as exc:
            messagebox.showerror("Faltan datos", str(exc), parent=self.root)
            return
        self._start_job(
            "Empaquetando y comprobando...",
            lambda cancel_event, log, progress: self.service.pack_and_check(
                config, log, progress, cancel_event
            ),
            "empaquetar",
            config.container.name,
            (
                f"Origen: {config.source}",
                f"Contenedor: {config.container}",
                f"Manifiesto SHA-256: {config.manifest}",
                f"Nivel Zstandard: {config.compression_level}",
            ),
        )

    def _start_extract(self) -> None:
        try:
            config = self._extract_config()
        except ValueError as exc:
            messagebox.showerror("Faltan datos", str(exc), parent=self.root)
            return
        self._start_job(
            "Extrayendo y comprobando...",
            lambda cancel_event, log, progress: self.service.extract_and_check(
                config, log, progress, cancel_event
            ),
            "extraer",
            config.container.name,
            (
                f"Contenedor: {config.container}",
                f"Destino: {config.extraction}",
                f"Manifiesto SHA-256: {config.manifest or '(no seleccionado)'}",
            ),
        )

    def _start_job(
        self,
        title: str,
        operation: Callable[
            [Event, Callable[[str], None], Callable[[ProgressUpdate], None]],
            None,
        ],
        operation_name: str,
        file_name: str,
        operation_details: tuple[str, ...],
    ) -> None:
        if self._job_running:
            return
        safe_name = re.sub(
            r'[<>:"/\\|?*\x00-\x1f]', "_", Path(file_name).name
        ).rstrip(" .")[:120]
        timestamp = datetime.now().strftime("%Y-%m-%d_%H-%M-%S-%f")
        log_path = self.service.log_directory / (
            f"{timestamp}_{operation_name}_{safe_name}.log"
        )
        zstandard_log_path: Path | None = None
        zstandard_log_stream: TextIO | None = None
        build_log_path: Path | None = None
        build_log_stream: TextIO | None = None
        log_stream: TextIO | None = None
        try:
            self.service.log_directory.mkdir(parents=True, exist_ok=True)
            log_stream = log_path.open("x", encoding="utf-8", newline="\n")
            if operation_name in ("empaquetar", "extraer"):
                build_directory = self.service.log_directory / "build"
                build_directory.mkdir(parents=True, exist_ok=True)
                build_path = (
                    build_directory
                    / f"{timestamp}_{operation_name}_{safe_name}.log"
                )
                build_log_path = build_path
                build_log_stream = build_path.open(
                    "x", encoding="utf-8", newline="\n"
                )
            if operation_name == "empaquetar":
                zstandard_directory = self.service.log_directory / "zstandard"
                zstandard_directory.mkdir(parents=True, exist_ok=True)
                zstandard_path = (
                    zstandard_directory
                    / f"{timestamp}_{safe_name}_zstandard.log"
                )
                zstandard_log_path = zstandard_path
                zstandard_log_stream = zstandard_path.open(
                    "x", encoding="utf-8", newline="\n"
                )
            if zstandard_log_stream is not None:
                zstandard_log_stream.write(
                    f"{datetime.now().isoformat(timespec='milliseconds')} "
                    "| Inicio del registro detallado de progreso Zstandard.\n"
                )
                zstandard_log_stream.flush()
            if build_log_stream is not None:
                build_log_stream.write(
                    f"PyNup: detalle por archivo de {operation_name}\n"
                    f"Fecha: {datetime.now().isoformat(timespec='seconds')}\n"
                    f"Archivo asociado: {file_name}\n"
                    "Los registros se escriben durante el procesamiento.\n\n"
                )
                build_log_stream.flush()
        except OSError as exc:
            if build_log_stream is not None:
                try:
                    build_log_stream.close()
                except OSError:
                    pass
            if zstandard_log_stream is not None:
                try:
                    zstandard_log_stream.close()
                except OSError:
                    pass
            if log_stream is not None:
                try:
                    log_stream.close()
                except OSError:
                    pass
            messagebox.showerror(
                "No se pudo crear el registro",
                f"No se puede iniciar la operación porque no se pudo crear "
                f"su registro en {log_path}"
                + (
                    f" o {zstandard_log_path}"
                    if zstandard_log_path is not None
                    else ""
                )
                + (
                    f" o {build_log_path}"
                    if build_log_path is not None
                    else ""
                )
                + f":\n{exc}",
                parent=self.root,
            )
            return
        if log_stream is None:
            raise RuntimeError("No se pudo abrir el registro de la operación.")
        self._operation_log_stream = log_stream
        self._zstandard_log_stream = zstandard_log_stream
        self._build_log_stream = build_log_stream

        self.log_text.configure(state="normal")
        self.log_text.delete("1.0", "end")
        self.log_text.configure(state="disabled")
        self._job_running = True
        cancel_event = Event()
        self._cancel_event = cancel_event
        self._set_job_controls_enabled(False)
        self.cancel_button.state(["!disabled"])
        self.step_progress.configure(value=0)
        self.overall_progress.configure(value=0)
        self.step_var.set("Paso 0 de 6 — Preparando...")
        self.progress_detail_var.set("Iniciando la operación.")
        self.status_var.set(title)

        def operation_log(message: str) -> None:
            self._queue_log(message)

        last_progress_log_time = 0.0
        last_progress_step: int | None = None
        last_zstandard_flush_time = 0.0
        last_build_flush_time = 0.0

        def operation_progress(update: ProgressUpdate) -> None:
            nonlocal last_progress_log_time, last_progress_step
            nonlocal last_zstandard_flush_time, last_build_flush_time
            details = [
                f"Paso {update.step}/6: {update.title}",
                (
                    f"avance={update.percent:.2f}%"
                    if update.percent is not None
                    else "avance=—"
                ),
            ]
            if update.phase:
                details.append(f"fase={update.phase}")
            if update.detail:
                details.append(update.detail)
            if update.bytes_done is not None and update.total_bytes is not None:
                details.append(
                    f"bytes={update.bytes_done}/{update.total_bytes}"
                )
            if update.stored_bytes is not None:
                details.append(f"almacenados={update.stored_bytes}")
            if update.speed_mb_s is not None:
                details.append(f"velocidad={update.speed_mb_s:.3f} MB/s")
            if update.eta_seconds is not None:
                details.append(f"ETA={update.eta_seconds:.1f}s")
            progress_message = "PROGRESO | " + " | ".join(details)
            now = time.monotonic()
            is_final = _is_final_progress(update)
            if update.step == 3 and self._zstandard_log_stream is not None:
                self._write_zstandard_progress(
                    progress_message,
                    flush=now - last_zstandard_flush_time >= 1 or is_final,
                )
                if now - last_zstandard_flush_time >= 1 or is_final:
                    last_zstandard_flush_time = now
            if (
                update.step in (3, 4, 5)
                and update.detail
                and not update.detail.startswith("Paquete ")
                and self._build_log_stream is not None
            ):
                self._write_build_progress(
                    update,
                    flush=now - last_build_flush_time >= 10 or is_final,
                )
                if now - last_build_flush_time >= 10 or is_final:
                    last_build_flush_time = now
            if _should_log_progress(
                update,
                now,
                last_progress_step,
                last_progress_log_time,
            ):
                self._write_operation_log(progress_message)
                last_progress_log_time = now
                last_progress_step = update.step

            self.events.put(("progress", update))

        def worker() -> None:
            terminal_event: tuple[str, object] = (
                "error",
                "La operación terminó de forma inesperada.",
            )
            try:
                self._write_operation_log(f"INICIO: {title}")
                self._write_operation_log(f"Archivo asociado: {file_name}")
                self._write_operation_log(f"Registro: {log_path}")
                for detail in operation_details:
                    self._write_operation_log(detail)
                if zstandard_log_path is not None:
                    self._write_operation_log(
                        f"Log detallado Zstandard: {zstandard_log_path}"
                    )
                if build_log_path is not None:
                    self._write_operation_log(
                        f"Log detallado de archivos: {build_log_path}"
                    )
                operation(cancel_event, operation_log, operation_progress)
                if cancel_event.is_set():
                    self._write_operation_log(
                        "CANCELADA: cancelación solicitada."
                    )
                    terminal_event = ("cancelled", "Operación cancelada.")
                else:
                    self._write_operation_log(
                        "FIN: operación completada correctamente."
                    )
                    terminal_event = ("success", title)
            except OperationCancelled as exc:
                try:
                    self._write_operation_log(f"CANCELADA: {exc}")
                except OSError:
                    pass
                terminal_event = ("cancelled", str(exc))
            except Exception as exc:
                try:
                    self._write_operation_log(
                        f"ERROR: {type(exc).__name__}: {exc}"
                    )
                    for line in traceback.format_exception(exc):
                        for trace_line in line.rstrip().splitlines():
                            self._write_operation_log(f"DEBUG: {trace_line}")
                except OSError as log_exc:
                    terminal_event = (
                        "error",
                        f"{exc}\nAdemás, no se pudo escribir el registro: "
                        f"{log_exc}",
                    )
                else:
                    terminal_event = ("error", str(exc))
            finally:
                zstandard_log_error: OSError | None = None
                build_log_error: OSError | None = None
                with self._operation_log_lock:
                    if zstandard_log_stream is not None:
                        try:
                            zstandard_log_stream.flush()
                        except OSError as exc:
                            zstandard_log_error = exc
                        try:
                            zstandard_log_stream.close()
                        except OSError as exc:
                            zstandard_log_error = zstandard_log_error or exc
                        self._zstandard_log_stream = None
                    if build_log_stream is not None:
                        try:
                            build_log_stream.flush()
                        except OSError as exc:
                            build_log_error = exc
                        try:
                            build_log_stream.close()
                        except OSError as exc:
                            build_log_error = build_log_error or exc
                        self._build_log_stream = None
                if zstandard_log_stream is not None:
                    try:
                        if zstandard_log_path is None:
                            raise OSError(
                                "No se definió la ruta del log de Zstandard."
                            )
                        if zstandard_log_error is not None:
                            raise zstandard_log_error
                        self.service.retain_recent_zstandard_logs(
                            zstandard_log_path,
                            self._write_operation_log,
                        )
                    except OSError as exc:
                        warning = (
                            "No se pudo guardar o rotar el log de Zstandard: "
                            f"{exc}"
                        )
                        try:
                            self._write_operation_log(f"ERROR: {warning}")
                        except OSError:
                            pass
                        self.events.put(("log", f"ERROR: {warning}"))
                if build_log_error is not None:
                    warning = (
                        f"No se pudo completar el log de archivos "
                        f"{build_log_path}: {build_log_error}"
                    )
                    try:
                        self._write_operation_log(f"ERROR: {warning}")
                    except OSError:
                        pass
                    self.events.put(("log", f"ERROR: {warning}"))
                with self._operation_log_lock:
                    try:
                        log_stream.close()
                    except OSError as exc:
                        terminal_event = (
                            "error",
                            f"No se pudo cerrar el registro: {exc}",
                        )
                    self._operation_log_stream = None
                self.events.put(terminal_event)

        threading.Thread(target=worker, daemon=True).start()

    def _set_job_controls_enabled(self, enabled: bool) -> None:
        if enabled:
            for widget, was_readonly in self._job_control_readonly.items():
                widget.state(["!disabled"])
                if was_readonly:
                    widget.state(["readonly"])
            self._job_control_readonly.clear()
            self._job_running = False
            self._cancel_event = None
            return
        self._job_control_readonly = {
            widget: "readonly" in widget.state()
            for widget in self.job_controls
        }
        for widget in self.job_controls:
            widget.state(["disabled"])

    def _cancel_job(self) -> None:
        if self._cancel_event is None or self._cancel_event.is_set():
            return
        self._cancel_event.set()
        self.cancel_button.state(["disabled"])
        self.status_var.set(
            "Cancelando... se detendrá al terminar el bloque actual."
        )
        self._queue_log("Cancelación solicitada; limpiando temporales.")

    def _queue_log(self, message: str) -> None:
        self._write_operation_log(message)
        if not message.startswith("COMANDO INTERNO:"):
            self.events.put(("log", message))

    def _write_operation_log(self, message: str) -> None:
        with self._operation_log_lock:
            if self._operation_log_stream is None:
                return
            self._operation_log_stream.write(
                f"{datetime.now().isoformat(timespec='milliseconds')} "
                f"| {message}\n"
            )
            self._operation_log_stream.flush()

    def _write_zstandard_progress(self, message: str, flush: bool) -> None:
        with self._operation_log_lock:
            if self._zstandard_log_stream is None:
                raise OSError("El registro de progreso Zstandard no está abierto.")
            self._zstandard_log_stream.write(
                f"{datetime.now().isoformat(timespec='milliseconds')} "
                f"| {message}\n"
            )
            if flush:
                self._zstandard_log_stream.flush()

    def _write_build_progress(
        self, update: ProgressUpdate, flush: bool
    ) -> None:
        with self._operation_log_lock:
            if self._build_log_stream is None:
                raise OSError("El registro detallado de archivos no está abierto.")
            action = (
                "EMPAQUETADO"
                if update.step == 3
                else "DESEMPAQUETADO"
            )
            file_name = update.detail.replace("\r", "\\r").replace(
                "\n", "\\n"
            )
            detail = (
                f"{datetime.now().isoformat(timespec='milliseconds')} "
                f"| {action} | paso={update.step} | archivo={file_name}"
            )
            if update.phase:
                detail += f" | fase={update.phase}"
            if update.bytes_done is not None and update.total_bytes is not None:
                detail += f" | bytes={update.bytes_done}/{update.total_bytes}"
            self._build_log_stream.write(detail + "\n")
            if flush:
                self._build_log_stream.flush()

    def _update_progress(self, update: ProgressUpdate) -> None:
        self.step_var.set(f"Paso {update.step} de 6 — {update.title}")
        self.step_progress.configure(value=update.step)
        percent = min(max(update.percent or 0.0, 0.0), 100.0)
        overall = ((update.step - 1) + percent / 100) / 6 * 100
        self.overall_progress.configure(value=overall)
        self.progress_detail_var.set(
            f"Avance general: {overall:.1f}%"
            + (f" — {update.detail}" if update.detail else "")
        )
        if self._cancel_event is None or not self._cancel_event.is_set():
            self.status_var.set(f"Paso {update.step} de 6 — {update.title}")

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
            elif event == "progress" and isinstance(payload, ProgressUpdate):
                self._update_progress(payload)
                self.root.update_idletasks()
            elif event == "cancelled":
                self._append_log("Operación cancelada; temporales eliminados.")
                self.status_var.set(
                    "Cancelado. Temporales eliminados; manifiestos conservados."
                )
                self._finish_job()
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
        self.cancel_button.state(["disabled"])
        self._set_job_controls_enabled(True)


def main() -> None:
    root = tk.Tk()
    PyNupApp(root)
    root.mainloop()


if __name__ == "__main__":
    main()
