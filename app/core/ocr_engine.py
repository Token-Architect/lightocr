import io
import base64
import os
import subprocess
import tempfile
import json
import docx
import pypdfium2
import fitz  # PyMuPDF
import xlrd
import openpyxl
import cv2
import numpy as np
import re
import asyncio
import shutil
from pathlib import Path
from concurrent.futures import ThreadPoolExecutor, as_completed
from typing import List, Tuple, Optional, Dict, Any
from PIL import Image, ImageDraw
from openai import OpenAI
from dotenv import load_dotenv

# 加载 .env 文件
load_dotenv()

# 环境变量获取
OCR_SERVICE_URL = os.getenv("OCR_SERVICE_URL")
OCR_API_KEY = os.getenv("OCR_API_KEY")
OCR_MODEL_NAME = os.getenv("OCR_MODEL_NAME")


def _get_int_env(name: str, default: int) -> int:
    value = os.getenv(name)
    if value is None:
        return default
    try:
        return int(value)
    except ValueError:
        return default


# Optional OpenCV CPU thread cap (0 means use library default)
OCR_MAX_THREADS = max(0, _get_int_env("OCR_MAX_THREADS", 0))
if OCR_MAX_THREADS > 0:
    cv2.setNumThreads(OCR_MAX_THREADS)

def extract_text_by_file_extension(*, file_content: bytes, file_extension: str, task_id: str = "unknown", cleanup_source: bool = False, **kwargs) -> str:
    """Extract text from a file based on its file extension."""
    file_extension = file_extension.lower()
    
    # 优先使用 kwargs 中的配置，否则回退到环境变量
    ocr_service_url = kwargs.get("ocr_service_url") or OCR_SERVICE_URL
    ocr_api_key = kwargs.get("ocr_api_key") or OCR_API_KEY
    ocr_model_name = kwargs.get("ocr_model_name") or OCR_MODEL_NAME
    
    # 更新 kwargs 以便后续传递
    kwargs.update({
        "ocr_service_url": ocr_service_url,
        "ocr_api_key": ocr_api_key,
        "ocr_model_name": ocr_model_name,
        "task_id": task_id,
        "cleanup_source": cleanup_source
    })

    match file_extension:
        case ".txt" | ".md" | ".html":
            return _extract_text_from_plain_text(file_content)
        case ".xls" | ".xlsx":
            return _extract_text_from_excel(file_content, file_extension)
        case ".doc" | ".docx":
            return _extract_text_from_office_via_ocr(file_content, file_extension, **kwargs)
        case ".pdf":
            return _extract_text_from_pdf(file_content, **kwargs)
        case ".ppt" | ".pptx":
            return _extract_text_from_office_via_ocr(file_content, file_extension, **kwargs)
        case ".jpg" | ".jpeg" | ".png":
            return _extract_text_from_image(file_content, file_extension, **kwargs)
        case _:
            return f"{file_extension}不是支持的文件格式"

def _extract_text_from_plain_text(file_content: bytes) -> str:
    try:
        try:
            return file_content.decode("utf-8")
        except UnicodeDecodeError:
            try:
                return file_content.decode("gbk")
            except UnicodeDecodeError:
                return file_content.decode("utf-8", "ignore")
    except Exception as e:
        raise Exception(f"Failed to decode plain text file: {e}")

def _extract_text_from_excel(file_content: bytes, file_extension: str) -> str:
    try:
        text = ""
        if file_extension == '.xlsx':
            wb = openpyxl.load_workbook(filename=io.BytesIO(file_content), data_only=True)
            for sheet in wb.sheetnames:
                ws = wb[sheet]
                text += f"=== Sheet: {sheet} ===\n"
                for row in ws.rows:
                    row_text = "\t".join([str(cell.value) if cell.value is not None else "" for cell in row])
                    if row_text.strip():
                         text += row_text + "\n"
        elif file_extension == '.xls':
             wb = xlrd.open_workbook(file_contents=file_content)
             for sheet in wb.sheets():
                text += f"=== Sheet: {sheet.name} ===\n"
                for row_idx in range(sheet.nrows):
                    row = sheet.row(row_idx)
                    row_text = "\t".join([str(cell.value) if cell.value is not None else "" for cell in row])
                    if row_text.strip():
                        text += row_text + "\n"
        return text
    except Exception as e:
        raise Exception(f"Failed to extract text from Excel: {e}")

import platform
import threading

# 全局锁，用于防止并发调用 Office 转换导致 COM 冲突
office_conversion_lock = threading.Lock()

def convert_office_to_pdf(file_content: bytes, file_extension: str, task_id: str = "unknown", ocr_method: str = "rapiddoc") -> str:
    """
    Public wrapper for _convert_office_to_pdf
    """
    return _convert_office_to_pdf(file_content, file_extension, task_id, ocr_method)

