#!/usr/bin/env python3
from notificator import sendmsg
from random import choice
from requests import get, RequestException
import sys
from time import sleep

def main():
    domain = choice([
        "golang.org",
        "doubleclick.net",
        "fonts.googleapis.com",
        "photos.google.com",
        "peering.google.com",
        "youtube.com"
    ])
    timeout_s = 5
    sleep_s = 1
    attempts = 20
    success = 0

    for i in range(1, attempts+1):
        try:
            get(f"https://{domain}", timeout=timeout_s)
            success += 1
        except RequestException as e:
            pass
        print(f"testing `{domain}`: attempt {i} - success {success} ({100*success/attempts}%) "
              f"- remaining {attempts-i}")
        sleep(sleep_s)

    # Send message through notifier
    uni = ("✅", "❌")
    msg = f"{uni[int(success != attempts)]} *Internet test* [{domain}](https://{domain}) " \
          f"success {success}/{attempts} ({100*success/attempts}%)"
    sendmsg(msg, "internet_status")

    return int(success != attempts)

if __name__ == "__main__":
    sys.exit(main())
