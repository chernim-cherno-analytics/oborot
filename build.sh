#!/bin/bash
set -e
echo "=== Installing Node ==="
curl -fsSL https://deb.nodesource.com/setup_20.x | bash -
apt-get install -y nodejs
echo "=== Building frontend ==="
cd frontend
npm install
npm run build
cd ..
echo "=== Installing Python deps ==="
pip install -r backend/requirements.txt
echo "=== Done ==="
