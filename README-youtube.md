# HKfuntown notifications

`scripts/youtube_feed.py` reads the channel's RSS and uses `youtube_state.json`.
`last_video.txt` is retained only as a historical reference and is no longer used.
Both IDs from its Git history were migrated as `legacy_seen`.

Only unseen videos published after the migration cutoff (2026-09-26 11:25:15 UTC)
and no later than the current time can notify. The cutoff is fixed: delayed RSS
entries after this date still qualify. IDs are never pruned. Missing, corrupt,
empty, or invalid input fails closed. RSS outages retry on the next schedule;
the inconsistent `/videos` yt-dlp fallback was removed.

Scheduled runs deliver automatically. Manual runs default to dry run. The
separate YouTube Preview workflow is also log-only, with no webhook secret.
Run `python scripts/youtube_feed.py` for a live RSS dry run and
`python -m unittest discover -s tests -v` for offline regression tests.

The feed workflow checks out current main under a shared concurrency lock.
Each new ID is committed and pushed BEFORE its webhook POST. A rejected push
stops delivery. This deliberately provides at-most-once attempts: a crash after
reservation, or an unsuccessful/uncertain POST, may miss a notification, but
will not spam Discord on retry. `reserved` means attempted or pending, not proof
of successful delivery. Successful acceptance is recorded in the run log.
For recovery, first inspect the failed run and Discord; only if the video was
not delivered, manually remove its reservation and rerun. Never clear the state
file or rerun an old version of the workflow to recover.

The existing `contents: write` permission and `DISCORD_WEBHOOK_URL` secret are
used; no new secret or wider permission is required. The webhook content retains
a bare YouTube watch URL without suppression flags so Discord can embed it.
