# Lightweight DMSR Telegram uploader

Downloads pending supplements from `alireza5969/dmsr-supp` at a fixed source commit, sends them to Telegram, and retains only a small progress journal in this repository. Python 3.10+ and Git are the only requirements. GitHub's Ubuntu runner already provides both; no R or pip installation is needed.

## Setup and migration

1. Disable the old upload workflow/job, let any running upload finish and push its final `upload_cache.txt`, then check that the last push succeeded. Do not run old and new uploaders concurrently against the same chat. Disabling a workflow does not necessarily stop a run already in progress.
2. Create a new repository, for example `dmsr-uploader`, with `main` as its default branch. Extract this ZIP and put its **contents** in the new repository root. Include the hidden `.github` directory. Do not commit the ZIP itself or nest the project under another folder.
3. Add eight Actions secrets: `TG_TOKEN_FZM`, `TG_TOKEN_AJK`, `TG_TOKEN_MAS`, `TG_TOKEN_KF` and `CHAT_ID_FZM`, `CHAT_ID_AJK`, `CHAT_ID_MAS`, `CHAT_ID_KF`. Each token must belong to the bot added as an administrator to its corresponding reviewer channel. **Set `CHAT_ID_FZM` to the exact existing channel ID already recorded in `state/metadata.json`** (`-1003920194277` in the current repository); do not create a replacement channel for fzm. The uploader now aborts if this value changes, preventing accidental re-upload of fzm's existing files. Do not put credentials in files.
4. The source is currently public, so no source token is needed. If it becomes private, add `SOURCE_TOKEN`: a fine-grained token with Contents: read for the source repo. The new repo's built-in `GITHUB_TOKEN` cannot automatically read a different private repository. Private-source file downloads use the GitHub Contents API, so its rate limits can become relevant for small files.
5. Under Settings → Actions → General, allow the workflow to write repository contents. Branch rules must allow the workflow bot to push checkpoints to the default branch. The script verifies checkpoint persistence before sending any documents. Do not edit this branch during an upload run.
6. Open Actions → Upload stored supplements to Telegram → Run workflow. Keep `dry_run=true`. Inspect the discovered source commit, pending count and sample captions. This reads the source and cache but does not send or persist migration state.
7. Run again with `dry_run=false`, `max_files=5`. Inspect the five send attempts, the Telegram documents, and committed `state/` files. Empty/oversized/LFS files are skipped and do not count as send attempts.
8. For the full backlog, run with `dry_run=false`, `max_files=0`. To automate, uncomment the two `schedule` lines in `.github/workflows/upload.yml` and commit. Scheduled runs process the backlog within a 300-minute script budget, every six hours.

**Keep the downloader if you want new files.** Disabling all workflows on the old repo freezes its stored collection. That is appropriate for uploading the existing backlog. To keep collecting new supplements, retain a download-only workflow there and remove only the upload job. This uploader never runs the downloader. Files added during a run are picked up on the next run.

## Four reviewer channels

The workflow runs four independent jobs, one for each reviewer. Routing uses the fixed article-number ranges, not modulo sharding:

- `fzm`: 1–6,164

The existing fzm channel is already complete through article **04443 inclusive**. The fzm job excludes every file belonging to articles 00001–04443, even if an individual path is missing from the old `upload_cache.txt`. Article 04444 onward is processed normally. This cutoff applies only to fzm; it does not affect the other reviewers.
- `ajk`: 6,165–12,012
- `mas`: 12,013–17,937
- `kf`: 17,938–23,968

Each job reads its matching `TG_TOKEN_<REVIEWER>` and `CHAT_ID_<REVIEWER>` Actions secrets. Add each bot to its own channel as an administrator with permission to post. Numbers outside these ranges are not uploaded.

Each reviewer writes to `state/uploads.reviewer.<reviewer>.jsonl`. Previous journals remain readable. Journal events record their destination chat ID; an event from a different chat does not suppress delivery to a new reviewer channel. The imported legacy cache only suppresses a file when its original chat ID matches the current destination. Files previously delivered to the old shared channel are therefore eligible for delivery to the new reviewer channels.

The four jobs share the repository checkpoint branch. Each writes a separate journal and uses the existing pull/rebase/push retry logic to preserve concurrent progress. Do not run overlapping workflow runs from another repository against these same bots/channels.

## What it preserves

- Recursive discovery beneath `data/`, including the current nested range folders.
- Only regular files whose immediate parent is `<number>.PMC<digits>` are eligible, matching the old layout rule.
- Same caption text, zero-padded numbers and reviewer ranges: 1–6164 fzm; 6165–12012 ajk; 12013–17937 mas; 17938–23968 kf; otherwise unassigned.
- Sequence positions are calculated from all eligible files in the article directory before excluding uploaded files. Ordering is deterministic Unicode path ordering; unusual filename collation may differ from R/fs locale ordering. Counts describe the snapshot for that run.
- First live run imports the source `upload_cache.txt` automatically. A failed/missing cache download aborts migration; it never silently starts from scratch. It imports at runtime, not from an old copy bundled in this ZIP.
- The imported cache is path-based, just like the original. It cannot detect changed contents at previously uploaded legacy paths. Newly recorded successes use path and Git blob hash, so a changed file at the same path can be uploaded again. Renamed files are new paths.

