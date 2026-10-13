"""Write nginx/auth/htpasswd using the same crypt as Alpine NGINX.

NGINX checks passwords with musl crypt(). A hash from OpenSSL's passwd
command is not always accepted: Apache $apr1$ is unsupported, and a
SHA-256 hash copied through the shell can pick up an extra character.
"""
import os
import pathlib
import warnings

warnings.filterwarnings("ignore", category=DeprecationWarning)
import crypt

password = pathlib.Path("/tmp/password").read_text(encoding="utf-8")
username = os.environ["ADMIN_USER"]
hashed = crypt.crypt(password, crypt.METHOD_SHA256)
if crypt.crypt(password, hashed) != hashed:
    raise SystemExit("Generated hash does not verify.")
path = pathlib.Path("/auth/htpasswd")
path.write_text(f"{username}:{hashed}\n", encoding="utf-8")
os.chmod(path, 0o644)
