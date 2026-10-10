# Vereinskalender – Claude-Kontext

## Kerninfos

- **GitHub (Source of Truth):** `https://github.com/sEppofaz/Webhook-Services`
- **Lokale Arbeitskopie:** `~/Dropbox/Apps/Claude/Vereinskalender/src/` – hier bearbeiten, dann `git push`
- **Auf Server:** `/opt/rename-webhook/` – zieht per `git pull` von GitHub
- **Credentials:** ausschließlich in `/etc/pka/secrets.env` (via EnvironmentFile im Service)
- **Deployment-SOP:** `PKA/SOPs/Vereinskalender-Deployment.md`
- **Server-Arbeitsverzeichnis sauber halten:** `git status` in `/opt/rename-webhook` muss leer sein. Am 2026-09-27 lagen dort vier Monate lang eine Debug-Zeile in `services/rename/routes.py`, eine `routes.py.bak_debug` und eine nicht eingecheckte Modusänderung – der Server lief damit teilweise mit Code, der nicht auf GitHub steht. Bereinigt per `git stash push -u` (liegt als `stash@{0}` weiter auf dem Server, mit #410 im Text), **nicht** per `rm`/`checkout` – so bleibt alles zurückholbar.

### Deployment-Flow (Mac → GitHub → Hetzner)
```bash
cd ~/Library/CloudStorage/Dropbox/Apps/Claude/Vereinskalender/src
git add . && git commit -m "Beschreibung" && git push
ssh root@89.167.104.145 "git -C /opt/rename-webhook pull && systemctl restart rename-webhook"
```

### Code vom Server ziehen (Ausnahme!)
```bash
ssh root@89.167.104.145 "git -C /opt/rename-webhook add . && git -C /opt/rename-webhook commit -m 'Hotfix' && git -C /opt/rename-webhook push"
# Dann lokal: git pull
```

---

## Kalender-Input via Dropbox

Dateien in `/Dokumente/Vereinskalender/input/` (Dropbox) werden automatisch verarbeitet:

1. Dropbox-Webhook triggert → webhook.py lädt Datei herunter
2. Claude Vision extrahiert Termine
3. Telegram-Nachricht mit Vorschau + Inline-Buttons **✅ Importieren / ❌ Verwerfen**
4. Datei wird sofort nach Extraktion nach `/Dokumente/Vereinskalender/verarbeitet/` verschoben
5. Bei ✅: Termine landen in `vereinstermine.json`; Telegram-Bestätigung mit Statistik
6. Bei ❌: Vorschau wird verworfen; Datei bleibt in `verarbeitet/`

**Pending-Store:** In-Memory (`_kalender_pending`, key = 8-stellige UUID). Bei Server-Neustart gehen offene Bestätigungen verloren → Datei erneut in `input/` legen.
**Cursor:** `/opt/rename-webhook/kalender_input_cursor.txt`
**Erlaubte Dateitypen:** PDF, JPG, PNG, HEIC, TIFF, WEBP, BMP, TXT, RTF

---

## Termin-Import Gemeinden (heimat_import.py + termin_scraper.py, seit v1.38)

**Scripts:** `/opt/rename-webhook/heimat_import.py` (Lauf, Pending, Import), `termin_scraper.py` (Abrufwege ohne heimat-info)
**Gemeinden-Konfiguration:** `/opt/rename-webhook/heimat_gemeinden.json` – **Laufzeitdatei, seit v1.38 nicht mehr in Git** (`.gitignore`), Sicherung `/root/heimat_gemeinden.json.bak-2026-10-04`
**Log:** `/var/log/pka-heimat.log` · **Pending:** `/opt/rename-webhook/imports/heimat_pending_*.json` · **Status Admin-Trigger:** `imports/letzter_lauf.json`
**Tests:** `python3 tests/test_scraper.py` (offline, Fixtures `tests/fixtures/scraper/`, KI per Attrappe) · **ADR-020**

### Abrufwege (Feld `typ` je Gemeinde)
| `typ` | Quelle | Felder |
|---|---|---|
| `heimat` (oder fehlt) | heimat-info Export-API | `c_id` |
| `html` | Parser für bekannten Seiten-Baukasten (`termin_scraper.PARSER`) | `parser` (z. B. `event_overview` = Mallersdorf-Pfaffenberg) |
| `jsonld` | schema.org-Events im Quelltext | – |
| `ical` | iCal-Export | `ical_url` |
| `ki` | Claude liest den Seitentext (`KI_MODELL = claude-haiku-4-5`, ~2 ct/Abruf) | – |

Weitere Felder: `name`, `label` (`Veranstaltungen <Name>`), `verein_key` (Sammel-Key für Termine ohne Veranstalter), `url`, `landkreis`, `gemeinde` (amtlich, z. B. „Markt Mallersdorf-Pfaffenberg"), `plz`.

**Neue URL** (Admin „Starten" oder `heimat_import.py --add <url>`): 1. Parser/JSON-LD/iCal ohne Browser (JSON-LD/iCal erst ab 3 Treffern) → 2. heimat-info per Playwright → 3. KI-Rückfall: Telegram an Josef (Kosten, Anzahl) + PKA-Todo „Parser bauen" (`shared/pka_todos.py`). Findet auch die KI nichts, wird nichts gespeichert. Gemeinde/Landkreis über PLZ im Seitentext + Abgleich mit Titel/Domain (`gemeinde_aus_seite`, `plz_gemeinden.json`); VG-Sammelseiten über den URL-Pfad, aber nur wenn eine Seiten-PLZ im selben Landkreis liegt.
**Wochenlauf** (Mi 07:00): alle Gemeinden je nach `typ`. `ki`-Gemeinden werden zuerst gegen die Parser geprüft und **wechseln automatisch**, sobald einer passt (Telegram-Hinweis). Kostenbremse `MAX_KI_PRO_LAUF = 5`.
**Neuen Baukasten abfangen:** Parser-Funktion in `termin_scraper.py`, Eintrag in `PARSER` (Signatur + Blätter-Funktion), Fixture + Test ergänzen – mehr nicht.

### Zuordnung + Duplikate
- Verein = `_slugify(Veranstalter)` (über `_heimat_aliases` aufgelöst), sonst Sammel-Key der Gemeinde. Sitzungstitel ohne Veranstalter (`_SITZUNG_TITEL`) → amtliche Gemeinde. Veranstalter `Gemeinde/Markt/Stadt/VG/Rathaus…` → `_rubrik = "Gemeinde"` (wird in `_meta.rubrik` gesetzt, wenn leer).
- **Namenslisten** als Veranstalter („Werner K., Helmut H., …") werden geleert (`termin_scraper.ist_namensliste`) – nicht als Verein veröffentlichen (Josef 2026-10-04). Eine einzelne Person bleibt.
- Duplikat = gleiches Datum + Uhrzeit + gleicher/enthaltener Titel (≥ 6 Zeichen), gegen den ganzen Kalender inkl. gelöschter Termine (verworfene kommen nicht wieder). Geprüft beim Abruf (`_neu`) **und** beim Bestätigen.
- `do_import(uid, vereine, excluded_events, geo)` läuft komplett im Lock von `KalenderStore.update()`; Geo-Angaben aus der Admin-Ansicht gelten nur für tatsächlich übernommene Vereine, leere Felder ändern nichts. `uhrzeit_bis` wird übernommen. Fehlende Pending-Datei → `FileNotFoundError` (Admin 404, Telegram „fehlgeschlagen").
- Admin-Ansicht zeigt nur Vereine mit neuen Terminen (Rest: „N Vereine ohne neue Termine ausgeblendet"); bleiben nach Teilbestätigung nur Duplikate übrig, wird die Pending-Datei gelöscht.

### Pitfalls
- **`heimat_gemeinden.json` nicht wieder einchecken.** Beim Herausnehmen aus Git (v1.38) hat der nächste `git pull` die Datei auf dem Server gelöscht (Pull einer Löschung entfernt die Arbeitskopie) – aus `git show 0d4d3e3:heimat_gemeinden.json` wiederhergestellt.
- **`imports/` muss `webhook` gehören** (`chown -R webhook:webhook`). Bis 2026-10-04 war es `root:root 755` (vom Cron angelegt): der Admin-Trigger konnte keine Pending-Datei schreiben, und das Löschen nach dem Bestätigen scheiterte still (`except OSError: pass`).
- **Playwright-Browser liegen pro Benutzer** (`$HOME/.cache/ms-playwright`). Der Service (`webhook`, HOME `/opt/rename-webhook`) braucht eigene: `sudo -u webhook HOME=/opt/rename-webhook /opt/rename-webhook/bin/python3 -m playwright install chromium`. Nach jedem Playwright-Update nötig (Vorfall 2026-10-04: Discovery brach mit „Executable doesn't exist" ab, root hatte die Browser, webhook nicht).
- **Export-API:** max. `pageSize=50`, Paginierung `pageIndex`; ohne `Origin: https://www.heimat-info.de` HTTP 400; `startDate` auf `T00:00:00Z` = ganztägig.
- **Borlabs Cookie:** `discover_c_id()` versucht zuerst Base64-Decode, fällt auf Playwright-Intercept zurück.
- **Baukasten `event_overview`:** „bis Folgetag 0:00 Uhr" = offenes Ende, „0:00 Uhr" = ganztägig; Jahr fehlt in der Liste (aus Monatsfolge abgeleitet).
- **Einzelne URL per Code:** `fetch_and_save_pending_for_url("https://...")`; `--run` gibt es nicht.
- **Selbstverwaltende Vereine** werden normal dedupliziert und mit `_sv` markiert (ADR-003).
- **Verdacht + Abruf-Schalter (v1.66, ADR-027):** Selbst pflegender Verein mit Termin am selben Tag → `_verdacht` (Text des vorhandenen Termins). Telegram-„importieren“ ruft `do_import(uid, mit_verdacht=False)` – Verdachtsfälle bleiben im Pending, Entscheidung nur im Admin (dort nicht vorangehakt). `_meta[key].crawler_aus` (Schalter im Vereins-Dashboard, `POST /verein/crawler`): Wochenlauf verwirft die Termine schon beim Abruf (`_aus`), `do_import()` prüft beim Bestätigen erneut. Admin → Accounts zeigt je Konto `pflege` aus `/api/admin/users` (`_pflege_je_verein()` in `services/auth/routes.py`). **Neuer Aufrufer von `do_import()`, der nichts einzeln abhaken kann ⇒ `mit_verdacht=False`.**
- **`heimatort_gespeichert`/`gemeinde_vorschlag`/`landkreis_vorschlag`** in der Pending-Übersicht: aus `_meta`, sonst aus der Gemeinde-Konfiguration – Vorbelegung der Admin-Felder.
- **Neue Gemeinde ⇒ Ortschaften in `orte.json`** (Geo-Register, siehe unten) – die Import-Meldung erinnert daran.
- **Heimatort neuer Vereine (seit v1.41):** `shared.geo.ortschaft_aus_name(label, gemeinde)` – Ortschaft aus dem Vereinsnamen, wenn sie zur Gemeinde gehört und eindeutig ist (auch Adjektiv „Oberlindharther“); sonst wie bisher der Gemeindename. Am 2026-10-04 auf 9 Mallersdorfer Vereine angewandt (Backup `/root/vereinstermine.json.bak-2026-10-04-heimatorte`).

---

## Cron-Jobs (Vereinskalender-relevant)

**Überwachung (seit 2026-10-02, ADR-015):** Alle hier genannten Jobs laufen über `cronwrap.py NAME -- <Befehl>` und schreiben einen Heartbeat nach `/var/lib/pka-cron/NAME.json`. `cron_watchdog.py` (alle 10 Min, `/etc/cron.d/pka-cron-watchdog`, Quelle `deploy/cron.d/`) bewertet sie gegen `cron_registry.json` und meldet überfällige, fehlgeschlagene und hängende Jobs per Telegram; Lebenszeichen täglich 08:05. Ein Job steht ab seinem ersten Wrapper-Lauf automatisch unter Aufsicht. Bei Intervallen ≤ 30 Min alarmiert erst der zweite Fehlschlag in Folge. **Neuer Cronjob ⇒ in `cron_registry.json` eintragen und mit `cronwrap.py` starten** (`PKA/BKM/Neuer-Server-Service.md`, Punkt 12). Offline testen: `python3 tests/test_cronwatch.py`, Trockenlauf auf dem Server: `cron_watchdog.py --dry-run`. Nicht erfasst: systemd-Timer (`newsletter-fetch.timer`), `update_geoip.sh` (monatlich). **Dienste (seit 2026-10-04):** Abschnitt `dienste` in `cron_registry.json` – 11 Dauer-Dienste (nginx + alle App-Services, ohne `claude-code`) per `systemctl is-active`; bei „nicht aktiv“ wird nach 20 s einmal nachgefasst (Deploy-Neustart). Optional `selbsttest`: JSON-Datei `{name, zeit, ok, fehler[]}`, die der Dienst beim Start schreibt (bisher nur `kargl-invoice` → `/opt/kargl-invoice/zugferd_selftest.json`). Alarmtext enthält den `journalctl`-Befehl. Bewertungs-Schlüssel `dienst:<name>`, Lebenszeichen zählt Jobs und Dienste getrennt. **Seit 2026-10-04 (2. Ausbau) zusätzlich:** `timer` (5 systemd-Timer: aktiv, letzter Lauf `success`, Auslöser nicht älter als `max_alter_min`), `urls` (13 öffentliche Seiten, erwarteter Status ohne Redirect-Folgen, 10 s nachfassen), Zertifikate unter `/etc/letsencrypt/live/*/cert.pem` automatisch (< 14 Tage → Alarm), Platte `/` ab 85 % (`grenzwerte`). Optional `log` je Dienst für Dienste ohne Journal (life-doku). Selbsttests: kargl-invoice (ZUGFeRD), life-doku (Audio). **Abgleich (seit 2026-10-04):** Bei jedem Lauf vergleicht der Wächter eigene systemd-Units (Unit-Datei unter `/etc/systemd/system`: laufende Services, alle Timer) und nginx-Prefix-Locations (`nginx -T`, Pfade mit `/` am Ende) mit der Registry. Was weder eingetragen noch unter `ignoriert` steht (Muster mit `*`), meldet er einmal als „🆕 … ist nicht überwacht“ (Erinnerung nach 24 h, nach dem Eintragen still abgeräumt). Takt `monatstage` für Monatsjobs. **Pitfall:** `systemctl show --timestamp=unix` wirkt bei `show` nicht – Zeiten kommen als „Sun 2026-10-04 09:00:02 CEST“, `_systemd_zeit()` parst Datum+Uhrzeit (fiel im Trockenlauf auf: alle Timer „nie gelaufen“). Sicherung der Zeilen vor der Umstellung: `/root/vor-cronwrap-20261002/`.

Alle Jobs als `root`-Crontab. Timezone: `Europe/Berlin`. Logs: `/var/log/pka-*.log` – Rotation seit 2026-07-02 via `/etc/logrotate.d/pka` (weekly, 8 Rotationen, `su root root` nötig wegen `syslog`-Gruppenrechten auf `/var/log`).

| Zeit | Script | Beschreibung |
|------|--------|--------------|
| täglich 06:30 | `logbuch_summary.py` | Logbuch-Eintrag per Telegram (nicht im Repo) |
| täglich 18:00 | `event_reminder.py` | Erinnerung morgige Gottesdienste + Vereinstermine |
| täglich 18:00 | `kalender_erinnerung.py` | Telegram-Erinnerungen für Bot-Abonnenten |

**Die beiden 18:00-Jobs sind nicht dasselbe und nutzen verschiedene Bots** – bei „warum kam
die Meldung über Bot X und nicht Y" zuerst hier nachsehen:

| | `event_reminder.py` | `kalender_erinnerung.py` |
|---|---|---|
| Bot | `secrets["TOKEN"]` → **sEpp-hetzner-bot** (nur Josef) | `KALENDER_BOT_TOKEN` → **Veranstaltungen** (Abonnenten) |
| Liest | `gottesdienste.json` **und** `vereinstermine.json` | **nur** `vereinstermine.json` |

Gottesdienste aus `gottesdienste.json` erreichen die Abonnenten also **nicht**. Am
2026-09-30 war das ein Glücksfall: 23 erfundene Messen gingen nur an Josef.

| täglich 00:10, 20:00 | `kalender_report.py` | Vereinskalender-Bericht (verifiziert, DE); 20:00 mit „Offen im Admin“ (v1.62) |
| Mo 08:00 | `mail_lebenszeichen.py` | Testmail an Vereinskalender@icloud.com über Brevo – hält den SMTP-Schlüssel aktiv; Fehlschlag → Telegram (`_send`) + Exit 1 (Cron-Wächter). `/etc/cron.d/pka-mail-lebenszeichen` (Quelle `deploy/cron.d/`), Log `/var/log/pka-mail-lebenszeichen.log`. `--dry-run` prüft nur, ob der Zugang gesetzt ist. Liest `secrets.env` per `load_secrets()` → **nicht von Claude ausführen**, auch nicht als Trockenlauf |
| monatlich 1., 05:00 | `plz_check.py` | PLZ-Wächter (#419, ADR-016): meldet neuen OpenPLZ-Export per Telegram, sonst still. `/etc/cron.d/pka-plz-check` (Quelle `deploy/cron.d/`), Log `/var/log/pka-plz-check.log`, Zustand `/var/lib/pka-plz/stand.json`. Schreibt **nie** `plz_gemeinden.json`. Test: `plz_check.py --dry-run` (ohne Senden), `--force` rechnet auch ohne neuen Export (~2 Min, 55 MB + ~230 OpenPLZ-Abrufe) |
| täglich 00:05 | `stats_collector.py` | Besucherstatistik → `page_stats`-Tabelle |
| wöchentlich Mi 07:00 (`0 7 * * 3`) | `heimat_import.py` | Termine aller Gemeinden (heimat-info, Parser, KI) fetchen → Telegram-Vorschau |
| Di+Do 06:00 | `traffic_info.py` | Verkehrsinfo-Check |
| alle 15 Min | `pka_todos_reminder.py` | PKA Todos Fälligkeits-Erinnerungen |
| alle 30 Min | `telegram_webhook_guard.py` | Prüft `getWebhookInfo`, setzt Webhook automatisch neu + Telegram-Alarm falls weg (seit 2026-08-27, siehe Pitfall oben) |

**kalender_report.py (2026-05-22):** Datenquelle auf SQLite-DB umgestellt (`page_stats` + `page_stats_geo`). Zeigt verifizierte Zahlen (ohne Crawler), Datum-Label des letzten verfügbaren Tages, Deutschland-Besucher aus `page_stats_geo`. Keine nginx-Log-Analyse mehr. Läuft nur noch 00:10 + 20:00 Uhr (war: 4×täglich).
**kalender_report.py (2026-06-17):** Neue Funktion `verein_activity_stats()` – fragt `vk_audit` nach `aktion='erstellt'` und `aktion='geaendert'` ab (UTC-Timestamps, Cutoffs 24h + 7d). Zeigt im Bericht: `✏️ Vereinstermine 24h: X neu · Y geändert | 7 Tage: X neu · Y geändert`. Nur Aktionen durch Vereine selbst (Dashboard), keine heimat-info-Importe.
**kalender_report.py (2026-08-06):** Bug behoben – da `stats_collector.py` nur einmal täglich (00:05) den **abgeschlossenen Vortag** berechnet, zeigte der 20:00-Bericht bislang dieselben (eingefrorenen) Zahlen wie der 00:10-Bericht desselben Morgens, während der nächste 00:10-Bericht dann einen komplett anderen Kalendertag zeigte – wirkte wie unplausible Sprünge. Neue Funktion `get_live_today_stats()` berechnet beim 20:00-Lauf den laufenden Tag (00:00 bis jetzt) live aus dem aktuellen nginx-Log (dieselbe Zähl-/Crawler-Logik wie `stats_collector.collect_day()`), schreibt aber **nicht** in die DB (sonst würde die 7-Tage-Summe unvollständige mit vollständigen Tagen mischen). Umschaltung morgens/abends anhand der Uhrzeit (`< 12 Uhr` → Vortag aus DB, sonst live). Bericht-Label zeigt jetzt explizit „(Vortag, vollständig)" bzw. „(heute bis HH:MM Uhr)".
**kalender_report.py (2026-10-05, v1.62):** Der 20:00-Lauf hängt „🗂 Offen im Admin: …“ an (Importe bestätigen, Vereine freigeben, Orte zuordnen, Register prüfen) – täglich, solange etwas offen ist, kein Abschnitt bei 0 (Josef-Entscheidung statt eigenem Job). Zählung aus `shared/admin_aufgaben.py::offene_aufgaben()`; ein Fehler dort verhindert den Bericht nie. „Falsch“ gemeldete Register-Einträge zählen nicht (Claudes Aufgabe). Trockenlauf am Server: siehe Logbuch 2026-10-05.
**Bekannte, noch offene Bugs im selben Bericht (2026-08-06, mit Josef noch zu klären):**
- ~~`verein_activity_stats()` zählt nur `aktion IN ('erstellt','geaendert')`...~~ **Behoben 2026-08-06:** siehe unten.

**vk_audit / kalender_report.py (2026-08-06):** Bug behoben – Massen-Uploads im Vereins-Dashboard (`/verein/upload`, `/verein/confirm-upload`) loggten `aktion='upload'`/`'upload_confirmed'` mit `termin_id=f"bulk_{total}"`, wurden aber nie in „X neu" gezählt (Query fragte nur `aktion IN ('erstellt','geaendert')` ab). Fix: neue Spalte `vk_audit.anzahl` (Default 1, Migration nach bestehendem Muster in `vk_db.py`) – eine Audit-Zeile kann jetzt mehrere Termine repräsentieren (Upload-Batch statt Einzelaktion). `log_audit()` akzeptiert optionales `anzahl:int=1`; die beiden Upload-Call-Sites in `services/verein/routes.py` setzen `anzahl=total`. `verein_activity_stats()` summiert jetzt `SUM(anzahl)` über `aktion IN ('erstellt','upload','upload_confirmed')` für „neu" (Konstante `_NEU_AKTIONEN`); „geändert" bleibt unverändert `COUNT(*)` über `aktion='geaendert'`. Deployed inkl. `systemctl restart rename-webhook` (Migration lief beim Start automatisch, verifiziert: Spalte `anzahl` existiert in `vk_audit`).
- ~~„Letzter Import" (`last_import.json`) wird nur von `_do_save_import()` beschrieben (Admin-Web-Import, Telegram-Import, Vereins-Upload) – nicht vom eigentlichen wöchentlichen `heimat_import.py` (Mi 07:00).~~ **Behoben 2026-08-06:** siehe unten.

**heimat_import.py (2026-08-06):** Bug behoben – `do_import()` (schreibt die per Telegram/Admin-UI bestätigten heimat-info-Termine tatsächlich in `vereinstermine.json`) aktualisiert jetzt zusätzlich `/opt/rename-webhook/last_import.json` (Datum, Anzahl neuer Termine, Anzahl betroffener Vereine – gleiches Format wie `_do_save_import()`). Zuvor wurde diese Datei ausschließlich von Admin-Web-Import/Telegram-Direktimport/Vereins-Upload beschrieben, nie vom eigentlichen wöchentlichen heimat-info-Import (Mi 07:00 Cron → Telegram-Bestätigung → `do_import()`) – „Letzter Import" im Telegram-Bericht stand deshalb 2,5 Monate lang auf `2026-05-15 15:30, 0 Termine, 0 Vereine`, obwohl der wöchentliche Import ganz normal lief. Betrifft beide Aufrufer von `do_import()`: Telegram-Callback `heimat_ok:` (services/telegram/routes.py) und Admin-Web-UI-Import (services/kalender/routes.py).

---

## nginx-Konfiguration

- **Config:** `/etc/nginx/sites-available/vereinskalender` → Domains: `vereinskalender.online`, `www.vereinskalender.online`, `veranstaltungen.website`, `www.veranstaltungen.website` (alle zeigen denselben Inhalt, kein Redirect)
- **Seit 2026-07-02:** `sites-enabled/vereinskalender` ist ein echter Symlink auf `sites-available/vereinskalender` (vorher zwei divergierende Dateien). Immer nur `sites-available/vereinskalender` editieren. **Niemals** `.bak`-Kopien in `sites-enabled/` ablegen – nginx lädt alles dort automatisch mit (führte zu „conflicting server name"-Warnungen). Backups gehören nach `/root/nginx-backups/`.
- **SSL-Cert:** deckt alle 4 Domains ab, läuft bis 2026-09-05, Auto-Renewal aktiv
- **PWA-Titel domain-abhängig:** `manifest_json()` prüft `request.host` → `name: "Veranstaltungen"` bei `veranstaltungen.website`, sonst `"Vereinskalender"`. Gleich auch in `<title>` + `apple-mobile-web-app-title` per JS in kalender.html.
- `location = /` → `proxy_pass http://127.0.0.1:5000/kalender` + `Cache-Control: no-store`
- `location = /admin` → `proxy_pass http://127.0.0.1:5000` + `Cache-Control: no-store`
- `location = /sw.js` → `Cache-Control: no-cache, no-store` + `Service-Worker-Allowed: /`
- `location = /api/termine` → Rate-Limit 30 req/min, Burst 5 (Scraping-Schutz)
- `location /api/` → Rate-Limit 10 req/s, Burst 30
- `location = /api/termine/flyer` → `client_max_body_size 10m`, `proxy_read_timeout 60` (seit 2026-10-03, v1.35). Für den Rest von `/api/` gilt das nginx-Standardlimit **1 MB**.
- `location ~ ^/verein/(termine|upload)` → wie `/verein`, aber `client_max_body_size 45m` + `proxy_read_timeout 120` (seit 2026-10-03). **Pitfall:** Davor galt für alles unter `/verein` 1 MB – Flyer > 1 MB und größere Terminplan-PDFs scheiterten still mit 413, obwohl die Formulare „max. 8 MB“ versprachen. Neues Formular mit Datei-Upload unter `/verein` ⇒ prüfen, ob es unter diese Location fällt.
- `location /verein` → proxy_pass Flask (Auth-Seiten, Dashboard)
- `location /telegram` → Telegram Haupt-Bot-Webhook (**Pflicht!** Muss in dieser Config stehen)
- `location /kalender-bot` → Telegram Kalender-Bot-Webhook
- `location ~ ^/hero-region(-1000)?\.jpg$` → `root /opt/rename-webhook/static`, `Cache-Control: public, 30 Tage`, Security-Header wiederholt (seit 2026-10-04, v1.45, Titelbild). `/static/` selbst ist auf den VKO-Domains **nicht** freigegeben – neue statische Dateien brauchen eine eigene Location.
- **Rate-Limit-Conf:** `/etc/nginx/conf.d/rate-limit.conf` (api_zone, api_termine_zone, auth_zone)
- **Security-Header:** HSTS, X-Frame-Options, X-Content-Type-Options, Referrer-Policy – in Locations mit eigenem `add_header` explizit wiederholen (nginx-Vererbungsregel)
- Nach Änderungen: `nginx -t && systemctl reload nginx`
- **⚠️ Pitfall Telegram-Webhook:** Bei Domain-Änderungen oder neuer nginx-Config IMMER prüfen ob `/telegram` enthalten ist. Fehlt die Location → Telegram-Callbacks kommen nicht an → Bot stumm. Webhook-URL: `https://vereinskalender.online/telegram`. Prüfen: `getWebhookInfo`. Neu setzen: `setWebhook url=https://vereinskalender.online/telegram`. (Vorfall 2026-06-07 bis 2026-06-10)
- **⚠️ Pitfall Telegram-Webhook – Stille Deregistrierung (2026-08-12 bis 2026-08-27):** `getWebhookInfo` zeigte `"url":""` – die Webhook-Registrierung war komplett weg (nicht nur ein Erreichbarkeitsproblem; Server/nginx/`/telegram`-Route waren die ganze Zeit funktionsfähig, extern per `curl` verifiziert). Ursache nicht abschließend geklärt (typischer Auslöser: ein `getUpdates`-Call gegen denselben Bot-Token löscht automatisch jeden bestehenden Webhook – z.B. durch ein Test-Skript, einen zweiten Client oder eine parallele Bot-Instanz). Symptom: **alle** Telegram-Befehle (nicht nur `/heimat`) blieben 2 Wochen ohne Reaktion, `POST /telegram` verschwand komplett aus App- und nginx-Logs. Zwei `heimat_pending_*.json` blieben unbestätigt liegen (19.08., 26.08.) bis zum Fix. **Diagnose-Reihenfolge:** 1) `journalctl -u rename-webhook | grep 'POST /telegram'` – letzter Treffer? 2) externer `curl -X POST https://vereinskalender.online/telegram -d '{}'` → **seit v1.44 403** (Secret fehlt – bestätigt, dass Server und Route laufen; 502/404 wäre ein Server-/nginx-Problem). Webhook neu setzen immer über `telegram_webhook_guard.py --force` (setzt `secret_token`), nie per Hand ohne Secret (ADR-022). 3) `getWebhookInfo` (Token nötig – Josef selbst ausführen, `source /etc/pka/secrets.env` gehört zur Sicherheitsregel-Ausnahme für ihn, nicht für Claude). Leeres `url`-Feld → Fix ist einfach `setWebhook url=https://vereinskalender.online/telegram`, kein Code-/Config-Bug.

---

## Öffentliche Endpunkte `vereinskalender.online`

| Pfad | Beschreibung |
|------|--------------|
| `/` | Vereinskalender-PWA |
| `/api/termine` | GET/PATCH/DELETE – Termine (PATCH/DELETE: Auth X-Upload-Token) |
| `/api/termine/flyer` | POST multipart – Admin: Flyer hochladen/ersetzen (`aktion=hochladen`, Datei `flyer`) oder `aktion=entfernen`; Termin über `verein_key`+`datum`+`bezeichnung`, gelöschte Termine ausgenommen; alter Flyer wird in Dropbox gelöscht (Auth X-Upload-Token, nginx 10m) |
| `/api/ical` | GET – iCal-Export einzelner Termin |
| `/api/ical/feed` | GET – Abonnierbarer Feed (`webcal://`). Favoriten-Abo (v1.61, ADR-025): `v=` Vereine, `o=Ort\|Gemeinde`, `g=Gemeinde\|Landkreis X`, `r=Region` – ODER-verknüpft, Orte nach Mischregel; altes `?ort=` gleiche Regel. Je Parameter max. 50 Einträge à 100 Zeichen. Vereinssitz aus `_merged_meta()` (Datei + DB, wie `/api/termine`) |
| `/api/check-token` | POST – Admin-Token prüfen |
| `/api/confirm-import` | POST – Upload-Import bestätigen |
| `/api/admin/importe` | GET – Pending-Liste |
| `/api/admin/importe/<uid>` | GET/confirm/reject – Import-Detail |
| `/api/admin/importe/trigger` | POST – Import-Trigger (SSRF-Schutz: nur HTTPS, keine privaten IPs) |
| `/api/admin/importe/status` | GET – Ergebnis des letzten Admin-Triggers (`imports/letzter_lauf.json`) |
| `/api/admin/orte` | GET – Admin-Tab „Orte“: offene Veranstaltungsorte, Zuordnungen, feste Orte, Ortschaften |
| `/api/admin/orte/zuordnung` | POST/DELETE – Ort → Ortschaft zuordnen bzw. löschen (`{ort, gemeinde, verein?, ortschaft}`) |
| `/api/vereine` | GET/POST – Vereine + Meta |
| `/api/vereine/<key>` | DELETE – Verein löschen |
| `/api/admin/stats` | GET – Statistiken (Auth) |
| `/api/admin/stats/chart` | GET – Tages-Zeitreihe `?d=7|30|365` |
| `/api/admin/users` | GET – Alle Accounts (Auth) |
| `/api/admin/verein/<id>` | PATCH/DELETE – Verein-Account |
| `/api/admin/unregistered-keys` | GET – Keys in vereinstermine.json ohne Account (für Transfer-Dropdown) |
| `/api/admin/verein/<id>/transfer-key` | POST `{source_key}` – Termine + _meta + _labels + tg_subscriptions übertragen |
| `/upload` | Superadmin-Upload (PDF/JPG/PNG/HEIC/Excel) |
| `/#admin` | Admin-PWA (Tabs: Import/Importe/Vereine/Accounts/Termine/Stats/Verknüpfen/Orte) |
| `/verein/register` | Selbstregistrierung (PLZ → Ortschaft, Ansprechpartner, ADR-016) |
| `/verein/profil` | Vereinsprofil: alle Registrierungsdaten, E-Mail-Wechsel mit Bestätigung |
| `/verein/email-bestaetigen` | GET `?token=` – Link aus der Mail an die neue Adresse (24 h, einmalig) |
| `/api/orte` | GET `?plz=NNNNN` – öffentlich: Gemeinden + Ortschafts-Vorschläge (400 bei ungültiger PLZ) |
| `/verein/login` | Vereins-Login (bcrypt, Brute-Force-Schutz) |
| `/verein/dashboard` | Termin-Übersicht (nach Login) |
| `/verein/upload` | Vereinsadmin-Upload (Rate-Limit 3/Tag) |
| `/sw.js` | Service Worker (Network-first App-Shell, /api/* nie cachen) |
| `/manifest.json` / `/manifest-admin.json` | PWA-Manifeste |

---

## Datenstruktur `vereinstermine.json`

- **`_labels`**: `{vereinKey: "Anzeigename"}` – letztes Wort > 4 Zeichen = Heimatort-Fallback. **Maßgeblich für die Sichtbarkeit:** `/api/vereine` iteriert über `_labels`, nicht über die DB. Kein Label = der Verein existiert für den Kalender nicht.
- **`_meta[key]`**: `{plz, gemeinde, landkreis, heimatort?, selbstverwaltung?}`
- **`_ortschaften`**: `{gemeinde_map: {...}}` – Mapping Ortschaft→Gemeinde

### Pitfalls `vereinstermine.json`
- **✅ Behoben 2026-09-25 – Freigabe legt den JSON-Eintrag jetzt selbst an:** Vorher setzte die Freigabe nur `status=aktiv` in der DB; ein freigegebener Verein ohne eigene Termine fehlte in `_labels`/`_meta` und war in der Vereinsübersicht unsichtbar (Vorfall 2026-06-18: FFW Paindlkofen, musste manuell nachgetragen werden). Beide Freigabe-Wege rufen jetzt `shared/kalender_store.register_verein()` – siehe Abschnitt „Vereinsfreigabe" unten. **Achtung bei Altbestand:** Vereine, die *vor* dem Fix freigegeben wurden, sind weiterhin unsichtbar und müssen einmalig nachgetragen werden (Abgleich: `status='aktiv'` in `vereine_accounts` gegen die Keys in `_labels`).
- **`_heimat_aliases` (Stand 2026-07-16):** `{heimat_key: account_key}` in `vereinstermine.json`. `admin_transfer_key()` (`services/auth/routes.py`) schreibt hier automatisch rein. `heimat_import.py` löst den bei jedem Import frisch aus dem Veranstalter-Namen berechneten `verein_key` (`_slugify(veranst)`) zuerst gegen diese Map auf – sonst entsteht bei abweichender Schreibweise auf heimat-info.de erneut ein Duplikat-Key für einen bereits transferierten Verein. Selbstverwaltende Vereine werden von heimat-info NICHT mehr komplett ausgeschlossen, sondern normal dedupliziert (heimat-info bleibt Ergänzungsquelle, siehe ADR-003). (Vorfall 2026-07-16: FFW Paindlkofen / Freiwillige Feuerwehr Paindlkofen, doppeltes Weißwurstfrühstück)
- **Vereinsname-Umbenennung muss `_labels[verein_key]` mitziehen:** Der im Kalender angezeigte Vereinsname kommt ausschließlich aus `_labels[verein_key]` in `vereinstermine.json` – nicht aus `vereine_accounts.verein_name`. Jede Stelle, die `verein_name` in der DB ändert (`verein_profil()` in `services/verein/routes.py`, `admin_update_verein()` in `services/auth/routes.py`), muss bei geändertem Namen zusätzlich `_labels[verein_key]` per `KalenderStore.update()` nachziehen, sonst zeigt der Kalender weiter den alten Namen. (Gefixt 2026-07-15)
- **`KalenderStore.update()` als `root` → Owner-Problem:** Direkter Python-Aufruf als root ändert den Datei-Owner auf `root` → App-User `webhook` bekommt `Permission denied`. **Immer als Dienstbenutzer schreiben:** `sudo -u webhook /opt/rename-webhook/bin/python3 -c "…KalenderStore.update(…)"`. Falls doch als root passiert: sofort `chown webhook:webhook /opt/rename-webhook/vereinstermine.json` und Journal auf `Permission denied` prüfen. (Wieder passiert 2026-10-05 beim Setzen von `_meta.quelle`, nach 1 Min behoben, keine Schreibfehler.)
- **`_meta[key].quelle` (seit 2026-10-05):** Standard-Quelle je Verein – `/api/termine` setzt sie bei Terminen ohne eigene `quelle` ein (Kartenmarke „Quelle: …“). Gesetzt für `pfarrgemeinde_postaumoosthanno` = „Pfarrbrief“ (Pfarrbrief-Importe über den Kalender-Import); die Gottesdienste aus `gottesdienste.json` bekommen „Pfarrbrief“ fest in `gottesdienste_eintraege()`. Nicht im Admin-Formular editierbar (das Speichern dort lässt das Feld unberührt).
- **`KalenderStore.update()`-Mutator darf `d` nie durch einen vorher gelesenen Snapshot ersetzen:** `_do_save_import()` hat genau das getan (`d.clear() or d.update(data)`) und damit parallele Schreibzugriffe verloren gehen lassen, obwohl der Store selbst korrekt sperrt. Merge-Logik immer *innerhalb* des Mutators auf `d` selbst ausführen; langsame Netzwerk-Calls (z.B. `lookup_plz`) davor/außerhalb berechnen, nicht im Lock. (Regression gefixt 2026-07-05, Fable-5-Review)
- **`_do_save_import()` bei Vereinsadmin-Uploads:** Immer `verein_key=user["verein_key"]` übergeben, sonst wird der Key neu aus dem Vereinsnamen abgeleitet und kann vom Account-Key abweichen (Kürzung/Uniquifizierung) → Termine landen unsichtbar unter falschem Key.
- **`ortschaft`**: Pro Termin – Veranstaltungsort
- **`quelle`** / **`quelle_url`**: Pro Termin – Herkunft (heimat-info oder Vereinsadmin)
- **`flyer_url`** / **`flyer_path`** (optional, Stand 2026-06-07): Pro Termin – Dropbox-Link (`?raw=1` für Browser-Anzeige) + Dropbox-Pfad zum Löschen. Modul: `shared/flyer_store.py`. Upload-Ordner: `/Dokumente/Vereinskalender/flyer/`. Nur PDF/JPG/PNG/WebP, max. 8 MB, Magic-Bytes-geprüft, UUID-Dateiname.
- **Pitfall Flyer-Upload – `cid:image...`-Meldung:** Zieht ein Vereinsadmin ein Bild direkt aus einer Outlook-Mail (Drag&Drop) in das Flyer-Upload-Feld, übergibt der Browser oft nur Outlooks interne Content-ID (`cid:image001.png@...`) statt der echten Bilddaten – kein Bug im Vereinskalender, die Datei erreicht den Server so gar nicht (`upload_flyer()` in `shared/flyer_store.py` würde bei echtem Empfang einen deutschen Fehlertext liefern). Fix: Hinweistext direkt über dem Datei-Feld in `termin_neu()` + `termin_edit()` (Formulare) sowie im Hilfe/FAQ-Block auf `/verein/dashboard` (`services/verein/routes.py`) – Flyer immer zuerst lokal speichern, dann hochladen. (Vorfall 2026-07-31: FF Paindlkofen)
- **`uhrzeit_bis`** / **`beschreibung`** (optional, seit 2026-10-03, v1.32, ADR-017): Ende `HH:MM` (nur zusammen mit `uhrzeit`; kleiner als `uhrzeit` = endet nach Mitternacht) und Freitext ≤ 1000 Zeichen. Fehlen = Feld nicht vorhanden (beim Leeren wird es per `pop` entfernt, nicht als `""` gespeichert). Beide sind öffentlich (nicht in `_TERMIN_INTERNE_FELDER`). Anzeige: `kalender.html` render() (Zeitspanne, (i)-Symbol `.ev-info-ico`, aufklappbar `.ev-beschr` per Klick auf `.ev-card--info`), Past-Erkennung `_isPastT` nutzt das Ende, iCal über `_ics_zeiten()` in `services/kalender/routes.py` (Einzel-Export Parameter `b`=Ende, `x`=Beschreibung). Schreibwege: Vereinsformulare (neu/bearbeiten) und Admin-PATCH (`/api/termine`, Whitelist). heimat-Import, Excel-Upload und KI-Import setzen sie (noch) nicht.
- **Prüfregeln Termin-Felder zentral in `shared/termin_felder.py`** (`zeit_fehler`, `BESCHREIBUNG_MAX`, `UHRZEIT_RE`, `DATUM_RE`) – genutzt von den Vereinsformularen **und** vom Admin-PATCH (seit v1.36). Neue Feldregel ⇒ dort, nicht in einer der Routen. **Pitfall (gefunden 2026-10-05 im Prototyp Jahresplanung):** `DATUM_RE` prüft nur das Format `\d{4}-\d{2}-\d{2}` – „2027-13-01“ und „2027-02-30“ gehen durch und lassen später `date.fromisoformat` scheitern. Zusätzlich `date.fromisoformat` prüfen (Todo #435).
- **Termin-`id` – jeder Termin hat eine (seit v1.37, 2026-10-03):** `KalenderStore.update()` ruft nach jedem Mutator `stelle_ids_sicher()` (`shared/kalender_store.py`) – vergibt fehlende **und doppelte** IDs neu (8 Hex, dateiweit eindeutig), bestehende bleiben. Damit bekommen Termine aus **allen** Schreibwegen (heimat-Import, KI-/Excel-Import, Telegram, Transfer, Formulare) eine ID, ohne dass ein Schreibweg daran denken muss. **Pitfall:** Wer `vereinstermine.json` *nicht* über `KalenderStore.update()` schreibt, umgeht das – nicht tun. Migration am 2026-10-03: 457 von 459 Terminen hatten keine ID (u. a. alle 325 heimat-Termine); ein Leer-Update als `webhook` hat sie nachgetragen (Probe vorher an einer Kopie: sonst byte-gleich). Folge vorher: das Vereins-Dashboard (`t["id"]` im Bearbeiten-Link) wäre für FFW Paindlkofen mit `KeyError` abgestürzt. Admin-PATCH/DELETE/`/api/termine/flyer` suchen über `id` (`_termin_passt()`), Datum+Bezeichnung nur noch als Rückfall ohne ID; eine unbekannte ID gibt 404, kein Rückfall. **iCal-UIDs bewusst unverändert** (Datum+Titel+Verein) – neue UIDs würden bei allen Abonnenten Doppel erzeugen. Gottesdienste aus `gottesdienste.json` haben keine ID (nicht im Admin editierbar).
- **Keine Emojis im Vereinsbereich (seit v1.37):** Knöpfe, Überschriften und Meldungen in `services/verein/routes.py` und den Seiten in `services/auth/routes.py` sind emojifrei (Bearbeiten-Link als Text). Ausnahme: Telegram-Nachrichten an Josef (`auth/routes.py` ~186–292) – keine Oberfläche.
- **Admin-PATCH/DELETE `/api/termine` überspringen gelöschte Einträge (seit v1.36):** Die Suche läuft über `datum`+`bezeichnung`; vorher traf sie auch eine soft-gelöschte Kopie gleichen Namens (Verein löscht und legt neu an) – die Änderung landete unsichtbar, der Flyer-Endpunkt dagegen am lebenden Termin. PATCH prüft jetzt Zeitfelder/Beschreibung/Datum gegen den Endstand (400 mit Meldung, nichts geändert); leere `uhrzeit_bis`/`beschreibung` entfernen das Feld. **Restrisiko:** zwei *lebende* Termine mit gleichem Datum+Titel im selben Verein sind weiter nicht unterscheidbar – sauber wäre die Termin-`id` (fehlt bei Alt-/Importterminen).
- **Seit v1.44 (Komplett-Review 2026-10-04):**
  - **Admin-Löschen ist Soft-Delete** (`geloescht_von: "admin"`) wie im Vereinsformular. Endgültig gelöschte Termine sah der Gemeinde-Import nicht mehr und legte sie mittwochs neu an. Beim Löschen fliegt der Flyer mit raus (Admin **und** Verein).
  - **`/api/vereine` POST/DELETE** arbeiten im Store-Mutator. Vorher galt Snapshot + `d.clear() or d.update(raw)` mit `lookup_plz` dazwischen, ein Lost Update von mehreren Sekunden. `grep "d.clear() or d.update"` muss leer bleiben.
  - **`shared.kalender_store.uebertrage_key(source, target)`** ist die einzige Schreibstelle für Admin-Transfer und Telegram-„Verknüpfen“. Sie setzt `_heimat_aliases`, biegt Alias-Ketten um, zieht Abos ohne PK-Kollision um und verweigert `_…`-Keys.
  - **Admin-Fenster „Accounts“** (`PATCH /api/admin/verein/<id>`) gleicht `_meta` ab und bestimmt Gemeinde/Landkreis bei geändertem Ort neu. Vorher landete alles nur in der DB, und `_meta` (Vorrang in `/api/termine`) blieb alt.
  - **Registrierung:** `_unique_verein_key` meidet auch Keys aus `vereinstermine.json` (Termin-Keys, Labels, Aliase). Bestehende Import-Termine übernimmt ein neuer Account nur über „Verknüpfen“.
  - **`_do_save_import` behält vergangene Termine** des Vereins. Vorher löschte jeder Upload die Historie samt gelöschter Einträge.
  - **`KalenderStore.update()` prüft nach `flock` die Inode** und öffnet neu, falls ein anderer Prozess die Datei ersetzt hat. Mit zwei Schreibprozessen verlor die alte Version Einträge (Test: 41 von 80).
  - **iCal-Feed:** Bei gleicher UID (gleicher Tag, gleiche ersten 20 Titelzeichen, gleicher Key) behält der erste Termin seine UID, weitere bekommen eine Endung aus Uhrzeit + Ort (ADR-019 bleibt gewahrt).
  - **Lesende Stellen filtern `geloescht`/`deleted`:** `kalender_erinnerung.py` (Abonnenten!), `event_reminder.py`, `kalender_report.py`, Telegram `/verein`. Verworfene heimat-Termine liegen als `geloescht` beim Verein; ohne Filter gingen sie als Erinnerung raus.
  - **heimat-Pending** wird atomar geschrieben (`_schreibe_pending`, tmp + `replace`, Besitzer = `imports/`). Ein root-eigenes Pending ließ sich vom Dienst sonst nicht kürzen (`except OSError: pass`). `_log` bricht nicht mehr ab, wenn die Log-Datei nicht schreibbar ist.
  - **Service Worker `vko-v4`** cached nur noch `/` und nur `res.ok`. Vorher landeten Dashboard und `/admin` (mit E-Mails) und Fehlerseiten im Cache Storage.
- **Mehrtägige Termine = ein Eintrag pro Tag (ADR-017):** `termin_neu()` legt bei `datum_bis` bis zu `_MAX_TAGE = 16` unabhängige Einträge an (eigene `id`, eigene `beschreibung`/Flyer aus `beschreibung_{i}`/`flyer_{i}`), es gibt kein `datum_bis`-Feld und keine Serien-ID. Bearbeiten/Löschen wirkt je Tag. Flyer werden erst alle mit `pruefe_flyer()` geprüft, dann hochgeladen; bei Dropbox-Fehler werden die bereits hochgeladenen gelöscht. Summe aller Flyer je Formular ≤ `_MAX_UPLOAD_MB = 40` (nur JS-Hinweis; die harte Grenze ist nginx 45m), Terminplan-Upload `/verein/upload` ≤ `_MAX_PLAN_MB = 20` serverseitig vor dem Kontingent-Abzug.
- **Vorschau im Vereinsformular (seit 2026-10-03, v1.33, ADR-018):** Knopf „Vorschau“ in `termin_neu()`/`termin_edit()` (`_VORSCHAU_BTN`, `_VORSCHAU_JS` in `services/verein/routes.py`) öffnet ein Overlay mit `<iframe src="/?vorschau=1">` – das ist der **echte** `kalender.html`, die Karten zeichnet `render()`. Ablauf per `postMessage` (nur `location.origin`, Quelle geprüft): iframe meldet `vko-vorschau-bereit` → Formular schickt `vko-vorschau-daten` `{verein:{key,name}, termine:[…]}` → iframe lädt `/api/termine` nur für Labels/Farben/Meta und ersetzt die Termine. Neue Flyer gehen als `blob:`-URL mit (nichts wird hochgeladen); Klick auf die Büroklammer schickt `vko-vorschau-flyer` zurück, das Formular zeigt Bild/PDF im Overlay. Formular-Attribute: `id="termin-form"`, `data-verein-key`, `data-verein-name`, beim Bearbeiten `data-flyer-url`.
- **`VORSCHAU`-Modus in `kalender.html`:** Klasse `html.vorschau` (gesetzt im Anti-Flacker-Script) blendet Kopf, Filter, CTA, Disclaimer aus. `VORSCHAU` sperrt **alle** localStorage-Schreibwege (`saveState`, `loadState`, `saveFavorites`, `saveActiveFavs`, Rubrik-Speicherung), setzt Favoriten/Rubriken leer, `zeitraum="alle"`, alle Vereine aktiv (auch Pfarr), kein Service-Worker, kein Welcome-Banner, Herz/iCal ohne Wirkung. **Pitfall:** Neue localStorage-Schreibstelle in `kalender.html` ⇒ mit `if(VORSCHAU)return` absichern, sonst überschreibt eine Vorschau die echten Favoriten/Filter des Vereinsadmins (Migrationen beim Laden rufen `saveFavorites()` auf!). Statistik zählt `/?vorschau=1` nicht (`stats_collector` matcht nur `GET / ` und `GET /kalender`) – deshalb **nicht** auf `/kalender?vorschau=1` umstellen. Einbetten geht wegen `X-Frame-Options: SAMEORIGIN`.
- **Keine verschachtelten `<form>` in Server-Seiten:** Der Browser verwirft das innere `<form>` samt `onsubmit` – bis v1.33 erschien deshalb die Rückfrage „Flyer wirklich entfernen?“ nie. Jetzt Knopf im Hauptformular mit `onclick="return confirm(…)"`.
- **⚠️ Tests nie mit pytest starten (2026-10-10):** Alle `tests/*.py` sind Skripte mit eigener `pruefe()`-Funktion, die nicht wirft. `pytest tests` meldet deshalb „passed“, auch wenn Prüfungen fehlschlagen, und bei `test_geo.py` Schein-Fehler (fehlende Fixture `termine`). Richtig: jede Datei einzeln mit `python3 tests/<datei>.py`, Erfolg = letzte Zeile „ALLE PRÜFUNGEN BESTANDEN“.
- **Offline-Test der Vereinsformulare:** Seit v1.44 reicht lokal `python3 tests/test_app.py` (Python 3.13 vorhanden). Älterer Weg: lokal gab es nur Python 3.9 (Code braucht ≥ 3.10). Bewährt (2026-10-03): `src/` per rsync nach `/tmp/vko-test/` auf den Server, dort `runuser -u nobody -- /opt/rename-webhook/bin/python3 test.py` mit Attrappen für `load_secrets`, `upload_flyer`/`delete_flyer`, `log_audit`, `validate_csrf` und eigener `vereinstermine.json` unter `/tmp` (alle drei Modulkopien von `VEREINSTERMINE_FILE` umbiegen: `kalender_store`, `kalender_core`, `services.*.routes`). `nobody` kann weder Secrets noch Produktivdaten lesen/schreiben. Kopie danach löschen.
- **DSGVO – interne Felder pro Termin:** `erstellt_von`/`geaendert_von`/`geloescht_von` (Admin-E-Mail) + `flyer_path` (interner Dropbox-Pfad) dürfen nie an den öffentlichen `/api/termine`-Endpunkt raus. Blacklist: `_TERMIN_INTERNE_FELDER` in `services/kalender/routes.py`. Neues Feld mit potenziell sensiblem Inhalt pro Termin → dort ergänzen. (Fund 2026-07-05, Fable-5-Review)

- **`_meta[key].landkreis` immer in der Form `"Landkreis X"` (Stand 2026-09-20):** Das Frontend (Landkreis-Chips, Gemeinde-Schlüssel, Fallback `"Landkreis Landshut"`) erwartet das Präfix. Die Schreibweise `"Landshut"` (ohne Präfix) erzeugte einen zweiten Landkreis-Chip und – seit v1.9 – eine doppelte Gemeinde „Ergoldsbach (Landshut)". Fund: FFW Paindlkofen stand in `_meta` **und** DB (`vereine_accounts`) auf `"Landshut"`, am 2026-09-20 in beiden auf `"Landkreis Landshut"` korrigiert (Backups `/root/*.bak-2026-09-20*` am 2026-09-20 nach Abnahme wieder gelöscht). **Ursache behoben 2026-09-20 (ADR-009):** `lookup_plz()` (`shared/kalender_core.py`) entfernte per `re.sub` das Präfix aus Nominatims `county` und lieferte bei jeder PLZ-basierten Registrierung/Import/PATCH `"Landshut"`. Jetzt `_normalize_landkreis(county, stadt)`: Nominatim-`county` wird **unverändert** übernommen (`"Landkreis Landshut"`); fehlt `county` (kreisfreie Städte wie München/Regensburg/Landshut), wird `"Stadt <city|town>"` geliefert; **kein** Fallback mehr auf `state_district` (Regierungsbezirk „Niederbayern" ist kein Landkreis); ein Bare-County ohne Präfix (z. B. Straubing) bleibt unverändert. Bestand vorher geprüft: JSON `_meta` 125× `"Landkreis Landshut"` + 1× leer, DB gleich – keine Migration nötig. **Restrisiko:** Fehlt bei einer Landkreis-Gemeinde ausnahmsweise das Nominatim-`county` (OSM-Datenlücke), wird fälschlich `"Stadt X"` vergeben → im Admin-Tab „Vereine" korrigieren.

**Ortschaft-Hierarchie:** `Landkreis → Gemeinde → Ortschaft (Vereinsheimat) → Verein → Termin`
- Ortschaft-Chips = Heimatort des Vereins, NICHT Veranstaltungsort
- `_vereinsForOrt(o)` → Set aller Verein-Keys für Ortschaft
- `_gemeindeForOrt(o)` → Gemeinde-String für Ortschaft

---

## UI-Komponenten kalender.html (Stand 2026-06-07)

- **Skeleton Loader:** 5 `.sk-card`-Divs im `#ev-list` als Initialzustand. CSS-Shimmer via `background:linear-gradient` + Animation, kein JS.
- **Lucide Icons:** Alle UI-Icons als inline SVG (kein CDN). Filter-Chevrons: `<span class="f-arrow">` enthält SVG, `transform:rotate(90deg)` per CSS-Klasse `.f-arrow.open` animiert sie.
- **Aktive Filter Pills:** `renderActiveFilterPills()` baut `#active-pills`-Bar. Aktionen in `_activePillActions[]` (Module-Level). Aufruf aus `updateAllBadges()`. Click-Handler in `load()`.
- **Accordion-Transitions:** `.f-content` nutzt `max-height:0/1000px` + `overflow:hidden` – KEIN `display:none/block` mehr. Pitfall: Wenn neue Sections hinzugefügt werden, kein `display:none` im CSS setzen.
- **Scroll zu Datum (geändert 2026-09-20, v1.9):** Die App scrollt nur noch beim **ersten Laden und beim Refresh-Button** zum ersten `.ev-date-sep` mit `data-d >= heute` (lokales Datum) – gesteuert über das Flag `_scrollToToday` (in `render()` sofort verbraucht, `doRefresh()` setzt es neu). Bei Filterwechseln bleibt die Scroll-Position erhalten. **Pitfall:** Vergangene Termine an Tagen vor heute haben *nicht* die Klasse `.ev-card--past` (die gilt nur für heutige Termine mit Uhrzeit < jetzt) – die alte Logik „erste Karte ohne `--past`" sprang daher bei Zeiträumen mit Vergangenheit („Alle", „14/30 Tage zurück") an den Listenanfang statt zu heute.
- **Event-Card Icons (Stand 2026-06-07):** Alle drei Buttons nutzen gemeinsame CSS-Klasse `.ev-icon-btn` (position absolute, top:10px). Positionen: `.ev-cal` right:10px, `.ev-fav` right:50px, `.ev-flyer` right:90px. Icons als Lucide SVG inline.
- **Favoriten-Herz:** `.ev-fav.on` → `color:#ff3b30` + `svg path { fill:currentColor }` per CSS. Kein JS-`style.filter` mehr. `toggleFavVerein` setzt `innerHTML=icon("heart",14)` für alle `.fav-star`-Elemente (kein Emoji mehr).
- **Alle UI-Icons Lucide SVG (Stand 2026-06-08):** Kein Emoji als Icon irgendwo im UI. Zentrales `ICON_PATHS`-Objekt + `icon(name,size,filled)` Hilfsfunktion. Ausnahme: `alert()` / `confirm()` Browser-Dialoge – dort dürfen Emojis bleiben (kein SVG möglich).
- **Flyer-Button:** Nur gerendert wenn `t.flyer_url` gesetzt. `titlePadding` dynamisch: 74px (ohne Flyer) / 114px (mit Flyer). Click via Event-Delegation `.js-ev-flyer-btn` → `window.open(dataset.flyerUrl, '_blank', 'noopener')`.
- **Suchfeld-Lösch-Button (seit 2026-08-10, PWA-Standard):** Generische Helper `_toggleMiniClear(inputId)` / `_clearMiniSearch(inputId, cb)` (CSS-Klasse `.mini-srch-clear`) für Suchfelder außerhalb der Haupt-Suche (die hat bereits `#srch-clear`/`.srch-clear`). Konvention: Clear-Button-ID = `<inputId>-clear`. Aktuell genutzt in `#ver-srch` (Vereinsverwaltung) und `#trm-search` (Terminverwaltung).

## Registrierung `/verein/register` und Profil `/verein/profil` (Stand 2026-10-02, ADR-016)

- **Login ist die E-Mail-Adresse**, nicht der Vereinsname. Bis 2026-10-02 stand in der Registrierung fälschlich „Vereinsname (Euer Login-Name)“.
- **Reihenfolge und Pflicht (beide Formulare):** PLZ → Ortschaft (DB-Feld `heimatort`) → Anrede (`Herr`/`Frau`/`keine Angabe`, `shared/vk_mail.ANREDEN`; **seit v1.69 freiwillig** und in keiner Mail mehr genutzt) → Vorname → Nachname → E-Mail → Telefon. Bausteine `_ortschaft_felder()`, `_ansprechpartner_felder()`, `_ORTSCHAFT_JS`, `_PLZ_QUELLE` in `services/auth/routes.py`, vom Profil importiert.
- **Gemeinde/Landkreis:** `ortschaft_geo()` → `shared/geo.ortschaft_aufloesen()` (Register mit gleicher PLZ → Gemeinde der PLZ aus `plz_gemeinden.json`) → nur bei mehrdeutiger PLZ `lookup_plz()` (Nominatim). Hinweise („nicht im Register“, „PLZ passt nicht“, „mehrere Gemeinden“) gehen per Telegram an Josef, abgelehnt wird nie. Gleichnamige Orte anderswo (Bayerbach 94137) lösen bewusst kein „passt nicht“ aus.
- **E-Mail-Wechsel im Profil:** nur mit aktuellem Passwort; Spalten `email_neu`, `email_neu_token`, `email_neu_expires` (24 h); Mail an neu (`send_email_change_confirm`) + Hinweis an alt (`send_email_change_notice`); `/verein/email-bestaetigen` setzt `email`, `email_verified=1` und leert die drei Spalten. Andere Vereine derselben Person (Multi-Verein-Login) behalten ihre Adresse.
- **Profil → Kalender:** `_profil_in_kalender()` schreibt Rubrik, Ortschaft, PLZ, Gemeinde, Landkreis nach `_meta[verein_key]` und den Namen nach `_labels` – nur wenn der Key in `_labels` steht (freigegeben). Leere Werte überschreiben nichts, `selbstverwaltung`/`ortschaft_gemeinde` bleiben. Vorher landeten Profiländerungen nur in der DB und waren im Kalender unsichtbar.
- **Einladungsmail (v1.71):** Platzhalter `{eingeladen_von}` = Vor- + Nachname des einladenden Admins (`mitglieder()` lädt ihn aus `vk_users`, Fallback `name`, sonst „Ein Admin von <Verein>“). Begrüßung fest „Hallo,“ – vom Eingeladenen kennen wir nur die Adresse.
- **Anrede in Mails (v1.69):** `gruss_aus(row)` → immer „Hallo Maria Huber,“ (Vor- + Nachname, ohne Namen „Hallo,“). Josef will kein „Herr/Frau“ vor dem Du; die Anrede steht nicht auf der Texte-Seite, weil sie zum Rahmen im Code gehört, eingesetzt unter der `<h2>`. Selects, die eine Mail auslösen, müssen `u.anrede, u.vorname, u.nachname` mitselektieren (Freigabe in `auth` **und** `telegram`, Reset, Resend-Verify).
- **Admin-Dialog „Name“** ändert nur `vk_users.name`, nicht Vor-/Nachname – die Begrüßung nutzt Vor-/Nachname. Das Profil schreibt beides.
- **Rubriken:** zentral in `shared/rubriken.py`; `kalender.html` hat eine Kopie (`RUBRIKEN_OPT`, `#vd-rubrik`, Chip-Icons in `renderRubrikBar()`). Neue Rubrik ⇒ an allen drei Stellen. Seit v1.29: „Gaststätte/Pub/Bar“ (Lucide `beer`). Seit v1.38: „Gemeinde“ (Lucide `landmark`, Info-Abschnitt „Rubrik“) – gesetzt für Gemeinde Bayerbach, Markt Ergoldsbach, Markt Essenbach, Gemeinde Postau, Gemeinde Weng und automatisch beim Import für Gemeindeverwaltungen.
- **Offline testen ohne Secrets:** `CLAUDE_API_KEY=attrappe` setzen, `vk_db.DB_FILE` auf eine Temp-Datei, Mail-/Telegram-Funktionen in den Modulen ersetzen, Blueprints in eine eigene Flask-App hängen, CSRF-Feld heißt `_csrf`. Geschützte Routen per `inspect.unwrap(V.verein_profil)(user)` aufrufen.
- **Telefon:** serverseitig Pflicht (Registrierung und Profil).
- **Feldhöhe (v1.30):** `_CSS` setzt für Text- und Auswahlfelder `line-height:1.25` + feste Höhe (46 px) und `appearance:none` auf `<select>` mit eigenem Pfeil (Lucide `chevron-down` als Data-URI). Ohne das zeichnet Safari/iOS Auswahlfelder niedriger. Checkbox- und Dateifelder sind ausgenommen (`:not([type=checkbox]):not([type=file])`).
- **Admin-Dialog „Termin bearbeiten“ (v1.35, `showTerminEdit()`):** Felder über `_admFld()` mit Klassen `.adm-lbl`/`.adm-inp` (38 px, Datum `type=date`, Safari-Polsterung von Datum/Uhrzeit über `::-webkit-datetime-edit` entfernt), `Verein zuordnen` als `.ve-sel`, Knöpfe `.adm-btn`/`.adm-btn-sec` (auf `.filter-card` mit `--bg3`, weil die Karte selbst `--bg2` hat). Flyer: Upload erst **nach** erfolgreichem PATCH mit den neuen Werten (`_terminFlyer()`), Entfernen sofort mit Rückfrage. `showTerminEdit(idx,true)` zeichnet neu ohne die Selbstverwaltungs-Rückfrage. **Reiterleiste `.adm-tabs` (v1.70):** Symbol (18 px, `.adm-ico`) über dem Text (11 px), `flex-wrap` statt Wischen – am Mac eine Zeile (688 px, alle 10 sichtbar), unter 600 px zwei Zeilen à 5 (`flex:0 0 20%`, passt bis 320 px). Zähler als `.adm-badge` oben rechts am Symbol (Farbe inline, JS setzt nur `display`/Text). Abmelden `.adm-tab-logout` mit Text. Vorher waren am Mac „Texte“ und Abmelden abgeschnitten. **Neuer Reiter ⇒ gleiches Markup** (`<span class="adm-ico">svg</span><span>Text</span>`); ab 11 Reitern Handy auf 6 je Zeile prüfen.
- **Feldhöhe Admin-Dialog „Verein bearbeiten“ (v1.32, `kalender.html`):** Gleiches Problem wie v1.30, aber im Admin-Modal: `#verein-edit-modal input{height:38px}` + Klasse `.ve-sel` für `<select>` (38 px, `appearance:none`, eigener Chevron). Selects in Flex-Zeilen (`#ve-transfer-key`) brauchen `min-width:0`, sonst schiebt ein langer Vereinsname den Nachbarknopf aus dem Dialog. Neue Selects im Modal ⇒ `class="ve-sel"`. Der zweite Dialog im Tab „Vereine“ (`#vd-rubrik`) ist davon nicht erfasst.
- **`<textarea>` auf Server-Seiten:** `_CSS` setzt `font-family:inherit` (sonst Monospace).
- **Neue Checkbox `zugangsdaten_notiert`:** Pflicht, serverseitig geprüft (`elif not zn:`). Wird in `form_data` NICHT zurückgegeben (kein Preserve nötig – ist nach Submit weg).
- **Client-Validierung:** JS in `<script>`-Tag am Ende des Formulars. f-String → `{{` für JS-Objekte, `\d{{5}}` für Regex. Checkbox-Fehler highlightet `.chk`-Wrapper (nicht das Input selbst).
- **Passwort-Toggle (seit v1.28 an jedem Passwortfeld):** `services/auth/routes.py::_PW_TOGGLE_JS` wird von `_page()` in **jede** Server-Seite eingehängt (Login, Registrierung, Passwort ändern/zurücksetzen, Verein-Dashboard) und ergänzt `input[type=password]` selbst um `.pw-wrap` + `.pw-toggle` (Lucide eye/eye-off). Felder, die schon in `.pw-wrap` stehen, werden übersprungen. **Neue Passwortfelder brauchen nichts weiter**, solange die Seite über `_page()` läuft. Der Admin-Zugang in `kalender.html` hat seinen eigenen Knopf (`.pw-eye`, `togglePwEye()`). `tabindex="-1"`, `aria-pressed`, Rücksetzung auf „verborgen" bei `pageshow` (Bfcache).
- **Header-Button „Login"** (seit v1.26, `#verein-login-btn`, Ziel `/verein/login`): erscheint nur ohne Session, eingeloggt ersetzt ihn der Stift `#verein-admin-btn`. Er führt zum **Vereins**-Login, nicht zum Admin-Zugang (der bleibt `/admin` bzw. das Import-Symbol). Unter 480 px entfallen Titelzusatz und Versionsblock (`.hdr-ver` trägt inline `display:flex` → die Regel braucht `!important`). nginx begrenzt `/verein/login` (429 nach etwa 6 schnellen Aufrufen) – beim Testen mit curl daran denken.

## Filterlogik kalender.html – wichtige Pitfalls

- **Rubrik-Filter:** Kein „Alle"-Toggle. Kein aktiver Chip = alle Rubriken sichtbar
- ~~**Favoriten-Priorität:** Aktiver Favorit-Chip überschreibt Verein-Dropdown + Rubrik-Filter~~ – entfallen seit v1.49 (ADR-024), Favoriten sind normale Filter
- **Selbstverwaltungs-Schutz:** `_meta[key].selbstverwaltung = true` → Admin-Dialog vor Edit/Delete; heimat-Import überspringt diese Vereine
- **Landkreis-Fallback:** Vereine ohne `meta[key].landkreis` → `"Landkreis Landshut"`
- **Filter-Reihenfolge:** aktVereine → Rubrik → Landkreis → Zeitraum → Suche → Ortschaft → Favoriten
- **Gemeinde-Identität (seit 2026-09-20, v1.9, ADR-005):** Ein Gemeinde-Filter ist intern **normalisierter Name + Landkreis** (Schlüssel `"Postau|Landkreis Landshut"`), nie der Rohstring aus `_meta.gemeinde`. `_prepareGemeinden()` (nach `meta=j.meta` in `load()`) baut `_gemOf[verein_key]` = Schlüssel; `_gemNormName()` entfernt Präfix `Gemeinde/Markt/Stadt` und Zusatz `bei …`/`b. …`. Anzeige `_gemLabel(key)` = Name, Landkreis-Zusatz „(Rottal-Inn)" nur bei Namensgleichheit in mehreren Landkreisen. **Alle** Gemeinde-Vergleiche (Render-Filter, Chips, Pills, Badge, Favoriten, iCal-Feed, Ortschaft→Gemeinde) laufen über `_gemOf`, nie über `meta[k].gemeinde`. Alte localStorage-Einträge (Favoriten `type:"gemeinde"`, `vk_state.aktGemeinden`) werden beim Laden per `_gemKeysForOld()`/`_migrateGemFavorites()` migriert. **Neue Stellen, die nach Gemeinde filtern, müssen `_gemOf` nutzen.**
- **Ortschaft-Identität (seit 2026-09-20, v1.11, ADR-006):** Ortschaft = Ortsname + Gemeinde. Chip-Schlüssel = Anzeige-Label: `"Weng"`, bei Namensgleichheit in mehreren Gemeinden `"Weng (Postau)"` (bei mehrdeutiger Gemeinde mit Landkreis). `_prepareOrtschaften()` (nach `_prepareGemeinden()` in `load()`) baut `_ortOf[verein_key]` = Label und `allOrteAll[label]` = Vereine mit Termin-Label; **alle** Ortschaft-Vergleiche (Render-Filter, Vereinsliste, Favoriten, Such-Chips, iCal-Favoriten-Feed, `_vereinsForOrt`) laufen über `_ortOf`, nie über `meta[k].heimatort` oder `allOrteAll[o].includes(k)`. Ortsname = `meta.heimatort` sonst letztes Label-Wort (>4 Zeichen, `_ortNameOf`). Vereine ohne Gemeinde → größte Gemeinde-Gruppe des Ortsnamens. Migration alter Namen nur bei Kollision (`_ortKeysForOld`, `_migrateOrtFavorites`). **Override:** `_meta[key].ortschaft_gemeinde` (Gemeindename) ersetzt die Gemeinde nur für die Ortschaft-Zuordnung – nötig bei Postau: `kindergartenverein_weng_postau_e_v_fuer_postau` (Sitz Gemeinde Weng, betreut Ortschaft Postau) trägt `ortschaft_gemeinde: "Gemeinde Postau"`, sonst zerfiele Postau in „Postau (Postau)"/„Postau (Weng)". Kein Admin-UI; setzen per Server-Script (`KalenderStore.update()` als `webhook`, Backup vorher). **Pitfall:** Ein Import/Transfer, der `_meta[key]` komplett neu schreibt, würde das Override löschen.
- **Filter-Kaskade Region → Gemeinde → Ortschaft → Verein (seit 2026-09-20, v1.12, ADR-007):** Jede Ebene bietet nur an, was zu den darüber gewählten Filtern passt – zentral in `_applyHierarchy()` (setzt Chip-Sichtbarkeit + `on`-Zustand von Gemeinde-/Ortschaft-Chips, wählt unsichtbar gewordene Auswahlen ab, ruft `filterVereinListForOrt()`). Sichtbarkeit: `_gemVisible(g)` (Region), `_ortVisible(o)` (gewählte Gemeinden, sonst Region; nutzt `_ortGem[label]` = effektive Gemeinde inkl. `ortschaft_gemeinde`-Override). Aufrufe: `selectLandkreis`, `toggleGemeinde`, `resetAll`, **und am Anfang von `applyStateToUI()`** (deckt State-Laden und jeden Chip-Neubau nach Favoriten-Toggle ab). **Pitfall:** Vor v1.12 gab es drei Einzelfunktionen (`filterOrtChipsForGemeinde/Landkreis`, `filterGemeindeChipsForLandkreis`), die sich gegenseitig das `display` überschrieben und nach Reload/Chip-Neubau nicht liefen. **Neue Filterebene → in `_applyHierarchy()` einhängen**, nicht wieder eigene `display`-Logik.
- **Region = Landkreis-Name ohne Präfix (v1.12):** `aktLandkreis`, `data-lk`, Region-Favoriten (`type:"region"`) und `vk_state.aktLandkreis` enthalten jetzt `_regionOf(landkreis)` = Wert ohne `Landkreis/Stadt/Kreis`-Präfix („Landshut" umfasst Landkreis **und** Stadt Landshut). `_regionForVerein(k)` = Region mit Fallback `"Landkreis Landshut"` (gilt jetzt auch im Render-Filter; früher wurden Vereine ohne `landkreis` bei aktiver Region ausgeblendet). Gemeinde-Schlüssel (`Name|voller Landkreis`, ADR-005) bleibt unverändert – Stadt und Landkreis bleiben dort verschieden. Alte Werte in localStorage werden beim Laden migriert (`_migrateRegionFavorites`, `loadState`). Region-Section erscheint weiter nur bei ≥2 Regionen (heute: nur Landshut → ausgeblendet). `buildLandkreisFilter()` baut den Container jetzt neu auf (vorher Chip-Dubletten beim Region-Favorit-Toggle).
- **Chip-/Titel-Größen im Filter (v1.14–v1.16, 2026-09-20, live im Chrome gemessen):** `.filter-title` = 15px/700 in `var(--text)` (wie `.ev-title`, vorher 12px/600 in Akzentfarbe → wurde übersehen). **Alle Toggle-Chips der Filter-Card haben exakt 34px Höhe** (Zeitraum, Region, Gemeinde, Ortschaft, Rubrik, Favoriten-Chips, Button „Favoriten ausschalten"); Breite richtet sich nach dem Inhalt. Umgesetzt als eine Gruppenregel `.filter-card .chip,.filter-card .rubrik-chip,.fav-hint .fav-hint-btn{box-sizing:border-box;height:34px;padding-top/bottom:0;display:inline-flex;align-items:center;gap:5px}` (Spezifität 0,2,0, damit spätere Einzelregeln wie `.rubrik-chip{padding:…}` sie nicht überstimmen). Vorher: 32 / 32,5 / 33,1 / 37,5px (Favoriten-Chips wegen des gespeicherten 18px-Icons; jetzt `#fav-bar .chip>svg` 14px – das gespeicherte SVG selbst bleibt unverändert, das Entfernen-Kreuz ist verschachtelt und davon nicht betroffen). **Pitfall:** Die Gruppenregel setzt `display:inline-flex`; der Ausschalten-Button braucht deshalb `.fav-hint .fav-hint-btn{display:flex;width:fit-content}`, sonst steht er inline hinter dem Text (Regression v1.15, in v1.16 behoben). **Neue Chip-artige Elemente in der Filter-Card:** unter `.filter-card` mit Klasse `.chip` bauen bzw. in die Gruppenregel aufnehmen, nicht mit eigenen Höhen/Paddings abweichen. **Aktive Filter-Pills (`.act-pill`, `#active-pills`, seit v1.17):** ebenfalls 34px, Radius 20px, Schrift 13px, Entfernen-Kreuz als Lucide-`x` (14px). Sie zeigen die gesetzten Filter, weil die Filter-Card standardmäßig eingeklappt ist (`filterOpen=false`): Zeitraum, Region, Gemeinde, Ortschaft, „N/M Vereine". **Es gibt bewusst keine Rubrik-Pill:** die Rubrik-Chips liegen außerhalb des eingeklappten Bereichs (`#rubrik-bar`) und zeigen ihren Zustand selbst; die Pill war redundant und hieß irreführend „Verein" statt „Vereine". In der Favoriten-Ansicht entfallen die Pills der pausierten Filter (nur Zeitraum bleibt). Nicht betroffen (bewusst): Verein-Checkbox-Liste, „Alle/Keine"-Textlinks im Vereinsbereich.
- **Favoriten = Schnellauswahl (seit v1.49, ADR-024; ersetzt die Favoriten-Ansicht aus ADR-008):** Herz merkt nur vor (`toggleFav*`, Liste `vk_favorites` – auch für Kalender-Abo/Telegram). Favoriten-Chip antippen → `toggleFavChip()` setzt/löst den normalen Filter (`toggleOrt`/`toggleGemeinde`/`selectLandkreis`/`toggleVereinFilter`). Chip-Zustand **abgeleitet** aus den Filtern (`_favAktiv`), `renderFavBar()` läuft in `updateAllBadges()` mit – keinen eigenen aktiv-Zustand wieder einführen. `activeFavs`/`vk_active_favs`, `_favMode`, `leaveFavMode`, Hinweisbox `#fav-hint` und „pausiert“ gibt es nicht mehr; der alte Schlüssel wird beim Laden gelöscht. Entfernen-Kreuz = Lucide-`x`.
- **Zeitraum-Werte (2026-09-20):** `zukunft` | `-14` | `-30` (ab heute−N Tage **bis in die offene Zukunft**) | `30`/`90`/`365` (vorwärts begrenzt, „Nächste 30 Tage") | `alle`. Negativer Wert = Rückblick, Filter in `render()` über `parseInt(zeitraum)<0`. Neuer Wert → `ZEIT_LBL`, Chip (`data-z`), Info-Text (Zeile „Zeitraum") anpassen.
- **Offline:** `try/catch` um `/api/termine`-Fetch → Meldung „🔇 Keine Internetverbindung"
- **Suche-Dropdown muss `position:fixed` sein:** `.content{overflow-y:auto}` erstellt in iOS Safari einen eigenen Stacking-Context – ein `position:absolute` Dropdown mit `z-index:100` liegt trotzdem dahinter. Lösung: `position:fixed` + Position per `_positionDropdown()` via `getBoundingClientRect()` berechnen. Dropdown-Items als `<button>` statt `<div>` – `onclick` auf `<div>` ist auf iOS unzuverlässig.

---

## ⚠️ Dateiname-Prüfung: Schreib- und Leseseite müssen identisch sein (Vorfall 2026-09-27)

`rename_via_claude()` validiert den von Claude vorgeschlagenen Namen mit **derselben**
Funktion `is_already_renamed()`, die auch `process_changes()` benutzt. Das ist keine
Doppelung, sondern Absicht – siehe `ADR/ADR-011`.

**Warum:** Vorher akzeptierte die Schreibseite jeden Namen mit `^\d{4}[-_]`, die Leseseite
verlangte `YYYY-MM-DD_` oder `YYYY_`. `2026-11_Pfarrbrief.pdf` passierte die Umbenennung,
wurde danach nicht als fertig erkannt – und **die Umbenennung selbst löst den nächsten
Dropbox-Webhook aus**. Am 2026-09-27 wurde dieselbe Datei so dreimal umbenannt
(`2026-11_Pfarrbrief` → `2026-11_bis_2026-12_Pfarrbrief` → `2026_Pfarrgemeinde_Pfarrbrief`),
drei Claude-Calls statt einem, und im Endnamen fehlte das Datum komplett.

**Konsequenz für künftige Änderungen:** Wer im Rename-Prompt eine **neue Namensform**
einführt (wie damals `YYYY_Vereinsname_Jahreskalender`), muss `is_already_renamed()`
mitändern. Sonst lehnt der Service korrekt erzeugte Namen ab und benennt nicht mehr um.
Testrezept ohne API: Funktion per `ast` aus der Datei ziehen, gegen alle im Prompt
beschriebenen Formen prüfen (Rechnung, Pfarrbrief-Zeitraum, Jahreskalender, Kontoauszug)
**und** gegen Scanner-Rohnamen, die weiterhin als „offen" gelten müssen.

Bei schemawidrigem Vorschlag wird nicht umbenannt: Telegram-Warnung, Datei bleibt liegen.
Das ist der ruhige Zustand – ohne Ordneränderung kommt kein neuer Webhook, also kein
weiterer Call.

---

## Pfarr-Termine: zwei Quellen, Ortschafts-Filter am Termin (ADR-014)

**Zwei unabhängige Einspeisewege** beschreiben dieselbe Pfarrgemeinde:
`pfarrbrief_manager.py` → `gottesdienste.json` (`hk`/`pk`/`ok`) und der Kalender-Import über
Dropbox → `vereinstermine.json` unter `pfarrgemeinde*`. Bis 2026-09-30 unterdrückte ein
Guard die erste Quelle vollständig, sobald die zweite irgendeinen `pfarrgemeinde*`-Key
hatte – auch einen ohne künftige Termine. Seit ADR-013 werden beide über
`shared/kalender_core.py::gottesdienste_eintraege()` zusammengeführt (Dublettenschlüssel
`datum + uhrzeit + ort`), API und iCal-Feed nutzen dieselbe Funktion.

**Ortschafts-Filter hängt seit 2026-10-02 am Termin, nicht mehr am Verein (ADR-014, Stufe 0–2 live).**
`/api/termine` liefert pro Termin `_geo` (`orte`, `ortschaften`, `plz`, `gemeinden`, `landkreise`,
`bundeslaender`); `kalender.html::_terminOrte(t)` macht daraus Chip-Labels und ersetzt
`_ortOf[t.verein]` im Haupt-Terminfilter, den Such-Chips und den Favoriten. `_ortOf`
(Heimatort des Vereins) bleibt für die Kaskade und die Vereinsliste und ist der Rückfall, wenn
`_geo` fehlt. **Seit v1.61 (Stufe 3 + ADR-025 Mischregel):** Ortschaft, Gemeinde und Region zählen am Ort des Termins **und** am Vereinssitz, Pfarreien nur am Ort (`_terminOrte`/`_terminGems`/`_terminRegionen`, `_mitVereinssitz`). Server-Gegenstück für das Abo: `shared/geo.py::termin_orte_misch()`/`abo_treffer()` – **beide Seiten zusammen ändern**, Tests `tests/test_app.py` Abschnitt 19.
**heimat-Import (v1.62, Stufe 4):** Schreibt `ortschaft` nur noch, wenn eine erkannt wurde – vorher immer den Namen der heimat-Seite (= Gemeinde), 325 Bestandstermine tragen das noch (bewusst nicht migriert, ADR-014). Die Kartenmarke zeigt deshalb `_geo.orte` (Ort des Termins, ohne Vereinssitz) und nur ohne `_geo` das Feld.
Bei „Termin ist in der API, erscheint aber nicht in der App" zuerst `t._geo.orte` ansehen und
dann `python3 tests/test_geo.py` (Abschnitt „Veranstaltungsorte ohne Registertreffer").
Der iCal-Feed filtert `?ort=`/`o=`/`g=`/`r=` seit v1.61 nach der Mischregel (ADR-025); `?v=` allein
bleibt Verein-basiert (alte Favoriten-Abos liefern unverändert).

**Dateien der Geo-Zuordnung (ADR-014, live seit 2026-10-02):**

| Datei | Zweck |
|---|---|
| `orte.json` | **Ortschaften** (amtlich), 114 Einträge (seit v1.39 inkl. 51 Gemeindeteile Mallersdorf-Pfaffenberg, Landkreis Straubing-Bogen – `quelle`, BayernAtlas-Bestätigung offen) mit PLZ, Gemeinde, Landkreis, Bundesland, `hauptort`, `alias`, `geprueft`/`quelle` |
| `orte_frei.json` | **Orte** (alles Mögliche: Lokale, Gebäude, falsche Schreibweisen) → Ortschaft. Winklmoos → Hölskofen. Wird vor `orte.json` geprüft |
| `shared/geo.py` | `geo_fuer_termin()` → `{orte, plz, gemeinden, landkreise, bundeslaender}` |
| `tests/test_geo.py` | Offline-Abnahme gegen `tests/fixtures/termine.json` (inkl. PLZ-Prüfungen) |
| `plz_gemeinden.json` | **PLZ → Gemeinde(n)**, Landkreis, Bundesland, Postorte – bundesweit, aus OpenPLZ (ODbL). Seit v1.31 zusätzlich `t` = Stadtbezirke/-teile aus OSM (`Borough`/`Suburb`, ≥ 2 Straßen, Nummern wie Münchens „11.3“ verworfen) für ~2.800 PLZ – in Landshut/Regensburg/Nürnberg leer. Ein gewählter Stadtteil gilt als bekannt (kein Telegram-Hinweis). Ländliche Ortsteile weiter aus `orte.json`. Neu bauen: `python3 tools/build_plz_gemeinden.py` (lokal, ~230 API-Abrufe) |

```bash
python3 tests/test_geo.py             # Prüfungen
python3 tests/test_geo.py --bericht   # Differenzliste heute vs. neu
python3 tests/test_geo.py --register  # Register mit Herkunft und Nutzung je Ort
```

- **`shared/geo.py` ist bewusst stdlib-only.** `kalender_core` erzwingt beim Import
  `os.environ["CLAUDE_API_KEY"]` und zieht Dropbox/PIL mit – ein Resolver dort wäre ohne
  Secrets nicht lauffähig und damit nicht offline testbar. Neue Logik, die getestet werden
  soll, gehört aus demselben Grund **nicht** in `kalender_core`.
- **Kill-Switch:** Ohne `orte.json` liefert `geo_fuer_termin()` `None` → altes Verhalten.
- **Neue Gemeinde anlegen ⇒ alle ihre Ortschaften mit Gemeinde + PLZ prüfen und eintragen**, auch ohne Termine (Josef, 2026-10-02). Quelle BayernAtlas, ohne Zugriff mit `quelle` markieren und von Josef bestätigen lassen. **Ortschaft ≠ Ort:** Amtliches gehört in `orte.json`, alles andere in `orte_frei.json`. Winkelmoos (Ortschaft, Bayerbach) ≠ Winklmoos (Ort → Hölskofen). `tests/test_geo.py` listet Veranstaltungsorte ohne Treffer und prüft die Registerkonsistenz.
- **Register erweitern ist Faktenarbeit, nicht Codearbeit.** Eine falsche Gemeinde fällt
  nicht auf, sie zeigt still die falschen Termine. Nie aus dem Gedächtnis befüllen.
  Quellenrangfolge (ausführlich in ADR-014):
  1. **BayernAtlas** (`geoportal.bayern.de/bayernatlas`) – amtliche Gemeinde- und
     Gemarkungsgrenzen, für dieses Gebiet die verlässlichste Quelle. Bei Zweifeln fragen,
     Josef schaut dort nach.
  2. **Vereins-Metadaten** – massgeblich für die *Schreibweisen*, nicht für die
     Zugehörigkeit (sie beschreiben teils den Zuständigkeitsbereich eines Vereins).
  3. **Nominatim/OSM** – nur Lückenfüller. Zwei Fallstricke: `municipality` liefert die
     **Verwaltungsgemeinschaft** statt der Gemeinde, `village`/`town` den Ort selbst –
     verlässlich ist der **`display_name`** (Gemeinde steht vor der VGem bzw. vor dem
     Landkreis). Und jede Abfrage braucht Gemeinde + Landkreis im Suchstring, sonst trifft
     sie gleichnamige Orte in ganz Bayern. Kleine Ortsteile fehlen dort ganz.
- **Register ist global, ohne Regionsbezug:** ein Ortsname trifft als Wort in *jedem* Veranstaltungsort, egal welcher Landkreis. Mit Mallersdorf-Pfaffenberg kamen Allerweltsnamen dazu (Klause, Westen, Weinberg, Waldhof, Neuburg, Ried, Holzen, Winkl …): „Gasthaus zur Klause, Ergoldsbach" würde zusätzlich der Mallersdorfer Einöde Klause zugeordnet. Am 2026-10-04 gegen alle 519 Live-Termine geprüft: kein Fehltreffer. Vor dem Eintragen einer weiteren Gemeinde dieselbe Prüfung machen (Namen gegen `/api/termine`-Orte). **Seit v1.40 abgefedert:** `geo_fuer_termin()` behält bei Treffern aus mehreren Landkreisen nur die im Landkreis des Vereins (`_meta.landkreis`). Ein *einzelner* fremder Treffer bleibt (Vereinsausflug) – „Gasthaus zur Klause“ ohne Ortsnamen landete also weiter in Mallersdorf.
- **Gebäude ohne Ortsnamen:** in `orte_frei.json` nur eindeutige Namen (Haus der Generationen/HDG, Gasthaus Ganser, Sportzentrum Igeltal → Mallersdorf). **Nie Allerweltswörter wie „Rathaus“** – das träfe jedes Rathaus. Stattdessen den Heimatort des Vereins setzen: `markt_mallersdorf_pfaffenberg` hat `_meta.heimatort = "Mallersdorf"` (Rathaus liegt dort, Josef 2026-10-04), seine Termine ohne Ortsnamen fallen darauf zurück.
- **Admin-Tab „Orte“ (seit v1.42, ADR-021):** Zuordnungen stehen in `vereinstermine.json` unter `_orte_zuordnung` – Liste `{ort, gemeinde, verein, ortschaft, am}`, `verein` leer = gilt für alle Vereine der Gemeinde (`gemeinde` per `_gem_norm`, ohne „Markt“/„Gemeinde“). Treffer nur bei **exaktem** Ortstext (Groß/Klein, Leerzeichen egal, kein Teiltreffer); Vereins-Zuordnung schlägt Gemeinde-Zuordnung. Reihenfolge in `geo_fuer_termin()`: Ortsname im Text → Admin-Zuordnung → PLZ-Schwanz → Feld `ortschaft` → Heimatort. Neues Feld `_geo.quelle` (`ort`/`zuordnung`/`ortschaft`/`heimat`/leer) – der Tab listet alles außer `ort`/`zuordnung`, also auch nur *geratene* Ortschaften. **Jeder Aufrufer muss `raw.get("_orte_zuordnung")` mitgeben** (heute `/api/termine` und iCal `?ort=`), sonst greifen die Zuordnungen dort nicht. `orte_frei.json` bleibt für fest kuratierte, eindeutige Namen (Wortsuche, überall). **Ausflugsziele (v1.43):** Zuordnung mit `ausflug: true` (Knopf „Ausflugsziel“) – der Termin behält die Ortschaft aus Feld/Heimatort (Josefs Wahl: Treffpunkt), `quelle` wird `ausflug`, und der Ort verschwindet aus der offenen Liste.
- **Register prüfen (v1.62, Todo #417):** Unter den Zuordnungen im Tab „Orte“ stehen die Einträge aus `orte.json` mit `quelle`, aber ohne `geprueft` (Stand 2026-10-05: 70, davon 51 Mallersdorf-Pfaffenberg). „Stimmt“/„Falsch“ (Notiz Pflicht) → `POST /api/admin/register`, Rücknahme `DELETE`. **Urteile stehen in `vereinstermine.json` unter `_orte_geprueft`** (`{ort, gemeinde, ok, notiz, am}`), **nie in `orte.json`** – das liegt im Git, ein Schreibzugriff des Servers ließe den nächsten `git pull` scheitern. **Pflege-Pitfall:** „Falsch“-Meldungen sind Aufgaben für die Register-Pflege – `orte.json` lokal korrigieren, `geprueft` setzen, deployen, danach das Urteil per `DELETE` entfernen. Bestätigte Einträge gelegentlich in `orte.json` als `geprueft` übernehmen. BayernAtlas (atlas.bayern.de) nimmt **keine Suche per URL** an (`swisssearch`/`q` getestet) – der Knopf kopiert den Suchtext und öffnet die Karte. Gemeinsame Logik in `shared/admin_aufgaben.py` (stdlib + `shared.geo`): `offene_orte()` (auch für `/api/admin/orte`), `register_pruefung()`, `offene_aufgaben()`, `aufgaben_text()`.
- Bestätigte Einträge tragen `geprueft: "<Datum>"`; `--register` weist sie aus, damit sie
  nicht erneut geprüft werden.
- **`t.ortschaft` ist als Ortsangabe unzuverlässig:** `heimat_import.py:398` schreibt dort
  die Gemeinde, wenn kein Ort erkannt wurde (`e.get("ortschaft","") or e["_gemeinde"]`).
  Unter den künftigen Terminen stand dort 41× „Ergoldsbach" und 38× „Bayerbach", aber nur
  2× „Hölskofen". Nie ungeprüft als Ort übernehmen.

---

## ⚠️ pfarrbrief_manager.py – Jahresbezug und stille Filter (Vorfall 2026-09-27)

Aufruf ist **manuell**, mit dem Dropbox-Pfad als Argument – es gibt keinen automatischen
Trigger:

```bash
/opt/rename-webhook/bin/python3 /opt/rename-webhook/pfarrbrief_manager.py '/Dokumente/Pfarrbriefe/<datei>.pdf'
```

**Der Vorfall:** 89 Termine erkannt, 21 davon in Hölskofen/Paindlkofen/Oberköllnbach –
und **null gespeichert**, während die Telegram-Nachricht „Pfarrbrief verarbeitet" meldete
und die Termine auflistete. Der Prompt gab kein Bezugsjahr mit; bei Einträgen wie
„So., 1. November" schloss das Modell vom **Wochentag** auf das Jahr und landete bei
**2009**. `merge_termine()` filtert `datum >= heute` und verwarf damit korrekt alles – nur
sagte es niemandem. Erfolgsmeldung ohne Ergebnis.

**Die Streuung ist der eigentliche Befund:** Dieselbe Datei lieferte in drei Läufen drei
verschiedene Jahre – **2009** (ohne Jahresangabe im Prompt), **2020** (mit heutigem Datum und
ausdrücklichem Verbot des Wochentags-Rückschlusses) und **2026** (korrekt, derselbe Prompt).
Ein verschärfter Prompt verringert die Wahrscheinlichkeit, schließt den Fehler aber nicht aus.
Deshalb bestimmt jetzt der Code das Jahr – siehe `ADR/ADR-012`.

**Was jetzt gilt:**
- **`normalisiere_jahre()` setzt das Jahr, nicht das Modell.** Tag und Monat bleiben, das Jahr
  wird auf das **nächste Vorkommen** gesetzt (Toleranz: 30 Tage in die Vergangenheit,
  `_JAHR_TOLERANZ_TAGE`). Über den Jahreswechsel hinweg korrekt: November → dieses Jahr,
  Januar → nächstes. Ein 29.02. wandert ins nächste Schaltjahr. Unparsbare oder leere
  Datumsfelder bleiben unangetastet. Idempotent – ein schon korrektes Jahr bleibt stehen.
- **`save_raw_dump()` sichert das Rohergebnis** nach `pfarrbrief_last_raw.json`, **bevor**
  daran gerechnet wird. Damit sind Änderungen an der Jahreslogik gegen echte Modellausgaben
  prüfbar, **ohne** einen neuen Claude-Call. Ohne diesen Dump kostete jede Iteration Geld
  (am 2026-09-27 drei Läufe auf einer Datei). Die Datei hält nur den **letzten** Lauf – für
  einen Vergleich vorher wegkopieren.
- Der Prompt nennt weiterhin das heutige Datum und verbietet den Wochentags-Rückschluss –
  als erste Verteidigungslinie, nicht als Verlass.
- **Plausibilitätsprüfung vor Speichern und Verschieben:** Liegt kein einziger extrahierter
  Termin in der Zukunft, ist die Extraktion gescheitert und nicht der Pfarrbrief alt. Das
  Skript bricht ab, speichert nichts, **verschiebt nichts** und meldet die gefundenen Jahre.
  Die Datei bleibt liegen und kann nach einer Prompt-Korrektur erneut verarbeitet werden.
- `merge_termine()` sammelt verworfene Termine in einer Liste und nennt sie in der Meldung.
  Bewusst nur die **neu gelieferten** – zählte man über `bestehende`, meldete jeder normale
  Lauf die inzwischen abgelaufenen Termine aus der Datei als „ignoriert" (Dauer-Fehlalarm).
- `save_gottesdienste()` legt vorher `gottesdienste.json.bak` an. `write_text()` überschreibt
  direkt; am 2026-09-27 ging der Altstand bei einem manuellen Lauf verloren.

**Zum Merken:** `gottesdienste.json` gehört `webhook:webhook`. `write_text()` schreibt
in-place, ein Lauf als `root` kippt die Rechte also **nicht** – ein atomarer Write per
`tempfile` + `os.replace()` würde es dagegen tun. **Neu angelegte Dateien sind aber sehr wohl
betroffen:** `gottesdienste.json.bak` und `pfarrbrief_last_raw.json` entstanden bei einem
root-Lauf am 2026-09-27 als `root:root` und wurden nachträglich auf `webhook:webhook` gesetzt.
Nach einem manuellen Lauf als `root` deshalb `ls -la /opt/rename-webhook/` prüfen – sonst
scheitert ein späterer Lauf als `webhook` still am Schreiben (der Backup-Fehler wird nur als
Warnung ausgegeben). `pfarrbrief_last_raw.json` steht in `.gitignore`, damit `git status` im
Deployment leer bleibt (Regel aus PKA-Todo #410).

### ⚠️ Der Ort steht nur in der ersten Zeile des Tages (Vorfall 2026-09-30)

Im Pfarrbrief hat ein Tag mehrere Zeilen, aber der Ortsname steht **einmal**:

```
Dienstag, 06.10
Hölskofen 18.30 Oktoberrosenkranz
19.00 hl. M. Alfons und Erika Gahr f. + Eltern
```

Die 19:00-Messe findet in Hölskofen statt – die Zeile beginnt direkt mit der Uhrzeit.
Das Modell hat diesen Bezug nicht hergestellt und dort **konstant „Hölskofen" eingesetzt:
25 Messen statt 2.** Begünstigt hat das der Prompt selbst, der lautete: „Achte besonders
auf Ortsangaben wie Hölskofen und Paindlkofen." Die Rangfolge der Falschtreffer entsprach
exakt der Reihenfolge der Nennung (Hölskofen 25×, Paindlkofen 11×) – klassisches Priming.

**Regel: Im Prompt keine Ortsnamen als „besonders beachten" nennen.** Was man dem Modell
als wichtig verkauft, setzt es im Zweifel ein, statt die Lücke zu melden. Der Prompt
erklärt jetzt stattdessen die Ortsvererbung innerhalb eines Tages, weist „Keine
Abendmesse" als terminlosen Tag aus und verlangt `""` statt eines geratenen Orts.

**`pruefe_ortsverteilung()` als zweite Verteidigungslinie** warnt (ohne Abbruch), wenn ein
Ort den zweithäufigsten um Faktor 1,8 übertrifft. Geprüft wird bewusst der **Abstand zum
Zweiten, nicht der Anteil am Ganzen**: Hölskofen lag bei 27 % von 91 Terminen und wäre
unter einer 30-%-Schwelle durchgerutscht. Gemessen an echten Daten – kaputter Lauf 25 zu 11
(schlägt an), korrekter Pfarrbrief 16 zu 15 (bleibt still). „Keine Abendmesse" wird vorher
herausgefiltert, sonst stünde es mit 13 selbst auf Platz 2.

**Zur Fehlersuche:** Das PDF ist ein reiner Scan (`get_text()` liefert 0 Zeichen auf allen
vier Seiten) – deshalb Vision. Zum Gegenlesen **ohne** neuen API-Call die Seiten rendern
und selbst anschauen; die Seiten sind gedreht, Seite 1/3 brauchen `prerotate(270)`,
Seite 2 ebenfalls, Seite 4 `prerotate(180)`:

```python
import fitz
d = fitz.open("…/2026_Pfarrgemeinde_Pfarrbrief.pdf")
pix = d[0].get_pixmap(matrix=fitz.Matrix(2.6, 2.6).prerotate(270))
```

**Weitere Fallen, alle am 2026-09-27 aufgetreten:**
- **API-Timeout war 60s** – zu knapp für ein 2,7-MB-PDF mit ~90 Terminen, der Lauf endete im
  `TimeoutError`. Jetzt 300s. **Bewusst kein automatischer Retry:** der Call wird trotzdem
  abgerechnet, ein stiller Wiederholungsloop würde unbemerkt Geld verbrennen.
- **Selbst-Move:** Bei einem Wiederholungslauf liegt die Datei schon in
  `/Dokumente/Pfarrbriefe`. Ohne Prüfung hätte `files_move_v2(autorename=True)` eine Dublette
  `… (1).pdf` angelegt. Quelle == Ziel wird jetzt erkannt.
- **Die Extraktionsmenge schwankt** (89 / 67 / 91 Termine bei derselben Datei). Wer Zahlen
  zwischen Läufen vergleicht, vergleicht keine stabile Größe.

---

## ⚠️ Globale Funktionen nicht durch lokale Variablen verdecken (Vorfall 2026-09-27)

`kalender.html` hat **107 globale Funktionen** in einem einzigen `<script>`-Block. Eine lokale
Variable mit demselben Namen verdeckt die Funktion im ganzen Funktionsrumpf – JavaScript meldet
das nicht, der Fehler taucht erst zur Laufzeit auf.

**Der Vorfall:** In `startUpload()` und `doConfirmImport()` stand

```js
const icon = document.getElementById("uz-icon");   // verdeckt die Funktion icon()
icon.innerHTML = icon("timer", 32);                // TypeError: icon is not a function
```

Die lokale `const icon` gab es seit dem Initial Commit; kaputt wurde sie erst mit `835f530`
(2026-06-08), als die Lucide-Umstellung die globale Funktion `icon(name,size)` einführte.

**Warum es so lange unbemerkt blieb:** Der TypeError fliegt **vor** dem `try`-Block – also
nachdem die Fortschrittsanzeige eingeblendet wurde, aber bevor `fetch("/upload")` startet. Der
Upload sieht deshalb nicht nach Fehler aus, sondern nach „hängt": stehen bleibt der statische
Default-Text **„Verarbeite…"** aus `<div class="prog-lbl">`. Kein Fehler-Icon, keine Meldung,
kein Request im Netzwerk-Tab. Der Admin-Upload war so **knapp vier Monate** unbenutzbar.

**Regel:** Lokale DOM-Referenzen bekommen das Suffix `El` (`iconEl`, `lblEl`), nie den Namen
einer globalen Funktion. Gilt besonders für kurze Namen wie `icon`, `lbl`, `f`, `l`.

**Prüfung vor dem Deploy** (statisch, kein Browser nötig): Script-Block extrahieren, alle
globalen Funktionsnamen sammeln (`^function name(` und `^const name = (…) =>`), dann je
Funktionsrumpf prüfen, ob eine lokale `const/let/var` einen dieser Namen belegt **und** derselbe
Name im selben Rumpf als Funktion aufgerufen wird. Nur diese Kombination ist ein echter Fehler –
eine bloße Namensgleichheit ohne Aufruf ist harmlos (in dieser Datei 21 Namensgleichheiten, davon
2 echte Fehler). Danach `node --check` auf den extrahierten Block.

Dasselbe Muster ist am selben Tag im Vokabeltrainer aufgetreten (dort `tr()` statt `t()`, weil
`t` viermal als lokaler Parameter belegt war) – siehe `PKA/BKM/PWA-Standards.md`.

## Upload-Workflow (zweistufig)

1. PDF/Foto → Claude Vision extrahiert Termine (verein, datum, ort, ortschaft, bezeichnung)
2. Neue Vereine ohne Heimatort → Admin gibt Heimatort ein
3. Admin bestätigt → `/api/confirm-import` speichert Termine + `heimatort` in `_meta`

**Vereinsadmin-Upload:** `/verein/upload` – PDF/Foto oder Excel (5 Spalten, verein aus Session); Rate-Limit 3/Tag; Quota vor Claude-Call erhöht.

---

## Besucherstatistik

- **Script:** `stats_collector.py` (täglich 00:05 via Cron)
- Liest nginx-Log, zählt anonymisierte Unique-IPs (/24 IPv4, /48 IPv6), schreibt in `page_stats`
- **Backfill:** `python3 stats_collector.py --backfill 90`
- **iCal-Feed-Tracking:** `_track_ical_request()` in `services/kalender/routes.py`
- **Primäre Metrik:** 🇩🇪 Deutschland-Besucher (aus `page_stats_geo`) – sowohl im Telegram-Report als auch in den Stats-Kacheln der Admin-PWA. Gesamt-Besucher (inkl. Bots mit Browser-UA) wird nicht mehr prominent angezeigt.
- **Bekannte Bot-Muster (2026-06):** Tencent-Cloud-Scanner nutzt iOS-13.2-UA mit ~18 wechselnden IPs/Tag → `CRAWLER_UA` enthält `"iphone os 13_2"`. Auch `cms-checker` und `meta-externalagent` geblockt. Non-DE-Traffic (USA, NL, SE täglich) ist größtenteils automatisiert – kein Handlungsbedarf solange DE-Zahlen plausibel.

### GeoIP-Herkunftsstatistik

- **DB:** `page_stats_geo` (datum, land, stadt, besucher) – unique Besucher pro Geo-Kombination
- **Lookup:** `GeoLite2-City.mmdb` unter `/opt/rename-webhook/` – deutsche Namen via `.names.get("de")`
- **DSGVO:** GeoLookup passiert **vor** der IP-Anonymisierung; nur aggregierte Geo-Daten gespeichert
- **API:** `GET /api/admin/stats/geo?d=7|30|365` → `{laender, staedte_de}`
- **Stadt:** Nur für Deutschland (`iso_code == "DE"`)
- **Auto-Update:** `update_geoip.sh` monatlich am 1. um 04:00 (root-Crontab, seit 2026-10-04 über `cronwrap.py geoip_update`, vom Cron-Wächter überwacht – Takt `monatstage: [1]`), Log: `/var/log/pka-geoip.log`. Crontab-Sicherung davor: `/root/vor-cronwrap-geoip-20261004.txt`
- **Key:** `GEOIP_LICENSE_KEY` in `/etc/pka/secrets.env` (MaxMind-Account erforderlich)

---

## Privater Telegram-Bot (services/telegram/routes.py)

Endpunkt `/telegram` – nur Josefs Chat-ID. Token = `TOKEN` aus `/etc/pka/secrets.env`.

| Befehl | Beschreibung |
|--------|--------------|
| `/help` | Alle Befehle |
| `/status` | Server-Status |
| `/sicherheitscheck` | security_check.sh ausführen |
| `/update` | Sicherheitsupdates einspielen |
| `/reboot` | Server neu starten |
| `/pfarrbrief` | Bevorstehende Gottesdienste |
| `/verein` | Alle Vereinstermine |
| `/termine-30` | Nächste 30 Tage |
| `/verkehr <Adresse>` | Verkehrsinfo via TomTom (`shared/routing.py`, seit 2026-09-20; vorher Google Directions) |
| `/heimat` | heimat-info.de Import auslösen |
| `/heimat-add <url>` | Neue Gemeinde via Playwright entdecken |
| `/stopp-vko` / `/start-vko` | Wartungsmodus ein/aus |
| *(beliebiger Text)* | → `Todos.json` als `kategorie: pka` |

**Todo-Schema (`_save_todo()`):** Das Schema gehört dem Ziel-Projekt, nicht diesem Repo – maßgeblich ist `~/Developer/PKA-Todos/CLAUDE.md`; dort sind drei Vorfälle mit fehlenden bzw. falsch benannten Feldern dokumentiert. Seit 2026-09-27 (PKA-Todo #409): `prio: "mittel"` statt `"niedrig"` (Telegram **und** Siri-Webhook laufen über dieselbe Funktion), dazu `faelligkeit` und `faelligkeit_uhrzeit` explizit auf `None`. Vor jeder Änderung an diesem Dict die PKA-Todos-CLAUDE.md lesen.

**Pitfall – Callback-Guard:** Bei `callback_query`-Updates gibt es kein `message`-Objekt → Guard greift nur wenn `not data.get("callback_query")`.
**Pitfall – `send_telegram`:** Signatur `send_telegram(chat_id, text)` – nur 2 Argumente!

### Endpoint `/webhook/todo` (Apple Watch / iOS Kurzbefehl)

`POST /webhook/todo?token=...` – erstellt PKA-Todo direkt in `Todos.json` (Dropbox).

**Pitfalls iOS Shortcuts:**
- Kein `Content-Type: application/json` Header → `request.get_json(force=True, silent=True)` nötig
- JSON-Keys kommen als `Text` (Großbuchstabe), nicht `text` → case-insensitive Suche via `next((v for k,v in data.items() if k.lower()=="text"), None)`
- Siri klebt Hashtag an Folgewort: `#privatto-do` statt `#privat` → Regex `#(privat|arbeit)` ohne Whitespace-Pflicht, Strip mit `\S*`
- Token-Rotation: Bei versehentlicher Sichtbarkeit im Chat → `sed -i 's/^TODO_WEBHOOK_SECRET=.*/TODO_WEBHOOK_SECRET=NEU/' /etc/pka/secrets.env` + Service-Restart + Kurzbefehl-URL aktualisieren

---

## Sicherheitsfeatures

- **Admin-Gate:** X-Upload-Token in `localStorage` (Key `ut`, Flag `admin_auth`) – gleicher Origin wie die öffentliche Seite, deshalb ist jedes XSS dort ein Token-Diebstahl (Review v1.44)
- **Vereins-Auth:** bcrypt, httponly Cookie `vk_session`, 8h Timeout, Brute-Force-Schutz (5 Versuche → 15 Min.)
- **Multi-Verein-Login:** Eine E-Mail → mehrere Vereine möglich; Pre-Auth-Token (5 min) für Vereinsauswahl
- **DB:** `/opt/rename-webhook/vk_accounts.db` (SQLite WAL) – Tabellen: vereine_accounts, vk_users, vk_sessions, vk_audit, upload_quota, tg_subscriptions, page_stats, ical_feed_requests, ical_feed_vereine
- **Öffentlicher Telegram-Bot:** `@Vereinskalender_bot` – Token `KALENDER_BOT_TOKEN`; Endpunkt `/kalender-bot`. `/abo` zeigt die Vereine seitenweise (40 je Nachricht, `vk_seite:N`) – bei 138 Vereinen war eine Tastatur zu groß für Telegram.
- **Telegram-Webhooks fail closed (seit v1.44, ADR-022):** `/telegram` und `/kalender-bot` nur mit Header `X-Telegram-Bot-Api-Secret-Token`. Secret = `TELEGRAM_WEBHOOK_SECRET` falls gesetzt, sonst HMAC aus dem Bot-Token (`shared/telegram.webhook_secret`). Der Guard registriert beide Bots immer mit `secret_token` und heilt eine 403 von selbst. **Nach Token-Rotation:** `telegram_webhook_guard.py --force`.
- **Sessions (seit v1.44):** absolut 8 h ab Login (nicht mehr gleitend), `u.aktiv=1` Pflicht, Passwort-Reset beendet alle, Passwortänderung alle anderen Sessions (`vk_db.delete_user_sessions`). Abgelaufene Sessions räumt `create_session` weg. **Pitfall:** `vk_sessions`/`vk_audit` referenzieren `vk_users` (FK, `foreign_keys=ON`) – Benutzer erst löschen, nachdem Sessions gelöscht und Audit-`user_id` auf NULL gesetzt sind (sonst IntegrityError, so scheiterte „Mitglied entfernen“ bis v1.43).
- **Cookies:** `vk_session`, `vk_preauth` und Flask-Session mit `Secure` (Test: `VKO_COOKIE_INSECURE=1`).
- **Escaping:** Vereinsnamen, Heimatorte und Veranstalter kommen aus Formularen und fremden Webseiten. Im Frontend immer `escHtml()` (escapt seit v1.44 auch `'`). Namen nie in einen JS-String in `onclick` schreiben, sondern über `_vereinMap[id]` übergeben. Auch kein `escHtml()`/`&#39;` im `onclick`: Der Browser dekodiert Entities im Attribut vor dem JS, ein `'` bricht trotzdem aus (Favoriten-Chip bis v1.62). Werte als `data-`Attribut (escapt) setzen und per delegiertem Listener lesen (`#fav-bar`, `#rubrik-bar`, v1.63); `tests/test_app.py` 19c prüft das. Serverseitig `html.escape`, `_page()` escapt den Titel selbst. Telegram mit `parse_mode: HTML` heißt: alles escapen.
- **E-Mail-Prüfung `_valid_email`:** keine `<>"'(),;:\[]` und kein Leerzeichen (Adresse landet in HTML und Mail-Headern). `vk_mail._send` verweigert Adressen mit Zeilenumbruch.
- **Fremde Apps über VKO-Domains gesperrt:** `webhook.py` antwortet für `/api/verkehr`, `/autoquartett/`, `/aktien-` mit 404, wenn der Host `vereinskalender.online`/`veranstaltungen.website` ist (kostenpflichtige Endpunkte, nur über `umbenennen.duckdns.org` mit eigenem nginx-Limit gedacht).
- **E-Mail:** Brevo SMTP (`smtp-relay.brevo.com:587`); `FROM_EMAIL = noreply@vereinskalender.online`
- **Mailversand meldet Fehler (seit v1.67, 2026-10-07):** `vk_mail._send()` schickt bei jedem Fehlschlag (fehlender `BREVO_SMTP_*`, SMTP-Fehler) eine Telegram-Warnung (`_fehler_melden`, liest `TOKEN`/`CHAT_ID` aus der Umgebung, wirft nie). Neue Mailart ⇒ über `_send()`, nie eigenes `smtplib`. Freigabe (Telegram-Callback **und** `/api/admin/vereine/<id>/approve`) läuft über `vk_mail.konto_mail()`: E-Mail noch unbestätigt → neuer Bestätigungslink statt Willkommens-Mail (Login braucht `email_verified`), sonst Willkommens-Mail; `konto_mail_text()` liefert die Rückmeldung für Telegram/Admin. Bestätigt jemand nach der Freigabe, zeigt `/api/auth/verify` „Jetzt anmelden“. Admin → Accounts: „Bestätigungslink senden“/„Willkommens-Mail senden“ je Benutzer (`POST /api/admin/users/<id>/mail`, 409 wenn bestätigt, aber Verein nicht freigegeben).
- **E-Mail-Texte selbst bearbeitbar (v1.68, ADR-028):** Admin → Reiter „Texte“. Standard in `vk_mail.STANDARD` (je Mail Betreff/Überschrift/Text/Knopf/Hinweis + `platzhalter`, `knopf_link`, `anrede`), Josefs Fassung in `/opt/rename-webhook/mail_texte.json` (gitignored, atomar, `verlauf` je Mail max. 20). `baue_mail()` escapt alles und setzt Platzhalter ein (Links anklickbar, `**fett**`, Listen). Endpunkte `/api/admin/mailtexte[/<art>[/vorschau|/test|/zuruecksetzen]]` mit Admin-Token; Testmail an `Vereinskalender@icloud.com`. **Pitfall:** Standardtext im Code ändern wirkt nicht, wenn Josef die Mail überschrieben hat. **Neue Mailart ⇒ in `STANDARD` anlegen + `_mail()`**, nie eigenes HTML. Lokaler Klick-Test: Testserver mit `UPLOAD_TOKEN=admintoken`, Temp-Daten, `services.kalender.routes.KALENDER_HTML_FILE` auf `src/kalender.html` umbiegen; **nicht Port 5060/5061** (Chrome: `ERR_UNSAFE_PORT`).
- **Telegram-Gruppe „VKO Freigaben“ (seit 2026-10-07):** `shared/telegram.py::FREIGABE_CHAT_ID` (-5343827729, kein Geheimnis → im Repo), `freigabe_chat_id()` fällt bei leerem Eintrag auf `CHAT_ID` zurück. Dorthin gehen: Freigabe-Anfragen mit Knöpfen, Freigabe-/Ablehnungsergebnis, Verknüpfungsvorschläge (`vk_link`), Ortschaft-Hinweise, gescheiterte Mails (`vk_mail._fehler_melden`). Importe, Berichte, Befehle bleiben im Hauptchat. Knöpfe prüfen weiter die Nutzer-ID (`from.id == CHAT_ID`), nicht den Chat – Mitleser in der Gruppe können nicht freigeben. Der Bot meldet im Hauptchat die Chat-ID, wenn Josef ihn zu einer Gruppe hinzufügt (`my_chat_member`), und eine neue ID nach Umwandlung zur Supergruppe (`migrate_to_chat_id`) – **nie `getUpdates` benutzen** (schaltet den Webhook ab). Beim Start einmal je ID eine Begrüßung in die Gruppe (`freigabe_gruppe_ankuendigen`, Merkdatei `freigabe_chat_angekuendigt.txt`, gitignored). **Neue Meldung zu Vereinskonten ⇒ `freigabe_chat_id()`**, nicht `CHAT_ID`.
- **⚠️ Pitfall Brevo-Schlüssel inaktiv (Vorfall 2026-10-07, FF Hölskofen):** Brevo markiert SMTP-Schlüssel nach 3 Monaten ohne Nutzung als inaktiv. Bei wenigen Registrierungen gingen Bestätigungs- und Willkommens-Mail still verloren (`_send` gab nur `False` zurück, Telegram meldete trotzdem „✅ Freigegeben“), der Verein konnte sich nicht anmelden. Gegenmittel: Fehler-Telegram (oben) + `mail_lebenszeichen.py` (montags, Cron-Tabelle).

---

## Rename-Service (services/rename/routes.py)

- **529-Retry:** `rename_via_claude()` hat 3-Versuche-Retry (15s / 30s Backoff). Ohne Retry: Overload-Fehler wird geloggt, Cursor trotzdem gesetzt → Datei wird nie erneut versucht.
- **Cursor-Fix nach Stuck:** Datei manuell umbenennen; Cursor lebt weiter.

---

## logbuch_summary.py

- Liest `Logbuch.md` aus Dropbox via Invoice-Dropbox-Token
- Regex matcht `## YYYY-MM-DD` mit beliebigem Suffix
- Mehrere Nachtrag-Einträge werden chronologisch zusammengeführt
- Im GitHub-Repo getrackt (seit Initial-Commit 2026-05-06) – normaler Deployment-Flow gilt, kein Sonderfall
- Telegram-Versand splittet lange Texte automatisch (>4096 Zeichen, siehe `BKM/Telegram-Integration.md`) – der frühere `entry[:3800]`-Kürzungs-Fallback bei Claude-Fehler wurde entfernt, da nicht mehr nötig
- Log prüfen: `tail -20 /var/log/pka-logbuch.log`

## pip_update_audit.py (Sonntags-Pip-Report)

**Wo er läuft:** nicht im Crontab, sondern **sonntags 03:00 aus `/usr/local/bin/server-maintenance.sh`** (letzter Befehl des Skripts; `/etc/cron.d/server-maintenance`, täglich 03:00). Ein Audit-Fehlschlag macht damit das Skript fehlschlagen und wird über den Cron-Wächter-Job `server_maintenance` sichtbar. Am 2026-10-02 wurde er irrtümlich als „läuft nicht" gemeldet, weil nur Crontab und `cron.d` durchsucht wurden.

Prüft **alle** venvs unter `/opt` (dynamisch erkannt, seit 2026-09-20 – vorher feste `VENVS`-Liste mit nur 5 von 10 venvs, siehe Claude-Remote ADR-008) auf veraltete Pakete + CVEs und hängt einen Abschnitt „Server-Inventar“ an (Whitelist-Abgleich, crashende/gescheiterte systemd-Units, apt, Node); sendet den Ampel-Report per Telegram. Läuft sonntags via `server-maintenance.sh` → `/etc/cron.d/server-maintenance` (03:00 Uhr). Macht **keine** automatischen Updates.

- Im GitHub-Repo getrackt, normaler Deployment-Flow (`git push` → `ssh ... "git -C /opt/rename-webhook pull"`)
- **Pitfall (behoben 2026-08-16):** `pip_audit()` rief `pip-audit --path=<venv-Wurzel>` auf (z.B. `/opt/rename-webhook`) statt der Site-Packages (`/opt/rename-webhook/lib/python3.12/site-packages`). `--path` filtert die *installierten Pakete des laufenden Interpreters* auf den angegebenen Pfad – bei falschem Pfad landen 0 Treffer, ganz ohne Fehlermeldung. Ergebnis: der CVE-Check hat seit Einführung des Scripts **immer 0 CVEs gemeldet**, unabhängig vom tatsächlichen Stand. Beim Nachprüfen fanden sich reale, ungemeldete CVEs: `pillow 12.2.0` in `rechnungen` (20 Advisories, Fix in 12.3.0) und `pypdf 6.14.2` in `kargl-invoice` + `life-doku` (2 Advisories je, Fix ab 6.15.0). Fix: `pip_audit()` ermittelt jetzt den Site-Packages-Pfad per `Path(bin_dir).parent.glob("lib/python3.*/site-packages")`. Alle 3 betroffenen Apps sofort aktualisiert, CVE-Check danach mit 0 Treffern verifiziert.
- **`pyee`/`pydantic_core` bleiben dauerhaft zurück (Abhängigkeits-Pins):** `playwright` pinnt `pyee<14,>=13`, `pydantic` pinnt `pydantic_core` exakt. Seit 2026-09-20 erkennt der Audit das dynamisch (siehe unten) und zeigt es in **einer** Zeile „Durch Abhängigkeit gehalten“ statt als Dauer-Alarm. (Der `pyee-14`-Zustand vom 2026-08-30 war ein Zufallsergebnis der pip-Auflösung; beim `playwright`-Upgrade am 2026-09-20 hat pip auf 13.0.1 zurückgesetzt, `pip check` ist OK.)
- **Neu seit 2026-09-20 (Commit `93fe1fe`, Claude-Remote ADR-008):**
  - venv-Liste + Inventar kommen aus `/usr/local/bin/pip-upgrade-safe check --json` (Single Source of Truth, derselbe Check läuft vor jedem Upgrade). Fallback bei Ausfall: eigene venv-Erkennung + Warnzeile im Report. **Nicht** als Python-Modul importieren – das Script läuft als root, siehe ADR-008.
  - **Report:** CVEs je Paket zusammengefasst mit höchster Fix-Version, Major-Updates einzeln, Minor-Updates kompakt (Anzahl + max. 6 Namen), Abschnitt „Server-Inventar“; Aufteilung an Absatzgrenzen bei >4000 Zeichen; HTML-Escaping der dynamischen Texte. Header „kritisch“ zählt Apps und Server-Auffälligkeiten getrennt.
  - **`--dry-run`:** `/opt/rename-webhook/bin/python3 /opt/rename-webhook/pip_update_audit.py --dry-run` gibt den Report aus, **ohne** `secrets.env` zu lesen oder zu senden (~40 s) – so wird das Script getestet, ohne Telegram-Spam und ohne Secrets.
  - **Pitfall:** Beim Sichten von `pip-audit`-Ausgaben nie mit `tail` kürzen – der erste Blick am 2026-09-20 zeigte durch `tail -n 6` nur einen Teil der CVEs von `sentiment-scanner` (real: pillow 13, anyio 3, soupsieve 2, pip 6 Advisories).
  - **Gehaltene Pakete (Commit `2eed541`):** `held_back()` prüft im Python des jeweiligen venvs (pip-eigenes `packaging`), ob ein installiertes Paket die neueste Version per Requirement ausschließt; solche Pakete zählen nicht in die Ampel und stehen gesammelt in der Inventar-Zeile ⏸. Endet der Pin, erscheint das Paket automatisch wieder normal – es kann nichts still vergessen werden. Keine feste Liste zu pflegen.
  - **Ausnahme `MANUAL_HOLDS` (seit 2026-10-04, PKA #426):** Pakete, die per Requirement erlaubt, aber praktisch inkompatibel sind (der Abhängige verlangt nur `>=`), stehen mit Begründung in `MANUAL_HOLDS` und erscheinen in derselben ⏸-Zeile mit Präfix „manuell:“. Aktuell nur `av` (v19 bricht faster-whisper 1.2.1 in life-doku). Diese Liste **verfällt nicht von selbst** – Gegenstück ist `PINNED` in `Claude-Remote/scripts/pip-upgrade-safe` + `claude-remote-pip-upgrade`; beim Freigeben alle drei Stellen anpassen.

---

## Vereinsfreigabe (seit 2026-09-25)

Eine Freigabe muss **zwei** Dinge tun: `status='aktiv'` in `vereine_accounts` setzen **und**
den Verein in `vereinstermine.json` bekannt machen. Ohne den zweiten Schritt ist er in der
Vereinsübersicht unsichtbar, weil `/api/vereine` über `_labels` iteriert und nicht über die
DB (Vorfall 2026-06-18: FFW Paindlkofen).

**Zentral in `shared/kalender_store.register_verein(verein_key, verein_name, row=None)`:**

- `_labels[verein_key] = verein_name`
- `_meta[verein_key]["selbstverwaltung"] = True`
- aus `row` (der `sqlite3.Row` aus `vereine_accounts`) zusätzlich `plz`, `gemeinde`,
  `landkreis`, `heimatort`, `rubrik` – ohne die steht der Verein ohne Ort da und fällt
  aus dem Regionsfilter. Leere DB-Spalten werden nicht geschrieben.
- **durchgehend `setdefault`** → idempotent, überschreibt nie einen bestehenden Wert
  (ein Verein mit `selbstverwaltung: false` behält `false`)
- legt **keine** leere Terminliste `data[verein_key]` an – `nTermine` fällt in
  `/api/vereine` ohnehin auf 0 zurück

### ⚠️ Es gibt ZWEI Freigabe-Wege

| Weg | Datei |
|---|---|
| `POST /api/admin/vereine/<id>/approve` | `services/auth/routes.py` → `approve_verein()` |
| Telegram-Button „✅ Freigeben" (`callback_data` `verein_approve:<id>:<name>`) | `services/telegram/routes.py` |

Der Telegram-Weg **dupliziert die Freigabe-Logik** (eigene Query, eigenes
`UPDATE … status='aktiv'`, eigener Mailversand) und ist der real genutzte. Wer die
Freigabe ändert, muss beide Stellen anfassen – genau deshalb liegt der JSON-Teil jetzt in
einer gemeinsamen Funktion und nicht zweimal inline. Die Query muss `v.verein_key` und die
Ortsspalten mitselektieren, sonst kommt `register_verein` nicht an die Daten.

**Beide Queries joinen `vk_users` mit `AND u.role='admin'`** – ohne den Filter geht die
Willkommensmail bei einem Verein mit mehreren Nutzern an einen beliebigen davon (war im
API-Endpunkt bis 2026-09-25 der Fall, der Telegram-Weg hatte es schon richtig).

### Altbestand prüfen

Vereine, die vor dem Fix freigegeben wurden, bleiben unsichtbar. Abgleich:

```bash
ssh root@89.167.104.145 '/opt/rename-webhook/bin/python3 -c "
import sys, json; sys.path.insert(0, \"/opt/rename-webhook\")
from shared.kalender_store import VEREINSTERMINE_FILE
from shared.vk_db import db_conn
labels = json.loads(VEREINSTERMINE_FILE.read_text()).get(\"_labels\", {})
with db_conn() as c:
    for r in c.execute(\"SELECT verein_key, verein_name FROM vereine_accounts WHERE status=\x27aktiv\x27\"):
        if r[\"verein_key\"] not in labels:
            print(\"FEHLT:\", r[\"verein_key\"], r[\"verein_name\"])
"'
```

Stand 2026-09-25: `fkk_musendorf` fehlt (aktiv, aber nicht in `_labels`/`_meta`).

---

## Service läuft unter gunicorn (seit 2026-09-25, ADR-010)

Vorher: `ExecStart=/opt/rename-webhook/bin/python3 /opt/rename-webhook/webhook.py`
(Flask-Dev-Server, Warnung in den Logs). Jetzt:

```ini
ExecStart=/opt/rename-webhook/bin/gunicorn -w 1 --threads 8 -k gthread \
    -b 127.0.0.1:5000 --timeout 120 --graceful-timeout 30 \
    --access-logfile - --error-logfile - webhook:app
```

`webhook.py` war dafür schon vorbereitet – `app = create_app()` auf Modulebene.

### ⚠️ `-w 1` ist Pflicht, nicht Geschmack

`_import_lock` (`services/kalender/routes.py`) und `_preauth_lock`
(`services/auth/routes.py`) sind `threading.Lock` und wirken **nur prozessintern**. Mit
`-w 2` könnten zwei Requests zwei heimat-Importe gleichzeitig auf derselben JSON-Datei
fahren. Wer auf mehrere Worker will, muss diese Locks zuerst auf `fcntl` umstellen
(`KalenderStore` macht es bereits richtig). Begründung und verworfene Alternativen: ADR-010.

Ebenfalls bewusst weggelassen:
- **kein `--max-requests`** – viele Endpoints starten Fire-and-Forget-Daemon-Threads
  (Telegram-Antworten, heimat-Import, Verkehr) und antworten sofort. Worker-Recycling
  würde die mitten im Lauf abschneiden, ohne Fehler und ohne Log.
- **kein `--preload`**

### ⚠️ Beim Testen: App lässt sich nicht ohne systemd importieren

`shared/kalender_core.py` liest `os.environ["CLAUDE_API_KEY"]` beim Import. Ein
`python3 -c "from webhook import app"` auf der Shell scheitert deshalb mit
`KeyError: 'CLAUDE_API_KEY'` – die Variable kommt aus `EnvironmentFile=/etc/pka/secrets.env`
und **darf nicht** von Hand gesourct werden. Routen prüft man am laufenden Service
(`curl http://127.0.0.1:5000/…`) oder per grep über die `@*_bp.route`-Dekoratoren.

**Offline geht es seit v1.44 mit `python3 tests/test_app.py`:** Der Test setzt Dummy-Umgebungsvariablen,
legt DB und `vereinstermine.json` in ein Temp-Verzeichnis, biegt die `/opt/…`-Pfade aller
Module um (`PFADE` im Test) und ersetzt Netz-Funktionen durch Attrappen. Rund 90 Prüfungen
aus dem Komplett-Review (XSS, Webhook-Secret, Lost Update, Sessions, Soft-Delete, iCal-UIDs,
Store über zwei Prozesse …). Neue feste Pfade als Modulkonstante anlegen, sonst greift der
Test auf `/opt` zu (so lag z. B. `last_import.json` bis v1.44 als Literal im Code).

### ⚠️ Nicht jede Route hängt an jeder Domain

Beim Prüfen nach dem Umstellen leicht als Ausfall fehlzudeuten:

| URL | umbenennen.duckdns.org | vereinskalender.online | direkt :5000 |
|---|---|---|---|
| `/` | 200 | 200 | 404 |
| `/verein/dashboard` | **404** | 302 (Login) | 302 |

`/verein/**` hängt an `vereinskalender.online`, nicht an `umbenennen.duckdns.org`. Ein 404
dort ist normal. Entscheidend beim Verifizieren ist der direkte Aufruf auf `127.0.0.1:5000`.
Weitere existierende Pfade, die man leicht falsch rät: `/aktien-search` (nicht `/aktien/`),
`/autoquartett/car-lookup` (POST, nicht `/autoquartett/`), `/telegram` (nur POST → GET
liefert korrekt 405; ein 502 hieße, der Service ist tot).

## Cron-Pitfalls (2026-10-02)

- **Netatmo-Job beendet sich bei jedem API-Fehler still mit Exit 1** (`HTTP 503` von Netatmo). Vor der Überwachung unsichtbar; der Wächter toleriert den ersten Fehlschlag, alarmiert den zweiten in Folge. Log: `/var/log/pka-netatmo.log` (Rechte 600).
- **`secrets.env` Zeile `NETATMO_REFRESH_TOKEN` enthält ein unquotiertes `|`:** in bash ist die Variable nach `source` leer, `source` mit `set -e` scheitert (Exit 127). Harmlos (eigener Parser im Netatmo-Skript, systemd nimmt den Wert wörtlich). Neue Zeilen immer in Einzelanführungszeichen anhängen; Diagnose ohne Werte-Ausgabe: Muster in `PKA/SOPs/Server-Secret-Scan.md`.
- **Ein Cronjob-Wrapper auf Zeilen mit `bash -c '… source secrets.env …'`** (`pka_todos_reminder`) wird *innerhalb* der Anführungszeichen eingefügt, nicht davor.
