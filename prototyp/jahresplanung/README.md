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

1. **Jeder Verein plant in seinem Bereich** („Entwürfe“): anlegen, aus dem Vorjahr erzeugen, ändern.
2. **Ein Vereinsadmin startet eine Planungsrunde** („Planungsrunden“) und lädt die anderen per **Link** (WhatsApp/Mail)
   oder **Code** (z. B. beim Treffen am Beamer) ein.
3. **Die anderen melden sich an und treten aktiv bei.** In der Runde sieht jeder die Entwürfe aller Teilnehmer mit den
   Konflikten, ändert seine eigenen (Mac/Handy) und **bestätigt** sie („steht so“).
4. **Jeder Verein veröffentlicht selbst** – einzeln oder alle auf einmal.

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

## Bekannte Punkte zum Weiterbasteln

- **Gemeinsame Veranstaltungen zählen als Konflikt**: Volksfestauszug von Bergschützen, KSK und
  Weißbierzelt am selben Tag ist *ein* Fest, nicht drei kollidierende. Idee: gleicher Ort + gleiche
  Uhrzeit = „gemeinsam“, oder Knopf „gehört zusammen“.
- Datenlücke Jan–Apr 2026: Live per Upload des alten Jahresplans (KI-Import, ~2 ct, Kosten-Go) oder Excel.
- `_ics_escape`/`_ics_zeiten` sind Kopien aus `services/kalender/routes.py` – bei Integration nach `shared/`.
- Veröffentlichen schreibt im Prototyp nichts; live: `KalenderStore.update()` + Dubletten gegen Gemeinde-Import.
