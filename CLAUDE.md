# Vereinskalender – Claude-Kontext

## Kerninfos

- **GitHub (Source of Truth):** `https://github.com/sEppofaz/Webhook-Services`
- **Lokale Arbeitskopie:** `~/Dropbox/Apps/Claude/Vereinskalender/src/` – hier bearbeiten, dann `git push`
- **Auf Server:** `/opt/rename-webhook/` – zieht per `git pull` von GitHub
- **Credentials:** ausschließlich in `/etc/pka/secrets.env` (via EnvironmentFile im Service)
- **Deployment-SOP:** `PKA/SOPs/Vereinskalender-Deployment.md`

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

## heimat-info.de Import (heimat_import.py)

**Script:** `/opt/rename-webhook/heimat_import.py`
**Gemeinden-Konfiguration:** `/opt/rename-webhook/heimat_gemeinden.json`
**Log:** `/var/log/pka-heimat.log`

### Funktionsweise

1. Export-API: `https://heimatinfo-api-platform.azurewebsites.net/export/events?pageIndex=0&pageSize=50&c=<UUID>` (max. pageSize=50, CORS-Guard: Header `Origin: https://www.heimat-info.de` erforderlich)
2. UUID (`c=`) ist pro Gemeinde eindeutig – einmalig via Playwright ermittelt
3. `_fetch_all_events(c_id)` holt alle Events paginiert; `_parse_api_events()` parst JSON (UTC → Europe/Berlin via zoneinfo; T00:00:00Z = ganztägig)
4. **Duplikatprüfung:** `_existing_events()` → `set[(datum, uhrzeit, bezeichnung.lower())]` cross-key. `_is_duplicate()` prüft exakten Match + Substring-Match (nur wenn datum+uhrzeit übereinstimmen und Bezeichnung ≥ 6 Zeichen)
5. Bei ✅: `do_import(uid)` prüft `_neu`-Flag, dann `_is_duplicate()`; schreibt in `vereinstermine.json`
6. Bei ❌: Pending-Datei wird gelöscht

### Gemeinde hinzufügen
```bash
/heimat-add https://www.gemeinde-xyz.de/veranstaltungen/
# Oder direkt:
ssh root@89.167.104.145 "/opt/rename-webhook/bin/python3 /opt/rename-webhook/heimat_import.py --add https://..."
```

### Felder in heimat_gemeinden.json
```json
[{"name": "Bayerbach", "label": "Veranstaltungen Bayerbach", "verein_key": "bayerbach",
  "c_id": "77bc043e-...", "url": "https://www.gemeinde-bayerbach.de/veranstaltungen/"}]
```

### Pitfalls
- **Pending-Dir:** `/opt/rename-webhook/imports/heimat_pending_*.json` – persistiert Server-Neustart
- **Export-API pageSize-Limit:** Max. `pageSize=50`. Paginierung via `pageIndex=0,1,2…`
- **Export-API CORS-Guard:** Ohne `Origin: https://www.heimat-info.de` kommt HTTP 400
- **Ganztägige Termine:** `startDate` endet auf `T00:00:00Z` → kein Uhrzeitfeld
- **Log-Ownership:** `/var/log/pka-heimat.log` kann als `root` erstellt werden → `chown webhook:webhook`
- **`_meta.heimatort`:** Label-Format muss `"Veranstaltungen <Gemeindename>"` sein (letztes Wort = Heimatort-Fallback)
- **`veranstalter`-Feld:** Nur bei heimat-info-Importen. Badge-Fallback: `t.veranstalter || labels[t.verein] || t.verein`
- **Duplikat-Logik:** Substring-Check nur wenn datum + uhrzeit identisch
- **`_neu`-Flag in `do_import()`:** Erste Bedingung (vor `_is_duplicate()`). `_neu=False` schließt Event aus
- **`quelle`/`quelle_url`:** Werden aus Pending-Datei übernommen. `quelle = "heimat-info.de"`
- **Geo-Schutz in `do_import()`:** Geo-Felder werden nur gesetzt wenn `key not in data["_meta"]`
- **Borlabs Cookie:** `discover_c_id()` versucht zuerst Base64-Decode, fällt auf Playwright-Intercept zurück
- **`--run` CLI-Argument nicht implementiert:** `main()` kennt nur `--add`. Alles andere fällt auf `cmd_import()` → alle Gemeinden. Einzelnen Import per Code: `fetch_and_save_pending_for_url("https://...")`
- **Playwright nach Update:** Browser können fehlen → `playwright install chromium` auf dem Server
- **`api_admin_importe_confirm` gibt immer `ok: True` zurück** solange kein Exception auftritt – auch wenn `do_import()` „⚠️ Pending-Datei nicht gefunden" meldet
- **`heimatort_gespeichert`:** Im Pending-Meta-Response enthalten – aus bestehendem `_meta[k].heimatort`. Frontend nutzt das zur Vorausfüllung im Import-Dialog

