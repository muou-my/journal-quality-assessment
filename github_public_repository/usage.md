# Commands, recovery and common errors / 操作手册

All commands use Python scripts/public_cli.py or the installed `jqa` console command.
Run `doctor` first. Provide your own paths; outputs must be outside the installed
Skill and new output filenames/directories are required. No historical DB is needed.

## Agent file mode

1. `prepare --csv INPUT --mode single|multi --provider agent --output RUN --run-id ID`.
2. `status --run RUN --output STATUS.json`, then `next --run RUN --output DISPATCH`.
3. Read one public task only. `start --run RUN --task-id TASK` records actual start.
   Quality flags require the separately reviewed `--accept-quality-risks` choice.
4. Save actual JSONL through `save-response --run RUN --task-id TASK --attempt-id ATTEMPT
   --input ORIGINAL_UTF8 --response NEW_RAW`; then `import` with the same IDs/response.
5. `export --run RUN --output EXPORT`; append `--allow-partial` only for nonfinal diagnostics.

Use existing schema keys only. Originality/significance/rigour are strict integers,
never floats, strings or booleans. Evidence and uncertainty are required. Models
copy routing fields, which is not independent model identity verification.

## API mode

`prepare --provider deepseek` creates a fresh isolated API run from the user's CSV.
Its model run ID is the selected preparation ID with `-api` appended. There is no
dependency on another researcher's corpus or historical evaluation. `api-estimate`
prints next20/all-pending local estimates; `api-status` shows receipts and usage costs.

`run/resume --allow-paid --confirm-send` explicitly authorizes the displayed data
transfer/estimated charges. Omit confirm-send for a secure interactive SEND prompt.
Without paid authorization there is no key read or model call. `--max-tasks 10`
limits new unique tasks for that invocation, not retries. Without a limit execution
continues until completed, configured budget/total-task cap, interruption or error.

Do not edit frozen configuration to change model/Prompt/prices. Create a new run
for changed experimental parameters. Provider defaults and prices are examples,
not a promise of current alias availability or final billing; recheck the provider.
USD is the accounting currency; CNY conversion must be user-supplied and dated.

## Persistence and recovery

Each pair is an independent HTTP conversation. Request parameters, original HTTP
bytes, model content and validated JSONL are distinct archives. Usage is provider
reported; missing usage has a conservative cost hold. Retries get separate attempt
IDs with retry_of but do not increase independent papers/pairs. Completed imports
are idempotent. Conflicts are never overwritten. Same-directory fsync+hard-link
commits require filesystem support; `doctor` tests it at the user's output location.

Known429/5xx/pre-send connection faults retry with bounded exponential backoff.
401/402 stop for credentials/balance. A timeout after POST can have executed and
remains uncertain; never assume it was free or never sent. On Ctrl+C no more work
is dispatched; in-flight work is drained/saved. Socket timeout is not an absolute
wall-clock deadline when a service keeps sending data. On hard kill, inspect sent
records/receipts and provider-side history before further work.

`resume` recovers surviving receipts before new calls. Storage/score/schema errors
create persistent safety halts. Original-model failures are distinct from local
file-write failures. Up to the already-dispatched concurrency window may finish
after stop; no new tasks are replenished. Never substitute synthetic demo scores.

After actual audit, `api-reconcile --run RUN --request-id N --evidence DESCRIPTION`
authorizes a retry disposition without calling a model. Unknown active delivery
additionally requires `--delivery-reviewed` ONLY after confirming the prior work
ended and its original response is unrecoverable. Existing responses/attempts/cost
holds remain. Other unresolved work prevents safe clearance. `review-stop` by
itself cannot resolve an inconsistent API request ledger.

## Export and cleaning

`export` writes articles.csv, journals.csv, scores.json, progress.json, provenance,
raw responses/inventory, accepted JSONL, errors and execution logs. Coverage and
population SD are included. Partial outputs remain partial_not_final. Sort journal
means only with coverage displayed; statistical comparability is not automatically proved.

`runs --data-root DIRECTORY` lists local user runs. To permanently remove ONE owned
run, `clean --run RUN --data-root PARENT --confirm-run-id ID`; it checks the managed
marker, resolved child path and running state. It does not remove installed code.
Back up important research before requesting cleanup. Uninstalling the Skill never
cleans research data.

## Moving computers / 移机

Install the identical released code version, copy the complete user run, then run
`relocate --run NEW_PATH --confirm-run-id ID`. It backs up SQLite before physical
path rebinding; scores/attempt events and frozen data remain untouched. Resolve
running/unknown work before relocation. Frozen engine hashes prevent silent changes
across upgrades; keep the previous code backup for old experiments.

## Frequent errors

- No Python/sqlite/venv: install a complete Python3.10+ distribution; no API key fixes this.
- Output inside code: choose a separate data directory, not site-packages/Skill root.
- UTF-8/CSV errors: save UTF-8/BOM and quote commas/newlines; original files are never fixed in place.
- Duplicate DOI/conflict: review original records; do not silently choose arbitrary metadata.
- Existing output: choose a new export/install/build snapshot, not an overwrite.
- Hard-link denied: choose a supporting filesystem/authorized terminal. No overwrite fallback.
- Frozen hash mismatch: use the exact experiment version; do not edit pins to bypass it.
- Locked run: inspect safety-status, attempts, receipts and costs; do not simply restart every task.
- No Key: validation, preparation and synthetic demo still work. Keys stay in local hidden input/ignored files.
- Agent lacks shell/network: use the standalone CLI in a suitable environment; plugin install is not a permission grant.
