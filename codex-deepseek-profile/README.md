# Codex DeepSeek Profile

让 Codex 使用 `deepseek-v4-flash`，同时保留默认的 OpenAI 配置。

## 安装

```bash
cd /Users/yoko/dev/codex-deepseek-profile
./codex-deepseek install
```

安装程序会下载 DeepSeek 官方安装脚本，但不会执行它，只从中提取最新的模型目录。安装过程只会创建以下两个文件：

- `~/.codex/deepseek.config.toml`
- `~/.codex/model-catalogs/deepseek.json`

API Key 从环境变量读取，不会写入磁盘：

```bash
export DEEPSEEK_API_KEY='你的 API Key'
```
如果希望重启终端后仍然生效，请将这条配置放进你自己的私密 Shell 环境配置中

## 切换模型

新建一个使用 DeepSeek 的 Codex 任务：

```bash
./codex-deepseek start
```

执行一次非交互任务：

```bash
./codex-deepseek exec '检查当前改动'
```

切回 OpenAI 时，像平常一样启动 Codex 即可：

```bash
codex
```

查看状态或删除 DeepSeek Profile：

```bash
./codex-deepseek status
./codex-deepseek uninstall
```

## 当前限制

截至 2026-08-02，DeepSeek 的 Codex 兼容 Responses API 只支持 `deepseek-v4-flash`。该接口是无状态的，不支持图片或文件输入，部分 Responses API 参数和内置工具也不受支持。

这里提供的是“新任务切换”，不能在当前任务中原地更换模型。Skill 可以调用这个启动器，但执行 Skill 的当前任务仍会继续使用原来的模型。
