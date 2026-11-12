# Upload calibration rules: deploy and rollback

Branch `upload-calibration-rules` on top of deploy commit cbe9e6e. NOT deployed.

## Deploy (do not run without Bray's go; never while a stack/upload batch is in flight)
1. Back up:
   - `sudo -u starfront sqlite3 /var/lib/starfront/collab.sqlite ".backup /var/lib/starfront/collab-pre-rules-$(date +%Y%m%d-%H%M).sqlite"`
     (disk: root has ~19 GB free; the DB is 4 MB. Copy it off the box too.)
   - `tar czf /root/starfront-config-pre-rules-$(date +%Y%m%d).tgz /etc/systemd/system/starfront.service /etc/caddy/Caddyfile /var/lib/starfront/discord.env /var/lib/starfront/admin-token.txt`
2. Tag what runs now: `cd /opt/starfront && git tag pre-upload-rules cbe9e6e`
3. Bring the branch over (git bundle from the box), `git merge --ff-only upload-calibration-rules`,
   then `git tag deploy-2026-11-xx-upload-rules`.
4. `systemctl restart starfront` (the DB migration adds three columns to `files`: status, reasons, meta - additive only).
5. Check: `curl -s https://collab.starfront.space/api/v1/health`, open /files, and
   `curl -s https://collab.starfront.space/api/v1/projects/<id>/upload-rules`.
6. Optional, Bray's call: grandfather or recheck existing files
   (`POST /api/v1/projects/<id>/files/recheck` as the coordinator; header-only reads).

## Rollback
1. `cd /opt/starfront && git checkout pre-upload-rules` (or `git reset --hard pre-upload-rules` after confirming), `systemctl restart starfront`.
2. The old code ignores the three new columns, so the DB does not need restoring. If wanted:
   stop the service, copy the `.backup` file back over collab.sqlite (remove -wal/-shm), start it.
