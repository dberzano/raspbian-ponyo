#!/usr/bin/env python3

import json
from pathlib import Path
import sys

from requests import get
from requests.exceptions import RequestException


def read_configuration():
    cfg_fn = Path(__file__).resolve().parent / "notificator.json"
    with open(cfg_fn) as fp:
        configurations = json.load(fp)
    return configurations


def sendmsg(msg, config="default"):
    configurations = read_configuration()
    try:
        token = configurations[config]["token"]
        chat_id = configurations[config]["chat_id"]
    except KeyError:
        fatal(f"configuration {config} not found, try {', '.join(configurations.keys())}")

    try:
        r = get(f"https://api.telegram.org/bot{token}/sendMessage",
                data={"chat_id": chat_id,
                      "parse_mode": "Markdown",
                      "disable_web_page_preview": True,
                      "text": msg})
        r.raise_for_status()
    except RequestException as e:
        fatal(f"cannot send message: {e}")


if __name__ == "__main__":
    # Running as a script
    def fatal(msg):
        sys.stderr.write(msg + "\n")
        sys.exit(1)

    try:
        config = sys.argv[2]
    except IndexError:
        config = "default"

    try:
        message = sys.argv[1]
    except IndexError:
        fatal("no message specified")

    sendmsg(message, config)


else:
    # Imported
    def fatal(msg):
        raise RuntimeError(msg)
