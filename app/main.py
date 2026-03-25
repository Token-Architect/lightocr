import sys
from pathlib import Path

# 获取 main.py 所在的目录 (app/)
current_file_path = Path(__file__).resolve()
# 获取 app 目录
app_dir = current_file_path.parent
# 获取 OCR_SERVICE 目录 (app 的父目录)
project_root = app_dir.parent

# 将 OCR_SERVICE 目录添加到 sys.path
if str(project_root) not in sys.path:
    sys.path.insert(0, str(project_root))

import os
import json
import base64
import asyncio
import time
import math
import aiofiles
from datetime import datetime, timedelta
from fastapi import FastAPI, BackgroundTasks, HTTPException, UploadFile, File, Form
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles
from fastapi.responses import FileResponse, HTMLResponse
from contextlib import asynccontextmanager
from typing import Dict, Optional
try:
    import psutil
except ImportError:
    psutil = None

from app.schemas.ocr import OCRResponse, OCRResultResponse
from app.core.ocr_engine import extract_text_by_file_extension, convert_office_to_pdf

# 任务存储 (生产环境建议使用 Redis)
tasks: Dict[str, Dict] = {}

# 输出目录
# 使用绝对路径，确保文件保存位置正确
BASE_DIR = os.path.dirname(os.path.abspath(__file__)) # app/
PROJECT_ROOT = os.path.dirname(BASE_DIR) # 【OCR_SERVICE】【独立部署】/
OUTPUT_DIR = os.path.join(PROJECT_ROOT, "output")

METADATA_FILE = os.path.join(OUTPUT_DIR, "metadata.json")


def _get_int_env(name: str, default: int) -> int:
    value = os.getenv(name)
    if value is None:
        return default
    try:
        return int(value)
    except ValueError:
        return default


def apply_runtime_limits() -> None:
    """
    Optional runtime limits to keep OCR CPU usage under control.
    - OCR_CPU_LIMIT_PERCENT: limit process to a subset of CPU cores (via psutil cpu_affinity)
    - OCR_MAX_THREADS: cap native/math thread pools
    """
    cpu_limit_percent = max(1, min(100, _get_int_env("OCR_CPU_LIMIT_PERCENT", 100)))
    max_threads = _get_int_env("OCR_MAX_THREADS", 0)

    logical_cpus = os.cpu_count() or 1
    if max_threads <= 0 and cpu_limit_percent < 100:
        max_threads = max(1, math.floor(logical_cpus * cpu_limit_percent / 100))

    if max_threads > 0:
        os.environ.setdefault("OMP_NUM_THREADS", str(max_threads))
        os.environ.setdefault("OPENBLAS_NUM_THREADS", str(max_threads))
        os.environ.setdefault("MKL_NUM_THREADS", str(max_threads))
        os.environ.setdefault("NUMEXPR_NUM_THREADS", str(max_threads))
        os.environ.setdefault("VECLIB_MAXIMUM_THREADS", str(max_threads))
        os.environ.setdefault("BLIS_NUM_THREADS", str(max_threads))
        print(f"[RuntimeLimit] OCR_MAX_THREADS={max_threads}")

    if cpu_limit_percent >= 100:
        return

    if psutil is None:
        print("[RuntimeLimit] psutil not installed, skip OCR_CPU_LIMIT_PERCENT.")
        return

    try:
        process = psutil.Process(os.getpid())
        current_affinity = process.cpu_affinity() if hasattr(process, "cpu_affinity") else list(range(logical_cpus))
        if not current_affinity:
            current_affinity = list(range(logical_cpus))

        allow_cores = max(1, math.floor(len(current_affinity) * cpu_limit_percent / 100))
        new_affinity = current_affinity[:allow_cores]
        process.cpu_affinity(new_affinity)
        print(
            f"[RuntimeLimit] OCR_CPU_LIMIT_PERCENT={cpu_limit_percent}, "
            f"cpu_affinity={new_affinity}"
        )
    except Exception as e:
        print(f"[RuntimeLimit] Failed to apply CPU affinity: {e}")


OCR_MAX_CONCURRENT_TASKS = max(1, _get_int_env("OCR_MAX_CONCURRENT_TASKS", 2))
OCR_TASK_SEMAPHORE = asyncio.Semaphore(OCR_MAX_CONCURRENT_TASKS)

