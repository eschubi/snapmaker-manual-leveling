# snapmaker_manual_leveling.py — Snapmaker 2.0 Manual Leveling Tool (PC-Oberfläche)
"""
Snapmaker 2.0 – Manuelle Bettkalibrierung vom PC aus
=====================================================
Version: 1.0.9

Änderungen:
  1.0.9  Nachmessen auch mit CSV als Editor-Grundlage; Rastergröße im Drucker wird
         beim Anfahren selbst geprüft (M420 V), Hinweise zusätzlich im Log
  1.0.8  Klick ins angezeigte Drucker-/CSV-Gitter öffnet den Editor direkt;
         unvollständige alte Messung blockiert Editor/Anzeige nicht mehr
  1.0.7  Editor: einzelnen Punkt mit Unterlegkarte nachmessen (G1029 W fährt den
         Punkt aus dem gespeicherten Raster an – das Mesh bleibt dabei erhalten)
  1.0.6  Workspace-Offset robust lesen: M503 mit Wiederholung, Ersatz über M2000;
         Ausgaben vom Touchscreen (z. B. „SC req STOP“) stören nicht mehr
  1.0.5  Gitter-Editor: angezeigtes Mesh (Drucker oder CSV) übernehmen, jeden Punkt
         per Klick absolut oder relativ korrigieren, geändertes Gitter schreiben
  1.0.4  CSV-Messung im Raster anzeigen (auch ohne Verbindung); beim Schreiben
         aus CSV wird das Raster vor der Bestätigung angezeigt
  1.0.3  Verbindungsabbruch (z. B. Drucker neu gestartet) wird erkannt und klar
         gemeldet; gespeicherte Messung (CSV) kann ohne Neumessung geschrieben werden
  1.0.2  Mesh aus dem Drucker (M420 V) wird im Raster-Fenster grafisch angezeigt,
         inkl. Status Leveling ON/OFF; Auswertung von M420 V zentral (parse_m420v)
  1.0.1  Temperaturberichte während Heizwartezeit (M109/M190) werden als
         Lebenszeichen gewertet statt zu Zeitüberschreitung zu führen
  1.0.0  Erstversion

Bildet die manuelle Kalibrierung der Snapmaker-Firmware (BedLevelService::
DoManualLeveling / SetManualLevelingPoint / SaveAndExitLeveling) über die
USB-Schnittstelle nach – mit Tastatur/Maus statt Touchscreen.

Ablauf (entspricht der Firmware, Snapmaker2-Controller):
  1. G28                 – Referenzfahrt (aktiviert Leveling + wendet Live-Z an)
  2. G1029 P<n>          – Rastergröße setzen; Firmware meldet Startpunkt und
                           Rasterabstand ("X:<start> - <abstand>", Rohkoordinaten)
  3. M420 S0             – Leveling aus, damit Z die echte Düsenhöhe ist
  4. je Rasterpunkt      – Düse fährt direkt auf den Punkt (wie Firmware-
                           Handkalibrierung, ohne Sensor-Versatz), Benutzer senkt
                           bis die Karte leicht schleift, Z wird übernommen
  5. M421 I<i> J<j> Z<z> – Mesh-Punkte schreiben (G29 W lehnt Z>10 ab!)
  6. M420 S1             – Leveling ein
  7. M500                – dauerhaft speichern (separater, bewusster Schritt)

Voraussetzungen:
  - Python 3.8+, pyserial  (pip install pyserial)
  - USB-Kabel PC <-> Snapmaker-Controller, Luban-Verbindung vorher TRENNEN
    (der Port kann nur von einem Programm geöffnet sein)
  - Ein-Düsen-3D-Druckmodul

Tastatur:
  Pfeil hoch / runter   Z um die gewählte Schrittweite
  Bild hoch / runter    Z um 1,0 mm
  Enter                 Punkt übernehmen, nächsten Punkt anfahren
  Klick ins Raster      Punkt anfahren (zum Nachmessen)
"""

import csv
import datetime
import queue
import re
import sys
import threading
import time
import tkinter as tk
from pathlib import Path
from tkinter import filedialog, messagebox, simpledialog, ttk

try:
    import serial
    import serial.tools.list_ports
except ImportError:  # pyserial fehlt -> klare Meldung statt Traceback
    print("pyserial fehlt. Installation:  pip install pyserial")
    sys.exit(1)

APP_VERSION = "1.0.9"
BAUDRATE = 115200                   # Marlin/Configuration.h: #define BAUDRATE 115200
GRID_MIN, GRID_MAX = 2, 11          # GRID_MAX_NUM 11 in Configuration.h
OUTPUT_DIR = Path(__file__).resolve().parent   # Backups/CSV neben dem Skript

# Fallback nur falls die Firmware den Raster-Echo nicht liefert.
# Werte aus Snapmaker2-Controller, linear.cpp, MACHINE_SIZE_A250, Ein-Düse:
#   X_DEF_SIZE 230, MAGNET_X_SPAN 184, Y_DEF_SIZE 250, MAGNET_Y_SPAN 204
#   -> logischer Rasterbereich X 23..207, Y 23..227
A250_FALLBACK = {"x0": 23.0, "x1": 207.0, "y0": 23.0, "y1": 227.0}

RE_POS = re.compile(r"X:\s*(-?\d+\.?\d*)\s+Y:\s*(-?\d+\.?\d*)\s+Z:\s*(-?\d+\.?\d*)")
RE_M206 = re.compile(r"M206\s+X(-?\d+\.?\d*)\s+Y(-?\d+\.?\d*)\s+Z(-?\d+\.?\d*)")
# v1.0.6: M2000-Ausgabe, Zeile nach "workspace_offset:" -> "X: -15.50, Y: -7.00, Z: 0.00, B: 0.00"
RE_WS_M2000 = re.compile(r"X:\s*(-?\d+\.?\d*),\s*Y:\s*(-?\d+\.?\d*),\s*Z:\s*(-?\d+\.?\d*)")
# bilinear_grid_manual(): SERIAL_ECHOLNPAIR("X:", start, " - ", abstand) – nur ganze Zeile
RE_GRID_X = re.compile(r"^(?:echo:)?\s*X:\s*(-?\d+\.?\d*)\s+-\s+(-?\d+\.?\d*)\s*$")
RE_GRID_Y = re.compile(r"^(?:echo:)?\s*Y:\s*(-?\d+\.?\d*)\s+-\s+(-?\d+\.?\d*)\s*$")
# v1.0.1: Temperatur-Statuszeile während Heizwartezeit, z. B. "T:209.90 /210.00 B:49.98 /95.00 …"
RE_TEMP = re.compile(r"^(?:ok\s+)?T:\s*-?\d+\.?\d*\s*/")


