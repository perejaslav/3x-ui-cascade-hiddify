---
name: 3x-ui-cascade-hiddify
description: Use when configuring two 3X-UI/Xray servers as a cascade where Hiddify on Windows connects to a Russian entry server, which forwards traffic to a foreign exit server.
version: 1.2.0
author: Hermes Agent
license: MIT
platforms: [linux, windows]
metadata:
  hermes:
    tags: [3x-ui, xray, hiddify, cascade, vpn, proxy, devops]
    related_skills: [remote-server-management, vps-administration, hermes-network-diagnostics]
---

# 3X-UI Cascade for Hiddify

## Overview

Use this skill to configure or diagnose a two-server 3X-UI/Xray cascade. Keep this file class-level and compact; session-specific implementation notes belong in `references/`.

Support files:
- `references/cascade-implementation-notes.md` — compact implementation notes, client-role split, protocol cautions, rollback intent.

For a compact implementation summary, see `references/cascade-implementation-notes.md`.

```text
Windows 11 + Hiddify → Russian 3X-UI server → foreign 3X-UI server → internet
```

The Russian server is the user-facing entry point. The foreign server is the final internet exit point. The Hiddify profile must point to the Russian server only; routing on the Russian server forwards client traffic to the foreign server via Xray outbound/routing.

Do not treat this as two independent proxy servers. The required shape is:

```text
RU inbound for Hiddify → RU outbound to foreign server → foreign inbound → foreign direct/freedom outbound
```

## Client Roles and Routing Identity

The cascade has two different client identities, and the skill must keep them separate.

### Foreign service client

This client is created on the foreign server and is used only by the Russian server.

Rules:

- it belongs to the foreign inbound for `RU → foreign`;
- its UUID/password/Reality/TLS parameters are consumed by the RU outbound;
- it is not the user-facing Hiddify profile;
- do not export it to the user as the main import link;
- in reports, describe it as a service client for the RU → foreign hop.

### RU Hiddify client

This client is created on the Russian server and is the only profile the user imports into Hiddify on Windows 11.

Rules:

- it belongs to the Hiddify-facing inbound on the RU server;
- its host/address must be the Russian server only;
- after connection, the external IP must resolve to the foreign server;
- if the generated Hiddify link points to the foreign server as the primary address, stop and report a configuration error;
- never present the foreign service client as the main Hiddify profile unless the user explicitly asks for that separate service object.

Control rule:

```text
The Hiddify import link must point to the Russian entry server only.
The foreign server client credentials are service credentials for RU outbound only.
```

If the user explicitly asks for a direct foreign-only profile, treat it as a separate bypass profile and keep it clearly outside the cascade's main Hiddify import path.

## When to Use

Use when the user asks to:

- set up a two-hop/cascade proxy with 3X-UI;
- make Russian server accept Hiddify traffic and foreign server be the exit IP;
- configure 3X-UI panel-to-panel / outbound tunneling;
- create a Hiddify-compatible client profile for Windows 11;
- verify that traffic exits through the foreign server;
- diagnose a broken RU → foreign 3X-UI cascade.

Do not use for:

- a single-server proxy/VPN setup;
- Tor/I2P/anonymity network setup;
- requests involving illegal activity;
- promising guaranteed anonymity, censorship bypass, or unblockability.

## Docker Testing (no real servers)

When no real VPS servers are available, the cascade can be simulated with two Docker containers on one machine. See `references/cascade-implementation-notes.md` → "Docker testing approach" for commands and validation scope.

Key constraints:
- 3x-ui API has CSRF protection — get token from `/csrf-token` first, login with `username`/`password` fields (not `user`/`passwd`).
- Never edit `config.json` directly — 3x-ui regenerates it from SQLite on restart.
- Outbound connections and routing rules are NOT in the database — they live in the Xray template. Use `/panel/xray/update` with form field `xraySetting` (see "3X-UI API Pitfalls" section).
- Default metrics listener (`127.0.0.1:11111`) may conflict with inbound ports — check and change via `metrics.listen` in the template before adding inbounds on 11111.

## First Response / Confirmation

Before configuring anything, confirm the task and stop for the user's approval:

> Правильно ли я понял, что нужно настроить два сервера с 3X-UI в каскадную схему, где российский сервер принимает трафик от Hiddify на Windows 11, а зарубежный сервер является конечной точкой выхода в интернет?

