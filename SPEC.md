# taskdb — Specifica

**Data:** 2026-08-01
**Stato:** approvata dopo adversarial review Codex (GPT-5.6)
**Piattaforma:** macOS only (Darwin 25.x, Apple Silicon)
**Claude Code:** v2.1.220

---

## 1. Problema

Tre fallimenti concreti, osservati:

1. Claude Code perde traccia delle implementazioni fra sessioni quando il contesto viene compattato.
2. L'utente non ricorda a che punto è ciascuno dei ~25 progetti attivi.
3. Claude dichiara "fatto" cose che non sono verificate.

Il terzo è il più insidioso perché è *invisibile*: sparisce nella conversazione.

## 2. Vincoli ambientali reali

Esistono **tre config dir separate**, perché l'utente usa alias:

```
~/.claude              claude
~/.claude-lavoro        claude-lavoro     (CLAUDE_CONFIG_DIR)
~/.claude-cliente     claude-cliente  (CLAUDE_CONFIG_DIR)
```

Conseguenze non negoziabili:
- Gli hook vanno installati in **tutte e tre**, e l'installer deve essere rieseguibile.
- Il DB sta **fuori** da qualsiasi config dir: `~/.claude-taskdb/`.
- La dashboard deve segnalare se compare una quarta config dir non coperta.

## 3. Principi

**P1 — Un bug nel taskdb non deve mai rompere una sessione Claude Code.**
Ogni hook: try/except globale, fallimento silenzioso verso l'utente ma tracciato su
`~/.claude-taskdb/hook-errors.log`, budget di latenza, kill-switch `MYAGENTS_OFF=1`.

**P2 — Il taskdb tiene ciò che è ricalcolabile; la memoria tiene ciò che andrebbe perso.**
Confine con i file `~/.claude/projects/*/memory/*.md` esistenti. Un riassunto di stato si
rigenera da task e attività: sta qui. Un gotcha tipo *"`pkill -f` matcha se stesso"* non si
rigenera da nulla: sta nella memoria. Nessuna migrazione, nessuna sovrapposizione.

**P3 — Nel percorso critico solo scritture non bloccanti e letture indicizzate.**
Gli hook non contendono mai un lock (vedi §5).

**P4 — All'LLM si chiede di proporre, non di decidere.**
Il curatore propone task; `stale` è deterministico; `verified` richiede evidenza dell'harness.

**P5 — Onestà sul modello di minaccia.**
`evidence.source` è un meccanismo **anti-sciatteria**, non anti-bugia. Difende dalla
superficialità (dichiarare fatto ciò che sembra fatto), non dal dolo: un processo con gli
stessi privilegi può scrivere qualunque riga nel DB, e non esiste modo di impedirlo senza
un confine di privilegio che qui non c'è. Il valore sta nel rendere *visibile* la differenza
fra fatto e detto-fatto, non nel renderla impossibile.

## 4. Decisioni

| ID | Decisione | Nota post-review |
|---|---|---|
| **D1** | Cattura ibrida: hook automatici + curatore asincrono | Confermata. Corretto l'assolutismo: `tk add` manuale resta sempre disponibile |
| **D2** | Blocco morbido: `open`/`claimed`/`verified`, nessun hook bloccante | Confermata. Aggiunta la **verifica scaduta** (§8) |
| **D3** | Daemon residente: dashboard + menu bar + curatore | Confermata. La menu bar richiede un processo residente per definizione |
| **D4** | Confine con la memoria | Riformulata come P2 (ricalcolabile vs irrecuperabile) |

## 5. Architettura

```
   ┌─ PERCORSO CRITICO (sessione Claude Code) ────────────────────────┐
   │                                                                  │
   │  hook ──append O_APPEND──▶  spool/*.jsonl     (<1ms, mai un lock)│
   │  hook ──SELECT by PK────▶   project_state     (WAL: mai bloccato)│
   │                                                                  │
   └──────────────────────────────────────────────────────────────────┘
                                    │
                        drainer (UNICO scrittore)
                                    ▼
                        ~/.claude-taskdb/tasks.db
                                    ▲
              ┌─────────────────────┼─────────────────────┐
        server MCP              daemon                curatore
      (legge, propone)   (dashboard, menu bar,     (claude -p, sandbox,
                          notifiche, drainer)        solo JSON in output)
```