---

## Cron-Jobs (Vereinskalender-relevant)

Alle Jobs als `root`-Crontab. Timezone: `Europe/Berlin`. Logs: `/var/log/pka-*.log` – Rotation seit 2026-07-02 via `/etc/logrotate.d/pka` (weekly, 8 Rotationen, `su root root` nötig wegen `syslog`-Gruppenrechten auf `/var/log`).

| Zeit | Script | Beschreibung |
|------|--------|--------------|
| täglich 06:30 | `logbuch_summary.py` | Logbuch-Eintrag per Telegram (nicht im Repo) |
| täglich 18:00 | `event_reminder.py` | Erinnerung morgige Gottesdienste + Vereinstermine |
| täglich 18:00 | `kalender_erinnerung.py` | Telegram-Erinnerungen für Bot-Abonnenten |
| täglich 00:10, 20:00 | `kalender_report.py` | Vereinskalender-Bericht (verifiziert, DE) |
| täglich 00:05 | `stats_collector.py` | Besucherstatistik → `page_stats`-Tabelle |
| wöchentlich Mi 07:00 (`0 7 * * 3`) | `heimat_import.py` | heimat-info.de alle Gemeinden fetchen |
| Di+Do 06:00 | `traffic_info.py` | Verkehrsinfo-Check |
| alle 15 Min | `pka_todos_reminder.py` | PKA Todos Fälligkeits-Erinnerungen |

