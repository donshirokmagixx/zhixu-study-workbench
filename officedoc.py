"""Word / WPS COM 转 PDF，以及转换引擎自检。

固定顺序见规格第 13 节：优先 Word COM（ExportAsFixedFormat 失败再 SaveAs FileFormat=17），
Dispatch 失败/转换失败再试 WPS COM；两者都失败抛 PdfEngineUnavailable。

本机实测 Word 自动化在部分环境会一直挂起（Dispatch/Open 不返回），因此每次 COM 转换都在
独立子进程中执行并带超时；超时/失败后只清理本次尝试新出现的 Word/WPS 进程，再回退下一个
引擎，避免把请求线程或设置页卡死。转换失败必须关闭文档与进程，不得遗留锁文件。
"""
from __future__ import annotations

import os
import shutil
import subprocess
import sys
import tempfile
import threading
import time
import uuid
from functools import lru_cache
from pathlib import Path

import config
from llm import AppError

WORD_PROGID = "Word.Application"
WPS_PROGID = "KWps.Application"
WORD_TIMEOUT_SECONDS = 45.0
WPS_TIMEOUT_SECONDS = 90.0

_FIXED_MESSAGE = "PDF 转换不可用：请确认已安装 Microsoft Word 或 WPS，并在设置中运行转换自检。"

_SLIDE_LOCK = threading.Lock()
_SLIDE_SCRIPT = r"""
$ErrorActionPreference = 'Stop'
$application = $null
$presentation = $null
$security = $null
try {
    $application = New-Object -ComObject PowerPoint.Application
    $security = $application.AutomationSecurity
    $application.AutomationSecurity = 3
    $presentation = $application.Presentations.Open($env:ZHIXU_SLIDE_SOURCE, -1, 0, 0)
    $presentation.Slides.Item([int]$env:ZHIXU_SLIDE_NUMBER).Export($env:ZHIXU_SLIDE_OUTPUT, 'PNG', 1440)
} finally {
    try { if ($presentation) { $presentation.Close() } }
    finally {
        if ($application) {
            if ($null -ne $security) { $application.AutomationSecurity = $security }
            [void][Runtime.InteropServices.Marshal]::ReleaseComObject($application)
        }
    }
}
"""


@lru_cache(maxsize=24)
def _render_pptx_slide(source: Path, slide_number: int, modified_ns: int) -> bytes:
    with tempfile.TemporaryDirectory(prefix="slide-", dir=config.tmp_dir()) as directory:
        copy = Path(directory) / "presentation.pptx"
        target = Path(directory) / "slide.png"
        shutil.copyfile(source, copy)
        env = dict(os.environ, ZHIXU_SLIDE_SOURCE=str(copy), ZHIXU_SLIDE_OUTPUT=str(target),
                   ZHIXU_SLIDE_NUMBER=str(slide_number))
        result = subprocess.run(
            ["powershell.exe", "-NoProfile", "-NonInteractive", "-Command", _SLIDE_SCRIPT],
            env=env, capture_output=True, timeout=15, creationflags=subprocess.CREATE_NO_WINDOW,
        )
        if result.returncode != 0 or not target.is_file():
            raise OSError("PowerPoint did not export the requested slide")
        return target.read_bytes()


def pptx_slide_preview(source: Path, slide_number: int) -> bytes:
    if os.name == "nt":
        try:
            with _SLIDE_LOCK:
                return _render_pptx_slide(source, slide_number, source.stat().st_mtime_ns)
        except (OSError, subprocess.TimeoutExpired):
            pass
    raise AppError("page_preview_unavailable", "无法预览幻灯片。请用 PowerPoint 打开原文件查看。")

