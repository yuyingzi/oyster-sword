# Python LLM 批处理模板

适用于 macOS 或 Linux 上使用 Python 3.11+ 批量做文本编码、信息抽取或评测。每条 JSONL 记录对应一次模型请求，用 SQLite 保存输入和处理状态。任务的提示词、标签和输出 schema 集中在空白的 [`task.json`](task.json) 中，等你有具体任务时填写。模拟模式只依赖 Python 标准库。

## 跑通示例

从本目录执行以下命令。第二次运行会跳过三条已提交的记录，日志应显示 `scheduled=0` 与 `attempts=0`。

```bash
python3 -m llm_batch run --input examples/smoke.jsonl --db work/smoke.sqlite
python3 -m llm_batch run --input examples/smoke.jsonl --db work/smoke.sqlite
python3 -m llm_batch export --db work/smoke.sqlite --output work/results.jsonl
```

输入每行包含字符串 `id` 和 `text`。`id` 在文件内唯一，并由数据源稳定提供。导出按输入顺序包含已提交的成功和失败记录，输出文件名须未被占用。

```json
{"id":"sample-001","status":"succeeded","result":{},"error":null,"attempts":1}
```

用以下命令演练临时限流、无效输出和单次超时。第一条命令预期返回退出码 `2`，随后仍可以导出结果。重跑时追加 `--retry-failed` 会重新处理已提交的失败项，模拟失败仍会复现。

```bash
python3 -m llm_batch run --input examples/failures.jsonl --db work/failures.sqlite \
  --rpm 6000 --max-attempts 2 --request-timeout 0.15 --job-timeout 5 --progress-interval 0.2
python3 -m llm_batch export --db work/failures.sqlite --output work/failures.jsonl
```

退出码 `0` 表示批次无失败项，`2` 表示数据库中仍有记录级失败，`1` 表示配置、数据、服务商或其他执行错误，`130` 表示 Ctrl-C 中断。

## 接入自己的数据和模型

1. 把源数据转换成 UTF-8 JSONL，选择稳定的 `id`，把用于模型处理的字段组装为 `text`。保留相同输入文件以便续跑。
2. 填写 [`task.json`](task.json) 的 `prompt` 和 `output_schema`。有标签词表时再填写 `labels`，它会随提示词发给模型。字段名、字段类型、必填项和允许值由 JSON Schema 决定。如果标签需要严格校验，在对应字段的 schema 里用 `enum`。输出为 JSON 对象，内部可以包含数组、数值或嵌套对象。需要自定义配置路径时传入 `--task`。
3. 选择新数据库，用小样本跑通输入、校验、持久化、导出和续跑，再按服务商配额压测。实际 HTTP 适配器使用 `/chat/completions` 和 `max_completion_tokens`。如果你的兼容接口使用其他字段，请修改适配器的请求与响应映射。

```bash
python3 -m venv .venv
source .venv/bin/activate
python -m pip install -r requirements.txt
export LLM_BASE_URL='https://api.openai.com/v1'
export LLM_MODEL='your-model-id'
printf 'LLM API key: '
IFS= read -rs LLM_API_KEY
printf '\n'
export LLM_API_KEY
python -m llm_batch run --provider openai-compatible --task task.json \
  --input path/to/your-sample.jsonl --db work/api.sqlite \
  --workers 8 --queue-size 64 --concurrency 4 --rpm 60
```

`LLM_BASE_URL` 填接口根路径，如 `/v1`，适配器会追加 `/chat/completions`。真实请求请使用服务商的 HTTPS 地址，并按需要设定 `--tpm`。日志只记录运行指标、通用错误标识和必要的本地路径。SQLite 包含原始输入和模型结果，请按原数据的权限要求保存。

真实请求启动前会检查提示词和 JSON Schema Draft 2020-12。请将复用的 schema 定义放在同一配置的 `$defs` 内。模拟模式返回空对象 `{}`，可用来检查批处理、写入和导出。

## 模块与给 AI 的需求