**kalender_report.py (2026-05-22):** Datenquelle auf SQLite-DB umgestellt (`page_stats` + `page_stats_geo`). Zeigt verifizierte Zahlen (ohne Crawler), Datum-Label des letzten verfügbaren Tages, Deutschland-Besucher aus `page_stats_geo`. Keine nginx-Log-Analyse mehr. Läuft nur noch 00:10 + 20:00 Uhr (war: 4×täglich).
**kalender_report.py (2026-06-17):** Neue Funktion `verein_activity_stats()` – fragt `vk_audit` nach `aktion='erstellt'` und `aktion='geaendert'` ab (UTC-Timestamps, Cutoffs 24h + 7d). Zeigt im Bericht: `✏️ Vereinstermine 24h: X neu · Y geändert | 7 Tage: X neu · Y geändert`. Nur Aktionen durch Vereine selbst (Dashboard), keine heimat-info-Importe.
**kalender_report.py (2026-08-06):** Bug behoben – da `stats_collector.py` nur einmal täglich (00:05) den **abgeschlossenen Vortag** berechnet, zeigte der 20:00-Bericht bislang dieselben (eingefrorenen) Zahlen wie der 00:10-Bericht desselben Morgens, während der nächste 00:10-Bericht dann einen komplett anderen Kalendertag zeigte – wirkte wie unplausible Sprünge. Neue Funktion `get_live_today_stats()` berechnet beim 20:00-Lauf den laufenden Tag (00:00 bis jetzt) live aus dem aktuellen nginx-Log (dieselbe Zähl-/Crawler-Logik wie `stats_collector.collect_day()`), schreibt aber **nicht** in die DB (sonst würde die 7-Tage-Summe unvollständige mit vollständigen Tagen mischen). Umschaltung morgens/abends anhand der Uhrzeit (`< 12 Uhr` → Vortag aus DB, sonst live). Bericht-Label zeigt jetzt explizit „(Vortag, vollständig)" bzw. „(heute bis HH:MM Uhr)".
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
- `location /verein` → proxy_pass Flask (Auth-Seiten, Dashboard)
- `location /telegram` → Telegram Haupt-Bot-Webhook (**Pflicht!** Muss in dieser Config stehen)
- `location /kalender-bot` → Telegram Kalender-Bot-Webhook
- **Rate-Limit-Conf:** `/etc/nginx/conf.d/rate-limit.conf` (api_zone, api_termine_zone, auth_zone)
- **Security-Header:** HSTS, X-Frame-Options, X-Content-Type-Options, Referrer-Policy – in Locations mit eigenem `add_header` explizit wiederholen (nginx-Vererbungsregel)
- Nach Änderungen: `nginx -t && systemctl reload nginx`
- **⚠️ Pitfall Telegram-Webhook:** Bei Domain-Änderungen oder neuer nginx-Config IMMER prüfen ob `/telegram` enthalten ist. Fehlt die Location → Telegram-Callbacks kommen nicht an → Bot stumm. Webhook-URL: `https://vereinskalender.online/telegram`. Prüfen: `getWebhookInfo`. Neu setzen: `setWebhook url=https://vereinskalender.online/telegram`. (Vorfall 2026-06-07 bis 2026-06-10)

---

## Öffentliche Endpunkte `vereinskalender.online`