# 子进程脚本：只做一次 COM 转换，便于父进程按超时终止（避免 Word 挂起拖死调用线程）。
_CHILD_SCRIPT = r"""
import os
import sys

progid, src, out = sys.argv[1], sys.argv[2], sys.argv[3]
try:
    import pythoncom
    import win32com.client as win32
except Exception as exc:
    print("COM_IMPORT_FAILED %s: %s" % (type(exc).__name__, exc), file=sys.stderr)
    sys.exit(11)
pythoncom.CoInitialize()
app = None
doc = None
succeeded = False
error_text = ""
try:
    app = win32.Dispatch(progid)
    try:
        app.Visible = False
    except Exception:
        pass
    try:
        app.DisplayAlerts = 0
    except Exception:
        pass
    doc = app.Documents.Open(src, ReadOnly=True)
    try:
        doc.ExportAsFixedFormat(out, 17)
    except Exception:
        doc.SaveAs(out, FileFormat=17)
    if os.path.isfile(out) and os.path.getsize(out) > 0:
        succeeded = True
    else:
        error_text = "COM_EMPTY_OUTPUT %s" % progid
except BaseException as exc:
    error_text = "COM_ERROR %s: %s" % (type(exc).__name__, exc)
finally:
    if doc is not None:
        try:
            doc.Close(SaveChanges=0)
        except Exception:
            pass
    if app is not None:
        try:
            app.Quit()
        except Exception:
            pass
    try:
        pythoncom.CoUninitialize()
    except Exception:
        pass
if error_text:
    print(error_text, file=sys.stderr)
sys.exit(0 if succeeded else 13)
"""

# 进程内缓存：本进程已确认 Word 自动化不可用时，后续转换直接走 WPS，避免每次等待超时。
_WORD_FAILED_REASON: str | None = None
# COM 是单例自动化对象，同一进程的并发转换串行执行，避免互相抢占引擎。
_CONVERT_LOCK = threading.Lock()


class PdfEngineUnavailable(AppError):
    code = "pdf_engine_unavailable"


# ---------------------------------------------------------------------------
# 进程清理（只结束本次 COM 尝试新产生的进程，绝不误杀用户已打开的 Word/WPS）
# ---------------------------------------------------------------------------

def _process_pids(exe_names: set[str]) -> set[int]:
    try:
        import win32api
        import win32con
        import win32process
    except Exception:  # noqa: BLE001
        return set()
    pids: set[int] = set()
    for pid in win32process.EnumProcesses():
        handle = None
        try:
            handle = win32api.OpenProcess(
                win32con.PROCESS_QUERY_INFORMATION | win32con.PROCESS_VM_READ, False, pid
            )
            exe = win32process.GetModuleFileNameEx(handle, 0)
            if Path(exe).name.lower() in exe_names:
                pids.add(pid)
        except Exception:  # noqa: BLE001 - 无权限/已退出进程跳过
            continue
        finally:
            if handle is not None:
                try:
                    win32api.CloseHandle(handle)
                except Exception:  # noqa: BLE001
                    pass
    return pids


def _terminate_new_processes(exe_names: set[str], before: set[int]) -> None:
    try:
        import win32api
        import win32con
    except Exception:  # noqa: BLE001
        return
    for pid in _process_pids(exe_names) - before:
        handle = None
        try:
            handle = win32api.OpenProcess(win32con.PROCESS_TERMINATE, False, pid)
            win32api.TerminateProcess(handle, 1)
            time.sleep(0.2)
        except Exception:  # noqa: BLE001
            continue
        finally:
            if handle is not None:
                try:
                    win32api.CloseHandle(handle)
                except Exception:  # noqa: BLE001
                    pass


# ---------------------------------------------------------------------------
# 单引擎转换（子进程 + 超时）
# ---------------------------------------------------------------------------

def _cleanup_new_processes(exe_names: set[str], before: set[int]) -> None:
    """清理本次尝试新出现的进程；Word 被终止后仍可能有延迟启动的子进程，重试几轮。"""
    for _ in range(3):
        new_pids = _process_pids(exe_names) - before
        if not new_pids:
            return
        _terminate_new_processes(exe_names, before)
        time.sleep(0.5)


