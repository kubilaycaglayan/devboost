#!/usr/bin/env python3
"""
DevBoost • SSH Port Forward Manager & Discovery Dashboard
CLI & Web Dashboard for managing persistent (launchd) and session-based SSH port forwards.
"""

import sys
import os
import re
import json
import glob
import time
import signal
import plistlib
import posixpath
import shlex
import shutil
import subprocess
import tempfile
import urllib.parse
import uuid
from http.server import ThreadingHTTPServer, BaseHTTPRequestHandler

from docker_monitor import DockerMonitor, collect_docker_snapshot, collect_docker_logs
from remote_transport import run_ssh_command


def _code_dir():
    """Directory containing this file: repo root in dev, ~/.config/devboost when installed."""
    return os.path.dirname(os.path.realpath(__file__))


def _default_app_dir():
    """Per-user state kept separate from both source and the app bundle."""
    return os.path.expanduser("~/Library/Application Support/DevBoost")


def load_env(app_dir=None):
    """Loads key-value pairs from .env files without requiring external libraries."""
    code_dir = _code_dir()
    resolved_app = app_dir or _default_app_dir()
    candidates = []
    # Explicit override via environment always wins (set externally, before .env load).
    for _override_key in ("DEVBOOST_APP_DIR", "PORT_TRACKER_APP_DIR",
                          "DEVBOOST_CONFIG_DIR", "PORT_TRACKER_CONFIG_DIR"):
        _override_val = os.getenv(_override_key, "")
        if _override_val:
            candidates.append(os.path.join(os.path.expanduser(_override_val), ".env"))
    candidates += [
        # Canonical state. This is writable by the packaged app and does not
        # make its background work depend on access to Documents.
        os.path.join(resolved_app, ".env"),
        # Legacy fallbacks (migration period)
        os.path.join(code_dir, "app", ".env"),
        os.path.join(code_dir, ".env"),
        os.path.expanduser("~/.config/devboost/app/.env"),
        os.path.expanduser("~/.config/devboost/.env"),
        os.path.expanduser("~/.config/port-tracker/.env"),
        os.path.join(os.getcwd(), "app", ".env"),
        os.path.join(os.getcwd(), ".env"),
    ]
    seen = set()
    for env_path in candidates:
        if env_path in seen:
            continue
        seen.add(env_path)
        if os.path.exists(env_path):
            try:
                with open(env_path, "r", encoding="utf-8") as f:
                    for line in f:
                        line = line.strip()
                        if not line or line.startswith("#") or "=" not in line:
                            continue
                        k, v = line.split("=", 1)
                        k = k.strip()
                        v = v.strip().strip("'\"")
                        if k not in os.environ:
                            os.environ[k] = v
            except Exception:
                pass


# Initialize environment configuration
load_env()


def _env(new_key, legacy_key, default):
    """Reads DevBoost-prefixed env var with fallback to legacy PORT_TRACKER_ name."""
    return os.getenv(new_key, os.getenv(legacy_key, default))


SSH_HOST = _env("DEVBOOST_SSH_HOST", "PORT_TRACKER_SSH_HOST", "remote-server")
SERVER_NAME = _env("DEVBOOST_SERVER_NAME", "PORT_TRACKER_SERVER_NAME", "Remote Server")
SERVER_IP = _env("DEVBOOST_SERVER_IP", "PORT_TRACKER_SERVER_IP", "")
DEFAULT_DASHBOARD_PORT = int(_env("DEVBOOST_DASHBOARD_PORT", "PORT_TRACKER_DASHBOARD_PORT", "3080"))
AGENT_DOMAIN = _env("DEVBOOST_AGENT_DOMAIN", "PORT_TRACKER_AGENT_DOMAIN", "com.user.devboost")
AGENT_PREFIX = _env("DEVBOOST_AGENT_PREFIX", "PORT_TRACKER_AGENT_PREFIX", "com.user.devboost-forward")
# Folder-sync LaunchAgent prefix, derived from the tunnel prefix so custom
# AGENT_DOMAIN / AGENT_PREFIX installs stay namespaced (default ...-sync).
# Explicit override via DEVBOOST_SYNC_PREFIX (legacy PORT_TRACKER_SYNC_PREFIX).
_SYNC_PREFIX_OVERRIDE = _env("DEVBOOST_SYNC_PREFIX", "PORT_TRACKER_SYNC_PREFIX", "")
if _SYNC_PREFIX_OVERRIDE:
    SYNC_AGENT_PREFIX = _SYNC_PREFIX_OVERRIDE
elif AGENT_PREFIX.endswith("-forward"):
    SYNC_AGENT_PREFIX = AGENT_PREFIX[: -len("-forward")] + "-sync"
else:
    SYNC_AGENT_PREFIX = AGENT_PREFIX + "-sync"
# Folder-sync semantics (see README + dashboard modal for user-facing docs):
# - direction: "two-way" (merge, newer wins, deletions never propagate),
#   "push" (local -> remote), "pull" (remote -> local).
# - mirror (bool): one-way only — when True the destination becomes an exact
#   copy via rsync --delete. Ignored for two-way (always a safe merge), because
#   delete-propagation in both directions via plain rsync is order-dependent
#   and lossy. The dashboard disables the checkbox for two-way.
# - always (bool): False = "Once" (one-time sync, runs now + on-demand, no
#   background agent; Session equivalent). True = "Auto" (persistent LaunchAgent
#   with WatchPaths for instant local triggers + StartInterval polling for
#   remote changes; Always equivalent).
SYNC_DIRECTIONS = ("two-way", "push", "pull")
SYNC_DEFAULT_DIRECTION = "two-way"
SYNC_DEFAULT_INTERVAL = 15
SYNC_MIN_INTERVAL = 5
SYNC_MAX_INTERVAL = 600
# CODE_DIR holds the executable (repo root in development, app Resources when packaged).
CODE_DIR = _code_dir()
# APP_DIR holds per-user state (.env, config.json), separate from source code
# and the read-only app bundle. DEVBOOST_APP_DIR / DEVBOOST_CONFIG_DIR (legacy)
# override it explicitly (tests and custom deployments).
_APP_DIR_OVERRIDE = _env("DEVBOOST_APP_DIR", "PORT_TRACKER_APP_DIR", "")
_CONFIG_DIR_OVERRIDE = _env("DEVBOOST_CONFIG_DIR", "PORT_TRACKER_CONFIG_DIR", "")
if _APP_DIR_OVERRIDE:
    APP_DIR = os.path.expanduser(_APP_DIR_OVERRIDE)
elif _CONFIG_DIR_OVERRIDE:
    APP_DIR = os.path.expanduser(_CONFIG_DIR_OVERRIDE)
else:
    APP_DIR = _default_app_dir()
# Backward compat: CONFIG_DIR aliases the state dir (old code/tests patch it).
CONFIG_DIR = APP_DIR
CONFIG_FILE = os.path.join(APP_DIR, "config.json")
# Legacy state locations checked once for one-time migration into APP_DIR.
LEGACY_CONFIG_FILES = [
    os.path.expanduser("~/.config/devboost/app/config.json"),
    os.path.join(CODE_DIR, "app", "config.json"),
    os.path.join(CODE_DIR, "config.json"),
    os.path.expanduser("~/.config/devboost/config.json"),
    os.path.join(os.getcwd(), "config.json"),
]
BIN_DIR = os.path.join(CODE_DIR, "bin")
DASHBOARD_WRAPPER_NAME = "DevBoost-dashboard"
TUNNEL_WRAPPER_NAME = "DevBoost-tunnel"
LAUNCH_AGENTS_DIR = os.path.expanduser("~/Library/LaunchAgents")
LOG_DIR = os.path.expanduser(_env("DEVBOOST_LOG_DIR", "PORT_TRACKER_LOG_DIR",
                                 os.path.join(APP_DIR, "logs")))
SSH_CONFIG_PATH = os.path.expanduser("~/.ssh/config")

# Favicon (assets/favicon.png) served at /favicon.png + /favicon.ico.
# _FAVICON_FALLBACK_B64 is a 64x64 embedded copy so the icon works even when
# the installed copy under CONFIG_DIR/assets/ is missing (e.g. old installs).
_FAVICON_FALLBACK_B64 = (
    "iVBORw0KGgoAAAANSUhEUgAAAEAAAABACAYAAACqaXHeAAAAAXNSR0IArs4c6QAAAERlWElmTU0AKgAAAAgAAYdpAAQAAAABAAAAGgAAAAAAA6ABAAMAAAABAAEAAKACAAQAAAABAAAAQKADAAQAAAABAAAAQAAAAABGUUKwAAAaFElEQVR4Ae2bd7hdRbXAZ3Y559yS5KZAQjSIdAyQYGhSQ28qKFIi6KMJ+oCHKFL0qeFTwAKCFPkElM+CFJ8FVEikP3kKGEqA8EIaIBDSgCQ3995z9t4z6/3W7HNubkKiAcnnH4/JnTOzZ09Za82a1WbHmHfTuxR4lwL/nylg14r8hGmpGTQhNuYFumxiTMNY816qdUpNG5H7mvWR1Hupb0jZ1mzTchjPleZzStnFc88ya4YMMSZutlM1EfVB+q5ZWsoO403Ov2G6is15u17Smgmw+/MXGFv7pIkTwK6IiVNrksSayFoTRxakSsSqlFWmqIm2WdNOrgF9TWinrkTQUvspAciWujAVdWMT6qlYq+9i2hKxEtFXiZMa1rUNE0m3Se0Cm8hsH8kTJnGPmErlWWYq6PlPJ0BYLe0y+2IwuMBEbL5CGJFjoAUqFiU3+2upWQHWdi01aQmNtHt4byGGplZ/LTWJgGE5VCuhF3QMNZ0vJMpAGLOtVOz+Vuf0tiGFPGOc/Jq1bmHtec3Ob6torVQO3mnmN0xS+08Tx2KiqrD7JfIwgonArIVYCrhh5xkWuKBZ1kCoDRSape6yoR7KkmvKnacuCTgngJ8wV8kR7Dh9dfd1W8hSPpecUtaFueA2BYbk5Q1r5GYfRd9jprmh7S3+lAQYO6Niqvk3THX4uSAPokAel3xbcoByATNrVuAUMUUIZAMBFEntrs8t5FuE0X5abxJAadpCcGAJQQKi/WuwVmCICALFsEuCTGitrzIDyEE+MgmNvlhijPuOn/fSlWaLLRqssM5J0QG4FdtY276TSN+9Rto9M/LnWETpE3hYlyt5VvsLUHlvjaPeytru2RjX3y7G8cxUoa1gjCJV0ABOUTjsJefrsRGnq+m6kXh+Ih+1QZQh1sqGdB/OrsfheHmdkFUF8li6FuBrZYSN0+/Em40+0NUXn25qG8zSTuuSSg5Yl57/ij4zpALqXXDVmDgyH/TWHWzSaC+O3wjY31iXe2P5C7sAYZIU6uWvRN6fUKRd9/wrQF7/a4qMiXJ3ri3c3EhEorxH4rzbJflSF+evu9gvldi93h03Fn5sXYBZvxywSDoRp4dLYSaKkw2st4usM/e7NnO7GW171wXAtfYRGRH57POcgrOREe22aOj5oTvyIqJFrQrJj3LJRnetdY71+uJV2dku8E9EizisC8gvs1svkZ8XiWf6R80MGf9OrJ/ky3cnP5PICknyRS7JFsINC1zsFknkFi5Os4Xj/t4664cDFsl462UKYm2k1L1rCkpr1HRhk6IENZb5l30R7Wt2sLNXB3CCnJq2Z9Ut2mLzgTYvY6piO2riexn3QlwkT/2odvmcVcasWDQqqcU/Q4PtL3kfUpj9J0maRLbIniySjn2MHbp0lTHNh1UJsMPM0aazrctUOnlNDpYdlmBp0oYmhFJZ1jIsv+Y77a7qrQbztbvtbSX6BjpsU8m8fx8S/szBxmyBvpkG01+Nwnojgwjocul1v5Id46MgVAkxU3ygcd7YQZE/sSrFnlXjNmtP7fAh6N0KVLScJV/4bmftww0vNxSVRb/6pf2l6iGwfW1w4opbsV4PNkXdl4YW06aVyLi+H7hkk9NDv9V+VhJg3F9PAvFvm7Q2yFQ6kKhgVQWrDrqAALZ5mTuZtBMNpLY7Xaza8u3Nd2yVaY8xitGYhfhRDL13tDHbVNB5iiLPU5YZ+fg8K3WwQID3oU3Hmd1s/45OkMntFbNMukzhs27f2VGrvL/Dur2qIkfGRnazCczDVBnz1b29u89HZ0+tXj6DJmNmyqBks/l/xDbY1eQZvEYnq+Z1VFjvDy7STe4L/Qb8lASYeH9il3U8LZWurU3ahtEBZilWTRWLR5FrIhsQHlDX9kAIJU4nq6k/kMLkaCgPu5+N8/O9DX3k0fq6pi4Ge9oDZhl/z+u4FiSk+Ufdnsnv9P3fS0fJUXF7Y+RhmMZfL5L4gw24K6/EUV8hizLn/+2B2pVTtss+N+F5Oa1eT4b/EQ4YjYBUIogkUSx5/RGXFnsZO1bp159gUNIDD6JMk2dtAkYJ26s5xdblCPXnlPrArO94llabnmttKzBY1Hcjj1zJ2WGZFhWGaEX7AB5yQUn8D5Oy+k9qV98RJ20TG7m/pmC9IlMM7Ya5jX4+oXHW78TU7tq152TErTlTKS3irQgGnWqINNklzszRqy9UcoC27jl7A+PTE0zSsYFJ4fsEg78KfdrZz0FMpzuvMkF3Ouy4t1EnO6vP+q40hzt4+qix0Rjf8H4/GOmeLTgWcIT+cdTtwoaRHZ8w8nIfciCO2SS5Fxg/YQ6wyxSMdU0faZx9fh6bySyTNsRGWSUxvZnvbUhl3Nzat+ak2ZybJa0ca3LkgTJlYiNT5NNcancfyAUrCbCuK/+jftNlq8j5ByD+KMlR0qOMOW+0scNwfub0if3CbCNTlsD+UIXTgHMTxdbJFNe9bJL52Jol9ZuWhJqHZmdv27Dye0TxGPZXCFcQNEgcz7vMqXz38Wp93lYuKR414gfBBUp89Rwi690hRTpuSmvOlQTY85XdTXXI+8wwnPkxvNaAx0iM95EI2RHUh1JXgdcBF8RmBflu1mQf35ziR+UzeHLXSR+QIbhHIUtHYJq82GNNt2qAmOMSzoMSAS9AiZDJVBfbY9aFE46Qz3ex23cVlcqufVkhGdzVwFdu2CjqLcy+r7R9536FKs6evdqm6emc/8AFHAMkaP0WV50wqQV16Qzt/NyeUOpBk1Q41/ij6u2pBxfYmueWZ0eJdV7C7s1Z9LiyNdHA0u1kboj+7DfGabtAUhsvqBuzoFRWwdG0dfcwVPguDHARAZatTcM5SeKD4szd5u6Wo1cnAodZST+8bup42rVF6I+l+8lZR2SF+5KLo8+5OG4vcjQk+KFZNO4UEk7kta6on4jvhET3Yukk1h1g5NFRxu68QDuVQrAS99o4XmK0B8TE08vITCmZzannvoHh0miWGRR4wxTuxXKZNfzCb373+KvW2QNt5m+NCplJnhcX8hChtbOdjQ9yh9hfI0U+ZgqZZVjc9EEEGx8Y98rN5k5RxRtSlmXjvfjHvfdPV6X6FMT4a57nE++131/4UHr5OXiN+7rC3uvTKHKwJe7R0P6x1bEzUEdsLMJZrTFxnvrw2BV7tvqsPAIHLB5t0sHDzXCMG84sR8BSihkKz46ueFWBGESlqquY5bD/S61J1qm8Dd452q6igsK438nW6JI7UJBbSOacTZCMmdzp2u0kc6hd3iiKwytx/FuOiop15GvYs9eLojg2TdO7dY6j5Lb48fyxk+pxfGFW+GsWV791UQumtHjsBInjG02uvgJESONI8sb1rrLXqdpnJQFaI97JcqFsH+dmS1ReAe2fNZvZWWuc/veyDRIMJLUvpjN6O8ohQmEnydbX9RXjTvl2kkRne14BMnYNJBNZ6qw7NrXp1Naco/vO25hIQueC6iXPttpqfX/ZpIjNdMgHVxUeUaHa4KmiMmdHY0/L1w8BXsYXiM23TC4TAbiKB2ikT3qJ592FUPyyGb8GQvyXbI3/cDuHdEsCKCURGhAhs8fKyXZF4dxlqH6IoMYDAr0kwjKeJyVJsnaPD/8ryR76M/bKLibPNVqD7i6WO19sb9oOe7GUAS1yvRPlYtnbxjIV3joINZiiCbzv9Z56OyGfI5EFd5uHGmPftNQn7Ewp8sMJds7CREQmMC6yh2L+3my/vbgzjeMvFIW/DMR1KJYtQs3aIXEc38JxOOxN87UaMLjF+mkcWVoIJMEFJvKD0cqbapdSC2ht/BNdpmPoviZp68AY8qZGfLsD1dfB9qkhNAQ9pqKpDRHWxTwa8lYDSE0fNYx4JnK7rTTkVHaxSxrO74PWOBj5vRRG+8US419cztpxtLF18TWwMB7aaqbiJ6sz5SY5BBnwW7hlOwPhfBwdFg8ZfrO7Rjjz9hyHrIQI53AcIILW48E83wwRjoMT1mhSR8Y/QwwPIPUIQQS0hRTFZjzcXxJg++kdNvK/xRzeW0JAFF2nYXEltgYd6YUdVSbUpIb+gqpUdakztDLD0BbEIcSfCrGu3UANHnpD/M8MMXLIDCvPdQO0ifayU/3p/o7G/aZA9TIkeDgFZkyGXij8ZGujH0KE4ciEkgi5KBEmISK/lOfOJ0l8biCCEiSOB0GEX0CE4yHC7SWgK38JM/5NPclSgWsIjRT50VqUBJCeTU3UvrfaUyh65AWdFUvMaMABKXpqVgIWtKFOgy2fYeam7KK+01n1rNNdFfFXuqAZ5qAPJpgx74dDzhwpcsZrKr8YV9irbJE0OO/qD5Sw6Y1J8F+49MAy0M0Ka9b1OEQfjle4m91k+RSccB6cwAmJzgXYcBwgQCf5piYRfsuM/Qlv7DWBUAAJlAqhjnJq3jXtgMy/wJY+aGIsn5gdiQFEb4ASBoQSqZtwQ4HgKW+H4AdWDxNqAApbXF07CBGh500XCA3XCw1IwFplohyj3mJJUAgIIA5zqyCHUqijKl3I7bQlgTDqNCmBVCbE8YeTyP+HTsjZv4Apf4VaVNJDtyATOpALl0Jg5cX+ZNNqDwIjR4nSphmgywPd5IDn9uiWiYs+zE3EdrB+BeSYW7cbQpQ3UEoU7AjaYm4y1NNSf1eT3hxxtKPMf4jJv8wyHVh98nSvmF0HQ3IcsgAhU96zkGkb4QmiQT+IwXsNiZdI6ibptA7iEBunjspqvlMC98rcIi9+QwsCPZ/I9nwIxHliXNhdfWNuJivZVqasQQdQDkmZXseU0rQ8AvrigQ1X8PsXrb6dxJQPRA9jzCXRJfU+L/8+y8qPtxSzPYJSLfGfvWD9DfMAE8ZFLq+wzp0BV76oNx7hCOmiDSihaDuLoLTfb8oAACco3pBZaWGPyL5Z/V+QPwgOAFE7FBNetQEiA2nj/WTaL1wdfrEENsQjzDRpoZsQBSKtJIC++yeTT6JroxVyHJu17ROveb/HI1a2wbfqrlvznDq7RIGilH3L5Ap3XPKTNS53MdHjRC6GECMEf99akMeUFpeBfO05zvjhzP8zUB7kHchzBEjg7s8F+cvWNCedulgZFsZT4C5FiSA2De53kwBw6c7P7mGqXe8x7UO9GYLvMIrdUG9Qyw0YM4q6WtlDeO6krufZm+dNxT7av+iOdpmfkh0fRckvCWBu0YMTNK2bDWQIQJZM2uN/5JdG3+wfM7BySd8mcOpvgG68qlErAfkZUmRHmG/V5oD80SB/I3O1BxWIfOKwqJ44E+R/OHCqgXWxlfcoE9lMnVeQ52RxJT1f+5QEGD9tfyIdfxS9BVY1yPEMapC3FrUXvEKaw9V2jWf0ux5heK6ANXfgpuYZnSykgyvT/RTZGwb7PPLgUM7zSHYe50pmWudvcJ9Obml1XaW8pL6ptRXMYbudGk+B7XP/lGTREebS2vNFIZ8ijHo9yFd9qfpUnRAC9p9JouSm1lybv0YIdphpzLFXIY7LZF30gfKGG3WFWaoHAMfrBX1bEiCKupEJC63kg7laYnHFjr4qNti9IIjwE01Oi06rt8MaYTF2Hu8DK9G6Mh1sX2WS88xt8lXU3WAzi88nJttenWqN6XuyOccC1WXHhjCpCqhCHhMXfdxcav+WZe5UkL8awFNl+xBJElnOzn+6pfcnyuRkcd5zQp9xX5TcHMs60/vXipKdVE6qskKag0eeo7ZmKzwlAR6f8LDsMm8cgXTMF6SlQz6w3aoxQ4+w28RfwtbTlkHJGtfnxiziCCynXHNS7+8lDt1O/DuyGG6KeAFH5ykz1qrALdNl9S2JCGGE2W0UedaPUJEPY1UeaS6y89n5U2j5AeDHuMUt5JdgB0zCG7xHJ9kj/+Iey92KC7H39/VZ5OqYYs3ZGTZjlBR2B4SuIq/oWTjx1bxSfV77lATQ2iObLuRX8zuT5kt7VDfn84HLSXDQe4L0zWDaFWZWdHdxmTsgud5cLVtyTO5ga7YyBPeCr5vJfxOFOMpcbBeVgMgRsD2QqwwhhijyEsirK/znib3nvjdP3PmEw04poqhaYFtgPvShTdU0C6niavsgnEcYLkyC+iP0gDp/0thdw8atJEBrxDtRLpAO2yO3YiUeJghCUeQoOT7I62grKeLrolvcfrLEj2PDtyIYAnBIqczdJ43eY8xlg5e0wMjzxhlxXPsD7I59YhrYX1NB/vnduUBpxH6Ki5P31hGDIK97DPtGPpZK00gBZYmOX/nlhdrcehTiKa3533kCgHzU7a/iPv8wzylNAGU/nKWNuVB5fKmVxxZyr11H60h8DN8EYSSAPB6Syd1USeNjzUWDl7aA07Ktre0Fimu1PjANrvTOWZy3fyWT6IdYH2CMn4emYfddXkvCEUiz+TsSdtvPED8Pcg0OIjCygsuYqa25VhJgl2e3QOSfxn3ACJMM8qZCuJ5QcwiCBq+POTSCT8Q86qCuEaJBiAVta9d36Ngq7u/rfjzaa3sD8u1ImRvHGHN0lzpEqv6NnP+MkcunYx1ytxXsNdXzmfsDUZtPIijXLk9aEDfLu5Dy2zQmb4vPljrP9hsf+xT3gfhivYfQKAkj4jxM+6rJ9asMjE+NeRbuPlMbP681XUmAzR8ebPPeX5m2kdtJ8AfwCSL1COnG6WuVLQ9QUInhMxkt1erWkpn0m6pgOTeQ1WiNY4aJOXoon5OwDWp/VMD7wq2M+fUMIy/ytRxmsCI/RarxJHOe7WaWdUqbz76y2nhfz6WNxJ9hC4JNURo7cYtc7qZjaI4b0nFjdyNfdrBEnphjvbn7IKMy1sXXD1xEUWRXc6KkZlsTDizEUyEaBCkDNAwVsg5uZiUomYApxgV1TNiwo5jAppdO+v0gNsd47AVN6vzRApNaGYQBtbESTWUCcQIuy695K8gP7b5229c3sXfU0/YzevLY1+NKTEh8WeaTE+ZXLj4QJ/ETS819XZyrKzjrBDH4U42dcMvj5GFX3bqf/RW2kgOKYS9win6KIf/xIGnVwYJlFehQlv5e+axipAxkqKenPbRBLUzRmwlYppNbYZXD8uQbvH2vSu+yK6Vd3McFicp3tSuQbLBXCE/T8ndTtX7LlmKzk3ttdmoSJ10xDk5U6YyKvD4LY/jkxbXzH9IJ3mi7+CGbF7cRXN3KFN06P61YjOqU+colABPkQ2uxkgAzxuo+nmD2mP817gbU7WMM2amUwn3DO4PtISZHQxMEge21jQ8jlQAkgn6St1W5FZqENPo6X/NFt861cuBgkWM3ZlmIsJRrsS/cZ+RVLkYxJbG7/FzmmhnGT5uxcZz2jue7zCWFra9I213FR9koqWTb2iTfu8jy3UxH22C0CJTGY+LiP86Ln9g0+VqPPadU37Ba1Ouukjj5BIIPBNQnB7MEB6Toud1Vx7wpYqQ7+I6n6Nfu5wjC46QbFx4w9iBCshG0nPaKNXPYb+BXAkREfs40F8VXBwCmTu+INyqOMGnBrVI+gRBbp17DmyoTqFDX4+k4VzZ7FWzu5POY6/L2Ux7tB16kGr3hr8D6+CxHlk/q1KxgXMSnalIs8UWxm6ltOLu/f7OyXghgftazkfW1O4FgvPSAbR/boCY04gW7XPkHfNxPzRvxKea6VVnSTJ4cmWMOHBvXZILEjS0hyCCu/HKT5vMJ7z5ddPjHjT168SqILJRNsQCu4TPdg41GfuBhVbGWzwL0UNq879OuOuznq4xZrwTQya+RUYTELpYck9bhD6hiCoJPXuE2+Brzurn0Tcg3gVrnQnf9JX8iBtdXMZ1HB5mipzbhvMWYvG2o2KLvUp+2f2ltc64fDhi42iWyKch/EENnCL7AfPZmmrnErrqDA/uvS33GsmFxMvgjcMbpqNDS0UH4aAArGFcq2QbhK+fuJl+JT1xd8A1cYv0TYOBqb6euOvR/liAPRmyESTIeS3d/ie0BGBWbBAGvyl1tlbDzHC2NY3aiF3J3i++MTwJ5DQKsNa07AXZ76XisqiMx2VOMJEzuEDyF1ViQODuOqjpb+lEFGoI6+j7ED/h4M3wfzPVi+OBCbYNE65T6AYbuVtp8h+AKSIXP56kmrMXnshLLBuibkfRvD0aX6h0+By2NNB74mj/UUyQB1xl8N3KVGRqdAwT9ThEj1ph0+X+cdpl7FgBcwS0uEgWzTzHSjDwLACsZ9Z2qXE2l80bJn7Zp1o+wdae0r941hDYegxVZPuPrlh9MK1RktZ9UYGLT0h9tgNOjWi2M5X2YWyfkFdIASstyQiQXmI1i3Od1S/+YADvOOAOL5XL9ysJ4ja6SFDBsHeOUGDzoM0AEwBRYBZIcjMkmAZQmgQAUob+O0f3RGcn9sQd9bo4P8+l4hVL7ayedX+s6v/bTjy3YC75Ke5Cj8UUzOnqM1nVOOv3a0w7TPgvyVwgRBVYs7b4AMUMUCIQtiZ+yEhoVyJD1Fan1VrsqwK3Uem6VvGvNErpo31b//nf0KPvrHQWIKwvKXITs52S5PZDPb98S8rrOWjkg3u5Pp3qRqyAt5hTBmLBFuvqApI8tczgAxk/ZFpAJY9TzpS0YP8olilSzj5ZhF5WbMFVDO/ZLIG5ra7Q/xGYOfsvdDtwnMp2w2Y99R/RzM8a+Tq+3ldZAgMlRtM3up+FM/CAcsnC2+cSCgCkXzcBADm5fswzQsnbgCEoFXAFugUM9nFV9r8eg+b6fvXluyQkNtPYfFYVM5YOO0bHClyoicwiXPRjF/jfFkuRPZh9iTv9k0mVWTZtO2MyK+xSG+jTvkDx69aR3hVg0esuDh4UsCN5SEzCg03OpITdFTg0eLTXrf38JN9LUFQldTe8c1ZtUjaHIUQ33lvperVYdR5ifeXrhmiVomRdZcoarxU/yv9KeM++P9J7lHUu67GppsoJAmjxwHfrpnvaf0uaz9tO2IK/14S2k1lzNsf0swxSlt/kW5nq367sUeJcC71Lg7VHg/wAygMWOoyNgvgAAAABJRU5ErkJggg=="
)


def get_favicon_bytes():
    """Returns favicon PNG bytes, preferring on-disk assets, else embedded fallback."""
    candidates = [
        os.path.join(CODE_DIR, "assets", "favicon.png"),
        os.path.join(APP_DIR, "assets", "favicon.png"),
        os.path.join(os.getcwd(), "assets", "favicon.png"),
    ]
    for fav_path in candidates:
        try:
            if os.path.exists(fav_path):
                with open(fav_path, "rb") as f:
                    data = f.read()
                if data[:8] == b"\x89PNG\r\n\x1a\n":
                    return data
                # Accept any non-empty file; handler sends as image/png
                if data:
                    return data
        except Exception:
            continue
    import base64 as _b64
    try:
        return _b64.b64decode(_FAVICON_FALLBACK_B64)
    except Exception:
        return b""


def get_tunnel_executable():
    """Scoped wrapper so macOS Background Items shows DevBoost-tunnel instead of ssh."""
    packaged = get_packaged_app_executable()
    if packaged:
        return packaged
    return os.path.join(BIN_DIR, TUNNEL_WRAPPER_NAME)


def get_dashboard_executable():
    """Scoped wrapper so macOS Background Items shows DevBoost-dashboard instead of python3."""
    packaged = get_packaged_app_executable()
    if packaged:
        return packaged
    return os.path.join(BIN_DIR, DASHBOARD_WRAPPER_NAME)


def get_packaged_app_executable():
    """Returns the native DevBoost launcher when code is running from its app bundle."""
    resources = os.path.realpath(CODE_DIR)
    if os.path.basename(resources) != "Resources":
        return ""
    contents = os.path.dirname(resources)
    candidate = os.path.join(contents, "MacOS", "DevBoost")
    try:
        return candidate if os.path.isfile(candidate) and os.access(candidate, os.X_OK) else ""
    except OSError:
        return ""