def parse_m420v(lines):
    """v1.0.2: M420-V-Antwort auswerten.

    Rückgabe: (mesh {(i, j): z}, n, leveling_an oder None)
    Zeile = J (y-Index), Spalten = I (x-Index) – siehe print_2d_array() in bedlevel.cpp.
    """
    mesh, enabled, in_grid = {}, None, False
    for t in lines:
        low = t.lower()
        if "leveling on" in low:
            enabled = True
        elif "leveling off" in low:
            enabled = False
        if t.startswith("Bilinear Leveling Grid"):
            in_grid = True
            continue
        if in_grid:
            if not t or t.startswith("Subdivided"):
                in_grid = False
                continue
            parts = t.split()
            # Kopfzeile "0 1 2 3 4" überspringen: Datenwerte haben Dezimalpunkt oder "nan"
            if (parts and parts[0].isdigit() and len(parts) > 1
                    and all("." in v or "nan" in v.lower() for v in parts[1:])):
                j = int(parts[0])
                for i, v in enumerate(parts[1:]):
                    try:
                        mesh[(i, j)] = float(v)
                    except ValueError:      # "nan" o. Ä. überspringen
                        pass
    n = (max(i for i, _ in mesh) + 1) if mesh else 0
    return mesh, n, enabled


class PrinterError(Exception):
    """Fehler in der Kommunikation mit dem Controller."""


class Printer:
    """Serielle Verbindung zum Snapmaker-Controller (Marlin-Protokoll: Zeile -> ... -> ok)."""

    def __init__(self, port, log_fn):
        self.port = port
        self.log = log_fn
        self.ser = None
        self.lock = threading.Lock()

    def open(self):
        self.ser = serial.Serial(self.port, BAUDRATE, timeout=0.2)
        time.sleep(1.5)                       # Controller nach Portöffnung kurz Zeit geben
        self.ser.reset_input_buffer()
        self.cmd("M114", timeout=5)           # Lebenszeichen

    def close(self):
        if self.ser and self.ser.is_open:
            self.ser.close()
        self.ser = None

    def _drop(self):
        """v1.0.3: ungültigen Port schließen, ohne weitere Fehler zu werfen."""
        try:
            if self.ser:
                self.ser.close()
        except Exception:
            pass
        self.ser = None

    @property
    def connected(self):
        return self.ser is not None and self.ser.is_open

    def cmd(self, line, timeout=15.0):
        """Befehl senden, alle Antwortzeilen bis 'ok' sammeln."""
        if not self.connected:
            raise PrinterError("Nicht verbunden")
        with self.lock:
            self.log(f"> {line}")
            try:
                self.ser.write((line.strip() + "\n").encode("ascii"))
            except (serial.SerialException, OSError) as exc:
                # v1.0.3: Port nicht mehr gültig (Neustart, USB ab, anderes Programm)
                self._drop()
                raise PrinterError(
                    "Verbindung zum Drucker verloren – Drucker neu gestartet, USB getrennt "
                    "oder Port von Luban belegt? Bitte neu verbinden.") from exc
            lines = []
            deadline = time.time() + timeout
            while time.time() < deadline:
                try:
                    raw = self.ser.readline()
                except (serial.SerialException, OSError) as exc:
                    self._drop()                    # v1.0.3
                    raise PrinterError("Verbindung zum Drucker verloren – bitte neu verbinden.") from exc
                if not raw:
                    continue
                text = raw.decode("utf-8", errors="replace").strip()
                if not text:
                    continue
                self.log(f"  {text}")
                if text.startswith("ok"):
                    return lines
                if "busy" in text.lower():          # "echo:busy: processing" -> weiter warten
                    deadline = time.time() + timeout
                # v1.0.1: Temperaturberichte ("T:… B:… W:?") kommen, solange der Controller
                # in M109/M190 auf Temperatur wartet – Befehle stehen dann in der Warteschlange.
                # Als Lebenszeichen werten (Timeout verlängern), nicht als Antwort sammeln.
                if RE_TEMP.match(text):
                    deadline = time.time() + timeout
                    continue
                lines.append(text)
                if text.lower().startswith("error"):
                    raise PrinterError(f"{line}: {text}")
            raise PrinterError(f"Zeitüberschreitung bei '{line}'")

    def workspace_offset(self):
        """v1.0.6: Workspace-Offset (X, Y, Z) lesen.

        1. M503 (Zeile "M206 X… Y… Z…"), bis zu zweimal
        2. Ersatz: M2000 (Zeile nach "workspace_offset:")
        Hintergrund: Läuft/endet gerade ein Job am Touchscreen, kann M503 leer
        antworten, während der Controller Statusmeldungen ausgibt.
        """
        for attempt in range(2):
            for t in self.cmd("M503", timeout=10):
                m = RE_M206.search(t)
                if m:
                    return tuple(float(v) for v in m.groups())
            time.sleep(1.0)
            self.log("M206 nicht erhalten – neuer Versuch")
        lines = self.cmd("M2000", timeout=10)
        for k, t in enumerate(lines):
            if t.strip().startswith("workspace_offset") and k + 1 < len(lines):
                m = RE_WS_M2000.search(lines[k + 1])
                if m:
                    self.log("Workspace-Offset aus M2000 gelesen")
                    return tuple(float(v) for v in m.groups())
        raise PrinterError("Workspace-Offset weder über M503 noch M2000 lesbar – "
                           "läuft am Touchscreen noch ein Job? Abbruch, nichts geschrieben.")

    def position(self):
        """Logische Position (X, Y, Z) nach Abschluss aller Bewegungen."""
        self.cmd("M400", timeout=120)
        for text in self.cmd("M114"):
            m = RE_POS.search(text)
            if m:
                return tuple(float(v) for v in m.groups())
        raise PrinterError("M114 ohne Positionsangabe")