Do not install, restart, reconfigure, or open ports until the user has provided the required access details and explicitly approved starting work.

## Data to Request Before Starting

Ask for both servers separately.

### Russian entry server

- IP address or domain.
- SSH port.
- SSH username.
- SSH auth method: key, password, or other.
- Whether the user has root or sudo.
- Whether 3X-UI is already installed.
- If installed: panel URL/port and whether panel access works.
- Which port may be opened for the Hiddify client.
- Firewall type: ufw, firewalld, iptables/nftables, provider firewall.
- Provider firewall / security group state.
- Whether a domain is available.
- Whether TLS/Reality/certificates are required.
- Existing inbounds/outbounds that must not be touched.

### Foreign exit server

- IP address or domain.
- Country/location.
- SSH port.
- SSH username.
- SSH auth method.
- Whether the user has root or sudo.
- Whether 3X-UI is already installed.
- If installed: panel URL/port and whether panel access works.
- Which port may be opened for RU → foreign connection.
- Firewall type and provider firewall state.
- Whether a domain is available.
- Whether TLS/Reality/certificates are required.
- Existing inbounds/outbounds that must not be touched.

### Hiddify/client choices

- Desired protocol: VLESS, VLESS+Reality, Trojan, Shadowsocks, VMess, Hysteria2, or other.
- If the user does not choose a protocol, propose this default and ask for confirmation before applying it:
  - Hiddify → RU: VLESS + Reality.
  - RU → foreign: VLESS or Trojan, depending on what is simpler and more reliable for the two 3X-UI panels.
- Whether import link/subscription or manual profile is preferred.
- Whether domain/TLS is required or IP-only is acceptable.
- Whether traffic should look like HTTPS.
- Number of client profiles needed.

## SSH Credential Safety Rules

- Default assumption for this user: SSH access is performed with an SSH key already configured on the current Windows computer.
- Ask for host, SSH port, username, and which local SSH key/profile should be used if multiple keys exist.
- Do not ask for passwords, private keys, tokens, UUIDs, subscription links, or client configs in open text if a safer method is available.
- Prefer the already configured local SSH key. If that is not available, prefer a temporary SSH public key, temporary sudo user, or secure out-of-band transfer.
- Never write secrets to reports, logs, final answers, or persistent memory.
- Do not put secrets directly into shell commands where they may enter history.
- If a private key must be stored temporarily, use a temporary file, `chmod 600`, and delete it after work.
- Never print private keys, passwords, tokens, subscription links, or full client configs unless explicitly necessary and approved.
- Do not disable firewall globally. Open only required ports.
- Do not remove existing services or 3X-UI objects without explicit permission.
- Do not overwrite full Xray configs without backup.

## Preflight Checks Before Any Change

Run this on both servers before any configuration changes:

```bash
whoami
hostname
uname -a
cat /etc/os-release
id
date
timedatectl status || true
df -h
free -h
ps -p 1 -o comm=
ss -tulpen
```

Check required tools:

```bash
command -v curl || true
command -v jq || true
command -v ss || true
command -v systemctl || true
command -v ufw || true
command -v firewall-cmd || true
command -v iptables || true
command -v nft || true
command -v nc || true
command -v openssl || true
```

If the user provided a domain, verify name resolution:

```bash
getent hosts <DOMAIN> || true
dig <DOMAIN> || true
nslookup <DOMAIN> || true
```

Time sync matters because TLS and Reality can fail when server clocks are wrong. If time is broken, fix or ask the user before continuing.

## Backup Before Changes

Before modifying 3X-UI, Xray, routing, or firewall state, create a dated backup directory and store system state there.

Minimum backup scope:

- OS and host identity;
- current listeners and service state;
- firewall state;
- current 3X-UI/Xray configuration if the path is known and readable;
- current inbound/outbound/routing objects if they can be exported safely;
- if 3X-UI panel/API can export existing objects, save those exports into the backup directory;
- never dump secrets into chat or the final report.

Example:

