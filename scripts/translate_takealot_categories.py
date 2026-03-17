#!/usr/bin/env python3
"""
工具：为 takealot_categories.csv 追加中文类目列（不会改动原始英文列）。

行为：
    - 保留前两行说明原样不动
    - 第三行表头右侧追加 4 列：
        Division_ZH, Department_ZH, Main_ZH, Lowest_ZH
    - 从第四行开始的数据行：
        * 读取原始英文列：
              Division
              Loadsheet/Department
              Main Category
              Lowest Category
        * 按列去重后批量调用 LLM 逐个翻译为简体中文
        * 在每行末尾追加对应的中文列
    - 其他所有列（包括 Minimum Required Images 等）保持不变。

依赖：
    - 需要在环境变量里正确配置 LLM_BASE_URL / LLM_API_KEY / LLM_MODEL
      或者配置硅基流动 API（SILICONFLOW_API_KEY），与项目中其它 LLM 调用保持一致。
"""
from __future__ import annotations

import argparse
import csv
import sys
from pathlib import Path
from typing import Dict, Iterable, List


def _setup_path() -> None:
    """把项目 src 目录加入 sys.path，方便脚本直接运行。"""
    root = Path(__file__).resolve().parents[1]
    src = root / "src"
    if str(src) not in sys.path:
        sys.path.insert(0, str(src))


_setup_path()

from takealot_autolister.llm import (  # noqa: E402
    _call_llm_json,
    _call_llm_raw,
    is_llm_available,
)


_CACHE_PATH = Path("output/takealot_categories_zh_cache.json")


def _load_cache() -> Dict[str, Dict[str, str]]:
    """加载翻译缓存：{column_kind: {en: zh}}。"""
    if not _CACHE_PATH.exists():
        return {}
    try:
        import json

        with _CACHE_PATH.open("r", encoding="utf-8") as f:
            data = json.load(f)
        if isinstance(data, dict):
            # 只保留合法结构
            return {
                str(col): {str(k): str(v) for k, v in (mapping or {}).items()}
                for col, mapping in data.items()
                if isinstance(mapping, dict)
            }
    except Exception:
        pass
    return {}


def _save_cache(cache: Dict[str, Dict[str, str]]) -> None:
    """保存翻译缓存到磁盘。"""
    import json

    _CACHE_PATH.parent.mkdir(parents=True, exist_ok=True)
    with _CACHE_PATH.open("w", encoding="utf-8") as f:
        json.dump(cache, f, ensure_ascii=False, indent=2)


def _chunked(seq: List[str], size: int) -> Iterable[List[str]]:
    for i in range(0, len(seq), size):
        yield seq[i : i + size]


def _translate_values(values: List[str], kind: str, cache: Dict[str, Dict[str, str]]) -> Dict[str, str]:
    """
    使用 LLM 将一批英文类目翻译为简体中文。
    仅返回一个 mapping: 原始英文 -> 中文。
    """
    # 载入该列已有缓存
    col_cache = cache.get(kind, {})

    # 去重并去掉空值，但保留原始顺序；同时跳过已在缓存中的值
    seen = set()
    uniq: List[str] = []
    for v in values:
        if not v or not str(v).strip():
            continue
        if v in col_cache:
            continue
        if v not in seen:
            seen.add(v)
            uniq.append(v)

    # 已有缓存的直接作为初始 mapping
    mapping: Dict[str, str] = dict(col_cache)
    if not uniq:
        print(f"[{kind}] 已有缓存 {len(mapping)} 条，无需新增翻译。")
        return mapping

    if not is_llm_available():
        raise SystemExit("LLM 未配置：请先配置 LLM_BASE_URL / LLM_API_KEY 或硅基流动 API。")

    print(
        f"[{kind}] 唯一值总数：{len(mapping) + len(uniq)}，"
        f"缓存命中：{len(mapping)}，需翻译：{len(uniq)}"
    )

    # Lowest Category 数量最多，可以适当放大批量，减少 HTTP 请求次数
    batch_size = 120 if kind == "Lowest Category" else 40
    batches = list(_chunked(uniq, batch_size))
    for idx, batch in enumerate(batches, start=1):
        items = [{"id": str(i), "text": text} for i, text in enumerate(batch, start=1)]
        prompt = (
            "You are a bilingual e-commerce category translator.\n"
            "Platform: Takealot (South African e-commerce), audience: Chinese-speaking sellers.\n"
            f"Column type: {kind}.\n\n"
            "Task:\n"
            "- Translate each English category name into Simplified Chinese.\n"
            "- Use natural Chinese category wording suitable for an online marketplace.\n"
            "- Do NOT include the English in brackets.\n"
            "- Do NOT add explanations, extra punctuation or numbering.\n"
            "- Keep brand names and proper nouns in English.\n"
            "- If the input is empty or meaningless, return an empty string.\n\n"
            "Examples:\n"
            "  \"Beauty\" -> \"美容\"\n"
            "  \"Hair Styling Tools & Accessories\" -> \"美发工具及配件\"\n"
            "  \"Brushes & Combs\" -> \"梳子和梳具\"\n\n"
            "Input format (JSON):\n"
            '{"items": [{"id": "1", "text": "Beauty"}, ...]}\n\n'
            "Output format (STRICT JSON only):\n"
            '{"items": [{"id": \"1\", \"zh\": \"美容\"}, ...]}\n\n'
            f"Input items:\n{items}"
        )

        print(f"[{kind}] 调用 LLM 批次 {idx}/{len(batches)}，本批 {len(batch)} 条…")
        try:
            result = _call_llm_json(prompt, temperature=0.1)
        except Exception as e:
            # 有时候模型返回的 JSON 不完全合法，这里降级为“纯文本逐行”协议重试一遍。
            print(f"[{kind}] JSON 模式解析失败，改用纯文本重试: {e}")
            plain_prompt = (
                "You are a bilingual e-commerce category translator.\n"
                "Task: Translate each of the following English category names "
                "into concise Simplified Chinese category names.\n"
                "- Keep the order.\n"
                "- Return ONLY the Chinese names, one per line.\n"
                "- No numbering, no extra text.\n\n"
                "Input:\n"
                + "\n".join(batch)
            )
            raw = _call_llm_raw(plain_prompt, temperature=0.1)
            lines = [l.strip() for l in raw.splitlines() if l.strip()]
            if len(lines) < len(batch):
                raise SystemExit(
                    f"[{kind}] 纯文本重试仍然失败：期望 {len(batch)} 行，实际 {len(lines)} 行。"
                )
            for src, zh in zip(batch, lines):
                mapping[src] = zh

            cache[kind] = mapping
            _save_cache(cache)
            continue

        out_items = result.get("items") or []
        # 统一通过 id 把结果对齐回本批次的原文，避免依赖模型是否带回 text 字段。
        for obj in out_items:
            try:
                idx = int(obj.get("id"))
                zh = str(obj.get("zh") or "").strip()
            except Exception:
                continue
            if not zh:
                continue
            if 1 <= idx <= len(batch):
                src = batch[idx - 1]
                mapping[src] = zh

        # 每批更新缓存，便于中断后下次重用
        cache[kind] = mapping
        _save_cache(cache)

    return mapping


