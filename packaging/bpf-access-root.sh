#!/bin/sh
# Fixed root operations only. Installed copy is root-owned; never run app code as root.
set -eu
PATH=/usr/bin:/bin:/usr/sbin:/sbin
export PATH
umask 077
label=tech.vcs.network-watch.bpf
helper_dir='/Library/PrivilegedHelperTools/tech.vcs.network-watch.capture'
helper="$helper_dir/bpf-access.sh"
plist="/Library/LaunchDaemons/$label.plist"
group=access_bpf
test "$(/usr/bin/id -u)" = 0 || { echo 'Administrator authorization required.' >&2; exit 1; }

apply_access() {
    # Opening an unattached BPF descriptor creates nodes; no interface/filter is set.
    maximum=$(/usr/sbin/sysctl -n debug.bpf_maxdevices)
    case "$maximum" in ''|*[!0-9]*) exit 1;; esac
    if test "$maximum" -gt 64; then maximum=64; fi
    index=0
    while test "$index" -lt "$maximum"; do
        ( : < "/dev/bpf$index" ) 2>/dev/null || true
        index=$((index + 1))
    done
    found=0
    for device in /dev/bpf[0-9]*; do
        test -c "$device" && ! test -L "$device" || continue
        /usr/bin/chgrp "$group" "$device"
        /bin/chmod g+rw "$device"
        found=1
    done
    test "$found" = 1 || { echo 'No BPF devices available.' >&2; return 1; }
}

if test "${1:-}" = --apply; then
    apply_access
    exit
fi

uid=${VCS_BPF_UID:-}
case "$uid" in ''|*[!0-9]*) echo 'Invalid user id.' >&2; exit 1;; esac
test "$uid" -ge 501 || { echo 'Use a regular macOS account.' >&2; exit 1; }
user=$(/usr/bin/id -un "$uid")

# Do not follow existing symlinks or adopt files/directories owned by another user.
for path in /Library/PrivilegedHelperTools /Library/LaunchDaemons "$helper_dir" "$helper" "$plist"; do
    test ! -L "$path" || { echo 'Unsafe helper path.' >&2; exit 1; }
    if test -e "$path"; then
        test "$(/usr/bin/stat -f %u "$path")" = 0 || { echo 'Helper path is not root-owned.' >&2; exit 1; }
        mode=$(/usr/bin/stat -f %Lp "$path")
        test "$((0$mode & 022))" = 0 || { echo 'Helper path is writable by others.' >&2; exit 1; }
        test "$(/bin/ls -lde "$path" | /usr/bin/wc -l)" -eq 1 || { echo 'Unexpected helper path ACL.' >&2; exit 1; }
    fi
done

if test "${VCS_BPF_ACTION:-install}" = remove; then
    /bin/launchctl bootout "system/$label" 2>/dev/null || true
    /bin/rm -f "$plist" "$helper"
    /bin/rmdir "$helper_dir" 2>/dev/null || true
    echo 'LinkRadar BPF helper removed. Shared access_bpf group and membership retained.'
    exit
fi
test "${VCS_BPF_ACTION:-install}" = install || exit 1
test -n "${VCS_BPF_SOURCE:-}" || { echo 'Missing helper payload.' >&2; exit 1; }

if ! /usr/bin/dscl . -read "/Groups/$group" >/dev/null 2>&1; then
    /usr/sbin/dseditgroup -q -o create -r 'Packet capture access' "$group"
fi
# Do not add the admin group or any account other than the requesting user.
/usr/sbin/dseditgroup -q -o edit -a "$user" -t user "$group"
/usr/bin/install -d -o root -g wheel -m 755 "$helper_dir"
/bin/chmod -N "$helper_dir"
temporary=$(/usr/bin/mktemp "$helper_dir/.install.XXXXXX")
temporary_plist=$(/usr/bin/mktemp "$helper_dir/.plist.XXXXXX")
trap '/bin/rm -f "$temporary" "$temporary_plist"' EXIT HUP INT TERM
printf '%s\n' "$VCS_BPF_SOURCE" > "$temporary"
/bin/chmod 755 "$temporary"
/bin/mv -f "$temporary" "$helper"
cat > "$temporary_plist" <<'PLIST'
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0"><dict>
<key>Label</key><string>tech.vcs.network-watch.bpf</string>
<key>ProgramArguments</key><array><string>/Library/PrivilegedHelperTools/tech.vcs.network-watch.capture/bpf-access.sh</string><string>--apply</string></array>
<key>RunAtLoad</key><true/>
<key>WatchPaths</key><array><string>/dev</string></array>
<key>ThrottleInterval</key><integer>10</integer>
</dict></plist>
PLIST
/usr/bin/plutil -lint "$temporary_plist" >/dev/null
/usr/bin/install -o root -g wheel -m 644 "$temporary_plist" "$plist"
if /bin/launchctl print "system/$label" >/dev/null 2>&1; then
    /bin/launchctl bootout "system/$label"
fi
/bin/launchctl bootstrap system "$plist"
apply_access
echo 'LinkRadar BPF access installed for the requesting account.'