```bash
BACKUP_DIR="$HOME/3x-ui-cascade-backup-$(date +%Y%m%d-%H%M%S)"
mkdir -p "$BACKUP_DIR"

uname -a > "$BACKUP_DIR/uname.txt"
cat /etc/os-release > "$BACKUP_DIR/os-release.txt" 2>/dev/null || true
whoami > "$BACKUP_DIR/whoami.txt"
hostname > "$BACKUP_DIR/hostname.txt"
id > "$BACKUP_DIR/id.txt"
date > "$BACKUP_DIR/date.txt"
timedatectl status > "$BACKUP_DIR/timedatectl.txt" 2>/dev/null || true
ss -tulpen > "$BACKUP_DIR/ports.txt" 2>/dev/null || true
systemctl list-units --type=service > "$BACKUP_DIR/services.txt" 2>/dev/null || true
ufw status verbose > "$BACKUP_DIR/ufw.txt" 2>/dev/null || true
firewall-cmd --list-all > "$BACKUP_DIR/firewalld.txt" 2>/dev/null || true
iptables -S > "$BACKUP_DIR/iptables.txt" 2>/dev/null || true
nft list ruleset > "$BACKUP_DIR/nft.txt" 2>/dev/null || true
```

If backup cannot be created, stop and ask the user whether to continue without backup. Default answer is no.

Optional discovery if the 3X-UI/Xray path is unknown:

```bash
find /etc /usr/local /opt -maxdepth 4 \( -iname '*x-ui*' -o -iname '*xray*' \) 2>/dev/null | head -n 50
```

## Rollback Plan

Before making changes, the agent must be able to explain the rollback path.

Rollback must include:

- restoring the previous 3X-UI/Xray configuration from backup if one exists;
- removing any inbound/outbound/routing objects created for the cascade;
- closing any newly opened firewall ports;
- restarting affected services;
- confirming that pre-existing services are still healthy;
- deleting any temporary SSH user or temporary key if one was created.

If rollback path is unknown, do not proceed.

## Operating Algorithm

### 1. Verify SSH access

For each server:

```bash
ssh -p <SSH_PORT> <USER>@<HOST> 'whoami && hostname && uname -a'
```

Check sudo:

```bash
sudo -n true && echo sudo-ok || echo sudo-needs-password
```

Check OS and identity:

```bash
cat /etc/os-release
id
uname -a
```

After SSH is confirmed on a server, run the Preflight Checks Before Any Change block on that same server before making any other modification.

### 2. Check system readiness

```bash
df -h
free -h
date
ps -p 1 -o comm=
ss -tulpen
```

If the same host facts were already captured in Preflight Checks Before Any Change, reuse those results unless the system state changed.

Check firewall:

```bash
command -v ufw && ufw status verbose
command -v firewall-cmd && firewall-cmd --list-all
command -v iptables && iptables -S
command -v nft && nft list ruleset
```

### 3. Check 3X-UI/Xray

```bash
systemctl status x-ui --no-pager || true
systemctl status 3x-ui --no-pager || true
ps aux | grep -Ei 'x-ui|3x-ui|xray' | grep -v grep || true
command -v x-ui || true
command -v 3x-ui || true
command -v xray || true
x-ui version || true
3x-ui version || true
xray version || true
ss -tulpen
```

Support two scenarios:

1. **3X-UI is already installed on both servers.** Prefer this path when possible: verify services, panel access, existing inbounds/outbounds, and only add the minimum required inbound/outbound/routing objects. Do not reinstall or overwrite existing configuration.
2. **3X-UI is not installed on one or both servers.** Tell the user exactly where it is missing and ask for explicit confirmation before installing from scratch on the missing server(s).

If 3X-UI is missing, use only the official 3X-UI install source. Show the install command before running it.

### 4. Choose protocol layout

The Hiddify-facing protocol must be supported by Hiddify and 3X-UI. The server-to-server protocol must be supported by 3X-UI/Xray outbound on RU and inbound on foreign.

Common layout:

```text
Hiddify → RU: VLESS+Reality, Trojan TLS, Shadowsocks, VMess, or Hysteria2
RU → foreign: VLESS, Trojan, Shadowsocks, VMess, or Reality
foreign → internet: freedom/direct
```

If the user did not choose a protocol, recommend this default and wait for confirmation:

```text
Hiddify → RU: VLESS + Reality
RU → foreign: VLESS or Trojan
foreign → internet: freedom/direct
```

Use VLESS/Trojan for RU → foreign based on actual 3X-UI support, existing panel configuration, need for TLS/Reality, and the user's domain/certificate situation. Do not silently choose a different protocol without saying why.