def _installed_code_dir():
    """Where the legacy installed (launchd-safe) copy lives."""
    override = _env("DEVBOOST_CONFIG_DIR", "PORT_TRACKER_CONFIG_DIR", "")
    if override:
        return os.path.expanduser(override)
    return os.path.expanduser("~/.config/devboost")


def is_tcc_protected_path(path):
    """True for locations macOS privacy (TCC) shields from background daemons.

    launchd agents can neither EXECUTE code nor freely READ/WRITE here without
    an explicit access grant — ~/Documents, ~/Desktop, ~/Downloads, iCloud Drive.
    """
    try:
        p = os.path.normpath(os.path.expanduser(path or ""))
    except Exception:
        return False
    home = os.path.expanduser("~")
    for protected in ("Documents", "Desktop", "Downloads"):
        base = os.path.join(home, protected)
        if p == base or p.startswith(base + os.sep):
            return True
    icloud = os.path.join(home, "Library", "Mobile Documents")
    if p == icloud or p.startswith(icloud + os.sep):
        return True
    return False


def _is_launchable(path):
    """Whether launchd can exec this path (exists, executable, not TCC-shielded)."""
    try:
        return bool(path) and os.path.isfile(path) and os.access(path, os.X_OK) \
            and not is_tcc_protected_path(path)
    except OSError:
        return False

DEFAULT_LABELS = {
    3030: "Grafana Dashboard",
    3000: "Docker Web / App",
    8080: "Docker Proxy / Web App",
    9090: "Prometheus Metrics",
    9100: "Node Exporter",
    9835: "NVIDIA GPU Exporter",
    11434: "Ollama LLM API",
    15432: "PostgreSQL Docker",
    15433: "PostgreSQL Docker 2",
    18082: "Docker Service",
    43127: "WXT Extension Hot-Reload",
    50000: "App Port 50000",
    50001: "App Port 50001",
}


def ensure_dirs():
    # Primary state dir follows CONFIG_FILE so tests/custom overrides stay isolated.
    try:
        state_dir = os.path.dirname(os.path.abspath(CONFIG_FILE))
    except OSError:
        state_dir = APP_DIR
    os.makedirs(state_dir, exist_ok=True)
    # CONFIG_DIR is a backward-compat alias of APP_DIR; makedirs is idempotent.
    try:
        if os.path.abspath(CONFIG_DIR) != os.path.abspath(state_dir):
            os.makedirs(CONFIG_DIR, exist_ok=True)
    except OSError:
        pass
    if not get_packaged_app_executable():
        try:
            os.makedirs(BIN_DIR, exist_ok=True)
        except OSError:
            pass
    os.makedirs(LAUNCH_AGENTS_DIR, exist_ok=True)
    os.makedirs(LOG_DIR, exist_ok=True)


def slugify_server_id(ssh_host, existing_ids=None):
    """Converts an SSH host alias into a filesystem/plist-safe unique id."""
    base = re.sub(r"[^a-z0-9]+", "-", str(ssh_host or "server").lower()).strip("-") or "server"
    existing = set(existing_ids or [])
    if base not in existing:
        return base
    i = 2
    while f"{base}-{i}" in existing:
        i += 1
    return f"{base}-{i}"


def default_server_from_env(order=0):
    """Bootstraps the initial server entry from .env (legacy single-host settings)."""
    return {
        "id": slugify_server_id(SSH_HOST),
        "ssh_host": SSH_HOST,
        "name": SERVER_NAME,
        "ip": SERVER_IP,
        "order": order,
        "pinned": False,
    }


def ensure_servers_migrated(cfg):
    """Ensures cfg has a servers list; migrates legacy single-host .env config."""
    changed = False
    if not isinstance(cfg.get("servers"), list) or not cfg["servers"]:
        legacy = default_server_from_env(order=0)
        # Preserve any existing ids to avoid collision (unlikely on first migration)
        cfg["servers"] = [legacy]
        changed = True
    # Normalize server entries
    seen_ids = set()
    for idx, srv in enumerate(cfg["servers"]):
        if not isinstance(srv, dict):
            continue
        if not srv.get("ssh_host"):
            srv["ssh_host"] = SSH_HOST
        if not srv.get("id"):
            srv["id"] = slugify_server_id(srv.get("ssh_host", "server"), seen_ids)
        # De-duplicate ids
        if srv["id"] in seen_ids:
            srv["id"] = slugify_server_id(srv["id"], seen_ids)
        seen_ids.add(srv["id"])
        srv.setdefault("name", srv["ssh_host"])
        srv.setdefault("ip", "")
        srv.setdefault("order", idx)
        srv.setdefault("pinned", False)
    # Migrate legacy flat labels -> per-server labels for the bootstrap server.
    # Bootstrap = server matching current .env host, else first in file order
    # (stable: never the pinned/sorted tab order, so pinning can't move data).
    bootstrap_id = None
    for srv in cfg["servers"]:
        if isinstance(srv, dict) and srv.get("ssh_host") == SSH_HOST:
            bootstrap_id = srv.get("id")
            break
    if bootstrap_id is None:
        bootstrap_id = cfg["servers"][0]["id"] if isinstance(cfg["servers"][0], dict) else None
    if isinstance(cfg.get("labels"), dict) and cfg["labels"] and bootstrap_id:
        server_labels = cfg.setdefault("server_labels", {})
        if not server_labels.get(bootstrap_id):
            server_labels[bootstrap_id] = dict(cfg["labels"])
            changed = True
    # Tag legacy history entries missing server_id with the bootstrap server
    if isinstance(cfg.get("history"), list) and cfg["history"] and bootstrap_id:
        for h in cfg["history"]:
            if isinstance(h, dict) and not h.get("server_id"):
                h["server_id"] = bootstrap_id
                changed = True
    cfg.setdefault("server_labels", {})
    return cfg, changed


def _write_config_atomic(cfg):
    """Write config as a complete replacement so readers never see partial JSON."""
    directory = os.path.dirname(os.path.abspath(CONFIG_FILE))
    fd, temporary = tempfile.mkstemp(prefix=".config-", suffix=".json", dir=directory)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(cfg, f, indent=2)
            f.flush()
            os.fsync(f.fileno())
        os.replace(temporary, CONFIG_FILE)
    except Exception:
        try:
            os.unlink(temporary)
        except OSError:
            pass
        raise


def _migrate_legacy_config():
    """One-time copy of a legacy config.json into APP_DIR (never overwrites)."""
    if os.path.exists(CONFIG_FILE):
        return None
    try:
        # Custom/test overrides (CONFIG_FILE outside APP_DIR) never auto-migrate.
        if os.path.abspath(os.path.dirname(os.path.abspath(CONFIG_FILE))) != os.path.abspath(APP_DIR):
            return None
    except OSError:
        return None
    for legacy_path in LEGACY_CONFIG_FILES:
        try:
            if not legacy_path or os.path.abspath(legacy_path) == os.path.abspath(CONFIG_FILE):
                continue
        except OSError:
            continue
        if os.path.exists(legacy_path):
            try:
                with open(legacy_path, "r") as f:
                    legacy_cfg = json.load(f)  # validate before migrating
                ensure_dirs()
                _write_config_atomic(legacy_cfg)
                return legacy_path
            except Exception:
                continue
    return None


def load_config():
    ensure_dirs()
    _migrate_legacy_config()
    if os.path.exists(CONFIG_FILE):
        try:
            with open(CONFIG_FILE, "r") as f:
                cfg = json.load(f)
                cfg.setdefault("labels", {})
                cfg.setdefault("rules", {})
                cfg.setdefault("docker_labels", [
                    {"id": "dev", "name": "Dev", "match": "dev", "color": "#58a6ff", "enabled": True},
                    {"id": "production", "name": "production", "match": "production", "color": "#f85149", "enabled": True},
                ])
                cfg.setdefault("history", [])
                cfg, changed = ensure_servers_migrated(cfg)
                cfg, changed2 = ensure_syncs_migrated(cfg)
                changed = changed or changed2
                if changed:
                    try:
                        _write_config_atomic(cfg)
                    except Exception:
                        pass
                return cfg
        except Exception:
            pass
    cfg = {"labels": {}, "rules": {}, "docker_labels": [
               {"id": "dev", "name": "Dev", "match": "dev", "color": "#58a6ff", "enabled": True},
               {"id": "production", "name": "production", "match": "production", "color": "#f85149", "enabled": True},
           ], "history": [], "server_labels": {},
           "servers": [default_server_from_env(order=0)], "syncs": [],
           "folder_history": []}
    return cfg


def save_config(cfg):
    ensure_dirs()
    cfg, _ = ensure_servers_migrated(cfg)
    cfg, _ = ensure_syncs_migrated(cfg)
    _write_config_atomic(cfg)


def sort_servers(servers):
    """Pinned tabs first, then explicit order, then name."""
    return sorted(servers or [], key=lambda s: (not s.get("pinned", False), s.get("order", 0), (s.get("name") or s.get("ssh_host") or "").lower()))


def get_servers(cfg=None):
    cfg = cfg if cfg is not None else load_config()
    return sort_servers(cfg.get("servers", []))


def get_server(cfg, server_ref):
    """Looks up a server by id or ssh_host. Returns None if not found."""
    if not server_ref:
        return None
    for srv in cfg.get("servers", []):
        if srv.get("id") == server_ref or srv.get("ssh_host") == server_ref:
            return srv
    return None


def get_default_server(cfg=None):
    cfg = cfg if cfg is not None else load_config()
    servers = get_servers(cfg)
    if servers:
        return servers[0]
    return default_server_from_env(order=0)


def get_legacy_server(cfg=None):
    """Stable bootstrap server for backward compat (existing LaunchAgents/labels).

    Unlike get_default_server() (which follows pinned/sorted tab order for UX),
    this stays pinned to the .env host so legacy plist filenames never flip when
    the user pins or reorders tabs.
    """
    cfg = cfg if cfg is not None else load_config()
    servers = cfg.get("servers", [])
    if not servers:
        return default_server_from_env(order=0)
    for srv in servers:
        if srv.get("ssh_host") == SSH_HOST:
            return srv
    return min(servers, key=lambda s: (s.get("order", 0), s.get("id", "")))


def resolve_server(cfg, server_ref=None):
    """Resolves server_ref (id or ssh_host) to a server dict, falling back to default."""
    if server_ref:
        found = get_server(cfg, server_ref)
        if found:
            return found
    return get_default_server(cfg)


def get_server_labels(cfg, server_id):
    """Per-server labels with fallback to legacy flat labels for the default server."""
    labels = {}
    # Legacy flat labels act as fallback so old configs keep working
    if isinstance(cfg.get("labels"), dict):
        labels.update({str(k): v for k, v in cfg["labels"].items()})
    per = cfg.get("server_labels", {}).get(server_id, {})
    if isinstance(per, dict):
        labels.update({str(k): v for k, v in per.items()})
    return labels


def set_server_label(cfg, server_id, port, label):
    cfg.setdefault("server_labels", {}).setdefault(server_id, {})[str(port)] = label
    # Keep legacy flat labels in sync for the bootstrap server (backward compat)
    try:
        if get_legacy_server(cfg).get("id") == server_id:
            cfg.setdefault("labels", {})[str(port)] = label
    except Exception:
        pass


def remove_server_label(cfg, server_id, port):
    cfg.get("server_labels", {}).get(server_id, {}).pop(str(port), None)
    try:
        if get_legacy_server(cfg).get("id") == server_id:
            cfg.get("labels", {}).pop(str(port), None)
    except Exception:
        pass


def add_server(ssh_host, name=None, ip=""):
    """Adds a new server tab. Raises ValueError on empty/duplicate ssh_host."""
    ssh_host = (ssh_host or "").strip()
    if not ssh_host:
        raise ValueError("ssh_host is required")
    cfg = load_config()
    for srv in cfg.get("servers", []):
        if srv.get("ssh_host") == ssh_host:
            raise ValueError(f"Server '{ssh_host}' is already added")
    existing_ids = [s.get("id") for s in cfg.get("servers", [])]
    server_id = slugify_server_id(ssh_host, existing_ids)
    max_order = max([s.get("order", 0) for s in cfg.get("servers", [])] + [-1])
    server = {
        "id": server_id,
        "ssh_host": ssh_host,
        "name": (name or "").strip() or ssh_host,
        "ip": (ip or "").strip(),
        "order": max_order + 1,
        "pinned": False,
    }
    cfg["servers"].append(server)
    save_config(cfg)
    return server


def remove_server_entry(server_id):
    """Removes a server tab + its labels/history/syncs. Cleans tunnels/sync agents. Returns True if removed."""
    cfg = load_config()
    server = get_server(cfg, server_id)
    if not server:
        return False
    if len(cfg.get("servers", [])) <= 1:
        raise ValueError("Cannot remove the last server")
    cfg["servers"] = [s for s in cfg["servers"] if s.get("id") != server["id"]]
    cfg.get("server_labels", {}).pop(server["id"], None)
    cfg["history"] = [h for h in cfg.get("history", []) if h.get("server_id") != server["id"]]
    sync_ids = [s.get("id") for s in cfg.get("syncs", []) if isinstance(s, dict) and s.get("server_id") == server["id"]]
    cfg["syncs"] = [s for s in cfg.get("syncs", []) if not (isinstance(s, dict) and s.get("server_id") == server["id"])]
    cfg["folder_history"] = [h for h in cfg.get("folder_history", []) if h.get("server_id") != server["id"]]
    save_config(cfg)
    # Best-effort cleanup of that server's tunnels (outside config write)
    try:
        remove_launchagents_for_server(server)
    except Exception:
        pass
    try:
        kill_server_processes(server)
    except Exception:
        pass
    for sid in sync_ids:
        try:
            remove_sync_agent(sid)
        except Exception:
            pass
    return True


def update_server_entry(server_id, name=None, ip=None):
    cfg = load_config()
    server = get_server(cfg, server_id)
    if not server:
        return None
    if name is not None:
        server["name"] = name.strip() or server["ssh_host"]
    if ip is not None:
        server["ip"] = ip.strip()
    save_config(cfg)
    return server


def reorder_servers(order_ids):
    """Persists tab order from a list of server ids (tabs can be drag-sorted)."""
    cfg = load_config()
    id_to_server = {s.get("id"): s for s in cfg.get("servers", [])}
    order = 0
    for sid in order_ids or []:
        if sid in id_to_server:
            id_to_server[sid]["order"] = order
            order += 1
    # Any servers not mentioned keep their relative order at the end
    remaining = [s for s in sorted(cfg.get("servers", []), key=lambda s: s.get("order", 0)) if s.get("id") not in set(order_ids or [])]
    for srv in remaining:
        srv["order"] = order
        order += 1
    save_config(cfg)
    return get_servers(cfg)


def set_server_pinned(server_id, pinned):
    cfg = load_config()
    server = get_server(cfg, server_id)
    if not server:
        return None
    server["pinned"] = bool(pinned)
    save_config(cfg)
    return server


# -----------------------------
# Folder sync (two-way mirror)
# -----------------------------
# Semantics (user-facing, also in README + dashboard modal):
# - direction "two-way" (default): bidirectional merge, newer file wins
#   (rsync --update both ways). Deletions NEVER propagate — safe merge.
# - direction "push"/"pull": one-way. mirror=False (default) keeps extra files
#   on the destination; mirror=True makes the destination an exact copy
#   (rsync --delete, deletions propagate). Mirror is ignored for two-way.
# - always False = "Once": one-time sync (runs now + on-demand via Sync Now,
#   no background agent; the Session equivalent for ports).
# - always True = "Auto": persistent LaunchAgent (WatchPaths for instant local
#   triggers + StartInterval polling every `interval` seconds for remote
#   changes; the Always equivalent for ports).

def ensure_syncs_migrated(cfg):
    """Ensures cfg has syncs + folder_history lists; normalizes entries."""
    changed = False
    if not isinstance(cfg.get("syncs"), list):
        cfg["syncs"] = []
        changed = True
    if not isinstance(cfg.get("folder_history"), list):
        cfg["folder_history"] = []
        changed = True
    seen_ids = set()
    server_ids = set()
    try:
        server_ids = {s.get("id") for s in cfg.get("servers", []) if isinstance(s, dict) and s.get("id")}
    except Exception:
        pass
    for s in cfg["syncs"]:
        if not isinstance(s, dict):
            continue
        if not s.get("id"):
            s["id"] = uuid.uuid4().hex[:8]
            changed = True
        if s["id"] in seen_ids:
            s["id"] = uuid.uuid4().hex[:8]
            changed = True
        seen_ids.add(s["id"])
        if s.get("direction") not in SYNC_DIRECTIONS:
            s["direction"] = SYNC_DEFAULT_DIRECTION
            changed = True
        # Mirror is only meaningful for one-way; force off for two-way.
        if s.get("direction") == "two-way" and s.get("mirror"):
            s["mirror"] = False
            changed = True
        else:
            s["mirror"] = bool(s.get("mirror", False))
        s["always"] = bool(s.get("always", False))
        try:
            iv = int(s.get("interval", SYNC_DEFAULT_INTERVAL))
        except (TypeError, ValueError):
            iv = SYNC_DEFAULT_INTERVAL
        iv = max(SYNC_MIN_INTERVAL, min(SYNC_MAX_INTERVAL, iv))
        if s.get("interval") != iv:
            s["interval"] = iv
            changed = True
        s.setdefault("local_path", "")
        s.setdefault("remote_path", "")
        s.setdefault("server_id", "")
        s.setdefault("created_at", time.time())
        s.setdefault("last_sync", None)
        s.setdefault("last_status", "never")
        s.setdefault("last_message", "")
        # Drop syncs pointing at removed servers? Keep them but they resolve
        # to default on read; cleanup happens on remove_server_entry.
        _ = server_ids
    # Normalize folder_history entries
    kept = []
    for h in cfg["folder_history"]:
        if isinstance(h, dict) and (h.get("local_path") or h.get("remote_path")):
            h.setdefault("server_id", "")
            h.setdefault("last_used", 0)
            kept.append(h)
    if len(kept) != len(cfg["folder_history"]):
        cfg["folder_history"] = kept
        changed = True
    return cfg, changed


def _normalize_sync_path(p, is_remote=False):
    """Normalizes a sync path: expands ~ (local only), strips whitespace."""
    p = (p or "").strip()
    if not p:
        return ""
    if not is_remote:
        # Expand ~ and make absolute; keep as-entered for display otherwise.
        p = os.path.expanduser(p)
        if not os.path.isabs(p):
            # Resolve relative to $HOME (dashboard browsers always send absolute,
            # CLI users may send ~/x which is already expanded above).
            p = os.path.join(os.path.expanduser("~"), p)
        p = os.path.normpath(p)
    else:
        # Remote (POSIX): expand leading ~/ via $HOME is resolved server-side;
        # keep "~" as-is for display, normalize absolute paths with posixpath.
        if p.startswith("~"):
            return p  # e.g. ~, ~/projects — resolved by ssh/rsync remotely
        if not p.startswith("/"):
            p = "/" + p
        p = posixpath.normpath(p)
    return p


def validate_sync_paths(local_path, remote_path):
    """Validates a sync pair. Returns (local, remote) normalized or raises ValueError."""
    local = _normalize_sync_path(local_path, is_remote=False)
    remote = _normalize_sync_path(remote_path, is_remote=True)
    if not local or not remote:
        raise ValueError("Both local and remote folders are required")
    if local == "/":
        raise ValueError("Refusing to sync the filesystem root (/) — pick a subfolder")
    if remote in ("/",):
        raise ValueError("Refusing to sync the remote filesystem root (/) — pick a subfolder")
    if ":" in remote:
        raise ValueError("Remote path must not contain ':' (rsync host:path separator)")
    return local, remote


def get_sync(cfg, sync_id):
    for s in cfg.get("syncs", []):
        if isinstance(s, dict) and s.get("id") == sync_id:
            return s
    return None


def get_syncs_for_server(cfg, server_id):
    return [s for s in cfg.get("syncs", []) if isinstance(s, dict) and s.get("server_id") == server_id]


def get_sync_plist_label(sync_id):
    safe = re.sub(r"[^A-Za-z0-9_-]+", "-", str(sync_id)) or "sync"
    return f"{SYNC_AGENT_PREFIX}-{safe}"


def get_sync_plist_path(sync_id):
    return os.path.join(LAUNCH_AGENTS_DIR, f"{get_sync_plist_label(sync_id)}.plist")


def get_sync_executable_args(sync_id):
    """ProgramArguments for a sync LaunchAgent: runs `devboost.py sync-run <id>`.

    Prefers the running copy (latest code), but NEVER points launchd at an
    executable under a TCC-protected location (e.g. a repo checkout inside
    ~/Documents) — launchd cannot exec there and the agent would die with
    exit 126. Falls back to the installed copy, which exists exactly for that.
    """
    sid = str(sync_id)
    candidates = [get_dashboard_executable()]
    inst = _installed_code_dir()
    if os.path.abspath(inst) != os.path.abspath(CODE_DIR):
        candidates.append(os.path.join(inst, "bin", DASHBOARD_WRAPPER_NAME))
    for wrapper in candidates:
        if _is_launchable(wrapper):
            return [wrapper, "sync-run", sid]
    # Last resort: direct python on the installed script (or running copy).
    for script in (os.path.join(inst, "devboost.py"),
                   os.path.join(CODE_DIR, "devboost.py")):
        if os.path.isfile(script) and not is_tcc_protected_path(script):
            return [sys.executable or "/usr/bin/python3", script, "sync-run", sid]
    return [get_dashboard_executable(), "sync-run", sid]


def _scan_sync_agents_raw():
    """Scans LaunchAgents for sync plists -> {sync_id: agent}."""
    out = {}
    pat = os.path.join(LAUNCH_AGENTS_DIR, f"{SYNC_AGENT_PREFIX}-*.plist")
    for plist_path in glob.glob(pat):
        try:
            with open(plist_path, "rb") as f:
                data = plistlib.load(f)
        except Exception:
            continue
        label = data.get("Label", "")
        prefix = SYNC_AGENT_PREFIX + "-"
        sid = label[len(prefix):] if label.startswith(prefix) else None
        if not sid:
            m = re.search(rf"{re.escape(SYNC_AGENT_PREFIX)}-(.+)\.plist$", plist_path)
            sid = m.group(1) if m else None
        if sid:
            out.setdefault(sid, {"label": label, "path": plist_path, "data": data})
    return out


