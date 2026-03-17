from __future__ import annotations

import json
from collections.abc import Callable
from dataclasses import asdict
from datetime import datetime
from pathlib import Path
from typing import Any

from .csv_exporter import generate_loadsheet
from .llm import fallback_generate_draft, generate_draft_with_llm
from .oss_uploader import upload_bytes_list
from .portal import NeedLoginError, PortalFormNotReadyError, automate_listing, find_probe_category_path, probe_category_fields
from .rules import RuleSet, sanitize_draft, validate_draft
from .scraper_1688 import Need1688LoginError, Need1688RetryError, Need1688VerificationError, scrape_1688_product
from .types import PipelineResult


def _now_tag() -> str:
    return datetime.now().strftime("%Y%m%d-%H%M%S")


def _write_markdown(run_dir: Path, source: dict, draft: dict, validation: dict) -> Path:
    p = run_dir / "listing_package.md"
    lines: list[str] = []
    lines.append("# Takealot Listing Package")
    lines.append("")
    lines.append(f"- Source URL: {source.get('source_url', '')}")
    lines.append(f"- Generated At: {datetime.now().isoformat(timespec='seconds')}")
    lines.append("")
    lines.append("## Title")
    lines.append(draft.get("title", ""))
    lines.append("")
    lines.append("## Subtitle")
    lines.append(draft.get("subtitle", ""))
    lines.append("")
    lines.append("## Key Features")
    lines.append(draft.get("key_features", ""))
    lines.append("")
    lines.append("## What's In The Box")
    for x in draft.get("whats_in_box", []):
        lines.append(f"- {x}")
    lines.append("")
    lines.append("## Attributes")
    for k, v in (draft.get("attributes", {}) or {}).items():
        lines.append(f"- {k}: {v}")
    lines.append("")
    lines.append("## Validation")
    lines.append(f"- ok: {validation.get('ok')}")
    for e in validation.get("errors", []):
        lines.append(f"- error: {e}")
    for w in validation.get("warnings", []):
        lines.append(f"- warning: {w}")
    lines.append("")

    p.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return p