Reality is a VLESS-family option. Do not pair Reality with non-VLESS protocols.

### 5. Configure foreign server as exit

Create an inbound on the foreign 3X-UI server for the Russian server to connect to. Inside that inbound, create a service client dedicated to the RU → foreign hop.

Recommended names/tags:

- inbound tag: `from-ru`;
- service client name: `ru-to-foreign-client`.

Required properties:

- listen on appropriate external interface, normally `0.0.0.0`;
- port: `<FOREIGN_INBOUND_PORT>`;
- protocol and credentials matching RU outbound;
- TLS/Reality parameters if used;
- outbound/default route to `freedom`/direct internet;
- do not give this service client to the user as the Hiddify import profile.

Open only the required port.

For TCP-based protocols, open TCP only. For Hysteria2/TUIC/QUIC-based transports, open UDP as required. Do not open both TCP and UDP unless the selected protocol explicitly needs both.

ufw:

```bash
ufw allow <FOREIGN_INBOUND_PORT>/tcp
ufw status verbose
```

If the selected protocol requires UDP, add the matching UDP rule too:

```bash
ufw allow <FOREIGN_INBOUND_PORT>/udp
ufw status verbose
```

firewalld:

```bash
firewall-cmd --permanent --add-port=<FOREIGN_INBOUND_PORT>/tcp
firewall-cmd --reload
firewall-cmd --list-all
```

If the selected protocol requires UDP, add the matching UDP rule too:

```bash
firewall-cmd --permanent --add-port=<FOREIGN_INBOUND_PORT>/udp
firewall-cmd --reload
firewall-cmd --list-all
```

Verify listening:

```bash
ss -tulpen | grep <FOREIGN_INBOUND_PORT>
```

From the Russian server, verify reachability:

```bash
nc -vz <FOREIGN_HOST> <FOREIGN_INBOUND_PORT>
```

Fallback without `nc`:

```bash
timeout 5 bash -c '</dev/tcp/<FOREIGN_HOST>/<FOREIGN_INBOUND_PORT>' && echo open || echo closed
```

### 6. Configure Russian server as entry

Create Hiddify-facing inbound on RU server and create the user-facing client there.

Recommended names/tags:

- inbound tag: `hiddify-in`;
- user client name: `windows-hiddify-client`.

Required properties:

- listen: normally `0.0.0.0`;
- port: `<RU_CLIENT_PORT>`;
- protocol chosen for Hiddify;
- UUID/password/user credentials;
- transport/TLS/Reality settings as chosen;
- this is the only client that should be exported or imported into Hiddify.

Create RU outbound to the foreign inbound.

Open only the Hiddify client port.

For TCP-based protocols, open TCP only. For Hysteria2/TUIC/QUIC-based transports, open UDP as required. Do not open both TCP and UDP unless the selected protocol explicitly needs both.

```bash
ufw allow <RU_CLIENT_PORT>/tcp
ufw status verbose
```

If the selected protocol requires UDP, add the matching UDP rule too:

```bash
ufw allow <RU_CLIENT_PORT>/udp
ufw status verbose
```

or:

```bash
firewall-cmd --permanent --add-port=<RU_CLIENT_PORT>/tcp
firewall-cmd --reload
firewall-cmd --list-all
```

If the selected protocol requires UDP, add the matching UDP rule too:

```bash
firewall-cmd --permanent --add-port=<RU_CLIENT_PORT>/udp
firewall-cmd --reload
firewall-cmd --list-all
```

Verify listening:

```bash
ss -tulpen | grep <RU_CLIENT_PORT>
```

### 7. Configure RU routing

In 3X-UI/Xray on the Russian server, prefer the safest available control path:

1. 3X-UI panel/API, if credentials and access are available.
2. Existing 3X-UI CLI/menu tools, if they support the required operation.
3. Direct Xray/3X-UI config edits only when panel/API is unavailable or insufficient, and only after creating a backup.

If 3X-UI manages Xray configuration, do not manually rewrite the whole Xray JSON unless there is no safer option and the user approved that method.

Required routing state:

- tag the Hiddify-facing inbound, e.g. `hiddify-in`;
- tag the outbound to foreign, e.g. `to-foreign`;
- add routing rule mapping `inboundTag: hiddify-in` to `outboundTag: to-foreign`;
- ensure client traffic does not fall through to RU direct/freedom outbound.

