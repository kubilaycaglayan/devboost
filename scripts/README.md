# DevBoost scripts

These scripts are optional helpers for local automation. They use only the
Python standard library and keep personal configuration in environment
variables or the per-user DevBoost runtime state.

## Send the latest state to a phone

Install the [ntfy](https://ntfy.sh/) app on the phone, subscribe to a private
topic, and set the topic only in your shell (or export it from a user-owned
configuration file):

```sh
export NTFY_TOPIC='choose-your-private-topic'
python3 scripts/send_state_to_phone.py
```

Optional environment values:

```sh
export NTFY_SERVER='https://ntfy.sh'
export NTFY_TITLE='DevBoost'
export NTFY_PRIORITY='default'
export NTFY_TAGS='computer'
export DEVBOOST_DASHBOARD_URL='http://127.0.0.1:3080'
```

The standalone script reads environment variables; it does not read repo files
or contain personal settings. The message is a redacted summary. It does not send server hostnames,
filesystem paths, account names, raw configuration, or credentials.