def _run_com_child(progid: str, docx_path: str, pdf_path: str, timeout: float,
                   exe_names: set[str]) -> tuple[bool, str]:
    """在子进程中执行一次 COM 转换；返回 (是否成功, 错误摘要)。超时/失败会清理新进程。"""
    try:
        if pdf_path.exists():
            pdf_path.unlink()
    except OSError:
        pass
    before = _process_pids(exe_names)
    log_path = config.tmp_dir() / f"com_convert_{uuid.uuid4().hex[:10]}.log"
    log_handle = None
    try:
        config.ensure_dirs()
        log_handle = log_path.open("w", encoding="utf-8")
        env = dict(os.environ)
        env["PYTHONIOENCODING"] = "utf-8"
        try:
            completed = subprocess.run(
                [sys.executable, "-c", _CHILD_SCRIPT, progid, str(docx_path), str(pdf_path)],
                stdout=log_handle,
                stderr=subprocess.STDOUT,
                timeout=timeout,
                env=env,
            )
            returncode = completed.returncode
            timed_out = False
        except subprocess.TimeoutExpired:
            returncode = -1
            timed_out = True
        except Exception as exc:  # noqa: BLE001
            return False, f"启动 COM 转换子进程失败：{exc.__class__.__name__}: {exc}"
    finally:
        if log_handle is not None:
            try:
                log_handle.close()
            except OSError:
                pass

    success = (not timed_out) and returncode == 0 and pdf_path.is_file() and pdf_path.stat().st_size > 0
    if not success:
        # 只清理失败尝试新启动的进程；成功时由子进程自行 Quit，避免误杀用户新开的 Word/WPS
        _cleanup_new_processes(exe_names, before)
    detail = ""
    try:
        detail = log_path.read_text(encoding="utf-8", errors="replace").strip()[:600]
    except OSError:
        detail = ""
    try:
        log_path.unlink()
    except OSError:
        pass
    if timed_out:
        return False, f"{progid} 转换超时（>{timeout:g}s），已终止并清理本次启动的进程。"
    if returncode != 0:
        return False, f"{progid} 转换失败（exit={returncode}）：{detail or '无输出'}"
    if not success:
        return False, f"{progid} 未生成有效 PDF。"
    return True, ""


# ---------------------------------------------------------------------------
# 对外接口
# ---------------------------------------------------------------------------

def docx_to_pdf(docx_path: str, pdf_path: str) -> str:
    """把 DOCX 转 PDF，返回实际使用的引擎 "word" | "wps"；都失败抛 PdfEngineUnavailable。"""
    if not sys.platform.startswith("win"):
        raise PdfEngineUnavailable("pdf_engine_unavailable", _FIXED_MESSAGE)
    src = Path(docx_path).expanduser().resolve()
    out = Path(pdf_path).expanduser().resolve()
    if not src.is_file():
        raise AppError("invalid_request", "待转换的 DOCX 文件不存在。")
    out.parent.mkdir(parents=True, exist_ok=True)
    with _CONVERT_LOCK:
        return _convert_with_fallback(src, out)


def _convert_with_fallback(src: Path, out: Path) -> str:
    global _WORD_FAILED_REASON
    errors: list[str] = []
    if _WORD_FAILED_REASON is None:
        ok, detail = _run_com_child(WORD_PROGID, src, out, WORD_TIMEOUT_SECONDS, {"winword.exe"})
        if ok:
            return "word"
        _WORD_FAILED_REASON = detail
        errors.append(f"word: {detail}")
    else:
        errors.append(f"word: 本进程先前已判定不可用（{_WORD_FAILED_REASON}）")

    ok, detail = _run_com_child(WPS_PROGID, src, out, WPS_TIMEOUT_SECONDS, {"wps.exe"})
    if ok:
        return "wps"
    errors.append(f"wps: {detail}")
    detail_text = " | ".join(errors)[:800]
    raise PdfEngineUnavailable("pdf_engine_unavailable", _FIXED_MESSAGE + f"（{detail_text}）")


def selftest() -> dict:
    """生成临时 DOCX → 转 PDF → 清理；返回 {"ok","engine","message"}，不抛异常。"""
    config.ensure_dirs()
    token = uuid.uuid4().hex[:12]
    docx_path = config.tmp_dir() / f"pdf_selftest_{token}.docx"
    pdf_path = config.tmp_dir() / f"pdf_selftest_{token}.pdf"
    try:
        try:
            from docx import Document

            document = Document()
            document.add_paragraph("PDF 转换自检：这是一行测试文字。")
            document.save(str(docx_path))
        except Exception as exc:  # noqa: BLE001
            return {"ok": False, "engine": "none", "message": f"生成自检 DOCX 失败：{exc}"}
        try:
            engine = docx_to_pdf(str(docx_path), str(pdf_path))
        except PdfEngineUnavailable as exc:
            return {"ok": False, "engine": "none", "message": exc.message}
        except Exception as exc:  # noqa: BLE001
            return {"ok": False, "engine": "none", "message": f"PDF 转换自检失败：{exc}"}
        if not pdf_path.is_file() or pdf_path.stat().st_size == 0:
            return {"ok": False, "engine": "none", "message": "PDF 转换自检失败：未生成有效 PDF 文件。"}
        return {"ok": True, "engine": engine, "message": f"PDF 转换自检通过（引擎：{engine}）。"}
    finally:
        for path in (docx_path, pdf_path):
            try:
                if path.exists():
                    path.unlink()
            except OSError:
                pass
