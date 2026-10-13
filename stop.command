#!/bin/bash
cd "$(dirname "$0")"
touch KILL
echo "KILL file created - the bot will square off all positions and stop within a few seconds."
echo "Delete the KILL file before you start the bot again."
read -p "Press Enter to close"
