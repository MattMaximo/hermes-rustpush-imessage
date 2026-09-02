# Local iMessage (RustPush) setup

This plugin needs a local RustPush runtime before it can be enabled. The setup
signs directly into Apple IDS and may require Apple ID/password and 2FA input.
Credentials are read from the controlling TTY only; do not put them in a shell
command, environment variable, Hermes config, or issue report.

**Security decision:** Apple Contact Key Verification must be disabled for this
unofficial client. Decide whether that trade-off is acceptable before
continuing. Do not disable SIP; it is neither required nor supported here.

```bash
cd ~/.hermes/plugins/imessage_cli-platform
python3 scripts/build_runtime.py --clean
python3 scripts/standalone_preflight.py \
  --bindings dist/bindings \
  --state-dir ~/.hermes/rustpush/state
python3 scripts/local_account_setup.py \
  --bindings dist/bindings \
  --state-dir ~/.hermes/rustpush/state
python3 scripts/install_local.py --start
python3 scripts/healthcheck.py
hermes plugins enable imessage_cli-platform
```

Enable the route in `~/.hermes/config.yaml`:

```yaml
platforms:
  imessage_cli:
    enabled: true
    gateway_restart_notification: false
    extra:
      socket_path: ~/.hermes/rustpush/run/bridge.sock
```

Then run `hermes gateway restart`. The normal Hermes pairing policy applies;
approve an inbound pairing code with:

```bash
hermes pairing approve imessage_cli CODE
```

See `README.md` for prerequisites, architecture, verification, limitations,
and safe uninstall instructions.
