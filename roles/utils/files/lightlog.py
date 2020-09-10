#!/usr/bin/env python3

from argparse import ArgumentParser
from datetime import datetime, timedelta
import json
from pathlib import Path
import subprocess
import sys

from klein import route, run
from twisted.internet import reactor, task, threads
from twisted.internet.defer import inlineCallbacks, returnValue
from twisted.python import log


store_prefix = None
after_dump_cmd = None
cmd_timeout = None
write_buffer = {}


def jsonize(d, req=None):
    if req:
        req.setHeader("Content-Type", "application/json")
    return json.dumps(
        d, default=lambda o: o.isoformat() + "Z" if isinstance(o, datetime) else o.__dict__
    )


def shard_name(thing, year, month, day):
    return store_prefix / thing / f"{year:04d}" / f"{month:02d}" / f"{day:02d}.json"


@route("/write/<thing>", methods=["POST"])
def write(req, thing):
    global write_buffer
    try:
        buf = {k.decode("utf-8"): float(v[0]) for (k, v) in req.args.items()}
        buf["timestamp"] = datetime.utcnow()
    except ValueError:
        req.setResponseCode(400)
        return jsonize({"error": "only floats supported"}, req)
    key = shard_name(thing, buf["timestamp"].year, buf["timestamp"].month, buf["timestamp"].day)
    write_buffer[key] = write_buffer.get(key, []) + [buf]
    return jsonize(buf, req)


@route("/read/<thing>/<year>/<month>/<day>.json", methods=["GET"])
@inlineCallbacks
def read(req, thing, year, month, day):
    req.setHeader("Access-Control-Allow-Origin", "*")
    try:
        year = int(year)
        month = int(month)
        day = int(day)
    except ValueError:
        req.setResponseCode(400)
        errmsg = yield {"error": "invalid numbers in date"}
        returnValue(jsonize(errmsg, req))
    data = yield threads.deferToThread(async_read, thing, year, month, day)
    if not data:
        req.setResponseCode(404)
    returnValue(jsonize(data, req))


def async_read(thing, year, month, day):
    try:
        fn = shard_name(thing, year, month, day)
        with open(fn) as fp:
            data = json.loads(fp.read())
    except IOError:
        log.msg(f"shard {fn} not found, returning empty data")
        return []
    except ValueError:
        log.msg(f"JSON data is corrupted on shard {fn}")
        return []
    if fn in write_buffer:
        data = data + write_buffer[fn]
    return data


@inlineCallbacks
def dump_buf():
    global write_buffer
    if not write_buffer:
        return
    wb = write_buffer.copy()
    write_buffer = {}
    yield threads.deferToThread(async_dump_buf, wb)


def async_dump_buf(wb):
    for shard, content in wb.items():
        assert isinstance(shard, Path)
        content.sort(key=lambda x: x["timestamp"])
        try:
            with open(shard) as fp:
                content = json.loads(fp.read()) + content
        except:  # NOQA
            log.msg(f"cannot read shard {shard} from store, creating")
        shard.parent.mkdir(exist_ok=True, parents=True)
        shard_tmp = shard.with_name(f"{shard.name}.0")
        with open(shard_tmp, "w") as fp:
            fp.write(jsonize(content))
        shard_tmp.rename(shard)
    run_helper_cmd(after_dump_cmd)


def run_helper_cmd(cmd_template):
    if not cmd_template:
        log.msg("no command specified")
        return 0

    # Command context
    toda = datetime.utcnow()
    yest = toda - timedelta(days=1)
    toda = f"{toda.year:04d}/{toda.month:02d}/{toda.day:02d}.json"
    yest = f"{yest.year:04d}/{yest.month:02d}/{yest.day:02d}.json"
    fetch_shards = f"{yest},{toda}"

    # Command, with context
    cmd = cmd_template.format(store_prefix=str(store_prefix),
                              fetch_shards=fetch_shards)

    log.msg(f"running command `{cmd}`")
    popen = subprocess.Popen(
        ["timeout", "-s", "9", str(cmd_timeout), "bash", "-c", cmd],
        shell=False,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
    )
    out = popen.communicate()[0]
    log.msg(f"command returned {popen.returncode}")
    error_or_not = "ok" if popen.returncode == 0 else f"error {popen.returncode}"
    for line in out.decode("utf-8").split("\n"):
        log.msg(f"{error_or_not}: {line}")
    return popen.returncode


@inlineCallbacks
def startup_cmd_and_schedule(every):
    log.msg("executing initial fetch data command")
    yield threads.deferToThread(run_helper_cmd, before_start_cmd)
    log.msg(f"scheduling dump every {every}s")
    dump_task = task.LoopingCall(dump_buf)
    dump_task.start(every)


if __name__ == "__main__":
    parser = ArgumentParser()
    parser.add_argument("--store",
                        dest="store",
                        default="~/.lightlog", help="Where is the datastore")
    parser.add_argument("--before-start-cmd",
                        dest="before_start_cmd",
                        default="",
                        help="Command to execute before starting execution (to prepare input)")
    parser.add_argument("--after-dump-cmd",
                        dest="after_dump_cmd",
                        default="",
                        help="Command to execute after writing data (to dump output)")
    parser.add_argument("--host",
                        dest="host",
                        default="localhost",
                        help="Listen on this host")
    parser.add_argument("--port",
                        dest="port",
                        default=4242,
                        type=int,
                        help="Listen on this port")
    parser.add_argument("--dump-every",
                        dest="sync_every",
                        default=300,
                        type=int,
                        help="Dump data every SYNC_EVERY seconds")
    parser.add_argument("--cmd-timeout",
                        dest="cmd_timeout",
                        default=60,
                        type=int,
                        help="Kill after dump command after CMD_TIMEOUT seconds")
    args = parser.parse_args()
    store_prefix = Path(args.store).expanduser()
    after_dump_cmd = args.after_dump_cmd
    before_start_cmd = args.before_start_cmd
    cmd_timeout = args.cmd_timeout
    reactor.callLater(2, startup_cmd_and_schedule, args.sync_every)
    run(args.host, args.port)
    sys.exit(0)