def _convert_office_to_pdf(file_content: bytes, file_extension: str, task_id: str = "unknown", ocr_method: str = "rapiddoc") -> str:
    """
    将Office文件转换为PDF，自动根据操作系统选择转换方式：
    - Windows: 使用 win32com (需要安装 Office)
    - Linux/Docker: 使用 LibreOffice
    
    使用全局锁确保同一时间只有一个转换任务在进行。
    """
    # 获取锁，防止并发执行
    with office_conversion_lock:
        print(f"[OCR] Starting Office to PDF conversion for task {task_id} (Extension: {file_extension})")
        system_name = platform.system()
        base_dir = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
        
        # Use task_id as filename for predictability
        filename_base = task_id
        
        # Directory structure: output/temp/source/{task_id}
        # Unified location for converted PDFs, independent of OCR method
        final_output_dir = os.path.join(base_dir, "output", "temp", "source", task_id)
        os.makedirs(final_output_dir, exist_ok=True)
        
        # Target PDF path
        # Use task_id.pdf so it matches what frontend expects (or what we want to standardize)
        pdf_path = os.path.join(final_output_dir, f"{filename_base}.pdf")
        
        # Check if already exists (cache?)
        if os.path.exists(pdf_path) and os.path.getsize(pdf_path) > 0:
            print(f"[OCR] Using cached PDF for task {task_id}")
            return pdf_path

        # Create temporary input file
        temp_input_dir = tempfile.mkdtemp()
        input_path = os.path.join(temp_input_dir, f"{filename_base}{file_extension}")
        
        with open(input_path, 'wb') as f:
            f.write(file_content)
            
        try:
            if system_name == "Windows":
                # Windows 环境下使用 win32com 调用本地 Office
                import win32com.client
                import pythoncom
                
                # 初始化 COM 库 (对于多线程环境至关重要)
                pythoncom.CoInitialize()
                try:
                    if file_extension in ['.doc', '.docx']:
                        # Use DispatchEx to force a new instance, avoiding conflicts with existing instances
                        word = win32com.client.DispatchEx("Word.Application")
                        word.Visible = False
                        word.DisplayAlerts = False # Disable alerts
                        try:
                            doc = word.Documents.Open(input_path)
                            doc.SaveAs(pdf_path, FileFormat=17) # wdFormatPDF = 17
                            doc.Close()
                        finally:
                            try:
                                word.Quit()
                            except Exception as e:
                                print(f"Warning: Failed to quit Word: {e}")
                            
                    elif file_extension in ['.ppt', '.pptx']:
                        # Use DispatchEx to force a new instance
                        powerpoint = win32com.client.DispatchEx("PowerPoint.Application")
                        try:
                            presentation = powerpoint.Presentations.Open(input_path, WithWindow=False)
                            presentation.SaveAs(pdf_path, 32) # ppSaveAsPDF = 32
                            presentation.Close()
                        finally:
                            try:
                                powerpoint.Quit()
                            except Exception as e:
                                print(f"Warning: Failed to quit PowerPoint: {e}")
                    else:
                        raise Exception(f"Unsupported office file type on Windows: {file_extension}")
                        
                finally:
                    pythoncom.CoUninitialize()
                    
            else:
                # Linux/Docker 环境下使用 LibreOffice
                # Create a temporary directory for output to avoid permission issues
                output_dir = tempfile.mkdtemp()
                # Create a temporary directory for UserInstallation to allow concurrent execution
                user_profile_dir = tempfile.mkdtemp()
                
                try:
                    # Construct command as a list of strings
                    # Use a unique UserInstallation directory to avoid profile lock conflicts
                    cmd = [
                        "libreoffice",
                        f"-env:UserInstallation=file://{user_profile_dir}",
                        "--headless",
                        "--convert-to",
                        "pdf",
                        "--outdir",
                        output_dir,
                        input_path
                    ]
                    
                    # Execute command
                    result = subprocess.run(
                        cmd, 
                        check=True, 
                        stdout=subprocess.PIPE, 
                        stderr=subprocess.PIPE
                    )
                    
                    # Check for output file
                    # LibreOffice usually names it [filename].pdf in the output directory
                    # Since input filename is task_id.ext, output should be task_id.pdf
                    generated_filename = f"{filename_base}.pdf"
                    generated_pdf_path = os.path.join(output_dir, generated_filename)
                    
                    if os.path.exists(generated_pdf_path):
                        # Move to expected location or return this path
                        # Here we move it to match the expected pdf_path if possible, or just return content
                        # Let's move it to be safe with the rest of the logic
                        shutil.move(generated_pdf_path, pdf_path)
                    else:
                        # Fallback check: sometimes LibreOffice renames differently?
                        # List dir content
                        files = os.listdir(output_dir)
                        if files:
                            shutil.move(os.path.join(output_dir, files[0]), pdf_path)
                        else:
                            raise Exception(f"LibreOffice succeeded but PDF not found at {generated_pdf_path}")
                        
                except subprocess.CalledProcessError as e:
                    error_msg = e.stderr.decode() if e.stderr else "Unknown error"
                    raise Exception(f"LibreOffice conversion failed with code {e.returncode}: {error_msg}")
                except FileNotFoundError:
                    raise Exception("LibreOffice not found. Please install libreoffice on your system.")
                finally:
                    # Cleanup temp output dir
                    if os.path.exists(output_dir):
                        shutil.rmtree(output_dir, ignore_errors=True)
                    # Cleanup user profile dir
                    if os.path.exists(user_profile_dir):
                        shutil.rmtree(user_profile_dir, ignore_errors=True)
            
            if os.path.exists(pdf_path):
                return pdf_path
            else:
                raise Exception("PDF conversion failed, output file not found")
                
        except Exception as e:
            if os.path.exists(pdf_path):
                try:
                    os.remove(pdf_path)
                except:
                    pass
            raise Exception(f"Office to PDF conversion failed: {e}")
        finally:
            # 清理输入临时文件
            if os.path.exists(temp_input_dir):
                try:
                    shutil.rmtree(temp_input_dir, ignore_errors=True)
                except:
                    pass

