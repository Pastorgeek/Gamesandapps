#!/bin/bash
# Double-click this file to start the Hashtag Photo Wall on your Mac.
cd "$(dirname "$0")" || exit 1

if ! command -v python3 >/dev/null 2>&1 || ! python3 -c "import sys" >/dev/null 2>&1; then
  echo "Python 3 is needed to run the Photo Wall."
  echo "Install it from https://www.python.org/downloads/macos/ and then double-click start.command again."
  open "https://www.python.org/downloads/macos/"
  read -n 1 -s -r -p "Press any key to close this window..."
  exit 1
fi

python3 server.py
