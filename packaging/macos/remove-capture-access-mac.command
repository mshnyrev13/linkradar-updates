#!/bin/sh
set -eu
task_directory=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)
exec /bin/sh "$task_directory/install-capture-access-mac.command" --remove "$@"