async def update_metadata(uuid: str, filename: str, status: str, 
                          file_size: int = 0, start_time: Optional[float] = None, end_time: Optional[float] = None):
    """更新元数据文件 (线程安全简单实现，高并发建议用数据库)"""
    try:
        # 读取现有数据
        data = {}
        if os.path.exists(METADATA_FILE):
            async with aiofiles.open(METADATA_FILE, 'r', encoding='utf-8') as f:
                content = await f.read()
                if content:
                    try:
                        data = json.loads(content)
                    except json.JSONDecodeError:
                        pass
        
        # 获取或初始化当前记录
        record = data.get(uuid, {})
        
        # 更新字段
        record.update({
            "original_filename": filename,
            "result_file": f"{uuid}.md",
            "status": status,
        })
        
        if file_size > 0:
            record["file_size_mb"] = round(file_size / (1024 * 1024), 2)
            
        if start_time:
            record["start_time"] = datetime.fromtimestamp(start_time).strftime('%Y-%m-%d %H:%M:%S')
            
        if end_time:
            record["end_time"] = datetime.fromtimestamp(end_time).strftime('%Y-%m-%d %H:%M:%S')
            # 尝试从 start_time 字符串反解析出时间戳来计算耗时
            # 这样就不需要持久化 _start_ts 字段了
            start_time_str = record.get("start_time")
            if start_time_str:
                try:
                    start_ts = datetime.strptime(start_time_str, '%Y-%m-%d %H:%M:%S').timestamp()
                    record["duration_seconds"] = round(end_time - start_ts, 2)
                except ValueError:
                    pass
        
        data[uuid] = record
        
        # 写入文件
        async with aiofiles.open(METADATA_FILE, 'w', encoding='utf-8') as f:
            await f.write(json.dumps(data, ensure_ascii=False, indent=2))
            
    except Exception as e:
        print(f"Failed to update metadata: {e}")

@asynccontextmanager
async def lifespan(app: FastAPI):
    apply_runtime_limits()
    print(f"[RuntimeLimit] OCR_MAX_CONCURRENT_TASKS={OCR_MAX_CONCURRENT_TASKS}")

    # 启动时确保输出目录存在
    if not os.path.exists(OUTPUT_DIR):
        os.makedirs(OUTPUT_DIR)
    
    # 确保 metadata.json 存在
    if not os.path.exists(METADATA_FILE):
        async with aiofiles.open(METADATA_FILE, 'w', encoding='utf-8') as f:
            await f.write("{}")

    # 启动后台清理任务
    cleanup_task_obj = asyncio.create_task(cleanup_cron_job())
            
    yield
    # 关闭时清理逻辑 (如有)
    cleanup_task_obj.cancel()
    try:
        await cleanup_task_obj
    except asyncio.CancelledError:
        pass

async def cleanup_cron_job():
    """每天 00:00 执行一次文件清理"""
    while True:
        try:
            now = datetime.now()
            # 计算距离明天 00:00 的秒数
            next_run = (now + timedelta(days=1)).replace(hour=0, minute=0, second=0, microsecond=0)
            sleep_seconds = (next_run - now).total_seconds()
            
            print(f"Next cleanup scheduled in {sleep_seconds:.2f} seconds (at {next_run})")
            await asyncio.sleep(sleep_seconds)
            
            # 执行清理
            await cleanup_expired_files()
            
        except asyncio.CancelledError:
            break
        except Exception as e:
            print(f"Cleanup cron job error: {e}")
            # 出错后等待一段时间重试，避免死循环
            await asyncio.sleep(60)

