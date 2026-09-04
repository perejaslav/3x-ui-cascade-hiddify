# Security and Rollback

Secret handling, backup procedure (WAL-safe), and the rollback algorithm with verification.

## Secret handling

### Always redact (never print, never store in reports)

```text
UUID
password
Reality private key
API token
session cookie
subscription URL
full share URI (vless:// trojan:// ss:// ...)
```

### Safe to show

```text
host, port, protocol, tag
masked UUID: 8f23****91ab
masked token: eyJhbGci**** (first 8 chars + mask, only when identification is required)
```

Masking rule: first 4 + last 4 characters, `****` in between. If a value is shorter than 12 characters, show only the type ("UUID: (redacted)").

### Rules

- Never output secrets in chat, final reports, logs, or persistent memory.
- Do not put secrets inline into shell commands (shell history). Use env vars or `--header @file`.
- Never commit credentials; keep them out of repository files and test fixtures.
- Delete temporary credential/cookie files after work (`cookies.txt`, token files).
- If a temporary SSH private key must be stored: temp file, `chmod 600`, delete after work.
- Log redaction: before showing `journalctl` / Xray / 3x-ui / Docker logs, filter out UUIDs, passwords, keys, tokens. Prefer showing only line counts, error levels, and error text with secrets stripped.

### Default credentials

Never assume `admin/admin` works. It exists only in fresh Docker test images as a historical/testing fact and must be changed after first login even there. Production workflows always ask the user for credentials and never fall back to defaults.

## Backup procedure

Create a dated backup directory on each server **before** any modification.

```bash
BACKUP_DIR="$HOME/3x-ui-cascade-backup-$(date +%Y%m%d-%H%M%S)"
mkdir -p "$BACKUP_DIR"

# System identity and state
uname -a > "$BACKUP_DIR/uname.txt"
cat /etc/os-release > "$BACKUP_DIR/os-release.txt" 2>/dev/null || true
whoami > "$BACKUP_DIR/whoami.txt"; hostname > "$BACKUP_DIR/hostname.txt"
date > "$BACKUP_DIR/date.txt"
timedatectl status > "$BACKUP_DIR/timedatectl.txt" 2>/dev/null || true
ss -tulpen > "$BACKUP_DIR/ports.txt" 2>/dev/null || true
systemctl list-units --type=service > "$BACKUP_DIR/services.txt" 2>/dev/null || true

# Firewall state
ufw status verbose > "$BACKUP_DIR/ufw.txt" 2>/dev/null || true
firewall-cmd --list-all > "$BACKUP_DIR/firewalld.txt" 2>/dev/null || true
iptables -S > "$BACKUP_DIR/iptables.txt" 2>/dev/null || true
nft list ruleset > "$BACKUP_DIR/nft.txt" 2>/dev/null || true

# Locate 3x-ui data
find /etc /usr/local /opt -maxdepth 4 \( -iname '*x-ui*' -o -iname '*xray*' \) 2>/dev/null | head -n 50
```

### 3x-ui database backup (SQLite WAL-safe)

The database (typically `/etc/x-ui/x-ui.db`) runs in WAL mode. A plain `cp` of a live WAL database can produce an inconsistent snapshot. Use one of, in order of preference:

**1. 3x-ui built-in backup** (panel: Панель настроек → Резервное копирование, or CLI `x-ui` menu → backup) when available.

**2. sqlite3 backup/checkpoint inside the host or container:**

```bash
# inside the container (Alpine: apk add --no-cache sqlite) or on the host
sqlite3 /etc/x-ui/x-ui.db ".backup '$BACKUP_DIR/x-ui.db.bak'"
# or checkpoint WAL first, then copy:
sqlite3 /etc/x-ui/x-ui.db "PRAGMA wal_checkpoint(TRUNCATE);"
docker cp <container>:/etc/x-ui/x-ui.db "$BACKUP_DIR/x-ui.db"   # Docker installs
```

