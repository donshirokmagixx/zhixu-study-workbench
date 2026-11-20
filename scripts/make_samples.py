"""生成演示用样例资料（不写入数据库、不进入正式界面）。

输出到 data/samples/：可直接在网页“上传资料”里逐个上传，用于演示/验收六种格式。

用法：
    .venv\\Scripts\\python.exe scripts\\make_samples.py
"""
from __future__ import annotations

import io
import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

import config  # noqa: E402

OUT = config.DATA_DIR / "samples"


def write_text() -> None:
    (OUT / "信号与系统-课堂笔记.md").write_text(
        "# 第一章 信号与系统\n\n"
        "## 1.1 信号的分类\n\n"
        "连续信号在某一时间区间内都有定义；离散信号只在离散时刻有定义。\n"
        "周期信号满足 f(t+T)=f(t)，其中 T 为周期。\n\n"
        "## 1.2 线性时不变系统\n\n"
        "线性系统满足叠加性与齐次性；时不变系统的参数不随时间变化。\n"
        "线性时不变系统的输出等于输入与系统冲激响应的卷积。\n",
        encoding="utf-8",
    )
    (OUT / "数电-复习提纲.txt").write_text(
        "第一章 逻辑代数基础\n"
        "1.1 基本逻辑运算：与、或、非。\n"
        "1.2 布尔代数基本定律：交换律、结合律、分配律、反演律。\n"
        "第二章 组合逻辑电路\n"
        "2.1 编码器与译码器\n"
        "2.2 数据选择器\n"
        "第三章 时序逻辑电路\n"
        "3.1 触发器：RS、JK、D、T。\n"
        "3.2 寄存器与计数器。\n",
        encoding="utf-8",
    )


def write_docx() -> None:
    from docx import Document

    doc = Document()
    doc.add_heading("大学物理 · 热力学第二定律", level=1)
    doc.add_heading("5.1 熵与熵增原理", level=2)
    doc.add_paragraph("熵是系统无序度的度量。孤立系统的熵不会减少，这就是熵增原理。")
    doc.add_heading("5.2 热力学第二定律的两种表述", level=2)
    doc.add_paragraph("克劳修斯表述：热量不能自发地从低温物体传向高温物体。")
    doc.add_paragraph("开尔文表述：不可能从单一热源吸热使之完全变为有用功而不产生其他影响。")
    doc.save(str(OUT / "大学物理-热力学.docx"))


def write_pptx() -> None:
    from pptx import Presentation

    prs = Presentation()
    slide = prs.slides.add_slide(prs.slide_layouts[1])
    slide.shapes.title.text = "数据结构 · 树与二叉树"
    slide.placeholders[1].text = "树的定义与基本术语\n二叉树的遍历方式\n二叉搜索树"
    slide2 = prs.slides.add_slide(prs.slide_layouts[1])
    slide2.shapes.title.text = "二叉树的遍历"
    slide2.placeholders[1].text = "前序：根—左—右\n中序：左—根—右\n后序：左—右—根"
    prs.save(str(OUT / "数据结构-二叉树.pptx"))


def write_scanned_pdf() -> None:
    """生成一页"扫描件风格"PDF（文字→图片→PDF），用于验证 OCR 通路。"""
    import fitz

    text = "计算方法 第一章 误差分析。绝对误差等于近似值减真实值。相对误差等于绝对误差除以真实值。"
    page = fitz.open()
    p = page.new_page(width=595, height=842)
    p.insert_text((50, 100), text, fontsize=14, fontname="china-s")
    pix = p.get_pixmap(dpi=150)
    img_bytes = pix.tobytes("png")
    page.close()

    scanned = fitz.open()
    sp = scanned.new_page(width=595, height=842)
    sp.insert_image(fitz.Rect(0, 0, 595, 842), stream=img_bytes)
    scanned.save(str(OUT / "计算方法-扫描件.pdf"))
    scanned.close()

    # 普通文本层 PDF（非扫描件）
    normal = fitz.open()
    np = normal.new_page(width=595, height=842)
    np.insert_text((50, 100), "线性代数 第一章 行列式。二阶行列式等于主对角线乘积减副对角线乘积。", fontsize=14, fontname="china-s")
    normal.save(str(OUT / "线性代数-文本层.pdf"))
    normal.close()


def main() -> int:
    config.ensure_dirs()
    OUT.mkdir(parents=True, exist_ok=True)
    write_text()
    write_docx()
    write_pptx()
    write_scanned_pdf()
    files = sorted(OUT.iterdir())
    print(f"已生成 {len(files)} 份样例资料：{OUT}")
    for f in files:
        print(f"  - {f.name}  ({f.stat().st_size} bytes)")
    print("用法：网页 → 知识库 → 选择知识区 → 上传资料，逐个上传上面文件即可。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
