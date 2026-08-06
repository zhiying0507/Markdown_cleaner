# Technical Markdown Cleaner

面向 MinerU 等文档解析流程的技术 Markdown 保真清洗工具。它从带有版面噪声、HTML 表格、图片引用和异常代码围栏的 Markdown 中提取更适合检索、知识库构建与大模型处理的高价值文本，同时保留技术正文、数值、单位、公式、代码和可追溯的结构化信息。

项目采用保守、可审计的清洗策略：不覆盖源文件，所有输出均以原子方式写入，并为表格、图片和质量检查生成独立报告。核心程序仅使用 Python 标准库，适合在大规模技术文档语料上部署。

## 主要功能

- 删除可确认的目录、索引、图片引用及相邻图注，降低版面噪声。
- 将 HTML 表格转换为 Markdown，支持 `rowspan` / `colspan` 展开并保留重复键值。
- 隔离疑似解析损坏、超长或结构异常的表格，避免静默污染正文。
- 修复章节编号层级、合并 `Chapter N` 与章节标题、提升未标记的编号标题。
- 保护代码内容，并处理被 MinerU 错误扩张的代码围栏。
- 统一 UTF-8、换行与空白格式，移除软连字符等不可见噪声。
- 生成 SHA-256、字符保留率、幂等性及 `PASS` / `PASS_WARN` / `REVIEW` / `REJECT` 质量状态。
- 支持单文件处理、递归批处理、多线程和基于源文件/配置哈希的断点续跑。

## 环境要求

- Python 3.10 或更高版本
- 无第三方依赖

## 快速开始

### 1. 查看文档画像

只分析文档结构，不执行清洗：

```bash
python clean_markdown.py profile input.md \
  --config configs/ngspice_pilot.json \
  --output profile.json \
  --overwrite
```

### 2. 清洗单个 Markdown

```bash
python clean_markdown.py clean \
  --input-file input.md \
  --output-file output/cleaned.md \
  --report output/report.json \
  --artifact-dir output/artifacts \
  --config configs/ngspice_pilot.json
```

输出已存在时，可显式添加 `--overwrite`。

### 3. 批量清洗目录

```bash
python clean_markdown.py batch \
  --input-root data/raw \
  --output-root data/output \
  --config configs/ngspice_pilot.json \
  --workers 8 \
  --resume
```

`--resume` 仅在源内容哈希、配置哈希、清洗器版本及全部输出产物均一致时跳过文件；任意一项发生变化都会重新处理。

## 输出结构

批处理会在输出目录生成：

```text
output-root/
├── cleaned/                         # 清洗后的 Markdown
├── reports/                         # 每个文件的质量与审计报告
├── artifacts/
│   ├── *.tables.jsonl               # 转换后的表格记录
│   ├── *.images.jsonl               # 图片与图注审计记录
│   └── *.quarantined_tables.jsonl   # 待人工复核的异常表格
└── summary.json                     # 批处理汇总
```

清洗器禁止输入路径和输出路径相同，避免意外覆盖原始语料。

## 配置

默认策略位于 `clean_markdown.py` 的 `DEFAULT_CONFIG`。可通过 JSON 配置覆盖部分规则，示例见：

- `configs/ngspice_pilot.json`
- `RULES.md`

配置可控制目录和索引识别、图片策略、表格转换、标题修复、代码围栏处理及质量阈值。

## 测试

```bash
python -m unittest discover -s tests -v
```

当前测试覆盖 HTML 表格转换、合并单元格、重复键保留、图片审计、代码保护、目录删除、标题修复和幂等性。

## 使用边界

当前版本为面向 MinerU 技术手册的 `1.0.0-pilot`。对新的文档类型或领域语料，建议先构建代表性回归样本，检查 `REVIEW` / `REJECT` 报告后再进行全量处理。工具遵循保真优先原则，不主动猜测 OCR 数字、不改写公式，也不会自动删除正文中的悬空图引用。

## 项目结构

```text
technical_markdown_cleaner/
├── clean_markdown.py
├── configs/
│   └── ngspice_pilot.json
├── tests/
│   └── test_clean_markdown.py
├── pilot_profile.json
└── RULES.md
```

