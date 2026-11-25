# Clip consent, provenance, and `forget`

A clip is a recording of a person. This page says what the bank records about each clip, what the tool refuses to do with a clip that lacks the record, and how a clip is removed everywhere this tool can reach.

Nothing here identifies a speaker. Every reference is **given** by a person and **opaque**: the tool never listens to a clip to work out who spoke, never matches voices, and never stores a voice embedding. The key that links an opaque id to a real person is kept by you, outside the bank.

## What each manifest field means

Added to `manifest.csv`, all optional. `clipbank build` writes them from what you give it (`--provenance`, `--speaker-ref`, `--lawful-basis`, `--consent-ref`, `--recorded-on`, `--retain-until`, `--call-fields`). Existing banks need no change to load.

| Column | Meaning | Allowed values |
|---|---|---|
| `provenance` | Is this a recording of a person, or a made voice? | `real`, or `synthetic` (a description may follow the word). `real-call` is read as `real`. Empty or anything else is **unknown** |
| `source_kind` | What the recording came from | `call`, `session` (a recording session) |
| `source_ref` | Its id on the platform it came from | An id of 3 to 80 characters, no spaces; not an address or a number. `build` fills it with the call id |
| `speaker_ref` | Who spoke, as an id | `spk_` then 8 to 32 lowercase letters and digits, at least one of each. Never a name or a phone number |
| `lawful_basis` | The UK GDPR Article 6(1) basis you rely on to hold and use the recording | `consent`, `contract`, `legal_obligation`, `vital_interests`, `public_task`, `legitimate_interests` |
| `consent_ref` | Where the consent record is held | An id of a record kept elsewhere. Required when the basis is `consent` |
| `recorded_on` | The day it was recorded | `YYYY-MM-DD` |
| `retain_until` | The last day the clip may be used | `YYYY-MM-DD`, not before `recorded_on` |
| `label_confirmed_by` | Who listened and confirmed what the clip says | `rev_` then 8 to 32 random lowercase letters and digits. Needs `label_confirmed_on` |
| `label_confirmed_on` | When they did | `YYYY-MM-DD` |

The older `checked_by_human` column still works and still means "a person confirmed this label". A clip counts as confirmed if either it or `label_confirmed_by` is filled in.

The id rules cannot stop someone typing `spk_jane1985`. Mint ids at random and keep the mapping to people somewhere else.

**Not in the tool, and a decision for the owner.** Recordings of what patients say about their health may be special-category data, which needs an Article 9 condition as well as an Article 6 basis. There is no field for it. Using a recording of a caller as test audio is also a different purpose from the call it was made for, so it is not enough that the call was lawful; decide the basis with whoever advises you on data protection. This page is not legal advice.

### What the tool does with them

- **`validate`** (with `--clips`) fails when a clip is past `retain_until` or a field is written and wrong. It reports, without failing, each clip with no provenance and each real recording that lacks a speaker ref, a lawful basis or a date. A scenario that names a clip past retention is marked `CLIPS PAST RETENTION`; one that names a clip with unknown provenance says so.
- **`batch` and `run`** will not play a clip past its retention date (no flag allows it). They will not play a clip whose provenance is unknown unless given `--allow-unknown-provenance`; the call is then stored with `provenance_unknown` listing the clips, and `report` and `acceptance` say "provenance unknown", the same way they say "labels unverified". A clip that is not in any manifest has unknown provenance.
- A clip in an old manifest, or one with no `provenance` cell, is *unknown*, not *real*. Before this change the bank treated a missing value as real. To play an existing bank without the flag, add the column: `provenance` is `real` for recordings of people and `synthetic` for made voices.

## `forget`

```
synthiq-caller forget <clip id | spk_ref> --clips <bank> [--clips-extra <bank>]
    [--scenarios <dir>]... [--results <dir>]...
    --operator op_<opaque id> [--reason erasure_request|consent_withdrawn|retention_expired|other]
    [--request-ref <id>] [--dry-run]
```

Run it with `--dry-run` first: it lists everything below and changes nothing, not even the log.

It takes a clip id (with or without `.wav`), or a speaker ref, which means every clip with that ref in any manifest or earlier removal record. Then, for each clip:

1. **Audio.** Deletes `<clip>.wav` (and any other audio file of that name) from every bank directory.
2. **Manifest.** Removes the clip's row from each `manifest.csv` and its entry from `overrides.json`, because both hold a transcript of what the person said.
3. **Scenarios and chains** in each `--scenarios` directory that play it. The step's clip becomes `missing/<clip>` and the scenario gets a `missing_clips` entry saying the clip was removed. From then on `validate` prints `MISSING CLIP` for it and a batch lists it under "not run: missing clips". The file is rewritten as JSON in the indent it had; key order is kept. Restore it by recording a new clip and editing the step back.
4. **Stored results** in each `--results` directory: every `run-*.json`, `chain-*.json` and `batch.json` that names a clip gets a top-level `clips_removed` list (`clip`, `removal`, `removed_on`). A chain file is marked when any run it points to was. The grade is not touched, so a result stays what it was; `report` adds "clips removed: N call(s)" so nobody mistakes it for something that can be played again.
5. **The record.** Appends one line to `removals.jsonl` in the `--clips` directory: what (clip ids, the speaker refs they carried), when, why, which operator (an opaque id), the request reference, and the counts and file names touched. It holds no name, no transcript and no audio. Nothing is ever rewritten or removed from it. A bank reads it, so a removed clip stays ruled out even if a copy of the file is put back; a line that is not valid JSON stops the bank from loading, because skipping it would bring a clip back.

It is idempotent. Run again, it finds nothing left to change and says so; no second record is written for no work. Run again with a further `--results` directory, it marks that directory too and writes a record of that work. If it is interrupted, run it again: the audio is deleted first, the record is written after the stored files change, and the manifest rows go last.

### What it lists and does not change

- Any other file in a results directory that names the clip (`upload.json`, scorecards, acceptance pages) is listed as "left alone".
- Sound files of calls that played the clip (the phone leg's `audio/` recordings) are listed with their paths and **not deleted**: the caller's side of the call holds the clip's voice. Delete them if the request covers them.

### Outside this tool: said again every time it runs

- **Runs uploaded to a dashboard** hold the clip names and the transcript of what the caller said, in the server's database. `forget` cannot change them. Delete those runs on the server, or upload the directory again with `upload --replace` after the results here were handled. A directory with `upload.json` was uploaded.
- The carrier's and the voice engine's own recordings of the calls, and any backup or copy of the bank, scenarios or results made before.
- Transcripts in stored results (`record.said`, `read_back`, `trace`) stay as they are. `forget` marks them; it does not redact them. Whether stored results must be redacted to meet an erasure request is a decision for the owner.

### On timing

Under UK GDPR an erasure request is answered within one month. The log is how you show that the removal happened and when; the request itself, and who made it, belong in your own records, referred to here only by `--request-ref`.