| 模块 | 适用场景与常见错误 | 可以直接交给 AI 的需求 |
| --- | --- | --- |
| asyncio · `pipeline.py` | 远程 LLM 的 I/O 等待。同步 HTTP、`time.sleep()` 或同步写磁盘可能拖住事件循环。 | 把 HTTP、等待和关闭流程写成可取消的异步任务，用 `TaskGroup` 管理生命周期。运行期间的 SQLite 操作交给一个线程。 |
| bounded Queue · `pipeline.py` | 输入远多于同时能处理的数量。一次创建所有任务使内存随输入增长。 | 从 SQLite 分页取任务，用正数 `maxsize` 和 `await queue.put()`。队列满时暂停读取，每次 `get()` 必须和 `task_done()` 配对。 |
| worker pool · `pipeline.py` | 大量独立行复用固定数量的协程。worker 死亡被吞掉，可能导致队列永远无法结清。 | 只启动固定数量的 worker，每个一次处理一条。校验失败局部记录，未预期异常停止整批。 |
| semaphore · `pipeline.py` | 限制在途请求。每个 worker 自建信号量，或在退避时占着许可，会破坏控制。 | 所有 worker 共用一个 semaphore。许可覆盖单次调用，请求未获速率预算时立即释放许可，退避在许可外进行。 |
| RPM / TPM · `control.py` | 接口有单位时间配额。只调并发，或漏算重试，仍然会遇到 429。 | 均匀限制请求开始速率，按滚动 60 秒限制估算 token。首次和重试都走同一准入逻辑，用测试验证时间边界。 |
| 重试 · `provider.py`、`pipeline.py` | 429、408、指定 5xx、连接问题或超时。重试永久失败或同时启用 SDK 重试会增加负载。 | 总尝试数含首次，遵守 `Retry-After`，否则使用带随机扰动的指数退避。429 启动共享冷却，鉴权和已识别的配额耗尽停止整批。 |
| 超时 · `pipeline.py` | 单次请求挂起，或一条记录等待和重试太久。只配 socket 超时覆盖不了完整调用。 | 分别设定单次请求与 worker 取件后的处理截止时间，并统计两类超时。允许外层取消正常传播。 |
| checkpoint · `store.py` | 长任务中断后恢复。按行号恢复在并发乱序完成时会丢任务。 | 用记录 ID 和输入指纹识别运行，在同一数据库上默认跳过已提交记录，显式选择重跑失败，模拟进程被杀。 |
| 校验与持久化 · `provider.py`、`store.py` | 真实模型可能返回语法有效但业务字段错误的 JSON。先写完成标志再保存结果会留下不一致。 | 校验字段、类型和允许值。结果、状态、错误原因和尝试数在同一次提交中更新，导出按 ID 对齐并保留失败信息。 |
| 进度 · `pipeline.py` | 找出瓶颈与重试开销。发出请求数和已提交数口径不同，只看平均延迟会忽略尾部。 | 定期打印已提交数、队列深度、在途请求、重试、429、两类超时、成功吞吐和最近请求 P95。日志省略原文、响应正文和密钥。 |

## 参数与恢复规则

- `--workers 8` 限制同时由 worker 持有的记录数，`--concurrency 4` 限制同时发出的模型调用数，`--queue-size 64` 限制队列缓存的记录数。读取页以及 worker 已持有的记录也会占内存。
- `--rpm 60` 默认每秒最多开始一次请求。`--tpm 0` 默认关闭本地 token 预算，设为正数后按最近 60 秒累加估算量。每次预留使用输入和提示词的 UTF-8 字节数，再加输出 token 上限和固定开销。使用真实接口时按服务商的 token 计算调整 `estimate_tokens()`。多个进程共享服务商配额时，需要集中限流。重启会重置进程内的速率计数和冷却。
- `--request-timeout 45` 约束一次调用。`--job-timeout 180` 从 worker 取件起计时，包含准入等待、请求和退避。输入队列等待与 SQLite 提交在这两个超时之外。`--max-attempts 3` 包含首次。
- `--progress-interval 5` 控制日志间隔，`committed` 表示此次运行已提交的成功与失败条数，`goodput` 表示成功提交条数除以本次运行秒数。`request_p95` 以最新最多 1,024 次调用估算，从准入成功计时到单次调用及轻量响应处理结束。
- 同一数据库同时只允许一个模板进程运行。续跑时用原 JSONL、原任务配置和同一数据库。输入内容、提示词、标签、schema、接口、模型或输出上限变化时，使用新数据库。并发、速率、时间预算和最大尝试次数可在续跑时调整。早期情感分类版创建的数据库仍可导出，使用新版运行时请选择新数据库。
- 通常只会续跑 `pending` 项。已提交的 `failed` 项由 `--retry-failed` 显式纳入处理。若服务商已执行请求而 SQLite 尚未提交，续跑可能重发这条请求。导出的 `attempts` 累加已提交轮次中的尝试，进程在提交前被杀的尝试无法计入。服务端的幂等语义请按具体接口验证。

## 验证

```bash
python -m unittest discover -s tests -v
```

测试覆盖共享并发上限、RPM/TPM、`Retry-After`、重试与超时、无效输出、强制中断和恢复、重复 ID、任务配置变更以及导出。安装 `requirements.txt` 后也会运行 HTTPX transport 测试，使用模拟响应验证真实适配器的请求格式和错误处理。业务输出质量和服务商实际配额需用自己的小样本测量。