class LevelingApp:
    def __init__(self, root):
        self.root = root
        root.title(f"Snapmaker 2.0 – Manuelle Kalibrierung  v{APP_VERSION}")
        self.printer = None
        self.busy = False
        self.log_queue = queue.Queue()

        # Kalibrierzustand
        self.n = 5
        self.xs, self.ys = [], []           # logische Koordinaten der Rasterpunkte
        self.mesh = {}                      # (i, j) -> gemessenes Z (logisch)
        self.order = []                     # Messreihenfolge (Schlangenlinie)
        self.current = None                 # aktuell angefahrener Punkt (i, j)
        self.last_contact = None
        self.z_now = None
        self.ws_offset = (0.0, 0.0, 0.0)    # M206 = Workspace-Offset
        self.leveling_started = False
        # v1.0.2: zuletzt aus dem Drucker gelesenes Mesh (Anzeige außerhalb einer Messung)
        self.printer_mesh = {}
        self.printer_n = 0
        self.printer_leveling = None
        # v1.0.4: aus CSV geladene Messung und zuletzt gewählte Ansicht
        self.csv_mesh = {}
        self.csv_n = 0
        self.csv_name = ""
        self.view = None                    # "printer" | "csv" | "edit" | None
        # v1.0.5: Gitter-Editor (Werte als Rohkoordinaten wie z_values)
        self.edit_mesh = {}
        self.edit_orig = {}
        self.edit_n = 0
        self.edit_src = ""
        self.edit_logical = False
        # v1.0.7: Einzelpunkt-Nachmessung im Editor
        self.remeasure = None               # (i, j) während der Nachmessung
        self.remeasure_homed = False        # G28 in dieser Verbindung schon erfolgt

        self._build_ui()
        self._refresh_ports()
        self.root.after(100, self._drain_log)

    # ------------------------------------------------------------------ UI
    def _build_ui(self):
        top = ttk.Frame(self.root, padding=6)
        top.pack(fill="x")

        ttk.Label(top, text="Port:").grid(row=0, column=0, sticky="w")
        self.port_var = tk.StringVar()
        self.port_box = ttk.Combobox(top, textvariable=self.port_var, width=14, state="readonly")
        self.port_box.grid(row=0, column=1, padx=2)
        ttk.Button(top, text="↻", width=3, command=self._refresh_ports).grid(row=0, column=2)
        self.conn_btn = ttk.Button(top, text="Verbinden", command=self._toggle_connect)
        self.conn_btn.grid(row=0, column=3, padx=6)

        ttk.Label(top, text="Raster:").grid(row=0, column=4, sticky="e")
        self.grid_var = tk.IntVar(value=5)
        ttk.Spinbox(top, from_=GRID_MIN, to=GRID_MAX, textvariable=self.grid_var, width=4).grid(row=0, column=5)

        ttk.Label(top, text="Start-Z:").grid(row=0, column=6, sticky="e")
        self.startz_var = tk.DoubleVar(value=30.0)
        ttk.Spinbox(top, from_=5, to=100, increment=1, textvariable=self.startz_var, width=6).grid(row=0, column=7)

        ttk.Label(top, text="Vorpos. über letztem Kontakt:").grid(row=0, column=8, sticky="e")
        self.prepos_var = tk.DoubleVar(value=1.0)
        ttk.Spinbox(top, from_=0.3, to=5, increment=0.1, textvariable=self.prepos_var, width=5).grid(row=0, column=9)

        ttk.Label(top, text="Bett °C (0=aus):").grid(row=0, column=10, sticky="e")
        self.bed_var = tk.IntVar(value=0)
        ttk.Spinbox(top, from_=0, to=100, increment=5, textvariable=self.bed_var, width=5).grid(row=0, column=11)

        self.start_btn = ttk.Button(top, text="Start: G28 + Raster", command=self._start)
        self.start_btn.grid(row=0, column=12, padx=8)

        mid = ttk.Frame(self.root, padding=6)
        mid.pack(fill="both", expand=True)

        self.canvas = tk.Canvas(mid, width=460, height=460, bg="#1e1e1e", highlightthickness=0)
        self.canvas.pack(side="left", fill="both", expand=True)
        self.canvas.bind("<Button-1>", self._on_canvas_click)
        self.canvas.bind("<Configure>", lambda e: self._draw_grid())

        side = ttk.Frame(mid, padding=(10, 0))
        side.pack(side="left", fill="y")

        self.point_lbl = ttk.Label(side, text="Punkt: –", font=("Segoe UI", 11))
        self.point_lbl.pack(anchor="w")
        self.z_lbl = ttk.Label(side, text="Z = –", font=("Consolas", 26, "bold"))
        self.z_lbl.pack(anchor="w", pady=(4, 10))

        ttk.Label(side, text="Schrittweite (Pfeiltasten):").pack(anchor="w")
        self.step_var = tk.DoubleVar(value=0.05)
        steps = ttk.Frame(side)
        steps.pack(anchor="w")
        for s in (0.01, 0.02, 0.05, 0.1, 0.5, 1.0):
            ttk.Radiobutton(steps, text=f"{s:g}", value=s, variable=self.step_var).pack(side="left")

        jog = ttk.Frame(side)
        jog.pack(anchor="w", pady=8)
        ttk.Button(jog, text="▲  Z +", width=10, command=lambda: self._jog(+1)).grid(row=0, column=0, padx=2, pady=2)
        ttk.Button(jog, text="▼  Z −", width=10, command=lambda: self._jog(-1)).grid(row=1, column=0, padx=2, pady=2)
        ttk.Button(jog, text="Düse +5 mm", width=12, command=lambda: self._jog_abs(5.0)).grid(row=0, column=1, padx=2)
        ttk.Button(jog, text="Position lesen", width=12, command=self._read_pos).grid(row=1, column=1, padx=2)

        self.accept_btn = ttk.Button(side, text="✔ Übernehmen + weiter  [Enter]", command=self._accept)
        self.accept_btn.pack(anchor="w", fill="x", pady=(6, 14))

        ttk.Separator(side).pack(fill="x", pady=4)
        self.stats_lbl = ttk.Label(side, text="", justify="left")
        self.stats_lbl.pack(anchor="w", pady=4)
        ttk.Button(side, text="Mesh schreiben (M421 + M420 S1)", command=self._write_mesh).pack(anchor="w", fill="x", pady=2)
        # v1.0.3: früher gespeicherte Messung schreiben, ohne neu zu messen
        ttk.Button(side, text="Messung aus CSV schreiben…", command=self._write_from_csv).pack(anchor="w", fill="x", pady=2)
        # v1.0.4: nur ansehen, ohne Drucker
        ttk.Button(side, text="CSV anzeigen…", command=self._view_csv).pack(anchor="w", fill="x", pady=2)
        # v1.0.5: Gitter-Editor
        ttk.Separator(side).pack(fill="x", pady=6)
        ttk.Button(side, text="Angezeigtes Gitter bearbeiten", command=self._edit_start).pack(anchor="w", fill="x", pady=2)
        ttk.Button(side, text="Bearbeitetes Gitter schreiben", command=self._edit_write).pack(anchor="w", fill="x", pady=2)
        ttk.Button(side, text="Dauerhaft speichern (M500)", command=self._save_eeprom).pack(anchor="w", fill="x", pady=2)
        ttk.Button(side, text="Mesh anzeigen (M420 V)", command=self._show_mesh).pack(anchor="w", fill="x", pady=2)

        ttk.Label(side, text=(
            "Karte unter die Düse, absenken bis sie\n"
            "LEICHT schleift – nicht tiefer.\n"
            "Bei Gefahr: Netzschalter / Not-Aus."),
            foreground="#b05000").pack(anchor="w", pady=(14, 0))

        logf = ttk.Frame(self.root, padding=6)
        logf.pack(fill="both")
        self.log_txt = tk.Text(logf, height=9, font=("Consolas", 9), state="disabled")
        self.log_txt.pack(side="left", fill="both", expand=True)
        sb = ttk.Scrollbar(logf, command=self.log_txt.yview)
        sb.pack(side="right", fill="y")
        self.log_txt["yscrollcommand"] = sb.set

        self.root.bind("<Up>", lambda e: self._jog(+1))
        self.root.bind("<Down>", lambda e: self._jog(-1))
        self.root.bind("<Prior>", lambda e: self._jog_abs(+1.0))
        self.root.bind("<Next>", lambda e: self._jog_abs(-1.0))
        self.root.bind("<Return>", lambda e: self._accept())
        self.root.protocol("WM_DELETE_WINDOW", self._on_close)

    # ------------------------------------------------------- Hilfsfunktionen
    def log(self, text):
        self.log_queue.put(text)

    def _drain_log(self):
        lines = []
        while not self.log_queue.empty():
            lines.append(self.log_queue.get_nowait())
        if lines:
            self.log_txt.configure(state="normal")
            self.log_txt.insert("end", "\n".join(lines) + "\n")
            # Log auf ~3000 Zeilen begrenzen
            if int(self.log_txt.index("end-1c").split(".")[0]) > 3000:
                self.log_txt.delete("1.0", "500.0")
            self.log_txt.see("end")
            self.log_txt.configure(state="disabled")
        self.root.after(100, self._drain_log)

    def _run(self, func, done=None):
        """Aufgabe im Hintergrund ausführen; GUI bleibt bedienbar, keine Parallelbefehle."""
        if self.busy:
            return
        if not (self.printer and self.printer.connected):
            messagebox.showwarning("Nicht verbunden", "Bitte zuerst verbinden.")
            return
        self.busy = True
        self.root.config(cursor="watch")

        def worker():
            err, result = None, None
            try:
                result = func()
            except Exception as exc:            # Fehler an die GUI melden
                err = exc
                # Sicherheitsnetz: absolute Positionierung wiederherstellen
                try:
                    self.printer.cmd("G90", timeout=5)
                except Exception:
                    pass
            self.root.after(0, lambda: self._finish(err, result, done))

        threading.Thread(target=worker, daemon=True).start()

    def _finish(self, err, result, done):
        self.busy = False
        self.root.config(cursor="")
        if not (self.printer and self.printer.connected):
            self.conn_btn.config(text="Verbinden")   # v1.0.3: Zustand nach Abbruch anzeigen
        if err:
            self.log(f"FEHLER: {err}")
            messagebox.showerror("Fehler", str(err))
        elif done:
            done(result)
        self._update_labels()
        self._draw_grid()

    def _refresh_ports(self):
        ports = [p.device for p in serial.tools.list_ports.comports()]
        self.port_box["values"] = ports
        if ports and not self.port_var.get():
            self.port_var.set(ports[0])

    def _toggle_connect(self):
        if self.printer and self.printer.connected:
            self.printer.close()
            self.printer = None
            self.conn_btn.config(text="Verbinden")
            self.log("Verbindung getrennt.")
            return
        port = self.port_var.get()
        if not port:
            messagebox.showwarning("Port", "Kein serieller Port gewählt.")
            return
        try:
            self.printer = Printer(port, self.log)
            self.printer.open()
        except Exception as exc:
            self.printer = None
            messagebox.showerror("Verbindung", f"{exc}\n\nLuban-Verbindung getrennt? USB-Kabel am Controller?")
            return
        self.conn_btn.config(text="Trennen")
        self.remeasure_homed = False        # v1.0.7: nach (Neu-)Verbindung neu referenzieren
        self.log(f"Verbunden mit {port} @ {BAUDRATE}")

    # ------------------------------------------------------------- Ablauf
    def _start(self):
        n = int(self.grid_var.get())
        if not GRID_MIN <= n <= GRID_MAX:
            messagebox.showwarning("Raster", f"Rastergröße {GRID_MIN}…{GRID_MAX}")
            return
        if not messagebox.askokcancel(
                "Start",
                "Der Drucker fährt jetzt eine Referenzfahrt (G28) und setzt das Raster neu.\n"
                "Das bisherige Mesh wird dabei im Speicher überschrieben (EEPROM erst bei M500).\n\n"
                "Vorher wird das aktuelle Mesh als Backup gesichert.\n"
                "Druckplatte aufgelegt, Bereich frei?"):
            return
        start_z = float(self.startz_var.get())
        bed = int(self.bed_var.get())
        p = self.printer

        def task():
            # Backup des aktuellen Mesh (vor jeder Änderung)
            self._backup_mesh()

            # Workspace-Offset (M206) lesen – für Umrechnung roh <-> logisch
            ws = p.workspace_offset()           # v1.0.6: robust (M503 / M2000)

            if bed > 0:
                p.cmd(f"M190 S{bed}", timeout=900)  # warten bis Bett warm

            p.cmd("G28", timeout=240)
            grid_lines = p.cmd(f"G1029 P{n}", timeout=15)
            p.cmd("M420 S0")                       # Leveling aus: Z = reale Düsenhöhe
            p.cmd("G90")

            gx = gy = None
            for t in grid_lines:
                mx, my = RE_GRID_X.match(t), RE_GRID_Y.match(t)
                if mx:
                    gx = (float(mx.group(1)), float(mx.group(2)))
                if my:
                    gy = (float(my.group(1)), float(my.group(2)))

            p.cmd(f"G0 Z{start_z:.2f} F600", timeout=60)
            return ws, gx, gy

        def done(res):
            ws, gx, gy = res
            self.ws_offset = ws
            self.n = n
            if gx and gy:
                # Firmware meldet Rohkoordinaten: logisch = roh + Workspace-Offset
                self.xs = [gx[0] + ws[0] + i * gx[1] for i in range(n)]
                self.ys = [gy[0] + ws[1] + j * gy[1] for j in range(n)]
                src = "von der Firmware gemeldet"
            else:
                if not messagebox.askyesno(
                        "Raster-Koordinaten",
                        "Die Firmware hat Startpunkt/Abstand nicht gemeldet.\n"
                        "Werte aus dem Firmware-Quellcode für A250 (Ein-Düse) verwenden?\n"
                        f"X {A250_FALLBACK['x0']}…{A250_FALLBACK['x1']}, "
                        f"Y {A250_FALLBACK['y0']}…{A250_FALLBACK['y1']}"):
                    self.log("Abgebrochen – keine Raster-Koordinaten.")
                    return
                f = A250_FALLBACK
                self.xs = [f["x0"] + i * (f["x1"] - f["x0"]) / (n - 1) for i in range(n)]
                self.ys = [f["y0"] + j * (f["y1"] - f["y0"]) / (n - 1) for j in range(n)]
                src = "A250-Werte aus Quellcode"
            self.mesh = {}
            self.order = [(i if j % 2 == 0 else n - 1 - i, j) for j in range(n) for i in range(n)]
            self.current = None
            self.last_contact = None
            self.leveling_started = True
            self.log(f"Raster {n}×{n} ({src}): X {self.xs[0]:.1f}…{self.xs[-1]:.1f}, "
                     f"Y {self.ys[0]:.1f}…{self.ys[-1]:.1f}, Workspace-Offset {ws}")
            self._goto(self.order[0])

        self._run(task, done)

    def _goto(self, ij):
        i, j = ij
        x, y = self.xs[i], self.ys[j]
        prepos = float(self.prepos_var.get())
        start_z = float(self.startz_var.get())
        last = self.last_contact
        p = self.printer

        def task():
            _, _, z = p.position()
            # wie Firmware: vor dem Wechsel Z anheben (+3 mm über letztem Kontakt)
            travel = (last + 3.0) if last is not None else start_z
            if z < travel:
                p.cmd(f"G0 Z{travel:.2f} F600", timeout=60)
            p.cmd(f"G0 X{x:.2f} Y{y:.2f} F3000", timeout=60)
            if last is not None:
                p.cmd(f"G0 Z{last + prepos:.2f} F300", timeout=60)
            return p.position()

        def done(pos):
            self.current = (i, j)
            self.z_now = pos[2]

        self._run(task, done)

    def _jog(self, sign):
        self._jog_abs(sign * float(self.step_var.get()))

    def _jog_abs(self, dz):
        if not self.leveling_started and self.remeasure is None:   # v1.0.7
            return
        p = self.printer

        def task():
            p.cmd("G91")
            p.cmd(f"G0 Z{dz:.3f} F300", timeout=60)
            p.cmd("G90")
            return p.position()

        self._run(task, lambda pos: setattr(self, "z_now", pos[2]))

    def _read_pos(self):
        self._run(lambda: self.printer.position(), lambda pos: setattr(self, "z_now", pos[2]))

    def _accept(self):
        if self.remeasure is not None:          # v1.0.7: Nachmessung abschließen
            self._remeasure_accept()
            return
        if self.busy or not self.leveling_started or self.current is None or self.z_now is None:
            return
        self.mesh[self.current] = self.z_now
        self.last_contact = self.z_now
        self.log(f"Punkt I{self.current[0]} J{self.current[1]}: Z = {self.z_now:.3f}")
        missing = [ij for ij in self.order if ij not in self.mesh]
        if missing:
            # nächster Punkt in Schlangenreihenfolge nach dem aktuellen, sonst erster fehlender
            idx = self.order.index(self.current)
            nxt = next((ij for ij in self.order[idx + 1:] if ij not in self.mesh), missing[0])
            self._goto(nxt)
        else:
            self._update_labels()
            self._draw_grid()
            self._jog_abs(3.0)
            messagebox.showinfo("Fertig", "Alle Punkte gemessen.\nPrüfen, dann „Mesh schreiben“.")

    def _on_canvas_click(self, ev):
        if self.busy:
            self.log("Klick ignoriert – Drucker arbeitet noch")
            return
        # v1.0.8: Klick in Drucker-/CSV-Ansicht startet den Editor automatisch
        if self.view in ("printer", "csv") and not self._measuring():
            n = self.printer_n if self.view == "printer" else self.csv_n
            cell = self._cell_at(ev.x, ev.y, n, require_measuring=False)
            if cell:
                self._edit_start()
                if self.view == "edit":
                    self._edit_cell(cell)
            return
        # v1.0.5: im Editor Punkt korrigieren statt anfahren
        if self.view == "edit" and not self._measuring():
            cell = self._cell_at(ev.x, ev.y, self.edit_n, require_measuring=False)
            if cell:
                self._edit_cell(cell)
            return
        if not self.leveling_started:
            return
        cell = self._cell_at(ev.x, ev.y)
        if cell:
            self._goto(cell)

    # ------------------------------------------------------ Gitter-Editor (v1.0.5)
    def _edit_start(self):
        """Angezeigtes Gitter (Drucker-Mesh oder CSV) als Arbeitskopie übernehmen."""
        if self._measuring():
            # v1.0.8: unvollständige Messung verwerfen statt blockieren
            if not messagebox.askyesno(
                    "Editor", f"Laufende Messung ({len(self.mesh)}/{self.n * self.n} Punkte) verwerfen?"):
                return
            self._discard_session()
        if self.view == "csv" and self.csv_mesh:
            # CSV enthält logische Z; Umrechnung erfolgt beim Schreiben (M206 Z)
            src, n, name, logical = self.csv_mesh, self.csv_n, f"CSV {self.csv_name}", True
        elif self.printer_mesh:
            src, n, name, logical = self.printer_mesh, self.printer_n, "Drucker-Mesh", False
        else:
            messagebox.showinfo("Editor", "Zuerst „Mesh anzeigen“ oder „CSV anzeigen…“.")
            return
        self.edit_mesh = dict(src)
        self.edit_orig = dict(src)
        self.edit_n = n
        self.edit_src = name
        self.edit_logical = logical
        self.view = "edit"
        self.log(f"Editor: {n}×{n} aus {name} – Punkt anklicken zum Korrigieren")
        self._update_labels()
        self._draw_grid()

    def _edit_cell(self, ij):
        """Wert setzen: absolut (18.65) oder relativ (+0.05 / -0.03).

        v1.0.7: vorher Auswahl – mit Unterlegkarte nachmessen oder Wert eingeben.
        """
        i, j = ij
        if self.remeasure is not None:
            messagebox.showinfo("Nachmessen", "Erst die laufende Nachmessung mit Enter abschließen.")
            return
        choice = messagebox.askyesnocancel(
            f"Punkt I{i} J{j}",
            "Ja  = mit Unterlegkarte nachmessen (Drucker fährt den Punkt an)\n"
            "Nein = Wert eingeben (absolut oder relativ)")
        if choice is None:
            return
        if choice:
            self._remeasure_start(ij)
            return
        old = self.edit_mesh[ij]
        orig = self.edit_orig[ij]
        s = simpledialog.askstring(
            "Punkt korrigieren",
            f"Punkt I{i} J{j}\naktuell {old:.3f}   (ursprünglich {orig:.3f})\n\n"
            "Neuer Wert absolut (z. B. 18.65)\noder relativ (z. B. +0.05 / -0.03).\n"
            "Höherer Wert = Düse druckt dort höher.",
            parent=self.root)
        if not s:
            return
        s = s.strip().replace(",", ".")
        try:
            new = old + float(s) if s[0] in "+-" else float(s)
        except ValueError:
            messagebox.showerror("Eingabe", f"Ungültiger Wert: {s}")
            return
        if abs(new - orig) > 0.5 and not messagebox.askyesno(
                "Große Änderung",
                f"{new - orig:+.3f} mm gegenüber dem Original – wirklich setzen?"):
            return
        self.edit_mesh[ij] = round(new, 3)
        self.log(f"Editor I{i} J{j}: {old:.3f} -> {new:.3f}")
        self._update_labels()
        self._draw_grid()

    def _remeasure_start(self, ij):
        """v1.0.7: Punkt aus dem gespeicherten Raster anfahren, Leveling aus.

        G1029 W I<i> J<j> fährt (bei aktivem Leveling) auf den Rasterpunkt und
        Z 13 über dem Bett – ohne das Mesh zurückzusetzen (anders als G1029 P).
        Danach M420 S0: Marlin hält die reale Position, Z wird zur Rohhöhe.
        """
        # v1.0.9: CSV als Grundlage erlaubt; Rastergröße wird im Task am Drucker geprüft
        i, j = ij
        n_edit = self.edit_n
        home = not self.remeasure_homed
        if home and not messagebox.askokcancel(
                "Referenzfahrt", "Für das Anfahren ist zuerst eine Referenzfahrt (G28) nötig. Fortfahren?"):
            return
        p = self.printer

        def task():
            ws = p.workspace_offset()
            # v1.0.9: G1029 W nutzt das im Drucker gespeicherte Raster -> Größe muss passen
            _, n_pr, _ = parse_m420v(p.cmd("M420 V", timeout=10))
            if n_pr != n_edit:
                raise PrinterError(
                    f"Raster im Drucker {n_pr}×{n_pr}, im Editor {n_edit}×{n_edit} – "
                    "Punkt-Koordinaten passen nicht. Erst das Gitter schreiben, dann nachmessen.")
            if home:
                p.cmd("G28", timeout=240)       # aktiviert Leveling (Firmware)
            p.cmd("G90")
            p.cmd("M420 S1")                    # Anfahrt über dem Bett, nicht in Rohhöhe
            p.cmd("G0 Z15 F600", timeout=60)
            p.cmd(f"G1029 W I{i} J{j}", timeout=60)
            p.cmd("M420 S0")                    # ab jetzt Z = reale Düsenhöhe
            return ws, p.position()

        def done(res):
            ws, pos = res
            self.ws_offset = ws
            self.remeasure_homed = True
            self.remeasure = (i, j)
            self.z_now = pos[2]
            self.point_lbl.config(
                text=f"NACHMESSEN I{i} J{j}  (X {pos[0]:.1f}, Y {pos[1]:.1f}) – "
                     "absenken bis Karte leicht schleift, dann Enter")
            self.log(f"Nachmessen I{i} J{j}: Düse bei X {pos[0]:.2f} Y {pos[1]:.2f}, Z {pos[2]:.2f}")

        self._run(task, done)

    def _remeasure_accept(self):
        """v1.0.7: gemessene Höhe in den Editor übernehmen, Düse heben, Leveling ein."""
        if self.busy or self.z_now is None:
            return
        ij = self.remeasure
        raw = self.z_now - self.ws_offset[2]    # logisch -> roh (z_values)
        # v1.0.9: Editor-Werte aus CSV sind logisch, aus dem Drucker roh
        val = self.z_now if self.edit_logical else raw
        old = self.edit_mesh[ij]
        if abs(val - self.edit_orig[ij]) > 0.5 and not messagebox.askyesno(
                "Große Abweichung",
                f"Gemessen {val:.3f}, Original {self.edit_orig[ij]:.3f} "
                f"({val - self.edit_orig[ij]:+.3f} mm) – übernehmen?"):
            return
        self.edit_mesh[ij] = round(val, 3)
        self.log(f"Nachgemessen I{ij[0]} J{ij[1]}: {old:.3f} -> {val:.3f}")
        self.remeasure = None
        p = self.printer

        def task():
            p.cmd("G91")
            p.cmd("G0 Z3 F300", timeout=60)
            p.cmd("G90")
            p.cmd("M420 S1")
            return p.position()

        def done(pos):
            self.z_now = pos[2]
            self.point_lbl.config(text="Nachmessung übernommen – weiteren Punkt anklicken "
                                       "oder „Bearbeitetes Gitter schreiben“")

        self._run(task, done)

    def _edit_write(self):
        """Arbeitskopie komplett ins Mesh schreiben (G1029 P, M421 je Punkt, M420 S1)."""
        if self.view != "edit" or not self.edit_mesh:
            messagebox.showinfo("Editor", "Zuerst „Angezeigtes Gitter bearbeiten“.")
            return
        if self.remeasure is not None:          # v1.0.7
            messagebox.showinfo("Editor", "Erst die laufende Nachmessung mit Enter abschließen.")
            return
        changed = [ij for ij in self.edit_mesh if abs(self.edit_mesh[ij] - self.edit_orig[ij]) > 1e-9]
        if not messagebox.askokcancel(
                "Gitter schreiben",
                f"{self.edit_n}×{self.edit_n} Punkte schreiben, davon {len(changed)} geändert.\n"
                "(Noch ohne M500 – danach prüfen und dauerhaft speichern.)"):
            return
        mesh, n, logical = dict(self.edit_mesh), self.edit_n, self.edit_logical
        p = self.printer

        def task():
            wz = p.workspace_offset()[2]        # v1.0.6: robust (M503 / M2000)
            p.cmd(f"G1029 P{n}", timeout=15)
            p.cmd("M420 S0")
            for (i, j), z in sorted(mesh.items()):
                raw = z - wz if logical else z
                p.cmd(f"M421 I{i} J{j} Z{raw:.3f}")
            p.cmd("M420 S1")
            # Bearbeitung als CSV sichern (logische Z = roh + M206 Z)
            stamp = datetime.datetime.now().strftime("%Y%m%d_%H%M%S")
            path = OUTPUT_DIR / f"snapmaker_mesh_manual_{stamp}_edit.csv"
            with open(path, "w", newline="", encoding="utf-8") as fh:
                w = csv.writer(fh, delimiter=";")
                w.writerow(["I", "J", "X", "Y", "Z_logisch"])
                for (i, j), z in sorted(mesh.items()):
                    zl = z if logical else z + wz
                    xs = f"{self.xs[i]:.2f}" if len(self.xs) == n else ""
                    ys = f"{self.ys[j]:.2f}" if len(self.ys) == n else ""
                    w.writerow([i, j, xs, ys, f"{zl:.3f}"])
            self.log(f"Bearbeitung gesichert: {path}")
            return p.cmd("M420 V", timeout=10)

        def done(lines):
            self._take_printer_mesh(lines)
            messagebox.showinfo("Gitter geschrieben",
                                "Mesh ist aktiv (noch nicht dauerhaft).\n"
                                "Prüfen, dann „Dauerhaft speichern (M500)“.")

        self._run(task, done)

    def _write_mesh(self):
        if len(self.mesh) != self.n * self.n or not self.leveling_started:
            messagebox.showwarning("Mesh", "Noch nicht alle Punkte gemessen.")
            return
        zs = list(self.mesh.values())
        span = max(zs) - min(zs)
        if span > 2.0 and not messagebox.askyesno(
                "Große Abweichung",
                f"Spanne {span:.2f} mm ist ungewöhnlich groß – Messfehler?\nTrotzdem schreiben?"):
            return
        wz = self.ws_offset[2]
        p = self.printer

        def task():
            self._save_csv()
            for (i, j), z in sorted(self.mesh.items()):
                raw = z - wz                       # z_values sind Rohkoordinaten
                p.cmd(f"M421 I{i} J{j} Z{raw:.3f}")
            p.cmd("M420 S1")
            return p.cmd("M420 V", timeout=10)

        def done(lines):
            self._take_printer_mesh(lines)      # v1.0.2: geschriebenes Mesh anzeigen
            messagebox.showinfo(
                "Mesh geschrieben",
                "Mesh ist aktiv (noch nicht dauerhaft).\n"
                "Kontrolle: Log prüfen, dann „Dauerhaft speichern (M500)“.")

        self._run(task, done)

    def _load_csv(self):
        """v1.0.4: CSV wählen und laden -> (mesh, n, dateiname) oder None."""
        path = filedialog.askopenfilename(
            title="Messung (CSV) wählen", initialdir=str(OUTPUT_DIR),
            filetypes=[("Messwerte", "snapmaker_mesh_manual_*.csv"), ("CSV", "*.csv")])
        if not path:
            return None
        mesh = {}
        try:
            with open(path, newline="", encoding="utf-8") as fh:
                for row in csv.DictReader(fh, delimiter=";"):
                    mesh[(int(row["I"]), int(row["J"]))] = float(row["Z_logisch"])
        except (OSError, KeyError, ValueError) as exc:
            messagebox.showerror("CSV", f"Datei nicht lesbar: {exc}")
            return None
        n = int(round(len(mesh) ** 0.5))
        if n < GRID_MIN or n * n != len(mesh) or any((i, j) not in mesh for i in range(n) for j in range(n)):
            messagebox.showerror("CSV", f"Unvollständiges Raster ({len(mesh)} Punkte).")
            return None
        # im Raster anzeigen
        self.csv_mesh, self.csv_n, self.csv_name = mesh, n, Path(path).name
        self.view = "csv"
        self._update_labels()
        self._draw_grid()
        return mesh, n, Path(path).name

    def _discard_session(self):
        """v1.0.8: laufende (unvollständige) Messung verwerfen."""
        self.leveling_started = False
        self.mesh = {}
        self.current = None
        self.log("Laufende Messung verworfen.")

    def _view_csv(self):
        """v1.0.4: CSV nur anzeigen – keine Verbindung nötig, nichts wird geschrieben."""
        if self.busy:
            return
        if self._load_csv():
            self.log(f"CSV angezeigt: {self.csv_name}")

    def _write_from_csv(self):
        """v1.0.3: CSV einer früheren Messung laden und ins Mesh schreiben.

        Ablauf: G1029 P<n> (Raster + Koordinaten der Firmware setzen), M420 S0,
        M421 je Punkt, M420 S1, M420 V. Keine Bewegung, keine Referenzfahrt nötig.
        """
        if self.busy:
            return
        if not (self.printer and self.printer.connected):
            messagebox.showwarning("Nicht verbunden", "Bitte zuerst verbinden.")
            return
        loaded = self._load_csv()           # v1.0.4: lädt und zeigt das Raster an
        if not loaded:
            return
        mesh, n, name = loaded
        zs = list(mesh.values())
        if not messagebox.askokcancel(
                "Mesh schreiben",
                f"{n}×{n} Punkte aus\n{name}\n"
                f"min {min(zs):.3f}  max {max(zs):.3f}  Spanne {max(zs) - min(zs):.3f} mm\n\n"
                "Ins Mesh des Druckers schreiben (noch ohne M500)?"):
            return
        p = self.printer

        def task():
            wz = p.workspace_offset()[2]        # v1.0.6: robust (M503 / M2000)
            p.cmd(f"G1029 P{n}", timeout=15)
            p.cmd("M420 S0")
            for (i, j), z in sorted(mesh.items()):
                p.cmd(f"M421 I{i} J{j} Z{z - wz:.3f}")
            p.cmd("M420 S1")
            return p.cmd("M420 V", timeout=10)

        def done(lines):
            self._take_printer_mesh(lines)
            messagebox.showinfo("Mesh geschrieben",
                                "Mesh ist aktiv (noch nicht dauerhaft).\n"
                                "Werte im Raster prüfen, dann „Dauerhaft speichern (M500)“.")

        self._run(task, done)

    def _save_eeprom(self):
        if not messagebox.askokcancel("M500", "Aktuelle Einstellungen inkl. Mesh dauerhaft speichern?"):
            return
        self._run(lambda: self.printer.cmd("M500", timeout=15),
                  lambda _: messagebox.showinfo("Gespeichert", "M500 ausgeführt."))

    def _show_mesh(self):
        # v1.0.2: Ergebnis nicht nur loggen, sondern im Raster anzeigen
        self._run(lambda: self.printer.cmd("M420 V", timeout=10), self._take_printer_mesh)

    def _take_printer_mesh(self, lines):
        """v1.0.2: M420-V-Ergebnis übernehmen; Anzeige wählt _display_source()."""
        mesh, n, enabled = parse_m420v(lines or [])
        if not mesh:
            self.log("M420 V: kein Raster erkannt.")
            return
        self.printer_mesh, self.printer_n, self.printer_leveling = mesh, n, enabled
        self.view = "printer"                   # v1.0.4
        if self._measuring() and not self.busy and messagebox.askyesno(
                "Anzeige", "Unvollständige Messung verwerfen und Drucker-Mesh anzeigen?"):
            self._discard_session()             # v1.0.8
        state = {True: "ON", False: "OFF", None: "?"}[enabled]
        self.log(f"Mesh im Drucker: {n}×{n}, Leveling {state}")

    # -------------------------------------------------------- Dateiausgabe
    def _backup_mesh(self):
        lines = self.printer.cmd("M420 V", timeout=10)
        stamp = datetime.datetime.now().strftime("%Y%m%d_%H%M%S")
        path = OUTPUT_DIR / f"snapmaker_mesh_backup_{stamp}.txt"
        # v1.0.2: Auswertung über parse_m420v() (Zeile = J, Spalte = I)
        mesh, _, _ = parse_m420v(lines)
        restore = [f"M421 I{i} J{j} Z{z:.3f}" for (i, j), z in sorted(mesh.items())]
        with open(path, "w", encoding="utf-8") as fh:
            fh.write("; Snapmaker Mesh-Backup (M420 V)\n")
            fh.write("\n".join("; " + t for t in lines) + "\n\n")
            fh.write("; Wiederherstellen (Rastergröße muss gleich sein; danach M420 S1, M500):\n")
            fh.write("\n".join(restore) + "\n")
        self.log(f"Backup: {path}")

    def _save_csv(self):
        stamp = datetime.datetime.now().strftime("%Y%m%d_%H%M%S")
        path = OUTPUT_DIR / f"snapmaker_mesh_manual_{stamp}.csv"
        with open(path, "w", newline="", encoding="utf-8") as fh:
            w = csv.writer(fh, delimiter=";")
            w.writerow(["I", "J", "X", "Y", "Z_logisch"])
            for (i, j), z in sorted(self.mesh.items()):
                w.writerow([i, j, f"{self.xs[i]:.2f}", f"{self.ys[j]:.2f}", f"{z:.3f}"])
        self.log(f"Messwerte: {path}")

    # -------------------------------------------------------------- Anzeige
    def _measuring(self):
        """v1.0.2: True, solange eine Messung läuft und noch Punkte fehlen."""
        return self.leveling_started and len(self.mesh) < self.n * self.n

    def _display_source(self):
        """v1.0.2: (mesh, n, titel) für die Anzeige – laufende Messung hat Vorrang."""
        if self._measuring():
            return self.mesh, self.n, "Messung (diese Sitzung)"
        if self.view == "edit" and self.edit_mesh:      # v1.0.5
            return self.edit_mesh, self.edit_n, f"EDITOR ({self.edit_src}) – Punkt anklicken"
        if self.view == "csv" and self.csv_mesh:        # v1.0.4
            return self.csv_mesh, self.csv_n, f"CSV: {self.csv_name}"
        if not self.printer_mesh:
            return self.mesh, self.n, "Messung (diese Sitzung)"
        state = {True: "ON", False: "OFF", None: "?"}[self.printer_leveling]
        return self.printer_mesh, self.printer_n, f"Mesh im Drucker (M420 V) – Leveling {state}"

    def _layout(self, n=None):
        w, h = self.canvas.winfo_width(), self.canvas.winfo_height()
        size = min(w, h) - 40
        cell = size / max(n or self.n, 1)
        ox, oy = (w - size) / 2, (h - size) / 2
        return ox, oy, cell

    def _cell_at(self, px, py, n=None, require_measuring=True):
        # v1.0.5: Rastergröße und Messzwang als Parameter (für den Editor)
        n = n or self.n
        if require_measuring and (not self.xs or not self._measuring()):
            return None                         # v1.0.2: Klick nur während der Messung
        ox, oy, cell = self._layout(n)
        i = int((px - ox) // cell)
        jr = int((py - oy) // cell)
        if 0 <= i < n and 0 <= jr < n:
            return i, n - 1 - jr               # J=0 unten (vorn) darstellen
        return None

    def _draw_grid(self):
        c = self.canvas
        c.delete("all")
        mesh, n, title = self._display_source()   # v1.0.2: Sitzung oder Drucker-Mesh
        if not n or (not self.xs and not mesh):
            c.create_text(c.winfo_width() / 2, c.winfo_height() / 2, fill="#aaa",
                          text="Verbinden, dann „Start“ oder „Mesh anzeigen“", font=("Segoe UI", 12))
            return
        ox, oy, cell = self._layout(n)
        c.create_text(ox, oy - 12, anchor="w", fill="#ddd", text=title, font=("Segoe UI", 9, "bold"))
        zs = list(mesh.values())
        zmin, zmax = (min(zs), max(zs)) if zs else (0, 0)
        for j in range(n):
            for i in range(n):
                x0 = ox + i * cell
                y0 = oy + (n - 1 - j) * cell
                z = mesh.get((i, j))
                if z is None:
                    fill = "#3a3a3a"
                else:
                    t = 0.5 if zmax == zmin else (z - zmin) / (zmax - zmin)
                    # blau (tief) -> grün -> rot (hoch)
                    r = int(255 * max(0, 2 * t - 1))
                    b = int(255 * max(0, 1 - 2 * t))
                    g = int(255 * (1 - abs(2 * t - 1)))
                    fill = f"#{r:02x}{g:02x}{b:02x}"
                outline = "#ffffff" if self.current == (i, j) else "#111"
                width = 3 if self.current == (i, j) else 1
                # v1.0.5: geänderte Punkte im Editor gelb umranden
                edited = (self.view == "edit" and (i, j) in self.edit_orig and z is not None
                          and abs(z - self.edit_orig[(i, j)]) > 1e-9)
                if edited:
                    outline, width = "#ffd400", 4
                c.create_rectangle(x0 + 2, y0 + 2, x0 + cell - 2, y0 + cell - 2,
                                   fill=fill, outline=outline, width=width)
                fmt = "{:.3f}" if self.view == "edit" else "{:.2f}"   # v1.0.5: Editor 3 Stellen
                label = fmt.format(z) if z is not None else f"{i},{j}"
                if edited:                         # v1.0.5: Änderung mit anzeigen
                    label += f"\n({z - self.edit_orig[(i, j)]:+.2f})"
                c.create_text(x0 + cell / 2, y0 + cell / 2, text=label,
                              fill="#000" if z is not None else "#bbb",
                              font=("Consolas", max(7, int(cell / 6))))
        c.create_text(ox, oy + n * cell + 12, anchor="w", fill="#888",
                      text="vorn = unten (J=0)   links = X klein (I=0)", font=("Segoe UI", 8))

    def _update_labels(self):
        if self.current and self.remeasure is None and self.view != "edit" \
                and len(self.xs) > self.current[0] and len(self.ys) > self.current[1]:
            i, j = self.current
            self.point_lbl.config(
                text=f"Punkt I{i} J{j}  (X {self.xs[i]:.1f}, Y {self.ys[j]:.1f})   "
                     f"{len(self.mesh)}/{self.n * self.n} gemessen")
        if self.z_now is not None:
            self.z_lbl.config(text=f"Z = {self.z_now:7.3f}")
        mesh, _, _ = self._display_source()     # v1.0.2: Statistik zur angezeigten Quelle
        if mesh:
            zs = list(mesh.values())
            self.stats_lbl.config(text=f"min {min(zs):.3f}   max {max(zs):.3f}\n"
                                       f"Spanne {max(zs) - min(zs):.3f} mm")

    def _on_close(self):
        if self.printer:
            self.printer.close()
        self.root.destroy()


def main():
    root = tk.Tk()
    try:
        ttk.Style().theme_use("vista" if sys.platform == "win32" else "clam")
    except tk.TclError:
        pass
    LevelingApp(root)
    root.mainloop()


if __name__ == "__main__":
    main()
