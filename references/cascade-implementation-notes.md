# Cascade implementation notes

This reference is a compact summary for agents applying the `3x-ui-cascade-hiddify` skill (v2.0.0). Detailed procedures live in the sibling references: `api-compatibility.md` (endpoints, discovery, auth), `acceptance-tests.md` (test table, DNS/IPv6/UDP), `security-and-rollback.md` (backup, rollback), `troubleshooting.md` (symptom tables).

## Core topology

- User-facing profile: Hiddify on Windows 11 connects only to the Russian entry server.
- Russian server: Hiddify inbound `hiddify-entry`, RU outbound `to-foreign`, routing rule between them.
- Foreign server: service inbound `from-ru` for the RU → foreign hop, then direct/freedom outbound to the internet.

## Client roles

- `windows-hiddify-client`: user-facing client created on the Russian server; the only profile imported into Hiddify.
- `ru-to-foreign-client`: service client created on the foreign server and consumed only by the Russian outbound. Never given to the user as the main profile.

If existing configuration uses different names, adopt them; do not rename working objects without need.

## Mandatory safety gates

- Confirm the task and capture `approved_scope` (one approval for the whole routine scope; extra confirmation only for destructive/deletion/SSH/firewall-off/reboot/rollback-with-loss cases).
- Collect SSH access details for both servers.
- Prefer the already configured SSH key on the user's Windows computer.
- Run discovery on both servers: system, firewall (OS + provider), 3x-ui version, API capabilities.
- Run 3X-UI API discovery before the first write (modern vs legacy, auth mode, serialization mode).
- Create backups before modifying 3X-UI/Xray/routing/firewall (WAL-safe DB copy).
- Build the change plan with reuse/update/create per object; check port collisions before creating inbounds.
- Keep a rollback path available — if unknown for a planned change, do not make it.
- Do not expose secrets in the final report.

## Idempotency

- Before creating any object, search for it (inbound tag/remark/port/protocol, client email/UUID, outbound tag, routing rule tag/match).
- Not found → create; compatible → reuse; minor change → update; conflict with the architecture → stop and report, do not overwrite.
- A second run on a configured system must end with `NO CHANGES REQUIRED` (no duplicate inbound/client/outbound/rule/firewall entry, no unnecessary restart).

## Verification intent

- The Russian host must be the primary Hiddify target.
- The foreign host must be the exit IP observed from the Hiddify client.
- If the Hiddify profile points to the foreign server as the primary target, stop and report an error.

## Protocol note

- Hiddify-facing protocols are chosen by the user or proposed by default (VLESS+Reality for Hiddify → RU; VLESS for RU → foreign), then validated against the installed Xray, Hiddify, firewall, and domain/TLS situation.
- Reality must be treated as a VLESS-family option, not a generic transport for every protocol.
- For UDP-based transports, open UDP only when the selected protocol requires it.

## Live apply

- Prefer live apply after a config change; validate; restart only when live apply is unavailable/failed, the change requires it, or runtime state diverges from config. State the reason and check the backup before restarting.

## Rollback intent

- Restore the previous config from backup if needed.
- Remove only objects created by this run; never delete unknown pre-existing objects.
- Close newly opened ports.
- Restart services and verify the old state is healthy.
- Output `ROLLBACK STATUS: SUCCESS` or `ROLLBACK STATUS: PARTIAL FAILURE` (with the list of what remains changed).

## Pitfalls (learned the hard way)

### 3x-ui regenerates Xray config from DB on restart

**Never** edit `/app/bin/config.json` (or wherever xray config.json lives) directly. 3x-ui overwrites it from its SQLite database (`/etc/x-ui/x-ui.db`) on every restart. All changes must go through:
1. The 3X-UI panel UI (preferred for outbounds/routing).
2. The 3X-UI API (inbounds API + Xray template update — see `api-compatibility.md`).
3. Direct DB modification (last resort, requires copying db out since containers lack sqlite3).

### 3x-ui API requires CSRF token (session mode)

The API has CSRF protection. Correct flow:
```bash
# 1. Get CSRF token (no auth needed)
CSRF=$(curl -s -c cookies.txt http://HOST:PORT/csrf-token | jq -r '.obj')

# 2. Login with CSRF header (field names are "username" and "password", NOT "user"/"passwd")
curl -s -b cookies.txt -c cookies.txt \
  -X POST http://HOST:PORT/login \
  -H "Content-Type: application/json" \
  -H "X-CSRF-Token: $CSRF" \
  -d '{"username":"...","password":"..."}'

# 3. Use session cookie for subsequent API calls
curl -s -b cookies.txt http://HOST:PORT/panel/api/inbounds/list
```

Bearer token auth (Settings → Security → API Token) bypasses CSRF entirely — use it when available and prefer it.

### Probe endpoints before use (modern vs legacy)

Xray template endpoints moved between versions:

