from __future__ import annotations

import base64
import hashlib
import json
import os
import platform
import subprocess
import uuid
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any

from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import padding


def _b64url_encode(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).decode("utf-8").rstrip("=")


def _b64url_decode(text: str) -> bytes:
    s = str(text or "").strip()
    if not s:
        return b""
    s += "=" * ((4 - len(s) % 4) % 4)
    return base64.urlsafe_b64decode(s.encode("utf-8"))


def _normalize_machine_code(text: str) -> str:
    s = str(text or "").strip().upper()
    # 只保留十六进制字符，统一重新格式化，避免用户复制时混入空格、换行、不同横线字符。
    hex_only = "".join(ch for ch in s if ch in "0123456789ABCDEF")
    if len(hex_only) == 32:
        return f"{hex_only[:8]}-{hex_only[8:16]}-{hex_only[16:24]}-{hex_only[24:32]}"
    return s


def _try_cmd(cmd: list[str]) -> str:
    try:
        out = subprocess.check_output(cmd, stderr=subprocess.DEVNULL, timeout=4)
        return out.decode("utf-8", errors="ignore").strip()
    except Exception:
        return ""


def machine_fingerprint() -> str:
    """跨平台获取机器指纹原始串（尽量稳定，不保证绝对唯一）。"""
    sysname = platform.system().lower()

    if sysname == "darwin":
        v = _try_cmd(["ioreg", "-rd1", "-c", "IOPlatformExpertDevice"])
        if v:
            for line in v.splitlines():
                if "IOPlatformUUID" in line:
                    parts = line.split("=")
                    if len(parts) >= 2:
                        return parts[-1].replace('"', "").strip()

    if sysname == "windows":
        v = _try_cmd(["wmic", "csproduct", "get", "uuid"])
        if v:
            lines = [x.strip() for x in v.splitlines() if x.strip() and "uuid" not in x.lower()]
            if lines:
                return lines[0]
        v = _try_cmd(["powershell", "-NoProfile", "-Command", "(Get-CimInstance Win32_ComputerSystemProduct).UUID"])
        if v:
            return v.splitlines()[-1].strip()

    # fallback
    host = platform.node()
    mac = f"{uuid.getnode():012x}"
    raw = f"{platform.system()}|{host}|{mac}"
    return raw


def machine_code() -> str:
    raw = machine_fingerprint().encode("utf-8")
    h = hashlib.sha256(raw).hexdigest().upper()
    return _normalize_machine_code(f"{h[:8]}-{h[8:16]}-{h[16:24]}-{h[24:32]}")


@dataclass
class LicenseState:
    valid: bool
    message: str
    machine_code: str
    payload: dict[str, Any] | None = None


def _load_public_key(pubkey_pem: str):
    data = Path(pubkey_pem).read_bytes()
    return serialization.load_pem_public_key(data)


def parse_and_verify_token(token: str, pubkey_pem: str) -> dict[str, Any]:
    text = str(token or "").strip()
    if "." not in text:
        raise RuntimeError("授权码格式错误")
    p1, p2 = text.split(".", 1)
    payload_bytes = _b64url_decode(p1)
    sig_bytes = _b64url_decode(p2)
    if not payload_bytes or not sig_bytes:
        raise RuntimeError("授权码格式错误")

    pub = _load_public_key(pubkey_pem)
    pub.verify(
        sig_bytes,
        payload_bytes,
        padding.PSS(mgf=padding.MGF1(hashes.SHA256()), salt_length=padding.PSS.MAX_LENGTH),
        hashes.SHA256(),
    )
    payload = json.loads(payload_bytes.decode("utf-8"))
    if not isinstance(payload, dict):
        raise RuntimeError("授权码载荷无效")
    return payload


def validate_payload(payload: dict[str, Any], *, product: str, local_machine_code: str) -> None:
    p = str(payload.get("product", "")).strip()
    if p and p != product:
        raise RuntimeError("授权码不适用于当前产品")

    m = _normalize_machine_code(str(payload.get("machine_code", "")))
    if not m:
        raise RuntimeError("授权码缺少机器码")
    local_mc = _normalize_machine_code(local_machine_code)
    if m != local_mc:
        raise RuntimeError(
            "授权码与当前机器码不匹配"
            f"\n授权码机器码：{m}"
            f"\n当前机器码：{local_mc}"
        )

    exp = str(payload.get("expires_at", "")).strip()
    if exp:
        # 支持 YYYY-MM-DD
        try:
            exp_dt = datetime.strptime(exp, "%Y-%m-%d").date()
        except Exception as e:
            raise RuntimeError(f"授权码过期字段格式错误: {e}") from e
        if datetime.now().date() > exp_dt:
            raise RuntimeError("授权码已过期")


def check_local_license(
    *,
    license_file: str,
    public_key_file: str,
    product: str,
) -> LicenseState:
    mc = machine_code()

    if not Path(public_key_file).exists():
        return LicenseState(False, "未找到公钥文件（config/license_public.pem）", mc, None)

    lf = Path(license_file)
    if not lf.exists():
        return LicenseState(False, "未激活，请先输入授权码", mc, None)

    try:
        raw = json.loads(lf.read_text(encoding="utf-8"))
        token = str((raw or {}).get("token", "")).strip()
        if not token:
            return LicenseState(False, "授权文件缺少 token", mc, None)
        payload = parse_and_verify_token(token, public_key_file)
        validate_payload(payload, product=product, local_machine_code=mc)
        return LicenseState(True, "已激活", mc, payload)
    except Exception as e:
        return LicenseState(False, f"授权校验失败：{e}", mc, None)


def activate_and_save(
    *,
    token: str,
    license_file: str,
    public_key_file: str,
    product: str,
) -> LicenseState:
    mc = machine_code()
    payload = parse_and_verify_token(token, public_key_file)
    validate_payload(payload, product=product, local_machine_code=mc)

    lf = Path(license_file)
    lf.parent.mkdir(parents=True, exist_ok=True)
    lf.write_text(
        json.dumps(
            {
                "token": str(token).strip(),
                "machine_code": mc,
                "activated_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
            },
            ensure_ascii=False,
            indent=2,
        ),
        encoding="utf-8",
    )
    return LicenseState(True, "激活成功", mc, payload)


def build_token(payload: dict[str, Any], private_key_file: str) -> str:
    priv = serialization.load_pem_private_key(Path(private_key_file).read_bytes(), password=None)
    body = json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
    sig = priv.sign(
        body,
        padding.PSS(mgf=padding.MGF1(hashes.SHA256()), salt_length=padding.PSS.MAX_LENGTH),
        hashes.SHA256(),
    )
    return f"{_b64url_encode(body)}.{_b64url_encode(sig)}"
