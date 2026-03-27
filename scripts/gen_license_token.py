#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
from datetime import datetime, timedelta
from pathlib import Path


def main() -> None:
    root = Path(__file__).resolve().parents[1]
    import sys
    sys.path.insert(0, str(root / "src"))

    from takealot_autolister.licensing import build_token, _normalize_machine_code

    parser = argparse.ArgumentParser(description="生成绑定机器码的授权码（卡密）")
    parser.add_argument("--machine", required=True, help="机器码，例如 ABCD1234-EF567890-...")
    parser.add_argument("--card-id", required=True, help="卡号标识，例如 CARD-20260317-001")
    parser.add_argument("--days", type=int, default=365, help="有效天数，默认 365")
    parser.add_argument("--product", default="takealot-autolister", help="产品标识")
    parser.add_argument("--private-key", default=str(root / ".runtime" / "license_private.pem"))
    args = parser.parse_args()

    exp = (datetime.now().date() + timedelta(days=max(1, args.days))).strftime("%Y-%m-%d")
    payload = {
        "product": str(args.product).strip(),
        "card_id": str(args.card_id).strip(),
        "machine_code": _normalize_machine_code(str(args.machine)),
        "issued_at": datetime.now().strftime("%Y-%m-%d"),
        "expires_at": exp,
    }

    token = build_token(payload, args.private_key)
    print("✅ 授权码生成成功")
    print("payload:")
    print(json.dumps(payload, ensure_ascii=False, indent=2))
    print("\n授权码（发给用户）：")
    print(token)


if __name__ == "__main__":
    main()