def _extract_text_from_office_via_ocr(file_content: bytes, file_extension: str, **kwargs) -> str:
    pdf_path = None
    task_id = kwargs.get("task_id", "unknown")
    cleanup_source = kwargs.get("cleanup_source", False)
    
    try:
        # Convert Office to PDF first
        pdf_path = _convert_office_to_pdf(file_content, file_extension, task_id=task_id)
        
        # Read the converted PDF
        with open(pdf_path, 'rb') as f:
            pdf_content = f.read()
            
        # Extract text from the PDF using the specified method (or default)
        # Ensure ocr_method is passed correctly
        return _extract_text_from_pdf(pdf_content, **kwargs)
    except Exception as e:
        raise Exception(f"Office OCR processing failed: {e}")
    finally:
        # Only cleanup if explicitly requested (e.g. redundant task in Compare Mode)
        if cleanup_source and pdf_path:
             try:
                 folder = os.path.dirname(pdf_path)
                 if "source" in folder and task_id in folder: # Safety check
                    shutil.rmtree(folder, ignore_errors=True)
             except:
                 pass

def _call_ocr_llm(client: OpenAI, model_name: str, image_base64: str, mime_type: str) -> Dict[str, Any]:
    """
    Call OCR LLM to extract text and detect diagrams.
    Returns a dictionary with 'content' and 'diagram_bboxes'.
    """
    try:
        response = client.chat.completions.create(
            model=model_name,
            stream=False,
            response_format={ "type": "json_object" },
            messages=[
                {"role": "system", "content": "你是专业的理科助教，负责提取题目内容。"},
                {
                    "role": "user",
                    "content": [
                        {
                            "type": "image_url",
                            "image_url": {"url": f"data:{mime_type};base64,{image_base64}"},
                        },
                        {
                            "type": "text",
                            "text": """
                            请分析图片内容并返回JSON格式数据。
                            要求：
                            1. "content": 识别出的完整文本内容（Markdown格式）。
                               - 公式使用LaTeX格式，行内公式用$包裹，独立公式用$$包裹。
                               - **重要**：请确保输出合法的JSON字符串。LaTeX公式中的反斜杠应正确转义，例如输出 `\\frac` (即JSON中的 `\\frac`) 来表示 `\frac`。
                               - **表格处理**：遇到表格时，必须将其转换为Markdown表格格式，直接包含在文本中。**严禁**将表格作为图片处理。
                               - 保持原文结构。
                               - 忽略页眉页脚和水印。
                               - **关键要求**：在识别文本时，如果遇到文中原本包含配图的位置（如“如图所示”后、段落间、题目与解题过程之间），请插入自定义占位符 `[[IMAGE_0]]`, `[[IMAGE_1]]` 等。
                               - 占位符的索引（0, 1...）必须与 `diagram_bboxes` 列表中的顺序严格对应。
                               
                            2. "diagram_bboxes": 识别图片中所有的**几何图形、物理结构图、函数图像**的边界框列表。
                               - **排除对象**：**严禁**包含表格、纯文本段落、数学公式推导过程。表格必须在 content 中以 Markdown 呈现。
                               - 格式：[[xmin, ymin, xmax, ymax], ...] (0-1000 归一化坐标)。
                               - 顺序说明：[左, 上, 右, 下]。
               - **精确性要求**：
                  - **核心原则**：只包含图形本身，**绝对排除**周围的文字行、公式行。
                  - **边界控制**：
                    - 上边缘：不要包含上一行文字的下沿。
                    - 下边缘：不要包含下一行公式或说明文字（如 "图1"、"(a)" 或算式）。
                    - 左右边缘：紧贴图形，不要包含段落文字。
                  - 如果图形与文字距离很近，**宁可切掉一点空白或边缘，也不要包含文字**。
                - 边界框应紧贴图形边缘，不要包含周围文字。
                            """,
                        },
                    ],
                },
            ],
            temperature=0.1,
            max_tokens=4000,
        )
        content_str = response.choices[0].message.content
        
        # Fix: OpenAI JSON mode might return unescaped backslashes for LaTeX commands
        # causing json.loads to interpret them as control characters or invalid escapes.
        if content_str:
            # 1. Fix illegal JSON escapes: \ followed by any char that is NOT valid for JSON escape
            # Valid JSON escapes: ", \, /, b, f, n, r, t, u
            # We escape any backslash that is NOT followed by one of these.
            # This handles \Delta, \alpha, \sigma, \lim, \sum, etc.
            content_str = re.sub(r'(?<!\\)\\(?![\\"/bfnrtu])', r'\\\\', content_str)
            
            # 2. Fix potential conflicts with control characters (\b, \f, \r, \t)
            # We assume the model intends to output LaTeX, not control characters.
            # \frac, \forall -> \f
            # \beta, \bar, \begin -> \b
            # \right, \rho -> \r
            # \tan, \times, \text, \tau, \theta -> \t
            content_str = re.sub(r'(?<!\\)\\(b|f|r|t)', r'\\\\\1', content_str)
            
            # 3. Fix conflicts with \n (newline) vs LaTeX (\nabla, \nu, \neq, \notin, \natural)
            # We only escape \n if it looks like a known LaTeX command
            content_str = re.sub(r'(?<!\\)\\n(?=abla|eq|u|atural|eg|otin)', r'\\\\n', content_str)

        try:
            return json.loads(content_str)
        except json.JSONDecodeError:
            # Fallback if model returns text instead of JSON
            return {"content": content_str, "diagram_bboxes": []}
            
    except Exception as e:
        print(f"OCR LLM call failed: {e}")
        # Return a partial result with error info
        return {"content": "", "diagram_bboxes": [], "error": str(e)}