```text
modern: POST /panel/api/xray/                  (read CURRENT saved template)
        POST /panel/api/xray/update            (form field xraySetting)
legacy: POST /panel/xray/                      (same semantics)
        POST /panel/xray/update
```

**Do not read the current template via `GET getDefaultJsonConfig`** — despite the name, it returns the *default* template, not the saved one; a read-modify-write cycle through it silently wipes existing outbounds/routing. Probe modern first; fall back to legacy only if modern is absent (404) — never because the version "looks old". A 404/405 or schema mismatch on a known endpoint means: re-run discovery, do not assume breakage.

Handy diagnostic: `POST /panel/api/xray/testOutbound` (form fields `outbound` = JSON of the outbound, `mode` = `tcp` for dial-only or `real` for a full request) tests a VLESS outbound end-to-end without touching traffic.

### Port conflict: metrics listener vs inbound

Do not assume metrics is on `127.0.0.1:11111` — detect the metrics listener dynamically (template `metrics`/`api` blocks + `ss -lntup`). If it collides with a desired inbound port, change `metrics.listen` in the template. Xray error signature: `listen tcp 127.0.0.1:11111: bind: address already in use`.

### Outbound + routing: use API with xraySetting field

The 3x-ui API handles inbounds and the Xray template through different endpoints:

**Inbounds** — `POST /panel/api/inbounds/add` with JSON body. Creates database records.

**Outbound + routing** — Xray template update with form field `xraySetting`. Read the CURRENT template first:
```python
saved = requests.post(f'{BASE}/panel/api/xray/', headers=auth).json()
data = json.loads(saved['obj']) if isinstance(saved['obj'], str) else saved['obj']
config = data['xraySetting']            # parse again if it is a string
if isinstance(config, str): config = json.loads(config)
# Add your outbound to config['outbounds']
# Add routing rule to config['routing']['rules']
# Change metrics.listen if it conflicts
requests.post(f'{BASE}/panel/api/xray/update',
              data={'xraySetting': json.dumps(config)},
              headers=auth)
```

**Pitfalls:** Sending as JSON body (`json=...`) or wrong field name returns `"unexpected end of JSON input"`. Must use `data=` (form-encoded) with exact field name `xraySetting`. Reading via `GET getDefaultJsonConfig` gives the DEFAULT template — using it in a read-modify-write cycle wipes existing outbounds/routing.

Prefer typed nested JSON where the API accepts it (v3.x does for both inbounds and the template); fall back to string serialization only on a type/encoding error (probe once, record the mode). Client field `tgId` must be an int (0), not a string.

After changes: live apply if supported; otherwise restart the container/service or click "Перезапуск Xray".

### Inbound settings fields: typed on v3.x, strings on legacy

v3.7.0 accepts **typed nested JSON** in add/update inbound payloads and returns typed dicts in list/get responses. Legacy versions use JSON-encoded strings and may return strings. Normalize both on read (`as_dict` helper in `tests/cascade_docker_test.py`); prefer typed on write. The `tgId` client field must be an **int** (`0`) — a string fails with `cannot unmarshal string into Go struct field .0.tgId of type int64`.

## Docker testing approach

When no real servers are available, simulate the cascade with two Docker containers. Use the **official image with a pinned version** — never `bigbugcc/3x-ui` (removed) and never `latest` for acceptance tests:

```bash
THREEXUI_VERSION=v3.7.0
docker pull ghcr.io/mhsanaei/3x-ui:$THREEXUI_VERSION

docker network create cascade-net

# Foreign exit (panel 22210, inbound 22220)
docker run -d --name foreign-cascade --network cascade-net --hostname foreign-cascade \
  -p 22210:2053 -p 22220:22220 ghcr.io/mhsanaei/3x-ui:$THREEXUI_VERSION

# RU entry (panel 11110, inbound 11111)
docker run -d --name ru-cascade --network cascade-net --hostname ru-cascade \
  -p 11110:2053 -p 11111:11111 ghcr.io/mhsanaei/3x-ui:$THREEXUI_VERSION
```

Containers resolve each other by hostname within the Docker network. Outbound address on RU = `foreign-cascade`.

Automated harness: `tests/cascade_docker_test.py` (network + containers + API configuration + traffic test + idempotency re-run).

**Validates:** config correctness, Xray startup, protocol handshake, traffic flow, idempotency of a re-run.
**Does NOT validate:** real IPs, firewalls/provider ACLs, Reality/TLS with domains, Hiddify from Windows, DNS leaks, UDP over the production path.

Docker is an integration test, not production validation. See `acceptance-tests.md` for the full scope split.

## Version awareness

- Record the 3x-ui and Xray versions in the discovery summary of every run.
- The capability decision table (SKILL.md) is driven by probes; the version is a hint only.
- Tested baselines are listed in `api-compatibility.md` (3x-ui v3.7.0, Xray v26.3.27, Hiddify v4.1.1).
