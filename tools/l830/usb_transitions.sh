#!/usr/bin/env bash
# Passive USB recorder for the Fibocom L830-EB (vendor 0x2cb7): every ~50 ms, log each change of the
# device's (sessionID, idProduct, bcdDevice), or ABSENT. It never opens the device. The first time a
# product ID other than 0x0210 (528) shows up (0x0203 = the firmware's boot stage), dump the full ioreg.
#   usb_transitions.sh SECONDS OUTDIR      -> OUTDIR/transitions.log ("<epoch> <session> <pid> <bcd>")
set -uo pipefail
dur="${1:-}"; out="${2:-}"
[[ "$dur" =~ ^[0-9]+$ && -d "$out" ]] || { echo "usage: $0 SECONDS OUTDIR" >&2; exit 2; }
end=$((SECONDS + dur)); prev=""; dumped=""
while (( SECONDS < end )); do
    t=$(perl -MTime::HiRes=time -e 'printf "%.3f", time')
    cur=$(ioreg -p IOUSB -l -w0 | awk '
        /\+-o /{s=b=p=v=""}
        /"sessionID"/{s=$NF} /"bcdDevice"/{b=$NF} /"idProduct"/{p=$NF} /"idVendor"/{v=$NF}
        v==11447 && s!="" && b!="" && p!="" {print s, p, b; exit}')
    [[ -z "$cur" ]] && cur="ABSENT"
    if [[ "$cur" != "$prev" ]]; then
        echo "$t $cur" >> "$out/transitions.log"
        prev="$cur"
        pid=$(awk '{print $2}' <<< "$cur")
        if [[ "$cur" != ABSENT && "$pid" != 528 && -z "$dumped" ]]; then
            ioreg -l -w0 > "$out/ioreg_pid_$pid.txt"
            dumped=1
        fi
    fi
done