Logical structure:

```json
{
  "routing": {
    "rules": [
      {
        "type": "field",
        "inboundTag": ["hiddify-in"],
        "outboundTag": "to-foreign"
      }
    ]
  }
}
```

This is a logical example, not a copy-paste full config. Adapt it to 3X-UI's UI/API/config shape and selected protocols.

Restart only if required:

```bash
systemctl restart x-ui || systemctl restart 3x-ui
systemctl status x-ui --no-pager || systemctl status 3x-ui --no-pager
```

Check logs:

```bash
journalctl -u x-ui -n 100 --no-pager || true
journalctl -u 3x-ui -n 100 --no-pager || true
```

### 8. Create Hiddify profile

The Hiddify profile must point to the Russian server, not the foreign server.

Before giving the profile to the user, inspect the primary address/host. If the generated Hiddify link contains the foreign server host or IP as the primary target, stop and report a configuration error.

Include only the parameters required by the chosen protocol:

- RU address/domain;
- RU client port;
- protocol;
- UUID/password;
- transport;
- TLS/Reality settings;
- SNI/server name if needed;
- Reality public key/short ID/fingerprint/flow if needed.

Do not expose full subscription links or secrets in final answer unless the user explicitly asks and understands the risk.
By default, redact UUIDs, passwords, and similar fields when a concise report is enough.

### 9. Verify route

Expected result:

```text
Hiddify external IP == foreign server IP/location
```

On foreign server:

```bash
curl -4 ifconfig.me
curl -4 ipinfo.io/ip
```

## Client-Side Verification on Windows 11 + Hiddify

On user client via Hiddify, ask the user to verify manually on Windows 11:

1. Disable other VPN/proxy clients temporarily to avoid a false result.
2. Open Hiddify.
3. Import or manually add the profile that points to the Russian server.
4. Connect to the profile.
5. Open:

```text
https://ifconfig.me
https://ipinfo.io/ip
```

6. Confirm that the shown external IP/location belongs to the foreign server, not the Russian server and not the user's ISP.

On foreign server, verify connections from RU:

```bash
ss -tnp | grep <FOREIGN_INBOUND_PORT> || true
```

On RU server, checking `curl ifconfig.me` only shows the RU server's own direct IP and does not prove client routing. The decisive test is the IP observed from the Hiddify client.

## Final Report Format

Use a short safe report:

```text
Готово.

Схема:
Windows 11 + Hiddify → российский сервер → зарубежный сервер → интернет

Что настроено:
- Российский сервер: входная точка для Hiddify.
- Зарубежный сервер: конечная точка выхода.
- На российском сервере трафик клиентского inbound направлен в outbound на зарубежный сервер.
- На зарубежном сервере трафик выпускается через direct/freedom.
- Служебный клиент на зарубежном сервере создан только для RU → foreign и не выдан пользователю.

Протоколы:
- Hiddify → RU: <protocol>
- RU → foreign: <protocol>

Открытые порты:
- RU: <port>/tcp или udp
- Foreign: <port>/tcp или udp

Проверки:
- SSH к обоим серверам: OK/failed
- 3X-UI на обоих серверах: OK/failed
- Порты слушают: OK/failed
- RU видит foreign inbound: OK/failed
- Внешний IP через Hiddify: foreign / not verified / failed

Как добавить и проверить в Hiddify на Windows 11:
1. Временно отключить другие VPN/proxy, чтобы не исказить проверку.
2. Открыть Hiddify.
3. Добавить профиль импортом или вручную.
4. Убедиться, что профиль указывает на российский сервер.
5. Подключиться.
6. Проверить IP на ifconfig.me или ipinfo.io/ip.
7. Ожидаемый результат: внешний IP соответствует зарубежному серверу.

Секретные параметры не включены в отчёт.
```

## Diagnostics

### No SSH access

Symptoms: `Permission denied`, `Connection timed out`, `No route to host`, `Host key verification failed`.

Check:

```bash
ssh -vvv -p <SSH_PORT> <USER>@<HOST>
```

Likely causes: wrong host, port, user, missing key, root login disabled, firewall/provider firewall, password auth disabled, changed host key.

Do not brute-force passwords. Ask for corrected access or a temporary public key setup.

### Port closed

Check:

```bash
ss -tulpen | grep <PORT>
ufw status verbose || true
firewall-cmd --list-all || true
iptables -S || true
nft list ruleset || true
nc -vz <HOST> <PORT>
```

Likely causes: inbound disabled, Xray not listening, local firewall, provider firewall, wrong protocol TCP/UDP, service bound to `127.0.0.1`.

Open only the required port. Do not disable the firewall.

### 3X-UI not working

Check:

```bash
systemctl status x-ui --no-pager || true
systemctl status 3x-ui --no-pager || true
journalctl -u x-ui -n 200 --no-pager || true
journalctl -u 3x-ui -n 200 --no-pager || true
ps aux | grep -Ei 'x-ui|3x-ui|xray' | grep -v grep || true
ss -tulpen
```

Likely causes: port conflict, invalid Xray config, wrong Reality/TLS fields, occupied panel port, disk full, permission issue.

Back up config before changing. Roll back the last inbound/outbound/routing change first.

### Hiddify does not connect

Check:

- RU address/domain;
- RU port;
- UUID/password;
- protocol;
- transport;
- SNI;
- ALPN;
- fingerprint;
- Reality public key;
- short ID;
- flow;
- TLS mode;
- client/server time.

Server checks:

```bash
ss -tulpen | grep <RU_CLIENT_PORT>
journalctl -u x-ui -n 100 --no-pager || true
journalctl -u 3x-ui -n 100 --no-pager || true
```

Common mistake: Hiddify points to the foreign server directly instead of the Russian entry server.

### Traffic exits through RU instead of foreign

Check RU routing:

- Hiddify inbound tag;
- outbound tag to foreign;
- routing rule order;
- whether default `freedom/direct` catches traffic;
- whether Hiddify is using the intended profile.

Check foreign server sees RU connections:

```bash
ss -tnp | grep <FOREIGN_INBOUND_PORT> || true
```

Fix the routing rule instead of rebuilding the whole setup.

### Firewall or DNS conflict

DNS checks:

```bash
getent hosts <DOMAIN>
dig <DOMAIN> || true
nslookup <DOMAIN> || true
```

TLS check:

```bash
openssl s_client -connect <DOMAIN>:443 -servername <DOMAIN>
```

Likely causes: wrong A/AAAA records, wrong SNI, certificate mismatch, provider firewall, IPv6 pointing elsewhere.

### Wrong inbound/outbound pairing

Layer check:

```text
1. Does Hiddify reach RU inbound?
2. Does RU inbound have the expected tag?
3. Does routing map that tag to foreign outbound?
4. Does RU outbound connect to foreign inbound?
5. Does foreign inbound accept RU?
6. Does foreign outbound use direct/freedom?
7. Does client-observed IP equal foreign IP?
```

Fix the first failing layer only. Do not randomly change multiple layers.

## Docker Testing (Single-Host Simulation)

When no two real servers are available, the cascade can be validated on a single machine using Docker containers.

### Architecture
```text
curl (test client) → RU container (localhost:11112 mixed) → Foreign container (foreign-cascade:22220) → internet
```

### Steps
1. Create Docker network: `docker network create cascade-net`
2. Pull image: `docker pull bigbugcc/3x-ui:latest`
3. Run foreign container: `docker run -d --name foreign-cascade --network cascade-net --hostname foreign-cascade -p 22210:2053 -p 22220:22220 bigbugcc/3x-ui:latest`
4. Run RU container: `docker run -d --name ru-cascade --network cascade-net --hostname ru-cascade -p 11110:2053 -p 11111:11111 bigbugcc/3x-ui:latest`
5. Configure via API (see pitfalls below)
6. Test: `docker exec ru-cascade sh -c 'curl -s --proxy http://127.0.0.1:11112 http://ifconfig.me'`

### Verification
- Foreign server traffic stats must show non-zero `up`/`down` after test requests
- `netstat -tn` on foreign port 22220 should show connections from RU container IP
- Hiddify VLESS link must point to RU container (localhost:11111), NOT foreign

### Limitations
- Both containers share the same external IP (same Docker host) — cannot verify exit IP difference
- SSH, ufw, firewalld checks are irrelevant in Docker
- Reality/TLS testing requires a real domain

## 3X-UI API Pitfalls