async def cleanup_expired_files():
    """清理 output 目录下超过指定天数的文件"""
    # 从环境变量获取保留天数，默认为 3 天
    try:
        retention_days = int(os.getenv("CLEANUP_RETENTION_DAYS", 3))
    except ValueError:
        retention_days = 3
        
    print(f"Starting daily cleanup of expired files (retention: {retention_days} days)...")
    cutoff_time = time.time() - (retention_days * 24 * 3600)
    
    deleted_files_count = 0
    
    # 1. 清理 output 目录下的文件
    for root, dirs, files in os.walk(OUTPUT_DIR):
        # 遍历文件
        for name in files:
            file_path = os.path.join(root, name)
            
            # 跳过 metadata.json 本身
            if os.path.abspath(file_path) == os.path.abspath(METADATA_FILE):
                continue
                
            try:
                # 获取文件修改时间
                mtime = os.path.getmtime(file_path)
                if mtime < cutoff_time:
                    os.remove(file_path)
                    deleted_files_count += 1
                    print(f"Deleted expired file: {file_path}")
            except Exception as e:
                print(f"Error checking/deleting file {file_path}: {e}")
    
    print(f"Cleanup finished. Deleted {deleted_files_count} files.")

    # 2. 清理 metadata.json 中的过期记录
    # 注意：这里我们只移除 metadata 中的记录，确保 metadata 不会无限增长
    # 实际文件可能已经被上面的逻辑删除了，也可能没被删（比如文件没过期但记录逻辑上过期了？通常保持一致）
    try:
        if os.path.exists(METADATA_FILE):
            async with aiofiles.open(METADATA_FILE, 'r', encoding='utf-8') as f:
                content = await f.read()
            
            data = {}
            if content:
                try:
                    data = json.loads(content)
                except json.JSONDecodeError:
                    pass
            
            new_data = {}
            modified = False
            
            for uuid, info in data.items():
                keep = True
                
                # 检查记录中的时间字段
                ts = None
                # 优先检查 end_time
                if info.get("end_time"):
                    try:
                        ts = datetime.strptime(info["end_time"], '%Y-%m-%d %H:%M:%S').timestamp()
                    except: pass
                
                # 其次检查 start_time
                if not ts and info.get("start_time"):
                    try:
                        ts = datetime.strptime(info["start_time"], '%Y-%m-%d %H:%M:%S').timestamp()
                    except: pass
                
                # 如果记录中有时间且超过 3 天，则标记为移除
                if ts and ts < cutoff_time:
                    keep = False
                    modified = True
                
                # 如果记录中没有时间（异常情况），但关联的文件已经不存在了，也移除？
                # 暂时保守策略：只移除明确过期的
                
                if keep:
                    new_data[uuid] = info
            
            if modified:
                async with aiofiles.open(METADATA_FILE, 'w', encoding='utf-8') as f:
                    await f.write(json.dumps(new_data, ensure_ascii=False, indent=2))
                print(f"Cleaned {len(data) - len(new_data)} expired entries from metadata.")
                
    except Exception as e:
        print(f"Error cleaning metadata: {e}")

app = FastAPI(title="LightOCR Service", lifespan=lifespan)

# 配置 CORS
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],  # 允许所有来源
    allow_credentials=True,
    allow_methods=["*"],  # 允许所有方法
    allow_headers=["*"],  # 允许所有头
)

# 挂载静态文件目录，用于访问图片
IMAGES_DIR = os.path.join(OUTPUT_DIR, "images")
if not os.path.exists(IMAGES_DIR):
    os.makedirs(IMAGES_DIR)
app.mount("/images", StaticFiles(directory=IMAGES_DIR), name="images")

# 挂载静态文件目录，用于 Web UI
STATIC_DIR = os.path.join(BASE_DIR, "static")
if not os.path.exists(STATIC_DIR):
    os.makedirs(STATIC_DIR)
app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")

# 挂载静态文件目录，用于临时文件访问（如转换后的PDF）
TEMP_DIR = os.path.join(OUTPUT_DIR, "temp")
if not os.path.exists(TEMP_DIR):
    os.makedirs(TEMP_DIR)
app.mount("/temp", StaticFiles(directory=TEMP_DIR), name="temp")

@app.get("/", response_class=HTMLResponse)
async def read_index():
    # 读取模板文件
    index_path = os.path.join(STATIC_DIR, "index.html")
    if not os.path.exists(index_path):
        return "Index file not found"
        
    async with aiofiles.open(index_path, 'r', encoding='utf-8') as f:
        content = await f.read()
        
    # 获取环境变量中的 SERVICE_BASE_URL，默认为 localhost
    service_base_url = os.getenv("SERVICE_BASE_URL", "http://目标ip:目标端口")
    # 移除末尾可能的斜杠，保持格式统一
    service_base_url = service_base_url.rstrip("/")
    
    # 替换模板变量
    content = content.replace("{{ SERVICE_BASE_URL }}", service_base_url)
    
    return content

