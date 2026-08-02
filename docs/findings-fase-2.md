# Fase 2 — il contratto di iniezione del contesto

**Data:** 2026-08-01 · **Claude Code:** v2.1.220 · **macOS**

## Fonte

Non documentazione ne' inferenza: **codice funzionante installato su questa macchina**,
`~/.claude/plugins/cache/claude-plugins-official/superpowers/6.1.1/hooks/session-start`.
E' il meccanismo che inietta il bootstrap di superpowers all'avvio di ogni sessione —
osservabile in cima al contesto di qualunque sessione su questo Mac.

## Il canale

Un hook inietta contesto scrivendo **JSON su stdout** e uscendo con **0**:

```json
{
  "hookSpecificOutput": {
    "hookEventName": "UserPromptSubmit",
    "additionalContext": "il testo che Claude vedra'"
  }
}
```

`hookEventName` va valorizzato con il nome dell'evento corrente.

## Trappola: non emettere due formati

Dal commento nel codice di superpowers:

> Claude Code reads BOTH `additional_context` and `hookSpecificOutput` without
> deduplication, so we must emit only the field the current platform consumes.

Emettere entrambi i formati "per sicurezza" — la scelta istintiva quando non si sa
quale funzioni — **inietta il testo due volte**, a ogni singolo messaggio, per sempre.
Su un bigliettino da 500 token significa 1000 token sprecati per prompt.

Le tre forme esistenti, da non confondere:

| Piattaforma | Campo |
|---|---|
| **Claude Code** | `hookSpecificOutput.additionalContext` (annidato) |
| Cursor | `additional_context` (primo livello, snake_case) |
| Copilot CLI / SDK | `additionalContext` (primo livello) |

Si distinguono per variabile d'ambiente: `CLAUDE_PLUGIN_ROOT` senza `COPILOT_CLI`
identifica Claude Code. taskdb gira solo su Claude Code (SPEC: solo macOS, un utente),
quindi emette **solo** la forma annidata: nessun rilevamento di piattaforma da mantenere.

## Ancora da verificare empiricamente

Il canale e' certo. Questi no, e vanno misurati prima di costruirci sopra
(la §16 della SPEC vieta di assumere, e la fase 1 ha gia' pagato una volta per un
`exit_code` dato per scontato e mai esistito):

1. **Timeout degli hook.** Quanto, e cosa succede al superamento: l'iniezione va persa
   in silenzio o il turno dell'utente si blocca? Il budget di 50ms della SPEC nasce da
   una stima, non da questo dato.
2. **Limite di dimensione** di `additionalContext`, e comportamento al superamento —
   troncamento silenzioso o errore.
3. **Composizione con altri hook** sullo stesso evento (ECC ne ha di suoi su
   `UserPromptSubmit`): gli output si sommano? In che ordine? Uno puo' sopprimere l'altro?
4. **Dove finisce il testo iniettato**: nel transcript come se l'avessi scritto tu,
   oppure come contesto separato tipo system-reminder.

I punti 1 e 3 si misurano alla prima sessione reale con l'iniezione attiva: basta
guardare se il testo arriva, quanto ci mette e se convive con gli hook di ECC.
