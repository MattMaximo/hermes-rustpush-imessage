# Hermes Local iMessage (RustPush)

A thin, local-only Hermes platform plugin for iMessage using
[OpenBubbles/rustpush](https://github.com/OpenBubbles/rustpush). It does not
control Messages.app, use Accessibility automation, disable SIP, or route
messages through a third-party relay.

> **Experimental/private-protocol warning:** RustPush uses Apple's private
> iMessage protocols. Apple can change or restrict them at any time. Signing in
> may generate Apple account/device notifications. Use a dedicated Mac and an
> account posture you understand.
>
> **Contact Key Verification:** this client currently requires Apple Contact
> Key Verification to be disabled. That is a real security trade-off. The
> setup does not disable it for you.
>
> **macOS sign-in fallback:** when native AOSKit headers are unavailable, sign-in
> uses a third-party anisette-v3 metadata service and a separate local
> provisioning cache. Review [Compatibility and replay fixes](#compatibility-and-replay-fixes)
> before signing in; messages themselves are not relayed through that service.

## Architecture

```text
Hermes `imessage_cli` platform plugin
    │ persistent newline-delimited JSON over a mode-0600 Unix socket
launchd-supervised Python sidecar (`ai.hermes.rustpush-sidecar`)
    │ Python UniFFI binding
thin Corten wrapper + pinned OpenBubbles RustPush
    │ persistent APNs/IDS connections
Apple iMessage service
```

The adapter maps Hermes platform operations to RustPush and leaves Apple's
protocol implementation in RustPush. The socket server verifies the peer UID,
and no TCP listener is opened.

## Supported surface

Where supported by the pinned RustPush revision, the adapter exposes:

- inbound and outbound messages;
- persistent event-driven delivery;
- typing start/stop and read/delivery receipts;
- tapback/reaction add and remove;
- replies, edits, and undo-send events;
- inbound and outbound attachments;
- one-to-one and group conversations, including create, rename, and participant
  changes.

This is iMessage only. It does not provide SMS/RCS fallback, import the local
Messages.app database, or automate Messages.app. Some Apple client behaviors
and attachment downloads can be asynchronous. Group operations are covered by
adapter/runtime contract tests; test them with authorized participants before
relying on them operationally.

## Requirements

- macOS 12 or newer, with SIP left enabled;
- current Hermes Agent with native plugin support;
- Python 3.9+;
- Xcode Command Line Tools;
- Rust/Cargo;
- Protocol Buffers compiler (`protoc`);
- Git and network access to the pinned upstream repositories;
- an Apple ID eligible for iMessage, with access to its 2FA challenge;
- Apple Contact Key Verification disabled by the operator.

No Apple credential is accepted on the command line or through an environment
variable.

## Install

Install the plugin disabled first so Hermes does not connect before the local
runtime is ready:

```bash
hermes plugins install boobutler/hermes-rustpush-imessage --no-enable
cd ~/.hermes/plugins/imessage_cli-platform
```

Build the exact pinned Rust sources locally. Build products stay ignored and
are not part of the repository:

```bash
python3 scripts/build_runtime.py --clean
```

Exercise APNs independently before signing into IDS:

```bash
python3 scripts/standalone_preflight.py \
  --bindings dist/bindings \
  --state-dir ~/.hermes/rustpush/state
```

Sign in interactively. The script reads the Apple ID, password, and any 2FA
challenge directly from the controlling TTY, zeroes mutable buffers where
possible, and encrypts account and transport state. The macOS compatibility
fallback also uses a separate anisette provisioning cache as described below:

```bash
python3 scripts/local_account_setup.py \
  --bindings dist/bindings \
  --state-dir ~/.hermes/rustpush/state
```

Install and start the per-user launchd sidecar:

```bash
python3 scripts/install_local.py --start
python3 scripts/healthcheck.py
```

A healthy response has `"ok": true`, `"ready": true`, and a capability list.
The health check intentionally prints no account handles, tokens, or identity
material.

Enable the plugin:

```bash
hermes plugins enable imessage_cli-platform
```

Add the platform route to `~/.hermes/config.yaml` (the socket entry is optional
when using its default path):

```yaml
platforms:
  imessage_cli:
    enabled: true
    gateway_restart_notification: false
    extra:
      socket_path: ~/.hermes/rustpush/run/bridge.sock
```

Restart and inspect Hermes:

```bash
hermes gateway restart
hermes gateway status
```

Hermes's normal access and pairing policy applies. If an unknown sender gets a
pairing code, approve it explicitly:

```bash
hermes pairing approve imessage_cli CODE
```

## Compatibility and replay fixes

- On macOS 27, empty or unavailable AOSKit authentication headers fall back to
  the pinned software-signin/anisette-v3 path. Valid native headers remain the
  preferred path. Setup reports only a fixed failure category and numeric IDS
  status code, never the raw registration exception.
- Concurrent event replays are claimed before gateway admission. Native-message
  identity is deduplicated independently of transport event IDs; distinct native
  messages with identical text remain distinct. Active claims are not evicted
  by completed-cache churn, and admission is retained across receipt/ack failure.
- Outgoing message echoes are acknowledged without another agent turn. A
  self-chat reply retains a single own-handle recipient; peer replies still
  exclude the account's own handles.

Deduplication is bounded and in-memory: it is not an exactly-once guarantee
across adapter replacement or completed-cache eviction.

The macOS fallback uses SideStore's anisette-v3 service at
`https://ani.sidestore.io` for provisioning and authentication headers. That
exchanges an anisette identifier, provisioning material and header-generation
requests with that third party; iMessage content is not relayed through it, and
this provider does not receive the Apple ID, password or 2FA code. Its separate
`rustpush-anisette` cache under the user's platform data directory is not part
of the Keychain-encrypted account-state envelope. Review that dependency and
local cache before signing in; this is not an entirely offline authentication
path.

## Security model

- directories containing runtime/state use mode `0700`;
- encrypted state files and the Unix socket use mode `0600`;
- a random state-encryption key is stored in the login Keychain under service
  `ai.hermes.rustpush`, not beside ciphertext;
- Apple credentials and 2FA codes are read from `/dev/tty`, never argv/env;
- source patches remove upstream diagnostics that could expose private values;
- sidecar errors returned to Hermes are stable error codes rather than raw
  exception text;
- build inputs are pinned in `pins.env` and the compiled library is locally
  codesigned after inspection;
- the launch agent runs in the logged-in user's domain, without root.

Do not upload `~/.hermes/rustpush`, a built `dist/` directory, logs from a
modified verbose build, or Apple account diagnostics to an issue.

## Verification

Run the source tests against a Hermes checkout:

```bash
HERMES_AGENT_ROOT=/path/to/hermes-agent \
  python3 -m unittest discover -s tests -v
hermes plugins doctor . --ci
```

Then perform an authorized two-client pilot for send/receive, typing, receipts,
reaction add/remove, reply, edit, undo-send, attachment transfer, and group
operations. Measure transport latency from event timestamps rather than from
the time a human starts a test.

The original deployment of this adapter was verified with SIP enabled and
observed inbound transport latency of 44.5–120 ms (107 ms median) on its local
pilot. These measurements are not a service-level guarantee.

## Update

Review upstream changes and licenses before changing either pin. Rebuild from a
clean tree after updating `pins.env`:

```bash
python3 scripts/build_runtime.py --clean
python3 scripts/install_local.py --start
python3 scripts/healthcheck.py
```

## Uninstall

Disable the Hermes route first, then remove the sidecar/runtime:

```bash
hermes plugins disable imessage_cli-platform
hermes gateway restart
cd ~/.hermes/plugins/imessage_cli-platform
python3 scripts/uninstall_local.py
hermes plugins remove imessage_cli-platform
```

The default uninstall preserves encrypted account/device identity state so an
accidental uninstall is reversible. To irreversibly remove that state and the
matching Keychain key, use `python3 scripts/uninstall_local.py --purge-state`
before removing the plugin. Neither mode reads or deletes Messages.app chat
history or attachments.

## Licenses

Original adapter, sidecar, installer, and test code is MIT licensed. The build
patches modify upstream projects under their own licenses:

- Corten wrapper: MPL-2.0;
- OpenBubbles RustPush: SSPL-1.0.

See `THIRD_PARTY_NOTICES.md` and `LICENSES/`. This repository distributes source
and patches, not a prebuilt RustPush runtime.