@app.get("/tool", response_class=HTMLResponse)
async def read_tool():
    # 读取模板文件
    tool_path = os.path.join(STATIC_DIR, "ocr_tool.html")
    if not os.path.exists(tool_path):
        return "Tool file not found"
        
    async with aiofiles.open(tool_path, 'r', encoding='utf-8') as f:
        content = await f.read()
        
    # 获取环境变量中的 SERVICE_BASE_URL，默认为 localhost
    service_base_url = os.getenv("SERVICE_BASE_URL", "http://目标ip:目标端口")
    # 移除末尾可能的斜杠，保持格式统一
    service_base_url = service_base_url.rstrip("/")
    
    # 替换模板变量
    content = content.replace("{{ SERVICE_BASE_URL }}", service_base_url)
    
    return content

async def process_ocr_task(uuid: str, filename: str, content_bytes: bytes, ocr_method: str = "rapiddoc", cleanup_source: bool = False):
    """后台处理 OCR 任务"""
    start_time = None
    file_size = len(content_bytes)
    
    try:
        file_extension = os.path.splitext(filename)[1]
        
        # 运行 OCR (CPU 密集型)，通过全局信号量限制并发任务数
        async with OCR_TASK_SEMAPHORE:
            start_time = time.time()
            tasks[uuid]["status"] = "processing"
            await update_metadata(uuid, filename, "processing", file_size=file_size, start_time=start_time)

            loop = asyncio.get_event_loop()
            text = await loop.run_in_executor(
                None,
                lambda: extract_text_by_file_extension(
                    file_content=content_bytes,
                    file_extension=file_extension,
                    task_id=uuid,  # Pass the UUID as task_id/file_id
                    ocr_method=ocr_method,  # Pass the OCR method
                    cleanup_source=cleanup_source  # Pass cleanup flag
                )
            )
        
        # 保存结果到文件
        output_path = os.path.join(OUTPUT_DIR, f"{uuid}.md")
        async with aiofiles.open(output_path, 'w', encoding='utf-8') as f:
            await f.write(text)
            
        end_time = time.time()
        duration = round(end_time - (start_time or end_time), 2)
        tasks[uuid]["status"] = "completed"
        tasks[uuid]["content"] = text # 可选：在内存中也保留一份，或者只依靠文件
        tasks[uuid]["duration"] = duration
        await update_metadata(uuid, filename, "completed", end_time=end_time)
        
    except Exception as e:
        end_time = time.time()
        tasks[uuid]["status"] = "failed"
        tasks[uuid]["error"] = str(e)
        await update_metadata(uuid, filename, "failed", end_time=end_time)

@app.get("/.well-known/appspecific/com.chrome.devtools.json")
async def chrome_devtools_config():
    return {}

@app.get("/favicon.ico", include_in_schema=False)
async def favicon():
    # 优先查找 favicon.ico，如果没有则尝试使用 favicon.jpeg
    ico_path = os.path.join(STATIC_DIR, "favicon.ico")
    jpeg_path = os.path.join(STATIC_DIR, "favicon.jpeg")
    
    if os.path.exists(ico_path):
        return FileResponse(ico_path)
    elif os.path.exists(jpeg_path):
        return FileResponse(jpeg_path, media_type="image/jpeg")
    else:
        return HTMLResponse("")

@app.post("/ocr/preview")
async def preview_office_file(
    file: UploadFile = File(...),
    uuid: str = Form(...),
    ocr_method: str = Form("rapiddoc"),
):
    """
    预览 Office 文件
    - 将 Office 文件转换为 PDF 并返回预览 URL
    """
    try:
        content_bytes = await file.read()
        filename = file.filename
        file_extension = os.path.splitext(filename)[1].lower()
        
        if file_extension not in ['.doc', '.docx', '.ppt', '.pptx', '.pdf']:
            raise HTTPException(status_code=400, detail="Only Office files and PDF are supported for preview")

        # 如果是 PDF，直接保存到目标位置
        # 统一使用 source 目录存放源文件/转换后的文件
        final_output_dir = os.path.join(OUTPUT_DIR, "temp", "source", uuid)
        os.makedirs(final_output_dir, exist_ok=True)
        pdf_path = os.path.join(final_output_dir, f"{uuid}.pdf")
        
        if file_extension == '.pdf':
            async with aiofiles.open(pdf_path, 'wb') as f:
                await f.write(content_bytes)
        else:
            # 转换 Office 文件为 PDF
            # 这是一个同步/CPU密集型操作，建议放入线程池
            loop = asyncio.get_event_loop()
            # convert_office_to_pdf 内部也已更新为使用 output/temp/source/{uuid}
            pdf_path = await loop.run_in_executor(
                None, 
                lambda: convert_office_to_pdf(
                    file_content=content_bytes,
                    file_extension=file_extension,
                    task_id=uuid,
                    ocr_method=ocr_method
                )
            )

        # 构建预览 URL
        # 路径: /temp/source/{uuid}/{uuid}.pdf
        service_base_url = os.getenv("SERVICE_BASE_URL", "http://目标ip:目标端口").rstrip('/')
        preview_url = f"{service_base_url}/temp/source/{uuid}/{uuid}.pdf"
        
        return {
            "task_id": uuid,
            "preview_url": preview_url,
            "status": "ready"
        }

    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))

