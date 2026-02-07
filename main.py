import json
import time
import sys
import httpx
import uvicorn
from fastapi import FastAPI, Request, Response
from fastapi.responses import StreamingResponse
from loguru import logger

# ================= 配置区域 =================

# 1. 你的真实供应商 Base URL (不要带最后的斜杠)
# 通常是 https://api.openai.com/v1 或 https://api.deepseek.com
TARGET_BASE_URL = "https://zenmux.ai/api/v1" 

# 2. 你的 API Key (建议留空，让 Roo Code 传过来；如果填了会强制覆盖)
API_KEY = ""

# 3. 你的梯子代理地址
PROXY_URL = "http://127.0.0.1:10809" 

# ================= 日志配置 =================
logger.remove()
logger.add(
    sys.stderr,
    format="<green>{time:HH:mm:ss}</green> | <level>{level: <8}</level> | <cyan>{message}</cyan>",
    level="INFO"
)

app = FastAPI()

# ================= 辅助函数 =================

def get_clean_headers(request: Request):
    """清理并构造请求头"""
    headers = {
        k: v for k, v in request.headers.items() 
        if k.lower() not in ("host", "content-length", "connection", "accept-encoding")
    }
    if API_KEY:
        headers["authorization"] = f"Bearer {API_KEY}"
    return headers

async def stream_generator(response, start_time, model_name=None, is_chat=False):
    """通用的流式响应生成器"""
    try:
        chunk_count = 0
        total_bytes = 0
        
        async for chunk in response.aiter_bytes():
            chunk_count += 1
            total_bytes += len(chunk)
            yield chunk

            # 只有对话接口才显示底部动态进度条
            if is_chat:
                now_str = time.strftime("%H:%M:%S")
                spinner = "⠋⠙⠹⠸⠼⠴⠦⠧⠇⠏"[chunk_count % 10]
                sys.stderr.write(
                    f"\r\033[K⚡ [活跃] {spinner} {model_name} | 块数: {chunk_count} | {total_bytes/1024:.1f}KB | {now_str}"
                )
                sys.stderr.flush()

        # 结束处理
        if is_chat:
            sys.stderr.write("\n")
            total_duration = (time.time() - start_time) * 1000
            logger.success(f"✅ 传输完成: {model_name} | chunks: {chunk_count} | 总耗时: {total_duration:.0f}ms")
            
    except Exception as e:
        if is_chat: sys.stderr.write("\n")
        # 🔥 修改这里：打印错误类型和详细 repr，而不仅仅是 str(e)
        logger.error(f"❌ 流传输中断 | 类型: {type(e).__name__} | 详情: {repr(e)}")
        yield str(e).encode()

# ================= 核心路由 1: 对话接口 (特殊处理) =================

@app.post("/v1/chat/completions")
@app.post("/chat/completions")
async def handle_chat_completions(request: Request):
    start_time = time.time()
    headers = get_clean_headers(request)
    
    try:
        body = await request.json()
    except:
        body = {}

    model = body.get("model", "unknown")
    logger.info(f"🚀 [Chat] 发起请求 -> {model}")

    if body.get("stream") is True and "stream_options" not in body:
        body["stream_options"] = {"include_usage": True}
        logger.opt(colors=True).info(f"💉 <yellow>已注入 usage 补丁</yellow>")

    target_url = f"{TARGET_BASE_URL}/chat/completions"

    # 🔥 核心修改 1: 不要使用 async with，而是直接实例化
    client = httpx.AsyncClient(proxy=PROXY_URL, timeout=None)
    
    try:
        req = client.build_request("POST", target_url, json=body, headers=headers)
        # 发起请求（注意：这里只是握手成功，还没开始读 body）
        r = await client.send(req, stream=True)
    except Exception as e:
        # 如果握手阶段就失败了，必须手动关闭 client，否则会泄漏
        await client.aclose()
        logger.error(f"❌ 连接建立失败: {e}")
        return Response(content=f"Connection Error: {e}", status_code=502)

    # 🔥 核心修改 2: 将 client.aclose 放入 background，确保流传完后再关闭
    return StreamingResponse(
        stream_generator(r, start_time, model, is_chat=True),
        status_code=r.status_code,
        media_type="text/event-stream",
        background=client.aclose 
    )

# ================= 核心路由 2: 通用转发 (修复版) =================

@app.api_route("/{path:path}", methods=["GET", "POST", "PUT", "DELETE", "OPTIONS", "HEAD"])
async def proxy_all(request: Request, path: str):
    start_time = time.time()
    method = request.method
    
    # URL 处理逻辑...
    clean_path = path
    if TARGET_BASE_URL.endswith("/v1") and path.startswith("v1/"):
        clean_path = path[3:]
    elif path.startswith("/"):
         clean_path = path[1:]
    target_url = f"{TARGET_BASE_URL}/{clean_path}"
    
    params = dict(request.query_params)
    try:
        req_body = await request.body()
    except:
        req_body = None

    req_headers = {
        k: v for k, v in request.headers.items() 
        if k.lower() not in ("host", "content-length", "connection", "accept-encoding")
    }
    if API_KEY:
        req_headers["authorization"] = f"Bearer {API_KEY}"

    logger.info(f"🔄 [Proxy] {method} {clean_path} -> 转发中...")

    # 🔥 核心修改: 去掉 async with
    client = httpx.AsyncClient(proxy=PROXY_URL, timeout=None)

    try:
        resp = await client.request(
            method=method,
            url=target_url,
            headers=req_headers,
            params=params,
            content=req_body
        )
        
        excluded_headers = {"content-encoding", "content-length", "transfer-encoding", "connection"}
        resp_headers = {k: v for k, v in resp.headers.items() if k.lower() not in excluded_headers}

        logger.info(f"⬅️ [Proxy] 响应: {resp.status_code} (耗时: {(time.time()-start_time)*1000:.0f}ms)")
        
        # 对于普通响应，我们需要读取内容后再关闭 client
        # 或者使用 Response 直接返回 bytes，FastAPI 会处理
        content = await resp.aread() 
        await client.aclose() # 普通请求可以直接关闭

        return Response(
            content=content,
            status_code=resp.status_code,
            headers=resp_headers
        )
            
    except Exception as e:
        await client.aclose() # 出错也要关闭
        logger.error(f"❌ 代理失败: {e}")
        return Response(content=f"Proxy Error: {e}", status_code=502)

if __name__ == "__main__":
    logger.info(f"🔥 全能代理已启动: http://0.0.0.0:11731")
    logger.info(f"🔗 上游: {TARGET_BASE_URL} | 代理: {PROXY_URL}")
    uvicorn.run(app, host="0.0.0.0", port=11731, log_level="error")