def _refine_bbox_with_opencv(image_cv, xmin, ymin, xmax, ymax):
    """
    Use OpenCV to refine the bounding box within the given ROI.
    Expands the ROI slightly, finds contours, and returns the tight bounding box of the content.
    Only considers contours that intersect significantly (>20%) with the original LLM bbox.
    """
    h, w = image_cv.shape[:2]
    
    # 1. Expand ROI (Region of Interest) by a margin to include potential cut-off parts
    # Reduced margin to avoid catching too much surrounding text/formulas
    margin_x = 40
    margin_y = 60 
    
    roi_xmin = max(0, int(xmin - margin_x))
    roi_ymin = max(0, int(ymin - margin_y))
    roi_xmax = min(w, int(xmax + margin_x))
    roi_ymax = min(h, int(ymax + margin_y))
    
    roi = image_cv[roi_ymin:roi_ymax, roi_xmin:roi_xmax]
    
    if roi.size == 0:
        return xmin, ymin, xmax, ymax

    # 2. Preprocessing
    gray = cv2.cvtColor(roi, cv2.COLOR_BGR2GRAY)
    
    # Use Canny edge detection instead of binary threshold
    # This is much better at catching thin lines (axes, arrows) even if they are faint
    edges = cv2.Canny(gray, 50, 150)
    
    # 2.5 Dilation to connect dashed lines and nearby components
    kernel = np.ones((5,5), np.uint8)
    dilated = cv2.dilate(edges, kernel, iterations=2)
    
    # 3. Find Contours on Dilated Image
    contours, _ = cv2.findContours(dilated, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE)
    
    if not contours:
        return xmin, ymin, xmax, ymax

    # 4. Filter contours based on Intersection Ratio
    # We only keep contours that have a significant overlap with the original bbox
    
    # Original bbox in ROI coordinates:
    orig_x = xmin - roi_xmin
    orig_y = ymin - roi_ymin
    orig_w = xmax - xmin
    orig_h = ymax - ymin
    orig_area = orig_w * orig_h
    
    valid_rects = []
    min_area = 50
    
    for cnt in contours:
        if cv2.contourArea(cnt) < min_area:
            continue
            
        x, y, cw, ch = cv2.boundingRect(cnt)
        cnt_area = cw * ch
        
        # Check intersection
        inter_xmin = max(x, orig_x)
        inter_ymin = max(y, orig_y)
        inter_xmax = min(x + cw, orig_x + orig_w)
        inter_ymax = min(y + ch, orig_y + orig_h)
        
        if inter_xmin < inter_xmax and inter_ymin < inter_ymax:
            inter_w = inter_xmax - inter_xmin
            inter_h = inter_ymax - inter_ymin
            inter_area = inter_w * inter_h
            
            # Ratio of the contour that is inside the original box
            # If the contour is the diagram, it should be mostly inside (ratio close to 1.0)
            # If the contour is text below, it touches only the edge (ratio close to 0.0)
            ratio = inter_area / cnt_area
            
            # Threshold: 20% of the contour must be inside the original box
            if ratio > 0.2:
                valid_rects.append((x, y, cw, ch))
    
    if not valid_rects:
        # Fallback: if no valid contours found, return original
        return xmin, ymin, xmax, ymax
        
    # Combine all valid rects to find the big bounding box
    # Find min_x, min_y, max_x, max_y among all valid rects
    min_x = min([r[0] for r in valid_rects])
    min_y = min([r[1] for r in valid_rects])
    max_x = max([r[0] + r[2] for r in valid_rects])
    max_y = max([r[1] + r[3] for r in valid_rects])
    
    # 5. Map back to original image coordinates
    new_xmin = roi_xmin + min_x
    new_ymin = roi_ymin + min_y
    new_xmax = roi_xmin + max_x
    new_ymax = roi_ymin + max_y
    
    # Optional: Add a small padding (5px) for aesthetics
    pad = 5
    new_xmin = max(0, new_xmin - pad)
    new_ymin = max(0, new_ymin - pad)
    new_xmax = min(w, new_xmax + pad)
    new_ymax = min(h, new_ymax + pad)
    
    return new_xmin, new_ymin, new_xmax, new_ymax