def process_one_link(
    link: str,
    output_dir: Path,
    rules: RuleSet,
    use_llm: bool,
    headless: bool,
    browser_channel: str,
    user_data_dir: str | None,
    storage_state_1688: str | None,
    storage_state_takealot: str | None,
    remove_bg: bool,
    automate_portal_enabled: bool,
    selectors_path: str | None,
    portal_mode: str,
    login_wait_seconds: int = 0,
    browser_profile_directory: str = "Default",
    generate_loadsheet_enabled: bool = True,
    log_callback: Callable[[str, str], None] | None = None,
    preview_callback: Callable[[Any], Any] | None = None,
) -> PipelineResult:
    def _log(level: str, msg: str) -> None:
        if log_callback:
            log_callback(level, msg)
        else:
            print(f"[{level}] {msg}")

    run_dir = output_dir / _now_tag()
    run_dir.mkdir(parents=True, exist_ok=True)

    def _write_result_file(action: str, message: str, ok: bool) -> None:
        (run_dir / "result.json").write_text(
            json.dumps({"ok": ok, "action": action, "message": message}, ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )

    _log("info", f"📁 输出目录：{run_dir.name}")
    _log("info", "─" * 50)

    # ── Step 1: 抓取 1688 ────────────────────────────────────────────────────
    _log("info", "【1/5】🌐 正在抓取 1688 商品数据...")
    try:
        source = scrape_1688_product(
            url=link,
            run_dir=run_dir,
            headless=headless,
            browser_channel=browser_channel,
            user_data_dir=user_data_dir,
            storage_state_path=storage_state_1688,
            login_wait_seconds=login_wait_seconds,
            browser_profile_directory=browser_profile_directory,
        )
    except Need1688LoginError as e:
        _write_result_file(action="need_login_1688", message=str(e), ok=False)
        return PipelineResult(ok=False, run_dir=str(run_dir), source_file="", draft_file="",
                              markdown_file="", image_files=[], action="need_login_1688", message=str(e))
    except Need1688VerificationError as e:
        _write_result_file(action="need_verify_1688", message=str(e), ok=False)
        return PipelineResult(ok=False, run_dir=str(run_dir), source_file="", draft_file="",
                              markdown_file="", image_files=[], action="need_verify_1688", message=str(e))
    except Need1688RetryError as e:
        _write_result_file(action="need_retry_1688", message=str(e), ok=False)
        return PipelineResult(ok=False, run_dir=str(run_dir), source_file="", draft_file="",
                              markdown_file="", image_files=[], action="need_retry_1688", message=str(e))
    except Exception as e:
        msg = f"SOURCE_CAPTURE_FAILED: {e}"
        _write_result_file(action="source_capture_failed", message=msg, ok=False)
        return PipelineResult(ok=False, run_dir=str(run_dir), source_file="", draft_file="",
                              markdown_file="", image_files=[], action="source_capture_failed", message=msg)

    source_file = run_dir / "source.json"
    if not source_file.exists():
        source_file.write_text(json.dumps(source.to_dict(), ensure_ascii=False, indent=2) + "\n", encoding="utf-8")

    _log("ok",   f"  ✅ 抓取成功：{source.title or '(无标题)'}")
    _log("info", f"  类目：{' > '.join(source.category_path or [])}")
    _log("info", f"  图片：{len(source.image_urls)} 张")
    n_attrs = len(source.product_attrs or {})
    if n_attrs:
        _log("info", f"  商品属性：{n_attrs} 条（品牌/型号/材质等）")

    # ── Step 2: 仅匹配 Takealot 类目，不自动探测字段 ────────────────────────
    # 说明：
    #   1) 这里只负责把 1688 类目映射到 Takealot 的英文类目路径（en_path），
    #      方便预览时默认带出一个建议路径、以及 loadsheet 选模板用。
    #   2) 不再自动调用 probe_category_fields 打开浏览器探测字段。
    #      字段探测改为在预览对话框里，由用户手动选择类目后点击
    #     「保存并重探测字段」触发。这样可以避免误探测，同时保留缓存机制。
    _log("info", "【2/5】🔍 正在匹配 Takealot 类目（不自动探测字段）...")
    probe_result: dict = {}
    en_path: list[str] = []
    if selectors_path and source.category_path:
        from .portal import load_probed_fields
        en_path = find_probe_category_path(
            source_category_path=source.category_path,
            source_title=source.title or "",
            selectors_cfg_path=selectors_path,
        )
        if not en_path:
            from .csv_exporter import _translate_zh_category as _tzc
            en_path = _tzc(source.category_path)
        _log("info", f"  🗂️  类目路径：{' > '.join(en_path)}")

        # 只在已有缓存时加载字段定义；否则交由预览里的「保存并重探测字段」按钮手动触发。
        cat_key = " > ".join(str(x).strip() for x in en_path if x)
        cached = load_probed_fields(cat_key)
        if cached:
            _log("info", f"  📋 已加载缓存的 portal 字段定义：{cat_key}")
            probe_result = cached
        else:
            _log("info", "  （本类目尚未探测字段，将在预览中由你手动选择类目并点击“保存并重探测字段”来生成。)")

    # ── Step 3: LLM 生成草稿 ──────────────────────────────────────────────────
    _log("info", "【3/5】📋 正在准备商品描述草稿（预览中再用 AI 一键生成）...")
    if use_llm:
        # 仍然保留后备方案：如果需要，可以按配置直接用 LLM 先生成一版草稿
        try:
            draft = generate_draft_with_llm(source, rules)
            _log("ok", f"  ✅ AI 草稿生成完成：{draft.title[:50]}")
        except Exception as _llm_err:
            _log("warn", f"  ⚠️  AI 生成失败，使用基础模板（{_llm_err}）")
            draft = fallback_generate_draft(source, rules)
    else:
        # GUI 默认走这里：不在第 3 步调用 LLM，先用规则生成一个简单草稿，
        # 真正的文案由预览里的「AI 生成」按钮按需生成
        draft = fallback_generate_draft(source, rules)
        _log("ok", "  ✅ 草稿基础结构已就绪，详细文案请在预览里点『AI 生成』")
    draft = sanitize_draft(draft, rules)
    validation = validate_draft(draft, rules)
    # 把 probe 已匹配的英文类目路径存入 draft，供 generate_loadsheet 直接使用
    if en_path:
        draft.attributes["_category_path"] = en_path  # type: ignore[assignment]
    # 把探测到的 portal 字段信息存入 draft，供 ai_fill_missing_fields 过滤只填相关字段
    if probe_result.get("fields"):
        draft.attributes["_probe_fields"] = probe_result["fields"]  # type: ignore[assignment]
    _log("info", f"  → 基础草稿就绪：{draft.title or '(待 AI 生成)'}")

    draft_file = run_dir / "draft.json"
    draft_file.write_text(json.dumps(asdict(draft), ensure_ascii=False, indent=2) + "\n", encoding="utf-8")

    md_file = _write_markdown(
        run_dir,
        source=source.to_dict(),
        draft=asdict(draft),
        validation={"ok": validation.ok, "errors": validation.errors, "warnings": validation.warnings},
    )

    action = "package_only"
    message = "listing package generated"

    # ── Step 4: 预览编辑对话框（仅 GUI 模式）────────────────────────────────
    preview_confirmed_images: list[bytes] = []
    preview_field_values: dict[str, str] = {}
    preview_category_path: list[str] = []
    preview_portal_fields: list[dict] = []
    if preview_callback is not None:
        from .preview_dialog import PreviewData
        from .image_generator import ImageGeneratorSession

        img_session = ImageGeneratorSession(
            source_urls=source.image_urls or [],
            product_title=draft.title or "",
        )
        preview_data = PreviewData(
            title="",
            subtitle="",
            source_image_urls=source.image_urls or [],
            portal_fields=probe_result.get("fields", []),
            field_values={},
            category_path=en_path or source.category_path or [],
            product_info=source.to_dict(),
            run_dir=str(run_dir),
        )
        _log("info", "  💬 等待用户在预览对话框中确认...")
        preview_result = preview_callback(preview_data, img_session)
        if preview_result is None or not getattr(preview_result, "confirmed", False):
            _write_result_file(action="preview_cancelled", message="用户取消了预览", ok=False)
            _log("info", "  ❌ 用户取消，流程终止")
            return PipelineResult(ok=False, run_dir=str(run_dir), source_file=str(source_file),
                                  draft_file=str(draft_file), markdown_file=str(md_file),
                                  image_files=[], action="preview_cancelled", message="用户取消")
        if preview_result.title:
            draft.title = preview_result.title
        if preview_result.subtitle:
            draft.subtitle = preview_result.subtitle
        preview_confirmed_images = preview_result.selected_image_bytes or []
        preview_field_values = preview_result.field_values or {}
        preview_category_path = [str(x).strip() for x in (getattr(preview_result, "category_path", []) or []) if str(x).strip()]
        preview_portal_fields = getattr(preview_result, "portal_fields", []) or []
        # 把预览对话框里 AI 生成（或用户编辑）的 key_features 写回 draft
        for kf_label in ("Key Selling Features", "key_features", "Key Features"):
            kf_val = preview_field_values.get(kf_label, "").strip()
            if kf_val:
                draft.key_features = kf_val
                break
        _log("ok", f"  ✅ 用户确认：{len(preview_confirmed_images)} 张图片，{len(preview_field_values)} 个字段")
        if preview_category_path:
            _log("info", f"  🗂️  采用预览类目：{' > '.join(preview_category_path)}")

    # ── Step 5: 上传 OSS + 生成 xlsm ────────────────────────────────────────
    if generate_loadsheet_enabled:
        oss_urls: list[str] = []

        if preview_confirmed_images:
            _log("info", "【4/5】☁️  正在上传 AI 生成图到 OSS...")
            oss_urls = upload_bytes_list(preview_confirmed_images, stem="ai_product")
            if oss_urls:
                _log("ok", f"  ✅ 上传完成：{len(oss_urls)} 个 URL")
            else:
                _log("warn", "  ⚠️  OSS 未配置或上传失败，xlsm 图片留空")
        else:
            _log("info", "【4/5】⏭  无 AI 生成图，跳过 OSS 上传")

        # 合并用户在预览对话框中编辑的字段值到 draft.attributes
        combined_attrs: dict[str, str] = dict(draft.attributes or {})
        combined_attrs.update(preview_field_values)

        # 预览里一些 portal 字段需要同步回核心属性，方便 loadsheet 映射：
        # 1）颜色：只在预览中用户明确选择时才写入；如果预览没选，则完全不写颜色，
        #    不再从 1688/AI 猜测，避免出现平台不认可的颜色值。
        preview_colour = ""
        for colour_key in ("Colour", "Main Colour", "Main Color", "Main/Secondary Colour"):
            cv = str(preview_field_values.get(colour_key, "")).strip()
            if cv:
                preview_colour = cv
                break
        if preview_colour:
            combined_attrs["colour"] = preview_colour
            combined_attrs["colour_name"] = preview_colour
        else:
            # 用户未在预览中选择主颜色时，清空所有自动推断的颜色字段。
            for k in ("colour", "color", "colour_name", "color_name", "secondary_colour", "secondary_color"):
                combined_attrs.pop(k, None)

        # 2）材质：优先使用预览里用户选的 portal 字段，覆盖草稿中的粗略材质
        #   - 手表类目使用「Main Strap Material」
        #   - 行李/包类目使用「Main Material/Fabric」
        msm = str(combined_attrs.get("Main Strap Material", "")).strip()
        mmf = str(combined_attrs.get("Main Material/Fabric", "")).strip()
        if msm:
            combined_attrs["material"] = msm
        elif mmf:
            combined_attrs["material"] = mmf

        if preview_category_path:
            combined_attrs["_category_path"] = preview_category_path  # type: ignore[index]
        if isinstance(preview_portal_fields, list) and preview_portal_fields:
            combined_attrs["_probe_fields"] = preview_portal_fields  # type: ignore[index]
        draft.attributes = combined_attrs

        _log("info", "【5/5】📊 正在生成 Takealot loadsheet xlsm...")
        xlsm_path = generate_loadsheet(draft, source, run_dir, image_urls=oss_urls or None)
        if xlsm_path:
            action = "loadsheet_generated"
            message = f"loadsheet generated: {xlsm_path.name}"
            _log("ok", f"  ✅ xlsm 已生成：{xlsm_path.name}")
        else:
            _log("warn", "  ⚠️  xlsm 生成失败（类目映射找不到对应 loadsheet）")

    if automate_portal_enabled:
        if not selectors_path:
            raise ValueError("selectors file is required when --automate-portal is enabled")
        try:
            portal_evidence = automate_listing(
                draft=draft,
                image_paths=[],
                selectors_cfg_path=selectors_path,
                run_dir=run_dir,
                mode=portal_mode,
                headless=headless,
                browser_channel=browser_channel,
                user_data_dir=user_data_dir,
                storage_state_path=storage_state_takealot,
                login_wait_seconds=login_wait_seconds,
                browser_profile_directory=browser_profile_directory,
                source_title=source.title,
                source_category_path=source.category_path,
            )
            warnings = portal_evidence.get("warnings", []) if isinstance(portal_evidence, dict) else []
            if warnings:
                action = f"portal_{portal_mode}_partial"
                message = f"portal {portal_mode} done with warnings: {','.join(str(x) for x in warnings)}"
            else:
                action = f"portal_{portal_mode}"
                message = f"portal {portal_mode} done"
        except NeedLoginError as e:
            action = "need_login"
            message = str(e)
        except PortalFormNotReadyError as e:
            action = "portal_form_not_ready"
            message = str(e)

    _write_result_file(action=action, message=message, ok=validation.ok)
    _log("info", "─" * 50)
    _log("ok",   f"🎉 全部完成！action={action}")
    return PipelineResult(
        ok=validation.ok,
        run_dir=str(run_dir),
        source_file=str(source_file),
        draft_file=str(draft_file),
        markdown_file=str(md_file),
        image_files=[],
        action=action,
        message=message,
    )
