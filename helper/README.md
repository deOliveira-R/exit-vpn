# exitctl

The Mac-side provider client.
- **Login:** logs in to the VPN provider. The session lives in the macOS Keychain and never goes to the exit node.
- **Keys:** makes and renews WireGuard keys and uploads them into the exit node's slots.
- **Server list:** pushes the provider's live server list.

The ExitControl app drives it with `--json`. Concepts:
[ARCHITECTURE.md](../ARCHITECTURE.md).

## Install (for the app and the launchd agent)

    uv tool install --force --reinstall --python /opt/homebrew/opt/python@3.13/bin/python3.13 ./helper

- **Homebrew Python:** proton-core loads `libssl` through ctypes. uv's standalone Python finds only the system library, which macOS refuses to load, and the process aborts.
- **`--reinstall`:** without it, uv reuses a cached build of the same version.

## Use

    exitctl proton login                    # password, then 2FA / CAPTCHA as asked
    exitctl proton whoami | list | options
    exitctl proton rotate --slot a [--netshield 2 ...]   # new key; the old certificate is deleted
    exitctl proton renew --slot a           # same key, later expiry
    exitctl proton servers [--push|--revert]
    exitctl keys                            # the exit node's slots
    exitctl manual import --slot a file.conf

`--url` names the controller (default `$EXITCTL_URL` or `http://exit-proton:8080`). `--json`
prints machine output on stdout, including errors. Shapes and exit codes:
[contracts/README.md](../contracts/README.md).

## Tests

    uv venv -p 3.13 && uv pip install -e '.[test]'
    .venv/bin/pytest -q

`test_contracts.py` runs the CLI against a fake Proton session and a fake exit node and checks
every `--json` output against `contracts/exitctl/`.

## Adding a provider

1. **The adapter:** a module (or package) in `src/exitctl/providers/` with a `Provider` subclass (`providers/base.py`). Set `name`, `title`, `capabilities` (which commands the CLI offers), and `tunnel_address` / `tunnel_dns` (the Address and DNS its configs carry; they must match the exit node's).
2. **Implement what the capabilities promise:**

   | Capability | Methods |
   |---|---|
   | `login` | `account()`, `login(Credentials)` (raise `NeedInput` for each step the user must answer), `logout()`. Keep sessions in a `SessionStore`. |
   | `create` | `levers()` and `create_key(device_name, values, peer)`. The latter returns a `NewKey` whose `KeyMeta` the controller records. |
   | `renew` | `renew_key(KeyMeta)`, and `renewal_meta(Certificate)` for keys made elsewhere. |
   | `list`, `delete` | `certificates()` and `delete_certificate(id)`. |
   | `servers` | `servers()`: rows in the controller's push schema (`providers/base.ServerRow`). |
   | `import` | Nothing to implement: `KeyService.import_conf` uploads a `.conf` as it is. |

3. **Register it** in `providers/__init__.py`.
4. **Configure the node:** set `VPN_PROVIDER` on the exit node (it refuses pushes from another provider) and `CATALOG_HOSTNAME_SUFFIX`.
5. **Test it** with a fake session, like `tests/harness.py`'s `FakeSession` for Proton (see TESTING.md).

The exit node names its keys `<node>-<slot>-<yyyymmdd>` (`keyservice.DeviceNaming`), which is
how `list` tells its certificates (and orphans) from the account's other devices.
