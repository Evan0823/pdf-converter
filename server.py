import os
import uuid
import subprocess
import zipfile
import threading
import hashlib
import json
import time
import fitz  # PyMuPDF
from pathlib import Path
from flask import Flask, request, jsonify, send_file, render_template

app = Flask(__name__)
app.config["MAX_CONTENT_LENGTH"] = 50 * 1024 * 1024  # 最大 50MB

BASE_DIR = Path(__file__).parent
UPLOAD_DIR = BASE_DIR / "uploads"
OUTPUT_DIR = BASE_DIR / "outputs"
CACHE_DIR = BASE_DIR / "cache"
UPLOAD_DIR.mkdir(exist_ok=True)
OUTPUT_DIR.mkdir(exist_ok=True)
CACHE_DIR.mkdir(exist_ok=True)

# pandoc 安装路径（Windows winget 安装位置）
PANDOC = "C:/Users/dell/AppData/Local/Microsoft/WinGet/Packages/JohnMacFarlane.Pandoc_Microsoft.Winget.Source_8wekyb3d8bbwe/pandoc-3.10/pandoc.exe"
if not os.path.exists(PANDOC):
    PANDOC = "pandoc"  # fallback to PATH

# ── 全局状态 ──────────────────────────────────────────
sessions = {}       # { sid: { pdf_name, md_text, md_path, ... } }
progress_store = {}  # { sid: { stage, detail, done, error } }

# ── 懒加载 Docling converter ──────────────────────────
_converter = None
_fast_converter = None

def get_converter(fast_mode=True):
    """fast_mode=True：跳过 OCR 和表格识别，速度提升 3-5 倍"""
    global _converter, _fast_converter
    from docling.document_converter import DocumentConverter, PdfFormatOption
    from docling.datamodel.pipeline_options import PdfPipelineOptions
    from docling.datamodel.base_models import InputFormat

    if fast_mode:
        if _fast_converter is None:
            pipeline_options = PdfPipelineOptions()
            pipeline_options.do_ocr = False
            pipeline_options.do_table_structure = False
            _fast_converter = DocumentConverter(
                format_options={InputFormat.PDF: PdfFormatOption(pipeline_options=pipeline_options)}
            )
            print("[init] Docling fast converter 已加载")
        return _fast_converter
    else:
        if _converter is None:
            pipeline_options = PdfPipelineOptions()
            pipeline_options.do_ocr = True
            pipeline_options.do_table_structure = True
            _converter = DocumentConverter(
                format_options={InputFormat.PDF: PdfFormatOption(pipeline_options=pipeline_options)}
            )
            print("[init] Docling full converter 已加载 (OCR + 表格)")
        return _converter


def _is_scanned_pdf(pdf_path: str) -> bool:
    """检测 PDF 是否为扫描件（图片型，非文字型）"""
    try:
        doc = fitz.open(pdf_path)
        total_text = 0
        for page in doc:
            total_text += len(page.get_text().strip())
        doc.close()
        # 平均每页少于 50 个可提取字符 → 扫描件
        return total_text < 50
    except Exception:
        return False


# ════════════════════════════════════════════════════════
# 1. 上传 PDF → 解析为 Markdown
# ════════════════════════════════════════════════════════
@app.route("/upload", methods=["POST"])
def upload():
    file = request.files.get("pdf")
    if not file or not file.filename:
        return jsonify(error="请选择 PDF 文件"), 400

    sid = str(uuid.uuid4())[:8]
    pdf_name = file.filename

    # 保存 PDF 并计算哈希
    pdf_path = UPLOAD_DIR / f"{sid}.pdf"
    file.save(str(pdf_path))

    file_hash = _sha256(str(pdf_path))

    # 查缓存（磁盘持久化，不依赖内存）
    cached = _cache_lookup(file_hash)
    if cached:
        print(f"[cache] 命中 → {pdf_name}")
        progress_store[sid] = {"stage": "done", "detail": "转换完成！（缓存命中，秒出）", "done": True}
        # 从磁盘缓存恢复完整结果
        sessions[sid] = {
            "pdf_name": pdf_name,
            "pdf_path": str(pdf_path),
            "md_text": cached["md_text"],
            "md_path": cached["md_path"],
            "img_count": cached.get("img_count", 0),
            "img_dir": cached.get("img_dir", ""),
            "file_hash": file_hash,
        }
        return jsonify({"ok": True, "sid": sid, "filename": pdf_name, "cached": True})

    # 未命中：初始化进度 + 后台转换
    progress_store[sid] = {"stage": "start", "detail": "准备中...", "done": False, "error": None}
    thread = threading.Thread(target=_convert_worker, args=(sid, pdf_name, str(pdf_path), file_hash))
    thread.start()

    return jsonify({"ok": True, "sid": sid, "filename": pdf_name, "cached": False})


