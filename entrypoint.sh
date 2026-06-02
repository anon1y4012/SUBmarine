#!/bin/sh
set -e
python -c "from app import init_db; init_db(); print('DB initialized')"
exec gunicorn --bind 0.0.0.0:5000 --workers 2 --timeout 120 app:app
