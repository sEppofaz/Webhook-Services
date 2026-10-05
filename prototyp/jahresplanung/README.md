# Prototyp Jahresplanung (Branch `jahresplanung`)

Lokal, nicht integriert (Josef 2026-10-05). Schreibt nie in den Live-Kalender.
Plan: `~/.claude/plans/2-ja-3-erkl-re-agile-squirrel.md`, Ideen: `PKA/Expert-Knowledge/Vereinskalender-Mehrwert-Ideen.md`.

## Starten

```bash
cd ~/Developer/vko-jahresplanung   # git worktree, Branch jahresplanung
~/.venvs/vko-jahresplanung/bin/python prototyp/jahresplanung/app.py
# → http://localhost:5050   (Strg+C beendet)
```

Die venv liegt bewusst außerhalb der Dropbox. Neu anlegen:
`python3 -m venv --system-site-packages ~/.venvs/vko-jahresplanung && ~/.venvs/vko-jahresplanung/bin/pip install fpdf2 odfpy`

Tests: `~/.venvs/vko-jahresplanung/bin/python tests/test_jahresplanung.py`

## Ablauf (ADR-026)

1. **Anmelden als Verein → Startseite „Termine“**: alle Termine des Vereins – die schon im Kalender stehen
   (Momentaufnahme, nur lesend) und die eigenen Entwürfe. Filter „ab heute“ oder Jahr.
2. **Neuer Termin**: beim Tippen von Datum/Ort erscheint die **Kollisionswarnung** (blockiert nie). Zwei Knöpfe:
   „Veröffentlichen“ oder „Als Entwurf speichern“; mehrtägig = je Tag ein Termin. Gleiche Warnung beim „Ändern“.
   Für die Jahresplanung: „Entwürfe aus dem Vorjahr erzeugen“.
3. **Einstellungen**: mit welchen Vereinen auf Überschneidungen geprüft wird – eigene Gemeinde automatisch,
   einzelne ausschließen, weitere (Nachbarn) dazunehmen. Gilt für Warnung und Markierungen.
4. **Planungsrunde**: ein Vereinsadmin startet sie (Organisator) und lädt per **Link** oder **Code** ein; die anderen
   treten angemeldet und aktiv bei, sehen alle Entwürfe mit Konflikten, ändern und **bestätigen** ihre eigenen.
5. **Jeder Verein veröffentlicht selbst** – einzeln oder alle auf einmal.
6. **Organisator schließt die Runde ab** → Ergebnis (Teilnehmer, Termine mit Status, offene Konflikte, Verlauf)
   wird eingefroren und steht als **PDF bei der Runde** im „Planungsmodul“ – für jeden beim Abschluss beteiligten
   Verein, auch nach einem Austritt. Erneut abschließen = neue Version, alte bleibt.

**Josef gibt jedes neue Konto persönlich frei** (live: App/Telegram; hier `/anmelden` → „Freigeben (VKO)“). Vorher:
Entwürfe ja, Runden und Veröffentlichen nein. Anmeldung ist simuliert (Verein wählen); Veröffentlichen setzt im
Prototyp nur einen Zeitstempel.

**Netz-Hinweis:** Banner bei Verbindungsabbruch (Formulare werden dann nicht abgeschickt, Eingaben bleiben) und bei
langsamer Verbindung; „Wird gespeichert …“ im Knopf, kein Doppel-Absenden; in der Runde alle 15 s Abfrage
„Andere Vereine haben etwas geändert – neu laden“ (pausiert im Hintergrund-Tab).

**Josef gibt jedes neue Konto persönlich frei** (live: App/Telegram; hier `/anmelden` → „Freigeben (VKO)“). Vorher:
eigene Entwürfe ja, Runden starten/beitreten nein, veröffentlichen nein.
Anmeldung ist simuliert (Verein wählen). Veröffentlichen setzt im Prototyp nur einen Zeitstempel.

## Bausteine

| Datei | Inhalt |
|---|---|
| `shared/kollision.py` | Kollisionsprüfung (Gemeinde nach Mischregel, ohne Pfarrbrief-Gottesdienste, optional Wochenende) |
| `shared/wiederholung.py` | Vorjahres-Vorlage: Feste/Ostern/Advent, Feiertage, n-ter Wochentag, Serien, Blöcke |
| `shared/export.py` | PDF, Word, Excel, LibreOffice Text/Tabelle, Kalenderdatei |
| `prototyp/jahresplanung/app.py` | Flask-Oberfläche (Kollision, Vorlage, Planungsraum, Vereins-Links) |
| `prototyp/jahresplanung/db.py` | SQLite: Konten (simuliert), Entwürfe, Planungsrunden, Teilnehmer (Schema für `vk_db.py`) |
| `prototyp/jahresplanung/daten/` | Momentaufnahme `/api/termine` + `planung.sqlite` – **nicht im Git** |

## Pitfall: Sitzungsschlüssel

Der Prototyp startet bei jeder Code-Änderung neu (Reloader). Mit `os.urandom` als Sitzungsschlüssel war danach jeder
abgemeldet, und vorher geöffnete Formulare scheiterten am CSRF-Token mit „Forbidden“ (Josef 2026-10-05: „ich sehe
nur noch Anmelden“). Jetzt: fester Schlüssel in `daten/sitzung.key` (nicht im Git), veraltete Formulare führen mit
Hinweis zurück, Fehlerseiten auf Deutsch. Live gibt es das Problem nicht (fester Schlüssel `flask_secret.key`).

## Bekannte Punkte zum Weiterbasteln

- **Gemeinsame Veranstaltungen zählen als Konflikt**: Volksfestauszug von Bergschützen, KSK und
  Weißbierzelt am selben Tag ist *ein* Fest, nicht drei kollidierende. Idee: gleicher Ort + gleiche
  Uhrzeit = „gemeinsam“, oder Knopf „gehört zusammen“.
- Datenlücke Jan–Apr 2026: Live per Upload des alten Jahresplans (KI-Import, ~2 ct, Kosten-Go) oder Excel.
- `_ics_escape`/`_ics_zeiten` sind Kopien aus `services/kalender/routes.py` – bei Integration nach `shared/`.
- Veröffentlichen schreibt im Prototyp nichts; live: `KalenderStore.update()` + Dubletten gegen Gemeinde-Import.