def _convert_worker(sid, pdf_name, pdf_path, file_hash=""):
    """后台转换线程：更新 progress_store 每个阶段"""
    try:
        # 检测 PDF 类型
        is_scanned = _is_scanned_pdf(pdf_path)
        mode_label = "完整模式（OCR + 表格识别）" if is_scanned else "快速模式（文字型 PDF）"
        print(f"[convert] {pdf_name}: {mode_label}")

        # 阶段 1：加载模型
        progress_store[sid] = {"stage": "loading", "detail": f"正在加载 AI 解析模型（{mode_label}）...", "done": False}
        converter = get_converter(fast_mode=not is_scanned)

        # 阶段 2：解析 PDF（可能很慢，启动心跳更新已用时间）
        page_count = _count_pdf_pages(pdf_path)
        progress_store[sid] = {
            "stage": "parsing",
            "detail": f"正在解析文档（共 {page_count} 页，OCR 模式，请耐心等待）...",
            "pageCount": page_count,
            "done": False,
        }
        # 心跳线程：每秒更新已用时间，让前端看到没卡死
        def heartbeat():
            start = time.time()
            while not progress_store.get(sid, {}).get("done", True):
                elapsed = int(time.time() - start)
                p = progress_store.get(sid)
                if p and p.get("stage") == "parsing" and not p.get("done"):
                    p["detail"] = f"正在解析文档（共 {page_count} 页，已用 {elapsed} 秒）..."
                time.sleep(1)
        hb = threading.Thread(target=heartbeat, daemon=True)
        hb.start()
        result = converter.convert(pdf_path)

        # 阶段 3：生成 Markdown
        progress_store[sid] = {"stage": "markdown", "detail": "正在生成 Markdown...", "done": False}
        md_text = result.document.export_to_markdown()

        # 阶段 4：提取图片（仅文字型 PDF，扫描件全是页面碎片，跳过）
        if is_scanned:
            img_count = 0
            img_dir = ""
        else:
            progress_store[sid] = {"stage": "images", "detail": "正在提取文档中的图片...", "done": False}
            img_dir = OUTPUT_DIR / f"{sid}_images"
            img_dir.mkdir(exist_ok=True)
            img_count = _extract_images(pdf_path, str(img_dir))
            if img_count > 0:
                md_text += f"\n\n---\n\n## 文档中的图片（共 {img_count} 张）\n\n"
                for i in range(1, img_count + 1):
                    md_text += f"![图片 {i}](./{sid}_images/img_{i:03d}.png)\n\n"

        # 保存 Markdown
        md_path = OUTPUT_DIR / f"{sid}.md"
        md_path.write_text(md_text, encoding="utf-8")

        sessions[sid] = {
            "pdf_name": pdf_name,
            "pdf_path": pdf_path,
            "md_text": md_text,
            "md_path": str(md_path),
            "img_count": img_count,
            "img_dir": str(img_dir),
            "file_hash": file_hash,
        }

        # 写入磁盘缓存（完整结果 + 文件路径）
        if file_hash:
            _cache_write(file_hash, {
                "sid": sid,
                "md_text": md_text,
                "md_path": str(md_path),
                "img_count": img_count,
                "img_dir": str(img_dir),
                "pdf_name": pdf_name,
            })

        # 清理旧会话
        if len(sessions) > 10:
            oldest = list(sessions.keys())[0]
            _cleanup(oldest)

        progress_store[sid] = {"stage": "done", "detail": "转换完成！", "done": True}
        print(f"[convert] 完成: {pdf_name} → {len(md_text)} 字符, {img_count} 张图")

    except Exception as e:
        import traceback
        err_detail = traceback.format_exc()
        print(f"[错误] {e}\n{err_detail}")
        # 写错误日志文件方便排查
        error_log = BASE_DIR / "error.log"
        error_log.write_text(f"{pdf_name}\n{err_detail}", encoding="utf-8")
        progress_store[sid] = {"stage": "error", "detail": str(e), "done": True, "error": str(e)}
        if os.path.exists(pdf_path):
            os.remove(pdf_path)


