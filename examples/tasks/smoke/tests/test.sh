#!/bin/sh
mkdir -p /logs/verifier
if [ "$(cat /done.txt 2>/dev/null)" = "OK" ]; then
  echo "1.0" > /logs/verifier/reward.txt
else
  echo "0.0" > /logs/verifier/reward.txt
fi