## How it stays small

A temporary `git clone --filter=blob:none --no-checkout --depth=1` retrieves the latest commit and directory metadata, with no source-file checkout or full history. `git ls-tree` enumerates paths and hashes without asking Git to retrieve blobs. Metadata is deleted after discovery. Only pending documents are fetched over HTTPS from the pinned commit, verified against their Git blob SHA-1, held temporarily in memory for multipart upload, and released. The script never commits supplementary bytes or source Git objects.

This avoids GitHub's recursive-tree API truncation limit and repeated scans of an 86,000-file working directory. There is no per-file API metadata request for the public source. GitHub supports the clone filter; this implementation is intended for GitHub sources, not arbitrary Git servers that may ignore it.

The persistent files are:

- `state/metadata.json`: source repo, destination chat and migration commit.
- `state/legacy_uploaded.txt`: one-time imported success cache.
- `state/uploads.jsonl`: the original append-only status journal (path/hash, status, timestamp and Telegram message/file IDs on success). It is read-only history once sharding is in use.
- `state/uploads.shard<N>.jsonl`: legacy modulo-shard journals, retained for backward-compatible reads.
- `state/uploads.reviewer.<reviewer>.jsonl`: the active append-only journal for each reviewer/bot.

The script commits these small files every 25 send attempts and on normal exit/error. It pulls and pushes only the new repository. It uses Git as durable state, **not an evictable Actions cache**. A recovery artifact (one per bot) is also retained for 30 days when the workflow's final steps run. The journal/history will still grow gradually; it contains no document payloads. A private uploader repo is appropriate if you do not want its chat ID, paths and Telegram IDs publicly readable.

## Reliability and recovery

- Each bot waits the configured `SEND_INTERVAL` (default 3.85 seconds) **after the previous Telegram request finishes** before starting another. This avoids bursts caused by measuring only request start times. After a 429, the interval increases by 25% (up to `MAX_SEND_INTERVAL`, default 30 seconds); after 20 successful sends without another 429, it decreases by 0.1 seconds toward the configured baseline. Telegram's limits are approximate, and other processes using the same bot or channel can still trigger 429s. Separate bots have separate pacing, but jobs or workflows sharing one bot must share a rate budget.
- HTTP 429 honors `retry_after`, subject to the time budget. Source GET downloads retry transient failures. Telegram transport failures and server errors are **not automatically retried**, since a lost response can mean the message was already delivered. The entry becomes `uncertain` and the run stops. Uncertain or interrupted `inflight` entries remain held on later runs.
- Inspect the chat for the affected filename/caption. To resolve a held entry, while no run is active, append a JSON event for the same path/hash with `status: "sent"` if delivery is confirmed, or `status: "failed"` if you want to retry. Preserve other fields, **set `time` to the current UTC time**, and append it to the end of the same journal file that holds the held entry. Commit the change. Retrying an unverified delivery can duplicate it.
- Five consecutive definite failures stop the run. `failed` entries retry on a future run. Failures are journaled with sanitized reason codes; credentials and raw network exception messages are never intentionally printed.
- Empty files, files over 50,000,000 bytes, and Git LFS pointers are recorded as `skipped`. LFS is not implemented. Large files need a different transfer approach. To retry a skipped file after fixing the problem, change its contents or append a `failed` journal event.
- **Exactly-once delivery is not guaranteed.** Telegram sending and Git checkpointing are separate operations. A hard runner loss can leave up to 25 successful sends without a remote checkpoint; those files may be resent. The final commit and recovery artifact reduce this risk, but cannot eliminate it. Do not cancel a live run casually.
- If a checkpoint push still fails after its retries, that bot's run stops. Before restarting, download its recovery artifact, restore/merge the state events into the repository, and commit them. If neither Git nor an artifact contains the last events, reconcile the chat manually before restarting. Do not delete `state/` to resolve an error.
- Network timeouts are socket timeouts; a pathological trickling connection can last longer. The workflow step/job limits provide a final bound. Normal runs reserve time for finishing the active upload and saving state.
- One uploader configuration per chat is assumed: all bots must come from the same workflow matrix. Workflow concurrency prevents overlapping runs in this repo only. It does not coordinate with another repository.

## Local commands

Run from the new repository root:

```bash
python3 -m unittest discover -s tests -v
python3 uploader.py             # read-only discovery and migration preview
python3 uploader.py --run       # requires secrets in environment and configured Git push access
python3 uploader.py --checkpoint
REVIEWER=ajk CHAT_ID=-100... python3 uploader.py   # preview a specific reviewer's destination
```