# ════════════════════════════════════════════════════════
# 1b. 轮询进度
# ════════════════════════════════════════════════════════
@app.route("/progress/<sid>")
def get_progress(sid):
    p = progress_store.get(sid)
    if not p:
        return jsonify(error="无效会话"), 404
    return jsonify(p)


# ════════════════════════════════════════════════════════
# 1c. 获取转换结果（前端轮询直到 done 后调用）
# ════════════════════════════════════════════════════════
@app.route("/result/<sid>")
def get_result(sid):
    session = sessions.get(sid)
    if not session:
        return jsonify(error="转换结果不存在，请重新上传"), 404
    return jsonify({
        "ok": True,
        "sid": sid,
        "filename": session["pdf_name"],
        "mdPreview": session["md_text"][:3000],
        "mdLength": len(session["md_text"]),
        "imgCount": session.get("img_count", 0),
    })


# ════════════════════════════════════════════════════════
# 2. 下载 Markdown
# ════════════════════════════════════════════════════════
@app.route("/download/<sid>/<fmt>")
def download(sid, fmt):
    if sid not in sessions:
        return jsonify(error="会话已过期，请重新上传"), 404

    session = sessions[sid]
    md_path = Path(session["md_path"])
    base_name = Path(session["pdf_name"]).stem

    # Markdown: 直接返回
    if fmt == "md":
        return send_file(
            md_path,
            as_attachment=True,
            download_name=f"{base_name}.md",
            mimetype="text/markdown",
        )

    # 其他格式：用 pandoc 转换
    output_path = OUTPUT_DIR / f"{sid}.{fmt}"
    _pandoc_convert(str(md_path), str(output_path), fmt)

    mime_map = {
        "docx": "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
        "pptx": "application/vnd.openxmlformats-officedocument.presentationml.presentation",
        "html": "text/html",
    }

    return send_file(
        output_path,
        as_attachment=True,
        download_name=f"{base_name}.{fmt}",
        mimetype=mime_map.get(fmt, "application/octet-stream"),
    )


# ════════════════════════════════════════════════════════
# 2b. 下载 Markdown + 图片打包 ZIP
# ════════════════════════════════════════════════════════
@app.route("/download-zip/<sid>")
def download_zip(sid):
    if sid not in sessions:
        return jsonify(error="会话已过期，请重新上传"), 404

    session = sessions[sid]
    base_name = Path(session["pdf_name"]).stem
    zip_path = OUTPUT_DIR / f"{sid}_bundle.zip"

    with zipfile.ZipFile(str(zip_path), "w", zipfile.ZIP_DEFLATED) as zf:
        # Markdown 文件
        zf.write(session["md_path"], f"{base_name}.md")
        # 图片（如果有）
        if session.get("img_count", 0) > 0:
            img_dir = session["img_dir"]
            for f in sorted(Path(img_dir).glob("*.png")):
                zf.write(str(f), f"{base_name}_images/{f.name}")

    return send_file(
        zip_path,
        as_attachment=True,
        download_name=f"{base_name}.zip",
        mimetype="application/zip",
    )


# ════════════════════════════════════════════════════════
# 3. 查看 Markdown 全文
# ════════════════════════════════════════════════════════
@app.route("/preview/<sid>")
def preview(sid):
    if sid not in sessions:
        return jsonify(error="会话已过期"), 404
    return jsonify({
        "mdText": sessions[sid]["md_text"],
        "filename": sessions[sid]["pdf_name"],
    })


# ════════════════════════════════════════════════════════
# 首页
# ════════════════════════════════════════════════════════
@app.route("/")
def index():
    return render_template("index.html")


# ════════════════════════════════════════════════════════
# 工具函数
# ════════════════════════════════════════════════════════
def _pandoc_convert(src: str, dst: str, fmt: str):
    """调用 pandoc 将 MD 转为目标格式"""
    if fmt == "docx":
        subprocess.run(
            [PANDOC, src, "-o", dst, "--from=markdown", "--to=docx"],
            check=True, capture_output=True, text=True,
        )
    elif fmt == "pptx":
        subprocess.run(
            [PANDOC, src, "-o", dst, "--from=markdown", "--to=pptx"],
            check=True, capture_output=True, text=True,
        )
    elif fmt == "html":
        subprocess.run(
            [PANDOC, src, "-o", dst, "--from=markdown", "--to=html5",
             "--standalone", "--self-contained"],
            check=True, capture_output=True, text=True,
        )
    else:
        raise ValueError(f"不支持的格式: {fmt}")