def _process_diagram_cropping(image_bytes: bytes, bbox: List[int], file_id: str = "unknown") -> str:
    """
    Crop image based on bbox (0-1000 normalized) and upload to TOS.
    Uses OpenCV to refine the cropping area.
    Returns the markdown image link.
    """
    if not bbox or len(bbox) != 4:
        return ""
    
    try:
        # Load image with PIL for initial size info and later cropping
        with Image.open(io.BytesIO(image_bytes)) as img:
            w, h = img.size
            
            # Convert bytes to OpenCV format for processing
            nparr = np.frombuffer(image_bytes, np.uint8)
            image_cv = cv2.imdecode(nparr, cv2.IMREAD_COLOR)
            
            xmin, ymin, xmax, ymax = bbox
            
            # Convert 0-1000 to pixels (LLM rough estimate)
            left_llm = int((xmin / 1000) * w)
            top_llm = int((ymin / 1000) * h)
            right_llm = int((xmax / 1000) * w)
            bottom_llm = int((ymax / 1000) * h)
            
            # Refine with OpenCV
            left, top, right, bottom = _refine_bbox_with_opencv(
                image_cv, left_llm, top_llm, right_llm, bottom_llm
            )
            
            # Debug: Draw bbox on a copy
            try:
                # Use path relative to the application root to avoid CWD issues and absolute path issues in Docker
                # app/core/ocr_engine.py -> app/core -> app -> PROJECT_ROOT
                base_dir = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
                
                # Modified: Use task-specific debug directory output/debug/{task_id}
                debug_dir = os.path.join(base_dir, "output", "debug", file_id)
                os.makedirs(debug_dir, exist_ok=True)
                
                import uuid
                u_id = str(uuid.uuid4())[:8]
                debug_path = os.path.join(debug_dir, f"bbox_{u_id}.png")
                
                # Draw both LLM bbox (blue) and Refined bbox (red)
                debug_img = image_cv.copy()
                cv2.rectangle(debug_img, (left_llm, top_llm), (right_llm, bottom_llm), (255, 0, 0), 2) # Blue: LLM
                cv2.rectangle(debug_img, (left, top), (right, bottom), (0, 0, 255), 2) # Red: Refined
                cv2.imwrite(debug_path, debug_img)
                print(f"[DEBUG] Saved bbox visualization to {debug_path}")
            except Exception as e:
                print(f"[DEBUG] Failed to save debug image: {e}")

            # Crop using the refined coordinates
            cropped = img.crop((left, top, right, bottom))
            
            # Enhance image for display: Add white padding and resize if needed
            # 1. Create a white background slightly larger
            cw, ch = cropped.size
            padding = 20
            new_w = cw + 2 * padding
            new_h = ch + 2 * padding
            new_img = Image.new("RGB", (new_w, new_h), (255, 255, 255))
            new_img.paste(cropped, (padding, padding))
            
            # 2. Resize if too wide (e.g. > 1000px) to keep markdown clean, or keep original
            # Here we keep original resolution for quality, but the white padding helps it stand out
            final_img = new_img
            
            # Save to local file instead of upload
            try:
                # Use path relative to the application root
                # app/core/ocr_engine.py -> app/core -> app -> PROJECT_ROOT
                base_dir = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
                
                # Use file_id (usually task_id) as subdirectory to organize images
                task_dir = os.path.join(base_dir, "output", "images", file_id)
                os.makedirs(task_dir, exist_ok=True)
                
                # Generate unique filename
                out_format = img.format if img.format else 'PNG'
                filename = f"diagram_{u_id}.{out_format.lower()}"
                file_path = os.path.join(task_dir, filename)
                
                final_img.save(file_path)
                
                # Return local URL (assuming static file serving is set up at /images/)
                # Using relative path for portability or absolute URL based on service config
                # Here we return a path that the frontend/client can access via the static mount
                base_url = os.getenv("SERVICE_BASE_URL", "http://localhost:7778")
                url = f"{base_url}/images/{file_id}/{filename}"
                return f"\n\n![diagram]({url})\n\n"
            except Exception as e:
                print(f"Failed to save local image: {e}")
                return ""
    except Exception as e:
        print(f"Cropping failed: {e}")
        return ""