@app.post("/ocr/submit", response_model=OCRResponse)
async def submit_ocr_task(
    background_tasks: BackgroundTasks,
    file: Optional[UploadFile] = File(None),
    uuid: str = Form(...),
    ocr_method: str = Form("rapiddoc"),
    preview_id: Optional[str] = Form(None),
):
    """
    提交 OCR 任务 (Multipart/Form-Data)
    - file: 上传的文件二进制流 (可选，如果已预览则可为空)
    - uuid: 任务唯一标识
    - ocr_method: OCR 方法选择，可选值: "rapiddoc" (默认), "vlm_opencv"
    - preview_id: 预览任务的 ID (可选，用于复用已转换的 PDF)
    """
    if uuid in tasks:
         # 简单去重，如果任务已存在且未失败，直接返回
         if tasks[uuid]["status"] not in ["failed"]:
             return OCRResponse(task_id=uuid, status=tasks[uuid]["status"])

    filename = "unknown"
    content_bytes = None
    used_preview = False

    # 1. 尝试从预览结果中复用 PDF (优先级最高，因为可以节省转换时间)
    if preview_id:
        # 预览文件现在统一在 source 目录下
        # 路径: output/temp/source/{preview_id}/{preview_id}.pdf
        preview_pdf_path = os.path.join(OUTPUT_DIR, "temp", "source", preview_id, f"{preview_id}.pdf")
        
        # 增加短暂等待机制：如果用户点击太快，预览可能正在生成中
        # 等待最多 2 秒 (0.1s * 20)
        for _ in range(20):
            if os.path.exists(preview_pdf_path) and os.path.getsize(preview_pdf_path) > 0:
                break
            await asyncio.sleep(0.1)
        
        if os.path.exists(preview_pdf_path) and os.path.getsize(preview_pdf_path) > 0:
            print(f"[Task {uuid}] Reusing preview PDF from {preview_id}")
            async with aiofiles.open(preview_pdf_path, 'rb') as f:
                content_bytes = await f.read()
            filename = f"{uuid}.pdf" # 复用后视为 PDF 任务
            used_preview = True
            
            # 如果复用了预览文件，我们可以预先将其复制到当前任务的 temp 目录
            # 这样 process_ocr_task 中的 convert 逻辑就会发现文件已存在并直接返回
            # 但由于我们把 filename 改成了 .pdf，process_ocr_task 会直接走 PDF 处理流程，跳过 convert
            # 这也是符合预期的
            
            # 为了确保一致性，如果当前方法不是 rapiddoc (比如是 vlm_opencv)，
            # 或者是 rapiddoc 但 task_id 不同，我们需要把文件放到正确的位置
            # 目标位置: output/temp/{ocr_method}/{uuid}/{uuid}.pdf (如果不改 filename)
            # 或者 output/temp/{ocr_method}/{uuid}/{filename}
            
            # 这里我们不做手动复制，直接把 content_bytes 传给 process_ocr_task
            # process_ocr_task 会根据 filename (.pdf) 调用 extract_text_by_file_extension
            # -> _extract_text_from_pdf
            # -> _process_with_rapiddoc 或 _extract_text_from_pdf_vlm_opencv
            # 这些函数内部会处理文件保存

    # 2. 如果没能复用预览，则使用上传的文件
    if not content_bytes and file:
        try:
            content_bytes = await file.read()
            filename = file.filename
        except Exception:
            raise HTTPException(status_code=400, detail="Failed to read file")
    
    # 3. 最后的兜底：检查是否直接以当前 uuid 作为 preview_id 传进来了但没文件 (旧逻辑兼容)
    if not content_bytes:
          # 检查 source 目录
          preview_pdf_path = os.path.join(OUTPUT_DIR, "temp", "source", uuid, f"{uuid}.pdf")
          if os.path.exists(preview_pdf_path):
               async with aiofiles.open(preview_pdf_path, 'rb') as f:
                  content_bytes = await f.read()
               filename = f"{uuid}.pdf"
          else:
              raise HTTPException(status_code=400, detail="File is required (or valid preview_id)")

    tasks[uuid] = {
        "status": "pending",
        "filename": filename
    }
    
    # 初始记录元数据 (pending 状态暂时不记录开始时间，等真正开始处理时记录)
    await update_metadata(uuid, filename, "pending", file_size=len(content_bytes))
    
    # 判断是否需要清理源文件
    # 如果当前任务是用于预览生成的（即复用了 preview_id 且 uuid==preview_id），则不清理
    # 如果是全新的任务（uuid != preview_id），则可以在处理完后清理
    # 注意：这里的 cleanup_source 逻辑需要传递给 process_ocr_task
    # 但 process_ocr_task 目前签名不支持。我们需要修改 process_ocr_task。
    
    # 暂时简化策略：
    # 如果我们复用了 preview_id (filename是.pdf)，说明源文件是 preview 目录下的。
    # 那个文件是公共的，不应该被某个任务删掉。
    # 只有当 filename 是原始 Office 文件（未复用），且 uuid != preview_id 时，
    # 说明我们在 process_ocr_task 内部生成了临时的 PDF。
    # 这种情况下，process_ocr_task 内部的 extract_text_by_file_extension 会处理。
    # 我们需要在 extract_text_by_file_extension 中知道是否清理。
    
    # 实际上，现在的逻辑是：
    # 1. 预览/SingleMode -> 存入 temp/source/{uuid}。保留。
    # 2. CompareMode -> 复用 1 的文件。不生成新文件。保留。
    # 3. Fallback (CompareMode, Preview未完成) -> 生成到 temp/source/{uuid}。
    #    这个是冗余的。应该清理。
    
    # 我们通过参数 cleanup_source_after 传递意图
    cleanup_source = False
    if preview_id and uuid != preview_id and not used_preview:
        # 这是一个临时任务，且没有复用现成文件（即自己上传/转换了新文件）
        cleanup_source = True

    background_tasks.add_task(process_ocr_task, uuid, filename, content_bytes, ocr_method, cleanup_source)
    
    return OCRResponse(task_id=uuid, status="pending")

