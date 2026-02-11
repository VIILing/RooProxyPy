import json
import time
import sys
import httpx
import uvicorn
from fastapi import FastAPI, Request, Response
from fastapi.responses import StreamingResponse
from loguru import logger
from dotenv import load_dotenv
import os

# 加载 .env 文件
load_dotenv()
  
# ================= 配置区域 =================  

# 端口号
LISTEN_PORT: int = int(os.getenv("LISTEN_PORT", "11731").strip() or 11731)

# 1. OpenAI 兼容的上游地址  
TARGET_BASE_URL = "https://zenmux.ai/api/v1"  
  
# 2. Anthropic 专用上游地址
ANTHROPIC_BASE_URL = "https://zenmux.ai/api/anthropic/v1"
  
# 3. API Key (留空让 Roo Code 传入；填了会覆盖)  
API_KEY = ""  
  
# 4. 你的梯子代理地址（可选，留空代表不设置）
# 从环境变量 PROXY_URL 读取，如果未设置或为空则为 None
PROXY_URL = os.getenv("PROXY_URL", "").strip() or None
  
# ================= Anthropic 特有配置 =================  
  
# 5. Anthropic 模型名映射（未匹配则直接报错）  
ANTHROPIC_MODEL_MAP = {
    # "claude-3-5-haiku-20241022": "",
    # "claude-3-5-sonnet-20241022": "",
    # "claude-3-7-sonnet-20250219": "",
    # "claude-3-7-sonnet-20250219:thinking": "",
    # "claude-3-haiku-20240307": "",
    # "claude-3-opus-20240229": "",
    "claude-haiku-4-5-20251001": "anthropic/claude-haiku-4.5",
    "claude-opus-4-1-20250805": "anthropic/claude-opus-4.1",
    "claude-opus-4-20250514": "anthropic/claude-opus-4",
    "claude-opus-4-5-20251101": "anthropic/claude-opus-4.5",
    "claude-opus-4-6": "anthropic/claude-opus-4.6",
    "claude-sonnet-4-20250514": "anthropic/claude-sonnet-4",
    "claude-sonnet-4-5": "anthropic/claude-sonnet-4.5",
}  
  
# 6. 是否自动注入 ZenMux Web Search 工具  
ENABLE_WEB_SEARCH = True  
  
# 7. Web Search 工具配置（按 ZenMux 文档可调整）  
ZENMUX_WEB_SEARCH_TOOL = {
    "type": "web_search_20250305",
    "name": "web_search"
}
# 文档：https://zenmux.ai/docs/guide/advanced/web-search.html  
  
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
        headers["x-api-key"] = API_KEY
    return headers

def redact_headers(headers: dict):
    """用于日志的请求头脱敏"""
    redacted = {}
    for k, v in headers.items():
        if k.lower() in ("authorization", "x-api-key"):
            redacted[k] = "***"
        else:
            redacted[k] = v
    return redacted
  
def modify_anthropic_body(body: dict):  
    """修改 Anthropic 请求体：模型映射 + 注入 web_search"""  
    if not isinstance(body, dict):  
        return body, None  
  
    model = body.get("model")  
    if model not in ANTHROPIC_MODEL_MAP:  
        return body, model  # 返回未匹配的模型名  
  
    body["model"] = ANTHROPIC_MODEL_MAP[model]  
    logger.opt(colors=True).info(  
        f"🔁 <yellow>模型名替换</yellow>: {model} -> {body['model']}"  
    )  
  
    if ENABLE_WEB_SEARCH and ZENMUX_WEB_SEARCH_TOOL:  
        tools = body.get("tools", [])  
        if not isinstance(tools, list):  
            tools = []  
  
        existing_types = {t.get("type") for t in tools if isinstance(t, dict)}  
        if ZENMUX_WEB_SEARCH_TOOL.get("type") not in existing_types:  
            tools.append(ZENMUX_WEB_SEARCH_TOOL)  
            body["tools"] = tools  
            logger.opt(colors=True).info("🔍 <cyan>已注入 ZenMux Web Search 工具</cyan>")  
  
    return body, None  
  
async def stream_generator(response, start_time, model_name=None, is_chat=False):  
    """通用的流式响应生成器"""  
    try:  
        chunk_count = 0  
        total_bytes = 0  
  
        async for chunk in response.aiter_bytes():  
            chunk_count += 1  
            total_bytes += len(chunk)  
            yield chunk  
  
            if is_chat:  
                now_str = time.strftime("%H:%M:%S")  
                spinner = "⠋⠙⠹⠸⠼⠴⠦⠧⠇⠏"[chunk_count % 10]  
                sys.stderr.write(  
                    f"\r\033[K⚡ [活跃] {spinner} {model_name} | 块数: {chunk_count} | {total_bytes/1024:.1f}KB | {now_str}"  
                )  
                sys.stderr.flush()  
  
        if is_chat:  
            sys.stderr.write("\n")  
            total_duration = (time.time() - start_time) * 1000  
            logger.success(f"✅ 传输完成: {model_name} | chunks: {chunk_count} | 总耗时: {total_duration:.0f}ms")  
  
    except Exception as e:  
        if is_chat:  
            sys.stderr.write("\n")  
        logger.error(f"❌ 流传输中断 | 类型: {type(e).__name__} | 详情: {repr(e)}")  
        yield str(e).encode()  
  