def _process_with_rapiddoc(file_content: bytes, file_extension: str, task_id: str = "unknown", **kwargs) -> str:
    """
    使用 RapidDoc 方法提取文本（支持图片和PDF）
    """
    print(f"[OCR] Processing task {task_id} with RapidDoc (Extension: {file_extension})")
    try:
        # 设置项目根目录
        base_dir = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
        
        # 配置 RapidDoc 模型下载路径到 app/models，方便后续 Docker 打包
        # 注意：必须在 import rapid_doc 之前设置环境变量，否则不会生效
        models_dir = os.path.join(base_dir, "app", "models")
        os.makedirs(models_dir, exist_ok=True)
        os.environ["RAPID_MODELS_DIR"] = models_dir
        
        from rapid_doc.cli.common import aio_do_parse, read_fn
        from rapid_doc.model.layout.rapid_layout_self import ModelType
        
        # 创建临时目录用于输出
        # Modified: Use structured path output/temp/rapiddoc/{task_id}
        temp_dir = os.path.join(base_dir, "output", "temp", "rapiddoc", task_id)
        os.makedirs(temp_dir, exist_ok=True)
        
        # 保存文件到临时路径
        temp_file_path = os.path.join(temp_dir, f"{task_id}{file_extension}")
        with open(temp_file_path, 'wb') as f:
            f.write(file_content)
        
        # 准备 RapidDoc 参数
        pdf_file_names = [task_id]
        p_lang_list = ["ch"]
        
        # 根据文件类型准备输入数据
        if file_extension.lower() == '.pdf':
            # PDF 直接使用二进制内容
            pdf_bytes_list = [file_content]
        else:
            # 图片使用 read_fn 读取并转换 (会自动转换为 PDF 格式)
            pdf_bytes_list = [read_fn(temp_file_path)]
        
        # 配置 PP-DocLayoutV3
        layout_config = {
            "model_type": ModelType.PP_DOCLAYOUTV3
        }
        
        # 调用 RapidDoc
        async def run_rapiddoc():
            await aio_do_parse(
                output_dir=temp_dir,
                pdf_file_names=pdf_file_names,
                pdf_bytes_list=pdf_bytes_list,
                p_lang_list=p_lang_list,
                layout_config=layout_config
            )
        
        # 运行异步函数
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
        try:
            loop.run_until_complete(run_rapiddoc())
        finally:
            loop.close()
        
        # 读取生成的 Markdown 文件
        # RapidDoc 输出结构: output_dir/pdf_file_name/parse_method/pdf_file_name.md
        # 这里 output_dir=temp_dir, pdf_file_name=task_id, parse_method="auto"
        md_file_path = os.path.join(temp_dir, task_id, "auto", f"{task_id}.md")
        if os.path.exists(md_file_path):
            with open(md_file_path, 'r', encoding='utf-8') as f:
                text = f.read()
            
            # 处理图片路径：将相对路径转换为绝对路径
            # RapidDoc 生成的图片路径是 images/xxx.png
            # 需要转换为 /images/xxx.png 以便通过静态文件服务访问
            
            # 复制图片到全局 images 目录
            # Modified: Use task-specific image directory output/images/{task_id}
            # To avoid cluttering the root images folder
            images_dir = os.path.join(base_dir, "output", "images", task_id)
            os.makedirs(images_dir, exist_ok=True)
            
            # 查找所有图片文件
            auto_images_dir = os.path.join(temp_dir, task_id, "auto", "images")
            if os.path.exists(auto_images_dir):
                for img_file in os.listdir(auto_images_dir):
                    src_path = os.path.join(auto_images_dir, img_file)
                    dst_path = os.path.join(images_dir, img_file)
                    if not os.path.exists(dst_path):
                        shutil.copy(src_path, dst_path)
            
            # 替换 Markdown 中的图片路径
            # 使用 SERVICE_BASE_URL 拼接完整路径
            # Modified: path should be /images/{task_id}/xxx.png
            service_base_url = os.getenv("SERVICE_BASE_URL", "http://localhost:7778").rstrip('/')
            text = re.sub(r'!\[.*?\]\(images/(.*?)\)', fr'![\1]({service_base_url}/images/{task_id}/\1)', text)
            
            # 添加处理方法标识
            text += "\n\n<!-- Generated by RapidDoc -->"
            
            return text
        else:
            raise Exception(f"RapidDoc output file not found: {md_file_path}")
            
    except ImportError as e:
        raise Exception(f"RapidDoc not installed: {e}")
    except Exception as e:
        raise Exception(f"Failed to extract text with RapidDoc: {e}")
    finally:
        # 清理临时目录
        # 修改：由于源文件已统一保存在 output/temp/source 中，
        # RapidDoc 工作目录下的所有文件（包括输入副本和中间产物）都应被视为临时文件，任务完成后全部清理。
        cleanup_source = kwargs.get("cleanup_source", False)
        
        try:
            if os.path.exists(temp_dir):
                # 如果这是 RapidDoc 的工作目录，它可能包含从 input 复制过来的 task_id.pdf
                # 如果这个 task_id.pdf 也是我们唯一的源文件（比如 SingleMode 刚生成），不能删？
                # 不，_process_with_rapiddoc 的 temp_dir 是 `output/temp/rapiddoc/{task_id}`
                # 而源文件现在在 `output/temp/source/{task_id}`
                # 所以这里删除 temp_dir 是完全安全的！它只包含 RapidDoc 的中间产物。
                shutil.rmtree(temp_dir, ignore_errors=True)
                
            # 如果需要清理源文件 (cleanup_source=True)，则删除 source 目录
            # 注意：source 目录不在 temp_dir 中，它在 output/temp/source/{task_id}
            if cleanup_source:
                source_dir = os.path.join(base_dir, "output", "temp", "source", task_id)
                if os.path.exists(source_dir):
                    shutil.rmtree(source_dir, ignore_errors=True)
                    
        except Exception as e:
            print(f"Cleanup warning: {e}")

