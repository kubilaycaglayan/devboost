#!/usr/bin/env python3
"""Build, install, launch, and report the DevBoost iOS app."""

import argparse
import importlib.util
import json
import os
import subprocess
import sys
import tempfile


REPO_DIR = os.path.dirname(os.path.dirname(os.path.realpath(__file__)))
HELPER_PATH = os.path.join(os.path.dirname(os.path.realpath(__file__)), "send_state_to_phone.py")
HELPER_SPEC = importlib.util.spec_from_file_location("devboost_state_notifier", HELPER_PATH)
HELPER = importlib.util.module_from_spec(HELPER_SPEC)
HELPER_SPEC.loader.exec_module(HELPER)


class MobileTestError(RuntimeError):
    """Raised when device discovery, build, install, or notification fails."""


def _env(name, default=""):
    return os.environ.get(name, default).strip()


def _run(command, label):
    print(f"{label}…")
    result = subprocess.run(command)
    if result.returncode != 0:
        raise MobileTestError(f"{label} failed with exit code {result.returncode}")


def discover_device():
    """Return paired physical-device IDs for xcodebuild and devicectl."""
    name_filter = _env("DEVBOOST_IOS_DEVICE_NAME")
    with tempfile.TemporaryDirectory(prefix="devboost-devices-") as directory:
        output = os.path.join(directory, "devices.json")
        result = subprocess.run(
            ["xcrun", "devicectl", "list", "devices", "--json-output", output],
            capture_output=True, text=True,
        )
        if result.returncode != 0:
            raise MobileTestError(result.stderr.strip() or "Could not list paired iOS devices.")
        try:
            with open(output, "r", encoding="utf-8") as handle:
                devices = json.load(handle).get("result", {}).get("devices", [])
        except (OSError, ValueError) as exc:
            raise MobileTestError(f"Could not read device list: {exc}") from exc
    candidates = [
        device for device in devices
        if device.get("hardwareProperties", {}).get("platform") == "iOS"
        and device.get("hardwareProperties", {}).get("reality") == "physical"
        and device.get("connectionProperties", {}).get("pairingState") == "paired"
    ]
    if name_filter:
        candidates = [device for device in candidates if device.get("deviceProperties", {}).get("name") == name_filter]
    if not candidates:
        requested = f" named {name_filter!r}" if name_filter else ""
        raise MobileTestError(f"No paired physical iPhone was found{requested}.")
    device = candidates[0]
    hardware = device.get("hardwareProperties", {})
    properties = device.get("deviceProperties", {})
    return {
        "name": properties.get("name", "iPhone"),
        "xcode_id": _env("DEVBOOST_IOS_XCODE_DEVICE_ID", hardware.get("udid", "")),
        "core_id": _env("DEVBOOST_IOS_CORE_DEVICE_ID", device.get("identifier", "")),
    }


def build_install_launch(device):
    """Build, install, and launch the app on one physical iPhone."""
    if not device["xcode_id"] or not device["core_id"]:
        raise MobileTestError("The selected iPhone is missing a usable device identifier.")
    derived_data = os.path.expanduser("~/Library/Developer/Xcode/DerivedData/DevBoost-mobile")
    project = os.path.join(REPO_DIR, "ios", "DevBoost", "DevBoost.xcodeproj")
    team = _env("IOS_DEVELOPMENT_TEAM")
    if not team:
        raise MobileTestError("Set IOS_DEVELOPMENT_TEAM in your user-owned .env before building for a phone.")
    command = [
        "xcodebuild", "-project", project, "-scheme", "DevBoost",
        "-destination", f"platform=iOS,id={device['xcode_id']}",
        "-derivedDataPath", derived_data, "-allowProvisioningUpdates",
        f"DEVELOPMENT_TEAM={team}",
        "build",
    ]
    _run(command, f"Building DevBoost for {device['name']}")
    app = os.path.join(derived_data, "Build", "Products", "Debug-iphoneos", "DevBoost.app")
    if not os.path.isdir(app):
        raise MobileTestError(f"Build succeeded but the app was not found at {app}")
    _run([
        "xcrun", "devicectl", "device", "install", "app", "--device", device["core_id"], app,
    ], "Installing DevBoost")
    _run([
        "xcrun", "devicectl", "device", "process", "launch", "--device", device["core_id"],
        "com.personal.devboost",
    ], "Launching DevBoost")


def send_state_notification():
    """Send the existing redacted state summary after the app is launched."""
    HELPER.load_env()
    try:
        return HELPER.send_ntfy(
            HELPER._redacted_summary(HELPER.fetch_state(
                HELPER._env("DEVBOOST_DASHBOARD_URL", HELPER.DEFAULT_DASHBOARD_URL),
            )),
            HELPER._env("NTFY_SERVER", HELPER.DEFAULT_NTFY_SERVER),
            HELPER._env("NTFY_TOPIC"),
            HELPER._env("NTFY_TITLE", "DevBoost"),
            HELPER._env("NTFY_PRIORITY"),
            HELPER._env("NTFY_TAGS"),
        )
    except HELPER.StateSendError as exc:
        raise MobileTestError(str(exc)) from exc


def main(argv=None):
    HELPER.load_env()
    parser = argparse.ArgumentParser(description="Build, install, launch, and report DevBoost on an iPhone.")
    parser.add_argument("--no-build", action="store_true", help="Only send the state notification.")
    parser.add_argument("--no-notification", action="store_true", help="Build/install/launch without ntfy.")
    args = parser.parse_args(argv)
    try:
        if not args.no_build:
            build_install_launch(discover_device())
        if not args.no_notification:
            if not _env("NTFY_TOPIC"):
                raise MobileTestError("Set NTFY_TOPIC in your environment or DevBoost .env first.")
            send_state_notification()
    except MobileTestError as exc:
        print(str(exc), file=sys.stderr)
        return 1
    print("Mobile test workflow completed.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