def _count_pdf_pages(pdf_path: str) -> int:
    """快速获取 PDF 页数"""
    try:
        doc = fitz.open(pdf_path)
        count = len(doc)
        doc.close()
        return count
    except Exception:
        return 1  # fallback


def _extract_images(pdf_path: str, output_dir: str) -> int:
    """从 PDF 提取嵌入图片，保存为 PNG。返回图片数量"""
    count = 0
    try:
        doc = fitz.open(pdf_path)
        for page_num in range(len(doc)):
            page = doc[page_num]
            image_list = page.get_images(full=True)
            for img_index, img_info in enumerate(image_list):
                xref = img_info[0]
                base_image = doc.extract_image(xref)
                image_bytes = base_image["image"]
                # 保存为 PNG
                out_path = os.path.join(output_dir, f"img_{count + 1:03d}.png")
                with open(out_path, "wb") as f:
                    f.write(image_bytes)
                count += 1
        doc.close()
    except Exception as e:
        print(f"[image] 图片提取异常: {e}")
    return count


CACHE_MAX_AGE = 7 * 24 * 3600  # 7 天过期
CACHE_MAX_COUNT = 50           # 最多保留 50 条缓存


def _sha256(filepath: str) -> str:
    """计算文件 SHA256"""
    h = hashlib.sha256()
    with open(filepath, "rb") as f:
        for chunk in iter(lambda: f.read(8192), b""):
            h.update(chunk)
    return h.hexdigest()


def _cache_lookup(file_hash: str):
    """查找缓存：返回完整的缓存数据（含 md_text、img_dir），或 None。不依赖内存 sessions"""
    cache_file = CACHE_DIR / f"{file_hash}.json"
    if cache_file.exists():
        try:
            data = json.loads(cache_file.read_text(encoding="utf-8"))
            # 检查过期
            if time.time() - data.get("time", 0) > CACHE_MAX_AGE:
                _cache_delete(file_hash)
                return None
            # 刷新访问时间（续期）
            data["time"] = time.time()
            cache_file.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
            return data
        except Exception:
            return None
    return None


def _cache_write(file_hash: str, cache_data: dict):
    """写缓存：存完整数据到磁盘"""
    cache_data["time"] = time.time()
    cache_file = CACHE_DIR / f"{file_hash}.json"
    cache_file.write_text(json.dumps(cache_data, ensure_ascii=False), encoding="utf-8")
    # 超过数量限制就清理最老的
    _cache_prune()


def _cache_delete(file_hash: str):
    """删除单个缓存及其关联文件"""
    cache_file = CACHE_DIR / f"{file_hash}.json"
    if cache_file.exists():
        try:
            data = json.loads(cache_file.read_text(encoding="utf-8"))
            # 清理关联的 markdown 和图片
            sid = data.get("sid", "")
            for f in OUTPUT_DIR.glob(f"{sid}*"):
                try:
                    os.remove(str(f))
                except Exception:
                    pass
        except Exception:
            pass
    try:
        os.remove(str(cache_file))
    except Exception:
        pass


def _cache_prune():
    """如果缓存超过数量上限，删除最旧的"""
    cache_files = sorted(CACHE_DIR.glob("*.json"), key=lambda f: f.stat().st_mtime)
    while len(cache_files) > CACHE_MAX_COUNT:
        oldest = cache_files.pop(0)
        file_hash = oldest.stem  # 文件名去掉 .json
        _cache_delete(file_hash)

    # 同时清理超过 7 天的旧缓存
    now = time.time()
    for f in CACHE_DIR.glob("*.json"):
        if now - f.stat().st_mtime > CACHE_MAX_AGE:
            _cache_delete(f.stem)


def _cleanup(sid: str):
    """清理会话文件及图片目录"""
    if sid in sessions:
        del sessions[sid]
    # 上传的 PDF / 输出的 MD / bundle ZIP
    for d in [UPLOAD_DIR, OUTPUT_DIR]:
        for f in d.glob(f"{sid}*"):
            try:
                if f.is_dir():
                    import shutil
                    shutil.rmtree(str(f))
                else:
                    os.remove(str(f))
            except Exception:
                pass


# ════════════════════════════════════════════════════════
# 启动
# ════════════════════════════════════════════════════════
if __name__ == "__main__":
    print("=" * 50)
    print("PDF 格式转换器 - http://localhost:5000")
    print("  PDF → Markdown → Word / PPT / HTML")
    print("=" * 50)
    app.run(host="0.0.0.0", port=5000, debug=False)
