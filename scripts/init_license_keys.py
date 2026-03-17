#!/usr/bin/env python3
from __future__ import annotations

from pathlib import Path

from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import rsa


def main() -> None:
    root = Path(__file__).resolve().parents[1]
    priv_path = root / ".runtime" / "license_private.pem"
    pub_path = root / "config" / "license_public.pem"
    priv_path.parent.mkdir(parents=True, exist_ok=True)
    pub_path.parent.mkdir(parents=True, exist_ok=True)

    if priv_path.exists() and pub_path.exists():
        print("密钥已存在：")
        print(f"  Private: {priv_path}")
        print(f"  Public : {pub_path}")
        return

    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    priv_pem = key.private_bytes(
        encoding=serialization.Encoding.PEM,
        format=serialization.PrivateFormat.PKCS8,
        encryption_algorithm=serialization.NoEncryption(),
    )
    pub_pem = key.public_key().public_bytes(
        encoding=serialization.Encoding.PEM,
        format=serialization.PublicFormat.SubjectPublicKeyInfo,
    )
    priv_path.write_bytes(priv_pem)
    pub_path.write_bytes(pub_pem)
    print("✅ 已生成授权密钥对：")
    print(f"  Private: {priv_path}  （仅作者保管，千万不要发给用户）")
    print(f"  Public : {pub_path}  （随软件一起发布）")


if __name__ == "__main__":
    main()

