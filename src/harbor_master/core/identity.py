"""Who is running: name, affiliation, email, GitHub handle, signing key (DESIGN.md Section 8)."""
from __future__ import annotations

import base64
import os
import tomllib
from dataclasses import dataclass
from pathlib import Path

import tomli_w
from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey, Ed25519PublicKey


def config_dir() -> Path:
    return Path(os.environ.get("HM_CONFIG_DIR", Path.home() / ".config" / "harbor-master"))


@dataclass
class Identity:
    name: str
    affiliation: str
    email: str
    github: str
    public_key: str  # OpenSSH format, "ssh-ed25519 AAAA..."

    def label(self) -> str:
        return f"{self.name} ({self.affiliation}, @{self.github})"

    def sign(self, message: str) -> str:
        key = serialization.load_pem_private_key((config_dir() / "signing_key").read_bytes(), password=None)
        return base64.b64encode(key.sign(message.encode())).decode()


def verify(public_key: str, message: str, signature: str) -> bool:
    try:
        key = serialization.load_ssh_public_key(public_key.encode())
        if not isinstance(key, Ed25519PublicKey):
            return False
        key.verify(base64.b64decode(signature), message.encode())
        return True
    except (InvalidSignature, ValueError, TypeError):
        return False


def create(name: str, affiliation: str, email: str, github: str) -> Identity:
    """Write identity.toml and a new signing key. Keeps an existing key."""
    d = config_dir()
    d.mkdir(parents=True, exist_ok=True)
    key_path = d / "signing_key"
    if key_path.exists():
        key = serialization.load_pem_private_key(key_path.read_bytes(), password=None)
    else:
        key = Ed25519PrivateKey.generate()
        key_path.touch(mode=0o600)
        key_path.write_bytes(key.private_bytes(
            serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8, serialization.NoEncryption()))
    public = key.public_key().public_bytes(
        serialization.Encoding.OpenSSH, serialization.PublicFormat.OpenSSH).decode()
    ident = Identity(name.strip(), affiliation.strip(), email.strip(), github.strip().lstrip("@"), public)
    (d / "identity.toml").write_text(tomli_w.dumps({
        "name": ident.name, "affiliation": ident.affiliation, "email": ident.email,
        "github": ident.github, "public_key": ident.public_key}))
    return ident


def load() -> Identity | None:
    path = config_dir() / "identity.toml"
    if not path.exists() or not (config_dir() / "signing_key").exists():
        return None
    data = tomllib.loads(path.read_text())
    try:
        return Identity(data["name"], data["affiliation"], data["email"], data["github"], data["public_key"])
    except KeyError:
        return None
