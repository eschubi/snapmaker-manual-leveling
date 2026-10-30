<!-- README.md — snapmaker-manual-leveling, Projektbeschreibung -->
# snapmaker-manual-leveling

PC-Oberfläche für die **manuelle Bettkalibrierung des Snapmaker 2.0** (getestet am A250 mit Ein-Düsen-3D-Druckmodul) über USB – mit Tastatur und Maus statt Touchscreen.

Aktuelle Version: **1.1.0**

## Funktionen

- **Messung mit Unterlegkarte:** Das Tool fährt jeden Rasterpunkt an (2×2 bis 11×11), du senkst per Pfeiltasten in 0,01–1-mm-Schritten ab, bis die Karte leicht schleift, und übernimmst mit Enter.
- **Gitter-Editor:** Das Drucker-Mesh (`M420 V`) oder eine gespeicherte Messung (CSV) anzeigen und einzelne Punkte **absolut oder relativ korrigieren** oder **mit der Karte nachmessen**.
- **Ganzes Gitter verschieben:** alle Punkte um einen Offset anheben oder absenken (z. B. `+0.05`), etwa um den ersten Layer insgesamt dünner oder dicker zu machen.
- **Farbige Rasteranzeige** mit min/max/Spanne und Status Leveling ON/OFF.
- **Sicherungen:** Vor jeder Messung wird das aktuelle Mesh als Backup mit `M421`-Wiederherstellungsbefehlen gespeichert. Messungen und Bearbeitungen werden als CSV abgelegt.
- **Dauerhaft speichern** (`M500`) ist ein eigener, bewusster Schritt.

## Voraussetzungen

- Python 3.8+ mit tkinter (bei Anaconda und python.org-Installern enthalten)
- `pip install pyserial`
- USB-Kabel **Mini-USB am Controller** ↔ PC. Die USB-A-Buchse am Controller ist nur für USB-Sticks, die USB-C-förmigen Buchsen an Achsen und Modulen sind Snapmakers Modulbus und **nicht** mit dem PC verbinden.
- Luban-Verbindung vorher trennen, da nur ein Programm den COM-Port öffnen kann.
- Am Touchscreen darf **kein Job laufen** (kein Druck, kein Vorheizen).

## Start

```
python snapmaker_manual_leveling.py
```

Port wählen → **Verbinden** → **Start: G28 + Raster** (neue Messung) oder **Mesh anzeigen** (Editor).

| Taste | Wirkung |
|---|---|
| Pfeil hoch/runter | Z um die gewählte Schrittweite |
| Bild hoch/runter | Z um 1 mm |
| Enter | Punkt übernehmen, nächsten anfahren |
| Klick ins Raster | Punkt anfahren bzw. im Editor korrigieren |

**Höherer Mesh-Wert = Düse druckt dort höher.**

## Hintergrund (aus dem Firmware-Quellcode)

Grundlage ist der offizielle [Snapmaker2-Controller](https://github.com/Snapmaker/Snapmaker2-Controller):

- **Ablauf wie die Firmware-Handkalibrierung** (`BedLevelService::DoManualLeveling` / `SaveAndExitLeveling`): Leveling aus, Düse direkt auf den Rasterpunkt, gemessene Z-Höhe ist der Mesh-Wert.
- **Rasterkoordinaten** liefert die Firmware selbst nach `G1029 P<n>` (`bilinear_grid_manual()`, Rohkoordinaten). Für den A250 mit einer Düse ergibt das logisch X 23…207 und Y 23…227 (`linear.cpp`: `X_DEF_SIZE 230`, `MAGNET_X_SPAN 184`, `Y_DEF_SIZE 250`, `MAGNET_Y_SPAN 204`).
- **`M421` statt `G29 W`:** `G29 W … Z` lehnt Werte außerhalb ±10 mm ab, die Snapmaker-Mesh-Werte liegen aber typisch bei 9–20 mm. `M421 I J Z` hat diese Grenze nicht.
- **Einzelpunkt-Nachmessung** über `G1029 W I J`: fährt den gespeicherten Rasterpunkt an, ohne das Mesh zurückzusetzen (im Gegensatz zu `G1029 P`).
- **`M206 X-15.5 Y-7`** ist der Werks-Home-Offset des A250 mit Ein-Düsen-Kopf (`M_HOME_OFFSET_DEFAULT`).
- `G28` wendet den am Touchscreen eingestellten **Live-Z-Offset** an. Vor dem Messen am besten auf 0 stellen.

## Bekannte Einschränkung

Der Touchscreen meldet beim Druckstart vom Stick eventuell „keine Kalibrierungsdaten“, weil er offenbar einen eigenen Merker führt, der nur bei einer Kalibrierung über seine Oberfläche gesetzt wird. Das Mesh im Controller ist trotzdem gespeichert und aktiv. Drucken über Luban/USB umgeht die Abfrage.

## Sicherheit

Beim ersten Einsatz die Hand am Netzschalter behalten und das Log beobachten. Nutzung auf eigenes Risiko, siehe [LICENSE](LICENSE).