**Perché lo spool.** SQLite in WAL permette letture concorrenti ma **un solo scrittore alla
volta**. Con hook di sessioni parallele + daemon + curatore, le scritture si serializzano; un
lock oltre il timeout, combinato con il fallimento silenzioso di P1, diventerebbe perdita
silenziosa della fonte di verità. Lo spool elimina la contesa alla radice: gli hook appendono,
un solo processo drena. Se il daemon muore, lo spool cresce e nulla si perde.

Formato spool: una riga JSON per evento, file `spool/<pid>-<YYYYMMDD>.jsonl`, scrittura con
`O_APPEND`. Righe oltre 4KB vengono troncate con un campo `truncated: true`.
Il drainer gira nel daemon; se il daemon è giù, il primo hook `SessionStart` successivo lo
avvia in background. Il draining è idempotente: ogni evento ha un `event_id` (UUIDv4).

## 6. Schema DB

```sql
PRAGMA journal_mode = WAL;
PRAGMA synchronous  = NORMAL;
PRAGMA foreign_keys = ON;
PRAGMA busy_timeout = 3000;

CREATE TABLE schema_meta (version INTEGER NOT NULL);

CREATE TABLE projects (
  id           INTEGER PRIMARY KEY,
  key          TEXT NOT NULL UNIQUE,        -- 'negozio'
  name         TEXT NOT NULL,
  root_path    TEXT NOT NULL,
  git_remote   TEXT,
  created_at   TEXT NOT NULL,               -- ISO-8601 UTC
  last_seen_at TEXT NOT NULL
);

CREATE TABLE sessions (
  id          INTEGER PRIMARY KEY,
  session_id  TEXT NOT NULL UNIQUE,
  project_id  INTEGER NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
  config_dir  TEXT NOT NULL,                -- claude / claude-lavoro / claude-cliente
  cwd         TEXT NOT NULL,
  is_internal INTEGER NOT NULL DEFAULT 0,   -- 1 = sessione del curatore (§9)
  started_at  TEXT NOT NULL,
  ended_at    TEXT,
  end_reason  TEXT,                         -- clean | swept | unknown
  curated_at  TEXT
);

CREATE TABLE prompts (
  id         INTEGER PRIMARY KEY,
  session_id INTEGER NOT NULL REFERENCES sessions(id) ON DELETE CASCADE,
  project_id INTEGER NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
  text       TEXT NOT NULL,
  ts         TEXT NOT NULL,
  curated    INTEGER NOT NULL DEFAULT 0
);

CREATE TABLE tasks (
  id                   INTEGER PRIMARY KEY,
  task_key             TEXT NOT NULL UNIQUE,   -- immutabile: 'negozio/T-0042'
  project_id           INTEGER NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
  title                TEXT NOT NULL,
  detail               TEXT,
  status               TEXT NOT NULL
                       CHECK (status IN ('open','in_progress','claimed','verified','archived')),
  origin               TEXT NOT NULL
                       CHECK (origin IN ('todo_mirror','curator','manual','mcp')),
  origin_ref           TEXT,
  verify_cmd           TEXT,
  verify_cwd           TEXT,
  priority             INTEGER NOT NULL DEFAULT 0,
  confidence           REAL,                   -- solo se origin='curator'
  parent_id            INTEGER REFERENCES tasks(id) ON DELETE SET NULL,
  created_at           TEXT NOT NULL,
  updated_at           TEXT NOT NULL,
  last_touched_at      TEXT NOT NULL,
  claimed_at           TEXT,
  verified_at          TEXT,
  verified_commit      TEXT,                   -- HEAD al momento della verifica
  verified_fingerprint TEXT,                   -- hash dei file coinvolti (§8)
  closed_at            TEXT
);
-- 'stale' NON e' una colonna: e' derivato da last_touched_at. Deterministico, non LLM.

CREATE TABLE prompt_tasks (                    -- ponte N:N
  prompt_id INTEGER NOT NULL REFERENCES prompts(id) ON DELETE CASCADE,
  task_id   INTEGER NOT NULL REFERENCES tasks(id)   ON DELETE CASCADE,
  PRIMARY KEY (prompt_id, task_id)
);

CREATE TABLE evidence (
  id              INTEGER PRIMARY KEY,
  task_id         INTEGER NOT NULL REFERENCES tasks(id) ON DELETE CASCADE,
  kind            TEXT NOT NULL
                  CHECK (kind IN ('file_edit','command','test','claim','manual_confirm')),
  payload         TEXT NOT NULL,               -- JSON
  payload_version INTEGER NOT NULL DEFAULT 1,
  source          TEXT NOT NULL CHECK (source IN ('hook','mcp','user')),
  ts              TEXT NOT NULL
);

CREATE TABLE activity (
  id         INTEGER PRIMARY KEY,
  session_id INTEGER NOT NULL REFERENCES sessions(id) ON DELETE CASCADE,
  project_id INTEGER NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
  tool       TEXT NOT NULL,
  target     TEXT,
  exit_code  INTEGER,
  cwd        TEXT,
  ts         TEXT NOT NULL
);

CREATE TABLE curation_queue (
  id          INTEGER PRIMARY KEY,
  session_id  INTEGER NOT NULL UNIQUE REFERENCES sessions(id) ON DELETE CASCADE,
  enqueued_at TEXT NOT NULL
);

CREATE TABLE curation_runs (                   -- idempotenza reale, non un flag
  id          INTEGER PRIMARY KEY,
  session_id  INTEGER NOT NULL REFERENCES sessions(id) ON DELETE CASCADE,
  input_hash  TEXT NOT NULL,
  status      TEXT NOT NULL CHECK (status IN ('leased','done','failed')),
  lease_until TEXT,
  attempts    INTEGER NOT NULL DEFAULT 0,
  started_at  TEXT NOT NULL,
  finished_at TEXT,
  error       TEXT,
  UNIQUE (session_id, input_hash)
);

CREATE TABLE suggestions (                     -- sotto-soglia: si approvano in un click
  id         INTEGER PRIMARY KEY,
  project_id INTEGER NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
  session_id INTEGER REFERENCES sessions(id) ON DELETE SET NULL,
  title      TEXT NOT NULL,
  detail     TEXT,
  confidence REAL NOT NULL,
  status     TEXT NOT NULL DEFAULT 'pending'
             CHECK (status IN ('pending','accepted','rejected')),
  created_at TEXT NOT NULL
);

CREATE TABLE project_state (                   -- read-model materializzato
  project_id INTEGER PRIMARY KEY REFERENCES projects(id) ON DELETE CASCADE,
  injection  TEXT NOT NULL,                    -- testo GIA' renderizzato, <=500 token
  summary    TEXT,
  updated_at TEXT NOT NULL
);

CREATE TABLE settings (key TEXT PRIMARY KEY, value TEXT NOT NULL);

CREATE INDEX idx_tasks_project_status ON tasks(project_id, status, last_touched_at DESC);
CREATE INDEX idx_activity_session     ON activity(session_id, ts DESC);
CREATE INDEX idx_evidence_task        ON evidence(task_id, ts DESC);
CREATE INDEX idx_prompts_uncurated    ON prompts(session_id) WHERE curated = 0;
```

