# Is the directory what was planned?

A pass rate over the calls that happen to be in a directory says nothing about the calls that
should be. A batch stopped half-way, a run file deleted, a file left unread: each leaves a
smaller directory that grades as well as a whole one. Every command that reads a run directory
now asks the same question first (`synthiq_caller.completeness`): was every planned call
recorded, and is every recorded file still as the rig wrote it?

## What is compared

- **Planned.** `manifest.json` records, before the first call, every call a batch means to place:
  one `{file, repeat, kind}` per scenario repeat and per chain repeat. The batch entry carries
  `status`: `running`, then `finished`, `interrupted` (Ctrl-C or SIGTERM) or `failed`.
- **Recorded.** A run file, a chain file, or a held-back record (`held_back`, written when the
  batch ends) answers to each planned call. A call the rig held back (a clip past retention,
  a clip file that is missing, a scenario that asks nothing) is accounted for: it was not
  meant to be a result.
- **Readable.** Every run, chain, manifest and batch file parses as what it is. A file that
  does not is never skipped: the command prints the file and why, and exits 2.

## The four states

| State | Meaning |
|---|---|
| complete | every planned call is accounted for |
| incomplete | planned calls never ran, a run file was removed, `batch.json` counts more placed calls than there are run files, or a batch has files that are not in its plan or repeat a call already recorded (a copied or added run: never counted as a call) |
| altered | a run file no longer matches the digest the rig stamped on it, or a run in a planned directory has had its digest removed |
| unknown | the directory was made before plans were recorded; a missing call cannot be seen |

## What each command does

| Command | complete | incomplete / altered | unknown (older directory) |
|---|---|---|---|
| `gate` | as before | exit 1, the directory is incomplete | judged on the numbers, prints `completeness unknown`; `--require-complete` makes it exit 1 |
| `acceptance` | as before | the claim is not met (exit 1), the reason on the page | judged on the numbers; the page says `completeness unknown` and lists it under "not shown"; `--require-complete` makes the claim not met |
| `report --fail-under` | as before | threshold not met, exit 1 | prints `completeness unknown`, exits by the numbers |
| `compare` | as before | `--fail-on any` and `beyond-noise` exit 1 | warns |
| `baseline` | writes | writes nothing, exit 1 | warns |
| `upload` | as before | refused unless `--allow-incomplete`; the bundle's summary carries `completeness` (status, planned, accounted, held back, unstamped, problems, and `override`: true when `--allow-incomplete` sent a directory known not to be complete) | warns; the summary says `unknown` |
| `scorecard` | as before | the card records `completeness` | records `unknown` |
| `regrade` | as before | warns first (regrading cannot make a call happen) | as before |

Exit code 2 (an unreadable file) is new for every reader and is the same everywhere. No other
exit code changed meaning.

`compare --fail-on any` and `beyond-noise` also fail when none of the baseline's scenarios is in
the batch (an empty or wrong directory has no regression in it because it has nothing in it).
A directory that does not exist exits 2.

## Other refusals that belong with this

- `batch` of a directory with no scenarios, or `--only` matching none, stops before any call
  (exit non-zero with a message); a batch that places no call at all exits non-zero.
- `validate` of an empty or missing scenario directory exits non-zero.
- `validate` and `batch` look inside a clip. A WAV of a few bytes, with no audio, or cut short is
  `CLIPS NOT USABLE`, and a batch holds the call back as the rig's fault with the reason instead
  of playing silence and charging the agent. A clip file that is missing at call time is held
  back the same way.
- `gate` refuses runs made against different versions of one scenario file, as acceptance and
  regrade do (`--allow-mixed-scenarios`).
- `acceptance` treats a manifest with no batch entries as no manifest.
- `gate` prints the consent, provenance and unverified-label warnings acceptance prints. They do
  not change its verdict.

## Held-back calls

A call the rig held back is accounted for, but it did not run. Planned 79, 20 held back, 59 passed
is not a pass of the plan. `gate` and `acceptance` count held-back calls as "not run" (in the
"Not judged" figure and the "attempted" denominator), print `held back: N of M planned` beside
the verdict, and are NOT met while any call was held back. `--allow-held-back` (gate and
acceptance) judges only the calls that ran and says so in the output and in the report
(`held_back.allowed`).

## Counts and the baseline

`compare` fails (both `--fail-on` modes) when a scenario has fewer graded calls than the
baseline was made from: a batch that lost a call, or its manifest, does not read as "same". More
calls than the baseline is not a failure. Wherever completeness is printed, the number of run
files with no digest is printed too.

## The digest

Each run and chain file the rig writes carries `integrity.sha256` over the rest of the file. A
hand edit of `grade.outcome` is caught as `altered`. It sits in the file it protects, so anyone
who means to alter a run can recompute it: it detects accidental edits and is not
tamper-proofing. `regrade` recomputes a grade from the recorded evidence, so it can repair an
edited grade and stamps the file again. Files written before digests existed have none and are
only counted.

## Older directories

A directory with no plan reads as `unknown`. It stays readable by every command and none
refuses it by default; each says "completeness unknown". A run file deleted from one is still
seen when `batch.json` records more placed calls than there are files.

## Not covered

A run file copied in from another directory with the same batch id and a call the plan lacks is
caught; one that swaps in for a recorded call of the same batch (same file, same repeat) is not.
Accidental-edit protection only.

Only a call the rig planned is counted. Phone-leg server runs and `forget`'s own bookkeeping are
outside this check.
