# RSSHub 每日采集 + 统一处理

每天定时从 RSSHub 抓取中文资讯（社会 / 科技 / 金融 / 文化等），存入本地 SQLite，再统一生成一份 Markdown 日报。

## 环境要求

- Python 3.10+（建议 3.12+）
- 能访问 `rsshub.app`（公共实例，无需本地部署）

## 安装

```bash
cd E:\gitHubProject\rssHub数据采集
pip install -r requirements.txt
```

## 配置

编辑 `config.yaml`：

1. 在 `feeds` 下按分类填你要订阅的 RSSHub 路由（示例路由仅供占位，请到 <https://docs.rsshub.app/zh> 确认最新路径后替换）。
2. `settings` 里可改数据库路径、日报目录、回溯时长等。

## 运行

```bash
python collector.py run        # 采集 + 生成当日日报（推荐）
python collector.py collect    # 只采集
python collector.py process    # 只对今天已采集的数据生成日报
```

日报输出在 `digest/YYYY-MM-DD.md`。

## 可选：LLM 智能日报（统一处理）

默认 `llm_mode: auto`。只要设置了 `ANTHROPIC_API_KEY` 环境变量并 `pip install anthropic`，`process` 就会把当天所有条目一次性丢给 Claude，让它自动分类、挑重点、生成带一句话摘要的日报；没有 key 时自动退回本地汇总（只按分类列条目）。

```bash
set ANTHROPIC_API_KEY=你的密钥        # Windows
python collector.py run
```

## 定时任务（Windows 任务计划程序）

把下面的路径换成你自己的 Python 路径后执行：

```bash
schtasks /Create /TN "RSSHub每日采集" /TR "\"C:\你的Python路径\python.exe\" \"E:\gitHubProject\rssHub数据采集\collector.py\" run" /SC DAILY /ST 23:00
```

之后每天 23:00 自动跑一次，生成当天的日报。

## 文件说明

- `collector.py` —— 主脚本
- `config.yaml` —— 订阅源 + 设置
- `requirements.txt` —— 依赖
- `data/items.db` —— 本地数据库（首次运行自动创建）
- `digest/` —— 生成的日报