**`evidence.source`** è il campo centrale:
`hook` = osservato dall'harness · `mcp` = dichiarato dal modello · `user` = confermato a mano.
Un task passa a `verified` **solo** con ≥1 evidenza `source='hook'`. Con sole evidenze `mcp`
resta `claimed`. Vedi P5 per i limiti onesti di questo meccanismo.

**`project_state.injection`** è testo pre-renderizzato: l'hook più caldo fa **una SELECT per
chiave primaria**, non una query aggregata. È così che il budget di latenza smette di dipendere
dal numero di righe.

## 7. Hook (6, nessuno bloccante)

Un solo eseguibile `tk-hook` che dispatcha sull'evento. Solo stdlib Python.

| Evento | Azione | Budget |
|---|---|---|
| `SessionStart` | registra sessione, spazza sessioni orfane (crash/kill), avvia il drainer se il daemon è giù | <60ms |
| `UserPromptSubmit` | append prompt allo spool; legge `project_state.injection` e la inietta | <50ms |
| `PostToolUse` Edit/Write/MultiEdit | append `activity` + `evidence(source='hook')` | <20ms |
| `PostToolUse` Bash | append `activity` con exit_code; se il comando corrisponde a un `verify_cmd` → evidenza + `verified` | <20ms |
| `PostToolUse` TodoWrite | mirror dei todo interni → task (upsert su titolo normalizzato) | <30ms |
| `SessionEnd` | chiude sessione, inserisce riga in `curation_queue` | <20ms |

