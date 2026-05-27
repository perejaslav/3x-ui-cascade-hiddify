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