def process_csv(input_path: Path, output_path: Path) -> None:
    with input_path.open("r", encoding="utf-8-sig", newline="") as f:
        reader = list(csv.reader(f))

    if len(reader) < 3:
        raise SystemExit("CSV 行数不足（需要至少三行：两行说明 + 一行表头）。")

    # 前两行说明原样保留
    header = reader[2]

    # 找到关键列索引
    def idx_of(name: str) -> int:
        try:
            return header.index(name)
        except ValueError:
            raise SystemExit(f"表头中未找到列名：{name!r}")

    idx_div = idx_of("Division")
    idx_dept = idx_of("Loadsheet/Department")
    idx_main = idx_of("Main Category")
    idx_low = idx_of("Lowest Category")

    data_rows = reader[3:]

    # 收集各列的所有取值
    divisions = [row[idx_div] for row in data_rows if len(row) > idx_div]
    departments = [row[idx_dept] for row in data_rows if len(row) > idx_dept]
    mains = [row[idx_main] for row in data_rows if len(row) > idx_main]
    lows = [row[idx_low] for row in data_rows if len(row) > idx_low]

    # 加载/更新缓存，分列批量翻译
    cache: Dict[str, Dict[str, str]] = _load_cache()
    div_map = _translate_values(divisions, "Division", cache)
    dept_map = _translate_values(departments, "Loadsheet/Department", cache)
    main_map = _translate_values(mains, "Main Category", cache)
    low_map = _translate_values(lows, "Lowest Category", cache)

    # 写出新的 CSV
    output_path.parent.mkdir(parents=True, exist_ok=True)
    with output_path.open("w", encoding="utf-8", newline="") as f:
        writer = csv.writer(f)

        # 1-2 行说明原样写回
        writer.writerow(reader[0])
        writer.writerow(reader[1])

        # 表头追加 4 个中文列
        new_header = header + ["Division_ZH", "Department_ZH", "Main_ZH", "Lowest_ZH"]
        writer.writerow(new_header)

        # 数据行：原样写英文列，再在最右侧追加中文列
        for row in data_rows:
            # 确保至少有表头长度那么多列，避免下标异常（用空字符串填充）
            if len(row) < len(header):
                row = row + [""] * (len(header) - len(row))

            div = row[idx_div]
            dept = row[idx_dept]
            main = row[idx_main]
            low = row[idx_low]

            div_zh = div_map.get(div, "") if div.strip() else ""
            dept_zh = dept_map.get(dept, "") if dept.strip() else ""
            main_zh = main_map.get(main, "") if main.strip() else ""
            low_zh = low_map.get(low, "") if low.strip() else ""

            writer.writerow(row + [div_zh, dept_zh, main_zh, low_zh])


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(
        description="为 Takealot 类目表追加中文列（*_ZH），不修改原始英文列。"
    )
    parser.add_argument(
        "-i",
        "--input",
        type=Path,
        default=Path("input/takealot_categories.csv"),
        help="输入 CSV 路径（默认：input/takealot_categories.csv）",
    )
    parser.add_argument(
        "-o",
        "--output",
        type=Path,
        default=Path("output/takealot_categories_zh.csv"),
        help="输出 CSV 路径（默认：output/takealot_categories_zh.csv）",
    )

    args = parser.parse_args(argv)

    if not args.input.exists():
        raise SystemExit(f"输入文件不存在：{args.input}")

    process_csv(args.input, args.output)
    print(f"完成：{args.input} -> {args.output}")


if __name__ == "__main__":
    main()