**Nessun hook `Stop`, nessun `PreToolUse`, nessun hook che può bloccare o rallentare l'utente.**

**Degrado sicuro:** ogni hook valida la forma del payload JSON in ingresso; se non la riconosce
(cambio di versione di Claude Code) diventa no-op e scrive una riga in `hook-errors.log`.
La dashboard mostra un avviso se il tasso di errori supera una soglia.

## 8. Verifica e verifica scaduta

`verify_cmd` non prova i criteri di accettazione: `true # pytest`, un test filtrato o il
comando giusto nel worktree sbagliato danno tutti exit 0. È un fallimento **onesto**, quindi
molto probabile. Mitigazioni:

- Si registra il comando **effettivo**, la `cwd`, l'exit code e il commit HEAD.
- Alla verifica si calcola `verified_fingerprint` = hash dei path coinvolti + loro mtime/size.
- **Verifica scaduta:** se quei file cambiano dopo, il task resta `verified` ma viene mostrato
  come *scaduto* (giallo) in dashboard e menu bar, e rientra nell'iniezione.

Un `verified` vecchio su codice cambiato non è una verifica.

## 9. Curatore

**Trigger:** riga in `curation_queue` (inserita da `SessionEnd`). Mai a timer.

**Esecuzione:** `claude -p` headless, che usa l'abbonamento Claude Code invece dell'API a consumo.

**Sandbox — obbligatoria:**
- `MYAGENTS_OFF=1` e config dir dedicata → i suoi hook non scattano. Senza questo, la sessione
  del curatore genererebbe il proprio `SessionEnd`, riaccodando la cura di sé stessa,
  all'infinito.
- La sua sessione, se registrata, è marcata `is_internal = 1`.
- **Nessun tool:** niente Bash, niente Edit, niente MCP. Legge testo, restituisce JSON.
  Necessario perché legge testo non fidato: qualunque cosa l'utente incolli in chat passa di lì.

**Input:** prompt non curati + `activity` + todo mirrorati della sessione.
**Output JSON:** `{nuovi_task[], collegamenti[], suggerimenti[], summary}`.

**Limiti duri:**
- Massimo **5** nuovi task per sessione. Il resto va in `suggestions`.
- Sotto soglia di confidenza → `suggestions`, mai `tasks`.
- Il curatore **non può** marcare `stale` né chiudere task: `stale` è temporale, la chiusura
  richiede evidenza.

**Idempotenza:** `curation_runs` con `input_hash` e lease. Un crash a metà non duplica e non perde.

## 10. Iniezione di contesto

Budget duro ~500 token. Contiene **stato, non storia**:

```
[taskdb · negozio]
Aperti: homepage redesign · PR-B2 pending merge
⚠ Claimed non verificato: "badge PRO·Lv PR#4" (2gg) — nessuna prova raccolta
Ultima sessione 3h fa: toccati 4 file in src/components/pricing/
```

Decadimento: task non toccato da 7 giorni → considerato stale → fuori dall'iniezione,
visibile solo in dashboard.

## 11. Server MCP — esattamente 6 tool

`task_list` · `task_add` · `task_update` · `task_link_evidence` · `project_state` · `search_history`

Ogni tool esposto consuma contesto in *ogni* sessione, per sempre. Sei è il tetto.
`task_link_evidence` scrive sempre `source='mcp'`: il valore non è passabile dal chiamante.

## 12. Daemon, dashboard, menu bar

**Daemon** — FastAPI, avvio al login via `launchd`, `localhost:7777`, SSE per il live update.
Ospita: drainer, curatore, notifiche, API della dashboard e della menu bar.
Non è mai nel percorso critico di una sessione.

