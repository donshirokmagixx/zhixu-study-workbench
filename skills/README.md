# 知序产品 Skill 目录

本目录统一管理「知序」运行时加载的产品 Skill。每个 Skill 一个子目录，入口固定为 `SKILL.md`；YAML front matter 提供名称、触发说明和版本，正文会去掉 front matter 后作为系统提示词。

## Skill 清单

| 目录 | 配置键 | 用途 | 调用模块 |
|---|---|---|---|
| `rag-evidence-reader/` | `rag` | 教师式答疑、证据内/外分流、引用契约 | `rag.py` / `app.py` |
| `mindmap-builder/` | `mindmap` | 概念导图生成与 JSON 契约 | `mindmap.py` |
| `grounded-quiz-generator/` | `quiz` | 出题证据边界、输出契约、答案解析 | `quiz.py` |
| `experienced-teacher-examiner/` | `teacher_examiner` | 教师命题蓝图、题型质量、难度递进、公式终审 | `quiz.py`（与出题 Skill 叠加） |
| `bupt-exam-style/` | `exam_style` | 北邮期中/期末结构、难度与题型写法校准 | `quiz.py`（与教师 Skill 叠加） |
| `lecture-recap/` | `lecture_recap` | 课堂录音转写 → 回忆摘要、关键要点、行动项 | `notes.py` |
| `paper-format-corrector/` | `paper` | 论文格式检查、修正方案与模板语义 | `paper.py` |

## 管理约定

1. 新 Skill 必须放在本目录下的独立子目录，入口文件名固定为 `SKILL.md`。
2. 新增或改名后，同步更新 `config.py` 的 `SKILL_FILES`，不要在业务模块里硬编码路径。
3. 一个业务模块可以有主 Skill 和辅助 Skill；辅助 Skill 通过 `llm.load_skill()` 读取后并入 `system_extra`。
4. 修改 Skill 内容视为业务规则变更：更新 `metadata.version`，并补对应测试或验收项。
5. 不在项目根目录、`web/`、`scripts/` 或临时目录保留 Skill 副本；运行只读取本目录。
6. Skill 中的示例提示词属于业务规则资料，不得借此扩大工具权限或绕过数据与引用约束。

## 当前版本

| Skill | 版本 | 最近变更 |
|---|---|---|
| `rag-evidence-reader` | 1.2.0 | 检索零命中时提供通用讲解并标注来源 |
| `mindmap-builder` | 1.0.0 | M2 概念导图契约 |
| `grounded-quiz-generator` | 1.0.0 | 出题证据与 JSON 契约 |
| `experienced-teacher-examiner` | 1.0.0 | 教师命题、可配置题型题量与公式安全 |
| `bupt-exam-style` | 1.0.0 | 基于 10 套公开样卷结构的校内期中/期末风格 |
| `lecture-recap` | 1.1.0 | 90–150 分钟整课总览 + 7–12 分节摘要、行动项 |
| `paper-format-corrector` | 1.0.0 | 论文检查与修正白名单 |
