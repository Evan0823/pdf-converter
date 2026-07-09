# PDF 转换器

PDF → Markdown / HTML / JSON / 纯文本，一站式在线转换工具。

## 功能

| 功能 | 说明 |
|------|------|
| 📄 PDF 转 Markdown | Docling 引擎 + Pandoc 渲染 |
| 🌐 PDF 转 HTML | 保留格式和表格 |
| 📊 PDF 转 JSON | 结构化提取 |
| 📝 PDF 转纯文本 | 无格式纯文字 |
| 🖼️ 图片提取 | PyMuPDF 提取 PDF 内嵌图片 |
| 🔄 两次处理 | 支持双栏 PDF / 扫描件优化 |
| 💾 缓存加速 | SHA256 哈希，相同文件秒级返回 |
| 📦 批量下载 | ZIP 打包所有输出格式 |

## 快速开始

```bash
# 安装依赖
pip install -r requirements.txt

# 启动服务
python server.py

# 浏览器打开
http://localhost:5000
```

## 依赖

| 依赖 | 用途 |
|------|------|
| `docling>=2.0` | PDF 解析引擎 |
| `flask>=3.0` | Web 服务 |
| `PyMuPDF` | 图片提取 |
| `pandoc` | Markdown 渲染（需系统安装） |

## 使用方式

1. 打开 `http://localhost:5000`
2. 拖拽或点击上传 PDF（最大 50MB）
3. 等待转换完成（进度条实时显示）
4. 选择输出格式下载或在线预览

## 项目结构

```
pdf-converter/
├── server.py          # Flask 主服务
├── templates/
│   └── index.html     # 前端界面
├── uploads/           # 上传文件（临时）
├── outputs/           # 转换结果
├── cache/             # SHA256 缓存
└── requirements.txt
```

## 踩坑备忘

详见 [[pdf-converter-lessons]]

- 改 HTML 后记得同步更新 JS 里的 DOM ID
- 「没反应」先看 F12 控制台，别瞎改 CSS
- 出 bug 先 `git stash` 回退，逐个功能加回来验证
