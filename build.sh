#!/bin/bash
set -e

echo "=== Installing Python dependencies ==="
pip install -r requirements.txt

echo "=== Installing Node dependencies ==="
npm install
npm run build

echo "=== Build complete ==="
