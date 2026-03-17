"""
五音科技 NanoBanana2 异步图片生成接口适配器

根据你提供的文档片段，核心信息是：
- 提交任务：POST https://api.wuyinkeji.com/api/async/image_nanoBanana2?key=你的密钥
  Header:
    - Authorization: 接口密钥（控制台-密钥管理申请）
    - Content-Type : application/json
  Body(JSON):
    - prompt      : 提示词（必填）
    - size        : 输出图像大小，可选，支持 1K / 2K / 4K，默认 1K
    - aspectRatio : 图像比例，可选，支持 auto / 1:1 / 16:9 / 9:16 / 4:3 / 3:4 / ...
    - urls        : 参考图 URL 数组，可选
- 查询结果：GET  https://api.wuyinkeji.com/api/async/detail?key=你的密钥&id=任务ID

注意：接口是异步的，这里封装为同步调用：
  generate_image(...) 会内部轮询 detail，直到完成或超时，最终返回图片二进制 bytes 列表。

环境变量：
    WUYIN_API_KEY        五音接口密钥
    WUYIN_API_BASE_URL   基础地址（默认 https://api.wuyinkeji.com）
"""
from __future__ import annotations

import os
import time
from typing import Any, Iterable

import requests


def _base_url() -> str:
    return os.getenv("WUYIN_API_BASE_URL", "https://api.wuyinkeji.com").rstrip("/")


def _api_key() -> str:
    return os.getenv("WUYIN_API_KEY", "").strip()


def is_available() -> bool:
    """是否配置了五音 API Key。"""
    return bool(_api_key())


def _extract_task_id(resp_json: dict[str, Any]) -> str | None:
    """
    从异步提交返回中提取任务 ID。
    不同产品 data 结构可能略有差异，这里做宽松匹配。
    """
    data = resp_json.get("data") or resp_json
    for key in ("id", "task_id", "job_id"):
        v = data.get(key)
        if isinstance(v, (str, int)) and str(v).strip():
            return str(v).strip()
    return None


def _extract_status_and_urls(resp_json: dict[str, Any]) -> tuple[str, list[str]]:
    """
    从 detail 返回中提取状态和结果 URL 列表。

    对于 NanoBanana2，典型返回结构：
    {
      "code": 200,
      "msg": "成功",
      "data": {
        "task_id": "...",
        "status": 2,
        "result": ["https://...png"],
        ...
      }
    }
    """
    data = resp_json.get("data") or resp_json

    # 状态：优先用 data.status（整数），否则退回到可能的字符串字段
    status_val = data.get("status")
    status = ""
    if isinstance(status_val, (int, float)):
        status = str(int(status_val))
    elif isinstance(status_val, str) and status_val.strip():
        status = status_val.strip().lower()
    else:
        for key in ("state", "task_status"):
            v = data.get(key)
            if isinstance(v, str) and v.strip():
                status = v.strip().lower()
                break

    urls: list[str] = []
    # NanoBanana2 把结果放在 data.result 数组里
    for key in ("result", "images", "image_urls", "urls", "url", "video_url"):
        v = data.get(key)
        if isinstance(v, str) and v.strip():
            urls = [v.strip()]
            break
        if isinstance(v, Iterable):
            vals = []
            for item in v:
                if isinstance(item, str) and item.strip():
                    vals.append(item.strip())
            if vals:
                urls = vals
                break

    return status, urls


