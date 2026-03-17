from __future__ import annotations

import argparse
import json
import os
from pathlib import Path

from dotenv import load_dotenv

from .pipeline import process_one_link
from .rules import load_rules


def _read_links(path: Path) -> list[str]:
    if not path.exists():
        return []
    links: list[str] = []
    for raw in path.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        links.append(line)
    return links


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(description="Takealot Auto Lister MVP")
    p.add_argument("--link", default="", help="single 1688 product link")
    p.add_argument(
        "--links-file",
        default="input/links.txt",
        help="file with one 1688 url per line",
    )
    p.add_argument("--output-dir", default="output/runs", help="run output directory")
    p.add_argument("--rules", default="config/rules.yaml", help="rules yaml")
    p.add_argument("--selectors", default="config/selectors.yaml", help="portal selectors yaml")

    p.add_argument("--headless", action="store_true", help="run browser in background")
    p.add_argument("--headed", action="store_true", help="run browser with UI")

    p.add_argument("--browser-channel", default=os.getenv("BROWSER_CHANNEL", "msedge"))
    p.add_argument("--browser-user-data-dir", default=os.getenv("BROWSER_USER_DATA_DIR", ""))
    p.add_argument("--storage-state-1688", default=os.getenv("STORAGE_STATE_1688", ""))
    p.add_argument("--storage-state-takealot", default=os.getenv("STORAGE_STATE_TAKEALOT", ""))
    p.add_argument(
        "--browser-profile-directory",
        default=os.getenv("BROWSER_PROFILE_DIRECTORY", "Default"),
        help="browser profile directory name under user data dir, e.g. Default / Profile 1",
    )

    p.add_argument("--no-llm", action="store_true", help="disable LLM generation")
    p.add_argument("--remove-bg", action="store_true", help="enable rembg if installed")

    p.add_argument("--automate-portal", action="store_true", help="fill and click actions in seller portal")
    p.add_argument("--portal-mode", choices=["draft", "publish"], default=os.getenv("DEFAULT_PORTAL_MODE", "draft"))
    p.add_argument(
        "--login-wait-seconds",
        type=int,
        default=180,
        help="when running headed mode and login is required, wait up to N seconds before failing",
    )

    p.add_argument("--limit", type=int, default=1, help="max links to process")
    return p


def main() -> None:
    load_dotenv()
    parser = build_parser()
    args = parser.parse_args()

    headless = True
    if args.headed:
        headless = False
    elif args.headless:
        headless = True
    else:
        headless = str(os.getenv("DEFAULT_HEADLESS", "true")).lower() == "true"

    links: list[str] = []
    if args.link.strip():
        links.append(args.link.strip())
    links.extend(_read_links(Path(args.links_file)))

    dedup: list[str] = []
    seen = set()
    for x in links:
        if x not in seen:
            seen.add(x)
            dedup.append(x)
    links = dedup[: max(0, args.limit)]

    if not links:
        raise SystemExit("No links found. Use --link or put urls in input/links.txt")

    rules = load_rules(args.rules)
    output_dir = Path(args.output_dir)

    results = []
    for link in links:
        res = process_one_link(
            link=link,
            output_dir=output_dir,
            rules=rules,
            use_llm=not args.no_llm,
            headless=headless,
            browser_channel=args.browser_channel,
            user_data_dir=args.browser_user_data_dir.strip() or None,
            storage_state_1688=args.storage_state_1688.strip() or None,
            storage_state_takealot=args.storage_state_takealot.strip() or None,
            remove_bg=args.remove_bg,
            automate_portal_enabled=args.automate_portal,
            selectors_path=args.selectors,
            portal_mode=args.portal_mode,
            login_wait_seconds=(max(0, args.login_wait_seconds) if not headless else 0),
            browser_profile_directory=args.browser_profile_directory,
        )
        results.append(res)

    out = {
        "count": len(results),
        "results": [r.__dict__ for r in results],
    }
    print(json.dumps(out, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
