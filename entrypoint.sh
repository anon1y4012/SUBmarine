#!/bin/sh
set -e

# When started as root (the default), repair /data ownership for volumes
# created by older root-only images, then drop to the unprivileged user.
if [ "$(id -u)" = "0" ]; then
    chown -R submarine:submarine /data
    exec gosu submarine "$0" "$@"
fi

python -c "from app import init_db; init_db(); print('DB initialized')"
exec gunicorn --bind 0.0.0.0:5000 --workers "${WEB_WORKERS:-2}" --timeout 120 app:app
