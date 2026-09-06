# DevBoost for iOS

Native iPhone companion for DevBoost: secure SSH hosts and terminal, Docker
observability, file uploads to Ubuntu over SFTP, and live Codex subscription
usage from a selected SSH host.

## Open and run

Open `DevBoost.xcodeproj` in Xcode and run the `DevBoost` scheme on an iPhone or
simulator. The local signing team is read from the ignored root `.env` as
`IOS_DEVELOPMENT_TEAM`; regenerate the project with:

```sh
./ios/DevBoost/generate-project.sh
```

The checked-in Xcode project is generated from `project.yml`.

The first connection uses SSH trust-on-first-use. Verify the fingerprint before
accepting it. Private device keys and any future provider secrets are kept in
the device-only Keychain.

## Test the mobile app

Run the unit and first-run UI suites on an installed simulator runtime:

```sh
xcodebuild test \
  -project DevBoost.xcodeproj \
  -scheme DevBoost \
  -destination 'platform=iOS Simulator,name=iPhone 17' \
  CODE_SIGNING_ALLOWED=NO
```

The `DevBoostTests` target covers persistence, Keychain behavior, command
escaping, Docker and Codex parsing, transfer paths, and local SSH preconditions.
The `DevBoostUITests` target covers a fresh install's tab navigation and empty
states. UI tests pass `--ui-test-reset-state` so they never reuse a developer's
saved hosts.

## Current mobile behavior

- File Transfer is foreground SFTP upload. It supports Files and Photos
  selection, creates missing remote folders, lists server folders, and
  remembers recent per-host destinations.
- Docker data is obtained from the selected server through authenticated SSH:
  running containers, one-shot stats, and recent logs.
- Codex usage starts the signed-in `codex app-server` on the selected SSH host
  and reads only `account/rateLimits/read`; DevBoost does not copy ChatGPT
  credentials to the phone. The Home screen refreshes the short window while
  it is visible.

Background uploads, downloads, and bidirectional folder synchronization are
intentionally outside this first mobile upload scope.