@app.get("/ocr/status/{task_id}", response_model=OCRResponse)
async def get_task_status(task_id: str):
    """查询任务状态"""
    if task_id not in tasks:
        raise HTTPException(status_code=404, detail="Task not found")
    
    return OCRResponse(task_id=task_id, status=tasks[task_id]["status"])

@app.get("/ocr/result/{task_id}", response_model=OCRResultResponse)
async def get_task_result(task_id: str):
    """获取任务结果"""
    if task_id not in tasks:
        raise HTTPException(status_code=404, detail="Task not found")
    
    task = tasks[task_id]
    
    if task["status"] == "completed":
        # 尝试从文件读取（以防重启后内存丢失但文件还在，虽然这里内存是易失的）
        # 实际生产应优先读数据库或持久化存储
        content = task.get("content")
        if not content:
            output_path = os.path.join(OUTPUT_DIR, f"{task_id}.md")
            if os.path.exists(output_path):
                async with aiofiles.open(output_path, 'r', encoding='utf-8') as f:
                    content = await f.read()
            else:
                # 异常情况：状态完成但文件丢失
                return OCRResultResponse(
                    task_id=task_id, 
                    status="failed", 
                    error="Result file missing"
                )
                
        return OCRResultResponse(
            task_id=task_id,
            status="completed",
            filename=task["filename"],
            content=content,
            duration=task.get("duration")
        )
        
    elif task["status"] == "failed":
        return OCRResultResponse(
            task_id=task_id,
            status="failed",
            error=task.get("error", "Unknown error")
        )
    else:
        return OCRResultResponse(
            task_id=task_id,
            status=task["status"]
        )

if __name__ == "__main__":
    import uvicorn
    uvicorn.run("app.main:app", host="0.0.0.0", port=7778, reload=True)