def generate_image(
    prompt: str,
    *,
    reference_urls: list[str] | None = None,
    aspect_ratio: str = "1:1",
    size: str = "",
    duration: str = "",
    count: int = 1,
    prompts: list[str] | None = None,
    poll_interval: float = 3.0,
    max_wait_seconds: float = 300.0,
) -> list[bytes]:
    """
    用五音 grok 接口生成图片/视频，返回 bytes 列表。

    prompt:          文本提示词
    reference_urls:  参考图 URL 列表（可选）
    aspect_ratio:    宽高比（如 \"1:1\"），可选
    size:            分辨率标记（按文档，可选）
    duration:        时长（对视频生成功能有效，可选）
    poll_interval:   轮询 detail 间隔
    max_wait_seconds:最大等待时间，超时抛错
    """
    api_key = _api_key()
    if not api_key:
        raise RuntimeError("WUYIN_API_KEY 未配置，无法调用五音接口")

    # 图片生成任务提交地址（NanoBanana2）
    submit_url = f"{_base_url()}/api/async/image_nanoBanana2"
    detail_url = f"{_base_url()}/api/async/detail"

    # 对 1688 图像 URL 做一次格式修正：
    #  - 把 "...jpg_.webp" / "...png_.webp" 这类后缀还原成原始 JPG/PNG，
    #    避免把 WebP 伪后缀传给五音接口。
    def _fix_urls(urls: list[str] | None) -> list[str] | None:
        if not urls:
            return None
        fixed: list[str] = []
        for u in urls:
            s = str(u or "").strip()
            if not s:
                continue
            for ext in (".jpg", ".jpeg", ".png"):
                tag = ext + "_.webp"
                if tag in s:
                    s = s.replace(tag, ext)
            if s.endswith("_.webp"):
                s = s[:-6]
            fixed.append(s)
        return fixed or None

    reference_urls = _fix_urls(reference_urls)

    # ── Step1: 提交多个异步任务（1 张 = 1 任务），并发在服务端生成 ─────────────
    jobs: dict[str, dict[str, Any]] = {}  # task_id -> submit_json
    n = max(1, int(count))

    # 为每个任务准备各自的 prompt；如果提供了 prompts，则逐个使用，否则全部用同一个 prompt
    if prompts:
        prompt_list = list(prompts)
        if len(prompt_list) < n:
            prompt_list += [prompt_list[-1]] * (n - len(prompt_list))
    else:
        prompt_list = [prompt] * n

    for i in range(n):
        payload: dict[str, Any] = {
            "prompt": prompt_list[i],
        }
        if size:
            payload["size"] = size
        if aspect_ratio:
            payload["aspectRatio"] = aspect_ratio
        if reference_urls:
            payload["urls"] = reference_urls

        print(f"[wuyin] 提交任务 {i+1}/{n} 到 {submit_url} ...")
        try:
            resp = requests.post(
                submit_url,
                params={"key": api_key},
                headers={
                    "Authorization": api_key,
                    "Content-Type": "application/json",
                },
                json=payload,
                timeout=30,
            )
        except Exception as e:
            raise RuntimeError(f"五音接口提交失败: {e}") from e

        try:
            resp.raise_for_status()
            submit_json = resp.json()
        except Exception as e:
            raise RuntimeError(f"五音接口提交返回异常: {e} | body={resp.text[:500]!r}") from e

        task_id = _extract_task_id(submit_json)
        if not task_id:
            raise RuntimeError(f"五音接口提交未返回任务ID: {submit_json}")
        jobs[task_id] = submit_json
        print(f"[wuyin] 任务已提交，id={task_id}")

    # ── Step2: 轮询所有任务的 detail，直到全部完成或超时 ─────────────────────
    deadline = time.time() + max_wait_seconds
    pending_ids = set(jobs.keys())
    results: list[bytes] = []

    last_status_map: dict[str, str] = {}
    last_payload: dict[str, Any] | None = None

    while pending_ids and time.time() < deadline:
        for task_id in list(pending_ids):
            try:
                r2 = requests.get(detail_url, params={"key": api_key, "id": task_id}, timeout=30)
            except Exception as e:
                print(f"[wuyin] 查询 detail 失败: {e}，稍后重试...")
                continue

            try:
                r2.raise_for_status()
                detail_json = r2.json()
            except Exception as e:
                print(f"[wuyin] detail 返回解析失败: {e} | body={r2.text[:300]!r}")
                continue

            last_payload = detail_json
            status, urls = _extract_status_and_urls(detail_json)
            prev = last_status_map.get(task_id)
            if status and status != prev:
                print(f"[wuyin] 任务 {task_id} 状态：{status}")
                last_status_map[task_id] = status

            # 数字状态约定：0/1=排队/处理中，2=完成
            # 注意：这里不要因为 status=0 就立刻判失败，保持为 pending 等下一轮，
            # 否则会出现你看到的“所有任务状态 0，随后又陆续变 2”的情况。
            if status in {"queued", "waiting", "processing", "running", "pending", "0", "1"} or (not status):
                continue

            if status in {"success", "succeed", "finished", "done", "ok", "2"}:
                if not urls:
                    print(f"[wuyin] 任务 {task_id} 标记成功但无结果URL")
                    pending_ids.discard(task_id)
                    continue
                for u in urls:
                    try:
                        print(f"[wuyin] 下载结果：{u[:80]}...")
                        file_resp = requests.get(u, timeout=60)
                        file_resp.raise_for_status()
                        results.append(file_resp.content)
                    except Exception as e:
                        print(f"[wuyin] 下载 {u} 失败: {e}")
                pending_ids.discard(task_id)
                continue

            # 其它状态：记录错误并移出 pending（单个任务失败不影响已完成的任务）
            print(f"[wuyin] 任务 {task_id} 失败或未知状态: {status}")
            pending_ids.discard(task_id)

        if pending_ids:
            time.sleep(poll_interval)

    # 超时：如果已经至少拿到一张图，就直接返回部分成功的结果；
    # 只在完全没有成功结果时才当作错误抛出。
    if pending_ids and not results:
        raise RuntimeError(
            f"五音接口查询超时（等待 {max_wait_seconds}s），仍有未完成任务: {pending_ids}, 最后 payload={last_payload}"
        )
    if not results:
        raise RuntimeError("五音接口结果URL下载失败（所有任务均无可用结果）")

    return results
