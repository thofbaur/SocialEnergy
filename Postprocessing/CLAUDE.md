# Bekannte Datenfehler in den Logs

## ID1-Fehler: 48-57 als 0-9 geloggt

In den Original-Logs (`Logs/` im Studienordner, `--study-dir`, Standard
`../../Studies/DSA2026`) ist die **ID2**
eines Kontakts (der gesehene Beacon) immer korrekt.

Die **ID1** (der meldende Beacon) kann falsch sein - aber nur unter diesen
beiden Bedingungen gleichzeitig:

- Die Log-Zeile wurde **vor 2026-08-15 22:00 Uhr** aufgezeichnet - gemeint ist
  die Zeilenzeit im Log (Zeit der Auslesung), NICHT die aus dem Timer
  berechnete Kontaktzeit. Ein vor dem 15.8. 22:00 Uhr ausgelesener Kontakt kann
  eine deutlich frühere oder spätere Kontaktzeit tragen.
- Die **geloggte** ID1 liegt in **0-9**. Der Logger schrieb die Beacons 48-57
  als 0-9: eine geloggte ID1 d kann der Beacon d oder d + 48 sein. Eine
  geloggte ID1 48-57 ist immer richtig, ebenso jede ID1 außerhalb 0-9.

**Behandlung:** `run_pipeline.py` entscheidet die betroffenen Auslesungen
automatisch (`steps/id_split_analysis.py`, Postprocessing Method.md Abschnitt 15)
und schreibt Arbeitskopien der Logs mit der entschiedenen ID
(`Output/pipeline/logs/`); die Entscheidungen stehen in
`Output/pipeline/id_correction/id_split_analysis.csv`. Alles Weitere
(Uptime-Modell, `Output/data/`) beruht auf diesen Arbeitskopien. Jede
Logdatei wird nur einmal gelesen: die Originale in Schritt 1 in einen Cache
(`lib/logcache.py`, `Output/pipeline/cache/`, ein Unterordner je Logordner,
nie in einem Logordner), die Arbeitskopien entstehen aus diesem Cache mit
eigenem Cache; kein Skript liest den Text der Logs. Der Cache wird neu
geschrieben, sobald sich eine Logdatei ändert. Neuer Code, der
Logs auswertet, liest über `lib/logcache.py`. Auslesungen,
die unentschieden bleiben, behalten ihre geloggte ID; ihre Records haben in
`Output/data/` den Status `ID undecided (0-9 / 48-57)`. Handkorrigierte Logs
werden nicht verwendet.

Wer Original-Logs direkt auswertet, muss die Regel oben selbst beachten.
Zeitpunkt und Schalter stehen in `study.toml` im Studienordner (`[id_bug]`,
`enabled = false` für Studien ohne den Fehler); alle Werte der Studie stehen
dort, nicht im Code.

# Arbeiten an der Pipeline

- **Einstieg:** `python run_pipeline.py [--reports] [--study-dir <Ordner>]` aus `Postprocessing/`;
  Original-Logs -> `Output/data/` (Auswertung) und `Output/pipeline/`
  (Zwischenstände). Die Original-Logs werden nie verändert.
- **Zwei Runden:** Runde 1 baut das Uptime-Modell, die zweite ID-Aufteilung
  entscheidet weitere Auslesungen. Runde 2 läuft, wenn dabei Labels neu
  entschieden wurden oder die Contact Sync in Runde 1 Reboot-Hinweise
  (`timer_zero/reboot_hints.csv`, Regel a″) gefunden hat; die Hinweise werden
  erst in Runde 2 benutzt. `reboot_hints.csv` wird zu Beginn jedes Laufs
  gelöscht: ein Lauf hängt nur von den Eingaben im Studienordner ab.
- **Studienordner** (`lib/study.py`): `Logs/`, `list_persons.csv`,
  `list_rooms.csv`, `logger.toml` (Format des Loggers: Nachrichten und
  Log-Zeilen - der Code enthält keinen Zeilentext und benutzt keinen
  Logger-Code), `study.toml` (alle Werte der Studie: Zeiten, IDs, RSSI,
  Beacons, ID-Fehler) und optional `battery_replacements.csv`. Der Code enthält
  keine Werte einer Studie. `run_pipeline.py` schreibt einen Fingerabdruck der
  Eingaben (`Output/pipeline/inputs.json`); die Datierung verweigert eine
  Pipeline-Ausgabe aus anderen Eingaben.
- **Ablauf einer Änderung:** vorschlagen -> umsetzen -> Pipeline neu laufen
  lassen -> Ausgaben per SHA-256 mit dem Stand davor vergleichen (jede
  Abweichung erklärt oder gewollt; `uptime_model_validation_state.pkl` ändert
  sich, wenn `validate_model.py` neu fittet: nach einem neuen Modell der
  Pipeline, einer Änderung von `uptime_model.py` oder mit `--refit`) -> Doku nachziehen:
  `Postprocessing Method.md` (datiertes Protokoll, Messwerte nur dort),
  `POSTPROCESSING.md`, `../ToDo.md` (nur Postprocessing und Auswertung;
  Logger und Firmware in `BeaconNRF54/ToDo.md`), diese Datei.
- **Warnungen beachten:** `run_pipeline.py` meldet Reboot-Hinweise, die erst
  Runde 2 findet (eine dritte Runde gibt es nicht), `uptime_model.py` eine
  Iteration ohne Konvergenz der Schnittpunkte und Verzögerungen nahe
  `MAX_DELAY_S`. Die Boots selbst konvergieren nicht exakt (sie pendeln um
  wenige Sekunden, Postprocessing Method.md Abschnitt 19): eine Iteration
  wegzulassen ändert daher Boots.
- **Verlorene Records:** eine Lücke zwischen zwei Auslesungen gilt nur dann
  als `read out, not logged`, wenn in ihr eine Auslesung möglich war (die
  Base liest erst ab ihrer Schwelle aus, siehe `Contact Count`); sonst
  `not recorded (no readout possible)` - der Beacon hat nicht aufgezeichnet,
  obwohl andere ihn sahen (Abschnitt 21, Readout test).
- **Durch Reboot gelöscht** nur, wo die Partner den Beacon sahen; der Rest
  dieser Zeit ist `not seen (off or alone)`: der Beacon war aus (noch nicht
  aktiviert, unbenutzt) oder allein und hatte keine Kontakte. Dort fehlen
  auch die Records der Partner - in der Auswertung nicht als Zeit ohne
  Kontakt einer anwesenden Person zählen.
- **Ausgaben:** `Output/data/` enthält nur die Daten für die Auswertung
  (Kontakte pro Tag, Self-Reports, Eco-Sessions, Batterie getrennt;
  Coverage mit den Belegen der verlorenen Records als Spalten, Reboots,
  nicht datierte Records). Prüfungen der Datierung (Logger-Stalls, Test E,
  Both-sides check) liegen in `Output/pipeline/quality/`.
- **Veralteter Code** wird gelöscht (er bleibt in der git-Historie); kein
  Skript und keine Doku verweist auf alten Code. Keine Änderungen an Logger oder Firmware. In `Background
  Information.md` keine aus den Daten gemessenen Zahlen.
