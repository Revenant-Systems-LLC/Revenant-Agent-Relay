"""Read and edit the DPAPI secret container without ever writing plaintext to disk.

The .dpapi file is JSON — {version, scheme, encrypted} — where `encrypted` is a
base64 DPAPI blob holding ordinary KEY=VALUE env text. DPAPI is bound to the
current Windows user, so this only works as Dave on this machine. That is the
whole security model: the file can sit on any drive and still be useless to
anyone else.

`dpapi_decrypt.py` already had encrypt_bytes(); nothing ever called it. This is
the missing writer.

Usage:
    python tools/secret_tool.py list                       # key names + SET/EMPTY, no values
    python tools/secret_tool.py show  RR_REDDIT_CLIENT_ID  # print one value
    python tools/secret_tool.py set   RR_REDDIT_CLIENT_ID  # prompts, input hidden
    python tools/secret_tool.py unset RR_SNAPCHAT_PASSWORD # delete the line entirely

`set` prompts rather than taking the value as an argument, so secrets never land
in shell history. Every write backs the file up first — it holds credentials for
five platforms and a bad write would lose all of them.
"""

from __future__ import annotations

import base64
import json
import shutil
import sys
from datetime import datetime
from getpass import getpass
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from src.dpapi_decrypt import decrypt_bytes, encrypt_bytes  # noqa: E402

SECRETS = Path(r"A:\env\revenant-relay.dpapi")
_VERSION = 1
_SCHEME = "windows-dpapi-current-user"


def _read() -> tuple[dict, str]:
    """Return the raw JSON payload and the decrypted env text."""
    payload = json.loads(SECRETS.read_text(encoding="utf-8"))
    if payload.get("version") != _VERSION:
        raise SystemExit(f"Unsupported version: {payload.get('version')!r}")
    if payload.get("scheme") != _SCHEME:
        raise SystemExit(f"Unsupported scheme: {payload.get('scheme')!r}")
    text = decrypt_bytes(base64.b64decode(payload["encrypted"])).decode("utf-8")
    return payload, text


def _write(payload: dict, text: str) -> None:
    """Re-encrypt and write, preserving every other field in the JSON."""
    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    backup = SECRETS.with_suffix(f".dpapi.bak-{stamp}")
    shutil.copy2(SECRETS, backup)

    payload["encrypted"] = base64.b64encode(encrypt_bytes(text.encode("utf-8"))).decode("ascii")
    SECRETS.write_text(json.dumps(payload, indent=2), encoding="utf-8")

    # Read it back. A file that cannot be decrypted is worse than no change.
    _, verify = _read()
    if verify != text:
        shutil.copy2(backup, SECRETS)
        raise SystemExit(f"Verification failed — restored from {backup.name}")
    print(f"OK. Backup: {backup.name}")


def _pairs(text: str) -> dict[str, str]:
    out = {}
    for line in text.splitlines():
        line = line.strip()
        if line and not line.startswith("#") and "=" in line:
            k, _, v = line.partition("=")
            out[k.strip()] = v.strip()
    return out


def cmd_list() -> None:
    for k, v in sorted(_pairs(_read()[1]).items()):
        print(f"  {k:<28} {'SET (len=' + str(len(v)) + ')' if v else 'EMPTY'}")


def cmd_show(key: str) -> None:
    val = _pairs(_read()[1]).get(key)
    if val is None:
        raise SystemExit(f"{key} not found")
    print(val if val else "(empty)")


def cmd_set(key: str) -> None:
    payload, text = _read()
    value = getpass(f"Value for {key} (hidden): ").strip()
    if not value:
        raise SystemExit("Empty value, nothing changed")

    lines = text.splitlines()
    for i, line in enumerate(lines):
        if line.strip().startswith(f"{key}="):
            lines[i] = f"{key}={value}"
            break
    else:
        lines.append(f"{key}={value}")  # new key, keep existing order intact

    _write(payload, "\n".join(lines) + "\n")


def cmd_unset(key: str) -> None:
    """Remove a key outright, rather than blanking it.

    Blanking leaves the name behind, which reads as "configured but empty" to
    anyone looking later. If the credential is gone, the line should be gone.
    """
    payload, text = _read()
    lines = text.splitlines()
    kept = [ln for ln in lines if not ln.strip().startswith(f"{key}=")]
    if len(kept) == len(lines):
        raise SystemExit(f"{key} not found — nothing changed")

    print(f"Removing {key} ({len(lines)} keys -> {len(kept)})")
    _write(payload, "\n".join(kept) + "\n")


if __name__ == "__main__":
    args = sys.argv[1:]
    if not args or args[0] not in {"list", "show", "set", "unset"}:
        raise SystemExit(__doc__)
    if args[0] == "list":
        cmd_list()
    elif len(args) < 2:
        raise SystemExit(f"{args[0]} needs a key name")
    elif args[0] == "show":
        cmd_show(args[1])
    elif args[0] == "set":
        cmd_set(args[1])
    else:
        cmd_unset(args[1])
