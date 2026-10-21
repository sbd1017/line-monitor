#!/bin/bash
# Create (or keep) speedmon's site proxy key and the matching nginx snippet, both root 0600.
#   sudo scripts/site-proxy-key.sh     then: systemctl reload nginx && systemctl restart speedmon
# nginx sends the key as X-Site-Proxy-Key; with SITE_AUTH=1 the app trusts the central-login headers only with it.
# The key never appears on a command line. Rotate: delete both files and run this again.
set -euo pipefail
[ "$(id -u)" = 0 ] || { echo "run with sudo" >&2; exit 1; }
install -d -m 0700 -o root -g root /etc/speedmon
python3 - <<'PY'
import os, secrets
key_path, snip_path = "/etc/speedmon/site-proxy-key", "/etc/nginx/snippets/site-speedtest-proxy-key.conf"
def write(path, text):
    fd = os.open(path + ".new", os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w") as f:
        f.write(text)
    os.replace(path + ".new", path)
try:
    key = open(key_path).read().strip()
except FileNotFoundError:
    key = ""
if len(key) < 32:
    key = secrets.token_urlsafe(48)
    write(key_path, key + "\n")
write(snip_path, 'proxy_set_header X-Site-Proxy-Key "%s";\n' % key)
for path in (key_path, snip_path):
    os.chown(path, 0, 0)
    os.chmod(path, 0o600)
PY
echo "site proxy key present (root 0600, key and nginx snippet match)"
