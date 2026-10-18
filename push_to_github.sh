#!/usr/bin/env bash
set -e
if [ -z "$1" ]; then
  echo "Usage: ./push_to_github.sh https://github.com/<your-username>/<your-repo>.git"
  exit 1
fi
git init -b main
git add .
git commit -m "Initial commit: OrderBridge WhatsApp Order Capture Suite" || true
git branch -M main
git remote remove origin 2>/dev/null || true
git remote add origin "$1"
git push -u origin main
echo "Successfully pushed OrderBridge to $1"