def _extract_text_from_image_vlm_opencv(file_content: bytes, file_extension: str, 
                                        ocr_service_url: str = None,
                                        ocr_api_key: str = None,
                                        ocr_model_name: str = None,
                                        **kwargs) -> str:
    """
    使用 VLM+OpenCV 方法提取图片文本（备选方案）
    """
    if not ocr_api_key:
        return "未配置OCR服务，无法识别图片内容"

    try:
        if file_extension.lower() in ['.jpg', '.jpeg']:
            mime_type = 'image/jpeg'
        elif file_extension.lower() == '.png':
            mime_type = 'image/png'
        else:
            mime_type = 'image/jpeg'

        image_base64 = base64.b64encode(file_content).decode("utf-8")
        client = OpenAI(base_url=ocr_service_url, api_key=ocr_api_key)
        
        result = _call_ocr_llm(client, ocr_model_name, image_base64, mime_type)
        
        text = result.get("content", "")
        
        # Handle multiple bboxes
        bboxes = result.get("diagram_bboxes", [])
        if "diagram_bbox" in result and result["diagram_bbox"]:
            bboxes.append(result["diagram_bbox"])
            
        # Upload images and replace placeholders
        uploaded_urls = []
        for bbox in bboxes:
            diagram_md = _process_diagram_cropping(file_content, bbox, file_id=kwargs.get("task_id", "unknown"))
            uploaded_urls.append(diagram_md)
            
        # Replace [[IMAGE_i]] placeholders with actual markdown
        # If placeholder exists, replace it. If not, append to end (fallback).
        for i, md_link in enumerate(uploaded_urls):
            placeholder = f"[[IMAGE_{i}]]"
            if placeholder in text:
                text = text.replace(placeholder, md_link)
            else:
                # Fallback: append to end if placeholder missing
                text += md_link
            
        return text
    except Exception as e:
        raise Exception(f"Failed to extract text from image with VLM+OpenCV: {e}")

def _extract_text_from_image(file_content: bytes, file_extension: str, 
                           ocr_service_url: str = None,
                           ocr_api_key: str = None,
                           ocr_model_name: str = None,
                           ocr_method: str = "rapiddoc",
                           **kwargs) -> str:
    """
    提取图片文本，支持多种 OCR 方法
    - rapiddoc: 使用 RapidDoc 方法（默认）
    - vlm_opencv: 使用 VLM+OpenCV 方法（备选）
    """
    if ocr_method == "vlm_opencv":
        return _extract_text_from_image_vlm_opencv(
            file_content, file_extension, 
            ocr_service_url, ocr_api_key, ocr_model_name, 
            **kwargs
        )
    else:
        # 显式获取 task_id，如果 kwargs 中有就取出来，没有就用 unknown
        task_id = kwargs.get("task_id", "unknown")
        
        # 构造一个新的 kwargs，排除 task_id 以避免重复传递
        rapiddoc_kwargs = {k: v for k, v in kwargs.items() if k != "task_id"}
        
        # 确保 ocr_method 正确传递给 _process_with_rapiddoc (实际上它不需要，但为了清晰)
        # RapidDoc 的处理函数不需要 ocr_method 参数
        
        return _process_with_rapiddoc(
            file_content, file_extension, 
            task_id=task_id,
            **rapiddoc_kwargs
        )

