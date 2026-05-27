# Cascade implementation notes

This reference is a compact summary for agents applying the `3x-ui-cascade-hiddify` skill.

## Core topology

- User-facing profile: Hiddify on Windows 11 connects only to the Russian entry server.
- Russian server: Hiddify inbound, RU outbound to foreign.
- Foreign server: service inbound for the RU → foreign hop, then direct/freedom outbound to the internet.

## Client roles

- `windows-hiddify-client`: user-facing client created on the Russian server.
- `ru-to-foreign-client`: service client created on the foreign server and consumed only by the Russian outbound.

## Mandatory safety gates

- Confirm the task before changes.
- Collect SSH access details for both servers.
- Prefer the already configured SSH key on the user's Windows computer.
- Run preflight checks on both servers.
- Create backups before modifying 3X-UI/Xray/routing/firewall.
- Keep a rollback path available.
- Do not expose secrets in the final report.

## Verification intent

- The Russian host must be the primary Hiddify target.
- The foreign host must be the exit IP observed from the Hiddify client.
- If the Hiddify profile points to the foreign server as the primary target, stop and report an error.

## Protocol note

- Hiddify-facing protocols are chosen by the user or proposed by default.
- Reality must be treated as a VLESS-family option, not a generic transport for every protocol.
- For UDP-based transports, open UDP only when the selected protocol requires it.

## Rollback intent

- Restore the previous config from backup if needed.
- Remove newly created inbound/outbound/routing objects.
- Close newly opened ports.
- Restart services and verify the old state is healthy.

## Pitfalls (learned the hard way)

### 3x-ui regenerates Xray config from DB on restart

**Never** edit `/app/bin/config.json` (or wherever xray config.json lives) directly. 3x-ui overwrites it from its SQLite database (`/etc/x-ui/x-ui.db`) on every restart. All changes must go through:
1. The 3X-UI panel UI (preferred for outbounds/routing).
2. The 3X-UI API (`/panel/api/inbounds/add`, `/panel/xray/update`).
3. Direct DB modification (last resort, requires copying db out since containers lack sqlite3).

### 3x-ui API requires CSRF token

The API has CSRF protection. Correct flow:
```bash
# 1. Get CSRF token (no auth needed)
CSRF=$(curl -s -c cookies.txt http://HOST:PORT/csrf-token | jq -r '.obj')

# 2. Login with CSRF header (field names are "username" and "password", NOT "user"/"passwd")
curl -s -b cookies.txt -c cookies.txt \
  -X POST http://HOST:PORT/login \
  -H "Content-Type: application/json" \
  -H "X-CSRF-Token: $CSRF" \
  -d '{"username":"admin","password":"admin"}'

# 3. Use session cookie for subsequent API calls
curl -s -b cookies.txt http://HOST:PORT/panel/api/inbounds/list
```

Bearer token auth (from Settings → Security → API Token) bypasses CSRF entirely — use it when available.

### Port conflict: metrics listener vs inbound

The default 3x-ui config may have `metrics.listen` on `127.0.0.1:11111` which conflicts with an inbound on the same port. Xray fails with: `listen tcp 127.0.0.1:11111: bind: address already in use`. Fix via the UI: Настройки Xray → Основное → change metrics port, or use the "Расширенный шаблон" tab.

### Outbound + routing: use API with xraySetting field

The 3x-ui API handles both inbounds and the Xray template, but through different endpoints:

**Inbounds** — `POST /panel/api/inbounds/add` with JSON body. Creates database records.

**Outbound + routing** — `POST /panel/xray/update` with form field `xraySetting`. First fetch the template:
```python
config = requests.get('http://HOST:PORT/panel/xray/getDefaultJsonConfig').json()['obj']
# Add your outbound to config['outbounds']
# Add routing rule to config['routing']['rules']
# Change metrics.listen if it conflicts (e.g. 127.0.0.1:62790)
requests.post('http://HOST:PORT/panel/xray/update',
              data={'xraySetting': json.dumps(config)},
              headers={'X-CSRF-Token': csrf})
```

**Pitfall:** Sending as JSON body (`json=...`) or wrong field name returns `"unexpected end of JSON input"`. Must use `data=` (form-encoded) with exact field name `xraySetting`.

After changes: restart the container or service. Click "Перезапуск Xray" in the UI also works.

## Docker testing approach

When no real servers are available, simulate the cascade with two Docker containers:

```bash
docker network create cascade-net
docker pull bigbugcc/3x-ui:latest

# Foreign exit (panel 22210, inbound 22220)
docker run -d --name foreign-cascade --network cascade-net --hostname foreign-cascade \
  -p 22210:2053 -p 22220:22220 bigbugcc/3x-ui:latest

# RU entry (panel 11110, inbound 11111)
docker run -d --name ru-cascade --network cascade-net --hostname ru-cascade \
  -p 11110:2053 -p 11111:11111 bigbugcc/3x-ui:latest
```

Containers resolve each other by hostname within the Docker network. Outbound address on RU = `foreign-cascade`.

**Validates:** config correctness, Xray startup, protocol handshake, traffic flow.
**Does NOT validate:** real IPs, firewalls, Reality/TLS with domains, Hiddify from Windows.