**3. Stop-then-copy** (only if the panel can be briefly stopped and the user approved): stop 3x-ui, copy `x-ui.db` (+ `-wal`, `-shm` if present), start 3x-ui.

Also export the logical state via API when possible (read-only):

```bash
curl -s -H "Authorization: Bearer $TOKEN" "$PANEL/panel/api/inbounds/list" > "$BACKUP_DIR/inbounds.json"
curl -s $AUTH "$PANEL/panel/api/xray/getDefaultJsonConfig" > "$BACKUP_DIR/xray-template.json"
```

Backup content checklist:

```text
3x-ui database (WAL-safe)     x-ui.db.bak
3x-ui configuration           settings export
Xray template                 xray-template.json
inbounds / outbounds / routing  inbounds.json (+ template content)
firewall rules                ufw/firewalld/iptables/nft dumps
service state                 services.txt
port listeners                ports.txt
```

If backup cannot be created, stop and ask the user whether to continue without backup. Default answer: no. Record the backup path in the change plan — it is the rollback source.

## Rollback algorithm

Rollback is a mandatory part of the transaction, not advice. Trigger: critical acceptance tests fail after apply and the failure cannot be fixed by a minimal update.

```text
failure → diagnose → rollback → verify restored state
```

### Preconditions (checked during planning, before any change)

- Backup path known and verified readable.
- A written list of objects the run creates (inbounds, clients, outbound, routing rule, firewall rules).
- For every planned change, the inverse operation is known (delete created inbound; remove added outbound/routing rule from the template; close added firewall port; restore replaced template from backup).

If the rollback path for a planned change is unknown, do not make that change.

### Rollback steps

```bash
# 1. Stop traffic changes from the skill (revert Xray template to backup copy)
#    via API: fetch backup template, POST it back as xraySetting
#    (modern: /panel/api/xray/update, legacy: /panel/xray/update)

# 2. Delete objects created by this run (only the ones this run created —
#    never delete unknown pre-existing objects)
curl -s $AUTH -X POST "$PANEL/panel/api/inbounds/del/<id>"   # created inbound
# clients created via addClient: delClient with the recorded client id

# 3. Close firewall ports opened by this run
ufw delete allow <PORT>/tcp        # or the exact rule added
firewall-cmd --permanent --remove-port=<PORT>/tcp && firewall-cmd --reload

# 4. Restore full DB from backup ONLY if template/inbound rollback is insufficient
#    (stop 3x-ui → restore x-ui.db → start 3x-ui)

# 5. Restart services as needed
systemctl restart x-ui || systemctl restart 3x-ui || docker restart <container>

# 6. Clean up temporary files (keys, cookies, token files)
```

### Rollback verification

After rollback, verify the restored state:

```bash
systemctl status x-ui --no-pager || docker ps        # service healthy
ss -lntup | grep -E '<OLD_INBOUND_PORTS>'            # old listeners back
ss -lntup | grep <NEW_PORT>                          # new listener gone (if inbound removed)
# pre-existing inbounds still present (API list)
# Xray log free of new config errors
```

Then output exactly:

```text
ROLLBACK STATUS: SUCCESS
```

or

```text
ROLLBACK STATUS: PARTIAL FAILURE
```

With `PARTIAL FAILURE`, list explicitly every item that remains changed (e.g. "firewall port 22220/tcp still open; database not restored — API template rollback succeeded but new inbound `from-ru` could not be deleted: <reason>").

### Rollback with potential loss

If rollback would discard newly applied changes that already work partially (e.g. foreign side works, RU side broken), ask the user before rolling back — that case falls under the extra-confirmation list in SKILL.md.

## Fail-safe behavior

If the agent cannot confidently determine:

```text
API schema
existing routing
rollback path
```

it must not perform destructive changes. Allowed actions: read-only discovery, diagnostics, report to the user with a concrete question.