def _extract_text_from_pdf_vlm_opencv(file_content: bytes,
                           ocr_service_url: str,
                           ocr_api_key: str,
                           ocr_model_name: str,
                           max_workers: Optional[int] = None,
                           request_timeout: int = 1800,
                           use_ocr: bool = True,
                           image_format: str = 'jpg',
                           **kwargs) -> str:
    """
    使用 VLM+OpenCV 方法提取 PDF 文本（备选方案）
    """
    if not use_ocr or not ocr_api_key:
        try:
            pdf_file = io.BytesIO(file_content)
            pdf_document = pypdfium2.PdfDocument(pdf_file, autoclose=True)
            text = ""
            for page in pdf_document:
                text_page = page.get_textpage()
                text += text_page.get_text_bounded()
                text_page.close()
                page.close()
            return text
        except Exception as e:
            raise Exception(f"Failed to extract text from PDF: {e}")

    try:
        client = OpenAI(base_url=ocr_service_url, api_key=ocr_api_key, timeout=request_timeout)
        doc = fitz.open(stream=file_content, filetype="pdf")
        page_count = doc.page_count

        def ocr_page(page_num: int) -> Tuple[int, Optional[str], Optional[Exception]]:
            try:
                page = doc.load_page(page_num)
                pix = page.get_pixmap()

                if image_format.lower() == 'png':
                    image_bytes = pix.tobytes(output='png')
                    mime_type = 'image/png'
                else:
                    image_bytes = pix.tobytes(output='jpg')
                    mime_type = 'image/jpeg'

                image_base64 = base64.b64encode(image_bytes).decode("utf-8")
                
                # Call LLM
                result = _call_ocr_llm(client, ocr_model_name, image_base64, mime_type)
                content = result.get("content", "")
                
                # Handle multiple bboxes
                bboxes = result.get("diagram_bboxes", [])
                if "diagram_bbox" in result and result["diagram_bbox"]:
                    bboxes.append(result["diagram_bbox"])
                
                # Process diagrams
                uploaded_urls = []
                for bbox in bboxes:
                    diagram_md = _process_diagram_cropping(image_bytes, bbox, file_id=kwargs.get("task_id", "unknown"))
                    uploaded_urls.append(diagram_md)
                
                for i, md_link in enumerate(uploaded_urls):
                    placeholder = f"[[IMAGE_{i}]]"
                    if placeholder in content:
                        content = content.replace(placeholder, md_link)
                    else:
                        content += md_link
                        
                return page_num, content, None
            except Exception as e:
                return page_num, None, e

        # Use ThreadPoolExecutor for concurrent page processing
        results = [None] * page_count
        errors = []
        
        # Limit max_workers to avoid hitting API rate limits or OOM
        if max_workers is None:
            cpu_count = os.cpu_count() or 4
            max_workers = max(1, _get_int_env("OCR_MAX_WORKERS", min(8, cpu_count)))
        actual_workers = min(max_workers, page_count)
        
        with ThreadPoolExecutor(max_workers=actual_workers) as executor:
            future_to_page = {executor.submit(ocr_page, i): i for i in range(page_count)}
            for future in as_completed(future_to_page):
                page_num = future_to_page[future]
                try:
                    res_page_num, content, exc = future.result()
                    if exc is not None:
                        errors.append((res_page_num, exc))
                    results[res_page_num] = content
                except Exception as exc:
                    errors.append((page_num, exc))

        if errors and len(errors) == page_count:
            doc.close()
            try:
                pdf_file = io.BytesIO(file_content)
                pdf_document = pypdfium2.PdfDocument(pdf_file, autoclose=True)
                text = ""
                for page in pdf_document:
                    text_page = page.get_textpage()
                    text += text_page.get_text_bounded()
                    text_page.close()
                    page.close()
                return text
            except Exception as e:
                raise Exception(f"OCR failed and fallback extraction failed: {e}")

        doc.close()
        all_content = "\n\n".join([c for c in results if c is not None])
        return all_content

    except Exception as e:
        raise Exception(f"Failed to extract text from PDF with OCR: {e}")

def _extract_text_from_pdf(file_content: bytes,
                           ocr_service_url: str = None,
                           ocr_api_key: str = None,
                           ocr_model_name: str = None,
                           ocr_method: str = "rapiddoc",
                           **kwargs) -> str:
    """
    提取 PDF 文本，支持多种 OCR 方法
    """
    print(f"[OCR] Extracting text from PDF. Method: {ocr_method}")
    if ocr_method == "vlm_opencv":
        # VLM+OpenCV 需要这些参数
        if not ocr_service_url or not ocr_api_key or not ocr_model_name:
             # 如果缺少参数，尝试从 kwargs 获取或使用环境变量
             ocr_service_url = ocr_service_url or kwargs.get("ocr_service_url") or OCR_SERVICE_URL
             ocr_api_key = ocr_api_key or kwargs.get("ocr_api_key") or OCR_API_KEY
             ocr_model_name = ocr_model_name or kwargs.get("ocr_model_name") or OCR_MODEL_NAME
             
        text = _extract_text_from_pdf_vlm_opencv(
            file_content, 
            ocr_service_url=ocr_service_url, 
            ocr_api_key=ocr_api_key, 
            ocr_model_name=ocr_model_name,
            **kwargs
        )
        text += "\n\n<!-- Generated by VLM+OpenCV -->"
        return text
    else:
        # RapidDoc
        task_id = kwargs.get("task_id", "unknown")
        rapiddoc_kwargs = {k: v for k, v in kwargs.items() if k != "task_id"}
        
        return _process_with_rapiddoc(
            file_content, 
            file_extension=".pdf", 
            task_id=task_id, 
            **rapiddoc_kwargs
        )