| Pfad | Beschreibung |
|------|--------------|
| `/` | Vereinskalender-PWA |
| `/api/termine` | GET/PATCH/DELETE – Termine (PATCH/DELETE: Auth X-Upload-Token) |
| `/api/ical` | GET – iCal-Export einzelner Termin |
| `/api/ical/feed` | GET – Abonnierbarer Feed (`webcal://`), optional `?v=key1,key2` oder `?ort=Ortschaft` |
| `/api/check-token` | POST – Admin-Token prüfen |
| `/api/confirm-import` | POST – Upload-Import bestätigen |
| `/api/admin/importe` | GET – Pending-Liste |
| `/api/admin/importe/<uid>` | GET/confirm/reject – Import-Detail |
| `/api/admin/importe/trigger` | POST – Import-Trigger (SSRF-Schutz: nur HTTPS, keine privaten IPs) |
| `/api/vereine` | GET/POST – Vereine + Meta |
| `/api/vereine/<key>` | DELETE – Verein löschen |
| `/api/admin/stats` | GET – Statistiken (Auth) |
| `/api/admin/stats/chart` | GET – Tages-Zeitreihe `?d=7|30|365` |
| `/api/admin/users` | GET – Alle Accounts (Auth) |
| `/api/admin/verein/<id>` | PATCH/DELETE – Verein-Account |
| `/api/admin/unregistered-keys` | GET – Keys in vereinstermine.json ohne Account (für Transfer-Dropdown) |
| `/api/admin/verein/<id>/transfer-key` | POST `{source_key}` – Termine + _meta + _labels + tg_subscriptions übertragen |
| `/upload` | Superadmin-Upload (PDF/JPG/PNG/HEIC/Excel) |
| `/#admin` | Admin-PWA (Tabs: Import/Importe/Vereine/Accounts/Termine/Stats) |
| `/verein/register` | Selbstregistrierung |
| `/verein/login` | Vereins-Login (bcrypt, Brute-Force-Schutz) |
| `/verein/dashboard` | Termin-Übersicht (nach Login) |
| `/verein/upload` | Vereinsadmin-Upload (Rate-Limit 3/Tag) |
| `/sw.js` | Service Worker (Network-first App-Shell, /api/* nie cachen) |
| `/manifest.json` / `/manifest-admin.json` | PWA-Manifeste |

---

## Datenstruktur `vereinstermine.json`

- **`_labels`**: `{vereinKey: "Anzeigename"}` – letztes Wort > 4 Zeichen = Heimatort-Fallback
- **`_meta[key]`**: `{plz, gemeinde, landkreis, heimatort?, selbstverwaltung?}`
- **`_ortschaften`**: `{gemeinde_map: {...}}` – Mapping Ortschaft→Gemeinde

### Pitfalls `vereinstermine.json`
- **`approve`-Endpunkt legt keinen JSON-Eintrag an:** `POST /api/admin/vereine/<id>/approve` setzt nur `status=aktiv` in der DB. Wenn ein freigegebener Verein noch nie Termine importiert hat, fehlt er in `_labels`/`_meta` → unsichtbar in Vereinsübersicht. Fix: `_labels` + `_meta` (mit `selbstverwaltung: true`) manuell via `KalenderStore.update()` nachtragen. (Vorfall 2026-06-18: FFW Paindlkofen)
- **`_heimat_aliases` (Stand 2026-07-16):** `{heimat_key: account_key}` in `vereinstermine.json`. `admin_transfer_key()` (`services/auth/routes.py`) schreibt hier automatisch rein. `heimat_import.py` löst den bei jedem Import frisch aus dem Veranstalter-Namen berechneten `verein_key` (`_slugify(veranst)`) zuerst gegen diese Map auf – sonst entsteht bei abweichender Schreibweise auf heimat-info.de erneut ein Duplikat-Key für einen bereits transferierten Verein. Selbstverwaltende Vereine werden von heimat-info NICHT mehr komplett ausgeschlossen, sondern normal dedupliziert (heimat-info bleibt Ergänzungsquelle, siehe ADR-003). (Vorfall 2026-07-16: FFW Paindlkofen / Freiwillige Feuerwehr Paindlkofen, doppeltes Weißwurstfrühstück)
- **Vereinsname-Umbenennung muss `_labels[verein_key]` mitziehen:** Der im Kalender angezeigte Vereinsname kommt ausschließlich aus `_labels[verein_key]` in `vereinstermine.json` – nicht aus `vereine_accounts.verein_name`. Jede Stelle, die `verein_name` in der DB ändert (`verein_profil()` in `services/verein/routes.py`, `admin_update_verein()` in `services/auth/routes.py`), muss bei geändertem Namen zusätzlich `_labels[verein_key]` per `KalenderStore.update()` nachziehen, sonst zeigt der Kalender weiter den alten Namen. (Gefixt 2026-07-15)
- **`KalenderStore.update()` als `root` → Owner-Problem:** Direkter Python-Aufruf als root ändert den Datei-Owner auf `root` → App-User `webhook` bekommt `Permission denied`. Danach immer: `chown webhook:webhook /opt/rename-webhook/vereinstermine.json`
- **`KalenderStore.update()`-Mutator darf `d` nie durch einen vorher gelesenen Snapshot ersetzen:** `_do_save_import()` hat genau das getan (`d.clear() or d.update(data)`) und damit parallele Schreibzugriffe verloren gehen lassen, obwohl der Store selbst korrekt sperrt. Merge-Logik immer *innerhalb* des Mutators auf `d` selbst ausführen; langsame Netzwerk-Calls (z.B. `lookup_plz`) davor/außerhalb berechnen, nicht im Lock. (Regression gefixt 2026-07-05, Fable-5-Review)
- **`_do_save_import()` bei Vereinsadmin-Uploads:** Immer `verein_key=user["verein_key"]` übergeben, sonst wird der Key neu aus dem Vereinsnamen abgeleitet und kann vom Account-Key abweichen (Kürzung/Uniquifizierung) → Termine landen unsichtbar unter falschem Key.
- **`ortschaft`**: Pro Termin – Veranstaltungsort
- **`quelle`** / **`quelle_url`**: Pro Termin – Herkunft (heimat-info oder Vereinsadmin)
- **`flyer_url`** / **`flyer_path`** (optional, Stand 2026-06-07): Pro Termin – Dropbox-Link (`?raw=1` für Browser-Anzeige) + Dropbox-Pfad zum Löschen. Modul: `shared/flyer_store.py`. Upload-Ordner: `/Dokumente/Vereinskalender/flyer/`. Nur PDF/JPG/PNG/WebP, max. 8 MB, Magic-Bytes-geprüft, UUID-Dateiname.
- **Pitfall Flyer-Upload – `cid:image...`-Meldung:** Zieht ein Vereinsadmin ein Bild direkt aus einer Outlook-Mail (Drag&Drop) in das Flyer-Upload-Feld, übergibt der Browser oft nur Outlooks interne Content-ID (`cid:image001.png@...`) statt der echten Bilddaten – kein Bug im Vereinskalender, die Datei erreicht den Server so gar nicht (`upload_flyer()` in `shared/flyer_store.py` würde bei echtem Empfang einen deutschen Fehlertext liefern). Fix: Hinweistext direkt über dem Datei-Feld in `termin_neu()` + `termin_edit()` (Formulare) sowie im Hilfe/FAQ-Block auf `/verein/dashboard` (`services/verein/routes.py`) – Flyer immer zuerst lokal speichern, dann hochladen. (Vorfall 2026-07-31: FF Paindlkofen)
- **DSGVO – interne Felder pro Termin:** `erstellt_von`/`geaendert_von`/`geloescht_von` (Admin-E-Mail) + `flyer_path` (interner Dropbox-Pfad) dürfen nie an den öffentlichen `/api/termine`-Endpunkt raus. Blacklist: `_TERMIN_INTERNE_FELDER` in `services/kalender/routes.py`. Neues Feld mit potenziell sensiblem Inhalt pro Termin → dort ergänzen. (Fund 2026-07-05, Fable-5-Review)

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
- **Scroll zu Datum:** Beim Render scrollt die App zum `.ev-date-sep` vor dem ersten kommenden Termin (rückwärts durch `previousElementSibling` bis zur Klasse `ev-date-sep`).
- **Event-Card Icons (Stand 2026-06-07):** Alle drei Buttons nutzen gemeinsame CSS-Klasse `.ev-icon-btn` (position absolute, top:10px). Positionen: `.ev-cal` right:10px, `.ev-fav` right:50px, `.ev-flyer` right:90px. Icons als Lucide SVG inline.
- **Favoriten-Herz:** `.ev-fav.on` → `color:#ff3b30` + `svg path { fill:currentColor }` per CSS. Kein JS-`style.filter` mehr. `toggleFavVerein` setzt `innerHTML=icon("heart",14)` für alle `.fav-star`-Elemente (kein Emoji mehr).
- **Alle UI-Icons Lucide SVG (Stand 2026-06-08):** Kein Emoji als Icon irgendwo im UI. Zentrales `ICON_PATHS`-Objekt + `icon(name,size,filled)` Hilfsfunktion. Ausnahme: `alert()` / `confirm()` Browser-Dialoge – dort dürfen Emojis bleiben (kein SVG möglich).
- **Flyer-Button:** Nur gerendert wenn `t.flyer_url` gesetzt. `titlePadding` dynamisch: 74px (ohne Flyer) / 114px (mit Flyer). Click via Event-Delegation `.js-ev-flyer-btn` → `window.open(dataset.flyerUrl, '_blank', 'noopener')`.
- **Suchfeld-Lösch-Button (seit 2026-08-10, PWA-Standard):** Generische Helper `_toggleMiniClear(inputId)` / `_clearMiniSearch(inputId, cb)` (CSS-Klasse `.mini-srch-clear`) für Suchfelder außerhalb der Haupt-Suche (die hat bereits `#srch-clear`/`.srch-clear`). Konvention: Clear-Button-ID = `<inputId>-clear`. Aktuell genutzt in `#ver-srch` (Vereinsverwaltung) und `#trm-search` (Terminverwaltung).

## Registrierungsform `/verein/register` – Pitfalls (Stand 2026-06-07)

- **Pflichtfelder:** `plz` + `telefon` sind serverseitig required. Validierung: PLZ muss `^\d{5}$`, Telefon non-empty.
- **Neue Checkbox `zugangsdaten_notiert`:** Pflicht, serverseitig geprüft (`elif not zn:`). Wird in `form_data` NICHT zurückgegeben (kein Preserve nötig – ist nach Submit weg).
- **Client-Validierung:** JS in `<script>`-Tag am Ende des Formulars. f-String → `{{` für JS-Objekte, `\d{{5}}` für Regex. Checkbox-Fehler highlightet `.chk`-Wrapper (nicht das Input selbst).
- **Passwort-Toggle:** `.pw-wrap` wrapper + `.pw-toggle` Button mit Eye/EyeOff SVG. `tabindex="-1"` damit Tab-Reihenfolge unberührt bleibt.

## Filterlogik kalender.html – wichtige Pitfalls

- **Rubrik-Filter:** Kein „Alle"-Toggle. Kein aktiver Chip = alle Rubriken sichtbar
- **Favoriten-Priorität:** Aktiver Favorit-Chip überschreibt Verein-Dropdown + Rubrik-Filter
- **Selbstverwaltungs-Schutz:** `_meta[key].selbstverwaltung = true` → Admin-Dialog vor Edit/Delete; heimat-Import überspringt diese Vereine
- **Landkreis-Fallback:** Vereine ohne `meta[key].landkreis` → `"Landkreis Landshut"`
- **Filter-Reihenfolge:** aktVereine → Rubrik → Landkreis → Zeitraum → Suche → Ortschaft → Favoriten
- **Offline:** `try/catch` um `/api/termine`-Fetch → Meldung „🔇 Keine Internetverbindung"
- **Suche-Dropdown muss `position:fixed` sein:** `.content{overflow-y:auto}` erstellt in iOS Safari einen eigenen Stacking-Context – ein `position:absolute` Dropdown mit `z-index:100` liegt trotzdem dahinter. Lösung: `position:fixed` + Position per `_positionDropdown()` via `getBoundingClientRect()` berechnen. Dropdown-Items als `<button>` statt `<div>` – `onclick` auf `<div>` ist auf iOS unzuverlässig.

---

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
- **Auto-Update:** `update_geoip.sh` monatlich am 1. um 04:00 (Cron), Log: `/var/log/pka-geoip.log`
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
| `/verkehr <Adresse>` | Verkehrsinfo via Google Directions API |
| `/heimat` | heimat-info.de Import auslösen |
| `/heimat-add <url>` | Neue Gemeinde via Playwright entdecken |
| `/stopp-vko` / `/start-vko` | Wartungsmodus ein/aus |
| *(beliebiger Text)* | → `Todos.json` als `kategorie: pka` |

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

- **Admin-Gate:** X-Upload-Token in `sessionStorage`
- **Vereins-Auth:** bcrypt, httponly Cookie `vk_session`, 8h Timeout, Brute-Force-Schutz (5 Versuche → 15 Min.)
- **Multi-Verein-Login:** Eine E-Mail → mehrere Vereine möglich; Pre-Auth-Token (5 min) für Vereinsauswahl
- **DB:** `/opt/rename-webhook/vk_accounts.db` (SQLite WAL) – Tabellen: vereine_accounts, vk_users, vk_sessions, vk_audit, upload_quota, tg_subscriptions, page_stats, ical_feed_requests, ical_feed_vereine
- **Öffentlicher Telegram-Bot:** `@Vereinskalender_bot` – Token `KALENDER_BOT_TOKEN`; Endpunkt `/kalender-bot`
- **E-Mail:** Brevo SMTP (`smtp-relay.brevo.com:587`); `FROM_EMAIL = noreply@vereinskalender.online`

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