# ================= OpenAI Chat =================  
  
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
    logger.info(f"➡️ [IN]  {request.url.path}")  
    logger.info(f"🚀 [Chat] 发起请求 -> {model}")  
  
    if body.get("stream") is True and "stream_options" not in body:  
        body["stream_options"] = {"include_usage": True}  
        logger.opt(colors=True).info(f"💉 <yellow>已注入 usage 补丁</yellow>")  
  
    target_url = f"{TARGET_BASE_URL}/chat/completions"  
    logger.info(f"⬅️ [OUT] {target_url}")  
  
    client = httpx.AsyncClient(proxy=PROXY_URL, timeout=None)  
  
    try:  
        req = client.build_request("POST", target_url, json=body, headers=headers)  
        r = await client.send(req, stream=True)  
    except Exception as e:  
        await client.aclose()  
        logger.error(f"❌ 连接建立失败: {e}")  
        return Response(content=f"Connection Error: {e}", status_code=502)  
  
    return StreamingResponse(  
        stream_generator(r, start_time, model, is_chat=True),  
        status_code=r.status_code,  
        media_type="text/event-stream",  
        background=client.aclose  
    )  
  
# ================= Anthropic Messages =================  
  
@app.post("/v1/messages")  
@app.post("/messages")  
async def handle_anthropic_messages(request: Request):  
    start_time = time.time()  
    headers = get_clean_headers(request)  
  
    try:  
        body = await request.json()  
    except:  
        body = {}  
  
    body, unmatched_model = modify_anthropic_body(body)  
    if unmatched_model:  
        logger.error(f"❌ Anthropic 模型未匹配: {unmatched_model}")  
        return Response(  
            content=json.dumps({  
                "error": f"Model '{unmatched_model}' not found in ANTHROPIC_MODEL_MAP"  
            }),  
            status_code=400,  
            media_type="application/json"  
        )  
  
    model = body.get("model", "unknown")
    logger.info(f"➡️ [IN]  {request.url.path}")
    logger.info(f"🟣 [Anthropic] 发起请求 -> {model}")
   
    target_url = f"{ANTHROPIC_BASE_URL}/messages"
    logger.info(f"⬅️ [OUT] {target_url}")
    logger.info(f"🧾 [Anthropic] 出站请求头: {redact_headers(headers)}")
   
    client = httpx.AsyncClient(proxy=PROXY_URL, timeout=None)
  
    try:  
        req = client.build_request("POST", target_url, json=body, headers=headers)  
  
        if body.get("stream") is True:
            r = await client.send(req, stream=True)
            logger.info(f"🧪 [Anthropic] 上游状态码: {r.status_code}")
            return StreamingResponse(
                stream_generator(r, start_time, model, is_chat=True),
                status_code=r.status_code,
                media_type="text/event-stream",
                background=client.aclose
            )
        else:
            r = await client.send(req)
            content = await r.aread()
            logger.info(f"🧪 [Anthropic] 上游状态码: {r.status_code}")
            if r.status_code >= 400:
                try:
                    logger.error(f"🧨 [Anthropic] 上游错误响应: {content.decode(errors='ignore')}")
                except Exception as log_err:
                    logger.error(f"🧨 [Anthropic] 上游错误响应读取失败: {log_err}")
   
            excluded_headers = {"content-encoding", "content-length", "transfer-encoding", "connection"}
            resp_headers = {k: v for k, v in r.headers.items() if k.lower() not in excluded_headers}
   
            await client.aclose()
            return Response(content=content, status_code=r.status_code, headers=resp_headers)
  
    except Exception as e:  
        await client.aclose()  
        logger.error(f"❌ Anthropic 代理失败: {e}")  
        return Response(content=f"Anthropic Proxy Error: {e}", status_code=502)  
  
# ================= 通用转发 =================  
  
@app.api_route("/{path:path}", methods=["GET", "POST", "PUT", "DELETE", "OPTIONS", "HEAD"])  
async def proxy_all(request: Request, path: str):  
    start_time = time.time()  
    method = request.method  
  
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
  
    logger.info(f"➡️ [IN]  {request.url.path}")  
    logger.info(f"🔄 [Proxy] {method} {clean_path} -> 转发中...")  
    logger.info(f"⬅️ [OUT] {target_url}")  
  
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
  
        content = await resp.aread()  
        await client.aclose()  
  
        return Response(  
            content=content,  
            status_code=resp.status_code,  
            headers=resp_headers  
        )  
  
    except Exception as e:  
        await client.aclose()  
        logger.error(f"❌ 代理失败: {e}")  
        return Response(content=f"Proxy Error: {e}", status_code=502)  
  
if __name__ == "__main__":
     logger.info(f"🔥 全能代理已启动: http://0.0.0.0:{LISTEN_PORT}")
     proxy_info = PROXY_URL if PROXY_URL else "未设置"
     logger.info(f"🔗 上游: {TARGET_BASE_URL} | 代理: {proxy_info}")
     uvicorn.run(app, host="0.0.0.0", port=LISTEN_PORT, log_level="error")
