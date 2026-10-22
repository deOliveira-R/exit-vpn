#!/bin/sh
# A note in the connection monitor's log, e.g. when a connection just dropped:
#   monitor/mark.sh "video call froze"
LOG_DIR=${LOG_DIR:-$HOME/Library/Logs/exit-vpn}
exec /usr/bin/python3 -c 'import json,sys,time,pathlib
t=time.time(); p=pathlib.Path(sys.argv[1])/time.strftime("connection-%Y-%m-%d.jsonl",time.gmtime(t))
p.open("a").write(json.dumps({"ts":round(t,3),"kind":"mark","note":" ".join(sys.argv[2:])})+"\n")' "$LOG_DIR" "$@"
