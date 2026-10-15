#!/bin/sh
# Container entry point.
#
# Two things differ from running the binary on a desktop:
#
# 1. There is nobody to press a key. When arguments were given the run is a
#    scripted one -- compose, cron, CI -- so --no-pause is appended to skip the
#    "press any key" prompt. Without arguments the guided wizard runs, and there
#    keeping the console open is the whole point, so nothing is appended.
#
# 2. The upstream image makes you name the command ("docker run img icloudpd
#    ..."). Here `icloud` selects the other console script and anything else is
#    passed straight to icloudpd, so both spellings work.
set -eu

if [ "$#" -gt 0 ] && [ "$1" = "icloud" ]; then
    shift
    exec icloud "$@"
fi

if [ "$#" -gt 0 ] && [ "$1" = "icloudpd" ]; then
    shift
fi

if [ "$#" -eq 0 ]; then
    exec icloudpd
fi

for arg in "$@"; do
    case "$arg" in
        --no-pause|--help|-h|--version)
            # --no-pause is already there, or argparse exits before it would
            # ever look at another option
            exec icloudpd "$@"
            ;;
    esac
done

exec icloudpd "$@" --no-pause
