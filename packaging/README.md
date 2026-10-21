# 大模型服务测试平台（免安装版）

单文件可执行程序，内置 Python 运行时、FastAPI 后端与 Vue 前端产物，目标机器无需安装
Python / pip / Node.js 即可启动服务。

## 启动

```bash
./start.sh                 # 默认 0.0.0.0:8000
HOST=127.0.0.1 PORT=8080 ./start.sh
./llm-server-eval --help        # 也可直接调用可执行文件
```

启动后浏览器访问 `http://<机器IP>:8000/`。停止用 `Ctrl+C`。

后台运行：

```bash
nohup ./llm-server-eval --host 0.0.0.0 --port 8000 > service.log 2>&1 &
```

## 目录约定

```
llm-server-eval-<版本>/
├── llm-server-eval     可执行文件（内置运行时，勿删除）
├── start.sh       启动脚本
├── dataset/       数据集与 Agent 任务（datasets.json 为清单）
├── model/         分词器目录，压力测试下拉框的候选来源
└── runs/          运行产物（任务记录、日志、CSV），请保持可写
```

`dataset/`、`model/`、`runs/` 必须与可执行文件**同级**。若要把数据放到别处，
设置环境变量 `APP_ROOT=/data/llm-server-eval` 再启动。前端产物已内置，不需要 `frontend/`。

## 可选外部组件

以下组件不随包分发，缺失时对应功能不可用，其余功能正常：

| 组件 | 用途 | 缺失时表现 |
|---|---|---|
| `evalscope`（`pip install 'evalscope[perf]'`） | 压力测试 | 页面提示 evalscope 不可用，压测无法启动；可用 `EVALSCOPE_BIN` 指定路径 |
| `pier` + `docker` | Agent 评测 | 页面提示 pier / docker 未就绪，Agent 评测无法启动 |

精度测试只依赖平台内置能力，无需额外安装。

## 环境要求

- Linux x86_64（本包在 Ubuntu 22.04 / glibc 2.35 上构建，目标机器 glibc 不低于该版本）
- 建议 2 核 4G 以上；Agent 评测另需 docker 可用

## 升级

用新版本的可执行文件覆盖 `llm-server-eval` 即可，`dataset/`、`model/`、`runs/` 保持不变；
如果新版本改了数据集清单，按需替换 `dataset/datasets.json`。
