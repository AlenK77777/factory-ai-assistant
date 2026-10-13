#!/bin/sh
# Alpine resolves host.docker.internal to IPv6 first, and that address is not
# reachable from this container. Use the IPv4 gateway from /etc/hosts instead.
# The password file is mounted read-only and may be mode 600, which the worker
# cannot open. Copy it somewhere the worker can read.
set -e
IP=$(awk '$2 == "host.docker.internal" && $1 !~ /:/ { print $1; exit }' /etc/hosts)
if [ -z "$IP" ]; then
  echo "host.docker.internal has no IPv4 address in /etc/hosts" >&2
  exit 1
fi
cp /etc/nginx/auth/htpasswd /tmp/htpasswd
chmod 644 /tmp/htpasswd
sed -e "s/host\\.docker\\.internal/${IP}/g" -e "s|/etc/nginx/auth/htpasswd|/tmp/htpasswd|g" /etc/nginx/nginx.conf > /tmp/nginx.conf
exec nginx -c /tmp/nginx.conf -g "daemon off;"