def create_sync_agent(sync):
    """Installs/refreshes the persistent LaunchAgent for an Auto sync."""
    sid = sync.get("id")
    interval = max(SYNC_MIN_INTERVAL, min(SYNC_MAX_INTERVAL, int(sync.get("interval", SYNC_DEFAULT_INTERVAL))))
    plist_path = get_sync_plist_path(sid)
    data = {
        "Label": get_sync_plist_label(sid),
        "ProgramArguments": get_sync_executable_args(sid),
        "RunAtLoad": True,
        "StartInterval": interval,
        "ThrottleInterval": 10,
        "StandardOutPath": os.path.join(LOG_DIR, f"devboost-sync-{sid}.log"),
        "StandardErrorPath": os.path.join(LOG_DIR, f"devboost-sync-{sid}.err"),
    }
    # WatchPaths gives near-real-time triggers for local changes; polling
    # (StartInterval) covers remote changes. Only set when the local dir exists.
    try:
        local = sync.get("local_path", "")
        if local and os.path.isdir(os.path.expanduser(local)):
            data["WatchPaths"] = [os.path.expanduser(local)]
    except Exception:
        pass
    # Reload if already loaded (unload errors are fine — first install).
    try:
        subprocess.run(["launchctl", "unload", "-w", plist_path],
                       stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    except Exception:
        pass
    with open(plist_path, "wb") as f:
        plistlib.dump(data, f)
    try:
        subprocess.run(["launchctl", "load", "-w", plist_path],
                       stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    except Exception:
        pass
    return plist_path


def restore_auto_sync_agents():
    """Refresh persistent sync agents after the packaged app is updated."""
    cfg = load_config()
    for sync in cfg.get("syncs", []):
        if isinstance(sync, dict) and sync.get("always") and sync.get("id"):
            try:
                create_sync_agent(sync)
            except Exception:
                pass


def restore_packaged_forward_agents():
    """Move legacy DevBoost tunnel agents to the packaged app launcher."""
    packaged = get_packaged_app_executable()
    if not packaged:
        return 0
    patterns = [
        os.path.join(LAUNCH_AGENTS_DIR, f"{AGENT_PREFIX}-*.plist"),
        os.path.join(LAUNCH_AGENTS_DIR, "com.*.ssh-forward-*.plist"),
    ]
    migrated = 0
    seen = set()
    for pattern in patterns:
        for plist_path in glob.glob(pattern):
            if plist_path in seen:
                continue
            seen.add(plist_path)
            try:
                with open(plist_path, "rb") as f:
                    data = plistlib.load(f)
                args = data.get("ProgramArguments", [])
                if not args:
                    continue
                already_packaged = os.path.abspath(args[0]) == os.path.abspath(packaged)
                # Only rewrite DevBoost's own wrapper; never touch another app's SSH agent.
                if not already_packaged and os.path.basename(args[0]) != TUNNEL_WRAPPER_NAME:
                    continue
                tunnel_args = args[1:] if not already_packaged else args[1:]
                if tunnel_args and tunnel_args[0] == "--tunnel":
                    continue
                data["ProgramArguments"] = [packaged, "--tunnel"] + tunnel_args
                for key in ("StandardOutPath", "StandardErrorPath"):
                    previous = data.get(key, "")
                    if os.path.basename(previous).startswith("devboost-forward-"):
                        data[key] = os.path.join(LOG_DIR, os.path.basename(previous))
                subprocess.run(["launchctl", "unload", "-w", plist_path],
                               stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
                with open(plist_path, "wb") as f:
                    plistlib.dump(data, f)
                subprocess.run(["launchctl", "load", "-w", plist_path],
                               stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
                migrated += 1
            except Exception:
                continue
    return migrated


def remove_sync_agent(sync_id):
    plist_path = get_sync_plist_path(sync_id)
    # Also catch legacy/renamed files for the same id (glob by suffix).
    candidates = {plist_path}
    try:
        for p in glob.glob(os.path.join(LAUNCH_AGENTS_DIR, f"*{sync_id}*.plist")):
            if os.path.basename(p).startswith(SYNC_AGENT_PREFIX) or sync_id in p:
                candidates.add(p)
    except Exception:
        pass
    for p in candidates:
        try:
            subprocess.run(["launchctl", "unload", "-w", p],
                           stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        except Exception:
            pass
        try:
            if os.path.exists(p):
                os.remove(p)
        except OSError:
            pass


def remove_sync_agents_for_server(server_id):
    cfg = load_config()
    for s in get_syncs_for_server(cfg, server_id):
        try:
            remove_sync_agent(s.get("id"))
        except Exception:
            pass


def _ssh_remote_cmd(host, remote_cmd):
    """Runs a remote shell command via ssh. Returns CompletedProcess."""
    return run_ssh_command(host, remote_cmd, timeout=30, connect_timeout=5)


def _ensure_remote_dir(host, remote_path):
    quoted = shlex.quote(remote_path)
    res = _ssh_remote_cmd(host, f"mkdir -p -- {quoted} && echo OK")
    return res.returncode == 0


def check_rsync_prereqs(ssh_host):
    """Verifies rsync exists locally and on the remote host.

    Returns None when fine, else an actionable error message. Code-12
    protocol errors almost always mean one of these two is missing.
    """
    if not shutil.which("rsync"):
        return ("rsync not found on this Mac "
                "(install with `brew install rsync`)")
    try:
        res = _ssh_remote_cmd(ssh_host, "command -v rsync")
    except Exception as e:
        return f"Cannot reach {ssh_host} to check for rsync: {e}"
    if res.returncode != 0 or not (res.stdout or "").strip():
        return (f"rsync not found on server '{ssh_host}' "
                f"(install with `sudo apt install rsync`)")
    return None


def explain_rsync_output(text, ssh_host=""):
    """Appends ONE targeted hint to raw rsync stderr based on known patterns.

    Hints are exclusive and ordered by conclusiveness: a local TCC denial
    explains the whole failure (including the follow-on "connection closed" /
    code-12 lines), so no other guess is appended in that case.
    """
    t = text or ""
    host = f" '{ssh_host}'" if ssh_host else ""
    if "Operation not permitted" in t:
        return t + " — macOS blocked local file access (grant Full Disk Access, see dashboard help)"
    if "command not found" in t and "rsync" in t:
        return t + f" — rsync is missing on the remote side (install with `sudo apt install rsync` on{host})"
    if "Permission denied (publickey" in t:
        return t + f" — SSH key rejected by{host} (check `ssh{host}` works without a password prompt)"
    if "No such file or directory" in t:
        return t + " — a synced path (or its parent) does not exist on that side"
    if "connection unexpectedly closed" in t:
        return (t + f" — the remote rsync never started on{host}: "
                "either rsync is not installed there, or the remote shell prints "
                "startup text (motd/echo in ~/.bashrc) that corrupts the protocol stream")
    return t


def build_rsync_commands(sync, ssh_host):
    """Builds the ordered rsync command list for a sync entry (no shell)."""
    direction = sync.get("direction", SYNC_DEFAULT_DIRECTION)
    mirror = bool(sync.get("mirror", False)) and direction in ("push", "pull")
    local = sync.get("local_path", "")
    remote = sync.get("remote_path", "")
    ssh_opts = "ssh -o BatchMode=yes -o ConnectTimeout=5 -o ServerAliveInterval=15 -o ServerAliveCountMax=3"
    local_src = local.rstrip("/") + "/"
    remote_spec = f"{ssh_host}:{remote.rstrip('/')}/"
    remote_src = f"{ssh_host}:{remote.rstrip('/')}/"
    cmds = []
    base = ["rsync", "-az", "--update", "-e", ssh_opts]
    if direction == "push":
        cmd = base + (["--delete"] if mirror else []) + [local_src, remote_spec]
        cmds.append(cmd)
    elif direction == "pull":
        cmd = base + (["--delete"] if mirror else []) + [remote_src, local_src]
        cmds.append(cmd)
    else:  # two-way: push then pull, newer wins, never delete
        cmds.append(base + [local_src, remote_spec])
        cmds.append(base + [remote_src, local_src])
    return cmds


def run_sync(sync_id, timeout=300):
    """Runs one sync pass now (used by Sync Now, Once creation, and LaunchAgent).

    Returns {"ok": bool, "message": str}. Updates last_sync/last_status and
    folder_history in config. Never raises (errors are captured in message).
    """
    try:
        cfg = load_config()
    except Exception as e:
        return {"ok": False, "message": f"Cannot load config: {e}"}
    sync = get_sync(cfg, sync_id)
    if not sync:
        return {"ok": False, "message": f"Sync '{sync_id}' not found"}
    server = get_server(cfg, sync.get("server_id"))
    if not server:
        # Server tab removed but sync row survived: resolve to default so the
        # error message names a concrete host instead of failing silently.
        server = get_default_server(cfg)
    ssh_host = server.get("ssh_host")
    local = sync.get("local_path", "")
    remote = sync.get("remote_path", "")

    def _fail(msg):
        sync["last_status"] = "error"
        sync["last_message"] = msg
        try:
            save_config(cfg)
        except Exception:
            pass
        return {"ok": False, "message": msg}

    # Local side: create on pull/two-way so first sync just works.
    try:
        os.makedirs(os.path.expanduser(local), exist_ok=True)
    except Exception as e:
        return _fail(f"Cannot create local folder {local}: {e}")
    if not _ensure_remote_dir(ssh_host, remote):
        return _fail(f"Cannot reach {ssh_host} or create {remote} (check SSH keys)")
    # Preflight: code-12 protocol errors almost always mean rsync is missing
    # on one side — fail fast with the fix instead of cryptic stderr.
    prereq_msg = check_rsync_prereqs(ssh_host)
    if prereq_msg:
        return _fail(prereq_msg)
    cmds = build_rsync_commands(sync, ssh_host)
    errors = []
    for cmd in cmds:
        try:
            res = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
        except FileNotFoundError:
            errors.append("rsync not found (install rsync on this Mac)")
            break
        except subprocess.TimeoutExpired:
            errors.append(f"rsync timed out after {timeout}s")
            break
        except Exception as e:
            errors.append(str(e))
            break
        if res.returncode != 0:
            lines = [l for l in (res.stderr or res.stdout or f"exit {res.returncode}").strip().splitlines() if l.strip()]
            # The cause is usually the first line(s); the last line is just the
            # "code 12" summary — keep up to 3 lines so the cause survives.
            shown = " / ".join(lines[:2] + ([lines[-1]] if len(lines) > 2 and lines[-1] not in lines[:2] else []))
            if not shown:
                shown = f"rsync exit {res.returncode}"
            errors.append(explain_rsync_output(shown, ssh_host))
            break
    now = time.time()
    sync["last_sync"] = now
    if errors:
        sync["last_status"] = "error"
        sync["last_message"] = "; ".join(errors)[:500]
    else:
        sync["last_status"] = "ok"
        n = len(cmds)
        sync["last_message"] = f"Synced {local} <-> {ssh_host}:{remote} ({sync.get('direction')})"
    try:
        save_config(cfg)
    except Exception:
        pass
    try:
        record_folder_history(sync.get("server_id"), local, remote)
    except Exception:
        pass
    if errors:
        return {"ok": False, "message": "; ".join(errors)[:500]}
    return {"ok": True, "message": sync["last_message"]}


def add_sync(server_ref=None, local_path="", remote_path="", direction="two-way",
             mirror=False, always=False, interval=SYNC_DEFAULT_INTERVAL, run_now=True):
    """Creates a sync entry, installs agent if Auto, optionally runs once now."""
    cfg = load_config()
    server = resolve_server(cfg, server_ref)
    sid = server.get("id")
    local, remote = validate_sync_paths(local_path, remote_path)
    direction = (direction or SYNC_DEFAULT_DIRECTION).strip().lower()
    if direction not in SYNC_DIRECTIONS:
        raise ValueError(f"direction must be one of {', '.join(SYNC_DIRECTIONS)}")
    if direction == "two-way":
        mirror = False  # safe merge only (see semantics above)
    try:
        interval = int(interval or SYNC_DEFAULT_INTERVAL)
    except (TypeError, ValueError):
        interval = SYNC_DEFAULT_INTERVAL
    interval = max(SYNC_MIN_INTERVAL, min(SYNC_MAX_INTERVAL, interval))
    # De-dupe: same server + same pair reuses the row (updates params instead).
    for s in get_syncs_for_server(cfg, sid):
        if s.get("local_path") == local and s.get("remote_path") == remote:
            s["direction"] = direction
            s["mirror"] = bool(mirror)
            s["always"] = bool(always)
            s["interval"] = interval
            save_config(cfg)
            sync = s
            break
    else:
        sync = {
            "id": uuid.uuid4().hex[:8],
            "server_id": sid,
            "local_path": local,
            "remote_path": remote,
            "direction": direction,
            "mirror": bool(mirror),
            "always": bool(always),
            "interval": interval,
            "created_at": time.time(),
            "last_sync": None,
            "last_status": "never",
            "last_message": "",
        }
        cfg.setdefault("syncs", []).append(sync)
        save_config(cfg)
    try:
        record_folder_history(sid, local, remote)
    except Exception:
        pass
    # Ensure the local dir exists now so the Auto agent can WatchPaths it.
    try:
        os.makedirs(os.path.expanduser(local), exist_ok=True)
    except Exception:
        pass
    if sync.get("always"):
        try:
            create_sync_agent(sync)
        except Exception:
            pass
    else:
        try:
            remove_sync_agent(sync.get("id"))
        except Exception:
            pass
    result = {"ok": True, "sync": dict(sync), "message": "Folder sync added"}
    if run_now:
        res = run_sync(sync.get("id"))
        # Re-read for fresh last_sync/status after the run.
        try:
            cfg2 = load_config()
            sync2 = get_sync(cfg2, sync.get("id"))
            if sync2:
                result["sync"] = dict(sync2)
        except Exception:
            pass
        result["run"] = res
        result["message"] = res.get("message", result["message"])
        result["ok"] = bool(res.get("ok"))
    return result


def remove_sync_entry(sync_id):
    cfg = load_config()
    sync = get_sync(cfg, sync_id)
    if not sync:
        return False
    cfg["syncs"] = [s for s in cfg.get("syncs", []) if s.get("id") != sync_id]
    save_config(cfg)
    try:
        remove_sync_agent(sync_id)
    except Exception:
        pass
    return True


def toggle_sync_always(sync_id, make_always, interval=None):
    cfg = load_config()
    sync = get_sync(cfg, sync_id)
    if not sync:
        return None
    sync["always"] = bool(make_always)
    if interval is not None:
        try:
            sync["interval"] = max(SYNC_MIN_INTERVAL, min(SYNC_MAX_INTERVAL, int(interval)))
        except (TypeError, ValueError):
            pass
    save_config(cfg)
    try:
        if sync["always"]:
            create_sync_agent(sync)
        else:
            remove_sync_agent(sync_id)
    except Exception:
        pass
    return sync


def update_sync(sync_id, local_path=None, remote_path=None, direction=None,
                mirror=None, always=None, interval=None):
    """Edits a sync entry's paths/params (server tab never changes).

    Only arguments that are not None are updated. Returns the updated sync
    dict, None if not found. Raises ValueError on invalid input.
    """
    cfg = load_config()
    sync = get_sync(cfg, sync_id)
    if not sync:
        return None
    if local_path is not None or remote_path is not None:
        local, remote = validate_sync_paths(
            local_path if local_path is not None else sync.get("local_path", ""),
            remote_path if remote_path is not None else sync.get("remote_path", ""))
        sync["local_path"] = local
        sync["remote_path"] = remote
    if direction is not None:
        direction = (direction or "").strip().lower()
        if direction not in SYNC_DIRECTIONS:
            raise ValueError(f"direction must be one of {', '.join(SYNC_DIRECTIONS)}")
        sync["direction"] = direction
    if mirror is not None:
        sync["mirror"] = bool(mirror)
    if sync.get("direction") == "two-way":
        sync["mirror"] = False  # safe merge only (see semantics above)
    if always is not None:
        sync["always"] = bool(always)
    if interval is not None:
        try:
            sync["interval"] = max(SYNC_MIN_INTERVAL, min(SYNC_MAX_INTERVAL, int(interval)))
        except (TypeError, ValueError):
            pass
    save_config(cfg)
    try:
        record_folder_history(sync.get("server_id"), sync.get("local_path"), sync.get("remote_path"))
    except Exception:
        pass
    # Ensure the local dir exists so the Auto agent can WatchPaths it.
    try:
        os.makedirs(os.path.expanduser(sync.get("local_path", "")), exist_ok=True)
    except Exception:
        pass
    try:
        if sync.get("always"):
            create_sync_agent(sync)
        else:
            remove_sync_agent(sync_id)
    except Exception:
        pass
    return sync


def get_syncs_status(server_ref=None):
    """Sync rows for one server tab, with agent presence (like forwards)."""
    cfg = load_config()
    server = resolve_server(cfg, server_ref)
    sid = server.get("id")
    agents = _scan_sync_agents_raw()
    rows = []
    for s in get_syncs_for_server(cfg, sid):
        sid_row = s.get("id")
        agent = agents.get(sid_row)
        rows.append({
            "id": sid_row,
            "server_id": sid,
            "local_path": s.get("local_path", ""),
            "remote_path": s.get("remote_path", ""),
            "direction": s.get("direction", SYNC_DEFAULT_DIRECTION),
            "mirror": bool(s.get("mirror", False)),
            "always": bool(s.get("always", False)),
            "agent_installed": agent is not None,
            "active": bool(s.get("always", False)) and agent is not None,
            "local_protected": is_tcc_protected_path(s.get("local_path", "")),
            "interval": s.get("interval", SYNC_DEFAULT_INTERVAL),
            "created_at": s.get("created_at"),
            "last_sync": s.get("last_sync"),
            "last_status": s.get("last_status", "never"),
            "last_message": s.get("last_message", ""),
        })
    rows.sort(key=lambda r: (r.get("local_path") or "", r.get("remote_path") or ""))
    return {
        "server_id": sid,
        "server_name": server.get("name") or server.get("ssh_host"),
        "server_host": server.get("ssh_host"),
        "packaged_app": bool(get_packaged_app_executable()),
        "syncs": rows,
        "folder_history": get_folder_history(limit=10, server_id=sid),
    }


def record_folder_history(server_id, local_path, remote_path):
    """Remembers used folder pairs for quick-select (max 20 per server)."""
    try:
        cfg = load_config()
        server = resolve_server(cfg, server_id)
        sid = server.get("id")
        local = (local_path or "").strip()
        remote = (remote_path or "").strip()
        if not local and not remote:
            return
        history = cfg.setdefault("folder_history", [])
        kept = []
        for h in history:
            if not isinstance(h, dict):
                continue
            if (h.get("server_id") or sid) == sid and (h.get("local_path") or "") == local and (h.get("remote_path") or "") == remote:
                continue
            kept.append(h)
        kept.insert(0, {"server_id": sid, "local_path": local, "remote_path": remote, "last_used": time.time()})
        per_counts = {}
        capped = []
        for h in kept:
            key = h.get("server_id") or sid
            per_counts[key] = per_counts.get(key, 0) + 1
            if per_counts[key] <= 20:
                capped.append(h)
        cfg["folder_history"] = capped[:100]
        save_config(cfg)
    except Exception:
        pass


def get_folder_history(limit=10, server_id=None):
    """Most-recently-used folder pairs for a server (for quick-select chips)."""
    try:
        cfg = load_config()
        server = resolve_server(cfg, server_id)
        sid = server.get("id")
        out = [h for h in cfg.get("folder_history", []) if (h.get("server_id") or sid) == sid]
        return out[:limit]
    except Exception:
        return []


def browse_local(path=""):
    """Lists local directories for the folder picker. Returns dict with entries."""
    raw = (path or "").strip() or os.path.expanduser("~")
    raw = os.path.expanduser(raw)
    if not os.path.isabs(raw):
        raw = os.path.join(os.path.expanduser("~"), raw)
    target = os.path.normpath(raw)
    home = os.path.expanduser("~")
    if not os.path.exists(target):
        return {"ok": False, "path": target, "parent": os.path.dirname(target),
                "home": home, "entries": [], "message": f"Path does not exist: {target}"}
    if not os.path.isdir(target):
        target = os.path.dirname(target)
    try:
        names = sorted(os.listdir(target), key=lambda n: n.lower())
    except OSError as e:
        # Errno 1 (EPERM) / 13 (EACCES) on ~/Documents, ~/Desktop, ... is
        # macOS TCC privacy protection, not a missing folder: the dashboard
        # process needs Full Disk Access (see README + dashboard help).
        if getattr(e, "errno", None) in (1, 13):
            return {"ok": False, "denied": True, "path": target,
                    "parent": os.path.dirname(target),
                    "home": home, "entries": [],
                    "message": f"macOS blocked access to {target} (privacy protection)"}
        return {"ok": False, "path": target, "parent": os.path.dirname(target),
                "home": home, "entries": [], "message": f"Cannot list {target}: {e}"}
    entries = []
    for name in names:
        full = os.path.join(target, name)
        try:
            if os.path.isdir(full):
                entries.append({"name": name, "path": full,
                                "hidden": name.startswith(".")})
        except OSError:
            continue
    parent = os.path.dirname(target.rstrip("/")) or "/"
    # Root's parent is itself; avoid empty.
    if not parent:
        parent = "/"
    return {"ok": True, "path": target, "parent": parent, "home": home, "entries": entries}


def browse_remote(server_ref=None, path=""):
    """Lists remote directories over SSH for the folder picker."""
    cfg = load_config()
    server = resolve_server(cfg, server_ref)
    ssh_host = server.get("ssh_host")
    want = (path or "").strip() or "~"
    # Resolve ~ to absolute once so parent navigation works with posixpath.
    if want in ("~", "~/", "$HOME"):
        probe = _ssh_remote_cmd(ssh_host, "pwd && echo OK")
        if probe.returncode == 0:
            lines = [l for l in (probe.stdout or "").splitlines() if l.strip()]
            if lines:
                want = lines[0].strip()
            else:
                want = "~"
        else:
            return {"ok": False, "path": want, "parent": "~", "entries": [],
                    "message": f"Cannot reach {ssh_host} (check SSH keys)"}
    if want.startswith("~"):
        # ~/sub -> ask remote shell to expand (keep display absolute when possible)
        probe = _ssh_remote_cmd(ssh_host, f"cd -- {shlex.quote(want)} 2>/dev/null && pwd")
        if probe.returncode == 0 and (probe.stdout or "").strip():
            want = (probe.stdout or "").strip().splitlines()[0].strip()
    # List with ls -1pa -a so hidden/dot folders (.output, .git, ...) are
    # visible too (dirs end with /). Quote for the remote shell.
    res = _ssh_remote_cmd(ssh_host, f"ls -1pa -- {shlex.quote(want)} 2>&1")
    out = (res.stdout or "") + (res.stderr or "")
    if res.returncode != 0:
        # ls writes the error to stdout/stderr; surface the last line.
        lines = [l for l in out.splitlines() if l.strip()]
        msg = lines[-1] if lines else f"Cannot list {want}"
        return {"ok": False, "path": want, "parent": posixpath.dirname(want.rstrip("/")) or "/",
                "entries": [], "message": msg[:300]}
    entries = []
    for line in (res.stdout or "").splitlines():
        name = line.rstrip("\n")
        if not name or name in ("./", "../"):
            continue
        is_dir = name.endswith("/")
        name = name[:-1] if is_dir else name
        if name in (".", "..") or not name:
            continue
        if is_dir:
            full = posixpath.join(want.rstrip("/"), name)
            entries.append({"name": name, "path": full,
                            "hidden": name.startswith(".")})
    entries.sort(key=lambda e: e["name"].lower())
    parent = posixpath.dirname(want.rstrip("/")) or "/"
    return {"ok": True, "path": want, "parent": parent, "entries": entries}


def _validate_new_folder_name(name):
    """Validates a new folder name. Returns stripped name or raises ValueError."""
    clean = (name or "").strip().strip("'\"")
    if not clean or clean in (".", ".."):
        raise ValueError("Enter a folder name")
    if len(clean) > 255:
        raise ValueError("Folder name is too long")
    if "/" in clean or "\\" in clean or "\x00" in clean:
        raise ValueError(f"Invalid folder name: {clean!r} (no slashes)")
    return clean


def mkdir_local(parent="", name=""):
    """Creates a local folder for the picker. Returns {ok, path, ...}."""
    clean = _validate_new_folder_name(name)
    base = (parent or "").strip() or "~"
    base = os.path.expanduser(base)
    if not os.path.isabs(base):
        base = os.path.join(os.path.expanduser("~"), base)
    new = os.path.normpath(os.path.join(base, clean))
    try:
        if os.path.isdir(new):
            return {"ok": True, "path": new, "message": "Folder already exists"}
        os.makedirs(new, exist_ok=True)
    except OSError as e:
        if getattr(e, "errno", None) in (1, 13):
            return {"ok": False, "denied": True, "path": new,
                    "message": f"macOS blocked creating {new} (privacy protection)"}
        return {"ok": False, "path": new, "message": f"Cannot create {new}: {e}"}
    return {"ok": True, "path": new, "message": f"Created {new}"}


def mkdir_remote(server_ref=None, parent="", name=""):
    """Creates a remote folder over SSH for the picker. Returns {ok, path, ...}."""
    clean = _validate_new_folder_name(name)
    cfg = load_config()
    server = resolve_server(cfg, server_ref)
    ssh_host = server.get("ssh_host")
    base = (parent or "").strip() or "~"
    if base.startswith("~") or base in ("$HOME", "") or not base.startswith("/"):
        home_probe = _ssh_remote_cmd(ssh_host, "pwd")
        if home_probe.returncode != 0:
            return {"ok": False, "path": base,
                    "message": f"Cannot reach {ssh_host} (check SSH keys)"}
        home = (home_probe.stdout or "").strip().splitlines()[0].strip()
        if base in ("~", "~/", "$HOME", ""):
            base = home
        elif base.startswith("~/"):
            base = posixpath.join(home, base[2:])
        else:
            base = posixpath.join(home, base)
    new = posixpath.normpath(posixpath.join(base, clean))
    res = _ssh_remote_cmd(ssh_host, f"mkdir -p -- {shlex.quote(new)} 2>&1")
    if res.returncode != 0:
        lines = [l for l in ((res.stdout or "") + (res.stderr or "")).splitlines() if l.strip()]
        return {"ok": False, "path": new,
                "message": (lines[-1] if lines else f"Cannot create {new}")[:300]}
    return {"ok": True, "path": new, "message": f"Created {ssh_host}:{new}"}


# -----------------------------
# ~/.ssh/config discovery
# -----------------------------

def parse_ssh_config_file(path):
    """Parses an OpenSSH config file into {alias: {hostname, user, port}}.

    Handles `Host` blocks with multiple patterns and `Include` directives.
    Wildcard patterns (* ? !) are skipped — they are not connectable tabs.
    """
    results = {}
    order = []

    def _parse_file(file_path, depth=0):
        if depth > 5:
            return
        try:
            with open(os.path.expanduser(file_path), "r", encoding="utf-8", errors="ignore") as f:
                lines = f.readlines()
        except OSError:
            return
        base_dir = os.path.dirname(os.path.expanduser(file_path))
        current_aliases = []
        current_opts = {}
        pending_aliases = None

        def _flush():
            if pending_aliases is None:
                return
            for alias in pending_aliases:
                if alias not in results:
                    results[alias] = {}
                    order.append(alias)
                for k, v in current_opts.items():
                    results[alias].setdefault(k, v)

        for raw in lines:
            # Strip comments (naive: cut at first #)
            line = raw.split("#", 1)[0].strip()
            if not line:
                continue
            if "=" in line:
                # Support `Key=Value` style (e.g. HostName=example.com)
                key, _, value = line.partition("=")
                key, value = key.strip(), value.strip()
            else:
                parts = line.split(None, 1)
                if len(parts) < 2:
                    continue
                key, value = parts[0].strip(), parts[1].strip()
            key_low = key.lower()
            if key_low == "host":
                _flush()
                patterns = value.split()
                # A `Host` line opens a new block; concrete aliases become pending
                pending_aliases = [p for p in patterns if p and "*" not in p and "?" not in p and "!" not in p]
                current_aliases = pending_aliases
                current_opts = {}
                if not pending_aliases:
                    pending_aliases = []  # wildcard-only block: parse but don't record
            elif key_low == "include":
                _flush()
                pending_aliases = None
                current_opts = {}
                for pat in value.split():
                    # Strip optional quotes (Include "~/.ssh/config.d/*") and
                    # expand ~ BEFORE the isabs check — os.path.isabs("~/.ssh/..")
                    # is False, so checking first would wrongly join it onto base_dir
                    # and break forms like `Include ~/.ssh/config.d/xyz`.
                    pat = pat.strip().strip("'\"")
                    if not pat:
                        continue
                    pat = os.path.expanduser(pat)
                    full = pat if os.path.isabs(pat) else os.path.join(base_dir, pat)
                    for expanded in sorted(glob.glob(full)):
                        _parse_file(expanded, depth + 1)
            elif key_low in ("hostname", "user", "port"):
                if pending_aliases:
                    current_opts[key_low] = value
            # Other keys ignored for tab suggestions
        _flush()

    _parse_file(path)
    hosts = []
    for alias in order:
        opts = results.get(alias, {})
        try:
            port = int(str(opts.get("port", 22)))
        except ValueError:
            port = 22
        hosts.append({
            "ssh_host": alias,
            "hostname": opts.get("hostname", ""),
            "user": opts.get("user", ""),
            "port": port,
        })
    return hosts


def get_ssh_config_hosts():
    """Lists connectable Host entries from ~/.ssh/config (empty list if missing)."""
    try:
        if not os.path.exists(os.path.expanduser(SSH_CONFIG_PATH)):
            return []
        return parse_ssh_config_file(SSH_CONFIG_PATH)
    except Exception:
        return []


def record_port_history(local_port, remote_port, label="", server_id=None):
    """Appends/updates the recently-used port history (most recent first, max 20 per server)."""
    try:
        cfg = load_config()
        server = resolve_server(cfg, server_id)
        sid = server.get("id")
        history = cfg.setdefault("history", [])
        # Remove existing entry for the same server+local port to re-insert at front
        kept = []
        for h in history:
            h_sid = h.get("server_id") or get_legacy_server(cfg).get("id")
            if h_sid == sid and int(h.get("local_port", -1)) == int(local_port):
                continue
            kept.append(h)
        kept.insert(0, {
            "server_id": sid,
            "local_port": int(local_port),
            "remote_port": int(remote_port),
            "label": label or DEFAULT_LABELS.get(int(local_port), f"Port {local_port}"),
            "last_used": time.time(),
        })
        # Cap at 20 per server (and 100 total safety)
        per_server_counts = {}
        capped = []
        for h in kept:
            key = h.get("server_id") or sid
            per_server_counts[key] = per_server_counts.get(key, 0) + 1
            if per_server_counts[key] <= 20:
                capped.append(h)
        cfg["history"] = capped[:100]
        save_config(cfg)
    except Exception:
        pass


def get_port_history(limit=8, exclude_ports=None, server_id=None):
    """Returns most-recently-used ports for a server, optionally excluding configured ones."""
    try:
        cfg = load_config()
        server = resolve_server(cfg, server_id)
        sid = server.get("id")
        history = cfg.get("history", [])
        excluded = set(exclude_ports or [])
        result = [h for h in history
                  if (h.get("server_id") or get_legacy_server(cfg).get("id")) == sid
                  and int(h.get("local_port", -1)) not in excluded]
        # Legacy entries without server_id belong to the default server (already tagged on migration)
        return result[:limit]
    except Exception:
        return []


def get_plist_label(port, server_id=None):
    # Legacy single-arg format preserved for the default server (backward compat with
    # existing tests and already-installed LaunchAgents): "<prefix>-<port>".
    # Additional servers are namespaced: "<prefix>-<server_id>-<port>" to avoid
    # filename collisions when two hosts forward the same local port.
    if server_id is None:
        return f"{AGENT_PREFIX}-{port}"
    try:
        cfg = load_config()
        legacy_id = get_legacy_server(cfg).get("id")
    except Exception:
        legacy_id = None
    if legacy_id is not None and server_id == legacy_id:
        # Keep legacy filename for the bootstrap server so existing agents survive
        # pinning/reordering tabs. Additional servers are namespaced below.
        return f"{AGENT_PREFIX}-{port}"
    safe_sid = re.sub(r"[^A-Za-z0-9_-]+", "-", str(server_id)) or "server"
    return f"{AGENT_PREFIX}-{safe_sid}-{port}"


def get_plist_path(port, server_id=None):
    return os.path.join(LAUNCH_AGENTS_DIR, f"{get_plist_label(port, server_id)}.plist")


def _infer_server_id_for_plist(data, ssh_dest, port):
    """Best-effort attribution of a LaunchAgent plist to a server id."""
    try:
        cfg = load_config()
        servers = cfg.get("servers", [])
    except Exception:
        servers = []
    # Primary: match ssh destination arg to a known server
    if ssh_dest:
        for srv in servers:
            if ssh_dest == srv.get("ssh_host") or (srv.get("ip") and ssh_dest == srv.get("ip")):
                return srv.get("id")
    # Secondary: parse namespaced label "<prefix>-<server_id>-<port>"
    try:
        label = data.get("Label", "")
        prefix = AGENT_PREFIX + "-"
        if label.startswith(prefix):
            rest = label[len(prefix):]
            # rest is either "<port>" (legacy default) or "<server_id>-<port>"
            if "-" in rest:
                maybe_sid, _, maybe_port = rest.rpartition("-")
                if maybe_port == str(port) and any(s.get("id") == maybe_sid for s in servers):
                    return maybe_sid
    except Exception:
        pass
    return None


def is_server_reachable(ssh_host=None, server=None):
    target = None
    if isinstance(server, dict):
        target = server.get("ssh_host")
    elif ssh_host:
        target = ssh_host
    else:
        target = SSH_HOST
    try:
        res = subprocess.run(
            ["ssh", "-o", "BatchMode=yes", "-o", "ConnectTimeout=2", target, "true"],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        return res.returncode == 0
    except Exception:
        return False


def get_listening_ports():
    """Returns dict of {port: {'pid': int, 'cmd': str, 'node': str}}."""
    ports = {}
    try:
        res = subprocess.run(["lsof", "-nP", "-iTCP", "-sTCP:LISTEN"], capture_output=True, text=True)
        for line in res.stdout.splitlines():
            parts = line.split()
            if len(parts) >= 9:
                cmd, pid, node = parts[0], parts[1], parts[8]
                m = re.search(r":(\d+)$", node)
                if m:
                    p = int(m.group(1))
                    ports[p] = {"cmd": cmd, "pid": int(pid), "node": node}
    except Exception:
        pass
    return ports


def _extract_ssh_destination(cmdline):
    """Best-effort extraction of the ssh destination host (last bare arg)."""
    try:
        tokens = cmdline.strip().split()
        # Skip argv[0] pid in ps output? ps -A -o pid,command includes pid first.
        # Walk backwards for first token not starting with '-' and not a value of known flags.
        skip_prev = {"-L", "-o", "-i", "-F", "-p", "-l"}
        prev = None
        for tok in reversed(tokens):
            if tok.startswith("-"):
                prev = tok
                continue
            if prev in skip_prev:
                prev = None
                continue
            # Skip -L values and host:port fragments
            if ":" in tok and re.search(r"\d+:\d+", tok):
                prev = None
                continue
            if tok in ("ssh", get_tunnel_executable(), "/usr/bin/ssh"):
                prev = None
                continue
            # First plausible bare host token from the end is the destination
            if re.match(r"^[\w.@-]+$", tok):
                return tok
            prev = None
    except Exception:
        pass
    return ""


def get_ssh_forwards(server=None, server_id=None):
    """Parses ps output to find SSH forwards, optionally filtered to one server.

    Without a server filter returns all DevBoost-like forwards (backward compat).
    With a server, matches destination host or configured IP.
    """
    target_hosts = set()
    if isinstance(server, dict):
        if server.get("ssh_host"):
            target_hosts.add(server["ssh_host"])
        if server.get("ip"):
            target_hosts.add(server["ip"])
    elif server_id:
        try:
            cfg = load_config()
            srv = get_server(cfg, server_id)
            if srv:
                if srv.get("ssh_host"):
                    target_hosts.add(srv["ssh_host"])
                if srv.get("ip"):
                    target_hosts.add(srv["ip"])
        except Exception:
            pass
    forwards = []
    listening_ports = get_listening_ports()
    try:
        res = subprocess.run(["ps", "-A", "-o", "pid,command"], capture_output=True, text=True)
        for line in res.stdout.splitlines():
            if "ssh" in line and "-L" in line:
                dest = _extract_ssh_destination(line)
                if target_hosts:
                    if dest not in target_hosts and not any(h in line for h in target_hosts):
                        continue
                else:
                    # Unfiltered (legacy): keep previous broad behavior so old
                    # installs/tests still see their tunnels.
                    if not (SSH_HOST in line or (SERVER_IP and SERVER_IP in line) or "localhost" in line or "127.0.0.1" in line):
                        continue
                m = re.search(r"-L\s+(?:127\.0\.0\.1:|localhost:)?(\d+):(?:127\.0\.0\.1|localhost)?:?(\d+)", line)
                if m:
                    local_p = int(m.group(1))
                    remote_p = int(m.group(2))
                    try:
                        pid = int(line.strip().split()[0])
                    except ValueError:
                        continue
                    is_listen = (local_p in listening_ports and listening_ports[local_p]["pid"] == pid)
                    forwards.append({
                        "pid": pid,
                        "local_port": local_p,
                        "remote_port": remote_p,
                        "is_listening": is_listen,
                        "cmd": line.strip(),
                        "ssh_dest": dest,
                    })
    except Exception:
        pass
    return forwards


def get_launchagents(server_id=None):
    """Finds all LaunchAgents for port forwarding (both current prefix and legacy formats).

    Returns {port: agent} for backward compat when server_id is None and only one
    server exists; otherwise returns {(server_id, port): agent} flattened per server.
    For multi-server callers use get_launchagents_for_server(server).
    """
    # Full discovery with server attribution
    by_server_port = {}
    patterns = [
        os.path.join(LAUNCH_AGENTS_DIR, f"{AGENT_PREFIX}-*.plist"),
        os.path.join(LAUNCH_AGENTS_DIR, "com.*.ssh-forward-*.plist"),
        os.path.join(LAUNCH_AGENTS_DIR, "*ssh-forward-*.plist"),
    ]
    seen_paths = set()
    for pat in patterns:
        for plist_path in glob.glob(pat):
            if plist_path in seen_paths:
                continue
            seen_paths.add(plist_path)
            m = re.search(r"-(\d+)\.plist$", plist_path)
            if m:
                port = int(m.group(1))
                try:
                    with open(plist_path, "rb") as f:
                        data = plistlib.load(f)
                        remote_port = port
                        ssh_dest = ""
                        args = data.get("ProgramArguments", [])
                        for i, arg in enumerate(args):
                            if arg == "-L" and i + 1 < len(args):
                                lm = re.search(r"(\d+):(?:127\.0\.0\.1|localhost)?:?(\d+)", args[i+1])
                                if lm:
                                    remote_port = int(lm.group(2))
                        if args:
                            ssh_dest = args[-1] if args[-1] and not args[-1].startswith("-") else ""
                        sid = _infer_server_id_for_plist(data, ssh_dest, port)
                        by_server_port.setdefault((sid, port), {
                            "port": port,
                            "remote_port": remote_port,
                            "label": data.get("Label", get_plist_label(port)),
                            "path": plist_path,
                            "ssh_dest": ssh_dest,
                            "server_id": sid,
                        })
                except Exception:
                    by_server_port.setdefault((None, port), {
                        "port": port,
                        "remote_port": port,
                        "label": get_plist_label(port),
                        "path": plist_path,
                        "ssh_dest": "",
                        "server_id": None,
                    })
    if server_id is not None:
        return {port: agent for (sid, port), agent in by_server_port.items() if sid == server_id}
    # Backward compat: legacy callers expect {port: agent}. If only the default
    # server exists (or attribution failed -> None), flatten None/default entries.
    try:
        default_id = get_legacy_server().get("id")
    except Exception:
        default_id = None
    try:
        cfg_servers = load_config().get("servers", [])
    except Exception:
        cfg_servers = []
    if len(cfg_servers) <= 1:
        flat = {}
        for (sid, port), agent in by_server_port.items():
            if sid is None or sid == default_id:
                flat.setdefault(port, agent)
        # Also surface namespaced entries whose sid didn't resolve (e.g. tests
        # creating mock plists with a foreign host) so legacy flows keep working.
        for (sid, port), agent in by_server_port.items():
            flat.setdefault(port, agent)
        return flat
    # Multi-server without filter: return only unattributed/default flattened to
    # avoid silently mixing hosts; per-server callers should pass server_id.
    flat = {}
    for (sid, port), agent in by_server_port.items():
        if sid is None or sid == default_id:
            flat.setdefault(port, agent)
    return flat


def get_launchagents_for_server(server):
    """LaunchAgents belonging to one server: {port: agent}."""
    sid = server.get("id") if isinstance(server, dict) else server
    # Attributed discovery filtered by sid (handles legacy None attribution)
    result = {}
    try:
        cfg = load_config()
    except Exception:
        cfg = {"servers": []}
    # Direct scan to avoid flattening ambiguity
    patterns = [os.path.join(LAUNCH_AGENTS_DIR, f"{AGENT_PREFIX}-*.plist")]
    for plist_path in glob.glob(patterns[0]):
        m = re.search(r"-(\d+)\.plist$", plist_path)
        if not m:
            continue
        port = int(m.group(1))
        try:
            with open(plist_path, "rb") as f:
                data = plistlib.load(f)
        except Exception:
            continue
        args = data.get("ProgramArguments", [])
        ssh_dest = args[-1] if args and args[-1] and not args[-1].startswith("-") else ""
        inferred = _infer_server_id_for_plist(data, ssh_dest, port)
        # Attribute: explicit match, or legacy unattributed plist whose ssh_dest
        # matches this server, or legacy default-server filename for default server.
        is_legacy_name = os.path.basename(plist_path) == f"{get_plist_label(port)}.plist"
        default_id = None
        try:
            default_id = get_legacy_server(cfg).get("id")
        except Exception:
            pass
        match = False
        if inferred == sid:
            match = True
        elif inferred is None:
            if isinstance(server, dict):
                if ssh_dest in (server.get("ssh_host"), server.get("ip")):
                    match = True
                elif is_legacy_name and sid == default_id:
                    match = True
                elif is_legacy_name and sid is None:
                    match = True
        if match:
            remote_port = port
            for i, arg in enumerate(args):
                if arg == "-L" and i + 1 < len(args):
                    lm = re.search(r"(\d+):(?:127\.0\.0\.1|localhost)?:?(\d+)", args[i+1])
                    if lm:
                        remote_port = int(lm.group(2))
            result[port] = {"port": port, "remote_port": remote_port,
                            "label": data.get("Label", ""), "path": plist_path,
                            "ssh_dest": ssh_dest, "server_id": sid}
    # Fallback: include anything the generic scan attributed to this sid
    for (asid, aport), agent in _scan_all_agents_raw().items():
        if asid == sid:
            result.setdefault(aport, agent)
    return result


def _scan_all_agents_raw():
    """Raw {(server_id, port): agent} scan used by per-server resolution."""
    out = {}
    patterns = [
        os.path.join(LAUNCH_AGENTS_DIR, f"{AGENT_PREFIX}-*.plist"),
        os.path.join(LAUNCH_AGENTS_DIR, "com.*.ssh-forward-*.plist"),
        os.path.join(LAUNCH_AGENTS_DIR, "*ssh-forward-*.plist"),
    ]
    seen = set()
    for pat in patterns:
        for plist_path in glob.glob(pat):
            if plist_path in seen:
                continue
            seen.add(plist_path)
            m = re.search(r"-(\d+)\.plist$", plist_path)
            if not m:
                continue
            port = int(m.group(1))
            try:
                with open(plist_path, "rb") as f:
                    data = plistlib.load(f)
                args = data.get("ProgramArguments", [])
                ssh_dest = args[-1] if args and args[-1] and not args[-1].startswith("-") else ""
                remote_port = port
                for i, arg in enumerate(args):
                    if arg == "-L" and i + 1 < len(args):
                        lm = re.search(r"(\d+):(?:127\.0\.0\.1|localhost)?:?(\d+)", args[i+1])
                        if lm:
                            remote_port = int(lm.group(2))
                sid = _infer_server_id_for_plist(data, ssh_dest, port)
                out.setdefault((sid, port), {"port": port, "remote_port": remote_port,
                                             "label": data.get("Label", ""), "path": plist_path,
                                             "ssh_dest": ssh_dest, "server_id": sid})
            except Exception:
                continue
    return out


def get_all_forwards_status(server_ref=None):
    cfg = load_config()
    server = resolve_server(cfg, server_ref)
    sid = server.get("id")
    ssh_procs = get_ssh_forwards(server=server)
    agents = get_launchagents_for_server(server)

    all_ports = set()
    all_ports.update(agents.keys())
    for f in ssh_procs:
        all_ports.add(f["local_port"])
    for p_str in get_server_labels(cfg, sid).keys():
        try:
            all_ports.add(int(p_str))
        except ValueError:
            pass

    proc_map = {}
    for proc in ssh_procs:
        lp = proc["local_port"]
        proc_map.setdefault(lp, []).append(proc)

    # Cross-server local-port conflict detection: same local port bound by another host.
    # Maps port -> owner info so the UI can say *which* connection holds the port.
    conflicting_owners = {}
    try:
        all_procs = get_ssh_forwards()
        own_dests = {d for d in (server.get("ssh_host"), server.get("ip")) if d}
        # Known servers by ssh_host/ip for attribution
        dest_to_server = {}
        for srv in cfg.get("servers", []):
            if srv.get("id") == sid:
                continue
            for key in (srv.get("ssh_host"), srv.get("ip")):
                if key:
                    dest_to_server.setdefault(key, srv)
        for p in all_procs:
            if not p.get("is_listening"):
                continue
            dest = p.get("ssh_dest", "") or ""
            if dest and dest in own_dests:
                continue
            if not dest and own_dests and any(h in (p.get("cmd") or "") for h in own_dests):
                continue
            port = p["local_port"]
            if port in conflicting_owners:
                continue
            owner = dest_to_server.get(dest)
            if owner is not None:
                conflicting_owners[port] = {
                    "server_id": owner.get("id"),
                    "name": owner.get("name") or owner.get("ssh_host"),
                    "ssh_host": owner.get("ssh_host"),
                }
            elif dest:
                # Listening proc for an unknown host — still report the raw destination
                conflicting_owners[port] = {"server_id": None, "name": dest, "ssh_host": dest}
            # else: unattributed listener (no dest parsed) — leave unclaimed
    except Exception:
        pass

    results = []
    orphaned_count = 0

    for port in sorted(all_ports):
        is_always = port in agents
        procs = proc_map.get(port, [])
        active_proc = next((p for p in procs if p["is_listening"]), None)
        orphans = [p["pid"] for p in procs if not p["is_listening"]]
        orphaned_count += len(orphans)

        remote_port = port
        if is_always:
            remote_port = agents[port]["remote_port"]
        elif procs:
            remote_port = procs[0]["remote_port"]

        label = get_server_labels(cfg, sid).get(str(port))
        if not label:
            label = DEFAULT_LABELS.get(port, f"Port {port}")

        is_active = active_proc is not None
        pid = active_proc["pid"] if active_proc else None

        owner = conflicting_owners.get(port)
        results.append({
            "local_port": port,
            "remote_port": remote_port,
            "label": label,
            "always": is_always,
            "active": is_active,
            "pid": pid,
            "orphans": orphans,
            "plist_label": agents[port]["label"] if is_always else None,
            "conflict": owner is not None and not is_active,
            "conflict_with": owner if (owner is not None and not is_active) else None,
        })

    return {
        "server_id": sid,
        "server_name": server.get("name") or server.get("ssh_host"),
        "server_host": server.get("ssh_host"),
        "server_ip": server.get("ip", ""),
        "server_reachable": is_server_reachable(server=server),
        "forwards": results,
        "orphaned_count": orphaned_count,
        "history": get_port_history(limit=10, exclude_ports=all_ports, server_id=sid),
    }


def get_servers_status():
    """Lightweight per-tab summary for the tab bar (no per-port detail)."""
    cfg = load_config()
    servers = get_servers(cfg)
    sync_agents = {}
    try:
        sync_agents = _scan_sync_agents_raw()
    except Exception:
        sync_agents = {}
    try:
        sync_server_ids = {}
        for s in cfg.get("syncs", []):
            if isinstance(s, dict) and s.get("server_id"):
                sync_server_ids.setdefault(s["server_id"], []).append(s)
    except Exception:
        sync_server_ids = {}
    out = []
    for srv in servers:
        try:
            agents = get_launchagents_for_server(srv)
            procs = get_ssh_forwards(server=srv)
            active = sum(1 for p in procs if p.get("is_listening"))
            srv_syncs = sync_server_ids.get(srv.get("id"), [])
            auto_syncs = sum(1 for s in srv_syncs if s.get("always") and s.get("id") in sync_agents)
            out.append({
                "id": srv.get("id"),
                "ssh_host": srv.get("ssh_host"),
                "name": srv.get("name") or srv.get("ssh_host"),
                "ip": srv.get("ip", ""),
                "order": srv.get("order", 0),
                "pinned": bool(srv.get("pinned", False)),
                "always_count": len(agents),
                "active_count": active,
                "sync_count": len(srv_syncs),
                "auto_sync_count": auto_syncs,
            })
        except Exception:
            out.append({
                "id": srv.get("id"),
                "ssh_host": srv.get("ssh_host"),
                "name": srv.get("name") or srv.get("ssh_host"),
                "ip": srv.get("ip", ""),
                "order": srv.get("order", 0),
                "pinned": bool(srv.get("pinned", False)),
                "always_count": 0,
                "active_count": 0,
                "sync_count": 0,
                "auto_sync_count": 0,
            })
    return out


def create_launchagent(local_port, remote_port, server_ref=None):
    cfg = load_config()
    server = resolve_server(cfg, server_ref)
    sid = server.get("id")
    ssh_host = server.get("ssh_host")
    plist_path = get_plist_path(local_port, sid)
    label = get_plist_label(local_port, sid)
    # Remove any legacy-named agent for the same port+server to avoid duplicates
    # after migrating to namespaced filenames.
    try:
        legacy_path = os.path.join(LAUNCH_AGENTS_DIR, f"{AGENT_PREFIX}-{local_port}.plist")
        if legacy_path != plist_path and os.path.exists(legacy_path):
            try:
                with open(legacy_path, "rb") as f:
                    legacy_data = plistlib.load(f)
                legacy_args = legacy_data.get("ProgramArguments", [])
                legacy_dest = legacy_args[-1] if legacy_args else ""
                if legacy_dest == ssh_host and sid != get_legacy_server(cfg).get("id"):
                    pass  # namespaced server: keep legacy only if it belongs elsewhere
                elif legacy_dest == ssh_host:
                    subprocess.run(["launchctl", "unload", "-w", legacy_path],
                                   stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
                    os.remove(legacy_path)
            except OSError:
                pass
    except Exception:
        pass
    data = {
        "Label": label,
        "ProgramArguments": (
            [get_tunnel_executable()] +
            (["--tunnel"] if get_packaged_app_executable() else []) + [
            "-N",
            "-T",
            "-o", "ServerAliveInterval=15",
            "-o", "ServerAliveCountMax=3",
            "-o", "ExitOnForwardFailure=yes",
            "-o", "BatchMode=yes",
            "-L", f"{local_port}:127.0.0.1:{remote_port}",
            ssh_host
            ]
        ),
        "RunAtLoad": True,
        "KeepAlive": True,
        "ThrottleInterval": 10,
        "StandardOutPath": os.path.join(LOG_DIR, f"devboost-forward-{sid}-{local_port}.log"),
        "StandardErrorPath": os.path.join(LOG_DIR, f"devboost-forward-{sid}-{local_port}.err"),
    }
    with open(plist_path, "wb") as f:
        plistlib.dump(data, f)
    subprocess.run(["launchctl", "load", "-w", plist_path], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    return plist_path


def remove_launchagent(local_port, server_ref=None):
    if server_ref is None:
        # Legacy behavior: remove by port across the default view (keeps old tests green)
        agents = get_launchagents()
        if local_port in agents:
            plist_path = agents[local_port]["path"]
            subprocess.run(["launchctl", "unload", "-w", plist_path], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            try:
                os.remove(plist_path)
            except OSError:
                pass
        return
    cfg = load_config()
    server = resolve_server(cfg, server_ref)
    agents = get_launchagents_for_server(server)
    if local_port in agents:
        plist_path = agents[local_port]["path"]
        subprocess.run(["launchctl", "unload", "-w", plist_path], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        try:
            os.remove(plist_path)
        except OSError:
            pass


def remove_launchagents_for_server(server):
    agents = get_launchagents_for_server(server)
    for port, agent in agents.items():
        try:
            subprocess.run(["launchctl", "unload", "-w", agent["path"]],
                           stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            os.remove(agent["path"])
        except OSError:
            pass


def kill_port_processes(local_port, server_ref=None):
    if server_ref is None:
        ssh_procs = get_ssh_forwards()
    else:
        cfg = load_config()
        ssh_procs = get_ssh_forwards(server=resolve_server(cfg, server_ref))
    killed = 0
    for p in ssh_procs:
        if p["local_port"] == local_port:
            try:
                os.kill(p["pid"], signal.SIGKILL)
                killed += 1
            except OSError:
                pass
    return killed


def kill_server_processes(server):
    procs = get_ssh_forwards(server=server if isinstance(server, dict) else None,
                             server_id=None if isinstance(server, dict) else server)
    killed = 0
    for p in procs:
        try:
            os.kill(p["pid"], signal.SIGKILL)
            killed += 1
        except OSError:
            pass
    return killed


def add_forward(local_port, remote_port=None, label="", always=False, server_ref=None):
    if remote_port is None:
        remote_port = local_port
    cfg = load_config()
    server = resolve_server(cfg, server_ref)
    sid = server.get("id")
    ssh_host = server.get("ssh_host")
    existing_labels = get_server_labels(cfg, sid)
    if label:
        set_server_label(cfg, sid, local_port, label)
    elif str(local_port) not in existing_labels:
        set_server_label(cfg, sid, local_port, DEFAULT_LABELS.get(int(local_port), f"Port {local_port}"))
    save_config(cfg)
    record_port_history(local_port, remote_port,
                        label=get_server_labels(load_config(), sid).get(str(local_port), ""),
                        server_id=sid)

    kill_port_processes(local_port, server_ref=sid)

    if always:
        create_launchagent(local_port, remote_port, server_ref=sid)
    else:
        remove_launchagent(local_port, server_ref=sid)
        cmd = [
            "/usr/bin/ssh",
            "-fN",
            "-o", "ServerAliveInterval=15",
            "-o", "ServerAliveCountMax=3",
            "-o", "ExitOnForwardFailure=yes",
            "-o", "BatchMode=yes",
            "-L", f"{local_port}:127.0.0.1:{remote_port}",
            ssh_host
        ]
        subprocess.run(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)


def remove_forward(local_port, server_ref=None):
    cfg = load_config()
    server = resolve_server(cfg, server_ref)
    sid = server.get("id")
    remove_launchagent(local_port, server_ref=sid)
    kill_port_processes(local_port, server_ref=sid)
    cfg = load_config()
    remove_server_label(cfg, sid, local_port)
    save_config(cfg)


def toggle_always(local_port, make_always, server_ref=None):
    cfg = load_config()
    server = resolve_server(cfg, server_ref)
    status = get_all_forwards_status(server.get("id"))
    current = next((f for f in status["forwards"] if f["local_port"] == local_port), None)
    remote_port = current["remote_port"] if current else local_port
    label = current["label"] if current else ""
    add_forward(local_port, remote_port, label=label, always=make_always, server_ref=server.get("id"))


def clean_orphaned_tunnels(server_ref=None):
    if server_ref:
        cfg = load_config()
        ssh_procs = get_ssh_forwards(server=resolve_server(cfg, server_ref))
    else:
        ssh_procs = get_ssh_forwards()
    killed = 0
    for p in ssh_procs:
        if not p["is_listening"]:
            try:
                os.kill(p["pid"], signal.SIGKILL)
                killed += 1
            except OSError:
                pass
    return killed


DOCKER_MONITOR = DockerMonitor(interval=5)


def get_docker_labels():
    cfg = load_config()
    return [dict(label) for label in cfg.get("docker_labels", []) if isinstance(label, dict)]


def apply_docker_labels(containers, labels):
    """Attach all matching case-insensitive name rules, preserving rule order."""
    for container in containers:
        name = str(container.get("name") or "").lower()
        matched = []
        for label in labels:
            needle = str(label.get("match") or "").strip().lower()
            if label.get("enabled", True) and needle and needle in name:
                matched.append({
                    "id": str(label.get("id") or label.get("name") or needle),
                    "name": str(label.get("name") or needle),
                    "color": str(label.get("color") or "#8b949e"),
                })
        container["labels"] = matched
    return containers


def get_docker_status(server_ref=None):
    """Returns the cached Docker snapshot for one configured server."""
    cfg = load_config()
    server = resolve_server(cfg, server_ref)
    snapshot = DOCKER_MONITOR.get(server.get("id"), server.get("ssh_host"))
    apply_docker_labels(snapshot.get("containers", []), get_docker_labels())
    snapshot.update({
        "server_id": server.get("id"),
        "server_name": server.get("name") or server.get("ssh_host"),
        "server_host": server.get("ssh_host"),
    })
    return snapshot


def get_docker_logs(server_ref, container, tail=200):
    cfg = load_config()
    server = resolve_server(cfg, server_ref)
    result = collect_docker_logs(server.get("ssh_host"), container, tail=tail)
    result.update({"server_id": server.get("id"), "container": container})
    return result


def scan_remote_services(server_ref=None):
    cfg = load_config() if server_ref else None
    if isinstance(server_ref, dict):
        ssh_host = server_ref.get("ssh_host")
    elif server_ref and cfg is not None:
        ssh_host = resolve_server(cfg, server_ref).get("ssh_host")
    else:
        ssh_host = SSH_HOST
    services = []
    try:
        res = subprocess.run(
            ["ssh", "-o", "BatchMode=yes", "-o", "ConnectTimeout=3", ssh_host, "ss -tlnp 2>/dev/null"],
            capture_output=True,
            text=True,
        )
        if res.returncode == 0:
            for line in res.stdout.splitlines():
                if "LISTEN" in line:
                    parts = line.split()
                    if len(parts) >= 4:
                        addr_port = parts[3]
                        m = re.search(r":(\d+)$", addr_port)
                        if m:
                            port = int(m.group(1))
                            if port in (22, 53, 54):
                                continue
                            proc = "Unknown"
                            proc_match = re.search(r'users:\(\("([^"]+)"', line)
                            if proc_match:
                                proc = proc_match.group(1)
                            label = DEFAULT_LABELS.get(port, proc)
                            services.append({
                                "port": port,
                                "address": addr_port,
                                "process": proc,
                                "label": label
                            })
    except Exception:
        pass
    return services


# -----------------------------
# Local listening ports (this Mac)
# -----------------------------

def get_local_listening_ports():
    """Listening sockets on this Mac, with DevBoost attribution.

    Returns [{port, pid, cmd, node, devboost}] sorted by port. devboost=True
    when the listener belongs to one of our SSH tunnels (Always or Session).
    """
    try:
        listening = get_listening_ports()
    except Exception:
        return []
    tunnel_pids = set()
    try:
        for f in get_ssh_forwards():
            if f.get("is_listening") and f.get("pid"):
                tunnel_pids.add(f.get("pid"))
    except Exception:
        pass
    rows = []
    for port in sorted(listening):
        info = listening.get(port) or {}
        if not isinstance(info, dict):
            continue
        rows.append({
            "port": port,
            "pid": info.get("pid"),
            "cmd": info.get("cmd", ""),
            "node": info.get("node", ""),
            "devboost": info.get("pid") in tunnel_pids,
        })
    return rows


def _pid_is_dead(pid):
    """True when pid is gone (reaps our own zombie children for accuracy)."""
    try:
        os.kill(pid, 0)
    except OSError:
        return True
    # A zombie still answers kill(pid, 0) — reap it if it's our child.
    try:
        done, _ = os.waitpid(pid, os.WNOHANG)
        if done == pid:
            return True
    except (ChildProcessError, OSError):
        pass
    return False


def kill_listening_process(pid):
    """Stops a process holding a local listening socket. Never raises.

    Tries SIGTERM first, escalates to SIGKILL. Refuses pid<=1, our own
    dashboard process, and PIDs that don't currently hold a listening socket
    (stale clicks). Returns {"ok", "message"}.
    """
    try:
        pid = int(pid)
    except (TypeError, ValueError):
        return {"ok": False, "message": f"Invalid pid: {pid!r}"}
    if pid <= 1:
        return {"ok": False, "message": "Refusing to kill pid 1"}
    if pid == os.getpid():
        return {"ok": False, "message": "Refusing to kill the dashboard itself"}
    try:
        holders = {info.get("pid") for info in get_listening_ports().values()
                   if isinstance(info, dict)}
    except Exception:
        holders = set()
    if pid not in holders:
        return {"ok": False,
                "message": f"PID {pid} no longer holds a listening port (refresh and retry)"}
    try:
        os.kill(pid, signal.SIGTERM)
    except OSError as e:
        return {"ok": False, "message": f"Cannot signal PID {pid}: {e}"}
    time.sleep(0.5)
    if _pid_is_dead(pid):
        return {"ok": True, "message": f"Stopped PID {pid} (SIGTERM)"}
    try:
        os.kill(pid, signal.SIGKILL)
    except OSError as e:
        return {"ok": False, "message": f"PID {pid} ignored SIGTERM and cannot be force-killed: {e}"}
    time.sleep(0.3)
    if _pid_is_dead(pid):
        return {"ok": True, "message": f"Killed PID {pid} (SIGKILL)"}
    return {"ok": False, "message": f"PID {pid} is still alive (zombie or kernel process?)"}


# -----------------------------
# HTML & JS DASHBOARD TEMPLATE
# -----------------------------

HTML_DASHBOARD = """<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="UTF-8" />
  <meta name="viewport" content="width=device-width, initial-scale=1.0" />
  <title>DevBoost • Port Forward Manager</title>
  <link rel="icon" type="image/png" href="/favicon.png" />
  <link rel="shortcut icon" type="image/png" href="/favicon.png" />
  <link rel="apple-touch-icon" href="/favicon.png" />
  <style>
    :root {
      --bg: #0d1117;
      --card-bg: #161b22;
      --border: #30363d;
      --text: #c9d1d9;
      --text-muted: #8b949e;
      --accent: #58a6ff;
      --success: #3fb950;
      --warning: #d29922;
      --danger: #f85149;
      --btn-bg: #21262d;
      --btn-hover: #30363d;
      --font-mono: ui-monospace, SFMono-Regular, "SF Mono", Menlo, Consolas, monospace;
    }
    * { box-sizing: border-box; margin: 0; padding: 0; }
    body {
      background-color: var(--bg);
      color: var(--text);
      font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Helvetica, Arial, sans-serif;
      padding: 24px;
      line-height: 1.5;
    }
    .container { max-width: 1060px; margin: 0 auto; }
    header {
      display: flex;
      justify-content: space-between;
      align-items: center;
      padding-bottom: 20px;
      border-bottom: 1px solid var(--border);
      margin-bottom: 24px;
      flex-wrap: wrap;
      gap: 16px;
    }
    .title-group h1 { font-size: 22px; font-weight: 600; color: #fff; display: flex; align-items: center; gap: 8px; }
    .server-tag {
      font-size: 13px;
      color: var(--text-muted);
      font-family: var(--font-mono);
      display: flex;
      align-items: center;
      gap: 6px;
      margin-top: 4px;
    }
    .status-dot {
      width: 8px; height: 8px; border-radius: 50%; display: inline-block;
    }
    .status-dot.online { background-color: var(--success); box-shadow: 0 0 8px var(--success); }
    .status-dot.offline { background-color: var(--danger); }
    
    .stats-bar {
      display: grid;
      grid-template-columns: repeat(auto-fit, minmax(200px, 1fr));
      gap: 16px;
      margin-bottom: 24px;
    }
    .stat-card {
      background: var(--card-bg);
      border: 1px solid var(--border);
      border-radius: 8px;
      padding: 16px;
    }
    .stat-card .label { font-size: 12px; color: var(--text-muted); text-transform: uppercase; letter-spacing: 0.5px; }
    .stat-card .value { font-size: 26px; font-weight: 600; color: #fff; margin-top: 4px; font-family: var(--font-mono); }

    .btn {
      background: var(--btn-bg);
      color: #c9d1d9;
      border: 1px solid var(--border);
      padding: 7px 14px;
      border-radius: 6px;
      font-size: 13px;
      cursor: pointer;
      font-weight: 500;
      display: inline-flex;
      align-items: center;
      gap: 6px;
      transition: all 0.15s ease;
      text-decoration: none;
    }
    .btn:hover { background: var(--btn-hover); color: #fff; border-color: #8b949e; }
    .btn-primary { background: #238636; color: #fff; border-color: rgba(240,246,252,0.1); }
    .btn-primary:hover { background: #2ea043; border-color: rgba(240,246,252,0.1); }
    .btn-danger { color: var(--danger); }
    .btn-danger:hover { background: rgba(248, 81, 73, 0.15); border-color: var(--danger); }
    .btn-sm { padding: 4px 8px; font-size: 12px; }

    .section-card {
      background: var(--card-bg);
      border: 1px solid var(--border);
      border-radius: 8px;
      margin-bottom: 24px;
      overflow: hidden;
    }
    .section-header {
      padding: 16px 20px;
      border-bottom: 1px solid var(--border);
      display: flex;
      justify-content: space-between;
      align-items: center;
      background: rgba(255, 255, 255, 0.02);
    }
    .section-header h2 { font-size: 16px; font-weight: 600; color: #fff; }
    .mini-tabs { display: inline-flex; gap: 4px; background: #0d1117; border: 1px solid var(--border); border-radius: 16px; padding: 3px; }
    .mini-tab {
      background: transparent; border: none; border-radius: 12px;
      color: var(--text-muted); font-size: 12px; font-weight: 600;
      padding: 4px 12px; cursor: pointer; white-space: nowrap;
    }
    .mini-tab:hover { color: #fff; }
    .mini-tab.active { background: var(--btn-hover); color: #fff; }

    table { width: 100%; border-collapse: collapse; text-align: left; }
    th { padding: 12px 18px; font-size: 12px; color: var(--text-muted); font-weight: 600; border-bottom: 1px solid var(--border); }
    td { padding: 14px 18px; font-size: 13px; border-bottom: 1px solid rgba(48, 54, 61, 0.4); vertical-align: middle; }
    tr:last-child td { border-bottom: none; }
    tr:hover td { background: rgba(255, 255, 255, 0.015); }

    .badge {
      font-size: 11px;
      padding: 3px 8px;
      border-radius: 12px;
      font-weight: 500;
      display: inline-flex;
      align-items: center;
      gap: 4px;
      white-space: nowrap;
    }
    .badge-always { background: rgba(88, 166, 255, 0.15); color: #58a6ff; border: 1px solid rgba(88, 166, 255, 0.3); }
    .badge-session { background: rgba(210, 153, 34, 0.15); color: #d29922; border: 1px solid rgba(210, 153, 34, 0.3); }
    .badge-active { background: rgba(63, 185, 80, 0.15); color: #3fb950; border: 1px solid rgba(63, 185, 80, 0.3); }
    .badge-inactive { background: rgba(248, 81, 73, 0.15); color: #f85149; border: 1px solid rgba(248, 81, 73, 0.3); }

    .port-link {
      font-family: var(--font-mono);
      font-weight: 600;
      color: var(--accent);
      text-decoration: none;
      display: inline-flex;
      align-items: center;
      gap: 4px;
    }
    .port-link:hover { text-decoration: underline; }

    .actions-cell { display: flex; gap: 8px; justify-content: flex-end; }

    .tabs-bar {
      display: flex;
      gap: 8px;
      align-items: center;
      flex-wrap: wrap;
      margin-bottom: 20px;
      padding: 10px 12px;
      background: var(--card-bg);
      border: 1px solid var(--border);
      border-radius: 8px;
    }
    .tab {
      display: inline-flex;
      align-items: center;
      gap: 7px;
      padding: 7px 10px 7px 12px;
      border: 1px solid var(--border);
      border-radius: 18px;
      background: #0d1117;
      color: var(--text);
      font-size: 13px;
      cursor: pointer;
      user-select: none;
      max-width: 260px;
    }
    .tab:hover { border-color: #8b949e; color: #fff; }
    .tab.active { border-color: var(--accent); background: rgba(88,166,255,0.12); color: #fff; }
    .tab.dragging { opacity: 0.45; }
    .tab .tab-name { overflow: hidden; text-overflow: ellipsis; white-space: nowrap; font-weight: 600; }
    .tab .tab-meta { font-size: 11px; color: var(--text-muted); font-family: var(--font-mono); }
    .tab .tab-dot { width: 7px; height: 7px; border-radius: 50%; background: var(--text-muted); flex-shrink: 0; }
    .tab .tab-dot.online { background: var(--success); box-shadow: 0 0 6px var(--success); }
    .tab button { background: transparent; border: none; color: var(--text-muted); cursor: pointer; font-size: 12px; padding: 0 2px; }
    .tab button:hover { color: #fff; }
    .tab-add {
      border-style: dashed;
      color: var(--text-muted);
      font-weight: 600;
    }
    .server-list { display: flex; flex-direction: column; gap: 8px; max-height: 260px; overflow-y: auto; margin-top: 8px; }
    .server-row {
      display: flex; align-items: center; justify-content: space-between; gap: 10px;
      padding: 9px 12px; border: 1px solid var(--border); border-radius: 8px; background: #0d1117;
    }
    .server-row .srv-main { min-width: 0; }
    .server-row .srv-host { font-family: var(--font-mono); font-weight: 600; color: #fff; font-size: 13px; }
    .server-row .srv-sub { font-size: 11px; color: var(--text-muted); font-family: var(--font-mono); overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
    .search-input { margin-bottom: 4px; }

    .modal-overlay {
      position: fixed; inset: 0; background: rgba(0, 0, 0, 0.7);
      display: none; align-items: center; justify-content: center; z-index: 1000;
    }
    .modal {
      background: var(--card-bg);
      border: 1px solid var(--border);
      border-radius: 10px;
      width: 100%;
      max-width: 440px;
      box-shadow: 0 10px 30px rgba(0,0,0,0.5);
      overflow: hidden;
    }
    .modal-header {
      padding: 16px 20px;
      border-bottom: 1px solid var(--border);
      display: flex;
      justify-content: space-between;
      align-items: center;
    }
    .modal-header h3 { font-size: 16px; color: #fff; }
    .modal-body { padding: 20px; }
    /* The folder-sync form can be taller than short laptop viewports. Keep
       its title and actions in view while only the form fields scroll. */
    #sync-modal .modal {
      max-height: calc(100vh - 32px);
      max-height: calc(100dvh - 32px);
      display: flex;
      flex-direction: column;
    }
    #sync-modal .modal-header, #sync-modal .modal-footer { flex: 0 0 auto; }
    #sync-modal .modal-body { min-height: 0; overflow-y: auto; }
    .form-group { margin-bottom: 16px; }
    .form-group label { display: block; font-size: 12px; color: var(--text-muted); margin-bottom: 6px; font-weight: 500; }
    .form-group input[type="text"], .form-group input[type="number"] {
      width: 100%;
      padding: 8px 12px;
      background: #0d1117;
      border: 1px solid var(--border);
      border-radius: 6px;
      color: #fff;
      font-size: 14px;
      font-family: var(--font-mono);
    }
    .form-group input:focus { outline: none; border-color: var(--accent); }
    .form-group select {
      width: 100%;
      padding: 8px 12px;
      background: #0d1117;
      border: 1px solid var(--border);
      border-radius: 6px;
      color: #fff;
      font-size: 13px;
    }
    .form-group select:focus { outline: none; border-color: var(--accent); }
    .form-hint { font-size: 11px; color: var(--text-muted); margin-top: 4px; line-height: 1.4; }
    .path-row { display: flex; gap: 8px; }
    .path-row input { flex: 1; min-width: 0; }
    .path-row .btn { flex-shrink: 0; }
    .suggest-wrap { position: relative; flex: 1; min-width: 0; display: flex; }
    .suggest-wrap input { flex: 1; min-width: 0; width: 100%; }
    .suggest-list {
      position: absolute; top: calc(100% + 4px); left: 0; right: 0;
      background: #0d1117;
      border: 1px solid var(--border);
      border-radius: 6px;
      max-height: 200px;
      overflow-y: auto;
      z-index: 60;
      display: none;
      box-shadow: 0 8px 24px rgba(0,0,0,0.5);
    }
    .suggest-list.open { display: block; }
    .suggest-item {
      display: flex; align-items: center; gap: 8px;
      width: 100%; text-align: left;
      background: transparent; border: none; border-bottom: 1px solid rgba(48,54,61,0.4);
      color: var(--text); font-size: 12px; font-family: var(--font-mono);
      padding: 7px 10px; cursor: pointer;
    }
    .suggest-item:hover, .suggest-item.active { background: rgba(88,166,255,0.15); color: #fff; }
    .suggest-item:last-child { border-bottom: none; }
    .suggest-item.is-hidden { opacity: 0.6; }
    .browser {
      margin-top: 8px;
      border: 1px solid var(--border);
      border-radius: 6px;
      background: #0d1117;
      overflow: hidden;
      display: none;
    }
    .browser.open { display: block; }
    .browser-bar {
      display: flex; align-items: center; gap: 6px;
      padding: 8px 10px;
      border-bottom: 1px solid var(--border);
      font-family: var(--font-mono);
      font-size: 11px;
      color: var(--text-muted);
    }
    .browser-bar .cur { flex: 1; min-width: 0; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; color: var(--text); }
    .browser-bar .suggest-wrap { flex: 1; min-width: 0; }
    .browser-bar .suggest-list { max-height: 160px; z-index: 70; }
    .browser-path {
      flex: 1; min-width: 0;
      padding: 5px 8px;
      background: #0d1117;
      border: 1px solid var(--border);
      border-radius: 6px;
      color: var(--text);
      font-size: 11px;
      font-family: var(--font-mono);
    }
    .browser-path:focus { outline: none; border-color: var(--accent); color: #fff; }
    .browser-item.is-hidden { opacity: 0.6; }
    .browser-list { max-height: 180px; overflow-y: auto; }
    .browser-item {
      display: flex; align-items: center; gap: 8px;
      width: 100%; text-align: left;
      background: transparent; border: none; border-bottom: 1px solid rgba(48,54,61,0.4);
      color: var(--text); font-size: 12px; font-family: var(--font-mono);
      padding: 7px 10px; cursor: pointer;
    }
    .browser-item:hover { background: rgba(88,166,255,0.08); color: #fff; }
    .browser-item:last-child { border-bottom: none; }
    .mono { font-family: var(--font-mono); }
    .muted { color: var(--text-muted); }
    .sync-path { font-family: var(--font-mono); font-size: 12px; color: #fff; word-break: break-all; }
    .sync-sub { font-size: 11px; color: var(--text-muted); font-family: var(--font-mono); word-break: break-all; }
    .form-checkbox { display: flex; align-items: center; gap: 8px; cursor: pointer; user-select: none; }
    .recent-chips { display: flex; flex-wrap: wrap; gap: 8px; margin-top: 8px; }
    .recent-chip {
      background: #0d1117;
      border: 1px solid var(--border);
      color: var(--text);
      border-radius: 16px;
      padding: 5px 12px;
      font-size: 12px;
      font-family: var(--font-mono);
      cursor: pointer;
      transition: all 0.15s ease;
    }
    .recent-chip:hover { border-color: var(--accent); color: #fff; background: rgba(88,166,255,0.12); }
    .recent-chip small { color: var(--text-muted); margin-left: 4px; }
    .recent-hint { font-size: 12px; color: var(--text-muted); margin-bottom: 2px; }
    .modal-footer {
      padding: 14px 20px;
      background: rgba(255, 255, 255, 0.02);
      border-top: 1px solid var(--border);
      display: flex;
      justify-content: flex-end;
      gap: 10px;
    }
    .docker-sort { cursor: pointer; user-select: none; white-space: nowrap; }
    .docker-sort:hover { color: #fff; }
    .docker-label { display: inline-flex; align-items: center; gap: 4px; padding: 2px 6px; margin: 1px 3px 1px 0; border-radius: 9px; font-size: 10px; color: #fff; border: 1px solid rgba(255,255,255,.25); white-space: nowrap; }
    .docker-log-output { height: 60vh; overflow: auto; background: #0d1117; color: #c9d1d9; padding: 14px; font: 12px/1.5 var(--font-mono); white-space: pre-wrap; word-break: break-word; }

    #toast {
      position: fixed; bottom: 20px; right: 20px;
      background: #1f242c;
      color: #fff;
      border: 1px solid var(--border);
      padding: 12px 18px;
      border-radius: 8px;
      box-shadow: 0 4px 15px rgba(0,0,0,0.4);
      display: none;
      z-index: 2000;
      font-size: 13px;
    }
  </style>
</head>
<body>
  <div class="container">
    <header>
      <div class="title-group">
        <h1 id="header-server-title">DevBoost • Port Forward Manager</h1>
        <div class="server-tag">
          <span class="status-dot" id="server-status-dot"></span>
          <span id="server-status-text">Checking server...</span> •
          <span id="header-server-host">Connecting...</span>
        </div>
      </div>
      <div style="display:flex; gap:10px;">
        <button class="btn" onclick="cleanOrphans()" id="clean-orphans-btn" title="Clean up lingering duplicate ssh processes">
          🧹 Clean Orphans <span id="orphans-badge" style="font-size:11px; opacity:0.8;"></span>
        </button>
        <button class="btn btn-primary" onclick="openAddModal()">
          + Add Port Forward
        </button>
      </div>
    </header>

    <!-- SSH connection tabs -->
    <div class="tabs-bar" id="tabs-bar">
      <span style="font-size:12px; color:var(--text-muted);">Loading servers...</span>
    </div>

    <!-- Stats -->
    <div class="stats-bar">
      <div class="stat-card">
        <div class="label">Active Forwards</div>
        <div class="value" id="stat-active">0</div>
      </div>
      <div class="stat-card">
        <div class="label">Always Forward (Persistent)</div>
        <div class="value" id="stat-always">0</div>
      </div>
      <div class="stat-card">
        <div class="label">Folder Syncs (Auto)</div>
        <div class="value" id="stat-syncs">0</div>
      </div>
      <div class="stat-card">
        <div class="label">Discovered Remote Ports</div>
        <div class="value" id="stat-remote">-</div>
      </div>
    </div>

    <!-- Docker monitoring (cached snapshots from the active server) -->
    <div class="section-card">
      <div class="section-header">
        <h2>Docker Containers <span class="muted" style="font-weight:400; font-size:12px;" id="docker-subtitle"></span></h2>
        <div style="display:flex; gap:8px;">
          <button class="btn btn-sm" onclick="openDockerLabelsModal()">🏷 Labels</button>
          <button class="btn btn-sm" onclick="fetchDocker()">↻ Refresh</button>
        </div>
      </div>
      <table>
        <thead>
          <tr>
            <th class="docker-sort" onclick="sortDocker('name', event)">CONTAINER <span id="sort-name"></span></th>
            <th class="docker-sort" onclick="sortDocker('labels', event)">LABELS <span id="sort-labels"></span></th>
            <th class="docker-sort" onclick="sortDocker('status', event)">STATUS <span id="sort-status"></span></th>
            <th class="docker-sort" onclick="sortDocker('cpu', event)">CPU <span id="sort-cpu"></span></th>
            <th class="docker-sort" onclick="sortDocker('memory', event)">MEMORY <span id="sort-memory"></span></th>
            <th class="docker-sort" onclick="sortDocker('memory_percent', event)">MEM % <span id="sort-memory_percent"></span></th>
            <th class="docker-sort" onclick="sortDocker('network', event)">NET I/O <span id="sort-network"></span></th>
            <th>IMAGE</th>
          </tr>
        </thead>
        <tbody id="docker-body">
          <tr><td colspan="8" style="text-align:center; color:var(--text-muted); padding:30px;">Checking Docker on the active server...</td></tr>
        </tbody>
      </table>
    </div>

    <!-- Active Port Forwards Table -->
    <div class="section-card">
      <div class="section-header">
        <h2>Configured Port Forwards</h2>
        <button class="btn btn-sm" onclick="fetchStatus()">↻ Refresh</button>
      </div>
      <table>
        <thead>
          <tr>
            <th>LOCAL PORT</th>
            <th>REMOTE</th>
            <th>SERVICE / LABEL</th>
            <th>MODE</th>
            <th>STATUS</th>
            <th style="text-align:right;">ACTIONS</th>
          </tr>
        </thead>
        <tbody id="forwards-body">
          <tr><td colspan="6" style="text-align:center; color:var(--text-muted); padding:30px;">Loading forwards...</td></tr>
        </tbody>
      </table>
    </div>

    <!-- Folder Syncs (tracked mirrors, managed like ports) -->
    <div class="section-card">
      <div class="section-header">
        <h2>Folder Syncs <span class="muted" style="font-weight:400; font-size:12px;" id="syncs-subtitle"></span></h2>
        <div style="display:flex; gap:8px;">
          <button class="btn btn-sm" onclick="fetchSyncs()">↻ Refresh</button>
          <button class="btn btn-sm btn-primary" onclick="openSyncModal()">+ Add Folder Sync</button>
        </div>
      </div>
      <table>
        <thead>
          <tr>
            <th>LOCAL FOLDER</th>
            <th>REMOTE FOLDER</th>
            <th>MODE</th>
            <th>STATUS</th>
            <th style="text-align:right;">ACTIONS</th>
          </tr>
        </thead>
        <tbody id="syncs-body">
          <tr><td colspan="5" style="text-align:center; color:var(--text-muted); padding:30px;">Loading folder syncs...</td></tr>
        </tbody>
      </table>
    </div>

    <!-- Discovered Services: Remote Server / This Mac tabs -->
    <div class="section-card">
      <div class="section-header">
        <div style="display:flex; align-items:center; gap:12px; flex-wrap:wrap;">
          <h2 id="services-section-title">Discovered Services on Remote Server</h2>
          <div class="mini-tabs">
            <button class="mini-tab active" id="tab-remote" onclick="switchServicesTab('remote')" title="Listening services on the active server">🌐 Remote Server</button>
            <button class="mini-tab" id="tab-local" onclick="switchServicesTab('local')" title="Listening ports on this Mac, with kill">💻 This Mac</button>
          </div>
        </div>
        <div style="display:flex; gap:8px;">
          <button class="btn btn-sm" id="scan-remote-btn" onclick="scanRemoteServices()">🔍 Scan Ports</button>
          <button class="btn btn-sm" id="refresh-local-btn" onclick="fetchLocalPorts()" style="display:none;">↻ Refresh</button>
        </div>
      </div>
      <div id="remote-services-wrap">
      <table>
        <thead>
          <tr>
            <th>PORT</th>
            <th>PROCESS</th>
            <th>IDENTIFIED SERVICE</th>
            <th>FORWARD STATUS</th>
            <th style="text-align:right;">ACTION</th>
          </tr>
        </thead>
        <tbody id="remote-services-body">
          <tr><td colspan="5" style="text-align:center; color:var(--text-muted); padding:20px;">Click "Scan Ports" to detect running services on the remote server.</td></tr>
        </tbody>
      </table>
      </div>
      <div id="local-services-wrap" style="display:none;">
      <table>
        <thead>
          <tr>
            <th>PORT</th>
            <th>PROCESS</th>
            <th>SOURCE</th>
            <th style="text-align:right;">ACTION</th>
          </tr>
        </thead>
        <tbody id="local-services-body">
          <tr><td colspan="4" style="text-align:center; color:var(--text-muted); padding:20px;">Loading listening ports on this Mac...</td></tr>
        </tbody>
      </table>
      </div>
    </div>
  </div>

  <!-- Add Forward Modal -->
  <div class="modal-overlay" id="add-modal">
    <div class="modal">
      <div class="modal-header">
        <h3>Add Port Forward</h3>
        <button class="btn btn-sm" onclick="closeAddModal()" style="border:none; background:transparent;">✕</button>
      </div>
      <div class="modal-body">
        <div class="form-group" id="recent-ports-section" style="display:none;">
          <label class="recent-hint" id="recent-ports-title">Previously used — click to quick select</label>
          <div class="recent-chips" id="recent-ports-chips"></div>
        </div>
        <div class="form-group">
          <label>Local Port</label>
          <input type="number" id="modal-local-port" placeholder="e.g. 3030" oninput="syncRemotePort(); maybeAutofillLabel()" />
        </div>
        <div class="form-group">
          <label id="modal-remote-label">Remote Port</label>
          <input type="number" id="modal-remote-port" placeholder="defaults to local port" />
        </div>
        <div class="form-group">
          <label>Service Description / Label</label>
          <input type="text" id="modal-label" placeholder="e.g. Grafana Dashboard" oninput="this.dataset.autoFilled='false'" />
        </div>
        <div class="form-group">
          <label class="form-checkbox">
            <input type="checkbox" id="modal-always" checked />
            <span>Always forward (Persistent LaunchAgent / Auto-reconnect)</span>
          </label>
        </div>
      </div>
      <div class="modal-footer">
        <button class="btn" onclick="closeAddModal()">Cancel</button>
        <button class="btn btn-primary" onclick="submitAddForward()">Forward Port</button>
      </div>
    </div>
  </div>

  <!-- Add Folder Sync Modal (dual file-system path matcher) -->
  <div class="modal-overlay" id="sync-modal">
    <div class="modal" style="max-width:560px;">
      <div class="modal-header">
        <h3 id="sync-modal-title">Add Folder Sync</h3>
        <button class="btn btn-sm" onclick="closeSyncModal()" style="border:none; background:transparent;">✕</button>
      </div>
      <div class="modal-body">
        <div class="form-group" id="recent-folders-section" style="display:none;">
          <label class="recent-hint">Recently synced — click to quick select</label>
          <div class="recent-chips" id="recent-folders-chips"></div>
        </div>
        <div class="form-group">
          <label>Local folder (this Mac) <span class="form-hint">Type to match folders; Tab / Shift+Tab cycles matches.</span></label>
          <div class="path-row">
            <div class="suggest-wrap">
              <input type="text" id="sync-local-path" placeholder="e.g. /Users/you/projects/app — type to search" autocomplete="off" autocorrect="off" autocapitalize="none" spellcheck="false"
                     oninput="onSyncPathInput('local')" onkeydown="onSuggestKey(event, 'local')" onblur="hideSuggest('local')" onfocus="onSyncPathInput('local')" />
              <div class="suggest-list" id="suggest-local"></div>
            </div>
            <button class="btn btn-sm" onclick="toggleBrowser('local')">📂 Browse</button>
          </div>
          <div class="browser" id="browser-local">
            <div class="browser-bar">
              <button class="btn btn-sm" onclick="browseLocalGo('..')" title="Up one level">⬆</button>
              <button class="btn btn-sm" onclick="browseLocalGo('~')" title="Home">⌂</button>
              <div class="suggest-wrap">
                <input class="browser-path" id="browser-local-path" value="~" autocomplete="off" autocorrect="off" autocapitalize="none" spellcheck="false"
                       title="Type to match folders. Tab / Shift+Tab cycles matches; Enter opens one."
                       oninput="onBrowserPathInput('local')" onkeydown="onBrowserSuggestKey(event, 'local')" onblur="hideBrowserSuggest('local')" />
                <div class="suggest-list" id="browser-suggest-local"></div>
              </div>
              <button class="btn btn-sm" onclick="browserGo('local')" title="Go to typed path">Go</button>
              <button class="btn btn-sm" onclick="mkdirBrowser('local')" title="Create new folder here">＋</button>
              <button class="btn btn-sm btn-primary" onclick="pickBrowserPath('local')">Select</button>
            </div>
            <div class="browser-list" id="browser-local-list"></div>
          </div>
        </div>
        <div class="form-group">
          <label><span id="sync-remote-label">Remote folder (on server)</span> <span class="form-hint">Type to match folders; Tab / Shift+Tab cycles matches.</span></label>
          <div class="path-row">
            <div class="suggest-wrap">
              <input type="text" id="sync-remote-path" placeholder="e.g. ~/projects/app — type to search" autocomplete="off" autocorrect="off" autocapitalize="none" spellcheck="false"
                     oninput="onSyncPathInput('remote')" onkeydown="onSuggestKey(event, 'remote')" onblur="hideSuggest('remote')" onfocus="onSyncPathInput('remote')" />
              <div class="suggest-list" id="suggest-remote"></div>
            </div>
            <button class="btn btn-sm" onclick="toggleBrowser('remote')">📂 Browse</button>
          </div>
          <div class="browser" id="browser-remote">
            <div class="browser-bar">
              <button class="btn btn-sm" onclick="browseRemoteGo('..')" title="Up one level">⬆</button>
              <button class="btn btn-sm" onclick="browseRemoteGo('~')" title="Home">⌂</button>
              <div class="suggest-wrap">
                <input class="browser-path" id="browser-remote-path" value="~" autocomplete="off" autocorrect="off" autocapitalize="none" spellcheck="false"
                       title="Type to match folders. Tab / Shift+Tab cycles matches; Enter opens one."
                       oninput="onBrowserPathInput('remote')" onkeydown="onBrowserSuggestKey(event, 'remote')" onblur="hideBrowserSuggest('remote')" />
                <div class="suggest-list" id="browser-suggest-remote"></div>
              </div>
              <button class="btn btn-sm" onclick="browserGo('remote')" title="Go to typed path">Go</button>
              <button class="btn btn-sm" onclick="mkdirBrowser('remote')" title="Create new folder here">＋</button>
              <button class="btn btn-sm btn-primary" onclick="pickBrowserPath('remote')">Select</button>
            </div>
            <div class="browser-list" id="browser-remote-list"></div>
          </div>
        </div>
        <div class="form-group">
          <label>Direction</label>
          <select id="sync-direction" onchange="onSyncDirectionChange()">
            <option value="two-way" selected>⇄ Two-way merge (newer wins, safe)</option>
            <option value="push">⬆ Push — local → remote</option>
            <option value="pull">⬇ Pull — remote → local</option>
          </select>
          <div class="form-hint" id="sync-direction-hint">Two-way keeps both sides merged. Newer file wins. Deletions never propagate.</div>
        </div>
        <div class="form-group" id="sync-mirror-group">
          <label class="form-checkbox">
            <input type="checkbox" id="sync-mirror" />
            <span>Mirror — exact copy (delete extra files on destination)</span>
          </label>
          <div class="form-hint" id="sync-mirror-hint">One-way only: destination becomes an exact copy via rsync --delete. Disabled for two-way (always a safe merge).</div>
        </div>
        <div class="form-group">
          <label class="form-checkbox">
            <input type="checkbox" id="sync-always" checked onchange="document.getElementById('sync-interval-group').style.display = this.checked ? 'block' : 'none'" />
            <span>Auto — keep in sync continuously (persistent agent)</span>
          </label>
          <div class="form-hint">Auto = LaunchAgent with instant local triggers + polling for remote changes (the Always equivalent for ports). Unchecked = one-time sync: runs now + on-demand via Sync Now.</div>
        </div>
        <div class="form-group" id="sync-interval-group">
          <label>Poll interval (seconds, 5–600)</label>
          <input type="number" id="sync-interval" value="15" min="5" max="600" />
        </div>
      </div>
      <div class="modal-footer">
        <button class="btn" onclick="closeSyncModal()">Cancel</button>
        <button class="btn btn-primary" id="sync-submit-btn" onclick="submitAddSync()">Start Sync</button>
      </div>
    </div>
  </div>

  <!-- Add Server Modal (selective import from ~/.ssh/config) -->
  <div class="modal-overlay" id="server-modal">
    <div class="modal" style="max-width:520px;">
      <div class="modal-header">
        <h3>Add SSH Connection Tab</h3>
        <button class="btn btn-sm" onclick="closeServerModal()" style="border:none; background:transparent;">✕</button>
      </div>
      <div class="modal-body">
        <div class="form-group">
          <label>Search ~/.ssh/config hosts</label>
          <input type="text" id="server-search" class="search-input" placeholder="e.g. prod, ubuntu..." oninput="renderSshHostList()" />
        </div>
        <div class="server-list" id="ssh-host-list">
          <div style="color:var(--text-muted); font-size:13px;">Loading SSH hosts...</div>
        </div>
        <div style="border-top:1px solid var(--border); margin:16px 0;"></div>
        <div class="form-group">
          <label>Or add manually (SSH Host alias)</label>
          <input type="text" id="manual-ssh-host" placeholder="e.g. my-remote-server" />
        </div>
        <div class="form-group">
          <label>Display name (optional)</label>
          <input type="text" id="manual-server-name" placeholder="e.g. Production Server" />
        </div>
      </div>
      <div class="modal-footer">
        <button class="btn" onclick="closeServerModal()">Cancel</button>
        <button class="btn btn-primary" onclick="submitManualServer()">Add Manually</button>
      </div>
    </div>
  </div>

  <div class="modal-overlay" id="docker-labels-modal">
    <div class="modal" style="max-width:620px;">
      <div class="modal-header"><h3>Docker label rules</h3><button class="btn btn-sm" onclick="closeDockerLabelsModal()" style="border:none; background:transparent;">✕</button></div>
      <div class="modal-body">
        <div class="form-hint" style="margin-bottom:12px;">A rule matches text contained in a container name. Matching rules are shown as compact labels.</div>
        <div id="docker-label-list"></div>
        <button class="btn btn-sm" onclick="addDockerLabelRow()" style="margin-top:12px;">＋ Add rule</button>
      </div>
      <div class="modal-footer"><button class="btn" onclick="closeDockerLabelsModal()">Cancel</button><button class="btn btn-primary" onclick="saveDockerLabels()">Save labels</button></div>
    </div>
  </div>

  <div class="modal-overlay" id="docker-log-modal">
    <div class="modal" style="max-width:1000px;"><div class="modal-header"><h3 id="docker-log-title">Container logs</h3><button class="btn btn-sm" onclick="closeDockerLog()" style="border:none; background:transparent;">✕</button></div><div class="modal-body" style="padding:0;"><pre id="docker-log-output" class="docker-log-output">Connecting...</pre></div></div>
  </div>

  <div id="toast"></div>

  <script>
    let currentForwards = [];
    let currentServerHost = "";
    let currentServerId = localStorage.getItem("devboost-active-server") || "";
    let cachedHistory = [];
    let allServers = [];
    let sshHostCache = [];
    let serverReachability = {};
    let currentSyncs = [];
    let cachedFolderHistory = [];
    let folderChipCache = [];
    let editingSyncId = null;
    let browserLocalCur = "";
    let browserRemoteCur = "";

    function escapeHtml(s) {
      return String(s == null ? "" : s).replace(/[&<>"']/g, c => ({"&":"&amp;","<":"&lt;",">":"&gt;",'"':"&quot;","'":"&#39;"}[c]));
    }

    function showToast(msg) {
      const t = document.getElementById("toast");
      t.innerText = msg;
      t.style.display = "block";
      setTimeout(() => { t.style.display = "none"; }, 3500);
    }

    function syncRemotePort() {
      const lp = document.getElementById("modal-local-port").value;
      const rp = document.getElementById("modal-remote-port");
      if (!rp.value || rp.dataset.autoSynced === "true") {
        rp.value = lp;
        rp.dataset.autoSynced = "true";
      }
    }

    function openAddModal(local = "", remote = "", label = "") {
      document.getElementById("modal-local-port").value = local;
      document.getElementById("modal-remote-port").value = remote || local;
      document.getElementById("modal-remote-port").dataset.autoSynced = "";
      const labelEl = document.getElementById("modal-label");
      labelEl.value = label;
      // If a label was explicitly passed (e.g. from Scan), treat as manual;
      // otherwise allow auto-fill from stored SERVICE/LABEL mapping.
      labelEl.dataset.autoFilled = label ? "false" : "true";
      if (!label && local) maybeAutofillLabel();
      document.getElementById("modal-always").checked = true;
      document.getElementById("modal-remote-label").innerText = "Remote Port (on " + (currentServerHost || 'server') + ")";
      renderRecentChips(local);
      document.getElementById("add-modal").style.display = "flex";
      document.getElementById("modal-local-port").focus();
      // Refresh history in background so quick-select is always fresh (per active tab)
      fetch("/api/history" + serverQuery()).then(r => r.json()).then(d => {
        if (d.history) {
          const forwarded = new Set((currentForwards || []).map(f => f.local_port));
          const cur = document.getElementById("modal-local-port").value;
          cachedHistory = d.history.filter(h => !forwarded.has(h.local_port) && String(h.local_port) !== String(cur));
          renderRecentChips(cur);
        }
      }).catch(() => {});
    }

    function renderRecentChips(preselectLocal = "") {
      const section = document.getElementById("recent-ports-section");
      const container = document.getElementById("recent-ports-chips");
      const title = document.getElementById("recent-ports-title");
      let items = (cachedHistory || []).filter(h => String(h.local_port) !== String(preselectLocal));
      // Fallback to common ports when no history yet
      if (!cachedHistory || cachedHistory.length === 0) {
        const forwarded = new Set((currentForwards || []).map(f => f.local_port));
        const common = [
          {local_port: 3000, remote_port: 3000, label: "Docker Web / App"},
          {local_port: 3030, remote_port: 3030, label: "Grafana Dashboard"},
          {local_port: 8080, remote_port: 8080, label: "Docker Proxy / Web App"},
          {local_port: 9090, remote_port: 9090, label: "Prometheus Metrics"},
          {local_port: 11434, remote_port: 11434, label: "Ollama LLM API"},
        ].filter(c => !forwarded.has(c.local_port) && String(c.local_port) !== String(preselectLocal));
        if (common.length === 0) { section.style.display = "none"; return; }
        title.innerText = "Common ports — click to quick select";
        items = common;
      } else {
        title.innerText = "Previously used — click to quick select";
      }
      if (items.length === 0) { section.style.display = "none"; return; }
      section.style.display = "block";
      quickSelectCache = items;
      container.innerHTML = items.map((h, i) => {
        const remoteSuffix = (h.remote_port && h.remote_port !== h.local_port) ? ` → :${h.remote_port}` : "";
        return `<button class="recent-chip" onclick="quickSelectRecent(${i})" title="${escapeHtml(h.label || '')}">:${h.local_port}${remoteSuffix}<small>${escapeHtml((h.label || '').slice(0, 22))}</small></button>`;
      }).join("");
    }

    let quickSelectCache = [];

    function lookupStoredLabel(port) {
      const key = String(port == null ? "" : port).trim();
      if (!key) return "";
      const pools = [cachedHistory || [], quickSelectCache || [], currentForwards || []];
      for (const pool of pools) {
        const hit = pool.find(x => String(x.local_port) === key);
        if (hit && hit.label) return hit.label;
      }
      return "";
    }

    function maybeAutofillLabel() {
      const lp = document.getElementById("modal-local-port").value;
      const labelEl = document.getElementById("modal-label");
      if (!lp) return;
      // Don't clobber text the user typed manually
      if (labelEl.value && labelEl.dataset.autoFilled !== "true") return;
      const found = lookupStoredLabel(lp);
      if (found) {
        labelEl.value = found;
        labelEl.dataset.autoFilled = "true";
      }
    }

    function quickSelectRecent(i) {
      const h = quickSelectCache[i];
      if (!h) return;
      document.getElementById("modal-local-port").value = h.local_port;
      document.getElementById("modal-remote-port").value = h.remote_port || h.local_port;
      document.getElementById("modal-remote-port").dataset.autoSynced = "";
      const labelEl = document.getElementById("modal-label");
      labelEl.value = h.label || "";
      labelEl.dataset.autoFilled = "false";
      renderRecentChips(h.local_port);
      document.getElementById("modal-local-port").focus();
    }

    function closeAddModal() {
      document.getElementById("add-modal").style.display = "none";
    }

    function activeServer() {
      return (allServers || []).find(s => s.id === currentServerId) || allServers[0] || null;
    }

    function serverQuery() {
      return currentServerId ? `?server=${encodeURIComponent(currentServerId)}` : "";
    }

    async function fetchServers() {
      try {
        const res = await fetch("/api/servers");
        const data = await res.json();
        allServers = data.servers || [];
        if (!allServers.some(s => s.id === currentServerId)) {
          currentServerId = (allServers[0] || {}).id || "";
          localStorage.setItem("devboost-active-server", currentServerId);
        }
        renderTabs();
      } catch (err) {
        console.error(err);
      }
    }

    function renderTabs() {
      const bar = document.getElementById("tabs-bar");
      if (!allServers.length) {
        bar.innerHTML = '<span style="font-size:12px; color:var(--text-muted);">No servers yet.</span>';
        return;
      }
      bar.innerHTML = allServers.map(s => {
        const isActive = s.id === currentServerId;
        const dotCls = serverReachability[s.id] === true ? "tab-dot online" : (serverReachability[s.id] === false ? "tab-dot" : "tab-dot");
        const pinIcon = s.pinned ? "📍" : "📌";
        return `<div class="tab ${isActive ? 'active' : ''}" draggable="true" data-server-id="${s.id}"
            onclick="selectServer('${s.id}')"
            ondragstart="onTabDragStart(event, '${s.id}')" ondragover="onTabDragOver(event)" ondrop="onTabDrop(event, '${s.id}')" ondragend="onTabDragEnd(event)"
            title="${escapeHtml(s.ssh_host)}${s.ip ? ' (' + escapeHtml(s.ip) + ')' : ''} — drag to reorder">
          <span class="${dotCls}"></span>
          <span class="tab-name">${escapeHtml(s.name || s.ssh_host)}</span>
          <span class="tab-meta">${s.active_count || 0}● ${s.always_count || 0}📌${(s.sync_count || 0) ? ` ${s.sync_count}🔄` : ''}</span>
          <button onclick="event.stopPropagation(); togglePin('${s.id}')" title="${s.pinned ? 'Unpin tab' : 'Pin tab (stays first)'}">${pinIcon}</button>
          <button onclick="event.stopPropagation(); removeServerTab('${s.id}')" title="Remove tab">✕</button>
        </div>`;
      }).join("") + `<button class="tab tab-add" onclick="openServerModal()">+ Add Server</button>`;
    }

    let draggedServerId = null;
    function onTabDragStart(e, sid) { draggedServerId = sid; e.currentTarget.classList.add("dragging"); e.dataTransfer.effectAllowed = "move"; }
    function onTabDragOver(e) { e.preventDefault(); e.dataTransfer.dropEffect = "move"; }
    function onTabDragEnd(e) { e.currentTarget.classList.remove("dragging"); }
    async function onTabDrop(e, targetId) {
      e.preventDefault();
      if (!draggedServerId || draggedServerId === targetId) return;
      const ids = allServers.map(s => s.id);
      const from = ids.indexOf(draggedServerId);
      const to = ids.indexOf(targetId);
      if (from < 0 || to < 0) return;
      ids.splice(to, 0, ids.splice(from, 1)[0]);
      allServers.sort((a, b) => ids.indexOf(a.id) - ids.indexOf(b.id));
      renderTabs();
      try {
        await fetch("/api/servers/reorder", { method: "POST", headers: {"Content-Type": "application/json"}, body: JSON.stringify({ order: ids }) });
        fetchServers();
      } catch (err) { console.error(err); }
      draggedServerId = null;
    }

    function selectServer(sid) {
      if (currentServerId === sid) return;
      currentServerId = sid;
      localStorage.setItem("devboost-active-server", sid);
      document.getElementById("remote-services-body").innerHTML = '<tr><td colspan="5" style="text-align:center; color:var(--text-muted); padding:20px;">Click "Scan Ports" to detect running services on the remote server.</td></tr>';
      document.getElementById("stat-remote").innerText = "-";
      document.getElementById("docker-body").innerHTML = '<tr><td colspan="7" style="text-align:center; color:var(--text-muted); padding:30px;">Checking Docker on the active server...</td></tr>';
      document.getElementById("docker-subtitle").innerText = "";
      document.getElementById("syncs-body").innerHTML = '<tr><td colspan="5" style="text-align:center; color:var(--text-muted); padding:30px;">Loading folder syncs...</td></tr>';
      renderTabs();
      fetchStatus();
      fetchDocker();
      fetchSyncs();
    }

    async function togglePin(sid) {
      const srv = allServers.find(s => s.id === sid);
      if (!srv) return;
      try {
        await fetch("/api/servers/pin", { method: "POST", headers: {"Content-Type": "application/json"}, body: JSON.stringify({ id: sid, pinned: !srv.pinned }) });
        fetchServers();
      } catch (err) { alert("Failed to pin tab: " + err); }
    }

    async function removeServerTab(sid) {
      const srv = allServers.find(s => s.id === sid);
      if (!srv) return;
      if (allServers.length <= 1) { alert("Cannot remove the last server tab."); return; }
      if (!confirm(`Remove tab "${srv.name || srv.ssh_host}"? Its tunnels and persistent agents will be stopped.`)) return;
      try {
        const res = await fetch("/api/servers/remove", { method: "POST", headers: {"Content-Type": "application/json"}, body: JSON.stringify({ id: sid }) });
        const d = await res.json();
        if (!d.ok) { alert(d.message || "Failed to remove server"); return; }
        if (currentServerId === sid) { currentServerId = ""; localStorage.removeItem("devboost-active-server"); }
        showToast(d.message || "Server removed");
        await fetchServers();
        fetchStatus();
        fetchSyncs();
      } catch (err) { alert("Failed to remove server: " + err); }
    }

    function openServerModal() {
      document.getElementById("server-search").value = "";
      document.getElementById("manual-ssh-host").value = "";
      document.getElementById("manual-server-name").value = "";
      document.getElementById("server-modal").style.display = "flex";
      loadSshHosts();
    }
    function closeServerModal() { document.getElementById("server-modal").style.display = "none"; }

    async function loadSshHosts() {
      const list = document.getElementById("ssh-host-list");
      list.innerHTML = '<div style="color:var(--text-muted); font-size:13px;">Loading SSH hosts from ~/.ssh/config...</div>';
      try {
        const res = await fetch("/api/ssh-hosts");
        const data = await res.json();
        sshHostCache = data.hosts || [];
        renderSshHostList();
      } catch (err) {
        list.innerHTML = `<div style="color:var(--danger); font-size:13px;">Failed to load ~/.ssh/config: ${err}</div>`;
      }
    }

    function renderSshHostList() {
      const list = document.getElementById("ssh-host-list");
      const q = (document.getElementById("server-search").value || "").toLowerCase();
      const items = (sshHostCache || []).filter(h => !q || h.ssh_host.toLowerCase().includes(q) || (h.hostname || "").toLowerCase().includes(q));
      if (!items.length) {
        list.innerHTML = '<div style="color:var(--text-muted); font-size:13px;">No matching SSH hosts. Add manually below.</div>';
        return;
      }
      filteredSshHosts = items;
      list.innerHTML = items.map((h, i) => {
        const sub = [h.user ? h.user + "@" : "", h.hostname || "", h.port && h.port !== 22 ? ":" + h.port : ""].join("");
        return `<div class="server-row">
          <div class="srv-main">
            <div class="srv-host">${escapeHtml(h.ssh_host)}</div>
            <div class="srv-sub">${escapeHtml(sub || "ssh-config entry")}</div>
          </div>
          ${h.added
            ? `<span class="badge badge-active">Added</span>`
            : `<button class="btn btn-sm btn-primary" onclick="addServerFromSshByIndex(${i})">+ Add Tab</button>`}
        </div>`;
      }).join("");
    }

    let filteredSshHosts = [];

    async function addServerFromSshByIndex(i) {
      const h = (filteredSshHosts || [])[i] || {};
      const sshHost = h.ssh_host;
      if (!sshHost) return;
      showToast(`Adding ${sshHost}...`);
      try {
        const res = await fetch("/api/servers", { method: "POST", headers: {"Content-Type": "application/json"}, body: JSON.stringify({ ssh_host: sshHost, name: sshHost, ip: h.hostname || "" }) });
        const d = await res.json();
        if (!d.ok) { alert(d.message || "Failed to add server"); return; }
        closeServerModal();
        showToast(`Tab "${sshHost}" added`);
        await fetchServers();
        selectServer(d.server.id);
      } catch (err) { alert("Failed to add server: " + err); }
    }

    async function submitManualServer() {
      const sshHost = document.getElementById("manual-ssh-host").value.trim();
      const name = document.getElementById("manual-server-name").value.trim();
      if (!sshHost) { alert("Enter an SSH Host alias."); return; }
      try {
        const res = await fetch("/api/servers", { method: "POST", headers: {"Content-Type": "application/json"}, body: JSON.stringify({ ssh_host: sshHost, name: name || sshHost }) });
        const d = await res.json();
        if (!d.ok) { alert(d.message || "Failed to add server"); return; }
        closeServerModal();
        await fetchServers();
        selectServer(d.server.id);
      } catch (err) { alert("Failed to add server: " + err); }
    }

    async function fetchStatus() {
      try {
        const res = await fetch("/api/status" + serverQuery());
        const data = await res.json();
        if (data.servers) { allServers = data.servers; if (!currentServerId && allServers[0]) { currentServerId = allServers[0].id; localStorage.setItem("devboost-active-server", currentServerId); } renderTabs(); }
        if (data.server_id) { serverReachability[data.server_id] = !!data.server_reachable; }
        renderStatus(data);
      } catch (err) {
        console.error(err);
      }
    }

    function renderStatus(data) {
      const dot = document.getElementById("server-status-dot");
      const txt = document.getElementById("server-status-text");
      if (data.server_reachable) {
        dot.className = "status-dot online";
        txt.innerText = "Connected";
      } else {
        dot.className = "status-dot offline";
        txt.innerText = "Unreachable / Offline";
      }

      if (data.server_name) {
        document.getElementById("header-server-title").innerText = `DevBoost • ${data.server_name} • Port Forwards`;
        if (servicesTab === "local") {
          document.getElementById("services-section-title").innerText = "Listening Ports on This Mac";
        } else {
          document.getElementById("services-section-title").innerText = `Discovered Services on ${data.server_name}`;
        }
        document.title = `DevBoost • ${data.server_name} • Port Forward Manager`;
      }
      if (data.server_host) {
        currentServerHost = data.server_host;
        if (data.server_id) { currentServerId = data.server_id; localStorage.setItem("devboost-active-server", currentServerId); }
        const ipPart = data.server_ip ? ` (${data.server_ip})` : "";
        document.getElementById("header-server-host").innerText = `Host: ${data.server_host}${ipPart}`;
      }

      currentForwards = data.forwards;
      cachedHistory = data.history || [];
      const activeCount = data.forwards.filter(f => f.active).length;
      const alwaysCount = data.forwards.filter(f => f.always).length;
      document.getElementById("stat-active").innerText = activeCount;
      document.getElementById("stat-always").innerText = alwaysCount;

      const orphanBtn = document.getElementById("clean-orphans-btn");
      const orphanBadge = document.getElementById("orphans-badge");
      if (data.orphaned_count > 0) {
        orphanBadge.innerText = `(${data.orphaned_count} found)`;
        orphanBtn.style.borderColor = "var(--warning)";
      } else {
        orphanBadge.innerText = "";
        orphanBtn.style.borderColor = "var(--border)";
      }

      const tbody = document.getElementById("forwards-body");
      if (data.forwards.length === 0) {
        tbody.innerHTML = '<tr><td colspan="6" style="text-align:center; color:var(--text-muted); padding:30px;">No port forwards configured. Click "+ Add Port Forward" above.</td></tr>';
        return;
      }

      tbody.innerHTML = data.forwards.map(f => {
        const url = `http://localhost:${f.local_port}`;
        const conflictOwner = (f.conflict_with && (f.conflict_with.name || f.conflict_with.ssh_host)) || "";
        const conflictBadge = f.conflict ? `<span class="badge badge-inactive" title="${conflictOwner ? `Local port ${f.local_port} is already held by ${conflictOwner}` : "Another server tab is already listening on this local port"}">⚠️ ${conflictOwner ? `Port in use by ${escapeHtml(conflictOwner)}` : "Port in use by another tab"}</span>` : "";
        const activeBadge = f.active
          ? `<span class="badge badge-active">🟢 Active ${f.pid ? '(PID ' + f.pid + ')' : ''}</span>`
          : `<span class="badge badge-inactive">🔴 Stopped</span>`;
        const modeBadge = f.always
          ? `<span class="badge badge-always" title="Managed by LaunchAgent">📌 Always</span>`
          : `<span class="badge badge-session" title="Temporary SSH tunnel">⚡ Session</span>`;

        return `
          <tr>
            <td>
              <a href="${url}" target="_blank" class="port-link">
                :${f.local_port} ↗
              </a>
            </td>
            <td style="font-family:var(--font-mono); color:var(--text-muted);">
              :${f.remote_port}
            </td>
            <td>
              <strong>${escapeHtml(f.label)}</strong><br/>${conflictBadge}
            </td>
            <td>${modeBadge}</td>
            <td>${activeBadge}</td>
            <td>
              <div class="actions-cell">
                <button class="btn btn-sm" onclick="toggleAlways(${f.local_port}, ${!f.always})" title="${f.always ? 'Change to temporary session' : 'Make persistent (Always Forward)'}">
                  ${f.always ? 'Make Session' : 'Make Always'}
                </button>
                <button class="btn btn-sm btn-danger" onclick="deleteForward(${f.local_port})" title="Remove forward">
                  Delete
                </button>
              </div>
            </td>
          </tr>
        `;
      }).join("");
    }

    async function submitAddForward() {
      const localPort = parseInt(document.getElementById("modal-local-port").value, 10);
      let remotePort = parseInt(document.getElementById("modal-remote-port").value, 10);
      if (isNaN(remotePort)) remotePort = localPort;
      const label = document.getElementById("modal-label").value.trim();
      const always = document.getElementById("modal-always").checked;

      if (isNaN(localPort) || localPort <= 0) {
        alert("Please specify a valid port number.");
        return;
      }

      closeAddModal();
      showToast(`Setting up forward for port ${localPort}...`);
      try {
        const res = await fetch("/api/forward", {
          method: "POST",
          headers: {"Content-Type": "application/json"},
          body: JSON.stringify({ server_id: currentServerId, local_port: localPort, remote_port: remotePort, label: label, always: always })
        });
        const d = await res.json();
        showToast(d.message || "Forward created!");
        fetchStatus();
      } catch (err) {
        alert("Failed to add forward: " + err);
      }
    }

    async function toggleAlways(localPort, makeAlways) {
      showToast(`Updating persistence rule for port ${localPort}...`);
      try {
        await fetch("/api/toggle", {
          method: "POST",
          headers: {"Content-Type": "application/json"},
          body: JSON.stringify({ server_id: currentServerId, local_port: localPort, always: makeAlways })
        });
        fetchStatus();
      } catch (err) {
        alert("Error toggling persistence: " + err);
      }
    }

    async function deleteForward(localPort) {
      if (!confirm(`Are you sure you want to remove forward for port ${localPort}?`)) return;
      showToast(`Removing forward for port ${localPort}...`);
      try {
        await fetch("/api/remove", {
          method: "POST",
          headers: {"Content-Type": "application/json"},
          body: JSON.stringify({ server_id: currentServerId, local_port: localPort })
        });
        fetchStatus();
      } catch (err) {
        alert("Error removing forward: " + err);
      }
    }

    async function cleanOrphans() {
      showToast("Cleaning up duplicate/hung SSH processes...");
      try {
        const res = await fetch("/api/clean", { method: "POST", headers: {"Content-Type": "application/json"}, body: JSON.stringify({ server_id: currentServerId }) });
        const d = await res.json();
        showToast(`Cleaned up ${d.killed} orphaned process(es).`);
        fetchStatus();
      } catch (err) {
        alert("Error cleaning orphans: " + err);
      }
    }

    async function scanRemoteServices() {
      const tbody = document.getElementById("remote-services-body");
      tbody.innerHTML = `<tr><td colspan="5" style="text-align:center; color:var(--text-muted); padding:20px;">Scanning remote ports on ${currentServerHost || 'remote server'}...</td></tr>`;
      try {
        const res = await fetch("/api/scan" + serverQuery());
        const data = await res.json();
        document.getElementById("stat-remote").innerText = data.services.length;
        if (data.services.length === 0) {
          tbody.innerHTML = '<tr><td colspan="5" style="text-align:center; color:var(--text-muted); padding:20px;">No listening ports detected or server unreachable.</td></tr>';
          return;
        }

        tbody.innerHTML = data.services.map(s => {
          const isForwarded = currentForwards.some(f => f.remote_port === s.port);
          return `
            <tr>
              <td style="font-family:var(--font-mono); font-weight:600; color:#fff;">:${s.port}</td>
              <td style="font-family:var(--font-mono); color:var(--text-muted);">${s.process}</td>
              <td><strong>${s.label}</strong></td>
              <td>
                ${isForwarded 
                  ? '<span class="badge badge-active">Forwarded</span>' 
                  : '<span class="badge" style="background:rgba(255,255,255,0.05); color:var(--text-muted);">Not forwarded</span>'}
              </td>
              <td style="text-align:right;">
                ${isForwarded
                  ? `<a href="http://localhost:${s.port}" target="_blank" class="btn btn-sm">Open ↗</a>`
                  : `<button class="btn btn-sm btn-primary" onclick='openAddModal(${s.port}, ${s.port}, ${JSON.stringify(s.label || "")})'>+ Forward</button>`}
              </td>
            </tr>
          `;
        }).join("");
      } catch (err) {
        tbody.innerHTML = `<tr><td colspan="5" style="text-align:center; color:var(--danger); padding:20px;">Scan failed: ${err}</td></tr>`;
      }
    }

    let dockerRows = [];
    let dockerSorts = [{key: "name", dir: 1}];
    let dockerLogTimer = null;
    let dockerLogContainer = "";

    function numericDockerValue(value) {
      const m = String(value || "").replace(/,/g, "").match(/-?[0-9]+(?:\\.[0-9]+)?/);
      return m ? Number(m[0]) : -Infinity;
    }
    function dockerSortValue(row, key) {
      const s = row.stats || {};
      if (key === "name") return String(row.name || row.id || "").toLowerCase();
      if (key === "labels") return (row.labels || []).length;
      if (key === "status") return String(row.status || "").toLowerCase();
      if (key === "cpu") return numericDockerValue(s.cpu_percent);
      if (key === "memory") return numericDockerValue(s.memory_usage);
      if (key === "memory_percent") return numericDockerValue(s.memory_percent);
      if (key === "network") return numericDockerValue(s.network_io);
      return "";
    }
    function sortDocker(key, event) {
      const multi = event && event.shiftKey;
      const current = dockerSorts.find(s => s.key === key);
      if (!multi) dockerSorts = [{key, dir: current ? -current.dir : (key === "name" ? 1 : -1)}];
      else if (current) current.dir *= -1;
      else dockerSorts.push({key, dir: key === "name" ? 1 : -1});
      renderDockerRows();
    }
    function renderDockerSortIndicators() {
      ["name", "labels", "status", "cpu", "memory", "memory_percent", "network"].forEach(key => {
        const el = document.getElementById("sort-" + key);
        if (!el) return;
        const i = dockerSorts.findIndex(s => s.key === key);
        el.innerText = i < 0 ? "" : (dockerSorts[i].dir > 0 ? "↑" : "↓") + (dockerSorts.length > 1 ? (i + 1) : "");
      });
    }
    function renderDockerRows() {
      const tbody = document.getElementById("docker-body");
      const rows = dockerRows.slice().sort((a, b) => {
        for (const sort of dockerSorts) {
          const av = dockerSortValue(a, sort.key), bv = dockerSortValue(b, sort.key);
          if (av < bv) return -1 * sort.dir;
          if (av > bv) return 1 * sort.dir;
        }
        return dockerSortValue(a, "name").localeCompare(dockerSortValue(b, "name"));
      });
      tbody.innerHTML = rows.map(c => {
        const s = c.stats || {};
        const labels = (c.labels || []).map(l => `<span class="docker-label" style="background:${escapeHtml(l.color || '#8b949e')}" title="matches: ${escapeHtml(l.name || '')}">${escapeHtml(l.name || '')}</span>`).join("") || '<span class="muted">—</span>';
        return `<tr>
          <td><button class="btn btn-sm" onclick='openDockerLog(${JSON.stringify(c.name || c.id)})' title="Watch logs">▣</button> <strong>${escapeHtml(c.name || c.id)}</strong><div class="sync-sub">${escapeHtml(c.id || "")}</div></td>
          <td>${labels}</td><td>${escapeHtml(c.status || "")}</td>
          <td class="mono">${escapeHtml(s.cpu_percent || "-")}</td><td class="mono">${escapeHtml(s.memory_usage || "-")}</td>
          <td class="mono">${escapeHtml(s.memory_percent || "-")}</td><td class="mono">${escapeHtml(s.network_io || "-")}</td>
          <td class="mono" style="max-width:220px; overflow:hidden; text-overflow:ellipsis; white-space:nowrap;" title="${escapeHtml(c.image || "")}">${escapeHtml(c.image || "")}</td>
        </tr>`;
      }).join("");
      renderDockerSortIndicators();
    }
    async function fetchDocker() {
      const tbody = document.getElementById("docker-body");
      const subtitle = document.getElementById("docker-subtitle");
      try {
        const res = await fetch("/api/docker" + serverQuery());
        const data = await res.json();
        if (data.available === null) {
          subtitle.innerText = "checking...";
          return;
        }
        if (!data.available) {
          subtitle.innerText = "unavailable";
          tbody.innerHTML = `<tr><td colspan="8" style="text-align:center; color:var(--warning); padding:30px;">${escapeHtml(data.message || "Docker is unavailable on this server.")}</td></tr>`;
          return;
        }
        const age = data.age_seconds == null ? "just now" : `${data.age_seconds}s ago`;
        subtitle.innerText = `${data.containers.length} running • updated ${age}`;
        if (!data.containers.length) {
          tbody.innerHTML = '<tr><td colspan="8" style="text-align:center; color:var(--text-muted); padding:30px;">Docker is available, but no containers are running.</td></tr>';
          return;
        }
        dockerRows = data.containers || [];
        renderDockerRows();
      } catch (err) {
        subtitle.innerText = "error";
        tbody.innerHTML = `<tr><td colspan="8" style="text-align:center; color:var(--danger); padding:30px;">Docker query failed: ${escapeHtml(String(err))}</td></tr>`;
      }
    }

    function dockerLabelRow(label = {}) {
      const id = escapeHtml(label.id || "");
      return `<div class="docker-label-edit" data-id="${id}" style="display:grid; grid-template-columns:1.1fr 1.3fr 72px 28px 28px; gap:8px; align-items:center; margin-bottom:8px;">
        <input type="text" value="${escapeHtml(label.name || "")}" placeholder="Label name" data-field="name" />
        <input type="text" value="${escapeHtml(label.match || "")}" placeholder="name contains..." data-field="match" />
        <input type="color" value="${/^#[0-9a-fA-F]{6}$/.test(label.color || '') ? label.color : '#8b949e'}" data-field="color" title="Label color" />
        <input type="checkbox" ${label.enabled === false ? "" : "checked"} data-field="enabled" title="Enabled" />
        <button class="btn btn-sm btn-danger" onclick="this.parentElement.remove()" title="Remove">✕</button></div>`;
    }
    function addDockerLabelRow() { document.getElementById("docker-label-list").insertAdjacentHTML("beforeend", dockerLabelRow()); }
    async function openDockerLabelsModal() {
      const res = await fetch("/api/docker/labels"); const data = await res.json();
      document.getElementById("docker-label-list").innerHTML = (data.labels || []).map(dockerLabelRow).join("");
      document.getElementById("docker-labels-modal").style.display = "flex";
    }
    function closeDockerLabelsModal() { document.getElementById("docker-labels-modal").style.display = "none"; }
    async function saveDockerLabels() {
      const labels = [...document.querySelectorAll(".docker-label-edit")].map(row => {
        const get = field => row.querySelector(`[data-field="${field}"]`);
        return {id: row.dataset.id, name: get("name").value, match: get("match").value, color: get("color").value, enabled: get("enabled").checked};
      });
      const res = await fetch("/api/docker/labels", {method:"POST", headers:{"Content-Type":"application/json"}, body:JSON.stringify({labels})});
      const data = await res.json(); if (!data.ok) { alert(data.message || "Could not save labels"); return; }
      closeDockerLabelsModal(); fetchDocker(); showToast("Docker labels saved");
    }
    async function openDockerLog(container) {
      closeDockerLog(); dockerLogContainer = container; document.getElementById("docker-log-title").innerText = `Logs • ${container}`;
      document.getElementById("docker-log-output").innerText = "Connecting..."; document.getElementById("docker-log-modal").style.display = "flex";
      await refreshDockerLog(); dockerLogTimer = setInterval(refreshDockerLog, 2000);
    }
    function closeDockerLog() { if (dockerLogTimer) clearInterval(dockerLogTimer); dockerLogTimer = null; document.getElementById("docker-log-modal").style.display = "none"; }
    async function refreshDockerLog() {
      if (!dockerLogContainer) return;
      try {
        const query = serverQuery();
        const res = await fetch("/api/docker/logs" + (query ? query + "&" : "?") + "container=" + encodeURIComponent(dockerLogContainer) + "&tail=300");
        const data = await res.json(); const output = document.getElementById("docker-log-output");
        if (!data.ok) { output.innerText = data.message || "Logs unavailable"; return; }
        const previous = output.dataset.content || ""; const next = data.logs || "";
        output.dataset.content = next; output.innerText = next || "(no logs yet — waiting for the container)"; output.scrollTop = output.scrollHeight;
        if (previous && next && next.length < previous.length && !next.includes(previous)) output.innerText = `[container restarted or rebuilt]\n${next}`;
      } catch (err) { document.getElementById("docker-log-output").innerText = String(err); }
    }

    let servicesTab = "remote";
    let currentLocalPorts = [];

    function switchServicesTab(which) {
      servicesTab = which;
      document.getElementById("tab-remote").classList.toggle("active", which === "remote");
      document.getElementById("tab-local").classList.toggle("active", which === "local");
      document.getElementById("remote-services-wrap").style.display = which === "remote" ? "block" : "none";
      document.getElementById("local-services-wrap").style.display = which === "local" ? "block" : "none";
      document.getElementById("scan-remote-btn").style.display = which === "remote" ? "" : "none";
      document.getElementById("refresh-local-btn").style.display = which === "local" ? "" : "none";
      const title = document.getElementById("services-section-title");
      if (which === "local") {
        title.innerText = "Listening Ports on This Mac";
        fetchLocalPorts();
      } else {
        const srv = activeServer();
        title.innerText = `Discovered Services on ${(srv && (srv.name || srv.ssh_host)) || "Remote Server"}`;
      }
    }

    async function fetchLocalPorts() {
      if (servicesTab !== "local") return;
      const tbody = document.getElementById("local-services-body");
      try {
        const res = await fetch("/api/local-ports");
        const data = await res.json();
        currentLocalPorts = data.ports || [];
        renderLocalPorts();
      } catch (err) {
        tbody.innerHTML = `<tr><td colspan="4" style="text-align:center; color:var(--danger); padding:20px;">Failed to list local ports: ${escapeHtml(String(err))}</td></tr>`;
      }
    }

    function renderLocalPorts() {
      const tbody = document.getElementById("local-services-body");
      if (!currentLocalPorts.length) {
        tbody.innerHTML = '<tr><td colspan="4" style="text-align:center; color:var(--text-muted); padding:20px;">No listening ports on this Mac.</td></tr>';
        return;
      }
      tbody.innerHTML = currentLocalPorts.map(p => {
        const url = `http://localhost:${p.port}`;
        const srcBadge = p.devboost
          ? `<span class="badge badge-always" title="Held by one of your DevBoost SSH tunnels">🔗 DevBoost tunnel</span>`
          : `<span class="badge" style="background:rgba(255,255,255,0.05); color:var(--text-muted);">Other app</span>`;
        return `
          <tr>
            <td><a href="${url}" target="_blank" class="port-link">:${p.port} ↗</a></td>
            <td><strong class="mono">${escapeHtml(p.cmd || "?")}</strong><br/><span class="muted" style="font-size:11px;">PID ${p.pid}</span></td>
            <td>${srcBadge}</td>
            <td>
              <div class="actions-cell">
                <button class="btn btn-sm btn-danger" onclick="killLocalPort(${p.pid})" title="Stop the process holding this port">
                  Kill
                </button>
              </div>
            </td>
          </tr>
        `;
      }).join("");
    }

    async function killLocalPort(pid) {
      const entry = (currentLocalPorts || []).find(p => p.pid === pid) || {};
      const port = entry.port != null ? entry.port : "?";
      const cmd = entry.cmd || "process";
      const extra = entry.devboost ? "\\n\\nNote: this is a DevBoost tunnel — an Always-managed one will restart automatically." : "";
      if (!confirm(`Kill ${cmd} (PID ${pid}) listening on :${port}?\\nThe port will be freed immediately.${extra}`)) return;
      showToast(`Stopping PID ${pid}...`);
      try {
        const res = await fetch("/api/local-ports/kill", {
          method: "POST",
          headers: {"Content-Type": "application/json"},
          body: JSON.stringify({ pid: pid })
        });
        const d = await res.json();
        showToast(d.message || (d.ok ? "Process stopped" : "Could not stop process"));
        fetchLocalPorts();
        fetchStatus();
      } catch (err) {
        alert("Failed to kill process: " + err);
      }
    }

    async function fetchSyncs() {
      try {
        const res = await fetch("/api/syncs" + serverQuery());
        const data = await res.json();
        if (data.server_id) { currentServerId = data.server_id; localStorage.setItem("devboost-active-server", currentServerId); }
        cachedFolderHistory = data.folder_history || [];
        renderSyncs(data);
      } catch (err) {
        console.error(err);
      }
    }

    function formatSyncAge(ts) {
      if (!ts) return "never";
      const delta = (Date.now() / 1000) - ts;
      if (delta < 60) return Math.max(0, Math.floor(delta)) + "s ago";
      if (delta < 3600) return Math.floor(delta / 60) + "m ago";
      if (delta < 86400) return Math.floor(delta / 3600) + "h ago";
      return new Date(ts * 1000).toLocaleString();
    }

    function directionBadge(direction, mirror) {
      const icons = { "two-way": "⇄ Two-way", "push": "⬆ Push", "pull": "⬇ Pull" };
      const label = icons[direction] || escapeHtml(direction || "two-way");
      const mirrorTag = (mirror && direction !== "two-way") ? " +mirror" : "";
      return `<span class="badge badge-session" title="${direction === 'two-way' ? 'Bidirectional merge, newer wins, deletions never propagate' : (mirror ? 'Exact copy — deletions propagate (rsync --delete)' : 'One-way, extra files kept')}">${label}${mirrorTag}</span>`;
    }

    function modeBadge(s) {
      return s.always
        ? `<span class="badge badge-always" title="Persistent LaunchAgent: instant local triggers + polling every ${s.interval || 15}s">📌 Auto</span>`
        : `<span class="badge badge-session" title="One-time sync: runs now + on-demand">⚡ Once</span>`;
    }

    function formatSyncTime(ts) {
      if (!ts) return "—";
      try {
        return new Date(ts * 1000).toLocaleString();
      } catch (err) {
        return "—";
      }
    }

    function syncStatusBadge(s) {
      if (s.last_status === "ok") return `<span class="badge badge-active" title="Last synced: ${escapeHtml(formatSyncTime(s.last_sync))}">✓ ${escapeHtml(formatSyncAge(s.last_sync))}</span>`;
      if (s.last_status === "error") return `<span class="badge badge-inactive" title="${escapeHtml(s.last_message || 'Sync failed')}">✕ failed</span>`;
      return `<span class="badge" style="background:rgba(255,255,255,0.05); color:var(--text-muted);">never synced</span>`;
    }

    function renderSyncs(data) {
      currentSyncs = data.syncs || [];
      const autoCount = currentSyncs.filter(s => s.always).length;
      document.getElementById("stat-syncs").innerText = currentSyncs.length ? `${currentSyncs.length} (${autoCount} auto)` : "0";
      const sub = document.getElementById("syncs-subtitle");
      if (sub) sub.innerText = data.server_name ? `• ${data.server_name}` : "";
      const tbody = document.getElementById("syncs-body");
      if (!currentSyncs.length) {
        tbody.innerHTML = '<tr><td colspan="5" style="text-align:center; color:var(--text-muted); padding:30px;">No folder syncs yet. Click "+ Add Folder Sync" to mirror a folder with this server.</td></tr>';
        return;
      }
      tbody.innerHTML = currentSyncs.map(s => {
        const msg = s.last_message ? `<br/><span class="muted" style="font-size:11px;">${escapeHtml((s.last_message || '').slice(0, 120))}</span>` : "";
        const protectedWarn = s.local_protected && s.last_status !== "ok"
          ? `<br/><span style="font-size:11px; color:var(--warning);">⚠️ ${data.packaged_app ? 'Allow DevBoost to access this protected folder in macOS Privacy & Security before background sync can write here.' : 'Background runs from a source checkout cannot access this folder — run the packaged DevBoost app.'}</span>`
          : "";
        const lastSyncLine = (s.last_status === "ok" && s.last_sync)
          ? `<br/><span class="muted mono" style="font-size:11px;" title="Exact time of the last successful sync">Last synced: ${escapeHtml(formatSyncTime(s.last_sync))}</span>`
          : "";
        return `
          <tr>
            <td><span class="sync-path">${escapeHtml(s.local_path)}</span><br/><span class="muted" style="font-size:11px;">this Mac</span></td>
            <td><span class="sync-path">${escapeHtml(s.remote_path)}</span><br/><span class="muted" style="font-size:11px;">${escapeHtml(data.server_host || currentServerHost || 'server')}</span></td>
            <td>${directionBadge(s.direction, s.mirror)}<br/><span style="display:inline-block; margin-top:4px;">${modeBadge(s)}</span></td>
            <td>${syncStatusBadge(s)}${lastSyncLine}${protectedWarn}${msg}</td>
            <td>
              <div class="actions-cell">
                <button class="btn btn-sm" onclick="openSyncModal('${s.id}')" title="Edit paths and options">Edit</button>
                <button class="btn btn-sm" onclick="runSyncNow('${s.id}')" title="Run this sync immediately">Sync Now</button>
                <button class="btn btn-sm" onclick="toggleSyncAlways('${s.id}', ${!s.always})" title="${s.always ? 'Switch to one-time (remove background agent)' : 'Keep in sync continuously (persistent agent)'}">
                  ${s.always ? 'Make Once' : 'Make Auto'}
                </button>
                <button class="btn btn-sm btn-danger" onclick="deleteSync('${s.id}')" title="Remove sync">Delete</button>
              </div>
            </td>
          </tr>
        `;
      }).join("");
    }

    async function runSyncNow(sid) {
      showToast("Syncing folders...");
      try {
        const res = await fetch("/api/syncs/run", { method: "POST", headers: {"Content-Type": "application/json"}, body: JSON.stringify({ id: sid }) });
        const d = await res.json();
        showToast(d.message || (d.ok ? "Sync complete" : "Sync failed"));
        fetchSyncs();
      } catch (err) { alert("Sync failed: " + err); }
    }

    async function toggleSyncAlways(sid, makeAlways) {
      showToast(makeAlways ? "Enabling Auto sync..." : "Switching to one-time...");
      try {
        await fetch("/api/syncs/toggle", { method: "POST", headers: {"Content-Type": "application/json"}, body: JSON.stringify({ id: sid, always: makeAlways }) });
        fetchSyncs();
        fetchServers();
      } catch (err) { alert("Error toggling sync mode: " + err); }
    }

    async function deleteSync(sid) {
      const s = (currentSyncs || []).find(x => x.id === sid);
      const label = s ? `${s.local_path} <-> ${s.remote_path}` : sid;
      if (!confirm(`Remove folder sync "${label}"? Files are kept on both sides; only the tracking + background agent are removed.`)) return;
      showToast("Removing folder sync...");
      try {
        await fetch("/api/syncs/remove", { method: "POST", headers: {"Content-Type": "application/json"}, body: JSON.stringify({ id: sid }) });
        fetchSyncs();
        fetchServers();
      } catch (err) { alert("Error removing sync: " + err); }
    }

    function onSyncDirectionChange() {
      const dir = document.getElementById("sync-direction").value;
      const mirrorEl = document.getElementById("sync-mirror");
      const hint = document.getElementById("sync-direction-hint");
      if (dir === "two-way") {
        mirrorEl.checked = false;
        mirrorEl.disabled = true;
        document.getElementById("sync-mirror-group").style.opacity = "0.55";
        hint.innerText = "Two-way keeps both sides merged. Newer file wins. Deletions never propagate.";
      } else {
        mirrorEl.disabled = false;
        document.getElementById("sync-mirror-group").style.opacity = "1";
        hint.innerText = dir === "push"
          ? "Push: local is the source of truth, copied to the server."
          : "Pull: server is the source of truth, copied to this Mac.";
      }
    }

    function openSyncModal(syncId) {
      const existing = syncId ? (currentSyncs || []).find(x => x.id === syncId) : null;
      editingSyncId = existing ? existing.id : null;
      document.getElementById("sync-modal-title").innerText = existing ? "Edit Folder Sync" : "Add Folder Sync";
      document.getElementById("sync-submit-btn").innerText = existing ? "Save & Sync" : "Start Sync";
      document.getElementById("sync-local-path").value = existing ? (existing.local_path || "") : "";
      document.getElementById("sync-remote-path").value = existing ? (existing.remote_path || "") : "";
      document.getElementById("sync-direction").value = existing ? (existing.direction || "two-way") : "two-way";
      document.getElementById("sync-mirror").checked = existing ? !!existing.mirror : false;
      document.getElementById("sync-always").checked = existing ? !!existing.always : true;
      document.getElementById("sync-interval").value = existing ? (existing.interval || 15) : 15;
      document.getElementById("sync-interval-group").style.display = document.getElementById("sync-always").checked ? "block" : "none";
      document.getElementById("sync-remote-label").innerText = "Remote folder (on " + (currentServerHost || "server") + ")";
      onSyncDirectionChange();
      document.getElementById("browser-local").classList.remove("open");
      document.getElementById("browser-remote").classList.remove("open");
      hideSuggest("local");
      hideSuggest("remote");
      hideBrowserSuggest("local");
      hideBrowserSuggest("remote");
      renderFolderChips();
      document.getElementById("sync-modal").style.display = "flex";
      // Refresh recent pairs in background (per active tab)
      fetch("/api/folder-history" + serverQuery()).then(r => r.json()).then(d => {
        if (d.history) { cachedFolderHistory = d.history; renderFolderChips(); }
      }).catch(() => {});
      browseLocalGo(existing ? (existing.local_path || "~") : "~");
      browseRemoteGo(existing ? (existing.remote_path || "~") : "~");
    }

    function closeSyncModal() {
      hideBrowserSuggest("local");
      hideBrowserSuggest("remote");
      document.getElementById("sync-modal").style.display = "none";
      editingSyncId = null;
    }

    function renderFolderChips() {
      const section = document.getElementById("recent-folders-section");
      const container = document.getElementById("recent-folders-chips");
      const items = cachedFolderHistory || [];
      if (!items.length) { section.style.display = "none"; return; }
      section.style.display = "block";
      folderChipCache = items;
      container.innerHTML = items.map((h, i) => {
        const localShort = (h.local_path || "").split("/").slice(-2).join("/") || h.local_path;
        const remoteShort = (h.remote_path || "").split("/").slice(-2).join("/") || h.remote_path;
        return `<button class="recent-chip" onclick="quickSelectFolder(${i})" title="local: ${escapeHtml(h.local_path || '')}&#10;remote: ${escapeHtml(h.remote_path || '')}">${escapeHtml(localShort)} ⇄ ${escapeHtml(remoteShort)}</button>`;
      }).join("");
    }

    function quickSelectFolder(i) {
      const h = (folderChipCache || [])[i];
      if (!h) return;
      if (h.local_path) document.getElementById("sync-local-path").value = h.local_path;
      if (h.remote_path) document.getElementById("sync-remote-path").value = h.remote_path;
    }

    function toggleBrowser(which) {
      const el = document.getElementById(which === "local" ? "browser-local" : "browser-remote");
      el.classList.toggle("open");
      if (!el.classList.contains("open")) hideBrowserSuggest(which);
    }

    function pickBrowserPath(which) {
      if (which === "local" && browserLocalCur) {
        document.getElementById("sync-local-path").value = browserLocalCur;
        document.getElementById("browser-local").classList.remove("open");
      } else if (which === "remote" && browserRemoteCur) {
        document.getElementById("sync-remote-path").value = browserRemoteCur;
        document.getElementById("browser-remote").classList.remove("open");
      }
    }

    function browserPathEl(which) {
      return document.getElementById(which === "local" ? "browser-local-path" : "browser-remote-path");
    }

    function browserGo(which) {
      hideBrowserSuggest(which);
      const typed = (browserPathEl(which).value || "").trim();
      if (which === "local") browseLocalGo(typed || "~");
      else browseRemoteGo(typed || "~");
    }

    async function mkdirBrowser(which) {
      const cur = (which === "local" ? browserLocalCur : browserRemoteCur)
        || (browserPathEl(which).value || "").trim() || "~";
      const name = prompt(`New folder name (inside ${cur}):`, "");
      if (name === null) return;
      if (!name.trim()) return;
      showToast("Creating folder...");
      try {
        const body = which === "local"
          ? { which: "local", path: cur, name: name }
          : { which: "remote", server_id: currentServerId, path: cur, name: name };
        const res = await fetch("/api/browse/mkdir", {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify(body)
        });
        const d = await res.json();
        if (!d.ok) { alert(d.message || "Could not create folder"); return; }
        showToast(d.message || "Folder created");
        if (which === "local") {
          document.getElementById("sync-local-path").value = d.path;
          browseLocalGo(d.path);
        } else {
          document.getElementById("sync-remote-path").value = d.path;
          browseRemoteGo(d.path);
        }
      } catch (err) {
        alert("Could not create folder: " + err);
      }
    }

    function renderBrowserList(which, data) {
      const listEl = document.getElementById(which === "local" ? "browser-local-list" : "browser-remote-list");
      const pathEl = browserPathEl(which);
      if (!data || data.ok === false) {
        if (data && data.path) pathEl.value = data.path;
        if (data && data.denied) {
          listEl.innerHTML = `<div style="padding:12px; font-size:12px; line-height:1.6;">
            <div style="color:var(--warning); font-weight:600; margin-bottom:6px;">🔒 macOS blocked access to this folder</div>
            <div style="color:var(--text-muted);">The dashboard process isn't allowed to read<br/><span class="mono">${escapeHtml(data.path || '')}</span></div>
            <div style="margin-top:8px; color:var(--text);">Fix: System Settings → Privacy &amp; Security → <b>Full Disk Access</b> → add your terminal app (Terminal, iTerm, VS Code…), then restart the dashboard. If the dashboard runs in the background, add the Python that runs it instead.</div>
            <div style="margin-top:6px; color:var(--text-muted);">Tip: you can still type the full path above, but syncing the folder needs the same permission.</div>
          </div>`;
          return;
        }
        listEl.innerHTML = `<div style="padding:10px; font-size:12px; color:var(--danger);">${escapeHtml((data && data.message) || 'Cannot list folders')}</div>`;
        return;
      }
      pathEl.value = data.path || "";
      const entries = data.entries || [];
      browserCache[which] = entries;
      if (!entries.length) {
        listEl.innerHTML = `<div style="padding:10px; font-size:12px; color:var(--text-muted);">No subfolders here. <button class="btn btn-sm" onclick="pickBrowserPath('${which}')">Select this folder</button></div>`;
        return;
      }
      listEl.innerHTML = entries.map((e, i) =>
        `<button class="browser-item${e.hidden ? ' is-hidden' : ''}" onclick="browseCacheGo('${which}', ${i})" title="${escapeHtml(e.path)}"><span>📁</span><span style="flex:1; overflow:hidden; text-overflow:ellipsis;">${escapeHtml(e.name)}</span><span style="color:var(--text-muted);">→</span></button>`
      ).join("");
    }

    let browserCache = { local: [], remote: [] };

    function browseCacheGo(which, i) {
      const e = (browserCache[which] || [])[i];
      if (!e) return;
      if (which === "local") browseLocalGo(e.path);
      else browseRemoteGo(e.path);
    }

    async function browseLocalGo(path) {
      let target = path;
      if (path === "..") target = browserLocalCur ? browserLocalCur + "/.." : "~";
      if (path === "~") target = "";
      const listEl = document.getElementById("browser-local-list");
      listEl.innerHTML = '<div style="padding:10px; font-size:12px; color:var(--text-muted);">Loading...</div>';
      try {
        const res = await fetch("/api/browse/local?path=" + encodeURIComponent(target || ""));
        const data = await res.json();
        if (data.ok) browserLocalCur = data.path;
        renderBrowserList("local", data);
      } catch (err) {
        listEl.innerHTML = `<div style="padding:10px; font-size:12px; color:var(--danger);">Browse failed: ${escapeHtml(String(err))}</div>`;
      }
    }

    async function browseRemoteGo(path) {
      let target = path;
      if (path === "..") {
        // Go up from current remote dir (posix-style, remote is ~-aware)
        const cur = browserRemoteCur || "~";
        target = cur === "/" ? "/" : cur.replace(/[/]+$/, "").split("/").slice(0, -1).join("/") || "/";
        if (cur === "~") target = "~";
      }
      if (path === "~") target = "~";
      const listEl = document.getElementById("browser-remote-list");
      listEl.innerHTML = '<div style="padding:10px; font-size:12px; color:var(--text-muted);">Loading remote folders...</div>';
      try {
        const res = await fetch("/api/browse/remote" + serverQuery() + (serverQuery() ? "&" : "?") + "path=" + encodeURIComponent(target || "~"));
        const data = await res.json();
        if (data.ok) browserRemoteCur = data.path;
        renderBrowserList("remote", data);
      } catch (err) {
        listEl.innerHTML = `<div style="padding:10px; font-size:12px; color:var(--danger);">Remote browse failed: ${escapeHtml(String(err))}</div>`;
      }
    }

    let browserSuggestState = { local: { active: -1, req: 0, timer: null }, remote: { active: -1, req: 0, timer: null } };
    let browserSuggestCache = { local: [], remote: [] };

    function browserSuggestListEl(which) {
      return document.getElementById(which === "local" ? "browser-suggest-local" : "browser-suggest-remote");
    }

    function onBrowserPathInput(which) {
      const st = browserSuggestState[which];
      clearTimeout(st.timer);
      hideBrowserSuggest(which);
      st.timer = setTimeout(() => fetchBrowserSuggest(which), 250);
    }

    function hideBrowserSuggest(which) {
      const st = browserSuggestState[which];
      clearTimeout(st.timer);
      st.timer = null;
      st.req++;
      st.active = -1;
      browserSuggestCache[which] = [];
      browserSuggestListEl(which).classList.remove("open");
    }

    async function fetchBrowserSuggest(which) {
      const input = browserPathEl(which);
      const listEl = browserSuggestListEl(which);
      const val = (input.value || "").trim();
      if (!val) { hideBrowserSuggest(which); return; }
      let dir, prefix;
      if (val.endsWith("/")) { dir = val; prefix = ""; }
      else {
        const idx = val.lastIndexOf("/");
        if (idx < 0) { dir = "~"; prefix = val; }
        else if (idx === 0) { dir = "/"; prefix = val.slice(1); }
        else { dir = val.slice(0, idx) || "/"; prefix = val.slice(idx + 1); }
      }
      const myReq = ++browserSuggestState[which].req;
      const url = which === "local"
        ? "/api/browse/local?path=" + encodeURIComponent(dir)
        : "/api/browse/remote" + serverQuery() + (serverQuery() ? "&" : "?") + "path=" + encodeURIComponent(dir);
      try {
        const res = await fetch(url);
        const data = await res.json();
        if (myReq !== browserSuggestState[which].req) return;
        if (!data || data.ok === false) { hideBrowserSuggest(which); return; }
        const pl = prefix.toLowerCase();
        const items = (data.entries || [])
          .filter(e => !prefix || e.name.toLowerCase().startsWith(pl))
          .slice(0, 8);
        if (!items.length) { hideBrowserSuggest(which); return; }
        browserSuggestCache[which] = items;
        browserSuggestState[which].active = -1;
        listEl.innerHTML = items.map((e, i) =>
          `<button class="suggest-item${e.hidden ? ' is-hidden' : ''}" onmousedown="event.preventDefault(); pickBrowserSuggest('${which}', ${i})" title="${escapeHtml(e.path)}"><span>📁</span><span style="flex:1; overflow:hidden; text-overflow:ellipsis;">${escapeHtml(e.path)}</span></button>`
        ).join("");
        listEl.classList.add("open");
      } catch (err) {
        hideBrowserSuggest(which);
      }
    }

    function setActiveBrowserSuggest(which, i) {
      browserSuggestState[which].active = i;
      [...browserSuggestListEl(which).children].forEach((child, index) => {
        child.classList.toggle("active", index === i);
      });
    }

    function pickBrowserSuggest(which, i, keepSuggestions = false) {
      const e = (browserSuggestCache[which] || [])[i];
      if (!e) return;
      browserPathEl(which).value = e.path;
      setActiveBrowserSuggest(which, i);
      if (!keepSuggestions) browserGo(which);
    }

    function onBrowserSuggestKey(ev, which) {
      const listEl = browserSuggestListEl(which);
      const items = browserSuggestCache[which] || [];
      if (!listEl.classList.contains("open")) {
        if (ev.key === "Enter") { ev.preventDefault(); browserGo(which); }
        return;
      }
      if (ev.key === "ArrowDown" || ev.key === "ArrowUp") {
        ev.preventDefault();
        if (!items.length) return;
        let a = browserSuggestState[which].active + (ev.key === "ArrowDown" ? 1 : -1);
        if (browserSuggestState[which].active < 0) a = ev.key === "ArrowDown" ? 0 : items.length - 1;
        a = (a + items.length) % items.length;
        setActiveBrowserSuggest(which, a);
      } else if (ev.key === "Tab") {
        if (!items.length) return;
        ev.preventDefault();
        const current = browserSuggestState[which].active;
        const step = ev.shiftKey ? -1 : 1;
        const a = current < 0
          ? (step > 0 ? 0 : items.length - 1)
          : (current + step + items.length) % items.length;
        pickBrowserSuggest(which, a, true);
      } else if (ev.key === "Enter") {
        ev.preventDefault();
        pickBrowserSuggest(which, browserSuggestState[which].active < 0 ? 0 : browserSuggestState[which].active);
      } else if (ev.key === "Escape") {
        hideBrowserSuggest(which);
      }
    }

    let suggestState = { local: { active: -1, req: 0, timer: null }, remote: { active: -1, req: 0, timer: null } };
    let suggestCache = { local: [], remote: [] };

    function suggestInputEl(which) {
      return document.getElementById(which === "local" ? "sync-local-path" : "sync-remote-path");
    }

    function suggestListEl(which) {
      return document.getElementById(which === "local" ? "suggest-local" : "suggest-remote");
    }

    function onSyncPathInput(which) {
      const st = suggestState[which];
      clearTimeout(st.timer);
      // Never leave results for a previous query visible while the new query
      // is being debounced. This also ensures Tab only cycles current matches.
      hideSuggest(which);
      st.timer = setTimeout(() => fetchSuggest(which), 250);
    }

    function hideSuggest(which) {
      const st = suggestState[which];
      clearTimeout(st.timer);
      st.timer = null;
      st.req++; // invalidate in-flight requests
      st.active = -1;
      suggestCache[which] = [];
      suggestListEl(which).classList.remove("open");
    }

    async function fetchSuggest(which) {
      const input = suggestInputEl(which);
      const listEl = suggestListEl(which);
      const val = (input.value || "").trim();
      if (!val) { hideSuggest(which); return; }
      // Split typed text into dir-to-list + name prefix to match.
      let dir, prefix;
      if (val.endsWith("/")) { dir = val; prefix = ""; }
      else {
        const idx = val.lastIndexOf("/");
        if (idx < 0) { dir = "~"; prefix = val; }
        else if (idx === 0) { dir = "/"; prefix = val.slice(1); }
        else { dir = val.slice(0, idx) || "/"; prefix = val.slice(idx + 1); }
      }
      const myReq = ++suggestState[which].req;
      const url = which === "local"
        ? "/api/browse/local?path=" + encodeURIComponent(dir)
        : "/api/browse/remote" + serverQuery() + (serverQuery() ? "&" : "?") + "path=" + encodeURIComponent(dir);
      try {
        const res = await fetch(url);
        const data = await res.json();
        if (myReq !== suggestState[which].req) return; // stale response
        if (!data || data.ok === false) { hideSuggest(which); return; }
        const pl = prefix.toLowerCase();
        const items = (data.entries || [])
          .filter(e => !prefix || e.name.toLowerCase().startsWith(pl))
          .slice(0, 8);
        if (!items.length) { hideSuggest(which); return; }
        suggestCache[which] = items;
        suggestState[which].active = -1;
        listEl.innerHTML = items.map((e, i) =>
          `<button class="suggest-item${e.hidden ? ' is-hidden' : ''}" onmousedown="event.preventDefault(); pickSuggest('${which}', ${i})" title="${escapeHtml(e.path)}"><span>📁</span><span style="flex:1; overflow:hidden; text-overflow:ellipsis;">${escapeHtml(e.path)}</span></button>`
        ).join("");
        listEl.classList.add("open");
      } catch (err) {
        hideSuggest(which);
      }
    }

    function setActiveSuggest(which, i) {
      suggestState[which].active = i;
      [...suggestListEl(which).children].forEach((child, index) => {
        child.classList.toggle("active", index === i);
      });
    }

    function pickSuggest(which, i, keepSuggestions = false) {
      const e = (suggestCache[which] || [])[i];
      if (!e) return;
      suggestInputEl(which).value = e.path;
      setActiveSuggest(which, i);
      if (!keepSuggestions) {
        hideSuggest(which);
        if (which === "local") browseLocalGo(e.path);
        else browseRemoteGo(e.path);
      }
    }

    function onSuggestKey(ev, which) {
      const listEl = suggestListEl(which);
      if (!listEl.classList.contains("open")) return;
      const items = suggestCache[which] || [];
      if (ev.key === "ArrowDown" || ev.key === "ArrowUp") {
        ev.preventDefault();
        if (!items.length) return;
        let a = suggestState[which].active + (ev.key === "ArrowDown" ? 1 : -1);
        if (suggestState[which].active < 0) a = ev.key === "ArrowDown" ? 0 : items.length - 1;
        a = (a + items.length) % items.length;
        setActiveSuggest(which, a);
      } else if (ev.key === "Tab") {
        if (!items.length) return;
        ev.preventDefault();
        const current = suggestState[which].active;
        const step = ev.shiftKey ? -1 : 1;
        const a = current < 0
          ? (step > 0 ? 0 : items.length - 1)
          : (current + step + items.length) % items.length;
        pickSuggest(which, a, true);
      } else if (ev.key === "Enter") {
        if (items.length) {
          ev.preventDefault();
          pickSuggest(which, suggestState[which].active < 0 ? 0 : suggestState[which].active);
        }
      } else if (ev.key === "Escape") {
        hideSuggest(which);
      }
    }

    async function submitAddSync() {
      const localPath = document.getElementById("sync-local-path").value.trim();
      const remotePath = document.getElementById("sync-remote-path").value.trim();
      const direction = document.getElementById("sync-direction").value;
      const mirror = document.getElementById("sync-mirror").checked;
      const always = document.getElementById("sync-always").checked;
      let interval = parseInt(document.getElementById("sync-interval").value, 10);
      if (isNaN(interval)) interval = 15;
      if (!localPath || !remotePath) {
        alert("Pick both a local folder and a remote folder (use Browse for quick select).");
        return;
      }
      const isEdit = !!editingSyncId;
      const url = isEdit ? "/api/syncs/update" : "/api/syncs";
      const payload = isEdit
        ? { id: editingSyncId, local_path: localPath, remote_path: remotePath, direction: direction, mirror: mirror, always: always, interval: interval, run_now: true }
        : { server_id: currentServerId, local_path: localPath, remote_path: remotePath, direction: direction, mirror: mirror, always: always, interval: interval, run_now: true };
      closeSyncModal();
      showToast(isEdit ? `Saving folder sync...` : `Starting folder sync...`);
      try {
        const res = await fetch(url, {
          method: "POST",
          headers: {"Content-Type": "application/json"},
          body: JSON.stringify(payload)
        });
        const d = await res.json();
        showToast(d.message || (d.ok ? "Sync complete" : "Sync failed"));
        fetchSyncs();
        fetchServers();
      } catch (err) {
        alert("Failed to save sync: " + err);
      }
    }

    fetchServers().then(() => { fetchStatus(); fetchDocker(); fetchSyncs(); });
    setInterval(fetchStatus, 4000);
    setInterval(fetchDocker, 5000);
    setInterval(fetchSyncs, 8000);
    setInterval(fetchLocalPorts, 10000);
    setInterval(fetchServers, 15000);
  </script>
</body>
</html>
"""


# -----------------------------
# HTTP REQUEST HANDLER
# -----------------------------

class DashboardHandler(BaseHTTPRequestHandler):
    def _send_json(self, data, status=200):
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Access-Control-Allow-Origin", "*")
        self.end_headers()
        self.wfile.write(json.dumps(data).encode("utf-8"))

    def _read_json(self):
        content_length = int(self.headers.get("Content-Length", 0))
        if content_length > 0:
            raw = self.rfile.read(content_length)
            return json.loads(raw.decode("utf-8"))
        return {}

    def do_HEAD(self):
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.end_headers()

    def _server_param(self, query, body=None):
        # ?server=<id|ssh_host> on GET, or server_id/server field on POST
        srv = query.get("server", [None])[0] or query.get("server_id", [None])[0]
        if not srv and isinstance(body, dict):
            srv = body.get("server_id") or body.get("server")
        return srv

    def do_GET(self):
        parsed = urllib.parse.urlparse(self.path)
        path = parsed.path
        query = urllib.parse.parse_qs(parsed.query)
        if path == "/" or path == "/index.html":
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.end_headers()
            self.wfile.write(HTML_DASHBOARD.encode("utf-8"))
        elif path in ("/favicon.png", "/favicon.ico", "/favicon-32x32.png", "/assets/favicon.png"):
            try:
                data = get_favicon_bytes()
            except Exception:
                data = b""
            if not data:
                self.send_response(404)
                self.end_headers()
                return
            self.send_response(200)
            self.send_header("Content-Type", "image/png")
            self.send_header("Content-Length", str(len(data)))
            self.send_header("Cache-Control", "public, max-age=86400")
            self.end_headers()
            self.wfile.write(data)
        elif path == "/api/status":
            srv = query.get("server", [None])[0] or query.get("server_id", [None])[0]
            data = get_all_forwards_status(srv)
            data["servers"] = get_servers_status()
            self._send_json(data)
        elif path == "/api/servers":
            self._send_json({"servers": get_servers_status()})
        elif path == "/api/ssh-hosts":
            cfg = load_config()
            added_hosts = {s.get("ssh_host") for s in cfg.get("servers", [])}
            added_ids = {s.get("ssh_host"): s.get("id") for s in cfg.get("servers", [])}
            hosts = []
            for h in get_ssh_config_hosts():
                hosts.append({
                    **h,
                    "added": h["ssh_host"] in added_hosts,
                    "server_id": added_ids.get(h["ssh_host"]),
                })
            self._send_json({"hosts": hosts})
        elif path == "/api/scan":
            srv = query.get("server", [None])[0] or query.get("server_id", [None])[0]
            self._send_json({"services": scan_remote_services(srv)})
        elif path == "/api/docker":
            srv = query.get("server", [None])[0] or query.get("server_id", [None])[0]
            self._send_json(get_docker_status(srv))
        elif path == "/api/docker/logs":
            srv = query.get("server", [None])[0] or query.get("server_id", [None])[0]
            container = query.get("container", [""])[0]
            if not container:
                self._send_json({"ok": False, "message": "container is required", "logs": ""}, status=400)
            else:
                self._send_json(get_docker_logs(srv, container, query.get("tail", [200])[0]))
        elif path == "/api/docker/labels":
            self._send_json({"labels": get_docker_labels()})
        elif path == "/api/history":
            srv = query.get("server", [None])[0] or query.get("server_id", [None])[0]
            self._send_json({"history": get_port_history(limit=10, server_id=srv)})
        elif path == "/api/local-ports":
            try:
                self._send_json({"ports": get_local_listening_ports()})
            except Exception as e:
                self._send_json({"ports": [], "message": str(e)})
        elif path == "/api/syncs":
            srv = query.get("server", [None])[0] or query.get("server_id", [None])[0]
            try:
                self._send_json(get_syncs_status(srv))
            except Exception as e:
                self._send_json({"syncs": [], "folder_history": [], "message": str(e)})
        elif path == "/api/folder-history":
            srv = query.get("server", [None])[0] or query.get("server_id", [None])[0]
            self._send_json({"history": get_folder_history(limit=10, server_id=srv)})
        elif path == "/api/browse/local":
            p = query.get("path", [""])[0]
            try:
                self._send_json(browse_local(p))
            except Exception as e:
                self._send_json({"ok": False, "path": p, "parent": "/", "home": os.path.expanduser("~"), "entries": [], "message": str(e)})
        elif path == "/api/browse/remote":
            srv = query.get("server", [None])[0] or query.get("server_id", [None])[0]
            p = query.get("path", [""])[0]
            try:
                self._send_json(browse_remote(srv, p))
            except Exception as e:
                self._send_json({"ok": False, "path": p, "parent": "/", "entries": [], "message": str(e)})
        else:
            self.send_response(404)
            self.end_headers()

    def do_POST(self):
        parsed = urllib.parse.urlparse(self.path)
        path = parsed.path
        try:
            body = self._read_json()
        except Exception:
            body = {}

        if path == "/api/forward":
            lp = body.get("local_port")
            rp = body.get("remote_port", lp)
            label = body.get("label", "")
            always = body.get("always", False)
            srv = body.get("server_id") or body.get("server")
            add_forward(lp, rp, label=label, always=always, server_ref=srv)
            self._send_json({"ok": True, "message": f"Port {lp} forwarded successfully"})
        elif path == "/api/remove":
            lp = body.get("local_port")
            srv = body.get("server_id") or body.get("server")
            remove_forward(lp, server_ref=srv)
            self._send_json({"ok": True, "message": f"Port {lp} removed"})
        elif path == "/api/toggle":
            lp = body.get("local_port")
            always = body.get("always", False)
            srv = body.get("server_id") or body.get("server")
            toggle_always(lp, always, server_ref=srv)
            self._send_json({"ok": True, "message": f"Port {lp} persistence updated"})
        elif path == "/api/clean":
            srv = body.get("server_id") or body.get("server")
            killed = clean_orphaned_tunnels(server_ref=srv)
            self._send_json({"ok": True, "killed": killed})
        elif path == "/api/docker/labels":
            labels = body.get("labels")
            if not isinstance(labels, list):
                self._send_json({"ok": False, "message": "labels must be a list"}, status=400)
                return
            clean = []
            seen = set()
            for raw in labels:
                if not isinstance(raw, dict):
                    continue
                name = str(raw.get("name") or "").strip()
                match = str(raw.get("match") or "").strip()
                color = str(raw.get("color") or "#8b949e").strip()
                if not name or not match:
                    continue
                ident = str(raw.get("id") or re.sub(r"[^a-z0-9]+", "-", name.lower()).strip("-") or uuid.uuid4().hex[:8])
                if ident in seen:
                    continue
                seen.add(ident)
                if not re.fullmatch(r"#[0-9a-fA-F]{6}", color):
                    color = "#8b949e"
                clean.append({"id": ident, "name": name[:80], "match": match[:120], "color": color, "enabled": bool(raw.get("enabled", True))})
            cfg = load_config()
            cfg["docker_labels"] = clean
            save_config(cfg)
            self._send_json({"ok": True, "labels": clean})
        elif path == "/api/local-ports/kill":
            pid = body.get("pid")
            if pid is None:
                self._send_json({"ok": False, "message": "pid is required"}, status=400)
                return
            result = kill_listening_process(pid)
            self._send_json(result, status=200 if result.get("ok") else 500)
        elif path == "/api/servers":
            ssh_host = (body.get("ssh_host") or "").strip()
            name = (body.get("name") or "").strip()
            ip = (body.get("ip") or "").strip()
            if not ssh_host:
                self._send_json({"ok": False, "message": "ssh_host is required"}, status=400)
                return
            try:
                server = add_server(ssh_host, name=name or ssh_host, ip=ip)
            except ValueError as e:
                self._send_json({"ok": False, "message": str(e)}, status=400)
                return
            self._send_json({"ok": True, "server": server, "message": f"Server '{ssh_host}' added"})
        elif path == "/api/servers/remove":
            sid = body.get("id") or body.get("server_id") or body.get("server")
            if not sid:
                self._send_json({"ok": False, "message": "server id is required"}, status=400)
                return
            try:
                ok = remove_server_entry(sid)
            except ValueError as e:
                self._send_json({"ok": False, "message": str(e)}, status=400)
                return
            if not ok:
                self._send_json({"ok": False, "message": "Server not found"}, status=404)
                return
            self._send_json({"ok": True, "message": "Server removed"})
        elif path == "/api/servers/reorder":
            order = body.get("order", [])
            servers = reorder_servers(order)
            self._send_json({"ok": True, "servers": servers})
        elif path == "/api/servers/pin":
            sid = body.get("id") or body.get("server_id") or body.get("server")
            pinned = body.get("pinned", True)
            server = set_server_pinned(sid, pinned)
            if not server:
                self._send_json({"ok": False, "message": "Server not found"}, status=404)
                return
            self._send_json({"ok": True, "server": server})
        elif path == "/api/servers/update":
            sid = body.get("id") or body.get("server_id") or body.get("server")
            server = update_server_entry(sid, name=body.get("name"), ip=body.get("ip"))
            if not server:
                self._send_json({"ok": False, "message": "Server not found"}, status=404)
                return
            self._send_json({"ok": True, "server": server})
        elif path == "/api/syncs":
            try:
                result = add_sync(
                    server_ref=body.get("server_id") or body.get("server"),
                    local_path=body.get("local_path", ""),
                    remote_path=body.get("remote_path", ""),
                    direction=body.get("direction", SYNC_DEFAULT_DIRECTION),
                    mirror=bool(body.get("mirror", False)),
                    always=bool(body.get("always", False)),
                    interval=body.get("interval", SYNC_DEFAULT_INTERVAL),
                    run_now=bool(body.get("run_now", True)),
                )
            except ValueError as e:
                self._send_json({"ok": False, "message": str(e)}, status=400)
                return
            except Exception as e:
                self._send_json({"ok": False, "message": f"Failed to add sync: {e}"}, status=500)
                return
            self._send_json(result, status=200 if result.get("ok") else 500)
        elif path == "/api/syncs/remove":
            sid = body.get("id") or body.get("sync_id")
            if not sid:
                self._send_json({"ok": False, "message": "sync id is required"}, status=400)
                return
            ok = remove_sync_entry(sid)
            if not ok:
                self._send_json({"ok": False, "message": "Sync not found"}, status=404)
                return
            self._send_json({"ok": True, "message": "Folder sync removed"})
        elif path == "/api/syncs/run":
            sid = body.get("id") or body.get("sync_id")
            if not sid:
                self._send_json({"ok": False, "message": "sync id is required"}, status=400)
                return
            res = run_sync(sid)
            self._send_json({"ok": bool(res.get("ok")), "message": res.get("message", "")},
                            status=200 if res.get("ok") else 500)
        elif path == "/api/syncs/toggle":
            sid = body.get("id") or body.get("sync_id")
            if not sid:
                self._send_json({"ok": False, "message": "sync id is required"}, status=400)
                return
            sync = toggle_sync_always(sid, bool(body.get("always", False)),
                                      interval=body.get("interval"))
            if not sync:
                self._send_json({"ok": False, "message": "Sync not found"}, status=404)
                return
            mode = "Auto (persistent)" if sync.get("always") else "Once (one-time)"
            self._send_json({"ok": True, "sync": sync, "message": f"Sync mode: {mode}"})
        elif path == "/api/syncs/update":
            sid = body.get("id") or body.get("sync_id")
            if not sid:
                self._send_json({"ok": False, "message": "sync id is required"}, status=400)
                return
            try:
                sync = update_sync(
                    sid,
                    local_path=body.get("local_path") if "local_path" in body else None,
                    remote_path=body.get("remote_path") if "remote_path" in body else None,
                    direction=body.get("direction") if "direction" in body else None,
                    mirror=body.get("mirror") if "mirror" in body else None,
                    always=body.get("always") if "always" in body else None,
                    interval=body.get("interval") if "interval" in body else None,
                )
            except ValueError as e:
                self._send_json({"ok": False, "message": str(e)}, status=400)
                return
            if not sync:
                self._send_json({"ok": False, "message": "Sync not found"}, status=404)
                return
            result = {"ok": True, "sync": dict(sync), "message": "Folder sync updated"}
            if bool(body.get("run_now", True)):
                res = run_sync(sid)
                try:
                    sync2 = get_sync(load_config(), sid)
                    if sync2:
                        result["sync"] = dict(sync2)
                except Exception:
                    pass
                result["run"] = res
                result["message"] = res.get("message", result["message"])
                result["ok"] = bool(res.get("ok"))
            self._send_json(result, status=200 if result.get("ok") else 500)
        elif path == "/api/browse/mkdir":
            which = (body.get("which") or "").strip().lower()
            if which == "local":
                try:
                    result = mkdir_local(body.get("path", ""), body.get("name", ""))
                except ValueError as e:
                    self._send_json({"ok": False, "message": str(e)}, status=400)
                    return
                self._send_json(result, status=200 if result.get("ok") else 500)
            elif which == "remote":
                try:
                    result = mkdir_remote(body.get("server_id") or body.get("server"),
                                          body.get("path", ""), body.get("name", ""))
                except ValueError as e:
                    self._send_json({"ok": False, "message": str(e)}, status=400)
                    return
                self._send_json(result, status=200 if result.get("ok") else 500)
            else:
                self._send_json({"ok": False, "message": "which must be 'local' or 'remote'"}, status=400)
        else:
            self.send_response(404)
            self.end_headers()

    def log_message(self, format, *args):
        return


class ReusableHTTPServer(ThreadingHTTPServer):
    """Threaded local server that can immediately retake its dashboard port."""
    allow_reuse_address = True
    daemon_threads = True


def serve(port=DEFAULT_DASHBOARD_PORT):
    # Auto sync agents execute the app's native launcher. Recreate them on
    # each packaged-app start so an update always refreshes their path.
    if get_packaged_app_executable():
        restore_packaged_forward_agents()
        restore_auto_sync_agents()
    server = ReusableHTTPServer(("127.0.0.1", port), DashboardHandler)
    print(f"DevBoost Dashboard running at http://localhost:{port}")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nStopping dashboard server.")


# -----------------------------
# CLI COMMANDS
# -----------------------------

def _extract_server_flag(args):
    """Extracts --server <id|host> / -s <id|host> from CLI args. Returns (server_ref, remaining_args)."""
    server_ref = None
    remaining = []
    i = 0
    while i < len(args):
        if args[i] in ("--server", "-s") and i + 1 < len(args):
            server_ref = args[i + 1]
            i += 2
        elif args[i].startswith("--server="):
            server_ref = args[i].split("=", 1)[1]
            i += 1
        else:
            remaining.append(args[i])
            i += 1
    return server_ref, remaining


def cli_list(server_ref=None, show_all=False):
    cfg = load_config()
    servers = get_servers(cfg)
    targets = servers if show_all else [resolve_server(cfg, server_ref)]
    for status in [get_all_forwards_status(s.get("id")) for s in targets]:
        reachable = "🟢 Online" if status["server_reachable"] else "🔴 Offline"
        display_title = f"{status['server_name']} ({status['server_host']})"
        print(f"\n{display_title} • {reachable}")
        print("=" * 80)
        forwards = status["forwards"]
        if not forwards:
            print("No active or configured port forwards.")
            print("Use 'devboost add <port>' to forward a port.")
            continue

        header = f"{'LOCAL':<10}{'REMOTE':<10}{'MODE':<12}{'STATUS':<18}{'SERVICE / LABEL':<25}"
        print(header)
        print("-" * 80)
        for f in forwards:
            local_str = f":{f['local_port']}"
            remote_str = f":{f['remote_port']}"
            mode_str = "ALWAYS" if f["always"] else "SESSION"
            status_str = f"ACTIVE (PID {f['pid']})" if f["active"] else "STOPPED"
            label_str = (f["label"] or "")[:24]
            owner = (f.get("conflict_with") or {}).get("name") or (f.get("conflict_with") or {}).get("ssh_host")
            flag = f" ⚠️ in use by {owner}" if f.get("conflict") and owner else (" ⚠️" if f.get("conflict") else "")
            print(f"{local_str:<10}{remote_str:<10}{mode_str:<12}{status_str:<18}{label_str:<25}{flag}")
        print("=" * 80)
        if status["orphaned_count"] > 0:
            print(f"⚠️  {status['orphaned_count']} duplicate/orphaned SSH processes detected. Run 'devboost clean' to clean them up.")
    print(f"Dashboard: http://localhost:{DEFAULT_DASHBOARD_PORT}\n")


def cli_servers():
    servers = get_servers_status()
    print("\nConfigured SSH connections (tabs):")
    print("=" * 78)
    print(f"{'ID':<24}{'NAME':<24}{'SSH HOST':<20}{'TABS':<10}")
    print("-" * 78)
    for s in servers:
        flags = f"{s['active_count']} active"
        if s.get("pinned"):
            flags += " 📍"
        print(f"{s['id']:<24}{(s['name'] or '')[:23]:<24}{(s['ssh_host'] or '')[:19]:<20}{flags:<10}")
    print("=" * 78)
    hosts = get_ssh_config_hosts()
    if hosts:
        cfg = load_config()
        added = {srv.get("ssh_host") for srv in cfg.get("servers", [])}
        available = [h for h in hosts if h["ssh_host"] not in added]
        if available:
            print("\nAvailable in ~/.ssh/config (add with: devboost server add <host>):")
            for h in available[:20]:
                detail = h.get("hostname", "")
                print(f"  {h['ssh_host']:<24} {detail}")
    print("")


def cli_scan(server_ref=None):
    cfg = load_config()
    server = resolve_server(cfg, server_ref)
    print(f"\nScanning listening services on {server.get('ssh_host')}...")
    services = scan_remote_services(server.get("id"))
    if not services:
        print("No remote services found or server unreachable.")
        return
    print("=" * 70)
    print(f"{'PORT':<10}{'PROCESS':<20}{'IDENTIFIED SERVICE':<35}")
    print("-" * 70)
    for s in services:
        print(f":{s['port']:<9}{s['process']:<20}{s['label']:<35}")
    print("=" * 70)
    print("To forward any port, run: devboost add <port> [--always]\n")


def cli_docker(server_ref=None):
    cfg = load_config()
    server = resolve_server(cfg, server_ref)
    print(f"\nChecking Docker on {server.get('ssh_host')}...")
    # CLI users expect a result now; the dashboard uses the cached worker path.
    snapshot = collect_docker_snapshot(server.get("ssh_host"))
    if snapshot.get("available") is not True:
        print(snapshot.get("message") or "Docker is unavailable on the server.")
        return
    containers = snapshot.get("containers", [])
    if not containers:
        print("Docker is available, but no containers are running.")
        return
    print("=" * 120)
    print(f"{'NAME':<24}{'STATUS':<28}{'CPU':<10}{'MEMORY':<24}{'NET I/O':<24}{'IMAGE'}")
    print("-" * 120)
    for container in containers:
        stats = container.get("stats") or {}
        print(
            f"{container.get('name', '')[:23]:<24}"
            f"{container.get('status', '')[:27]:<28}"
            f"{stats.get('cpu_percent', ''):<10}"
            f"{stats.get('memory_usage', '')[:23]:<24}"
            f"{stats.get('network_io', '')[:23]:<24}"
            f"{container.get('image', '')}"
        )
    print("=" * 120)


def cli_lazydocker(server_ref=None):
    """Open the interactive lazydocker TUI on the selected SSH server."""
    cfg = load_config()
    server = resolve_server(cfg, server_ref)
    host = server.get("ssh_host")
    print(f"Opening lazydocker on {host}...")
    result = subprocess.run([
        "ssh", "-t", "-o", "BatchMode=yes", "-o", "ConnectTimeout=5",
        host, "lazydocker",
    ])
    if result.returncode != 0:
        print("Could not start lazydocker. Check that it is installed on the server and that SSH works.")


def print_history_hint(server_ref=None):
    history = get_port_history(limit=10, server_id=server_ref)
    if not history:
        return
    print("\nPreviously used ports (quick select):")
    print("-" * 60)
    for h in history:
        remote = f" -> :{h['remote_port']}" if h.get("remote_port") != h.get("local_port") else ""
        print(f"  :{h['local_port']}{remote:<12} {h.get('label', '')}")
    print("\nReuse with: devboost add <port> [--always]\n")


def _format_sync_age(ts):
    if not ts:
        return "never"
    try:
        delta = time.time() - float(ts)
    except (TypeError, ValueError):
        return "never"
    if delta < 60:
        return f"{int(delta)}s ago"
    if delta < 3600:
        return f"{int(delta // 60)}m ago"
    if delta < 86400:
        return f"{int(delta // 3600)}h ago"
    return time.strftime("%Y-%m-%d %H:%M", time.localtime(ts))


def cli_sync_list(server_ref=None, show_all=False):
    cfg = load_config()
    servers = get_servers(cfg)
    targets = servers if show_all else [resolve_server(cfg, server_ref)]
    for srv in targets:
        status = get_syncs_status(srv.get("id"))
        print(f"\nFolder syncs • {status['server_name']} ({status['server_host']})")
        print("=" * 100)
        if not status["syncs"]:
            print("No folder syncs configured.")
            print("Use 'devboost sync add <local> <remote> [--auto]' to add one.")
            continue
        print(f"{'ID':<10}{'DIRECTION':<10}{'MODE':<10}{'LAST SYNC':<14}{'LOCAL':<28}{'REMOTE'}")
        print("-" * 100)
        for s in status["syncs"]:
            mode = "AUTO" if s["always"] else "ONCE"
            if s["mirror"] and s["direction"] in ("push", "pull"):
                mode += "+MIRROR"
            last = _format_sync_age(s.get("last_sync"))
            st = (s.get("last_status") or "never").upper()
            print(f"{s['id']:<10}{s['direction']:<10}{mode:<10}{last:<14}{(s['local_path'] or '')[:27]:<28}{s['remote_path']}  [{st}]")
        print("=" * 100)
    print(f"Dashboard: http://localhost:{DEFAULT_DASHBOARD_PORT}\n")


def print_help():
    print("""DevBoost • SSH Port Forward Manager

Usage:
  devboost                       List forwards for the default server tab
  devboost ls [--server ID] [--all]   List forwards (one tab or all tabs)
  devboost add <port> [remote] [--server ID] [--always]   Forward a port
  devboost add                   Show previously used ports for quick select
  devboost rm <port> [--server ID]    Remove a port forward and stop its tunnel
  devboost clean [--server ID]   Kill lingering duplicate/orphaned SSH processes
  devboost scan [--server ID]    Scan listening ports on a remote server
  devboost docker [--server ID]  Show cached Docker container stats on a remote server
  devboost lazydocker [--server ID]  Open the interactive lazydocker TUI over SSH
  devboost server list           List SSH connection tabs
  devboost server add <ssh-host> [display-name]   Add a tab (host should exist in ~/.ssh/config)
  devboost server rm <id>        Remove a tab (stops its tunnels)
  devboost server pin <id> [--off]    Pin/unpin a tab (pinned tabs sort first)
  devboost sync [--server ID] [--all]   List folder syncs (tracked mirrors)
  devboost sync add <local> <remote> [--server ID] [--direction two-way|push|pull] [--mirror] [--auto] [--interval N] [--no-run]
                                      Add a folder sync (runs once now unless --no-run)
  devboost sync run <id>         Run a folder sync now
  devboost sync edit <id> [--local PATH] [--remote PATH] [--direction two-way|push|pull] [--mirror|--no-mirror] [--auto|--once] [--interval N] [--no-run]
                                      Edit a folder sync (runs once now unless --no-run)
  devboost sync rm <id>          Remove a folder sync (stops its agent)
  devboost sync auto <id> [--off] [--interval N]   Make a sync Auto (persistent) or Once (one-time)
  devboost ui / dashboard        Open the web dashboard in Chrome/browser
  devboost serve [--port 3080]   Run the web dashboard server

Folder sync modes (see dashboard ? help):
  direction two-way (default) = bidirectional merge, newer wins, deletions never propagate.
  direction push/pull + --mirror = exact copy (rsync --delete, deletions propagate).
  --auto = persistent background agent (WatchPaths + polling every --interval sec).
  Without --auto the sync is one-time (runs now + on-demand via Sync Now).

Tabs: the dashboard shows one tab per SSH connection. Add tabs from
~/.ssh/config hosts (selective — nothing is auto-added), then drag to
reorder and pin important ones. Per-command target a tab with --server.
""")


def main():
    args = sys.argv[1:]
    if not args or args[0] in ("ls", "list", "status"):
        server_ref, rest = _extract_server_flag(args[1:] if args else [])
        show_all = "--all" in rest
        # `devboost` bare with no servers configured still works via migration
        cli_list(server_ref=server_ref, show_all=show_all)
        return

    cmd = args[0]
    if cmd in ("-h", "--help", "help"):
        print_help()
    elif cmd in ("ui", "dashboard", "open"):
        subprocess.run(["open", f"http://localhost:{DEFAULT_DASHBOARD_PORT}"])
    elif cmd == "serve":
        port = DEFAULT_DASHBOARD_PORT
        if len(args) >= 3 and args[1] in ("-p", "--port"):
            port = int(args[2])
        serve(port)
    elif cmd == "server":
        if len(args) < 2:
            cli_servers()
            return
        sub = args[1]
        if sub == "list":
            cli_servers()
        elif sub == "add":
            if len(args) < 3:
                print("Error: Specify an SSH host. e.g. 'devboost server add my-remote-server'")
                print("Available hosts in ~/.ssh/config:")
                for h in get_ssh_config_hosts()[:20]:
                    print(f"  {h['ssh_host']}")
                sys.exit(1)
            ssh_host = args[2]
            name = args[3] if len(args) > 3 else ssh_host
            try:
                server = add_server(ssh_host, name=name)
            except ValueError as e:
                print(f"Error: {e}")
                sys.exit(1)
            print(f"Server tab '{server['name']}' ({server['ssh_host']}) added.")
        elif sub in ("rm", "remove", "del", "delete"):
            if len(args) < 3:
                print("Error: Specify a server id. e.g. 'devboost server rm my-host'")
                sys.exit(1)
            try:
                ok = remove_server_entry(args[2])
            except ValueError as e:
                print(f"Error: {e}")
                sys.exit(1)
            print("Server tab removed." if ok else "Server not found.")
        elif sub == "pin":
            if len(args) < 3:
                print("Error: Specify a server id. e.g. 'devboost server pin my-host'")
                sys.exit(1)
            pinned = "--off" not in args
            server = set_server_pinned(args[2], pinned)
            print(f"Server '{args[2]}' {'pinned 📍' if pinned else 'unpinned'}." if server else "Server not found.")
        else:
            print_help()
    elif cmd == "clean":
        server_ref, _ = _extract_server_flag(args[1:])
        killed = clean_orphaned_tunnels(server_ref=server_ref)
        print(f"Cleaned up {killed} orphaned SSH forward process(es).")
    elif cmd == "scan":
        server_ref, _ = _extract_server_flag(args[1:])
        cli_scan(server_ref=server_ref)
    elif cmd == "docker":
        server_ref, _ = _extract_server_flag(args[1:])
        cli_docker(server_ref=server_ref)
    elif cmd == "lazydocker":
        server_ref, _ = _extract_server_flag(args[1:])
        cli_lazydocker(server_ref=server_ref)
    elif cmd in ("add", "forward"):
        server_ref, filtered = _extract_server_flag(args[1:])
        fargs = [cmd] + filtered
        if len(fargs) < 2:
            print("Error: Specify at least a port number. e.g. 'devboost add 8080'")
            print_history_hint(server_ref=server_ref)
            sys.exit(1)
        lp = int(fargs[1])
        rp = lp
        always = "--always" in fargs or "-a" in fargs
        name = ""
        remaining = [a for a in fargs[2:] if a not in ("--always", "-a")]
        if remaining:
            if remaining[0].isdigit():
                rp = int(remaining[0])
                remaining = remaining[1:]
        if remaining:
            name = " ".join(remaining)
        add_forward(lp, rp, label=name, always=always, server_ref=server_ref)
        cfg = load_config()
        ssh_host = resolve_server(cfg, server_ref).get("ssh_host")
        mode = "persistent (ALWAYS)" if always else "temporary (SESSION)"
        print(f"Port {lp} -> {ssh_host}:{rp} forwarded [{mode}].")
    elif cmd in ("rm", "remove", "del", "delete"):
        server_ref, filtered = _extract_server_flag(args[1:])
        fargs = [cmd] + filtered
        if len(fargs) < 2:
            print("Error: Specify a port number to remove. e.g. 'devboost rm 8080'")
            sys.exit(1)
        lp = int(fargs[1])
        remove_forward(lp, server_ref=server_ref)
        print(f"Port {lp} forward removed.")
    elif cmd == "sync-run":
        # Hidden entry point for sync LaunchAgents: `devboost.py sync-run <id>`
        if len(args) < 2 or not args[1].strip():
            print("Error: sync-run requires a sync id.")
            sys.exit(1)
        res = run_sync(args[1].strip())
        print(res.get("message", ""))
        sys.exit(0 if res.get("ok") else 1)
    elif cmd == "refresh-agents":
        # Hidden entry point used by the packaged runner after each rebuild.
        forwards = restore_packaged_forward_agents()
        restore_auto_sync_agents()
        print(f"Refreshed {forwards} persistent forward agent(s).")
    elif cmd == "sync":
        server_ref, filtered = _extract_server_flag(args[1:])
        rest = filtered[1:] if filtered and filtered[0] == "sync" else filtered
        # `devboost sync` bare == list
        sub = rest[0] if rest else "list"
        if sub in ("list", "ls", "status"):
            show_all = "--all" in rest
            cli_sync_list(server_ref=server_ref, show_all=show_all)
        elif sub == "add":
            positional = [a for a in rest[1:] if not a.startswith("--")]
            flags = {a for a in rest[1:] if a.startswith("--")}
            direction = SYNC_DEFAULT_DIRECTION
            interval = SYNC_DEFAULT_INTERVAL
            for a in rest[1:]:
                if a.startswith("--direction="):
                    direction = a.split("=", 1)[1]
                elif a.startswith("--interval="):
                    try:
                        interval = int(a.split("=", 1)[1])
                    except ValueError:
                        pass
            if len(positional) >= 2 and positional[0].startswith("--direction"):
                pass
            # Support `--direction X` (space-separated) form
            if "--direction" in rest[1:]:
                try:
                    direction = rest[rest.index("--direction") + 1]
                except IndexError:
                    pass
            if "--interval" in rest[1:]:
                try:
                    interval = int(rest[rest.index("--interval") + 1])
                except (IndexError, ValueError):
                    pass
            if len(positional) < 2:
                print("Error: Specify local and remote folders. e.g. 'devboost sync add ~/projects/app ~/projects/app'")
                print("Previously used folders:")
                for h in get_folder_history(limit=10, server_id=server_ref):
                    print(f"  local={h.get('local_path')} remote={h.get('remote_path')}")
                sys.exit(1)
            mirror = "--mirror" in flags
            always = "--auto" in flags or "--always" in flags
            run_now = "--no-run" not in flags
            try:
                result = add_sync(server_ref=server_ref, local_path=positional[0],
                                 remote_path=positional[1], direction=direction,
                                 mirror=mirror, always=always, interval=interval,
                                 run_now=run_now)
            except ValueError as e:
                print(f"Error: {e}")
                sys.exit(1)
            print(result.get("message", "Folder sync added"))
            if not result.get("ok"):
                sys.exit(1)
        elif sub in ("rm", "remove", "del", "delete"):
            if len(rest) < 2:
                print("Error: Specify a sync id. e.g. 'devboost sync rm a1b2c3d4'")
                sys.exit(1)
            ok = remove_sync_entry(rest[1])
            print("Folder sync removed." if ok else "Sync not found.")
        elif sub == "run":
            if len(rest) < 2:
                print("Error: Specify a sync id. e.g. 'devboost sync run a1b2c3d4'")
                sys.exit(1)
            res = run_sync(rest[1])
            print(res.get("message", ""))
            if not res.get("ok"):
                sys.exit(1)
        elif sub == "auto":
            if len(rest) < 2:
                print("Error: Specify a sync id. e.g. 'devboost sync auto a1b2c3d4'")
                sys.exit(1)
            make_always = "--off" not in rest
            interval = None
            for a in rest:
                if a.startswith("--interval="):
                    try:
                        interval = int(a.split("=", 1)[1])
                    except ValueError:
                        pass
            if "--interval" in rest:
                try:
                    interval = int(rest[rest.index("--interval") + 1])
                except (IndexError, ValueError):
                    pass
            sync = toggle_sync_always(rest[1], make_always, interval=interval)
            if not sync:
                print("Sync not found.")
                sys.exit(1)
            print(f"Sync '{rest[1]}' is now {'AUTO (persistent)' if make_always else 'ONCE (one-time)'}.")
        elif sub == "edit":
            if len(rest) < 2:
                print("Error: Specify a sync id. e.g. 'devboost sync edit a1b2c3d4 --remote ~/new-path'")
                sys.exit(1)
            args = rest[1:]
            sid = args[0]
            kwargs = {}
            i = 1
            while i < len(args):
                a = args[i]
                if a.startswith("--local="):
                    kwargs["local_path"] = a.split("=", 1)[1]
                elif a == "--local" and i + 1 < len(args):
                    kwargs["local_path"] = args[i + 1]
                    i += 1
                elif a.startswith("--remote="):
                    kwargs["remote_path"] = a.split("=", 1)[1]
                elif a == "--remote" and i + 1 < len(args):
                    kwargs["remote_path"] = args[i + 1]
                    i += 1
                elif a.startswith("--direction="):
                    kwargs["direction"] = a.split("=", 1)[1]
                elif a == "--direction" and i + 1 < len(args):
                    kwargs["direction"] = args[i + 1]
                    i += 1
                elif a == "--mirror":
                    kwargs["mirror"] = True
                elif a == "--no-mirror":
                    kwargs["mirror"] = False
                elif a.startswith("--interval="):
                    kwargs["interval"] = a.split("=", 1)[1]
                elif a == "--interval" and i + 1 < len(args):
                    kwargs["interval"] = args[i + 1]
                    i += 1
                elif a in ("--auto", "--always"):
                    kwargs["always"] = True
                elif a == "--once":
                    kwargs["always"] = False
                i += 1
            run_now = "--no-run" not in args
            if "interval" in kwargs:
                try:
                    kwargs["interval"] = int(kwargs["interval"])
                except (TypeError, ValueError):
                    print("Error: --interval must be a number of seconds.")
                    sys.exit(1)
            try:
                sync = update_sync(sid, **kwargs)
            except ValueError as e:
                print(f"Error: {e}")
                sys.exit(1)
            if not sync:
                print("Sync not found.")
                sys.exit(1)
            if run_now:
                res = run_sync(sid)
                print(res.get("message", "Folder sync updated."))
                if not res.get("ok"):
                    sys.exit(1)
            else:
                print(f"Sync '{sid}' updated.")
        else:
            print_help()
    else:
        print_help()


if __name__ == "__main__":
    main()
