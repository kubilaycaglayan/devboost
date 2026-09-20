# DevBoost scripts

These scripts are optional helpers for local automation. They use only the
Python standard library and keep personal configuration in environment
variables or the per-user DevBoost runtime state.

## Build, install, launch, and report the mobile app

With the iPhone paired and Developer Mode enabled, one command builds the iOS
app, installs it, launches it, and sends a redacted DevBoost state summary:

```sh
python3 scripts/mobile_test.py
```

For the normal data-preserving refresh workflow, run the repository launcher:

```sh
./run-ios.sh
```

It builds the app, installs it over the existing `com.personal.devboost` app,
and launches it without uninstalling the app or sending a notification. After
the Personal Team profile expires, Xcode can issue a fresh 7-day profile during
the build. Select a specific paired phone with `DEVBOOST_IOS_DEVICE_NAME` in
the user-owned `.env`.

The equivalent shell entry point is:

```sh
./scripts/build_and_send_to_phone.sh
```

The script discovers the first available paired physical iPhone. Set
`DEVBOOST_IOS_DEVICE_NAME` in the user-owned `.env` to select a specific phone.
Use `--no-notification` to only build/install/launch, or `--no-build` to only
send the state notification.

## Send the latest state to a phone

Install the [ntfy](https://ntfy.sh/) app on the phone, subscribe to a private
topic, and set the topic only in your shell (or export it from a user-owned
configuration file):

```sh
export NTFY_TOPIC='choose-your-private-topic'
python3 scripts/mobile_test.py --no-build
```

Optional environment values:

```sh
export NTFY_SERVER='https://ntfy.sh'
export NTFY_TITLE='DevBoost'
export NTFY_PRIORITY='default'
export NTFY_TAGS='computer'
export DEVBOOST_DASHBOARD_URL='http://127.0.0.1:3080'
```

The standalone script reads environment variables and the user-owned DevBoost
`.env` locations. It does not contain personal settings. The message is a
redacted summary. It does not send server hostnames,
filesystem paths, account names, raw configuration, or credentials.

`send_state_to_phone.py` remains available for the old notification-only
workflow; new automation should use `mobile_test.py`.
