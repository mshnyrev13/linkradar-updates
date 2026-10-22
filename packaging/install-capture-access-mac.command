#!/bin/sh
set -eu
test "$(/usr/bin/id -u)" != 0 || { echo 'Run as your regular user, not root.' >&2; exit 1; }
task_uid=$(/usr/bin/id -u)
task_action=install
task_gui=no
for argument in "$@"; do
    case "$argument" in
        --gui) task_gui=yes;;
        --remove) task_action=remove;;
        *) echo 'Unknown argument.' >&2; exit 1;;
    esac
done
task_directory=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)
task_payload=$(/bin/cat "$task_directory/bpf-access-root.sh")
# Freeze the entire payload BEFORE the authorization prompt. Elevated execution
# never reads a script from a user-writable app or temporary directory.
if test "$task_gui" = yes; then
    /usr/bin/osascript - "$task_payload" "$task_uid" "$task_action" <<'APPLESCRIPT'
on run argv
    set payload to item 1 of argv
    set command to "VCS_BPF_SOURCE=" & quoted form of payload & linefeed & "VCS_BPF_UID=" & quoted form of item 2 of argv & linefeed & "VCS_BPF_ACTION=" & quoted form of item 3 of argv & linefeed & payload
    do shell script command with administrator privileges
end run
APPLESCRIPT
else
    # sh -c receives the already-read snapshot; no elevated re-read of this file.
    # The already-read script is passed as data, not reloaded after elevation.
    /usr/bin/sudo /bin/sh -c 'VCS_BPF_UID=$1; VCS_BPF_ACTION=$2; VCS_BPF_SOURCE=$3; eval "$3"' sh "$task_uid" "$task_action" "$task_payload"
fi