### CSRF Protection
3X-UI has CSRF protection. All API calls require:
1. GET `/csrf-token` → extract `obj` field as CSRF token
2. POST `/login` with `{"username":"admin","password":"admin"}` + `X-CSRF-Token` header
3. All subsequent requests need `X-CSRF-Token` header

### Xray Config Update
The `/panel/xray/update` endpoint expects form-encoded data with field name `xraySetting`:
```python
import requests, json
s = requests.Session()
# ... login ...
config = s.get('http://host:port/panel/xray/getDefaultJsonConfig').json()['obj']
# Modify config['outbounds'], config['routing']['rules'], etc.
s.post('http://host:port/panel/xray/update',
       data={'xraySetting': json.dumps(config)},
       headers={'X-CSRF-Token': csrf})
```
**Pitfall:** Sending as JSON body or wrong field name returns `"unexpected end of JSON input"`. Must use `data=` (form-encoded) with field name `xraySetting`.

### Two-Step Configuration Process
3X-UI separates inbound management (database) from outbound/routing (Xray template). You cannot configure everything through one API call:

**Step A — Create inbounds** via `/panel/api/inbounds/add` (JSON body). This creates the database records. Both Hiddify inbound (RU) and service inbound (foreign) are created this way.

**Step B — Configure outbound + routing** via `/panel/xray/update` (form field `xraySetting`). First fetch the current template with `GET /panel/xray/getDefaultJsonConfig`, then add your outbound and routing rule to it, then POST back. Without this step, Xray has no outbound to the foreign server and no routing rule.

**Step C — Restart** the container or service to apply the new Xray config.

### Default Credentials
Fresh 3X-UI Docker image uses `admin` / `admin`. Change after first login in production.

### Inbound Creation
```python
s.post('http://host:port/panel/api/inbounds/add',
       json={...},  # JSON body works for inbounds
       headers={'X-CSRF-Token': csrf})
```
Supported protocols: `vmess vless trojan shadowsocks wireguard hysteria http mixed tunnel`. SOCKS is NOT supported — use `mixed` for HTTP+SOCKS proxy.

### Config Persistence
3X-UI stores Xray template in SQLite `settings` table with key `xraySetting`. Inbounds are in the `inbounds` table. On restart, `config.json` is regenerated from the database. Direct file edits are overwritten on restart.

### Port Conflict
Default metrics listener uses `127.0.0.1:11111` which conflicts with custom inbounds on port 11111. Change `metrics.listen` to a different port (e.g., `127.0.0.1:62790`) before adding inbounds on 11111.

### SQLite WAL Mode
The database uses WAL mode. To read/modify from outside the container:
1. Install sqlite in container: `apk add --no-cache sqlite`
2. Checkpoint WAL: `sqlite3 /etc/x-ui/x-ui.db "PRAGMA wal_checkpoint(TRUNCATE);"`
3. Then copy: `docker cp container:/etc/x-ui/x-ui.db /tmp/db.sqlite`

## Completion Checklist

- [ ] User explicitly approved configuration work.
- [ ] SSH access uses the already configured local SSH key where possible.
- [ ] SSH access to both servers verified.
- [ ] Preflight checks completed on both servers before any change.
- [ ] Backup created before touching 3X-UI, Xray, routing, or firewall.
- [ ] Rollback path is known and can be explained.
- [ ] OS, sudo/root, firewall, ports checked.
- [ ] Provider firewall/security group checked if local port looks open but is still unreachable.
- [ ] 3X-UI checked or installed on both servers.
- [ ] 3X-UI/Xray version checked when compatibility or protocol behavior matters.
- [ ] Foreign server service client created only for RU → foreign.
- [ ] Russian server user client created for Hiddify only.
- [ ] Foreign server configured as exit.
- [ ] Russian server configured as Hiddify entry.
- [ ] RU outbound points to foreign inbound.
- [ ] RU routing sends Hiddify inbound to foreign outbound.
- [ ] Only needed ports opened.
- [ ] Hiddify profile points to RU server.
- [ ] Hiddify profile was checked to ensure the foreign host/IP is not the primary target.
- [ ] Client-side verification on Windows 11 was performed or explicitly delegated to the user.
- [ ] Client external IP verified as foreign, or user asked to verify.
- [ ] Final report contains no secrets unless explicitly approved.
- [ ] No claims of guaranteed anonymity or universal blocking resistance.