Optional environment variables: `SOURCE_REPO` (default `alireza5969/dmsr-supp`), `SOURCE_BRANCH` (`main`), `SOURCE_TOKEN` (private source only), `TIME_BUDGET_MIN` (`300`), `MAX_FILES` (`0`, unlimited within budget), `REVIEWER` (`fzm`, `ajk`, `mas`, or `kf`), `SEND_INTERVAL` (`3.85`, minimum quiet seconds after a Telegram request), `MAX_SEND_INTERVAL` (`30`, adaptive upper bound). The shipped workflow defaults manual live runs to five attempts. Change source settings in workflow env if needed. State is tied to one source/destination; use a new repo/state for a different destination rather than recycling its success cache.

## Inspection findings, 6 November 2026

Inspected source scripts: `.github/workflows/pipeline.yml`, `R/00-utils.r`, `R/03-upload_telegram.r`, and `R/05-main_upload_telegram.r`.

The read-only smoke test examined source commit `1808bff04a6f68fa8be7ff474fafe4b3a83dcb8d`. It found 86,027 eligible files, 5,952 cached paths and 80,075 pending files. Metadata discovery took 5.59 seconds in this environment; a separate metadata clone occupied 3.2 MB. These are observations, not promised GitHub Actions or Telegram timings.

Public Actions step timings:

| Run/job | Checkout | R setup | R dependencies |
|---|---:|---:|---:|
| 42179482685 / upload | 8m 55s | 40s | 32s |
| 42232018040 / upload | 10m 53s | 17m 44s | 6m 11s |
| 42232018040 / download | 9m 08s | 31s | 29s |

These timings establish substantial checkout overhead and variable setup times. They do not establish the reason for the anomalous R installation or prove that Telegram byte throughput itself declined. Authenticated job logs and per-file timing measurements would be needed for that claim. The new uploader logs source-discovery, download, Telegram-request and checkpoint seconds separately.

The original uploader calls `git_checkpoint()` before each pass and every 100 attempts. That function stages the entire working tree (`git add -A`) and pulls/rebases/pushes the storage repo while the downloader adds new files. The uploader also recursively lists the full data directory each pass. These are confirmed sources of work that grow with the stored collection. Its 3.025-second sleep already imposes about five minutes of waiting per 100 successful files, before transfer/checkpoint time. It deliberately waits up to 20 minutes when idle. Oversized files are skipped only for the current run, so they are reconsidered next run.

Commit count alone is unlikely to explain startup: `actions/checkout` defaults to fetching one commit. It still checks out all current files. Later `git pull` operations can acquire the downloader's newer commits and file contents. Avoiding both the payload checkout and repeated storage-repo synchronization is the central improvement.

## Faster R setup if retaining the old downloader

Your workflow already uses `use-public-rspm: true` and `setup-r-dependencies@v2`, which supports binary repositories and package caching. Do not put an installed R library into Git: compiled packages depend on OS, architecture, R version and system libraries, and would enlarge checkout further.

Keep those actions; restrict dependency resolution to hard dependencies and preserve cache even after failed long runs:

```yaml
- uses: r-lib/actions/setup-r@v2
  with:
    r-version: '4.5.2'  # example fixed release; validate against your scripts
    use-public-rspm: true
- uses: r-lib/actions/setup-r-dependencies@v2
  with:
    cache: always
    dependencies: 'NA'
    packages: |
      any::readr
      any::dplyr
      any::tibble
      any::httr2
      any::jsonlite
```

Use a fixed runner such as `ubuntu-24.04` and a tested fixed R version to avoid environmental changes. `NA` means Depends/Imports/LinkingTo; the action's default `"all"` also resolves optional dependencies. Removing optional dependencies can reduce cold setup; it does not explain every observed slow run. Inspect cache-hit and source-compilation messages in Actions logs before attributing the 17-minute outlier. A prebuilt container with the needed R packages is another option for repeated cold setups, but its image download/maintenance cost should be measured first.

## Validation and references

Twenty-three offline tests cover reviewer boundaries, stable sequence counts across resume, path/hash deduplication, migration skips, journal corruption, multipart filename escaping, size bounds, rate-limit retries, ambiguous responses and budget behavior. For reviewer routing they also cover the fixed ranges, separate reviewer journals, timestamp-ordered journal merging, checkpoint push retries, and send pacing. Four concurrent checkpoint pushes to one local bare Git remote lost no events and had no failed pushes. Live read-only discovery succeeded against the public source. A real pending GIF download/hash verification and a checkpoint push to a temporary local Git remote were also checked. A live oversized-file download was correctly rejected before upload. No Telegram messages were sent, no repository was modified remotely, and the workflow has not been exercised end-to-end with your secrets. In particular, the multi-bot rate-limit behavior has not been observed against a real chat.

- Source: https://github.com/alireza5969/dmsr-supp
- Run timings: https://github.com/alireza5969/dmsr-supp/actions/runs/42179482685 and https://github.com/alireza5969/dmsr-supp/actions/runs/42232018040
- R dependency action: https://github.com/r-lib/actions/tree/v2/setup-r-dependencies
- Checkout defaults: https://github.com/actions/checkout
- Telegram group limits: https://core.telegram.org/bots/faq#how-do-i-avoid-hitting-limits
- Telegram document limits: https://core.telegram.org/bots/api#senddocument
- Tree API limits: https://docs.github.com/en/rest/git/trees#get-a-tree