**Dashboard** — vista globale progetti → drill-down → task → evidenza (file e comandi reali).
Azioni: conferma `claimed`→`verified`, archivia, riapri, priorità, approva/rifiuta suggerimento.

**Menu bar** (`rumps` / PyObjC) — l'icona *è* l'informazione:

| Icona | Significato |
|---|---|
| `○` grigio | niente di aperto |
| `● 7` blu | 7 task aperti, tracciati |
| `🟡 3` giallo | 3 dichiarati fatti senza prova, o con verifica scaduta |
| `🔴` rosso | daemon giù: la cattura continua, la cura no |

Dropdown: progetti con conteggi → sottomenu per progetto con i task e le azioni rapide →
`Riprendi in terminale` (apre il terminale nella cartella giusta con l'alias giusto).
La menu bar non ha una copia dei dati: interroga il daemon. Se il daemon è giù mostra rosso
e non finge di sapere.

**Vista markdown** — il daemon rigenera anche un `.md` per progetto. Costa dieci righe, è
leggibile da qualunque cosa, e se un giorno l'intero sistema muore lo stato resta in chiaro.

## 13. Rilevamento progetto

`git remote` → git root → path del cwd, con override manuale persistente per cartella
(`~/.claude-taskdb/overrides.json`). Casi da gestire: monorepo, git worktree, cwd = `~`.

## 14. Cosa NON fa

Niente sync cloud, niente multi-utente, niente integrazione Jira/GitHub, niente stime o
burndown, niente dipendenze fra task, e soprattutto **nessun agente che rilegge il codice per
giudicare se il lavoro è fatto**: costa molto e sbaglia. L'exit code costa zero e non mente.

## 15. Fasi

Ogni fase è utile da sola e ha un criterio di verifica eseguibile.

| Fase | Contenuto | Verifica |
|---|---|---|
| **1** | core + schema + spool + drainer + 3 hook + CLI `tk` | Sessione reale con 2 edit → `tk list` mostra attività e task veri |
| **2** | iniezione contesto + server MCP | Aprendo un progetto, lo stato compare in contesto senza chiederlo |
| **3** | daemon + dashboard + menu bar | Icona in barra con conteggi corretti, dropdown navigabile |
| **4** | curatore | Una sessione chiusa produce task sensati, zero ricorsione, zero duplicati al retry |
| **5** | notifiche, stale decay, verifica scaduta, installer 3 config dir | Un `verified` su file modificati torna giallo |

Fermandosi alla Fase 2 metà del problema è già risolto.

## 16. Da verificare empiricamente prima di scrivere l'hook (Fase 1)

1. **Canale di iniezione:** `UserPromptSubmit` inietta da stdout grezzo o dal JSON
   `additionalContext`? Sono contratti diversi con effetti diversi sul contesto.
   Test da cinque minuti sulla v2.1.220 installata. Non assumere.
2. **Timeout reale degli hook** e comportamento in caso di superamento.
3. **Cold start Python** su questa macchina: misurare, non stimare.
4. **Comportamento nei subagent:** gli hook `PostToolUse` scattano anche lì? Se sì,
   l'attività va attribuita alla sessione padre (accettabile) — ma va confermato.

## 17. Rischi residui accettati

| | Rischio | Perché lo accetto |
|---|---|---|
| R1 | Il mirror TodoWrite dipende dal fatto che Claude apra un todo | Compensato dal curatore; copertura parziale è meglio di zero |
| R2 | `claude -p` headless attinge alla stessa quota della sessione utente | Da misurare in Fase 4; una passata per sessione è poco |
| R3 | Il curatore vede cosa è stato chiesto e toccato, non cosa Claude ha risposto | Mitigato passandogli l'attività |
| R4 | Le 3 config dir vanno tenute sincronizzate | Installer rieseguibile + avviso in dashboard |
| R5 | `evidence.source` non è enforced a livello di privilegio | Vedi P5: è anti-sciatteria, non anti-dolo. Dichiarato, non nascosto |
| R6 | La cattura automatica produce falsi negativi e qualche task fantasma | Il confronto non è con un sistema perfetto ma con lo stato attuale, che ha recall zero. Il rumore si archivia in un click |
